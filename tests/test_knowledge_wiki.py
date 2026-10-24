import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.knowledge_wiki import KnowledgeWiki
from knowledge_bridge.writer_core import Writer
from knowledge_bridge import manual_extraction as manual
from knowledge_bridge.organization_review import review_state
from knowledge_bridge.conversation_visibility import SourceVisibility

class WikiFixture(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.ctx=initialize_workspace(Path(self.tmp.name)/'vault');self.wiki=KnowledgeWiki(self.ctx)
  self.file=self.ctx.raw_archive/'Hermes/demo/01_对话/conversation.md';self.file.parent.mkdir(parents=True);self.file.write_text('# 来源\n\n用户决定使用 CSV。\n','utf-8')
  self.meta=self.file.parent.parent/'03_元数据';self.meta.mkdir();(self.meta/'manifest.json').write_text(json.dumps({'source_key':'hermes','source_session_id':'ordinary','session_key':'s'}),'utf-8')
  (self.meta/'messages.jsonl').write_text(json.dumps({'message_id':'m','role':'user','content':'用户决定使用 CSV。'})+'\n','utf-8')
 def claim(self,**kwargs):return self.wiki.claim('client',lambda path:None,**kwargs)
 def complete(self,job):
  for row in job['items']:
   self.wiki.organizer.read('client',job['job_id'],row['source_path'],0,65536)
   self.wiki.organizer.finish_source('client',job['job_id'],row['source_path'],'processed')
class WikiTests(WikiFixture):
 def test_frozen_prefix_accepts_append_and_next_batch_reads_only_tail(self):
  from knowledge_bridge.wiki_snapshots import WikiSnapshots
  job=self.claim();row=job['items'][0];source=row['source_path'];store=WikiSnapshots(self.ctx)
  self.wiki.organizer.read('client',job['job_id'],source,0,65536,authorization_revision='fixture-revision')
  self.wiki.organizer.finish_source('client',job['job_id'],source,'processed')
  original=self.file.read_bytes();tail='后续更正：改用 JSON。\n'.encode('utf-8');self.file.write_bytes(original+tail)
  proof=store.verify_frozen_prefix(row)
  self.assertEqual(len(tail),proof['appended_bytes'])
  with self.assertRaises(ValueError):store.verify_current(row)
  self.assertEqual(1,self.wiki.status()['next_round_changed_sources'])
  next_job=self.claim(new_batch=True);self.assertEqual(len(original),next_job['items'][0]['offset'])
  page=self.wiki.organizer.read('client',next_job['job_id'],source,len(original),65536)
  self.assertEqual(tail.decode('utf-8'),page['content'])
  self.wiki.organizer.finish_source('client',next_job['job_id'],source,'processed')

 def test_frozen_prefix_rejects_rewrite_truncation_and_identity_change(self):
  from knowledge_bridge.wiki_snapshots import WikiSnapshots
  job=self.claim();row=job['items'][0];store=WikiSnapshots(self.ctx);original=self.file.read_bytes()
  self.file.write_bytes(b'X'+original[1:]+b'new tail')
  with self.assertRaises(ValueError):store.verify_frozen_prefix(row)
  self.file.write_bytes(original[:-1])
  with self.assertRaises(ValueError):store.verify_frozen_prefix(row)
  self.file.write_bytes(original+b'new tail')
  (self.meta/'manifest.json').write_text(json.dumps({'source_key':'hermes','source_session_id':'replaced','session_key':'other'}),'utf-8')
  with self.assertRaises(ValueError):store.verify_frozen_prefix(row)

 def test_disabled_and_copy_are_read_only(self):
  self.assertEqual('disabled',self.wiki.status()['phase']);self.assertTrue(self.wiki.instructions());self.assertFalse(self.wiki.path.exists())
  self.assertEqual([],self.wiki.pages([]));self.assertTrue(self.file.exists())
 def test_claim_retry_resume_pause_and_increment(self):
  first=self.claim();self.assertEqual('claimed',first['status']);self.assertTrue(self.wiki.status()['started_at'])
  source=first['items'][0]['source_path'];page=self.wiki.organizer.read('client',first['job_id'],source,0,8)
  resumed=KnowledgeWiki(self.ctx).claim('client',lambda path:None);self.assertTrue(resumed['resumed']);self.assertEqual(first['job_id'],resumed['job_id']);self.assertEqual(page['resume_offset'],resumed['items'][0]['offset'])
  self.wiki.pause();self.assertEqual('paused',self.wiki.status()['phase']);self.assertEqual(first['job_id'],self.claim()['job_id'])
  self.complete(self.claim());self.assertEqual('batch_complete',self.claim()['status']);self.assertEqual(1,len(self.wiki.load()['jobs']))
  self.file.write_text(self.file.read_text('utf-8')+'追加：保留来源。\n','utf-8')
  changed=self.claim(new_batch=True);self.assertNotEqual(first['job_id'],changed['job_id']);self.assertEqual(1,changed['total']);self.complete(changed);self.assertEqual('up_to_date',self.wiki.status()['phase'])
 def test_unauthorized_claim_never_enables(self):
  with self.assertRaises(PermissionError):self.wiki.claim('bad',lambda path:(_ for _ in ()).throw(PermissionError()))
  self.assertFalse(self.wiki.load()['enabled']);self.assertFalse(self.wiki.path.exists())
 def test_failure_and_recovery_preserve_job(self):
  job=self.claim();self.file.write_bytes(b'\xffbad')
  self.wiki.organizer.read('client',job['job_id'],job['items'][0]['source_path'])
  self.assertEqual('running',self.wiki.status()['phase']);self.assertEqual(1,self.wiki.status()['next_round_changed_sources'])
  self.assertTrue(self.wiki.organizer.path(job['job_id']).exists())
 def test_real_writer_page_and_sources_no_mirror_store(self):
  writer=Writer(self.ctx.root,self.ctx.versions/'fixture');path='03_知识/数据格式.md';source=self.file.relative_to(self.ctx.root).as_posix()
  preview=writer.preview(path,'数据格式','# 数据格式\n\n使用 CSV。',action='create');writer.apply(preview,True)
  pages=self.wiki.pages([{'status':'written','provenance':{'target_path':path},'evidence':[{'source_path':source,'quote':'用户决定使用 CSV。'}]}])
  self.assertEqual(1,len(pages));self.assertEqual(source,pages[0]['sources'][0]['path']);self.assertFalse(self.wiki.path.exists())
 def test_schedule_proof_is_separate_from_read_receipt(self):
  receipt=self.ctx.state/'organization-review-receipt.json';receipt.write_text(json.dumps({'schema_version':1,'workspace_id':self.ctx.workspace_id,'updated_at':'2026-01-01T00:00:00Z','rows':[]}),'utf-8')
  self.assertEqual('not_arranged',self.wiki.schedule()['arrangement'])
  config=self.ctx.control/'scheduler.toml';config.write_text('id="daily"\nstatus="ACTIVE"\ntarget_thread_id="runner"\nrrule="FREQ=DAILY"\n','utf-8')
  binding={'workspace_id':self.ctx.workspace_id,'identities':[{'source_key':'codex','source_session_id':'runner'}],'scheduler':{'id':'daily','config_path':str(config),'execution_receipt':'.jiedu/state/scheduler-execution.json'}}
  (self.ctx.state/'organization-runner.json').write_text(json.dumps(binding),'utf-8')
  status=self.wiki.schedule();self.assertEqual('arranged',status['arrangement']);self.assertIsNone(status['last_started_at']);self.assertIsNone(status['last_success_at'])
  (self.ctx.state/'scheduler-execution.json').write_text(json.dumps({'workspace_id':self.ctx.workspace_id,'scheduler_id':'daily','provider':'codex_scheduler','run_id':'r1','status':'failed','started_at':'2026-01-02T00:00:00Z','failure':'测试服务不可用'}),'utf-8')
  self.assertIsNone(self.wiki.schedule()['last_success_at']);self.assertNotEqual('failed',self.wiki.schedule()['execution_state'])
  config.unlink();self.assertEqual('unavailable',self.wiki.schedule()['arrangement'])

 def test_mixed_changes_preserve_unchanged_partial_cursor(self):
  import shutil
  second=self.file.parent.parent.with_name('second');shutil.copytree(self.file.parent.parent,second)
  (second/'03_元数据/manifest.json').write_text(json.dumps({'source_key':'hermes','source_session_id':'second','session_key':'second'}),'utf-8')
  extra=second/'01_对话/conversation.md';extra.write_text('another source long enough for partial reads','utf-8')
  first=self.claim();source=self.file.relative_to(self.ctx.root).as_posix()
  read=self.wiki.organizer.read('client',first['job_id'],source,0,8)
  extra.write_text('changed source','utf-8')
  next_job=self.claim();self.assertEqual(first['job_id'],next_job['job_id'])
  self.assertEqual(read['resume_offset'],next(r for r in next_job['items'] if r['source_path']==source)['offset'])
 def test_timestamp_change_does_not_return_unreadable_snapshot(self):
  import os
  first=self.claim();st=self.file.stat();os.utime(self.file,ns=(st.st_atime_ns,st.st_mtime_ns+1000000000))
  next_job=self.claim();self.assertEqual(first['job_id'],next_job['job_id'])
  self.wiki.organizer.read('client',next_job['job_id'],next_job['items'][0]['source_path'])
 def test_second_client_does_not_duplicate_active_work(self):
  self.claim()
  with self.assertRaisesRegex(ValueError,'原 AI'):self.wiki.claim('another-client',lambda _:None)
  self.assertEqual(1,len(self.wiki.load()['jobs']))
 def test_failed_read_same_version_preserves_reason_and_resumes(self):
  job=self.claim()
  with patch('knowledge_bridge.wiki_snapshots.WikiSnapshots.page',side_effect=OSError('fixture failure')):
   with self.assertRaises(ValueError):self.wiki.organizer.read('client',job['job_id'],job['items'][0]['source_path'])
  self.assertEqual('failed',self.wiki.status()['phase']);self.assertTrue(self.wiki.status()['reason'])
  self.assertEqual(job['job_id'],self.claim()['job_id']);self.complete(self.claim())
 def test_missing_snapshot_does_not_reconstruct_from_current_source(self):
  job=self.claim();row=job['items'][0]
  from knowledge_bridge.wiki_snapshots import WikiSnapshots
  WikiSnapshots(self.ctx).path(row['snapshot_id']).unlink()
  with self.assertRaises(ValueError):self.wiki.organizer.read('client',job['job_id'],row['source_path'])
  self.assertEqual('failed',self.wiki.status()['phase'])
  self.assertEqual(job['job_id'],self.claim()['job_id'])

class OrganizerIdentityTests(WikiFixture):
 def binding(self,identity):
  (self.ctx.state/'organization-runner.json').write_text(json.dumps({'workspace_id':self.ctx.workspace_id,'identities':[{'source_key':'hermes','source_session_id':identity}]}),'utf-8')
 def test_stable_self_excludes_growth_and_live_snapshot_in_both_consumers(self):
  self.binding('ordinary');item={'archive_path':str(self.file.parent.parent),'live_snapshot_pending':True,'canonical_messages':[{'message_id':'m','role':'user','content':'追加'}]}
  self.file.write_text(self.file.read_text('utf-8')+'整理任务持续追加\n','utf-8')
  self.assertTrue(review_state(self.ctx,item,None,lambda _: {})['stable_exclusion']);self.assertEqual('internal_organization_task',SourceVisibility(self.ctx).reason(self.file.relative_to(self.ctx.root).as_posix()))
  with patch.object(manual,'active_workspace',return_value=self.ctx),patch.object(manual,'_sessions',return_value={'s':item}),patch.object(manual,'_session_rows',return_value=item['canonical_messages']):
   result=manual.status(state_path=self.ctx.state/'manual.json');self.assertEqual(0,result['pending_session_count']);self.assertEqual(1,len(result['excluded_sessions']))
 def test_ordinary_growth_and_empty_to_public_remain_pending(self):
  self.binding('someone-else');item={'archive_path':str(self.file.parent.parent)};raw=self.file.read_bytes();row={'source_path':self.file.relative_to(self.ctx.root).as_posix(),'source_bytes':len(raw),'source_sha256':hashlib.sha256(raw).hexdigest(),'outcome':'excluded','exception':'no_public_messages'}
  self.file.write_bytes(raw+b'new user message\n');self.assertEqual('changed',review_state(self.ctx,item,row,lambda _: {})['review_state']);self.assertIsNone(SourceVisibility(self.ctx).reason(row['source_path']))

if __name__=='__main__':unittest.main()
