"""Read-only metadata audit of archived tool payloads; never repairs history.

Only raw-messages, archive registry/aliases and registered canonical JSONL are
read. No application imports, source DB/rollout lookup, body heuristics or apply
mode. Reports contain identifiers, hashes and allowlisted structural facts.
"""
from __future__ import annotations
import argparse,hashlib,json,os,re,sys,tempfile
from pathlib import Path

SCHEMA='jiedu-tool-payload-history-audit-v1'
MAX_ROW_BYTES=16*1024*1024
TOOL_TYPES=frozenset({'function_call','function_call_output','custom_tool_call','custom_tool_call_output','tool_call','tool_output','tool_result','tool_search_call','tool_search_output','web_search_call'})
TOOL_ROUTES=('functions.','tools.','multi_tool_use.','mcp__','web.','python.','container.','browser.','file_search.','canmore.')
TOOL_ROLES=frozenset({'tool','function','tool_result','tool_output','output','function_call_output','custom_tool_call_output'})
KNOWN_ROLES=frozenset({'user','assistant','system','developer'})|TOOL_ROLES
KNOWN_PHASES=frozenset({'analysis','reasoning','commentary','final','final_answer'})

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _id(value):
    if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_:.@/+~\-]{1,256}',value):return value
    return 'id-sha256:'+digest(value)

def classify_record(row):
    """Classify only explicit structure. All findings require human review."""
    if not isinstance(row,dict):return {'classification':'keep','reasons':['unknown_record_shape'],'structured_facts':{}}
    provenance=row.get('provenance') if isinstance(row.get('provenance'),dict) else {}
    role=str(row.get('role') or '').casefold()
    source_role=str(provenance.get('source_role') or '').casefold()
    kind=row.get('type') or row.get('message_type') or provenance.get('source_payload_type')
    recipient=provenance.get('source_recipient',row.get('recipient'))
    route=recipient.casefold() if isinstance(recipient,str) else ''
    channel=provenance.get('source_channel',row.get('channel'))
    phase=provenance.get('assistant_phase',row.get('phase'))
    internal_analysis=channel in {'analysis','reasoning'} if isinstance(channel,str) else False
    internal_analysis=internal_analysis or (phase in {'analysis','reasoning'} if isinstance(phase,str) else False)
    tool_kind=kind in TOOL_TYPES if isinstance(kind,str) else False
    tool_route=bool(route and (route.startswith(TOOL_ROUTES) or route in {'python','browser','container','web'}))
    body=row.get('original_text') if row.get('original_text') is not None else row.get('content')
    reasons=[]
    if role in TOOL_ROLES:reasons.append('explicit_tool_role')
    if source_role in TOOL_ROLES:reasons.append('explicit_source_tool_role')
    if tool_kind:reasons.append('explicit_tool_payload_type')
    if tool_route:reasons.append('known_tool_execution_route')
    facts={'role':role if role in KNOWN_ROLES else 'unknown','source_role':source_role if source_role in KNOWN_ROLES else 'unknown','payload_type':kind if tool_kind else 'other_or_absent',
           'known_tool_route':tool_route,'analysis_marker':bool(internal_analysis),
           'assistant_body_with_tool_calls':bool(role=='assistant' and isinstance(body,str) and body.strip() and row.get('tool_calls')),
           'historical_supplement':row.get('archive_kind')=='historical_supplement' or provenance.get('archive_kind')=='historical_supplement'}
    if reasons:return {'classification':'needs_review','reasons':reasons,'structured_facts':facts}
    if internal_analysis:return {'classification':'audit_only','reasons':['analysis_marker_preserved'],'structured_facts':facts}
    return {'classification':'keep','reasons':[],'structured_facts':facts}

def _no_links(path):
    path=Path(os.path.abspath(path))
    for part in (path,*path.parents):
        if part.is_symlink() or getattr(part,'is_junction',lambda:False)():raise ValueError('symlink_or_junction_rejected')
    return path

def _object(path,default):
    if not path.exists():return default
    if not path.is_file():raise ValueError('input_not_regular_file')
    with path.open('r',encoding='utf-8-sig') as stream:value=json.load(stream)
    if not isinstance(value,dict):raise ValueError('metadata_object_required')
    return value

def _resolve(value,links):
    value=str(value or '');seen=set()
    while value in links:
        if value in seen or not isinstance(links[value],str):raise ValueError('ambiguous_alias_map')
        seen.add(value);value=links[value]
        if len(seen)>64:raise ValueError('alias_chain_too_long')
    return value

def _fingerprint(path):
    before=path.stat();hasher=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):hasher.update(block)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('input_changed_during_audit')
    return hasher.hexdigest(),before.st_size

def _output_guard(workspace,output,inputs):
    output=_no_links(output)
    if output.suffix.casefold()!='.json':raise ValueError('output_must_be_json')
    resolved=output.resolve()
    if resolved.is_relative_to(workspace):
        parts=resolved.relative_to(workspace).parts
        if len(parts)<3 or parts[:2] not in {('.jiedu','artifacts'),('.jiedu','audit')}:raise ValueError('output_cannot_target_workspace_data_or_state')
    for source in inputs:
        if resolved==source or resolved.is_relative_to(source) or source.is_relative_to(resolved):raise ValueError('output_overlaps_input')
        if output.exists() and os.path.samefile(output,source):raise ValueError('output_aliases_input')
    if output.exists():
        if not output.is_file() or _object(output,{}).get('schema')!=SCHEMA:raise ValueError('refuse_overwrite_non_audit_report')
    return output

def audit(workspace,output):
    workspace=_no_links(workspace).resolve()
    if not workspace.is_dir():raise ValueError('workspace_directory_required')
    state=_no_links(workspace/'.jiedu/state')
    raw_root=_no_links(state/'raw-messages')
    registry_path=_no_links(state/'archive-sessions.json')
    aliases_path=_no_links(state/'archive-session-aliases.json')
    metadata_inputs=[p.resolve() for p in (registry_path,aliases_path) if p.exists()]
    registry=_object(registry_path,{'sessions':{}}).get('sessions',{})
    alias_data=_object(aliases_path,{})
    aliases=alias_data.get('aliases',{});message_aliases=alias_data.get('message_aliases',{})
    if not isinstance(registry,dict) or not isinstance(aliases,dict) or not isinstance(message_aliases,dict):raise ValueError('invalid_registry_or_aliases')
    sources=[];gaps=[]
    if not registry_path.exists():gaps.append({'kind':'archive_registry_missing'})
    if not aliases_path.exists():gaps.append({'kind':'archive_alias_map_missing'})
    if raw_root.exists():
        if not raw_root.is_dir():raise ValueError('raw_messages_directory_required')
        for path in sorted(raw_root.glob('*.jsonl')):
            path=_no_links(path)
            if not path.is_file():raise ValueError('raw_input_not_regular_file')
            sources.append(('raw',path.resolve(),None))
    else:gaps.append({'kind':'raw_messages_directory_missing'})
    for key,item in sorted(registry.items()):
        if not isinstance(item,dict):raise ValueError('invalid_registry_session')
        relative=item.get('relative_path')
        if not isinstance(relative,str):gaps.append({'kind':'registered_path_missing','session_id':_id(key)});continue
        relative_path=Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts or not relative_path.parts or relative_path.parts[0]!='90_原始档案':raise ValueError('registered_path_escape_or_wrong_root')
        folder=_no_links(workspace/relative_path)
        if not folder.resolve().is_relative_to(workspace/'90_原始档案'):raise ValueError('registered_path_escape')
        path=_no_links(folder/'03_元数据/messages.jsonl')
        if path.is_file():sources.append(('canonical',path.resolve(),str(key)))
        else:gaps.append({'kind':'registered_canonical_jsonl_missing','session_id':_id(key)})
    inputs=metadata_inputs+[path for _,path,_ in sources]
    output=_output_guard(workspace,output,inputs)
    # Fingerprint metadata before processing and verify again before committing.
    metadata_hashes={str(path):_fingerprint(path) for path in metadata_inputs}
    counts={'raw_files':0,'raw_records':0,'canonical_files':0,'canonical_records':0,'needs_review':0,'audit_only':0,'kept':0,'bad_rows':0,
            'oversized_rows':0,'partial_rows':0,'duplicate_ids':0,'alias_equivalent_rows':0,'body_hash_mismatches':0,'historical_supplement_rows':0}
    findings=[];inventory=[];logical_seen={};physical_sources=set()
    for view,path,registered_key in sources:
        if (view,str(path)) in physical_sources:continue
        physical_sources.add((view,str(path)));counts[view+'_files']+=1
        before=path.stat();file_hasher=hashlib.sha256();rows=0;seen=set();offset=0
        file_id=digest([view,registered_key or path.stem])
        with path.open('rb') as stream:
            while True:
                start=offset;line=stream.readline(MAX_ROW_BYTES+1)
                if not line:break
                file_hasher.update(line);offset+=len(line)
                if len(line)>MAX_ROW_BYTES:
                    while line and not line.endswith(b'\n'):
                        line=stream.readline(MAX_ROW_BYTES+1);file_hasher.update(line);offset+=len(line)
                    counts['oversized_rows']+=1;continue
                if not line.endswith(b'\n'):counts['partial_rows']+=1;continue
                if not line.strip():continue
                try:row=json.loads(line)
                except (ValueError,UnicodeError):counts['bad_rows']+=1;continue
                if not isinstance(row,dict):counts['bad_rows']+=1;continue
                rows+=1;counts[view+'_records']+=1
                sid=str(registered_key or row.get('session_key') or row.get('session_id') or path.stem)
                canonical_sid=_resolve(sid,aliases)
                links=message_aliases.get(canonical_sid,{})
                if not isinstance(links,dict):raise ValueError('invalid_message_aliases')
                mid=row.get('message_id')
                if not isinstance(mid,str) or not mid:
                    gaps.append({'kind':'message_id_missing','file_id':file_id,'byte_offset':start});continue
                canonical_mid=_resolve(mid,links)
                physical_key=(sid,mid)
                if physical_key in seen:counts['duplicate_ids']+=1
                seen.add(physical_key)
                body=row.get('original_text') if row.get('original_text') is not None else row.get('content','')
                actual_hash=hashlib.sha256(body.encode('utf-8')).hexdigest() if isinstance(body,str) else None
                declared=row.get('content_sha256') or row.get('hash')
                if declared and declared!=actual_hash:counts['body_hash_mismatches']+=1
                logical_key=(view,canonical_sid,canonical_mid,actual_hash)
                origin=(sid,mid)
                if logical_key in logical_seen and logical_seen[logical_key]!=origin:counts['alias_equivalent_rows']+=1
                logical_seen.setdefault(logical_key,origin)
                result=classify_record(row)
                if result['structured_facts'].get('historical_supplement'):counts['historical_supplement_rows']+=1
                classification=result['classification'];counts[{'needs_review':'needs_review','audit_only':'audit_only','keep':'kept'}[classification]]+=1
                if classification!='keep':
                    identity={'session_id':_id(canonical_sid),'message_id':_id(canonical_mid),'body_sha256':actual_hash}
                    finding_id=digest([identity,result['classification'],result['reasons'],result['structured_facts']])
                    findings.append({'finding_id':finding_id,'view':view,'file_id':file_id,'byte_offset':start,'raw_line_sha256':hashlib.sha256(line).hexdigest(),
                                     **identity,**result,'source_binding_checked':False,'action':'review_only_keep_unchanged'})
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('input_changed_during_audit')
        inventory.append({'file_id':file_id,'view':view,'sha256':file_hasher.hexdigest(),'bytes':before.st_size,'records':rows})
    for path in metadata_inputs:
        if _fingerprint(path)!=metadata_hashes[str(path)]:raise ValueError('registry_or_alias_changed_during_audit')
    coverage={'raw_messages':True,'registered_canonical_jsonl_only':True,'source_binding_checked':False,'source_db_checked':False,'rollout_checked':False,
              'body_used_only_for_transient_hash':True,'body_heuristics_used':False,'unregistered_legacy_projection_checked':False,
              'analysis_markers_preserved':True,'assistant_body_with_tool_calls_preserved':True}
    gaps.extend([{'kind':'source_binding_not_checked'},{'kind':'unknown_or_missing_markers_preserved'},{'kind':'supplement_web_doubao_require_independent_source_evidence'},
                 {'kind':'unregistered_legacy_and_markdown_projections_not_scanned'}])
    metadata_inventory=[{'kind':'registry' if path==registry_path.resolve() else 'aliases','sha256':metadata_hashes[str(path)][0],'bytes':metadata_hashes[str(path)][1]} for path in metadata_inputs]
    findings.sort(key=lambda item:(item['finding_id'],item['view'],item['file_id'],item['byte_offset']))
    report={'schema':SCHEMA,'read_only':True,'source_binding_checked':False,'proposed_changes':[],'production_changes':0,
            'counts':counts,'coverage':coverage,'gaps':gaps,'inputs':sorted(inventory,key=lambda item:(item['view'],item['file_id'])),'metadata_inputs':metadata_inventory,
            'findings':findings,'claim':'Metadata tool-payload candidates require source-bound human review. Zero metadata hits are not proof that every historical source was verified.'}
    report['fingerprint']=digest(report)
    output=_output_guard(workspace,output,inputs)
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=output.parent,prefix='.'+output.name+'.',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);json.dump(report,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n')
        os.replace(temporary,output)
    finally:
        if temporary and temporary.exists():temporary.unlink()
    return report

def main(argv=None):
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--workspace',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:report=audit(args.workspace,args.output)
    except (OSError,ValueError,TypeError):print('audit_failed: invalid_or_changed_input_or_unsafe_output',file=sys.stderr);return 2
    print(json.dumps({'schema':SCHEMA,'fingerprint':report['fingerprint'],'counts':report['counts'],'proposed_changes':[],'production_changes':0},sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
