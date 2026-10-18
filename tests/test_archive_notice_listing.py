import json,unittest
from unittest.mock import patch
from pathlib import Path
from tests.test_legacy_session_adoption import LegacySessionAdoptionTests as Fixture
from knowledge_bridge import session_archive as sa
from knowledge_bridge.archive_store import NormalizedMessage
from knowledge_bridge.inbox_capture import capture

class ArchiveNoticeListingTests(unittest.TestCase):
 setUp=Fixture.setUp
 legacy=Fixture.legacy
 def adopted(self,system=False):
  folder,rows=self.legacy('hermes:listing',1);item=self.store.ensure_session(source_key='hermes',source_session_id='hermes:listing',source_instance='desktop',title='fixture');provenance={'adapter':'hermes_sqlite_readonly','source_role':'user','display_semantics':'system_notice','knowledge_eligible':False} if system else {}
  self.store.append_normalized_message(item['session_key'],NormalizedMessage(message_id='current-0',role='system' if system else 'user',content=rows[0]['original_text'],timestamp=rows[0]['timestamp'],provenance=provenance),materialize=False);self.store.materialize_session(item['session_key']);return folder,item
 def test_system_only_session_is_listed_counted_filtered_and_cannot_save_clue(self):
  folder,item=self.adopted(True)
  with patch.object(sa,'active_workspace',return_value=self.context),patch.object(sa,'CANONICAL_ROOT',self.context.raw_archive),patch.object(sa,'SESSIONS_PATH',self.context.sessions_state):
   listed=sa.list_archive_summaries(client='hermes');self.assertEqual(1,listed['total']);self.assertEqual(1,listed['items'][0]['message_count']);self.assertEqual(1,listed['items'][0]['system_count']);self.assertEqual(0,listed['items'][0]['user_count']);self.assertEqual(1,sa.list_archive_summaries(client='hermes',status='complete')['total'])
   with self.assertRaises(ValueError):capture(self.context,item['session_key'],'current-0')
   with self.assertRaises(ValueError):capture(self.context,'hermes:listing','legacy-0')
 def test_latest_available_state_binds_once_and_summary_uses_visible_references(self):
  folder,item=self.adopted();rows=self.store.attachment_status(item['session_key']);bound=next(r for r in rows if r['source_message_id']=='current-0');self.assertEqual('available',bound['status'])
  ledger=folder/'03_元数据/attachments.jsonl';prefix=ledger.read_bytes();old=next(r for r in rows if r['source_message_id']=='legacy-0');stale=dict(old,source_message_id='current-0');stale.pop('status',None)
  with ledger.open('a',encoding='utf-8') as stream:stream.write(json.dumps(stale)+'\n')
  broken=ledger.read_bytes();self.store.materialize_session(item['session_key']);self.assertTrue(ledger.read_bytes().startswith(broken));latest=next(r for r in self.store.attachment_status(item['session_key']) if r['source_message_id']=='current-0');self.assertEqual('available',latest['status'])
  with patch.object(sa,'active_workspace',return_value=self.context):stats=sa._archive_summary_stats(folder/'03_元数据')
  self.assertEqual(1,stats['attachment_count']);self.assertEqual(1,stats['available_attachment_count']);self.assertEqual(0,stats['missing_attachment_count']);self.assertEqual(1,stats['attachment_references']['reference_count']);self.assertEqual(1,stats['attachment_entity_count'])
  before=ledger.read_bytes();self.store.materialize_session(item['session_key']);self.assertEqual(before,ledger.read_bytes());self.assertTrue(before.startswith(prefix))
del Fixture
