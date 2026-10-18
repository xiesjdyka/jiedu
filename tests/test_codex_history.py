"""First-install history imports must remain honest, resumable and fair."""
import json
import unittest
from unittest import mock

from tests.test_codex_source_adapter import CodexSourceAdapterTests, _line, _message, _meta


class CodexHistoryTests(unittest.TestCase):
    setUp = CodexSourceAdapterTests.setUp
    tearDown = CodexSourceAdapterTests.tearDown
    _source = CodexSourceAdapterTests._source
    _index = CodexSourceAdapterTests._index
    _adapter = CodexSourceAdapterTests._adapter

    def enable(self, adapter):
        adapter.configure(policy='all', owner_confirmed=True)

    def rows(self, adapter):
        return [json.loads(line) for path in adapter.context.raw_message_store.glob('*.jsonl')
                for line in path.read_text('utf-8').splitlines()]

    def test_fixed_batch_tail_fairness_restart_pause_and_no_duplicates(self):
        source = self._source('large', [_message(f'm{i:02}', 'user', f'history {i}') for i in range(1, 11)])
        self._index([('large', '历史')])
        adapter = self._adapter(); adapter.page_events = 2; self.enable(adapter)
        adapter.sync(batch_size=1)
        before = adapter.status()['history']
        self.assertEqual('importing', before['status'])
        with source.open('ab') as handle:
            handle.write(_line(_message('m11', 'user', 'new live message')))
        restarted = self._adapter(); restarted.page_events = 2
        restarted.sync(batch_size=1)
        self.assertIn('m11', [r['message_id'] for r in self.rows(restarted)])
        self.assertEqual(before['batch_id'], restarted.status()['history']['batch_id'])
        self.assertEqual(before['total_bytes'], restarted.status()['history']['total_bytes'])
        restarted.configure(enabled=False, owner_confirmed=True)
        saved = restarted.state_path.read_bytes()
        self.assertEqual('paused', restarted.sync()['status'])
        self.assertEqual(saved, restarted.state_path.read_bytes())
        restarted.configure(enabled=True, owner_confirmed=True)
        for _ in range(10):
            restarted.sync(batch_size=1)
        self.assertEqual('completed', restarted.status()['history']['status'])
        self.assertEqual(11, len(self.rows(restarted)))
        self.assertEqual(1, restarted.status()['live']['saved_messages'])
        self.assertEqual(1, restarted.status()['caught_up_session_count'])

    def test_all_fragments_overlap_dedup_chronological_order_and_archive_move(self):
        old = self._source('multi', [_message('m01', 'user', 'old' + 'x'*5000), _message('m02', 'user', 'middle')], 'rollout-old.jsonl')
        archive = self.codex / 'archived_sessions'; archive.mkdir()
        new = archive / 'rollout-new.jsonl'
        new.write_bytes(_line(_meta('multi')) + _line(_message('m02', 'user', 'middle')) + _line(_message('m03', 'user', 'new')))
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        self.assertEqual(2, adapter.status()['discovered_fragment_count'])
        self.assertEqual(3, len(self.rows(adapter)))
        session = next(iter(adapter.store._registry()['sessions'].values()))
        path = adapter.context.root / session['relative_path']
        portable = [json.loads(line) for line in (path/'03_元数据/messages.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(['m01', 'm02', 'm03'], [r['message_id'] for r in portable])
        self.assertTrue(all(r['source_position']['source_fragment'] for r in portable))
        moved = archive / old.name; old.rename(moved)
        with moved.open('ab') as handle: handle.write(_line(_message('m04', 'user', 'after move')))
        restarted = self._adapter(); restarted.sync()
        self.assertEqual(4, len(self.rows(restarted)))
        self.assertEqual(2, restarted.status()['history']['fragment_count'])

    def test_unknown_and_invalid_events_do_not_claim_complete(self):
        path = self._source('unknown', [_message('m01', 'user', 'safe'), {'type':'future-shape'}, ['array']])
        with path.open('ab') as handle: handle.write(b'not-json\n')
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        status = adapter.status()
        self.assertEqual('completed_with_issues', status['history']['status'])
        self.assertEqual(0, status['history']['completed_sessions'])
        self.assertEqual('needs_review', status['current_cursors']['unknown']['body_status'])
        self.assertEqual(3, status['needs_review_event_count'])
        self.assertEqual(0, status['caught_up_session_count'])
        self.assertEqual('needs_review', next(iter(adapter.store._registry()['sessions'].values()))['sync_status'])
        adapter.sync(manual=True)
        self.assertEqual(3, adapter.status()['needs_review_event_count'])
        self.assertEqual(1, len(self.rows(adapter)))

    def test_missing_and_failed_sources_do_not_starve_old_history(self):
        for i in range(6):
            self._source(f's{i}', [_message('m01', 'user', f'body {i}', attachments=[{'type':'input_file','path':f'missing{i}.txt'}])])
        adapter = self._adapter(); self.enable(adapter)
        original = adapter._sync_one
        def fail_one(state, source):
            if source.session_id == 's5': raise OSError('fixture read error')
            return original(state, source)
        with mock.patch.object(adapter, '_sync_one', side_effect=fail_one):
            for _ in range(12): adapter.sync(batch_size=1)
        self.assertEqual(5, len(self.rows(adapter)))
        self.assertEqual(5, adapter.status()['history']['completed_fragments'])
        self.assertEqual(1, adapter.status()['history']['counts']['failed'])
        self.assertEqual(5, adapter.status()['missing_attachment_count'])
        adapter.sync(manual=True, batch_size=8)
        self.assertEqual(6, len(self.rows(adapter)))
        self.assertEqual('completed_with_issues', adapter.status()['history']['status'])
        self.assertEqual(6, adapter.status()['missing_attachment_count'])

    def test_attachment_failure_reports_saved_body_and_retries(self):
        attachment = self.codex/'attachments/exists.txt'; attachment.write_text('attachment', 'utf-8')
        self._source('attach', [_message('m01', 'user', 'body retained', attachments=[{'type':'input_file','path':'exists.txt'}])])
        adapter = self._adapter(); self.enable(adapter)
        with mock.patch.object(adapter.store, '_archive_normalized_attachment', side_effect=OSError('fixture copy error')):
            adapter.sync()
        status = adapter.status(); cursor = status['current_cursors']['attach']
        self.assertEqual(1, cursor['archived_message_count'])
        self.assertEqual('failed', cursor['attachment_status'])
        self.assertEqual('body retained', self.rows(adapter)[0]['original_text'])
        adapter.sync(manual=True)
        self.assertEqual('complete', adapter.status()['current_cursors']['attach']['attachment_status'])
        self.assertEqual(1, len(self.rows(adapter)))

    def test_event_page_limit_and_partial_boundary_are_truthful(self):
        path = self._source('partial', [_message('m01', 'user', 'first')])
        unfinished = _line(_message('m02', 'user', 'second'))
        with path.open('ab') as handle: handle.write(unfinished[:30])
        adapter = self._adapter(); adapter.page_events = 1; self.enable(adapter)
        adapter.sync(batch_size=1)
        history = adapter.status()['history']
        self.assertEqual(30, history['deferred_partial_bytes'])
        self.assertLess(history['processed_bytes'], history['total_bytes'])
        with path.open('ab') as handle: handle.write(unfinished[30:])
        for _ in range(6): adapter.sync(batch_size=1)
        self.assertEqual(2, len(self.rows(adapter)))
        self.assertEqual('completed', adapter.status()['history']['status'])

    def test_oversized_event_is_failure_and_exclusions_survive_all_mode(self):
        self._source('large', [_message('m01', 'user', 'a'*3000)])
        self._source('excluded', [_message('m01', 'user', 'do not import')])
        adapter = self._adapter(); adapter.max_event_bytes = 1024
        adapter.configure(policy='all', exclude=['excluded'], owner_confirmed=True)
        for _ in range(3): adapter.sync(manual=True)
        self.assertEqual('completed_with_issues', adapter.status()['history']['status'])
        self.assertEqual(0, len(self.rows(adapter)))
        self.assertEqual(['excluded'], adapter.status()['excluded_session_ids'])

    def test_new_sessions_do_not_expand_first_batch(self):
        self._source('first', [_message('m01', 'user', 'first')])
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        history = adapter.status()['history']
        self._source('new', [_message('m01', 'user', 'new')]); adapter.sync()
        self.assertEqual(history['batch_id'], adapter.status()['history']['batch_id'])
        self.assertEqual(1, adapter.status()['history']['session_count'])
        self.assertEqual(2, len(self.rows(adapter)))
        self.assertEqual(1, adapter.status()['live']['saved_messages'])

    def test_discovery_errors_are_visible_but_subagents_are_not_errors(self):
        self._source('good', [_message('m01', 'user', 'good')])
        (self.sessions/'rollout-invalid.jsonl').write_bytes(b'not-json\n')
        (self.sessions/'rollout-unknown.jsonl').write_bytes(_line({'type':'future-header'}))
        (self.sessions/'rollout-partial.jsonl').write_bytes(b'{"type":')
        child = _meta('child'); child['payload']['source'] = {'subagent':{'thread_spawn':{}}}
        (self.sessions/'rollout-child.jsonl').write_bytes(_line(child))
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        self.assertEqual(3, len(adapter.status()['discovery_issues']))
        self.assertEqual(1, adapter.status()['discovered_session_count'])
        self.assertEqual('completed_with_issues', adapter.status()['history']['status'])

    def test_legacy_visibility_repair_cannot_delete_other_fragment_messages(self):
        self._source('multi', [_message('m01', 'user', 'first')], 'rollout-one.jsonl')
        self._source('multi', [_message('m02', 'user', 'second')], 'rollout-two.jsonl')
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        state = adapter._state(); state['sessions']['multi']['visibility_policy_version'] = 1
        adapter._save_state(state); adapter.sync(manual=True)
        self.assertEqual({'m01', 'm02'}, {r['message_id'] for r in self.rows(adapter)})
        self.assertEqual('failed', adapter.status()['current_cursors']['multi']['current_status'])

    def test_legacy_synced_cursor_does_not_hide_unknown_events(self):
        self._source('legacy', [_message('m01','user','body'), {'type':'future-event'}])
        adapter = self._adapter(); self.enable(adapter); adapter.sync()
        state = adapter._state(); cursor = state['sessions']['legacy']
        cursor.pop('fragments'); cursor.pop('unknown_event_count', None)
        cursor.update(current_status='synced', body_status='complete')
        state['history_batch'] = None
        adapter._save_state(state)
        adapter.sync()
        self.assertEqual('needs_review', adapter.status()['current_cursors']['legacy']['body_status'])
        self.assertEqual('completed_with_issues', adapter.status()['history']['status'])

    def test_replaced_source_between_discovery_and_read_is_not_misattributed(self):
        path = self._source('original', [_message('m01','user','original')])
        adapter = self._adapter(); self.enable(adapter)
        original_read = adapter._complete_region
        def replace_then_read(*args, **kwargs):
            path.write_bytes(_line(_meta('other')) + _line(_message('m02','user','other private body')))
            return original_read(*args, **kwargs)
        with mock.patch.object(adapter, '_complete_region', side_effect=replace_then_read): adapter.sync()
        self.assertEqual([], self.rows(adapter))
        self.assertEqual('failed', adapter.status()['current_cursors']['original']['current_status'])

    def test_truncation_after_freeze_does_not_shrink_fixed_batch(self):
        from knowledge_bridge import codex_history
        path = self._source('fixed', [_message('m01','user','original body')])
        adapter = self._adapter(); self.enable(adapter)
        frozen_size = path.stat().st_size
        original = codex_history.boundary
        def truncate(source_adapter, source, item):
            path.write_bytes(_line(_meta('fixed')))
            return original(source_adapter, source, item)
        with mock.patch.object(codex_history, 'boundary', side_effect=truncate): adapter.sync()
        history = adapter.status()['history']
        self.assertEqual(frozen_size, history['total_bytes'])
        self.assertEqual('completed_with_issues', history['status'])
        self.assertEqual(0, history['completed_fragments'])


if __name__ == '__main__': unittest.main()
