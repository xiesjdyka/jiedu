import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.knowledge_objects import KnowledgeObjectService
from knowledge_bridge.protection_control import ProtectionLifecycleManager, OwnerSessionRequired
from knowledge_bridge.workspace import initialize_workspace


class SessionProtectionTests(unittest.TestCase):
    def test_browsing_survives_admin_expiry_but_not_lock_or_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=initialize_workspace(root/'workspace');clock=[100.0]
            manager=ProtectionLifecycleManager(context,settings_path=root/'machine.json',clock=lambda:clock[0],owner_session_seconds=3)
            _,credentials=manager.initialize('fictional-session-password')
            clock[0]+=10
            self.assertTrue(manager.status(credentials.session_token)['browser_session_active'])
            with self.assertRaises(OwnerSessionRequired):manager.owner_session(credentials.session_token)
            manager.browser_session(credentials.session_token,credentials.csrf_token,require_csrf=True)
            manager.lock()
            with self.assertRaises(OwnerSessionRequired):manager.browser_session(credentials.session_token)
            renewed=manager.unlock_with_passphrase('fictional-session-password')
            manager.close()
            restarted=ProtectionLifecycleManager(context,settings_path=root/'machine.json')
            with self.assertRaises(OwnerSessionRequired):restarted.browser_session(renewed.session_token)

    def test_session_grants_are_read_only_and_identity_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=initialize_workspace(root/'workspace')
            registry=ClientIdentityRegistry(context,root/'machine');permissions=ClientPermissionService(registry)
            one=registry.create('fixture_one','One','test','stdio');two=registry.create('fixture_two','Two','test','stdio')
            identity=registry.authenticate('fixture_one',one['secret']);other=registry.authenticate('fixture_two',two['secret'])
            with self.assertRaises(ValueError):
                permissions.grant_session_read(one['internal_id'],'epoch-one',['protected','inbox'])
            permissions.grant_session_read(one['internal_id'],'epoch-one',['protected'])
            def decision(who,scope,operation,epoch='epoch-one'):
                return permissions.authorize(who,scope,operation,protection_session=epoch).outcome.value
            self.assertEqual('allow',decision(identity,'protected','read_protected'))
            self.assertEqual('deny',decision(identity,'inbox','read'))
            for who,scope,op,epoch in [(other,'protected','read_protected','epoch-one'),(identity,'protected','read_protected','epoch-two'),(identity,'protected','export_protected','epoch-one'),(identity,'protected','delete','epoch-one'),(identity,'formal','submit_candidate','epoch-one')]:
                self.assertEqual('deny',decision(who,scope,op,epoch))
            permissions.grant_session_read(one['internal_id'],'epoch-one',[])
            self.assertEqual('deny',decision(identity,'protected','read_protected'))
            permissions.grant_session_read(one['internal_id'],'epoch-one',['protected'])
            registry.rotate(one['internal_id'])
            self.assertEqual('deny',decision(identity,'protected','read_protected'))

    def test_protected_preview_original_and_derived_do_not_leak_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=initialize_workspace(root/'workspace')
            manager=ProtectionLifecycleManager(context,settings_path=root/'machine.json')
            manager.initialize('fictional-session-password')
            source=root/'outside';source.mkdir();marker='PrismSensitiveFixture981'
            original=source/(marker+'.txt');original.write_text(marker,'utf-8')
            service=KnowledgeObjectService(context,manager.provider)
            preview=service.preview_import(str(source),'protected')
            self.assertFalse(service.preview_root.exists())
            result=service.execute_import(preview['preview_id'],owner_confirmed=True)
            self.assertEqual(1,result['counts']['protected'])
            objects=service.list_objects(include_protected=True)['items']
            encrypted=[r for r in objects if r['policy']=='protected']
            self.assertEqual(2,len(encrypted))
            self.assertTrue(all(marker in service.object_detail(r['entry_id'],include_protected=True)['content'] for r in encrypted))
            for path in context.root.rglob('*'):
                self.assertNotIn(marker,str(path))
                if path.is_file():self.assertNotIn(marker.encode(),path.read_bytes(),str(path))
            self.assertEqual(marker,original.read_text('utf-8'))
            manager.lock()
            self.assertEqual([], [r for r in service.list_objects()['items'] if r['policy']=='protected'])
            with self.assertRaises(PermissionError):service.object_detail(encrypted[0]['entry_id'],include_protected=True)


if __name__=='__main__':unittest.main()
