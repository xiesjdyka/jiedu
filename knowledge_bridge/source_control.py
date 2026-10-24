"""Resume only a fingerprinted, pre-existing paused source after Owner confirmation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ResumeBlocked(ValueError):
    NEXT_STEP={
        'never_configured':'请先在归档页面核对并配置来源。',
        'not_paused':'此来源当前不是暂停状态，请查看归档页面。',
        'original_source_identity_missing':'无法证明原来源身份，请到归档页面核对原来源后恢复。',
        'source_changed':'来源身份或文件已变化，请到归档页面重新核对。',
        'scope_changed':'归档范围或游标已变化，请重新发起并确认。',
        'source_unavailable':'原来源当前不可读，请在归档页面检查。',
        'unknown_source':'没有这项来源采集。',
    }
    def __init__(self,code):
        self.code=code if code in self.NEXT_STEP else 'source_changed'
        self.next_step=self.NEXT_STEP[self.code]
        super().__init__(self.next_step)


def _stamp(path):
    item=Path(path).resolve()
    try:stat=item.stat()
    except OSError as exc:raise ResumeBlocked('source_unavailable') from exc
    return {'path':str(item),'device':stat.st_dev,'inode':stat.st_ino,
            'bytes':stat.st_size,'modified_ns':stat.st_mtime_ns}


def _source(source,context):
    if source=='codex':
        from .codex_source_adapter import CodexSourceAdapter
        adapter=CodexSourceAdapter(context,status_only=True)
        if not adapter.state_path.is_file():raise ResumeBlocked('never_configured')
        state=adapter._state()
        policy=state['archive_policy'];resume=state['resume_policy']
        identity=json.dumps(_stamp(adapter.codex_home),sort_keys=True)
        selected=state['selected_session_ids'];excluded=state['excluded_session_ids']
        if resume=='selected' and not selected:raise ResumeBlocked('scope_changed')
    elif source in {'dsh','claude','cursor'}:
        if source=='dsh':
            from .dsh_source_adapter import DSHSourceAdapter
            adapter=DSHSourceAdapter(context);identity=str(adapter.root)
        elif source=='claude':
            from .claude_source_adapter import ClaudeSourceAdapter
            adapter=ClaudeSourceAdapter(context)
            identity=json.dumps({'profile':adapter.profile_id,'cache':str(adapter.leveldb)},sort_keys=True)
        else:
            from .cursor_source_adapter import CursorSourceAdapter
            adapter=CursorSourceAdapter(context)
            identity=json.dumps({'database':str(adapter.database),'projects':str(adapter.projects)},sort_keys=True)
        if not adapter.state_path.is_file():raise ResumeBlocked('never_configured')
        state=adapter._load();policy=state['policy'];resume=state['resume_policy']
        selected=state['selected_session_ids'];excluded=[]
        if source=='claude':
            if not state.get('profile_id') or state['profile_id']!=adapter.profile_id or not adapter.leveldb or not (adapter.leveldb/'CURRENT').is_file():
                raise ResumeBlocked('original_source_identity_missing')
            identity+=json.dumps(_stamp(adapter.leveldb/'CURRENT'),sort_keys=True)
        else:
            discovered={row['session_id']:str(Path(row['path']).resolve()) for row in adapter.discover()}
            bindings=state.get('selected_sources') or {}
            if resume=='selected':
                if any(sid not in bindings or sid not in discovered or
                       str(Path(bindings[sid]).resolve())!=discovered[sid] for sid in selected):
                    raise ResumeBlocked('source_changed')
            else:
                previous=state.get('sessions') or {}
                if not previous or any(not row.get('source_path') or sid not in discovered or
                   str(Path(row['source_path']).resolve())!=discovered[sid] for sid,row in previous.items()):
                    raise ResumeBlocked('original_source_identity_missing')
            proof_ids=selected if resume=='selected' else sorted((state.get('sessions') or {}))
            identity+=json.dumps([_stamp(discovered[sid]) for sid in proof_ids],sort_keys=True)
    elif source=='hermes':
        from . import hermes_session_adapter as adapter
        if not adapter.STATE.is_file():raise ResumeBlocked('never_configured')
        state=adapter._load();history=state.get('history') or {}
        policy=history.get('status');resume='existing_history'
        selected=[];excluded=[]
        identity=str(adapter.selected_db_path().resolve())
        if not state.get('db_path') or Path(state['db_path']).resolve()!=Path(identity) or not Path(identity).is_file():
            raise ResumeBlocked('source_changed')
        # Old Hermes state stores only a path, not the database file identity.
        # A replacement at the same path cannot be proven safe, so refuse it.
        recorded=state.get('source_identity') or history.get('source_identity')
        current=Path(identity).stat()
        if not isinstance(recorded,dict) or not current.st_ino:
            raise ResumeBlocked('original_source_identity_missing')
        if recorded.get('device')!=current.st_dev or recorded.get('inode')!=current.st_ino:
            raise ResumeBlocked('source_changed')
        identity=json.dumps(_stamp(identity),sort_keys=True)
    else:raise ResumeBlocked('unknown_source')
    state_path=adapter.STATE if source=='hermes' else adapter.state_path
    if not state_path.is_file():raise ResumeBlocked('never_configured')
    if policy!='paused':raise ResumeBlocked('not_paused')
    if resume not in {'all','selected','existing_history'}:raise ResumeBlocked('scope_changed')
    if resume=='selected' and not selected:raise ResumeBlocked('scope_changed')
    # Bind every saved cursor and policy byte, plus the currently selected source
    # identity. The digest is shown to the Owner, never the private identity.
    state_bytes=state_path.read_bytes()
    state_fingerprint=hashlib.sha256(state_bytes).hexdigest()
    identity_fingerprint=hashlib.sha256(identity.encode('utf-8')).hexdigest()
    digest=hashlib.sha256(state_bytes+b'\0'+identity.encode('utf-8')).hexdigest()
    scope_digest=hashlib.sha256(json.dumps({'resume':resume,'selected':selected,'excluded':excluded},
                                          sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
    preview={'source':source,'action':'resume_existing','policy':resume,
             'selected_count':len(selected),'excluded_count':len(excluded),
             'fingerprint':digest,'scope_fingerprint':scope_digest,
             'state_fingerprint':state_fingerprint,'identity_fingerprint':identity_fingerprint,
             'scope_preserved':True,'cursor_preserved':True}
    return adapter,state,preview


def resume_preview(source:str,context):
    return _source(source,context)[2]


def resume_existing(source:str,context,*,expected:dict):
    adapter,state,preview=_source(source,context)
    def validate_before_write():
        # Run inside the adapter's own write lock. Web configuration and worker
        # writes use that same lock, so the confirmed state cannot change
        # between this comparison and the adapter's state update.
        current=_source(source,context)[2]
        if not isinstance(expected,dict) or expected!=current:
            scope_or_cursor_changed=isinstance(expected,dict) and (
                expected.get('scope_fingerprint')!=current['scope_fingerprint'] or
                expected.get('state_fingerprint')!=current['state_fingerprint'])
            raise ResumeBlocked('scope_changed' if scope_or_cursor_changed else 'source_changed')
    validate_before_write()
    if source=='codex':
        result=adapter.configure(enabled=True,owner_confirmed=True,
                                 validate_before_write=validate_before_write)
        policy=result.get('archive_policy')
    elif source in {'dsh','claude','cursor'}:
        result=adapter.configure(state['resume_policy'],tuple(state['selected_session_ids']),
                                 owner_confirmed=True,validate_before_write=validate_before_write)
        policy=result.get('policy')
    else:
        adapter.history_control('resume',db_path=Path(adapter.selected_db_path()).resolve(),
                                owner_confirmed=True,validate_before_write=validate_before_write)
        policy='existing_history'
    return {'source':source,'state':'resumed','policy':policy,
            'scope_preserved':True,'cursor_preserved':True}
