import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import json, tomllib, yaml
import knowledge_bridge.client_config as cc

class ClientConfigTests(unittest.TestCase):
    def test_connected_application_accepts_only_exact_existing_locator(self):
        from knowledge_bridge.portable_entry import locator_configuration
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            program=root/'application'/'.jiedu'/'program'
            program.mkdir(parents=True)
            control=program.parent
            (control/'package.json').write_text(json.dumps({
                'format_version':1,'layout':'jiedu-application-v1',
                'program':'program','control':'control'}),'utf-8')
            workspace=root/'workspace'
            (workspace/'.jiedu'/'control').mkdir(parents=True)
            (workspace/'.jiedu'/'workspace.json').write_text(json.dumps({'workspace_id':'fixture'}),'utf-8')
            reference=control/'control-reference.json'
            reference.write_text(json.dumps({'format':'jiedu-workspace-connection-v1',
                'workspace':str(workspace),'workspace_id':'fixture'}),'utf-8')
            local=root/'local'
            shell=local/'Microsoft'/'WindowsApps'/'pwsh.exe'
            shell.parent.mkdir(parents=True)
            shell.touch()
            locator=local/'Jiedu'/'jiedu_locator.ps1'
            locator.parent.mkdir(parents=True)
            locator.write_text('synthetic locator','utf-8')
            with patch.dict('os.environ',{'LOCALAPPDATA':str(local)}),patch.object(cc,'ROOT',program):
                entry=locator_configuration('fixture')
                self.assertTrue(cc._valid_entry(entry,'fixture'))
                changed={**entry,'args':entry['args'][:-1]+['other']}
                self.assertFalse(cc._valid_entry(changed,'fixture'))
                arbitrary={**entry,'args':['-Command','knowledge_bridge.cli']}
                self.assertFalse(cc._valid_entry(arbitrary,'fixture'))
                reference.unlink()
                self.assertFalse(cc._valid_entry(entry,'fixture'))
                reference.write_text('{invalid','utf-8')
                self.assertFalse(cc._valid_entry(entry,'fixture'))

    def test_application_codex_uses_bundled_stdio_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            program=root/'application'/'.jiedu'/'program'
            program.mkdir(parents=True)
            control=program.parent
            (control/'package.json').write_text(json.dumps({
                'format_version':1,'layout':'jiedu-application-v1',
                'program':'program','control':'control',
                'python':'runtime/python/python.exe'}),'utf-8')
            python=control/'runtime'/'python'/'python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            workspace=root/'workspace'
            local_data=workspace/'.jiedu'/'control'
            with patch.object(cc,'ROOT',program), patch.object(cc,'VAULT',workspace), \
                 patch.object(cc,'local_path',return_value=local_data):
                entry=tomllib.loads(cc.CodexAdapter().render('', 'fixture', 'fixture-secret'))['mcp_servers']['knowledge_bridge']
                self.assertEqual(str(python),entry['command'])
                self.assertEqual(['-I','-X','utf8','-m','knowledge_bridge.cli','mcp','--transport','stdio'],entry['args'])
                self.assertEqual(str(program),entry['cwd'])
                self.assertEqual(str(workspace),entry['env']['KNOWLEDGE_BRIDGE_VAULT'])
                self.assertEqual(str(local_data),entry['env']['KNOWLEDGE_BRIDGE_LOCAL_DATA'])
                self.assertEqual('fixture-secret',entry['env']['KNOWLEDGE_BRIDGE_CLIENT_SECRET'])
                self.assertTrue(cc._valid_entry(entry,'fixture'))

    def test_codex_preserves_other_servers_and_does_not_duplicate(self):
        adapter=cc.CodexAdapter(); old='[mcp_servers.other]\ncommand = "other.exe"\n\n'
        first=adapter.render(old); second=adapter.render(first)
        data=tomllib.loads(second)
        self.assertEqual("other.exe",data["mcp_servers"]["other"]["command"])
        self.assertEqual(1,second.count("[mcp_servers.knowledge_bridge]"))

    def test_hermes_preserves_other_settings(self):
        adapter=cc.HermesAdapter(); old='theme: dark\nmcp_servers:\n  other:\n    command: other\n'
        result=yaml.safe_load(adapter.render(old))
        self.assertEqual("dark",result["theme"]); self.assertIn("other",result["mcp_servers"]); self.assertIn("knowledge_bridge",result["mcp_servers"])

    def test_hermes_preview_ignores_unrelated_remote_settings_but_rejects_remote_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/"config.yaml"
            target.write_text('models:\n  provider_url: https://model.example.invalid/v1\nserver:\n  bind: 0.0.0.0\n','utf-8')
            adapter=cc.HermesAdapter();adapter.path=target
            with patch.dict(cc.ADAPTERS,{"hermes":adapter},clear=True), patch.object(cc,"DATA",root/"state"):
                result=cc.preview("hermes",ephemeral=True)
                parsed=yaml.safe_load(result["config"])
                self.assertEqual("https://model.example.invalid/v1",parsed["models"]["provider_url"])
                self.assertEqual("0.0.0.0",parsed["server"]["bind"])
                self.assertIn("knowledge_bridge",parsed["mcp_servers"])
                with patch.object(adapter,"render",return_value='mcp_servers:\n  knowledge_bridge:\n    command: https://outside.example.invalid/mcp\n'):
                    with self.assertRaisesRegex(ValueError,"安全策略拒绝非本机配置"):
                        cc.preview("hermes",ephemeral=True)

    def test_preview_apply_backup_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/"config.toml"; target.write_text('[mcp_servers.other]\ncommand="ok"\n','utf-8')
            adapter=cc.CodexAdapter(); adapter.path=target
            with patch.dict(cc.ADAPTERS,{"codex":adapter},clear=True), patch.object(cc,"DATA",root/"data"), patch.object(cc,"BACKUPS",root/"data"/"backups"), patch.object(cc,"AUDIT",root/"data"/"audit.jsonl"), patch.object(cc,"PENDING",root/"data"/"pending.json"):
                preview=cc.preview("codex"); result=cc.apply(preview["token"])
                self.assertTrue(result["applied"]); self.assertTrue(Path(result["backup"]).exists()); self.assertIn("knowledge_bridge",target.read_text("utf-8"))
                remove=cc.preview("codex","remove"); cc.apply(remove["token"]); self.assertNotIn("knowledge_bridge",target.read_text("utf-8")); self.assertIn("other",target.read_text("utf-8"))

    def test_legacy_reconfigure_cannot_strip_existing_pairing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/"config.toml"
            target.write_text('[mcp_servers.knowledge_bridge]\ncommand="old.exe"\n[mcp_servers.knowledge_bridge.env]\nKNOWLEDGE_BRIDGE_CLIENT_SECRET="paired-secret"\n','utf-8')
            adapter=cc.CodexAdapter();adapter.path=target
            with patch.dict(cc.ADAPTERS,{"codex":adapter},clear=True):
                with self.assertRaisesRegex(ValueError,"修复连接"):
                    cc.preview("codex",ephemeral=True)
                with self.assertRaisesRegex(ValueError,"修复连接"):
                    cc.preview("codex","remove",ephemeral=True)
            self.assertIn('paired-secret',target.read_text('utf-8'))

if __name__=="__main__": unittest.main()
