import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.retrieval import RetrievalService
from knowledge_bridge import session_archive


class ImportRetrievalArchiveTests(unittest.TestCase):
    def test_knowledge_search_route_does_not_require_a_sidebar_item(self):
        app = (Path(__file__).parents[1] / "static" / "app.js").read_text("utf-8")
        self.assertIn('button.onclick=()=>openKnowledgeSearch(\'formal\')', app)
        self.assertIn('button[data-page="${name}"]`)?.classList.add', app)

    def test_directory_retrieval_keeps_formal_default_and_inbox_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            formal = root / "03_知识" / "formal.md"
            inbox = root / "99_收件箱" / "导入" / "_派生知识" / "report.rtf.md"
            formal.parent.mkdir(parents=True)
            inbox.parent.mkdir(parents=True)
            formal.write_text("formalalphaonly", "utf-8")
            inbox.write_text("---\njiedu_derived: true\nsource_path: source/report.rtf\n---\n\ninboxbetaonly", "utf-8")
            service = RetrievalService.for_root(root)
            formal_result = service.search("formalalphaonly", mode="hybrid")
            self.assertEqual("keyword", formal_result["effective_mode"])
            self.assertEqual(["03_知识/formal.md"], [row["source_path"] for row in formal_result["results"]])
            self.assertEqual([], service.search("inboxbetaonly", mode="keyword")["results"])
            inbox_result = service.search("inboxbetaonly", mode="vector", scope="inbox")
            self.assertEqual("keyword", inbox_result["effective_mode"])
            self.assertFalse(inbox_result["vector_used"])
            self.assertEqual("99_收件箱/导入/_派生知识/report.rtf.md", inbox_result["results"][0]["source_path"])

    def test_synced_manifest_without_legacy_completeness_has_derived_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "ChatGPT" / "fixture" / "03_元数据"
            metadata.mkdir(parents=True)
            manifest = {
                "source_key": "codex", "session_key": "codex:fixture", "sync_status": "synced",
                "message_count": 2, "user_count": 1, "assistant_count": 1,
                "started_at": "2026-09-03T00:00:00Z", "updated_at": "2026-09-03T00:01:00Z",
            }
            (metadata / "manifest.json").write_text(json.dumps(manifest), "utf-8")
            with patch.object(session_archive, "CANONICAL_ROOT", root), \
                 patch.object(session_archive, "SESSIONS_PATH", root / "missing.json"), \
                 patch("knowledge_bridge.original_archive_import.session_records", return_value=[]):
                result = session_archive.list_archive_summaries(0, 25)
            self.assertEqual("已归档", result["items"][0]["completeness"])
            self.assertFalse(result["has_more"])
            self.assertEqual(25, result["limit"])

    def test_display_cache_infers_generic_title_without_changing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); canonical = root / "90_原始档案"; metadata = canonical / "Hermes" / "A" / "03_元数据"
            metadata.mkdir(parents=True)
            manifest = {"source_key":"hermes","session_key":"hermes:fixture","title":"A","message_count":2,"updated_at":"2026-09-03T00:01:00Z"}
            manifest_path = metadata / "manifest.json"; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
            messages_path = metadata / "messages.jsonl"; messages_path.write_text("\n".join([
                json.dumps({"role":"system","content":"system"}, ensure_ascii=False),
                json.dumps({"role":"user","content":"继续修复界渡对话档案的标题和多项目关联"}, ensure_ascii=False),
            ]), "utf-8")
            display_cache = root / ".jiedu" / "cache" / "archive-display.json"
            projects = root / "02_项目" / "界渡"; projects.mkdir(parents=True)
            before_manifest = manifest_path.read_bytes(); before_messages = messages_path.read_bytes()
            with patch.object(session_archive, "CANONICAL_ROOT", canonical), \
                 patch.object(session_archive, "DISPLAY_CACHE", display_cache), \
                 patch.object(session_archive, "SESSIONS_PATH", root / "missing.json"), \
                 patch.object(session_archive, "_manifest_paths", return_value=[manifest_path]), \
                 patch.object(session_archive, "active_workspace") as workspace, \
                 patch("knowledge_bridge.original_archive_import.session_records", return_value=[]):
                workspace.return_value.projects = root / "02_项目"
                result = session_archive.rebuild_archive_display_cache()
                listing = session_archive.list_archive_summaries(0, 25)
            item = listing["items"][0]
            self.assertEqual("content_inferred", item["title_origin"])
            self.assertTrue(item["display_title"].startswith("推定｜继续修复界渡"))
            self.assertEqual(["界渡"], item["related_projects"])
            self.assertEqual(1, result["inferred_titles"])
            self.assertEqual(before_manifest, manifest_path.read_bytes())
            self.assertEqual(before_messages, messages_path.read_bytes())

    def test_legacy_counts_are_cached_and_filters_apply_before_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);canonical=root/"90_原始档案"
            for index,title in enumerate(("学习","聊天杂活","学校网站")):
                metadata=canonical/"ChatGPT"/title/"03_元数据";metadata.mkdir(parents=True)
                manifest={"source_key":"chatgpt","session_key":f"chatgpt:{index}","title":title,"sync_status":"synced","updated_at":f"2026-09-0{index+1}T00:00:00Z"}
                (metadata/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False),"utf-8")
                rows=[{"message_id":f"m{index}-1","role":"user","content":"question"},{"message_id":f"m{index}-2","role":"assistant","content":"answer"}]
                (metadata/"messages.jsonl").write_text("\n".join(json.dumps(row) for row in rows),"utf-8")
                attachments=[] if index!=1 else [{"source_message_id":f"m{index}-1","source_reference":"a","status":"missing"}]
                if attachments:(metadata/"attachments.jsonl").write_text("\n".join(json.dumps(row) for row in attachments),"utf-8")
            with patch.object(session_archive,"CANONICAL_ROOT",canonical),patch.object(session_archive,"SESSIONS_PATH",root/"missing.json"),patch("knowledge_bridge.original_archive_import.session_records",return_value=[]):
                found=session_archive.list_archive_summaries(0,1,client="chatgpt",query="学校")
                attention=session_archive.list_archive_summaries(0,25,status="attention")
            self.assertEqual(1,found["total"]);self.assertEqual("学校网站",found["items"][0]["display_title"])
            self.assertEqual(2,found["items"][0]["message_count"]);self.assertEqual(1,found["items"][0]["user_count"])
            self.assertEqual(["聊天杂活"],[row["display_title"] for row in attention["items"]])
            self.assertTrue((canonical/"ChatGPT"/"学校网站"/"03_元数据"/"archive-summary.json").is_file())

    def test_summary_falls_back_when_ledgers_are_absent_and_missing_overrides_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);metadata=root/"ChatGPT"/"legacy"/"03_元数据";metadata.mkdir(parents=True)
            manifest={"source_key":"chatgpt","session_key":"chatgpt:legacy","message_count":8,"user_count":4,"assistant_count":4,"attachment_count":3,"missing_attachment_count":1,"completeness":"完整"}
            manifest_path=metadata/"manifest.json";manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),"utf-8")
            with patch.object(session_archive,"active_workspace") as workspace:
                workspace.return_value.root=root
                item=session_archive._manifest_summary(manifest_path)
            self.assertEqual(8,item["message_count"]);self.assertEqual(3,item["attachment_count"])
            self.assertEqual(1,item["missing_attachment_count"]);self.assertEqual("附件有缺失",item["completeness"])

    def test_summary_skips_bad_rows_counts_inline_attachments_and_existing_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);metadata=root/"ChatGPT"/"fixture"/"03_元数据";metadata.mkdir(parents=True)
            entity=root/"90_原始档案"/"entity.bin";entity.parent.mkdir(parents=True);entity.write_bytes(b"ok")
            manifest={"source_key":"chatgpt","session_key":"chatgpt:fixture","message_count":9,"user_count":5,"assistant_count":4,"attachment_count":2}
            manifest_path=metadata/"manifest.json";manifest_path.write_text(json.dumps(manifest),"utf-8")
            rows=[json.dumps({"message_id":"m1","role":"user","attachments":[{"reference":"a","status":"available","stored_path":"90_原始档案/entity.bin"},{"reference":"b","status":"missing","stored_path":"90_原始档案/no.bin"}]}),"{broken",json.dumps({"message_id":"m2","role":"assistant"})]
            (metadata/"messages.jsonl").write_text("\n".join(rows),"utf-8")
            with patch.object(session_archive,"active_workspace") as workspace, \
                 patch.object(session_archive,"_read_message_rows",side_effect=AssertionError("JSONL must stream")):
                workspace.return_value.root=root
                item=session_archive._manifest_summary(manifest_path)
            self.assertEqual(2,item["message_count"]);self.assertEqual(2,item["attachment_count"])
            self.assertEqual(1,item["missing_attachment_count"]);self.assertEqual(1,item["attachment_entity_count"])
            self.assertEqual("附件有缺失",item["completeness"])

    def test_partial_archive_is_attention_and_never_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);canonical=root/"90_原始档案"
            for title,broken in (("正常归档",False),("部分归档",True)):
                metadata=canonical/"ChatGPT"/title/"03_元数据";metadata.mkdir(parents=True)
                manifest={"source_key":"chatgpt","session_key":f"chatgpt:{title}","title":title,"sync_status":"synced"}
                (metadata/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False),"utf-8")
                lines=[json.dumps({"message_id":f"{title}-1","role":"user","content":"q"}),json.dumps({"message_id":f"{title}-2","role":"assistant","content":"a"})]
                if broken:lines.insert(1,"{broken")
                (metadata/"messages.jsonl").write_text("\n".join(lines),"utf-8")
            with patch.object(session_archive,"CANONICAL_ROOT",canonical), \
                 patch.object(session_archive,"SESSIONS_PATH",root/"missing.json"), \
                 patch.object(session_archive,"_manifest_paths",return_value=sorted(canonical.glob("*/**/manifest.json"))), \
                 patch.object(session_archive,"active_workspace") as workspace, \
                 patch("knowledge_bridge.original_archive_import.session_records",return_value=[]):
                workspace.return_value.root=root
                complete=session_archive.list_archive_summaries(0,25,status="complete")
                attention=session_archive.list_archive_summaries(0,25,status="attention")
            self.assertEqual(["正常归档"],[row["display_title"] for row in complete["items"]])
            self.assertEqual(["部分归档"],[row["display_title"] for row in attention["items"]])
            self.assertEqual("归档需检查",attention["items"][0]["completeness"])


if __name__ == "__main__":
    unittest.main()
