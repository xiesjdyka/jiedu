import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import knowledge_bridge.client_config as cc
import knowledge_bridge.claude_extension as extension
import knowledge_bridge.web as web
from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.workspace import initialize_workspace


class BrandedConnectTests(unittest.TestCase):
    def test_dsh_preserves_unrelated_patch_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'profiles'/'desktop'/'cordis.patch.yml'
            path.parent.mkdir(parents=True)
            adapter=cc.DSHAdapter(path)
            original='- insert:\n  - id: unrelated\n    name: other-plugin\n    config:\n      serverName: other\n      script: !!js "return 1"\n'
            updated=adapter.render(original,'dsh_fixture','fixture-secret')
            adapter.validate(updated)
            self.assertIn(original.rstrip(),updated)
            self.assertEqual(1,updated.count(cc._DSH_BEGIN))
            self.assertEqual(original.rstrip(),adapter.remove(updated).rstrip())
            with self.assertRaises(ValueError):adapter.render('- insert:\n  - id: x\n    config:\n      serverName: "knowledge_bridge"\n')
            with self.assertRaises(ValueError):adapter.render('mapping: not-a-patch-list\n')
            with self.assertRaises(ValueError):adapter.render(updated+'\n'+updated)

    def test_second_identity_is_reused_without_touching_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            registry=ClientIdentityRegistry(context,root/'machine')
            permissions=ClientPermissionService(registry)
            first=registry.create('codex_first','First','codex','stdio')
            second=registry.create('codex_second','Second','codex','stdio')
            permissions.set_policy(first['internal_id'],'formal','read','deny')
            adapter=cc.CodexAdapter()
            adapter.path=root/'codex'/'config.toml'
            adapter.path.parent.mkdir()
            adapter.path.write_text(adapter.render('',second['claimed_id'],second['secret']),'utf-8')
            before=adapter.path.read_bytes()
            manager=PairingTransactionManager(registry,permissions)
            with patch.dict(cc.ADAPTERS,{'codex':adapter}), patch.object(manager,'_ensure_existing_collection',return_value='existing_settings_preserved'):
                result=manager.quick_connect('codex')
            self.assertEqual('reused',result['outcome'])
            self.assertEqual(second['internal_id'],result['client']['internal_id'])
            self.assertEqual(before,adapter.path.read_bytes())
            self.assertEqual('deny',next(row['decision'] for row in permissions.policies(first['internal_id'])
                                    if row['scope']=='formal' and row['operation']=='read'))

    def test_dsh_quick_connect_appends_only_its_own_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            registry=ClientIdentityRegistry(context,root/'machine')
            permissions=ClientPermissionService(registry)
            path=root/'dsh'/'profiles'/'desktop'/'cordis.patch.yml'
            path.parent.mkdir(parents=True)
            original='- insert:\n  - id: keep-this\n    name: other-plugin\n    config:\n      serverName: another-service\n'
            path.write_text(original,'utf-8')
            adapter=cc.DSHAdapter(path)
            manager=PairingTransactionManager(registry,permissions)
            with patch.dict(cc.ADAPTERS,{'dsh':adapter}), \
                 patch.object(cc,'DATA',root/'config-state'), \
                 patch.object(cc,'BACKUPS',root/'config-state'/'backups'), \
                 patch.object(cc,'AUDIT',root/'config-state'/'audit.jsonl'), \
                 patch.object(cc,'PENDING',root/'config-state'/'pending.json'), \
                 patch.object(manager,'_ensure_existing_collection',return_value='waiting_for_source'):
                result=manager.quick_connect('dsh')
            self.assertEqual('paired',result['outcome'])
            updated=path.read_text('utf-8')
            self.assertIn(original.rstrip(),updated)
            self.assertEqual(1,updated.count(cc._DSH_BEGIN))
            self.assertEqual(1,len(registry.list_clients()))

    def test_claude_bundle_is_staged_only_and_owner_download_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'jiedu-claude.mcpb'
            extension.prepare_bundle('claude_fixture','fixture-secret',path)
            self.assertEqual('claude_fixture',extension.inspect_bundle(path)['client_id'])
            self.assertFalse((path.parent/'claude_desktop_config.json').exists())
            self.assertIn('/api/client-access/claude-extension/download',
                          [route.path for route in web.app.routes])
            request=SimpleNamespace(headers={'origin':'http://127.0.0.1:8765','host':'127.0.0.1:8765',
                                             'content-type':'application/json'},
                                    url=SimpleNamespace(scheme='http',hostname='127.0.0.1'))
            web._same_origin(request)
            with self.assertRaises(HTTPException):
                web._same_origin(SimpleNamespace(headers={'origin':'http://127.0.0.1:8765',
                     'host':'127.0.0.1:8765'},url=request.url))
            registry=SimpleNamespace(list_clients=lambda:[{'client_type':'claude','status':'paired_not_seen',
                                                              'claimed_id':'claude_fixture','internal_id':'fixture'}],
                                     configured_secret_matches=lambda _id, secret:secret=='fixture-secret')
            service=SimpleNamespace(registry=registry)
            with patch.object(web,'_same_origin'), patch.object(web,'_owner',side_effect=HTTPException(403,'Owner required')):
                with self.assertRaises(HTTPException) as denied:
                    web.api_claude_extension_download(SimpleNamespace())
            self.assertEqual(403,denied.exception.status_code)
            inspect=extension.inspect_bundle
            with patch.object(web,'_same_origin'), patch.object(web,'_owner'), \
                 patch.object(web,'_pairing_service',return_value=service), \
                 patch.object(extension,'BUNDLE',path), \
                 patch.object(extension,'inspect_bundle',side_effect=lambda:inspect(path)):
                response=web.api_claude_extension_download(SimpleNamespace())
            self.assertEqual(path,response.path)
            self.assertEqual('no-store',response.headers['cache-control'])

    def test_claude_without_chat_cache_can_prepare_extension_without_source_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            registry=ClientIdentityRegistry(context,root/'machine')
            permissions=ClientPermissionService(registry)
            profile_root=root/'Claude'
            profile_root.mkdir()
            database=profile_root/'IndexedDB'/'https_claude.ai_0.indexeddb.leveldb'
            profiles={'desktop':('桌面版',database,profile_root/'IndexedDB'/'blob')}
            bundle=root/'private'/'jiedu-claude.mcpb'
            manager=PairingTransactionManager(registry,permissions)
            with patch('knowledge_bridge.claude_source_adapter.profile_paths',return_value=profiles), \
                 patch.object(extension,'BUNDLE',bundle):
                result=manager.quick_connect('claude')
            self.assertEqual('paired',result['outcome'])
            self.assertEqual('waiting_for_source',result['auto_archive'])
            self.assertEqual('/api/client-access/claude-extension/download',result['extension_download'])
            self.assertTrue(bundle.is_file())
            self.assertFalse((context.sync/'claude-source-adapter.json').exists())


if __name__=='__main__':unittest.main()
