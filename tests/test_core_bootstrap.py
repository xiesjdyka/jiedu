import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import core_bootstrap


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


if __name__=="__main__":unittest.main()
