import json,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_codex_source_adapter import CodexSourceAdapterTests as Fixture,_message,_line
from knowledge_bridge import session_archive as sa

class ArchiveTitleSyncTests(unittest.TestCase):
 setUp=Fixture.setUp
 tearDown=Fixture.tearDown
 _source=Fixture._source
 _index=Fixture._index
 _adapter=Fixture._adapter
 _authorize=Fixture._authorize
 def archive(self,folder_name='旧目录 [12345678]',title='当前正式标题',key='stable-key'):
  folder=self.workspace.raw_archive/'ChatGPT'/folder_name;metadata=folder/'03_元数据';metadata.mkdir(parents=True)
  manifest={'session_key':key,'source_session_id':'source-one','source_key':'codex','title':title,'session_title':'旧记录标题','sync_status':'synced'}
  (metadata/'manifest.json').write_text(json.dumps(manifest),'utf-8')
  (metadata/'messages.jsonl').write_text(json.dumps({'message_id':'m1','session_id':key,'role':'user','original_text':'synthetic unchanged body'})+'\n','utf-8')
  (metadata/'attachments.jsonl').write_text('','utf-8');(folder/'01_对话').mkdir();(folder/'01_对话/conversation.md').write_text('# old heading\n\nsynthetic unchanged body','utf-8')
  return folder,metadata
 def scope(self):
  from contextlib import ExitStack
  stack=ExitStack()
  for name,value in [('active_workspace',lambda:self.workspace),('CANONICAL_ROOT',self.workspace.raw_archive),('SESSIONS_PATH',self.workspace.sessions_state),('DISPLAY_CACHE',self.workspace.cache/'archive-display.json')]:stack.enter_context(patch.object(sa,name,value))
  return stack
 def cache(self,key,**values):
  self.workspace.cache.mkdir(parents=True,exist_ok=True);(self.workspace.cache/'archive-display.json').write_text(json.dumps({'sessions':{key:values}}),'utf-8')
 def test_current_manifest_beats_old_directory_and_collision_suffix_without_evidence_changes(self):
  folder,metadata=self.archive();before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
  with self.scope():
   self.assertEqual('当前正式标题',sa._canonical_item(metadata)['session_title'])
   self.assertEqual('当前正式标题',sa._manifest_summary(metadata/'manifest.json')['session_title'])
   self.assertEqual('当前正式标题',sa.list_archive_summaries()['items'][0]['display_title'])
   self.assertEqual('当前正式标题',sa.materialize_page('stable-key')['display_title'])
  for p,digest in before.items():self.assertEqual(digest,hashlib.sha256(p.read_bytes()).hexdigest())
 def test_stale_display_cache_cannot_override_title_in_list_or_page(self):
  folder,metadata=self.archive();self.cache('stable-key',display_title='推定｜旧首条消息',original_title='旧标题',title_origin='content_inferred',related_projects=['保留关联'],relation_origin='content_inferred')
  cache_before=(self.workspace.cache/'archive-display.json').read_bytes()
  with self.scope():
   for item in [sa.list_archive_summaries()['items'][0],sa.materialize_page('stable-key')]:
    self.assertEqual('当前正式标题',item['display_title']);self.assertEqual('当前正式标题',item['session_title']);self.assertEqual(['保留关联'],item['related_projects'])
  self.assertEqual(cache_before,(self.workspace.cache/'archive-display.json').read_bytes())
 def test_cache_with_current_original_but_old_recorded_display_is_rejected(self):
  self.archive();self.cache('stable-key',display_title='旧显示名',original_title='当前正式标题',title_origin='recorded')
  with self.scope():self.assertEqual('当前正式标题',sa.list_archive_summaries()['items'][0]['display_title'])
 def test_valid_generic_inferred_cache_stays_usable(self):
  self.archive(title='未命名对话');self.cache('stable-key',display_title='推定｜合成主题',original_title='未命名对话',title_origin='content_inferred')
  with self.scope():self.assertEqual('推定｜合成主题',sa.materialize_page('stable-key')['display_title'])
 def test_legacy_import_directory_rename_compatibility_is_preserved(self):
  folder,metadata=self.archive(folder_name='主人改名的旧导入');p=metadata/'manifest.json';manifest=json.loads(p.read_text('utf-8'));manifest.pop('session_key');manifest['session_id']='legacy-import';p.write_text(json.dumps(manifest),'utf-8')
  with self.scope():
   self.assertEqual(folder.name,sa._manifest_summary(p)['session_title']);self.assertEqual(folder.name,sa._canonical_item(metadata)['session_title'])
 def test_rename_only_sync_and_repeat_preserve_messages_attachment_ledger_and_cursor(self):
  sid='rename-only';(self.codex/'attachments/rename.bin').write_bytes(b'synthetic unchanged attachment');source=self._source(sid,[_message('m01','user','unchanged synthetic message',attachments=[{'type':'input_file','path':'rename.bin','name':'rename.bin'}])]);self._index([(sid,'旧名称')]);adapter=self._adapter();self._authorize(adapter,sid);adapter.sync()
  initial=next(iter(adapter.store._registry()['sessions'].values()));key=initial['session_key'];folder=self.workspace.root/initial['relative_path'];metadata=folder/'03_元数据';before=(metadata/'messages.jsonl').read_bytes();attachments_before=(metadata/'attachments.jsonl').read_bytes();source_before=source.read_bytes();offset=adapter.store.session(key)['last_sync_position']['confirmed_offset']
  self.cache(key,display_title='旧名称',original_title='旧名称',title_origin='recorded');self._index([(sid,'当前侧栏名称')]);adapter.sync();adapter.sync()
  current=adapter.store.session(key);metadata=Path(current['archive_path'])/'03_元数据'
  self.assertEqual(key,current['session_key']);self.assertEqual(offset,current['last_sync_position']['confirmed_offset']);self.assertEqual(before,(metadata/'messages.jsonl').read_bytes());self.assertEqual(attachments_before,(metadata/'attachments.jsonl').read_bytes());self.assertEqual(source_before,source.read_bytes());self.assertEqual(2,len(current['title_history']))
  with self.scope():self.assertEqual('当前侧栏名称',sa.list_archive_summaries()['items'][0]['display_title'])
 def test_existing_index_thread_name_title_and_latest_row_are_supported(self):
  adapter=self._adapter();p=self.codex/'session_index.jsonl';p.write_bytes(_line({'id':'one','thread_name':'旧标题'})+_line({'id':'two','title':'兼容标题字段'})+_line({'id':'one','thread_name':'当前标题'}))
  self.assertEqual({'one':'当前标题','two':'兼容标题字段'},adapter._titles())
 def test_no_title_and_empty_or_invalid_cache_has_explicit_fallback(self):
  for display in [{},{'display_title':' ','original_title':None,'title_origin':'fallback'},{'display_title':'旧名','title_origin':'recorded'}]:
   item=sa._apply_display_title({'session_title':None},display)
   self.assertEqual('未命名对话',item['display_title']);self.assertIsNone(item['original_title']);self.assertEqual('fallback',item['title_origin'])

del Fixture
