"""Cooperative updater ownership tests; no real service/process is stopped."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from knowledge_bridge import process_runtime
from knowledge_bridge.bridge_launcher import Launcher

TOKEN='0123456789abcdef0123456789abcdef'

class WaitIdentityTests(unittest.TestCase):
    def identity(self,**changes):
        return {'pid':12345,'created':100,'executable':'synthetic-native.exe','argv':['synthetic-native.exe'],**changes}

    @unittest.skipUnless(os.name=='nt','Windows process identity API')
    def test_current_test_process_zero_wait_is_read_only(self):
        record=process_runtime.identity(os.getpid())
        self.assertIsNotNone(record)
        self.assertFalse(process_runtime.wait_identity_exit(record,0))

    def test_wait_uses_query_and_synchronize_never_terminate_rights(self):
        for status,exited in ((0,True),(258,False)):
            kernel=MagicMock();kernel.OpenProcess.return_value=8;kernel.WaitForSingleObject.return_value=status
            with self.subTest(status=status),patch.object(process_runtime,'_kernel',return_value=kernel),patch.object(process_runtime,'_inspect',return_value=self.identity()):
                self.assertEqual(exited,process_runtime.wait_identity_exit(self.identity(),.01))
            self.assertEqual(0x101000,kernel.OpenProcess.call_args.args[0])
            kernel.TerminateProcess.assert_not_called();kernel.CloseHandle.assert_called_once_with(8)

    def test_reused_pid_is_old_identity_exit_not_authority_to_stop_new_process(self):
        kernel=MagicMock();kernel.OpenProcess.return_value=8
        with patch.object(process_runtime,'_kernel',return_value=kernel),patch.object(process_runtime,'_inspect',return_value=self.identity(created=200)):
            self.assertTrue(process_runtime.wait_identity_exit(self.identity(),1))
        kernel.WaitForSingleObject.assert_not_called();kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(8)

    def test_incomplete_proof_and_unbounded_wait_are_rejected(self):
        for value,timeout in (({'pid':12345},1),(self.identity(),-1),(self.identity(),601)):
            with self.subTest(value=value,timeout=timeout),patch.object(process_runtime,'_kernel') as kernel,self.assertRaises(ValueError):
                process_runtime.wait_identity_exit(value,timeout)
            kernel.assert_not_called()

    def test_image_collector_filters_exact_path_not_filename(self):
        target=Path(tempfile.gettempdir())/'fixture-native.exe'
        correct=self.identity(executable=os.path.normcase(os.path.realpath(target)))
        wrong=self.identity(pid=12346,executable=os.path.normcase(os.path.realpath(target.parent/'other/fixture-native.exe')))
        with patch.object(process_runtime,'_process_ids_named',return_value=[12345,12346]),patch.object(process_runtime,'identity',side_effect=[correct,wrong]):
            self.assertEqual([correct],process_runtime.image_processes(target))

    def test_uninspectable_live_same_name_process_is_conservative_refusal(self):
        kernel=MagicMock();kernel.OpenProcess.return_value=8;kernel.WaitForSingleObject.return_value=258
        with patch.object(process_runtime,'_process_ids_named',return_value=[12345]),patch.object(process_runtime,'identity',return_value=None),patch.object(process_runtime,'_kernel',return_value=kernel),self.assertRaises(OSError):
            process_runtime.image_processes('synthetic-native.exe')
        kernel.TerminateProcess.assert_not_called();kernel.CloseHandle.assert_called_once_with(8)


class BytecodeFreeServiceTests(unittest.TestCase):
    def test_both_existing_and_bytecode_free_identity_require_exact_arguments(self):
        for flag in ([], ['-B']):
            arguments=['synthetic-python.exe',*flag,'-m','knowledge_bridge.cli','console','--launch-id',TOKEN,
                       '--vault','synthetic-workspace','--local-data','synthetic-control']
            actual={'pid':12345,'created':100,'executable':'synthetic-python.exe','argv':arguments}
            expected={**actual,'role':'console','launch_id':TOKEN}
            self.assertTrue(process_runtime.matches(actual,expected,{'synthetic-python.exe'}))
            changed={**actual,'argv':[*arguments,'--unexpected']}
            self.assertFalse(process_runtime.matches(changed,{**changed,'role':'console','launch_id':TOKEN},{'synthetic-python.exe'}))

    def test_broker_command_disables_bytecode_without_starting_process(self):
        result=MagicMock(returncode=0,stdout=b'{"ReturnValue":0,"ProcessId":12345}')
        with patch.object(process_runtime.subprocess,'run',return_value=result) as run,patch.dict(os.environ,{'SystemRoot':'C:/Windows'},clear=True):
            self.assertEqual(12345,process_runtime.spawn_service('synthetic-python.exe','synthetic-program','console',TOKEN,'synthetic-workspace','synthetic-control'))
        self.assertIn('synthetic-python.exe -B -m knowledge_bridge.cli console',run.call_args.args[0][-1])


class LauncherUpdateTests(unittest.TestCase):
    def setUp(self):
        root=Path(tempfile.mkdtemp(prefix='cooperative-update-'))
        self.launcher=object.__new__(Launcher)
        self.launcher.bridge_dir=root/'app/.jiedu/program'
        self.launcher.bridge_dir.mkdir(parents=True)
        self.launcher.local_data=root/'app/.jiedu/control';self.launcher.vault=root/'workspace'
        self.launcher.allowed_images={'synthetic-python.exe'}
        self.state={'launch_id':TOKEN,'bridge_dir':str(self.launcher.bridge_dir),'vault':str(self.launcher.vault)}
        self.launcher.load=MagicMock(return_value=self.state)
        self.launcher._receipt=MagicMock(return_value=None)
        self.native=self.launcher.bridge_dir.parent.parent/'界渡.exe';self.native.write_bytes(b'synthetic-not-executed')
        self.record={'pid':12345,'created':100,'executable':'synthetic-python.exe','argv':['synthetic-python.exe'],'role':'console','launch_id':TOKEN}

    def snapshot(self):
        with patch('knowledge_bridge.bridge_launcher.launch_processes',return_value=[self.record]),patch('knowledge_bridge.bridge_launcher.image_processes',return_value=[]):
            return self.launcher.snapshot_update_processes(TOKEN)

    def test_snapshot_is_separate_from_request_and_binds_current_launch(self):
        with patch('knowledge_bridge.update_quiesce.request_shutdown') as request:
            proof=self.snapshot()
        request.assert_not_called();self.assertEqual([self.record],proof['processes'])
        self.assertEqual(str(self.native.resolve()),proof['native_executable'])
        with self.assertRaises(ValueError):self.launcher.snapshot_update_processes('old-launch-0000000')
        with self.assertRaises(ValueError):self.launcher.snapshot_update_processes(TOKEN,native_executable=self.native.parent/'other.exe')

    def test_fast_request_passes_exact_program_scope_and_does_not_wait_or_kill(self):
        proof=self.snapshot()
        with patch.object(self.launcher,'snapshot_update_processes',return_value=proof),patch('knowledge_bridge.update_quiesce.request_shutdown',return_value={'token':TOKEN}) as request,patch('knowledge_bridge.bridge_launcher.wait_identity_exit') as wait,patch.object(self.launcher,'_stop_owned') as stop:
            result=self.launcher.request_update_stop(TOKEN,TOKEN)
        request.assert_called_once_with(self.launcher.local_data,TOKEN,TOKEN,program=self.launcher.bridge_dir)
        self.assertEqual(TOKEN,result['token']);wait.assert_not_called();stop.assert_not_called()

    def test_wait_requires_exit_and_rescan_and_leaves_prefix_scan_to_helper(self):
        proof=self.snapshot()
        with patch('knowledge_bridge.bridge_launcher.wait_identity_exit',return_value=True) as wait,patch('knowledge_bridge.bridge_launcher.launch_processes',return_value=[]),patch('knowledge_bridge.bridge_launcher.image_processes',return_value=[]),patch.object(self.launcher,'_port_open',return_value=False),patch.object(self.launcher,'_stop_owned') as stop:
            result=self.launcher.wait_update_stop(proof,.1)
        self.assertTrue(result['stopped']);self.assertTrue(result['installation_prefix_process_scan_still_required'])
        wait.assert_called_once();stop.assert_not_called()

    def test_timeout_or_raced_new_process_refuses_without_forced_stop(self):
        proof=self.snapshot()
        for exited,remaining in ((False,[]),(True,[self.record])):
            with self.subTest(exited=exited),patch('knowledge_bridge.bridge_launcher.wait_identity_exit',return_value=exited),patch('knowledge_bridge.bridge_launcher.launch_processes',return_value=remaining),patch('knowledge_bridge.bridge_launcher.image_processes',return_value=[]),patch.object(self.launcher,'_port_open',return_value=False),patch.object(self.launcher,'_stop_owned') as stop,self.assertRaises(RuntimeError):
                self.launcher.wait_update_stop(proof,0)
            stop.assert_not_called()

    def test_update_marker_blocks_automatic_backend_restart_before_observation(self):
        with patch('knowledge_bridge.update_quiesce.read_stop_request',return_value={'token':TOKEN}),patch.object(self.launcher,'_observe') as observe,patch.object(self.launcher,'_stop_owned') as stop,self.assertRaises(RuntimeError):
            self.launcher._start()
        observe.assert_not_called();stop.assert_not_called()

if __name__=='__main__':unittest.main()
