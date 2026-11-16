"""배포 게이트 (P45 12절, D109).

게이트는 리비전 밖의 Agent 설정이고, 배포, 재배포, 되돌리기가 핀하는 새 내용에만 걸린다. 지나려면
게이트 세트의 지금 지문으로 그 내용을 평가한 끝난 실행이 나이 한도 안에 있어야 하고, 센 사례와 지난
비율, 켰으면 기준선보다 나빠진 사례가 없어야 한다. 건너뛰기는 이유를 기록에 남긴다. 실행 문서는
저장소에 바로 넣고 클러스터는 fake 다.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from daegam.auth.principal import header_principal
from daegam.services.evaluation.cases import case_digest
from daegam.services.evaluation.errors import EvaluationError
from daegam.store.base import utcnow
from daegam.store.evaluation import CaseCheck, CaseContent
from daegam.store.evaluation_runs import CaseVerdict, EvaluationRun, RunActor, RunTotals
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import agent_body, agent_env, create_agent, node_of, template_for
from tests.helpers.consts import AG
from tests.helpers.evaluation import seed_set, verdict
from tests.helpers.http import detail_code

CHECKED = CaseContent(messages=["안녕"], checks=[CaseCheck(kind="answer_contains", text="안녕")])
DIGEST = case_digest(CHECKED)


@pytest.fixture
def agent(client, fake_clusters, mongo) -> DeployedAgent:
    return deployed_agent(client, fake_clusters, mongo, runtime=False)


def _gate(client, agent: DeployedAgent, body: dict[str, Any] | None):
    return client.put(f"{agent.url}/evaluation-gate", json=body)


def _greet(client, agent: DeployedAgent, text: str) -> int:
    spec = agent.spec()
    node_of(spec, "entry")["data"]["greeting"] = text
    r = client.put(agent.url, json=agent_body(spec))
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _digests(client, agent: DeployedAgent) -> dict[int, str]:
    return {r["revision"]: r["spec_digest"] for r in client.get(f"{agent.url}/revisions").json()}


def _set_digest(client, agent: DeployedAgent) -> str:
    return client.get(f"{agent.url}/evaluation-sets/set-1/cases").json()["set_digest"]


def _verdict(case: str, passed: bool, *, repeat: int = 0, counted: bool = True) -> CaseVerdict:
    return verdict(case, passed, repeat=repeat, counted=counted, digest=DIGEST)


def _cases(client, agent: DeployedAgent, names: tuple[str, ...] = ("c-1",), repeats: int = 1):
    return seed_set(client, agent.id, dict.fromkeys(names, CHECKED), repeats=repeats)


def _finished(
    client,
    agent: DeployedAgent,
    revision: int,
    *verdicts: CaseVerdict,
    ended_days_ago: float = 0,
    set_digest: str | None = None,
) -> EvaluationRun:
    runs = client.app.state.evaluation_runner.runs
    ended = utcnow() - timedelta(days=ended_days_ago)
    run = EvaluationRun(
        tenant="default",
        agent_id=agent.id,
        set_id="set-1",
        set_digest=set_digest or _set_digest(client, agent),
        revision=revision,
        spec_digest=_digests(client, agent)[revision],
        by=RunActor(sub="header"),
        state="finished",
        verdicts=list(verdicts),
        totals=RunTotals(),
        created_at=ended,
        ended_at=ended,
    )
    client.portal.call(runs.runs.insert_one, run.to_doc())
    return run


def _redeploy(client, agent: DeployedAgent, **body: Any):
    return client.post(f"{agent.url}/redeploy", json=body)


def _required(r) -> dict[str, Any]:
    assert detail_code(r) == (409, "evaluation_required"), r.text
    return r.json()["detail"]


# -- 설정 -------------------------------------------------------------------------------------


def test_the_gate_is_an_agent_setting_on_a_set_of_the_agent(client, agent):
    r = _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 0.8})
    assert r.status_code == 404
    _cases(client, agent)
    r = _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 0.8, "no_worse_cases": True})
    # 한 번의 아쉬운 답이 배포를 막지 않게, 기준선과 견주려면 반복이 둘 이상이다
    assert detail_code(r) == (422, "invalid_value")
    r = _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 0.8})
    assert r.status_code == 200, r.text
    assert r.json()["gate"]["max_age_days"] == 7 and r.json()["gate"]["updated_by"] == "header"
    shown = client.get(agent.url).json()["evaluation_gate"]
    assert (shown["set_id"], shown["min_pass_rate"], shown["no_worse_cases"]) == (
        "set-1",
        0.8,
        False,
    )
    # 게이트가 읽는 세트는 지울 수 없다
    r = client.delete(f"{agent.url}/evaluation-sets/set-1")
    assert detail_code(r) == (409, "evaluation_set_in_gate")
    r = _gate(client, agent, None)
    assert r.status_code == 200 and r.json() == {"gate": None}
    assert client.get(agent.url).json()["evaluation_gate"] is None
    assert client.delete(f"{agent.url}/evaluation-sets/set-1").status_code == 204
    r = _gate(client, agent, {"set_id": "x", "min_pass_rate": 1.5})
    assert r.status_code == 422


def test_a_gated_set_keeps_two_repeats_while_it_compares_baselines(client, agent):
    _cases(client, agent, repeats=2)
    body = {"set_id": "set-1", "min_pass_rate": 0.5, "no_worse_cases": True}
    assert _gate(client, agent, body).status_code == 200
    r = client.put(f"{agent.url}/evaluation-sets/set-1", json={"name": "평가", "repeats": 1})
    assert detail_code(r) == (422, "invalid_value") and r.json()["detail"]["field"] == "repeats"


# -- 판정 -------------------------------------------------------------------------------------


def test_new_content_needs_a_passed_run_and_unchanged_content_passes(client, mongo, agent):
    _cases(client, agent)
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    # 복제 수만 바꾼 재배포는 배포된 내용 그대로라 지난다
    assert _redeploy(client, agent, replicas=2).status_code == 200
    assert _greet(client, agent, "반갑습니다.") == 2
    before = mongo["agents"].find_one({"_id": agent.id})["deploy_history"]
    detail = _required(_redeploy(client, agent))
    assert detail["reason"] == "no_run" and detail["run"] is None
    assert mongo["agents"].find_one({"_id": agent.id})["deploy_history"] == before
    run = _finished(client, agent, 2, _verdict("c-1", True))
    r = _redeploy(client, agent)
    assert r.status_code == 200, r.text
    last = r.json()["deploy_history"][-1]
    assert (last["revision"], last["evaluation_run"], last["skipped_reason"]) == (2, run.id, None)


@pytest.mark.parametrize(
    ("reason", "verdicts", "ended_days_ago", "stale_set"),
    [
        ("stale", [("c-1", True, True)], 8, False),
        ("nothing_counted", [("c-1", True, False)], 0, False),
        ("pass_rate", [("c-1", False, True)], 0, False),
        # 그 내용의 끝난 실행이 있지만 그 뒤에 세트가 바뀌었다
        ("stale", [("c-1", True, True)], 0, True),
    ],
)
def test_a_run_that_does_not_pass_is_refused_with_its_reason(
    client, agent, reason, verdicts, ended_days_ago, stale_set
):
    _cases(client, agent)
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    _greet(client, agent, "반갑습니다.")
    made = [_verdict(c, ok, counted=counted) for c, ok, counted in verdicts]
    digest = "older" if stale_set else None
    run = _finished(client, agent, 2, *made, ended_days_ago=ended_days_ago, set_digest=digest)
    detail = _required(_redeploy(client, agent))
    assert detail["reason"] == reason
    # run 은 그 내용의 가장 새 실행, judged 는 판정한 실행이다
    assert detail["run"]["id"] == run.id and detail["run"]["state"] == "finished"
    assert detail["judged"] == (None if stale_set else detail["run"])
    if reason == "pass_rate":
        assert (detail["pass_rate"], detail["min_pass_rate"]) == (0, 1)


def test_a_skip_deploys_and_keeps_its_reason(client, agent):
    _cases(client, agent)
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    _greet(client, agent, "반갑습니다.")
    r = _redeploy(client, agent, skip_evaluation={"reason": "장애 대응"})
    assert r.status_code == 200, r.text
    last = r.json()["deploy_history"][-1]
    assert (last["skipped_reason"], last["by"], last["evaluation_run"]) == (
        "장애 대응",
        "header",
        None,
    )
    r = _redeploy(client, agent, skip_evaluation={"reason": ""})
    assert r.status_code == 422


def test_with_evaluations_off_the_gate_warns_and_does_not_apply(client, agent):
    _cases(client, agent)
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    _greet(client, agent, "반갑습니다.")
    gate = client.app.state.deploy_gate
    gate.settings = dataclasses.replace(gate.settings, evaluations_enabled=False)
    r = _redeploy(client, agent)
    assert r.status_code == 200, r.text
    assert any(w.startswith("evaluation_gate_off:") for w in r.json()["warnings"])


def test_no_worse_cases_compares_each_case_with_the_deployed_contents_baseline(client, agent):
    _cases(client, agent, ("c-1", "c-2"), repeats=2)
    body = {"set_id": "set-1", "min_pass_rate": 0, "no_worse_cases": True}
    assert _gate(client, agent, body).status_code == 200
    # 배포된 r1 의 기준선: c-1 은 지났다. c-2 의 기준선은 없다
    _finished(client, agent, 1, _verdict("c-1", True), _verdict("c-1", True, repeat=1))
    _greet(client, agent, "반갑습니다.")
    both = [_verdict("c-2", True), _verdict("c-2", True, repeat=1)]
    _finished(client, agent, 2, _verdict("c-1", False), _verdict("c-1", False, repeat=1), *both)
    detail = _required(_redeploy(client, agent))
    assert (detail["reason"], detail["cases"]) == ("worse_cases", ["c-1"])
    assert detail["offer"] == {"revision": 1}
    good = [_verdict("c-1", True), _verdict("c-1", True, repeat=1)]
    run = _finished(client, agent, 2, *good, *both)
    detail = _required(_redeploy(client, agent))
    assert (detail["reason"], detail["cases"]) == ("baseline_missing", ["c-2"])
    # 실행 하나의 읽기가 같은 비교를 싣는다
    got = client.get(f"{agent.url}/evaluation-sets/set-1/runs/{run.id}").json()
    assert got["baseline"] == {"worse": [], "missing": ["c-2"]}


def test_a_baseline_past_the_age_bound_counts_as_missing(client, agent):
    _cases(client, agent, repeats=2)
    body = {"set_id": "set-1", "min_pass_rate": 0, "no_worse_cases": True, "max_age_days": 3}
    assert _gate(client, agent, body).status_code == 200
    _finished(client, agent, 1, _verdict("c-1", True), ended_days_ago=4)
    _greet(client, agent, "반갑습니다.")
    _finished(client, agent, 2, _verdict("c-1", True), _verdict("c-1", True, repeat=1))
    assert _required(_redeploy(client, agent))["reason"] == "baseline_missing"


def test_a_rollback_is_judged_before_it_saves_and_content_that_ran_passes(client, mongo, agent):
    _cases(client, agent)
    _greet(client, agent, "반갑습니다.")  # r2 는 배포한 적이 없다
    _greet(client, agent, "어서 오세요.")
    assert _redeploy(client, agent).status_code == 200  # r3 을 배포한다
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    r = client.post(f"{agent.url}/rollback", json={"revision": 2})
    assert _required(r)["reason"] == "no_run"
    assert mongo["agents"].find_one({"_id": agent.id})["revision"] == 3
    # 전에 돈 r1 의 내용은 비상길이라 실행 없이 지난다
    r = client.post(f"{agent.url}/rollback", json={"revision": 1})
    assert r.status_code == 200, r.text
    last = r.json()["deploy_history"][-1]
    assert (last["action"], last["evaluation_run"], last["skipped_reason"]) == (
        "rollback",
        None,
        None,
    )
    r = client.post(
        f"{agent.url}/rollback", json={"revision": 2, "skip_evaluation": {"reason": "급함"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["deploy_history"][-1]["skipped_reason"] == "급함"


def test_a_deploy_after_an_undeploy_is_gated_against_the_last_deployed_content(client, agent):
    """해제한 Agent 의 새 내용도 게이트가 걸리고, 기준선은 마지막으로 배포된 기록 줄의 내용이다.
    처음 배포만 기준선 없이 지난다 (12절)."""
    _cases(client, agent, repeats=2)
    assert client.post(f"{agent.url}/undeploy").status_code == 200
    body = {"set_id": "set-1", "min_pass_rate": 0, "no_worse_cases": True}
    assert _gate(client, agent, body).status_code == 200
    _greet(client, agent, "반갑습니다.")
    r = client.post(f"{agent.url}/deploy", json={})
    assert _required(r)["reason"] == "no_run"
    # r1 에서 지난 사례가 r2 에서 지나지 못한다
    _finished(client, agent, 1, _verdict("c-1", True), _verdict("c-1", True, repeat=1))
    _finished(client, agent, 2, _verdict("c-1", False), _verdict("c-1", False, repeat=1))
    detail = _required(client.post(f"{agent.url}/deploy", json={}))
    assert (detail["reason"], detail["cases"], detail["offer"]) == (
        "worse_cases",
        ["c-1"],
        {"revision": 1},
    )
    _finished(client, agent, 2, _verdict("c-1", True), _verdict("c-1", True, repeat=1))
    r = client.post(f"{agent.url}/deploy", json={})
    assert r.status_code == 201, r.text


def test_a_first_deploy_passes_no_worse_cases_without_baselines(client, fake_clusters, mongo):
    _cid, ds, gw = agent_env(client, mongo)
    created = create_agent(client, template_for(gw, ds))
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    seed_set(client, agent_id, {"c-1": CHECKED}, repeats=2)
    body = {"set_id": "set-1", "min_pass_rate": 1, "no_worse_cases": True}
    assert client.put(f"{AG}/{agent_id}/evaluation-gate", json=body).status_code == 200
    r = client.post(f"{AG}/{agent_id}/deploy", json={})
    assert _required(r)["reason"] == "no_run"
    digest = client.get(f"{AG}/{agent_id}/revisions").json()[0]["spec_digest"]
    set_digest = client.get(f"{AG}/{agent_id}/evaluation-sets/set-1/cases").json()["set_digest"]
    run = EvaluationRun(
        tenant="default",
        agent_id=agent_id,
        set_id="set-1",
        set_digest=set_digest,
        revision=1,
        spec_digest=digest,
        by=RunActor(sub="header"),
        state="finished",
        verdicts=[_verdict("c-1", True), _verdict("c-1", True, repeat=1)],
        ended_at=utcnow(),
    )
    mongo["evaluation_runs"].insert_one(run.to_doc())
    r = client.post(f"{AG}/{agent_id}/deploy", json={})
    assert r.status_code == 201, r.text
    assert r.json()["deploy_history"][-1]["evaluation_run"] == run.id


def test_a_redeploy_judges_the_revision_its_claim_read_again(client, agent):
    """재배포는 라우트가 읽은 문서가 아니라 클레임이 다시 읽은 현재 리비전을 판정한다."""
    _cases(client, agent)
    assert _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1}).status_code == 200
    service = client.app.state.agent_service
    stale = client.portal.call(client.app.state.agents.get, agent.id)
    assert stale is not None and stale.revision == 1
    _greet(client, agent, "반갑습니다.")  # 그 사이 r2 가 저장됐다
    with pytest.raises(EvaluationError) as e:
        client.portal.call(lambda: service.redeploy(stale, principal=header_principal()))
    assert (e.value.code, e.value.extra["reason"]) == ("evaluation_required", "no_run")


def test_a_gate_whose_set_went_away_during_the_write_is_turned_off(
    client, mongo, agent, monkeypatch
):
    """게이트를 쓰는 사이 세트가 지워졌으면 다시 끄고 404 다. 세트의 삭제는 쓰기 앞의 게이트를
    보기 때문이다."""
    _cases(client, agent)
    sets = client.app.state.evaluation_sets
    original = sets.get_set
    calls = {"n": 0}

    async def vanishing(a, set_id):
        calls["n"] += 1
        if calls["n"] > 1:
            raise EvaluationError(404, "not_found", "gone")
        return await original(a, set_id)

    monkeypatch.setattr(sets, "get_set", vanishing)
    r = _gate(client, agent, {"set_id": "set-1", "min_pass_rate": 1})
    assert r.status_code == 404
    assert "evaluation_gate" not in mongo["agents"].find_one({"_id": agent.id})
