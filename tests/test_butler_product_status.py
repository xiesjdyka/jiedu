import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import knowledge_bridge.product_status as status_module
import knowledge_bridge.source_control as source_control
from knowledge_bridge.hermes_tool_bridge import ALLOWED
from knowledge_bridge.butler_tools import ProductTools,ToolRunner
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.workspace import initialize_workspace


class ButlerProductStatusTests(unittest.TestCase):
    def test_product_status_only_returns_public_counts(self):
        registry=SimpleNamespace(list_clients=lambda:[{'client_type':'claude','claimed_id':'private-id',
            'display_name':'private-title','secret':'private-secret','status':'authenticated',
            'verified_at':'2026-01-01','last_read_at':None}])
        permissions=SimpleNamespace(registry=registry,butler_policies=lambda:{'policies':{'a':'allow','b':'ask','c':'deny'}})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'software-update').mkdir()
            (root/'knowledge_bridge').mkdir()
            (root/'knowledge_bridge'/'update-channel.json').write_text('{"manifest_url":"https://updates.example.invalid/manifest"}','utf-8')
            (root/'software-update'/'last-check.json').write_text(json.dumps({'state':'available',
                'latest':'1.2.0','private_path':'C:/private/title'}),'utf-8')
            with patch.object(status_module,'PROGRAM_ROOT',root), \
                 patch.object(status_module,'local_path',side_effect=lambda *p:root.joinpath(*p)), \
                 patch.object(status_module,'version_info',return_value={'product_version':'1.1.8'}), \
                 patch('knowledge_bridge.online_update.OnlineUpdate._helper_outcome',return_value={}), \
                 patch('knowledge_bridge.memory_control.interface_state',return_value={'mcp_enabled':True}):
                result=status_module.product_status(permissions)
        result_text=json.dumps(result,ensure_ascii=False)
        self.assertEqual(1,result['connection']['claude']['authenticated'])
        self.assertEqual({'allow':1,'ask':1,'deny':1},result['permissions']['butler_decisions'])
        self.assertEqual('1.2.0',result['update']['latest'])
        self.assertEqual({'state':'unknown','id':None},result['maintenance_build'])
        for private in ('private-id','private-title','private-secret','C:/private/title'):
            self.assertNotIn(private,result_text)

    def test_update_receipt_overrides_stale_workflow_without_resuming(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'knowledge_bridge').mkdir();(root/'software-update').mkdir()
            (root/'knowledge_bridge'/'update-channel.json').write_text('{"manifest_url":"https://updates.example.invalid/m"}','utf-8')
            (root/'software-update'/'last-check.json').write_text('{"state":"available","latest":"1.2.0"}','utf-8')
            (root/'software-update'/'workflow.json').write_text('{"state":"queued"}','utf-8')
            with patch.object(status_module,'PROGRAM_ROOT',root), \
                 patch.object(status_module,'local_path',side_effect=lambda *p:root.joinpath(*p)), \
                 patch('knowledge_bridge.online_update.OnlineUpdate._helper_outcome',return_value={'state':'complete','message':'C:/private'}):
                result=status_module._update_summary()
            self.assertEqual('complete',result['state'])
            self.assertEqual('complete',result['receipt_state'])
            self.assertEqual('1.2.0',result['latest'])
            self.assertNotIn('C:/private',json.dumps(result))

    def test_archive_progress_allowlists_fields_and_keeps_unknown_unknown(self):
        raw={'sources':[{'id':'codex','detected':True,'readable':True,'configured':True,'paused':False,
            'locations':[{'path':'C:/private/session'}], 'last_error':'C:/private/error',
            'history':{'status':'reading','unit':'records','archived_unit':'messages','known_total':None,'checked':2,'archived':5,
                       'by_source':[{'source':'jsonl','unit':'files','archived_unit':'messages','known_total':3,'checked':2,'archived':5,
                                     'title':'secret-title','last_error':'secret-error'}]},
            'live':{'status':'watching','saved_messages':1,'last_message_at':'private-time'}}]}
        with patch('knowledge_bridge.local_source_status.status',return_value=raw):
            result=status_module.source_archive_progress()
        self.assertEqual(['codex','hermes','dsh','claude','cursor'],[row['id'] for row in result['sources']])
        self.assertIsNone(result['sources'][0]['history']['known_total'])
        self.assertEqual('records',result['sources'][0]['history']['unit'])
        self.assertEqual('files',result['sources'][0]['history']['by_source'][0]['unit'])
        self.assertEqual('messages',result['sources'][0]['history']['by_source'][0]['archived_unit'])
        self.assertEqual('unknown',result['sources'][1]['history']['status'])
        text=json.dumps(result,ensure_ascii=False)
        for private in ('C:/private','secret-title','secret-error','private-time'):
            self.assertNotIn(private,text)

    def test_resume_requires_existing_paused_state_and_preserves_codex_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'codex-source-adapter.json'
            class Adapter:
                state_path=path
                codex_home=Path(temporary)/'private-codex-source'
                def _state(self):return {'archive_policy':'paused','resume_policy':'selected',
                                         'selected_session_ids':['old-session'],'excluded_session_ids':['excluded']}
                def configure(self,**kwargs):
                    self.called=kwargs
                    return {'archive_policy':'selected'}
            instance=Adapter()
            instance.codex_home.mkdir()
            with patch('knowledge_bridge.codex_source_adapter.CodexSourceAdapter',return_value=instance):
                with self.assertRaises(ValueError):source_control.resume_preview('codex',None)
                path.write_text('{}','utf-8')
                preview=source_control.resume_preview('codex',None)
                self.assertEqual(1,preview['selected_count'])
                self.assertNotIn(str(instance.codex_home),json.dumps(preview))
                result=source_control.resume_existing('codex',None,expected=preview)
            self.assertEqual('resumed',result['state'])
            self.assertEqual(True,instance.called['enabled'])
            self.assertEqual(True,instance.called['owner_confirmed'])
            self.assertTrue(callable(instance.called['validate_before_write']))
            self.assertTrue(result['cursor_preserved'])

    def test_resume_rejects_scope_or_source_change_after_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'codex-source-adapter.json';path.write_text('{"cursor":4}','utf-8')
            class Adapter:
                state_path=path
                codex_home=Path(temporary)/'original-source'
                def _state(self):return {'archive_policy':'paused','resume_policy':'all',
                                         'selected_session_ids':[],'excluded_session_ids':[]}
                def configure(self,**kwargs):raise AssertionError('must not resume')
            instance=Adapter()
            instance.codex_home.mkdir()
            with patch('knowledge_bridge.codex_source_adapter.CodexSourceAdapter',return_value=instance):
                preview=source_control.resume_preview('codex',None)
                path.write_text('{"cursor":5}','utf-8')
                with self.assertRaises(source_control.ResumeBlocked) as changed:
                    source_control.resume_existing('codex',None,expected=preview)
                self.assertEqual('scope_changed',changed.exception.code)
                path.write_text('{"cursor":4}','utf-8')
                instance.codex_home=Path(temporary)/'new-source'
                instance.codex_home.mkdir()
                with self.assertRaises(ValueError):source_control.resume_existing('codex',None,expected=preview)

    def test_codex_resume_rechecks_scope_inside_adapter_write_lock(self):
        from knowledge_bridge.codex_source_adapter import CodexSourceAdapter
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);context=initialize_workspace(root/'workspace')
            codex=root/'codex';codex.mkdir()
            path=context.sync/'codex-source-adapter.json'
            path.write_text(json.dumps({'archive_policy':'paused','resume_policy':'selected',
                'selected_session_ids':['original-session'],'excluded_session_ids':[],
                'sessions':{'original-session':{'confirmed_offset':17}}}),'utf-8')
            original=CodexSourceAdapter.configure
            entered=[]
            def race_before_lock(adapter,*args,**kwargs):
                entered.append('configure')
                changed=json.loads(path.read_text('utf-8'))
                changed['resume_policy']='all'
                path.write_text(json.dumps(changed),'utf-8')
                return original(adapter,*args,**kwargs)
            with patch.dict(os.environ,{'CODEX_HOME':str(codex)}), \
                 patch.object(CodexSourceAdapter,'configure',race_before_lock), \
                 patch.object(CodexSourceAdapter,'_configure',side_effect=AssertionError('scope widened')):
                preview=source_control.resume_preview('codex',context)
                with self.assertRaises(source_control.ResumeBlocked) as blocked:
                    source_control.resume_existing('codex',context,expected=preview)
            self.assertEqual('scope_changed',blocked.exception.code)
            self.assertEqual(['configure'],entered)
            current=json.loads(path.read_text('utf-8'))
            self.assertEqual('paused',current['archive_policy'])
            self.assertEqual(17,current['sessions']['original-session']['confirmed_offset'])

    def test_dsh_resume_reuses_exact_saved_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'dsh-source-adapter.json';path.write_text('{}','utf-8')
            class Adapter:
                state_path=path
                root=Path(temporary)/'private-dsh-source'
                def _load(self):return {'policy':'paused','resume_policy':'selected',
                                        'selected_session_ids':['old-session'],
                                        'selected_sources':{'old-session':str(self.root/'old.jsonl')},
                                        'sessions':{'old-session':{'confirmed_offset':42,'source_path':str(self.root/'old.jsonl')}}}
                def discover(self):return [{'session_id':'old-session','path':str(self.root/'old.jsonl')}]
                def configure(self,*args,**kwargs):
                    self.called=(args,kwargs)
                    return {'policy':'selected'}
            instance=Adapter()
            instance.root.mkdir();(instance.root/'old.jsonl').write_text('fixture','utf-8')
            with patch('knowledge_bridge.dsh_source_adapter.DSHSourceAdapter',return_value=instance):
                preview=source_control.resume_preview('dsh',None)
                result=source_control.resume_existing('dsh',None,expected=preview)
            self.assertEqual(('selected',('old-session',)),instance.called[0])
            self.assertEqual(True,instance.called[1]['owner_confirmed'])
            self.assertTrue(callable(instance.called[1]['validate_before_write']))
            self.assertEqual('resumed',result['state'])
            moved=instance.root/'moved.jsonl';moved.write_text('same id, new source','utf-8')
            instance.discover=lambda:[{'session_id':'old-session','path':str(moved)}]
            with patch('knowledge_bridge.dsh_source_adapter.DSHSourceAdapter',return_value=instance):
                with self.assertRaises(ValueError):source_control.resume_preview('dsh',None)

    def test_claude_resume_requires_original_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state_path=root/'claude-source-adapter.json';state_path.write_text('{}','utf-8')
            leveldb=root/'cache';leveldb.mkdir();(leveldb/'CURRENT').write_text('current','utf-8')
            adapter=SimpleNamespace(state_path=state_path,profile_id='store',leveldb=leveldb,
                                    _load=lambda:{'policy':'paused','resume_policy':'all',
                                                  'profile_id':'desktop','selected_session_ids':[]})
            with patch('knowledge_bridge.claude_source_adapter.ClaudeSourceAdapter',return_value=adapter):
                with self.assertRaises(ValueError):source_control.resume_preview('claude',None)

    def test_hermes_same_path_replacement_is_not_original_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);db=root/'state.db';db.write_bytes(b'original')
            state_path=root/'hermes-adapter.json';state_path.write_text('{}','utf-8')
            stat=db.stat()
            state={'db_path':str(db),'history':{'status':'paused'},
                   'source_identity':{'device':stat.st_dev,'inode':stat.st_ino}}
            with patch('knowledge_bridge.hermes_session_adapter.STATE',state_path), \
                 patch('knowledge_bridge.hermes_session_adapter._load',return_value=state), \
                 patch('knowledge_bridge.hermes_session_adapter.selected_db_path',return_value=db):
                preview=source_control.resume_preview('hermes',None)
                replacement=root/'replacement.db';replacement.write_bytes(b'replacement')
                os.replace(replacement,db)
                with self.assertRaises(ValueError):source_control.resume_existing('hermes',None,expected=preview)

    def test_status_only_codex_adapter_never_recovers_archive_temps(self):
        from knowledge_bridge.archive_store import ArchiveStore
        from knowledge_bridge.codex_source_adapter import CodexSourceAdapter
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);context=initialize_workspace(root/'workspace')
            with patch.object(ArchiveStore,'recover_orphaned_manifest_temps',side_effect=AssertionError('recovery is a write')):
                adapter=CodexSourceAdapter(context,codex_home=root/'codex',machine_data=root/'machine',status_only=True)
            self.assertEqual([],adapter.store.recovery_report)

    def test_external_and_hermes_dispatch_names_are_wired(self):
        for name in ('product_status','source_archive_progress','source_archive_resume'):
            self.assertIn(name,ALLOWED)

    def test_external_product_status_needs_no_private_data_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            permissions=ClientPermissionService(ClientIdentityRegistry(context,root/'machine'))
            with patch('knowledge_bridge.product_status.product_status',return_value={'public':'status'}):
                tools=ProductTools(context)
            permissions.set_butler_policy('product_status','allow',known_operations=tools.tools)
            runner=ToolRunner(tools,permissions,session='fixture-session',job='fixture-job',
                provider={'selected':'external','external':{'data_scopes':[]},'revision':'fixture'},
                cancel=threading.Event(),provider_current=lambda:True)
            result=runner.dispatch('product_status','{}')
            self.assertTrue(result['ok'])
            self.assertEqual({'public':'status'},result['result'])
            self.assertEqual([],runner.receipts[0]['data_scopes'])

    def test_upgrade_defaults_allow_only_public_diagnostics_and_keep_owner_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            permissions=ClientPermissionService(ClientIdentityRegistry(context,root/'machine'))
            with patch('knowledge_bridge.product_status.source_archive_progress',return_value={'sources':[]}):
                tools=ProductTools(context)
            tools.permissions=permissions
            policy=permissions.butler_policies()
            self.assertEqual('allow',policy['policies']['product_status'])
            self.assertEqual('allow',policy['policies']['source_archive_progress'])
            self.assertNotIn('source_archive_resume',policy['policies'])
            self.assertEqual({},permissions._load()['butler']['policies'])
            runner=ToolRunner(tools,permissions,session='fixture-session',job='fixture-job',
                provider={'selected':'external','external':{'data_scopes':[]},'revision':'fixture'},
                cancel=threading.Event(),provider_current=lambda:True)
            self.assertTrue(runner.dispatch('source_archive_progress','{}')['ok'])
            self.assertEqual([],runner.receipts[-1]['data_scopes'])
            self.assertEqual('denied',runner.dispatch('source_archive_resume','{"source":"codex"}')['state'])
            permissions.set_butler_policy('product_status','deny',known_operations=tools.tools)
            self.assertEqual('deny',permissions.butler_policies()['policies']['product_status'])
            runner=ToolRunner(tools,permissions,session='fixture-session',job='fixture-job',
                provider={'selected':'external','external':{'data_scopes':[]},'revision':'fixture'},
                cancel=threading.Event(),provider_current=lambda:True)
            self.assertEqual('denied',runner.dispatch('product_status','{}')['state'])
            permissions.set_butler_policy('source_archive_progress','ask',known_operations=tools.tools)
            self.assertEqual('ask',permissions.butler_policies()['policies']['source_archive_progress'])
            ticket=permissions.request_butler_operation('source_archive_progress',{},session='other',job='other',
                provider_revision='fixture',known_operations=tools.tools)
            self.assertEqual('ask',ticket['decision'])
            permissions.revoke_butler_session('other')
            self.assertEqual('revoked',permissions.butler_request_state(ticket['id']))

    def test_resume_waits_for_owner_even_when_policy_is_allow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            context=initialize_workspace(root/'workspace')
            permissions=ClientPermissionService(ClientIdentityRegistry(context,root/'machine'))
            calls=[]
            with patch('knowledge_bridge.source_control.resume_preview',return_value={'source':'codex','fingerprint':'fixture'}), \
                 patch('knowledge_bridge.source_control.resume_existing',side_effect=lambda source,ctx,expected:
                       calls.append((source,expected)) or {'source':source,'state':'resumed'}):
                tools=ProductTools(context)
            permissions.set_butler_policy('source_archive_resume','allow',known_operations=tools.tools)
            runner=ToolRunner(tools,permissions,session='fixture-session',job='fixture-job',
                provider={'selected':'external','external':{'data_scopes':[]},'revision':'fixture'},
                cancel=threading.Event(),provider_current=lambda:True)
            results=[]
            thread=threading.Thread(target=lambda:results.append(runner.dispatch('source_archive_resume','{"source":"codex"}')))
            thread.start()
            try:
                pending=[]
                for _ in range(100):
                    pending=permissions.butler_policies()['pending']
                    if pending:break
                    time.sleep(.01)
                self.assertEqual([],calls)
                self.assertEqual(1,len(pending))
                with self.assertRaises(ValueError):
                    permissions.confirm_butler_operation(pending[0]['id'],approved=True,duration='persistent')
                permissions.confirm_butler_operation(pending[0]['id'],approved=True,duration='request')
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual([('codex',{'source':'codex','fingerprint':'fixture'})],calls)
                self.assertTrue(results[0]['ok'])
            finally:
                runner.cancel.set()
                thread.join(3)

    def test_catalog_maps_new_status_tools_to_relevant_topics(self):
        with tempfile.TemporaryDirectory() as temporary:
            tools=ProductTools(initialize_workspace(Path(temporary)/'workspace'))
            features={row['id']:row for row in tools.catalog()['features']}
            self.assertIn('source_archive_progress',features['sources']['operations'])
            self.assertIn('product_status',features['package']['operations'])


if __name__=='__main__':unittest.main()
