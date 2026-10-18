import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.runtime=Mock(config=Mock(return_value={}),status=Mock(return_value={'state':'stopped'}))
        self.permissions=ClientPermissionService(ClientIdentityRegistry(self.ctx))
    def service(self):
        service=LocalButler(self.ctx,runtime=self.runtime,permissions=self.permissions,start_worker=False)
        self.addCleanup(service.close);return service
    def test_legacy_migration_preserves_fields_and_does_not_rearchive_restored_session(self):
        root=self.ctx.state/'local-butler';root.mkdir();path=root/'conversations.sqlite3'
        # Use the actual configured DB name so the migration is tested on the
        # previous public schema rather than freshly created rows.
        service=self.service();path=service.db_path;service.close()
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.execute("DELETE FROM butler_migrations WHERE name='session-archive-v1'")
                db.execute("INSERT INTO sessions(id,title,created,updated,deleted_at) VALUES('legacy','Original title','created-value','updated-value','deleted-value')")
                db.execute("INSERT INTO messages(session,role,content,sources,created,job) VALUES('legacy','user','original body','[]','message-time','old-job')")
        migrated=self.service();row=migrated.sessions(True)[0]
        self.assertEqual({key:row[key] for key in ['title','created','updated','deleted_at']},{'title':'Original title','created':'created-value','updated':'updated-value','deleted_at':'deleted-value'})
        self.assertEqual(row['archived_at'],'deleted-value');migrated.trash('legacy',restore=True)
        reopened=self.service();self.assertEqual(reopened.sessions(True),[]);self.assertEqual(reopened.conversation('legacy')['messages'][0]['content'],'original body')
    def test_delete_cancels_active_and_queued_then_blocks_late_outputs_and_keeps_other_data(self):
        service=self.service();sid=service.create_session()['id'];other=service.create_session()['id']
        jid=service.submit(sid,'first')['id']
        service.cancel_event=threading.Event();service.active=jid;service.active_provider='local'
        service._update(jid,state='running')
        with service.db() as db:
            job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            queued={**job,'id':'queued-fixture','state':'queued'}
            db.execute('INSERT INTO jobs('+','.join(queued)+') VALUES('+','.join('?' for _ in queued)+')',list(queued.values()))
        original=self.ctx.root/'90_原始档案'/'original.jsonl';original.write_text('original archive sentinel',encoding='utf-8')
        with self.assertRaises(ValueError):service.delete_session(sid,confirmed=False,expected_title='新对话')
        title=next(row['title'] for row in service.sessions() if row['id']==sid)
        with self.assertRaises(ValueError):service.delete_session(sid,confirmed=True,expected_title='wrong title')
        service.delete_session(sid,confirmed=True,expected_title=title)
        if service.cancel_thread:service.cancel_thread.join()
        service.cancel_event.clear()
        with self.assertRaises(InterruptedError):service._delta(job,'late chunk')
        with self.assertRaises(InterruptedError):service._complete(job,'late completion',{})
        service._update(jid,state='completed');service._provider_changed()
        self.assertTrue(service.delete_session(sid,confirmed=True,expected_title=title)['already_deleted'])
        with service.db() as db:
            for table in ['messages','jobs','stream_events']:self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE session=?',(sid,)).fetchone()[0],0)
        self.assertEqual(service.conversation(other)['messages'],[])
        self.assertEqual(original.read_text(),'original archive sentinel')
