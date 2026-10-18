import unittest
from unittest.mock import patch

import knowledge_bridge.source_adapters as adapters


class SourceAdapterRegistryTests(unittest.TestCase):
    def test_saved_messages_and_check_time_are_not_connection_success(self):
        with patch('knowledge_bridge.codex_source_adapter.CodexSourceAdapter') as cls:
            cls.return_value.status.return_value={'enabled':True,'discovered_session_count':2,'authorized_session_count':2,
                'last_sync_at':'2026-09-25T01:00:00Z','last_message_sync_at':'2026-09-24T10:00:00Z','total_archived_message_count':17,'pending_backfill_count':1}
            row=adapters._codex_probe()
        self.assertEqual(17,row['saved']['message_count'])
        self.assertNotEqual(row['saved']['last_checked_at'],row['saved']['last_saved_at'])
        self.assertIn('待补档',row['saved']['last_result'])
        self.assertEqual('degraded',row['state'])
        self.assertNotIn('saved',adapters._generic_probe())

    def test_known_clients_select_verified_adapters_and_unknown_uses_push(self):
        self.assertEqual("codex_local", adapters.adapter_id_for("Codex"))
        self.assertEqual("hermes_local", adapters.adapter_id_for("hermes"))
        self.assertEqual("doubao_work_local", adapters.adapter_id_for("doubao_work"))
        self.assertEqual("generic_mcp_push", adapters.adapter_id_for("fictional-ai"))
        generic = adapters.for_client("fictional-ai")
        self.assertEqual("push_only", generic["state"])
        self.assertEqual("unsupported", generic["capabilities"]["source_discovery"])
        self.assertEqual("submitted_only", generic["capabilities"]["assistant_final_text"])

    def test_matrix_keeps_capabilities_separate_and_reports_probe_failure(self):
        good = lambda: {"state": "automatic", "source_detected": True,
                        "summary": "ok", "detail": "ok",
                        "capabilities": adapters._capabilities(user_text="supported")}
        bad = lambda: (_ for _ in ()).throw(OSError("private path omitted"))
        fixtures = (("known", "Known", "local", good),
                    ("unknown", "Unknown", "local", bad))
        with patch.object(adapters, "ADAPTERS", fixtures):
            rows = adapters.matrix()
        self.assertEqual("supported", rows[0]["capabilities"]["user_text"])
        self.assertEqual("unsupported", rows[0]["capabilities"]["attachments"])
        self.assertEqual("error", rows[1]["state"])
        self.assertEqual("OSError", rows[1]["detail"])

    def test_generator_is_explicitly_unimplemented(self):
        status = adapters.generator_status()
        self.assertEqual("not_implemented", status["state"])
        self.assertIn("隔离验证", "".join(status["requirements"]))

    def test_doubao_binding_failures_are_paused_not_active(self):
        for runtime_status in ("no_active_doubao_binding",
                               "multiple_active_doubao_bindings_require_selection"):
            with self.subTest(runtime_status=runtime_status), patch(
                    "knowledge_bridge.doubao_session_adapter.status",
                    return_value={"source_found": True, "status": runtime_status,
                                  "public_message_count": 1, "review_message_count": 53}):
                row = adapters._doubao_probe()
                self.assertEqual("paused", row["state"])
                self.assertNotIn("只自动收集候选", row["summary"])
        with patch("knowledge_bridge.doubao_session_adapter.status", return_value={
                "source_found": True, "status": "candidate_collection_active_public_visibility_limited",
                "public_message_count": 1, "review_message_count": 53}):
            healthy = adapters._doubao_probe()
        self.assertEqual("partial", healthy["state"])
        self.assertIn("公开正文 1 条", healthy["detail"])

    def test_codex_requires_source_and_successful_runtime_evidence(self):
        with patch("knowledge_bridge.codex_source_adapter.CodexSourceAdapter") as cls:
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 0, "last_error": None}
            missing = adapters._codex_probe()
            self.assertEqual("source_unavailable", missing["state"])
            self.assertNotIn("正在", missing["summary"])
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 2,
                "last_error": "fixture failure", "last_sync_at": "2026-01-01"}
            failed = adapters._codex_probe()
            self.assertEqual("error", failed["state"])
            self.assertIn("fixture failure", failed["detail"])
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 2,
                "authorized_session_count": 2,
                "last_error": None, "last_sync_at": "2026-01-01",
                "failed_session_count": 0, "missing_attachment_count": 0}
            healthy = adapters._codex_probe()
            self.assertEqual("automatic", healthy["state"])

    def test_codex_snapshot_time_is_not_sync_evidence_and_empty_scope_is_not_running(self):
        with patch("knowledge_bridge.codex_source_adapter.CodexSourceAdapter") as cls:
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 2,
                "authorized_session_count": 2, "last_error": None,
                "snapshot_updated_at": "2026-01-01", "last_sync_at": None}
            configured_only = adapters._codex_probe()
            self.assertEqual("awaiting_first_sync", configured_only["state"])
            self.assertNotIn("正在", configured_only["summary"])
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 59,
                "authorized_session_count": 0, "last_error": None,
                "last_sync_at": "2026-01-01"}
            empty_scope = adapters._codex_probe()
            self.assertEqual("awaiting_authorization", empty_scope["state"])
            self.assertIn("授权会话为 0", empty_scope["detail"])

    def test_codex_gap_or_pending_backfill_is_not_reported_as_fully_caught_up(self):
        with patch("knowledge_bridge.codex_source_adapter.CodexSourceAdapter") as cls:
            cls.return_value.status.return_value = {
                "enabled": True, "discovered_session_count": 4,
                "authorized_session_count": 3, "last_error": None,
                "last_sync_at": "2026-01-01", "pending_backfill_count": 1,
                "gap_detected": True}
            degraded = adapters._codex_probe()
        self.assertEqual("degraded", degraded["state"])
        self.assertIn("待补档 1", degraded["detail"])
        self.assertIn("断档检测：有", degraded["detail"])

    def test_connection_status_does_not_attribute_global_collector_to_revoked_client(self):
        adapter = {"adapter_id": "codex_local", "runtime": {
            "state": "automatic", "summary": "本机采集器正在自动归档", "detail": "ok"}}
        archive = {"bound": True}
        policies = [{"scope": "evidence", "operation": "raw_archive", "decision": "allow", "selector": {}}]
        revoked = adapters.connection_status({"status": "revoked"}, archive, policies, adapter)
        self.assertEqual("revoked", revoked["state"])
        self.assertNotIn("自动归档", revoked["summary"])
        active = adapters.connection_status({"status": "authenticated"}, archive, policies, adapter)
        self.assertTrue(active["state"].startswith("independent_collector_"))
        self.assertIn("独立运行", active["detail"])


if __name__ == "__main__":
    unittest.main()
