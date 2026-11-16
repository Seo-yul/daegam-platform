"""시작 워커 (P41 5·6절).

`starting` 기록 하나에 리스를 걸고, 소스·전제 조건·묶인 사람·상한을 다시 본 뒤 그 사람으로 런타임의
`POST /chat` 을 세션 없이 부른다. 헤더만 읽고 답을 닫는다. 턴은 떼어 낸 채로 이어 돈다(P78). 이 판이
아는 출처의 기록만 잡고, 뒤 판이 더한 출처(P46 의 `agent`)의 기록은 그 판의 워커에 둔다
(`MongoAgentRunStore.acquire_start`, P46 A0).

- **다시 보기.** 스케줄이 지워졌거나 멈췄거나 만료됐거나 묶인 리비전이 지금 배포된 것이 아니면,
  Agent 가 없거나 내려가면, 상한에 닿았으면 기록을 그 까닭으로 `cancelled` 로 끝낸다. 전제 조건이
  맞지 않으면 소스를 `prerequisites_failed` 로, 묶인 사람이 접근을 잃었으면 그 사람의 이 Agent
  소스를 `acts_as_lost_access` 로 함께 멈춘다. 그 사람의 마지막 IdP 확인이 긴 한도를 넘었으면 기록의
  까닭은 `acts_as_unconfirmed` 다. 배포 중인 Agent 는 시도로 세지 않고 미룬다.
- **누구로.** local 모드와 oidc 모드에서 위임을 열 수 있으면 `trigger: run` 위임을 묶인 사람으로
  열어 싣는다. local 은 묶을 때의 `tv` 로 열고, oidc 는 사람 기록을 긴 한도로 본다(P53b 6.3절).
  위임을 싣고 답을 보는 일은 채팅 라우트와 같은 `TurnDelegation.send` 가 하고 세션의 소유자 기록도
  적는다. 그 밖(header 모드, 기록이 한 번도 없었던 oidc 의 사람, 위임이 없거나 파드가 받지 않았을
  때)은 Agent 의 이름으로 돌고 기록의 `acts_as` 가 까닭을 적는다. 어느 쪽이든 묶인 사람이 기억한
  거부를 싣는다(P43 5절).
- **답 보기.** 성공한 답은 `X-Daegam-Detached-Turn: 1` 과 이 실행의 `X-Daegam-Run-Id` 를 실어야
  한다. 없으면 실행을 모르는 옛 파드라 본문을 끝까지 읽고 `runs_unsupported` 로 끝낸다. 실어 왔으면
  세션과 턴 id 를 적고 `running` 으로 옮기며 남은 입력 본문을 지운다. 그사이 중지 표시가 남았거나
  기록이 끝났으면 그 턴을 멈춘다. 기록을 적고 옮기는 것은 리스를 쥔 워커뿐이다.
- **다시 하기.** 502·503·504·연결 실패·`tools_unavailable`·`turn_tokens_unavailable` 은 10분 안에
  세 번까지 물러나며 다시 한다. `run_exists` 는 앞 시도가 턴을 연 것이라 그 세션과 턴으로 잇고,
  턴 id 가 없으면 `run_lost` 다. 그 밖의 4xx 는 그 코드로 `failed` 이고 다시 하지 않는다.
- **잠든 Agent** (P44 8절). 위임을 열기 전에 쉬기의 게이트를 지난다. 실행 시작은 Agent 를 깨우고
  활동을 적는다. 깨우는 중(`agent_waking`, 게이트의 답이든 프록시가 드레인 거절을 바꾼 답이든)이면
  시도로 세지 않고 `Retry-After` 만큼 미룬다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from daegam.errors import ServiceError
from daegam.services.agent_runtime import (
    AGENT_WAKING,
    WAKE_RETRY_SECONDS,
    AgentRuntimeProxy,
    ProxyResponse,
    read_code,
)
from daegam.services.agent_sleep import RUN
from daegam.services.approval_choices import ApprovalChoiceService
from daegam.services.lease_worker import LeaseWorker
from daegam.services.runs.fire import GONE_STATES
from daegam.services.runs.person import ended_reason
from daegam.services.runs.service import (
    AGENT_CHANGED,
    AGENT_UNDEPLOYED,
    PAUSED_BY_HAND,
    PREREQUISITES_FAILED,
    SOURCE_DELETED,
    RunError,
    RunService,
    SourceKind,
)
from daegam.services.sandboxes import code_execution_on
from daegam.services.turn_grants import GrantPerson, OpenedGrant, TurnTokensUnavailable
from daegam.services.turn_person import (
    DETACHED_HEADER,
    SESSION_HEADER,
    TURN_GRANT_HEADER,
    TURN_ID_HEADER,
    TurnDelegation,
    TurnPlan,
)
from daegam.store.agent_runs import ActsAs, AgentRun, RunStatus
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.run_sources import RunSource

logger = logging.getLogger("daegam.runs.start")

# 한 레플리카가 함께 시작하는 실행 수 (5절). Agent 하나는 한 번에 하나다
MAX_PARALLEL = 5
# 다시 할 수 있는 시작의 시도 수와 그 창, 첫 물러남 (5절의 표)
MAX_ATTEMPTS = 3
RETRY_WINDOW = timedelta(minutes=10)
RETRY_BASE = timedelta(seconds=30)
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_CODES = frozenset({"tools_unavailable", "turn_tokens_unavailable"})
# 깨우는 중인 Agent 의 시작을 미루는 시간 (P44 8절). 런타임의 Retry-After 와 같다
WAKE_DEFER = timedelta(seconds=WAKE_RETRY_SECONDS)
# 런타임이 실행의 답에 싣는 헤더. 프록시가 넘긴 헤더는 소문자다
RUN_ID_HEADER = "x-daegam-run-id"
# 기록의 까닭
RUN_EXISTS = "run_exists"
RUN_LOST = "run_lost"
RUNS_UNSUPPORTED = "runs_unsupported"
AGENT_RUN_LIMIT = "agent_run_limit"
# 샌드박스 실행 몫을 쥔 실행의 상태 (P40 7절). 시작하는 이 실행은 빼고 센다
SANDBOX_RUN_STATUSES: tuple[RunStatus, ...] = ("running", "waiting_approval")
EXPIRED = "expired"
# Agent 의 이름으로 도는 까닭 (6절). run_exists 는 앞 시도의 이름을 알 수 없을 때다
HEADER_MODE = "header_mode"
OIDC_MODE = "oidc_mode"
NO_TURN_TOKEN = "no_turn_token"
GRANT_NOT_ACCEPTED = "grant_not_accepted"
# 런타임을 부르지 못한 시작의 코드
RUNTIME_UNREACHABLE = "runtime_unreachable"


@dataclass(frozen=True)
class _Ended:
    """시작하기 전에 `cancelled` 로 끝낼 까닭. pause 가 있으면 소스를 그 까닭으로, lost 가 있으면
    묶인 사람의 이 Agent 소스를 그 원인으로 멈춘다."""

    reason: str
    pause: str | None = None
    lost: str | None = None


class SleepGate(Protocol):
    """턴 앞의 쉬기의 게이트 (P44 8절, `services/agent_sleep.py` `AgentSleepControl.admit`)."""

    async def admit(self, doc: Agent, reason: str) -> None: ...


def _as_agent(acts_as: ActsAs, cause: str) -> ActsAs:
    return acts_as.model_copy(update={"kind": "agent", "cause": cause})


class RunStartWorker(LeaseWorker[AgentRun]):
    task_prefix = "run-start"
    tick_label = "run start tick"
    item_label = "run"
    log = logger

    def __init__(
        self,
        agents: MongoAgentStore,
        service: RunService,
        proxy: AgentRuntimeProxy,
        delegation: TurnDelegation,
        *,
        choices: ApprovalChoiceService | None = None,
        owner: str | None = None,
        interval: float,
        lease_seconds: float,
        sandbox_runs: int | None = None,
        sleep: SleepGate | None = None,
    ):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease_seconds)
        # 잠든 Agent 를 깨우는 게이트 (P44 8절). 없으면 보지 않는다
        self.sleep = sleep
        # 코드 실행 Agent 의 실행 몫 (P40 7절, DAEGAM_SANDBOX_RUNS_PER_AGENT). None 이면 보지 않는다
        self.sandbox_runs = sandbox_runs
        self.agents = agents
        self.service = service
        self.runs = service.runs
        self.proxy = proxy
        self.delegation = delegation
        self.choices = choices

    # -- 리스와 한 바퀴 --------------------------------------------------------------------

    async def acquire(self, busy: frozenset[str] = frozenset()) -> AgentRun | None:
        return await self.runs.acquire_start(self.owner, self.lease_seconds, utcnow(), busy=busy)

    async def renew(self, item_id: str) -> bool:
        return await self.runs.renew(item_id, self.owner, self.lease_seconds, utcnow())

    async def tick(self) -> int:
        """`MAX_PARALLEL` 자리에서 함께 시작하고, 자리가 비거나 틱 간격이 지날 때마다 다음 기록을
        잡는다. 도는 Agent 의 기록은 잡지 않는다. 옛 파드의 답을 끝까지 읽는 시작은 제 자리만 쥔다.
        잡기가 실패하면 틱 간격이 지날 때까지 더 잡지 않고 도는 시작을 이어 가며, 틱이 멈추면 도는
        시작을 취소하고 끝날 때까지 기다린다. 시작한 수를 돌려준다."""
        n = 0
        slots: dict[asyncio.Task[None], str] = {}
        acquiring = True
        try:
            while True:
                while acquiring and len(slots) < MAX_PARALLEL:
                    try:
                        run = await self.acquire(frozenset(slots.values()))
                    except Exception:  # noqa: BLE001 - 도는 시작은 끝까지 마친다
                        self.log.exception("run start: acquiring failed, finishing the starts")
                        acquiring = False
                        break
                    if run is None:
                        break
                    slots[asyncio.create_task(self.process(run))] = run.agent_id
                    n += 1
                if not slots:
                    return n
                done, _ = await asyncio.wait(
                    slots, timeout=self.interval, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    acquiring = True
                for task in done:
                    slots.pop(task)
                    self._log_failure(task)
        finally:
            for task in slots:
                task.cancel()
            await asyncio.gather(*slots, return_exceptions=True)

    def _log_failure(self, task: asyncio.Task[None]) -> None:
        """끝난 시작의 예외를 꺼내 남긴다. `process` 가 삼키지 못한 것뿐이다."""
        if not task.cancelled() and (error := task.exception()) is not None:
            self.log.error("run start: a start ended with %r", error)

    async def process(self, item: AgentRun) -> None:
        try:
            async with self.heartbeating(item.id):
                await self.start_run(item, utcnow())
        except Exception:  # noqa: BLE001 - 기록 하나의 실패가 다른 시작을 막지 않는다
            self.log.exception("run %s: start failed", item.id)
            await self.runs.defer(item.id, self.owner, utcnow() + self.wait())

    def wait(self) -> timedelta:
        """미룬 기록을 다시 보기까지 쉬는 바닥. 틱 간격이고 적어도 30초다."""
        return timedelta(seconds=max(self.interval, 30.0))

    # -- 시작 ----------------------------------------------------------------------------

    async def start_run(self, run: AgentRun, now: datetime) -> None:
        agent = await self.agents.get(run.agent_id, tenant=run.tenant)
        if agent is not None and agent.state == "deploying":
            # 지나가는 상태다. 시도로 세지 않고 미룬다
            await self.runs.defer(run.id, self.owner, now + self.wait())
            return
        kind = self.service.kind_of(run)
        source = await self._source(run, kind)
        # 스케줄 실행은 스케줄의 메시지를, 지금 실행과 웹훅 실행은 기록에 둔 메시지를 보낸다
        message = (source.run_message() if source is not None else None) or run.message
        ended = await self._check(run, agent, kind, source, message, now)
        if ended is not None:
            await self._end_before_start(run, kind, ended, now)
            return
        assert agent is not None
        if await self._sandbox_runs_full(run, agent):
            # 몫이 비면 다시 본다. 시도로 세지 않는다
            await self.runs.defer(run.id, self.owner, now + self.wait())
            return
        if not await self._awake(run, agent, now):
            return
        await self._call(run, agent, message or "", now)

    async def _awake(self, run: AgentRun, agent: Agent, now: datetime) -> bool:
        """위임을 열기 전의 쉬기의 게이트 (P44 8절). 잠든 Agent 를 깨우고 활동을 적는다. 깨우는
        중이면 시도로 세지 않고 미루며 거짓이다."""
        if self.sleep is None:
            return True
        try:
            await self.sleep.admit(agent, RUN)
        except ServiceError as e:
            if e.code != AGENT_WAKING:
                raise
            await self.runs.defer(run.id, self.owner, now + WAKE_DEFER)
            return False
        return True

    async def _sandbox_runs_full(self, run: AgentRun, agent: Agent) -> bool:
        """코드 실행을 켠 Agent 의 실행 몫이 찼는가 (P40 7절). 열린 실행 하나가 시작부터 끝까지 몫
        하나를 쥔다. 그 실행의 샌드박스 열기는 사람의 몫이 아니라 이 몫을 쓰고, 열기의 조건부
        쓰기가 몫을 다시 지킨다."""
        if self.sandbox_runs is None or not code_execution_on(agent):
            return False
        held = await self.runs.count_in_statuses(run.tenant, run.agent_id, SANDBOX_RUN_STATUSES)
        return held >= self.sandbox_runs

    @staticmethod
    async def _source(run: AgentRun, kind: SourceKind | None) -> RunSource | None:
        if kind is None or run.source_id is None:
            return None
        return await kind.store.get(run.tenant, run.agent_id, run.source_id)

    async def _check(
        self,
        run: AgentRun,
        agent: Agent | None,
        kind: SourceKind | None,
        source: RunSource | None,
        message: str | None,
        now: datetime,
    ) -> _Ended | None:
        """시작 전에 다시 보는 것. 지나면 None 이다."""
        if agent is None or agent.state in GONE_STATES:
            return _Ended(AGENT_UNDEPLOYED)
        if kind is not None:
            if source is None:
                return _Ended(SOURCE_DELETED)
            if source.paused:
                return _Ended(source.paused_reason or PAUSED_BY_HAND)
            if source.expires_at <= now:
                return _Ended(EXPIRED)
            deployed = agent.deployed
            if deployed is None or deployed.revision != source.revision:
                return _Ended(AGENT_CHANGED)
        try:
            self.service.ready(agent, message, kind)
        except RunError as e:
            if e.code == "not_deployed":
                return _Ended(AGENT_UNDEPLOYED)
            return _Ended(e.code, pause=PREREQUISITES_FAILED)
        cause = await self.service.person.lost(run.acts_as, run.tenant)
        if cause is not None:
            return _Ended(ended_reason(cause), lost=cause)
        if await self.service.start_cap_error(run.tenant, run.agent_id) is not None:
            return _Ended(AGENT_RUN_LIMIT)
        return None

    async def _end_before_start(
        self, run: AgentRun, kind: SourceKind | None, ended: _Ended, now: datetime
    ) -> None:
        """기록을 먼저 그 까닭으로 끝내고 소스를 멈춘다. 멈춤이 소스의 열린 실행을 자기 까닭으로
        끝내기 전이다."""
        await self._end(run, "cancelled", ended.reason, now)
        if ended.lost is not None:
            await self.service.pause_person(
                run.acts_as.sub, ended.lost, tenant=run.tenant, agent_id=run.agent_id
            )
        if ended.pause is None or kind is None:
            return
        source = await self._source(run, kind)
        if source is not None and not source.paused:
            await self.service.pause_source(kind.store, source, ended.pause)

    # -- 런타임 부르기 ---------------------------------------------------------------------

    async def _call(self, run: AgentRun, agent: Agent, message: str, now: datetime) -> None:
        try:
            acts_as, person, grant = await self._identity(run, agent)
        except TurnTokensUnavailable as e:
            await self._retry_or_fail(run, e.code, now)
            return
        plan = TurnPlan(agent, None, now, grant, confirms=person)

        async def call(turn_grant: dict[str, Any] | None) -> ProxyResponse:
            # 묶인 사람이 기억한 거부다(P43 5절). 읽지 못하면 연결 실패처럼 다시 한다
            rejects = (
                await self.choices.for_run_start(agent, run.acts_as.sub)
                if self.choices is not None
                else None
            )
            return await self.proxy.chat(
                agent,
                user_sub=run.acts_as.sub,
                message=message,
                session_id=None,
                ephemeral=True,
                turn_grant=turn_grant,
                run_id=run.id,
                # 웹훅 본문은 밖에서 온 글이라 런타임 가드의 엄격한 URL 규칙을 받는다 (4절)
                untrusted=self.service.kind_of(run) is self.service.trigger_kind,
                approval_choices=rejects,
            )

        try:
            proxied = await self.delegation.send(plan, call)
        except ServiceError as e:
            await self._answered_error(run, e.status, e.code, None, now)
            return
        except Exception as e:  # noqa: BLE001 - 연결 실패는 다시 할 수 있는 시작이다
            self.log.warning("run %s: runtime call failed: %s", run.id, type(e).__name__)
            await self._retry_or_fail(run, RUNTIME_UNREACHABLE, now)
            return
        if proxied.status >= 400:
            code, body = await read_code(proxied)
            await self._answered_error(run, proxied.status, code, body, now)
            return
        await self._accepted(run, proxied, acts_as, grant, now)

    async def _identity(
        self, run: AgentRun, agent: Agent
    ) -> tuple[ActsAs, GrantPerson | None, OpenedGrant | None]:
        """실행이 누구의 이름으로 도는가 (6절). (실행 이름, 소유자 기록을 적을 사람, 위임). 사람은
        토큰 인증 모드에서이고, oidc 모드는 사람 기록이 있었던 사람만이다. 위임은 열 수 있을 때만
        이다."""
        settings = self.service.settings
        if not settings.token_auth:
            return _as_agent(run.acts_as, HEADER_MODE), None, None
        if not await self.service.person.runs_as_person(run.acts_as.sub):
            return _as_agent(run.acts_as, OIDC_MODE), None, None
        person = GrantPerson(
            sub=run.acts_as.sub, username=run.acts_as.username or "", tv=run.acts_as.tv
        )
        outcome = await self.delegation.grants.open(
            agent=agent, person=person, trigger="run", session_id=None
        )
        if outcome.grant is None:
            return _as_agent(run.acts_as, outcome.refused or NO_TURN_TOKEN), person, None
        person_acts = run.acts_as.model_copy(update={"kind": "person", "cause": None})
        return person_acts, person, outcome.grant

    async def _answered_error(
        self, run: AgentRun, status: int, code: str | None, body: Any, now: datetime
    ) -> None:
        if status == 409 and code == RUN_EXISTS and isinstance(body, dict):
            await self._earlier_turn(run, body, now)
            return
        if code == AGENT_WAKING:
            # 프록시가 드레인 거절을 바꾼 답이다. 다음 시도의 게이트가 깨운다 (P44 8절)
            await self.runs.defer(run.id, self.owner, now + WAKE_DEFER)
            return
        if status in RETRY_STATUSES or code in RETRY_CODES:
            await self._retry_or_fail(run, code or f"http_{status}", now)
            return
        await self._fail(run, code or f"http_{status}", now)

    async def _earlier_turn(self, run: AgentRun, body: dict[str, Any], now: datetime) -> None:
        """앞 시도가 턴을 열었다 (5절). 그 턴으로 잇는다. 앞 시도가 적어 둔 위임과 실행 이름이
        있으면 그대로 두고, 없으면 누구의 이름으로 돌았는지 모르므로 Agent 의 이름과 까닭을
        적는다."""
        session_id, turn_id = body.get("session_id"), body.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            await self._fail(run, RUN_LOST, now)
            return
        noted = run.started_at is not None
        acts_as = run.acts_as if noted else _as_agent(run.acts_as, RUN_EXISTS)
        grant_id = run.grant_id if noted else None
        await self._running(run, run.session_id or session_id, turn_id, grant_id, acts_as, now)

    async def _accepted(
        self,
        run: AgentRun,
        proxied: ProxyResponse,
        acts_as: ActsAs,
        grant: OpenedGrant | None,
        now: datetime,
    ) -> None:
        headers = proxied.headers
        session_id = headers.get(SESSION_HEADER)
        turn_id = headers.get(TURN_ID_HEADER)
        detached = headers.get(DETACHED_HEADER) == "1"
        if not detached or headers.get(RUN_ID_HEADER) != run.id or not session_id or not turn_id:
            # 실행을 모르는 옛 파드가 받았다. 턴이 연결에 묶여 있을 수 있어 끝까지 읽는다. 이
            # 자리만 붙잡고 다른 시작은 남은 자리에서 돈다
            async for _ in proxied.body:
                pass
            await self._fail(run, RUNS_UNSUPPORTED, now)
            return
        await proxied.body.aclose()
        if grant is not None and headers.get(TURN_GRANT_HEADER) != grant.id:
            # 위임을 모르는 파드가 받았다. send 가 위임을 끝냈다
            grant, acts_as = None, _as_agent(acts_as, GRANT_NOT_ACCEPTED)
        grant_id = grant.id if grant is not None else None
        await self._running(run, session_id, turn_id, grant_id, acts_as, now)

    async def _running(
        self,
        run: AgentRun,
        session_id: str,
        turn_id: str,
        grant_id: str | None,
        acts_as: ActsAs,
        now: datetime,
    ) -> None:
        """런타임이 턴을 받았다. 세션·턴·위임과 실행 이름을 적고 입력 본문을 지운 뒤 기록을
        `running` 으로 옮긴다. 이벤트가 먼저 옮겼으면 상태는 그대로 둔다. 그사이 기록이 끝났거나
        중지 표시가 남았으면 그 턴을 멈춘다 (7절). 리스를 잃었으면 쥔 워커가 `run_exists` 로
        잇는다."""
        noted = await self.runs.note_start(
            run.id,
            self.owner,
            session_id=session_id,
            turn_id=turn_id,
            grant_id=grant_id,
            acts_as=acts_as,
            now=now,
        )
        if not noted:
            self.log.warning("run %s: lease lost before its start was recorded", run.id)
            return
        await self.runs.move(
            run.id,
            from_status=("starting",),
            to_status="running",
            now=now,
            extra={"lease": None},
            owner=self.owner,
        )
        await self.service.stop_if_marked(run)

    # -- 다시 하기와 실패 --------------------------------------------------------------------

    async def _retry_or_fail(self, run: AgentRun, code: str, now: datetime) -> None:
        attempts = run.attempts + 1
        at = now + RETRY_BASE * (2 ** (attempts - 1))
        if attempts >= MAX_ATTEMPTS or at - run.created_at > RETRY_WINDOW:
            await self._end(run, "failed", code, now, attempts=attempts)
            return
        await self.runs.retry_start(run.id, self.owner, attempts=attempts, at=at, now=now)

    async def _fail(self, run: AgentRun, code: str, now: datetime) -> None:
        await self._end(run, "failed", code, now, attempts=run.attempts + 1)

    async def _end(
        self,
        run: AgentRun,
        status: RunStatus,
        reason: str,
        now: datetime,
        *,
        attempts: int | None = None,
    ) -> None:
        """리스를 쥔 채 `starting` 에서 끝낸다."""
        extra: dict[str, Any] = {"lease": None}
        if attempts is not None:
            extra["attempts"] = attempts
        moved = await self.runs.move(
            run.id,
            from_status=("starting",),
            to_status=status,
            now=now,
            reason=reason,
            extra=extra,
            owner=self.owner,
        )
        if moved:
            self.log.info("run %s: %s before its turn ran (%s)", run.id, status, reason)


__all__ = [
    "GRANT_NOT_ACCEPTED",
    "MAX_ATTEMPTS",
    "MAX_PARALLEL",
    "RETRY_WINDOW",
    "RUNS_UNSUPPORTED",
    "RUN_LOST",
    "RunStartWorker",
]
