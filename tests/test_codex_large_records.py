import hashlib
import json
import unittest
from tests import test_codex_source_adapter as fixtures
from knowledge_bridge.codex_large_records import feed, new_parser


class LargeCodexRecordsTests(unittest.TestCase):
    setUp = fixtures.CodexSourceAdapterTests.setUp
    tearDown = fixtures.CodexSourceAdapterTests.tearDown
    _source = fixtures.CodexSourceAdapterTests._source
    _index = fixtures.CodexSourceAdapterTests._index
    _adapter = fixtures.CodexSourceAdapterTests._adapter

    def adapter(self):
        adapter = self._adapter()
        adapter.max_event_bytes = 1024
        adapter.stream_page_bytes = 2048
        adapter.configure(policy='all', owner_confirmed=True)
        return adapter

    def event(self, **changes):
        return {'type':'compacted', 'payload':{'nested':[{'type':'message', 'role':'user',
                 'content':[{'type':'input_image','image_url':'data:image/png;base64,'+'A'*16000}]}]}, **changes}

    def row(self, adapter, sid='large'):
        return adapter.status()['current_cursors'][sid]

    def finish(self, adapter, ticks=30):
        for _ in range(ticks):
            adapter.sync(manual=True, batch_size=1)
            if adapter.status()['history']['status'] != 'importing': break

    def test_resume_large_internal_page_end_without_rewind_or_new_batch(self):
        path = self._source('large', [self.event()])
        adapter = self.adapter(); adapter.sync(batch_size=1)
        before = adapter.status()['history']['batch_id']
        position = self.row(adapter)['confirmed_offset']
        adapter.sync(batch_size=1)
        stream = self.row(adapter)['large_record']
        self.assertGreater(stream['scan_offset'], position)
        self.assertEqual(position, self.row(adapter)['confirmed_offset'])
        self.assertLess(len(json.dumps(stream)), 4096)
        restarted = self.adapter(); restarted.sync(batch_size=1)
        self.assertGreater(self.row(restarted)['large_record']['scan_offset'], stream['scan_offset'])
        self.finish(restarted)
        self.assertEqual(before, restarted.status()['history']['batch_id'])
        self.assertEqual(path.stat().st_size, self.row(restarted)['confirmed_offset'])
        self.assertNotIn('large_record', self.row(restarted))
        source = restarted.discover()['large'].fragments[0]
        cursor = next(c for k,c in restarted._state()['sessions']['large']['fragments'].items() if not k.endswith('::live'))
        self.assertTrue(restarted._can_resume(source, cursor))
        with path.open('ab') as handle: handle.write(fixtures._line(fixtures._message('m01','user','after snapshot')))
        restarted.sync(batch_size=1)
        self.assertEqual(1, self.row(restarted)['archived_message_count'])
        self.assertEqual(before, restarted.status()['history']['batch_id'])
        self.assertEqual(1, cursor['streamed_internal_event_count'])

    def test_public_large_text_and_inline_attachment_are_not_silently_skipped(self):
        for sid, event in [('text', fixtures._message('m01','user','x'*16000)),
                           ('attachment',fixtures._message('m02','user','valid body',attachments=[{'type':'input_image','image_url':'data:image/png;base64,'+'A'*16000}]))]:
            self._source(sid, [event])
        adapter = self.adapter()
        for _ in range(25): adapter.sync(manual=True)
        for sid in ('text','attachment'):
            cursor = self.row(adapter, sid)
            self.assertEqual('failed', cursor['current_status'])
            self.assertIn('原正文和附件未跳过', cursor['last_error'])
            self.assertEqual(0, cursor.get('archived_message_count', 0))
            self.assertEqual('completed_with_issues', adapter.status()['history']['status'])
        adapter.sync(manual=True)
        self.assertTrue(all(self.row(adapter,sid)['current_status']=='failed' for sid in ('text','attachment')))

    def test_internal_record_without_final_newline_waits_and_then_resumes(self):
        path = self._source('large', [])
        record = fixtures._line(self.event())
        with path.open('ab') as handle: handle.write(record[:-1])
        adapter = self.adapter()
        for _ in range(14): adapter.sync(manual=True)
        self.assertNotEqual(path.stat().st_size, self.row(adapter)['confirmed_offset'])
        with path.open('ab') as handle: handle.write(b'\n')
        for _ in range(3): adapter.sync(manual=True)
        self.assertEqual(path.stat().st_size, self.row(adapter)['confirmed_offset'])
        self.assertEqual('complete', self.row(adapter)['body_status'])

    def test_corruption_inside_large_internal_record_never_confirms_the_line(self):
        path = self._source('large', [])
        with path.open('ab') as handle: handle.write(fixtures._line(self.event()).replace(b'"payload":',b'"payload"!'))
        adapter = self.adapter(); self.finish(adapter)
        self.assertEqual('failed', self.row(adapter)['current_status'])
        self.assertIn('完整性验证', self.row(adapter)['last_error'])
        self.assertLess(self.row(adapter)['confirmed_offset'], path.stat().st_size)

    def test_root_type_not_nested_or_duplicate_type_controls_visibility(self):
        for raw in (b'{"type":"response_item","payload":{"type":"compacted","text":"'+b'x'*3000+b'"}}\n',
                    b'{"type":"compacted","payload":"'+b'x'*3000+b'","type":"response_item"}\n'):
            self._source('large', [])
            path = self.sessions/'rollout-large.jsonl'
            with path.open('ab') as handle: handle.write(raw)
            adapter = self.adapter()
            for _ in range(10): adapter.sync(manual=True, batch_size=1)
            self.assertEqual('failed', self.row(adapter)['current_status'])

    def test_checkpoint_guard_rejects_changed_bytes(self):
        path = self._source('large', [self.event()])
        adapter = self.adapter(); adapter.sync(batch_size=1); adapter.sync(batch_size=1)
        cursor = self.row(adapter); position = cursor['confirmed_offset']; guard = cursor['large_record']['guard_start']
        # Keep size and mtime: the persisted chunk guard still catches mutation.
        import os
        stat = path.stat()
        with path.open('r+b') as handle: handle.seek(guard+40); handle.write(b'B')
        os.utime(path, ns=(stat.st_atime_ns,stat.st_mtime_ns))
        adapter.sync(manual=True, batch_size=1)
        self.assertEqual(position, self.row(adapter)['confirmed_offset'])
        self.assertEqual('failed', self.row(adapter)['current_status'])

    def test_stream_parser_chunk_edges_unicode_escapes_numbers_and_late_type(self):
        value = {'payload':{'texts':['中文\\\"\n', True, None, -23.12e3, {'other':['a','b']}]}, 'type':'compacted'}
        raw = fixtures._line(value)
        for size in (1,2,3,7,64):
            parser = new_parser()
            for at in range(0,len(raw),size): feed(parser,raw[at:at+size],final=at+size>=len(raw))
            self.assertEqual('compacted',parser['event_type'])
            self.assertEqual(1,parser['type_count'])
        for raw in (b'{"type":"compacted",}', b'{"type":"compacted","x":[1,]}', b'{"type":"compacted"} {}'):
            with self.assertRaises(ValueError): feed(new_parser(),raw,final=True)

    def test_existing_failed_batch_recovers_without_reset_or_public_attachment_loss(self):
        from unittest import mock
        attachment = self.codex/'attachments/public.txt'; attachment.write_text('public attachment','utf-8')
        path = self._source('large', [fixtures._message('m01','user','before'), self.event(),
            fixtures._message('m02','user','after',attachments=[{'type':'input_file','path':'public.txt'}])])
        original = path.read_bytes()
        adapter = self.adapter(); adapter.sync(batch_size=1)
        frozen = adapter.status()['history']['batch_id']; confirmed = self.row(adapter)['confirmed_offset']
        with mock.patch.object(adapter,'_complete_region',side_effect=ValueError('单条日志超过 8 MiB 读取上限，尚未确认完整')):
            adapter.sync(manual=True,batch_size=1)
        self.assertEqual('failed',self.row(adapter)['current_status'])
        restarted = self.adapter(); self.finish(restarted)
        self.assertEqual(frozen,restarted.status()['history']['batch_id'])
        self.assertGreater(self.row(restarted)['confirmed_offset'],confirmed)
        self.assertEqual(2,self.row(restarted)['archived_message_count'])
        self.assertEqual(1,self.row(restarted)['attachment_count'])
        self.assertEqual('complete',self.row(restarted)['attachment_status'])
        self.assertEqual(original,path.read_bytes())

    def test_rewritten_earlier_type_is_not_trusted_from_checkpoint(self):
        import os
        path = self._source('large',[self.event()])
        adapter = self.adapter()
        for _ in range(3): adapter.sync(batch_size=1)
        cursor = self.row(adapter); start=cursor['large_record']['start']
        stat=path.stat()
        with path.open('r+b') as handle:
            handle.seek(start); prefix=handle.read(100)
            at=prefix.index(b'compacted')
            handle.seek(start+at);handle.write(b'futurexxx')
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        for _ in range(15): adapter.sync(manual=True,batch_size=1)
        self.assertEqual('failed',self.row(adapter)['current_status'])
        self.assertEqual(start,self.row(adapter)['confirmed_offset'])

    def test_failed_position_backfill_does_not_commit_started_flag(self):
        from unittest import mock
        path=self._source('large',[{'type':'event_msg','payload':{'type':'internal'}} for _ in range(4)]+[
            fixtures._message('m01','user','body')])
        adapter=self.adapter();adapter.sync()
        session=next(iter(adapter.store._registry()['sessions'].values()))
        from pathlib import Path
        ledger=adapter.context.root/session['relative_path']/'03_元数据/message-positions.jsonl';ledger.unlink()
        state=adapter._state();next(iter(state['history_batch']['items'].values()))['status']='pending'
        adapter._save_state(state);adapter.page_events=1
        with mock.patch.object(adapter.store,'materialize_session',side_effect=OSError('fixture materialize interruption')):
            adapter.sync(manual=True,batch_size=1)
        cursor=self.row(adapter)
        self.assertFalse(cursor.get('position_backfill_started',False))
        self.assertEqual(path.stat().st_size,cursor['confirmed_offset'])
        adapter.sync(manual=True,batch_size=1)
        self.assertTrue(self.row(adapter)['position_backfill_started'])
        self.assertLess(self.row(adapter)['confirmed_offset'],path.stat().st_size)
        self.finish(adapter)
        self.assertIn('m01',ledger.read_text('utf-8'))


if __name__ == '__main__': unittest.main()
