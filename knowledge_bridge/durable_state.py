"""Fail closed on unreadable state; flushed atomic writes keep one validated copy."""
import json
import os
import time
import uuid
from pathlib import Path


class StateCorruptError(ValueError):
    pass


def read_object(path, default=None):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if path.with_name(path.name + '.last-good').exists():
            raise StateCorruptError(f'{path.name} 缺失；已停止采集以保护原进度，请核验备份后恢复')
        return {} if default is None else default
    except OSError as exc:
        raise StateCorruptError(f'{path.name} 无法读取；已停止采集以保护原进度') from exc
    try:
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('state must be an object')
        return value
    except (ValueError, UnicodeError) as exc:
        raise StateCorruptError(f'{path.name} 已损坏；已停止采集以保护原进度，请核验备份后恢复') from exc


def replace_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(min(.4, .05 * (attempt + 1)))
    finally:
        temporary.unlink(missing_ok=True)


def write_object(path, value, *, backup=True):
    path = Path(path)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if backup and path.exists():
        old = read_object(path)  # A damaged primary must never replace its good backup.
        previous = (json.dumps(old, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        replace_bytes(path.with_name(path.name + '.last-good'), previous)
    replace_bytes(path, raw)
    if backup and not path.with_name(path.name + '.last-good').exists():
        replace_bytes(path.with_name(path.name + '.last-good'), raw)
