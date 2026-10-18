from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .core import all_rows
from .workspace import active_workspace

INDEX_META=active_workspace().index_root("evidence")/"context-windows.json"
INDEX_VECTOR=active_workspace().index_root("evidence")/"context-windows.npy"
WINDOW_SIZE=5
WINDOW_OVERLAP=2

def _normalize(vector):
    vector=np.asarray(vector,dtype="float32"); norm=float(np.linalg.norm(vector))
    return vector/norm if norm else vector

def build_context_index(force=False):
    if not force and INDEX_META.exists() and INDEX_VECTOR.exists(): return context_status()
    grouped={}
    for row in all_rows():
        if row.get("source_type")!="raw_chat" or not row.get("message_id"): continue
        key=(row.get("raw_source") or row.get("source_path"),row["message_id"])
        item=grouped.setdefault(key,{"message_id":row["message_id"],"role":row.get("role"),"source":row.get("raw_source"),"standardized_file":row.get("standardized_file"),"order":row.get("message_order",int(row["message_id"])),"vectors":[],"contents":[]})
        item["vectors"].append(row["vector"]); item["contents"].append(row.get("content", ""))
    conversations={}
    for (source,_),item in grouped.items():
        item["vector"]=_normalize(np.mean(np.asarray(item.pop("vectors"),dtype="float32"),axis=0)); item["content"]="\n".join(item.pop("contents"))
        conversations.setdefault(source,[]).append(item)
    windows=[]; vectors=[]; stride=WINDOW_SIZE-WINDOW_OVERLAP
    for source,messages in conversations.items():
        messages.sort(key=lambda x:x["order"])
        for start in range(0,len(messages),stride):
            members=messages[start:start+WINDOW_SIZE]
            if len(members)<2: continue
            window_id=f"ctx:{Path(source).stem}:{members[0]['message_id']}-{members[-1]['message_id']}"
            windows.append({"window_id":window_id,"member_message_ids":[m["message_id"] for m in members],"roles":[m["role"] for m in members],"source":source,"start_id":members[0]["message_id"],"end_id":members[-1]["message_id"],"standardized_files":list(dict.fromkeys(m["standardized_file"] for m in members)),"member_contents":[m["content"] for m in members]})
            vectors.append(_normalize(np.mean(np.asarray([m["vector"] for m in members],dtype="float32"),axis=0)))
    INDEX_META.parent.mkdir(parents=True,exist_ok=True); INDEX_META.write_text(json.dumps(windows,ensure_ascii=False),"utf-8"); np.save(INDEX_VECTOR,np.asarray(vectors,dtype="float32"))
    return context_status()

def context_status():
    windows=json.loads(INDEX_META.read_text("utf-8")) if INDEX_META.exists() else []
    return {"window_count":len(windows),"window_size":WINDOW_SIZE,"overlap":WINDOW_OVERLAP,"history_reembedded":0,"ready":bool(windows) and INDEX_VECTOR.exists()}

def context_search(query_vector,limit=50):
    if not INDEX_META.exists() or not INDEX_VECTOR.exists(): build_context_index()
    windows=json.loads(INDEX_META.read_text("utf-8")); vectors=np.load(INDEX_VECTOR,mmap_mode="r")
    if not windows: return []
    scores=np.asarray(vectors@_normalize(query_vector),dtype="float32"); indexes=np.argsort(-scores)[:min(limit,len(windows))]
    return [windows[int(i)]|{"score":float(scores[int(i)]),"rank":rank+1} for rank,i in enumerate(indexes)]
