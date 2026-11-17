import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import core_bootstrap, memory_control


class CoreBootstrapTests(unittest.TestCase):
    def test_formal_sources_only_and_gate_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); vault=root/"vault"; system=vault/"00_系统"; system.mkdir(parents=True)
            sources=[system/"我的长期画像.md",system/"AI协作规则.md",system/"AI错误与教训.md",system/"知识库规则.md"]
            for path in sources:path.write_text(f"# {path.stem}\n\n长期稳定内容。\n","utf-8")
            store=root/"store"
            with patch.object(core_bootstrap,"VAULT",vault),patch.object(core_bootstrap,"SOURCES",sources),patch.object(core_bootstrap,"STORE",store),patch.object(core_bootstrap,"CURRENT",store/"current.json"):
                item=core_bootstrap.compile_core()
            self.assertEqual(["00_系统/我的长期画像.md","00_系统/AI协作规则.md","00_系统/AI错误与教训.md","00_系统/知识库规则.md"],[x["path"] for x in item["source_files"]])
            self.assertTrue(item["gate"]["formal_sources_only"])
            self.assertNotIn("memory-draft",item["content"])

    def test_non_source_project_files_are_not_compiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); vault=root/"vault"; system=vault/"00_系统"; system.mkdir(parents=True)
            sources=[system/"我的长期画像.md",system/"AI协作规则.md",system/"AI错误与教训.md",system/"知识库规则.md"]
            for path in sources:path.write_text("稳定内容。","utf-8")
            (vault/"项目状态.md").write_text("不应进入Core", "utf-8")
            store=root/"store"
            with patch.object(core_bootstrap,"VAULT",vault),patch.object(core_bootstrap,"SOURCES",sources),patch.object(core_bootstrap,"STORE",store),patch.object(core_bootstrap,"CURRENT",store/"current.json"):
                item=core_bootstrap.compile_core()
            self.assertNotIn("不应进入Core",item["content"])
            self.assertEqual(4,item["gate"]["source_count"])


    def test_awareness_and_core_share_empty_and_missing_status_without_stale_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)
            source=vault/"00_系统"/"我的长期画像.md"
            source.parent.mkdir()
            store=vault/"store"
            selected=[]
            with patch.object(core_bootstrap,"VAULT",vault), patch.object(core_bootstrap,"SOURCES",selected), \
                 patch.object(core_bootstrap,"STORE",store), patch.object(core_bootstrap,"CURRENT",store/"current.json"), \
                 patch.object(memory_control,"PROJECT_ROOT",vault/"projects"), patch.object(memory_control,"MANIFEST",vault/"manifest.json"):
                empty,_=core_bootstrap.read_status()
                self.assertEqual("not_configured",empty["reason"])
                self.assertEqual(empty["reason"],memory_control.memory_awareness()["bootstrap"]["reason"])
                self.assertFalse(store.exists())

                source.write_text("# 画像\n- 示例内容\n","utf-8-sig")
                selected.append(source)
                ready,_=core_bootstrap.read_status()
                awareness=memory_control.memory_awareness()
                self.assertTrue(ready["ready"])
                self.assertEqual(set(ready),set(empty))
                self.assertTrue(awareness["bootstrap"]["has_content"])
                self.assertEqual(["示例内容"],awareness["known"]["user_profile"])
                self.assertEqual([],awareness["known"]["collaboration_rules"])
                self.assertIsNone(awareness["known"]["sources"]["collaboration_rules"])

                source.unlink()
                missing,documents=core_bootstrap.read_status()
                awareness=memory_control.memory_awareness()
                self.assertEqual("source_missing",missing["reason"])
                self.assertEqual(set(ready),set(missing))
                self.assertEqual({},documents)
                self.assertEqual([],missing["source_files"])
                self.assertEqual("",missing["content"])
                self.assertIsNone(missing["version"])
                self.assertEqual(missing["message"],awareness["bootstrap"]["message"])
                self.assertFalse(awareness["bootstrap"]["has_content"])
                self.assertEqual([],awareness["known"]["user_profile"])
                self.assertTrue((store/"current.json").is_file())

    def test_compiler_failure_is_not_reported_as_missing_configuration(self):
        with patch.object(core_bootstrap,"_compile",side_effect=PermissionError("cache failure")):
            with self.assertRaises(PermissionError):
                core_bootstrap.read_status()


if __name__=="__main__":unittest.main()
