"""Publish complete attachment bytes without trusting or replacing existing evidence."""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path


def store_verified(folder: Path, name_for_digest, *, source=None, data=None):
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with (io.BytesIO(data) if data is not None else Path(source).open('rb')) as incoming:
            before = os.fstat(incoming.fileno()) if data is None else None
            digest = hashlib.sha256(); size = 0
            with tempfile.NamedTemporaryFile(dir=folder, prefix='.attachment-', delete=False) as outgoing:
                temporary = Path(outgoing.name)
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk); digest.update(chunk); size += len(chunk)
                outgoing.flush(); os.fsync(outgoing.fileno())
            if before is not None:
                after = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError('附件读取期间来源发生变化，请重试')
        sha = digest.hexdigest(); destination = folder / name_for_digest(sha)
        duplicate = False
        try:
            # Same-volume hard link atomically publishes completed bytes and never clobbers.
            os.link(temporary, destination)
        except FileExistsError:
            duplicate = True
        with destination.open('rb') as stored:
            actual = hashlib.sha256(); actual_size = 0
            while chunk := stored.read(1024 * 1024):
                actual.update(chunk); actual_size += len(chunk)
        if actual.hexdigest() != sha or actual_size != size:
            raise OSError('已归档附件SHA256校验失败；保留现有实体等待核查')
        return destination, sha, size, duplicate
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
