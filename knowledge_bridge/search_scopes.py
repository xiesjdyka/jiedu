"""Explicit retrieval boundaries for formal knowledge, evidence, and inbox material."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .workspace import WorkspaceContext, active_workspace
from .conversation_visibility import EXCLUDED_ROLES, exclusion_aliases, exclusion_reason, _content_digest


SCOPES = {"formal", "evidence", "inbox"}
EXCLUDED_PARTS = {".jiedu", ".obsidian", ".git", ".knowledge-bridge", "node_modules", "__pycache__"}
ATTACHMENT_PARTS = {"02_附件", "attachments", "01_图片附件", "00_原始数据", "导入包"}
UNREGISTERED_ATTACHMENT_PARTS = {"02_附件", "attachments", "01_图片附件"}
EVIDENCE_EXCLUDED_ROLES = EXCLUDED_ROLES
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()
_STATUS_TTL_SECONDS = 15.0


def files_for_scope(context: WorkspaceContext, scope: str) -> list[Path]:
    if scope not in SCOPES:
        raise ValueError('无效检索范围；有效值：formal（正式知识）、evidence（原始证据）、inbox（收件箱）')
    if scope == "formal":
        roots = list(context.formal_roots)
    elif scope == "evidence":
        return _canonical_evidence_files(context)
    else:
        roots = [context.inbox]
    rows: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            relative_parts = path.relative_to(context.root).parts
            if any(part in EXCLUDED_PARTS for part in relative_parts):
                continue
            if scope == "evidence" and any(part in ATTACHMENT_PARTS for part in relative_parts):
                continue
            rows.append(path)
    return sorted(rows)


def _canonical_evidence_files(context: WorkspaceContext) -> list[Path]:
    """Merge canonical sessions with independent evidence without duplicate projections."""
    return _canonical_evidence_selection(context)[0]


@lru_cache(maxsize=512)
def _ledger_attachment_specs(path_text, signature):
    # Cache only attachment metadata, never full chat bodies. The caller stats
    # the ledger on every scan, including identity, size and nanosecond times.
    path=Path(path_text);specs=[]
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():continue
            row=json.loads(line);specs.extend(row.get('attachments') or [])
            if path.name=='attachments.jsonl':specs.append(row)
    return tuple(specs)


def _declared_document_role(path: Path) -> str:
    try:
        with path.open('r',encoding='utf-8-sig',errors='replace') as stream:head=stream.read(8192)
    except OSError:return ''
    match=re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)',head,re.S)
    if not match:return ''
    for line in match[1].splitlines():
        key,separator,value=line.partition(':')
        if separator and key.strip() in {'document_role','data_role','archive_kind'}:
            return value.strip().strip('"\'').casefold()
    return ''


def _canonical_evidence_selection(context: WorkspaceContext) -> tuple[list[Path],dict]:
    """Return selected evidence and auditable reason counts.

    A registered archive contributes one canonical conversation and referenced
    readable attachments. Markdown outside every registered archive remains
    eligible as independent evidence. Exact byte duplicates are selected once.
    """
    root=context.raw_archive;resolved_root=root.resolve()
    audit=Counter()
    if not root.exists():return [],{'selected':0,'reasons':{}}
    chosen={};attachments={};registered_roots=[];manifests=[];excluded_digests=set()
    for manifest_path in sorted(root.rglob('manifest.json')):
        relative=manifest_path.relative_to(context.root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):audit['manifest_hidden']+=1;continue
        try:manifest=json.loads(manifest_path.read_text('utf-8'))
        except (OSError,ValueError,UnicodeError):audit['manifest_invalid']+=1;continue
        manifests.append((manifest_path,manifest))
    blocked=exclusion_aliases([row for _,row in manifests],getattr(context,'state',context.root/'.jiedu'/'state'))
    for manifest_path,manifest in manifests:
        metadata=manifest_path.parent;archive=metadata.parent if metadata.name=='03_元数据' else metadata
        registered_roots.append(archive.resolve())
        reason=exclusion_reason(manifest,blocked,metadata)
        if reason:
            if reason!='no_public_messages':audit['manifest_explicitly_excluded']+=1
            audit['manifest_excluded_'+reason]+=1
            for path in (archive/'01_对话'/'conversation.md',archive/'02_标准化对话'/'conversation.md',archive/'conversation.md'):
                try:
                    stat=path.stat();excluded_digests.add(_content_digest(str(path),stat.st_size,stat.st_mtime_ns))
                except OSError:pass
            continue
        identity=str(manifest.get('session_key') or manifest.get('session_id') or manifest.get('source_session_id') or '')
        if not identity:identity=hashlib.sha256(str(archive.relative_to(root)).encode('utf-8')).hexdigest()
        candidates=(archive/'01_对话'/'conversation.md',archive/'02_标准化对话'/'conversation.md',archive/'conversation.md')
        conversation=next((path for path in candidates if path.is_file()),None)
        if conversation:
            priority=candidates.index(conversation);current=chosen.get(identity)
            if current is None or priority<current[0]:
                if current is not None:audit['canonical_identity_replaced']+=1
                chosen[identity]=(priority,conversation)
            else:audit['canonical_identity_duplicate']+=1
        ledgers=[metadata/'attachments.jsonl',metadata/'messages.jsonl']
        for ledger in ledgers:
            if not ledger.is_file():continue
            try:
                stat=ledger.stat()
                specs=_ledger_attachment_specs(str(ledger),(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
            except (OSError,ValueError,UnicodeError):continue
            for spec in specs:
                if not isinstance(spec,dict) or str(spec.get('status') or 'available').casefold() not in {'available','mapped'}:continue
                stored=spec.get('archived_path') or spec.get('stored_path')
                if not stored:continue
                candidate=Path(str(stored));candidate=candidate if candidate.is_absolute() else context.root/candidate
                # Only Markdown contributes here. Resolve links when needed,
                # but don't walk Windows filesystem ancestry for every image,
                # video and installer in the attachment ledger on every tick.
                if candidate.suffix.casefold()!='.md' and not candidate.is_symlink():continue
                try:resolved=candidate.resolve();resolved.relative_to(resolved_root)
                except (OSError,ValueError):continue
                # Plain-text RAW_SOURCE exports are conversation containers and
                # would duplicate the canonical message projection. Markdown
                # attachments are independently readable derived material.
                if resolved.is_file() and resolved.suffix.casefold()=='.md':
                    try:digest=hashlib.sha256(resolved.read_bytes()).hexdigest()
                    except OSError:continue
                    previous=attachments.get(digest)
                    if previous is None or len(str(resolved))<len(str(previous)):attachments[digest]=resolved
    candidates=[]
    for _,path in sorted(chosen.values(),key=lambda item:str(item[1])):candidates.append(('canonical',path))
    for path in sorted(set(attachments.values())):candidates.append(('attachment',path))
    for path in sorted(root.rglob('*.md')):
        relative=path.relative_to(context.root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):audit['loose_hidden']+=1;continue
        if any(part in UNREGISTERED_ATTACHMENT_PARTS for part in relative.parts):
            audit['unregistered_attachment']+=1;continue
        resolved=path.resolve()
        if any(resolved==archive or resolved.is_relative_to(archive) for archive in registered_roots):
            audit['loose_under_registered_archive']+=1;continue
        if _declared_document_role(path) in EVIDENCE_EXCLUDED_ROLES:
            audit['loose_explicitly_excluded']+=1;continue
        candidates.append(('independent',path))
    selected=[];seen_paths=set();seen_digests=set()
    for kind,path in candidates:
        resolved=path.resolve()
        if resolved in seen_paths:continue
        try:digest=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:audit['unreadable']+=1;continue
        if kind=='independent' and digest in excluded_digests:
            audit['loose_excluded_source_copy']+=1;continue
        if digest in seen_digests:audit['exact_content_duplicate']+=1;continue
        seen_paths.add(resolved);seen_digests.add(digest);selected.append(path);audit['selected_'+kind]+=1
    return sorted(selected),{'selected':len(selected),'registered_archives':len(registered_roots),'reasons':dict(sorted(audit.items()))}


def evidence_selection_status(context: WorkspaceContext | None = None) -> dict:
    """Report selection reasons without exposing evidence contents."""
    context=context or active_workspace();return _canonical_evidence_selection(context)[1]


def rebuild_scope(scope: str, context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    files = files_for_scope(context, scope)
    records = []
    chunk_count = 0
    for path in files:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        if scope=='evidence':
            from .passage_search import metadata,passages
            _,begin=metadata(text);chunks=len(list(passages(text,begin)))
        else:chunks = max(1, len([x for x in re.split(r"\n\s*\n", text) if x.strip()])) if text.strip() else 0
        chunk_count += chunks
        records.append({
            "path": context.relative(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "chunks": chunks,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    result = {
        "scope": scope, "workspace_id": context.workspace_id, "format": "jiedu-scope-index-v1",
        "file_count": len(records), "chunk_count": chunk_count,
        "layers": dict(Counter(Path(x["path"]).parts[0] for x in records)),
        "files": records, "rebuilt_at": datetime.now(timezone.utc).isoformat(),
        "filesystem_fingerprint": _filesystem_fingerprint(context, files),
        "rebuildable": True,
    }
    target = context.index_root(scope) / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    with _STATUS_LOCK:_STATUS_CACHE.pop(context.workspace_id, None)
    return result

def publish_evidence_manifest(context: WorkspaceContext, file_state: dict) -> dict:
    """Publish evidence scope health from an already parsed vector generation."""
    records=[];files=[]
    for relative,state in sorted(file_state.items()):
        path=context.root/relative
        try:
            stat=path.stat();files.append(path)
            records.append({'path':relative,'sha256':state.get('hash'),'bytes':stat.st_size,
                'chunks':int(state.get('chunks') or 0),'modified_at':datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()})
        except OSError:continue
    result={'scope':'evidence','workspace_id':context.workspace_id,'format':'jiedu-scope-index-v1',
        'file_count':len(records),'chunk_count':sum(row['chunks'] for row in records),
        'layers':dict(Counter(Path(row['path']).parts[0] for row in records)),'files':records,
        'rebuilt_at':datetime.now(timezone.utc).isoformat(),'filesystem_fingerprint':_filesystem_fingerprint(context,files),'rebuildable':True}
    target=context.index_root('evidence')/'manifest.json';target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8');os.replace(temporary,target)
    with _STATUS_LOCK:_STATUS_CACHE.pop(context.workspace_id,None)
    return result


def rebuild_all(context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    return {scope: rebuild_scope_stable(scope, context) for scope in ("formal", "evidence", "inbox")}


def rebuild_scope_stable(scope: str, context: WorkspaceContext | None = None, attempts: int = 4) -> dict:
    """Retry the cheap manifest pass when an archive is appended mid-snapshot."""
    context=context or active_workspace();result={}
    for attempt in range(max(1,int(attempts))):
        result=rebuild_scope(scope,context)
        files=files_for_scope(context,scope)
        stable=result.get('file_count')==len(files) and result.get('filesystem_fingerprint')==_filesystem_fingerprint(context,files)
        if stable:return result|{'stable_snapshot':True,'attempts':attempt+1}
    return result|{'stable_snapshot':False,'attempts':max(1,int(attempts))}


def scope_status(context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    result = {}
    for scope in SCOPES:
        path = context.index_root(scope) / "manifest.json"
        try:
            result[scope] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result[scope] = {"scope": scope, "file_count": 0, "chunk_count": 0, "status": "missing"}
    return result


def _filesystem_fingerprint(context: WorkspaceContext, files: list[Path]) -> str:
    rows=[]
    for path in files:
        try:
            stat=path.stat();rows.append((context.relative(path),stat.st_size,stat.st_mtime_ns))
        except OSError:rows.append((context.relative(path),None,None))
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def scope_health(context: WorkspaceContext | None = None, *, max_age_seconds: float = _STATUS_TTL_SECONDS) -> dict:
    """Lightweight manifest freshness with a short cache for status polling."""
    context=context or active_workspace();now=time.monotonic()
    with _STATUS_LOCK:
        cached=_STATUS_CACHE.get(context.workspace_id)
        if cached and now-cached['at']<=max(0,float(max_age_seconds)):
            return cached['value']
    manifests=scope_status(context);result={}
    for scope in ('formal','evidence','inbox'):
        files=files_for_scope(context,scope);recorded=manifests.get(scope,{})
        fingerprint=_filesystem_fingerprint(context,files)
        fresh=(recorded.get('workspace_id')==context.workspace_id and
               recorded.get('file_count')==len(files) and
               recorded.get('filesystem_fingerprint')==fingerprint)
        result[scope]={"actual_files":len(files),"manifest_files":recorded.get('file_count',0),
                       "manifest_rebuilt_at":recorded.get('rebuilt_at'),"manifest_fresh":fresh,
                       "filesystem_fingerprint":fingerprint,"manifest_fingerprint":recorded.get('filesystem_fingerprint')}
    with _STATUS_LOCK:_STATUS_CACHE[context.workspace_id]={'at':now,'value':result}
    return result


def _formal_compatibility_selection(context, files):
    """Collapse explicit one-target redirect notes only; never match product names."""
    from .passage_search import metadata
    available={}
    for path in files:
        resolved=path.resolve()
        try:relative=resolved.relative_to(context.root.resolve())
        except ValueError:continue
        if not path.is_symlink() and relative.parts[0] in {'00_系统','01_当前状态','02_项目','03_知识'}:available[resolved]=path
    notes={}
    for resolved,path in available.items():
        try:
            if path.stat().st_size>8192:continue
            text=path.read_text('utf-8-sig');meta,begin=metadata(text)
        except (OSError,UnicodeError):continue
        if meta.get('document_role')=='旧路径兼容指引':notes[resolved]=text[begin:]
    redirects={}
    for source,body in notes.items():
        links=re.findall(r'(?<!!)\[\[([^\]\n]+)\]\]',body)
        targets={link.split('|',1)[0].split('#',1)[0].strip() for link in links}
        if len(targets)!=1:continue
        name=next(iter(targets))
        if not name or ':' in name or '\\' in name:continue
        raw=Path(name)
        if raw.is_absolute():continue
        target=(source.parent/raw).with_suffix('.md') if not raw.suffix else source.parent/raw
        target=target.resolve()
        # Only collapse onto an existing, independently readable formal source.
        # Broken targets, chains/cycles and targets outside this scope remain visible.
        if target not in available or target in notes or target==source:continue
        redirects[source]=target
    selected=[path for path in files if path.resolve() not in redirects]
    audit=[{'source_path':context.relative(source),'target_path':context.relative(target)} for source,target in sorted(redirects.items())]
    return selected,audit


def search_scope(query: str, scope: str = "formal", limit: int = 10,
                 context: WorkspaceContext | None = None, **options) -> dict:
    context = context or active_workspace()
    if scope not in SCOPES:raise ValueError('无效检索范围；有效值：formal（正式知识）、evidence（原始证据）、inbox（收件箱）')
    from .passage_search import search
    files=files_for_scope(context,scope);redirects=[]
    if scope=='formal' and not any(options.get(key) for key in ('folder','project','source_type')):
        files,redirects=_formal_compatibility_selection(context,files)
    result=search(query,scope,context,files,limit=max(1,min(int(limit),50)),**options)
    if redirects:result['compatibility_redirects']=redirects
    return result
