"""Recognized auxiliary envelopes and the older public assistant envelope.

These rules classify structure, never keywords in message text. Unknown shapes
remain reviewable. Agent-to-agent content is not an owner conversation.
"""

FORMAT_POLICY = 4


def public_question(event, session_id):
    """The desktop renders async question cards only as completed UI items."""
    payload = event.get('payload')
    if event.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'item_completed':
        return None
    item = payload.get('item')
    if not isinstance(item, dict) or item.get('type') != 'AgentMessage' or not (item.get('questions') or item.get('delivery') == 'async'):
        return None
    questions = item.get('questions')
    content = item.get('content')
    if (payload.get('thread_id') != session_id or not isinstance(payload.get('turn_id'), str)
            or item.get('phase') != 'final_answer' or item.get('delivery') != 'async'
            or not isinstance(item.get('id'), str) or not item['id'].startswith('call_')
            or not isinstance(questions, list) or not questions
            or not all(isinstance(q, dict) and isinstance(q.get('title'), str) and q['title']
                       and (q.get('options') is None or isinstance(q['options'], list) and all(isinstance(x, str) for x in q['options'])) for q in questions)
            or not isinstance(content, list) or not content
            or not all(isinstance(c, dict) and c.get('type') == 'Text' and isinstance(c.get('text'), str) and c['text'].strip() for c in content)):
        return {'status': 'needs_review', 'reason': 'unrecognized_public_question', 'metadata': {'item_type': 'AgentMessage'}}
    return {'id': 'codex-question:' + item['id'], 'text': '\n\n'.join(c['text'] for c in content)}


def auxiliary_kind(event):
    if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
        return None
    payload = event['payload']
    kind = event.get('type')
    if kind == 'token_usage_record' and 'usage' in payload and not ({'content', 'role'} & payload.keys()):
        return 'usage'
    if kind == 'inter_agent_communication_metadata' and set(payload) <= {'trigger_turn'} and isinstance(payload.get('trigger_turn'), bool):
        return 'agent_routing'
    if kind == 'realtime_item' and payload.get('type') in {'realtime_session_started', 'realtime_session_closed'}:
        if set(payload) <= {'type', 'id', 'realtime_session_id', 'outcome'} and payload.get('realtime_session_id'):
            return 'voice_lifecycle'
    if kind != 'response_item':
        return None
    subtype = payload.get('type')
    if subtype == 'agent_message' and isinstance(payload.get('author'), str) and isinstance(payload.get('recipient'), str):
        if payload['author'].startswith('/') and payload['recipient'].startswith('/') and isinstance(payload.get('content'), list):
            return 'agent_message'
    if subtype in {'tool_search_call', 'tool_search_output', 'web_search_call'}:
        if 'content' not in payload and 'role' not in payload and (payload.get('call_id') or payload.get('id')):
            return 'tool_activity'
    return None


def legacy_public_assistant(payload):
    """Old desktop output_text envelope; do not accept arbitrary phase-less rows."""
    meta = payload.get('internal_chat_message_metadata_passthrough')
    content = payload.get('content')
    return bool(
        payload.get('role') == 'assistant' and payload.get('phase') is None
        and isinstance(payload.get('id'), str) and payload['id']
        and isinstance(meta, dict) and isinstance(meta.get('turn_id'), str) and meta['turn_id']
        and payload.get('channel') is None and payload.get('recipient') is None
        and isinstance(content, list) and content
        and all(isinstance(item, dict) and item.get('type') == 'output_text'
                and isinstance(item.get('text'), str) for item in content)
    )
