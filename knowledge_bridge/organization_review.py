"""Read-only display of external review receipts; never grants write authority."""
import hashlib
import json
from pathlib import Path

_hash_cache = {}
MAX_SOURCE_BYTES = 16 * 1024 * 1024


def load_receipt(context):
    path = context.state / 'organization-review-receipt.json'
    try:
        if path.stat().st_size > 2 * 1024 * 1024: raise ValueError('receipt too large')
        data = json.loads(path.read_text('utf-8'))
        if not isinstance(data,dict) or data.get('schema_version') != 1 or data.get('workspace_id') != context.workspace_id:
            raise ValueError('workspace mismatch')
        rows = data.get('rows', [])
        if not isinstance(rows, list) or len(rows) > 2000: raise ValueError('invalid rows')
        by_id = {}
        for row in rows:
            sid = row.get('session_id') if isinstance(row, dict) else None
            if sid:
                # Ambiguous identities do not count as reviewed.
                by_id[sid] = None if sid in by_id else row
        return by_id, {'state': 'available', 'reviewer': data.get('reviewer'),
                       'updated_at': data.get('updated_at'), 'source': path.name}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, {'state': 'unavailable', 'source': path.name}


def _fingerprint(path):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if stat.st_size > MAX_SOURCE_BYTES: raise ValueError('source exceeds display budget')
    if key not in _hash_cache:
        raw = path.read_bytes()
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('source changed during validation')
        if len(_hash_cache) >= 512: _hash_cache.clear()
        _hash_cache[key] = hashlib.sha256(raw).hexdigest()
    return stat.st_size, _hash_cache[key]


def _complete(ranges, size):
    cursor = 0
    for row in sorted(ranges, key=lambda r: r['from']):
        start, end = row['from'], row['to']
        if type(start) is not int or type(end) is not int or start > cursor or end < start or end > size:
            return False
        cursor = max(cursor, end)
    return cursor == size and size > 0


def review_state(context, item, receipt, candidate_lookup):
    unknown = {'review_state': 'unreviewed', 'review_label': '尚无阅读回执'}
    # Stable source identity precedes file versions and temporary review outcomes.
    from .conversation_visibility import organization_runner, read_json
    archive = Path(item.get('archive_path') or '').resolve()
    if archive.is_relative_to(context.raw_archive.resolve()):
        manifest = read_json(archive / '03_元数据' / 'manifest.json', {})
        if organization_runner(context, manifest):
            return {'review_state': 'excluded', 'review_label': '整理任务自身，无需整理',
                    'review_exception': 'internal_organization_task', 'stable_exclusion': True}
    if not receipt: return unknown
    result = {'reviewed_at': receipt.get('reviewed_at'), 'review_note': str(receipt.get('note') or '')[:400],
              'review_exception': str(receipt.get('exception') or '')[:300]}
    try:
        relative = Path(receipt['source_path'])
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('invalid source')
        path = (context.root / relative).resolve()
        if not path.is_relative_to(context.raw_archive.resolve()): raise ValueError('wrong scope')
        archive = Path(item.get('archive_path') or '').resolve()
        if path != archive and not path.is_relative_to(archive): raise ValueError('session mismatch')
        size, digest = _fingerprint(path)
        if size != receipt.get('source_bytes') or digest != receipt.get('source_sha256'):
            return result | {'review_state': 'changed', 'review_label': '来源有变化，待续读'}
        if receipt.get('outcome') == 'excluded':
            return result | {'review_state': 'excluded', 'review_label': '本轮无需整理'}
        if not _complete(receipt.get('reviewed_ranges', []), size):
            return result | {'review_state': 'partial', 'review_label': '已读部分，待续读'}
        ids = receipt.get('candidate_ids') or []
        if receipt.get('outcome') == 'no_change' and not ids:
            return result | {'review_state': 'no_change', 'review_label': '已读，无新建议'}
        if receipt.get('outcome') in {'candidates', 'written'} and ids:
            candidates = [candidate_lookup(cid) for cid in ids]
            written = all(row.get('status') == 'written' for row in candidates)
            return result | {'review_state': 'organized' if written else 'reviewed',
                             'review_label': '已整理，建议已保存' if written else '已读，建议另见整理记录',
                             'candidate_ids': ids}
        return result | {'review_state': 'partial', 'review_label': '阅读结果待核对'}
    except (OSError, ValueError, KeyError, TypeError):
        return result | unknown | {'review_label': '阅读回执待核对'}
