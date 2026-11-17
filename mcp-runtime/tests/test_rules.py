"""Tool 규칙 (P52 4절): 인자 조건, readOnlyHint, 엄격한 profile 모델, initialize 의 보고.

REST 백엔드는 받은 요청을 적어 두는 HTTP 서버이고, 거절한 호출이 백엔드에 닿지 않았는지를 그
기록으로 본다. SOAP 백엔드는 spec2openapi 의 시험 서버다. 클라이언트는 인메모리 FastMCP Client 다.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from spec2openapi.bridge import BridgeOptions

from runtime.conditions import ARG_CONDITIONS, Place, _sent, compile_guard
from runtime.main import PROTOCOL_HEADERS, build_composite_server, build_server, http_middleware
from runtime.profile import ArgCondition, ProfileError, parse_profile
from tests.conftest import load_fixture, serve

SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "orders", "version": "1.0.0"},
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "status", "in": "query", "schema": {"type": "string"}},
                    {"name": "tags", "in": "query", "schema": {"type": "array", "items": {}}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "createOrder",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "amount": {"type": "number"},
                                    "currency": {"type": "string"},
                                    "note": {"type": "string"},
                                    "express": {"type": "boolean"},
                                    "customer": {
                                        "type": "object",
                                        "properties": {
                                            "tier": {"type": "string"},
                                            "id": {"type": "integer"},
                                        },
                                    },
                                    "items": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {"sku": {"type": "string"}},
                                        },
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/orders/{orderId}": {
            "head": {
                "operationId": "orderExists",
                "parameters": [
                    {
                        "name": "orderId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {
                "operationId": "deleteOrder",
                "parameters": [
                    {
                        "name": "orderId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            },
        },
    },
}


class _Recorder(BaseHTTPRequestHandler):
    """받은 요청을 `seen` 에, 그 머리를 `headers` 에 적고 JSON 으로 답하는 REST 백엔드다."""

    seen: ClassVar[list[tuple[str, str, str]]] = []
    headers_seen: ClassVar[list[dict[str, str]]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append((self.command, self.path, body))
        type(self).headers_seen.append({k.lower(): v for k, v in self.headers.items()})
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_HEAD = _answer  # noqa: N815

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def backend() -> Iterator[str]:
    _Recorder.seen = []
    _Recorder.headers_seen = []
    with serve(_Recorder) as base_url:
        yield base_url


def _server(backend: str, rules: dict[str, Any], **profile: Any):
    return build_server(
        SPEC, parse_profile({"rules": rules, **profile}), BridgeOptions(endpoint=backend)
    )


def _cond(path: str, op: str, value: Any = None, **extra: Any) -> dict[str, Any]:
    return {"path": path, "op": op, "value": value, **extra}


async def _call(mcp, tool: str, args: dict[str, Any]) -> Any:
    async with Client(mcp) as c:
        return await c.call_tool(tool, args)


async def _refused(mcp, tool: str, args: dict[str, Any]) -> str:
    async with Client(mcp) as c:
        with pytest.raises(ToolError) as ei:
            await c.call_tool(tool, args)
    return str(ei.value)


# -- 연산자 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cond", "good", "bad", "said"),
    [
        (_cond("currency", "equals", "KRW"), "KRW", "USD", "must equal 'KRW'"),
        (_cond("currency", "one_of", ["KRW", "JPY"]), "JPY", "USD", "must be one of 'KRW', 'JPY'"),
        (_cond("amount", "min", 10), 10, 9.5, "must be at least 10"),
        (_cond("amount", "max", 100), 100, 100.01, "must be at most 100"),
        (_cond("note", "max_length", 3), "abc", "abcd", "must be at most 3 characters long"),
        (_cond("note", "prefix", "ord-"), "ord-1", "x-ord-1", "must start with 'ord-'"),
        (_cond("express", "equals", False), False, True, "must equal False"),
    ],
    ids=["equals", "one_of", "min", "max", "max_length", "prefix", "equals-boolean"],
)
async def test_each_operator_admits_and_refuses(backend, cond, good, bad, said):
    """조건을 지키는 값은 백엔드까지 가고, 어긋나는 값은 그 인자를 이름 댄 Tool 오류이며 백엔드에
    닿지 않는다."""
    mcp = _server(backend, {"createOrder": {"conditions": [cond]}})
    await _call(mcp, "createOrder", {cond["path"]: good})
    assert len(_Recorder.seen) == 1
    message = await _refused(mcp, "createOrder", {cond["path"]: bad})
    assert f"argument {cond['path']!r} {said}" in message
    assert len(_Recorder.seen) == 1


async def test_nested_path_reads_the_object_argument(backend):
    mcp = _server(
        backend, {"createOrder": {"conditions": [_cond("customer.tier", "equals", "gold")]}}
    )
    await _call(mcp, "createOrder", {"customer": {"tier": "gold", "id": 7}})
    assert json.loads(_Recorder.seen[0][2])["customer"] == {"tier": "gold", "id": 7}
    assert "'customer.tier' must equal 'gold'" in await _refused(
        mcp, "createOrder", {"customer": {"tier": "silver"}}
    )
    assert "'customer.tier' is required" in await _refused(mcp, "createOrder", {"customer": {}})
    assert "'customer' is not an object" in await _refused(mcp, "createOrder", {"customer": "gold"})
    assert len(_Recorder.seen) == 1


async def test_query_parameters_are_checked_too(backend):
    mcp = _server(backend, {"listOrders": {"conditions": [_cond("limit", "max", 50)]}})
    await _call(mcp, "listOrders", {"limit": 50})
    assert "'limit' must be at most 50" in await _refused(mcp, "listOrders", {"limit": 51})
    assert [s[1] for s in _Recorder.seen] == ["/orders?limit=50"]


async def test_absent_argument_fails_unless_optional(backend):
    required = _server(backend, {"createOrder": {"conditions": [_cond("amount", "max", 100)]}})
    assert "'amount' is required" in await _refused(required, "createOrder", {"currency": "KRW"})
    assert "'amount' is required" in await _refused(required, "createOrder", {"amount": None})
    optional = _server(
        backend, {"createOrder": {"conditions": [_cond("amount", "max", 100, optional=True)]}}
    )
    await _call(optional, "createOrder", {"currency": "KRW"})
    assert "'amount' must be at most 100" in await _refused(
        optional, "createOrder", {"amount": 101}
    )
    assert len(_Recorder.seen) == 1


@pytest.mark.parametrize(
    ("cond", "value", "kind"),
    [
        (_cond("amount", "max", 100), "50", "number"),
        (_cond("amount", "max", 100), True, "number"),
        (_cond("customer.id", "min", 1), 1.5, "integer"),
        (_cond("note", "prefix", "a"), 1, "string"),
        (_cond("express", "equals", True), 1, "boolean"),
    ],
)
async def test_a_value_of_the_wrong_type_fails(backend, cond, value, kind):
    mcp = _server(backend, {"createOrder": {"conditions": [cond]}})
    args = {"customer": {"id": value}} if cond["path"] == "customer.id" else {cond["path"]: value}
    assert f"{cond['path']!r} must be a {kind}" in await _refused(mcp, "createOrder", args)
    assert _Recorder.seen == []


# -- 풀리지 않는 조건 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond",
    [
        _cond("missing", "equals", "x"),
        _cond("items.sku", "equals", "x"),  # 배열을 지난다
        _cond("items", "max_length", 3),  # 배열 자체다
        _cond("customer", "equals", "x"),  # 객체에는 연산자가 없다
        _cond("note", "min", 1),  # 글에 min 은 없다
        _cond("amount", "max", "100"),  # 값의 형식이 틀렸다
        _cond("currency", "one_of", []),
    ],
    ids=["missing", "through-array", "array", "object", "op-kind", "value-type", "empty-one-of"],
)
async def test_an_unresolved_condition_closes_the_tool_and_is_reported(backend, cond):
    mcp = _server(
        backend,
        {
            "createOrder": {"conditions": [cond]},
            "listOrders": {"conditions": [_cond("limit", "max", 5)]},
        },
    )
    message = await _refused(mcp, "createOrder", {"amount": 1, "currency": "KRW"})
    assert "closed by its Tool rules" in message and cond["path"] in message
    async with Client(mcp, mode="legacy") as c:
        experimental = c.initialize_result.capabilities.experimental
    assert experimental[ARG_CONDITIONS] == {
        "version": 1,
        "unresolved": {"createOrder": [cond["path"]]},
    }
    # 같은 서버의 다른 Tool 은 그대로 조건을 지킨다
    await _call(mcp, "listOrders", {"limit": 5})
    assert [s[0] for s in _Recorder.seen] == ["GET"]


def test_a_dotted_argument_name_that_collides_is_unresolved():
    """이름에 점이 든 인자와 객체의 속성이 같은 점 경로가 되면 어느 것인지 몰라 닫는다."""
    schema = {
        "type": "object",
        "properties": {
            "a.b": {"type": "string"},
            "a": {"type": "object", "properties": {"b": {"type": "string"}}},
        },
    }
    guard = compile_guard([ArgCondition(path="a.b", op="equals", value="x")], schema)
    assert guard.unresolved == ("a.b",) and guard.checks == ()


def _boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("boom")


async def test_a_guard_that_fails_to_compile_closes_the_tool(monkeypatch, backend):
    """FastMCP 는 컴포넌트 콜백의 예외를 경고로만 남기고 Tool 을 그대로 둔다. 조건을 풀다 실패하면
    닫힌 지킴이를 건다."""
    monkeypatch.setattr("runtime.main.compile_guard", _boom)
    mcp = _server(backend, {"createOrder": {"conditions": [_cond("amount", "max", 1)]}})
    assert "closed by its Tool rules" in await _refused(mcp, "createOrder", {"amount": 1})
    assert _Recorder.seen == []


def test_a_conditioned_tool_left_without_a_guard_refuses_to_start(monkeypatch, backend):
    """지킴이를 걸지 못한 채 Tool 이 된 operationId 가 있으면 서버를 띄우지 않는다."""
    monkeypatch.setattr("runtime.main.compile_guard", _boom)
    monkeypatch.setattr("runtime.main.closed_guard", _boom)
    with pytest.raises(ProfileError) as ei:
        _server(backend, {"createOrder": {"conditions": [_cond("amount", "max", 1)]}})
    assert ei.value.code == "conditions_not_applied" and "createOrder" in ei.value.message


# -- readOnlyHint ---------------------------------------------------------------------------


async def test_read_only_hint_follows_the_mark_then_the_method(backend):
    mcp = _server(
        backend,
        {"createOrder": {"read_only": True}, "listOrders": {"read_only": False}},
    )
    async with Client(mcp) as c:
        hints = {t.name: t.annotations.readOnlyHint for t in await c.list_tools()}
    assert hints == {
        "createOrder": True,
        "listOrders": False,
        "orderExists": True,  # HEAD
        "deleteOrder": False,
    }


async def test_soap_operations_default_to_write_and_keep_conditions(calculator_spec, mock_soap):
    profile = parse_profile(
        {
            "soap": True,
            "rules": {"Add": {"conditions": [{"path": "a", "op": "max", "value": 10}]}},
        }
    )
    mcp = build_server(calculator_spec, profile, BridgeOptions(endpoint=f"{mock_soap}/calc"))
    async with Client(mcp) as c:
        hints = {t.name: t.annotations.readOnlyHint for t in await c.list_tools()}
        assert (await c.call_tool("Add", {"a": 3, "b": 4})).structured_content == {"result": 7}
        with pytest.raises(ToolError) as ei:
            await c.call_tool("Add", {"a": 11, "b": 4})
    assert hints == {"Add": False, "Divide": False}
    assert "'a' must be at most 10" in str(ei.value)


# -- 여러 backend -----------------------------------------------------------------------------


async def test_composite_server_checks_each_backend_and_reports_both(backend, mock_soap):
    profile = parse_profile(
        {
            "backends": [
                {
                    "spec": "openapi-0.yaml",
                    "rules": {"createOrder": {"conditions": [_cond("currency", "equals", "KRW")]}},
                },
                {
                    "spec": "openapi-1.yaml",
                    "soap": True,
                    "rules": {"Divide": {"conditions": [_cond("nope", "equals", 1)]}},
                },
            ]
        }
    )
    mcp = build_composite_server(
        [SPEC, load_fixture("calculator.openapi.yaml")],
        profile,
        [BridgeOptions(endpoint=backend), BridgeOptions(endpoint=f"{mock_soap}/calc")],
    )
    async with Client(mcp, mode="legacy") as c:
        experimental = c.initialize_result.capabilities.experimental
        await c.call_tool("createOrder", {"currency": "KRW"})
        with pytest.raises(ToolError) as ei:
            await c.call_tool("createOrder", {"currency": "USD"})
        with pytest.raises(ToolError) as closed:
            await c.call_tool("Divide", {"numerator": 1, "denominator": 2})
        assert (await c.call_tool("Add", {"a": 1, "b": 2})).structured_content == {"result": 3}
    assert "'currency' must equal 'KRW'" in str(ei.value)
    assert "closed by its Tool rules" in str(closed.value)
    assert experimental[ARG_CONDITIONS] == {"version": 1, "unresolved": {"Divide": ["nope"]}}
    assert len(_Recorder.seen) == 1


# -- initialize 의 보고와 두 번째 길 --------------------------------------------------------------


async def test_the_image_reports_arg_conditions_without_rules(backend):
    """조건이 없어도 이 이미지가 조건을 지킨다는 것을 알린다. 새 규약(discover)의 능력에도
    실린다."""
    mcp = _server(backend, {})
    async with Client(mcp, mode="legacy") as c:
        legacy = c.initialize_result.capabilities.experimental
    async with Client(mcp) as c:
        modern = c.server_capabilities.experimental
    assert legacy[ARG_CONDITIONS] == modern[ARG_CONDITIONS] == {"version": 1, "unresolved": {}}


async def test_no_resource_or_template_gives_a_ruled_operation_a_second_route(backend):
    mcp = _server(backend, {"listOrders": {"conditions": [_cond("limit", "max", 5)]}})
    async with Client(mcp) as c:
        assert await c.list_resources() == []
        assert await c.list_resource_templates() == []
        names = sorted(t.name for t in await c.list_tools())
    assert names == ["createOrder", "deleteOrder", "listOrders", "orderExists"]


async def test_a_conditioned_tool_left_out_of_the_selection_needs_no_guard(backend):
    mcp = _server(
        backend,
        {"createOrder": {"conditions": [_cond("amount", "max", 1)]}},
        tools=["listOrders"],
    )
    async with Client(mcp) as c:
        assert [t.name for t in await c.list_tools()] == ["listOrders"]


# -- 엄격한 모델 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"limits": {}},
        {"tool_overrides": {"Add": {"name": "add", "title": "x"}}},
        {"rules": {"Add": {"read_only": True, "callers": {}}}},
        {"rules": {"Add": {"conditions": [{"path": "a", "op": "regex", "value": "."}]}}},
        {"rules": {"Add": {"conditions": [{"path": "a", "op": "max", "value": 1, "x": 1}]}}},
        {"rules": {"Add": {"conditions": [{"path": "", "op": "max", "value": 1}]}}},
        {"backends": [{"spec": "openapi-0.yaml", "limits": {}}]},
    ],
    ids=["top", "override", "rule", "operator", "condition", "empty-path", "backend"],
)
def test_profile_models_refuse_unknown_fields(data):
    with pytest.raises(ProfileError) as ei:
        parse_profile(data)
    assert ei.value.code == "invalid_profile"


def test_rules_parse_at_the_top_and_per_backend():
    flat = parse_profile({"rules": {"Add": {"read_only": True}}})
    assert flat.rules["Add"].read_only is True and flat.rules["Add"].conditions == []
    many = parse_profile(
        {"backends": [{"spec": "s.yaml", "rules": {"Add": {"conditions": [_cond("a", "max", 1)]}}}]}
    )
    assert many.backends[0].rules["Add"].conditions[0].op == "max"


# -- 백엔드로 나가는 요청 ---------------------------------------------------------------------
# FastMCP 가 짓는 요청은 arguments 와 다를 수 있다. 조건은 나가는 요청의 그 자리에서 다시 판정한다


def _spec_with(path: str, method: str, operation: dict[str, Any]) -> dict[str, Any]:
    op = {"responses": {"200": {"description": "ok"}}, **operation}
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {path: {method: op}},
    }


def _guarded(backend: str, spec: dict[str, Any], op: str, *conds: dict[str, Any]):
    profile = parse_profile({"rules": {op: {"conditions": list(conds)}}})
    return build_server(spec, profile, BridgeOptions(endpoint=backend))


def _query_spec(**filter_param: Any) -> dict[str, Any]:
    param = {"type": "object", "properties": {"status": {"type": "string"}}}
    return _spec_with(
        "/orders",
        "get",
        {
            "operationId": "listOrders",
            "parameters": [
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                {"name": "filter", "in": "query", "schema": param, **filter_param},
            ],
        },
    )


async def test_an_exploded_query_object_cannot_overwrite_a_conditioned_parameter(backend):
    mcp = _guarded(backend, _query_spec(), "listOrders", _cond("limit", "max", 50))
    overwrite = {"limit": 10, "filter": {"status": "open", "limit": 100000}}
    message = await _refused(mcp, "listOrders", overwrite)
    assert "'limit' as sent to the backend must be at most 50" in message
    assert _Recorder.seen == []
    await _call(mcp, "listOrders", {"limit": 10, "filter": {"status": "open"}})
    assert [s[1] for s in _Recorder.seen] == ["/orders?limit=10&status=open"]


@pytest.mark.parametrize(
    ("extra", "sent"),
    [({}, "/orders?status=open"), ({"style": "deepObject"}, "/orders?filter%5Bstatus%5D=open")],
    ids=["form", "deep-object"],
)
async def test_a_query_object_property_is_checked_where_it_is_sent(backend, extra, sent):
    mcp = _guarded(
        backend, _query_spec(**extra), "listOrders", _cond("filter.status", "equals", "open")
    )
    await _call(mcp, "listOrders", {"filter": {"status": "open"}})
    assert "'filter.status' must equal 'open'" in await _refused(
        mcp, "listOrders", {"filter": {"status": "closed"}}
    )
    assert [s[1] for s in _Recorder.seen] == [sent]


async def test_an_unexploded_query_object_property_cannot_be_checked(backend):
    spec = _query_spec(explode=False)
    mcp = _guarded(backend, spec, "listOrders", _cond("filter.status", "equals", "open"))
    message = await _refused(mcp, "listOrders", {"filter": {"status": "open"}})
    assert "closed by its Tool rules" in message and "filter.status" in message


@pytest.mark.parametrize(
    "schema",
    [
        {
            "allOf": [
                {"type": "object", "properties": {"amount": {"type": "number"}}},
                {"type": "object", "properties": {"note": {"type": "string"}}},
            ]
        },
        {"properties": {"amount": {"type": "number"}, "note": {"type": "string"}}},
    ],
    ids=["all-of", "no-type"],
)
async def test_a_body_without_type_object_is_checked_as_sent(backend, schema):
    """`type: object` 를 적지 않은 본문도 인자가 하나면 그 인자를 든 객체로 나간다(FastMCP 4.1).
    다른 인자 안에 든 같은 이름은 본문의 맨 위로 올라오지 않으므로 조건에 걸리지 않고, 조건은 나간
    본문의 그 자리를 본다."""
    spec = _spec_with(
        "/pay",
        "post",
        {
            "operationId": "pay",
            "requestBody": {"content": {"application/json": {"schema": schema}}},
        },
    )
    mcp = _guarded(backend, spec, "pay", _cond("amount", "max", 100, optional=True))
    await _call(mcp, "pay", {"note": {"amount": 1000000}})
    assert json.loads(_Recorder.seen[-1][2]) == {"note": {"amount": 1000000}}
    await _call(mcp, "pay", {"amount": 5})
    assert json.loads(_Recorder.seen[-1][2]) == {"amount": 5}
    assert "'amount' must be at most 100" in await _refused(mcp, "pay", {"amount": 500})
    await _call(mcp, "pay", {"amount": 5, "note": "lunch"})
    assert json.loads(_Recorder.seen[-1][2]) == {"amount": 5, "note": "lunch"}
    assert len(_Recorder.seen) == 3


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b'"lunch"', "cannot be read because the request body is not an object"),
        (b"{not json", "cannot be read because the request body is not JSON"),
        (b'{"amount": 1000000}', None),
    ],
)
def test_the_sent_body_is_read_where_the_condition_stands(content, said):
    """나가는 본문이 객체가 아니거나 JSON 이 아니면 조건의 값을 읽지 못한다고 말한다. FastMCP 가
    본문을 어떻게 짓든 조건은 실제로 나간 본문에서 읽는다."""
    value, why = _sent(Place("json", ("amount",)), "number", SimpleNamespace(content=content))
    assert why == said
    if said is None:
        assert value == 1000000


def _form_spec() -> dict[str, Any]:
    form = {"type": "object", "properties": {"amount": {"type": "number"}}}
    return _spec_with(
        "/pay",
        "post",
        {
            "operationId": "pay",
            "requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": form}}},
        },
    )


async def test_a_form_body_field_is_checked_as_sent(backend):
    mcp = _guarded(backend, _form_spec(), "pay", _cond("amount", "max", 100))
    await _call(mcp, "pay", {"amount": 5})
    assert "'amount' must be at most 100" in await _refused(mcp, "pay", {"amount": 500})
    assert [s[2] for s in _Recorder.seen] == ["amount=5"]


async def test_a_multipart_body_field_cannot_be_checked(backend, petstore_spec):
    profile = parse_profile(
        {"rules": {"uploadPhoto": {"conditions": [_cond("caption", "max_length", 9)]}}}
    )
    mcp = build_server(petstore_spec, profile, BridgeOptions(endpoint=backend))
    async with Client(mcp, mode="legacy") as c:
        unresolved = c.initialize_result.capabilities.experimental[ARG_CONDITIONS]["unresolved"]
    assert unresolved == {"uploadPhoto": ["caption"]}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_that_is_not_finite_fails(value):
    schema = {"type": "object", "properties": {"amount": {"type": "number"}}}
    guard = compile_guard([ArgCondition(path="amount", op="max", value=100)], schema)
    assert guard.refusal({"amount": value}) == (
        "The Tool rules refused this call: argument 'amount' must be a number."
    )


async def test_nan_over_json_rpc_is_refused(backend):
    """JSON-RPC 본문의 NaN 글자(파이썬의 json 은 읽는다)도 형식이 틀린 값이다."""
    mcp = _server(backend, {"createOrder": {"conditions": [_cond("amount", "max", 100)]}})
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    call = (
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"createOrder","arguments":{"amount":NaN}}}'
    )
    with _serve_asgi(mcp.http_app(path="/mcp", middleware=http_middleware())) as url:
        async with httpx2.AsyncClient(timeout=10) as hc:
            first = await hc.post(url, headers=accept, json=init)
            headers = {**accept, "mcp-session-id": first.headers.get("mcp-session-id", "")}
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            await hc.post(url, headers=headers, json=note)
            answer = await hc.post(url, headers=headers, content=call)
    assert "'amount' must be a number" in answer.text
    assert _Recorder.seen == []


@pytest.mark.parametrize("bound", [float("nan"), float("inf")])
def test_a_bound_that_is_not_finite_is_unresolved(bound):
    schema = {"type": "object", "properties": {"amount": {"type": "number"}}}
    guard = compile_guard([ArgCondition(path="amount", op="max", value=bound)], schema)
    assert guard.unresolved == ("amount",)


async def test_a_call_that_skips_the_middleware_is_checked_by_the_request_hook(backend):
    """미들웨어를 거치지 않고 Tool 을 바로 돌려도 요청 훅이 조건이 있는 라우트의 요청을 본다."""
    mcp = _server(backend, {"createOrder": {"conditions": [_cond("amount", "max", 100)]}})
    tool = await mcp.get_tool("createOrder")
    with pytest.raises(ToolError) as ei:
        await tool.run({"amount": 1000})
    assert "as sent to the backend must be at most 100" in str(ei.value)
    await tool.run({"amount": 10})
    assert len(_Recorder.seen) == 1


# -- 클라이언트의 HTTP 머리 ----------------------------------------------------------------------


def _header_spec() -> dict[str, Any]:
    return _spec_with(
        "/pay",
        "get",
        {
            "operationId": "pay",
            "parameters": [
                {"name": "X-Account", "in": "header", "schema": {"type": "string"}},
                {"name": "amount", "in": "query", "schema": {"type": "number"}},
            ],
        },
    )


@contextmanager
def _serve_asgi(app: Any) -> Iterator[str]:
    """ASGI 앱을 uvicorn 으로 스레드에 띄운다. 클라이언트의 HTTP 머리가 실제로 오가게 하려는
    것이다."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(5)


HEADERS = {"X-Account": "acct-999", "X-HTTP-Method-Override": "DELETE"}
ACCOUNT = _cond("X-Account", "equals", "acct-1", optional=True)


# 호출자가 지어낸 규약 머리다. legacy 서버는 이것을 보지 않으므로 거르기만으로는 백엔드에 닿는다
FORGED = {"Mcp-Method": "smuggled", "Mcp-Name": "evil"}
MODES = ("legacy", *MODERN_PROTOCOL_VERSIONS)


async def _call_over_http(
    url: str, args: dict[str, Any], mode: str = "auto", extra: dict[str, str] | None = None
) -> Any:
    headers = {**HEADERS, **(extra or {})}
    async with Client(StreamableHttpTransport(url, headers=headers), mode=mode) as c:
        assert [t.name for t in await c.list_tools()] == ["pay"]
        return await c.call_tool("pay", args)


@pytest.mark.parametrize("mode", MODES)
async def test_client_headers_are_dropped_before_they_reach_the_backend(backend, mode):
    """legacy 와 Modern(vMCP 처럼 initialize 로 내려가지 않는다) 클라이언트 모두 머리를 거르는
    서버에서 Tool 을 나열하고 부른다. 거른 머리도, 남긴 MCP 규약 머리도 백엔드에 닿지 않는다."""
    mcp = _guarded(backend, _header_spec(), "pay", ACCOUNT, _cond("amount", "max", 100))
    with _serve_asgi(mcp.http_app(path="/mcp", middleware=http_middleware())) as url:
        await _call_over_http(url, {"amount": 5}, mode, FORGED if mode == "legacy" else None)
    (sent,) = _Recorder.headers_seen
    assert "x-account" not in sent and "x-http-method-override" not in sent
    assert not set(PROTOCOL_HEADERS) & set(sent)


async def test_an_argument_marked_as_a_header_is_called_by_a_modern_client(backend):
    """OpenAPI 의 `x-mcp-header` 는 Tool 스키마로 옮겨지지만 걷힌다. 남으면 Modern 서버가 거른
    `Mcp-Param-*` 머리를 요구해 호출을 거절한다."""
    spec = _spec_with(
        "/pay",
        "get",
        {
            "operationId": "pay",
            "parameters": [
                {
                    "name": "region",
                    "in": "query",
                    "schema": {"type": "string", "x-mcp-header": "Region"},
                },
            ],
        },
    )
    mcp = build_server(spec, parse_profile({}), BridgeOptions(endpoint=backend))
    with _serve_asgi(mcp.http_app(path="/mcp", middleware=http_middleware())) as url:
        async with Client(StreamableHttpTransport(url), mode=MODERN_PROTOCOL_VERSIONS[0]) as c:
            (tool,) = await c.list_tools()
            assert "x-mcp-header" not in json.dumps(tool.inputSchema)
            await c.call_tool("pay", {"region": "kr"})
    ((method, path, _),) = _Recorder.seen
    assert (method, path) == ("GET", "/pay?region=kr")


async def test_a_forwarded_client_header_is_checked_as_sent(backend):
    """머리를 버리지 않는 앱에서도 요청 훅이 백엔드로 옮겨진 머리를 조건으로 본다."""
    mcp = _guarded(backend, _header_spec(), "pay", ACCOUNT)
    with _serve_asgi(mcp.http_app(path="/mcp")) as url, pytest.raises(ToolError) as ei:
        await _call_over_http(url, {"amount": 5})
    assert "'X-Account' as sent to the backend must equal 'acct-1'" in str(ei.value)
    assert _Recorder.seen == []
