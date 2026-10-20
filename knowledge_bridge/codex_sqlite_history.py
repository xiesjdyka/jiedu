"""Bounded, read-only import of Codex's local thread history projection.

The SQLite projection is an additional source of authored messages.  It never
replaces rollout JSONL, and its cursor is independent of the JSONL cursor.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .archive_store import NormalizedMessage
from .durable_state import StateCorruptError, read_object, write_object
from .redaction import redact_text


VISIBLE_TYPES = {'userMessage', 'agentMessage'}
VISIBLE_PHASES = {None, 'commentary', 'final', 'final_answer'}
REQUIRED_COLUMNS = {'thread_id', 'turn_id', 'item_id', 'rollout_ordinal',
                    'created_at_ms', 'item_json', 'item_type'}
HISTORY_PAGE = 128
LIVE_PAGE = 32
MATCH_WINDOW_MS = 10_000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path):
    connection = sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute('pragma query_only=on')
    return connection


def _tables(db):
    return {row[0] for row in db.execute("select name from sqlite_master where type='table'")}


def probe(adapter):
    """Return usable projections and explicit unsupported/unreadable evidence."""
    result = []; issues = []
    for path in sorted(adapter.codex_home.glob('thread_history_*.sqlite')):
        try:
            with closing(_connect(path)) as db:
                if 'thread_items' not in _tables(db):
                    issues.append({'source': path.name, 'reason': 'thread_items_table_missing'})
                    continue
                columns = {row['name'] for row in db.execute('pragma table_info(thread_items)')}
                missing = REQUIRED_COLUMNS - columns
                if missing:
                    issues.append({'source': path.name, 'reason': 'unsupported_thread_items_schema',
                                   'missing_columns': sorted(missing)})
                    continue
                db.execute('select rowid from thread_items limit 1').fetchone()
            result.append(path)
        except (OSError, sqlite3.Error) as exc:
            issues.append({'source': path.name, 'reason': 'sqlite_unreadable',
                           'error_type': type(exc).__name__})
            continue
    return result, issues


def discover(adapter):
    return probe(adapter)[0]


def _metadata(adapter):
    """Use structural thread metadata to distinguish user chats from agents."""
    result = {}
    for path in sorted(adapter.codex_home.glob('state_*.sqlite')):
        try:
            with closing(_connect(path)) as db:
                if 'threads' not in _tables(db):
                    continue
                columns = {row['name'] for row in db.execute('pragma table_info(threads)')}
                if not {'id', 'thread_source'} <= columns:
                    continue
                names = ['id', 'thread_source'] + [name for name in ('title', 'name') if name in columns]
                for row in db.execute('select ' + ','.join(names) + ' from threads'):
                    result[str(row['id'])] = {
                        'thread_source': row['thread_source'],
                        'title': next((str(row[name]).strip() for name in ('name', 'title')
                                       if name in columns and row[name] and str(row[name]).strip()), None),
                    }
        except (OSError, sqlite3.Error):
            continue
    return result


def _source_disposition(session_id, metadata, jsonl_sessions):
    if session_id in jsonl_sessions:
        return 'allowed'
    kind = (metadata.get(session_id) or {}).get('thread_source')
    if kind in {'user', 'agent_created_thread', 'voice_chat', 'realtime_voice'}:
        return 'allowed'
    if kind == 'subagent':
        return 'subagent'
    return 'identity_unconfirmed'


def _timestamp(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _timestamp_ms(value):
    try:
        return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def _raw_rows(adapter, session_key):
    path = adapter.context.raw_message_store / f'{session_key}.jsonl'
    if not path.is_file():
        return []
    rows = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def duplicate_id(rows, message, *, other_adapter=None):
    """Match a stable ID, or one unique body/role/time pair across sources.

    Plain text equality is insufficient: repeated genuine turns must survive.
    """
    by_id = {row.get('message_id'): row for row in rows}
    exact = by_id.get(message.message_id)
    if exact:
        if exact.get('role') == message.role and exact.get('content_sha256') == hashlib.sha256(message.content.encode()).hexdigest():
            return exact['message_id']
        return None
    # A body/time pair alone can be two genuine identical utterances.  A
    # projected assistant turn supplies additional structural identity; user
    # messages without a common item ID remain separate and reviewable.
    turn_id = (message.provenance or {}).get('source_turn_id')
    if message.role != 'assistant' or not turn_id:
        return None
    digest = hashlib.sha256(message.content.encode()).hexdigest()
    stamp = _timestamp_ms(message.timestamp)
    if stamp is None:
        return None
    matches = [row for row in rows if row.get('role') == message.role and row.get('content_sha256') == digest
               and (other_adapter is None or (row.get('provenance') or {}).get('adapter') == other_adapter)
               and (row.get('provenance') or {}).get('source_turn_id') == turn_id
               and (other_stamp := _timestamp_ms(row.get('timestamp'))) is not None
               and abs(other_stamp - stamp) <= MATCH_WINDOW_MS]
    return matches[0]['message_id'] if len(matches) == 1 else None


def _claim(state, session_id, sqlite_id, jsonl_id, archive_id):
    """A fallback body match may pair each source item only once."""
    claims = state.setdefault('cross_source_claims', {}).setdefault(session_id, [])
    for item in claims:
        if item['sqlite_id'] == sqlite_id or item['jsonl_id'] == jsonl_id:
            return item['archive_id'] if item['sqlite_id'] == sqlite_id and item['jsonl_id'] == jsonl_id else None
    claims.append({'sqlite_id': sqlite_id, 'jsonl_id': jsonl_id, 'archive_id': archive_id})
    return archive_id


def match_jsonl(adapter, session_id, session_key, message):
    """Resolve a JSONL message against earlier SQLite import, if unambiguous."""
    path = adapter.context.sync / 'codex-sqlite-adapter.json'
    if not path.is_file():
        return None
    state_path, state = _load(adapter)
    rows = _raw_rows(adapter, session_key)
    candidate_id = duplicate_id(rows, message, other_adapter='codex_sqlite_readonly')
    if candidate_id is None or candidate_id == message.message_id:
        return candidate_id
    candidate = next(row for row in rows if row['message_id'] == candidate_id)
    sqlite_id = (candidate.get('provenance') or {}).get('source_item_id')
    if not sqlite_id:
        return None
    matched = _claim(state, session_id, sqlite_id, message.message_id, candidate_id)
    if matched:
        _save(state_path, state)
    return matched


def _normalize(adapter, row):
    try:
        item = json.loads(row['item_json'])
    except (TypeError, ValueError):
        return None, 'invalid_item_json'
    if not isinstance(item, dict) or item.get('type') != row['item_type']:
        return None, 'item_type_mismatch'
    if row['item_type'] == 'userMessage':
        parts = []; attachments = []; unknown = []
        content = item.get('content')
        if not isinstance(content, list):
            return None, 'invalid_user_content'
        for index, part in enumerate(content):
            if not isinstance(part, dict):
                unknown.append(type(part).__name__); continue
            kind = part.get('type')
            if kind == 'text':
                parts.append(str(part.get('text') or ''))
            elif kind == 'localImage':
                path = part.get('path')
                if not isinstance(path, str) or not path:
                    unknown.append('localImage_without_path'); continue
                try:
                    attachments.append(adapter._attachment({'type': 'local_image', 'local_path': path}, index))
                except (ValueError, TypeError, OSError):
                    unknown.append('localImage_unreadable_reference')
            else:
                unknown.append(str(kind))
        if adapter._is_internal_user_context(parts) and not attachments:
            return None, 'internal_user_context'
        role = 'user'; text = '\n\n'.join(parts)
    elif row['item_type'] == 'agentMessage':
        phase = item.get('phase')
        if phase not in VISIBLE_PHASES:
            return None, 'nonpublic_agent_phase'
        role = 'assistant'; text = str(item.get('text') or '')
        attachments = adapter._delivery_attachments(text, role, 'final' if phase == 'final_answer' else phase)
    else:
        return None, 'internal_item_type'
    if not text.strip() and not attachments:
        return None, 'unknown_user_content' if role == 'user' and unknown else 'empty_visible_item'
    source_id = str(item.get('id') or row['item_id'])
    if source_id != str(row['item_id']):
        return None, 'item_id_mismatch'
    message = NormalizedMessage(
        message_id=source_id, role=role, content=text,
        timestamp=_timestamp(row['created_at_ms']), attachments=tuple(attachments),
        provenance={'adapter': 'codex_sqlite_readonly', 'source_session_id': str(row['thread_id']),
                    'source_item_id': source_id, 'source_turn_id': str(row['turn_id']),
                    'sqlite_rowid': int(row['rowid']), 'knowledge_eligible': True,
                    'assistant_phase': item.get('phase') if role == 'assistant' else None,
                    'unknown_content_types': unknown if role == 'user' and unknown else []},
        sync_position={'source': 'sqlite', 'rowid': int(row['rowid']),
                       'rollout_ordinal': int(row['rollout_ordinal'])},
    )
    return message, ('unknown_user_content:' + ','.join(unknown[:5])) if role == 'user' and unknown else None


def _initial_state(path):
    return {'source_file': path.name, 'history': None, 'live': {'cursor': 0, 'checked': 0,
            'archived': 0, 'duplicates': 0, 'excluded': 0},
            'recheck': {'cursor': 0, 'checked': 0, 'archived': 0, 'duplicates': 0,
                        'excluded': 0, 'review': 0, 'passes': 0},
            'generation': 0, 'file_identity': None, 'last_error': None}


def _load(adapter):
    path = adapter.context.sync / 'codex-sqlite-adapter.json'
    state = read_object(path)
    if path.exists() and not isinstance(state.get('sources'), dict):
        raise StateCorruptError('Codex SQLite 归档进度损坏；保留原文件，停止推进游标')
    return path, {'format': 'codex-sqlite-progress-v1', 'sources': state.get('sources', {}),
                  'cross_source_claims': state.get('cross_source_claims', {}),
                  'observations': state.get('observations', {}),
                  'last_sync_at': state.get('last_sync_at'), 'last_error': state.get('last_error')}


def _save(path, state):
    write_object(path, state, backup=True)


def _process_page(adapter, db, source, lane, state, metadata, jsonl_sessions, limit, *, rows_override=None):
    progress = source[lane]
    upper = progress.get('target_rowid') if lane in {'history', 'recheck'} else None
    sql = ("select rowid,thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type "
           "from thread_items where rowid>? and item_type in ('userMessage','agentMessage')")
    args = [int(progress['cursor'])]
    if upper is not None:
        sql += ' and rowid<=?'; args.append(int(upper))
    sql += ' order by rowid limit ?'; args.append(limit)
    rows = rows_override if rows_override is not None else db.execute(sql, args).fetchall()
    observations = state['sqlite_state'].setdefault('observations', {})
    for row in rows:
        sid = str(row['thread_id'])
        disposition = _source_disposition(sid, metadata, jsonl_sessions)
        if disposition != 'allowed':
            if rows_override is None: progress['excluded'] += 1
            if disposition == 'identity_unconfirmed':
                reviewed=source.setdefault('unconfirmed_thread_ids', [])
                if sid not in reviewed:
                    reviewed.append(sid)
                    source.setdefault('identity_review_lanes', {})[sid] = lane
                    progress['review'] = progress.get('review', 0) + 1
                    _review_item(source, row, 'source_identity_unconfirmed')
        elif sid in state['excluded_session_ids'] or (state['scope'] == 'selected' and sid not in state['selected_session_ids']):
            if rows_override is None: progress['excluded'] += 1
        else:
            observation_key = source['source_file'] + '\0' + sid + '\0' + str(row['item_id'])
            fingerprint = hashlib.sha256(row['item_json'].encode('utf-8')).hexdigest()
            observed = observations.get(observation_key)
            if observed and observed.get('fingerprint') == fingerprint and not observed.get('has_attachments'):
                if rows_override is None:
                    progress['checked'] += 1; progress['cursor'] = int(row['rowid'])
                continue
            message, reason = _normalize(adapter, row)
            if message is None:
                if not observed or observed.get('fingerprint') != fingerprint:
                    progress['excluded'] += 1
                if reason not in {'internal_user_context', 'empty_visible_item', 'nonpublic_agent_phase'} and (
                        not observed or observed.get('fingerprint') != fingerprint):
                    progress['review'] = progress.get('review', 0) + 1
                    _review_item(source, row, reason)
                observations[observation_key] = {'fingerprint': fingerprint, 'status': reason,
                                                  'rowid': int(row['rowid'])}
            else:
                if reason and (not observed or observed.get('fingerprint') != fingerprint):
                    progress['review'] = progress.get('review', 0) + 1
                    _review_item(source, row, reason)
                info = metadata.get(sid) or {}
                title = (jsonl_sessions[sid].title if sid in jsonl_sessions else info.get('title') or f'Codex 会话 {sid[:8]}')
                session = adapter.store.ensure_session(
                    source_key='codex', source_session_id=sid, title=title,
                    source_instance=adapter._instance_id(), acquisition_method='codex_local_sqlite_readonly')
                from dataclasses import replace
                message = replace(message, provenance={**(message.provenance or {}),
                    'source_file': source['source_file'], 'source_generation': source.get('generation', 1)},
                    sync_position={**(message.sync_position or {}),
                        'source_fragment': 'sqlite:' + source['source_file'],
                        'source_fragment_order': source['source_file'], 'event_timestamp': message.timestamp,
                        'byte_offset': int(row['rowid'])})
                prior = None
                if observed and observed.get('archive_message_id') and observed.get('fingerprint') == fingerprint:
                    effective_id = observed['archive_message_id']
                elif observed and observed.get('archive_message_id'):
                    effective_id = message.message_id + '~revision-' + fingerprint[:20]
                    message = replace(message, provenance={**message.provenance,
                        'supersedes_message_id': observed['archive_message_id']})
                else:
                    prior = _raw_rows(adapter, session['session_key'])
                    matched = duplicate_id(prior, message, other_adapter='codex_source_adapter')
                    if matched and matched != message.message_id:
                        matched = _claim(state['sqlite_state'], sid, message.message_id, matched, matched)
                    effective_id = matched or message.message_id
                    conflicting = next((x for x in prior if x.get('message_id') == effective_id and
                        (x.get('role') != message.role or x.get('content_sha256') !=
                         hashlib.sha256(message.content.encode()).hexdigest())), None)
                    if conflicting:
                        effective_id = message.message_id + '~revision-' + fingerprint[:20]
                        message = replace(message, provenance={**message.provenance,
                            'supersedes_message_id': conflicting['message_id']})
                message = replace(message, message_id=effective_id)
                saved = adapter.store.append_normalized_message(session['session_key'],
                            replace(message, attachments=()), materialize=False)
                attachment_rows = adapter.store.reconcile_attachments(session['session_key'], effective_id,
                                                                      message.attachments, materialize=False)
                if saved['saved'] or attachment_rows:
                    adapter.store.materialize_session(session['session_key'])
                if saved['saved']:
                    progress['archived'] += 1
                    state['tick_saved'][0] += 1
                elif not observed or observed.get('fingerprint') != fingerprint:
                    progress['duplicates'] += 1
                observations[observation_key] = {'fingerprint': fingerprint,
                    'archive_message_id': effective_id, 'session_key': session['session_key'],
                    'has_attachments': bool(message.attachments), 'rowid': int(row['rowid'])}
                _clear_identity_review(source, sid)
        # Never acknowledge the next source record before its body/attachments are durable.
        if rows_override is None:
            progress['checked'] += 1
            progress['cursor'] = int(row['rowid'])
    return len(rows)


def _review_item(source, row, reason):
    items = source.setdefault('review_items', [])
    if len(items) < 200:
        items.append({'thread_id': str(row['thread_id']), 'item_id': str(row['item_id']),
                      'rowid': int(row['rowid']), 'reason': reason})
    else:
        source['review_overflow'] = source.get('review_overflow', 0) + 1


def _clear_identity_review(source, session_id):
    pending = source.get('unconfirmed_thread_ids', [])
    if session_id not in pending:
        return
    pending.remove(session_id)
    lane = source.get('identity_review_lanes', {}).pop(session_id, 'history')
    progress = source.get(lane) or {}
    progress['review'] = max(0, int(progress.get('review') or 0) - 1)
    source['review_items'] = [item for item in source.get('review_items', [])
                              if not (item.get('thread_id') == session_id and
                                      item.get('reason') == 'source_identity_unconfirmed')]
    history = source.get('history') or {}
    if history.get('status') == 'completed_with_review' and not history.get('review'):
        history['status'] = 'complete'


def sync(adapter, policy_state, jsonl_sessions):
    """Process one bounded history page and one live page per detected database."""
    path, state = _load(adapter)
    files, issues = probe(adapter)
    if not files:
        return summary(adapter, state=state, files=[], issues=issues)
    tick_saved = [0]
    metadata = _metadata(adapter)
    for db_path in files:
        source = state['sources'].setdefault(db_path.name, _initial_state(db_path))
        source['source_file'] = db_path.name
        source['excluded_session_ids'] = policy_state['excluded_session_ids']
        source['selected_session_ids'] = policy_state['selected_session_ids']
        source['scope'] = policy_state['archive_policy']
        try:
            with closing(_connect(db_path)) as db:
                max_rowid = int(db.execute('select coalesce(max(rowid),0) from thread_items').fetchone()[0])
                stat = db_path.stat()
                identity = f'{stat.st_dev}:{stat.st_ino}'
                old = source.get('history') or {}
                replaced = (source.get('file_identity') not in {None, identity} or
                            max_rowid < max(int(old.get('cursor') or 0),
                                            int((source.get('live') or {}).get('cursor') or 0)))
                if replaced:
                    source.setdefault('previous_generations', []).append({
                        'generation': source.get('generation', 1), 'closed_at': _now(),
                        'history_checked': old.get('checked', 0),
                        'history_archived': old.get('archived', 0),
                        'reason': 'source_replaced_or_rowid_receded'})
                    source['history'] = None
                    source['live'] = {'cursor': 0, 'checked': 0, 'archived': 0,
                                      'duplicates': 0, 'excluded': 0}
                    source['recheck'] = {'cursor': 0, 'checked': 0, 'archived': 0,
                                         'duplicates': 0, 'excluded': 0, 'review': 0, 'passes': 0}
                source['file_identity'] = identity
                if source['history'] is None:
                    source['generation'] = int(source.get('generation') or 0) + 1
                    known = int(db.execute("select count(*) from thread_items where rowid<=? and item_type in ('userMessage','agentMessage')", (max_rowid,)).fetchone()[0])
                    source['history'] = {'status': 'reading', 'cursor': 0, 'target_rowid': max_rowid,
                                         'known_total': known, 'unit': 'records', 'checked': 0,
                                         'archived': 0, 'duplicates': 0, 'excluded': 0, 'review': 0,
                                         'started_at': _now()}
                    source['live']['cursor'] = max_rowid
                    _save(path, state)
                source.setdefault('recheck', {'cursor': 0, 'checked': 0, 'archived': 0,
                    'duplicates': 0, 'excluded': 0, 'review': 0, 'passes': 0})
                state_wrapper = {'sqlite_state': state, 'scope': source['scope'],
                                 'selected_session_ids': source['selected_session_ids'],
                                 'excluded_session_ids': source['excluded_session_ids'],
                                 'tick_saved': tick_saved}
                if source['history']['status'] != 'complete':
                    count = _process_page(adapter, db, source, 'history', state_wrapper, metadata,
                                          jsonl_sessions, HISTORY_PAGE)
                    if count == 0:
                        source['history']['status'] = 'completed_with_review' if source['history'].get('review') else 'complete'
                _process_page(adapter, db, source, 'live', state_wrapper, metadata,
                              jsonl_sessions, LIVE_PAGE)
                # Revisit recently projected items even while older history is
                # still loading; the source may update an item in place.
                tail = db.execute("select rowid,thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type "
                    "from thread_items where item_type in ('userMessage','agentMessage') "
                    "order by rowid desc limit 16").fetchall()
                _process_page(adapter, db, source, 'recheck', state_wrapper, metadata,
                              jsonl_sessions, 16, rows_override=list(reversed(tail)))
                if source['history']['status'] in {'complete', 'completed_with_review'}:
                    source['recheck']['target_rowid'] = max_rowid
                    if not _process_page(adapter, db, source, 'recheck', state_wrapper, metadata,
                                         jsonl_sessions, 64):
                        source['recheck']['cursor'] = 0
                        source['recheck']['passes'] += 1
                source['last_error'] = None
        except (OSError, sqlite3.Error, ValueError, StateCorruptError) as exc:
            source['last_error'] = redact_text(exc)
            source['failure_count'] = source.get('failure_count', 0) + 1
        _save(path, state)
    state['last_sync_at'] = _now()
    _save(path, state)
    result = summary(adapter, state=state, files=files, issues=issues)
    result['saved_messages'] = tick_saved[0]
    return result


def summary(adapter, *, state=None, files=None, issues=None):
    if state is None:
        _, state = _load(adapter)
    if files is None:
        files, issues = probe(adapter)
    if issues is None:
        issues = []
    entries = []
    for path in files:
        source = state['sources'].get(path.name) or _initial_state(path)
        history = source.get('history') or {}
        live = source.get('live') or {}
        recheck = source.get('recheck') or {}
        entries.append({'source': path.name, 'detected': True, 'readable': True,
                        'history': {key: history.get(key) for key in ('status','unit','known_total','checked','archived','duplicates','excluded','review')},
                        'live': {key: live.get(key) for key in ('checked','archived','duplicates','excluded')},
                        'recheck': {key: recheck.get(key) for key in ('checked','archived','passes','review')},
                        'needs_review_items': len(source.get('review_items', [])) +
                                              int(source.get('review_overflow') or 0),
                        'needs_review_unit': 'items',
                        'generation': source.get('generation', 0),
                        'last_error': source.get('last_error'), 'failure_count': source.get('failure_count', 0)})
    return {'adapter': 'codex_sqlite', 'detected': bool(files), 'sources': entries,
            'issues': issues,
            'last_sync_at': state.get('last_sync_at'), 'total_is_dynamic': True}
