import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_bridge.durable_state import StateCorruptError, read_object, write_object
from knowledge_bridge.codex_source_adapter import CodexSourceAdapter, status_summary
from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.workspace import initialize_workspace


class StateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = initialize_workspace(self.root/'workspace')
        self.home = self.root/'codex'
        (self.home/'sessions').mkdir(parents=True)
        self.machine = self.root/'machine'

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self):
        return CodexSourceAdapter(self.context, codex_home=self.home, machine_data=self.machine)

    def test_corrupt_primary_blocks_sync_configuration_and_reports_recovery(self):
        adapter = self.adapter()
        adapter.configure(policy='all', owner_confirmed=True)
        backup = adapter.state_path.with_name(adapter.state_path.name+'.last-good')
        good = backup.read_bytes()
        adapter.state_path.write_bytes(b'\0'*300)
        with self.assertRaises(StateCorruptError): adapter.sync()
        with self.assertRaises(StateCorruptError): adapter.configure(enabled=True, owner_confirmed=True)
        summary = status_summary(self.context)
        self.assertTrue(summary['recovery_required'])
        self.assertIsNone(summary['archive_policy'])
        self.assertEqual(b'\0'*300, adapter.state_path.read_bytes())
        self.assertEqual(good, backup.read_bytes())

    def test_missing_primary_with_backup_never_reinitializes(self):
        adapter=self.adapter(); adapter.configure(policy='all',owner_confirmed=True)
        adapter.state_path.unlink()
        with self.assertRaises(StateCorruptError):adapter.sync()

    def test_corrupt_registry_and_instance_never_create_new_identity(self):
        adapter=self.adapter(); adapter._instance_id()
        adapter.instance_path.write_text('{}',encoding='utf-8')
        with self.assertRaises(StateCorruptError):adapter._instance_id()
        self.context.archive_registry.write_bytes(b'\0'*20)
        with self.assertRaises(StateCorruptError):ArchiveStore(self.context)
        self.assertTrue(status_summary(self.context)['recovery_required'])

    def test_failed_primary_replace_leaves_readable_primary_and_backup(self):
        path=self.root/'state.json';write_object(path,{'offset':12})
        import knowledge_bridge.durable_state as durable
        original=durable.os.replace
        def fail_primary(src,dst):
            if Path(dst)==path:raise PermissionError('fixture')
            return original(src,dst)
        with mock.patch.object(durable.os,'replace',side_effect=fail_primary),mock.patch.object(durable.time,'sleep'):
            with self.assertRaises(PermissionError):write_object(path,{'offset':99})
        self.assertEqual(12,read_object(path)['offset'])
        self.assertEqual(12,read_object(path.with_name('state.json.last-good'))['offset'])

    def test_resume_restart_and_repeat_do_not_duplicate_messages_or_attachments(self):
        path=self.home/'sessions/rollout-test.jsonl'
        def line(v):return (json.dumps(v)+'\n').encode()
        def message(text):return {'timestamp':'2026-10-01T00:00:00Z','type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}}
        path.write_bytes(line({'type':'session_meta','payload':{'id':'recovery-test'}})+line(message('first')))
        adapter=self.adapter();adapter.configure(policy='all',owner_confirmed=True)
        first=adapter.sync();self.assertEqual(1,first['saved_messages'])
        saved=adapter.state_path.read_bytes()
        adapter.state_path.write_bytes(b'\0'*len(saved))
        with self.assertRaises(StateCorruptError):adapter.sync()
        # Explicitly verified restoration, never an automatic cursor rollback.
        adapter.state_path.write_bytes(saved)
        with path.open('ab') as f:f.write(line(message('second')))
        restarted=self.adapter();self.assertEqual(1,restarted.sync()['saved_messages'])
        self.assertEqual(0,self.adapter().sync()['saved_messages'])
        registry=read_object(self.context.archive_registry)['sessions']
        self.assertEqual(1,len(registry));entry=next(iter(registry.values()))
        raw=self.context.raw_message_store/(entry['session_key']+'.jsonl')
        ids=[json.loads(line)['message_id'] for line in raw.read_bytes().splitlines()]
        self.assertEqual(2,len(ids));self.assertEqual(2,len(set(ids)))

    def test_audit_omits_snapshot_and_rotates(self):
        adapter=self.adapter();adapter.audit_path.parent.mkdir(parents=True,exist_ok=True)
        adapter.audit_path.write_bytes(b'x'*(8*1024*1024))
        adapter._audit({'status':'synced','saved_messages':1,'adapter_status':{'private':'large'},'sessions':[]})
        self.assertTrue(adapter.audit_path.with_name('codex-sync.previous.jsonl').exists())
        row=json.loads(adapter.audit_path.read_text('utf-8'))
        self.assertNotIn('adapter_status',row)

    def test_larger_repair_pages_preserve_messages_across_internal_records(self):
        adapter=self.adapter();adapter.read_chunk_bytes=adapter.max_event_bytes+1
        adapter.page_bytes=8*1024*1024;adapter.processing_budget_seconds=2
        def line(v):return (json.dumps(v)+'\n').encode()
        events=[{'type':'session_meta','payload':{'id':'bulk-repair'}}]
        for i in range(8):
            events.append({'type':'response_item','payload':{'type':'custom_tool_call_output','output':'x'*80000}})
            events.append({'type':'response_item','timestamp':'2026-10-01T00:00:00Z','payload':{'type':'message','id':f'm{i}','role':'user','content':[{'type':'input_text','text':str(i)}]}})
        path=self.home/'sessions/rollout-bulk.jsonl';path.write_bytes(b''.join(map(line,events)))
        adapter.configure(policy='all',owner_confirmed=True)
        for _ in range(5):adapter.sync()
        row=next(iter(adapter.store._registry()['sessions'].values()))
        raw=self.context.raw_message_store/(row['session_key']+'.jsonl')
        ids=[json.loads(line)['message_id'] for line in raw.read_bytes().splitlines()]
        self.assertEqual([f'm{i}' for i in range(8)],ids)
        self.assertEqual(0,adapter.sync()['saved_messages'])


if __name__=='__main__':unittest.main()
