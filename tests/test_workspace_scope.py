"""Ordinary Obsidian notes stay ordinary while remaining searchable and readable."""
import tempfile
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_bridge
from knowledge_bridge.workspace import initialize_workspace, load_workspace
from knowledge_bridge.search_scopes import files_for_scope, rebuild_scope_stable
from knowledge_bridge.retrieval import RetrievalService
from knowledge_bridge.client_access import (KnowledgeScope, ClientIdentityRegistry,
    ClientPermissionService, ClientAccessGate, PolicyDecision, resolve_document_target)
from knowledge_bridge.source_reading import read_source


class WorkspaceScopeTests(unittest.TestCase):
    def test_ordinary_markdown_search_returns_source_and_original_without_modifying_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'Obsidian';root.mkdir()
            note=root/'Ideas'/'航海.md';note.parent.mkdir()
            note.write_text('# 航海\n\n蓝鲸航线使用北极星定位。\n',encoding='utf-8')
            other=root/'00_系统'/'规则.md';other.parent.mkdir()
            other.write_text('# 规则\n\n正式知识。',encoding='utf-8')
            hidden=root/'.obsidian'/'secret.md';hidden.parent.mkdir()
            hidden.write_text('隐藏设置',encoding='utf-8')
            before={path.relative_to(root):path.read_bytes() for path in (note,other,hidden)}
            context=initialize_workspace(root)
            self.assertEqual(['Ideas/航海.md'],[context.relative(path) for path in files_for_scope(context,'workspace')])
            manifest=rebuild_scope_stable('workspace',context)
            self.assertTrue(manifest['stable_snapshot'])
            self.assertEqual(1,manifest['file_count'])
            result=RetrievalService(context,vector_backend=False).search('北极星定位',scope='workspace',mode='keyword')
            self.assertEqual(1,result['returned'])
            hit=result['results'][0]
            self.assertEqual('workspace',hit['scope'])
            self.assertEqual('Ideas/航海.md',hit['source_path'])
            self.assertIn('蓝鲸航线使用北极星定位',hit['content'])
            self.assertEqual(KnowledgeScope.WORKSPACE,resolve_document_target(context,hit['source_path']).scope)
            opened=read_source(context,hit['source_path'],hit['chunk_id'])
            self.assertIn('蓝鲸航线使用北极星定位',opened['content'])
            self.assertEqual('workspace',opened['scope'])
            for relative,raw in before.items():self.assertEqual(raw,(root/relative).read_bytes())

    def test_hidden_and_external_sources_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'vault';context=initialize_workspace(root)
            hidden=root/'.obsidian'/'secret.md';hidden.parent.mkdir(exist_ok=True)
            hidden.write_text('secret',encoding='utf-8')
            with self.assertRaises(ValueError):resolve_document_target(context,'.obsidian/secret.md')
            with self.assertRaises(ValueError):resolve_document_target(context,'../secret.md')

    def test_semantic_candidate_routes_through_workspace_scope(self):
        from knowledge_bridge import workspace_vector
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'vault';context=initialize_workspace(root)
            note=root/'Plans'/'voyage.md';note.parent.mkdir()
            note.write_text('# Voyage\n\n蓝鲸航线使用北极星定位。\n',encoding='utf-8')
            rebuild_scope_stable('workspace',context)
            fake_core=types.ModuleType('knowledge_bridge.core')
            fake_core.VAULT=root
            fake_core.local_model_status=lambda:{'state':'loaded','installation_state':'installed'}
            fake_core.model_busy=lambda:False
            fake_core.embed=lambda *args,**kwargs:[[0.1,0.2]]
            vector_hit={'source_path':'Plans/voyage.md','content':'蓝鲸航线使用北极星定位。',
                        'source_type':'source_document','source_version':'mock','score':1.0}
            with patch.dict(sys.modules,{'knowledge_bridge.core':fake_core}), \
                 patch.object(knowledge_bridge,'core',fake_core,create=True), \
                 patch.object(workspace_vector,'status',return_value={'ready':True,'fresh':True}), \
                 patch.object(workspace_vector,'table',return_value=object()), \
                 patch.object(workspace_vector,'vector_rows',return_value=[vector_hit]):
                result=RetrievalService(context).search('海上导航',scope='workspace',mode='hybrid')
            self.assertTrue(result['vector_used'])
            self.assertEqual('workspace',result['results'][0]['scope'])
            self.assertEqual('Plans/voyage.md',result['results'][0]['source_path'])

    def test_existing_token_needs_workspace_rule_and_can_open_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'vault';context=initialize_workspace(root)
            note=root/'Personal'/'plan.md';note.parent.mkdir()
            note.write_text('# Plan\n\nAlpha is the original text.',encoding='utf-8')
            registry=ClientIdentityRegistry(context,root=Path(temporary)/'identities')
            prepared=registry.prepare('fixture_ai','Fixture AI','custom','stdio')
            registry.commit_prepared(prepared)
            identity=registry.authenticate(prepared['claimed_id'],prepared['secret'])
            permissions=ClientPermissionService(registry);gate=ClientAccessGate(permissions,identity)
            self.assertEqual(PolicyDecision.DENY,gate.search('Alpha',5,False,scope='workspace')[1].outcome)
            for operation in ('search','read','context'):
                permissions.set_policy(identity.internal_id,'workspace',operation,'allow')
            self.assertEqual(PolicyDecision.ALLOW,gate.search('Alpha',5,False,scope='workspace')[1].outcome)
            self.assertEqual(PolicyDecision.ALLOW,gate.document('Personal/plan.md')[1].outcome)
            self.assertIn('Alpha is the original text',read_source(context,'Personal/plan.md')['content'])

    def test_background_task_reuses_running_job_and_interrupted_marker(self):
        from knowledge_bridge import workspace_vector
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'vault')
            release=threading.Event();entered=threading.Event()
            def delayed(_context,*,progress):
                entered.set();release.wait(3)
                progress(1,1)
                return {'status':'rebuilt','vector_count':1}
            with patch.object(workspace_vector,'rebuild',side_effect=delayed):
                first=workspace_vector.start_rebuild(context)
                self.assertTrue(entered.wait(2))
                second=workspace_vector.start_rebuild(context)
                self.assertEqual(first['task_id'],second['task_id'])
                self.assertTrue(second['reused'])
                release.set()
                workspace_vector._TASKS[first['task_id']]['_thread'].join(5)
            self.assertEqual('rebuilt',workspace_vector.task_status(first['task_id'],context)['status'])
            marker=workspace_vector._task_path(context)
            workspace_vector._write_json(marker,{'task_id':'orphan','workspace_id':context.workspace_id,'status':'running'})
            self.assertEqual('interrupted',workspace_vector.task_status('orphan',context)['status'])

    @unittest.skipUnless(importlib.util.find_spec('lancedb'), 'LanceDB runtime unavailable')
    def test_two_ordinary_notes_build_separate_vector_index(self):
        import numpy as np
        from knowledge_bridge import core, workspace_vector
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'vault';context=initialize_workspace(root)
            a=root/'Ideas'/'navigation.md';b=root/'Ideas'/'garden.md';a.parent.mkdir()
            a.write_text('# 航线\n\n蓝鲸航线使用北极星定位。',encoding='utf-8')
            b.write_text('# 花园\n\n花园今天需要给番茄浇水。',encoding='utf-8')
            formal=root/'03_知识'/'formal.md';formal.write_text('正式资料',encoding='utf-8')
            original={path: path.read_bytes() for path in (a,b,formal)}
            def encoded(texts,**_):
                return np.asarray([[1.,0.] if '北极星' in text or 'ocean direction' in text else [0.,1.] for text in texts],dtype='float32')
            model={'state':'loaded','installation_state':'installed'}
            with patch.object(core,'embed',side_effect=encoded), \
                 patch.object(core,'local_model_status',return_value=model), \
                 patch.object(core,'model_busy',return_value=False), \
                 patch.object(core,'VAULT',root):
                result=workspace_vector.rebuild(context)
                self.assertEqual('rebuilt',result['status'])
                self.assertEqual(2,result['vector_count'])
                self.assertIsNotNone(workspace_vector.table(context))
                hit=RetrievalService(context).search('ocean direction',scope='workspace',mode='vector')
            self.assertTrue(hit['results'],hit)
            self.assertEqual('navigation.md',Path(hit['results'][0]['source_path']).name)
            self.assertTrue(hit['vector_used'])
            self.assertEqual('workspace',hit['results'][0]['scope'])
            self.assertEqual({a,b},set(files_for_scope(context,'workspace')))
            for path,data in original.items():self.assertEqual(data,path.read_bytes())

    @unittest.skipUnless(importlib.util.find_spec('lancedb'), 'LanceDB runtime unavailable')
    def test_build_status_distinguishes_empty_keyword_and_vector(self):
        import os
        import numpy as np
        from knowledge_bridge import core, knowledge_base, workspace_vector
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'vault';root.mkdir()
            note=root/'Notes'/'one.md';note.parent.mkdir()
            note.write_text('# One\n\n普通资料的原文。',encoding='utf-8')
            inventory={'vector_count':0,'vector_index_ready':False,'active_generation':None,
                       'chunk_count':0,'file_count':0,'project_count':0}
            def vectors(texts,**_):return np.asarray([[1.,0.] for _ in texts],dtype='float32')
            with patch.dict(os.environ,{'KNOWLEDGE_BRIDGE_VAULT':str(root)}), \
                 patch.object(knowledge_base,'CONFIG_PATH',Path(temporary)/'control'/'knowledge-base.json'), \
                 patch.object(knowledge_base,'AUDIT_PATH',Path(temporary)/'audit.json'), \
                 patch.object(knowledge_base,'VAULT',root), \
                 patch.object(core,'VAULT',root), \
                 patch.object(core,'overview',return_value=inventory), \
                 patch.object(core,'embed',side_effect=vectors):
                knowledge_base.connect(str(root))
                before=knowledge_base.status()
                self.assertFalse(before['indexed'])
                self.assertEqual('not_built',before['index_state'])
                result=knowledge_base.build_index(True)
                self.assertEqual('indexing',result['status'])
                self.assertEqual('skipped_no_formal_sources',result['sync']['status'])
                workspace_vector._TASKS[result['task_id']]['_thread'].join(5)
                task=workspace_vector.task_status(result['task_id'],load_workspace(root))
                self.assertEqual('rebuilt',task['status'])
                after=knowledge_base.status()
                self.assertTrue(after['indexed'])
                self.assertEqual('ready',after['index_state'])
                self.assertEqual(1,after['workspace_index']['file_count'])
                self.assertTrue(after['workspace_index']['vector_ready'])
                self.assertEqual(1,after['workspace_index']['vector_count'])
                self.assertEqual(0,after['index']['vector_count'])
                self.assertEqual('普通资料的原文。',note.read_text('utf-8').splitlines()[-1])
                note.unlink()
                empty=knowledge_base.build_index(True)
                self.assertEqual('empty_corpus',empty['status'])
                self.assertFalse(empty['knowledge_base']['indexed'])
                self.assertEqual('not_built',empty['knowledge_base']['index_state'])


if __name__=='__main__':unittest.main()
