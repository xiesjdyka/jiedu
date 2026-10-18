import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

spec=importlib.util.spec_from_file_location('export_source',Path(__file__).resolve().parents[1]/'scripts/export_source.py')
exporter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class SourceExportTests(unittest.TestCase):
    def test_unlisted_private_file_is_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'README.md').write_text('synthetic source')
            (root/'public-source-files.txt').write_text('README.md\n')
            (root/'records_v1.json').write_text('synthetic private data')
            archive=root/'public.zip'
            exporter.export(root,archive)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual({'jiedu/README.md','jiedu/release-manifest.json'},set(z.namelist()))
            with self.assertRaises(ValueError):exporter.export(root,archive)

    def test_accidentally_approved_private_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ['../private.txt','/private.txt','C:/secret','config.json','data/message.json',
                         '.git/config','memory_compiler/records_v1.json','memory_compiler/export.txt',
                         'data\\archive.json','credentials.key']:
                with self.subTest(name=name),self.assertRaises(ValueError):
                    exporter.approved_path(Path(tmp),name)
