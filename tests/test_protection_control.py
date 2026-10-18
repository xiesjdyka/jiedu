import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_bridge.protected_storage import InvalidUnlockMaterial
from knowledge_bridge.protection_control import (
    OwnerSessionRequired, ProtectionLifecycleManager, UnlockRateLimited,
)
from knowledge_bridge.workspace import initialize_workspace


PASSPHRASE = "fictional-owner-passphrase-2026"
NEW_PASSPHRASE = "fictional-owner-new-passphrase-2026"
TEST_KDF = {"name": "argon2id", "iterations": 2, "lanes": 1,
            "memory_cost_kib": 19 * 1024, "length": 32}


class FakeClock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


def manager_for(root: Path, clock=None):
    context = initialize_workspace(root)
    return ProtectionLifecycleManager(
        context, settings_path=root.parent / "machine" / "protection-control.json",
        clock=clock or FakeClock(), default_auto_lock_seconds=0,
        owner_session_seconds=3,
        minimum_auto_lock_seconds=1, failure_delay_base=.1,
        initialization_kdf_parameters=TEST_KDF,
    )


class ProtectionControlLifecycleTests(unittest.TestCase):
    def test_initialize_session_lock_two_unlocks_and_one_time_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            manager = manager_for(root)
            initialized, owner = manager.initialize(PASSPHRASE)
            recovery = initialized.recovery_key
            self.assertEqual("unlocked", manager.status(owner.session_token)["state"])
            self.assertNotIn("recovery_key", manager.status(owner.session_token))
            with self.assertRaises(FileExistsError): manager.initialize(PASSPHRASE)
            manager.acknowledge_recovery(owner.session_token, owner.csrf_token)
            self.assertTrue(manager.status(owner.session_token)["recovery_acknowledged"])
            manager.lock()
            password_owner = manager.unlock_with_passphrase(PASSPHRASE)
            manager.lock()
            recovery_owner = manager.unlock_with_recovery(recovery)
            self.assertTrue(manager.status(recovery_owner.session_token)["owner_session_active"])
            with self.assertRaises(OwnerSessionRequired):
                manager.owner_session(password_owner.session_token)

    def test_wrong_unlock_is_rate_limited_without_damaging_keyring(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock(); manager = manager_for(Path(temporary) / "workspace", clock)
            manager.initialize(PASSPHRASE); manager.lock()
            keyring_before = hashlib.sha256(manager.provider.keyring_path.read_bytes()).hexdigest()
            with self.assertRaises(InvalidUnlockMaterial):
                manager.unlock_with_passphrase("wrong-fictional-password")
            with self.assertRaises(UnlockRateLimited):
                manager.unlock_with_passphrase(PASSPHRASE)
            clock.advance(1)
            owner = manager.unlock_with_passphrase(PASSPHRASE)
            self.assertEqual(keyring_before, hashlib.sha256(manager.provider.keyring_path.read_bytes()).hexdigest())
            self.assertTrue(manager.status(owner.session_token)["owner_session_active"])

    def test_auto_lock_health_equivalent_status_does_not_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock(); manager = manager_for(Path(temporary) / "workspace", clock)
            _, owner = manager.initialize(PASSPHRASE)
            manager.set_auto_lock(3, owner.session_token, owner.csrf_token)
            clock.advance(2)
            self.assertEqual(1, manager.status(owner.session_token)["auto_lock_remaining_seconds"])
            clock.advance(2)
            self.assertTrue(manager.expire_if_needed())
            self.assertEqual("locked", manager.status(owner.session_token)["state"])
            with self.assertRaises(OwnerSessionRequired): manager.owner_session(owner.session_token)

    def test_standard_mode_keeps_storage_unlocked_after_owner_session_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock(); manager = manager_for(Path(temporary) / "workspace", clock)
            _, owner = manager.initialize(PASSPHRASE)
            self.assertFalse(manager.status(owner.session_token)["auto_lock_enabled"])
            clock.advance(61)
            status = manager.status(owner.session_token)
            self.assertEqual("unlocked", status["state"])
            self.assertFalse(status["owner_session_active"])
            self.assertIsNone(status["auto_lock_remaining_seconds"])
            manager.lock()
            self.assertEqual("locked", manager.status()["state"])

    def test_recovery_acknowledgement_persists_across_lock_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / "workspace"; manager = manager_for(root)
            _, owner = manager.initialize(PASSPHRASE)
            self.assertEqual("not_recorded", manager.status(owner.session_token)["recovery_acknowledgement_status"])
            manager.acknowledge_recovery(owner.session_token, owner.csrf_token)
            acknowledged_at = manager.status(owner.session_token)["recovery_acknowledged_at"]
            self.assertTrue(acknowledged_at)
            manager.close()
            restarted = ProtectionLifecycleManager(
                manager.context, settings_path=base / "machine" / "protection-control.json",
                minimum_auto_lock_seconds=1, initialization_kdf_parameters=TEST_KDF)
            status = restarted.status()
            self.assertEqual("locked", status["state"])
            self.assertEqual("confirmed", status["recovery_acknowledgement_status"])
            self.assertEqual(acknowledged_at, status["recovery_acknowledged_at"])

    def test_legacy_sixty_second_setting_migrates_to_process_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / "workspace"; context = initialize_workspace(root)
            settings = base / "machine" / "protection-control.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"format_version": 1, "workspaces": {
                context.workspace_id: {"auto_lock_seconds": 60}}}), encoding="utf-8")
            manager = ProtectionLifecycleManager(
                context, settings_path=settings, minimum_auto_lock_seconds=1,
                initialization_kdf_parameters=TEST_KDF)
            self.assertEqual(0, manager.status()["auto_lock_seconds"])
            saved = json.loads(settings.read_text("utf-8"))
            self.assertEqual(2, saved["format_version"])
            self.assertEqual("process", saved["workspaces"][context.workspace_id]["lock_mode"])

    def test_change_passphrase_requires_owner_and_workspace_switch_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); first = manager_for(base / "first")
            _, owner = first.initialize(PASSPHRASE)
            with self.assertRaises(OwnerSessionRequired):
                first.change_passphrase(PASSPHRASE, NEW_PASSPHRASE, "forged", "forged")
            first.change_passphrase(PASSPHRASE, NEW_PASSPHRASE,
                                    owner.session_token, owner.csrf_token)
            first.lock(); self.assertRaises(InvalidUnlockMaterial,
                first.unlock_with_passphrase, PASSPHRASE)
            first._next_unlock_at = 0
            new_owner = first.unlock_with_passphrase(NEW_PASSPHRASE)
            second_context = initialize_workspace(base / "second")
            first.switch_workspace(second_context)
            self.assertEqual("not_configured", first.status()["state"])
            with self.assertRaises(OwnerSessionRequired): first.owner_session(new_owner.session_token)

    def test_restart_constructs_locked_provider_and_error_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"; first = manager_for(root)
            first.initialize(PASSPHRASE); first.close()
            restarted = ProtectionLifecycleManager(
                first.context, settings_path=Path(temporary) / "machine.json",
                minimum_auto_lock_seconds=1, initialization_kdf_parameters=TEST_KDF)
            self.assertEqual("locked", restarted.status()["state"])
            restarted.provider.format_path.write_text("{}", encoding="utf-8")
            damaged = ProtectionLifecycleManager(
                first.context, settings_path=Path(temporary) / "machine2.json",
                minimum_auto_lock_seconds=1, initialization_kdf_parameters=TEST_KDF)
            self.assertEqual("error", damaged.status()["state"])
            self.assertFalse(damaged.status()["protection_active"])


WEB_SCRIPT = r'''
import json, os
from pathlib import Path
from fastapi.testclient import TestClient
from knowledge_bridge.workspace import initialize_workspace

root=Path(os.environ["KB_TEST_WORKSPACE"])
initialize_workspace(root)
from knowledge_bridge.web import app, OWNER_COOKIE
origin="http://127.0.0.1:8765"
results={}
with TestClient(app,base_url=origin) as client:
    start=client.get("/api/protection/status")
    results["start"]=start.json()
    csrf=start.json()["csrf_token"]
    results["cookie_flags"]=start.headers.get("set-cookie","")
    repeated=client.get("/api/protection/status")
    results["challenge_reused"]=repeated.json()["csrf_token"]==csrf
    forged=client.post("/api/protection/lock",json={"owner_confirmed":True},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    results["forged_status"]=forged.status_code
    bad_origin=client.post("/api/protection/initialize",json={"passphrase":"fictional-passphrase-2026","confirmation":"fictional-passphrase-2026"},headers={"Origin":"http://evil.invalid","X-Jiedu-CSRF":csrf})
    results["bad_origin"]=bad_origin.status_code
    start=client.get("/api/protection/status");csrf=start.json()["csrf_token"]
    missing_csrf=client.post("/api/protection/initialize",json={"passphrase":"fictional-passphrase-2026","confirmation":"fictional-passphrase-2026"},headers={"Origin":origin})
    results["missing_csrf"]=missing_csrf.status_code
    start=client.get("/api/protection/status");csrf=start.json()["csrf_token"]
    initialized=client.post("/api/protection/initialize",json={"passphrase":"fictional-passphrase-2026","confirmation":"fictional-passphrase-2026"},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    body=initialized.json();results["initialized_status"]=initialized.status_code
    results["recovery_once"]="recovery_key" in body
    results["recovery"]=body.get("recovery_key")
    results["owner_cookie"]=initialized.headers.get("set-cookie","")
    results["initialized_body"]={k:v for k,v in body.items() if k!="recovery_key"}
    state=client.get("/api/protection/status").json();results["status_has_recovery"]="recovery_key" in state
    csrf=state["csrf_token"]
    duplicate=client.post("/api/protection/initialize",json={"passphrase":"fictional-passphrase-2026","confirmation":"fictional-passphrase-2026"},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    results["duplicate"]=duplicate.status_code
    locked=client.post("/api/protection/lock",json={},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    results["locked"]=locked.status_code
    results["no_object_api"]=client.get("/api/protection/objects/anything").status_code
print(json.dumps(results,ensure_ascii=False))
'''


class ProtectionControlWebTests(unittest.TestCase):
    def test_owner_cookie_csrf_origin_and_no_object_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            env = os.environ.copy()
            env.update({
                "KB_TEST_WORKSPACE": str(base / "workspace"),
                "KNOWLEDGE_BRIDGE_VAULT": str(base / "workspace"),
                "KNOWLEDGE_BRIDGE_LOCAL_DATA": str(base / "local"),
                "KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE": "1",
                "PYTHONUTF8": "1",
            })
            project_root=Path(__file__).resolve().parents[1]
            runtime=project_root / ".venv312" / "Scripts" / "python.exe"
            completed = subprocess.run(
                [str(runtime if runtime.exists() else Path(sys.executable)), "-c", WEB_SCRIPT], cwd=project_root,
                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(0, completed.returncode, completed.stderr)
            data = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual("disabled", data["start"]["state"])
            self.assertFalse(data["start"]["storage_enabled"])
            self.assertTrue(data["challenge_reused"])
            # Open mode intentionally gives the local Console a same-origin
            # management session without an Owner/password round trip.
            self.assertEqual(200, data["forged_status"])
            self.assertEqual(403, data["bad_origin"])
            self.assertEqual(403, data["missing_csrf"])
            self.assertEqual(200, data["initialized_status"])
            self.assertTrue(data["recovery_once"])
            self.assertFalse(data["status_has_recovery"])
            self.assertIn("HttpOnly", data["owner_cookie"])
            self.assertIn("SameSite=strict", data["owner_cookie"])
            self.assertNotIn(data["recovery"], json.dumps(data["initialized_body"]))
            self.assertEqual(409, data["duplicate"])
            self.assertEqual(200, data["locked"])
            self.assertEqual(404, data["no_object_api"])
            protected = base / "workspace" / ".jiedu" / "protected"
            disk = b"".join(path.read_bytes() for path in protected.rglob("*") if path.is_file())
            self.assertNotIn(PASSPHRASE.encode(), disk)

    def test_recovery_confirmation_ui_has_direct_primary_action(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text("utf-8")
        self.assertIn("我已安全保存，继续", script)
        self.assertNotIn("id=\"recoverySaved\"", script)
        self.assertIn("系统没有保存此前的恢复密钥确认记录", script)
        self.assertIn("我确认此前的恢复密钥已安全保存", script)
        self.assertIn("保护层已配置，但当前锁定。解锁后可以配对客户端或修改权限。", script)
        self.assertIn("data-go-protection", script)


if __name__ == "__main__": unittest.main()
