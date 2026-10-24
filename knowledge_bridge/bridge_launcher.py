from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from filelock import FileLock, Timeout
from .process_runtime import atomic_json, identity, matches, terminate_owned, launch_processes, spawn_service, in_job, image_processes, wait_identity_exit
from datetime import datetime, timezone
from pathlib import Path
from .workspace import resolve_workspace_root
from .package_layout import component, python_executable


def now(): return datetime.now(timezone.utc).isoformat()


class Launcher:
    def __init__(self, bridge_dir: Path, vault: Path | None = None):
        self.bridge_dir = bridge_dir.resolve()
        self.local_data = Path(
            os.environ.get("KNOWLEDGE_BRIDGE_LOCAL_DATA") or component("control", self.bridge_dir) or self.bridge_dir / "data"
        ).expanduser().resolve()
        self.config_path = self.local_data / "knowledge-base.json"
        if vault is None:
            vault=resolve_workspace_root(self.bridge_dir,self.local_data,
                                         override=os.environ.get('KNOWLEDGE_BRIDGE_VAULT'))
        self.vault = vault.resolve()
        self.runtime = self.local_data / "runtime"; self.state_path = self.runtime / "bridge-runtime.json"
        self.log_path = self.runtime / "bridge-launcher.log"
        env_dir = next((path for path in (self.bridge_dir / ".venv312", self.bridge_dir / ".venv") if (path / "Scripts" / "python.exe").exists()), self.bridge_dir / ".venv312")
        self.runtime_profile_path=self.runtime/"runtime-profile.json";self.runtime_profile={"version":1,"model_precision":"fp32","embedding_batch_size":32,"embedding_duty_cycle":1.0}
        try:
            saved=json.loads(self.runtime_profile_path.read_text('utf-8'))
            if saved.get('version')!=1:raise ValueError('运行配置版本无效')
            precision=str(saved.get('model_precision') or 'fp32').lower();batch=int(saved.get('embedding_batch_size') or (4 if precision=='fp16' else 32));duty=float(saved.get('embedding_duty_cycle',1.0))
            candidate=component('python', self.bridge_dir) or Path(saved['python']).expanduser().resolve()
            if precision not in {'fp32','fp16'} or not 1<=batch<=128 or not .1<=duty<=1 or not candidate.is_file():raise ValueError('运行配置字段无效')
            self.runtime_profile={"version":1,"python":str(candidate),"model_precision":precision,"embedding_batch_size":batch,"embedding_duty_cycle":duty}
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError) as exc:raise ValueError(f'运行配置无效：{exc}') from exc
        self.python=component("python", self.bridge_dir) or Path(self.runtime_profile.get('python') or env_dir/"Scripts"/"python.exe")
        self.pythonw=self.python.with_name('pythonw.exe')
        self.kb = self.bridge_dir / "knowledge_bridge" / "cli.py" if component("python", self.bridge_dir) else env_dir / "Scripts" / "kb.exe"; self.settings_path = self.local_data / "bridge-settings.json"
        self.lock = FileLock(str(self.runtime / "launcher.lock"), timeout=120)
        bootstrap=(env_dir/"Scripts"/"python.exe",env_dir/"Scripts"/"pythonw.exe")
        self.allowed_images = {os.path.normcase(str(p.resolve())) for p in (self.python,self.pythonw,*bootstrap)}
        for configured in {env_dir,self.python.parent.parent}:
            try:
                home = next(line.split("=", 1)[1].strip() for line in (configured / "pyvenv.cfg").read_text("utf-8").splitlines() if line.startswith("home ="))
                self.allowed_images.update(os.path.normcase(str((Path(home) / name).resolve())) for name in ("python.exe", "pythonw.exe"))
            except (OSError, StopIteration): pass

    def settings(self):
        defaults={"console":{"host":"127.0.0.1","port":8765},"mcp":{"mode":"stdio","host":"127.0.0.1","port":8766}}
        try:
            saved=json.loads(self.settings_path.read_text("utf-8"))
            for key,value in saved.items(): defaults[key]=defaults.get(key,{})|value if isinstance(value,dict) else value
        except (OSError,ValueError): pass
        if defaults["console"]["host"]!="127.0.0.1" or defaults["mcp"]["host"]!="127.0.0.1": raise ValueError("安全策略拒绝非localhost监听")
        return defaults

    def load(self):
        try:return json.loads(self.state_path.read_text("utf-8"))
        except (OSError,ValueError):return {}

    def save(self,data):
        atomic_json(self.state_path, data)

    def log(self,message):
        self.runtime.mkdir(parents=True,exist_ok=True)
        with self.log_path.open("a",encoding="utf-8") as handle:handle.write(f"{now()} {message}\n")

    @staticmethod
    def alive(pid):
        return identity(pid) is not None

    def health(self,timeout=.8):
        settings=self.settings(); url=f"http://127.0.0.1:{settings['console']['port']}/api/health"
        try:
            # Loopback health must never use the desktop's HTTP/system proxy.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=timeout) as response:return json.loads(response.read().decode("utf-8"))
        except Exception:return None

    def _receipt(self, state, role):
        token = state.get("launch_id")
        if not token or not isinstance(token, str) or not token.isalnum(): return None
        try:
            record = json.loads((self.runtime / f"process-{role}-{token}.json").read_text("utf-8"))
            if (record.get("role") != role or record.get("launch_id") != token
                    or record.get("vault") != state.get("vault")
                    or record.get("bridge_dir") != str(self.bridge_dir)
                    or record.get("local_data") != str(self.local_data)): return None
            return record
        except (OSError, ValueError): return None

    def _owned(self, record):
        return matches(identity((record or {}).get("pid")), record, self.allowed_images)

    def _observe(self, state):
        health = self.health()
        console = self._receipt(state, "console"); watcher = self._receipt(state, "watch")
        console_alive = self._owned(console); watcher_alive = self._owned(watcher)
        health_owned = bool(health and console_alive and health.get("pid") == console["pid"]
                            and health.get("launch_id") == state.get("launch_id")
                            and (health.get("workspace_root") or health.get("candidate_path")) == state.get("vault"))
        if health_owned and watcher_alive and watcher.get("ready") and state.get("vault") == str(self.vault): status = "connected"
        elif health or console_alive or watcher_alive or self._port_open(): status = "partial"
        elif state.get("status") == "failed": status = "failed"
        else: status = "stopped" if self.kb.exists() else "not_configured"
        return {**state, "status": status, "bridge_dir": str(self.bridge_dir), "vault": str(self.vault),
                "console_pid": console["pid"] if console_alive else None,
                "watcher_pid": watcher["pid"] if watcher_alive else None,
                "console_identity": console if console_alive else None,
                "watcher_identity": watcher if watcher_alive else None,
                "console_alive": console_alive, "watcher_alive": watcher_alive,
                "health": health, "health_owned": health_owned, "observed_at": now(),
                "last_error": state.get("last_error"), "log_path": str(self.log_path)}

    def status(self):
        # A running mutation owns persistence. Status remains responsive during startup.
        self.runtime.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock.acquire(timeout=0):
                state = self.load(); observed = self._observe(state)
                self.save(state | {key: observed[key] for key in ("status", "observed_at", "console_pid", "watcher_pid")})
                return observed
        except Timeout:
            return self._observe(self.load())

    def _spawn(self, role, token, log_handle):
        executable = self.pythonw if self.pythonw.exists() else self.python
        pid = spawn_service(executable,self.bridge_dir,role,token,self.vault,self.local_data,
                            model_precision=self.runtime_profile['model_precision'],
                            embedding_batch_size=self.runtime_profile['embedding_batch_size'],
                            embedding_duty_cycle=self.runtime_profile['embedding_duty_cycle'])
        if in_job(pid):
            raise OSError("独立启动核验失败：启动壳仍位于Windows Job中；已拒绝交付附属服务")
        return pid

    def _port_open(self):
        with socket.socket() as probe:
            probe.settimeout(.3)
            return probe.connect_ex(("127.0.0.1", self.settings()["console"]["port"])) == 0

    def start(self, wait=90):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with self.lock: return self._start(wait)

    def _start(self, wait=90):
        from .update_quiesce import read_stop_request
        if read_stop_request(self.local_data, self.load().get('launch_id'), 'console'):
            raise RuntimeError('软件更新正在安全退出，请等待安装完成后再打开界渡')
        current = self._observe(self.load())
        if current.get('console_alive') and not current.get('health_owned'):
            # A busy existing server can miss one short health timeout. Do not
            # tear it down merely because a second desktop window was opened.
            retry_until = time.monotonic() + min(5, wait)
            while time.monotonic() < retry_until and not current.get('health_owned'):
                time.sleep(.25)
                current = self._observe(self.load())
        if current["status"] == "connected" and self.load().get("vault") == str(self.vault):
            return current | {"reused": True}
        state = self.load()
        started_new = False
        try:
            if not self.kb.exists(): raise FileNotFoundError(f"找不到正式启动器: {self.kb}")
            if current["health"] and not current["health_owned"]:
                raise RuntimeError("端口由未核验的服务占用；拒绝接管或停止，请检查监听进程")
            # Also detect a listener which does not speak our health protocol.
            if not current["health_owned"] and self._port_open():
                raise RuntimeError("Console端口已被其他进程占用")
            self._stop_owned(state)
            started_new = True
            state = {"status": "starting", "bridge_dir": str(self.bridge_dir), "vault": str(self.vault),
                     "started_at": now(), "stopped_at": None, "last_error": None,
                     "launch_id": uuid.uuid4().hex, "wrappers": [],
                     "runtime_python":str(self.python),"model_precision":self.runtime_profile['model_precision'],
                     "embedding_batch_size":self.runtime_profile['embedding_batch_size'],
                     "embedding_duty_cycle":self.runtime_profile['embedding_duty_cycle']}
            self.save(state)
            with self.log_path.open("a", encoding="utf-8") as log_handle:
                for role in ("console", "watch"):
                    pid = self._spawn(role, state["launch_id"], log_handle)
                    record = identity(pid)
                    if record:
                        record.update(role=role, launch_id=state["launch_id"])
                        state["wrappers"].append(record)
                    self.save(state)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    observed = self._observe(state)
                    if observed["status"] == "connected":
                        state.update(status="connected", ready_at=now(),
                                     console_pid=observed["console_pid"], watcher_pid=observed["watcher_pid"])
                        self.save(state); self.log("Bridge ready: " + state["launch_id"])
                        return self._observe(state) | {"reused": False}
                    for role in ("console", "watch"):
                        wrappers = [r for r in state["wrappers"] if r["role"] == role]
                        if not self._owned(self._receipt(state, role)) and not any(self._owned(r) for r in wrappers):
                            raise RuntimeError(f"{role}进程在就绪前退出或身份无法核验")
                    time.sleep(.25)
                raise RuntimeError("Console/Watcher启动超时，已回收本次启动的已核验进程")
        except Exception as exc:
            if started_new:
                self._stop_owned(state)
                state.update(status="failed", last_error=str(exc), console_pid=None, watcher_pid=None)
            else:
                # A rejected preflight owns no new process. Keep the existing
                # launch receipts and PIDs so a retry can reconnect safely.
                state.update(status=current['status'], last_error=str(exc))
            self.save(state); self.log(f"Start failed: {exc}")
            raise

    def _stop_owned(self, state):
        # Discover by this launch's exact command line, never by ancestry or PID alone.
        # Do this before stopping wrappers, which could otherwise leave an orphan.
        records = launch_processes(state.get("launch_id"), self.allowed_images)
        for role in ("watch", "console"):
            terminate_owned(self._receipt(state, role), self.allowed_images)
        for record in records:
            terminate_owned(record, self.allowed_images)
        # A redirector can create its interpreter between the first snapshot and
        # its termination. With redirectors gone, a second exact-token pass closes
        # that race without walking/killing any unrelated process tree.
        for record in launch_processes(state.get("launch_id"), self.allowed_images):
            terminate_owned(record, self.allowed_images)

    def snapshot_update_processes(self, launch_id=None, *, native_executable=None):
        """Capture ownership before an external helper starts; do not stop here."""
        from .update_quiesce import TOKEN
        state = self.load()
        current = state.get('launch_id')
        if not TOKEN.fullmatch(str(current or '')) or (launch_id is not None and launch_id != current):
            raise ValueError('更新启动身份已变化，请重新准备')
        if state.get('bridge_dir') != str(self.bridge_dir) or state.get('vault') != str(self.vault):
            raise ValueError('更新启动记录不属于当前程序与工作区')
        records = launch_processes(current, self.allowed_images)
        for role in ('console', 'watch'):
            receipt = self._receipt(state, role)
            if receipt and self._owned(receipt):
                records.append(receipt)
        expected_native = self.bridge_dir.parent.parent / '界渡.exe'
        if native_executable is not None and Path(native_executable).resolve() != expected_native.resolve():
            raise ValueError('原生程序不属于当前安装目录')
        native = expected_native.resolve()
        if native.is_file():
            records.extend(record | {'role': 'native', 'launch_id': current}
                           for record in image_processes(native))
        unique = {(record['pid'], record['created']): record for record in records}
        return {'launch_id': current, 'program': str(self.bridge_dir), 'local_data': str(self.local_data),
                'native_executable': str(native),
                'processes': sorted(unique.values(), key=lambda record: (record['pid'], record['created']))}

    def request_update_stop(self, token, launch_id=None):
        """Fast cooperative request. The prepared external helper waits for exit."""
        from .update_quiesce import request_shutdown
        proof = self.snapshot_update_processes(launch_id)
        request = request_shutdown(self.local_data, token, proof['launch_id'], program=self.bridge_dir)
        return proof | {'token': token, 'request': request}

    def wait_update_stop(self, proof, timeout=120):
        """External-helper utility: no forced stop and no sleep-as-safety proof."""
        if (proof.get('program') != str(self.bridge_dir) or proof.get('local_data') != str(self.local_data)
                or not isinstance(proof.get('processes'), list)):
            raise ValueError('更新退出证明不属于当前程序')
        deadline = time.monotonic() + max(0, min(float(timeout), 600))
        pending = []
        for record in proof['processes']:
            if not wait_identity_exit(record, max(0, deadline - time.monotonic())):
                pending.append({'pid': record['pid'], 'role': record.get('role')})
        remaining = launch_processes(proof.get('launch_id'), self.allowed_images)
        native = Path(proof.get('native_executable', ''))
        if native.is_file():
            remaining.extend(image_processes(native))
        if pending or remaining or self._port_open():
            raise RuntimeError('仍有服务或窗口未安全退出；已保留原程序，没有强制停止')
        return {'stopped': True, 'launch_id': proof.get('launch_id'), 'forced_termination': False,
                'installation_prefix_process_scan_still_required': True}

    def stop(self, graceful=False):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with self.lock: return self._stop(graceful)

    def _stop(self, graceful=False):
        state=self.load()
        if graceful:
            try:
                sessions=json.loads((self.local_data/"sessions"/"sessions.json").read_text("utf-8")).get("sessions",{}).values()
                active=any(not x.get("ended_at") and time.time()-datetime.fromisoformat(x["last_activity_at"].replace("Z","+00:00")).timestamp()<120 for x in sessions)
            except Exception:active=False
            if active:time.sleep(20)
        self._stop_owned(state)
        from .encoder_service import stop as stop_encoder
        stop_encoder(self.vault/'.jiedu/state/encoder')
        state.update({"status":"stopped","stopped_at":now(),"console_pid":None,"watcher_pid":None,"last_error":None});self.save(state);self.log("Bridge stopped")
        observed = self._observe(state)
        if observed["status"] == "partial":
            state.update(status="partial", last_error="端口仍有未核验的服务；未按历史PID停止其他进程")
            self.save(state); observed.update(status="partial", last_error=state["last_error"])
        return observed

    def restart(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._stop()
            return self._start()
    def sync(self):
        env=os.environ.copy();env["KNOWLEDGE_BRIDGE_VAULT"]=str(self.vault)
        result=subprocess.run([str(self.python),"-m","knowledge_bridge.cli","sync"],cwd=self.bridge_dir,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise RuntimeError(result.stderr.strip() or "同步失败")
        return json.loads(result.stdout)


def main():
    parser=argparse.ArgumentParser("Knowledge Bridge launcher");parser.add_argument("command",choices=["start","status","stop","restart","sync"]);parser.add_argument("--bridge-dir",default=str(Path(__file__).resolve().parents[1]));parser.add_argument("--vault");parser.add_argument("--graceful",action="store_true");args=parser.parse_args()
    launcher=Launcher(Path(args.bridge_dir),Path(args.vault) if args.vault else None)
    try:result=getattr(launcher,args.command)(**({"graceful":args.graceful} if args.command=="stop" else {}));print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        import traceback
        launcher.log(traceback.format_exc())
        print(json.dumps({"status":"failed","last_error":str(exc)},ensure_ascii=False));raise SystemExit(1)

if __name__=="__main__":main()
