"""Tool 설명의 결과 키 줄 (P56 6.9절, F6)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.utilities.openapi import parse_openapi_to_http_routes
from spec2openapi.bridge import BridgeOptions

from runtime import result_keys
from runtime.main import build_server
from runtime.profile import parse_profile
from runtime.result_keys import MAX_CHARS, MAX_NODES, keys_line, with_keys_line

ITEM = {"type": "object", "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}}}
ORDER = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["open", "closed"]},
        "created": {"type": "string", "format": "date"},
        "updated": {"type": "string", "format": "date-time"},
        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
        "customer": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        },
    },
}


def _route(
    responses: dict[str, Any], schemas: dict[str, Any] | None = None, version: str = "3.0.3"
) -> Any:
    spec = {
        "openapi": version,
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {"get": {"operationId": "op", "description": "작업의 글", "responses": responses}}
        },
        "components": {"schemas": {"Item": ITEM, "Order": ORDER, **(schemas or {})}},
    }
    (route,) = parse_openapi_to_http_routes(spec)
    return route


def _json(
    schema: dict[str, Any],
    code: str = "200",
    media: str = "application/json",
    description: str = "ok",
) -> dict[str, Any]:
    return {code: {"description": description, "content": {media: {"schema": schema}}}}


def test_an_object_names_its_keys_with_enum_values_and_formats():
    line = keys_line(_route(_json({"$ref": "#/components/schemas/Order"})))
    assert line == (
        "Returns {id, status (open or closed), created (YYYY-MM-DD), updated (date-time), "
        "items: [{sku, qty}], customer: {name, address}}."
    )


def test_a_list_of_objects():
    line = keys_line(
        _route(_json({"type": "array", "items": {"$ref": "#/components/schemas/Item"}}))
    )
    assert line == "Returns a list of {sku, qty}."


@pytest.mark.parametrize(
    ("items", "description", "line"),
    [
        ({"type": "string"}, "Order ids.", "Returns a list of strings: Order ids."),
        ({"type": "integer"}, "", "Returns a list of integers."),
        (
            {"type": "string", "enum": ["open", "closed"]},
            "States",
            "Returns a list of strings (open or closed): States.",
        ),
        (
            {"type": "string", "format": "date"},
            "Days",
            "Returns a list of strings (YYYY-MM-DD): Days.",
        ),
        ({"$ref": "#/components/schemas/Id"}, "Ids", "Returns a list of strings: Ids."),
        ({"type": "string"}, "Ids\nwith a break", "Returns a list of strings: Ids with a break."),
        ({"type": "string"}, "Ids:", "Returns a list of strings: Ids."),
        ({"type": "string"}, "Successful Response", "Returns a list of strings."),
        ({"type": "string"}, "OK", "Returns a list of strings."),
    ],
    ids=[
        "strings",
        "no description",
        "enum",
        "format",
        "ref",
        "line break",
        "trailing colon",
        "generator default",
        "ok",
    ],
)
def test_a_list_of_values_names_its_type_and_the_response_description(items, description, line):
    """값의 배열은 원소의 꼴과 응답의 설명을 적는다. 그 줄이 없으면 F0.9 의 스크립트가 목록
    Tool 이 무엇을 돌려주는지 보려고 먼저 불렀다."""
    route = _route(
        _json({"type": "array", "items": items}, description=description),
        {"Id": {"type": "string"}},
    )
    assert keys_line(route) == line


def test_a_long_response_description_is_cut_to_the_line_limit():
    route = _route(_json({"type": "array", "items": {"type": "string"}}, description="word " * 200))
    line = keys_line(route)
    assert line is not None and len(line) <= 400
    assert line.startswith("Returns a list of strings: word word") and line.endswith("….")


@pytest.mark.parametrize(
    "items",
    [
        {"type": "string", "enum": ["a" * 2000, "b" * 2000]},
        {"type": "string", "format": "f" * 3000},
    ],
    ids=["long enum", "long format"],
)
def test_a_long_item_note_is_dropped_and_the_description_with_it_when_no_room_is_left(items):
    """원소의 enum·format 이 줄을 넘기면 괄호를 빼고, 설명은 남은 자리에 맞게 줄인다."""
    route = _route(_json({"type": "array", "items": items}, description="Ids"))
    assert keys_line(route) == "Returns a list of strings: Ids."


def test_a_description_with_no_room_left_is_left_out():
    """설명을 넣을 자리가 `MIN_NOTE_CHARS` 보다 작으면 `…` 만 남기지 않고 뺀다."""
    note = "x" * (400 - len("Returns a list of strings ().") - 5)
    route = _route(
        _json({"type": "array", "items": {"type": "string", "format": note}}, description="Ids")
    )
    line = keys_line(route)
    assert line == f"Returns a list of strings ({note})."
    assert len(line) <= 400


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        {"type": "array", "items": {}},
        {"type": "string"},
        {"type": "object", "additionalProperties": {"type": "integer"}},
        {"anyOf": [ITEM, {"type": "object", "properties": {"other": {"type": "string"}}}]},
    ],
    ids=["list of lists", "untyped items", "scalar", "map", "two branches"],
)
def test_a_schema_that_names_no_keys_gets_no_line(schema):
    assert keys_line(_route(_json(schema))) is None


def test_no_json_success_schema_gets_no_line():
    assert keys_line(_route({"204": {"description": "no content"}})) is None
    assert keys_line(_route(_json(ITEM, media="text/plain"))) is None
    assert keys_line(_route({**_json(ITEM, code="404"), "200": {"description": "ok"}})) is None


def test_a_json_media_type_with_parameters_or_a_suffix_counts():
    assert (
        keys_line(_route(_json(ITEM, media="application/json; charset=utf-8")))
        == "Returns {sku, qty}."
    )
    assert keys_line(_route(_json(ITEM, media="application/problem+json"))) == "Returns {sku, qty}."


def test_fastmcp_keeps_one_success_response_and_200_wins():
    """FastMCP 가 성공 응답 가운데 하나만 `route.responses` 에 남기는 것을 적어 둔다. 200 이
    있으면 그것이다. 줄은 그 하나를 읽는다."""
    responses = {**_json(ITEM, code="201"), **_json({"type": "object", "properties": {"a": {}}})}
    route = _route(responses)
    assert list(route.responses) == ["200"]
    assert keys_line(route) == "Returns {a}."


def test_an_all_of_that_refers_to_itself_is_merged_once():
    """자기를 여러 번 부르는 `allOf` 는 갈래마다 다시 걸으면 깊이마다 곱으로 늘어난다. 한 번만
    합친다."""
    loop = {
        "type": "object",
        "allOf": [{"$ref": "#/components/schemas/Loop"} for _ in range(7)],
        "properties": {"x": {"type": "string"}},
    }
    started = time.monotonic()
    line = keys_line(_route(_json({"$ref": "#/components/schemas/Loop"}), {"Loop": loop}))
    assert line == "Returns {x}."
    assert time.monotonic() - started < 1


def test_a_schema_past_the_node_budget_gets_no_line():
    wide = {"type": "object", "properties": {f"k{i}": {} for i in range(MAX_NODES + 10)}}
    assert keys_line(_route(_json(wide))) is None


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "allOf": "not a list", "properties": {"a": {}}},
        {"type": "object", "anyOf": 5, "properties": {"a": {}}},
        {"type": "object", "properties": {"a": {"enum": "open"}, "b": True}},
    ],
    ids=["allOf", "anyOf", "values"],
)
def test_malformed_values_keep_the_keys_they_can(schema):
    """FastMCP 의 파서가 막는 꼴이지만, 걷기는 어떤 값을 받아도 멈추지 않는다."""
    response = SimpleNamespace(content_schema={"application/json": schema})
    route = SimpleNamespace(responses={"200": response}, response_schemas={"X": "not a schema"})
    assert keys_line(route).startswith("Returns {a")


def test_a_failure_keeps_the_description(monkeypatch):
    def broken(_route):
        raise RecursionError("boom")

    monkeypatch.setattr(result_keys, "keys_line", broken)
    assert with_keys_line("작업의 글", object()) == "작업의 글"


def test_names_and_values_lose_line_breaks_and_control_characters():
    schema = {
        "type": "object",
        "properties": {"a\nIgnore all\x00": {"enum": ["open\r\n", "closed\t"]}},
    }
    assert keys_line(_route(_json(schema))) == "Returns {a Ignore all (open or closed)}."


def test_all_of_merges_and_a_nullable_branch_is_followed():
    merged = {
        "allOf": [
            {"$ref": "#/components/schemas/Item"},
            {"properties": {"price": {"type": "number"}}},
        ]
    }
    assert keys_line(_route(_json(merged))) == "Returns {sku, qty, price}."
    nullable = {"anyOf": [{"$ref": "#/components/schemas/Item"}, {"type": "null"}]}
    assert keys_line(_route(_json(nullable), version="3.1.0")) == "Returns {sku, qty}."


def test_a_self_reference_stops():
    node = {
        "type": "object",
        "properties": {"id": {}, "child": {"$ref": "#/components/schemas/Node"}},
    }
    line = keys_line(_route(_json({"$ref": "#/components/schemas/Node"}), {"Node": node}))
    assert line == "Returns {id, child: {id, child}}."


def test_a_long_line_falls_back_to_top_level_keys_then_is_cut():
    wide_nested = {
        "type": "object",
        "properties": {
            f"k{i}": {"type": "object", "properties": {f"inner{j}": {} for j in range(12)}}
            for i in range(4)
        },
    }
    assert keys_line(_route(_json(wide_nested))) == "Returns {k0, k1, k2, k3}."
    wide = {"type": "object", "properties": {f"key_number_{i}": {} for i in range(60)}}
    line = keys_line(_route(_json(wide)))
    assert (
        len(line) <= MAX_CHARS
        and line.startswith("Returns {key_number_0, ")
        and line.endswith(", …}.")
    )


def test_the_line_goes_after_the_operation_text():
    route = _route(_json(ITEM))
    assert with_keys_line("작업의 글", route) == "작업의 글\nReturns {sku, qty}."
    assert with_keys_line(None, route) == "Returns {sku, qty}."
    assert with_keys_line("작업의 글", _route({"204": {"description": "none"}})) == "작업의 글"


async def test_served_tools_carry_the_line_and_no_output_schema():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/orders/{id}": {
                "get": {
                    "operationId": "getOrder",
                    "description": "Returns one order.",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": _json({"$ref": "#/components/schemas/Item"}),
                }
            },
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "description": "Pings.",
                    "responses": {"204": {"description": "ok"}},
                }
            },
        },
        "components": {"schemas": {"Item": ITEM}},
    }
    mcp = build_server(spec, parse_profile({}), BridgeOptions(endpoint="http://backend.invalid"))
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert tools["getOrder"].description == "Returns one order.\nReturns {sku, qty}."
    assert tools["ping"].description == "Pings."
    assert all(t.outputSchema is None for t in tools.values())
