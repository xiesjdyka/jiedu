import json,tempfile,unittest,hashlib
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.legacy_layout import normalize
from knowledge_bridge import session_archive

class LegacyLayoutTests(unittest.TestCase):
 def test_explicit_image_id_mapping_never_uses_similar_filename(self):
  from knowledge_bridge.legacy_layout import preserve_image_relations
  with tempfile.TemporaryDirectory() as tmp:
   ctx=initialize_workspace(Path(tmp)/'vault');target=ctx.raw_archive/'ChatGPT/旧档'
   (target/'02_附件').mkdir(parents=True);(target/'03_元数据').mkdir()
   (target/'02_附件/IMG_1.png').write_bytes(b'image');(target/'02_附件/IMG_2-preview.png').write_bytes(b'preview')
   preserve_image_relations(ctx,target,{'image_ids':['IMG_1','IMG_2'],'messages':[{'message_id':'m','image_refs':['IMG_1','IMG_2']}]})
   rows=[json.loads(x) for x in (target/'03_元数据/attachments.jsonl').read_text('utf-8').splitlines()]
   self.assertEqual(['available','missing'],[r['status'] for r in rows])
   self.assertEqual(hashlib.sha256(b'image').hexdigest(),rows[0]['sha256'])

 def test_live_materialize_replays_new_messages_after_existing_archive(self):
  with tempfile.TemporaryDirectory() as tmp:
   ctx=initialize_workspace(Path(tmp)/'vault');sid='hermes:fixture-live';rawroot=ctx.state/'session-archives'
   rawroot.mkdir();raw=session_archive._archive_path(sid,rawroot)
   ctx.sessions_state.write_text(json.dumps({'sessions':{sid:{'client_id':'hermes','source':'hermes','session_title':'原题'}}}),'utf-8')
   rows=[{'message_id':'m1','role':'user','original_text':'first'}]
   raw.write_text(json.dumps(rows[0])+'\n','utf-8')
   with patch.object(session_archive,'active_workspace',return_value=ctx),patch.object(session_archive,'CANONICAL_ROOT',ctx.raw_archive),patch.object(session_archive,'SESSIONS_PATH',ctx.sessions_state),patch.object(session_archive,'ARCHIVE_ROOT',rawroot),patch.object(session_archive,'ATTACHMENT_MANIFEST',ctx.state/'none'),patch('knowledge_bridge.original_archive_import.get_session',return_value=None):
    first=session_archive.materialize(sid)
    with raw.open('a',encoding='utf-8') as f:f.write(json.dumps({'message_id':'m2','role':'assistant','original_text':'second'})+'\n')
    second=session_archive.materialize(sid);self.assertEqual(first['session_key'],second['session_key']);self.assertEqual(2,second['message_count'])
    repeat=session_archive.materialize(sid);self.assertEqual(2,repeat['message_count'])
    self.assertEqual(['m1','m2'],[r['message_id'] for r in session_archive._find_canonical(sid)['messages']])

 def test_additive_conversion_idempotence_and_old_link(self):
  with tempfile.TemporaryDirectory() as tmp:
   ctx=initialize_workspace(Path(tmp)/'vault');old=ctx.raw_archive/'Hermes/abcdef123456';old.mkdir(parents=True)
   rows=[{'message_id':'m1','session_id':'hermes:stable','role':'user','original_text':'原文','timestamp':None}]
   (old/'messages.jsonl').write_text(json.dumps(rows[0],ensure_ascii=False)+'\n','utf-8')
   (old/'manifest.json').write_text(json.dumps({'session_id':'hermes:stable','session_title':'真实标题','client':'hermes','message_count':1}),'utf-8')
   (old/'conversation.md').write_text('原始正文','utf-8');(old/'attachments').mkdir();(old/'attachments/a.bin').write_bytes(b'original')
   before={str(p):p.read_bytes() for p in old.rglob('*') if p.is_file()}
   result=normalize(ctx,old);self.assertEqual('normalized',result['status']);self.assertEqual('already_normalized',normalize(ctx,old)['status'])
   self.assertTrue(all(Path(p).read_bytes()==raw for p,raw in before.items()))
   target=ctx.root/result['target_path'];self.assertEqual(b'original',(target/'02_附件/a.bin').read_bytes())
   with patch.object(session_archive,'active_workspace',return_value=ctx),patch.object(session_archive,'CANONICAL_ROOT',ctx.raw_archive):
    self.assertEqual(1,len(session_archive._manifest_paths()))
    item=session_archive._find_canonical('hermes:stable');self.assertEqual(rows,item['messages'])
    self.assertEqual(str(target),item['archive_path'])

if __name__=='__main__':unittest.main()
