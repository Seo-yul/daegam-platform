"""P45 B1: Agent 되돌리기, 내용의 지문, 배포 기록 (6절, D109).

되돌리기는 옛 리비전의 내용을 새 리비전으로 저장하고 재배포한다. 현재 리비전과 같은 내용이면
리비전을 만들지 않고, 배포된 내용과 같으면 409 `nothing_to_roll_back`, 오늘의 플랫폼에서 검증되지
않으면 422 로 아무것도 바꾸지 않는다. 배포, 재배포, 되돌리기는 `deploying` 전이와 같은 갱신으로 기록
한 줄을 넣고, 그 배포가 끝나는 자리가 결과를 적는다. 미리 보기에는 기록이 없고 해제가 기록을
남기며 20줄을 넘지 않는다. 클러스터는 fake 이고 Mongo 는 진짜다.
"""

from __future__ import annotations

import copy
from typing import Any

import anyio
import pytest
from pydantic import Field

from daegam.auth.principal import Principal
from daegam.errors import ServiceError
from daegam.services.agent_rollback import record_deploy_end, spec_digest
from daegam.store.agents import (
    DEPLOY_HISTORY_LIMIT,
    Agent,
    AgentRevision,
    AgentSpec,
    DeployRecord,
    MongoAgentStore,
    ServiceSettings,
)
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import agent_body, node_of
from tests.helpers.aio import run_store
from tests.helpers.auth import bearer
from tests.helpers.docs import agent_doc
from tests.helpers.http import detail_code
from tests.helpers.previews import deployed_preview, plain_spec
from tests.helpers.workers import stale_tick


def _greet(client, agent: DeployedAgent, text: str) -> int:
    """인사말만 바꾼 정의를 저장하고 그 리비전 번호를 돌려준다."""
    spec = agent.spec()
    node_of(spec, "entry")["data"]["greeting"] = text
    r = client.put(agent.url, json=agent_body(spec))
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _redeploy(client, agent: DeployedAgent) -> dict[str, Any]:
    r = client.post(f"{agent.url}/redeploy")
    assert r.status_code == 200, r.text
    return r.json()


def _rows(body: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (h["action"], h["revision"], h["restored_from"], h["outcome"])
        for h in body["deploy_history"]
    ]


def _revisions(client, agent: DeployedAgent) -> dict[int, dict[str, Any]]:
    return {r["revision"]: r for r in client.get(f"{agent.url}/revisions").json()}


# -- 내용의 지문 --------------------------------------------------------------------------------


def test_spec_digest_follows_content_not_defaults_or_numbers():
    """기본값인 칸은 지문에 들지 않아 뒤 판이 기본값을 가진 칸을 더해도 지문이 같다. 리비전 번호와
    만든 때도 들지 않고, 내용이 바뀌면 지문이 바뀐다."""
    spec = AgentSpec.model_validate(plain_spec({"id": "gw-1"}, {"id": "ds-1"}))
    digest = spec_digest(spec)
    assert digest.startswith("v1:") and len(digest) == len("v1:") + 64

    # 기본값을 적어 둔 같은 내용
    explicit = spec.model_copy(deep=True)
    explicit.service.observability.record_bodies = False
    explicit.service.evaluation = None
    assert spec_digest(explicit) == digest

    # 뒤 판이 기본값을 가진 칸을 더한 모델. 기본값이면 지문이 같고 다른 값이면 다르다
    class LaterService(ServiceSettings):
        sampling: float = 1.0

    class LaterSpec(AgentSpec):
        service: LaterService = Field(default_factory=LaterService)

    later = LaterSpec(graph=spec.graph, service=LaterService(**spec.service.model_dump()))
    assert spec_digest(later) == digest
    sampled = LaterService(**spec.service.model_dump(), sampling=0.5)
    assert spec_digest(LaterSpec(graph=spec.graph, service=sampled)) != digest

    # 리비전 번호와 만든 때는 내용이 아니다
    rev = AgentRevision(revision=7, **spec.model_dump())
    assert spec_digest(rev) == digest

    changed = spec.model_copy(deep=True)
    changed.service.observability.record_bodies = True
    assert spec_digest(changed) != digest


# 오늘의 리비전 모델 기본값을 모두 적어 둔 내용과 고정한 정의 하나. 기본값을 바꾸면 저장된 리비전의
# 지문이 바뀌므로 이 두 지문이 그것을 잡는다(`spec_digest` 의 규칙)
TODAYS_DEFAULTS = {
    "graph": {"nodes": [{"id": "n", "type": "entry", "data": {}}], "edges": []},
    "service": {
        "budget": {"session_tokens": None, "daily_tokens": None},
        "observability": {"record_bodies": False},
        "evaluation": None,
    },
}
GOLDEN = {
    "graph": {
        "nodes": [
            {"id": "entry", "type": "entry", "data": {"greeting": "안녕하세요"}},
            {"id": "main", "type": "agent", "data": {}},
        ],
        "edges": [{"id": "e-entry", "source": "entry", "target": "main", "kind": "entry"}],
    },
    "service": {"budget": {"session_tokens": 1000}},
}


def test_spec_digests_of_stored_content_stay_fixed():
    bare = {"graph": {"nodes": [{"id": "n", "type": "entry"}]}}
    assert spec_digest(AgentSpec.model_validate(TODAYS_DEFAULTS)) == spec_digest(
        AgentSpec.model_validate(bare)
    )
    assert spec_digest(AgentSpec.model_validate(TODAYS_DEFAULTS)) == (
        "v1:9de72d20d10ba1bcf5da3c37c844325cfdd5f96db5907c925cb5c53f999da527"
    )
    assert spec_digest(AgentSpec.model_validate(GOLDEN)) == (
        "v1:85ddea54b191b461aa4d27b25ed470a18d8d3d2628d480ef2b7ba5ed882fa6a6"
    )


# -- 되돌리기 ----------------------------------------------------------------------------------


def test_rollback_saves_the_old_content_as_a_new_revision_and_redeploys(
    client, fake_clusters, mongo
):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    assert _redeploy(client, agent)["deployed_revision"] == 2

    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "deployed"
    assert (body["revision"], body["deployed_revision"], body["revisions"]) == (3, 3, [1, 2, 3])
    assert node_of(body, "entry")["data"]["greeting"] == "안녕하세요. 무엇을 도와드릴까요?"
    revs = _revisions(client, agent)
    assert revs[3]["graph"] == revs[1]["graph"] and revs[3]["service"] == revs[1]["service"]
    assert revs[3]["spec_digest"] == revs[1]["spec_digest"] != revs[2]["spec_digest"]

    assert _rows(body) == [
        ("deploy", 1, None, "deployed"),
        ("redeploy", 2, None, "deployed"),
        ("rollback", 3, 1, "deployed"),
    ]
    last = body["deploy_history"][-1]
    assert last["spec_digest"] == revs[1]["spec_digest"]
    assert last["image"] == body["effective_config"]["images"]["agent_image"]
    assert last["by"] == "header"
    assert last["evaluation_run"] is None and last["skipped_reason"] is None
    # 저장된 기록도 같다. 결과는 끝 전이 뒤에 적힌다
    stored = mongo["agents"].find_one({"_id": agent.id})["deploy_history"]
    assert [(h["action"], h["outcome"]) for h in stored] == [
        ("deploy", "deployed"),
        ("redeploy", "deployed"),
        ("rollback", "deployed"),
    ]
    # 리비전이 바뀐 재배포처럼 토큰이 새 리비전의 scope 를 얻는다
    tok = mongo["agent_instance_tokens"].find_one({"_id": body["token_id"]})
    assert f"agent-profile:{agent.id}:rev:3" in tok["scope"]


def test_rollback_to_the_deployed_content_is_refused_and_changes_nothing(
    client, fake_clusters, mongo
):
    """배포된 리비전 자신도, 같은 내용의 다른 리비전도 되돌릴 것이 없다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    _redeploy(client, agent)
    client.post(f"{agent.url}/rollback", json={"revision": 1})
    before = mongo["agents"].find_one({"_id": agent.id})
    for n in (3, 1):
        r = client.post(f"{agent.url}/rollback", json={"revision": n})
        assert detail_code(r) == (409, "nothing_to_roll_back"), n
        assert r.json()["detail"]["revision"] == n
    after = mongo["agents"].find_one({"_id": agent.id})
    assert after["revision"] == before["revision"] == 3
    assert after["deploy_history"] == before["deploy_history"]


def test_rollback_to_the_current_content_makes_no_revision(client, fake_clusters, mongo):
    """r1 의 내용을 r3 로 다시 저장해 두었으면 되돌리기는 리비전 없이 r3 를 재배포한다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    _redeploy(client, agent)
    r = client.put(agent.url, json=agent_body(agent.spec()))
    assert r.json()["revision"] == 3

    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["revision"], body["deployed_revision"], body["revisions"]) == (3, 3, [1, 2, 3])
    assert _rows(body)[-1] == ("rollback", 3, 1, "deployed")


def test_rollback_that_no_longer_validates_changes_nothing(client, fake_clusters, mongo):
    """r1 이 저장될 때는 맞았지만 오늘은 키 없는 provider 를 가리키면 422 이고 리비전도 기록도
    배포도 그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    _redeploy(client, agent)
    doc = mongo["agents"].find_one({"_id": agent.id})
    revisions = copy.deepcopy(doc["revisions"])
    node_of(revisions[0], "model-main")["data"]["provider"] = "nokey"
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"revisions": revisions}})
    before = mongo["agents"].find_one({"_id": agent.id})

    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert detail_code(r) == (422, "invalid_graph")
    assert r.json()["detail"]["issues"]
    after = mongo["agents"].find_one({"_id": agent.id})
    for field in ("revision", "revisions", "deployed_revision", "state", "deploy_history"):
        assert after[field] == before[field], field


def test_a_failed_agent_rolls_back(client, fake_clusters, mongo):
    """재배포가 실패해 failed 인 Agent 도 되돌린다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    agent.fake.unreachable = True
    assert detail_code(client.post(f"{agent.url}/redeploy")) == (502, "cluster_unreachable")
    agent.fake.unreachable = False
    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["deployed_revision"]) == ("deployed", 3)
    assert _rows(r.json()) == [
        ("deploy", 1, None, "deployed"),
        ("redeploy", 2, None, "failed"),
        ("rollback", 3, 1, "deployed"),
    ]


def test_a_rollback_whose_redeploy_fails_keeps_the_new_revision(client, fake_clusters, mongo):
    """저장 뒤 재배포가 실패하면 새 리비전은 남고 Agent 는 실패한 재배포가 남기는 꼴이다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    _redeploy(client, agent)
    agent.fake.unreachable = True
    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert detail_code(r) == (502, "cluster_unreachable")
    stored = mongo["agents"].find_one({"_id": agent.id})
    assert (stored["revision"], stored["deployed_revision"], stored["state"]) == (3, 3, "failed")
    revs = _revisions(client, agent)
    assert revs[3]["spec_digest"] == revs[1]["spec_digest"]
    last = stored["deploy_history"][-1]
    assert (last["action"], last["revision"], last["restored_from"], last["outcome"]) == (
        "rollback",
        3,
        1,
        "failed",
    )


def test_the_service_refuses_a_preview(client, fake_clusters, mongo):
    """미리 보기는 Agent 가 아니다. 서비스도 라우트와 같은 404 로 거절한다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    service = client.app.state.agent_service
    with pytest.raises(ServiceError) as caught:
        client.portal.call(
            lambda: service.rollback(preview, revision=1, principal=Principal(sub="header"))
        )
    assert (caught.value.status, caught.value.code) == (404, "not_found")
    assert mongo["agents"].find_one({"_id": preview.id})["revision"] == preview.revision


def test_rollback_needs_a_known_revision_and_a_deployed_agent(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    r = client.post(f"{agent.url}/rollback", json={"revision": 9})
    assert detail_code(r) == (404, "not_found")
    assert client.post(f"{agent.url}/rollback", json={"revision": 0}).status_code == 422
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert detail_code(r) == (409, "not_deployed")


def test_a_viewer_cannot_roll_back(oidc, token):
    """되돌리기는 저장과 배포다. viewer 는 agent:write 에서 막힌다."""
    viewer = bearer(token("viewer1", "daegam-a-view"))
    r = oidc.post("/agents/nope/rollback", headers=viewer, json={"revision": 1})
    assert r.status_code == 403
    assert r.json()["detail"]["scope"] == "agent:write"


# -- 배포 기록 ----------------------------------------------------------------------------------


def test_a_failed_redeploy_records_failed_and_the_reconcile_records_deployed(
    client, fake_clusters, mongo
):
    """실패한 배포는 그 줄에 failed 를 적고, 기동 뒤의 리컨실이 그 배포를 마치면 같은 줄이
    deployed 가 된다. 새 줄은 생기지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    assert _greet(client, agent, "반갑습니다.") == 2
    agent.fake.unreachable = True
    r = client.post(f"{agent.url}/redeploy")
    assert detail_code(r) == (502, "cluster_unreachable")
    stored = mongo["agents"].find_one({"_id": agent.id})
    assert stored["state"] == "failed"
    assert [(h["action"], h["revision"], h["outcome"]) for h in stored["deploy_history"]] == [
        ("deploy", 1, "deployed"),
        ("redeploy", 2, "failed"),
    ]
    agent.fake.unreachable = False
    stale_tick(client, client.app.state.reconciler)
    stored = mongo["agents"].find_one({"_id": agent.id})
    assert stored["state"] == "deployed"
    assert [(h["action"], h["outcome"]) for h in stored["deploy_history"]] == [
        ("deploy", "deployed"),
        ("redeploy", "deployed"),
    ]


def test_undeploy_keeps_the_history_and_previews_have_none(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    assert preview.deploy_history == []
    assert mongo["agents"].find_one({"_id": preview.id}).get("deploy_history") == []
    source = client.get(agent.url).json()
    assert _rows(source) == [("deploy", 1, None, "deployed")]

    r = client.post(f"{agent.url}/undeploy")
    assert r.status_code == 200 and r.json()["state"] == "draft"
    assert _rows(r.json()) == [("deploy", 1, None, "deployed")]
    r = client.post(f"{agent.url}/deploy", json={})
    assert r.status_code == 201, r.text
    assert _rows(r.json()) == [("deploy", 1, None, "deployed"), ("deploy", 1, None, "deployed")]


def test_history_keeps_the_last_rows_in_the_store(settings):
    """`$push` 의 `$slice` 가 저장된 기록을 마지막 20줄로 둔다. 메모리의 기록도 같은 줄이다."""

    async def go(store: MongoAgentStore) -> None:
        doc = agent_doc("ag-1")
        await store.create(doc)
        for n in range(DEPLOY_HISTORY_LIMIT + 3):
            record = DeployRecord(action="redeploy", revision=n + 1, spec_digest="v1:x")
            push = doc.push_deploy_record(record)
            assert await store.transition(doc, expect={"draft"}, push=push)
        stored = await store.get("ag-1")
        assert stored is not None
        assert [h.revision for h in stored.deploy_history] == list(
            range(4, DEPLOY_HISTORY_LIMIT + 4)
        )
        assert [h.id for h in stored.deploy_history] == [h.id for h in doc.deploy_history]

    run_store(settings, MongoAgentStore, go)


def test_a_deployed_row_keeps_its_outcome(settings):
    """결과 적기는 deployed 인 줄을 바꾸지 않는다."""

    async def go(store: MongoAgentStore) -> None:
        doc = agent_doc("ag-1")
        await store.create(doc)
        record = DeployRecord(action="deploy", revision=1, spec_digest="v1:x")
        assert await store.transition(doc, expect={"draft"}, push=doc.push_deploy_record(record))
        assert await store.end_deploy_record("ag-1", record.id, "failed")
        assert await store.end_deploy_record("ag-1", record.id, "deployed")
        assert not await store.end_deploy_record("ag-1", record.id, "failed")
        assert not await store.end_deploy_record("ag-1", "gone", "failed")
        stored = await store.get("ag-1")
        assert stored is not None and stored.deploy_history[0].outcome == "deployed"

    run_store(settings, MongoAgentStore, go)


@pytest.mark.parametrize(
    ("history", "deployed_revision", "written"),
    [
        # 결과를 기다리는 마지막 줄이고 핀이 같다
        ([("pending", 2)], 2, True),
        ([("failed", 2)], 2, True),
        # 이 판 없이 시작된 배포(앞 판의 재배포)를 마친다. 앞 배포의 줄은 그대로 둔다
        ([("pending", 1)], 2, False),
        # 이미 배포된 줄, 기록이 없는 문서
        ([("deployed", 2)], 2, False),
        ([], 2, False),
    ],
)
def test_deploy_ended_writes_only_the_row_of_this_deployment(history, deployed_revision, written):
    calls: list[tuple[str, str, str]] = []

    class Store:
        async def end_deploy_record(self, agent_id: str, record_id: str, outcome: str) -> bool:
            calls.append((agent_id, record_id, outcome))
            return True

    doc: Agent = agent_doc("ag-1")
    doc.deployed_revision = deployed_revision
    doc.deploy_history = [
        DeployRecord(action="redeploy", revision=rev, spec_digest="v1:x", outcome=outcome)
        for outcome, rev in history
    ]
    anyio.run(record_deploy_end, Store(), doc, "deployed")
    assert bool(calls) is written
    if written:
        assert calls == [("ag-1", doc.deploy_history[-1].id, "deployed")]
        assert doc.deploy_history[-1].outcome == "deployed"
