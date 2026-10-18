"""Candidate-bound renewal jobs over the existing authenticated Organizer API."""
import hashlib,json,uuid
from datetime import datetime,timezone
from filelock import FileLock
from fastapi import HTTPException
from .organizer import Organizer,reject_protected
from .writer_core import Writer,save,sha
from .passage_search import file_version
from .client_access import resolve_document_target

def now():return datetime.now(timezone.utc).isoformat()

class CandidateReorganization:
 def __init__(self,context):
  self.context=context;self.path=context.state/'candidate-reorganization.json';self.organizer=Organizer(context)
 def lock(self):
  self.context.state.mkdir(parents=True,exist_ok=True);return FileLock(str(self.path.with_suffix('.lock')))
 def load(self):
  try:data=json.loads(self.path.read_text('utf-8'))
  except FileNotFoundError:data={}
  if data.get('workspace_id')!=self.context.workspace_id:return {'workspace_id':self.context.workspace_id,'active':{},'batches':{}}
  return data
 def attach(self,result,client,authorize,readiness,revision,*,new_batch=False,run_id=None,existing_batch_id=None):
  from .memory_pipeline import list_candidates,candidate_source
  from .knowledge_wiki import KnowledgeWiki
  parent=result.get('job_id') or KnowledgeWiki(self.context).load().get('batch_job_id')
  binding=run_id or parent or 'empty-wiki-batch'
  with self.lock():
   data=self.load();key=data['active'].get(client);batch=data['batches'].get(key)
   if existing_batch_id is not None:
    if not batch or key!=existing_batch_id:raise ValueError('原旧建议批次已变化，请重新检查；未创建新批')
    if all(t.get('resolved') for t in batch['tasks']):return result|{'status':'reorganization_complete','reorganization':{'batch_id':key,'pending':0,'items':[],'fixed_candidate_boundary':True}}
   if not batch or (all(t.get('resolved') for t in batch['tasks']) and (batch['binding']!=binding or new_batch)):
    tasks=[]
    for candidate in list_candidates():
     state=readiness(candidate)
     if state.get('state')!='needs_reorganization':continue
     tasks.append({'old_candidate_id':candidate['candidate_id'],'status':'waiting','failure':state.get('message'),'resolved':False})
    if len(tasks)>1000:raise ValueError('待更新建议超过1000项，请先核对范围；未领取不完整批次')
    key='renew_'+uuid.uuid4().hex;batch={'batch_id':key,'client':client,'binding':binding,'parent_job_id':parent,'run_id':run_id,'created_at':now(),'tasks':tasks};data['batches'][key]=batch;data['active'][client]=key
   fresh_daily_boundary=bool(run_id and run_id not in batch.setdefault('run_ids',[]))
   if fresh_daily_boundary:batch['run_ids'].append(run_id)
   refresh_versions=new_batch or fresh_daily_boundary
   for task in batch['tasks']:
    if task.get('resolved'):continue
    candidate=candidate_source(task['old_candidate_id'])
    if candidate.get('status')=='reorganized':
     task.update(status='completed',resolved=True,resolution=candidate.get('reorganization'));continue
    if candidate.get('status') not in {'pending','approved_staging'}:
     task.update(status='completed',resolved=True,resolution={'outcome':'user_decision','candidate_status':candidate.get('status'),'formal_write_performed':False});continue
    try:
     occupied=any(other['client']!=client and any(t['old_candidate_id']==task['old_candidate_id'] and not t.get('resolved') and t.get('job_id') for t in other['tasks']) for other in data['batches'].values())
     if occupied:raise ValueError('另一已连接 AI 正在更新此条建议，请原 AI 接续；未创建重复任务')
     if task.get('job_id'):
      job=self.organizer.load(task['job_id'],client)
      if job.get('old_candidate_fingerprint')!=sha(json.dumps(candidate,sort_keys=True).encode()):
       if not refresh_versions:raise ValueError('领取后旧建议被编辑，请开始下一次整理核对新版本；未关闭旧项')
       task.pop('job_id',None)
      for row in job['sources']:authorize(row['source_path'])
      if job['captured_authorization_revision']!=revision:task.pop('job_id',None)
      elif any(not (self.context.root/r['source_path']).is_file() or file_version((self.context.root/r['source_path']).stat())!=r['version'] for r in job['sources']):
       if not refresh_versions:raise ValueError('本条来源或目标再变化，需开始下一次整理重新读取；旧项仍待处理')
       task.pop('job_id',None)
     if not task.get('job_id'):
      reject_protected(candidate);p=candidate.get('provenance') or {};target=p.get('target_path')
      if not target:raise ValueError('缺少确切目标，需处理；不能按项目或来源猜测事项')
      destination=Writer(self.context.root,self.context.versions/'formal-memory').target(target)
      authorize(target)
      sources=sorted({e.get('source_path') for e in candidate.get('evidence',[]) if e.get('source_path')})
      if not sources:raise ValueError('缺少确切来源，需处理；没有自动生成结果')
      rows=[]
      for source in sources+([target] if destination.is_file() and target not in sources else []):
       resolved=resolve_document_target(self.context,source);authorize(source);path=self.context.root/resolved.relative_path
       if not path.is_file() or path.suffix.lower() not in {'.md','.txt','.jsonl'}:raise ValueError('来源缺失或不是可读正文，需处理；原建议保留')
       stat=path.stat();rows.append({'source_path':resolved.relative_path,'version':file_version(stat),'total_bytes':stat.st_size,'readable':True,'offset':0,'status':'unprocessed','failure':None,'candidate_ids':[],'read_role':'target' if source==target else 'source'})
      identity=sha(json.dumps([key,task['old_candidate_id'],candidate,target,rows,revision],sort_keys=True).encode());job_id='org_'+identity[:32]
      job={'job_id':job_id,'client':client,'scope':'evidence','folder':'90_原始档案','sources':rows,'snapshot_id':identity,'reorganization_job_schema':1,'wiki_parent_job_id':parent,'reorganization_batch_id':key,'old_candidate_id':task['old_candidate_id'],'old_candidate_fingerprint':sha(json.dumps(candidate,sort_keys=True).encode()),'target_path':target,'expected_target_hash':sha(destination.read_bytes()) if destination.is_file() else 'missing','required_sources':sources,'captured_authorization_revision':revision,'notice':'本任务仅更新此条旧建议，不批准正式写入；不同事项不得因同源或同目标合并。'}
      job['old_candidate']={'candidate_id':candidate['candidate_id'],'title':candidate.get('title'),'content':candidate.get('content'),'reason':p.get('reason'),'target_path':target,'basis':'旧建议待更新内容，不是当前事实或执行授权'}
      with self.organizer.lock():
       path=self.organizer.path(job_id)
       if path.exists():job=self.organizer.load(job_id,client)
       else:save(path,job)
      task.update(job_id=job_id,target_path=target)
     task.update(status='running',failure=None)
    except (ValueError,PermissionError,OSError,HTTPException) as exc:
     task.update(status='needs_attention',failure=str(exc) if isinstance(exc,ValueError) else '来源或目标暂不可读，需处理；当前权限未提升')
   save(self.path,data)
   public=[]
   for task in batch['tasks']:
    entry={k:task.get(k) for k in ('old_candidate_id','status','failure','resolved')}
    if task.get('job_id') and task['status']!='needs_attention':
     job=self.organizer.load(task['job_id'],client);entry.update(job_id=job['job_id'],target_path=job['target_path'],expected_target_hash=job['expected_target_hash'],source_paths=job['required_sources'])
    public.append(entry)
   pending=any(not t.get('resolved') for t in batch['tasks'])
   instructions=('先处理 reorganization.items 中每条独立旧建议：对其 job_id 用 organization_status 取得来源和目标，organization_read 从 offset 连续读完全部正文；缺失目标由服务端确认 missing。结合该 old_candidate_id 的原事项核对，使用 request_knowledge_change 原有参数并传此 job_id、确切 target_path、当前 expected_target_hash 和本任务来源证据。普通更新任务的 evidence 只含 source_path、version、byte_start、byte_end、quote，不要添加 snapshot_id/snapshot_job_id；任务清单的 snapshot_id 不是逐来源历史快照，具体以 organization_read 返回的 evidence_mode 为准。不同事项不得合并。若目标已包含所需结果，提交与当前目标相同的 proposed_content 供服务端核验 unchanged_verified。提交新候选只进待审核，正式写入需用户确认。organization_finish_source 单独调用不代表旧项更新完成。重复领取接续同批；新增建议留下一轮。')
   return result|({'status':'claimed'} if pending and result.get('status') in {'up_to_date','batch_complete'} else {})|{'reorganization':{'batch_id':key,'pending':sum(not t.get('resolved') for t in batch['tasks']),'items':public,'fixed_candidate_boundary':True,'new_items_policy':'next_explicit_batch','instructions':instructions},'instructions':str(result.get('instructions') or '')+'\n'+instructions}
 def validate_submission(self,client,job_id,target_path,evidence,authorization):
  if not job_id:return None
  job=self.organizer.load(job_id,client)
  if not job.get('reorganization_job_schema'):return None
  from .memory_pipeline import candidate_source
  old=candidate_source(job['old_candidate_id'])
  resolved=old.get('status')=='reorganized' and (old.get('reorganization') or {}).get('job_id')==job_id
  if not resolved and (old.get('status') not in {'pending','approved_staging'} or sha(json.dumps(old,sort_keys=True).encode())!=job['old_candidate_fingerprint']):raise ValueError('领取后旧建议已被用户编辑或处理，不能覆盖用户后续决定')
  if target_path!=job['target_path']:raise ValueError('此任务绑定确切旧建议及目标，不能改为另一事项')
  revision=(authorization or {}).get('revision')
  if not revision or revision!=job['captured_authorization_revision']:raise ValueError('当前授权已变化，请重新领取并从头读取；旧项仍待处理')
  for row in job['sources']:
   path=self.context.root/row['source_path']
   if not path.is_file() or file_version(path.stat())!=row['version']:raise ValueError('来源或目标已再变化，未关闭旧建议')
   if row['offset']!=row['total_bytes'] or row.get('delivered_authorization_revision')!=revision:raise ValueError('必须按当前授权读完本条全部来源及现有目标，不能复用旧内容')
  if not set(job['required_sources']).issubset({e['source_path'] for e in evidence}):raise ValueError('结果需包含此旧事项的全部确切来源证据')
  return job
 def complete(self,client,job,candidate_id=None,unchanged=False):
  from .memory_pipeline import candidate_source,mark_candidate_reorganized
  if not unchanged:
   new=candidate_source(candidate_id);p=new.get('provenance') or {}
   if new['status']!='pending' or p.get('job_id')!=job['job_id'] or (p.get('reorganization') or {}).get('old_candidate_id')!=job['old_candidate_id']:raise ValueError('新待审核建议尚未核实绑定此旧事项，旧项保留')
  existing=candidate_source(job['old_candidate_id'])
  proof=(existing.get('reorganization') if existing.get('status')=='reorganized' and (existing.get('reorganization') or {}).get('job_id')==job['job_id'] else None) or {'job_id':job['job_id'],'old_candidate_id':job['old_candidate_id'],'successor_candidate_id':candidate_id,'outcome':'unchanged_verified' if unchanged else 'new_pending','target_path':job['target_path'],'completed_at':now(),'formal_write_performed':False}
  mark_candidate_reorganized(job['old_candidate_id'],proof,job['old_candidate_fingerprint'])
  with self.lock():
   data=self.load();batch=data['batches'][job['reorganization_batch_id']];task=next(t for t in batch['tasks'] if t['old_candidate_id']==job['old_candidate_id']);task.update(status='completed',resolved=True,resolution=proof);save(self.path,data)
  with self.organizer.lock():
   current=self.organizer.load(job['job_id'],client);current['reorganization_result']=proof;save(self.organizer.path(job['job_id']),current)
  return proof
 def pending_for_run(self,client,run_id):
  return [t for b in self.load()['batches'].values() if b['client']==client and (b.get('run_id')==run_id or run_id in b.get('run_ids',[])) for t in b['tasks'] if not t.get('resolved')]
 def refresh_pending(self,client,authorize,readiness,revision):
  """Refresh only the caller's existing unfinished candidate boundary.

  Keep old jobs and completed resolutions. Do not discover new candidates or
  advance the Wiki source batch merely to retry a changed old source.
  """
  from .knowledge_wiki import KnowledgeWiki
  with self.lock():
   data=self.load();batch=data['batches'].get(data['active'].get(client))
   if not batch or not any(not t.get('resolved') for t in batch['tasks']):return None
   if not KnowledgeWiki(self.context).load().get('enabled'):raise ValueError('知识 Wiki 已暂停，请恢复后接续原事项')
   parent=batch.get('parent_job_id')
  return self.attach({'status':'reorganization_refresh','job_id':parent,
    'boundary':'existing_reorganization_only','wiki_batch_unchanged':True,
    'instructions':'仅重领原固定批次中未完成的旧建议；已完成项和旧阅读记录保留，新来源和新建议未加入。来源版本变化的项需按新任务重新读取。继续已有 Wiki 正文任务时用原 job_id；这不是全库新批。'},
    client,authorize,readiness,revision,new_batch=True,existing_batch_id=batch['batch_id'])
 def verify_run_completed(self,client,run_id,authorize,revision):
  for batch in self.load()['batches'].values():
   if batch['client']!=client or not (batch.get('run_id')==run_id or run_id in batch.get('run_ids',[])):continue
   for task in batch['tasks']:
    if not task.get('resolved'):raise ValueError('本轮旧建议尚未产生可核实的新结果，不能登记每日成功')
    if (task.get('resolution') or {}).get('outcome')=='user_decision':continue
    job=self.organizer.load(task['job_id'],client)
    for row in job['sources']:authorize(row['source_path'])
    self.validate_submission(client,job['job_id'],job['target_path'],[{'source_path':p} for p in job['required_sources']],{'revision':revision})
 def public_states(self):
  states={}
  for batch in self.load()['batches'].values():
   for task in batch['tasks']:
    state={k:task.get(k) for k in ('status','failure','resolved')}
    if not task.get('resolved') and task.get('job_id'):
     try:
      job=self.organizer.load(task['job_id'],batch['client'])
      if any(not (self.context.root/r['source_path']).is_file() or file_version((self.context.root/r['source_path']).stat())!=r['version'] for r in job['sources']):
       state.update(status='needs_attention',failure='来源或目标已再变化，需重新整理；旧建议与读取进度保留')
      elif any(r.get('status')=='failed' for r in job['sources']):state.update(status='needs_attention',failure='来源读取失败，需处理；旧建议与读取进度保留')
     except (ValueError,OSError,KeyError):state.update(status='needs_attention',failure='整理任务或来源暂不可核对，需处理；旧建议保留')
    states[task['old_candidate_id']]=state
  return states
