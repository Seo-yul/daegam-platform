"""런타임 설정과 파드 안 상태다(D26). 라우트(routes.py)와 앱 조립(main.py)이 같이 본다.

Settings 는 env 에서 읽는 기동 설정, RuntimeState 는 한 요청이 쓸 수 있는 것 전부다. 턴·지연·
Tool·거절의 수는 파드가 세지 않는다. 플랫폼이 이벤트 저장소에서 모든 파드의 것을 센다(P37 11절).
`runtime(request)` 는 FastAPI 의존성이라 라우트가 상태를 인자로 받는다. 문서에 모르는 기능 이름이
있으면 상태 대신 `refusal` 만 있고(D65), 저장소를 다시 여는 동안에는 상태 대신 `starting` 이유만
있다(T22).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request
from langchain_core.language_models import BaseChatModel

from runtime.auth import Authenticator, IssuerSettings, read_issuers, split_csv
from runtime.cancel import CANCEL_POLL_SECONDS, CancelWatch
from runtime.compile import CompiledAgent, compile_agent
from runtime.config import AgentConfig, ConfigError
from runtime.drain import DRAIN_ENV, Drain, drain_seconds
from runtime.events import EventLog
from runtime.knowledge import KnowledgeTools
from runtime.lease_keeper import LeaseKeeper
from runtime.long_term import LongTermMemory
from runtime.mcp import REFRESH_INTERVAL_SECONDS, ToolSources
from runtime.sandbox import SANDBOX_URL_ENV, Sandboxes
from runtime.sessions import (
    DEFAULT_TRANSCRIPT_RETENTION_DAYS,
    TRANSCRIPT_RETENTION_ENV,
    SessionStore,
    transcript_retention_days,
)
from runtime.turn_grants import TurnGrants
from runtime.turn_repair import TurnRepairs

log = logging.getLogger("daegam.agent-runtime")

SWEEP_INTERVAL_SECONDS = 60.0
# 기동 뒤 배경에서 다시 시도하는 간격이다. 저장소를 다시 열 때(T22)와 도구 목록을 다시 읽을 때(T18)
# 같이 쓴다. 처음 값에서 두 배씩 늘려 상한에서 멈춘다(boot.next_retry). 모델 호출의 재시도
# (resilience.py)와는 다른 것이다
BACKGROUND_RETRY_INITIAL_SECONDS = 2.0
BACKGROUND_RETRY_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class Settings:
    config_path: str | None = None
    instance_token: str | None = None
    tenant: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    refresh_interval: float = REFRESH_INTERVAL_SECONDS
    # 다시 시도하는 첫 간격이다(T18·T22). 테스트가 줄인다
    retry_initial: float = BACKGROUND_RETRY_INITIAL_SECONDS
    sweep_interval: float = SWEEP_INTERVAL_SECONDS
    # 다른 파드가 적은 중지 표시를 읽는 간격이다(D65). 테스트가 줄인다
    cancel_poll_interval: float = CANCEL_POLL_SECONDS
    # 파드 종료 때 도는 턴을 기다리는 초다(P78 5절, drain.py). 플랫폼이 provision 마다 렌더한다.
    # None 이면 SIGTERM 에 곧바로 멈춘다
    turn_drain_seconds: float | None = None
    # 닫힌 대화의 기록이 남는 날 수다(D46). 플랫폼이 ConfigMap 값을 파드 env 로 내려 준다
    retention_days: int = DEFAULT_TRANSCRIPT_RETENTION_DAYS
    # 외부 노출(D27)의 데이터 플레인 audience. 두 발급자가 같이 쓰고, 없으면 외부 토큰을 받지 않는다
    oidc_audience: str | None = None
    # 데이터 플레인이 믿는 발급자(`auth.OIDC_ENV`)와 그 곁의 IdP(`auth.IDP_ENV`, P53b 6.8절)다.
    # env 에서는 `auth.read_issuers` 가 읽는다
    oidc: IssuerSettings | None = None
    idp: IssuerSettings | None = None
    cors_origins: tuple[str, ...] = ()
    # 코드 실행의 기계 경로(P40 5절). 플랫폼이 코드 실행을 켠 Agent 의 파드에만 렌더한다
    sandbox_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        oidc, idp = read_issuers(env)
        return cls(
            config_path=env.get("DAEGAM_AGENT_CONFIG") or None,
            instance_token=env.get("DAEGAM_INSTANCE_TOKEN") or None,
            tenant=env.get("DAEGAM_TENANT") or None,
            host=env.get("RUNTIME_HOST", "0.0.0.0"),
            port=int(env.get("RUNTIME_PORT", "8080")),
            oidc_audience=env.get("DAEGAM_OIDC_AUDIENCE") or None,
            oidc=oidc,
            idp=idp,
            cors_origins=split_csv(env.get("DAEGAM_CORS_ORIGINS")),
            retention_days=transcript_retention_days(env.get(TRANSCRIPT_RETENTION_ENV)),
            turn_drain_seconds=drain_seconds(env.get(DRAIN_ENV)),
            sandbox_url=(env.get(SANDBOX_URL_ENV) or "").strip() or None,
        )

    @property
    def issuers(self) -> tuple[IssuerSettings, ...]:
        """외부 토큰을 받는 발급자들이다. audience 가 없으면 하나도 없고, 한쪽 발급자만 있으면 그
        하나다."""
        if not self.oidc_audience:
            return ()
        return tuple(spec for spec in (self.oidc, self.idp) if spec is not None)


@dataclass
class RuntimeState:
    config: AgentConfig
    settings: Settings
    events: EventLog
    store: SessionStore
    sources: ToolSources
    agent: CompiledAgent
    auth: Authenticator
    # 이 파드가 쥔 턴 리스를 늘리는 관리자다(P79, lease_keeper.py). 저장소와 같이 기동 때 연다
    leases: LeaseKeeper
    # knowledge[] 의 검색 도구와 풀(D32). 기동 때 한 번 열고 재컴파일마다 같은 것을 넘긴다
    knowledge: KnowledgeTools | None = None
    # 장기 기억 저장소(D34). knowledge 와 같이 기동 때 한 번 열고 재컴파일은 같은 것을 넘긴다.
    # 여기서 다시 열면 TTL sweeper 와 배치 루프가 루프에 쌓인다
    long_term: LongTermMemory | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    model_override: BaseChatModel | None = None
    # 이 파드가 돌리는 턴의 중지 감시(D65). 세션 id 로 찾고 같은 파드로 들어온 중지 요청은 색인의
    # 표시를 기다리지 않고 바로 알린다. 턴이 끝나면 turn.py 가 뺀다
    cancels: dict[str, CancelWatch] = field(default_factory=dict)
    # 이 파드가 돌리는 턴 태스크(D92, turn_task.py). 요청과 떨어져 돌므로 여기서 들어 GC 되지 않게
    # 하고, 파드 종료가 남은 턴을 기다린다(drain.py). 끝난 태스크는 스스로 빠진다
    turns: set[asyncio.Task[Any]] = field(default_factory=set)
    # 파드가 내려가는 중이다(drain.py). 그동안 거절된 인스턴스 토큰은 턴을 멈춘다
    draining: bool = False
    # 내려가기 시작한 뒤의 종료 기다림이다(drain.py). 그 뒤에 감시를 건 턴도 같은 규칙을 받는다
    drain: Drain | None = None
    # 읽은 도구 목록으로 그래프를 컴파일했는가(T18). 준비 검사와 턴 입구가 이것을 본다. 소스를
    # 읽었어도 그 목록으로 컴파일하지 못했으면 거짓이다. 도구 없는 그래프로 답하지 않게 하려는
    # 것이다
    tools_ready: bool = False
    # 읽은 목록으로 다시 컴파일하다 난 오류다. healthz 상세가 소스의 읽기 오류보다 먼저 보인다
    compile_error: str | None = None
    # 이 파드에 열린 턴의 위임 토큰(P53 6절). 도구 소스(sources)와 같은 것을 든다
    grants: TurnGrants = field(default_factory=lambda: TurnGrants(instance_token=None))
    # 끊긴 턴의 되살리기 자리와 쉬는 시간이다(P79 3절, turn_repair.py)
    repairs: TurnRepairs = field(default_factory=TurnRepairs)
    # 코드 실행 문서의 세션마다 샌드박스다(P40, sandbox.py). 꺼진 문서는 None 이다
    sandboxes: Sandboxes | None = None

    async def refresh_tools(self) -> bool:
        """도구 목록을 다시 읽고, 바뀌었거나 아직 준비 전이면 그래프를 다시 컴파일한다. 그래프를
        바꿨으면 True 다(호출자가 tools_refreshed 를 낸다). 컴파일이 실패하면 이전 그래프와 준비
        상태를 그대로 두고 오류를 `compile_error` 에 남긴다."""
        changed = await self.sources.refresh()
        if not self.sources.ready or not (changed or not self.tools_ready):
            return False
        try:
            self.recompile()
        except Exception as e:  # noqa: BLE001  이전 그래프를 계속 쓰고 다음 읽기에 다시 본다
            self.compile_error = f"{type(e).__name__}: {e}"
            log.warning("도구 목록으로 다시 컴파일하지 못했다: %s", self.compile_error)
            return False
        self.compile_error = None
        self.tools_ready = True
        return True

    def recompile(self) -> None:
        self.agent = compile_agent(
            self.config,
            tools_by_ref=self.sources.tools_by_ref,
            knowledge=self.knowledge,
            checkpointer=self.store.checkpointer,
            model=self.model_override,
            long_term=self.long_term,
            sandboxes=self.sandboxes,
        )


def refusal(request: Request) -> ConfigError | None:
    """문서에 모르는 기능 이름이 있어 채팅을 받지 않는 상태의 이유다(D65). 보통은 None 이다. 그
    상태에서는 create_app 의 미들웨어가 healthz 를 뺀 모든 요청을 503 으로 끊어 여기까지 오지
    않는다."""
    return getattr(request.app.state, "refusal", None)


def starting(request: Request) -> ConfigError | None:
    """저장소에 아직 연결하지 못해 자원을 다시 여는 상태의 이유다(T22). 보통은 None 이다. 그
    상태에서는 create_app 의 미들웨어가 healthz 를 뺀 모든 요청을 503 으로 끊어 여기까지 오지
    않는다."""
    return getattr(request.app.state, "starting", None)


def runtime(request: Request) -> RuntimeState:
    return request.app.state.runtime


Runtime = Annotated[RuntimeState, Depends(runtime)]
