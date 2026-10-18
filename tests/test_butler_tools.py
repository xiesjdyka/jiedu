import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_butler_provider as provider_fixture
from knowledge_bridge import memory_pipeline as memory
from knowledge_bridge.butler_provider import CompatibleTransport
from knowledge_bridge.butler_tools import ProductTools,ToolRunner
from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.workspace import initialize_workspace


class ToolTests(unittest.TestCase):
    configure=provider_fixture.ProviderTests.configure
    def setUp(self):
        provider_fixture.ProviderTests.setUp(self)
        self.context=initialize_workspace(Path(self.temp.name)/'workspace')
        self.registry=ProductTools(self.context)
        self.permissions=ClientPermissionService(ClientIdentityRegistry(self.context,root=Path(self.temp.name)/'identities'))
        self.activity=[];self.cancel=threading.Event()

    def policy(self,operation,decision):self.permissions.set_butler_policy(operation,decision,known_operations=self.registry.tools)
    def runner(self):
        return ToolRunner(self.registry,self.permissions,session='session-1',job='job-1',provider=self.provider.snapshot(),
            cancel=self.cancel,provider_current=lambda:True,activity=self.activity.append)
    def call(self,runner,operation,args):
        return runner.dispatch_model('product_operation',json.dumps({'operation':operation,'arguments':json.dumps(args)}))

    def test_real_http_nested_candidate_raw_quote_is_not_outbound(self):
        self.configure(data_scopes=['formal','inbox']);self.policy('candidate_detail','allow')
        with memory.in_workspace(self.context):
            candidate=memory.create_candidate({'type':'knowledge','title':'synthetic title','content':'synthetic proposal',
                'evidence':[{'source_path':'90_原始档案/fixture.md','quote':'RAW_SECRET_SENTINEL'}]})['candidate']
        runner=self.runner()
        def fragments(request):
            if len(request['messages'])==1:
                args={'operation':'candidate_detail','arguments':json.dumps({'candidate_id':candidate['candidate_id']})}
                return [({'tool_calls':[{'index':0,'id':'privacy-1','function':{'name':'product_operation','arguments':json.dumps(args)}}]},'tool_calls')]
            return [({'content':'未发送未授权的证据'},'stop')]
        self.fragments=fragments
        transport=CompatibleTransport(self.provider,self.provider.snapshot(),cancel=self.cancel,before_send=runner.before_send)
        transport.run([{'role':'user','content':'fixture question'}],dispatch=runner.dispatch_model,tools=self.registry.model_tools())
        self.assertEqual(2,len(self.requests));self.assertNotIn('RAW_SECRET_SENTINEL',json.dumps(self.requests))
        self.assertIn('RAW_SECRET_SENTINEL',json.dumps(self.activity))
        self.assertEqual('outbound_denied',self.activity[-1]['state'])
        # Formal-only scope fails even before reading the unreviewed candidate.
        self.configure(data_scopes=['formal']);runner=self.runner()
        self.assertEqual('outbound_denied',self.call(runner,'candidate_detail',{'candidate_id':candidate['candidate_id']})['state'])

    def test_deny_and_invalid_model_grants_do_not_execute(self):
        self.configure();called=[]
        self.registry.add('fixture_write','synthetic write','fixture',{'content':(str,...)},lambda args,check:called.append(args),mutation=True)
        runner=self.runner()
        self.assertEqual('denied',self.call(runner,'fixture_write',{'content':'fixture'})['state'])
        self.policy('fixture_write','allow');runner=self.runner()
        self.assertFalse(self.call(runner,'fixture_write',{'content':'fixture','owner_confirmed':True})['ok'])
        self.assertEqual([],called)

    def test_missing_empty_and_absolute_evidence_paths_do_not_leak(self):
        self.configure(data_scopes=['formal','inbox','selected_folders']);self.policy('candidate_detail','allow')
        for index,path in enumerate((None,'',str(self.context.raw_archive/'fixture.md'),str(Path(self.temp.name)/'outside'/'90_原始档案'/'fixture.md'))):
            with self.subTest(path=path):
                evidence={'quote':'UNCLASSIFIED_RAW_SENTINEL_'+str(index)}
                if path is not None:evidence['source_path']=path
                with memory.in_workspace(self.context):
                    candidate=memory.create_candidate({'type':'knowledge','title':'fixture','content':'fixture-'+str(index),'evidence':[evidence]})['candidate']
                runner=self.runner();reply=self.call(runner,'candidate_detail',{'candidate_id':candidate['candidate_id']})
                self.assertNotIn('UNCLASSIFIED_RAW_SENTINEL',json.dumps(reply));self.assertEqual('result_withheld',reply['state'])

    def test_owner_confirmation_wait_cancel_and_session_revoke(self):
        self.configure();self.policy('workspace_status','ask');runner=self.runner();results=[]
        worker=threading.Thread(target=lambda:results.append(self.call(runner,'workspace_status',{})));worker.start()
        deadline=time.monotonic()+2
        while not self.activity and time.monotonic()<deadline:time.sleep(.01)
        request=self.activity[-1]['request_id']
        self.assertTrue(worker.is_alive());self.permissions.confirm_butler_operation(request,approved=True,duration='session')
        worker.join(2);self.assertTrue(results[0]['ok'])
        self.permissions.revoke_butler_session('session-1')
        with self.assertRaises(ValueError):runner.before_send([])
        runner=self.runner();self.activity.clear();errors=[]
        def execute():
            try:self.call(runner,'workspace_status',{})
            except InterruptedError as error:errors.append(error)
        worker=threading.Thread(target=execute);worker.start()
        deadline=time.monotonic()+2
        while not self.activity and time.monotonic()<deadline:time.sleep(.01)
        self.cancel.set();worker.join(1);self.assertFalse(worker.is_alive());self.assertEqual(1,len(errors))

    def test_workspace_binding_nested_exception_and_two_thread_workers(self):
        other=initialize_workspace(Path(self.temp.name)/'other')
        def create(context,text):
            with memory.in_workspace(context):
                return memory.create_candidate({'type':'knowledge','title':'fixture','content':text,'evidence':[{'quote':text}]})['candidate']['candidate_id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(create,self.context,'workspace-A');b=pool.submit(create,other,'workspace-B')
            a.result();b.result()
        with memory.in_workspace(self.context):
            self.assertEqual(['workspace-A'],[row['content'] for row in memory.list_candidates()])
            with self.assertRaises(RuntimeError):
                with memory.in_workspace(other):
                    self.assertEqual(['workspace-B'],[row['content'] for row in memory.list_candidates()]);raise RuntimeError('fixture unwind')
            self.assertEqual(['workspace-A'],[row['content'] for row in memory.list_candidates()])
        # New threads must receive an explicit context. There is no claim that ContextVar propagates itself.
        self.assertIsNone(memory._candidate_workspace.get())

    def test_real_http_read_candidate_preview_owner_write_and_undo(self):
        self.configure(data_scopes=['formal','inbox'])
        source=self.context.knowledge/'fixture-source.md';source.write_text('合成可核验来源。',encoding='utf-8')
        target=self.context.knowledge/'fixture-result.md'
        for operation in ['source_read','candidate_submit','candidate_preview']:
            self.policy(operation,'allow')
        for operation in ['candidate_apply','writer_undo']:self.policy(operation,'ask')
        runner=self.runner();phases=[]
        def fragments(request):
            tool_rows=[json.loads(row['content']) for row in request['messages'] if row['role']=='tool']
            phase=len(tool_rows)
            if phase==0:operation,args='source_read',{'source_path':'03_知识/fixture-source.md'}
            elif phase==1:
                page=tool_rows[-1]['result']
                evidence=[{'source_path':page['source_path'],'version':page['version'],'byte_start':page['offset'],
                    'byte_end':page['offset']+page['returned_bytes'],'quote':page['content']}]
                operation,args='candidate_submit',{'target_path':'03_知识/fixture-result.md','content':'合成提炼结论。',
                    'reason':'合成验证','evidence_json':json.dumps(evidence),'expected_target_hash':'missing','request_id':'fixture-submit'}
            elif phase==2:
                result=tool_rows[-1]['result'];candidate=result.get('candidate',result)
                operation,args='candidate_preview',{'candidate_id':candidate['candidate_id']}
            elif phase==3:
                preview=tool_rows[-1]['result'];operation,args='candidate_apply',{
                    'candidate_id':preview['context']['candidate_id'],'preview_id':preview['preview_id']}
            elif phase==4:
                self.assertEqual('合成提炼结论。',target.read_text('utf-8'))
                operation,args='writer_undo',{'version':tool_rows[-1]['result']['version']}
            else:return [({'content':'已按授权写入并撤销，原始来源保留。'},'stop')]
            phases.append(operation)
            return [({'tool_calls':[{'index':0,'id':'fixture-call-'+str(phase),'function':{
                'name':'product_operation','arguments':json.dumps({'operation':operation,'arguments':json.dumps(args)})}}]},'tool_calls')]
        self.fragments=fragments;errors=[];answers=[]
        transport=CompatibleTransport(self.provider,self.provider.snapshot(),cancel=self.cancel,before_send=runner.before_send)
        def execute():
            try:answers.append(transport.run([{'role':'user','content':'fixture chain'}],dispatch=runner.dispatch_model,tools=self.registry.model_tools()))
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=execute);worker.start()
        confirmed=set();deadline=time.monotonic()+8
        while worker.is_alive() and time.monotonic()<deadline:
            for activity in list(self.activity):
                if activity['state']=='confirmation_required' and activity['request_id'] not in confirmed:
                    request=self.permissions.butler_policies()['pending']
                    row=next(row for row in request if row['id']==activity['request_id'])
                    if activity['operation']=='candidate_apply':
                        self.assertFalse(target.exists());self.assertIn('合成提炼结论。',row['arguments']['preview']['diff'])
                    self.permissions.confirm_butler_operation(activity['request_id'],approved=True,duration='request')
                    confirmed.add(activity['request_id'])
            time.sleep(.01)
        worker.join(1)
        self.assertFalse(worker.is_alive());self.assertEqual([],errors)
        self.assertEqual(['source_read','candidate_submit','candidate_preview','candidate_apply','writer_undo'],phases)
        self.assertEqual(2,len(confirmed));self.assertEqual(6,len(self.requests));self.assertEqual(1,len(answers))
        self.assertFalse(target.exists());self.assertEqual('合成可核验来源。',source.read_text('utf-8'))
        self.assertTrue(all(json.loads(row['content'])['ok'] for row in self.requests[-1]['messages'] if row['role']=='tool'))

    def test_selected_import_snapshot_baseline_revoke_and_bounded_object_read(self):
        from knowledge_bridge.bounded_files import SelectedFolders
        from knowledge_bridge.knowledge_objects import KnowledgeObjectService
        from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider
        folders=SelectedFolders(self.context);external=Path(self.temp.name)/'selected';external.mkdir()
        source=external/'fixture.md';source.write_text('合成导入正文。'*2000,encoding='utf-8')
        grant=folders.add(str(external));objects=KnowledgeObjectService(self.context,EncryptedProtectedStorageProvider(self.context))
        self.registry=ProductTools(self.context,objects=objects,folders=folders)
        self.configure(data_scopes=['formal','inbox','selected_folders'])
        for operation in ['import_selected_preview','import_selected_apply','object_list','object_read']:
            self.policy(operation,'allow')
        runner=self.runner();reply=self.call(runner,'import_selected_preview',{'folder_id':grant['id'],'paths':['fixture.md']})
        self.assertTrue(reply['ok']);preview=reply['result'];self.assertNotIn(str(external),json.dumps(preview))
        source.write_text('changed baseline',encoding='utf-8')
        self.assertFalse(self.call(runner,'import_selected_apply',{'preview_id':preview['preview_id']})['ok'])
        self.assertFalse(any(self.context.inbox.rglob('fixture.md')))
        source.write_text('合成导入正文。'*2000,encoding='utf-8')
        reply=self.call(runner,'import_selected_apply',{'preview_id':preview['preview_id']});self.assertTrue(reply['ok'])
        imported=next(self.context.inbox.rglob('fixture.md'));self.assertEqual(source.read_bytes(),imported.read_bytes())
        listing=self.call(runner,'object_list',{})['result'];row=next(row for row in listing['items'] if row['title']=='fixture.md')
        page=self.call(runner,'object_read',{'entry_id':row['entry_id'],'page_bytes':256})['result']
        self.assertLessEqual(page['returned_bytes'],256);self.assertFalse(page['complete']);self.assertTrue(page['next_cursor'])
        self.folders=folders;folders.revoke(grant['id'])
        with self.assertRaises(PermissionError):runner.before_send([])
        self.assertFalse(self.call(self.runner(),'import_selected_apply',{'preview_id':preview['preview_id']})['ok'])
        self.assertEqual(source.read_bytes(),imported.read_bytes())

    def test_original_organization_resume_delivery_and_actor_boundary(self):
        self.configure(data_scopes=['formal','inbox','evidence'])
        source=self.context.knowledge/'fixture.md';source.write_text('fixture text',encoding='utf-8')
        for operation in ['organization_start','organization_status','organization_read','organization_finish_source','wiki_claim']:
            self.policy(operation,'allow')
        runner=self.runner();reply=self.call(runner,'organization_start',{'scope':'formal','folder':'03_知识','request_id':'fixture-inventory'})
        self.assertTrue(reply['ok']);job=reply['result']['job_id']
        self.assertFalse(self.call(runner,'organization_finish_source',{'job_id':job,'source_path':'03_知识/fixture.md','outcome':'processed'})['ok'])
        page=self.call(runner,'organization_read',{'job_id':job,'source_path':'03_知识/fixture.md','page_bytes':256})
        self.assertTrue(page['ok']);self.assertEqual('fixture text',page['result']['content'])
        self.assertEqual('processed',self.call(runner,'organization_finish_source',{'job_id':job,'source_path':'03_知识/fixture.md','outcome':'processed'})['result']['status'])
        resumed=self.call(self.runner(),'organization_start',{'scope':'formal','folder':'03_知识','request_id':'fixture-inventory'})
        self.assertEqual(job,resumed['result']['job_id']);self.assertEqual(1,resumed['result']['counts']['processed'])
        other=ToolRunner(self.registry,self.permissions,session='other-session',job='other-job',provider=self.provider.snapshot(),
            cancel=self.cancel,provider_current=lambda:True)
        self.assertFalse(self.call(other,'organization_status',{'job_id':job})['ok'])
        (self.context.raw_archive/'fixture.md').write_text('synthetic archive',encoding='utf-8')
        claimed=self.call(runner,'wiki_claim',{})
        self.assertTrue(claimed['ok']);self.assertEqual('claimed',claimed['result']['status'])
        self.assertEqual('fixed_text_snapshot',claimed['result']['boundary'])
        self.assertFalse(self.call(other,'wiki_claim',{})['ok'])

    def test_archive_current_uses_saved_messages_and_is_idempotent(self):
        self.configure(data_scopes=['evidence']);self.policy('archive_current_conversation','allow')
        self.registry.conversation=lambda sid:{'session':{'title':'fixture conversation'},'messages':[
            {'id':1,'role':'user','content':'fixture original user','created':'2026-01-01T00:00:00Z','job':'job-1'},
            {'id':2,'role':'assistant','content':'fixture original answer','created':'2026-01-01T00:00:01Z','job':'job-1'}]}
        runner=self.runner();first=self.call(runner,'archive_current_conversation',{});second=self.call(runner,'archive_current_conversation',{})
        self.assertTrue(first['ok']);self.assertEqual(2,first['result']['saved']);self.assertEqual(2,second['result']['duplicates'])
        from knowledge_bridge.archive_store import ArchiveStore
        store=ArchiveStore(self.context);rows=store._message_rows(self.context.raw_message_store/(first['result']['session']['session_key']+'.jsonl'))
        self.assertEqual(['1','2'],[row['message_id'] for row in rows]);self.assertEqual('fixture original answer',rows[1]['original_text'])

    def test_revoke_between_tool_result_and_second_http_request(self):
        self.configure();self.policy('workspace_status','allow');runner=self.runner()
        self.fragments=[({'content':'fixture'},'stop')]
        transport=CompatibleTransport(self.provider,self.provider.snapshot(),cancel=self.cancel,before_send=runner.before_send)
        transport.complete([{'role':'user','content':'fixture'}]);self.assertTrue(self.call(runner,'workspace_status',{})['ok'])
        self.permissions.revoke_butler_session('session-1')
        with self.assertRaises(ValueError):transport.complete([{'role':'user','content':'fixture'},{'role':'tool','tool_call_id':'fixture','content':'fixture result'}])
        self.assertEqual(1,len(self.requests))


if __name__=='__main__':unittest.main()
