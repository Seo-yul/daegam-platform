"""P52 F4: 서버와 허브 항목의 읽기 전용 표시가 Tool 효과가 되고, 캔버스의 표시와 갈린다.

- `server_source` 는 서버 주인의 표시를 메서드보다 먼저 쓴다. Gateway 의 `image` 백엔드와 그 위의
  `published` 백엔드는 항목의 표시를 백엔드의 원래 이름으로 따르고, 표시가 없으면 `write` 다.
- `SourceTools.marked` 는 서버나 항목의 표시가 효과 뒤에 설 수 있는 이름이다. 캔버스의 표시는
  `SourceTools` 를 바꾸지 않고 `node_effects` 에서만 `write` 를 `read` 로 바꾸며, 승인 규칙에서는
  표시가 설 수 없는 Tool 에서만 센다.
- 배포 뒤 소스의 효과가 핀과 달라지면 Agent 상태가 `tool_marks_changed` 이고, 다시 배포하면
  사라진다.
- 거절된 호출을 견디지 못하는 이미지가 규칙 있는 Tool 을 쓰면 `runtime_tool_refusals` 다.
"""

from __future__ import annotations

from typing import Any

from daegam.services.agent_status import refusals_unknown, uses_ruled_tools
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    SourceTools,
    gateway_source,
    graph_tools,
    node_effects,
    server_source,
)
from daegam.services.tool_pins import TOOL_MARKS_CHANGED, marks_drift, pin_drift, pins_of
from daegam.store.agents import AgentRuntime
from daegam.store.hub import HubItem, HubK8s
from tests.helpers.docs import agent_doc, gateway_doc
from tests.helpers.gateways import backend_snapshot
from tests.test_tool_effects import _gateway, _server, _version

DEV = {"callers": {"roles": ["developer"]}}


def _marked_server(**rules: Any) -> SourceTools:
    return server_source(_server(tool_rules=rules).deployed, {("bp", 1): _version()})


def _item(rules: dict[str, Any] | None = None, item_id: str = "hi-mail") -> HubItem:
    return HubItem.model_validate(
        {
            "id": item_id,
            "tenant": "default",
            "name": "mail",
            "display_name": "mail",
            "kind": "image",
            "cluster_id": "c1",
            "k8s": HubK8s(kind="MCPServer", name="mail", namespace="ns").model_dump(),
            "tool_names": ["send_mail", "list_mail"],
            "tool_rules": rules or {},
        }
    )


# -- 소스의 효과 -----------------------------------------------------------------------------


def test_the_servers_stored_mark_replaces_the_method_effect() -> None:
    tools = _marked_server(listOrders={"read_only": False}, createOrder={"read_only": True, **DEV})
    assert dict(tools.effects) == {
        "listOrders": EFFECT_WRITE,
        "createOrder": EFFECT_READ,
        "orderExists": EFFECT_READ,  # 표시가 없으면 메서드(HEAD)다
    }
    # 우리 mcp-runtime 의 Tool 은 모두 서버가 효과를 정한다
    assert tools.marked == {"listOrders", "createOrder", "orderExists"}
    assert tools.ruled == {"createOrder"}


def test_conditions_rule_a_server_tool_and_a_mark_alone_does_not() -> None:
    cond = {"conditions": [{"path": "a", "op": "max", "value": 1}]}
    tools = _marked_server(listOrders=cond, orderExists={"read_only": False})
    assert tools.ruled == {"listOrders"}


def test_an_image_backend_follows_its_items_marks_and_writes_where_none_is_stored() -> None:
    marked = _item({"list_mail": {"read_only": True}, "send_mail": DEV})
    gw = _gateway(
        backend_snapshot("mail", "image", ["send_mail", "list_mail"]),
        backend_snapshot("other", "image", ["fetch"], hub_item_id="hi-other"),
        backend_snapshot("calc", "remote", ["Add"]),
        backend_snapshot("feed", "entry", ["read_feed"]),
    )
    plain = _item({}, item_id="hi-other")
    tools = gateway_source(
        gw, servers={}, published={}, items={"hi-mail": marked, "hi-other": plain}
    )
    assert dict(tools.effects) == {
        "send_mail": EFFECT_WRITE,
        "list_mail": EFFECT_READ,
        "fetch": EFFECT_WRITE,
        "Add": EFFECT_WRITE,
        "read_feed": EFFECT_WRITE,
    }
    # 규칙이 있는 image 항목의 Tool 만 항목의 표시가 선다. remote·entry 와 규칙 없는 image 는 아니다
    assert tools.marked == {"send_mail", "list_mail"}
    assert tools.ruled == {"send_mail"}


def test_a_published_backend_over_an_image_item_follows_it() -> None:
    marked = _item({"list_mail": {"read_only": True}})
    below = gateway_source(
        _gateway(backend_snapshot("mail", "image", ["send_mail", "list_mail"])),
        servers={},
        published={},
        items={"hi-mail": marked},
    )
    top = gateway_doc(
        "gw-2",
        state="deployed",
        deployed_backends=[
            backend_snapshot("pub", "published", ["send_mail", "list_mail"], tools=["list_mail"])
        ],
    )
    tools = gateway_source(top, servers={}, published={"hi-pub": below})
    assert dict(tools.effects) == {"list_mail": EFFECT_READ}
    assert tools.marked == {"list_mail"} and dict(tools.origins) == {"list_mail": "image"}


def test_an_internal_backend_carries_the_servers_marks_and_rules() -> None:
    server = _marked_server(createOrder={"read_only": True, **DEV})
    gw = _gateway(
        backend_snapshot("orders", "internal", ["listOrders", "createOrder"], server_id="mi-1")
    )
    tools = gateway_source(gw, servers={"mi-1": server}, published={})
    assert dict(tools.effects) == {"listOrders": EFFECT_READ, "createOrder": EFFECT_READ}
    assert tools.marked == {"listOrders", "createOrder"} and tools.ruled == {"createOrder"}


def test_the_gateways_own_rules_rule_its_advertised_names() -> None:
    gw = gateway_doc(
        "gw-1",
        state="deployed",
        spec={"backends": [{"hub_item_id": "hi-calc", "tool_rules": {"Add": DEV}}]},
        deployed_backends=[backend_snapshot("calc", "remote", ["Add", "Sub"])],
    )
    tools = gateway_source(gw, servers={}, published={})
    assert tools.ruled == {"Add"} and tools.marked == frozenset()


# -- 캔버스의 표시 ---------------------------------------------------------------------------------


def _origin_source() -> SourceTools:
    server = _marked_server()
    marked = _item({"list_mail": {"read_only": False}})
    gw = _gateway(
        backend_snapshot("orders", "internal", ["createOrder"], server_id="mi-1"),
        backend_snapshot("mail", "image", ["send_mail", "list_mail"]),
        backend_snapshot("other", "image", ["fetch"], hub_item_id="hi-other"),
        backend_snapshot("calc", "remote", ["Add"]),
        backend_snapshot("feed", "entry", ["read_feed"]),
    )
    items = {"hi-mail": marked, "hi-other": _item({}, item_id="hi-other")}
    return gateway_source(gw, servers={"mi-1": server}, published={}, items=items)


def test_the_canvas_mark_counts_only_where_no_server_or_item_mark_can_exist() -> None:
    tools = _origin_source()
    names = list(tools.names or ())
    assert {n: tools.canvas_mark_counts(n) for n in names} == {
        "createOrder": False,  # mcp-runtime
        "send_mail": False,  # 규칙이 있는 image 항목
        "list_mail": False,
        "fetch": True,  # 규칙 없는 image 항목
        "Add": True,  # remote
        "read_feed": True,  # entry
    }
    before = dict(tools.effects)
    effects = node_effects(names, tools, read_only=names, tool_safety=True)
    assert dict(tools.effects) == before  # 캔버스의 표시는 소스를 바꾸지 않는다
    assert effects == {
        "createOrder": EFFECT_WRITE,
        "send_mail": EFFECT_WRITE,
        "list_mail": EFFECT_WRITE,
        "fetch": EFFECT_READ,
        "Add": EFFECT_READ,
        "read_feed": EFFECT_READ,
    }
    # 정적 승인의 기본값에서는 캔버스의 표시가 어디서든 write 만 read 로 바꾼다
    assert set(node_effects(names, tools, read_only=names).values()) == {EFFECT_READ}


def test_validate_reports_the_marked_names_per_node() -> None:
    spec = agent_doc(
        spec={
            "graph": {
                "nodes": [
                    {"id": "entry", "type": "entry", "data": {}},
                    {
                        "id": "t1",
                        "type": "tools",
                        "data": {"source": {"kind": "gateway_instance", "id": "gw-1"}},
                    },
                ],
                "edges": [],
            }
        }
    ).spec
    tools = graph_tools(spec, {"gw-1": _origin_source()}, tool_safety=True)
    assert tools.marked == {"t1": ["createOrder", "send_mail", "list_mail"]}


# -- 배포 뒤 바뀐 표시 -------------------------------------------------------------------------


def _tools_spec(source_id: str = "mi-1", **data: Any):
    return agent_doc(
        spec={
            "graph": {
                "nodes": [
                    {"id": "entry", "type": "entry", "data": {}},
                    {
                        "id": "t1",
                        "type": "tools",
                        "data": {"source": {"kind": "mcp_instance", "id": source_id}, **data},
                    },
                ],
                "edges": [],
            }
        }
    ).spec


def test_a_mark_change_raises_tool_marks_changed_until_the_agent_is_redeployed() -> None:
    spec = _tools_spec()
    deployed = {"mi-1": _marked_server()}
    pins = pins_of(spec, deployed)
    assert marks_drift(spec, pins, deployed) == []
    changed = {"mi-1": _marked_server(createOrder={"read_only": True})}
    assert marks_drift(spec, pins, changed) == [f"{TOOL_MARKS_CHANGED}: t1"]
    # 다시 배포하면 핀이 지금 효과를 박는다
    assert marks_drift(spec, pins_of(spec, changed), changed) == []


def test_a_vanished_tool_is_tools_missing_not_a_mark_change() -> None:
    spec = _tools_spec()
    deployed = {"mi-1": _marked_server()}
    pins = pins_of(spec, deployed)
    fewer = {"mi-1": server_source(_server(tools=["listOrders"]).deployed, {("bp", 1): _version()})}
    assert marks_drift(spec, pins, fewer) == []
    assert pin_drift(spec, pins, fewer) == ["tools_missing: t1"]


def test_web_effects_are_not_marks() -> None:
    spec = _tools_spec("gw-1")
    web = SourceTools(names=("web_fetch",), effects={"web_fetch": EFFECT_WEB})
    plain = SourceTools(names=("web_fetch",), effects={"web_fetch": EFFECT_WRITE})
    assert marks_drift(spec, pins_of(spec, {"gw-1": web}), {"gw-1": plain}) == []


# -- 거절을 견디지 못하는 이미지 -------------------------------------------------------------------


def test_an_old_image_that_uses_a_ruled_tool_is_flagged() -> None:
    doc = agent_doc(
        state="deployed",
        effective_config={"images": {"agent_image": "agent:1"}},
        spec=_tools_spec().model_dump(),
    )
    ruled = {"mi-1": _marked_server(listOrders=DEV)}
    open_ = {"mi-1": _marked_server()}
    assert not refusals_unknown(doc)  # 기록이 없으면 모른다
    doc.runtime = AgentRuntime(image="agent:1", features=["turn_tokens"])
    assert refusals_unknown(doc)
    assert uses_ruled_tools(doc.spec, ruled) and not uses_ruled_tools(doc.spec, open_)
    picked = _tools_spec(allow_tools=["createOrder"])
    assert not uses_ruled_tools(picked, ruled)
    doc.runtime = AgentRuntime(image="agent:1", features=["tool_refusals"])
    assert not refusals_unknown(doc)
