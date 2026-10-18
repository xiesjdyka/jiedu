import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import knowledge_bridge.memory_control as mc

class ClientPermissionTests(unittest.TestCase):
    def test_three_level_permission_chain_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/"permissions.json"
            with patch.object(mc,"INTERFACE_STATE",state):
                self.assertFalse(mc.interface_state()["clients"]["codex"]["enabled"])
                mc.update_interface("mcp",True);mc.update_client_permission("codex",True)
                mc.require_interface("memory_bootstrap",True,"codex")
                with self.assertRaises(PermissionError):mc.require_interface("memory_bootstrap",True,"hermes")
                mc.update_client_permission("codex",False);mc.update_client_permission("hermes",True)
                mc.require_interface("memory_bootstrap",True,"hermes")
                with self.assertRaises(PermissionError):mc.require_interface("memory_bootstrap",True,"codex")
                mc.update_client_permission("hermes",False,"memory_bootstrap")
                with self.assertRaisesRegex(PermissionError,"该能力未授权"):mc.require_interface("memory_bootstrap",True,"hermes")
                self.assertTrue(json.loads(state.read_text("utf-8"))["clients"]["hermes"]["enabled"])
                mc.update_interface("mcp",False)
                with self.assertRaises(PermissionError):mc.require_interface("search_memory",True,"hermes")
