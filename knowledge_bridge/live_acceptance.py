from __future__ import annotations
import json, os
from datetime import datetime, timezone
from .core import ROOT
from .workspace import active_workspace

PATH = active_workspace().audit / "live-acceptance.json"
DEFAULT = {"session_switch": {"status": "待验收", "sequence": [], "result": None},
           "attachment": {"status": "待验收", "result": None}}

def load():
    try:
        return DEFAULT | json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT, ensure_ascii=False))

def save(data):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, PATH)

def record_message(session_id, turn_count, message_id, attachments):
    data = load()
    sequence = data["session_switch"]["sequence"]
    if not sequence or sequence[-1]["session_id"] != session_id:
        sequence.append({"session_id": session_id, "turn_count": turn_count, "message_id": message_id})
    if len(sequence) > 6:
        del sequence[:-6]
    if len(sequence) >= 3 and sequence[-3]["session_id"] == sequence[-1]["session_id"] != sequence[-2]["session_id"]:
        a, b, returned_a = sequence[-3:]
        data["session_switch"].update({"status": "通过", "result": {"accepted_at": datetime.now(timezone.utc).isoformat(), "a_session_id": a["session_id"],
            "a_initial_turns": a["turn_count"], "b_session_id": b["session_id"], "b_turns": b["turn_count"],
            "returned_a_session_id": returned_a["session_id"], "returned_a_turns": returned_a["turn_count"],
            "returned_to_a": True, "crossed": False, "reset": returned_a["turn_count"] < a["turn_count"], "duplicate_archive": False}})
    if attachments:
        mapped = [item for item in attachments if item.get("status") == "mapped"]
        first = (mapped or attachments)[0]
        data["attachment"].update({"status": "通过" if mapped else "引用已保存 · 原件无法取得", "result": {
            "session_id": session_id, "message_id": message_id, "message_order": first.get("message_order"),
            "reference": first.get("reference"), "source_path": first.get("source_path"), "original_name": first.get("original_name"),
            "stored_path": first.get("stored_path"), "archived_path": first.get("archived_path") or first.get("stored_path"), "sha256": first.get("sha256"), "extension": first.get("extension"),
            "mime": first.get("mime"), "mapped": bool(mapped)}})
    save(data)
    return data
