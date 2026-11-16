"""P48 6.1·6.2절: 청지기의 Agent 정의는 제안이다 (stub provider, 실제 Agent API).

만들기와 바꾸기가 검사를 거쳐 홀로 기다리고, 결정은 제안의 지문을 실어야 하며, 승인은 기준
리비전 위에 다시 짜 맞춰 저장하고, 넘지 못하는 선은 도구 결과로 보이는 것을 본다. 비교와 먼저 볼
것의 경우들은 test_agent_changes.py 가 DB 없이 본다.
"""

from __future__ import annotations

import copy
import json

from daegam.services.assistant import proposals
from daegam.services.assistant.proposals import PROPOSAL_KEY, PROPOSAL_MAX_BYTES, fit
from daegam.services.assistant.stub import LAST_ERROR_FORMAT
from tests.helpers.agents import (
    BASE_PROVIDERS,
    agent_env,
    create_agent,
    node_of,
    template_for,
)
from tests.helpers.assistant import (
    decide,
    decided,
    enable,
    names,
    new_session,
    say,
    session_doc,
    steps,
    tool,
    turn,
)
from tests.helpers.auth import bearer, map_groups
from tests.helpers.consts import AG, GROUP_MAPPINGS, A
from tests.helpers.http import detail_code


def _env(client, mongo):
    _cid, ds, gw = agent_env(client, mongo)
    enable(client, providers=BASE_PROVIDERS)
    return template_for(gw, ds)


def _pending(client, sid):
    (call,) = client.get(f"{A}/sessions/{sid}").json()["pending"]["calls"]
    return call


def _content(event) -> dict:
    return json.loads(event[1]["content"])


def test_a_creation_waits_alone_and_every_decision_carries_its_digest(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    create = tool("create_agent", name="support", display_name="상담", **spec)
    script = steps(
        {
            "tools": [
                create,
                tool("deploy_mcp_server", server_id="p"),
                tool("create_agent", name="second", **spec),
            ]
        },
        say("저장했다"),
    )
    events = turn(client, sid, script)
    # 같은 메시지의 다른 승인 대상은 두 번째 제안까지 미뤄진다
    assert names(events) == ["tool_call", "tool_result", "tool_call", "tool_result", "interrupt"]
    assert [events[0][1]["name"], events[2][1]["name"]] == ["deploy_mcp_server", "create_agent"]
    assert {_content(events[1])["code"], _content(events[3])["code"]} == {"deferred"}
    (request,) = events[4][1]["action_requests"]
    assert request["name"] == "create_agent" and "layout" not in request["args"]
    block = request["proposal"]
    reserved = request["args"][PROPOSAL_KEY]
    assert block["proposal_id"] == reserved["id"] and reserved["base_revision"] is None
    assert block["kind"] == "create" and block["agent"]["display_name"] == "상담"
    added = [n["id"] for n in block["changes"]["nodes"]["added"]]
    assert added == [n["id"] for n in spec["graph"]["nodes"]]
    codes = {m["code"] for m in block["attention"]}
    assert {"system_prompt_changed", "tool_source_added", "service_changed"} <= codes
    # 소스 이름은 그 문서에서 읽는다
    source = next(m for m in block["attention"] if m["code"] == "tool_source_added")["source"]
    assert source["name"] == "calc"
    call = _pending(client, sid)
    assert call["args"][PROPOSAL_KEY]["digest"] == reserved["digest"]
    # 지문이 없거나 다르면 승인도 거절도 409 다
    for decision in (
        {"id": call["id"], "type": "approve"},
        {"id": call["id"], "type": "approve", "proposal_digest": "v1:other"},
        {"id": call["id"], "type": "reject"},
    ):
        assert detail_code(decide(client, sid, [decision])) == (409, "proposal_changed")
    assert client.get(AG).json() == []
    events = decided(
        client,
        sid,
        [{"id": call["id"], "type": "approve", "proposal_digest": reserved["digest"]}],
    )
    assert names(events) == ["tool_call", "tool_result", "resource", "token", "done"]
    assert PROPOSAL_KEY not in events[0][1]["args"]
    saved = _content(events[1])
    assert events[1][1]["status"] == "success" and saved["name"] == "support"
    assert events[-1][1]["text"] == "저장했다"
    # 제안의 검사와 저장은 한 호출로 센다. 미뤄진 호출은 세지 않는다
    assert events[-1][1]["tool_calls"] == 1
    agent = client.get(f"{AG}/{saved['id']}").json()
    assert agent["display_name"] == "상담" and agent["layout"] == {}
    # 검사·승인·저장은 사람의 신원으로 그 세션의 via 를 남긴다
    rows = client.get("/audit", params={"via": f"assistant/{sid}"}).json()
    assert sorted(r["action"] for r in rows) == [
        "create_agent",
        "validate_agent_graph",
        "validate_agent_graph",
    ]


def test_refused_proposals_show_as_a_tool_call_and_its_result(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    marked = copy.deepcopy(spec)
    node_of(marked, "tools-calculator")["data"]["read_only"] = ["Add"]
    planted = copy.deepcopy(spec)
    node_of(planted, "tools-calculator")["data"].update({"allow_tools": [], "allow_domain": ["x"]})
    node_of(planted, "model-main")["data"]["provider"] = "missing"
    huge = copy.deepcopy(spec)
    node_of(huge, "agent-main")["data"]["system_prompt"] = "x" * PROPOSAL_MAX_BYTES
    script = steps(
        tool("create_agent", name="support", **marked),
        tool("create_agent", name="support", **planted),
        tool("create_agent", name="support", **huge),
        say("$0.code $1.code $2.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == ["tool_call", "tool_result"] * 3 + ["token", "done"]
    first, second, third = _content(events[1]), _content(events[3]), _content(events[5])
    assert first["code"] == "person_marks_only"
    assert first["marks"] == [{"node_id": "tools-calculator", "mark": "read_only", "tool": "Add"}]
    assert second["code"] == "proposal_invalid" and second["marks"] == []
    assert {(i["code"], i["node_id"]) for i in second["issues"]} >= {
        ("unknown_provider", "model-main"),
        ("unknown_node_keys", "tools-calculator"),
        ("empty_allow_tools", "tools-calculator"),
    }
    assert third["code"] == "proposal_too_large"
    # 제안 인자는 화면과 전사의 tool_call 에 4KB 로 자른 사본이 간다 (6.6절)
    cut = json.dumps(events[4][1]["args"], ensure_ascii=False).encode("utf-8")
    assert len(cut) <= 4096 and events[4][1]["args"]["name"] == "support"
    done = events[-1][1]
    assert done["text"].startswith("person_marks_only proposal_invalid proposal_too_large")
    assert done["tool_calls"] == 3
    assert client.get(f"{A}/sessions/{sid}").json()["pending"] is None
    assert client.get(AG).json() == []


def _saved(client, spec):
    r = create_agent(
        client, {**spec, "layout": {"agent-main": {"x": 1, "y": 2}}}, display_name="상담"
    )
    assert r.status_code == 201, r.text
    return r.json()


def _change(agent_id: str, agent_node: dict, **extra):
    node = copy.deepcopy(agent_node)
    node["data"]["system_prompt"] = "너는 친절한 상담원이다.\n모르면 모른다고 한다."
    return tool(
        "update_agent",
        agent_id=agent_id,
        set_nodes=[node],
        remove_nodes=["guardrail-input"],
        set_display_name="새 상담",
        **extra,
    )


def test_a_change_is_a_patch_composed_onto_the_saved_revision(client, mongo):
    spec = _env(client, mongo)
    agent = _saved(client, spec)
    sid = new_session(client)
    main = node_of(agent, "agent-main")
    events = turn(client, sid, steps(_change(agent["id"], main), say("저장 r$0.revision")))
    assert names(events) == ["interrupt"]
    (request,) = events[0][1]["action_requests"]
    block = request["proposal"]
    assert block["kind"] == "update" and block["base_revision"] == 1
    assert block["agent"] == {"id": agent["id"], "name": "support", "display_name": "새 상담"}
    changes = block["changes"]
    assert changes["nodes"]["removed"] == [{"id": "guardrail-input", "type": "guardrail"}]
    assert changes["nodes"]["changed"][0]["fields"] == ["system_prompt"]
    assert changes["display_name"] == {"before": "상담", "after": "새 상담"}
    (text,) = changes["texts"]
    assert text["field"] == "system_prompt" and {line["op"] for line in text["lines"]} == {"-", "+"}
    codes = [m["code"] for m in block["attention"]]
    assert codes == ["system_prompt_changed", "guardrail_weakened"]
    digest = request["args"][PROPOSAL_KEY]["digest"]
    call = _pending(client, sid)
    events = decided(
        client, sid, [{"id": call["id"], "type": "approve", "proposal_digest": digest}]
    )
    assert names(events) == ["tool_call", "tool_result", "resource", "token", "done"]
    assert events[-1][1]["text"] == "저장 r2"
    after = client.get(f"{AG}/{agent['id']}").json()
    assert after["revision"] == 2 and after["display_name"] == "새 상담"
    before_nodes = {n["id"]: n for n in agent["graph"]["nodes"]}
    for node in after["graph"]["nodes"]:
        if node["id"] != "agent-main":
            assert node == before_nodes[node["id"]]
    assert "guardrail-input" not in {n["id"] for n in after["graph"]["nodes"]}
    # 제안은 배치를 보내지 않으므로 남은 노드의 저장된 배치가 그대로다 (6.7절)
    assert after["layout"] == {"agent-main": {"x": 1.0, "y": 2.0}}
    rows = client.get("/audit", params={"via": f"assistant/{sid}"}).json()
    assert "put_agent" in {r["action"] for r in rows}


def test_a_patch_naming_an_unknown_node_is_refused(client, mongo):
    spec = _env(client, mongo)
    agent = _saved(client, spec)
    sid = new_session(client)
    script = steps(
        tool("update_agent", agent_id=agent["id"], remove_nodes=["ghost"]),
        tool("update_agent", agent_id="nope", remove_nodes=["ghost"]),
        say("$0.code $1.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "token",
        "done",
    ]
    refused = _content(events[1])
    assert refused["code"] == "proposal_invalid"
    assert refused["issues"][0]["code"] == "unknown_node"
    missing = _content(events[3])
    assert (missing["status"], missing["code"]) == (404, "not_found")


def test_a_newer_revision_or_a_new_mark_saves_nothing(client, mongo):
    spec = _env(client, mongo)
    agent = _saved(client, spec)
    main = node_of(agent, "agent-main")
    # 그사이 사람이 새 리비전을 저장했다. 승인은 기준 리비전을 실어 409 다
    sid = new_session(client)
    turn(client, sid, steps(_change(agent["id"], main), say("$0.code")))
    call = _pending(client, sid)
    digest = call["args"][PROPOSAL_KEY]["digest"]
    body = {"name": "support", **copy.deepcopy(spec)}
    node_of(body, "agent-main")["data"]["system_prompt"] = "사람이 고친 글"
    assert client.put(f"{AG}/{agent['id']}", json=body).status_code == 200
    events = decided(
        client, sid, [{"id": call["id"], "type": "approve", "proposal_digest": digest}]
    )
    conflict = _content(events[1])
    assert (conflict["status"], conflict["code"]) == (409, "revision_conflict")
    assert conflict["revision"] == 2
    assert client.get(f"{AG}/{agent['id']}").json()["revision"] == 2
    # 승인 때 다시 센 먼저 볼 것이 카드에 없던 것이면 저장하지 않는다
    sid = new_session(client)
    main = node_of(client.get(f"{AG}/{agent['id']}").json(), "agent-main")
    turn(client, sid, steps(_change(agent["id"], main), say("$0.code")))
    call = _pending(client, sid)
    digest = call["args"][PROPOSAL_KEY]["digest"]
    mongo["assistant_sessions"].update_one(
        {"_id": sid}, {"$set": {f"pending.calls.0.args.{PROPOSAL_KEY}.attention": []}}
    )
    events = decided(
        client, sid, [{"id": call["id"], "type": "approve", "proposal_digest": digest}]
    )
    changed = _content(events[1])
    assert changed["code"] == "proposal_changed"
    assert changed["attention"] == ["guardrail_weakened", "system_prompt_changed"]
    assert events[-1][1]["text"] == "proposal_changed" + LAST_ERROR_FORMAT.format(
        code="proposal_changed"
    )
    assert client.get(f"{AG}/{agent['id']}").json()["revision"] == 2
    assert session_doc(mongo, sid)["pending"] is None


def test_a_viewer_gets_the_validation_403_as_a_tool_result_and_no_card(oidc_client, token, mongo):
    map_groups(mongo, GROUP_MAPPINGS)
    admin = bearer(token("root", "daegam-admins"))
    enable(oidc_client, headers=admin)
    viewer = bearer(token("viewer1", "daegam-a-view"), tenant="a")
    sid = new_session(oidc_client, headers=viewer)
    script = steps(tool("create_agent", name="mine", graph={"nodes": []}), say("$0.code"))
    events = turn(oidc_client, sid, script, headers=viewer)
    assert names(events) == ["tool_call", "tool_result", "token", "done"]
    refused = _content(events[1])
    assert (refused["status"], refused["code"]) == (403, "forbidden")
    assert refused["scope"] == "agent:write"
    assert oidc_client.get(f"{A}/sessions/{sid}", headers=viewer).json()["pending"] is None


# -- 정의 읽기와 한 구간의 한도 (6.2절, 6.6절) -------------------------------------------------


def _long_prompt(spec, chars: int, letter: str = "가"):
    long = copy.deepcopy(spec)
    node_of(long, "agent-main")["data"]["system_prompt"] = letter * chars
    return long


def test_the_definition_read_is_whole_for_the_model_and_cut_for_the_transcript(client, mongo):
    spec = _env(client, mongo)
    agent = _saved(client, _long_prompt(spec, 3000))
    sid = new_session(client)
    script = steps(
        tool("get_agent_definition", agent_id=agent["id"]),
        tool("get_agent_definition", agent_id=agent["id"], node_id="agent-main"),
        tool("get_agent_definition", agent_id=agent["id"], node_id="ghost"),
        say("$2.code"),
    )
    events = turn(client, sid, script)
    assert names(events) == ["tool_call", "tool_result"] * 3 + ["token", "done"]
    overview = _content(events[1])
    main = next(n for n in overview["nodes"] if n["id"] == "agent-main")
    assert main["text_chars"]["system_prompt"] == 3000 and overview["revision"] == 1
    # 화면과 전사의 사본은 4KB 로 잘리고 모델의 것은 통째다
    shown = events[3][1]["content"]
    assert len(shown.encode("utf-8")) <= 4096 and "가" * 3000 not in shown
    context = session_doc(mongo, sid)["context"]
    reads = [
        m for m in context if m["type"] == "tool" and m["data"]["name"] == "get_agent_definition"
    ]
    node = json.loads(reads[1]["data"]["content"])["node"]
    assert node["data"]["system_prompt"] == "가" * 3000
    assert _content(events[5])["code"] == "unknown_node"


def test_a_node_above_the_cap_is_neither_read_nor_replaced(client, mongo):
    spec = _env(client, mongo)
    # 턴 메시지는 20만 자까지라 큰 노드는 ASCII 로 채운다
    agent = _saved(client, _long_prompt(spec, PROPOSAL_MAX_BYTES, "x"))
    # 통째로 읽을 수 없는 노드는 짧은 것으로 바꾸는 패치도 받지 않는다
    small = {"id": "agent-main", "type": "agent", "data": {"role": "main", "name": "Agent"}}
    sid = new_session(client)
    script = steps(
        tool("get_agent_definition", agent_id=agent["id"], node_id="agent-main"),
        tool("update_agent", agent_id=agent["id"], set_nodes=[small]),
        say("$0.code $1.code"),
    )
    events = turn(client, sid, script)
    assert _content(events[1])["code"] == "node_too_large"
    refused = _content(events[3])
    assert refused["code"] == "proposal_invalid"
    assert [i["code"] for i in refused["issues"]] == ["node_too_large"]


def _reject(client, sid):
    call = _pending(client, sid)
    digest = call["args"][PROPOSAL_KEY]["digest"]
    decision = {"id": call["id"], "type": "reject", "message": "다시", "proposal_digest": digest}
    return decided(client, sid, [decision])


def test_three_card_rejections_in_a_span_end_the_proposals_until_a_new_message(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    create = tool("create_agent", name="support", **spec)
    events = turn(client, sid, steps(create, create, create, create, say("$3.code")))
    assert names(events) == ["interrupt"]
    digests = [_pending(client, sid)["args"][PROPOSAL_KEY]["digest"]]
    for _ in range(2):
        assert names(_reject(client, sid))[-1] == "interrupt"
        digests.append(_pending(client, sid)["args"][PROPOSAL_KEY]["digest"])
    # 같은 인자를 다시 낸 제안도 지문이 다르다. 앞 카드의 결정이 뒤 제안에 닿지 못한다
    assert len(set(digests)) == 3
    events = _reject(client, sid)
    # 셋째 거절 뒤의 제안은 카드로 가지 않는다
    assert names(events) == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "token",
        "done",
    ]
    assert _content(events[3])["code"] == "proposal_limit"
    assert events[-1][1]["text"].startswith("proposal_limit")
    # 사람의 새 메시지가 구간을 연다
    assert names(turn(client, sid, steps(create, say("x")))) == ["interrupt"]


def test_a_canvas_save_closes_the_call_with_its_own_detail_and_no_card_count(client, mongo):
    """사람이 캔버스에서 제 판을 저장한 뒤의 거절은 카드 거절이 아니다(6.5절). 제 detail 을 싣고 한
    구간의 카드 거절 수에 들지 않으며, 거절에만 지문과 함께만 실린다."""
    spec = _env(client, mongo)
    sid = new_session(client)
    create = tool("create_agent", name="support", **spec)
    events = turn(client, sid, steps(create, create, create, create, say("ok")))
    assert names(events) == ["interrupt"]
    call = _pending(client, sid)
    digest = call["args"][PROPOSAL_KEY]["digest"]
    for wrong in (
        {"id": call["id"], "type": "approve", "proposal_digest": digest, "canvas_saved": True},
        {"id": call["id"], "type": "reject", "canvas_saved": True},
    ):
        assert detail_code(decide(client, sid, [wrong]))[0] == 422
    closing = {"id": call["id"], "type": "reject", "message": "r1 저장", "proposal_digest": digest}
    events = decided(client, sid, [{**closing, "canvas_saved": True}])
    closed = _content(events[1])
    assert closed["code"] == "rejected" and "card" not in closed
    assert closed["detail"] == f"r1 저장 ({proposals.CANVAS_SAVED})"
    assert proposals.CARD_REJECTED not in closed["detail"]
    assert names(events)[-1] == "interrupt"
    # 캔버스 저장 셋 뒤에도 카드 거절은 0 이라 다음 제안이 카드로 간다
    for _ in range(2):
        call = _pending(client, sid)
        digest = call["args"][PROPOSAL_KEY]["digest"]
        closing = {"id": call["id"], "type": "reject", "proposal_digest": digest}
        assert names(decided(client, sid, [{**closing, "canvas_saved": True}]))[-1] == "interrupt"
    assert _pending(client, sid)["name"] == "create_agent"
    # 지문을 실은 캔버스 저장도 제안이 아닌 호출에는 닿지 못한다
    sid = new_session(client)
    turn(client, sid, steps(tool("deploy_mcp_server", server_id="p"), say("x")))
    call = _pending(client, sid)
    plain = {"id": call["id"], "type": "reject", "proposal_digest": "p48.2:any"}
    assert detail_code(decide(client, sid, [{**plain, "canvas_saved": True}])) == (
        409,
        "proposal_changed",
    )


def test_exempt_reads_and_proposals_share_one_span_budget(client, mongo):
    spec = _env(client, mongo)
    agent = _saved(client, _long_prompt(spec, 20_000))
    sid = new_session(client)
    read = tool("get_agent_definition", agent_id=agent["id"], node_id="agent-main")
    # 노드 하나가 60 KB 남짓이라 넷째까지 읽고 다섯째는 256 KiB 를 넘는다
    events = turn(client, sid, steps(read, read, read, read, read, say("$3.code $4.code")))
    results = [_content(e) for e in events if e[0] == "tool_result"]
    assert [r.get("code") for r in results[:4]] == [None] * 4
    assert results[4]["code"] == "proposal_limit"
    # 제안 인자도 같은 예산에 든다. 셋째 읽기 뒤의 큰 제안(걸려도 센다)이 넷째 읽기를 막는다
    marked = _long_prompt(spec, 55_000, "x")
    node_of(marked, "tools-calculator")["data"]["read_only"] = ["Add"]
    create = tool("create_agent", name="big", **marked)
    sid = new_session(client)
    events = turn(client, sid, steps(read, read, read, create, read, say("$4.code")))
    results = [_content(e) for e in events if e[0] == "tool_result"]
    assert [r.get("code") for r in results] == [
        None,
        None,
        None,
        "person_marks_only",
        "proposal_limit",
    ]


# -- 제안 다시 읽기 (6.1절) ---------------------------------------------------------------------


def test_the_proposal_route_composes_onto_the_stored_base_and_reports_the_latest(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    r = client.get(f"{A}/sessions/{sid}/proposal")
    assert detail_code(r) == (404, "proposal_not_found")
    turn(client, sid, steps(tool("create_agent", name="support", **spec), say("x")))
    reserved = _pending(client, sid)["args"][PROPOSAL_KEY]
    rows = client.get("/audit", params={"via": f"assistant/{sid}"}).json()
    created = client.get(f"{A}/sessions/{sid}/proposal").json()
    # 화면의 다시 읽기는 청지기의 감사 행을 남기지 않는다
    assert client.get("/audit", params={"via": f"assistant/{sid}"}).json() == rows
    assert created["proposal_id"] == reserved["id"] and created["kind"] == "create"
    assert created["latest_revision"] is None and created["graph"] == spec["graph"]
    assert created["proposal"]["proposal_id"] == reserved["id"]
    agent = _saved(client, spec)
    sid = new_session(client)
    turn(client, sid, steps(_change(agent["id"], node_of(agent, "agent-main")), say("x")))
    view = client.get(f"{A}/sessions/{sid}/proposal").json()
    assert (view["kind"], view["base_revision"], view["latest_revision"]) == ("update", 1, 1)
    assert view["display_name"] == "새 상담"
    assert "guardrail-input" not in {n["id"] for n in view["graph"]["nodes"]}
    # 사람이 그사이 r2 를 저장해도 제안은 기준 r1 위에 짜 맞춰지고 최신은 따로 알린다
    body = {"name": "support", **copy.deepcopy(spec)}
    node_of(body, "entry")["data"]["greeting"] = "사람이 고친 인사"
    assert client.put(f"{AG}/{agent['id']}", json=body).status_code == 200
    stale = client.get(f"{A}/sessions/{sid}/proposal").json()
    assert (stale["base_revision"], stale["latest_revision"]) == (1, 2)
    assert node_of(stale, "entry")["data"]["greeting"] == node_of(spec, "entry")["data"]["greeting"]
    assert stale["proposal"]["attention"] == view["proposal"]["attention"]
    # 대상 Agent 를 읽지 못하면 그 상태를 그대로 낸다
    assert client.delete(f"{AG}/{agent['id']}").status_code == 200
    assert detail_code(client.get(f"{A}/sessions/{sid}/proposal")) == (404, "not_found")


# -- 결정과 지문의 가장자리 (6.1절, 10절) ---------------------------------------------------------


def test_a_digest_on_a_plain_call_and_an_old_proposal_without_its_key(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    turn(client, sid, steps(tool("deploy_mcp_server", server_id="p"), say("x")))
    call = _pending(client, sid)
    # 지문을 실은 결정은 제안 카드의 것이라 같은 id 의 다른 호출을 결정하지 못한다
    plain = {"id": call["id"], "type": "approve", "proposal_digest": "p48.2:any"}
    assert detail_code(decide(client, sid, [plain])) == (409, "proposal_changed")
    # 예약 칸 없이 기다리는 제안 도구(앞 판이 만든 호출)는 지문 없이 거절만 된다
    sid = new_session(client)
    turn(client, sid, steps(tool("create_agent", name="support", **spec), say("x")))
    mongo["assistant_sessions"].update_one(
        {"_id": sid}, {"$unset": {f"pending.calls.0.args.{PROPOSAL_KEY}": ""}}
    )
    call = _pending(client, sid)
    assert PROPOSAL_KEY not in call["args"]
    approve = {"id": call["id"], "type": "approve"}
    assert detail_code(decide(client, sid, [approve])) == (409, "proposal_changed")
    events = decided(client, sid, [{"id": call["id"], "type": "reject"}])
    assert _content(events[1])["code"] == "rejected"
    assert client.get(AG).json() == []


def test_a_reserved_key_the_model_sends_is_dropped(client, mongo):
    spec = _env(client, mongo)
    sid = new_session(client)
    forged = {"id": "mine", "digest": "p48.2:forged", "attention": []}
    turn(
        client, sid, steps(tool("create_agent", name="support", _proposal=forged, **spec), say("x"))
    )
    reserved = _pending(client, sid)["args"][PROPOSAL_KEY]
    assert reserved["digest"] != forged["digest"] and reserved["id"] != "mine"
    context = session_doc(mongo, sid)["context"]
    ai = next(m for m in context if m["type"] == "ai")
    assert PROPOSAL_KEY not in ai["data"]["tool_calls"][0]["args"]
    decision = {
        "id": _pending(client, sid)["id"],
        "type": "approve",
        "proposal_digest": forged["digest"],
    }
    assert detail_code(decide(client, sid, [decision])) == (409, "proposal_changed")


def test_approval_checks_the_person_marks_again(client, mongo, monkeypatch):
    """승인 때 Tool 의 백엔드 종류가 바뀌어 사람 표시를 넘게 되면 저장하지 않는다(6.2절)."""
    spec = _env(client, mongo)
    sid = new_session(client)
    turn(client, sid, steps(tool("create_agent", name="support", **spec), say("$0.code")))
    call = _pending(client, sid)
    mark = {"node_id": "tools-calculator", "mark": "outside_true_removed", "tool": "Add"}
    monkeypatch.setattr(proposals, "refusals", lambda *a, **kw: ([], [mark]))
    digest = call["args"][PROPOSAL_KEY]["digest"]
    events = decided(
        client, sid, [{"id": call["id"], "type": "approve", "proposal_digest": digest}]
    )
    assert _content(events[1])["code"] == "person_marks_only"
    assert client.get(AG).json() == []


def test_the_proposal_route_is_the_session_owners(oidc_client, token, mongo):
    map_groups(mongo, GROUP_MAPPINGS)
    admin = bearer(token("root", "daegam-admins"))
    enable(oidc_client, headers=admin)
    owner = bearer(token("dev1", "daegam-a-dev"), tenant="a")
    other = bearer(token("dev2", "daegam-a-dev"), tenant="a")
    sid = new_session(oidc_client, headers=owner)
    r = oidc_client.get(f"{A}/sessions/{sid}/proposal", headers=other)
    assert detail_code(r) == (404, "session_not_found")
    r = oidc_client.get(f"{A}/sessions/{sid}/proposal", headers=owner)
    assert detail_code(r) == (404, "proposal_not_found")


def test_fit_drops_from_the_longest_list_and_counts_what_it_dropped():
    content = {"code": "x", "issues": [{"m": "a" * 300}] * 30, "marks": [{"m": "b"}] * 2}
    out = fit(content, "issues", "marks", max_bytes=2000)
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 2000
    assert out["omitted"] == 30 - len(out["issues"]) and len(out["marks"]) == 2
