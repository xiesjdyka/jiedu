import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.session_manager import session_start,session_append
from knowledge_bridge.memory_pipeline import review_candidate
from knowledge_bridge import manual_extraction as manual

class ManualExtractionTests(unittest.TestCase):
 def test_export_import_candidate_roundtrip_and_processed_gate(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);sessions=root/'sessions.json';archives=root/'archives';state=root/'manual.json';packages=root/'packages';results=root/'results';events=root/'candidates.jsonl';audit=root/'audit.jsonl'
   session_start('hermes:A','hermes','fixture','full',session_title='A',state_path=sessions,archive_root=archives)
   exact='原文  保留\n**Markdown不改写**'
   session_append('hermes:A','m1','user',exact,'2026-08-13T00:00:00+00:00',state_path=sessions,archive_root=archives)
   self.assertEqual('deferred',manual.session_state('hermes:A',state,sessions))
   manual.set_session_state('hermes:A','pending',state,sessions)
   package=manual.export_package(False,state,sessions,archives,packages);text=Path(package['path']).read_text(encoding='utf-8')
   self.assertIn(exact,text);self.assertIn('KB_MEMORY_CANDIDATES_V1',text)
   payload={'format':'KB_MEMORY_CANDIDATES_V1','package_id':package['package_id'],'candidates':[{'candidate_id':'external-1','package_id':package['package_id'],'type':'preference','proposed_content':'偏好逐字证据','target_layer':'00_系统','target_path':'00_系统/我的长期画像.md','source_session_ids':['hermes:A'],'source_message_ids':['m1'],'evidence':[{'session_id':'hermes:A','message_id':'m1','quote':'原文'}],'reason':'长期偏好','confidence':.9,'conflict_status':'none','conflict_with':[],'proposed_action':'update','supersedes':[],'notes':''}]}
   with patch('knowledge_bridge.memory_pipeline.CANDIDATE_EVENTS',events),patch('knowledge_bridge.memory_pipeline.AUDIT_EVENTS',audit):
    imported=manual.import_result(payload,state,sessions,archives,results);self.assertEqual(1,imported['imported']);self.assertFalse(imported['direct_write'])
    self.assertEqual(1,manual.status(state,sessions,archives)['pending_message_count'])
    self.assertEqual('待Owner审核',manual.status(state,sessions,archives)['current_node_status'])
    review_candidate(imported['candidates'][0]['candidate_id'],'reject');manual.update_review_state(imported['candidates'][0]['candidate_id'],state)
    done=manual.status(state,sessions,archives);self.assertEqual('已完成',done['current_node_status']);self.assertEqual(0,done['pending_message_count'])
    self.assertTrue(list(results.glob('KB_MEMORY_CANDIDATES_V1_*.json')))
    with self.assertRaises(ValueError):manual.import_result(payload|{'candidates':[payload['candidates'][0]|{'target_path':'../../escape.md'}]},state,sessions,archives,results)

if __name__=='__main__':unittest.main()
