"""Copy-on-write evidence snapshots. Call writers only under IndexWriteGate.

The published JSON selects a Lance version; incomplete writes are never selected.
Only changed chunks are appended. File-level freshness lives in the accompanying
state JSON, not in every unchanged vector row. No original files are modified.
"""
import json
import time
import uuid
from datetime import timedelta
from filelock import FileLock


def reader_guard(root):
    # Queries hold this through materialization. Maintenance cannot vacuum their
    # files. Normal publishing does not take this lock or interrupt readers.
    return FileLock(root / 'readers-maintenance.lock', timeout=30)


def select(database, descriptor):
    table = database.open_table(descriptor['table'])
    if descriptor.get('table_version') is not None:
        table.checkout(int(descriptor['table_version']))
    return table


def migrate_legacy(context, descriptor):
    """One copy on upgrade, even when no source has changed yet."""
    if descriptor.get('version') != 1 or not descriptor.get('table'):
        return False
    import lancedb
    from .evidence_index import _atomic
    root=context.index_root('evidence');db=lancedb.connect(root/'lancedb')
    old=db.open_table(descriptor['table']);generation=uuid.uuid4().hex
    name='evidence_'+generation;created=db.create_table(name,data=old.to_arrow())
    if created.count_rows()!=old.count_rows():raise RuntimeError('证据快照升级校验失败')
    state=json.loads((root/descriptor['state_file']).read_text('utf-8'))
    state_file='generations/state-'+generation+'.json';_atomic(root/state_file,state)
    _atomic(root/'active-vector.json',dict(descriptor,version=2,table=name,
        table_version=created.version,generation=generation,state_file=state_file,
        previous_state_file=descriptor['state_file']))
    return True


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def condition(column, values):
    return column + ' IN (' + ','.join(quote(v) for v in sorted(values)) + ')'


def chunk_delta(old_rows, new_rows):
    # Changing a conversation's file hash/time must not rewrite all its vectors.
    # Retrieval supplies these two fields from the SAME published state snapshot.
    volatile = {'source_version', 'modified_at'}
    previous = {row['chunk_id']: row for row in old_rows}
    current = {row['chunk_id']: row for row in new_rows}
    changed = [row for key, row in current.items()
               if key not in previous or
               {k:v for k,v in row.items() if k not in volatile} !=
               {k:v for k,v in previous[key].items() if k not in volatile}]
    removed = (set(previous) - set(current)) | {row['chunk_id'] for row in changed if row['chunk_id'] in previous}
    return changed, removed


def write_delta(database, descriptor, new_rows, removed, schema, expected_count):
    import pyarrow as pa
    table = database.open_table(descriptor['table'])
    committed = int(descriptor['table_version'])
    if table.version != committed:
        # A failed publish or terminated process may have left newer versions.
        # Restore is metadata-only; the archive/synchronization cursor is untouched.
        table.restore(committed)
        table.checkout_latest()
    if removed:
        table.delete(condition('chunk_id', removed))
    if new_rows:
        table.add(pa.Table.from_pylist(new_rows, schema=schema))
    if table.count_rows() != expected_count:
        raise RuntimeError('证据增量索引校验失败：落盘数量不一致')
    return table


def maintain(context, descriptor, *, force=False, interval=1800, safety_margin=60):
    """Bound snapshot storage without compacting a whole library every sync.

    Writer gate + reader guard exclude both incomplete writes and live searches.
    Current/previous published versions are tagged before any vacuum. Other tags
    (including user pins) remain untouched. One small receipt is overwritten.
    """
    import lancedb
    root = context.index_root('evidence')
    receipt = root / 'storage-maintenance.json'
    try:
        previous = json.loads(receipt.read_text('utf-8'))
    except (OSError, ValueError):
        previous = {}
    now = time.time()
    if not force and now - previous.get('at', 0) < interval:
        return None
    if descriptor.get('version') != 2 or not descriptor.get('table'):
        return None
    with reader_guard(root):
        table = lancedb.connect(root/'lancedb').open_table(descriptor['table'])
        version = int(descriptor['table_version'])
        if table.version != version:
            table.restore(version)
            table.checkout_latest()
        tags = table.tags.list()
        for name, value in [('jiedu-published', version),
                            ('jiedu-previous', descriptor.get('previous_table_version') or version)]:
            if name in tags:
                table.tags.update(name, int(value))
            else:
                table.tags.create(name, int(value))
        # Native API; no extra pylance installation. This compacts only on the
        # maintenance cadence and prunes obsolete transactions, including failed
        # unpublished writes. Tagged published snapshots remain searchable.
        # The bundled Rust API refuses a cutoff that includes ANY tagged
        # version (rather than silently skipping tags). Derive the age from the
        # oldest protected timestamp, including foreign/user tags. An additional
        # grace covers compaction time; an overrun fails closed, never drops pins.
        protected={int(tag['version']) for tag in table.tags.list().values()}
        versions=table.list_versions()
        oldest=min(v['timestamp'].timestamp() for v in versions if v['version'] in protected)
        cleanup_age=max(0,time.time()-oldest)+safety_margin
        table.optimize(cleanup_older_than=timedelta(seconds=cleanup_age), delete_unverified=True)
        # Optimize may publish an equivalent compacted version. Select it in the
        # descriptor so subsequent updates do not restore the pre-compaction tree.
        table.checkout_latest()
        descriptor = dict(descriptor, table_version=table.version)
        from .evidence_index import _atomic
        _atomic(root/'active-vector.json', descriptor)
        table.tags.update('jiedu-published', table.version)
        result = {'at': now, 'table': descriptor['table'], 'version': table.version,
                  'interval_seconds': interval, 'cleanup_age_seconds':cleanup_age,
                  'retained_versions': sorted({table.version, int(descriptor.get('previous_table_version') or version)})}
        _atomic(receipt, result)
        return result
