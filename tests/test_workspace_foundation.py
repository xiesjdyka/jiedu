import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.archive_store import ArchiveStore, SourceRegistry
from knowledge_bridge.search_scopes import files_for_scope, rebuild_all, search_scope, scope_health, evidence_selection_status
from knowledge_bridge.workspace import initialize_workspace, load_workspace


class WorkspaceFoundationTests(unittest.TestCase):
    def test_evidence_selection_merges_canonical_and_independent_history_without_duplicate_projection(self):
        from knowledge_bridge.archive_store import ArchiveStore
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace");store=ArchiveStore(context)
            canonical=store.ensure_session(source_key='codex',source_session_id='canonical',title='标准会话')
            store.append_message(canonical['session_key'],message_id='m1',role='user',content='标准会话原文。')
            canonical_path=Path(store.session(canonical['session_key'])['archive_path'])/'01_对话'/'conversation.md'
            historical=context.raw_archive/'资料导入'/'历史快照.md';historical.parent.mkdir(parents=True)
            historical.write_text('---\ndocument_role: historical_record\n---\n# 历史快照\n\n独立历史证据。','utf-8')
            duplicate=context.raw_archive/'资料导入'/'标准会话副本.md';duplicate.write_bytes(canonical_path.read_bytes())
            validation=context.raw_archive/'资料导入'/'验收日志.md'
            validation.write_text('---\ndocument_role: validation\n---\n# 验收日志\n\n不进入证据。','utf-8')
            subtask=store.ensure_session(source_key='codex',source_session_id='subtask',title='内部子任务')
            store.append_message(subtask['session_key'],message_id='s1',role='user',content='内部执行记录。')
            manifest=Path(store.session(subtask['session_key'])['archive_path'])/'03_元数据'/'manifest.json'
            value=json.loads(manifest.read_text('utf-8'));value['archive_kind']='subtask'
            manifest.write_text(json.dumps(value,ensure_ascii=False),'utf-8')
            selected={context.relative(path) for path in files_for_scope(context,'evidence')}
            self.assertIn(context.relative(canonical_path),selected)
            self.assertIn(context.relative(historical),selected)
            self.assertNotIn(context.relative(duplicate),selected)
            self.assertNotIn(context.relative(validation),selected)
            self.assertNotIn(context.relative(Path(store.session(subtask['session_key'])['archive_path'])/'01_对话'/'conversation.md'),selected)
            status=evidence_selection_status(context)
            self.assertEqual(2,status['selected'])
            self.assertEqual(1,status['reasons']['exact_content_duplicate'])
            self.assertEqual(1,status['reasons']['manifest_explicitly_excluded'])

    def test_initialize_is_idempotent_movable_and_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            marker = first / "03_知识" / "已有资料.md"
            context = initialize_workspace(first)
            marker.write_text("不得覆盖", encoding="utf-8")
            again = initialize_workspace(first)
            self.assertEqual(context.workspace_id, again.workspace_id)
            self.assertEqual("不得覆盖", marker.read_text(encoding="utf-8"))
            moved = root / "moved"
            shutil.move(first, moved)
            loaded = load_workspace(moved)
            second = initialize_workspace(root / "second")
            self.assertEqual(context.workspace_id, loaded.workspace_id)
            self.assertNotEqual(loaded.workspace_id, second.workspace_id)
            manifest = json.loads(loaded.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn(str(first), json.dumps(manifest, ensure_ascii=False))

    def test_product_family_source_identity_title_and_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            context = initialize_workspace(temp)
            registry = SourceRegistry(context)
            self.assertEqual(("ChatGPT", "ChatGPT Web"), (registry.resolve("chatgpt").product_family, registry.resolve("chatgpt").session_source))
            self.assertEqual(("ChatGPT", "Codex"), (registry.resolve("codex").product_family, registry.resolve("codex").session_source))
            store = ArchiveStore(context)
            web = store.ensure_session(source_key="chatgpt", source_session_id="same-id", title="同名：对话")
            codex = store.ensure_session(source_key="codex", source_session_id="different-id", title="同名：对话")
            self.assertEqual("ChatGPT", web["product_family"])
            self.assertEqual("ChatGPT", codex["product_family"])
            self.assertEqual("ChatGPT Web", web["session_source"])
            self.assertEqual("Codex", codex["session_source"])
            self.assertNotEqual(web["archive_path"], codex["archive_path"])
            renamed = store.ensure_session(source_key="chatgpt", source_session_id="same-id", title="界渡")
            self.assertEqual(web["session_key"], renamed["session_key"])
            self.assertEqual("界渡", Path(renamed["archive_path"]).name)
            self.assertEqual(["同名：对话", "界渡"], [x["title"] for x in renamed["title_history"]])

    def test_folder_rescue_import_is_immutable_linked_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = initialize_workspace(root / "workspace")
            source = root / "export"
            source.mkdir()
            conversation = source / "conversation.md"
            image = source / "photo.png"
            document = source / "brief.pdf"
            conversation.write_text("# 导出\n\n## USER\n请看 photo.png 和 brief.pdf\n\n## ASSISTANT\n已收到。\n", encoding="utf-8")
            image.write_bytes(b"png-content")
            document.write_bytes(b"pdf-content")
            before = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in source.iterdir()}
            store = ArchiveStore(context)
            first = store.import_folder(source, source_key="chatgpt", title="抢救对话")
            second = store.import_folder(source, source_key="chatgpt", title="抢救对话")
            after = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in source.iterdir()}
            self.assertEqual(before, after)
            self.assertTrue(first["raw_package_copied"])
            self.assertFalse(second["raw_package_copied"])
            self.assertEqual(first["package_id"], second["package_id"])
            self.assertEqual(2, len(first["attachment_links"]))
            self.assertTrue((context.root / first["raw_package_path"] / "conversation.md").is_file())
            messages = Path(first["session"]["archive_path"]) / "03_元数据" / "messages.jsonl"
            self.assertEqual(2, len(messages.read_text(encoding="utf-8").splitlines()))

    def test_same_attachment_name_different_content_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = initialize_workspace(root / "workspace")
            one = root / "one" / "same.txt"
            two = root / "two" / "same.txt"
            one.parent.mkdir(); two.parent.mkdir()
            one.write_text("one", encoding="utf-8"); two.write_text("two", encoding="utf-8")
            store = ArchiveStore(context)
            session = store.ensure_session(source_key="hermes", source_session_id="a", title="附件测试")
            result = store.append_message(session["session_key"], message_id="m1", role="user", content="附件", attachment_paths=[one, two], provenance={"source_db": r"C:\\private\\client.db"})
            attachments = result["record"]["attachments"]
            self.assertEqual(2, len({x["sha256"] for x in attachments}))
            self.assertEqual(2, len({x["stored_path"] for x in attachments}))
            self.assertTrue(all((context.root / x["stored_path"]).is_file() for x in attachments))
            portable = (Path(session["archive_path"]) / "03_元数据" / "messages.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(r"C:\\private", portable)
            self.assertIn("client.db", portable)

    def test_search_boundaries_and_rebuild_after_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            context = initialize_workspace(temp)
            (context.root / "03_知识" / "formal.md").write_text("边界词 正式事实", encoding="utf-8")
            evidence = context.raw_archive / "ChatGPT" / "会话" / "01_对话"
            evidence.mkdir(parents=True)
            (evidence / "conversation.md").write_text("边界词 原始证据", encoding="utf-8")
            attachment = context.raw_archive / "ChatGPT" / "会话" / "02_附件"
            attachment.mkdir(parents=True)
            (attachment / "污染.md").write_text("边界词 附件污染", encoding="utf-8")
            (context.inbox / "pending.md").write_text("边界词 待识别", encoding="utf-8")
            formal = search_scope("边界词", "formal", context=context)
            raw = search_scope("边界词", "evidence", context=context)
            inbox = search_scope("边界词", "inbox", context=context)
            self.assertEqual(["03_知识/formal.md"], [x["source_path"] for x in formal["results"]])
            self.assertEqual(["90_原始档案/ChatGPT/会话/01_对话/conversation.md"], [x["source_path"] for x in raw["results"]])
            self.assertEqual(["99_收件箱/pending.md"], [x["source_path"] for x in inbox["results"]])
            first = rebuild_all(context)
            self.assertTrue(all(row['manifest_fresh'] for row in scope_health(context,max_age_seconds=0).values()))
            (context.root / "03_知识" / "formal.md").write_text("边界词 已修改但文件数不变", encoding="utf-8")
            self.assertFalse(scope_health(context,max_age_seconds=0)['formal']['manifest_fresh'])
            rebuild_all(context)
            self.assertTrue(scope_health(context,max_age_seconds=0)['formal']['manifest_fresh'])
            shutil.rmtree(context.indices)
            second = rebuild_all(context)
            self.assertEqual(first["formal"]["file_count"], second["formal"]["file_count"])
            self.assertTrue((context.index_root("formal") / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
