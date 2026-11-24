"""플랫폼 MCP 클라이언트가 세션을 모른다는 답을 받았을 때 (T85).

백엔드에 붙기 전의 vMCP 는 2026-07-28 의 첫 요청(`server/discover`)에 실패해 클라이언트가 세션을
여는 `initialize` 로 내려가고, 그 세션을 곧바로 끝낸 뒤 다음 요청에 본문 없는 404 로 답한다. 가짜
앞단은 FastMCP 서버 앞에서 그 모양만 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from fastmcp import FastMCP
from mcp import MCPError
from sse_starlette.sse import AppStatus

from daegam import mcp_client
from daegam.mcp_client import SESSION_GONE_MESSAGE, call_tool, list_tools, session_gone


@pytest.fixture(autouse=True)
def sse_not_exiting() -> None:
    """sse-starlette 의 프로세스 전역 종료 표시를 테스트마다 내린다. uvicorn 서버 하나가 내려가면
    서고, 선 뒤에는 같은 프로세스의 SSE 응답(initialize 의 답)이 곧바로 끝난다."""
    AppStatus.should_exit = False


@pytest.fixture(autouse=True)
def short_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client, "SESSION_RETRY_PAUSE_SECONDS", 0.01)


@asynccontextmanager
async def serve(app: Any) -> AsyncIterator[str]:
    """ASGI 앱을 uvicorn 으로 임의 포트에 띄우고 MCP 주소를 준다."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    try:
        while not uv.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = uv.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await task


def _calc() -> tuple[FastMCP, list[int]]:
    ran = [0]
    server = FastMCP("calc")

    @server.tool(name="Add")
    def add(a: int, b: int) -> int:
        """두 수를 더한다."""
        ran[0] += 1
        return a + b

    return server, ran


def _dropping(inner: Any, drops: dict[str, int]) -> Any:
    """세션이 있는 요청 가운데 `drops` 에 남은 수가 있는 메서드를 본문 없는 404 로 답하는 앞단."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await inner(scope, receive, send)
            return
        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        data = json.loads(body or b"{}")
        method = data.get("method") if isinstance(data, dict) else None
        if method == "server/discover":
            error = {"code": -32601, "message": "Method not found"}
            payload = json.dumps({"jsonrpc": "2.0", "id": data.get("id"), "error": error})
            headers = [(b"content-type", b"application/json")]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": payload.encode()})
            return
        if drops.get(method or "", 0) > 0:
            drops[method] -= 1
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await inner(scope, replay, send)

    return app


def test_a_listing_whose_session_ended_is_read_again() -> None:
    async def run() -> None:
        server, _ran = _calc()
        drops = {"tools/list": 2}
        async with serve(_dropping(server.http_app(), drops)) as url:
            listed = await list_tools(url, timeout=10)
        assert listed.tools == ["Add"] and drops["tools/list"] == 0

    asyncio.run(run())


def test_a_call_whose_session_ended_is_sent_again_and_runs_once() -> None:
    async def run() -> None:
        server, ran = _calc()
        drops = {"tools/call": 2}
        async with serve(_dropping(server.http_app(), drops)) as url:
            result = await call_tool(url, "Add", {"a": 1, "b": 2}, timeout=10)
        assert not result.is_error and (drops["tools/call"], ran[0]) == (0, 1)

    asyncio.run(run())


def test_a_server_that_keeps_ending_sessions_fails_with_that_answer() -> None:
    """제한 시간까지 계속 끝나면 시간 초과가 아니라 서버의 답으로 실패한다."""

    async def run() -> None:
        server, ran = _calc()
        async with serve(_dropping(server.http_app(), {"tools/list": 10**6})) as url:
            with pytest.raises(Exception) as ei:  # noqa: PT011 - 그룹에 싸여 올 수 있다
                await list_tools(url, timeout=2)
        assert session_gone(ei.value) and ran[0] == 0

    asyncio.run(run())


def test_only_the_session_message_counts() -> None:
    gone = MCPError(code=-32600, message=SESSION_GONE_MESSAGE)
    assert session_gone(gone)
    assert session_gone(ExceptionGroup("g", [ExceptionGroup("h", [gone])]))
    assert not session_gone(MCPError(code=-32600, message="Unauthorized"))
    assert not session_gone(ExceptionGroup("g", [gone, ValueError("x")]))
    assert not session_gone(TimeoutError())
