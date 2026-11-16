"""바깥 표시와 읽기 표시의 렌더와 경고다(P43 2·4·8절, `services/agent_tool_safety.py`).

템플릿의 계산기 소스 하나에 백엔드 종류가 다른 Tool 을 둔다. `list_orders`·`create_order` 는 우리
mcp-runtime 의 GET(읽기)과 POST(쓰기), `send_mail` 은 remote 항목의 Tool, `web_fetch` 는 웹 Tool,
`ghost` 는 백엔드 종류를 모르는 이름이다."""

from __future__ import annotations

from typing import Any

from daegam.services.agent_features import TOOL_SAFETY, finish_document
from daegam.services.agent_tool_safety import (
    OUTSIDE_MARK_OFF,
    OUTSIDE_WRITES_REFUSED,
    READ_ONLY_BY_CANVAS,
    safety_issues,
)
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    WEB_FETCH,
    SourceTools,
    graph_tools,
)
from daegam.services.tool_pins import apply_pins
from daegam.store.agents import AgentSpec, ToolPin
from tests.helpers.agents import (
    FAQ_ID,
    attach,
    compile_spec,
    ctx_with,
    kb,
    knowledge_node,
    make_ctx,
    node_of,
    pinned_document,
    template_spec,
    without_memory,
)

GW = "gi-calc-0001"
NODE = "tools-calculator"
SOURCE = SourceTools(
    names=("list_orders", "create_order", "send_mail", "web_fetch", "ghost"),
    effects={
        "list_orders": EFFECT_READ,
        "create_order": EFFECT_WRITE,
        "send_mail": EFFECT_WRITE,
        "web_fetch": EFFECT_WEB,
        "ghost": EFFECT_WRITE,
    },
    web={"web_fetch": WEB_FETCH},
    origins={
        "list_orders": "internal",
        "create_order": "internal",
        "send_mail": "remote",
        "web_fetch": "platform",
    },
)


def _spec(*, memory: bool = True, **tools: Any) -> dict[str, Any]:
    spec = template_spec()
    data = node_of(spec, NODE)["data"]
    data.update({"allow_tools": [], "approval": {}, "timeouts": {}, **tools})
    return spec if memory else without_memory(spec)


def _ctx():
    ctx = make_ctx()
    ctx.source_tools = {GW: SOURCE}
    return ctx


def _final(spec: dict[str, Any], ctx) -> dict[str, Any]:
    """배포처럼 핀을 박고 기능 tool_safety 로 붙인 최종 문서다(스키마 검사 포함)."""
    rev, document = pinned_document(spec, ctx)
    return finish_document(document, frozenset({TOOL_SAFETY}), ctx, rev)


def _codes(spec: dict[str, Any], ctx=None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for issue in safety_issues(AgentSpec.model_validate(spec), ctx or _ctx()):
        out.setdefault(issue.code, []).append(issue.message)
    return out


def test_sources_carry_what_is_known_safe_by_backend_kind() -> None:
    """우리 Tool 은 안쪽, 바깥 서비스·웹·모르는 이름은 바깥이다. 읽기는 GET 과 웹 Tool 뿐이다."""
    final = _final(_spec(), _ctx())
    [source] = final["tools"]
    assert final["features"] == [TOOL_SAFETY]
    assert source["inside"] == ["list_orders", "create_order"]
    assert source["read"] == ["list_orders", "web_fetch"]


def test_the_owner_switches_the_outside_mark_per_tool() -> None:
    final = _final(
        _spec(outside={"create_order": True, "send_mail": False, "ghost": False}), _ctx()
    )
    [source] = final["tools"]
    # 모르는 이름은 끌 수 없다
    assert source["inside"] == ["list_orders", "send_mail"]
    [message] = _codes(_spec(outside={"send_mail": False}))[OUTSIDE_MARK_OFF]
    assert "send_mail" in message


def test_canvas_marks_count_only_for_tools_whose_server_cannot_say() -> None:
    spec = _spec(read_only=["create_order", "send_mail", "ghost"])
    [source] = _final(spec, _ctx())["tools"]
    assert source["read"] == ["list_orders", "send_mail", "web_fetch"]
    [message] = _codes(spec)[READ_ONLY_BY_CANVAS]
    assert "send_mail" in message and "create_order" not in message
    # 캔버스의 효과 배지도 같은 효과다. 적용되지 않은 표시는 쓰기로 남는다
    effects = graph_tools(AgentSpec.model_validate(spec), {GW: SOURCE}, tool_safety=True).effects
    assert effects[NODE]["create_order"] == EFFECT_WRITE
    assert effects[NODE]["send_mail"] == EFFECT_READ


def test_the_canvas_learns_each_tools_backend_kind() -> None:
    """바깥 표시의 기본값과 읽기 전용 표시의 효과를 캔버스가 가르는 재료다. 모르는 이름은 없다."""

    def origins(spec: dict[str, Any], sources: dict[str, SourceTools]) -> dict[str, dict[str, str]]:
        return graph_tools(AgentSpec.model_validate(spec), sources).origins

    assert origins(_spec(), {GW: SOURCE}) == {NODE: {**SOURCE.origins}}
    picked = origins(_spec(allow_tools=["send_mail", "ghost"]), {GW: SOURCE})
    assert picked == {NODE: {"send_mail": "remote"}}
    assert origins(_spec(), {}) == {NODE: {}}


def test_a_read_tool_of_unknown_backend_kind_is_a_write_after_outside_content() -> None:
    """백엔드 종류를 모르는 GET Tool 은 런타임이 읽기로 받지 않는다. 캔버스의 효과도 그 규칙을
    따른다."""
    unnamed = SourceTools(names=None, effects={"list_orders": EFFECT_READ, "web_fetch": EFFECT_WEB})
    spec = AgentSpec.model_validate(_spec(allow_tools=["list_orders", "web_fetch"]))
    after = graph_tools(spec, {GW: unnamed}, tool_safety=True).effects[NODE]
    assert after == {"list_orders": EFFECT_WRITE, "web_fetch": EFFECT_WEB}
    assert graph_tools(spec, {GW: unnamed}).effects[NODE]["list_orders"] == EFFECT_READ


def test_knowledge_items_are_outside_unless_switched_off() -> None:
    ctx = ctx_with(kb(FAQ_ID))
    ctx.source_tools = {GW: SOURCE}
    for data, outside in (({}, True), ({"outside": False}, False)):
        spec = _spec()
        spec["graph"]["nodes"].append(knowledge_node(**data))
        attach(spec, "agent-main", "kb-faq")
        assert [item["outside"] for item in _final(spec, ctx)["knowledge"]] == [outside]


def test_a_graph_without_a_memory_node_warns_that_writes_are_refused() -> None:
    assert OUTSIDE_WRITES_REFUSED not in _codes(_spec())
    assert OUTSIDE_WRITES_REFUSED in _codes(_spec(memory=False))
    reads = _spec(memory=False, allow_tools=["list_orders"])
    assert OUTSIDE_WRITES_REFUSED not in _codes(reads), "읽기만 하는 그래프는 거절할 쓰기가 없다"


def test_a_preview_warns_that_writes_are_refused_only_where_its_source_would() -> None:
    """미리 보기의 정의에는 memory 노드가 없다. 원본에 있었으면 파드 안에서 묻으므로 경고하지
    않고, 없었으면 배포한 원본처럼 거절하므로 경고한다(P44b 7절, `in_pod_approvals`)."""
    ctx = _ctx()
    ctx.in_pod_state = True
    assert OUTSIDE_WRITES_REFUSED in _codes(_spec(memory=False), ctx)
    ctx.in_pod_approvals = True
    assert OUTSIDE_WRITES_REFUSED not in _codes(_spec(memory=False), ctx)


def test_a_pinned_read_without_a_known_backend_is_not_read() -> None:
    """핀이 읽기라고 해도 지금 소스가 백엔드 종류를 모르는 이름은 읽기가 아니다."""
    rev = AgentSpec.model_validate(_spec())
    ctx = _ctx()
    ctx.tool_pins = {NODE: ToolPin(names=["ghost"], effects={"ghost": EFFECT_READ})}
    document = apply_pins(compile_spec(_spec(), ctx), rev, ctx.tool_pins, ctx.source_tools)
    [source] = finish_document(document, frozenset({TOOL_SAFETY}), ctx, rev)["tools"]
    assert source["allow"] == ["ghost"] and source["read"] == [] and source["inside"] == []
