"""Enrich existing LanceDB rows with raw-chat metadata without embedding text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from knowledge_bridge import core


def vector_fingerprint(rows: list[dict]) -> str:
    digest=hashlib.sha256()
    for row in sorted(rows,key=lambda item:item["chunk_id"]):
        digest.update(row["chunk_id"].encode("utf-8"))
        digest.update(np.asarray(row["vector"],dtype="float32").tobytes())
    return digest.hexdigest()


def migrate() -> dict:
    database=core.db(); current=core.table()
    if current is None: raise RuntimeError("chunks table does not exist")
    old_rows=current.to_arrow().to_pylist(); before=vector_fingerprint(old_rows)
    metadata={}
    for path in core.scan_files():
        if core._chat_context(core.rel(path)):
            _,rows=core.parse_markdown(path)
            metadata.update({row["chunk_id"]:row for row in rows})
    enriched=[]
    defaults={"source_type":"vault_document","raw_source":"","standardized_file":"","message_id":"","role":"",
      "image_refs":[],"message_order":-1,"chunk_index_in_message":-1}
    for row in old_rows:
        item=dict(row); parsed=metadata.get(row["chunk_id"])
        if parsed:
            if parsed["content_hash"] != row["content_hash"]: raise RuntimeError(f"content changed: {row['chunk_id']}")
            item.update({key:parsed[key] for key in defaults})
            item["project"]=parsed["project"]
        else:
            for key,value in defaults.items(): item.setdefault(key,value)
        enriched.append(item)
    temp="chunks_metadata_v1"
    if temp in database.table_names(): database.drop_table(temp)
    migrated=database.create_table(temp,data=enriched)
    migrated.create_fts_index("content",replace=True,base_tokenizer="ngram",ngram_min_length=2,ngram_max_length=4,
      stem=False,remove_stop_words=False,ascii_folding=False,with_position=True)
    check=migrated.to_arrow().to_pylist()
    if len(check)!=len(old_rows) or vector_fingerprint(check)!=before: raise RuntimeError("vector preservation check failed")
    final_table=database.create_table("chunks",data=check,mode="overwrite")
    final_table.create_fts_index("content",replace=True,base_tokenizer="ngram",ngram_min_length=2,ngram_max_length=4,
      stem=False,remove_stop_words=False,ascii_folding=False,with_position=True)
    final=final_table.to_arrow().to_pylist()
    if vector_fingerprint(final)!=before: raise RuntimeError("post-migration vector check failed")
    database.drop_table(temp)
    result={"rows":len(final),"raw_chat_rows":sum(r["source_type"]=="raw_chat" for r in final),
      "raw_chat_metadata_rows":sum(r["source_type"]=="raw_chat_metadata" for r in final),
      "vectors_preserved":True,"vector_fingerprint":before,"embedding_calls":0}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=="__main__": migrate()
