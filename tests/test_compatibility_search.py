import tempfile,unittest
from pathlib import Path
from knowledge_bridge.retrieval import RetrievalService,DirectoryRetrievalContext
from knowledge_bridge.search_scopes import search_scope,_formal_compatibility_selection,files_for_scope
from knowledge_bridge.source_reading import read_source
from knowledge_bridge.workspace import initialize_workspace
class CompatibilitySearchTests(unittest.TestCase):
 def test_explicit_redirect_collapses_but_old_file_reads_and_brand_text_stays(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);ctx=initialize_workspace(root);folder=root/'02_项目';(folder/'Old').mkdir();(folder/'Actual').mkdir()
   target=folder/'Actual/当前状态.md';target.write_text('# 界渡当前状态\n\n界渡实际内容，工程验收已完成。','utf-8')
   alias=folder/'Old/当前状态.md';alias.write_text('---\ndocument_role: 旧路径兼容指引\n---\n# Knowledge Bridge 界渡兼容入口\n\n界渡本文件保留旧链接。[[../Actual/当前状态]]','utf-8')
   ordinary=folder/'history.md';ordinary.write_text('# Knowledge Bridge 历史\n\nKnowledge Bridge 普通历史正文，必须保留。','utf-8')
   original=alias.read_bytes();result=RetrievalService.for_root(root).search('界渡',mode='keyword',limit=10)
   self.assertIn('02_项目/Actual/当前状态.md',[x['source_path'] for x in result['results']]);self.assertNotIn('02_项目/Old/当前状态.md',[x['source_path'] for x in result['results']]);self.assertEqual(len(result['compatibility_redirects']),1)
   brand=RetrievalService.for_root(root).search('Knowledge Bridge',mode='keyword');self.assertIn('02_项目/history.md',[x['source_path'] for x in brand['results']])
   old=read_source(ctx,'02_项目/Old/当前状态.md');self.assertEqual(old['content'].encode(),original)
   explicit=RetrievalService.for_root(root).search('界渡',mode='keyword',folder='02_项目/Old');self.assertIn('02_项目/Old/当前状态.md',[x['source_path'] for x in explicit['results']])
   vector=search_scope('界渡','formal',10,ctx,vector_hits=[{'source_path':'02_项目/Old/当前状态.md','content':'界渡本文件保留旧链接。[[../Actual/当前状态]]'},{'source_path':'02_项目/Actual/当前状态.md','content':'界渡实际内容，工程验收已完成。'}],requested_mode='hybrid')
   self.assertNotIn('02_项目/Old/当前状态.md',[x['source_path'] for x in vector['results']]);self.assertTrue(vector['vector_matched_passages'])
 def test_missing_ambiguous_cycle_and_normal_named_pages_remain(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);folder=root/'02_项目';folder.mkdir();ctx=DirectoryRetrievalContext(root)
   content={'missing.md':'[[missing-target]]','ambiguous.md':'[[one]] [[two]]','one.md':'[[two]]','two.md':'[[one]]','outside.md':'[[../../outside]]'}
   for name,body in content.items():(folder/name).write_text('---\ndocument_role: 旧路径兼容指引\n---\n'+body,'utf-8')
   (folder/'Knowledge Bridge.md').write_text('# 普通知识\n\n[[one]]','utf-8')
   selected,redirects=_formal_compatibility_selection(ctx,files_for_scope(ctx,'formal'));self.assertEqual(len(selected),6);self.assertEqual(redirects,[])
if __name__=='__main__':unittest.main()
