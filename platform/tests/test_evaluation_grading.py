"""평가 실행의 채점 (P45 9절, 11절).

사례가 다 돌면 실행은 미리 보기를 끝내고 `grading` 으로 옮겨 채점 기준이 있는 사례와 반복을 차례로
채점한다. 채점 호출은 미리 보기의 원장 행 모양으로 남고, 실행이 끝날 때 결과마다 금액을 적는다. 가짜
런타임과 픽스처 `evaluated` 는 `tests/helpers/evaluation.py` 의 것이다. 템플릿 Agent 의 모델은
`local-stub` 이라 그 이름의 채점 모델은 stub 채점이다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from daegam.errors import ServiceError
from daegam.services.evaluation.grader import EvaluationGrader, Grade, GraderUnavailable
from daegam.store.evaluation import CaseCheck, CaseContent
from daegam.store.evaluation_runs import EvaluationRun, Grader
from daegam.store.settings import LLMProvider
from daegam.store.usage import UsageSource
from tests.helpers.agent_runtime import set_ready
from tests.helpers.evaluation import DirectEndpoints, drive, new_run, seed_set
from tests.helpers.usage import usage_row

STUB = Grader(provider="local-stub", model="m")
GRADED = CaseContent(
    messages=["read"],
    criteria=["7일", "사과"],
    checks=[CaseCheck(kind="answer_contains", text="7일")],
)
PLAIN = CaseContent(messages=["안녕"], checks=[CaseCheck(kind="answer_contains", text="안녕")])


def _start(client, agent, run: EvaluationRun) -> EvaluationRun:
    run = drive(client, run.id)
    assert run.state == "starting", run
    preview = client.portal.call(client.app.state.agents.with_previews().get, run.preview_id)
    set_ready(agent.fake, preview.k8s.name)
    return run


def _results(mongo, run: EvaluationRun) -> dict[str, dict[str, Any]]:
    return {r["case_id"]: r for r in mongo["evaluation_results"].find({"run_id": run.id})}


def test_cases_with_criteria_are_graded_after_the_preview_ends(client, mongo, evaluated):
    agent, _runtime = evaluated
    cases = seed_set(client, agent.id, {"c-graded": GRADED, "c-plain": PLAIN})
    run = _start(client, agent, new_run(client, agent.id, cases, grader=STUB))
    preview_id = run.preview_id
    run = drive(client, run.id)
    assert run.state == "finished", run
    assert mongo["agents"].find_one({"_id": preview_id}) is None
    verdicts = {v.case_id: v for v in run.verdicts}
    graded = verdicts["c-graded"]
    assert (graded.criteria, graded.criteria_passed, graded.grader_error) == (2, False, False)
    assert verdicts["c-plain"].criteria == 0 and verdicts["c-plain"].criteria_passed is None
    [one, two] = _results(mongo, run)["c-graded"]["criteria"]
    assert (one["criterion"], one["passed"], one["gap"]) == ("7일", True, None)
    assert (two["criterion"], two["passed"]) == ("사과", False) and two["gap"]
    # 게이트가 기준을 세지 않는 동안 합계는 확인 항목만 센다
    assert (run.totals.counted, run.totals.passed) == (2, 2)
    assert run.step.graded == 2


class _ModelGrader(EvaluationGrader):
    """provider 를 openai 계열로 읽는 채점 모델. 길은 늘 직접이고 모델은 가짜다."""

    def __init__(self, base: EvaluationGrader, reply: AIMessage):
        super().__init__(
            base.platform_settings,
            DirectEndpoints(),  # type: ignore[arg-type]
            base.ledger,
            base.settings,
            model_factory=lambda _c, _e: FakeMessagesListChatModel(responses=[reply]),
        )

    async def provider(self, grader: Grader) -> LLMProvider | None:
        return LLMProvider(name=grader.provider, kind="openai", model=grader.model)


def test_grader_calls_are_ledger_rows_of_the_run_and_costs_reach_the_results(
    client, mongo, evaluated, monkeypatch
):
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner
    answer = {"criteria": [{"index": 1, "passed": True}, {"index": 2, "passed": True}]}
    usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
    reply = AIMessage(content=json.dumps(answer), usage_metadata=usage)
    monkeypatch.setattr(runner, "grader", _ModelGrader(runner.grader, reply))
    cases = seed_set(client, agent.id, {"c-graded": GRADED})
    grader = Grader(provider="local-stub", model="gpt-4o")
    run = _start(client, agent, new_run(client, agent.id, cases, grader=grader))
    # 사례의 대화(s-1)에서 런타임이 남긴 행이다
    turn = usage_row(
        "turn-1",
        tenant="default",
        source=UsageSource(kind="agent", id=agent.id, preview=run.preview_id),
        ref="s-1",
        cost_usd=0.25,
    )
    mongo["usage_records"].insert_one(turn.to_doc())
    run = drive(client, run.id)
    assert run.state == "finished", run
    [row] = list(mongo["usage_records"].find({"ref": run.id}))
    assert row["source"]["preview"] == run.preview_id and row["source"]["id"] == agent.id
    assert row["writer"] == "platform" and row["cost_usd"] > 0
    result = _results(mongo, run)["c-graded"]
    assert result["grader_call"] == row["_id"] and result["session_id"] == "s-1"
    assert result["tokens"]["grader_input"] == 1000 and result["tokens"]["input"] == 10
    assert result["cost_usd"] == pytest.approx(0.25 + row["cost_usd"])
    assert run.totals.cost_usd == pytest.approx(0.25 + row["cost_usd"])
    assert run.totals.tokens == 1100 + turn.usage.input_tokens + turn.usage.output_tokens
    assert run.verdicts[0].criteria_passed is True


def test_an_answer_the_grader_could_not_read_is_a_grader_error(
    client, mongo, evaluated, monkeypatch
):
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner

    async def broken(*_a: Any, **_k: Any) -> Grade:
        return Grade(None, error="grader_error")

    monkeypatch.setattr(runner.grader, "grade", broken)
    cases = seed_set(client, agent.id, {"c-graded": GRADED})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, grader=STUB)).id)
    assert run.state == "finished"
    [verdict] = run.verdicts
    assert verdict.grader_error and verdict.criteria_passed is False
    assert _results(mongo, run)["c-graded"]["criteria"] == []


def test_a_case_changed_while_it_is_graded_keeps_no_result(client, mongo, evaluated, monkeypatch):
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner
    original = runner.grader.grade

    async def changing(*a: Any, **k: Any) -> Grade:
        mongo["evaluation_cases"].update_one({"_id": "c-graded"}, {"$set": {"digest": "other"}})
        return await original(*a, **k)

    monkeypatch.setattr(runner.grader, "grade", changing)
    cases = seed_set(client, agent.id, {"c-graded": GRADED, "c-plain": PLAIN})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, grader=STUB)).id)
    assert run.state == "finished"
    assert [(v.case_id, v.state) for v in run.verdicts] == [
        ("c-graded", "changed"),
        ("c-plain", "done"),
    ]
    assert set(_results(mongo, run)) == {"c-plain"}
    assert run.totals.cases == 1


def test_a_grader_whose_provider_is_gone_ends_the_run(client, evaluated, monkeypatch):
    """채점은 provider 규칙을 한 번 더 본다. 그 이름의 provider 가 없어졌으면 끝낸다 (11절)."""
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner

    async def gone(_grader: Grader) -> None:
        return None

    monkeypatch.setattr(runner.grader, "provider", gone)
    cases = seed_set(client, agent.id, {"c-graded": GRADED})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, grader=STUB)).id)
    assert (run.state, run.reason) == ("error", "evaluation_grader_provider")
    assert run.verdicts[0].criteria_passed is None


def test_a_run_stopped_early_grades_what_ran_and_ends_with_its_reason(
    client, mongo, evaluated, monkeypatch
):
    agent, _runtime = evaluated
    previews = client.app.state.evaluation_runner.previews
    calls = {"n": 0}
    original = previews.check_budget

    async def budget(preview: Any) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise ServiceError(429, "daily_budget_exceeded", "spent")
        await original(preview)

    monkeypatch.setattr(previews, "check_budget", budget)
    cases = seed_set(client, agent.id, {"c-graded": GRADED, "c-plain": PLAIN})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, grader=STUB)).id)
    assert (run.state, run.reason) == ("error", "daily_budget")
    [verdict] = run.verdicts
    assert verdict.case_id == "c-graded" and verdict.criteria_passed is False
    assert len(_results(mongo, run)["c-graded"]["criteria"]) == 2
    assert mongo["agents"].count_documents({"preview.run": run.id}) == 0


def test_grading_stops_when_the_run_spends_its_tokens(client, mongo, evaluated, monkeypatch):
    """사례는 원장의 80% 에서 멈추고 채점은 나머지를 쓴다. 다 쓰면 `token_limit` 로 끝난다 (9절)."""
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner
    cases = seed_set(client, agent.id, {"c-graded": GRADED})
    run = _start(client, agent, new_run(client, agent.id, cases, grader=STUB))
    original = runner.run_tokens

    async def spent(r: EvaluationRun) -> int:
        # 사례 앞의 검사는 지나고, 채점 앞의 검사에서 상한에 닿는다
        return runner.settings.evaluation_run_tokens if r.state == "grading" else await original(r)

    monkeypatch.setattr(runner, "run_tokens", spent)
    run = drive(client, run.id)
    assert (run.state, run.reason) == ("error", "token_limit")
    assert run.verdicts[0].criteria_passed is None


def test_a_grader_without_a_route_ends_the_run(client, evaluated, monkeypatch):
    """키가 없어 길을 고를 수 없는 채점 모델은 provider 가 없어진 것과 같다 (11절)."""
    agent, _runtime = evaluated
    runner = client.app.state.evaluation_runner

    async def unavailable(*_a: Any, **_k: Any) -> Grade:
        raise GraderUnavailable("no_api_key")

    monkeypatch.setattr(runner.grader, "grade", unavailable)
    cases = seed_set(client, agent.id, {"c-graded": GRADED})
    run = drive(client, _start(client, agent, new_run(client, agent.id, cases, grader=STUB)).id)
    assert (run.state, run.reason) == ("error", "evaluation_grader_provider")
