"""Evidence-bound manual attachment recovery; source messages and locators stay intact."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def review_key(session_key, message_id, reference):
    return (str(session_key), str(message_id), str(reference))


def read_reviews(context):
    path = context.audit / 'attachment-reference-review.jsonl'
    rows = {}
    if path.is_file():
        for line in path.read_text('utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                rows[review_key(row['session_key'], row['message_id'], row['source_reference'])] = row
    return rows


def apply_review(context, plan, *, apply=False):
    from .archive_store import ArchiveStore, NormalizedAttachment, archive_transaction
    from .attachment_repair import canonical
    keys = [review_key(r['session_key'], r['message_id'], r['source_reference']) for r in plan]
    if not plan or len(keys) != len(set(keys)):
        raise ValueError('附件核对清单为空或身份重复')
    with archive_transaction(context):
        store = ArchiveStore(context, recover=False)
        prepared = []
        for r in plan:
            session = store.session(r['session_key'])
            if session['source_session_id'] != r['source_session_id']:
                raise ValueError('来源会话身份变化')
            message = next((m for m in store._message_rows(context.raw_message_store / (r['session_key'] + '.jsonl'))
                            if m['message_id'] == r['message_id']), None)
            if not message or canonical(message) != r['message_canonical_sha256']:
                raise ValueError('原始消息基线变化')
            prior = next((a for a in store.attachment_status(r['session_key'])
                          if a['source_message_id'] == r['message_id'] and a['source_reference'] == r['source_reference']), None)
            if not prior:
                raise ValueError('找不到原始附件引用')
            data = None
            if r['decision'] == 'recover':
                if not r.get('proof'):
                    raise ValueError('没有历史身份凭据')
                data = Path(r['candidate']).read_bytes()
                if hashlib.sha256(data).hexdigest() != r['expected_sha256']:
                    raise ValueError('附件SHA256不符，不能用同名文件替代')
                if not prior.get('missing') and prior.get('sha256') != r['expected_sha256']:
                    raise ValueError('已有原件不同，拒绝覆盖')
            elif r['decision'] != 'unresolved':
                raise ValueError('未知核对结论')
            prepared.append((r, data))
        if not apply:
            return {'state': 'preview', 'reviewed': len(plan), 'recoverable': sum(data is not None for _, data in prepared)}
        receipts = []
        for r, data in prepared:
            if data is None:
                continue
            result = store.reconcile_attachments(r['session_key'], r['message_id'], [NormalizedAttachment(
                reference=r['source_reference'], original_name=r['original_name'], inline_data=data)], materialize=False)[0]
            if result['sha256'] != r['expected_sha256'] or result['missing']:
                raise ValueError('附件落盘校验失败')
            receipts.append(dict(result, session_key=r['session_key']))
        previous = read_reviews(context)
        path = context.audit / 'attachment-reference-review.jsonl'
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            for r in plan:
                key = review_key(r['session_key'], r['message_id'], r['source_reference'])
                old = {k: v for k, v in previous.get(key, {}).items() if k != 'reviewed_at'}
                if old != r:
                    stream.write(json.dumps(dict(r, reviewed_at=datetime.now(timezone.utc).isoformat()), ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        for key in {r['session_key'] for r, data in prepared if data is not None}:
            store.materialize_session(key)
        return {'state': 'applied', 'reviewed': len(plan), 'recovered': len(receipts), 'receipts': receipts}
