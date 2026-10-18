import hashlib
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from knowledge_bridge import evidence_index as index
from knowledge_bridge.evidence_storage import maintain, reader_guard
from knowledge_bridge.index_gate import IndexWriteGate
from knowledge_bridge.workspace import initialize_workspace


class EvidenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=initialize_workspace(Path(self.tmp.name)/'workspace')
        self.file=self.context.raw_archive/'fictional.md';self.file.write_text('fixture 0','utf-8')
        self.keys=list(range(300));self.text={};self.root=self.context.index_root('evidence')
        self.embed=patch('knowledge_bridge.core.embed',side_effect=lambda texts,**kw: np.ones((len(texts),256),dtype='float32'))
        for mock in (self.embed,patch('knowledge_bridge.core.local_model_status',return_value={}),
                     patch('knowledge_bridge.core.configured_embedding_duty_cycle',return_value=1),
                     patch('knowledge_bridge.search_scopes.files_for_scope',side_effect=lambda *args: [self.file] if self.file.exists() else []),
                     patch.object(index,'rows_for_file',side_effect=self.rows)):
            mock.start();self.addCleanup(mock.stop)
        index.rebuild(self.context)

    def rows(self,path,context):
        raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        return raw,[dict(source_path=context.relative(path),document_title='Synthetic',section_title='Synthetic',
            chunk_id=f'fictional-{i}',content=self.text.get(i,('fictional %s '%i)*70),
            embedding_text=self.text.get(i,'embedding %s'%i),vector_cache_key=self.text.get(i,'key-%s'%i),
            source_version=digest,modified_at=str(path.stat().st_mtime_ns),session_id='synthetic',message_id=str(i),
            role='USER',message_order=i,chunk_index_in_message=0,source_type='raw_chat') for i in self.keys]

    def change(self):
        self.file.write_text('fictional revision '+str(time.time_ns()),'utf-8')

    def sync(self):
        return index.sync(context=self.context,lock_timeout=2)

    def disk(self):
        return {str(p.relative_to(self.root)):p.stat().st_size for p in self.root.rglob('*') if p.is_file()}

    def test_sixty_updates_write_only_delta_and_bound_retained_storage(self):
        before=self.disk();peak=sum(before.values());written=0;seen=set(before)
        name=index._descriptor(self.context)['table']
        for i in range(60):
            self.keys.append(300+i);self.change();result=self.sync()
            self.assertEqual('chunk_delta',result['storage_mode'])
            self.assertEqual(1,result['written_chunks'])
            self.assertEqual(name,index._descriptor(self.context)['table'])
            now=self.disk();written+=sum(v for k,v in now.items() if k not in seen);seen.update(now)
            if i%10==9:
                with IndexWriteGate(self.context).hold('test_maintenance'):
                    maintain(self.context,index._descriptor(self.context),force=True,safety_margin=1)
                now=self.disk();written+=sum(v for k,v in now.items() if k not in seen);seen.update(now)
            peak=max(peak,sum(self.disk().values()))
        self.assertEqual(360,index.table(self.context).count_rows())
        rows=index.table(self.context).search().select(['chunk_id']).to_arrow().to_pylist()
        self.assertEqual(360,len({r['chunk_id'] for r in rows}))
        self.assertEqual(1,len(list((self.root/'lancedb').glob('*.lance'))))
        self.assertLess(len(index.table(self.context).list_versions()),30)
        self.assertLess(written,sum(before.values())*15)  # old code writes 60 full copies
        self.assertLess(peak,sum(before.values())*8)
        # A repeated sync neither writes vectors nor creates a new state snapshot.
        pointer=(self.root/'active-vector.json').read_bytes();files=self.disk()
        for _ in range(5):self.assertEqual('unchanged',self.sync()['status'])
        self.assertEqual(pointer,(self.root/'active-vector.json').read_bytes())
        self.assertEqual(files,self.disk())
        print(json.dumps({'storage_fixture_initial_bytes':sum(before.values()),'sixty_updates_new_file_bytes':written,'peak_bytes':peak,'final_bytes':sum(files.values()),'rows':360}))

    def test_failed_publish_is_invisible_and_restart_resumes_without_duplicates(self):
        old=index._descriptor(self.context);self.keys.remove(0);self.text[1]='edited synthetic';self.keys.append(301);self.change()
        original=index._atomic
        def fail(path,value):
            if path.name=='active-vector.json':raise OSError('synthetic interruption before publish')
            return original(path,value)
        with patch.object(index,'_atomic',side_effect=fail):
            with self.assertRaises(OSError):self.sync()
        self.assertEqual(old,index._descriptor(self.context))
        self.assertEqual(300,index.table(self.context).count_rows())
        ids={r['chunk_id'] for r in index.table(self.context).search().select(['chunk_id']).to_arrow().to_pylist()}
        self.assertIn('fictional-0',ids);self.assertNotIn('fictional-301',ids)
        # New table connections recover using durable descriptor, no process memory.
        result=self.sync();self.assertEqual('chunk_delta',result['storage_mode'])
        ids=[r['chunk_id'] for r in index.table(self.context).search().select(['chunk_id']).to_arrow().to_pylist()]
        self.assertEqual(300,len(ids));self.assertEqual(300,len(set(ids)))
        self.assertNotIn('fictional-0',ids);self.assertIn('fictional-301',ids)
        self.file.unlink();result=self.sync();self.assertEqual(1,result['deleted'])
        self.assertEqual(0,index.table(self.context).count_rows())

    def test_legacy_migration_only_once_and_query_uses_current_file_hash(self):
        descriptor=index._descriptor(self.context);descriptor['version']=1;descriptor.pop('table_version')
        index._atomic(self.root/'active-vector.json',descriptor)
        self.keys.append(301);self.change();first=self.sync()
        self.assertEqual('initial_or_legacy_migration',first['storage_mode'])
        self.keys.append(302);self.change();second=self.sync()
        self.assertEqual('chunk_delta',second['storage_mode']);self.assertEqual(1,second['written_chunks'])
        self.assertEqual(302,index.table(self.context).count_rows())
        digest=hashlib.sha256(self.file.read_bytes()).hexdigest()
        found=index.vector_rows([1.]*256,limit=20,context=self.context)
        self.assertEqual(20,len(found));self.assertTrue(all(row['source_version']==digest for row in found))

    def test_unchanged_legacy_upgrade_once_and_ignores_directory_metadata_events(self):
        descriptor=index._descriptor(self.context);descriptor['version']=1;descriptor.pop('table_version')
        index._atomic(self.root/'active-vector.json',descriptor)
        self.assertEqual('unchanged',self.sync()['status'])
        upgraded=index._descriptor(self.context);self.assertEqual(2,upgraded['version'])
        self.assertNotEqual(descriptor['table'],upgraded['table'])
        self.sync();self.assertEqual(upgraded,index._descriptor(self.context))
        from watchdog.events import DirModifiedEvent,DirMovedEvent
        from knowledge_bridge import watcher
        with patch.object(watcher,'VAULT',self.context.root),patch('knowledge_bridge.evidence_updates.enqueue') as enqueue:
            handler=watcher.Handler();handler.on_any_event(DirModifiedEvent(str(self.context.raw_archive)))
            enqueue.assert_not_called()
            handler.on_any_event(DirMovedEvent(str(self.context.raw_archive/'a'),str(self.context.raw_archive/'b')))
            enqueue.assert_called_once()

    def test_maintenance_waits_for_reader_and_old_published_snapshot_survives(self):
        old=index.table(self.context);old_version=old.version
        self.keys.append(301);self.change();self.sync()
        started=threading.Event();finished=threading.Event();errors=[]
        def work():
            try:
                with IndexWriteGate(self.context).hold('test_maintenance'):
                    started.set();maintain(self.context,index._descriptor(self.context),force=True)
            except Exception as exc:errors.append(exc)
            finally:finished.set()
        with reader_guard(self.root):
            thread=threading.Thread(target=work);thread.start();self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.15));self.assertEqual(300,old.count_rows())
        thread.join(10);self.assertFalse(thread.is_alive());self.assertEqual([],errors)
        self.assertEqual(300,old.count_rows());self.assertEqual(301,index.table(self.context).count_rows())

    def test_expired_generations_and_state_files_are_pruned_but_pins_survive(self):
        import lancedb
        from knowledge_bridge.index_retention import prune
        db=lancedb.connect(self.root/'lancedb');names=[]
        for i in range(5):
            name='evidence_'+str(i+1)*32;names.append(name)
            db.create_table(name,data=[{'vector':[1.,0.],'content':'disposable synthetic'}])
            (self.root/'generations'/('state-'+str(i+1)*32+'.json')).write_text('{}')
        pinned=names[0];(self.root/'pinned-generations.json').write_text(json.dumps({'tables':[pinned]}))
        extra=self.root/'generations'/('state-'+'a'*32+'.json');extra.write_text('{}')
        old=time.time()-7200
        for path in list((self.root/'lancedb').glob('*.lance'))+list((self.root/'generations').glob('*.json')):
            os.utime(path,(old,old))
        with IndexWriteGate(self.context).hold('test_retention'):
            with reader_guard(self.root):removed=prune(self.context,'evidence')
        self.assertNotIn(pinned,removed)
        self.assertGreater(len(removed),0)
        self.assertFalse(extra.exists());self.assertEqual(300,index.table(self.context).count_rows())
        self.assertTrue((self.root/index._descriptor(self.context)['state_file']).exists())


if __name__=='__main__':unittest.main()
