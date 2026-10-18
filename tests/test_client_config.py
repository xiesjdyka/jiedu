import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import tomllib, yaml
import knowledge_bridge.client_config as cc

class ClientConfigTests(unittest.TestCase):
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

    def test_preview_apply_backup_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/"config.toml"; target.write_text('[mcp_servers.other]\ncommand="ok"\n','utf-8')
            adapter=cc.CodexAdapter(); adapter.path=target
            with patch.dict(cc.ADAPTERS,{"codex":adapter},clear=True), patch.object(cc,"DATA",root/"data"), patch.object(cc,"BACKUPS",root/"data"/"backups"), patch.object(cc,"AUDIT",root/"data"/"audit.jsonl"), patch.object(cc,"PENDING",root/"data"/"pending.json"):
                preview=cc.preview("codex"); result=cc.apply(preview["token"])
                self.assertTrue(result["applied"]); self.assertTrue(Path(result["backup"]).exists()); self.assertIn("knowledge_bridge",target.read_text("utf-8"))
                remove=cc.preview("codex","remove"); cc.apply(remove["token"]); self.assertNotIn("knowledge_bridge",target.read_text("utf-8")); self.assertIn("other",target.read_text("utf-8"))

if __name__=="__main__": unittest.main()
