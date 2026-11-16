"""평가 실행의 라우트 (P45 9절, 13절).

시작은 본 세트의 지문을 싣고 하루 상한의 자리를 잡으며 Agent 마다 하나만 돈다. 목록은 부른 사람이
확인할 사례를 함께 준다. 실행 하나의 판정은 승인자 모두가 보고 결과의 내용은 시작한 사람만 본다.
취소와, 열린 미리 보기 목록에서 평가 미리 보기를 끝내는 일은 실행을 취소하고 그 미리 보기를 끝낸다.
"""

from __future__ import annotations

import dataclasses

import pytest

from daegam.store.evaluation import CaseCheck, CaseContent
from tests.helpers.agent_runtime import deployed_agent, set_ready
from tests.helpers.consts import AG
from tests.helpers.evaluation import drive, seed_set
from tests.helpers.http import detail_code

CONTENT = CaseContent(
    messages=["write"],
    tool_results={"Add": "가상 결과"},
    checks=[CaseCheck(kind="answer_contains", text="가상 결과")],
)


@pytest.fixture
def seeded(client, evaluated):
    """`evaluated` 의 Agent 에 사례 하나의 세트를 넣는다."""
    agent, _runtime = evaluated
    seed_set(client, agent.id, {"c-1": CONTENT})
    return evaluated


def _runs(agent) -> str:
    return f"{AG}/{agent.id}/evaluation-sets/set-1/runs"


def _digest(client, agent) -> str:
    return client.get(f"{AG}/{agent.id}/evaluation-sets/set-1/cases").json()["set_digest"]


def _start(client, agent, **body):
    return client.post(_runs(agent), json={"set_digest": _digest(client, agent), **body})


def test_a_run_starts_runs_and_shows_its_results(client, mongo, seeded):
    agent, runtime = seeded
    r = _start(client, agent)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["state"] == "queued" and run["started_by"] == "header" and run["header_mode"]
    assert (run["cases"], run["repeats"], run["revision"]) == (1, 1, 1)
    fresh = drive(client, run["id"])
    preview = mongo["agents"].find_one({"_id": fresh.preview_id})
    set_ready(agent.fake, f"agent-{preview['_id']}")
    assert drive(client, run["id"]).state == "finished"
    got = client.get(f"{_runs(agent)}/{run['id']}")
    assert got.status_code == 200, got.text
    detail = got.json()
    assert detail["state"] == "finished" and detail["content_hidden"] is False
    [result] = detail["results"]
    assert (
        result["answer"] == "결과: 가상 결과" and result["tool_calls"][0]["answer"] == "simulated"
    )
    assert detail["totals"]["passed"] == 1 and detail["done_steps"] == 1
    listed = client.get(_runs(agent)).json()
    assert [i["id"] for i in listed["items"]] == [run["id"]]
    # 결과의 읽기는 감사에 남는다
    assert mongo["audit_events"].count_documents({"path": {"$regex": f"runs/{run['id']}$"}}) >= 1


def test_only_the_starter_sees_the_content(client, mongo, seeded):
    agent, _runtime = seeded
    run = _start(client, agent).json()
    mongo["evaluation_runs"].update_one({"_id": run["id"]}, {"$set": {"by.sub": "dev-x"}})
    mongo["evaluation_results"].insert_one(
        {"_id": "r", "run_id": run["id"], "case_id": "c-1", "answer": "비밀"}
    )
    detail = client.get(f"{_runs(agent)}/{run['id']}").json()
    assert detail["content_hidden"] is True and detail["results"] == []


def test_a_start_is_refused_for_each_reason(client, settings, mongo, seeded):
    agent, _runtime = seeded
    r = client.post(_runs(agent), json={"set_digest": "stale"})
    assert detail_code(r) == (409, "evaluation_set_changed")
    assert r.json()["detail"]["set_digest"] == _digest(client, agent)
    r = _start(client, agent, revision=9)
    assert r.status_code == 404
    assert _start(client, agent).status_code == 202
    r = _start(client, agent)
    assert detail_code(r) == (409, "evaluation_run_active")
    # 거절된 시작은 하루 자리를 쓰지 않는다
    assert [d["count"] for d in mongo["evaluation_run_days"].find()] == [1]
    runs = client.app.state.evaluation_runs
    runs.settings = dataclasses.replace(runs.settings, evaluation_runs_per_day=1)
    mongo["evaluation_runs"].delete_many({})
    r = _start(client, agent)
    assert detail_code(r) == (429, "evaluation_run_limit")
    runs.settings = dataclasses.replace(runs.settings, evaluations_enabled=False)
    r = _start(client, agent)
    assert detail_code(r) == (409, "evaluations_disabled")
    runs.settings = dataclasses.replace(settings)
    previews = client.app.state.agent_previews
    previews.settings = dataclasses.replace(previews.settings, agent_previews_enabled=False)
    r = _start(client, agent)
    assert detail_code(r) == (409, "preview_disabled")


def test_a_grader_on_a_provider_the_revision_does_not_use_is_refused(client, mongo, seeded):
    """채점 기준이 있는 사례가 든 세트의 채점 모델은 리비전의 모델 자리가 쓰는 provider 라야 한다
    (11절). 시작한 실행은 채점 모델과 리비전 내용의 지문을 적는다."""
    agent, _runtime = seeded
    graded = {"$set": {"content.criteria": ["가상 결과를 말한다"]}}
    mongo["evaluation_cases"].update_many({}, graded)
    mongo["evaluation_sets"].update_one(
        {"_id": "set-1"}, {"$set": {"grader": {"provider": "elsewhere", "model": "m"}}}
    )
    r = _start(client, agent)
    assert detail_code(r) == (409, "evaluation_grader_provider")
    assert r.json()["detail"]["provider"] == "elsewhere"
    provider = mongo["agents"].find_one({"_id": agent.id})["spec"]["graph"]["nodes"]
    used = next(n["data"]["provider"] for n in provider if n["id"] == "model-main")
    mongo["evaluation_sets"].update_one(
        {"_id": "set-1"}, {"$set": {"grader": {"provider": used, "model": "m"}}}
    )
    r = _start(client, agent)
    assert r.status_code == 202, r.text
    stored = mongo["evaluation_runs"].find_one({"_id": r.json()["id"]})
    assert stored["grader"] == {"provider": used, "model": "m"}
    assert stored["spec_digest"].startswith("v1:")


def test_an_empty_set_has_nothing_to_run(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    seed_set(client, agent.id, {})
    r = _start(client, agent)
    assert detail_code(r) == (422, "invalid_value")


def test_the_review_lists_cases_others_wrote_or_changed(client, mongo, seeded):
    """확인할 사례는 마지막 실행이 기록한 (id, 지문)에 없고 다른 사람이 쓰거나 고친 것이다."""
    agent, _runtime = seeded
    mongo["evaluation_cases"].update_one({"_id": "c-1"}, {"$set": {"updated_by": "dev-x"}})
    review = client.get(_runs(agent)).json()["review"]
    assert review["cases"] == ["c-1"] and review["since"] is None
    assert review["set_digest"] == _digest(client, agent)
    run = _start(client, agent).json()
    client.delete(f"{_runs(agent)}/{run['id']}")
    # 그 지문으로 시작했으므로 다시 볼 것이 없다
    assert client.get(_runs(agent)).json()["review"]["cases"] == []
    # 다른 사람이 다시 고쳐 지문이 바뀌면 다시 보인다
    mongo["evaluation_cases"].update_one({"_id": "c-1"}, {"$set": {"digest": "changed"}})
    assert client.get(_runs(agent)).json()["review"]["cases"] == ["c-1"]


def test_cancelling_ends_the_run_and_its_preview(client, mongo, seeded):
    agent, _runtime = seeded
    run = _start(client, agent).json()
    fresh = drive(client, run["id"])
    assert fresh.state == "starting" and fresh.preview_id is not None
    r = client.delete(f"{_runs(agent)}/{run['id']}")
    assert r.status_code == 204, r.text
    doc = mongo["evaluation_runs"].find_one({"_id": run["id"]})
    assert doc["state"] == "cancelled" and doc["cancelled_by"] == "header"
    assert mongo["agents"].find_one({"_id": fresh.preview_id}) is None
    # 준비 전에 끝났으므로 하루 자리를 돌려준다
    assert [d["count"] for d in mongo["evaluation_run_days"].find()] == [0]
    assert client.delete(f"{_runs(agent)}/{run['id']}").status_code == 409


def test_ending_the_evaluation_preview_from_the_open_previews_cancels_its_run(
    client, mongo, seeded
):
    agent, _runtime = seeded
    run = _start(client, agent).json()
    fresh = drive(client, run["id"])
    r = client.delete(f"{AG}/{agent.id}/previews/{fresh.preview_id}")
    assert r.status_code == 200, r.text
    doc = mongo["evaluation_runs"].find_one({"_id": run["id"]})
    assert doc["state"] == "cancelled" and doc["cancelled_by"] == "header"
    assert mongo["agents"].find_one({"_id": fresh.preview_id}) is None
