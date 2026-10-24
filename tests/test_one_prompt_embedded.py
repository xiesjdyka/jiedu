"""Simulate the installed _pth layout without loading production program code."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name=='nt','embedded Windows Python layout')
class EmbeddedHandoffTests(unittest.TestCase):
    def test_embedded_python_imports_candidate_and_exposes_handoff_cli(self):
        runtime_source=Path(r'D:\Jiedu\.jiedu\runtime\python')
        if not (runtime_source/'python313._pth').is_file():
            self.skipTest('local embedded Python 3.13 fixture unavailable')
        candidate=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='jiedu-pth-fixture-') as temporary:
            control=Path(temporary)/'.jiedu'
            runtime=control/'runtime'/'python';runtime.mkdir(parents=True)
            program=control/'program';program.mkdir()
            for source in runtime_source.iterdir():
                if source.is_file():shutil.copy2(source,runtime/source.name)
            shutil.copytree(candidate/'knowledge_bridge',program/'knowledge_bridge',
                ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            executable=runtime/'python.exe'
            output=subprocess.run([str(executable),'-I','-X','utf8','-c',
                'import knowledge_bridge.cli,knowledge_bridge.one_prompt_connect; '
                'print(knowledge_bridge.cli.__file__)'],cwd=program,
                capture_output=True,text=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(0,output.returncode,output.stderr)
            self.assertTrue(Path(output.stdout.strip()).is_relative_to(program),output.stdout)
            help_result=subprocess.run([str(executable),'-I','-X','utf8','-m',
                'knowledge_bridge.cli','handoff','--help'],cwd=program,
                capture_output=True,text=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(0,help_result.returncode,help_result.stderr)
            self.assertIn('--workspace-id',help_result.stdout)


if __name__=='__main__':unittest.main()
