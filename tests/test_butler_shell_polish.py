import json,tempfile,threading,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.butler_provider import ButlerProviders,ProviderError
from knowledge_bridge.butler_network import WebSettings
from knowledge_bridge.butler_tools import ProductTools,ToolRunner
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.local_butler import LocalButler

class Secrets:
    def __init__(self):self.values={}
    def put(self,key,value):self.values[key]=value
    def get(self,key):return self.values.get(key,'')
    def delete(self,key):self.values.pop(key,None)

def saved_job(service,jid):
    with service.db() as db:return json.loads(db.execute('SELECT context FROM jobs WHERE id=?',(jid,)).fetchone()[0])

class ShellPolishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.ctx=initialize_workspace(Path(self.temp.name)/'workspace');self.secrets=Secrets();self.calls=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.calls.append(('GET',self.path,self.headers.get('Authorization')));data=json.dumps({'data':[{'id':'fixture','name':'Synthetic model','context_window':2000000,'max_output_tokens':64000}]}).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_POST(self):
                row=json.loads(self.rfile.read(int(self.headers['Content-Length'])));owner.calls.append(('POST',row));self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                first=not any(m.get('role')=='tool' for m in row['messages'])
                delta={'tool_calls':[{'index':0,'id':'call-1','function':{'name':'product_operation','arguments':json.dumps({'operation':'workspace_status','arguments':'{}'})}}]} if first and row.get('tools') else {'content':'合成回答'}
                event={'choices':[{'delta':delta,'finish_reason':'tool_calls' if 'tool_calls' in delta else 'stop'}]}
                if first:event['search_info']={'search_results':[{'title':'合成来源','url':'https://example.com/fixture'}]}
                self.wfile.write(('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown);self.url='http://127.0.0.1:'+str(self.server.server_port)+'/v1'
        self.providers=ButlerProviders(self.ctx,secrets=self.secrets,isolated=True);self.network=WebSettings(self.ctx,secrets=self.secrets,isolated=True);self.registry=ProductTools(self.ctx,network=self.network);self.permissions=ClientPermissionService(ClientIdentityRegistry(self.ctx,root=Path(self.temp.name)/'identities'))
    def configure(self,**extra):return self.providers.configure(selected='external',base_url=self.url,model='fixture',api_key='synthetic-key',dialect='dashscope',tools=True,context_tokens=65536,**extra)
    def test_model_metadata_fetch_no_secret_or_configuration_save(self):
        result=self.providers.list_models(base_url=self.url,api_key='temporary-key');self.assertEqual(2000000,result['metadata'][0]['context_window']);self.assertEqual([('GET','/v1/models','Bearer temporary-key')],self.calls);self.assertFalse(self.providers.path.exists());self.assertEqual({},self.secrets.values)
    def test_native_search_save_only_preserves_all_other_fields_and_rejects_local(self):
        before=self.configure()['external'];after=self.providers.set_native_search(True)['external'];self.assertEqual({k:v for k,v in before.items() if k!='native_search'},{k:v for k,v in after.items() if k!='native_search'});self.providers.configure(selected='local');snapshot=self.providers.path.read_bytes()
        with self.assertRaises(ProviderError):self.providers.set_native_search(True)
        self.assertEqual(snapshot,self.providers.path.read_bytes())
    def test_group_ask_and_deny_preserve_other_policies(self):
        groups=self.registry.permission_groups();all_ops=[op for g in groups for op in g['operations']];self.assertEqual(set(self.registry.tools),set(all_ops));self.assertEqual(len(all_ops),len(set(all_ops)));read=next(g for g in groups if g['id']=='read')['operations'];self.permissions.set_butler_policy('web_search','allow',known_operations=self.registry.tools);self.permissions.set_butler_group(read,True,known_operations=self.registry.tools);self.assertTrue(all(self.permissions.butler_policies()['policies'][op]=='ask' for op in read));self.permissions.set_butler_group(read,False,known_operations=self.registry.tools);self.assertTrue(all(self.permissions.butler_policies()['policies'][op]=='deny' for op in read));self.assertEqual('allow',self.permissions.butler_policies()['policies']['web_search'])
    def test_native_plus_tools_real_http_retains_sources_and_no_replay_metadata(self):
        self.configure(native_search=True);self.network.configure(enabled=True)
        for op in ['web_search','workspace_status']:self.permissions.set_butler_policy(op,'allow',known_operations=self.registry.tools)
        service=LocalButler(self.ctx,providers=self.providers,permissions=self.permissions,tool_registry=self.registry,start_worker=False);self.addCleanup(service.close);sid=service.create_session()['id'];jid=service.submit(sid,'你好',tools_enabled=True)['id'];service.cancel_event=threading.Event();service._update(jid,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        service._execute_external(job,json.loads(job['requested'])['provider_snapshot']);posts=[r[1] for r in self.calls if r[0]=='POST'];self.assertEqual(2,len(posts));self.assertTrue(all(r['enable_search'] is True for r in posts));self.assertTrue(all('provider_search_sources' not in m for r in posts for m in r['messages']));saved=saved_job(service,jid);self.assertEqual('sources_returned',saved['provider_search']['execution']);self.assertIn('https://example.com/fixture',json.dumps(saved['provider_search']));self.assertEqual('completed',service.conversation(sid)['jobs'][0]['state']);self.assertTrue(saved['activity'])
    def test_native_search_network_and_private_context_denied_before_http(self):
        self.configure(native_search=True);self.permissions.set_butler_policy('web_search','allow',known_operations=self.registry.tools)
        runner=ToolRunner(self.registry,self.permissions,session='fixture',job='fixture',provider=self.providers.snapshot(),cancel=threading.Event(),provider_current=lambda:True)
        with self.assertRaises(PermissionError):runner.authorize_native_search()
        self.network.configure(enabled=True);runner=ToolRunner(self.registry,self.permissions,session='fixture',job='fixture',provider=self.providers.snapshot(),cancel=threading.Event(),provider_current=lambda:True,initial_private=True)
        with self.assertRaises(PermissionError):runner.authorize_native_search()
        self.assertEqual([],self.calls)

if __name__=='__main__':unittest.main()
