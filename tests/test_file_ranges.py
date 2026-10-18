import json
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.client_access import ClientPermissionService
from knowledge_bridge.client_access import ClientIdentityRegistry
from knowledge_bridge.bounded_files import SelectedFolders
from knowledge_bridge.file_browser import FileBrowser,root_id
from knowledge_bridge.butler_tools import ProductTools,ToolRunner

class FileRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.permissions=ClientPermissionService(ClientIdentityRegistry(self.ctx))
        self.folders=SelectedFolders(self.ctx,self.permissions)
        self.tools=ProductTools(self.ctx,folders=self.folders)
        self.key=root_id(self.ctx.root)
    def mode(self,value):return self.permissions.set_file_access(value,**self.tools.file_range_operations())
    def test_explicit_range_allows_ordinary_read_write_and_keeps_destructive_confirmed(self):
        self.assertEqual(self.permissions.file_access()['mode'],'off')
        with self.assertRaises(PermissionError):self.folders.read(self.key,'sample.txt')
        self.mode('workspace')
        ticket=self.permissions.request_butler_operation('folder_write',{},session='s',job='j',provider_revision='p',known_operations=self.tools.tools)
        self.assertEqual(ticket['decision'],'allow')
        ticket=self.permissions.request_butler_operation('folder_delete',{'path':'sample.txt'},session='s',job='j',provider_revision='p',known_operations=self.tools.tools)
        self.assertEqual(ticket['decision'],'ask')
        self.assertEqual(self.permissions.butler_policies()['policies'].get('web_search','deny'),'deny')
    def test_text_file_write_recover_and_formal_writer_version(self):
        self.mode('workspace');(self.ctx.root/'sample.txt').write_text('old',encoding='utf-8')
        preview=self.folders.preview(self.key,'write','sample.txt',content='new')
        receipt=self.folders.apply(preview['preview_id']);self.assertEqual((self.ctx.root/'sample.txt').read_text(),'new')
        self.assertEqual(self.folders.apply(preview['preview_id']),receipt)
        self.folders.recover(preview['preview_id']);self.assertEqual((self.ctx.root/'sample.txt').read_text(),'old')
        (self.ctx.knowledge/'daily.md').write_text('old knowledge',encoding='utf-8')
        preview=self.folders.preview(self.key,'write','03_知识/daily.md',content='new knowledge')
        receipt=self.folders.apply(preview['preview_id']);self.assertIn('writer_version',receipt)
        self.assertEqual(self.folders.apply(preview['preview_id']),receipt)
        self.folders.recover(preview['preview_id']);self.assertEqual((self.ctx.knowledge/'daily.md').read_text(),'old knowledge')
    def test_off_revokes_old_preview_and_does_not_revive_legacy_allow(self):
        self.permissions.set_butler_policy('folder_write','allow',known_operations=self.tools.tools)
        self.mode('workspace');preview=self.folders.preview(self.key,'write','sample.txt',content='new')
        self.mode('off')
        self.assertEqual(self.permissions.butler_policies()['policies']['folder_write'],'deny')
        with self.assertRaises(PermissionError):self.folders.apply(preview['preview_id'])
        self.assertEqual(self.folders.public_grants(),[])
        self.assertFalse((self.ctx.root/'sample.txt').exists())
    def test_workspace_filters_old_external_grants_and_computer_narrowing_invalidates(self):
        external=Path(self.temp.name)/'external';external.mkdir();(external/'file.txt').write_text('old')
        old=self.folders.add(str(external))['id'];self.mode('workspace')
        self.assertNotIn(old,[row['folder_id'] for row in self.folders.public_grants()])
        with self.assertRaises(PermissionError):self.folders.read(old,'file.txt')
        with patch('knowledge_bridge.file_browser.local_drives',return_value=[external]):
            self.mode('computer');key=root_id(external);preview=self.folders.preview(key,'write','file.txt',content='new')
            self.mode('workspace')
            with self.assertRaises(PermissionError):self.folders.apply(preview['preview_id'])
        self.assertEqual((external/'file.txt').read_text(),'old')
    def test_raw_internal_and_credentials_stay_outside_ordinary_mutation(self):
        self.mode('workspace')
        for path in ['90_原始档案/original.txt','.jiedu/secret.txt','.ssh/id_rsa']:
            with self.assertRaises((PermissionError,ValueError)):self.folders.preview(self.key,'write',path,content='no')
    def test_owner_browser_has_roots_directory_pagination_and_model_filter(self):
        folder=self.ctx.root/'picker';folder.mkdir();(folder/'child').mkdir()
        (folder/'model.gguf').write_bytes(b'GGUF');(folder/'note.txt').write_text('text')
        browser=FileBrowser(self.ctx,drives=lambda:[])
        self.assertEqual(browser.browse()['items'][0]['path'],str(self.ctx.root))
        self.assertEqual([r['name'] for r in browser.browse(str(folder),kind='folder')['items']],['child'])
        models=browser.browse(str(folder),kind='model',limit=1);self.assertTrue(models['has_more'])
        self.assertEqual(browser.browse(str(folder),kind='model',offset=1)['items'][0]['name'],'model.gguf')
        with self.assertRaises((PermissionError,ValueError)):browser.browse(str(self.ctx.state))

    def test_tool_runner_ordinary_write_does_not_wait_for_per_operation_confirmation(self):
        self.mode('workspace')
        runner=ToolRunner(self.tools,self.permissions,session='fixture',job='fixture',provider={'selected':'local','revision':'fixture'},cancel=threading.Event(),provider_current=lambda:True)
        preview=runner.dispatch('folder_change_preview',json.dumps({'folder_id':self.key,'path':'ordinary.txt','action':'write','content':'saved through actual tool dispatch'}))
        self.assertTrue(preview['ok'],preview)
        result=runner.dispatch('folder_write',json.dumps({'preview_id':preview['result']['preview_id']}))
        self.assertTrue(result['ok'],result)
        self.assertEqual((self.ctx.root/'ordinary.txt').read_text(),'saved through actual tool dispatch')
        self.assertEqual(self.permissions.butler_policies()['pending'],[])

    def test_destructive_confirmation_cannot_grant_future_unrelated_objects(self):
        self.mode('workspace')
        self.permissions.set_butler_policy('folder_delete','allow',known_operations=self.tools.tools)
        ticket=self.permissions.request_butler_operation('folder_delete',{'path':'actual.txt'},session='s',job='j',provider_revision='p',known_operations=self.tools.tools)
        self.assertEqual(ticket['decision'],'ask')
        with self.assertRaises(ValueError):self.permissions.confirm_butler_operation(ticket['id'],approved=True,duration='persistent')
        self.permissions.confirm_butler_operation(ticket['id'],approved=True,duration='request')
        next_ticket=self.permissions.request_butler_operation('folder_delete',{'path':'other.txt'},session='s',job='j',provider_revision='p',known_operations=self.tools.tools)
        self.assertEqual(next_ticket['decision'],'ask')
