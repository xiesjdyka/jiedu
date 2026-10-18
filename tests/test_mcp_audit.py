import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import knowledge_bridge.mcp_audit as audit

class MCPAuditTests(unittest.TestCase):
    def test_success_rejection_and_parameter_summary(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(audit,"AUDIT_PATH",Path(tmp)/"audit.jsonl"):
            with audit.audited("hermes","search_memory",{"query":"x"*400}):pass
            with self.assertRaises(PermissionError):
                with audit.audited("hermes","memory_bootstrap",{}):raise PermissionError("closed")
            result=audit.client_summary("hermes")
            self.assertEqual(2,result["mcp_call_count"]);self.assertEqual("rejected",result["last_mcp_outcome"]);self.assertLessEqual(len(result["recent_mcp_calls"][0]["parameters"]["query"]),181)
