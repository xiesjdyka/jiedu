import json
import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.bridge_settings import load_settings, reminder_state, save_settings
from knowledge_bridge.session_manager import refresh_memory_context, session_append, session_start, session_status,record_bootstrap


class SessionManagerTests(unittest.TestCase):
    def test_full_session_exact_archive_idempotency_and_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); state=root/"sessions.json"; archive=root/"archives"
            session_start("fixture","test-client","fixture","full","Knowledge Bridge",state_path=state,archive_root=archive)
            user="原文  \n# 不能改格式\n\n重复。"
            assistant="回答\r\n原样保留"
            first=session_append("fixture","m1","user",user,"2026-01-01T00:00:00Z",state_path=state,archive_root=archive)
            duplicate=session_append("fixture","m1","user",user,"2026-01-01T00:00:00Z",state_path=state,archive_root=archive)
            session_append("fixture","m2","assistant",assistant,"2026-01-01T00:00:01Z",state_path=state,archive_root=archive)
            rows=[json.loads(line) for line in next(archive.glob("*.jsonl")).read_text("utf-8").splitlines()]
            self.assertTrue(first["saved"]); self.assertTrue(duplicate["duplicate"]); self.assertEqual(2,len(rows))
            self.assertEqual(user,rows[0]["content"]); self.assertEqual(assistant,rows[1]["content"])
            status=session_status("fixture",state_path=state)
            self.assertEqual(1,status["turn_count"]); self.assertEqual("estimated",status["token_kind"]); self.assertGreater(status["display_tokens"],0)

    def test_readonly_is_unavailable_and_cannot_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); state=root/"sessions.json"; archive=root/"archives"
            session_start("read","codex","fixture","readonly",state_path=state,archive_root=archive)
            status=session_status("read",state_path=state)
            self.assertFalse(status["turn_count_available"]); self.assertEqual("unavailable",status["token_kind"])
            with self.assertRaises(PermissionError): session_append("read","m1","user","x","2026-01-01T00:00:00Z",state_path=state,archive_root=archive)

    def test_session_isolation_and_core_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/"sessions.json";archive=root/"archives"
            session_start("A","hermes","test","full",state_path=state,archive_root=archive);session_start("B","hermes","test","full",state_path=state,archive_root=archive)
            session_append("A","a1","user","A","2026-01-01T00:00:00Z",state_path=state,archive_root=archive)
            session_append("B","b1","user","B","2026-01-01T00:00:00Z",state_path=state,archive_root=archive);session_append("B","b2","assistant","B2","2026-01-01T00:00:01Z",state_path=state,archive_root=archive)
            self.assertEqual(0,session_status("A",state_path=state)["turn_count"]);self.assertEqual(1,session_status("B",state_path=state)["turn_count"])
            self.assertEqual("pending",session_status("A",state_path=state)["core_status"])
            read=record_bootstrap("A","test-core-v1","manual",state_path=state);self.assertEqual("read_manual",read["core_status"]);self.assertEqual("test-core-v1",read["core_version"])

    def test_reminder_and_refresh_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); state=root/"sessions.json"; archive=root/"archives"
            session_start("thirty","fixture","fixture","full",state_path=state,archive_root=archive)
            for index in range(30):
                session_append("thirty",f"u{index}","user","u","2026-01-01T00:00:00Z",state_path=state,archive_root=archive)
                session_append("thirty",f"a{index}","assistant","a","2026-01-01T00:00:01Z",state_path=state,archive_root=archive)
            status=session_status("thirty",state_path=state)
            self.assertTrue(reminder_state(status,{"weight_boost":{"reminders":True,"turn_threshold":30}})["due"])
            result=refresh_memory_context("thirty",False,state_path=state)
            self.assertEqual("refresh_required",result["status"]); self.assertFalse(result["executed"])

    def test_settings_persist_reject_public_bind_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"settings.json"
            saved=save_settings({"console":{"port":9876},"weight_boost":{"turn_threshold":20}},path)
            self.assertEqual(9876,load_settings(path)["console"]["port"]); self.assertEqual(20,saved["weight_boost"]["turn_threshold"])
            before=path.read_bytes()
            with self.assertRaises(ValueError): save_settings({"console":{"host":"0.0.0.0"}},path)
            self.assertEqual(before,path.read_bytes())
            enabled=save_settings({"weight_boost":{"automatic":True}},path)
            self.assertTrue(enabled["weight_boost"]["automatic"])
            self.assertFalse(enabled["weight_boost"]["formal_core_enabled"])


if __name__=="__main__": unittest.main()
