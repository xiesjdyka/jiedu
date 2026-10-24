import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.codex_source_adapter import CodexSourceAdapter
from knowledge_bridge import knowledge_governance, session_archive
from knowledge_bridge.workspace import initialize_workspace


class PerformanceSnapshotTests(unittest.TestCase):
    def test_codex_status_never_discovers_on_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = initialize_workspace(root / "workspace")
            adapter = CodexSourceAdapter(context, codex_home=root / "codex", machine_data=root / "machine")
            with patch.object(adapter, "discover", side_effect=AssertionError("status must not discover")):
                started = time.perf_counter(); result = adapter.status(); elapsed = time.perf_counter() - started
            self.assertEqual("codex", result["adapter"])
            self.assertLess(elapsed, .3)

    def test_governance_get_uses_snapshot_without_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "governance.json"
            cache.write_text(json.dumps({"archive_counts":{"chatgpt":2,"hermes":1,"codex":3,"other":0},
                                         "project_change_suggestions":[],"snapshot_updated_at":"fictional"}), "utf-8")
            with patch.object(knowledge_governance, "_cache_path", return_value=cache), \
                 patch.object(knowledge_governance, "list_archives", side_effect=AssertionError("GET must not scan")):
                started = time.perf_counter(); result = knowledge_governance.status(); elapsed = time.perf_counter() - started
            self.assertEqual(3, result["archive_counts"]["codex"])
            self.assertLess(elapsed, .3)

    def test_project_mentions_are_grouped_as_sources_not_fake_change_tasks(self):
        archives=[{"client":"codex","session_id":"one","session_title":"界渡讨论","updated_at":"2026-09-21T01:00:00Z","messages":[{"message_id":"m1","original_text":"界渡只是被提及"}]},
                  {"client":"hermes","session_id":"two","session_title":"再次提到界渡","updated_at":"2026-09-21T02:00:00Z","messages":[]}]
        detail={"summary":"检索整改中","source":"02_项目/界渡/当前状态.md"}
        with patch.object(knowledge_governance,"_project_names",return_value=["界渡"]), \
             patch.object(knowledge_governance,"list_archives",return_value=archives), \
             patch("knowledge_bridge.memory_control.project_detail",return_value=detail), \
             patch("knowledge_bridge.memory_pipeline.list_candidates",return_value=[]):
            rows=knowledge_governance.project_change_suggestions()
        self.assertEqual(1,len(rows));self.assertEqual("related_sources",rows[0]["kind"])
        self.assertEqual(2,rows[0]["related_source_count"])
        self.assertIn("不作为待办",rows[0]["reason"])

    def test_archive_list_is_metadata_only_and_detail_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); canonical = root / "90_原始档案"; metadata = canonical / "ChatGPT" / "示例" / "03_元数据"
            metadata.mkdir(parents=True)
            manifest = {"source_key":"chatgpt","session_key":"chatgpt:fictional","session_title":"示例",
                        "message_count":40,"user_count":20,"assistant_count":20,"turn_count":20,
                        "attachment_count":4,"completeness":"完整","updated_at":"2026-09-02T00:00:00Z"}
            (metadata / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
            rows = [{"message_id":str(index),"role":"user" if index % 2 else "assistant",
                     "original_text":f"message {index}","attachments":[{"original_name":"fictional.png"}]}
                    for index in range(40)]
            (metadata / "messages.jsonl").write_text("\n".join(json.dumps(x) for x in rows), "utf-8")
            with patch.object(session_archive, "CANONICAL_ROOT", canonical), \
                 patch.object(session_archive, "SESSIONS_PATH", root / "sessions.json"), \
                 patch("knowledge_bridge.original_archive_import.session_records", return_value=[]):
                listing = session_archive.list_archive_summaries(0, 20)
                detail = session_archive.materialize_page("chatgpt:fictional", limit=12)
            self.assertEqual(1, listing["total"])
            self.assertNotIn("messages", listing["items"][0])
            self.assertEqual(12, len(detail["messages"]))
            self.assertTrue(detail["page"]["has_earlier"])
            self.assertNotIn("attachments", detail["messages"][0])
            self.assertEqual(1, detail["messages"][0]["attachment_count"])

    def test_long_archive_pagination_has_no_duplicates_gaps_or_write_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);canonical=root/"90_原始档案";metadata=canonical/"Codex"/"长会话"/"03_元数据"
            metadata.mkdir(parents=True);manifest={"source_key":"codex","session_key":"codex:long",
                "session_title":"长会话","message_count":43,"updated_at":"2026-09-03T00:00:00Z"}
            (metadata/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False),"utf-8")
            rows=[{"message_id":f"m-{index:03d}","role":"user","original_text":f"message {index}",
                   "attachments":[{"original_name":f"file-{index}.png"}]} for index in range(43)]
            (metadata/"messages.jsonl").write_text("\n".join(json.dumps(row) for row in rows),"utf-8")
            before=None;pages=[];seen=[]
            with patch.object(session_archive,"CANONICAL_ROOT",canonical), \
                 patch.object(session_archive,"SESSIONS_PATH",root/"sessions.json"), \
                 patch("knowledge_bridge.original_archive_import.session_records",return_value=[]):
                while True:
                    page=session_archive.materialize_page("codex:long",before=before,limit=6)
                    pages.append(page);seen[0:0]=[row["message_id"] for row in page["messages"]]
                    self.assertLessEqual(len(page["messages"]),6);self.assertNotIn("conversation",page)
                    self.assertTrue(all("attachments" not in row for row in page["messages"]))
                    if not page["page"]["has_earlier"]:break
                    before=page["page"]["next_before"]
            self.assertEqual([row["message_id"] for row in rows],seen)
            self.assertEqual(43,len(set(seen)));self.assertEqual(8,len(pages))


class PerformanceShellStaticTests(unittest.TestCase):
    def test_navigation_cache_abort_and_polling_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text("utf-8")
        html = (root / "static" / "index.html").read_text("utf-8")
        self.assertNotIn("setInterval(syncActivePage,2500)", script)
        self.assertIn("setInterval(refreshHealth,30000)", script)
        self.assertIn("getInflight", script)
        self.assertIn("abortPageRequests(outgoing)", script)
        self.assertNotIn("loadFiles().catch(console.error);\nlet liveSyncTimer", script)
        for label in ("首页", "知识", "对话档案", "整理中心", "AI连接", "设置"):
            self.assertIn(f">{label}<", html)
        self.assertIn("localStorage.getItem(BRAND_SEQUENCE_KEY)==='1'", script)
        # Membership matters; adding other uncached APIs must not break this gate.
        import re
        prefixes=re.search(r'const volatileApiPrefixes=\[([^\]]+)\]',script).group(1)
        for prefix in ('/api/protection','/api/client-access'):
            self.assertIn(repr(prefix),prefixes)
        self.assertIn("cache:volatile?'no-store':'default'", script)
        self.assertIn("pending_confirmations||0", script)
        self.assertIn("{ttl:0,force:true}", script)
        self.assertIn("data-return-access>恢复本机管理", script)
        self.assertIn("ownerReturnToAccess", script)
        self.assertIn("querySelector('[data-approve-request]')", script)
        self.assertIn("已允许待消费", script)
        self.assertIn("本次请求已允许，等待原请求方使用一次", script)
        self.assertIn("testEnvironmentBanner", script)
        self.assertIn("isolated_test", script)
        self.assertIn("隔离验收环境 · 数据与配置独立于正式工作区", html)


if __name__ == "__main__":
    unittest.main()
