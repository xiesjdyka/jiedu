import json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from knowledge_bridge.package_layout import component
from knowledge_bridge.workspace import initialize_workspace,resolve_workspace_root
from knowledge_bridge.knowledge_base import markdown_files
from knowledge_bridge.search_scopes import files_for_scope


class PortablePackageTests(unittest.TestCase):
    def test_locator_prefers_powershell7_and_has_system_fallback(self):
        from knowledge_bridge.portable_entry import locator_shell
        import os
        with patch('pathlib.Path.is_file',return_value=True):
            self.assertEqual(Path(os.environ['LOCALAPPDATA'])/'Microsoft/WindowsApps/pwsh.exe',locator_shell())
        with patch('pathlib.Path.is_file',return_value=False),patch('shutil.which',return_value=None):
            self.assertEqual('powershell.exe',locator_shell().name)
        with patch('pathlib.Path.is_file',return_value=False),patch('shutil.which',return_value='C:/PowerShell7/pwsh.exe'):
            self.assertEqual(Path('C:/PowerShell7/pwsh.exe'),locator_shell())

    def test_clients_render_movable_locator_without_stale_cwd(self):
        import tomllib,yaml
        from knowledge_bridge.client_config import CodexAdapter,HermesAdapter,NAME
        command={'command':'system-shell','args':['locator','mcp'],'env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':'existing-id','KNOWLEDGE_BRIDGE_CLIENT_SECRET':'existing-secret'}}
        with patch('knowledge_bridge.client_config._command',return_value=command):
            old='[mcp_servers.knowledge_bridge]\ncommand="old"\ncwd="old-root"\n\n[mcp_servers.unrelated]\ncommand="keep"\n'
            parsed=tomllib.loads(CodexAdapter().render(old,'existing-id','existing-secret'))
            self.assertEqual(command,parsed['mcp_servers'][NAME])
            self.assertEqual('keep',parsed['mcp_servers']['unrelated']['command'])
            parsed=yaml.safe_load(HermesAdapter().render('other: keep\nmcp_servers:\n  knowledge_bridge:\n    cwd: old-root\n','existing-id','existing-secret'))
            self.assertEqual({**command,'enabled':True},parsed['mcp_servers'][NAME])
            self.assertEqual('keep',parsed['other'])

    def test_explicit_package_survives_outer_rename_and_has_relative_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'任意外层名字';program=root/'.jiedu/program';program.mkdir(parents=True)
            marker={'format_version':1,'layout':'jiedu-self-contained-v1','program':'program',
                    'workspace':'..','control':'control','python':'runtime/python/python.exe'}
            (program.parent/'package.json').write_text(json.dumps(marker),'utf-8')
            initialize_workspace(root)
            self.assertEqual(root,resolve_workspace_root(program,program.parent/'control'))
            renamed=root.with_name('改名后');root.rename(renamed)
            program=renamed/'.jiedu/program'
            self.assertEqual(renamed,component('workspace',program))
            self.assertEqual(renamed/'.jiedu/runtime/python/python.exe',component('python',program))
            self.assertNotIn(str(root),(program.parent/'package.json').read_text('utf-8'))

    def test_package_marker_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            program=Path(temporary)/'.jiedu/program';program.mkdir(parents=True)
            marker={'format_version':1,'layout':'jiedu-self-contained-v1','program':'program',
                    'workspace':'..','python':'../../old/python.exe'}
            (program.parent/'package.json').write_text(json.dumps(marker),'utf-8')
            with self.assertRaises(ValueError):component('python',program)

    def test_initialize_empty_and_existing_preserves_user_files_and_excludes_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'existing';(root/'03_知识').mkdir(parents=True)
            user=root/'03_知识/原有文件.md';user.write_bytes(b'original user bytes')
            context=initialize_workspace(root);identity=context.workspace_id
            secret=context.control/'program/README.md';secret.parent.mkdir();secret.write_text('internal','utf-8')
            self.assertEqual(identity,initialize_workspace(root).workspace_id)
            self.assertEqual(b'original user bytes',user.read_bytes())
            self.assertEqual(['03_知识/原有文件.md'],markdown_files(root))
            self.assertNotIn(secret,files_for_scope(context,'formal'))
            empty=initialize_workspace(Path(temporary)/'empty')
            self.assertEqual(7,len(list(empty.root.iterdir())))

    def test_no_implicit_parent_without_package_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            program=Path(temporary)/'program';program.mkdir()
            self.assertEqual(program/'knowledge-space',resolve_workspace_root(program,program/'data'))


if __name__=='__main__':unittest.main()
