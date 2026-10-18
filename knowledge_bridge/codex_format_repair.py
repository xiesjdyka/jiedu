"""Resumable format reconciliation without rewinding the normal sync cursors.

The independent pass fixes public-message omissions, checks existing messages,
and records exact source fingerprints for resolved review entries. Original
review entries and source logs are retained. No log payload is archived as chat.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import time
import threading
from collections import OrderedDict

from .codex_event_formats import FORMAT_POLICY
from .durable_state import read_object, write_object

_summary_cache = OrderedDict()
_summary_cache_lock = threading.Lock()


def _summary_signature(adapter, ids):
    files = (*paths(adapter), adapter.review_path)
    stamps = []
    for path in files:
        try:
            st = path.stat()
            stamps.append((str(path.resolve()), st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            stamps.append((str(path.resolve()), None))
    return (FORMAT_POLICY, tuple(sorted(ids)), tuple(stamps))


def paths(adapter):
    return (adapter.context.sync / 'codex-format-repair.json',
            adapter.context.audit / 'codex-format-resolutions.jsonl')


def review_key(row):
    return str(row.get('source_session_id')) + ':' + str(row.get('event_fingerprint'))


def rows(path):
    if not path.exists():
        return []
    result = []
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                break  # An interrupted final append is not a committed entry.
            if line.strip():
                result.append(json.loads(line))
    return result


def resolutions(adapter):
    return {r['key']: r for r in rows(paths(adapter)[1]) if r.get('policy') in {3, FORMAT_POLICY}}


def review_fingerprints(event, digest):
    """Match old bytes before the desktop added ordinal; require exact hash."""
    candidates = [digest]
    if isinstance(event, dict) and isinstance(event.get('ordinal'), int):
        original = {k: v for k, v in event.items() if k != 'ordinal'}
        for ascii_text in (True, False):
            candidates.append(hashlib.sha256(json.dumps(original, ensure_ascii=ascii_text,
                separators=(',', ':')).encode()).hexdigest())
    return list(dict.fromkeys(candidates))


def append_resolutions(adapter, entries):
    if not entries:
        return
    path = paths(adapter)[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as stream:
        for entry in entries:
            stream.write((json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())


def tick(adapter, state, discovered, ids):
    """Called under the existing Codex process lock; at most one source page."""
    from .codex_history import boundary, check_boundary, fragments, key, clock
    from .codex_source_adapter import _fingerprint
    from .codex_large_records import StreamedInternal

    path, _ = paths(adapter)
    if not path.exists() and not adapter.review_path.exists():
        return {'saved_messages': 0, 'active': False}
    audit = read_object(path) if path.exists() else {'policy': FORMAT_POLICY, 'created_at': clock(), 'sources': {}}
    if audit.get('policy') == 3:
        # Only the independent verification pass restarts. Live/history cursors,
        # source identity, captured boundaries and original receipts are retained.
        audit.setdefault('previous_passes', []).append({'policy': 3, 'sources': deepcopy(audit['sources']), 'finished_at': clock()})
        for item in audit['sources'].values():
            item.update(confirmed_offset=0, counts={}, status='pending')
            for name in ('last_start', 'last_end', 'last_fingerprint', 'large_record', 'checked_at', 'error'):
                item.pop(name, None)
        audit['policy'] = FORMAT_POLICY
    if audit.get('policy') != FORMAT_POLICY or not isinstance(audit.get('sources'), dict):
        raise ValueError('旧格式核对进度不兼容，已保留原记录等待恢复')
    sources = {key(source): source for sid in ids if sid in discovered for source in fragments(discovered[sid])}
    for token, source in sources.items():
        audit['sources'].setdefault(token, {
            'session_id': source.session_id, 'source_file': source.relative_path,
            'object_id': source.object_id, 'identity': source.meta_fingerprint,
            'captured_size': source.size, 'target_offset': None, 'confirmed_offset': 0,
            'counts': {}, 'saved_messages': 0, 'status': 'pending',
        })
    pending = [(token, item) for token, item in audit['sources'].items()
               if token in sources and item['status'] not in {'complete', 'complete_with_issues'}]
    if not pending:
        # Persist additions even if the selected scope happens to be empty.
        if not path.exists():
            write_object(path, audit, backup=True)
        return {'saved_messages': 0, 'active': False}
    token, item = min(pending, key=lambda pair: (pair[1].get('attempt', 0), pair[1]['source_file']))
    source = sources[token]
    audit['attempt'] = audit.get('attempt', 0) + 1
    item.update(attempt=audit['attempt'], status='reading', error=None)
    review = {review_key(r): r for r in rows(adapter.review_path) if r.get('source_session_id') == source.session_id}
    resolved = resolutions(adapter)
    committed = []
    saved = 0
    session = None
    page_start = deepcopy(item)
    previous_cursor, previous_target = adapter._work_cursor, adapter._read_target
    adapter._work_cursor, adapter._read_target = item, item.get('target_offset')
    try:
        boundary(adapter, source, item)
        check_boundary(source, item)
        # Check the last consumed boundary before resuming the independent pass.
        if item.get('last_end'):
            with source.path.open('rb') as stream:
                from .codex_large_records import fingerprint
                if fingerprint(stream, item['last_start'], item['last_end']) != item['last_fingerprint']:
                    raise ValueError('核对续读边界已变化；保留已归档内容和所有游标')
        complete, partial = adapter._complete_region(source.path, item['confirmed_offset'], item['target_offset'], source)
        session = adapter.store.ensure_session(source_key='codex', source_session_id=source.session_id,
            title=source.title, source_instance=adapter._instance_id(), acquisition_method='codex_local_jsonl_readonly')
        existing = {r['message_id']: r for r in adapter.store._message_rows(adapter.context.raw_message_store / (session['session_key'] + '.jsonl'))}
        existing_positions = adapter.store._position_map(session)
        began = time.monotonic()
        for number, (raw, start, end) in enumerate(complete):
            if number and time.monotonic() - began >= adapter.processing_budget_seconds:
                break
            if isinstance(raw, StreamedInternal):
                digest = raw.fingerprint
                parsed = {'status': 'ignored', 'category': 'validated_large_internal'}
                event = None
            else:
                digest = _fingerprint(raw)
                try:
                    event = json.loads(raw)
                    parsed = adapter._normalize_event(event, source.session_id)
                except (ValueError, TypeError) as exc:
                    event = None
                    parsed = {'status': 'needs_review', 'reason': 'invalid_json', 'metadata': {}}
            status = parsed['status']
            category = parsed.get('category', status)
            if status == 'message':
                message = parsed['message']
                old = existing.get(message.message_id)
                if old and (old.get('role') != message.role or old.get('original_text') != message.content):
                    raise ValueError('同一来源消息编号与已存正文冲突，未覆盖原文')
                position = {'source_file': source.relative_path, 'source_file_identity': source.meta_fingerprint,
                    'source_object_id': source.object_id, 'byte_offset': start, 'byte_end': end,
                    'source_fragment': token, 'source_fragment_order': source.relative_path, 'event_timestamp': message.timestamp}
                if not old:
                    item['projection_pending'] = True
                    result = adapter.store.append_normalized_message(session['session_key'],
                        replace(message, attachments=(), sync_position=position), materialize=False)
                    saved += int(result['saved'])
                    existing[message.message_id] = result['record']
                    adapter._remember_attachment_locators(source.session_id, message)
                    adapter._append_public_evidence(session, event, message)
                if message.message_id not in existing_positions:
                    item['projection_pending'] = True
                    adapter.store.record_message_position(session['session_key'], message.message_id, position)
                    existing_positions[message.message_id] = position
                if message.attachments:
                    item['projection_pending'] = True
                    adapter.store.reconcile_attachments(session['session_key'], message.message_id, message.attachments, materialize=False)
            elif status == 'needs_review':
                adapter._review(source.session_id, digest, parsed['reason'], start, parsed.get('metadata', {}))
                category = 'unresolved'
            for original_digest in review_fingerprints(event, digest):
                rk = source.session_id + ':' + original_digest
                if rk in review and status in {'message', 'ignored'} and rk not in resolved:
                    receipt = {'key': rk, 'policy': FORMAT_POLICY, 'source_session_id': source.session_id,
                        'event_fingerprint': original_digest, 'current_event_fingerprint': digest,
                        'match_method': 'exact_bytes' if original_digest == digest else 'before_ordinal_field',
                        'source_file': source.relative_path, 'byte_offset': start,
                        'byte_end': end, 'classification': 'public_message' if status == 'message' else category,
                        'resolved_at': clock()}
                    committed.append(receipt)
                    resolved[rk] = receipt
            item['counts'][category] = item['counts'].get(category, 0) + 1
            item.update(confirmed_offset=end, last_start=start, last_end=end, last_fingerprint=digest)
            if item.get('large_record', {}).get('scan_offset', source.size + 1) <= end:
                item.pop('large_record', None)
        # Materialization precedes any completion receipt: a retry may dedupe
        # appended bodies, but must still rebuild the chronological reader view.
        if item.get('projection_pending'):
            adapter.store.materialize_session(session['session_key'])
            item['projection_pending'] = False
        if item['confirmed_offset'] >= item['target_offset']:
            item['status'] = 'complete_with_issues' if item['counts'].get('unresolved') else 'complete'
            item['checked_at'] = clock()
        append_resolutions(adapter, committed)
    except Exception as exc:
        from .redaction import redact_text
        # Retry this independent page after projection/attachment failure;
        # appended bodies dedupe, and the normal sync cursor is never touched.
        for field in ('confirmed_offset', 'counts', 'last_start', 'last_end', 'last_fingerprint'):
            if field in page_start:
                item[field] = page_start[field]
            else:
                item.pop(field, None)
        item.update(status='failed', error=redact_text(exc))
    finally:
        item['saved_messages'] += saved
        adapter._work_cursor, adapter._read_target = previous_cursor, previous_target
        if session:
            current = adapter.store.session(session['session_key'])
            logical = state['sessions'].get(source.session_id, {})
            for cursor in [logical, *logical.get('fragments', {}).values()]:
                cursor['archived_message_count'] = int(current.get('message_count') or 0)
            # Preserve the production cursor in the session manifest.
            adapter.store.update_session(session['session_key'], sync_position=logical)
        audit['updated_at'] = clock()
        write_object(path, audit, backup=True)
    return {'saved_messages': saved, 'active': True, 'source_file': source.relative_path,
            'status': item['status'], 'error': item.get('error')}


def summary(adapter, ids):
    # A status refresh must not reparse unchanged multi-megabyte audit ledgers.
    # Cache only the small computed result, never all historical rows.
    signature = _summary_signature(adapter, ids)
    with _summary_cache_lock:
        if signature in _summary_cache:
            _summary_cache.move_to_end(signature)
            return deepcopy(_summary_cache[signature])
    result = _compute_summary(adapter, ids)
    if signature == _summary_signature(adapter, ids):
        with _summary_cache_lock:
            _summary_cache[signature] = deepcopy(result)
            while len(_summary_cache) > 4:
                _summary_cache.popitem(last=False)
    return result


def _compute_summary(adapter, ids):
    path, _ = paths(adapter)
    audit = read_object(path) if path.exists() else {}
    sources = [x for x in audit.get('sources', {}).values() if x['session_id'] in ids]
    if audit and audit.get('policy') != FORMAT_POLICY:
        sources = [dict(x, status='pending', confirmed_offset=0) for x in sources]
    resolved = resolutions(adapter)
    review = [r for r in rows(adapter.review_path) if r.get('source_session_id') in ids]
    remaining = [r for r in review if review_key(r) not in resolved]
    unresolved_by_session = Counter(r['source_session_id'] for r in remaining)
    verified = [sid for sid in ids if any(x['session_id'] == sid for x in sources)
                and all(x['status'] == 'complete' for x in sources if x['session_id'] == sid)
                and not unresolved_by_session[sid]]
    categories = Counter(resolved[review_key(r)]['classification'] for r in review if review_key(r) in resolved)
    return {'policy': FORMAT_POLICY, 'started': bool(audit), 'sources': len(sources),
        'completed_sources': sum(x['status'] == 'complete' for x in sources),
        'verified_session_ids': verified, 'pending_sources': sum(x['status'] in {'pending', 'reading'} for x in sources),
        'failed_sources': sum(x['status'] == 'failed' for x in sources),
        'checked_bytes': sum(x.get('confirmed_offset', 0) for x in sources),
        'target_bytes': sum(x.get('target_offset') or x.get('captured_size', 0) for x in sources),
        'repaired_messages': sum(x.get('saved_messages', 0) for x in sources),
        'unresolved_events': len(remaining), 'unresolved_by_session': dict(unresolved_by_session),
        'resolved_categories': dict(categories), 'resolved_events': len(review) - len(remaining),
        'remaining_categories': dict(Counter(r['reason'] for r in remaining)),
        'errors': [{'source_file': x['source_file'], 'error': x.get('error')} for x in sources if x['status'] == 'failed'],
        'scope': '已发现并获准的本地来源，按冻结字节边界核对正文；之后新增消息继续自动采集'}
