import tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from knowledge_bridge.client_access import ClientIdentityRegistry
from knowledge_bridge.workspace import initialize_workspace
import knowledge_bridge.client_config as cc

class ConnectionDeletionTests(unittest.TestCase):
 def test_same_name_repair_authentication_and_tombstone(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);r=ClientIdentityRegistry(initialize_workspace(p/'workspace'),p/'registry')
   old=r.create('hermes','Hermes','hermes','stdio');identity=r.authenticate('hermes',old['secret']);r.revoke(old['internal_id'])
   new=r.create('hermes','Hermes','hermes','stdio')
   self.assertTrue(r.authenticate('hermes',new['secret']).authenticated)
   self.assertFalse(r.authenticate('hermes',old['secret']).authenticated)
   self.assertFalse(r.authenticate('codex',new['secret']).authenticated)
   self.assertFalse(r.authenticate('hermes','bad-token').authenticated)
   r.delete(old['internal_id']);self.assertFalse(r.identity_current(identity))
   self.assertEqual([new['internal_id']],[x['internal_id'] for x in r.list_clients()])
   self.assertTrue(r.authenticate('hermes',new['secret']).authenticated)
   r.delete(new['internal_id']);self.assertFalse(r.authenticate('hermes',new['secret']).authenticated)
   third=r.create('hermes','Hermes','hermes','stdio');self.assertTrue(r.authenticate('hermes',third['secret']).authenticated)
   self.assertTrue(r._load()['clients'][old['internal_id']]['revoked_at'])
 def test_config_cleanup_exact_identity_preserves_successor_and_other_data(self):
  with tempfile.TemporaryDirectory() as t,ExitStack() as stack:
   p=Path(t);r=ClientIdentityRegistry(initialize_workspace(p/'workspace'),p/'registry')
   old=r.create('hermes','Hermes','hermes','stdio');r.delete(old['internal_id']);new=r.create('hermes','Hermes','hermes','stdio')
   adapter=cc.HermesAdapter();adapter.path=p/'config.yaml'
   entry=lambda x:{'command':'test','env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':'hermes','KNOWLEDGE_BRIDGE_CLIENT_SECRET':x['secret']}}
   def write(x):adapter.path.write_text(cc.yaml.safe_dump({'other':'preserved','mcp_servers':{'other_server':{'command':'keep'},'knowledge_bridge':entry(x)}}),'utf-8')
   for key,value in {'ADAPTERS':{'hermes':adapter},'DATA':p/'state','BACKUPS':p/'backups','AUDIT':p/'audit.jsonl','PENDING':p/'pending.json'}.items():stack.enter_context(patch.object(cc,key,value))
   write(new);before=adapter.path.read_bytes();self.assertEqual([],cc.remove_identity_configuration(r,old['internal_id'])['removed']);self.assertEqual(before,adapter.path.read_bytes())
   archives=p/'workspace'/'history.txt';archives.write_text('keep chats and knowledge');r.delete(new['internal_id'])
   self.assertEqual(['hermes'],cc.remove_identity_configuration(r,new['internal_id'])['removed'])
   data=cc.yaml.safe_load(adapter.path.read_text());self.assertEqual({'other_server':{'command':'keep'}},data['mcp_servers']);self.assertEqual('preserved',data['other']);self.assertEqual('keep chats and knowledge',archives.read_text())
 def test_configuration_rewrite_during_preview_does_not_delete_successor(self):
  with tempfile.TemporaryDirectory() as t,ExitStack() as stack:
   p=Path(t);r=ClientIdentityRegistry(initialize_workspace(p/'workspace'),p/'registry');old=r.create('hermes','Hermes','hermes','stdio');r.delete(old['internal_id']);new=r.create('hermes','Hermes','hermes','stdio')
   a=cc.HermesAdapter();a.path=p/'config.yaml'
   def write(row):a.path.write_text(cc.yaml.safe_dump({'mcp_servers':{'knowledge_bridge':{'env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':'hermes','KNOWLEDGE_BRIDGE_CLIENT_SECRET':row['secret']}}}}),'utf-8')
   write(old)
   for key,value in {'ADAPTERS':{'hermes':a},'DATA':p/'state','BACKUPS':p/'backups','AUDIT':p/'audit.jsonl'}.items():stack.enter_context(patch.object(cc,key,value))
   original=cc.preview
   def racing(*args,**kwargs):write(new);return original(*args,**kwargs)
   stack.enter_context(patch.object(cc,'preview',racing))
   result=cc.remove_identity_configuration(r,old['internal_id']);self.assertEqual(['hermes'],result['skipped']);self.assertEqual(new['secret'],a.detect()['entry']['env']['KNOWLEDGE_BRIDGE_CLIENT_SECRET'])
if __name__=='__main__':unittest.main()
