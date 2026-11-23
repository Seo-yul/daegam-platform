"""outputSchema 광고를 걷었는지 본다.

스펙이 응답 스키마를 선언해도 REST 백엔드가 JSON 을 준다는 보장이 없다. outputSchema 를 광고한 툴이
structuredContent 없이 답하면 mcp 클라이언트가 RuntimeError 를 내고 에이전트 턴이 통째로 죽는다.
그래서 응답 스키마를 선언한 operation 셋(백엔드가 JSON / text·plain / 204)을 REST 백엔드로 띄우고,
tools/list 에 outputSchema 가 없고 세 툴 모두 call_tool 이 예외 없이 돌아오는지 확인한다.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest
from fastmcp import Client
from spec2openapi.bridge import BridgeOptions

from runtime.main import build_server
from runtime.profile import ProfileConfig, parse_profile
from tests.conftest import serve

_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}


def _operation(operation_id: str) -> dict[str, Any]:
    return {
        "get": {
            "operationId": operation_id,
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": copy.deepcopy(_OBJECT_SCHEMA)}},
                }
            },
        }
    }


SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "echo", "version": "1.0.0"},
    "paths": {
        "/json": _operation("ok_json"),
        "/text": _operation("declares_json_returns_text"),
        "/empty": _operation("declares_json_returns_empty"),
    },
}


class _RestHandler(BaseHTTPRequestHandler):
    """세 경로가 각각 JSON, text/plain, 204 로 답하는 REST 백엔드다."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/json":
            body = b'{"a":"hello"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/text":
            body = b"plain text body"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        elif self.path == "/empty":
            self.send_response(204)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def rest_backend() -> Iterator[str]:
    with serve(_RestHandler) as base_url:
        yield base_url


def _server(rest_backend: str, profile: ProfileConfig | None = None):
    return build_server(SPEC, profile or ProfileConfig(), BridgeOptions(endpoint=rest_backend))


async def test_tools_list_advertises_no_output_schema(rest_backend):
    mcp = _server(rest_backend)
    async with Client(mcp) as c:
        tools = await c.list_tools()
    assert sorted(t.name for t in tools) == [
        "declares_json_returns_empty",
        "declares_json_returns_text",
        "ok_json",
    ]
    assert {t.name: t.outputSchema for t in tools} == {t.name: None for t in tools}


async def test_output_schema_stripped_even_with_descriptions(rest_backend):
    """descriptions 가 있어도(component_fn 이 원래 걸리던 경로) 함께 걷는다."""
    profile = parse_profile({"tool_overrides": {"ok_json": {"description": "JSON 을 돌려준다"}}})
    mcp = _server(rest_backend, profile)
    async with Client(mcp) as c:
        by_name = {t.name: t for t in await c.list_tools()}
    assert by_name["ok_json"].description.split("\n")[0] == "JSON 을 돌려준다"
    assert all(t.outputSchema is None for t in by_name.values())


async def test_call_tool_survives_non_json_2xx(rest_backend):
    mcp = _server(rest_backend)
    async with Client(mcp) as c:
        ok = await c.call_tool("ok_json", {})
        text = await c.call_tool("declares_json_returns_text", {})
        empty = await c.call_tool("declares_json_returns_empty", {})

    # JSON 응답은 structured_content 를 그대로 유지한다
    assert ok.structured_content == {"a": "hello"}
    assert [b.text for b in ok.content] == ['{"a":"hello"}']
    # 비-JSON 2xx 는 예외 없이 텍스트로 돌아온다
    assert text.structured_content is None
    assert [b.text for b in text.content] == ["plain text body"]
    assert empty.structured_content is None
    assert [b.text for b in empty.content] == [""]
    assert not any(r.is_error for r in (ok, text, empty))
