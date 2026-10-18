import tempfile
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

                pending=manager.begin("codex","Codex","codex","stdio",chosen)
                result=manager.commit(pending["pairing_id"])
                self.assertTrue(result["paired"]);self.assertTrue(result["restart_required"])
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
                chosen={'formal':'deny','evidence':'allow','inbox':'deny','formal_write':'ask','archive_write':'deny'}
                automatic=manager.begin('codex_custom_policy','Codex','codex','stdio',chosen)
                manual=manager.begin('custom_policy','Custom','custom','stdio',chosen,'Test client')
                policies=lambda result:{(x['scope'],x['operation']):x['decision'] for x in result['initial_policies']}
                self.assertEqual(policies(automatic),policies(manual))
                self.assertEqual('deny',policies(automatic)[('formal','read')])
                self.assertEqual('allow',policies(automatic)[('evidence','read')])


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
