import json,tempfile,unittest
from pathlib import Path
from knowledge_bridge.codex_source_adapter import CodexSourceAdapter
from knowledge_bridge.archive_store import NormalizedMessage

class EvidenceCacheTests(unittest.TestCase):
    def test_append_repeat_external_append_truncate_and_replace(self):
        with tempfile.TemporaryDirectory() as t:
            adapter=object.__new__(CodexSourceAdapter);session={'archive_path':t};event={'timestamp':'2026-10-14'}
            def add(id):adapter._append_public_evidence(session,event,NormalizedMessage(message_id=id,role='user',content=id))
            add('one');add('one');path=Path(t)/'00_原始数据/codex-public-events.jsonl'
            self.assertEqual(len(path.read_text().splitlines()),1)
            with path.open('a') as f:f.write(json.dumps({'message_id':'external'})+'\n')
            add('external');self.assertEqual(len(path.read_text().splitlines()),2)
            add('two');self.assertEqual(len(path.read_text().splitlines()),3)
            path.write_text('');add('one');self.assertEqual(len(path.read_text().splitlines()),1)
            replacement=path.with_suffix('.replace');replacement.write_text('{"message_id":"other"}\n');replacement.replace(path)
            add('one');self.assertEqual(len(path.read_text().splitlines()),2)
    def test_partial_line_stops_without_appending(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'00_原始数据/codex-public-events.jsonl';p.parent.mkdir();p.write_text('{"message_id":')
            a=object.__new__(CodexSourceAdapter)
            with self.assertRaises(ValueError):a._append_public_evidence({'archive_path':t},{},NormalizedMessage(message_id='new',role='user',content='new'))
            self.assertEqual(p.read_text(),'{"message_id":')
