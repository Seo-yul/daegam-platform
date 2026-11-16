"""Agent 답변의 피드백 (P36 6절, D61).

세션 소유자가 끝난 턴마다 좋아요·아쉬워요와 이유, 의견을 남기면 남의 전사를 읽을 수 있는 사람
(`agent:approve`)이 그것을 모아 보고 평가 단계(P45)가 읽을 NDJSON 으로 내보낸다. 현재 상태는
`store/agent_feedback.py` 에 턴마다 한 건이고, 만들기·고치기·거두기마다 `agent_events` 에
`feedback` 이벤트 하나를 남긴다. 이벤트에는 사람과 의견을 싣지 않는다. 그래도 같은 목록의
`turn_start` 와 `session_id`·`turn_id` 로 이으면 사람이 드러나므로 이벤트 목록 라우트는 이
이벤트를 `agent:approve` 에게만 보인다. 트레이스와는 `turn_id` 로 잇는다.

턴 확인은 런타임 세션 상세의 전사다. 소유자 판정은 라우터가 런타임 응답으로 하고(승인자 헤더 없이
부른다), 여기서는 전사의 `done` 가운데 그 `turn_id` 를 찾아 턴 번호를 얻는다. 없으면 404
turn_not_found 다. 다시 생성으로 걷힌 턴, 오류로 끝난 턴, `turn_id` 가 없는 옛 전사가 그렇다.

지우기 규칙은 셋이다. 플랫폼 경로로 세션을 지우면 그 세션의 평가를, Agent 를 지우면 그 Agent 의
평가를 지운다. 외부 경로의 일은 수집기 훅(`apply_runtime_events`)이 런타임 관측 이벤트로 안다.
`session_deleted` 면 그 세션의 평가를 지우고, `turn_end.replaced_turn_id` 면 그 턴의 평가에
`superseded` 를 단다. 플랫폼 경로의 일도 같은 이벤트를 내므로 훅이 한 번 더 지나가지만 결과는 같다.
다시 생성으로 바뀐 턴의 평가는 평가 자료로 쓸모가 있어 지우지 않는다.

평가는 그 질문과 답을 평가 사례로 복사해도 된다는 동의(`evaluation_use`, P45 8절)를 함께 가진다.
평가를 고치면 그 평가에서 복사한 사례의 수명이 따라 옮기고, 동의를 끄거나 평가를 거두면 복사본이
지워진다(`EvaluationCopyHooks`, `services/evaluation/copies.py`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from pymongo.errors import OperationFailure

from daegam.errors import ServiceError
from daegam.services.agent_runtime import done_index, transcript_items
from daegam.services.telemetry.normalize import (
    FEEDBACK_EVENT,
    REPLACED_TURN_ID,
    SESSION_DELETED,
    SOURCE_PLATFORM,
    TURN_END,
    agent_event_fields,
)
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.agent_feedback import (
    AgentFeedback,
    MongoAgentFeedbackStore,
    PageCursor,
    Rating,
)
from daegam.store.agents import Agent
from daegam.store.base import iso_utc, parse_iso_utc, utcnow

logger = logging.getLogger("daegam.agent_feedback")

EXPORT_SCHEMA = "daegam.feedback/1"
# 승인자 목록 한 쪽의 상한과 내보내기 한 번의 구간·줄 상한이다
PAGE_MAX = 200
EXPORT_MAX_DAYS = 90
EXPORT_MAX_LINES = 10_000
# 피드백 탭 머리의 수가 세는 기간이다
RECENT_DAYS = 7
# 쪽 넘김 표지는 마지막 행의 만든 시각과 id 를 이 글자로 잇는다. 둘 다 이 글자를 담지 않는다
PAGE_TOKEN_SEP = "_"


class FeedbackError(ServiceError):
    """피드백 서비스 오류. 전역 핸들러가 플랫폼 형식으로 낸다."""


@dataclass(frozen=True)
class FeedbackInput:
    """사람이 쓴 평가다. 이유와 의견은 아쉬워요가 아니어도 받는다."""

    rating: Rating
    tags: list[str]
    comment: str | None
    ephemeral: bool
    # 이 질문과 답을 평가 사례로 복사해도 된다는 동의 (P45 8절)
    evaluation_use: bool = False


class EvaluationCopyHooks(Protocol):
    """평가가 바뀌거나 사라질 때 그 평가에서 복사한 사례를 따라 움직인다 (P45 8절)."""

    async def rating_changed(self, fb: AgentFeedback) -> object: ...

    async def ratings_gone(self, rating_ids: list[str]) -> object: ...


def done_turn(session: Any, turn_id: str) -> int | None:
    """런타임 세션 상세의 전사에서 그 `turn_id` 로 끝난 턴의 번호다. 없으면 None 이다."""
    items = transcript_items(session)
    at = done_index(items, turn_id)
    turn = items[at].get("turn") if at is not None else None
    return turn if isinstance(turn, int) else None


def page_token(fb: AgentFeedback) -> str:
    """다음 쪽의 `before` 다. 만든 시각과 id 를 함께 실어 같은 시각의 행이 쪽 경계에 걸려도 빠지지
    않는다. 화면은 받은 값을 그대로 돌려줄 뿐 풀지 않는다."""
    return f"{iso_utc(fb.created_at)}{PAGE_TOKEN_SEP}{fb.id}"


def parse_page_token(token: str | None) -> PageCursor | None:
    """`page_token` 이 낸 값을 (만든 시각, id) 로 푼다. 비면 None 이고 읽을 수 없으면 422 다."""
    if not token:
        return None
    stamp, sep, feedback_id = token.rpartition(PAGE_TOKEN_SEP)
    created = parse_iso_utc(stamp) if sep else None
    if created is None or not feedback_id:
        raise FeedbackError(
            422, "invalid_value", "before must be the next_before of the previous page"
        )
    return created, feedback_id


def export_line(fb: AgentFeedback, agent: Agent) -> bytes:
    """내보내기 한 줄이다. 사람 식별자는 싣지 않는다."""
    row = {
        "schema": EXPORT_SCHEMA,
        "id": fb.id,
        "tenant": fb.tenant,
        "agent": {"id": agent.id, "name": agent.name},
        "revision": fb.revision,
        "session_id": fb.session_id,
        "turn_id": fb.turn_id,
        "turn": fb.turn,
        "rating": fb.rating,
        "tags": fb.tags,
        "comment": fb.comment,
        "ephemeral": fb.ephemeral,
        "superseded": fb.superseded,
        "created_at": iso_utc(fb.created_at),
        "updated_at": iso_utc(fb.updated_at),
    }
    return (json.dumps(row, ensure_ascii=False) + "\n").encode()


class AgentFeedbackService:
    def __init__(
        self,
        store: MongoAgentFeedbackStore,
        events: MongoAgentEventStore,
        *,
        retention_days: int,
        copies: EvaluationCopyHooks | None = None,
    ):
        self.store = store
        self.events = events
        self.retention = timedelta(days=retention_days)
        # 평가 사례의 복사본 (P45 8절). 평가 라우트가 바꾸면 그 자리에서 수명을 옮기거나 지운다
        self.copies = copies

    # -- 세션 소유자 ---------------------------------------------------------------------

    async def rate(
        self,
        agent: Agent,
        session: Any,
        *,
        session_id: str,
        turn_id: str,
        user_sub: str,
        value: FeedbackInput,
    ) -> AgentFeedback:
        """턴의 평가를 만들거나 고친다. 보존 기한은 고칠 때마다 다시 잰다."""
        turn = done_turn(session, turn_id)
        if turn is None:
            raise FeedbackError(
                404, "turn_not_found", f"session {session_id} has no finished turn {turn_id}"
            )
        now = utcnow()
        fb = await self.store.upsert(
            AgentFeedback(
                tenant=agent.tenant,
                agent_id=agent.id,
                session_id=session_id,
                turn_id=turn_id,
                turn=turn,
                user_sub=user_sub,
                rating=value.rating,
                tags=value.tags,
                comment=value.comment,
                ephemeral=value.ephemeral,
                evaluation_use=value.evaluation_use,
                revision=agent.deployed_revision,
                created_at=now,
                updated_at=now,
                expires_at=now + self.retention,
            )
        )
        await self._record(agent, session_id, turn_id, rating=fb.rating, tags=fb.tags)
        if self.copies is not None:
            await self.copies.rating_changed(fb)
        return fb

    async def withdraw(self, agent: Agent, *, session_id: str, turn_id: str, user_sub: str) -> None:
        """자기 평가를 거둔다. 거둘 것이 없으면 404 feedback_not_found 다."""
        gone = await self.store.withdraw(
            agent.tenant, agent.id, session_id, turn_id, user_sub=user_sub
        )
        if gone is None:
            raise FeedbackError(
                404, "feedback_not_found", f"no feedback of yours on turn {turn_id}"
            )
        await self._record(agent, session_id, turn_id, rating=None, tags=[])
        if self.copies is not None:
            await self.copies.ratings_gone([gone])

    async def mine(self, agent: Agent, session_id: str, *, user_sub: str) -> list[AgentFeedback]:
        return await self.store.for_session(agent.tenant, agent.id, session_id, user_sub=user_sub)

    async def _record(
        self,
        agent: Agent,
        session_id: str,
        turn_id: str,
        *,
        rating: str | None,
        tags: list[str],
    ) -> None:
        """`feedback` 이벤트다. 바뀔 때마다 새 id 이고 테넌트·인스턴스는 Agent 문서에서 찍는다.
        수집한 런타임 이벤트와 같은 모양이라 `AGENT_EVENT_FIELDS` 의 칸(평가에서는 `session_id` 만
        값이 있다)이 최상위이고 본문은 data 아래에 있다. 사람과 의견은 싣지 않는다. 거두면
        `data.rating` 이 null 이다."""
        body = {
            "source": SOURCE_PLATFORM,
            "session_id": session_id,
            "turn_id": turn_id,
            "rating": rating,
            "tags": tags,
        }
        await self.events.add(
            [
                {
                    "_id": uuid.uuid4().hex,
                    "ts": utcnow(),
                    "tenant": agent.tenant,
                    "instance_id": agent.id,
                    "event": FEEDBACK_EVENT,
                    "source": SOURCE_PLATFORM,
                    **agent_event_fields(body),
                }
            ]
        )

    # -- agent:approve ------------------------------------------------------------------

    async def page(
        self,
        agent: Agent,
        *,
        rating: str | None,
        tag: str | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[AgentFeedback], str | None]:
        """새것부터 한 쪽과 다음 쪽의 `before` 다. 마지막 쪽이면 None 이다."""
        rows = await self.store.page(
            agent.tenant,
            agent.id,
            rating=rating,
            tag=tag,
            before=parse_page_token(before),
            limit=limit,
        )
        return rows, page_token(rows[-1]) if len(rows) == limit else None

    async def recent_counts(self, agent: Agent) -> dict[str, int]:
        since = utcnow() - timedelta(days=RECENT_DAYS)
        return await self.store.counts_since(agent.tenant, agent.id, since)

    async def export(
        self,
        agent: Agent,
        *,
        since: datetime | None,
        until: datetime | None,
        rating: str | None,
    ) -> AsyncIterator[bytes]:
        """구간의 평가를 NDJSON 줄로 낸다. 구간은 기본이 지금까지 90일이다. since 가 until 보다
        앞이 아니면 422 invalid_value, 구간이 그보다 길면 422 export_range_too_long, 줄이 1만을
        넘으면 422 export_too_large 다. 검사는 첫 줄 전에 끝난다."""
        until = until or utcnow()
        since = since or until - timedelta(days=EXPORT_MAX_DAYS)
        if since >= until:
            raise FeedbackError(422, "invalid_value", "since must come before until")
        if until - since > timedelta(days=EXPORT_MAX_DAYS):
            raise FeedbackError(
                422,
                "export_range_too_long",
                f"the export range is at most {EXPORT_MAX_DAYS} days",
            )
        window = {"since": since, "until": until, "rating": rating}
        if await self.store.count_range(agent.tenant, agent.id, **window) > EXPORT_MAX_LINES:
            raise FeedbackError(
                422,
                "export_too_large",
                f"more than {EXPORT_MAX_LINES} feedback rows in the range; narrow it",
                max_lines=EXPORT_MAX_LINES,
            )
        rows = self.store.iter_range(agent.tenant, agent.id, **window)
        return (export_line(fb, agent) async for fb in rows)

    # -- 지우기 ------------------------------------------------------------------------

    async def forget_session(self, agent: Agent, session_id: str) -> int:
        return await self.store.delete_session(agent.tenant, agent.id, session_id)

    async def forget_agent(self, agent: Agent) -> int:
        return await self.store.delete_agent(agent.tenant, agent.id)

    async def apply_runtime_events(self, docs: Iterable[dict[str, Any]]) -> None:
        """수집기 훅이다. 저장한 관측 이벤트 가운데 세션 지우기와 다시 생성의 턴 마감을 평가에
        옮긴다. 겹쳐 읽은 이벤트가 다시 와도 결과는 같다.

        줄은 파드 로그라 밖의 글이 섞일 수 있다. 그래서 질의에 들어가는 칸은 비지 않은 글자일 때만
        쓰고, 한 줄의 저장소 오류는 남기고 건너뛴다. 한 줄 때문에 수집 커서가 그 자리에 묶이지 않게
        하려는 것이다. 연결 오류는 그대로 올려 다음 폴링이 같은 줄을 다시 읽는다."""
        for d in docs:
            try:
                await self._apply_runtime_event(d)
            except OperationFailure as e:
                logger.warning("feedback hook skipped a %s event: %s", d.get("event"), e)

    async def _apply_runtime_event(self, doc: dict[str, Any]) -> None:
        tenant, agent_id, session_id = (
            _text(doc.get(key)) for key in ("tenant", "instance_id", "session_id")
        )
        if not (tenant and agent_id and session_id):
            return
        if doc.get("event") == SESSION_DELETED:
            await self.store.delete_session(tenant, agent_id, session_id)
        elif doc.get("event") == TURN_END and (replaced := _replaced_turn(doc)):
            await self.store.mark_superseded(tenant, agent_id, session_id, replaced)


def _text(value: Any) -> str | None:
    """질의에 넣을 수 있는 값이다. 비지 않은 글자가 아니면 None 이다."""
    return value if isinstance(value, str) and value else None


def _replaced_turn(doc: dict[str, Any]) -> str | None:
    """수집한 `turn_end` 의 `replaced_turn_id` 다. 수집기는 최상위로 끌어올리지 않은 칸을 `data`
    아래에 둔다."""
    data = doc.get("data")
    return _text(data.get(REPLACED_TURN_ID)) if isinstance(data, dict) else None
