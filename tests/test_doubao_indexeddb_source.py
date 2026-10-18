import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge.doubao_indexeddb_source import (
    CACHE_PUBLICATION_PROFILE, SnapshotBusyError, UnsupportedDoubaoSchema, consistent_snapshot,
    UnsafeAttachmentReference, extract_task_messages, fetch_signed_attachment, read_snapshot,
    redacted_attachment_reference,
)
from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.workspace import initialize_workspace


class Record:
    def __init__(self, key, value, seq=1, live=True):
        self.key=SimpleNamespace(value=key,raw_key=str(key).encode());self.value=value
        self.ldb_seq_no=seq;self.is_live=live


class Store:
    def __init__(self, rows):self.rows=rows
    def iterate_records(self, bad_deserializer_data_handler=None):yield from self.rows


class Database:
    def __init__(self, stores):self.stores=stores;self.object_store_names=stores.keys()
    def __getitem__(self,key):return self.stores[key]


class Wrapper:
    def __init__(self, databases):
        self.databases=databases
        self.database_ids=[SimpleNamespace(dbid_no=key) for key in databases]
        self.closed=False
    def __getitem__(self,key):return self.databases[key]
    def close(self):self.closed=True


def message(mid, cid, user_type, text, index):
    return {"extra":{"message_id":mid,"conversation_id":cid},"user_type":user_type,
            "index":index,"content_blocks_v2":[{"block_type":10000,
            "content":{"text_block":{"text":text}}},{"block_type":10040,
            "content":{"text_block":{"text":"private thinking"}}}]}


class DoubaoIndexedDBSourceTests(unittest.TestCase):
    def test_consistent_snapshot_and_busy_source_preserves_previous(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);level=root/'source';blob=root/'blob';dest=root/'snapshot'
            level.mkdir();blob.mkdir();(level/'CURRENT').write_text('MANIFEST-000001\n')
            (level/'MANIFEST-000001').write_bytes(b'm');(level/'000001.log').write_bytes(b'a')
            result=consistent_snapshot(level,blob,dest)
            self.assertEqual(b'a',(dest/'leveldb/000001.log').read_bytes())
            def mutate(_):
                with (level/'000001.log').open('ab') as handle:handle.write(b'x')
            with self.assertRaises(SnapshotBusyError):
                consistent_snapshot(level,blob,dest,attempts=2,after_copy=mutate,retry_delay=0)
            self.assertEqual(b'a',(dest/'leveldb/000001.log').read_bytes())
            self.assertEqual(1,result['attempts'])

    def test_snapshot_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);level=root/'source';blob=root/'blob';dest=root/'snapshot'
            level.mkdir();blob.mkdir();(level/'CURRENT').write_text('MANIFEST-000001\n')
            (level/'MANIFEST-000001').write_bytes(b'm');(level/'000001.log').write_bytes(b'a')
            calls=[]
            def transient(attempt):
                calls.append(attempt)
                if attempt==0:raise PermissionError('busy')
            result=consistent_snapshot(level,blob,dest,attempts=2,after_copy=transient,retry_delay=0)
            self.assertEqual(2,result['attempts']);self.assertEqual([0,1],calls)

    def test_extracts_known_user_and_keeps_assistant_closed_until_real_terminal_validation(self):
        running={"mainTaskDataMap":{"t1":{"stage":"sync","sentMessages":[message('u1','c1',1,'hello',1)],
                 "receivedMessages":[message('a1','c1',2,'partial',2)]}}}
        rows=extract_task_messages(running,10)
        self.assertEqual([('u1','user','hello')],[(x.message_id,x.role,x.content) for x in rows])
        finished={"mainTaskDataMap":{"t1":{"stage":"completed","sentMessages":[message('u1','c1',1,'hello',1)],
                  "receivedMessages":[message('a1','c1',2,'answer',2)]}}}
        rows=extract_task_messages(finished,11)
        self.assertEqual(['u1'],[x.message_id for x in rows])

        terminal=message('a2','c1',2,'verified terminal',2)
        terminal['extra'].update({'stage':4,'final_status':{'message':'Success'},'ext':{'is_finish':'1'}})
        rows=extract_task_messages({'mainTaskDataMap':{'t':{'receivedMessages':[terminal]}}},11)
        self.assertEqual(['a2'],[x.message_id for x in rows])

        unknown=message('x','c1',999,'hidden',3)
        unknown['content']='untyped-direct-string'
        unknown['content_blocks_v2']=[{'block_type':10040,'content':{'text_block':{'text':'private'}}}]
        state={'mainTaskDataMap':{'t':{'stage':'completed','receivedMessages':[unknown]}}}
        self.assertEqual([],extract_task_messages(state,12))

    def test_pure_attachment_message_preserves_identity_without_text(self):
        photo={'extra':{'message_id':'p1','conversation_id':'c1'},'user_type':1,'index':4,
               'content_blocks_v2':[{'block_type':10012,'content':{'image':{
                   'url':'https://example.invalid/signed-image','name':'photo.png'}}}]}
        rows=extract_task_messages({'mainTaskDataMap':{'t':{'sentMessages':[photo]}}},13)
        self.assertEqual(1,len(rows));self.assertEqual('',rows[0].content)
        self.assertEqual('https://example.invalid/signed-image',rows[0].attachments[0]['reference'])

    def test_signed_attachment_download_is_host_type_size_and_expiry_bounded(self):
        url='https://p11-flow-imagex-sign.byteimg.com/path/photo.png?x-signature=s&x-expires=9999999999'
        headers=Message();headers['Content-Type']='image/png';headers['Content-Length']='3'
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):return False
            def geturl(self):return url
            def read(self,_):return b'png'
        Response.headers=headers
        raw,mime,name=fetch_signed_attachment(url,opener=lambda *_args,**_kwargs:Response())
        self.assertEqual((b'png','image/png','photo.png'),(raw,mime,name))
        redacted=redacted_attachment_reference(url)
        self.assertNotIn('x-signature',redacted);self.assertIn('signed_reference_sha256=',redacted)
        expired='https://p11-flow-imagex-sign.byteimg.com/x?x-signature=s&x-expires=1'
        with self.assertRaises(UnsafeAttachmentReference):fetch_signed_attachment(expired,opener=lambda *_a,**_k:Response())
        hostile='https://example.com/x?x-signature=s&x-expires=9999999999'
        with self.assertRaises(UnsafeAttachmentReference):fetch_signed_attachment(hostile,opener=lambda *_a,**_k:Response())

    def test_latest_versions_titles_tombstones_and_unknown_schema(self):
        task=lambda text,stage='completed':{"state":json.dumps({"mainTaskDataMap":{"t":{"stage":stage,
             "sentMessages":[message('u','c',1,text,1)],"receivedMessages":[]}}})}
        stores={"store":Store([Record('samantha-web-super-task-store',task('old'),3),
                                Record('samantha-web-super-task-store',task('new'),5)]),
                "conversationListDataSnapshots":Store([
                    Record('overall',{"schema":"conversation-list-data-overall-v2",
                    "data":{"conversations":[{"conversation_id":"c","name":"Title","version":2}]}},4)])}
        wrapper=Wrapper({2:Database(stores)})
        result=read_snapshot(Path('.'),wrapper_factory=lambda *_:wrapper)
        self.assertEqual('new',result.messages[0].content)
        self.assertEqual('Title',result.titles['c'])
        self.assertTrue(wrapper.closed)

        tombstone=Wrapper({2:Database({'store':Store([
            Record('samantha-web-super-task-store',task('old'),3),
            Record('samantha-web-super-task-store',None,5,live=False)])})})
        self.assertEqual((),read_snapshot(Path('.'),wrapper_factory=lambda *_:tombstone).messages)
        cleared=Wrapper({2:Database({'store':Store([
            Record('samantha-web-super-task-store',task('old'),3),
            Record('samantha-web-super-task-store',{'state':json.dumps({'mainTaskDataMap':{}})},5)])})})
        self.assertEqual((),read_snapshot(Path('.'),wrapper_factory=lambda *_:cleared).messages)
        bad=Wrapper({3:Database({"conversationListDataSnapshots":Store([
            Record('overall',{"schema":"future-v99","data":{}},9)])})})
        with self.assertRaises(UnsupportedDoubaoSchema):
            read_snapshot(Path('.'),wrapper_factory=lambda *_:bad)

    def test_undecodable_effective_latest_value_never_falls_back_to_old_put(self):
        prefix=b'P';key='samantha-web-super-task-store'
        old=Record(key,{"state":json.dumps({"mainTaskDataMap":{"t":{"sentMessages":[
            message('u','c',1,'old',1)]}}})},3)
        raw_record=SimpleNamespace(user_key=prefix+key.encode(),seq=5,
            state=SimpleNamespace(name='Live'),origin_file='000001.log')
        edit=SimpleNamespace(deleted_files=(),new_files=(),log_number=1,prev_log_number=0)
        raw_level=SimpleNamespace(manifest=[edit])
        raw_obj=SimpleNamespace(_db=raw_level,_fetched_records=[raw_record],
            make_prefix=lambda *args:prefix)
        class BrokenLatestStore(Store):
            _raw_db=raw_obj;_dbid_no=2;_obj_store_id=1
            def iterate_records(self,bad_deserializer_data_handler=None):
                yield old
                bad_deserializer_data_handler(SimpleNamespace(raw_key=key.encode()),b'bad-current')
        wrapper=Wrapper({2:Database({'store':BrokenLatestStore([])})})
        result=read_snapshot(Path('.'),wrapper_factory=lambda *_:wrapper)
        self.assertEqual((),result.messages)
        self.assertEqual(1,result.current_decode_errors)
        self.assertFalse(result.current_state_verified)

    def test_materialized_conversation_shows_only_latest_source_revision(self):
        with tempfile.TemporaryDirectory() as root:
            context=initialize_workspace(Path(root)/'workspace');store=ArchiveStore(context)
            session=store.ensure_session(source_key='doubao_work',source_session_id='c',title='T',
                                         source_instance='fixture')
            for mid,text in [('base','old'),('base:v2','new')]:
                store.append_message(session['session_key'],message_id=mid,role='assistant',content=text,
                    provenance={'source_message_id':'source-a'},materialize=False)
            metadata=context.root/session['relative_path']/'03_元数据'
            (metadata/'message-latest.json').write_text(json.dumps({'latest_by_source_message':{
                'source-a':{'archive_message_id':'base:v2','source_sequence':2}}}),encoding='utf-8')
            store.materialize_session(session['session_key'])
            rendered=(context.root/session['relative_path']/'01_对话/conversation.md').read_text('utf-8')
            self.assertIn('new',rendered);self.assertNotIn('\nold\n',rendered)

    def test_reviewed_release_profile_is_the_single_persistent_publication_gate(self):
        held=read_snapshot(Path('.'),wrapper_factory=lambda *_:Wrapper({1:Database({})}))
        self.assertFalse(held.publication_ready)
        with patch('knowledge_bridge.doubao_indexeddb_source.VALIDATED_PUBLICATION_PROFILES',
                   frozenset({CACHE_PUBLICATION_PROFILE})):
            released=read_snapshot(Path('.'),wrapper_factory=lambda *_:Wrapper({1:Database({})}))
        self.assertTrue(released.publication_ready)


if __name__ == '__main__':unittest.main()
