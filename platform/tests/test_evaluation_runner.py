"""평가 실행기와 확인 항목 (P45 9절, 10절, 11절, D109).

실행기는 시작한 사람의 지금 주체로 고른 리비전의 평가 미리 보기를 열고, 준비되면 사례와 반복마다 새
대화를 열어 질문을 차례로 보내며, 잡힌 쓰기는 가상 결과로 respond 하거나 reject 하고, 기억한 선택은
싣지 않으며, 결과와 내용 없는 판정을 남기고, 마지막 사례 뒤에 미리 보기를 끝낸다. 걸음의 쓰기는
실행을 맡은 레플리카와 다음 걸음을 조건으로 걸고, 한도와 사람의 확인에 걸리면 그 까닭으로 끝난다.
가짜 런타임은 `tests/helpers/evaluation.py` `scripted_runtime` 이고 픽스처 `evaluated` 도 거기
있다.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import anyio
import pytest

from daegam.services.agent_runtime import ProxyResponse, sse_events
from daegam.services.evaluation import checks, pii
from daegam.services.evaluation.runner import MAX_ATTEMPTS, answers
from daegam.services.turn_grants import OpenedGrant, OpenOutcome
from daegam.store.base import utcnow
from daegam.store.evaluation import CaseCheck, CaseContent, CaseReference
from daegam.store.evaluation_runs import EvaluationRun, ToolCallRecord
from daegam.store.memberships import Membership
from daegam.store.usage import TokenUsage, UsageSource
from tests.helpers.agent_runtime import deployed_agent, set_ready
from tests.helpers.agent_sleep import in_app
from tests.helpers.agents import agent_body, deploy_spec, node_of, template_for
from tests.helpers.evaluation import (
    bodies_json,
    decisions_sent,
    drive,
    new_run,
    scripted_runtime,
    seed_set,
    take_day,
)
from tests.helpers.skills import created_skill
from tests.helpers.usage import usage_row

# -- 확인 항목 ------------------------------------------------------------------------------


def _seen(answer: str = "환불은 7일", turns: tuple[str, ...] = ("done",)) -> checks.Observed:
    calls = [ToolCallRecord(name="Lookup", args={"q": "환불 기간", "n": 3}, answer="ran")]
    return checks.Observed(turns=list(turns), calls=calls, answer=answer)


@pytest.mark.parametrize(
    ("check", "passed"),
    [
        (CaseCheck(kind="tool_called", tool="Lookup"), True),
        (CaseCheck(kind="tool_called", tool="Lookup", args={"q": "환불", "n": 3}), True),
        (CaseCheck(kind="tool_called", tool="Lookup", args={"n": 4}), False),
        (CaseCheck(kind="tool_called", tool="Add"), False),
        (CaseCheck(kind="tool_not_called", tool="Add"), True),
        (CaseCheck(kind="tool_not_called", tool="Lookup"), False),
        (CaseCheck(kind="answer_contains", text="7일"), True),
        (CaseCheck(kind="answer_not_contains", text="7일"), False),
        (CaseCheck(kind="answer_matches", text=r"\d+일"), True),
        (CaseCheck(kind="answer_matches", text=r"^없음$"), False),
        (CaseCheck(kind="later_kind", text="x"), False),
    ],
)
def test_each_check_judges_the_kept_stream(check: CaseCheck, passed: bool) -> None:
    verdict = checks.check_one(check, _seen())
    assert verdict.passed is passed and (verdict.gap is None) is passed


def test_a_pattern_that_runs_past_its_time_limit_fails(monkeypatch) -> None:
    """`pii_mask` 식과 같은 시간 한도다. 되돌아가기가 긴 식은 지나지 못한다."""
    monkeypatch.setattr(pii, "PATTERN_TIMEOUT_SECONDS", 0.05)
    verdict = checks.check_one(
        CaseCheck(kind="answer_matches", text=r"(\d|\d)+x"), _seen(answer="1" * 40 + "yx")
    )
    assert not verdict.passed and verdict.gap is not None and "time limit" in verdict.gap


def test_done_is_checked_first_and_only_checks_count() -> None:
    content = CaseContent(messages=["q"], checks=[CaseCheck(kind="answer_contains", text="7")])
    verdicts = checks.check_all(content, _seen(turns=("done", "error:turn_timeout")))
    assert [(v.kind, v.passed) for v in verdicts] == [("done", False), ("answer_contains", True)]
    [none] = checks.check_all(CaseContent(messages=["q"]), _seen(turns=()))
    assert not none.passed and none.gap == "no turn ran"
    assert checks.counted(content) and not checks.counted(CaseContent(messages=["q"]))


def test_held_calls_are_answered_by_simulated_results_or_rejected() -> None:
    payload = {
        "action_requests": [{"name": "Add"}, {"name": "Post"}, {"name": "Lookup"}],
        "review_configs": [
            {"allowed_decisions": ["respond", "reject"]},
            {"allowed_decisions": ["respond", "reject"]},
            {"allowed_decisions": ["approve", "reject"]},
        ],
    }
    decisions, answered = answers(payload, {"Add": "3"})
    assert [d["type"] for d in decisions] == ["respond", "reject", "approve"]
    assert decisions[0]["message"] == "3"
    assert [a for _n, _args, a in answered] == ["simulated", "rejected", "ran"]


def test_the_stream_reader_parses_events_across_chunks() -> None:
    async def body():
        yield b'event: token\ndata: {"text": "a"}\n'
        yield b'\nevent: done\ndata: {"text":\ndata: "b"}\n\n: ping\n\n'

    async def read() -> list[tuple[str, dict[str, Any]]]:
        proxied = ProxyResponse(status=200, headers={}, body=body())
        return [e async for e in sse_events(proxied)]

    assert anyio.run(read) == [("token", {"text": "a"}), ("done", {"text": "b"})]


# -- 실행 -------------------------------------------------------------------------------------

WRITE_SIMULATED = CaseContent(
    messages=["write"],
    tool_results={"Add": "가상 결과 3"},
    checks=[
        CaseCheck(kind="tool_called", tool="Add"),
        CaseCheck(kind="answer_contains", text="가상 결과 3"),
    ],
)
WRITE_REJECTED = CaseContent(
    messages=["write"], checks=[CaseCheck(kind="answer_contains", text="가상")]
)
READ_TWO_TURNS = CaseContent(
    messages=["안녕", "read"],
    reference=CaseReference(text="7일"),
    checks=[
        CaseCheck(kind="tool_called", tool="Lookup", args={"q": "환불"}),
        CaseCheck(kind="tool_not_called", tool="Add"),
        CaseCheck(kind="answer_matches", text=r"\d일"),
    ],
)


def _preview_of(client, run: EvaluationRun):
    return client.portal.call(client.app.state.agents.with_previews().get, run.preview_id)


def _start(client, agent, run: EvaluationRun) -> EvaluationRun:
    """미리 보기를 열고 준비시킨다. 다음 조각에서 `running` 이다."""
    run = drive(client, run.id)
    assert run.state == "starting" and run.preview_id is not None, run
    preview = _preview_of(client, run)
    assert preview is not None and preview.k8s is not None
    set_ready(agent.fake, preview.k8s.name)
    return run


def _chats(runtime) -> list[dict[str, Any]]:
    return [b for kind, b in runtime.state.bodies if kind == "chat"]


def test_a_run_answers_every_case_with_writes_held_and_ends_its_preview(client, mongo, evaluated):
    agent, runtime = evaluated
    cases = seed_set(
        client,
        agent.id,
        {"c-sim": WRITE_SIMULATED, "c-rej": WRITE_REJECTED, "c-read": READ_TWO_TURNS},
    )
    day = take_day(client)
    run = _start(client, agent, new_run(client, agent.id, cases, day=day))
    preview_id = run.preview_id
    run = drive(client, run.id)
    assert run.state == "finished", run
    assert run.active is None and run.driver is None
    # 쓰기는 불리지 않았다. 결정은 respond 와 reject 뿐이고 기억한 선택은 없다
    assert runtime.state.backend == 0
    assert [d["type"] for d in decisions_sent(runtime)] == ["respond", "reject"]
    assert "approval_choices" not in bodies_json(runtime)
    # 사례마다 새 대화이고 끝나면 지운다. 두 질문은 한 대화다
    assert [b["session_id"] for b in _chats(runtime)] == [None, None, None, "s-3"]
    assert sorted(runtime.state.deleted) == ["s-1", "s-2", "s-3"]
    verdicts = {v.case_id: v for v in run.verdicts}
    assert {k: (v.state, v.checks_passed) for k, v in verdicts.items()} == {
        "c-sim": ("done", True),
        "c-rej": ("done", False),
        "c-read": ("done", True),
    }
    assert (run.totals.cases, run.totals.counted, run.totals.passed) == (3, 3, 2)
    results = {r["case_id"]: r for r in mongo["evaluation_results"].find({"run_id": run.id})}
    assert set(results) == {"c-sim", "c-rej", "c-read"}
    # 재개한 턴이 대기하던 호출을 다시 알려도 호출은 하나이고 답한 꼴을 지킨다
    [call] = results["c-sim"]["tool_calls"]
    assert (call["name"], call["answer"], call["args"]) == ("Add", "simulated", {"a": 1, "b": 2})
    [call] = results["c-rej"]["tool_calls"]
    assert (call["answer"], call["status"]) == ("rejected", "error")
    assert results["c-read"]["tool_calls"][0]["answer"] == "ran"
    assert results["c-read"]["answer"] == "환불은 7일 안에 한다"
    assert results["c-read"]["turns"] == ["done", "done"]
    assert results["c-read"]["tokens"] == {"input": 20, "output": 10}
    assert results["c-sim"]["started_by"] == "header"
    # 미리 보기는 끝났고 하루 자리는 남는다
    assert mongo["agents"].find_one({"_id": preview_id}) is None
    assert mongo["agent_preview_slots"].count_documents({}) == 0
    assert mongo["evaluation_run_days"].find_one({"_id": day})["count"] == 1


def test_runtime_refusals_and_blocked_arguments_read_refused(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": CaseContent(messages=["refused"])})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases)).id)
    [result] = list(mongo["evaluation_results"].find({"run_id": run.id}))
    assert [(c["name"], c["answer"]) for c in result["tool_calls"]] == [
        ("web_fetch", "refused"),
        ("Lookup", "refused"),
        ("Lookup", "ran"),
    ]


def test_a_case_changed_during_the_run_is_not_run_or_counted(client, mongo, evaluated):
    agent, runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": READ_TWO_TURNS, "c-2": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases))
    # 앞선 시도가 남긴 대화가 있어도 넘기기 전에 멈추고 지운다
    mongo["evaluation_runs"].update_one({"_id": run.id}, {"$set": {"step.session_id": "s-left"}})
    mongo["evaluation_cases"].update_one({"_id": "c-1"}, {"$set": {"digest": "other"}})
    run = drive(client, run.id)
    assert run.state == "finished"
    assert runtime.state.deleted[0] == "s-left"
    assert [(v.case_id, v.state) for v in run.verdicts] == [("c-1", "changed"), ("c-2", "done")]
    assert {r["case_id"] for r in mongo["evaluation_results"].find()} == {"c-2"}
    assert run.totals.cases == 1


def test_repeats_run_each_case_in_order(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": READ_TWO_TURNS, "c-2": WRITE_SIMULATED}, repeats=2)
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, repeats=2)).id)
    assert [(v.case_id, v.repeat) for v in run.verdicts] == [
        ("c-1", 0),
        ("c-1", 1),
        ("c-2", 0),
        ("c-2", 1),
    ]
    assert mongo["evaluation_results"].count_documents({"run_id": run.id}) == 4


def test_a_turn_error_fails_the_case_and_the_run_goes_on(client, evaluated):
    agent, _runtime = evaluated
    content = CaseContent(messages=["fail", "never sent"])
    cases = seed_set(client, agent.id, {"c-1": content, "c-2": WRITE_SIMULATED})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases)).id)
    assert run.state == "finished"
    assert [(v.state, v.checks_passed) for v in run.verdicts] == [("error", False), ("done", True)]


def test_the_run_stops_at_its_token_share(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    day = take_day(client)
    run = _start(client, agent, new_run(client, agent.id, cases, day=day))
    row = usage_row(
        "r-1",
        tenant="default",
        source=UsageSource(kind="agent", id=agent.id, preview=run.preview_id),
        usage=TokenUsage(input_tokens=800_000, output_tokens=1),
    )
    mongo["usage_records"].insert_one(row.to_doc())
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "token_limit") and run.verdicts == []
    assert mongo["agents"].count_documents({"preview.run": run.id}) == 0
    # 준비된 실행은 하루 자리를 남긴다
    assert mongo["evaluation_run_days"].find_one({"_id": day})["count"] == 1


def test_a_run_waits_for_a_slot_then_gives_its_day_back(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    previews = client.app.state.agent_previews
    previews.settings = dataclasses.replace(previews.settings, agent_previews_per_tenant=0)
    day = take_day(client)
    run = new_run(client, agent.id, cases, day=day)
    assert drive(client, run.id).state == "queued"
    mongo["evaluation_runs"].update_one(
        {"_id": run.id}, {"$set": {"created_at": utcnow() - timedelta(minutes=31)}}
    )
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "preview_limit")
    assert mongo["evaluation_run_days"].find_one({"_id": day})["count"] == 0


def test_a_preview_past_its_end_ends_the_run(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases))
    past = utcnow() - timedelta(minutes=1)
    mongo["agents"].update_one({"_id": run.preview_id}, {"$set": {"preview.ends_at": past}})
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "preview_ended")


def _member(client, sub: str, role: str = "developer") -> None:
    store = client.app.state.memberships
    client.portal.call(store.upsert, Membership(sub=sub, tenant="default", role=role))


def test_a_revision_with_a_skill_starts_with_the_starters_memberships(client, mongo, evaluated):
    """미리 보기는 시작한 사람의 지금 소속으로 연다. 소속 없는 주체로 열면 그 테넌트의 Skill 이
    보이지 않아 `invalid_graph` 다."""
    agent, _runtime = evaluated
    skill = created_skill(client, "tone", **{"references/tone.md": "짧게 쓴다."})
    spec = agent.spec()
    node_of(spec, "agent-main")["data"]["skills"] = [{"id": skill["id"], "revision": 1}]
    r = client.put(agent.url, json=agent_body(spec))
    assert r.status_code == 200, r.text
    _member(client, "dev-a")
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = new_run(client, agent.id, cases, revision=2, header_mode=False, by="dev-a")
    run = drive(client, run.id)
    assert run.state == "starting", (run.state, run.reason)
    doc = mongo["agents"].find_one({"_id": run.preview_id})
    [main] = [n for n in doc["spec"]["graph"]["nodes"] if n["data"].get("role") == "main"]
    assert main["data"]["skills"] == [{"id": skill["id"], "revision": 1}]


def test_a_starter_without_agent_deploy_is_not_acted_as(client, evaluated):
    agent, runtime = evaluated
    _member(client, "viewer-a", role="viewer")
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = drive(client, new_run(client, agent.id, cases, header_mode=False, by="viewer-a").id)
    assert (run.state, run.reason) == ("error", "acts_as_unavailable")
    assert run.preview_id is None and not _chats(runtime)


def _person_runs(client, monkeypatch, outcome: OpenOutcome) -> None:
    runner = client.app.state.evaluation_runner

    async def anyone(_sub: str) -> bool:
        return True

    async def opened(**_kw: Any) -> OpenOutcome:
        return outcome

    monkeypatch.setattr(runner.person, "runs_as_person", anyone)
    monkeypatch.setattr(runner.delegation.grants, "open", opened)


@pytest.mark.parametrize(
    "outcome",
    [OpenOutcome(None, refused="lost_access"), OpenOutcome(None)],
    ids=["refused", "no-mint"],
)
def test_a_run_never_falls_back_to_the_agents_identity(client, evaluated, monkeypatch, outcome):
    """토큰 인증 모드의 실행이다. 사람이 확인을 지나지 못하거나, Tool 소스가 있는데 위임을 낼 수
    없으면 Agent 의 이름으로 돌지 않는다."""
    agent, runtime = evaluated
    _member(client, "dev-a")
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = new_run(client, agent.id, cases, header_mode=False, by="dev-a")
    run = _start(client, agent, run)
    _person_runs(client, monkeypatch, outcome)
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "acts_as_unavailable")
    assert not _chats(runtime)


def test_a_grant_the_pod_did_not_take_ends_the_run(client, evaluated, monkeypatch):
    agent, runtime = evaluated
    _member(client, "dev-a")
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases, header_mode=False, by="dev-a"))
    grant = OpenedGrant(id="g-1", turn_grant={"id": "g-1", "secret": "s"})
    _person_runs(client, monkeypatch, OpenOutcome(grant))
    runtime.state.echo_grant = False
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "acts_as_unavailable")
    [chat] = _chats(runtime)
    assert chat["turn_grant"]["id"] == "g-1"


def test_a_missing_session_runs_the_step_once_more_then_stops(client, evaluated):
    agent, runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": CaseContent(messages=["gone"])})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases)).id)
    assert (run.state, run.reason) == ("error", "case_not_started")
    assert [b["message"] for b in _chats(runtime)] == ["gone", "gone"]


def test_a_missing_session_on_a_decision_also_runs_the_step_again(client, evaluated):
    agent, runtime = evaluated
    runtime.state.gone_on_decide = True
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases)).id)
    assert (run.state, run.reason) == ("error", "case_not_started")
    assert len(decisions_sent(runtime)) == 2


def test_a_step_that_keeps_failing_ends_the_run(client, mongo, evaluated):
    agent, runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases))
    mongo["evaluation_runs"].update_one(
        {"_id": run.id}, {"$set": {"state": "running", "step.attempts": MAX_ATTEMPTS}}
    )
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "case_not_started")
    assert not _chats(runtime)


@pytest.mark.parametrize("lost_at", [0, 2])
def test_a_replica_that_lost_the_run_writes_nothing(client, mongo, evaluated, monkeypatch, lost_at):
    """다른 레플리카가 실행을 맡은 뒤의 쓰기는 진다. 0 은 준비를 적는 쓰기, 2 는 사례를 넘기는
    쓰기다. 진 레플리카는 판정도 결과도 남기지 않고 미리 보기도 끝내지 않는다."""
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases))
    runs = client.app.state.evaluation_runner.runs
    original = runs.update
    writes = {"n": 0}

    async def taken(run_id: str, owner: str, fields: dict[str, Any], **kw: Any) -> bool:
        if writes["n"] == lost_at:
            await runs.claim(run_id, "replica-b")
        writes["n"] += 1
        return await original(run_id, owner, fields, **kw)

    if lost_at:
        # 준비는 이미 적었다. 쓰기는 시도 수(0), 세션 id(1), 사례 넘기기(2)다
        mongo["evaluation_runs"].update_one({"_id": run.id}, {"$set": {"state": "running"}})
    monkeypatch.setattr(runs, "update", taken)
    drive(client, run.id)
    fresh = client.portal.call(runs.by_id, run.id)
    assert fresh is not None and fresh.driver == "replica-b" and fresh.verdicts == []
    assert fresh.state in ("starting", "running") and fresh.active
    assert mongo["evaluation_results"].count_documents({}) == 0
    assert mongo["agents"].count_documents({"preview.run": run.id}) == 1


def test_turning_evaluations_off_ends_active_runs_and_their_previews(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-1": WRITE_SIMULATED})
    run = _start(client, agent, new_run(client, agent.id, cases))
    runner = client.app.state.evaluation_runner
    runner.settings = dataclasses.replace(runner.settings, evaluations_enabled=False)
    worker = client.app.state.evaluation_run_worker
    assert in_app(client, worker.tick) == 0
    fresh = client.portal.call(runner.runs.by_id, run.id)
    assert fresh is not None and fresh.state == "cancelled"
    assert fresh.reason == "evaluations_disabled"
    assert mongo["agents"].count_documents({"preview.run": run.id}) == 0


def test_the_worker_drives_runs_of_several_agents_in_one_tick(client, fake_clusters, mongo):
    first = deployed_agent(client, fake_clusters, mongo, runtime=False)
    first.fake.service_app = scripted_runtime()
    _p, second = deploy_spec(client, template_for(first.gw, first.ds), name="second")
    runs = []
    for n, agent_id in enumerate((first.id, second["id"])):
        # 세트 id 는 도우미가 정한 하나라 앞의 세트를 지우고 넣는다. 실행은 사례만 읽는다
        mongo["evaluation_sets"].delete_many({})
        cases = seed_set(client, agent_id, {f"c-{n}": WRITE_SIMULATED})
        runs.append(new_run(client, agent_id, cases))
    worker = client.app.state.evaluation_run_worker
    assert in_app(client, worker.tick) == 2
    for run in runs:
        fresh = client.portal.call(worker.runner.runs.by_id, run.id)
        assert fresh is not None and fresh.state == "starting" and fresh.driver == worker.owner
