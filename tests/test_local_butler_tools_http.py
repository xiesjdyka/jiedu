import json,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.butler_tools import ProductTools
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService

class LocalToolHTTPTests(unittest.TestCase):
    def test_owned_local_http_tools_use_real_tokenizer_each_round(self):
        calls=[];chat=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path,request,self.headers.get('Authorization')))
                if self.path=='/apply-template':result={'prompt':'synthetic template'}
                elif self.path=='/tokenize':result={'tokens':[1]*17}
                else:
                    chat.append(request);self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                    if len(chat)==1:
                        args=json.dumps({'operation':'workspace_status','arguments':'{}'})
                        delta={'tool_calls':[{'index':0,'id':'local-tool-1','function':{'name':'product_operation','arguments':args}}]};finish='tool_calls'
                    else:delta={'content':'合成工具回合完成。','reasoning_content':'Original mixed reasoning 保留。'};finish='stop'
                    self.wfile.write(('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})+'\n\ndata: [DONE]\n\n').encode());return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as root:
            context=initialize_workspace(Path(root)/'workspace');registry=ProductTools(context)
            permissions=ClientPermissionService(ClientIdentityRegistry(context,root=Path(root)/'identities'))
            permissions.set_butler_policy('workspace_status','allow',known_operations=registry.tools)
            runtime=SimpleNamespace(close=lambda:None,config=lambda:{'context_tokens':8192},status=lambda:{'state':'ready'},
                headers=lambda:{'Authorization':'Bearer synthetic-owned-key'},endpoint=lambda:'http://127.0.0.1:'+str(server.server_port))
            service=LocalButler(context,runtime=runtime,permissions=permissions,tool_registry=registry,start_worker=False)
            self.addCleanup(service.close);sid=service.create_session()['id'];jid=service.submit(sid,'synthetic tool question',tools_enabled=True)['id']
            saved={'snapshot':{},'sources':[],'budget':{'effective':{'max_output_tokens':512},'history_messages':0}}
            service._update(jid,state='running',context=json.dumps(saved));service.cancel_event=threading.Event()
            with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            provider=json.loads(job['requested'])['provider_snapshot']
            service._execute_local_tools(job,[{'role':'system','content':'synthetic system'},{'role':'user','content':'synthetic request'}],saved,provider)
            self.assertEqual(['/apply-template','/tokenize','/v1/chat/completions']*2,[row[0] for row in calls])
            self.assertTrue(all(row[2]=='Bearer synthetic-owned-key' for row in calls))
            self.assertTrue(all(row[1].get('tools') for row in calls if row[0] in ('/apply-template','/v1/chat/completions')))
            tool=next(row for row in chat[-1]['messages'] if row['role']=='tool');self.assertTrue(json.loads(tool['content'])['ok']);self.assertEqual('local-tool-1',tool['tool_call_id'])
            conversation=service.conversation(sid);self.assertEqual('completed',conversation['jobs'][0]['state'])
            self.assertEqual('合成工具回合完成。',conversation['messages'][-1]['content'])
            self.assertEqual('Original mixed reasoning 保留。',conversation['messages'][-1]['reasoning'])
            budget=service.saved_snapshot(jid)['budget']['last_round'];self.assertEqual(17,budget['input_tokens'])

if __name__=='__main__':unittest.main()
