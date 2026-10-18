"""Verified Codex heartbeat -> optional Wiki run -> Organizer job receipts.

Reads Codex history read-only. A completed Codex turn alone never means Wiki success.
Only this server emits Wiki run timestamps and validates completion against its jobs.
"""
import hashlib,json,os,re,sqlite3,tomllib
from datetime import datetime,timedelta,timezone
from pathlib import Path
from filelock import FileLock
from contextlib import closing
from dateutil.tz import gettz
from .conversation_visibility import read_json
from .writer_core import save

UTC=timezone.utc

def timestamp(value):return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(UTC)
def iso(value):return value.astimezone(UTC).isoformat()

def daily_window(now,rule,schedule,config,host_zone=None):
    """Use the scheduler's named zone, or date-aware host local conversion.

    A legacy cached offset is not a timezone: it cannot describe DST or host moves.
    host_zone is an injectable equivalent of the host zone for deterministic tests.
    """
    name=config.get('timezone') or schedule.get('timezone')
    zone=gettz(str(name)) if name else host_zone
    if name and zone is None:raise ValueError('unknown scheduler timezone')
    local=now.astimezone(zone)
    def at(day):
        wall=datetime(day.year,day.month,day.day,int(rule['BYHOUR']),int(rule.get('BYMINUTE',0)),int(rule.get('BYSECOND',0)))
        return wall.replace(tzinfo=zone).astimezone(UTC) if zone else wall.astimezone(UTC)
    day=local.date();due=at(day)
    if due>now:day-=timedelta(days=1);due=at(day)
    return due,at(day+timedelta(days=1)),{'timezone':str(name or local.tzname()),'timezone_source':'scheduler_config' if config.get('timezone') else 'scheduler_binding' if name else 'scheduler_host_local'}

class WikiDailyRuns:
    def __init__(self,context,clock=None):
        self.context=context;self.path=context.state/'wiki-daily-runs.json'
        self.clock=clock or (lambda:datetime.now(UTC))
    def lock(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        return FileLock(str(self.path.with_suffix('.lock')))
    def load(self):
        value=read_json(self.path,{})
        return value if value.get('workspace_id')==self.context.workspace_id else {'schema_version':1,'workspace_id':self.context.workspace_id,'runs':[]}
    def binding(self):
        binding=read_json(self.context.state/'organization-runner.json',{})
        if binding.get('workspace_id')!=self.context.workspace_id:raise ValueError('尚未绑定每日整理任务')
        schedule=binding.get('scheduler') or {};path=Path(schedule.get('config_path',''))
        if not path.is_file() or path.stat().st_size>65536:raise ValueError('调度配置不可读')
        config=tomllib.loads(path.read_text('utf-8'))
        ids={r.get('source_session_id') for r in binding.get('identities',[])}
        if config.get('id')!=schedule.get('id') or config.get('target_thread_id') not in ids:raise ValueError('调度身份不匹配')
        return schedule,config
    def heartbeats(self,schedule,config):
        path=Path(schedule.get('history_db') or (Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'thread_history_1.sqlite'))
        if not path.is_file():raise ValueError('调度运行记录尚不可读')
        # Do not copy or modify Codex databases. Missing/schema-changed data is unknown.
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            rows=db.execute('SELECT turn_id,status,started_at,completed_at,first_user_item_id FROM thread_turns WHERE thread_id=? AND first_user_item_id IS NOT NULL ORDER BY started_at DESC LIMIT 100',(config['target_thread_id'],)).fetchall()
            result=[]
            for row in rows:
                item=db.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?',(config['target_thread_id'],row['turn_id'],row['first_user_item_id'])).fetchone()
                if not item or len(item[0])>262144:continue
                data=json.loads(item[0])
                if data.get('type')!='userMessage':continue
                text='\n'.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text').strip()
                # Only the actual first user item in the bound turn, not arbitrary tool text.
                match=re.match(r'\A<heartbeat>\s*<automation_id>([^<]+)</automation_id>\s*<current_time_iso>([^<]+)</current_time_iso>',text)
                if not match or match[1]!=config['id']:continue
                began=datetime.fromtimestamp(row['started_at'],UTC);declared=timestamp(match[2])
                if abs((began-declared).total_seconds())>300:continue
                result.append({'heartbeat_turn_id':row['turn_id'],'scheduler_id':config['id'],'thread_id':config['target_thread_id'],'provider_status':row['status'],'provider_started_at':iso(began),'provider_completed_at':iso(datetime.fromtimestamp(row['completed_at'],UTC)) if row['completed_at'] else None})
            return result
    def _validate_client(self,schedule,client):
        if not schedule.get('client_id') or client!=schedule['client_id']:raise PermissionError('当前身份不是已绑定的每日整理客户端')
    def _heartbeat(self,schedule,config,turn_id):
        rows=self.heartbeats(schedule,config)
        row=next((r for r in rows if not turn_id or r['heartbeat_turn_id']==turn_id),None)
        if not row:raise ValueError('没有找到匹配的真实心跳记录，不可把手动普通整理冒充每日 Wiki')
        if not 0<=(self.clock()-timestamp(row['provider_started_at'])).total_seconds()<86400:raise ValueError('心跳记录已过期，请等待当前每日任务')
        return row
    def _public(self,run):
        return {k:v for k,v in run.items() if k not in {'client','empty_check','source_fingerprints'}}
    @staticmethod
    def manual_recovery(enabled=True):
        return {'available':bool(enabled),'kind':'manual','counts_as_scheduled_success':False,
                'tool':'knowledge_wiki_start','arguments':{'new_batch':False},
                'notice':'定时回合不能补登记；可由原 AI 手动补跑，旧失败记录和已有进度保留。',
                'instructions':('请在原 Wiki 整理任务中进行一次明确的手动补跑，不登记为定时成功。'
                    '先调用 knowledge_wiki_status，再调用 knowledge_wiki_start(new_batch=false) 接续已有游标。'
                    '若返回 legacy_snapshot_unavailable（仅剩无法恢复的旧版本），或返回 batch_complete 但仍有新增或变化，'
                    '本次手动补跑明确调用 knowledge_wiki_start(new_batch=true)，由服务端保留旧记录、复用已验证结果并冻结剩余资料；不得把旧失效版本标为已处理。'
                    '通过 organization_status/read、request_knowledge_change、organization_finish_source 处理实际资料；'
                    '保留旧失败和旧阅读记录，不调用 knowledge_wiki_daily_finish 登记本次手动结果，不直接批准正式写入。'
                    '若 Wiki 已暂停，先等待用户恢复，不自行启用。')}
    def start(self,client,authorize,heartbeat_turn_id=None):
        from .knowledge_wiki import KnowledgeWiki
        wiki=KnowledgeWiki(self.context);schedule,config=self.binding();self._validate_client(schedule,client)
        if config.get('status')!='ACTIVE':raise ValueError('每日任务当前未安排运行')
        if not wiki.load().get('enabled'):return {'status':'disabled','wiki_started':False,'reason':'知识 Wiki 未启用或已暂停；每日任务不得自动启用'}
        heartbeat=self._heartbeat(schedule,config,heartbeat_turn_id)
        with self.lock():
            data=self.load();run_id='wrun_'+hashlib.sha256((self.context.workspace_id+'\0'+config['id']+'\0'+heartbeat['heartbeat_turn_id']).encode()).hexdigest()[:32]
            run=next((r for r in data['runs'] if r['run_id']==run_id),None)
            if run and run['status'] in {'succeeded','failed','paused'}:return {'status':run['status'],'run':self._public(run),'meaning':'同一调度回合已结束，不重复生成每日成功记录'}
            if heartbeat['provider_status'] not in {'inProgress','in_progress','running'}:
                raise ValueError('该心跳回合已结束；旧普通整理不能补登记为每日 Wiki 启动。'
                                 '后续空输入回合不证明定时续跑；可由原 AI 调用 knowledge_wiki_start(new_batch=false) 手动接续，'
                                 '旧版本不可恢复时明确 new_batch=true 开始手动补跑；不记为定时成功。')
            if not run:
                run={**heartbeat,'run_id':run_id,'purpose':'knowledge_wiki','client':client,'started_at':iso(self.clock()),'status':'running','job_ids':[],'last_activity_at':iso(self.clock()),'completed_at':None,'failure':None}
                data['runs'].append(run);save(self.path,data)
            # Run start is recorded before claim so actual claim failures are visible.
            try:
                job=wiki.claim(client,authorize,require_enabled=True,new_batch=not run['job_ids'])
                if job.get('job_id') and job['job_id'] not in run['job_ids']:run['job_ids'].append(job['job_id'])
                run['empty_check']=job.get('status')=='up_to_date';run['last_activity_at']=iso(self.clock());save(self.path,data)
                return {'status':'running','run':self._public(run),'job':job}
            except Exception as exc:
                run.update(status='failed',completed_at=iso(self.clock()),failure='Wiki 任务领取失败：'+str(exc)[:300]);save(self.path,data);raise
    def finish(self,client,authorize,run_id,outcome,reason=''):
        from .knowledge_wiki import KnowledgeWiki
        if outcome not in {'succeeded','failed'}:raise ValueError('结果必须为 succeeded 或 failed')
        wiki=KnowledgeWiki(self.context);schedule,config=self.binding();self._validate_client(schedule,client)
        with self.lock():
            data=self.load();run=next((r for r in data['runs'] if r.get('run_id')==run_id),None)
            if not run or run['client']!=client or run.get('purpose')!='knowledge_wiki' or run['scheduler_id']!=config['id'] or run['thread_id']!=config['target_thread_id']:raise ValueError('不是当前身份与计划绑定的 Wiki 运行')
            if run['status'] in {'succeeded','failed','paused'}:return {'status':run['status'],'run':self._public(run),'duplicate':True}
            verified=next((r for r in self.heartbeats(schedule,config) if r['heartbeat_turn_id']==run['heartbeat_turn_id']),None)
            if not verified:raise ValueError('调度回合无法核实，不能登记每日完成')
            if outcome=='failed':
                if not reason.strip():raise ValueError('失败需要说明实际原因')
                run.update(status='failed',failure=reason.strip()[:500],failure_origin='authenticated_runner_report',completed_at=iso(self.clock()));save(self.path,data)
                return {'status':'failed','run':self._public(run)}
            if not wiki.load().get('enabled'):
                run.update(status='paused',failure='用户已暂停 Wiki，不能登记成功',completed_at=iso(self.clock()));save(self.path,data)
                return {'status':'paused','run':self._public(run)}
            if verified['provider_status'] in {'failed','interrupted'}:raise ValueError('调度回合失败或中断，不能登记成功')
            if verified['provider_status'] not in {'inProgress','in_progress','running'}:
                raise ValueError('调度回合已结束，不能由后续手动整理补登记为定时成功；请保留原回执并明确手动补跑')
            from .organization_review import _fingerprint
            refs={r['job_id']:r for r in wiki.load().get('jobs',[])}
            fixed_batch=bool(run['job_ids'])
            for job_id in run['job_ids']:
                if not wiki.owns_job(job_id):raise ValueError('运行包含非 Wiki 任务')
                job=wiki.organizer.load(job_id,client)
                fixed_batch=fixed_batch and job.get('snapshot_schema')==1
                for row in job['sources']:
                    authorize(row['source_path'])
                    # A replaced version is not counted complete; the fresh claim below
                    # must link its current replacement before this run may succeed.
                    if row.get('snapshot_id'):
                        from .wiki_snapshots import WikiSnapshots
                        WikiSnapshots(self.context).raw(row)
                    elif _fingerprint(self.context.root/row['source_path'])[1]!=refs[job_id]['source_hashes'][row['source_path']]:continue
                    if row['status'] not in {'processed','skipped'} or row['offset']!=row['total_bytes']:raise ValueError('关联 Wiki 来源尚未读完并处理，不能登记成功')
            # Server performs a fresh incremental check; clients cannot post job IDs/counts/times.
            job={'status':'batch_complete'} if fixed_batch else wiki.claim(client,authorize,require_enabled=True)
            if job.get('job_id'):
                if job['job_id'] not in run['job_ids']:run['job_ids'].append(job['job_id'])
                run.update(last_activity_at=iso(self.clock()),empty_check=False);save(self.path,data)
                return {'status':'more_work','run':self._public(run),'job':job}
            with wiki.lock():
                if not wiki.load().get('enabled'):
                    run.update(status='paused',failure='用户已暂停 Wiki，不能登记成功',completed_at=iso(self.clock()));save(self.path,data)
                    return {'status':'paused','run':self._public(run)}
                run.update(status='succeeded',completed_at=iso(self.clock()),last_activity_at=iso(self.clock()),empty_check=not fixed_batch,completion_basis='server_verified_fixed_snapshot_batch' if fixed_batch else 'server_verified_wiki_jobs_and_fresh_incremental_check',result_kind='sources_processed' if run['job_ids'] else 'no_changes')
                save(self.path,data)
            return {'status':'succeeded','run':self._public(run),'meaning':'本轮固定 Wiki 资料已处理，新增留待下一轮；不代表候选已获批准或写入正式知识' if fixed_batch else '本轮 Wiki 来源已处理/无新增；不代表候选已获批准或写入正式知识'}
    def status(self):
        from .knowledge_wiki import KnowledgeWiki
        result={'arrangement':'not_arranged','label':'未安排','execution_state':'not_run','last_started_at':None,'last_success_at':None,'failure':None}
        try:schedule,config=self.binding()
        except (OSError,ValueError,TypeError,KeyError):
            if read_json(self.context.state/'organization-runner.json',{}).get('scheduler'):
                result.update(arrangement='unavailable',label='调度状态无法核实',execution_state='unverified',failure='调度配置不可读或身份不匹配')
            return result
        active=config.get('status')=='ACTIVE'
        result.update(arrangement='arranged' if active else 'paused',label='已安排' if active else '调度已暂停',scheduler_id=config['id'],rule=config.get('rrule'),name=config.get('name'))
        state=KnowledgeWiki(self.context).load();runs=[r for r in self.load()['runs'] if r.get('purpose')=='knowledge_wiki' and r.get('scheduler_id')==config['id'] and r.get('thread_id')==config['target_thread_id'] and r.get('client')==schedule.get('client_id')]
        try:beats=self.heartbeats(schedule,config);by_id={r['heartbeat_turn_id']:r for r in beats}
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            result.update(execution_state='unverified',execution_notice='调度运行记录暂不可读，不能确认每日 Wiki 成功',
                          recovery=self.manual_recovery(state.get('enabled')));return result
        runs=[r for r in runs if r.get('heartbeat_turn_id') in by_id];latest=runs[-1] if runs else None
        successes=[r for r in runs if r['status']=='succeeded' and ((r.get('completion_basis')=='server_verified_wiki_jobs_and_fresh_incremental_check' and r.get('empty_check')) or (r.get('completion_basis')=='server_verified_fixed_snapshot_batch' and r.get('job_ids')))]
        if successes:result['last_success_at']=successes[-1]['completed_at']
        if latest:result.update(last_started_at=latest['started_at'],run=self._public(latest),execution_state=latest['status'],failure=latest.get('failure'),execution_notice='来自真实心跳与本轮 Wiki 任务关联回执；普通整理完成不计入')
        if beats:result['scheduler_last_trigger_at']=beats[0]['provider_started_at']
        # Empty-input turns have no trusted parent/resume relation in the local
        # projection. Never turn adjacency or a null first user item into proof.
        newest=beats[0] if beats else None
        unlinked_newest=bool(newest and not any(r['heartbeat_turn_id']==newest['heartbeat_turn_id'] for r in runs))
        if not state.get('enabled'):
            result.update(execution_state='disabled',execution_notice='知识 Wiki 未启用或已暂停；现有每日任务只运行其他已授权整理，不自动开启 Wiki');return result
        if not active:return result
        try:
            # Current bound plan is daily; unsupported rules stay unknown rather than guessed.
            rule=dict(x.split('=',1) for x in str(config.get('rrule','')).removeprefix('RRULE:').split(';'))
            if rule.get('FREQ')!='DAILY' or set(rule)-{'FREQ','BYHOUR','BYMINUTE','BYSECOND','INTERVAL'} or int(rule.get('INTERVAL','1'))!=1:raise ValueError('unsupported rule')
            expected,next_expected,zone_info=daily_window(self.clock(),rule,schedule,config)
            result.update(zone_info)
            since=max(timestamp(state.get('enabled_since') or state.get('started_at') or iso(self.clock())),timestamp(schedule.get('integrated_at') or iso(self.clock())))
            result['next_expected_at']=iso(next_expected);result['receipt_grace_seconds']=int(schedule.get('receipt_grace_seconds',1800))
            if expected>=since and self.clock()>expected+timedelta(seconds=result['receipt_grace_seconds']):
                matching=[r for r in runs if expected<=timestamp(r['provider_started_at'])<next_expected]
                if not matching:result.update(execution_state='missing_receipt',failure='已到每日更新时间，但没有关联本轮 Wiki 的运行回执；普通整理记录不算成功',expected_at=iso(expected))
            if latest and latest['status']=='running':
                beat=by_id[latest['heartbeat_turn_id']]
                if beat['provider_status'] in {'failed','interrupted'}:result.update(execution_state='failed',failure='关联调度回合已失败或中断，Wiki 未完成')
                elif beat['provider_status']=='completed':result.update(execution_state='missing_receipt',failure='关联定时回合已结束，但未收到 Wiki 完成回执；后续可手动补跑，不补登记成功')
                else:
                    activity=timestamp(latest.get('last_activity_at') or latest['started_at'])
                    for job_id in latest['job_ids']:
                        try:activity=max(activity,datetime.fromtimestamp(KnowledgeWiki(self.context).organizer.path(job_id).stat().st_mtime,UTC))
                        except OSError:pass
                    if self.clock()>activity+timedelta(seconds=result['receipt_grace_seconds']):result.update(execution_state='missing_receipt',failure='Wiki 已开始，但运行回执已过期；尚未确认完成，成果与游标保留')
            if unlinked_newest and timestamp(newest['provider_started_at'])>=max(expected,since):
                if newest['provider_status'] in {'interrupted','failed','completed'}:
                    result.update(execution_state='interrupted' if newest['provider_status']=='interrupted' else 'missing_receipt',
                                  failure='本次定时心跳已中断，尚未收到 Wiki 开始回执' if newest['provider_status']=='interrupted' else '本次定时心跳已结束，尚未收到 Wiki 开始回执',
                                  current_heartbeat=newest,last_started_at=None,
                                  execution_notice='后续普通或空输入回合不能冒充本次定时续跑；可明确手动补跑')
                    if latest:
                        result['previous_run']=result.pop('run',None)
                        result['last_run_started_at']=latest['started_at']
            if not latest and result['execution_state']=='not_run':result['execution_notice']='已安排，尚未收到每日 Wiki 实际开始回执'
        except (ValueError,TypeError,KeyError):result.update(execution_state='unverified',execution_notice='计划时间无法核实，不能推断每日执行成功')
        if result['execution_state'] in {'interrupted','failed','missing_receipt','unverified'}:
            result['recovery']=self.manual_recovery(state.get('enabled'))
        return result
