"""다시 생성과 고쳐 보내기다(P36 5절). 마지막 턴을 다시 생성하는 라우트를 routes.py 밖에 둔다.

  POST /sessions/{sid}/regenerate  {user_sub?, message?, ephemeral?} -> /chat 과 같은 SSE

`message` 가 없으면 마지막 질문 그대로, 있으면 마지막 질문을 그 글로 바꿔 다시 생성한다. 마지막 턴만
되고, 오류나 중지로 끝난 턴도 마지막 턴 시도라 대상이다. 검사는 `/chat` 과 같은 함수를 같은 차례로
쓴다(turn_gate.py). 요청 검사(도구 준비, 분당 한도, 고쳐 보낼 때의 입력 길이와 입력 가드레일, 일당
예산), 소유자, 닫힘, 승인 대기, 세션 예산, 턴 리스 다음에 입력 체크포인트를 찾는다. 소유자만 부르고
승인자 헤더는 통하지 않는다.

질문을 그대로 다시 생성할 때도 입력 체크포인트에서 읽은 질문을 입력 길이와 입력 가드레일로 다시
검사한다. 질문이 단 글 첨부는 그대로 보내든 고쳐 보내든 `/chat` 의 입구처럼 첨부마다 입력
가드레일로 검사한다(attachments.py 의 `check_attachment_guard`). 그 질문과 첨부가 처음 지난 검사는
그때의 정책일 뿐이다. 재배포로 강화한 규칙이 그 전에 받은 질문과 첨부에도 적용돼야 하고, flag
위반도 다시 돌 때마다 앞머리와 관측 이벤트에 남아야 감사에서 되풀이를 볼 수 있다. 검사하려고 읽은
세션의 첨부는 씨앗에 실어 턴을 열 때 저장소를 다시 읽지 않는다.

입력 체크포인트는 리스를 잡은 뒤에 찾는다. 먼저 찾으면 그 사이에 끝난 턴까지 버릴 수 있기 때문이다.
입력 체크포인트 E 는 가장 최근 루트 체크포인트(head)에서 부모 사슬을 거슬러 처음 만나는
`source: input` 체크포인트다. `thread_config` 그대로 `alist(filter={"source": "input"}, limit=1)` 를
부르면 서브에이전트가 `tools:<id>` 네임스페이스에 남긴 부모 없는 input 체크포인트를 집는다.
Postgres·Redis 의 alist 는 네임스페이스와 상관없이 id 를 거꾸로 세우기 때문이다. 결정 재개는 input
체크포인트를 남기지 않으므로 승인을 거친 턴도 같은 E 를 찾는다.

E 에 부모가 있으면 그 부모를 `checkpoint_id` 로 주어 새 입력으로 돌린다(갈래, fork.py). 다음
`aget_state` 는 가장 최근 체크포인트인 새 갈래를 읽는다. 부모가 없는 첫 턴만 스레드를 지우고 새로
돈다. 질문은 E 의 입력 채널(`__start__`)에서 읽는다. E 는 입력이 그 채널에 들어간 직후라
`aget_state(E)` 의 마지막 사람 메시지는 앞 턴의 질문이기 때문이다. 그 사람 메시지를 글만 바꾸거나
그대로 새 id `u-<turn_id>` 로 다시 싣는다. 이 턴의 씨앗(`TurnState.seeded`)은 `aget_state(E)` 로
만든다. E 의 메시지가 곧 턴 직전 이력이다.

턴 번호는 바꾸는 턴의 것이고 `turn_id` 는 새로 낸다(turn_record.py). 버린 갈래의 토큰도 쓴 것이라
세션 사용량과 일당 카운터에 남는다. 체크포인트 밖인 장기 기억과 도구가 한 일은 되돌리지 않는다.
전사는 갈래가 생겼을 때만 맨 앞에 rewind 표식을 두고 읽을 때 접는다(transcript.py). 갈래가
생겼는지는 턴을 마감할 때 가장 최근 루트 체크포인트가 시작 때와 달라졌는가로 본다(`Fork.settle`).

버린 갈래는 세션이 닫히거나 지워질 때까지 체크포인터에 남는다. F0 실측으로 Redis 는 첨부가 없어도
실행 한 번에 약 75KB 이고, 50턴에 턴마다 다시 생성 셋을 둔 세션이 14.9MB 였다. 새 턴은 `max_turns`
가 세션의 실행 수를 묶지만 다시 생성은 턴 번호를 늘리지 않아 그 묶음 밖이다. 그래서 한 턴을
다시 생성하는 횟수를 하루(UTC)에 `MAX_REGENERATIONS_PER_TURN` 번으로 묶고, 넘으면 429
`max_regenerations` 다. 앞 턴은 다음 턴이 오면 다시 생성할 수 없으므로 세션의 실행 수는 대략
`max_turns` 의 11배 안에 든다. 기본 50턴이면 Redis 로 약 41MB 다. 횟수는 공유 카운터가
센다(counters.py). 상한은 번호를 정하자마자 입력 체크포인트를 찾기 전에 본다. 찾기는 세션 길이에
비례하는 저장소 왕복이라 상한을 넘긴 요청이 그 비용을 치르지 않게 하려는 것이다.

리스 안의 일은 둘로 나뉜다. `_plan` 은 읽고 판정만 하고, `_commit` 이 횟수를 올리고 첫 턴의 스레드를
지우고 시도 칸을 색인에 적는다. 횟수는 입력 체크포인트를 찾은 뒤에 올리므로 다시 생성할 턴이 없는
요청은 세지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.constants import START
from pydantic import BaseModel, Field

from runtime import stream
from runtime.approvals import MAX_CHOICES, ApprovalChoice
from runtime.attachments import check_attachment_guard, notice_of, session_uploads, typed_text
from runtime.auth import Caller, CallerDep
from runtime.counters import Counters, Window, day_window, regenerate_key
from runtime.fork import Fork, checkpoint_id, root_head
from runtime.grant_gate import check_grant, refuse_external_fields
from runtime.guards import Violation
from runtime.sessions import SessionRecord, utcnow
from runtime.state import Runtime, RuntimeState
from runtime.turn import open_turn
from runtime.turn_gate import (
    check_input,
    check_open_session,
    check_request,
    check_session_budget,
    check_still_open,
    error,
    guardrail_prelude,
    held_lease,
    load_session,
    note_attempt,
    rejected,
)
from runtime.turn_grants import TurnGrant
from runtime.turn_record import RUN_REGENERATE, TurnAttempt, human_message_id, turn_id_of
from runtime.uploads import TurnUploads, message_refs

router = APIRouter()

MAX_REGENERATIONS_PER_TURN = 10
"""한 턴을 하루(UTC)에 다시 생성할 수 있는 횟수다. 까닭은 모듈 설명에 적었다."""
CHAIN_LIMIT = 5000
"""입력 체크포인트를 찾아 부모 사슬을 거슬러 가는 걸음의 상한이다. 넘으면 다시 생성할 턴이 없는
것으로 본다."""
INPUT_SOURCE = "input"
"""입력이 입력 채널에 들어간 직후의 체크포인트가 metadata.source 에 다는 값이다."""


class RegenerateRequest(BaseModel):
    # 프록시 경로의 주체. 외부 경로는 무시하고 토큰 sub 를 쓴다. 읽는 곳은 runtime/auth.py 뿐이다
    user_sub: str | None = None
    # 있으면 마지막 질문을 이 글로 바꿔 보낸다(고쳐 보내기). 없으면 마지막 질문 그대로다
    message: str | None = Field(default=None, min_length=1)
    # /chat 과 같은 뜻이다. 재개 턴처럼 다시 실어야 그 턴이 임시로 돈다
    ephemeral: bool = False
    # 플랫폼이 실은 턴 위임(P53 6절). 프록시 경로만 받는다(grant_gate.py)
    turn_grant: TurnGrant | None = None
    # 이 턴의 사람이 기억한 선택(P43 5절). 프록시 경로만 받는다(approvals.py)
    approval_choices: list[ApprovalChoice] | None = Field(default=None, max_length=MAX_CHOICES)


@dataclass(frozen=True)
class TurnRegenerations:
    """한 턴을 오늘(UTC) 다시 생성한 횟수다. 공유 카운터의 키와 창을 든다(counters.py)."""

    counters: Counters
    key: str
    window: Window

    @classmethod
    def of(
        cls, state: RuntimeState, record: SessionRecord, attempt: TurnAttempt
    ) -> TurnRegenerations:
        window = day_window(utcnow())
        key = regenerate_key(state.config.instance["id"], record.session_id, attempt.number, window)
        return cls(state.store.counters, key, window)

    async def check(self, state: RuntimeState, caller: Caller, session_id: str) -> None:
        """이미 `MAX_REGENERATIONS_PER_TURN` 번 다시 생성했으면 429 다. 입력 체크포인트를 찾기 전에
        부른다."""
        if await self.counters.get(self.key) < MAX_REGENERATIONS_PER_TURN:
            return
        raise rejected(
            state,
            429,
            "max_regenerations",
            f"한 턴은 하루에 {MAX_REGENERATIONS_PER_TURN}번까지만 다시 생성할 수 있다. "
            "새 메시지를 보내야 한다",
            user_sub=caller.sub,
            session_id=session_id,
        )

    async def add(self) -> None:
        """다시 생성 하나를 센다. check 와 같은 턴 리스 안이라 같은 세션의 다른 요청이 그 사이에
        끼지 않는다."""
        await self.counters.incr(self.key, 1, window=self.window)


@dataclass(frozen=True)
class Plan:
    """리스 안에서 정한 다시 생성이다. 다시 실을 메시지, 세션의 첨부를 실은 이 턴의 씨앗, 갈래,
    다시 싣는 질문과 그 글 첨부가 걸린 flag 위반, 이 턴의 다시 생성 횟수다."""

    record: SessionRecord
    attempt: TurnAttempt
    message: HumanMessage
    seed: stream.TurnState
    fork: Fork
    flagged: list[Violation]
    regenerations: TurnRegenerations


@router.post("/sessions/{session_id}/regenerate")
async def regenerate(
    session_id: str, body: RegenerateRequest, caller: CallerDep, state: Runtime
) -> StreamingResponse:
    refuse_external_fields(
        caller, turn_grant=body.turn_grant, approval_choices=body.approval_choices
    )
    flagged = await check_request(state, caller, session_id, body.message)
    record = await load_session(state, session_id)
    check_open_session(caller, record)
    grant = check_grant(caller, body.turn_grant, owner=record.user_sub)
    check_session_budget(state, record)
    async with held_lease(state, session_id) as lease:
        plan = await _plan(state, caller, session_id, body)
        await _commit(state, plan, token=lease.token)
        return await open_turn(
            state,
            plan.record,
            plan.message,
            lease=lease,
            attempt=plan.attempt,
            actor=plan.record.user_sub,
            turn=plan.seed,
            ephemeral=body.ephemeral,
            prelude=guardrail_prelude([*flagged, *plan.flagged]),
            fork=plan.fork,
            grant=grant,
            choices=body.approval_choices,
        )


async def _plan(
    state: RuntimeState, caller: Caller, session_id: str, body: RegenerateRequest
) -> Plan:
    """리스를 잡은 뒤 다시 생성을 정한다. 읽고 판정만 하고 쓰지 않는다.

    색인은 리스 안에서 다시 읽는다. 그 사이에 끝난 턴이 있으면 그 턴이 마지막 턴이고 사용량도 그
    턴까지 든 값이어야 하기 때문이다. 다시 읽은 한 줄은 이 요청의 리스가 잡고 있어 TTL 이 지나도
    닫힌 것으로 읽히지 않으므로 TTL 을 다시 본다(`check_still_open`, P44 11절). 번호를 정하면
    먼저 횟수 상한을 보고 입력 체크포인트를 찾은 뒤, 다시 싣는 질문과 그 글 첨부를 검사하고 씨앗을
    읽는다. 씨앗은 첫 턴의 스레드를 지우기(`_commit`) 전에 읽어야 한다.
    """
    record = await load_session(state, session_id)
    check_open_session(caller, record)
    check_still_open(state, record, resume=False)
    attempt = TurnAttempt.regenerated(record)
    regenerations = TurnRegenerations.of(state, record, attempt)
    await regenerations.check(state, caller, session_id)
    saver = state.store.checkpointer
    head = await root_head(saver, session_id)
    checkpoint = await find_input_checkpoint(saver, head)
    question = input_question(checkpoint) if checkpoint is not None else None
    if checkpoint is None or question is None:
        raise error(409, "nothing_to_regenerate", "다시 생성할 턴이 없다")
    message = resend_message(question, body.message, attempt.turn_id)
    flagged = (
        check_input(state, caller, typed_text(message), session_id) if body.message is None else []
    )
    uploads = TurnUploads(files=await session_uploads(state, record))
    flagged += check_attachment_guard(
        state, caller, uploads.select(message_refs(message)), session_id
    )
    seed = await stream.seed_from(state.agent, checkpoint.config, ephemeral=body.ephemeral)
    seed.uploads = uploads
    fork = Fork(
        session_id=session_id,
        parent_id=checkpoint_id(checkpoint.parent_config),
        head_id=checkpoint_id(head.config if head else None),
        replaced_turn_id=turn_id_of(question.id),
        edited=body.message is not None,
    )
    return Plan(
        record=record,
        attempt=attempt,
        message=message,
        seed=seed,
        fork=fork,
        flagged=flagged,
        regenerations=regenerations,
    )


async def _commit(state: RuntimeState, plan: Plan, *, token: str) -> None:
    """정한 다시 생성을 저장소에 적는다. 횟수를 올리고, 첫 턴이면 스레드를 지우고, 이 시도의 칸을
    색인에 적는다."""
    await plan.regenerations.add()
    if plan.fork.parent_id is None:
        await state.store.delete_checkpoints(plan.record.session_id)
    await note_attempt(
        state,
        plan.record,
        plan.attempt,
        token=token,
        kind=RUN_REGENERATE,
        trace=plan.seed.trace,
    )


async def find_input_checkpoint(
    saver: BaseCheckpointSaver, head: CheckpointTuple | None
) -> CheckpointTuple | None:
    """루트 head 에서 부모를 거슬러 처음 만나는 input 체크포인트다. 사슬이 끝나거나 `CHAIN_LIMIT`
    걸음을 넘으면 None 이다."""
    tup = head
    for _ in range(CHAIN_LIMIT):
        if tup is None:
            return None
        if (tup.metadata or {}).get("source") == INPUT_SOURCE:
            return tup
        if tup.parent_config is None:
            return None
        tup = await saver.aget_tuple(tup.parent_config)
    return None


def input_question(checkpoint: CheckpointTuple) -> HumanMessage | None:
    """입력 체크포인트의 입력 채널에 든 사람 메시지다. 그 턴에 받은 입력 그대로라 옛 세션도 같은
    길로 풀린다."""
    start = checkpoint.checkpoint["channel_values"].get(START)
    messages = start.get("messages") if isinstance(start, dict) else None
    return next((m for m in reversed(messages or []) if isinstance(m, HumanMessage)), None)


def resend_message(question: HumanMessage, message: str | None, turn_id: str) -> HumanMessage:
    """다시 실을 사람 메시지다. 고쳐 보내면 친 글만 바꾸고 id 는 새 턴의 것을 단다. 입력에 든
    메시지라 `additional_kwargs` 의 첨부 참조도 그대로 옮겨지고, 첨부 안내는 바꾼 글 뒤에 다시
    붙는다(attachments.py)."""
    update: dict[str, Any] = {"id": human_message_id(turn_id)}
    if message is not None:
        update["content"] = message + notice_of(question)
    return question.model_copy(update=update, deep=True)
