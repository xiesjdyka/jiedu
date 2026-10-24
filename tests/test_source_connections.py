import json,tempfile,unittest,base64,hashlib
from unittest.mock import patch
from pathlib import Path
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.source_connections import SourceConnections
from knowledge_bridge.inbox_capture import capture


class SourceConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.context=initialize_workspace(root/'workspace')
        self.registry=ClientIdentityRegistry(self.context,root/'identities')
        self.permissions=ClientPermissionService(self.registry)
        self.pairing=PairingTransactionManager(self.registry,self.permissions)
        self.sources=SourceConnections(self.context)

    def pair(self,name):
        preview=self.pairing.begin(name,name,'custom','stdio',product_family='豆包')
        self.pairing.commit(preview['pairing_id'])
        return self.registry.authenticate(name,preview['secret'])

    def test_defaults_cancel_append_restart_pause_and_identity_isolation(self):
        preview=self.pairing.begin('cancel_me','cancel','custom','stdio',product_family='豆包')
        self.pairing.cancel(preview['pairing_id'])
        self.assertFalse(self.sources.path.exists());self.assertFalse((self.context.raw_archive/'豆包').exists())
        one=self.pair('one');two=self.pair('two')
        self.assertEqual([],list(self.context.raw_archive.iterdir()))
        self.assertEqual(0,self.sources.status(one.internal_id)['message_count'])
        payload={'conversation_id':'same','message_id':'m2','role':'assistant','content':'later','source_order':2,'title':'same title'}
        saved=self.sources.append(one,self.permissions,payload)
        self.assertEqual(['豆包'],[p.name for p in self.context.raw_archive.iterdir()])
        self.assertTrue(saved['saved']);self.assertTrue(self.sources.append(one,self.permissions,payload)['duplicate'])
        earlier=payload|{'message_id':'m1','role':'user','content':'earlier','source_order':1}
        self.sources.append(one,self.permissions,earlier)
        folder=self.context.root/saved['archive_path']
        rows=[json.loads(x) for x in (folder/'03_元数据/messages.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(['m1','m2'],[r['message_id'] for r in rows])
        second=self.sources.append(two,self.permissions,payload)
        self.assertNotEqual(saved['session_key'],second['session_key'])
        self.assertNotEqual(saved['archive_path'],second['archive_path'])
        self.assertEqual(2,SourceConnections(self.context).status(one.internal_id)['message_count'])
        renamed=self.sources.append(one,self.permissions,payload|{'title':'改名后'})
        self.assertEqual(saved['session_key'],renamed['session_key'])
        untitled=dict(payload);untitled.pop('title')
        self.assertEqual(renamed['archive_path'],self.sources.append(one,self.permissions,untitled)['archive_path'])
        with self.assertRaises(ValueError):self.sources.append(one,self.permissions,payload|{'content':'overwrite'})
        with self.assertRaises(ValueError):self.sources.append(one,self.permissions,payload|{'source_key':'codex'})
        self.permissions.set_policy(one.internal_id,'evidence','raw_archive','deny')
        with self.assertRaises(PermissionError):self.sources.append(one,self.permissions,payload)
        self.registry.revoke(two.internal_id)
        with self.assertRaises(PermissionError):self.sources.append(two,self.permissions,payload)

    def test_explicit_inbox_capture_preserves_original_and_is_idempotent(self):
        identity=self.pair('fixture')
        result=self.sources.append(identity,self.permissions,{'conversation_id':'s1','message_id':'m1','role':'user','content':'A video link; save this idea','title':'Ideas'})
        raw=self.context.raw_message_store/(result['session_key']+'.jsonl');before=raw.read_bytes()
        first=capture(self.context,result['session_key'],'m1','用户明确保存')
        second=capture(self.context,result['session_key'],'m1','repeat')
        self.assertTrue(second['duplicate']);self.assertEqual(first['capture_id'],second['capture_id'])
        self.assertEqual(before,raw.read_bytes())
        self.assertNotIn('A video link; save this idea',(self.context.root/first['path']).read_text('utf-8'))
        self.assertEqual('allow',self.permissions.authorize(identity,'inbox','save_inbox').outcome.value)
        self.permissions.set_policy(identity.internal_id,'inbox','save_inbox','deny')
        self.assertEqual('deny',self.permissions.authorize(identity,'inbox','save_inbox').outcome.value)

    def test_attachment_backfill_and_binding_failure_rollback(self):
        preview=self.pairing.begin('failed','失败','custom','stdio',product_family='测试产品')
        with patch('knowledge_bridge.source_connections._atomic_json',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.pairing.commit(preview['pairing_id'])
        self.assertEqual([],self.registry.list_clients())
        self.assertFalse((self.context.raw_archive/'测试产品').exists())
        identity=self.pair('attachment');data=b'original image bytes'
        payload={'conversation_id':'attachments','message_id':'m1','role':'user','content':'source attachment','attachment_refs':['img:1']}
        first=self.sources.append(identity,self.permissions,payload)
        raw=self.context.raw_message_store/(first['session_key']+'.jsonl');original=raw.read_bytes()
        attachment={'reference':'img:1','original_name':'same.png','data_base64':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}
        second=self.sources.append(identity,self.permissions,payload|{'attachments':[attachment]})
        self.assertTrue(second['duplicate']);self.assertEqual(original,raw.read_bytes())
        captured=capture(self.context,first['session_key'],'m1')
        self.assertEqual(1,len(captured['attachments']))
        self.assertEqual(hashlib.sha256(data).hexdigest(),captured['attachments'][0]['sha256'])
        with self.assertRaises(ValueError):self.sources.append(identity,self.permissions,payload|{'attachments':[attachment|{'sha256':'wrong'}]})

if __name__=='__main__':unittest.main()
