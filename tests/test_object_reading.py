import hashlib
import io
import os
import tempfile
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.document_converter import DocumentConverter
from knowledge_bridge.knowledge_objects import KnowledgeObjectService
from knowledge_bridge.object_reader import ObjectReadError
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider, ProtectedIntegrityError
from knowledge_bridge.storage import StorageAuthorization, StorageOperation
from knowledge_bridge.workspace import initialize_workspace
from fixture_documents import docx,xlsx,pdf

AUTH=StorageAuthorization.allow(StorageOperation.WRITE_ORIGINAL,StorageOperation.READ,StorageOperation.VERIFY)


class ObjectReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jiedu-reading-unit-')
        self.root=Path(self.temp.name);self.context=initialize_workspace(self.root/'workspace')
        self.provider=EncryptedProtectedStorageProvider(self.context)
        self.provider.initialize('fictional-reading-password',kdf_parameters={'name':'argon2id','iterations':2,'lanes':1,'memory_cost_kib':19456,'length':32})
        self.service=KnowledgeObjectService(self.context,self.provider)

    def tearDown(self):self.provider.lock();self.temp.cleanup()

    def row(self,data):
        self.provider.write_original('fixture',data,AUTH)
        return {'entry_id':'fixture','content_object':'fixture','policy':'protected','readable':True,'logical_path':'fixture.md'}

    def test_chinese_utf8_utf16_pages_are_exact_and_cursors_bind_session(self):
        content='中文跨页🙂\n行尾。'*200
        for encoding in ('utf-8-sig','utf-16'):
            with self.subTest(encoding=encoding):
                data=content.encode(encoding)
                object_id=encoding
                self.provider.write_original(object_id,data,AUTH)
                row={'entry_id':encoding,'content_object':object_id,'policy':'protected','readable':True,'logical_path':'fixture.md'}
                cursor=None;pieces=[];position=0
                while True:
                    page=self.service.reader.page(row,mode='text',cursor=cursor,binding='epoch-a',page_bytes=31)
                    self.assertEqual(position,page['offset']);position+=page['returned_bytes'];pieces.append(page['content'])
                    if page['complete']:break
                    cursor=page['next_cursor']
                self.assertEqual(content,''.join(pieces));self.assertEqual(len(data),position)
                with self.assertRaises(ObjectReadError):self.service.reader.page(row,mode='text',cursor=cursor,binding='epoch-b')

    def test_large_range_reads_only_intersecting_chunks_without_temp_plaintext(self):
        data=('ReadableFixture742中文🙂\n'*160000).encode()
        self.assertGreater(len(data),2*1024*1024)
        row=self.row(data)
        with patch('knowledge_bridge.protected_storage.tempfile.TemporaryFile',side_effect=AssertionError('plaintext tempfile')):
            self.assertEqual(data,self.provider.read_object('fixture',AUTH))
            ref=self.provider._reference_from_folder(self.provider._object('fixture')[1],self.provider._object('fixture')[0])
            self.assertTrue(self.provider.verify_integrity(ref,AUTH))
            with patch.object(self.provider,'_verified_chunk',wraps=self.provider._verified_chunk) as chunks:
                response=self.provider.read_range('fixture',AUTH,offset=2*1024*1024+10,length=32768)
                self.assertEqual(1,chunks.call_count)
                self.assertEqual(data[2*1024*1024+10:2*1024*1024+32778],response['data'])

    def test_open_file_change_and_invalid_utf8_are_rejected(self):
        path=self.context.inbox/'sample.txt';path.write_text('中文'*400,'utf-8')
        os.utime(path,(time.time()-60,time.time()-60))
        row=next(r for r in self.service._open_items() if r['title']=='sample.txt')
        page=self.service.reader.page(row,mode='text',binding='owner',page_bytes=40)
        path.write_text('另一版本'*400,'utf-8')
        with self.assertRaisesRegex(ObjectReadError,'版本'):self.service.reader.page(row,mode='text',binding='owner',cursor=page['next_cursor'])
        invalid=self.row(b'\xffabc')
        with self.assertRaisesRegex(ObjectReadError,'编码'):self.service.reader.page(invalid,mode='text')

    def test_range_tamper_and_changed_version_never_return_bytes(self):
        row=self.row(b'abc'*1000000)
        first=self.provider.read_range('fixture',AUTH,length=100)
        with self.assertRaisesRegex(ValueError,'版本'):self.provider.read_range('fixture',AUTH,version='different')
        _,folder,_=self.provider._object('fixture');chunk=folder/'chunks'/'00000000.bin'
        raw=bytearray(chunk.read_bytes());raw[-1]^=1;chunk.write_bytes(raw)
        with self.assertRaises(ProtectedIntegrityError):self.provider.read_range('fixture',AUTH,length=100,version=first['version'])

    def test_memory_conversion_and_protected_import_provenance(self):
        source=self.root/'source';source.mkdir();marker='ReadableFixture742'
        samples={'sample.docx':docx(),'sample.xlsx':xlsx(),'text.pdf':pdf(),'scan.pdf':pdf(scan=True),'broken.docx':b'broken','unknown.bin':b'binary'}
        for name,data in samples.items():(source/name).write_bytes(data)
        with patch('knowledge_bridge.protected_storage.tempfile.TemporaryFile',side_effect=AssertionError('plaintext tempfile')):
            preview=self.service.preview_import(str(source),'protected')
            result=self.service.execute_import(preview['preview_id'],owner_confirmed=True)
        self.assertEqual(3,result['conversion']['counts']['converted'])
        self.assertEqual(1,result['conversion']['counts']['partial'])
        self.assertEqual(1,result['conversion']['counts']['failed'])
        rows=self.service._catalog();derived=[r for r in rows if r.get('source_entry_id')]
        self.assertEqual(4,len(derived))
        for row in derived:
            original=next(r for r in rows if r['entry_id']==row['source_entry_id'])
            self.assertIn(row['entry_id'],original['derived_entry_ids'])
            self.assertEqual(original['sha256'],row['source_sha256'])
            text=self.service.reader.page(row,mode='text')['content']
            if original['title']=='scan.pdf':
                self.assertIn('symbol_only_candidate',text)
                self.assertFalse(row['conversion']['metadata']['coverage_complete'])
            else:self.assertIn(marker,text)
        repeated=self.service.execute_import(self.service.preview_import(str(source),'protected')['preview_id'],owner_confirmed=True)
        self.assertEqual(5,repeated['conversion']['counts']['already_imported'])
        self.assertEqual(1,repeated['conversion']['counts']['partial'])
        self.assertEqual(10,len(self.service._catalog()))
        for path in self.context.root.rglob('*'):
            if path.is_file():self.assertNotIn(marker.encode(),path.read_bytes())
        self.assertTrue(all((source/name).read_bytes()==data for name,data in samples.items()))

    def test_revoke_and_lock_do_not_wait_for_slow_read_and_discard_result(self):
        from knowledge_bridge import web
        from knowledge_bridge.protection_control import ProtectionLifecycleManager
        from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
        from fastapi import HTTPException
        manager=ProtectionLifecycleManager(self.context,settings_path=self.root/'control.json')
        manager.unlock_with_passphrase('fictional-reading-password')
        registry=ClientIdentityRegistry(self.context,self.root/'identities');permissions=ClientPermissionService(registry)
        client=registry.create('fixture','Fixture','test','stdio')
        service=KnowledgeObjectService(self.context,manager.provider)
        source=self.root/'outside';source.mkdir();(source/'test.md').write_text('中文正文'*50000,'utf-8')
        service.execute_import(service.preview_import(str(source),'protected')['preview_id'],owner_confirmed=True)
        row=service._catalog()[0];payload=web.ClientObjectQuery(entry_id=row['entry_id'],mode='text')
        for action in ('revoke','lock'):
            with self.subTest(action=action):
                permissions.grant_session_read(client['internal_id'],manager.protection_session_id,['protected'])
                entered=threading.Event();resume=threading.Event();original=service.reader._raw
                def slow(*args,**kwargs):
                    value=original(*args,**kwargs);entered.set();resume.wait(5);return value
                with patch.object(web,'_protection_manager',return_value=manager),patch.object(web,'_client_services',return_value=(registry,permissions)),patch.object(web,'_knowledge_object_services',return_value=(service,None)),patch.object(web,'_http_client_identity',side_effect=lambda _:registry.authenticate('fixture',client['secret'])),patch.object(service.reader,'_raw',side_effect=slow):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(web._client_object_operation,None,payload,False)
                        self.assertTrue(entered.wait(5));start=time.monotonic()
                        if action=='lock':manager.lock()
                        else:permissions.grant_session_read(client['internal_id'],manager.protection_session_id,[])
                        self.assertLess(time.monotonic()-start,.5)
                        resume.set()
                        with self.assertRaises(HTTPException) as error:future.result(timeout=5)
                        self.assertIn(error.exception.status_code,(401,403,423))

    def test_budgets_reject_oversized_files_and_office_expansion(self):
        from fixture_documents import _zip
        path=self.context.inbox/'huge.md'
        with path.open('wb') as handle:handle.truncate(64*1024*1024+1)
        row=next(r for r in self.service._open_items() if r['title']=='huge.md')
        with self.assertRaisesRegex(ObjectReadError,'64 MiB'):self.service.reader.page(row,mode='text')
        oversized=b'a'*(16*1024*1024+1)
        self.assertEqual('size_limit',DocumentConverter().convert_bytes(oversized,'.txt').status)
        expanded=_zip({'word/document.xml':b'a'*(32*1024*1024+1)})
        self.assertEqual('size_limit',DocumentConverter().convert_bytes(expanded,'.docx').status)


if __name__=='__main__':unittest.main()
