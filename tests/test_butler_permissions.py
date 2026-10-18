import tempfile
import threading
import unittest
from pathlib import Path

from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService
from knowledge_bridge.workspace import initialize_workspace


class ButlerPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.context=initialize_workspace(Path(self.temp.name)/'workspace')
        registry=ClientIdentityRegistry(self.context,root=Path(self.temp.name)/'identities')
        self.service=ClientPermissionService(registry);self.operations={'read','write'}

    def policy(self,decision):self.service.set_butler_policy('write',decision,known_operations=self.operations)
    def request(self,job='job-1',session='session-1',args=None):
        return self.service.request_butler_operation('write',args or {'target':'fixture.md'},session=session,job=job,
            provider_revision='fixture-provider',known_operations=self.operations)

    def test_default_deny_cannot_be_overridden_by_model_arguments(self):
        self.assertEqual('deny',self.request(args={'owner_confirmed':True,'permission':'allow'})['decision'])
        forged={'decision':'allow','revision':self.service.butler_policies()['revision'],'binding':'fake','operation':'write'}
        with self.assertRaises(ValueError),self.service.butler_operation(forged):self.fail('forged authorization')

    def test_confirmation_exact_parameters_and_single_use(self):
        self.policy('ask');ticket=self.request()
        with self.assertRaises(ValueError),self.service.butler_operation(ticket):pass
        self.service.confirm_butler_operation(ticket['id'],approved=True)
        altered={**ticket,'binding':'changed'}
        with self.assertRaises(ValueError),self.service.butler_operation(altered):pass
        with self.service.butler_operation(ticket):pass
        with self.assertRaises(ValueError),self.service.butler_operation(ticket):pass
        self.assertEqual('ask',self.request(job='job-2')['decision'])

    def test_session_and_persistent_grants_and_revocation(self):
        self.policy('ask');ticket=self.request();self.service.confirm_butler_operation(ticket['id'],approved=True,duration='session')
        with self.service.butler_operation(ticket):pass
        second=self.request(job='job-2');self.assertEqual('allow',second['decision'])
        self.assertEqual('ask',self.request(job='job-3',session='session-2')['decision'])
        self.service.revoke_butler_session('session-1')
        with self.assertRaises(ValueError),self.service.butler_operation(second):pass
        third=self.request(job='job-4');self.service.confirm_butler_operation(third['id'],approved=True,duration='persistent')
        self.assertEqual('allow',self.request(job='job-5',session='session-2')['decision'])
        self.policy('deny');self.assertEqual('deny',self.request(job='job-6')['decision'])

    def test_allow_tickets_are_not_replayable_cancel_and_provider_change_block_commit(self):
        self.policy('allow');ticket=self.request()
        self.service.cancel_butler_job('job-1')
        with self.assertRaises(ValueError),self.service.butler_operation(ticket):pass
        second=self.request(job='job-2');event=threading.Event();event.set()
        with self.assertRaises(InterruptedError),self.service.butler_operation(second,cancel=event):pass
        third=self.request(job='job-3')
        with self.assertRaises(ValueError),self.service.butler_operation(third,provider_current=lambda:False):pass

    def test_policy_revocation_serializes_with_operation(self):
        self.policy('allow');ticket=self.request();started=threading.Event();release=threading.Event();revoked=threading.Event()
        def execute():
            with self.service.butler_operation(ticket):started.set();release.wait(2)
        worker=threading.Thread(target=execute);worker.start();self.assertTrue(started.wait(1))
        changer=threading.Thread(target=lambda:(self.policy('deny'),revoked.set()));changer.start()
        self.assertFalse(revoked.wait(.05));release.set();worker.join(1);changer.join(1)
        self.assertTrue(revoked.is_set());self.assertEqual('deny',self.request(job='after')['decision'])


if __name__=='__main__':unittest.main()
