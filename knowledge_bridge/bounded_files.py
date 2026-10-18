"""Owner-selected Windows folders: pinned boundaries and recoverable file changes."""
from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
import re
import unicodedata
import uuid
from contextlib import ExitStack,contextmanager
from pathlib import Path

from filelock import FileLock

from .writer_core import save,sha


def relative_parts(value,allow_empty=False):
    if not isinstance(value,str):raise ValueError('文件路径格式无效')
    if allow_empty and value=='':return ()
    value=value.replace('\\','/')
    parts=value.split('/')
    if not parts or len(parts)>32 or len(value)>1024:raise ValueError('文件路径过长')
    for part in parts:
        normalized=unicodedata.normalize('NFKC',part).upper().split('.')[0]
        if (not part or part in {'.','..'} or part[-1:] in {' ','.'} or ':' in part
            or any(ord(char)<32 for char in part) or any(char in '<>"|?*' for char in part)
            or normalized in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$'}
            or re.fullmatch(r'(COM|LPT)[1-9]',normalized)
            or part.casefold()=='.jiedu' or part.casefold().startswith('.jiedu-')):
            raise ValueError('文件路径不在允许范围')
    return tuple(parts)


class WindowsHandles:
    """Keep every directory ancestor open without delete sharing, reject reparse points.

    Mutating leaves are held with DELETE access and no write/delete sharing. Handle
    renames move the original into recovery before a new file is installed without
    replacement. Concurrent destination creation therefore fails instead of losing data.
    """
    def __init__(self):
        if os.name!='nt':raise ValueError('当前指定文件夹操作需要Windows文件句柄支持')
        from ctypes import wintypes
        self.w=wintypes;self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        self.api.CreateFileW.restype=wintypes.HANDLE
        self.api.CloseHandle.argtypes=[wintypes.HANDLE];self.api.CloseHandle.restype=wintypes.BOOL
        self.api.GetFileInformationByHandleEx.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.api.GetFileInformationByHandleEx.restype=wintypes.BOOL
        self.api.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
        self.api.WriteFile.argtypes=list(self.api.ReadFile.argtypes)
        self.api.FlushFileBuffers.argtypes=[wintypes.HANDLE]
        self.api.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.api.SetFileInformationByHandle.restype=wintypes.BOOL

    @contextmanager
    def open(self,path,*,directory=False,mutate=False,create=False):
        access=0x80000000
        if mutate:access|=0x10000
        if create:access|=0x40000000
        handle=self.api.CreateFileW(str(path),access,3 if directory else 1,None,1 if create else 3,0x00200000|(0x02000000 if directory else 0),None)
        if handle==ctypes.c_void_p(-1).value:raise OSError('文件正在变更、无权限或不可用')
        try:
            attrs=(ctypes.c_ulong*2)()
            if not self.api.GetFileInformationByHandleEx(handle,9,ctypes.byref(attrs),ctypes.sizeof(attrs)) or attrs[0]&0x400:
                raise ValueError('链接、重解析点或无法核验的文件不可访问')
            if bool(attrs[0]&0x10)!=directory:raise ValueError('路径的文件类型不符')
            if not directory:
                class Standard(ctypes.Structure):
                    _fields_=[('AllocationSize',ctypes.c_longlong),('EndOfFile',ctypes.c_longlong),
                              ('NumberOfLinks',self.w.DWORD),('DeletePending',ctypes.c_ubyte),('Directory',ctypes.c_ubyte)]
                standard=Standard()
                if not self.api.GetFileInformationByHandleEx(handle,1,ctypes.byref(standard),ctypes.sizeof(standard)) or standard.NumberOfLinks!=1:
                    raise ValueError('硬链接或无法核验的文件不可访问')
            yield handle
        finally:self.api.CloseHandle(handle)

    @contextmanager
    def pin(self,root,parents):
        with ExitStack() as stack:
            current=Path(root.anchor)
            stack.enter_context(self.open(current,directory=True))
            for part in root.parts[1:]+tuple(parents):
                current=current/part;stack.enter_context(self.open(current,directory=True))
            yield current

    def read(self,handle,limit=131072):
        output=bytearray();buffer=ctypes.create_string_buffer(65536);size=self.w.DWORD()
        while True:
            if not self.api.ReadFile(handle,buffer,len(buffer),ctypes.byref(size),None):raise OSError('文件读取失败')
            output.extend(buffer.raw[:size.value])
            if len(output)>limit:raise ValueError('文件超过本次读取额度')
            if not size.value:return bytes(output)

    def write(self,handle,raw):
        buffer=ctypes.create_string_buffer(raw);size=self.w.DWORD()
        if not self.api.WriteFile(handle,buffer,len(raw),ctypes.byref(size),None) or size.value!=len(raw):raise OSError('文件保存未完成')
        if not self.api.FlushFileBuffers(handle):raise OSError('文件保存未落盘')

    def rename(self,handle,destination):
        class Rename(ctypes.Structure):
            _fields_=[('ReplaceIfExists',self.w.BOOL),('RootDirectory',self.w.HANDLE),
                      ('FileNameLength',self.w.DWORD),('FileName',self.w.WCHAR*1)]
        raw=str(destination).encode('utf-16-le')
        size=Rename.FileName.offset+len(raw)+ctypes.sizeof(self.w.WCHAR)
        buffer=ctypes.create_string_buffer(max(size,ctypes.sizeof(Rename)))
        info=Rename.from_buffer(buffer);info.ReplaceIfExists=False;info.RootDirectory=None;info.FileNameLength=len(raw)
        ctypes.memmove(ctypes.addressof(buffer)+Rename.FileName.offset,raw,len(raw))
        if not self.api.SetFileInformationByHandle(handle,3,buffer,size):raise OSError('目标已存在、文件在使用或移动无法完成')


class SelectedFolders:
    def __init__(self,context,permissions=None):
        self.context=context;self.root=context.state/'selected-folders';self.path=self.root/'grants.json';self.on_change=[]
        self.permissions=permissions

    def range_grants(self):
        from .file_browser import root_id,local_drives
        access=self.permissions.file_access() if self.permissions else {'mode':'off'}
        if access['mode']=='off':return []
        roots=[self.context.root]+(local_drives() if access['mode']=='computer' else [])
        return [dict(id=root_id(root),path=str(root),label='界渡全部文件' if root==self.context.root else str(root),revision=access['revision'],revoked=False,range=True) for root in roots if root.is_dir()]

    def _check_path(self,root,parts,mutation=False):
        from .file_browser import check_private_path
        path=root.joinpath(*parts)
        workspace=self.context.root.absolute()
        check_private_path(path.relative_to(workspace) if path==workspace or workspace in path.parents else path)
        if path==workspace or workspace in path.parents:
            relative=path.relative_to(workspace)
            if mutation and relative.parts and relative.parts[0]=='90_原始档案':raise PermissionError('原始档案保留原件，不允许普通文件操作覆盖或删除')
            if path.is_file():
                from .organizer import reject_protected
                reject_protected(relative.as_posix())
        return path

    def lock(self):
        self.root.mkdir(parents=True,exist_ok=True);return FileLock(str(self.root/'folders.lock'))

    def _load(self):
        if not self.path.exists():return {'folders':{}}
        try:return json.loads(self.path.read_text('utf-8'))
        except (OSError,ValueError) as exc:raise ValueError('指定文件夹授权记录损坏') from exc

    def list(self):
        with self.lock():return self.range_grants()+[dict(row,id=key) for key,row in self._load()['folders'].items()]

    def add(self,path):
        root=Path(path).expanduser().absolute()
        from .file_browser import check_private_path
        check_private_path(root)
        if not root.is_absolute() or str(root).startswith('\\\\') or not root.is_dir():raise ValueError('请选择实际存在的本机文件夹')
        workspace=self.context.root.resolve();resolved=root.resolve()
        if resolved==workspace or workspace in resolved.parents or resolved in workspace.parents:
            raise ValueError('界渡工作区及其上级不能作为普通文件夹授权；正式知识使用Writer，原始证据保持只读')
        handles=WindowsHandles()
        with handles.pin(root,()):
            stat=root.stat();identity=[stat.st_dev,stat.st_ino]
            with self.lock():
                data=self._load();key=next((key for key,row in data['folders'].items()
                    if row['path']==str(root) and row['identity']==identity),uuid.uuid4().hex)
                data['folders'][key]={'path':str(root),'label':root.name,'identity':identity,'revision':uuid.uuid4().hex,'revoked':False}
                save(self.path,data)
        for callback in self.on_change:callback(key)
        return {'id':key,'label':root.name}

    def revoke(self,key):
        with self.lock():
            data=self._load();row=data['folders'].get(key)
            if not row:raise ValueError('文件夹授权不存在')
            row['revoked']=True;row['revision']=uuid.uuid4().hex;save(self.path,data)
        for callback in self.on_change:callback(key)
        return {'id':key,'revoked':True}

    def _grant(self,key):
        if self.permissions and self.permissions.file_access()['mode']=='off' and 'file_access' in self.permissions._load().get('butler',{}):raise PermissionError('文件访问已关闭')
        virtual=next((row for row in self.range_grants() if row['id']==key),None)
        if virtual:return Path(virtual['path']),virtual
        row=self._load()['folders'].get(key)
        if not row or row['revoked']:raise PermissionError('此文件夹未授权或已撤销')
        if self.permissions and self.permissions.file_access()['mode']=='workspace':raise PermissionError('当前范围仅包含界渡文件，未允许本机其他文件夹')
        root=Path(row['path']);stat=root.stat()
        if [stat.st_dev,stat.st_ino]!=row['identity']:raise PermissionError('文件夹身份已变化，请重新选择')
        return root,row

    def public_grants(self):
        rows=self.list()
        if self.permissions:
            access=self.permissions.file_access()
            if access['mode']=='workspace':rows=[row for row in rows if row.get('range')]
            elif access['mode']=='off' and 'file_access' in self.permissions._load().get('butler',{}):rows=[]
        return [{'folder_id':row['id'],'label':row['label'],'revision':row['revision']} for row in rows if not row['revoked']]

    def saved_preview(self,preview_id):
        if not re.fullmatch('[a-f0-9]{32}',preview_id):raise ValueError('预览标识无效')
        with self.lock():return json.loads((self.root/'previews'/(preview_id+'.json')).read_text('utf-8'))

    def changes(self,limit=50):
        with self.lock():
            paths=sorted((self.root/'changes').glob('*.json'),key=lambda path:path.stat().st_mtime,reverse=True)[:limit]
            rows=[]
            for path in paths:
                row=json.loads(path.read_text('utf-8'));grant=self._load()['folders'].get(row['folder_id']) or {}
                rows.append({**row,'folder_label':grant.get('label','指定文件夹'),'folder_revoked':grant.get('revoked',True)})
            return rows

    def list_files(self,key,path='',limit=100):
        parts=relative_parts(path,True);handles=WindowsHandles()
        with self.lock():
            root,grant=self._grant(key)
            self._check_path(root,parts)
            with handles.pin(root,parts) as directory:
                self._grant(key);rows=[]
                with os.scandir(directory) as entries:
                    for item in entries:
                        if item.name.casefold()=='.jiedu' or item.name.casefold().startswith('.jiedu-'):continue
                        try:self._check_path(root,(*parts,item.name))
                        except (ValueError,PermissionError):continue
                        stat=item.stat(follow_symlinks=False);linked=bool(getattr(stat,'st_file_attributes',0)&0x400)
                        rows.append({'name':item.name,'kind':'blocked_link' if linked else 'directory' if item.is_dir(follow_symlinks=False) else 'file','size_bytes':stat.st_size})
                        if len(rows)>limit:break
                return {'folder_id':key,'path':path,'items':rows[:limit],'truncated':len(rows)>limit,'revision':grant['revision']}

    def read(self,key,path):
        parts=relative_parts(path);handles=WindowsHandles()
        with self.lock():
            root,grant=self._grant(key)
            self._check_path(root,parts)
            with handles.pin(root,parts[:-1]) as parent,handles.open(parent/parts[-1]) as handle:
                self._grant(key);raw=handles.read(handle)
        return {'folder_id':key,'path':path,'content':raw.decode('utf-8-sig'),'sha256':sha(raw),'revision':grant['revision'],'size_bytes':len(raw)}

    def preview(self,key,action,path,*,destination=None,content=None):
        if action not in {'write','rename','move','delete'}:raise ValueError('文件操作不支持')
        parts=relative_parts(path);dest=relative_parts(destination) if destination is not None else None
        if action in {'rename','move'} and (not dest or dest==parts):raise ValueError('请填写不同的目标相对路径')
        if action=='rename' and dest[:-1]!=parts[:-1]:raise ValueError('重命名须在同一目录；跨目录请使用移动')
        if action=='write' and (not isinstance(content,str) or len(content.encode())>131072):raise ValueError('写入正文须在128KiB以内')
        if action!='write' and content is not None:raise ValueError('该操作不接受写入正文')
        handles=WindowsHandles()
        with self.lock():
            root,grant=self._grant(key)
            source=self._check_path(root,parts,True)
            if dest:self._check_path(root,dest,True)
            workspace=self.context.root.absolute()
            if workspace in source.parents and source.suffix.lower()=='.md' and source.relative_to(workspace).parts[0] in {'00_系统','01_当前状态','02_项目','03_知识','99_收件箱'}:
                from .writer_core import Writer
                if action!='write':raise ValueError('正式知识的移动与删除请使用知识页面；普通写入由 Writer 保存版本')
                relative=source.relative_to(workspace).as_posix();writer=Writer(workspace,self.context.versions/'formal-memory')
                preview=writer.preview(relative,source.stem,content,action='update' if source.exists() else 'create',expected_hash=sha(source.read_bytes()) if source.exists() else None)
                item={'preview_id':preview['preview_id'],'folder_id':key,'folder_revision':grant['revision'],'action':action,'path':'/'.join(parts),'destination':None,'content':content,'writer_preview':preview,'before_hash':preview['before_hash'],'diff':preview['diff']}
                save(self.root/'previews'/(item['preview_id']+'.json'),item);return item
            with handles.pin(root,parts[:-1]) as parent:
                source=parent/parts[-1];existed=source.exists();raw=b''
                if existed:
                    with handles.open(source) as handle:raw=handles.read(handle,131072 if action=='write' else 64*1024*1024)
                elif action!='write':raise ValueError('源文件不存在')
                if dest:
                    with handles.pin(root,dest[:-1]) as other:
                        if (other/dest[-1]).exists():raise ValueError('目标已经存在，不会覆盖')
                preview_id=uuid.uuid4().hex
                item={'preview_id':preview_id,'folder_id':key,'folder_revision':grant['revision'],'action':action,
                    'path':'/'.join(parts),'destination':'/'.join(dest) if dest else None,'content':content,
                    'existed':existed,'before_hash':sha(raw),'diff':'\n'.join(difflib.unified_diff(raw.decode('utf-8-sig').splitlines(),(content or '').splitlines(),fromfile='原文件',tofile='拟保存文件',lineterm='')) if action=='write' else None}
                save(self.root/'previews'/(preview_id+'.json'),item);return item

    def apply(self,preview_id,*,validate=lambda:None):
        if not re.fullmatch('[a-f0-9]{32}',preview_id):raise ValueError('预览标识无效')
        handles=WindowsHandles()
        with self.lock():
            item=json.loads((self.root/'previews'/(preview_id+'.json')).read_text('utf-8'))
            root,grant=self._grant(item['folder_id'])
            if grant['revision']!=item['folder_revision']:raise PermissionError('文件夹授权已变化，请重新预览')
            self._check_path(root,relative_parts(item['path']),True)
            receipt_path=self.root/'changes'/(preview_id+'.json')
            if receipt_path.exists():
                previous=json.loads(receipt_path.read_text('utf-8'))
                if previous['state']=='applied':return previous
                raise ValueError('此变更未完成或已恢复，请核对记录并重新预览')
            if item.get('writer_preview'):
                from .writer_core import Writer
                def current(_):
                    validate();_,latest=self._grant(item['folder_id'])
                    if latest['revision']!=item['folder_revision']:raise PermissionError('文件范围已撤销，请重新预览')
                writer=Writer(self.context.root,self.context.versions/'formal-memory')
                result=writer.apply(item['writer_preview'],owner_confirmed=True,validate=current)
                receipt={**item,'change_id':preview_id,'state':'applied','recoverable':True,'writer_version':result['version']}
                receipt.pop('content',None);save(self.root/'changes'/(preview_id+'.json'),receipt);return receipt
            receipt_path=self.root/'changes'/(preview_id+'.json')
            if receipt_path.exists():
                previous=json.loads(receipt_path.read_text('utf-8'))
                if previous['state']=='applied':return previous
                raise ValueError('此变更未完成或已恢复，请核对记录并重新预览')
            parts=relative_parts(item['path']);dest=relative_parts(item['destination']) if item['destination'] else None
            with ExitStack() as stack:
                parent=stack.enter_context(handles.pin(root,parts[:-1]));source=parent/parts[-1]
                destination_parent=stack.enter_context(handles.pin(root,dest[:-1])) if dest else parent
                destination=destination_parent/dest[-1] if dest else source
                if source.exists()!=item['existed']:raise ValueError('预览后文件已变化')
                old=stack.enter_context(handles.open(source,mutate=True)) if item['existed'] else None
                if old is not None and sha(handles.read(old,131072 if item['action']=='write' else 64*1024*1024))!=item['before_hash']:raise ValueError('预览后文件内容已变化')
                recovery=root/'.jiedu-recovery';recovery.mkdir(exist_ok=True)
                stack.enter_context(handles.pin(root,('.jiedu-recovery',)))
                backup=recovery/(preview_id+'.original')
                temp=None
                if item['action']=='write':
                    temp_path=parent/('.jiedu-'+preview_id+'.tmp')
                    temp=stack.enter_context(handles.open(temp_path,mutate=True,create=True));handles.write(temp,item['content'].encode())
                receipt={'change_id':preview_id,'folder_id':item['folder_id'],'action':item['action'],'path':item['path'],
                    'destination':item['destination'],'before_hash':item['before_hash'],'after_hash':sha(item['content'].encode()) if temp else item['before_hash'],
                    'existed':item['existed'],'state':'prepared','recoverable':True,'folder_revision':grant['revision']}
                validate();self._grant(item['folder_id']);save(receipt_path,receipt)
                try:
                    if item['action'] in {'write','delete'} and old is not None:handles.rename(old,backup)
                    validate()
                    if item['action']=='write':handles.rename(temp,destination)
                    elif item['action'] in {'move','rename'}:handles.rename(old,destination)
                    receipt['state']='applied';save(receipt_path,receipt)
                except BaseException:
                    receipt['state']='interrupted';receipt['original_in_recovery']=backup.exists();save(receipt_path,receipt)
                    raise
                return receipt

    def recover(self,change_id,*,validate=lambda:None):
        if not re.fullmatch('[a-f0-9]{32}',change_id):raise ValueError('变更标识无效')
        handles=WindowsHandles()
        with self.lock():
            receipt_path=self.root/'changes'/(change_id+'.json');receipt=json.loads(receipt_path.read_text('utf-8'))
            root,grant=self._grant(receipt['folder_id'])
            self._check_path(root,relative_parts(receipt['path']),True)
            if receipt.get('writer_version'):
                from .writer_core import Writer
                result=Writer(self.context.root,self.context.versions/'formal-memory').undo(receipt['writer_version'],owner_confirmed=True,validate=lambda _:validate())
                receipt['state']='recovered';save(receipt_path,receipt);return {**receipt,'writer_undo':result}
            parts=relative_parts(receipt['path']);dest=relative_parts(receipt['destination']) if receipt['destination'] else None
            with ExitStack() as stack:
                parent=stack.enter_context(handles.pin(root,parts[:-1]));original=parent/parts[-1]
                recovery=root/'.jiedu-recovery';stack.enter_context(handles.pin(root,('.jiedu-recovery',)))
                backup=recovery/(change_id+'.original')
                if receipt['state']=='recovered':return receipt
                if receipt['state'] in {'prepared','interrupted'} and original.exists():
                    with handles.open(original) as handle:
                        if sha(handles.read(handle,64*1024*1024))==receipt['before_hash']:
                            receipt['state']='recovered';save(receipt_path,receipt);return receipt
                current_parent=stack.enter_context(handles.pin(root,dest[:-1])) if dest else parent
                current=current_parent/dest[-1] if dest else original
                validate()
                if receipt['action'] in {'move','rename'}:
                    if original.exists():raise ValueError('原位置已有文件，恢复不会覆盖')
                    with handles.open(current,mutate=True) as handle:
                        if sha(handles.read(handle,64*1024*1024))!=receipt['after_hash']:raise ValueError('移动后文件已变化')
                        validate();handles.rename(handle,original)
                else:
                    if current.exists():
                        with handles.open(current,mutate=True) as handle:
                            if receipt['action']!='write' or sha(handles.read(handle))!=receipt['after_hash']:raise ValueError('保存后文件已变化，恢复不会覆盖')
                            validate();handles.rename(handle,recovery/(change_id+'.replaced'))
                    if receipt['existed']:
                        with handles.open(backup,mutate=True) as handle:
                            if sha(handles.read(handle,64*1024*1024))!=receipt['before_hash']:raise ValueError('恢复备份校验失败')
                            validate();handles.rename(handle,original)
                receipt['state']='recovered';save(receipt_path,receipt);return receipt
