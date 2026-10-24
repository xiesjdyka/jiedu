"""Build the desktop shell and set its Windows release resource from VERSION.

Uses an explicit local tools directory; never installs system tools or publishes.
Post-link resources use the approved v1 ICO and the release RT_VERSION. The
manifest and unrelated resources are preserved and checked. Provisioning the
pinned tools is a separate step.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_version import check, version_info
from icon_assets import verify_assets, ico_frames


def resource_snapshot(path):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.LoadLibraryExW.argtypes=[wintypes.LPCWSTR,ctypes.c_void_p,wintypes.DWORD];k.LoadLibraryExW.restype=ctypes.c_void_p
    k.FreeLibrary.argtypes=[ctypes.c_void_p]
    k.FindResourceExW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD];k.FindResourceExW.restype=ctypes.c_void_p
    k.SizeofResource.argtypes=[ctypes.c_void_p,ctypes.c_void_p];k.SizeofResource.restype=wintypes.DWORD
    k.LoadResource.argtypes=[ctypes.c_void_p,ctypes.c_void_p];k.LoadResource.restype=ctypes.c_void_p
    k.LockResource.argtypes=[ctypes.c_void_p];k.LockResource.restype=ctypes.c_void_p
    names_cb=ctypes.WINFUNCTYPE(wintypes.BOOL,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ssize_t)
    langs_cb=ctypes.WINFUNCTYPE(wintypes.BOOL,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_ssize_t)
    k.EnumResourceNamesW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,names_cb,ctypes.c_ssize_t]
    k.EnumResourceLanguagesW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,langs_cb,ctypes.c_ssize_t]
    h=k.LoadLibraryExW(str(Path(path).resolve()),None,2)
    if not h:raise ctypes.WinError(ctypes.get_last_error())
    result={};failures=[]
    @langs_cb
    def visit_lang(module,kind,name,language,_):
        try:
            entry=k.FindResourceExW(module,kind,name,language)
            if not entry:raise ctypes.WinError(ctypes.get_last_error())
            size=k.SizeofResource(module,entry);data=k.LockResource(k.LoadResource(module,entry))
            if not data:raise ctypes.WinError(ctypes.get_last_error())
            label=int(name) if int(name)<=65535 else ctypes.wstring_at(name)
            result[(int(kind),label,int(language))]=ctypes.string_at(data,size)
            return True
        except Exception as exc:failures.append(exc);return False
    @names_cb
    def visit_name(module,kind,name,_):
        if not k.EnumResourceLanguagesW(module,kind,name,visit_lang,0):
            if not failures:failures.append(ctypes.WinError(ctypes.get_last_error()))
            return False
        return True
    try:
        for kind in [3,14,16,24]:
            ctypes.set_last_error(0)
            if not k.EnumResourceNamesW(h,ctypes.c_void_p(kind),visit_name,0):
                code=ctypes.get_last_error()
                if code not in (0,1813):raise ctypes.WinError(code)
        if failures:raise failures[0]
        return result
    finally:k.FreeLibrary(h)


def align(value):return (value+3)&~3


def set_icon_resource(path,ico):
    """Embed the exact approved ICO frames; keep version and manifest untouched."""
    frames=ico_frames(ico);before=resource_snapshot(path)
    group=bytearray(struct.pack('<HHH',0,1,len(frames)));replacement={}
    for index,((width,height),raw) in enumerate(sorted(frames.items()),1):
        group.extend(struct.pack('<BBBBHHIH',width%256,height%256,0,0,1,32,len(raw),index))
        replacement[(3,index,1033)]=raw
    replacement[(14,1,1033)]=bytes(group)
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.BeginUpdateResourceW.argtypes=[wintypes.LPCWSTR,wintypes.BOOL];k.BeginUpdateResourceW.restype=ctypes.c_void_p
    k.UpdateResourceW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_void_p,wintypes.DWORD]
    k.EndUpdateResourceW.argtypes=[ctypes.c_void_p,wintypes.BOOL]
    handle=k.BeginUpdateResourceW(str(Path(path).resolve()),False)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    committed=False
    def update(key,raw):
        kind,name,language=key
        ptr=ctypes.c_void_p(name) if isinstance(name,int) else ctypes.cast(ctypes.c_wchar_p(name),ctypes.c_void_p)
        buffer=ctypes.create_string_buffer(raw) if raw is not None else None
        if not k.UpdateResourceW(handle,ctypes.c_void_p(kind),ptr,language,buffer,len(raw) if raw is not None else 0):raise ctypes.WinError(ctypes.get_last_error())
    try:
        for key in before:
            if key[0] in (3,14) and key not in replacement:update(key,None)
        for key,raw in replacement.items():update(key,raw)
        if not k.EndUpdateResourceW(handle,False):raise ctypes.WinError(ctypes.get_last_error())
        committed=True
    finally:
        if not committed:k.EndUpdateResourceW(handle,True)
    after=resource_snapshot(path)
    if {k:v for k,v in after.items() if k[0] in (3,14)}!=replacement:raise ValueError('PE icon resource differs from approved ICO')
    if {k:v for k,v in before.items() if k[0] not in (3,14)}!={k:v for k,v in after.items() if k[0] not in (3,14)}:raise ValueError('Icon update changed manifest/version')
    return {'ico_sha256':hashlib.sha256(ico).hexdigest(),'frame_count':len(frames),
            'frame_sizes':sorted(w for w,h in frames),'exact_approved_frames':True,'version_and_manifest_preserved':True}


def parse_block(raw,start=0):
    length,value_length,kind=struct.unpack_from('<HHH',raw,start)
    if length<6 or start+length>len(raw):raise ValueError('Invalid version block')
    cursor=start+6;key_start=cursor
    while raw[cursor:cursor+2]!=b'\0\0':
        cursor+=2
        if cursor>=start+length:raise ValueError('Unterminated version key')
    key=raw[key_start:cursor].decode('utf-16-le');cursor=align(cursor+2)
    size=value_length*(2 if kind==1 else 1);value=raw[cursor:cursor+size]
    if len(value)!=size:raise ValueError('Truncated version value')
    child_start=align(cursor+size);children=[]
    while child_start+6<=start+length:
        child_length=struct.unpack_from('<H',raw,child_start)[0]
        if not child_length:break
        child=parse_block(raw,child_start);children.append(child);child_start=align(child_start+child_length)
    return {'key':key,'kind':kind,'value':value,'children':children}


def serialize(node):
    raw=bytearray(b'\0'*6+node['key'].encode('utf-16-le')+b'\0\0')
    raw.extend(b'\0'*(align(len(raw))-len(raw)));raw.extend(node['value'])
    if node['children']:raw.extend(b'\0'*(align(len(raw))-len(raw)))
    for child in node['children']:
        raw.extend(serialize(child));raw.extend(b'\0'*(align(len(raw))-len(raw)))
    count=len(node['value'])//2 if node['kind']==1 else len(node['value'])
    struct.pack_into('<HHH',raw,0,len(raw),count,node['kind']);return bytes(raw)


def set_version_resource(path,info):
    before=resource_snapshot(path);versions={key:raw for key,raw in before.items() if key[0]==16}
    if not versions:raise ValueError('Desktop has no version resource')
    numbers=[int(v) for v in info['file_version'].split('.')]
    ms=(numbers[0]<<16)|numbers[1];ls=(numbers[2]<<16)|numbers[3]
    replacement={}
    def change(node):
        if node['key']=='VS_VERSION_INFO':
            values=list(struct.unpack('<13I',node['value']))
            if values[0]!=0xFEEF04BD:raise ValueError('Unknown fixed version signature')
            values[2:6]=[ms,ls,ms,ls];node['value']=struct.pack('<13I',*values)
        elif node['key']=='FileVersion':node['value']=(info['file_version']+'\0').encode('utf-16-le')
        elif node['key']=='ProductVersion':node['value']=(info['version']+'\0').encode('utf-16-le')
        for child in node['children']:change(child)
    for key,raw in versions.items():
        tree=parse_block(raw);change(tree);replacement[key]=serialize(tree)
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.BeginUpdateResourceW.argtypes=[wintypes.LPCWSTR,wintypes.BOOL];k.BeginUpdateResourceW.restype=ctypes.c_void_p
    k.UpdateResourceW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_void_p,wintypes.DWORD]
    k.EndUpdateResourceW.argtypes=[ctypes.c_void_p,wintypes.BOOL]
    handle=k.BeginUpdateResourceW(str(Path(path).resolve()),False)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    committed=False
    try:
        for (kind,name,language),raw in replacement.items():
            name_ptr=ctypes.c_void_p(name) if isinstance(name,int) else ctypes.cast(ctypes.c_wchar_p(name),ctypes.c_void_p)
            buffer=ctypes.create_string_buffer(raw)
            if not k.UpdateResourceW(handle,ctypes.c_void_p(kind),name_ptr,language,buffer,len(raw)):raise ctypes.WinError(ctypes.get_last_error())
        if not k.EndUpdateResourceW(handle,False):raise ctypes.WinError(ctypes.get_last_error())
        committed=True
    finally:
        if not committed:k.EndUpdateResourceW(handle,True)
    after=resource_snapshot(path)
    preserved={key:raw for key,raw in before.items() if key[0]!=16}
    if preserved!={key:raw for key,raw in after.items() if key[0]!=16}:raise ValueError('Icon or manifest changed during version update')
    strings={}
    def collect(node):
        if node['key'] in {'FileVersion','ProductVersion'}:strings[node['key']]=node['value'].decode('utf-16-le').rstrip('\0')
        for child in node['children']:collect(child)
    for key,raw in after.items():
        if key[0]==16:
            tree=parse_block(raw);values=struct.unpack('<13I',tree['value'])
            if list(values[2:6])!=[ms,ls,ms,ls]:raise ValueError('Numeric PE version verification failed')
            collect(tree)
    if strings!={'FileVersion':info['file_version'],'ProductVersion':info['version']}:raise ValueError('String PE version verification failed')
    return {'numeric_file_version':info['file_version'],'numeric_product_version':info['file_version'],**strings,
            'icons_and_manifest_unchanged':True,'preserved_resource_count':len(preserved),
            'preserved_resource_sha256':{str(key):hashlib.sha256(raw).hexdigest() for key,raw in preserved.items()}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args();source=args.source.resolve();tools=args.tools.resolve();q=args.candidate.resolve()
    info=check(source);artwork=verify_assets(source);receipts=q/'receipts';receipts.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy();env.update(CARGO_HOME=str(tools/'cargo'),RUSTUP_HOME=str(tools/'rustup'),CARGO_TARGET_DIR=str(tools/'target'),TEMP=str(q/'temp/native'),TMP=str(q/'temp/native'),PYTHONDONTWRITEBYTECODE='1')
    Path(env['TEMP']).mkdir(parents=True,exist_ok=True)
    ready_path=tools/'native-tools-ready.json'
    if ready_path.is_file():
        # Official standalone rustc/cargo/std archives avoid a user rustup install.
        ready=json.loads(ready_path.read_text('utf-8'))
        if not ready.get('passed') or Path(ready['tool_root']).resolve()!=tools:raise ValueError('Native tool receipt belongs to another root')
        for row in ready['identities']:
            path=Path(row['path'])
            if path.stat().st_size!=row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Pinned native tool changed: '+path.name)
        env.update(ready['environment'])
        cargo=next(Path(row['path']) for row in ready['identities'] if Path(row['path']).name=='cargo.exe')
        rc=Path(env['RC']);link=Path(env['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'])
    else:
        finder=Path(os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
        vs=Path(subprocess.check_output([str(finder),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip())
        vc=max((vs/'VC/Tools/MSVC').iterdir(),key=lambda p:tuple(map(int,p.name.split('.'))))
        sdk=tools/'microsoft.windows.sdk.cpp/c';sdk64=tools/'microsoft.windows.sdk.cpp.x64/c'
        sdkversion=max((p for p in (sdk/'bin').iterdir() if p.name.startswith('10.')),key=lambda p:tuple(map(int,p.name.split('.')))).name
        rc=sdk/'bin'/sdkversion/'x64/rc.exe';link=vc/'bin/Hostx64/x64/link.exe';cargo=tools/'cargo/bin/cargo.exe'
        env['LIB']=';'.join(map(str,[vc/'lib/x64',sdk64/'um/x64',sdk64/'ucrt/x64']))
        env['INCLUDE']=';'.join(map(str,[vc/'include',sdk/'Include'/sdkversion/'ucrt',sdk/'Include'/sdkversion/'shared',sdk/'Include'/sdkversion/'um',sdk/'Include'/sdkversion/'winrt']))
    for p in [rc,link,cargo]:
        if not p.is_file():raise ValueError('Required native tool missing: '+p.name)
    host_profile=Path.home()
    for name in ['HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME']:
        location=q/'temp/native-home'/name.lower();location.mkdir(parents=True,exist_ok=True);env[name]=str(location)
    env['PATH']=';'.join(map(str,[cargo.parent,link.parent,rc.parent]))+';'+env.get('PATH','');env['RC']=str(rc)
    env['RUSTC']=str(cargo.parent/'rustc.exe');env['RUSTDOC']=str(cargo.parent/'rustdoc.exe')
    env['RUSTFLAGS']=' '.join(['--remap-path-prefix='+str(source.parent)+'=/jiedu-build','--remap-path-prefix='+str(host_profile)+'=/build-user','-C debuginfo=0','-C link-arg=/PDBALTPATH:jiedu-desktop.pdb'])
    command=[str(cargo),'build','--release','--locked','--manifest-path',str(source/'desktop/Cargo.toml')]
    record={'version':info['version'],'expected_file_version':info['file_version'],'command':command,'environment':{k:env[k] for k in ['CARGO_HOME','RUSTUP_HOME','CARGO_TARGET_DIR','TEMP','TMP','RC','LIB','INCLUDE','RUSTFLAGS']},'started_at':time.time()}
    for name in ['rustc','cargo']:
        record[name+'_version']=subprocess.check_output([str(cargo.parent/f'{name}.exe'),'--version','--verbose'],env=env,text=True).strip()
    with (receipts/'native-build.log').open('w',encoding='utf-8') as log:
        result=subprocess.run(command,env=env,cwd=source/'desktop',stdout=log,stderr=subprocess.STDOUT,creationflags=0x08000000)
    record['returncode']=result.returncode
    if result.returncode:
        (receipts/'native-build.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8');raise SystemExit(result.returncode)
    output=q/'native/界渡.exe';output.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(Path(env['CARGO_TARGET_DIR'])/'release/jiedu-desktop.exe',output)
    record['pre_resource_sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
    record['artwork']=artwork
    record['icon_resource']=set_icon_resource(output,(source/'desktop/icons/icon.ico').read_bytes())
    record['version_resource']=set_version_resource(output,info)
    report=receipts/'native-version-report.json'
    subprocess.run([str(output),'--version-report',str(report)],env=env,check=True,timeout=20,creationflags=0x08000000)
    runtime=json.loads(report.read_text('utf-8'))
    if runtime['native_build_version']!=info['version'] or runtime['windows_file_version']!=info['file_version']:raise ValueError('Native compiled version does not match VERSION')
    record['product_version_runtime_verified']=runtime.get('product_version_verified',False)
    record.update(output=str(output),sha256=hashlib.sha256(output.read_bytes()).hexdigest(),bytes=output.stat().st_size,version_report=runtime,elapsed_seconds=round(time.time()-record['started_at'],3),passed=True)
    (receipts/'native-build.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'output':str(output),'sha256':record['sha256'],'bytes':record['bytes'],'product_version':info['version'],'file_version':info['file_version'],'passed':True},ensure_ascii=False))

if __name__=='__main__':main()
