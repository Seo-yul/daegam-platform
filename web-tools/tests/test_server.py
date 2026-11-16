"""서버가 계약 파일의 스키마를 그대로 내고, 결과와 오류를 계약대로 돌려준다."""

from __future__ import annotations

import httpx
from fastmcp import Client

from tests.conftest import make_fetcher
from webtools.contract import tool_spec
from webtools.main import build_server


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>hi</p>")


async def test_the_served_schema_is_the_contract():
    mcp = build_server(make_fetcher(handler))
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["web_fetch"]
    assert tools[0].inputSchema == tool_spec("web_fetch")["inputSchema"]
    assert tools[0].description == tool_spec("web_fetch")["description"]


async def test_a_call_returns_structured_content_and_the_wrapped_text():
    mcp = build_server(make_fetcher(handler))
    async with Client(mcp) as client:
        result = await client.call_tool("web_fetch", {"url": "http://site.example.com/"})
        refused = await client.call_tool(
            "web_fetch", {"url": "http://10.0.0.1/"}, raise_on_error=False
        )
    assert result.structured_content["content"] == "hi"
    assert 'untrusted="true"' in result.content[0].text
    assert refused.is_error
    assert refused.structured_content["code"] == "url_private"
    assert "10.0.0.1" not in refused.structured_content["message"]


async def test_health_routes():
    app = build_server(make_fetcher(handler)).http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            for path in ("/", "/health"):
                assert (await c.get(path)).json() == {"status": "ok"}


async def test_an_unexpected_error_becomes_our_code():
    """예상하지 못한 예외의 글은 모델에게 가지 않는다. 우리 코드 fetch_failed 와 고정 문장이다."""
    fetcher = make_fetcher(handler)

    async def broken(args):
        raise RuntimeError("internal detail from a library")

    fetcher.fetch_sized = broken
    async with Client(build_server(fetcher)) as client:
        result = await client.call_tool(
            "web_fetch", {"url": "http://site.example.com/"}, raise_on_error=False
        )
    assert result.is_error and result.structured_content["code"] == "fetch_failed"
    assert "internal detail" not in result.content[0].text
