"""Isolated vector index for canonical public evidence projections."""
from __future__ import annotations

import hashlib,json,os,threading,time,uuid
from datetime import datetime,timezone
from pathlib import Path

import lancedb

from .index_gate import IndexBusyError,IndexWriteGate
from .workspace import active_workspace

CHUNK_CONFIG='evidence-message-char-v2:1400:180:adjacent-500-700'
EMBEDDING_PRECISION=os.environ.get('KNOWLEDGE_BRIDGE_MODEL_PRECISION','fp32').strip().lower()
if EMBEDDING_PRECISION not in {'fp32','fp16'}:raise ValueError('KNOWLEDGE_BRIDGE_MODEL_PRECISION 只支持 fp32 或 fp16')


def _sha(value:bytes|str)->str:
    if isinstance(value,str):value=value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()

EMBEDDING_CONFIG_HASH=_sha('BAAI/bge-m3|'+CHUNK_CONFIG+'|precision='+EMBEDDING_PRECISION)
_TASK_LOCK=threading.Lock();_TASKS={}
_SYNC_THREAD_LOCK=threading.Lock();_SYNC_THREAD=None

def _paths(context):
    root=context.index_root('evidence')
    return root,root/'lancedb',root/'active-vector.json'

def _atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8');os.replace(temporary,path)

def _descriptor(context):
    _,_,path=_paths(context)
    try:
        value=json.loads(path.read_text('utf-8'))
        if value.get('version') in (1,2):return value
    except (OSError,ValueError,AttributeError):pass
    return {'version':0,'table':None,'state_file':None}

def _state(context):
    root,_,_=_paths(context);relative=_descriptor(context).get('state_file')
    if not relative:return {'files':{},'source_version':None}
    try:return json.loads((root/relative).read_text('utf-8'))
    except (OSError,ValueError):return {'files':{},'source_version':None}

def table(context=None,descriptor=None):
    context=context or active_workspace();_,db_path,_=_paths(context)
    descriptor=descriptor or _descriptor(context);name=descriptor.get('table')
    if not name or not db_path.exists():return None
    database=lancedb.connect(db_path)
    # Never infer existence from the first page of generation names.
    try:
        from .evidence_storage import select
        return select(database,descriptor)
    except ValueError as exc:
        if 'was not found' in str(exc):return None
        raise

def _session_identity(path,context):
    current=path.parent
    while current!=context.raw_archive.parent and current.is_relative_to(context.raw_archive):
        for metadata in (current/'03_元数据',current):
            manifest=metadata/'manifest.json'
            if not manifest.is_file():continue
            try:value=json.loads(manifest.read_text('utf-8'))
            except (OSError,ValueError):continue
            return str(value.get('session_key') or value.get('session_id') or value.get('source_session_id') or _sha(context.relative(current))[:24])
        current=current.parent
    return _sha(context.relative(path))[:24]

def _split(text,limit=1400,overlap=180):
    text=text.strip()
    if not text:return []
    if len(text)<=limit:return [text]
    rows=[];start=0
    while start<len(text):
        end=min(len(text),start+limit);cut=text.rfind('\n',start,end)
        if cut<=start+limit//2:cut=end
        rows.append(text[start:cut].strip())
        if cut>=len(text):break
        start=max(start+1,cut-overlap)
    return [row for row in rows if row]

def rows_for_file(path,context):
    from .passage_search import metadata,passages
    raw=path.read_bytes();text=raw.decode('utf-8-sig',errors='replace');_,begin=metadata(text)
    parsed=list(passages(text,begin));source=context.relative(path);session_id=_session_identity(path,context)
    canonical_conversation=path.name.casefold()=='conversation.md'
    source_version=_sha(raw);rows=[];message_piece_indices={}
    modified_at=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()
    for passage_index,passage in enumerate(parsed):
        before=parsed[passage_index-1]['text'][-500:] if passage_index else ''
        after=parsed[passage_index+1]['text'][:700] if passage_index+1<len(parsed) else ''
        pieces=_split(passage['text'])
        for piece_index,piece in enumerate(pieces):
            message_id=str(passage.get('message_id') or '')
            if message_id and canonical_conversation:
                stable_piece=message_piece_indices.get(message_id,0);message_piece_indices[message_id]=stable_piece+1
                stable=f'{session_id}|{message_id}|{stable_piece}'
            else:
                stable_piece=piece_index;stable=f'{source}|{passage_index}|{piece_index}'
            embedding_text='\n'.join(x for x in (f'前文：{before}' if before else '',f'当前：{piece}',f'后文：{after}' if after else '') if x)
            rows.append({'source_path':source,'document_title':path.stem,'section_title':passage.get('section_title') or path.stem,
                'chunk_id':'e1-'+_sha(stable)[:32],'content':piece,'embedding_text':embedding_text[:3000],
                'content_hash':_sha(piece),'embedding_config_hash':EMBEDDING_CONFIG_HASH,
                'embedding_precision':EMBEDDING_PRECISION,
                'vector_cache_key':_sha(embedding_text+'|'+EMBEDDING_CONFIG_HASH),
                'source_version':source_version,'session_id':session_id,
                'message_id':message_id,'role':str(passage.get('message_role') or ''),'message_order':int(passage.get('message_order') or -1),
                'chunk_index_in_message':stable_piece,'source_type':'raw_chat' if message_id and canonical_conversation else 'derived_document',
                'modified_at':modified_at})
    return raw,rows

_HIGH_VALUE=('纠正','不是','不要','停止','决定','改为','采用','方案','最终','确认','替代','暂停','恢复','失败','结论','历史','最初')

def benchmark_projection_rows(rows):
    """Measurement-only sample policy; never used by production index builds."""
    selected=[];groups={};derived=[]
    for row in rows:
        if row.get('message_id'):groups.setdefault(row['message_id'],[]).append(row)
        else:derived.append(row)
    for group in groups.values():
        combined='\n'.join(row['content'] for row in group);role=group[0].get('role')
        if role!='USER' and not any(term in combined for term in _HIGH_VALUE):continue
        best=max(group,key=lambda row:(sum(term in row['content'] for term in _HIGH_VALUE),len(row['content'])))
        content=best['content'];positions=[content.find(term) for term in _HIGH_VALUE if term in content]
        point=min((position for position in positions if position>=0),default=0);start=max(0,point-100);excerpt=content[start:start+520]
        item=dict(best);item['content']=excerpt;item['embedding_text']=excerpt;item['content_hash']=_sha(excerpt)
        item['projection_reason']='user_message' if role=='USER' else 'decision_or_correction'
        selected.append(item)
    for row in derived:
        item=dict(row);item['embedding_text']=row['content'][:520];item['content']=row['content'][:520]
        item['content_hash']=_sha(item['content']);item['projection_reason']='readable_derived_attachment';selected.append(item)
    return selected

def _source_version(files):
    compact={key:{'hash':value['hash'],'chunks':value['chunks']} for key,value in sorted(files.items())}
    return _sha(json.dumps(compact,ensure_ascii=False,separators=(',',':')))

def freshness(context=None):
    from .search_scopes import files_for_scope
    context=context or active_workspace();saved=_state(context).get('files',{});current={};unreadable=[]
    for path in files_for_scope(context,'evidence'):
        try:current[context.relative(path)]=_sha(path.read_bytes())
        except OSError:unreadable.append(context.relative(path))
    stale=sorted(key for key,value in current.items() if saved.get(key,{}).get('hash')!=value)
    missing=sorted(set(saved)-set(current))
    return {'fresh':not stale and not missing and not unreadable,'actual_files':len(current),'indexed_files':len(saved),
        'stale_or_unindexed':stale,'indexed_but_missing':missing,'unreadable':unreadable,
        'source_version':_source_version(saved) if saved else None}

def rebuild(context=None,*,operation='evidence_rebuild',lock_timeout=8.0,max_chunks=5000,allow_large=False,
            cancel_event=None,progress=None):
    from .core import embed,model,local_model_status,configured_embedding_batch_size,configured_embedding_duty_cycle
    from .search_scopes import files_for_scope
    context=context or active_workspace();root,db_path,pointer=_paths(context)
    try:
        with IndexWriteGate(context).hold(operation,lock_timeout):
            started=time.perf_counter();rows=[];file_state={}
            for path in files_for_scope(context,'evidence'):
                raw,parsed=rows_for_file(path,context);source=context.relative(path)
                rows.extend(parsed);file_state[source]={'hash':_sha(raw),'chunks':len(parsed),'mtime_ns':path.stat().st_mtime_ns}
            ids=[row['chunk_id'] for row in rows]
            if len(ids)!=len(set(ids)):raise RuntimeError('证据索引校验失败：知识块身份重复')
            if len(rows)>int(max_chunks) and not allow_large:
                return {'status':'build_deferred','reason':'measured_corpus_exceeds_unapproved_build_limit',
                    'file_count':len(file_state),'source_chunks':len(rows),'max_chunks':int(max_chunks),
                    'source_version':_source_version(file_state),'elapsed_ms':round((time.perf_counter()-started)*1000,1)}
            generation=uuid.uuid4().hex;table_name=f'evidence_{generation}'
            db_path.mkdir(parents=True,exist_ok=True);database=lancedb.connect(db_path)
            batch_size=configured_embedding_batch_size();duty_cycle=configured_embedding_duty_cycle();throttle_wait=0.0
            if rows:
                if duty_cycle<1:model(local_only=True)  # Do not count one-time model loading as batch compute duty.
                vectors=[]
                for start in range(0,len(rows),batch_size):
                    if cancel_event is not None and cancel_event.is_set():
                        return {'status':'cancelled','generation':None,'embedded_chunks':len(vectors),
                            'total_chunks':len(rows),'pointer_switched':False}
                    batch=rows[start:start+batch_size]
                    encoded_at=time.perf_counter()
                    encoded=embed([row['embedding_text'] for row in batch],local_only=True,batch_size=batch_size)
                    encoded_seconds=time.perf_counter()-encoded_at
                    vectors.extend(encoded)
                    if progress:progress(len(vectors),len(rows))
                    if start+batch_size<len(rows) and duty_cycle<1:
                        wait_seconds=min(2.0,encoded_seconds*(1/duty_cycle-1));throttle_wait+=wait_seconds
                        if cancel_event is not None:
                            if cancel_event.wait(wait_seconds):
                                return {'status':'cancelled','generation':None,'embedded_chunks':len(vectors),
                                    'total_chunks':len(rows),'pointer_switched':False,
                                    'throttle_duty_cycle':duty_cycle,'throttle_wait_seconds':round(throttle_wait,3)}
                        else:time.sleep(wait_seconds)
                if cancel_event is not None and cancel_event.is_set():
                    return {'status':'cancelled','generation':None,'embedded_chunks':len(vectors),
                        'total_chunks':len(rows),'pointer_switched':False}
                for row,vector in zip(rows,vectors):row.pop('embedding_text',None);row['vector']=vector.tolist()
                created=database.create_table(table_name,data=rows)
                if created.count_rows()!=len(rows) or len({row['chunk_id'] for row in created.to_arrow().to_pylist()})!=len(rows):
                    database.drop_table(table_name);raise RuntimeError('证据索引校验失败：落盘数据重复或缺失')
            source_version=_source_version(file_state);state_relative=f'generations/state-{generation}.json'
            state={'files':file_state,'source_version':source_version,'last_sync':datetime.now(timezone.utc).isoformat(),
                'last_result':{'status':'rebuilt','generation':generation,'file_count':len(file_state),'embedded_chunks':len(rows),
                    'source_version_after':source_version,'elapsed_ms':round((time.perf_counter()-started)*1000,1),
                    'throttle_duty_cycle':duty_cycle,'throttle_wait_seconds':round(throttle_wait,3),
                    'model_process':{'pid':os.getpid(),**local_model_status()}}}
            _atomic(root/state_relative,state)
            _atomic(pointer,{'version':2,'workspace_id':context.workspace_id,'generation':generation,
                'table_version':created.version if rows else None,
                'table':table_name if rows else None,'state_file':state_relative,'activated_at':state['last_sync']})
            from .search_scopes import publish_evidence_manifest
            publish_evidence_manifest(context,file_state)
            from .index_retention import after_publish
            after_publish(context,'evidence')
            return state['last_result']
    except IndexBusyError as exc:return exc.public_status()

def start_rebuild(context=None,*,max_chunks=5000,allow_large=False):
    context=context or active_workspace();task_id=uuid.uuid4().hex;cancel=threading.Event()
    task={'task_id':task_id,'status':'queued','created_at':datetime.now(timezone.utc).isoformat(),
          'embedded_chunks':0,'total_chunks':None,'cancel_requested':False,'result':None,'_cancel':cancel}
    with _TASK_LOCK:_TASKS[task_id]=task
    def update(done,total):
        with _TASK_LOCK:task.update(status='running',embedded_chunks=done,total_chunks=total)
    def run():
        with _TASK_LOCK:task['status']='running';task['started_at']=datetime.now(timezone.utc).isoformat()
        try:result=rebuild(context,operation=f'evidence_task_{task_id[:8]}',lock_timeout=8,max_chunks=max_chunks,
                           allow_large=allow_large,cancel_event=cancel,progress=update)
        except Exception as exc:result={'status':'failed','error':type(exc).__name__,'message':str(exc)}
        with _TASK_LOCK:
            task['result']=result;task['status']=result.get('status','failed');task['finished_at']=datetime.now(timezone.utc).isoformat()
            task['embedded_chunks']=result.get('embedded_chunks',task['embedded_chunks'])
            task['total_chunks']=result.get('total_chunks',task['total_chunks'])
    thread=threading.Thread(target=run,name=f'evidence-index-{task_id[:8]}',daemon=True);task['_thread']=thread;thread.start()
    return task_status(task_id)

def cancel_rebuild(task_id):
    with _TASK_LOCK:
        task=_TASKS.get(task_id)
        if not task:raise ValueError('证据索引任务不存在')
        if task['status'] not in {'rebuilt','cancelled','failed','build_deferred'}:
            task['cancel_requested']=True;task['_cancel'].set()
    return task_status(task_id)

def task_status(task_id):
    with _TASK_LOCK:
        task=_TASKS.get(task_id)
        if not task:raise ValueError('证据索引任务不存在')
        return {key:value for key,value in task.items() if not key.startswith('_')}

def sync(paths=None,context=None,*,operation='evidence_incremental',lock_timeout=.15):
    """Append changed chunks, then atomically publish a versioned snapshot."""
    from .core import embed,local_model_status,configured_embedding_batch_size,configured_embedding_duty_cycle,ModelBusyError
    from .interactive_priority import foreground
    from .vector_progress import VectorProgress
    from .search_scopes import files_for_scope
    context=context or active_workspace();root,db_path,pointer=_paths(context)
    if foreground.is_set():return {'status':'foreground_deferred','pointer_switched':False}
    cache=None
    try:
        with IndexWriteGate(context).hold(operation,lock_timeout):
            started=time.perf_counter();saved=_state(context);prior=saved.get('files',{});selected=None
            if paths is not None:selected={context.relative(Path(path)) if Path(path).is_absolute() else str(path).replace('\\','/') for path in paths}
            current={context.relative(path):path for path in files_for_scope(context,'evidence')}
            changed=[]
            for source,path in current.items():
                if selected is not None and source not in selected:continue
                digest=_sha(path.read_bytes())
                if prior.get(source,{}).get('hash')!=digest:changed.append((source,path,digest))
            deleted=[source for source in set(prior)-set(current) if selected is None or source in selected]
            if not changed and not deleted:
                from .evidence_storage import migrate_legacy
                migrate_legacy(context,_descriptor(context))
                from .index_retention import after_publish
                after_publish(context,'evidence')
                return {'status':'unchanged','generation':_descriptor(context).get('generation'),'added_or_modified':0,'deleted':0,
                    'embedded_chunks':0,'source_version_after':saved.get('source_version'),'elapsed_ms':round((time.perf_counter()-started)*1000,1)}
            from .evidence_storage import condition,chunk_delta,write_delta
            import pyarrow as pa
            descriptor=_descriptor(context);active=table(context,descriptor)
            active_rows=[];active_schema=None;untouched_ids=[]
            touched={source for source,_,_ in changed}|set(deleted)
            if active is not None:
                active_schema=active.schema
                active_rows=active.search().where(condition('source_path',touched)).to_arrow().to_pylist()
                # Only IDs of the rest are needed for cross-source collision checks.
                untouched_ids=active.search().where('NOT ('+condition('source_path',touched)+')').select(['chunk_id']).to_arrow().column('chunk_id').to_pylist()
            reusable={(row.get('chunk_id'),row.get('vector_cache_key')):row.get('vector') for row in active_rows
                      if row.get('chunk_id') and row.get('vector_cache_key') and row.get('vector') is not None}
            cache=VectorProgress(root);batch_size=configured_embedding_batch_size();duty_cycle=configured_embedding_duty_cycle()
            embedded_count=0;cached_count=0;throttle_wait=0.0
            file_state=dict(prior);new_rows=[]
            for source in deleted:file_state.pop(source,None)
            for source,path,digest in changed:
                raw,parsed=rows_for_file(path,context)
                if _sha(raw)!=digest:
                    return {'status':'source_changed','pointer_switched':False,'path':source}
                missing=[]
                for row in parsed:
                    vector=reusable.get((row['chunk_id'],row['vector_cache_key']))
                    if vector is None:
                        vector=cache.get(row['vector_cache_key'])
                        if vector is not None:cached_count+=1
                    if vector is None:missing.append(row)
                    else:row.pop('embedding_text',None);row['vector']=vector
                if missing:
                    for offset in range(0,len(missing),batch_size):
                        if foreground.is_set():return {'status':'foreground_deferred','pointer_switched':False,'embedded_chunks':embedded_count,'cached_vectors':cached_count}
                        if embedded_count and time.perf_counter()-started>20:return {'status':'budget_deferred','pointer_switched':False,'embedded_chunks':embedded_count,'cached_vectors':cached_count}
                        if not path.is_file() or _sha(path.read_bytes())!=digest:return {'status':'source_changed','pointer_switched':False,'path':source,'embedded_chunks':embedded_count,'cached_vectors':cached_count}
                        batch=missing[offset:offset+batch_size];tick=time.perf_counter()
                        try:vectors=embed([row['embedding_text'] for row in batch],local_only=True,batch_size=batch_size,lock_timeout=.1)
                        except ModelBusyError:return {'status':'foreground_deferred','pointer_switched':False,'embedded_chunks':embedded_count}
                        for row,vector in zip(batch,vectors):row['vector']=vector.tolist()
                        cache.put_batch(batch);embedded_count+=len(batch)
                        wait=(time.perf_counter()-tick)*(1/max(.05,duty_cycle)-1)
                        throttle_wait+=wait
                        if foreground.wait(wait):return {'status':'foreground_deferred','pointer_switched':False,'embedded_chunks':embedded_count,'cached_vectors':cached_count}
                for row in parsed:row.pop('embedding_text',None)
                new_rows.extend(parsed)
                file_state[source]={'hash':digest,'chunks':len(parsed),'mtime_ns':path.stat().st_mtime_ns}
            ids=untouched_ids+[row['chunk_id'] for row in new_rows]
            if len(ids)!=len(set(ids)):raise RuntimeError('证据增量索引校验失败：知识块身份重复')
            for source,path,digest in changed:
                if not path.is_file() or _sha(path.read_bytes())!=digest:
                    return {'status':'source_changed','pointer_switched':False,'path':source}
            if deleted:
                # A canonical duplicate or excluded document may still exist on
                # disk. Revalidate membership, not existence: otherwise every
                # reconciliation retries forever and no generation can publish.
                selected_now={context.relative(path) for path in files_for_scope(context,'evidence')}
                if any(source in selected_now for source in deleted) or any(source not in selected_now for source,_,_ in changed):
                    return {'status':'source_changed','pointer_switched':False,'reason':'evidence_selection_changed'}
            generation=uuid.uuid4().hex;db_path.mkdir(parents=True,exist_ok=True);database=lancedb.connect(db_path)
            delta,removed=chunk_delta(active_rows,new_rows)
            if active is not None and descriptor.get('version')==2:
                table_name=descriptor['table']
                created=write_delta(database,descriptor,delta,removed,active_schema,len(ids))
                storage_mode='chunk_delta';written_chunks=len(delta)
            else:
                # One-time legacy migration. Old processes only understand v1
                # pointers and will fall back to lexical retrieval, never read
                # an unpublished mutable snapshot. Restart loads the new reader.
                table_name=f'evidence_{generation}'
                if active is not None:
                    kept=active.search().where('NOT ('+condition('source_path',touched)+')').to_arrow()
                    combined=pa.concat_tables([kept,pa.Table.from_pylist(new_rows,schema=active_schema)])
                else:combined=new_rows
                created=database.create_table(table_name,data=combined) if len(ids) else None
                if created is not None and created.count_rows()!=len(ids):
                    raise RuntimeError('证据索引迁移校验失败：落盘数量不一致')
                storage_mode='initial_or_legacy_migration';written_chunks=len(ids)
            source_version=_source_version(file_state);state_relative=f'generations/state-{generation}.json';at=datetime.now(timezone.utc).isoformat()
            result={'status':'incremental_synced','generation':generation,'storage_mode':storage_mode,'written_chunks':written_chunks,'removed_chunks':len(removed),'added_or_modified':len(changed),'deleted':len(deleted),
                'changed_paths':[row[0] for row in changed],'deleted_paths':deleted,'embedded_chunks':embedded_count,'cached_vectors':cached_count,'batch_size':batch_size,'duty_cycle':duty_cycle,'throttle_wait_seconds':round(throttle_wait,3),
                'reused_vectors':sum(1 for row in new_rows if (row['chunk_id'],row['vector_cache_key']) in reusable),
                'source_version_before':saved.get('source_version'),'source_version_after':source_version,
                'elapsed_ms':round((time.perf_counter()-started)*1000,1),'model_process':{'pid':os.getpid(),**local_model_status()}}
            _atomic(root/state_relative,{'files':file_state,'source_version':source_version,'last_sync':at,'last_result':result})
            _atomic(pointer,{'version':2,'workspace_id':context.workspace_id,'generation':generation,
                'table':table_name if created is not None else None,
                'table_version':created.version if created is not None else None,
                'previous_table_version':descriptor.get('table_version') if descriptor.get('table')==table_name else None,
                'previous_state_file':descriptor.get('state_file'),
                'state_file':state_relative,'activated_at':at})
            from .search_scopes import publish_evidence_manifest
            publish_evidence_manifest(context,file_state)
            cache.trim_published()
            from .index_retention import after_publish
            after_publish(context,'evidence')
            return result
    except IndexBusyError as exc:return exc.public_status()
    finally:
        if cache:cache.close()

def start_sync(context=None,*,operation='evidence_background_incremental'):
    """Coalesce query-triggered freshness work without delaying retrieval."""
    global _SYNC_THREAD
    context=context or active_workspace()
    with _SYNC_THREAD_LOCK:
        if _SYNC_THREAD and _SYNC_THREAD.is_alive():return {'status':'already_running'}
        def run():sync(None,context,operation=operation,lock_timeout=.15)
        _SYNC_THREAD=threading.Thread(target=run,daemon=True,name='evidence-index-incremental')
        _SYNC_THREAD.start();return {'status':'started'}

def vector_rows(query_vector,limit=80,context=None,source_type=None,folder=None,project=None):
    from .evidence_storage import reader_guard
    context=context or active_workspace();root,_,_=_paths(context)
    with reader_guard(root):
        descriptor=_descriptor(context);active=table(context,descriptor)
        if active is None:return []
        rows=active.search(query_vector).limit(max(100,int(limit)*4)).to_arrow().to_pylist()
        # Hydrate volatile file metadata from the same published snapshot.
        files=json.loads((root/descriptor['state_file']).read_text('utf-8')).get('files',{}) if descriptor.get('version')==2 else {}
        for row in rows:
            if row['source_path'] in files:row['source_version']=files[row['source_path']]['hash']
    result=[]
    for row in rows:
        if source_type and row.get('source_type')!=source_type:continue
        if folder and not row.get('source_path','').startswith(folder.strip('/')+'/'):continue
        distance=float(row.get('_distance',1));item={key:row.get(key) for key in ('source_path','document_title','section_title','chunk_id','content','source_version','session_id','message_id','role','message_order','chunk_index_in_message','source_type')}
        item.update(score=1/(1+distance),search_method='vector');result.append(item)
        if len(result)>=limit:break
    return result

def benchmark(context=None,sample_size=32):
    """Encode a bounded, length-stratified real sample without writing an index."""
    from .core import model,local_model_status
    from .search_scopes import files_for_scope
    context=context or active_workspace();sample_size=max(20,min(50,int(sample_size)));pool=[]
    for path in files_for_scope(context,'evidence'):
        _,rows=rows_for_file(path,context);pool.extend(rows)
    pool.sort(key=lambda row:len(row['embedding_text']))
    if not pool:return {'status':'empty','sample_size':0}
    indices=sorted({round(i*(len(pool)-1)/max(1,sample_size-1)) for i in range(sample_size)})
    sample=[pool[index] for index in indices];texts=[row['embedding_text'] for row in sample]
    encoder=model(local_only=True);tokens=encoder.tokenizer(texts,add_special_tokens=True,truncation=False,
                                                            padding=False,return_length=True)['length']
    started=time.perf_counter();vectors=encoder.encode(texts,normalize_embeddings=True,show_progress_bar=False,batch_size=len(texts));elapsed=time.perf_counter()-started
    lengths=[len(text) for text in texts]
    return {'status':'measured','sample_size':len(texts),'population_chunks':len(pool),
        'sampling':'length_stratified_quantiles','sequence_limit':encoder.max_seq_length,'batch_size':len(texts),
        'characters':{'min':min(lengths),'median':sorted(lengths)[len(lengths)//2],'max':max(lengths)},
        'tokens':{'min':min(tokens),'median':sorted(tokens)[len(tokens)//2],'max':max(tokens),
                  'over_limit':sum(length>encoder.max_seq_length for length in tokens)},
        'elapsed_seconds':round(elapsed,3),'chunks_per_second':round(len(texts)/elapsed,4),
        'estimated_full_hours':round(len(pool)/max(.0001,len(texts)/elapsed)/3600,3),
        'dimensions':int(vectors.shape[1]),'sample_hashes':[row['vector_cache_key'] for row in sample],
        'model_process':{'pid':os.getpid(),**local_model_status()}}
