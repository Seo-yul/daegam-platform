"""사례의 판정과 실행의 합계, 기준선 (P45 11절).

실행 문서의 내용 없는 판정(`CaseVerdict`)만 읽는다. 결과 문서보다 오래 살기 때문에 배포 게이트와
기준선이 결과가 지워진 뒤에도 쓴다.

- **반복 하나가 지나는 것.** 그 반복이 `done` 으로 돌았고 확인 항목이 모두 지났으며, 기준을 세면
  (`criteria=True`) 채점 기준도 모두 지났다. `error` 와 `grader_error` 는 지나지 못한 것이다.
- **사례가 지나는 것.** 반복의 절반 넘게 지났다.
- **세는 사례.** `done` 밖의 확인 항목이 있거나, 기준을 셀 때 채점 기준이 있는 사례다. 반복 하나라도
  `changed` 인 사례는 세지 않는다(그 뒤 반복은 다른 내용이다).
- **기준선.** 사례의 기준선은 배포된 내용에서 끝난 가장 새 실행 가운데 그 사례를 같은 지문으로 든
  실행의 판정이다. 기준선에서 지났는데 지금 지나지 못한 사례가 나빠진 사례다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from daegam.services.evaluation.checks import DONE
from daegam.store.evaluation_runs import CaseVerdict, EvaluationRun, RunTotals

# 도는 동안 바뀌거나 지워져 돌거나 채점하지 않은 사례와 반복의 판정
CHANGED = "changed"


@dataclass(frozen=True)
class CaseOutcome:
    """사례 하나의 판정. 반복을 모은 것이다."""

    case_id: str
    case_digest: str
    counted: bool
    passed: bool


def _repeat_passed(v: CaseVerdict, *, criteria: bool) -> bool:
    if v.state != DONE or not v.checks_passed:
        return False
    if criteria and v.criteria:
        return v.criteria_passed is True and not v.grader_error
    return True


def case_outcomes(verdicts: Iterable[CaseVerdict], *, criteria: bool) -> dict[str, CaseOutcome]:
    """사례 id 마다의 판정이다. `changed` 가 든 사례는 빠진다. 차례는 처음 나온 차례다."""
    by_case: dict[str, list[CaseVerdict]] = {}
    for v in verdicts:
        by_case.setdefault(v.case_id, []).append(v)
    out: dict[str, CaseOutcome] = {}
    for case_id, vs in by_case.items():
        if any(v.state == CHANGED for v in vs):
            continue
        counted = any(v.counted or (criteria and v.criteria > 0) for v in vs)
        ok = sum(1 for v in vs if _repeat_passed(v, criteria=criteria))
        out[case_id] = CaseOutcome(case_id, vs[0].case_digest, counted, ok * 2 > len(vs))
    return out


def totals(run: EvaluationRun, tokens: int, *, criteria: bool, cost_usd: float = 0.0) -> RunTotals:
    """실행의 합계다. 판정이 난 사례, 그 가운데 센 사례와 지난 사례, 원장의 토큰과 금액이다."""
    outcomes = case_outcomes(run.verdicts, criteria=criteria).values()
    counted = [o for o in outcomes if o.counted]
    return RunTotals(
        cases=len(outcomes),
        counted=len(counted),
        passed=sum(1 for o in counted if o.passed),
        tokens=tokens,
        cost_usd=cost_usd,
    )


@dataclass(frozen=True)
class Comparison:
    """실행과 기준선의 비교. worse 는 나빠진 사례, missing 은 기준선이 없는 센 사례다."""

    worse: list[str]
    missing: list[str]


def baselines(
    runs: Iterable[EvaluationRun], *, criteria: bool
) -> dict[tuple[str, str], CaseOutcome]:
    """(사례 id, 지문)마다의 기준선이다. runs 는 배포된 내용에서 끝난 실행이고 새것이 앞이다.
    사례마다 그것을 든 가장 새 실행의 판정이 기준선이다."""
    out: dict[tuple[str, str], CaseOutcome] = {}
    for run in runs:
        for o in case_outcomes(run.verdicts, criteria=criteria).values():
            out.setdefault((o.case_id, o.case_digest), o)
    return out


def compare(
    run: EvaluationRun,
    base: dict[tuple[str, str], CaseOutcome],
    *,
    criteria: bool,
) -> Comparison:
    """센 사례마다 기준선과 견준다."""
    worse: list[str] = []
    missing: list[str] = []
    for o in case_outcomes(run.verdicts, criteria=criteria).values():
        if not o.counted:
            continue
        before = base.get((o.case_id, o.case_digest))
        if before is None:
            missing.append(o.case_id)
        elif before.passed and not o.passed:
            worse.append(o.case_id)
    return Comparison(worse, missing)


__all__ = [
    "CHANGED",
    "CaseOutcome",
    "Comparison",
    "baselines",
    "case_outcomes",
    "compare",
    "totals",
]
