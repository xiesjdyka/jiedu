import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge import ai_access, core
from knowledge_bridge import original_archive_import
from knowledge_bridge import session_archive
from knowledge_bridge import session_manager
from knowledge_bridge.retrieval import DirectoryRetrievalContext


class AIAccessTests(unittest.TestCase):
    def test_all_four_core_documents_are_directly_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault=Path(temporary)
            sources=("00_系统/我的长期画像.md","00_系统/AI协作规则.md",
                     "00_系统/AI错误与教训.md","00_系统/知识库规则.md")
            for index,source in enumerate(sources):
                path=vault/source;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(f"# core {index}\n","utf-8")
            with patch.object(core,"VAULT",vault):
                results=[core.get_document(source) for source in sources]
            self.assertEqual(list(sources),[row["source_path"] for row in results])
            self.assertTrue(all(row["content"].startswith("# core") for row in results))

    def test_direct_search_and_context_do_not_need_an_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "00_系统").mkdir()
            (root / "00_系统" / "原则.md").write_text("界渡只读优先，正式写入需要Owner确认。", encoding="utf-8")
            with patch.object(ai_access, "load_knowledge_base", return_value={"vault_path": str(root), "core_files": ["00_系统/原则.md"]}), \
                 patch.object(ai_access, "markdown_files", return_value=["00_系统/原则.md"]):
                found = ai_access.knowledge_search("只读优先")
                state = ai_access.context_state()
            self.assertEqual(1, found["returned"])
            self.assertFalse(found["vector_used"])
            self.assertFalse(found["embedding_used"])
            self.assertTrue(state["core_files"][0]["available"])

    def test_mcp_hybrid_search_uses_authenticated_console_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();context=SimpleNamespace(initialized=True,root=root,workspace_id='fixture-workspace')
            relayed={'requested_mode':'hybrid','effective_mode':'hybrid','embedding_used':True,
                     'vector_used':True,'retrieval_process':'console_single_instance','results':[], 'returned':0}
            with patch.object(ai_access,'load_knowledge_base',return_value={'vault_path':str(root)}), \
                 patch.object(ai_access,'active_workspace',return_value=context), \
                 patch.object(ai_access,'_console_search',return_value=relayed) as call, \
                 patch.dict('os.environ',{'KNOWLEDGE_BRIDGE_PROCESS_ROLE':'mcp',
                                          'KNOWLEDGE_BRIDGE_CLIENT_ID':'fixture',
                                          'KNOWLEDGE_BRIDGE_CLIENT_SECRET':'secret'},clear=False):
                result=ai_access.knowledge_search('语义问题',mode='hybrid',project='界渡')
            self.assertTrue(result['embedding_used']);self.assertTrue(result['vector_used'])
            self.assertEqual('console_single_instance',result['retrieval_process'])
            call.assert_called_once()

    def test_mcp_workspace_mismatch_is_rejected_before_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();other=root/'other';other.mkdir()
            context=SimpleNamespace(initialized=True,root=other,workspace_id='other-workspace')
            with patch.object(ai_access,'load_knowledge_base',return_value={'vault_path':str(root)}), \
                 patch.object(ai_access,'active_workspace',return_value=context), \
                 patch.dict('os.environ',{'KNOWLEDGE_BRIDGE_PROCESS_ROLE':'mcp'},clear=False):
                with self.assertRaises(PermissionError):
                    ai_access.knowledge_search('不应返回',mode='hybrid')

    def test_mcp_console_unavailable_falls_back_without_loading_in_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'02_项目'/'界渡').mkdir(parents=True)
            (root/'02_项目'/'界渡'/'当前状态.md').write_text('界渡当前保持普通模式。','utf-8')
            context=DirectoryRetrievalContext(root);context.workspace_id='fixture-workspace'
            with patch.object(ai_access,'load_knowledge_base',return_value={'vault_path':str(root)}), \
                 patch.object(ai_access,'active_workspace',return_value=context), \
                 patch.object(ai_access,'_console_search',side_effect=ConnectionError('Console 不可用')), \
                 patch.dict('os.environ',{'KNOWLEDGE_BRIDGE_PROCESS_ROLE':'mcp',
                                          'KNOWLEDGE_BRIDGE_CLIENT_ID':'fixture',
                                          'KNOWLEDGE_BRIDGE_CLIENT_SECRET':'secret'},clear=False):
                result=ai_access.knowledge_search('普通模式')
            self.assertEqual('keyword',result['effective_mode'])
            self.assertFalse(result['embedding_used']);self.assertFalse(result['vector_used'])
            self.assertEqual('mcp_keyword_fallback',result['retrieval_process'])
            self.assertIn('Console 不可用',result['fallback_reason'])

    def test_change_request_is_pending_and_never_writes_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "00_系统").mkdir()
            target = root / "00_系统" / "原则.md"
            target.write_text("原文", encoding="utf-8")
            requests = root / "control" / "requests.jsonl"
            with patch.object(ai_access, "REQUESTS", requests), patch.object(ai_access, "load_knowledge_base", return_value={"vault_path": str(root)}):
                with self.assertRaises(ValueError):
                    ai_access.request_change("fixture", "00_系统/原则.md", "建议文本", "需要Owner检查")
            self.assertFalse(requests.exists(), '缺少真实证据的请求不能再进入独立待办队列')
            self.assertEqual("原文", target.read_text(encoding="utf-8"))

    def test_full_session_is_mirrored_to_l0_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "sessions" / "sessions.json"
            raw = root / "sessions" / "raw-archives"
            canonical = root / "90_原始档案"
            with patch.object(session_manager, "SESSIONS_PATH", state), patch.object(session_manager, "ARCHIVE_ROOT", raw), \
                 patch.object(session_archive, "SESSIONS_PATH", state), patch.object(session_archive, "ARCHIVE_ROOT", raw), \
                 patch.object(session_archive, "CANONICAL_ROOT", canonical), \
                 patch.object(original_archive_import, "get_session", return_value=None):
                session_manager.session_start("fixture:1", "fixture", "test", "full", state_path=state, archive_root=raw)
                first = session_manager.session_append("fixture:1", "m1", "user", "原文\n不改写", "2026-08-20T00:00:00Z", state_path=state, archive_root=raw)
                second = session_manager.session_append("fixture:1", "m1", "user", "原文\n不改写", "2026-08-20T00:00:00Z", state_path=state, archive_root=raw)
            archive = next(canonical.glob("fixture/*/messages.jsonl"))
            rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(first["saved"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(1, len(rows))
            self.assertEqual("原文\n不改写", rows[0]["original_text"])


if __name__ == "__main__":
    unittest.main()
