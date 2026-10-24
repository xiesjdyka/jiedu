"""Read-only local installation evidence, separate from conversation discovery."""
from __future__ import annotations

import os
import json
import shutil
import time
from pathlib import Path

_NAMES = {
    'workbuddy': ('WorkBuddy', 'workbuddy'),
    'opencode': ('OpenCode', 'opencode'),
    'zcode': ('ZCode', 'zcode'),
    'kimi': ('Kimi', 'kimi'),
}
_CACHE: dict[str, tuple[float, str]] = {}


def _registered(name: str, command: str) -> str:
    if os.name != 'nt':
        return 'unknown'
    import winreg
    roots = ((winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_LOCAL_MACHINE, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'))
    matched = False
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for index in range(count):
                    try:
                        with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as app:
                            display = str(winreg.QueryValueEx(app, 'DisplayName')[0])
                            if name.casefold() in display.casefold():
                                matched = True
                                try:
                                    install = Path(str(winreg.QueryValueEx(app, 'InstallLocation')[0]).strip('"'))
                                    if any((install / executable).is_file() for executable in
                                           (name + '.exe', command + '.exe')):
                                        return 'present'
                                except (OSError, ValueError):
                                    pass
                                try:
                                    icon = str(winreg.QueryValueEx(app, 'DisplayIcon')[0]).split(',', 1)[0].strip('"')
                                    icon_path = Path(icon)
                                    if icon_path.is_file() and icon_path.suffix.casefold() == '.exe' and \
                                            icon_path.stem.casefold() in {name.casefold(), command.casefold()}:
                                        return 'present'
                                except (OSError, ValueError):
                                    pass
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue
    return 'unknown' if matched else 'absent'


def _npm_binary(package: Path, command: str) -> bool:
    try:
        data = json.loads(package.read_text('utf-8'))
        if data.get('name') != 'opencode-ai':
            return False
        binary = (data.get('bin') or {}).get(command)
        if not isinstance(binary, str):
            return False
        root = package.parent.resolve()
        target = (root / binary).resolve()
        return target.is_relative_to(root) and target.is_file()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def state(kind: str) -> str:
    """Present, absent from supported install locations, or ambiguous remnants.

    Process liveness is never used. Data roots are not proof of installation.
    """
    if kind not in _NAMES:
        return 'unknown'
    cached = _CACHE.get(kind)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    title, command = _NAMES[kind]
    home = Path.home()
    local = Path(os.getenv('LOCALAPPDATA') or home / 'AppData/Local')
    roaming = Path(os.getenv('APPDATA') or home / 'AppData/Roaming')
    program_roots = [local / 'Programs', Path(os.getenv('ProgramFiles') or 'C:/Program Files'),
                     Path(os.getenv('ProgramFiles(x86)') or 'C:/Program Files (x86)')]
    executables = [root / folder / exe for root in program_roots
                   for folder in (title, command)
                   for exe in (title + '.exe', command + '.exe')]
    npm_package = roaming / 'npm/node_modules/opencode-ai/package.json' if kind == 'opencode' else None
    path_hits = [Path(hit) for executable in (command, command + '.exe', command + '.cmd')
                 if (hit := shutil.which(executable))]
    real_executable = any(hit.is_file() and hit.suffix.casefold() == '.exe' and
                          hit.stem.casefold() in {title.casefold(), command.casefold()}
                          for hit in path_hits)
    package_present = bool(npm_package and _npm_binary(npm_package, command))
    registration = _registered(title, command)
    installed = bool(real_executable or package_present or
                     any(path.is_file() for path in executables) or registration == 'present')
    if installed:
        result = 'present'
    else:
        remnants = {'workbuddy': home / '.workbuddy',
                    'opencode': home / '.local/share/opencode',
                    'zcode': home / '.zcode',
                    'kimi': roaming / 'kimi-desktop'}
        result = 'unknown' if remnants[kind].exists() or registration == 'unknown' or path_hits else 'absent'
    _CACHE[kind] = (time.monotonic(), result)
    return result
