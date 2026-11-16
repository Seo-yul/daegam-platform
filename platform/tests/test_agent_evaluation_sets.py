"""P45 B2: 평가 세트와 사례의 저장, 지문, 상한, 범위 (7절, 13절).

세트는 Agent 하나의 것이고 사례 수 × 반복 수가 100 을 넘지 않는다. 사례마다 내용의 지문이 있고
세트의 지문은 채점 모델, 반복 수, 사례마다의 지문으로 짓는다. 채점 기준이 있는 사례는 채점 모델이
있는 세트에만 든다. 읽기는 `agent:approve`, 쓰기는 `agent:write` 를 더하고 사례 목록의 읽기는
감사에 남는다. Agent 를 지우면 그 세트와 사례도 지운다. Mongo 는 진짜다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from daegam.services.evaluation.cases import case_digest, set_digest
from daegam.store.evaluation import CaseContent, EvaluationSet, MongoEvaluationStore
from daegam.store.evaluation_runs import Grader
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import agent_env, create_agent, template_for
from tests.helpers.aio import run_store
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, T2
from tests.helpers.http import detail_code

GRADER = {"provider": "local-stub", "model": "stub"}
# `tool_called` 의 인자 JSON 상한(2000자)을 넘는 인자
LONG_ARGS = {"a": "x" * 2001}


@pytest.fixture
def env(client, mongo) -> tuple[Any, ...]:
    """Agent 를 만들 환경(클러스터, Database, 도구 소스 Gateway)이다."""
    return agent_env(client, mongo)


def _draft(client, env: tuple[Any, ...], name: str = "support") -> str:
    _cid, ds, gw = env
    r = create_agent(client, template_for(gw, ds), name=name)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def graded(client, env) -> tuple[str, str]:
    """draft Agent 하나와 채점 모델이 있는 그 세트 하나의 (Agent id, 세트 id)."""
    agent_id = _draft(client, env)
    return agent_id, _new_set(client, agent_id, grader=GRADER)["id"]


def _sets(agent_id: str) -> str:
    return f"{AG}/{agent_id}/evaluation-sets"


def _new_set(client, agent_id: str, **body: Any) -> dict[str, Any]:
    r = client.post(_sets(agent_id), json={"name": "기본", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _content(**over: Any) -> dict[str, Any]:
    return {"messages": ["환불 규정을 알려 주세요"], **over}


def _add(client, agent_id: str, set_id: str, **over: Any):
    return client.post(f"{_sets(agent_id)}/{set_id}/cases", json={"content": _content(**over)})


def test_sets_and_cases_carry_digests_and_writers(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id, grader=GRADER, repeats=2)
    assert (item["name"], item["repeats"], item["case_count"]) == ("기본", 2, 0)
    assert item["grader"] == GRADER and item["created_by"] == "header"
    empty_digest = item["digest"]
    assert empty_digest.startswith("v1:")

    r = _add(client, agent_id, item["id"], criteria=["환불 기간을 말한다"])
    assert r.status_code == 201, r.text
    case = r.json()
    assert case["created_by"] == case["updated_by"] == "header"
    assert case["content"]["expect_done"] is True and case["origin"] is None
    assert case["digest"] == case_digest(CaseContent.model_validate(case["content"]))

    listed = client.get(f"{_sets(agent_id)}/{item['id']}/cases").json()
    assert [c["id"] for c in listed["items"]] == [case["id"]]
    with_case = listed["set_digest"]
    assert with_case != empty_digest
    assert client.get(f"{_sets(agent_id)}/{item['id']}").json()["case_count"] == 1

    # 이름은 실행에 닿지 않아 지문이 그대로이고, 반복 수는 지문을 바꾼다
    body = {"name": "새 이름", "grader": GRADER, "repeats": 2}
    r = client.put(f"{_sets(agent_id)}/{item['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "새 이름" and r.json()["digest"] == with_case
    r = client.put(f"{_sets(agent_id)}/{item['id']}", json={**body, "repeats": 3})
    assert r.json()["digest"] != with_case

    # 사례를 고치면 지문과 고친 사람이 바뀐다
    url = f"{_sets(agent_id)}/{item['id']}/cases/{case['id']}"
    r = client.put(url, json={"content": _content(messages=["다른 질문"])})
    assert r.status_code == 200, r.text
    assert r.json()["digest"] != case["digest"] and r.json()["id"] == case["id"]
    assert client.get(f"{_sets(agent_id)}/{item['id']}/cases").json()["set_digest"] != with_case

    # 지우면 자리가 돌아온다
    assert client.delete(url).status_code == 204
    assert client.get(f"{_sets(agent_id)}/{item['id']}").json()["case_count"] == 0
    assert detail_code(client.delete(url)) == (404, "not_found")
    assert client.delete(f"{_sets(agent_id)}/{item['id']}").status_code == 204
    assert client.get(_sets(agent_id)).json() == []


def test_cases_times_repeats_stay_within_the_bound(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id, repeats=3)
    # 33 × 3 = 99 까지 든다. 자리 수는 저장소에서 맞춰 두고 마지막 둘을 라우트로 넣는다
    mongo["evaluation_sets"].update_one({"_id": item["id"]}, {"$set": {"case_count": 32}})
    assert _add(client, agent_id, item["id"]).status_code == 201
    r = _add(client, agent_id, item["id"])
    assert detail_code(r) == (422, "evaluation_set_full")
    assert r.json()["detail"]["max"] == 100
    url = f"{_sets(agent_id)}/{item['id']}"
    r = client.put(url, json={"name": "기본", "repeats": 1})
    assert r.status_code == 200 and r.json()["case_count"] == 33
    mongo["evaluation_sets"].update_one({"_id": item["id"]}, {"$set": {"case_count": 51}})
    assert detail_code(client.put(url, json={"name": "기본", "repeats": 2})) == (
        422,
        "evaluation_set_full",
    )
    assert client.get(url).json()["repeats"] == 1


def test_two_reservations_at_the_last_place_take_one(settings):
    """마지막 한 자리를 두 저장이 동시에 잡으면 하나만 잡는다."""

    async def go(store: MongoEvaluationStore) -> None:
        item = EvaluationSet(
            tenant="t",
            agent_id="a",
            name="n",
            repeats=3,
            case_count=32,
            created_by="u",
            updated_by="u",
        )
        await store.create_set(item)
        got = await asyncio.gather(*(store.reserve_case(item, "u") for _ in range(2)))
        assert sorted(got) == [False, True]
        stored = await store.get_set("t", "a", item.id)
        assert stored is not None and stored.case_count == 33

    run_store(settings, MongoEvaluationStore, go)


def test_criteria_need_a_grader(client, env, mongo):
    agent_id = _draft(client, env)
    plain = _new_set(client, agent_id)
    r = _add(client, agent_id, plain["id"], criteria=["정중하다"])
    assert detail_code(r) == (422, "invalid_value") and r.json()["detail"]["field"] == "grader"
    graded = _new_set(client, agent_id, grader=GRADER)
    assert _add(client, agent_id, graded["id"], criteria=["정중하다"]).status_code == 201
    url = f"{_sets(agent_id)}/{graded['id']}"
    assert detail_code(client.put(url, json={"name": "기본"})) == (422, "invalid_value")
    # 기준이 없는 세트에서는 채점 모델을 뺄 수 있다
    _add(client, agent_id, plain["id"])
    url = f"{_sets(agent_id)}/{plain['id']}"
    assert client.put(url, json={"name": "기본", "grader": GRADER}).status_code == 200
    assert client.put(url, json={"name": "기본"}).status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        {"messages": []},
        {"messages": ["q"] * 6},
        {"messages": ["  "]},
        {"messages": ["q"], "criteria": ["c"] * 11},
        {"messages": ["q"], "checks": [{"kind": "tool_called"}]},
        {"messages": ["q"], "checks": [{"kind": "answer_contains", "tool": "x", "text": "y"}]},
        {"messages": ["q"], "checks": [{"kind": "tool_not_called", "tool": "x", "args": {"a": 1}}]},
        {"messages": ["q"], "checks": [{"kind": "answer_matches", "text": "(a+)+"}]},
        {"messages": ["q"], "checks": [{"kind": "answer_contains", "text": "x"}] * 11},
        {"messages": ["q"], "reference": {"text": ""}},
        {"messages": ["q"], "unknown": 1},
        {"messages": ["q"], "checks": [{"kind": "answer_shouts", "text": "x"}]},
        {"messages": ["q"], "checks": [{"kind": "tool_called", "tool": "T", "args": LONG_ARGS}]},
    ],
)
def test_case_content_is_validated(client, graded, content):
    agent_id, set_id = graded
    r = client.post(f"{_sets(agent_id)}/{set_id}/cases", json={"content": content})
    assert r.status_code == 422, r.text
    assert client.get(f"{_sets(agent_id)}/{set_id}").json()["case_count"] == 0


def test_checks_and_simulated_results_round_trip(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id)
    checks = [
        {"kind": "tool_called", "tool": "Add", "args": {"a": 1}},
        {"kind": "tool_not_called", "tool": "Delete"},
        {"kind": "answer_contains", "text": "환불"},
        {"kind": "answer_not_contains", "text": "모릅니다"},
        {"kind": "answer_matches", "text": r"\d+일"},
    ]
    content = _content(
        checks=checks,
        reference={"text": "7일 안에 환불됩니다", "good": True},
        tool_results={"Add": "3"},
        expect_done=False,
    )
    r = client.post(f"{_sets(agent_id)}/{item['id']}/cases", json={"content": content})
    assert r.status_code == 201, r.text
    got = r.json()["content"]
    assert got["checks"] == [{"tool": None, "args": {}, "text": None, **c} for c in checks]
    assert got["tool_results"] == {"Add": "3"} and got["expect_done"] is False


def test_unknown_sets_and_cases_are_404(client, env, mongo):
    agent_id = _draft(client, env)
    other = _draft(client, env, name="other")
    item = _new_set(client, agent_id)
    case = _add(client, agent_id, item["id"]).json()
    # 다른 Agent 의 경로로는 그 세트를 찾지 못한다
    assert detail_code(client.get(f"{_sets(other)}/{item['id']}")) == (404, "not_found")
    assert detail_code(client.get(f"{_sets(agent_id)}/nope/cases")) == (404, "not_found")
    other_set = _new_set(client, agent_id)
    url = f"{_sets(agent_id)}/{other_set['id']}/cases/{case['id']}"
    assert detail_code(client.put(url, json={"content": _content()})) == (404, "not_found")
    assert detail_code(client.get(f"{AG}/nope/evaluation-sets")) == (404, "not_found")


def test_another_tenant_finds_neither_the_agent_nor_its_sets(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id)
    base = f"{_sets(agent_id)}/{item['id']}"
    for method, path, body in (
        ("GET", _sets(agent_id), None),
        ("GET", f"{base}/cases", None),
        ("POST", f"{base}/cases", {"content": _content()}),
        ("PUT", base, {"name": "x"}),
        ("DELETE", base, None),
    ):
        r = client.request(method, path, headers=T2, json=body)
        assert r.status_code == 404, (method, path, r.text)
    assert client.get(base).json()["case_count"] == 0


def test_the_case_list_read_is_audited(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id)
    mongo["audit_events"].delete_many({})
    assert client.get(f"{_sets(agent_id)}/{item['id']}/cases").status_code == 200
    assert client.get(_sets(agent_id)).status_code == 200
    rows = list(mongo["audit_events"].find({"method": "GET"}))
    assert [r["action"] for r in rows] == ["list_evaluation_cases"]
    assert rows[0]["resource_id"] == agent_id


def test_deleting_the_agent_deletes_its_sets_and_cases(client, env, mongo):
    agent_id = _draft(client, env)
    item = _new_set(client, agent_id)
    _add(client, agent_id, item["id"])
    assert client.delete(f"{AG}/{agent_id}").status_code == 200
    assert mongo["evaluation_sets"].count_documents({"agent_id": agent_id}) == 0
    assert mongo["evaluation_cases"].count_documents({"agent_id": agent_id}) == 0


def test_undeploy_keeps_the_sets_and_deleting_a_deployed_agent_removes_them(
    client, fake_clusters, mongo
):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    item = _new_set(client, agent.id)
    _add(client, agent.id, item["id"])
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    assert client.get(f"{_sets(agent.id)}/{item['id']}").json()["case_count"] == 1
    assert client.post(f"{agent.url}/deploy", json={}).status_code == 201
    assert client.delete(agent.url).status_code == 200
    assert mongo["evaluation_sets"].count_documents({"agent_id": agent.id}) == 0
    assert mongo["evaluation_cases"].count_documents({"agent_id": agent.id}) == 0


# 오늘의 사례 모델 기본값을 모두 적어 둔 사례와 고정한 사례 하나, 세트 하나. 기본값이나 지문 규칙을
# 바꾸면 이 지문들이 그것을 잡는다(`case_digest`, `set_digest`)
FULL_CASE = {
    "messages": ["환불 규정을 알려 주세요", "기간은?"],
    "reference": {"text": "7일 안입니다", "good": True},
    "criteria": ["기간을 말한다"],
    "checks": [
        {"kind": "tool_called", "tool": "Lookup", "args": {"id": 1}},
        {"kind": "answer_contains", "text": "7일"},
    ],
    "expect_done": False,
    "tool_results": {"Refund": "ok"},
}


def test_case_and_set_digests_of_stored_content_stay_fixed():
    bare = CaseContent.model_validate({"messages": ["q"]})
    written = CaseContent.model_validate(
        {
            "messages": ["q"],
            "reference": None,
            "criteria": [],
            "checks": [],
            "expect_done": True,
            "tool_results": {},
        }
    )
    assert (
        case_digest(bare)
        == case_digest(written)
        == ("v1:974261bbe369a5e020b4d28810ffd09010be9d67bef74d4f980269361bbccbc7")
    )
    assert case_digest(CaseContent.model_validate(FULL_CASE)) == (
        "v1:dc1f90f5088fb618067bac901fdbc6ad521dac0bb65e99c25e474eb491105560"
    )
    item = EvaluationSet(
        id="set-1",
        tenant="t",
        agent_id="a",
        name="n",
        grader=Grader(provider="p", model="m"),
        repeats=2,
        created_by="u",
        updated_by="u",
    )
    assert set_digest(item, [("c-2", "v1:b"), ("c-1", "v1:a")]) == (
        "v1:02aae84141695f15518ce62e12714c740c0f74f2c86e3450479df47af6eaf176"
    )
    # 사례의 차례와 세트 이름은 지문에 들지 않는다
    renamed = item.model_copy(update={"name": "다른 이름"})
    assert set_digest(renamed, [("c-1", "v1:a"), ("c-2", "v1:b")]) == set_digest(
        item, [("c-2", "v1:b"), ("c-1", "v1:a")]
    )


def test_a_viewer_reads_nothing_and_writes_nothing(oidc, token):
    viewer = bearer(token("v1", "daegam-a-view"), tenant="a")
    base = f"{AG}/nope/evaluation-sets"
    for method, path in (("get", base), ("get", f"{base}/s/cases")):
        r = oidc.request(method.upper(), path, headers=viewer)
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:approve", path
    r = oidc.post(base, headers=viewer, json={"name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:approve"
