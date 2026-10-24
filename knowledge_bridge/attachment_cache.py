"""Hash-bound attachment cache location; append-only state, original references retained."""
import hashlib,json,os,re,threading
from contextlib import contextmanager
from pathlib import Path
from filelock import FileLock
_locks={};_guard=threading.Lock()

@contextmanager
def cache_transaction(root):
    root.mkdir(parents=True,exist_ok=True);key=str(root.resolve()/'manifest.lock')
    with _guard:lock=_locks.setdefault(key,FileLock(key))
    with lock:yield

def identity(row):return (str(row.get('session_id')),str(row.get('message_id')),row.get('reference'))

def latest_rows(manifest):
    latest={}
    if manifest.is_file():
        with manifest.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():continue
                row=json.loads(line)
                if not isinstance(row,dict):raise ValueError('附件账本格式无效，不能静默丢弃记录')
                latest[identity(row)]=row
    return latest

def append_rows(root,rows):
    with cache_transaction(root):
        latest=latest_rows(root/'manifest.jsonl');changed=[]
        for row in rows:
            if latest.get(identity(row))!=row:changed.append(row);latest[identity(row)]=row
        if changed:
            with (root/'manifest.jsonl').open('a',encoding='utf-8',newline='\n') as stream:
                for row in changed:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                stream.flush();os.fsync(stream.fileno())
        return bool(changed)

def resolve(root,row,*,with_data=False):
    """Only exact hash filenames in the known current cache; never basename guesses."""
    digest=row.get('sha256');extension=row.get('extension') or Path(row.get('original_name') or '').suffix.lower()
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or not re.fullmatch(r'\.[a-zA-Z0-9]{1,12}',extension or ''):
        return (dict(row),None) if with_data else dict(row)
    candidate=root/(digest+extension.lower())
    if not candidate.is_file():return (dict(row),None) if with_data else dict(row)
    from .bounded_files import WindowsHandles
    handles=WindowsHandles()
    with handles.pin(root.resolve(),()) as parent,handles.open(parent/candidate.name) as handle:
        data=handles.read(handle,64*1024*1024)
        actual=hashlib.sha256(data).hexdigest()
    if actual!=digest:raise ValueError('当前附件缓存与历史SHA256不一致，拒绝修复')
    updated=dict(row,stored_path=str(candidate.resolve()),archived_path=str(candidate.resolve()),status='mapped')
    if str(row.get('stored_path') or '')!=str(candidate.resolve()):
        updated['cache_resolution']={'method':'exact_sha256_and_extension','previous_stored_path':row.get('stored_path'),'sha256_verified':True}
    return (updated,data) if with_data else updated

def session_rows(manifest,session_id,*,persist=False):
    root=manifest.parent
    with cache_transaction(root):
        rows=[resolve(root,row) for row in latest_rows(manifest).values() if row.get('session_id')==session_id]
        if persist:append_rows(root,rows)
        return rows
