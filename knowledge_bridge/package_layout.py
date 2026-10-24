"""Explicit self-contained package layout; no installation-name assumptions."""
from __future__ import annotations
import json
from pathlib import Path


def package_root(program_root: Path | None = None) -> Path | None:
    program = Path(program_root or Path(__file__).resolve().parents[1]).resolve()
    control = program.parent
    marker = control / 'package.json'
    if control.name != '.jiedu' or not marker.is_file():
        return None
    data = json.loads(marker.read_text('utf-8-sig'))
    if data.get('format_version') != 1 or data.get('layout') not in {'jiedu-self-contained-v1','jiedu-application-v1'}:
        raise ValueError('Unsupported Jiedu package layout')
    if data.get('program') != 'program' or (control / 'program').resolve() != program:
        raise ValueError('Package program location mismatch')
    return control


def connected_workspace(control: Path) -> Path | None:
    """A validated application connection may reuse an existing workspace control."""
    reference = control / 'control-reference.json'
    if not reference.exists() and not reference.is_symlink():
        return None
    if reference.is_symlink() or reference.stat().st_size > 4096:
        raise ValueError('Invalid Jiedu workspace connection')
    try:
        data = json.loads(reference.read_text('utf-8-sig'))
        workspace = Path(data['workspace'])
        if (data.get('format') != 'jiedu-workspace-connection-v1'
                or not workspace.is_absolute() or workspace == Path(workspace.anchor)
                or not data.get('workspace_id')):
            raise ValueError('Invalid Jiedu workspace connection')
        for part in (workspace, *workspace.parents):
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise ValueError('Linked Jiedu workspace connection')
        workspace = workspace.resolve(strict=True)
        workspace_control = workspace / '.jiedu'
        local_data = workspace_control / 'control'
        manifest = workspace_control / 'workspace.json'
        if any(path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
               for path in (workspace_control, local_data, manifest)):
            raise ValueError('Linked Jiedu workspace connection')
        if not local_data.is_dir():
            raise ValueError('Jiedu workspace control is missing')
        existing = json.loads(manifest.read_text('utf-8-sig'))
        if existing.get('workspace_id') != data['workspace_id']:
            raise ValueError('Jiedu workspace connection identity mismatch')
        return workspace
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid Jiedu workspace connection') from exc


def component(name: str, program_root: Path | None = None) -> Path | None:
    control = package_root(program_root)
    if control is None:
        return None
    data = json.loads((control / 'package.json').read_text('utf-8-sig'))
    if data.get('layout') == 'jiedu-application-v1' and name == 'control':
        connected = connected_workspace(control)
        if connected is not None:
            return connected / '.jiedu' / 'control'
    if data.get('layout') == 'jiedu-application-v1' and name == 'workspace':
        return None # Installed software follows the saved workspace choice.
    value = Path(data[name])
    if name == 'workspace':
        if str(value) != '..':
            raise ValueError('Package workspace must contain .jiedu')
        return control.parent
    if value.is_absolute() or '..' in value.parts:
        raise ValueError('Package component must use an internal relative path')
    target = (control / value).resolve()
    if control not in target.parents:
        raise ValueError('Package component escapes .jiedu')
    return target


def python_executable(program_root: Path, *, windowless: bool = False) -> Path:
    portable = component('pythonw' if windowless else 'python', program_root)
    if portable is not None:
        return portable
    return Path(program_root) / '.venv312' / 'Scripts' / ('pythonw.exe' if windowless else 'python.exe')


def configure_environment():
    import os
    control = package_root()
    if control is None:
        return
    cache = component('cache')
    for directory in (cache, cache/'tmp'):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {'HF_HOME': component('models'), 'TORCH_HOME': cache/'torch',
                       'TORCHINDUCTOR_CACHE_DIR': cache/'torchinductor',
                       'XDG_CACHE_HOME': cache, 'TEMP': cache/'tmp', 'TMP': cache/'tmp'}.items():
        os.environ[key] = str(value)
