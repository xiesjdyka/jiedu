import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import memory_pipeline as mp


class MemoryPipelineTests(unittest.TestCase):
    def test_raw_conversation_is_append_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            payload={"conversation_id":"fixture-conversation","message_id":"fixture-001","role":"user","content":"fixture only","source":"unittest","attachment_refs":["IMG_FIXTURE"],"provenance":{"test":True}}
            first=mp.save_raw_conversation(payload,root)
            second=mp.save_raw_conversation(payload,root)
            self.assertTrue(first["saved"]); self.assertTrue(second["duplicate"])
            self.assertEqual(1,len((root/"messages.jsonl").read_text("utf-8").splitlines()))
            with self.assertRaises(ValueError): mp.save_raw_conversation(payload|{"content":"conflict"},root)

    def test_candidate_review_stages_without_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(mp,"VAULT",root/'vault'),patch.object(mp,"CANDIDATE_EVENTS",root/"candidates.jsonl"),patch.object(mp,"STAGING_EVENTS",root/"staging.jsonl"),patch.object(mp,"AUDIT_EVENTS",root/"audit.jsonl"):
                result=mp.create_candidate({"type":"collaboration_rule","title":"测试规则","content":"仅供fixture验证","evidence":[{"conversation_id":"fixture","message_id":"001"}]},"deterministic-test")
                cid=result["candidate"]["candidate_id"]
                mp.edit_candidate(cid,{"content":"仅供fixture验证，已修改"})
                approved=mp.review_candidate(cid,"accept")
                self.assertEqual("approved_staging",approved["status"])
                preview=mp.memory_apply_dry_run(cid)
                self.assertTrue(preview["dry_run"]); self.assertIn("仅供fixture验证，已修改",preview["diff"])
                self.assertEqual(1,len((root/"staging.jsonl").read_text("utf-8").splitlines()))

    def test_deterministic_provider_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(mp,"CANDIDATE_EVENTS",root/"candidates.jsonl"),patch.object(mp,"STAGING_EVENTS",root/"staging.jsonl"),patch.object(mp,"AUDIT_EVENTS",root/"audit.jsonl"):
                messages=[{"conversation_id":"fixture","message_id":"002","content":"[LESSON] 连续失败后换路线"}]
                output=mp.extract_candidates(messages,mp.DeterministicTestProvider())
                again=mp.extract_candidates(messages,mp.DeterministicTestProvider())
                self.assertTrue(output[0]["created"]); self.assertFalse(again[0]["created"])


    def test_candidate_written_lifecycle_requires_owner_writer_then_reverts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(mp,"CANDIDATE_EVENTS",root/"candidates.jsonl"),patch.object(mp,"STAGING_EVENTS",root/"staging.jsonl"),patch.object(mp,"AUDIT_EVENTS",root/"audit.jsonl"):
                result=mp.create_candidate({"type":"knowledge","title":"待写入测试","content":"仅在验收fixture中使用","evidence":[{"conversation_id":"fixture","message_id":"003"}]},"deterministic-test")
                cid=result["candidate"]["candidate_id"]
                mp.review_candidate(cid,"accept")
                with self.assertRaises(ValueError): mp.mark_candidate_written("nonexistent","v_x")
                written=mp.mark_candidate_written(cid,"v_fixture_123")
                self.assertEqual("written",written["status"]); self.assertEqual("v_fixture_123",written["writer_version"])
                reverted=mp.revert_candidate_written(cid,"v_fixture_123")
                self.assertEqual("approved_staging",reverted["status"]); self.assertNotIn("writer_version",reverted)
                events=mp._read_jsonl(root/"candidates.jsonl")
                self.assertEqual(["created","accepted","written","reverted"],[e["event"] for e in events])

    def test_package_status_distinguishes_review_done_from_write_done(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sessions=root/"sessions.jsonl";archives=root/"archives";state=root/"manual.json";packages=root/"packages";results=root/"results";events=root/"candidates.jsonl";audit=root/"audit.jsonl"
            from knowledge_bridge.session_manager import session_start,session_append
            from knowledge_bridge import manual_extraction as manual
            session_start("hermes:B","hermes","fixture","full",session_title="B",state_path=sessions,archive_root=archives)
            session_append("hermes:B","m1","user","fixture正文","2026-08-16T00:00:00+00:00",state_path=sessions,archive_root=archives)
            manual.set_session_state('hermes:B','pending',state,sessions)
            package=manual.export_package(False,state,sessions,archives,packages)
            payload={"format":"KB_MEMORY_CANDIDATES_V1","package_id":package["package_id"],"candidates":[{"candidate_id":"ext-a","package_id":package["package_id"],"type":"preference","proposed_content":"fixture","target_layer":"00_系统","target_path":"00_系统/我的长期画像.md","source_session_ids":["hermes:B"],"source_message_ids":["m1"],"evidence":[{"session_id":"hermes:B","message_id":"m1","quote":"x"}],"reason":"r","confidence":.9,"conflict_status":"none","conflict_with":[],"proposed_action":"update","supersedes":[],"notes":""}]}
            with patch("knowledge_bridge.memory_pipeline.CANDIDATE_EVENTS",events),patch("knowledge_bridge.memory_pipeline.STAGING_EVENTS",root/"staging.jsonl"),patch("knowledge_bridge.memory_pipeline.AUDIT_EVENTS",audit):
                imported=manual.import_result(payload,state,sessions,archives,results)
                cid=imported["candidates"][0]["candidate_id"]
                from knowledge_bridge.memory_pipeline import review_candidate,mark_candidate_written
                review_candidate(cid,"accept");manual.update_review_state(cid,state)
                status_after_accept=manual.status(state,sessions,archives)
                self.assertEqual("已审核 · 待写入",status_after_accept["current_node_status"])
                mark_candidate_written(cid,"v_abc");manual.update_review_state(cid,state)
                status_after_write=manual.status(state,sessions,archives)
                self.assertEqual("已写入",status_after_write["current_node_status"])


if __name__=="__main__": unittest.main()
