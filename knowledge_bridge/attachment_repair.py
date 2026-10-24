"""Explicit, baseline-bound repair of proven relocated cache references only."""
import hashlib,json
from pathlib import Path
from .archive_store import ArchiveStore,NormalizedAttachment,archive_transaction
from .attachment_cache import cache_transaction,latest_rows,resolve,append_rows

def canonical(row):return hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def verify_messages(context,baseline):
    for relative,info in baseline['original_message_files'].items():
        path=(context.root/relative).resolve()
        if context.raw_message_store.resolve() not in path.parents:raise ValueError('基线文件越过原始消息目录')
        raw=path.read_bytes()
        # New messages may have been appended; the original byte prefix must remain exact.
        if hashlib.sha256(raw[:info['bytes']]).hexdigest()!=info['sha256']:raise ValueError('原始消息文件基线变化，拒绝修复')
    for key,expected in baseline['original_messages'].items():
        rows={str(row['message_id']):canonical(row) for row in ArchiveStore(context,recover=False)._message_rows(context.raw_message_store/(key+'.jsonl'))}
        if any(rows.get(mid)!=digest for mid,digest in expected.items()):raise ValueError('原始消息身份或正文发生变化')

def repair(context,baseline,*,apply=False):
    targets=baseline['targets'];root=context.state/'attachments'
    if not targets:raise ValueError('没有明确修复清单')
    if len({(t['session_key'],t['message_id'],t['source_reference']) for t in targets})!=len(targets):raise ValueError('重复修复目标')
    with archive_transaction(context),cache_transaction(root):
        verify_messages(context,baseline);cache=latest_rows(root/'manifest.jsonl');planned=[]
        for target in targets:
            current=cache.get((target['session_id'],target['message_id'],target['source_reference']))
            if not current or current.get('sha256')!=target['expected_sha256']:raise ValueError('附件账本身份或历史SHA256变化')
            source_reference=current.get('reference');resolved,data=resolve(root,current,with_data=True)
            if data is None or hashlib.sha256(data).hexdigest()!=target['expected_sha256']:raise ValueError('附件字节校验失败')
            expected=(context.root/target['current_cache_relative']).resolve()
            if expected.parent!=root.resolve() or resolved.get('stored_path')!=str(expected) or not expected.is_file():raise ValueError('没有SHA256匹配的当前缓存')
            if resolved['reference']!=source_reference or resolved.get('source_path')!=current.get('source_path'):raise ValueError('原始定位器不得改变')
            store=ArchiveStore(context,recover=False);session=store.session(target['session_key'])
            original=next((r for r in store._message_rows(context.raw_message_store/(target['session_key']+'.jsonl')) if str(r['message_id'])==target['message_id']),None)
            if not original or canonical(original)!=target['message_canonical_sha256']:raise ValueError('目标消息校验失败')
            planned.append((target,resolved,data))
        receipts=[]
        if apply:
            append_rows(root,[row for _,row,_ in planned])
            store=ArchiveStore(context,recover=False)
            for target,row,data in planned:
                result=store.reconcile_attachments(target['session_key'],target['message_id'],[
                    NormalizedAttachment(reference=target['source_reference'],original_name=target['original_name'],inline_data=data)],materialize=False)[0]
                if result['sha256']!=target['expected_sha256'] or result['missing']:raise ValueError('归档后附件校验失败')
                receipts.append({'session_key':target['session_key'],'message_id':target['message_id'],'source_reference':target['source_reference'],
                    'sha256':result['sha256'],'stored_path':result['stored_path'],'status':result['status']})
            for key in {t['session_key'] for t,_,_ in planned}:store.materialize_session(key)
            verify_messages(context,baseline)
        return {'state':'applied' if apply else 'preview','targets':len(planned),'unique_images':len({t['expected_sha256'] for t,_,_ in planned}),
            'sessions':len({t['session_key'] for t,_,_ in planned}),'immutable_baseline_verified':True,
            'changes':[{'session_key':t['session_key'],'session_id':t['session_id'],'message_id':t['message_id'],'source_reference':t['source_reference'],
                'current_cache_relative':t['current_cache_relative'],'expected_sha256':t['expected_sha256']} for t,_,_ in planned],
            'receipts':receipts,'untouched_incomplete_references':baseline.get('untouched_incomplete_references',[])}
