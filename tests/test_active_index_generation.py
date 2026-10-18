import json
import tempfile
import unittest
from unittest.mock import patch

import lancedb
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge import core, evidence_index


class ActiveGenerationTests(unittest.TestCase):
    def test_chinese_title_remains_readable_and_in_organization_inventory(self):
        from knowledge_bridge.client_access import resolve_document_target
        from knowledge_bridge.organizer import Organizer
        from knowledge_bridge.source_reading import read_source
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(temporary)
            folder='90_原始档案/ChatGPT/项目：先阅读，再讨论…（一）/01_对话'
            source=folder+'/conversation.md';path=context.root/source
            path.parent.mkdir(parents=True);path.write_text('## USER\n\n保留原始资料。','utf-8')
            self.assertEqual(source,resolve_document_target(context,source).relative_path)
            self.assertIn('保留原始资料',read_source(context,source)['content'])
            job=Organizer(context).start('test','evidence',folder,'punctuation-source')
            self.assertEqual(1,job['total'])
            self.assertEqual(source,job['items'][0]['source_path'])

    def test_formal_and_evidence_open_generation_beyond_first_catalog_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(temporary)
            root=context.index_root('evidence');root.mkdir(parents=True,exist_ok=True)
            database=lancedb.connect(root/'lancedb')
            for index in range(12):
                database.create_table(f'generation_{index:02d}',data=[{'content':str(index),'vector':[1.0,0.0]}])
            name='generation_11'
            (root/'active-vector.json').write_text(json.dumps({'version':1,'table':name}),'utf-8')
            self.assertEqual('11',evidence_index.table(context).to_arrow().to_pylist()[0]['content'])
            with patch.object(core,'active_workspace',return_value=context),patch.object(core,'db',return_value=database),patch.object(core,'_active_descriptor',return_value={'table':name}):
                self.assertEqual('11',core.table().to_arrow().to_pylist()[0]['content'])
            (root/'active-vector.json').write_text(json.dumps({'version':1,'table':'missing'}),'utf-8')
            self.assertIsNone(evidence_index.table(context))
            with patch.object(core,'active_workspace',return_value=context),patch.object(core,'db',return_value=database),patch.object(core,'_active_descriptor',return_value={'table':'missing'}):
                self.assertIsNone(core.table())


if __name__=='__main__':unittest.main()
