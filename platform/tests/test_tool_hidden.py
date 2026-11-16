"""P52 F5: 누가 부를지의 규칙이 Agent 가 고른 Tool 을 숨기면 Agent 상태가 원인마다 알린다.

- 소스의 `gates` 는 규칙 하나마다 그 규칙이 이름 댄 Agent 다. 서버의 규칙은 서버에서, Gateway 의
  `internal` 백엔드에서는 그 vMCP 에서 집행되고, `image` 항목의 규칙은 어떤 Agent 도 받지 않는다.
- 원인은 넷이다. 규칙이 이 Agent 를 이름 대지 않았다, 허브 항목의 규칙이다, oidc 모드의 Gateway
  다, 이름 댄 Agent 의 토큰을 새로 내는 중이다. 마지막은 서비스가 판을 견주어 더한다.
"""

from __future__ import annotations

from daegam.services.agent_status import (
    AGENT_TOOL_HIDDEN,
    HIDDEN_ITEM,
    HIDDEN_NOT_NAMED,
    HIDDEN_OIDC,
    HIDDEN_REISSUE,
    hidden_tool_warnings,
    hidden_tools,
)
from daegam.services.tool_effects import AgentGate, SourceTools, gateway_source, server_source
from tests.helpers.docs import gateway_doc
from tests.helpers.gateways import backend_snapshot
from tests.test_tool_effects import _gateway, _server, _version
from tests.test_tool_marks import _item, _tools_spec

AGENT = "ag-1"


def _rule(*agents: str, roles: tuple[str, ...] = ()) -> dict:
    return {"callers": {"agents": list(agents), "roles": list(roles)}}


def _server_tools(**rules: dict) -> SourceTools:
    return server_source(_server(tool_rules=rules).deployed, {("bp", 1): _version()})


def test_a_servers_rule_gates_its_tool_by_the_named_agents() -> None:
    tools = _server_tools(createOrder=_rule(AGENT), listOrders={"read_only": True})
    assert dict(tools.gates) == {"createOrder": (AgentGate(frozenset({AGENT})),)}


def test_a_rule_that_does_not_name_the_agent_hides_the_tool() -> None:
    spec = _tools_spec()
    sources = {"mi-1": _server_tools(createOrder=_rule(roles=("developer",)))}
    hidden, admitted = hidden_tools(AGENT, spec, sources, trusts_idp=False)
    assert hidden == {HIDDEN_NOT_NAMED: ["createOrder"]} and admitted == []
    named = {"mi-1": _server_tools(createOrder=_rule(AGENT))}
    assert hidden_tools(AGENT, spec, named, trusts_idp=False) == ({}, ["createOrder"])
    # 서버의 Agent 절은 모든 모드에서 짓는다
    assert hidden_tools(AGENT, spec, named, trusts_idp=True) == ({}, ["createOrder"])


def test_only_the_selected_tools_count() -> None:
    sources = {"mi-1": _server_tools(createOrder=_rule(roles=("viewer",)))}
    picked = _tools_spec(allow_tools=["listOrders"])
    assert hidden_tools(AGENT, picked, sources, trusts_idp=False) == ({}, [])


def test_an_internal_backend_carries_the_servers_rule_to_the_vmcp() -> None:
    server = _server_tools(createOrder=_rule(AGENT))
    gw = _gateway(
        backend_snapshot("orders", "internal", ["listOrders", "createOrder"], server_id="mi-1")
    )
    tools = gateway_source(gw, servers={"mi-1": server}, published={})
    assert dict(tools.gates) == {"createOrder": (AgentGate(frozenset({AGENT}), gateway=True),)}
    spec = _tools_spec("gw-1")
    assert hidden_tools(AGENT, spec, {"gw-1": tools}, trusts_idp=False) == ({}, ["createOrder"])
    # oidc 모드의 vMCP 는 Agent 절을 짓지 않는다
    hidden, admitted = hidden_tools(AGENT, spec, {"gw-1": tools}, trusts_idp=True)
    assert hidden == {HIDDEN_OIDC: ["createOrder"]} and admitted == []


def test_an_image_items_rule_hides_the_tool_from_every_agent() -> None:
    gw = _gateway(backend_snapshot("mail", "image", ["send_mail", "list_mail"]))
    item = _item({"send_mail": _rule(roles=("viewer",)), "list_mail": {"read_only": True}})
    tools = gateway_source(gw, servers={}, published={}, items={"hi-mail": item})
    hidden, _admitted = hidden_tools(AGENT, _tools_spec("gw-1"), {"gw-1": tools}, trusts_idp=False)
    assert hidden == {HIDDEN_ITEM: ["send_mail"]}


def test_the_gateways_own_rule_and_the_servers_rule_both_hold() -> None:
    server = _server_tools(createOrder=_rule(AGENT))
    dev = _rule(roles=("developer",))
    gw = gateway_doc(
        "gw-1",
        state="deployed",
        spec={"backends": [{"hub_item_id": "hi-orders", "tool_rules": {"createOrder": dev}}]},
        deployed_backends=[
            backend_snapshot("orders", "internal", ["listOrders", "createOrder"], server_id="mi-1")
        ],
    )
    tools = gateway_source(gw, servers={"mi-1": server}, published={})
    assert len(tools.gates["createOrder"]) == 2
    hidden, admitted = hidden_tools(AGENT, _tools_spec("gw-1"), {"gw-1": tools}, trusts_idp=False)
    assert hidden == {HIDDEN_NOT_NAMED: ["createOrder"]} and admitted == []


def test_a_published_backend_keeps_the_original_gateways_gates() -> None:
    below = gateway_source(
        _gateway(backend_snapshot("mail", "image", ["send_mail"])),
        servers={},
        published={},
        items={"hi-mail": _item({"send_mail": _rule(roles=("viewer",))})},
    )
    top = gateway_doc(
        "gw-2",
        state="deployed",
        deployed_backends=[backend_snapshot("pub", "published", ["send_mail"])],
    )
    tools = gateway_source(top, servers={}, published={"hi-pub": below})
    assert tools.gates["send_mail"] == (AgentGate(frozenset(), item=True, gateway=True),)


def test_one_line_per_cause_in_a_fixed_order() -> None:
    lines = hidden_tool_warnings(
        {HIDDEN_REISSUE: ["a"], HIDDEN_NOT_NAMED: ["b", "c", "b"], HIDDEN_ITEM: []}
    )
    assert lines == [
        f"{AGENT_TOOL_HIDDEN}: a Tool rule hides Tools this Agent selected "
        "(cause: not_named; tools: b, c)",
        f"{AGENT_TOOL_HIDDEN}: a Tool rule hides Tools this Agent selected "
        "(cause: reissue; tools: a)",
    ]
