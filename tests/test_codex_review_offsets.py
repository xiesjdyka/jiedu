import unittest,tempfile,json
from pathlib import Path
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.codex_source_adapter import CodexSourceAdapter

class CodexReviewOffsetTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.context=initialize_workspace(self.root/'workspace');self.adapter=self.fresh()
 def fresh(self):return CodexSourceAdapter(self.context,codex_home=self.root/'codex',machine_data=self.root/'machine')
 def rows(self):return [json.loads(line) for line in self.adapter.review_path.read_text('utf-8').splitlines() if line.strip()]
 def test_source_offsets_survive_sequential_events_restart_and_ledger_increment(self):
  self.adapter._review('source-one','fingerprint-1','synthetic_unknown',12345,{'fixture':1});self.adapter._review('source-one','fingerprint-2','synthetic_unknown',987654,{'fixture':2});self.assertEqual([12345,987654],[r['source_offset'] for r in self.rows()])
  restarted=self.fresh();restarted._review('source-one','fingerprint-2','synthetic_unknown',987654,{});self.assertEqual(2,len(self.rows()));restarted._review('source-one','fingerprint-3','synthetic_unknown',3456789,{})
  self.adapter._review('source-one','fingerprint-3','synthetic_unknown',3456789,{});self.adapter._review('source-one','fingerprint-4','synthetic_unknown',9999999,{})
  self.assertEqual([12345,987654,3456789,9999999],[r['source_offset'] for r in self.rows()]);self.assertEqual(4,len({r['event_fingerprint'] for r in self.rows()}))
 def test_missing_ledger_resets_cache_and_recreated_event_is_recorded(self):
  self.adapter._review('source-one','same-fingerprint','synthetic_unknown',12345,{});self.adapter.review_path.unlink();self.adapter._review('source-one','same-fingerprint','synthetic_unknown',12345,{})
  self.assertEqual([12345],[r['source_offset'] for r in self.rows()]);self.adapter._review('source-one','same-fingerprint','synthetic_unknown',12345,{});self.assertEqual(1,len(self.rows()))
