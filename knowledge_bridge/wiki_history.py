"""Server-side validation of completed historical results, never a read grant."""
import json
import re
from .organization_review import _complete, _fingerprint
from .passage_search import file_version
from .wiki_snapshots import WikiSnapshots, source_identity, digest

def candidate_states(context):
    """Replay the existing authoritative queue without touching its history."""
    from .memory_pipeline import _candidate_snapshot
    path=context.state/'memory-control/candidate-events.jsonl'
    try:
        events=[json.loads(line) for line in path.read_text('utf-8').splitlines() if line.strip()]
        return _candidate_snapshot(events)
    except (OSError,ValueError,KeyError,TypeError):return {}

def candidate_summary(context,ids):
    rows=candidate_states(context);ids=set(ids)
    replaced={cid for row in rows.values() if row.get('status')=='written' for cid in row.get('supersedes',[]) if isinstance(cid,str)}
    counts={'pending':0,'saved':0,'rejected':0,'superseded':0,'unknown':0}
    for cid in ids:
        status=rows.get(cid,{}).get('status')
        key='superseded' if cid in replaced or status=='superseded' else 'pending' if status in {'pending','approved_staging'} else 'saved' if status=='written' else 'rejected' if status in {'rejected','less_often'} else 'unknown'
        counts[key]+=1
    return counts

def review_rows(context):
    """Keep distinct historical versions; duplicate exact identities fail closed."""
    from collections import Counter
    path=context.state/'organization-review-receipt.json'
    try:
        if path.stat().st_size>2*1024*1024:return []
        data=json.loads(path.read_text('utf-8'))
        if data.get('schema_version')!=1 or data.get('workspace_id')!=context.workspace_id:return []
        rows=data.get('rows')
        if not isinstance(rows,list) or len(rows)>2000:return []
        rows=[r for r in rows if isinstance(r,dict)]
        def key(row):return json.dumps([row.get(k) for k in ('session_id','source_path','source_bytes','source_sha256')],sort_keys=True)
        counts=Counter(key(row) for row in rows)
        return [row for row in rows if counts[key(row)]==1]
    except (OSError,ValueError,TypeError,AttributeError):return []

class WikiHistory:
    def __init__(self,context):
        self.context=context;self.snapshots=WikiSnapshots(context)
        self.identities={}
        for path in context.raw_archive.rglob('manifest.json'):
            if path.parent.name!='03_元数据':continue
            try:
                item=json.loads(path.read_text('utf-8-sig'))
                key=tuple(item.get(k) for k in ('source_key','source_instance','source_session_id'))
                if all(isinstance(v,str) and v for v in key):self.identities[key]=self.identities.get(key,0)+1
            except (OSError,ValueError,TypeError):continue
        self.candidates=candidate_states(context)
    def results(self,row,raw):
        ids=row.get('candidate_ids',[])
        if not isinstance(ids,list) or len(ids)!=len(set(ids)):return None
        proof=[];rejected=[]
        for cid in ids:
            candidate=self.candidates.get(cid,{})
            if candidate.get('status')=='rejected':rejected.append(candidate);continue
            version=candidate.get('writer_version')
            if candidate.get('status')!='written' or not isinstance(version,str) or not re.fullmatch('v_[0-9a-f]{32}',version):return None
            try:
                meta=json.loads((self.context.versions/'formal-memory'/version/'manifest.json').read_text('utf-8'))
                if meta.get('state')!='applied' or meta.get('context',{}).get('candidate_id')!=cid:return None
                if meta.get('target')!=(candidate.get('provenance') or {}).get('target_path'):return None
                target=(self.context.root/meta['target']).resolve()
                if not target.is_relative_to(self.context.root.resolve()) or meta['target'].split('/')[0] not in {'00_系统','01_当前状态','02_项目','03_知识'}:return None
                # Conservatively require the actual written result still present;
                # an edited/reverted result needs review, never inferred success.
                if digest(target.read_bytes())!=meta.get('after_hash'):return None
                evidence=[e for e in meta.get('evidence',[]) if e.get('source_path')==row['source_path']]
                if not evidence:return None
                for ev in evidence:
                    a,b=ev.get('byte_start'),ev.get('byte_end')
                    if type(a) is not int or type(b) is not int or not 0<=a<b<=len(raw) or ev.get('version')!=row['version']:return None
                    if digest(raw[a:b])!=ev.get('quote_sha256') or raw[a:b].decode('utf-8')!=ev.get('quote'):return None
                proof.append({'candidate_id':cid,'writer_version':version,'after_hash':meta['after_hash']})
            except (OSError,ValueError,TypeError,KeyError):return None
        for old in rejected:
            previous=old.get('provenance') or {}
            replacement=None
            for saved in proof:
                if 'writer_version' not in saved:continue
                current=self.candidates[saved['candidate_id']].get('provenance') or {}
                if not all(previous.get(k) and previous[k]==current.get(k) for k in ('client_id','job_id','target_path')):continue
                evidence=[e for e in old.get('evidence',[]) if e.get('source_path')==row['source_path'] and e.get('version')==row['version']]
                if not evidence:continue
                try:
                    if not all(type(e.get('byte_start')) is int and type(e.get('byte_end')) is int and 0<=e['byte_start']<e['byte_end']<=len(raw) and digest(raw[e['byte_start']:e['byte_end']])==e.get('quote_sha256') for e in evidence):continue
                except (KeyError,TypeError):continue
                replacement=saved['candidate_id'];break
            if not replacement:return None
            proof.append({'candidate_id':old['candidate_id'],'disposition':'rejected_with_verified_same_source_target_result','replacement_candidate_id':replacement})
        return proof
    def complete_row(self,row,raw):
        if row.get('status') not in {'processed','skipped'} or type(row.get('offset')) is not int or row['offset']!=row.get('total_bytes') or row['offset']!=len(raw):return None
        return self.results(row,raw)
    def completed(self,refs):
        """Return exact-content proofs. No production job or receipt is mutated."""
        from .organizer import Organizer
        service=Organizer(self.context);proofs={}
        for ref,job in refs:
            if not job:continue
            for row in job.get('sources',[]):
                try:
                    path=self.context.root/row['source_path']
                    size,sha=_fingerprint(path)
                    if row.get('snapshot_id'):
                        raw=self.snapshots.raw(row)
                        if sha!=row['source_sha256'] or source_identity(self.context,row['source_path'])!=row['source_identity']:continue
                    else:
                        if row.get('version')!=file_version(path.stat()) or sha!=ref.get('source_hashes',{}).get(row['source_path']):continue
                        raw=path.read_bytes()
                    result=self.complete_row(row,raw)
                    if result is not None:proofs[row['source_path']]={'source_sha256':sha,'source_bytes':size,'job_id':job['job_id'],'results':result,'source_identity':source_identity(self.context,row['source_path'])}
                except (OSError,ValueError,KeyError,TypeError):continue
        for receipt in review_rows(self.context):
            try:
                if not receipt or receipt.get('outcome') not in {'written','no_change'}:continue
                relative=receipt['source_path'];path=(self.context.root/relative).resolve()
                if not path.is_relative_to(self.context.raw_archive.resolve()):continue
                if relative!=path.relative_to(self.context.root.resolve()).as_posix():continue
                identity=source_identity(self.context,relative)
                key=tuple(identity.get(k) for k in ('source_key','source_instance','source_session_id'))
                if self.identities.get(key)!=1:continue
                if not all(identity.get(k) and identity[k]==receipt.get(k) for k in ('source_instance','source_session_id')):continue
                if not identity.get('source_key') or identity.get('session_key')!=receipt.get('session_id'):continue
                size,sha=_fingerprint(path)
                if size!=receipt['source_bytes'] or sha!=receipt['source_sha256'] or not _complete(receipt.get('reviewed_ranges',[]),size):continue
                if any(r['from']<0 for r in receipt['reviewed_ranges']):continue
                job=json.loads(service.path(receipt['bridge_job_id']).read_text('utf-8'))
                matches=[r for r in job['sources'] if r['source_path']==relative]
                if len(matches)!=1:continue
                row=matches[0]
                if row['version']!=receipt.get('bridge_version') or not row.get('delivered_authorization_revision'):continue
                if row.get('snapshot_id'):
                    raw=self.snapshots.raw(row)
                    if digest(raw)!=sha:continue
                else:
                    # Without old immutable bytes, a changed stat-version cannot
                    # be retrospectively declared the old fully delivered text.
                    if file_version(path.stat())!=row['version']:continue
                    raw=path.read_bytes()
                result=self.complete_row(row,raw)
                if result is None:continue
                declared=set(receipt.get('candidate_ids',[]));written={p['candidate_id'] for p in result if p.get('writer_version')}
                if declared not in (set(row.get('candidate_ids',[])),written):continue
                if (receipt['outcome']=='written')!=bool(written):continue
                if result is not None:proofs[relative]={'source_sha256':sha,'source_bytes':size,'source_identity':identity,'job_id':job['job_id'],'receipt_sha256':digest(json.dumps(receipt,sort_keys=True).encode()),'results':result}
            except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
        return proofs
