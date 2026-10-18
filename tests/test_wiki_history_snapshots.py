import copy,json,shutil,unittest
from unittest.mock import patch
from tests.test_knowledge_wiki import WikiFixture
from knowledge_bridge import memory_pipeline as memory
from knowledge_bridge.organizer import submit,evidence_checked
from knowledge_bridge.writer_core import Writer,sha
from knowledge_bridge.wiki_history import WikiHistory
from knowledge_bridge.wiki_snapshots import WikiSnapshots
from knowledge_bridge.passage_search import file_version

class HistorySnapshotTests(WikiFixture):
 def setUp(self):
  super().setUp()
  for name in ('CANDIDATE_EVENTS','STAGING_EVENTS','AUDIT_EVENTS'):
   filename={'CANDIDATE_EVENTS':'candidate-events.jsonl','STAGING_EVENTS':'approved-staging.jsonl','AUDIT_EVENTS':'audit.jsonl'}[name]
   p=patch.object(memory,name,self.ctx.state/'memory-control'/filename);p.start();self.addCleanup(p.stop)
  self.relative=self.file.relative_to(self.ctx.root).as_posix()
  data=json.loads((self.meta/'manifest.json').read_text('utf-8'));data['source_instance']='instance'
  (self.meta/'manifest.json').write_text(json.dumps(data),'utf-8')
 def old(self,written=False,pending=False):
  job=self.wiki.organizer.start('client','evidence','90_原始档案','legacy')
  page=self.wiki.organizer.read('client',job['job_id'],self.relative,0,65536,authorization_revision='revision')
  ids=[]
  if written or pending:
   ev={'source_path':self.relative,'version':page['version'],'byte_start':0,'byte_end':len(page['content'].encode()),'quote':page['content']}
   result=submit(self.ctx,'client','03_知识/历史.md','# 历史\nCSV','记录格式',[ev],job_id=job['job_id'],request_id='legacy-candidate')
   ids=[result['candidate_id']];candidate=memory.candidate_source(ids[0])
   if written:
    writer=Writer(self.ctx.root,self.ctx.versions/'formal-memory')
    preview=writer.preview('03_知识/历史.md','历史',candidate['content'],candidate['evidence'],action='create',context={'candidate_id':ids[0]})
    meta=writer.apply(preview,True,lambda _:evidence_checked(self.ctx,candidate['evidence']))
    memory.review_candidate(ids[0],'accept');memory.mark_candidate_written(ids[0],meta['version'])
  self.wiki.organizer.finish_source('client',job['job_id'],self.relative,'processed')
  receipt={'session_id':'s','source_session_id':'ordinary','source_instance':'instance','source_path':self.relative,'source_bytes':self.file.stat().st_size,'source_sha256':sha(self.file.read_bytes()),'reviewed_ranges':[{'from':0,'to':self.file.stat().st_size}],'outcome':'written' if written or pending else 'no_change','candidate_ids':ids,'bridge_job_id':job['job_id'],'bridge_version':page['version']}
  self.receipt(receipt)
  return job,receipt,ids
 def receipt(self,row):
  (self.ctx.state/'organization-review-receipt.json').write_text(json.dumps({'schema_version':1,'workspace_id':self.ctx.workspace_id,'rows':[row]}),'utf-8')
 def test_actual_written_and_no_change_reuse_without_job_mutation(self):
  job,receipt,ids=self.old(written=True);old=self.wiki.organizer.path(job['job_id']).read_bytes()
  result=self.claim();self.assertEqual('up_to_date',result['status']);self.assertEqual(1,result['reused_sources'])
  self.assertEqual(old,self.wiki.organizer.path(job['job_id']).read_bytes());self.assertEqual([],self.wiki.load()['jobs'])
  self.assertTrue(self.wiki.load()['history_reuse'][self.relative]['results'])
 def test_no_change_requires_actual_complete_delivery(self):
  job,receipt,_=self.old();self.assertEqual('up_to_date',self.claim()['status'])
  raw=json.loads(self.wiki.organizer.path(job['job_id']).read_text('utf-8'));raw['sources'][0]['offset']=1
  self.wiki.organizer.path(job['job_id']).write_text(json.dumps(raw),'utf-8')
  self.assertEqual('claimed',self.claim()['status'])
 def test_receipt_tampering_rejected(self):
  _,receipt,_=self.old(written=True)
  for changes in ({'source_sha256':'0'*64},{'source_bytes':1},{'source_session_id':'other'},{'source_instance':'other'},{'reviewed_ranges':[{'from':1,'to':receipt['source_bytes']}]},{'reviewed_ranges':[{'from':-1,'to':receipt['source_bytes']}]},{'candidate_ids':[]},{'bridge_job_id':'org_'+'0'*32},{'bridge_version':'0'*64}):
   with self.subTest(changes=changes):
    self.receipt(receipt|changes);self.assertNotIn(self.relative,WikiHistory(self.ctx).completed([]))
 def test_pending_candidate_is_not_completed_history(self):
  self.old(pending=True);result=self.claim();self.assertEqual('claimed',result['status']);self.assertEqual(0,result['reused_sources'])
 def test_distinct_receipt_versions_do_not_shadow_current_proof(self):
  _,receipt,_=self.old(written=True);path=self.ctx.state/'organization-review-receipt.json'
  old=receipt|{'source_bytes':receipt['source_bytes']-1,'source_sha256':'0'*64,'outcome':'no_change','candidate_ids':[]}
  for rows in ([old,receipt],[receipt,old]):
   path.write_text(json.dumps({'schema_version':1,'workspace_id':self.ctx.workspace_id,'rows':rows}),'utf-8')
   self.assertIn(self.relative,WikiHistory(self.ctx).completed([]))
  path.write_text(json.dumps({'schema_version':1,'workspace_id':self.ctx.workspace_id,'rows':[receipt,receipt]}),'utf-8')
  self.assertNotIn(self.relative,WikiHistory(self.ctx).completed([]))
 def test_rejected_old_candidate_with_verified_same_target_replacement_is_complete(self):
  job,receipt,ids=self.old(written=True);written=memory.candidate_source(ids[0])
  old=submit(self.ctx,'client','03_知识/历史.md','# 旧版\n需要修正','旧解释',written['evidence'],action='update',expected_target_hash=sha((self.ctx.root/'03_知识/历史.md').read_bytes()),job_id=job['job_id'],request_id='old-rejected')['candidate_id']
  memory.review_candidate(old,'reject')
  proof=WikiHistory(self.ctx).completed([])[self.relative]
  self.assertTrue(any(p.get('replacement_candidate_id')==ids[0] for p in proof['results']))
  row=self.wiki.organizer.load(job['job_id'],'client')['sources'][0]
  history=WikiHistory(self.ctx);history.candidates[old]['status']='pending'
  self.assertIsNone(history.complete_row(row,self.file.read_bytes()))
  history=WikiHistory(self.ctx);history.candidates[old]['provenance']['target_path']='03_知识/其他.md'
  self.assertIsNone(history.complete_row(row,self.file.read_bytes()))
  history=WikiHistory(self.ctx);history.candidates[ids[0]]['status']='approved_staging'
  self.assertIsNone(history.complete_row(row,self.file.read_bytes()))
 def test_duplicate_stable_identity_is_not_reused(self):
  self.old(written=True);shutil.copytree(self.file.parent.parent,self.file.parent.parent.with_name('duplicate'))
  self.assertNotIn(self.relative,WikiHistory(self.ctx).completed([]))
 def test_modified_writer_result_is_not_success(self):
  self.old(written=True);(self.ctx.root/'03_知识/历史.md').write_text('用户另行修改','utf-8')
  self.assertNotIn(self.relative,WikiHistory(self.ctx).completed([]))
 def test_snapshot_append_finishes_and_next_batch_reads_only_verified_prefix_tail(self):
  first=self.claim();row=first['items'][0];original=self.file.read_bytes()
  self.wiki.organizer.read('client',first['job_id'],self.relative,0,65536,authorization_revision='revision')
  self.file.write_bytes(original+'新增知识\n'.encode())
  restored=type(self.wiki)(self.ctx);self.assertEqual(first['job_id'],restored.claim('client',lambda _:None)['job_id'])
  self.wiki.organizer.finish_source('client',first['job_id'],self.relative,'processed')
  self.assertEqual('batch_complete',self.claim()['status']);self.assertEqual(1,len(self.wiki.load()['jobs']))
  second=self.claim(new_batch=True);self.assertEqual(len(original),second['items'][0]['offset'])
  page=self.wiki.organizer.read('client',second['job_id'],self.relative,len(original),65536,authorization_revision='revision')
  self.assertEqual('新增知识\n',page['content'])
 def test_changed_prefix_and_missing_old_bytes_cannot_inherit_cursor(self):
  first=self.claim();self.wiki.organizer.read('client',first['job_id'],self.relative,0,65536,authorization_revision='revision')
  self.wiki.organizer.finish_source('client',first['job_id'],self.relative,'processed')
  WikiSnapshots(self.ctx).path(first['items'][0]['snapshot_id']).unlink();self.file.write_bytes(b'changed\n')
  second=self.claim(new_batch=True);self.assertEqual(0,second['items'][0]['offset'])
 def test_new_files_are_not_pulled_into_active_batch(self):
  first=self.claim();extra=self.ctx.raw_archive/'独立导入资料.md';extra.write_text('真实资料','utf-8')
  self.complete(first);self.assertEqual('batch_complete',self.claim()['status'])
  second=self.claim(new_batch=True);self.assertEqual(1,second['total']);self.assertTrue(second['items'][0]['source_path'].endswith('独立导入资料.md'))
 def snapshot_evidence(self):
  job=self.claim();page=self.wiki.organizer.read('client',job['job_id'],self.relative,0,65536,authorization_revision='revision')
  return job,{k:page[k] for k in ('source_path','version','snapshot_id','snapshot_job_id')}|{'byte_start':0,'byte_end':len(page['content'].encode()),'quote':page['content']}
 def test_snapshot_candidate_uses_existing_queue_writer_and_rejects_stale_apply(self):
  job,ev=self.snapshot_evidence()
  result=submit(self.ctx,'client','03_知识/新知识.md','# 新知识\nCSV','知识',[ev],job_id=job['job_id'],request_id='new')
  candidate=memory.candidate_source(result['candidate_id']);writer=Writer(self.ctx.root,self.ctx.versions/'formal-memory')
  preview=writer.preview('03_知识/新知识.md','知识',candidate['content'],candidate['evidence'],action='create',context={'candidate_id':candidate['candidate_id']})
  self.file.write_text('来源后来变化','utf-8')
  with self.assertRaisesRegex(ValueError,'快照之后'):writer.apply(preview,True,lambda _:evidence_checked(self.ctx,candidate['evidence']))
  self.assertFalse((self.ctx.root/'03_知识/新知识.md').exists());self.assertEqual('pending',memory.candidate_source(result['candidate_id'])['status'])
 def test_snapshot_forged_quote_cross_client_and_unread_finish_rejected(self):
  job=self.claim()
  with self.assertRaises(ValueError):self.wiki.organizer.finish_source('client',job['job_id'],self.relative,'skipped')
  job,ev=self.snapshot_evidence()
  with self.assertRaises(ValueError):evidence_checked(self.ctx,[ev|{'quote':'假'}])
  with self.assertRaises(PermissionError):submit(self.ctx,'other','03_知识/假.md','假','假',[ev],job_id=job['job_id'],request_id='bad')
 def test_authorization_change_rejects_inherited_resume(self):
  first=self.claim();self.wiki.organizer.read('client',first['job_id'],self.relative,0,65536,authorization_revision='old');self.wiki.organizer.finish_source('client',first['job_id'],self.relative,'processed')
  self.file.write_bytes(self.file.read_bytes()+b'append')
  second=self.claim(new_batch=True)
  with self.assertRaisesRegex(ValueError,'授权已变化'):self.wiki.organizer.read('client',second['job_id'],self.relative,second['items'][0]['offset'],65536,authorization_revision='new')
 def test_quota_and_dedup(self):
  store=WikiSnapshots(self.ctx)
  with patch('knowledge_bridge.wiki_snapshots.MAX_STORE',1):
   with self.assertRaisesRegex(ValueError,'容量上限'):self.claim()
  self.assertFalse(self.wiki.load()['enabled'])
  captured=store.capture([self.relative]);store.capture([self.relative]);self.assertEqual(1,len(list(store.root.glob('*.blob'))))
  with patch('knowledge_bridge.wiki_snapshots.MAX_SOURCE',1):
   with self.assertRaises(ValueError):store.capture([self.relative])
 def test_legacy_job_bytes_and_stale_not_reconstructed(self):
  old=self.wiki.organizer.start('client','evidence','90_原始档案','legacy')
  oldpath=self.wiki.organizer.path(old['job_id']);before=oldpath.read_bytes();self.file.write_text('后来内容','utf-8')
  self.claim();self.assertEqual(before,oldpath.read_bytes())
  with self.assertRaises(ValueError):self.wiki.organizer.read('client',old['job_id'],self.relative)
 def test_active_work_beats_stale_phase(self):
  first=self.claim();stored=self.wiki.organizer.load(first['job_id'],'client')
  row=copy.deepcopy(stored['sources'][0]);row.update(source_path=self.relative+'.missing',status='stale');row.pop('snapshot_id');stored['sources'].append(row)
  self.wiki.organizer.path(first['job_id']).write_text(json.dumps(stored),'utf-8')
  self.assertEqual('running',self.wiki.status()['phase'])
 def test_active_legacy_job_survives_upgrade_with_stale_source(self):
  extra=self.ctx.raw_archive/'稳定资料.md';extra.write_text('稳定原文','utf-8');extra_relative=extra.relative_to(self.ctx.root).as_posix()
  old=self.wiki.organizer.start('client','evidence','90_原始档案','legacy-current',source_paths={self.relative,extra_relative})
  state={'workspace_id':self.ctx.workspace_id,'enabled':True,'jobs':[{'job_id':old['job_id'],'client':'client','claimed_at':'2026-01-01T00:00:00+00:00','source_hashes':{r['source_path']:sha((self.ctx.root/r['source_path']).read_bytes()) for r in old['items']}}]}
  self.wiki.path.write_text(json.dumps(state),'utf-8');self.file.write_text('变更来源','utf-8')
  oldpath=self.wiki.organizer.path(old['job_id']);before=oldpath.read_bytes()
  self.assertEqual(old['job_id'],self.claim()['job_id']);self.assertEqual(old['job_id'],self.claim(new_batch=True)['job_id']);self.assertEqual(before,oldpath.read_bytes())
  self.wiki.organizer.read('client',old['job_id'],extra_relative,0,65536);self.wiki.organizer.finish_source('client',old['job_id'],extra_relative,'processed')
  self.assertEqual('legacy_snapshot_unavailable',self.claim()['status'])
  before=oldpath.read_bytes();self.assertNotEqual(old['job_id'],self.claim(new_batch=True)['job_id']);self.assertEqual(before,oldpath.read_bytes())
 def test_explicit_legacy_boundary_transition_reuses_history_without_rewriting_old_job(self):
  self.old(written=True)
  stale=[self.ctx.raw_archive/f'活动资料{i}.md' for i in range(2)]
  for path in stale:path.write_text('历史字节'+path.name,'utf-8')
  new=self.ctx.raw_archive/'真正新增.md';new.write_text('新知识','utf-8')
  paths={self.relative,*[p.relative_to(self.ctx.root).as_posix() for p in stale],new.relative_to(self.ctx.root).as_posix()}
  current=self.wiki.organizer.start('client','evidence','90_原始档案','current-legacy',source_paths=paths)
  state={'workspace_id':self.ctx.workspace_id,'enabled':True,'jobs':[{'job_id':current['job_id'],'client':'client','claimed_at':'2026-01-01T00:00:00+00:00','source_hashes':{r['source_path']:sha((self.ctx.root/r['source_path']).read_bytes()) for r in current['items']}}]}
  self.wiki.path.write_text(json.dumps(state),'utf-8')
  for path in stale:path.write_text('新版本'+path.name,'utf-8')
  path=self.wiki.organizer.path(current['job_id']);before=path.read_bytes()
  with self.assertRaises(ValueError):self.wiki.claim('other',lambda _:None,new_batch=True,close_legacy=True)
  result=self.claim(new_batch=True,close_legacy=True)
  self.assertEqual(3,result['total']);self.assertEqual(1,result['reused_sources']);self.assertEqual(before,path.read_bytes())
  self.assertEqual(3,sum(self.wiki.status()['counts'].values()));self.assertIn(current['job_id'],self.wiki.load()['legacy_batch_closures'])

if __name__=='__main__':unittest.main()
