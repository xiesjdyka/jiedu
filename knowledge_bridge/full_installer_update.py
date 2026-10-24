"""Verified full-installer handoff. No process is killed by this module.

The native helper runs outside the installation, waits for the exact owned
processes to exit, and owns install/rollback/restart. A signed channel proves
origin and bytes; it does not claim a Windows Authenticode signature.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from packaging.version import Version

MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
INSTALL_IDENTITY = 'jiedu-consumer-install-v1'
APP_ID = '{6DA20431-B9C2-4C8E-A814-86A059B235D1}_is1'
HANDOFF_FORMAT = 'jiedu-verified-update-handoff-v1'
INVENTORY_FORMAT = 'jiedu-install-files-v1'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def no_links(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('更新路径不能经过符号链接或目录联接')
    return path.resolve()


def tree_inventory(root, *, excluded=()):
    root = no_links(root)
    result = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            no_links(Path(parent) / name)
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative not in excluded:
                result[relative] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
    return result


def verify_installation_inventory(program):
    program = no_links(program)
    target = program.parent.parent
    path = no_links(program.parent / 'install-files.json')
    if not path.is_file():
        raise ValueError('此安装缺少受审文件清单，不能自动完整升级；请手动安装新版并连接原工作区')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('安装文件清单过大或无效')
    record = json.loads(path.read_text('utf-8-sig'))
    if record.get('format') != INVENTORY_FORMAT or not isinstance(record.get('files'), dict):
        raise ValueError('安装文件清单格式无效')
    expected, seen = record['files'], set()
    required = {'界渡.exe', '.jiedu/package.json', 'jiedu-install.txt'}
    if not required <= set(expected):
        raise ValueError('安装文件清单缺少归属文件')
    for name, row in expected.items():
        relative = Path(name)
        if (not name or '\\' in name or ':' in name or relative.is_absolute() or '..' in relative.parts
                or name.casefold() in seen or not isinstance(row, dict)
                or not re.fullmatch('[0-9a-f]{64}', row.get('sha256', ''))
                or type(row.get('bytes')) is not int or row['bytes'] < 0):
            raise ValueError('安装文件清单含不安全或重复路径')
        seen.add(name.casefold())
        member = no_links(target / relative)
        if not member.is_file() or member.stat().st_size != row['bytes'] or digest(member) != row['sha256']:
            raise ValueError('安装程序或运行环境文件已变化，请使用完整安装器修复：' + name)
    private = {'config.json','AGENTS.md','data','knowledge-space'}
    actual = set()
    optional_runtime = []
    runtime = program.parent / 'runtime'
    if runtime.exists():
        for entry in runtime.iterdir():
            no_links(entry)
            if entry.name != 'python':
                # Optional OCR/runtime components are user-installed. The helper
                # moves these exact top-level components and journals rollback.
                tree_inventory(entry) if entry.is_dir() else digest(entry)
                optional_runtime.append(entry.name)
    ignored_caches = 0
    for base in (program, runtime / 'python'):
        for parent, directories, files in os.walk(base, followlinks=False):
            if base == program and Path(parent) == program:
                directories[:] = [name for name in directories if name not in private]
            for name in directories + files:
                no_links(Path(parent) / name)
            for name in files:
                member = Path(parent) / name
                if base == program and member.parent == program and name in private:
                    continue
                relative = member.relative_to(target).as_posix()
                if relative not in expected and member.parent.name == '__pycache__':
                    match = re.fullmatch(r'(.+)\.cpython-(?:312|313)(?:\.opt-[12])?\.pyc', name)
                    source = member.parent.parent / (match.group(1) + '.py') if match else None
                    # Only interpreter-generated cache for a bundled source is
                    # disposable. Other files under __pycache__ still block.
                    if source and source.relative_to(target).as_posix() in expected:
                        with member.open('rb') as stream:
                            header = stream.read(16)
                        if len(header) == 16 and header[:4] in (b'\xcb\x0d\r\n', b'\xf3\x0d\r\n') and int.from_bytes(header[4:8], 'little') in (0, 1, 3):
                            ignored_caches += 1
                            continue
                actual.add(relative)
    inventory_scope = {name for name in expected if name.startswith(('.jiedu/program/','.jiedu/runtime/'))}
    if actual != inventory_scope:
        raise ValueError('程序或运行环境含未登记、缺失或新增文件，自动升级已暂停；请先审阅额外文件，不会静默丢弃')
    return {'sha256': digest(path), 'files': len(expected), 'program_runtime_files': len(actual),
            'generated_caches': ignored_caches, 'preserved_optional_runtime': optional_runtime}


def registrations():
    """Read only this AppId, in all registry views; never create a key."""
    if os.name != 'nt':
        raise ValueError('完整安装器升级仅支持 Windows')
    import winreg
    rows = []
    for label, hive in [('user', winreg.HKEY_CURRENT_USER), ('machine', winreg.HKEY_LOCAL_MACHINE)]:
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID,
                                    0, winreg.KEY_READ | view) as key:
                    location = winreg.QueryValueEx(key, 'InstallLocation')[0]
                    rows.append({'scope': label, 'view': view, 'location': str(location)})
            except FileNotFoundError:
                pass
    return rows


def installation_owner(program, *, registry_reader=None):
    program = no_links(program)
    if program.name != 'program' or program.parent.name != '.jiedu':
        raise ValueError('未识别此程序的完整安装归属，请使用安装器手动升级')
    target = no_links(program.parent.parent)
    if target.parent == target:
        raise ValueError('拒绝在磁盘根目录执行升级')
    try:
        package = json.loads((program.parent / 'package.json').read_text('utf-8-sig'))
        if package.get('layout') == 'jiedu-self-contained-v1':
            raise ValueError('当前为自包含工作区，完整升级请先单独安装程序后连接原工作区，资料无需搬移')
        marker = (target / 'jiedu-install.txt').read_text('utf-8-sig').strip()
    except (OSError, ValueError):
        if 'package' in locals() and package.get('layout') == 'jiedu-self-contained-v1':
            raise ValueError('当前为自包含工作区，完整升级请先单独安装程序后连接原工作区，资料无需搬移') from None
        raise ValueError('安装标记缺失或无效，请使用安装器手动升级') from None
    if (marker != INSTALL_IDENTITY or package.get('format_version') != 1
            or package.get('layout') != 'jiedu-application-v1' or package.get('program') != 'program'):
        if package.get('layout') == 'jiedu-self-contained-v1':
            raise ValueError('当前为自包含工作区，完整升级请先单独安装程序后连接原工作区，资料无需搬移')
        raise ValueError('安装标记不匹配，请使用安装器手动升级')
    allowed = {'.jiedu', '界渡.exe', 'jiedu-install.txt', 'ThirdPartyNotices.zip', 'jiedu-v1.ico'}
    uninstall_files = {'unins000.exe', 'unins000.dat', 'unins000.msg'}
    for entry in target.iterdir():
        if entry.name not in allowed and (entry.name not in uninstall_files or not entry.is_file()):
            raise ValueError('安装目录含额外文件或资料；请先将程序独立安装，再连接原工作区，资料不会自动搬移')
        no_links(entry)
    rows = (registry_reader or registrations)()
    if not rows or any(row.get('scope') != 'user' or no_links(row['location']) != target for row in rows):
        raise ValueError('安装注册归属不一致或需要管理员权限，请使用安装器手动升级')
    shell = no_links(target / '界渡.exe')
    if not shell.is_file():
        raise ValueError('原生桌面程序缺失，请使用完整安装器修复')
    return {'target': str(target), 'program': str(program), 'native_exe': str(shell),
            'app_id': APP_ID, 'install_identity': INSTALL_IDENTITY, 'scope': 'user'}


def check_workspace_boundary(target, vault, *, selected=False):
    target, vault = no_links(target), no_links(vault)
    # The unconfigured, absent fallback is not a selected workspace. Existing
    # paths inside the installation cannot safely survive a directory swap.
    if vault.is_relative_to(target) and (selected or vault.exists()):
        raise ValueError('工作区位于安装目录内，完整升级已暂停；请先独立安装程序并连接原工作区，资料不会自动搬移')


def retention_plan(target):
    """Only old successful, fully attributed transactions may be retired.

    Failed or unresolved material blocks another full update; it is never
    silently deleted. Retirement happens only after the next healthy update,
    so one complete previous installation always remains.
    """
    target = no_links(target)
    result = []
    for work in target.parent.glob('.jiedu-update-*'):
        no_links(work)
        file = work / 'job.json'
        if not file.is_file():
            continue
        try:
            job = json.loads(file.read_text('utf-8'))
        except (OSError, ValueError):
            continue
        if job.get('target') != str(target):
            continue
        token = job.get('token', '')
        if (not re.fullmatch('[0-9a-f]{32}', token) or work.name != '.jiedu-update-' + token
                or job.get('format') != HANDOFF_FORMAT
                or (work / 'helper-identity.txt').read_text('ascii').strip() != HANDOFF_FORMAT):
            raise ValueError('旧更新材料归属无法核验；请先审阅恢复材料，未删除任何内容')
        backup = no_links(target.with_name(target.name + '.previous-' + token))
        failed = no_links(target.with_name(target.name + '.failed-' + token))
        try:
            state = json.loads((work / 'result.json').read_text('utf-8')).get('state')
        except (OSError, ValueError):
            state = None
        if state != 'complete':
            raise ValueError('存在未完成或失败的更新恢复材料；请先恢复或审阅，避免反复下载和累积完整副本')
        if failed.exists():
            raise ValueError('旧更新含失败副本，请先审阅恢复材料；不会自动删除')
        if backup.exists():
            from .release_version import version_info
            if ((backup / 'jiedu-install.txt').read_text('utf-8-sig').strip() != INSTALL_IDENTITY
                    or digest(backup / '界渡.exe') != job.get('old_native_sha256')
                    or version_info(backup / '.jiedu/program')['product_version'] != job.get('old_version')):
                raise ValueError('上一回退副本已变化；请先审阅，未删除任何内容')
            baseline = work / 'backup-files.json'
            outcome = json.loads((work / 'result.json').read_text('utf-8'))
            if not baseline.is_file() or digest(baseline) != outcome.get('backup_inventory_sha256'):
                raise ValueError('旧回退目录缺少完成时精确清单，自动清理已暂停')
            if tree_inventory(backup) != json.loads(baseline.read_text('utf-8-sig')).get('files'):
                raise ValueError('回退副本后来新增或修改了文件，自动升级及清理已暂停，资料未删除')
        helper_baseline = work / 'helper-files.json'
        outcome = json.loads((work / 'result.json').read_text('utf-8'))
        if not helper_baseline.is_file() or digest(helper_baseline) != outcome.get('helper_inventory_sha256'):
            raise ValueError('旧更新助手材料后来新增或变化，自动清理已暂停')
        expected_helper_files = json.loads(helper_baseline.read_text('utf-8-sig')).get('files')
        excluded = {'result.json','helper-files.json'}
        if isinstance(expected_helper_files, dict) and 'helper.log' not in expected_helper_files:
            excluded.add('helper.log')
        if tree_inventory(work, excluded=excluded) != expected_helper_files:
            raise ValueError('旧更新助手材料后来新增或变化，自动清理已暂停')
        result.append({'token': token, 'work': str(work), 'backup': str(backup),
                       'target': str(target), 'old_native_sha256': job.get('old_native_sha256')})
    attributed = {row['backup'] for row in result}
    for backup in target.parent.glob(target.name + '.previous-*'):
        if str(no_links(backup)) not in attributed:
            raise ValueError('发现未归属的回退目录；请先审阅，未删除任何内容')
    return result


def inspect_installer(path, record, *, version_reader=None):
    """Read PE metadata, never execute the downloaded installer."""
    path = no_links(path)
    if (record.get('kind') != 'complete-installer' or type(record.get('size')) is not int
            or not 0 < record['size'] <= MAX_INSTALLER_BYTES):
        raise ValueError('完整安装器发布信息无效')
    expected_name = 'Jiedu-' + record['version'] + '-windows-x64-setup.exe'
    if record.get('installer_name') != expected_name or not re.fullmatch(r'Jiedu-[0-9A-Za-z.+-]+-windows-x64-setup\.exe', expected_name):
        raise ValueError('完整安装器文件名与版本不一致')
    if path.stat().st_size != record['size'] or digest(path) != record['sha256']:
        raise ValueError('完整安装器大小或 SHA256 校验失败')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('完整安装器不是 Windows 可执行文件')
    if version_reader is None:
        from desktop.update_release import pe_product_version
        version_reader = pe_product_version
    if Version(version_reader(path)) != Version(record['version']):
        raise ValueError('完整安装器产品版本与签名发布信息不一致')
    return {'kind': 'complete-installer', 'sha256': record['sha256'], 'bytes': record['size'],
            'version': record['version'], 'authenticode_required': False,
            'windows_signature': 'not asserted; signed channel and SHA256 verified'}


def prepare_handoff(program, root, record, artifact, *, registry_reader=None):
    """Persist an inert request; only an explicit native callback may consume it."""
    root = no_links(root)
    owner = installation_owner(program, registry_reader=registry_reader)
    artifact = no_links(artifact)
    if not artifact.is_relative_to(root) or artifact.stat().st_size != record['size'] or digest(artifact) != record['sha256']:
        raise ValueError('已核验的更新文件发生变化，请重新下载')
    token = uuid.uuid4().hex
    request = {'format': HANDOFF_FORMAT, 'token': token, 'kind': record.get('kind', 'program-only'),
               **owner, 'artifact': str(artifact), 'sha256': record['sha256'],
               'bytes': record['size'], 'version': record['version'],
               'old_version': __import__('knowledge_bridge.online_update', fromlist=['version']).version(program),
               'old_native_sha256': digest(owner['native_exe']),
               'channel_envelope': str(root / 'channel-envelope.json'),
               'installer_name': record.get('installer_name'),
               'phase': 'verified_waiting_for_owned_process_exit', 'force_stop_allowed': False,
               'rollback_required': True, 'authenticode_asserted': False}
    root.mkdir(parents=True, exist_ok=True)
    path = root / ('handoff-' + token + '.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(request, stream, ensure_ascii=False, indent=2)
    return {**request, 'request_path': str(path)}


def validate_handoff(path, program, *, registry_reader=None, version_reader=None):
    """Recheck signed origin and installation before a helper moves anything."""
    path = no_links(path)
    request = json.loads(path.read_text('utf-8'))
    if request.get('format') != HANDOFF_FORMAT or not re.fullmatch('[0-9a-f]{32}', request.get('token', '')):
        raise ValueError('更新交接请求无效')
    owner = installation_owner(program, registry_reader=registry_reader)
    if any(request.get(key) != value for key, value in owner.items()):
        raise ValueError('更新交接的安装归属发生变化')
    from .online_update import OnlineUpdate, version
    updater = OnlineUpdate(program, path.parent.parent)
    record = updater.verify((path.parent / 'channel-envelope.json').read_bytes())
    artifact = no_links(request['artifact'])
    if (artifact.parent != path.parent or record['version'] != request['version']
            or record['sha256'] != request['sha256'] or record['size'] != request['bytes']
            or record.get('kind', 'program-only') != request['kind']
            or Version(record['version']) <= Version(version(program))):
        raise ValueError('更新交接与签名发布信息不一致')
    if request['kind'] == 'complete-installer':
        verify_installation_inventory(program)
        inspect_installer(artifact, record, version_reader=version_reader)
    else:
        from .desktop_update import inspect_package
        manifest = inspect_package(artifact, program)
        if Version(manifest['version']) != Version(record['version']):
            raise ValueError('更新包版本与交接不一致')
        if digest(artifact) != request['sha256'] or artifact.stat().st_size != request['bytes']:
            raise ValueError('更新包交接哈希不一致')
    return request


def _powershell(target):
    candidates = [str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WindowsApps/pwsh.exe'),
                  shutil.which('pwsh'), str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                  'System32/WindowsPowerShell/v1.0/powershell.exe')]
    for value in candidates:
        if value and Path(value).is_file() and not Path(value).resolve().is_relative_to(target):
            return value
    raise ValueError('未找到独立的 Windows 更新助手宿主，未关闭界渡')


def start_helper(request):
    """Start a fixed external helper before requesting cooperative service exit."""
    validate_handoff(request['request_path'], request['program'])
    from .bridge_launcher import Launcher
    launcher = Launcher(Path(request['program']))
    check_workspace_boundary(request['target'], launcher.vault,
                             selected=launcher.config_path.exists() or (Path(request['program']) / 'config.json').exists())
    cleanup = retention_plan(request['target']) if request['kind'] == 'complete-installer' else []
    if request['kind'] == 'complete-installer':
        verify_installation_inventory(request['program'])
    proof = launcher.snapshot_update_processes()
    if not proof.get('launch_id') or not proof.get('processes'):
        raise ValueError('无法核验当前服务身份，未关闭或安装任何程序')
    target = no_links(request['target'])
    work = no_links(target.parent / ('.jiedu-update-' + request['token']))
    if work.exists():
        raise ValueError('更新助手目录已存在，请重新开始更新')
    host = _powershell(target)
    work.mkdir()
    (work / 'helper-identity.txt').write_text(HANDOFF_FORMAT, 'ascii')
    script = work / 'update-helper.ps1'
    # BOM makes the same fixed UTF-8 script readable by Windows PowerShell 5.1
    # when PowerShell 7 is unavailable. It runs outside the runtime being replaced.
    script.write_text(HELPER_SCRIPT, 'utf-8-sig')
    required_acks = [dict(role=role, pid=receipt['pid']) for role in ('console','watch')
                     if (receipt := launcher._receipt(launcher.load(), role)) and launcher._owned(receipt)]
    job = {**request, 'proof': proof, 'work': str(work), 'cleanup': cleanup, 'helper_host': host,
           'required_acks': required_acks,
           'backup': str(target.with_name(target.name + '.previous-' + request['token'])),
           'failed': str(target.with_name(target.name + '.failed-' + request['token'])),
           'local_data': str(launcher.local_data), 'python': str(launcher.python)}
    job_path = work / 'job.json'
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), 'utf-8')
    state = Path(request['request_path']).parent
    log = (work / 'helper.log').open('ab')
    try:
        process = subprocess.Popen([host, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                    '-File', str(script), '-JobFile', str(job_path)],
                                   cwd=work, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
    finally:
        log.close()
    (state / 'helper.json').write_text(json.dumps({'token': request['token'], 'work': str(work), 'helper_pid': process.pid}, ensure_ascii=False), 'utf-8')
    try:
        launcher.request_update_stop(request['token'], proof['launch_id'])
    except Exception:
        # The helper requires the matching marker and times out without mutation.
        (work / 'abort.json').write_text('{"abort":true}', 'ascii')
        raise
    return {'helper_pid': process.pid, 'work': str(work), 'token': request['token']}


def start_recovery(program, state):
    """Resume a retained full-install rollback after another idle handshake."""
    program, state = no_links(program), no_links(state)
    owner = installation_owner(program)
    pointer = json.loads((state / 'helper.json').read_text('utf-8'))
    token = pointer.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('恢复事务标识无效')
    work = no_links(pointer['work'])
    if work != Path(owner['target']).parent / ('.jiedu-update-' + token):
        raise ValueError('恢复事务目录不匹配')
    job_path = work / 'job.json'
    job = json.loads(job_path.read_text('utf-8'))
    outcome = json.loads((work / 'result.json').read_text('utf-8'))
    backup = no_links(Path(owner['target']).with_name(Path(owner['target']).name + '.previous-' + token))
    from .release_version import version_info
    journal = json.loads((work / 'transaction.json').read_text('utf-8-sig'))
    previous = backup if backup.exists() else Path(owner['target'])
    if (job.get('format') != HANDOFF_FORMAT or job.get('token') != token
            or job.get('target') != owner['target'] or job.get('backup') != str(backup)
            or job.get('kind') != 'complete-installer' or outcome.get('state') != 'recovery_required'
            or (work / 'helper-identity.txt').read_text('ascii').strip() != HANDOFF_FORMAT
            or not journal.get('moved') or not isinstance(journal.get('privateMoves'), list)
            or (previous / 'jiedu-install.txt').read_text('utf-8-sig').strip() != INSTALL_IDENTITY
            or digest(previous / '界渡.exe') != job.get('old_native_sha256')
            or version_info(previous / '.jiedu/program')['product_version'] != job.get('old_version')):
        raise ValueError('可恢复副本的归属或版本不匹配，未移动任何资料')
    from .bridge_launcher import Launcher
    launcher = Launcher(program)
    check_workspace_boundary(owner['target'], launcher.vault, selected=launcher.config_path.exists())
    proof = launcher.snapshot_update_processes()
    required_acks = [dict(role=role, pid=receipt['pid']) for role in ('console','watch')
                     if (receipt := launcher._receipt(launcher.load(), role)) and launcher._owned(receipt)]
    job.update(proof=proof, python=str(launcher.python), local_data=str(launcher.local_data), required_acks=required_acks)
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), 'utf-8')
    script = work / 'update-helper.ps1'
    script.write_text(HELPER_SCRIPT, 'utf-8-sig')
    (work / 'abort.json').unlink(missing_ok=True)
    _atomic_report(work / 'result.json', {'state':'waiting_exit','phase':'waiting_exit','token':token,
                   'message':'正在等待当前版本安全退出后恢复。'})
    try:
        with (work / 'helper.log').open('ab') as log:
            process = subprocess.Popen([_powershell(Path(owner['target'])), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script), '-JobFile', str(job_path), '-Recover'],
                cwd=work, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=0x08000000 if os.name == 'nt' else 0)
    except Exception:
        _atomic_report(work / 'result.json', outcome)
        raise
    try:
        launcher.request_update_stop(token, proof['launch_id'])
    except Exception:
        (work / 'abort.json').write_text('{"abort":true}', 'ascii')
        raise
    return {'helper_pid': process.pid, 'work': str(work), 'token': token}


def _atomic_report(path, record):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(tmp, path)


HELPER_SCRIPT = r'''param([Parameter(Mandatory=$true)][string]$JobFile,[switch]$Recover)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$j=Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
$work=[IO.Path]::GetFullPath($j.work)
$target=[IO.Path]::GetFullPath($j.target).TrimEnd('\')
$backup=[IO.Path]::GetFullPath($j.backup)
$failed=[IO.Path]::GetFullPath($j.failed)
$moved=$false
$privateMoves=@()
$registry=@()
$restoreStage='not_started'
function SaveJournal {
  @{privateMoves=$privateMoves;moved=$moved;restore_stage=$restoreStage}|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $work 'transaction.json') -Encoding UTF8
}
function SafePath([string]$p) {
  $item=[IO.Path]::GetFullPath($p)
  while($item) {
    if(Test-Path -LiteralPath $item) {
      if(((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {throw 'Reparse paths are forbidden'}
    }
    $parent=[IO.Path]::GetDirectoryName($item)
    if($parent -eq $item){break};$item=$parent
  }
}
function Report([string]$state,[string]$message) {
  $r=@{state=$state;phase=$state;message=$message;token=$j.token;version=$j.version;backup=$backup;failed=$failed;force_stop_used=$false;helper_host=$j.helper_host;helper_pid=$PID}
  foreach($entry in @(@{file='backup-files.json';field='backup_inventory_sha256'},@{file='helper-files.json';field='helper_inventory_sha256'})) { $file=Join-Path $work $entry.file;if(Test-Path -LiteralPath $file){$r[$entry.field]=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()} }
  $json=$r|ConvertTo-Json -Depth 8
  $path=Join-Path $work 'result.json';$temporary=$path+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
  [IO.File]::WriteAllText($temporary,$json,[Text.UTF8Encoding]::new($false))
  for($attempt=0;$attempt -lt 5;$attempt++) {
    try{if(Test-Path -LiteralPath $path){[IO.File]::Replace($temporary,$path,[NullString]::Value)}else{[IO.File]::Move($temporary,$path)};break}
    catch{if($attempt -eq 4){throw};Start-Sleep -Milliseconds 20}
  }
}
function TreeInventory([string]$root,[string[]]$exclude=@()) {
  SafePath $root;$rows=@{};$prefix=$root.TrimEnd('\')+'\'
  foreach($entry in Get-ChildItem -LiteralPath $root -Recurse -Force) {
    if(($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Linked tree retained'}
    if(-not $entry.PSIsContainer) {
      $relative=$entry.FullName.Substring($prefix.Length).Replace('\','/')
      if($relative -notin $exclude){$rows[$relative]=@{bytes=$entry.Length;sha256=(Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}
    }
  }
  return $rows
}
function WriteTree([string]$root,[string]$path,[string[]]$exclude=@()) {
  @{format='jiedu-update-tree-v1';files=(TreeInventory $root $exclude)}|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $path -Encoding UTF8
}
function VerifyTree([string]$root,[string]$path,[string]$expectedHash,[string[]]$exclude=@()) {
  if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash){throw 'Recovery inventory changed'}
  $expected=Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json
  if($expected.format -ne 'jiedu-update-tree-v1'){throw 'Recovery inventory format mismatch'}
  $actual=TreeInventory $root $exclude;$properties=@($expected.files.PSObject.Properties)
  if($actual.Count -ne $properties.Count){throw 'Later files found; recovery material retained'}
  foreach($property in $properties){if(-not $actual.ContainsKey($property.Name) -or $actual[$property.Name].bytes -ne $property.Value.bytes -or $actual[$property.Name].sha256 -ne $property.Value.sha256){throw 'Recovery file changed; retained'}}
}
function NormalAcks {
  foreach($expected in $j.required_acks) {
    if($expected.role -notin @('console','watch')){throw 'Invalid normal-exit role'}
    $path=Join-Path $j.local_data ('runtime\update-stop-'+$j.token+'-'+$expected.role+'.json')
    SafePath $path
    $ack=Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json
    if($ack.token -ne $j.token -or $ack.launch_id -ne $j.proof.launch_id -or $ack.role -ne $expected.role -or $ack.pid -ne $expected.pid -or $ack.complete -ne $true){throw 'A service did not acknowledge a successful normal exit; no installation files were moved'}
  }
}
function TargetProcesses {
  $prefix=$target+'\'
  @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.ExecutablePath -and ($_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) })
}
function WaitEmpty([int]$seconds) {
  $until=[DateTime]::UtcNow.AddSeconds($seconds)
  while([DateTime]::UtcNow -lt $until) {
    if(Test-Path -LiteralPath (Join-Path $work 'abort.json')){throw 'Update cancelled before handoff'}
    $alive=$false
    foreach($p in $j.proof.processes) {
      $process=Get-Process -Id $p.pid -ErrorAction SilentlyContinue
      if($process) {
        if($process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne [int64]$p.created){throw 'Process identity changed; no process was stopped'}
        $alive=$true
      }
    }
    if(-not $alive -and @(TargetProcesses).Count -eq 0){return}
    Start-Sleep -Milliseconds 500
  }
  throw 'Programs are still using the installation. Close them and retry; no force stop was used.'
}
function ClearMarker {
  $marker=Join-Path $j.local_data 'runtime\update-stop.json'
  if(Test-Path -LiteralPath $marker){$s=Get-Content -LiteralPath $marker -Raw -Encoding UTF8|ConvertFrom-Json;if($s.token -eq $j.token){Remove-Item -LiteralPath $marker}}
}
function SnapshotRegistry {
  $list=@()
  foreach($view in @([Microsoft.Win32.RegistryView]::Registry32,[Microsoft.Win32.RegistryView]::Registry64)) {
    $h=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$view)
    try {
      $key=$h.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$j.app_id)
      if($key){try{$values=@();foreach($n in $key.GetValueNames()){$values+=@{name=$n;value=$key.GetValue($n,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames);kind=[int]$key.GetValueKind($n)}};$list+=@{view=[int]$view;exists=$true;values=$values}}finally{$key.Dispose()}}
      else{$list+=@{view=[int]$view;exists=$false;values=@()}}
    }finally{$h.Dispose()}
  }
  return $list
}
function RestoreRegistry {
  foreach($record in $registry) {
    $h=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,[Microsoft.Win32.RegistryView]$record.view)
    try {
      if(-not $record.exists) {
        $existing=$h.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$j.app_id)
        if($existing){try{if($existing.GetValue('InstallLocation').TrimEnd('\') -ne $target){throw 'Registry ownership changed; retained'}}finally{$existing.Dispose()};$h.DeleteSubKeyTree('Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$j.app_id,$false)}
        continue
      }
      $key=$h.CreateSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$j.app_id)
      try {foreach($n in $key.GetValueNames()){$key.DeleteValue($n,$false)};foreach($v in $record.values){$value=$v.value;switch([int]$v.kind){3{$value=[byte[]]$v.value};7{$value=[string[]]$v.value};4{$value=[int]$v.value};11{$value=[long]$v.value}};$key.SetValue($v.name,$value,[Microsoft.Win32.RegistryValueKind]$v.kind)}}finally{$key.Dispose()}
    }finally{$h.Dispose()}
  }
}
function Preserve([string]$old,[string]$new) {
  if(Test-Path -LiteralPath $old) {
    if(Test-Path -LiteralPath $new){throw 'A private-data destination unexpectedly exists; recovery is retained'}
    SafePath $old;SafePath $new
    $parent=Split-Path -Parent $new;[IO.Directory]::CreateDirectory($parent)|Out-Null
    $script:privateMoves+=@{old=$old;new=$new}
    SaveJournal
    Move-Item -LiteralPath $old -Destination $new
  }
}
function RestorePrevious {
  if(@(TargetProcesses).Count -ne 0){throw 'New processes are still running; automatic recovery will not force stop them'}
  $alreadyRestored=$false
  if(-not (Test-Path -LiteralPath $backup)) {
    if((Test-Path -LiteralPath (Join-Path $target '界渡.exe')) -and (Get-FileHash -LiteralPath (Join-Path $target '界渡.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -eq $j.old_native_sha256 -and (Get-Content -LiteralPath (Join-Path $target '.jiedu\program\VERSION') -Raw).Trim() -eq $j.old_version){$alreadyRestored=$true}else{throw 'Previous files unavailable; recovery retained'}
  }
  if(-not $alreadyRestored) {
   for($i=$privateMoves.Count-1;$i -ge 0;$i--) {
    $m=$privateMoves[$i];$old=[IO.Path]::GetFullPath($m.old);$new=[IO.Path]::GetFullPath($m.new)
    if(-not $old.StartsWith($backup+'\',[StringComparison]::OrdinalIgnoreCase) -or $new -ne ($target+$old.Substring($backup.Length))){throw 'Invalid private-data recovery path'}
    SafePath $old;SafePath $new
    if(Test-Path -LiteralPath $new){if(Test-Path -LiteralPath $old){throw 'Recovery destination exists; materials retained'};Move-Item -LiteralPath $new -Destination $old}
   }
   if(Test-Path -LiteralPath $target){if(Test-Path -LiteralPath $failed){throw 'Failed destination already exists; retained'};Move-Item -LiteralPath $target -Destination $failed}
   Move-Item -LiteralPath $backup -Destination $target
  }
  $script:restoreStage='files_restored';SaveJournal
  RestoreRegistry
  ClearMarker
  Start-Process -FilePath (Join-Path $target '界渡.exe') -WorkingDirectory $target -WindowStyle Hidden|Out-Null
  $script:restoreStage='complete';SaveJournal
}
function RetireSuccessful {
  if(-not (Test-Path -LiteralPath $backup) -or (Get-Content -LiteralPath (Join-Path $backup 'jiedu-install.txt') -Raw).Trim() -ne $j.install_identity -or (Get-FileHash -LiteralPath (Join-Path $backup '界渡.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $j.old_native_sha256){throw 'Newest rollback is unavailable; older material retained'}
  foreach($old in $j.cleanup) {
    if($old.target -ne $target -or $old.token -notmatch '^[0-9a-f]{32}$' -or $old.token -eq $j.token){throw 'Invalid retention identity'}
    $oldBackup=$target+'.previous-'+$old.token
    $oldWork=Join-Path (Split-Path -Parent $target) ('.jiedu-update-'+$old.token)
    if($old.backup -ne $oldBackup -or $old.work -ne $oldWork){throw 'Invalid retention paths'}
    $oldJob=Get-Content -LiteralPath (Join-Path $oldWork 'job.json') -Raw -Encoding UTF8|ConvertFrom-Json
    $oldResult=Get-Content -LiteralPath (Join-Path $oldWork 'result.json') -Raw -Encoding UTF8|ConvertFrom-Json
    if($oldJob.target -ne $target -or $oldJob.token -ne $old.token -or $oldResult.state -ne 'complete'){throw 'Old transaction is not healthy and attributable'}
    $oldHelperFiles=(Get-Content -LiteralPath (Join-Path $oldWork 'helper-files.json') -Raw -Encoding UTF8|ConvertFrom-Json).files
    $oldExcluded=@('result.json','helper-files.json')
    if($null -eq $oldHelperFiles.PSObject.Properties['helper.log']){$oldExcluded+=@('helper.log')}
    VerifyTree $oldWork (Join-Path $oldWork 'helper-files.json') $oldResult.helper_inventory_sha256 $oldExcluded
    foreach($folder in @($oldBackup,$oldWork)) {
      if(Test-Path -LiteralPath $folder) {
        SafePath $folder
        if(@(Get-ChildItem -LiteralPath $folder -Recurse -Force|Where-Object {($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0}).Count -ne 0){throw 'Linked recovery tree retained'}
        if(@(Get-CimInstance Win32_Process|Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($folder+'\',[StringComparison]::OrdinalIgnoreCase)}).Count -ne 0){throw 'Old recovery tree is still in use'}
        if($folder -eq $oldBackup) {
          VerifyTree $oldBackup (Join-Path $oldWork 'backup-files.json') $oldResult.backup_inventory_sha256
          if((Get-Content -LiteralPath (Join-Path $folder 'jiedu-install.txt') -Raw).Trim() -ne $j.install_identity -or (Get-FileHash -LiteralPath (Join-Path $folder '界渡.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $old.old_native_sha256){throw 'Old backup changed; retained'}
        } elseif((Get-Content -LiteralPath (Join-Path $folder 'helper-identity.txt') -Raw).Trim() -ne $j.format){throw 'Old helper identity changed'}
        Remove-Item -LiteralPath $folder -Recurse -Force
      }
    }
  }
}
try {
  if($j.format -ne 'jiedu-verified-update-handoff-v1' -or $j.token -notmatch '^[0-9a-f]{32}$'){throw 'Invalid handoff'}
  if($work -ne (Join-Path (Split-Path -Parent $target) ('.jiedu-update-'+$j.token)) -or $backup -ne ($target+'.previous-'+$j.token) -or $failed -ne ($target+'.failed-'+$j.token)){throw 'Invalid transaction paths'}
  foreach($p in @($work,$target,$backup,$failed,$j.artifact,$j.python,$j.request_path)){SafePath $p}
  if(-not $Recover -and (Test-Path -LiteralPath $backup)){throw 'Backup already exists'}
  if(-not $Recover -and (Test-Path -LiteralPath $failed)){throw 'Recovery directory already exists'}
  $marker=Join-Path $j.local_data 'runtime\update-stop.json'
  $until=[DateTime]::UtcNow.AddSeconds(15)
  $accepted=$false
  while([DateTime]::UtcNow -lt $until) {
    if(Test-Path -LiteralPath (Join-Path $work 'abort.json')){throw 'Handoff aborted'}
    if(Test-Path -LiteralPath $marker){$s=Get-Content -LiteralPath $marker -Raw -Encoding UTF8|ConvertFrom-Json;if($s.token -eq $j.token -and $s.launch_id -eq $j.proof.launch_id){$accepted=$true;break}}
    Start-Sleep -Milliseconds 100
  }
  if(-not $accepted){throw 'No matching cooperative shutdown request'}
  Report 'waiting_exit' 'Waiting for owned processes to exit normally.'
  WaitEmpty 600
  NormalAcks
  if($Recover) {
    $t=Get-Content -LiteralPath (Join-Path $work 'transaction.json') -Raw -Encoding UTF8|ConvertFrom-Json
    $privateMoves=@($t.privateMoves)
    $restoreStage=$t.restore_stage
    $registry=@(Get-Content -LiteralPath (Join-Path $work 'registry-before.json') -Raw -Encoding UTF8|ConvertFrom-Json)
    $moved=$true
    RestorePrevious
    Report 'rolled_back' 'Previous installation restored after cooperative exit; failed materials retained.'
    exit 0
  }
  # This independent verifier exits before the runtime directory is moved.
  Push-Location -LiteralPath $j.program
  try {& $j.python -B -m knowledge_bridge.full_installer_update check-handoff --request $j.request_path --program $j.program;if($LASTEXITCODE -ne 0){throw 'Final signature/ownership verification failed'}}finally{Pop-Location}
  if(@(TargetProcesses).Count -ne 0){throw 'A program reopened during final verification'}
  if((Get-FileHash -LiteralPath $j.artifact -Algorithm SHA256).Hash.ToLowerInvariant() -ne $j.sha256 -or (Get-Item -LiteralPath $j.artifact).Length -ne $j.bytes){throw 'Artifact bytes changed'}
  if($j.kind -eq 'program-only') {
    Report 'applying' 'Applying verified program update.'
    Push-Location -LiteralPath $j.program
    try {& $j.python -B -m knowledge_bridge.desktop_update apply $j.artifact --handoff $j.request_path;if($LASTEXITCODE -ne 0){throw 'Program update failed; transaction journal retains recovery'}}finally{Pop-Location}
    Start-Process -FilePath (Join-Path $target '界渡.exe') -WorkingDirectory $target -WindowStyle Hidden|Out-Null
    Report 'complete' 'Program update completed and service health verified.'
    exit 0
  }
  if($j.kind -ne 'complete-installer'){throw 'Unsupported update kind'}
  $registry=SnapshotRegistry
  $registry|ConvertTo-Json -Depth 12|Set-Content -LiteralPath (Join-Path $work 'registry-before.json') -Encoding UTF8
  # Move only the verified installer outside the tree. No runtime copy is made.
  $setup=Join-Path $work $j.installer_name
  Move-Item -LiteralPath $j.artifact -Destination $setup
  if((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $j.sha256){throw 'Moved artifact verification failed'}
  Report 'applying' 'Installing full update; previous installation is retained for recovery.'
  Move-Item -LiteralPath $target -Destination $backup
  $moved=$true
  SaveJournal
  $arguments=@('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',('/DIR="'+$target+'"'),('/LOG="'+(Join-Path $work 'installer.log')+'"'))
  $p=Start-Process -FilePath $setup -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
  if($p.ExitCode -ne 0){throw ('Installer exit code '+$p.ExitCode)}
  if((Get-Content -LiteralPath (Join-Path $target 'jiedu-install.txt') -Raw).Trim() -ne $j.install_identity){throw 'New installation identity mismatch'}
  # Every unbundled .jiedu component is retained, including saved workspace,
  # credentials/control, models/cache and local history. No knowledge is copied.
  $oldControl=Join-Path $backup '.jiedu';$newControl=Join-Path $target '.jiedu'
  foreach($entry in Get-ChildItem -LiteralPath $oldControl -Force) {
    if($entry.Name -notin @('program','runtime','package.json','install-files.json')){Preserve $entry.FullName (Join-Path $newControl $entry.Name)}
  }
  foreach($name in @('config.json','data','knowledge-space','AGENTS.md')) {Preserve (Join-Path $backup ('.jiedu\program\'+$name)) (Join-Path $target ('.jiedu\program\'+$name))}
  $oldRuntime=Join-Path $backup '.jiedu\runtime';$newRuntime=Join-Path $target '.jiedu\runtime'
  if(Test-Path -LiteralPath $oldRuntime) {
    foreach($entry in Get-ChildItem -LiteralPath $oldRuntime -Force) {
      if($entry.Name -ne 'python'){Preserve $entry.FullName (Join-Path $newRuntime $entry.Name)}
    }
  }
  $newPython=Join-Path $target '.jiedu\runtime\python\python.exe'
  Push-Location -LiteralPath (Join-Path $target '.jiedu\program')
  try {$v=& $newPython -B -m knowledge_bridge.release_version;if($LASTEXITCODE -ne 0){throw 'New product version cannot be verified'};$version=$v|ConvertFrom-Json;if($version.product_version -ne $j.version){throw 'Installed product version mismatch'}}finally{Pop-Location}
  ClearMarker
  Start-Process -FilePath (Join-Path $target '界渡.exe') -WorkingDirectory $target -WindowStyle Hidden|Out-Null
  Report 'installed' 'Installation completed. Previous installation retained; waiting for service health.'
  $until=[DateTime]::UtcNow.AddSeconds(120);$healthy=$false
  while([DateTime]::UtcNow -lt $until) {
    Push-Location -LiteralPath (Join-Path $target '.jiedu\program')
    try {$raw=& $newPython -B -m knowledge_bridge.bridge_launcher status;if($LASTEXITCODE -eq 0){try{$status=$raw|ConvertFrom-Json;if($status.health_owned){$healthy=$true;break}}catch{}}}finally{Pop-Location}
    Start-Sleep -Seconds 1
  }
  if(-not $healthy){throw 'New service health not confirmed. Close the new application before recovery; previous files are retained.'}
  WriteTree $backup (Join-Path $work 'backup-files.json')
  Report 'finalizing' 'Service health verified; keeping the newest rollback and retiring attributable older successful copies.'
  try {
    RetireSuccessful
    if(Test-Path -LiteralPath $setup){if((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -eq $j.sha256){Remove-Item -LiteralPath $setup}}
    # The running helper owns its stdout/stderr log on Windows; it is mutable
    # diagnostic output and cannot be part of an immutable tree inventory.
    WriteTree $work (Join-Path $work 'helper-files.json') @('result.json','helper-files.json','helper.log')
    Report 'complete' 'Update completed and service health verified. One previous installation retained.'
  } catch {
    WriteTree $work (Join-Path $work 'helper-files.json') @('result.json','helper-files.json','helper.log')
    Report 'complete' ('Update healthy; older recovery cleanup was deferred: '+$_.Exception.Message)
  }
} catch {
  $message=$_.Exception.Message
  if($moved) {
    try {
      RestorePrevious
      Report 'rolled_back' ('Update failed; previous installation restored. '+$message)
    } catch {Report 'recovery_required' ($message+' Recovery deferred: '+$_.Exception.Message)}
  } else {
    try{ClearMarker}catch{}
    if($Recover){Report 'recovery_required' ('Recovery not completed; previous materials retained. '+$message)}else{Report 'failed' ('No installation replacement completed. '+$message)}
    try{if(@(TargetProcesses).Count -eq 0){Start-Process -FilePath (Join-Path $target '界渡.exe') -WorkingDirectory $target -WindowStyle Hidden|Out-Null}}catch{}
  }
  exit 1
}
'''


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check-handoff', 'verify-installation'])
    parser.add_argument('--request')
    parser.add_argument('--program', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'check-handoff':
            if not args.request:
                raise ValueError('缺少更新交接请求')
            validate_handoff(args.request, args.program)
            print('Signed update handoff verified; no files changed.')
        else:
            program = no_links(args.program)
            report = verify_installation_inventory(program)
            bundled = program / 'resources'
            if not ((bundled / 'models/bge-m3/pytorch_model.bin').is_file()
                    and (bundled / 'ocr/python/python.exe').is_file()
                    and all((bundled / 'ocr/models' / name).is_file() for name in (
                        'PP-OCRv6_det_small.onnx', 'PP-OCRv6_rec_small.onnx',
                        'ch_ppocr_mobile_v2.0_cls_mobile.onnx'))):
                raise ValueError('离线模型或 OCR 资源缺失，安装不能视为完成')
            print(json.dumps({'installed': True, **report}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    sys.exit(main())
