"""Additive legacy layout conversion. Original files and identifiers stay intact."""
import hashlib,json,shutil
from pathlib import Path
from filelock import FileLock
from .archive_store import _atomic_json,_safe_title

def preserve_image_relations(context, target, manifest):
    """Legacy IMG identifiers are explicit manifest references, not fuzzy filenames."""
    ledger=target/'03_元数据/attachments.jsonl'
    if ledger.exists() or not manifest.get('image_ids'):return
    rows=[]
    for message in manifest.get('messages',[]):
        for ref in message.get('image_refs',[]):
            # Require an explicit known identifier and exactly one exact stem.
            files=[p for p in (target/'02_附件').iterdir() if p.is_file() and p.stem==ref] if ref in manifest['image_ids'] else []
            original=files[0] if len(files)==1 else None
            rows.append({'source_message_id':message['message_id'],'source_reference':ref,
                         'original_name':original.name if original else ref,
                         'stored_path':context.relative(original) if original else None,
                         'sha256':hashlib.sha256(original.read_bytes()).hexdigest() if original else None,
                         'status':'available' if original else 'missing','missing':original is None,
                         'mapping_basis':'explicit_manifest_IMG_identifier_exact_unique_stem'})
    ledger.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),'utf-8')

def normalize(context, source):
    source=Path(source).resolve()
    if not source.is_relative_to(context.raw_archive.resolve()):raise ValueError('来源必须在原始档案内')
    metadata=source/'03_元数据' if (source/'03_元数据/manifest.json').is_file() else source
    manifest=json.loads((metadata/'manifest.json').read_text('utf-8'))
    if manifest.get('session_key'):return {'status':'already_canonical','path':context.relative(source)}
    from .session_archive import _canonical_item
    item=_canonical_item(metadata)
    if not item:raise ValueError('缺少可核验原始消息')
    sid=item['session_id'];family=source.parent.name
    title=item.get('session_title') or source.name
    if title==source.name and len(title)==12 and all(c in '0123456789abcdef' for c in title):
        title='未命名对话 '+sid.replace('hermes:','')
    target=source if metadata!=source else source.parent/_safe_title(title)
    if target!=source and target.exists():target=source.parent/(_safe_title(title,limit=85)+' ['+hashlib.sha256(sid.encode()).hexdigest()[:8]+']')
    state=context.state/'legacy-layouts.json'
    with FileLock(str(context.state/'legacy-layouts.lock')):
        index=json.loads(state.read_text('utf-8')) if state.exists() else {'sessions':{}}
        previous=index['sessions'].get(sid)
        if previous:
            preserve_image_relations(context,context.root/previous['target_path'],manifest)
            return previous|{'status':'already_normalized'}
        originals={context.relative(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
        for name in ('00_原始数据','01_对话','02_附件','03_元数据'):(target/name).mkdir(parents=True,exist_ok=True)
        def copy(src,dst):
            dst.parent.mkdir(parents=True,exist_ok=True)
            if dst.exists() and dst.read_bytes()!=src.read_bytes():raise ValueError('目标已有不同内容，停止且不覆盖：'+str(dst))
            if not dst.exists():shutil.copy2(src,dst)
        # Exact original files, including metadata, are the recovery baseline.
        for rel in originals:
            src=context.root/rel
            copy(src,target/'00_原始数据'/'旧结构原件'/src.relative_to(source))
        for child in metadata.glob('*'):
            if child.is_file():copy(child,target/'03_元数据'/child.name)
        conversation=source/'conversation.md'
        if not conversation.exists():conversation=source/'02_标准化对话/conversation.md'
        if conversation.exists():copy(conversation,target/'01_对话/conversation.md')
        else:
            body='# '+title+'\n\n'+'\n\n'.join('## MESSAGE '+str(r['message_id'])+'｜'+r['role'].upper()+'\n\n'+r.get('original_text','') for r in item['messages'])+'\n'
            (target/'01_对话/conversation.md').write_text(body,'utf-8')
        for name in ('attachments','01_图片附件'):
            folder=source/name
            for src in folder.rglob('*') if folder.exists() else []:
                if src.is_file():copy(src,target/'02_附件'/src.relative_to(folder))
        # Missing old message ledger: derive, preserving source IDs and sequence.
        ledger=target/'03_元数据/messages.jsonl'
        if not ledger.exists():ledger.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in item['messages']),'utf-8')
        preserve_image_relations(context,target,manifest)
        for rel,digest in originals.items():
            if hashlib.sha256((context.root/rel).read_bytes()).hexdigest()!=digest:raise ValueError('原件核验失败')
        record={'session_id':sid,'source_path':context.relative(source),'target_path':context.relative(target),'message_count':len(item['messages']),'original_hashes':originals,'originals_preserved':True,'rollback':'保留旧结构原件；移除本记录即恢复旧路径优先读取，新增副本不自动删除','coverage':'旧档现有消息；不证明源端完整历史或附件完整'}
        _atomic_json(target/'03_元数据/layout-conversion.json',record)
        index['sessions'][sid]=record;_atomic_json(state,index)
        return record|{'status':'normalized'}
