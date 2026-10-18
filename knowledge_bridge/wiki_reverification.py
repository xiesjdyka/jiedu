"""Explicit current-archive location verification, never a Writer evidence repair."""
import hashlib,json,re
from datetime import datetime,timezone
from .client_access import resolve_document_target
from .conversation_visibility import visibility_operation,source_exclusion_reason
from .passage_search import file_version

MAX_ARCHIVE_BYTES=16*1024*1024
MAX_ROWS=20000

def _digest(raw):return hashlib.sha256(raw).hexdigest()

def _read(path,root,limit):
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():raise ValueError('原档路径不在当前工作区内')
    before=path.stat()
    if before.st_size>limit:raise ValueError('此原档超过单次重新核验范围，未自动扫描')
    with path.open('rb') as stream:raw=stream.read(limit+1)
    after=path.stat()
    if len(raw)>limit or len(raw)!=before.st_size or file_version(before)!=file_version(after):raise ValueError('读取期间原档发生变化，请稍后重新核验')
    return raw,file_version(after)

def _rows(raw):
    lines=raw.splitlines()
    if len(lines)>MAX_ROWS:raise ValueError('此原档的消息数超过单次重新核验范围')
    rows=[json.loads(line) for line in lines if line.strip()]
    if any(not isinstance(row,dict) for row in rows):raise ValueError('原档消息格式无法核验')
    return rows

@visibility_operation
def reverify_current(context,source,candidates):
    base={'source_path':source['path'],'source_id':source['source_id'],'quote':source.get('quote',''),'located':False,'verification':'current_reverification','historical_evidence_unchanged':True}
    try:
        candidate=next((c for c in candidates if c.get('candidate_id')==source.get('candidate_id')),None)
        if not candidate or candidate.get('status')!='written':raise ValueError('只重新核验已保存知识的原有引用')
        index=source.get('evidence_index')
        if type(index) is not int or index<0:raise ValueError('没有可重新核验的原有引用')
        ev=candidate['evidence'][index];quote=ev.get('quote');quote_bytes=quote.encode('utf-8') if isinstance(quote,str) else b''
        if not quote_bytes or len(quote_bytes)>8192 or ev.get('quote_sha256')!=_digest(quote_bytes):raise ValueError('旧记录缺少有效的精确引文哈希，不能自动重新定位')
        if source['path']!=ev.get('source_path') or source.get('quote')!=quote:raise ValueError('引用关联发生变化，请刷新知识条目')
        if source_exclusion_reason(context,ev['source_path']):raise ValueError('该来源当前不可用于知识核验')
        target=resolve_document_target(context,ev['source_path']);path=context.root/target.relative_path
        if not path.resolve().is_relative_to(context.raw_archive.resolve()) or path.name!='conversation.md' or path.parent.name!='01_对话':raise ValueError('当前仅支持有稳定消息记录的原始会话')
        metadata=path.parent.parent/'03_元数据'
        inputs={}
        for name,p,limit in [('conversation',path,MAX_ARCHIVE_BYTES),('manifest',metadata/'manifest.json',262144),('messages',metadata/'messages.jsonl',MAX_ARCHIVE_BYTES),('attachments',metadata/'attachments.jsonl',MAX_ARCHIVE_BYTES)]:
            if name=='attachments' and not p.exists():inputs[name]=(p,b'',None,limit)
            else:inputs[name]=(p,*_read(p,context.root,limit),limit)
        raw=inputs['conversation'][1];manifest=json.loads(inputs['manifest'][1]);messages=_rows(inputs['messages'][1]);attachments=_rows(inputs['attachments'][1])
        if not isinstance(manifest,dict) or any(not isinstance(manifest.get(k),str) or not manifest[k] for k in ('session_key','source_key','source_instance','source_session_id')):raise ValueError('原档缺少稳定会话身份，不能自动重新定位')
        if raw.count(quote_bytes)!=1:raise ValueError('引文在当前原档中缺失或有多处匹配，不能自动选择位置')
        start=raw.index(quote_bytes);end=start+len(quote_bytes)
        headings=list(re.finditer(rb'(?m)^## MESSAGE ([^\r\n]+)',raw))
        selected=next((i for i in range(len(headings)-1,-1,-1) if headings[i].end()<=start),None)
        if selected is None or (selected+1<len(headings) and end>headings[selected+1].start()):raise ValueError('引文不在一条可核验的消息正文内')
        ids=[];roles=[]
        for h in headings:
            mid,separator,role=h[1].decode('utf-8').partition('｜')
            if not separator or not mid:raise ValueError('原档消息标题缺少身份')
            ids.append(mid);roles.append(role)
        latest={}
        for item in attachments:latest[(item.get('source_message_id'),item.get('source_reference'))]=item
        by_id={}
        for msg in messages:by_id.setdefault(msg.get('message_id'),[]).append(msg)
        def verified_message(i):
            mid=ids[i];rows=by_id.get(mid,[])
            if ids.count(mid)!=1 or len(rows)!=1:raise ValueError('消息身份存在重复，不能自动选择位置')
            msg=rows[0];body=msg.get('original_text');source_mid=msg.get('source_message_id')
            if not isinstance(source_mid,str) or not source_mid or msg.get('session_key')!=manifest['session_key']:raise ValueError('消息缺少稳定身份或会话身份不一致')
            if sum(r.get('source_message_id')==source_mid for r in messages)!=1:raise ValueError('原始消息身份有多个版本，不能自动选择位置')
            if not isinstance(body,str) or _digest(body.encode('utf-8'))!=msg.get('content_sha256'):raise ValueError('消息正文哈希不一致，无法重新核验')
            if str(msg.get('role','')).upper()!=roles[i]:raise ValueError('消息角色与原档投影不一致')
            message_attachments=[value for (message_id,_),value in latest.items() if message_id==mid] or msg.get('attachments',[])
            expected=body+'\n\n'+'\n'.join(f"- 附件：{a.get('original_name')}（{a.get('sha256') or '缺失'}）" for a in message_attachments)
            actual=raw[headings[i].end():headings[i+1].start() if i+1<len(headings) else len(raw)].decode('utf-8')
            if actual.replace('\r\n','\n').strip()!=expected.replace('\r\n','\n').strip():raise ValueError('消息正文及附件与原档投影不一致')
            return msg
        msg=verified_message(selected);body=msg['original_text']
        if body.count(quote)!=1:raise ValueError('引文在原始消息正文中缺失或不唯一')
        if ev.get('archive_message_id') and ev['archive_message_id']!=msg['message_id']:raise ValueError('当前消息身份与旧证据不一致')
        neighbors={}
        for label,i in [('before',selected-1),('after',selected+1)]:
            if 0<=i<len(headings):
                neighbor=verified_message(i);text=neighbor['original_text'];neighbors[label]={'message_id':neighbor['message_id'],'content_sha256':neighbor['content_sha256'],'text':text[-2048:] if label=='before' else text[:2048]}
        # Re-read the same bounded inputs: even same-stat content replacement must fail.
        for p,original,version,limit in inputs.values():
            if version is None:
                if p.exists():raise ValueError('读取期间附件记录变化，请重新核验')
                continue
            current,current_version=_read(p,context.root,limit)
            if current_version!=version or current!=original:raise ValueError('读取期间原档发生变化，请稍后重新核验')
        pos=body.index(quote);core=body[max(0,pos-2048):pos+len(quote)+2048]
        return base|{'located':True,'reason':'已在当前原档重新核验；不表示旧文件版本一致，旧证据保持不变。','checked_at':datetime.now(timezone.utc).isoformat(),'original_version':ev.get('version'),'version':inputs['conversation'][2],'source_sha256':_digest(raw),'quote_sha256':_digest(quote_bytes),'session_key':manifest['session_key'],'source_session_id':manifest['source_session_id'],'message_id':msg['message_id'],'source_message_id':msg['source_message_id'],'message_content_sha256':msg['content_sha256'],'byte_start':start,'byte_end':end,'core':core,'before':neighbors.get('before',{}).get('text',''),'after':neighbors.get('after',{}).get('text',''),'neighbor_messages':neighbors,'context_limited':True,'position_type':'current_verified_message','chunk_id':f"p1-{inputs['conversation'][2]}-{start}"}
    except (ValueError,OSError,KeyError,IndexError,TypeError,AttributeError) as exc:
        return base|{'reason':str(exc) if isinstance(exc,ValueError) and not isinstance(exc,json.JSONDecodeError) else '原档记录不完整或无法读取，当前不能重新核验。'}
