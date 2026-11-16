"""평가 미리 보기 (P45 9절·10절·16절, D109).

평가 실행은 고른 리비전으로 미리 보기를 연다. 표시의 `run` 이 그 실행이고 시작한 사람이 주인이다.
사람의 미리 보기와 함께 열리고, 서랍과 미리 보기 라우트에는 닿지 않으며, 열린 미리 보기 목록에는
실행과 함께 든다. 본문 기록은 꺼 두고, agent.json 은 쓰기 Tool 을 잡으며 파드 안의 승인 기능을
요구한다. 끝나도 서랍이 읽는 끝 기록은 남지 않는다. 옛 이름의 고유 인덱스는 시작마다 지운다.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from daegam.services.agent_features import IN_POD_STATE, KNOWN
from daegam.services.agent_preview import PreviewError
from daegam.services.agents import AgentError
from daegam.services.evaluation.holds import HELD_DECISIONS
from daegam.store.agents import OLD_PREVIEW_OWNER_INDEX, PREVIEW_OWNER_INDEX, PREVIEWS_ONLY
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import agent_deployment, deployed_agent
from tests.helpers.agent_sleep import in_app
from tests.helpers.agents import agent_body, node_of
from tests.helpers.gateways import backend_snapshot, seed_deployed_backends
from tests.helpers.http import api_client, make_app
from tests.helpers.previews import (
    HEADER_SUB,
    agent_json,
    preview_service,
    source_doc,
    start_evaluation_preview,
    start_preview,
)


def _second_revision(client, agent) -> None:
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "두 번째 인사"
    changed["service"]["observability"] = {"record_bodies": True}
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200


def _run_preview(client, agent_id: str, run: str):
    store = client.app.state.agents.with_previews()
    return client.portal.call(store.preview_of_run, "default", agent_id, run)


def test_an_evaluation_preview_runs_the_chosen_revision_beside_the_persons_own(
    client, fake_clusters, mongo
):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    _second_revision(client, agent)
    mine = start_preview(client, agent.id)
    run = start_evaluation_preview(client, agent.id, 1, "run-1")
    assert mine.id != run.id and mine.revision == 2
    assert run.preview is not None and run.preview.run == "run-1" and run.evaluation_run == "run-1"
    assert run.preview.owner == HEADER_SUB and run.state == "deployed"
    assert run.revision == 1 and run.deployed_revision == 1
    assert run.spec.graph.node("memory") is None
    # 원본의 r2 는 본문을 기록하지만 평가 미리 보기는 끈다
    again = start_evaluation_preview(client, agent.id, 2, "run-2", sub="dev-b")
    assert again.spec.service.observability.record_bodies is False
    # 서랍은 그 사람의 미리 보기만 찾는다
    source = source_doc(client, agent.id)
    previews = preview_service(client)
    assert in_app(client, previews.require_mine, source, HEADER_SUB).id == mine.id
    with pytest.raises(PreviewError):
        in_app(client, previews.require_mine, source, "dev-b")
    # 열린 미리 보기 목록에는 실행과 함께 든다
    listed = client.get(f"{agent.url}/previews").json()["previews"]
    assert {(p["id"], p["run"]) for p in listed} == {
        (mine.id, None),
        (run.id, "run-1"),
        (again.id, "run-2"),
    }
    # 자리 셋, 그 사람의 시작 둘
    assert mongo["agent_preview_slots"].count_documents({}) == 3
    starts = {d["_id"]: d["count"] for d in mongo["agent_preview_starts"].find()}
    assert [n for key, n in starts.items() if key.startswith(f"default:{HEADER_SUB}")] == [2]
    # 같은 실행을 다시 열면 그 미리 보기다
    assert start_evaluation_preview(client, agent.id, 1, "run-1").id == run.id


def test_an_evaluation_preview_holds_writes_and_requires_in_pod_state(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    run = start_evaluation_preview(client, agent.id, 1, "run-1")
    r = agent_json(client, agent.fake, run, KNOWN)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert IN_POD_STATE in doc["features"]
    assert doc["service"]["observability"]["record_bodies"] is False
    [source] = doc["tools"]
    assert source["allow"] == ["Add", "Divide"]
    assert {n: source["approval"][n] for n in source["allow"]} == {
        "Add": list(HELD_DECISIONS),
        "Divide": list(HELD_DECISIONS),
    }
    # 원본의 agent.json 은 노드의 승인 그대로다
    plain = agent_json(client, agent.fake, source_doc(client, agent.id), KNOWN).json()
    assert plain["tools"][0]["approval"] == {"Divide": ["approve", "reject"]}
    # 파드 안의 승인을 모르는 이미지는 init 에서 멈춘다
    old = agent_json(client, agent.fake, run, KNOWN - {IN_POD_STATE})
    assert old.status_code == 409 and old.json()["detail"]["code"] == "runtime_image_too_old"


def test_a_source_without_known_names_refuses_the_evaluation_compile(client, fake_clusters, mongo):
    """이름을 모르는 소스는 쓰기를 가를 수 없다. 그래서 시작이 파드를 띄우기 전에 409
    `evaluation_tools_unknown` 과 그 노드로 멈추고 미리 보기의 `last_error` 에 남긴다. init 받기도
    같은 409 다 (10절). Gateway 의 백엔드가 광고하는 이름이 없으면 이름을 모른다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    spec = copy.deepcopy(agent.spec())
    node_of(spec, "tools-calculator")["data"]["allow_tools"] = []
    assert client.put(agent.url, json=agent_body(spec)).status_code == 200
    seed_deployed_backends(mongo, agent.gw["id"], [backend_snapshot("spike", "remote", [])])
    with pytest.raises(AgentError) as refused:
        start_evaluation_preview(client, agent.id, 2, "run-1")
    assert (refused.value.status, refused.value.code) == (409, "evaluation_tools_unknown")
    assert refused.value.extra["nodes"] == ["tools-calculator"]
    # 설정 지문의 컴파일에서 멈춰 Deployment 가 없다
    run = _run_preview(client, agent.id, "run-1")
    assert run is not None and run.state == "failed"
    assert run.last_error is not None and "evaluation_tools_unknown" in run.last_error
    assert run.k8s is not None and agent_deployment(agent.fake, run.k8s.name) is None
    r = agent_json(client, agent.fake, run, KNOWN)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "evaluation_tools_unknown"


def test_ending_an_evaluation_preview_leaves_the_persons_drawer_alone(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    run = start_evaluation_preview(client, agent.id, 1, "run-1")
    assert in_app(client, preview_service(client).end, run, "ended", by=HEADER_SUB)
    assert mongo["agents"].find_one({"_id": run.id}) is None
    assert mongo["agent_preview_slots"].count_documents({}) == 0
    assert mongo["agent_preview_ends"].count_documents({}) == 0


def test_an_evaluation_start_that_stopped_before_its_deploy_is_started_again(
    client, fake_clusters, mongo
):
    """시작이 문서를 넣고 배포 전에 멈췄으면 남은 draft 는 다음 시작이 지우고 새로 연다. 지우는
    중인 것은 409 `in_progress` 다."""
    from daegam.services.agent_preview import _new_preview, evaluation_spec

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    source = source_doc(client, agent.id)
    stuck = _new_preview(source, HEADER_SUB, utcnow(), evaluation_spec(source.current), run="run-1")
    in_app(client, client.app.state.agents.create, stuck)
    fresh = start_evaluation_preview(client, agent.id, 1, "run-1")
    assert fresh.id != stuck.id and fresh.state == "deployed"
    assert mongo["agents"].find_one({"_id": stuck.id}) is None
    mongo["agents"].update_one({"_id": fresh.id}, {"$set": {"state": "deleting"}})
    with pytest.raises(PreviewError) as e:
        start_evaluation_preview(client, agent.id, 1, "run-1")
    assert (e.value.status, e.value.code) == (409, "in_progress")


def test_starting_an_evaluation_preview_obeys_the_preview_rules(
    settings, mongo, fake_clusters, fake_secrets
):
    app = make_app(
        dataclasses.replace(settings, agent_previews_per_tenant=1), fake_clusters, fake_secrets
    )
    with api_client(app) as client:
        agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
        with pytest.raises(PreviewError) as e:
            start_evaluation_preview(client, agent.id, 9, "run-1")
        assert (e.value.status, e.value.code) == (404, "not_found")
        start_preview(client, agent.id)
        with pytest.raises(PreviewError) as e:
            start_evaluation_preview(client, agent.id, 1, "run-1")
        assert (e.value.status, e.value.code) == (409, "preview_limit")
        assert mongo["agents"].count_documents({"preview.run": "run-1"}) == 0
        preview_service(client).settings = dataclasses.replace(
            settings, agent_previews_enabled=False
        )
        with pytest.raises(PreviewError) as e:
            start_evaluation_preview(client, agent.id, 1, "run-1")
        assert (e.value.status, e.value.code) == (409, "preview_disabled")


def test_the_start_drops_the_old_preview_index(client, mongo):
    """옛 이름의 인덱스가 남은 저장소에서도 시작은 새 이름의 인덱스 하나만 남긴다 (16절, F0.1)."""
    agents = mongo["agents"]
    agents.drop_index(PREVIEW_OWNER_INDEX)
    agents.create_index(
        [("preview.source", ASCENDING), ("preview.owner", ASCENDING)],
        unique=True,
        name=OLD_PREVIEW_OWNER_INDEX,
        partialFilterExpression=PREVIEWS_ONLY,
    )
    store = client.app.state.agents
    in_app(client, store.init_schema)
    in_app(client, store.init_schema)
    names = set(agents.index_information())
    assert PREVIEW_OWNER_INDEX in names and OLD_PREVIEW_OWNER_INDEX not in names


def test_the_index_holds_one_preview_per_person_and_one_per_run(mongo, client):
    agents = mongo["agents"]

    def preview(doc_id: str, **mark):
        base = {"source": "ai-1", "source_name": "a", "owner": "dev-a"}
        doc = {"_id": doc_id, "tenant": "default", "name": doc_id, "preview": {**base, **mark}}
        agents.insert_one(doc)

    preview("p-1")
    with pytest.raises(DuplicateKeyError):
        preview("p-2", run=None)
    preview("p-3", run="run-1")
    preview("p-4", run="run-2")
    with pytest.raises(DuplicateKeyError):
        preview("p-5", run="run-1")
