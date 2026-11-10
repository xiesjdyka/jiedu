"""Bounded, authenticated calls from MCP tools to the local Console."""
import json
import sys

import anyio
from anyio import from_thread
import httpx


class ConsoleHTTPError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"Console 返回 HTTP {status}")
        self.status = status
        self.detail = detail


def _diagnostic(message):
    print(f"Jiedu MCP: {message}", file=sys.stderr, flush=True)


async def _post(url, payload, headers, timeout, max_bytes):
    with anyio.fail_after(timeout):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
        ) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise ConnectionError("界渡控制台的响应超过大小限制")
                    data.extend(chunk)
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeError):
                    result = None
                if response.is_error or response.is_redirect:
                    detail = result.get("detail") if isinstance(result, dict) else None
                    raise ConsoleHTTPError(response.status_code, detail)
                if not isinstance(result, dict):
                    raise ConnectionError("界渡控制台返回了无效响应")
                return result


def post_console(port, path, payload, *, authorization, workspace_id=None,
                 timeout=20.0, max_bytes=16 * 1024 * 1024):
    headers = {"Authorization": authorization}
    if workspace_id is not None:
        headers["X-Jiedu-Workspace-ID"] = workspace_id
    url = f"http://127.0.0.1:{int(port)}{path}"
    _diagnostic("connecting to local Console.")
    try:
        try:
            # The SDK runs sync tools in an AnyIO worker. Rejoin the request
            # scope so client disconnects also cancel the HTTP operation.
            result = from_thread.run(_post, url, payload, headers, timeout, max_bytes)
        except anyio.NoEventLoopError:
            result = anyio.run(_post, url, payload, headers, timeout, max_bytes)
    except httpx.ConnectTimeout:
        _diagnostic("local Console connection timed out.")
        raise ConnectionError("无法连接界渡控制台，连接超时") from None
    except (TimeoutError, httpx.TimeoutException):
        _diagnostic("local Console request timed out.")
        raise TimeoutError("界渡控制台请求超时，请稍后重试") from None
    except httpx.RequestError:
        _diagnostic("local Console connection failed.")
        raise ConnectionError("无法连接界渡控制台，请确认界渡已启动") from None
    except (ConsoleHTTPError, ConnectionError):
        _diagnostic("local Console returned an error.")
        raise
    _diagnostic("local Console response received.")
    return result
