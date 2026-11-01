"""Run real status-evidence functions against isolated synthetic config files."""
import ast,json,sys,types,unittest,hashlib,tempfile
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock,patch
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
SOURCE=ROOT/'knowledge_bridge/web.py'
names={'_client_configuration_current','_client_configuration_evidence','_client_read_verification_current'}
nodes=[node for node in ast.parse(SOURCE.read_text('utf-8')).body if isinstance(node,ast.FunctionDef) and node.name in names]
namespace={'__name__':'knowledge_bridge.web','__package__':'knowledge_bridge','Path':Path,'datetime':datetime,'timezone':timezone,
    'KnowledgeScope':lambda value:value,'ClientOperation':lambda value:value,'PolicyDecision':types.SimpleNamespace(ALLOW='allow')}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),'exec'),namespace)
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,1,12,0,tzinfo=timezone.utc)
namespace['datetime']=Clock
class Adapter:
    def __init__(self,path):self.path=path
    def validate(self,raw):json.loads(raw)
    def detect(self):
        value=json.loads(self.path.read_text('utf-8')) if self.path.is_file() else {}
        return {'config_exists':self.path.is_file(),'entry':value.get('entry')}
class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.json';self.adapter=Adapter(self.path)
        self.install=types.ModuleType('knowledge_bridge.client_installation');self.install.state=lambda _: 'present'
        self.config=types.ModuleType('knowledge_bridge.client_config');self.config.ADAPTERS={k:self.adapter for k in ('codex','hermes','dsh','cursor')}
        self.config._entry_identity=lambda entry:entry.get('env',{}).get('KNOWLEDGE_BRIDGE_CLIENT_ID') if isinstance(entry,dict) else None
        self.config._valid_entry=lambda entry,identity:bool(isinstance(entry,dict) and entry.get('command')=='synthetic')
        self.modules=patch.dict(sys.modules,{'knowledge_bridge.client_config':self.config,'knowledge_bridge.client_installation':self.install});self.modules.start();self.addCleanup(self.modules.stop)
        self.registry=Mock();self.registry.configured_secret_matches.side_effect=lambda internal,secret:secret=='synthetic-secret'
        self.row={'client_type':'codex','claimed_id':'synthetic-client','internal_id':'fixture','last_read_at':'2026-11-01T11:59:00+00:00','last_read_scope':'formal','last_read_tool':'get_document','status':'read_verified'}
        self.entry={'command':'synthetic','env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':'synthetic-client','KNOWLEDGE_BRIDGE_CLIENT_SECRET':'synthetic-secret'}}
    def save(self,entry):self.path.write_text(json.dumps({'entry':entry}),encoding='utf-8')
    def evidence(self,row=None):return namespace['_client_configuration_evidence'](row or self.row,self.registry)
    def recent(self,current):return namespace['_client_read_verification_current'](self.row,[],types.SimpleNamespace(_policy=lambda *_:'allow'),current)
    def test_valid_config_is_read_only_and_fresh_read_is_not_installation_evidence(self):
        self.save(self.entry);before=self.path.read_bytes();value=self.evidence()
        self.assertEqual(value,{'current':True,'present':True,'state':'valid'});self.assertTrue(self.recent(value['current']))
        self.assertEqual(self.path.read_bytes(),before);self.assertNotIn('synthetic-secret',json.dumps(value))
    def test_missing_file_and_missing_entry_are_distinct_from_mismatch(self):
        self.assertEqual(self.evidence()['state'],'missing')
        self.save(None);self.assertEqual(self.evidence()['state'],'missing')
        self.save({**self.entry,'env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':'other'}})
        value=self.evidence();self.assertEqual(value['state'],'mismatch');self.assertTrue(value['present']);self.assertFalse(value['current']);self.assertFalse(self.recent(value['current']))
    def test_codex_and_hermes_explicit_disabled_blocks_recent_read_without_rewriting(self):
        self.save({**self.entry,'enabled':False});before=self.path.read_bytes()
        for kind in ('codex','hermes'):
            value=self.evidence(self.row|{'client_type':kind});self.assertEqual(value['state'],'disabled');self.assertFalse(value['current']);self.assertFalse(self.recent(value['current']))
        self.assertEqual(self.path.read_bytes(),before)
    def test_recent_read_then_corrupt_config_is_invalid_and_no_longer_verified(self):
        self.save(self.entry);self.assertTrue(self.recent(self.evidence()['current']))
        self.path.write_text('{invalid',encoding='utf-8');before=self.path.read_bytes();value=self.evidence()
        self.assertEqual(value['state'],'invalid');self.assertFalse(value['current']);self.assertFalse(self.recent(value['current']));self.assertEqual(self.path.read_bytes(),before)
    def test_permission_denied_is_unknown_not_removed_or_uninstalled(self):
        self.save(self.entry)
        with patch.object(self.adapter,'detect',side_effect=PermissionError('synthetic')):value=self.evidence()
        self.assertIsNone(value['current']);self.assertIsNone(value['present']);self.assertEqual(value['state'],'unreadable')
    def test_unexpected_probe_error_is_unknown(self):
        with patch.object(self.adapter,'detect',side_effect=RuntimeError('synthetic')):value=self.evidence()
        self.assertEqual(value['state'],'unknown');self.assertIsNone(value['current'])
    def test_credential_store_error_does_not_claim_config_was_removed(self):
        self.save(self.entry);self.registry.configured_secret_matches.side_effect=FileNotFoundError('synthetic store')
        value=self.evidence();self.assertEqual(value['state'],'unreadable');self.assertTrue(value['present']);self.assertIsNone(value['current'])
    def test_manual_client_program_remnants_do_not_prove_config_or_connection(self):
        value=self.evidence(self.row|{'client_type':'opencode'});self.assertIsNone(value['current']);self.assertEqual(value['state'],'unknown')
        self.install.state=lambda _: 'absent';value=self.evidence(self.row|{'client_type':'workbuddy'})
        self.assertFalse(value['current']);self.assertIsNone(value['present']);self.assertEqual(value['state'],'unknown')
    def test_old_read_and_changed_permission_cannot_be_current(self):
        permissions=types.SimpleNamespace(_policy=lambda *_:'allow')
        old=self.row|{'last_read_at':'2026-10-01T11:59:00+00:00'}
        self.assertFalse(namespace['_client_read_verification_current'](old,[],permissions,True))
        changed=[{'updated_at':'2026-11-01T11:59:30+00:00'}]
        self.assertFalse(namespace['_client_read_verification_current'](self.row,changed,permissions,True))
    def test_installation_cache_expires_within_the_five_second_refresh_window(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('synthetic_installation',ROOT/'knowledge_bridge/client_installation.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module._CACHE['unsupported-fixture']=(100.0,'present')
        # Exercise the real supported path with the expensive discovery mocked.
        module._CACHE['workbuddy']=(100.0,'present')
        fake_os=types.SimpleNamespace(name='nt',getenv=lambda key:None)
        with patch.object(module.time,'monotonic',return_value=104.9):
            self.assertEqual(module.state('workbuddy'),'present')
        with patch.object(module.time,'monotonic',return_value=105.1),patch.object(module,'_registered',return_value='absent'),patch.object(module.shutil,'which',return_value=None),patch.object(module.Path,'home',return_value=Path(self.temp.name)),patch.object(module.Path,'is_file',return_value=False),patch.object(module.Path,'exists',return_value=False):
            self.assertEqual(module.state('workbuddy'),'absent')

if __name__=='__main__':unittest.main()
