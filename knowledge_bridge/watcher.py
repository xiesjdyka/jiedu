import logging,os,threading,time
from pathlib import Path
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from .core import VAULT, is_allowed, rel, recover_index_writer, sync
from .workspace import active_workspace

INTERNAL_ROOTS={'.jiedu','.knowledge-bridge','.obsidian','.git','node_modules','__pycache__'}
SCOPE_QUIET_SECONDS=5.0
WATCH_ROOTS=('00_系统','01_当前状态','02_项目','03_知识','90_原始档案','99_收件箱')

def _normalized(path):
    value=os.path.normpath(os.path.abspath(str(path)))
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    elif value.startswith('\\\\?\\'):value=value[4:]
    return Path(value)

def _relative(path):return _normalized(path).relative_to(_normalized(VAULT))

class Handler(FileSystemEventHandler):
    def __init__(self):
        super().__init__();self._pending=set();self._scope_pending=set();self._lock=threading.Lock();self._last_change=0.0;self._scope_last_change=0.0
    def dispatch(self,event):
        try:
            return super().dispatch(event)
        except Exception:
            logging.getLogger(__name__).exception('File event failed; keeping formal paths for retry')
            # An unrelated archive queue failure must not kill the observer's
            # dispatch thread or drop a formal document change.
            for value in (event.src_path,getattr(event,'dest_path',None)):
                if not value:continue
                path=_normalized(value)
                try:parts=_relative(path).parts
                except ValueError:continue
                if path.suffix.lower()=='.md':
                    self._mark_scope(parts)
                    if is_allowed(path):self._add(path)
    def _mark_scope(self,parts):
        if not parts:return
        # Evidence manifests are published from the evidence generation's
        # already-parsed file state.  Re-parsing every long transcript here
        # would duplicate incremental work and periodically consume one core.
        scope=('formal' if parts[0] in {'00_系统','01_当前状态','02_项目','03_知识'} else
               'inbox' if parts[0]=='99_收件箱' else None)
        if scope:
            with self._lock:self._scope_pending.add(scope);self._scope_last_change=time.monotonic()
    def _add(self,path):
        try:relative=_relative(path).as_posix()
        except ValueError:return
        with self._lock:self._pending.add(relative);self._last_change=time.monotonic()
    def on_any_event(self,event):
        if event.event_type not in {"created", "modified", "deleted", "moved"}: return
        # Register evidence before the formal-index exclusions, including folder renames.
        evidence_paths=[]
        for value in (event.src_path, getattr(event,'dest_path',None)):
            if not value:continue
            try:relative=_relative(value)
            except ValueError:continue
            if relative.parts and relative.parts[0]=='90_原始档案':
                # Directory metadata is also modified by archive locks/manifests.
                # Real file changes have their own events; only membership changes
                # require a scope-wide reconciliation (plus the 5-minute fallback).
                if event.is_directory and event.event_type in {'created','deleted','moved'}:evidence_paths.append('*')
                elif relative.suffix.lower()=='.md':evidence_paths.append(relative.as_posix())
        if evidence_paths:
            from .evidence_updates import enqueue
            enqueue(evidence_paths,active_workspace())
        if event.is_directory:return
        p=Path(event.src_path)
        try: parts=_relative(p).parts
        except ValueError: return
        if parts and parts[0].casefold() in INTERNAL_ROOTS:return
        from .continuous_organization import ContinuousOrganizer
        intake=ContinuousOrganizer(active_workspace())
        intake.capture(Path(*parts).as_posix())
        if p.suffix.lower()=='.md':self._mark_scope(parts)
        if event.event_type=='moved' and getattr(event,'dest_path',None):
            destination=_normalized(event.dest_path)
            try:relative=_relative(destination)
            except ValueError:relative=None
            if relative is not None:
                intake.capture(relative.as_posix())
                if destination.suffix.lower()=='.md':self._mark_scope(relative.parts)
        if len(parts)>=2 and parts[0]=="90_原始档案" and parts[1] in {"ChatGPT","Hermes","Codex"}: return
        if p.suffix.lower()==".md" and (is_allowed(p) or event.event_type in {"deleted","moved"}):self._add(p)
        destination=Path(event.dest_path) if event.event_type=="moved" and getattr(event,"dest_path",None) else None
        if destination and destination.suffix.lower()==".md" and is_allowed(destination):self._add(destination)
    def flush(self,force=False):
        with self._lock:
            now=time.monotonic()
            paths=sorted(self._pending) if force or now-self._last_change>=.4 else []
            scopes=sorted(self._scope_pending) if force or now-self._scope_last_change>=SCOPE_QUIET_SECONDS else []
            if not paths and not scopes:return None
        result={'status':'scope_only'}
        if paths:
            result=sync(paths=paths,operation="watcher_incremental",lock_timeout=.15)
            if result.get("status")!="index_busy":
                with self._lock:self._pending.difference_update(paths)
            else:return result
        from .search_scopes import rebuild_scope_stable
        completed=[]
        for scope in scopes:
            snapshot=rebuild_scope_stable(scope,active_workspace())
            if snapshot.get('stable_snapshot'):
                completed.append(scope)
        with self._lock:self._scope_pending.difference_update(completed)
        return result|{'scope_manifests':completed}
def run(stop_event=None):
    # 启动只建立监听，不扫描或重建已有知识库。完整刷新必须由用户明确触发。
    from .process_runtime import publish_process
    if not active_workspace().initialized: publish_process("watch",ready=True)
    stop_event=stop_event or threading.Event()
    while not active_workspace().initialized:
        if stop_event.wait(1):return
    if stop_event.is_set():return
    recover_index_writer();handler=Handler();observer=Observer()
    # Subscribe only to user-facing knowledge roots.  Watching the whole vault
    # also receives every LanceDB, audit and runtime write before Handler can
    # reject it, which can saturate one core during an incremental generation.
    for name in WATCH_ROOTS:
        path=VAULT/name
        if path.is_dir():observer.schedule(handler,str(path),recursive=True)
    observer.start()
    publish_process("watch",ready=True)
    try:
        while not stop_event.wait(.5):
            try:handler.flush()
            except Exception:
                logging.getLogger(__name__).exception('Watcher update failed; pending paths retained for retry')
    except KeyboardInterrupt:pass
    finally:
        # Finish the current flush, stop admitting new file events, and only
        # then persist the last pending batch before acknowledging shutdown.
        observer.stop();observer.join()
        while True:
            result=handler.flush(force=True)
            if not result or result.get('status')!='index_busy':break
            # An existing writer keeps ownership. Preserve pending paths and
            # wait; the update helper may time out but must never force exit.
            time.sleep(.15)
    observer.join()
