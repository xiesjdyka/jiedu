"""Truthful capability registry for conversation source adapters.

Acquisition capabilities stay independent from MCP identity and knowledge-read
permissions. Unknown products never gain support through brand-name guessing.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


CAPABILITY_KEYS = (
    "source_discovery", "new_session_discovery", "user_text",
    "assistant_final_text", "attachments", "continuous_collection",
    "restart_resume", "standard_archive", "searchable_archive",
    "knowledge_organization_input",
)


def _capabilities(**values: str) -> dict[str, str]:
    result = {key: "unsupported" for key in CAPABILITY_KEYS}
    result.update(values)
    return result


def _codex_probe() -> dict:
    from .codex_source_adapter import CodexSourceAdapter
    status = CodexSourceAdapter().status()
    enabled = bool(status.get("enabled"))
    policy = status.get('archive_policy')
    found = int(status.get("discovered_session_count") or 0)
    sqlite = status.get('sqlite') or {}
    sqlite_detected = bool(sqlite.get('detected'))
    sqlite_sources = sqlite.get('sources') or []
    sqlite_pending = any((item.get('history') or {}).get('status') not in {'complete','completed_with_review'}
                         for item in sqlite_sources)
    sqlite_ready = any(item.get('readable') and (item.get('history') or {}).get('status') in {'complete','completed_with_review'}
                       for item in sqlite_sources)
    authorized = int(status.get("authorized_session_count") or 0)
    missing = int(status.get("missing_attachment_count") or 0)
    error = status.get("last_error")
    failed = int(status.get("failed_session_count") or 0)
    unresolved = int(status.get("needs_review_event_count") or 0)
    synced = bool(status.get("last_sync_at"))
    try:
        sync_age=(datetime.now(timezone.utc)-datetime.fromisoformat(str(status['last_sync_at']).replace('Z','+00:00'))).total_seconds()
        recent_sync=0<=sync_age<120
    except (KeyError,TypeError,ValueError):recent_sync=False
    pending = int(status.get("pending_backfill_count") or 0)
    gap = bool(status.get("gap_detected"))
    if error or failed:
        state, summary = "error", "自动归档异常"
        detail = f"最近错误：{error or f'{failed} 个会话同步失败'}"
    elif not enabled:
        state, summary = "paused", "自动归档已暂停"
        detail = f"已发现 {found} 个会话；恢复后才会继续采集。"
    elif not found and not sqlite_detected:
        state, summary = "source_unavailable", "未发现 Codex 会话来源"
        detail = "采集开关已开启，但没有发现可读取的源会话，因此不能标为正在归档。"
    elif policy not in {'all','selected'}:
        state, summary = 'unverified', '采集范围需要核对'
        detail = '未取得明确的 all 或 selected 采集策略；旧同步记录不能证明当前正在采集。'
    elif not authorized and (policy=='selected' or not sqlite_ready):
        state, summary = "awaiting_authorization", "已发现 Codex 会话，尚无获准采集范围"
        detail = f"已发现 {found} 个 JSONL 会话，当前授权会话为 0；SQLite 来源尚无可确认的完成状态。"
    elif not synced:
        state, summary = "awaiting_first_sync", "已发现来源，等待首次同步"
        detail = f"已发现 {found} 个会话，已授权 {authorized} 个；尚无真实成功同步记录。"
    elif gap or pending or sqlite_pending or unresolved:
        state, summary = "degraded", "自动归档运行中，但仍有待处理范围"
        detail = f"已发现 {found} 个会话，已授权 {authorized} 个；待补档 {pending} 个；未识别来源记录 {unresolved} 项；断档检测：{'有' if gap else '无'}。"
    elif not recent_sync:
        state, summary = 'degraded', '自动归档最近未获运行证据'
        detail = '最近一次成功同步已超过两分钟，或缺少有效同步时间；请检查后台采集器。'
    else:
        state, summary = "automatic", "本机采集器正在自动归档"
        detail = f"已发现 {found} 个会话，已授权 {authorized} 个；缺失附件 {missing} 个；当前没有待补档或断档。"
    return {
        "state": state,
        "source_detected": found > 0 or sqlite_detected,
        "summary": summary, "detail": detail,
        "saved": {"message_count": int(status.get("total_archived_message_count") or 0),
                  "last_checked_at": status.get("last_sync_at"),
                  "last_saved_at": status.get("last_message_sync_at"),
                  "location": "90_原始档案/ChatGPT",
                  "last_result": "同步异常" if error or failed else "已检查，仍有待补档" if gap or pending else "已检查来源" if synced else "尚无同步记录"},
        "capabilities": _capabilities(
            source_discovery="supported", new_session_discovery="supported",
            user_text="supported", assistant_final_text="supported",
            attachments="supported", continuous_collection="supported",
            restart_resume="supported", standard_archive="supported",
            searchable_archive="supported", knowledge_organization_input="review_required",
        ),
    }


def _hermes_probe() -> dict:
    from .hermes_session_adapter import selected_db_path, status
    path = selected_db_path()
    schema_ok = False
    if path.is_file():
        try:
            with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1)) as db:
                tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
                schema_ok = {"sessions", "messages"}.issubset(tables)
        except sqlite3.Error:
            pass
    runtime = status()
    runtime_status = str(runtime.get("status") or "unknown")
    error = runtime.get("last_error")
    try:
        sync_age = (datetime.now(timezone.utc) - datetime.fromisoformat(runtime["last_sync"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        sync_age = None
    watching = runtime_status in {"watching", "running"} and runtime.get('worker_alive') is not False and sync_age is not None and 0 <= sync_age < 120
    ready = schema_ok and watching
    if error:
        state, summary, detail = "error", "Hermes 采集异常", f"最近错误：{error}"
    elif runtime_status == "paused":
        state, summary, detail = "paused", "Hermes 采集已暂停", "本机会话库保持不变，恢复后才会继续采集。"
    elif runtime.get('worker_alive') is False:
        state, summary, detail = "error", "Hermes 采集线程已停止", "已保存的聊天仍保留；需要恢复采集器，旧同步时间不代表仍在运行。"
    elif ready:
        state, summary, detail = "automatic", "本机采集器正在自动归档", "只读读取本机 SQLite；双方正文、附件引用和续扫游标已接入。"
    elif schema_ok and runtime_status in {"watching", "running"}:
        state, summary, detail = "degraded", "Hermes 采集状态需要检查", "曾显示监听中，但最近两分钟没有可确认的同步；不会标为正在自动归档。"
    elif schema_ok:
        state, summary, detail = "awaiting_first_sync", "来源可用，等待采集器运行", "已确认会话库结构，尚无正在监听的运行状态。"
    else:
        state, summary, detail = "source_unavailable", "未发现 Hermes 本机会话库", "没有找到可验证的 sessions/messages 来源结构。"
    return {
        "state": state,
        "source_detected": schema_ok,
        "summary": summary, "detail": detail,
        "saved": {"message_count": int(runtime.get("synced_messages") or 0),
                  "last_checked_at": runtime.get("last_sync"),
                  "last_saved_at": None,
                  "location": "90_原始档案/Hermes",
                  "last_result": "同步异常" if error else "已暂停" if runtime_status=="paused" else "已检查来源" if sync_age is not None else "尚无同步记录"},
        "capabilities": _capabilities(
            source_discovery="supported", new_session_discovery="supported",
            user_text="supported", assistant_final_text="supported", attachments="supported",
            continuous_collection="supported", restart_resume="supported",
            standard_archive="legacy_compatible", searchable_archive="supported",
            knowledge_organization_input="review_required",
        ),
    }


def legacy_client_view(row: dict, adapters: list) -> dict:
    """Describe an existing legacy connection without issuing an identity or policy."""
    source = for_client(row.get('id'), adapters)
    read_tools = {'knowledge_search', 'search_memory', 'read_source', 'context_state',
                  'memory_bootstrap', 'get_project_state', 'get_raw_message'}
    reads = [call for call in row.get('recent_mcp_calls', [])
             if call.get('tool') in read_tools and call.get('outcome') == 'success']
    return {'claimed_id': row.get('id'), 'display_name': row.get('client'),
            'transport': row.get('connection'), 'status': 'legacy_unverified',
            'configured': bool(row.get('configured')),
            'public_access_unchanged': bool(row.get('client_access')),
            'read_tools': sorted(tool for tool, enabled in row.get('tool_permissions', {}).items()
                                 if enabled and tool in read_tools),
            'permission_summary': row.get('permission'),
            'tool_permissions': {tool: bool(enabled) for tool,enabled in row.get('tool_permissions',{}).items()
                                 if tool in {'memory_bootstrap','get_project_state','search_memory','get_raw_message',
                                             'save_raw_conversation','create_candidate','submit_review'}},
            'permission_controls_supported': True,
            'last_successful_read': reads[-1] if reads else None,
            'protected_access': 'deny', 'connection_status': row.get('status'),
            'source_adapter': source}


def _doubao_probe() -> dict:
    from .doubao_session_adapter import status
    runtime = status()
    found = bool(runtime.get("source_found"))
    public_count = int(runtime.get("public_message_count") or runtime.get("archived_message_count") or 0)
    review_count = int(runtime.get("review_message_count") or runtime.get("visibility_review_count") or 0)
    runtime_status = str(runtime.get("status") or "unknown")
    error = runtime.get("last_error")
    paused = {
        "no_active_doubao_binding": "没有可用的豆包归档绑定",
        "no_doubao_binding": "尚未绑定豆包归档来源",
        "multiple_active_doubao_bindings_require_selection": "存在多个活动豆包绑定，需要明确选择",
        "bound_client_revoked_or_collection_paused": "绑定已撤销或归档权限未允许",
    }
    if error:
        state, summary, detail = "error", "豆包候选采集异常", f"最近错误：{error}"
    elif not found or runtime_status == "source_not_found":
        state, summary, detail = "source_unavailable", "未发现豆包工作版本机会话来源", "未发现可验证的本机轨迹目录。"
    elif runtime_status in paused:
        state, summary, detail = "paused", "豆包自动归档未运行", paused[runtime_status]
    elif runtime_status == "candidate_collection_active_public_visibility_limited":
        state, summary = "partial", "归档不完整：只自动收集候选"
        detail = f"公开正文 {public_count} 条，待核对候选 {review_count} 条；公开助手最终回复和附件来源仍缺失。"
    elif runtime_status == "structured_cache_detected_awaiting_body_validation":
        cache = runtime.get("structured_cache") or {}
        state, summary = "unverified", "已发现豆包结构化缓存，正文尚未通过真实样本验证"
        detail = (f"已识别 {int(cache.get('conversation_count') or 0)} 个近期对话；"
                  f"隔离保留 {int(cache.get('validation_sample_count') or 0)} 条待核对样本；"
                  f"位置 {cache.get('validation_path') or '尚无样本'}。"
                  "新缓存消息暂不公开发布。豆包已清理的旧缓存不会猜测补全。")
    elif runtime_status == "validation_capture_full":
        state, summary = "needs_attention", "豆包验证样本区已满"
        detail = str(runtime.get("last_error") or "已保留原有样本，未覆盖、未提升新快照。")
    elif runtime_status in {"validation_capture_failed", "cache_snapshot_promotion_failed"}:
        state, summary = "error", "豆包验证样本未能安全保留"
        detail = str(runtime.get("last_error") or "新快照未提升，不会误报样本已保留。")
    elif runtime_status == "ready":
        state, summary, detail = "awaiting_first_sync", "豆包来源已绑定，等待首次采集", "尚无成功采集状态，不能标为正在归档。"
    else:
        state, summary, detail = "unverified", "豆包采集状态尚未验证", f"采集器返回状态：{runtime_status}。"
    return {
        "state": state,
        "source_detected": found,
        "summary": summary, "detail": detail,
        "capabilities": _capabilities(
            source_discovery="supported", new_session_discovery="supported",
            user_text="schema_validation_required", assistant_final_text="unsupported_until_validated",
            attachments="references_only", continuous_collection="filesystem_events_with_verification_scan",
            restart_resume="supported", standard_archive="partial",
            searchable_archive="verified_messages_only",
            knowledge_organization_input="verified_messages_only",
        ),
    }


def _generic_probe() -> dict:
    return {
        "state": "push_only", "source_detected": False,
        "summary": "等待客户端主动提交原始对话",
        "detail": "MCP 配对只提供身份与知识读取；客户端调用原始归档工具后，正文和附件才会进入标准档案。",
        "capabilities": _capabilities(
            source_discovery="unsupported", new_session_discovery="submitted_only",
            user_text="submitted_only", assistant_final_text="submitted_only",
            attachments="submitted_only", continuous_collection="client_dependent",
            restart_resume="supported", standard_archive="supported",
            searchable_archive="supported_after_submit",
            knowledge_organization_input="review_required_after_submit",
        ),
    }


def _local_collector_saved(adapter, state: dict, source_key: str) -> dict:
    """Count this local collector's sessions in the standard archive index."""
    from .durable_state import read_object, StateCorruptError

    registry = read_object(adapter.context.archive_registry, {"sessions": {}})
    sessions = registry.get("sessions")
    if not isinstance(sessions, dict):
        raise StateCorruptError("会话目录表结构无效")
    indexed = []
    for item in sessions.values():
        if not isinstance(item, dict) or item.get("source_key") != source_key:
            continue
        if not str(item.get("source_instance") or "").startswith(source_key + ":"):
            continue
        relative = item.get("relative_path")
        if not isinstance(relative, str):
            continue
        folder = (adapter.context.root / relative).resolve()
        try:
            folder.relative_to(adapter.context.raw_archive.resolve())
        except ValueError:
            continue
        manifest = folder / "03_元数据" / "manifest.json"
        if manifest.is_file() and int(item.get("message_count") or 0) > 0:
            indexed.append(item)
    messages = sum(int(item.get("message_count") or 0) for item in indexed)
    recorded = int((state.get("history") or {}).get("archived_messages") or 0)
    error = state.get("last_error")
    return {"session_count": len(indexed), "message_count": messages,
            "last_checked_at": state.get("last_sync_at"),
            "last_saved_at": max((str(item.get("updated_at") or "") for item in indexed), default=None),
            "location": "90_原始档案/" + {"dsh":"DSH","claude":"Claude","cursor":"Cursor"}.get(source_key,"其他"),
            "last_result": "档案目录待核对" if recorded > messages else
                           "采集异常" if error else "已检查来源" if state.get("last_sync_at") else "尚无同步记录"}


def _dsh_probe() -> dict:
    from .dsh_source_adapter import DSHSourceAdapter
    adapter = DSHSourceAdapter()
    state = adapter.status()
    saved = _local_collector_saved(adapter, state, "dsh")
    index_gap = saved["last_result"] == "档案目录待核对"
    found = len(state["discovered_sessions"])
    enabled = state["enabled"]
    return {
        "state": ("awaiting_owner" if not state.get("ever_configured") else
                  "paused" if not enabled else "source_unavailable" if not found else
                  "degraded" if state.get("last_error") or index_gap else "automatic" if state.get("last_sync_at")
                  else "awaiting_first_sync"),
        "source_detected": bool(found),
        "summary": ("等待你接入 DSH 聊天归档" if not state.get("ever_configured") else
                    "DSH 采集已暂停" if not enabled else
                    "DSH 来源不可用" if not found else
                    "DSH 归档需要检查" if state.get("last_error") or index_gap else
                    "DSH 本机归档已检查" if state.get("last_sync_at") else "等待 DSH 首次采集"),
        "detail": f"本机发现 {found} 个会话；仅采集获准范围中的用户消息和已完成最终答复。"
                  "工具与思考不进入档案。",
        "saved": saved,
        "capabilities": _capabilities(source_discovery="supported", new_session_discovery="supported",
            user_text="supported", assistant_final_text="supported", attachments="explicit_references",
            continuous_collection="polling", restart_resume="supported", standard_archive="supported",
            searchable_archive="supported_after_save", knowledge_organization_input="review_required"),
    }


def _claude_probe() -> dict:
    from .claude_source_adapter import ClaudeSourceAdapter
    adapter = ClaudeSourceAdapter()
    state = adapter.status()
    saved = _local_collector_saved(adapter, state, "claude")
    index_gap = saved["last_result"] == "档案目录待核对"
    found = state["source_detected"]
    enabled = state["enabled"]
    errors = state["history"]["current_decode_errors"]
    return {
        "state": ("awaiting_owner" if not state.get("ever_configured") else
                  "paused" if not enabled else "source_unavailable" if not found else
                  "partial" if errors or state.get("last_error") or index_gap else
                  "automatic" if state.get("last_sync_at") else "awaiting_first_sync"),
        "source_detected": found,
        "summary": ("等待你接入 Claude 聊天归档" if not state.get("ever_configured") else
                    "Claude 采集已暂停" if not enabled else
                    "Claude 本机缓存不可用" if not found else
                    "Claude 本机归档需要检查" if index_gap else
                    "Claude 本机缓存仅部分可读" if errors else
                    "Claude 本机归档已检查" if state.get("last_sync_at") else "等待 Claude 首次采集"),
        "detail": "只采集本机当前缓存中的用户消息和已完成最终答复；云端及已清理历史总量未知。",
        "saved": saved,
        "capabilities": _capabilities(source_discovery="supported", new_session_discovery="local_cache_only",
            user_text="supported", assistant_final_text="supported", attachments="explicit_references_only",
            continuous_collection="polling_current_cache", restart_resume="supported",
            standard_archive="local_cache_only", searchable_archive="supported_after_save",
            knowledge_organization_input="review_required"),
    }


def _cursor_probe() -> dict:
    from .cursor_source_adapter import CursorSourceAdapter
    adapter = CursorSourceAdapter()
    state = adapter.status()
    saved = _local_collector_saved(adapter, state, "cursor")
    configured = state.get("ever_configured")
    enabled = state.get("enabled")
    found = state.get("source_detected")
    error = state.get("last_error")
    indexed_gap = (state.get("indexed_without_transcript") or {}).get("in_scope_count") or 0
    pending = (state.get("history") or {}).get("pending_turns") or 0
    return {"state": "awaiting_owner" if not configured else "paused" if not enabled else
            "source_unavailable" if not found else "degraded" if error or indexed_gap else
            "automatic" if state.get("last_sync_at") else "awaiting_first_sync",
            "source_detected": bool(found),
            "summary": "等待你接入 Cursor" if not configured else "Cursor 采集已暂停" if not enabled else
                       "Cursor 来源不可用" if not found else "Cursor 归档需要检查" if error else
                       "Cursor 有索引历史待核验" if indexed_gap else
                       "Cursor 等待当前答复完成" if pending else "Cursor 本机归档已检查",
            "detail": "获准范围内的用户正文和成功结束回合的最终答复进入标准档案；进度原文单独可查。"
                      + (f"另有 {indexed_gap} 个非空索引会话缺少结束转录，暂未入档。" if indexed_gap else "")
                      + "图片附件已发送链路尚未真实验证。",
            "saved": saved,
            "capabilities": _capabilities(source_discovery="supported", new_session_discovery="local_transcripts_with_index_gap_report",
                user_text="supported", assistant_final_text="verified_completed_turns_only",
                attachments="unverified", continuous_collection="polling_after_owner_connection",
                restart_resume="supported", standard_archive="verified_messages_only",
                searchable_archive="verified_messages_only", knowledge_organization_input="review_required")}


def _four_probe(source: str) -> dict:
    from .four_local_collectors import LocalCompletedCollector, SOURCES
    adapter = LocalCompletedCollector(source)
    state = adapter.status()
    saved = _local_collector_saved(adapter, state, source)
    configured = state.get("ever_configured")
    enabled = state.get("enabled")
    found = state.get("source_detected")
    error = state.get("last_error")
    pending = (state.get("history") or {}).get("pending_turns") or 0
    unverified = (state.get("history") or {}).get("unverified_turns") or 0
    gap = (state.get("indexed_without_transcript") or {}).get("in_scope_count") or 0
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(
            str(state["last_sync_at"]).replace("Z", "+00:00"))).total_seconds()
        recent = 0 <= age < 120
    except (KeyError, TypeError, ValueError):
        recent = False
    stale = bool(state.get("last_sync_at")) and not recent
    label = SOURCES[source]
    attachment_detail = ("已发送本地图片逐件核验并归档，缺原件的引用待核对。" if source == "workbuddy" else
                         "图片仅保留来源引用，尚未取得可核验原件。" if source == "zcode" else
                         "真实已发送附件待样本验证。")
    return {"state": "awaiting_owner" if not configured else "paused" if not enabled else
            "source_unavailable" if not found else "degraded" if error or unverified or gap or stale else
            "automatic" if state.get("last_sync_at") else "awaiting_first_sync",
            "source_detected": bool(found),
            "summary": f"等待你接入 {label}" if not configured else f"{label} 采集已暂停" if not enabled else
                       f"{label} 来源不可用" if not found else f"{label} 归档需要检查" if error or unverified else
                       f"{label} 有索引历史待核验" if gap else
                       f"{label} 最近检查已过期" if stale else
                       f"{label} 等待当前答复完成" if pending else f"{label} 本机归档已检查",
            "detail": "只保存获准来源中已完成轮次的用户正文与最终答复；思考、工具和系统注入排除。"
                      + (f"范围内另有 {gap} 个索引会话缺少可读转录。" if gap else "")
                      + "进度说明只在私有原文查看；" + attachment_detail,
            "saved": saved,
            "capabilities": _capabilities(source_discovery="supported", new_session_discovery="local_index_only",
                user_text="completed_turns_only", assistant_final_text="verified_completed_turns_only",
                attachments="verified_local_images_only" if source == "workbuddy" else "references_only" if source == "zcode" else "unverified", continuous_collection="supported",
                restart_resume="supported", standard_archive="verified_messages_only",
                searchable_archive="verified_messages_only", knowledge_organization_input="review_required")}


def _workbuddy_probe(): return _four_probe("workbuddy")
def _opencode_probe(): return _four_probe("opencode")
def _zcode_probe(): return _four_probe("zcode")
def _kimi_probe(): return _four_probe("kimi")


ADAPTERS = (
    ("codex_local", "Codex", "authorized_local_read", _codex_probe),
    ("hermes_local", "Hermes", "authorized_local_read", _hermes_probe),
    ("dsh_local", "DSH", "authorized_local_read", _dsh_probe),
    ("claude_local", "Claude", "authorized_local_read", _claude_probe),
    ("cursor_local", "Cursor", "authorized_local_read", _cursor_probe),
    ("workbuddy_local", "WorkBuddy", "authorized_local_read", _workbuddy_probe),
    ("opencode_local", "OpenCode", "authorized_local_read", _opencode_probe),
    ("zcode_local", "ZCode", "authorized_local_read", _zcode_probe),
    ("kimi_local", "Kimi Work", "authorized_local_read", _kimi_probe),
    ("doubao_work_local", "豆包工作版", "authorized_local_read", _doubao_probe),
    ("generic_mcp_push", "通用 MCP", "explicit_client_push", _generic_probe),
)


def adapter_id_for(client_type: str | None) -> str:
    return {"codex": "codex_local", "hermes": "hermes_local",
            "dsh": "dsh_local", "claude": "claude_local", "cursor": "cursor_local",
            "workbuddy": "workbuddy_local", "opencode": "opencode_local",
            "zcode": "zcode_local", "kimi": "kimi_local",
            "doubao_work": "doubao_work_local"}.get(
        (client_type or "").strip().casefold(), "generic_mcp_push")


def matrix() -> list[dict]:
    rows = []
    for adapter_id, label, acquisition, probe in ADAPTERS:
        try:
            runtime = probe()
        except Exception as exc:
            runtime = {"state": "error", "source_detected": False,
                       "summary": "能力探测失败", "detail": type(exc).__name__,
                       "capabilities": _capabilities()}
        runtime_view = {key: runtime.get(key) for key in ("state", "source_detected", "summary", "detail")}
        rows.append({"adapter_id": adapter_id, "label": label,
                     "acquisition": acquisition, **runtime, "runtime": runtime_view})
    return rows


def for_client(client_type: str | None, rows: list[dict] | None = None) -> dict:
    adapter_id = adapter_id_for(client_type)
    if rows is not None:
        return next(row for row in rows if row["adapter_id"] == adapter_id)
    adapter = next(row for row in ADAPTERS if row[0] == adapter_id)
    adapter_key, label, acquisition, probe = adapter
    try:
        runtime = probe()
    except Exception as exc:
        runtime = {"state": "error", "source_detected": False,
                   "summary": "能力探测失败", "detail": type(exc).__name__,
                   "capabilities": _capabilities()}
    runtime_view = {key: runtime.get(key) for key in ("state", "source_detected", "summary", "detail")}
    return {"adapter_id": adapter_key, "label": label,
            "acquisition": acquisition, **runtime, "runtime": runtime_view}


def connection_status(client: dict, archive: dict, policies: list[dict], adapter: dict) -> dict:
    """Describe this identity without attributing a global collector to it."""
    from .client_access import ClientOperation, KnowledgeScope, default_client_decision
    decision = next((row.get("decision") for row in policies
                     if row.get("scope") == "evidence" and row.get("operation") == "raw_archive"
                     and not row.get("selector")), default_client_decision(KnowledgeScope.EVIDENCE,ClientOperation.RAW_ARCHIVE).value)
    if client.get("status") == "revoked":
        return {"state": "revoked", "summary": "当前客户端已撤销",
                "detail": "该身份不会继续提交或被归因为自动归档；已有档案保持不变。"}
    if not archive.get("bound"):
        return {"state": "source_unbound", "summary": "当前客户端尚未绑定聊天来源",
                "detail": "即使本机存在独立采集器，也不能把它的运行状态归到这条连接。"}
    adapter_id = adapter.get("adapter_id")
    runtime = adapter.get("runtime") or adapter
    if adapter_id in {"codex_local", "hermes_local", "dsh_local", "claude_local", "cursor_local",
                      "workbuddy_local", "opencode_local", "zcode_local", "kimi_local", "doubao_work_local"}:
        return {"state": "independent_collector_" + str(runtime.get("state") or "unverified"),
                "summary": (runtime.get("summary") or "本机采集状态未验证") + "（独立于此连接）",
                "detail": (runtime.get("detail") or "") + " 该采集器独立运行；MCP 读取和主动提交权限另行判定。"}
    if decision != "allow":
        return {"state": "permission_required" if decision == "ask" else "permission_denied",
                "summary": "当前客户端等待一次性归档确认" if decision == "ask" else "当前客户端未获准归档",
                "detail": "来源已登记，但原始归档权限不是永久允许。"}
    if archive.get("last_archived_at") or int(archive.get("message_count") or 0) > 0:
        return {"state": "push_received", "summary": "已收到客户端主动提交",
                "detail": f"已收到 {int(archive.get('message_count') or 0)} 条消息；源端历史总量仍未知。"}
    return {"state": "awaiting_push", "summary": "等待客户端主动提交原始对话",
            "detail": "身份和知识读取可用，但当前连接尚未提交聊天正文。"}


def generator_status() -> dict:
    return {
        "state": "not_implemented", "summary": "未知来源适配器自动生成尚未实现",
        "requirements": ["公开且稳定的消息格式", "会话与消息身份", "双方最终正文事件",
                         "附件关联证据", "隔离验证与重启去重测试"],
        "model_dependency": "需要生成器或人工开发；当前运行服务不安装、不假装具备此能力。",
    }
