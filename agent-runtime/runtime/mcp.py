"""MCP 도구 소스다. tools[] 를 langchain.mcp 의 `as_langchain_tool` 로 읽는다(D26·T6).

- 인증은 oauth_client_credentials(httpx2.Auth 로 요청마다 Bearer 를 붙이고 만료 60초 전에 갱신),
  bearer(파드 env 의 고정 토큰을 그대로 붙인다. 플랫폼이 낸 서비스 토큰, D41) 또는 none 이다.
  비밀은 secret_env 가 가리키는 파드 env 에서만 읽는다.
- allow 가 비어 있지 않으면 그 이름만 남긴다.
- 기동은 tools/list 를 소스마다 최대 15초만 기다리고, 실패해도 이어 간다(T18). 클러스터가 통째로
  다시 뜨면 Agent 가 도구 서버보다 먼저 뜨기 때문이다. 소스 하나라도 못 읽으면 전체가 실패이고
  `ready` 가 거짓이다. 호출자(`RuntimeState.refresh_tools`)가 배경에서 다시 읽고, 읽은 목록으로
  그래프를 컴파일한 뒤에야 준비된다. 읽은 뒤에는 5분마다 다시 읽고 바뀌면 다시 컴파일한다.
- **소스마다 어댑터를 하나씩 둔다.** 여러 서버를 한 어댑터에 넣으면 툴 이름이 `{server}_{tool}` 로
  프리픽스되는데, allow·timeouts·agent.json 이 전부 맨 이름을 키로 쓰므로 그러면 계약이 깨진다.
- 툴 호출마다 세션이 새로 열린다(fastmcp 클라이언트는 재진입 가능하다). 툴별 타임아웃은
  langchain.mcp 에 인터셉터가 없으므로 도구를 감싸서 건다.
- 턴 위임(P53 6절, turn_grants.py)이 그 소스의 대상에 토큰을 실은 턴의 호출은 파드의 클라이언트가
  아니라 그 턴의 클라이언트로 나간다. 같은 감싸개가 호출할 때마다 그 턴을 찾는다. 목록은 파드 자신의
  자격으로 읽고, 턴의 Tool 은 그 목록의 정의로 만든다.
- 클라이언트마다 사용자 입력 요청(elicitation)을 거절하는 처리기를 단다. 런타임은 턴 도중
  사용자에게 되물을 길이 없다(승인은 도구 호출 단위의 HITL 이다, D27). 처리기가 있어야
  langchain.mcp 가 LangGraph interrupt 로 답하게 무장하지 않고, 우리 손의 클라이언트가 실제로 맺은
  세션이라 MCP 규약 판을 읽을 수 있다.
- tools/list 를 읽는 연결이 열려 있는 동안 소스마다 맺은 규약 판을 `protocols` 에 든다(P35). 기본
  mode="auto" 는 2026-07-28 server/discover 를 먼저 보내고 안 되면 initialize 로 내려간다.
- 도구 소스가 호출을 거절하거나(403, Tool 규칙) 한도에 걸리면(429) 턴을 끝내지 않고 모델이 보는
  Tool 오류로 바꾼다(P52 5절, 능력 `tool_refusals`). 규칙이 이 Agent 에게 숨긴 Tool 은 목록에
  없으므로 거절은 드물다(목록을 읽은 뒤 바뀐 규칙, 조건 때문에 닫힌 Tool, 지문이 뒤처진 Gateway).
  둘은 fastmcp 클라이언트와 LangChain Tool 이 똑같이 올리는 `MCPError` 의 메시지로 가른다(F0 4번).
- 도구 소스가 방금 연 세션을 모른다고 답한 호출(404)은 그 Tool 의 제한 시간 안에서 새 세션으로 다시
  보낸다. 막 교체된 Gateway 의 vMCP 는 백엔드에 붙기 전까지 여는 세션마다 곧바로 끝내고, 다음 건강
  검사(30초 간격) 뒤에야 받는다. 404 는 호출이 백엔드에 닿기 전의 답이라 다시 보내도 두 번 실행되지
  않는다. 한 소스에 겹쳐 나간 호출들은 세션 하나를 함께 쓰므로(클라이언트가 재진입이다), 끝난 세션을
  오래 걸리는 다른 호출이 쥐고 있는 동안에는 다시 보내도 받아들여지지 않고 제한 시간에 Tool 오류가
  된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from langchain_core._api import suppress_langchain_beta_warning

# langchain.mcp 는 beta 라 import 마다 LangChainBetaWarning 을 낸다. 파드가 뜰 때마다
# 로그에 찍히므로 이 한 번의 import 에서만 눌러 둔다. 베타라는 사실은 T6 기록
# (docs/archive/reviews/2026-09-10-t6-langchain-mcp-parity.md)에 적었다.
with suppress_langchain_beta_warning():
    from langchain.mcp import as_langchain_tool
from langchain_core.tools import BaseTool, ToolException
from mcp import MCPError
from mcp.types import Tool

from runtime.config import AgentConfig, ConfigError
from runtime.trace import (
    ERROR_RATE_LIMITED,
    ERROR_REFUSED,
    TOOL_REFUSED,
    TOOL_TIMEOUT,
    signal,
)
from runtime.turn_grants import Target, TurnGrants, TurnTokenUnavailable

log = logging.getLogger("daegam.agent-runtime.mcp")

REFRESH_INTERVAL_SECONDS = 300
# 소스 하나의 tools/list 를 기다리는 시간. 연결만 받고 답하지 않는 서버 앞에서는 클라이언트
# 기본(읽기 300초)까지 묶이고, 기동 중이면 살아 있음 검사(약 180초)가 먼저 끝나 파드가 죽으므로
# 상한을 둔다
TOOLS_LIST_TIMEOUT_SECONDS = 15.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 60
TOKEN_REFRESH_MARGIN_SECONDS = 60.0
DEFAULT_TOKEN_TTL_SECONDS = 3600.0


# ToolHive 의 프록시와 vMCP 가 거절한 Tool 호출의 `MCPError` 메시지다 (P52 5절, F0 4번). 403 은
# Cedar 가 거절한 것이고 429 는 호출 한도다. REFUSALS_MEASURED_ON 은 이 메시지를 잰 ToolHive 판이고,
# 플랫폼 시험(`tests/test_tool_refusal_messages.py`)이 플랫폼이 요구하고 설치하는 판과 맞댄다. 판을
# 올리면 메시지를 다시 재고 이 판을 고친다
REFUSED_MESSAGE = "Unauthorized"
LIMITED_MESSAGE = "Rate limit exceeded"
REFUSALS_MEASURED_ON = "0.53.0"

# 세션이 있는 연결에서 요청이 404 를 받으면 MCP 클라이언트(mcp/client/streamable_http.py)가 올리는
# `MCPError` 의 메시지다. 서버가 그 세션을 모른다는 뜻이고, 그 요청은 처리되지 않았다
SESSION_GONE_MESSAGE = "Session terminated"
# 세션이 끝난 호출을 다시 보내기 전에 쉬는 시간이다. 다시 보내기는 그 Tool 의 제한 시간까지만 한다
SESSION_RETRY_PAUSE_SECONDS = 2.0


class _ToolTimeout(ToolException):
    """툴이 제한 시간 안에 답하지 않았다. handle_tool_error 가 받아 모델에게 알린다."""


class _ToolRefused(ToolException):
    """도구 소스가 이 호출을 거절했거나(403) 한도에 걸렸다(429). handle_tool_error 가 받아 모델에게
    알린다. 턴은 이어 간다."""


def refusal_kind(error: MCPError) -> str | None:
    """거절된 호출의 종류(스팬의 error_type)다. 다른 오류면 None 이다(그 오류는 원래대로
    올라간다). 메시지만으로는 무엇이 거절했는지 알 수 없다(Cedar 규칙, 토큰, 한도)."""
    if error.message == REFUSED_MESSAGE:
        return ERROR_REFUSED
    if error.message == LIMITED_MESSAGE:
        return ERROR_RATE_LIMITED
    return None


def refusal_text(tool: str, kind: str) -> str:
    """모델에게 보일 글이다."""
    if kind == ERROR_RATE_LIMITED:
        return (
            f"tool {tool} 호출이 도구 소스의 호출 한도에 걸렸다. 잠시 뒤 다시 부르거나 다른 방법을 "
            "찾는다"
        )
    return f"tool {tool} 호출을 도구 소스가 거절했다. 같은 호출을 되풀이하지 말고 사용자에게 알린다"


class ToolsListError(RuntimeError):
    """소스 하나의 tools/list 를 읽지 못했다. 문구가 소스 ref 로 시작해 어느 소스인지 보인다."""


class ClientCredentialsAuth(httpx2.Auth):
    """client credentials 토큰을 요청마다 Authorization 에 붙인다. 만료 60초 전이면 새로 받는다."""

    requires_response_body = False

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        http: httpx2.AsyncClient | None = None,
        now: Callable[[], float] = time.monotonic,
        refresh_margin: float = TOKEN_REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http = http
        self._owns_http = False
        self._now = now
        self._margin = refresh_margin
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock: asyncio.Lock | None = None
        self.fetches = 0

    def sync_auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        raise RuntimeError("ClientCredentialsAuth 는 async 전용이다")

    async def async_auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {await self.token()}"
        response = yield request
        if response.status_code == 401:
            # 서버가 토큰을 거부하면 한 번만 새로 받아 다시 보낸다.
            request.headers["Authorization"] = f"Bearer {await self.token(force=True)}"
            yield request

    async def token(self, *, force: bool = False) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not force and self._token and self._now() < self._expires_at - self._margin:
                return self._token
            await self._fetch()
            assert self._token is not None
            return self._token

    def _client(self) -> httpx2.AsyncClient:
        """토큰 조회에 쓰는 클라이언트다. 주입받은 것이 있으면 그것을 쓰고 없으면 만들어 계속 쓴다.

        조회마다 새로 만들면 갱신할 때와 401 재시도마다 TLS 핸드셰이크를 다시 하고 연결을
        재사용하지 못한다. 기동 때 소스가 여럿이면 그 비용이 한꺼번에 든다."""
        if self._http is None:
            self._http = httpx2.AsyncClient(timeout=httpx2.Timeout(15.0))
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        """직접 만든 클라이언트만 닫는다. 주입받은 것은 준 쪽이 닫는다."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False

    async def _fetch(self) -> None:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope
        response = await self._client().post(self._token_url, data=data)
        if response.status_code != 200:
            raise RuntimeError(
                f"token endpoint {self._token_url} 가 {response.status_code} 를 냈다"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"token endpoint {self._token_url} 응답에 access_token 이 없다")
        ttl = payload.get("expires_in")
        ttl = float(ttl) if isinstance(ttl, int | float) else DEFAULT_TOKEN_TTL_SECONDS
        self._token = token
        self._expires_at = self._now() + ttl
        self.fetches += 1


class BearerAuth(httpx2.Auth):
    """파드 env 의 고정 토큰을 요청마다 Authorization 에 붙인다.

    401 재시도는 없다. env 는 파드 수명 동안 같으므로 다시 읽어도 같은 값이고, 만료된 토큰이면
    파드가 굴러야 한다(플랫폼 리컨실이 굴린다, D41)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def build_auth(auth: dict[str, Any]) -> httpx2.Auth | None:
    if auth["kind"] == "none":
        return None
    secret = os.environ.get(auth["secret_env"])
    if not secret:
        raise ConfigError(
            "tool_secret_missing", f"tools[].auth.secret_env {auth['secret_env']!r} 가 비어 있다"
        )
    if auth["kind"] == "bearer":
        return BearerAuth(secret)
    return ClientCredentialsAuth(
        token_url=auth["token_url"],
        client_id=auth["client_id"],
        client_secret=secret,
        scope=auth["scope"],
    )


async def decline_elicitation(
    message: str, response_type: Any, params: Any, context: Any
) -> ElicitResult[Any]:
    """서버의 사용자 입력 요청(elicitation)을 거절한다.

    처리기가 없는 클라이언트는 langchain.mcp 가 LangGraph interrupt 로 답하게 무장하는데
    (langchain/mcp/adapter.py), 그 interrupt 는 action_requests 가 비어 /decisions 로 풀 수 없다.
    거절은 도구에 decline 으로 전해지고 그 뒤는 도구가 정한다. 2026-07-28 서버는
    InputRequiredResult 로 묻고 옛 서버는 elicitation/create 로 묻는데 fastmcp 가 둘 다 이 처리기로
    보낸다."""
    log.warning("MCP 서버가 사용자 입력을 요청했다. 거절한다: %s", message)
    return ElicitResult(action="decline")


def build_client(source: dict[str, Any], auth: httpx2.Auth | None) -> Client[Any]:
    """소스 하나짜리 fastmcp 클라이언트다. 서버가 하나뿐이라 툴 이름에 프리픽스가 안 붙는다.

    처리기가 있는 클라이언트는 langchain.mcp 가 무장하지 않고 그대로 쓴다(모듈 docstring)."""
    return Client(source["url"], auth=auth, elicitation_handler=decline_elicitation)


def tool_signature(tools: list[BaseTool]) -> str:
    """이름·설명·인자 스키마로 만든 지문이다. 주기 갱신에서 바뀜을 판정한다."""
    items = []
    for tool in sorted(tools, key=lambda t: t.name):
        schema: Any = None
        try:
            schema = tool.tool_call_schema.model_json_schema()
        except Exception:  # noqa: BLE001  스키마가 없거나 pydantic 이 아니어도 지문은 만든다
            schema = getattr(tool, "args", None)
        items.append({"name": tool.name, "description": tool.description, "schema": schema})
    return json.dumps(items, sort_keys=True, ensure_ascii=False, default=str)


class ToolSources:
    """agent.json tools[] 전부를 관리한다. ref 별 도구 목록을 든다."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        default_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        list_timeout: float = TOOLS_LIST_TIMEOUT_SECONDS,
        session_retry_pause: float = SESSION_RETRY_PAUSE_SECONDS,
        grants: TurnGrants | None = None,
    ) -> None:
        self._sources = {source["ref"]: source for source in config.tools}
        # 소스마다의 위임 대상이다. 위임 토큰은 ref 가 아니라 이것에 붙는다 (P53 2절)
        self._targets: dict[str, Target] = {
            ref: (source["source"]["kind"], source["source"]["id"])
            for ref, source in self._sources.items()
        }
        self.grants = grants or TurnGrants(instance_token=None)
        self._list_timeout = list_timeout
        self._timeouts = {
            name: float(seconds)
            for name, seconds in config.timeouts_for(list(self._sources)).items()
        }
        self._default_timeout = default_timeout
        self._session_retry_pause = session_retry_pause
        # 소스마다 클라이언트 하나다. 한 클라이언트에 여러 서버를 넣으면
        # 툴 이름이 프리픽스된다(모듈 docstring). client credentials 인증은 토큰 조회
        # 클라이언트를 스스로 만들므로 들고 있다가 close 에서 닫는다
        self._clients: dict[str, Client[Any]] = {}
        self._auths: list[ClientCredentialsAuth] = []
        for ref, source in self._sources.items():
            auth = build_auth(source["auth"])
            if isinstance(auth, ClientCredentialsAuth):
                self._auths.append(auth)
            self._clients[ref] = build_client(source, auth)
        self.tools_by_ref: dict[str, list[BaseTool]] = {ref: [] for ref in self._sources}
        # 소스마다 tools/list 를 읽은 연결이 맺은 MCP 규약 판이다. 아직 읽지 않았으면 None 이다
        self.protocols: dict[str, str | None] = dict.fromkeys(self._sources)
        self.signature = tool_signature([])
        self.loaded_at: float | None = None
        self.last_error: str | None = None

    @property
    def refs(self) -> list[str]:
        return list(self._sources)

    @property
    def ready(self) -> bool:
        """모든 소스의 tools/list 를 한 번이라도 읽었는가. 소스가 없으면 읽을 것이 없어 참이다."""
        return not self._sources or self.loaded_at is not None

    async def load(self) -> bool:
        """모든 소스의 tools/list 를 읽는다. 도구 지문이 바뀌었으면 True 다. 소스 하나라도 못 읽으면
        `ToolsListError` 다."""
        if not self._clients:
            return False
        loaded: dict[str, list[BaseTool]] = {}
        protocols: dict[str, str | None] = {}
        for ref, source in self._sources.items():
            try:
                tools, protocols[ref] = await self._list(ref)
            except Exception as e:  # noqa: BLE001  어느 소스인지 붙여 다시 올린다
                detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                raise ToolsListError(f"tools[{ref}]: {detail}") from e
            allow = set(source["allow"])
            if allow:
                tools = [tool for tool in tools if tool.name in allow]
                missing = sorted(allow - {tool.name for tool in tools})
                if missing:
                    log.warning("tools[%s] allow 에 있지만 서버에 없는 툴: %s", ref, missing)
            loaded[ref] = tools
        log.info("tools/list 를 읽었다. 소스별 MCP 규약 판: %s", protocols)
        signature = tool_signature([tool for tools in loaded.values() for tool in tools])
        changed = signature != self.signature
        self.tools_by_ref = loaded
        self.protocols = protocols
        self.signature = signature
        self.loaded_at = time.time()
        self.last_error = None
        return changed

    async def _list(self, ref: str) -> tuple[list[BaseTool], str | None]:
        """소스 하나의 (도구, 맺은 규약 판)이다. `_list_timeout` 안에 끝나야 한다.

        목록을 읽는 연결을 여기서 연다. list_tools 가 스스로 여닫는 연결은 돌아올 때 닫혀
        protocol_version 이 None 이다. 클라이언트는 재진입이라 안쪽은 같은 세션을 쓴다. 도구 정의는
        감싸개가 들고 있다가 턴의 클라이언트로 같은 도구를 만들 때 쓴다."""
        client = self._clients[ref]
        async with asyncio.timeout(self._list_timeout), client:
            tools = [
                self._wrap(await as_langchain_tool(definition, client), ref, definition)
                for definition in await client.list_tools()
            ]
            return tools, client.protocol_version

    async def close(self) -> None:
        """인증이 만든 토큰 조회 클라이언트를 닫는다. 주기 갱신이 멈춘 뒤에 부른다."""
        for auth in self._auths:
            await auth.aclose()

    async def refresh(self) -> bool:
        """기동과 주기 갱신용이다. 실패해도 예외를 내지 않고 이전 목록을 유지한다."""
        try:
            return await self.load()
        except Exception as e:  # noqa: BLE001  일시 장애면 다음 주기에 다시 시도한다
            own = isinstance(e, ToolsListError)
            self.last_error = str(e) if own else f"{type(e).__name__}: {e}"
            log.warning("tools/list 읽기 실패: %s", self.last_error)
            return False

    def _wrap(self, tool: BaseTool, ref: str, definition: Tool) -> BaseTool:
        """툴별 타임아웃을 걸고, 위임 토큰이 있는 턴의 호출은 그 턴의 클라이언트로 보낸다(`_route`).

        langchain.mcp 에는 tool_interceptors 가 없다(상류가 없앴다). 도구의 코루틴을 감싸
        `asyncio.timeout` 을 걸고, 넘으면 `_ToolTimeout` 을 올린다. 그것을 도구의
        handle_tool_error 가 받아 status="error" 인 ToolMessage 로 만든다. 옛 인터셉터가
        isError=True 인 CallToolResult 로 만들던 것과 모델이 보는 모양이 같다.

        상류 핸들러는 자기 예외가 아니면 되던진다(langchain/mcp/tools.py 의
        _handle_mcp_tool_error). 그래서 우리 예외만 가로채고 나머지는 원래 핸들러에 넘긴다."""
        seconds = self._timeouts.get(tool.name, self._default_timeout)
        inner = tool.coroutine
        if inner is None:  # pragma: no cover  MCPAdapter 는 늘 비동기 도구를 준다
            return tool
        upstream = tool.handle_tool_error

        async def run(*args: Any, **kwargs: Any) -> Any:
            call = await self._route(ref, definition, inner)
            # 도구 소스가 세션을 끝낸 횟수와, 지금 다시 보내기를 기다리는 중인지다
            gone, pausing = 0, False
            try:
                async with asyncio.timeout(seconds):
                    while True:
                        try:
                            return await call(*args, **kwargs)
                        except MCPError as e:
                            # 도구 소스가 방금 연 세션을 모른다(모듈 docstring). 같은 호출을 새
                            # 세션으로 다시 보낸다
                            if e.message != SESSION_GONE_MESSAGE:
                                raise
                        gone += 1
                        if gone == 1:
                            log.warning(
                                "tools[%s] %s 호출의 세션을 도구 소스가 끝냈다. 다시 보낸다",
                                ref,
                                tool.name,
                            )
                        pausing = True
                        await asyncio.sleep(self._session_retry_pause)
                        # 쉬는 동안 턴의 위임 토큰이 끝났을 수 있으므로 보낼 곳을 다시 고른다
                        call = await self._route(ref, definition, inner)
                        pausing = False
            except TimeoutError:
                # ToolMessage 로 접히면 종류를 잃으므로 트레이서에 먼저 알린다(P37)
                await signal(TOOL_TIMEOUT, {"tool": tool.name})
                if pausing:
                    # 받아들여진 시도가 없다. 시도 도중에 시간이 다 됐으면 Tool 이 실행 중일 수
                    # 있으므로 이 문장을 쓰지 않는다
                    raise _ToolTimeout(
                        f"tool {tool.name} 의 도구 소스가 {seconds:g}초 동안 호출을 받지 않았다. "
                        "도구 소스가 다시 뜨는 중일 수 있다"
                    ) from None
                raise _ToolTimeout(
                    f"tool {tool.name} 가 {seconds:g}초 안에 답하지 않았다"
                ) from None
            except MCPError as e:
                # 도구 소스의 거절과 한도는 턴을 끝내지 않는다 (P52 5절). 그 밖의 오류는 그대로
                # 올린다. ToolMessage 로 접히면 종류를 잃으므로 트레이서에 먼저 알린다(P37)
                kind = refusal_kind(e)
                if kind is None:
                    raise
                log.warning(
                    "tools[%s] %s 호출을 도구 소스가 거절했다: %s", ref, tool.name, e.message
                )
                await signal(TOOL_REFUSED, {"tool": tool.name, "kind": kind})
                raise _ToolRefused(refusal_text(tool.name, kind)) from None

        def handle(error: ToolException) -> Any:
            if isinstance(error, (_ToolTimeout, _ToolRefused, TurnTokenUnavailable)):
                return str(error)
            if callable(upstream):
                return upstream(error)
            raise error

        tool.coroutine = run
        tool.handle_tool_error = handle
        return tool

    async def _route(
        self, ref: str, definition: Tool, inner: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        """이 호출을 보낼 코루틴. 위임 없는 턴이나 위임이 이 대상에 토큰을 싣지 않은 턴은 파드의
        것(inner)이다. 실었으면 그 턴의 클라이언트로 부르는 같은 도구의 것이고, 그 토큰이 끝났으면
        보내지 않는다(`TurnCalls.tool`). 누구의 턴인지 모르면 `TurnGrants.current` 가 막는다 (P53
        6절)."""
        calls = self.grants.current()
        target = self._targets[ref]
        if calls is None or not calls.credentials.holds(target):
            return inner
        source = self._sources[ref]
        return await calls.tool(
            ref,
            definition,
            target,
            build=lambda auth: build_client(source, auth),
            adapt=as_langchain_tool,
        )
