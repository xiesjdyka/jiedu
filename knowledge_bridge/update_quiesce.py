"""Cooperative, launch-scoped update shutdown; never terminates a process."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
import re
import threading
import time

TOKEN = re.compile(r'[0-9a-f]{32}\Z')
ROLES = {'console', 'watch', 'native'}
MAINTENANCE_SECONDS = 600
MAINTENANCE_MESSAGE = '界渡正在维护更新，本次请求未开始，请稍后重连。'
_IDENTITY_FIELDS = ('pid', 'created', 'executable')


def maintenance_path(program):
    program = Path(program).resolve()
    base = os.environ.get('LOCALAPPDATA')
    if os.name != 'nt' or not base:
        return None
    name = sha256(os.path.normcase(str(program)).encode('utf-8')).hexdigest()
    return Path(base).resolve() / 'Jiedu' / 'update-maintenance' / (name + '.json')


def maintenance_identity(pid):
    from .process_runtime import identity
    row = identity(pid)
    return {key: row[key] for key in _IDENTITY_FIELDS} if row else None


def read_maintenance(program):
    path = maintenance_path(program)
    if path is None:
        return None
    try:
        if any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
               for part in (path, path.parent, path.parent.parent)) or path.stat().st_size > 8192:
            return None
        row = json.loads(path.read_text('utf-8-sig'))
        if (row['format'] != 'jiedu-maintenance-v1' or row['program'] != str(Path(program).resolve())
                or not TOKEN.fullmatch(row['token'])):
            return None
        if row['phase'] == 'draining' and not 0 <= time.time() - row['created'] < MAINTENANCE_SECONDS:
            return None
        if row['phase'] not in {'draining', 'helper'}:
            return None
        owner = row['owner']
        if not isinstance(owner['pid'], int) or owner['pid'] <= 0:
            return None
        return row if maintenance_identity(owner['pid']) == owner else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def require_maintenance_open(program):
    if read_maintenance(program):
        raise RuntimeError(MAINTENANCE_MESSAGE)


def begin_maintenance(program, token):
    from filelock import FileLock, Timeout
    from .process_runtime import atomic_json
    path = maintenance_path(program)
    if path is None:
        return
    if not TOKEN.fullmatch(token):
        raise ValueError('更新维护标识无效')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(path.with_suffix('.lock')), timeout=0):
            require_maintenance_open(program)
            owner = maintenance_identity(os.getpid())
            if not owner:
                raise RuntimeError('无法核验更新协调进程的身份')
            atomic_json(path, {'format': 'jiedu-maintenance-v1', 'program': str(Path(program).resolve()),
                               'token': token, 'phase': 'draining', 'created': time.time(), 'owner': owner})
    except Timeout:
        raise RuntimeError(MAINTENANCE_MESSAGE) from None


def handoff_maintenance(program, token, pid):
    from filelock import FileLock
    from .process_runtime import atomic_json
    path = maintenance_path(program)
    if path is None:
        return
    with FileLock(str(path.with_suffix('.lock')), timeout=1):
        row = read_maintenance(program)
        owner = maintenance_identity(pid)
        if not row or row['token'] != token or row['owner']['pid'] != os.getpid() or not owner:
            raise RuntimeError('更新维护的交接进程身份无法核验')
        atomic_json(path, {**row, 'phase': 'helper', 'owner': owner})
    return owner


def claim_program_replacement(program, token, parent_pid):
    from filelock import FileLock
    from .process_runtime import atomic_json
    path = maintenance_path(program)
    with FileLock(str(path.with_suffix('.lock')), timeout=1):
        row = read_maintenance(program)
        owner = maintenance_identity(os.getpid())
        if (not row or row['phase'] != 'helper' or row['token'] != token
                or row['owner']['pid'] != parent_pid or not owner):
            raise RuntimeError('程序替换未获得更新助手的维护状态')
        atomic_json(path, {**row, 'owner': owner})


def end_maintenance(program, token, *, owner_pid=None):
    from filelock import FileLock, Timeout
    path = maintenance_path(program)
    if path is None:
        return
    try:
        with FileLock(str(path.with_suffix('.lock')), timeout=1):
            row = json.loads(path.read_text('utf-8'))
            if row['token'] == token and row['owner'] == maintenance_identity(owner_pid or os.getpid()):
                path.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError, Timeout):
        pass


def read_stop_request(local_data, launch_id, role, *, program=None):
    """A marker from an old launch cannot stop a newly started service."""
    if not TOKEN.fullmatch(str(launch_id or '')) or role not in ROLES:
        return None
    path = Path(local_data) / 'runtime/update-stop.json'
    try:
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            return None
        if path.stat().st_size > 8192:
            return None
        row = json.loads(path.read_text('utf-8-sig'))
        if not isinstance(row,dict) or not isinstance(row.get('roles'),list):return None
        expected_program=Path(program or Path(__file__).resolve().parents[1]).resolve()
        if (row.get('schema') != 1 or row.get('format') != 'jiedu-update-stop-v1'
                or Path(row.get('bridge_dir','')).resolve() != expected_program
                or Path(row.get('local_data','')).resolve() != Path(local_data).resolve()
                or row.get('launch_id') != launch_id or not set(row['roles']) <= ROLES or role not in row['roles']
                or not TOKEN.fullmatch(str(row.get('token', '')))):
            return None
        expires = row.get('expires_at_epoch')
        created = row.get('created_at_epoch')
        if (not isinstance(expires, (int, float)) or not isinstance(created, (int, float))
                or not 0 < expires-created <= 601 or created > time.time()+30 or expires < time.time()):
            return None
        if row.get('maintenance_managed') is True:
            maintenance = read_maintenance(expected_program)
            if not maintenance or maintenance['token'] != row['token']:
                return None
        return row
    except (OSError, ValueError, TypeError):
        return None


def request_shutdown(local_data, token, launch_id, *, program, roles=('console', 'watch', 'native')):
    from .process_runtime import atomic_json
    if not TOKEN.fullmatch(str(token)) or not TOKEN.fullmatch(str(launch_id)) or not roles or not set(roles) <= ROLES:
        raise ValueError('更新停机身份无效')
    row = {'format': 'jiedu-update-stop-v1', 'schema': 1, 'token': token, 'launch_id': launch_id,
           'bridge_dir': str(Path(program).resolve()), 'local_data': str(Path(local_data).resolve()),
           'roles': list(roles), 'created_at_epoch': time.time(), 'expires_at_epoch': time.time() + 600}
    maintenance = read_maintenance(program)
    if maintenance and maintenance['phase'] == 'helper' and maintenance['token'] == token:
        row['maintenance_managed'] = True
    atomic_json(Path(local_data) / 'runtime/update-stop.json', row)
    return row


def acknowledge_shutdown(local_data, request, role):
    from .process_runtime import atomic_json
    atomic_json(Path(local_data) / f"runtime/update-stop-{request['token']}-{role}.json",
                {'token': request['token'], 'launch_id': request['launch_id'],
                 'role': role, 'pid': os.getpid(), 'complete': True})


def monitor_shutdown(local_data, launch_id, role, on_shutdown, stopped):
    """Return the accepted request after requesting a normal service exit."""
    while not stopped.wait(.25):
        request = read_stop_request(local_data, launch_id, role)
        if request:
            on_shutdown()
            return request
    return None


class UpdateAdmission:
    """Count admitted requests and atomically close admission before draining."""
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = False
        self.active = 0

    def enter(self):
        with self.lock:
            if self.closed:
                return False
            self.active += 1
            return True

    def leave(self):
        with self.lock:
            self.active -= 1

    def close(self):
        with self.lock:
            self.closed = True
            return self.active == 0

    def reopen(self):
        with self.lock:
            self.closed = False
