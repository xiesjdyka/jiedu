"""Bounded, version-pinned knowledge reading; no plaintext cache or new storage."""
import base64
import codecs
import hashlib
import hmac
import json
import os
import threading

from .storage import StorageAuthorization, StorageOperation

MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 65536


class ObjectReadError(ValueError):
    pass


class ObjectReader:
    def __init__(self, service):
        self.service = service
        self._cursor_key = os.urandom(32)
        self.slots = threading.BoundedSemaphore(4)

    @staticmethod
    def limits():
        return {"max_object_bytes": MAX_OBJECT_BYTES, "max_page_bytes": MAX_PAGE_BYTES,
                "text_encodings": ["utf-8", "utf-16-le (BOM)", "utf-16-be (BOM)"],
                "position_unit": "source_bytes", "max_concurrent_pages": 4}

    def _seal(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, "sha256")).decode()

    def _open(self, token):
        try:
            if len(token) > 4096: raise ValueError()
            raw = base64.urlsafe_b64decode(token)
            if not hmac.compare_digest(raw[-32:], hmac.digest(self._cursor_key, raw[:-32], "sha256")): raise ValueError()
            return json.loads(raw[:-32])
        except Exception:
            raise ObjectReadError("续读凭据已失效，请从对象开头重新读取") from None

    def _raw(self, row, offset, length, version):
        if row["policy"] == "protected":
            return self.service.protected.read_range(row["content_object"],
                StorageAuthorization.allow(StorageOperation.READ), offset=offset, length=length,
                version=version, max_size=MAX_OBJECT_BYTES)
        _, path = self.service._workspace_relative(row["logical_path"])
        def fingerprint(stat):
            return hashlib.sha256(str((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)).encode()).hexdigest()
        def identity(stat):
            # Windows Python may expose different ctime semantics for stat and
            # fstat. Compare ctime only within the same API across the read.
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno()); path_before = path.stat()
            current = fingerprint(path_before)
            if identity(before) != identity(path_before): raise ObjectReadError("打开期间对象发生变化，请重新读取")
            if version and current != version: raise ObjectReadError("对象版本已变化，请从头重新读取")
            if before.st_size > MAX_OBJECT_BYTES: raise ObjectReadError("对象超过 64 MiB 有界读取上限")
            if offset > before.st_size: raise ObjectReadError("读取位置超出对象末尾")
            handle.seek(offset); raw = handle.read(length)
            if fingerprint(os.fstat(handle.fileno())) != fingerprint(before) or fingerprint(path.stat()) != current:
                raise ObjectReadError("读取期间对象发生变化，请重新读取")
        return {"data": raw, "version": current, "total_bytes": before.st_size,
                "sha256": row.get("sha256"), "integrity": "file_identity_size_and_timestamps"}

    def page(self, row, *, mode="bytes", offset=0, cursor=None, version=None,
             binding="", page_bytes=MAX_PAGE_BYTES):
        if mode not in {"bytes", "text"}: raise ObjectReadError("请选择正文或原件字节读取")
        if not 1 <= page_bytes <= MAX_PAGE_BYTES: raise ObjectReadError("单页读取预算超出限制")
        expected = {"entry": row["entry_id"], "binding": binding, "mode": mode}
        continuation = self._open(cursor) if cursor else None
        if continuation:
            if any(continuation.get(k) != v for k, v in expected.items()):
                raise ObjectReadError("续读的对象、身份或授权会话已变化，请重新读取")
            offset = continuation["offset"]; version = continuation["version"]
        elif mode == "text" and offset:
            raise ObjectReadError("正文续读必须使用上一页的 next_cursor")
        if offset < 0: raise ObjectReadError("读取位置无效")
        if mode == "text" and not row.get("readable"):
            raise ObjectReadError("这是二进制原件，请读取关联派生正文，或使用有界字节入口")
        raw = self._raw(row, offset, page_bytes, version)
        chunk = raw.pop("data"); consumed = len(chunk); encoding = "base64"
        if mode == "text":
            encoding = continuation.get("encoding") if continuation else (
                "utf-16-le" if chunk.startswith(b"\xff\xfe") else "utf-16-be" if chunk.startswith(b"\xfe\xff") else "utf-8")
            decoder_name = ("utf-16" if encoding.startswith("utf-16") else "utf-8-sig") if offset == 0 else encoding
            try:
                decoder = codecs.getincrementaldecoder(decoder_name)(errors="strict")
                content = decoder.decode(chunk, final=offset+len(chunk) == raw["total_bytes"])
                consumed -= len(decoder.getstate()[0])
            except UnicodeError:
                raise ObjectReadError("正文编码无效或不受支持；请使用字节入口读取原件，不以替换字符冒充完整正文") from None
            if chunk and consumed == 0: raise ObjectReadError("单页预算不足以读取一个完整字符")
        else:
            content = base64.b64encode(chunk).decode("ascii")
        next_offset = offset + consumed if offset+consumed < raw["total_bytes"] else None
        next_cursor = self._seal({**expected, "offset": next_offset, "version": raw["version"],
                                  "encoding": encoding}) if next_offset is not None else None
        return {**self.service._public_protected_item(row), **raw, "content": content, "encoding": encoding,
                "mode": mode, "offset": offset, "offset_unit": "source_bytes", "returned_bytes": consumed,
                "returned_characters": len(content) if mode == "text" else None,
                "next_offset": next_offset, "next_cursor": next_cursor, "complete": next_offset is None,
                "read_limits": self.limits(),
                "source": {"entry_id": row["entry_id"], "logical_path": row["logical_path"],
                           "source_entry_id": row.get("source_entry_id"), "source_sha256": row.get("source_sha256"),
                           "derived_entry_ids": row.get("derived_entry_ids", []), "conversion": row.get("conversion")}}
