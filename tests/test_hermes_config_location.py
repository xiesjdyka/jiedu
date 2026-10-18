import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.client_config import hermes_config_path


@unittest.skipUnless(os.name=='nt','Windows desktop path selection')
class HermesConfigLocationTests(unittest.TestCase):
    def test_desktop_user_data_override_keeps_its_own_home(self):
        with patch.dict(os.environ,{'HERMES_HOME':'','HERMES_DESKTOP_USER_DATA_DIR':r'C:\Hermes Desktop'}):
            self.assertEqual(Path(r'C:\Hermes Desktop\hermes-home\config.yaml'),hermes_config_path())

    def test_explicit_home_wins_over_other_configuration(self):
        with patch.dict(os.environ,{'HERMES_HOME':r'C:\Chosen Hermes'}):
            self.assertEqual(Path(r'C:\Chosen Hermes\config.yaml'),hermes_config_path())

    def test_user_environment_wins_when_service_environment_is_stale(self):
        with patch.dict(os.environ,{'HERMES_HOME':''}),patch('winreg.QueryValueEx',return_value=(r'C:\User Hermes',1)):
            self.assertEqual(Path(r'C:\User Hermes\config.yaml'),hermes_config_path())

    def test_native_home_wins_even_when_legacy_entry_is_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);native=root/'local/hermes';legacy=root/'.hermes'
            native.mkdir(parents=True);legacy.mkdir()
            (native/'config.yaml').write_text('{}','utf-8')
            (legacy/'config.yaml').write_text('mcp_servers:\n  knowledge_bridge: {}','utf-8')
            os.utime(legacy/'config.yaml',(2000000000,2000000000))
            with patch.dict(os.environ,{'HERMES_HOME':'','LOCALAPPDATA':str(root/'local')}),patch('pathlib.Path.home',return_value=root),patch('winreg.OpenKey',side_effect=OSError):
                self.assertEqual(native/'config.yaml',hermes_config_path())

    def test_legacy_fallback_only_when_native_home_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.hermes').mkdir()
            with patch.dict(os.environ,{'HERMES_HOME':'','LOCALAPPDATA':str(root/'local')}),patch('pathlib.Path.home',return_value=root),patch('winreg.OpenKey',side_effect=OSError):
                self.assertEqual(root/'.hermes/config.yaml',hermes_config_path())

if __name__=='__main__':unittest.main()
