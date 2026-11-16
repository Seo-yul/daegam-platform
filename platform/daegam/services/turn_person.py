"""턴의 사람과 그 위임의 앞뒤 (P53 5절, 4절의 끝내기).

채팅, 다시 생성, 결정 재개는 런타임을 부르기 전에 누구의 이름으로 턴 위임을 열지 정하고
(`TurnDelegation.chat`·`regenerate`·`decide`), 런타임의 답을 보고 소유자 기록을 적고 위임을 스스로
끝낸다(`send`). 위임을 열고 토큰을 만드는 일은 `services/turn_grants.py` 에 있다.

- **채팅과 다시 생성**은 부른 사람이다. 소유자 기록이 다른 사람을 대는 세션이면 위임을 열지 않는다.
  런타임이 어차피 그 사람을 거절한다.
- **결정**은 결정자, 결정의 입력, 소유자 검사로 정한다. 소유자가 결정하면 소유자 자신이다. 승인자가
  입력 없이(승인과 글 없는 거절만) 결정하면 기록의 소유자로 열고 승인자를 `decided_by` 에 적는다.
  소유자가 검사를 지나지 못하거나, 결정에 승인자의 입력이 있거나, 기록이 없으면 승인자 자신의
  위임이고 기록의 소유자가 있으면 `session_owner` 에 적는다. 어느 재개도 Agent 의 이름으로 돌아가지
  않는다.
- **소유자 기록**은 런타임이 턴을 받아들였을 때만 적는다. 요청에 세션 id 가 없었거나 답의
  `X-Daegam-Session-Id` 가 요청의 것과 같을 때다. 승인자가 소유자의 이름으로 재개한 결정은
  `expires_at` 만 늘린다.
- **끝내기.** 런타임을 부르다 답 전에 실패하면 `unreachable`, 오류 상태로 답하면 `refused`, 답에
  위임 id 의 메아리(`X-Daegam-Turn-Grant`)가 없으면 `not_accepted` 다. 런타임은 턴을 요청과 떼어
  돌리므로(P78 6절) 스트림이 닫히거나 답 전에 브라우저가 떠나도 턴을 이어 가고, 위임은 런타임의
  `/end` 와 기한이 끝낸다. 위임을 연 뒤 런타임을
  부르기 전의 일(`preparing`)이 실패하면 `not_sent` 다. 끝내기와 기록 쓰기는 턴을 막지 않는다.
  실패하면 로그만 남기고 위임은 기한이 끝낸다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anyio

from daegam.auth.principal import Principal, token_username
from daegam.services.agent_runtime import ProxyResponse
from daegam.services.turn_grants import (
    END_NOT_ACCEPTED,
    END_NOT_SENT,
    END_REFUSED,
    END_UNREACHABLE,
    GrantPerson,
    OpenedGrant,
    Trigger,
    TurnGrantService,
    turn_limits,
)
from daegam.store.agent_session_owners import MongoAgentSessionOwnerStore, SessionOwner
from daegam.store.agents import Agent
from daegam.store.base import utcnow

logger = logging.getLogger("daegam.turn_person")

# 런타임이 턴의 답에 싣는 헤더. 프록시가 넘긴 헤더는 소문자다
SESSION_HEADER = "x-daegam-session-id"
# 이 답이 연 시도의 id. 재개 턴은 멈춘 턴의 id 를 그대로 쓴다. 중지가 이 시도만 멈춘다(P78 2절)
TURN_ID_HEADER = "x-daegam-turn-id"
TURN_GRANT_HEADER = "x-daegam-turn-grant"
# 이 답의 턴을 런타임이 요청과 떼어 돌린다(P78 1절). 롤링 중에는 옛 이미지의 파드가 받을 수 있어
# Agent 의 기록이 아니라 답마다 본다
DETACHED_HEADER = "x-daegam-detached-turn"
# 소유자 기록이 마지막 턴과 세션 TTL 뒤에 더 남는 시간. 그때는 런타임이 세션을 닫았다
OWNER_RECORD_SLACK = timedelta(days=1)
# 결정이 결정자의 입력을 싣지 않는 꼴. 런타임의 `grant_gate.holds_input` 과 같은 규칙이다
_NO_INPUT_TYPES = frozenset({"approve", "reject"})

TurnCall = Callable[[dict[str, Any] | None], Awaitable[ProxyResponse]]


def holds_input(decisions: Sequence[Any]) -> bool:
    """결정이 결정자 자신의 입력을 싣는가. 승인과 글 없는(`message` 가 없거나 null) 거절만 아니다.
    빈 글도 대화에 실리므로 입력이고, 모르는 꼴과 새 종류도 입력이다 (5.3절)."""
    return any(
        not isinstance(d, dict)
        or d.get("type") not in _NO_INPUT_TYPES
        or (d.get("type") == "reject" and d.get("message") is not None)
        for d in decisions
    )


def person_of(user: Principal) -> GrantPerson:
    """요청한 사람이다. `tv` 는 local 모드 토큰의 것이고 다른 모드는 None 이다. 사용자 이름은
    토큰의 `preferred_username`, 없으면 이메일이다."""
    tv = user.raw.get("tv")
    return GrantPerson(
        sub=user.sub,
        username=token_username(user.raw, user.email),
        tv=tv if isinstance(tv, int) and not isinstance(tv, bool) else None,
    )


def owner_person(record: SessionOwner) -> GrantPerson:
    """승인자가 소유자의 이름으로 재개할 때의 사람이다. `tv` 는 소유자의 마지막 자기 요청의 것이다.
    oidc 모드의 한도는 기록의 `confirmed_at` 이 아니라 소유자의 사람 기록, 곧 IdP 의 시각으로 본다
    (P53b 6.3절). 기록의 `confirmed_at` 은 되돌릴 때를 위해 계속 적는다."""
    return GrantPerson(sub=record.sub, username=record.username, tv=record.tv)


@dataclass(frozen=True)
class TurnPlan:
    """런타임을 부르기 전에 정한 것이다. `confirms` 는 런타임이 받아들이면 소유자 기록을 적을
    사람(채팅, 다시 생성, 소유자의 결정), `extends` 는 승인자의 재개가 `expires_at` 을 늘릴 기록의
    소유자다. `at` 은 플랫폼이 요청을 인증한 때다."""

    doc: Agent
    session_id: str | None
    at: datetime
    # 비밀과 토큰을 담으므로 repr 에서 뺀다
    grant: OpenedGrant | None = field(default=None, repr=False)
    confirms: GrantPerson | None = None
    extends: str | None = None

    @property
    def owner_sub(self) -> str | None:
        """이 턴이 세션 소유자의 이름으로 돌면 그 소유자의 sub 다. 소유자 자신의 턴이거나 승인자가
        소유자의 이름으로 재개한 턴이다. 기억한 승인 선택이 이 사람의 것으로 간다(P43 5절)."""
        return self.confirms.sub if self.confirms is not None else self.extends


class TurnDelegation:
    """세 턴 라우트가 런타임을 부르는 앞뒤의 일이다. 라우트마다 요청 하나에 하나 만든다."""

    def __init__(
        self,
        grants: TurnGrantService,
        owners: MongoAgentSessionOwnerStore,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self.grants = grants
        self.owners = owners
        self.now = now

    # -- 누구의 이름인가 ---------------------------------------------------------------------

    async def chat(self, doc: Agent, user: Principal, session_id: str | None) -> TurnPlan:
        return await self._own_turn(doc, user, session_id, "chat")

    async def regenerate(self, doc: Agent, user: Principal, session_id: str) -> TurnPlan:
        return await self._own_turn(doc, user, session_id, "regenerate")

    async def decide(
        self,
        doc: Agent,
        user: Principal,
        session_id: str,
        decisions: Sequence[Any],
        *,
        approver: bool,
    ) -> TurnPlan:
        at = self.now()
        caller = person_of(user)
        record = await self.owners.get(doc.id, session_id)
        if record is None:
            # 소유자를 모른다. 런타임은 소유자가 아닌 결정자를 승인자일 때만 받는다
            grant = await self._open(doc, caller, "decide", session_id)
            return TurnPlan(doc, session_id, at, grant)
        if record.sub == caller.sub:
            grant = await self._open(doc, caller, "decide", session_id)
            return TurnPlan(doc, session_id, at, grant, confirms=caller)
        if not approver:
            # 런타임이 소유자도 승인자도 아닌 결정을 거절한다
            return TurnPlan(doc, session_id, at)
        if not holds_input(decisions):
            outcome = await self.grants.open(
                agent=doc,
                person=owner_person(record),
                trigger="decide",
                session_id=session_id,
                decided_by=caller.sub,
            )
            if outcome.refused is None:
                return TurnPlan(doc, session_id, at, outcome.grant, extends=record.sub)
            logger.info(
                "세션 %s 의 소유자가 검사를 지나지 못해(%s) 승인자 자신의 위임으로 재개한다",
                session_id,
                outcome.refused,
            )
        grant = await self._open(doc, caller, "decide", session_id, session_owner=record.sub)
        return TurnPlan(doc, session_id, at, grant)

    async def _own_turn(
        self, doc: Agent, user: Principal, session_id: str | None, trigger: Trigger
    ) -> TurnPlan:
        """채팅과 다시 생성이다. 사람은 부른 사람이고, 기록이 다른 사람을 대는 세션이면 위임이
        없다."""
        at = self.now()
        caller = person_of(user)
        if session_id is not None:
            record = await self.owners.get(doc.id, session_id)
            if record is not None and record.sub != caller.sub:
                return TurnPlan(doc, session_id, at)
        grant = await self._open(doc, caller, trigger, session_id)
        return TurnPlan(doc, session_id, at, grant, confirms=caller)

    async def _open(
        self,
        doc: Agent,
        person: GrantPerson,
        trigger: Trigger,
        session_id: str | None,
        *,
        session_owner: str | None = None,
    ) -> OpenedGrant | None:
        """부른 사람 자신의 위임이다. 검사를 지나지 못한 사람은 위임 없이 Agent 의 이름으로 돈다."""
        outcome = await self.grants.open(
            agent=doc,
            person=person,
            trigger=trigger,
            session_id=session_id,
            session_owner=session_owner,
        )
        return outcome.grant

    # -- 런타임을 부른 앞뒤 ------------------------------------------------------------------

    @asynccontextmanager
    async def preparing(self, plan: TurnPlan) -> AsyncIterator[None]:
        """위임을 연 뒤 런타임을 부르기 전에 라우트가 하는 일이다(기억한 승인 선택 읽기와 검사).
        그 일이 실패하거나 요청이 취소되면 연 위임을 `not_sent` 로 끝낸다."""
        try:
            yield
        except BaseException:
            await self._end(plan.grant, END_NOT_SENT)
            raise

    async def send(self, plan: TurnPlan, call: TurnCall) -> ProxyResponse:
        """위임을 실어 런타임을 부르고 답을 본다. 오류 상태의 답도 그대로 돌려주므로 라우트가
        플랫폼 형식으로 올린다."""
        grant = plan.grant
        try:
            proxied = await call(grant.turn_grant if grant is not None else None)
        except Exception:
            await self._end(grant, END_UNREACHABLE)
            raise
        if proxied.status >= 400:
            await self._end(grant, END_REFUSED)
            return proxied
        session_id = proxied.headers.get(SESSION_HEADER)
        if grant is not None and proxied.headers.get(TURN_GRANT_HEADER) != grant.id:
            # 그 필드를 모르는 옛 이미지의 파드가 받았다. 위임은 실제로 쓰이지 않았다
            await self._end(grant, END_NOT_ACCEPTED)
            grant = None
        if grant is not None and plan.session_id is None and session_id:
            await self._attach(grant, session_id)
        if session_id and plan.session_id in (None, session_id):
            await self._record(plan, session_id)
        return proxied

    async def _end(self, grant: OpenedGrant | None, reason: str) -> None:
        if grant is None:
            return
        with anyio.CancelScope(shield=True):
            try:
                await self.grants.end(grant.id, reason)
            except Exception as e:  # noqa: BLE001 - 끝내지 못한 위임은 기한이 끝낸다
                logger.warning("턴 위임 %s 를 %s 로 끝내지 못했다: %s", grant.id, reason, e)

    async def _attach(self, grant: OpenedGrant, session_id: str) -> None:
        try:
            await self.grants.attach_session(grant.id, session_id)
        except Exception as e:  # noqa: BLE001 - 세션 id 가 없어도 위임은 돈다
            logger.warning("턴 위임 %s 에 세션 id 를 적지 못했다: %s", grant.id, e)

    async def _record(self, plan: TurnPlan, session_id: str) -> None:
        """런타임이 받아들인 턴의 소유자 기록이다."""
        now = self.now()
        expires_at = now + timedelta(seconds=turn_limits(plan.doc).session_ttl_seconds)
        expires_at += OWNER_RECORD_SLACK
        try:
            if plan.confirms is not None:
                person = plan.confirms
                owner = SessionOwner(
                    agent_id=plan.doc.id,
                    session_id=session_id,
                    sub=person.sub,
                    username=person.username,
                    tv=person.tv,
                    confirmed_at=plan.at,
                    last_turn_at=now,
                    expires_at=expires_at,
                )
                if not await self.owners.confirm(owner):
                    logger.warning(
                        "Agent %s 의 세션 %s 는 다른 사람의 것으로 적혀 있어 기록을 바꾸지 않았다",
                        plan.doc.id,
                        session_id,
                    )
            elif plan.extends is not None:
                await self.owners.extend(
                    plan.doc.id, session_id, sub=plan.extends, expires_at=expires_at
                )
        except Exception as e:  # noqa: BLE001 - 기록이 없으면 승인자의 재개가 자기 위임으로 돈다
            logger.warning("세션 %s 의 소유자 기록을 적지 못했다: %s", session_id, e)
