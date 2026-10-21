"""Installation-scoped first-run and update notice state.

The successful installer records a version transition outside the workspace.
The UI never infers an installed version from the remote update channel.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from packaging.version import Version

from .release_version import version_info

FORMAT = "jiedu-install-notice-v1"
INSTALL_IDENTITY = "jiedu-consumer-install-v1"
_LOCK = threading.RLock()
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-rc\.\d+)?\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _state_root(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    configured = os.environ.get("JIEDU_UPDATE_NOTICE_ROOT")
    if configured:
        return Path(configured).expanduser()
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Jiedu" / "install-notices"


def _installation(program: Path) -> tuple[Path, str]:
    program = Path(program).resolve()
    if program.name != "program" or program.parent.name != ".jiedu":
        raise ValueError("不是完整的界渡安装目录")
    target = program.parent.parent
    marker = target / "jiedu-install.txt"
    if marker.is_symlink() or not marker.is_file() or marker.read_text("utf-8-sig").strip() != INSTALL_IDENTITY:
        raise ValueError("未核实界渡安装身份")
    current = version_info(program)["product_version"]
    if not _VERSION.fullmatch(current):
        raise ValueError("安装版本无效")
    return target, current


def _path(target: Path, state_root: Path | None = None) -> Path:
    identity = os.path.normcase(str(target.resolve())).encode("utf-8")
    return _state_root(state_root) / (hashlib.sha256(identity).hexdigest() + ".json")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) and value.get("format") == FORMAT else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _helper_state(target: Path, token: str) -> str | None:
    if not _TOKEN.fullmatch(token):
        return None
    try:
        result = json.loads((target.parent / (".jiedu-update-" + token) / "result.json").read_text("utf-8"))
        return result.get("state") if result.get("token") == token else None
    except (OSError, ValueError):
        return None


def _active_update_previous(target: Path, current: str) -> tuple[str, str] | None:
    """Read an old 1.1.5 helper job while its original tree is safely backed up."""
    try:
        candidates = sorted(target.parent.glob(".jiedu-update-*/job.json"),
                            key=lambda path: path.stat().st_mtime, reverse=True)[:16]
    except OSError:
        return None
    for path in candidates:
        token = path.parent.name.removeprefix(".jiedu-update-")
        if not _TOKEN.fullmatch(token) or path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            job = json.loads(path.read_text("utf-8"))
            backup = target.with_name(target.name + ".previous-" + token)
            previous = job.get("old_version")
            if (job.get("token") != token or job.get("kind") != "complete-installer"
                    or job.get("target") != str(target) or job.get("backup") != str(backup)
                    or job.get("version") != current or _helper_state(target, token) != "applying"
                    or not isinstance(previous, str)
                    or not _VERSION.fullmatch(previous) or Version(previous) >= Version(current)
                    or backup.is_symlink() or not backup.is_dir()):
                continue
            old_program = backup / ".jiedu" / "program"
            if version_info(old_program)["product_version"] == previous:
                return previous, token
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def record_install(program: Path, previous: str | None = None, *,
                   state_root: Path | None = None) -> dict:
    """Only the installer calls this after its file inventory validates."""
    target, current = _installation(program)
    if previous and (not _VERSION.fullmatch(previous) or Version(previous) > Version(current)):
        raise ValueError("此前安装版本无效")
    path = _path(target, state_root)
    with _LOCK:
        prior = _read(path)
        # Re-running the same installer must not turn an unseen upgrade into a
        # fresh install or clear its acknowledgement state.
        if prior.get("installed_version") == current:
            helper_state = _helper_state(target, prior.get("helper_token") or "")
            if prior.get("confirmed_version") == current or helper_state in {
                    "applying", "installed", "finalizing", "complete"}:
                return status(program, state_root=state_root)
        helper = None if previous else _active_update_previous(target, current)
        if helper:
            previous, helper_token = helper
        else:
            helper_token = None
        if not previous and prior.get("installed_version") and prior["installed_version"] != current:
            candidate = prior["installed_version"]
            if isinstance(candidate, str) and _VERSION.fullmatch(candidate) and Version(candidate) < Version(current):
                previous = candidate
        upgraded = bool(previous and Version(previous) < Version(current))
        state = {"format": FORMAT, "installed_version": current,
                 "previous_version": previous if upgraded else None,
                 "kind": "upgrade" if upgraded else "fresh_install",
                 "helper_token": helper_token,
                 "confirmed_version": None if helper_token else current,
                 "acknowledged_version": (prior.get("acknowledged_version") if
                                          prior.get("installed_version") == current else None),
                 "onboarding_completed": bool(upgraded or prior.get("onboarding_completed")),
                 "recorded_at": datetime.now(timezone.utc).isoformat()}
        _write(path, state)
    return status(program, state_root=state_root)


def status(program: Path, *, state_root: Path | None = None) -> dict:
    try:
        target, current = _installation(program)
    except (OSError, ValueError):
        return {"state": "not_installed", "version": None, "show_update": False,
                "show_first_run": False, "message": "尚未核实完整安装。"}
    state = _read(_path(target, state_root))
    if state.get("installed_version") != current:
        return {"state": "unconfirmed", "version": current, "show_update": False,
                "show_first_run": False, "message": "安装记录尚未核实，请稍后重新检查。"}
    if state.get("confirmed_version") != current:
        helper_state = _helper_state(target, state.get("helper_token") or "")
        if helper_state == "complete":
            with _LOCK:
                state = _read(_path(target, state_root))
                if state.get("installed_version") == current:
                    state["confirmed_version"] = current
                    _write(_path(target, state_root), state)
        elif helper_state in {"applying", "installed", "finalizing"}:
            return {"state": "pending_confirmation", "version": current, "show_update": False,
                    "show_first_run": False, "message": "正在确认更新完成；你可以继续使用。"}
        else:
            return {"state": "unconfirmed", "version": current, "show_update": False,
                    "show_first_run": False, "message": "更新结果暂时无法确认，请稍后重新检查。"}
    kind = state.get("kind")
    return {"state": "ready", "version": current, "previous_version": state.get("previous_version"),
            "kind": kind, "show_update": kind == "upgrade" and state.get("acknowledged_version") != current,
            "show_first_run": kind == "fresh_install" and not state.get("onboarding_completed"),
            "onboarding_completed": bool(state.get("onboarding_completed")),
            "acknowledged_version": state.get("acknowledged_version"),
            "message": "本机安装版本已核实。"}


def acknowledge(program: Path, *, state_root: Path | None = None) -> dict:
    target, current = _installation(program)
    path = _path(target, state_root)
    with _LOCK:
        state = _read(path)
        if state.get("installed_version") != current:
            raise ValueError("安装版本尚未核实")
        state["acknowledged_version"] = current
        _write(path, state)
    return status(program, state_root=state_root)


def complete_onboarding(program: Path, *, state_root: Path | None = None) -> dict:
    target, current = _installation(program)
    path = _path(target, state_root)
    with _LOCK:
        state = _read(path)
        if state.get("installed_version") != current:
            raise ValueError("安装版本尚未核实")
        state["onboarding_completed"] = True
        _write(path, state)
    return status(program, state_root=state_root)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["record-install"])
    parser.add_argument("--program", required=True)
    parser.add_argument("--previous")
    args = parser.parse_args()
    print(json.dumps(record_install(Path(args.program), args.previous), ensure_ascii=False))
