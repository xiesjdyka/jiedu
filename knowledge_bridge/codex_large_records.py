"""Bounded JSON validation for large internal Codex context snapshots.

Only a fully validated record with one known top-level type may be omitted from
the public-message projection. Parser state contains no payload text and can be
checkpointed between worker ticks. Public/unknown records are never skipped.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import re
from dataclasses import dataclass

CHUNK = 64 * 1024
STRING_SPECIAL = re.compile(r'["\\\x00-\x1f]')
NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')


@dataclass(frozen=True)
class StreamedInternal:
    event_type: str
    fingerprint: str


def new_parser():
    return {'stack': [], 'started': False, 'done': False, 'mode': None,
            'type_count': 0, 'event_type': None, 'payload_count':0,'payload_type_count':0,'payload_type':None,'utf8_tail': ''}


def internal_prefix(raw):
    """Choose the streaming path early; this is NOT permission to skip a row."""
    state = new_parser()
    try:
        feed(state, raw)
    except (ValueError, UnicodeError):
        return False
    return known_internal(state)


def known_internal(state):
    from .codex_source_adapter import KNOWN_TOP_LEVEL_INTERNAL,KNOWN_RESPONSE_INTERNAL
    if state.get('type_count')!=1:return False
    if state.get('event_type') in KNOWN_TOP_LEVEL_INTERNAL:return True
    return state.get('event_type')=='response_item' and state.get('payload_count')==1 and state.get('payload_type_count')==1 and state.get('payload_type') in KNOWN_RESPONSE_INTERNAL


def feed(state, raw, *, final=False):
    """Validate JSON incrementally; retain only root field names and type."""
    for name,default in [('payload_count',0),('payload_type_count',0),('payload_type',None)]:state.setdefault(name,default)
    decoder = codecs.getincrementaldecoder('utf-8')()
    decoder.setstate((bytes.fromhex(state['utf8_tail']), 0))
    text = decoder.decode(raw, final=final)
    state['utf8_tail'] = decoder.getstate()[0].hex()
    at = 0

    def capture(part):
        if state.get('capture') is not None:
            state['capture'] += part
            if len(state['capture']) > 1024:
                state['capture'] = None

    def value_started():
        if state['stack']:
            parent = state['stack'][-1]
            if len(state['stack']) == 1 and parent.get('key') == 'type':
                state['type_count'] += 1
            if len(state['stack'])==1 and parent.get('key')=='payload':state['payload_count']+=1
            if len(state['stack'])==2 and parent.get('is_payload') and parent.get('key')=='type':state['payload_type_count']+=1
            parent['phase'] = 'comma'
        else:
            state['started'] = True

    while at < len(text):
        mode = state['mode']
        if mode == 'string':
            if state.get('unicode_left'):
                char = text[at]
                if char not in '0123456789abcdefABCDEF': raise ValueError('无效 JSON Unicode 转义')
                capture(char); at += 1; state['unicode_left'] -= 1
                continue
            if state.get('escape'):
                char = text[at]
                if char not in '"\\/bfnrtu': raise ValueError('无效 JSON 字符串转义')
                capture(char); at += 1; state['escape'] = False
                if char == 'u': state['unicode_left'] = 4
                continue
            match = STRING_SPECIAL.search(text, at)
            if not match:
                capture(text[at:]); at = len(text); continue
            capture(text[at:match.start()]); at = match.end(); char = match.group()
            if char == '\\':
                capture(char); state['escape'] = True; continue
            if char != '"': raise ValueError('JSON 字符串含未转义控制字符')
            value = json.loads('"' + state['capture'] + '"') if state.get('capture') is not None else None
            if state['string_role'] == 'key':
                state['stack'][-1].update(key=value, phase='colon')
            elif state['string_role'] == 'type':
                state['event_type'] = value
            elif state['string_role']=='payload_type':state['payload_type']=value
            state['mode'] = None
            continue
        if mode == 'primitive':
            end = at
            while end < len(text) and text[end] not in ' \t\r\n,]}': end += 1
            state['token'] += text[at:end]; at = end
            if len(state['token']) > 128: raise ValueError('JSON 数值或常量格式无法确认')
            if at == len(text) and not final: break
            token = state['token']
            if token not in ('true', 'false', 'null') and not NUMBER.fullmatch(token):
                raise ValueError('无效 JSON 数值或常量')
            state['mode'] = None
            continue
        char = text[at]
        if char in ' \r\n\t': at += 1; continue
        if state['done']: raise ValueError('JSON 根对象之后存在额外内容')
        if not state['stack']:
            if state['started'] or char != '{': raise ValueError('Codex 记录必须是 JSON 对象')
            state['started'] = True; state['stack'].append({'kind':'object', 'phase':'key_or_end'})
            at += 1; continue
        parent = state['stack'][-1]; phase = parent['phase']; kind = parent['kind']
        close = '}' if kind == 'object' else ']'
        if phase in ('comma', 'key_or_end', 'value_or_end') and char == close:
            state['stack'].pop(); at += 1
            if not state['stack']: state['done'] = True
            continue
        if phase == 'comma':
            if char != ',': raise ValueError('JSON 项目之间缺少分隔符')
            parent['phase'] = 'key' if kind == 'object' else 'value'; at += 1; continue
        if phase == 'colon':
            if char != ':': raise ValueError('JSON 字段缺少冒号')
            parent['phase'] = 'value'; at += 1; continue
        if phase in ('key', 'key_or_end'):
            if char != '"': raise ValueError('JSON 对象字段名无效')
            state.update(mode='string', string_role='key', capture='' if len(state['stack']) == 1 or (len(state['stack']) == 2 and parent.get('is_payload')) else None,
                         escape=False, unicode_left=0)
            at += 1; continue
        if phase not in ('value', 'value_or_end'): raise ValueError('JSON 状态无效')
        type_value = len(state['stack']) == 1 and parent.get('key') == 'type'
        payload_type_value=len(state['stack'])==2 and parent.get('is_payload') and parent.get('key')=='type'
        payload_object=len(state['stack'])==1 and parent.get('key')=='payload'
        value_started()
        if char in '{[':
            if len(state['stack']) >= 256: raise ValueError('JSON 嵌套过深，无法确认内部记录')
            state['stack'].append({'kind':'object' if char=='{' else 'array',
                                   'phase':'key_or_end' if char=='{' else 'value_or_end','is_payload':payload_object and char=='{'})
            at += 1
        elif char == '"':
            state.update(mode='string', string_role='type' if type_value else 'payload_type' if payload_type_value else 'value',
                         capture='' if type_value or payload_type_value else None, escape=False, unicode_left=0)
            at += 1
        else:
            state.update(mode='primitive', token='')
    if final and (not state['done'] or state['mode'] or state['stack'] or state['utf8_tail']):
        raise ValueError('JSON 记录尚未完整')


def fingerprint(handle, start, end, *, parser=None):
    """Same SHA-256 as raw.rstrip(CR/LF), without holding the record in RAM."""
    handle.seek(start); remaining = end-start; digest = hashlib.sha256(); trailing = 0
    while remaining:
        block = handle.read(min(CHUNK, remaining))
        if not block: raise ValueError('来源读取中缩短')
        remaining -= len(block)
        if parser is not None:
            feed(parser, block, final=remaining == 0)
        clean = block.rstrip(b'\r\n')
        if clean:
            while trailing:
                take = min(CHUNK, trailing); digest.update(b'\r'*take); trailing -= take
            digest.update(clean)
        # Within one JSONL line, any withheld bytes before the LF are CRs.
        trailing += len(block)-len(clean)
    return digest.hexdigest()


def scan(adapter, handle, source, start, target):
    """Read one fixed-size slice of a large record; commit only on valid LF."""
    cursor = adapter._work_cursor if adapter._work_cursor is not None else {}
    state = cursor.get('large_record')
    if not state or state['start'] != start or state['object_id'] != source.object_id:
        state = {'start':start, 'scan_offset':start, 'object_id':source.object_id,
                 'parser':new_parser()}
        cursor['large_record'] = state
    if state.get('error'):
        if state.get('error_mtime_ns') == source.mtime_ns:
            raise ValueError(state['error'])
        cursor.pop('large_record', None)
        return scan(adapter, handle, source, start, target)
    if state.get('guard_start') is not None:
        handle.seek(state['guard_start'])
        guard = handle.read(state['scan_offset']-state['guard_start'])
        if hashlib.sha256(guard).hexdigest() != state['guard_sha256']:
            cursor.pop('large_record', None)
            raise ValueError('大记录续读位置发生变化，保留原确认游标待重试')
    handle.seek(state['scan_offset'])
    budget = adapter.stream_page_bytes
    while budget > 0 and state['scan_offset'] < target:
        block_start = handle.tell()
        block = handle.readline(min(CHUNK, budget, target-state['scan_offset']))
        if not block: raise ValueError('大记录读取中来源缩短')
        final = block.endswith(b'\n')
        try:
            feed(state['parser'], block, final=final)
        except (ValueError, UnicodeError) as exc:
            state.update(error='大记录 JSON 未通过完整性验证：'+str(exc), error_mtime_ns=source.mtime_ns)
            raise ValueError(state['error']) from exc
        state.update(scan_offset=block_start+len(block), guard_start=block_start,
                     guard_sha256=hashlib.sha256(block).hexdigest())
        budget -= len(block)
        if final:
            end = state['scan_offset']
            # Revalidate the complete record in the same bounded pass used for
            # its final hash. A stale checkpoint must not classify rewritten
            # earlier bytes as an internal event solely from its cached type.
            fresh = new_parser()
            try:
                digest = fingerprint(handle, start, end, parser=fresh)
            except (ValueError, UnicodeError) as exc:
                state.update(error='大记录最终校验失败：'+str(exc), error_mtime_ns=source.mtime_ns)
                raise ValueError(state['error']) from exc
            kind = fresh['event_type']
            if not known_internal(fresh):
                state.update(error='超大公开消息或未知记录尚未支持分段归档；原正文和附件未跳过，确认游标保留', error_mtime_ns=source.mtime_ns)
                raise ValueError(state['error'])
            return StreamedInternal(kind, digest), end, 0
    pending = state['scan_offset']-start if state['scan_offset'] >= target else 0
    return None, start, pending
