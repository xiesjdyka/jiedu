import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from knowledge_bridge.butler_runtime import LocalButlerRuntime
from knowledge_bridge.local_butler import LocalButler,machine_fields,status_only
from knowledge_bridge.workspace import initialize_workspace


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace');self.runtime=LocalButlerRuntime(self.ctx)
        self.runtime.root.mkdir(parents=True)
        for name in ('server','model'):(self.ctx.control/name).write_bytes(b'fixture')
        digest=hashlib.sha256(b'fixture').hexdigest()
        self.runtime.config_path.write_text(json.dumps({'enabled':True,'server_path':'server','model_path':'model','server_sha256':digest,'model_sha256':digest}))
        self.child=Mock();self.child.poll.return_value=None
        self.addCleanup(self.runtime.close)

    def run_start(self,client,cancel=None,clock=None):
        with patch('knowledge_bridge.butler_runtime.subprocess.Popen',return_value=self.child),patch.object(self.runtime,'_own_windows_child'),patch('knowledge_bridge.butler_runtime.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=client
            if clock:
                with patch('knowledge_bridge.butler_runtime.time.monotonic',side_effect=clock):self.runtime.start(cancel or threading.Event())
            else:self.runtime.start(cancel or threading.Event())

    def test_public_occupied_endpoint_cannot_be_ready(self):
        client=Mock();client.get.return_value=SimpleNamespace(status_code=200)
        with self.assertRaises(TimeoutError):self.run_start(client,clock=[0,1,121])
        self.assertFalse(self.runtime.ready);self.assertIsNone(self.runtime.process);self.child.terminate.assert_called_once()

    def test_loading_cancel_closes_child_and_can_retry(self):
        event=threading.Event();client=Mock()
        def response(*args,**kw):event.set();return SimpleNamespace(status_code=503)
        client.get.side_effect=response
        with self.assertRaises(InterruptedError):self.run_start(client,event)
        self.assertIsNone(self.runtime.process)
        client.get.side_effect=[SimpleNamespace(status_code=200),SimpleNamespace(status_code=401)]
        self.run_start(client);self.assertTrue(self.runtime.ready)

    def test_alive_not_ready_is_not_reused(self):
        old=Mock();old.poll.return_value=None;self.runtime.process=old
        client=Mock();client.get.side_effect=[SimpleNamespace(status_code=200),SimpleNamespace(status_code=401)]
        self.run_start(client);old.terminate.assert_called_once();self.assertTrue(self.runtime.ready)


class JobTests(unittest.TestCase):
    def test_explicit_machine_status_uses_snapshot_without_model_or_document_citation(self):
        question='Hermes最近同步的时间和证据索引待处理数是多少？请只用本轮运行快照回答并注明来源。'
        with tempfile.TemporaryDirectory() as root:
            ctx=initialize_workspace(Path(root)/'workspace');runtime=Mock();runtime.config.return_value={};runtime.status.return_value={"state":"stopped"};service=LocalButler(ctx,runtime=runtime,start_worker=False)
            sid=service.create_session()['id'];jid=service.submit(sid,question)['id'];service.cancel_event=threading.Event();service._update(jid,state='running')
            snapshot={'captured_at':'fixture-time','hermes':{'last_live_sync':'fixture-checkpoint'},'evidence_queue':{'pending':7,'last_result':'source_changed'}}
            with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            with patch.object(service,'snapshot',return_value=snapshot),patch.object(service,'_messages') as model_context:service._execute(job)
            answer=service.conversation(sid)['messages'][-1]
            self.assertIn('fixture-checkpoint',answer['content']);self.assertIn('待处理数：7',answer['content']);self.assertIn('[运行快照]',answer['content']);self.assertNotIn('[S',answer['content'])
            self.assertEqual(answer['sources'],[]);self.assertEqual(service.saved_snapshot(jid)['snapshot'],snapshot)
            runtime.start.assert_not_called();model_context.assert_not_called();service.close()

    def test_mixed_question_keeps_runtime_numbers_out_of_model_context(self):
        question='当前Hermes最近同步时间是多少，并解释资料中的同步机制？'
        fields=machine_fields(question);self.assertEqual(fields,['hermes']);self.assertFalse(status_only(question))
        with tempfile.TemporaryDirectory() as root:
            ctx=initialize_workspace(Path(root)/'workspace');runtime=Mock();runtime.config.return_value={};runtime.status.return_value={"state":"stopped"};runtime.config.return_value={};runtime.endpoint.return_value='http://127.0.0.1:1'
            service=LocalButler(ctx,runtime=runtime,start_worker=False);sid=service.create_session()['id'];jid=service.submit(sid,question)['id'];service.cancel_event=threading.Event()
            with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            snapshot={'captured_at':'fixture-time','hermes':{'last_live_sync':'private-runtime-marker'}}
            context={'user':{},'project':{},'state':snapshot,'related_knowledge':[{'id':'S1','source_path':'fixture.md','version':'v1','content':'fixture explanation'}],'permissions':{'read_only':True}}
            client=Mock();client.post.return_value.json.side_effect=[{'prompt':'fixture'},{'tokens':[1]}]
            with patch.object(service,'_context',return_value=(context,{})):messages,saved=service._messages(job,client,fields)
            self.assertEqual(saved['snapshot'],snapshot);self.assertNotIn('private-runtime-marker',json.dumps(messages));self.assertIn('fixture explanation',json.dumps(messages));service.close()

    def test_saved_snapshot_does_not_substitute_current_state(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=initialize_workspace(Path(root)/'workspace');service=LocalButler(ctx,runtime=Mock(config=Mock(return_value={}),status=Mock(return_value={"state":"stopped"})),start_worker=False)
            sid=service.create_session()['id'];jid=service.submit(sid,'fixture')['id']
            saved={'snapshot':{'captured_at':'fixture-time','evidence_queue':{'pending':7}},'budget':{'input_tokens':123},'retrieval':{'effective_mode':'keyword'}}
            with service.db() as db:db.execute('UPDATE jobs SET context=? WHERE id=?',(json.dumps(saved),jid))
            with patch.object(service,'snapshot',return_value={'evidence_queue':{'pending':99}}):self.assertEqual(service.saved_snapshot(jid),saved)
            service.close()

    def test_worker_failure_is_durable_without_assistant_success(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=initialize_workspace(Path(root)/'workspace');runtime=Mock();runtime.config.return_value={};runtime.status.return_value={"state":"stopped"}
            service=LocalButler(ctx,runtime=runtime,start_worker=False)
            with patch.object(service,'_execute',side_effect=RuntimeError('fixture failure')):
                service.worker.start();sid=service.create_session()['id'];service.submit(sid,'fixture')
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and service.conversation(sid)['jobs'][0]['state']!='failed':time.sleep(.02)
                result=service.conversation(sid);service.close()
            self.assertEqual(result['jobs'][0]['state'],'failed');self.assertEqual(len(result['messages']),1)
            reopened=LocalButler(ctx,runtime=runtime,start_worker=False)
            self.assertEqual(reopened.conversation(sid)['jobs'][0]['state'],'failed');reopened.close()

    def test_cancel_ack_is_immediate_and_rejects_inflight_result(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=initialize_workspace(Path(root)/'workspace');runtime=Mock();runtime.config.return_value={};runtime.status.return_value={"state":"stopped"};runtime.close.side_effect=lambda:time.sleep(.15)
            service=LocalButler(ctx,runtime=runtime,start_worker=False)
            entered=threading.Event()
            def execute(job):entered.set();service.cancel_event.wait(2);raise InterruptedError()
            with patch.object(service,'_execute',side_effect=execute):
                service.worker.start();sid=service.create_session()['id'];jid=service.submit(sid,'fixture')['id'];self.assertTrue(entered.wait(2))
                start=time.monotonic();self.assertEqual(service.cancel(jid)['state'],'canceled');self.assertLess(time.monotonic()-start,.1)
                time.sleep(.2);result=service.conversation(sid);service.close()
            self.assertEqual(result['jobs'][0]['state'],'canceled');self.assertEqual(len(result['messages']),1)

    def test_every_read_endpoint_requires_owner_gate(self):
        from fastapi import HTTPException
        from knowledge_bridge import web
        with patch.object(web,'_owner_read',side_effect=HTTPException(403,'fixture denied')):
            for fn,args in [(web.butler_status,(None,)),(web.butler_sessions,(None,)),(web.butler_conversation,('id',None)),(web.butler_source,('job','source',None)),(web.butler_snapshot,('job',None)),(web.butler_translate,('job',None))]:
                with self.assertRaises(HTTPException) as denied:fn(*args)
                self.assertEqual(denied.exception.status_code,403)
        self.assertIn('/api/butler',web._LOCAL_ADMIN_MUTATIONS)

class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.runtime=Mock();self.runtime.config.return_value={'idle_unload_seconds':15};self.runtime.status.return_value={'state':'stopped'}
        self.service=LocalButler(self.ctx,runtime=self.runtime,start_worker=False);self.service.cancel_event=threading.Event();self.addCleanup(self.service.close)

    def test_unicode_delta_and_reconnect_are_exact(self):
        service=self.service;sid=service.create_session()['id'];jid=service.submit(sid,'test')['id']
        job={'id':jid,'session':sid};service._delta(job,'你😀');service._delta(job,'你😀好')
        events=service.stream_events(sid,0,0);deltas=[json.loads(row['payload']) for row in events if row['kind']=='delta']
        self.assertEqual([d['offset'] for d in deltas],[0,2]);self.assertEqual(''.join(d['text'] for d in deltas),'你😀好')
        seq,snapshot=service.initial_stream(sid);self.assertEqual(snapshot['conversation']['jobs'][0]['preview'],'你😀好')
        self.assertEqual(service.stream_events(sid,seq,0),[])

    def test_deleted_active_session_rejects_late_result_and_restores_original(self):
        service=self.service;sid=service.create_session()['id'];jid=service.submit(sid,'原文😀')['id']
        original=service.conversation(sid)['messages']
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        service.trash(sid)
        with self.assertRaises(InterruptedError):service._complete(job,'late answer',{'sources':[]})
        with self.assertRaises(ValueError):service.submit(sid,'new')
        self.assertFalse(service.sessions());self.assertEqual(service.sessions(True)[0]['id'],sid)
        service.trash(sid,restore=True);result=service.conversation(sid)
        self.assertEqual(result['messages'],original);self.assertEqual(result['jobs'][0]['state'],'canceled')

    def test_idle_runtime_releases_owned_model(self):
        self.runtime.status.return_value={'state':'ready'}
        self.service.last_used=time.monotonic()-30
        self.runtime.close.side_effect=lambda:self.runtime.status.configure_mock(return_value={'state':'stopped'})
        self.service.worker.start()
        deadline=time.monotonic()+2
        while not self.runtime.close.called and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(self.runtime.close.called)

    def test_status_reply_uses_readable_new_labels(self):
        from knowledge_bridge.local_butler import status_answer
        value=status_answer({'evidence_queue':{'pending':2,'last_result':{'status':'source_changed'}}},['evidence_queue'])
        self.assertIn('查询时的运行状态',value);self.assertIn('资料在更新',value);self.assertNotIn('source_changed',value)

if __name__=='__main__':unittest.main()
