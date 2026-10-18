from __future__ import annotations
import difflib, hashlib, json, os, re, uuid
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

def sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def atomic(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
def save(path,data):atomic(path,json.dumps(data,ensure_ascii=False,indent=2).encode())

class Writer:
    def __init__(self,root:Path,versions:Path):
        self.root=root.resolve();self.versions=versions.resolve()
    def lock(self):
        self.versions.mkdir(parents=True,exist_ok=True)
        return FileLock(str(self.versions/'writer.lock'))
    def target(self,relative):
        p=Path(relative)
        if p.is_absolute() or not p.parts or p.parts[0] not in {'00_系统','01_当前状态','02_项目','03_知识','99_收件箱'} or p.suffix.lower()!='.md' or any(x.startswith('.') or ':' in x for x in p.parts):raise ValueError('目标必须是知识层内的 Markdown 文件')
        path=(self.root/p).resolve()
        if self.root not in path.parents:raise ValueError('目标越界')
        if path.relative_to(self.root).parts[0]!=p.parts[0]:raise ValueError('目标链接不得跨越知识层边界')
        return path
    def preview(self,relative,title,content,evidence=None,supersedes=None,*,action='append',expected_hash=None,context=None):
        from .organizer import reject_protected
        reject_protected(evidence);reject_protected(context or {})
        with self.lock():
            path=self.target(relative);existed=path.exists();raw=path.read_bytes() if existed else b''
            if expected_hash is not None and expected_hash!=(sha(raw) if existed else 'missing'):raise ValueError('目标已变化，请重新读取后提交候选')
            if action not in {'append','create','update','supersede'}:raise ValueError('不支持该写入动作')
            if action=='create' and existed:raise ValueError('创建目标已存在，请读取后提交更新')
            if action in {'update','supersede'} and not existed:raise ValueError('更新目标不存在')
            old=raw.decode('utf-8-sig');new=old.rstrip()+f'\n\n## {title}\n\n{content}\n' if action=='append' else content
            item={'preview_id':uuid.uuid4().hex,'target':str(path),'relative_target':Path(relative).as_posix(),'original':old,'proposed':new,'before_hash':sha(raw),'existed':existed,'action':action,'diff':'\n'.join(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile='old',tofile='new',lineterm='')),'evidence':evidence or [],'supersedes':supersedes or [],'context':context or {}}
            save(self.versions/'previews'/(item['preview_id']+'.json'),item)
            return item
    def saved_preview(self,item):
        key=item.get('preview_id','')
        if not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('请重新生成服务端预览')
        saved=json.loads((self.versions/'previews'/(key+'.json')).read_text('utf-8'))
        if item!=saved:raise ValueError('预览被修改，请重新生成')
        return saved
    def apply(self,preview,owner_confirmed=False,validate=None):
        if not owner_confirmed:raise PermissionError('正式写入需要 Owner 确认')
        with self.lock():
            item=self.saved_preview(preview);path=self.target(item['relative_target'])
            version='v_'+item['preview_id'];manifest=self.versions/version/'manifest.json'
            if manifest.exists():
                meta=json.loads(manifest.read_text('utf-8'))
                if meta.get('state')=='undone':raise ValueError('本预览已撤销，请重新提交')
                if path.exists() and sha(path.read_bytes())==meta['after_hash']:
                    if meta['state']!='applied':meta['state']='applied';save(manifest,meta)
                    return meta
                if meta['state']=='applied':raise ValueError('写入已完成，但目标随后发生变化；不会重复覆盖')
            if validate:validate(item)
            existed=path.exists();before=path.read_bytes() if existed else b''
            if existed!=item['existed'] or sha(before)!=item['before_hash']:raise ValueError('预览后目标已变化，请重新读取并生成预览')
            backup=self.versions/version/item['relative_target'];atomic(backup,before)
            meta={'version':version,'target':item['relative_target'],'before_hash':sha(before),'after_hash':sha(item['proposed'].encode()),'at':datetime.now(timezone.utc).isoformat(),'diff':item['diff'],'evidence':item['evidence'],'existed':existed,'state':'prepared','context':item['context']}
            save(manifest,meta)
            if path.exists()!=existed or (path.exists() and sha(path.read_bytes())!=sha(before)):raise ValueError('写入准备期间目标已变化')
            if validate:validate(item)
            atomic(path,item['proposed'].encode());meta['state']='applied';save(manifest,meta);self._audit('apply',{'version':version,'target':meta['target']})
            return meta
    def undo(self,version,owner_confirmed=False,validate=None):
        if not owner_confirmed:raise PermissionError('撤销需要 Owner 确认')
        if not re.fullmatch('v_[a-f0-9]{12,32}',version):raise ValueError('版本无效')
        with self.lock():
            manifest=self.versions/version/'manifest.json';meta=json.loads(manifest.read_text('utf-8'));path=self.target(meta['target'])
            if meta.get('state')=='undone':return {'undone':True,'version':version,'target':meta['target'],'restored_hash':meta['before_hash'] if meta['existed'] else None}
            if meta.get('state')=='undo_prepared' and ((not meta['existed'] and not path.exists()) or (meta['existed'] and path.exists() and sha(path.read_bytes())==meta['before_hash'])):
                meta['state']='undone';save(manifest,meta)
                return {'undone':True,'version':version,'target':meta['target'],'restored_hash':meta['before_hash'] if meta['existed'] else None}
            if not path.exists() or sha(path.read_bytes())!=meta['after_hash']:raise ValueError('写入后目标已变化，拒绝覆盖用户修改')
            before=(self.versions/version/meta['target']).read_bytes()
            if sha(before)!=meta['before_hash']:raise ValueError('版本备份校验失败')
            if validate:validate(meta)
            meta['state']='undo_prepared';save(manifest,meta)
            if validate:validate(meta)
            if meta.get('existed',True):atomic(path,before)
            else:path.unlink()
            meta['state']='undone';save(manifest,meta)
            result={'undone':True,'restored_hash':sha(before) if meta['existed'] else None,'target':meta['target'],'version':version};self._audit('undo',result);return result
    def _audit(self,action,payload):
        with (self.versions/'audit.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps({'action':action,**payload},ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
