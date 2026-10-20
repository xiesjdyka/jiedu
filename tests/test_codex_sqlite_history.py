"""SQLite history is a read-only second source for the same Codex threads."""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.test_codex_source_adapter import CodexSourceAdapterTests, _message


def _ms(second):
    return int(datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc).timestamp() * 1000)


class CodexSqliteHistoryTests(unittest.TestCase):
    setUp = CodexSourceAdapterTests.setUp
    tearDown = CodexSourceAdapterTests.tearDown
    _source = CodexSourceAdapterTests._source
    _adapter = CodexSourceAdapterTests._adapter

    def _db(self, rows, *, source='user'):
        path = self.codex / 'thread_history_1.sqlite'
        db = sqlite3.connect(path)
        try:
            db.execute('create table thread_items(thread_id text,turn_id text,item_id text,rollout_ordinal integer,created_at_ms integer,item_json text,item_type text)')
            for index, entry in enumerate(rows, 1):
                sid, item, second = entry[:3]
                turn_id = entry[3] if len(entry) > 3 else 'turn-' + str(index)
                db.execute('insert into thread_items values(?,?,?,?,?,?,?)',
                           (sid, turn_id, item['id'], index, _ms(second),
                            json.dumps(item), item['type']))
            db.commit()
        finally:
            db.close()
        db = sqlite3.connect(self.codex / 'state_5.sqlite')
        try:
            db.execute('create table threads(id text,thread_source text,title text)')
            for sid in {row[0] for row in rows}:
                db.execute('insert into threads values(?,?,?)', (sid, source, 'SQLite 会话'))
            db.commit()
        finally:
            db.close()
        return path

    def _rows(self, adapter):
        return [json.loads(line) for path in adapter.context.raw_message_store.glob('*.jsonl')
                for line in path.read_text('utf-8').splitlines()]

    def test_sqlite_only_visible_text_and_user_image_are_archived(self):
        image = self.codex / 'attachments' / 'photo.png'; image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self._db([
            ('thread-a', {'type': 'userMessage', 'id': 'u1', 'content': [
                {'type': 'text', 'text': 'question'}, {'type': 'localImage', 'path': str(image)}]}, 1),
            ('thread-a', {'type': 'agentMessage', 'id': 'a1', 'text': 'answer', 'phase': 'final_answer'}, 2),
            ('thread-a', {'type': 'commandExecution', 'id': 'tool1', 'command': 'internal'}, 3),
        ])
        adapter = self._adapter(); adapter.enable_for_first_pairing()
        result = adapter.sync()
        self.assertEqual(2, result['sqlite']['saved_messages'])
        self.assertEqual({'u1', 'a1'}, {row['message_id'] for row in self._rows(adapter)})
        self.assertEqual(2, result['sqlite']['sources'][0]['history']['checked'])
        self.assertEqual('records', result['sqlite']['sources'][0]['history']['unit'])
        session = next(iter(adapter.store._registry()['sessions'].values()))
        attachment = adapter.store.attachment_status(session['session_key'])[0]
        self.assertEqual('u1', attachment['source_message_id'])
        self.assertFalse(attachment['missing'])
        self.assertEqual(0, adapter.sync()['sqlite']['saved_messages'])

    def test_jsonl_overlap_dedup_and_real_repeated_message_survives(self):
        self._source('thread-b', [_message('same-id', 'user', 'same words')])
        self._db([
            ('thread-b', {'type': 'userMessage', 'id': 'same-id', 'content': [
                {'type': 'text', 'text': 'same words'}]}, 1),
            ('thread-b', {'type': 'userMessage', 'id': 'later-id', 'content': [
                {'type': 'text', 'text': 'same words'}]}, 40),
        ])
        adapter = self._adapter(); adapter.enable_for_first_pairing()
        for _ in range(3):
            adapter.sync()
        self.assertEqual({'same-id', 'later-id'}, {row['message_id'] for row in self._rows(adapter)})
        self.assertEqual(2, len(self._rows(adapter)))

    def test_explicit_pause_preserves_sqlite_cursor(self):
        self._db([('thread-c', {'type': 'userMessage', 'id': 'u1', 'content': [
            {'type': 'text', 'text': 'first'}]}, 1)])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        state_path = adapter.context.sync / 'codex-sqlite-adapter.json'
        before = state_path.read_bytes()
        adapter.configure(enabled=False, owner_confirmed=True)
        self.assertEqual('paused', adapter.sync()['status'])
        self.assertEqual(before, state_path.read_bytes())

    def test_short_interval_repeated_user_text_is_not_collapsed(self):
        self._source('thread-d', [_message('jsonl-id', 'user', 'repeat')])
        self._db([
            ('thread-d', {'type': 'userMessage', 'id': 'sql-1', 'content': [
                {'type': 'text', 'text': 'repeat'}]}, 1),
            ('thread-d', {'type': 'userMessage', 'id': 'sql-2', 'content': [
                {'type': 'text', 'text': 'repeat'}]}, 2),
        ])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        self.assertEqual({'jsonl-id', 'sql-1', 'sql-2'}, {row['message_id'] for row in self._rows(adapter)})

    def test_assistant_fallback_requires_same_turn(self):
        self._source('thread-f', [
            {'type': 'turn_context', 'payload': {'turn_id': 'turn-shared'}},
            _message('jsonl-answer', 'assistant', 'answer', phase='final_answer'),
        ])
        self._db([('thread-f', {'type': 'agentMessage', 'id': 'sql-answer',
                               'text': 'answer', 'phase': 'final_answer'}, 1, 'turn-shared')])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        self.assertEqual({'jsonl-answer'}, {row['message_id'] for row in self._rows(adapter)})

    def test_new_sqlite_rows_use_live_cursor_during_history_backfill(self):
        path = self._db([('thread-e', {'type': 'userMessage', 'id': 'old', 'content': [
            {'type': 'text', 'text': 'old'}]}, 1)])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        db = sqlite3.connect(path)
        try:
            db.execute('insert into thread_items values(?,?,?,?,?,?,?)',
                       ('thread-e', 'turn-new', 'new', 2, _ms(3), json.dumps({
                           'type': 'userMessage', 'id': 'new', 'content': [
                               {'type': 'text', 'text': 'new'}]}), 'userMessage'))
            db.commit()
        finally:
            db.close()
        adapter.sync()
        self.assertEqual({'old', 'new'}, {row['message_id'] for row in self._rows(adapter)})
        self.assertEqual(1, adapter.status()['sqlite']['sources'][0]['live']['archived'])

    def test_updated_item_is_revisited_and_latest_projection_wins(self):
        path = self._db([('thread-g', {'type': 'agentMessage', 'id': 'a1',
                                      'text': 'draft', 'phase': 'commentary'}, 1)])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        db = sqlite3.connect(path)
        try:
            db.execute('update thread_items set item_json=? where item_id=?',
                       (json.dumps({'type': 'agentMessage', 'id': 'a1',
                                    'text': 'final text', 'phase': 'final_answer'}), 'a1'))
            db.commit()
        finally:
            db.close()
        adapter.sync()
        raw = self._rows(adapter)
        self.assertEqual(2, len(raw))
        self.assertEqual('a1', raw[1]['provenance']['supersedes_message_id'])
        session = next(iter(adapter.store._registry()['sessions'].values()))
        projected = adapter.context.root / session['relative_path'] / '03_元数据' / 'messages.jsonl'
        self.assertEqual(1, len(projected.read_text('utf-8').splitlines()))
        self.assertIn('final text', projected.read_text('utf-8'))

    def test_rebuilt_db_with_lower_rowid_restarts_without_erasing_archive(self):
        path = self._db([
            ('thread-h', {'type': 'userMessage', 'id': 'old1', 'content': [
                {'type': 'text', 'text': 'old one'}]}, 1),
            ('thread-h', {'type': 'userMessage', 'id': 'old2', 'content': [
                {'type': 'text', 'text': 'old two'}]}, 2),
        ])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        path.unlink()
        db = sqlite3.connect(path)
        try:
            db.execute('create table thread_items(thread_id text,turn_id text,item_id text,rollout_ordinal integer,created_at_ms integer,item_json text,item_type text)')
            db.execute('insert into thread_items values(?,?,?,?,?,?,?)',
                       ('thread-h', 'turn-new', 'new1', 1, _ms(3), json.dumps({
                           'type': 'userMessage', 'id': 'new1', 'content': [
                               {'type': 'text', 'text': 'new one'}]}), 'userMessage'))
            db.commit()
        finally:
            db.close()
        adapter.sync()
        self.assertEqual({'old1', 'old2', 'new1'}, {row['message_id'] for row in self._rows(adapter)})
        self.assertEqual(2, adapter.status()['sqlite']['sources'][0]['generation'])

    def test_unknown_user_part_keeps_known_text_and_records_review(self):
        self._db([('thread-i', {'type': 'userMessage', 'id': 'u1', 'content': [
            {'type': 'text', 'text': 'keep me'}, {'type': 'futureAttachment', 'opaque': 'x'}]}, 1)])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        self.assertEqual('keep me', self._rows(adapter)[0]['original_text'])
        self.assertIn('futureAttachment', self._rows(adapter)[0]['provenance']['unknown_content_types'])
        state = json.loads((adapter.context.sync / 'codex-sqlite-adapter.json').read_text('utf-8'))
        source = state['sources']['thread_history_1.sqlite']
        self.assertEqual('u1', source['review_items'][0]['item_id'])

    def test_cross_source_image_keeps_two_references_one_markdown_card(self):
        image = self.codex / 'attachments' / 'shared.png'; image.write_bytes(b'\x89PNG\r\n\x1a\nshared')
        self._source('thread-j', [_message('u1', 'user', 'look', attachments=[
            {'type': 'input_image', 'id': 'jsonl-image', 'local_path': str(image)}])])
        self._db([('thread-j', {'type': 'userMessage', 'id': 'u1', 'content': [
            {'type': 'text', 'text': 'look'}, {'type': 'localImage', 'path': str(image)}]}, 1)])
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync()
        session = next(iter(adapter.store._registry()['sessions'].values()))
        refs = adapter.store.attachment_status(session['session_key'])
        self.assertEqual(2, len(refs))
        self.assertEqual(1, len({x['sha256'] for x in refs}))
        card = (adapter.context.root / session['relative_path'] / '01_对话' / 'conversation.md').read_text('utf-8')
        self.assertEqual(1, card.count('- 附件：shared.png'))

    def test_unknown_thread_source_is_reviewed_without_being_archived(self):
        self._db([('thread-unknown', {'type': 'userMessage', 'id': 'u1', 'content': [
            {'type': 'text', 'text': 'private text'}]}, 1)], source='unrecognized_kind')
        adapter = self._adapter(); adapter.enable_for_first_pairing(); adapter.sync(); adapter.sync()
        self.assertEqual([], self._rows(adapter))
        source = adapter.status()['sqlite']['sources'][0]
        self.assertEqual('completed_with_review', source['history']['status'])
        self.assertEqual(1, source['history']['review'])

        db = sqlite3.connect(self.codex / 'state_5.sqlite')
        try:
            db.execute("update threads set thread_source='user' where id='thread-unknown'")
            db.commit()
        finally:
            db.close()
        adapter.sync()
        resolved = adapter.status()['sqlite']['sources'][0]
        self.assertEqual('complete', resolved['history']['status'])
        self.assertEqual(0, resolved['needs_review_items'])
        self.assertEqual({'u1'}, {row['message_id'] for row in self._rows(adapter)})

    def test_unsupported_schema_is_reported_instead_of_silent_skip(self):
        path = self.codex / 'thread_history_1.sqlite'
        db = sqlite3.connect(path)
        try:
            db.execute('create table thread_items(thread_id text,item_id text,item_json text,item_type text,created_at_ms integer)')
            db.commit()
        finally:
            db.close()
        adapter = self._adapter(); adapter.enable_for_first_pairing()
        result = adapter.sync()
        self.assertEqual('unsupported_thread_items_schema', result['sqlite']['issues'][0]['reason'])
        self.assertIn('turn_id', result['sqlite']['issues'][0]['missing_columns'])
