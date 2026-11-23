"""거절된 Tool 호출 (P52 5절, 능력 `tool_refusals`).

ToolHive 의 프록시와 vMCP 는 Cedar 가 거절한 `tools/call` 을 HTTP 403 과 메시지 `Unauthorized` 의
JSON-RPC 오류로, 한도에 걸린 호출을 429 와 `Rate limit exceeded` 로 답한다(F0 4번). 가짜 서버는 그
앞단을 흉내 내는 ASGI 감싸개이고, 이름을 정한 Tool 의 호출만 그렇게 답하고 나머지는 FastMCP 에
넘긴다. fastmcp 클라이언트는 둘 다 `MCPError` 로 올리고, 런타임은 그것을 모델이 보는 Tool 오류로
바꿔 턴을 이어 간다. 그 밖의 `MCPError` 는 예전처럼 올라간다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp import MCPError

from runtime.config import parse_config
from runtime.mcp import LIMITED_MESSAGE, REFUSED_MESSAGE, SESSION_GONE_MESSAGE, ToolSources
from tests.conftest import chat, event_names, run_app, serve

# 앞단이 거절하는 Tool 마다의 (HTTP 상태, JSON-RPC 오류 메시지)
REFUSALS: dict[str, tuple[int, str]] = {
    "Guarded": (403, REFUSED_MESSAGE),
    "Limited": (429, LIMITED_MESSAGE),
    "Broken": (500, "Internal error"),
}


def _server() -> FastMCP:
    server = FastMCP("ruled")
    for name in ("Add", *REFUSALS):

        def tool(a: int, b: int) -> int:
            return a + b

        server.tool(name=name)(tool)
    return server


def _front(inner: Any) -> Any:
    """`REFUSALS` 의 Tool 호출을 그 상태와 메시지의 JSON-RPC 오류로 답하는 앞단이다."""

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
        name = (data.get("params") or {}).get("name") if isinstance(data, dict) else None
        if data.get("method") == "tools/call" and name in REFUSALS:
            status, text = REFUSALS[name]
            error = {"code": -32603, "message": text}
            payload = json.dumps({"jsonrpc": "2.0", "id": data.get("id"), "error": error})
            headers = [(b"content-type", b"application/json")]
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": payload.encode()})
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


@asynccontextmanager
async def _ruled_server() -> AsyncIterator[str]:
    async with serve(_front(_server().http_app())) as base_url:
        yield f"{base_url}/mcp"


def _doc(support_doc: dict[str, Any], url: str) -> dict[str, Any]:
    support_doc["tools"][0]["url"] = url
    support_doc["tools"][0]["allow"] = ["Add", *REFUSALS]
    return support_doc


async def test_the_client_raises_the_refusal_messages_the_runtime_reads() -> None:
    """앞단의 답이 fastmcp 클라이언트에서 그 메시지의 `MCPError` 가 되는지 본다. 런타임은 그
    메시지로 거절과 한도를 가른다."""
    async with _ruled_server() as url, Client(url) as client:
        for name, (_status, text) in REFUSALS.items():
            with pytest.raises(MCPError) as ei:
                await client.call_tool(name, {"a": 1, "b": 2})
            assert ei.value.message == text, name


@pytest.mark.parametrize(
    ("name", "said"),
    [("Guarded", "도구 소스가 거절했다"), ("Limited", "호출 한도에 걸렸다")],
)
async def test_a_refusal_and_a_limit_become_tool_errors(support_doc, name, said) -> None:
    async with _ruled_server() as url:
        sources = ToolSources(parse_config(_doc(support_doc, url)))
        await sources.load()
        tools = {t.name: t for t in sources.tools_by_ref["tools-calculator"]}
        message = await tools[name].ainvoke(
            {"type": "tool_call", "id": "c1", "name": name, "args": {"a": 1, "b": 2}}
        )
        ok = await tools["Add"].ainvoke(
            {"type": "tool_call", "id": "c2", "name": "Add", "args": {"a": 1, "b": 2}}
        )
    assert message.status == "error"
    assert name in str(message.content) and said in str(message.content)
    assert ok.status == "success" and "3" in str(ok.content)


async def test_other_mcp_errors_still_raise(support_doc) -> None:
    async with _ruled_server() as url:
        sources = ToolSources(parse_config(_doc(support_doc, url)))
        await sources.load()
        (broken,) = [t for t in sources.tools_by_ref["tools-calculator"] if t.name == "Broken"]
        with pytest.raises(MCPError):
            await broken.ainvoke(
                {"type": "tool_call", "id": "c1", "name": "Broken", "args": {"a": 1, "b": 2}}
            )


async def test_the_turn_goes_on_after_a_refused_call(support_doc) -> None:
    """모델이 거절된 Tool 을 불러도 턴은 실패하지 않고 그 결과를 보고 끝난다."""
    async with _ruled_server() as url, run_app(_doc(support_doc, url)) as running:
        response, sse = await chat(running, '불러 줘 {"tool": "Guarded", "args": {"a": 1, "b": 2}}')
    assert response.status_code == 200
    names = event_names(sse)
    assert "tool_result" in names and names[-1] == "done"
    (result,) = [data for event, data in sse if event == "tool_result"]
    assert "도구 소스가 거절했다" in json.dumps(result, ensure_ascii=False)
    # 거절의 문장은 무엇이 거절했는지 말하지 않는다(규칙인지 토큰인지 알 수 없다)
    assert "규칙" not in json.dumps(result, ensure_ascii=False)
    # 트레이서의 도구 스팬이 거절을 종류로 싣는다
    (span,) = running.observed("tool_exec")
    assert (span["status"], span["error_type"]) == ("error", "refused")


def _dropping(inner: Any, drops: list[int]) -> Any:
    """세션이 있는 `tools/call` 을 `drops[0]` 번 404 로 답하는 앞단이다. 백엔드에 붙기 전의 vMCP 가
    방금 연 세션을 끝낸 뒤 답하는 모양이다. 본문이 JSON-RPC 오류가 아니라서 클라이언트가
    `Session terminated` 로 올린다."""

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
            # 그 vMCP 는 2026-07-28 의 첫 요청에 실패해, 클라이언트가 세션을 여는 initialize 로
            # 내려간다
            error = {"code": -32601, "message": "Method not found"}
            payload = json.dumps({"jsonrpc": "2.0", "id": data.get("id"), "error": error})
            headers = [(b"content-type", b"application/json")]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": payload.encode()})
            return
        if method == "tools/call" and drops[0] > 0:
            drops[0] -= 1
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


def _calls(seconds: float = 0) -> tuple[FastMCP, list[int]]:
    """부른 횟수를 세는 Add 하나짜리 서버다. Add 는 `seconds` 만큼 걸린다."""
    server, ran = FastMCP("counted"), [0]

    async def add(a: int, b: int) -> int:
        ran[0] += 1
        await asyncio.sleep(seconds)
        return a + b

    server.tool(name="Add")(add)
    return server, ran


async def test_the_client_raises_the_message_of_an_ended_session() -> None:
    """세션이 있는 호출의 404 가 fastmcp 클라이언트에서 그 메시지의 `MCPError` 가 되는지 본다.
    런타임은 그 메시지로 다시 보낼 호출을 가른다."""
    server, _ran = _calls()
    async with serve(_dropping(server.http_app(), [1])) as base_url:
        async with Client(f"{base_url}/mcp") as client:
            with pytest.raises(MCPError) as ei:
                await client.call_tool("Add", {"a": 1, "b": 2})
    assert ei.value.message == SESSION_GONE_MESSAGE


async def test_a_call_whose_session_ended_is_sent_again(support_doc) -> None:
    """도구 소스가 세션을 끝낸 호출은 새 세션으로 다시 나가고, Tool 은 한 번만 실행된다."""
    server, ran = _calls()
    drops = [2]
    async with serve(_dropping(server.http_app(), drops)) as base_url:
        support_doc["tools"][0]["url"] = f"{base_url}/mcp"
        support_doc["tools"][0]["allow"] = ["Add"]
        sources = ToolSources(parse_config(support_doc), session_retry_pause=0.01)
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        message = await add.ainvoke(
            {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 1, "b": 2}}
        )
    assert message.status == "success" and "3" in str(message.content)
    assert (drops[0], ran[0]) == (0, 1)


async def test_a_source_that_keeps_ending_sessions_becomes_a_tool_error(support_doc) -> None:
    """제한 시간까지 세션이 계속 끝나면 턴을 끝내지 않고 모델이 보는 Tool 오류가 된다."""
    server, ran = _calls()
    async with serve(_dropping(server.http_app(), [10**6])) as base_url:
        support_doc["tools"][0]["url"] = f"{base_url}/mcp"
        support_doc["tools"][0]["allow"] = ["Add"]
        # The pause outlasts the limit, so the limit always falls inside a pause, never inside an
        # attempt that a loaded machine answers slowly.
        sources = ToolSources(
            parse_config(support_doc), default_timeout=2.0, session_retry_pause=30
        )
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        message = await add.ainvoke(
            {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 1, "b": 2}}
        )
    assert message.status == "error"
    assert "호출을 받지 않았다" in str(message.content)
    assert ran[0] == 0


async def test_a_timeout_during_an_accepted_attempt_does_not_say_the_call_was_not_taken(
    support_doc,
) -> None:
    """세션이 한 번 끝난 뒤 받아들여진 호출이 제한 시간을 넘기면, 호출을 받지 않았다고 말하지
    않는다. Tool 이 실행 중이라 모델이 그 말을 믿고 다시 부르면 두 번 실행된다."""
    server, ran = _calls(seconds=2)
    async with serve(_dropping(server.http_app(), [1])) as base_url:
        support_doc["tools"][0]["url"] = f"{base_url}/mcp"
        support_doc["tools"][0]["allow"] = ["Add"]
        sources = ToolSources(
            parse_config(support_doc), default_timeout=0.5, session_retry_pause=0.01
        )
        await sources.load()
        (add,) = sources.tools_by_ref["tools-calculator"]
        message = await add.ainvoke(
            {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 1, "b": 2}}
        )
    assert message.status == "error"
    assert "안에 답하지 않았다" in str(message.content)
    assert "호출을 받지 않았다" not in str(message.content)
    assert ran[0] == 1
