"""플랫폼 웹 Tool 의 안전 바닥 가운데 런타임의 몫이다(P39b 14절 2·3·6부, D64).

기능 `web_guard` 가 든 문서에만 붙는다. 문서는 최상위 `web_data_tools` 와 소스별 `tools[].web`
(fetch·search Tool 이름, 허용·막는 도메인, 사용자별 한도)을 싣는다.

- **가드(`WebFetchGuard`).** 메인과 웹 Tool 을 가진 서브에이전트마다 하나씩 선다. 우리
  서브에이전트는 isolated 라 메인의 미들웨어를 받지 않기 때문이다. 메인은 웹 Tool 이 없어도 서서
  사람의 URL 을 모은다. ToolGuard 와 인용 단계(`WebCitations`)의 바깥이라 호출 전에 판정하고,
  모델이 실제로 받은 마지막 ToolMessage 에서 URL 을 모은다. 막힌 결과(artifact 가 빈다)와 가려진
  URL 은 글에 없으므로 출처가 되지 못한다.
- **세 규칙.** fetch 의 URL 은 대화에 그대로 나온 것이어야 한다. 출처는 이 턴의 사람 메시지
  (`web_urls_human`, 메인이 `before_agent` 에서 모으고 줄이지 않는다), 웹 Tool 결과(`web_urls`,
  오래된 것부터 2000 개 넘게는 버린다)와 그 Tool 이 속한 소스의 허용 목록이다. 모델이 지은 URL 과
  서브에이전트가 받은 위임 글은 출처가 아니다. 호스트가 막는 목록에 있으면 거절한다. 그래프가 데이터
  Tool 을 가졌거나(`web_data_tools`) 대화가 첨부를 받았거나 웹훅 실행의 것이면
  (`TurnContext.holds_attachments`, P41 9절) 호스트가 허용 목록에 있어야 하고 빈 목록은 모두
  거절한다. 턴 문맥 없이 불린 호출은 첨부를 받은 것으로 본다.
- **한도.** 턴마다 그래프 전체에 fetch 다섯과 search 다섯이다(`WebBudget`, 서브에이전트도 같은
  문맥을 받으므로 위임으로 다시 차지 않는다). 자리는 기다리기 전에 잡고 거절되면 돌려주므로 한꺼번에
  부른 호출도 상한을 넘지 못한다. 턴 문맥이 없으면 웹 Tool 을 부르지 않는다. 세션 소유자마다 분·일
  한도를 공유 카운터 `<instance>:web:<tool>:<sub>:<창>` 으로 센다(memory 노드가 없으면 파드마다라
  느슨하다).
- **나머지 둘.** URL 의 뽑기·맞추기·도메인 규칙은 web_urls.py, 인용 번호와 외부 웹 표시는
  web_citations.py 다. 컴파일은 `web_plan` 으로 에이전트마다의 미들웨어를 받는다.

거절은 오류 ToolMessage `{code, message}` 이고 Tool 은 불리지 않는다. 문장은 우리가 쓴 것뿐이다.
거절마다 관측 신호 `web_guard{code, tool, host, reason}` 을 낸다(stdout 만, P39b 16절).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from runtime import signals
from runtime.config import AgentConfig, ConfigError
from runtime.counters import Counters, day_window, minute_window, web_key
from runtime.features import WEB_GUARD
from runtime.guards import effective_call, refusal_result
from runtime.sessions import utcnow
from runtime.web_citations import WEB_FETCH, WEB_SEARCH, WebCitations
from runtime.web_urls import (
    add_recent_urls,
    add_urls,
    domain_matches,
    extract_urls,
    host_of,
    normalize_url,
    result_urls,
)

WEB_URLS_HUMAN = "web_urls_human"
WEB_URLS = "web_urls"

TURN_CAPS = {WEB_FETCH: 5, WEB_SEARCH: 5}
"""한 턴에 그래프 전체가 부를 수 있는 웹 Tool 호출 수다."""

URL_NOT_IN_CONVERSATION = "url_not_in_conversation"
URL_BLOCKED = "url_blocked"
URL_NOT_ALLOWED = "url_not_allowed"
RATE_LIMITED = "rate_limited"
WEB_TURN_LIMIT = "web_turn_limit"
WEB_NO_TURN = "web_no_turn"
REASON_DATA_TOOLS = "data_tools"
REASON_ATTACHMENT = "attachment"

MESSAGES = {
    URL_NOT_IN_CONVERSATION: (
        "This URL did not appear in the conversation. Fetch only URLs the user wrote, URLs from "
        "web tool results, or hosts on the allow list."
    ),
    URL_BLOCKED: "This host is on a block list and is not fetched.",
    RATE_LIMITED: "The per-user limit for this web tool is used up. Try again later.",
    WEB_TURN_LIMIT: "This turn has used all its calls of this web tool. Answer with what you have.",
    WEB_NO_TURN: "Web tools run only inside a chat turn, so this call was not made.",
}
NOT_ALLOWED_MESSAGES = {
    REASON_DATA_TOOLS: (
        "This agent can reach company data, so it fetches only hosts on its allow list, and this "
        "host is not on it."
    ),
    REASON_ATTACHMENT: (
        "This conversation holds files the user attached, so it fetches only hosts on the allow "
        "list, and this host is not on it. The user can ask without attachments in a new "
        "conversation."
    ),
}

# --- 문서 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class WebSource:
    """웹 Tool 을 주는 소스 하나의 `tools[].web` 이다."""

    ref: str
    fetch: str | None
    search: str | None
    allow_domains: tuple[str, ...]
    block_domains: tuple[str, ...]
    per_user: Mapping[str, int]

    @classmethod
    def of(cls, source: Mapping[str, Any]) -> WebSource:
        web = source["web"]
        return cls(
            ref=source["ref"],
            fetch=web["fetch"],
            search=web["search"],
            allow_domains=tuple(web["allow_domains"]),
            block_domains=tuple(web["block_domains"]),
            per_user=dict(web["per_user"]),
        )

    def limits(self, kind: str) -> tuple[int, int]:
        """(분당, 일당) 사용자별 한도다."""
        return self.per_user[f"{kind}_per_minute"], self.per_user[f"{kind}_per_day"]


@dataclass(frozen=True)
class WebTool:
    kind: str
    source: WebSource


WebTools = dict[str, WebTool]
"""에이전트 하나의 웹 Tool 이름마다의 종류와 소스다."""


def web_sources(config: AgentConfig) -> dict[str, WebSource]:
    """기능 web_guard 가 든 문서의 웹 소스다. ref 마다 하나이고 기능이 없으면 비었다."""
    if WEB_GUARD not in config.features:
        return {}
    return {t["ref"]: WebSource.of(t) for t in config.tools if t.get("web")}


def web_tools_for(where: str, refs: Sequence[str], sources: Mapping[str, WebSource]) -> WebTools:
    """에이전트 하나가 쓰는 웹 Tool 이다. 가드가 Tool 이름으로 목록을 고르므로 한 에이전트의 두
    소스가 같은 이름을 주면 ConfigError(tool_name_conflict) 다."""
    out: WebTools = {}
    for ref in refs:
        source = sources.get(ref)
        if source is None:
            continue
        for kind, name in ((WEB_FETCH, source.fetch), (WEB_SEARCH, source.search)):
            if name is None:
                continue
            if name in out:
                raise ConfigError(
                    "tool_name_conflict", f"{where} 의 웹 Tool 이름 {name!r} 가 두 소스에 있다"
                )
            out[name] = WebTool(kind, source)
    return out


def attachments_limit_fetch(config: AgentConfig) -> bool:
    """`/metrics` 의 `chat.attachments_limit_fetch` 다. 그래프에 fetch 가 있고 데이터 Tool 이 없어
    첨부가 fetch 를 허용 목록으로 좁히는 문서면 참이다(채팅 입력이 첨부 때 그것을 알린다)."""
    sources = web_sources(config)
    has_fetch = any(source.fetch for source in sources.values())
    return has_fetch and not config.web_data_tools


# --- 상태와 턴 문맥 ---------------------------------------------------------------------------


class WebState(AgentState[Any]):
    """가드가 쓰는 상태 키다. 사적 키가 아니라 서브에이전트에 내려가고 돌아온다(deepagents
    0.7.20)."""

    web_urls_human: NotRequired[Annotated[list[str], add_urls]]
    web_urls: NotRequired[Annotated[list[str], add_recent_urls]]


@dataclass
class WebBudget:
    """한 턴의 웹 Tool 몫이다. 턴 상한을 세고, 사용자별 한도를 셀 공유 카운터와 인스턴스 id 를 든다.
    턴 문맥(`TurnContext.web`)에 실려 메인과 서브에이전트가 같은 것을 본다."""

    counters: Counters | None = None
    instance_id: str = ""
    used: dict[str, int] = field(default_factory=dict)

    def take(self, kind: str) -> bool:
        """이 턴에 kind 를 한 번 더 부를 자리를 잡는다. 자리가 없으면 거짓이다. 기다리기 전에 잡아야
        한꺼번에 부른 호출이 함께 상한을 넘지 않는다."""
        if self.used.get(kind, 0) >= TURN_CAPS[kind]:
            return False
        self.used[kind] = self.used.get(kind, 0) + 1
        return True

    def release(self, kind: str) -> None:
        """잡은 자리를 돌려준다. 그 호출이 거절됐을 때다."""
        self.used[kind] = max(0, self.used.get(kind, 0) - 1)


# --- 가드 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    host: str | None = None
    reason: str | None = None
    retry_after: int | None = None


def _context(request: ToolCallRequest) -> Any:
    return getattr(request.runtime, "context", None)


def _refused(call: Mapping[str, Any], refusal: Refusal) -> ToolMessage:
    return refusal_result(call, refusal.code, refusal.message, retry_after=refusal.retry_after)


class WebFetchGuard(AgentMiddleware[WebState]):
    """웹 Tool 호출의 출처·목록·한도를 보고, 받은 결과에서 출처 URL 을 모은다.

    main 이면 `before_agent` 가 이 턴의 사람 메시지에서 URL 을 모은다. 서브에이전트의 첫 사람
    메시지는 모델이 쓴 위임 글이라 읽지 않는다.
    """

    state_schema = WebState

    def __init__(self, tools: WebTools, *, data_tools: bool, main: bool) -> None:
        super().__init__()
        self.web_tools = dict(tools)
        self.data_tools = data_tools
        self.main = main

    @property
    def name(self) -> str:
        return "DaegamWebFetchGuard"

    async def abefore_agent(self, state: WebState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        if not self.main:
            return None
        human = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        if human is None:
            return None
        known = set(state.get(WEB_URLS_HUMAN) or [])
        urls = [u for raw in extract_urls(human.text) if (u := normalize_url(raw)) is not None]
        fresh = [u for u in add_urls([], urls) if u not in known]
        return {WEB_URLS_HUMAN: fresh} if fresh else None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = effective_call(request)
        tool = self.web_tools.get(call["name"])
        if tool is None:
            return await handler(request)
        refusal = await self._check(call, tool, request)
        if refusal is not None:
            signals.emit(
                signals.WEB_GUARD,
                code=refusal.code,
                tool=call["name"],
                host=refusal.host,
                reason=refusal.reason,
            )
            return _refused(call, refusal)
        result = await handler(request)
        if not isinstance(result, ToolMessage) or result.status == "error":
            return result
        state = request.state if isinstance(request.state, Mapping) else {}
        known = set(state.get(WEB_URLS) or [])
        fresh = [u for u in result_urls(result) if u not in known]
        if not fresh:
            return result
        return Command(update={"messages": [result], WEB_URLS: fresh})

    async def _check(
        self, call: Mapping[str, Any], tool: WebTool, request: ToolCallRequest
    ) -> Refusal | None:
        """URL 규칙, 턴 상한, 사용자별 한도 차례다. 앞에서 거절되면 뒤의 셈은 늘지 않는다."""
        context = _context(request)
        if tool.kind == WEB_FETCH:
            refusal = self._check_url(call, tool.source, request.state, context)
            if refusal is not None:
                return refusal
        budget = getattr(context, "web", None)
        # 턴 문맥이 없으면 턴 상한을 셀 수 없으므로 부르지 않는다
        if not isinstance(budget, WebBudget):
            return Refusal(WEB_NO_TURN, MESSAGES[WEB_NO_TURN])
        if not budget.take(tool.kind):
            return Refusal(WEB_TURN_LIMIT, MESSAGES[WEB_TURN_LIMIT])
        refusal = await self._check_limits(call["name"], tool, budget, context)
        if refusal is not None:
            budget.release(tool.kind)
        return refusal

    def _check_url(
        self, call: Mapping[str, Any], source: WebSource, state: Any, context: Any
    ) -> Refusal | None:
        raw = (call.get("args") or {}).get("url")
        url = normalize_url(raw) if isinstance(raw, str) else None
        if url is None:
            return Refusal(URL_NOT_IN_CONVERSATION, MESSAGES[URL_NOT_IN_CONVERSATION])
        host = host_of(url)
        if domain_matches(host, source.block_domains):
            return Refusal(URL_BLOCKED, MESSAGES[URL_BLOCKED], host=host)
        allowed = domain_matches(host, source.allow_domains)
        reason = self._allow_list_reason(context)
        if reason is not None and not allowed:
            return Refusal(URL_NOT_ALLOWED, NOT_ALLOWED_MESSAGES[reason], host=host, reason=reason)
        state = state if isinstance(state, Mapping) else {}
        origins = {*(state.get(WEB_URLS_HUMAN) or []), *(state.get(WEB_URLS) or [])}
        if not allowed and url not in origins:
            return Refusal(URL_NOT_IN_CONVERSATION, MESSAGES[URL_NOT_IN_CONVERSATION], host=host)
        return None

    def _allow_list_reason(self, context: Any) -> str | None:
        """허용 목록이 필요한 까닭이다. 데이터 Tool 이 먼저다. 턴 문맥이 없으면 첨부를 받은 것으로
        본다."""
        if self.data_tools:
            return REASON_DATA_TOOLS
        if getattr(context, "holds_attachments", True):
            return REASON_ATTACHMENT
        return None

    async def _check_limits(
        self, name: str, tool: WebTool, budget: WebBudget, context: Any
    ) -> Refusal | None:
        """세션 소유자의 분·일 한도다. 카운터나 소유자를 모르면 세지 않는다."""
        owner = getattr(context, "owner_sub", None)
        if budget.counters is None or not owner:
            return None
        now = utcnow()
        for window, limit in zip(
            (minute_window(now), day_window(now)), tool.source.limits(tool.kind), strict=True
        ):
            key = web_key(budget.instance_id, name, owner, window)
            if await budget.counters.incr(key, 1, window=window) > limit:
                return Refusal(
                    RATE_LIMITED, MESSAGES[RATE_LIMITED], retry_after=window.retry_after(now)
                )
        return None


@dataclass(frozen=True)
class WebLayers:
    """에이전트 하나에 거는 웹 미들웨어다. 가드는 ToolGuard 바깥(`outer`)에, 인용 단계는 그
    안쪽(`inner`)에 선다. 메인은 웹 Tool 이 없어도 가드를 둔다. 사람의 URL 을 모으기 때문이다."""

    outer: tuple[AgentMiddleware, ...] = ()
    inner: tuple[AgentMiddleware, ...] = ()


NO_LAYERS = WebLayers()


@dataclass(frozen=True)
class WebPlan:
    """문서 하나의 웹 미들웨어 계획이다. 기능 web_guard 가 없으면 아무 에이전트에도 걸지 않는다."""

    sources: Mapping[str, WebSource]
    data_tools: bool
    guarded: bool

    def layers(self, where: str, refs: Sequence[str], *, main: bool) -> WebLayers:
        """where 에이전트가 refs 소스로 받는 미들웨어다. 메인은 웹 Tool 이 없어도 가드를 둔다."""
        if not self.guarded:
            return NO_LAYERS
        tools = web_tools_for(where, refs, self.sources)
        return web_layers(tools, data_tools=self.data_tools, main=main)


def web_plan(config: AgentConfig) -> WebPlan:
    return WebPlan(
        sources=web_sources(config),
        data_tools=config.web_data_tools,
        guarded=WEB_GUARD in config.features,
    )


def web_layers(tools: WebTools, *, data_tools: bool, main: bool) -> WebLayers:
    if not tools and not main:
        return NO_LAYERS
    guard = WebFetchGuard(tools, data_tools=data_tools, main=main)
    return WebLayers(outer=(guard,), inner=(WebCitations(tools),) if tools else ())
