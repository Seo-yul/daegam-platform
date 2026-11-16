"""그래프 전체의 웹 Tool 결정 (P39b 14절 2·4·5부). 데이터 Tool 판정, 읽기 전용 표시, 소스의 `web`
블록, 웹 Tool 이 있는 그래프의 검증과 문서 기능 `web_guard` 를 든다.
"""

from __future__ import annotations

from typing import Any

from daegam.services import agent_features
from daegam.services.agent_features import TOOL_SAFETY, WEB_GUARD, finish_document
from daegam.services.agent_graph import validate
from daegam.services.agent_web import (
    attach_web,
    uses_web,
    web_issues,
)
from daegam.services.tool_effects import (
    EFFECT_READ,
    EFFECT_WEB,
    EFFECT_WRITE,
    WEB_FETCH,
    WEB_SEARCH,
    SourceTools,
    gateway_source,
    graph_tools,
)
from daegam.services.tool_pins import pins_of
from daegam.store.agents import AgentSpec, ToolPin
from daegam.store.gateways import Gateway
from daegam.store.settings import WebToolsSettings
from tests.helpers.agents import (
    add_sub_agent,
    make_ctx,
    node_of,
    pinned_document,
    template_spec,
    without_memory,
)
from tests.helpers.docs import agent_doc
from tests.helpers.gateways import backend_snapshot

GW = "gi-calc-0001"
NODE = "tools-calculator"
WEB_SOURCE = SourceTools(
    names=("Add", "Divide", "web_fetch", "web_search"),
    effects={
        "Add": EFFECT_WRITE,
        "Divide": EFFECT_WRITE,
        "web_fetch": EFFECT_WEB,
        "web_search": EFFECT_WEB,
    },
    web={"web_fetch": WEB_FETCH, "web_search": WEB_SEARCH},
)
ONLY_WEB = SourceTools(
    names=("web_fetch",), effects={"web_fetch": EFFECT_WEB}, web={"web_fetch": WEB_FETCH}
)


def _ctx(source: SourceTools = WEB_SOURCE, **web: Any):
    """웹 Tool 소스의 문맥이다."""
    ctx = make_ctx()
    ctx.source_tools = {GW: source}
    ctx.web_tools = WebToolsSettings.model_validate(
        {"fetch": {"blocked_host_suffixes": ["corp.example"], **web}}
    )
    return ctx


def _spec(**tools: Any) -> dict[str, Any]:
    """고른 Tool 이 없는 템플릿이다. tools 는 그 tools 노드의 data 에 덮는다."""
    spec = template_spec()
    data = node_of(spec, NODE)["data"]
    data.update({"allow_tools": [], "approval": {}, "timeouts": {}, **tools})
    return spec


def _issues(spec: dict[str, Any], ctx=None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for issue in web_issues(AgentSpec.model_validate(spec), ctx or _ctx()):
        out.setdefault(issue.code, []).append(issue.to_dict())
    return out


def _final(spec: dict[str, Any], ctx) -> dict[str, Any]:
    """`web_guard` 로 붙인 최종 문서다(스키마 검사 포함)."""
    rev, document = pinned_document(spec, ctx)
    return finish_document(document, frozenset({WEB_GUARD}), ctx, rev)


def test_web_tools_add_the_web_block() -> None:
    final = _final(_spec(allow_domains=["docs.example.com"]), _ctx())
    [source] = final["tools"]
    assert final["features"] == [WEB_GUARD] and final["web_data_tools"] is True
    assert source["allow"] == ["Add", "Divide", "web_fetch", "web_search"]
    assert source["approval"] == {}
    assert source["web"] == {
        "fetch": "web_fetch",
        "search": "web_search",
        "allow_domains": ["docs.example.com"],
        "block_domains": ["corp.example"],
        "per_user": {
            "fetch_per_minute": 10,
            "fetch_per_day": 200,
            "search_per_minute": 10,
            "search_per_day": 200,
        },
    }


def test_read_only_marks_and_hand_set_approvals_are_kept() -> None:
    spec = _spec(read_only=["Add"], approval={"Divide": ["approve", "reject"]})
    [source] = _final(spec, _ctx())["tools"]
    assert source["approval"] == {"Divide": ["approve", "reject"]}
    effects = graph_tools(AgentSpec.model_validate(spec), _ctx().source_tools).effects
    assert effects[NODE] == {
        "Add": EFFECT_READ,
        "Divide": EFFECT_WRITE,
        "web_fetch": EFFECT_WEB,
        "web_search": EFFECT_WEB,
    }


def test_a_graph_of_web_tools_only_holds_no_data() -> None:
    """웹 Tool 만 있고 Knowledge·장기 기억이 없으면 데이터 Tool 이 없다. 첨부가 있는 대화만 허용
    목록으로 좁혀진다."""
    ctx = _ctx(ONLY_WEB)
    final = _final(_spec(), ctx)
    assert final["web_data_tools"] is False and final["tools"][0]["approval"] == {}
    [warning] = _issues(_spec(), ctx)["web_fetch_allowlist_empty"]
    assert warning["level"] == "warning" and "attachments" in warning["message"]


def test_web_graphs_warn_about_an_empty_allow_list() -> None:
    issues = _issues(_spec())
    assert "every conversation" in issues["web_fetch_allowlist_empty"][0]["message"]
    assert set(issues) == {"web_fetch_allowlist_empty"}


def test_web_graphs_ask_after_outside_content_instead_of_a_default_approval() -> None:
    """웹 Tool 곁의 write Tool 에는 기본 승인이 없고, memory 노드가 없어도 그 이유로 막히지 않는다.
    바깥 내용 뒤의 승인 규칙이 그 쓰기를 묻는다(P43 4절)."""
    spec = _spec()
    alone = AgentSpec.model_validate(without_memory(_spec()))
    ctx = _ctx()
    assert not any("web default approval" in i.message for i in validate(alone, ctx))
    rev, document = pinned_document(spec, ctx)
    final = finish_document(document, frozenset({WEB_GUARD, TOOL_SAFETY}), ctx, rev)
    [source] = final["tools"]
    assert final["features"] == sorted([WEB_GUARD, TOOL_SAFETY])
    assert source["approval"] == {}
    # 웹 Tool 은 읽기이고 바깥 내용이다. 백엔드를 모르는 이름은 바깥이고 쓰기다
    assert source["read"] == ["web_fetch", "web_search"] and source["inside"] == []


def test_unknown_names_beside_web_tools_are_an_error() -> None:
    spec = _spec()
    add_sub_agent(spec, "agent-sub", "researcher")
    spec["graph"]["nodes"].append(
        {
            "id": "tools-dyn",
            "type": "tools",
            "data": {"source": {"kind": "mcp_instance", "id": "mi-orders-0001"}},
        }
    )
    edges = spec["graph"]["edges"]
    edges.append({"id": "e-del", "source": "agent-main", "target": "agent-sub", "kind": "delegate"})
    edges.append({"id": "e-dyn", "source": "agent-sub", "target": "tools-dyn", "kind": "tools"})
    [issue] = _issues(spec)["web_tools_unknown_names"]
    assert issue["node_id"] == "tools-dyn"


def test_two_web_sources_of_one_agent_may_not_share_a_name() -> None:
    spec = _spec()
    second = {**node_of(spec, NODE), "id": "tools-second"}
    spec["graph"]["nodes"].append(second)
    spec["graph"]["edges"].append(
        {"id": "e-second", "source": "agent-main", "target": "tools-second", "kind": "tools"}
    )
    codes = [i["node_id"] for i in _issues(spec)["duplicate_tool_name"]]
    assert codes == ["tools-second", "tools-second"]


def test_a_web_source_of_unknown_names_is_an_error() -> None:
    """이름을 모르는 소스도 아는 웹 Tool 이 있으면(게시 항목을 거친 웹 Tool) 웹 노드다."""
    source = SourceTools(names=None, web={"web_fetch": WEB_FETCH})
    [issue] = _issues(_spec(), _ctx(source))["web_tools_unknown_names"]
    assert issue["node_id"] == NODE


def _chain(published: list[str], **extra: Any) -> SourceTools:
    """게시 항목 아래에 web_fetch 를 가진 Gateway 위의 Gateway 다. 이름을 모르는 remote 백엔드와,
    published 를 스냅샷 이름으로 든 게시 백엔드를 배포했다(probe_holes2 의 두 단 사슬)."""
    gw = Gateway.model_construct(
        deployed_revision=1,
        deployed_backends=[
            backend_snapshot("r", "remote", []),
            backend_snapshot("g", "published", published, hub_item_id="pub-g", **extra),
        ],
        deployed_conflict_resolution="prefix",
        tool_overrides={},
    )
    below = SourceTools(
        names=("Add", "web_fetch"),
        effects={"Add": EFFECT_WRITE, "web_fetch": EFFECT_WEB},
        web={"web_fetch": WEB_FETCH},
    )
    return gateway_source(gw, servers={}, published={"pub-g": below})


def test_a_stale_published_snapshot_still_counts_as_a_web_source() -> None:
    """위 Gateway 의 스냅샷이 아래 Gateway 가 web_fetch 를 얻기 전의 것이어도, 필터 없는 게시
    백엔드는 원본이 지금 광고하는 것을 낸다. 그래서 이름을 모르는 웹 소스이고 검증이 막는다.
    스냅샷이 web_fetch 를 알아도 같다. 필터가 있으면 이름을 안다."""
    for names in (["Add"], ["Add", "web_fetch"]):
        source = _chain(names)
        assert source.names is None and source.web == {"gw-g_web_fetch": WEB_FETCH}
        [issue] = _issues(_spec(), _ctx(source))["web_tools_unknown_names"]
        assert issue["node_id"] == NODE
    filtered = _chain(["Add", "web_fetch"], tools=["Add"])
    assert filtered.web == {} and "web_tools_unknown_names" not in _issues(_spec(), _ctx(filtered))


def test_domain_lists_refuse_public_suffixes_in_any_graph() -> None:
    spec = _spec(allow_domains=["co.kr"], block_domains=["not a domain"])
    issues = _issues(spec, _ctx(SourceTools(names=("Add",), effects={"Add": EFFECT_READ})))
    assert [i["node_id"] for i in issues["domain_is_public_suffix"]] == [NODE]
    assert [i["node_id"] for i in issues["invalid_value"]] == [NODE]


def test_domain_lists_need_two_labels_a_name_and_at_most_a_hundred_values() -> None:
    """한 라벨, 마지막 라벨이 숫자뿐인 값(IP 주소 모양), 100 개를 넘는 목록은 invalid_value 다."""
    plain = _ctx(SourceTools(names=("Add",), effects={"Add": EFFECT_READ}))
    for value in ("intranet", "10.0.0.1", "example.123"):
        assert set(_issues(_spec(allow_domains=[value]), plain)) == {"invalid_value"}, value
    many = [f"d{n}.example.com" for n in range(101)]
    [issue] = _issues(_spec(block_domains=many), plain)["invalid_value"]
    assert "more than 100" in issue["message"]
    assert _issues(_spec(block_domains=many[:100]), plain) == {}


def test_web_guard_is_required_only_when_a_pinned_tool_is_web() -> None:
    rev = AgentSpec.model_validate(_spec())
    doc = agent_doc("ag-1", spec=_spec(), state="deployed")
    ctx = _ctx()
    pins = pins_of(rev, ctx.source_tools)
    assert uses_web(rev, pins, ctx.source_tools)
    assert not uses_web(rev, {NODE: ToolPin(names=["Add"])}, ctx.source_tools), "핀에 웹이 없다"
    gone = {NODE: ToolPin(names=["Add", "web_fetch"], web={"web_fetch": WEB_FETCH})}
    plain = _ctx(SourceTools(names=("Add",), effects={"Add": EFFECT_WRITE}))
    assert uses_web(rev, gone, plain.source_tools), "핀한 웹 Tool 이 사라져도 웹 그래프다"
    wanted = agent_features.agent_features(doc, rev, ctx.providers, web=True)
    assert WEB_GUARD in wanted.required


def test_attach_web_leaves_the_input_alone() -> None:
    ctx = _ctx()
    rev, document = pinned_document(_spec(), ctx)
    before = repr(document)
    attach_web(document, rev, ctx)
    assert repr(document) == before
