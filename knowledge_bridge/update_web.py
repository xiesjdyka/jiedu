"""Drain existing web work before an authorized update handoff."""
import sys
from filelock import FileLock, Timeout
from .update_quiesce import UpdateAdmission


class WebUpdateCoordinator:
    def __init__(self, app, context, stop_workers, resume_workers):
        self.app, self.context = app, context
        self.admission = UpdateAdmission()
        self.stop_workers, self.resume_workers = stop_workers, resume_workers

    def busy(self):
        reasons = []
        if self.admission.active:
            reasons.append('正在完成界面请求')
        butler = getattr(self.app.state, 'local_butler', None)
        if butler:
            try:
                with butler.db() as db:
                    if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
                        reasons.append('管家问答或工具任务尚未完成')
            except Exception:
                reasons.append('暂时无法核验管家任务')
        objects = getattr(self.app.state, 'knowledge_object_jobs', None)
        if objects:
            with objects._lock:
                if objects._cancel:
                    reasons.append('资料导入或迁移尚未完成')
        models = getattr(self.app.state, 'butler_models', None)
        if models and models.worker and models.worker.is_alive():
            reasons.append('模型下载或验证尚未完成')
        index = sys.modules.get('knowledge_bridge.evidence_index')
        if index:
            with index._TASK_LOCK:
                if any(row.get('_thread') and row['_thread'].is_alive() for row in index._TASKS.values()):
                    reasons.append('索引任务尚未完成')
            thread = index._SYNC_THREAD
            if thread and thread.is_alive():
                reasons.append('增量索引尚未完成')
        if self.context.initialized:
            for path in (self.context.index_root('formal') / 'write.lock',
                         self.context.versions / 'formal-memory/writer.lock'):
                if not path.parent.exists():
                    continue
                try:
                    with FileLock(str(path), timeout=0):pass
                except Timeout:
                    reasons.append('正在安全写入资料或索引')
                except OSError:
                    reasons.append('暂时无法核验资料写入状态')
        return reasons

    def quiesce(self):
        if not self.admission.close() or self.busy():return False
        self.stop_workers()
        # A bounded join is never interpreted as proof that a writer finished.
        return not any(worker.is_alive() for worker in getattr(self.app.state, 'update_workers', []))

    def resume(self):
        self.resume_workers()
        self.admission.reopen()
