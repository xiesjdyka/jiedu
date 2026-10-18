import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from knowledge_bridge.protection_control import ProtectionLifecycleManager, OwnerSessionRequired
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider, InvalidUnlockMaterial
from knowledge_bridge.storage import StorageAuthorization, StorageOperation
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.windows_trust import dpapi, verify_current_user_peer, DeviceTrustUnavailable

PASSWORD = 'fictional-device-migration-2026'
KDF = {'name':'argon2id','iterations':2,'lanes':1,'memory_cost_kib':19456,'length':32}
RW = StorageAuthorization.allow(StorageOperation.WRITE_ORIGINAL, StorageOperation.READ, StorageOperation.LIST)


@unittest.skipUnless(os.name == 'nt', 'Windows trust is fail-closed elsewhere')
class DeviceAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.context = initialize_workspace(self.base/'workspace')
        self.settings = self.base/'machine/settings.json'
        self.clock = [100.0]

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self):
        return ProtectionLifecycleManager(self.context, settings_path=self.settings, device_mode=True,
                    clock=lambda:self.clock[0], owner_session_seconds=3, initialization_kdf_parameters=KDF)

    def legacy(self):
        p = EncryptedProtectedStorageProvider(self.context)
        recovery = p.initialize(PASSWORD,kdf_parameters=KDF).recovery_key
        p.write_original('fixture/secret',b'DeviceFixtureSensitiveValue928',RW)
        p.lock()
        return recovery

    def digest(self):
        return {str(p.relative_to(self.context.protected_root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.context.protected_root.rglob('*') if p.is_file()}

    def test_locked_legacy_does_not_block_os_authenticated_management(self):
        self.legacy();before=self.digest();m=self.manager();c=m.local_browser()
        self.assertEqual('locked',m.status(c.session_token)['state'])
        self.assertTrue(m.status(c.session_token)['migration_required'])
        self.clock[0]+=86400
        m.owner_session(c.session_token,c.csrf_token,require_csrf=True)
        with self.assertRaises(OwnerSessionRequired):m.owner_session(c.session_token,'wrong',require_csrf=True)
        with self.assertRaises(OwnerSessionRequired):m.owner_session('unknown')
        self.assertEqual(before,self.digest())

    def test_additive_migration_refresh_restart_and_recovery(self):
        recovery=self.legacy();before=self.digest();m=self.manager()
        m.unlock_with_passphrase(PASSWORD);c=m.local_browser()
        self.assertEqual(c,m.local_browser(c.session_token))
        self.clock[0]+=86400
        self.assertTrue(m.status(c.session_token)['owner_session_active'])
        m.close();restarted=self.manager()
        self.assertEqual('unlocked',restarted.status()['state'])
        with self.assertRaises(OwnerSessionRequired):restarted.owner_session(c.session_token)
        fresh=restarted.local_browser();restarted.owner_session(fresh.session_token)
        self.assertEqual(b'DeviceFixtureSensitiveValue928',restarted.provider.read_object('fixture/secret',RW))
        restarted.provider.write_original('fixture/new',b'new encrypted content',RW)
        self.assertEqual(b'new encrypted content',restarted.provider.read_object('fixture/new',RW))
        for path,digest in before.items():self.assertEqual(digest,self.digest()[path])
        restarted.close();p=EncryptedProtectedStorageProvider(self.context)
        p.unlock_with_recovery(recovery)
        self.assertEqual(b'new encrypted content',p.read_object('fixture/new',RW))
        for path in self.base.rglob('*'):
            if path.is_file():self.assertNotIn(b'DeviceFixtureSensitiveValue928',path.read_bytes())

    def test_failed_custody_keeps_ciphertext_and_recovery(self):
        recovery=self.legacy();before=self.digest();m=self.manager()
        with patch('knowledge_bridge.device_keys.dpapi',side_effect=DeviceTrustUnavailable('fixture')):
            m.unlock_with_passphrase(PASSWORD)
        self.assertTrue(m.status()['device_error'])
        self.assertEqual(before,self.digest());self.assertFalse(m.device_store.path.exists())
        m.close();m=self.manager();m.unlock_with_recovery(recovery)
        self.assertTrue(m.device_store.path.exists())

    def test_wrong_account_or_corrupt_envelope_fails_closed_and_can_recover(self):
        recovery=self.legacy();m=self.manager();m.unlock_with_passphrase(PASSWORD);m.close()
        before=self.digest();envelope=m.device_store.path.read_bytes()
        m.device_store.path.write_text('{}','utf-8')
        restarted=self.manager();self.assertEqual('locked',restarted.status()['state'])
        self.assertTrue(restarted.status()['device_error']);self.assertEqual(before,self.digest())
        restarted.unlock_with_recovery(recovery);restarted.close()
        with patch('knowledge_bridge.device_keys.dpapi',side_effect=DeviceTrustUnavailable('other account')):
            blocked=self.manager();self.assertEqual('locked',blocked.status()['state'])
        self.assertEqual(before,self.digest())

    def test_manual_lock_persists_until_explicitly_disabled(self):
        recovery=self.legacy();m=self.manager();m.unlock_with_passphrase(PASSWORD);old=m.local_browser()
        m.enable_manual_lock('fictional-extra-manual-lock')
        with self.assertRaises(OwnerSessionRequired):m.local_browser()
        m.close();m=self.manager();self.assertFalse(m.device_mode)
        self.assertEqual('locked',m.status()['state'])
        with self.assertRaises(OwnerSessionRequired):m.owner_session(old.session_token)
        m.unlock_with_recovery(recovery);m.enroll_device();m.close()
        self.assertEqual('unlocked',self.manager().status()['state'])

    def test_dpapi_entropy_and_live_socket_owner(self):
        value=b'fictional-key-material';wrapped=dpapi(value,b'workspace-a')
        self.assertEqual(value,dpapi(wrapped,b'workspace-a',decrypt=True))
        with self.assertRaises(DeviceTrustUnavailable):dpapi(wrapped,b'workspace-b',decrypt=True)
        with socket.socket() as server:
            server.bind(('127.0.0.1',0));server.listen()
            with socket.create_connection(server.getsockname()) as client:
                accepted,peer=server.accept()
                with accepted:
                    verify_current_user_peer(peer,accepted.getsockname())
                    with patch('knowledge_bridge.windows_trust._token_sid',side_effect=[b'one',b'two']):
                        with self.assertRaises(DeviceTrustUnavailable):verify_current_user_peer(peer,accepted.getsockname())
        with self.assertRaises(DeviceTrustUnavailable):verify_current_user_peer(('192.0.2.1',4567),('127.0.0.1',8765))

    def test_unused_encryption_off_survives_restart_and_never_prompts_migration(self):
        provider=EncryptedProtectedStorageProvider(self.context)
        provider.initialize(PASSWORD,kdf_parameters=KDF);provider.lock()
        before=self.digest();m=self.manager();c=m.local_browser();m.disable_empty_storage()
        self.assertFalse(m.status(c.session_token)['migration_required'])
        self.assertEqual('disabled',m.status(c.session_token)['state'])
        open_session=m.open_browser(c.session_token)
        open_status=m.status(open_session.session_token)
        self.assertTrue(open_status['owner_session_active'])
        self.assertTrue(open_status['browser_session_active'])
        self.assertEqual('open',open_status['access_mode'])
        self.assertEqual('本机普通模式',open_status['local_identity'])
        m.owner_session(c.session_token);m.close();m=self.manager();c=m.local_browser()
        self.assertTrue(m.status(c.session_token)['owner_session_active'])
        self.assertFalse(m.storage_enabled);self.assertEqual(before,self.digest())
        self.assertFalse(m.device_store.path.exists())

    def test_disable_refuses_to_hide_existing_ciphertext_objects(self):
        self.legacy();m=self.manager();before=self.digest()
        with self.assertRaises(ValueError):m.disable_empty_storage()
        self.assertTrue(m.storage_enabled);self.assertEqual(before,self.digest())

    def test_disabled_new_workspace_can_opt_in_without_partial_key_on_cancel(self):
        m=self.manager();c=m.local_browser();before=self.digest()
        self.assertFalse(m.storage_enabled)
        self.assertEqual('disabled',m.status(c.session_token)['state'])
        self.assertFalse(m.status(c.session_token)['configured'])
        self.assertEqual(before,self.digest())
        initialized,credentials=m.enable_new_storage('fictional-opt-in-passphrase')
        self.assertTrue(initialized.recovery_key)
        self.assertTrue(m.storage_enabled)
        self.assertEqual('unlocked',m.status(credentials.session_token)['state'])
        self.assertEqual('manual',m.status(credentials.session_token)['access_mode'])
        self.assertFalse(m.device_store.path.exists())
        m.close();restarted=self.manager()
        self.assertEqual('locked',restarted.status()['state'])
        self.assertEqual('manual',restarted.status()['access_mode'])

    def test_disabled_preserved_configuration_reenables_without_changing_ciphertext(self):
        provider=EncryptedProtectedStorageProvider(self.context)
        provider.initialize(PASSWORD,kdf_parameters=KDF);provider.lock()
        before=self.digest();m=self.manager();c=m.local_browser();m.disable_empty_storage();m.close()
        restarted=self.manager();self.assertFalse(restarted.storage_enabled)
        restarted.enable_existing_storage()
        self.assertTrue(restarted.storage_enabled)
        self.assertEqual('locked',restarted.status()['state'])
        self.assertEqual(before,self.digest())

    def test_empty_protection_can_be_cancelled_and_recreated_with_a_new_password(self):
        open_note=self.context.knowledge/'still-readable.md'
        open_note.write_text('plain knowledge survives protection changes','utf-8')
        first=self.manager();first_session=first.local_browser()
        _,credentials=first.enable_new_storage('fictional-first-password',)
        self.assertFalse(first.status(credentials.session_token)['has_protected_objects'])
        first.disable_empty_storage();first.close()
        second=self.manager();open_session=second.open_browser()
        self.assertEqual('plain knowledge survives protection changes',open_note.read_text('utf-8'))
        initialized,recreated=second.enable_new_storage('fictional-second-password')
        self.assertTrue(initialized.recovery_key)
        self.assertEqual('unlocked',second.status(recreated.session_token)['state'])
        self.assertEqual('manual',second.status(recreated.session_token)['access_mode'])
        self.assertTrue(second.provider.verify_passphrase('fictional-second-password'))
        with self.assertRaises(InvalidUnlockMaterial):
            second.provider.verify_passphrase('fictional-first-password')
        second.close();third=self.manager()
        self.assertEqual('locked',third.status()['state'])
        credentials=third.unlock_with_passphrase('fictional-second-password')
        self.assertEqual('unlocked',third.status(credentials.session_token)['state'])
        third.disable_empty_storage();third.close()
        fourth=self.manager();self.assertFalse(fourth.storage_enabled)
        self.assertEqual('open',fourth.status(fourth.open_browser().session_token)['access_mode'])
        self.assertEqual('plain knowledge survives protection changes',open_note.read_text('utf-8'))
        self.assertTrue(any((self.context.versions/'retired-protection').iterdir()))


if __name__=='__main__':unittest.main()
