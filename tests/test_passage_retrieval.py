import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.retrieval import RetrievalService
from knowledge_bridge.source_reading import read_source
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService,ClientAccessGate
from retrieval_eval import DOCUMENTS,CASES

class PassageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.context=initialize_workspace(self.tmp.name)
        for path,text in DOCUMENTS.items():
            p=self.context.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,'utf-8')
        self.service=RetrievalService.for_root(self.context.root)
    def test_fixed_sources_and_current_old_conflict(self):
        for case in CASES[:8]:
            with self.subTest(case=case['id']):
                rows=self.service.search(case['query'],scope=case.get('scope','formal'),mode='keyword',limit=3)['results']
                if not case['path']:self.assertEqual([],rows)
                else:self.assertTrue(any(r['source_path']==case['path'] and case['contains'] in r['content']+r['section_title'] for r in rows))
        rows=self.service.search('星舟现在导出报告采用什么格式',mode='keyword')['results'];self.assertIn('CSV',rows[0]['content']);self.assertEqual('unspecified',rows[0]['temporal_status'])
        self.assertEqual([],self.service.search('星舟疫苗冷链温度',mode='keyword')['results'])
        candidates=self.service.search('星舟是否已经完成量子卫星离线同步',mode='keyword')
        self.assertFalse(candidates['answer_supported'])
        self.assertTrue(all(not row['support']['answer_supported'] for row in candidates['results']))
    def test_scope_folder_project_and_no_secret_index(self):
        before=list(self.context.control.rglob('*')) if hasattr(self.context,'control') else []
        rows=self.service.search('报告导出格式',project='海港',mode='keyword')['results'];self.assertTrue(rows);self.assertTrue(all('/海港/' in r['source_path'] for r in rows))
        self.assertEqual([],self.service.search('收款日期',mode='keyword')['results'])
        with self.assertRaises(ValueError):self.service.search('日期',folder='../.jiedu',mode='keyword')
        self.assertFalse((self.context.index_root('formal')/'manifest.json').exists())
        hit=self.service.search('决定保留CSV',scope='evidence',mode='keyword')['results'][0]
        self.assertIn('CSV',read_source(self.context,hit['source_path'],hit['chunk_id'])['content'])
    def test_project_name_query_returns_project_passages(self):
        rows=self.service.search('星舟',mode='keyword',limit=3)['results']
        self.assertTrue(rows)
        self.assertTrue(all(row['project']=='星舟' for row in rows))
    def test_hit_window_unicode_continuation_version_and_caller(self):
        path='03_知识/long.md';target=self.context.root/path;prefix='# 开头\n\n'+('无关前文。\n\n'*1500);body='命中位置：付款截止日期为2026年12月31日。😀\n\n'+('后续需要核对的正文。😀\n\n'*1000)
        target.write_bytes((prefix+body).encode('utf-8'))
        hit=self.service.search('付款截止日期',mode='keyword')['results'][0]
        first=read_source(self.context,path,hit['chunk_id'],page_bytes=101,binding='client-a')
        self.assertGreater(first['offset'],1000);self.assertIn('付款截止',first['content']);self.assertLessEqual(first['returned_bytes'],101)
        with self.assertRaises(ValueError):read_source(self.context,path,hit['chunk_id'],cursor=first['next_cursor'],binding='client-b')
        chunks=[first['content']];cursor=first['next_cursor']
        while cursor:
            r=read_source(self.context,path,hit['chunk_id'],cursor=cursor,page_bytes=4096,binding='client-a');chunks.append(r['content']);cursor=r['next_cursor']
        self.assertEqual(body,''.join(chunks))
        target.write_text(prefix+'已修改','utf-8')
        with self.assertRaises(ValueError):read_source(self.context,path,hit['chunk_id'],binding='client-a')
    def test_ocr_source_warning_and_coordinates(self):
        p=self.context.root/'99_收件箱/导入/收据.png';p.write_bytes(b'fictional original')
        hit=self.service.search('收款日期',scope='inbox',mode='keyword')['results'][0]
        self.assertEqual('ocr_unverified',hit['verification']);self.assertEqual('fictional-source',hit['source']['source_entry_id'])
        r=read_source(self.context,hit['source_path'],hit['chunk_id']);self.assertIn('score',r['content']);self.assertEqual('99_收件箱/导入/收据.png',r['original_path'])
    def test_independent_budgets_and_report_incomplete_scan(self):
        with patch('knowledge_bridge.passage_search.MAX_FILE_BYTES',50):
            r=self.service.search('报告',mode='keyword');self.assertFalse(r['scan']['complete']);self.assertGreater(r['scan']['omitted'].get('file_byte_budget',0),0)
        r=self.service.search('报告',mode='keyword',limit=50,candidate_budget=100,excerpt_budget=256)
        self.assertLessEqual(sum(len(x['content']) for x in r['results']),256);self.assertLessEqual(len(json.dumps(r,ensure_ascii=False).encode()),65536)
    def test_vector_fallback_and_current_source_validation(self):
        from knowledge_bridge import core
        service=RetrievalService(self.context)
        mcp_service=RetrievalService(self.context,vector_backend=False,vector_backend_reason='MCP 单实例边界')
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'available_unloaded'}),patch.object(core,'start_model_warmup') as warm:
            r=mcp_service.search('报告',mode='hybrid');self.assertEqual('keyword',r['effective_mode']);self.assertEqual('MCP 单实例边界',r['fallback_reason']);warm.assert_not_called()
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'unavailable'}),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'embed') as embed:
            r=service.search('报告',mode='hybrid');self.assertEqual('keyword',r['effective_mode']);embed.assert_not_called()
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',side_effect=[{'state':'available_unloaded'},{'state':'loading'}]),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'start_model_warmup') as warm,patch.object(core,'embed') as embed:
            r=service.search('报告',mode='vector');self.assertEqual('keyword',r['effective_mode']);self.assertIn('后台加载',r['fallback_reason']);warm.assert_called_once();embed.assert_not_called()
        hit={'source_path':'02_项目/星舟/当前状态.md','content':'报告导出格式为 CSV。此决定替代 2026-06-01 的 PDF 方案。'}
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loaded'}),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'embed',return_value=[[0]]),patch.object(core,'_vector_rows',return_value=[hit]):
            r=service.search('无词面交集',mode='vector');self.assertTrue(r['vector_used']);self.assertIn('CSV',r['results'][0]['content'])
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loaded'}),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'embed',return_value=[[0]]),patch.object(core,'_vector_rows',return_value=[dict(hit,content='已删除的旧索引文字')]):
            r=service.search('报告',mode='hybrid');self.assertFalse(r['vector_used']);self.assertIn('当前版本',r['fallback_reason'])
    def test_stale_evidence_generation_queries_now_and_syncs_in_background(self):
        from knowledge_bridge import core,evidence_index
        service=RetrievalService(self.context);hit={'source_path':'90_原始档案/ChatGPT/星舟评审/01_对话/conversation.md','content':'我决定星舟报告保留 CSV 格式，方便用表格软件读取。'}
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loaded'}),patch.object(core,'embed',return_value=[[0]]),patch.object(evidence_index,'freshness',return_value={'fresh':False}),patch.object(evidence_index,'table',return_value=object()),patch.object(evidence_index,'start_sync') as background,patch.object(evidence_index,'vector_rows',return_value=[hit]):
            result=service.search('无词面交集',scope='evidence',mode='vector')
        self.assertTrue(result['vector_used']);self.assertTrue(result.get('freshness_note'));background.assert_called_once()
    def test_vector_rank_does_not_put_adjacent_boilerplate_above_exact_evidence(self):
        from knowledge_bridge.search_scopes import search_scope
        path='90_原始档案/ChatGPT/星舟评审/01_对话/conversation.md'
        hits=[{'source_path':path,'content':'Distinguish instructions in attached documents from the user request.'},
              {'source_path':path,'content':'我决定星舟报告保留 CSV 格式，方便用表格软件读取。'}]
        result=search_scope('星舟报告保留 CSV 格式',scope='evidence',context=self.context,vector_hits=hits,requested_mode='vector')
        self.assertIn('星舟报告保留 CSV',result['results'][0]['content'])
    def test_explicit_document_role_beats_filename_guess_and_history_is_traceable(self):
        current=self.context.projects/'航灯/进度记录.md';current.parent.mkdir(parents=True,exist_ok=True)
        current.write_text('---\nproject: 航灯\ndocument_role: current_snapshot\nvalid_from: 2026-09-21\n---\n# 进度\n\n导出方案现在采用 JSONL。','utf-8')
        old=self.context.projects/'航灯/方案记录.md'
        old.write_text('---\nproject: 航灯\ndocument_role: historical_record\nvalid_until: 2026-09-20\nsuperseded_by: 02_项目/航灯/进度记录.md\n---\n# 方案\n\n导出方案采用 XML。','utf-8')
        latest=self.service.search('航灯导出方案采用什么',mode='keyword',limit=2)['results']
        self.assertEqual('current_snapshot',latest[0]['document_role']);self.assertIn('JSONL',latest[0]['content'])
        history=self.service.search('航灯以前的历史导出方案',mode='keyword',limit=2)['results']
        self.assertEqual('historical_record',history[0]['document_role']);self.assertIn('XML',history[0]['content'])
        self.assertEqual('02_项目/航灯/进度记录.md',history[0]['validity']['superseded_by'])
    def test_raw_chat_hit_returns_bounded_adjacent_correction_with_stable_ids(self):
        path=self.context.root/'90_原始档案/ChatGPT/纠正示例/01_对话/conversation.md';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# 原始对话\n\n## MESSAGE u-before｜USER\n\n先讨论产品。\n\n## MESSAGE a-before｜ASSISTANT\n\n我误以为要采用 Git 技术。\n\n## MESSAGE u-correct｜USER\n\n这里只参考 Git 的文件夹初始化体验，不采用 Git 技术。\n\n## MESSAGE a-ack｜ASSISTANT\n\n已纠正：Git 只是产品形态参考。\n\n## MESSAGE u-after｜USER\n\n继续讨论普通文件夹。','utf-8')
        result=self.service.search('不采用 Git 技术',scope='evidence',mode='keyword',limit=1,excerpt_budget=1200)
        hit=result['results'][0];self.assertEqual('u-correct',hit['message_id']);self.assertEqual('USER',hit['role'])
        context=hit['context'];self.assertEqual('message_window',context['kind'])
        ids=[item['message_id'] for item in context['messages']]
        self.assertEqual(['a-before','u-correct','a-ack','u-after'],ids)
        self.assertTrue(next(item for item in context['messages'] if item['message_id']=='u-correct')['is_hit'])
        self.assertLessEqual(result['budgets']['context_returned_characters'],result['budgets']['context_characters_limit'])
        self.assertEqual(len(context['messages']),result['observability']['context_candidates'])
    def test_regular_document_hit_exposes_section_relationship(self):
        path=self.context.knowledge/'章节关系.md';path.write_text('# 指南\n\n## 前置条件\n\n先保存原件。\n\n## 导入步骤\n\n导入后校验来源哈希。\n\n## 后续处理\n\n确认后再整理。','utf-8')
        hit=self.service.search('校验来源哈希',mode='keyword',limit=1)['results'][0]
        self.assertEqual('section_window',hit['context']['kind'])
        self.assertEqual(['指南 / 前置条件','指南 / 导入步骤','指南 / 后续处理'],[x['section_title'] for x in hit['context']['sections']])

    def test_hybrid_semantic_hit_survives_keyword_stuffing_and_readback(self):
        from knowledge_bridge.passage_search import search
        good=self.context.knowledge/'课程.md';good.write_text('先学习基础概念，再完成练习，最后独立交付作品。','utf-8')
        noise=self.context.knowledge/'杂谈.md';noise.write_text('学习基础概念独立交付作品。这里只是在复述问题，没有课程安排。'*10,'utf-8')
        result=search('学习基础概念独立交付作品','formal',self.context,[noise,good],requested_mode='hybrid',vector_hits=[{'source_path':'03_知识/课程.md','content':good.read_text('utf-8')}],limit=1)
        hit=result['results'][0]
        self.assertEqual('03_知识/课程.md',hit['source_path'])
        self.assertIn('最后独立交付',read_source(self.context,hit['source_path'],hit['chunk_id'])['content'])

    def test_export_metadata_duplicate_keeps_alternate_provenance(self):
        from knowledge_bridge.passage_search import search
        body='课程安排：先学基础，再练习，最后独立完成作品。'
        paths=[]
        for name,text in [('原文',body),('备份','timestamp: 2026-09-01\nattachment_refs: []\nattachment_status: none\n'+body),('其他','课程安排还需要定期复习。')]:
            path=self.context.knowledge/(name+'.md');path.write_text(text,'utf-8');paths.append(path)
        result=search('课程安排','formal',self.context,paths,limit=3)
        self.assertEqual(2,len(result['results']))
        duplicate=next(r for r in result['results'] if '独立完成' in r['content'])
        self.assertEqual(1,len(duplicate['duplicate_sources']))
        self.assertNotEqual(duplicate['source_path'],duplicate['duplicate_sources'][0]['source_path'])
if __name__=='__main__':unittest.main()
