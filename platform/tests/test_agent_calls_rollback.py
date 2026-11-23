"""뒤 판이 저장하는 값을 이 판이 읽는다 (P46 A0, 8절 F0.3).

P46 은 허브 항목의 종류 `agent`, tools 노드의 소스 종류 `agent`, 출처 `agent` 와 `caller` 를 든 실행
기록, 계기 `agent` 와 대상 종류 `agent`·뿌리의 한도 종류(`long_bound`)를 든 턴 위임을 쓴다. 이 판은
그런 값들을 만나도 멈추지 않는다. 그 항목은 목록에서 빠지고 id 나 이름으로는 볼 수 있는 사람에게만
422 `hub_item_newer` 다. 그 소스는 컴파일에서 빠지고 상태가 알린다. 그 실행은 글자 그대로 읽히고
워커가 잡지 않으며 상한에 세지 않는다. 위임의 갱신은 `tests/test_turn_grants.py` 가 본다.

문서의 꼴은 P46 6.1~6.4절의 것이고 Mongo 에 바로 넣는다. 종류와 출처만은 이 시험의
이름(`LATER`)이다. P46 이 `agent` 를 이 판이 아는 값으로 만든 뒤에도 이 시험은 그다음 판이 더할 값을
본다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from daegam.services.agent_graph import check_schema
from daegam.services.runs.events import RunEvents
from daegam.services.runs.service import MAX_RUNNING, MAX_RUNS_PER_DAY
from daegam.services.runs.watch import RunWatchWorker
from daegam.store.agent_runs import ActsAs, AgentRun, MongoAgentRunStore, RunStatus, new_run
from daegam.store.base import utcnow
from daegam.store.hub import HubItem, HubK8s
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.aio import run_store
from tests.helpers.auth import bearer, map_groups
from tests.helpers.consts import AG, DEFAULT_NS, GROUP_MAPPINGS, HUB
from tests.helpers.http import api_client, detail_code, make_app

CALLED = "ag-billing"
# 뒤 판이 더할 허브 항목 종류, 소스 종류, 실행 출처. 이름은 이 시험의 것이다. P46 의 `agent` 가 그런
# 값이다
LATER = "later_kind"


# -- P46 이 쓰는 문서 ---------------------------------------------------------------------------


def _agent_item(cluster_id: str, tenant: str = "default") -> dict[str, Any]:
    """게시된 Agent 의 허브 항목 (P46 6.1절). `k8s` 는 Agent 의 이름만이고 `kind` 가 없다."""
    doc = HubItem(
        tenant=tenant,
        name="billing",
        display_name="Billing",
        kind="entry",
        cluster_id=cluster_id,
        k8s=HubK8s(name="billing", namespace=DEFAULT_NS),
    ).to_doc()
    return doc | {
        "kind": LATER,
        "source": {"agent_id": CALLED},
        "description": "Answers billing questions",
        "calls_trusted": False,
    }


# 메인 에이전트에 붙은 Agent 소스의 tools 노드와 그 간선 (P46 6.2절). 캔버스는 항목 id 를 든다
AGENT_NODE = {
    "id": "tools-billing",
    "type": "tools",
    "data": {"source": {"kind": LATER, "id": "hi-billing"}, "timeouts": {"ask_billing": 600}},
}
AGENT_EDGE = {"id": "e-billing", "source": "agent-main", "target": "tools-billing", "kind": "tools"}


def _agent_run(status: str, **fields: Any) -> dict[str, Any]:
    """다른 Agent 의 호출로 만든 실행 기록 (P46 6.3절). 소스 id 가 없고 `caller` 를 든다."""
    now = utcnow()
    base = new_run(
        tenant="default",
        agent_id=CALLED,
        source="manual",
        acts_as=ActsAs(sub="alice", username="alice"),
        now=now - timedelta(minutes=10),
        message="what is my balance?",
    ).to_doc()
    caller = {
        "agent_id": "ag-support",
        "session_id": "s-calling",
        "grant_id": "g-calling",
        "chain": ["ag-support"],
        "long_bound": False,
        "later_field": "x",
    }
    return base | {"source": LATER, "status": status, "caller": caller, **fields}


def _manual(status: RunStatus, at: datetime, **fields: Any) -> AgentRun:
    """다른 Agent 의 지금 실행 하나."""
    run = new_run(
        tenant="default",
        agent_id="ag-other",
        source="manual",
        acts_as=ActsAs(sub="bob"),
        now=at,
        message="hi",
    )
    return run.model_copy(update={"status": status, **fields})


# -- 허브 ---------------------------------------------------------------------------------------


def test_an_agent_item_is_out_of_the_list_and_its_reads_answer_hub_item_newer(
    client, mongo, fake_clusters
):
    """목록은 그 항목을 빼고, 상세·삭제·접근 정책·참조·Tool·매니페스트와 그 이름을 쓰는 생성은 500
    이 아니라 422 `hub_item_newer` 다. 이름으로 읽은 답은 항목 id 를 싣지 않는다. 이 판은 그 항목을
    지우지도 고치지도 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    doc = _agent_item(agent.cluster_id)
    mongo["hub_items"].insert_one(doc)
    stored = mongo["hub_items"].find_one({"_id": doc["_id"]})
    url = f"{HUB}/{doc['_id']}"

    r = client.get(HUB)
    assert r.status_code == 200, r.text
    assert "billing" not in [i["name"] for i in r.json()]
    for method, path, body in (
        ("GET", url, None),
        ("DELETE", url, None),
        ("PUT", f"{url}/access", {}),
        ("GET", f"{url}/references", None),
        ("GET", f"{url}/tools", None),
        ("GET", f"{url}/manifest", None),
    ):
        r = client.request(method, path, json=body)
        assert detail_code(r) == (422, "hub_item_newer"), (method, path, r.text)
        detail = r.json()["detail"]
        assert detail["hub_item_id"] == doc["_id"] and detail["fields"] == ["kind"]
    entry = {"name": "billing", "kind": "entry", "url": "https://mcp.example/mcp"}
    r = client.post(HUB, json={**entry, "transport": "streamable-http"})
    assert detail_code(r) == (422, "hub_item_newer"), r.text
    assert "hub_item_id" not in r.json()["detail"]
    assert mongo["hub_items"].find_one({"_id": doc["_id"]}) == stored


def test_an_item_of_another_tenant_is_not_found_even_when_this_release_cannot_read_it(
    oidc_client, mongo, token
):
    """볼 수 없는 사람에게는 뒤 판의 항목도 없는 항목이라 404 다. 422 의 이름과 종류가 그 항목을
    드러내지 않는다. 주인 테넌트의 사람은 422 를 받는다."""
    map_groups(mongo, GROUP_MAPPINGS)
    doc = _agent_item("c1", tenant="a")
    mongo["hub_items"].insert_one(doc)
    url = f"{HUB}/{doc['_id']}"
    dev_a = bearer(token("dev-a", "daegam-a-dev"))
    dev_b = bearer(token("dev-b", "daegam-b-dev"))

    assert detail_code(oidc_client.get(url, headers=dev_a)) == (422, "hub_item_newer")
    for path in (url, f"{url}/manifest", f"{url}/references"):
        r = oidc_client.get(path, headers=dev_b)
        assert detail_code(r) == (404, "not_found"), (path, r.text)
        assert "billing" not in r.text and LATER not in r.text


# -- 컴파일 --------------------------------------------------------------------------------------


def test_a_pod_starts_without_an_agent_source_and_the_status_warns(client, mongo, fake_clusters):
    """뒤 판이 배포한 리비전에 Agent 소스가 있다. init 의 agent.json 은 그 노드와 그 노드를
    가리키는 ref 를 빼고 스키마를 지나며, 재적용의 시작 검사도 지난다. 상태는 `tools_source_newer`
    로 그 노드를 이름 댄다. 저장의 검증은 그대로라 그 노드를 든 정의는 이 판에서 저장되지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    deployed = mongo["agents"].find_one({"_id": agent.id})["deployed_revision"]
    mongo["agents"].update_one(
        {"_id": agent.id, "revisions.revision": deployed},
        {"$push": {"revisions.$.graph.nodes": AGENT_NODE, "revisions.$.graph.edges": AGENT_EDGE}},
    )

    r = client.get(f"{agent.url}/agent.json", headers=bearer(agent.token))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert check_schema(doc) == []
    assert [t["ref"] for t in doc["tools"]] == ["tools-calculator"]
    assert doc["main"]["tools"] == ["tools-calculator"]

    service = client.app.state.agent_service
    stored = client.portal.call(service.agents.get, agent.id)
    assert client.portal.call(service._check_start, stored) is None

    warnings = client.get(agent.url).json()["status"]["warnings"]
    [newer] = [w for w in warnings if w.startswith("tools_source_newer:")]
    assert newer.endswith(f"(tools-billing: {LATER})")

    spec = mongo["agents"].find_one({"_id": agent.id})["revisions"][-1]
    body = {"name": "support", "graph": spec["graph"], "service": spec["service"]}
    r = client.put(agent.url, json=body)
    assert detail_code(r) == (422, "invalid_graph"), r.text
    assert [i["node_id"] for i in r.json()["detail"]["issues"]] == ["tools-billing"]


# -- 실행 기록 ----------------------------------------------------------------------------------


def test_the_start_and_watch_workers_lease_only_runs_of_known_sources(settings):
    """가장 오래 기다린 `starting` 기록이 Agent 의 호출이어도 시작 워커는 그것에 리스를 걸지 않고
    다음 기록을 잡는다. 지켜보기 워커도 조용한 호출과 마감이 지난 호출을 잡지 않는다. 그 기록은 리스
    없이 그대로 남아 뒤 판의 워커가 잡는다."""
    now = utcnow()

    async def body(runs: MongoAgentRunStore) -> dict[str, Any]:
        await runs.coll.insert_one(_agent_run("starting", _id="call-start"))
        await runs.insert(_manual("starting", now - timedelta(minutes=5)))
        started = [await runs.acquire_start("w1", 60, now), await runs.acquire_start("w1", 60, now)]

        quiet = now - timedelta(hours=2)
        await runs.coll.insert_many(
            [
                _agent_run("running", _id="call-quiet", last_event_at=quiet, session_id="s-a"),
                _agent_run(
                    "waiting_approval",
                    _id="call-due",
                    last_event_at=quiet,
                    deadline=now - timedelta(minutes=1),
                    session_id="s-b",
                ),
            ]
        )
        await runs.insert(_manual("running", quiet, session_id="s-m", last_event_at=quiet))
        watched = [
            await runs.acquire_watch("w1", 60, now, quiet_since=now - timedelta(minutes=30)),
            await runs.acquire_watch("w1", 60, now, quiet_since=now - timedelta(minutes=30)),
        ]
        calls = {d["_id"]: d async for d in runs.coll.find({"source": LATER})}
        return {
            "started": [r.source if r else None for r in started],
            "watched": [r.source if r else None for r in watched],
            "calls": calls,
        }

    out = run_store(settings, MongoAgentRunStore, body)
    assert out["started"] == ["manual", None]
    assert out["watched"] == ["manual", None]
    for call_id, status in (
        ("call-start", "starting"),
        ("call-quiet", "running"),
        ("call-due", "waiting_approval"),
    ):
        call = out["calls"][call_id]
        assert (call["status"], call["lease"]) == (status, None), call_id


def test_this_release_creates_runs_only_of_its_own_sources():
    """읽기는 모르는 출처를 받지만 만드는 쪽은 이 판의 출처만이다."""
    with pytest.raises(ValueError, match=LATER):
        new_run(
            tenant="default",
            agent_id=CALLED,
            source=LATER,  # type: ignore[arg-type]
            acts_as=ActsAs(sub="alice"),
            now=utcnow(),
        )


class _NoAgents:
    """지켜보기 워커의 Agent 저장소 자리다. 호출받은 Agent 가 지워진 뒤다."""

    async def get(self, _agent_id: str, *, tenant: str) -> None:
        return None


def test_the_watch_ends_a_stopped_quiet_call_of_a_later_release(settings):
    """멈추라고 했는데 턴이 끝을 알리지 않은 호출은 뒤 판의 워커가 없는 동안 아무도 끝내지 못한다.
    지켜보기 워커는 출처와 상관없이 중지 표시가 남은 조용한 `running` 기록을 잡아 표시의 까닭으로
    끝낸다. 표시가 없는 호출은 그대로 둔다."""
    quiet = utcnow() - timedelta(hours=2)

    async def body(runs: MongoAgentRunStore) -> dict[str, Any]:
        stop = {"stop_requested_at": quiet, "stop_reason": "agent_undeployed"}
        await runs.coll.insert_many(
            [
                _agent_run("running", _id="call-stopped", last_event_at=quiet, **stop),
                _agent_run("running", _id="call-quiet", last_event_at=quiet),
            ]
        )
        worker = RunWatchWorker(
            _NoAgents(),  # type: ignore[arg-type]
            runs,
            None,  # type: ignore[arg-type]
            owner="w1",
            interval=15,
            lease_seconds=60,
        )
        handled = await worker.tick()
        return {"handled": handled, "calls": {d["_id"]: d async for d in runs.coll.find({})}}

    out = run_store(settings, MongoAgentRunStore, body)
    assert out["handled"] == 1
    stopped, untouched = out["calls"]["call-stopped"], out["calls"]["call-quiet"]
    assert (stopped["status"], stopped["reason"]) == ("cancelled", "agent_undeployed")
    assert (untouched["status"], untouched["lease"]) == ("running", None)


def test_agent_runs_read_as_text_and_settle_by_their_events(settings):
    """읽기(하나, 목록, 세션, 세션 목록)는 출처를 글자 그대로 읽고 `caller` 는 아는 칸만 읽는다.
    뒤 판이 그 안에 더한 칸은 버린다. 이벤트는 그
    기록을 옮기고, Agent 를 지우는 중지는 그 기록에도 표시를 남긴다."""

    async def body(runs: MongoAgentRunStore) -> dict[str, Any]:
        doc = _agent_run("running", _id="call-1", session_id="s-a", started_at=utcnow())
        await runs.coll.insert_one(doc)
        read = [
            await runs.get("default", CALLED, "call-1"),
            *(await runs.list_for_agent("default", CALLED, limit=10)),
            await runs.for_session("default", CALLED, "s-a"),
            *(await runs.for_sessions("default", CALLED, ["s-a"])).values(),
        ]
        event = {
            "_id": "ev1",
            "event": "turn_end",
            "status": "done",
            "session_id": "s-a",
            "ts": utcnow(),
            "data": {"run_id": "call-1"},
        }
        await RunEvents(runs, None).apply(CALLED, "default", [event])  # type: ignore[arg-type]
        settled = await runs.coll.find_one({"_id": "call-1"})
        await runs.coll.insert_one(_agent_run("running", _id="call-2", session_id="s-b"))
        stopped = await runs.cancel_open_for_agent("default", CALLED, "agent_deleted", utcnow())
        return {"read": read, "settled": settled, "stopped": stopped}

    out = run_store(settings, MongoAgentRunStore, body)
    assert [(r.id, r.source) for r in out["read"]] == [("call-1", LATER)] * 4
    assert out["read"][0].caller.chain == ["ag-support"]
    assert "later_field" not in out["read"][0].model_dump()["caller"]
    assert out["settled"]["status"] == "succeeded" and out["settled"]["caller"]["agent_id"]
    [stopped] = out["stopped"]
    assert (stopped.id, stopped.stop_reason) == ("call-2", "agent_deleted")


@pytest.fixture
def runs_client(settings, mongo, fake_clusters, fake_secrets):
    app = make_app(
        settings, fake_clusters, fake_secrets, runs_enabled=True, runs_workers_enabled=False
    )
    with api_client(app) as c:
        yield c


def test_run_lists_show_the_agent_source_and_calls_take_no_run_slot(
    runs_client, mongo, fake_clusters
):
    """실행 목록과 실행 하나는 뒤 판의 출처를 그대로 낸다. 도는 호출이 동시 상한만큼 있고 오늘 만든
    호출이 하루 상한만큼 있어도 지금 실행은 상한에 걸리지 않는다. 호출에는 뒤 판의 상한이 따로
    있다. 도는 호출이 있는 Agent 도 지운다."""
    client = runs_client
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    now = utcnow()
    running = [
        _agent_run("running", _id=f"call-{i}", agent_id=agent.id, session_id=f"s-{i}")
        for i in range(MAX_RUNNING)
    ]
    done = [
        _agent_run("succeeded", _id=f"done-{i}", agent_id=agent.id, created_at=now)
        for i in range(MAX_RUNS_PER_DAY - MAX_RUNNING)
    ]
    mongo["agent_runs"].insert_many([*running, *done])

    r = client.get(f"{AG}/{agent.id}/runs")
    assert r.status_code == 200, r.text
    assert {run["source"] for run in r.json()["runs"]} == {LATER}
    r = client.get(f"{AG}/{agent.id}/runs/call-0")
    assert r.status_code == 200 and r.json()["source"] == LATER, r.text
    r = client.post(f"{AG}/{agent.id}/runs", json={"message": "check the queue"})
    assert r.status_code == 202, r.text

    r = client.delete(agent.url)
    assert r.status_code == 200, r.text
    marked = mongo["agent_runs"].find({"source": LATER, "status": "running"})
    assert {d["stop_reason"] for d in marked} == {"agent_deleted"}
