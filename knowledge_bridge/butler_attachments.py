"""Conversation-local image originals. No OCR, knowledge import, or remote fetch."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import threading
import uuid
import zlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 4
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 8192
DRAFT_RETENTION_HOURS = 24
_ID = re.compile(r'^[a-f0-9]{32}$')
_STORE_LOCKS = {}
_STORE_LOCK_GUARD = threading.Lock()


def image_info(data):
    """Validate image container headers and dimensions without decoding or rewriting."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('图片为空或超过单张8MiB上限')
    mime = None
    width = height = 0
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        position = 8
        seen_header = seen_pixels = ended = False
        while position + 12 <= len(data):
            length = struct.unpack_from('>I', data, position)[0]
            kind = data[position + 4:position + 8]
            stop = position + 12 + length
            if stop > len(data):
                raise ValueError('PNG图片数据不完整')
            payload = data[position + 8:position + 8 + length]
            checksum = struct.unpack_from('>I', data, position + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xffffffff != checksum:
                raise ValueError('PNG图片校验失败')
            if not seen_header:
                if kind != b'IHDR' or length != 13:
                    raise ValueError('PNG图片头无效')
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
                depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
                if depth not in depths.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                    raise ValueError('PNG图片头无效')
                seen_header = True
            elif kind == b'IHDR':
                raise ValueError('PNG图片头重复')
            if kind == b'IDAT' and payload:
                seen_pixels = True
            if kind == b'IEND':
                if length or stop != len(data):
                    raise ValueError('PNG图片结束标记无效')
                ended = True
                break
            position = stop
        if not (seen_header and seen_pixels and ended):
            raise ValueError('PNG图片数据不完整')
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8'):
        position = 2
        seen_scan = False
        while position < len(data):
            if data[position] != 0xff:
                raise ValueError('JPEG图片标记无效')
            while position < len(data) and data[position] == 0xff:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker == 0xd9:
                break
            if marker in {0x01, *range(0xd0, 0xd8)}:
                continue
            if position + 2 > len(data):
                raise ValueError('JPEG图片数据不完整')
            length = struct.unpack_from('>H', data, position)[0]
            if length < 2 or position + length > len(data):
                raise ValueError('JPEG图片数据不完整')
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if length < 8:
                    raise ValueError('JPEG图片头无效')
                height, width = struct.unpack_from('>HH', data, position + 3)
                if not 1 <= data[position + 7] <= 4 or length != 8 + 3 * data[position + 7]:
                    raise ValueError('JPEG图片分量无效')
            if marker == 0xda:
                if length < 8 or length != 6 + 2 * data[position + 2] or position + length >= len(data) - 2:
                    raise ValueError('JPEG图片扫描数据无效')
                seen_scan = True
                break
            position += length
        if not (width and height and seen_scan and data.endswith(b'\xff\xd9')):
            raise ValueError('JPEG图片数据不完整')
        mime = 'image/jpeg'
    elif data[:6] in {b'GIF87a', b'GIF89a'}:
        if len(data) < 14:
            raise ValueError('GIF图片数据不完整')
        width, height = struct.unpack_from('<HH', data, 6)
        position = 13
        if data[10] & 0x80:
            position += 3 * (1 << ((data[10] & 7) + 1))
        seen_frame = ended = False
        def blocks(offset, *, pixels=False):
            nonempty = False
            while offset < len(data):
                size = data[offset]
                offset += 1
                if size == 0:
                    if pixels and not nonempty:
                        raise ValueError('GIF图片缺少图像数据')
                    return offset
                if offset + size > len(data):
                    break
                nonempty = True
                offset += size
            raise ValueError('GIF图片数据不完整')
        while position < len(data):
            marker = data[position]
            position += 1
            if marker == 0x3b:
                ended = position == len(data)
                break
            if marker == 0x21:
                if position >= len(data):
                    raise ValueError('GIF图片扩展无效')
                position = blocks(position + 1)
            elif marker == 0x2c:
                if position + 9 >= len(data):
                    raise ValueError('GIF图片帧无效')
                left, top, frame_width, frame_height, packed = struct.unpack_from('<HHHHB', data, position)
                if not frame_width or not frame_height or left + frame_width > width or top + frame_height > height:
                    raise ValueError('GIF图片帧尺寸无效')
                position += 9
                if packed & 0x80:
                    position += 3 * (1 << ((packed & 7) + 1))
                if position >= len(data) or not 2 <= data[position] <= 8:
                    raise ValueError('GIF图片像素编码无效')
                position = blocks(position + 1, pixels=True)
                seen_frame = True
            else:
                raise ValueError('GIF图片标记无效')
        if not (seen_frame and ended):
            raise ValueError('GIF图片数据不完整')
        mime = 'image/gif'
    elif len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        if struct.unpack_from('<I', data, 4)[0] + 8 != len(data):
            raise ValueError('WebP图片数据不完整')
        position = 12
        seen_pixels = False
        while position + 8 <= len(data):
            kind = data[position:position + 4]
            size = struct.unpack_from('<I', data, position + 4)[0]
            stop = position + 8 + size
            if stop + (size & 1) > len(data):
                raise ValueError('WebP图片数据不完整')
            payload = data[position + 8:stop]
            if kind == b'VP8X':
                if position != 12 or size != 10:
                    raise ValueError('WebP图片头无效')
                if payload[0] & 2:
                    raise ValueError('动画WebP暂不支持，请使用静态图片或GIF')
                width = int.from_bytes(payload[4:7], 'little') + 1
                height = int.from_bytes(payload[7:10], 'little') + 1
            elif kind in {b'VP8 ', b'VP8L'}:
                if seen_pixels:
                    raise ValueError('WebP图片图像块重复')
                if kind == b'VP8 ' and size > 10 and payload[3:6] == b'\x9d\x01\x2a':
                    frame_width = struct.unpack_from('<H', payload, 6)[0] & 0x3fff
                    frame_height = struct.unpack_from('<H', payload, 8)[0] & 0x3fff
                elif kind == b'VP8L' and size > 5 and payload[0] == 0x2f:
                    bits = int.from_bytes(payload[1:5], 'little')
                    frame_width = (bits & 0x3fff) + 1
                    frame_height = ((bits >> 14) & 0x3fff) + 1
                else:
                    raise ValueError('WebP图片图像块无效')
                if width and (width != frame_width or height != frame_height):
                    raise ValueError('WebP图片尺寸不一致')
                width, height = frame_width, frame_height
                seen_pixels = True
            elif kind in {b'ANIM', b'ANMF'}:
                raise ValueError('动画WebP暂不支持，请使用静态图片或GIF')
            position = stop + (size & 1)
        if not seen_pixels or position != len(data):
            raise ValueError('WebP图片缺少完整图像块')
        mime = 'image/webp'
    else:
        raise ValueError('仅支持实际内容为PNG、JPEG、GIF或WebP的图片；不接受SVG或改名文件')
    if not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
        raise ValueError('图片每边须在1至8192像素之间')
    return {'mime': mime, 'width': width, 'height': height}


class AttachmentStore:
    def __init__(self, context, *, clock=None):
        self.root = context.state / 'local-butler' / 'attachments'
        self.root.mkdir(parents=True, exist_ok=True)
        # Refresh/reopen can construct another store in the same service process.
        # Both must count and reserve capacity under the same lock.
        with _STORE_LOCK_GUARD:
            self.lock = _STORE_LOCKS.setdefault(str(self.root.resolve()).casefold(), threading.RLock())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sweep_directories = self._sweep_files = None
        self._sweep_sid = None
        self.cleanup_expired()

    @staticmethod
    def _timestamp(value):
        if not isinstance(value, str):
            raise ValueError('附件时间无效')
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('附件时间无效')
        return stamp.astimezone(timezone.utc)

    def _sent_ids(self, sid):
        """None means the frozen-message evidence could not be verified."""
        database = self.root.parent / 'conversations.sqlite'
        try:
            if (not database.is_file() or database.is_symlink()
                    or getattr(database, 'is_junction', lambda: False)()):
                return None
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                result = set()
                rows = db.execute("SELECT attachments FROM messages WHERE session=? AND attachments IS NOT NULL AND attachments!='[]'", (sid,))
                for row in rows:
                    if not isinstance(row[0], str) or len(row[0]) > 65536:
                        return None
                    values = json.loads(row[0])
                    if not isinstance(values, list) or len(values) > MAX_IMAGES:
                        return None
                    for value in values:
                        attachment_id = value.get('id') if isinstance(value, dict) else None
                        if not isinstance(attachment_id, str) or not _ID.fullmatch(attachment_id):
                            return None
                        result.add(attachment_id)
                return result
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None

    @staticmethod
    def _physical(paths):
        """Return known physical bytes and whether an entry cannot be bounded."""
        size, unbounded = 0, False
        for path in paths:
            try:
                if (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                        or not path.is_file()):
                    unbounded = True
                else:
                    size += path.stat().st_size
            except OSError:
                unbounded = True
        return size, unbounded

    def _inventory(self, sid):
        directory = self._directory(sid)
        drafts, groups, unknown = [], {}, []
        if not directory.exists():
            return drafts, unknown
        sent = self._sent_ids(sid)
        try:
            paths = list(directory.iterdir())
        except OSError:
            return drafts, [(0, True)]
        for path in paths:
            match = re.fullmatch(r'([a-f0-9]{32})\.(blob|json)', path.name)
            if match:
                groups.setdefault(match[1], []).append(path)
            else:
                unknown.append(self._physical([path]))
        for attachment_id, paths in groups.items():
            if sent is not None and attachment_id in sent:
                continue  # Frozen messages protect originals even if metadata was changed.
            try:
                value, _ = self._load(sid, attachment_id)
                self._timestamp(value.get('created_at'))
                if value['job_ids']:
                    continue
                if sent is None:
                    unknown.append(self._physical(paths))
                else:
                    drafts.append(value)
            except ValueError:
                unknown.append(self._physical(paths))
        return drafts, unknown

    def _expire_current(self, sid, now):
        drafts, unknown = self._inventory(sid)
        retained = []
        removed_count = removed_bytes = 0
        for value in drafts:
            if self._timestamp(value['created_at']) + timedelta(hours=DRAFT_RETENTION_HOURS) <= now:
                paths = self._paths(sid, value['id'])
                try:
                    for path in paths:
                        path.unlink()
                    removed_count += 1
                    removed_bytes += value['bytes']
                except OSError:
                    unknown.append(self._physical(paths))
            else:
                retained.append(value)
        return retained, unknown, removed_count, removed_bytes

    def cleanup_expired(self, *, now=None, max_sessions=4, max_files=16):
        """Bounded, rotating cross-session sweep. Never infer unsent from age alone."""
        now = now or self._clock()
        if now.tzinfo is None or not 1 <= max_sessions <= 32 or not 1 <= max_files <= 256:
            raise ValueError('附件清理范围无效')
        with self.lock:
            # Validate the root without creating or following a session directory.
            self._directory('0' * 32)
            result = {'removed_count': 0, 'removed_bytes': 0, 'scanned_sessions': 0, 'examined_files': 0}
            sent = None
            current_sid = None
            root_entries = 0
            while result['examined_files'] < max_files:
                if self._sweep_files is None:
                    if result['scanned_sessions'] >= max_sessions or root_entries >= max_sessions * 4:
                        break
                    if self._sweep_directories is None:
                        try:
                            self._sweep_directories = os.scandir(self.root)
                        except OSError:
                            break
                    try:
                        entry = next(self._sweep_directories)
                    except StopIteration:
                        self._sweep_directories.close()
                        self._sweep_directories = None
                        break
                    root_entries += 1
                    if not _ID.fullmatch(entry.name):
                        continue
                    try:
                        directory = self._directory(entry.name)
                        if not directory.is_dir():
                            continue
                        self._sweep_files = os.scandir(directory)
                        self._sweep_sid = entry.name
                    except (OSError, ValueError):
                        continue
                if current_sid != self._sweep_sid:
                    current_sid = self._sweep_sid
                    sent = self._sent_ids(current_sid)
                    result['scanned_sessions'] += 1
                try:
                    entry = next(self._sweep_files)
                except StopIteration:
                    self._sweep_files.close()
                    self._sweep_files = None
                    self._sweep_sid = None
                    continue
                result['examined_files'] += 1
                match = re.fullmatch(r'([a-f0-9]{32})\.json', entry.name)
                if not match or sent is None or match[1] in sent:
                    continue
                try:
                    value, _ = self._load(current_sid, match[1])
                    if value['job_ids'] or self._timestamp(value.get('created_at')) + timedelta(hours=DRAFT_RETENTION_HOURS) > now:
                        continue
                    for path in self._paths(current_sid, match[1]):
                        path.unlink()
                    result['removed_count'] += 1
                    result['removed_bytes'] += value['bytes']
                except (OSError, ValueError):
                    continue  # Damaged, unverified, and unknown originals remain visible in quota.
            return result

    def close(self):
        with self.lock:
            for name in ('_sweep_files', '_sweep_directories'):
                iterator = getattr(self, name, None)
                if iterator is not None:
                    iterator.close()
                    setattr(self, name, None)
            self._sweep_sid = None

    def __del__(self):
        for name in ('_sweep_files', '_sweep_directories'):
            iterator = getattr(self, name, None)
            if iterator is not None:
                iterator.close()

    def drafts(self, sid):
        with self.lock:
            self.cleanup_expired()
            values, unknown, _, _ = self._expire_current(sid, self._clock())
            values.sort(key=lambda value: (value['created_at'], value['id']))
            unknown_bytes = sum(size for size, _ in unknown)
            unbounded = any(flag for _, flag in unknown)
            count = len(values) + (max(MAX_IMAGES, len(unknown)) if unbounded else len(unknown))
            size = sum(value['bytes'] for value in values) + (max(MAX_TOTAL_BYTES, unknown_bytes) if unbounded else unknown_bytes)
            return {'attachments': [self._public(value) for value in values[:MAX_IMAGES]],
                    'retained_unknown': {'count': len(unknown), 'bytes': unknown_bytes,
                                         'message': '有无法核验的附件原件已保留并计入草稿额度；未自动删除，请核对后处理该对话。' if unknown else ''},
                    'quota': {'count': count, 'bytes': size, 'max_count': MAX_IMAGES, 'max_bytes': MAX_TOTAL_BYTES},
                    'retention_hours': DRAFT_RETENTION_HOURS}

    def _directory(self, sid, *, create=False):
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            raise ValueError('对话标识无效')
        path = self.root / sid
        if (self.root.is_symlink() or getattr(self.root, 'is_junction', lambda: False)()
                or path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                or path.resolve().parent != self.root.resolve()):
            raise ValueError('附件目录无效')
        if create:
            path.mkdir(exist_ok=True)
        return path

    def _paths(self, sid, attachment_id):
        if not isinstance(attachment_id, str) or not _ID.fullmatch(attachment_id):
            raise ValueError('附件标识无效')
        directory = self._directory(sid)
        blob = directory / (attachment_id + '.blob')
        metadata = directory / (attachment_id + '.json')
        if blob.is_symlink() or metadata.is_symlink():
            raise ValueError('附件文件无效')
        return blob, metadata

    @staticmethod
    def _public(value):
        return {key: value[key] for key in ('id', 'session', 'filename', 'mime', 'bytes', 'width', 'height', 'sha256', 'url')}

    @staticmethod
    def _write_metadata(path, value):
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, sid, filename, data):
        info = image_info(data)
        if not isinstance(filename, str):
            raise ValueError('图片名称无效')
        filename = filename.replace('\\', '/').split('/')[-1]
        filename = ''.join(char for char in filename if char >= ' ' and char not in '\x7f').strip()[:160] or 'image'
        with self.lock:
            directory = self._directory(sid, create=True)
            capacity = self.drafts(sid)['quota']
            if capacity['count'] >= MAX_IMAGES or capacity['bytes'] + len(data) > MAX_TOTAL_BYTES:
                raise ValueError('本对话未发送图片最多4张、合计16MiB；请发送或移除草稿后再添加。无法核验的保留原件也计入额度。')
            attachment_id = uuid.uuid4().hex
            value = {'id': attachment_id, 'session': sid, 'filename': filename, **info,
                     'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                     'created_at': self._clock().isoformat(), 'job_ids': [],
                     'url': f'/api/butler/sessions/{sid}/attachments/{attachment_id}'}
            blob, metadata = self._paths(sid, attachment_id)
            try:
                with blob.open('xb') as stream:
                    stream.write(data)
                self._write_metadata(metadata, value)
            except Exception:
                blob.unlink(missing_ok=True)
                raise
            return self._public(value)

    def _load(self, sid, attachment_id):
        blob, metadata = self._paths(sid, attachment_id)
        try:
            if metadata.stat().st_size > 1024 * 1024 or not 0 < blob.stat().st_size <= MAX_IMAGE_BYTES:
                raise ValueError()
            value = json.loads(metadata.read_text('utf-8'))
            if value.get('id') != attachment_id or value.get('session') != sid:
                raise ValueError()
            data = blob.read_bytes()
            if len(data) != value['bytes'] or hashlib.sha256(data).hexdigest() != value['sha256']:
                raise ValueError()
            info = image_info(data)
            if any(value.get(key) != expected for key, expected in info.items()):
                raise ValueError()
            if value.get('url') != f'/api/butler/sessions/{sid}/attachments/{attachment_id}':
                raise ValueError()
            filename = value.get('filename')
            if not isinstance(filename, str) or not 1 <= len(filename) <= 160 or any(char < ' ' or char in '/\\\x7f' for char in filename):
                raise ValueError()
            if not isinstance(value.get('job_ids'), list) or any(not isinstance(jid, str) or not _ID.fullmatch(jid) for jid in value['job_ids']):
                raise ValueError()
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ValueError('附件不存在、已删除或原件已变化，请重新上传') from error
        return value, data

    def get(self, sid, attachment_id):
        with self.lock:
            value, _ = self._load(sid, attachment_id)
            return self._public(value)

    def original(self, sid, attachment_id):
        with self.lock:
            self._load(sid, attachment_id)
            return self._paths(sid, attachment_id)[0]

    def select(self, sid, attachment_ids):
        if not isinstance(attachment_ids, (list, tuple)) or len(attachment_ids) > MAX_IMAGES:
            raise ValueError('每轮最多选择4张图片')
        if any(not isinstance(value, str) for value in attachment_ids) or len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError('附件标识重复或无效')
        with self.lock:
            values = [self.get(sid, value) for value in attachment_ids]
            if sum(value['bytes'] for value in values) > MAX_TOTAL_BYTES:
                raise ValueError('每轮图片合计不能超过16MiB')
            return values

    def image_parts(self, sid, attachment_ids):
        with self.lock:
            self.select(sid, attachment_ids)
            parts = []
            for attachment_id in attachment_ids:
                value, data = self._load(sid, attachment_id)
                url = 'data:' + value['mime'] + ';base64,' + base64.b64encode(data).decode('ascii')
                parts.append({'type': 'image_url', 'image_url': {'url': url}})
            return parts

    def bind(self, sid, attachment_ids, jid):
        if not isinstance(jid, str) or not _ID.fullmatch(jid):
            raise ValueError('请求标识无效')
        with self.lock:
            self.select(sid, attachment_ids)
            for attachment_id in attachment_ids:
                value, _ = self._load(sid, attachment_id)
                value['job_ids'] = sorted(set(value.get('job_ids', [])) | {jid})
                self._write_metadata(self._paths(sid, attachment_id)[1], value)

    def delete(self, sid, attachment_id):
        with self.lock:
            value, _ = self._load(sid, attachment_id)
            sent = self._sent_ids(sid)
            if value.get('job_ids') or (sent is not None and attachment_id in sent):
                raise ValueError('已发送的图片原件随对话保留；可在核对后永久删除该对话')
            if sent is None:
                raise ValueError('暂时无法核对图片是否已发送，原件已保留；请恢复会话记录后再处理')
            for path in self._paths(sid, attachment_id):
                path.unlink()
            return {'id': attachment_id, 'removed': True}

    def delete_session(self, sid):
        with self.lock:
            directory = self._directory(sid)
            if not directory.exists():
                return
            if self._sweep_sid == sid:
                self._sweep_files.close()
                self._sweep_files = None
                self._sweep_sid = None
            paths = list(directory.iterdir())
            if any(path.is_symlink() or not path.is_file() or not re.fullmatch(r'[a-f0-9]{32}\.(blob|json)', path.name) for path in paths):
                raise ValueError('附件目录含未知文件，未清除原件')
            for path in paths:
                path.unlink()
            directory.rmdir()
