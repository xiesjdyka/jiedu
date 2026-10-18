import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from knowledge_bridge.workspace import resolve_workspace_root
from knowledge_bridge.bridge_launcher import Launcher


class IndependentApplicationPathsTests(unittest.TestCase):
    def test_explicit_selection_and_override_do_not_depend_on_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);app=root/'independent-app';local=root/'machine';vault=root/'documents'/'knowledge'
            app.mkdir();local.mkdir();vault.mkdir(parents=True)
            (app/'config.json').write_text(json.dumps({'vault_root':'..'}),'utf-8')
            (local/'knowledge-base.json').write_text(json.dumps({'vault_path':str(vault)}),'utf-8')
            self.assertEqual(vault.resolve(),resolve_workspace_root(app,local))
            other=root/'other-workspace'
            self.assertEqual(other.resolve(),resolve_workspace_root(app,local,override=str(other)))
            with patch.dict('os.environ',{'KNOWLEDGE_BRIDGE_LOCAL_DATA':str(local),'KNOWLEDGE_BRIDGE_VAULT':''}):
                launcher=Launcher(app)
                self.assertEqual(vault.resolve(),launcher.vault)
                self.assertEqual(local.resolve(),launcher.local_data)

    def test_unselected_application_does_not_guess_parent_or_create_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=Path(temporary)/'app';app.mkdir();local=app/'data'
            self.assertEqual(app/'knowledge-space',resolve_workspace_root(app,local))
            self.assertFalse(local.exists())
            self.assertFalse((app/'knowledge-space').exists())
            (app/'config.json').write_text(json.dumps({'vault_root':'..'}),'utf-8')
            with self.assertRaises(ValueError):resolve_workspace_root(app,local)
            with self.assertRaises(ValueError):resolve_workspace_root(app,local,override=app.anchor)

    def test_explicit_config_and_machine_selection_are_consistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=Path(temporary)/'app';app.mkdir();local=app/'data';local.mkdir()
            (app/'config.json').write_text(json.dumps({'vault_root':'knowledge-space'}),'utf-8')
            self.assertEqual(app/'knowledge-space',resolve_workspace_root(app,local))
            selected=Path(temporary)/'selected'
            (local/'knowledge-base.json').write_text(json.dumps({'vault_path':str(selected)}),'utf-8')
            self.assertEqual(selected,resolve_workspace_root(app,local))

    def test_desktop_entry_uses_selected_launcher_and_configured_port(self):
        from knowledge_bridge import desktop_launcher
        launcher=Mock();launcher.settings.return_value={'console':{'port':9876}}
        with patch.object(desktop_launcher,'Launcher',return_value=launcher),patch.object(desktop_launcher.webbrowser,'open') as browser,patch('sys.argv',['jiedu','start']):
            self.assertEqual(0,desktop_launcher.main());launcher.start.assert_called_once()
            browser.assert_called_once_with('http://127.0.0.1:9876/')
        with patch.object(desktop_launcher,'Launcher',return_value=launcher),patch.object(desktop_launcher.webbrowser,'open') as browser,patch('sys.argv',['jiedu','stop']):
            self.assertEqual(0,desktop_launcher.main());launcher.stop.assert_called_once();browser.assert_not_called()

    def test_generated_mcp_config_explicitly_carries_workspace_and_machine_data(self):
        from knowledge_bridge import client_config
        import tomllib
        row=tomllib.loads(client_config.CodexAdapter().render('',client_id='fixture',secret='fixture-secret'))['mcp_servers']['knowledge_bridge']
        self.assertEqual('fixture-secret',row['env']['KNOWLEDGE_BRIDGE_CLIENT_SECRET'])
        self.assertTrue(Path(row['env']['KNOWLEDGE_BRIDGE_VAULT']).is_absolute())
        self.assertTrue(Path(row['env']['KNOWLEDGE_BRIDGE_LOCAL_DATA']).is_absolute())
        self.assertEqual(str(client_config.ROOT),row['cwd'])
