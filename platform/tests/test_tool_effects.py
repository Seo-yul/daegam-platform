"""Tool 효과 (P39b 14절 4부). MCP Server 는 Backend API 버전의 메서드로, Gateway 는 백엔드 종류로
정하고 게시 항목은 원본 Gateway 까지 따라 내려간다. 읽기 전용 표시는 write 만 read 로 바꾼다. 옛
버전의 메서드는 변환 스펙에서 한 번 채운다.
"""

from __future__ import annotations

from daegam.pipeline.convert import tool_details_of
from daegam.services.advertised_names import advertised_tools_of
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    WEB_FETCH,
    WEB_SEARCH,
    SourceTools,
    gateway_source,
    method_effect,
    node_effects,
    server_source,
)
from daegam.store.backend_apis import BackendAPIVersion, ToolDetail
from daegam.store.gateways import GatewayBackendSnapshot
from daegam.store.mcp_servers import MCPServerRevision
from tests.helpers.backend_apis import backend_version
from tests.helpers.docs import gateway_doc, mcp_server_doc
from tests.helpers.gateways import backend_snapshot

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "orders", "version": "1"},
    "paths": {
        "/orders": {
            "get": {"operationId": "listOrders", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createOrder", "responses": {"200": {"description": "ok"}}},
        },
        "/orders/{id}": {
            "head": {"operationId": "orderExists", "responses": {"200": {"description": "ok"}}},
        },
    },
}


def _version(methods: bool = True) -> BackendAPIVersion:
    details = [
        ToolDetail(operation_id=d["operation_id"], method=d["method"] if methods else None)
        for d in tool_details_of(SPEC)
    ]
    return backend_version([d.operation_id for d in details], details=details)


def _server(**selection):
    return mcp_server_doc(
        "mi-1",
        spec={"backends": [{"backend_profile_id": "bp", "spec_version": 1, **selection}]},
        state="deployed",
    )


def _gateway(*backends: GatewayBackendSnapshot, resolution: str = "priority"):
    return gateway_doc(
        "gw-1",
        state="deployed",
        deployed_backends=list(backends),
        deployed_conflict_resolution=resolution,
    )


def test_converted_operations_carry_their_method() -> None:
    methods = {d["operation_id"]: d["method"] for d in tool_details_of(SPEC)}
    assert methods == {"listOrders": "GET", "createOrder": "POST", "orderExists": "HEAD"}
    assert method_effect("get") == EFFECT_READ and method_effect(None) == EFFECT_WRITE


def test_server_tools_follow_the_method_through_renames() -> None:
    rev = _server(tool_overrides={"listOrders": {"name": "orders_list"}}).deployed
    tools = server_source(rev, {("bp", 1): _version()})
    assert tools.names == ("orders_list", "createOrder", "orderExists")
    assert dict(tools.effects) == {
        "orders_list": EFFECT_READ,
        "createOrder": EFFECT_WRITE,
        "orderExists": EFFECT_READ,
    }


def test_a_version_without_methods_writes_and_a_missing_version_has_no_names() -> None:
    rev = _server().deployed
    assert set(server_source(rev, {("bp", 1): _version(methods=False)}).effects.values()) == {
        EFFECT_WRITE
    }
    assert server_source(rev, {}).names is None
    filtered = _server(tools=["listOrders"]).deployed
    assert server_source(filtered, {}).names == ("listOrders",)


def test_gateway_backends_take_their_kinds_effect() -> None:
    server = server_source(_server().deployed, {("bp", 1): _version()})
    gw = _gateway(
        backend_snapshot("orders", "internal", ["listOrders", "createOrder"], server_id="mi-1"),
        backend_snapshot("web-fetch", "platform", ["web_fetch"]),
        backend_snapshot("web-search", "platform", ["web_search"]),
        backend_snapshot("calc", "remote", ["Add"]),
    )
    tools = gateway_source(gw, servers={"mi-1": server}, published={})
    assert tools.names == ("listOrders", "createOrder", "web_fetch", "web_search", "Add")
    assert dict(tools.effects) == {
        "listOrders": EFFECT_READ,
        "createOrder": EFFECT_WRITE,
        "web_fetch": EFFECT_WEB,
        "web_search": EFFECT_WEB,
        "Add": EFFECT_WRITE,
    }
    assert dict(tools.web) == {"web_fetch": WEB_FETCH, "web_search": WEB_SEARCH}


def test_each_name_carries_its_backend_kind() -> None:
    """P43 의 표시가 읽는 백엔드 종류다. 게시 항목은 원본의 것을 따르고, 원본을 모르면 없다."""
    server = server_source(_server().deployed, {("bp", 1): _version()})
    assert set(server.origins.values()) == {"internal"}
    gw = _gateway(
        backend_snapshot("orders", "internal", ["listOrders"], server_id="mi-1"),
        backend_snapshot("web-fetch", "platform", ["web_fetch"]),
        backend_snapshot("calc", "remote", ["Add"]),
        backend_snapshot("mail", "image", ["send_mail"]),
        backend_snapshot("pub", "published", ["Mul"], tools=["Mul"]),
        backend_snapshot("lost", "published", ["Sub"], tools=["Sub"], hub_item_id="hi-lost"),
    )
    below = SourceTools(names=("Mul",), effects={"Mul": EFFECT_WRITE}, origins={"Mul": "entry"})
    tools = gateway_source(gw, servers={"mi-1": server}, published={"hi-pub": below})
    assert dict(tools.origins) == {
        "listOrders": "internal",
        "web_fetch": "platform",
        "Add": "remote",
        "send_mail": "image",
        "Mul": "entry",
    }


def test_a_gateway_with_a_nameless_backend_knows_no_backend_kinds() -> None:
    """이름을 모르는 백엔드가 같은 이름을 낼 수 있으므로 어느 이름의 백엔드 종류도 믿지 않는다."""
    gw = _gateway(
        backend_snapshot("dyn", "remote", []),
        backend_snapshot("orders", "internal", ["listOrders"], server_id="mi-1"),
    )
    server = server_source(_server().deployed, {("bp", 1): _version()})
    assert gateway_source(gw, servers={"mi-1": server}, published={}).origins == {}


def test_canvas_marks_count_only_where_the_server_cannot_say_on_tool_safety_images() -> None:
    tools = SourceTools(
        names=("mine", "theirs"),
        effects={"mine": EFFECT_WRITE, "theirs": EFFECT_WRITE},
        origins={"mine": "internal", "theirs": "remote"},
    )
    marked = ["mine", "theirs"]
    assert node_effects(marked, tools, read_only=marked) == dict.fromkeys(marked, EFFECT_READ)
    assert node_effects(marked, tools, read_only=marked, tool_safety=True) == {
        "mine": EFFECT_WRITE,
        "theirs": EFFECT_READ,
    }


def test_prefix_mode_and_published_items_keep_the_web_kind() -> None:
    """접두사 모드는 `<workload>_<tool>` 이고, 게시 항목은 원본 Gateway 의 효과를 이어받는다. 필터
    없는 게시 백엔드 아래에 웹 Tool 이 있으면 원본이 광고하는 것을 그대로 내므로 이름을 모른다.
    필터가 있으면 그 이름을 안다."""
    below = SourceTools(
        names=("web_fetch",), effects={"web_fetch": EFFECT_WEB}, web={"web_fetch": WEB_FETCH}
    )
    for extra, names in (({}, None), ({"tools": ["web_fetch"]}, ("gw-pub_web_fetch",))):
        gw = _gateway(
            backend_snapshot("pub", "published", ["web_fetch"], **extra), resolution="prefix"
        )
        tools = gateway_source(gw, servers={}, published={"hi-pub": below})
        assert tools.names == names
        assert tools.effect("gw-pub_web_fetch") == EFFECT_WEB
        assert dict(tools.web) == {"gw-pub_web_fetch": WEB_FETCH}


def test_a_backend_without_names_makes_the_gateway_names_unknown() -> None:
    gw = _gateway(backend_snapshot("calc", "remote", []), resolution="prefix")
    assert gateway_source(gw, servers={}, published={}).names is None
    assert gateway_source(_gateway(), servers={}, published={}).names == ()


def test_read_only_marks_turn_write_into_read_but_not_web() -> None:
    tools = SourceTools(
        names=("a", "b", "web_fetch"),
        effects={"a": EFFECT_WRITE, "b": EFFECT_READ, "web_fetch": EFFECT_WEB},
    )
    marked = node_effects(["a", "b", "web_fetch", "unknown"], tools, read_only=["a", "web_fetch"])
    assert marked == {
        "a": EFFECT_READ,
        "b": EFFECT_READ,
        "web_fetch": EFFECT_WEB,
        "unknown": EFFECT_WRITE,
    }


def test_server_names_follow_the_fastmcp_default_name() -> None:
    """이름 오버라이드가 없는 operationId 는 FastMCP 4.0.3 처럼 `__` 앞을 정규화한 이름으로
    광고한다. 핀과 효과가 런타임이 실제로 광고하는 이름을 본다."""
    rev = MCPServerRevision.model_validate(
        {
            "revision": 1,
            "backends": [
                {
                    "backend_profile_id": "bp-1",
                    "spec_version": 1,
                    "tools": ["get-user.by id", "list__v2", "Add"],
                    "tool_overrides": {"Add": {"name": "Plus"}},
                }
            ],
        }
    )
    assert advertised_tools_of(rev, {}) == ["get_user_by_id", "list", "Plus"]
