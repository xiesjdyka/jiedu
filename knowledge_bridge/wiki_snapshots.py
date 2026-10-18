"""Bounded, content-addressed text snapshots for newly claimed Wiki batches.

Control artifacts only: no mirrored knowledge tree, no automatic eviction of
evidence used by jobs/candidates, and no reconstruction of missing old bytes.
"""
import codecs
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock
from .client_access import resolve_document_target
from .passage_search import file_version
from .writer_core import save

MAX_SOURCE = 16 * 1024 * 1024
MAX_STORE = 256 * 1024 * 1024
MAX_BATCH = 5000

def digest(raw): return hashlib.sha256(raw).hexdigest()

def source_identity(context, relative):
    path=context.root/relative
    for parent in path.parents:
        if parent==context.root:break
        manifest=parent/'03_元数据/manifest.json'
        if manifest.is_file():
            data=json.loads(manifest.read_text('utf-8-sig'))
            return {k:data.get(k) for k in ('source_key','source_instance','source_session_id','session_key')} | {'member':path.relative_to(parent).as_posix()}
    # Imported objects without verified provider identity remain path-bound;
    # they cannot inherit a legacy conversation receipt or a renamed source.
    return {'workspace_id':context.workspace_id,'member':relative,'role':'unclassified_document'}

class WikiSnapshots:
    def __init__(self,context):
        self.context=context;self.root=context.queues/'wiki-snapshots'
    def path(self,key):
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('快照标识无效')
        return self.root/(key+'.blob')
    def capture(self,paths):
        if len(paths)>MAX_BATCH:raise ValueError('本轮资料超过5000项，请限定范围')
        self.root.mkdir(parents=True,exist_ok=True)
        with FileLock(str(self.root/'store.lock')):
            used=sum(p.stat().st_size for p in self.root.glob('*.blob'))
            result={}
            for relative in sorted(paths):
                target=resolve_document_target(self.context,relative)
                if target.scope.value!='evidence':raise ValueError('Wiki快照只接受获准的普通原始资料')
                path=self.context.root/relative;before=file_version(path.stat())
                if path.stat().st_size>MAX_SOURCE:raise ValueError('单份Wiki文本快照超过16MiB，请先分批处理')
                identity=source_identity(self.context,relative)
                with path.open('rb') as stream:raw=stream.read(MAX_SOURCE+1)
                if before!=file_version(path.stat()) or identity!=source_identity(self.context,relative):raise ValueError('冻结期间来源变化，请重试本轮领取')
                if len(raw)>MAX_SOURCE:raise ValueError('单份Wiki文本快照超过16MiB')
                key=digest(raw);blob=self.path(key)
                if blob.exists():
                    if digest(blob.read_bytes())!=key:raise ValueError('已有Wiki快照完整性校验失败')
                else:
                    if used+len(raw)>MAX_STORE:raise ValueError('Wiki快照已达256MiB容量上限；本轮未领取，请先核对历史快照保留范围')
                    # Atomic replacement; failed captures are still bounded by
                    # the global quota and cannot be mistaken for a claimed job.
                    temporary=blob.with_suffix('.tmp')
                    try:temporary.write_bytes(raw);temporary.replace(blob)
                    finally:temporary.unlink(missing_ok=True)
                    used+=len(raw)
                result[relative]={'snapshot_id':key,'source_sha256':key,'version':before,'total_bytes':len(raw),'source_identity':identity,'captured_at':datetime.now(timezone.utc).isoformat()}
            return result
    def raw(self,row):
        path=self.path(row['snapshot_id'])
        if not path.is_file():raise ValueError('原始快照缺失，无法恢复该历史字节；不能用当前正文替代')
        if path.stat().st_size>MAX_SOURCE:raise ValueError('快照超过读取上限')
        with path.open('rb') as stream:raw=stream.read(MAX_SOURCE+1)
        if len(raw)!=row['total_bytes'] or digest(raw)!=row['snapshot_id'] or row.get('source_sha256')!=row['snapshot_id']:raise ValueError('Wiki快照完整性校验失败')
        return raw
    def page(self,row,offset,page_bytes,job_id):
        raw=self.raw(row)
        if type(offset) is not int or not 0<=offset<=len(raw):raise ValueError('快照读取位置无效')
        chunk=raw[offset:offset+page_bytes]
        decoder=codecs.getincrementaldecoder('utf-8-sig' if offset==0 else 'utf-8')('strict')
        try:content=decoder.decode(chunk,final=offset+len(chunk)==len(raw))
        except UnicodeError:raise ValueError('快照正文不是有效UTF-8文本，未登记完整读取') from None
        consumed=len(chunk)-len(decoder.getstate()[0])
        if chunk and not consumed:raise ValueError('单页预算不足以读取一个字符')
        end=offset+consumed
        return {'source_path':row['source_path'],'scope':'evidence','version':row['version'],'snapshot_id':row['snapshot_id'],'snapshot_job_id':job_id,'source_sha256':row['source_sha256'],'source_identity':row['source_identity'],'captured_at':row['captured_at'],'content':content,'offset':offset,'offset_unit':'source_bytes','returned_bytes':consumed,'total_bytes':len(raw),'next_offset':end if end<len(raw) else None,'complete':end==len(raw),'encoding':'utf-8','verification':'immutable_batch_snapshot','notice':'本轮截止时的原始资料快照；新增内容留待下一轮。引用需携带 snapshot_job_id 和 snapshot_id；正式写入前仍核验当前来源。'}
    def evidence_row(self,ev):
        from .organizer import Organizer
        if not ev.get('snapshot_id') or not ev.get('snapshot_job_id'):
            raise ValueError('历史快照引用必须同时使用读取结果中的 snapshot_id 和 snapshot_job_id；普通当前版本来源请保留顶层 job_id，evidence 不传这两个字段。未降级或跳过验证。')
        service=Organizer(self.context)
        job=json.loads(service.path(ev.get('snapshot_job_id')).read_text('utf-8'))
        if job.get('snapshot_schema')!=1:raise ValueError('任务没有可验证历史快照：这是当前版本来源任务。请按 organization_read 返回的 evidence_mode 引用，保留顶层 job_id，evidence 不要添加 snapshot_id/snapshot_job_id；原进度保留。')
        row=next((r for r in job['sources'] if r['source_path']==ev['source_path'] and r.get('snapshot_id')==ev.get('snapshot_id') and r['version']==ev['version']),None)
        if not row or row['offset']<ev['byte_end']:raise ValueError('快照引用尚未通过任务实际交付')
        return row,self.raw(row)
    def verify_current(self,row):
        target=resolve_document_target(self.context,row['source_path'])
        path=self.context.root/target.relative_path
        before=file_version(path.stat())
        if path.stat().st_size>MAX_SOURCE:raise ValueError('来源已超出快照校验范围')
        with path.open('rb') as stream:raw=stream.read(MAX_SOURCE+1)
        if before!=file_version(path.stat()) or digest(raw)!=row['source_sha256'] or source_identity(self.context,row['source_path'])!=row['source_identity']:
            raise ValueError('快照之后来源已更新；候选保留，请在下一轮核对新来源后重新提交，不能作为当前事实写入')
        return raw
