"""P48 6.2~6.4절: 제안 패치의 짜 맞추기, 정의 개요, 넘지 못하는 선과 사람만 다는 표시, 비교와 먼저
볼 것.

DB 없이 순수 함수만 본다. 기준은 템플릿 "단일 상담 에이전트" 를 채운 spec 이다.
"""

from __future__ import annotations

import copy

import pytest

from daegam.services.agent_changes import (
    DIFF_LINE_CHARS,
    DIFF_LINES,
    PatchError,
    apply_patch,
    attention,
    attention_key,
    compare,
    node_of,
    outline,
    person_marks,
    refusals,
    text_diff,
)
from daegam.services.assistant.proposals import PROPOSAL_MAX_BYTES as CAP
from daegam.store.agents import AgentSpec
from tests.helpers.agents import node_of as spec_node
from tests.helpers.agents import template_spec


def _base(**tools_data) -> AgentSpec:
    spec = template_spec()
    spec_node(spec, "tools-calculator")["data"].update(tools_data)
    return AgentSpec.model_validate(spec)


def _codes(e: pytest.ExceptionInfo[PatchError]) -> list[str]:
    return [i["code"] for i in e.value.issues]


# -- 짜 맞추기 -------------------------------------------------------------------------------


def test_patch_keeps_untouched_nodes_and_applies_each_part() -> None:
    base = _base()
    prompt = "x" * 3000
    agent = copy.deepcopy(spec_node(base.model_dump(), "agent-main"))
    agent["data"]["system_prompt"] = prompt
    composed, touched = apply_patch(
        base,
        {
            "set_nodes": [
                agent,
                {"id": "kb", "type": "knowledge", "data": {"knowledge_base_id": "kb-1"}},
            ],
            "remove_nodes": ["guardrail-input"],
            "add_edges": [{"source": "agent-main", "target": "kb", "kind": "knowledge"}],
            "remove_edges": [{"source": "agent-main", "target": "memory", "kind": "memory"}],
            "set_service": {"budget": {"session_tokens": 10}},
        },
        node_cap=CAP,
    )
    assert touched == {"agent-main", "kb"}
    ids = [n.id for n in composed.graph.nodes]
    # 기준 순서를 지키고 더한 노드는 뒤에 붙는다. 지운 노드와 그 간선은 빠진다
    assert ids == ["entry", "agent-main", "model-main", "tools-calculator", "memory", "kb"]
    before = {n.id: n for n in base.graph.nodes}
    for node in composed.graph.nodes:
        if node.id not in touched:
            assert node == before[node.id]
    assert spec_node(composed.model_dump(), "agent-main")["data"]["system_prompt"] == prompt
    edges = {(e.source, e.target, e.kind): e.id for e in composed.graph.edges}
    assert ("agent-main", "guardrail-input", "guardrail") not in edges
    assert ("agent-main", "memory", "memory") not in edges
    # 간선 id 는 캔버스와 같은 꼴로 짓는다
    assert edges[("agent-main", "kb", "knowledge")] == "e-agent-main-kb"
    assert composed.service.budget.session_tokens == 10
    assert composed.service.observability == base.service.observability


def test_patch_reports_every_problem_at_once() -> None:
    base = _base()
    spec_big = base.model_dump()
    spec_node(spec_big, "agent-main")["data"]["system_prompt"] = "y" * (CAP + 1)
    big = AgentSpec.model_validate(spec_big)
    with pytest.raises(PatchError) as e:
        apply_patch(
            big,
            {
                "set_nodes": [
                    {"id": "agent-main", "type": "agent", "data": {"role": "main", "name": "A"}},
                    {"id": "bad", "type": "nope"},
                ],
                "remove_nodes": ["ghost"],
                "remove_edges": [{"source": "entry", "target": "model-main", "kind": "entry"}],
                "add_edges": [{"source": "entry", "target": "agent-main", "kind": "entry"}],
                "set_service": {"colour": "red"},
            },
            node_cap=CAP,
        )
    assert _codes(e) == [
        "node_too_large",
        "invalid_node",
        "unknown_node",
        "unknown_edge",
        "edge_exists",
        "invalid_service",
    ]
    with pytest.raises(PatchError) as e:
        apply_patch(base, {"set_nodes": "x"}, node_cap=CAP)
    assert _codes(e) == ["invalid_patch"]


# -- 읽기 ------------------------------------------------------------------------------------


def test_outline_names_what_a_patch_needs_without_the_texts() -> None:
    spec = template_spec()
    agent = {"id": "a1", "name": "support", "display_name": "상담", "revision": 3, **spec}
    out = outline(agent)
    assert out["revision"] == 3 and out["edges"][0]["kind"] == "entry"
    nodes = {n["id"]: n for n in out["nodes"]}
    main = nodes["agent-main"]
    assert main["role"] == "main" and main["name"] == "Agent"
    assert set(main["text_chars"]) == {"system_prompt", "description"}
    assert "system_prompt" not in main and main["bytes"] > 0
    assert nodes["tools-calculator"]["tools"] == ["Add", "Divide"]
    assert nodes["memory"]["data_service_id"] == "ds-pg"
    assert nodes["model-main"]["provider"] == "local-stub"
    assert node_of(agent, "agent-main")["data"]["role"] == "main"
    assert node_of(agent, "ghost") is None


# -- 사람만 다는 표시 -------------------------------------------------------------------------


def _with_tools(spec: AgentSpec, **data) -> AgentSpec:
    out = spec.model_dump()
    spec_node(out, "tools-calculator")["data"].update(data)
    return AgentSpec.model_validate(out)


def test_marks_are_compared_by_what_they_bind_to() -> None:
    base = _base(read_only=["Divide"], rememberable=["Add"])
    # 같은 노드 id 에서 표시를 지키면 넘는 것이 없다. 더 엄격해지는 것도 된다
    assert person_marks(base, base, {}) == []
    assert person_marks(base, _with_tools(base, read_only=[], rememberable=[]), {}) == []
    # 새로 단 표시는 자리와 함께 나온다
    looser = _with_tools(base, read_only=["Divide", "Add"], outside={"Add": False})
    assert person_marks(base, looser, {}) == [
        {"node_id": "tools-calculator", "mark": "read_only", "tool": "Add"},
        {"node_id": "tools-calculator", "mark": "outside_false", "tool": "Add"},
    ]
    # 노드 id 를 지켜도 소스를 바꾸면 표시를 데려가지 못한다
    swapped = _with_tools(base, source={"kind": "mcp_instance", "id": "other"})
    marks = {m["mark"] for m in person_marks(base, swapped, {})}
    assert marks == {"read_only", "rememberable"}
    # 노드 id 가 달라도 같은 소스면 표시는 그대로다
    moved = base.model_dump()
    spec_node(moved, "tools-calculator")["id"] = "tools-2"
    assert person_marks(base, AgentSpec.model_validate(moved), {}) == []


def test_knowledge_mark_follows_the_knowledge_base() -> None:
    def with_kb(kb_id: str, outside: bool) -> AgentSpec:
        spec = template_spec()
        spec["graph"]["nodes"].append(
            {
                "id": "kb",
                "type": "knowledge",
                "data": {"knowledge_base_id": kb_id, "outside": outside},
            }
        )
        return AgentSpec.model_validate(spec)

    base = with_kb("kb-1", False)
    assert person_marks(base, base, {}) == []
    assert person_marks(base, with_kb("kb-2", False), {}) == [
        {"node_id": "kb", "mark": "outside_false", "knowledge_base_id": "kb-2"}
    ]
    assert person_marks(base, with_kb("kb-1", True), {}) == []
    # 만들기는 기준이 없어 모든 표시가 새것이다
    assert person_marks(None, base, {})[0]["knowledge_base_id"] == "kb-1"


def test_removing_a_persons_outside_true_loosens_only_an_inside_tool() -> None:
    base = _base(outside={"Add": True})
    dropped = _with_tools(base, outside={})
    internal = {"tools-calculator": {"Add": "internal"}}
    assert person_marks(base, dropped, internal) == [
        {"node_id": "tools-calculator", "mark": "outside_true_removed", "tool": "Add"}
    ]
    # 바깥이 기본인 Tool 은 거둬도 그대로 바깥이다
    assert person_marks(base, dropped, {"tools-calculator": {"Add": "remote"}}) == []
    # 백엔드 종류를 아직 모르면 안쪽일 수 있으므로 거두지 못한다
    assert person_marks(base, dropped, {})[0]["mark"] == "outside_true_removed"
    # 그 Tool 을 더는 쓰지 않으면 거둔 것이 아니다
    unused = _with_tools(base, outside={}, allow_tools=["Divide"])
    assert person_marks(base, unused, internal) == []


# -- 넘지 못하는 선 --------------------------------------------------------------------------


def test_refusals_hold_errors_unknown_keys_evaluation_and_empty_tool_lists() -> None:
    base = _base(future_mark=["Add"])
    issues = [
        {"code": "unknown_provider", "level": "error", "node_id": "model-main", "message": "m"},
        {"code": "unattached_node", "level": "warning", "node_id": "x", "message": "w"},
    ]
    # 기준 노드에 있던 모르는 키는 그대로 둔다. 건드리지 않은 노드는 보지 않는다
    problems, marks = refusals(base, base, {"tools-calculator"}, issues=issues, origins={})
    assert [p["code"] for p in problems] == ["unknown_provider"] and marks == []
    planted = _with_tools(base, other_mark=["Add"], allow_tools=[])
    out = planted.model_dump()
    out["service"]["evaluation"] = {"gate": "off"}
    planted = AgentSpec.model_validate(out)
    problems, _ = refusals(base, planted, {"tools-calculator"}, issues=[], origins={})
    assert [(p["code"], p["node_id"]) for p in problems] == [
        ("unknown_node_keys", "tools-calculator"),
        ("empty_allow_tools", "tools-calculator"),
        ("evaluation_changed", None),
    ]
    assert "other_mark" in problems[0]["message"] and "future_mark" not in problems[0]["message"]
    problems, _ = refusals(base, planted, set(), issues=[], origins={})
    assert [p["code"] for p in problems] == ["evaluation_changed"]


def test_a_key_a_later_release_stored_stays_only_as_it_was() -> None:
    """뒤 판이 저장한 키는 같은 종류·같은 소스·같은 값일 때만 남는다. 값이나 소스를 바꾸면 뒤 판이
    읽을 표시를 심을 수 있다 (6.2절 4)."""
    base = _base(future_mark=["Add"])

    def unknown(composed: AgentSpec) -> list[str]:
        problems, _ = refusals(base, composed, {"tools-calculator"}, issues=[], origins={})
        return [p["code"] for p in problems if p["code"] == "unknown_node_keys"]

    assert unknown(_with_tools(base, allow_tools=["Add"])) == []
    assert unknown(_with_tools(base, future_mark=["Add", "Divide"])) == ["unknown_node_keys"]
    swapped = _with_tools(base, source={"kind": "mcp_instance", "id": "other"})
    assert unknown(swapped) == ["unknown_node_keys"]


# -- 비교 (6.4절) ---------------------------------------------------------------------------


def _changed(spec: AgentSpec, node_id: str, **data) -> AgentSpec:
    out = spec.model_dump()
    spec_node(out, node_id)["data"].update(data)
    return AgentSpec.model_validate(out)


def test_compare_lists_changes_by_node_id_with_bounded_text_diffs() -> None:
    base = _base()
    prompt = "\n".join(f"줄 {i}" for i in range(50))
    composed, _ = apply_patch(
        _changed(base, "agent-main", system_prompt=prompt, name="상담"),
        {
            "remove_nodes": ["guardrail-input"],
            "set_nodes": [{"id": "kb", "type": "knowledge", "data": {"knowledge_base_id": "k"}}],
            "add_edges": [{"source": "agent-main", "target": "kb", "kind": "knowledge"}],
            "set_service": {"observability": {"record_bodies": True}},
        },
        node_cap=CAP,
    )
    out = compare(base, composed, display_before="상담", display_after="상담 봇")
    assert out["nodes"]["added"] == [{"id": "kb", "type": "knowledge"}]
    assert out["nodes"]["removed"] == [{"id": "guardrail-input", "type": "guardrail"}]
    assert out["nodes"]["changed"] == [
        {"id": "agent-main", "type": "agent", "name": "상담", "fields": ["name", "system_prompt"]}
    ]
    assert out["edges"]["added"] == [{"source": "agent-main", "target": "kb", "kind": "knowledge"}]
    assert out["edges"]["removed"] == [
        {"source": "agent-main", "target": "guardrail-input", "kind": "guardrail"}
    ]
    assert out["service"] == ["observability"]
    assert out["display_name"] == {"before": "상담", "after": "상담 봇"}
    (text,) = [t for t in out["texts"] if t["node_id"] == "agent-main"]
    assert text["field"] == "system_prompt" and text["cut"] is True
    assert len(text["lines"]) == DIFF_LINES and text["lines"][0]["op"] == "-"
    # 바뀌지 않았으면 글 비교도 표시 이름도 없다
    same = compare(base, base, display_before="a", display_after="a")
    assert same["texts"] == [] and "display_name" not in same
    assert same["nodes"] == {"added": [], "changed": [], "removed": []}


def test_text_diff_marks_long_lines_and_keeps_both_sides() -> None:
    out = text_diff("하나\n둘", "하나\n" + "셋" * 300)
    assert out["cut"] is True
    assert [line["op"] for line in out["lines"]] == ["-", "+"]
    assert out["lines"][0]["text"] == "둘" and len(out["lines"][1]["text"]) == DIFF_LINE_CHARS
    assert text_diff("a", "a") == {"lines": [], "cut": False}


def test_a_creation_adds_every_node() -> None:
    spec = AgentSpec.model_validate(template_spec())
    out = compare(None, spec, display_after="상담")
    assert [n["id"] for n in out["nodes"]["added"]] == [n.id for n in spec.graph.nodes]
    assert {t["field"] for t in out["texts"]} == {
        "system_prompt",
        "description",
        "greeting",
        "message",
    }
    assert out["display_name"] == {"before": None, "after": "상담"}


# -- 먼저 볼 것 (6.4절) ------------------------------------------------------------------------

WRITE = {"tools-calculator": {"Add": "write", "Divide": "write"}}


def _attention_codes(base, composed, effects=WRITE) -> list[tuple[str, str | None]]:
    return [(m["code"], m.get("node_id")) for m in attention(base, composed, effects)]


def test_attention_of_a_creation() -> None:
    spec = AgentSpec.model_validate(template_spec())
    marks = attention(None, spec, WRITE)
    assert [m["code"] for m in marks] == [
        "system_prompt_changed",
        "tool_source_added",
        "write_without_approval",
        "service_changed",
    ]
    assert marks[0]["agent"] == "Agent"
    assert marks[1]["source"] == {"kind": "gateway_instance", "id": "gi-calc-0001"}
    # Divide 는 승인이 있다
    assert marks[2]["tool"] == "Add"
    assert marks[3]["fields"] == ["budget"]


def test_attention_is_empty_for_no_change_and_names_each_risky_change() -> None:
    base = _base(allow_domains=["a.example"], block_domains=["b.example"])
    assert attention(base, base, WRITE) == []
    cases = {
        "approval_removed": _changed(base, "tools-calculator", approval={}),
        "web_reach_widened": _changed(base, "tools-calculator", block_domains=[]),
        "rate_limit_removed": _changed(base, "entry", rate_limit={"per_user_per_minute": 99}),
        "guardrail_weakened": _changed(base, "guardrail-input", on_violation="flag"),
        "code_execution": _changed(base, "agent-main", code_execution={"approval": []}),
        "skill_attached": _changed(base, "agent-main", skills=[{"id": "s1", "revision": 2}]),
        "memory_changed": _changed(base, "memory", long_term={"retention_days": 7}),
    }
    for code, composed in cases.items():
        assert code in [c for c, _ in _attention_codes(base, composed)], code
    # 허용 목록을 비우면 어디든 가므로 넓히는 것이다. 막는 곳을 더하는 것은 넓히는 것이 아니다
    emptied = _changed(base, "tools-calculator", allow_domains=[], block_domains=["b.example"])
    assert ("web_reach_widened", "tools-calculator") in _attention_codes(base, emptied)
    stricter = _changed(base, "tools-calculator", block_domains=["b.example", "c.example"])
    assert _attention_codes(base, stricter) == []


def test_attention_for_models_knowledge_guardrail_rules_and_retention() -> None:
    base = _base()
    model = _changed(base, "model-main", provider="local-anthropic", name="다른 이름")
    (mark,) = attention(base, model, WRITE)
    assert mark == {
        "code": "model_changed",
        "node_id": "agent-main",
        "agent": "Agent",
        "provider": "local-anthropic",
        "model": None,
    }
    # 표시 이름만 바꾼 모델 노드는 먼저 볼 것이 아니다
    assert attention(base, _changed(base, "model-main", name="새 이름"), WRITE) == []
    kb, _ = apply_patch(
        base,
        {
            "set_nodes": [{"id": "kb", "type": "knowledge", "data": {"knowledge_base_id": "k"}}],
            "add_edges": [{"source": "agent-main", "target": "kb", "kind": "knowledge"}],
        },
        node_cap=CAP,
    )
    assert attention(base, kb, WRITE) == [
        {"code": "knowledge_attached", "node_id": "kb", "knowledge": {"id": "k"}}
    ]
    rule = _changed(base, "guardrail-input", rules=[{"kind": "max_length", "value": 9000}])
    assert _attention_codes(base, rule) == [("guardrail_weakened", "guardrail-input")]
    unhooked, _ = apply_patch(
        base,
        {
            "remove_edges": [
                {"source": "agent-main", "target": "guardrail-input", "kind": "guardrail"}
            ]
        },
        node_cap=CAP,
    )
    assert ("guardrail_weakened", "guardrail-input") in _attention_codes(base, unhooked)
    kept = _changed(base, "memory", long_term={"retention_days": 30})
    assert attention(kept, _changed(base, "memory", long_term={"retention_days": 7}), WRITE) == []
    assert _attention_codes(kept, _changed(base, "memory", long_term={})) == [
        ("memory_changed", "memory")
    ]


def test_write_tools_are_named_only_where_the_proposal_changed_them() -> None:
    base = _base(approval={})
    # 손대지 않은 노드의 승인 없는 쓰기 Tool 은 바뀐 것이 아니다
    assert attention(base, base, WRITE) == []
    # 같은 소스에서 이미 승인 없이 쓰던 Tool 도 그렇다. 새로 고른 Tool 은 이름이 나온다
    picked = _changed(base, "tools-calculator", allow_tools=["Add", "Divide", "Multiply"])
    effects = {"tools-calculator": {**WRITE["tools-calculator"], "Multiply": "write"}}
    marks = attention(base, picked, effects)
    assert [(m["code"], m.get("tool")) for m in marks] == [("write_without_approval", "Multiply")]
    # MCP 프로필이 받지 않는 꼴의 이름은 싣지 않는다
    odd = attention(
        None, AgentSpec.model_validate(template_spec()), {"tools-calculator": {"x y": "write"}}
    )
    assert {"code": "write_without_approval", "node_id": "tools-calculator"} in odd
    assert attention_key(marks[0]) == attention_key(dict(reversed(list(marks[0].items()))))


def _with_node(spec: dict, node: dict, agent: str | None = "agent-main") -> AgentSpec:
    spec["graph"]["nodes"].append(node)
    if agent is not None:
        edge = {"id": f"e-{node['id']}", "source": agent, "target": node["id"], "kind": "tools"}
        spec["graph"]["edges"].append(edge)
    return AgentSpec.model_validate(spec)


def test_attaching_an_existing_tools_node_counts_as_a_new_source() -> None:
    mail = {
        "id": "tools-mail",
        "type": "tools",
        "data": {"source": {"kind": "mcp_instance", "id": "mail"}, "allow_tools": ["send_email"]},
    }
    base = _with_node(template_spec(), mail, agent=None)
    attached, _ = apply_patch(
        base,
        {"add_edges": [{"source": "agent-main", "target": "tools-mail", "kind": "tools"}]},
        node_cap=CAP,
    )
    effects = {**WRITE, "tools-mail": {"send_email": "write"}}
    assert _attention_codes(base, attached, effects) == [
        ("tool_source_added", "tools-mail"),
        ("write_without_approval", "tools-mail"),
    ]


def test_a_web_tool_joining_a_node_widens_web_reach() -> None:
    web = {
        "id": "tools-web",
        "type": "tools",
        "data": {"source": {"kind": "gateway_instance", "id": "gw"}, "allow_tools": ["search"]},
    }
    base = _with_node(template_spec(), web)
    effects = {**WRITE, "tools-web": {"search": "web", "fetch": "web"}}
    assert attention(base, base, effects) == []
    joined = _changed(base, "tools-web", allow_tools=["search", "fetch"])
    assert _attention_codes(base, joined, effects) == [("web_reach_widened", "tools-web")]
    # 웹 Tool 이 있는 노드를 새로 붙여도 넓힌 것이다
    fresh = _with_node(template_spec(), web)
    assert ("web_reach_widened", "tools-web") in _attention_codes(
        AgentSpec.model_validate(template_spec()), fresh, effects
    )


def test_approvals_are_compared_per_node_and_a_lowered_skill_pin_counts() -> None:
    spec = template_spec()
    second = copy.deepcopy(spec_node(spec, "tools-calculator"))
    second["id"] = "tools-calc-2"
    base = _with_node(spec, second)
    read = {
        "tools-calculator": {"Add": "read", "Divide": "read"},
        "tools-calc-2": {"Add": "read", "Divide": "read"},
    }
    # 같은 소스의 두 노드 가운데 하나에서만 승인을 없애도 그 노드의 승인이 사라진 것이다
    dropped = _changed(base, "tools-calc-2", approval={})
    assert _attention_codes(base, dropped, read) == [("approval_removed", "tools-calc-2")]
    pinned = _changed(base, "agent-main", skills=[{"id": "s1", "revision": 3}])
    lowered = _changed(pinned, "agent-main", skills=[{"id": "s1", "revision": 1}])
    skill = [m for m in attention(pinned, lowered, read) if m["code"] == "skill_attached"]
    assert skill == [
        {"code": "skill_attached", "node_id": "agent-main", "skill": {"id": "s1", "revision": 1}}
    ]
