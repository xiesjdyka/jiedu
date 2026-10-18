import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.organizer import Organizer,evidence_checked,submit,reject_protected
from knowledge_bridge.writer_core import Writer,sha
from knowledge_bridge import memory_pipeline as memory

class OrganizerTests(unittest.TestCase):
 def test_public_document_can_describe_protection_without_claiming_protected_source(self):
  reject_protected({'content':'产品说明：密文位于 .jiedu/protected','evidence':[{'quote':'保护路径为 .jiedu/protected','source_path':'03_知识/说明.md'}]})
  for value in [{'source_path':'.jiedu/protected/objects/secret'},{'evidence':[{'scope':'protected'}]},{'content':{'protected':True}},{'attachments':['protected:secret']}]:
   with self.assertRaises(PermissionError):reject_protected(value)
 def setUp(self):
  self.stack=ExitStack();self.addCleanup(self.stack.close);tmp=self.stack.enter_context(tempfile.TemporaryDirectory());self.context=initialize_workspace(Path(tmp)/'workspace');self.service=Organizer(self.context)
  self.path=self.context.root/'90_原始档案/测试/对话.md';self.path.parent.mkdir(parents=True);self.path.write_text('用户：报告使用 CSV。\n'+('附带背景😀。\n'*90),'utf-8')
  for name in ['CANDIDATE_EVENTS','STAGING_EVENTS','AUDIT_EVENTS']:self.stack.enter_context(patch.object(memory,name,Path(tmp)/(name+'.jsonl')))
 def job(self):return self.service.start('fixture','evidence','90_原始档案/测试','request-1')
 def evidence(self,job):
  page=self.service.read('fixture',job['job_id'],'90_原始档案/测试/对话.md',0,128);quote='用户：报告使用 CSV。'
  return {'source_path':page['source_path'],'version':page['version'],'byte_start':0,'byte_end':len(quote.encode()),'quote':quote}
 def test_current_evidence_contract_distinguishes_inventory_from_file_snapshot(self):
  job=self.job();page=self.service.read('fixture',job['job_id'],'90_原始档案/测试/对话.md',0,128)
  self.assertEqual(job['snapshot_id'],job['inventory_id'])
  for item in (job['items'][0],page):
   self.assertEqual('current_source_version',item['evidence_mode'])
   self.assertNotIn('snapshot_job_id',item['evidence_fields'])
   self.assertNotIn('snapshot_id',item['evidence_fields'])
  self.assertNotIn('snapshot_id',page);self.assertNotIn('snapshot_job_id',page)
 def test_wrong_snapshot_fields_preserve_progress_then_correct_retry_is_idempotent(self):
  job=self.job();ev=self.evidence(job);path=self.service.path(job['job_id']);before=path.read_bytes()
  options={'job_id':job['job_id'],'request_id':'corrected-reference'}
  for bad in (ev|{'snapshot_job_id':job['job_id']},ev|{'snapshot_job_id':job['job_id'],'snapshot_id':job['snapshot_id']}):
   with self.assertRaisesRegex(ValueError,'顶层 job_id'):
    submit(self.context,'fixture','03_知识/修正.md','CSV','测试',[bad],**options)
   self.assertEqual(before,path.read_bytes());self.assertEqual([],memory.list_candidates())
  first=submit(self.context,'fixture','03_知识/修正.md','CSV','测试',[ev],**options)
  second=submit(self.context,'fixture','03_知识/修正.md','CSV','测试',[ev],**options)
  self.assertEqual(first['candidate_id'],second['candidate_id']);self.assertFalse(second['candidate_created'])
  self.assertEqual(1,len(memory.list_candidates()));self.assertFalse((self.context.root/'03_知识/修正.md').exists())
 def test_immutable_snapshot_contract_requires_real_source_snapshot(self):
  from knowledge_bridge.wiki_snapshots import WikiSnapshots
  source='90_原始档案/测试/对话.md';snapshots=WikiSnapshots(self.context).capture([source])
  job=self.service.start('fixture','evidence','90_原始档案/测试','immutable',snapshots=snapshots)
  page=self.service.read('fixture',job['job_id'],source,0,65536)
  self.assertEqual('immutable_batch_snapshot',page['evidence_mode'])
  self.assertIn('snapshot_id',page['evidence_fields']);self.assertIn('snapshot_job_id',page['evidence_fields'])
  quote='用户：报告使用 CSV。';ev={k:page[k] for k in ('source_path','version','snapshot_id','snapshot_job_id')}|{'byte_start':0,'byte_end':len(quote.encode()),'quote':quote}
  self.assertEqual(quote,evidence_checked(self.context,[ev])[0]['quote'])
  wrong={k:v for k,v in ev.items() if k not in {'snapshot_id','snapshot_job_id'}}
  with self.assertRaisesRegex(ValueError,'快照引用必须绑定'):
   submit(self.context,'fixture','03_知识/不能降级.md','CSV','测试',[wrong],job_id=job['job_id'],request_id='no-downgrade')
  self.assertEqual([],memory.list_candidates())
 def test_inventory_resume_and_source_change(self):
  job=self.job();self.assertEqual(job,self.job());page=self.service.read('fixture',job['job_id'],'90_原始档案/测试/对话.md',0,128)
  self.assertFalse(page['complete']);self.assertEqual(page['content'],Organizer(self.context).read('fixture',job['job_id'],page['source_path'],0,128)['content'])
  with self.assertRaises(ValueError):self.service.finish_source('fixture',job['job_id'],page['source_path'],'processed')
  with self.assertRaises(PermissionError):self.service.status('other',job['job_id'])
  offset=page['resume_offset']
  while offset<self.path.stat().st_size:offset=self.service.read('fixture',job['job_id'],page['source_path'],offset,256)['resume_offset']
  self.assertEqual('processed',self.service.finish_source('fixture',job['job_id'],page['source_path'],'processed')['status'])
  self.path.write_text('变更','utf-8');self.assertEqual(1,self.service.status('fixture',job['job_id'])['counts']['stale'])
  with self.assertRaises(ValueError):self.service.read('fixture',job['job_id'],page['source_path'])
 def test_evidence_exact_and_protected_denial(self):
  ev=self.evidence(self.job());self.assertEqual(ev['quote'],evidence_checked(self.context,[ev])[0]['quote'])
  with self.assertRaises(ValueError):evidence_checked(self.context,[ev|{'quote':'模型编造'}])
  with self.assertRaises(PermissionError):evidence_checked(self.context,[ev|{'scope':'protected'}])
  self.path.write_text('来源改了','utf-8')
  with self.assertRaises(ValueError):evidence_checked(self.context,[ev])
 def test_candidate_single_queue_idempotency_and_target(self):
  job=self.job();ev=self.evidence(job);args=(self.context,'fixture','03_知识/报告.md','# 报告\n\n采用 CSV。','维护报告知识',[ev]);options={'job_id':job['job_id'],'request_id':'change-1'}
  first=submit(*args,**options);second=submit(*args,**options);self.assertEqual(first['candidate_id'],second['candidate_id']);self.assertFalse(second['candidate_created']);self.assertEqual(1,len(memory.list_candidates()))
  job_path=self.service.path(job['job_id']);saved=json.loads(job_path.read_text('utf-8'));saved['sources'][0]['candidate_ids']=[];job_path.write_text(json.dumps(saved),'utf-8')
  submit(*args,**options);self.assertIn(first['candidate_id'],json.loads(job_path.read_text('utf-8'))['sources'][0]['candidate_ids'])
  self.assertFalse((self.context.root/'03_知识/报告.md').exists())
  with self.assertRaises(ValueError):submit(*args,**(options|{'action':'update'}))
 def test_writer_stale_tampered_retry_and_undo(self):
  writer=Writer(self.context.root,self.context.versions/'test');target='03_知识/报告.md';p=writer.preview(target,'报告','CSV',action='create',expected_hash='missing')
  with self.assertRaises(ValueError):writer.apply(p|{'proposed':'篡改'},True)
  first=writer.apply(p,True);second=writer.apply(p,True);self.assertEqual(first['version'],second['version'])
  path=self.context.root/target;new=writer.preview(target,'报告','PDF',action='update',expected_hash=sha(path.read_bytes()));path.write_text('用户修改','utf-8')
  with self.assertRaises(ValueError):writer.apply(new,True)
  with self.assertRaises(ValueError):writer.undo(first['version'],True)
  self.assertEqual('用户修改',path.read_text('utf-8'))
 def test_writer_source_changes_and_version_restore(self):
  ev=self.evidence(self.job());writer=Writer(self.context.root,self.context.versions/'test');p=writer.preview('03_知识/报告.md','报告','CSV',[ev],action='create')
  self.path.write_text('更新','utf-8')
  with self.assertRaises(ValueError):writer.apply(p,True,lambda _:evidence_checked(self.context,[ev]))
  p=writer.preview('03_知识/另页.md','标题','原文',action='create');meta=writer.apply(p,True);writer.undo(meta['version'],True);self.assertFalse((self.context.root/'03_知识/另页.md').exists())

 def test_writer_recovers_after_content_commit_before_manifest(self):
  from knowledge_bridge import writer_core
  writer=Writer(self.context.root,self.context.versions/'test');p=writer.preview('03_知识/恢复.md','标题','原文',action='create');original=writer_core.save
  def fail_manifest(path,data):
   if data.get('state')=='applied':raise OSError('fictional interruption after content commit')
   original(path,data)
  with patch.object(writer_core,'save',side_effect=fail_manifest):
   with self.assertRaises(OSError):writer.apply(p,True)
  result=Writer(self.context.root,self.context.versions/'test').apply(p,True)
  self.assertEqual('applied',result['state']);self.assertEqual(1,len(list(writer.versions.glob('v_*'))))

 def test_concurrent_request_ids_and_explicit_document_denial(self):
  from concurrent.futures import ThreadPoolExecutor
  job=self.job();ev=self.evidence(job)
  def create():return submit(self.context,'fixture','03_知识/并发.md','CSV','并发测试',[ev],request_id='same')
  with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(lambda _:create(),range(4)))
  self.assertEqual(1,len({r['candidate_id'] for r in rows}));self.assertEqual(1,len(memory.list_candidates()))
  def deny(_):raise PermissionError('explicit source deny')
  with self.assertRaises(PermissionError):self.service.start('fixture','evidence','90_原始档案/测试','denied',deny)

 def test_manual_partial_failure_retries_same_queue_without_duplicates(self):
  from knowledge_bridge import manual_extraction as manual
  root=self.context.cache/'manual-test';root.mkdir(parents=True);state=root/'state.json'
  message={'message_id':'m1','role':'user','content':'报告使用 CSV。'}
  with patch.object(manual,'_sessions',return_value={'test':{'client_id':'chatgpt'}}),patch.object(manual,'_messages',return_value=[message]):
   package=manual.export_package(state_path=state,state_sessions=root/'sessions',archive_root=root/'archives',packages_dir=root/'packages')
   row={'candidate_id':'one','package_id':package['package_id'],'type':'knowledge','proposed_content':'CSV','target_layer':'03_知识','target_path':'03_知识/一.md','expected_target_hash':'missing','source_session_ids':['test'],'source_message_ids':['m1'],'evidence':[{'session_id':'test','message_id':'m1','quote':'CSV'}],'reason':'测试','confidence':.8,'conflict_status':'none','conflict_with':[],'proposed_action':'create','supersedes':[],'notes':''}
   payload={'format':'KB_MEMORY_CANDIDATES_V1','package_id':package['package_id'],'candidates':[row,row|{'candidate_id':'two','target_path':'03_知识/二.md'}]}
   original=manual.create_candidate;count=0
   def interrupted(*args,**kwargs):
    nonlocal count
    count+=1
    if count==2:raise OSError('fictional candidate interruption')
    return original(*args,**kwargs)
   options={'state_path':state,'state_sessions':root/'sessions','archive_root':root/'archives','results_dir':root/'results'}
   with patch.object(manual,'create_candidate',side_effect=interrupted):
    with self.assertRaises(OSError):manual.import_result(payload,**options)
   self.assertEqual(1,len(memory.list_candidates()))
   result=manual.import_result(payload,**options);self.assertEqual(2,len(memory.list_candidates()));self.assertEqual(2,len(result['candidates']))
   again=manual.import_result(payload,**options);self.assertEqual(0,again['imported'])

if __name__=='__main__':unittest.main()
