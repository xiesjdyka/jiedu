import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge import client_config

class GenericPairingTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.context=initialize_workspace(self.root/'中文 空间')
  self.registry=ClientIdentityRegistry(self.context,self.root/'identities');self.permissions=ClientPermissionService(self.registry);self.manager=PairingTransactionManager(self.registry,self.permissions)
 def begin(self):return self.manager.begin('generic_fixture','虚构通用客户端','custom','stdio',{'formal':'allow','evidence':'allow','inbox':'deny','formal_write':'ask','archive_write':'deny'})
 def test_cancel_and_delivery_without_adapter_or_cwd(self):
  with patch('knowledge_bridge.client_pairing.config_preview') as adapter:
   result=self.begin();adapter.assert_not_called();self.assertNotIn(result['secret'],str(result['delivery']));self.assertNotIn('cwd',result['delivery']);self.assertTrue(Path(result['delivery']['args'][0]).is_absolute())
   self.assertEqual([],self.registry.list_clients());self.manager.cancel(result['pairing_id']);self.assertEqual([],self.registry.list_clients())
 def test_failure_rolls_back_identity_and_policies(self):
  result=self.begin()
  with patch.object(self.permissions,'set_policy',side_effect=OSError('fixture failure')):
   with self.assertRaises(OSError):self.manager.commit(result['pairing_id'])
  self.assertEqual([],self.registry.list_clients());self.assertEqual([],self.permissions.policies(result['client']['internal_id']))
 def test_status_rotation_and_no_brand_restriction(self):
  preview=self.begin();committed=self.manager.commit(preview['pairing_id']);key=committed['client']['internal_id'];self.assertEqual('awaiting_configuration',committed['client']['status'])
  self.registry.acknowledge_configuration(key);identity=self.registry.authenticate('generic_fixture',preview['secret'],mark_seen=False);self.assertEqual('paired_not_seen',self.registry.list_clients()[0]['status'])
  self.permissions.authorize(identity,'formal','read');self.assertEqual('authenticated',self.registry.list_clients()[0]['status'])
  self.registry.mark_read_success(identity,'formal','get_document');row=self.registry.list_clients()[0];self.assertEqual('read_verified',row['status']);self.assertEqual(('formal','get_document'),(row['last_read_scope'],row['last_read_tool']))
  rotation=self.manager.begin_rotation(key);self.assertNotIn('secret',rotation);changed=self.manager.commit_rotation(rotation['rotation_id']);self.assertFalse(self.registry.authenticate('generic_fixture',preview['secret']).authenticated);self.assertTrue(self.registry.authenticate('generic_fixture',changed['secret']).authenticated)
 def test_http_refused_without_identity_and_invalid_policies(self):
  with self.assertRaisesRegex(ValueError,'HTTP'):self.manager.begin('generic_fixture','测试','custom','localhost_http')
  with self.assertRaises(ValueError):self.manager.begin('generic_fixture','测试','custom','stdio',{'protected':'allow'})
  self.assertEqual([],self.registry.list_clients())
 def test_hermes_existing_adapter_transaction(self):
  adapter=client_config.HermesAdapter();adapter.path=self.root/'hermes.yaml';adapter.path.write_text('display: fictional\n','utf-8')
  with patch.dict(client_config.ADAPTERS,{'hermes':adapter}),patch.object(client_config,'BACKUPS',self.root/'backups'),patch.object(client_config,'AUDIT',self.root/'audit.jsonl'):
   preview=self.manager.begin('hermes_fixture','虚构Hermes','hermes','stdio');self.manager.commit(preview['pairing_id']);self.assertIn(preview['secret'],adapter.path.read_text('utf-8'))

if __name__=='__main__':unittest.main()
