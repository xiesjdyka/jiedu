"""Public scoped sources reuse the version-pinned ObjectReader page contract."""
import hashlib,re,threading,json
from pathlib import Path
from .client_access import resolve_document_target
from .knowledge_objects import KnowledgeObjectService
from .object_reader import ObjectReader
from .passage_search import metadata

_services={};_lock=threading.Lock()
def generated_header(context,relative,head):
    """Recognize only import headers backed by the object's saved provenance.

    This describes generated metadata, not accuracy of converted/OCR content.
    Original bytes and reader positions always remain authoritative.
    """
    try:
        registry=json.loads((context.state/'knowledge-objects.json').read_text('utf-8'))
        registered=(registry.get('open_metadata') or {}).get(relative) or {}
        if registered.get('source_kind')!='open_derived':return None
        text=head.decode('utf-8',errors='replace')
        match=re.match(r'\A---\n(jiedu_derived: true\nsource_path: [^\n]+\nsource_sha256: [0-9a-f]{64}\nsource_entry_id: open-[0-9a-f]{24}\nconverter: [^\n]+\nsource_format: [^\n]+\nconversion_metadata: [^\n]+\n)---\n\n',text)
        if not match:return None
        fields=dict(line.split(': ',1) for line in match[1].splitlines())
        original=json.loads(fields['source_path']);target=resolve_document_target(context,original)
        source_id='open-'+hashlib.sha256(target.relative_path.casefold().encode()).hexdigest()[:24]
        conversion=registered.get('conversion') or {}
        original_meta=(registry.get('open_metadata') or {}).get(target.relative_path) or {}
        derived_id='open-'+hashlib.sha256(relative.casefold().encode()).hexdigest()[:24]
        if target.relative_path!=original or source_id!=fields['source_entry_id'] or registered.get('source_entry_id')!=source_id:return None
        if registered.get('source_sha256')!=fields['source_sha256'] or derived_id not in original_meta.get('derived_entry_ids',[]):return None
        if conversion.get('converter')!=fields['converter'] or conversion.get('source_format')!=fields['source_format'] or conversion.get('metadata')!=json.loads(fields['conversion_metadata']):return None
        raw=match[0].encode('utf-8')
        ocr_metadata=conversion.get('metadata') or {}
        return {'header':match[0],'body_byte_start':len(raw),'body_line_start':match[0].count('\n')+1,'original_path':original,'title':Path(original).stem+' · 导入正文','verification':'registered_import_metadata','content_accuracy':'requires_review','ocr_presentation':bool(isinstance(ocr_metadata,dict) and ocr_metadata.get('automatic_ocr'))}
    except (OSError,ValueError,KeyError,TypeError,UnicodeError):return None
class ScopedSourceService:
    """Authorization stays at the endpoint; evidence never enters business catalog."""
    _public_protected_item=staticmethod(KnowledgeObjectService._public_protected_item)
    def __init__(self,context):self.context=context;self.reader=ObjectReader(self)
    def _workspace_relative(self,source):
        target=resolve_document_target(self.context,source)
        return target.relative_path,self.context.root/target.relative_path

def read_source(context,source_path,chunk_id=None,*,cursor=None,page_bytes=8192,binding='public'):
    target=resolve_document_target(context,source_path,chunk_id)
    path=context.root/target.relative_path
    if path.suffix.casefold() not in {'.md','.txt'}:raise ValueError('请使用原件字节或关联派生正文入口')
    with _lock:
        key=str(context.root.resolve())
        if key not in _services:_services[key]=ScopedSourceService(context)
        service=_services[key]
    relative=target.relative_path
    row={'entry_id':'open-'+hashlib.sha256(relative.casefold().encode()).hexdigest()[:24],'policy':'open','logical_path':relative,'title':path.name,'readable':True}
    reader=service.reader
    if chunk_id and not cursor:
        match=re.fullmatch(r'p1-([0-9a-f]{64})-(\d+)',chunk_id)
        if not match:raise ValueError('段落定位已失效，请重新检索；已知文档可不传 chunk_id 从头分页')
        # The search position points to a UTF-8 boundary, and the stat-based
        # version is checked before returning any bytes. Cursor binds caller.
        cursor=reader._seal({'entry':row['entry_id'],'binding':binding,'mode':'text','offset':int(match[2]),'version':match[1],'encoding':'utf-8'})
    if not reader.slots.acquire(blocking=False):raise ValueError('读取并发已满，请稍后重试')
    try:
        head=reader._raw(row,0,16384,None)
        meta,_=metadata(head['data'].decode('utf-8',errors='ignore'))
        for name in ('source_entry_id','source_sha256'):row[name]=meta.get(name)
        result=reader.page(row,mode='text',cursor=cursor,version=head['version'],binding=binding,page_bytes=page_bytes)
        result.update(source_path=relative,scope=target.scope.value,chunks=[],read_started_at_hit=bool(chunk_id),original_path=None,
                      notice='这是有界正文片段；从命中位置展开不代表已读全文。可从头读取或使用 next_cursor 继续。')
        original=meta.get('source_path')
        if original:
            try:
                linked=resolve_document_target(context,original)
                if linked.scope==target.scope:result['original_path']=linked.relative_path
            except ValueError:pass
        result['verification']='unconfirmed_derived' if meta.get('jiedu_derived')=='true' else 'raw_evidence' if target.scope.value=='evidence' else 'source_statement'
        generated=generated_header(context,relative,head['data'])
        if generated:
            result['generated_metadata']=generated
            # Presentation only. Raw content, returned bytes, hit/cursor offsets
            # and physical line locations are intentionally unchanged.
            raw_content=result['content'].encode('utf-8')
            hidden=max(0,generated['body_byte_start']-result['offset'])
            result['display_content']=raw_content[min(hidden,len(raw_content)):].decode('utf-8')
        return result
    finally:reader.slots.release()
