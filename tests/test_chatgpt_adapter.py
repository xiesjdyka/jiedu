import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import chatgpt_adapter as adapter
from knowledge_bridge import memory_control as control
from knowledge_bridge import mcp_audit


class ChatGPTAdapterTests(unittest.TestCase):
    def test_conversation_bridge_automatically_retrieves_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);permissions=root/"permissions.json";state=root/"adapter.json"
            with patch.object(control,"INTERFACE_STATE",permissions),patch.object(mcp_audit,"AUDIT_PATH",root/"audit.jsonl"),patch.object(adapter,"STATE_PATH",state),patch.object(adapter,"require_interface",control.require_interface),patch.object(adapter,"record_call",control.record_call),patch.object(adapter,"interface_state",control.interface_state),patch.object(adapter,"_user_context",return_value={"documents":[{"path":"00_系统/画像.md","content":"用户背景"}],"read_only":True}),patch.object(adapter,"_detect_project",return_value=None),patch.object(adapter,"knowledge_search",return_value={"results":[{"source_path":"02_项目/界渡/当前状态.md","excerpt":"界渡正在运行"}],"returned":1}),patch.object(adapter,"context_state",return_value={"knowledge_root":"fixture","read_only":True}):
                control.update_interface("mcp",True);control.update_client_permission("chatgpt",True)
                result=adapter.conversation_request([{"role":"user","content":"查询当前项目"}],"fixture-session","fixture-client")
            self.assertEqual("ready",result["bridge_status"])
            self.assertEqual(1,result["knowledge"]["search"]["returned"])
            self.assertTrue(result["knowledge"]["context_state"]["read_only"])
            self.assertEqual("用户背景",result["context"]["user"]["documents"][0]["content"])
            self.assertIn("permissions",result["context"])
            self.assertEqual("fixture-client",result["audit"]["source"])
            self.assertFalse(result["formal_write_performed"])

    def test_local_adapter_reuses_permissions_and_never_claims_test_as_chatgpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);permissions=root/"permissions.json";state=root/"adapter.json"
            with patch.object(control,"INTERFACE_STATE",permissions),patch.object(mcp_audit,"AUDIT_PATH",root/"audit.jsonl"),patch.object(adapter,"STATE_PATH",state),patch.object(adapter,"require_interface",control.require_interface),patch.object(adapter,"record_call",control.record_call),patch.object(adapter,"knowledge_search",return_value={"projects":["界渡"],"returned":1}),patch.object(adapter,"context_state",return_value={"knowledge_root":"fixture","read_only":True}),patch.object(adapter,"request_change",return_value={"status":"pending_owner_confirmation","formal_write_performed":False}):
                control.update_interface("mcp",True);control.update_client_permission("chatgpt",True)
                tools=adapter.discover("local-adapter-test")
                self.assertEqual(3,len(tools["tools"]))
                self.assertIsNone(adapter.status()["chatgpt_handshake"])
                found=adapter.call("knowledge_search",{"query":"当前有哪些项目"})
                context=adapter.call("context_state",{})
                with self.assertRaisesRegex(PermissionError,"MCP"):
                    adapter.call("request_knowledge_change",{"target_path":"02_项目/界渡/当前状态.md","proposed_content":"建议","reason":"测试"})
                adapter.request_change.assert_not_called()
                self.assertIn("界渡",found["result"]["projects"])
                self.assertTrue(context["result"]["read_only"])
                control.update_client_permission("chatgpt",False)
                with self.assertRaises(PermissionError):adapter.call("context_state",{})

    def test_unified_bridge_context_is_read_only_and_lists_projects(self):
        base={"context":{"user":{"documents":[{"path":"00_系统/画像.md","sha256":"abc"}]},"project":None,"state":{"knowledge_root":"fixture","read_only":True},"related_knowledge":{"results":[{"source_path":"02_项目/界渡/当前状态.md","title":"当前状态","sha256":"def"}],"returned":1},"permissions":{"global_access":True,"client_access":True,"formal_write":False,"change_policy":"需要Owner确认"}},"audit":{"client_id":"chatgpt","tools":["context_state","knowledge_search"]},"request_id":"bridge-1"}
        with patch.object(adapter,"context_request",return_value=base),patch.object(adapter,"_projects",return_value=["界渡","明证"]),patch.object(adapter,"project_detail",side_effect=lambda name:{"project":name,"status":"进行中" if name=="界渡" else "暂停"}):
            result=adapter.bridge_context("chatgpt","当前有哪些项目","session-1")
        self.assertEqual(["界渡","明证"],[x["project"] for x in result["projects"]])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["formal_write_performed"])
        self.assertEqual("02_项目/界渡/当前状态.md",result["sources"][0]["path"])


if __name__=="__main__":unittest.main()
