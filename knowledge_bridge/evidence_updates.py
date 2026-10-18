"""Durable watcher inbox, consumed by the console's existing embedding process."""
import json
import logging
import time
import uuid
from filelock import FileLock
from .workspace import active_workspace
from .index_gate import _atomic_json


def _read(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'pending': {}}
    try:
        state = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(state, dict) or not isinstance(state.get('pending', {}), dict):
            raise ValueError('Invalid evidence queue structure')
        return state
    except (ValueError, UnicodeError):
        # Callers hold the queue lock. Keep the exact damaged bytes and schedule
        # a hash reconciliation against source files rather than lose updates.
        backup = path.with_name(path.name + '.corrupt-' + uuid.uuid4().hex)
        backup.write_bytes(raw)
        now = time.time()
        state = {'pending': {'*': uuid.uuid4().hex}, 'first_change': now,
                 'last_change': now, 'recovered_from': backup.name}
        _atomic_json(path, state)
        logging.getLogger(__name__).warning('Recovered damaged evidence queue; backup: %s', backup.name)
        return state


def enqueue(paths=None, context=None):
    context = context or active_workspace()
    path = context.queues / 'evidence-updates.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock', timeout=5):
        state = _read(path)
        now = time.time()
        if not state.get('pending'): state['first_change'] = now
        for relative in paths or ['*']:
            state.setdefault('pending', {})[str(relative)] = uuid.uuid4().hex
        state['last_change'] = now
        _atomic_json(path, state)


def drain(context=None, *, force=False):
    from . import evidence_index
    context = context or active_workspace()
    path = context.queues / 'evidence-updates.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock', timeout=5):
        state = _read(path); pending = dict(state.get('pending', {}))
    if not pending: return {'status': 'empty'}
    if not force and time.time()<state.get('retry_at',0):return {'status':'retry_backoff','retry_at':state['retry_at']}
    if not force and time.time() - state.get('last_change', 0) < 3 and time.time() - state.get('first_change', 0) < 15:
        return {'status': 'debouncing'}
    if not evidence_index._descriptor(context).get('version'):
        return {'status': 'awaiting_initial_index'}
    try:
        result = evidence_index.sync(None if '*' in pending else list(pending), context,
                                     operation='evidence_watcher_incremental')
    except Exception as exc:
        result = {'status': 'failed', 'error': type(exc).__name__, 'message': str(exc)}
    with FileLock(str(path) + '.lock', timeout=5):
        state = _read(path)
        if result.get('status') in {'unchanged', 'incremental_synced'}:
            for key, token in pending.items():
                if state.get('pending', {}).get(key) == token: state['pending'].pop(key)
        state['last_attempt'] = time.time(); state['last_result'] = result
        state['retry_count']=min(5,int(state.get('retry_count',0))+1) if result.get('status') in {'source_changed','failed'} else 0
        state['retry_at']=time.time()+(min(60,5*2**state['retry_count']) if state['retry_count'] else 2 if result.get('status')=='budget_deferred' else 0)
        _atomic_json(path, state)
    return result


def run(stop_event, context=None):
    context = context or active_workspace(); next_reconcile = 0
    while not stop_event.is_set():
        try:
            if context.initialized:
                if time.monotonic() >= next_reconcile:
                    # Reconcile missed events using content hashes and vector reuse.
                    enqueue(context=context); next_reconcile = time.monotonic() + 300
                drain(context)
        except Exception:
            logging.getLogger(__name__).exception('Evidence update remains queued for retry')
        stop_event.wait(5)
