import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge import evidence_index as index


class SelectionReconcileTests(unittest.TestCase):
    def fixture(self, root):
        context=initialize_workspace(root/'workspace')
        keep=context.raw_archive/'keep.md';removed=context.raw_archive/'removed.md'
        keep.write_text('保留的正式对话证据。','utf-8')
        removed.write_text('最初被选择的历史文档。','utf-8')
        return context,keep,removed

    def model(self):
        return patch('knowledge_bridge.core.embed',side_effect=lambda texts,**kw:np.ones((len(texts),4),dtype='float32'))

    def test_excluded_existing_file_can_leave_index_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as temp,self.model(),patch('knowledge_bridge.core.local_model_status',return_value={}):
            context,keep,removed=self.fixture(Path(temp))
            index.rebuild(context)
            removed.write_text('---\ndocument_role: internal_process\n---\n完整原件仍保留。','utf-8')
            original=removed.read_bytes()
            result=index.sync(context=context)
            self.assertEqual('incremental_synced',result['status'])
            self.assertEqual(1,result['deleted'])
            self.assertEqual(original,removed.read_bytes())
            self.assertEqual({context.relative(keep)},{r['source_path'] for r in index.table(context).to_arrow().to_pylist()})
            self.assertEqual('unchanged',index.sync(context=context)['status'])

    def test_selection_reappearing_during_work_keeps_original_generation(self):
        with tempfile.TemporaryDirectory() as temp,self.model(),patch('knowledge_bridge.core.local_model_status',return_value={}):
            context,keep,removed=self.fixture(Path(temp));index.rebuild(context)
            before=index._descriptor(context)
            with patch('knowledge_bridge.search_scopes.files_for_scope',side_effect=[[keep],[keep,removed]]):
                result=index.sync(context=context)
            self.assertEqual('source_changed',result['status'])
            self.assertEqual(before,index._descriptor(context))
            self.assertTrue(removed.exists())

    def test_actual_removed_file_still_leaves_index(self):
        with tempfile.TemporaryDirectory() as temp,self.model(),patch('knowledge_bridge.core.local_model_status',return_value={}):
            context,keep,removed=self.fixture(Path(temp));index.rebuild(context)
            removed.unlink()
            self.assertEqual('incremental_synced',index.sync(context=context)['status'])
            self.assertNotIn(context.relative(removed),index._state(context)['files'])

    def test_incremental_retains_untouched_vectors_and_replays_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp,self.model(),patch('knowledge_bridge.core.local_model_status',return_value={}):
            context,keep,changed=self.fixture(Path(temp));index.rebuild(context)
            def kept_rows():
                return [r for r in index.table(context).to_arrow().to_pylist() if r['source_path']==context.relative(keep)]
            before=kept_rows()
            changed.write_text('新增资料，只应更新这一份文档。','utf-8')
            result=index.sync([context.relative(changed)],context)
            self.assertEqual('incremental_synced',result['status'])
            self.assertEqual(before,kept_rows())
            rows=index.table(context).to_arrow().to_pylist()
            self.assertEqual(len(rows),len({r['chunk_id'] for r in rows}))
            self.assertEqual('unchanged',index.sync([context.relative(changed)],context)['status'])


if __name__=='__main__':unittest.main()
