import tempfile,unittest
from pathlib import Path
from knowledge_bridge.provider_config import save,status
from knowledge_bridge.writer_core import Writer
class ProviderWriterProductionTests(unittest.TestCase):
 def test_provider_configuration_without_secret_or_network(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/"provider.json";s=save({"provider_type":"openai-compatible","base_url":"https://example.invalid/v1","model":"test-model","api_key_secret_ref":"env:KB_TEST_KEY","timeout":45,"max_input":12000,"enabled":True},path)
   self.assertEqual("已配置",s["state"]);self.assertEqual(0,s["network_calls"]);self.assertNotIn('"api_key":',path.read_text("utf-8"))
   with self.assertRaises(ValueError):save({"api_key":"plaintext"},path)
 def test_production_like_writer_same_core_and_undo(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/"vault";versions=Path(tmp)/"versions";w=Writer(root,versions)
   p=w.preview("03_知识/fixture.md","生产路径fixture","编辑后内容",[{"message_id":"fixture"}])
   with self.assertRaises(PermissionError):w.apply(p)
   a=w.apply(p,True);self.assertNotEqual(a["before_hash"],a["after_hash"])
   self.assertTrue((root/"03_知识"/"fixture.md").exists())
   u=w.undo(a["version"],True)
   self.assertFalse((root/"03_知识"/"fixture.md").exists());self.assertIsNone(u["restored_hash"])
 def test_writer_undo_restores_existing_file_bytes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/"vault";versions=Path(tmp)/"versions";w=Writer(root,versions)
   target=root/"02_项目"/"示例"/"当前状态.md";target.parent.mkdir(parents=True,exist_ok=True);target.write_text("原正文",encoding="utf-8")
   p=w.preview("02_项目/示例/当前状态.md","新增小节","新增内容",[])
   a=w.apply(p,True);u=w.undo(a["version"],True)
   self.assertEqual("原正文",target.read_text(encoding="utf-8"));self.assertEqual(a["before_hash"],u["restored_hash"])
if __name__=="__main__":unittest.main()
