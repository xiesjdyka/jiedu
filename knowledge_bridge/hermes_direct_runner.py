"""Run one isolated Hermes Agent turn in the child process, without its gateway."""
from __future__ import annotations

import json
import os
import sys


def install_http_send_gate():
    """Gate every SDK HTTP attempt, including retries inside Hermes' turn loop."""
    import httpx
    from .hermes_tool_bridge import authorize_model_send

    original_sync=httpx.Client.send
    original_async=httpx.AsyncClient.send
    def sync_send(client,request,*args,**kwargs):
        authorize_model_send()
        return original_sync(client,request,*args,**kwargs)
    async def async_send(client,request,*args,**kwargs):
        authorize_model_send()
        return await original_async(client,request,*args,**kwargs)
    httpx.Client.send=sync_send
    httpx.AsyncClient.send=async_send


def main():
    from hermes_cli.stream_json import StreamJsonEmitter
    from run_agent import AIAgent
    from .hermes_tool_bridge import authorize_model_send, install_hermes_tool, TOOLSET
    import toolsets

    enabled = os.environ.get('JIEDU_TOOLS_ENABLED') == '1'
    install_http_send_gate()
    if enabled:
        install_hermes_tool()
        selected = TOOLSET
    else:
        selected = 'jiedu-chat-only'
        toolsets.TOOLSETS[selected] = {'tools': [], 'includes': []}
        if toolsets.resolve_toolset(selected) != []:
            raise RuntimeError('Hermes 工具隔离未就绪')
    messages = json.loads(open(sys.argv[1], encoding='utf-8').read())
    if not isinstance(messages,list) or len(messages)<2 or messages[0].get('role')!='system' or messages[-1].get('role')!='user':
        raise ValueError('请求消息格式无效')
    if any(not isinstance(row,dict) or row.get('role') not in {'system','user','assistant'} or not isinstance(row.get('content'),str) for row in messages):
        raise ValueError('请求消息格式无效')
    emitter = StreamJsonEmitter(model=os.environ['JIEDU_HERMES_MODEL'])
    agent = None
    try:
        agent = AIAgent(provider=os.environ['JIEDU_HERMES_PROVIDER'],
                        api_key=os.environ[os.environ['JIEDU_HERMES_KEY_NAME']],
                        base_url=os.environ.get('JIEDU_HERMES_BASE_URL') or None,
                        model=os.environ['JIEDU_HERMES_MODEL'],
                        enabled_toolsets=[selected], max_iterations=6 if enabled else 1,
                        run_budget_seconds=120, quiet_mode=True, platform='tool',
                        skip_context_files=True, skip_memory=True, skip_background_review=True,
                        session_db=None, cwd=os.getcwd())
        allowed = {'product_operation','tool_search','tool_describe','tool_call'} if enabled else set()
        if not set(agent.valid_tool_names) <= allowed or (enabled and toolsets.resolve_toolset(TOOLSET) != ['product_operation']):
            raise RuntimeError('Hermes 工具集合超出界渡本轮授权')
        # Both streaming and non-streaming retry paths pass through these methods.
        # The parent checks current provider, cancellation, permissions and result scopes
        # synchronously before each actual request, including a post-tool follow-up.
        for method_name in ('_interruptible_streaming_api_call','_interruptible_api_call'):
            original=getattr(agent,method_name)
            def guarded(*args,_original=original,**kwargs):
                authorize_model_send()
                return _original(*args,**kwargs)
            setattr(agent,method_name,guarded)
        agent.suppress_status_output = True
        emitter.attach(agent)
        result = agent.run_conversation(messages[-1]['content'], system_message=messages[0]['content'],
                                        conversation_history=messages[1:-1])
        return emitter.emit_result(result, exit_code=1 if result.get('failed') else 0)
    except Exception as exc:
        return emitter.emit_result({'final_response':'','error':type(exc).__name__}, exit_code=1)
    finally:
        if agent:
            try:agent.close()
            except Exception:pass


if __name__ == '__main__':
    raise SystemExit(main())
