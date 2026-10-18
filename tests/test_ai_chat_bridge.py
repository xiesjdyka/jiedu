import json
import unittest
from unittest.mock import MagicMock, patch

from knowledge_bridge import ai_chat_bridge as bridge


class AIChatBridgeTests(unittest.TestCase):
    def test_requires_configured_provider(self):
        with patch.object(bridge, "load_provider", return_value={"provider_type": "manual-chatgpt", "enabled": False}):
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                bridge.ask("当前有哪些项目")

    def test_context_to_model_to_read_only_answer(self):
        config = {"provider_type": "openai-compatible", "enabled": True, "base_url": "https://fixture.invalid", "model": "fixture-model", "api_key_secret_ref": "env:FIXTURE", "timeout": 10, "max_input": 32000}
        context = {"context": {"user": {"documents": [{"path": "00_系统/画像.md", "content": "用户背景"}]}, "project": {"name": "界渡", "status": "进行中"}, "state": {"knowledge_units": 5005}, "related_knowledge": {"results": [{"source_path": "02_项目/界渡/当前状态.md", "title": "当前状态", "excerpt": "界渡正在运行"}]}, "permissions": {"read_allowed": True, "formal_write": False}}, "audit": {"requested_at": "2026-08-21T00:00:00+00:00", "tools": ["context_state", "knowledge_search"]}}
        response = MagicMock();response.read.return_value = json.dumps({"choices": [{"message": {"content": "界渡当前正在运行。"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108}}).encode("utf-8")
        response.__enter__.return_value = response
        with patch.object(bridge, "load_provider", return_value=config), patch.object(bridge, "context_request", return_value=context) as retrieve, patch.object(bridge, "_secret", return_value="fixture-secret"), patch.object(bridge.urllib.request, "urlopen", return_value=response), patch.object(bridge, "record_call") as audit:
            result = bridge.ask("当前有哪些项目")
        retrieve.assert_called_once_with("当前有哪些项目", None, None, "jiedu-ai-chat", "chatgpt")
        self.assertEqual("界渡当前正在运行。", result["answer"])
        self.assertEqual(108, result["usage"]["total_tokens"])
        self.assertEqual("02_项目/界渡/当前状态.md", result["sources"][0]["path"])
        self.assertFalse(result["formal_write_performed"])
        self.assertTrue(audit.call_args.args[0]["success"])

    def test_connection_returns_real_model_name(self):
        config = {"provider_type": "openai-compatible", "enabled": True, "base_url": "https://fixture.invalid/v1", "model": "fixture-model", "api_key_secret_ref": "wincred:Jiedu.AI.Provider", "timeout": 10}
        response = MagicMock();response.read.return_value = json.dumps({"choices": [{"message": {"content": "连接成功"}}], "usage": {"total_tokens": 5}}).encode("utf-8");response.__enter__.return_value = response
        with patch.object(bridge, "load_provider", return_value=config), patch.object(bridge, "_secret", return_value="fixture-secret"), patch.object(bridge.urllib.request, "urlopen", return_value=response), patch.object(bridge, "record_call") as audit:
            result = bridge.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual("fixture-model", result["model"])
        self.assertTrue(audit.call_args.args[0]["success"])


if __name__ == "__main__":
    unittest.main()
