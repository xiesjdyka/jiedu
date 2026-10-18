import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from knowledge_bridge.bridge_launcher import Launcher

class LauncherPreflightTests(unittest.TestCase):
    def launcher(self,observe):
        x=Launcher.__new__(Launcher);x.vault=Path('D:/fixture');x.kb=Mock();x.kb.exists.return_value=True
        x.local_data=Path(tempfile.gettempdir())/'launcher-preflight-unconfigured'
        x.load=Mock(return_value={'vault':str(x.vault),'console_pid':123,'watcher_pid':456,'launch_id':'original'})
        x._observe=Mock(side_effect=observe);x._port_open=Mock(return_value=True);x._stop_owned=Mock();x.save=Mock();x.log=Mock()
        return x
    def test_preflight_error_never_stops_existing_processes(self):
        x=self.launcher([{'status':'partial','health':None,'health_owned':False,'console_alive':True}])
        with self.assertRaisesRegex(RuntimeError,'端口'):x._start(wait=0)
        x._stop_owned.assert_not_called();state=x.save.call_args.args[0]
        self.assertEqual((state['console_pid'],state['watcher_pid'],state['launch_id']),(123,456,'original'))
    def test_transient_health_failure_reuses_original_launch(self):
        x=self.launcher([{'status':'partial','health':None,'health_owned':False,'console_alive':True},{'status':'connected','health_owned':True}])
        with patch('knowledge_bridge.bridge_launcher.time.sleep'):
            self.assertTrue(x._start()['reused'])
        x._stop_owned.assert_not_called();x.save.assert_not_called()
    def test_loopback_health_disables_proxy(self):
        x=Launcher.__new__(Launcher);x.settings=Mock(return_value={'console':{'port':8765}})
        response=Mock();response.read.return_value=b'{"status":"ready"}'
        with patch('knowledge_bridge.bridge_launcher.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value=response
            self.assertEqual(x.health(),{'status':'ready'});self.assertEqual(build.call_args.args[0].proxies,{})
