"""Real Windows loopback acceptance with only temporary workspace and client config."""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from knowledge_bridge.workspace import initialize_workspace


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.name=='nt','Windows peer verification requires Windows')
class OnePromptLoopbackTests(unittest.TestCase):
    def test_owner_ticket_cli_redeem_and_replay_denial(self):
        with tempfile.TemporaryDirectory(prefix='jiedu-one-prompt-') as temporary:
            base=Path(temporary)
            workspace=base/'workspace';context=initialize_workspace(workspace)
            control=base/'control';control.mkdir()
            codex=base/'codex';codex.mkdir()
            port=_free_port();mcp_port=_free_port()
            (control/'bridge-settings.json').write_text(json.dumps({
                'console':{'host':'127.0.0.1','port':port},
                'mcp':{'mode':'stdio','host':'127.0.0.1','port':mcp_port}}),'utf-8')
            root=Path(__file__).resolve().parents[1]
            env=os.environ.copy()
            env.update({'KNOWLEDGE_BRIDGE_VAULT':str(workspace),
                'KNOWLEDGE_BRIDGE_LOCAL_DATA':str(control),
                'KNOWLEDGE_BRIDGE_CODEX_HOME':str(codex),'CODEX_HOME':str(codex),
                'HERMES_HOME':str(base/'hermes'),
                'KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE':'1',
                'KNOWLEDGE_BRIDGE_OWNER_SESSION_SECONDS':'120',
                'PYTHONDONTWRITEBYTECODE':'1',
                'PYTHONUTF8':'1',
                'PYTHONPATH':str(root)})
            origin=f'http://127.0.0.1:{port}'
            opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
            process=subprocess.Popen([sys.executable,'-B','-m','knowledge_bridge.cli','console'],
                cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                health=None
                for _ in range(100):
                    if process.poll() is not None:break
                    try:
                        with opener.open(origin+'/api/health',timeout=1) as response:health=json.load(response)
                        if health.get('workspace_ready'):break
                    except (OSError,ValueError):pass
                    time.sleep(.2)
                self.assertIsNotNone(health,'isolated console failed to start')
                self.assertEqual(context.workspace_id,health['workspace_id'])
                self.assertEqual(root,Path(health['program_root']).resolve())
                with opener.open(origin+'/api/protection/status',timeout=5) as response:
                    protection=json.load(response)
                csrf=protection['csrf_token']
                if protection.get('storage_enabled'):
                    request=Request(origin+'/api/protection/device-session',b'{}',
                        {'Origin':origin,'Content-Type':'application/json','X-Jiedu-CSRF':csrf},method='POST')
                    with opener.open(request,timeout=5) as response:protection=json.load(response)
                    csrf=protection['csrf_token']
                request=Request(origin+'/api/client-access/handoff/codex',b'{}',
                    {'Origin':origin,'Content-Type':'application/json','X-Jiedu-CSRF':csrf},method='POST')
                with opener.open(request,timeout=5) as response:ticket=json.load(response)
                self.assertEqual(context.workspace_id,ticket['workspace_id'])
                self.assertEqual(str(root),ticket['program_root'])
                command=[sys.executable,'-B','-m','knowledge_bridge.cli','handoff',
                    '--client','codex','--console',origin,
                    '--workspace-id',context.workspace_id,'--program-root',str(root),
                    '--console-pid',str(ticket['console_pid']),'--token',ticket['token']]
                redeemed=subprocess.run(command,cwd=root,env=env,capture_output=True,
                    text=True,encoding='utf-8',timeout=35,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(0,redeemed.returncode,redeemed.stderr)
                self.assertIn('MCP',redeemed.stdout)
                self.assertNotIn(ticket['token'],redeemed.stdout+redeemed.stderr)
                self.assertIn('mcp_servers.knowledge_bridge',(codex/'config.toml').read_text('utf-8'))
                state=json.loads((context.sync/'codex-source-adapter.json').read_text('utf-8'))
                self.assertEqual('all',state['archive_policy'])
                replay=subprocess.run(command,cwd=root,env=env,capture_output=True,
                    text=True,encoding='utf-8',timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(2,replay.returncode)
                self.assertNotIn(ticket['token'],replay.stdout+replay.stderr)
            finally:
                process.terminate()
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=8)


if __name__=='__main__':unittest.main()
