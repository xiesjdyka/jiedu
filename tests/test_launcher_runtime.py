"""Real Windows lifecycle tests. Only temporary workspaces/processes are used."""
import ctypes
from ctypes import wintypes as w
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from knowledge_bridge.bridge_launcher import Launcher
from knowledge_bridge.process_runtime import atomic_json, identity, launch_processes, terminate_owned, in_job
from knowledge_bridge.workspace import initialize_workspace

ROOT = Path(__file__).resolve().parents[1]


def parent_job():
    """Model a disposable host whose job kills attached children on exit."""
    class Basic(ctypes.Structure):
        _fields_ = [("per_process", ctypes.c_longlong), ("per_job", ctypes.c_longlong),
                    ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.GetCurrentProcess.restype = w.HANDLE
    job = k.CreateJobObjectW(None, None); limits = Extended()
    limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return job  # Deliberately held until this temporary parent exits.


def parent_main(mode, ready_path, vault):
    job = parent_job()
    if mode == "attached":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
        result = {"child_pid": child.pid}
    else:
        result = Launcher(ROOT, Path(vault)).start()
    atomic_json(ready_path, result)
    # The driver chooses normal exit or terminates this exact, disposable parent.
    while not Path(ready_path + ".exit").exists(): time.sleep(.05)


@unittest.skipUnless(os.name == "nt", "Windows process/job semantics")
class LauncherRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jiedu-lifecycle-test-")
        self.base = Path(self.temp.name); self.vault = self.base / "fictional-vault"
        self.context = initialize_workspace(self.vault)
        self.local = self.base / "machine-local"; self.local.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); self.port = sock.getsockname()[1]
        atomic_json(self.local / "bridge-settings.json", {"console": {"host": "127.0.0.1", "port": self.port}})
        self.environment = patch.dict(os.environ, {
            "KNOWLEDGE_BRIDGE_LOCAL_DATA": str(self.local),
            "KNOWLEDGE_BRIDGE_VAULT": str(self.vault),
            "KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE": "1"})
        self.environment.start(); self.launcher = Launcher(ROOT, self.vault)

    def tearDown(self):
        try: self.launcher.stop()
        finally:
            self.environment.stop(); self.temp.cleanup()

    def parent(self, mode):
        ready = self.base / (mode + ".json")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(sys.path)
        # Base interpreter avoids a redirector so Popen identifies the parent itself.
        process = subprocess.Popen([sys._base_executable, str(Path(__file__).resolve()), "--parent",
                                    mode, str(ready), str(self.vault)], cwd=ROOT, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 40
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None: time.sleep(.05)
        if not ready.exists():
            process.kill(); _, error = process.communicate(timeout=5)
            self.fail(error.decode("utf-8", "replace"))
        return process, ready, json.loads(ready.read_text("utf-8"))

    def test_runtime_profile_selects_interpreter_and_keeps_cpu_fallback_owned(self):
        profile=self.local/'runtime'/'runtime-profile.json';profile.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(profile,{"version":1,"python":sys.executable,"model_precision":"fp16","embedding_batch_size":4,"embedding_duty_cycle":.5})
        selected=Launcher(ROOT,self.vault)
        self.assertEqual(Path(sys.executable).resolve(),selected.python)
        self.assertEqual('fp16',selected.runtime_profile['model_precision'])
        self.assertEqual(4,selected.runtime_profile['embedding_batch_size'])
        self.assertEqual(.5,selected.runtime_profile['embedding_duty_cycle'])
        self.assertIn(os.path.normcase(str(self.launcher.python.resolve())),selected.allowed_images)

    def test_old_attached_child_dies_with_temporary_job_parent(self):
        process, ready, result = self.parent("attached")
        self.assertIsNotNone(identity(result["child_pid"]))
        ready.with_name(ready.name + ".exit").touch()
        process.communicate(timeout=10)
        deadline = time.monotonic() + 5
        while identity(result["child_pid"]) and time.monotonic() < deadline: time.sleep(.05)
        self.assertIsNone(identity(result["child_pid"]))

    def test_services_survive_normal_and_terminated_temporary_parent(self):
        for mode in ("normal", "terminated"):
            with self.subTest(mode=mode):
                process, ready, result = self.parent(mode)
                try:
                    self.assertEqual("connected", result["status"])
                    if mode == "normal": ready.with_name(ready.name + ".exit").touch()
                    else: process.terminate()
                    process.communicate(timeout=10)
                    observed = self.launcher.status()
                    self.assertEqual("connected", observed["status"])
                    self.assertEqual(result["console_pid"], observed["health"]["pid"])
                    self.assertEqual(self.context.workspace_id, observed["health"]["workspace_id"])
                    self.assertEqual("isolated_test", observed["health"]["environment"])
                    self.assertTrue(observed["watcher_alive"])
                    # CPython's venv redirector creates a private child Job. Its
                    # presence is expected; the detached redirector must be outside
                    # the disposable parent's Job and survives along with the service.
                    for wrapper in observed["wrappers"]:
                        self.assertFalse(in_job(wrapper["pid"]))
                finally:
                    if process.poll() is None: process.kill(); process.communicate(timeout=10)
                    self.launcher.stop()

    def test_concurrent_start_restart_stop_and_watcher_loss(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: Launcher(ROOT, self.vault).start(), range(2)))
        self.assertEqual({False, True}, {r["reused"] for r in results})
        self.assertEqual(results[0]["console_pid"], results[1]["console_pid"])
        first = results[0]; token = first["launch_id"]
        terminate_owned(first["watcher_identity"], self.launcher.allowed_images)
        self.assertEqual("partial", self.launcher.status()["status"])
        self.assertEqual("partial", self.launcher.load()["status"])
        restarted = self.launcher.restart()
        self.assertEqual("connected", restarted["status"])
        self.assertNotEqual(token, restarted["launch_id"])
        self.assertFalse(launch_processes(token, self.launcher.allowed_images))
        self.assertEqual("stopped", self.launcher.stop()["status"])
        self.assertFalse(launch_processes(restarted["launch_id"], self.launcher.allowed_images))
        self.assertEqual("stopped", self.launcher.stop()["status"])

    def test_web_restart_resolves_explicit_machine_selection(self):
        import httpx
        atomic_json(self.local / 'knowledge-base.json', {'vault_path': str(self.vault)})
        started = self.launcher.start()
        origin=f'http://127.0.0.1:{self.port}'
        with httpx.Client(base_url=origin, timeout=10) as client:
            csrf=client.get('/api/protection/status').json()['csrf_token']
            headers={'Origin':origin,'X-Jiedu-CSRF':csrf}
            session=client.post('/api/protection/device-session',json={},headers=headers)
            session.raise_for_status()
            headers['X-Jiedu-CSRF']=session.json()['csrf_token']
            response=client.post('/api/lifecycle/restart',json={'owner_confirmed':True},headers=headers)
            response.raise_for_status()
            self.assertTrue(response.json()['accepted'])
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            current=self.launcher.status()
            if current['status']=='connected' and current['launch_id']!=started['launch_id']:
                break
            time.sleep(.5)
        else:
            self.fail('Independent web restart did not become ready')
        self.assertEqual(str(self.vault), current['health']['workspace_root'])
        self.assertEqual(str(ROOT), self.launcher.load()['bridge_dir'])
        self.assertFalse(launch_processes(started['launch_id'], self.launcher.allowed_images))

    def test_stale_pid_and_wrong_birth_or_role_never_terminate_other_process(self):
        process = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(90)"])
        try:
            self.launcher.save({"status": "connected", "console_pid": process.pid, "watcher_pid": process.pid})
            self.assertEqual("stopped", self.launcher.status()["status"])
            self.assertEqual("stopped", self.launcher.load()["status"])
            self.launcher.stop(); self.assertIsNone(process.poll())
            actual = identity(process.pid)
            forged = actual | {"role": "console", "launch_id": "fictionaltoken"}
            self.assertFalse(terminate_owned(forged, self.launcher.allowed_images))
            self.assertIsNone(process.poll())
            started = self.launcher.start()
            wrong = started["console_identity"] | {"created": started["console_identity"]["created"] + 1}
            self.assertFalse(terminate_owned(wrong, self.launcher.allowed_images))
            self.assertEqual("connected", self.launcher.status()["status"])
        finally:
            process.terminate(); process.wait(timeout=5)

    def test_failure_cleans_early_console_and_reports_failed(self):
        original = self.launcher._spawn
        def failing(role, token, log):
            if role == "watch": raise OSError("fictional watcher spawn failure")
            return original(role, token, log)
        with patch.object(self.launcher, "_spawn", side_effect=failing):
            with self.assertRaisesRegex(OSError, "fictional watcher"):
                self.launcher.start()
        state = self.launcher.load()
        self.assertEqual("failed", self.launcher.status()["status"])
        self.assertFalse(launch_processes(state["launch_id"], self.launcher.allowed_images))

    def test_foreign_listener_not_adopted_or_stopped(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port)); sock.listen()
            with self.assertRaisesRegex(RuntimeError, "占用"):
                self.launcher.start()
            self.assertEqual("partial", self.launcher.stop()["status"])
            self.assertGreaterEqual(sock.fileno(), 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--parent": parent_main(*sys.argv[2:])
    else: unittest.main()
