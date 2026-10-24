import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock
from knowledge_bridge.update_quiesce import (UpdateAdmission, request_shutdown,
    read_stop_request, acknowledge_shutdown, monitor_shutdown)
from knowledge_bridge.update_web import WebUpdateCoordinator


class CooperativeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.launch='a'*32;self.token='b'*32

    def test_old_launch_wrong_directory_and_expired_marker_cannot_stop_service(self):
        row=request_shutdown(self.root,self.token,self.launch,program=self.root/'program')
        self.assertIsNotNone(read_stop_request(self.root,self.launch,'console',program=self.root/'program'))
        self.assertIsNone(read_stop_request(self.root,'c'*32,'console',program=self.root/'program'))
        self.assertIsNone(read_stop_request(self.root,self.launch,'console',program=self.root/'other'))
        path=self.root/'runtime/update-stop.json'
        for changes in ({'expires_at_epoch':1},{'token':'../escape'},
                        {'local_data':str(self.root/'other')},{'roles':['native']},{'roles':'console'},
                        {'roles':[{}]}):
            path.write_text(json.dumps({**row,**changes}),'utf-8')
            self.assertIsNone(read_stop_request(self.root,self.launch,'console',program=self.root/'program'))
        for invalid in ('[]','null','[1,2,3]'):
            path.write_text(invalid,'utf-8')
            self.assertIsNone(read_stop_request(self.root,self.launch,'console',program=self.root/'program'))

    def test_ack_is_only_a_cooperative_receipt(self):
        row=request_shutdown(self.root,self.token,self.launch,program=self.root/'program')
        acknowledge_shutdown(self.root,row,'watch')
        proof=json.loads((self.root/f'runtime/update-stop-{self.token}-watch.json').read_text())
        self.assertTrue(proof['complete']);self.assertEqual(proof['token'],self.token)
        self.assertNotIn('terminated',proof)

    def test_unowned_process_monitor_performs_no_action(self):
        stopped=threading.Event();stopped.set();callback=Mock()
        self.assertIsNone(monitor_shutdown(self.root,None,'console',callback,stopped))
        callback.assert_not_called()

    def test_admission_closes_before_drain_and_reopens_on_cancel(self):
        admission=UpdateAdmission()
        self.assertTrue(admission.enter());self.assertFalse(admission.close())
        self.assertFalse(admission.enter());admission.leave()
        self.assertTrue(admission.close());admission.reopen();self.assertTrue(admission.enter())
        admission.leave()

    def test_quiesce_waits_for_requests_then_workers_without_forcing_them(self):
        worker=Mock();worker.is_alive.return_value=True
        app=SimpleNamespace(state=SimpleNamespace(update_workers=[worker]))
        stop=Mock();resume=Mock()
        coordinator=WebUpdateCoordinator(app,SimpleNamespace(initialized=False),stop,resume)
        coordinator.admission.enter()
        self.assertFalse(coordinator.quiesce());stop.assert_not_called()
        coordinator.admission.leave()
        self.assertFalse(coordinator.quiesce());stop.assert_called_once()
        worker.is_alive.return_value=False
        self.assertTrue(coordinator.quiesce())
        coordinator.resume();resume.assert_called_once();self.assertFalse(coordinator.admission.closed)

    def test_queued_import_is_not_dismissed_or_cancelled(self):
        jobs=SimpleNamespace(_lock=threading.RLock(),_cancel={'fixture':threading.Event()})
        app=SimpleNamespace(state=SimpleNamespace(knowledge_object_jobs=jobs,update_workers=[]))
        coordinator=WebUpdateCoordinator(app,SimpleNamespace(initialized=False),Mock(),Mock())
        self.assertTrue(coordinator.busy());self.assertFalse(coordinator.quiesce())
        self.assertFalse(jobs._cancel['fixture'].is_set())

    def test_resume_waits_for_stopped_worker_cleanup_then_replaces_once(self):
        # Compile only these two actual functions; importing the web application
        # would initialize unrelated services and is unnecessary for this race.
        path=Path(__file__).resolve().parents[1]/'knowledge_bridge/web.py'
        tree=ast.parse(path.read_text('utf-8-sig'))
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef)
                   and node.name in {'_start_workspace_workers','_stop_workspace_workers'}]
        self.assertEqual(len(functions),2)
        stop=threading.Event();started=threading.Event();cleanup=threading.Event()
        release=threading.Event();successor_started=threading.Event();join_entered=threading.Event()
        resumed=threading.Event();created=[];generations=[];errors=[]
        class TrackedThread(threading.Thread):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);created.append(self)
            def join(self,timeout=None):
                join_entered.set();return super().join(timeout)
        def worker():
            generations.append(len(generations)+1)
            if len(generations)==1:
                started.set()
                try:stop.wait()
                finally:
                    cleanup.set();release.wait()
            else:
                successor_started.set();stop.wait()
        app=SimpleNamespace(state=SimpleNamespace(update_workers=[]))
        namespace={'threading':SimpleNamespace(Thread=TrackedThread),
            '_workspace_workers_lock':threading.RLock(),
            'active_workspace':lambda:SimpleNamespace(initialized=True),
            '_workspace_workers':lambda app:[('synthetic-collector',worker,(),stop)],
            '_adapter_stop':stop, '_codex_adapter_stop':threading.Event(),
            '_doubao_adapter_stop':threading.Event(), '_governance_stop':threading.Event(),
            '_dsh_adapter_stop':threading.Event(), '_claude_adapter_stop':threading.Event(),
            '_cursor_adapter_stop':threading.Event(), '_four_adapter_stop':threading.Event(),
            '_evidence_stop':threading.Event()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),str(path),'exec'),namespace)
        start,halt=namespace['_start_workspace_workers'],namespace['_stop_workspace_workers']
        def resume():
            try:start(app)
            except BaseException as error:errors.append(error)
            finally:resumed.set()
        resume_thread=None
        try:
            start(app);self.assertTrue(started.wait(1))
            halt(app);self.assertTrue(cleanup.wait(1))
            old=created[0];self.assertTrue(old.is_alive())
            resume_thread=threading.Thread(target=resume,daemon=True);resume_thread.start()
            self.assertTrue(join_entered.wait(1))
            self.assertTrue(stop.is_set())
            self.assertFalse(resumed.is_set())
            self.assertFalse(successor_started.is_set())
            release.set();self.assertTrue(resumed.wait(1));self.assertTrue(successor_started.wait(1))
            self.assertFalse(old.is_alive());self.assertFalse(stop.is_set())
            self.assertEqual(len(created),2)
            start(app)  # Non-stopped live successor is reused, not duplicated.
            self.assertEqual(len(created),2)
            self.assertEqual(sum(thread.is_alive() for thread in created),1)
            self.assertFalse(errors)
        finally:
            release.set();stop.set()
            if resume_thread:resume_thread.join(1)
            for thread in created:thread.join(1)


if __name__=='__main__':unittest.main()
