import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from knowledge_bridge import web


class CandidateWriterAuthorizationTests(unittest.TestCase):
    def candidate(self, status="pending"):
        return {
            "candidate_id": "mem_fixture",
            "title": "测试候选",
            "content": "# 测试\n",
            "status": status,
            "evidence": [{"source_path": "03_知识/来源.md", "scope": "formal"}],
            "conflicts": [],
            "supersedes": [],
            "provenance": {
                "interface": "external_organizer",
                "client_id": "client-fixture",
                "authorization": {"scopes": ["formal"], "revision": "old"},
                "target_path": "03_知识/目标.md",
                "proposed_action": "create",
                "expected_target_hash": "missing",
                "conflict_status": "none",
                "uncertainties": [],
            },
        }

    def test_normal_pending_candidate_preview_returns_diff(self):
        candidate = self.candidate()
        preview = {"relative_target": "03_知识/目标.md", "diff": "+测试"}
        with patch.object(web, "candidate_source", return_value=candidate), patch.object(web, "_candidate_preview", return_value=preview):
            result = web.api_candidate_writer_preview.__wrapped__("mem_fixture", None)
        self.assertEqual(preview, result)

    def test_authorization_change_preview_is_actionable_403(self):
        candidate = self.candidate()
        error = web._candidate_authorization_error("authorization_revision_expired")
        with patch.object(web, "candidate_source", return_value=candidate), patch.object(web, "_candidate_preview", side_effect=error):
            with self.assertRaises(HTTPException) as raised:
                web.api_candidate_writer_preview.__wrapped__("mem_fixture", None)
        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual("authorization_revision_expired", raised.exception.detail["code"])
        self.assertTrue(raised.exception.detail["candidate_retained"])
        self.assertIn("不需要重新输入密码", raised.exception.detail["message"])

    def test_revoked_client_has_distinct_reason(self):
        candidate = self.candidate()
        registry = SimpleNamespace(list_clients=lambda: [{"internal_id": "client-fixture", "status": "revoked"}])
        previous_registry = getattr(web.app.state, "client_registry", None)
        previous_permissions = getattr(web.app.state, "client_permissions", None)
        web.app.state.client_registry = registry
        web.app.state.client_permissions = Mock()
        try:
            with self.assertRaises(web.CandidateAuthorizationError) as raised:
                web._validate_external_candidate_authorization(candidate)
            self.assertEqual("client_revoked", raised.exception.code)
        finally:
            web.app.state.client_registry = previous_registry
            web.app.state.client_permissions = previous_permissions

    def test_source_read_revocation_has_distinct_reason(self):
        candidate = self.candidate()
        registry = SimpleNamespace(list_clients=lambda: [{"internal_id": "client-fixture", "status": "authenticated"}])
        permissions = Mock()
        # Explicit denial still wins when ordinary storage has no encryption.
        permissions._ordinary_reads_are_open.return_value = True
        permissions._policy.return_value = web.PolicyDecision.DENY
        target = SimpleNamespace(scope=web.KnowledgeScope.FORMAL, selector={"path": "03_知识/来源.md"})
        previous_registry = getattr(web.app.state, "client_registry", None)
        previous_permissions = getattr(web.app.state, "client_permissions", None)
        web.app.state.client_registry = registry
        web.app.state.client_permissions = permissions
        try:
            with patch.object(web, "resolve_document_target", return_value=target), patch.object(web, "active_workspace", return_value=Mock()):
                with self.assertRaises(web.CandidateAuthorizationError) as raised:
                    web._validate_external_candidate_authorization(candidate)
            self.assertEqual("source_read_not_allowed", raised.exception.code)
            permissions._policy.assert_called_once()
        finally:
            web.app.state.client_registry = previous_registry
            web.app.state.client_permissions = previous_permissions

    def test_failed_pending_apply_does_not_accept_or_write(self):
        candidate = self.candidate()
        digest = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
        preview = {"relative_target": "03_知识/目标.md", "context": {"candidate_id": "mem_fixture", "candidate_digest": digest}}
        request = web.CandidateWriteApplyRequest(preview=preview, owner_confirmed=True, review_uncertainties=False)
        error = web._candidate_authorization_error("authorization_revision_expired")
        with patch.object(web, "candidate_source", return_value=candidate), \
             patch.object(web, "_validate_candidate_evidence", side_effect=error), \
             patch.object(web, "review_candidate") as review, \
             patch.object(web, "production_apply") as apply:
            with self.assertRaises(HTTPException) as raised:
                web.api_candidate_writer_apply.__wrapped__("mem_fixture", request, None)
        self.assertEqual(403, raised.exception.status_code)
        review.assert_not_called()
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
