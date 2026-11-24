"""Agent 런타임 프록시 (D21). 플랫폼 API 가 배포된 런타임의 `/chat`·`/sessions`·`/metrics` 를 대신
부르는 자리다.

플랫폼이 인바운드 인증을 하고 런타임에는 인스턴스 토큰을 Bearer 로 붙인다. 토큰은 Mongo 에
해시뿐이라 대상 클러스터의 토큰 Secret 을 읽어 파드 로컬 TTL 캐시에 둔다. 토큰 Secret 을 다시 쓰거나
회수한 서비스는 `forget_token` 으로 그 캐시를 비운다.

런타임 Service 로 가는 길은 `bearer_service_client` 가 정한다(D41). 플랫폼이 그 클러스터 안에
있으면 svc DNS 직행이고, 아니면(원격 클러스터, kubecontext 로컬 개발) pods/portforward 터널이다. API
서버 services/proxy 는 인증을 마치면 Authorization 을 지워 런타임이 인스턴스 토큰을 못 받기
때문이다. 터널은 Ready 파드 가운데 무작위로 하나다.

호출 꼴은 셋이다. JSON 하나(`request`)는 (상태, 본문, 헤더)로 돌려주고 상태 조회가 쓴다.
배포된 문서만 받는 JSON 하나(`call`)는 그 위에 상태 가드를 얹어 (상태, 본문)을 주고 조회와 중지
같은 라우트가 쓴다. 스트림(`stream`)은 SSE 턴과 파일 바이트에 쓰고 `ProxyResponse` 로 상태·헤더·
청크 반복자를 그대로 넘기며, 반복이 끝나면(또는 끊기면) 런타임 연결과 터널이 닫힌다. 셋 다
메서드와 쿼리를 받는다. 준비되지 않은 파드의 이유(`unready_reason`, T21)는 Service 가 그 파드로
가지 않으므로 `bearer_pod_client` 로 그 파드에 바로 연결하고, 실패를 오류로 올리지 않는다. 연결
실패는 502 runtime_unreachable, 시간 초과는 504 runtime_timeout 이다. 상태 가드와 클러스터
클라이언트는 AgentService 의 것을 받아 쓴다. 배포된 것만 받으므로 밖의 상태는 409 not_deployed
다. 런타임 능력에 기대는 라우트의 가르기(기록에 이름이 없으면 409 runtime_feature_missing, D65)와
롤링 창의 409 runtime_rolling(D61)은 라우터 쪽 `api/_agent_proxy.py` 가 프록시를 부르기 전과
응답을 받은 뒤에 한다.

잠든 Agent (P44 8절) 의 읽기는 런타임을 부르지 않고 409 agent_asleep, 깨우는 중이면 503
agent_waking 이다. 턴은 라우트의 게이트가 먼저 깨우고, 런타임의 드레인 표시에 거절된 턴은 여기서
한 번 다시 보내거나 503 agent_waking 으로 바꾼다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import anyio
import httpx2

from daegam.errors import ServiceError
from daegam.k8s.client import AccessDenied, ClusterUnreachable, KubeClient
from daegam.k8s.portforward import (
    PortForwardError,
    bearer_header,
    bearer_pod_client,
    bearer_service_client,
)
from daegam.k8s.templates import AGENT_PORT, HEALTHZ_PATH
from daegam.services.instance_base import EDIT_STATES
from daegam.store.agents import Agent, AgentK8s
from daegam.ttl_cache import TTLCache

logger = logging.getLogger(__name__)

RUNTIME_TIMEOUT = 30.0  # 세션·메트릭 조회와 스트림의 연결·쓰기
# 인스턴스 전체를 다루는 플랫폼 경로(`/turns/cancel`, `/drain`) 부름 하나의 상한(초). 연결을 삼키는
# 런타임이 리컨실 틱이나 쉬기 워커의 틱 하나를 다 쓰지 않게 짧게 둔다
INSTANCE_CALL_TIMEOUT = 5.0
# 플랫폼이 스스로 하는 일로 런타임을 부를 때 싣는 주체(`system:` 이름). 배포 해제와 삭제의 턴
# 멈추기(P78 5절)와 쉬기의 드레인 표시(P44 7절)다
TURN_STOP_SUB = "system:turn-stop"
SLEEP_SUB = "system:sleep"
# 런타임의 드레인 표시 경로 (P44 7절). 표시가 선 동안 리스를 잡는 요청은 503 `agent_draining` 이다
DRAIN_PATH = "/drain"
AGENT_DRAINING = "agent_draining"
# 잠든 Agent 의 읽기와 깨우는 중의 답 (P44 8절, `daegam/error_codes.py`). 깨우는 중의 답은
# Retry-After 를 싣는다
AGENT_ASLEEP = "agent_asleep"
AGENT_WAKING = "agent_waking"
WAKE_RETRY_SECONDS = 5
# 준비되지 않은 파드의 이유를 읽는 상한(토큰 읽기, 터널, 요청을 다 합한 것). 상세 상태 조회와 그
# 폴링이 이것 때문에 오래 걸리지 않게 짧게 둔다
UNREADY_TIMEOUT = 3.0
CHAT_READ_TIMEOUT = 900.0  # 한 턴의 SSE 읽기. 런타임 턴 타임아웃보다 길다
TOKEN_CACHE_TTL = 60.0  # 인스턴스 토큰 Secret 을 매 요청 읽지 않는다
APPROVER_HEADER = "X-Daegam-Approver"  # agent:approve scope 가 있으면 1
# 진행 중 턴을 화면이 다시 읽는 간격(초, P78 4절). 런타임 Service 에 바로 닿으면 짧게, API 서버의
# 터널을 거치면 읽기마다 API 서버를 세 번 부르므로 길게 둔다
POLL_SECONDS_DIRECT = 3
POLL_SECONDS_TUNNELLED = 10
# 프록시가 그대로 넘기지 않는 응답 헤더. 본문은 다시 청크로 나가므로 길이·인코딩 헤더는 틀린다
HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "date",
        "server",
    }
)


def path_segment(value: str) -> str:
    """값 하나를 런타임 URL 의 조각 하나로 다시 인코딩한다. `/`·`?`·`#` 가 경로를 바꾸지 못하게
    하고 점도 `%2E` 로 바꾼다. 그대로 두면 HTTP 클라이언트가 `.`·`..` 조각을 경로 정규화로 지워
    `/sessions` 나 `/` 같은 다른 경로를 부른다."""
    return quote(value, safe="").replace(".", "%2E")


def file_segments(path: str) -> str:
    """파일 경로를 런타임 URL 로 옮긴다. `/` 는 두고 조각마다 `path_segment` 로 다시 인코딩한다.
    `..` 조각도 정규화되지 않은 채 런타임까지 가서 런타임의 경로 검사(422 invalid_path)가
    거절한다."""
    return "/".join(path_segment(part) for part in path.split("/"))


def session_path(session_id: str, *rest: str) -> str:
    """런타임의 세션 경로다. 세션 id 를 조각 하나로 다시 인코딩한다(`path_segment`)."""
    return "/".join(("/sessions", path_segment(session_id), *rest))


def memory_file_path(key: str) -> str:
    """런타임의 장기 기억 파일 경로다. key 는 `/memories/` 밑의 경로다(P36)."""
    return f"/memories/files/{file_segments(key)}"


def transcript_items(detail: Any) -> list[dict[str, Any]]:
    """런타임 세션 상세의 전사 항목(`transcript.items`)이다. 본문을 믿지 않으므로 dict 가 아닌 것은
    빼고, 전사가 없는 옛 런타임이면 빈 목록이다."""
    transcript = detail.get("transcript") if isinstance(detail, dict) else None
    items = transcript.get("items") if isinstance(transcript, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def done_index(items: list[dict[str, Any]], turn_id: str) -> int | None:
    """전사 항목에서 그 `turn_id` 로 끝난 턴의 `done` 항목 자리다. 없으면 None 이다. 피드백의 턴
    확인과 평가 사례의 복사가 쓴다."""
    return next(
        (
            i
            for i, it in enumerate(items)
            if it.get("kind") == "done" and it.get("turn_id") == turn_id
        ),
        None,
    )


def turn_answer(items: list[dict[str, Any]], end: int) -> tuple[int, str]:
    """end 자리의 `done` 으로 끝난 턴의 (첫 자리, 답)이다. 첫 자리는 그 턴의 사용자 항목 바로
    뒤이고 사용자 항목이 없으면 0 이다. 답은 메인 에이전트의 말(하위 에이전트의 이름표가 없는 것)을
    잇고, 없으면 그 턴의 모든 말을 잇는다. 인사말은 뺀다."""
    start = end
    while start > 0 and items[start - 1].get("kind") != "user":
        start -= 1
    said = [
        it
        for it in items[start:end]
        if it.get("kind") == "assistant"
        and not it.get("greeting")
        and isinstance(it.get("text"), str)
    ]
    main = [it["text"] for it in said if not it.get("agent")] or [it["text"] for it in said]
    return start, "\n\n".join(t for t in main if t.strip())


def last_answer(items: list[dict[str, Any]]) -> str | None:
    """전사의 마지막 `done` 으로 끝난 턴의 답이다. 끝난 턴이 없으면 None 이다. 다른 Agent 가 부른
    실행의 답을 이것으로 읽는다."""
    end = next((i for i in range(len(items) - 1, -1, -1) if items[i].get("kind") == "done"), None)
    return turn_answer(items, end)[1] if end is not None else None


def ends_undecided(items: list[dict[str, Any]]) -> bool:
    """전사가 아직 결정되지 않은 인터럽트로 끝나는가. 그 세션은 결정이 올 때까지 새 턴을 받지
    않는다."""
    last = items[-1] if items else {}
    return last.get("kind") == "interrupt" and not last.get("resolved")


def _with_choices(body: dict[str, Any], choices: list[dict[str, str]] | None) -> dict[str, Any]:
    """기억한 승인 선택을 실은 본문이다(P43 5절). 실을 것이 있을 때만 칸을 둔다. `tool_safety` 를
    모르는 옛 이미지는 모르는 칸을 버린다."""
    return {**body, "approval_choices": choices} if choices else body


def _with_grant(body: dict[str, Any], turn_grant: dict[str, Any] | None) -> dict[str, Any]:
    """턴 본문에 플랫폼이 연 턴 위임을 싣는다(P53 4절). 위임이 없으면 칸도 없어 본문이 지금과
    같다."""
    return {**body, "turn_grant": turn_grant} if turn_grant is not None else body


def _query(params: dict[str, Any] | None) -> dict[str, Any]:
    """런타임에 싣는 쿼리다. 값이 None 인 키는 뺀다."""
    return {k: v for k, v in (params or {}).items() if v is not None}


def unready_text(body: Any) -> str | None:
    """런타임 healthz 상세의 `unready{code, message}` 를 `code: message` 로 옮긴다. 순수 함수.
    칸이 없거나(옛 이미지, 준비된 파드) 모양이 다르면 None 이다."""
    unready = body.get("unready") if isinstance(body, dict) else None
    if not isinstance(unready, dict) or not unready.get("code"):
        return None
    message = unready.get("message")
    return f"{unready['code']}: {message}" if message else str(unready["code"])


# 프록시가 런타임을 부르는 상태. 배포를 마쳤거나, 재배포가 실패해도 앞 파드가 도는 상태다. 턴 위임도
# 이 상태의 Agent 에만 연다(`services/turn_grants.py`)
RUNTIME_STATES: frozenset[str] = EDIT_STATES


def serves_turns(doc: Agent) -> bool:
    """프록시가 그 Agent 의 런타임을 부르는가 (`AgentRuntimeProxy.require_deployed`)."""
    return doc.state in RUNTIME_STATES


@dataclass
class ProxyResponse:
    """런타임 SSE 응답. 상태·헤더는 그대로이고 본문은 청크 반복자다. 반복이 끝나거나 본문을
    `aclose` 하면 런타임 연결과 터널이 닫힌다."""

    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]


async def read_json(proxied: ProxyResponse) -> Any:
    """스트림 응답의 본문을 끝까지 읽어 JSON 으로 준다. 읽기가 끝나면 런타임 연결이 닫힌다. 비었거나
    JSON 이 아니면 None 이다. 오류 응답은 스트림이 시작되기 전에 오므로 이것으로 읽는다."""
    return _json_or_none(await _read_raw(proxied))


async def read_code(proxied: ProxyResponse) -> tuple[str | None, Any]:
    """오류 응답의 (code, 본문)이다. 본문을 끝까지 읽는다. code 가 없으면 None 이다."""
    body = await read_json(proxied)
    code = body.get("code") if isinstance(body, dict) else None
    return (code if isinstance(code, str) else None), body


async def sse_events(proxied: ProxyResponse) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """런타임 SSE 본문의 (이벤트, 데이터)다. 블록은 빈 줄로 가르고, 한 블록의 `data:` 줄 여럿은
    SSE 처럼 줄바꿈으로 이어 JSON 하나로 읽는다. 데이터가 JSON 객체가 아니면 빈 dict 다. 끝까지
    읽으면 연결이 닫힌다."""
    buffer = ""
    async for chunk in proxied.body:
        buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if (parsed := _sse_block(block)) is not None:
                yield parsed
    if (parsed := _sse_block(buffer)) is not None:
        yield parsed


def _sse_block(block: str) -> tuple[str, dict[str, Any]] | None:
    event: str | None = None
    lines: list[str] = []
    for line in block.strip().splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    if event is None:
        return None
    data = _json_or_none("\n".join(lines).encode()) if lines else None
    return event, data if isinstance(data, dict) else {}


def _json_or_none(raw: bytes) -> Any:
    """본문 바이트의 JSON. 비었거나 JSON 이 아니면 None 이다."""
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _read_raw(proxied: ProxyResponse) -> bytes:
    """스트림 응답의 본문 바이트. 읽기가 끝나면 런타임 연결이 닫힌다."""
    return b"".join([chunk async for chunk in proxied.body])


async def _once(raw: bytes) -> AsyncIterator[bytes]:
    yield raw


def _drained(status: int, body: Any) -> bool:
    """런타임이 드레인 표시로 리스를 거절한 답인가 (P44 7절)."""
    return status == 503 and isinstance(body, dict) and body.get("code") == AGENT_DRAINING


def waking_body(doc: Agent) -> dict[str, Any]:
    """깨우는 중의 오류 본문 (P44 8절). 런타임이 내는 거절과 같은 꼴이다."""
    return {
        "code": AGENT_WAKING,
        "message": f"agent {doc.name} is starting its pod again; retry after "
        f"{WAKE_RETRY_SECONDS} seconds",
    }


def waking_error(error_cls: type[ServiceError], doc: Agent) -> ServiceError:
    """503 `agent_waking` 과 `Retry-After`. 런타임이 넘긴 503 처럼 본문에도 retry_after 가 있다."""
    retry = str(WAKE_RETRY_SECONDS)
    return error_cls(
        503,
        AGENT_WAKING,
        waking_body(doc)["message"],
        headers={"Retry-After": retry},
        retry_after=retry,
    )


def asleep_error(error_cls: type[ServiceError], doc: Agent, action: str) -> ServiceError:
    """409 `agent_asleep`. 런타임을 읽는 요청은 Agent 를 깨우지 않는다 (P44 8절)."""
    return error_cls(
        409,
        AGENT_ASLEEP,
        f"agent {doc.name} is asleep and runs no pod; wake it to {action}",
    )


def waking_response(doc: Agent) -> ProxyResponse:
    """503 `agent_waking` 스트림 응답 (P44 8절). 런타임의 503 과 같은 꼴이라 라우트와 실행 시작
    워커가 같은 자리에서 읽는다."""
    raw = json.dumps(waking_body(doc)).encode()
    headers = {"content-type": "application/json", "retry-after": str(WAKE_RETRY_SECONDS)}
    return ProxyResponse(status=503, headers=headers, body=_once(raw))


class ClosingBody:
    """끝까지 읽거나 `aclose` 하면 chunks 를 닫고 on_close 를 부르는 스트림 본문. 생성기와 달리 한
    번도 읽지 않고 닫혀도 닫는다(스트림을 흘리기 전에 브라우저가 끊은 경우). 두 번 닫아도 한 번이다.
    런타임 SSE 본문(응답·클라이언트·터널 문맥을 닫는다)과 턴 위임을 끝내는 본문
    (`services/turn_person.py`)이 쓴다.

    닫기는 막이 안에서 한다. 브라우저가 끊으면 본문이 anyio 취소 범위 안에서 닫히는데, 막이가 없으면
    정리의 await 가 다시 취소되어 port-forward 웹소켓이 남고 위임이 끝나지 않는다."""

    def __init__(self, chunks: AsyncIterator[bytes], on_close: Callable[[], Awaitable[None]]):
        self._chunks = chunks
        self._on_close = on_close
        self._closed = False

    def __aiter__(self) -> ClosingBody:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await anext(self._chunks)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                aclose = getattr(self._chunks, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                await self._on_close()


class StateGuard(Protocol):
    """`BaseInstanceService._require` 의 모양. 상태 표 밖의 동작을 409 로 막는다."""

    def __call__(
        self, inst: Agent, allowed: Collection[str], *, action: str, outside: str
    ) -> None: ...


class SleepHooks(Protocol):
    """프록시가 부르는 쉬기의 훅 (P44, `services/agent_sleep.py` `AgentSleepControl`)."""

    async def met_draining(self, doc: Agent) -> bool: ...

    async def podless(self, doc: Agent) -> bool: ...

    def just_woken(self, doc: Agent) -> bool: ...


class AgentRuntimeProxy:
    """배포된 런타임 하나를 부르는 프록시. AgentService 가 `__init__` 에서 하나 만들어 붙인다.

    - client(doc): 문서가 사는 클러스터의 KubeClient. 못 만들면 error_cls 를 던진다
    - read_token(doc, client): 토큰 Secret 의 인스턴스 토큰. 없으면 None 이다. 프록시는 그 위에
      TTL 캐시와 503 만 얹는다
    - require(doc, allowed, action=, outside=): 상태 가드. `require_deployed` 가 배포된 것만 받는
      동작에 건다
    - k8s(doc): 배포된 K8s 이름 묶음. 배포 쪽이 비어 있으면 409 not_deployed 를 던진다
    - error_cls: `(status, code, detail, **extra)` 로 만드는 서비스 오류 클래스
    """

    def __init__(
        self,
        *,
        client: Callable[[Agent], Awaitable[KubeClient]],
        read_token: Callable[[Agent, KubeClient], Awaitable[str | None]],
        require: StateGuard,
        k8s: Callable[[Agent], AgentK8s],
        error_cls: type[Exception],
    ):
        self.client = client
        self.read_token = read_token
        self.require = require
        self.k8s = k8s
        self.error = error_cls
        self._token_cache: TTLCache[str, str] = TTLCache(TOKEN_CACHE_TTL)
        # 쉬기의 훅 (P44). AgentService 가 만든 뒤에 단다. 없으면 드레인 거절을 그대로 넘긴다
        self.sleep: SleepHooks | None = None

    def require_deployed(self, doc: Agent, *, action: str) -> None:
        """런타임이 도는 상태(`RUNTIME_STATES`)만 받는다. 밖의 상태는 409 not_deployed 다. 런타임을
        부르는 프록시와 런타임에 보낼 것을 미리 받는 라우트(첨부 업로드)가 같은 검사를 쓴다."""
        self.require(doc, RUNTIME_STATES, action=action, outside="not_deployed")

    def refuse_asleep(self, doc: Agent, *, action: str) -> None:
        """잠든 Agent 의 런타임 읽기는 런타임을 부르지 않는다 (P44 8절). asleep 이면 409
        `agent_asleep`, waking 이면 503 `agent_waking` 이다. 읽기는 Agent 를 깨우지 않으므로 열어 둔
        화면이 Agent 를 깨워 두지 않는다. 턴은 라우트의 게이트가 먼저 깨운다."""
        state = doc.sleep.state
        if state == "asleep":
            raise asleep_error(self.error, doc, action)
        if state == "waking":
            raise waking_error(self.error, doc)

    def forget_token(self, agent_id: str) -> None:
        """토큰 Secret 을 다시 쓰거나 회수했을 때 캐시를 비운다. 다음 호출이 Secret 을 다시
        읽는다."""
        self._token_cache.pop(agent_id)

    # -- 호출 준비 --------------------------------------------------------------------------

    async def _instance_token(self, doc: Agent, client: KubeClient) -> str:
        """플랫폼과 런타임이 나눠 갖는 비밀. Mongo 에는 해시뿐이라 대상 클러스터의 토큰 Secret 을
        읽는다. 없으면 503 이다."""
        hit = self._token_cache.get(doc.id)
        if hit is not None:
            return hit
        token = await self.read_token(doc, client)
        if not token:
            k8s = self.k8s(doc)
            raise self.error(
                503,
                "instance_token_missing",
                f"token secret {k8s.namespace}/{k8s.token_secret} is missing",
            )
        self._token_cache.put(doc.id, token)
        return token

    def is_direct(self, doc: Agent, client: KubeClient) -> bool:
        """런타임 Service 에 svc DNS 로 바로 닿는가. 아니면 호출마다 pods/portforward 터널을
        연다."""
        k8s = self.k8s(doc)
        return client.service_route(k8s.namespace, k8s.name, AGENT_PORT).via == "direct"

    @contextlib.asynccontextmanager
    async def _runtime_client(
        self, doc: Agent, client: KubeClient, *, read_timeout: float | None = None
    ) -> AsyncIterator[tuple[httpx2.AsyncClient, str]]:
        """런타임 Service 로 가는 (httpx2 클라이언트, 기준 URL). 인스턴스 토큰이 Bearer 로 실려
        있고 직행이 아니면 pods/portforward 다(D41). 클러스터에 연결할 수 없으면 502
        cluster_unreachable, 터널을 못 열거나(Ready 파드 없음) 클러스터 권한이 모자라면 502
        runtime_unreachable 이다."""
        k8s = self.k8s(doc)
        async with contextlib.AsyncExitStack() as stack:
            try:
                token = await self._instance_token(doc, client)
                hc, base_url = await stack.enter_async_context(
                    bearer_service_client(
                        client,
                        k8s.namespace,
                        k8s.name,
                        AGENT_PORT,
                        headers=bearer_header(token),
                        timeout=RUNTIME_TIMEOUT,
                        read_timeout=read_timeout,
                    )
                )
            except ClusterUnreachable as e:
                raise self.error(502, "cluster_unreachable", str(e)) from e
            except AccessDenied as e:
                raise self.error(502, "runtime_unreachable", f"forbidden: {e.missing}") from e
            except PortForwardError as e:
                raise self.error(502, "runtime_unreachable", str(e)) from e
            yield hc, base_url

    @staticmethod
    def _headers(*, approver: bool) -> dict[str, str]:
        """요청마다 얹는 헤더. Authorization 은 클라이언트가 이미 들고 있다."""
        return {APPROVER_HEADER: "1"} if approver else {}

    def _transport_error(
        self, e: httpx2.HTTPError, method: str, path: str, doc: Agent
    ) -> Exception:
        """httpx2 오류를 서비스 오류로 옮긴다. 시간 초과는 504, 그 밖의 연결 실패는 502 다.
        깨우기가 막 끝난 Agent 의 연결 거부는 Service 의 엔드포인트가 아직 없는 것이라 503
        `agent_waking` 이고 부른 쪽이 Retry-After 뒤에 다시 보낸다 (P44 8절,
        `AgentSleepControl.just_woken`)."""
        if isinstance(e, httpx2.TimeoutException):
            return self.error(504, "runtime_timeout", f"{method} {path}: {type(e).__name__}")
        if (
            isinstance(e, httpx2.ConnectError)
            and self.sleep is not None
            and self.sleep.just_woken(doc)
        ):
            return waking_error(self.error, doc)
        return self.error(502, "runtime_unreachable", f"{method} {path}: {type(e).__name__}: {e}")

    # -- 호출 ------------------------------------------------------------------------------

    async def unready_reason(
        self, doc: Agent, client: KubeClient, pod: dict[str, Any]
    ) -> str | None:
        """준비되지 않은 파드 하나의 healthz 상세에서 이유(`unready{code, message}`)를 읽어
        `code: message` 로 준다(T21). Service 는 Ready 파드로만 가므로 그 파드에 바로 닿는다. 읽지
        못하면(토큰 없음, 터널 실패, 옛 이미지라 칸이 없음) None 이다. 상태 조회를 막지 않는다."""
        k8s = self.k8s(doc)
        try:
            # asyncio 의 시한은 한 번만 취소하므로 터널 정리(TunnelBridge 의 닫기)는 끝까지 돈다
            async with asyncio.timeout(UNREADY_TIMEOUT):
                token = await self._instance_token(doc, client)
                async with bearer_pod_client(
                    client,
                    k8s.namespace,
                    k8s.name,
                    pod,
                    AGENT_PORT,
                    bearer=token,
                    timeout=UNREADY_TIMEOUT,
                ) as (hc, base_url):
                    body = (await hc.get(f"{base_url}{HEALTHZ_PATH}")).json()
        except Exception as e:  # noqa: BLE001 - 이유를 못 읽어도 상태는 낸다
            logger.debug("unready reason of %s unavailable: %s", pod["metadata"]["name"], e)
            return None
        return unready_text(body)

    async def request(
        self,
        doc: Agent,
        client: KubeClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        approver: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        """런타임에 요청 하나를 보내 (상태, JSON 본문, 헤더) 로 받는다. 연결 실패는 502
        runtime_unreachable, 시간 초과는 504 runtime_timeout 이다."""
        try:
            async with self._runtime_client(doc, client) as (hc, base_url):
                r = await hc.request(
                    method,
                    f"{base_url}{path}",
                    json=json,
                    params=_query(params),
                    headers=self._headers(approver=approver),
                )
        except httpx2.HTTPError as e:
            raise self._transport_error(e, method, path, doc) from e
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = None
        return r.status_code, body, dict(r.headers)

    async def stream(
        self,
        doc: Agent,
        method: str,
        path: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        approver: bool = False,
        read_timeout: float | None = None,
        gated: bool = True,
    ) -> ProxyResponse:
        """배포된 런타임의 스트림 응답(SSE 턴, 파일 바이트)을 연다. 배포된 것만 받는다.
        read_timeout 은 청크 사이의 읽기 상한이고 None 이면 `RUNTIME_TIMEOUT` 이다. 턴은
        `CHAT_READ_TIMEOUT` 을 준다.

        상태·본문·Retry-After 를 그대로 넘기고 본문은 청크로 흘린다. 클라이언트·터널 문맥은
        `AsyncExitStack` 으로 연 채 본문(`ClosingBody`)에 넘겨 본문이 끝나거나 닫힐 때 닫는다.
        응답을 받기 전에 실패하면 그 자리에서 닫는다.

        gated 면 잠든 Agent 를 부르지 않는다(`refuse_asleep`). 턴은 라우트의 게이트가 먼저 깨우므로
        끈다. 드레인 표시의 거절은 `_past_drain` 이 가른다 (P44 7절)."""
        self.require_deployed(doc, action=action)
        if gated:
            self.refuse_asleep(doc, action=action)

        async def send() -> tuple[ProxyResponse, int, Any]:
            proxied = await self._stream(
                doc,
                method,
                path,
                params=params,
                json=json,
                approver=approver,
                read_timeout=read_timeout,
            )
            if proxied.status != 503:
                return proxied, proxied.status, None
            # 오류 응답은 스트림이 시작되기 전에 오므로 읽어서 가른 뒤 같은 상태로 다시 낸다
            raw = await _read_raw(proxied)
            replayed = ProxyResponse(proxied.status, proxied.headers, _once(raw))
            return replayed, proxied.status, _json_or_none(raw)

        answer = await self._past_drain(doc, send)
        return waking_response(doc) if answer is None else answer

    async def _past_drain[R](
        self, doc: Agent, send: Callable[[], Awaitable[tuple[R, int, Any]]]
    ) -> R | None:
        """리스를 잡는 요청(턴, 세션 지우기)이 런타임의 드레인 표시에 거절되면(503
        `agent_draining`) 문서가 awake 일 때 표시를 지우고 한 번 다시 보낸다. send 는 (답, 상태,
        본문)이다. 그래도 거절되거나 문서가 잠드는 중이면 None 이라 부른 쪽이 503 `agent_waking`
        으로 답하고, 다음 시도의 게이트가 Agent 를 깨운다. 라우트와 실행 시작 워커가 같은 자리를
        지난다."""
        answer, status, body = await send()
        if not _drained(status, body):
            return answer
        if self.sleep is not None and await self.sleep.met_draining(doc):
            answer, status, body = await send()
            if not _drained(status, body):
                return answer
        return None

    async def _stream(
        self,
        doc: Agent,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: Any,
        approver: bool,
        read_timeout: float | None,
    ) -> ProxyResponse:
        client = await self.client(doc)
        async with contextlib.AsyncExitStack() as stack:
            hc, base_url = await stack.enter_async_context(
                self._runtime_client(doc, client, read_timeout=read_timeout)
            )
            req = hc.build_request(
                method,
                f"{base_url}{path}",
                json=json,
                params=_query(params),
                headers=self._headers(approver=approver),
            )
            try:
                r = await hc.send(req, stream=True)
            except httpx2.HTTPError as e:
                raise self._transport_error(e, method, path, doc) from e
            stack.push_async_callback(r.aclose)
            body = ClosingBody(r.aiter_raw(), stack.pop_all().aclose)
        headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
        return ProxyResponse(status=r.status_code, headers=headers, body=body)

    async def call(
        self,
        doc: Agent,
        method: str,
        path: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        approver: bool = False,
        gated: bool = True,
    ) -> tuple[int, Any]:
        """배포된 런타임에 JSON 요청 하나를 보내 (상태, JSON 본문) 으로 받는다. 조회 프록시와
        중지가 같은 절차다. 배포된 것만 받으므로 밖의 상태는 409 not_deployed 다. 값이 None 인
        쿼리는 싣지 않는다. gated 면 잠든 Agent 를 부르지 않는다(`refuse_asleep`, P44 8절). 리스를
        잡는 요청(세션 지우기)의 드레인 거절은 턴과 같이 `_past_drain` 이 가른다."""
        self.require_deployed(doc, action=action)
        if gated:
            self.refuse_asleep(doc, action=action)
        client = await self.client(doc)

        async def send() -> tuple[tuple[int, Any], int, Any]:
            st, body, _ = await self.request(
                doc, client, method, path, params=params, json=json, approver=approver
            )
            return (st, body), st, body

        answer = await self._past_drain(doc, send)
        if answer is None:
            return 503, {**waking_body(doc), "retry_after": str(WAKE_RETRY_SECONDS)}
        return answer

    async def _get(
        self,
        doc: Agent,
        path: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
        approver: bool = False,
    ) -> tuple[int, Any]:
        return await self.call(doc, "GET", path, action=action, params=params, approver=approver)

    # -- 플랫폼 API 가 부르는 라우트 --------------------------------------------------------

    async def chat(
        self,
        doc: Agent,
        *,
        user_sub: str,
        message: str,
        session_id: str | None,
        ephemeral: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        turn_grant: dict[str, Any] | None = None,
        run_id: str | None = None,
        untrusted: bool = False,
        task: bool = False,
        approval_choices: list[dict[str, str]] | None = None,
    ) -> ProxyResponse:
        """POST /chat. 세션 소유자는 호출자의 sub 다. ephemeral 이면 이 턴에 장기 기억 저장소를
        안 붙인다(D34). attachments 는 런타임 모양의 첨부이고 있을 때만 싣는다(P36). turn_grant 는
        플랫폼이 연 턴 위임이고 있을 때만 싣는다(P53). run_id 는 백그라운드 실행의 id 이고 시작
        워커만 싣는다(P41 9절). untrusted 는 웹훅 실행의 표시이고 런타임이 세션에 둔다(4·9절).
        task 는 다른 Agent 가 맡긴 작업의 세션이라는 표시이고 run_id 와 함께만 싣는다(P46 6.7절).
        작업 세션은 턴이 끝나도 닫히지 않고, 그 작업의 다음 실행은 그 session_id 와 새 run_id 를
        싣는다. approval_choices 는 턴의 사람이 기억한 승인 선택이다(P43 5절). 배포된 문서만
        된다."""
        body: dict[str, Any] = {
            "session_id": session_id,
            "message": message,
            "user_sub": user_sub,
            "ephemeral": ephemeral,
        }
        if attachments:
            body["attachments"] = attachments
        if run_id is not None:
            body["run_id"] = run_id
        if untrusted:
            body["untrusted"] = True
        if task:
            body["task"] = True
        return await self.stream(
            doc,
            "POST",
            "/chat",
            action="chat",
            json=_with_grant(_with_choices(body, approval_choices), turn_grant),
            read_timeout=CHAT_READ_TIMEOUT,
            gated=False,
        )

    async def decisions(
        self,
        doc: Agent,
        session_id: str,
        *,
        user_sub: str,
        decisions: list[dict[str, Any]],
        approver: bool,
        ephemeral: bool = False,
        turn_grant: dict[str, Any] | None = None,
        run_id: str | None = None,
        approval_choices: list[dict[str, str]] | None = None,
    ) -> ProxyResponse:
        """POST /sessions/{sid}/decisions (재개 턴, /chat 과 같은 SSE). approver 면 런타임이 소유자
        검사를 건너뛰도록 X-Daegam-Approver: 1 을 붙인다. 결정자 sub 는 본문에 있다. run_id 는
        실행 세션의 결정에만 싣는다(P41 9절). approval_choices 는 `chat` 과 같다."""
        body: dict[str, Any] = {
            "decisions": decisions,
            "user_sub": user_sub,
            "ephemeral": ephemeral,
        }
        if run_id is not None:
            body["run_id"] = run_id
        return await self.stream(
            doc,
            "POST",
            session_path(session_id, "decisions"),
            action="decide",
            json=_with_grant(_with_choices(body, approval_choices), turn_grant),
            approver=approver,
            read_timeout=CHAT_READ_TIMEOUT,
            gated=False,
        )

    async def regenerate(
        self,
        doc: Agent,
        session_id: str,
        *,
        user_sub: str,
        message: str | None,
        ephemeral: bool = False,
        turn_grant: dict[str, Any] | None = None,
        approval_choices: list[dict[str, str]] | None = None,
    ) -> ProxyResponse:
        """POST /sessions/{sid}/regenerate (P36, /chat 과 같은 SSE). 마지막 턴을 다시 만들고
        message 가 있으면 그 글로 고쳐 보낸다. 소유자만 부르므로 승인자 헤더를 붙이지 않는다.
        approval_choices 는 `chat` 과 같다."""
        body = {"user_sub": user_sub, "message": message, "ephemeral": ephemeral}
        return await self.stream(
            doc,
            "POST",
            session_path(session_id, "regenerate"),
            action="regenerate",
            json=_with_grant(_with_choices(body, approval_choices), turn_grant),
            read_timeout=CHAT_READ_TIMEOUT,
            gated=False,
        )

    async def sessions(
        self,
        doc: Agent,
        *,
        user_sub: str,
        approver: bool,
        status: str | None = None,
        limit: int = 100,
        all_users: bool = False,
        q: str | None = None,
        runs: str | None = None,
    ) -> tuple[int, Any]:
        """GET /sessions. approver 가 아니면 자기 세션만 본다. q 는 제목 검색이다(P36). runs 는
        실행 세션을 넣거나(`include`) 그것만 보는(`only`) 것이다(P41 9절)."""
        params = {
            "user_sub": None if (approver and all_users) else user_sub,
            "status": status,
            "limit": limit,
            "q": q,
            "runs": runs,
        }
        return await self._get(
            doc, "/sessions", action="list sessions", params=params, approver=approver
        )

    async def session(
        self,
        doc: Agent,
        session_id: str,
        *,
        user_sub: str,
        approver: bool,
        view: str | None = None,
    ) -> tuple[int, Any]:
        """GET /sessions/{sid}. view 가 `progress` 면 체크포인트를 읽지 않는 가벼운 읽기이고(P78
        2절), 화면이 다시 읽을 간격 `poll_seconds` 를 런타임에 닿는 길에 따라 얹는다(4절). 그
        view 를 모르는 옛 런타임은 쿼리를 버리고 전체를 준다."""
        self.require_deployed(doc, action="read a session")
        self.refuse_asleep(doc, action="read a session")
        client = await self.client(doc)
        params = {"user_sub": user_sub, "view": view}
        st, body, _ = await self.request(
            doc, client, "GET", session_path(session_id), params=params, approver=approver
        )
        if view is not None and st == 200 and isinstance(body, dict):
            direct = self.is_direct(doc, client)
            body["poll_seconds"] = POLL_SECONDS_DIRECT if direct else POLL_SECONDS_TUNNELLED
        return st, body

    async def pending(
        self, doc: Agent, session_id: str, *, user_sub: str, approver: bool
    ) -> tuple[int, Any]:
        return await self._get(
            doc,
            session_path(session_id, "pending"),
            action="read pending approvals",
            params={"user_sub": user_sub},
            approver=approver,
        )

    async def cancel(
        self,
        doc: Agent,
        session_id: str,
        *,
        user_sub: str,
        approver: bool,
        turn_id: str | None = None,
    ) -> tuple[int, Any]:
        """POST /sessions/{sid}/cancel (진행 중 턴 멈추기, D65). 주체는 결정과 같아서 approver 면
        X-Daegam-Approver: 1 을 붙인다. turn_id 를 주면 런타임이 그 시도일 때만 멈춘다(P78 2절).
        모르는 옛 런타임은 그 칸을 버리고 도는 턴을 멈춘다. 런타임은 202 `{session_id}` 또는 409
        no_turn_in_progress 다.

        잠든 Agent 는 Running 파드가 남았을 때만 런타임을 부른다. 남지 않았으면 드레인 표시가 어떤
        턴도 시작하지 못하게 했으므로 런타임처럼 409 no_turn_in_progress 로 답한다 (P44 8절)."""
        if self.sleep is not None and await self.sleep.podless(doc):
            return 409, {
                "code": "no_turn_in_progress",
                "message": f"agent {doc.name} is asleep and runs no pod",
            }
        body: dict[str, Any] = {"user_sub": user_sub}
        if turn_id is not None:
            body["turn_id"] = turn_id
        return await self.call(
            doc,
            "POST",
            session_path(session_id, "cancel"),
            action="cancel a turn",
            json=body,
            approver=approver,
            gated=False,
        )

    async def _instance_call(
        self,
        doc: Agent,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """인스턴스 전체를 다루는 플랫폼 경로의 부름 하나(`/turns/cancel`, `/drain`). 배포 해제와
        삭제, 쉬기가 부르므로 상태 가드와 쉬기 검사를 거치지 않는다. `INSTANCE_CALL_TIMEOUT` 을
        넘기면 TimeoutError 다."""
        with anyio.fail_after(INSTANCE_CALL_TIMEOUT):
            client = await self.client(doc)
            st, body, _ = await self.request(doc, client, method, path, json=json, params=params)
        return st, body

    async def stop_turns(self, doc: Agent, *, user_sub: str) -> int:
        """POST /turns/cancel (P78 5절). 이 인스턴스의 진행 중 턴을 모두 멈추고, 아직 리스가 살아
        있는 턴의 수를 돌려준다. 배포 해제와 삭제가 인스턴스 토큰을 회수하기 전에 부른다. 202 가
        아니면 502 runtime_unreachable 이다."""
        st, body = await self._instance_call(
            doc, "POST", "/turns/cancel", json={"user_sub": user_sub}
        )
        if st != 202 or not isinstance(body, dict):
            raise self.error(502, "runtime_unreachable", f"POST /turns/cancel answered {st}")
        return int(body.get("running") or 0)

    async def drain(self, doc: Agent, *, user_sub: str, mark: str) -> int:
        """POST /drain (P44 7절). 런타임이 공유 색인에 이 인스턴스의 드레인 표시 mark 를 두고 살아
        있는 턴 리스의 수(`running`)를 답한다. 그 뒤로 리스를 잡는 요청은 503 `agent_draining`
        이다. 200 과 수가 아니면 502 runtime_unreachable 이다."""
        st, body = await self._instance_call(
            doc, "POST", DRAIN_PATH, json={"user_sub": user_sub, "mark": mark}
        )
        running = body.get("running") if isinstance(body, dict) else None
        if st != 200 or not isinstance(running, int):
            raise self.error(502, "runtime_unreachable", f"POST {DRAIN_PATH} answered {st}")
        return running

    async def undrain(self, doc: Agent, *, mark: str) -> None:
        """DELETE /drain (P44 7절). 드레인 표시가 mark 일 때만 런타임이 지운다. 그 사이 다른
        잠들기가 세운 표시는 남는다. 2xx 가 아니면 502 runtime_unreachable 이다."""
        st, _ = await self._instance_call(doc, "DELETE", DRAIN_PATH, params={"mark": mark})
        if not 200 <= st < 300:
            raise self.error(502, "runtime_unreachable", f"DELETE {DRAIN_PATH} answered {st}")

    async def rename_session(
        self, doc: Agent, session_id: str, *, user_sub: str, title: str
    ) -> tuple[int, Any]:
        """PATCH /sessions/{sid} (P36). 소유자만 바꾸므로 승인자 헤더를 붙이지 않는다."""
        return await self.call(
            doc,
            "PATCH",
            session_path(session_id),
            action="rename a session",
            params={"user_sub": user_sub},
            json={"title": title},
        )

    async def delete_session(
        self, doc: Agent, session_id: str, *, user_sub: str
    ) -> tuple[int, Any]:
        """DELETE /sessions/{sid} (P36). 이름 바꾸기와 같은 주체 규칙이다."""
        return await self.call(
            doc,
            "DELETE",
            session_path(session_id),
            action="delete a session",
            params={"user_sub": user_sub},
        )

    async def session_files(
        self, doc: Agent, session_id: str, *, user_sub: str, approver: bool
    ) -> tuple[int, Any]:
        """GET /sessions/{sid}/files (P36). 세션 상세와 같은 주체 규칙이고, 런타임이 쿼리의 sub 를
        세션 소유자와 직접 대조한다. approver 면 승인자 헤더를 붙인다."""
        return await self._get(
            doc,
            session_path(session_id, "files"),
            action="list session files",
            params={"user_sub": user_sub},
            approver=approver,
        )

    async def session_file(
        self, doc: Agent, session_id: str, path: str, *, user_sub: str, approver: bool
    ) -> ProxyResponse:
        """GET /sessions/{sid}/files/{path} (P36). 파일 바이트를 스트림으로 연다. path 는 앞 `/` 를
        뗀 파일 경로이고 조각마다 다시 인코딩한다(`file_segments`). 주체 규칙은 목록과 같다."""
        return await self.stream(
            doc,
            "GET",
            session_path(session_id, "files", file_segments(path)),
            action="download a session file",
            params={"user_sub": user_sub},
            approver=approver,
        )

    async def memories(self, doc: Agent, *, user_sub: str) -> tuple[int, Any]:
        """GET /memories (P36). 호출자 자신의 장기 기억 파일 목록이다. 런타임이 늘 쿼리의 sub 로
        네임스페이스를 고르므로 승인자 헤더를 붙이지 않는다."""
        return await self._get(
            doc, "/memories", action="read memories", params={"user_sub": user_sub}
        )

    async def memory_file(self, doc: Agent, key: str, *, user_sub: str) -> tuple[int, Any]:
        """GET /memories/files/{key} (P36). 주체 규칙은 목록과 같다."""
        return await self._get(
            doc, memory_file_path(key), action="read a memory", params={"user_sub": user_sub}
        )

    async def delete_memory_file(self, doc: Agent, key: str, *, user_sub: str) -> tuple[int, Any]:
        """DELETE /memories/files/{key} (P36). 그 파일 하나만 지운다. 주체 규칙은 목록과 같다."""
        return await self.call(
            doc,
            "DELETE",
            memory_file_path(key),
            action="delete a memory",
            params={"user_sub": user_sub},
        )

    async def metrics(self, doc: Agent) -> tuple[int, Any]:
        return await self._get(doc, "/metrics", action="read metrics")
