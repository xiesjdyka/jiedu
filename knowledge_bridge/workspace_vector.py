"""Separate semantic index for ordinary local Markdown, never formal knowledge."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from .workspace import active_workspace

_TASK_LOCK=threading.Lock()
_TASKS={}
_ACTIVE={}


def _write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
    os.replace(temporary,path)


def _root(context):
    return context.index_root('workspace')


def _descriptor(context):
    try:
        value=json.loads((_root(context)/'active-vector.json').read_text('utf-8'))
        if value.get('workspace_id')==context.workspace_id:return value
    except (OSError, ValueError, AttributeError):pass
    return {}


def _fingerprint(context, files):
    from .search_scopes import _filesystem_fingerprint
    return _filesystem_fingerprint(context, files)


def status(context=None):
    from .search_scopes import files_for_scope
    context=context or active_workspace();files=files_for_scope(context,'workspace')
    descriptor=_descriptor(context)
    fresh=bool(descriptor.get('filesystem_fingerprint')==_fingerprint(context,files))
    return {'ready':bool(descriptor.get('table') and fresh), 'fresh':fresh,
            'vector_count':int(descriptor.get('vector_count') or 0),
            'source_count':int(descriptor.get('file_count') or 0),
            'generation':descriptor.get('generation'),
            'built_at':descriptor.get('built_at')}


def rebuild(context=None,*,progress=None):
    from .core import embed, configured_embedding_batch_size
    from .passage_search import metadata, passages
    from .search_scopes import files_for_scope
    from .index_gate import IndexWriteGate, IndexBusyError
    import lancedb

    context=context or active_workspace();files=files_for_scope(context,'workspace')
    root=_root(context);root.mkdir(parents=True,exist_ok=True)
    try:
        with IndexWriteGate(context).hold('workspace_vector_rebuild',8.0):
            before=_fingerprint(context,files);rows=[]
            for path in files:
                relative=context.relative(path)
                raw=path.read_bytes();text=raw.decode('utf-8-sig',errors='replace')
                _,begin=metadata(text)
                for passage_index,passage in enumerate(passages(text,begin)):
                    content=passage['text'].strip()
                    for piece_index,start in enumerate(range(0,len(content),1200)):
                        piece=content[start:start+1400]
                        if not piece:continue
                        stable=f'{relative}|{passage_index}|{piece_index}'
                        rows.append({'source_path':relative,'document_title':path.stem,
                                     'section_title':passage.get('section_title') or path.stem,
                                     'chunk_id':'w1-'+hashlib.sha256(stable.encode()).hexdigest()[:32],
                                     'content':piece,'source_version':hashlib.sha256(raw).hexdigest(),
                                     'source_type':'source_document'})
            if _fingerprint(context,files)!=before:
                return {'status':'source_changed','pointer_switched':False}
            generation=uuid.uuid4().hex;table_name=f'workspace_{generation}'
            if progress:progress(0,len(rows))
            if rows:
                batch_size=configured_embedding_batch_size()
                for start in range(0,len(rows),batch_size):
                    batch=rows[start:start+batch_size]
                    vectors=embed([row['content'] for row in batch],local_only=True,batch_size=batch_size)
                    for row,vector in zip(batch,vectors):row['vector']=vector.tolist()
                    if progress:progress(min(start+len(batch),len(rows)),len(rows))
                database=lancedb.connect(root/'lancedb')
                created=database.create_table(table_name,data=rows)
                if created.count_rows()!=len(rows):
                    database.drop_table(table_name)
                    raise RuntimeError('本机资料向量索引校验失败')
            if _fingerprint(context,files)!=before:
                if rows:database.drop_table(table_name)
                return {'status':'source_changed','pointer_switched':False}
            value={'version':1,'workspace_id':context.workspace_id,'generation':generation,
                   'table':table_name if rows else None,'file_count':len(files),'vector_count':len(rows),
                   'filesystem_fingerprint':before,'built_at':datetime.now(timezone.utc).isoformat()}
            target=root/'active-vector.json';temporary=root/f'.active-vector-{generation}.tmp'
            temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
            os.replace(temporary,target)
            return {'status':'rebuilt' if rows else 'empty_corpus','file_count':len(files),
                    'vector_count':len(rows),'generation':generation,'pointer_switched':True}
    except IndexBusyError as exc:return exc.public_status()


def _task_path(context):return _root(context)/'task.json'


def _public_task(row):return {key:value for key,value in row.items() if not key.startswith('_')}


def active_task(context=None):
    context=context or active_workspace()
    with _TASK_LOCK:
        row=_ACTIVE.get(context.workspace_id)
        return _public_task(row) if row and row.get('_thread') and row['_thread'].is_alive() else None


def latest_task(context=None):
    context=context or active_workspace()
    running=active_task(context)
    if running:return running
    try:row=json.loads(_task_path(context).read_text('utf-8'))
    except (OSError,ValueError):return None
    if row.get('workspace_id')!=context.workspace_id:return None
    if row.get('status') in {'queued','running'}:
        row['status']='interrupted'
        row['result']={'status':'interrupted','message':'索引进程已退出；可重新建立索引'}
    return row


def task_status(task_id,context=None):
    context=context or active_workspace()
    with _TASK_LOCK:row=_TASKS.get(task_id)
    if row and row.get('workspace_id')==context.workspace_id:return _public_task(row)
    persisted=latest_task(context)
    if persisted and persisted.get('task_id')==task_id:return persisted
    raise ValueError('本机资料索引任务不存在')


def start_rebuild(context=None):
    context=context or active_workspace()
    with _TASK_LOCK:
        current=_ACTIVE.get(context.workspace_id)
        if current and current.get('_thread') and current['_thread'].is_alive():
            return _public_task(current)|{'reused':True}
        task_id=uuid.uuid4().hex
        row={'task_id':task_id,'workspace_id':context.workspace_id,'status':'queued',
             'created_at':datetime.now(timezone.utc).isoformat(),
             'embedded_chunks':0,'total_chunks':None,'result':None}
        _TASKS[task_id]=row;_ACTIVE[context.workspace_id]=row
        _write_json(_task_path(context),_public_task(row))
        def progress(done,total):
            with _TASK_LOCK:
                row.update(status='running',embedded_chunks=done,total_chunks=total)
                _write_json(_task_path(context),_public_task(row))
        def run():
            with _TASK_LOCK:
                row.update(status='running',started_at=datetime.now(timezone.utc).isoformat())
                _write_json(_task_path(context),_public_task(row))
            try:result=rebuild(context,progress=progress)
            except Exception as exc:result={'status':'failed','error':type(exc).__name__,'message':str(exc)}
            with _TASK_LOCK:
                row.update(status=result.get('status','failed'),result=result,
                           finished_at=datetime.now(timezone.utc).isoformat())
                _write_json(_task_path(context),_public_task(row))
                if _ACTIVE.get(context.workspace_id) is row:_ACTIVE.pop(context.workspace_id,None)
        thread=threading.Thread(target=run,name=f'workspace-index-{task_id[:8]}',daemon=True)
        row['_thread']=thread;thread.start()
        return _public_task(row)


def table(context=None):
    import lancedb
    context=context or active_workspace();name=_descriptor(context).get('table')
    if not name:return None
    try:return lancedb.connect(_root(context)/'lancedb').open_table(name)
    except ValueError as exc:
        if 'was not found' in str(exc):return None
        raise


def vector_rows(query_vector,limit=80,context=None,source_type=None,folder=None,project=None):
    context=context or active_workspace();active=table(context)
    if active is None or not status(context)['ready']:return []
    rows=active.search(query_vector).limit(max(100,int(limit)*4)).to_arrow().to_pylist();result=[]
    from .search_scopes import files_for_scope
    allowed={context.relative(path) for path in files_for_scope(context,'workspace')}
    for row in rows:
        source=row.get('source_path','')
        if source not in allowed or (source_type and row.get('source_type')!=source_type):continue
        if folder and not source.startswith(folder.strip('/')+'/'):continue
        distance=float(row.get('_distance',1))
        result.append({key:row.get(key) for key in ('source_path','document_title','section_title','chunk_id','content','source_version','source_type')}|
                      {'score':1/(1+distance),'search_method':'vector'})
        if len(result)>=limit:break
    return result
