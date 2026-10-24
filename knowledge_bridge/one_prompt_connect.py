"""Local-only handoff client. Never prints or persists the one-time capability."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_LOCAL_OPENER=build_opener(ProxyHandler({}),_NoRedirect())


def _open_local(request,timeout):
    return _LOCAL_OPENER.open(request,timeout=timeout)


def redeem(client: str, console: str, workspace_id: str, program_root: str,
           console_pid: int, token: str) -> str:
    parsed=urlsplit(console)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
        raise ValueError('控制台地址必须是本机 127.0.0.1 端口')
    if client not in {'codex','hermes','dsh','claude','cursor'} or not token or len(token)>128:
        raise ValueError('接入参数无效')
    root=Path(program_root).resolve()
    if Path(__file__).resolve().parents[1]!=root:
        raise ValueError('提示词中的界渡程序与正在执行的程序不一致')
    origin=f'http://127.0.0.1:{parsed.port}'
    try:
        with _open_local(origin+'/api/health',timeout=5) as response:
            health=json.load(response)
        if (not health.get('workspace_ready') or not health.get('program_root') or
                health.get('workspace_id')!=workspace_id or
                Path(health.get('program_root') or '').resolve()!=root or health.get('pid')!=console_pid):
            raise ValueError('界渡工作区或运行进程已变化，请在界渡重新复制接入提示词')
        body=json.dumps({'token':token,'workspace_id':workspace_id}).encode('utf-8')
        request=Request(origin+f'/api/client-access/handoff/{client}/redeem',body,
            {'Content-Type':'application/json','Origin':origin},method='POST')
        with _open_local(request,timeout=30) as response:
            result=json.load(response)
    except HTTPError as exc:
        raise ValueError(f'本机接入请求未通过（HTTP {exc.code}）；请在界渡重新复制接入提示词') from None
    except URLError as exc:
        raise OSError('无法连接本机界渡控制台') from None
    outcome=result.get('outcome')
    if outcome not in {'paired','reused','repaired','existing_configuration'}:
        raise ValueError('界渡没有返回可确认的接入结果')
    states={'paired':'已保存新的 MCP 配置与配对身份',
            'reused':'已保留现有 MCP 连接与权限',
            'repaired':'已修复当前身份的 MCP 配置并保留权限',
            'existing_configuration':'发现未验证的既有配置，已保留原样；需要在界渡人工核对身份'}
    collection=result.get('auto_archive')
    collection_note=({'enabled':'后台采集已启用，仍需查看首次同步状态',
        'existing_settings_preserved':'原有采集范围、暂停设置和游标已保留',
        'activation_failed':'后台采集启用失败，请查看界渡状态',
        'waiting_for_source':'等待本机来源可读后采集'}).get(collection,'采集状态请在界渡核对')
    restart='需要重开目标客户端后再做真实 MCP 读取验证。' if result.get('restart_required') else '请重新加载目标客户端 MCP，并做真实读取验证。'
    return f'{states[outcome]}。{collection_note}。{restart}'
