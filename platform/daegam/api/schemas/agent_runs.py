"""실행·스케줄 라우트의 요청과 응답 (P41 2절). 시각은 UTC 표시를 붙여 낸다(`UtcDatetime`)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from daegam.services.runs.service import EXPIRY_DEFAULT_DAYS, RUNTIME_INPUT_CAP
from daegam.store.agent_runs import RunStatus
from daegam.store.base import UtcDatetime


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("the message is empty")
    return value


# 웹훅 메시지 틀의 길이 위 끝. 틀로 만든 메시지는 호출마다 16 KiB 와 입력 한도를 따로 본다
TEMPLATE_MAX_CHARS = 8192

# 실행에 보내는 메시지. 리비전의 입력 한도는 서비스가 보고, 여기서는 런타임의 위 끝만 막는다
Message = Annotated[
    str, Field(min_length=1, max_length=RUNTIME_INPUT_CAP), AfterValidator(_not_blank)
]


class ActsAsOut(BaseModel):
    sub: str
    username: str | None = None
    kind: Literal["person", "agent"]
    cause: str | None = None


class RunUsageOut(BaseModel):
    """실행 세션의 토큰. 원장 행의 합이다."""

    input_tokens: int
    output_tokens: int


class RunCallerOut(BaseModel):
    """다른 Agent 가 Tool 로 부른 실행의 부른 쪽 (P46 6.5절). `chain` 은 뿌리부터 부른 Agent 까지의
    Agent id 다."""

    agent_id: str
    session_id: str | None
    chain: list[str]


class AgentRunOut(BaseModel):
    """실행 기록 하나. 입력은 본문 대신 크기와 sha256 만 낸다. `source` 는 `schedule`·`webhook`·
    `manual`·`agent` 이고, 뒤 판이 더한 출처의 기록도 글자 그대로 낸다 (P46 A0). `caller` 는 출처가
    `agent` 인 실행만 채운다."""

    id: str
    agent_id: str
    source: str
    source_id: str | None
    scheduled_for: UtcDatetime | None
    acts_as: ActsAsOut
    input_bytes: int
    input_sha256: str | None
    session_id: str | None
    turn_id: str | None
    status: RunStatus
    reason: str | None
    attempts: int
    stop_requested_at: UtcDatetime | None
    last_event_at: UtcDatetime
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    deadline: UtcDatetime | None
    # skipped 행만 채운다
    count: int | None
    first_at: UtcDatetime | None
    last_at: UtcDatetime | None
    # 세션의 원장 몫. 목록과 읽기만 채우고 원장에 행이 없으면 null 이다
    usage: RunUsageOut | None = None
    cost_usd: float | None = None
    caller: RunCallerOut | None = None


class AgentRunsOut(BaseModel):
    runs: list[AgentRunOut]


class RunNowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Message


class RunSourceOut(BaseModel):
    """스케줄과 트리거가 함께 내는 칸."""

    id: str
    agent_id: str
    acts_as: ActsAsOut
    revision: int
    expires_at: UtcDatetime
    expires_in_days: int
    paused: bool
    paused_reason: str | None
    # 묶인 사람이 접근을 잃어 멈췄을 때의 원인 (account, password, role)
    paused_cause: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class BoundOut(BaseModel):
    """나를 대신하는 소스의 칸. 테넌트를 가로지르므로 테넌트와 Agent 이름을 함께 낸다. Agent 가
    지워졌으면 이름이 null 이다."""

    tenant: str
    agent_name: str | None


class ScheduleOut(RunSourceOut):
    cron: str
    time_zone: str
    message: str
    next_run_at: UtcDatetime
    last_fired_at: UtcDatetime | None
    last_run_id: str | None


class SchedulesOut(BaseModel):
    schedules: list[ScheduleOut]


class BoundScheduleOut(ScheduleOut, BoundOut):
    """나를 대신하는 스케줄 하나."""


class TriggerOut(RunSourceOut):
    """웹훅 트리거 하나. 비밀은 내지 않는다. `path` 는 호출 경로이고 화면이 공개 주소 앞에 붙인다.
    `previous_until` 은 앞 비밀을 받는 끝이다."""

    name: str
    template: str
    path: str
    previous_until: UtcDatetime | None


class TriggersOut(BaseModel):
    triggers: list[TriggerOut]


class TriggerSecretOut(TriggerOut):
    """만들 때와 비밀을 새로 만들 때의 답. `secret` 은 이때 한 번만 보인다."""

    secret: str


class BoundTriggerOut(TriggerOut, BoundOut):
    """나를 대신하는 트리거 하나."""


class RunsAsMeOut(BaseModel):
    """나에게 묶인 소스 (6절)."""

    schedules: list[BoundScheduleOut]
    triggers: list[BoundTriggerOut]


class TriggerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    template: str = Field(min_length=1, max_length=TEMPLATE_MAX_CHARS)
    expires_in_days: int = Field(default=EXPIRY_DEFAULT_DAYS)


class TriggerPatch(BaseModel):
    """보낸 칸만 바꾼다. 이름만 바꾸면 묶음은 그대로이고, 그 밖의 변경은 바꾼 사람에게 다시 묶고
    만료를 새로 센다."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    template: str | None = Field(default=None, min_length=1, max_length=TEMPLATE_MAX_CHARS)
    expires_in_days: int | None = None


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str = Field(min_length=1, max_length=200)
    time_zone: str = Field(default="UTC", min_length=1, max_length=64)
    message: Message
    expires_in_days: int = Field(default=EXPIRY_DEFAULT_DAYS)


class SchedulePatch(BaseModel):
    """보낸 칸만 바꾼다. 무엇을 바꾸든 바꾼 사람에게 다시 묶고 만료를 새로 센다."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = Field(default=None, min_length=1, max_length=200)
    time_zone: str | None = Field(default=None, min_length=1, max_length=64)
    message: Message | None = None
    expires_in_days: int | None = None
