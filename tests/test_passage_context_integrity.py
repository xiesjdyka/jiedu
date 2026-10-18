import tempfile,unittest
from pathlib import Path
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.passage_search import passages
from knowledge_bridge.search_scopes import search_scope
from knowledge_bridge.source_reading import read_source

class ContextIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=initialize_workspace(self.tmp.name)
    def source(self,text,path='03_知识/边界.md'):
        target=self.context.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text,'utf-8');return path
    def search(self,query,**kwargs):return search_scope(query,context=self.context,scope='formal',**kwargs)
    def test_actual_negation_quote_excerpt_and_pinned_read_agree(self):
        text='# 隐私与外部AI边界\n\n本地知识默认采用最小必要外发原则。\n\n不因为：\n\n> “AI可能有用”\n\n就把整个Vault发给外部模型。\n\n外部AI只获得当前任务真正需要的知识。'
        source=self.source(text)
        for vector in ([],[{'source_path':source,'content':'就把整个Vault发给外部模型。'}]):
            result=self.search('整个Vault发给外部模型',vector_hits=vector,requested_mode='vector' if vector else 'keyword')
            hit=result['results'][0]
            self.assertIn('不因为：',hit['content']);self.assertIn('“AI可能有用”',hit['excerpt'])
            self.assertEqual(hit['content'],hit['excerpt'])
            self.assertEqual(5,hit['position']['line_start'])
            read=read_source(self.context,**hit['read'])
            self.assertTrue(read['content'].startswith('不因为：'))
            self.assertEqual((self.context.root/source).read_bytes()[hit['position']['byte_start']:hit['position']['byte_end']].decode().strip(),hit['content'])
            self.assertFalse(hit['support']['answer_supported'])
        (self.context.root/source).write_text(text+'\n修改。','utf-8')
        with self.assertRaises(Exception):read_source(self.context,**hit['read'])
    def test_conditions_lists_and_quote_survive_layout(self):
        source=self.source('# 发送\n\n只有满足：\n\n- 客户身份已核对\n- 负责人已确认\n\n才可以把回复发送给客户。\n\n## 另一个章节\n\n直接发送历史草稿。')
        hit=self.search('回复发送给客户',limit=1)['results'][0]
        self.assertIn('只有满足：',hit['content']);self.assertIn('负责人已确认',hit['content'])
        self.assertNotIn('历史草稿',hit['content'])
        self.assertNotIn('历史草稿',list(passages((self.context.root/source).read_text('utf-8')))[0]['text'])
    def test_long_condition_stays_at_read_anchor_and_honors_budget(self):
        self.source('# 长条件\n\n未经确认，不得因为：\n\n> '+('补充说明。'*1800)+'\n\n就自动发送客户回复。')
        result=self.search('自动发送客户回复',limit=1,excerpt_budget=180)
        hit=result['results'][0]
        self.assertTrue(hit['content'].startswith('未经确认，不得因为：'))
        self.assertTrue(hit['excerpt_truncated']);self.assertLessEqual(len(hit['content']),180)
        self.assertLessEqual(result['budgets']['returned_characters'],180)
        page=read_source(self.context,**hit['read']);self.assertTrue(page['content'].startswith('未经确认'))
        self.assertTrue(page['next_cursor'])
    def test_raw_messages_keep_roles_and_do_not_merge_across_speakers(self):
        self.source('# 对话\n\n## MESSAGE a1｜ASSISTANT\n\n如果需要：\n\n> 我猜可以直接发送。\n\n## MESSAGE u1｜USER\n\n不可以直接发送，必须由我确认。',path='90_原始档案/ChatGPT/发送/01_对话/conversation.md')
        result=search_scope('不可以直接发送',context=self.context,scope='evidence',limit=1)
        hit=result['results'][0];self.assertEqual('u1',hit['message_id']);self.assertEqual('USER',hit['role'])
        self.assertNotIn('我猜',hit['content'])
        prior=hit['context']['messages'][0];self.assertEqual('ASSISTANT',prior['role']);self.assertEqual('a1',prior['message_id'])

if __name__=='__main__':unittest.main()
