"""Signed program-only updates. Workspace state is never part of the payload."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time
import uuid
import zipfile

MAX_BYTES=64*1024*1024
ALLOWED_ROOTS={'knowledge_bridge','memory_compiler','static','docs','desktop'}
ALLOWED_FILES={'VERSION','pyproject.toml','requirements.lock.txt','README.md','QUICK_START.md','LICENSE','THIRD_PARTY_NOTICES.txt','butler-resources.lock.json','ocr-resources.lock.json'}
NATIVE_INPUTS={'desktop/Cargo.toml','desktop/Cargo.lock','desktop/tauri.conf.json','desktop/build.rs','desktop/build_native.py','desktop/icon_assets.py'}

def safe_member(name):
    p=PurePosixPath(name)
    return bool(name and '\\' not in name and ':' not in name and not p.is_absolute() and '..' not in p.parts
        and all(part.rstrip(' .')==part and not any(ord(c)<32 for c in part) and part.split('.')[0].upper() not in {'CON','PRN','AUX','NUL',*('COM'+str(i) for i in range(1,10)),*('LPT'+str(i) for i in range(1,10))} for part in p.parts)
        and not any(x.startswith('.') for x in p.parts) and (p.parts[0] in ALLOWED_ROOTS or name in ALLOWED_FILES)
        and name!='knowledge_bridge/update-public-key.txt' and '__pycache__' not in p.parts
        and (p.parts[0]!='memory_compiler' or (len(p.parts)==2 and p.suffix=='.py')))


def inspect_package(package,program,public_key=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key=public_key or base64.b64decode((Path(program)/'knowledge_bridge/update-public-key.txt').read_text('ascii').strip(),validate=True)
    with zipfile.ZipFile(package) as z:
        entries=z.infolist();names=[i.filename for i in entries]
        if len(names)!=len(set(n.casefold() for n in names)) or len(names)>3000 or sum(i.file_size for i in entries)>MAX_BYTES:raise ValueError('更新包结构或大小无效')
        manifest_raw=z.read('manifest.json');signature=base64.b64decode(z.read('signature.txt'),validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(signature,manifest_raw)
        manifest=json.loads(manifest_raw)
        if manifest.get('format')!='jiedu-program-update-v1' or not manifest.get('version'):raise ValueError('更新包版本无效')
        files=manifest.get('files',{})
        if not files or set(names)!={'manifest.json','signature.txt',*('program/'+p for p in files)}:raise ValueError('更新清单与包内容不一致')
        for name,digest in files.items():
            if not safe_member(name):raise ValueError('更新包包含非程序路径')
            if hashlib.sha256(z.read('program/'+name)).hexdigest()!=digest:raise ValueError('更新文件校验失败：'+name)
            if name in NATIVE_INPUTS or name.startswith(('desktop/src/','desktop/icons/','desktop/ui/')):
                installed=Path(program)/name
                if not installed.is_file() or installed.read_bytes()!=z.read('program/'+name):
                    raise ValueError('此版本需要更新原生桌面程序，请使用完整安装器升级')
        if 'requirements.lock.txt' in files:
            installed=Path(program)/'requirements.lock.txt'
            if not installed.is_file() or installed.read_bytes()!=z.read('program/requirements.lock.txt'):
                raise ValueError('此版本需要更新运行环境，请使用完整安装器升级')
        if 'pyproject.toml' in files:
            import tomllib
            from packaging.version import Version
            if Version(tomllib.loads(z.read('program/pyproject.toml').decode('utf-8'))['project']['version'])!=Version(manifest['version']):
                raise ValueError('程序版本与更新清单不一致')
        if (Path(program)/'VERSION').is_file() and not {'VERSION','pyproject.toml'}<=set(files):
            raise ValueError('更新包缺少完整版本元数据，请使用重新生成的更新包')
        if 'VERSION' in files:
            from packaging.version import Version
            if Version(z.read('program/VERSION').decode('ascii').strip())!=Version(manifest['version']):
                raise ValueError('产品版本与更新清单不一致')
    return manifest


def apply_files(package,program,history,public_key=None,after_write=None):
    """Caller stops services first; transaction journal permits rollback after failure."""
    manifest=inspect_package(package,program,public_key);program=Path(program).resolve();history=Path(history)
    transaction=history/('update-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);transaction.mkdir(parents=True)
    saved=[];journal=transaction/'journal.json'
    def receipt(status):journal.write_text(json.dumps({'status':status,'version':manifest['version'],'files':saved},ensure_ascii=False,indent=2),'utf-8')
    with zipfile.ZipFile(package) as z:
        # Back up every overwritten file before the first mutation.
        for name in manifest['files']:
            target=(program/name).resolve()
            if not target.is_relative_to(program):raise ValueError('更新路径越界')
            old=transaction/'before'/name
            if target.exists():old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
            saved.append({'path':name,'existed':target.exists()})
        receipt('prepared')
        try:
            for item in saved:
                name=item['path'];target=program/name;target.parent.mkdir(parents=True,exist_ok=True)
                tmp=target.with_name(target.name+'.update-'+uuid.uuid4().hex)
                try:
                    with tmp.open('wb') as f:f.write(z.read('program/'+name));f.flush();os.fsync(f.fileno())
                    os.replace(tmp,target)
                finally:tmp.unlink(missing_ok=True)
                if after_write:after_write(name)
            receipt('files_applied')
        except BaseException:
            restore(program,transaction);raise
    return transaction,manifest


def restore(program,transaction):
    program=Path(program).resolve();transaction=Path(transaction)
    record=json.loads((transaction/'journal.json').read_text('utf-8'))
    for item in record['files']:
        if not safe_member(item['path']):raise ValueError('回退清单路径无效')
        target=(program/item['path']).resolve()
        if not target.is_relative_to(program):raise ValueError('回退路径越界')
        if item['existed']:
            tmp=target.with_name(target.name+'.restore-'+uuid.uuid4().hex);shutil.copy2(transaction/'before'/item['path'],tmp);os.replace(tmp,target)
        else:target.unlink(missing_ok=True)
    record['status']='rolled_back';(transaction/'journal.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')


def main():
    from .workspace import PROGRAM_ROOT
    from .bridge_launcher import Launcher
    from filelock import FileLock
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['check','apply']);parser.add_argument('package');parser.add_argument('--handoff');a=parser.parse_args()
    program=PROGRAM_ROOT;manifest=inspect_package(a.package,program)
    from packaging.version import Version
    from .online_update import version
    if Version(manifest['version'])<=Version(version(program)):raise ValueError('更新版本不高于当前版本，不执行降级或重复安装')
    if a.action=='check':print(f"界渡程序更新 {manifest['version']}\n签名与 {len(manifest['files'])} 个文件校验通过。\n不会替换知识库、模型、配置或归档进度。");return
    if not a.handoff:
        raise ValueError('请在界渡的软件更新页开始更新；安装前必须等待任务自然完成，命令行不会强制停止服务')
    from .full_installer_update import validate_handoff
    request=validate_handoff(a.handoff,program)
    if request['kind']!='program-only' or Path(request['artifact']).resolve()!=Path(a.package).resolve():
        raise ValueError('更新交接与程序包不一致')
    launcher=Launcher(program)
    # The external helper already requested cooperative exit. Recheck every
    # interpreter/native instance before touching files; this updater is the
    # only allowed interpreter, and runtime bytes cannot change in this format.
    from .process_runtime import image_processes
    for executable in (launcher.python,launcher.pythonw,program.parent.parent/'界渡.exe'):
        if any(row['pid']!=os.getpid() for row in image_processes(executable)):
            raise ValueError('仍有程序使用此安装目录，未替换任何文件；请关闭后重试')
    marker=launcher.runtime/'update-stop.json'
    stop=json.loads(marker.read_text('utf-8'))
    if stop.get('token')!=request['token'] or stop.get('launch_id')!=launcher.load().get('launch_id'):
        raise ValueError('缺少匹配的协作退出记录')
    history=launcher.vault/'.jiedu/history/software-updates';history.mkdir(parents=True,exist_ok=True)
    with FileLock(str(history/'update.lock'),timeout=1):
        transaction=None
        try:
            transaction,manifest=apply_files(a.package,program,history)
            marker.unlink()
            launcher.start()
            if not launcher.status().get('health_owned'):raise RuntimeError('更新后服务未通过归属及健康检查')
            record=json.loads((transaction/'journal.json').read_text('utf-8'));record['status']='healthy';(transaction/'journal.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
        except Exception:
            # Never replace code underneath a newly started process after a
            # failed health probe. The transaction remains available for a
            # later cooperative recovery instead of killing current work.
            running=any(row['pid']!=os.getpid() for executable in (launcher.python,launcher.pythonw)
                        for row in image_processes(executable))
            if not running:
                if transaction:restore(program,transaction)
                if marker.exists() and json.loads(marker.read_text('utf-8')).get('token')==request['token']:marker.unlink()
                launcher.start()
            raise
    print('程序更新完成。资料和原有进度保留，服务已重新就绪。')

if __name__=='__main__':main()
