import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.archive_store import ArchiveStore, NormalizedAttachment
from knowledge_bridge.attachment_repair import canonical
from knowledge_bridge.attachment_reference_review import apply_review, read_reviews
from knowledge_bridge.attachment_reference_stats import summarize
from knowledge_bridge.workspace import initialize_workspace


class ReferenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = initialize_workspace(self.root / 'workspace')
        self.store = ArchiveStore(self.context)
        self.session = self.store.ensure_session(source_key='codex', source_session_id='s', title='Fixture')
        self.key = self.session['session_key']
        self.store.append_message(self.key, message_id='m', role='assistant', content='Historical file')
        self.old = self.root / 'old.txt'
        self.spec = NormalizedAttachment('delivery:fixture', 'old.txt', self.old)
        self.store.reconcile_attachments(self.key, 'm', [self.spec])
        self.raw = self.context.raw_message_store / (self.key + '.jsonl')
        self.raw_before = self.raw.read_bytes()
        self.ledger = self.context.root / self.session['relative_path'] / '03_元数据/attachments.jsonl'
        self.prefix = self.ledger.read_bytes()
        self.candidate = self.root / 'verified.txt'
        self.candidate.write_bytes(b'historical version')
        self.sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.plan = [{'session_key': self.key, 'source_session_id': 's', 'message_id': 'm',
                     'source_reference': self.spec.reference, 'source_path': str(self.old),
                     'message_canonical_sha256': canonical(self.store._message_rows(self.raw)[0]),
                     'original_name': 'old.txt', 'decision': 'recover', 'candidate': str(self.candidate),
                     'expected_sha256': self.sha, 'proof': {'type': 'fixture historical manifest'},
                     'reason': 'verified', 'reason_detail': 'fixture reason'}]

    def test_recovery_is_append_only_idempotent_and_resists_path_reuse(self):
        self.assertEqual(1, apply_review(self.context, self.plan)['recoverable'])
        self.assertEqual(self.prefix, self.ledger.read_bytes())
        result = apply_review(self.context, self.plan, apply=True)
        self.assertEqual(1, result['recovered'])
        ledger = self.ledger.read_bytes()
        audit = (self.context.audit / 'attachment-reference-review.jsonl').read_bytes()
        apply_review(self.context, self.plan, apply=True)
        self.assertEqual(ledger, self.ledger.read_bytes())
        self.assertEqual(audit, (self.context.audit / 'attachment-reference-review.jsonl').read_bytes())
        # An old missing path or a new unrelated file must never downgrade/replace the archived bytes.
        self.store.reconcile_attachments(self.key, 'm', [self.spec])
        self.old.write_bytes(b'unrelated later same-name file')
        self.store.reconcile_attachments(self.key, 'm', [self.spec])
        self.assertEqual(ledger, self.ledger.read_bytes())
        self.assertTrue(ledger.startswith(self.prefix))
        self.assertEqual(self.raw_before, self.raw.read_bytes())
        row = self.store.attachment_status(self.key)[0]
        self.assertEqual(self.sha, hashlib.sha256((self.context.root / row['stored_path']).read_bytes()).hexdigest())

    def test_wrong_hash_rejected_before_any_write(self):
        self.candidate.write_bytes(b'same name, wrong version')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            apply_review(self.context, self.plan, apply=True)
        self.assertEqual(self.prefix, self.ledger.read_bytes())
        self.assertFalse((self.context.audit / 'attachment-reference-review.jsonl').exists())

    def test_message_identity_and_duplicates_rejected(self):
        with self.assertRaisesRegex(ValueError, '重复'):
            apply_review(self.context, self.plan * 2, apply=True)
        self.plan[0]['message_canonical_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, '消息'):
            apply_review(self.context, self.plan, apply=True)
        self.assertEqual(self.prefix, self.ledger.read_bytes())

    def test_unverified_gap_remains_missing_even_if_path_reappears(self):
        self.plan[0].update(decision='unresolved', reason='historical_version_unverified', reason_detail='当时版本无法确认')
        apply_review(self.context, self.plan, apply=True)
        self.old.write_bytes(b'later version')
        self.store.reconcile_attachments(self.key, 'm', [self.spec])
        row = self.store.attachment_status(self.key)[0]
        self.assertTrue(row['missing'])
        locator = {'s\0m\0delivery:fixture': {'local_path': str(self.old)}}
        summary = summarize([dict(row, session_key=self.key, source_session_id='s')], locator, read_reviews(self.context))
        self.assertEqual('当时版本无法确认', summary['unavailable'][0]['reason_detail'])
        self.assertEqual(1, summary['unavailable_by_reason']['historical_version_unverified'])
        self.assertEqual(self.prefix, self.ledger.read_bytes())

    def test_corrupt_archived_copy_is_reported_not_replaced(self):
        apply_review(self.context, self.plan, apply=True)
        row = self.store.attachment_status(self.key)[0]
        archived = self.context.root / row['stored_path']
        archived.write_bytes(b'corrupt')
        with self.assertRaisesRegex(OSError, 'SHA256'):
            self.store.reconcile_attachments(self.key, 'm', [self.spec])
        self.assertEqual(b'corrupt', archived.read_bytes())


if __name__ == '__main__':
    unittest.main()
