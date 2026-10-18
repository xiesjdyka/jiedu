"""Owner directory picker and the explicitly selected local file ranges."""
import ctypes
import hashlib
import os
from pathlib import Path
from .bounded_files import WindowsHandles,relative_parts

def root_id(path):return hashlib.md5(str(Path(path).absolute()).casefold().encode()).hexdigest()

def local_drives():
    if os.name!='nt':return [Path('/')]
    mask=ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(chr(65+i)+':\\') for i in range(26) if mask&(1<<i) and ctypes.windll.kernel32.GetDriveTypeW(chr(65+i)+':\\') in {2,3,5}]

def check_private_path(path):
    parts=[part.casefold() for part in Path(path).parts]
    if any(part in {'.jiedu','.ssh','.aws','.azure','.gnupg'} or part.startswith('.jiedu-') for part in parts):raise PermissionError('运行数据、保护区与凭据目录不在普通文件范围内')
    text='/'.join(parts)
    if any(marker in text for marker in ['appdata/local/microsoft/credentials','appdata/roaming/microsoft/credentials','appdata/local/microsoft/vault','appdata/roaming/microsoft/vault']):raise PermissionError('Windows 凭据目录不在普通文件范围内')
    if '.codex' in parts and parts[-1] in {'auth.json','config.toml'}:raise PermissionError('AI 连接凭据由对应设置页面管理')

class FileBrowser:
    def __init__(self,context,drives=None):self.context=context;self.drives=drives or local_drives
    def roots(self):
        roots=[self.context.root,*self.drives()]
        return [{'id':root_id(p),'path':str(p),'name':'界渡文件' if p==self.context.root else str(p),'kind':'directory'} for i,p in enumerate(roots) if p not in roots[:i] and p.is_dir()]
    def browse(self,path='',*,kind='folder',offset=0,limit=100):
        roots=self.roots()
        if not path:return {'path':'','parent':None,'items':roots,'total':len(roots),'has_more':False}
        target=Path(path).absolute();root=next((Path(row['path']) for row in roots if target==Path(row['path']) or Path(row['path']) in target.parents),None)
        if root is None:raise ValueError('请选择本机磁盘或界渡文件夹')
        workspace=self.context.root.absolute()
        def check(path):check_private_path(path.relative_to(workspace) if path==workspace or workspace in path.parents else path)
        check(target);parts=relative_parts(target.relative_to(root).as_posix(),True) if target!=root else ()
        rows=[];handles=WindowsHandles()
        with handles.pin(root,parts) as directory:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        check(directory/entry.name)
                        stat=entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(stat,'st_file_attributes',0)&0x400:continue
                        isdir=entry.is_dir(follow_symlinks=False)
                        if not isdir and (kind!='model' or not entry.name.lower().endswith('.gguf')):continue
                        rows.append({'name':entry.name,'path':str(directory/entry.name),'kind':'directory' if isdir else 'file','size_bytes':stat.st_size})
                    except (OSError,PermissionError):continue
        rows.sort(key=lambda row:(row['kind']!='directory',row['name'].casefold()))
        return {'path':str(target),'parent':'' if target==root else str(target.parent),'items':rows[offset:offset+limit],'total':len(rows),'has_more':offset+limit<len(rows),'offset':offset}
