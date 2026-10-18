"""Synchronize release metadata from VERSION; no build, upload or publication."""
import argparse
import json
from pathlib import Path
import re
import tomllib


def version_info(source):
    value=(Path(source)/'VERSION').read_text('ascii').strip()
    match=re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.(0|[1-9]\d*))?',value)
    if not match:raise ValueError('VERSION must be major.minor.patch or major.minor.patch-rc.N')
    major,minor,patch,rc=match.groups()
    if any(int(n)>65535 for n in [major,minor,patch,rc or '0']):raise ValueError('Windows version components exceed 65535')
    base=f'{major}.{minor}.{patch}'
    return {'version':value,'python_version':base+(f'rc{rc}' if rc is not None else ''),'file_version':base+'.'+(rc or '0')}


def metadata(source):
    root=Path(source);result=version_info(root)
    result['pyproject']=tomllib.loads((root/'pyproject.toml').read_text('utf-8'))['project']['version']
    result['python_package']=re.search(r'__version__\s*=\s*[\'"]([^\'"]+)',(root/'knowledge_bridge/__init__.py').read_text('utf-8')).group(1)
    result['cargo']=tomllib.loads((root/'desktop/Cargo.toml').read_text('utf-8'))['package']['version']
    lock=tomllib.loads((root/'desktop/Cargo.lock').read_text('utf-8'))
    result['cargo_lock']=next(p['version'] for p in lock['package'] if p['name']=='jiedu-desktop')
    result['tauri']=json.loads((root/'desktop/tauri.conf.json').read_text('utf-8'))['version']
    installer=(root/'desktop/installer.iss').read_text('utf-8')
    result['installer_uses_version_file']=all(value in installer for value in ['\\.jiedu\\program\\VERSION','AppVersion={#ReleaseVersion}','VersionInfoVersion={#ReleaseFileVersion}','VersionInfoProductVersion={#ReleaseFileVersion}','VersionInfoProductTextVersion={#ReleaseVersion}','OutputBaseFilename=Jiedu-{#ReleaseVersion}-windows-x64-setup'])
    return result


def check(source,scope='full'):
    data=metadata(source)
    if scope not in {'full','program'}:raise ValueError('Unknown release scope')
    native_version=data['version'] if scope=='full' else data['cargo']
    mismatches={name:data[name] for name in ['cargo','cargo_lock','tauri'] if data[name]!=native_version}
    mismatches.update({name:data[name] for name in ['pyproject','python_package'] if data[name]!=data['python_version']})
    if not data['installer_uses_version_file']:mismatches['installer']='Must read VERSION for product/output and Windows file versions'
    if mismatches:raise ValueError('Version metadata differs from VERSION: '+json.dumps(mismatches,ensure_ascii=False))
    return {**data,'scope':scope,'native_build_version':data['cargo'],'product_matches_native_build':data['cargo']==data['version'],'passed':True}


def sync(source,scope='full'):
    root=Path(source);info=version_info(root);changed=[]
    updates=[
        ('pyproject.toml',r'(?m)^version\s*=\s*"[^"]+"',f'version = "{info["python_version"]}"'),
        ('knowledge_bridge/__init__.py',r'(?m)^__version__\s*=\s*"[^"]+"',f'__version__ = "{info["python_version"]}"')]
    if scope=='full':updates.extend([
        ('desktop/Cargo.toml',r'(?m)^version\s*=\s*"[^"]+"',f'version = "{info["version"]}"'),
        ('desktop/Cargo.lock',r'(name = "jiedu-desktop"\s*\n)version = "[^"]+"',lambda m:m.group(1)+f'version = "{info["version"]}"')])
    for name,pattern,replacement in updates:
        path=root/name;old=path.read_text('utf-8');new,count=re.subn(pattern,replacement,old,count=1)
        if count!=1:raise ValueError('Expected one version field: '+name)
        if new!=old:path.write_text(new,'utf-8');changed.append(name)
    path=root/'desktop/tauri.conf.json';old=json.loads(path.read_text('utf-8'))
    if scope=='full' and old['version']!=info['version']:
        old['version']=info['version'];path.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n','utf-8');changed.append('desktop/tauri.conf.json')
    return {**check(root,scope),'changed':changed}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','sync'],nargs='?',default='check')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--scope',choices=['full','program'],default='full')
    args=parser.parse_args()
    print(json.dumps((sync if args.action=='sync' else check)(args.source,args.scope),ensure_ascii=False,indent=2))
