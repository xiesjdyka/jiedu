"""Synthetic broker receipts and birth identity failures; no real processes."""
import ctypes
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from knowledge_bridge import process_runtime as runtime
from knowledge_bridge.bridge_launcher import Launcher

TOKEN='syntheticlaunchidentity'


class ServiceCreationTests(unittest.TestCase):
    def setUp(self):
        self.args=['synthetic-python.exe','-B','-m','knowledge_bridge.cli','console','--launch-id',TOKEN,
                   '--vault','synthetic-workspace','--local-data','synthetic-control']
        self.record={'pid':12345,'created':100,'executable':os.path.normcase(os.path.realpath(self.args[0])),'argv':self.args}

    def spawn(self,response=None,actual=None,returncode=0):
        response={'ReturnValue':0,'ProcessId':12345,'Created':100} if response is None else response
        result=SimpleNamespace(returncode=returncode,stdout=json.dumps(response).encode())
        with patch.dict(os.environ,{'SystemRoot':'C:/Windows'},clear=True),patch.object(runtime.subprocess,'run',return_value=result) as run,patch.object(runtime,'identity',return_value=self.record if actual is None else actual),patch.object(runtime,'terminate_owned') as terminate:
            try:return runtime.spawn_service(self.args[0],'synthetic-program','console',TOKEN,'synthetic-workspace','synthetic-control')
            finally:
                self.run_call=run.call_args
                terminate.assert_not_called()  # No kill or adoption from a returned PID alone.

    def test_success_binds_creation_birth_image_and_complete_command(self):
        record=self.spawn();self.assertEqual(record['created'],100);self.assertEqual(record['creation_method'],'wmi-breakaway')
        script=self.run_call.args[0][-1]
        self.assertIn('CreateFlags=[uint32]16777224',script)
        self.assertIn('StartTime.ToUniversalTime().ToFileTimeUtc()',script)
        self.assertIn('System32',self.run_call.args[0][0])
        self.assertIn('WindowsPowerShell',self.run_call.args[0][0])

    def test_creation_denied_does_not_fallback_or_accept_a_pid(self):
        for code in (2,3,8,9,21):
            with self.subTest(code=code),self.assertRaisesRegex(OSError,'返回码 '+str(code)):
                self.spawn({'ReturnValue':code,'ProcessId':12345,'Created':100})

    def test_broker_shell_failure_does_not_accept_success_shaped_output(self):
        with self.assertRaisesRegex(OSError,'创建接口失败'):self.spawn(returncode=1)

    def test_malformed_or_incomplete_receipts_are_rejected(self):
        for response in ([],{}, {'ReturnValue':False}, {'ReturnValue':0,'ProcessId':True,'Created':100},
                         {'ReturnValue':0,'ProcessId':12345}, {'ReturnValue':0,'ProcessId':12345,'Created':'100'}):
            with self.subTest(response=response),self.assertRaises(OSError):self.spawn(response)

    def test_recycled_pid_with_wrong_birth_is_not_adopted_or_killed(self):
        with self.assertRaisesRegex(OSError,'身份不一致'):self.spawn(actual=self.record|{'created':101})

    def test_wrong_executable_or_any_changed_argument_is_rejected(self):
        variants=[self.record|{'executable':'unrelated.exe'},self.record|{'argv':self.args+['--unexpected']},
                  self.record|{'argv':[value if value!=TOKEN else 'differentlaunch' for value in self.args]},
                  self.record|{'argv':[value if value!='console' else 'watch' for value in self.args]}]
        for record in variants:
            with self.subTest(record=record),self.assertRaisesRegex(OSError,'身份不一致'):self.spawn(actual=record)

    def test_process_exited_before_identity_capture_is_rejected(self):
        with self.assertRaisesRegex(OSError,'进程已退出'):self.spawn(actual={})

    def test_job_query_can_check_a_specific_owner_instead_of_any_job(self):
        kernel=Mock();kernel.OpenProcess.return_value=44
        def query(process,job,result):
            self.assertEqual((process,job),(44,55));ctypes.cast(result,ctypes.POINTER(ctypes.wintypes.BOOL)).contents.value=False
            return True
        kernel.IsProcessInJob.side_effect=query
        with patch.object(runtime,'_kernel',return_value=kernel):self.assertFalse(runtime.in_job(12345,55))
        kernel.CloseHandle.assert_called_once_with(44)

    def launcher(self):
        launcher=Launcher.__new__(Launcher);launcher.python=Path(self.args[0]);launcher.pythonw=Path('missing-synthetic-pythonw.exe')
        launcher.bridge_dir=Path('synthetic-program');launcher.vault=Path('synthetic-workspace');launcher.local_data=Path('synthetic-control')
        launcher.allowed_images={self.record['executable']};launcher.runtime_profile={'model_precision':'fp32','embedding_batch_size':32,'embedding_duty_cycle':1.0}
        return launcher

    def test_launcher_retains_verified_birth_for_state_and_cleanup(self):
        launcher=self.launcher();record=self.record|{'creation_method':'wmi-breakaway','creation_flags':16777224}
        with patch('knowledge_bridge.bridge_launcher.spawn_service',return_value=record),patch('knowledge_bridge.bridge_launcher.identity') as recapture:
            captured=launcher._spawn('console',TOKEN,None)
        self.assertEqual(captured['created'],100);self.assertEqual(captured['launch_id'],TOKEN)
        recapture.assert_not_called()  # Preserve the broker-verified identity, avoiding a PID re-open race.

    def test_launcher_rejects_an_unallowed_image(self):
        launcher=self.launcher()
        with patch('knowledge_bridge.bridge_launcher.spawn_service',return_value=self.record|{'executable':'foreign.exe'}),self.assertRaisesRegex(OSError,'不属于本次服务'):
            launcher._spawn('console',TOKEN,None)


if __name__=='__main__':unittest.main()
