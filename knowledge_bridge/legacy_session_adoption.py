"""Adopt a proven legacy session without discarding its unavailable-source history."""
from __future__ import annotations
import hashlib,json,os,shutil
from pathlib import Path
from datetime import datetime

def _read(path,default):
    try:return json.loads(path.read_text('utf-8'))
    except FileNotFoundError:return default

def _atomic(path,value):
    from .archive_store import _atomic_json
    _atomic_json(path,value)

def _state(context):return context.state/'archive-session-aliases.json'

def _identity(source_instance,source_session_id):return 'hermes\0'+source_instance+'\0'+source_session_id

def resolve_session(context,session_id):
    return _read(_state(context),{}).get('aliases',{}).get(str(session_id),str(session_id))

def find_legacy(context,source_key,source_instance,source_session_id):
    if source_key!='hermes':return None
    state=_read(_state(context),{})
    identity=_identity(source_instance,source_session_id)
    for plan in state.get('adoptions',{}).values():
        if plan.get('source_session_id')==source_session_id and plan.get('source_instance')!=source_instance:raise ValueError('旧会话来源实例冲突，保留原件等待身份核对')
    if identity in state.get('adoptions',{}):return state['adoptions'][identity]
    migration=_read(context.state/'legacy-layouts.json',{}).get('sessions',{})
    replaced={str((context.root/r['source_path']).resolve()) for r in migration.values() if r.get('source_path')!=r.get('target_path')}
    found=[]
    for path in context.raw_archive.glob('Hermes/**/manifest.json'):
        if '00_原始数据' in path.parts or str(path.parent.resolve()) in replaced:continue
        manifest=_read(path,{})
        if manifest.get('session_key'):continue
        if str(manifest.get('session_id') or '')!=source_session_id:continue
        proven_instance=manifest.get('source_instance') or manifest.get('source')
        if not proven_instance or proven_instance.lower()=='hermes':raise ValueError('旧会话缺少可靠来源实例，保留原件等待身份核对')
        if proven_instance!=source_instance:raise ValueError('旧会话来源实例冲突，保留原件等待身份核对')
        messages=path.parent/'messages.jsonl'
        if not messages.is_file():continue
        folder=path.parent.parent if path.parent.name=='03_元数据' else path.parent
        found.append((folder,path,messages,manifest))
    if len(found)>1:raise ValueError('同一来源存在多个旧会话投影，需先核对身份')
    if not found:return None
    folder,manifestpath,messages,manifest=found[0]
    # Capture paths before creating a nested snapshot, preventing recursive copy.
    files=[p for p in folder.rglob('*') if p.is_file() and '旧会话接入原件' not in p.parts]
    token=hashlib.sha256(manifestpath.read_bytes()+messages.read_bytes()).hexdigest()
    snapshot=folder/'00_原始数据'/'旧会话接入原件'/token
    inventory=[]
    for path in files:
        relative=path.relative_to(folder);dest=snapshot/relative;digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:raise ValueError('旧会话保护副本校验冲突')
        if not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
        if hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:raise ValueError('旧会话保护副本未完整保存')
        inventory.append({'path':relative.as_posix(),'sha256':digest,'bytes':path.stat().st_size})
    plan={'source_session_id':source_session_id,'source_instance':source_instance,'folder':context.relative(folder),'snapshot':context.relative(snapshot),'messages':context.relative(snapshot/messages.relative_to(folder)),'manifest':manifest,'inventory':inventory,'status':'prepared'}
    state.setdefault('adoptions',{})[identity]=plan;_atomic(_state(context),state)
    return plan

def finish_adoption(store,item,plan):
    from .archive_store import NormalizedMessage,NormalizedAttachment
    context=store.context;state=_read(_state(context),{});current=state.get('adoptions',{}).get(_identity(plan['source_instance'],plan['source_session_id']),plan)
    if current.get('status')=='complete':return
    folder=context.root/plan['folder'];snapshot=context.root/plan['snapshot'];rows=[]
    with (context.root/plan['messages']).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():rows.append(json.loads(line))
    attachments=[]
    for path in snapshot.rglob('attachments.jsonl'):
        for line in path.read_text('utf-8').splitlines():
            if line.strip():attachments.append(json.loads(line))
    for row in rows:
        mid=str(row.get('message_id') or '')
        if not mid:raise ValueError('旧会话消息缺少稳定身份')
        text=row.get('original_text',row.get('content','')) or '';declared=row.get('content_sha256') or row.get('hash')
        if declared and declared!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('旧会话消息原文校验失败')
        provenance=dict(row.get('provenance') or {});provenance.update(legacy_adoption={'source_session_id':plan['source_session_id'],'snapshot':plan['snapshot'],'original_message_id':mid,'original_role':row.get('role')})
        store.append_normalized_message(item['session_key'],NormalizedMessage(message_id=mid,role=row.get('role','user'),content=text,timestamp=row.get('timestamp'),provenance=provenance),materialize=False)
        specs=[]
        candidates=list(row.get('attachments') or [])+[x for x in attachments if str(x.get('source_message_id') or x.get('message_id'))==mid]
        seen=set()
        for attachment in candidates:
            if not isinstance(attachment,dict):continue
            ref=str(attachment.get('source_reference') or attachment.get('reference') or attachment.get('original_name') or '')
            if not ref or ref in seen:continue
            seen.add(ref);stored=attachment.get('stored_path') or attachment.get('archived_path');path=Path(stored) if stored else None
            if path and not path.is_absolute():
                path=context.root/path if (context.root/path).is_file() else folder/path
            if path and path.is_file():
                if not path.resolve().is_relative_to(context.root.resolve()):raise ValueError('旧附件定位超出当前工作区')
                expected=attachment.get('sha256')
                if expected and hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('旧附件原件校验失败')
            else:path=None
            specs.append(NormalizedAttachment(reference=ref,original_name=attachment.get('original_name') or (path.name if path else '旧附件'),local_path=path))
        if specs:store.reconcile_attachments(item['session_key'],mid,specs,materialize=False)
    # Commit aliases only after every old message is durably retained. A crash retries exact IDs.
    state=_read(_state(context),{});state.setdefault('aliases',{})[plan['source_session_id']]=item['session_key'];state['adoptions'][_identity(plan['source_instance'],plan['source_session_id'])].update(status='complete',session_key=item['session_key'],retained_messages=len(rows));_atomic(_state(context),state)

def _signature(row):
    text=row.get('original_text',row.get('content','')) or '';stamp=row.get('timestamp')
    try:stamp=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError,AttributeError):pass
    return row.get('role'),hashlib.sha256(text.encode()).hexdigest(),stamp

def project_aliases(context,item,rows):
    """Exact legacy/current signature links; raw evidence and deleted-source rows stay intact."""
    if not item.get('legacy_adoption'):return rows
    legacy={};current={}
    for row in rows:
        provenance=row.get('provenance') or {}
        target=legacy if provenance.get('legacy_adoption') else current
        signature_row=row
        if provenance.get('adapter')=='hermes_sqlite_readonly' and provenance.get('display_semantics')=='system_notice':
            signature_row=dict(row,role=provenance.get('source_role'))
        target.setdefault(_signature(signature_row),[]).append(row)
    aliases={}
    for signature,old in legacy.items():
        new=current.get(signature,[])
        if len(old)==1 and len(new)==1 and old[0]['message_id']!=new[0]['message_id']:aliases[str(old[0]['message_id'])]=str(new[0]['message_id'])
    state=_read(_state(context),{});links=state.setdefault('message_aliases',{}).setdefault(item['session_key'],{})
    overrides=state.setdefault('semantic_overrides',{}).setdefault(item['session_key'],{})
    visible={str(row['message_id']):row for row in rows}
    for source,target in aliases.items():
        if source in links and links[source]!=target and (visible[target].get('provenance') or {}).get('supersedes_message_id')!=links[source]:raise ValueError('旧消息身份映射发生冲突')
        links[source]=target
        provenance=visible[target].get('provenance') or {}
        if provenance.get('adapter')=='hermes_sqlite_readonly' and provenance.get('display_semantics')=='system_notice':overrides[source]=dict(provenance)
    if aliases:_atomic(_state(context),state)
    # Preserve attachment bindings from the old message on its exact current counterpart.
    metadata=context.root/item['relative_path']/'03_元数据';ledger=metadata/'attachments.jsonl'
    historical=[]
    if ledger.is_file():historical=[json.loads(x) for x in ledger.read_text('utf-8').splitlines() if x.strip()]
    latest={(str(x.get('source_message_id')),x.get('source_reference')):x for x in historical};copies=[]
    signature=lambda row:tuple(row.get(k) for k in ('status','missing','sha256','stored_path','archived_path','size_bytes','original_name','mime_type'))
    for row in list(latest.values()):
        old=str(row.get('source_message_id'));new=links.get(old)
        if new not in visible:continue
        key=(new,row.get('source_reference'))
        if new and signature(latest.get(key,{}))!=signature(row):
            copy=dict(row,source_message_id=new,legacy_bound_message_id=old);copies.append(copy);latest[key]=copy
    if copies:
        _append_alias_attachments(ledger,copies)
    output=[]
    for row in rows:
        mid=str(row.get('message_id'))
        if mid in links and links[mid] in visible:continue
        if mid in overrides:row=dict(row,role='system',provenance=dict(row.get('provenance') or {},**overrides[mid],legacy_semantics_corrected=True))
        output.append(row)
    return output

def _append_alias_attachments(ledger,copies):
    # Preserve the exact ledger prefix; atomic replacement prevents a torn last JSON row.
    import uuid
    prefix=ledger.read_bytes() if ledger.is_file() else b''
    suffix=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in copies).encode('utf-8')
    temporary=ledger.with_name('.'+ledger.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('wb') as stream:stream.write(prefix+suffix);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,ledger)
    finally:
        if temporary.exists():temporary.unlink()

def resolve_message(context,session_key,message_id):
    target=_read(_state(context),{}).get('message_aliases',{}).get(session_key,{}).get(str(message_id))
    if not target:return str(message_id)
    registry=_read(context.archive_registry,{}).get('sessions',{});item=registry.get(session_key,{})
    path=context.root/item.get('relative_path','')/'03_元数据'/'messages.jsonl'
    if path.is_file():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip() and str(json.loads(line).get('message_id'))==target:return target
    return str(message_id)
