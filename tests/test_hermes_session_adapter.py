import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import knowledge_bridge.hermes_session_adapter as ha
from knowledge_bridge.session_manager import session_status,_archive_path

class HermesSessionAdapterTests(unittest.TestCase):
 def test_four_messages_are_exact_and_idempotent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);db=root/'state.db';state=root/'adapter.json';sessions=root/'sessions.json';archives=root/'archives'
   con=sqlite3.connect(db);con.executescript('create table sessions(id text primary key,source text,started_at real); create table messages(id integer primary key,session_id text,role text,content text,timestamp real,token_count integer,platform_message_id text,display_metadata text,active integer); insert into sessions values("s1","desktop",1);')
   exact=['用户  原文\n**不改格式**','助手原文\r\n第二行','再问一次？','最终回答。']
   for i,(role,text) in enumerate(zip(['user','assistant','user','assistant'],exact),1):con.execute('insert into messages values(?,?,?,?,?,?,?,?,?)',(i,'s1',role,text,10+i,None,f'm{i}',json.dumps({'attachments':[]}),1))
   con.commit();con.close()
   with patch.object(ha,'STATE',state),patch('knowledge_bridge.session_archive.materialize'),patch('knowledge_bridge.live_acceptance.PATH',root/'live-acceptance.json'),patch('knowledge_bridge.hermes_session_adapter.session_start') as start,patch('knowledge_bridge.hermes_session_adapter.session_append') as append,patch('knowledge_bridge.hermes_session_adapter.update_session_metadata') as metadata:
    # Route to isolated Session Manager stores.
    from knowledge_bridge import session_manager as sm
    start.side_effect=lambda *a,**k:sm.session_start(*a,**k,state_path=sessions,archive_root=archives)
    append.side_effect=lambda *a,**k:sm.session_append(*a,**k,state_path=sessions,archive_root=archives)
    metadata.side_effect=lambda sid,**k:sm.update_session_metadata(sid,session_title=k.get('session_title'),source_last_activity_at=k.get('source_last_activity_at'),state_path=sessions)
    ha.initialize(db); # baseline old rows
    con=sqlite3.connect(db)
    for i,(role,text) in enumerate(zip(['user','assistant','user','assistant'],exact),5):con.execute('insert into messages values(?,?,?,?,?,?,?,?,?)',(i,'s2',role,text,20+i,None,f'm{i}',json.dumps({'attachments':[]}),1))
    con.execute('insert into sessions values("s2","desktop",2)');con.commit();con.close()
    first=ha.sync_once(db);second=ha.sync_once(db)
    rows=[json.loads(x) for x in _archive_path('hermes:s2',archives).read_text('utf-8').splitlines()]
    self.assertEqual(exact,[x['content'] for x in rows]);self.assertEqual(4,first['synced_messages']);self.assertEqual(4,second['synced_messages'])
    status=session_status('hermes:s2',state_path=sessions);self.assertEqual((2,2,2),(status['user_message_count'],status['assistant_message_count'],status['turn_count']))

 def test_inline_image_is_archived_with_manifest_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'真实图片.png';source.write_bytes(b'fixture-png');attachment_root=root/'attachment-store'
   with patch.object(ha,'ROOT',root):
    first=ha._attachments('hermes:s1','m1',3,[str(source)]);second=ha._attachments('hermes:s1','m1',3,[str(source)])
   self.assertEqual('mapped',first[0]['status']);self.assertEqual(3,first[0]['message_order']);self.assertEqual('image/png',first[0]['mime'])
   self.assertEqual(first[0]['sha256'],second[0]['sha256']);self.assertEqual(1,len(list((root/'data'/'sessions'/'attachments').glob('*.png'))))
   manifest=(root/'data'/'sessions'/'attachments'/'manifest.jsonl').read_text('utf-8').splitlines();self.assertEqual(1,len(manifest))
   self.assertEqual([str(source)],ha._inline_attachments('看图 @image:'+str(source)))
