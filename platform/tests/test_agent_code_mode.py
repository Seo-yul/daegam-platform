"""Agent 가 code mode Gateway 의 스크립트를 쓰는 규칙이다 (P56 6.5절, `agent_code_mode.py`).

템플릿의 계산기 Gateway `gi-calc-0001` 이 code mode 로 돈다. `Add` 는 읽기, `execute_tool_script` 는
그 Gateway 의 스크립트다. 다른 Gateway `gi-other-0001` 도 code mode 로 돈다."""

from __future__ import annotations

import copy
from typing import Any

from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.agent_code_mode import (
    CODE_MODE_DUPLICATE_SCRIPT,
    CODE_MODE_PARTIAL_SELECTION,
    DIALECT_LINE,
    SCRIPT_TIMEOUT_SECONDS,
    attach_code_mode,
    code_mode_issues,
)
from daegam.services.agent_features import TOOL_SAFETY, finish_document
from daegam.services.agent_tool_safety import (
    OUTSIDE_MARK_OFF,
    READ_ONLY_BY_CANVAS,
    safety_issues,
)
from daegam.services.tool_effects import (
    EFFECT_READ,
    ORIGIN_SCRIPT,
    SourceTools,
    gateway_source,
    graph_tools,
    node_effects,
    without_script,
)
from daegam.services.tool_pins import apply_pins
from daegam.store.agents import AgentSpec
from daegam.store.gateways import GatewayK8s
from tests.helpers.agents import (
    CTX_TENANT,
    add_sub_agent,
    attach,
    compile_spec,
    make_ctx,
    node_of,
    pinned_document,
    template_spec,
)
from tests.helpers.docs import gateway_doc
from tests.helpers.gateways import backend_snapshot

GW = "gi-calc-0001"
OTHER = "gi-other-0001"
NODE = "tools-calculator"
CALC = SourceTools(
    names=("Add", "Mul", CODE_MODE_TOOL),
    effects={"Add": EFFECT_READ, "Mul": EFFECT_READ, CODE_MODE_TOOL: EFFECT_READ},
    origins={"Add": "internal", "Mul": "internal", CODE_MODE_TOOL: ORIGIN_SCRIPT},
    marked=frozenset({"Add", "Mul", CODE_MODE_TOOL}),
)
OTHER_TOOLS = SourceTools(
    names=("Sub", CODE_MODE_TOOL),
    effects={"Sub": EFFECT_READ, CODE_MODE_TOOL: EFFECT_READ},
    origins={"Sub": "internal", CODE_MODE_TOOL: ORIGIN_SCRIPT},
    marked=frozenset({"Sub", CODE_MODE_TOOL}),
)


def _spec(**tools: Any) -> dict[str, Any]:
    spec = template_spec()
    node_of(spec, NODE)["data"].update({"allow_tools": [], "approval": {}, "timeouts": {}, **tools})
    return spec


def _tools_node(spec: dict[str, Any], node_id: str, source: str, agent: str, **data: Any) -> None:
    spec["graph"]["nodes"].append(
        {
            "id": node_id,
            "type": "tools",
            "data": {"source": {"kind": "gateway_instance", "id": source}, **data},
        }
    )
    attach(spec, agent, node_id, "tools")


def _sub(spec: dict[str, Any], node_id: str = "agent-sub", name: str = "researcher") -> None:
    add_sub_agent(spec, node_id, name)
    attach(spec, "agent-main", node_id, "delegate")


def _ctx(**sources: SourceTools):
    ctx = make_ctx()
    other = gateway_doc(
        OTHER,
        tenant=CTX_TENANT,
        name="other",
        state="deployed",
        k8s=GatewayK8s(name="gw-other", namespace=CTX_TENANT, group="gw-other"),
    )
    ctx.gateway_instances[other.id] = other
    ctx.source_tools = {GW: CALC, OTHER: OTHER_TOOLS, **sources}
    return ctx


def _codes(spec: dict[str, Any], ctx=None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for issue in code_mode_issues(AgentSpec.model_validate(spec), ctx or _ctx()):
        out.setdefault(issue.code, []).append(issue.to_dict())
    return out


def _compiled(spec: dict[str, Any], ctx=None) -> dict[str, Any]:
    """배포처럼 핀을 박고 스크립트의 규칙을 실은 문서다."""
    ctx = ctx or _ctx()
    rev, document = pinned_document(spec, ctx)
    return attach_code_mode(document, rev, ctx)


# -- 소스 ----------------------------------------------------------------------------------------


def _code_gateway(*, applied: bool, **extra: Any):
    return gateway_doc(
        GW,
        state="deployed",
        deployed_backends=[
            backend_snapshot("orders", "internal", ["listOrders"], server_id="mi-1"),
            backend_snapshot("pub", "published", ["Mul", CODE_MODE_TOOL]),
        ],
        deployed_conflict_resolution="priority",
        code_mode_applied=applied,
        **extra,
    )


def test_a_gateway_whose_running_render_carries_code_mode_offers_its_script() -> None:
    server = SourceTools(
        names=("listOrders",),
        effects={"listOrders": EFFECT_READ},
        origins={"listOrders": "internal"},
    )
    below = SourceTools(
        names=("Mul", CODE_MODE_TOOL),
        effects={"Mul": EFFECT_READ, CODE_MODE_TOOL: EFFECT_READ},
        origins={"Mul": "internal", CODE_MODE_TOOL: ORIGIN_SCRIPT},
    )
    on = gateway_source(
        _code_gateway(applied=True), servers={"mi-1": server}, published={"hi-pub": below}
    )
    # 게시된 원본의 스크립트는 빠지고 이 Gateway 의 스크립트가 끝에 선다
    assert on.names == ("listOrders", "Mul", CODE_MODE_TOOL)
    assert on.effect(CODE_MODE_TOOL) == EFFECT_READ
    assert on.origins[CODE_MODE_TOOL] == ORIGIN_SCRIPT
    assert CODE_MODE_TOOL in on.marked
    off = gateway_source(
        _code_gateway(applied=False), servers={"mi-1": server}, published={"hi-pub": below}
    )
    assert off.names == ("listOrders", "Mul")
    assert CODE_MODE_TOOL not in off.effects


def test_the_gateway_verdict_reads_the_source_without_its_own_script() -> None:
    assert without_script(CALC) == SourceTools(
        names=("Add", "Mul"),
        effects={"Add": EFFECT_READ, "Mul": EFFECT_READ},
        origins={"Add": "internal", "Mul": "internal"},
        marked=frozenset({"Add", "Mul"}),
    )
    # 백엔드가 같은 이름을 낸 것은 판정이 예약 이름으로 거절해야 하므로 남는다
    backend = SourceTools(
        names=("Add", CODE_MODE_TOOL),
        effects={"Add": EFFECT_READ, CODE_MODE_TOOL: EFFECT_READ},
        origins={"Add": "internal", CODE_MODE_TOOL: "internal"},
    )
    assert without_script(backend) is backend


# -- P43 의 표시 ---------------------------------------------------------------------------------


def test_the_script_is_read_always_outside_and_takes_no_canvas_mark() -> None:
    """스크립트는 `read` 에 들고, 노드가 바깥 표시를 꺼도 `inside` 에 들지 않으며, 캔버스 표시의
    경고를 내지 않는다(P56 9절 F3)."""
    ctx = _ctx()
    spec = _spec(outside={CODE_MODE_TOOL: False}, read_only=[CODE_MODE_TOOL])
    rev, document = pinned_document(spec, ctx)
    final = finish_document(document, frozenset({TOOL_SAFETY}), ctx, rev)
    [source] = final["tools"]
    assert CODE_MODE_TOOL in source["read"]
    assert CODE_MODE_TOOL not in source["inside"]
    assert source["inside"] == ["Add", "Mul"]
    codes = {i.code for i in safety_issues(AgentSpec.model_validate(spec), ctx)}
    assert not codes & {OUTSIDE_MARK_OFF, READ_ONLY_BY_CANVAS}
    effects = node_effects([CODE_MODE_TOOL], CALC, [CODE_MODE_TOOL], tool_safety=True)
    assert effects == {CODE_MODE_TOOL: EFFECT_READ}
    origins = graph_tools(AgentSpec.model_validate(spec), ctx.source_tools).origins
    assert origins[NODE][CODE_MODE_TOOL] == ORIGIN_SCRIPT


# -- 선택과 중복 ---------------------------------------------------------------------------------


def test_a_node_that_takes_the_script_selects_every_tool_of_its_gateway() -> None:
    assert _codes(_spec()) == {}
    assert _codes(_spec(allow_tools=["Add", "Mul", CODE_MODE_TOOL])) == {}
    assert _codes(_spec(allow_tools=["Add", "Mul"])) == {}
    [issue] = _codes(_spec(allow_tools=["Add", CODE_MODE_TOOL]))[CODE_MODE_PARTIAL_SELECTION]
    assert issue["level"] == "error" and issue["node_id"] == NODE
    assert "Mul" in issue["message"]


def test_an_agent_takes_the_script_from_one_gateway_only() -> None:
    spec = _spec()
    _tools_node(spec, "tools-other", OTHER, "agent-main")
    [issue] = _codes(spec)[CODE_MODE_DUPLICATE_SCRIPT]
    assert issue["level"] == "error" and issue["node_id"] == "tools-other"
    # 그 노드가 스크립트를 빼고 고르면 된다
    fixed = _spec()
    _tools_node(fixed, "tools-other", OTHER, "agent-main", allow_tools=["Sub"])
    assert _codes(fixed) == {}
    # 같은 Gateway 를 두 노드가 들거나, 다른 에이전트가 다른 Gateway 를 들면 섞이지 않는다
    same = _spec()
    _tools_node(same, "tools-calc-2", GW, "agent-main")
    assert _codes(same) == {}
    split = _spec()
    _sub(split)
    _tools_node(split, "tools-other", OTHER, "agent-sub")
    assert _codes(split) == {}


def test_a_server_tool_named_like_the_script_is_not_the_script() -> None:
    plain = SourceTools(
        names=("Add", CODE_MODE_TOOL),
        effects={"Add": EFFECT_READ, CODE_MODE_TOOL: EFFECT_READ},
        origins={"Add": "internal", CODE_MODE_TOOL: "internal"},
    )
    ctx = _ctx(**{GW: plain})
    assert _codes(_spec(allow_tools=[CODE_MODE_TOOL]), ctx) == {}
    final = _compiled(_spec(), ctx)
    [source] = final["tools"]
    assert CODE_MODE_TOOL not in source["timeouts"]
    assert DIALECT_LINE not in final["main"]["system_prompt"]


# -- 컴파일 --------------------------------------------------------------------------------------


def test_the_script_waits_past_the_scripts_minute_unless_the_node_sets_its_own() -> None:
    [source] = _compiled(_spec())["tools"]
    assert CODE_MODE_TOOL in source["allow"]
    assert source["timeouts"][CODE_MODE_TOOL] == SCRIPT_TIMEOUT_SECONDS
    [own] = _compiled(_spec(timeouts={CODE_MODE_TOOL: 30}))["tools"]
    assert own["timeouts"][CODE_MODE_TOOL] == 30


def test_an_approval_on_a_gateway_tool_anywhere_in_the_agent_reaches_the_script() -> None:
    """하위 에이전트의 노드가 Gateway 의 Tool 에 둔 승인이 메인의 스크립트에도 선다. 결정은
    합집합이다."""
    spec = _spec(approval={"Mul": ["approve"]})
    _sub(spec)
    _tools_node(
        spec,
        "tools-sub",
        GW,
        "agent-sub",
        allow_tools=["Add", "Mul", CODE_MODE_TOOL],
        approval={"Add": ["approve", "reject"]},
    )
    final = _compiled(spec)
    sources = {s["ref"]: s for s in final["tools"]}
    assert sources[NODE]["approval"][CODE_MODE_TOOL] == ["approve", "reject"]
    assert sources["tools-sub"]["approval"][CODE_MODE_TOOL] == ["approve", "reject"]
    [sub] = final["subagents"]
    assert sub["interrupt_on"][CODE_MODE_TOOL] == ["approve", "reject"]
    # 다른 Tool 의 승인은 그대로다
    assert sources[NODE]["approval"]["Mul"] == ["approve"]


def test_no_approval_on_the_gateways_tools_leaves_the_script_without_one() -> None:
    spec = _spec()
    _tools_node(spec, "tools-orders", OTHER, "agent-main", allow_tools=["Sub"])
    node_of(spec, "tools-orders")["data"]["approval"] = {"Sub": ["approve"]}
    sources = {s["ref"]: s for s in _compiled(spec)["tools"]}
    assert CODE_MODE_TOOL not in sources[NODE]["approval"]


def test_agents_that_take_the_script_get_the_dialect_line_after_their_prompt() -> None:
    spec = _spec()
    _sub(spec)
    _tools_node(spec, "tools-sub", OTHER, "agent-sub", allow_tools=["Sub"])
    final = _compiled(spec)
    prompt = node_of(spec, "agent-main")["data"]["system_prompt"]
    assert final["main"]["system_prompt"] == f"{prompt}\n\n{DIALECT_LINE}"
    [sub] = final["subagents"]
    assert DIALECT_LINE not in sub["system_prompt"]


def test_a_graph_without_the_script_compiles_as_before() -> None:
    ctx = _ctx(**{GW: SourceTools(names=("Add",), effects={"Add": EFFECT_READ})})
    spec = _spec()
    rev, document = pinned_document(spec, ctx)
    before = copy.deepcopy(document)
    assert attach_code_mode(document, rev, ctx) is document
    assert document == before


def test_a_script_the_pins_left_out_is_not_held() -> None:
    """code mode 를 켜기 전에 배포한 Agent 의 핀에는 스크립트가 없다. 재배포할 때까지 들지
    않는다."""
    ctx = _ctx(**{GW: SourceTools(names=("Add", "Mul"), effects={"Add": EFFECT_READ})})
    spec = _spec()
    rev, document = pinned_document(spec, ctx)
    ctx.source_tools[GW] = CALC
    document = apply_pins(document, rev, ctx.tool_pins, ctx.source_tools)
    final = attach_code_mode(document, rev, ctx)
    [source] = final["tools"]
    assert CODE_MODE_TOOL not in source["allow"]
    assert DIALECT_LINE not in final["main"]["system_prompt"]


def test_an_approval_on_the_script_itself_reaches_every_node_that_holds_it() -> None:
    spec = _spec()
    _sub(spec)
    _tools_node(
        spec,
        "tools-sub",
        GW,
        "agent-sub",
        allow_tools=["Add", "Mul", CODE_MODE_TOOL],
        approval={CODE_MODE_TOOL: ["reject"]},
    )
    final = _compiled(spec)
    sources = {s["ref"]: s for s in final["tools"]}
    assert sources[NODE]["approval"][CODE_MODE_TOOL] == ["reject"]
    [sub] = final["subagents"]
    assert sub["interrupt_on"][CODE_MODE_TOOL] == ["reject"]
    assert DIALECT_LINE in sub["system_prompt"]


def test_an_empty_developer_prompt_gets_the_dialect_line_alone() -> None:
    spec = _spec()
    node_of(spec, "agent-main")["data"]["system_prompt"] = ""
    assert _compiled(spec)["main"]["system_prompt"] == DIALECT_LINE


def test_an_approval_on_a_name_the_gateway_lacks_now_still_reaches_the_script() -> None:
    """그 Gateway 를 소스로 둔 노드의 승인은 이름과 상관없이 스크립트에 선다. 컴파일 때 스냅샷에
    없던 Tool 이 뒤의 재배포로 돌아올 수 있다."""
    spec = _spec(approval={"Divide": ["approve"]})
    [source] = _compiled(spec)["tools"]
    assert source["approval"][CODE_MODE_TOOL] == ["approve"]


def test_a_source_compiled_while_code_mode_was_closed_still_guards_the_script() -> None:
    """컴파일할 때 code mode 가 닫혀 있었어도, 런타임이 다시 읽어 스크립트를 받을 수 있는 소스는
    승인과 타임아웃을 받는다. 방언 줄은 지금 스크립트를 든 에이전트만이다."""
    closed = SourceTools(
        names=("Add", "Mul"),
        effects={"Add": EFFECT_READ, "Mul": EFFECT_READ},
        origins={"Add": "internal", "Mul": "internal"},
    )
    ctx = _ctx(**{GW: closed})
    # 고른 Tool 에 스크립트가 남은 노드
    named = _spec(allow_tools=["Add", "Mul", CODE_MODE_TOOL], approval={"Add": ["approve"]})
    final = _compiled(named, ctx)
    [source] = final["tools"]
    assert source["approval"][CODE_MODE_TOOL] == ["approve"]
    assert source["timeouts"][CODE_MODE_TOOL] == SCRIPT_TIMEOUT_SECONDS
    assert DIALECT_LINE not in final["main"]["system_prompt"]
    # 핀이 없어 다시 읽을 때 모든 Tool 을 받는 노드
    empty = _spec(approval={"Add": ["approve"]})
    rev = AgentSpec.model_validate(empty)
    document = compile_spec(empty, ctx)
    [source] = attach_code_mode(document, rev, ctx)["tools"]
    assert source["allow"] == []
    assert source["approval"][CODE_MODE_TOOL] == ["approve"]
    assert source["timeouts"][CODE_MODE_TOOL] == SCRIPT_TIMEOUT_SECONDS


def test_an_own_script_timeout_anywhere_keeps_the_default_out() -> None:
    """런타임은 타임아웃을 맨 이름으로 합치므로, 한 소스가 정한 값을 다른 소스의 기본값이 덮지 않게
    한다."""
    spec = _spec(timeouts={CODE_MODE_TOOL: 40})
    _tools_node(spec, "tools-calc-2", GW, "agent-main")
    sources = {s["ref"]: s for s in _compiled(spec)["tools"]}
    assert sources[NODE]["timeouts"][CODE_MODE_TOOL] == 40
    assert CODE_MODE_TOOL not in sources["tools-calc-2"]["timeouts"]
