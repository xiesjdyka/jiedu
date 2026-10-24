"""Persistent change intake for Organizer. No model, semantic classifier or new writer.

Only explicitly configured source roots are observed. A provider proposes section
changes; policy, current identity, source versions and target baselines gate writes.
"""
import hashlib,json,re,uuid
from datetime import datetime,timezone
from pathlib import Path
from filelock import FileLock
from .conversation_visibility import visibility_operation, visibility_snapshot, source_exclusion_reason, knowledge_message
from .organizer import evidence_checked,reject_protected,submit
from .client_access import resolve_document_target
from .passage_search import file_version
from .writer_core import Writer,save,sha

def now():return datetime.now(timezone.utc).isoformat()

class ContinuousOrganizer:
    def __init__(self,context):
        self.context=context;self.root=context.queues/'external-organization'
        self.path=self.root/'continuous.json';self.policy_path=context.state/'continuous-organization-policy.json'
        self.project_path=self.root/'project-continuity.json'
        self._lock=FileLock(str(self.root/'continuous.lock'))
    def lock(self):self.root.mkdir(parents=True,exist_ok=True);return self._lock
    def policy(self):
        if not self.policy_path.exists():return {'enabled':False,'automatic':False,'source_roots':[],'targets':[],'client_id':None,'revision':'disabled'}
        with self.lock():return json.loads(self.policy_path.read_text('utf-8'))
    def configure(self,value):
        # Caller must enforce Owner authentication. Never callable by a provider.
        if set(value)-{'enabled','automatic','source_roots','targets','client_id'}:raise ValueError('未知整理策略字段')
        if any(type(value.get(k,False)) is not bool for k in ['enabled','automatic']):raise ValueError('启用和自动应用必须明确为布尔值')
        roots=value.get('source_roots',[]);targets=value.get('targets',[])
        if not isinstance(roots,list) or not 1<=len(roots)<=8 or not isinstance(targets,list) or not 1<=len(targets)<=20:raise ValueError('需1–8个来源目录与1–20个目标')
        for root in roots:
            p=Path(root)
            if len(p.parts)<2 or p.parts[0] not in {'90_原始档案','99_收件箱'} or any(x.startswith('.') or ':' in x for x in p.parts):raise ValueError('仅支持明确的公开证据/收件箱子目录')
            if not (self.context.root/p).resolve().is_relative_to(self.context.root.resolve()):raise ValueError('来源越界')
        for target in targets:
            p=Path(target['path']);Writer(self.context.root,self.context.versions/'formal-memory').target(target['path'])
            if not ((len(p.parts)==3 and p.parts[0]=='02_项目' and p.name=='当前状态.md') or (len(p.parts)==2 and p.parts[0]=='01_当前状态')):raise ValueError('自动整理仅支持普通项目当前状态/跨项目摘要，画像与核心规则不在此策略内')
            if not isinstance(target.get('sections'),list) or not target['sections'] or len(target['sections'])>8 or any(not isinstance(s,str) or not s.strip() or '\n' in s or '\r' in s or s in {'人工备注','人工安排','历史','核心规则','长期画像'} for s in target['sections']):raise ValueError('需明确可更新章节，人工/历史/核心内容不可自动覆盖')
        if len({t['path'] for t in targets})!=len(targets):raise ValueError('目标重复')
        if value.get('automatic') and not value.get('client_id'):raise ValueError('自动策略必须绑定既有认证客户端')
        policy={'enabled':bool(value.get('enabled')),'automatic':bool(value.get('automatic')),'source_roots':roots,'targets':targets,'client_id':value.get('client_id'),'revision':uuid.uuid4().hex}
        with self.lock():save(self.policy_path,policy)
        return policy
    def load(self):
        with self.lock():
            if not self.path.exists():return {'tasks':{},'last_checked':None,'intake_failure':None}
            return json.loads(self.path.read_text('utf-8'))
    def load_project(self,project):
        with self.lock():
            state=json.loads(self.project_path.read_text('utf-8')) if self.project_path.exists() else {'version':2,'projects':{}}
            return state.setdefault('projects',{}).setdefault(project,{'messages':{},'issues':{},'source_versions':{},'archive_cursors':{}})
    def _project_state(self):
        return json.loads(self.project_path.read_text('utf-8')) if self.project_path.exists() else {'version':2,'projects':{}}
    def _project_policy(self,project,client):
        policy=self.policy()
        if not policy['enabled']:raise PermissionError('持续整理策略未启用')
        if policy['client_id'] and policy['client_id']!=client:raise PermissionError('策略绑定另一客户端')
        targets=[t for t in policy['targets'] if Path(t['path']).parts==('02_项目',project,'当前状态.md')]
        if len(targets)!=1:raise PermissionError('项目目标不在已授权整理策略中')
        return policy,targets[0]
    @staticmethod
    def message_key(session_id,message_id):return str(session_id)+'\0'+str(message_id)
    @staticmethod
    def _ensure_queue_indexes(data):
        """Assign immutable per-source positions to legacy and new message rows."""
        counters=data.setdefault('source_queue_counters',{})
        groups={}
        for row in data['messages'].values():groups.setdefault(row.get('source_id') or '',[]).append(row)
        for source,rows in groups.items():
            next_index=max(int(counters.get(source,0)),max((int(row['queue_index'])+1 for row in rows
                if type(row.get('queue_index')) is int and row['queue_index']>=0),default=0))
            missing=[row for row in rows if type(row.get('queue_index')) is not int or row['queue_index']<0]
            missing.sort(key=lambda row:(row.get('observed_at') or '',row['evidence'].get('byte_start',0),
                                         row.get('session_id') or '',row.get('message_id') or ''))
            for row in missing:row['queue_index']=next_index;next_index+=1
            counters[source]=next_index
    @visibility_operation
    def consume_project_delta(self,project,source_id,source_version,messages,client):
        """Record an adapter-supplied message delta by stable identity and version.

        The adapter supplies additions or locally detected edits; IDs are never
        treated as sortable cursors. Replaying a delta is idempotent and its work
        is proportional to the supplied rows, not the conversation length.
        """
        policy,_=self._project_policy(project,client)
        if not isinstance(source_id,str) or not source_id or not isinstance(source_version,str) or not source_version:raise ValueError('需要稳定来源与来源版本')
        if not isinstance(messages,list) or len(messages)>500:raise ValueError('单次增量最多500条消息')
        prepared=[];bytes_read=0;verification_bytes=0
        for row in messages:
            if isinstance(row,dict) and not knowledge_message(row):continue
            required={'session_id','message_id','version','content','evidence'}
            if not isinstance(row,dict) or not required<=set(row):raise ValueError('增量消息字段不完整')
            content=str(row['content']);version=sha(content.encode())
            if row['version']!=version:raise ValueError('消息内容与版本不一致')
            checked=evidence_checked(self.context,[row['evidence']])[0]
            verification_bytes+=checked['byte_end']-checked['byte_start']
            if checked.get('archive_line_end'):verification_bytes+=checked['archive_line_end']-checked['byte_start']
            if not self.source_allowed(checked['source_path'],policy):raise PermissionError('消息来源不在策略范围')
            if checked['quote']!=content:
                if checked.get('archive_message_id')!=str(row['message_id']) or checked['version']!=version:raise ValueError('归档行与消息身份或正文不一致')
            prepared.append((row,checked));bytes_read+=len(content.encode())
        with self.lock():
            state=self._project_state();data=state.setdefault('projects',{}).setdefault(project,{'messages':{},'issues':{},'source_versions':{},'archive_cursors':{}});changed=[];unchanged=[];model_bytes=0
            self._ensure_queue_indexes(data)
            for row,checked in prepared:
                key=self.message_key(row['session_id'],row['message_id']);old=data['messages'].get(key)
                item={'session_id':str(row['session_id']),'message_id':str(row['message_id']),'version':row['version'],'role':str(row.get('role') or '').lower(),'provenance':row.get('provenance') or {},'evidence':checked,'source_id':source_id,'source_version':source_version,'client_id':client,'policy_revision':policy['revision'],'observed_at':now()}
                if old:item['queue_index']=old['queue_index']
                else:
                    counter=data['source_queue_counters'];item['queue_index']=counter.get(source_id,0)
                    counter[source_id]=item['queue_index']+1
                if old and old['version']==row['version']:
                    item['processed_issues']=old.get('processed_issues',{})
                    item['provider_processed_version']=old.get('provider_processed_version')
                    item['provider_exposed_version']=old.get('provider_exposed_version')
                    data['messages'][key]=item;unchanged.append(key);continue
                if old:item['previous_version']=old['version']
                data['messages'][key]=item;changed.append({'key':key,'change':'modified' if old else 'added','version':row['version']});model_bytes+=len(str(row['content']).encode())
            data['source_versions'][source_id]=source_version;save(self.project_path,state)
            return {'project':project,'changed':changed,'unchanged':unchanged,'validated_messages':len(messages),'validated_bytes':bytes_read,'verification_bytes_read':verification_bytes,'model_deliverable_bytes':model_bytes,'ordering_assumption':'none'}
    @visibility_operation
    def consume_project_archive(self,project,client,source_path,session_id):
        """Reconcile a canonical messages.jsonl in bounded, restartable pages.

        Canonical projections may be replaced or edited while growing. A changed
        file is therefore rechecked from byte zero; unchanged messages are never
        redelivered to the model. The cursor only resumes a fixed file snapshot.
        """
        policy,_=self._project_policy(project,client)
        if not self.source_allowed(source_path,policy) or not source_path.endswith('/messages.jsonl'):raise PermissionError('来源必须是策略内标准消息归档')
        resolved=resolve_document_target(self.context,source_path);path=self.context.root/resolved.relative_path
        with self.lock():
            state=self._project_state();data=state.setdefault('projects',{}).setdefault(project,{'messages':{},'issues':{},'source_versions':{},'archive_cursors':{}})
            cursor=data.setdefault('archive_cursors',{}).get(source_path,{})
            if cursor and cursor.get('session_id')!=session_id:raise ValueError('同一归档路径不能更换会话身份')
            stat=path.stat();size=stat.st_size;identity=str(stat.st_ino)
            same_snapshot=(cursor.get('file_identity')==identity and cursor.get('mtime_ns')==stat.st_mtime_ns and cursor.get('file_size')==size)
            continuing=bool(cursor.get('scan_in_progress') and same_snapshot)
            if not continuing and cursor and same_snapshot:return {'changed':[],'source_read_bytes':0,'physical_bytes_read':0,'verification_bytes_read':0,'model_deliverable_bytes':0,'scan':'unchanged','next_offset':None}
            start=int(cursor['offset']) if continuing else 0;rows=[];physical=0;end=start;scanned=0
            if not continuing:
                for item in data['messages'].values():
                    if item.get('source_id')==source_path:item['source_recheck_pending']=True
                save(self.project_path,state)
            with path.open('rb') as stream:
                stream.seek(start)
                while scanned<500:
                    line_start=stream.tell();raw=stream.readline(1024*1024+1);physical+=len(raw)
                    if not raw or not raw.endswith(b'\n'):break
                    if len(raw)>1024*1024:raise ValueError('单条归档消息超过1MiB核验预算')
                    row=json.loads(raw);content=str(row.get('original_text') if row.get('original_text') is not None else row.get('content') or '')
                    scanned+=1;end=stream.tell()
                    if not knowledge_message(row):continue
                    declared=str(row.get('content_sha256') or row.get('hash') or '')
                    if declared and declared!=sha(content.encode()):raise ValueError('归档消息内容校验失败')
                    if not row.get('message_id') or not content:raise ValueError('归档消息缺少身份或正文')
                    prefix=raw[:4096]
                    while prefix:
                        try:quote=prefix.decode('utf-8');break
                        except UnicodeDecodeError:prefix=prefix[:-1]
                    if not prefix:raise ValueError('归档消息引用不是UTF-8')
                    version=sha(content.encode())
                    rows.append({'session_id':session_id,'message_id':str(row['message_id']),'version':version,'content':content,'role':row.get('role'),'provenance':row.get('provenance') or {},'evidence':{'source_path':source_path,'version':version,'byte_start':line_start,'byte_end':line_start+len(prefix),'quote':quote,'archive_line_end':line_start+len(raw),'archive_message_id':str(row['message_id']),'archive_session_id':str(row.get('session_key') or session_id)}})
                    end=stream.tell()
            if end==start and size>start:return {'changed':[],'source_read_bytes':physical,'physical_bytes_read':physical,'verification_bytes_read':0,'model_deliverable_bytes':0,'scan':'incomplete_tail','next_offset':start}
            if path.stat().st_size!=size or path.stat().st_mtime_ns!=stat.st_mtime_ns or str(path.stat().st_ino)!=identity:raise ValueError('归档核对期间变化，请重试')
            result=self.consume_project_delta(project,source_path,file_version(stat),rows,client)
            state=self._project_state();data=state['projects'][project]
            more=end<size
            if not more:
                for item in data['messages'].values():
                    if item.get('source_id')==source_path and item.pop('source_recheck_pending',False):item['source_missing']=True
            data.setdefault('archive_cursors',{})[source_path]={'offset':end,'session_id':session_id,'file_identity':identity,'mtime_ns':stat.st_mtime_ns,'file_size':size,'scan_in_progress':more}
            save(self.project_path,state)
            return result|{'source_read_bytes':physical,'physical_bytes_read':physical,'scan':'rescan_page' if continuing else 'rescan','confirmed_offset':end,'next_offset':end if more else None}
    @visibility_operation
    def recheck_project_message(self,project,client,session_id,message_id):
        """Recheck one known archive line when an adapter reports a changed ID.

        An offset shift cannot be guessed: the caller must use the full archive
        reconciliation for that case. This local read does not advance its cursor.
        """
        policy,_=self._project_policy(project,client)
        with self.lock():
            data=self.load_project(project);known=data['messages'].get(self.message_key(session_id,message_id))
            if not known or known.get('client_id')!=client or known.get('policy_revision')!=policy['revision']:raise ValueError('消息未在当前策略下登记')
            old=known['evidence'];source=old['source_path']
            if not self.source_allowed(source,policy) or not old.get('archive_message_id'):raise PermissionError('仅可局部复查已登记的标准归档消息')
            path=self.context.root/source;size=old.get('archive_line_end',old['byte_end'])-old['byte_start']
            if size>1024*1024:raise ValueError('消息超过1MiB局部核验预算')
            with path.open('rb') as stream:stream.seek(old['byte_start']);raw=stream.read(size)
            if len(raw)!=size or not raw.endswith(b'\n'):raise ValueError('消息字节位置已变化，请重查归档')
            try:record=json.loads(raw)
            except ValueError:raise ValueError('消息行已变化，请重查归档') from None
            if str(record.get('message_id'))!=message_id:raise ValueError('消息字节位置已变化，请重查归档')
            if not knowledge_message(record):raise ValueError('此消息不是可提炼的公开聊天正文；原始证据仍保留')
            content=str(record.get('original_text') if record.get('original_text') is not None else record.get('content') or '')
            if len(raw)!=size or not content:raise ValueError('消息行已变化，请重查归档')
            prefix=raw[:old['byte_end']-old['byte_start']]
            row={'session_id':session_id,'message_id':message_id,'version':sha(content.encode()),'content':content,'role':record.get('role'),'provenance':record.get('provenance') or {},'evidence':{'source_path':source,'version':sha(content.encode()),'byte_start':old['byte_start'],'byte_end':old['byte_end'],'quote':prefix.decode('utf-8'),'archive_line_end':old['byte_start']+len(raw),'archive_message_id':message_id,'archive_session_id':old['archive_session_id']}}
            result=self.consume_project_delta(project,source,file_version(path.stat()),[row],client)
            return result|{'source_read_bytes':size,'physical_bytes_read':size,'scan':'local_message_recheck'}
    @visibility_operation
    def project_batch(self,project,client,source_path,session_id,section,limit=4,work_offset=0):
        """Bounded model input from current, versioned messages and target section."""
        policy,target_rule=self._project_policy(project,client)
        if section not in target_rule['sections']:raise PermissionError('目标章节不在策略内')
        scan=self.consume_project_archive(project,client,source_path,session_id)
        if scan.get('next_offset') is not None:
            return {'status':'reconciling','project':project,'source_path':source_path,'next_scan_offset':scan['next_offset'],
                    'source_read_bytes':scan['source_read_bytes'],'verification_bytes_read':scan.get('verification_bytes_read',0),
                    'model_deliverable_bytes':0,'model_actually_consumed':False}
        target=self.context.root/target_rule['path'];raw=target.read_bytes()
        if len(raw)>131072:raise ValueError('目标文件超过128KiB读取预算')
        body=raw.decode('utf-8');pattern=re.compile(r'(?m)^## '+re.escape(section)+r'\r?\n([\s\S]*?)(?=^#{1,2} |\Z)')
        sections=list(pattern.finditer(body))
        if len(sections)!=1:raise ValueError('目标章节不存在或重复')
        current_section=sections[0][1].strip()
        if len(current_section.encode())>32768:raise ValueError('目标章节超过32KiB模型输入预算')
        data=self.load_project(project)
        pending=[row for row in data['messages'].values() if row.get('source_id')==source_path and row.get('session_id')==session_id
                 and knowledge_message(row) and not row.get('source_missing') and not row.get('source_recheck_pending') and row.get('provider_processed_version')!=row['version']]
        pending.sort(key=lambda row:row['evidence']['byte_start'])
        offset=max(0,int(work_offset));bounded=max(1,min(4,int(limit)));selected=pending[offset:offset+bounded]
        messages=[];verify_bytes=0;input_bytes=0
        for row in selected:
            ev=evidence_checked(self.context,[row['evidence']])[0]
            line_end=ev.get('archive_line_end',ev['byte_end']);length=line_end-ev['byte_start']
            verify_bytes+=ev['byte_end']-ev['byte_start']+length
            with (self.context.root/source_path).open('rb') as stream:stream.seek(ev['byte_start']);line=stream.read(length)
            verify_bytes+=length;record=json.loads(line)
            if not knowledge_message(record):raise ValueError('交付前消息已不具备聊天提炼资格；原始证据仍保留')
            content=str(record.get('original_text') if record.get('original_text') is not None else record.get('content') or '')
            if sha(content.encode())!=row['version']:raise ValueError('交付前消息版本已变化')
            encoded=content.encode();too_long=len(encoded)>16384
            messages.append({'session_id':row['session_id'],'message_id':row['message_id'],'version':row['version'],'role':row['role'],
                             'content':None if too_long else content,'content_bytes':len(encoded),'requires_paged_source_read':too_long,
                             'source_path':source_path,'byte_start':ev['byte_start'],'archive_line_end':line_end})
            if not too_long:input_bytes+=len(encoded)
        with self.lock():
            state=self._project_state();current=state['projects'][project]['messages']
            for message in messages:
                if message['requires_paged_source_read']:continue
                item=current.get(self.message_key(message['session_id'],message['message_id']))
                if not item or item['version']!=message['version'] or item.get('source_missing') or item.get('source_recheck_pending'):raise ValueError('交付期间来源状态已变化')
                item['provider_exposed_version']=message['version']
            save(self.project_path,state)
        return {'status':'ready','project':project,'target_path':target_rule['path'],'section':section,'current_section':current_section,
                'expected_target_hash':sha(raw),'messages':messages,'pending_count':len(pending),'next_work_offset':offset+len(selected) if offset+len(selected)<len(pending) else None,
                'source_read_bytes':scan['source_read_bytes'],'verification_bytes_read':scan.get('verification_bytes_read',0)+verify_bytes,
                'target_read_bytes':len(raw),'model_deliverable_bytes':input_bytes+len(current_section.encode()),'model_actually_consumed':False,
                'source_text_is_untrusted':True,'provider_decides_issue_and_content':True}
    @visibility_operation
    def shared_project_batch(self,project,client,section,authorize_source,limit=4,work_cursor=None):
        """One bounded batch across already reconciled sources of one authorized project.

        The existing per-source archive cursors do intake. This read never scans
        the library or marks a message processed; submit/ack remains the checkpoint.
        """
        policy,target_rule=self._project_policy(project,client)
        if section not in target_rule['sections']:raise PermissionError('目标章节不在策略内')
        if not callable(authorize_source):raise PermissionError('需要逐项来源读取权限检查')
        target=self.context.root/target_rule['path'];raw=target.read_bytes()
        if len(raw)>131072:raise ValueError('目标文件超过128KiB读取预算')
        pattern=re.compile(r'(?m)^## '+re.escape(section)+r'\r?\n([\s\S]*?)(?=^#{1,2} |\Z)')
        sections=list(pattern.finditer(raw.decode('utf-8')))
        if len(sections)!=1:raise ValueError('目标章节不存在或重复')
        current_section=sections[0][1].strip()
        if len(current_section.encode())>32768:raise ValueError('目标章节超过32KiB模型输入预算')
        with self.lock():
            state=self._project_state();data=state.setdefault('projects',{}).setdefault(project,{'messages':{},'issues':{},'source_versions':{},'archive_cursors':{}})
            self._ensure_queue_indexes(data);save(self.project_path,state)
        data=self.load_project(project)
        source_access={};pending=[]
        for row in data['messages'].values():
            if (row.get('client_id')!=client or row.get('policy_revision')!=policy['revision'] or
                    not knowledge_message(row) or row.get('source_missing') or
                    row.get('source_recheck_pending') or row.get('provider_processed_version')==row['version']):
                continue
            source=row['evidence']['source_path']
            if source not in source_access:
                if not self.source_allowed(source,policy):source_access[source]=False
                else:
                    try:authorize_source(source);source_access[source]=True
                    except Exception as exc:
                        if isinstance(exc,PermissionError) or getattr(exc,'status_code',None)==403:
                            source_access[source]=False
                        else:raise
            if source_access[source]:pending.append(row)
        def queue_key(row):return (row['queue_index'],row.get('source_id') or '',row['session_id'],row['message_id'])
        if work_cursor is not None:
            if not isinstance(work_cursor,dict) or set(work_cursor)!={'queue_index','source_id','session_id','message_id'} or \
                    type(work_cursor['queue_index']) is not int or work_cursor['queue_index']<0 or \
                    any(not isinstance(work_cursor[key],str) for key in ('source_id','session_id','message_id')):
                raise ValueError('共享批次游标无效')
            after=(work_cursor['queue_index'],work_cursor['source_id'],work_cursor['session_id'],work_cursor['message_id'])
        else:after=None
        ordered=sorted((row for row in pending if after is None or queue_key(row)>after),key=queue_key)
        bounded=max(1,min(4,int(limit)))
        messages=[];seen={};verify_bytes=0;input_bytes=0;invalid=0;cursor=0;examined=0;last_key=None
        while cursor<len(ordered) and len(messages)<bounded and examined<bounded*4:
            row=ordered[cursor];cursor+=1;examined+=1;last_key=queue_key(row)
            source=row['evidence']['source_path']
            try:
                ev=evidence_checked(self.context,[row['evidence']])[0]
                line_end=ev.get('archive_line_end',ev['byte_end']);length=line_end-ev['byte_start']
                with (self.context.root/source).open('rb') as stream:stream.seek(ev['byte_start']);line=stream.read(length)
                record=json.loads(line)
                if not knowledge_message(record):raise ValueError('交付前消息已不具备聊天提炼资格')
                content=str(record.get('original_text') if record.get('original_text') is not None else record.get('content') or '')
                if sha(content.encode())!=row['version']:raise ValueError('交付前消息版本已变化')
            except PermissionError:
                invalid+=1;continue
            except (OSError,ValueError,KeyError,TypeError):
                invalid+=1;continue
            verify_bytes+=ev['byte_end']-ev['byte_start']+length
            encoded=content.encode();too_long=len(encoded)>16384
            duplicate_key=(row['role'],row['version']) if len(encoded)>=80 else None
            duplicate_of=seen.get(duplicate_key) if duplicate_key else None
            identity={'session_id':row['session_id'],'message_id':row['message_id'],'version':row['version']}
            messages.append(identity|{'role':row['role'],'content':None if too_long or duplicate_of else content,
                'content_bytes':len(encoded),'requires_paged_source_read':too_long,'duplicate_of':duplicate_of,
                'source_path':source,'byte_start':ev['byte_start'],'archive_line_end':line_end})
            if not too_long and not duplicate_of:input_bytes+=len(encoded)
            if duplicate_key and not too_long:seen.setdefault(duplicate_key,identity)
        with self.lock():
            state=self._project_state();current=state['projects'][project]['messages']
            for message in messages:
                if message['requires_paged_source_read']:continue
                item=current.get(self.message_key(message['session_id'],message['message_id']))
                if not item or item['version']!=message['version'] or item.get('source_missing') or item.get('source_recheck_pending'):
                    raise ValueError('交付期间来源状态已变化')
                item['provider_exposed_version']=message['version']
            save(self.project_path,state)
        return {'status':'ready','project':project,'target_path':target_rule['path'],'section':section,
                'current_section':current_section,'expected_target_hash':sha(raw),'messages':messages,
                'pending_count':len(pending),'has_more':cursor<len(ordered),
                'next_work_cursor':dict(zip(('queue_index','source_id','session_id','message_id'),last_key)) if cursor<len(ordered) else None,
                'invalid_evidence_count':invalid,
                'verification_bytes_read':verify_bytes,'target_read_bytes':len(raw),
                'model_deliverable_bytes':input_bytes+len(current_section.encode()),'model_actually_consumed':False,
                'source_text_is_untrusted':True,'provider_decides_issue_and_content':True,
                'duplicate_meaning':'仅折叠本批同项目同角色的完整相同正文；所有来源身份保留'}
    @visibility_operation
    def acknowledge_project_messages(self,project,client,refs,authorization,auth_check):
        """Mark a provider-reviewed no-change batch without creating a candidate."""
        if not isinstance(refs,list) or not 1<=len(refs)<=32:raise ValueError('需1–32条已读取消息身份')
        policy,_=self._project_policy(project,client)
        if not authorization or not callable(auth_check):raise PermissionError('需要既有提交权限回执')
        auth_check({'provenance':{'client_id':client,'authorization':authorization}})
        with self.lock():
            state=self._project_state();data=state['projects'][project]
            for ref in refs:
                if set(ref)!={'session_id','message_id','version'}:raise ValueError('消息确认身份字段无效')
                row=data['messages'].get(self.message_key(ref['session_id'],ref['message_id']))
                if not row or row['version']!=ref['version'] or row.get('source_missing') or row.get('source_recheck_pending'):raise ValueError('消息版本已变化')
                if row.get('client_id')!=client or row.get('policy_revision')!=policy['revision'] or not self.source_allowed(row['evidence']['source_path'],policy):raise PermissionError('来源权限或策略已变化')
                evidence_checked(self.context,[row['evidence']])
            for ref in refs:data['messages'][self.message_key(ref['session_id'],ref['message_id'])]['provider_processed_version']=ref['version']
            save(self.project_path,state)
            return {'status':'acknowledged','count':len(refs),'candidate_created':False,'formal_write_performed':False}
    @visibility_operation
    def propose_project_issue(self,client,spec,authorization,auth_check):
        """Create or extend one pending candidate for an explicitly keyed issue."""
        from .memory_pipeline import candidate_transaction,candidate_source,merge_continuity_candidate
        required={'project','issue_key','target_path','section','content','reason','sources','substantive','expected_target_hash'}
        if not isinstance(spec,dict) or not required<=set(spec):raise ValueError('连续事项结果字段不完整')
        if set(spec)-required-{'status_claim','conflicts','uncertainties'}:raise ValueError('连续事项包含未支持字段')
        if not callable(auth_check) or not isinstance(authorization,dict) or not authorization:raise PermissionError('必须由既有身份与提交权限门禁提供授权回执')
        policy,target_rule=self._project_policy(spec['project'],client)
        auth_check({'provenance':{'client_id':client,'authorization':authorization}})
        if spec['target_path']!=target_rule['path']:raise PermissionError('目标不在已授权项目策略中')
        issue=str(spec['issue_key']).strip();section=str(spec['section']).strip()
        if not issue or len(issue)>160 or not section or '\n' in section:raise ValueError('需要模型明确提供事项标识与目标章节')
        if section not in target_rule['sections']:raise PermissionError('目标章节不在策略内')
        if not isinstance(spec['sources'],list) or not spec['sources']:raise ValueError('至少需要一条已登记消息来源')
        kinds={'user_decision','planner_restatement','execution_report','independent_verification'}
        with candidate_transaction(),self.lock():
            state=self._project_state();data=state.setdefault('projects',{}).setdefault(spec['project'],{'messages':{},'issues':{},'source_versions':{},'archive_cursors':{}});selected=[]
            for ref in spec['sources']:
                if set(ref)-{'session_id','message_id','version','kind'}:raise ValueError('来源角色字段不支持')
                if ref.get('kind') not in kinds:raise ValueError('来源角色无效')
                key=self.message_key(ref.get('session_id'),ref.get('message_id'));row=data['messages'].get(key)
                if not row or row['version']!=ref.get('version') or row.get('source_missing') or row.get('source_recheck_pending'):raise ValueError('消息未登记或旧版本已变化')
                if row.get('policy_revision')!=policy['revision'] or row.get('client_id')!=client:raise PermissionError('来源登记属于旧策略或另一客户端，请重新读取')
                if not self.source_allowed(row['evidence']['source_path'],policy):raise PermissionError('来源不在已授权范围')
                selected.append((key,row,dict(ref)))
            if spec.get('status_claim')=='accepted':raise ValueError('来源角色由提供者标注，系统不能据此确认独立验收')
            merge_key=sha((spec['project']+'\0'+spec['target_path']+'\0'+section+'\0'+issue).encode())
            existing=data['issues'].get(merge_key)
            def mark_processed():
                for key,row,_ in selected:
                    data['messages'][key].setdefault('processed_issues',{})[merge_key]={'version':row['version'],'at':now()}
                    data['messages'][key]['provider_processed_version']=row['version']
            if not spec['substantive']:
                mark_processed();save(self.project_path,state)
                return {'status':'no_substantive_change','candidate_created':False,'formal_write_performed':False,'merge_key':merge_key}
            target=self.context.root/spec['target_path'];baseline=sha(target.read_bytes())
            expected=spec['expected_target_hash']
            if baseline!=expected:raise ValueError('目标已变化，旧候选不得续并')
            content=self.replace_sections(target.read_text('utf-8'),{section:str(spec['content'])},[section])
            evidence=[row['evidence'] for _,row,_ in selected];source_roles=[ref for _,_,ref in selected]
            digest=sha(json.dumps({'content':content,'source_roles':source_roles,'evidence':evidence,'conflicts':spec.get('conflicts') or [],'uncertainties':spec.get('uncertainties') or []},sort_keys=True).encode())
            if existing:
                candidate=candidate_source(existing['candidate_id'])
                auth_check(candidate)
                provenance=candidate.get('provenance') or {}
                if provenance.get('client_id')!=client or existing.get('client_id')!=client or existing.get('policy_revision')!=policy['revision']:raise PermissionError('身份或整理策略已变化，请重新授权规划')
                if existing.get('digest')==digest:
                    mark_processed();save(self.project_path,state)
                    return {'status':candidate['status'],'candidate_id':candidate['candidate_id'],'candidate_created':False,'candidate_merged':False,'formal_write_performed':False,'merge_key':merge_key}
                if candidate['status']=='pending':
                    if existing['target_path']!=spec['target_path'] or existing['section']!=section or existing['expected_target_hash']!=baseline:raise ValueError('来源或目标已变化，旧候选不得续并')
                    evidence_checked(self.context,candidate['evidence'])
                    for old in existing['source_roles']:
                        current=data['messages'].get(self.message_key(old['session_id'],old['message_id']))
                        if not current or current['version']!=old['version'] or current.get('source_missing') or current.get('source_recheck_pending'):raise ValueError('旧来源版本已变化，旧候选不得续并')
                oldkeys={(x['session_id'],x['message_id'],x['version'],x['kind']) for x in existing['source_roles']}
                newkeys={(x['session_id'],x['message_id'],x['version'],x['kind']) for x in source_roles}
                if candidate['status']=='pending':
                    if not oldkeys<newkeys:raise ValueError('没有新的实质来源，不生成或改写候选')
                    candidate=merge_continuity_candidate(self.context,candidate['candidate_id'],content,evidence,source_roles,sha(content.strip().encode()),spec.get('conflicts') or [],spec.get('uncertainties') or [])
                    existing.update(source_roles=source_roles,digest=digest,updated_at=now());mark_processed();save(self.project_path,state)
                    return {'status':'awaiting_review','candidate_id':candidate['candidate_id'],'candidate_created':False,'candidate_merged':True,'formal_write_performed':False,'merge_key':merge_key}
                if not newkeys-oldkeys:raise ValueError('审核后没有新来源，不生成下一版本候选')
            generation=int(existing.get('generation',0))+1 if existing else 1
            continuity={'mode':'project_issue','merge_key':merge_key,'generation':generation,'previous_candidate_id':existing['candidate_id'] if existing else None,'project':spec['project'],'issue_key':issue,'section':section,'policy_revision':policy['revision'],'source_roles':source_roles,'content_hash':sha(content.strip().encode()),'source_roles_meaning':'provider_claim_not_independent_verification'}
            request_id='project_issue_'+sha((merge_key+'\0'+str(generation)).encode())[:48]
            result=submit(self.context,client,spec['target_path'],content,spec['reason'],evidence,action='update',expected_target_hash=baseline,request_id=request_id,conflict_status='possible' if spec.get('conflicts') else 'none',uncertainties=spec.get('uncertainties') or [],authorization=authorization,continuity=continuity)
            data['issues'][merge_key]={'candidate_id':result['candidate_id'],'client_id':client,'policy_revision':policy['revision'],'generation':generation,'previous_candidate_id':existing['candidate_id'] if existing else None,'target_path':spec['target_path'],'section':section,'expected_target_hash':baseline,'source_roles':source_roles,'digest':digest,'created_at':now()};mark_processed();save(self.project_path,state)
            return {'status':'awaiting_review','candidate_id':result['candidate_id'],'candidate_created':result['candidate_created'],'candidate_merged':False,'formal_write_performed':False,'merge_key':merge_key}
    def source_allowed(self,path,policy):
        from .conversation_visibility import source_exclusion_reason
        p=Path(path)
        return any(p==Path(root) or Path(root) in p.parents for root in policy['source_roots']) and not any(x.startswith('.') for x in p.parts) and not source_exclusion_reason(self.context,path)
    @visibility_operation
    def observe(self,path):
        policy=self.policy()
        if not policy['enabled'] or not self.source_allowed(path,policy):return None
        with self.lock():
            data=self.load();file=self.context.root/path
            if not file.is_file():
                for task in data['tasks'].values():
                    if task['source_path']==path and task['status']!='written':task.update(status='stale',failure='来源已删除；不得沿用旧结果')
                save(self.path,data);return None
            resolve_document_target(self.context,path)
            if file.stat().st_size>4*1024*1024 or file.suffix.lower() not in {'.md','.txt'}:
                data['intake_failure']='来源超出4MiB文本预算或需先转换；未将其当作已处理';save(self.path,data);return None
            raw=file.read_bytes();fingerprint=file_version(file.stat());key='change_'+sha((path+'\0'+sha(raw)+'\0'+fingerprint).encode())[:32]
            existing=data['tasks'].get(key)
            if existing and existing['source_version']==fingerprint:return key
            for task in data['tasks'].values():
                if task['source_path']==path and task['status']!='written':task.update(status='stale',failure='出现新来源版本，请使用新任务或重新规划')
            generation=(existing or {}).get('generation',0)+1
            data['tasks'][key]={'task_id':key,'source_path':path,'source_hash':sha(raw),'source_version':fingerprint,'total_bytes':len(raw),'generation':generation,'policy_revision':policy['revision'],'status':'pending','failure':None,'created_at':now(),'deliveries':{},'targets':{},'last_verified':None,'provider_attached':False}
            save(self.path,data);return key
    @visibility_operation
    def reconcile(self):
        policy=self.policy()
        if not policy['enabled']:return
        try:
            with self.lock():
                data=self.load();data['intake_failure']=None;save(self.path,data)
            visibility=visibility_snapshot(self.context);files=[]
            for root in policy['source_roots']:
                for file in (self.context.root/root).rglob('*'):
                    if file.is_file() and not visibility.reason(file.relative_to(self.context.root)):files.append(file)
                    if len(files)>500:raise ValueError('限定来源超过500项，请缩小范围；覆盖未完成')
            for file in files:self.observe(file.relative_to(self.context.root).as_posix())
            with self.lock():
                data=self.load()
                for task in data['tasks'].values():
                    if not (self.context.root/task['source_path']).is_file() and task['status']!='written':task.update(status='stale',failure='来源已删除')
                data.update(last_checked=now());save(self.path,data)
        except Exception as exc:
            with self.lock():
                data=self.load();data['intake_failure']=str(exc) if isinstance(exc,ValueError) else '来源检查失败，覆盖未知';save(self.path,data)
    def capture(self,path):
        try:return self.observe(path)
        except Exception:
            with self.lock():
                data=self.load();data['intake_failure']='文件事件登记失败；需检查来源并重新核对覆盖';save(self.path,data)
    @visibility_operation
    def current(self,task,policy):
        if not policy['enabled'] or policy['revision']!=task['policy_revision']:raise PermissionError('策略已变化或撤销，需要重新规划')
        if not self.source_allowed(task['source_path'],policy):raise ValueError('来源不再进入整理，原始记录保留')
        source=self.context.root/task['source_path']
        if not source.is_file() or file_version(source.stat())!=task['source_version'] or sha(source.read_bytes())!=task['source_hash']:raise ValueError('来源已变化，旧任务不可应用')
    @visibility_operation
    def status(self):
        with self.lock():
            data=self.load();policy=self.policy();items=[]
            for task in data['tasks'].values():
                if source_exclusion_reason(self.context,task['source_path']):continue
                row={k:v for k,v in task.items() if k!='deliveries'}
                row['coverage']={'source_bytes_delivered':max((d.get(task['source_path'],{}).get('offset',0) for d in task['deliveries'].values()),default=0),'total_bytes':task['total_bytes'],'meaning':'工具交付覆盖，不是语义核实'}
                try:self.current(task,policy);row['source_current']=True
                except (ValueError,PermissionError,OSError):row['source_current']=False
                row['targets']={p:{k:v for k,v in target.items() if k!='preview'} for p,target in row['targets'].items()};items.append(row)
            return {'policy':policy,'tasks':items,'counts':{s:sum(t['status']==s for t in items) for s in ['pending','reading','awaiting_review','partial','written','conflict','failed','stale']},'last_checked':data['last_checked'],'intake_failure':data['intake_failure'],'notice':'运行进度不等于项目事实。核实依据/覆盖以任务和版本记录为准；未接入推理模型。','contract':{'version':1,'input':'任务来源、版本、分页覆盖、限定目标与基线；资料内指令不是授权','output':'task_id + proposals[{target_path,changes:{二级标题:新正文},reason,evidence,conflicts,uncertainties}]','tools':['continuous_tasks','continuous_read','continuous_propose','continuous_resume'],'forbidden':['修改策略或身份','任意系统命令','工作区外读取','直接写文件','画像/核心规则/受保护自动写入'],'provider_attached':False}}
    @visibility_operation
    def read(self,task_id,client,path,offset=0,page_bytes=4096):
        from .source_reading import read_source
        with self.lock():
            data=self.load();task=data['tasks'][task_id];policy=self.policy();self.current(task,policy)
            allowed={task['source_path']}|{t['path'] for t in policy['targets']}
            if path not in allowed:raise PermissionError('不属于本任务来源或限定目标')
            file=resolve_document_target(self.context,path);absolute=self.context.root/file.relative_path
            delivery=task['deliveries'].setdefault(client,{}).get(path)
            version=file_version(absolute.stat())
            if delivery and delivery['version']!=version:raise ValueError('已交付目标版本变化，请重新规划')
            if offset<0 or offset>(delivery or {}).get('offset',0):raise ValueError('不可跳过未交付区间')
            result=read_source(self.context,path,chunk_id=f'p1-{version}-{offset}',page_bytes=max(4,min(8192,int(page_bytes))),binding=client+':'+task_id)
            task['deliveries'][client][path]={'version':version,'offset':offset+result['returned_bytes'],'complete':result['complete'],'hash':sha(absolute.read_bytes())}
            task['status']='reading' if not task['targets'] else task['status'];save(self.path,data)
            suggestions=[{'target_path':t['path'],'sections':t['sections'],'basis':'显式授权目标目录；是否受影响须由推理提供者判断'} for t in policy['targets']]
            return result|{'target_suggestions':suggestions,'complete_is_not_verified':True}
    @staticmethod
    def replace_sections(old,changes,sections):
        if not changes or set(changes)-set(sections):raise PermissionError('结果超出允许更新的章节')
        for heading,body in changes.items():
            if not isinstance(body,str) or re.search(r'(?m)^#{1,2}\s',body):raise ValueError('章节正文不能注入一级或二级标题')
            pattern=re.compile(r'(?m)(^## '+re.escape(heading)+r'\r?\n)([\s\S]*?)(?=^#{1,2} |\Z)')
            matches=list(pattern.finditer(old))
            if len(matches)!=1:raise ValueError('允许章节不存在或重复，需人工核对')
            m=matches[0];newline='\r\n' if '\r\n' in old else '\n'
            old=old[:m.start()]+m[1]+body.strip().replace('\r\n','\n').replace('\n',newline)+newline+newline+old[m.end():]
        return old
    def validate_project_review_content(self,candidate,content):
        """A review edit may change only the already authorized target section."""
        p=candidate.get('provenance') or {};receipt=p.get('continuity') or {}
        if receipt.get('mode')!='project_issue':raise ValueError('不是连续事项候选')
        if not content.strip() or len(content.encode('utf-8'))>131072:raise ValueError('审核改稿为空或超过128KiB')
        target=self.context.root/p['target_path'];raw=target.read_bytes()
        if sha(raw)!=p.get('expected_target_hash'):raise ValueError('项目目标基线已变化')
        section=receipt['section'];pattern=re.compile(r'(?m)^## '+re.escape(section)+r'\r?\n([\s\S]*?)(?=^#{1,2} |\Z)')
        matches=list(pattern.finditer(content))
        if len(matches)!=1 or self.replace_sections(target.read_text('utf-8'),{section:matches[0][1]},[section]).rstrip()!=content.rstrip():
            raise ValueError('审核改稿只能修改已授权目标章节')
    @visibility_operation
    def validate_candidate(self,candidate):
        p=candidate.get('provenance') or {};receipt=p.get('continuity')
        if not receipt:return
        with self.lock():
            if receipt.get('mode')=='project_issue':
                policy,target=self._project_policy(receipt['project'],p.get('client_id'))
                if receipt['policy_revision']!=policy['revision'] or p.get('target_path')!=target['path'] or receipt['section'] not in target['sections']:raise PermissionError('项目整理策略或目标已变化')
                current_hash=sha(candidate['content'].strip().encode())
                if current_hash!=receipt['content_hash']:
                    review=receipt.get('review_edit') or {}
                    if (review.get('content_hash')!=current_hash or
                        review.get('original_model_content_hash')!=receipt['content_hash']):
                        raise ValueError('连续事项候选正文已变化')
                if sha((self.context.root/target['path']).read_bytes())!=p.get('expected_target_hash'):raise ValueError('项目目标基线已变化')
                self.validate_project_review_content(candidate,candidate['content'])
                data=self.load_project(receipt['project']);issue=data['issues'].get(receipt['merge_key'])
                if not issue or issue['candidate_id']!=candidate['candidate_id'] or issue['generation']!=receipt['generation']:raise ValueError('事项候选已被后续版本取代')
                for ref in receipt['source_roles']:
                    row=data['messages'].get(self.message_key(ref['session_id'],ref['message_id']))
                    if not row or row['version']!=ref['version'] or row.get('source_missing') or row.get('source_recheck_pending'):raise ValueError('事项来源已变化')
                evidence_checked(self.context,candidate['evidence'])
                return
            task=self.load()['tasks'][receipt['task_id']];policy=self.policy();self.current(task,policy)
            if receipt['generation']!=task['generation'] or receipt['policy_revision']!=policy['revision']:raise PermissionError('任务已重新规划，旧候选不可应用')
            if sha(candidate['content'].encode())!=receipt['content_hash']:raise ValueError('持续整理候选正文已编辑，请重新提交')
    @visibility_operation
    def propose(self,task_id,client,proposals,authorization,auth_check):
        from .memory_pipeline import candidate_transaction
        if not isinstance(proposals,list) or not 1<=len(proposals)<=20:raise ValueError('需1–20项结果')
        with candidate_transaction(),self.lock():
            data=self.load();task=data['tasks'][task_id];policy=self.policy();self.current(task,policy)
            if policy['client_id'] and policy['client_id']!=client:raise PermissionError('任务绑定了另一客户端')
            digest=sha(json.dumps(proposals,sort_keys=True).encode())
            if len({s['target_path'] for s in proposals})!=len(proposals):raise ValueError('结果目标重复')
            if task.get('plan_digest') and task['plan_digest']!=digest:raise ValueError('已有不同结果，请重新规划')
            if task.get('plan_digest')==digest and all(s['target_path'] in task['targets'] for s in proposals):return self.resume(task_id,client,auth_check)
            delivered=task['deliveries'].get(client,{})
            if not delivered.get(task['source_path'],{}).get('complete'):raise ValueError('尚未完整交付来源')
            prepared=[]
            for spec in proposals:
                if task['targets'].get(spec['target_path'],{}).get('status')=='written':raise ValueError('已写入目标仅回查；重新规划只提交未完成目标')
                if set(spec)-{'target_path','changes','reason','evidence','conflicts','uncertainties'}:raise ValueError('输出包含未支持字段；置信度不产生授权')
                target=next((t for t in policy['targets'] if t['path']==spec['target_path']),None)
                if not target or not delivered.get(spec['target_path'],{}).get('complete'):raise ValueError('目标不在策略内或尚未完整读取')
                if any(e['source_path']!=task['source_path'] for e in spec['evidence']):raise ValueError('引用必须来自本任务已交付版本')
                evidence_checked(self.context,spec['evidence']);reject_protected(spec)
                path=self.context.root/spec['target_path'];baseline=delivered[spec['target_path']]['hash']
                if sha(path.read_bytes())!=baseline:raise ValueError('目标已被外部修改，请重新读取并规划')
                content=self.replace_sections(path.read_bytes().decode('utf-8'),spec['changes'],target['sections'])
                prepared.append((spec,baseline,content))
            # Validate the entire plan before creating any candidate.
            task['plan_digest']=digest;save(self.path,data)
            for spec,baseline,content in prepared:
                if spec['target_path'] in task['targets']:continue
                continuity={'task_id':task_id,'generation':task['generation'],'policy_revision':policy['revision'],'content_hash':sha(content.strip().encode())}
                result=submit(self.context,client,spec['target_path'],content,spec['reason'],spec['evidence'],action='update',expected_target_hash=baseline,request_id=sha((task_id+str(task['generation'])+spec['target_path']).encode()),conflict_status='possible' if spec.get('conflicts') else 'none',uncertainties=spec.get('uncertainties') or [],authorization=authorization,continuity=continuity)
                task['targets'][spec['target_path']]={'candidate_id':result['candidate_id'],'status':'awaiting_review','failure':None,'version':None}
                save(self.path,data)
            task['status']='awaiting_review';save(self.path,data)
        return self.resume(task_id,client,auth_check)
    @visibility_operation
    def resume(self,task_id,client,auth_check):
        from .memory_pipeline import candidate_transaction,candidate_source,review_candidate,mark_candidate_written
        with candidate_transaction(),self.lock():
            data=self.load();task=data['tasks'][task_id];policy=self.policy();self.current(task,policy)
            if policy['client_id'] and policy['client_id']!=client:raise PermissionError('任务绑定了另一客户端')
            for target,row in task['targets'].items():
                try:
                    candidate=candidate_source(row['candidate_id'])
                    if candidate['status']=='written':
                        manifest=self.context.versions/'formal-memory'/candidate['writer_version']/'manifest.json';meta=json.loads(manifest.read_text('utf-8'))
                        if sha((self.context.root/target).read_bytes())!=meta['after_hash']:raise ValueError('写入后目标又被修改，当前摘要需要复核')
                        row.update(status='written',version=candidate['writer_version'],failure=None);continue
                    auth_check(candidate);self.validate_candidate(candidate);evidence_checked(self.context,candidate['evidence'])
                    if candidate.get('conflicts') or candidate['provenance'].get('uncertainties'):
                        row.update(status='conflict',failure='冲突或证据不足，需Owner核对');continue
                    if not policy['automatic']:continue
                    if candidate['provenance']['client_id']!=policy['client_id']:raise PermissionError('自动策略未授予当前身份')
                    if candidate['status']=='pending':review_candidate(candidate['candidate_id'],'accept');candidate=candidate_source(candidate['candidate_id'])
                    if candidate['status']!='approved_staging':raise ValueError('候选状态不可应用')
                    writer=Writer(self.context.root,self.context.versions/'formal-memory')
                    if not row.get('preview'):
                        row['preview']=writer.preview(target,candidate['title'],candidate['content'],candidate['evidence'],action='update',expected_hash=candidate['provenance']['expected_target_hash'],context={'candidate_id':candidate['candidate_id'],'authorization_kind':'explicit_continuous_policy','policy_revision':policy['revision']});save(self.path,data)
                    def validate(_):
                        self.validate_candidate(candidate);auth_check(candidate);evidence_checked(self.context,candidate['evidence'])
                    meta=writer.apply(row['preview'],owner_confirmed=True,validate=validate)
                    mark_candidate_written(candidate['candidate_id'],meta['version']);row.update(status='written',version=meta['version'],failure=None,notification='已按已启用策略更新；可查看差异与版本')
                except (ValueError,PermissionError,OSError) as exc:row.update(status='failed',failure=str(exc))
                save(self.path,data)
            states=[r['status'] for r in task['targets'].values()]
            task['status']='written' if states and all(s=='written' for s in states) else 'partial' if 'written' in states else 'conflict' if 'conflict' in states else 'failed' if 'failed' in states else 'awaiting_review'
            if task['status']=='written':task['last_verified']={'at':now(),'source_version':task['source_version'],'source_hash':task['source_hash'],'coverage':'本任务完整来源；不代表全部项目资料'}
            save(self.path,data);return {k:v for k,v in task.items() if k!='deliveries'}
    @visibility_operation
    def replan(self,task_id):
        from .memory_pipeline import candidate_transaction,candidate_source
        with candidate_transaction(),self.lock():
            data=self.load();task=data['tasks'][task_id]
            retained={}
            for path,row in task['targets'].items():
                candidate=candidate_source(row['candidate_id'])
                if candidate['status']=='written':
                    meta=json.loads((self.context.versions/'formal-memory'/candidate['writer_version']/'manifest.json').read_text('utf-8'))
                    if sha((self.context.root/path).read_bytes())!=meta['after_hash']:raise ValueError('已写入目标又被修改，请先人工回查该版本')
                    retained[path]=row|{'status':'written','version':candidate['writer_version'],'failure':None}
            policy=self.policy();task.update(generation=task['generation']+1,policy_revision=policy['revision'],deliveries={},targets=retained,status='partial' if retained else 'pending',failure=None,plan_digest=None)
            source=self.context.root/task['source_path']
            if not source.is_file() or sha(source.read_bytes())!=task['source_hash']:raise ValueError('来源内容已变，请使用新来源任务')
            task['source_version']=file_version(source.stat());save(self.path,data);return task_id
