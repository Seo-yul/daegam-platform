"""평가 컴파일이 쓰기 Tool 을 잡는지 본다 (P45 10절, F0.2).

D89 묶음의 유효한 그래프 모두, 템플릿과 e2e 가 저장하는 그래프(`scripts/lib/agent_body.py`), 그리고
다섯 가지 더한 그래프(한 이름이 한 소스에서는 읽기이고 다른 소스에서는 쓰기인 것, 캔버스 표시가
쓰기를 읽기로 바꾼 것, 고른 Tool 이 소스에서 사라진 것, 백엔드 종류를 모르는 읽기, 배포 때 웹 Tool
이던 이름이 지금은 쓰기인 것)를 배포처럼 핀하고 컴파일한 뒤 평가 단계를 태운다. 기대값은 구현을
부르지 않고 이 파일의 소스 표(`TABLE`)로 따로 센다.

- 메서드, 서버나 항목의 표시, 웹 Tool 로 읽기가 아닌 이름은 그것을 내는 모든 소스와 서브에이전트에서
  결정이 `respond`·`reject` 뿐이다. 런타임처럼 메인은 붙인 소스의 승인을 합친 것을, 서브에이전트는
  제 `interrupt_on` 을 읽어 `approve`·`edit` 이 없는지 본다.
- 모든 소스가 읽기라고 하는 이름은 노드의 승인을 그대로 둔다.
- 이름을 모르는 소스가 있는 그래프는 거절된다.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import agent_body
import pytest

from daegam.services import agent_graph
from daegam.services.agent_graph import GraphContext
from daegam.services.agent_templates import templates
from daegam.services.evaluation.holds import HELD_DECISIONS, ToolsUnknown, hold_writes
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    WEB_FETCH,
    SourceTools,
)
from daegam.services.tool_pins import apply_pins, pins_of
from daegam.store.agents import AgentSpec
from tests.helpers.agents import CTX_TENANT, add_sub_agent, make_ctx, node_of, template_spec
from tests.helpers.docs import agent_doc
from tests.test_agent_graph_corpus import CORPUS, build_graph, corpus_spec

CALC = "gi-calc-0001"
ORDERS = "mi-orders-0001"

# 이름 → (효과, 백엔드 종류)
Rows = Mapping[str, tuple[str, str | None]]
# 소스 id → Rows. 효과는 플랫폼이 아는 것(메서드, 서버나 항목의 표시, 웹)이다
Table = Mapping[str, Rows]

TABLE: Table = {
    CALC: {
        "Add": (EFFECT_WRITE, "internal"),
        "Divide": (EFFECT_WRITE, "internal"),
        "Lookup": (EFFECT_READ, "internal"),
        "Rate": (EFFECT_READ, "image"),
        "Post": (EFFECT_WRITE, "entry"),
        "fetch_url": (EFFECT_WEB, "platform"),
    },
    ORDERS: {
        "GetOrder": (EFFECT_READ, "internal"),
        # 같은 이름이 Gateway 에서는 쓰기다
        "Add": (EFFECT_READ, "internal"),
        "CancelOrder": (EFFECT_WRITE, "internal"),
    },
}


def source_tools(table: Table) -> dict[str, SourceTools]:
    return {
        source_id: SourceTools(
            names=tuple(rows),
            effects={n: effect for n, (effect, _o) in rows.items()},
            web={n: WEB_FETCH for n, (effect, _o) in rows.items() if effect == EFFECT_WEB},
            origins={n: origin for n, (_e, origin) in rows.items() if origin is not None},
        )
        for source_id, rows in table.items()
    }


def corpus_table() -> Table:
    """D89 묶음의 소스 둘에 같은 표를 준다."""
    return {"gw-ok": TABLE[CALC], "mi-ok": TABLE[ORDERS]}


def known_read(table: Table, source_id: str, name: str) -> bool:
    """기대값의 읽기. 표에 지금 있고 웹이거나, 백엔드 종류를 아는 읽기다."""
    row = table.get(source_id, {}).get(name)
    if row is None:
        return False
    effect, origin = row
    return effect == EFFECT_WEB or (effect == EFFECT_READ and origin is not None)


def evaluate(
    spec: dict[str, Any],
    ctx: GraphContext,
    *,
    pins_from: Mapping[str, SourceTools] | None = None,
) -> tuple[AgentSpec, dict[str, Any], dict[str, Any]]:
    """배포처럼 핀하고 컴파일한 문서와 평가 단계를 지난 문서다. pins_from 을 주면 그 소스로 핀한다
    (배포 뒤 소스가 바뀐 것)."""
    rev = AgentSpec.model_validate(spec)
    ctx.tool_pins = pins_of(rev, pins_from if pins_from is not None else ctx.source_tools)
    doc = agent_doc("ai-eval-0001", spec=spec, tenant=CTX_TENANT, name="evaluated")
    pinned = apply_pins(agent_graph.compile(doc, rev, ctx), rev, ctx.tool_pins, ctx.source_tools)
    return rev, pinned, hold_writes(pinned, rev, ctx)


def main_rules(document: dict[str, Any]) -> dict[str, set[str]]:
    """메인의 승인이다. 런타임의 `AgentConfig.approvals_for` 처럼 메인이 붙인 소스의 결정을 이름마다
    합집합으로 합친다."""
    out: dict[str, set[str]] = {}
    by_ref = {s["ref"]: s for s in document["tools"]}
    for ref in document["main"]["tools"]:
        for name, decisions in by_ref[ref]["approval"].items():
            out.setdefault(name, set()).update(decisions)
    return out


def assert_held(
    spec: AgentSpec, before: dict[str, Any], after: dict[str, Any], table: Table
) -> set[str]:
    """F0.2 의 막대. 잡힌 이름을 돌려준다."""
    source_of = {n.id: n.data["source"]["id"] for n in spec.graph.nodes if n.type == "tools"}
    held = {
        name
        for s in before["tools"]
        for name in s["allow"]
        if not known_read(table, source_of[s["ref"]], name)
    }
    original = {s["ref"]: s["approval"] for s in before["tools"]}
    offered = {s["ref"]: set(s["allow"]) for s in after["tools"]}
    for source in after["tools"]:
        for name in source["allow"]:
            if name in held:
                assert source["approval"].get(name) == list(HELD_DECISIONS), (source["ref"], name)
            else:
                assert source["approval"].get(name) == original[source["ref"]].get(name)
    agents = [("main", main_rules(after), after["main"]["tools"])]
    agents += [
        (sub["name"], {n: set(d) for n, d in sub["interrupt_on"].items()}, sub["tools"])
        for sub in after["subagents"]
    ]
    for name, rules, refs in agents:
        names = set().union(*(offered[ref] for ref in refs))
        for tool in held & names:
            assert rules.get(tool) == set(HELD_DECISIONS), (name, tool, rules.get(tool))
    return held


# -- D89 묶음 -------------------------------------------------------------------------------


def _valid(case: dict[str, Any]) -> bool:
    return not any(i.get("level", "error") == "error" for i in case["expect"])


def _has_tools(case: dict[str, Any]) -> bool:
    return any(n["type"] == "tools" for n in build_graph(case.get("graph", {}))["nodes"])


VALID_CASES = [c for c in CORPUS["cases"] if _valid(c)]


@pytest.mark.parametrize("case", VALID_CASES, ids=lambda c: c["name"])
def test_every_corpus_graph_holds_what_is_not_read(case: dict[str, Any]) -> None:
    spec, ctx = corpus_spec(case)
    table = corpus_table()
    ctx.source_tools = source_tools(table)
    rev, before, after = evaluate(spec, ctx)
    assert_held(rev, before, after, table)


@pytest.mark.parametrize("case", [c for c in VALID_CASES if _has_tools(c)], ids=lambda c: c["name"])
def test_a_corpus_graph_whose_source_names_are_unknown_is_refused(case: dict[str, Any]) -> None:
    spec, ctx = corpus_spec(case)
    with pytest.raises(ToolsUnknown) as refused:
        evaluate(spec, ctx)
    assert refused.value.refs


# -- 템플릿과 e2e 의 그래프 ---------------------------------------------------------------------


def e2e_spec(flags: str = "") -> dict[str, Any]:
    """e2e 가 저장하는 본문(`scripts/lib/agent_body.py` `build`)의 graph 와 service 다. flags 는
    e2e 스크립트가 주는 플래그를 띄어쓰기로 가른 것이다."""
    words = ["--name", "e2e", "--display", "e2e", "--provider", "local-stub", *flags.split()]
    args = agent_body.parser().parse_args(words)
    body = agent_body.build(copy.deepcopy(templates()[0] | {"layout": {}}), args)
    return {"graph": body["graph"], "service": body["service"]}


GW = f"--source gateway_instance:{CALC}"
E2E_GRAPHS = {
    "template": template_spec(source_id=CALC),
    "mcp server add with approval": e2e_spec(f"--ms-id {ORDERS} --approve --ds-id ds-pg"),
    "gateway with chosen tools": e2e_spec(f"{GW} --allow Add,Lookup --ds-id ds-pg"),
    "gateway, every tool": e2e_spec(f"{GW} --ds-id ds-pg"),
    "sub agent sharing the tools": e2e_spec(
        f"{GW} --sub-name calc --sub-tools --approval-for Add --ds-id ds-pg"
    ),
    "outside and rememberable": e2e_spec(f"{GW} --outside Lookup --rememberable Add --ds-id ds-pg"),
    "every tool, the sub agent's own node asks": e2e_spec(
        f"{GW} --every-tool --sub-name calc --ds-id ds-pg"
        f" --sub-approval gateway_instance:{CALC}:Add"
    ),
    "without memory": e2e_spec(GW),
}


@pytest.mark.parametrize("name", list(E2E_GRAPHS))
def test_every_e2e_graph_holds_what_is_not_read(name: str) -> None:
    ctx = make_ctx()
    ctx.source_tools = source_tools(TABLE)
    rev, before, after = evaluate(copy.deepcopy(E2E_GRAPHS[name]), ctx)
    assert_held(rev, before, after, TABLE)


def test_a_sub_agent_sharing_the_tools_holds_them_in_its_own_rules() -> None:
    """서브에이전트는 제 `interrupt_on` 을 쓴다. 노드의 승인(`--approval-for Add`)이 거기 실려
    있어도 잡힌 결정으로 바뀐다."""
    ctx = make_ctx()
    ctx.source_tools = source_tools(TABLE)
    _rev, before, after = evaluate(copy.deepcopy(E2E_GRAPHS["sub agent sharing the tools"]), ctx)
    [sub] = before["subagents"]
    assert sub["interrupt_on"]["Add"] == ["approve", "reject"]
    [sub] = after["subagents"]
    assert sub["interrupt_on"]["Add"] == list(HELD_DECISIONS)
    assert sub["interrupt_on"]["Divide"] == list(HELD_DECISIONS)


def test_an_e2e_graph_whose_gateway_names_are_unknown_is_refused() -> None:
    spec = e2e_spec(f"{GW} --ds-id ds-pg")
    node_of(spec, "tools-calculator")["data"]["allow_tools"] = []
    with pytest.raises(ToolsUnknown) as refused:
        evaluate(spec, make_ctx())
    assert refused.value.refs == ["tools-calculator"]


# -- 더한 그래프 --------------------------------------------------------------------------------


def two_sources() -> dict[str, Any]:
    """메인은 Gateway 를, 서브에이전트는 MCP Server 를 쓴다. Add 는 서버에서 읽기, Gateway 에서
    쓰기다."""
    spec = template_spec(source_id=CALC)
    tools = node_of(spec, "tools-calculator")["data"]
    tools["allow_tools"] = ["Add", "Lookup", "Rate", "Post", "fetch_url"]
    tools["approval"] = {"Lookup": ["approve", "reject"]}
    add_sub_agent(spec, "agent-orders", "orders")
    spec["graph"]["nodes"].append(
        {
            "id": "tools-orders",
            "type": "tools",
            "data": {
                "source": {"kind": "mcp_instance", "id": ORDERS},
                "allow_tools": ["GetOrder", "Add"],
                "approval": {"Add": ["approve", "edit", "reject"]},
                "timeouts": {},
            },
        }
    )
    spec["graph"]["edges"] += [
        {"id": "e-del", "source": "agent-main", "target": "agent-orders", "kind": "delegate"},
        {"id": "e-orders", "source": "agent-orders", "target": "tools-orders", "kind": "tools"},
    ]
    return spec


def _ctx(table: Table) -> GraphContext:
    ctx = make_ctx()
    ctx.source_tools = source_tools(table)
    return ctx


def test_a_name_read_in_one_source_and_written_in_another_is_held_in_both() -> None:
    rev, before, after = evaluate(two_sources(), _ctx(TABLE))
    held = assert_held(rev, before, after, TABLE)
    assert {"Add", "Post"} <= held and not held & {"Lookup", "Rate", "fetch_url", "GetOrder"}
    orders = next(s for s in after["tools"] if s["ref"] == "tools-orders")
    assert orders["approval"]["Add"] == ["respond", "reject"]
    [sub] = after["subagents"]
    assert sub["interrupt_on"]["Add"] == ["respond", "reject"]
    calc = next(s for s in after["tools"] if s["ref"] == "tools-calculator")
    # 읽기는 노드의 승인을 그대로 둔다
    assert calc["approval"]["Lookup"] == ["approve", "reject"]


def test_a_canvas_read_mark_does_not_make_a_write_read() -> None:
    spec = two_sources()
    node_of(spec, "tools-calculator")["data"]["read_only"] = ["Post", "Add"]
    rev, before, after = evaluate(spec, _ctx(TABLE))
    assert {"Post", "Add"} <= assert_held(rev, before, after, TABLE)


def test_a_chosen_tool_missing_from_its_source_is_held() -> None:
    """핀할 때는 읽기였던 Lookup 이 지금 소스에 없다. 런타임은 실행 중에 서버가 내면 부르므로
    읽기라고 알 수 없다."""
    spec = template_spec(source_id=CALC)
    node_of(spec, "tools-calculator")["data"]["allow_tools"] = ["Lookup", "Rate"]
    gone = {CALC: {k: v for k, v in TABLE[CALC].items() if k != "Lookup"}}
    rev, before, after = evaluate(spec, _ctx(gone), pins_from=source_tools(TABLE))
    assert assert_held(rev, before, after, gone) == {"Lookup"}


def test_names_of_unknown_origin_are_held() -> None:
    """백엔드 종류를 모르는 Gateway(이름을 모르는 백엔드가 있다)의 읽기는 읽기라고 알 수 없다."""
    spec = template_spec(source_id=CALC)
    node_of(spec, "tools-calculator")["data"]["allow_tools"] = ["Lookup"]
    unknown: Table = {CALC: {"Lookup": (EFFECT_READ, None)}}
    rev, before, after = evaluate(spec, _ctx(unknown))
    assert assert_held(rev, before, after, unknown) == {"Lookup"}


def test_a_name_pinned_as_web_that_is_now_a_write_is_held() -> None:
    """배포 때 웹 Tool 이던 fetch_url 이 지금은 `remote` 백엔드의 쓰기다. 핀과 지금 가운데 센 쪽은
    웹이지만, 지금 소스가 주는 효과로 가른다."""
    spec = template_spec(source_id=CALC)
    node_of(spec, "tools-calculator")["data"]["allow_tools"] = ["fetch_url"]
    now: Table = {CALC: {"fetch_url": (EFFECT_WRITE, "remote")}}
    rev, before, after = evaluate(spec, _ctx(now), pins_from=source_tools(TABLE))
    assert assert_held(rev, before, after, now) == {"fetch_url"}
