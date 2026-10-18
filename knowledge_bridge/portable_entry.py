"""Small machine locator; installation dependencies remain inside .jiedu."""
from __future__ import annotations
import os,shutil
from pathlib import Path


def locator_shell() -> Path:
    preferred=Path(os.environ['LOCALAPPDATA'])/'Microsoft/WindowsApps/pwsh.exe'
    if preferred.is_file():return preferred
    discovered=shutil.which('pwsh.exe')
    if discovered:return Path(discovered)
    return Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'


def locator_configuration(client_id: str, secret: str | None = None) -> dict:
    # The OS launcher is intentional: locating a moved Python must not need Python.
    shell = locator_shell()
    locator = Path(os.environ['LOCALAPPDATA']) / 'Jiedu/jiedu_locator.ps1'
    env = {'KNOWLEDGE_BRIDGE_CLIENT_ID': client_id, 'PYTHONIOENCODING': 'utf-8'}
    if secret is not None:
        env['KNOWLEDGE_BRIDGE_CLIENT_SECRET'] = secret
    return {'command': str(shell), 'args': ['-NoLogo', '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'Bypass', '-File', str(locator), '-Action', 'mcp'], 'env': env}
