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

    adapter = CodexSourceAdapter()
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
    return {'sources': [codex_source, hermes_source, doubao_source],
            'detected_source_ids': [x['id'] for x in (codex_source, hermes_source, doubao_source)
                                    if x['detected'] and x['readable']],
            'generated_at': _now()}
