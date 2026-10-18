import json,os,tempfile,unittest
from pathlib import Path
from knowledge_bridge.search_scopes import _ledger_attachment_specs

class AttachmentSelectionCacheTests(unittest.TestCase):
    def test_append_replace_same_size_edit_and_corruption_are_observed(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'messages.jsonl'
            def read():
                s=p.stat()
                return _ledger_attachment_specs(str(p),(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
            p.write_text(json.dumps({'attachments':[{'stored_path':'a.md'}],'original_text':'private body'})+'\n','utf-8')
            first=read();self.assertNotIn('original_text',str(first));self.assertEqual(first,read())
            with p.open('a',encoding='utf-8') as f:f.write(json.dumps({'attachments':[{'stored_path':'b.md'}]})+'\n')
            self.assertEqual(2,len(read()))
            original=p.stat();p.write_text(p.read_text().replace('b.md','c.md'),'utf-8');os.utime(p,ns=(original.st_atime_ns,original.st_mtime_ns+1000000))
            self.assertEqual('c.md',read()[1]['stored_path'])
            other=p.with_suffix('.tmp');other.write_text('{"attachments":[]}\n','utf-8');os.replace(other,p)
            self.assertEqual((),read())
            p.write_text('broken','utf-8')
            with self.assertRaises(ValueError):read()

if __name__=='__main__':unittest.main()
