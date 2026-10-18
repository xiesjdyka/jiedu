import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from knowledge_bridge import codex_format_repair as repair

class SummaryCacheTests(unittest.TestCase):
    def test_unchanged_reads_are_cached_and_all_inputs_invalidate(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);adapter=SimpleNamespace(context=SimpleNamespace(sync=root,audit=root),review_path=root/'review.jsonl')
            adapter.review_path.write_text(json.dumps({'source_session_id':'s','event_fingerprint':'a','reason':'unknown'})+'\n')
            first=repair.summary(adapter,['s']);self.assertEqual(first['unresolved_events'],1)
            with patch.object(repair,'rows',side_effect=AssertionError('unchanged ledger reread')):
                cached=repair.summary(adapter,['s']);cached['unresolved_by_session'].clear()
                self.assertEqual(repair.summary(adapter,['s'])['unresolved_by_session'],{'s':1})
            self.assertEqual(repair.summary(adapter,['other'])['unresolved_events'],0)
            repair.append_resolutions(adapter,[{'key':'s:a','policy':repair.FORMAT_POLICY,'classification':'internal'}])
            self.assertEqual(repair.summary(adapter,['s'])['unresolved_events'],0)
            with adapter.review_path.open('a') as f:f.write(json.dumps({'source_session_id':'s','event_fingerprint':'b','reason':'unknown'})+'\n')
            self.assertEqual(repair.summary(adapter,['s'])['unresolved_events'],1)
            repair.paths(adapter)[1].unlink()
            self.assertEqual(repair.summary(adapter,['s'])['unresolved_events'],2)
