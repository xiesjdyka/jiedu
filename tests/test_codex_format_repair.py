import json
from unittest import TestCase, mock

from knowledge_bridge.codex_event_formats import auxiliary_kind
from knowledge_bridge.codex_format_repair import paths, summary, review_fingerprints
from tests import test_codex_source_adapter as fixtures
from tests.test_codex_source_adapter import _message, _line


def legacy(mid='old01', text='旧格式的完整回答'):
    event = _message(mid, 'assistant', text)
    event['payload']['internal_chat_message_metadata_passthrough'] = {'turn_id': 'old-turn'}
    return event


class CodexFormatRepairTests(TestCase):
    setUp = fixtures.CodexSourceAdapterTests.setUp
    tearDown = fixtures.CodexSourceAdapterTests.tearDown
    _source = fixtures.CodexSourceAdapterTests._source
    _index = fixtures.CodexSourceAdapterTests._index
    _adapter = fixtures.CodexSourceAdapterTests._adapter
    _authorize = fixtures.CodexSourceAdapterTests._authorize

    def seed_old(self, events):
        self.source = self._source('main', events)
        self._index([('main', '旧对话')])
        adapter = self._adapter()
        self._authorize(adapter, 'main')
        normalize = adapter._normalize_event

        def old(event, sid):
            if event.get('payload', {}).get('role') == 'assistant' and event['payload'].get('phase') is None:
                return {'status': 'needs_review', 'reason': 'uncertain_assistant_phase', 'metadata': {'phase': None}}
            if event.get('type') == 'token_usage_record':
                return {'status': 'needs_review', 'reason': 'unknown_top_level_event', 'metadata': {'event_type': 'token_usage_record'}}
            return normalize(event, sid)

        with mock.patch.object(adapter, '_normalize_event', side_effect=old), mock.patch(
                'knowledge_bridge.codex_format_repair.tick', return_value={'saved_messages': 0, 'active': False}):
            adapter.sync()
        return adapter

    def finish(self, adapter):
        for _ in range(40):
            result = adapter.sync()
            info = summary(adapter, {'main'})
            if not info['pending_sources'] and not info['failed_sources']:
                return result, info
        self.fail('repair did not finish')

    def test_only_confirmed_legacy_public_envelope_is_accepted(self):
        adapter = self._adapter()
        self.assertEqual('message', adapter._normalize_event(legacy(), 'main')['status'])
        ordinary = _message('unknown', 'assistant', 'unknown phase')
        self.assertEqual('needs_review', adapter._normalize_event(ordinary, 'main')['status'])
        for change in ({'phase': 'analysis'}, {'channel': 'analysis'}, {'recipient': 'tools'},
                       {'content': [{'type': 'encrypted_content', 'encrypted_content': 'opaque'}]}):
            event = legacy(); event['payload'].update(change)
            self.assertEqual('needs_review', adapter._normalize_event(event, 'main')['status'])

    def test_auxiliary_types_do_not_become_chat_and_future_shapes_stay_unknown(self):
        samples = [
            {'type': 'token_usage_record', 'payload': {'usage': {'input_tokens': 10}}},
            {'type': 'inter_agent_communication_metadata', 'payload': {'trigger_turn': True}},
            {'type': 'realtime_item', 'payload': {'type': 'realtime_session_started', 'realtime_session_id': 'voice'}},
            {'type': 'response_item', 'payload': {'type': 'agent_message', 'author': '/root/a', 'recipient': '/root', 'content': [{'type': 'input_text', 'text': 'internal'}]}},
            {'type': 'response_item', 'payload': {'type': 'tool_search_call', 'call_id': 'call', 'arguments': {}}},
            {'type': 'response_item', 'payload': {'type': 'tool_search_output', 'call_id': 'call', 'tools': []}},
            {'type': 'response_item', 'payload': {'type': 'web_search_call', 'id': 'call', 'action': {}}},
        ]
        for event in samples:
            self.assertIsNotNone(auxiliary_kind(event))
            self.assertEqual('ignored', self._adapter()._normalize_event(event, 'main')['status'])
        self.assertIsNone(auxiliary_kind({'type': 'realtime_item', 'payload': {'type': 'transcript', 'text': '用户发言'}}))
        self.assertIsNone(auxiliary_kind({'type': 'token_usage_record', 'payload': {'content': 'unknown'}}))

    def test_public_question_card_and_only_its_known_shape_are_archived(self):
        event = {'type': 'event_msg', 'timestamp': '2026-07-01T00:00:00Z', 'payload': {
            'type': 'item_completed', 'thread_id': 'main', 'turn_id': 'turn', 'item': {
                'type': 'AgentMessage', 'id': 'call_question', 'phase': 'final_answer', 'delivery': 'async',
                'questions': [{'title': '选择哪种？', 'options': ['甲', '乙']}],
                'content': [{'type': 'Text', 'text': '选择哪种？\n- 甲\n- 乙'}]}}}
        adapter = self._adapter()
        result = adapter._normalize_event(event, 'main')
        self.assertEqual('codex-question:call_question', result['message'].message_id)
        self.assertEqual('选择哪种？\n- 甲\n- 乙', result['message'].content)
        event['payload']['item']['questions'][0]['options'] = None
        self.assertEqual('message', adapter._normalize_event(event, 'main')['status'])
        self.assertEqual('needs_review', adapter._normalize_event(event, 'another')['status'])
        event['payload']['item']['phase'] = 'analysis'
        self.assertEqual('needs_review', adapter._normalize_event(event, 'main')['status'])
        event['payload']['item'].pop('questions'); event['payload']['item'].pop('delivery')
        self.assertEqual('ignored', adapter._normalize_event(event, 'main')['status'])

    def test_ordinal_migration_requires_the_exact_old_fingerprint(self):
        from knowledge_bridge.codex_source_adapter import _fingerprint
        event = {'timestamp': 'now', 'type': 'inter_agent_communication_metadata', 'payload': {'trigger_turn': True}}
        original = _fingerprint(json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode())
        event['ordinal'] = 22
        self.assertIn(original, review_fingerprints(event, 'current'))
        event['payload']['trigger_turn'] = False
        self.assertNotIn(original, review_fingerprints(event, 'current'))

    def test_policy_upgrade_rechecks_without_readding_messages_or_rewinding_live_cursor(self):
        adapter = self.seed_old([legacy()]); self.finish(adapter)
        path = paths(adapter)[0]; audit = json.loads(path.read_text(encoding='utf-8'))
        audit['policy'] = 3
        from knowledge_bridge.durable_state import write_object
        write_object(path, audit, backup=True)
        before = adapter._state()['sessions']['main']['fragments']
        self.assertGreater(summary(adapter, {'main'})['pending_sources'], 0)
        result, info = self.finish(adapter)
        self.assertEqual(0, result['saved_messages']); self.assertEqual(1, info['repaired_messages'])
        self.assertEqual({k:v['confirmed_offset'] for k,v in before.items()},
                         {k:v['confirmed_offset'] for k,v in adapter._state()['sessions']['main']['fragments'].items()})
        self.assertEqual(3, json.loads(path.read_text(encoding='utf-8'))['previous_passes'][0]['policy'])

    def test_backfill_preserves_cursors_order_and_is_idempotent_after_restart(self):
        adapter = self.seed_old([_message('u01', 'user', '问题'), legacy(),
            {'type': 'token_usage_record', 'payload': {'usage': {}}},
            _message('u03', 'user', '追问')])
        before = adapter._state()['sessions']['main']['fragments']
        original_review = adapter.review_path.read_bytes()
        _, info = self.finish(adapter)
        self.assertEqual(0, info['unresolved_events'])
        self.assertEqual(1, info['repaired_messages'])
        self.assertEqual({'public_message': 1, 'usage': 1}, info['resolved_categories'])
        after = adapter._state()['sessions']['main']['fragments']
        self.assertEqual({k: v['confirmed_offset'] for k, v in before.items()},
                         {k: v['confirmed_offset'] for k, v in after.items()})
        self.assertEqual(original_review, adapter.review_path.read_bytes())
        item = next(iter(adapter.store._registry()['sessions'].values()))
        public = adapter.store._message_rows(self.workspace.root / item['relative_path'] / '03_元数据/messages.jsonl')
        self.assertEqual(['u01', 'old01', 'u03'], [r['message_id'] for r in public])
        for _ in range(3):
            self.assertEqual(0, self._adapter().sync(manual=True)['saved_messages'])
        self.assertEqual(3, len(adapter.store._message_rows(self.workspace.raw_message_store / (item['session_key'] + '.jsonl'))))

    def test_projection_failure_retries_without_losing_receipt_or_duplicating_body(self):
        adapter = self.seed_old([legacy()])
        with mock.patch.object(adapter.store, 'materialize_session', side_effect=OSError('synthetic disk failure')):
            first = adapter.sync()
        self.assertEqual('failed', first['format_repair']['status'])
        self.assertEqual(1, summary(adapter, {'main'})['unresolved_events'])
        _, info = self.finish(self._adapter())
        self.assertEqual(0, info['unresolved_events'])
        item = next(iter(adapter.store._registry()['sessions'].values()))
        self.assertEqual(1, len(adapter.store._message_rows(self.workspace.raw_message_store / (item['session_key'] + '.jsonl'))))

    def test_old_review_offset_is_relocated_by_exact_fingerprint(self):
        adapter = self.seed_old([legacy()])
        row = json.loads(adapter.review_path.read_text().strip()); row['source_offset'] = 999999
        adapter.review_path.write_bytes(_line(row))
        _, info = self.finish(adapter)
        self.assertEqual(0, info['unresolved_events'])
        resolution = json.loads(paths(adapter)[1].read_text().strip())
        self.assertNotEqual(999999, resolution['byte_offset'])

    def test_unknown_message_remains_visible_as_incomplete(self):
        adapter = self.seed_old([_message('u01', 'user', '问题'), _message('unknown', 'assistant', '不明消息')])
        _, info = self.finish(adapter)
        self.assertEqual(1, info['unresolved_events'])
        self.assertEqual([], info['verified_session_ids'])
        self.assertEqual('completed_with_issues', adapter.status()['history']['status'])

    def test_modified_frozen_source_is_not_accepted_as_complete(self):
        adapter = self.seed_old([legacy()])
        adapter.page_bytes = 10
        adapter.sync()
        original = self.source.read_bytes()
        self.source.write_bytes(original.replace('旧格式'.encode(), '新格式'.encode()))
        result = adapter.sync()
        self.assertEqual('failed', result['format_repair']['status'])
        self.assertNotIn('main', summary(adapter, {'main'})['verified_session_ids'])
