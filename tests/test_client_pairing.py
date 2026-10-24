import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_bridge.client_config as cc
from knowledge_bridge.client_access import ClientAccessGate, ClientIdentityRegistry, ClientPermissionService, PolicyDecision
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.source_connections import SourceConnections
from knowledge_bridge.workspace import initialize_workspace


class PairingTransactionTests(unittest.TestCase):
    def harness(self, root: Path):
        context=initialize_workspace(root/"workspace")
        registry=ClientIdentityRegistry(context,root/"machine")
        permissions=ClientPermissionService(registry)
        adapter=cc.CodexAdapter();adapter.path=root/"codex"/"config.toml"
        adapter.path.parent.mkdir(parents=True);adapter.path.write_text(
            '[mcp_servers.existing]\ncommand = "existing.exe"\n',"utf-8")
        patches=(patch.dict(cc.ADAPTERS,{"codex":adapter},clear=True),
                 patch.object(cc,"DATA",root/"config-state"),
                 patch.object(cc,"BACKUPS",root/"config-state"/"backups"),
                 patch.object(cc,"AUDIT",root/"config-state"/"audit.jsonl"),
                 patch.object(cc,"PENDING",root/"config-state"/"pending.json"))
        return registry,permissions,adapter,patches

    def test_preview_cancel_and_commit_have_no_ghost_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                original=adapter.path.read_text("utf-8")
                chosen={"formal":"allow","evidence":"allow","inbox":"deny","formal_write":"ask","archive_write":"deny"}
                pending=manager.begin("codex","Codex","codex","stdio",chosen)
                secret=pending["secret"]
                self.assertEqual([],registry.list_clients())
                self.assertEqual(original,adapter.path.read_text("utf-8"))
                self.assertNotIn(secret,pending["config"]["diff"])
                self.assertIn("配对秘密",pending["config"]["diff"])
                self.assertFalse((root/"config-state"/"pending.json").exists())
                manager.cancel(pending["pairing_id"])
                self.assertEqual([],registry.list_clients())
                self.assertEqual(original,adapter.path.read_text("utf-8"))
                state_path=registry.context.sync/'codex-source-adapter.json'
                self.assertFalse(state_path.exists())

                pending=manager.begin("codex","Codex","codex","stdio",chosen)
                result=manager.commit(pending["pairing_id"])
                self.assertTrue(result["paired"]);self.assertTrue(result["restart_required"])
                self.assertEqual('enabled',result['auto_archive'])
                self.assertEqual('all',json.loads(state_path.read_text('utf-8'))['archive_policy'])
                self.assertEqual(1,len(registry.list_clients()))
                text=adapter.path.read_text("utf-8")
                self.assertIn("KNOWLEDGE_BRIDGE_CLIENT_SECRET",text)
                self.assertIn(pending["secret"],text)
                policies={(row["scope"],row["operation"]):row["decision"]
                          for row in permissions.policies(result["client"]["internal_id"])}
                self.assertEqual("allow",policies[("formal","search")])
                self.assertEqual("allow",policies[("evidence","search")])
                self.assertEqual("allow",policies[("evidence","read")])
                self.assertEqual("deny",policies[("protected","read_protected")])
                self.assertEqual("deny",policies[("evidence","raw_archive")])

                identity=registry.authenticate("codex",pending["secret"]);gate=ClientAccessGate(permissions,identity)
                for query in ("fictional first","fictional second"):
                    self.assertEqual(PolicyDecision.ALLOW,gate.search(query,7,True)[1].outcome)
                for session_id in ("fictional-session-a","fictional-session-b"):
                    self.assertEqual(PolicyDecision.ALLOW,gate.session_archive(session_id,5)[1].outcome)
                self.assertEqual([],permissions.pending())
                self.assertEqual(PolicyDecision.ASK,permissions.authorize(identity,"formal","submit_candidate").outcome)
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(identity,"protected","read_protected").outcome)
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(identity,"evidence","raw_archive").outcome)
                unauthenticated=registry.authenticate("codex",None)
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(unauthenticated,"evidence","read",{"session_id":"fictional"}).outcome)

    def test_quick_connect_creates_once_and_preserves_existing_identity_and_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                first=manager.quick_connect('codex')
                self.assertEqual('paired',first['outcome'])
                self.assertNotIn('secret',str(first).lower())
                client_id=first['client']['internal_id']
                permissions.set_policy(client_id,'formal','read','deny')
                config=adapter.path.read_bytes()
                second=manager.quick_connect('codex')
                self.assertEqual('reused',second['outcome'])
                self.assertEqual(client_id,second['client']['internal_id'])
                self.assertEqual(config,adapter.path.read_bytes())
                self.assertEqual('deny',next(row['decision'] for row in permissions.policies(client_id)
                                             if row['scope']=='formal' and row['operation']=='read'))
                self.assertEqual(1,len(registry.list_clients()))

    def test_quick_connect_preserves_legacy_configuration_without_new_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                adapter.path.write_text('[mcp_servers.knowledge_bridge]\ncommand = "old.exe"\n', 'utf-8')
                before=adapter.path.read_bytes()
                result=PairingTransactionManager(registry,permissions).quick_connect('codex')
                self.assertEqual('existing_configuration',result['outcome'])
                self.assertEqual(before,adapter.path.read_bytes())
                self.assertEqual([],registry.list_clients())

    def test_quick_connect_rejects_missing_client_without_ghost_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            adapter.path.unlink();adapter.path.parent.rmdir()
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                with self.assertRaisesRegex(ValueError,'尚未检测到'):
                    PairingTransactionManager(registry,permissions).quick_connect('codex')
                self.assertEqual([],registry.list_clients())

    def test_quick_connect_hermes_custom_home_pairs_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,_,patches=self.harness(root)
            custom=root/'chosen-hermes';custom.mkdir()
            with patch.dict(os.environ,{'HERMES_HOME':str(custom),
                                         'HERMES_DESKTOP_USER_DATA_DIR':'',
                                         'LOCALAPPDATA':str(root/'unused-local')}), \
                 patch.object(Path,'home',return_value=root/'unused-home'):
                adapter=cc.HermesAdapter()
            self.assertEqual(custom/'config.yaml',adapter.path)
            self.assertTrue(adapter.detect()['installed'])
            with patch.dict(cc.ADAPTERS,{'hermes':adapter},clear=True), \
                 patches[1],patches[2],patches[3],patches[4], \
                 patch('knowledge_bridge.hermes_session_adapter.enable_history_for_first_pairing',
                       return_value='waiting_for_source'):
                manager=PairingTransactionManager(registry,permissions)
                first=manager.quick_connect('hermes')
                self.assertEqual('paired',first['outcome'])
                self.assertNotIn('secret',str(first).lower())
                self.assertTrue(adapter.path.is_file())
                client_id=first['client']['internal_id']
                permissions.set_policy(client_id,'formal','read','deny')
                config=adapter.path.read_bytes()
                second=manager.quick_connect('hermes')
                self.assertEqual('reused',second['outcome'])
                self.assertEqual(config,adapter.path.read_bytes())
                self.assertEqual('deny',next(row['decision'] for row in permissions.policies(client_id)
                                             if row['scope']=='formal' and row['operation']=='read'))
                self.assertEqual(1,len(registry.list_clients()))

    def test_failure_after_config_apply_rolls_back_config_and_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                original=adapter.path.read_bytes();pending=manager.begin("codex","Codex","codex","stdio")
                with patch.object(registry,"commit_prepared",side_effect=OSError("fictional commit failure")):
                    with self.assertRaises(OSError):manager.commit(pending["pairing_id"])
                self.assertEqual(original,adapter.path.read_bytes())
                self.assertEqual([],registry.list_clients())
                self.assertEqual([],permissions.policies(pending["client"]["internal_id"]))
                self.assertFalse((registry.context.sync/'codex-source-adapter.json').exists())

    def test_rotation_preview_is_secret_free_and_commit_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                pairing=manager.begin("codex","Codex","codex","stdio")
                committed=manager.commit(pairing["pairing_id"]);old=pairing["secret"]
                rotation=manager.begin_rotation(committed["client"]["internal_id"])
                self.assertNotIn(pairing["secret"],str(rotation))
                self.assertNotIn("secret",rotation)
                self.assertTrue(registry.authenticate("codex",old).authenticated)
                before=adapter.path.read_bytes()
                with patch.object(registry,"commit_rotation",side_effect=OSError("fictional rotation failure")):
                    with self.assertRaises(OSError):manager.commit_rotation(rotation["rotation_id"])
                self.assertEqual(before,adapter.path.read_bytes())
                self.assertTrue(registry.authenticate("codex",old).authenticated)

    def test_existing_client_repair_rewrites_entry_without_new_identity_or_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                first=manager.begin('codex','Codex','codex','stdio')
                paired=manager.commit(first['pairing_id'])['client']
                internal_id=paired['internal_id']
                before_policies=permissions.policies(internal_id)
                before_source=SourceConnections(registry.context).status(internal_id)
                replacement=root/'repaired-python.exe';replacement.touch()
                def repaired_command(client_id='other',secret=None):
                    return {'command':str(replacement),'args':['-m','knowledge_bridge.cli','mcp'],
                            'cwd':str(root),'env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':client_id,
                                                 'KNOWLEDGE_BRIDGE_CLIENT_SECRET':secret}}
                with patch.object(cc,'_command',side_effect=repaired_command):
                    rotation=manager.begin_rotation(internal_id)
                    new_secret=manager._pending[rotation['rotation_id']]['prepared']['secret']
                    self.assertNotIn(new_secret,str(rotation))
                    result=manager.commit_rotation(rotation['rotation_id'])
                self.assertEqual(internal_id,result['client']['internal_id'])
                self.assertEqual(1,len(registry.list_clients()))
                self.assertEqual(before_policies,permissions.policies(internal_id))
                self.assertEqual(before_source,SourceConnections(registry.context).status(internal_id))
                entry=tomllib.loads(adapter.path.read_text('utf-8'))['mcp_servers']['knowledge_bridge']
                self.assertEqual(str(replacement),entry['command'])
                self.assertFalse(registry.authenticate('codex',first['secret']).authenticated)
                self.assertTrue(registry.authenticate('codex',new_secret).authenticated)

    def test_static_flow_uses_direct_confirmation_without_checkbox(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/"static"/"index.html").read_text("utf-8")
        script=(root/"static"/"app.js").read_text("utf-8")
        self.assertIn("确认配对并应用配置",html)
        self.assertNotIn("clientSecretSaved",html+script)
        self.assertIn("showRotationPreview",script)
        self.assertIn("'rotations':'pairings'",script)
        self.assertNotIn("showRotatedSecret",script)
        self.assertIn("data-pair-permission=\"evidence\"",html)
        self.assertIn("data-pair-permission=\"formal_write\"",html)
        self.assertIn('data-pair-permission="inbox_write"',html)
        self.assertIn('<option value="allow" selected>允许提交候选</option>',html)
        self.assertIn('restore-recommended',script)
        self.assertIn('历史补齐＋新消息',script)
        self.assertIn("read_verified",script)
        self.assertIn("/static/app.js?v=",html)

    def test_presets_suggest_unique_ids_and_brand_does_not_choose_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                first=manager.begin("codex","Codex","codex","stdio")
                presets={row['id']:row for row in manager.presets()}
                self.assertEqual('codex_2',presets['codex']['suggested_claimed_id'])
                self.assertEqual(('Codex','Hermes Agent','自定义'),tuple(presets[x]['label'] for x in ('codex','hermes','custom')))
                self.assertEqual('verified_template',presets['codex']['configuration'])
                self.assertEqual('manual_fields',presets['custom']['configuration'])
                self.assertTrue(all(row['decision']=='allow' for row in first['initial_policies'] if row['scope']=='formal' and row['operation'] in {'search','read','context'}))
                self.assertEqual('allow',next(row['decision'] for row in first['initial_policies'] if row['operation']=='raw_archive'))
                self.assertEqual('allow',next(row['decision'] for row in first['initial_policies'] if row['operation']=='submit_candidate'))
                self.assertEqual('allow',next(row['decision'] for row in first['initial_policies'] if row['operation']=='save_inbox'))
                chosen={'formal':'deny','evidence':'allow','inbox':'deny','formal_write':'ask','archive_write':'deny'}
                automatic=manager.begin('codex_custom_policy','Codex','codex','stdio',chosen)
                manual=manager.begin('custom_policy','Custom','custom','stdio',chosen,'Test client')
                policies=lambda result:{(x['scope'],x['operation']):x['decision'] for x in result['initial_policies']}
                self.assertEqual(policies(automatic),policies(manual))
                self.assertEqual('deny',policies(automatic)[('formal','read')])
                self.assertEqual('allow',policies(automatic)[('evidence','read')])

    def test_existing_pause_is_preserved_and_recommended_restore_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            state_path=registry.context.sync/'codex-source-adapter.json'
            state_path.write_text(json.dumps({'archive_policy':'paused','resume_policy':'selected',
                                              'sessions':{},'selected_session_ids':[]}),encoding='utf-8')
            original=state_path.read_bytes()
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                pending=manager.begin('codex','Codex','codex','stdio')
                result=manager.commit(pending['pairing_id'])
                self.assertEqual('existing_settings_preserved',result['auto_archive'])
                self.assertEqual(original,state_path.read_bytes())
                identity=registry.authenticate('codex',pending['secret'])
                self.assertEqual(PolicyDecision.ALLOW,permissions.authorize(identity,'inbox','save_inbox').outcome)
                self.assertEqual(PolicyDecision.ALLOW,permissions.authorize(identity,'formal','submit_candidate').outcome)
                permissions.set_policy(identity.internal_id,'inbox','save_inbox','deny')
                permissions.set_policy(identity.internal_id,'formal','search','deny',{'project_id':'private'})
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(identity,'inbox','save_inbox').outcome)
                permissions.restore_recommended(identity.internal_id)
                self.assertEqual(PolicyDecision.ALLOW,permissions.authorize(identity,'inbox','save_inbox').outcome)
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(identity,'formal','search',{'project_id':'private'}).outcome)
                self.assertEqual(PolicyDecision.DENY,permissions.authorize(identity,'protected','read_protected').outcome)


    def test_doubao_preset_binds_local_collector_without_editing_client_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);registry,permissions,adapter,patches=self.harness(root)
            with patches[0],patches[1],patches[2],patches[3],patches[4]:
                manager=PairingTransactionManager(registry,permissions)
                original=adapter.path.read_bytes()
                preview=manager.begin('doubao_work','豆包工作版','doubao_work','stdio')
                self.assertFalse(preview['manual_configuration'])
                self.assertEqual('local_source_auto_detect',next(
                    row['configuration'] for row in manager.presets() if row['id']=='doubao_work'))
                result=manager.commit(preview['pairing_id'])
                self.assertTrue(result['local_source_collection'])
                self.assertFalse(result['manual_configuration'])
                self.assertEqual(original,adapter.path.read_bytes())
                binding=SourceConnections(registry.context).load()['connections'][result['client']['internal_id']]
                self.assertEqual('doubao_work',binding['source_key'])


if __name__=="__main__":unittest.main()
