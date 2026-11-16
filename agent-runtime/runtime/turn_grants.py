"""턴 위임 토큰 (P53 6절). 사람이 턴을 열거나 이어 갈 때 플랫폼이 Tool 대상마다 짧은 토큰을 내어
`turn_grant` 로 건넨다. `sub` 는 사람이고 `act` 는 이 Agent 다. 그 턴의 Tool 호출은 그 토큰으로
나가고, 목록 갱신과 위임 없는 턴은 파드 자신의 자격을 쓴다.

- **받기.** 프록시 경로만 받고 위임의 사람을 맞춰 보는 것은 입구 검사다(grant_gate.py 의
  `check_grant`).
- **대상으로 고른다.** 토큰은 `ref` 가 아니라 `tools[].source` 의 (kind, id) 에 붙는다. 롤아웃
  동안 앞 리비전의 파드는 같은 ref 가 다른 대상을 가리킬 수 있기 때문이다. 이 파드에 없는 대상의
  토큰은 버리고, 맞는 토큰이 없는 소스는 파드 자신의 자격으로 부른다.
- **턴마다 클라이언트.** 위임에 토큰이 있는 소스는 그 턴의 클라이언트로 부른다(`TurnCalls`). 한
  파드가 여러 사람의 턴을 함께 돌리고, 세션을 두는 Legacy 서버는 세션을 연 `sub` 에 묶기 때문이다.
  턴은 그래프를 돌리기 전에 자기 위임(없으면 None)을 문맥 변수에 둔다. asyncio 가 그 값을 그래프의
  태스크와 서브에이전트의 실행에 옮기므로 호출은 자기 턴의 것만 본다. 값을 두지 않은 문맥(턴 밖)의
  호출은 이 파드에 열린 위임이 있으면 누구의 호출인지 모르므로 Tool 오류로 닫힌 쪽으로 넘어진다.
- **갱신.** 수명의 3분의 1이 남으면 `renew_url` 의 `/renew` 를 인스턴스 토큰과 위임의 비밀로 부른다.
  만료는 플랫폼의 `exp` 가 아니라 받은 `expires_in` 을 이 파드의 단조 시계로 잰다. 429·5xx·연결
  오류는 `Retry-After` 를 따라 만료 전까지 다시 하고, 그 밖의 4xx(401·403·404·410)면 그 위임의
  갱신을 멈춘다. 403 은 사람이 접근을 잃은 것이라 턴도 멈추고(turn_access_lost), 파드가
  내려가는 중의 401 은 배포 해제·삭제가 인스턴스 토큰을 회수한 것이라 기다리던 턴을 멈춘다
  (turn.py `_grant_refused`).
  토큰이 끝났는데 갱신하지 못했으면 그 대상의 호출을 보내지 않고 Tool 오류로 답한다. Agent 자신의
  자격으로 내려가지 않는다. P52 의 규칙에서 Agent 는 그 사람보다 넓게 허락될 수 있다.
- **끝내기.** 턴이 끝나면(완료·오류·승인 대기·중지) 세션의 리스를 풀기 전에 `/end` 를 부른다.
  파드의 drain 이 멈춘 턴(turn_aborted)은 `shutdown`, 접근을 잃어 멈춘 턴은 `error` 다. 턴은 요청과
  떨어져 돌므로(D92) 플랫폼은 스트림이 닫혀도 위임을 끝내지 않는다. 이 알림이 실패하면 마감 시각이
  마지막 그물이다.
- 토큰과 비밀은 턴의 메모리에만 있다. 그래프 상태·체크포인트·전사·이벤트·로그·오류 본문에 싣지
  않는다. `turn_start` 는 위임 id 만 싣는다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from fastmcp import Client
from langchain_core.tools import BaseTool, ToolException
from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("daegam.agent-runtime.turn-grants")

# 응답 머리. 위임을 받은 런타임이 그 id 를 되돌려 플랫폼이 받았는지 안다(P53 4절). 이 머리가 없는
# 답은 위임을 모르는 옛 이미지의 것이다
TURN_GRANT_HEADER = "X-Daegam-Turn-Grant"
# 남은 수명이 이 몫이 되면 갱신한다
RENEW_AT_FRACTION = 1 / 3
# 갱신을 다시 할 때 처음 기다리는 시간과 그 상한(초). Retry-After 가 있으면 그것이다
RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 15.0
# 갱신과 다음 갱신 사이의 가장 짧은 간격(초). 아주 짧은 수명을 받아도 갱신 고리가 쉬지 않고
# 돌지 않는다
RENEW_MIN_WAIT_SECONDS = 1.0
# 끊긴 호출의 클라이언트를 닫을 때 기다리는 상한(초)
CLIENT_CLOSE_SECONDS = 3.0
# 갱신과 끝내기 한 번의 상한(초)
RENEW_TIMEOUT_SECONDS = 10.0
END_TIMEOUT_SECONDS = 3.0
# 이 상태면 다시 갱신해 본다. 그 밖의 4xx(401·403·404·410 따위)는 거절이라 그 위임의 갱신을 멈춘다
RETRIED_STATUSES = frozenset({429})
# `/end` 에 싣는 끝난 까닭 (P53 4절). 턴이 끝난 길 넷과 파드가 내려가며 닫은 것이다
END_DONE = "done"
END_ERROR = "error"
END_INTERRUPT = "interrupt"
END_CANCELLED = "cancelled"
END_SHUTDOWN = "shutdown"

Target = tuple[str, str]


class GrantToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    id: str = Field(min_length=1)
    token: str = Field(min_length=1)


class TurnGrant(BaseModel):
    """플랫폼이 `/chat`·결정·다시 생성 본문에 싣는 위임이다(P53 4절)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    secret: str = Field(min_length=1, max_length=256)
    sub: str = Field(min_length=1)
    tokens: list[GrantToken] = Field(default_factory=list)
    expires_in: float = Field(gt=0)
    renew_url: str = Field(min_length=1)


class TurnTokenUnavailable(ToolException):
    """이 턴의 위임 토큰으로 부를 수 없다. 만료했는데 갱신하지 못했거나, 누구의 호출인지 모른다.
    Tool 오류로 모델에게 간다."""


# 화면 문구 표에서 정한 문구다 (P53 의 런타임 Tool 오류 두 줄). 모델에게 가는
# 런타임 문구라 시간 초과 문구처럼 한다체다. 보내기 전에 막은 호출과 도중에 끊은 호출이다. 끊은
# 호출은 서버가 이미 실행했을 수 있어 모델이 그대로 되풀이하지 않게 한다
UNAVAILABLE_MESSAGE = (
    "대화를 시작한 사용자의 Tool 권한을 다시 확인하지 못해 {tool} 호출을 보내지 않았다."
)
INTERRUPTED_MESSAGE = (
    "대화를 시작한 사용자의 Tool 권한을 다시 확인하지 못해 {tool} 호출을 도중에 멈췄다. "
    "이미 실행됐을 수 있다."
)
UNKNOWN_TURN_MESSAGE = "어느 턴의 호출인지 알 수 없어 Tool 호출을 보내지 않았다."


@dataclass
class TurnCredentials:
    """한 턴의 위임 토큰과 그 갱신·끝내기다."""

    grant: TurnGrant
    instance_token: str | None
    http: Callable[[], httpx2.AsyncClient]
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    tokens: dict[Target, str] = field(init=False)
    granted: frozenset[Target] = field(init=False)
    life: float = field(init=False)
    expires_at: float = field(init=False)
    stopped: bool = field(default=False, init=False)
    # 턴이 끝나 닫았다. 닫은 위임의 토큰은 남은 수명이 있어도 쓰지 않는다
    closed: bool = field(default=False, init=False)
    # 갱신이 거절된 상태 코드를 받는다(P78 1절). 403 이면 사람이 접근을 잃었고, 파드 종료 중의 401
    # 은 인스턴스 토큰이 회수됐다. 턴이 그 까닭으로 멈춘다(turn.py)
    on_refused: Callable[[int], None] | None = None
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._set(self.grant.tokens, self.grant.expires_in)
        self.granted = frozenset(self.tokens)

    def _set(self, tokens: list[GrantToken], expires_in: float) -> None:
        self.tokens = {(t.kind, t.id): t.token for t in tokens}
        self.life = expires_in
        self.expires_at = self.now() + expires_in

    def holds(self, target: Target) -> bool:
        """위임이 이 대상에 토큰을 실었는가. 아니면 그 대상은 파드 자신의 자격으로 부른다."""
        return target in self.granted

    def token(self, target: Target) -> str | None:
        """이 대상의 살아 있는 토큰. 갱신이 이 대상을 뺐거나, 만료했거나, 닫았으면 None 이다."""
        if self.closed or self.now() >= self.expires_at:
            return None
        return self.tokens.get(target)

    def start(self) -> None:
        self._task = asyncio.create_task(self._renew_loop())

    async def close(self, reason: str) -> None:
        """갱신을 멈추고 플랫폼에 턴이 끝났다고 알린다."""
        self.closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._end(reason)

    async def _renew_loop(self) -> None:
        while not self.stopped:
            wait = self.expires_at - self.now() - self.life * RENEW_AT_FRACTION
            await self.sleep(max(RENEW_MIN_WAIT_SECONDS, wait))
            await self._renew()

    async def _renew(self) -> None:
        """한 번 갱신한다. 다시 할 만한 실패는 만료 전까지 기다렸다 다시 한다."""
        delay = RETRY_INITIAL_SECONDS
        while self.now() < self.expires_at:
            wait = await self._renew_once()
            if wait is None:
                return
            if self.stopped:
                return
            await self.sleep(min(wait or delay, max(0.0, self.expires_at - self.now())))
            delay = min(delay * 2, RETRY_MAX_SECONDS)
        self.stopped = True

    async def _renew_once(self) -> float | None:
        """None 이면 끝났다(받았거나 멈췄다). 0 이면 기본 간격, 그 밖은 Retry-After 초만큼 뒤에 다시
        한다. 모양이 맞지 않는 200 도 다시 한다."""
        try:
            response = await self._post("renew", {}, RENEW_TIMEOUT_SECONDS)
        except httpx2.HTTPError as e:
            log.warning(
                "플랫폼에 연결할 수 없어 턴 위임 %s 를 갱신하지 못했다: %s",
                self.grant.id,
                type(e).__name__,
            )
            return 0.0
        if response.status_code == 200:
            return self._take(response)
        if 400 <= response.status_code < 500 and response.status_code not in RETRIED_STATUSES:
            log.warning("턴 위임 %s 갱신이 %s 로 거절됐다", self.grant.id, response.status_code)
            self.stopped = True
            if self.on_refused is not None:
                self.on_refused(response.status_code)
            return None
        return _retry_after(response.headers.get("retry-after"))

    def _take(self, response: httpx2.Response) -> float | None:
        """갱신 답을 받는다. 본문이 JSON 이 아니거나(JSONDecodeError) 모양이 다르면(ValidationError,
        ValueError 다) 0 을 돌려 다시 한다. 답에는 토큰이 있으므로 오류도 본문도 남기지 않는다."""
        try:
            renewed = TurnGrantRenewal.model_validate(response.json())
        except ValueError:
            log.warning("턴 위임 %s 갱신 답의 모양이 맞지 않다", self.grant.id)
            return 0.0
        self._set(renewed.tokens, renewed.expires_in)
        return None

    async def _end(self, reason: str) -> None:
        """플랫폼에 턴이 끝났다고 알린다. 실패하면 위임의 마감 시각이 끝낸다."""
        try:
            await self._post("end", {"reason": reason}, END_TIMEOUT_SECONDS)
        except httpx2.HTTPError as e:
            log.info("턴 위임 %s 를 끝냈다고 알리지 못했다: %s", self.grant.id, type(e).__name__)

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> httpx2.Response:
        """위임의 `renew_url` 아래 path 를 인스턴스 토큰과 위임의 비밀로 부른다."""
        headers = {"Authorization": f"Bearer {self.instance_token}"} if self.instance_token else {}
        return await self.http().post(
            f"{self.grant.renew_url}/{path}",
            json={"secret": self.grant.secret, **body},
            headers=headers,
            timeout=timeout,
        )


def _retry_after(value: str | None) -> float:
    """Retry-After 의 초. 없거나, 초가 아니거나(날짜, `²` 같은 글자), 음수나 무한이면 0 이라 기본
    간격을 쓴다."""
    try:
        seconds = float(value) if value else 0.0
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


class TurnGrantRenewal(BaseModel):
    """`/renew` 의 답이다. 같은 대상의 새 토큰들과 새 수명이다."""

    model_config = ConfigDict(extra="ignore")

    tokens: list[GrantToken] = Field(default_factory=list)
    expires_in: float = Field(gt=0)


class GrantAuth(httpx2.Auth):
    """턴의 클라이언트가 요청마다 그 대상의 지금 토큰을 붙인다. 갱신이 바꾼 토큰도 다음 요청부터
    실린다. 토큰이 없으면 요청을 보내지 않는다. 여기서 올린 예외는 MCP 전송의 배경 태스크가 삼켜
    호출이 Tool 타임아웃까지 기다리므로, 먼저 lapse 로 그 대상을 부르던 턴의 호출을 끊는다."""

    def __init__(
        self, credentials: TurnCredentials, target: Target, lapse: Callable[[], None]
    ) -> None:
        self._credentials = credentials
        self._target = target
        self._lapse = lapse

    def auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        token = self._credentials.token(self._target)
        if token is None:
            self._lapse()
            raise TurnTokenUnavailable("turn token expired")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


@dataclass(eq=False)
class TurnCalls:
    """한 턴이 쓰는 소스마다의 클라이언트와 Tool 이다. 처음 부를 때 만든다. 같은 것인지는 객체로
    가린다(eq=False). 한 세션의 앞 턴과 다음 턴의 것이 따로다."""

    credentials: TurnCredentials
    _clients: dict[str, Client[Any]] = field(default_factory=dict)
    _tools: dict[tuple[str, str], BaseTool] = field(default_factory=dict)
    # 대상마다 지금 도는 호출의 취소 범위다. 토큰이 끝나면 `_lapse` 가 끊는다
    _running: dict[Target, set[anyio.CancelScope]] = field(default_factory=dict)

    async def tool(
        self,
        ref: str,
        definition: Tool,
        target: Target,
        build: Callable[[httpx2.Auth], Client[Any]],
        adapt: Callable[[Tool, Client[Any]], Awaitable[BaseTool]],
    ) -> Callable[..., Awaitable[Any]]:
        """이 턴의 클라이언트로 부르는 같은 Tool 의 코루틴이다. 그 대상의 토큰이 끝났으면 보내지
        않고, 부르는 사이 끝나면 기다리지 않고 끊어 Tool 오류다. 끊은 호출의 클라이언트는 버린다."""
        if self.credentials.token(target) is None:
            raise TurnTokenUnavailable(UNAVAILABLE_MESSAGE.format(tool=definition.name))
        client = self._clients.get(ref)
        if client is None:
            auth = GrantAuth(self.credentials, target, lambda: self._lapse(target))
            client = self._clients[ref] = build(auth)
        key = (ref, definition.name)
        built = self._tools.get(key)
        if built is None:
            built = self._tools[key] = await adapt(definition, client)
        call = built.coroutine
        assert call is not None  # as_langchain_tool 은 늘 비동기 도구를 준다

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            running = self._running.setdefault(target, set())
            with anyio.CancelScope() as scope:
                running.add(scope)
                try:
                    return await call(*args, **kwargs)
                except Exception:
                    # 끊기 전에 전송이 인증의 예외를 오류로 넘겨 돌아온 호출도 끊긴 것이다
                    if not scope.cancel_called:
                        raise
                finally:
                    running.discard(scope)
            await self._forget(ref)
            raise TurnTokenUnavailable(INTERRUPTED_MESSAGE.format(tool=definition.name))

        return guarded

    def _lapse(self, target: Target) -> None:
        for scope in self._running.get(target, ()):
            scope.cancel()

    async def _forget(self, ref: str) -> None:
        """끊은 호출의 클라이언트와 그 Tool 을 버리고 닫는다. 끊을 때 fastmcp 의 연결 닫기도 함께
        끊겨 클라이언트가 반쯤 닫힌 채 남으므로, 갱신으로 토큰이 다시 살아도 그 클라이언트로는 부를
        수 없다. 다음 호출은 새로 만든다."""
        client = self._clients.pop(ref, None)
        for key in [k for k in self._tools if k[0] == ref]:
            del self._tools[key]
        if client is None:
            return
        with anyio.move_on_after(CLIENT_CLOSE_SECONDS, shield=True):
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001  닫기는 끊은 까닭(인증의 예외)을 다시 올린다
                log.debug("끊은 턴 클라이언트를 닫았다: %s", type(e).__name__)


# 지금 도는 턴의 위임이다. 턴이 그래프를 돌리기 전에 정하고(위임 없는 턴은 None) 그래프의 태스크와
# 서브에이전트의 실행이 물려받는다. 정하지 않은 문맥은 턴 밖이다. 턴이 끝나도 되돌리지 않는다. 턴의
# 마감은 응답을 닫는 다른 태스크(다른 문맥)에서 돌 수 있고, 다음 턴은 제 값을 다시 둔다
_CURRENT: ContextVar[TurnCalls | None] = ContextVar("daegam_turn_calls")


class TurnGrants:
    """이 파드에 열린 턴의 위임이다. 턴마다 객체 하나이고, 턴은 문맥 변수로 제 것을 찾는다."""

    def __init__(
        self,
        *,
        instance_token: str | None,
        http: httpx2.AsyncClient | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._instance_token = instance_token
        self._http = http
        self._owns_http = http is None
        self._now = now
        self._sleep = sleep
        self.open_calls: set[TurnCalls] = set()

    def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = httpx2.AsyncClient()
        return self._http

    def open(
        self, grant: TurnGrant | None, *, on_refused: Callable[[int], None] | None = None
    ) -> TurnCalls | None:
        """이 턴의 위임을 열어 지금 문맥에 둔다. 위임 없는 턴은 None 을 두어 이 문맥이 턴 안이고
        위임이 없음을 적는다. on_refused 는 갱신이 거절된 상태 코드를 받는다."""
        calls = None
        if grant is not None:
            credentials = TurnCredentials(
                grant,
                self._instance_token,
                self._client,
                now=self._now,
                sleep=self._sleep,
                on_refused=on_refused,
            )
            credentials.start()
            calls = TurnCalls(credentials)
            self.open_calls.add(calls)
        _CURRENT.set(calls)
        return calls

    async def close(self, calls: TurnCalls | None, reason: str) -> None:
        """그 턴의 위임만 닫는다. 위임 없는 턴이거나 이미 닫았으면 아무것도 하지 않는다."""
        if calls is None or calls not in self.open_calls:
            return
        self.open_calls.discard(calls)
        await calls.credentials.close(reason)

    def current(self) -> TurnCalls | None:
        """지금 도는 Tool 호출의 턴 위임. 위임 없는 턴이면 None 이다. 턴 밖의 호출인데 이 파드에
        열린 위임이 있으면 누구의 호출인지 모르므로 `TurnTokenUnavailable` 이다."""
        try:
            return _CURRENT.get()
        except LookupError:
            if self.open_calls:
                raise TurnTokenUnavailable(UNKNOWN_TURN_MESSAGE) from None
            return None

    async def aclose(self) -> None:
        for calls in list(self.open_calls):
            await self.close(calls, END_SHUTDOWN)
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
