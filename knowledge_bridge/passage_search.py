"""Bounded, read-only passage retrieval. Scores locate sources, not answer truth."""
import codecs,hashlib,math,re,time
from collections import Counter
from pathlib import Path

STOP={'什么','怎么','如何','是否','哪些','多少','为何','为什么','现在','时候','一个','这个','那个','可以','需要','之后','之前','了吗','的是','的旧','哪天','一天','哪一','是谁','采用','还是'}
MAX_SCAN_BYTES=16*1024*1024
MAX_FILE_BYTES=2*1024*1024
MAX_FILES=256

def terms(query):
    query=re.sub('|'.join(sorted(STOP,key=len,reverse=True)),' ',query)
    result=re.findall(r'[a-z0-9]+(?:[._+-][a-z0-9]+)*',query.casefold())
    for run in re.findall(r'[\u3400-\u9fff]+',query):
        result.extend(run[i:i+n] for n in (2,3,4) for i in range(len(run)-n+1) if not any(s in run[i:i+n] for s in STOP))
    return list(dict.fromkeys(result))[:160]

def file_version(stat):
    return hashlib.sha256(str((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)).encode()).hexdigest()

def _passage_identity(text):
    # Export wrappers are not a second piece of evidence. Strip only known
    # leading metadata; retain the returned text and every source coordinate.
    lines=text.splitlines()
    while lines and (not lines[0].strip() or re.match(r'^\s*(?:[-*]\s*)?(?:timestamp|attachment_refs|attachment_status)\s*:',lines[0],re.I)):
        lines.pop(0)
    return re.sub(r'\s+','','\n'.join(lines)).casefold()

def metadata(text):
    match=re.match(r'\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)',text,re.S)
    meta={}
    if match:
        for line in match[1].splitlines():
            key,sep,value=line.partition(':')
            if sep and key in {'project','updated','date','status','source_path','source_sha256','source_entry_id','jiedu_derived',
                               'document_role','valid_from','valid_until','superseded_by','supersedes','source_version'}:
                meta[key]=value.strip().strip('"\'')
    return meta,match.end() if match else 0

def _paragraph_passages(text,begin=0):
    headings=[];offset=0;line_no=1;pending=[];start=begin;start_line=1;message_id=None;message_role=None;message_order=-1
    def row(end):return {'text':text[start:end].strip(),'start':start,'end':end,'line_start':start_line,'line_end':line_no-1,
                         'section_title':' / '.join(h[1] for h in headings),'message_id':message_id,
                         'message_role':message_role,'message_order':message_order}
    for line in text.splitlines(keepends=True):
        heading=re.match(r'^(#{1,6})\s+(.+)',line)
        if offset>=begin:
            # Raw conversations are message records. Blank lines inside one message
            # are formatting, not independent retrieval units.
            if heading or (not line.strip() and message_id is None) or (message_id is not None and pending and offset-start>=1400):
                if pending:yield row(offset);pending=[]
            if heading:
                depth=len(heading[1]);headings=[h for h in headings if h[0]<depth]+[(depth,heading[2].strip())]
                message=re.fullmatch(r'MESSAGE\s+([^｜|\s]+)\s*[｜|]\s*(USER|ASSISTANT)',heading[2].strip(),re.I)
                if message:
                    message_id=message.group(1);message_role=message.group(2).upper();message_order+=1
                else:
                    legacy=re.fullmatch(r'(USER|ASSISTANT|SYSTEM)',heading[2].strip(),re.I)
                    if legacy:
                        message_order+=1;message_id=f'legacy-{message_order+1:06d}';message_role=legacy.group(1).upper()
            elif line.strip():
                # Extremely long single lines are split into bounded passages.
                if not pending:start=offset;start_line=line_no
                pending.append(line)
                if message_id is not None and len(line)>1800:
                    if offset>start:yield row(offset)
                    for part in range(0,len(line),1220):
                        yield {'text':line[part:part+1400].strip(),'start':offset+part,'end':offset+min(part+1400,len(line)),
                               'line_start':line_no,'line_end':line_no,'section_title':' / '.join(h[1] for h in headings),
                               'message_id':message_id,'message_role':message_role,'message_order':message_order}
                    pending=[]
        offset+=len(line);line_no+=1
    if pending:yield row(offset)


def passages(text,begin=0):
    """Keep Markdown premises, quotations and their consequence in one read unit.

    Blank lines are layout, not sentence boundaries. Units may exceed the excerpt
    budget; callers must then expose truncation and continue the pinned source.
    No inferred context crosses a heading, separator or raw-message identity.
    """
    pending=None
    for part in _paragraph_passages(text,begin):
        separator=bool(re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*',part['text']))
        same=bool(pending and not part.get('message_id') and not pending.get('message_id')
                  and pending['section_title']==part['section_title']
                  and not re.search(r'(?m)^\s*(?:#{1,6}\s|[-*_]{3,}\s*$)',text[pending['end']:part['start']]))
        previous=(pending or {}).get('text','').rstrip()
        # Include quoted premises/lists after a colon, incomplete sentences,
        # and explicit continuations even if an earlier block has punctuation.
        unfinished=bool(previous and not re.search(r'[。！？.!?；;][”’"\')）\]]*$',previous))
        continuation=bool(re.match(r'\s*(?:[>\s]*)?(?:才|就|否则|那么|但|然而|因此|所以|除非|并不|也不|而不|而是|方可|then\b|otherwise\b|but\b|unless\b)',part['text'],re.I))
        if same and not separator and (unfinished or continuation):
            pending={**pending,'end':part['end'],'line_end':part['line_end'],
                     'text':text[pending['start']:part['end']].strip(),
                     '_match_texts':pending.get('_match_texts',[pending['text']])+[part['text']]}
        else:
            if pending:yield pending
            pending=None if separator else part
    if pending:yield pending

def _window_context(row,source_passages,remaining):
    if remaining<=0:return None,0
    if row.get('message_id'):
        grouped=[]
        for passage in source_passages:
            if not passage.get('message_id'):continue
            if grouped and grouped[-1]['message_id']==passage['message_id']:
                grouped[-1]['parts'].append(passage)
            else:grouped.append({'message_id':passage['message_id'],'role':passage.get('message_role'),'order':passage.get('message_order',-1),'parts':[passage]})
        index=next((i for i,item in enumerate(grouped) if item['message_id']==row['message_id']),None)
        if index is None:return None,0
        window=grouped[max(0,index-1):min(len(grouped),index+3)];messages=[];used=0;truncated=0
        for item in window:
            body='\n\n'.join(part['text'] for part in item['parts']).strip();keep=min(len(body),900,max(0,remaining-used))
            if keep<=0:break
            first,last=item['parts'][0],item['parts'][-1]
            messages.append({'message_id':item['message_id'],'role':item['role'],'message_order':item['order'],
                             'content':body[:keep],'content_truncated':keep<len(body),'is_hit':item['message_id']==row['message_id'],
                             'position':{'line_start':first['line_start'],'line_end':last['line_end']}})
            used+=keep;truncated+=max(0,len(body)-keep)
        return {'kind':'message_window','messages':messages,'window':{'before':1,'after':2},
                'omitted':{'messages_outside_window':max(0,len(grouped)-len(window)),'characters_due_to_budget':truncated}},used
    index=next((i for i,item in enumerate(source_passages) if item['start']==row.get('start')),None)
    if index is None:return None,0
    window=source_passages[max(0,index-1):min(len(source_passages),index+2)];sections=[];used=0;truncated=0
    for item in window:
        body=item['text'];keep=min(len(body),500,max(0,remaining-used))
        if keep<=0:break
        sections.append({'section_title':item['section_title'],'content':body[:keep],'content_truncated':keep<len(body),
                         'is_hit':item['start']==row.get('start'),'position':{'line_start':item['line_start'],'line_end':item['line_end']}})
        used+=keep;truncated+=max(0,len(body)-keep)
    return {'kind':'section_window','sections':sections,'window':{'before':1,'after':1},
            'omitted':{'passages_outside_window':max(0,len(source_passages)-len(window)),'characters_due_to_budget':truncated}},used

def search(query,scope,context,files,*,limit=10,candidate_budget=80,excerpt_budget=6000,folder=None,project=None,source_type=None,vector_hits=(),requested_mode='keyword'):
    started=time.perf_counter();query=query.strip()
    if not query or len(query)>500:raise ValueError('查询须为 1–500 个字符')
    if folder:
        folder=folder.replace('\\','/').strip('/')
        if any(p in {'','..','.'} for p in folder.split('/')) or ':' in folder:raise ValueError('目录筛选必须是工作区内相对目录')
    if project and any(c in project for c in '/\\'):raise ValueError('项目筛选只接受项目名')
    from .knowledge_entities import KnowledgeEntities
    entities=KnowledgeEntities(context);project_entity=entities.resolve(name=project) if project and scope=='formal' else None
    qt=terms(query);documents=[];skipped=Counter();bytes_read=0;file_count=0;rows=[];source_passages={}
    for path in files:
        try:
            relative=path.resolve().relative_to(context.root.resolve()).as_posix()
            first=relative.split('/')[0]
            if path.is_symlink():continue
            if scope=='workspace':
                from .search_scopes import RESERVED_ROOTS, EXCLUDED_PARTS
                if first.casefold() in {name.casefold() for name in RESERVED_ROOTS}:continue
                if any(part.startswith('.') or part.casefold() in EXCLUDED_PARTS for part in relative.split('/')[:-1]):continue
            elif first not in ({'00_系统','01_当前状态','02_项目','03_知识'} if scope=='formal' else {'90_原始档案'} if scope=='evidence' else {'99_收件箱'}):continue
            if folder and not relative.startswith(folder+'/'):continue
            if project and scope=='formal':
                prefixes=project_entity['paths'] if project_entity else ['02_项目/'+project]
                if not any(relative.startswith(prefix.rstrip('/')+'/') for prefix in prefixes):continue
            if file_count>=MAX_FILES or bytes_read>=MAX_SCAN_BYTES or time.perf_counter()-started>3:
                skipped['scan_budget']+=1;break
            stat=path.stat();version=file_version(stat)
            with path.open('rb') as handle:raw=handle.read(min(MAX_FILE_BYTES,MAX_SCAN_BYTES-bytes_read))
            bytes_read+=len(raw);file_count+=1
            if file_version(path.stat())!=version:skipped['changed_during_scan']+=1;continue
            partial=len(raw)<stat.st_size
            if partial:skipped['file_byte_budget']+=1
            text=codecs.getincrementaldecoder('utf-8')(errors='strict').decode(raw,final=not partial);meta,begin=metadata(text)
            parsed=list(passages(text,begin));source_passages[relative]=parsed
            has_messages=any(item.get('message_id') for item in parsed)
            kind='derived_document' if meta.get('jiedu_derived')=='true' else 'raw_chat' if scope=='evidence' and has_messages else 'source_document' if scope in {'evidence','workspace'} else 'vault_document'
            if source_type and source_type not in {kind,'formal_core' if relative.startswith('00_系统/') else kind}:continue
            doc_terms=set();seen=set();doc_rows=[];previous_char=0;previous_byte=0
            digest=hashlib.sha256(raw).hexdigest() if not partial else None
            for p in parsed:
                body=p['text'];folded=body.casefold();section=p['section_title'];searchable=folded+' '+section.casefold()
                present=[t for t in qt if t in searchable]
                normalized=re.sub(r'\s+','',body).casefold()
                # Existing vectors may contain only one old blank-line fragment.
                # Match those current-source parts, but return/read the whole unit.
                match_texts=[re.sub(r'\s+','',part).casefold() for part in p.get('_match_texts',[body])]
                vector_rank=next((i+1 for i,v in enumerate(vector_hits) if v.get('source_path')==relative and any(part and part in re.sub(r'\s+','',v.get('content','')).casefold() for part in match_texts)),None)
                if (not present and not vector_rank) or normalized in seen:continue
                if vector_hits and requested_mode=='vector' and not vector_rank:continue
                seen.add(normalized);doc_terms.update(present)
                byte_start=previous_byte+len(text[previous_char:p['start']].encode('utf-8'));byte_end=byte_start+len(text[p['start']:p['end']].encode('utf-8'))
                previous_char=p['start'];previous_byte=byte_start
                declared_role=meta.get('document_role','');known_roles={'current_snapshot','historical_record','dated_review','source','evidence','stable_definition'}
                role=declared_role if declared_role in known_roles else 'source' if scope in {'evidence','workspace'} else declared_role
                old=role in {'history','historical_record','dated_review'} or bool(meta.get('superseded_by')) or bool(re.search('历史|旧方案|此前|已被.*取代|暂停点',section))
                temporal='current_authoritative' if role=='current_snapshot' and not meta.get('valid_until') else 'historical_explicit' if old else 'source_record' if role in {'source','evidence'} else 'unspecified'
                dates=re.findall(r'20\d{2}-\d{2}-\d{2}',section)
                vector_hit=vector_hits[vector_rank-1] if vector_rank else {}
                doc_rows.append({**p,'role':p.get('message_role'),'source_path':relative,'document_title':path.stem,'title':path.stem,'scope':scope,'project':meta.get('project') or (Path(relative).parts[1] if relative.startswith('02_项目/') else None),'source_type':kind,'version':version,'source_version':vector_hit.get('source_version') or digest,'session_id':vector_hit.get('session_id'),'sha256':digest,'chunk_id':f'p1-{version}-{byte_start}','position':{'byte_start':byte_start,'byte_end':byte_end,'line_start':p['line_start'],'line_end':p['line_end']},'document_date':meta.get('updated') or meta.get('date'),'section_dates':dates,'document_role':role or None,'validity':{k:meta.get(k) for k in ('valid_from','valid_until','superseded_by','supersedes','source_version') if meta.get(k)},'temporal_status':temporal,'source':{k:v for k,v in meta.items() if k.startswith('source_')},'verification':'ocr_unverified' if kind=='derived_document' and ('OCR' in text[:begin+1500] or 'ocr' in text[:begin+1500]) else 'unconfirmed_inbox' if scope=='inbox' else 'raw_evidence' if scope in {'evidence','workspace'} else 'source_statement','_present':present,'_folded':folded,'_vector_rank':vector_rank})
            documents.append(doc_terms);rows.extend(doc_rows)
        except (OSError,ValueError,UnicodeError):skipped['unreadable_or_encoding']+=1
    df=Counter(t for r in rows for t in set(r['_present']));n=max(1,len(rows));project_names={r['project'] for r in rows if r.get('project')};named=[p for p in project_names if p in query]
    project_only=bool(named) and not any(not any(term in project for project in named) for term in qt)
    historic=bool(re.search(r'以前|当时|旧方案|历史方案|历史决定|最初|早期|20\d{2}-\d{2}-\d{2}',query))
    current_intent=bool(re.search('现在|目前|最新|当前',query)) and not historic
    rows=[r for r in rows if (not named or not r.get('project') or r['project'] in named)
          and (r['_vector_rank'] or (project_only and r.get('project') in named) or
               any(t in r['_folded'] and not any(t in p for p in named) for t in r['_present']))]
    for r in rows:
        matched=r['_present'];body=r['_folded'];section=r['section_title'].casefold()
        score=sum((math.log(1+(n-df[t]+.5)/(df[t]+.5)))*(1+math.log(1+min(body.count(t),3)) if t in body else .45)*(len(t)**.7) for t in matched)
        score/=1+max(0,len(body)-300)/1800
        if query.casefold() in body:score+=8
        if named and r.get('project') not in named:score*=.2
        if r['temporal_status']=='current_authoritative':score*=.72 if historic else 1.65
        elif r['temporal_status']=='historical_explicit':score*=1.35 if historic else .48
        if historic and any(d in query for d in r['section_dates']):score*=1.4
        if not historic and re.search('最新|当前|已确认',section):score*=1.15
        # A vector row may have been embedded with bounded neighboring context.
        # Keep the semantic rank, but also reward evidence present in the text
        # we actually return so a short adjacent boilerplate message cannot
        # outrank an exact current decision merely because its neighbor matched.
        r['_lexical_score']=score
        if r['_vector_rank']:score+=100/(20+r['_vector_rank'])
        r['score']=round(score,6);r['match_count']=r['score'];r['matched_terms']=matched[:20]
    if requested_mode=='hybrid' and any(r['_vector_rank'] for r in rows):
        # Fuse ranks, not incomparable raw lexical scores and semantic ranks.
        # Otherwise numerous overlapping Chinese n-grams swamp semantic hits.
        lexical=sorted((r for r in rows if r['_lexical_score']>0),key=lambda r:-r['_lexical_score'])
        for rank,r in enumerate(lexical,1):r['_lexical_rank']=rank
        for r in rows:
            r['score']=round(sum(1/(60+rank) for rank in (r.get('_lexical_rank'),r['_vector_rank']) if rank),8)
            r['match_count']=r['score']
    rows.sort(key=lambda r:(bool(current_intent and r['temporal_status']=='historical_explicit'),-r['score'],r['source_path'],r['start']))
    # Candidate computation and returned body are separately bounded.
    candidates=rows[:max(limit,min(200,candidate_budget))];selected=[];selected_rows=[];seen={};per_file=Counter();used=0
    for r in candidates:
        key=_passage_identity(r['text'])
        if key in seen:
            sources=seen[key].setdefault('duplicate_sources',[])
            if len(sources)<5:sources.append({'source_path':r['source_path'],'chunk_id':r['chunk_id'],'message_id':r.get('message_id')})
            continue
        if per_file[r['source_path']]>=2 or len(selected)>=limit or used>=excerpt_budget:continue
        text=r['text'];keep=min(len(text),1200,excerpt_budget-used)
        if keep<=0:break
        per_file[r['source_path']]+=1
        out={k:v for k,v in r.items() if not k.startswith('_') and k not in {'text','start','end','line_start','line_end'}}
        out.update(content=text[:keep],excerpt=text[:keep],excerpt_truncated=keep<len(text),rank=len(selected)+1,search_method='keyword',
                   support={'status':'candidate_only','answer_supported':False,'basis':'search_match_requires_source_read'},
                   read={'source_path':r['source_path'],'chunk_id':r['chunk_id'],'page_bytes':8192})
        selected.append(out);selected_rows.append(r);seen[key]=out;used+=keep
    context_limit=min(4000,max(0,excerpt_budget));context_used=0;context_candidates=0
    for out,row in zip(selected,selected_rows):
        window,window_used=_window_context(row,source_passages.get(row['source_path'],[]),context_limit-context_used)
        if window:
            out['context']=window;context_used+=window_used
            context_candidates+=len(window.get('messages',window.get('sections',[])))
    return {'query':query,'scope':scope,'results':selected,'returned':len(selected),'matched_files':len({r['source_path'] for r in rows}),'matched_passages':len(rows),'vector_matched_passages':sum(bool(r['_vector_rank']) for r in rows),'context_passages':context_candidates,'candidate_count':len(candidates),'boundary':'explicit','vector_used':False,'answer_supported':False,'answer_status':'candidates_require_reading' if selected else 'no_supported_match_in_scanned_content','scan':{'files':file_count,'bytes':bytes_read,'complete':not skipped,'limits':{'files':MAX_FILES,'bytes':MAX_SCAN_BYTES,'bytes_per_file':MAX_FILE_BYTES,'seconds':3},'omitted':dict(skipped)},'budgets':{'candidate_limit':candidate_budget,'result_limit':limit,'excerpt_characters_limit':excerpt_budget,'returned_characters':used,'context_characters_limit':context_limit,'context_returned_characters':context_used},'notice':'检索只定位候选出处，不代表答案已确认或完成历史审查；请展开正文核对时间与来源。','elapsed_ms':round((time.perf_counter()-started)*1000,3)}
