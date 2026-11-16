"""LLM 호출의 전송 규칙 (P37 2절, D62).

플랫폼(`platform/daegam/llm_http.py`)과 에이전트 런타임(`agent-runtime/runtime/llm_http.py`)이 이
파일을 바이트 그대로 나눠 가진다. `auth/oidc.py` 와 같은 방식이고 `test_llm_http_copy.py` 가
지킨다. 그래서 `daegam` 도 `runtime` 도 import 하지 않는다. 한쪽을 고치면 다른 쪽에 그대로 복사한다.

- 호출 id 와 상관 헤더. 모델·임베딩 요청마다 W3C `traceparent`(`00-<trace_id>-<호출 span_id>-01`)를
  싣는다. SDK 가 쓰는 httpx2 클라이언트의 요청 훅이 ContextVar 에 든 호출 id 를 읽는다. 호출 id 는
  되돌리지 않고 다음 호출이 덮는다. 한 id 는 HTTP 요청 하나라 훅은 id 마다 헤더를 한 번만 싣고,
  같은 id 의 둘째 요청은 헤더 없이 보내며 `correlation_reuse` 로 센다
- `HookedChatAnthropic`. ChatAnthropic 에는 http 클라이언트 인자가 없어 그 클라이언트를 만드는
  자리만 바꾼다. anthropic SDK 는 `httpx2.AsyncClient` 가 아닌 비동기 클라이언트를 거절한다.
  SDK 2.x 는 API 판 `2026-10-21` 을 보내고 키를 `Authorization: Bearer` 로 싣는다. SDK 의
  OpenTelemetry 는 끈다. 켜 두면 SDK 가 지금 문맥의 span 을 `traceparent` 로 실어서, 훅이 헤더를
  싣지 않는 요청(같은 id 의 둘째 요청)에 우리 것이 아닌 id 가 간다
- `usage_from_metadata`. langchain `usage_metadata` 의 유일한 독자다. input 은 cache 읽기·쓰기를,
  output 은 reasoning 을 품는다(langchain 과 GenAI 의 정의)
- 글자 수 추정. 사용량을 받지 못한 호출은 글자 수를 4로 나눠 올린 값을 텍스트마다 더한다
- 다시 보내기 규칙. SDK 가 다시 보내던 것(연결 오류와 시간 초과, 408, 409, 429, 5xx)을 다시 보내고
  크레딧 소진(T20)은 429 여도 다시 보내지 않는다. 기다려도 풀리지 않기 때문이다
- 게이트웨이 경로 (P38 5절·7절·10절). 게이트웨이가 스스로 낸 답(한도의 429, 토큰 거절의 401·403)은
  `gateway_error` 가 세 코드로 가르고 다시 보내지 않는다. 1분 창이라 1·2초 뒤의 답도 같기 때문이다.
  게이트웨이 경로의 클라이언트는 연결을 5초 안에 맺지 못하면 끝낸다(`gateway_timeout`). 플랫폼의
  호출자는 요청마다 토큰을 받는 `bearer` 를 준다. 그 훅이 `Authorization` 을 덮고 `x-api-key` 를
  뺀다
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property, lru_cache
from typing import Any

import anthropic
import httpx2
import openai
from langchain_anthropic import ChatAnthropic

# -- 호출 id ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallId:
    """호출 하나의 W3C Trace Context id. 원장의 `_id` 는 `<trace_id>-<span_id>` 다."""

    trace_id: str
    span_id: str

    @property
    def id(self) -> str:
        return f"{self.trace_id}-{self.span_id}"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


_CALL: ContextVar[CallId | None] = ContextVar("daegam_llm_call", default=None)
# W3C traceparent: 판-trace id-span id-깃발. id 는 소문자 16진수다
TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def parse_traceparent(value: object) -> CallId | None:
    """`traceparent` 글을 호출 id 로 읽는다. 꼴이 맞지 않으면 None 이다. 게이트웨이가 옮겨 실은
    우리 헤더(`daegam.traceparent`)를 사용량 수집이 읽는다."""
    match = TRACEPARENT_RE.match(value) if isinstance(value, str) else None
    return None if match is None else CallId(match[1], match[2])


def new_trace_id() -> str:
    """소문자 16진수 32자. W3C 는 모두 0 인 id 를 받지 않지만 128비트 난수가 그럴 일은 없다."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def set_call(trace_id: str, span_id: str | None = None) -> CallId:
    """이 문맥의 다음 요청이 실을 호출 id 를 둔다. 되돌리지 않는다. 다음 호출이 덮는다."""
    call = CallId(trace_id, span_id or new_span_id())
    _CALL.set(call)
    return call


def current_call() -> CallId | None:
    return _CALL.get()


@contextmanager
def call_scope(trace_id: str | None = None) -> Iterator[CallId]:
    """콜백이 없는 호출(임베딩 한 번)이 요청 앞에 호출 id 를 두는 자리. 끝나면 되돌린다."""
    token = _CALL.set(CallId(trace_id or new_trace_id(), new_span_id()))
    try:
        yield _CALL.get()  # type: ignore[misc]
    finally:
        _CALL.reset(token)


# -- 상관 헤더 ---------------------------------------------------------------------------------

TRACEPARENT = "traceparent"
# 클라이언트의 기본값. SDK 의 기본 클라이언트와 같은 값이다. SDK 는 요청마다 자기 타임아웃을 준다
DEFAULT_TIMEOUT = 600.0
# 헤더를 실은 호출 id 를 기억하는 수. 다시 쓰인 id 를 알아보는 데만 쓰므로 오래 들고 있지 않는다
SEEN_CALLS = 4096
# 공유 클라이언트의 수. (주소, 시간 제한)마다 하나다. 턴·문서·작업마다 만들면 연결 풀이 쌓인다
SHARED_CLIENTS = 64


class CorrelationHook:
    """httpx2 요청 훅. 지금 문맥의 호출 id 를 id 마다 한 번 `traceparent` 로 싣는다."""

    def __init__(self) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.correlation_reuse = 0

    async def __call__(self, request: httpx2.Request) -> None:
        call = _CALL.get()
        if call is None:
            return
        if call.id in self._seen:
            # 같은 id 의 둘째 요청(놓친 SDK 재시도, 낡은 id)이다. 그 게이트웨이 기록은 잇지 못한
            # `gw:` 행이 되어 잃지도 두 번 세지도 않는다
            self.correlation_reuse += 1
            return
        self._seen[call.id] = None
        if len(self._seen) > SEEN_CALLS:
            self._seen.popitem(last=False)
        request.headers[TRACEPARENT] = call.traceparent


CORRELATION = CorrelationHook()


class BearerUnavailable(Exception):
    """토큰 훅이 토큰을 받지 못했다. SDK 는 요청 중의 예외를 연결 실패로 감싸므로 이 예외가 그
    원인(`__cause__`)에 남고, 분류가 그것을 보고 `no_signer` 로 가른다(`gateway_error`)."""


class BearerHook:
    """게이트웨이 경로의 요청 훅 (P38 5절). 요청마다 토큰을 받아 `Authorization` 에 싣고
    `x-api-key` 를 뺀다. 플랫폼의 5분 토큰은 300초 호출을 몇 번이고 다시 하는 작업보다 먼저 끝나므로
    모델을 만들 때가 아니라 요청마다 받는다. SDK 에는 자리표시 키를 준다. 두 SDK 는 그 키를
    `Authorization: Bearer` 로 싣고 훅이 그 값을 토큰으로 덮는다. `x-api-key` 는 SDK 가 싣지 않지만
    설정(`default_headers`, anthropic 의 `ANTHROPIC_CUSTOM_HEADERS`)이 실을 수 있어 뺀다. 토큰은
    base(게이트웨이의 provider 경로) 아래로 가는 요청에만 싣는다."""

    def __init__(self, bearer: Callable[[], str], base: str) -> None:
        self.bearer = bearer
        self.base = httpx2.URL(base)

    async def __call__(self, request: httpx2.Request) -> None:
        if not under_url(request.url, self.base):
            return
        try:
            token = self.bearer()
        except Exception as e:
            raise BearerUnavailable(f"no llm gateway token: {e}") from e
        request.headers["Authorization"] = f"Bearer {token}"
        request.headers.pop("x-api-key", None)


def under_url(url: httpx2.URL, base: httpx2.URL) -> bool:
    """url 이 base 와 같은 출처(scheme·host·port)이고 그 경로 아래인가. 경로는 조각 단위로 본다.
    `/p/a` 는 `/p/ab` 를 덮지 않는다."""
    if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
        return False
    root = base.path.rstrip("/")
    return url.path == root or url.path.startswith(f"{root}/")


def hooked_async_client(
    client: httpx2.AsyncClient | None = None,
    *,
    base_url: str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    bearer: Callable[[], str] | None = None,
) -> httpx2.AsyncClient:
    """상관 헤더 훅을 단 클라이언트. client 를 주면 그것에 훅을 달아 돌려주고(테스트의 목
    transport), 아니면 (base_url, timeout) 마다 하나인 공유 클라이언트다. 클라이언트는 SDK 의
    재시도를 끈 호출자가 쓴다. 한 id 가 요청 하나여야 하기 때문이다. bearer 를 주면 그 토큰 훅도
    단다. 그때 base_url 은 토큰을 실을 게이트웨이 경로라 꼭 준다. 훅이 호출자마다 달라 공유
    클라이언트 대신 연결 풀 하나를 함께 쓰는 클라이언트다."""
    if bearer is not None and not base_url:
        raise ValueError("a bearer client needs the gateway base_url")
    if client is not None:
        return _with_hook(client, bearer, base_url)
    if bearer is not None:
        assert base_url is not None
        return _bearer_client(base_url, timeout, bearer)
    return _shared_client(base_url, timeout)


@lru_cache(maxsize=SHARED_CLIENTS)
def _shared_client(base_url: str | None, timeout: float | None) -> httpx2.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if base_url is not None:
        kwargs["base_url"] = base_url
    return _with_hook(httpx2.AsyncClient(**kwargs))


@lru_cache(maxsize=1)
def _gateway_transport() -> httpx2.AsyncHTTPTransport:
    """토큰 훅을 단 클라이언트가 함께 쓰는 연결 풀. 게이트웨이는 클러스터 안 주소라 env 의 프록시를
    쓰지 않는다."""
    return httpx2.AsyncHTTPTransport()


def _bearer_client(
    base_url: str, timeout: float | None, bearer: Callable[[], str]
) -> httpx2.AsyncClient:
    """게이트웨이는 리디렉트를 내지 않는다. 따라가지 않아야 훅이 다른 곳으로 가는 요청에 토큰을
    싣지 않는다. httpx 는 출처가 바뀌면 `Authorization` 을 빼지만 요청 훅은 매 걸음 다시 돈다."""
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False, "trust_env": False}
    client = httpx2.AsyncClient(transport=_gateway_transport(), base_url=base_url, **kwargs)
    return _with_hook(client, bearer, base_url)


def _with_hook(
    client: httpx2.AsyncClient,
    bearer: Callable[[], str] | None = None,
    base: str | None = None,
) -> httpx2.AsyncClient:
    hooks = client.event_hooks
    added = [] if CORRELATION in hooks["request"] else [CORRELATION]
    if bearer is not None:
        assert base is not None
        client.follow_redirects = False
        added.append(BearerHook(bearer, base))
    if added:
        client.event_hooks = {**hooks, "request": [*hooks["request"], *added]}
    return client


def has_correlation_hook(client: Any) -> bool:
    hooks = getattr(client, "event_hooks", None)
    return isinstance(hooks, dict) and CORRELATION in hooks.get("request", ())


def lacks_correlation_hook(model: Any) -> bool:
    """LangChain 채팅 모델의 SDK 비동기 HTTP 클라이언트에 상관 헤더 훅이 없다. OpenAI·Anthropic
    모델이 아니면(스텁처럼 HTTP 를 쓰지 않는 모델) False 다."""
    from langchain_openai import ChatOpenAI

    if isinstance(model, ChatOpenAI):
        client = model.http_async_client
    elif isinstance(model, ChatAnthropic):
        client = model._async_client._client
    else:
        return False
    return not has_correlation_hook(client)


class HookedChatAnthropic(ChatAnthropic):
    """비동기 클라이언트만 훅 단 클라이언트로 바꾼 ChatAnthropic. `http_client` 는 테스트의 목
    transport 를 끼우는 자리다. 키는 `api_key` 하나이고 SDK 가 `Authorization: Bearer` 로 싣는다.
    두 게이트웨이 경로(P38 5절)는 연결 제한 `connect_timeout` 을 준다. 런타임은 90일 토큰을 키
    자리에 주고, 플랫폼은 자리표시 키와 요청마다 토큰을 받는 `bearer`(`BearerHook`)를 준다."""

    http_client: Any = None
    bearer: Any = None
    connect_timeout: float | None = None

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        params = dict(self._client_params)
        http = hooked_async_client(
            self.http_client,
            base_url=params["base_url"],
            timeout=params.get("timeout", DEFAULT_TIMEOUT),
            bearer=self.bearer,
        )
        if self.connect_timeout is not None:
            params["timeout"] = httpx2.Timeout(params.get("timeout"), connect=self.connect_timeout)
        return anthropic.AsyncClient(**params, http_client=http, open_telemetry=False)


# -- 사용량 ---------------------------------------------------------------------------------

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
# Anthropic 의 cache 쓰기는 일반 키 하나이거나 TTL 별 키 둘이다. langchain-anthropic 은 TTL 별 키가
# 있으면 일반 키를 0 으로 둔다
CACHE_WRITE_DETAILS = ("cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def _detail(details: Any, key: str) -> int:
    value = details.get(key) if isinstance(details, Mapping) else None
    return int(value) if isinstance(value, int | float) else 0


def usage_from_metadata(meta: Mapping[str, Any] | None) -> dict[str, int]:
    """langchain `usage_metadata` 를 다섯 수로. 없으면 모두 0 이다."""
    if not meta:
        return dict.fromkeys(USAGE_KEYS, 0)
    inputs = meta.get("input_token_details")
    outputs = meta.get("output_token_details")
    return {
        "input_tokens": int(meta.get("input_tokens") or 0),
        "output_tokens": int(meta.get("output_tokens") or 0),
        "cache_read_tokens": _detail(inputs, "cache_read"),
        "cache_write_tokens": sum(_detail(inputs, k) for k in CACHE_WRITE_DETAILS),
        "reasoning_tokens": _detail(outputs, "reasoning"),
    }


# 사용량을 받지 못한 호출의 추정. 글자 4자에 토큰 하나로 본다
CHARS_PER_TOKEN = 4


def estimate_tokens(texts: Sequence[str]) -> int:
    """글자 수를 4로 나눠 올린 값을 텍스트마다 더한다. 추정은 `estimated` 로 표시한다."""
    return sum(math.ceil(len(t) / CHARS_PER_TOKEN) for t in texts)


# -- 다시 보내기 -------------------------------------------------------------------------------

# 다시 보내도 되는 4xx. 나머지 4xx 는 같은 요청이면 같은 답이다
RETRY_4XX = frozenset({408, 409, 429})
MAX_RETRY_AFTER = 10.0
BASE_DELAY = 1.0


def retryable_status(status: Any) -> bool:
    """같은 요청을 다시 보내도 되는 응답 상태인가. 5xx 는 범위다. 업체가 5xx 를 늘리기 때문이다
    (Anthropic 의 529)."""
    return isinstance(status, int) and (status in RETRY_4XX or status >= 500)


def not_retried(exc: BaseException, *, gateway: bool = False) -> bool:
    """기다려도 같은 답이라 다시 보내지 않는 실패인가. 크레딧 소진(T20), 토큰을 받지 못한
    게이트웨이 호출, 게이트웨이 경로에서 게이트웨이가 스스로 낸 거절(P38 7절)이다. 상태가 429 여도
    그렇다. 직접 경로의 provider 가 낸 빈 429 는 게이트웨이의 표시와 꼴이 같아도 다시 보낸다."""
    if quota_exhausted(exc) or _caused_by(exc, BearerUnavailable):
        return True
    return gateway and gateway_answer(exc) is not None


def is_retryable(exc: BaseException, *, gateway: bool = False) -> bool:
    """다시 보내도 되는 실패인가. 크레딧 소진과 게이트웨이의 거절은 상태와 무관하게 아니다."""
    if not_retried(exc, gateway=gateway):
        return False
    if isinstance(exc, openai.APIConnectionError | anthropic.APIConnectionError):
        return True
    if isinstance(exc, httpx2.TransportError):
        return True
    return retryable_status(getattr(exc, "status_code", None))


def retry_delay(exc: BaseException | None = None) -> float:
    """다시 보내기 전 기다릴 초. `Retry-After` 가 있으면 그것을 10초까지 따르고, 없으면 1초에
    흔들림을 더한다."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if raw is not None:
        try:
            return min(MAX_RETRY_AFTER, max(0.0, float(raw)))
        except ValueError:
            pass
    return BASE_DELAY + random.uniform(0, BASE_DELAY / 2)


async def retry_once[T](
    attempt: Callable[[int], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    gateway: bool = False,
) -> T:
    """attempt(1) 을 부르고, 다시 보내도 되는 실패(`is_retryable`)면 `retry_delay` 만큼 기다려
    attempt(2) 를 한 번 더 부른다. 그래도 실패하면 그 예외가 그대로 올라간다. 시도마다 호출 id 를
    따로 두는 것은 attempt 의 몫이다. 런타임 Knowledge 검색과 플랫폼 인덱서의 임베딩이 쓴다.
    gateway 는 호출이 게이트웨이 경로인가다."""
    try:
        return await attempt(1)
    except Exception as e:
        if not is_retryable(e, gateway=gateway):
            raise
        await sleep(retry_delay(e))
    return await attempt(2)


# -- 크레딧 소진 (T20) --------------------------------------------------------------------------

# provider 의 크레딧·사용 한도가 바닥났다는 표시다. Z.AI 는 429 에 숫자 문자열 코드를 싣는다.
# 1113 은 잔액 부족, 1309·1314 는 구독 패키지 만료다
QUOTA_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted", "1113", "1309", "1314"})
QUOTA_ERROR_TYPES = frozenset({"billing_error", "insufficient_quota"})
PAYMENT_REQUIRED = 402
QUOTA_TEXTS = (
    "credit balance is too low",
    "key limit exceeded",
    "budget limit exceeded",
    "reached your specified api usage limits",
)


def quota_exhausted(exc: BaseException) -> bool:
    """provider 의 크레딧을 다 썼거나 사용 한도에 도달한 오류인가. 예외 타입을 묻지 않고
    status_code·body 속성(OpenAI 는 code 도)을 읽는다. SDK 예외와 그것을 감싼 langchain 예외가
    같은 속성을 가지기 때문이다."""
    if getattr(exc, "status_code", None) == PAYMENT_REQUIRED:
        return True
    if _one_of(getattr(exc, "code", None), QUOTA_CODES):
        return True
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body["error"] if isinstance(body.get("error"), dict) else body
    if _one_of(error.get("type"), QUOTA_ERROR_TYPES) or _one_of(error.get("code"), QUOTA_CODES):
        return True
    message = str(error.get("message") or "").lower()
    return any(text in message for text in QUOTA_TEXTS)


def _one_of(value: object, names: frozenset[str]) -> bool:
    """문자열이면서 names 가운데 하나인가. 업체가 dict 같은 값을 실어도 판정이 예외를 내지
    않는다."""
    return isinstance(value, str) and value in names


# -- 게이트웨이의 답 (P38 7절·10절) ------------------------------------------------------------

GATEWAY_RATE_LIMITED = "llm_gateway_rate_limited"
GATEWAY_DENIED = "llm_gateway_denied"
GATEWAY_UNREACHABLE = "llm_gateway_unreachable"
# 토큰 훅이 토큰을 받지 못한 게이트웨이 호출이다. 연결 실패가 아니라 다시 보내지 않는다
NO_SIGNER = "no_signer"
# 게이트웨이 경로의 연결 제한(초). 파드가 없는 게이트웨이를 부른 호출이 이만큼만 기다린다 (10절)
GATEWAY_CONNECT_SECONDS = 5.0
# 게이트웨이가 토큰을 거절할 때의 text/plain 본문 앞머리다. provider 의 오류는 JSON 이라 겹치지
# 않는다
DENIED_TEXTS = ("authentication failure", "authorization failed")
RATELIMIT_REMAINING = "x-ratelimit-remaining"


def gateway_timeout(total: float | None = None) -> httpx2.Timeout:
    """게이트웨이 경로 SDK 의 시간 제한. 연결만 5초이고 나머지는 total 이다. SDK 가 요청마다 이
    값을 넘기므로 클라이언트가 아니라 SDK 에 준다."""
    return httpx2.Timeout(total, connect=GATEWAY_CONNECT_SECONDS)


def gateway_answer(exc: BaseException) -> str | None:
    """게이트웨이가 스스로 낸 답이면 그 코드다. 크레딧 소진을 먼저 본다. 게이트웨이는 provider 의
    오류를 상태와 JSON 본문 그대로 넘기므로 본문의 꼴로 가른다. 빈 본문과
    `x-ratelimit-remaining: 0` 의 429 는 한도, text/plain 거절 문구의 401·403 은 토큰 거절이다.
    본문이 있는 429 는 provider 의 것이다."""
    if quota_exhausted(exc):
        return None
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not isinstance(status, int) or headers is None:
        return None
    body = getattr(exc, "body", None)
    if status == 429 and body in (None, "") and headers.get(RATELIMIT_REMAINING) == "0":
        return GATEWAY_RATE_LIMITED
    plain = "text/plain" in (headers.get("content-type") or "")
    if status in (401, 403) and plain and isinstance(body, str) and body.startswith(DENIED_TEXTS):
        return GATEWAY_DENIED
    return None


def gateway_error(exc: BaseException, *, gateway: bool) -> str | None:
    """게이트웨이 경로 호출의 실패 코드다. 차례는 크레딧 소진(코드 없음), 게이트웨이의 표시, 토큰을
    받지 못함(`no_signer`), 연결 실패(`llm_gateway_unreachable`)이고, 그 밖은 provider 의 코드라
    None 이다. 직접 경로는 늘 None 이다."""
    if not gateway or quota_exhausted(exc):
        return None
    if (code := gateway_answer(exc)) is not None:
        return code
    if _caused_by(exc, BearerUnavailable):
        return NO_SIGNER
    connection = openai.APIConnectionError | anthropic.APIConnectionError | httpx2.TransportError
    return GATEWAY_UNREACHABLE if isinstance(exc, connection) else None


def _caused_by(exc: BaseException, cls: type[BaseException]) -> bool:
    """exc 나 그 원인 사슬(`__cause__`, `__context__`)에 cls 가 있는가."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, cls):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False
