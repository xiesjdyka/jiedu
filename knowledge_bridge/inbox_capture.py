"""Explicitly save a reference to one archived message, never a second transcript."""
import hashlib
import json
from pathlib import Path
from filelock import FileLock
from .archive_store import ArchiveStore, _atomic_json, _now
from .conversation_visibility import knowledge_message


def capture(context, session_key, message_id, note=''):
    if not isinstance(note,str) or len(note.encode())>8192:
        raise ValueError('保存说明最多8 KiB')
    store=ArchiveStore(context,recover=False)
    from .legacy_session_adoption import resolve_session,resolve_message
    session_key=resolve_session(context,session_key);message_id=resolve_message(context,session_key,message_id)
    try:
        session=store.session(session_key)
        projected=context.root/session['relative_path']/'03_元数据/messages.jsonl'
        rows=store._message_rows(projected if projected.is_file() else context.raw_message_store/(session_key+'.jsonl'))
        attachments=store.attachment_status(session_key)
    except ValueError:
        from .session_archive import _find_canonical
        legacy=_find_canonical(session_key)
        if not legacy:raise ValueError('请先归档该原始会话')
        relative=context.relative(Path(legacy['archive_path']))
        session={'product_family':relative.split('/')[1],'source_session_id':session_key,'relative_path':relative}
        rows=legacy['messages'];attachments=[]
    message=next((r for r in rows if r['message_id']==message_id),None)
    if not message:raise ValueError('请先归档该原始消息，再保存到收件箱')
    if not knowledge_message(message):
        raise ValueError('系统通知、内部消息和工具输出不作为用户线索；请在原会话查看')
    message_hash=message.get('content_sha256') or hashlib.sha256(message.get('original_text',message.get('content','')).encode()).hexdigest()
    key=hashlib.sha256((session_key+'\0'+message_id).encode()).hexdigest()
    folder=context.root/'99_收件箱'/'已保存线索'
    lock=FileLock(str(context.state/'inbox-capture.lock'))
    with lock:
        index=context.state/'inbox-captures.json'
        data=json.loads(index.read_text('utf-8')) if index.exists() else {'items':{}}
        existing=data['items'].get(key)
        if existing:return existing|{'duplicate':True}
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(key[:24]+'.md')
        record={'capture_id':key,'source_product':session['product_family'],'session_key':session_key,
                'source_session_id':session['source_session_id'],'message_id':message_id,
                'source_timestamp':message.get('timestamp'),'saved_at':_now(),'status':'待处理',
                'attachments':[a for a in attachments if a.get('source_message_id')==message_id] or message.get('attachments',[]),'source_hash':message_hash,
                'path':context.relative(path),'note':note}
        source=session['relative_path']+'/01_对话/conversation.md'
        body=f"# 已保存线索\n\n状态：待处理\n\n来源产品：{session['product_family']}\n\n原始会话：[[{source}]]\n\n稳定会话：{session_key}\n消息：{message_id}\n来源时间：{message.get('timestamp') or '源端未提供'}\n\n## 保存说明\n\n{note or '用户明确保存，尚未整理。'}\n\n原文与附件仍保存在原始会话；此条仅保存定位关系，不代表正式知识。\n"
        if path.exists() and path.read_text('utf-8')!=body:
            raise ValueError('保存位置已有不同内容，请核对，未覆盖')
        if not path.exists():path.write_text(body,'utf-8')
        data['items'][key]=record;_atomic_json(index,data)
        return record|{'duplicate':False}
