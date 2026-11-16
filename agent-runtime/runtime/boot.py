"""런타임 자원을 열어 RuntimeState 를 만들고 배경 작업을 띄운다. main.py 의 lifespan 이 부른다.

자원은 세션 저장소, knowledge, 장기 기억, 도구 소스, OIDC 검증기 순으로 연다. 클러스터가 통째로
다시 뜨면 Agent 가 Database·Redis 와 도구 서버보다 먼저 뜬다. 그래서 둘 다 기동을 막지 않는다.

- 첫 시도에서 저장소에 아직 연결할 수 없거나(T22) 저장소가 접속을 거부하면(T23) HTTP 를 먼저
  열고 배경에서 다시 연다(`dsn.is_waitable`). 그동안 앱에 `starting` 이유를 두어 준비 검사와
  healthz 밖의 요청이 503 이고, 살아 있음 검사는 200 이라 다시 뜨기를 되풀이하지 않는다. 첫 시도의
  그 밖의 오류(env 가 비었다, 스키마가 없다)는 설정이 틀린 것이라 기동이 그대로 실패한다. 그 이유는
  termination log 에 남긴다. 파드가 CrashLoopBackOff 에 빠지면 플랫폼이 이 글을 상태 메시지로 보여
  준다(T23). 다시 열기 도중에 드러난 설정 오류는 파드를 죽이지 않고 그 이유를 보이며 계속 다시
  연다. 준비되지 않은 이유를 화면(T21)에서 보는 편이 재시작 되풀이보다 알아보기 쉽기 때문이다.
- 도구 소스를 못 읽으면(T18) 도구 없이 열고 `_refresh_loop` 가 다시 읽는다
  (`RuntimeState.refresh_tools`).
  저장소를 먼저 여는 것은 도구 서버까지 내려가 있을 때 다시 시도마다 도구 읽기(소스당 최대
  15초)를 기다린 뒤에야 저장소에서 실패하지 않게 하려는 것이다.

두 다시 시도 모두 `BACKGROUND_RETRY_INITIAL_SECONDS` 부터 두 배씩 늘려
`BACKGROUND_RETRY_MAX_SECONDS` 에서 멈추고 간격을 흩는다(`next_retry`). 파드들이 같은 시각에 떠도
같은 박자로 두드리지 않게 하려는 것이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import partial

import httpx
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel

from runtime import events as ev
from runtime.auth import AGENT_TARGET, Authenticator
from runtime.background import stop_tasks
from runtime.compile import compile_agent
from runtime.config import AgentConfig, ConfigError
from runtime.dsn import is_waitable
from runtime.events import EventLog
from runtime.knowledge import KnowledgeTools
from runtime.lease_keeper import LeaseKeeper, renewer_for
from runtime.long_term import LongTermMemory, open_long_term
from runtime.mcp import ToolSources
from runtime.sandbox import open_sandboxes
from runtime.sessions import SessionStore, memory_kind, open_session_store, sweep_loop
from runtime.skills import load_skills
from runtime.state import BACKGROUND_RETRY_MAX_SECONDS, RuntimeState, Settings
from runtime.turn_gate import TOOLS_UNAVAILABLE
from runtime.turn_grants import TurnGrants

log = logging.getLogger("daegam.agent-runtime.boot")

# 다시 열다 ConfigError 가 아닌 예외(코드 결함)가 났을 때 healthz 에 싣는 이유의 코드다
STARTUP_FAILED = "startup_failed"
# 기동이 설정 오류로 실패할 때 이유를 남기는 곳이다. kubelet 이 컨테이너의 마지막 종료 message 로
# 옮기고, 플랫폼이 CrashLoopBackOff 의 이유로 상태 메시지에 보여 준다(T23). init 의 받기 실패
# (플랫폼 `k8s/templates/agent.py` 의 `stop`)와 같은 자리다
TERMINATION_LOG = "/dev/termination-log"


@dataclass(frozen=True)
class Injected:
    """테스트가 끼운 자원이다. 주면 여는 것을 건너뛰고 닫지도 않는다(준 쪽이 닫는다)."""

    model: BaseChatModel | None = None
    sources: ToolSources | None = None
    store: SessionStore | None = None
    knowledge: KnowledgeTools | None = None
    long_term: LongTermMemory | None = None
    oidc_http_client: httpx.AsyncClient | None = None


def next_retry(retry: float) -> tuple[float, float]:
    """(이번에 쉴 시간, 다음 간격). 쉴 시간은 [retry/2, retry] 에서 고르고 간격은 두 배로 늘려
    `BACKGROUND_RETRY_MAX_SECONDS` 에서 멈춘다."""
    return random.uniform(retry / 2, retry), min(retry * 2, BACKGROUND_RETRY_MAX_SECONDS)


@dataclass
class Boot:
    """앱 하나의 기동이다. lifespan 이 만들어 `start` 를 부른다. 저장소에 아직 연결하지 못했으면
    배경 태스크(`loop`)가 이어서 연다. 연 자원은 모두 앱의 스택(`stack`)에 오른다."""

    app: FastAPI
    cfg: AgentConfig
    settings: Settings
    evlog: EventLog
    injected: Injected
    stack: AsyncExitStack

    async def start(self) -> None:
        """첫 시도를 한다. 되면 상태를 건다. 저장소에 아직 연결할 수 없거나 접속이 거부되면(T22,
        T23) `starting` 이유를 두고 배경에서 다시 연다. 그 밖의 오류는 설정이 틀린 것이라 이유를
        termination log 에 남기고 그대로 올려 기동이 실패한다."""
        try:
            state = await self.attempt()
        except ConfigError as e:
            if not is_waitable(e):
                write_termination_message(str(e))
                raise
            log.warning("런타임 자원을 아직 열지 못했다. 배경에서 다시 연다: %s", e)
            self.app.state.starting = e
            self.stack.push_async_callback(stop_tasks, [asyncio.create_task(self.loop())])
            return
        self.activate(state)

    async def attempt(self) -> RuntimeState:
        """한 번 열어 본다. 이번에 연 것은 자기 스택에 두었다가 실패하면 닫고, 되면 앱의 스택에
        넘긴다. 그래서 다시 시도가 앞 시도의 반쯤 열린 자원을 끌고 가지 않는다."""
        opened = AsyncExitStack()
        try:
            state = await self._open(opened)
        except BaseException:
            await opened.aclose()
            raise
        self.stack.push_async_callback(opened.aclose)
        return state

    def activate(self, state: RuntimeState) -> None:
        """연 상태를 앱에 걸고 기동 이벤트를 내고 배경 작업을 띄운다. 작업은 자원보다 먼저 멈춘다
        (스택은 나중에 올린 것을 먼저 닫는다)."""
        self.app.state.runtime = state
        self.app.state.starting = None
        agent = state.agent
        state.events.emit(
            ev.RUNTIME_START,
            warnings=[
                *state.store.warnings,
                *state.config.warnings,
                *agent.warnings,
                *_tools_warnings(state.sources),
            ],
            tools=[tool.name for tool in agent.tools],
            subagents=agent.subagent_names,
            harness_key=agent.harness_key,
            shared_state=state.store.shared,
            memory_kind=memory_kind(state.config),
            long_term=state.long_term is not None,
        )
        _start_background_tasks(state)
        self.stack.push_async_callback(stop_tasks, state.tasks)

    async def loop(self) -> None:
        """저장소에 연결될 때까지 다시 연다(T22). 첫 시도는 `start` 가 이미 했다. 실패할 때마다
        이유를 `app.state.starting` 에 바꿔 둔다."""
        retry = self.settings.retry_initial
        while True:
            pause, retry = next_retry(retry)
            await asyncio.sleep(pause)
            try:
                state = await self.attempt()
            except ConfigError as e:
                self.app.state.starting = e
                log.warning("런타임 자원을 아직 열지 못했다: %s", e)
                continue
            except Exception as e:  # noqa: BLE001 - 코드 결함이어도 다시 연다. 원문은 로그에만
                waiting = ConfigError(STARTUP_FAILED, type(e).__name__)
                # traceback 은 이유가 바뀔 때만 싣는다. 다시 시도마다 같은 것이 쌓이지 않게 한다
                changed = str(waiting) != str(self.app.state.starting)
                self.app.state.starting = waiting
                log.warning("런타임 자원을 열다 예외가 났다: %s", waiting, exc_info=changed)
                continue
            try:
                self.activate(state)
            except Exception:
                # 이 태스크의 예외는 종료 때 stop_tasks 가 삼키므로 여기서 남긴다
                log.exception("런타임 자원을 연 뒤 기동을 마치지 못했다")
                raise
            log.info("런타임 자원을 열었다")
            return

    async def _open(self, stack: AsyncExitStack) -> RuntimeState:
        """자원을 열어 상태를 만든다. 연 자원은 stack 에 올린다. 실패하면 호출자가 stack 을
        닫는다."""
        cfg, settings, injected = self.cfg, self.settings, self.injected
        # 주입된 것은 그대로 쓰고 닫지 않는다. 여기서 연 것만 stack 에 닫기를 올린다
        store = injected.store
        if store is None:
            store = await open_session_store(cfg)
            stack.push_async_callback(store.close)
        # 턴 리스의 관리자는 저장소를 연 뒤에 뜨고 저장소를 닫기 바로 전에 멈춘다. 스택은 종료
        # 기다림(drain.py)의 마감 뒤에 닫히므로 마감하는 턴의 리스도 끝까지 늘린다
        leases = LeaseKeeper(renewer_for(store))
        leases.start()
        stack.push_async_callback(leases.stop)
        knowledge = injected.knowledge
        if knowledge is None:
            knowledge = await KnowledgeTools.open(cfg)
            stack.push_async_callback(knowledge.close)
        memory = injected.long_term
        if memory is None:
            memory = await open_long_term(cfg, tenant=settings.tenant)
            if memory is not None:
                stack.push_async_callback(memory.close)
        sources = injected.sources
        if sources is None:
            grants = TurnGrants(instance_token=settings.instance_token)
            stack.push_async_callback(grants.aclose)
            sources = ToolSources(cfg, grants=grants)
            stack.push_async_callback(sources.close)
        # 실패해도 기동은 이어 간다. 못 읽었으면 _refresh_loop 가 배경에서 다시 읽는다(T18)
        await sources.refresh()
        sandboxes = open_sandboxes(
            cfg,
            settings.sandbox_url,
            settings.instance_token,
            skills_root=load_skills(cfg).root,
        )
        if sandboxes is not None:
            stack.push_async_callback(sandboxes.aclose)
            # 세션이 닫히거나 지워지거나 실행이 끝나면 그 샌드박스를 닫는다(P40 5절)
            store.on_close = sandboxes.close
        agent = compile_agent(
            cfg,
            tools_by_ref=sources.tools_by_ref,
            knowledge=knowledge,
            checkpointer=store.checkpointer,
            model=injected.model,
            long_term=memory,
            sandboxes=sandboxes,
        )
        # 관측 이벤트의 본문이 에이전트의 pii_mask 규칙을 모두 지난다(P37). 규칙은 문서에서 오므로
        # 도구 목록으로 다시 컴파일해도 같다
        self.evlog.masks = agent.body_masks
        # 외부 토큰의 발급자마다 검증기와 계약(D27, P53b 6.8절)
        issuers = [
            spec.trusted(
                str(settings.oidc_audience),
                # 발급자가 플랫폼이면 위임 토큰은 이 Agent 에 묶인 것만 받는다 (P53 3.3절)
                delegation_target=AGENT_TARGET.format(cfg.instance["id"]),
                http_client=injected.oidc_http_client,
            )
            for spec in settings.issuers
        ]
        for trusted in issuers:
            stack.push_async_callback(trusted.verifier.aclose)
        return RuntimeState(
            config=cfg,
            settings=settings,
            events=self.evlog,
            store=store,
            sources=sources,
            agent=agent,
            knowledge=knowledge,
            long_term=memory,
            leases=leases,
            auth=Authenticator(instance_token=settings.instance_token, issuers=issuers),
            grants=sources.grants,
            model_override=injected.model,
            tools_ready=sources.ready,
            sandboxes=sandboxes,
        )


def _start_background_tasks(state: RuntimeState) -> None:
    """도구 읽기 루프(tools[] 가 있을 때), 세션 sweep 루프, 장기 기억의 한 번뿐인 만료 정리(장기
    기억이 있을 때, P44 11절), 이벤트 보내기(수집 주소가 있을 때)를 띄운다. state.tasks 가 든다.
    모두 기동이 끝난 뒤라 플랫폼이나 저장소가 느려도 준비 검사가 기다리지 않는다."""
    if state.events.shipper is not None:
        state.tasks.append(asyncio.create_task(state.events.shipper.run()))
    if state.sources.refs:
        state.tasks.append(asyncio.create_task(_refresh_loop(state)))
    state.tasks.append(
        asyncio.create_task(
            sweep_loop(
                state.store,
                state.config.entry["session_ttl_seconds"],
                state.settings.sweep_interval,
                lambda sid: state.events.emit(ev.SESSION_EXPIRED, session_id=sid),
                retention_days=state.settings.retention_days,
                repair=partial(state.repairs.sweep, state),
            )
        )
    )
    if state.long_term is not None:
        state.tasks.append(asyncio.create_task(state.long_term.sweep_expired()))


def write_termination_message(message: str) -> None:
    """기동 실패의 이유를 termination log 에 쓴다. 파드 밖(로컬 실행)에는 그 파일이 없으므로 쓰지
    못해도 넘어간다."""
    with contextlib.suppress(OSError), open(TERMINATION_LOG, "w", encoding="utf-8") as f:
        f.write(message)


def _tools_warnings(sources: ToolSources) -> list[str]:
    """기동 이벤트의 경고다. 도구 소스를 아직 못 읽었으면 그 이유를 싣는다."""
    if sources.ready:
        return []
    return [f"{TOOLS_UNAVAILABLE}: {sources.last_error}"]


async def _refresh_loop(state: RuntimeState) -> None:
    """도구 목록을 다시 읽는다. 준비되기 전에는 짧은 간격에서 두 배씩 늘리며 다시 읽고, 된 뒤에는
    `refresh_interval` 마다 읽는다(T18). 그래프를 바꿨으면(바뀐 목록이거나 처음 준비됨)
    tools_refreshed 를 낸다."""
    retry = state.settings.retry_initial
    while True:
        if state.tools_ready:
            await asyncio.sleep(state.settings.refresh_interval)
        else:
            pause, retry = next_retry(retry)
            await asyncio.sleep(pause)
        if await state.refresh_tools():
            state.events.emit(ev.TOOLS_REFRESHED, tools=[tool.name for tool in state.agent.tools])
