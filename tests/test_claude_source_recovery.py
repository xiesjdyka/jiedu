"""Synthetic per-message recovery and current Chromium external-blob selection."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from knowledge_bridge.claude_source_adapter import ClaudeSourceAdapter, _messages
from knowledge_bridge.doubao_indexeddb_source import _latest_records
from knowledge_bridge.workspace import initialize_workspace


def varint(n):
    result=bytearray()
    while n>=128:result.append((n&127)|128);n>>=7
    return bytes(result)+bytes([n])


def row(identity,sender='assistant',text='synthetic final'):
    return {'uuid':identity,'sender':sender,'stop_reason':'end_turn','content':[{'type':'text','text':text}]}


class ClaudeMessageRecoveryTests(unittest.TestCase):
    def test_empty_final_and_unknown_records_do_not_drop_valid_messages(self):
        rows=[row('user','human','synthetic question'),row('empty'),None,
            {'uuid':'unknown','sender':'assistant','stop_reason':'end_turn','content':{'text':'DO_NOT_GUESS'}},
            row('final')]
        rows[1]['content']=[];issues=[]
        messages=_messages({'messages':rows},diagnostics=issues)
        self.assertEqual(['user','final'],[m.message_id for m in messages])
        self.assertEqual({'empty_final_answer','unknown_message_structure','unknown_content_structure'},
            {i['reason'] for i in issues})
        self.assertTrue(all(i['status']=='needs_review' for i in issues))
        self.assertNotIn('DO_NOT_GUESS',json.dumps(issues))

    def test_thinking_tools_and_nonfinal_are_never_published_as_final(self):
        hidden='SYNTHETIC_INTERNAL_ONLY'
        draft=row('draft',text=hidden);draft['stop_reason']='tool_use'
        final=row('final');final['content']=[{'type':'thinking','thinking':hidden},
            {'type':'text','text':None},{'type':'tool_use','input':hidden},
            {'type':'tool_result','content':hidden},{'type':'text','text':'visible final'}]
        only_internal=row('no-final');only_internal['content']=[{'type':'thinking','thinking':hidden}]
        issues=[];messages=_messages({'messages':[draft,final,only_internal]},diagnostics=issues)
        self.assertEqual(['visible final'],[m.content for m in messages])
        self.assertEqual(['non_final_assistant','empty_final_answer'],[i['reason'] for i in issues])
        self.assertNotIn(hidden,repr(messages)+json.dumps(issues))

    def test_unknown_block_and_nonstring_role_are_reported_without_crashing(self):
        invalid=row('bad');invalid['content']=[{'type':['future'],'text':'DO_NOT_GUESS'}]
        unknown_sender=row('bad-role');unknown_sender['sender']={}
        issues=[];messages=_messages({'messages':[invalid,unknown_sender,row('valid')]},diagnostics=issues)
        self.assertEqual(['valid'],[m.message_id for m in messages])
        self.assertIn('unknown_content_block',{i['reason'] for i in issues})
        self.assertIn('unknown_sender',{i['reason'] for i in issues})

    def test_empty_record_can_recover_later_and_repeat_sync_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ctx=initialize_workspace(root/'vault');db=root/'leveldb';db.mkdir()
            (db/'CURRENT').write_text('MANIFEST-fixture');blob=root/'blob';blob.mkdir()
            tree={'messages':[row('user','human','synthetic question'),row('missing'),row('final')]}
            tree['messages'][1]['content']=[]
            with patch('knowledge_bridge.claude_source_adapter.profile_paths',return_value={}):
                adapter=ClaudeSourceAdapter(ctx,leveldb=db,blob=blob,reader=lambda:({'synthetic-session':tree},0,True))
            adapter.configure('selected',['synthetic-session'],owner_confirmed=True)
            first=adapter.sync();self.assertEqual(first['saved_this_sync'],2)
            self.assertIsNotNone(first['sessions']['synthetic-session']['last_error'])
            self.assertEqual(first['sessions']['synthetic-session']['message_issues'][0]['message_id'],'missing')
            self.assertEqual(adapter.sync()['saved_this_sync'],0)
            tree['messages'][1]=row('missing',text='restored final')
            after=adapter.sync();self.assertEqual(after['saved_this_sync'],1)
            self.assertEqual(after['sessions']['synthetic-session']['message_issues'],[])
            self.assertIsNone(after['sessions']['synthetic-session']['last_error'])
            self.assertIsNone(after['last_error']);self.assertEqual(adapter.sync()['saved_this_sync'],0)

    def test_conflicting_existing_id_keeps_original_and_does_not_block_later_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ctx=initialize_workspace(root/'vault');db=root/'leveldb';db.mkdir()
            (db/'CURRENT').write_text('MANIFEST-fixture');blob=root/'blob';blob.mkdir()
            tree={'messages':[row('fixed',text='original evidence')]}
            with patch('knowledge_bridge.claude_source_adapter.profile_paths',return_value={}):
                adapter=ClaudeSourceAdapter(ctx,leveldb=db,blob=blob,reader=lambda:({'session':tree},0,True))
            adapter.configure('selected',['session'],owner_confirmed=True)
            self.assertEqual(adapter.sync()['saved_this_sync'],1)
            tree['messages']=[row('fixed',text='conflicting later body'),row('later',text='normal later answer')]
            result=adapter.sync();self.assertEqual(result['saved_this_sync'],1)
            issue=result['sessions']['session']['message_issues'][0]
            self.assertEqual(issue['reason'],'archive_message_conflict');self.assertEqual(issue['message_id'],'fixed')
            raw=[json.loads(line) for p in ctx.raw_message_store.glob('*.jsonl') for line in p.read_text('utf-8').splitlines()]
            self.assertEqual({m['message_id']:m['original_text'] for m in raw},
                {'fixed':'original evidence','later':'normal later answer'})
            self.assertEqual(adapter.sync()['saved_this_sync'],0)

    def test_unknown_block_is_held_as_one_row_then_can_recover_without_partial_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ctx=initialize_workspace(root/'vault');db=root/'leveldb';db.mkdir()
            (db/'CURRENT').write_text('MANIFEST-fixture');blob=root/'blob';blob.mkdir()
            unknown=row('future',text='known fragment');unknown['content'].append({'type':'future','text':'DO_NOT_GUESS'})
            tree={'messages':[unknown,row('later')]}
            with patch('knowledge_bridge.claude_source_adapter.profile_paths',return_value={}):
                adapter=ClaudeSourceAdapter(ctx,leveldb=db,blob=blob,reader=lambda:({'session':tree},0,True))
            adapter.configure('selected',['session'],owner_confirmed=True)
            result=adapter.sync();self.assertEqual(result['saved_this_sync'],1)
            self.assertEqual(result['sessions']['session']['message_issues'][0]['reason'],'unknown_content_block')
            tree['messages'][0]=row('future',text='complete understood body')
            result=adapter.sync();self.assertEqual(result['saved_this_sync'],1)
            self.assertEqual(result['sessions']['session']['message_issues'],[])


class CurrentBlobTests(unittest.TestCase):
    def setUp(self):
        try:from ccl_chromium_reader import ccl_chromium_indexeddb as ccl
        except ImportError:self.skipTest('ccl_chromium_reader required')
        self.ccl=ccl;self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.blob=Path(self.temp.name)/'blob';(self.blob/'1/00').mkdir(parents=True)
        self.key=b'\x01\x01\x00k';self.prefix=ccl.IndexedDb.make_prefix(1,1,1)
        self.meta_prefix=ccl.IndexedDb.make_prefix(1,1,3)
        self.body=self.serialized('synthetic current')
        (self.blob/'1/00/2').write_bytes(self.body)

    def serialized(self,text):
        raw=text.encode();return b'\xff\x10\xff\x0fS'+varint(len(raw))+raw

    def raw(self,prefix,seq,value,*,file='000001.log',deleted=False):
        return NS(user_key=prefix+self.key,seq=seq,value=value,origin_file=file,
            state=NS(name='Deleted' if deleted else 'Live'))

    def meta(self,number,size):return b'\0'+varint(number)+b'\0'+varint(size)

    def store(self,*,decoded=False,deleted=False,unknown=False):
        body=self.body
        if unknown:
            body=b'\xff\x10\xff\x0f~';(self.blob/'1/00/2').write_bytes(body)
        value=varint(1)+b'\xff\x11\x01'+varint(len(body))+b'\0'
        current=self.raw(self.prefix,10,value)
        latest=self.raw(self.meta_prefix,12,self.meta(2,len(body)),deleted=deleted)
        old=self.raw(self.meta_prefix,2,self.meta(1,len(body)),file='000002.ldb')
        inactive=self.raw(self.meta_prefix,99,self.meta(99,len(body)),file='000003.ldb')
        records=[current,latest,old,inactive]
        raw=NS(_blob_dir=self.blob,_fetched_records=records,make_prefix=self.ccl.IndexedDb.make_prefix,
            read_record_precursor=lambda *a:None,
            _db=NS(manifest=[NS(deleted_files=(),new_files=[NS(file_no=2)],log_number=1,prev_log_number=0)]))
        class Store:
            _raw_db=raw;_dbid_no=1;_obj_store_id=1
            def iterate_records(inner,bad_deserializer_data_handler=None):
                if decoded:
                    yield NS(key=NS(raw_key=self.key),ldb_seq_no=10,value='WRONG_OLD_VALUE',is_live=True)
                else:bad_deserializer_data_handler(NS(raw_key=self.key),b'synthetic failure')
        return Store()

    def test_current_metadata_can_follow_value_seq_and_ignores_old_or_inactive(self):
        diagnostics=[];records,bad,old,verified=_latest_records(self.store(),diagnostics=diagnostics)
        self.assertTrue(verified);self.assertEqual(bad,0);self.assertEqual(old,0)
        self.assertEqual(records[0].value,'synthetic current')
        self.assertEqual(diagnostics[0]['blob_metadata_sequence'],12)
        self.assertEqual(diagnostics[0]['sequence'],10)
        self.assertEqual(diagnostics[0]['blob_number'],2)
        self.assertEqual(diagnostics[0]['status'],'recovered')

    def test_missing_current_blob_does_not_fall_back_to_old_decoded_value(self):
        (self.blob/'1/00/2').unlink();diagnostics=[]
        records,bad,_,_=_latest_records(self.store(decoded=True),diagnostics=diagnostics)
        self.assertEqual(records,[]);self.assertEqual(bad,1)
        self.assertEqual(diagnostics[0]['reason'],'missing_blob')

    def test_deleted_current_metadata_does_not_use_previous_live_blob(self):
        diagnostics=[];records,bad,_,_=_latest_records(self.store(deleted=True),diagnostics=diagnostics)
        self.assertEqual(records,[]);self.assertEqual(bad,1)
        self.assertEqual(diagnostics[0]['reason'],'blob_metadata_missing')

    def test_unsupported_serializer_is_distinct_from_missing_blob(self):
        diagnostics=[];records,bad,_,_=_latest_records(self.store(unknown=True),diagnostics=diagnostics)
        self.assertEqual(records,[]);self.assertEqual(bad,1)
        self.assertNotEqual(diagnostics[0]['reason'],'missing_blob')
        self.assertIn(diagnostics[0]['reason'],{'decode_error','unsupported_serialization'})

    def test_existing_blob_size_mismatch_is_not_claimed_missing_or_recovered(self):
        store=self.store();(self.blob/'1/00/2').write_bytes(b'short')
        diagnostics=[];records,bad,_,_=_latest_records(store,diagnostics=diagnostics)
        self.assertEqual(records,[]);self.assertEqual(bad,1)
        self.assertEqual(diagnostics[0]['reason'],'blob_size_mismatch')

    def test_recovered_current_failure_is_not_reclassified_as_historical_error(self):
        store=self.store()
        def legacy(bad_deserializer_data_handler=None):
            for _ in range(3):bad_deserializer_data_handler(NS(raw_key=self.key),b'synthetic failure')
            yield from ()
        store.iterate_records=legacy
        diagnostics=[];records,bad,historical,_=_latest_records(store,diagnostics=diagnostics)
        self.assertEqual(len(records),1);self.assertEqual(bad,0);self.assertEqual(historical,2)
        self.assertEqual(diagnostics[-1]['decode_errors'],2)

    def test_one_unknown_current_value_does_not_block_another_valid_current_value(self):
        store=self.store(unknown=True)
        other=self.raw(self.prefix,11,varint(1)+self.serialized('another valid current'))
        other.user_key=self.prefix+b'\x01\x01\x00z'
        store._raw_db._fetched_records.append(other)
        diagnostics=[];records,bad,_,_=_latest_records(store,diagnostics=diagnostics)
        self.assertEqual([r.value for r in records],['another valid current']);self.assertEqual(bad,1)
