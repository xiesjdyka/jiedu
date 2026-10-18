"""Explicit image archive preserves independent originals after Butler deletion.

The runner redirects control/vault/TEMP before importing tests. Fixtures live in
TemporaryDirectory; no model, credential store, real workspace or OCR is used.
"""
import ast
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.archive_store import ArchiveStore

# Exercise the exact archive handler without importing unrelated indexing/Writer
# modules. The attachment store, archive store and Butler database are real.
SOURCE=Path(__file__).resolve().parents[1]
tool_path=SOURCE/'knowledge_bridge/butler_tools.py'
tree=ast.parse(tool_path.read_text('utf-8'))
product=next(node for node in tree.body if getattr(node,'name','')=='ProductTools')
method=next(node for node in product.body if getattr(node,'name','')=='_archive_conversation')
budget=next(node for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='ARCHIVE_IMAGE_BUDGET' for target in node.targets))
HANDLER={'hashlib':hashlib,'__name__':'knowledge_bridge.butler_tools','__package__':'knowledge_bridge'}
exec(compile(ast.Module(body=[budget,method],type_ignores=[]),str(tool_path),'exec'),HANDLER)


def fixture_png(color=(255,0,0)):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+bytes(color)))+chunk(b'IEND',b'')


class SyntheticProviders:
    def __init__(self):self.on_change=[]
    def snapshot(self):
        return {'selected':'external','revision':'synthetic','external':{'base_url':'https://api.deepseek.com',
            'dialect':'deepseek','model':'deepseek-flash','context_tokens':8192,'max_output_tokens':1024,
            'thinking_mode':'switchable','thinking':True,'tools':False,'data_scopes':[],'vision_supported':True}}


class ButlerAttachmentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='jiedu-butler-archive-')
        self.addCleanup(self.temporary.cleanup)
        self.context=initialize_workspace(Path(self.temporary.name)/'workspace')
        runtime=SimpleNamespace(config=lambda:{},status=lambda:{'state':'not_configured'},close=lambda:None)
        self.service=LocalButler(self.context,providers=SyntheticProviders(),runtime=runtime,start_worker=False)
        self.addCleanup(self.service.close)
        self.sid=self.service.create_session()['id']
        self.original=fixture_png()
        self.metadata=self.service.attachments.save(self.sid,'synthetic.png',self.original)
        self.jid=self.service.submit(self.sid,'',attachments=[self.metadata['id']])['id']
        self.holder=SimpleNamespace(context=self.context,conversation=self.service.conversation)
        self.check=lambda:None;self.check.session=self.sid
    def archive(self):return HANDLER['_archive_conversation'](self.holder,{},self.check)
    def no_archived_session(self):self.assertEqual(ArchiveStore(self.context)._registry().get('sessions',{}),{})
    def test_image_only_explicit_archive_is_idempotent_and_survives_permanent_delete(self):
        self.no_archived_session()
        result=self.archive();key=result['session']['session_key'];store=ArchiveStore(self.context)
        rows=store.attachment_status(key)
        self.assertEqual(result['attachment_count'],1);self.assertTrue(result['attachments_complete'])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['sha256'],hashlib.sha256(self.original).hexdigest())
        self.assertEqual(rows[0]['source_reference'],'butler-attachment:'+self.metadata['id'])
        stored=self.context.root/rows[0]['stored_path'];self.assertEqual(stored.read_bytes(),self.original)
        raw=self.context.raw_message_store/(key+'.jsonl')
        raw_rows=[json.loads(line) for line in raw.read_text('utf-8').splitlines()]
        self.assertEqual(raw_rows[0]['original_text'],'')
        self.assertEqual(rows[0]['source_message_id'],raw_rows[0]['message_id'])
        folder=self.context.root/result['session']['relative_path']
        ledger=folder/'03_元数据'/'attachments.jsonl';before=(raw.read_bytes(),ledger.read_bytes())
        repeated=self.archive();self.assertEqual(repeated['saved'],0);self.assertEqual(repeated['duplicates'],1)
        self.assertEqual((raw.read_bytes(),ledger.read_bytes()),before)
        original_path=self.service.attachments.original(self.sid,self.metadata['id'])
        title=self.service.conversation(self.sid)['session']['title']
        self.service.delete_session(self.sid,confirmed=True,expected_title=title)
        self.assertFalse(original_path.exists());self.assertTrue(raw.is_file())
        self.assertEqual(stored.read_bytes(),self.original)
        self.assertEqual(store.attachment_status(key)[0]['sha256'],hashlib.sha256(self.original).hexdigest())
    def test_missing_original_preflight_does_not_create_archive_session(self):
        self.service.attachments.original(self.sid,self.metadata['id']).unlink()
        with self.assertRaises(ValueError):self.archive()
        self.no_archived_session()
    def test_tampered_original_preflight_does_not_create_archive_session(self):
        self.service.attachments.original(self.sid,self.metadata['id']).write_bytes(b'not-an-image')
        with self.assertRaises(ValueError):self.archive()
        self.no_archived_session()
    def test_replaced_original_and_live_metadata_cannot_override_frozen_message(self):
        data=fixture_png((0,0,255))
        blob,metadata=self.service.attachments._paths(self.sid,self.metadata['id'])
        value=json.loads(metadata.read_text('utf-8'));blob.write_bytes(data)
        value['sha256']=hashlib.sha256(data).hexdigest();value['bytes']=len(data)
        self.service.attachments._write_metadata(metadata,value)
        with self.assertRaisesRegex(ValueError,'已发送记录'):self.archive()
        self.no_archived_session()
    def test_unbound_attachment_is_not_archived(self):
        _,metadata=self.service.attachments._paths(self.sid,self.metadata['id'])
        value=json.loads(metadata.read_text('utf-8'));value['job_ids']=[]
        self.service.attachments._write_metadata(metadata,value)
        with self.assertRaisesRegex(ValueError,'已发送记录'):self.archive()
        self.no_archived_session()
    def test_image_budget_rejects_whole_archive_and_preserves_original(self):
        with patch.dict(HANDLER,{'ARCHIVE_IMAGE_BUDGET':len(self.original)-1}):
            with self.assertRaisesRegex(ValueError,'未执行归档'):self.archive()
        self.no_archived_session()
        self.assertEqual(self.service.attachments.original(self.sid,self.metadata['id']).read_bytes(),self.original)
    def test_existing_text_archive_gets_missing_image_reference_on_retry(self):
        current=self.service.conversation(self.sid);row=current['messages'][0];store=ArchiveStore(self.context)
        session=store.ensure_session(source_key='owner-butler',source_session_id=self.sid,title=current['session']['title'],
            source_instance=self.context.workspace_id,product_family='界渡管家',session_source='owner-butler',client_type='butler',acquisition_method='owner_authorized_saved_conversation')
        store.append_message(session['session_key'],message_id=str(row['id']),role=row['role'],content='',timestamp=row['created'])
        self.assertEqual(store.attachment_status(session['session_key']),[])
        result=self.archive();self.assertEqual(result['saved'],0);self.assertEqual(result['duplicates'],1)
        self.assertEqual((self.context.root/store.attachment_status(session['session_key'])[0]['stored_path']).read_bytes(),self.original)


if __name__=='__main__':unittest.main()
