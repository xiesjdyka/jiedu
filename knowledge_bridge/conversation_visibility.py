"""Shared, reversible source classification; never edits original evidence."""
from __future__ import annotations
import json
import hashlib
from functools import lru_cache
from functools import wraps
from contextvars import ContextVar
from pathlib import Path

EXCLUDED_ROLES = frozenset({
    'validation', 'test', 'test_fixture', 'internal_process', 'tool_log',
    'quarantine', 'subtask', 'subagent', 'guardian', 'system',
})
_operation_snapshots = ContextVar('conversation_visibility_snapshots', default=None)


def visibility_operation(function):
    """Nested source checks share one lazy snapshot, disposed at operation end."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _operation_snapshots.get() is not None:
            return function(*args, **kwargs)
        token = _operation_snapshots.set({})
        try:return function(*args, **kwargs)
        finally:_operation_snapshots.reset(token)
    return wrapped


def visibility_snapshot(context):
    snapshots=_operation_snapshots.get()
    if snapshots is None:return SourceVisibility(context)
    key=str(context.root.resolve())
    if key not in snapshots:snapshots[key]=SourceVisibility(context)
    return snapshots[key]


def codex_exclusion(meta):
    for field in ('source', 'thread_source'):
        source = meta.get(field)
        if isinstance(source, dict) and isinstance(source.get('subagent'), dict):
            return 'codex_subagent'
    return None


def hermes_exclusions(rows):
    """Mirror installed Hermes lineage semantics, not a parent-ID heuristic.

    hermes_state_common.py: _BRANCH_CHILD_SQL, _RESET_CHILD_SQL,
    _COMPRESSION_CHILD_SQL and _ephemeral_child_sql. Missing metadata is unknown.
    Compression continuations remain eligible because they carry public chat.
    """
    by_id = {str(row['id']): dict(row) for row in rows}
    excluded = {}
    for sid, row in by_id.items():
        parent_id = row.get('parent_session_id')
        if not parent_id:
            continue
        parent = by_id.get(str(parent_id))
        if parent is None or 'model_config' not in row:
            continue
        try:
            config = json.loads(row.get('model_config') or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        reason = parent.get('end_reason')
        if config.get('_branched_from') is not None or config.get('_reset_from') is not None:
            continue
        if reason == 'compression':
            continue
        if reason == 'branched':
            if row.get('started_at') is None or parent.get('ended_at') is None:
                continue
            if row['started_at'] >= parent['ended_at']:
                continue
        if reason in {'session_reset', 'session_switch', 'idle', 'daily', 'suspended', 'resume_pending_expired'}:
            if 'session_key' not in row or 'session_key' not in parent:
                continue
            if row.get('session_key') and row['session_key'] == parent.get('session_key'):
                continue
        excluded[sid] = {'reason': 'hermes_ephemeral_child', 'parent_session_id': str(parent_id),
                         'source': row.get('source'), 'parent_end_reason': reason}
    return excluded


def aliases(row):
    # Preserve provider prefixes; bare source IDs only gain an explicit provider.
    client = str(row.get('source_key') or row.get('client') or row.get('client_id') or '').lower()
    result = set()
    for key in ('session_key', 'session_id', 'source_session_id'):
        value = row.get(key)
        if not isinstance(value, str) or not value:
            continue
        result.add(f'{client}:{value}' if client and ':' not in value else value)
    return result


def declared_exclusion(row):
    for field in ('archive_kind', 'data_role', 'document_role'):
        role = str(row.get(field) or '').casefold()
        if role in EXCLUDED_ROLES:
            return role
    if row.get('searchable') is False:
        return 'explicitly_excluded'
    return codex_exclusion(row)


def organization_runner(context, row):
    """Only a configured stable provider identity excludes the organizer itself."""
    config = read_json(context.state / 'organization-runner.json', {})
    if config.get('workspace_id') != context.workspace_id:
        return False
    source = str(row.get('source_key') or '').casefold()
    session = str(row.get('source_session_id') or '')
    return bool(source and session and any(
        item.get('source_key') == source and item.get('source_session_id') == session
        for item in config.get('identities', []) if isinstance(item, dict)))


@lru_cache(maxsize=2048)
def _cached_json(path, size, mtime):
    return json.loads(Path(path).read_text('utf-8'))


def read_json(path, default):
    try:
        stat=path.stat()
        return _cached_json(str(path),stat.st_size,stat.st_mtime_ns)
    except (OSError, ValueError):
        return default


def exclusion_aliases(manifests, state_root):
    blocked = set()
    for row in manifests:
        if declared_exclusion(row):
            blocked.update(aliases(row))
    ledger = read_json(Path(state_root) / 'conversation-exclusions.json', {})
    for row in ledger.get('records', []):
        if row.get('excluded') is True:
            blocked.update(aliases(row))
    adapter = read_json(Path(state_root) / 'sync' / 'hermes-adapter.json', {})
    for sid in adapter.get('excluded_sessions', {}):
        blocked.add(f'hermes:{sid}')
    return blocked


def public_message(row):
    provenance=row.get('provenance') or {}
    notice=(str(row.get('role') or '').casefold()=='system' and provenance.get('adapter')=='hermes_sqlite_readonly' and provenance.get('display_semantics')=='system_notice')
    return ((str(row.get('role') or '').casefold() in {'user', 'assistant'} or notice)
            and bool(str(row.get('original_text') or row.get('content') or '').strip()))


def message_structure_reason(channel=None, recipient=None):
    """Classify explicit routing metadata, never words in the message body.

    Unknown values remain distinguishable from known internal routes. Codex can
    quarantine those envelopes; legacy sources without these fields still work.
    """
    if isinstance(channel,str) and channel.casefold() in {'analysis', 'reasoning'}:return 'internal_message_channel'
    if recipient not in (None, ''):
        route=recipient.casefold() if isinstance(recipient,str) else None
        if route and route not in {'all', 'user'}:
            if route.startswith(('/', 'functions.', 'tools.', 'multi_tool_use.', 'mcp__',
                                 'web.', 'python.', 'container.', 'browser.', 'file_search.', 'canmore.')) or route in {'python', 'browser', 'container', 'web'}:
                return 'internal_message_recipient'
    if channel not in (None, '') and (not isinstance(channel,str) or channel.casefold() not in {'commentary','final','final_answer'}):return 'unknown_message_channel'
    if recipient not in (None, '') and (not isinstance(recipient,str) or recipient.casefold() not in {'all','user'}):return 'unknown_message_recipient'
    return None


def knowledge_message(row):
    """Ordinary chat eligible for organization; raw evidence may retain more.

    Missing legacy metadata is compatible. A tool call attached to visible
    assistant prose, an unfamiliar display kind, or a code block is not a reason
    to discard that prose.
    """
    if not isinstance(row, dict) or str(row.get('role') or '').casefold() not in {'user', 'assistant'}:return False
    provenance=row.get('provenance') or {}
    if not isinstance(provenance, dict):return False
    if provenance.get('knowledge_eligible') is False:return False
    if provenance.get('display_semantics') in {'hidden', 'model_only', 'legacy_heartbeat', 'system_notice', 'internal_message_channel', 'internal_message_recipient'}:return False
    if str(provenance.get('source_role') or '').casefold() in {'system', 'developer', 'tool'}:return False
    if str(row.get('role') or '').casefold()=='assistant' and str(provenance.get('assistant_phase') or '').casefold() in {'analysis', 'reasoning'}:return False
    reason=message_structure_reason(provenance.get('source_channel',row.get('channel')),
                                    provenance.get('source_recipient',row.get('recipient')))
    return reason not in {'internal_message_channel', 'internal_message_recipient'}


@lru_cache(maxsize=1024)
def _file_has_public(path, size, mtime):
    try:
        with Path(path).open('r', encoding='utf-8-sig') as stream:
            if Path(path).suffix == '.json':
                rows = json.load(stream)
                return any(public_message(row) for row in rows if isinstance(row, dict)) if isinstance(rows, list) else None
            invalid = False
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    invalid = True
                    continue
                if isinstance(row, dict) and public_message(row):
                    return True
            return None if invalid else False
    except (OSError, ValueError, UnicodeError):
        return None


def has_public_file(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return _file_has_public(str(path), stat.st_size, stat.st_mtime_ns)


def exclusion_reason(row, blocked=(), metadata_folder=None):
    reason = declared_exclusion(row)
    if reason:
        return reason
    if aliases(row).intersection(blocked):
        return 'source_classification'
    if 'messages' in row:
        return None if any(public_message(message) for message in row['messages']) else 'no_public_messages'
    if metadata_folder is None and row.get('archive_path'):
        folder = Path(row['archive_path'])
        if folder.is_file():
            return 'no_public_messages' if has_public_file(folder) is False else None
        metadata_folder = folder / '03_元数据' if (folder / '03_元数据').is_dir() else folder
    if metadata_folder is not None:
        for name in ('messages.jsonl', 'messages.json'):
            path = Path(metadata_folder) / name
            if path.is_file():
                return 'no_public_messages' if has_public_file(path) is False else None
    if row.get('message_count') == 0:
        return 'no_public_messages'
    return None


class SourceVisibility:
    """One operation's snapshot, covering standard ledgers and legacy mirrors."""
    def __init__(self, context):
        self.root = context.root.resolve()
        self.raw = context.raw_archive.resolve()
        manifests = [(path, read_json(path, {})) for path in self.raw.rglob('manifest.json')]
        blocked = exclusion_aliases([row for _, row in manifests], getattr(context, 'state', self.root/'.jiedu'/'state'))
        self.excluded_roots = []
        self.registered_roots = []
        self.excluded_conversations = []
        for path, row in manifests:
            folder = path.parent.parent if path.parent.name == '03_元数据' else path.parent
            self.registered_roots.append(folder.resolve())
            reason = ('internal_organization_task' if organization_runner(context, row)
                      else exclusion_reason(row, blocked, path.parent))
            if not reason:
                continue
            self.excluded_roots.append((folder.resolve(), reason))
            for candidate in (folder/'conversation.md', folder/'01_对话'/'conversation.md', folder/'02_标准化对话'/'conversation.md'):
                if candidate.is_file():self.excluded_conversations.append(candidate)

    def reason(self, source):
        path = (self.root / source).resolve()
        if not path.is_relative_to(self.raw):
            return None
        for folder, reason in self.excluded_roots:
            if path.is_relative_to(folder):
                return reason
        if any(path.is_relative_to(folder) for folder in self.registered_roots):
            return None
        if path.suffix.casefold() == '.md' and path.is_file():
            from .search_scopes import _declared_document_role
            if _declared_document_role(path) in EXCLUDED_ROLES:
                return 'declared_document_role'
            digests=set()
            for candidate in self.excluded_conversations:
                try:
                    stat=candidate.stat();digests.add(_content_digest(str(candidate),stat.st_size,stat.st_mtime_ns))
                except OSError:pass
            stat=path.stat()
            if digests and _content_digest(str(path),stat.st_size,stat.st_mtime_ns) in digests:
                return 'excluded_source_copy'
        return None


def source_exclusion_reason(context, source):
    path = (context.root / source).resolve()
    if not path.is_relative_to(context.raw_archive.resolve()):
        return None
    return visibility_snapshot(context).reason(source)


@lru_cache(maxsize=1024)
def _content_digest(path,size,mtime):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
