"""Persistent first-import watermarks and fair live/history source-fragment queues."""
import hashlib,time,uuid,os
from filelock import Timeout
from datetime import datetime,timezone

def fragments(source):return source.fragments or (source,)
def key(source):return hashlib.sha256((source.session_id+'\0'+source.object_id).encode()).hexdigest()[:24]
def clock():return datetime.now(timezone.utc).isoformat()
def cursor_for(state,source,lane='history'):
    row=state['sessions'].setdefault(source.session_id,{'source_session_id':source.session_id})
    streams=row.setdefault('fragments',{});token=key(source)+('::live' if lane=='live' else '')
    if token not in streams:
        # Carry the old single-file cursor only to the exact physical source it described.
        old={k:v for k,v in row.items() if k not in ('fragments', 'fragment_count')}
        matches = old.get('source_object_id') == source.object_id or old.get('source_file') == source.relative_path
        streams[token]=old if lane!='live' and matches else {'source_session_id':source.session_id}
    return streams[token]

def freeze(adapter,state,discovered,ids):
    if state.get('history_batch') is not None:return
    items={}
    for sid in ids:
        if sid not in discovered:continue
        for source in fragments(discovered[sid]):
            items[key(source)]={'session_id':sid,'source_file':source.relative_path,'object_id':source.object_id,'identity':source.meta_fingerprint,'captured_size':source.size,'target_offset':None,'status':'pending'}
    state['history_batch']={'batch_id':uuid.uuid4().hex,'created_at':clock(),'items':items}
    state.setdefault('scheduler',{'turn':0,'attempt':0})
    state.setdefault('live_progress',{'saved_messages':0,'processed_bytes':0})

def boundary(adapter,source,item):
    if item.get('target_offset') is not None:return
    from .codex_large_records import CHUNK, fingerprint
    size = item['captured_size']
    with source.path.open('rb') as handle:
        stat = os.fstat(handle.fileno())
        if stat.st_size < size or f'{stat.st_dev}:{stat.st_ino}' != item['object_id']:
            raise ValueError('来源在冻结批次后缩短或被替换，历史边界未确认')

        def preceding_newline(before):
            while before:
                begin = max(0, before-CHUNK)
                handle.seek(begin); block = handle.read(before-begin)
                at = block.rfind(b'\n')
                if at >= 0: return begin+at
                before = begin
            return -1

        target = preceding_newline(size)+1
        start = preceding_newline(max(0, target-1))+1
        digest = fingerprint(handle, start, target)
        tail_bytes = min(target, CHUNK)
        handle.seek(target-tail_bytes); tail = handle.read(tail_bytes)
    item.update(target_offset=target, last_event_start=start, last_event_fingerprint=digest,
                tail_sha256=hashlib.sha256(tail).hexdigest(), tail_bytes=tail_bytes,
                deferred_partial_bytes=size-target)


def check_boundary(source,item):
    target=item['target_offset']
    if source.meta_fingerprint!=item['identity'] or source.size<target:raise ValueError('历史来源身份或冻结水位已变化，未确认完成')
    with source.path.open('rb') as handle:
        handle.seek(target-item['tail_bytes']);tail=handle.read(item['tail_bytes'])
    if hashlib.sha256(tail).hexdigest()!=item['tail_sha256']:raise ValueError('历史冻结边界内容已变化，需要核对原档')

def history_summary(state, format_review=None):
    batch=state.get('history_batch')
    if not batch:return {'status':'not_started','session_count':0,'fragment_count':0,'completed_fragments':0,'total_bytes':0,'processed_bytes':0}
    verified=set((format_review or {}).get('verified_session_ids', []))
    items=[dict(i, status='complete') if i.get('status')=='needs_review' and i['session_id'] in verified else i
           for i in batch['items'].values()]
    counts={name:sum(i.get('status')==name for i in items) for name in ('complete','needs_review','failed','pending','reading','excluded','source_missing')}
    completed_sessions=sum(all(i.get('status')=='complete' for i in items if i['session_id']==sid) for sid in {i['session_id'] for i in items})
    pending=counts['pending']+counts['reading'];issues=counts['needs_review']+counts['failed']+counts['source_missing']+counts['excluded']
    session_ids = {i['session_id'] for i in items}
    missing_attachments = sum(len(state['sessions'].get(sid, {}).get('missing_attachments', [])) for sid in session_ids)
    failed_attachments = sum(state['sessions'].get(sid, {}).get('attachment_failed_count', 0) for sid in session_ids)
    issues += missing_attachments + failed_attachments
    if state.get('resume_policy') == 'all' or state.get('archive_policy') == 'all':
        issues += len(state.get('discovery_issues', []))
    return {'batch_id':batch['batch_id'],'created_at':batch['created_at'],'status':'paused' if state['archive_policy']=='paused' else 'importing' if pending else 'completed_with_issues' if issues else 'completed','session_count':len({i['session_id'] for i in items}),'fragment_count':len(items),'completed_sessions':completed_sessions,'completed_fragments':counts['complete'],'counts':counts,'total_bytes':sum(i.get('target_offset') if i.get('target_offset') is not None else i['captured_size'] for i in items),'processed_bytes':sum(min(i.get('confirmed_offset',0),i.get('target_offset') or i['captured_size']) for i in items),'deferred_partial_bytes':sum(i.get('deferred_partial_bytes',0) for i in items),'scope':'本批固定源分片与字节水位；不包含之后新增内容','items':[{'session_id':i['session_id'],'source_file':i['source_file'],'status':i['status'],'error':i.get('error')} for i in items]}

def aggregate(state,discovered):
    for sid,row in state['sessions'].items():
        active = {key(s) for s in fragments(discovered[sid])} if sid in discovered else set()
        streams=[c for token,c in row.get('fragments',{}).items() if token.split('::')[0] in active]
        if not streams:continue
        latest=max(streams,key=lambda c:c.get('last_attempt',0))
        row.pop('large_record', None)
        row.update({k:v for k,v in latest.items() if k not in ('fragments','last_attempt')})
        # Message and attachment counts are shared ArchiveStore totals, not sums per source file.
        row['archived_message_count']=max(c.get('archived_message_count',0) for c in streams)
        failures = [c for c in streams if c.get('current_status') == 'failed']
        if failures:
            failure = max(failures, key=lambda c:c.get('last_attempt', 0))
            row.update(current_status='failed', last_error=failure.get('last_error'), failure_stage=failure.get('failure_stage'))
        elif any(c.get('unknown_event_count') for c in streams):row['current_status']='needs_review'
        elif any(c.get('current_status') in (None, 'reading', 'processing') for c in streams):row['current_status']='reading'
        row['fragment_count']=len(fragments(discovered[sid])) if sid in discovered else len(row['fragments'])
        row['body_status']='failed' if row['current_status']=='failed' and row.get('failure_stage')!='attachments' else 'needs_review' if row['current_status']=='needs_review' else 'reading' if row['current_status'] in ('failed','reading','partial_line_pending','processing') else 'complete'
        row['attachment_failed_count']=max(c.get('attachment_failed_count',0) for c in streams)
        row['attachment_status']='failed' if row['attachment_failed_count'] else 'missing' if row.get('missing_attachments') else 'complete'


def is_caught_up(adapter, source, row):
    if source is None or row.get('body_status') != 'complete' or row.get('attachment_status') != 'complete':
        return False
    for fragment in fragments(source):
        streams = row.get('fragments', {})
        cursor = streams.get(key(fragment) + '::live') or streams.get(key(fragment), {})
        if adapter._needs_sync(fragment, cursor):
            return False
    return True


def _tail_cursor(adapter, state, source, item):
    cursor = cursor_for(state, source, 'live')
    if not cursor.get('source_file_identity'):
        cursor.update(source_file_identity=source.meta_fingerprint, source_object_id=source.object_id,
                      source_file=source.relative_path, confirmed_offset=item['target_offset'],
                      last_event_start=item['last_event_start'],
                      last_complete_event_fingerprint=item['last_event_fingerprint'],
                      file_size=item['captured_size'], file_mtime_ns=source.mtime_ns,
                      resolved_title=source.title, title_source=source.title_source,
                      parser_version=adapter.parser_version, visibility_policy_version=2,
                      review_policy_version=2, initial_tail_cursor=True, current_status='synced')
    return cursor


def sync(adapter, manual=False, batch_size=None):
    """One bounded, interleaved tick. All durable cursors share a process lock."""
    if not adapter._lock.acquire(blocking=False):
        return {'status':'busy', **adapter.status()}
    acquired = False
    try:
        try:
            adapter._process_lock.acquire(timeout=0)
            acquired = True
        except Timeout:
            return {'status':'busy', **adapter.status()}
        state = adapter._state()
        if state['archive_policy'] == 'paused':
            return {'status':'paused', 'enabled':False, 'saved_messages':0}
        discovered = adapter.discover()
        state['discovery_issues'] = adapter.discovery_issues
        ids = adapter._effective_ids(state, discovered)
        # An empty selected scope must not consume the one initial-import batch.
        if ids:
            freeze(adapter, state, discovered, ids)
        items = (state.get('history_batch') or {}).get('items', {})
        history, live, known = [], [], set()
        scheduler = state['scheduler']
        now = time.time()
        for sid in ids:
            if sid not in discovered:
                state['sessions'].setdefault(sid, {}).update(current_status='source_missing', last_error='本地未发现来源')
                continue
            for source in fragments(discovered[sid]):
                token = key(source)
                known.add(token)
                item = items.get(token)
                cursor = cursor_for(state, source)
                legacy_policy = state['sessions'][sid].get('visibility_policy_version', 2)
                if legacy_policy < 2:
                    cursor['visibility_policy_version'] = legacy_policy
                    if item:
                        item['status'] = 'pending'
                if item:
                    if item['status'] == 'excluded':
                        item['status'] = 'pending'
                    try:
                        boundary(adapter, source, item)
                        check_boundary(source, item)
                    except (ValueError, OSError) as exc:
                        item.update(status='failed', error=str(exc))
                        # Preserve the failed original batch; new/corrected source
                        # bytes can still be retried without claiming it completed.
                        if manual or now >= cursor.get('retry_after', 0):
                            live.append((cursor.get('last_attempt', 0), source, 'history', None))
                        continue
                    target = item['target_offset']
                    # Format repair has its own resumable verification cursor.
                    # A retry must never rewind the normal historical cursor.
                    needs_history = int(cursor.get('confirmed_offset', 0)) < target or item['status'] not in ('complete', 'needs_review')
                    if needs_history and (manual or now >= cursor.get('retry_after', 0)):
                        history.append((cursor.get('last_attempt', 0), source, 'history', item))
                    tail = _tail_cursor(adapter, state, source, item)
                    if legacy_policy < 2:
                        tail['visibility_policy_version'] = legacy_policy
                    # Missing attachments belong to the logical session, so the
                    # tail queue can retry them independently after body import.
                    tail['missing_attachments'] = state['sessions'][sid].get('missing_attachments', [])
                    if adapter._needs_sync(source, tail) and (manual or now >= tail.get('retry_after', 0)):
                        live.append((tail.get('last_attempt', 0), source, 'live', None))
                elif adapter._needs_sync(source, cursor) and (manual or now >= cursor.get('retry_after', 0)):
                    live.append((cursor.get('last_attempt', 0), source, 'history', None))
        for token, item in items.items():
            if item['session_id'] not in ids:
                item['status'] = 'excluded'
            elif token not in known and item['status'] not in ('complete', 'needs_review'):
                item.update(status='source_missing', error='冻结源分片暂时不在本地；已有档案保留')
        history.sort(key=lambda x:(x[0], x[1].mtime_ns, x[1].relative_path))
        live.sort(key=lambda x:(x[0], -x[1].mtime_ns, x[1].relative_path))
        todo = []
        budget=max(1,min(16,int(batch_size or 8)))
        # A bounded small source append gets priority over attachment-only retries and large backlogs.
        # Keep at least one ordinary slot so historical/backlogged conversations cannot starve.
        urgent=[]
        for entry in live:
            _,source,lane,_=entry;cursor=cursor_for(state,source,lane)
            pending=source.size-int(cursor.get('confirmed_offset') or 0)
            if 0<pending<=64*1024:urgent.append(entry)
        urgent.sort(key=lambda x:(x[0],-x[1].mtime_ns,x[1].relative_path))
        for entry in urgent[:min(3,max(0,budget-1))]:
            todo.append(entry);live.remove(entry)
        for _ in range(budget-len(todo)):
            queue = history if scheduler['turn'] % 2 == 0 else live
            if not queue:
                queue = live if queue is history else history
            if not queue:
                break
            todo.append(queue.pop(0))
            scheduler['turn'] += 1
        adapter._save_state(state)
        results = []
        saved = duplicates = unsupported = 0
        for _, source, lane, item in todo:
            cursor = cursor_for(state, source, lane)
            scheduler['attempt'] += 1
            cursor.update(last_attempt=scheduler['attempt'], current_status='processing', last_error=None)
            adapter._work_cursor = cursor
            adapter._read_target = item['target_offset'] if item else None
            adapter._save_state(state)
            try:
                result = adapter._sync_one(state, source)
                cursor['retry_after'] = time.time() + 60 if cursor.get('missing_attachments') and int(cursor.get('confirmed_offset',0))>=int(cursor.get('file_size',0)) else 0
                if item:
                    reached = int(cursor.get('confirmed_offset', 0)) >= item['target_offset']
                    item.update(confirmed_offset=cursor.get('confirmed_offset', 0),
                                status='needs_review' if reached and cursor.get('unknown_event_count') else 'complete' if reached else 'reading', error=None)
                else:
                    state['live_progress']['saved_messages'] += result['saved_messages']
                    state['live_progress']['processed_bytes'] += max(0, result['confirmed_offset'] - result['previous_offset'])
                saved += result['saved_messages']
                duplicates += result['duplicate_messages']
                unsupported += result['unsupported_events']
                # Attachment totals are session-wide snapshots. Refresh every
                # fragment's view so an old missing marker cannot remain forever.
                for other in state['sessions'][source.session_id]['fragments'].values():
                    other['missing_attachments'] = cursor.get('missing_attachments', [])
                    other['attachment_count'] = cursor.get('attachment_count', 0)
            except Exception as exc:
                from .redaction import redact_text
                cursor.update(current_status='failed', last_error=redact_text(exc), retry_after=time.time()+60)
                if item:
                    item.update(status='failed', error=redact_text(exc))
                result = {'session_id':source.session_id, 'source_file':source.relative_path,
                          'status':'failed', 'changed':True, 'saved_messages':0,
                          'duplicate_messages':0, 'unsupported_events':0, 'error':redact_text(exc)}
            finally:
                adapter._work_cursor = None
                adapter._read_target = None
            aggregate(state, discovered)
            logical = state['sessions'][source.session_id]
            if logical.get('session_key'):
                adapter.store.update_session(logical['session_key'], sync_status=logical['current_status'],
                                             sync_position=logical)
            adapter._save_state(state)
            results.append(result)
        try:
            reviewed=adapter._review_file_deliveries(ids)
            if reviewed:
                logical=state['sessions'].get(reviewed['session_id'],{})
                for target in [logical,*logical.get('fragments',{}).values()]:
                    target.update(attachment_count=reviewed['attachment_count'],missing_attachments=reviewed['missing_attachments'])
            state.pop('file_delivery_review_error',None)
        except Exception as exc:
            from .redaction import redact_text
            state['file_delivery_review_error']=redact_text(exc)
        from .codex_format_repair import tick as reconcile_formats
        format_result = reconcile_formats(adapter, state, discovered, ids)
        saved += format_result.get('saved_messages', 0)
        changed = any(result.get('changed', True) for result in results) or format_result.get('active', False)
        if changed or manual:
            state.update(last_sync_at=clock(), last_error=None)
            if saved:
                state.update(last_sync_message_count=saved, last_message_sync_at=state['last_sync_at'])
        aggregate(state, discovered)
        snapshot = adapter.status(discovered=discovered, _state_override=state)
        state['status_snapshot'] = snapshot
        adapter._save_state(state)
        result = {'status':'synced' if changed or manual else 'unchanged', 'manual':manual,
                  'saved_messages':saved, 'duplicate_messages':duplicates, 'unsupported_events':unsupported,
                  'sessions':results, 'format_repair':format_result,
                  'synced_at':state.get('last_sync_at'), 'adapter_status':snapshot}
        if changed or manual:
            adapter._audit(result)
        return result
    finally:
        if acquired:
            adapter._process_lock.release()
        adapter._lock.release()

