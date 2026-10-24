import hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.archive_store import ArchiveStore,NormalizedAttachment,archive_transaction
from knowledge_bridge.attachment_cache import append_rows,latest_rows,resolve,cache_transaction
from knowledge_bridge.attachment_repair import repair,canonical

class CacheRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.context=initialize_workspace(self.base/'workspace');self.root=self.context.state/'attachments'
        self.root.mkdir();self.store=ArchiveStore(self.context)
        self.targets=[];self.raw={};self.messages={};self.files={};self.cache_rows=[]
        sessions=[self.store.ensure_session(source_key='hermes',source_session_id='fixture-session-'+str(i),title='fixture '+str(i)) for i in range(2)]
        for index,session in enumerate(sessions):
            key=session['session_key'];self.store.append_message(key,message_id='1',role='user',content='synthetic original '+str(index));self.store.append_message(key,message_id='2',role='assistant',content='synthetic answer '+str(index))
            path=self.context.raw_message_store/(key+'.jsonl');self.raw[key]=path.read_bytes()
            self.files[self.context.relative(path)]={'bytes':len(self.raw[key]),'sha256':hashlib.sha256(self.raw[key]).hexdigest()}
            self.messages[key]={str(row['message_id']):canonical(row) for row in self.store._message_rows(path)}
        for index in range(6):
            image=max(0,index-1);raw=('synthetic image '+str(image)).encode();digest=hashlib.sha256(raw).hexdigest();name='fixture-'+str(image)+'.png'
            cached=self.root/(digest+'.png');cached.write_bytes(raw)
            session=sessions[0 if index<2 else 1];mid='1' if index%2==0 else '2';ref=str(self.base/'original-composer'/name)
            legacy={'session_id':session['source_session_id'],'message_id':mid,'reference':ref,'source_path':ref,'original_name':name,'extension':'.png',
                'sha256':digest,'status':'mapped','stored_path':str(self.base/'old-machine-cache'/(digest+'.png')),'archived_path':str(self.base/'old-machine-cache'/(digest+'.png'))}
            self.cache_rows.append(legacy);append_rows(self.root,[legacy])
            self.store.reconcile_attachments(session['session_key'],mid,[NormalizedAttachment(reference=ref,original_name=name)],materialize=False)
            self.targets.append({'session_key':session['session_key'],'session_id':session['source_session_id'],'message_id':mid,'source_reference':ref,'original_name':name,
                'expected_sha256':digest,'current_cache_relative':self.context.relative(cached),'message_canonical_sha256':self.messages[session['session_key']][mid]})
        self.baseline={'targets':self.targets,'original_message_files':self.files,'original_messages':self.messages,'untouched_incomplete_references':['...fixture-unknown.png']}

    def test_relocation_repair_appends_only_and_keeps_exact_originals(self):
        before=(self.root/'manifest.jsonl').read_bytes();preview=repair(self.context,self.baseline)
        self.assertEqual('preview',preview['state']);self.assertEqual(before,(self.root/'manifest.jsonl').read_bytes())
        result=repair(self.context,self.baseline,apply=True);self.assertEqual(6,len(result['receipts']));self.assertEqual(5,result['unique_images'])
        self.assertTrue((self.root/'manifest.jsonl').read_bytes().startswith(before))
        self.assertEqual(12,len((self.root/'manifest.jsonl').read_text('utf-8').splitlines()))
        for key,raw in self.raw.items():
            self.assertEqual(raw,(self.context.raw_message_store/(key+'.jsonl')).read_bytes())
            for row in self.store.attachment_status(key):
                self.assertFalse(row['missing']);path=self.context.root/row['stored_path'];self.assertEqual(row['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        repair(self.context,self.baseline,apply=True)
        self.assertEqual(12,len((self.root/'manifest.jsonl').read_text('utf-8').splitlines()))
        latest=latest_rows(self.root/'manifest.jsonl');self.assertEqual(6,len(latest));self.assertEqual(self.cache_rows[0]['source_path'],list(latest.values())[0]['source_path'])

    def test_corrupt_hash_incomplete_reference_and_changed_message_fail_closed(self):
        target=self.targets[-1];cache=self.context.root/target['current_cache_relative'];cache.write_bytes(b'wrong bytes same filename')
        before=(self.root/'manifest.jsonl').read_bytes()
        with self.assertRaises(ValueError):repair(self.context,self.baseline,apply=True)
        self.assertEqual(before,(self.root/'manifest.jsonl').read_bytes())
        self.assertEqual({'reference':'...unknown.png','sha256':None},resolve(self.root,{'reference':'...unknown.png','sha256':None}))
        image=4;cache.write_bytes(('synthetic image '+str(image)).encode())
        key=next(iter(self.raw));raw_path=self.context.raw_message_store/(key+'.jsonl');raw_path.write_bytes(self.raw[key].replace(b'synthetic original',b'modified original!'))
        with self.assertRaises(ValueError):repair(self.context,self.baseline,apply=True)
        self.assertEqual(before,(self.root/'manifest.jsonl').read_bytes())

    def test_cache_replaced_after_verification_cannot_publish_changed_bytes(self):
        from unittest.mock import patch
        original=ArchiveStore.reconcile_attachments
        invoked=[]
        def raced(store,key,mid,specs,**kwargs):
            spec=specs[0]
            target=next(t for t in self.targets if t['session_key']==key and t['message_id']==mid and t['source_reference']==spec.reference)
            path=self.context.root/target['current_cache_relative'];saved=path.read_bytes()
            path.write_bytes(b'changed after validation')
            try:
                self.assertIsNone(spec.local_path)
                self.assertEqual(target['expected_sha256'],hashlib.sha256(spec.inline_data).hexdigest())
                result=original(store,key,mid,specs,**kwargs)
                self.assertEqual(target['expected_sha256'],result[0]['sha256'])
                self.assertEqual(target['expected_sha256'],hashlib.sha256((self.context.root/result[0]['stored_path']).read_bytes()).hexdigest())
                invoked.append(result[0])
                return result
            finally:path.write_bytes(saved)
        with patch.object(ArchiveStore,'reconcile_attachments',raced):
            result=repair(self.context,self.baseline,apply=True)
        self.assertEqual(6,len(invoked));self.assertEqual(6,len(result['receipts']))
        for key,raw in self.raw.items():self.assertEqual(raw,(self.context.raw_message_store/(key+'.jsonl')).read_bytes())

    def test_reference_categories_deduplicate_sources_and_keep_incomplete_locators(self):
        from knowledge_bridge.attachment_reference_stats import summarize
        rows=[{'session_key':'A','source_session_id':'fixture-session','source_message_id':str(i),'source_reference':'delivery:same','status':'missing','missing':True} for i in (1,2)]
        rows += [{'source_message_id':'3','source_reference':'inline:fixture:0','status':'available'},
            {'source_message_id':'4','source_reference':'...fixture.png','status':'missing'}]
        locators={'fixture-session\0'+str(i)+'\0delivery:same':{'local_path':'old-machine/fixture.md'} for i in (1,2)}
        result=summarize(rows,locators)
        self.assertEqual(2,result['by_kind']['delivery_link']['unavailable']);self.assertEqual(0,result['by_kind']['uploaded']['unavailable'])
        self.assertEqual(2,result['unique_unavailable_sources']);self.assertEqual('old-machine/fixture.md',result['unavailable'][0]['source_path'])
        self.assertEqual('incomplete_reference',result['unavailable'][-1]['reason'])

    def test_live_hermes_sync_independent_process_preserves_repair_states(self):
        program=Path(__file__).resolve().parents[1];source=self.base/'live.png';source.write_bytes(b'live fixture')
        code="""import sys,time
sys.path.insert(0,sys.argv[1])
from knowledge_bridge.hermes_session_adapter import _attachments
print('READY',flush=True)
for i in range(12):
 _attachments('live-session',str(i),i,[sys.argv[2]])
 time.sleep(.005)
"""
        environment=os.environ.copy();environment.update(KNOWLEDGE_BRIDGE_VAULT=str(self.context.root),KNOWLEDGE_BRIDGE_LOCAL_DATA=str(self.base/'control'),PYTHONDONTWRITEBYTECODE='1')
        with archive_transaction(self.context),cache_transaction(self.root):
            worker=subprocess.Popen([sys.executable,'-c',code,str(program),str(source)],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.assertEqual(b'READY',worker.stdout.readline().strip())
            self.assertIsNone(worker.poll()) # Live sync is waiting on the same cache transaction.
            result=repair(self.context,self.baseline,apply=True)
        out,error=worker.communicate(timeout=20)
        self.assertEqual(0,worker.returncode,error.decode(errors='replace'));self.assertEqual(6,len(result['receipts']))
        latest=latest_rows(self.root/'manifest.jsonl');self.assertEqual(18,len(latest))
        self.assertEqual(12,sum(row['session_id']=='live-session' for row in latest.values()))
        for key,raw in self.raw.items():self.assertEqual(raw,(self.context.raw_message_store/(key+'.jsonl')).read_bytes())

if __name__=='__main__':unittest.main()
