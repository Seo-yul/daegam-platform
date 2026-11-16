"""거절된 Tool 호출 (P52 5절, 능력 `tool_refusals`).

ToolHive 의 프록시와 vMCP 는 Cedar 가 거절한 `tools/call` 을 HTTP 403 과 메시지 `Unauthorized` 의
JSON-RPC 오류로, 한도에 걸린 호출을 429 와 `Rate limit exceeded` 로 답한다(F0 4번). 가짜 서버는 그
앞단을 흉내 내는 ASGI 감싸개이고, 이름을 정한 Tool 의 호출만 그렇게 답하고 나머지는 FastMCP 에
넘긴다. fastmcp 클라이언트는 둘 다 `MCPError` 로 올리고, 런타임은 그것을 모델이 보는 Tool 오류로
바꿔 턴을 이어 간다. 그 밖의 `MCPError` 는 예전처럼 올라간다.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp import MCPError

from runtime.config import parse_config
from runtime.mcp import LIMITED_MESSAGE, REFUSED_MESSAGE, ToolSources
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
