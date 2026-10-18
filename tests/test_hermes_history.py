"""Historical source positions must never overwrite the live high-water mark."""
import json,sqlite3,tempfile,unittest,time
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge import hermes_session_adapter as ha,session_manager as sm

class HermesHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.db=self.root/'source.db';self.sessions=self.root/'sessions.json';self.archives=self.root/'raw'
        db=sqlite3.connect(self.db);db.executescript('create table sessions(id text primary key,source text,started_at real,title text);create table messages(id integer primary key,session_id text,role text,content text,timestamp real,token_count integer,platform_message_id text,display_metadata text,active integer);insert into sessions values("history","desktop",1,"Fixture");')
        for i in range(1,126):db.execute('insert into messages values(?,?,?,?,?,?,?,?,?)',(i,'history','user' if i%2 else 'assistant',f'exact {i}\n  body',10+i,None,f'm{i}','{}',1))
        db.commit();db.close()
        patches=[patch.object(ha,'STATE',self.root/'adapter.json'),patch.object(ha,'list_sessions',return_value=[]),patch.object(ha,'session_start',side_effect=lambda *a,**k:sm.session_start(*a,**k,state_path=self.sessions,archive_root=self.archives)),patch.object(ha,'session_append',side_effect=lambda *a,**k:sm.session_append(*a,**k,state_path=self.sessions,archive_root=self.archives)),patch.object(ha,'update_session_metadata',side_effect=lambda sid,**k:sm.update_session_metadata(sid,state_path=self.sessions,**k)),patch.object(ha,'_flush_projection',side_effect=lambda state:state.get('pending_materializations',{}).clear()),patch('knowledge_bridge.live_acceptance.record_message')]
        for item in patches:item.start();self.addCleanup(item.stop)
        ha.initialize(self.db)
    def rows(self):
        p=sm._archive_path('hermes:history',self.archives)
        return [json.loads(x) for x in p.read_text('utf-8').splitlines()] if p.is_file() else []
    def test_explicit_history_is_bounded_pause_resume_and_live_is_independent(self):
        with self.assertRaises(PermissionError):ha.history_control('start',self.db)
        ha.history_control('start',self.db,owner_confirmed=True);ha.sync_once(self.db)
        state=ha._load();self.assertEqual(125,state['cursor']);self.assertLessEqual(state['history']['processed_rows'],50);self.assertGreater(state['history']['processed_rows'],0)
        old_position=state['history']['cursor'];ha.history_control('pause',self.db,owner_confirmed=True)
        db=sqlite3.connect(self.db);db.execute('insert into messages values(126,"history","assistant","new while paused",200,null,"m126","{}",1)');db.commit();db.close()
        ha.sync_once(self.db);self.assertEqual(old_position,ha._load()['history']['cursor']);self.assertEqual(126,ha._load()['cursor'])
        ha.history_control('resume',self.db,owner_confirmed=True)
        for _ in range(30):
            ha.sync_once(self.db)
            if ha._load()['history']['status']=='complete':break
        self.assertEqual('complete',ha._load()['history']['status']);self.assertEqual(126,ha._load()['cursor']);self.assertEqual(126,len(self.rows()));self.assertEqual(126,len({r['message_id'] for r in self.rows()}))
        before=self.rows();ha.sync_once(self.db);self.assertEqual(before,self.rows())
    def test_failed_history_append_retry_preserves_raw_evidence_and_cursor(self):
        ha.history_control('start',self.db,owner_confirmed=True);actual=ha._accept_row
        def failure(*args):actual(*args);raise OSError('injected after append')
        with patch.object(ha,'_accept_row',side_effect=failure):ha._history_step(ha._load(),self.db)
        self.assertEqual('error',ha._load()['history']['status']);self.assertEqual(0,ha._load()['history']['cursor']);self.assertEqual(125,ha._load()['cursor']);self.assertEqual(1,len(self.rows()))
        ha.history_control('resume',self.db,owner_confirmed=True);ha._history_step(ha._load(),self.db)
        self.assertEqual(len(self.rows()),len({r['message_id'] for r in self.rows()}));self.assertEqual('exact 1\n  body',self.rows()[0]['content'])
    def test_slow_query_still_makes_at_least_one_row_of_progress(self):
        ha.history_control('start',self.db,owner_confirmed=True);ha._history_step(ha._load(),self.db,batch_size=1)
        old=ha._load()['history']['cursor'];connect=ha._connect
        def slow(path):time.sleep(.55);return connect(path)
        with patch.object(ha,'_connect',side_effect=slow):ha._history_step(ha._load(),self.db)
        self.assertGreater(ha._load()['history']['cursor'],old);self.assertLessEqual(ha._load()['history']['cursor']-old,50)
    def test_orphan_source_rows_are_counted_without_inventing_a_session(self):
        db=sqlite3.connect(self.db);db.execute('insert into messages values(126,"missing-parent","user","orphan",200,null,"orphan","{}",1)');db.commit();db.close()
        ha.history_control('start',self.db,owner_confirmed=True)
        for _ in range(30):
            ha._history_step(ha._load(),self.db)
            if ha._load()['history']['status']=='complete':break
        state=ha._load()['history'];self.assertEqual('complete',state['status']);self.assertEqual(126,state['processed_rows']);self.assertEqual(1,state['orphan_rows']);self.assertEqual(125,len(self.rows()));self.assertEqual(0,state['missing_rows'])
    def test_historical_checkpoint_refuses_a_different_source_database(self):
        ha.history_control('start',self.db,owner_confirmed=True);ha._history_step(ha._load(),self.root/'different.db')
        self.assertEqual('error',ha._load()['history']['status']);self.assertEqual(0,ha._load()['history']['cursor']);self.assertEqual(125,ha._load()['cursor'])

if __name__=='__main__':unittest.main()
