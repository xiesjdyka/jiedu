"""Small, read-only product facts safe to return to an external Butler model."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .release_version import version_info
from .workspace import PROGRAM_ROOT, local_path

SOURCE_IDS=('codex','hermes','dsh','claude','cursor')
HISTORY_STATES={'not_started','reading','running','paused','complete','partial','error','unknown'}
LIVE_STATES={'paused','watching','waiting','stale','stopped','running','not_connected','error','unknown'}
SOURCE_UNITS={'files','records','sessions','local_cache_messages','messages','items'}
UPDATE_STATES={'unpublished','not_checked','checking','current','available','unavailable','downloaded',
               'queued','downloading','verifying','waiting_idle','handoff_ready','waiting_exit',
               'restarting','applying','installed','finalizing','complete','cancelled','failed',
               'rolled_back','recovery_required','unknown'}


def _count(value):
    return value if type(value) is int and value>=0 else None


def _state(value,allowed):
    return value if isinstance(value,str) and value in allowed else 'unknown'


def _read_json(path:Path):
    try:
        value=json.loads(path.read_text('utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def _update_summary():
    channel=_read_json(PROGRAM_ROOT/'knowledge_bridge'/'update-channel.json')
    if not channel.get('manifest_url'):
        return {'state':'unpublished','latest':None,'receipt_state':None}
    update_root=local_path('software-update')
    checked=_read_json(update_root/'last-check.json')
    workflow=_read_json(update_root/'workflow.json')
    # This helper reader verifies token and work-directory identity. Calling
    # OnlineUpdate.status() here would be wrong: with a resume callback it can
    # change update lifecycle state during an otherwise read-only query.
    try:
        from .online_update import OnlineUpdate
        receipt=OnlineUpdate(PROGRAM_ROOT,local_path())._helper_outcome()
    except Exception:receipt={}
    merged={**checked,**workflow,**receipt}
    latest=merged.get('latest') or checked.get('latest')
    return {'state':_state(merged.get('state') or 'not_checked',UPDATE_STATES),
            'latest':latest if isinstance(latest,str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.\d+)?',latest) else None,
            'receipt_state':_state(receipt.get('state'),UPDATE_STATES) if receipt else None}


def product_status(permissions=None):
    """No client IDs, paths, titles, snippets, secrets or raw exception text."""
    try:version=version_info(PROGRAM_ROOT)['product_version']
    except (OSError,ValueError):version=None
    update=_update_summary()
    clients=[];butler={}
    if permissions is not None:
        try:clients=permissions.registry.list_clients()
        except (OSError,ValueError):clients=[]
        try:butler=permissions.butler_policies().get('policies') or {}
        except (OSError,ValueError):butler={}
    active=[row for row in clients if row.get('status')!='revoked']
    by_type={kind:{'registered':sum(row.get('client_type')==kind for row in active),
                   'authenticated':sum(row.get('client_type')==kind and bool(row.get('verified_at')) for row in active),
                   'read_seen':sum(row.get('client_type')==kind and bool(row.get('last_read_at')) for row in active)}
             for kind in SOURCE_IDS}
    decisions={key:sum(value==key for value in butler.values()) for key in ('allow','ask','deny')}
    try:
        from .memory_control import interface_state
        mcp_enabled=bool(interface_state().get('mcp_enabled'))
    except (OSError,ValueError):mcp_enabled=None
    return {'version':version,'public_version':version,
            'maintenance_build':{'state':'unknown','id':None},
            'update':{'state':update['state'],'latest':update['latest'],
                      'receipt_state':update['receipt_state']},
            'connection':by_type,'permissions':{'mcp_enabled':mcp_enabled,'butler_decisions':decisions},
            'notice':'已认证和曾成功读取是不同证据；当前读取能力以客户端实际调用结果为准。'}


def _source_summary(source):
    history=source.get('history') or {};live=source.get('live') or {}
    by_source=[]
    for item in (history.get('by_source') or [])[:8]:
        if not isinstance(item,dict):continue
        by_source.append({'kind':item.get('source') if item.get('source') in {'jsonl','sqlite'} else 'other',
                          'unit':_state(item.get('unit'),SOURCE_UNITS),
                          'archived_unit':_state(item.get('archived_unit'),SOURCE_UNITS),
                          'known_total':_count(item.get('known_total')),'checked':_count(item.get('checked')),
                          'archived':_count(item.get('archived')),'excluded':_count(item.get('excluded')),
                          'failed_missing':_count(item.get('failed_missing')),
                          'needs_review':_count(item.get('needs_review'))})
    return {'id':source['id'],'detected':bool(source.get('detected')),'readable':bool(source.get('readable')),
            'configured':bool(source.get('configured')),'paused':bool(source.get('paused')),
            'history':{'status':_state(history.get('status'),HISTORY_STATES),
                       'unit':_state(history.get('unit'),SOURCE_UNITS),
                       'archived_unit':_state(history.get('archived_unit'),SOURCE_UNITS),
                       'known_total':_count(history.get('known_total')),'checked':_count(history.get('checked')),
                       'archived':_count(history.get('archived')),'excluded':_count(history.get('excluded')),
                       'failed_missing':_count(history.get('failed_missing')),
                       'total_is_dynamic':bool(history.get('total_is_dynamic',True)),
                       'by_source':by_source},
            'live':{'status':_state(live.get('status'),LIVE_STATES),
                    'saved_messages':_count(live.get('saved_messages'))},
            'gaps':{'source_error':bool(source.get('last_error')),
                    'known_failed_missing':_count(history.get('failed_missing')),
                    'coverage':'local_visible_only' if source['id']=='claude' else 'configured_local_scope'}}


def source_archive_progress():
    """Ask the same read-only source monitor as the Owner UI, then allowlist fields."""
    try:
        from .local_source_status import status
        rows={row['id']:row for row in status()['sources'] if isinstance(row,dict) and row.get('id') in SOURCE_IDS}
    except Exception:
        rows={}
    result=[]
    for kind in SOURCE_IDS:
        row=rows.get(kind)
        if row:result.append(_source_summary(row))
        else:result.append({'id':kind,'detected':False,'readable':False,'configured':False,'paused':False,
                            'history':{'status':'unknown','unit':'unknown','archived_unit':'unknown',
                                       'known_total':None,'checked':None,'archived':None,
                                       'excluded':None,'failed_missing':None,'total_is_dynamic':True,'by_source':[]},
                            'live':{'status':'unknown','saved_messages':None},
                            'gaps':{'source_error':True,'known_failed_missing':None,'coverage':'unknown'}})
    return {'sources':result,'notice':'总量只涵盖当前本机可见且获准的来源；未知不等于零。'}
