from __future__ import annotations

import argparse, hashlib, importlib.util, json, re, sys
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

from .schema import Memory

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT=ROOT/"data"/"memory-draft"
from knowledge_bridge.workspace import configured_workspace_root
VAULT_ROOT=configured_workspace_root()
LIVE_STATUSES=("已实现","进行中","已决定未开始","仅方向/想法","暂停","否决","被替代","待确认")
CURATED_ASSOCIATIONS={
  "我的AI协作定位.md": {"profile-0001","profile-0002","profile-0004","profile-0006","profile-0007","profile-0009","profile-0010","profile-0011","profile-0012","rule-0001","rule-0002","rule-0003","rule-0004","rule-0005","rule-0006","rule-0007","rule-0010","rule-0012","rule-0013","rule-0014","rule-0015","rule-0016","rule-0019","failure-0002","failure-0003","failure-0004","failure-0005","failure-0006","failure-0007","failure-0009","failure-0010","route-0002","route-0003"},
  "AI开发方法论.md": {"profile-0002","profile-0004","rule-0005","rule-0017","knowledge-0001"},
  "AI商业方向-AI智能体服务.md": {"profile-0001","decision-0001"},
  "商业方向-当前商业路线.md": {"profile-0001","decision-0001"},
  "小龚豆花馆-决策记录.md": {"project-0001","knowledge-0001"},
  "小龚豆花馆-当前状态.md": {"project-0001"},
  "小龚豆花馆-技术资产.md": {"project-0001"},
  "小龚豆花馆-项目总览.md": {"project-0001"},
  "小龚豆花馆点餐系统项目档案.md": {"project-0001","failure-0001"},
  "本地AI环境与工具记录.md": {"knowledge-0002","rule-0018"},
}


def load_importer():
    path=ROOT/"importers"/"raw_chat_importer.py"
    spec=importlib.util.spec_from_file_location("memory_raw_chat_importer",path)
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
    return module


def normalized_source(manifest:dict,message_id:str)->str:
    number=int(message_id)
    for item in manifest["normalized_files"]:
        if int(item["message_start"])<=number<=int(item["message_end"]):
            base=Path(manifest["source_file"]).parent.parent
            return (base/item["file"]).as_posix()
    raise ValueError(f"No normalized source for {message_id}")


def extract_candidates(messages:list,output:Path)->dict:
    patterns={
      "user_profile":["我的目标","我的方向","我的优势","我的习惯","我希望","我负责","我能够","我喜欢"],
      "collaboration_rule":["不要","必须","以后","记住","直接","先不要","不得","不能"],
      "ai_failure":["不对","理解错","你又","为什么","乱","死磕","幻觉","逗我呢","没有体现"],
      "project":["项目","工作流","系统","产品","V1","落地"],
    }
    result={"compiler_version":"1.0.0","message_count":len(messages),"categories":{}}
    for category,words in patterns.items():
        candidates=[]
        for message in messages:
            hits=[word for word in words if word in message.body]
            if hits:
                candidates.append({"message_id":message.message_id,"role":message.role,"signals":hits,
                  "preview":re.sub(r"\s+"," ",message.body)[:260]})
        result["categories"][category]={"count":len(candidates),"candidates":candidates}
    output.mkdir(parents=True,exist_ok=True)
    (output/"candidate_extraction.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n","utf-8")
    return result


def evidence_lines(memory:Memory,message_map:dict)->list[str]:
    lines=[]
    for message_id in memory.evidence_message_ids:
        message=message_map[message_id]
        preview=re.sub(r"\s+"," ",message.body).strip()[:240]
        lines.append(f"> **MESSAGE {message_id}｜{message.role}**：{preview}")
    return lines


def render_group(title:str,records:list[Memory],message_map:dict)->str:
    lines=[f"# {title}","","> Dry Run：仅供审核，未写入正式长期记忆。",""]
    for memory in records:
        lines += [f"## {memory.title}","",f"- memory_id：`{memory.memory_id}`",f"- 类型：`{memory.type}`",f"- 状态：`{memory.status}`",
          f"- 项目：{memory.project or '无'}",f"- 首次出现：MESSAGE {memory.first_seen}",f"- 最近确认：MESSAGE {memory.last_confirmed}",
          f"- 置信度：{memory.confidence:.2f}",f"- 来源优先级：{' > '.join(memory.source_priority) or 'RAW_SOURCE原始MESSAGE'}",f"- MESSAGE证据：{', '.join('MESSAGE '+x for x in memory.evidence_message_ids)}"]
        if memory.curated_source_files:
            lines += [f"- 人工档案：{'; '.join(memory.curated_source_files)}"]
        if memory.provenance_notes:
            lines += [f"- 合并说明：{'；'.join(memory.provenance_notes)}"]
        lines += ["",memory.content,"","### 原始证据",""]
        lines += evidence_lines(memory,message_map)+[""]
    return "\n".join(lines).rstrip()+"\n"


def stable_core(records:list[Memory])->str:
    def active(memory): return memory.status not in {"否决","被替代"}
    by_id={m.memory_id:m for m in records}
    profile_ids=["profile-0001","profile-0010","profile-0011","profile-0002","profile-0007","profile-0005","profile-0006","profile-0008"]
    rule_ids=["rule-0019","rule-0001","rule-0003","rule-0014","rule-0015","rule-0004","rule-0017","rule-0005","rule-0007","rule-0012","rule-0016","rule-0013"]
    failure_ids=["failure-0002","failure-0003","failure-0005","failure-0006","failure-0009","failure-0010"]
    profiles=[by_id[x] for x in profile_ids if x in by_id and active(by_id[x])]
    rules=[by_id[x] for x in rule_ids if x in by_id and active(by_id[x])]
    failures=[by_id[x] for x in failure_ids if x in by_id and active(by_id[x])]
    goals=[by_id[x] for x in ["profile-0012"] if x in by_id and active(by_id[x])]
    lines=["# Stable Core（草稿）","","> 稳定核心：来自长期重复确认的画像、习惯、规则与教训；L0仅作为历史证据。","","## 用户长期画像",""]
    lines += [f"- {m.content}〔{','.join(m.evidence_message_ids)}〕" for m in profiles]
    lines += ["","## 长期目标",""]+[f"- {m.content}〔{','.join(m.evidence_message_ids)}〕" for m in goals]
    lines += ["","## AI协作核心规则",""]+[f"- {m.content}〔{','.join(m.evidence_message_ids)}〕" for m in rules]
    lines += ["","## 最重要AI错误与教训",""]+[f"- {m.content}〔{','.join(m.evidence_message_ids)}〕" for m in failures]
    return "\n".join(lines).rstrip()+"\n"


def markdown_documents(folder:Path)->list[dict]:
    documents=[]
    if not folder.exists(): return documents
    for path in sorted(folder.rglob("*.md")):
        text=path.read_text("utf-8-sig")
        frontmatter={}
        match=re.match(r"\A---\s*\n(.*?)\n---\s*\n",text,re.S)
        if match:
            for line in match.group(1).splitlines():
                if ":" in line:
                    key,value=line.split(":",1); frontmatter[key.strip().lower()]=value.strip().strip("\"'")
        heading=re.search(r"(?m)^#\s+(.+?)\s*$",text)
        title=frontmatter.get("project") or frontmatter.get("title") or frontmatter.get("name") or (heading.group(1).strip() if heading else path.stem)
        status=frontmatter.get("status","")
        if status not in LIVE_STATUSES:
            status_match=re.search(r"(?:当前状态|项目状态|状态)\s*[：:]\s*(%s)"%"|".join(LIVE_STATUSES),text)
            status=status_match.group(1) if status_match else "待确认"
        body=re.sub(r"\A---\s*\n.*?\n---\s*\n","",text,flags=re.S)
        body=re.sub(r"(?m)^#+\s+.*$","",body).strip()
        summary=frontmatter.get("summary") or re.sub(r"\s+"," ",body)[:240] or "暂无摘要"
        documents.append({"title":title,"status":status,"summary":summary,"source":str(path.resolve()),"modified":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()})
    return documents


def load_confirmations(path:Path|None)->dict:
    if not path or not path.exists(): return {"projects":[],"priorities":[],"decisions":[],"next_steps":[]}
    data=json.loads(path.read_text("utf-8"))
    return {key:data.get(key,[]) for key in ("projects","priorities","decisions","next_steps")}


def section_items(path:Path,heading:str)->list[str]:
    if not path.exists(): return []
    text=path.read_text("utf-8-sig")
    match=re.search(rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)",text)
    return re.findall(r"(?m)^-\s+(.+?)\s*$",match.group(1)) if match else []


def active_project_documents(path:Path)->list[dict]:
    if not path.exists(): return []
    text=path.read_text("utf-8-sig")
    blocks=re.finditer(r"(?ms)^##\s+(?P<title>.+?)\s*$\n(?P<body>.*?)(?=^##\s|\Z)",text)
    projects=[]
    for block in blocks:
        body=block.group("body")
        status_match=re.search(r"(?m)^-\s*状态[：:]\s*(%s)\s*$"%"|".join(LIVE_STATUSES),body)
        current_match=re.search(r"(?m)^-\s*当前状态[：:]\s*(.+?)\s*$",body)
        evidence_match=re.search(r"(?m)^-\s*证据[：:]\s*(.+?)\s*$",body)
        if not status_match: continue
        projects.append({"title":block.group("title").strip(),"status":status_match.group(1),"summary":current_match.group(1).strip() if current_match else "暂无摘要","source":"01_当前状态/当前活跃项目.md","evidence":evidence_match.group(1).strip() if evidence_match else ""})
    return projects


def build_live_context(records:list[Memory],vault_root:Path,confirmations_path:Path|None)->tuple[str,dict]:
    current_docs=markdown_documents(vault_root/"01_当前状态")
    project_docs=[item for item in markdown_documents(vault_root/"02_项目") if Path(item["source"]).name=="当前状态.md"]
    for item in project_docs: item["source"]=Path(item["source"]).relative_to(vault_root).as_posix()
    confirmations=load_confirmations(confirmations_path)
    active_projects=active_project_documents(vault_root/"01_当前状态"/"当前活跃项目.md")
    priorities=section_items(vault_root/"01_当前状态"/"当前优先事项.md","当前最高优先级")
    next_steps=section_items(vault_root/"01_当前状态"/"当前优先事项.md","下一步")
    decisions=section_items(vault_root/"01_当前状态"/"最近关键决策.md","最近关键决策")
    if not decisions and (vault_root/"01_当前状态"/"最近关键决策.md").exists():
        decisions=re.findall(r"(?m)^-\s+(.+?)\s*$",(vault_root/"01_当前状态"/"最近关键决策.md").read_text("utf-8-sig"))
    projects={}
    def canonical(title:str)->str:
        if "小龚豆花馆" in title: return "小龚豆花馆"
        if title.startswith("AI教学") and ("潜客" in title or "潜在客户" in title): return "AI教学潜客工作流"
        if "明证" in title: return "明证"
        if "Knowledge Bridge" in title or "长期记忆系统" in title: return "Knowledge Bridge"
        return title
    # L0 projects are discovery fallback only: their historical status never becomes current automatically.
    for memory in records:
        if memory.type=="project":
            title=canonical(memory.title); projects[title]={"title":title,"status":"待确认","summary":"仅有L0历史证据，当前状态尚无实时来源。","source":"L0历史证据（仅兜底）"}
    for item in active_projects:
        item["title"]=canonical(item["title"]); projects[item["title"]]=item
    # Project current-state files are the canonical project truth source and override the cross-project view.
    for doc in project_docs:
        doc["title"]=canonical(doc["title"])
        projects[doc["title"]]=doc
    for item in confirmations["projects"]:
        projects[item["title"]]={**item,"source":item.get("source","最新人工确认记录")}
    lines=["# Live Context（草稿）","","> 实时上下文：项目状态优先级为最新用户明确确认 → `02_项目`最新状态 → `01_当前状态`跨项目视图 → 最新人工确认 → L0历史兜底。L0不能单独决定当前状态。","","## 项目总表",""]
    if projects:
        for item in projects.values(): lines.append(f"- **{item['title']}**｜{item.get('status','待确认')}｜{item.get('summary','暂无摘要')}｜来源：{item.get('source','待确认')}")
    else: lines.append("- 暂无项目数据。")
    lines += ["","## 当前优先事项",""]
    lines += [f"- {x}｜来源：`01_当前状态/当前优先事项.md`" for x in priorities]
    lines += [f"- {x['content']}｜来源：{x.get('source','最新用户明确确认')}" for x in confirmations["priorities"]]
    if not priorities and not confirmations["priorities"]: lines += ["- 待确认：没有可机器读取的最新用户确认或 `01_当前状态` 记录。"]
    lines += ["","## 最近决策",""]
    lines += [f"- {x}｜来源：`01_当前状态/最近关键决策.md`" for x in decisions]
    lines += [f"- {x['content']}｜来源：{x.get('source','最新用户明确确认')}" for x in confirmations["decisions"]]
    if not decisions and not confirmations["decisions"]: lines += ["- 待确认：没有当前决策来源；L0历史决策未提升为当前决策。"]
    lines += ["","## 下一步",""]
    lines += [f"- {x}｜来源：`01_当前状态/当前优先事项.md`" for x in next_steps]
    lines += [f"- {x['content']}｜来源：{x.get('source','最新用户明确确认')}" for x in confirmations["next_steps"]]
    if not next_steps and not confirmations["next_steps"]: lines += ["- 待确认：没有当前下一步来源。"]
    meta={"current_state_documents":len(current_docs),"project_documents":len(project_docs),"active_project_records":len(active_projects),"confirmed_projects":len(confirmations["projects"]),"project_count":len(projects),"pending_projects":[x["title"] for x in projects.values() if x.get("status")=="待确认"]}
    return "\n".join(lines).rstrip()+"\n",meta


def memory_bootstrap(stable_core_text:str,live_context_text:str)->str:
    return "# 启动记忆（合并草稿）\n\n> Dry Run：Stable Core 提供长期稳定背景；Live Context 提供可更新的当前事实。\n\n"+stable_core_text+"\n"+live_context_text


def curated_manifest(curated_source:Path,output:Path)->dict:
    files=[]
    if curated_source.exists():
        for path in sorted(curated_source.glob("*.md")):
            raw=path.read_bytes()
            files.append({"file":str(path.resolve()),"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)})
    result={"source_root":str(curated_source.resolve()),"exists":curated_source.exists(),"files":files}
    (output/"curated_sources_manifest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n","utf-8")
    return result


def compile_drafts(source:Path,records_path:Path,output:Path,curated_source:Path,vault_root:Path=VAULT_ROOT,live_confirmations:Path|None=None)->dict:
    importer=load_importer(); messages=importer.parse_messages(source.read_bytes().decode("utf-8-sig")); message_map={m.message_id:m for m in messages}
    manifest=json.loads((source.parent.parent/"manifest.json").read_text("utf-8"))
    raw_records=json.loads(records_path.read_text("utf-8")); records=[Memory.from_dict(item) for item in raw_records]
    output.mkdir(parents=True,exist_ok=True)
    archive=curated_manifest(curated_source,output); archive_paths={Path(x["file"]).name:x["file"] for x in archive["files"]}
    ids=set(message_map); seen=set()
    for memory in records:
        normalized=list(dict.fromkeys(normalized_source(manifest,x) for x in memory.evidence_message_ids))
        curated=[archive_paths[name] for name,record_ids in CURATED_ASSOCIATIONS.items() if memory.memory_id in record_ids and name in archive_paths]
        memory.curated_source_files=list(dict.fromkeys(memory.curated_source_files+curated))
        memory.source_files=list(dict.fromkeys(normalized+memory.curated_source_files))
        memory.source_priority=memory.source_priority or (["最新用户明确事实/项目真实状态","人工精炼档案","RAW_SOURCE原始MESSAGE"] if curated else ["最新用户明确事实/项目真实状态","RAW_SOURCE原始MESSAGE"])
        memory.created_at=memory.created_at or "2026-08-12T00:00:00+08:00"
        memory.updated_at=memory.updated_at or "2026-08-12T00:00:00+08:00"
        memory.validate(ids)
        if memory.memory_id in seen: raise ValueError(f"duplicate memory_id: {memory.memory_id}")
        seen.add(memory.memory_id)
    candidates=extract_candidates(messages,output)
    groups={"user_profile_draft.md":("我的长期画像（草稿）",{"user_profile","preference"}),
      "collaboration_rules_draft.md":("AI协作规则（草稿）",{"collaboration_rule"}),
      "ai_failures_draft.md":("AI错误与教训（草稿）",{"ai_failure"}),
      "projects_index_draft.md":("项目总索引（草稿）",{"project","project_state","decision","knowledge","rejected_route"})}
    for filename,(title,types) in groups.items():
        (output/filename).write_text(render_group(title,[m for m in records if m.type in types],message_map),"utf-8")
    stable=stable_core(records)
    live,live_meta=build_live_context(records,vault_root,live_confirmations)
    bootstrap=memory_bootstrap(stable,live)
    (output/"stable_core_draft.md").write_text(stable,"utf-8")
    (output/"live_context_draft.md").write_text(live,"utf-8")
    (output/"bootstrap_memory_draft.md").write_text(bootstrap,"utf-8")
    materialized=[]
    for memory in records:
        item=memory.__dict__.copy(); materialized.append(item)
    (output/"memory_records.json").write_text(json.dumps(materialized,ensure_ascii=False,indent=2)+"\n","utf-8")
    summary={"compiler_version":"1.3.0","source_sha256":manifest["sha256"],"curated_source_root":archive["source_root"],"curated_source_files":len(archive["files"]),"record_count":len(records),
      "counts":dict(Counter(m.type for m in records)),"stable_core_characters":len(stable),"live_context_characters":len(live),"bootstrap_characters":len(bootstrap),"bootstrap_estimated_tokens":round(len(bootstrap)*.85),"live_context":live_meta,
      "llm_api_calls":0,"llm_api_tokens":0,"status":"dry_run_complete"}
    (output/"compile_state.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n","utf-8")
    return summary


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--source",type=Path,required=True); parser.add_argument("--records",type=Path,default=ROOT/"memory_compiler"/"records_v1.json"); parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT); parser.add_argument("--curated-source",type=Path,required=True); parser.add_argument("--vault-root",type=Path,default=VAULT_ROOT); parser.add_argument("--live-confirmations",type=Path)
    args=parser.parse_args(); print(json.dumps(compile_drafts(args.source.resolve(),args.records.resolve(),args.output.resolve(),args.curated_source.resolve(),args.vault_root.resolve(),args.live_confirmations.resolve() if args.live_confirmations else None),ensure_ascii=False,indent=2))


if __name__=="__main__": main()
