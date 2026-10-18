"""Bounded retention for disposable index generations, never source documents."""
import json
import logging
import re
import time
from pathlib import Path


def _linked(path):
    return path.is_symlink() or bool(path.stat().st_file_attributes & 0x400) if hasattr(path.stat(),'st_file_attributes') else path.is_symlink()


def prune(context,kind,*,minimum_age=3600,keep=2):
    # Caller holds IndexWriteGate; its single writer lock covers both indexes.
    if kind not in {'formal','evidence'}:raise ValueError('Unknown index scope')
    root=context.index_root(kind);database=root/'lancedb'
    pointer=root/('active.json' if kind=='formal' else 'active-vector.json')
    if not pointer.exists() or not database.exists():return []
    if _linked(root) or _linked(database) or pointer.is_symlink():raise ValueError('Linked index paths are not eligible for retention')
    active=json.loads(pointer.read_text('utf-8'));prefix='chunks_' if kind=='formal' else 'evidence_'
    pattern=re.compile(re.escape(prefix)+r'[0-9a-f]{32}\.lance')
    if not re.fullmatch(re.escape(prefix)+r'[0-9a-f]{32}',str(active.get('table',''))):return []
    if not (database/(active['table']+'.lance')).is_dir():return []
    candidates=[p for p in database.iterdir() if p.is_dir() and not _linked(p) and pattern.fullmatch(p.name)]
    candidates.sort(key=lambda p:p.stat().st_mtime,reverse=True)
    retained={p.stem for p in candidates[:max(2,keep)]}|{active['table']}
    # Optional explicit pins allow future long-lived readers/restore tasks to retain a generation.
    pins=root/'pinned-generations.json'
    if pins.exists():retained.update(json.loads(pins.read_text('utf-8')).get('tables',[]))
    removed=[]
    import lancedb
    db=lancedb.connect(database)
    for p in candidates:
        if p.stem in retained or time.time()-p.stat().st_mtime<minimum_age:continue
        if p.resolve().parent!=database.resolve():raise ValueError('Index path escaped database')
        # Recheck the pointer immediately before removal, even with the writer gate.
        if json.loads(pointer.read_text('utf-8')).get('table')==p.stem:continue
        db.drop_table(p.stem);removed.append(p.stem)
        generation=p.stem[len(prefix):];state=root/'generations'/f'state-{generation}.json'
        state.unlink(missing_ok=True)
    # State snapshots are small, but also have a bounded lifetime now that many
    # generations share one table. Preserve active/previous and explicit pins.
    protected_states={active.get('state_file'),active.get('previous_state_file')}
    protected_states.update('generations/state-'+name[len(prefix):]+'.json' for name in retained)
    states=root/'generations'
    if states.is_dir() and not _linked(states):
        for state in states.glob('state-*.json'):
            if not re.fullmatch(r'state-[0-9a-f]{32}\.json',state.name) or state.is_symlink():continue
            if 'generations/'+state.name not in protected_states and time.time()-state.stat().st_mtime>=minimum_age:
                state.unlink()
    if removed:
        receipt=root/'retention-last.json';receipt.write_text(json.dumps({'at':time.time(),'removed':removed,'retained':sorted(retained),'minimum_age_seconds':minimum_age},indent=2),'utf-8')
    return removed


def after_publish(context,kind):
    try:
        if kind=='evidence':
            from .evidence_storage import reader_guard,maintain
            with reader_guard(context.index_root(kind)):
                removed=prune(context,kind)
            from .evidence_index import _descriptor
            maintain(context,_descriptor(context))
            return removed
        return prune(context,kind)
    except Exception:
        logging.getLogger(__name__).exception('Index retention deferred; active index remains published')
        return []
