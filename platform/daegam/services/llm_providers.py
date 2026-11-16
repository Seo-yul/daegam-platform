"""LLM provider 추상화. 계약은 최소다.

스펙 생성기·비서·Agent 프로필·Knowledge 임베딩이 같이 쓴다.

    complete(system, messages, max_tokens) -> (text, usage)

호출은 langchain 채팅 모델(ChatOpenAI·ChatAnthropic)로 한다. Agent 런타임이 같은 라이브러리로
같은 모델을 부르므로 두 평면이 어긋나지 않고, 모델 계열별 인자 제약을 우리가 이름으로 분기하지
않아도 된다. langchain-openai 는 `max_tokens` 를 `max_completion_tokens` 로 바꾸고 gpt-5 계열
(chat 변형과 effort none 제외)에서 기본값 아닌 temperature 를 뗀다. 그 처리는 모델 생성 시점에만
일어나므로 temperature 는 생성자 인자이지 호출 인자가 아니다.

구조화 출력은 provider 가 아니라 파서와 verify() 가 맡는다. OpenAPI 3.x 문서는 재귀적이고
additionalProperties 가 열려 있어 OpenAI `json_schema` strict 모드로 표현할 수 없으므로
openai 는 `response_format: json_object` 로 JSON 만 강제하고, anthropic 도 텍스트 JSON 을 받는다.
두 구현이 같은 계약을 지키므로 테스트용 가짜는 하나면 된다.

langchain 이 올리는 예외는 하부 SDK 예외를 상속하므로(OpenAIRateLimitError <- openai.RateLimitError)
아래 except 절은 SDK 타입 그대로 둔다. 연결 확인(`check_provider`)만 SDK 를 직접 쓴다. langchain 에
`models.list` 대응이 없고, 확인은 채팅을 돌리지 않고 키만 보는 용도이기 때문이다.

키는 플랫폼 네임스페이스의 K8s Secret(`api_key_secret`) 에서만 온다(D25). job 시작 시
`SecretStore.get` 으로 읽어 메모리에만 두고, 다른 경로(파드 env 등)는 없다. `api_key_secret` 이
비어 있거나 Secret 에 키가 없으면 `ProviderError(no_api_key)`, Secret 자체가 없으면 `no_secret`
이다. 주소와 키는 `services/llm_endpoint.py` 의 `Endpoint` 로 받는다. 게이트웨이 경로(P38 6절)면
주소가 게이트웨이 경로이고 키 자리는 자리표시이며 요청마다 토큰을 받는 `bearer` 가 붙는다. 그
경로의 실패는 게이트웨이의 세 코드로도 가른다(P38 7절, `llm_http.gateway_error`).
`check_provider` 는 게이트웨이를 켜도 키를 Secret 에서 찾은 뒤 provider 에 바로 `models.list` 를
한 번 불러 연결까지 확인하고, OpenRouter 와 OpenAI 자체는 크레딧이 남았는지도 본다(T20).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import anthropic
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from starlette.concurrency import run_in_threadpool

from daegam import llm_http
from daegam.names import provider_slug
from daegam.secrets import SecretStore
from daegam.store.settings import LLMProvider

if TYPE_CHECKING:
    from daegam.services.llm_endpoint import Endpoint

Usage = dict[str, int]  # {"input_tokens": n, "output_tokens": m}
Message = dict[str, str]  # {"role": "user" | "assistant", "content": str}

# Secret 안에서 키 값을 담는 항목 이름. kind 별 옛 이름(env 이름 그대로 넣은 Secret)도 읽는다.
SECRET_KEY = "api_key"
# 화면에서 받은 키를 플랫폼이 넣어 두는 Secret 의 이름 앞머리 (D56). 이 앞머리의 Secret 만 플랫폼이
# 만들고 지운다. 운영자가 직접 만든 Secret(`api_key_secret` 에 이름만 적은 것)은 건드리지 않는다
MANAGED_SECRET_PREFIX = "daegam-llm-key-"
ALT_SECRET_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
# 연결 확인은 짧게 끝나야 하므로 job 용 타임아웃(300초)과 따로 둔다
CHECK_TIMEOUT = 20.0
# provider 가 요청 한도로 429 를 준 오류 코드다. 잠시 뒤 다시 보내면 되는 것이라 재시도한다.
# 에이전트 런타임의 `rate_limited`(사용자별 분당 한도, 공개 API 코드)와 뜻이 달라 이름을 가른다
PROVIDER_RATE_LIMITED = "provider_rate_limited"
# 크레딧 소진(T20)의 표와 판정 `quota_exhausted` 는 llm_http.py 에 있다. 런타임과 바이트 그대로
# 나눠 가지는 파일이다
# 연결 확인이 OpenAI 의 크레딧을 보려고 부르는 가장 싼 호출이다. 모델 목록은 크레딧이 없어도
# 200 이다
QUOTA_PROBE_MODEL = "text-embedding-3-small"
OPENAI_HOST = "api.openai.com"


def managed_secret_name(provider_name: str) -> str:
    """provider 이름 → 플랫폼이 관리하는 키 Secret 이름 (D56). slug 규칙은 `names.provider_slug`
    하나라 런타임 파드의 키 env 이름(P35 12절)과 같은 글자에서 나온다."""
    return MANAGED_SECRET_PREFIX + provider_slug(provider_name)


def is_managed_secret(name: str | None) -> bool:
    """플랫폼이 만든 키 Secret 인가. 이름 앞머리로만 판정한다."""
    return bool(name) and str(name).startswith(MANAGED_SECRET_PREFIX)


class ProviderError(Exception):
    """provider 호출 실패. retryable 이면 잠시 뒤 같은 요청을 다시 보낼 수 있다."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable


@runtime_checkable
class SpecProvider(Protocol):
    name: str
    model: str

    async def complete(
        self, system: str, messages: list[Message], *, max_tokens: int
    ) -> tuple[str, Usage]: ...


# -- 답 읽기 -------------------------------------------------------------------

# 언어 꼬리표가 있든 없든 답 전체를 감싼 코드 펜스
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def json_object(text: str) -> dict[str, Any]:
    """LLM 의 답에서 JSON 객체 하나를 꺼낸다. 코드 펜스는 벗기고, 앞뒤에 말이 붙었으면 첫 '{' 부터
    마지막 '}' 까지 한 번 더 읽는다. 읽지 못하거나 객체가 아니면 그 까닭의 `ValueError` 다. 스펙
    생성기(`spec_generator/loop.py` `parse_draft`)와 평가의 채점 모델이 함께 쓴다."""
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1).strip()
    if not body:
        raise ValueError("empty response")
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"not JSON: {e.msg} at line {e.lineno}") from e
        try:
            doc = json.loads(body[start : end + 1])
        except json.JSONDecodeError as e2:
            raise ValueError(f"not JSON: {e2.msg} at line {e2.lineno}") from e2
    if not isinstance(doc, dict):
        raise ValueError(f"expected a JSON object, got {type(doc).__name__}")
    return doc


# -- 조회 ----------------------------------------------------------------------


def provider_named(providers: Iterable[LLMProvider], name: str) -> LLMProvider | None:
    """이름이 정확히 같은 provider. 폴백이 없다.

    `PlatformSettings.provider` 는 이름이 비었거나 못 찾으면 `defaults.spec_generator` 나 첫
    provider 로 떨어진다. 문서에 적힌 이름 하나만 쓰는 자리(Knowledge Base 의
    `embedding.provider`, Agent 그래프 노드의 provider)는 그 폴백이 있으면 안 된다. 이름이
    지워졌으면 다른 provider 로 조용히 임베딩하는 대신 `unknown_provider` 로 멈춰야 한다."""
    return next((p for p in providers if p.name == name), None)


# -- 키 ------------------------------------------------------------------------


def key_in_secret(data: dict[str, str], kind: str) -> str | None:
    """Secret 데이터에서 키 값을 고른다. `api_key`, 없으면 kind 별 옛 이름, 그것도 없고 항목이
    하나뿐이면 그 값이다."""
    for k in (SECRET_KEY, ALT_SECRET_KEYS.get(kind, "")):
        if k and data.get(k):
            return data[k]
    if len(data) == 1:
        (value,) = data.values()
        if value:
            return value
    return None


def resolve_api_key(cfg: LLMProvider, secret_store: SecretStore | None, *, namespace: str) -> str:
    """플랫폼 네임스페이스 Secret(api_key_secret) 에서 키를 읽는다. 다른 경로는 없다.
    오류: 참조가 비어 있거나 Secret 에 키가 없으면 no_api_key, Secret 이 없으면 no_secret,
    Secret 저장소(home 클러스터)가 없으면 no_secret_store. 메시지에 키 값은 절대 넣지 않는다."""
    if not cfg.api_key_secret:
        raise ProviderError("no_api_key", f"provider {cfg.name!r} has no api_key_secret")
    if secret_store is None:
        raise ProviderError(
            "no_secret_store", f"api_key_secret {cfg.api_key_secret!r} needs a secret store"
        )
    data = secret_store.get(cfg.api_key_secret, namespace=namespace)
    if data is None:
        raise ProviderError(
            "no_secret",
            f"provider {cfg.name!r}: secret {cfg.api_key_secret!r} not found in {namespace!r}",
        )
    key = key_in_secret(data, cfg.kind)
    if key is None:
        raise ProviderError(
            "no_api_key",
            f"provider {cfg.name!r}: secret {cfg.api_key_secret!r} has no {SECRET_KEY!r} entry",
        )
    return key


# -- 구현 ----------------------------------------------------------------------


def retryable_status(status: Any) -> bool:
    """같은 요청을 다시 보내도 되는 provider 응답 상태인가. 규칙은 llm_http.py 하나이고 스펙
    생성기와 비서가 같이 쓴다."""
    return llm_http.retryable_status(status)


def _status_error(code_prefix: str, e: Any, *, gateway: bool = False) -> ProviderError:
    """SDK 의 상태 오류를 코드로. 크레딧 소진이 먼저이고, 게이트웨이 경로면 그다음이 게이트웨이가
    스스로 낸 답(한도, 토큰 거절)이다. 둘 다 다시 보내지 않는다 (P38 7절)."""
    status = getattr(e, "status_code", None)
    message = getattr(e, "message", None) or str(e)
    if llm_http.quota_exhausted(e):
        return ProviderError("quota_exhausted", f"HTTP {status}: {message}")
    if (code := llm_http.gateway_error(e, gateway=gateway)) is not None:
        return ProviderError(code, f"HTTP {status}")
    if status == 429:
        return ProviderError(PROVIDER_RATE_LIMITED, message, retryable=True)
    if status in (401, 403):
        return ProviderError("auth", message)
    if retryable_status(status):
        return ProviderError("provider_error", f"HTTP {status}: {message}", retryable=True)
    return ProviderError(code_prefix, f"HTTP {status}: {message}")


def _messages(system: str, messages: list[Message]) -> list[BaseMessage]:
    """우리 계약의 (system, [{role, content}]) 를 langchain 메시지로. SystemMessage 는
    ChatAnthropic 에서 Messages API 의 `system` 필드로 간다."""
    out: list[BaseMessage] = [SystemMessage(system)]
    for m in messages:
        content = m.get("content", "")
        out.append(AIMessage(content) if m.get("role") == "assistant" else HumanMessage(content))
    return out


async def _invoke(
    chat: BaseChatModel,
    sdk: Any,
    system: str,
    messages: list[Message],
    max_tokens: int,
    *,
    gateway: bool = False,
) -> AIMessage:
    """채팅 한 번과 오류 변환. sdk 는 langchain 이 올리는 예외의 하부 SDK 모듈(openai·anthropic)
    이다. 두 SDK 의 429 도 APIStatusError 라 `_status_error` 가 provider_rate_limited 로 가른다.
    게이트웨이 경로의 연결 실패는 `llm_gateway_unreachable` 이고 직접 경로처럼 다시 보낸다. 토큰을
    받지 못한 게이트웨이 호출은 `no_signer` 이고 다시 보내지 않는다."""
    try:
        return await chat.ainvoke(_messages(system, messages), max_tokens=max_tokens)
    except sdk.APIStatusError as e:
        raise _status_error("bad_request", e, gateway=gateway) from e
    except sdk.APIConnectionError as e:
        code = llm_http.gateway_error(e, gateway=gateway) or "connection"
        retryable = not llm_http.not_retried(e, gateway=gateway)
        raise ProviderError(code, str(e), retryable=retryable) from e


def _usage(message: AIMessage) -> Usage:
    """다섯 수(llm_http.usage_from_metadata). 사용량이 없으면 빈 dict."""
    return llm_http.usage_from_metadata(message.usage_metadata) if message.usage_metadata else {}


class OpenAIProvider:
    """ChatOpenAI + response_format json_object. base_url 로 OpenAI 호환 게이트웨이도 된다.

    모델 계열별 인자 제약은 langchain-openai 가 지킨다. `max_tokens` 는 `max_completion_tokens`
    로 바뀌고, gpt-5 계열이면 기본값 아닌 temperature 가 떨어진다. 둘 다 모델 생성 시점의 처리라
    temperature 를 여기서 받는다(호출마다 바꾸면 그 처리를 지나친다)."""

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str,
        *,
        base_url: str | None = None,
        http_client: Any = None,
        timeout: float = 300.0,
        temperature: float = 0.0,
        max_retries: int | None = None,
        extra_body: dict[str, Any] | None = None,
        chat: BaseChatModel | None = None,
        bearer: Callable[[], str] | None = None,
        gateway: bool = False,
    ):
        self.name = name
        self.model = model
        self.gateway = gateway
        # 게이트웨이 경로의 토큰 훅은 그 provider 경로 아래로 가는 요청에만 토큰을 싣는다
        gateway_base = base_url if bearer is not None else None
        client = llm_http.hooked_async_client(
            http_client, base_url=gateway_base, timeout=timeout, bearer=bearer
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            # 상관 헤더 훅 (P37 2절). 준 클라이언트(테스트의 목)에도 훅을 단다. 게이트웨이 경로는
            # 요청마다 토큰을 싣는 훅도 달고 연결 제한이 5초다 (P38 5절·10절)
            "http_async_client": client,
            "request_timeout": llm_http.gateway_timeout(timeout) if gateway else timeout,
            "temperature": temperature,
            "model_kwargs": {"response_format": {"type": "json_object"}},
        }
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        if extra_body:
            kwargs["extra_body"] = extra_body
        self.chat: BaseChatModel = chat if chat is not None else ChatOpenAI(**kwargs)

    async def complete(
        self, system: str, messages: list[Message], *, max_tokens: int
    ) -> tuple[str, Usage]:
        resp = await _invoke(self.chat, openai, system, messages, max_tokens, gateway=self.gateway)
        return resp.text, _usage(resp)


class AnthropicProvider:
    """ChatAnthropic. JSON 은 system 지시로 강제하고 verify() 가 검증한다.

    temperature 는 보내지 않는다. SDK 2.x 가 보내는 API 판 2026-10-21 은 temperature·top_p·top_k 를
    모든 모델에서 400 으로 거부하는데 langchain-anthropic 은 받은 값을 extra_body 로 본문에 싣는다.
    결정성은 프롬프트로만 잡는다."""

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str,
        *,
        base_url: str | None = None,
        http_client: Any = None,
        timeout: float = 300.0,
        max_retries: int | None = None,
        chat: BaseChatModel | None = None,
        bearer: Callable[[], str] | None = None,
        gateway: bool = False,
    ):
        self.name = name
        self.model = model
        self.gateway = gateway
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "default_request_timeout": timeout,
            # 상관 헤더 훅을 단 클라이언트를 쓴다 (P37 2절). 준 클라이언트(테스트의 목)에도 단다
            "http_client": http_client,
            # 게이트웨이 경로의 요청마다 토큰과 연결 제한이다 (P38 5절·10절)
            "bearer": bearer,
            "connect_timeout": llm_http.GATEWAY_CONNECT_SECONDS if gateway else None,
        }
        if base_url is not None:
            kwargs["base_url"] = base_url
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        self.chat: BaseChatModel = (
            chat if chat is not None else llm_http.HookedChatAnthropic(**kwargs)
        )

    async def complete(
        self, system: str, messages: list[Message], *, max_tokens: int
    ) -> tuple[str, Usage]:
        resp = await _invoke(
            self.chat, anthropic, system, messages, max_tokens, gateway=self.gateway
        )
        if resp.response_metadata.get("stop_reason") == "refusal":
            raise ProviderError("refusal", "model refused the request")
        return resp.text, _usage(resp)


def build_provider(
    cfg: LLMProvider, endpoint: Endpoint, *, http_client: Any = None
) -> SpecProvider:
    """설정 한 줄과 그 호출의 길(`llm_endpoint.resolve_endpoint`)에서 provider 를 만든다. SDK
    자체 재시도는 끈다. 다시 보내기는 Spec Generator 의 루프(`retryable`)만 하므로, 크레딧 소진처럼
    다시 보내면 안 되는 오류가 SDK 안에서 되풀이되지 않는다."""
    model = cfg.effective_model
    route = {
        "base_url": endpoint.base_url,
        "http_client": http_client,
        "max_retries": 0,
        "bearer": endpoint.bearer,
        "gateway": endpoint.gateway,
    }
    if cfg.sdk_kind == "openai":
        return OpenAIProvider(cfg.name, model, endpoint.api_key, extra_body=cfg.extra_body, **route)
    if cfg.sdk_kind == "anthropic":
        return AnthropicProvider(cfg.name, model, endpoint.api_key, **route)
    raise ProviderError("unknown_kind", f"unsupported provider kind {cfg.kind!r}")


# -- 연결 확인 -------------------------------------------------------------------

CHECK_CODES = (
    "ok",
    "no_secret",
    "no_api_key",
    "auth",
    "connection",
    "quota_exhausted",
    "provider_error",
)
# 모델 목록을 키 없이도 주는 OpenAI 호환 게이트웨이와 키를 확인하는 경로. 목록만 부르면 틀린 키도
# ok 가 되므로 이 경로를 한 번 더 부른다. OpenRouter 의 `/key` 는 키의 한도와 사용량을 준다
KEY_CHECK_PATHS: dict[str, str] = {"openrouter.ai": "/key"}


def _key_check_path(base_url: str | None) -> str | None:
    host = urlparse(base_url).hostname if base_url else None
    return KEY_CHECK_PATHS.get(host or "")


@dataclass(frozen=True)
class ProviderCheck:
    """`check_provider` 결과. code 는 CHECK_CODES 중 하나이고 provider 의 원문 메시지는 싣지 않는다.
    OpenAI 의 401 메시지가 마스킹된 키 앞부분을 포함하기 때문이다."""

    ok: bool
    code: str


async def check_provider(
    cfg: LLMProvider,
    secret_store: SecretStore | None,
    *,
    namespace: str,
    http_client: Any = None,
) -> ProviderCheck:
    """Secret 존재·키 유무를 본 뒤 두 kind 모두 `models.list` 를 한 번 불러 키와 연결을 확인한다.
    모델 목록을 키 없이 주는 게이트웨이(`KEY_CHECK_PATHS`)는 키 확인 경로를 한 번 더 부른다.
    재시도 없이 요청마다 CHECK_TIMEOUT 안에 끝난다. Secret 저장소가 없으면
    ProviderError(no_secret_store) 를 그대로 올리므로 호출자가 먼저 막는다.

    크레딧을 다 쓴 키(T20)는 모델 목록이 200 이므로 따로 본다. OpenRouter 는 키 확인 경로가 주는
    남은 한도(`limit_remaining`)를, OpenAI 자체는 가장 싼 임베딩 한 번을 본다. 임베딩이 크레딧 소진
    말고 다른 이유로 실패하면 키와 연결은 이미 확인했으므로 결과는 ok 그대로다. 요청마다
    CHECK_TIMEOUT 이 상한이라 두 번 부르면 그 두 배까지 걸린다."""
    try:
        api_key = await run_in_threadpool(resolve_api_key, cfg, secret_store, namespace=namespace)
    except ProviderError as e:
        if e.code in ("no_secret", "no_api_key"):
            return ProviderCheck(False, e.code)
        raise
    client_kw: dict[str, Any] = {
        "api_key": api_key,
        "base_url": cfg.effective_base_url,
        "http_client": http_client,
        "timeout": CHECK_TIMEOUT,
        "max_retries": 0,
    }
    try:
        if cfg.sdk_kind == "openai":
            client = openai.AsyncOpenAI(**client_kw)
            await client.models.list()
            if (path := _key_check_path(cfg.effective_base_url)) is not None:
                if _key_spent(await client.get(path, cast_to=object)):
                    return ProviderCheck(False, "quota_exhausted")
            elif _is_openai(cfg.effective_base_url) and await _openai_quota_spent(client):
                return ProviderCheck(False, "quota_exhausted")
        elif cfg.sdk_kind == "anthropic":
            # 상관 헤더처럼 traceparent 는 SDK 가 아니라 우리가 싣는다(llm_http.HookedChatAnthropic)
            await anthropic.AsyncAnthropic(**client_kw, open_telemetry=False).models.list()
        else:
            raise ProviderError("unknown_kind", f"unsupported provider kind {cfg.kind!r}")
    except (openai.APIConnectionError, anthropic.APIConnectionError):
        return ProviderCheck(False, "connection")
    except (openai.APIStatusError, anthropic.APIStatusError) as e:
        if llm_http.quota_exhausted(e):
            return ProviderCheck(False, "quota_exhausted")
        status = getattr(e, "status_code", None)
        return ProviderCheck(False, "auth" if status in (401, 403) else "provider_error")
    return ProviderCheck(True, "ok")


def _key_spent(body: Any) -> bool:
    """OpenRouter `/key` 의 남은 한도가 0 이하인가. 한도가 없는 키는 null 이다."""
    data = body.get("data") if isinstance(body, dict) else None
    remaining = data.get("limit_remaining") if isinstance(data, dict) else None
    return isinstance(remaining, int | float) and remaining <= 0


def _is_openai(base_url: str | None) -> bool:
    """OpenAI 자체인가. 호환 게이트웨이는 임베딩 모델이 없을 수 있어 크레딧을 보지 않는다."""
    return base_url is None or urlparse(base_url).hostname == OPENAI_HOST


async def _openai_quota_spent(client: Any) -> bool:
    """가장 싼 임베딩 한 번으로 크레딧이 남았는지 본다(토큰 하나). 크레딧 소진 말고 다른 실패는
    모른다고 보고 넘어간다."""
    try:
        await client.embeddings.create(model=QUOTA_PROBE_MODEL, input="ping")
    except openai.APIStatusError as e:
        return llm_http.quota_exhausted(e)
    except openai.APIError:
        return False
    return False
