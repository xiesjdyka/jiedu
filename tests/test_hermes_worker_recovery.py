import ctypes
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import hermes_session_adapter as ha
from knowledge_bridge import source_adapters as adapters
from knowledge_bridge.knowledge_governance import _project_intro


class HermesWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'hermes.json'
        self.patch = patch.object(ha, 'STATE', self.state)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.old_worker, self.old_error = ha._worker_thread, ha._worker_error
        self.addCleanup(self.restore_runtime)

    def restore_runtime(self):
        ha._worker_thread, ha._worker_error = self.old_worker, self.old_error

    def test_transient_replace_failure_retries_without_touching_previous_checkpoint(self):
        ha._save({'cursor': 12})
        actual = os.replace
        observed = []

        def replace(source, target):
            observed.append(Path(source))
            if len(observed) < 3:
                self.assertEqual(12, json.loads(self.state.read_text())['cursor'])
                raise PermissionError('sharing violation')
            return actual(source, target)

        with patch.object(ha.os, 'replace', side_effect=replace), patch.object(ha.time, 'sleep'):
            ha._save({'cursor': 13})
        self.assertEqual(13, ha._load()['cursor'])
        self.assertEqual(3, len(observed))
        self.assertFalse(list(self.state.parent.glob('*.tmp')))

    def test_persistent_failure_preserves_checkpoint_and_cleans_own_temp(self):
        ha._save({'cursor': 12}); before = self.state.read_bytes()
        with patch.object(ha.os, 'replace', side_effect=PermissionError('denied')), patch.object(ha.time, 'sleep'):
            with self.assertRaises(PermissionError): ha._save({'cursor': 99})
        self.assertEqual(before, self.state.read_bytes())
        self.assertFalse(list(self.state.parent.glob('*.tmp')))

    def test_invalid_checkpoint_is_not_reset_to_source_end(self):
        self.state.write_text('{broken')
        with patch.object(ha, '_connect') as connect:
            with self.assertRaises(ValueError): ha.initialize('unused.db')
        connect.assert_not_called()
        self.assertEqual('{broken', self.state.read_text())

    def test_outer_failure_stays_visible_and_next_iteration_recovers(self):
        ha._save({'cursor': 12, 'last_sync': 'previous-success', 'status': 'watching'})
        stop = threading.Event(); calls = []

        def sync(_):
            calls.append(True)
            if len(calls) == 1: raise PermissionError('checkpoint locked')
            failed = ha._load()
            self.assertEqual(12, failed['cursor'])
            self.assertEqual('previous-success', failed['last_sync'])
            self.assertEqual('error', failed['status'])
            with patch.object(ha, 'list_sessions', return_value=[]):
                self.assertIn('checkpoint locked', ha.status()['last_error'])
            stop.set()
            return {'last_error': None}

        with patch.object(ha, 'sync_once', side_effect=sync), self.assertLogs(ha._logger, level='ERROR'):
            ha.run(.01, stop, 'fixture.db')
        self.assertEqual(2, len(calls)); self.assertIsNone(ha._worker_error)

    def test_stop_interrupts_poll_wait(self):
        stop = threading.Event(); entered = threading.Event()
        with patch.object(ha, 'sync_once', side_effect=lambda _: (entered.set() or {'last_error': None})):
            worker = threading.Thread(target=ha.run, args=(60, stop, 'fixture.db'))
            worker.start()
            self.assertTrue(entered.wait(1)); stop.set(); worker.join(1)
            self.assertFalse(worker.is_alive())

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing regression')
    def test_real_windows_reader_without_delete_share_recovers(self):
        ha._save({'cursor': 20})
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.state), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(ctypes.c_void_p(-1).value, handle)
        release = threading.Timer(.10, kernel.CloseHandle, args=(handle,)); release.start()
        try:
            with patch.object(ha.os, 'replace', wraps=os.replace) as replace:
                ha._save({'cursor': 21})
                self.assertGreater(replace.call_count, 1)
            self.assertEqual(21, ha._load()['cursor'])
        finally: release.join(1)


class HermesHealthAndLegacyControlsTests(unittest.TestCase):
    def test_stale_and_dead_workers_cannot_claim_automatic(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'state.db'
            with sqlite3.connect(database) as db:
                db.executescript('create table sessions(id);create table messages(id);')
            db.close()
            now = datetime.now(timezone.utc)
            for alive, stamp, expected in [(True, now-timedelta(seconds=121), 'degraded'),
                                           (False, now, 'error'), (True, now, 'automatic')]:
                with patch.object(ha, 'DEFAULT_DB', database), patch.object(ha, 'status', return_value={
                        'status': 'watching', 'last_sync': stamp.isoformat(), 'worker_alive': alive}):
                    self.assertEqual(expected, adapters._hermes_probe()['state'])

    def test_legacy_controls_retain_existing_values_without_identity_upgrade(self):
        row={'id':'hermes','client':'Hermes','client_access':False,'tool_permissions':{
            'search_memory':True,'save_raw_conversation':False,'create_candidate':True,'delete_memory':False}}
        with patch.object(adapters, 'for_client', return_value={}):
            view=adapters.legacy_client_view(row, [])
        self.assertEqual('legacy_unverified', view['status'])
        self.assertFalse(view['public_access_unchanged'])
        self.assertEqual({'search_memory':True,'save_raw_conversation':False,'create_candidate':True},view['tool_permissions'])
        self.assertEqual('deny', view['protected_access'])
        self.assertNotIn('internal_id', view)

    def test_project_intro_requires_saved_overview_not_detected_status(self):
        self.assertEqual('', _project_intro({'status':'暂无数据'}))
        self.assertEqual('保存过的项目说明。', _project_intro({'overview':'---\nstatus: old\n---\n# 项目\n\n> 文档说明\n\n保存过的项目说明。\n\n## 后续'}))


if __name__ == '__main__': unittest.main()
