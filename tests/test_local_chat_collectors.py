import json
import tempfile
import unittest
import hashlib
import os
from pathlib import Path
from unittest import mock

import zstd

from knowledge_bridge.claude_source_adapter import ClaudeSourceAdapter
from knowledge_bridge.dsh_source_adapter import DSHSourceAdapter, complete_frames, _read_candidate, SourceFormatError
from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.prompt_workflow import distillation_gate
from knowledge_bridge.prompt_templates import mother_distillation_prompt
from knowledge_bridge.workspace import initialize_workspace


def _frame(*events):
    return zstd.compress(b"".join(
        (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for event in events))


def _event(kind, seq, data):
    return {"type": kind, "seq": seq, "time": 1790000000000 + seq, "data": data}


class LocalChatCollectorsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = initialize_workspace(self.root / "workspace")
        self.dsh = self.root / "dsh"
        self.sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.source = self.dsh / "sessions" / "profile" / ("session-" + self.sid) / "session.v4.jsonl.zstd"
        self.source.parent.mkdir(parents=True)
        self.claude_db = self.root / "claude.leveldb"
        self.claude_db.mkdir()
        (self.claude_db / "CURRENT").write_text("MANIFEST-1\n")
        self.claude_blob = self.root / "claude.blob"
        self.claude_blob.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _workspace_files(self):
        return {str(path.relative_to(self.workspace.root)): path.read_bytes()
                for path in self.workspace.root.rglob("*") if path.is_file()}

    def _all_workspace_bytes(self):
        return b"\n".join(path.read_bytes() for path in self.workspace.root.rglob("*") if path.is_file())

    def test_dsh_completed_turn_only_partial_tail_and_replay(self):
        hidden = "HIDDEN_DSH_REASONING_AND_TOOL_SENTINEL"
        user = _event("user/message", 1, {
            "source": {"kind": "user"}, "id": "human-1",
            "content": [{"type": "text", "text": "用户提问"}]})
        synthetic = _event("user/message", 2, {
            "source": {"kind": "runtime-context"}, "id": "runtime-1",
            "content": [{"type": "text", "text": hidden}]})
        tool_step = _event("assistant/message", 3, {
            "turn": 1, "step": 1, "message": {"role": "assistant", "id": "tool-step",
            "content": [{"type": "reasoning", "text": hidden},
                        {"type": "text", "text": hidden},
                        {"type": "tool-call", "name": "shell", "arguments": hidden}]}})
        tool_result = _event("tool/result", 4, {
            "turn": 1, "message": {"role": "tool", "content": [{"type": "text", "text": hidden}]}})
        final = _event("assistant/message", 5, {
            "turn": 1, "step": 2, "message": {"role": "assistant", "id": "final-1",
            "content": [{"type": "reasoning", "text": hidden},
                        {"type": "text", "text": "The final answer is in English."}]}})
        end = _event("turn/end", 6, {"turn": 1, "reason": {"kind": "completed"}})
        first = _frame(user, synthetic, tool_step)
        second = _frame(tool_result, final)
        third = _frame(end)
        self.source.write_bytes(first + second + third[:5])
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        before = self._workspace_files()
        adapter.discover(); adapter.status()
        self.assertEqual(before, self._workspace_files())
        adapter.configure("selected", [self.sid], owner_confirmed=True)
        first_result = adapter.sync()
        self.assertEqual(1, first_result["saved_this_sync"])
        self.assertEqual(len(first + second), first_result["sessions"][self.sid]["confirmed_offset"])
        self.assertTrue(first_result["sessions"][self.sid]["has_pending_turn"])
        self.assertNotIn(hidden.encode(), self._all_workspace_bytes())
        self.source.write_bytes(first + second + third)
        second_result = adapter.sync()
        self.assertEqual(1, second_result["saved_this_sync"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        all_bytes = self._all_workspace_bytes()
        self.assertIn(b"The final answer is in English.", all_bytes)
        self.assertNotIn(hidden.encode(), all_bytes)
        fourth = _frame(_event("user/message", 7, {
            "source": {"kind": "user"}, "id": "human-2",
            "content": [{"type": "text", "text": "后续追加"}]}))
        self.source.write_bytes(first + second + third + fourth)
        self.assertEqual(1, adapter.sync()["saved_this_sync"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])

    def test_dsh_invalid_source_preserves_cursor_and_records_error(self):
        self.source.write_bytes(_frame(_event("user/message", 1, {
            "source": "invalid", "content": [{"type": "text", "text": "hidden"}]})))
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        adapter.configure("all", owner_confirmed=True)
        result = adapter.sync()
        self.assertEqual(0, result["saved_this_sync"])
        self.assertEqual(0, result["sessions"][self.sid]["confirmed_offset"])
        self.assertTrue(result["sessions"][self.sid]["last_error"])

    def test_dsh_crash_after_durable_append_replays_idempotently(self):
        self.source.write_bytes(_frame(_event("user/message", 1, {
            "source": {"kind": "user"}, "id": "human-1",
            "content": [{"type": "text", "text": "Question"}]}),
            _event("assistant/message", 2, {
                "turn": 1, "step": 1, "message": {"role": "assistant", "id": "final-1",
                "content": [{"type": "text", "text": "Answer"}]}}),
            _event("turn/end", 3, {"turn": 1, "reason": {"kind": "completed"}})))
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        adapter.configure("selected", [self.sid], owner_confirmed=True)
        append = ArchiveStore.append_normalized_message
        calls = 0
        def crash_after_first(store, *args, **kwargs):
            nonlocal calls
            result = append(store, *args, **kwargs)
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated process interruption")
            return result
        with mock.patch.object(ArchiveStore, "append_normalized_message", crash_after_first):
            with self.assertRaises(RuntimeError):
                adapter.sync()
        resumed = adapter.sync()
        self.assertEqual(1, resumed["saved_this_sync"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        self.assertEqual(2, resumed["history"]["archived_messages"])

    def test_dsh_concatenated_frames_and_unfinished_tail(self):
        raw = _frame(_event("session", 0, {})) + _frame(_event("session", 1, {}))
        self.assertEqual(2, len(list(complete_frames(raw))))
        self.assertEqual(1, len(list(complete_frames(raw[:-7]))))

    def test_dsh_bounded_scan_keeps_only_locator_across_syncs(self):
        first = _frame(_event("user/message", 1, {"source": {"kind": "user"},
            "id": "question", "content": [{"type": "text", "text": "Question"}]}))
        second = _frame(_event("assistant/message", 2, {"turn": 1, "step": 1,
            "message": {"role": "assistant", "id": "answer",
                        "content": [{"type": "text", "text": "Final response"}]}}))
        third = _frame(_event("turn/end", 3, {"turn": 1, "reason": {"kind": "completed"}}))
        self.source.write_bytes(first + second + third)
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        adapter.configure("selected", [self.sid], owner_confirmed=True)
        adapter.frame_budget = 1
        self.assertEqual(1, adapter.sync()["saved_this_sync"])
        self.assertEqual(len(first), adapter._load()["sessions"][self.sid]["confirmed_offset"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        row = adapter._load()["sessions"][self.sid]
        self.assertEqual(len(first + second), row["confirmed_offset"])
        self.assertEqual({"turn", "seq", "frame_offset", "frame_sha256"}, set(row["pending_turn"]))
        self.assertNotIn(b"Final response", self._all_workspace_bytes())
        self.assertEqual(1, adapter.sync()["saved_this_sync"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        self.assertIsNone(adapter._load()["sessions"][self.sid]["pending_turn"])

    def test_dsh_source_change_keeps_checkpoint_and_requires_review(self):
        original = _frame(_event("user/message", 1, {"source": {"kind": "user"},
            "id": "question", "content": [{"type": "text", "text": "First"}]}))
        self.source.write_bytes(original)
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        adapter.configure("selected", [self.sid], owner_confirmed=True)
        adapter.sync()
        checkpoint = adapter._load()["sessions"][self.sid]["confirmed_offset"]
        self.source.write_bytes(b"")
        result = adapter.sync()
        self.assertEqual(checkpoint, adapter._load()["sessions"][self.sid]["confirmed_offset"])
        self.assertIn("截短", result["sessions"][self.sid]["last_error"])
        self.source.write_bytes(_frame(_event("user/message", 1, {
            "source": {"kind": "user"}, "id": "question",
            "content": [{"type": "text", "text": "Changed"}]})))
        result = adapter.sync()
        self.assertEqual(checkpoint, adapter._load()["sessions"][self.sid]["confirmed_offset"])
        self.assertTrue(result["sessions"][self.sid]["last_error"])

    def test_dsh_pending_candidate_requires_original_frame_digest(self):
        frame = _frame(_event("assistant/message", 2, {"turn": 1, "step": 1,
            "message": {"role": "assistant", "id": "answer",
                        "content": [{"type": "text", "text": "Final response"}]}}))
        self.source.write_bytes(frame)
        with self.assertRaisesRegex(SourceFormatError, "已变化"):
            _read_candidate(self.source, {"turn": 1, "seq": 2, "frame_offset": 0,
                                          "frame_sha256": "0" * 64})

    def test_dsh_missing_attachment_is_rechecked_without_replaying_message(self):
        body = b"synthetic-image-bytes"
        digest = hashlib.sha256(body).hexdigest()
        self.source.write_bytes(_frame(_event("user/message", 1, {
            "source": {"kind": "user"}, "id": "human-image",
            "content": [{"type": "text", "text": "请看图"},
                        {"type": "image", "attachment": {
                            "attachmentId": "sha256:" + digest, "name": "image.png",
                            "mediaType": "image/png"}}]})))
        adapter = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        adapter.configure("selected", [self.sid], owner_confirmed=True)
        self.assertEqual(1, adapter.sync()["saved_this_sync"])
        state = adapter._load()
        self.assertEqual(1, len(state["sessions"][self.sid]["missing_attachments"]))
        object_file = self.dsh / "attachments" / "v1" / "objects" / digest[:2] / digest
        object_file.parent.mkdir(parents=True)
        object_file.write_bytes(body)
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        self.assertFalse(adapter._load()["sessions"][self.sid]["missing_attachments"])
        self.assertIn(body, self._all_workspace_bytes())

    def test_claude_final_visible_text_only_and_local_cache_boundary(self):
        hidden = "HIDDEN_CLAUDE_THINKING_TOOL_SYSTEM_SENTINEL"
        sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        tree = {"conversationUuid": sid, "messages": [
            {"uuid": "system-1", "sender": "system",
             "content": [{"type": "text", "text": hidden}]},
            {"uuid": "human-1", "sender": "human", "created_at": "2026-01-01T00:00:00Z",
             "content": [{"type": "text", "text": "Hello"}],
             "attachments": [], "files": [], "files_v2": []},
            {"uuid": "assistant-draft", "sender": "assistant", "stop_reason": "tool_use",
             "content": [{"type": "text", "text": hidden}]},
            {"uuid": "assistant-final", "sender": "assistant", "stop_reason": "end_turn",
             "content": [{"type": "thinking", "thinking": hidden},
                         {"type": "text", "text": hidden},
                         {"type": "tool_use", "input": hidden},
                         {"type": "tool_result", "content": hidden},
                         {"type": "text", "text": "Final English answer."}],
             "attachments": [], "files": [], "files_v2": []}]}
        reader = lambda: ({sid: tree}, 0, True)
        adapter = ClaudeSourceAdapter(self.workspace, leveldb=self.claude_db,
                                      blob=self.claude_blob, reader=reader)
        before = self._workspace_files()
        adapter.discover(); adapter.status()
        self.assertEqual(before, self._workspace_files())
        adapter.configure("selected", [sid], owner_confirmed=True)
        self.assertEqual(2, adapter.sync()["saved_this_sync"])
        self.assertEqual(0, adapter.sync()["saved_this_sync"])
        all_bytes = self._all_workspace_bytes()
        self.assertIn(b"Final English answer.", all_bytes)
        self.assertNotIn(hidden.encode(), all_bytes)
        self.assertIn("云端", adapter.status()["history_coverage"])

    def test_claude_current_cache_gap_is_visible_without_blocking_known_local_session(self):
        sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        tree = {"conversationUuid": sid, "messages": [
            {"uuid": "human", "sender": "human", "content": [{"type": "text", "text": "Hi"}],
             "attachments": [], "files": [], "files_v2": []}]}
        adapter = ClaudeSourceAdapter(self.workspace, leveldb=self.claude_db,
                                      blob=self.claude_blob, reader=lambda: ({sid:tree},1,True))
        adapter.configure("selected", [sid], owner_confirmed=True)
        result = adapter.sync()
        self.assertEqual(1, result["saved_this_sync"])
        self.assertEqual(1, result["history"]["current_decode_errors"])
        self.assertIn("不可读", result["last_error"])

    def test_disabled_probe_and_worker_do_not_create_workspace_state(self):
        from knowledge_bridge import dsh_source_adapter, claude_source_adapter, source_adapters
        self.source.write_bytes(_frame(_event("session", 0, {})))
        dsh = DSHSourceAdapter(self.workspace, source_root=self.dsh)
        claude = ClaudeSourceAdapter(self.workspace, leveldb=self.claude_db,
                                     blob=self.claude_blob, reader=lambda: ({},0,True))
        before = self._workspace_files()
        dsh.discover(); dsh.status(); claude.discover(); claude.status()
        with mock.patch.object(dsh_source_adapter, "DSHSourceAdapter", return_value=dsh), \
             mock.patch.object(claude_source_adapter, "ClaudeSourceAdapter", return_value=claude), \
             mock.patch.object(source_adapters, "ADAPTERS", (
                ("dsh_local", "DSH", "authorized_local_read", source_adapters._dsh_probe),
                ("claude_local", "Claude", "authorized_local_read", source_adapters._claude_probe))):
            source_adapters.matrix()
            class Once:
                done = False
                def is_set(self): return self.done
                def wait(self, _): self.done = True
            dsh_source_adapter.run(stop_event=Once())
            claude_source_adapter.run(stop_event=Once())
        self.assertEqual(before, self._workspace_files())

    def test_claude_multiple_local_profiles_require_explicit_choice(self):
        local = self.root / "local"
        roaming = self.root / "roaming"
        store = (local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" /
                 "Roaming" / "Claude" / "IndexedDB" /
                 "https_claude.ai_0.indexeddb.leveldb")
        desktop = roaming / "Claude" / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
        for path in (store, desktop):
            path.mkdir(parents=True)
            (path / "CURRENT").write_text("MANIFEST-1\n")
        before = self._workspace_files()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA":str(local),"APPDATA":str(roaming)}):
            ambiguous = ClaudeSourceAdapter(self.workspace)
            self.assertTrue(ambiguous.status()["source_ambiguous"])
            with self.assertRaisesRegex(ValueError, "多个"):
                ambiguous.discover()
            chosen = ClaudeSourceAdapter(self.workspace, profile_id="desktop")
            self.assertEqual("desktop", chosen.status()["selected_profile"])
        self.assertEqual(before, self._workspace_files())

    def test_new_source_gate_only_counts_explicit_participation_and_names_cache_boundary(self):
        inactive = {"sources":[{"id":"dsh","detected":True,"readable":True,
                                "participating":False,"history":{"status":"reading"}}]}
        self.assertNotIn("DSH", distillation_gate(inactive)["sources"])
        active = {"sources":[{"id":"claude","detected":True,"readable":True,
                              "participating":True,"history":{"status":"partial",
                              "failed_missing":1}}]}
        gate = distillation_gate(active)
        self.assertIn("Claude", gate["sources"])
        self.assertFalse(gate["ready"])
        self.assertIn("云端", mother_distillation_prompt(["Claude"], "测试库"))
        paused = {"sources":[{"id":"dsh","detected":True,"readable":True,
                               "participating":True,"paused":True,
                               "history":{"status":"complete"}}]}
        self.assertFalse(distillation_gate(paused)["ready"])
        self.assertTrue(any("暂停" in reason for reason in distillation_gate(paused)["reasons"]))

    def test_claude_evicted_current_cache_does_not_reuse_old_completion(self):
        from knowledge_bridge import local_source_status
        sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        tree = {"conversationUuid": sid, "messages": [{"uuid": "human", "sender": "human",
            "content": [{"type": "text", "text": "Hello"}],
            "attachments": [], "files": [], "files_v2": []}]}
        current = {"trees": {sid: tree}}
        adapter = ClaudeSourceAdapter(self.workspace, leveldb=self.claude_db,
            blob=self.claude_blob, reader=lambda: (current["trees"], 0, True))
        adapter.configure("all", owner_confirmed=True)
        adapter.sync()
        self.assertEqual([sid], adapter.status()["current_session_ids"])
        current["trees"] = {}
        adapter.sync()
        self.assertEqual([], adapter.status()["current_session_ids"])
        self.assertIn(sid, adapter.status()["sessions"])


if __name__ == "__main__":
    unittest.main()
