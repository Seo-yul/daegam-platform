"""게시한 Agent 를 Tool 로 쓰는 tools 노드 (P46 6.2절, 6.6절의 표시).

앞쪽은 문맥을 손으로 채워 규칙, 효과와 표시, 핀, 컴파일을 본다. 뒤쪽은 테스트 Mongo 위의
플랫폼에서 게시한 Agent 를 다른 Agent 의 캔버스에 붙여 저장, 배포, init 받기, 상태를 본다. header
모드라 요청한 사람은 플랫폼 관리자인 `header` 다.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from daegam.services import agent_features, agent_graph
from daegam.services.agent_calls import (
    CALL_GRACE_SECONDS,
    AgentSource,
    Refusal,
    addressed,
    call_issues,
    call_url,
    source_tools,
    status_warnings,
    tool_name,
)
from daegam.services.agent_features import AGENT_TOOLS, TOOL_SAFETY, finish_document
from daegam.services.agent_graph import GraphContext, GraphError
from daegam.services.agent_tool_safety import safety_issues
from daegam.services.evaluation.holds import HELD_DECISIONS, hold_writes
from daegam.services.runs.trigger_rule import unguarded_tools
from daegam.services.tool_effects import ORIGIN_AGENT, graph_tools
from daegam.services.tool_pins import apply_pins, pins_of
from daegam.store.agents import AgentSpec
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import (
    CTX_TENANT,
    attach,
    compile_spec,
    create_agent,
    deploy_agent,
    knowledge_node,
    make_ctx,
    node_of,
    template_for,
    template_spec,
    without_memory,
)
from tests.helpers.auth import as_principal, bearer
from tests.helpers.consts import AG, HUB, IMAGE, ISSUER
from tests.helpers.docs import agent_doc
from tests.helpers.http import api_client, detail_code, make_app

ITEM = "hub-research-0001"
CALLED = "ag-research-0001"
ASK = "tools-ask"
TOOL = "ask_research"
PLATFORM = "http://platform.daegam-system.svc:8000"
# 호출받는 Agent 의 턴 시간과 그것으로 정한 기다리는 시간
CALLED_TURN = 120
WAIT = CALLED_TURN + CALL_GRACE_SECONDS


def _source(**over: Any) -> AgentSource:
    """게시돼 있고 턴을 받는 Agent `research` 의 항목을 읽은 것이다."""
    base = AgentSource(
        ITEM,
        tool=TOOL,
        description="Answers research questions.",
        agent_id=CALLED,
        agent_name="research",
        state="deployed",
        timeout_seconds=WAIT,
        provider="local-stub",
    )
    return replace(base, **over)


def _calling(spec: dict[str, Any] | None = None, item: str = ITEM, **data: Any) -> dict[str, Any]:
    """spec(없으면 템플릿)의 메인에 item 을 가리키는 Agent 소스 노드를 붙인 것이다. 메인의 턴
    시간은 기다리는 시간보다 길게 둔다."""
    out = copy.deepcopy(spec if spec is not None else template_spec())
    node_of(out, "agent-main")["data"]["turn_timeout_seconds"] = 600
    out["graph"]["nodes"].append(
        {"id": ASK, "type": "tools", "data": {"source": {"kind": "agent", "id": item}, **data}}
    )
    attach(out, "agent-main", ASK, "tools")
    return out


def _ctx(*sources: AgentSource, **kw: Any) -> GraphContext:
    ctx = make_ctx(**kw)
    ctx.agent_sources = {s.item_id: s for s in sources}
    ctx.source_tools = source_tools(ctx.agent_sources)
    return ctx


def _issues(spec: dict[str, Any], ctx: GraphContext, **kw: Any) -> dict[str, str]:
    """이슈의 코드 → level 이다."""
    found = call_issues(AgentSpec.model_validate(spec), ctx, **kw)
    return {i.code: i.level for i in found}


# -- 이름과 규칙 ---------------------------------------------------------------------------------


def test_the_tool_is_named_after_the_item():
    assert tool_name("research") == TOOL
    assert tool_name("billing-help-kr") == "ask_billing_help_kr"


def test_a_usable_source_passes_and_a_graph_without_one_is_not_looked_at():
    ctx = _ctx(_source())
    assert _issues(_calling(), ctx) == {}
    assert agent_graph.validate(AgentSpec.model_validate(_calling()), ctx) == []
    assert _issues(template_spec(), make_ctx(), enabled=False) == {}


@pytest.mark.parametrize(
    "code",
    [
        "agent_source_unpublished",
        "agent_source_no_consume",
        "agent_source_self",
        "source_not_deployed",
        "agent_source_runs_unmet",
        "runtime_feature_missing",
    ],
)
def test_a_source_the_saver_cannot_use_is_an_error_on_its_node(code):
    found = call_issues(
        AgentSpec.model_validate(_calling()), _ctx(_source(refusal=Refusal(code, "why")))
    )
    assert [(i.code, i.level, i.node_id) for i in found] == [(code, "error", ASK)]


def test_an_item_the_context_did_not_read_counts_as_unpublished():
    assert _issues(_calling(), make_ctx()) == {"agent_source_unpublished": "error"}
    assert _issues(_calling(item=""), _ctx(_source())) == {"agent_source_unpublished": "error"}


@pytest.mark.parametrize(
    "data",
    [
        {"allow_tools": ["other"]},
        {"approval": {"other": ["approve"]}},
        {"timeouts": {"other": 30}},
        {"rememberable": ["other"]},
        {"read_only": [TOOL]},
        {"outside": {TOOL: False}},
    ],
)
def test_the_node_rules_name_only_its_own_tool_and_never_its_marks(data):
    assert _issues(_calling(**data), _ctx(_source())) == {"invalid_node_data": "error"}


def test_the_node_may_ask_for_approval_and_lower_the_wait():
    data = {"approval": {TOOL: ["approve", "reject"]}, "timeouts": {TOOL: 60}}
    assert _issues(_calling(**data), _ctx(_source())) == {}


def test_a_tool_name_another_node_of_the_same_agent_offers_is_refused():
    """검색 도구든 같은 항목을 가리키는 다른 노드든 같은 에이전트에서 이름이 겹치면 막는다."""
    ctx = _ctx(_source())
    spec = _calling()
    spec["graph"]["nodes"].append(knowledge_node(tool_name=TOOL))
    attach(spec, "agent-main", "kb-faq")
    found = call_issues(AgentSpec.model_validate(spec), ctx)
    assert [(i.code, i.node_id) for i in found] == [("duplicate_tool_name", ASK)]

    twice = _calling()
    twice["graph"]["nodes"].append(
        {"id": "tools-again", "type": "tools", "data": {"source": {"kind": "agent", "id": ITEM}}}
    )
    attach(twice, "agent-main", "tools-again", "tools")
    found = call_issues(AgentSpec.model_validate(twice), ctx)
    assert {(i.code, i.node_id) for i in found} == {
        ("duplicate_tool_name", ASK),
        ("duplicate_tool_name", "tools-again"),
    }


def test_the_save_warns_of_a_loop_a_short_turn_and_calls_turned_off():
    assert _issues(_calling(), _ctx(_source(cycle=True))) == {"agent_call_cycle": "warning"}

    short = _calling()
    node_of(short, "agent-main")["data"]["turn_timeout_seconds"] = WAIT
    assert _issues(short, _ctx(_source())) == {"agent_call_timeout_short": "warning"}
    node_of(short, "agent-main")["data"]["turn_timeout_seconds"] = WAIT + 1
    assert _issues(short, _ctx(_source())) == {}

    assert _issues(_calling(), _ctx(_source()), enabled=False) == {
        "agent_calls_disabled": "warning"
    }


# -- 효과와 표시 ---------------------------------------------------------------------------------


def test_the_tool_is_always_a_write_and_its_result_always_outside_content():
    """캔버스의 읽기 전용 표시와 바깥 표시가 저장돼 있어도 효과가 없다. 그 표시는 검증이 막는다."""
    marked = _calling(read_only=[TOOL], outside={TOOL: False})
    rev = AgentSpec.model_validate(marked)
    ctx = _ctx(_source())
    for tool_safety in (False, True):
        tools = graph_tools(rev, ctx.source_tools, tool_safety=tool_safety)
        assert tools.effects[ASK] == {TOOL: "write"}
        assert tools.origins[ASK] == {TOOL: ORIGIN_AGENT}

    ctx.tool_pins = pins_of(rev, ctx.source_tools, ctx.agent_sources)
    ctx.agent_sources = addressed(ctx.agent_sources, PLATFORM)
    document = apply_pins(compile_spec(marked, ctx), rev, ctx.tool_pins, ctx.source_tools)
    final = finish_document(document, frozenset({AGENT_TOOLS, TOOL_SAFETY}), ctx, rev)
    entry = next(s for s in final["tools"] if s["ref"] == ASK)
    assert "inside" not in entry and "read" not in entry
    other = next(s for s in final["tools"] if s["ref"] != ASK)
    assert "inside" in other and "read" in other


def test_a_graph_without_memory_is_warned_that_the_call_is_refused_after_outside_content():
    spec = without_memory(_calling())
    node_of(spec, "tools-calculator")["data"]["read_only"] = ["Add", "Divide"]
    codes = {i.code for i in safety_issues(AgentSpec.model_validate(spec), _ctx(_source()))}
    assert "outside_writes_refused" in codes


def test_a_trigger_and_an_evaluation_treat_the_call_as_a_write():
    """웹훅 트리거는 그 Tool 에 승인 규칙이 있어야 하고, 평가 미리 보기는 그 호출을 잡는다."""
    ctx = _ctx(_source())
    spec = _calling()
    rev = AgentSpec.model_validate(spec)
    pins = pins_of(rev, ctx.source_tools, ctx.agent_sources)
    doc = agent_doc(spec=spec, state="deployed", tool_pins=pins)
    assert f"{ASK}/{TOOL}" in unguarded_tools(doc)

    guarded = _calling(approval={TOOL: ["approve", "reject"]})
    doc = agent_doc(spec=guarded, state="deployed", tool_pins=pins)
    assert f"{ASK}/{TOOL}" not in unguarded_tools(doc)

    ctx.tool_pins = pins
    ctx.agent_sources = addressed(ctx.agent_sources, PLATFORM)
    document = apply_pins(compile_spec(guarded, ctx), rev, pins, ctx.source_tools)
    held = hold_writes(document, AgentSpec.model_validate(guarded), ctx)
    entry = next(s for s in held["tools"] if s["ref"] == ASK)
    assert entry["approval"] == {TOOL: list(HELD_DECISIONS)}


# -- 핀과 컴파일 ---------------------------------------------------------------------------------


def _compiled(spec: dict[str, Any], ctx: GraphContext, *, pin: bool = True) -> dict[str, Any]:
    """배포처럼 핀을 박고(pin) 컴파일한 문서의 Agent 소스 항목이다."""
    rev = AgentSpec.model_validate(spec)
    if pin:
        ctx.tool_pins = pins_of(rev, ctx.source_tools, ctx.agent_sources)
    ctx.agent_sources = addressed(ctx.agent_sources, PLATFORM)
    document = apply_pins(compile_spec(spec, ctx), rev, ctx.tool_pins, ctx.source_tools)
    final = finish_document(document, frozenset({AGENT_TOOLS}), ctx, rev)
    assert AGENT_TOOLS in final["features"]
    return next(s for s in final["tools"] if s["ref"] == ASK)


def test_the_deploy_pins_the_call_and_the_compile_writes_the_runtime_entry():
    """항목은 런타임 스키마의 Agent 소스다. 소스의 id 와 이름은 항목이 아니라 호출받는 Agent 의
    것이고, 주소는 그 Agent 의 호출 경로다. 메인의 ref 목록에도 든다."""
    ctx = _ctx(_source())
    spec = _calling(approval={TOOL: ["approve", "reject"]})
    rev = AgentSpec.model_validate(spec)

    pin = pins_of(rev, ctx.source_tools, ctx.agent_sources)[ASK]
    assert (pin.names, pin.effects) == ([TOOL], {TOOL: "write"})
    assert pin.call is not None and pin.call.model_dump() == {
        "agent_id": CALLED,
        "agent_name": "research",
        "description": "Answers research questions.",
        "timeout_seconds": WAIT,
    }
    assert pins_of(rev, ctx.source_tools)["tools-calculator"].call is None

    assert _compiled(spec, ctx) == {
        "ref": ASK,
        "source": {"kind": "agent", "id": CALLED, "name": "research"},
        "url": f"{PLATFORM}/api/internal/agent-calls/{CALLED}",
        "allow": [TOOL],
        "approval": {TOOL: ["approve", "reject"]},
        "timeouts": {},
        "description": "Answers research questions.",
        "timeout_seconds": WAIT,
    }
    assert call_url(PLATFORM + "/", CALLED) == f"{PLATFORM}/api/internal/agent-calls/{CALLED}"
    assert ASK in compile_spec(spec, ctx)["main"]["tools"]


def test_the_node_timeout_only_lowers_the_wait():
    assert _compiled(_calling(timeouts={TOOL: 45}), _ctx(_source()))["timeout_seconds"] == 45
    assert _compiled(_calling(timeouts={TOOL: 900}), _ctx(_source()))["timeout_seconds"] == WAIT


def test_the_compile_follows_the_pin_after_the_item_changed_or_went_away():
    """항목의 설명을 고치거나 게시를 거둬도, 호출받는 Agent 가 멈춰도 부르는 Agent 의 문서는 배포
    때의 것이다."""
    spec = _calling()
    ctx = _ctx(_source())
    pinned = _compiled(spec, ctx)
    pins = ctx.tool_pins

    for now in (
        _source(description="Changed.", timeout_seconds=999, agent_name="renamed"),
        _source(refusal=Refusal("source_not_deployed", "stopped")),
        AgentSource(ITEM, refusal=Refusal("agent_source_unpublished", "gone")),
    ):
        later = _ctx(now)
        later.tool_pins = pins
        assert _compiled(spec, later, pin=False) == pinned


def test_a_node_without_a_pin_compiles_from_the_source_or_is_refused():
    assert _compiled(_calling(), _ctx(_source()), pin=False)["description"] == (
        "Answers research questions."
    )
    gone = _ctx(AgentSource(ITEM, refusal=Refusal("agent_source_unpublished", "gone")))
    with pytest.raises(GraphError) as e:
        _compiled(_calling(), gone, pin=False)
    assert [i.code for i in e.value.issues] == ["agent_source_unpublished"]


def test_a_graph_with_an_agent_source_requires_the_feature():
    doc = agent_doc(spec=_calling(), tenant=CTX_TENANT)
    wanted = agent_features.agent_features(doc, doc.current, {})
    assert AGENT_TOOLS in wanted.required
    plain = agent_doc(spec=template_spec(), tenant=CTX_TENANT)
    assert AGENT_TOOLS not in agent_features.agent_features(plain, plain.current, {}).wanted


def test_agent_sources_are_no_turn_token_targets_but_are_listed_apart():
    rev = AgentSpec.model_validate(_calling())
    assert agent_graph.tool_sources(rev) == [("gateway_instance", "gi-calc-0001")]
    assert agent_graph.agent_source_items(rev) == [ITEM]
    assert agent_graph.references_tool_source(rev, "agent", ITEM)
    assert agent_graph.newer_sources(rev) == {}


# -- 상태 경고 -----------------------------------------------------------------------------------


def _status(source: AgentSource | None, spec: dict[str, Any] | None = None, **kw: Any) -> list[str]:
    rev = AgentSpec.model_validate(spec or _calling())
    lines = status_warnings(rev, {ITEM: source} if source is not None else {}, **kw)
    return [line.split(":")[0] for line in lines]


def test_status_warns_by_what_the_called_agent_is_now():
    assert _status(_source()) == []
    assert _status(None) == ["agent_source_unpublished"]
    assert _status(_source(refusal=Refusal("agent_source_unpublished", "gone"))) == [
        "agent_source_unpublished"
    ]
    stopped = Refusal("source_not_deployed", "stopped")
    assert _status(_source(refusal=stopped, state="draft")) == ["agent_source_not_serving"]
    assert _status(_source(refusal=stopped, state="deploying")) == []
    unmet = Refusal("agent_source_runs_unmet", "no memory")
    assert _status(_source(refusal=unmet)) == ["agent_source_not_serving"]
    assert _status(_source(refusal=Refusal("runtime_feature_missing", "old image"))) == []

    short = _calling()
    node_of(short, "agent-main")["data"]["turn_timeout_seconds"] = WAIT
    assert _status(_source(), short) == ["agent_call_timeout_short"]
    assert _status(_source(), enabled=False) == ["agent_calls_disabled"]
    assert status_warnings(AgentSpec.model_validate(template_spec()), {}, enabled=False) == []


def test_a_status_line_names_its_tools_nodes():
    rev = AgentSpec.model_validate(_calling())
    assert status_warnings(rev, {})[0].endswith(f"({ASK})")


# -- 플랫폼에서: 저장, 배포, init 받기, 상태 ------------------------------------------------------

# 호출을 받는 Agent 와 부르는 Agent 의 런타임이 알리는 이름
CALLED_FEATURES = "runs,turn_tokens"
CALLING_FEATURES = "runs,turn_tokens,agent_tools,tool_safety"
DESCRIPTION = "Answers billing questions"


@pytest.fixture
def call_client(settings, mongo, fake_clusters, fake_secrets):
    # 워커는 끈다. 저장과 배포는 실행을 만들지 않는다
    app = make_app(
        settings, fake_clusters, fake_secrets, runs_enabled=True, runs_workers_enabled=False
    )
    with api_client(app) as c:
        yield c


def _callable_spec(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """memory 노드와 세션 예산이 있는 템플릿이다."""
    spec = template_for(gw, ds)
    spec["service"].setdefault("budget", {})["session_tokens"] = 20_000
    return spec


def _published(client, fake_clusters, mongo, **body: Any) -> tuple[DeployedAgent, dict[str, Any]]:
    """게시한 Agent `support` 와 그 허브 항목이다."""
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=_callable_spec, runtime=False, features=CALLED_FEATURES
    )
    r = client.post(f"{agent.url}/publish", json={"description": DESCRIPTION, **body})
    assert r.status_code == 201, r.text
    return agent, r.json()


def _validate(client, spec: dict[str, Any], **params: Any) -> dict[str, Any]:
    r = client.post(f"{AG}/validate", json=spec, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _codes(body: dict[str, Any]) -> dict[str, str]:
    return {i["code"]: i["level"] for i in body["issues"]}


def _caller(client, called: DeployedAgent, item: dict[str, Any], **data: Any) -> dict[str, Any]:
    """항목을 Tool 로 쓰는 Agent `caller` 를 만들어 배포한 응답이다."""
    spec = _calling(called.spec(), item["id"], **data)
    created = create_agent(client, spec, "caller")
    assert created.status_code == 201, created.text
    r = deploy_agent(client, created.json()["id"])
    assert r.status_code == 201, r.text
    return r.json()


def _agent_json(client, fake, doc: dict[str, Any], features: str = CALLING_FEATURES):
    token = fake.get("v1", "Secret", doc["k8s"]["token_secret"], doc["k8s"]["namespace"])
    headers = {
        **bearer(token["stringData"]["token"]),
        agent_features.IMAGE_HEADER: IMAGE,
        agent_features.FEATURES_HEADER: features,
    }
    return client.get(f"{AG}/{doc['id']}/agent.json", headers=headers)


def _ask_entry(client, fake, doc: dict[str, Any]) -> dict[str, Any]:
    r = _agent_json(client, fake, doc)
    assert r.status_code == 200, r.text
    assert AGENT_TOOLS in r.json()["features"]
    return next(s for s in r.json()["tools"] if s["ref"] == ASK)


def test_a_published_agent_is_one_tool_of_the_agent_that_attaches_it(
    call_client, mongo, fake_clusters
):
    """검사는 그 Tool 의 효과와 종류, 호출받는 Agent 의 provider 와 본문 기록을 알린다. 배포는
    호출을 핀에 박고, init 이 받는 문서의 항목은 그 Agent 의 id 와 호출 경로를 든다."""
    called, item = _published(call_client, fake_clusters, mongo)
    spec = _calling(called.spec(), item["id"])

    body = _validate(call_client, spec)
    assert body["ok"] and _codes(body) == {}
    assert body["tool_effects"][ASK] == {"ask_support": "write"}
    assert body["tool_origins"][ASK] == {"ask_support": "agent"}
    assert body["agent_calls"] == {
        ASK: {
            "tool": "ask_support",
            "agent_id": called.id,
            "agent_name": "support",
            "provider": "local-stub",
            "record_bodies": False,
            "timeout_seconds": WAIT,
        }
    }

    caller = _caller(call_client, called, item)
    pin = mongo["agents"].find_one({"_id": caller["id"]})["tool_pins"][ASK]
    assert pin["names"] == ["ask_support"] and pin["effects"] == {"ask_support": "write"}
    assert pin["call"] == {
        "agent_id": called.id,
        "agent_name": "support",
        "description": DESCRIPTION,
        "timeout_seconds": WAIT,
    }
    assert _ask_entry(call_client, called.fake, caller) == {
        "ref": ASK,
        "source": {"kind": "agent", "id": called.id, "name": "support"},
        "url": f"{ISSUER}/api/internal/agent-calls/{called.id}",
        "allow": ["ask_support"],
        "approval": {},
        "timeouts": {},
        "description": DESCRIPTION,
        "timeout_seconds": WAIT,
    }
    assert call_client.get(f"{AG}/{caller['id']}").json()["status"]["warnings"] == []
    refs = call_client.get(f"{HUB}/{item['id']}/references").json()["used_by"]
    assert [r["id"] for r in refs if r["kind"] == "agent"] == [caller["id"]]


def test_the_save_refuses_an_item_the_saver_cannot_attach(call_client, mongo, fake_clusters):
    called, item = _published(call_client, fake_clusters, mongo)
    spec = _calling(called.spec(), item["id"])

    assert _codes(_validate(call_client, _calling(called.spec(), "no-such-item"))) == {
        "agent_source_unpublished": "error"
    }
    assert _codes(_validate(call_client, _calling(called.spec(), called.gw["id"]))) == {
        "agent_source_unpublished": "error"
    }
    assert _codes(_validate(call_client, spec, agent_id=called.id)) == {
        "agent_source_self": "error"
    }
    r = call_client.put(called.url, json={"name": "support", **spec})
    assert detail_code(r) == (422, "invalid_graph")
    assert [i["code"] for i in r.json()["detail"]["issues"]] == ["agent_source_self"]
    r = create_agent(call_client, _calling(called.spec(), "no-such-item"), "caller")
    assert detail_code(r) == (422, "invalid_graph")

    admins = {"roles": ["tenant-admin"]}
    r = call_client.put(f"{HUB}/{item['id']}/access", json={"consume": admins, "manage": admins})
    assert r.status_code == 200, r.text
    with as_principal(call_client, "dev", "developer"):
        assert _codes(_validate(call_client, spec)) == {"agent_source_no_consume": "error"}
    with as_principal(call_client, "boss", "tenant-admin"):
        assert _codes(_validate(call_client, spec)) == {}


def test_the_save_refuses_a_called_agent_that_cannot_take_calls(call_client, mongo, fake_clusters):
    """호출받는 Agent 가 세션 예산을 잃은 배포를 하면 그것을 붙이는 저장이 막히고, 이미 붙인 Agent
    의 상태가 알린다."""
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    spec = _calling(called.spec(), item["id"])

    broken = called.spec()
    broken["service"]["budget"]["session_tokens"] = None
    assert call_client.put(called.url, json={"name": "support", **broken}).status_code == 200
    assert call_client.post(f"{called.url}/redeploy", json={}).status_code == 200

    assert _codes(_validate(call_client, spec)) == {"agent_source_runs_unmet": "error"}
    warnings = call_client.get(f"{AG}/{caller['id']}").json()["status"]["warnings"]
    assert [w.split(":")[0] for w in warnings] == ["agent_source_not_serving"]


def test_unpublishing_stops_new_saves_but_the_calling_pod_still_starts(
    call_client, mongo, fake_clusters
):
    """게시를 거두면 그 항목을 쓰는 저장과 배포가 막히고 상태가 알린다. 부르는 Agent 의 파드는
    배포 때의 핀으로 같은 문서를 받는다."""
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    before = _ask_entry(call_client, called.fake, caller)

    mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": {"description": "Changed"}})
    assert _ask_entry(call_client, called.fake, caller) == before

    assert call_client.delete(f"{called.url}/publish").status_code == 200
    assert _ask_entry(call_client, called.fake, caller) == before
    warnings = call_client.get(f"{AG}/{caller['id']}").json()["status"]["warnings"]
    assert [w.split(":")[0] for w in warnings] == ["agent_source_unpublished"]
    r = call_client.post(f"{AG}/{caller['id']}/redeploy", json={})
    assert detail_code(r) == (422, "invalid_graph")
    assert [i["code"] for i in r.json()["detail"]["issues"]] == ["agent_source_unpublished"]


def test_a_redeploy_pins_the_item_description_again(call_client, mongo, fake_clusters):
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": {"description": "Changed"}})

    assert call_client.post(f"{AG}/{caller['id']}/redeploy", json={}).status_code == 200
    assert _ask_entry(call_client, called.fake, caller)["description"] == "Changed"


def test_a_deploy_onto_an_image_that_cannot_call_agents_is_refused(
    call_client, mongo, fake_clusters
):
    """이미지가 `agent_tools` 를 알리지 않았으면 그 그래프의 재배포는 409 다. init 받기도 그
    이미지를 거절한다."""
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    r = _agent_json(call_client, called.fake, caller, features="runs,turn_tokens,tool_safety")
    assert detail_code(r) == (409, "runtime_image_too_old")

    r = call_client.post(f"{AG}/{caller['id']}/redeploy", json={})
    assert detail_code(r) == (409, "runtime_feature_missing")
    assert r.json()["detail"]["feature"] == AGENT_TOOLS


def test_the_save_warns_when_the_called_agent_calls_back(call_client, mongo, fake_clusters):
    """`support` 를 부르는 `caller` 를 게시한 뒤 `support` 가 `caller` 를 붙이면 순환이다."""
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    assert _agent_json(call_client, called.fake, caller).status_code == 200
    r = call_client.post(f"{AG}/{caller['id']}/publish", json={"description": "Calls support"})
    assert r.status_code == 201, r.text

    back = _calling(called.spec(), r.json()["id"])
    node_of(back, "agent-main")["data"]["turn_timeout_seconds"] = 900
    assert _codes(_validate(call_client, back, agent_id=called.id)) == {
        "agent_call_cycle": "warning"
    }
    assert _codes(_validate(call_client, back)) == {}


def test_a_platform_with_calls_turned_off_warns_instead_of_refusing(
    call_client, settings, mongo, fake_clusters, fake_secrets
):
    called, item = _published(call_client, fake_clusters, mongo)
    caller = _caller(call_client, called, item)
    spec = _calling(called.spec(), item["id"])

    off = make_app(settings, fake_clusters, fake_secrets, runs_enabled=False)
    with api_client(off) as c:
        assert _codes(_validate(c, spec)) == {"agent_calls_disabled": "warning"}
        warnings = c.get(f"{AG}/{caller['id']}").json()["status"]["warnings"]
        assert [w.split(":")[0] for w in warnings] == ["agent_calls_disabled"]
