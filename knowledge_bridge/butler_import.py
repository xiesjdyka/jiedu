"""Selected-file snapshots through the existing object import service."""
import json,uuid
from contextlib import ExitStack
from pathlib import Path
from .bounded_files import WindowsHandles,relative_parts
from .writer_core import save,sha

class ButlerImports:
    def __init__(self,context,folders,objects):
        self.context=context;self.folders=folders;self.objects=objects
        self.root=context.cache/'butler-import'
        self.records=context.state/'local-butler'/'import-previews'

    def saved(self,preview_id):
        if len(preview_id)!=32 or any(c not in '0123456789abcdef' for c in preview_id):raise ValueError('预览无效')
        return json.loads((self.records/(preview_id+'.json')).read_text('utf-8'))

    def public(self,row):
        preview=row['preview']
        return {'preview_id':preview['preview_id'],'folder_id':row['folder_id'],'folder_revision':row['folder_revision'],
            'items':[{k:item.get(k) for k in ('relative_path','logical_path','sha256','size_bytes','policy','conflict','idempotent','conversion','derived_path')} for item in preview['items']],
            'counts':preview['counts'],'conflicts':preview['conflicts'],'total_bytes':preview['total_bytes'],
            'notice':'导入读取了指定文件的版本快照，执行前重新核验授权和原件哈希；原件不改动。派生转换使用已有本机能力。'}

    def preview(self,args,check):
        handles=WindowsHandles();paths=args['paths']
        if len(set(paths))!=len(paths):raise ValueError('文件清单不能重复')
        snapshot=self.root/uuid.uuid4().hex/('selected-'+args['folder_id'][:12]);snapshot.mkdir(parents=True)
        evidence=[];total=0
        with self.folders.lock():
            source,grant=self.folders._grant(args['folder_id'])
            revision=grant['revision']
            for value in paths:
                check();parts=relative_parts(value)
                with handles.pin(source,parts[:-1]) as parent,handles.open(parent/parts[-1]) as leaf:
                    raw=handles.read(leaf,64*1024*1024-total)
                total+=len(raw)
                destination=snapshot.joinpath(*parts);destination.parent.mkdir(parents=True,exist_ok=True)
                with handles.open(destination,mutate=True,create=True) as output:handles.write(output,raw)
                evidence.append({'path':value,'sha256':sha(raw),'size_bytes':len(raw)})
            self.folders._grant(args['folder_id']);check()
        # All snapshot leaves and ancestors stay pinned while converters inspect them.
        with self.pin(snapshot,evidence):
            preview=self.objects.preview_import(str(snapshot),default_policy='open',_trusted_snapshot=True)
        row={'preview':preview,'folder_id':args['folder_id'],'folder_revision':revision,'evidence':evidence}
        save(self.records/(preview['preview_id']+'.json'),row)
        return self.public(row)

    def pin(self,snapshot,evidence):
        handles=WindowsHandles();stack=ExitStack()
        try:
            for row in evidence:
                parts=relative_parts(row['path']);parent=stack.enter_context(handles.pin(snapshot,parts[:-1]))
                leaf=stack.enter_context(handles.open(parent/parts[-1]))
                if sha(handles.read(leaf,64*1024*1024))!=row['sha256']:raise ValueError('快照已变化')
        except BaseException:stack.close();raise
        return stack

    def apply(self,args,check):
        row=self.saved(args['preview_id']);preview=row['preview'];handles=WindowsHandles()
        with self.folders.lock():
            root,grant=self.folders._grant(row['folder_id'])
            if grant['revision']!=row['folder_revision']:raise PermissionError('原文件夹授权已变化')
            with self.pin(root,row['evidence']),self.pin(Path(preview['source_root']),row['evidence']):
                check()
                def canceled():check();return check.cancel.is_set()
                result=self.objects.execute_import(args['preview_id'],owner_confirmed=True,cancel=canceled)
                check()
        return {'folder_id':row['folder_id'],'folder_revision':row['folder_revision'],'result':result}
