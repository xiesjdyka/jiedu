"""Owner-selected source bindings and append-only, authenticated message intake."""
from __future__ import annotations

import hashlib
import base64
import json
import re
from pathlib import Path
from filelock import FileLock
from .archive_store import ArchiveStore, SourceRegistry, NormalizedMessage, NormalizedAttachment, _atomic_json, _safe_title


class SourceConnections:
    def __init__(self, context):
        self.context = context
        self.path = context.state / 'source-connections.json'
        self.lock = FileLock(str(context.state / 'source-connections.lock'))

    def load(self):
        if not self.path.exists():
            return {'connections': {}}
        return json.loads(self.path.read_text('utf-8'))

    @staticmethod
    def descriptor(client_type, product_family=None):
        if client_type in SourceRegistry.BUILTINS:
            family, label, kind = SourceRegistry.BUILTINS[client_type]
            return {'source_key': client_type, 'product_family': family, 'session_source': label, 'client_type': kind}
        family = (product_family or '自定义AI').strip()
        if not family or _safe_title(family) != family:
            raise ValueError('产品名称不能含路径分隔符或非法目录字符')
        key = 'custom_' + hashlib.sha256(family.encode()).hexdigest()[:16]
        return {'source_key': key, 'product_family': family, 'session_source': family, 'client_type': 'external_client'}

    def bind(self, internal_id, descriptor):
        with self.lock:
            data = self.load()
            existing = data['connections'].get(internal_id)
            if existing and existing != descriptor:
                raise ValueError('已绑定来源不能改为另一个产品')
            data['connections'][internal_id] = descriptor
            _atomic_json(self.path, data)
        return descriptor

    def status(self, internal_id):
        descriptor = self.load()['connections'].get(internal_id)
        if not descriptor:
            return {'bound': False, 'collection_status': '尚未绑定归档来源', 'history_coverage': '未知'}
        registry = json.loads(self.context.archive_registry.read_text('utf-8')) if self.context.archive_registry.exists() else {'sessions': {}}
        rows = [r for r in registry['sessions'].values() if r.get('source_instance') == internal_id]
        local_doubao = descriptor.get('source_key') == 'doubao_work'
        return {**descriptor, 'bound': True,
                'archive_path': '90_原始档案/' + descriptor['product_family'],
                'session_count': len(rows), 'message_count': sum(r.get('message_count', 0) for r in rows),
                'last_archived_at': max((r.get('updated_at', '') for r in rows), default=None),
                'collection_status': ('已发现本机结构化缓存；正文尚未通过真实样本验证' if local_doubao and rows else
                                      '已发现豆包本机来源；等待首条落盘记录' if local_doubao else
                                      '已收到客户端推送' if rows else '尚未归档；等待客户端推送或导入'),
                'history_coverage': ('结构化缓存局部覆盖；源端已清理历史不猜测补全' if local_doubao else
                                     '仅已收到的消息；源端历史总量未知')}

    def append(self, identity, permissions, payload):
        from .organizer import reject_protected
        reject_protected(payload)
        if not permissions.registry.identity_current(identity):
            raise PermissionError('客户端身份已失效')
        descriptor = self.load()['connections'].get(identity.internal_id)
        if not descriptor:
            raise PermissionError('请由Owner先绑定该连接的产品来源')
        selector = {'source_key': descriptor['source_key'], 'source_instance': identity.internal_id}
        decision = permissions.authorize(identity, 'evidence', 'raw_archive', selector, payload.get('approval_token'))
        if decision.outcome.value != 'allow':
            raise PermissionError('归档已暂停或需要Owner确认')
        for field in ('conversation_id', 'message_id', 'role', 'content'):
            if not isinstance(payload.get(field), str) or (field != 'content' and not payload[field].strip()):
                raise ValueError('缺少有效字段：' + field)
        if payload['role'] not in {'user', 'assistant', 'system', 'tool'}:
            raise ValueError('消息角色无效')
        # Untrusted clients cannot choose another product, identity or local attachment file.
        if payload.get('attachment_paths') or payload.get('source_key') or payload.get('source_instance'):
            raise ValueError('来源由Owner绑定，附件不能请求读取任意本机文件')
        if len(payload['content'].encode()) > 512 * 1024:
            raise ValueError('单条消息超过512 KiB')
        specs=[];total=0
        for item in payload.get('attachments') or []:
            if not isinstance(item,dict) or not item.get('reference') or not item.get('original_name'):
                raise ValueError('附件需要明确的消息引用和原始名称')
            raw=base64.b64decode(item['data_base64'],validate=True) if item.get('data_base64') else None
            total+=len(raw or b'')
            if total>8*1024*1024:raise ValueError('单次附件最多8 MiB，请分次追加')
            if raw is not None and item.get('sha256') and hashlib.sha256(raw).hexdigest()!=item['sha256']:
                raise ValueError('附件校验不一致')
            specs.append(NormalizedAttachment(reference=str(item['reference']),original_name=str(item['original_name']),
                inline_data=raw,mime_type=item.get('mime_type')))
        for reference in payload.get('attachment_refs') or []:
            if reference not in {s.reference for s in specs}:
                specs.append(NormalizedAttachment(reference=str(reference),original_name=Path(str(reference)).name or '附件'))
        supplied_provenance=payload.get('provenance') or {}
        if not isinstance(supplied_provenance,dict):raise ValueError('provenance格式无效')
        capture=supplied_provenance.get('capture')
        history_status=supplied_provenance.get('history_status')
        session_digest=supplied_provenance.get('openai_session_digest')
        if capture not in {None,'model_supplied_visible_verbatim','explicit_visible_message'}:
            raise ValueError('capture来源标记无效')
        if history_status not in {None,'complete_visible_history','incremental_since_confirmed_cursor','partial_visible_history'}:
            raise ValueError('history_status来源标记无效')
        if session_digest is not None and not re.fullmatch(r'[0-9a-f]{20}',str(session_digest)):
            raise ValueError('openai_session_digest格式无效')
        client_provenance={k:v for k,v in {
            'capture':capture,'history_status':history_status,
            'openai_session_digest':str(session_digest) if session_digest is not None else None
        }.items() if v is not None}
        with self.lock:
            if not permissions.registry.identity_current(identity):
                raise PermissionError('客户端身份已失效')
            store = ArchiveStore(self.context, recover=False)
            current=next((r for r in store._registry().get('sessions',{}).values()
                          if r.get('source_instance')==identity.internal_id and r.get('source_session_id')==payload['conversation_id']),{})
            session = store.ensure_session(**descriptor, source_session_id=payload['conversation_id'],
                source_instance=identity.internal_id, title=payload.get('title') or current.get('title'), acquisition_method='authenticated_push')
            result = store.append_normalized_message(session['session_key'], NormalizedMessage(message_id=payload['message_id'],
                role=payload['role'], content=payload['content'], timestamp=payload.get('timestamp'),
                sync_position={'byte_offset': payload['source_order']} if isinstance(payload.get('source_order'), int) and payload['source_order'] >= 0 else None,
                attachments=tuple(specs),
                provenance={'acquisition': 'authenticated_push', 'source_client': identity.internal_id,
                            'attachment_references': payload.get('attachment_refs') or [],
                            'attachment_coverage': 'per_attachment_status',
                            'history_coverage': history_status or 'unknown',
                            'client_claim': client_provenance}))
            if client_provenance:
                # An authenticated push proves only what the client actually submitted.  It must
                # never make a call-driven archive look like a complete source transcript.
                completeness = ('已同步所提交消息；可见历史由客户端声明完整，当前助手尾部仍可能待下次调用同步'
                                if history_status == 'complete_visible_history' else
                                '已同步所提交消息；完整历史未知，当前助手尾部待下次调用同步')
                store.update_session(session['session_key'], sync_status='submitted_messages_synced',
                                     metadata={'completeness': completeness,
                                               'history_coverage': history_status or 'unknown'})
            return {'saved': result['saved'], 'duplicate': result['duplicate'], 'session_key': session['session_key'],
                    'archive_path': result['session']['relative_path'], 'attachment_coverage': store.attachment_status(session['session_key'])}
