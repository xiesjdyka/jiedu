"""Resolve a saved Wiki citation to verified original words and bounded context."""
import re
from .client_access import resolve_document_target
from .passage_search import file_version

def evidence_context(context,source,candidates):
    base={'source_path':source['path'],'source_id':source['source_id'],'quote':source.get('quote',''),'located':False}
    if not source.get('located'):return base|{'reason':'这条旧记录只有来源链接，没有可核验的消息或范围定位。'}
    candidate=next((r for r in candidates if r.get('candidate_id')==source.get('candidate_id')),None)
    if not candidate or candidate.get('status')!='written':raise ValueError('来源关联已变化，请刷新知识条目')
    ev=candidate['evidence'][source['evidence_index']]
    from .organizer import evidence_checked
    try:checked=evidence_checked(context,[ev])[0]
    except (ValueError,OSError,KeyError):return base|{'reason':'原始来源版本已变化，当前不能确认原话位置。保留已登记引文，可选择打开完整会话核对。','stale':True}
    target=resolve_document_target(context,ev['source_path']);path=context.root/target.relative_path
    start,end=ev['byte_start'],ev['byte_end'];version=file_version(path.stat())
    if ev.get('archive_message_id'):
        # JSONL message evidence is already verified by evidence_checked. Never
        # expose an arbitrary neighboring JSON record as conversational context.
        return base|{'located':True,'message_id':ev['archive_message_id'],'position_type':'message','core':ev['quote'],'before':'','after':'','version':ev['version'],'byte_start':start,'byte_end':end,'context_limited':True,'reason':'已核对原始消息；完整会话可另行打开。'}
    low=max(0,start-16384);high=end+16384
    with path.open('rb') as stream:stream.seek(low);raw=stream.read(high-low)
    if file_version(path.stat())!=version:return base|{'stale':True,'reason':'读取期间来源变化，请刷新后核对。'}
    # Trim only the partial UTF-8 boundary at the beginning/end of the window.
    while raw and raw[0]&0xc0==0x80:raw=raw[1:];low+=1
    prefix=(start-low)
    headings=list(re.finditer(rb'(?m)^## MESSAGE ([^\r\n]+)',raw))
    previous=[m for m in headings if m.start()<=prefix]
    header=previous[-1] if previous else None
    upper=next((m.start() for m in headings if m.start()>end-low),len(raw))
    lower=header.end() if header else prefix
    if not header:upper=end-low
    if upper-lower>12288:lower=max(lower,prefix-2048);upper=min(upper,end-low+2048)
    core=raw[lower:upper].decode('utf-8',errors='ignore').strip()
    before=raw[max(0,lower-2048):lower].decode('utf-8',errors='ignore').strip()
    after=raw[upper:upper+2048].decode('utf-8',errors='ignore').strip()
    message=header[1].decode('utf-8',errors='ignore').split('｜')[0].strip() if header else None
    return base|{'located':True,'core':core,'before':before,'after':after,'message_id':message,'position_type':'message' if message else 'verified_range','version':ev['version'],'byte_start':start,'byte_end':end,'context_limited':True,'chunk_id':f'p1-{version}-{start}'}
