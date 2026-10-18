"""Durable exit warnings and conservative identity-bound plaintext cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


def exit_recovery_status(context) -> dict | None:
    path = context.state / "protection-exit.json"
    try:
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict) or value.get("workspace_id") != context.workspace_id:
            raise ValueError("invalid exit journal")
        if value.get("status") in {"completed", "rolled_back"}:
            return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        value = {"status": "unreadable"}
    return {
        "status": value.get("status", "unknown"),
        "transaction_id": value.get("transaction_id"),
        "plaintext_exposure_possible": True,
        "journal": str(path),
        "message": "退出保护尚未完成或回滚不完整，可能存在普通文件或临时明文；请保留恢复记录和旧密文，核对后再继续。",
    }


def file_identity(path: Path) -> list[int]:
    info = path.stat(follow_symlinks=False)
    if (not info.st_ino or not stat.S_ISREG(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise OSError("退出保护遇到非普通文件或重解析点")
    return [info.st_dev, info.st_ino]


def remove_verified_file(path: Path, identity: list[int], digest: str, size: int) -> str:
    """Delete only the opened, unchanged file, never a later pathname occupant.

    Windows denies write/delete sharing while hashing, then marks that very
    handle for deletion. Other systems conservatively retain a public copy:
    pathname stat/hash/unlink is not an atomic compare-and-delete primitive.
    """
    if not os.path.lexists(path):
        return "absent"
    if os.name != "nt":
        return "retained_no_identity_delete"
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    disposition = kernel.SetFileInformationByHandle
    disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    disposition.restype = wintypes.BOOL
    # GENERIC_READ | DELETE; only FILE_SHARE_READ; open the link itself.
    handle = create(str(path), 0x80010000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() in {2, 3}:
            return "absent"
        return "retained_busy"
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not info.st_ino or not stat.S_ISREG(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & 0x400 or
                [info.st_dev, info.st_ino] != identity):
            return "retained_identity_changed"
        actual = hashlib.sha256()
        length = 0
        while chunk := stream.read(1024 * 1024):
            actual.update(chunk)
            length += len(chunk)
        if actual.hexdigest() != digest or length != size:
            return "retained_content_changed"
        class Disposition(ctypes.Structure):
            _fields_ = [("delete_file", ctypes.c_ubyte)]
        flag = Disposition(1)
        if not disposition(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            return "retained_delete_failed"
    return "removed"
