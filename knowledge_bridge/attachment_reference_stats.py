"""Read-only reference categories; old totals remain compatible evidence."""
from collections import Counter

def reference_kind(row):
    ref=str(row.get('source_reference') or row.get('reference') or '')
    if ref.startswith('delivery:'):return 'delivery_link'
    if ref.startswith(('...','…')):return 'text_reference'
    explicit=row.get('reference_kind')
    if explicit in {'uploaded','delivery_link','text_reference'}:return explicit
    if ref.startswith(('inline:','locator:')):return 'uploaded'
    return 'legacy_reference'

def summarize(rows,locators=None,reviews=None):
    latest={}
    for row in rows:latest[(row.get('session_key'),str(row.get('source_message_id') or row.get('message_id')),row.get('source_reference') or row.get('reference'))]=row
    kinds={};unavailable=[];unique=set();locators=locators or {};reviews=reviews or {};reviewed=0;recovered=0;rendered_copies=0;reasons=Counter()
    for row in latest.values():
        kind=reference_kind(row);group=kinds.setdefault(kind,{'references':0,'available':0,'unavailable':0})
        group['references']+=1;ref=row.get('source_reference') or row.get('reference')
        okay=str(row.get('status') or '').lower() in {'available','mapped'} and not row.get('missing',False)
        review=reviews.get((str(row.get('session_key')),str(row.get('source_message_id') or row.get('message_id')),str(ref)),{})
        if review:
            reviewed+=1
            if okay and review.get('decision')=='recover' and row.get('sha256')==review.get('expected_sha256'):
                recovered+=1
                if review.get('reason')=='source_rendered_image_recovered':rendered_copies+=1
        group['available' if okay else 'unavailable']+=1
        if not okay:
            key=str(row.get('source_session_id') or '')+'\0'+str(row.get('source_message_id') or row.get('message_id') or '')+'\0'+str(ref or '')
            local=locators.get(key) or locators.get(ref) or {};path=local.get('local_path') or row.get('source_path')
            unique.add(path or ref)
            reason=review.get('reason') if review.get('decision')=='unresolved' and review.get('source_path')==path else None
            reason=reason or ('incomplete_reference' if kind=='text_reference' else 'recorded_location_unavailable' if path else 'source_locator_unavailable')
            reasons[reason]+=1
            unavailable.append({'kind':kind,'reference':ref,'original_name':row.get('original_name'),'source_path':path,
                'session_key':row.get('session_key'),'message_id':row.get('source_message_id') or row.get('message_id'),
                'title':review.get('title'),'message_timestamp':review.get('message_timestamp'),
                'reason':reason,'reason_detail':review.get('reason_detail') if reason==review.get('reason') else None})
    return {'reference_count':len(latest),'by_kind':kinds,'unavailable_references':len(unavailable),
        'unique_unavailable_sources':len(unique),'unavailable':unavailable[:200],'more_unavailable':len(unavailable)>200,
        'recovery_review':{'reviewed':reviewed,'recovered':recovered,'rendered_image_copies':rendered_copies},'unavailable_by_reason':dict(reasons),
        'notice':'引用次数与不同来源数量分别统计；交付链接和正文引用不等于原生上传。保留失效定位器，不以同名文件冒充历史版本。'}
