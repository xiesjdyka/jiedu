import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.butler_provider import ButlerProviders

READY={'ready':True,'model':'fixture','provider':'deepseek','revision':'fixture-revision'}

class HermesTests(unittest.TestCase):
    def test_completed_history_is_scoped_to_same_provider_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as d,patch('knowledge_bridge.hermes_client.status',return_value=READY):
            context=initialize_workspace(Path(d)/'workspace')
            runtime=Mock();runtime.config.return_value={};runtime.status.return_value={}
            provider=ButlerProviders(context);provider.select_hermes()
            service=LocalButler(context,runtime=runtime,providers=provider,start_worker=False)
            sid=service.create_session()['id']
            seen=[]
            def complete(messages):seen.append(messages);return '合成答案'
            def turn(question):
                jid=service.submit(sid,question)['id'];service.cancel_event=threading.Event();service._update(jid,state='running')
                with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
                with patch('knowledge_bridge.hermes_client.HermesTransport') as transport:
                    transport.return_value.complete.side_effect=complete
                    service._execute(job)
                return jid
            turn('代号只有本次的松林204')
            service.close();service=LocalButler(context,runtime=runtime,providers=provider,start_worker=False)
            turn('代号是什么')
            self.assertIn('松林204',json.dumps(seen[-1],ensure_ascii=False))
            self.assertEqual(len(seen[-1]),4)
            # Failed/cancelled questions are never replayed into a later request.
            jid=service.submit(sid,'不可重放的取消问题')['id'];service.cancel(jid)
            turn('继续')
            self.assertNotIn('不可重放',json.dumps(seen[-1],ensure_ascii=False))
            # Switching to a new Hermes configuration resets history at that boundary.
            provider._save({**provider._load(),'hermes_revision':'new'})
            with patch('knowledge_bridge.hermes_client.status',return_value={**READY,'revision':'new'}):
                turn('新服务问题')
            self.assertEqual(len(seen[-1]),2)
            runtime.start.assert_not_called();service.close()

    def test_changed_or_missing_install_does_not_silently_use_old_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            context=initialize_workspace(Path(d)/'workspace');provider=ButlerProviders(context)
            with patch('knowledge_bridge.hermes_client.status',return_value=READY):provider.select_hermes()
            with patch('knowledge_bridge.hermes_client.status',return_value={**READY,'revision':'different'}):
                with self.assertRaisesRegex(ValueError,'配置已变化'):provider.snapshot()
            with patch('knowledge_bridge.hermes_client.status',return_value={'ready':False,'error':'未找到本机 Hermes'}):
                with self.assertRaisesRegex(ValueError,'未找到'):provider.snapshot()

    def test_public_status_has_no_credential_or_installation_path(self):
        from knowledge_bridge.hermes_client import status
        with patch('knowledge_bridge.hermes_client.discover',return_value={**READY,'key':'private','source':'private-path'}):
            self.assertEqual(status(),READY)
