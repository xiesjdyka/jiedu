import json,tempfile,unittest,os
import hashlib
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.continuous_organization import ContinuousOrganizer
from knowledge_bridge.writer_core import Writer
from knowledge_bridge.passage_search import file_version
from knowledge_bridge import memory_pipeline as memory

class ContinuousTests(unittest.TestCase):
 def setUp(self):
  self.stack=ExitStack();self.addCleanup(self.stack.close);base=Path(self.stack.enter_context(tempfile.TemporaryDirectory()));self.context=initialize_workspace(base/'workspace');self.service=ContinuousOrganizer(self.context)
  for name in ['CANDIDATE_EVENTS','STAGING_EVENTS','AUDIT_EVENTS']:self.stack.enter_context(patch.object(memory,name,base/(name+'.jsonl')))
  self.source='90_原始档案/测试/记录.md';p=self.context.root/self.source;p.parent.mkdir(parents=True);p.write_text('用户确认：盘点10箱通过。上线日期未决定。','utf-8')
  self.targets=['02_项目/测试/当前状态.md','01_当前状态/摘要.md']
  for target in self.targets:
   p=self.context.root/target;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# 测试\n\n## 当前进度\n等待验收。\n\n## 人工备注\n保留架位。\n','utf-8')
 def configure(self,automatic=True):
  return self.service.configure({'enabled':True,'automatic':automatic,'client_id':'fixture','source_roots':['90_原始档案/测试'],'targets':[{'path':p,'sections':['当前进度']} for p in self.targets]})
 def plan(self,conflicts=False):
  key=self.service.observe(self.source);pages={}
  for p in [self.source]+self.targets:
   offset=0;parts=[]
   while True:
    r=self.service.read(key,'fixture',p,offset,32);parts.append(r['content']);offset+=r['returned_bytes']
    if r['complete']:break
   pages[p]=r|{'content':''.join(parts)}
  r=pages[self.source];quote=r['content'];ev={'source_path':self.source,'version':r['version'],'byte_start':0,'byte_end':len(quote.encode()),'quote':quote}
  return key,[{'target_path':p,'changes':{'当前进度':'盘点10箱通过；上线未决定。'},'reason':'虚构结果，仅检验程序边界','evidence':[ev],'conflicts':['有争议'] if conflicts else [],'uncertainties':[]} for p in self.targets]
 def test_disabled_has_no_scan_or_queue(self):
  with patch.object(Path,'rglob',side_effect=AssertionError('must not scan')):self.service.reconcile();self.assertIsNone(self.service.capture(self.source))
  self.assertFalse(self.service.path.exists());self.assertFalse(self.service.policy_path.exists())
 def test_duplicate_event_and_partial_read(self):
  self.configure();key=self.service.observe(self.source);self.assertEqual(key,self.service.observe(self.source));r=self.service.read(key,'fixture',self.source,0,12);self.assertFalse(r['complete'])
  self.assertEqual(1,len(ContinuousOrganizer(self.context).status()['tasks']))
  with self.assertRaises(ValueError):self.service.read(key,'fixture',self.source,999,12)
 def test_auto_exact_sections_idempotent(self):
  self.configure();key,plan=self.plan();result=self.service.propose(key,'fixture',plan,{},lambda c:None);self.assertEqual('written',result['status'])
  again=self.service.propose(key,'fixture',plan,{},lambda c:None);self.assertEqual(result['targets'],again['targets']);self.assertEqual(2,len(memory.list_candidates()))
  for target in self.targets:self.assertIn('## 人工备注\n保留架位。',(self.context.root/target).read_text('utf-8'))
 def test_partial_failure_resumes_only_remaining(self):
  self.configure();key,plan=self.plan();original=Writer.apply;calls=[]
  def failing(writer,preview,*args,**kwargs):
   calls.append(preview['relative_target'])
   if preview['relative_target']==self.targets[1]:raise OSError('fictional disk interruption')
   return original(writer,preview,*args,**kwargs)
  with patch.object(Writer,'apply',failing):result=self.service.propose(key,'fixture',plan,{},lambda c:None)
  self.assertEqual('partial',result['status']);first=result['targets'][self.targets[0]]['version'];calls=[]
  def tracked(writer,preview,*args,**kwargs):calls.append(preview['relative_target']);return original(writer,preview,*args,**kwargs)
  with patch.object(Writer,'apply',tracked):result=ContinuousOrganizer(self.context).resume(key,'fixture',lambda c:None)
  self.assertEqual('written',result['status']);self.assertEqual([self.targets[1]],calls);self.assertEqual(first,result['targets'][self.targets[0]]['version'])
 def test_conflict_not_automatic(self):
  self.configure();key,plan=self.plan(True);result=self.service.propose(key,'fixture',plan,{},lambda c:None);self.assertEqual('conflict',result['status']);self.assertIn('等待验收',(self.context.root/self.targets[0]).read_text('utf-8'))
 def test_replan_does_not_reuse_invalid_candidate(self):
  self.configure(False);key,plan=self.plan();first=self.service.propose(key,'fixture',plan,{},lambda c:None)
  self.service.replan(key);_,plan=self.plan();second=self.service.propose(key,'fixture',plan,{},lambda c:None)
  self.assertNotEqual(first['targets'][self.targets[0]]['candidate_id'],second['targets'][self.targets[0]]['candidate_id'])
  with self.assertRaises(PermissionError):self.service.validate_candidate(memory.candidate_source(first['targets'][self.targets[0]]['candidate_id']))
 def test_identical_bytes_new_version_preserves_written_history(self):
  import os
  self.configure();key,plan=self.plan();self.service.propose(key,'fixture',plan,{},lambda c:None)
  p=self.context.root/self.source;stat=p.stat();os.utime(p,ns=(stat.st_atime_ns,stat.st_mtime_ns+10000000))
  self.assertNotEqual(key,self.service.observe(self.source));self.assertEqual('written',self.service.load()['tasks'][key]['status'])
 def test_replan_partial_keeps_written_and_refreshes_remaining_baseline(self):
  self.configure();key,plan=self.plan();original=Writer.apply
  def fail(writer,preview,*a,**kw):
   if preview['relative_target']==self.targets[1]:raise OSError('fictional failure')
   return original(writer,preview,*a,**kw)
  with patch.object(Writer,'apply',fail):first=self.service.propose(key,'fixture',plan,{},lambda c:None)
  p=self.context.root/self.targets[1];p.write_bytes(p.read_bytes()+b'\nExternal manual note\n')
  self.service.replan(key);_,plan=self.plan();last=self.service.propose(key,'fixture',plan[1:],{},lambda c:None)
  self.assertEqual('written',last['status']);self.assertEqual(first['targets'][self.targets[0]]['version'],last['targets'][self.targets[0]]['version'])
  self.assertIn(b'External manual note',p.read_bytes())
 def test_external_target_and_source_change(self):
  self.configure(False);key,plan=self.plan();(self.context.root/self.targets[0]).write_text('人工新改动','utf-8')
  with self.assertRaises(ValueError):self.service.propose(key,'fixture',plan,{},lambda c:None)
  (self.context.root/self.source).write_text('新的证据','utf-8')
  with self.assertRaises(ValueError):self.service.resume(key,'fixture',lambda c:None)
  self.assertNotEqual(key,self.service.observe(self.source))
 def test_revocation_and_policy_change(self):
  self.configure(False);key,plan=self.plan();self.service.propose(key,'fixture',plan,{},lambda c:None)
  def revoked(c):raise PermissionError('fixture client revoked')
  result=self.service.resume(key,'fixture',revoked);self.assertEqual('failed',result['status']);self.assertIn('等待验收',(self.context.root/self.targets[0]).read_text('utf-8'))
  self.configure(True)
  with self.assertRaises(PermissionError):self.service.resume(key,'fixture',lambda c:None)
 def test_unsafe_target_sections_and_model_privilege(self):
  with self.assertRaises(ValueError):self.service.configure({'enabled':True,'automatic':True,'client_id':'fixture','source_roots':['90_原始档案/测试'],'targets':[{'path':'00_系统/画像.md','sections':['个人信息']}]})
  self.configure();key,plan=self.plan();plan[0]['confidence']=1
  with self.assertRaises(ValueError):self.service.propose(key,'fixture',plan,{},lambda c:None)
  with self.assertRaises(PermissionError):self.service.replace_sections('## 当前进度\n旧\n',{'人工备注':'覆盖'},['当前进度'])
  with self.assertRaises(ValueError):self.service.replace_sections('## 当前进度\n旧\n',{'当前进度':'## 核心规则\n覆盖'},['当前进度'])
 def test_watcher_registers_archive_before_index_exclusion(self):
  from knowledge_bridge import watcher
  from watchdog.events import FileCreatedEvent
  self.configure()
  handler=watcher.Handler()
  with patch.object(watcher,'VAULT',self.context.root),patch.object(watcher,'active_workspace',return_value=self.context),patch.object(watcher,'is_allowed',return_value=True):
   handler.on_any_event(FileCreatedEvent(str(self.context.root/self.source)))
   self.assertNotIn('evidence',handler._scope_pending)
   with patch('knowledge_bridge.search_scopes.rebuild_scope_stable') as rebuild:
    handler.flush(force=True);rebuild.assert_not_called()
  self.assertEqual(1,len(self.service.status()['tasks']))
 def test_watcher_ignores_internal_runtime_events(self):
  from knowledge_bridge import watcher
  from watchdog.events import FileModifiedEvent
  handler=watcher.Handler();internal=self.context.root/'.jiedu/index/evidence/manifest.json';internal.parent.mkdir(parents=True,exist_ok=True);internal.write_text('{}','utf-8')
  with patch.object(watcher,'VAULT',self.context.root),patch('knowledge_bridge.continuous_organization.ContinuousOrganizer') as organizer:
   handler.on_any_event(FileModifiedEvent(str(internal)))
  organizer.assert_not_called();self.assertFalse(handler._pending);self.assertFalse(handler._scope_pending)
 def test_watcher_waits_for_archive_quiet_before_scope_rescan(self):
  from knowledge_bridge import watcher
  handler=watcher.Handler()
  with patch.object(watcher.time,'monotonic',return_value=10):handler._mark_scope(('02_项目','测试','当前状态.md'))
  with patch('knowledge_bridge.search_scopes.rebuild_scope_stable') as rebuild,patch.object(watcher.time,'monotonic',return_value=14):
   self.assertIsNone(handler.flush());rebuild.assert_not_called()
  with patch('knowledge_bridge.search_scopes.rebuild_scope_stable',return_value={'stable_snapshot':True}) as rebuild,patch.object(watcher.time,'monotonic',return_value=15.1):
   handler.flush();rebuild.assert_called_once()
 def test_watcher_keeps_all_public_roots_and_incremental_formal_updates(self):
  from knowledge_bridge import watcher
  from watchdog.events import FileModifiedEvent
  self.assertEqual(('00_系统','01_当前状态','02_项目','03_知识','90_原始档案','99_收件箱'),watcher.WATCH_ROOTS)
  formal=self.context.root/'02_项目/测试/当前状态.md';handler=watcher.Handler()
  with patch.object(watcher,'VAULT',self.context.root),patch.object(watcher,'active_workspace',return_value=self.context),patch.object(watcher,'is_allowed',return_value=True):
   handler.on_any_event(FileModifiedEvent(str(formal)))
   self.assertIn('02_项目/测试/当前状态.md',handler._pending);self.assertIn('formal',handler._scope_pending)
   with patch.object(watcher,'sync',return_value={'status':'incremental_synced'}) as sync,patch('knowledge_bridge.search_scopes.rebuild_scope_stable',return_value={'stable_snapshot':True}):handler.flush(force=True)
  sync.assert_called_once()

 def project_message(self,session,message_id,content,role='assistant'):
  path=f'90_原始档案/连续整理/{message_id}.txt';target=self.context.root/path;target.parent.mkdir(parents=True,exist_ok=True);raw=content.encode();target.write_bytes(raw)
  version=hashlib.sha256(raw).hexdigest()
  return {'session_id':session,'message_id':message_id,'version':version,'content':content,'role':role,'evidence':{'source_path':path,'version':file_version(target.stat()),'byte_start':0,'byte_end':len(raw),'quote':content}}
 def project_spec(self,rows,content='示例来源归档已暂停；回到检索与知识整理主线。',status_claim='reported_complete'):
  kinds=['user_decision','planner_restatement','execution_report','independent_verification']
  target=self.context.root/'02_项目/界渡/当前状态.md'
  return {'project':'界渡','issue_key':'pause-fixture-return-retrieval','target_path':'02_项目/界渡/当前状态.md','section':'当前进度','content':content,'reason':'跨对话续并实例','sources':[{'session_id':r['session_id'],'message_id':r['message_id'],'version':r['version'],'kind':kinds[i]} for i,r in enumerate(rows)],'substantive':True,'status_claim':status_claim,'expected_target_hash':hashlib.sha256(target.read_bytes()).hexdigest()}
 def setup_project_target(self):
  target=self.context.root/'02_项目/界渡/当前状态.md';target.parent.mkdir(parents=True,exist_ok=True);target.write_text('# 界渡\n\n## 当前进度\n旧状态。\n','utf-8')
  self.service.configure({'enabled':True,'automatic':False,'client_id':'fixture','source_roots':['90_原始档案/连续整理'],'targets':[{'path':'02_项目/界渡/当前状态.md','sections':['当前进度']}]})
  consume=self.service.consume_project_delta;propose=self.service.propose_project_issue
  self.service.consume_project_delta=lambda project,source,version,rows,*rest:consume(project,source,version,rows,*(rest or ('fixture',)))
  self.service.propose_project_issue=lambda client,spec,*rest:propose(client,spec,*(rest or ({'scopes':['evidence'],'revision':'fixture'},lambda candidate:None)))
  return target
 def fixture_project_rows(self):
  return [
   self.project_message('fixture-session-a','fixture-user-a','暂停示例来源的自动归档，保留读取权限。其他来源继续使用；下一步检查检索和知识整理。','user'),
   self.project_message('fixture-session-a','fixture-planner-a','已记录暂停示例来源归档的决定。将只读核对检索覆盖，并整理知识处理的下一步。'),
   self.project_message('fixture-session-b','fixture-executor-b','示例来源自动归档已停止，写入权限为 permission_denied。读取权限和连接身份保留，另外两个测试来源未受影响。下一步只读核查三个检索范围。'),
  ]
 def test_project_message_delta_is_versioned_order_free_and_small(self):
  self.setup_project_target();rows=self.fixture_project_rows();long=[]
  for i in range(120):long.append(self.project_message('long',f'z-{119-i:03}',f'历史消息{i}'))
  first=self.service.consume_project_delta('界渡','long','v1',long)
  addition=self.project_message('long','a-000','新增一条')
  second=self.service.consume_project_delta('界渡','long','v2',[addition])
  self.assertEqual(120,len(first['changed']));self.assertEqual(1,len(second['changed']))
  self.assertEqual(len(addition['content'].encode()),second['model_deliverable_bytes']);self.assertEqual('none',second['ordering_assumption'])
  changed=dict(long[57]);changed['content']='历史消息57（本地修订）';changed['version']=hashlib.sha256(changed['content'].encode()).hexdigest();p=self.context.root/changed['evidence']['source_path'];p.write_text(changed['content'],'utf-8');changed['evidence']|={'version':file_version(p.stat()),'byte_end':len(changed['content'].encode()),'quote':changed['content']}
  local=self.service.consume_project_delta('界渡','long','v3',[changed]);self.assertEqual('modified',local['changed'][0]['change'])
 def test_project_issue_merges_three_roles_idempotently_without_writing(self):
  target=self.setup_project_target();rows=self.fixture_project_rows()
  self.service.consume_project_delta('界渡','real-a','v1',rows[:2]);first=self.service.propose_project_issue('fixture',self.project_spec(rows[:2]))
  self.assertTrue(first['candidate_created']);self.assertEqual('旧状态。',(target.read_text('utf-8').split('## 当前进度\n',1)[1].strip()))
  self.service.validate_candidate(memory.candidate_source(first['candidate_id']))
  duplicate=self.service.propose_project_issue('fixture',self.project_spec(rows[:2]));self.assertEqual(first['candidate_id'],duplicate['candidate_id']);self.assertFalse(duplicate['candidate_merged'])
  self.service.consume_project_delta('界渡','real-b','v2',[rows[2]])
  merged=self.service.propose_project_issue('fixture',self.project_spec(rows,'示例来源归档已停；检索与知识整理主线继续。'))
  self.assertEqual(first['candidate_id'],merged['candidate_id']);self.assertTrue(merged['candidate_merged']);candidate=memory.candidate_source(first['candidate_id'])
  self.assertEqual(3,len(candidate['evidence']));self.assertEqual(['user_decision','planner_restatement','execution_report'],[x['kind'] for x in candidate['provenance']['continuity']['source_roles']])
  again=self.service.propose_project_issue('fixture',self.project_spec(rows,'示例来源归档已停；检索与知识整理主线继续。'));self.assertFalse(again['candidate_merged']);self.assertEqual(1,len(memory.list_candidates()))
  ledger=self.service.load_project('界渡');self.assertTrue(all(ledger['messages'][self.service.message_key(r['session_id'],r['message_id'])]['processed_issues'] for r in rows))
 def test_project_review_edit_refreshes_preview_and_rejects_old_preview(self):
  from fastapi import HTTPException
  from knowledge_bridge import web
  target=self.setup_project_target();row=self.project_message('review-session','review-message','用户确认本次进度。','user')
  self.service.consume_project_delta('界渡','review-source','v1',[row])
  spec={'project':'界渡','issue_key':'review-edit-boundary','target_path':'02_项目/界渡/当前状态.md',
        'section':'当前进度','content':'已完成，待复核。','reason':'项目进度提议',
        'sources':[{'session_id':row['session_id'],'message_id':row['message_id'],'version':row['version'],'kind':'user_decision'}],
        'substantive':True,'expected_target_hash':hashlib.sha256(target.read_bytes()).hexdigest()}
  created=self.service.propose_project_issue('fixture',spec);cid=created['candidate_id']
  writer=Writer(self.context.root,self.context.versions/'formal-memory')
  def preview(relative,title,content,evidence,supersedes,mode,**options):
   self.assertEqual('production',mode)
   return writer.preview(relative,title,content,evidence,supersedes,**options)
  with patch.object(memory,'active_workspace',return_value=self.context),patch.object(web,'active_workspace',return_value=self.context),patch.object(web,'_validate_external_candidate_authorization',return_value=None),patch.object(web,'production_preview',side_effect=preview):
   original=memory.candidate_source(cid);old_preview=web._candidate_preview(original)
   with self.assertRaisesRegex(ValueError,'已授权目标章节'):
    memory.edit_candidate(cid,{'content':original['content'].replace('# 界渡','# 非授权标题')})
   edited=memory.edit_candidate(cid,{'content':original['content'].replace('已完成，待复核。','已完成。')})
   receipt=edited['provenance']['continuity']
   self.assertEqual('candidate_review_edit',receipt['review_edit']['origin'])
   self.assertEqual(receipt['content_hash'],receipt['review_edit']['original_model_content_hash'])
   self.service.validate_candidate(edited)
   new_preview=web._candidate_preview(edited)
   self.assertIn('已完成。',new_preview['diff'])
   self.assertNotEqual(old_preview['context']['candidate_digest'],new_preview['context']['candidate_digest'])
   with patch.object(web,'candidate_source',return_value=edited):
    with self.assertRaises(HTTPException) as denied:
     web.api_candidate_writer_apply.__wrapped__(cid,web.CandidateWriteApplyRequest(preview=old_preview,owner_confirmed=True),None)
   self.assertEqual(400,denied.exception.status_code)
   self.assertEqual('# 界渡\n\n## 当前进度\n旧状态。\n',target.read_text('utf-8'))
   writer.apply(new_preview,owner_confirmed=True,validate=lambda _:self.service.validate_candidate(memory.candidate_source(cid)))
   self.assertIn('## 当前进度\n已完成。',target.read_text('utf-8'))
 def test_project_issue_recovers_creation_progress_interruption_without_duplicate(self):
  self.setup_project_target();rows=self.fixture_project_rows()[:2];self.service.consume_project_delta('界渡','real','v1',rows);spec=self.project_spec(rows)
  from knowledge_bridge import continuous_organization as module
  original=module.save;failed={'done':False}
  def interrupt(path,value):
   if path==self.service.project_path and value.get('projects',{}).get('界渡',{}).get('issues') and not failed['done']:
    failed['done']=True;raise OSError('模拟候选创建后进度落盘中断')
   return original(path,value)
  with patch.object(module,'save',interrupt):
   with self.assertRaises(OSError):self.service.propose_project_issue('fixture',spec)
  recovered=self.service.propose_project_issue('fixture',spec)
  self.assertFalse(recovered['candidate_created']);self.assertEqual(1,len(memory.list_candidates()))
 def test_project_issue_requires_independent_acceptance_and_rejects_stale_source_or_target(self):
  target=self.setup_project_target();rows=self.fixture_project_rows();self.service.consume_project_delta('界渡','real','v1',rows)
  with self.assertRaises(ValueError):self.service.propose_project_issue('fixture',self.project_spec(rows,status_claim='accepted'))
  first=self.service.propose_project_issue('fixture',self.project_spec(rows))
  unchanged=self.project_spec(rows);unchanged['substantive']=False;self.assertEqual('no_substantive_change',self.service.propose_project_issue('fixture',unchanged)['status']);self.assertEqual(1,len(memory.list_candidates()))
  target.write_text(target.read_text('utf-8')+'\n人工改动\n','utf-8')
  with self.assertRaises(ValueError):self.service.validate_candidate(memory.candidate_source(first['candidate_id']))
  with self.assertRaises(ValueError):self.service.propose_project_issue('fixture',self.project_spec(rows,'新内容'))
  target.write_text('# 界渡\n\n## 当前进度\n旧状态。\n','utf-8')
  changed=dict(rows[0]);changed['content']+='补充';changed['version']=hashlib.sha256(changed['content'].encode()).hexdigest();p=self.context.root/changed['evidence']['source_path'];p.write_bytes(changed['content'].encode());changed['evidence']|={'version':file_version(p.stat()),'byte_end':len(changed['content'].encode()),'quote':changed['content']};self.service.consume_project_delta('界渡','real','v2',[changed])
  with self.assertRaises(ValueError):self.service.propose_project_issue('fixture',self.project_spec(rows,'新内容'))
 def test_project_archive_real_jsonl_append_restart_and_local_recheck(self):
  from knowledge_bridge.archive_store import ArchiveStore
  self.setup_project_target();store=ArchiveStore(self.context,recover=False);store.sources.register('continuity_fixture','连续整理','fixture','stdio')
  session=store.ensure_session(source_key='continuity_fixture',source_session_id='long-session',title='长对话')
  for i in range(120):store.append_message(session['session_key'],message_id=f'z-{119-i:03}',role='user',content=f'历史消息{i}',materialize=False)
  item=store._registry()['sessions'][session['session_key']];raw=self.context.raw_message_store/(session['session_key']+'.jsonl');store._materialize(item,raw)
  path=self.context.root/item['relative_path']/'03_元数据/messages.jsonl';relative=self.context.relative(path)
  first=self.service.consume_project_archive('界渡','fixture',relative,'long-session')
  self.assertEqual(120,len(first['changed']));self.assertEqual(path.stat().st_size,first['physical_bytes_read'])
  new={'session_key':'long-session','message_id':'a-000','role':'user','original_text':'新增一条','content_sha256':hashlib.sha256('新增一条'.encode()).hexdigest()}
  encoded=(json.dumps(new,ensure_ascii=False,separators=(',',':'))+'\n').encode()
  with path.open('ab') as handle:handle.write(encoded)
  service=ContinuousOrganizer(self.context);second=service.consume_project_archive('界渡','fixture',relative,'long-session')
  self.assertEqual(1,len(second['changed']));self.assertEqual('rescan',second['scan']);self.assertGreaterEqual(second['physical_bytes_read'],path.stat().st_size)
  self.assertEqual(len('新增一条'.encode()),second['model_deliverable_bytes']);self.assertEqual('a-000',second['changed'][0]['key'].split('\0')[1])
  self.assertEqual('unchanged',ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long-session')['scan'])
  lines=path.read_bytes().splitlines(keepends=True);old_line=lines[57];local_row=json.loads(old_line);local_row['original_text']='历史修订57';local_row['content_sha256']=hashlib.sha256(local_row['original_text'].encode()).hexdigest();newline=b'\r\n' if old_line.endswith(b'\r\n') else b'\n';new_line=json.dumps(local_row,ensure_ascii=False,separators=(',',':')).encode()+newline;self.assertEqual(len(old_line),len(new_line));lines[57]=new_line;path.write_bytes(b''.join(lines))
  local=ContinuousOrganizer(self.context).recheck_project_message('界渡','fixture','long-session',local_row['message_id'])
  self.assertEqual('local_message_recheck',local['scan']);self.assertEqual(len(new_line),local['physical_bytes_read']);self.assertEqual(len(local_row['original_text'].encode()),local['model_deliverable_bytes']);self.assertEqual(1,len(local['changed']))
  rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()];rows[57]['original_text']='历史消息57改';rows[57]['content_sha256']=hashlib.sha256(rows[57]['original_text'].encode()).hexdigest()
  replacement=path.with_suffix('.new');replacement.write_bytes((''.join(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n' for row in rows)).encode());os.replace(replacement,path)
  modified=ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long-session')
  self.assertEqual('rescan',modified['scan']);self.assertEqual(1,len(modified['changed']));self.assertEqual('modified',modified['changed'][0]['change']);self.assertGreaterEqual(modified['physical_bytes_read'],path.stat().st_size)
  store.append_message(session['session_key'],message_id='adapter-new',role='user',content='适配器新消息',materialize=True)
  rewritten=ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long-session')
  self.assertEqual('rescan',rewritten['scan']);self.assertGreaterEqual(rewritten['physical_bytes_read'],path.stat().st_size)
  self.assertLess(rewritten['model_deliverable_bytes'],rewritten['physical_bytes_read'])
  ledger=ContinuousOrganizer(self.context).load_project('界渡')
  self.assertTrue(ledger['messages'][self.service.message_key('long-session','a-000')]['source_missing'])
 def test_project_archive_501_messages_resume_and_long_message_bounded_evidence(self):
  self.setup_project_target();path=self.context.root/'90_原始档案/连续整理/长会话/03_元数据/messages.jsonl';path.parent.mkdir(parents=True)
  def encoded(mid,content):return (json.dumps({'session_key':'long','message_id':mid,'role':'user','original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False,separators=(',',':'))+'\n').encode()
  path.write_bytes(b''.join(encoded(f'm-{i}',f'消息{i}') for i in range(501)))
  relative=self.context.relative(path);first=self.service.consume_project_archive('界渡','fixture',relative,'long')
  self.assertEqual(500,len(first['changed']));self.assertIsNotNone(first['next_offset'])
  second=ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long')
  self.assertEqual('rescan_page',second['scan']);self.assertEqual(1,len(second['changed']));self.assertIsNone(second['next_offset'])
  self.assertEqual(501,len(ContinuousOrganizer(self.context).load_project('界渡')['messages']))
  with path.open('ab') as handle:handle.write(encoded('m-501','追加消息'))
  first_recheck=ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long')
  pending=ContinuousOrganizer(self.context).load_project('界渡')['messages'][self.service.message_key('long','m-500')]
  self.assertIsNotNone(first_recheck['next_offset']);self.assertTrue(pending['source_recheck_pending']);self.assertFalse(pending.get('source_missing',False))
  ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'long')
  with path.open('ab') as handle:handle.write(encoded('long-answer','x'*9000))
  batches=[];service=ContinuousOrganizer(self.context)
  while True:
   page=service.consume_project_archive('界渡','fixture',relative,'long');batches.append(page)
   if page['next_offset'] is None:break
  self.assertEqual(9000,sum(x['model_deliverable_bytes'] for x in batches))
  row=service.load_project('界渡')['messages'][service.message_key('long','long-answer')]
  self.assertLessEqual(len(row['evidence']['quote'].encode()),4096);self.assertEqual(hashlib.sha256(('x'*9000).encode()).hexdigest(),row['version'])
 def test_project_archive_edited_prefix_plus_append_is_detected(self):
  self.setup_project_target();path=self.context.root/'90_原始档案/连续整理/改写/03_元数据/messages.jsonl';path.parent.mkdir(parents=True)
  def encoded(mid,content):return (json.dumps({'session_key':'s','message_id':mid,'role':'user','original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False,separators=(',',':'))+'\n').encode()
  path.write_bytes(encoded('old','abc')+encoded('second','keep'));relative=self.context.relative(path)
  self.service.consume_project_archive('界渡','fixture',relative,'s')
  path.write_bytes(encoded('old','xyz')+encoded('second','keep')+encoded('third','new'))
  result=ContinuousOrganizer(self.context).consume_project_archive('界渡','fixture',relative,'s')
  self.assertEqual('rescan',result['scan']);self.assertEqual({'old','third'},{item['key'].split('\0')[1] for item in result['changed']})
  self.assertEqual(len('xyznew'.encode()),result['model_deliverable_bytes']);self.assertGreaterEqual(result['physical_bytes_read'],path.stat().st_size)
 def test_project_reviewed_issue_next_day_new_candidate_and_conflict_refresh(self):
  target=self.setup_project_target();rows=self.fixture_project_rows()[:2];self.service.consume_project_delta('界渡','today','v1',rows)
  first=self.service.propose_project_issue('fixture',self.project_spec(rows));memory.review_candidate(first['candidate_id'],'accept')
  old=memory.candidate_source(first['candidate_id']);writer=Writer(self.context.root,self.context.versions/'formal-memory')
  preview=writer.preview('02_项目/界渡/当前状态.md','第一天',old['content'],old['evidence'],action='update',expected_hash=old['provenance']['expected_target_hash'])
  applied=writer.apply(preview,owner_confirmed=True);memory.mark_candidate_written(first['candidate_id'],applied['version'])
  extra=self.project_message('tomorrow','next-day','第二天完成独立核对。');self.service.consume_project_delta('界渡','tomorrow','v2',[extra])
  spec=self.project_spec(rows+[extra],'第二天进展：待复核。');spec['conflicts']=['执行报告与现场数据不一致'];spec['uncertainties']=['等待现场核查']
  second=self.service.propose_project_issue('fixture',spec)
  self.assertNotEqual(first['candidate_id'],second['candidate_id']);self.assertEqual(2,len(memory.list_candidates()))
  candidate=memory.candidate_source(second['candidate_id']);self.assertEqual(['possible'],candidate['conflicts']);self.assertEqual(['等待现场核查'],candidate['provenance']['uncertainties'])
  self.assertEqual(first['candidate_id'],candidate['provenance']['continuity']['previous_candidate_id']);self.assertEqual('written',memory.candidate_source(first['candidate_id'])['status'])
  repeat=self.service.propose_project_issue('fixture',spec);self.assertEqual(second['candidate_id'],repeat['candidate_id']);self.assertFalse(repeat['candidate_created'])
 def test_project_pending_merge_refreshes_conflict_and_requires_policy_baseline(self):
  self.setup_project_target();rows=self.fixture_project_rows()[:2];self.service.consume_project_delta('界渡','today','v1',rows)
  first=self.service.propose_project_issue('fixture',self.project_spec(rows[:1]))
  spec=self.project_spec(rows,'进展出现相反证据，等待核对。');spec['conflicts']=['来源说法相反'];spec['uncertainties']=['待独立核对']
  merged=self.service.propose_project_issue('fixture',spec);self.assertEqual(first['candidate_id'],merged['candidate_id'])
  candidate=memory.candidate_source(first['candidate_id']);self.assertEqual(['来源说法相反'],candidate['conflicts']);self.assertEqual(['待独立核对'],candidate['provenance']['uncertainties'])
  missing=self.project_spec(rows);missing.pop('expected_target_hash')
  with self.assertRaises(ValueError):self.service.propose_project_issue('fixture',missing)
  self.service.configure({'enabled':False,'automatic':False,'client_id':'fixture','source_roots':['90_原始档案/连续整理'],'targets':[{'path':'02_项目/界渡/当前状态.md','sections':['当前进度']}]})
  with self.assertRaises(PermissionError):self.service.propose_project_issue('fixture',spec)
 def test_project_identity_separates_configured_projects(self):
  self.setup_project_target();other=self.context.root/'02_项目/别的项目/当前状态.md';other.parent.mkdir(parents=True);other.write_text('# 别的项目\n\n## 当前进度\n待办。\n','utf-8')
  self.service.configure({'enabled':True,'automatic':False,'client_id':'fixture','source_roots':['90_原始档案/连续整理'],'targets':[{'path':'02_项目/界渡/当前状态.md','sections':['当前进度']},{'path':'02_项目/别的项目/当前状态.md','sections':['当前进度']}]})
  row=self.project_message('same-session','same-message','同一消息可被两个显式项目分别引用。')
  service=ContinuousOrganizer(self.context)
  for project in ['界渡','别的项目']:service.consume_project_delta(project,'source','v1',[row],'fixture')
  one=self.project_spec([row]);one['sources'][0]['kind']='user_decision'
  two=one|{'project':'别的项目','target_path':'02_项目/别的项目/当前状态.md','expected_target_hash':hashlib.sha256(other.read_bytes()).hexdigest()}
  receipt={'scopes':['evidence'],'revision':'fixture'}
  first=service.propose_project_issue('fixture',one,receipt,lambda candidate:None)
  second=service.propose_project_issue('fixture',two,receipt,lambda candidate:None)
  self.assertNotEqual(first['candidate_id'],second['candidate_id']);self.assertEqual(2,len(memory.list_candidates()))
 def test_project_batch_bounded_read_submit_and_ack(self):
  target=self.setup_project_target();path=self.context.root/'90_原始档案/连续整理/模型入口/03_元数据/messages.jsonl';path.parent.mkdir(parents=True)
  def line(mid,role,content):return (json.dumps({'session_key':'model-session','message_id':mid,'role':role,'original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False,separators=(',',':'))+'\n').encode()
  path.write_bytes(line('u','user','用户决定：暂停示例来源。')+line('p','assistant','计划复述：回到检索。')+line('n','assistant','无关闲聊。'))
  relative=self.context.relative(path);batch=self.service.project_batch('界渡','fixture',relative,'model-session','当前进度',limit=3)
  self.assertEqual('ready',batch['status']);self.assertEqual(3,len(batch['messages']));self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),batch['expected_target_hash'])
  self.assertFalse(batch['model_actually_consumed']);self.assertGreater(batch['verification_bytes_read'],0);self.assertGreater(batch['source_read_bytes'],0)
  refs=[{'session_id':r['session_id'],'message_id':r['message_id'],'version':r['version']} for r in batch['messages']]
  self.service.acknowledge_project_messages('界渡','fixture',refs[2:],{'scopes':['evidence'],'revision':'fixture'},lambda candidate:None)
  spec={'project':'界渡','issue_key':'pause-doubao','target_path':batch['target_path'],'section':'当前进度','content':'示例来源暂停，检索继续。','reason':'模型判断的同一事项','sources':[refs[0]|{'kind':'user_decision'},refs[1]|{'kind':'planner_restatement'}],'substantive':True,'expected_target_hash':batch['expected_target_hash']}
  first=self.service.propose_project_issue('fixture',spec,{'scopes':['evidence'],'revision':'fixture'},lambda candidate:None)
  self.assertTrue(first['candidate_created']);self.assertEqual(1,len(memory.list_candidates()))
  again=self.service.project_batch('界渡','fixture',relative,'model-session','当前进度')
  self.assertEqual([],again['messages']);self.assertEqual(0,again['model_deliverable_bytes']-len(again['current_section'].encode()))
  self.assertEqual('# 界渡\n\n## 当前进度\n旧状态。\n',target.read_text('utf-8'))

 def test_shared_project_batch_deduplicates_only_full_long_text_and_preserves_refs(self):
  self.setup_project_target()
  content='跨来源完整相同的原始用户决定。'*12
  refs=[]
  for source in ('甲','乙'):
   path=self.context.root/f'90_原始档案/连续整理/{source}/03_元数据/messages.jsonl'
   path.parent.mkdir(parents=True)
   def line(mid,text):return (json.dumps({'session_key':source,'message_id':mid,'role':'user',
    'original_text':text,'content_sha256':hashlib.sha256(text.encode()).hexdigest()},ensure_ascii=False)+'\n').encode()
   path.write_bytes(line('long',content)+line('short','继续'))
   relative=self.context.relative(path)
   self.service.consume_project_archive('界渡','fixture',relative,source)
   refs.append(relative)
  checked=[]
  batch=self.service.shared_project_batch('界渡','fixture','当前进度',checked.append,limit=4)
  self.assertEqual('ready',batch['status'])
  self.assertEqual(4,len(batch['messages']))
  self.assertEqual(set(refs),set(checked))
  longs=[row for row in batch['messages'] if row['message_id']=='long']
  shorts=[row for row in batch['messages'] if row['message_id']=='short']
  self.assertEqual(1,sum(row['content']==content for row in longs))
  self.assertEqual(1,sum(row['duplicate_of'] is not None for row in longs))
  self.assertTrue(all(row['content']=='继续' and row['duplicate_of'] is None for row in shorts))
  self.service.acknowledge_project_messages('界渡','fixture',[
   {key:row[key] for key in ('session_id','message_id','version')} for row in batch['messages']],
   {'scopes':['evidence'],'revision':'fixture'},lambda candidate:None)
  self.assertEqual([],self.service.shared_project_batch('界渡','fixture','当前进度',checked.append)['messages'])

 def test_shared_project_batch_can_advance_past_failed_items_without_acknowledging_them(self):
  self.setup_project_target()
  for i in range(6):
   source=f'source{i}';path=self.context.root/f'90_原始档案/连续整理/{source}/03_元数据/messages.jsonl'
   path.parent.mkdir(parents=True)
   content=f'独立来源 {i} 的用户内容'
   path.write_text(json.dumps({'session_key':source,'message_id':'u','role':'user',
    'original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False)+'\n','utf-8')
   self.service.consume_project_archive('界渡','fixture',self.context.relative(path),source)
  first=self.service.shared_project_batch('界渡','fixture','当前进度',lambda source:None)
  self.assertEqual(4,len(first['messages']))
  second=self.service.shared_project_batch('界渡','fixture','当前进度',lambda source:None,
                                           work_cursor=first['next_work_cursor'])
  self.assertEqual(2,len(second['messages']))
  self.assertFalse(set((x['session_id'],x['message_id']) for x in first['messages']) &
                   set((x['session_id'],x['message_id']) for x in second['messages']))
  self.assertEqual(6,second['pending_count'])
  self.assertIsNone(second['next_work_cursor'])

 def test_shared_cursor_survives_ack_of_earlier_items(self):
  self.setup_project_target()
  for i in range(8):
   source=f'source{i}';path=self.context.root/f'90_原始档案/连续整理/{source}/03_元数据/messages.jsonl'
   path.parent.mkdir(parents=True)
   content=f'用户内容{i}'
   path.write_text(json.dumps({'session_key':source,'message_id':'u','role':'user',
    'original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False)+'\n','utf-8')
   self.service.consume_project_archive('界渡','fixture',self.context.relative(path),source)
  first=self.service.shared_project_batch('界渡','fixture','当前进度',lambda source:None)
  refs=[{key:row[key] for key in ('session_id','message_id','version')} for row in first['messages'][:2]]
  self.service.acknowledge_project_messages('界渡','fixture',refs,
   {'scopes':['evidence'],'revision':'fixture'},lambda candidate:None)
  second=self.service.shared_project_batch('界渡','fixture','当前进度',lambda source:None,
   work_cursor=first['next_work_cursor'])
  self.assertEqual(['source4','source5','source6','source7'],[row['session_id'] for row in second['messages']])
  self.assertEqual(6,second['pending_count'])

 def test_shared_batch_skips_invalid_and_denied_sources_without_exposing_them(self):
  self.setup_project_target();paths=[]
  for i in range(3):
   source=f'source{i}';path=self.context.root/f'90_原始档案/连续整理/{source}/03_元数据/messages.jsonl'
   path.parent.mkdir(parents=True)
   content=f'原始内容{i}'
   path.write_text(json.dumps({'session_key':source,'message_id':'u','role':'user',
    'original_text':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()},ensure_ascii=False)+'\n','utf-8')
   self.service.consume_project_archive('界渡','fixture',self.context.relative(path),source)
   paths.append(path)
  paths[0].write_bytes(paths[0].read_bytes().replace('原始内容0'.encode(),'已改内容0'.encode()))
  def authorize(source):
   if 'source1/' in source:raise PermissionError('此来源已拒绝')
  batch=self.service.shared_project_batch('界渡','fixture','当前进度',authorize)
  self.assertEqual(1,len(batch['messages']))
  self.assertEqual('source2',batch['messages'][0]['session_id'])
  self.assertEqual(1,batch['invalid_evidence_count'])
  self.assertEqual(2,batch['pending_count'])
  self.assertNotIn('source1',json.dumps(batch,ensure_ascii=False))

 def test_mcp_project_tool_accepts_shared_read_operation(self):
  from knowledge_bridge import mcp_server
  with patch.object(mcp_server,'_organizer_broker',return_value={'status':'ready'}) as broker:
   result=mcp_server.continuous_project('read_shared','界渡',{'section':'当前进度'})
  self.assertEqual({'status':'ready'},result)
  broker.assert_called_once_with('project',{'operation':'read_shared','project':'界渡',
                                          'details':{'section':'当前进度'}},'continuous')

if __name__=='__main__':unittest.main()
