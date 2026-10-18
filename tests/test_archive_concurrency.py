import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.workspace import initialize_workspace


class ArchiveConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.context=initialize_workspace(Path(self.temp.name)/'workspace')

    def test_independent_processes_and_instances_preserve_sources_messages_positions_and_manifests(self):
        worker=r'''
import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from knowledge_bridge.workspace import load_workspace
from knowledge_bridge.archive_store import ArchiveStore
context=load_workspace(sys.argv[2]);tag=sys.argv[3]
store=ArchiveStore(context);store.sources.register('process-'+tag,'Fixture','Fixture-'+tag,'fixture')
for i in range(6):
 store=ArchiveStore(context)
 own=store.ensure_session(source_key='process-'+tag,source_session_id=str(i),title='Fixture '+tag+' '+str(i))
 store.append_message(own['session_key'],message_id='own-'+tag+'-'+str(i),role='user',content='synthetic own '+tag+' '+str(i))
 common=store.ensure_session(source_key='codex',source_session_id='shared-fixture',title='Shared fixture')
 message='shared-'+tag+'-'+str(i)
 store.append_message(common['session_key'],message_id=message,role='user',content='synthetic shared '+tag+' '+str(i),materialize=False)
 store.record_message_position(common['session_key'],message,{'source_file_identity':'fixture-worker-'+tag,'source_object_id':'fixture-object','byte_offset':i*16,'byte_end':i*16+16})
store.materialize_session(common['session_key'])
print(json.dumps({'shared':common['session_key']}))
'''
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONIOENCODING']='utf-8'
        program=str(Path(__file__).resolve().parents[1]);processes=[]
        for tag in ('a','b'):
            processes.append(subprocess.Popen([sys.executable,'-c',worker,program,str(self.context.root),tag],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env))
        results=[]
        for process in processes:
            output,error=process.communicate(timeout=30);self.assertEqual(0,process.returncode,error);results.append(json.loads(output))
        store=ArchiveStore(self.context);registry=store._registry()
        self.assertEqual(13,len(registry['sessions']));self.assertEqual(results[0]['shared'],results[1]['shared'])
        self.assertEqual({'process-a','process-b'},set(store.sources._custom()))
        shared=results[0]['shared'];rows=[json.loads(line) for line in (self.context.raw_message_store/(shared+'.jsonl')).read_text('utf-8').splitlines()]
        self.assertEqual(12,len(rows));self.assertEqual(12,len({row['message_id'] for row in rows}))
        item=store.session(shared);positions=store._position_map(item);self.assertEqual(12,len(positions))
        manifest=self.context.root/item['relative_path']/'03_元数据'/'manifest.json'
        json.loads(manifest.read_text('utf-8'));self.assertFalse(list(manifest.parent.glob('.manifest.json.*.tmp')))
        # Validate the actual materialized content without assuming a guessed filename.
        bodies='\n'.join(path.read_text('utf-8') for path in (self.context.root/item['relative_path']).rglob('conversation.md'))
        for tag in ('a','b'):
            for index in range(6):self.assertIn('synthetic shared '+tag+' '+str(index),bodies)

    def test_recovery_constructor_waits_for_active_manifest_writer(self):
        store=ArchiveStore(self.context);session=store.ensure_session(source_key='codex',source_session_id='recovery-fixture',title='Fixture')
        started=threading.Event();release=threading.Event();recovered=threading.Event();errors=[];temporary=[]
        def delayed_manifest(path,value):
            path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name('.manifest.json.'+uuid.uuid4().hex+'.tmp')
            temp.write_text(json.dumps(value),'utf-8');temporary.append(temp);started.set();release.wait(3);os.replace(temp,path)
        def write():
            try:store.append_message(session['session_key'],message_id='fixture-message',role='user',content='synthetic recovery content')
            except Exception as error:errors.append(error)
        def recover():
            try:ArchiveStore(self.context);recovered.set()
            except Exception as error:errors.append(error)
        with patch.object(store,'_atomic_manifest',side_effect=delayed_manifest):
            writer=threading.Thread(target=write);writer.start();self.assertTrue(started.wait(2))
            reader=threading.Thread(target=recover);reader.start();self.assertFalse(recovered.wait(.1))
            self.assertTrue(temporary[0].exists());release.set();writer.join(2);reader.join(2)
        self.assertEqual([],errors);self.assertTrue(recovered.is_set());self.assertFalse(temporary[0].exists())


if __name__=='__main__':unittest.main()
