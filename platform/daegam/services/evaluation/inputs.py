"""평가 세트와 사례, 배포 게이트의 입력 (P45 7절, 12절). 요청 본문이 쓰고, 모르는 칸을 받지 않으며
상한과 꼴을 본다.

저장 모델(`store/evaluation.py`, `store/agents.py` `EvaluationGate`)은 모르는 칸을 버리고 확인
항목의 종류를 글자로 읽는다. 뒤 판이 칸이나 종류를 더하거나 상한을 넓혀도 이 판이 문서를 읽게
하려는 것이다. 그래서 상한과 꼴의 검사는 여기에만 있다. 입력은 `to_stored` 로 같은 이름의 저장
모델이 된다.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from daegam.regex_safety import check_regex
from daegam.store.agents import EvaluationGate
from daegam.store.evaluation import CaseContent, CaseOrigin
from daegam.store.evaluation_runs import Grader

# 반복 수 상한 (7절). 사례 수 × 반복 수의 상한은 저장소의 조건이 지킨다(`MAX_CASE_RUNS`)
MAX_REPEATS = 3
# 사례 하나의 질문, 채점 기준, 확인 항목, 가상 결과 수 상한 (7절)
MAX_MESSAGES = 5
MAX_CRITERIA = 10
MAX_CHECKS = 10
MAX_TOOL_RESULTS = 20
# 글자 수 상한. 질문과 참조 답, 가상 결과는 채팅 한 번과 Tool 결과 하나의 크기다
MAX_NAME_CHARS = 100
MAX_MODEL_CHARS = 200
MAX_MESSAGE_CHARS = 8000
MAX_REFERENCE_CHARS = 20000
MAX_CRITERION_CHARS = 500
MAX_CHECK_TEXT_CHARS = 500
# `tool_called` 의 인자 값 JSON 의 길이 상한. 개인정보 검사가 이 글을 읽는다
MAX_CHECK_ARGS_CHARS = 2000
MAX_TOOL_RESULT_CHARS = 8000
MAX_TOOL_NAME_CHARS = 128
# 평가 id, 세션 id, 턴 id 의 길이 상한. 플랫폼과 런타임의 id 는 36자 uuid 와 32자 hex 인데 따로 정한
# 상한이 없어, 질의에 싣는 값의 크기만 묶는 넉넉한 값이다
MAX_ID_CHARS = 200
# 배포 게이트 (12절). 실행과 기준선의 나이 한도(일)의 기본값과 상한, 기준선과 견주는 세트의 반복 수
# 하한이다. 한 번의 아쉬운 답이 배포를 막지 않게 반복이 둘 이상이어야 견준다
DEFAULT_MAX_AGE_DAYS = 7
MAX_AGE_DAYS = 30
BASELINE_REPEATS = 2

CheckKind = Literal[
    "tool_called", "tool_not_called", "answer_contains", "answer_not_contains", "answer_matches"
]
TOOL_CHECKS: frozenset[str] = frozenset({"tool_called", "tool_not_called"})


class GraderIn(BaseModel):
    """세트의 채점 모델. provider 는 등록된 provider 의 이름이다. 평가하는 리비전의 모델 노드가 쓰는
    provider 인지는 실행의 시작과 채점이 본다(11절)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    model: str = Field(min_length=1, max_length=MAX_MODEL_CHARS)

    def to_stored(self) -> Grader:
        return Grader.model_validate(self.model_dump())


class EvaluationGateIn(BaseModel):
    """배포 게이트 (12절). min_pass_rate 는 0 에서 1, max_age_days 는 1 에서 30 이다."""

    model_config = ConfigDict(extra="forbid")

    set_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    min_pass_rate: float = Field(ge=0, le=1)
    no_worse_cases: bool = False
    max_age_days: int = Field(default=DEFAULT_MAX_AGE_DAYS, ge=1, le=MAX_AGE_DAYS)

    def to_stored(self) -> EvaluationGate:
        return EvaluationGate.model_validate(self.model_dump())


class CheckIn(BaseModel):
    """모델 없이 플랫폼이 보는 확인 하나 (11절). Tool 확인은 tool 이름을, `tool_called` 는 인자에
    들어 있어야 할 값(args)을 함께 받는다. 답 확인은 text 이고 `answer_matches` 면 정규식이다."""

    model_config = ConfigDict(extra="forbid")

    kind: CheckKind
    tool: str | None = Field(default=None, min_length=1, max_length=MAX_TOOL_NAME_CHARS)
    args: dict[str, Any] = Field(default_factory=dict, max_length=10)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_CHECK_TEXT_CHARS)

    @model_validator(mode="after")
    def _fits_kind(self) -> CheckIn:
        if self.kind in TOOL_CHECKS:
            if self.tool is None or self.text is not None:
                raise ValueError(f"{self.kind} takes a tool and no text")
            if self.kind == "tool_not_called" and self.args:
                raise ValueError("tool_not_called takes no args")
        elif self.text is None or self.tool is not None or self.args:
            raise ValueError(f"{self.kind} takes a text and no tool or args")
        if len(json.dumps(self.args, ensure_ascii=False)) > MAX_CHECK_ARGS_CHARS:
            raise ValueError(f"args take at most {MAX_CHECK_ARGS_CHARS} characters of JSON")
        if self.kind == "answer_matches":
            check_regex(self.text or "")
        return self


class ReferenceIn(BaseModel):
    """참조 답. good 은 이 답이 좋은 답의 예인지다. 복사한 사례는 평가가 아쉬워요이면 거짓이다."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_REFERENCE_CHARS)
    good: bool = True


class CaseContentIn(BaseModel):
    """사례의 내용. 질문은 새 대화 하나에서 차례로 보낸다. expect_done 은 턴이 `done` 으로
    끝났는지의 확인이고 기본으로 켜져 있다. tool_results 는 쓰기 Tool 이름마다의 가상
    결과다(10절)."""

    model_config = ConfigDict(extra="forbid")

    messages: list[str] = Field(min_length=1, max_length=MAX_MESSAGES)
    reference: ReferenceIn | None = None
    criteria: list[str] = Field(default_factory=list, max_length=MAX_CRITERIA)
    checks: list[CheckIn] = Field(default_factory=list, max_length=MAX_CHECKS)
    expect_done: bool = True
    tool_results: dict[str, str] = Field(default_factory=dict, max_length=MAX_TOOL_RESULTS)

    @model_validator(mode="after")
    def _sizes(self) -> CaseContentIn:
        for text in self.messages:
            if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                raise ValueError(f"each message has 1 to {MAX_MESSAGE_CHARS} characters")
        for text in self.criteria:
            if not text.strip() or len(text) > MAX_CRITERION_CHARS:
                raise ValueError(f"each criterion has 1 to {MAX_CRITERION_CHARS} characters")
        for name, text in self.tool_results.items():
            if not name or len(name) > MAX_TOOL_NAME_CHARS or len(text) > MAX_TOOL_RESULT_CHARS:
                raise ValueError(
                    f"tool results map Tool names of 1 to {MAX_TOOL_NAME_CHARS} characters to "
                    f"results of at most {MAX_TOOL_RESULT_CHARS} characters"
                )
        return self

    def to_stored(self) -> CaseContent:
        return CaseContent.model_validate(self.model_dump())


class OriginIn(BaseModel):
    """`from-turn` 이 돌려준 출처 (8절)."""

    model_config = ConfigDict(extra="forbid")

    rating_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    session_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    turn_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)

    def to_stored(self) -> CaseOrigin:
        return CaseOrigin.model_validate(self.model_dump())
