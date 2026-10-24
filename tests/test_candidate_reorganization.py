import copy,json,tempfile,threading,unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi import HTTPException
from knowledge_bridge import web,memory_pipeline as memory,production_writer
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.organizer import Organizer
from knowledge_bridge.writer_core import sha


class CandidateReorganizationTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        base=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.context=initialize_workspace(base/'vault')
        self.registry=ClientIdentityRegistry(self.context,base/'machine')
        self.permissions=ClientPermissionService(self.registry)
        created=self.registry.create('renew_fixture','Renew fixture','test','stdio')
        self.client=created['internal_id'];self.identity=self.registry.authenticate('renew_fixture',created['secret'])
        self.manager=SimpleNamespace(_lock=threading.RLock(),protection_session_id='fixture',storage_enabled=False)
        for scope in ['formal','evidence']:
            for operation in ['search','read']:self.permissions.set_policy(self.client,scope,operation,'allow')
        self.permissions.set_policy(self.client,'formal','submit_candidate','allow')
        for key,value in [('client_registry',self.registry),('client_permissions',self.permissions),('protection_control',self.manager)]:self.stack.enter_context(patch.object(web.app.state,key,value,create=True))
        for name in ['CANDIDATE_EVENTS','STAGING_EVENTS','AUDIT_EVENTS']:self.stack.enter_context(patch.object(memory,name,base/(name+'.jsonl')))
        self.stack.enter_context(patch.object(web,'active_workspace',return_value=self.context))
        self.stack.enter_context(patch.object(web,'_client_services',return_value=(self.registry,self.permissions)))
        self.stack.enter_context(patch.object(web,'_protection_manager',return_value=self.manager))
        self.stack.enter_context(patch.object(web,'_http_client_identity',side_effect=lambda _:self.identity))
        self.owner_read=self.stack.enter_context(patch.object(web,'_owner_read'))
        self.stack.enter_context(patch.multiple(production_writer,VAULT=self.context.root,REAL_VERSIONS=self.context.versions/'formal-memory'))
        self.source='90_原始档案/测试/讨论.md';self.target='02_项目/测试/当前状态.md'
        for path,text in [(self.source,'用户决定使用 CSV。\n背景说明。\n'),(self.target,'# 测试\n旧格式 PDF。\n')]:
            dest=self.context.root/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text,'utf-8')
        self.initial_target=(self.context.root/self.target).read_bytes()
        self.evidence=self.read_current(self.source,'source-old')
        self.read_current(self.target,'target-old')
        self.payload={'target_path':self.target,'proposed_content':'# 测试\n用户决定使用 CSV。\n','reason':'依原文更新格式','evidence':[self.evidence],'action':'update','expected_target_hash':sha(self.initial_target),'request_id':'first'}
        self.old=self.call('submit',self.payload)['candidate_id']
        self.old_record=copy.deepcopy(memory.candidate_source(self.old))
    def call(self,action,payload):return web.api_external_organizer(action,None,payload)
    def read_current(self,path,request_id):
        scope='evidence' if path.startswith('90_') else 'formal'
        job=self.call('start',{'scope':scope,'folder':str(Path(path).parent).replace('\\','/'),'request_id':request_id})
        page=self.call('read',{'job_id':job['job_id'],'source_path':path})
        return {'source_path':path,'version':page['version'],'byte_start':0,'byte_end':page['returned_bytes'],'quote':page['content']}
    def change_revision(self):self.permissions.set_policy(self.client,'protected','read_protected','deny')
    def test_reread_new_receipt_creates_current_candidate_and_retry_is_idempotent(self):
        self.change_revision()
        state=web._candidate_readiness(memory.candidate_source(self.old))
        self.assertEqual('authorization_revision_expired',state['code'])
        with self.assertRaises(web.CandidateAuthorizationError):web._candidate_preview(memory.candidate_source(self.old))
        renewed=self.payload|{'request_id':'renewed'}
        with self.assertRaises(HTTPException) as blocked:self.call('submit',renewed)
        self.assertIn('实际重读',str(blocked.exception.detail))
        for forged in [{'authorization':{'revision':'fake'}},{'read_receipt':{'delivered':True}}]:
            with self.assertRaises(HTTPException):self.call('submit',renewed|forged)
        self.read_current(self.source,'source-new')
        with self.assertRaises(HTTPException) as blocked:self.call('submit',renewed)
        self.assertIn('读完当前目标',str(blocked.exception.detail))
        self.read_current(self.target,'target-new')
        fresh=self.call('submit',renewed)
        self.assertNotEqual(self.old,fresh['candidate_id']);self.assertTrue(fresh['candidate_created'])
        self.assertEqual(fresh['candidate_id'],self.call('submit',renewed)['candidate_id'])
        self.assertEqual(fresh['candidate_id'],self.call('submit',renewed|{'request_id':'another-retry'})['candidate_id'])
        self.assertEqual(2,len(memory.list_candidates()));self.assertEqual(self.old_record,memory.candidate_source(self.old))
        current=memory.candidate_source(fresh['candidate_id'])
        self.assertEqual('reviewable',web._candidate_readiness(current)['state'])
        self.assertIn('CSV',web._candidate_preview(current)['diff'])
        with self.assertRaises(web.CandidateAuthorizationError):web._candidate_preview(memory.candidate_source(self.old))
        self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
    def test_same_request_id_cannot_return_expired_receipt_after_fresh_reads(self):
        self.change_revision();self.read_current(self.source,'new-source');self.read_current(self.target,'new-target')
        current=self.call('submit',self.payload)
        self.assertNotEqual(self.old,current['candidate_id'])
        self.assertEqual(current['candidate_id'],self.call('submit',self.payload)['candidate_id'])
    def test_other_identity_delivery_and_stale_source_cannot_renew(self):
        self.change_revision()
        other=self.registry.create('other_fixture','Other','test','stdio')['internal_id']
        service=Organizer(self.context);job=service.start(other,'evidence','90_原始档案/测试','other')
        service.read(other,job['job_id'],self.source,authorization_revision=web._organization_auth_revision(self.client,[]))
        with self.assertRaises(HTTPException):self.call('submit',self.payload|{'request_id':'new'})
        self.read_current(self.source,'renew-source');self.read_current(self.target,'renew-target')
        (self.context.root/self.source).write_text('已经变更的来源','utf-8')
        with self.assertRaises(HTTPException):self.call('submit',self.payload|{'request_id':'new'})
    def test_list_is_read_only_metadata_and_authorization_gate_remains(self):
        snapshot=memory.CANDIDATE_EVENTS.read_bytes()
        with patch.object(web,'_candidate_preview',side_effect=AssertionError('no list preview')),patch('knowledge_bridge.organizer.evidence_checked',side_effect=AssertionError('no source body reads')):
            listed=web.api_candidates(None)
            self.assertEqual('reviewable',listed[0]['readiness']['state']);self.assertFalse(listed[0]['readiness']['can_save'])
            self.change_revision();listed=web.api_candidates(None)
            self.assertEqual('needs_reorganization',listed[0]['readiness']['state'])
        self.owner_read.assert_called();self.assertEqual(snapshot,memory.CANDIDATE_EVENTS.read_bytes())
        with patch.object(web,'_owner_read',side_effect=HTTPException(403,'blocked')):
            with self.assertRaises(HTTPException):web.api_candidates(None)
            with self.assertRaises(HTTPException):web.api_reorganization_instructions(None,{'candidate_ids':[self.old]})
    def test_selected_instructions_no_execution_missing_source_and_protected_boundary(self):
        with self.assertRaises(HTTPException):web.api_reorganization_instructions(None,{'candidate_ids':[]})
        self.change_revision();snapshot=memory.CANDIDATE_EVENTS.read_bytes()
        result=web.api_reorganization_instructions(None,{'candidate_ids':[self.old]})
        self.assertIn(self.source,result['instruction']);self.assertIn(self.target,result['instruction']);self.assertIn(self.old,result['instruction'])
        self.assertIn('organization_read',result['instruction']);self.assertFalse(result['execution_started'])
        self.assertEqual([self.old],result['candidate_ids']);self.assertEqual(snapshot,memory.CANDIDATE_EVENTS.read_bytes())
        (self.context.root/self.source).unlink();result=web.api_reorganization_instructions(None,{'candidate_ids':[self.old]})
        self.assertTrue(result['warnings']);self.assertIn('来源缺失',result['instruction'])
        protected=self.old_record|{'evidence':[{'scope':'protected','source_path':'protected:private'}]}
        with patch.object(web,'candidate_source',return_value=protected):
            with self.assertRaises(HTTPException):web.api_reorganization_instructions(None,{'candidate_ids':[self.old]})
    def test_source_denial_and_unknown_status_never_advertise_save(self):
        self.permissions.set_policy(self.client,'evidence','read','deny')
        state=web._candidate_readiness(self.old_record);self.assertEqual('source_read_not_allowed',state['code']);self.assertFalse(state['can_save'])
        with patch.object(web,'_validate_external_candidate_authorization',side_effect=OSError('metadata unavailable')):
            state=web._candidate_readiness(self.old_record);self.assertEqual('unknown',state['state']);self.assertFalse(state['can_save'])
    def test_authorization_read_mark_is_private_and_old_offset_cannot_resume(self):
        service=Organizer(self.context)
        job=self.call('start',{'scope':'evidence','folder':'90_原始档案/测试','request_id':'source-old'})
        self.assertNotIn('delivered_authorization_revision',json.dumps(job))
        self.change_revision()
        with self.assertRaises(HTTPException):self.call('read',{'job_id':job['job_id'],'source_path':self.source,'offset':1})
        page=self.call('read',{'job_id':job['job_id'],'source_path':self.source,'offset':0})
        self.assertNotIn('delivered_authorization_revision',json.dumps(page))
        saved=service.load(job['job_id'],self.client)
        self.assertEqual(web._organization_auth_revision(self.client,[]),saved['sources'][0]['delivered_authorization_revision'])

    def test_snapshot_candidate_append_stays_reviewable_but_new_permission_requires_reread(self):
        from knowledge_bridge.wiki_snapshots import WikiSnapshots
        frozen=WikiSnapshots(self.context).capture([self.source])
        job=Organizer(self.context).start(self.client,'evidence','90_原始档案/测试','snapshot-current',snapshots=frozen)
        page=self.call('read',{'job_id':job['job_id'],'source_path':self.source})
        ev={k:page[k] for k in ('source_path','version','snapshot_id','snapshot_job_id')}|{'byte_start':0,'byte_end':page['returned_bytes'],'quote':page['content']}
        result=self.call('submit',self.payload|{'evidence':[ev],'job_id':job['job_id'],'request_id':'snapshot-current'})
        current=memory.candidate_source(result['candidate_id'])
        source_file=self.context.root/self.source
        source_file.write_bytes(source_file.read_bytes()+'后续更正：改用 JSON。\n'.encode())
        legacy=copy.deepcopy(current);legacy['provenance'].pop('append_stable_window')
        self.assertEqual('source_version_changed',web._candidate_readiness(legacy)['code'])
        ready=web._candidate_readiness(current)
        self.assertEqual('reviewable',ready['state']);self.assertGreater(ready['append_pending_bytes'],0)
        self.assertIn('CSV',web._candidate_preview(current)['diff'])
        target_file=self.context.root/self.target
        target_file.write_text('# 测试\n另一个目标版本。\n','utf-8')
        with self.assertRaises(ValueError):web._candidate_preview(current)
        target_file.write_bytes(self.initial_target)
        self.change_revision()
        self.assertEqual('authorization_revision_expired',web._candidate_readiness(current)['code'])
        with self.assertRaises(web.CandidateAuthorizationError):web._candidate_preview(current)

    def test_reorganization_job_survives_append_and_closes_one_old_candidate(self):
        from knowledge_bridge.candidate_reorganization import CandidateReorganization
        from knowledge_bridge.knowledge_wiki import KnowledgeWiki
        self.change_revision()
        KnowledgeWiki(self.context).claim(self.client,lambda _:None)
        renewal=CandidateReorganization(self.context)
        revision=web._organization_auth_revision(self.client,[])
        batch=renewal.attach({'status':'up_to_date'},self.client,lambda _:None,web._candidate_readiness,revision)
        item=batch['reorganization']['items'][0];job_id=item['job_id']
        self.assertEqual(self.old,item['old_candidate_id'])
        source_file=self.context.root/self.source
        source_file.write_bytes(source_file.read_bytes()+'后续更正：改用 JSON。\n'.encode())
        self.assertTrue(renewal.row_current(Organizer(self.context).load(job_id,self.client)['sources'][0]))
        source_page=self.call('read',{'job_id':job_id,'source_path':self.source})
        self.call('read',{'job_id':job_id,'source_path':self.target})
        evidence={k:source_page[k] for k in ('source_path','version','snapshot_id','snapshot_job_id')}|{'byte_start':0,'byte_end':source_page['returned_bytes'],'quote':source_page['content']}
        result=self.call('submit',self.payload|{'evidence':[evidence],'job_id':job_id,'request_id':'renew-after-append'})
        successor=memory.candidate_source(result['candidate_id'])
        self.assertEqual('reorganized',memory.candidate_source(self.old)['status'])
        self.assertTrue(successor['provenance']['append_stable_window'])
        source_file.write_bytes(source_file.read_bytes()+'还有一条更新。\n'.encode())
        self.assertEqual('reviewable',web._candidate_readiness(successor)['state'])
        self.assertEqual(2,len(memory.list_candidates()))

    def test_retry_never_closes_old_item_when_saved_snapshot_was_lost(self):
        from knowledge_bridge.candidate_reorganization import CandidateReorganization
        from knowledge_bridge.knowledge_wiki import KnowledgeWiki
        from knowledge_bridge.wiki_snapshots import WikiSnapshots
        self.change_revision();KnowledgeWiki(self.context).claim(self.client,lambda _:None)
        renewal=CandidateReorganization(self.context);revision=web._organization_auth_revision(self.client,[])
        batch=renewal.attach({'status':'up_to_date'},self.client,lambda _:None,web._candidate_readiness,revision)
        job_id=batch['reorganization']['items'][0]['job_id']
        page=self.call('read',{'job_id':job_id,'source_path':self.source})
        self.call('read',{'job_id':job_id,'source_path':self.target})
        ev={k:page[k] for k in ('source_path','version','snapshot_id','snapshot_job_id')}|{'byte_start':0,'byte_end':page['returned_bytes'],'quote':page['content']}
        payload=self.payload|{'evidence':[ev],'job_id':job_id,'request_id':'interrupted-renewal'}
        with patch.object(CandidateReorganization,'complete',side_effect=OSError('interrupted')):
            with self.assertRaises(HTTPException):self.call('submit',payload)
        self.assertEqual('pending',memory.candidate_source(self.old)['status'])
        self.assertEqual(2,len(memory.list_candidates()))
        WikiSnapshots(self.context).path(page['snapshot_id']).unlink()
        with self.assertRaises(HTTPException):self.call('submit',payload)
        self.assertEqual('pending',memory.candidate_source(self.old)['status'])
        self.assertFalse(renewal.load()['batches'][batch['reorganization']['batch_id']]['tasks'][0]['resolved'])


if __name__=='__main__':unittest.main()
