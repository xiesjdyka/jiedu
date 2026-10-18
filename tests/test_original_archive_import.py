import hashlib
import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.original_archive_import import scan, set_status, session_records


class OriginalArchiveImportTests(unittest.TestCase):
    def test_owner_markdown_master_discovery_is_read_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "90_原始档案"
            root.mkdir()
            source = root / "ChatGPT_产品讨论.md"
            source.write_text("# 产品讨论\n\n## USER\n\n第一条原文。\n\n## ASSISTANT\n\n第二条原文。\n", encoding="utf-8")
            state = Path(temp) / "state.json"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            first = scan(root, state)
            second = scan(root, state)
            self.assertEqual(first["count"], 1)
            self.assertEqual(second["count"], 1)
            item = second["records"][0]
            self.assertEqual((item["source"], item["title"], item["message_count"], item["status"]), ("chatgpt", "产品讨论", 2, "未处理"))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            set_status(item["session_id"], "待提炼", state, sync_extraction=False)
            rows = session_records(state)
            self.assertEqual(rows[0]["session_title"], "产品讨论")
            self.assertEqual([x["original_text"] for x in rows[0]["messages"]], ["第一条原文。", "第二条原文。"])

    def test_only_named_chat_masters_are_discovered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "90_原始档案"
            root.mkdir()
            (root / "普通知识.md").write_text("# 普通知识", encoding="utf-8")
            (root / "Hermes_真实会话.md").write_text("## USER\n原文", encoding="utf-8")
            (root / "conversation.md").write_text("## USER\n旧归档", encoding="utf-8")
            result = scan(root, Path(temp) / "state.json")
            self.assertEqual([x["title"] for x in result["records"]], ["真实会话"])

    def test_source_directory_markdown_is_discovered_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "90_原始档案"
            source_dir = root / "ChatGPT"
            source_dir.mkdir(parents=True)
            source = source_dir / "界渡开发.md"
            source.write_text("# 界渡开发\n\n## USER\n原始问题\n\n## ASSISTANT\n原始回答\n", encoding="utf-8")
            attachment_dir = source_dir / "session" / "attachments"
            attachment_dir.mkdir(parents=True)
            (attachment_dir / "说明.md").write_text("# 附件说明", encoding="utf-8")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = scan(root, Path(temp) / "state.json")
            self.assertEqual(result["count"], 1)
            item = result["records"][0]
            self.assertEqual((item["source"], item["title"], item["message_count"]), ("chatgpt", "界渡开发", 2))
            self.assertEqual(item["source_sha256"], before)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
