"""Canonical, human-readable mirror of append-only L0 session evidence."""
from __future__ import annotations
import hashlib,json,os,re,shutil,uuid
from datetime import datetime,timezone
from pathlib import Path
from .session_manager import ARCHIVE_ROOT,SESSIONS_PATH,_archive_path
from .core import ROOT,VAULT
from .workspace import active_workspace
from .conversation_visibility import exclusion_aliases, exclusion_reason
CANONICAL_ROOT=active_workspace().raw_archive
ATTACHMENT_MANIFEST=active_workspace().state/"attachments"/"manifest.jsonl"
DISPLAY_CACHE=active_workspace().cache/"archive-display.json"
CLIENT_NAMES={"chatgpt":"ChatGPT","hermes":"Hermes","codex":"ChatGPT"}
STANDARD_MESSAGE_HEADING=re.compile(r"(?m)^## MESSAGE\s+([^\s｜|]+)\s*[｜|]\s*(USER|ASSISTANT|SYSTEM)\s*$")
GENERIC_ARCHIVE_TITLE=re.compile(r"^(?:未命名对话(?:\s+\d{4}-\d{2}-\d{2}(?:\s+\d{4})?)?(?:\s+\[[0-9a-f]{8}\])?|[AB]|[0-9a-f]{12}|(?:hermes|chatgpt)(?:\s*[·_-].*)?)$",re.I)
def _read_json(path,default):
 try:return json.loads(path.read_text("utf-8"))
 except (OSError,ValueError):return default
def _read_jsonl_optional(path):
 return _read_message_rows(path) if path.is_file() else []

def _display_cache():
 value=_read_json(DISPLAY_CACHE,{})
 return value.get("sessions",{}) if isinstance(value,dict) else {}

def _recorded_archive_title(manifest,archive_folder,classified):
 # Canonical identity owns its current title; collision suffixes are only paths.
 if manifest.get('session_key'):
  return manifest.get('title') or manifest.get('session_title') or archive_folder.name
 # Older imported layouts still support owner-renamed archive directories.
 if classified and archive_folder.parent.name.casefold() in CLIENT_NAMES:return archive_folder.name
 return manifest.get('session_title') or manifest.get('title')

def _apply_display_title(item,display):
 title=item.get('session_title')
 valid=isinstance(display.get('display_title'),str) and bool(display['display_title'].strip()) and 'original_title' in display and display.get('title_origin') in {'recorded','content_inferred','fallback'} and display.get('original_title')==title and (display.get('display_title')==title or (display.get('title_origin')=='content_inferred' and (not title or GENERIC_ARCHIVE_TITLE.fullmatch(title))))
 metadata={k:display[k] for k in ('related_projects','relation_origin') if k in display}
 metadata.setdefault('related_projects',[]);metadata.setdefault('relation_origin',None)
 if valid:metadata.update({k:display[k] for k in ('display_title','original_title','title_origin') if k in display})
 else:metadata.update(display_title=title or '未命名对话',original_title=title,title_origin='recorded' if title else 'fallback')
 item.update(metadata)
 return item

def _clean_inferred_title(text,limit=54):
 text=re.sub(r"<[^>]+>"," ",str(text or ""))
 text=re.sub(r"```.*?```"," ",text,flags=re.S)
 text=re.sub(r"[#>*_`\[\]{}]"," ",text)
 text=re.sub(r"\s+"," ",text).strip(" ：:，,。.!！?？-—")
 if not text:return None
 return text[:limit].rstrip()+("…" if len(text)>limit else "")

def _assistant_title(text):
 cleaned=_clean_inferred_title(text)
 if not cleaned:return None
 raw=str(text or "").strip()
 if (raw.startswith("{") and raw.endswith("}")) or re.match(r"^(?:selected_source|standard_visual_qa|qa_note)\s*=",raw,re.I):return None
 return cleaned

def _message_preview(metadata_folder,byte_budget=131072):
 path=metadata_folder/"messages.jsonl"
 if not path.is_file():path=metadata_folder/"messages.json"
 if not path.is_file():return [],""
 try:
  if path.suffix.casefold()==".json":rows=_read_message_rows(path)[:80]
  else:
   rows=[];used=0
   with path.open("r",encoding="utf-8") as stream:
    for line in stream:
     used+=len(line.encode("utf-8"))
     try:rows.append(json.loads(line))
     except ValueError:pass
     if used>=byte_budget or len(rows)>=80:break
 except (OSError,ValueError):return [],""
 text="\n".join(str(row.get("original_text") or row.get("content") or "") for row in rows)
 return rows,text

def rebuild_archive_display_cache():
 """Build reversible display metadata without changing archive evidence."""
 projects=[path.name for path in active_workspace().projects.iterdir() if path.is_dir()] if active_workspace().projects.exists() else []
 aliases={name:{name.casefold()} for name in projects}
 common={"界渡":{"界渡","knowledge bridge","jiedu"},"学校网站":{"学校网站","总控导演台"},"小龚":{"小龚"},"明证":{"明证"},"淘宝":{"淘宝"},"AI教学":{"ai教学","ai 教学"}}
 for name,values in common.items():
  if name in aliases:aliases[name].update(x.casefold() for x in values)
 sessions={}
 for manifest_path in _manifest_paths():
  summary=_manifest_summary(manifest_path)
  if not summary:continue
  original=str(summary.get("session_title") or "").strip()
  rows,text=_message_preview(manifest_path.parent)
  display=original;origin="recorded" if original else "fallback"
  generic=not original or bool(GENERIC_ARCHIVE_TITLE.fullmatch(original))
  if generic:
   first_user=next((row for row in rows if str(row.get("role","")).casefold()=="user" and _clean_inferred_title(row.get("original_text") or row.get("content"))),None)
   first_substantive=first_user or next((row for row in rows if str(row.get("role","")).casefold()=="assistant" and _assistant_title(row.get("original_text") or row.get("content"))),None)
   inferred=_clean_inferred_title((first_substantive or {}).get("original_text") or (first_substantive or {}).get("content"))
   if inferred:display=f"推定｜{inferred}";origin="content_inferred"
   elif not rows:origin="no_content"
  related=[name for name,terms in aliases.items() if any(term in text.casefold() for term in terms)]
  sessions[summary["session_id"]]={"display_title":display or original or "未命名对话","original_title":original or None,"title_origin":origin,"related_projects":related,"relation_origin":"content_inferred" if related else None}
 payload={"version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"sessions":sessions}
 DISPLAY_CACHE.parent.mkdir(parents=True,exist_ok=True);temporary=DISPLAY_CACHE.with_name(f".{DISPLAY_CACHE.name}.{uuid.uuid4().hex}.tmp")
 temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");os.replace(temporary,DISPLAY_CACHE)
 return {"sessions":len(sessions),"inferred_titles":sum(x["title_origin"]=="content_inferred" for x in sessions.values()),"related_sessions":sum(bool(x["related_projects"]) for x in sessions.values()),"path":str(DISPLAY_CACHE)}

def _read_message_rows(path):
 text=path.read_text("utf-8")
 if path.suffix.casefold()==".json":
  value=json.loads(text);return value if isinstance(value,list) else []
 rows=[]
 for line in text.splitlines():
  if not line.strip():continue
  try:row=json.loads(line)
  except ValueError:continue
  if isinstance(row,dict):rows.append(row)
 return rows
def _standardized_rows(files,session_id):
 rows=[]
 for path in files:
  text=path.read_text("utf-8-sig");matches=list(STANDARD_MESSAGE_HEADING.finditer(text))
  for index,match in enumerate(matches):
   body=text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)].strip("\r\n")
   rows.append({"client_id":"chatgpt","session_id":session_id,"message_id":match.group(1),"role":match.group(2).lower(),"timestamp":None,"original_text":body,"message_order":len(rows)+1,"attachment_refs":[],"attachments":[],"hash":hashlib.sha256(body.encode("utf-8")).hexdigest(),"provenance":{"kind":"standardized_archive"}})
 return rows
def _session_dir(item):
 safe=hashlib.sha256(item["session_id"].encode()).hexdigest()[:12]
 client=CLIENT_NAMES.get(str(item["client_id"]).lower(),item["client_id"])
 return CANONICAL_ROOT/client/safe
def _canonical_item(folder, *, include_conversation=True):
 # Canonical sessions may use either the legacy flat layout or the
 # owner-facing classified layout:
 #   <session>/03_元数据/manifest.json
 #   <session>/02_标准化对话/conversation.md
 # Keeping this resolution here lets the archive be reorganized without
 # rewriting any evidence or hashes.
 metadata_folder=folder
 classified=folder.name=="03_元数据"
 archive_folder=folder.parent if classified else folder
 manifest_path=metadata_folder/"manifest.json"
 manifest=_read_json(manifest_path,{})
 messages_path=metadata_folder/"messages.jsonl"
 if not messages_path.is_file():messages_path=metadata_folder/"messages.json"
 conversation_path=(archive_folder/"01_对话"/"conversation.md") if classified else (archive_folder/"conversation.md")
 if classified and not conversation_path.is_file():conversation_path=archive_folder/"02_标准化对话"/"conversation.md"
 if not manifest:return None
 raw_source=None
 if classified:
  candidates=sorted((archive_folder/"00_原始文件").glob("*.txt"))
  raw_source=candidates[0] if candidates else None
 if messages_path.is_file():
  try:rows=_read_message_rows(messages_path)
  except (OSError,ValueError):return None
 elif raw_source and raw_source.is_file():
  legacy_id=f"chatgpt:archive:{str(manifest.get('sha256') or hashlib.sha256(str(archive_folder).encode()).hexdigest())[:16]}"
  standard_files=sorted((archive_folder/"02_标准化对话").glob("*.md"))
  rows=_standardized_rows(standard_files,legacy_id)
 else:return None
 client=str(manifest.get("source_key") or manifest.get("client") or manifest.get("source") or archive_folder.parent.name).lower()
 if client not in CLIENT_NAMES:
  for key,label in CLIENT_NAMES.items():
   if label.lower()==client:client=key;break
 session_id=str(manifest.get("session_key") or manifest.get("session_id") or (rows[0].get("session_id") if rows else None) or archive_folder.name)
 started=manifest.get("started_at") or (rows[0].get("timestamp") if rows else None)
 updated=manifest.get("updated_at") or manifest.get("ended_at") or (rows[-1].get("timestamp") if rows else started)
 user_count=manifest.get("user_count",sum(str(x.get("role","")).lower()=="user" for x in rows))
 assistant_count=manifest.get("assistant_count",sum(str(x.get("role","")).lower()=="assistant" for x in rows))
 title=_recorded_archive_title(manifest,archive_folder,classified)
 if not title:
  heading=(conversation_path.read_text("utf-8").splitlines()[0].lstrip("# ").strip() if conversation_path.is_file() else "")
  title=heading if heading and session_id not in heading else None
 item=dict(manifest)
 conversation=(conversation_path.read_text("utf-8") if conversation_path.is_file() else (raw_source.read_text("utf-8-sig") if raw_source and raw_source.is_file() else "")) if include_conversation else ""
 if classified and "attachment_count" not in item:
  attachment_folder=(archive_folder/"02_附件") if (archive_folder/"02_附件").exists() else (archive_folder/"01_图片附件")
  item["attachment_count"]=len(list(attachment_folder.glob("*")))
 item.update({"client":client,"source":manifest.get("session_source") or manifest.get("source") or client,"session_id":session_id,"session_title":title,"started_at":started,"updated_at":updated,"message_count":len(rows),"user_count":user_count,"assistant_count":assistant_count,"turn_count":manifest.get("turn_count",min(user_count,assistant_count)),"archive_path":str(archive_folder),"conversation":conversation,"messages":rows})
 return item
def _manifest_paths():
 # Prefer normalized layouts while keeping old paths and their original bytes.
 records=_read_json(active_workspace().state/'legacy-layouts.json',{'sessions':{}})['sessions'].values()
 replaced={str((active_workspace().root/r['source_path']).resolve()) for r in records if r['source_path']!=r['target_path']}
 return [p for p in CANONICAL_ROOT.glob("*/**/manifest.json") if '00_原始数据' not in p.parts and str(p.parent.resolve()) not in replaced]

def _find_canonical(session_id):
 if not CANONICAL_ROOT.exists():return None
 for manifest in _manifest_paths():
  item=_canonical_item(manifest.parent)
  if item and session_id in {item["session_id"],item.get("source_session_id")}:return item
 return None

def _file_signature(path):
 if not path.is_file():return None
 stat=path.stat();return {"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}

def _archive_summary_stats(metadata_folder):
 """Return cached counts while streaming legacy JSONL only when it changed."""
 messages_path=metadata_folder/"messages.jsonl"
 if not messages_path.is_file():messages_path=metadata_folder/"messages.json"
 attachments_path=metadata_folder/"attachments.jsonl"
 signatures={"messages":_file_signature(messages_path),"attachments":_file_signature(attachments_path)}
 cache_path=metadata_folder/"archive-summary.json"
 cached=_read_json(cache_path,{})
 if cached.get("version")==8 and cached.get("signatures")==signatures:
  return cached.get("counts",{})
 roles={};message_count=0;message_errors=0;inline_latest={};visible_message_ids=set()
 def observe_message(row):
  nonlocal message_count
  message_count+=1;role=str(row.get("role") or "").casefold();roles[role]=roles.get(role,0)+1
  visible_message_ids.add(str(row.get('message_id') or row.get('source_message_id') or ''))
  for attachment in row.get("attachments") or row.get("attachment_refs") or []:
   if isinstance(attachment,dict):
    key=(attachment.get("source_message_id") or row.get("message_id"),attachment.get("source_reference") or attachment.get("reference") or attachment.get("id") or attachment.get("stored_path"))
    inline_latest[key]=attachment|{"source_message_id":key[0]}
 if signatures["messages"] is not None:
  try:
   if messages_path.suffix.casefold()==".json":
    for row in _read_message_rows(messages_path):
     if isinstance(row,dict):observe_message(row)
     else:message_errors+=1
   else:
    with messages_path.open("r",encoding="utf-8") as stream:
     for line in stream:
      if not line.strip():continue
      try:row=json.loads(line)
      except ValueError:message_errors+=1;continue
      if not isinstance(row,dict):message_errors+=1;continue
      observe_message(row)
  except (OSError,ValueError):message_errors+=1
 latest={};attachment_errors=0;all_entity_candidates=set()
 if signatures["attachments"] is not None:
  try:
   with attachments_path.open("r",encoding="utf-8") as stream:
    for line in stream:
     if not line.strip():continue
     try:row=json.loads(line)
     except ValueError:attachment_errors+=1;continue
     if not isinstance(row,dict):attachment_errors+=1;continue
     latest[(row.get("source_message_id"),row.get("source_reference") or row.get("reference") or row.get("id"))]=row
     stored=row.get("archived_path") or row.get("stored_path")
     if stored:all_entity_candidates.add(str(stored))
  except OSError:attachment_errors+=1
 elif inline_latest:
  latest=inline_latest
  all_entity_candidates={str(row.get("archived_path") or row.get("stored_path")) for row in latest.values() if row.get("archived_path") or row.get("stored_path")}
 if signatures['messages'] is not None and not message_errors:latest={key:row for key,row in latest.items() if str(key[0]) in visible_message_ids}
 all_entity_candidates={str(row.get('archived_path') or row.get('stored_path')) for row in latest.values() if row.get('archived_path') or row.get('stored_path')}
 entity_paths=set()
 workspace_root=active_workspace().root.resolve()
 for stored in all_entity_candidates:
  candidate=Path(stored);candidate=candidate if candidate.is_absolute() else workspace_root/candidate
  try:
   resolved=candidate.resolve()
   if resolved.is_file() and (resolved==workspace_root or workspace_root in resolved.parents):entity_paths.add(str(resolved))
  except OSError:pass
 available=sum(str(row.get("status") or "").casefold() in {"available","mapped"} for row in latest.values())
 missing=sum(str(row.get("status") or "").casefold() not in {"available","mapped"} for row in latest.values())
 counts={"messages_state":"absent" if signatures["messages"] is None else ("partial" if message_errors else "ok"),"attachments_state":"absent" if signatures["attachments"] is None and not inline_latest else ("partial" if attachment_errors else "ok"),
         "message_count":message_count,"user_count":roles.get("user",0),"assistant_count":roles.get("assistant",0),"system_count":roles.get("system",0),
         "turn_count":min(roles.get("user",0),roles.get("assistant",0)),"attachment_count":len(latest),
         "available_attachment_count":available,"missing_attachment_count":missing,"attachment_entity_count":len(entity_paths),
         "message_parse_error_count":message_errors,"attachment_parse_error_count":attachment_errors}
 from .attachment_reference_stats import summarize
 counts['attachment_references']=summarize(list(latest.values()))
 try:
  temporary=cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
  temporary.write_text(json.dumps({"version":8,"signatures":signatures,"counts":counts},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
  os.replace(temporary,cache_path)
 except OSError:pass
 return counts

def _manifest_summary(manifest_path):
 metadata_folder=manifest_path.parent;classified=metadata_folder.name=="03_元数据"
 archive_folder=metadata_folder.parent if classified else metadata_folder
 manifest=_read_json(manifest_path,{})
 if not manifest:return None
 client=str(manifest.get("source_key") or manifest.get("client") or manifest.get("source") or archive_folder.parent.name).lower()
 if client not in CLIENT_NAMES:
  for key,label in CLIENT_NAMES.items():
   if label.lower()==client:client=key;break
 session_id=str(manifest.get("session_key") or manifest.get("session_id") or "")
 if not session_id:
  conversion=_read_json(metadata_folder/"layout-conversion.json",{})
  session_id=conversion.get("session_id","")
 if not session_id:return None
 title=_recorded_archive_title(manifest,archive_folder,classified)
 derived=_archive_summary_stats(metadata_folder)
 observed=derived.get("messages_state") in {"ok","partial"} and (derived.get("message_count",0)>0 or derived.get("messages_state")=="ok")
 user_count=int(derived.get("user_count") if observed else manifest.get("user_count") or manifest.get("user_messages") or 0);assistant_count=int(derived.get("assistant_count") if observed else manifest.get("assistant_count") or manifest.get("assistant_messages") or 0)
 message_count=int(derived.get("message_count") if observed else manifest.get("message_count") or manifest.get("total_messages") or user_count+assistant_count)
 attachments_observed=derived.get("attachments_state") in {"ok","partial"}
 manifest_attachment_count=int(manifest.get("attachment_count") or len(manifest.get('image_attachments_found') or []))
 manifest_missing=int(manifest.get("missing_attachment_count") or len(manifest.get('image_attachments_missing') or []))
 attachment_count=int(derived.get("attachment_count",0) if attachments_observed else manifest_attachment_count)
 missing_attachment_count=int(derived.get("missing_attachment_count",0) if attachments_observed else manifest_missing)
 if derived.get("attachments_state")=="partial":
  attachment_count=max(attachment_count,manifest_attachment_count);missing_attachment_count=max(missing_attachment_count,manifest_missing)
 available_attachment_count=int(derived.get("available_attachment_count",max(0,attachment_count-missing_attachment_count)) if attachments_observed else max(0,attachment_count-missing_attachment_count))
 attachment_entity_count=int(derived.get("attachment_entity_count",0) if attachments_observed else manifest.get("attachment_entity_count") or max(0,attachment_count-missing_attachment_count))
 sync_status=str(manifest.get("sync_status") or "").casefold()
 completeness=manifest.get("completeness")
 if missing_attachment_count>0:completeness="附件有缺失"
 elif derived.get("messages_state")=="partial" or derived.get("attachments_state")=="partial":completeness="归档需检查"
 elif not completeness:
  if missing_attachment_count>0:completeness="附件有缺失"
  elif sync_status in {"pending","queued","processing"}:completeness="等待归档"
  elif sync_status in {"failed","error"}:completeness="归档失败"
  elif message_count>0:completeness="已归档"
  else:completeness="等待归档"
 return {"client":client,"source":manifest.get("session_source") or manifest.get("source") or client,
  "session_id":session_id,"source_session_id":manifest.get("source_session_id"),"session_title":title,"started_at":manifest.get("started_at"),
  "updated_at":manifest.get("updated_at") or manifest.get("ended_at") or manifest.get("started_at"),
  "message_count":message_count,"user_count":user_count,"assistant_count":assistant_count,"system_count":int(derived.get("system_count",0)),
  "turn_count":int(derived.get("turn_count",manifest.get("turn_count") or min(user_count,assistant_count))),
  "attachment_count":attachment_count,"available_attachment_count":available_attachment_count,
  "missing_attachment_count":missing_attachment_count,"attachment_entity_count":attachment_entity_count,
  "attachment_references":derived.get("attachment_references"),"messages_state":derived.get("messages_state","absent"),"attachments_state":derived.get("attachments_state","absent"),
  "message_parse_error_count":int(derived.get("message_parse_error_count") or 0),"attachment_parse_error_count":int(derived.get("attachment_parse_error_count") or 0),
  "sync_status":manifest.get("sync_status"),"completeness":completeness,
  "archive_kind":manifest.get("archive_kind") or "conversation","parent_source_session_id":manifest.get("parent_source_session_id"),
  "data_role":manifest.get('data_role'),"document_role":manifest.get('document_role'),"searchable":manifest.get('searchable'),
  "archive_path":str(archive_folder)}

def list_archive_summaries(offset=0,limit=50,client=None,status=None,query=None):
 """Return lightweight manifest metadata without loading conversation bodies."""
 result=[];seen=set();display_cache=_display_cache()
 if CANONICAL_ROOT.exists():
  for manifest in _manifest_paths():
   item=_manifest_summary(manifest)
   if item and item["session_id"] not in seen:result.append(item);seen.update({item["session_id"],item.get("source_session_id")})
 sessions=_read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{})
 for sid,item in sessions.items():
  if sid in seen:continue
  result.append({"client":item.get("client_id"),"source":item.get("source"),"session_id":sid,
   "session_title":item.get("session_title"),"started_at":item.get("started_at"),
   "updated_at":item.get("last_activity_at"),"message_count":int(item.get("user_message_count",0))+int(item.get("assistant_message_count",0))+int(item.get("system_message_count",0)),
   "user_count":int(item.get("user_message_count",0)),"assistant_count":int(item.get("assistant_message_count",0)),
   "turn_count":int(item.get("turn_count",0)),"attachment_count":0,"missing_attachment_count":0,
   "sync_status":item.get("raw_archive_status"),"completeness":"等待归档","archive_path":None});seen.add(sid)
 from .original_archive_import import session_records
 for item in session_records():
  if item["session_id"] not in seen:
   result.append({k:v for k,v in item.items() if k not in {"messages","conversation"}});seen.add(item["session_id"])
 for item in result:
  display=display_cache.get(item["session_id"],{})
  _apply_display_title(item,display)
 blocked=exclusion_aliases([_read_json(path,{}) for path in CANONICAL_ROOT.rglob('manifest.json')],active_workspace().state)
 result=[item for item in result if not exclusion_reason(item,blocked)]
 client=str(client or "").strip().casefold();status=str(status or "").strip().casefold();query=str(query or "").strip().casefold()
 if client and client!="all":result=[item for item in result if str(item.get("client") or "").casefold()==client]
 def is_complete(item):
  return (item.get("message_count",0)>0 and item.get("missing_attachment_count",0)==0
          and item.get("completeness") in {"已归档","完整"}
          and item.get("messages_state")!="partial" and item.get("attachments_state")!="partial")
 if status=="complete":result=[item for item in result if is_complete(item)]
 elif status=="attention":result=[item for item in result if not is_complete(item)]
 if query:
  result=[item for item in result if query in " ".join(str(item.get(key) or "") for key in ("display_title","session_title","session_id","source","client")).casefold()]
 result.sort(key=lambda x:str(x.get("updated_at") or ""),reverse=True)
 offset=max(0,int(offset));limit=max(1,min(100,int(limit)))
 return {"items":result[offset:offset+limit],"total":len(result),"offset":offset,"limit":limit,
         "has_more":offset+limit<len(result)}

def _attachment_ledger(metadata):
 latest={}
 for attachment in _read_jsonl_optional(metadata/'attachments.jsonl'):
  mid=str(attachment.get('source_message_id') or attachment.get('message_id') or '')
  ref=attachment.get('source_reference') or attachment.get('reference') or attachment.get('id')
  latest[(mid,ref)]=attachment
 return latest

def _message_attachments(metadata, row, latest=None):
 if latest is None:latest=_attachment_ledger(metadata) if metadata is not None else {}
 message_id=str(row.get('message_id') or row.get('source_message_id') or '')
 return [a for (mid,_),a in latest.items() if mid==message_id] or row.get('attachments') or []

def materialize_page(session_id,before=None,limit=20,include_attachments=False):
 from .legacy_session_adoption import resolve_session
 session_id=resolve_session(active_workspace(),session_id)
 """Read one bounded page. The response never contains an entire long conversation."""
 limit=max(1,min(50,int(limit)))
 if CANONICAL_ROOT.exists():
  for manifest_path in _manifest_paths():
   summary=_manifest_summary(manifest_path)
   if not summary or summary["session_id"]!=session_id:continue
   metadata=manifest_path.parent;messages_path=metadata/"messages.jsonl"
   if not messages_path.is_file():messages_path=metadata/"messages.json"
   if messages_path.is_file():
    all_rows=_read_message_rows(messages_path)
    total=len(all_rows);end=total if before is None else max(0,min(total,int(before)));start=max(0,end-limit)
    rows=all_rows[start:end]
    latest=_attachment_ledger(metadata)
    rows=[dict(row,attachments=_message_attachments(metadata,row,latest)) for row in rows]
    if not include_attachments:
     rows=[{k:v for k,v in row.items() if k not in {"attachments","attachment_refs"}}|{"attachment_count":len(row.get("attachments") or [])} for row in rows]
    display=_display_cache().get(session_id,{})
    return _apply_display_title(summary,display)|{"messages":rows,"page":{"start":start,"end":end,"total":total,
      "limit":limit,"has_earlier":start>0,"next_before":start if start>0 else None},
      "attachments_included":bool(include_attachments)}
 # Read legacy/imported sources directly. Pagination must never materialize,
 # rewrite, reindex or reorder L0 evidence as a side effect.
 from .original_archive_import import get_session
 item=get_session(session_id)
 if not item:
  sessions=_read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{});metadata=sessions.get(session_id)
  if not metadata:raise ValueError("会话不存在")
  source=_archive_path(session_id,ARCHIVE_ROOT)
  if not source.is_file():raise ValueError("会话原文不存在")
  rows=[json.loads(line) for line in source.read_text("utf-8").splitlines() if line.strip()]
  item={"session_id":session_id,"session_title":metadata.get("session_title"),
        "client":metadata.get("client_id"),"source":metadata.get("source"),
        "started_at":metadata.get("started_at"),"updated_at":metadata.get("last_activity_at"),
        "archive_path":str(source),"messages":rows}
 else:rows=item.get("messages",[])
 total=len(rows)
 end=total if before is None else max(0,min(total,int(before)));start=max(0,end-limit);page=rows[start:end]
 if not include_attachments:page=[{k:v for k,v in row.items() if k not in {"attachments","attachment_refs"}}|{"attachment_count":len(row.get("attachments") or [])} for row in page]
 return {k:v for k,v in item.items() if k not in {"messages","conversation","attachments"}}|{"messages":page,
  "page":{"start":start,"end":end,"total":total,"limit":limit,"has_earlier":start>0,"next_before":start if start>0 else None},
  "attachments_included":bool(include_attachments)}

def read_message(message_id,session_id=None):
 """Resolve one immutable message by archive identity without trusting a growing Markdown file."""
 message_id=str(message_id).strip();session_id=str(session_id).strip() if session_id else None
 from .legacy_session_adoption import resolve_session,resolve_message
 if session_id:
  session_id=resolve_session(active_workspace(),session_id);message_id=resolve_message(active_workspace(),session_id,message_id)
 matches=[];seen=set()
 if CANONICAL_ROOT.exists():
  for manifest_path in _manifest_paths():
   summary=_manifest_summary(manifest_path)
   if not summary:continue
   identities={str(summary.get('session_id') or ''),str(summary.get('source_session_id') or '')}
   if session_id and session_id not in identities:continue
   metadata=manifest_path.parent;messages_path=metadata/'messages.jsonl'
   if not messages_path.is_file():messages_path=metadata/'messages.json'
   rows=_read_message_rows(messages_path) if messages_path.is_file() else _canonical_item(metadata).get('messages',[])
   for row in rows:
    if str(row.get('message_id') or row.get('source_message_id') or '')!=message_id:continue
    text=str(row.get('original_text') if row.get('original_text') is not None else row.get('content') or '')
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    declared=str(row.get('content_sha256') or row.get('hash') or '')
    if declared and declared!=digest:raise ValueError('MESSAGE内容校验失败')
    sid=str(summary.get('session_id') or summary.get('source_session_id') or '')
    key=(sid,message_id,digest)
    if key in seen:continue
    seen.add(key);matches.append({'session_id':sid,'source_session_id':summary.get('source_session_id'),
      'message_id':message_id,'role':str(row.get('role') or '').upper(),'content':text,
      'timestamp':row.get('timestamp'),'message_order':row.get('message_order'),
      'attachments':_message_attachments(metadata,row),'attachment_refs':row.get('attachment_refs') or [],
      'provenance':row.get('provenance') or {},
      'source':active_workspace().relative(messages_path) if messages_path.is_file() else summary.get('archive_path'),
      'version':digest,'source_version':digest,'verification':'raw_evidence'})
 if not matches:
  # Compatibility for live session archives which have not yet been mirrored.
  registry=_read_json(active_workspace().archive_registry,{'sessions':{}}).get('sessions',{})
  for key,item in registry.items():
   identities={str(key),str(item.get('session_key') or ''),str(item.get('source_session_id') or '')}
   if session_id and session_id not in identities:continue
   path=active_workspace().raw_message_store/f'{key}.jsonl'
   for row in _read_jsonl_optional(path):
    if str(row.get('message_id') or row.get('source_message_id') or '')!=message_id:continue
    text=str(row.get('original_text') if row.get('original_text') is not None else row.get('content') or '')
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest();declared=str(row.get('content_sha256') or '')
    if declared and declared!=digest:raise ValueError('MESSAGE内容校验失败')
    identity=str(item.get('source_session_id') or key);dedupe=(identity,message_id,digest)
    if dedupe in seen:continue
    seen.add(dedupe);matches.append({'session_id':identity,'source_session_id':item.get('source_session_id'),
      'message_id':message_id,'role':str(row.get('role') or '').upper(),'content':text,
      'timestamp':row.get('timestamp'),'message_order':row.get('message_order'),
      'attachments':_message_attachments(active_workspace().root/item['relative_path']/'03_元数据' if item.get('relative_path') else None,row),'attachment_refs':row.get('attachment_refs') or [],
      'provenance':row.get('provenance') or {},
      'source':active_workspace().relative(path),'version':digest,'source_version':digest,
      'verification':'raw_evidence'})
 if not matches:raise ValueError('MESSAGE不存在')
 sessions={row['session_id'] for row in matches}
 if len(sessions)>1:raise ValueError('MESSAGE身份在多个会话中重复，请同时提供session_id')
 return matches[0]

def materialize(session_id,source_latest=None):
 from .legacy_session_adoption import resolve_session
 # An adopted source ID is still the owner of incoming raw records. Resolving
 # it to a canonical key before reading that ledger would silently return the
 # old projection and discard its pending work without mirroring new rows.
 source_registered=session_id in _read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{})
 if not (source_registered and _archive_path(session_id,ARCHIVE_ROOT).is_file()):
  session_id=resolve_session(active_workspace(),session_id)
 # Shared with source adapters and targeted repair; legacy test seams remain isolated.
 if CANONICAL_ROOT.resolve()==active_workspace().raw_archive.resolve() and SESSIONS_PATH.resolve()==active_workspace().sessions_state.resolve():
  from .archive_store import archive_transaction
  with archive_transaction(active_workspace()):return _materialize_locked(session_id,source_latest)
 return _materialize_locked(session_id,source_latest)

def _materialize_locked(session_id,source_latest=None):
 source_live=_archive_path(session_id,ARCHIVE_ROOT)
 registered_live=session_id in _read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{})
 existing=None if registered_live and source_live.is_file() else _find_canonical(session_id)
 if existing and not (registered_live and source_live.is_file()):return existing
 from .original_archive_import import get_session
 imported=get_session(session_id)
 if imported:return imported
 sessions=_read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{});item=sessions.get(session_id)
 if not item:raise ValueError("会话不存在")
 source=_archive_path(session_id,ARCHIVE_ROOT); rows=[json.loads(x) for x in source.read_text("utf-8").splitlines() if x.strip()]
 # New archives use a stable source identity and an owner-facing title folder.
 # Legacy classified/hash archives above remain read-only compatible.
 if CANONICAL_ROOT.resolve()==active_workspace().raw_archive.resolve() and SESSIONS_PATH.resolve()==active_workspace().sessions_state.resolve():
  from .archive_store import ArchiveStore,NormalizedMessage,NormalizedAttachment
  store=ArchiveStore(active_workspace(),recover=False)
  session=store.ensure_session(source_key=item["client_id"],source_session_id=session_id,title=item.get("session_title"),source_instance=item.get("source") or "default",acquisition_method="session_bridge")
  attachment_rows=[]
  if ATTACHMENT_MANIFEST.exists():
   from .attachment_cache import session_rows
   attachment_rows=session_rows(ATTACHMENT_MANIFEST,session_id,persist=True)
  by_message={}
  for attachment in attachment_rows:by_message.setdefault(str(attachment.get("message_id")),[]).append(attachment)
  # One linear identity pass, rather than re-reading the full immutable ledger
  # for every already-mirrored row on every late attachment/revision.
  mirrored={str(row['message_id']):row for row in store._message_rows(active_workspace().raw_message_store/f"{session['session_key']}.jsonl")}
  attachments_latest={(str(row.get('source_message_id')),row.get('source_reference')):row for row in store.attachment_status(session['session_key'])}
  for row in rows:
   mid=str(row['message_id']);text=row.get('original_text',row.get('content',''))
   old=mirrored.get(mid)
   if old is not None:
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    if old.get('content_sha256')!=digest or old.get('role')!=row['role']:
     raise ValueError('同一原始消息ID出现冲突内容')
    changes=[]
    for attachment in by_message.get(mid,[]):
     ref=attachment.get('reference') or attachment.get('original_name') or 'unknown'
     saved=attachments_latest.get((mid,ref)) or {}
     stored=saved.get('stored_path');available=attachment.get('status')=='mapped'
     if not saved or saved.get('sha256')!=attachment.get('sha256') or bool(saved.get('missing',True))==available or (available and (not stored or not (active_workspace().root/stored).is_file())):
      changes.append(attachment)
    if changes:
     specs=[NormalizedAttachment(reference=x.get('reference') or x.get('original_name') or 'unknown',original_name=x.get('original_name') or '附件',local_path=Path(x['stored_path']) if x.get('stored_path') else None) for x in changes]
     store.reconcile_attachments(session['session_key'],mid,specs,materialize=False)
    continue
   specs=tuple(NormalizedAttachment(reference=x.get('reference') or x.get('original_name') or 'unknown',original_name=x.get('original_name') or '附件',local_path=Path(x['stored_path']) if x.get('stored_path') else None) for x in by_message.get(str(row['message_id']),[]))
   store.append_normalized_message(session['session_key'],NormalizedMessage(message_id=str(row['message_id']),role=row['role'],content=row.get('original_text',row.get('content','')),timestamp=row.get('timestamp'),attachments=specs,provenance=row.get('provenance',{})),materialize=False)
  if source_latest:
   from .archive_store import _atomic_json
   latest_path=Path(session['archive_path'])/'03_元数据'/'message-latest.json'
   latest=_read_json(latest_path,{});latest.setdefault('latest_by_source_message',{}).update(source_latest)
   _atomic_json(latest_path,latest)
  store.materialize_session(session['session_key'])
  return store.session(session["session_key"])
 target=_session_dir(item);target.mkdir(parents=True,exist_ok=True);attachments=target/"attachments";attachments.mkdir(exist_ok=True)
 attachment_rows=[]
 if ATTACHMENT_MANIFEST.exists():
  from .attachment_cache import session_rows
  attachment_rows=session_rows(ATTACHMENT_MANIFEST,session_id,persist=True)
 by_message={}
 for row in attachment_rows:by_message.setdefault(str(row.get("message_id")),[]).append(row)
 normalized=[]
 for i,row in enumerate(rows,1):
  text=row.get("original_text",row.get("content",""));digest=hashlib.sha256(text.encode("utf-8")).hexdigest()
  message_attachments=[]
  for source_attachment in by_message.get(str(row["message_id"]),[]):
   copied=dict(source_attachment);stored=Path(source_attachment["stored_path"]) if source_attachment.get("stored_path") else None
   if stored and stored.is_file():
    target_name=f"{source_attachment['sha256'][:12]}_{source_attachment['original_name']}";dest=attachments/target_name
    if not dest.exists():shutil.copyfile(stored,dest)
    copied["archived_path"]=str(dest);copied["stored_path"]=str(dest);copied["status"]="mapped"
   message_attachments.append(copied)
  refs=list(dict.fromkeys((row.get("attachment_refs") or [])+[x.get("reference") for x in message_attachments if x.get("reference")]))
  normalized.append({"client_id":item["client_id"],"session_id":session_id,"message_id":row["message_id"],"role":row["role"],"timestamp":row["timestamp"],"original_text":text,"message_order":row.get("message_order",i),"attachment_refs":refs,"attachments":message_attachments,"token_usage":row.get("token_usage"),"token_status":"actual" if row.get("token_usage") is not None else "estimated","ingest_timestamp":row.get("ingest_timestamp"),"hash":digest,"provenance":row.get("provenance",{})})
 machine="".join(json.dumps(x,ensure_ascii=False,separators=(",",":"))+"\n" for x in normalized)
 title=item.get("session_title") or f"{item['client_id']} · {item['started_at']}"
 body=f"# {title}\n\n"+"\n\n".join(f"## {x['role'].upper()}\n\n{x['original_text']}" for x in normalized)+"\n"
 all_hash=hashlib.sha256(machine.encode("utf-8")).hexdigest();mapped=sum(sum(a.get("status")=="mapped" for a in x["attachments"]) for x in normalized);missing=sum(sum(a.get("status")!="mapped" for a in x["attachments"]) for x in normalized)
 manifest={"client":item["client_id"],"source":item["source"],"session_id":session_id,"session_title":item.get("session_title"),"started_at":item["started_at"],"ended_at":item.get("ended_at"),"user_count":item["user_message_count"],"assistant_count":item["assistant_message_count"],"message_count":len(normalized),"turn_count":item["turn_count"],"attachment_count":mapped,"missing_attachment_count":missing,"attachments":[a for x in normalized for a in x["attachments"]],"hash":all_hash,"sync_status":"synced","completeness":"完整" if not missing else "附件不完整","source_archive":str(source),"indexed":False,"updated_at":item["last_activity_at"]}
 for name,text in (("messages.jsonl",machine),("conversation.md",body),("manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")):
  path=target/name;tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text(text,"utf-8",newline="");os.replace(tmp,path)
 return manifest|{"archive_path":str(target),"conversation":body,"messages":normalized}
def _message_snapshot(rows):
 # Include order and exact message content, not titles or sample prefixes.
 return tuple((str(row.get('source_message_id') or row.get('message_id') or ''),
               str(row.get('role') or '').lower(),
               hashlib.sha256(str(row.get('original_text',row.get('content',''))).encode('utf-8')).hexdigest())
              for row in rows)

def _mark_alias_snapshots(items):
 # A manifest's explicit source identity is the only alias authority here.
 groups={}
 by_id={item['session_id']:item for item in items}
 for item in items:
  sid=item.get('source_session_id')
  old=by_id.get(sid)
  if item.get('session_key') and old and sid!=item['session_id'] and old.get('client')==item.get('client'):
   groups.setdefault(sid,[]).append(item)
 for sid,current in groups.items():
  for item in items:
   if item['session_id']==sid:
    item['snapshot_kind']='historical'
    item['snapshot_label']='历史快照'
  for item in current:
   item['snapshot_kind']='canonical'
   item['snapshot_label']='当前归档'
 return groups

def list_archives(*, read_only=False, include_conversation=True, include_internal=False):
 sessions=_read_json(SESSIONS_PATH,{"sessions":{}}).get("sessions",{});result=[];seen=set()
 if CANONICAL_ROOT.exists():
  for manifest in _manifest_paths():
   item=_canonical_item(manifest.parent,include_conversation=include_conversation)
   if item and not any(x['session_id']==item['session_id'] for x in result):result.append(item);seen.update({item["session_id"],item.get("source_session_id")})
 alias_groups=_mark_alias_snapshots(result)
 for sid in sessions:
  if read_only:
   # A status request must not materialize/rewrite the canonical evidence.
   # Registered live messages are authoritative when newer than that mirror.
   source=_archive_path(sid,ARCHIVE_ROOT)
   if source.is_file():
    try:rows=_read_message_rows(source)
    except (OSError,ValueError):continue
    item=sessions[sid]
    if sid in alias_groups:
     # Never overwrite a historical snapshot with a newer live version.
     # Only a unique, exact canonical match can represent this live body.
     current=alias_groups[sid]
     fingerprint=_message_snapshot(rows)
     matches=[x for x in current if _message_snapshot(x['messages'])==fingerprint]
     if len(matches)==1:
      matches[0]['live_snapshot_matched']=True
     else:
      for snapshot in current:
       snapshot['live_snapshot_pending']=True
       snapshot['live_message_count']=len(rows)
       snapshot['snapshot_label']='当前归档 · 新消息版本待归档核对'
     seen.add(sid)
     continue
    existing=next((x for x in result if sid in {x['session_id'],x.get('source_session_id')}),None)
    update={'messages':rows,'message_count':len(rows),'updated_at':item.get('last_activity_at'),
            'turn_count':item.get('turn_count')}
    if existing is not None:existing.update(update)
    elif sid not in seen:
     result.append({**item,**update,'session_id':sid,'client':item.get('client_id'),
                    'archive_path':str(source),'conversation':''})
    seen.add(sid)
   continue
  if sid in seen:continue
  try:result.append(materialize(sid))
  except (OSError,ValueError):pass
 from .original_archive_import import session_records
 for item in session_records(include_conversation=include_conversation,read_fresh=read_only):
  if item["session_id"] not in seen:result.append(item);seen.update({item["session_id"],item.get("source_session_id")})
 if not include_internal:
  blocked=exclusion_aliases([_read_json(path,{}) for path in CANONICAL_ROOT.rglob('manifest.json')],active_workspace().state)
  result=[item for item in result if not exclusion_reason(item,blocked)]
 return sorted(result,key=lambda x:str(x.get("updated_at") or ""),reverse=True)
