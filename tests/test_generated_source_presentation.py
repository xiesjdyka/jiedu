import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import HTTPException
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.knowledge_objects import KnowledgeObjectService
from knowledge_bridge.source_reading import read_source
from knowledge_bridge import web
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider

class GeneratedSourcePresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jiedu-generated-header-')
        root=Path(self.temp.name);self.context=initialize_workspace(root/'vault')
        source=root/'source';source.mkdir();self.body='独特词蓝鲸月台7249。\n\n---\n用户正文分隔线保留。\n'+'中文🙂段落。\n'*40
        (source/'项目便笺.txt').write_text(self.body,'utf-8')
        service=KnowledgeObjectService(self.context,EncryptedProtectedStorageProvider(self.context))
        preview=service.preview_import(str(source),'open');service.execute_import(preview['preview_id'],owner_confirmed=True)
        self.path=next((self.context.inbox/'导入').rglob('项目便笺.txt.md'))
        self.relative=self.path.relative_to(self.context.root).as_posix()
    def tearDown(self):self.temp.cleanup()
    def test_actual_import_metadata_body_title_and_bytes(self):
        before=self.path.read_bytes();page=read_source(self.context,self.relative)
        self.assertEqual(before.decode(),page['content'])
        self.assertEqual(self.body,page['display_content'])
        self.assertEqual('项目便笺 · 导入正文',page['generated_metadata']['title'])
        self.assertEqual(11,page['generated_metadata']['body_line_start'])
        self.assertEqual('unconfirmed_derived',page['verification'])
        self.assertEqual(before,self.path.read_bytes())
    def test_small_utf8_pages_raw_reconstruction_and_body_presentation(self):
        cursor=None;raw=[];shown=[];offset=0
        while True:
            page=read_source(self.context,self.relative,cursor=cursor,page_bytes=31)
            self.assertEqual(offset,page['offset']);offset+=page['returned_bytes']
            raw.append(page['content']);shown.append(page['display_content'])
            if not page['next_cursor']:break
            cursor=page['next_cursor']
        self.assertEqual(self.path.read_text('utf-8'),''.join(raw));self.assertEqual(self.body,''.join(shown))
        self.assertEqual(len(self.path.read_bytes()),offset)
    def test_hit_offset_and_physical_lines_unchanged(self):
        first=read_source(self.context,self.relative);start=first['generated_metadata']['body_byte_start']
        hit=read_source(self.context,self.relative,f'p1-{first["version"]}-{start}')
        self.assertEqual(start,hit['offset']);self.assertEqual(self.body,hit['content']);self.assertEqual(hit['content'],hit['display_content'])
        self.assertTrue(hit['read_started_at_hit'])
        self.assertEqual(11,self.path.read_bytes()[:start].count(b'\n')+1)
    def test_user_header_without_registry_is_never_folded(self):
        user=self.context.inbox/'user.md';user.write_bytes(self.path.read_bytes())
        page=read_source(self.context,user.relative_to(self.context.root).as_posix())
        self.assertNotIn('generated_metadata',page);self.assertTrue(page['content'].startswith('---'))
    def test_changed_header_does_not_inherit_generated_provenance(self):
        self.path.write_text(self.path.read_text('utf-8').replace('converter: native-text','converter: user-text'),'utf-8')
        self.assertNotIn('generated_metadata',read_source(self.context,self.relative))

class CandidateUndoVersionTests(unittest.TestCase):
    def test_stale_version_prevents_writer_call(self):
        with patch.object(web,'candidate_source',return_value={'status':'written','writer_version':'v_new'}),patch.object(web,'production_undo') as undo:
            with self.assertRaises(HTTPException) as error:
                web.api_candidate_writer_undo.__wrapped__('mem_fixture',web.CandidateWriteRequest(owner_confirmed=True,writer_version='v_old'),None)
            self.assertEqual(409,error.exception.status_code);undo.assert_not_called()
    def test_matching_version_keeps_owner_and_writer_verification(self):
        with patch.object(web,'candidate_source',return_value={'status':'written','writer_version':'v_new'}),patch.object(web,'production_undo',return_value={'target':'03_知识/fixture.md'}) as undo,patch.object(web,'revert_candidate_written') as revert,patch.object(web,'update_review_state'):
            result=web.api_candidate_writer_undo.__wrapped__('mem_fixture',web.CandidateWriteRequest(owner_confirmed=False,writer_version='v_new'),None)
            undo.assert_called_once_with('v_new',False,'production');revert.assert_called_once_with('mem_fixture','v_new');self.assertTrue(result['undone'])
