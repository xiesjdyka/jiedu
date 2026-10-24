import io
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from knowledge_bridge import one_prompt_connect, web


class OnePromptConnectTests(unittest.TestCase):
    def test_read_permission_summary_distinguishes_closed_partial_and_open(self):
        permissions=Mock()
        permissions._policy.return_value=web.PolicyDecision.ALLOW
        self.assertEqual('allow',web._client_ordinary_read_policy('fixture',[],permissions)['state'])
        specific=[{'scope':'workspace','operation':'read','selector':{'project':'a'},'decision':'deny'}]
        self.assertEqual('partial',web._client_ordinary_read_policy('fixture',specific,permissions)['state'])
        permissions._policy.return_value=web.PolicyDecision.DENY
        self.assertEqual('deny',web._client_ordinary_read_policy('fixture',[],permissions)['state'])
        permissions._policy.return_value=web.PolicyDecision.ASK
        self.assertEqual('partial',web._client_ordinary_read_policy('fixture',[],permissions)['state'])

    @patch.object(web,'datetime',wraps=web.datetime)
    def test_read_status_requires_current_permission_and_fresh_verification(self,clock):
        clock.now.return_value=web.datetime.fromisoformat('2026-10-22T01:01:00+00:00')
        permissions=Mock()
        permissions._policy.return_value=web.PolicyDecision.ALLOW
        row={'internal_id':'fixture','status':'read_verified','last_read_at':'2026-10-22T01:00:00+00:00',
             'last_read_scope':'formal','last_read_tool':'knowledge_search'}
        self.assertTrue(web._client_read_verification_current(row,[],permissions))
        self.assertFalse(web._client_read_verification_current(row|{
            'last_read_at':'2026-10-22T00:55:59+00:00'},[],permissions))
        self.assertFalse(web._client_read_verification_current(row,[],permissions,False))
        permissions._policy.return_value=web.PolicyDecision.DENY
        self.assertFalse(web._client_read_verification_current(row,[],permissions))
        permissions._policy.return_value=web.PolicyDecision.ALLOW
        self.assertFalse(web._client_read_verification_current(row,[
            {'updated_at':'2026-10-22T02:00:00+00:00'}],permissions))
        self.assertFalse(web._client_read_verification_current(row|{
            'rotated_at':'2026-10-22T02:00:00+00:00'},[],permissions))
        self.assertFalse(web._client_read_verification_current(row|{'status':'revoked'},[],permissions))
        self.assertTrue(web._client_read_verification_current(row|{'client_type':'cursor'},[],permissions,True,
            '2026-10-22T00:30:00+00:00'))
        self.assertFalse(web._client_read_verification_current(row|{'client_type':'cursor'},[],permissions,True,
            '2026-10-22T02:00:00+00:00'))

    def test_cursor_removed_config_is_missing_but_invalid_config_needs_review(self):
        from knowledge_bridge import client_config
        with tempfile.TemporaryDirectory() as tmp:
            adapter=client_config.CursorAdapter()
            adapter.path=Path(tmp)/'.cursor'/'mcp.json'
            row={'client_type':'cursor'}
            with patch.dict(client_config.ADAPTERS,{'cursor':adapter}):
                self.assertFalse(web._cursor_configuration_detail(row)['present'])
                adapter.path.parent.mkdir()
                adapter.path.write_text('{"mcpServers":{}}',encoding='utf-8')
                self.assertFalse(web._cursor_configuration_detail(row)['present'])
                adapter.path.write_text('{bad json',encoding='utf-8')
                self.assertTrue(web._cursor_configuration_detail(row)['present'])
                adapter.path.write_text('{"mcpServers":{"knowledge_bridge":{"command":"fixture"}}}',encoding='utf-8')
                self.assertTrue(web._cursor_configuration_detail(row)['present'])
                with patch.object(Path,'stat',side_effect=PermissionError('locked')):
                    self.assertIsNone(web._cursor_configuration_detail(row)['present'])

    def test_cli_checks_running_workspace_before_sending_one_time_token(self):
        root=Path(one_prompt_connect.__file__).resolve().parents[1]
        health={'workspace_ready':True,'workspace_id':'fixture-workspace',
                'program_root':str(root),'pid':54321}
        calls=[]
        def opened(request,timeout):
            calls.append(request)
            if isinstance(request,str):return io.BytesIO(json.dumps(health).encode())
            self.assertEqual('POST',request.get_method())
            self.assertIn('/handoff/codex/redeem',request.full_url)
            self.assertEqual('one-time-fixture',json.loads(request.data)['token'])
            return io.BytesIO(json.dumps({'outcome':'paired','restart_required':True,
                'auto_archive':'enabled'}).encode())
        with patch.object(one_prompt_connect,'_open_local',side_effect=opened):
            result=one_prompt_connect.redeem('codex','http://127.0.0.1:8765',
                'fixture-workspace',str(root),54321,'one-time-fixture')
        self.assertIn('已保存新的 MCP 配置',result)
        self.assertIn('需要重开',result)
        self.assertNotIn('one-time-fixture',result)
        self.assertEqual(2,len(calls))
        health['workspace_id']='different'
        calls.clear()
        with patch.object(one_prompt_connect,'_open_local',side_effect=opened):
            with self.assertRaisesRegex(ValueError,'工作区或运行进程已变化'):
                one_prompt_connect.redeem('codex','http://127.0.0.1:8765',
                    'fixture-workspace',str(root),54321,'one-time-fixture')
        self.assertEqual(1,len(calls))
        with self.assertRaises(ValueError):
            one_prompt_connect.redeem('codex','http://example.com:8765',
                'fixture-workspace',str(root),54321,'one-time-fixture')

    def test_local_handoff_never_follows_redirects(self):
        handler=one_prompt_connect._NoRedirect()
        self.assertIsNone(handler.redirect_request(None,None,302,'moved',{},'http://example.com/token'))

    def test_redeem_endpoint_requires_same_user_local_peer(self):
        manager=Mock()
        manager.redeem_handoff.return_value={'outcome':'reused'}
        request=SimpleNamespace(headers={},scope={'client':('127.0.0.1',1234),
            'server':('127.0.0.1',8765)},app=SimpleNamespace(state=SimpleNamespace(client_pairings=manager)))
        payload=web.ClientHandoffRequest(token='x'*32,workspace_id='fixture')
        with patch.object(web,'_same_origin') as origin,patch(
                'knowledge_bridge.windows_trust.verify_current_user_peer') as verify:
            self.assertEqual('reused',web.api_client_handoff_redeem('codex',request,payload)['outcome'])
            origin.assert_called_once_with(request)
            verify.assert_called_once()
        request.headers={'forwarded':'evil'}
        with patch.object(web,'_same_origin'),patch(
                'knowledge_bridge.windows_trust.verify_current_user_peer') as verify:
            with self.assertRaises(HTTPException) as raised:
                web.api_client_handoff_redeem('codex',request,payload)
            self.assertEqual(403,raised.exception.status_code)
            verify.assert_not_called()
        manager.redeem_handoff.assert_called_once_with('x'*32,'codex','fixture')

    def test_current_configuration_checks_persisted_secret(self):
        row={'client_type':'codex','claimed_id':'codex','internal_id':'fixture'}
        adapter=Mock()
        adapter.detect.return_value={'entry':{'env':{
            'KNOWLEDGE_BRIDGE_CLIENT_ID':'codex','KNOWLEDGE_BRIDGE_CLIENT_SECRET':'stale'}}}
        registry=Mock()
        registry.configured_secret_matches.return_value=False
        with patch('knowledge_bridge.client_config.ADAPTERS',{'codex':adapter}),patch(
                'knowledge_bridge.client_config._valid_entry',return_value=True):
            self.assertFalse(web._client_configuration_current(row,registry))
            registry.configured_secret_matches.assert_called_once_with('fixture','stale')
            registry.configured_secret_matches.return_value=True
            adapter.detect.return_value={'entry':{'env':{
                'KNOWLEDGE_BRIDGE_CLIENT_ID':'wrong_agent','KNOWLEDGE_BRIDGE_CLIENT_SECRET':'stale'}}}
            self.assertFalse(web._client_configuration_current(row,registry))


if __name__=='__main__':unittest.main()
