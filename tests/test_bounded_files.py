import os
import tempfile
import threading
import base64
import subprocess
import shutil
import unittest
from pathlib import Path

from knowledge_bridge.bounded_files import SelectedFolders,WindowsHandles,relative_parts
from knowledge_bridge.workspace import initialize_workspace


@unittest.skipUnless(os.name=='nt','Windows native folder boundary tests')
class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.context=initialize_workspace(self.base/'workspace')
        self.folder=self.base/'selected';self.folder.mkdir();(self.folder/'sub').mkdir()
        self.file=self.folder/'file.txt';self.file.write_text('fixture original','utf-8')
        self.service=SelectedFolders(self.context);self.key=self.service.add(str(self.folder))['id']

    def test_read_list_and_no_workspace_overlap_or_traversal(self):
        self.assertEqual('fixture original',self.service.read(self.key,'file.txt')['content'])
        self.assertIn('file.txt',[row['name'] for row in self.service.list_files(self.key)['items']])
        for path in ('../secret','/absolute','sub/../../secret','C:/secret','file.txt:stream','NUL.txt','COM1.txt','sub./file','sub /file','.jiedu-recovery/file'):
            with self.subTest(path=path),self.assertRaises(ValueError):self.service.read(self.key,path)
        with self.assertRaises(ValueError):self.service.add(str(self.context.root))
        with self.assertRaises(ValueError):self.service.add(str(self.base))

    def test_write_move_delete_and_recover_keep_original_bytes(self):
        preview=self.service.preview(self.key,'write','file.txt',content='fixture new')
        receipt=self.service.apply(preview['preview_id']);self.assertEqual('applied',receipt['state'])
        self.assertEqual('fixture new',self.file.read_text('utf-8'))
        self.service.recover(receipt['change_id']);self.assertEqual('fixture original',self.file.read_text('utf-8'))
        for action,dest in (('rename','renamed.txt'),('move','sub/moved.txt')):
            preview=self.service.preview(self.key,action,'file.txt',destination=dest)
            receipt=self.service.apply(preview['preview_id']);self.assertFalse(self.file.exists())
            self.assertEqual('fixture original',(self.folder/dest).read_text('utf-8'))
            self.service.recover(receipt['change_id']);self.assertTrue(self.file.exists())
        preview=self.service.preview(self.key,'delete','file.txt');receipt=self.service.apply(preview['preview_id'])
        self.assertFalse(self.file.exists());self.service.recover(receipt['change_id'])
        self.assertEqual('fixture original',self.file.read_text('utf-8'))

    def test_atomic_create_and_undo_does_not_permanently_delete(self):
        preview=self.service.preview(self.key,'write','new.txt',content='new fixture')
        receipt=self.service.apply(preview['preview_id']);self.service.recover(receipt['change_id'])
        self.assertFalse((self.folder/'new.txt').exists())
        self.assertTrue((self.folder/'.jiedu-recovery'/(receipt['change_id']+'.replaced')).exists())

    def test_stale_preview_revocation_and_changed_file_recovery_refuse_overwrite(self):
        preview=self.service.preview(self.key,'write','file.txt',content='proposed')
        self.file.write_text('user changed','utf-8')
        with self.assertRaises(ValueError):self.service.apply(preview['preview_id'])
        preview=self.service.preview(self.key,'write','file.txt',content='proposed');receipt=self.service.apply(preview['preview_id'])
        self.file.write_text('later edit','utf-8')
        with self.assertRaises(ValueError):self.service.recover(receipt['change_id'])
        self.assertEqual('later edit',self.file.read_text('utf-8'))
        preview=self.service.preview(self.key,'write','file.txt',content='denied');self.service.revoke(self.key)
        with self.assertRaises(PermissionError):self.service.apply(preview['preview_id'])

    def test_parent_and_leaf_pinning_prevent_external_swap(self):
        handles=WindowsHandles()
        with handles.pin(self.folder,('sub',)):
            with self.assertRaises(OSError):(self.folder/'sub').rename(self.folder/'swapped')
        with handles.pin(self.folder,()),handles.open(self.file,mutate=True):
            with self.assertRaises(OSError):self.file.write_text('external race','utf-8')
            with self.assertRaises(OSError):self.file.rename(self.folder/'changed.txt')
        self.assertEqual('fixture original',self.file.read_text('utf-8'))

    def test_reparse_and_hardlinks_cannot_escape(self):
        outside=self.base/'outside';outside.mkdir();(outside/'secret.txt').write_text('fixture private','utf-8')
        link=self.folder/'link'
        try:os.symlink(outside,link,target_is_directory=True)
        except OSError:
            quote=lambda value:"'"+str(value).replace("'","''")+"'"
            command='New-Item -ItemType Junction -Path '+quote(link)+' -Target '+quote(outside)+' | Out-Null'
            pwsh=shutil.which('pwsh')
            if not pwsh:self.skipTest('PowerShell 7 required for isolated junction fixture')
            result=subprocess.run([pwsh,'-NoProfile','-EncodedCommand',base64.b64encode(command.encode('utf-16-le')).decode()],capture_output=True)
            self.assertEqual(0,result.returncode,'fixture junction creation failed')
        try:
            with self.assertRaises(ValueError):self.service.read(self.key,'link/secret.txt')
            with self.assertRaises(ValueError):self.service.preview(self.key,'write','link/secret.txt',content='bad')
        finally:
            if link.is_symlink():link.unlink()
            else:link.rmdir()
        hard=self.folder/'hard.txt';os.link(outside/'secret.txt',hard)
        with self.assertRaises(ValueError):self.service.read(self.key,'hard.txt')
        self.assertEqual('fixture private',(outside/'secret.txt').read_text('utf-8'))

    def test_cancel_before_install_leaves_recoverable_original(self):
        preview=self.service.preview(self.key,'write','file.txt',content='never install');calls=[]
        def validate():
            calls.append(1)
            if len(calls)==2:raise InterruptedError('synthetic cancel')
        with self.assertRaises(InterruptedError):self.service.apply(preview['preview_id'],validate=validate)
        self.assertFalse(self.file.exists())
        self.service.recover(preview['preview_id']);self.assertEqual('fixture original',self.file.read_text('utf-8'))

    def test_two_writers_same_preview_are_idempotent(self):
        preview=self.service.preview(self.key,'write','file.txt',content='one fixture');results=[];errors=[]
        def write():
            try:results.append(self.service.apply(preview['preview_id']))
            except Exception as error:errors.append(error)
        workers=[threading.Thread(target=write) for _ in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(2)
        self.assertEqual([],errors);self.assertEqual(2,len(results));self.assertEqual(results[0],results[1])
        self.assertEqual('one fixture',self.file.read_text('utf-8'))


if __name__=='__main__':unittest.main()
