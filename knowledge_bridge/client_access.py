"""Machine-local client identity and one authoritative permission decision service."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .workspace import WorkspaceContext, local_path


CLIENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
SECRET_PREFIX = "jiedu-client-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


@contextmanager
def _interprocess_lock(path: Path):
    """Serialize permission transactions across Console and MCP processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0");handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class KnowledgeScope(str, Enum):
    FORMAL = "formal"
    EVIDENCE = "evidence"
    INBOX = "inbox"
    WORKSPACE = "workspace"
    PROTECTED = "protected"


class ClientOperation(str, Enum):
    SEARCH = "search"
    READ = "read"
    CONTEXT = "context"
    SUBMIT_CANDIDATE = "submit_candidate"
    RAW_ARCHIVE = "raw_archive"
    SAVE_INBOX = "save_inbox"
    READ_PROTECTED = "read_protected"
    EXPORT_PROTECTED = "export_protected"
    DELETE = "delete"
    MIGRATE = "migrate"


class PolicyDecision(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


@dataclass(frozen=True)
class ClientIdentity:
    claimed_id: str
    authenticated_id: str | None
    internal_id: str | None
    status: str
    display_name: str | None = None
    credential_version: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.status == "authenticated" and bool(self.authenticated_id)


@dataclass(frozen=True)
class AccessDecision:
    outcome: PolicyDecision
    reason: str
    approval_token: str | None = None


@dataclass(frozen=True)
class WorkspaceAccessTarget:
    scope: KnowledgeScope
    operation: ClientOperation
    selector: dict
    relative_path: str | None = None


def _digest_request(kind: str, value: dict) -> str:
    canonical=json.dumps({"kind":kind,**value},ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_query_selector(query: str, limit: int) -> dict:
    normalized=" ".join(unicodedata.normalize("NFKC",str(query)).split())
    if not normalized:raise ValueError("查询不能为空")
    bounded=max(1,min(int(limit),50))
    return {"request_digest":_digest_request("evidence_search",{"query":normalized,"limit":bounded})}


def normalize_message_selector(message_id: str, session_id: str | None = None) -> dict:
    # Evidence identities are exact. Compatibility padding belongs only to the
    # legacy flat-file reader; approval must never alias 12 and 000012.
    message=str(message_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:~\-]{0,127}",message):
        raise ValueError("消息身份格式无效")
    selector={"message_id":message}
    if session_id:
        selector["session_id"]=normalize_session_id(session_id)
    return selector


def normalize_session_id(session_id: str) -> str:
    value=unicodedata.normalize("NFKC",str(session_id)).strip()
    if not value or len(value)>512 or any(ord(char)<32 for char in value):
        raise ValueError("会话身份格式无效")
    return value


def normalize_archive_cursor(cursor: str | int | None) -> int | None:
    if cursor is None or cursor=="":return None
    value=str(cursor).strip()
    if not value.isdigit():raise ValueError("归档游标无效")
    return int(value)


def normalize_session_list_selector(limit: int, cursor: str | int | None = None) -> dict:
    bounded=max(1,min(int(limit),100))
    offset=normalize_archive_cursor(cursor) or 0
    return {"request_digest":_digest_request("evidence_session_list",{"limit":bounded,"offset":offset})}


def resolve_document_target(context: WorkspaceContext, source_path: str,
                            chunk_id: str | None = None) -> WorkspaceAccessTarget:
    # Paths identify real filesystem entries, not normalized search terms.
    # NFKC changes Chinese punctuation (，：… etc.) in archived chat titles.
    raw=str(source_path).replace("\\","/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:",raw):
        raise ValueError("文档路径必须是工作区内相对路径")
    parts=raw.split("/")
    if any(part in {"",".",".."} for part in parts) or any("\x00" in part for part in parts):
        raise ValueError("文档路径无效")
    root=context.root.resolve();candidate=(root/Path(*parts)).resolve()
    try:relative=candidate.relative_to(root).as_posix()
    except ValueError as exc:raise ValueError("文档路径越界") from exc
    if not candidate.is_file():raise ValueError("文档不存在")
    first=Path(relative).parts[0].casefold()
    formal={name.casefold() for name in ("00_系统","01_当前状态","02_项目","03_知识")}
    if first in formal:scope=KnowledgeScope.FORMAL
    elif first=="90_原始档案".casefold():scope=KnowledgeScope.EVIDENCE
    elif first=="99_收件箱".casefold():scope=KnowledgeScope.INBOX
    elif first==".jiedu" or candidate.is_relative_to(context.protected_root.resolve()):
        raise ValueError("受保护对象不能通过普通文档接口读取")
    else:
        from .search_scopes import EXCLUDED_PARTS, is_hidden
        original=root
        for part in parts:
            original=original/part
            if (is_hidden(original) or original.is_symlink() or
                (hasattr(original, 'is_junction') and original.is_junction())):
                raise ValueError("不能通过链接读取本机资料")
        if candidate.suffix.casefold()!='.md' or any(part.startswith('.') or part.casefold() in EXCLUDED_PARTS for part in parts):
            raise ValueError("文档不属于可读知识范围")
        scope=KnowledgeScope.WORKSPACE
    selector={"document_path":relative,"chunk_id":str(chunk_id) if chunk_id else ""}
    return WorkspaceAccessTarget(scope,ClientOperation.READ,selector,relative)


class ClientAccessGate:
    """Authoritative target derivation in front of the sole permission service."""
    def __init__(self, permissions: "ClientPermissionService", identity: ClientIdentity):
        self.permissions=permissions;self.identity=identity;self.context=permissions.context

    def decide(self, target: WorkspaceAccessTarget, approval_token: str | None = None) -> AccessDecision:
        return self.permissions.authorize(self.identity,target.scope.value,target.operation.value,
                                          target.selector,approval_token)

    def search(self, query: str, limit: int, include_raw: bool,
               approval_token: str | None = None, *, scope: str = 'formal', options: dict | None = None) -> tuple[WorkspaceAccessTarget,AccessDecision]:
        if scope not in {'formal','evidence','inbox','workspace'}:raise ValueError('无效检索范围')
        scope=KnowledgeScope.EVIDENCE if include_raw else KnowledgeScope(scope)
        selector=normalize_query_selector(query,limit) if scope!=KnowledgeScope.FORMAL else {}
        if options:selector={'request_digest':_digest_request('scoped_search',{'query':normalize_query_selector(query,limit),'scope':scope.value,'options':options})}
        target=WorkspaceAccessTarget(scope,ClientOperation.SEARCH,selector)
        return target,self.decide(target,approval_token)

    def document(self, source_path: str, chunk_id: str | None = None,
                 approval_token: str | None = None) -> tuple[WorkspaceAccessTarget,AccessDecision]:
        target=resolve_document_target(self.context,source_path,chunk_id)
        return target,self.decide(target,approval_token)

    def raw_message(self, message_id: str, approval_token: str | None = None,
                    session_id: str | None = None) -> tuple[WorkspaceAccessTarget,AccessDecision]:
        target=WorkspaceAccessTarget(KnowledgeScope.EVIDENCE,ClientOperation.READ,
                                     normalize_message_selector(message_id,session_id))
        return target,self.decide(target,approval_token)

    def session_archive(self, session_id: str | None, limit: int,
                        cursor: str | int | None = None, include_attachments: bool = False,
                        approval_token: str | None = None) -> tuple[WorkspaceAccessTarget,AccessDecision]:
        position=normalize_archive_cursor(cursor)
        if session_id:
            target=WorkspaceAccessTarget(KnowledgeScope.EVIDENCE,ClientOperation.READ,
                                         {"session_id":normalize_session_id(session_id),
                                          "cursor":position,"limit":max(1,min(int(limit),50)),
                                          "include_attachments":bool(include_attachments)})
        else:
            target=WorkspaceAccessTarget(KnowledgeScope.EVIDENCE,ClientOperation.SEARCH,
                                         normalize_session_list_selector(limit,position))
        return target,self.decide(target,approval_token)


class ClientIdentityRegistry:
    """Per-machine, per-workspace verifier registry; plaintext secrets are never stored."""

    def __init__(self, context: WorkspaceContext, root: Path | None = None):
        if not context.initialized or not context.workspace_id:
            raise ValueError("客户端身份注册表需要已加载工作区")
        self.context = context
        self.root = (root or local_path("client-identities")).expanduser().resolve()
        self.workspace_root = self.root / context.workspace_id
        self.registry_path = self.workspace_root / "registry.json"
        self.audit_path = self.workspace_root / "access-audit.jsonl"
        self.verifier_key_path = self.root / "verifier.key"
        self._lock = threading.RLock()
        self._failures: dict[str, tuple[int, float]] = {}

    def _key(self) -> bytes:
        with self._lock:
            try:
                value = self.verifier_key_path.read_bytes()
                if len(value) == 32:
                    return value
            except OSError:
                pass
            self.verifier_key_path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_bytes(32)
            temporary = self.verifier_key_path.with_name(f".{self.verifier_key_path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(value)
            os.replace(temporary, self.verifier_key_path)
            return value

    def _digest(self, secret: str) -> str:
        return hmac.new(self._key(), secret.encode("utf-8"), hashlib.sha256).hexdigest()

    def _load(self) -> dict:
        try:
            value = json.loads(self.registry_path.read_text("utf-8"))
            if value.get("workspace_id") != self.context.workspace_id:
                return self._default()
            return value
        except (OSError, ValueError, AttributeError):
            return self._default()

    def _default(self) -> dict:
        return {"format_version": 1, "workspace_id": self.context.workspace_id, "clients": {}}

    def _save(self, value: dict) -> None:
        _atomic(self.registry_path, value)

    @staticmethod
    def _secret(internal_id: str) -> str:
        return f"{SECRET_PREFIX}.{internal_id}.{secrets.token_urlsafe(32)}"

    def prepare(self, claimed_id: str, display_name: str, client_type: str,
                transport: str) -> dict:
        """Create an in-memory pairing candidate without registering an identity."""
        claimed_id=claimed_id.strip().casefold();display_name=display_name.strip()
        if not CLIENT_NAME_RE.fullmatch(claimed_id) or not display_name:
            raise ValueError("客户端名称或身份格式无效")
        if transport not in {"stdio","localhost_http"}:
            raise ValueError("客户端传输方式无效")
        with self._lock:
            if any(row.get("claimed_id")==claimed_id and not row.get("revoked_at")
                   for row in self._load()["clients"].values()):
                raise ValueError("该客户端声明身份已经配对")
        internal_id=str(uuid.uuid4())
        return {"internal_id":internal_id,"claimed_id":claimed_id,
                "display_name":display_name,"client_type":str(client_type)[:80],
                "transport":transport,"secret":self._secret(internal_id),
                "created_at":_now()}

    def commit_prepared(self, prepared: dict) -> dict:
        """Commit a prepared verifier only after its client configuration is durable."""
        with self._lock:
            data=self._load();claimed_id=str(prepared["claimed_id"])
            if any(row.get("claimed_id")==claimed_id and not row.get("revoked_at")
                   for row in data["clients"].values()):
                raise ValueError("该客户端声明身份已经配对")
            internal_id=str(prepared["internal_id"])
            row={"internal_id":internal_id,"claimed_id":claimed_id,
                 "display_name":str(prepared["display_name"]),
                 "client_type":str(prepared["client_type"])[:80],
                 "transport":str(prepared["transport"]),
                 "secret_verifier":self._digest(str(prepared["secret"])),
                 "created_at":prepared.get("created_at") or _now(),"rotated_at":None,
                 "revoked_at":None,"last_used_at":None,"verified_at":None,
                 "permission_policy_ref":"policy-v1"}
            data["clients"][internal_id]=row;self._save(data)
        self.audit("client_created",claimed_id,internal_id,"success")
        return self.public_row(row)

    def rollback_new(self, internal_id: str) -> bool:
        """Remove only an unverified identity created by a failed pairing transaction."""
        with self._lock:
            data=self._load();row=data["clients"].get(internal_id)
            if not row or row.get("verified_at"):return False
            data["clients"].pop(internal_id,None);self._save(data)
        self.audit("client_pairing_rolled_back",row.get("claimed_id"),internal_id,"success")
        return True

    def create(self, claimed_id: str, display_name: str, client_type: str,
               transport: str) -> dict:
        prepared=self.prepare(claimed_id,display_name,client_type,transport)
        return self.commit_prepared(prepared)|{"secret":prepared["secret"],"secret_once":True}

    @staticmethod
    def public_row(row: dict) -> dict:
        return {key: row.get(key) for key in (
            "internal_id", "claimed_id", "display_name", "client_type", "transport",
                 "created_at", "rotated_at", "revoked_at", "last_used_at", "verified_at",
            "permission_policy_ref", "configuration_acknowledged_at", "last_auth_failure_at",
            "last_read_at", "last_read_scope", "last_read_tool",
        )} | {"status": "revoked" if row.get("revoked_at") else
               "read_verified" if row.get("last_read_at") else
               "authenticated" if row.get("verified_at") else
               "configuration_failed" if row.get('last_auth_failure_at') else
               "awaiting_configuration" if row.get('client_type') in {'custom','other','local_agent'} and not row.get('configuration_acknowledged_at') else "paired_not_seen"}

    def acknowledge_configuration(self,internal_id):
        with self._lock:
            data=self._load();row=data['clients'].get(internal_id)
            if not row or row.get('revoked_at'):raise ValueError('客户端不存在或已撤销')
            row['configuration_acknowledged_at']=_now();self._save(data)
            return self.public_row(row)

    def mark_protocol_seen(self,identity):
        with self._lock:
            if not self.identity_current(identity):return
            data=self._load();row=data['clients'][identity.internal_id]
            if row.get('verified_at') and not row.get('last_auth_failure_at'):return
            row['verified_at']=row.get('verified_at') or _now();row['last_used_at']=_now();row['last_auth_failure_at']=None;self._save(data)

    def mark_read_success(self,identity,scope,tool):
        """Persist only after a read operation has returned successfully."""
        with self._lock:
            if not self.identity_current(identity):return
            data=self._load();row=data['clients'][identity.internal_id];stamp=_now()
            row['verified_at']=row.get('verified_at') or stamp;row['last_used_at']=stamp
            row['last_read_at']=stamp;row['last_read_scope']=str(scope);row['last_read_tool']=str(tool)[:80]
            row['last_auth_failure_at']=None;self._save(data)

    def list_clients(self) -> list[dict]:
        return [self.public_row(row) for row in self._load()["clients"].values() if not row.get("deleted_at")]

    def authenticate(self, claimed_id: str | None, secret: str | None, *, mark_seen:bool=True) -> ClientIdentity:
        claimed = (claimed_id or "other").strip().casefold()
        if not CLIENT_NAME_RE.fullmatch(claimed):
            return ClientIdentity(claimed, None, None, "invalid")
        if not secret:
            return ClientIdentity(claimed, None, None, "legacy_unverified")
        with self._lock:
            failure_key=claimed+":"+hashlib.sha256(secret.encode()).hexdigest()
            count, until = self._failures.get(failure_key, (0, 0.0))
            if time.monotonic() < until:
                return ClientIdentity(claimed, None, None, "rate_limited")
            data = self._load()
            parts=secret.split(".")
            row=data["clients"].get(parts[1]) if len(parts)==3 and parts[0]==SECRET_PREFIX else None
            if row and row.get("claimed_id")!=claimed:row=None
            valid = bool(row and not row.get("revoked_at") and
                         hmac.compare_digest(str(row.get("secret_verifier") or ""), self._digest(secret)))
            if not valid:
                if row and not row.get('revoked_at'):
                    row['last_auth_failure_at']=_now();self._save(data)
                count += 1
                self._failures[failure_key] = (count, time.monotonic() + min(5.0, .05 * (2 ** min(count, 6))))
                self.audit("authentication", claimed, row.get("internal_id") if row else None,
                           "rejected", "invalid_credentials")
                return ClientIdentity(claimed, None, row.get("internal_id") if row else None,
                                      "revoked" if row and row.get("revoked_at") else "invalid")
            self._failures.pop(failure_key, None)
            stamp = _now()
            if mark_seen:
                row["last_used_at"] = stamp
                row["verified_at"] = row.get("verified_at") or stamp
                row['last_auth_failure_at']=None;self._save(data)
            self.audit("authentication", claimed, row["internal_id"], "success")
            return ClientIdentity(claimed, claimed, row["internal_id"], "authenticated", row["display_name"], row['secret_verifier'])

    def identity_current(self, identity: ClientIdentity) -> bool:
        row = self._load()['clients'].get(identity.internal_id or '', {})
        return bool(identity.authenticated and row and not row.get('revoked_at') and
                    identity.credential_version and hmac.compare_digest(
                        identity.credential_version, str(row.get('secret_verifier') or '')))

    def rotate(self, internal_id: str) -> dict:
        with self._lock:
            data = self._load(); row = data["clients"].get(internal_id)
            if not row or row.get("revoked_at"):
                raise ValueError("客户端不存在或已撤销")
            secret = self._secret(internal_id)
            row["secret_verifier"] = self._digest(secret); row["rotated_at"] = _now()
            row["verified_at"] = None
            self._failures.pop(row["claimed_id"],None)
            self._save(data); self.audit("client_rotated", row["claimed_id"], internal_id, "success")
            return self.public_row(row) | {"secret": secret, "secret_once": True}

    def prepare_rotation(self, internal_id: str) -> dict:
        """Prepare a verifier change without altering the registered identity."""
        with self._lock:
            data=self._load();row=data["clients"].get(internal_id)
            if not row or row.get("revoked_at"):raise ValueError("客户端不存在或已撤销")
            return {"internal_id":internal_id,"claimed_id":row["claimed_id"],
                    "display_name":row["display_name"],"client_type":row["client_type"],
                    "transport":row["transport"],"secret":self._secret(internal_id),
                    "expected_verifier":row.get("secret_verifier")}

    def commit_rotation(self, prepared: dict) -> dict:
        with self._lock:
            data=self._load();row=data["clients"].get(str(prepared["internal_id"]))
            if not row or row.get("revoked_at"):raise ValueError("客户端不存在或已撤销")
            if not hmac.compare_digest(str(row.get("secret_verifier") or ""),
                                       str(prepared.get("expected_verifier") or "")):
                raise ValueError("客户端凭据已发生变化，请重新预览")
            row["secret_verifier"]=self._digest(str(prepared["secret"]));row["rotated_at"]=_now()
            row["verified_at"]=None;self._save(data)
        self.audit("client_rotated",row["claimed_id"],row["internal_id"],"success")
        return self.public_row(row)

    def revoke(self, internal_id: str) -> dict:
        with self._lock:
            data = self._load(); row = data["clients"].get(internal_id)
            if not row: raise ValueError("客户端不存在")
            # The revocation marker is the authorization boundary. Keep the
            # non-reversible verifier as part of the identity record/audit trail;
            # authenticate() and identity_current() both fail closed on revoked_at.
            row["revoked_at"] = row.get("revoked_at") or _now()
            self._save(data); self.audit("client_revoked", row["claimed_id"], internal_id, "success")
            return self.public_row(row)

    def delete(self, internal_id: str) -> dict:
        """Hide a connection while retaining its revocation tombstone and archives."""
        with self._lock:
            data=self._load();row=data['clients'].get(internal_id)
            if not row:raise ValueError('客户端不存在')
            row['revoked_at']=row.get('revoked_at') or _now()
            row['deleted_at']=row.get('deleted_at') or _now()
            self._save(data)
        self.audit('client_deleted',row['claimed_id'],internal_id,'success')
        return {'deleted':True,'internal_id':internal_id}

    def credential_matches(self, internal_id: str, claimed_id: str, secret: str) -> bool:
        """Match exact configuration ownership, including a revoked tombstone."""
        row=self._load()['clients'].get(internal_id)
        return bool(row and row.get('claimed_id')==claimed_id and secret and
                    hmac.compare_digest(str(row.get('secret_verifier') or ''),self._digest(secret)))

    def audit(self, action: str, claimed_id: str | None, internal_id: str | None,
              result: str, failure_category: str | None = None, **extra) -> None:
        row = {"at": _now(), "workspace_id": self.context.workspace_id,
               "action": action, "claimed_identity": claimed_id,
               "authenticated_identity": internal_id, "result": result}
        if failure_category: row["failure_category"] = failure_category
        row.update(extra)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush(); os.fsync(handle.fileno())


class ClientPermissionService:
    def __init__(self, registry: ClientIdentityRegistry, *, clock=time.time):
        self.registry = registry
        self.context = registry.context
        self.path = registry.workspace_root / "permissions.json"
        self.lock_path = registry.workspace_root / "permissions.lock"
        self._clock = clock
        self._lock = threading.RLock()
        self._session_reads: dict[str, tuple[str, set[str]]] = {}
        self._read_grant_revision: dict[str, int] = {}
        self.on_butler_change=[]

    # Owner-only Butler policies share this authoritative file and process lock.
    # These methods are never model tools, paired-client endpoints or document instructions.
    @staticmethod
    def _butler_state(data):
        return data.setdefault('butler', {'revision':uuid.uuid4().hex,'policies':{},'grants':{},'requests':{}})

    def butler_policies(self):
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();missing='butler' not in data;state=self._butler_state(data)
            if missing:self._save(data)
            return {'revision':state['revision'],'policies':state['policies'],
                    'file_access':state.get('file_access',{'mode':'off'}),
                    'pending':[dict(value,id=key) for key,value in state['requests'].items() if value['state']=='pending']}

    def file_access(self):
        state=self._load().get('butler') or {}
        return {**state.get('file_access',{'mode':'off'}),'revision':state.get('revision','')}

    def set_file_access(self,mode,*,ordinary,destructive,computer):
        if mode not in {'off','workspace','computer'}:raise ValueError('文件范围无效')
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            # First activation is an explicit Owner action. Old per-operation
            # settings are retained for reversing the new range selection.
            if 'file_access' not in state:state['legacy_file_policies']={op:state['policies'].get(op,'deny') for op in ordinary|destructive|computer}
            state['revision']=uuid.uuid4().hex;state['file_access']={'mode':mode}
            for op in ordinary|destructive|computer:
                state['policies'][op]=('deny' if mode=='off' else 'ask' if op in destructive else 'allow')
            state['grants']={}
            for request in state['requests'].values():
                if request['state'] in {'pending','approved','consumed'}:request['state']='revoked'
            self._save(data)
        self.registry.audit('butler_file_range_updated',None,'owner-butler','success',mode=mode)
        for callback in self.on_butler_change:callback('*')
        return self.butler_policies()

    def butler_session_revision(self,session):
        with self._lock,_interprocess_lock(self.lock_path):
            return self._butler_state(self._load()).get('session_revisions',{}).get(session,0)

    def butler_authorization_current(self,receipt,operation):
        """Read an atomic persisted snapshot; safe inside an already-held commit lock."""
        state=self._load().get('butler') or {}
        return bool(receipt.get('authority')=='owner_butler_service' and
            receipt.get('revision')==state.get('revision') and
            state.get('policies',{}).get(operation,'deny')!='deny' and
            receipt.get('session_revision',0)==state.get('session_revisions',{}).get(receipt.get('session'),0))

    def set_butler_policy(self,operation,decision,*,known_operations):
        if operation not in known_operations:raise ValueError('管家操作不存在')
        decision=PolicyDecision(decision).value
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            state['revision']=uuid.uuid4().hex;state['policies'][operation]=decision
            state['grants']={}
            for request in state['requests'].values():
                if request['state'] in {'pending','approved'}:request['state']='revoked'
            self._save(data)
        self.registry.audit('butler_policy_updated',None,'owner-butler','success',operation=operation,decision=decision)
        for callback in self.on_butler_change:callback('*')
        return {'operation':operation,'decision':decision}

    def set_butler_group(self,operations,enabled,*,known_operations):
        if type(enabled) is not bool or not operations or len(set(operations))!=len(operations) or any(op not in known_operations for op in operations):raise ValueError('权限组无效')
        decision='ask' if enabled else 'deny'
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            state['revision']=uuid.uuid4().hex
            for operation in operations:state['policies'][operation]=decision
            state['grants']={}
            for request in state['requests'].values():
                if request['state'] in {'pending','approved'}:request['state']='revoked'
            self._save(data)
        self.registry.audit('butler_group_updated',None,'owner-butler','success',operations=operations,decision=decision)
        for callback in self.on_butler_change:callback('*')
        return self.butler_policies()

    def request_butler_operation(self,operation,arguments,*,session,job,provider_revision,known_operations):
        if operation not in known_operations:raise ValueError('管家操作不存在')
        binding=_digest_request('butler_operation',{'operation':operation,'arguments':arguments,
            'session':session,'job':job,'provider_revision':provider_revision})
        session_binding=_digest_request('butler_session',{'operation':operation,'session':session,'provider_revision':provider_revision})
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            decision=state['policies'].get(operation,'deny')
            destructive=operation in {'folder_delete','folder_move','folder_rename'} and state.get('file_access',{}).get('mode','off')!='off'
            if destructive:decision='ask'
            if decision=='deny':return {'decision':'deny','reason':'operation_forbidden'}
            grant=state['grants'].get(session_binding) or {}
            allowed=not destructive and (decision=='allow' or (isinstance(grant,dict) and grant.get('revision')==state['revision']))
            for key,row in state['requests'].items():
                if row['binding']==binding and row['revision']==state['revision'] and row['state'] in {'pending','approved','denied'}:
                    return {'decision':'ask','id':key,'state':row['state'],'revision':state['revision'],'binding':binding,'operation':operation}
            state['requests']={key:row for key,row in state['requests'].items()
                if row['expires_at']>self._clock() and row['state'] in {'pending','approved','consumed'}}
            if len(state['requests'])>=1000:raise ValueError('待处理管家权限请求过多')
            key=uuid.uuid4().hex
            state['requests'][key]={'operation':operation,'arguments':arguments,'session':session,'job':job,
                'provider_revision':provider_revision,'binding':binding,'session_binding':session_binding,
                'session_revision':state.get('session_revisions',{}).get(session,0),
                'revision':state['revision'],'state':'approved' if allowed else 'pending','created_at':_now(),'expires_at':self._clock()+900}
            self._save(data)
            return {'decision':'allow' if allowed else 'ask','id':key,'state':'approved' if allowed else 'pending','revision':state['revision'],'binding':binding,'operation':operation}

    def confirm_butler_operation(self,request_id,*,approved,duration='request'):
        if type(approved) is not bool or duration not in {'request','session','persistent'}:raise ValueError('确认选择无效')
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data);row=state['requests'].get(request_id)
            if not row or row['state']!='pending' or row['revision']!=state['revision'] or row['expires_at']<=self._clock():
                raise ValueError('确认请求已失效、撤销或处理')
            if row['operation'] in {'folder_delete','folder_move','folder_rename'} and state.get('file_access',{}).get('mode','off')!='off' and duration!='request':raise ValueError('删除、移动和重命名须逐次核对具体对象')
            row['state']='approved' if approved else 'denied';row['duration']=duration
            if approved and duration=='session':state['grants'][row['session_binding']]={'revision':state['revision'],'session':row['session']}
            if approved and duration=='persistent':state['policies'][row['operation']]='allow'
            self._save(data)
        self.registry.audit('butler_owner_confirmation',None,'owner-butler','success',request_id=request_id,approved=approved,duration=duration)
        return {'id':request_id,'state':row['state']}

    def butler_request_state(self,request_id):
        with self._lock,_interprocess_lock(self.lock_path):
            state=self._butler_state(self._load());row=state['requests'].get(request_id)
            if not row:return 'revoked'
            if row['expires_at']<=self._clock():return 'expired'
            return row['state'] if row['revision']==state['revision'] else 'revoked'

    def cancel_butler_job(self,job):
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            for row in state['requests'].values():
                if row['job']==job and row['state'] in {'pending','approved','consumed'}:row['state']='canceled'
            self._save(data)

    def revoke_butler_session(self,session):
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            epochs=state.setdefault('session_revisions',{});epochs[session]=epochs.get(session,0)+1
            state['grants']={key:row for key,row in state['grants'].items() if not isinstance(row,dict) or row.get('session')!=session}
            for row in state['requests'].values():
                if row['session']==session and row['state'] in {'pending','approved'}:row['state']='revoked'
            self._save(data)
        for callback in self.on_butler_change:callback(session)
        return {'session':session,'revoked':True}

    def purge_butler_session(self,session):
        with self._lock,_interprocess_lock(self.lock_path):
            data=self._load();state=self._butler_state(data)
            state['requests']={key:row for key,row in state['requests'].items() if row.get('session')!=session}
            state['grants']={key:row for key,row in state['grants'].items() if not isinstance(row,dict) or row.get('session')!=session}
            state.setdefault('session_revisions',{}).pop(session,None)
            self._save(data)

    @contextmanager
    def butler_operation(self,ticket,*,cancel=None,provider_current=lambda:True,commit_lock=True):
        """Single-use service tickets. Serialize writes; reads stay actively revocable."""
        def consume(state):
            if ticket.get('decision') not in {'ask','allow'} or ticket.get('revision')!=state['revision']:
                raise ValueError('管家操作权限已撤销')
            row=state['requests'].get(ticket.get('id'))
            if (not row or row['state']!='approved' or row['binding']!=ticket['binding'] or
                    row['operation']!=ticket.get('operation') or row['expires_at']<=self._clock() or
                    row.get('session_revision',0)!=state.get('session_revisions',{}).get(row['session'],0)):
                raise ValueError('管家操作尚未确认或确认已失效')
            row['state']='consumed'
        def validate_runtime():
            if cancel is not None and cancel.is_set():raise InterruptedError('请求已取消')
            if not provider_current():raise ValueError('模型配置已变更或撤销')
        if commit_lock:
            with self._lock,_interprocess_lock(self.lock_path):
                data=self._load();state=self._butler_state(data);consume(state);self._save(data)
                validate_runtime();yield validate_runtime;validate_runtime()
        else:
            with self._lock,_interprocess_lock(self.lock_path):
                data=self._load();state=self._butler_state(data);consume(state);self._save(data)
            def validate():
                with self._lock,_interprocess_lock(self.lock_path):
                    state=self._butler_state(self._load());row=state['requests'].get(ticket.get('id'))
                    if (state['revision']!=ticket['revision'] or not row or row['state']!='consumed' or
                        row.get('session_revision',0)!=state.get('session_revisions',{}).get(row['session'],0)):
                        raise ValueError('管家操作权限已撤销')
                validate_runtime()
            validate();yield validate;validate()

    def grant_session_read(self, internal_id: str, session_id: str, scopes: list[str]) -> None:
        if not session_id or not set(scopes) <= {'protected'}:
            raise ValueError('本次会话授权只用于受保护知识')
        if not any(r['internal_id'] == internal_id and r['status'] != 'revoked' for r in self.registry.list_clients()):
            raise ValueError('客户端不存在或已撤销')
        with self._lock:
            self._session_reads[internal_id] = (session_id, set(scopes))
            self._read_grant_revision[internal_id] = self._read_grant_revision.get(internal_id, 0) + 1
        self.registry.audit('session_read_granted' if scopes else 'session_read_revoked', None, internal_id, 'success')

    def session_read_scopes(self, internal_id: str, session_id: str | None) -> list[str]:
        with self._lock:
            grant = self._session_reads.get(internal_id)
            return sorted(grant[1] & {'protected'}) if grant and session_id and grant[0] == session_id else []

    def _ordinary_reads_are_open(self) -> bool:
        """Encryption-off workspaces expose ordinary reads to paired identities.

        This is deliberately derived from the persisted workspace setting so every
        independent STDIO process observes the same rule after restart.  Missing or
        malformed settings remain conservative.
        """
        try:
            value = json.loads(local_path("protection-control.json").read_text("utf-8"))
            workspace = (value.get("workspaces") or {}).get(self.context.workspace_id) or {}
            return workspace.get("protected_storage_enabled") is False
        except (OSError, ValueError, AttributeError):
            return False

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text("utf-8"))
            if value.get("workspace_id") == self.context.workspace_id:return value
        except (OSError, ValueError, AttributeError):pass
        return {"format_version":1,"workspace_id":self.context.workspace_id,"rules":{},"approvals":{}}

    def _save(self, value: dict) -> None:_atomic(self.path,value)

    @staticmethod
    def _rule_key(scope: KnowledgeScope, operation: ClientOperation, selector: dict | None) -> str:
        canonical=json.dumps(selector or {},ensure_ascii=False,sort_keys=True,separators=(",",":"))
        return f"{scope.value}:{operation.value}:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def set_policy(self, internal_id: str, scope: str, operation: str, decision: str,
                   selector: dict | None = None) -> dict:
        scope_value=KnowledgeScope(scope);operation_value=ClientOperation(operation);decision_value=PolicyDecision(decision)
        if operation_value in {ClientOperation.EXPORT_PROTECTED,ClientOperation.DELETE,ClientOperation.MIGRATE} and decision_value==PolicyDecision.ALLOW:
            raise ValueError("导出、删除和迁移不能设置为永久允许")
        if not any(row["internal_id"]==internal_id and row["status"]!="revoked" for row in self.registry.list_clients()):
            raise ValueError("客户端不存在或已撤销")
        with self._lock, _interprocess_lock(self.lock_path):
            data=self._load();rules=data["rules"].setdefault(internal_id,{})
            key=self._rule_key(scope_value,operation_value,selector)
            rules[key]={"scope":scope_value.value,"operation":operation_value.value,
                        "decision":decision_value.value,"selector":selector or {},"updated_at":_now()}
            self._save(data)
        self.registry.audit("policy_updated",None,internal_id,"success",scope=scope_value.value,
                            operation=operation_value.value,decision=decision_value.value)
        return rules[key]

    def policies(self, internal_id: str) -> list[dict]:
        return list(self._load()["rules"].get(internal_id,{}).values())

    def restore_recommended(self, internal_id: str) -> list[dict]:
        """Owner-requested reset of only generic ordinary rules for one client.

        More specific project/session/object rules and protected grants are retained.
        This is never called during migration or when reading an old client.
        """
        from .client_pairing import PairingTransactionManager
        if not any(row['internal_id']==internal_id and row['status']!='revoked'
                   for row in self.registry.list_clients()):
            raise ValueError('客户端不存在或已撤销')
        recommended=[(KnowledgeScope(scope),ClientOperation(operation),PolicyDecision(decision))
                     for scope,operation,decision in PairingTransactionManager._policies(None)
                     if scope!='protected']
        with self._lock, _interprocess_lock(self.lock_path):
            data=self._load();rules=data['rules'].setdefault(internal_id,{})
            for scope,operation,decision in recommended:
                rules[self._rule_key(scope,operation,None)]={
                    'scope':scope.value,'operation':operation.value,'decision':decision.value,
                    'selector':{},'updated_at':_now()}
            self._save(data)
        self.registry.audit('recommended_policies_restored',None,internal_id,'success')
        return [rules[self._rule_key(scope,operation,None)] for scope,operation,_ in recommended]

    def remove_client(self, internal_id: str) -> None:
        with self._lock, _interprocess_lock(self.lock_path):
            data=self._load();data["rules"].pop(internal_id,None)
            for key,row in list(data["approvals"].items()):
                if row.get("client_internal_id")==internal_id:data["approvals"].pop(key,None)
            self._save(data)

    def _policy(self, internal_id: str, scope: KnowledgeScope, operation: ClientOperation,
                selector: dict | None) -> PolicyDecision:
        return self._policy_from(self._load(),internal_id,scope,operation,selector)

    def _policy_from(self, data: dict, internal_id: str, scope: KnowledgeScope,
                     operation: ClientOperation, selector: dict | None) -> PolicyDecision:
        row=self._policy_row_from(data,internal_id,scope,operation,selector)
        return PolicyDecision(row['decision']) if row else PolicyDecision.DENY

    def _policy_row_from(self, data: dict, internal_id: str, scope: KnowledgeScope,
                         operation: ClientOperation, selector: dict | None) -> dict | None:
        rules=data["rules"].get(internal_id,{})
        exact=rules.get(self._rule_key(scope,operation,selector))
        generic=rules.get(self._rule_key(scope,operation,None))
        return exact or generic

    @staticmethod
    def _binding(identity: ClientIdentity, scope: KnowledgeScope, operation: ClientOperation,
                 selector: dict | None) -> str:
        value={"client":identity.internal_id,"scope":scope.value,"operation":operation.value,
               "selector":selector or {}}
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()

    def authorize(self, identity: ClientIdentity, scope: str, operation: str,
                  selector: dict | None = None, approval_token: str | None = None,
                  approval_ttl_seconds: int = 300, protection_session: str | None = None) -> AccessDecision:
        scope_value=KnowledgeScope(scope);operation_value=ClientOperation(operation)
        binding_selector = selector
        if scope == 'protected' and protection_session:
            binding_selector = {**(selector or {}), 'protection_session': hashlib.sha256(protection_session.encode()).hexdigest()}
        if not self.registry.identity_current(identity):
            self.registry.audit("access",identity.claimed_id,identity.internal_id,"rejected","unverified_identity",
                                scope=scope_value.value,operation=operation_value.value)
            return AccessDecision(PolicyDecision.DENY,"client_not_authenticated")
        self.registry.mark_protocol_seen(identity)
        ordinary_default=(self._ordinary_reads_are_open() and
                          scope in {'formal','evidence','inbox'} and
                          operation in {'search','read','context'})
        binding=self._binding(identity,scope_value,operation_value,binding_selector)
        with self._lock, _interprocess_lock(self.lock_path):
            data=self._load();now=self._clock()
            rule=self._policy_row_from(data,identity.internal_id or "",scope_value,operation_value,selector)
            decision=PolicyDecision(rule['decision']) if rule else (PolicyDecision.ALLOW if ordinary_default else PolicyDecision.DENY)
            if (scope in self.session_read_scopes(identity.internal_id or '', protection_session) and
                    scope == 'protected' and operation in {'search', 'read_protected'}):
                decision = PolicyDecision.ALLOW
            elif scope == 'protected' and decision == PolicyDecision.ALLOW:
                decision = PolicyDecision.DENY
            expired=[key for key,row in data["approvals"].items()
                     if row.get("expires_at",0)<=now]
            for key in expired:data["approvals"].pop(key,None)
            if expired:self._save(data)
            if decision==PolicyDecision.ASK and approval_token:
                key=hashlib.sha256(approval_token.encode()).hexdigest();row=data["approvals"].get(key)
                valid=bool(row and not row.get("consumed_at") and row.get("confirmed_at") and
                           row.get("expires_at",0)>now and hmac.compare_digest(row.get("binding",""),binding))
                if valid:
                    row["consumed_at"]=_now();self._save(data)
                    self.registry.audit("approval_consumed",identity.claimed_id,identity.internal_id,"success",
                                        scope=scope_value.value,operation=operation_value.value)
                    return AccessDecision(PolicyDecision.ALLOW,"one_time_owner_confirmation")
                self.registry.audit("approval_rejected",identity.claimed_id,identity.internal_id,"rejected","invalid_or_replayed",
                                    scope=scope_value.value,operation=operation_value.value)
                return AccessDecision(PolicyDecision.DENY,"approval_invalid_or_replayed")
            if decision==PolicyDecision.ASK:
                confirmed=next((row for row in data["approvals"].values()
                                if not row.get("consumed_at") and row.get("confirmed_at") and
                                row.get("expires_at",0)>now and
                                hmac.compare_digest(row.get("binding",""),binding)),None)
                if confirmed:
                    confirmed["consumed_at"]=_now();self._save(data)
                    self.registry.audit("approval_consumed",identity.claimed_id,identity.internal_id,"success",
                                        scope=scope_value.value,operation=operation_value.value)
                    return AccessDecision(PolicyDecision.ALLOW,"one_time_owner_confirmation")
                existing=next((row for row in data["approvals"].values()
                               if not row.get("consumed_at") and not row.get("confirmed_at") and
                               row.get("expires_at",0)>now and
                               hmac.compare_digest(row.get("binding",""),binding)),None)
                if existing:
                    existing["expires_at"]=now+approval_ttl_seconds;self._save(data)
                    self.registry.audit("approval_refreshed",identity.claimed_id,identity.internal_id,"pending",
                                        scope=scope_value.value,operation=operation_value.value)
                    return AccessDecision(PolicyDecision.ASK,"owner_confirmation_required")
                token=secrets.token_urlsafe(32);key=hashlib.sha256(token.encode()).hexdigest()
                data["approvals"][key]={"binding":binding,"client_internal_id":identity.internal_id,
                    "claimed_id":identity.claimed_id,"scope":scope_value.value,"operation":operation_value.value,
                    "selector":selector or {},"created_at":_now(),"expires_at":now+approval_ttl_seconds,
                    "ttl_seconds":max(1,min(int(approval_ttl_seconds),300)),
                    "confirmed_at":None,"consumed_at":None}
                self._save(data)
                self.registry.audit("approval_requested",identity.claimed_id,identity.internal_id,"pending",
                                    scope=scope_value.value,operation=operation_value.value)
                return AccessDecision(PolicyDecision.ASK,"owner_confirmation_required",token)
        self.registry.audit("access",identity.claimed_id,identity.internal_id,
                            "success" if decision==PolicyDecision.ALLOW else "rejected",
                            None if decision==PolicyDecision.ALLOW else "policy_denied",
                            scope=scope_value.value,operation=operation_value.value)
        return AccessDecision(decision,"ordinary_read_open" if decision==PolicyDecision.ALLOW and ordinary_default and rule is None else
                              "policy_allow" if decision==PolicyDecision.ALLOW else "policy_deny")

    def pending(self) -> list[dict]:
        now=self._clock();rows=[]
        for digest,row in self._load()["approvals"].items():
            if not row.get("confirmed_at") and not row.get("consumed_at") and row.get("expires_at",0)>now:
                rows.append({"approval_id":digest,"approval_token_hint":digest[:8],
                             **{key:row.get(key) for key in ("client_internal_id","claimed_id","scope","operation","selector","created_at")}})
        return rows

    def confirmed(self) -> list[dict]:
        now=self._clock();rows=[]
        for row in self._load()["approvals"].values():
            if row.get("confirmed_at") and not row.get("consumed_at") and row.get("expires_at",0)>now:
                rows.append({"state":"confirmed_waiting_consumption",
                             **{key:row.get(key) for key in ("client_internal_id","claimed_id","scope","operation","selector","created_at","confirmed_at")}})
        return rows

    def confirm(self, approval_id: str) -> dict:
        key=approval_id if re.fullmatch(r"[0-9a-f]{64}",approval_id or "") else ""
        with self._lock, _interprocess_lock(self.lock_path):
            data=self._load();row=data["approvals"].get(key)
            if not row or row.get("consumed_at") or row.get("expires_at",0)<=self._clock():
                raise ValueError("确认请求不存在或已过期")
            if row.get("confirmed_at"):raise ValueError("确认请求已经处理")
            ttl=max(1,min(int(row.get("ttl_seconds") or 300),300))
            row["confirmed_at"]=_now();row["expires_at"]=self._clock()+ttl;self._save(data)
        self.registry.audit("approval_confirmed",row.get("claimed_id"),row.get("client_internal_id"),"success",
                            scope=row.get("scope"),operation=row.get("operation"))
        return {"confirmed":True,"state":"confirmed_waiting_consumption",
                "expires_in_seconds":ttl,"scope":row.get("scope"),"operation":row.get("operation")}


def parse_authorization(value: str | None) -> tuple[str | None, str | None]:
    if not value or not value.startswith("JieduClient "):
        return None, None
    payload=value[len("JieduClient "):]
    claimed,separator,secret=payload.partition(":")
    return (claimed,secret) if separator else (None,None)


def stdio_identity(context: WorkspaceContext, root: Path | None = None,
                   environ: dict | None = None) -> ClientIdentity:
    values=environ or os.environ
    return ClientIdentityRegistry(context,root).authenticate(
        values.get("KNOWLEDGE_BRIDGE_CLIENT_ID"),values.get("KNOWLEDGE_BRIDGE_CLIENT_SECRET"))
