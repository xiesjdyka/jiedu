import json,sqlite3,unittest
from unittest.mock import patch
from tests.test_hermes_history import HermesHistoryTests
from knowledge_bridge import hermes_session_adapter as ha,manual_extraction as me,session_manager as sm

class HermesNoticeTests(unittest.TestCase):
 setUp=HermesHistoryTests.setUp
 rows=HermesHistoryTests.rows
 def test_installed_notice_semantics_survive_history_and_are_not_extraction_input(self):
  db=sqlite3.connect(self.db);db.execute('alter table messages add column display_kind text');db.execute('update messages set display_kind="model_switch" where id<=11');db.execute('update messages set display_kind="hidden" where id=12');db.execute('update messages set display_metadata=? where id=13',(json.dumps({'model_only':True}),));db.commit();db.close()
  ha.history_control('start',self.db,owner_confirmed=True)
  for _ in range(30):
   ha.sync_once(self.db)
   if ha._load()['history']['status']=='complete':break
  rows=self.rows();self.assertEqual(123,len(rows));notices=[r for r in rows if r['role']=='system'];self.assertEqual(11,len(notices))
  for row in notices:self.assertEqual('model_switch',row['provenance']['source_display_kind']);self.assertIn(row['provenance']['source_role'],{'user','assistant'});self.assertFalse(row['provenance']['knowledge_eligible']);self.assertEqual('system_notice',row['provenance']['display_semantics'])
  authored=me._session_rows('hermes:history',{},self.archives);self.assertEqual(112,len(authored));self.assertTrue(all(r['role'] in {'user','assistant'} for r in authored));self.assertEqual('hidden',ha._load()['observations']['12']['display_decision']);self.assertEqual('model_only',ha._load()['observations']['13']['display_decision'])
  again=self.rows();ha.sync_once(self.db);self.assertEqual(again,self.rows());state=json.loads(self.sessions.read_text('utf-8'))['sessions']['hermes:history'];self.assertEqual(11,state['system_message_count']);self.assertEqual(112,state['user_message_count']+state['assistant_message_count'])
 def test_system_role_requires_verified_source_metadata(self):
  sm.session_start('fixture','hermes','desktop',memory_mode='full',state_path=self.sessions,archive_root=self.archives)
  with self.assertRaises(ValueError):sm.session_append('fixture','m1','system','synthetic','2026-01-01T00:00:00Z',state_path=self.sessions,archive_root=self.archives,materialize_archive=False)
 def test_previous_user_notice_observation_rechecks_as_system_and_extraction_excludes_old_revision(self):
  ha.history_control('start',self.db,owner_confirmed=True)
  for _ in range(30):
   ha.sync_once(self.db)
   if ha._load()['history']['status']=='complete':break
  before=self.rows();self.assertEqual('user',before[0]['role'])
  db=sqlite3.connect(self.db);db.execute('alter table messages add column display_kind text');db.execute('update messages set display_kind="model_switch" where id=1');db.commit();db.close()
  for _ in range(5):ha.sync_once(self.db)
  rows=self.rows();corrected=[r for r in rows if r['role']=='system'];self.assertEqual(1,len(corrected));self.assertEqual(before[0]['message_id'],corrected[0]['provenance']['supersedes_message_id']);self.assertEqual(124,len(me._session_rows('hermes:history',{},self.archives)));self.assertEqual(len(rows),len({r['message_id'] for r in rows}))

del HermesHistoryTests
