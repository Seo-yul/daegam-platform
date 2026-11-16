"""Agent 평가 세트, 사례, 실행의 본문과 응답 (P45 7절·9절·13절). 본문은
`services/evaluation/inputs.py` 의 모델이라 모르는 칸을 받지 않는다. 응답의 세트와 사례는 store
모델 그대로이고 세트는 지문을 더한다. 실행은 판정과 합계를, 실행 하나는 시작한 사람에게 결과를
더한다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.api.schemas.agents import EvaluationGateOut
from daegam.services.evaluation.inputs import (
    MAX_ID_CHARS,
    MAX_NAME_CHARS,
    MAX_REPEATS,
    CaseContentIn,
    EvaluationGateIn,
    GraderIn,
    OriginIn,
)
from daegam.store.base import UtcDatetime
from daegam.store.evaluation import CaseContent, CaseOrigin, EvaluationCase, EvaluationSet
from daegam.store.evaluation_runs import CaseVerdict, EvaluationResult, Grader, RunTotals


class EvaluationSetBody(BaseModel):
    """`POST`·`PUT .../evaluation-sets`. 이름은 앞뒤 공백을 뗀다. 채점 모델이 없으면 채점 기준이
    있는 사례를 넣을 수 없다."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    grader: GraderIn | None = None
    repeats: int = Field(default=1, ge=1, le=MAX_REPEATS)

    @field_validator("name")
    @classmethod
    def _strip(cls, name: str) -> str:
        stripped = name.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class EvaluationSetOut(EvaluationSet):
    """세트 하나. digest 는 채점 모델, 반복 수, 사례마다의 지문으로 지은 세트의 지문이다. 실행은
    시작할 때 사람이 본 지문을 싣는다(9절)."""

    model_config = REQUIRED_DEFAULTS

    digest: str


class EvaluationCaseUpdateBody(BaseModel):
    """`PUT .../cases/{case_id}`. 사례의 내용이다. 손으로 쓴 사례에 개인정보로 보이는 것이 있으면
    `pii_acknowledged` 를 실어야 저장된다. 복사한 사례는 확인으로 저장되지 않는다(8절)."""

    model_config = ConfigDict(extra="forbid")

    content: CaseContentIn
    pii_acknowledged: bool = False


class EvaluationCaseBody(EvaluationCaseUpdateBody):
    """`POST .../cases`. origin 은 `from-turn` 이 돌려준 출처이고, 있으면 그 평가가 지금도
    동의할 때만 저장된다."""

    origin: OriginIn | None = None


class FromTurnBody(BaseModel):
    """`POST .../cases/from-turn`. 평가를 받은 턴이다."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    turn_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)


class EvaluationCaseDraftOut(BaseModel):
    """저장하기 전의 복사본 (8절). 질문과 답의 개인정보로 보이는 것은 `[phone]` 같은 자리표로
    바꿨고 replaced 가 종류마다의 수다. 저장은 이 content 와 origin 을 `POST .../cases` 로
    보낸다."""

    model_config = REQUIRED_DEFAULTS

    content: CaseContent
    origin: CaseOrigin
    replaced: dict[str, int]


class EvaluationCaseOut(EvaluationCase):
    """사례 하나. created_by·updated_by 가 쓰거나 마지막으로 고친 사람이다."""

    model_config = REQUIRED_DEFAULTS


class EvaluationCasesOut(BaseModel):
    """`GET .../cases`. 만든 차례이고 set_digest 는 지금 세트의 지문이다."""

    model_config = REQUIRED_DEFAULTS

    items: list[EvaluationCaseOut]
    set_digest: str


class EvaluationRunBody(BaseModel):
    """`POST .../runs`. set_digest 는 시작하는 사람이 본 세트의 지문이고, revision 이 없으면 저장한
    최신 리비전이다 (9절)."""

    model_config = ConfigDict(extra="forbid")

    set_digest: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    revision: int | None = Field(default=None, ge=1)


class EvaluationRunOut(BaseModel):
    """실행 하나. 판정(`verdicts`)과 합계(`totals`)는 내용을 담지 않아 `agent:approve` 를 가진
    모두가 본다. started_by 는 시작한 사람이고 header_mode 면 턴이 Agent 의 자격으로 돌았다.
    done_steps 는 돈 사례와 반복의 수, cases 와 repeats 는 그 차례의 크기다. reason 은 `error` 의
    까닭이나 취소의 까닭이다. spec_digest 는 평가한 리비전 내용의 지문이고 grader 는 시작할 때의
    채점 모델이다. graded_steps 는 `grading` 에서 채점을 마친 판정의 수다 (11절)."""

    model_config = REQUIRED_DEFAULTS

    id: str
    set_id: str
    set_digest: str
    revision: int
    spec_digest: str | None
    grader: Grader | None
    state: str
    reason: str | None
    cancelled_by: str | None
    started_by: str
    started_by_name: str
    header_mode: bool
    cases: int
    repeats: int
    done_steps: int
    graded_steps: int
    verdicts: list[CaseVerdict]
    totals: RunTotals
    created_at: UtcDatetime
    running_at: UtcDatetime | None
    ended_at: UtcDatetime | None


class RunBaselineOut(BaseModel):
    """끝난 실행과 배포된 내용의 기준선의 비교 (P45 11절). worse 는 기준선에서 지났는데 지금 지나지
    못한 사례, missing 은 기준선이 없는 센 사례다."""

    model_config = REQUIRED_DEFAULTS

    worse: list[str]
    missing: list[str]


class EvaluationResultOut(EvaluationResult):
    """사례와 반복 하나의 결과. 시작한 사람만 받는다."""

    model_config = REQUIRED_DEFAULTS


class EvaluationRunDetailOut(EvaluationRunOut):
    """`GET .../runs/{run_id}`. results 는 시작한 사람에게만 있고, 다른 사람은 content_hidden 이
    참이다 (9절)."""

    results: list[EvaluationResultOut]
    content_hidden: bool
    # 끝난 실행만 있다. 기준선의 나이 한도는 게이트의 것이고 게이트가 없으면 7일이다
    baseline: RunBaselineOut | None


class EvaluationGateAnswer(BaseModel):
    """`PUT /agents/{id}/evaluation-gate` 의 답 (P45 12절). 게이트를 끄면 `gate` 가 null 이다."""

    model_config = REQUIRED_DEFAULTS

    gate: EvaluationGateOut | None


class EvaluationGateBody(EvaluationGateIn):
    """`PUT /agents/{id}/evaluation-gate` (P45 12절). null 이면 게이트를 끈다."""


class EvaluationReviewOut(BaseModel):
    """시작하기 전에 확인할 사례 (9절). 부른 사람이 그 세트로 마지막으로 시작한 때(since, 없으면
    null) 뒤에 다른 사람이 쓰거나 고친 사례의 id 이고, set_digest 는 시작이 실을 지금 세트의
    지문이다."""

    model_config = REQUIRED_DEFAULTS

    set_digest: str
    since: UtcDatetime | None
    cases: list[str]


class EvaluationRunsOut(BaseModel):
    """`GET .../runs`. 새것이 앞이고 review 는 부른 사람이 확인할 사례다."""

    model_config = REQUIRED_DEFAULTS

    items: list[EvaluationRunOut]
    review: EvaluationReviewOut
