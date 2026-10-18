"""Cooperative, launch-scoped update shutdown; never terminates a process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time

TOKEN = re.compile(r'[0-9a-f]{32}\Z')
ROLES = {'console', 'watch', 'native'}


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
