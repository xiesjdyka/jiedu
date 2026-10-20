"""Read-only ingestion of Hermes Desktop's stable SQLite session store."""
from __future__ import annotations
import hashlib,json,logging,mimetypes,os,re,shutil,sqlite3,tempfile,threading,time
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from .core import ROOT
from .workspace import PROGRAM_ROOT, active_workspace
from .session_manager import session_start,session_append,session_status,list_sessions,update_session_metadata
from .conversation_visibility import hermes_exclusions, message_structure_reason
from .session_manager import ARCHIVE_ROOT,_archive_path
STATE=active_workspace().sync/"hermes-adapter.json"
def db_candidates():
    local=Path(os.getenv('LOCALAPPDATA') or Path.home()/'AppData'/'Local')
    roaming=Path(os.getenv('APPDATA') or Path.home()/'AppData'/'Roaming')
    values=[os.getenv('KNOWLEDGE_BRIDGE_HERMES_DB'), local/'hermes'/'state.db',
            roaming/'hermes'/'state.db', Path.home()/'.hermes'/'state.db']
    result=[];seen=set()
    for value in values:
        if not value:continue
        path=Path(value).expanduser()
        token=os.path.normcase(str(path.resolve()))
        if token not in seen:seen.add(token);result.append(path)
    return result


def selected_db_path():
    for path in db_candidates():
        if not path.is_file():continue
        try:
            with closing(sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro',uri=True,timeout=1)) as db:
                tables={row[0] for row in db.execute("select name from sqlite_master where type='table'")}
                if {'sessions','messages'} <= tables:return path
        except (OSError,sqlite3.Error):continue
    return db_candidates()[0]


DEFAULT_DB=selected_db_path()
_lock=threading.RLock()
_worker_thread=None
_worker_error=None
_logger=logging.getLogger(__name__)
def _now():return datetime.now(timezone.utc).isoformat()
def _iso(value):return datetime.fromtimestamp(float(value),timezone.utc).isoformat()
def _load():
    try:return json.loads(STATE.read_text("utf-8"))
    except FileNotFoundError:return {"cursor":None,"status":"not_started","last_error":None,"last_sync":None,"synced_messages":0}
def _save(data):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    # Windows readers can briefly deny replace. Preserve the durable checkpoint.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=STATE.parent,
                                     prefix=STATE.name+'.',suffix='.tmp',delete=False) as handle:
        temporary=Path(handle.name)
        try:
            handle.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            handle.flush();os.fsync(handle.fileno())
        except Exception:
            handle.close();temporary.unlink(missing_ok=True);raise
    try:
        for attempt in range(6):
            try:os.replace(temporary,STATE);return
            except PermissionError:
                if attempt==5:raise
                time.sleep(.02*(2**attempt))
    finally:
        temporary.unlink(missing_ok=True)
def _connect(path):return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro",uri=True,timeout=2)
IMAGE_REF=re.compile(r"@image:([^\r\n]+?\.(?:png|jpe?g|gif|webp|bmp|tiff?))(?=\s|$)",re.I)
def _inline_attachments(content):return [x.strip().strip('"') for x in IMAGE_REF.findall(content or "")]
def _attachments(session_id,message_id,message_order,items,*,return_changed=False):
    from .attachment_cache import cache_transaction,latest_rows,append_rows,resolve,identity
    # A patched ROOT is retained only as an isolated legacy-test seam.
    root=(ROOT/"data"/"sessions"/"attachments" if ROOT!=PROGRAM_ROOT else active_workspace().state/"attachments")
    out=[]
    with cache_transaction(root):
        existing=latest_rows(root/'manifest.jsonl')
        for item in items or []:
            ref=item if isinstance(item,str) else item.get('path') or item.get('url') or item.get('name')
            row={'session_id':session_id,'message_id':message_id,'message_order':message_order,'reference':ref,'source_path':ref,
                'original_name':Path(ref).name if isinstance(ref,str) else None,'status':'missing','sha256':None,'stored_path':None,'archived_path':None,
                'extension':Path(ref).suffix.lower() if isinstance(ref,str) else None,'mime':mimetypes.guess_type(ref)[0] if isinstance(ref,str) else None}
            path=Path(ref) if isinstance(ref,str) else None;previous=existing.get(identity(row))
            if path and path.is_file():
                from .attachment_integrity import store_verified
                target,digest,_,_=store_verified(root,lambda sha:f'{sha}{path.suffix.lower()}',source=path)
                row.update(status='mapped',sha256=digest,stored_path=str(target),archived_path=str(target),extension=path.suffix.lower())
                if previous and previous.get('sha256')==digest and previous.get('cache_resolution'):row['cache_resolution']=previous['cache_resolution']
            elif previous and previous.get('sha256'):
                resolved=resolve(root,previous)
                if resolved.get('status')=='mapped' and Path(resolved.get('stored_path') or '').is_file():row=resolved
            out.append(row)
        changed=append_rows(root,out)
    return (out,changed) if return_changed else out

def initialize(db_path=None):
    db_path=Path(db_path) if db_path is not None else selected_db_path()
    with _lock:
        state=_load()
        previous=state.get('db_path')
        if previous and Path(previous).resolve()!=db_path.resolve():
            state.setdefault('previous_sources',[]).append({
                'db_path':previous,'cursor':state.get('cursor'),
                'history':state.get('history'),'closed_at':_now(),
                'reason':'selected_source_changed'})
            prior_history=state.get('history')
            state['cursor']=None
            if prior_history and prior_history.get('status')!='paused':
                state['history']=None
                state['auto_history_pending']=True
            # An Owner pause belongs to the old source and remains intact.
            # Resuming a different database still requires explicit review.
        if state.get("cursor") is None:
            with closing(_connect(db_path)) as db:state["cursor"]=db.execute("select coalesce(max(id),0) from messages").fetchone()[0]
        state.update({"status":"watching","db_path":str(db_path),"last_error":None});_save(state);return state

def enable_history_for_first_pairing(db_path=None):
    """Queue bounded historical backfill after a new Hermes source is selected."""
    from .memory_control import interface_state
    db_path=Path(db_path) if db_path is not None else selected_db_path()
    with _lock:
        state=_load()
        if (state.get('history') is not None or state.get('status')=='paused' or
                not interface_state()['interfaces']['save_raw_conversation']['enabled']):
            return 'existing_settings_preserved'
        state['auto_history_pending']=True
        if Path(db_path).is_file():
            _start_pending_history(state,db_path)
        _save(state)
        return 'enabled' if state.get('history') else 'waiting_for_source'

def _start_pending_history(state,db_path):
    if not state.get('auto_history_pending') or state.get('history') is not None:
        return
    with closing(_connect(db_path)) as db:
        upper,count=db.execute('select coalesce(max(id),0),count(*) from messages').fetchone()
    state['history']={'status':'running','cursor':0,'upper_id':upper,
                      'source_rows_at_start':count,'processed_rows':0,'accepted_rows':0,
                      'excluded_rows':0,'ignored_rows':0,'orphan_rows':0,'missing_rows':0,
                      'started_at':_now(),'db_path':str(Path(db_path).resolve()),'last_error':None}
    state['auto_history_pending']=False

def _refs(row):
    refs=[]
    if row['display_metadata']:
        try:
            meta=json.loads(row['display_metadata'])
            if isinstance(meta,dict) and isinstance(meta.get('attachments'),list):refs=meta['attachments']
        except ValueError:pass
    return list(refs)+[x for x in _inline_attachments(row['content']) if x not in refs]

NOTICE_KINDS={'model_switch','async_delegation_complete','process_complete','auto_continue','personality_switch','failed_turn'}
def _display_semantics(row):
    kind=row['display_kind'] if 'display_kind' in row.keys() else None
    try:meta=json.loads(row['display_metadata'] or '{}')
    except (ValueError,TypeError):meta={}
    model_only=isinstance(meta,dict) and bool(meta.get('model_only'))
    if model_only:return None,'model_only',kind
    if kind=='hidden':return None,'hidden',kind
    # Only optional, explicit source columns establish an internal route. Do not
    # interpret body text, unknown display kinds, or assistant tool_calls as one.
    channel=row['source_channel'] if 'source_channel' in row.keys() else row['channel'] if 'channel' in row.keys() else None
    recipient=row['source_recipient'] if 'source_recipient' in row.keys() else row['recipient'] if 'recipient' in row.keys() else None
    reason=message_structure_reason(channel,recipient)
    if reason in {'internal_message_channel','internal_message_recipient'}:return None,reason,kind
    if row['role']=='user' and re.match(r'^\[Background process \S+ heartbeat #\d+ /',(row['content'] or '').strip()):return None,'legacy_heartbeat',kind
    if kind in NOTICE_KINDS:return 'system','system_notice',kind
    return row['role'],'authored',kind

def _fingerprint(role,content,refs,semantics=None):
    values=[role,content or '',refs]
    if semantics is not None:values.append(semantics)
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()

def _seed_observations(state, db_path):
    """Only recheck source rows already accepted by this adapter, never unknown history."""
    sessions=sorted(list_sessions('hermes'),key=lambda x:x['session_id'])
    if not sessions:return
    scan=state.setdefault('observation_seed',{'next':0,'offsets':{}})
    sid=sessions[scan['next']%len(sessions)]['session_id'];path=_archive_path(sid,ARCHIVE_ROOT)
    offset=scan['offsets'].get(sid,0)
    if path.is_file():
        with path.open('rb') as handle:
            handle.seek(offset)
            for _ in range(50):
                line=handle.readline()
                if not line or not line.endswith(b'\n'):break
                row=json.loads(line);provenance=row.get('provenance') or {}
                if provenance.get('adapter')=='hermes_sqlite_readonly' and provenance.get('source_db')==str(db_path) and provenance.get('hermes_row_id') is not None:
                    key=str(provenance['hermes_row_id'])
                    state.setdefault('observations',{}).setdefault(key,{
                        'message_id':row['message_id'],'base_id':provenance.get('base_message_id') or row['message_id'],
                        'fingerprint':_fingerprint(row['role'],row.get('original_text',row.get('content')),row.get('attachment_refs') or [],{'source_role':provenance.get('source_role'),'display_kind':provenance.get('source_display_kind'),'decision':provenance.get('display_semantics')} if provenance.get('source_display_kind') is not None or provenance.get('display_semantics','authored')!='authored' else None),
                        'session_id':sid,'active':True})
                offset=handle.tell()
    scan['offsets'][sid]=offset;scan['next']=(scan['next']+1)%len(sessions)

def _queue_projection(state,sid,source_id=None,message_id=None):
    pending=state.setdefault('pending_materializations',{}).setdefault(sid,{})
    if source_id is not None:pending[str(source_id)]={'archive_message_id':message_id}

def _flush_projection(state):
    pending=state.get('pending_materializations',{})
    if not pending:return
    from .session_archive import materialize
    # At most one session projection per tick; new raw messages are already durable.
    sid=next(iter(pending));latest=pending[sid]
    try:materialize(sid,source_latest=latest or None)
    except Exception:
        pending[sid]=pending.pop(sid)  # Failed session retries without starving others.
        raise
    pending.pop(sid)

def _accept_row(row,state,db_path):
    key=str(row['id']);previous=state.setdefault('observations',{}).get(key)
    sid=f"hermes:{row['session_id']}"
    if row['role'] not in {'user','assistant'} or not row['active']:
        if previous and previous.get('active',True):
            _queue_projection(state,sid,key,None);previous['active']=False
        return False
    semantic_role,semantic_kind,display_kind=_display_semantics(row)
    if semantic_role is None:
        if previous and previous.get('active',True):_queue_projection(state,sid,key,None)
        state['observations'][key]={'base_id':str(row['platform_message_id'] or row['id']),'message_id':previous.get('message_id') if previous else None,'fingerprint':_fingerprint(row['role'],row['content'] or '',[]),'session_id':sid,'active':False,'display_decision':semantic_kind}
        return False
    refs=_refs(row);content=row['content'] or ''
    if not content.strip() and not refs and previous is None:return False
    fingerprint=_fingerprint(semantic_role,content,refs,{'source_role':row['role'],'display_kind':display_kind,'decision':semantic_kind} if display_kind is not None or semantic_kind!='authored' else None)
    base_id=previous['base_id'] if previous else str(row['platform_message_id'] or row['id'])
    changed=previous is None or previous['fingerprint']!=fingerprint
    message_id=(base_id if previous is None else base_id+'~revision-'+fingerprint[:20]) if changed else previous['message_id']
    if changed:
        session_start(sid,'hermes',row['source'] or 'hermes',memory_mode='full')
        provenance={'adapter':'hermes_sqlite_readonly','hermes_row_id':row['id'],
            'hermes_session_id':row['session_id'],'source_db':str(db_path),'source_message_id':key,
            'base_message_id':base_id,'source_version':fingerprint,'source_role':row['role'],'source_display_kind':display_kind,'display_semantics':semantic_kind,'knowledge_eligible':semantic_kind=='authored'}
        for field in ('source_channel','source_recipient'):
            if field in row.keys() and row[field] is not None:provenance[field]=row[field]
        if previous:provenance['supersedes_message_id']=previous['message_id']
        appended=session_append(sid,message_id,semantic_role,content,_iso(row['timestamp']),refs,row['token_count'],provenance,materialize_archive=False)
        _queue_projection(state,sid)
    attachment_rows,attachments_changed=_attachments(sid,message_id,row['message_order'],refs,return_changed=True) if refs else ([],False)
    if previous and (changed or not previous.get('active',True)):
        _queue_projection(state,sid,key,message_id)
    elif attachments_changed:
        _queue_projection(state,sid)
    if changed:
        from .live_acceptance import record_message
        record_message(sid,appended['session']['turn_count'],message_id,attachment_rows)
    state['observations'][key]={'message_id':message_id,'base_id':base_id,'fingerprint':fingerprint,'session_id':sid,'active':True}
    return changed

def history_control(action,db_path=None,*,owner_confirmed=False):
    """Explicit historical scan, independent of the live high-water mark."""
    db_path=Path(db_path) if db_path is not None else selected_db_path()
    if action not in {'start','pause','resume'}:raise ValueError('历史导入操作应为 start、pause 或 resume')
    if owner_confirmed is not True:raise PermissionError('请确认导入或调整 Hermes 历史归档')
    if action in {'start','resume'}:
        from .memory_control import interface_state
        if not interface_state()['interfaces']['save_raw_conversation']['enabled']:raise PermissionError('原始归档已关闭，请先开启归档再导入历史')
    with _lock:
        state=initialize(db_path);history=state.get('history')
        if action=='start':
            if history and history.get('status') not in {'complete'}:raise ValueError('已有历史任务，请继续或暂停该任务')
            with closing(_connect(db_path)) as db:
                upper,count=db.execute('select coalesce(max(id),0),count(*) from messages').fetchone()
            history={'status':'running','cursor':0,'upper_id':upper,'source_rows_at_start':count,'processed_rows':0,'accepted_rows':0,'excluded_rows':0,'ignored_rows':0,'orphan_rows':0,'missing_rows':0,'started_at':_now(),'db_path':str(Path(db_path).resolve()),'last_error':None}
            state['history']=history
        elif history is None:raise ValueError('尚未开始历史导入')
        elif action=='pause':
            if history['status']!='complete':history['status']='paused'
        elif history['status']!='complete':history.update(status='running',last_error=None)
        _save(state);return dict(history)

def _history_step(state,db_path,*,batch_size=50):
    history=state.get('history')
    if not history or history.get('status')!='running':return
    if history.get('db_path')!=str(Path(db_path).resolve()):
        history.update(status='error',last_error='来源数据库位置发生变化，请核对原历史任务');_save(state);return
    began=time.monotonic();processed_this_batch=0;cursor=int(history['cursor']);upper=int(history['upper_id'])
    try:
        with closing(_connect(db_path)) as db:
            db.row_factory=sqlite3.Row
            columns={row[1] for row in db.execute('pragma table_info(sessions)')}
            message_columns={row[1] for row in db.execute('pragma table_info(messages)')}
            kind_sql='m.display_kind' if 'display_kind' in message_columns else 'null'
            routing_sql=','.join(f'm.{name} as source_{name}' if name in message_columns else f'null as source_{name}' for name in ('channel','recipient'))
            title='s.title' if 'title' in columns else 'null'
            query=f"""select m.id,m.session_id,m.role,m.content,m.timestamp,m.token_count,m.platform_message_id,m.display_metadata,m.active,{kind_sql} as display_kind,{routing_sql},s.source,s.id as linked_session_id,{title} as source_title,
                (select count(*) from messages q where q.session_id=m.session_id and q.active=1 and q.role in ('user','assistant') and q.id<=m.id) as message_order
                from messages m left join sessions s on s.id=m.session_id where m.id>? and m.id<=? order by m.id limit ?"""
            rows=db.execute(query,(cursor,upper,max(1,min(50,int(batch_size))))).fetchall()
        excluded=state.get('excluded_sessions',{})
        for row in rows:
            if processed_this_batch and time.monotonic()-began>=.5:break
            if row['linked_session_id'] is None:history['orphan_rows']=history.get('orphan_rows',0)+1
            elif row['session_id'] in excluded:history['excluded_rows']+=1
            else:
                accepted=_accept_row(row,state,db_path);history['accepted_rows']+=int(accepted)
                if row['role'] not in {'user','assistant'} or not row['active'] or _display_semantics(row)[0] is None or not (row['content'] or _refs(row)):history['ignored_rows']=history.get('ignored_rows',0)+1
                if row['role'] in {'user','assistant'} and row['active'] and _display_semantics(row)[0] is not None and (row['content'] or _refs(row)):
                    update_session_metadata('hermes:'+row['session_id'],session_title=row['source_title'])
            history['cursor']=row['id'];history['processed_rows']+=1;processed_this_batch+=1;history['last_checked_at']=_now()
            _save(state)  # Append + observation + projection intent before each durable history position.
        if not rows or history['cursor']>=upper:
            history.update(status='complete',cursor=upper,completed_at=_now(),last_error=None,missing_rows=max(0,history['source_rows_at_start']-history['processed_rows']))
        _save(state)
    except Exception as error:
        history.update(status='error',last_error=str(error));_save(state)

def sync_once(db_path=None):
    db_path=Path(db_path) if db_path is not None else selected_db_path()
    with _lock:
        state=initialize(db_path);cursor=int(state.get("cursor") or 0)
        from .memory_control import interface_state
        if not interface_state()["interfaces"]["save_raw_conversation"]["enabled"]:
            state.update({"status":"paused","last_sync":_now(),"last_error":None});_save(state);return state
        if state.get('auto_history_pending') and state.get('history') is None:
            _start_pending_history(state,db_path)
            _save(state)
        try:
            _seed_observations(state,db_path)
            with closing(_connect(db_path)) as db:
                db.row_factory=sqlite3.Row
                session_columns={row[1] for row in db.execute("pragma table_info(sessions)")}
                message_columns={row[1] for row in db.execute('pragma table_info(messages)')}
                kind_sql='m.display_kind' if 'display_kind' in message_columns else 'null'
                routing_sql=','.join(f'm.{name} as source_{name}' if name in message_columns else f'null as source_{name}' for name in ('channel','recipient'))
                title_sql="title" if "title" in session_columns else "null as title"
                activity_sql="last_activity_at" if "last_activity_at" in session_columns else "started_at as last_activity_at"
                lineage_sql=','.join(name if name in session_columns else f'null as {name}' for name in ('parent_session_id','model_config','end_reason','ended_at','session_key'))
                session_rows=db.execute(f"select id,source,{title_sql},{activity_sql},started_at,{lineage_sql} from sessions").fetchall()
                excluded=hermes_exclusions([dict(row) for row in session_rows]) if {'parent_session_id','model_config','end_reason','ended_at','session_key'}<=session_columns else {}
                state['excluded_sessions']=excluded
                known_sessions={row["session_id"] for row in list_sessions("hermes")}
                for source_session in session_rows:
                    if source_session['id'] in excluded:continue
                    sid=f"hermes:{source_session['id']}"
                    if sid not in known_sessions:
                        continue
                    activity=source_session["last_activity_at"] or source_session["started_at"]
                    update_session_metadata(sid,session_title=source_session["title"],source_last_activity_at=_iso(activity))
                query=f"""select m.id,m.session_id,m.role,m.content,m.timestamp,m.token_count,m.platform_message_id,m.display_metadata,m.active,{kind_sql} as display_kind,{routing_sql},s.source,
                (select count(*) from messages q where q.session_id=m.session_id and q.active=1 and q.role in ('user','assistant') and q.id<=m.id) as message_order
                from messages m join sessions s on s.id=m.session_id """
                rows=db.execute(query+"where m.id>? order by m.id limit 50",(cursor,)).fetchall()
                keys=sorted(state.get('observations',{}),key=int);start=state.get('recheck_next',0)%max(1,len(keys))
                selected=(keys[start:]+keys[:start])[:20]
                rechecks=db.execute(query+'where m.id in ('+','.join('?' for _ in selected)+')',selected).fetchall() if selected else []
            new_count=0;excluded_count=0
            for row in sorted(rows,key=lambda item:item['id']):
                if row['session_id'] in excluded:
                    cursor=max(cursor,row['id']);excluded_count+=1;continue
                new_count+=int(_accept_row(row,state,db_path))
                cursor=max(cursor,row["id"])
            state['excluded_messages']=int(state.get('excluded_messages',0))+excluded_count
            state.update(cursor=cursor,last_live_sync=_now(),synced_messages=int(state.get('synced_messages',0))+new_count)
            _save(state)  # Old-history work must never hold the new-message checkpoint.
            rechecked=0;revisions=0;began=time.monotonic();lookup={str(row['id']):row for row in rechecks}
            for key in selected:
                if rechecked and time.monotonic()-began>=.5:break
                row=lookup.get(key)
                if row is not None and row['session_id'] not in excluded:revisions+=int(_accept_row(row,state,db_path))
                rechecked+=1
            state['recheck_next']=(start+rechecked)%max(1,len(keys))
            state['synced_messages']+=revisions
            _save(state)  # Persist projection intents before rendering; crash-safe retry.
            _history_step(state,db_path)
            _flush_projection(state)
            state.update({"status":"running" if new_count or revisions else "watching","last_sync":_now(),"last_error":None})
        except Exception as exc:state.update({"status":"error","last_error":str(exc),"last_sync":_now()})
        _save(state);return state
def status():
    state=_load();sessions=[session_status(x["session_id"]) for x in list_sessions("hermes") if x.get("memory_mode")=="full" and x.get("user_message_count",0)+x.get("assistant_message_count",0)>0]
    state['worker_alive']=_worker_thread.is_alive() if _worker_thread is not None else None
    if _worker_error:state.update(status='error',last_error=_worker_error)
    return state|{"current_session":sessions[0] if sessions else None,"memory_mode":"full" if state.get("status") in {"watching","running"} and sessions else "readonly","archive_enabled":state.get("status")!="paused"}
def run(interval=2.0,stop_event=None,db_path=None):
    global _worker_thread,_worker_error
    _worker_thread=threading.current_thread();_worker_error=None
    stop=stop_event if stop_event is not None else threading.Event()
    while not stop.is_set():
        try:
            result=sync_once(db_path)
            _worker_error=result.get('last_error')
        except Exception as exc:
            # Initialization and final checkpoint writes also need supervision.
            _worker_error=f'{type(exc).__name__}: {exc}'
            _logger.exception('Hermes collection failed; retrying from the last durable checkpoint')
            try:
                state=_load();state.update(status='error',last_error=_worker_error)
                _save(state)  # Never advance cursor/last_sync on an outer failure.
            except Exception:
                _logger.warning('Hermes error checkpoint could not be saved; runtime error remains visible')
        stop.wait(max(.01,interval))


def summary_status():
    """Status without observations, source fingerprints or session-body reconstruction."""
    state=_load()
    result={k:state.get(k) for k in ['cursor','status','db_path','last_error','last_sync','last_live_sync','synced_messages','history']}
    result['worker_alive']=_worker_thread.is_alive() if _worker_thread is not None else None
    if _worker_error:result.update(status='error',last_error=_worker_error)
    result['pending_projection_count']=len(state.get('pending_materializations',{}))
    result['observation_count']=len(state.get('observations',{}))
    result['archive_enabled']=state.get('status')!='paused'
    return result
