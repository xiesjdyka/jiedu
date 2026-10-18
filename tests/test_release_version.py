import base64
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop.release_version import check, sync, version_info
from knowledge_bridge.desktop_update import inspect_package
from knowledge_bridge.online_update import version
from knowledge_bridge.release_version import version_info as installed_version_info


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_single_version_synchronizes_metadata_and_detects_drift(self):
        source=Path(__file__).resolve().parents[1]
        for name in ['VERSION','pyproject.toml','knowledge_bridge/__init__.py','desktop/Cargo.toml','desktop/Cargo.lock','desktop/tauri.conf.json','desktop/installer.iss']:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
        (self.root/'VERSION').write_text('1.1.0-rc.9\n','ascii')
        result=sync(self.root)
        self.assertEqual(result['python_version'],'1.1.0rc9')
        self.assertEqual(result['file_version'],'1.1.0.9')
        self.assertEqual(version(self.root),'1.1.0-rc.9')
        (self.root/'desktop/tauri.conf.json').write_text('{"version":"1.0.0"}')
        with self.assertRaisesRegex(ValueError,'differs'):check(self.root)

    def test_windows_numeric_version_limits(self):
        (self.root/'VERSION').write_text('1.1.0-rc.65536','ascii')
        with self.assertRaisesRegex(ValueError,'65535'):version_info(self.root)

    def test_program_release_preserves_native_build_metadata(self):
        source=Path(__file__).resolve().parents[1]
        for name in ['VERSION','pyproject.toml','knowledge_bridge/__init__.py','desktop/Cargo.toml','desktop/Cargo.lock','desktop/tauri.conf.json','desktop/installer.iss']:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
        before={name:(self.root/name).read_bytes() for name in ['desktop/Cargo.toml','desktop/Cargo.lock','desktop/tauri.conf.json']}
        (self.root/'VERSION').write_text('1.1.0-rc.99','ascii')
        result=sync(self.root,scope='program')
        self.assertTrue(result['passed']);self.assertFalse(result['product_matches_native_build'])
        self.assertEqual(installed_version_info(self.root)['product_version'],'1.1.0-rc.99')
        self.assertEqual(before,{name:(self.root/name).read_bytes() for name in before})
        with self.assertRaisesRegex(ValueError,'differs'):check(self.root,scope='full')
        (self.root/'VERSION').write_text('1.1.0-rc.98','ascii')
        with self.assertRaisesRegex(ValueError,'disagree'):installed_version_info(self.root)

    def package(self,files):
        key=Ed25519PrivateKey.generate();path=self.root/'test.jiedu'
        manifest=json.dumps({'format':'jiedu-program-update-v1','version':'1.1.0-rc.5','files':{n:hashlib.sha256(raw).hexdigest() for n,raw in files.items()}}).encode()
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('manifest.json',manifest);z.writestr('signature.txt',base64.b64encode(key.sign(manifest)))
            for name,raw in files.items():z.writestr('program/'+name,raw)
        return path,key.public_key().public_bytes_raw()

    def test_version_marker_updates_and_mismatch_is_rejected(self):
        (self.root/'VERSION').write_text('1.1.0-rc.4','ascii')
        files={'VERSION':b'1.1.0-rc.5','pyproject.toml':b'[project]\nversion="1.1.0rc5"'}
        package,key=self.package(files)
        self.assertEqual(inspect_package(package,self.root,key)['version'],'1.1.0-rc.5')
        files['VERSION']=b'1.1.0-rc.6';package,key=self.package(files)
        with self.assertRaisesRegex(ValueError,'产品版本'):inspect_package(package,self.root,key)
        package,key=self.package({'static/fixture.js':b'new'})
        with self.assertRaisesRegex(ValueError,'版本元数据'):inspect_package(package,self.root,key)

    def test_native_source_change_requires_complete_installer(self):
        native=self.root/'desktop/src/main.rs';native.parent.mkdir(parents=True);native.write_bytes(b'old native')
        files={'VERSION':b'1.1.0-rc.5','pyproject.toml':b'[project]\nversion="1.1.0rc5"','desktop/src/main.rs':b'new native'}
        package,key=self.package(files)
        with self.assertRaisesRegex(ValueError,'完整安装器'):inspect_package(package,self.root,key)


if __name__=='__main__':unittest.main()
