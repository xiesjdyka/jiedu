import http.client
import json
import tempfile
import threading
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge.butler_provider import ButlerProviders, ProviderError
from knowledge_bridge.hermes_tool_bridge import HermesToolServer
from knowledge_bridge.hermes_direct_runner import install_http_send_gate
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.workspace import initialize_workspace


class Secrets:
    def get(self, _target):return ''


class HermesScopeTests(unittest.TestCase):
    def test_default_no_scope_and_scope_change_revises_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            context=SimpleNamespace(state=Path(folder),workspace_id='synthetic')
            providers=ButlerProviders(context,secrets=Secrets(),isolated=True)
            ready={'ready':True,'revision':'synthetic-hermes-revision','model':'fixture','provider':'deepseek'}
            with patch('knowledge_bridge.hermes_client.status',return_value=ready):
                selected=providers.select_hermes()
                self.assertEqual([],selected['hermes']['data_scopes'])
                revised=providers.set_hermes_data_scopes(['formal'])
                self.assertNotEqual(selected['revision'],revised['revision'])
                self.assertEqual(['formal'],revised['hermes']['data_scopes'])
                with self.assertRaises(ProviderError):providers.set_hermes_data_scopes(['protected'])
                providers.configure(selected='local')
                self.assertEqual(['formal'],providers._load()['hermes_data_scopes'])

    def test_hermes_mutation_requires_each_request_confirmation_even_if_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            context=initialize_workspace(Path(folder)/'workspace')
            permissions=ClientPermissionService(ClientIdentityRegistry(context,root=Path(folder)/'identities'))
            known={'candidate_submit':object()}
            permissions.set_butler_policy('candidate_submit','allow',known_operations=known)
            ticket=permissions.request_butler_operation('candidate_submit',{'fixture':'synthetic'},
                session='fixture-session',job='fixture-job',provider_revision='hermes-revision',
                known_operations=known,force_confirm=True)
            self.assertEqual('ask',ticket['decision'])
            with self.assertRaises(ValueError):permissions.confirm_butler_operation(ticket['id'],approved=True,duration='persistent')
            permissions.confirm_butler_operation(ticket['id'],approved=True,duration='request')


class BridgeTests(unittest.TestCase):
    def test_bearer_gate_and_closed_operation_set(self):
        class Runner:
            registry=SimpleNamespace(tools={})
            def __init__(self):self.calls=[]
            def dispatch_model(self,name,raw):self.calls.append((name,json.loads(raw)));return {'ok':True,'result':{'fixture':'safe'}}
        runner=Runner();server=HermesToolServer(runner).start()
        try:
            def call(operation,token):
                client=http.client.HTTPConnection('127.0.0.1',server.port,timeout=2)
                try:
                    body=json.dumps({'operation':operation,'arguments':'{}'}).encode()
                    client.request('POST','/operation',body=body,headers={'Authorization':'Bearer '+token})
                    response=client.getresponse();return response.status,response.read()
                finally:client.close()
            self.assertEqual(403,call('source_read','wrong')[0])
            status,body=call('folder_delete',server.token)
            self.assertEqual(200,status);self.assertEqual('unsupported',json.loads(body)['state'])
            self.assertEqual([],runner.calls)
            status,body=call('source_read',server.token)
            self.assertEqual(200,status);self.assertTrue(json.loads(body)['ok'])
            self.assertEqual('source_read',runner.calls[0][1]['operation'])
        finally:server.close()

    def test_second_http_attempt_after_revocation_never_reaches_provider(self):
        import httpx
        revoked=threading.Event();received=[]
        def gate():
            if revoked.is_set():raise PermissionError('synthetic permission revocation')
        server=HermesToolServer(send_gate=gate).start()
        original_sync=httpx.Client.send;original_async=httpx.AsyncClient.send
        try:
            with patch.dict(os.environ,{'JIEDU_TOOL_PORT':str(server.port),'JIEDU_TOOL_TOKEN':server.token}):
                install_http_send_gate()
                def provider(request):
                    received.append(request.url.path)
                    revoked.set()
                    return httpx.Response(503)
                with httpx.Client(transport=httpx.MockTransport(provider)) as client:
                    self.assertEqual(503,client.get('https://synthetic.example/first').status_code)
                    with self.assertRaises(InterruptedError):client.get('https://synthetic.example/retry')
            self.assertEqual(['/first'],received)
            self.assertEqual(1,server.rejected_send_count)
        finally:
            httpx.Client.send=original_sync;httpx.AsyncClient.send=original_async
            server.close()


class HermesUiTests(unittest.TestCase):
    def test_unsaved_scope_choice_survives_runtime_refresh(self):
        import shutil
        import subprocess
        node=shutil.which('node')
        if not node:self.skipTest('Node.js unavailable')
        source=(Path(__file__).resolve().parents[1]/'static/butler-controls.js').read_text('utf-8')
        snippet=source.split('  let hermesScopeDraftDirty=',1)[1].split('  let chosenPath=',1)[0]
        snippet='let hermesScopeDraftDirty='+snippet
        harness=r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const fields=['formal','evidence','inbox','selected_folders'].map(name=>({dataset:{hermesOutgoingScope:name},checked:false}));
const listeners={};const hermesPanel={addEventListener:(name,fn)=>listeners[name]=fn,querySelectorAll:()=>fields};
const note={textContent:''};const context={hermesPanel,provider:{selected:'hermes',revision:'r1',hermes:{data_scopes:['formal']}},scopes:{formal:'formal',evidence:'evidence'},q:()=>note,document:{addEventListener:(name,fn)=>listeners[name]=fn}};
vm.createContext(context);vm.runInContext(SNIPPET,context);
listeners['butler-model-state']();assert.equal(fields[0].checked,true);
fields[1].checked=true;listeners.change({target:{matches:()=>true}});
listeners['butler-model-state']();assert.equal(fields[1].checked,true);
context.provider={selected:'hermes',revision:'r2',hermes:{data_scopes:['formal']}};
listeners['butler-model-state']();assert.equal(fields[1].checked,false);
'''
        result=subprocess.run([node,'-e','const SNIPPET='+json.dumps(snippet)+';'+harness],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
