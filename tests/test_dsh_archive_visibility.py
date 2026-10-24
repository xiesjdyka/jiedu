"""A local DSH save must remain visible without being attributed to MCP push."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import zstd

from knowledge_bridge.client_config import DSHAdapter
from knowledge_bridge.archive_store import ArchiveStore, NormalizedMessage
from knowledge_bridge.dsh_source_adapter import DSHSourceAdapter
from knowledge_bridge.source_adapters import _claude_probe, _dsh_probe
from knowledge_bridge.source_connections import SourceConnections
from knowledge_bridge.workspace import initialize_workspace
import knowledge_bridge.session_archive as session_archive


class DSHArchiveVisibilityTests(unittest.TestCase):
    def test_claude_saved_summary_and_missing_manifest_are_truthful(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = initialize_workspace(Path(temporary) / "workspace")
            store = ArchiveStore(workspace, recover=False)
            session = store.ensure_session(source_key="claude", source_session_id="synthetic-session",
                                           source_instance="claude:synthetic", title="Claude synthetic",
                                           acquisition_method="claude_local_cache_readonly")
            store.append_normalized_message(session["session_key"], NormalizedMessage(
                message_id="human-1", role="user", content="合成消息", timestamp=None))
            state = {"enabled": True, "ever_configured": True, "source_detected": True,
                     "history": {"current_decode_errors": 0, "archived_messages": 1},
                     "last_sync_at": "2026-10-23T00:00:00+00:00", "last_error": None}
            adapter = SimpleNamespace(context=workspace, status=lambda: state)
            with patch("knowledge_bridge.claude_source_adapter.ClaudeSourceAdapter", return_value=adapter):
                saved = _claude_probe()
            self.assertEqual("automatic", saved["state"])
            self.assertEqual(1, saved["saved"]["session_count"])
            self.assertEqual(1, saved["saved"]["message_count"])
            manifest = next(workspace.raw_archive.rglob("manifest.json"))
            hidden = manifest.with_suffix(".hidden")
            manifest.rename(hidden)
            try:
                with patch("knowledge_bridge.claude_source_adapter.ClaudeSourceAdapter", return_value=adapter):
                    gap = _claude_probe()
                self.assertEqual("partial", gap["state"])
                self.assertEqual(0, gap["saved"]["message_count"])
                self.assertEqual("档案目录待核对", gap["saved"]["last_result"])
            finally:
                hidden.rename(manifest)

    def test_custom_home_saved_archive_and_connection_counts_stay_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = initialize_workspace(root / "知识 工作区")
            custom_home = root / "另一磁盘路径 空格" / "自定义 DSH"
            sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            source = custom_home / "sessions" / "profile" / ("session-" + sid) / "session.v4.jsonl.zstd"
            source.parent.mkdir(parents=True)
            events = [
                {"type": "user/message", "seq": 1, "time": 1790000000001,
                 "data": {"source": {"kind": "user"}, "id": "u1",
                          "content": [{"type": "text", "text": "测试用户消息"}]}},
                {"type": "assistant/message", "seq": 2, "time": 1790000000002,
                 "data": {"turn": 1, "message": {"role": "assistant", "id": "a1",
                          "content": [{"type": "text", "text": "测试最终答复"}]}}},
                {"type": "turn/end", "seq": 3, "time": 1790000000003,
                 "data": {"turn": 1, "reason": {"kind": "completed"}}},
            ]
            source.write_bytes(zstd.compress("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                                    for row in events).encode("utf-8")))
            with patch.dict(os.environ, {"DSH_HOME": str(custom_home)}):
                adapter = DSHSourceAdapter(workspace)
                self.assertEqual(custom_home.resolve(), adapter.root)
                self.assertEqual(custom_home / "profiles" / "desktop" / "cordis.patch.yml",
                                 DSHAdapter().path)
                adapter.configure("all", owner_confirmed=True)
                self.assertEqual(2, adapter.sync()["history"]["archived_messages"])
                with patch("knowledge_bridge.dsh_source_adapter.DSHSourceAdapter", return_value=adapter):
                    probe = _dsh_probe()
                self.assertEqual(1, probe["saved"]["session_count"])
                self.assertEqual(2, probe["saved"]["message_count"])
                self.assertEqual("90_原始档案/DSH", probe["saved"]["location"])
                with patch.object(session_archive, "CANONICAL_ROOT", workspace.raw_archive), \
                     patch.object(session_archive, "SESSIONS_PATH", workspace.sessions_state), \
                     patch.object(session_archive, "DISPLAY_CACHE", workspace.cache / "archive-display.json"), \
                     patch.object(session_archive, "active_workspace", return_value=workspace):
                    listed = session_archive.list_archive_summaries(client="dsh")
                self.assertEqual(1, listed["total"])
                self.assertEqual(2, listed["items"][0]["message_count"])
                connections = SourceConnections(workspace)
                connections.bind("client-identity", connections.descriptor("dsh"))
                pushed = connections.status("client-identity")
                self.assertEqual(0, pushed["message_count"])
                self.assertIn("当前连接主动提交", pushed["collection_status"])
                manifest = next(workspace.raw_archive.rglob("manifest.json"))
                hidden = manifest.with_suffix(".hidden")
                manifest.rename(hidden)
                try:
                    with patch("knowledge_bridge.dsh_source_adapter.DSHSourceAdapter", return_value=adapter):
                        gap = _dsh_probe()
                    self.assertEqual("degraded", gap["state"])
                    self.assertEqual(0, gap["saved"]["message_count"])
                    self.assertEqual("档案目录待核对", gap["saved"]["last_result"])
                finally:
                    hidden.rename(manifest)


if __name__ == "__main__":
    unittest.main()
