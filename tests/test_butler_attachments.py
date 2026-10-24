"""Synthetic attachment boundary checks; no app, OCR, models, or live workspace.

Run with TEMP/TMP and JIEDU_TEST_TEMP_ROOT set to an isolated test directory.
Container validation is intentionally not a claim of complete pixel decoding.
"""
import base64
import hashlib
import importlib.util
import json
import sqlite3
import struct
import tempfile
import types
import unittest
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import butler_attachments as attachments


def chunk(kind, payload=b''):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


def png(width=1, height=1, *, pixels=None, total_bytes=None):
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
    data += chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\xff') if pixels is None else pixels)
    if total_bytes is not None:
        size = total_bytes - len(data) - len(chunk(b'IEND')) - 12
        if size < 4:
            raise ValueError('fixture size too small')
        data += chunk(b'tEXt', b'key\x00' + b'x' * (size - 4))
    return data + chunk(b'IEND')


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        import os
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('JIEDU_TEST_TEMP_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.context = types.SimpleNamespace(state=Path(self.temp.name) / 'state')
        local = self.context.state / 'local-butler'
        local.mkdir(parents=True)
        self.database = local / 'conversations.sqlite'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE messages(session TEXT,attachments TEXT)')
        self.store = attachments.AttachmentStore(self.context)
        self.addCleanup(self.store.close)
        self.sid = uuid.uuid4().hex
        self.other = uuid.uuid4().hex
        self.data = png()

    def save(self, sid=None, data=None):
        return self.store.save(sid or self.sid, 'fixture.png', self.data if data is None else data)

    def metadata(self, value, **updates):
        _, path = self.store._paths(value['session'], value['id'])
        stored = json.loads(path.read_text('utf-8'))
        stored.update(updates)
        path.write_text(json.dumps(stored), encoding='utf-8')

    def freeze(self, value):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT INTO messages VALUES(?,?)', (value['session'], json.dumps([value])))

    def test_original_sha_and_data_url_preserve_exact_uploaded_bytes(self):
        value = self.store.save(self.sid, '../folder\\fixture\n.png', self.data)
        self.assertEqual(value['filename'], 'fixture.png')
        self.assertEqual(value['bytes'], len(self.data))
        self.assertEqual(value['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual((value['mime'], value['width'], value['height']), ('image/png', 1, 1))
        self.assertEqual(self.store.original(self.sid, value['id']).read_bytes(), self.data)
        self.assertEqual(self.store.get(self.sid, value['id']), value)
        part = self.store.image_parts(self.sid, [value['id']])[0]
        self.assertEqual(part['type'], 'image_url')
        prefix, encoded = part['image_url']['url'].split(',', 1)
        self.assertEqual(prefix, 'data:image/png;base64')
        self.assertEqual(base64.b64decode(encoded, validate=True), self.data)
        self.assertEqual(value['url'], f"/api/butler/sessions/{self.sid}/attachments/{value['id']}")
        self.assertNotIn('job_ids', value)

    def test_renamed_non_images_are_rejected_before_storage(self):
        for data in (b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'<script>fixture</script>', b'%PDF-fixture', b'MZfixture', b''):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                self.store.save(self.sid, 'renamed.png', data)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_supported_container_fixtures_preserve_bytes_and_derive_actual_mime(self):
        # Synthetic 1px GIF, JPEG (in-memory GDI+), and static VP8 WebP fixtures.
        # The production validator checks containers; these tests do not decode pixels.
        gif = bytes.fromhex('47494638396101000100800000000000ffffff2c00000000010001000002024401003b')
        jpeg = base64.b64decode(
            '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/'
            '2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QA'
            'HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
            'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG'
            'x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC'
            'AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE'
            'hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3'
            'E//Z')
        webp = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')
        for data, mime in ((self.data, 'image/png'), (jpeg, 'image/jpeg'), (gif, 'image/gif'), (webp, 'image/webp')):
            with self.subTest(mime=mime):
                value = self.store.save(self.sid, 'fixture.bin', data)
                self.assertEqual((value['mime'], value['width'], value['height']), (mime, 1, 1))
                self.assertEqual(self.store.original(self.sid, value['id']).read_bytes(), data)
                self.assertEqual(value['sha256'], hashlib.sha256(data).hexdigest())
                with self.assertRaises(ValueError):
                    attachments.image_info(data[:-1])

    def test_png_truncation_crc_dimensions_and_chunk_structure(self):
        broken_crc = bytearray(self.data)
        broken_crc[29] ^= 1
        malformed = [self.data[:-1], self.data[:-12], bytes(broken_crc), png(0, 1), png(8193, 1), png(1, 8193)]
        malformed += [b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) + chunk(b'IEND')]
        malformed += [self.data[:33] + self.data[8:33] + self.data[33:], self.data + b'extra']
        for index, data in enumerate(malformed):
            with self.subTest(index=index), self.assertRaises(ValueError):
                attachments.image_info(data)

    def test_single_image_limit_checks_actual_bytes(self):
        self.assertEqual(attachments.MAX_IMAGE_BYTES, 8 * 1024 * 1024)
        exact = png(total_bytes=attachments.MAX_IMAGE_BYTES)
        self.assertEqual(len(exact), attachments.MAX_IMAGE_BYTES)
        self.assertEqual(attachments.image_info(exact)['mime'], 'image/png')
        with self.assertRaises(ValueError):
            attachments.image_info(png(total_bytes=attachments.MAX_IMAGE_BYTES + 1))

    def test_identifiers_cannot_be_paths_and_sessions_do_not_share_originals(self):
        value = self.save()
        for invalid in ('../outside', '..', '/absolute', 'a' * 31, 'A' * 32, None, 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.save(invalid, 'fixture.png', self.data)
            with self.subTest(invalid_id=invalid), self.assertRaises(ValueError):
                self.store.original(self.sid, invalid)
        for operation in (self.store.get, self.store.original, self.store.delete):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(self.other, value['id'])
        with self.assertRaises(ValueError):
            self.store.select(self.other, [value['id']])
        self.assertEqual(self.store.original(self.sid, value['id']).read_bytes(), self.data)

    def test_blob_or_identity_metadata_tampering_is_rejected(self):
        value = self.save()
        blob, metadata = self.store._paths(self.sid, value['id'])
        original = metadata.read_bytes()
        for key, replacement in (('id', uuid.uuid4().hex), ('session', self.other), ('bytes', len(self.data) + 1), ('sha256', '0' * 64)):
            changed = json.loads(original)
            changed[key] = replacement
            metadata.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.get(self.sid, value['id'])
            metadata.write_bytes(original)
        blob.write_bytes(self.data[:-1] + b'x')
        for operation in (self.store.get, self.store.original):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(self.sid, value['id'])
        with self.assertRaises(ValueError):
            self.store.image_parts(self.sid, [value['id']])

    def test_preview_metadata_cannot_change_mime_dimensions_or_origin(self):
        value = self.save()
        _, metadata = self.store._paths(self.sid, value['id'])
        original = metadata.read_bytes()
        for key, replacement in (('mime', 'text/html'), ('width', 9000), ('height', 9000), ('url', 'https://fixture.invalid/image')):
            changed = json.loads(original)
            changed[key] = replacement
            metadata.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.get(self.sid, value['id'])
            metadata.write_bytes(original)

    def test_selection_limits_duplicates_types_and_actual_combined_size(self):
        self.assertEqual((attachments.MAX_IMAGES, attachments.MAX_TOTAL_BYTES), (4, 16 * 1024 * 1024))
        ids = [self.save()['id'] for _ in range(4)]
        self.assertEqual(len(self.store.select(self.sid, ids[:4])), 4)
        self.assertEqual(self.store.select(self.sid, []), [])
        for invalid in (ids + [uuid.uuid4().hex], [ids[0], ids[0]], [None], ids[0], None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.select(self.sid, invalid)
        # Scale only the total quota so boundary arithmetic uses tiny real originals.
        with patch.object(attachments, 'MAX_TOTAL_BYTES', 2 * len(self.data)):
            self.assertEqual(len(self.store.select(self.sid, ids[:2])), 2)
            with self.assertRaises(ValueError):
                self.store.select(self.sid, ids[:3])

    def test_bound_originals_are_retained_and_session_deletion_is_explicit(self):
        draft = self.save()
        sent = self.save()
        other = self.save(self.other)
        self.assertEqual(self.store.delete(self.sid, draft['id']), {'id': draft['id'], 'removed': True})
        jid = uuid.uuid4().hex
        self.store.bind(self.sid, [sent['id']], jid)
        self.store.bind(self.sid, [sent['id']], jid)
        _, metadata = self.store._paths(self.sid, sent['id'])
        self.assertEqual(json.loads(metadata.read_text('utf-8'))['job_ids'], [jid])
        with self.assertRaises(ValueError):
            self.store.delete(self.sid, sent['id'])
        self.assertTrue(self.store.original(self.sid, sent['id']).is_file())
        self.store.delete_session(self.sid)
        self.assertFalse((self.store.root / self.sid).exists())
        self.assertTrue(self.store.original(self.other, other['id']).is_file())
        self.store.delete_session(self.sid)  # Missing session is harmless.

    def test_unknown_file_prevents_partial_session_cleanup(self):
        value = self.save()
        unknown = self.store.root / self.sid / 'not-an-attachment.txt'
        unknown.write_text('synthetic guard', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.delete_session(self.sid)
        self.assertTrue(unknown.is_file())
        self.assertTrue(self.store.original(self.sid, value['id']).is_file())

    def test_refresh_lists_exact_drafts_and_allows_draft_removal(self):
        value = self.save()
        reopened = attachments.AttachmentStore(self.context)
        self.addCleanup(reopened.close)
        result = reopened.drafts(self.sid)
        self.assertEqual(result['attachments'], [value])
        self.assertEqual(result['retained_unknown'], {'count': 0, 'bytes': 0, 'message': ''})
        self.assertEqual(result['quota'], {'count': 1, 'bytes': len(self.data), 'max_count': 4, 'max_bytes': 16 * 1024 * 1024})
        self.assertEqual(result['retention_hours'], 24)
        reopened.delete(self.sid, value['id'])
        self.assertEqual(self.store.drafts(self.sid)['attachments'], [])

    def test_concurrent_reopened_stores_share_count_and_byte_reservations(self):
        reopened = attachments.AttachmentStore(self.context)
        self.addCleanup(reopened.close)
        for total, accepted in ((16 * 1024 * 1024, 4), (2 * len(self.data), 2)):
            sid = uuid.uuid4().hex
            def upload(index):
                store = self.store if index % 2 else reopened
                try:
                    store.save(sid, 'fixture.png', self.data)
                    return True
                except ValueError:
                    return False
            with patch.object(attachments, 'MAX_TOTAL_BYTES', total), ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(upload, range(12)))
                quota = self.store.drafts(sid)['quota']
            self.assertEqual(sum(results), accepted)
            self.assertEqual((quota['count'], quota['bytes']), (accepted, accepted * len(self.data)))

    def test_byte_quota_accepts_exact_boundary_and_recovers_after_delete(self):
        with patch.object(attachments, 'MAX_TOTAL_BYTES', 2 * len(self.data)):
            first, second = self.save(), self.save()
            with self.assertRaises(ValueError):
                self.save()
            self.assertEqual(self.store.drafts(self.sid)['quota']['bytes'], 2 * len(self.data))
            self.store.delete(self.sid, first['id'])
            third = self.save()
            self.assertEqual({value['id'] for value in self.store.drafts(self.sid)['attachments']}, {second['id'], third['id']})

    def test_expiry_retains_until_exact_24_hours_and_runs_on_reopen(self):
        start = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.store._clock = lambda: start
        value = self.save()
        self.store._clock = lambda: start + timedelta(hours=24) - timedelta(microseconds=1)
        self.assertEqual(self.store.drafts(self.sid)['attachments'], [value])
        reopened = attachments.AttachmentStore(self.context, clock=lambda: start + timedelta(hours=24))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.drafts(self.sid)['attachments'], [])
        with self.assertRaises(ValueError):
            reopened.original(self.sid, value['id'])

    def test_bounded_sweep_eventually_reaches_unvisited_sessions(self):
        start = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.store._clock = lambda: start
        values = [self.save(uuid.uuid4().hex) for _ in range(8)]
        self.store._clock = lambda: start + timedelta(hours=24)
        for _ in range(150):
            result = self.store.cleanup_expired(max_sessions=1, max_files=1)
            self.assertLessEqual(result['scanned_sessions'], 1)
            self.assertLessEqual(result['examined_files'], 1)
            if not list(self.store.root.glob('*/*.blob')):
                break
        self.assertEqual(list(self.store.root.glob('*/*.blob')), [])
        self.assertTrue(all(not (self.store.root / value['session'] / (value['id'] + '.json')).exists() for value in values))

    def test_frozen_message_protects_sent_original_when_job_ids_are_cleared(self):
        value = self.save()
        self.store.bind(self.sid, [value['id']], uuid.uuid4().hex)
        self.freeze(value)
        self.metadata(value, job_ids=[], created_at='2000-01-01T00:00:00+00:00')
        self.store.cleanup_expired()
        result = self.store.drafts(self.sid)
        self.assertEqual(result['attachments'], [])
        self.assertEqual(result['quota']['count'], 0)
        with self.assertRaises(ValueError):
            self.store.delete(self.sid, value['id'])
        self.assertEqual(self.store.original(self.sid, value['id']).read_bytes(), self.data)

    def test_database_failure_preserves_unverified_original_and_charges_quota(self):
        value = self.save()
        self.metadata(value, created_at='2000-01-01T00:00:00+00:00')
        self.database.write_bytes(b'synthetic invalid SQLite database')
        self.store.cleanup_expired()
        result = self.store.drafts(self.sid)
        self.assertEqual(result['attachments'], [])
        self.assertEqual(result['retained_unknown']['count'], 1)
        self.assertTrue(result['retained_unknown']['message'])
        self.assertEqual(result['quota']['count'], 1)
        self.assertGreater(result['quota']['bytes'], len(self.data))
        with self.assertRaises(ValueError):
            self.store.delete(self.sid, value['id'])
        self.assertEqual(self.store.original(self.sid, value['id']).read_bytes(), self.data)

    def test_corrupt_or_unknown_orphans_are_visible_preserved_and_use_quota(self):
        value = self.save()
        self.metadata(value, created_at='2000-01-01T00:00:00+00:00')
        blob, _ = self.store._paths(self.sid, value['id'])
        blob.write_bytes(b'synthetic changed original')
        directory = self.store.root / self.sid
        orphan = directory / (uuid.uuid4().hex + '.json')
        orphan.write_text('{}', encoding='utf-8')
        unknown = directory / 'unknown.tmp'
        unknown.write_bytes(b'leftover')
        expected = sum(path.stat().st_size for path in directory.iterdir())
        self.store.cleanup_expired()
        result = self.store.drafts(self.sid)
        self.assertEqual(result['attachments'], [])
        self.assertEqual(result['retained_unknown']['count'], 3)
        self.assertEqual(result['retained_unknown']['bytes'], expected)
        self.assertEqual((result['quota']['count'], result['quota']['bytes']), (3, expected))
        self.save()
        with self.assertRaises(ValueError):
            self.save()
        self.assertTrue(blob.exists() and orphan.exists() and unknown.exists())

    def test_unknown_directory_is_not_followed_and_reserves_full_quota(self):
        self.save()
        directory = self.store.root / self.sid / 'unknown-directory'
        directory.mkdir()
        result = self.store.drafts(self.sid)
        self.assertEqual(result['retained_unknown']['count'], 1)
        self.assertGreaterEqual(result['quota']['count'], attachments.MAX_IMAGES)
        self.assertGreaterEqual(result['quota']['bytes'], attachments.MAX_TOTAL_BYTES)
        with self.assertRaises(ValueError):
            self.save()
        self.assertTrue(directory.is_dir())

    def test_unreadable_inventory_is_visible_and_blocks_new_drafts(self):
        self.save()
        directory = self.store.root / self.sid
        original = Path.iterdir
        def unreadable(path):
            if path == directory:
                raise PermissionError('synthetic unavailable directory')
            return original(path)
        with patch.object(Path, 'iterdir', unreadable):
            result = self.store.drafts(self.sid)
            self.assertEqual(result['attachments'], [])
            self.assertEqual(result['retained_unknown']['count'], 1)
            self.assertTrue(result['retained_unknown']['message'])
            self.assertGreaterEqual(result['quota']['bytes'], attachments.MAX_TOTAL_BYTES)
            with self.assertRaises(ValueError):
                self.save()

    def test_failed_expiry_unlink_preserves_visible_original(self):
        value = self.save()
        self.metadata(value, created_at='2000-01-01T00:00:00+00:00')
        blob, metadata = self.store._paths(self.sid, value['id'])
        original = Path.unlink
        def cannot_remove(path, *args, **kwargs):
            if path == blob:
                raise PermissionError('synthetic original is busy')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', cannot_remove):
            result = self.store.drafts(self.sid)
            self.assertEqual(result['attachments'], [])
            self.assertEqual(result['retained_unknown']['count'], 1)
            self.assertTrue(result['retained_unknown']['message'])
        self.assertTrue(blob.exists() and metadata.exists())

    def test_truncated_gif_and_webp_need_real_image_structure(self):
        fake_gif = b'GIF89a' + struct.pack('<HHBBB', 1, 1, 0, 0, 0) + b',;'
        vp8x = b'\x00' * 10
        fake_webp = b'RIFF' + struct.pack('<I', 30) + b'WEBP' + b'VP8X' + struct.pack('<I', 10) + vp8x + b'VP8 junk'
        for data in (fake_gif, fake_webp):
            with self.subTest(kind=data[:6]), self.assertRaises(ValueError):
                attachments.image_info(data)

    def test_standalone_lifecycle_needs_no_knowledge_ocr_or_network(self):
        import builtins
        stdlib_import = builtins.__import__
        prohibited = ('knowledge_bridge', 'httpx', 'requests', 'onnxruntime', 'rapidocr', 'cv2', 'torch', 'lancedb')
        def guarded(name, *args, **kwargs):
            if any(name == prefix or name.startswith(prefix + '.') for prefix in prohibited):
                raise AssertionError('Attachment lifecycle attempted app/OCR/network import')
            return stdlib_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('synthetic_attachment_isolation', attachments.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=guarded):
            spec.loader.exec_module(module)
            store = module.AttachmentStore(types.SimpleNamespace(state=Path(self.temp.name) / 'standalone'))
            value = store.save(self.sid, 'fixture.png', self.data)
            self.assertEqual(store.original(self.sid, value['id']).read_bytes(), self.data)
            store.image_parts(self.sid, [value['id']])
            store.delete_session(self.sid)


if __name__ == '__main__':
    unittest.main()
