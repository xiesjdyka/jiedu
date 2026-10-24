"""Compile the real destination gate in a tiny, isolated Inno harness.

Set JIEDU_TEST_ISCC to an existing compiler to run the Windows integration test.
The harness has no registry entries, uninstaller, network assets, or launch action.
It verifies destination admission only, not the complete released installer.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from knowledge_bridge.full_installer_update import HELPER_SCRIPT

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'desktop/installer.iss'
ISCC = os.environ.get('JIEDU_TEST_ISCC') or shutil.which('ISCC.exe')


class InstallerDestinationContractTests(unittest.TestCase):
    def test_gate_runs_before_files_and_helper_installs_only_after_directory_move(self):
        source = SOURCE.read_text('utf-8-sig')
        prepare = source.split('function PrepareToInstall(', 1)[1].split('procedure InitializeWizard;', 1)[0]
        self.assertIn("Result := ValidateDestination;\n  if Result <> '' then Exit;", prepare)
        self.assertIn('Result := ValidateDestination;', prepare)
        self.assertLess(HELPER_SCRIPT.index('Move-Item -LiteralPath $target -Destination $backup'),
                        HELPER_SCRIPT.index('$p=Start-Process -FilePath $setup'))
        self.assertIn('Excludes: "config.json,AGENTS.md,__pycache__,*.pyc,data,knowledge-space"', source)


@unittest.skipUnless(os.name == 'nt' and ISCC and Path(ISCC).is_file(),
                     'Set JIEDU_TEST_ISCC to run isolated Inno destination checks')
class InstallerDestinationCompiledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='jiedu-destination-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        source = SOURCE.read_text('utf-8-sig')
        gate = 'function ValidateDestination: String;' + source.split('function ValidateDestination: String;', 1)[1].split('function NextButtonClick(', 1)[0]
        prepare = 'function PrepareToInstall(' + source.split('function PrepareToInstall(', 1)[1].split('procedure InitializeWizard;', 1)[0]
        payload = cls.root / 'synthetic.txt'; payload.write_text('synthetic-installed', 'utf-8')
        defines = '\n'.join('#define ' + name + ' 0' for name in (
            'RuntimeArchiveBytes', 'VectorArchiveBytes', 'OCRArchiveBytes',
            'RuntimeInstallBytes', 'VectorInstallBytes', 'OCRInstallBytes'))
        script = defines + f'''
[Setup]
AppName=Jiedu destination test
AppVersion=0.0.0
DefaultDirName={cls.root / 'unused'}
OutputDir={cls.root}
OutputBaseFilename=destination-test
PrivilegesRequired=lowest
Uninstallable=no
CreateUninstallRegKey=no
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
[Files]
Source: "{payload}"; DestDir: "{{app}}"; Flags: ignoreversion
[Code]
''' + gate + '\nvar PreviousProgramVersion: String;\n' + prepare
        path = cls.root / 'harness.iss'; path.write_text(script, 'utf-8-sig')
        result = subprocess.run([ISCC, '/Q', str(path)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.exe = cls.root / 'destination-test.exe'

    def inventory(self, root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def invoke(self, target):
        return subprocess.run([str(self.exe), '/VERYSILENT', '/SUPPRESSMSGBOXES',
            '/NORESTART', '/SP-', '/DIR=' + str(target),
            '/LOG=' + str(self.root / (target.name + '.log'))],
            capture_output=True, timeout=30)

    def test_existing_installs_and_unknown_data_are_rejected_without_changes(self):
        fixtures = {
            'registered': {'jiedu-install.txt': 'jiedu-consumer-install-v1'},
            'partial-old': {'.jiedu/program/VERSION': '1.1.8'},
            'self-contained': {'.jiedu/package.json': '{"layout":"jiedu-self-contained-v1"}'},
            'unrecognized': {'keep.txt': 'customer data'},
        }
        for label, contents in fixtures.items():
            with self.subTest(label=label):
                target = self.root / label
                for name, text in contents.items():
                    path = target / name; path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, 'utf-8')
                before = self.inventory(target)
                result = self.invoke(target)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(before, self.inventory(target))
                self.assertFalse((target / 'synthetic.txt').exists())

    def test_fresh_and_helper_vacated_destinations_accept(self):
        for label in ('fresh', 'helper-vacated'):
            with self.subTest(label=label):
                target = self.root / label
                if label == 'helper-vacated':
                    target.mkdir(); (target / 'jiedu-install.txt').write_text('jiedu-consumer-install-v1')
                    target.rename(self.root / (label + '.previous'))
                result = self.invoke(target)
                self.assertEqual(0, result.returncode)
                self.assertEqual('synthetic-installed', (target / 'synthetic.txt').read_text('utf-8'))
        self.assertTrue((self.root / 'helper-vacated.previous/jiedu-install.txt').is_file())


if __name__ == '__main__':
    unittest.main()
