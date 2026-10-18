import json,hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.archive_store import ArchiveStore,NormalizedMessage
from knowledge_bridge import session_archive as sa
from knowledge_bridge.legacy_session_adoption import resolve_session,resolve_message

class LegacySessionAdoptionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.context=initialize_workspace(Path(self.temp.name)/'workspace');self.store=ArchiveStore(self.context)
 def legacy(self,sid,count):
  folder=self.context.raw_archive/'Hermes'/('Legacy '+sid.replace(':','-'));metadata=folder/'03_元数据';metadata.mkdir(parents=True);rows=[]
  for i in range(count):
   text='synthetic original '+str(i);rows.append({'session_id':sid,'message_id':'legacy-'+str(i),'role':'user' if i%2==0 else 'assistant','timestamp':'2026-01-01T00:00:'+str(i%60).zfill(2)+'Z','original_text':text,'content_sha256':hashlib.sha256(text.encode()).hexdigest()})
  (metadata/'messages.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows),'utf-8');(metadata/'manifest.json').write_text(json.dumps({'session_id':sid,'client':'hermes','source':'desktop','session_title':'Owner title '+sid,'historical_field':'must survive','started_at':'2026-01-01T00:00:00Z'}),'utf-8')
  attachment=folder/'02_附件'/'same-name.txt';attachment.parent.mkdir();attachment.write_bytes(('synthetic attachment '+sid).encode());(metadata/'attachments.jsonl').write_text(json.dumps({'message_id':'legacy-0','source_message_id':'legacy-0','source_reference':'old-ref','original_name':'same-name.txt','stored_path':self.context.relative(attachment),'sha256':hashlib.sha256(attachment.read_bytes()).hexdigest(),'missing':False})+'\n','utf-8')
  return folder,rows
 def test_source_id_materialization_after_adoption_keeps_new_rows(self):
  from knowledge_bridge import session_manager as manager,original_archive_import
  context=self.context;sid='hermes:late-source';folder,old=self.legacy(sid,1)
  with patch.object(sa,'active_workspace',return_value=context),patch.object(sa,'CANONICAL_ROOT',context.raw_archive),patch.object(sa,'SESSIONS_PATH',context.sessions_state),patch.object(sa,'ARCHIVE_ROOT',context.raw_message_store),patch.object(sa,'ATTACHMENT_MANIFEST',context.state/'absent-attachments.jsonl'),patch.object(original_archive_import,'get_session',return_value=None):
   manager.session_start(sid,'hermes','desktop',memory_mode='full',state_path=context.sessions_state,archive_root=context.raw_message_store)
   manager.session_append(sid,'current-1','assistant','synthetic first current','2026-01-01T01:00:00Z',state_path=context.sessions_state,archive_root=context.raw_message_store,materialize_archive=False)
   first=sa.materialize(sid)
   manager.session_append(sid,'late-2','user','synthetic late source row','2026-01-01T01:00:01Z',state_path=context.sessions_state,archive_root=context.raw_message_store,materialize_archive=False)
   sa.materialize(sid)
   rows=[json.loads(line) for line in (folder/'03_元数据/messages.jsonl').read_text('utf-8').splitlines()]
   self.assertEqual({'legacy-0','current-1','late-2'},{r['message_id'] for r in rows})
   self.assertEqual(first['session_key'],resolve_session(context,sid))
 def test_197_and_1_old_rows_are_adopted_once_preserve_deleted_source_and_old_links(self):
  folders={};items={}
  for sid,count in [('hermes:old-197',197),('hermes:old-1',1)]:
   folder,rows=self.legacy(sid,count);before={p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()};item=self.store.ensure_session(source_key='hermes',source_session_id=sid,source_instance='desktop',title='New source title');items[sid]=item;folders[sid]=(folder,before)
   self.assertEqual(folder,Path(item['archive_path']));self.assertEqual('must survive',item['legacy_original_fields']['historical_field'])
   # Current source repeats one old message with a new stable ID; others are deleted from current source.
   old=rows[0];self.store.append_normalized_message(item['session_key'],NormalizedMessage(message_id='current-0',role=old['role'],content=old['original_text'],timestamp=old['timestamp'],provenance={'adapter':'hermes_sqlite_readonly','hermes_row_id':1}),materialize=False)
   self.store.append_normalized_message(item['session_key'],NormalizedMessage(message_id='new-live',role='assistant',content='synthetic new message',timestamp='2026-01-02T00:00:00Z'),materialize=False);self.store.materialize_session(item['session_key'])
   displayed=[json.loads(x) for x in (folder/'03_元数据/messages.jsonl').read_text('utf-8').splitlines()];self.assertEqual(count+1,len(displayed));self.assertEqual(count+1,len({(r['role'],r['original_text'],r['timestamp']) for r in displayed}));self.assertEqual(item['session_key'],resolve_session(self.context,sid));self.assertEqual('current-0',resolve_message(self.context,item['session_key'],'legacy-0'))
   snapshot=folder/item['legacy_adoption']['snapshot'] if False else self.context.root/item['legacy_adoption']['snapshot']
   for relative,digest in before.items():self.assertEqual(digest,hashlib.sha256((snapshot/relative).read_bytes()).hexdigest())
   again=ArchiveStore(self.context);again.ensure_session(source_key='hermes',source_session_id=sid,source_instance='desktop',title='Changed again');again.materialize_session(item['session_key']);self.assertEqual(displayed,[json.loads(x) for x in (folder/'03_元数据/messages.jsonl').read_text('utf-8').splitlines()])
  with patch.object(sa,'active_workspace',return_value=self.context),patch.object(sa,'CANONICAL_ROOT',self.context.raw_archive),patch.object(sa,'SESSIONS_PATH',self.context.sessions_state),patch.object(sa,'ARCHIVE_ROOT',self.context.raw_message_store):
   page=sa.materialize_page('hermes:old-197',limit=50,include_attachments=True);self.assertEqual(198,page['page']['total']);summaries=sa.list_archive_summaries(client='hermes',limit=100);self.assertEqual(2,summaries['total']);record=sa.read_message('legacy-0','hermes:old-197');self.assertEqual('current-0',record['message_id'])
   full=sa.materialize('hermes:old-197');self.assertEqual(items['hermes:old-197']['session_key'],full['session_id']);self.assertEqual(folders['hermes:old-197'][0],Path(full['archive_path']));self.assertEqual(b'synthetic attachment hermes:old-197',(Path(full['archive_path'])/'02_附件'/'same-name.txt').read_bytes())
  attachments=self.store.attachment_status(items['hermes:old-197']['session_key']);bound=next(x for x in attachments if x['source_message_id']=='current-0');self.assertEqual(hashlib.sha256(b'synthetic attachment hermes:old-197').hexdigest(),bound['sha256']);self.assertNotEqual(bound['sha256'],next(x for x in self.store.attachment_status(items['hermes:old-1']['session_key']) if x['source_message_id']=='current-0')['sha256'])
 def test_interrupted_adoption_restarts_from_retained_ids_without_overwriting_original(self):
  folder,rows=self.legacy('hermes:crash',5);before=(folder/'03_元数据/messages.jsonl').read_bytes();actual=self.store.append_normalized_message;calls=0
  def fail(*args,**kwargs):
   nonlocal calls
   calls+=1;result=actual(*args,**kwargs)
   if calls==3:raise OSError('synthetic crash after durable row')
   return result
  with patch.object(self.store,'append_normalized_message',side_effect=fail):
   with self.assertRaises(OSError):self.store.ensure_session(source_key='hermes',source_session_id='hermes:crash',source_instance='desktop',title='Crash')
  self.assertEqual(before,(folder/'03_元数据/messages.jsonl').read_bytes());store=ArchiveStore(self.context);item=store.ensure_session(source_key='hermes',source_session_id='hermes:crash',source_instance='desktop',title='Crash');store.materialize_session(item['session_key']);raw=[json.loads(x) for x in (self.context.raw_message_store/(item['session_key']+'.jsonl')).read_text('utf-8').splitlines()];self.assertEqual(5,len(raw));self.assertEqual(5,len({r['message_id'] for r in raw}))
 def test_instance_conflict_and_unproven_instance_stop_without_guessing(self):
  folder,rows=self.legacy('hermes:identity',1)
  self.store.ensure_session(source_key='hermes',source_session_id='hermes:identity',source_instance='desktop',title='same')
  with self.assertRaises(ValueError):self.store.ensure_session(source_key='hermes',source_session_id='hermes:identity',source_instance='telegram',title='different instance')
  folder,rows=self.legacy('hermes:unknown',1);path=folder/'03_元数据/manifest.json';manifest=json.loads(path.read_text());manifest.pop('source');path.write_text(json.dumps(manifest),'utf-8')
  with self.assertRaises(ValueError):self.store.ensure_session(source_key='hermes',source_session_id='hermes:unknown',source_instance='desktop',title='unknown')
 def test_old_notice_correction_withdrawal_and_attachment_alias_failure_recover(self):
  from knowledge_bridge import legacy_session_adoption as adoption,manual_extraction as extraction
  folder,rows=self.legacy('hermes:notice',1);original=(folder/'03_元数据/messages.jsonl').read_bytes();item=self.store.ensure_session(source_key='hermes',source_session_id='hermes:notice',source_instance='desktop',title='notice')
  self.store.append_normalized_message(item['session_key'],NormalizedMessage(message_id='notice-current',role='system',content=rows[0]['original_text'],timestamp=rows[0]['timestamp'],provenance={'adapter':'hermes_sqlite_readonly','source_role':'user','source_display_kind':'model_switch','display_semantics':'system_notice','knowledge_eligible':False,'source_message_id':'1'}),materialize=False)
  with patch.object(adoption,'_append_alias_attachments',side_effect=OSError('synthetic crash after durable alias')):
   with self.assertRaises(OSError):self.store.materialize_session(item['session_key'])
  self.store.materialize_session(item['session_key']);projected=json.loads((folder/'03_元数据/messages.jsonl').read_text('utf-8').strip());self.assertEqual('system',projected['role']);self.assertEqual([],extraction._session_rows('unused',{'canonical_messages':[projected]},self.context.raw_message_store));self.assertEqual('notice-current',adoption.resolve_message(self.context,item['session_key'],'legacy-0'))
  self.assertEqual(1,len([r for r in self.store.attachment_status(item['session_key']) if r['source_message_id']=='notice-current']))
  (folder/'03_元数据/message-latest.json').write_text(json.dumps({'latest_by_source_message':{'1':{'archive_message_id':None}}}),'utf-8');self.store.materialize_session(item['session_key']);fallback=json.loads((folder/'03_元数据/messages.jsonl').read_text('utf-8').strip());self.assertEqual('legacy-0',fallback['message_id']);self.assertEqual('system',fallback['role']);self.assertFalse(fallback['provenance']['knowledge_eligible']);self.assertEqual('legacy-0',adoption.resolve_message(self.context,item['session_key'],'legacy-0'))
  self.assertEqual(original,(self.context.root/item['legacy_adoption']['snapshot']/'03_元数据/messages.jsonl').read_bytes())

if __name__=='__main__':unittest.main()

