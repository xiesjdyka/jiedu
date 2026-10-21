import copy,json,os,unittest
from unittest.mock import patch
from fastapi import HTTPException
from tests.test_candidate_reorganization import CandidateReorganizationTests as Fixture
from knowledge_bridge import web,memory_pipeline as memory
from knowledge_bridge.organizer import Organizer
from knowledge_bridge.candidate_reorganization import CandidateReorganization
from knowledge_bridge.passage_search import file_version
from knowledge_bridge.writer_core import sha

class ReorganizationLinkTests(unittest.TestCase):
 setUp=Fixture.setUp
 call=Fixture.call
 read_current=Fixture.read_current
 def stale(self):
  (self.context.root/self.source).write_text('用户现在决定使用 JSON。\n新背景。\n','utf-8')
 def claim(self,new_batch=False):return web.api_external_wiki('start',None,{'new_batch':new_batch})
 def task(self,claim=None):return (claim or self.claim())['reorganization']['items'][0]
 def prepare(self,task):
  status=self.call('status',{'job_id':task['job_id']});evidence=[]
  for row in status['items']:
   page=self.call('read',{'job_id':task['job_id'],'source_path':row['source_path'],'offset':0,'page_bytes':65536})
   if row.get('read_role')=='source':
    proof={'source_path':row['source_path'],'version':page['version'],'byte_start':0,'byte_end':page['returned_bytes'],'quote':page['content']}
    if page.get('snapshot_id'):proof.update(snapshot_id=page['snapshot_id'],snapshot_job_id=page['snapshot_job_id'])
    evidence.append(proof)
  return self.payload|{'job_id':task['job_id'],'target_path':task['target_path'],'expected_target_hash':task['expected_target_hash'],'proposed_content':'# 测试\n用户现在决定使用 JSON。\n','evidence':evidence,'request_id':'renew-'+task['old_candidate_id']}
 def test_automatic_claim_fresh_reads_new_pending_history_and_retry(self):
  self.stale();first=self.claim();task=self.task(first);again=self.task(self.claim());self.assertEqual(task['job_id'],again['job_id'])
  status=self.call('status',{'job_id':task['job_id']});self.assertEqual(self.old,status['old_candidate']['candidate_id']);self.assertEqual(self.old_record['content'],status['old_candidate']['content'])
  source=next(r for r in status['items'] if r['read_role']=='source');self.assertEqual('immutable_batch_snapshot',source['evidence_mode']);self.assertTrue(source['snapshot_id'])
  before=memory.CANDIDATE_EVENTS.read_bytes();payload=self.prepare(task);new=self.call('submit',payload)
  self.assertEqual('pending',new['status']);self.assertFalse(new['formal_write_performed']);old=memory.candidate_source(self.old);self.assertEqual('reorganized',old['status']);self.assertEqual(new['candidate_id'],old['reorganization']['successor_candidate_id']);self.assertEqual(self.old_record['content'],old['content']);self.assertTrue(memory.CANDIDATE_EVENTS.read_bytes().startswith(before))
  count=len(memory.list_candidates());self.assertEqual(new['candidate_id'],self.call('submit',payload)['candidate_id']);self.assertEqual(count,len(memory.list_candidates()));self.assertEqual(0,self.claim()['reorganization']['pending']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
 def test_same_bytes_reexport_during_source_read_keeps_snapshot_and_one_successor(self):
  self.stale();task=self.task();job=task['job_id'];path=self.context.root/self.source
  first=self.call('read',{'job_id':job,'source_path':self.source,'offset':0,'page_bytes':4})
  self.assertLess(first['resume_offset'],first['total_bytes'])
  temporary=path.with_suffix('.new');temporary.write_bytes(path.read_bytes());os.replace(temporary,path)
  self.assertNotEqual(first['version'],file_version(path.stat()))
  self.assertNotEqual('stale',next(r for r in self.call('status',{'job_id':job})['items'] if r['source_path']==self.source)['status'])
  payload=self.prepare(task);first_result=self.call('submit',payload);second_result=self.call('submit',payload)
  self.assertEqual(first_result['candidate_id'],second_result['candidate_id'])
  self.assertEqual(2,len(memory.list_candidates()))
  self.assertEqual('reorganized',memory.candidate_source(self.old)['status'])
 def test_target_hash_change_keeps_old_candidate_pending(self):
  self.stale();task=self.task();payload=self.prepare(task);count=len(memory.list_candidates())
  target=self.context.root/self.target;target.write_text('# 测试\n用户后续修改。\n','utf-8')
  with self.assertRaises(HTTPException):self.call('submit',payload)
  self.assertEqual(count,len(memory.list_candidates()))
  self.assertEqual('pending',memory.candidate_source(self.old)['status'])
 def test_authorization_revision_change_keeps_old_candidate_pending(self):
  self.stale();task=self.task();payload=self.prepare(task);count=len(memory.list_candidates())
  self.permissions.set_policy(self.client,'protected','read_protected','deny')
  with self.assertRaises(HTTPException):self.call('submit',payload)
  self.assertEqual(count,len(memory.list_candidates()))
  self.assertEqual('pending',memory.candidate_source(self.old)['status'])
 def test_unread_target_and_finish_or_skip_do_not_close_old_item(self):
  self.stale();task=self.task();status=self.call('status',{'job_id':task['job_id']});source=next(r for r in status['items'] if r['read_role']=='source');page=self.call('read',{'job_id':task['job_id'],'source_path':source['source_path']})
  evidence={'source_path':self.source,'version':page['version'],'byte_start':0,'byte_end':page['returned_bytes'],'quote':page['content']}
  self.call('finish',{'job_id':task['job_id'],'source_path':self.source,'outcome':'processed'})
  with self.assertRaises(HTTPException):self.call('submit',self.payload|{'evidence':[evidence],'job_id':task['job_id'],'request_id':'not-read-target'})
  self.assertEqual('pending',memory.candidate_source(self.old)['status']);self.assertEqual(1,self.claim()['reorganization']['pending']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
 def test_byte_identical_existing_target_closes_with_auditable_unchanged_proof(self):
  self.stale();task=self.task();payload=self.prepare(task);payload['proposed_content']=self.initial_target.decode('utf-8');count=len(memory.list_candidates());result=self.call('submit',payload)
  self.assertEqual('unchanged_verified',result['status']);self.assertFalse(result['candidate_created']);self.assertEqual(count,len(memory.list_candidates()));self.assertEqual('unchanged_verified',memory.candidate_source(self.old)['reorganization']['outcome']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes());self.assertEqual(result['resolution'],self.call('submit',payload)['resolution'])
 def test_source_changes_again_fail_and_explicit_retry_preserves_fixed_candidate_boundary(self):
  self.stale();task=self.task();payload=self.prepare(task);(self.context.root/self.source).write_text('再变化的合成来源。\n','utf-8')
  with self.assertRaises(HTTPException):self.call('submit',payload)
  renewal=CandidateReorganization(self.context);ledger_before=renewal.path.read_bytes();job_before=renewal.organizer.path(task['job_id']).read_bytes();listed=next(c for c in web.api_candidates(None) if c['candidate_id']==self.old);self.assertEqual('needs_attention',listed['reorganization_state']['status']);self.assertIn('再变化',listed['reorganization_state']['failure']);self.assertEqual(ledger_before,renewal.path.read_bytes());self.assertEqual(job_before,renewal.organizer.path(task['job_id']).read_bytes())
  state=self.task();self.assertEqual('needs_attention',state['status']);self.assertEqual('pending',memory.candidate_source(self.old)['status']);new=self.task(self.claim(True));self.assertNotEqual(task['job_id'],new['job_id']);self.assertEqual(task['old_candidate_id'],new['old_candidate_id'])
 def test_refresh_old_item_does_not_open_wiki_batch_or_discover_new_work(self):
  self.stale();first=self.claim();task=self.task(first);payload=self.prepare(task)
  oldpath=Organizer(self.context).path(task['job_id']);oldbytes=oldpath.read_bytes()
  (self.context.root/self.source).write_text('来源新的版本。\n','utf-8')
  extra=memory.create_candidate({'type':'knowledge','title':'稍后新增','content':'新事项','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'target_path':'02_项目/测试/稍后.md','request_id':'late'}})['candidate']['candidate_id']
  from knowledge_bridge.knowledge_wiki import KnowledgeWiki
  wiki=KnowledgeWiki(self.context);before=wiki.path.read_bytes()
  (self.context.root/'90_原始档案/额外新来源.md').write_text('不属于当前边界','utf-8')
  refreshed=self.claim(True);fresh=self.task(refreshed)
  self.assertEqual('existing_reorganization_only',refreshed['boundary']);self.assertTrue(refreshed['wiki_batch_unchanged'])
  self.assertEqual(before,wiki.path.read_bytes());self.assertEqual(oldbytes,oldpath.read_bytes())
  self.assertEqual(first['reorganization']['batch_id'],refreshed['reorganization']['batch_id'])
  self.assertNotEqual(task['job_id'],fresh['job_id']);self.assertEqual([self.old],[t['old_candidate_id'] for t in refreshed['reorganization']['items']])
  self.assertEqual(0,self.call('status',{'job_id':fresh['job_id']})['items'][0]['offset'])
  self.assertEqual('pending',memory.candidate_source(extra)['status'])
  retry=self.claim(True);self.assertEqual(fresh['job_id'],self.task(retry)['job_id'])
 def test_legacy_job_without_source_snapshot_is_preserved_on_explicit_refresh(self):
  self.stale();task=self.task();renew=CandidateReorganization(self.context);path=renew.organizer.path(task['job_id'])
  legacy=json.loads(path.read_text('utf-8'));legacy.pop('snapshot_schema',None)
  for row in legacy['sources']:
   for name in ('snapshot_id','source_sha256','source_identity','captured_at'):row.pop(name,None)
  path.write_text(json.dumps(legacy,ensure_ascii=False),'utf-8');before=path.read_bytes()
  source=self.context.root/self.source;temporary=source.with_suffix('.new');temporary.write_bytes(source.read_bytes());os.replace(temporary,source)
  self.assertEqual('needs_attention',self.task()['status'])
  refreshed=self.task(self.claim(True));self.assertNotEqual(task['job_id'],refreshed['job_id'])
  self.assertEqual(before,path.read_bytes())
  fresh=renew.organizer.load(refreshed['job_id'],self.client)
  self.assertEqual(1,fresh['snapshot_schema'])
  self.assertTrue(next(row for row in fresh['sources'] if row['read_role']=='source')['snapshot_id'])
  self.assertEqual('pending',memory.candidate_source(self.old)['status'])
 def test_completed_fixed_boundary_cannot_reopen_on_refresh_race(self):
  self.stale();claim=self.claim();task=self.task(claim);self.call('submit',self.prepare(task))
  renewal=CandidateReorganization(self.context);before=renewal.path.read_bytes()
  result=renewal.attach({'status':'reorganization_refresh'},self.client,lambda _:None,web._candidate_readiness,web._organization_auth_revision(self.client,[]),new_batch=True,existing_batch_id=claim['reorganization']['batch_id'])
  self.assertEqual('reorganization_complete',result['status']);self.assertEqual(before,renewal.path.read_bytes())
 def user_decision(self,operation):
  self.stale();task=self.task();payload=self.prepare(task)
  if operation=='edit':memory.edit_candidate(self.old,{'title':'用户修改后的事项'})
  elif operation=='reject':memory.review_candidate(self.old,'reject')
  else:memory.review_candidate(self.old,'accept');memory.mark_candidate_written(self.old,'synthetic-writer-version')
  current=copy.deepcopy(memory.candidate_source(self.old))
  with self.assertRaises(HTTPException):self.call('submit',payload)
  self.assertEqual(current,memory.candidate_source(self.old));self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
 def test_user_edit_after_claim_cannot_be_overridden(self):self.user_decision('edit')
 def test_user_reject_after_claim_cannot_be_overridden(self):self.user_decision('reject')
 def test_user_write_after_claim_cannot_be_overridden(self):self.user_decision('write')
 def test_missing_source_blocks_and_permission_revocation_never_renews(self):
  self.stale();(self.context.root/self.source).unlink();task=self.task();self.assertEqual('needs_attention',task['status']);self.assertNotIn('job_id',task);self.assertEqual('pending',memory.candidate_source(self.old)['status'])
  (self.context.root/self.source).write_text('恢复来源。\n','utf-8');task=self.task();payload=self.prepare(task);self.permissions.set_policy(self.client,'evidence','read','deny')
  with self.assertRaises(HTTPException):self.call('submit',payload)
  self.assertEqual('pending',memory.candidate_source(self.old)['status']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
 def test_same_source_different_targets_and_same_target_different_issues_do_not_merge(self):
  second=memory.create_candidate({'type':'knowledge','title':'另一事项','content':'不同事项内容','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'target_path':'02_项目/测试/另一事项.md','request_id':'different'}})['candidate']['candidate_id'];(self.context.root/'02_项目/测试/另一事项.md').write_text('另一事项旧内容','utf-8');self.stale();claim=self.claim();tasks=claim['reorganization']['items'];self.assertEqual({self.old,second},{t['old_candidate_id'] for t in tasks});self.assertEqual(2,len({t['job_id'] for t in tasks}))
  first=next(t for t in tasks if t['old_candidate_id']==self.old);other=next(t for t in tasks if t['old_candidate_id']==second);payload=self.prepare(first)
  with self.assertRaises(HTTPException):self.call('submit',payload|{'target_path':other['target_path']})
  self.call('submit',payload);self.assertEqual('pending',memory.candidate_source(second)['status'])
 def test_another_client_cannot_claim_existing_task_or_use_its_delivery(self):
  self.stale();task=self.task();other=self.registry.create('other_renew','Other','test','stdio');identity=self.registry.authenticate('other_renew',other['secret'])
  for scope in ['formal','evidence']:
   self.permissions.set_policy(identity.internal_id,scope,'read','allow')
  self.permissions.set_policy(identity.internal_id,'formal','submit_candidate','allow')
  with patch.object(web,'_http_client_identity',return_value=identity):
   with self.assertRaises(HTTPException):self.call('status',{'job_id':task['job_id']})
   with self.assertRaises(HTTPException):self.claim()
  renew=CandidateReorganization(self.context);result=renew.attach({'status':'batch_complete'},identity.internal_id,lambda path:None,web._candidate_readiness,web._organization_auth_revision(identity.internal_id,[]));self.assertEqual('needs_attention',self.task(result)['status']);self.assertNotIn('job_id',self.task(result))
 def test_new_candidate_arrivals_do_not_extend_resumed_fixed_batch(self):
  self.stale();first=self.claim();count=first['reorganization']['pending'];extra=memory.create_candidate({'type':'knowledge','title':'新增事项','content':'新增内容','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'target_path':'02_项目/测试/新增.md','request_id':'new-extra'}})['candidate']['candidate_id'];again=self.claim();self.assertEqual(first['reorganization']['batch_id'],again['reorganization']['batch_id']);self.assertEqual(count,again['reorganization']['pending']);self.assertNotIn(extra,[t['old_candidate_id'] for t in again['reorganization']['items']])
 def test_daily_success_requires_verified_old_item_resolution(self):
  self.stale();self.claim();renew=CandidateReorganization(self.context);result=renew.attach({'status':'up_to_date'},self.client,lambda path:None,web._candidate_readiness,web._organization_auth_revision(self.client,[]),run_id='synthetic-run')
  with self.assertRaises(ValueError):renew.verify_run_completed(self.client,'synthetic-run',lambda path:None,web._organization_auth_revision(self.client,[]))
  payload=self.prepare(self.task(result));self.call('submit',payload);renew.verify_run_completed(self.client,'synthetic-run',lambda path:None,web._organization_auth_revision(self.client,[]))
 def test_missing_target_create_uses_server_confirmed_absence_without_fake_read(self):
  target='02_项目/测试/尚未创建.md';old=memory.create_candidate({'type':'knowledge','title':'创建独立事项','content':'旧提案','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'target_path':target,'proposed_action':'create','expected_target_hash':'missing','request_id':'create-old'}})['candidate']['candidate_id'];self.stale();task=next(t for t in self.claim()['reorganization']['items'] if t['old_candidate_id']==old);self.assertEqual('missing',task['expected_target_hash']);payload=self.prepare(task)|{'action':'create'};new=self.call('submit',payload);self.assertEqual('pending',new['status']);self.assertFalse((self.context.root/target).exists());self.assertEqual('reorganized',memory.candidate_source(old)['status'])
 def test_identical_new_body_for_distinct_old_issues_same_target_keeps_separate_candidates(self):
  other=memory.create_candidate({'type':'knowledge','title':'同目标第二事项','content':'独立的旧事项正文','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'request_id':'same-target-other'}})['candidate']['candidate_id'];self.stale();tasks=self.claim()['reorganization']['items'];first=next(t for t in tasks if t['old_candidate_id']==self.old);second=next(t for t in tasks if t['old_candidate_id']==other);one=self.call('submit',self.prepare(first));two=self.call('submit',self.prepare(second));self.assertNotEqual(one['candidate_id'],two['candidate_id']);self.assertEqual(one['candidate_id'],memory.candidate_source(self.old)['reorganization']['successor_candidate_id']);self.assertEqual(two['candidate_id'],memory.candidate_source(other)['reorganization']['successor_candidate_id']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())
 def test_next_daily_run_refreshes_stale_fixed_item_and_same_run_preserves_offset(self):
  self.stale();self.claim();renew=CandidateReorganization(self.context);revision=web._organization_auth_revision(self.client,[]);first=renew.attach({'status':'up_to_date'},self.client,lambda path:None,web._candidate_readiness,revision,run_id='day-one');task=self.task(first);payload=self.prepare(task);(self.context.root/self.source).write_text('第二天变更的来源。\n','utf-8')
  with self.assertRaises(HTTPException):self.call('submit',payload)
  late=memory.create_candidate({'type':'knowledge','title':'运行中新增事项','content':'新增旧依据','evidence':self.old_record['evidence'],'provenance':self.old_record['provenance']|{'target_path':'02_项目/测试/晚到.md','request_id':'late-new'}})['candidate']['candidate_id']
  second=renew.attach({'status':'up_to_date'},self.client,lambda path:None,web._candidate_readiness,revision,run_id='day-two');fresh=self.task(second);self.assertEqual(first['reorganization']['batch_id'],second['reorganization']['batch_id']);self.assertEqual(task['old_candidate_id'],fresh['old_candidate_id']);self.assertNotEqual(task['job_id'],fresh['job_id']);self.assertNotIn(late,[t['old_candidate_id'] for t in second['reorganization']['items']])
  page=self.call('read',{'job_id':fresh['job_id'],'source_path':self.source,'offset':0,'page_bytes':4});again=renew.attach({'status':'up_to_date'},self.client,lambda path:None,web._candidate_readiness,revision,run_id='day-two');self.assertEqual(fresh['job_id'],self.task(again)['job_id']);stored=renew.organizer.load(fresh['job_id'],self.client);row=next(r for r in stored['sources'] if r['source_path']==self.source);self.assertEqual(page['resume_offset'],row['offset']);self.assertEqual('pending',memory.candidate_source(self.old)['status']);self.assertEqual(self.initial_target,(self.context.root/self.target).read_bytes())

del Fixture
