"""사례의 판정과 실행의 합계, 기준선 (P45 11절)."""

from __future__ import annotations

from daegam.services.evaluation.verdicts import baselines, case_outcomes, compare, totals
from daegam.store.evaluation_runs import CaseVerdict, EvaluationRun, RunActor
from tests.helpers.evaluation import verdict


def _run(*verdicts: CaseVerdict, run_id: str = "r") -> EvaluationRun:
    return EvaluationRun(
        id=run_id,
        tenant="t",
        agent_id="a",
        set_id="s",
        set_digest="d",
        revision=1,
        by=RunActor(sub="x"),
        verdicts=list(verdicts),
    )


def test_totals_count_a_case_that_passes_in_most_repeats() -> None:
    run = _run(
        verdict("c1", True),
        verdict("c1", True, repeat=1),
        verdict("c1", None, repeat=2, state="error"),
        verdict("c2", True),
        verdict("c2", False, repeat=1),
        verdict("c3", True, counted=False),
        verdict("c4", None, repeat=0, state="changed"),
        # 뒤 반복이 바뀐 사례는 통째로 세지 않는다
        verdict("c5", True),
        verdict("c5", None, repeat=1, state="changed"),
    )
    out = totals(run, 42, criteria=False, cost_usd=0.5)
    assert (out.cases, out.counted, out.passed, out.tokens, out.cost_usd) == (3, 2, 1, 42, 0.5)


def test_criteria_count_only_when_the_gate_counts_them() -> None:
    """기준만 있는 사례는 기준을 셀 때만 센다. grader_error 는 지나지 못한 것이다."""
    run = _run(
        verdict("only", True, counted=False, criteria=2, criteria_passed=True),
        verdict("both", True, criteria=1, criteria_passed=False),
        verdict("broken", True, criteria=1, criteria_passed=False, grader_error=True),
    )
    off = case_outcomes(run.verdicts, criteria=False)
    assert not off["only"].counted and off["both"].passed and off["broken"].passed
    on = case_outcomes(run.verdicts, criteria=True)
    assert on["only"].counted and on["only"].passed
    assert not on["both"].passed and not on["broken"].passed
    assert totals(run, 0, criteria=True).passed == 1


def test_a_case_passed_in_its_baseline_and_failing_now_is_worse() -> None:
    newer = _run(verdict("c1", False), run_id="newer")
    older = _run(
        verdict("c1", True),
        verdict("c2", True),
        verdict("c3", True, digest="old"),
        run_id="older",
    )
    # 사례마다 그것을 같은 지문으로 든 가장 새 실행이 기준선이다
    base = baselines([newer, older], criteria=False)
    assert base[("c1", "d")].passed is False and base[("c2", "d")].passed is True
    now = _run(
        verdict("c1", False),
        verdict("c2", False),
        verdict("c3", False),
        verdict("c4", True),
        verdict("c5", False, counted=False),
    )
    got = compare(now, base, criteria=False)
    # c1 은 기준선에서도 지나지 못했다. c3 는 지문이 달라 기준선이 없다. 세지 않는 c5 는
    # 견주지 않는다
    assert got.worse == ["c2"] and got.missing == ["c3", "c4"]
