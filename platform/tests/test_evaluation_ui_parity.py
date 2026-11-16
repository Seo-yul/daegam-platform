"""P45: 평가 화면의 상한과 기본값 사본 파리티 (D109).

평가 세트와 사례의 상한, 배포 게이트의 나이 한도와 기준선 반복 수의 원본은 플랫폼
`services/evaluation/inputs.py` 이고, 사례 수 × 반복 수의 상한은 `store/evaluation.py` 의
`MAX_CASE_RUNS` 다. 화면 `ui/src/lib/evaluation.ts` 의 편집기와 게이트 카드가 같은 값을 쓰고, 상한은
오류 사전도 읽어 `ui/src/lib/limits.ts` 에 있다. 평가 없이 배포하는 까닭의 길이 상한은
`api/schemas/agents.py` 의 `SkipEvaluation` 이 원본이다. 평가 사례로 쓰는 동의의 안내가 설치의
보존 기간을 받지 못했을 때(그 값을 싣지 않는 옛 플랫폼) 적는 날 수는 `config` 의
`feedback_retention_days` 기본값이고, 실행이 미리 보기 자리를 기다리는 시간은
`services/evaluation/runner.py` 의 `QUEUED_FOR` 다. 어긋나면 화면이 서버가 거절할 값을 받거나
안내와 오류 문장이 틀린 기간을 말한다. 플랫폼이 원본이므로 화면의 사본을 그 값에 맞춘다.
"""

from __future__ import annotations

from daegam.api.schemas.agents import SkipEvaluation
from daegam.config import _DEFAULTS
from daegam.services.evaluation import inputs, runner
from daegam.store.evaluation import MAX_CASE_RUNS
from tests.helpers.source import REPO, UI_LIB, ts_number

EVALUATION_TS = UI_LIB / "evaluation.ts"
LIMITS_TS = UI_LIB / "limits.ts"
FEEDBACK_TS = UI_LIB / "feedback.ts"
SKIP_TSX = REPO / "ui" / "src" / "components" / "agents" / "EvaluationSkip.tsx"
FIX = (
    "플랫폼 services/evaluation/inputs.py·store/evaluation.py 가 원본이다."
    " 화면의 사본을 그 값에 맞춘다"
)


def test_case_limits_match_the_platform() -> None:
    copies = {
        "MAX_MESSAGES": inputs.MAX_MESSAGES,
        "MAX_CRITERIA": inputs.MAX_CRITERIA,
        "MAX_CHECKS": inputs.MAX_CHECKS,
        "MAX_TOOL_RESULTS": inputs.MAX_TOOL_RESULTS,
        "MAX_REPEATS": inputs.MAX_REPEATS,
        "MAX_NAME_CHARS": inputs.MAX_NAME_CHARS,
    }
    for name, value in copies.items():
        assert ts_number(EVALUATION_TS, name) == value, f"{name}: {FIX}"
    assert ts_number(LIMITS_TS, "EVALUATION_MAX_CASE_RUNS") == MAX_CASE_RUNS, FIX


def test_gate_limits_match_the_platform() -> None:
    assert ts_number(EVALUATION_TS, "DEFAULT_MAX_AGE_DAYS") == inputs.DEFAULT_MAX_AGE_DAYS, FIX
    assert ts_number(EVALUATION_TS, "MAX_AGE_DAYS") == inputs.MAX_AGE_DAYS, FIX
    assert ts_number(EVALUATION_TS, "BASELINE_REPEATS") == inputs.BASELINE_REPEATS, FIX


def test_skip_reason_limit_matches_the_request_body() -> None:
    field = SkipEvaluation.model_fields["reason"]
    maxima = [m.max_length for m in field.metadata if getattr(m, "max_length", None) is not None]
    assert maxima and ts_number(SKIP_TSX, "SKIP_REASON_MAX_CHARS") == maxima[0], FIX


def test_consent_hint_default_days_match_the_platform_default() -> None:
    default = _DEFAULTS["feedback_retention_days"]
    assert ts_number(FEEDBACK_TS, "FEEDBACK_RETENTION_DEFAULT_DAYS") == default, FIX


def test_queue_minutes_match_the_runner() -> None:
    """실행의 `preview_limit` 문장이 적는 기다림이다."""
    minutes = runner.QUEUED_FOR.total_seconds() / 60
    assert ts_number(LIMITS_TS, "EVALUATION_QUEUE_MINUTES") == minutes, FIX
