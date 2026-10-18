import json
import os
import tempfile
import unittest
import base64
import hashlib
import uuid
from unittest import mock
from pathlib import Path
from datetime import datetime,timezone

from knowledge_bridge.codex_source_adapter import CodexSourceAdapter, PARSER_VERSION
from knowledge_bridge.search_scopes import search_scope
from knowledge_bridge.workspace import initialize_workspace


def _line(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _meta(session_id: str) -> dict:
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
            "payload": {"id": session_id, "cli_version": "fixture"}}


def _message(message_id: str, role: str, text: str, *, phase=None, attachments=()) -> dict:
    kind = "input_text" if role == "user" else "output_text"
    payload = {"type": "message", "id": message_id, "role": role,
               "content": [{"type": kind, "text": text}, *attachments]}
    if phase is not None:
        payload["phase"] = phase
    return {"timestamp": f"2026-01-01T00:00:{message_id[-2:] if message_id[-2:].isdigit() else '01'}Z",
            "type": "response_item", "payload": payload}


class CodexSourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.base = Path(self.temporary.name)
        self.codex = self.base / "codex"; self.sessions = self.codex / "sessions" / "2026" / "01" / "01"
        self.sessions.mkdir(parents=True); (self.codex / "attachments").mkdir()
        self.workspace = initialize_workspace(self.base / "workspace")
        self.machine = self.base / "machine"

    def tearDown(self):
        self.temporary.cleanup()

    def _source(self, session_id: str, events: list[dict], name: str | None = None) -> Path:
        path = self.sessions / (name or f"rollout-{session_id}.jsonl")
        path.write_bytes(b"".join(_line(event) for event in [_meta(session_id), *events]))
        return path

    def _index(self, rows: list[tuple[str, str]]) -> None:
        (self.codex / "session_index.jsonl").write_bytes(b"".join(
            _line({"id": session_id, "thread_name": title, "updated_at": "2026-01-01T00:00:00Z"})
            for session_id, title in rows))

    def _adapter(self) -> CodexSourceAdapter:
        return CodexSourceAdapter(self.workspace, codex_home=self.codex, machine_data=self.machine)

    def _authorize(self, adapter: CodexSourceAdapter, *session_ids: str) -> None:
        adapter.configure(enabled=True, authorize=list(session_ids), owner_confirmed=True)

    def test_discovery_uses_session_meta_not_filename_title_or_body_mentions(self):
        first, second = "session-a", "session-b"
        self._source(first, [_message("m01", "user", f"正文提到 {second}")], "rollout-mentions-b.jsonl")
        self._source(second, [_message("m02", "user", f"正文提到 {first}")], "rollout-mentions-a.jsonl")
        self._index([(first, "同名"), (second, "同名")])
        found = self._adapter().discover()
        self.assertEqual({first, second}, set(found))
        self.assertEqual(first, found[first].session_id)
        self.assertTrue(found[first].path.name.endswith("mentions-b.jsonl"))

    def test_discovery_excludes_actual_subagent_metadata_preserves_short_untitled_parent(self):
        self._source('main', [_message('m01','user','好')])
        for index,field in enumerate(('source','thread_source')):
            sid=f'child-{index}';meta=_meta(sid)
            meta['payload'].update(parent_thread_id='main', **{field:{'subagent':{'thread_spawn':{}}}})
            path=self.sessions/f'rollout-{sid}.jsonl'
            path.write_bytes(_line(meta)+_line(_message('m01','user','internal')))
        self.assertEqual({'main'},set(self._adapter().discover()))

    def test_initial_incremental_idempotent_partial_line_and_offline_backfill(self):
        sid = "incremental-session"
        path = self._source(sid, [_message("m01", "user", "第一条"),
                                  _message("m02", "assistant", "回答", phase="final_answer")])
        self._index([(sid, "增量会话")]); adapter = self._adapter(); self._authorize(adapter, sid)
        first = adapter.sync(manual=True)
        self.assertEqual(2, first["saved_messages"]); offset1 = first["sessions"][0]["confirmed_offset"]
        self.assertEqual(0, adapter.sync(manual=True)["saved_messages"])
        complete = _line(_message("m03", "user", "离线期间新增"))
        partial = json.dumps(_message("m04", "assistant", "半行等待", phase="commentary"), ensure_ascii=False).encode()
        with path.open("ab") as handle: handle.write(complete + partial[:len(partial)//2])
        second = adapter.sync(manual=True); cursor = second["sessions"][0]
        self.assertEqual(1, second["saved_messages"]); self.assertGreater(cursor["confirmed_offset"], offset1)
        self.assertGreater(cursor["pending_partial_bytes"], 0)
        with path.open("ab") as handle: handle.write(partial[len(partial)//2:] + b"\n")
        third = adapter.sync(manual=True)
        self.assertEqual(1, third["saved_messages"]); self.assertEqual(0, third["sessions"][0]["pending_partial_bytes"])
        adapter.configure(enabled=False, owner_confirmed=True)
        with path.open("ab") as handle: handle.write(_line(_message("m05", "user", "界渡关闭期间")))
        self.assertEqual("paused", adapter.sync()["status"])
        adapter.configure(enabled=True, owner_confirmed=True); backfill = adapter.sync(manual=True)
        self.assertEqual(1, backfill["saved_messages"])
        cursor = adapter.status()["current_cursors"][sid]
        self.assertEqual(("m05", PARSER_VERSION, path.stat().st_size),
                         (cursor["last_normalized_message_id"], cursor["parser_version"], cursor["confirmed_offset"]))
        state_before = adapter.state_path.read_bytes(); audit_before = adapter.audit_path.read_bytes()
        self.assertEqual("unchanged", adapter.sync(manual=False)["status"])
        self.assertEqual(state_before, adapter.state_path.read_bytes())
        self.assertEqual(audit_before, adapter.audit_path.read_bytes())
        with path.open("ab") as handle:
            handle.write(_line({"timestamp": "2026-01-01T00:01:00Z", "type": "response_item",
                                "payload": {"type": "reasoning", "summary": ["hidden"]}}))
        adapter.sync(manual=False)
        self.assertEqual(1, adapter.status()["last_sync_message_count"])

    def test_truncate_replace_move_gap_rescan_and_conflict_refusal(self):
        sid = "gap-session"; path = self._source(sid, [_message("m01", "user", "原文"),
                                                      _message("m03", "assistant", "后文", phase="final_answer")])
        self._index([(sid, "断档测试")]); adapter = self._adapter(); self._authorize(adapter, sid); adapter.sync()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(_line(_meta(sid)) + _line(_message("m01", "user", "原文")) +
                                _line(_message("m02", "assistant", "补齐", phase="commentary")) +
                                _line(_message("m03", "assistant", "后文", phase="final_answer")))
        os.replace(replacement, path)
        recovered = adapter.sync(manual=True)["sessions"][0]
        self.assertTrue(recovered["gap_detected_during_sync"]); self.assertEqual("gap_recovered", recovered["status"])
        session = next(iter(adapter.store._registry()["sessions"].values()))
        conversation = (self.workspace.root / session["relative_path"] / "01_对话" / "conversation.md").read_text("utf-8")
        self.assertLess(conversation.index("原文"), conversation.index("补齐"))
        self.assertLess(conversation.index("补齐"), conversation.index("后文"))
        moved = path.with_name("rollout-moved.jsonl"); path.rename(moved)
        with moved.open("ab") as handle: handle.write(_line(_message("m04", "user", "轮换后新增")))
        self.assertEqual(1, adapter.sync(manual=True)["saved_messages"])
        conflict = moved.with_suffix(".new")
        conflict.write_bytes(_line(_meta(sid)) + _line(_message("m01", "user", "冲突改写")))
        os.replace(conflict, moved)
        failed = adapter.sync(manual=True)
        self.assertEqual("failed", failed["sessions"][0]["status"])
        state = adapter.status()["current_cursors"][sid]
        self.assertEqual("failed", state["current_status"])
        self.assertIn("冲突", state["last_error"])
        raw = next(self.workspace.raw_message_store.glob("*.jsonl")).read_text("utf-8")
        self.assertIn("原文", raw); self.assertNotIn("冲突改写", raw)

    def test_title_change_same_name_collision_and_stable_identity(self):
        one, two = "title-one", "title-two"
        self._source(one, [_message("m01", "user", "甲")]); self._source(two, [_message("m02", "user", "乙")])
        self._index([(one, "同名会话"), (two, "同名会话")]); adapter = self._adapter(); self._authorize(adapter, one, two)
        adapter.sync(); sessions = adapter.store._registry()["sessions"]
        self.assertEqual(2, len(sessions)); paths = {item["relative_path"] for item in sessions.values()}
        self.assertEqual(2, len(paths)); self.assertTrue(any("[" in path for path in paths))
        one_key = next(key for key, item in sessions.items() if item["source_session_id"] == one)
        self._index([(one, "改名后的会话"), (two, "同名会话")]); adapter.sync()
        updated = adapter.store.session(one_key)
        self.assertEqual("改名后的会话", updated["title"]); self.assertEqual(2, len(updated["title_history"]))
        self.assertEqual(one, updated["source_session_id"])

    def test_attachment_copy_collision_missing_and_retry_without_absolute_manifest_path(self):
        sid = "attachment-session"
        first = self.codex / "attachments" / "one" / "same.bin"; first.parent.mkdir(); first.write_bytes(b"one")
        second = self.codex / "attachments" / "two" / "same.bin"; second.parent.mkdir(); second.write_bytes(b"two")
        missing = self.codex / "attachments" / "later" / "missing.png"
        events = [
            _message("m01", "user", "附件一", attachments=[{"type": "input_file", "path": "one/same.bin", "name": "same.bin"}]),
            _message("m02", "user", "附件二", attachments=[{"type": "input_file", "path": "two/same.bin", "name": "same.bin"}]),
            _message("m03", "user", "稍后出现", attachments=[{"type": "input_image", "path": "later/missing.png", "name": "missing.png", "mime_type": "image/png"}]),
        ]
        self._source(sid, events); self._index([(sid, "附件测试")]); adapter = self._adapter(); self._authorize(adapter, sid); adapter.sync()
        session = next(iter(adapter.store._registry()["sessions"].values())); folder = self.workspace.root / session["relative_path"]
        files = list((folder / "02_附件").glob("*.bin")); self.assertEqual(2, len(files))
        self.assertEqual(2, len({path.name for path in files}))
        status = adapter.store.attachment_status(session["session_key"]); self.assertEqual(1, sum(row["missing"] for row in status))
        portable = (folder / "03_元数据" / "attachments.jsonl").read_text("utf-8")
        self.assertNotIn(str(self.codex), portable)
        missing.parent.mkdir(); missing.write_bytes(b"image")
        self.assertEqual(0, adapter.sync(manual=True)["saved_messages"])
        status = adapter.store.attachment_status(session["session_key"]); self.assertEqual(0, sum(row["missing"] for row in status))
        self.assertTrue(any(row["original_name"] == "missing.png" and row["sha256"] for row in status))

    def test_real_shape_inline_images_are_assets_and_internal_local_images_are_not(self):
        sid = "real-shape-session"; image = b"\x89PNG\r\n\x1a\nfixture-image"
        encoded = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        events = [
            _message("m01", "user", "两条显式图片引用", attachments=[
                {"type": "input_image", "image_url": encoded, "detail": "original"},
            ]),
            _message("m02", "user", r"正文普通提到 C:\clips\demo.mp4，不是附件"),
            {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"type": "ImageView", "id": "tool-image",
                "path": r"C:\private\tool.png", "content": [{"type": "local_image", "path": r"C:\private\tool.png"}]}}},
            _message("m03", "user", "相同实体再次关联", attachments=[
                {"type": "input_image", "image_url": encoded, "detail": "original"},
            ]),
        ]
        self._source(sid, events); self._index([(sid, "真实结构脱敏")]); adapter = self._adapter(); self._authorize(adapter, sid)
        result = adapter.sync(manual=True); self.assertEqual(3, result["saved_messages"])
        session = next(iter(adapter.store._registry()["sessions"].values()))
        folder = self.workspace.root / session["relative_path"]
        rows = adapter.store.attachment_status(session["session_key"])
        self.assertEqual(2, len(rows)); self.assertEqual({"m01", "m03"}, {row["source_message_id"] for row in rows})
        self.assertEqual({hashlib.sha256(image).hexdigest()}, {row["sha256"] for row in rows})
        self.assertEqual({len(image)}, {row["size_bytes"] for row in rows})
        self.assertEqual({"image"}, {row["media_type"] for row in rows})
        self.assertEqual({"generated_from_content"}, {row["original_name_source"] for row in rows})
        self.assertEqual(1, sum(row["duplicate"] for row in rows))
        self.assertEqual(1, len(list((folder / "02_附件").glob("*.png"))))
        portable = (folder / "03_元数据" / "attachments.jsonl").read_text("utf-8")
        self.assertNotIn("data:image", portable); self.assertNotIn(r"C:\private", portable)
        self.assertNotIn("tool-image", portable); self.assertNotIn("demo.mp4", portable)

    def test_manifest_temp_recovery_identity_newer_older_and_write_failure(self):
        sid = "recovery-session"; self._source(sid, [_message("m01", "user", "恢复")])
        self._index([(sid, "恢复测试")]); adapter = self._adapter(); self._authorize(adapter, sid); adapter.sync()
        session = next(iter(adapter.store._registry()["sessions"].values()))
        metadata = self.workspace.root / session["relative_path"] / "03_元数据"
        formal_path = metadata / "manifest.json"; formal = json.loads(formal_path.read_text("utf-8"))

        older = dict(formal, updated_at="2025-01-01T00:00:00+00:00", message_count=0)
        recoverable = metadata / f".manifest.json.{uuid.uuid4().hex}.tmp"
        recoverable.write_text(json.dumps(older), "utf-8")
        report = adapter.store.recover_orphaned_manifest_temps()
        self.assertEqual(1, report["moved"]); self.assertFalse(recoverable.exists())
        self.assertTrue(any(adapter.store._recovery_root.iterdir()))

        mismatch = metadata / f".manifest.json.{uuid.uuid4().hex}.tmp"
        mismatch.write_text(json.dumps(dict(formal, source_session_id="different")), "utf-8")
        report = adapter.store.recover_orphaned_manifest_temps()
        self.assertEqual(1, report["retained"]); self.assertTrue(mismatch.exists())

        newer_candidate = metadata / f".manifest.json.{uuid.uuid4().hex}.tmp"
        newer_candidate.write_text(json.dumps(dict(formal, updated_at="2099-01-01T00:00:00+00:00",
                                                    message_count=int(formal.get("message_count") or 0) + 1)), "utf-8")
        report = adapter.store.recover_orphaned_manifest_temps()
        self.assertEqual(2, report["retained"]); self.assertTrue(newer_candidate.exists())

        with mock.patch("knowledge_bridge.archive_store.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                adapter.store._atomic_manifest(formal_path, formal)
        failed_temps = [path for path in metadata.iterdir()
                        if path.name.startswith(".manifest.json.") and path not in {mismatch, newer_candidate}]
        self.assertEqual([], failed_temps)
        audit_rows = [json.loads(line) for line in adapter.store._recovery_audit.read_text("utf-8").splitlines()]
        self.assertTrue(any(row["reason"] == "atomic_replace_failed" for row in audit_rows))

    def test_unknown_and_hidden_events_are_isolated_from_visible_archive(self):
        sid = "visibility-session"
        synthetic = _message("synthetic", "user", "<environment_context>secret context</environment_context>")
        permission_injection = _message(
            "permission-injection", "user",
            "The following is the Codex agent history whose request action you are assessing. [redacted fixture]",
        )
        developer = _message("developer", "developer", "developer secret")
        uncertain = _message("uncertain", "assistant", "maybe hidden")
        events = [developer, synthetic, permission_injection,
                  {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "reasoning", "summary": ["hidden reasoning"]}},
                  uncertain, {"timestamp": "2026-01-01T00:00:02Z", "type": "future_private", "payload": {"value": "unknown secret"}},
                  _message("m01", "user", "用户可见"),
                  _message("m02", "assistant", "过程可见", phase="commentary"),
                  _message("m03", "assistant", "最终可见", phase="final_answer")]
        self._source(sid, events); self._index([(sid, "可见边界")]); adapter = self._adapter(); self._authorize(adapter, sid)
        result = adapter.sync(manual=True); self.assertEqual(3, result["saved_messages"])
        session = next(iter(adapter.store._registry()["sessions"].values())); folder = self.workspace.root / session["relative_path"]
        conversation = (folder / "01_对话" / "conversation.md").read_text("utf-8")
        for visible in ("用户可见", "过程可见", "最终可见"): self.assertIn(visible, conversation)
        for hidden in ("developer secret", "secret context", "request action you are assessing",
                       "hidden reasoning", "maybe hidden", "unknown secret"):
            self.assertNotIn(hidden, conversation)
        review = self.workspace.queues / "codex-needs-review.jsonl"
        reasons = {json.loads(line)["reason"] for line in review.read_text("utf-8").splitlines()}
        self.assertEqual({"uncertain_assistant_phase", "unknown_top_level_event"}, reasons)
        message_rows = (folder / "03_元数据" / "messages.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(3, len(message_rows))

    def test_unauthorized_session_never_archives_and_search_scope_stays_explicit(self):
        authorized, blocked = "allowed-session", "blocked-session"
        self._source(authorized, [_message("m01", "user", "唯一证据词")])
        self._source(blocked, [_message("m02", "user", "禁止归档词")])
        self._index([(authorized, "允许"), (blocked, "禁止")]); adapter = self._adapter()
        adapter.configure(enabled=True, authorize=[authorized], owner_confirmed=True); adapter.sync()
        registry = adapter.store._registry()["sessions"]
        self.assertEqual([authorized], [item["source_session_id"] for item in registry.values()])
        self.assertEqual([], search_scope("唯一证据词", "formal", context=self.workspace)["results"])
        evidence = search_scope("唯一证据词", "evidence", context=self.workspace)["results"]
        self.assertEqual(1, len(evidence)); self.assertTrue(evidence[0]["source_path"].startswith("90_原始档案/ChatGPT/"))
        self.assertEqual([], search_scope("禁止归档词", "evidence", context=self.workspace)["results"])
        with self.assertRaises(PermissionError): adapter.configure(enabled=False)

    def test_all_selected_paused_exclusion_future_discovery_and_restart_resume(self):
        one, two, future = "policy-one", "policy-two", "policy-future"
        first_path = self._source(one, [_message("m01", "user", "第一个")])
        self._source(two, [_message("m02", "user", "第二个")])
        self._index([(one, "一"), (two, "二")])
        adapter = self._adapter()
        status = adapter.configure(policy="all", exclude=[two], owner_confirmed=True)
        self.assertEqual(("all", 1, 1), (status["archive_policy"], status["authorized_session_count"],
                                         status["excluded_session_count"]))
        adapter.sync(manual=True)
        archived = {item["source_session_id"] for item in adapter.store._registry()["sessions"].values()}
        self.assertEqual({one}, archived)

        adapter.configure(include=[two], owner_confirmed=True)
        adapter.sync(manual=True)
        self.assertEqual({one, two}, {item["source_session_id"] for item in adapter.store._registry()["sessions"].values()})
        adapter.configure(policy="paused", owner_confirmed=True)
        with first_path.open("ab") as handle:
            handle.write(_line(_message("m03", "user", "暂停期间")))
        self.assertEqual("paused", adapter.sync()["status"])

        restarted = self._adapter()
        self.assertEqual(("paused", "all"), (restarted.status()["archive_policy"], restarted.status()["resume_policy"]))
        restarted.configure(policy="all", owner_confirmed=True)
        self.assertEqual(1, restarted.sync(manual=True)["saved_messages"])
        self._source(future, [_message("m04", "user", "未来新会话")])
        self._index([(one, "一"), (two, "二"), (future, "三")])
        restarted.sync(manual=True)
        self.assertIn(future, {item["source_session_id"] for item in restarted.store._registry()["sessions"].values()})
        persisted = json.loads(restarted.state_path.read_text("utf-8"))
        self.assertEqual("all", persisted["archive_policy"])
        self.assertEqual([], persisted["selected_session_ids"])
        self.assertNotIn("authorized_session_ids", persisted)

        restarted.configure(policy="selected", authorize=[one], owner_confirmed=True)
        selected = restarted.status()
        self.assertEqual([one], selected["authorized_session_ids"])

    def test_batch_checkpoint_failure_isolation_and_title_priority(self):
        derived, fallback, failed_sid = "derived-title", "fallback-title", "failed-session"
        self._source(derived, [
            _message("m01", "user", "<environment_context>internal</environment_context>"),
            _message("m02", "user", r"C:\private\only-path.txt"),
            _message("m03", "user", "# 真正用户标题\n详情"),
        ])
        self._source(fallback, [{"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
                                "payload": {"type": "reasoning", "summary": ["hidden"]}}])
        self._source(failed_sid, [_message("m04", "user", "失败不阻塞")])
        self._index([(derived, f"Codex 会话 {derived[:8]}"),
                     (fallback, f"Codex 会话 {fallback[:8]}"),
                     (failed_sid, "故障会话")])
        adapter = self._adapter(); adapter.configure(policy="all", owner_confirmed=True)
        discovered = adapter.discover()
        self.assertEqual(("真正用户标题", "derived_first_user_message"),
                         (discovered[derived].title, discovered[derived].title_source))
        local_date=datetime(2026,1,1,0,0,2,tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d')
        self.assertTrue(discovered[fallback].title.startswith("未命名对话 "+local_date))
        self.assertEqual("fallback_timestamp", discovered[fallback].title_source)

        original = adapter._sync_one
        def isolate(state, source):
            if source.session_id == failed_sid:
                raise RuntimeError("fixture failure")
            return original(state, source)
        with mock.patch.object(adapter, "_sync_one", side_effect=isolate):
            batch = adapter.sync(manual=True, batch_size=8)
        self.assertTrue(any(row["status"] == "failed" for row in batch["sessions"]))
        self.assertTrue({derived, fallback}.issubset(
            {item["source_session_id"] for item in adapter.store._registry()["sessions"].values()}))
        self.assertEqual(1, adapter.status()["failed_session_count"])

        # A finite one-session batch persists a checkpoint that a new adapter can continue.
        adapter.sync(manual=True, batch_size=1)
        continued = self._adapter(); continued.sync(manual=True, batch_size=8)
        self.assertEqual(3, continued.status()["caught_up_session_count"])
        self._index([(derived, "客户端正式标题"), (fallback, f"Codex 会话 {fallback[:8]}"),
                     (failed_sid, "故障会话")])
        continued.sync(manual=True)
        manifest = next(item for item in continued.store._registry()["sessions"].values()
                        if item["source_session_id"] == derived)
        self.assertEqual(("客户端正式标题", "session_index"),
                         (manifest["title"], manifest["title_source"]))
        self.assertGreaterEqual(len(manifest["title_history"]), 2)

    def test_visibility_policy_repairs_older_internal_injection_without_body_in_audit(self):
        sid = "visibility-repair"
        injected = "The following is the Codex agent history added since your last approval assessment. [private]"
        self._source(sid, [_message("internal-old", "user", injected),
                           _message("visible", "user", "真实用户消息")])
        self._index([(sid, "可见性修复")]); adapter = self._adapter(); self._authorize(adapter, sid)
        adapter.sync(manual=True)
        session = next(iter(adapter.store._registry()["sessions"].values()))
        adapter.store.append_message(session["session_key"], message_id="legacy-internal", role="user",
                                     content=injected, materialize=True)
        state = adapter._state(); state["sessions"][sid]["visibility_policy_version"] = 1
        adapter._save_state(state); adapter.sync(manual=True)
        raw = (self.workspace.raw_message_store / f"{session['session_key']}.jsonl").read_text("utf-8")
        self.assertIn("真实用户消息", raw); self.assertNotIn("legacy-internal", raw)
        audit = (self.workspace.audit / "codex-visibility-recovery.jsonl").read_text("utf-8")
        self.assertIn("legacy-internal", audit); self.assertNotIn("[private]", audit)

    def test_needs_review_deduplicates_per_session_not_globally(self):
        event = {"timestamp": "2026-01-01T00:00:02Z", "type": "future_private",
                 "payload": {"shape": "redacted"}}
        self._source("review-one", [event]); self._source("review-two", [event])
        self._index([]); adapter = self._adapter()
        adapter.configure(policy="all", owner_confirmed=True); adapter.sync(manual=True)
        rows = [json.loads(line) for line in adapter.review_path.read_text("utf-8").splitlines()]
        self.assertEqual({"review-one", "review-two"}, {row["source_session_id"] for row in rows})
        self.assertEqual(2, adapter.status()["needs_review_event_count"])


if __name__ == "__main__":
    unittest.main()
