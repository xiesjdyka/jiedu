"""Synthetic checks for the stdlib, read-only history tool payload auditor.

No knowledge_bridge imports, real source databases, credentials, or model calls.
The test runner selects an isolated TEMP before loading this module.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'audit_tool_payload_history.py'


def load_auditor():
    spec = importlib.util.spec_from_file_location('history_tool_audit_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Read exactly the public source; do not probe or create adjacent bytecode caches.
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), 'exec'), module.__dict__)
    return module


class HistoryToolClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auditor = load_auditor()

    def row(self, **changes):
        return {'message_id': 'synthetic-message', 'session_id': 'synthetic-session',
                'role': 'assistant', 'original_text': 'synthetic visible answer',
                'provenance': {}, **changes}

    def assert_not_tool_hit(self, row):
        result = self.auditor.classify_record(row)
        self.assertIn(result['classification'], {'keep', 'audit_only'})
        return result

    def test_actual_tool_role_and_known_route_require_review(self):
        for row in (self.row(role='tool'),
                    self.row(provenance={'source_recipient': 'functions.exec'}),
                    self.row(provenance={'source_recipient': 'mcp__fixture__tool'})):
            with self.subTest(row=row):
                result = self.auditor.classify_record(row)
                self.assertEqual('needs_review', result['classification'])
                self.assertTrue(result['reasons'])

    def test_visible_thinking_analysis_commentary_and_explanations_are_retained(self):
        for channel in ('analysis', 'reasoning', 'commentary', 'final', 'final_answer'):
            with self.subTest(channel=channel):
                self.assert_not_tool_hit(self.row(provenance={
                    'source_channel': channel, 'assistant_phase': channel}))

    def test_code_think_discussion_and_user_pasted_commands_are_not_tool_events(self):
        body = ('Visible explanation of <think> tags.\n\n'
                '```python\nprint("analysis and tool_call are examples")\n```\n'
                '```sh\nadb devices\n```\n'
                '{"role":"tool","recipient":"functions.exec","output":"example"}')
        for role in ('user', 'assistant'):
            with self.subTest(role=role):
                self.assert_not_tool_hit(self.row(role=role, original_text=body))

    def test_tool_calls_with_visible_prose_and_unknown_display_kind_are_retained(self):
        self.assert_not_tool_hit(self.row(
            tool_calls=[{'name': 'example_only', 'arguments': '{}'}],
            reasoning_content='synthetic separate reasoning column',
            provenance={'source_display_kind': 'future_visible_panel'},
            original_text='Visible assistant answer and a code example.'))

    def test_unknown_routing_and_missing_metadata_do_not_authorize_exclusion(self):
        for provenance in ({}, {'source_channel': 'future_display'},
                           {'source_recipient': 'future_display'},
                           {'source_channel': ['analysis']},
                           {'source_recipient': {'name': 'functions.exec'}}):
            with self.subTest(provenance=provenance):
                self.assert_not_tool_hit(self.row(provenance=provenance))

    def test_legacy_hermes_supplement_web_and_doubao_are_not_inferred_from_names(self):
        rows = [self.row(source='Hermes', provenance={
                    'archive_kind': 'historical_supplement', 'source_db_active': 0}),
                self.row(source='ChatGPT Web'),
                self.row(source='Doubao', role='user')]
        for row in rows:
            with self.subTest(source=row['source']):
                self.assert_not_tool_hit(row)


class HistoryToolAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auditor = load_auditor()

    def setUp(self):
        # Keep these small fixtures for the calling acceptance run's evidence policy.
        self.root = Path(tempfile.mkdtemp(prefix='history-tool-audit-'))
        self.workspace = self.root / 'workspace'
        self.state = self.workspace / '.jiedu' / 'state'
        self.raw = self.state / 'raw-messages'
        self.raw.mkdir(parents=True)
        self.registry = self.state / 'archive-sessions.json'
        self.aliases = self.state / 'archive-session-aliases.json'
        self.write_json(self.registry, {'sessions': {}})
        self.write_json(self.aliases, {'aliases': {}, 'message_aliases': {}})
        self.output = self.root / 'report.json'

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')

    @staticmethod
    def line(row):
        return (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode()

    def row(self, mid='message', sid='synthetic-session', body='synthetic visible body', **changes):
        return {'message_id': mid, 'session_id': sid, 'role': 'assistant',
                'original_text': body, 'content_sha256': hashlib.sha256(body.encode()).hexdigest(),
                'provenance': {}, **changes}

    def raw_rows(self, rows, filename='synthetic.jsonl'):
        path = self.raw / filename
        path.write_bytes(b''.join(self.line(row) for row in rows))
        return path

    def register_canonical(self, rows, key='canonical-session'):
        relative = '90_原始档案/Fixture/Synthetic'
        self.write_json(self.registry, {'sessions': {key: {'relative_path': relative}}})
        path = self.workspace / relative / '03_元数据' / 'messages.jsonl'
        path.parent.mkdir(parents=True)
        path.write_bytes(b''.join(self.line(row) for row in rows))
        return path

    def input_snapshot(self):
        return {path.relative_to(self.workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.workspace.rglob('*') if path.is_file()}

    def test_audit_is_read_only_and_findings_do_not_copy_body_or_titles(self):
        secret_body = 'Synthetic original text must never be copied into an audit finding.'
        self.raw_rows([
            self.row('tool', body=secret_body, role='tool', title='Synthetic private title'),
            self.row('thinking', provenance={'source_channel': 'analysis'}),
            self.row('visible')])
        self.write_json(self.state / 'sync' / 'cursor.json', {'offset': 123, 'message_id': 'stable'})
        (self.workspace / '03_知识').mkdir()
        (self.workspace / '03_知识' / 'sentinel.md').write_text('synthetic formal knowledge', 'utf-8')
        before = self.input_snapshot()
        result = self.auditor.audit(self.workspace, self.output)
        self.assertEqual(before, self.input_snapshot())
        self.assertEqual('jiedu-tool-payload-history-audit-v1', result['schema'])
        self.assertTrue(result['read_only'])
        self.assertEqual([], result['proposed_changes'])
        self.assertEqual(0, result['production_changes'])
        self.assertFalse(result['source_binding_checked'])
        self.assertFalse(result['coverage']['source_db_checked'])
        self.assertEqual((3, 1, 1, 1), tuple(result['counts'][key] for key in
                         ('raw_records', 'needs_review', 'audit_only', 'kept')))
        self.assertEqual({'needs_review', 'audit_only'},
                         {item['classification'] for item in result['findings']})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret_body, serialized)
        self.assertNotIn('Synthetic private title', serialized)
        for finding in result['findings']:
            self.assertFalse(finding['source_binding_checked'])
            self.assertEqual('review_only_keep_unchanged', finding['action'])
            self.assertFalse({'original_text', 'content', 'quote', 'title'} & finding.keys())
        self.assertEqual(result, json.loads(self.output.read_text('utf-8')))

    def test_aliases_and_raw_canonical_views_are_not_counted_as_duplicate_archives(self):
        old = self.row('old-message', 'hermes:legacy')
        new = self.row('new-message', 'canonical-session')
        self.raw_rows([old], 'legacy.jsonl')
        new_path = self.raw_rows([new], 'new.jsonl')
        self.register_canonical([new])
        self.write_json(self.aliases, {'aliases': {'hermes:legacy': 'canonical-session'},
                                      'message_aliases': {'canonical-session': {'old-message': 'new-message'}}})
        result = self.auditor.audit(self.workspace, self.output)
        self.assertEqual((2, 2, 1, 1), tuple(result['counts'][key] for key in
                         ('raw_files', 'raw_records', 'canonical_files', 'canonical_records')))
        self.assertEqual(0, result['counts']['duplicate_ids'])
        self.assertEqual(1, result['counts']['alias_equivalent_rows'])
        with new_path.open('ab') as stream:
            stream.write(self.line(new))
        self.raw_rows([self.row('new-message', 'independent-session')], 'independent.jsonl')
        again = self.auditor.audit(self.workspace, self.output)
        self.assertEqual(1, again['counts']['duplicate_ids'])

    def test_fingerprint_and_old_finding_identity_are_stable_across_rerun_and_append(self):
        path = self.raw_rows([self.row('tool-before', role='tool')])
        original = path.read_bytes()
        first = self.auditor.audit(self.workspace, self.output)
        second = self.auditor.audit(self.workspace, self.root / 'another-report.json')
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.assertEqual(first['findings'], second['findings'])
        with path.open('ab') as stream:
            stream.write(self.line(self.row('new-user', role='user')))
        appended = path.read_bytes()
        third = self.auditor.audit(self.workspace, self.output)
        self.assertNotEqual(first['fingerprint'], third['fingerprint'])
        self.assertEqual(first['findings'], third['findings'])
        self.assertTrue(appended.startswith(original))
        self.assertEqual(appended, path.read_bytes())

    def test_safe_report_locations_work_and_protected_or_existing_files_are_not_overwritten(self):
        self.raw_rows([self.row()])
        for output in (self.output, self.workspace / '.jiedu/artifacts/audit.json',
                       self.workspace / '.jiedu/audit/audit.json'):
            with self.subTest(allowed=str(output)):
                first = self.auditor.audit(self.workspace, output)
                self.assertEqual(first, self.auditor.audit(self.workspace, output))
        before = self.input_snapshot()
        for relative in ('.jiedu/state/new.json', '.jiedu/state/sync/new.json',
                         '.jiedu/state/raw-messages/new.json', '00_系统/new.json',
                         '01_当前状态/new.json', '02_项目/new.json', '03_知识/new.json',
                         '90_原始档案/new.json', '99_收件箱/new.json'):
            with self.subTest(forbidden=relative), self.assertRaises(ValueError):
                self.auditor.audit(self.workspace, self.workspace / relative)
        with self.assertRaises(ValueError):
            self.auditor.audit(self.workspace, self.registry)
        valuable = self.root / 'valuable.json'
        valuable.write_text('{"owner":"synthetic valuable data"}', 'utf-8')
        original = valuable.read_bytes()
        with self.assertRaises(ValueError):
            self.auditor.audit(self.workspace, valuable)
        self.assertEqual(original, valuable.read_bytes())
        self.assertEqual(before, self.input_snapshot())

    def test_registered_path_escape_and_alias_cycles_are_rejected_without_changes(self):
        self.raw_rows([self.row(sid='old')])
        for relative in ('../outside', '90_原始档案/../03_知识', '03_知识/pretend-archive',
                         str(self.root / 'outside')):
            self.write_json(self.registry, {'sessions': {'unsafe': {'relative_path': relative}}})
            before = self.input_snapshot()
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.auditor.audit(self.workspace, self.output)
            self.assertEqual(before, self.input_snapshot())
            self.assertFalse(self.output.exists())
        self.write_json(self.registry, {'sessions': {}})
        self.write_json(self.aliases, {'aliases': {'old': 'new', 'new': 'old'}})
        before = self.input_snapshot()
        with self.assertRaises(ValueError):
            self.auditor.audit(self.workspace, self.output)
        self.assertEqual(before, self.input_snapshot())

    def test_real_link_or_junction_inputs_and_outputs_are_rejected(self):
        target = self.root / 'outside.json'
        target.write_text('synthetic outside sentinel', 'utf-8')
        source_link = self.raw / 'linked.jsonl'
        try:
            os.symlink(target, source_link)
        except (OSError, NotImplementedError) as error:
            if os.name != 'nt':
                self.skipTest(f'Host cannot create a synthetic link: {type(error).__name__}')
            # NTFS junctions do not require the symlink privilege. Both ends are private fixtures.
            import _winapi
            outside = self.root / 'outside-directory'
            outside.mkdir()
            (outside / 'sentinel.jsonl').write_text('synthetic outside directory sentinel', 'utf-8')
            clean = self.root / 'junction-input-workspace'
            raw_parent = clean / '.jiedu/state'
            raw_parent.mkdir(parents=True)
            _winapi.CreateJunction(str(outside), str(raw_parent / 'raw-messages'))
            with self.assertRaisesRegex(ValueError, 'symlink_or_junction'):
                self.auditor.audit(clean, self.output)
            output_parent = self.root / 'junction-output'
            _winapi.CreateJunction(str(outside), str(output_parent))
            with self.assertRaisesRegex(ValueError, 'symlink_or_junction'):
                self.auditor.audit(self.workspace, output_parent / 'report.json')
            self.assertEqual('synthetic outside directory sentinel',
                             (outside / 'sentinel.jsonl').read_text('utf-8'))
            self.assertFalse((outside / 'report.json').exists())
        else:
            with self.assertRaisesRegex(ValueError, 'symlink_or_junction'):
                self.auditor.audit(self.workspace, self.output)
            clean = self.root / 'clean-workspace'
            clean.mkdir()
            output_link = self.root / 'linked-report.json'
            os.symlink(target, output_link)
            with self.assertRaisesRegex(ValueError, 'symlink_or_junction'):
                self.auditor.audit(clean, output_link)
        self.assertFalse(self.output.exists())
        self.assertEqual('synthetic outside sentinel', target.read_text('utf-8'))

    def test_invalid_partial_oversized_and_hash_mismatch_inputs_are_reported(self):
        valid = self.row('hash-mismatch', content_sha256='0' * 64)
        path = self.raw / 'bounded.jsonl'
        path.write_bytes(self.line(valid) + b'{bad json}\n[]\n{}\n' +
                         b'x' * 2048 + b'\n' + b'{"partial":true}')
        self.write_json(self.registry, {'sessions': {'missing': {'relative_path': '90_原始档案/Missing'}}})
        before = self.input_snapshot()
        with patch.object(self.auditor, 'MAX_ROW_BYTES', 1024):
            result = self.auditor.audit(self.workspace, self.output)
        self.assertEqual((2, 1, 1, 1), tuple(result['counts'][key] for key in
                         ('bad_rows', 'partial_rows', 'oversized_rows', 'body_hash_mismatches')))
        kinds = {item['kind'] for item in result['gaps']}
        self.assertTrue({'message_id_missing', 'registered_canonical_jsonl_missing',
                         'source_binding_not_checked'} <= kinds)
        self.assertEqual([], result['proposed_changes'])
        self.assertEqual(before, self.input_snapshot())

    def test_cli_has_only_read_only_mode_and_reports_unsafe_output_failure(self):
        self.raw_rows([self.row()])
        arguments = ['--workspace', str(self.workspace), '--output', str(self.output)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, self.auditor.main(arguments))
        result = json.loads(stdout.getvalue())
        self.assertEqual([], result['proposed_changes'])
        self.assertEqual(0, result['production_changes'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            self.auditor.main(arguments + ['--apply'])
        self.assertEqual(2, failure.exception.code)
        before = self.input_snapshot()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(2, self.auditor.main([
                '--workspace', str(self.workspace), '--output', str(self.registry)]))
        self.assertIn('audit_failed:', stderr.getvalue())
        self.assertEqual(before, self.input_snapshot())


if __name__ == '__main__':
    unittest.main()
