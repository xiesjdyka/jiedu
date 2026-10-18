"""Cross-process writer gate for one workspace's derived formal index."""
from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from .workspace import WorkspaceContext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


class IndexBusyError(RuntimeError):
    def __init__(self, owner: dict | None = None):
        super().__init__("正式索引正在由另一个进程更新")
        self.owner = owner or {}

    def public_status(self) -> dict:
        return {
            "status": "index_busy",
            "error": "index_busy",
            "message": str(self),
            "owner": {key: self.owner.get(key) for key in
                      ("workspace_id", "operation", "pid", "started_at")
                      if self.owner.get(key) is not None},
        }


class IndexWriteGate:
    """OS-backed lock plus a non-sensitive diagnostic owner record.

    The operating system releases the actual lock when a process exits.  A
    leftover owner record is treated as stale only after this lock is acquired,
    so a live writer is never displaced based on PID age or timestamps.
    """

    def __init__(self, context: WorkspaceContext):
        if not context.initialized:
            raise ValueError("尚未选择或初始化工作区")
        self.context = context
        self.root = context.index_root("formal")
        self.lock_path = self.root / "write.lock"
        self.owner_path = self.root / "write-owner.json"
        self.audit_path = context.audit / "index-lock-recovery.jsonl"
        self._thread_lock = threading.RLock()

    def _owner(self) -> dict:
        try:
            value = json.loads(self.owner_path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _record_stale_owner(self, owner: dict) -> None:
        if not owner:
            return
        row = {
            "at": _now(), "action": "stale_lock_recovered",
            "workspace_id": self.context.workspace_id,
            "previous_operation": owner.get("operation"),
            "previous_pid": owner.get("pid"),
            "previous_started_at": owner.get("started_at"),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @contextmanager
    def hold(self, operation: str, timeout: float = 8.0):
        self.root.mkdir(parents=True, exist_ok=True)
        file_lock = FileLock(str(self.lock_path))
        try:
            file_lock.acquire(timeout=max(0.0, float(timeout)))
        except Timeout as exc:
            raise IndexBusyError(self._owner()) from exc
        token = uuid.uuid4().hex
        with self._thread_lock:
            previous = self._owner()
            if previous:
                self._record_stale_owner(previous)
            owner = {
                "version": 1,
                "token": token,
                "workspace_id": self.context.workspace_id,
                "operation": str(operation)[:80],
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": _now(),
            }
            _atomic_json(self.owner_path, owner)
        try:
            yield owner
        finally:
            with self._thread_lock:
                current = self._owner()
                if current.get("token") == token:
                    self.owner_path.unlink(missing_ok=True)
            file_lock.release()

    def recover_if_abandoned(self) -> bool:
        """Recover diagnostics only when the OS proves no live writer owns it."""
        if not self.owner_path.exists():
            return False
        try:
            # Windows may release a terminated process' kernel lock a few
            # scheduling ticks after wait() observes exit.  A short bounded
            # acquisition handles that race without ever displacing a live
            # writer: the OS lock remains the authority.
            with self.hold("startup_recovery", timeout=.5):
                pass
            return True
        except IndexBusyError:
            return False
