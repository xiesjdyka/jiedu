"""Owner-facing, read-only discovery and truthful collection progress."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat()


def _recent(value, seconds=120):
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(str(value).replace('Z','+00:00'))).total_seconds()
        return 0 <= age <= seconds
    except (ValueError,TypeError,OverflowError):
        return False


def _readable_sqlite(path, required):
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True, timeout=1)) as db:
            names = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
            return required <= names
    except (OSError, sqlite3.Error):
        return False


def _codex_locations(adapter):
    jsonl_files = [next((adapter.codex_home / name).rglob('rollout-*.jsonl'), None)
                   for name in ('sessions', 'archived_sessions') if (adapter.codex_home / name).is_dir()]
    jsonl = any(path and path.is_file() for path in jsonl_files)
    jsonl_readable = False
    for path in jsonl_files:
        if path:
            try:
                with path.open('rb') as handle:handle.read(1)
                jsonl_readable = True;break
            except OSError:pass
    sqlite = list(adapter.codex_home.glob('thread_history_*.sqlite'))
    from .codex_sqlite_history import discover as discover_sqlite
    usable = set(discover_sqlite(adapter))
    return [
        {'kind': 'jsonl', 'root': str(adapter.codex_home), 'path': str(adapter.codex_home),
         'installed': adapter.codex_home.is_dir(), 'detected': jsonl, 'readable': jsonl_readable},
        {'kind': 'sqlite', 'root': str(adapter.codex_home), 'path': str(adapter.codex_home),
         'installed': adapter.codex_home.is_dir(), 'detected': bool(sqlite), 'readable': bool(usable)},
    ]


def status():
    from .codex_source_adapter import CodexSourceAdapter, status_summary
    from .codex_sqlite_history import summary as sqlite_summary
    from .hermes_session_adapter import db_candidates, selected_db_path, summary_status as hermes_status
    from .doubao_indexeddb_source import default_cache_paths
    from .doubao_session_adapter import status as doubao_status
    from .dsh_source_adapter import DSHSourceAdapter
    from .claude_source_adapter import ClaudeSourceAdapter
    from .cursor_source_adapter import CursorSourceAdapter
    from .four_local_collectors import LocalCompletedCollector, SOURCES

    adapter = CodexSourceAdapter(status_only=True)
    codex = status_summary(adapter.context)
    sqlite = sqlite_summary(adapter)
    locations = _codex_locations(adapter)
    history = codex.get('history') or {}
    counts = history.get('counts') or {}
    codex_jsonl = {
        'source': 'jsonl', 'unit': 'files', 'status': history.get('status') or 'not_started',
        'discovered': codex.get('discovered_fragment_count') or 0,
        'readable': codex.get('discovered_fragment_count') or 0,
        'known_total': history.get('fragment_count') or None,
        'checked': history.get('completed_fragments') or 0,
        'archived': (history.get('archived_messages') or 0) +
                    ((codex.get('live') or {}).get('saved_messages') or 0),
        'archived_unit': 'messages',
        'excluded': counts.get('excluded', 0),
        'failed_missing': counts.get('failed', 0) + counts.get('source_missing', 0),
        'needs_review': counts.get('needs_review', 0),
        'total_is_dynamic': True,
    }
    codex_sqlite = []
    for item in sqlite['sources']:
        row = item.get('history') or {}
        codex_sqlite.append({
            'source': item['source'], 'unit': 'records', 'status': row.get('status') or 'not_started',
            'discovered': row.get('known_total'), 'readable': row.get('known_total'),
            'known_total': row.get('known_total'), 'checked': row.get('checked') or 0,
            'archived': (row.get('archived') or 0) + ((item.get('recheck') or {}).get('archived') or 0) +
                        ((item.get('live') or {}).get('archived') or 0),
            'archived_unit': 'messages', 'excluded': row.get('excluded') or 0,
            'failed_missing': None if item.get('last_error') else 0,
            'source_error_count': 1 if item.get('last_error') else 0,
            'needs_review': item.get('needs_review_items') or 0,
            'needs_review_unit': 'items',
            'total_is_dynamic': True, 'last_error': item.get('last_error'),
        })
    codex_source = {
        'id': 'codex', 'installed': adapter.codex_home.is_dir(),
        'detected': any(x['detected'] for x in locations),
        'readable': any(x['readable'] for x in locations),
        'configured': bool(codex.get('enabled')), 'locations': locations,
        'participating': bool(codex.get('enabled') or history.get('status') not in (None, 'not_started') or
                              any((item.get('history') or {}).get('status') not in (None, 'not_started')
                                  for item in sqlite['sources'])),
        'paused': codex.get('archive_policy') == 'paused',
        'review_events': codex.get('needs_review_event_count') or 0,
        'history': {'status': history.get('status') or 'not_started', 'by_source': [codex_jsonl, *codex_sqlite],
                    'unit': 'records', 'known_total': None, 'total_is_dynamic': True},
        'live': {'status': 'paused' if not codex.get('enabled') else
                 'watching' if _recent(codex.get('last_sync_at')) else
                 'stale' if codex.get('last_sync_at') else 'waiting',
                 'last_message_at': codex.get('last_message_sync_at'),
                 'saved_messages': (codex.get('live') or {}).get('saved_messages', 0) +
                                   sum((item.get('live') or {}).get('archived') or 0 for item in sqlite['sources'])},
        'source_issues': sqlite.get('issues', []),
        'last_error': codex.get('last_error') or sqlite.get('last_error') or
                      ('本机 SQLite 来源格式或读取状态需复核' if sqlite.get('issues') else None),
    }

    hermes = hermes_status()
    hermes_path = selected_db_path()
    hermes_readable = _readable_sqlite(hermes_path, {'sessions', 'messages'})
    worker_path = hermes.get('db_path')
    worker_matches = not worker_path or Path(worker_path).resolve() == hermes_path.resolve()
    hermes_locations=[]
    for candidate in db_candidates():
        readable=_readable_sqlite(candidate, {'sessions', 'messages'})
        hermes_locations.append({'kind':'sqlite','root':str(candidate.parent),'path':str(candidate),
            'installed':candidate.parent.is_dir(), 'detected':candidate.is_file(),
            'readable':readable,'selected':candidate.resolve()==hermes_path.resolve()})
    hh = hermes.get('history') or {}
    hermes_source = {
        'id': 'hermes', 'installed': any(x['installed'] for x in hermes_locations),
        'detected': hermes_path.is_file(), 'readable': hermes_readable,
        'configured': bool(hermes_readable and worker_matches and
                           hermes.get('status') in {'watching','running'} and hermes.get('archive_enabled')),
        'participating': bool(hermes.get('db_path') or hh.get('status') not in (None, 'not_started') or
                              hh.get('processed_rows') or hh.get('accepted_rows')),
        'paused': hermes.get('status') == 'paused',
        'locations': hermes_locations,
        'history': {'status': hh.get('status') or 'not_started', 'unit': 'records',
                    'known_total': hh.get('source_rows_at_start'), 'checked': hh.get('processed_rows') or 0,
                    'archived': hh.get('accepted_rows') or 0, 'archived_unit': 'messages',
                    'excluded': (hh.get('excluded_rows') or 0) + (hh.get('ignored_rows') or 0) +
                                (hh.get('orphan_rows') or 0),
                    'failed_missing': hh.get('missing_rows') or 0, 'total_is_dynamic': True},
        'live': {'status': 'stopped' if hermes.get('worker_alive') is False else
                 'stale' if hermes.get('status') in {'watching','running'} and
                            not _recent(hermes.get('last_sync')) else hermes.get('status'),
                 'last_message_at': hermes.get('last_live_sync'),
                 'saved_messages': hermes.get('synced_messages') or 0},
        'last_error': hermes.get('last_error') or ('采集器数据库与当前探测路径不一致' if not worker_matches else None),
    }

    leveldb, blob = default_cache_paths()
    doubao_detected = leveldb.is_dir()
    try:
        doubao = doubao_status()
    except Exception as exc:
        doubao = {'last_error': type(exc).__name__}
    doubao_source = {
        'id': 'doubao', 'installed': leveldb.parent.is_dir(),
        'detected': doubao_detected, 'readable': (leveldb/'CURRENT').is_file(),
        'configured': bool(doubao_detected and doubao.get('enabled')), 'locations': [
            {'kind': 'indexeddb', 'root': str(leveldb.parent), 'path': str(leveldb),
             'installed': leveldb.parent.is_dir(), 'detected': doubao_detected,
             'readable': (leveldb/'CURRENT').is_file()},
            {'kind': 'blobs', 'root': str(blob.parent), 'path': str(blob),
             'installed': blob.parent.is_dir(), 'detected': blob.is_dir(),
             'readable': blob.is_dir()}],
        'history': {'status': 'unknown', 'unit': 'records', 'known_total': None,
                    'checked': None, 'archived': None, 'archived_unit': 'messages', 'excluded': None,
                    'failed_missing': None, 'total_is_dynamic': True},
        'live': {'status': doubao.get('status'), 'last_message_at': doubao.get('last_sync_at'),
                 'saved_messages': doubao.get('total_archived_message_count')},
        'last_error': doubao.get('last_error'),
    }

    try:
        ds = DSHSourceAdapter().status()
        dsh_error = ds.get('last_error')
    except Exception as exc:
        ds = {'enabled': False, 'discovered_sessions': [], 'sessions': {}, 'history': {}}
        dsh_error = type(exc).__name__
    dsh_discovered = ds.get('discovered_sessions') or []
    dsh_policy = ds.get('resume_policy') if ds.get('policy') == 'paused' else ds.get('policy')
    dsh_selected = ({row['session_id'] for row in dsh_discovered} if dsh_policy == 'all'
                    else set(ds.get('selected_session_ids') or []))
    dsh_cursors = ds.get('sessions') or {}
    dsh_found_ids = {row['session_id'] for row in dsh_discovered}
    dsh_checked = bool(dsh_selected) and all(
        sid in dsh_found_ids and sid in dsh_cursors and
        dsh_cursors[sid].get('confirmed_offset') is not None and
        not dsh_cursors[sid].get('pending_bytes') and
        not dsh_cursors[sid].get('has_pending_turn') and
        not dsh_cursors[sid].get('last_error')
        for sid in dsh_selected)
    dsh_participating = bool(ds.get('ever_configured'))
    dsh_source = {
        'id': 'dsh', 'installed': DSHSourceAdapter().root.is_dir(),
        'detected': bool(dsh_discovered), 'readable': bool(dsh_discovered),
        'configured': dsh_participating, 'participating': dsh_participating,
        'paused': dsh_participating and not ds.get('enabled'),
        'history': {'status': 'complete' if dsh_checked and not dsh_error else
                    'error' if dsh_error else 'paused' if dsh_participating and not ds.get('enabled') else
                    'reading' if ds.get('enabled') else 'not_started',
                    'unit': 'sessions', 'known_total': len(dsh_selected) if dsh_participating else None,
                    'checked': sum(sid in dsh_cursors for sid in dsh_selected),
                    'archived': (ds.get('history') or {}).get('archived_messages'),
                    'failed_missing': sum(bool(row.get('last_error')) for row in dsh_cursors.values()),
                    'total_is_dynamic': True},
        'live': {'status': 'paused' if dsh_participating and not ds.get('enabled') else
                 'not_connected' if not dsh_participating else
                 'watching' if _recent(ds.get('last_sync_at')) else 'stale',
                 'last_message_at': ds.get('last_sync_at'),
                 'saved_messages': (ds.get('history') or {}).get('archived_messages')},
        'last_error': dsh_error,
        'history_coverage': '仅获准的本机现存 DSH 会话；总量随来源增长',
    }

    try:
        cs = ClaudeSourceAdapter().status()
        claude_error = cs.get('last_error')
    except Exception as exc:
        cs = {'enabled': False, 'sessions': {}, 'history': {}, 'source_detected': False}
        claude_error = type(exc).__name__
    claude_sessions = cs.get('sessions') or {}
    claude_errors = (cs.get('history') or {}).get('current_decode_errors') or 0
    claude_policy = cs.get('resume_policy') if cs.get('policy') == 'paused' else cs.get('policy')
    claude_selected = (set(cs.get('current_session_ids') or []) if claude_policy == 'all' else
                       set(cs.get('selected_session_ids') or []))
    claude_checked = bool(cs.get('current_state_verified')) and bool(claude_selected) and all(
        sid in claude_sessions and claude_sessions[sid].get('checked_messages') is not None and
        not claude_sessions[sid].get('last_error') for sid in claude_selected)
    claude_participating = bool(cs.get('ever_configured'))
    claude_source = {
        'id': 'claude', 'installed': cs.get('source_detected', False),
        'detected': cs.get('source_detected', False), 'readable': cs.get('source_detected', False),
        'configured': claude_participating, 'participating': claude_participating,
        'paused': claude_participating and not cs.get('enabled'),
        'history': {'status': 'complete' if claude_checked and not claude_errors and not claude_error else
                    'partial' if claude_errors else 'error' if claude_error else
                    'paused' if claude_participating and not cs.get('enabled') else
                    'reading' if cs.get('enabled') else 'not_started',
                    'unit': 'local_cache_messages', 'known_total': None,
                    'checked': sum((row.get('checked_messages') or 0) for row in claude_sessions.values()),
                    'archived': (cs.get('history') or {}).get('archived_messages'),
                    'failed_missing': claude_errors, 'total_is_dynamic': True,
                    'cloud_total_known': False},
        'live': {'status': 'paused' if claude_participating and not cs.get('enabled') else
                 'not_connected' if not claude_participating else
                 'watching' if _recent(cs.get('last_sync_at')) else 'stale',
                 'last_message_at': cs.get('last_sync_at'),
                 'saved_messages': (cs.get('history') or {}).get('archived_messages')},
        'last_error': claude_error,
        'history_coverage': '仅本机当前可读缓存；不能证明云端或已清理历史完整',
    }
    cursor_state = CursorSourceAdapter(adapter.context).status()
    cursor_participating = bool(cursor_state.get('ever_configured'))
    cursor_error = cursor_state.get('last_error')
    cursor_gap = (cursor_state.get('indexed_without_transcript') or {}).get('in_scope_count') or 0
    cursor_pending = (cursor_state.get('history') or {}).get('pending_turns') or 0
    cursor_sessions = cursor_state.get('sessions') or {}
    cursor_source = {
        'id': 'cursor', 'installed': cursor_state.get('source_detected', False),
        'detected': cursor_state.get('source_detected', False),
        'readable': cursor_state.get('source_detected', False),
        'configured': cursor_participating, 'participating': cursor_participating,
        'paused': cursor_participating and not cursor_state.get('enabled'),
        'history': {'status': 'not_started' if not cursor_participating else
                    'paused' if not cursor_state.get('enabled') else
                    'error' if cursor_error else 'partial' if cursor_gap else
                    'reading' if cursor_pending else
                    'complete' if cursor_state.get('last_sync_at') else 'reading',
                    'unit': 'sessions', 'known_total': len(cursor_state.get('discovered_sessions') or []) + cursor_gap,
                    'checked': len(cursor_sessions),
                    'pending_turns': cursor_pending,
                    'archived': (cursor_state.get('history') or {}).get('archived_messages'),
                    'failed_missing': cursor_gap + sum(bool(row.get('last_error')) for row in cursor_sessions.values()),
                    'total_is_dynamic': True},
        'live': {'status': 'not_connected' if not cursor_participating else
                 'paused' if not cursor_state.get('enabled') else
                 'watching' if _recent(cursor_state.get('last_sync_at')) else 'stale',
                 'saved_messages': (cursor_state.get('history') or {}).get('archived_messages')},
        'last_error': cursor_error,
        'history_coverage': cursor_state.get('history_coverage'),
    }
    four_sources=[]
    for kind,label in SOURCES.items():
        collector=LocalCompletedCollector(kind,adapter.context)
        current=collector.status()
        participating=bool(current.get('ever_configured'))
        found_ids={row['session_id'] for row in current.get('discovered_sessions') or []}
        selected=(found_ids if current.get('policy')=='all' else
                  set(current.get('selected_session_ids') or []))
        sessions=current.get('sessions') or {}
        error=current.get('last_error')
        pending=sum(int((sessions.get(sid) or {}).get('pending_turns') or 0) for sid in selected)
        unverified=sum(int((sessions.get(sid) or {}).get('unverified_turns') or 0) for sid in selected)
        gap=(current.get('indexed_without_transcript') or {}).get('in_scope_count') or 0
        checked=sum(sid in sessions for sid in selected)
        complete=bool(selected) and selected<=found_ids and checked==len(selected) and not pending and not unverified and not gap and not error
        four_sources.append({
            'id':kind,'installed':collector._paths()[0].is_file(),
            'detected':current.get('source_detected',False),'readable':current.get('source_readable',False),
            'configured':participating,'participating':participating,
            'paused':participating and not current.get('enabled'),
            'history':{'status':'not_started' if not participating else
                       'paused' if not current.get('enabled') else
                       'error' if error else 'partial' if unverified or gap else
                       'reading' if pending else 'complete' if complete else 'reading',
                       'unit':'sessions','known_total':len(selected)+gap if participating else None,
                       'checked':checked,'pending_turns':pending,
                       'archived':(current.get('history') or {}).get('archived_messages'),
                       'failed_missing':unverified+gap,'total_is_dynamic':True},
            'live':{'status':'not_connected' if not participating else
                    'paused' if not current.get('enabled') else
                    'watching' if _recent(current.get('last_sync_at')) else 'stale',
                    'saved_messages':(current.get('history') or {}).get('archived_messages')},
            'last_error':error,'history_coverage':current.get('history_coverage')})
    all_sources = [codex_source, hermes_source, dsh_source, claude_source, cursor_source,
                   *four_sources, doubao_source]
    return {'sources': all_sources,
            'detected_source_ids': [x['id'] for x in all_sources
                                    if x['detected'] and x['readable']],
            'generated_at': _now()}
