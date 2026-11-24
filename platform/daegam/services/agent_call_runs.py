"""다른 Agent 를 Tool 로 부르는 호출 (P46 6.3~6.6절). 호출 하나는 호출받는 Agent 의 실행이다.

부르는 Agent 의 런타임이 기계 경로 셋(`api/agent_calls.py`)으로 실행을 시작하고 묻고 취소한다. 답은
모두 `{run_id, status, answer?, code?}` 이고 요청 하나는 25초 안에 답한다.

- **누구의 호출인가.** 토큰 인증 모드에서는 부르는 턴의 위임이 그 Agent 를 대상으로 낸 토큰만
  받는다(`verify_agent_call_token`). 토큰이 댄 위임을 찾아 열려 있고 마감 전이고 그 사람과 그
  대상의 것인지, 위임의 Agent 가 토큰의 행위자이고 같은 테넌트인지를 본 뒤 사람을 다시 본다
  (`BoundPersonCheck.lost`, `agent:chat` 과 뿌리 턴의 한도). 인스턴스 토큰만 온 요청은 403
  `acts_as_unavailable` 이다. header 모드에서만 인스턴스 토큰과 파드가 댄 세션·턴·소유자를 받고,
  두 Agent 가 같은 테넌트이고 부르는 리비전이 그 Agent 를 Tool 로 쓰는지를 본다. 묻기와 취소는
  실행의 `caller` 가 그 턴이 아니면 404 다.
- **사슬.** 부르는 세션의 실행 기록(`caller.chain`)에 부르는 Agent 를 더한 것이다. 채팅 세션은
  기록이 없어 부르는 Agent 하나다. 호출받는 Agent 가 사슬에 있으면 409 `agent_call_cycle`, 사슬이
  셋을 넘게 되면 409 `agent_call_too_deep` 이다. 승인으로 재개한 턴도 같은 기록을 읽는다.
- **한 턴의 호출 수.** 부르는 턴 하나는 다섯까지 시작한다(429 `agent_call_fanout`). 위임의 조건부
  갱신이 세고(`MongoTurnGrantStore.count_call`), 위임 없는 턴은 세션과 턴으로 센다.
- **상한과 속도.** 호출받는 Agent 마다 호출의 상한(`CALL_CAPS`)을 스케줄·웹훅·지금 실행과 따로
  세고, 넘으면 429 `agent_call_busy` 다. 배포 중인 Agent 도 같은 코드다. 사람마다의 속도는
  호출받는 리비전의 `entry.rate_limit` 이고 레플리카마다의 버킷이다(429 `agent_call_rate_limited`).
- **시작.** 실행 기록을 리스를 건 채 넣고 시작 워커의 한 걸음(`RunStartWorker.process`)을 그
  자리에서 돌린다. 깨우는 중이면 그 간격으로 다시 하고, 이 요청 안에 다시 할 수 없으면 기록을
  `starting` 으로 두어 틱이 잡게 한다.
- **믿지 않는 입력.** 메시지는 부르는 모델이 쓴 글이다. 항목이 호출을 믿지 않거나
  (`calls_trusted`), 부르는 대화가 바깥 내용을 가졌거나, 부르는 리비전이 바깥 표시를 하나라도
  껐거나, header 모드면 실행은 `untrusted` 다.
- **답.** 끝난 실행의 답은 실행 세션의 전사에서 마지막 `done` 으로 끝난 턴의 말이다. 전사를 아직
  읽지 못했으면 끝나지 않은 것처럼 `running` 으로 답해 다시 묻게 한다.
- **끝.** 부른 턴의 위임이 끝나면 아직 첫 턴인 실행을 `caller_ended` 로 취소한다(`caller_ended`,
  위임의 끝이 부른다). 마감이 지났거나 그 끝을 놓친 위임의 호출은 지켜보기의 틱이 취소한다(`sweep`).
- **끄기.** 호출이나 실행이 꺼진 플랫폼은 세 경로가 409 `agent_calls_disabled` 다.
- **감사.** 요청마다 사람, 부르는 Agent 와 호출받는 Agent, 실행, 결과를 실은 행 하나를 쓴다.
  Bearer 가 없는 요청과 뜻밖의 오류(500)도 행을 남긴다. 본문의 모양(422)과 크기(413)로 경로에
  닿기 전에 거절된 요청은 행이 없다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, get_args
from urllib.parse import unquote

from fastapi import HTTPException

from daegam.audit.store import AuditEvent, MongoAuditStore
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import InternalIssuer, verify_agent_call_token
from daegam.config import Settings
from daegam.errors import ServiceError, retry_after_header, unauthorized
from daegam.services.agent_calls import CALL_CYCLE, CALL_GRACE_SECONDS, SOURCE_UNPUBLISHED
from daegam.services.agent_graph import (
    AGENT_SOURCE,
    EntryData,
    agent_source_items,
    analyze_structure,
)
from daegam.services.agent_publish import CALLS_DISABLED, AgentPublishing
from daegam.services.agent_runtime import (
    AgentRuntimeProxy,
    last_answer,
    serves_turns,
    transcript_items,
)
from daegam.services.runs.person import ACTS_AS_UNAVAILABLE, CALL_SCOPES, ended_reason
from daegam.services.runs.service import AGENT_CALL_BUSY, CALL_CAPS, CAP_RETRY_AFTER, RunService
from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.services.tool_effects import tools_nodes
from daegam.services.turn_grants import call_targets, turn_limits
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, agent_of_token
from daegam.store.agent_runs import (
    CALL_SOURCE,
    MOVING_STATUSES,
    OPEN_STATUSES,
    ActsAs,
    AgentRun,
    RunCaller,
    input_digest,
    new_run,
)
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import ItemLease, new_id, utcnow
from daegam.store.hub import AGENT_KIND, HubItem, MongoHubStore
from daegam.store.turn_grants import GrantTarget, MongoTurnGrantStore, TurnGrant
from daegam.urls import AGENT_CALL_PATH, API_PREFIX

logger = logging.getLogger("daegam.agent_calls")

# 사슬에 드는 Agent 수의 위 끝과 부르는 턴 하나가 시작하는 호출 수 (6.5절)
MAX_CHAIN = 3
MAX_CALLS_PER_TURN = 5
# 요청 하나가 답을 기다리는 시간(초)의 위 끝이다. 런타임의 요청 상한 30초 안이다
REQUEST_WAIT_SECONDS = 25.0
# 기다리는 동안 실행 기록을 다시 읽는 간격(초)
POLL_STEP_SECONDS = 0.5
# 호출 하나가 호출받는 Agent 의 턴 시간보다 더 기다리는 시간. 컴파일이 Tool 에 박는 값이다
CALL_GRACE = timedelta(seconds=CALL_GRACE_SECONDS)
# 위임에 세션이 아직 붙지 않은 턴의 호출이 위임을 다시 읽기까지 기다리는 시간(초)
SESSION_ATTACH_WAIT_SECONDS = 0.5
# 취소가 받는 까닭. 런타임의 `CANCEL_TIMEOUT`, `CANCEL_ENDED` 와 같다
CancelReason = Literal["caller_timeout", "caller_ended"]
CALLER_TIMEOUT, CALLER_ENDED = get_args(CancelReason)
# 답 없이 끝난 실행의 상태. 답의 `code` 가 그 까닭이다
ENDED_WITHOUT_ANSWER = frozenset({"failed", "refused", "cancelled"})
# header 모드에서 파드가 소유자를 대지 않은 호출의 사람
HEADER_SUB = "header"
# 사람마다의 속도 버킷을 잊기까지의 한가한 시간(초)과 버킷 수의 위 끝
RATE_IDLE_SECONDS = 3600.0
RATE_ENTRIES = 10_000
RATE_RETRY_AFTER = timedelta(seconds=10)

CALL_TOO_DEEP = "agent_call_too_deep"
CALL_FANOUT = "agent_call_fanout"
CALL_RATE_LIMITED = "agent_call_rate_limited"
TURN_GRANT_ENDED = "turn_grant_ended"
# 감사 행의 리소스와 요청마다의 action
AUDIT_RESOURCE = "agent-calls"
START, POLL, CANCEL = "agent_call_start", "agent_call_poll", "agent_call_cancel"


class AgentCallError(ServiceError):
    """호출 경로의 거절이다. 런타임이 코드와 문장을 Tool 오류로 모델에게 옮긴다."""


class InlineStarter(Protocol):
    """경로가 그 자리에서 돌리는 시작 워커의 한 걸음이다(`RunStartWorker`)."""

    owner: str
    lease_seconds: float

    async def process(self, item: AgentRun) -> None: ...


@dataclass(frozen=True)
class CallCredentials:
    """요청 하나가 실어 온 것이다. bearer 가 없으면 401 이다. session_id, turn_id, owner 는 위임
    없는 턴만 대고 header 모드에서만 읽는다. outside 는 부르는 대화가 바깥 내용을 가졌는가다."""

    bearer: str | None
    session_id: str | None = None
    turn_id: str | None = None
    owner: str | None = None
    outside: bool = True


@dataclass(frozen=True)
class _Caller:
    """확인을 지난 부르는 쪽이다. agent 는 부르는 Agent 의 문서(미리 보기면 그 미리 보기)이고
    grant 는 token 모드의 위임이다. run 은 부르는 세션의 실행이고 채팅 세션이면 없다."""

    agent: Agent
    acts_as: ActsAs
    grant: TurnGrant | None
    session_id: str | None
    turn_id: str | None
    run: AgentRun | None

    @property
    def chain(self) -> list[str]:
        """뿌리부터 부르는 Agent 까지다."""
        before = self.run.caller.chain if self.run is not None and self.run.caller else []
        return [*before, self.agent.acting_id]

    @property
    def long_bound(self) -> bool:
        """뿌리 턴이 실행인가. 부른 실행이면 그 기록의 것이고, 스케줄·웹훅·지금 실행이면 참이다."""
        if self.run is None:
            return False
        return self.run.caller.long_bound if self.run.caller is not None else True

    def started(self, run: AgentRun) -> bool:
        """run 이 이 턴이 시작한 호출인가."""
        caller = run.caller
        if caller is None or caller.agent_id != self.agent.acting_id:
            return False
        if self.grant is not None:
            return caller.grant_id == self.grant.id
        return caller.grant_id is None and (caller.session_id, caller.turn_id) == (
            self.session_id,
            self.turn_id,
        )


@dataclass
class _Seen:
    """감사 행이 적을, 요청을 다루며 알게 된 것이다."""

    caller: _Caller | None = None
    run_id: str | None = None
    status: str | None = None


def _refused(status: int, code: str, detail: str, **extra: Any) -> AgentCallError:
    return AgentCallError(status, code, detail, **extra)


def _not_found() -> AgentCallError:
    return _refused(404, "not_found", "no such agent call")


def _busy(detail: str) -> AgentCallError:
    return AgentCallError(429, AGENT_CALL_BUSY, detail, headers={"Retry-After": CAP_RETRY_AFTER})


def turns_outside_off(agent: Agent) -> bool:
    """배포된 리비전이 어느 Tool 의 바깥 표시를 껐는가. 그런 Agent 의 대화는 바깥 내용을 가졌어도
    표시가 없을 수 있다."""
    rev = agent.deployed
    if rev is None:
        return True
    return any(mark is False for _, data in tools_nodes(rev) for mark in data.outside.values())


class AgentCallRuns:
    def __init__(
        self,
        *,
        agents: MongoAgentStore,
        hub: MongoHubStore,
        grants: MongoTurnGrantStore,
        instance_tokens: MongoAgentInstanceTokenStore,
        run_service: RunService,
        publishing: AgentPublishing,
        proxy: AgentRuntimeProxy,
        starter: InlineStarter,
        issuer: InternalIssuer,
        settings: Settings,
        audit: MongoAuditStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        # 부르는 Agent 는 미리 보기일 수 있다. 호출받는 Agent 는 게시한 것이라 미리 보기가 아니다
        self.agents = agents
        self.callers = agents.with_previews()
        self.hub = hub
        self.grants = grants
        self.instance_tokens = instance_tokens
        self.run_service = run_service
        self.runs = run_service.runs
        self.person = run_service.person
        self.publishing = publishing
        self.proxy = proxy
        self.starter = starter
        self.issuer = issuer
        self.settings = settings
        self.audit = audit
        self.clock = clock
        # 분당 한도마다의 버킷 묶음이다. 한도는 호출받는 리비전마다 다르다
        self._rates: dict[int, KeyedBuckets] = {}

    # -- 경로 셋 -----------------------------------------------------------------------------

    async def start(self, called_id: str, creds: CallCredentials, message: str) -> dict[str, Any]:
        """실행을 시작하고 `REQUEST_WAIT_SECONDS` 안에 그 상태를 답한다. 거절은 실행 기록을 남기지
        않는다."""
        ends = self.clock() + REQUEST_WAIT_SECONDS
        seen = _Seen()

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self._caller(called_id, creds)
            called, item = await self._called(caller, called_id)
            run = await self._admit(caller, called, item, creds, message)
            seen.run_id = run.id
            await self._start_inline(run, ends)
            return await self._settled(called, run, ends)

        return await self._audited(START, called_id, seen, work)

    async def poll(
        self, called_id: str, run_id: str, creds: CallCredentials, wait: float
    ) -> dict[str, Any]:
        """실행이 끝나거나 wait 초가 지나면 그 상태를 답한다."""
        ends = self.clock() + min(max(wait, 0.0), REQUEST_WAIT_SECONDS)
        seen = _Seen(run_id=run_id)

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self._caller(called_id, creds)
            run = await self._started_run(caller, called_id, run_id)
            called = await self.agents.get(called_id, tenant=caller.agent.tenant)
            return await self._settled(called, run, ends)

        return await self._audited(POLL, called_id, seen, work)

    async def cancel(
        self, called_id: str, run_id: str, creds: CallCredentials, reason: str
    ) -> dict[str, Any]:
        """실행을 reason 으로 취소한다. 이미 끝난 실행은 그대로 둔다."""
        seen = _Seen(run_id=run_id)

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self._caller(called_id, creds)
            run = await self._started_run(caller, called_id, run_id)
            if run.status in OPEN_STATUSES:
                run = await self.run_service.stop(run, reason)
            return {"run_id": run.id, "status": run.status, "cancelled": run.status == "cancelled"}

        return await self._audited(CANCEL, called_id, seen, work)

    # -- 부른 턴의 끝 (6.5절) ------------------------------------------------------------------

    async def caller_ended(self, grant_id: str) -> None:
        """위임이 끝났다. 그 턴이 시작한 호출 가운데 아직 첫 턴인 것을 취소한다."""
        await self._cancel_for([grant_id])

    async def sweep(self) -> int:
        """아직 첫 턴인 호출의 위임 가운데 끝났거나 마감이 지난 것의 호출을 취소한다. 도는 턴을
        멈추라고 한 실행 수다."""
        grant_ids = await self.runs.first_turn_caller_grants()
        return await self._cancel_for(await self.grants.closed(grant_ids, utcnow()))

    async def _cancel_for(self, grant_ids: list[str]) -> int:
        running = await self.runs.cancel_first_turn_for_grants(grant_ids, CALLER_ENDED, utcnow())
        await self.run_service.stop_turns(running)
        return len(running)

    # -- 누구의 호출인가 (6.4절) ---------------------------------------------------------------

    async def _caller(self, called_id: str, creds: CallCredentials) -> _Caller:
        if creds.bearer is None:
            raise unauthorized("Bearer token required")
        if not self.settings.agent_calls_on:
            raise _refused(409, CALLS_DISABLED, "agent calls are turned off on this platform")
        if not self.settings.token_auth:
            return await self._header_caller(called_id, creds.bearer, creds)
        return await self._granted_caller(called_id, creds.bearer)

    async def _granted_caller(self, called_id: str, bearer: str) -> _Caller:
        """토큰이 댄 위임의 턴이다. 위임이 열려 있고 그 사람과 대상과 부르는 Agent 의 것일 때다."""
        try:
            claims = verify_agent_call_token(
                bearer, self.issuer.signer, issuer=self.issuer.issuer, agent_id=called_id
            )
        except (AuthError, RuntimeError) as e:
            if InternalIssuer.peek(bearer) is None:
                # JWT 가 아니다. 위임 없는 턴이 인스턴스 토큰으로 불렀다
                raise _unavailable("this platform takes agent calls only with a turn token") from e
            raise unauthorized("invalid agent call token") from e
        grant = await self.grants.get(claims.grant_id)
        target = GrantTarget(kind=AGENT_SOURCE, id=called_id)
        if (
            grant is None
            or grant.sub != claims.sub
            or grant.tenant != claims.tenant
            or target not in grant.targets
        ):
            raise unauthorized("invalid agent call token")
        agent = await self.callers.get(grant.agent_id, tenant=grant.tenant)
        if agent is None or agent.acting_id != claims.actor_agent_id:
            raise unauthorized("invalid agent call token")
        if grant.ended_at is not None or grant.deadline <= utcnow():
            raise _refused(410, TURN_GRANT_ENDED, "the calling turn's grant has ended")
        caller = _Caller(
            agent=agent,
            acts_as=ActsAs(sub=grant.sub, username=grant.username, tv=grant.tv),
            grant=grant,
            session_id=grant.session_id,
            turn_id=None,
            run=await self._calling_run(agent, grant.run_id, grant.session_id),
        )
        await self._check_person(caller)
        return caller

    async def _header_caller(self, called_id: str, bearer: str, creds: CallCredentials) -> _Caller:
        """header 모드의 턴이다. 파드의 인스턴스 토큰과 파드가 댄 세션, 턴, 소유자를 믿는다."""
        agent = await agent_of_token(self.callers, self.instance_tokens, bearer)
        if not creds.session_id or not creds.turn_id:
            raise unauthorized("the calling session and turn are required")
        if GrantTarget(kind=AGENT_SOURCE, id=called_id) not in call_targets(agent):
            raise _refused(403, "forbidden", "the calling agent does not use this agent as a tool")
        return _Caller(
            agent=agent,
            acts_as=ActsAs(sub=unquote(creds.owner) if creds.owner else HEADER_SUB),
            grant=None,
            session_id=creds.session_id,
            turn_id=creds.turn_id,
            run=await self._calling_run(agent, None, creds.session_id),
        )

    async def _calling_run(
        self, agent: Agent, run_id: str | None, session_id: str | None
    ) -> AgentRun | None:
        """부르는 세션의 실행이다. 실행의 턴에 연 위임은 그 실행을 대고, 그 밖은 세션으로 찾는다.
        채팅 세션이면 없다."""
        if run_id is not None:
            return await self.runs.get(agent.tenant, agent.id, run_id)
        if session_id is not None:
            return await self.runs.for_session(agent.tenant, agent.id, session_id)
        return None

    async def _check_person(self, caller: _Caller) -> None:
        """사람이 그 Agent 와 직접 대화할 수 있고 뿌리 턴의 한도 안인가. 실행을 그 사람의 이름으로
        돌릴 수 없으면(사람 기록이 없는 oidc 의 사람) 그것도 거절이다."""
        cause = await self.person.lost(
            caller.acts_as,
            caller.agent.tenant,
            scopes=CALL_SCOPES,
            interactive=not caller.long_bound,
        )
        if cause is not None:
            raise _unavailable("the calling person lost access", reason=ended_reason(cause))
        if not await self.person.runs_as_person(caller.acts_as.sub):
            raise _unavailable("the calling person has no record to run as")

    async def _started_run(self, caller: _Caller, called_id: str, run_id: str) -> AgentRun:
        """이 턴이 시작한 호출의 실행이다. 아니면 404 다."""
        run = await self.runs.get(caller.agent.tenant, called_id, run_id)
        if run is None or not caller.started(run):
            raise _not_found()
        return run

    # -- 시작 전의 거절 (6.3·6.5절) ------------------------------------------------------------

    async def _called(self, caller: _Caller, called_id: str) -> tuple[Agent, HubItem]:
        """호출받는 Agent 와 그 게시 항목이다. 같은 테넌트에 있고 게시돼 있어야 하며, 그 항목이
        부르는 리비전이 Tool 로 붙인 항목이어야 한다. 게시를 거뒀다가 다시 게시한 Agent 는 새
        항목이라, 옛 항목을 붙인 리비전은 새 항목의 사용 권한을 보는 저장과 배포를 다시 거쳐야
        부른다. 사슬도 여기서 본다. 지워진 Agent 와 다른 테넌트의 Agent 는 게시하지 않은 Agent 와
        같이 답한다."""
        called = await self.agents.get(called_id, tenant=caller.agent.tenant)
        item = await self.hub.get_by_agent(called.id) if called is not None else None
        if (
            called is None
            or item is None
            or item.kind != AGENT_KIND
            or item.tenant != called.tenant
            or item.id not in _attached_items(caller.agent)
        ):
            raise _refused(409, SOURCE_UNPUBLISHED, "that agent is not published to the hub")
        chain = caller.chain
        if called.id in chain:
            raise _refused(409, CALL_CYCLE, f"agent {called.name!r} is already in this call chain")
        if len(chain) >= MAX_CHAIN:
            raise _refused(
                409, CALL_TOO_DEEP, f"a call chain holds at most {MAX_CHAIN} agents", max=MAX_CHAIN
            )
        return called, item

    async def _admit(
        self, caller: _Caller, called: Agent, item: HubItem, creds: CallCredentials, message: str
    ) -> AgentRun:
        """전제 조건, 속도, 상한, 한 턴의 호출 수를 지난 호출의 실행 기록을 리스를 건 채 넣는다."""
        if called.state == "deploying":
            raise _busy(f"agent {called.name!r} is deploying")
        self._require_callable(called, message)
        self._take_rate(called, caller.acts_as.sub)
        now = utcnow()
        cap = await self.run_service.cap_error(called.tenant, called.id, now, CALL_CAPS)
        if cap is not None:
            raise cap
        if not await self._count_call(caller, now):
            raise _refused(
                429,
                CALL_FANOUT,
                f"a turn starts at most {MAX_CALLS_PER_TURN} agent calls",
                max=MAX_CALLS_PER_TURN,
            )
        size, digest = input_digest(message)
        run = new_run(
            tenant=called.tenant,
            agent_id=called.id,
            source=CALL_SOURCE,
            acts_as=caller.acts_as,
            now=now,
            message=message,
            input_bytes=size,
            input_sha256=digest,
            caller=RunCaller(
                agent_id=caller.agent.acting_id,
                session_id=await self._caller_session(caller),
                turn_id=caller.turn_id,
                grant_id=caller.grant.id if caller.grant is not None else None,
                chain=caller.chain,
                long_bound=caller.long_bound,
                untrusted=(
                    not item.calls_trusted
                    or caller.grant is None
                    or creds.outside
                    or turns_outside_off(caller.agent)
                ),
                timeout_at=now
                + timedelta(seconds=turn_limits(called).turn_timeout_seconds)
                + CALL_GRACE,
            ),
            # 이 경로가 그 자리에서 시작한다. 리스가 끝나도 `starting` 이면 시작 워커가 잡는다
            lease=ItemLease(
                owner=self.starter.owner,
                until=now + timedelta(seconds=self.starter.lease_seconds),
            ),
        )
        await self.runs.insert(run)
        return run

    def _require_callable(self, called: Agent, message: str) -> None:
        """호출받는 Agent 가 이 메시지의 호출을 받는가. 게시와 저장이 보는 전제 조건을 먼저 보아
        memory 노드와 세션 예산을 잃은 리비전이 그때와 같은 409 agent_source_runs_unmet 으로
        답한다. 턴을 받지 않는 Agent(409 not_deployed)와 입력 한도(413)는 실행의 것이다."""
        if called.deployed is not None and serves_turns(called):
            refusal = self.publishing.call_refusal(called)
            if refusal is not None:
                raise _refused(refusal.status, refusal.code, refusal.detail, **refusal.extra)
        self.run_service.ready(called, message)

    async def _caller_session(self, caller: _Caller) -> str | None:
        """실행 기록에 적을 부르는 세션이다. 새 세션의 첫 턴은 런타임이 턴을 받아들인 뒤에야 위임에
        세션이 붙으므로, 그 전에 온 호출은 부르는 실행의 세션을 읽고 없으면 위임을 한 번 다시
        읽는다. 사슬과 호출 수는 위임으로 세므로 세션을 끝내 모르는 호출도 받는다."""
        if caller.session_id is not None or caller.grant is None:
            return caller.session_id
        if caller.run is not None and caller.run.session_id is not None:
            return caller.run.session_id
        await asyncio.sleep(SESSION_ATTACH_WAIT_SECONDS)
        grant = await self.grants.get(caller.grant.id)
        return grant.session_id if grant is not None else None

    def _take_rate(self, called: Agent, sub: str) -> None:
        """그 사람이 이 Agent 에 보내는 호출을 채팅처럼 센다. 한도는 호출받는 리비전의 것이고
        없으면 세지 않는다."""
        a = analyze_structure(called.deployed) if called.deployed is not None else None
        entry = a.typed(a.entry, EntryData) if a is not None and a.entry is not None else None
        if entry is None or entry.rate_limit is None:
            return
        per_minute = entry.rate_limit.per_user_per_minute
        buckets = self._rates.get(per_minute)
        if buckets is None:
            buckets = self._rates[per_minute] = KeyedBuckets(
                per_minute / 60.0,
                float(per_minute),
                idle_seconds=RATE_IDLE_SECONDS,
                max_entries=RATE_ENTRIES,
                clock=self.clock,
            )
        if not buckets.take(f"{called.id}:{sub}"):
            raise AgentCallError(
                429,
                CALL_RATE_LIMITED,
                f"this person calls agent {called.name!r} at most {per_minute} times a minute",
                headers=retry_after_header(RATE_RETRY_AFTER),
            )

    async def _count_call(self, caller: _Caller, now: datetime) -> bool:
        if caller.grant is not None:
            return await self.grants.count_call(caller.grant.id, limit=MAX_CALLS_PER_TURN, now=now)
        key = f"{caller.agent.id}:{caller.session_id}:{caller.turn_id}"
        return await self.grants.count_turn_call(key, limit=MAX_CALLS_PER_TURN, now=now)

    # -- 시작과 답 (6.3절) ---------------------------------------------------------------------

    async def _start_inline(self, run: AgentRun, ends: float) -> None:
        """시작 워커의 한 걸음을 이 요청에서 돌린다. 그 걸음이 기록을 미뤘으면(깨우는 중) 미룬
        때에 다시 하고, 이 요청이 끝나기 전에 다시 할 수 없으면 틱에 둔다."""
        owner = self.starter.owner
        while True:
            await self.starter.process(run)
            current = await self.runs.get(run.tenant, run.agent_id, run.id)
            if current is None or current.status != "starting":
                return
            if current.lease is None or current.lease.owner != owner:
                return
            wait = max(0.0, (current.lease.until - utcnow()).total_seconds())
            if self.clock() + wait >= ends:
                return
            await asyncio.sleep(wait)
            if not await self.runs.renew(run.id, owner, self.starter.lease_seconds, utcnow()):
                return
            run = current

    async def _settled(self, called: Agent | None, run: AgentRun, ends: float) -> dict[str, Any]:
        """실행이 끝나거나 ends 가 될 때까지 기록을 다시 읽고 그 답을 만든다."""
        while True:
            fresh = await self.runs.get(run.tenant, run.agent_id, run.id)
            if fresh is None:
                raise _not_found()
            if fresh.status not in MOVING_STATUSES or self.clock() >= ends:
                return await self._answer(called, fresh)
            await asyncio.sleep(POLL_STEP_SECONDS)

    async def _answer(self, called: Agent | None, run: AgentRun) -> dict[str, Any]:
        out: dict[str, Any] = {"run_id": run.id, "status": run.status}
        if run.status == "succeeded":
            text = await self._answer_text(called, run)
            if text is None:
                # 전사를 아직 읽지 못했다. 끝나지 않은 것처럼 답해 다시 묻게 한다
                return {**out, "status": "running"}
            out["answer"] = text
        elif run.status in ENDED_WITHOUT_ANSWER:
            out["code"] = run.reason or run.status
        return out

    async def _answer_text(self, called: Agent | None, run: AgentRun) -> str | None:
        """실행 세션의 전사에서 읽은 답이다. 읽지 못했으면 None 이다."""
        if called is None or run.session_id is None:
            return None
        try:
            status, body = await self.proxy.session(
                called, run.session_id, user_sub=run.acts_as.sub, approver=True, view="progress"
            )
        except Exception as e:  # noqa: BLE001 - 다음 묻기가 다시 읽는다
            logger.warning("run %s: its answer was not read: %s", run.id, type(e).__name__)
            return None
        return last_answer(transcript_items(body)) if status == 200 else None

    # -- 감사 (6.8절) --------------------------------------------------------------------------

    async def _audited(self, action: str, called_id: str, seen: _Seen, work: Any) -> dict[str, Any]:
        try:
            answer = await work()
        except ServiceError as e:
            await self._audit(action, called_id, seen, e.status, e.code)
            raise
        except HTTPException as e:
            # 401 은 앱의 다른 경로처럼 HTTPException 이다(`errors.unauthorized`)
            detail = e.detail if isinstance(e.detail, dict) else {}
            await self._audit(action, called_id, seen, e.status_code, detail.get("code"))
            raise
        except Exception:
            # 뜻밖의 오류도 요청 하나의 행을 남긴다. 답은 앱의 500 이고 코드는 없다
            await self._audit(action, called_id, seen, 500, None)
            raise
        seen.status = answer.get("status")
        await self._audit(action, called_id, seen, 200, answer.get("code"))
        return answer

    async def _audit(
        self, action: str, called_id: str, seen: _Seen, status: int, code: str | None
    ) -> None:
        """요청 하나의 감사 행이다. 막혀도 답은 같으므로 경고만 남긴다."""
        if self.audit is None:
            return
        caller = seen.caller
        event = AuditEvent(
            sub=caller.acts_as.sub if caller is not None else None,
            subject_type="user" if caller is not None else "system",
            tenant=caller.agent.tenant if caller is not None else None,
            action=action,
            resource=AUDIT_RESOURCE,
            resource_id=seen.run_id,
            path_params={
                "agent_id": called_id,
                "caller_agent_id": caller.agent.acting_id if caller is not None else None,
                "run_status": seen.status,
            },
            method="GET" if action == POLL else "POST",
            path=API_PREFIX + AGENT_CALL_PATH.format(agent_id=called_id),
            request_id=new_id(),
            status=status,
            result="ok" if status < 400 else "error",
            error_code=code,
            via=f"agent/{caller.agent.acting_id}" if caller is not None else None,
        )
        try:
            await self.audit.record(event)
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 답은 같다
            logger.warning("agent call audit row %s not written: %s", action, e)


def _attached_items(agent: Agent) -> list[str]:
    """부르는 Agent 의 배포된 리비전이 Tool 로 붙인 허브 항목의 id 다."""
    return agent_source_items(agent.deployed) if agent.deployed is not None else []


def _unavailable(detail: str, **extra: Any) -> AgentCallError:
    return _refused(403, ACTS_AS_UNAVAILABLE, detail, **extra)


__all__ = [
    "CALLER_ENDED",
    "CALLER_TIMEOUT",
    "MAX_CALLS_PER_TURN",
    "MAX_CHAIN",
    "REQUEST_WAIT_SECONDS",
    "AgentCallError",
    "AgentCallRuns",
    "CallCredentials",
    "CancelReason",
]
