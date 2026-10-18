"""In-process cooperative priority; background work yields only between batches."""
import threading
from contextlib import contextmanager

foreground=threading.Event()
_lock=threading.Lock()
_users=0

@contextmanager
def chatting():
    global _users
    with _lock:_users+=1;foreground.set()
    try:yield
    finally:
        with _lock:
            _users-=1
            if not _users:foreground.clear()
