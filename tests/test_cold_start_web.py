import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.bridge_launcher import Launcher
from knowledge_bridge.workspace import STANDARD_DIRS, initialize_workspace


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


_openers = {}

def _request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if method == "POST":
        parsed = urllib.parse.urlsplit(url)
        headers.update({
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "X-Jiedu-Local-Action": "1",
        })
    origin = urllib.parse.urlsplit(url)
    origin = f"{origin.scheme}://{origin.netloc}"
    opener = _openers.setdefault(origin, urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())))
    if method == "POST":
        try:
            with opener.open(origin+"/api/protection/status", timeout=5) as response:
                csrf = json.load(response).get("csrf_token")
            if csrf: headers["X-Jiedu-CSRF"] = csrf
        except urllib.error.HTTPError as error:
            if error.code != 409: raise
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class ColdStartWebTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]

    def _start(self, root: Path, local_data: Path, port: int) -> subprocess.Popen:
        environment = os.environ.copy()
        environment["KNOWLEDGE_BRIDGE_VAULT"] = str(root)
        environment["KNOWLEDGE_BRIDGE_LOCAL_DATA"] = str(local_data)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "knowledge_bridge.web:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if process.poll() is not None:
                self.fail(f"冷启动服务提前退出：{process.returncode}")
            try:
                status, _ = _request(f"http://127.0.0.1:{port}/api/health")
                if status == 200:
                    return process
            except OSError:
                pass
            time.sleep(0.1)
        process.terminate()
        self.fail("冷启动服务未在期限内就绪")

    def _launcher_environment(self, local_data: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("KNOWLEDGE_BRIDGE_VAULT", None)
        environment["KNOWLEDGE_BRIDGE_LOCAL_DATA"] = str(local_data)
        return environment

    def _launcher(self, action: str, local_data: Path, vault: Path | None = None) -> dict:
        command = [sys.executable, "-m", "knowledge_bridge.bridge_launcher", action,
                   "--bridge-dir", str(self.project_root)]
        if vault is not None:
            command.extend(["--vault", str(vault)])
        result = subprocess.run(command, cwd=self.project_root,
                                env=self._launcher_environment(local_data),
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=120)
        if result.returncode:
            self.fail(f"界渡启动器 {action} 失败：{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    @staticmethod
    def _write_launcher_settings(local_data: Path, port: int) -> None:
        local_data.mkdir(parents=True, exist_ok=True)
        (local_data / "bridge-settings.json").write_text(json.dumps({
            "console": {"host": "127.0.0.1", "port": port},
            "mcp": {"mode": "stdio", "host": "127.0.0.1", "port": _free_port()},
        }), encoding="utf-8")

    def _wait_for_workspace(self, port: int, workspace_id: str, previous_pid: int) -> dict:
        deadline = time.time() + 60
        last_error = None
        while time.time() < deadline:
            try:
                status, health = _request(f"http://127.0.0.1:{port}/api/health?t={time.time_ns()}")
                if (status == 200 and health.get("workspace_ready")
                        and health.get("workspace_id") == workspace_id
                        and health.get("pid") != previous_pid):
                    return health
            except OSError as error:
                last_error = error
            time.sleep(0.2)
        self.fail(f"受控重启未加载目标工作区：{last_error}")

    @staticmethod
    def _wait_for_local_logs_released(local_data: Path) -> None:
        """The detached restart helper may outlive health readiness by milliseconds."""
        deadline = time.time() + 8
        while time.time() < deadline:
            locked = False
            for path in (local_data / "runtime").glob("*.log"):
                probe = path.with_name(path.name + ".release-probe")
                try:
                    os.replace(path, probe); os.replace(probe, path)
                except OSError:
                    locked = True
                    break
            if not locked:
                return
            time.sleep(0.1)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_real_app_entry_supports_uninitialized_then_initialized_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "ordinary-empty-folder"
            local_data = base / "machine-local"
            workspace.mkdir()
            self.assertFalse((workspace / ".jiedu").exists())
            port = _free_port()
            first = self._start(workspace, local_data, port)
            try:
                status, health = _request(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(200, status)
                self.assertEqual(("uninitialized", False, None),
                                 (health["workspace_state"], health["workspace_ready"], health["workspace_id"]))
                self.assertFalse((workspace / ".jiedu").exists())
                status, blocked = _request(f"http://127.0.0.1:{port}/api/overview")
                self.assertEqual(409, status)
                self.assertEqual("workspace_not_initialized", blocked["error"])
                status, knowledge_base = _request(f"http://127.0.0.1:{port}/api/knowledge-base")
                self.assertEqual(200, status)
                self.assertEqual("uninitialized", knowledge_base["workspace_state"])
                status, initialized = _request(
                    f"http://127.0.0.1:{port}/api/knowledge-base/initialize", "POST",
                    {"path": str(workspace), "core_files": [], "owner_confirmed": True},
                )
                self.assertEqual(200, status)
                self.assertTrue(initialized["workspace_ready"])
                workspace_id = initialized["workspace_id"]
                self.assertTrue(workspace_id)
                self.assertTrue(all((workspace / name).is_dir() for name in STANDARD_DIRS))
                self.assertTrue((workspace / ".jiedu" / "workspace.json").is_file())
            finally:
                self._stop(first)
            second = self._start(workspace, local_data, port)
            try:
                status, health = _request(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(200, status)
                self.assertEqual(("loaded", True, workspace_id),
                                 (health["workspace_state"], health["workspace_ready"], health["workspace_id"]))
                status, overview = _request(f"http://127.0.0.1:{port}/api/overview")
                self.assertEqual(200, status)
                self.assertEqual(0, overview["vector_count"])
            finally:
                self._stop(second)

    def test_first_selection_initializes_b_then_controlled_restart_loads_b(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            candidate_a, selected_b = base / "candidate-a", base / "selected-b"
            local_data = base / "machine-local"
            candidate_a.mkdir(); selected_b.mkdir()
            port = _free_port(); self._write_launcher_settings(local_data, port)
            self.assertFalse((candidate_a / ".jiedu").exists())
            self._launcher("start", local_data, candidate_a)
            try:
                _, initial_health = _request(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(str(candidate_a.resolve()), initial_health["candidate_path"])
                old_pid = initial_health["pid"]
                status, initialized = _request(
                    f"http://127.0.0.1:{port}/api/knowledge-base/initialize", "POST",
                    {"path": str(selected_b), "core_files": [], "owner_confirmed": True},
                )
                self.assertEqual(200, status)
                workspace_id = initialized["workspace_id"]
                self.assertTrue(workspace_id)
                self.assertEqual(str(candidate_a.resolve()), initialized["current_runtime_path"])
                self.assertEqual(str(selected_b.resolve()), initialized["restart_target_path"])
                self.assertEqual("selected", initialized["workspace_state"])
                self.assertFalse(initialized["workspace_ready"])
                self.assertTrue(initialized["restart_required"])
                self.assertTrue(all((selected_b / name).is_dir() for name in STANDARD_DIRS))
                self.assertTrue((selected_b / ".jiedu" / "workspace.json").is_file())
                self.assertFalse((candidate_a / ".jiedu").exists())
                saved = json.loads((local_data / "knowledge-base.json").read_text("utf-8"))
                self.assertEqual((str(selected_b.resolve()), workspace_id),
                                 (saved["vault_path"], saved["workspace_id"]))
                status, denied = _request(f"http://127.0.0.1:{port}/api/lifecycle/restart", "POST",
                                          {"owner_confirmed": False})
                self.assertEqual((403, "需要Owner确认"), (status, denied["detail"]))
                status, accepted = _request(f"http://127.0.0.1:{port}/api/lifecycle/restart", "POST",
                                            {"owner_confirmed": True})
                self.assertEqual(200, status)
                self.assertEqual((str(candidate_a.resolve()), str(selected_b.resolve()), workspace_id),
                                 (accepted["current_runtime_path"], accepted["restart_target_path"], accepted["workspace_id"]))
                health = self._wait_for_workspace(port, workspace_id, old_pid)
                self.assertEqual(str(selected_b.resolve()), health["workspace_root"])
                status, knowledge_base = _request(f"http://127.0.0.1:{port}/api/knowledge-base")
                self.assertEqual((200, True, str(selected_b.resolve()), workspace_id),
                                 (status, knowledge_base["workspace_ready"], knowledge_base["current_runtime_path"], knowledge_base["workspace_id"]))
                status, overview = _request(f"http://127.0.0.1:{port}/api/overview")
                self.assertEqual((200, 0), (status, overview["vector_count"]))
                status, lifecycle = _request(f"http://127.0.0.1:{port}/api/lifecycle/status")
                self.assertEqual((200, str(selected_b.resolve()), True),
                                 (status, lifecycle["vault"], lifecycle["watcher_alive"]))
                self.assertFalse((candidate_a / ".jiedu").exists())
            finally:
                self._launcher("stop", local_data)
                self._wait_for_local_logs_released(local_data)

    def test_first_selection_connects_existing_c_then_restart_loads_c(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            candidate_a, existing_c = base / "candidate-a", base / "existing-c"
            local_data = base / "machine-local"
            candidate_a.mkdir()
            existing_context = initialize_workspace(existing_c)
            port = _free_port(); self._write_launcher_settings(local_data, port)
            self._launcher("start", local_data, candidate_a)
            try:
                _, initial_health = _request(f"http://127.0.0.1:{port}/api/health")
                old_pid = initial_health["pid"]
                status, connected = _request(
                    f"http://127.0.0.1:{port}/api/knowledge-base/connect", "POST",
                    {"path": str(existing_c), "core_files": [], "owner_confirmed": True},
                )
                self.assertEqual(200, status)
                self.assertEqual((existing_context.workspace_id, str(candidate_a.resolve()), str(existing_c.resolve()), False),
                                 (connected["workspace_id"], connected["current_runtime_path"],
                                  connected["restart_target_path"], connected["workspace_ready"]))
                status, _ = _request(f"http://127.0.0.1:{port}/api/lifecycle/restart", "POST",
                                     {"owner_confirmed": True})
                self.assertEqual(200, status)
                health = self._wait_for_workspace(port, existing_context.workspace_id, old_pid)
                self.assertEqual(str(existing_c.resolve()), health["workspace_root"])
                for endpoint in ("health", "knowledge-base", "overview"):
                    status, _ = _request(f"http://127.0.0.1:{port}/api/{endpoint}")
                    self.assertEqual(200, status, endpoint)
                self.assertFalse((candidate_a / ".jiedu").exists())
            finally:
                self._launcher("stop", local_data)
                self._wait_for_local_logs_released(local_data)

    def test_restart_failure_keeps_selected_workspace_and_identity_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            selected = base / "selected"
            context = initialize_workspace(selected)
            local_data = base / "machine-local"; local_data.mkdir()
            self._write_launcher_settings(local_data, _free_port())
            config_path = local_data / "knowledge-base.json"
            config_path.write_text(json.dumps({
                "vault_path": str(selected.resolve()), "workspace_id": context.workspace_id,
                "core_files": [], "initialized": True,
            }), encoding="utf-8")
            config_before = config_path.read_bytes()
            manifest_before = (selected / ".jiedu" / "workspace.json").read_bytes()
            broken_bridge = base / "broken-program"
            scripts = broken_bridge / ".venv312" / "Scripts"; scripts.mkdir(parents=True)
            (scripts / "kb.exe").touch()
            with patch.dict(os.environ, {"KNOWLEDGE_BRIDGE_LOCAL_DATA": str(local_data)}, clear=False):
                launcher = Launcher(broken_bridge)
                with patch.object(launcher, "health", return_value=None):
                    with self.assertRaises(OSError):
                        launcher.start(wait=0)
            self.assertEqual(config_before, config_path.read_bytes())
            self.assertEqual(manifest_before, (selected / ".jiedu" / "workspace.json").read_bytes())
            self.assertEqual(context.workspace_id,
                             json.loads(config_path.read_text("utf-8"))["workspace_id"])
            self.assertEqual("failed", json.loads((local_data / "runtime" / "bridge-runtime.json").read_text("utf-8"))["status"])


if __name__ == "__main__":
    unittest.main()

