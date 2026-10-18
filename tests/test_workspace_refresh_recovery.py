import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdog.events import FileModifiedEvent
from knowledge_bridge import evidence_updates, knowledge_base, watcher


class WorkspaceRefreshRecoveryTests(unittest.TestCase):
    def test_damaged_queue_preserves_bytes_and_reconciles(self):
        for raw in (b'\0' * 128, b'{"pending":', b'[]', b'{"pending":null}', b'\xff'):
            with self.subTest(raw=raw), TemporaryDirectory() as tmp:
                context = SimpleNamespace(queues=Path(tmp))
                queue = context.queues / 'evidence-updates.json'
                queue.write_bytes(raw)
                evidence_updates.enqueue(['new.md'], context)
                state = json.loads(queue.read_text('utf-8'))
                self.assertEqual(set(state['pending']), {'*', 'new.md'})
                backups = list(Path(tmp).glob('*.corrupt-*'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), raw)
                evidence_updates.enqueue(['late.md'], context)
                self.assertEqual(set(json.loads(queue.read_text())['pending']), {'*', 'new.md', 'late.md'})
                self.assertEqual(len(list(Path(tmp).glob('*.corrupt-*'))), 1)

    def test_normal_and_bom_queue_preserves_existing_tokens(self):
        for encoding in ('utf-8', 'utf-8-sig'):
            with TemporaryDirectory() as tmp:
                context = SimpleNamespace(queues=Path(tmp))
                queue = context.queues / 'evidence-updates.json'
                queue.write_text(json.dumps({'pending': {'old.md': 'stable-token'}}), encoding=encoding)
                evidence_updates.enqueue(['new.md'], context)
                self.assertEqual(json.loads(queue.read_text())['pending']['old.md'], 'stable-token')
                self.assertFalse(list(Path(tmp).glob('*.corrupt-*')))

    def test_event_failure_does_not_kill_dispatch_or_drop_formal_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / '00_系统' / 'example.md'
            file.parent.mkdir(); file.write_text('synthetic')
            handler = watcher.Handler()
            with patch.object(watcher, 'VAULT', root), patch.object(watcher, 'is_allowed', return_value=True), patch.object(handler, 'on_any_event', side_effect=ValueError('synthetic failure')):
                handler.dispatch(FileModifiedEvent(str(file)))
            self.assertIn('00_系统/example.md', handler._pending)
            self.assertIn('formal', handler._scope_pending)
            with patch.object(handler, 'on_any_event') as callback:
                handler.dispatch(FileModifiedEvent(str(file)))
                callback.assert_called_once()

    def test_status_reads_current_files_instead_of_saved_scan(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '00_系统').mkdir()
            config = root / 'config.json'; config.write_text('{}')
            data = {'vault_path': str(root), 'core_files': [], 'scan': {'markdown_files': ['old.md'], 'markdown_count': 437}}
            context = SimpleNamespace(workspace_id='fixture', initialized=True)
            with patch.object(knowledge_base, 'load', return_value=data), patch.object(knowledge_base, 'CONFIG_PATH', config), patch.object(knowledge_base, 'VAULT', root), patch.object(knowledge_base, 'is_workspace', return_value=True), patch.object(knowledge_base, 'load_workspace', return_value=context), patch.object(knowledge_base, 'active_workspace', return_value=context), patch.object(knowledge_base, '_audit_summary', return_value=None), patch('knowledge_bridge.core.overview', return_value={}):
                self.assertEqual(knowledge_base.status()['markdown_count'], 0)
                (root / '00_系统' / 'new.md').write_text('new')
                current = knowledge_base.status()
                self.assertEqual(current['markdown_count'], 1)
                self.assertEqual(current['available_core_files'], ['00_系统/new.md'])
                self.assertEqual(data['scan']['markdown_count'], 437)
                self.assertEqual(config.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
