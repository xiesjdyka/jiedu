import base64,hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from knowledge_bridge.desktop_update import inspect_package,apply_files,safe_member

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.program=self.root/'program';self.program.mkdir()
        self.key=Ed25519PrivateKey.generate();self.pub=self.key.public_key().public_bytes_raw()
    def tearDown(self):self.tmp.cleanup()
    def package(self,files):
        path=self.root/'update.jiedu';manifest=json.dumps({'format':'jiedu-program-update-v1','version':'1.1.1','files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}).encode()
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('manifest.json',manifest);z.writestr('signature.txt',base64.b64encode(self.key.sign(manifest)))
            for n,v in files.items():z.writestr('program/'+n,v)
        return path
    def test_applies_code_preserves_settings_and_data(self):
        (self.program/'config.json').write_text('private');(self.root/'progress.json').write_text('cursor42')
        package=self.package({'static/app.js':b'new'})
        apply_files(package,self.program,self.root/'history',self.pub)
        self.assertEqual((self.program/'static/app.js').read_bytes(),b'new')
        self.assertEqual((self.program/'config.json').read_text(),'private');self.assertEqual((self.root/'progress.json').read_text(),'cursor42')
    def test_failure_rolls_back_old_and_removes_new(self):
        (self.program/'static').mkdir();(self.program/'static/app.js').write_text('old')
        package=self.package({'static/app.js':b'new','static/new.js':b'new file'})
        def fail(name):
            if name=='static/new.js':raise OSError('simulated write failure')
        with self.assertRaises(OSError):apply_files(package,self.program,self.root/'history',self.pub,fail)
        self.assertEqual((self.program/'static/app.js').read_text(),'old');self.assertFalse((self.program/'static/new.js').exists())
    def test_wrong_signature_rejected(self):
        package=self.package({'static/app.js':b'new'})
        with self.assertRaises(Exception):inspect_package(package,self.program,Ed25519PrivateKey.generate().public_key().public_bytes_raw())
        self.assertFalse((self.program/'static').exists())
    def test_paths_restricted(self):
        for path in ['../state/x','config.json','knowledge_bridge/update-public-key.txt','static/../../x','C:/x','.git/config','memory_compiler/records_v1.json','memory_compiler/private.txt','memory_compiler/data/records.json']:
            self.assertFalse(safe_member(path),path)
            with self.assertRaises(ValueError):inspect_package(self.package({path:b'bad'}),self.program,self.pub)
    def test_duplicate_zip_entry_rejected(self):
        package=self.package({'static/a.js':b'new'})
        with zipfile.ZipFile(package,'a') as z:z.writestr('program/static/a.js',b'bad')
        with self.assertRaises(ValueError):inspect_package(package,self.program,self.pub)

    def test_icon_assets_content_change_requires_full_installer_without_writes(self):
        target=self.program/'desktop/icon_assets.py'
        target.parent.mkdir()
        target.write_bytes(b'# installed synthetic icon input')
        same=self.package({'desktop/icon_assets.py':target.read_bytes()})
        self.assertIn('desktop/icon_assets.py',inspect_package(same,self.program,self.pub)['files'])
        changed=self.package({'desktop/icon_assets.py':b'# changed synthetic icon input'})
        with self.assertRaisesRegex(ValueError,'原生桌面程序.*完整安装器'):
            inspect_package(changed,self.program,self.pub)
        self.assertEqual(target.read_bytes(),b'# installed synthetic icon input')

if __name__=='__main__':unittest.main()
