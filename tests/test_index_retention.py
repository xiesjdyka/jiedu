import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from knowledge_bridge.index_retention import prune

class RetentionTests(unittest.TestCase):
    def test_keeps_active_recent_pinned_and_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);db=root/'lancedb';db.mkdir();(root/'generations').mkdir()
            names=['evidence_'+str(i)*32 for i in range(1,7)]
            now=time.time()
            for i,name in enumerate(names):
                d=db/(name+'.lance');d.mkdir();os.utime(d,(now-(20-i)*86400,)*2)
                (root/'generations'/f'state-{str(i+1)*32}.json').write_text('{}')
            (db/'unrecognized.lance').mkdir()
            (root/'active-vector.json').write_text(json.dumps({'table':names[0]}))
            (root/'pinned-generations.json').write_text(json.dumps({'tables':[names[1]]}))
            context=Mock();context.index_root.return_value=root;fake=Mock()
            with patch('lancedb.connect',return_value=fake):removed=prune(context,'evidence')
            self.assertEqual(set(removed),set(names[2:4]));self.assertEqual(fake.drop_table.call_count,2)
            self.assertTrue((root/'generations'/('state-'+'1'*32+'.json')).exists())
    def test_invalid_active_pointer_never_deletes(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'lancedb').mkdir();(root/'active.json').write_text('{"table":"../bad"}')
            context=Mock();context.index_root.return_value=root
            self.assertEqual(prune(context,'formal'),[])

if __name__=='__main__':unittest.main()
