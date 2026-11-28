"""다른 Agent 를 Tool 로 부른다(기능 agent_tools).

agent.json 의 `tools[]` 가운데 `source.kind` 가 `agent` 인 항목마다 Tool 하나(`ask_*`)를 만든다.
이름은 항목의 `allow` 하나, 설명과 제한 시간은 항목에 박힌 것이고 인자는 `message` 하나다. 검색
도구처럼 항목만으로 만들므로 목록을 읽지 않고, 플랫폼이 답하지 않아도 파드는 준비된다. `mode` 가
`task` 인 항목은 답을 기다리지 않고 작업을 맡기는 소스라 여기서 Tool 을 만들지 않는다
(agent_task_tools.py). 요청의 자격(`CallSender`)과 거절의 Tool 오류(`refused`)는 두 쪽이 같이 쓴다.

- **호출은 플랫폼의 실행이다.** 항목의 `url` 이 호출받는 Agent 의 호출 경로다. Tool 은
  `POST <url>` 에 `{message}` 를 보내 실행을 시작하고, 끝나지 않았으면
  `GET <url>/runs/{run_id}?wait=<초>` 로 끝날 때까지 묻는다. 답은 모두 `{run_id, status, answer?,
  code?}` 다. 요청 하나는 30초를 넘지 않으므로 플랫폼이 다시 뜨거나 앞단이 연결을 끊어도 묻기 한
  번만 잃는다. 실패한 묻기는 제한 시간까지 다시 한다. 시작은 다시 하지 않는다. 같은 메시지로 실행이
  둘 생길 수 있어서다.
- **제한 시간.** 항목의 `timeout_seconds` 와 부르는 턴의 남은 시간 가운데 짧은 쪽이다. 넘으면
  `POST <url>/runs/{run_id}/cancel` 로 실행을 취소하고(`caller_timeout`) Tool 오류로 그렇게 말한다.
  부르는 턴이 멈춰 호출이 끊길 때도 취소를 보낸다(`caller_ended`).
- **누구의 호출인가.** 턴의 위임이 그 Agent 를 대상으로 토큰을 실었으면 요청마다 그 토큰을
  Bearer 로 붙인다(turn_grants.py). 토큰이 끝났으면 보내지 않고 Tool 오류다. 위임이 없거나 그 대상이
  없는 턴은 파드의 인스턴스 토큰과 함께 세션, 턴, 세션 소유자를 머리로 알린다. 그것을 받을지는
  플랫폼이 정한다.
- **바깥 내용.** 요청마다 이 대화가 바깥 내용을 가졌는지를 머리로 알린다(outside.py 의 표시).
  모르는 문맥은 가진 것으로 알린다.
- **결과.** 끝난 실행의 답은 글이고 `run_id` 는 모델이 보지 않는 artifact 의 `structured_content`
  에 싣는다. 승인을 기다리는 실행은 고정 문장으로 곧바로 답한다. 실패, 거절, 취소와 플랫폼이 실행
  전에 거절한 호출은 그 코드를 실은 Tool 오류다. 이 Tool 의 결과는 다른 소스의 Tool 처럼
  tool_safety.py 가 다룬다. 항목에 `inside` 와 `read` 가 없으므로 늘 바깥 내용이고 쓰기다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import anyio
import httpx2
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runtime.config import AgentConfig
from runtime.trace import (
    ERROR_RATE_LIMITED,
    ERROR_REFUSED,
    TOOL_REFUSED,
    TOOL_TIMEOUT,
    signal,
)
from runtime.turn_context import context_field
from runtime.turn_grants import (
    INTERRUPTED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    UNKNOWN_TURN_MESSAGE,
    Target,
    TurnGrants,
    TurnTokenUnavailable,
    current_turn,
)

log = logging.getLogger("daegam.agent-runtime.agent-calls")

# 요청 하나의 상한(초)이다. 플랫폼은 시작과 묻기를 25초 안에 답한다
REQUEST_TIMEOUT_SECONDS = 30.0
# 묻기 하나가 플랫폼에 기다려 달라고 하는 시간(초)의 상한이다
POLL_WAIT_SECONDS = 25
# 끝나지 않았다는 답이 이보다 빨리 오면 다음 묻기까지 남은 만큼 쉰다. 기다리지 않고 답하는
# 플랫폼 앞에서 묻기가 쉬지 않고 돌지 않는다
POLL_MIN_INTERVAL_SECONDS = 1.0
# 실패한 묻기를 다시 할 때 처음 쉬는 시간과 그 상한(초)이다
POLL_RETRY_INITIAL_SECONDS = 1.0
POLL_RETRY_MAX_SECONDS = 5.0
# 취소 요청 하나의 상한(초)이다
CANCEL_TIMEOUT_SECONDS = 5.0
# 이 상태의 묻기는 다시 한다. 그 밖의 4xx 는 거절이라 호출을 끝낸다
RETRIED_STATUSES = frozenset({429})

# 플랫폼이 답하는 실행의 상태다. 아래 다섯이면 끝났고 그 밖(`starting`, `running`)은 더 묻는다
SUCCEEDED = "succeeded"
WAITING_APPROVAL = "waiting_approval"
SETTLED = frozenset({SUCCEEDED, WAITING_APPROVAL, "failed", "refused", "cancelled"})

# 취소 본문의 까닭이다. 제한 시간을 넘겼거나 부르는 턴이 멈췄다
CANCEL_TIMEOUT = "caller_timeout"
CANCEL_ENDED = "caller_ended"

# 이 대화가 바깥 내용을 가졌는가다. 값은 "1" 이나 "0" 이다
OUTSIDE_HEADER = "X-Daegam-Caller-Outside"
# 위임 토큰 없는 호출이 알리는 세션, 턴, 세션 소유자다. 소유자는 UTF-8 의 퍼센트 인코딩이다
SESSION_HEADER = "X-Daegam-Caller-Session"
TURN_HEADER = "X-Daegam-Caller-Turn"
OWNER_HEADER = "X-Daegam-Caller-Owner"

# 모델이 읽는 Tool 설명의 꼬리다. 호출받는 Agent 는 이 대화를 보지 못한다
DESCRIPTION_SUFFIX = (
    " The called Agent does not see this conversation, so put everything it needs in `message`."
)
MESSAGE_DESCRIPTION = "A self-contained request for the called Agent."

# 모델에게 가는 문구다. 다른 런타임 Tool 문구처럼 한다체다
WAITING_MESSAGE = (
    "호출받은 Agent 가 승인을 기다린다. 요청한 사용자나 승인 권한(agent:approve)이 있는 사람이 "
    "승인함에서 결정하면 그 Agent 가 이어서 실행한다. 이 호출은 그 답을 기다리지 않는다."
)
EMPTY_ANSWER_MESSAGE = "호출받은 Agent 가 빈 답을 냈다."
RUN_ENDED_MESSAGE = (
    "tool {tool} 가 부른 Agent 의 실행이 답 없이 끝났다 "
    "(status: {status}, code: {code}, run_id: {run_id})."
)
REFUSED_MESSAGE = "tool {tool} 호출이 거절됐다 (code: {code})."
START_FAILED_MESSAGE = (
    "tool {tool} 호출을 플랫폼에 보내지 못했다 ({error}). 실행이 시작됐을 수 있으니 같은 호출을 "
    "되풀이하기 전에 사용자에게 알린다."
)
BAD_ANSWER_MESSAGE = "tool {tool} 호출에 플랫폼이 알 수 없는 답을 했다."
TIMEOUT_MESSAGE = "tool {tool} 호출이 제한 시간 {seconds:g}초 안에 끝나지 않아 멈췄다."
TIMEOUT_CANCELLED = " 호출받은 Agent 의 실행도 취소했다."
TIMEOUT_NOT_CANCELLED = " 호출받은 Agent 의 실행은 취소를 확인하지 못했다."
TIMEOUT_NOT_STARTED = " 실행이 시작됐을 수 있다."

# 플랫폼이 거절한 호출의 스팬 종류다(trace.py). 다른 상태는 종류 없이 Tool 오류다
REFUSAL_KINDS = {403: ERROR_REFUSED, 429: ERROR_RATE_LIMITED}


class AgentCallError(ToolException):
    """호출이 답을 얻지 못했다. `handle_tool_error` 가 받아 모델이 보는 Tool 오류가 된다."""


class AskArgs(BaseModel):
    message: str = Field(min_length=1, description=MESSAGE_DESCRIPTION)


class CallAnswer(BaseModel):
    """호출 경로 셋의 답이다. `code` 는 답 없이 끝난 실행의 코드다."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    answer: str | None = None
    code: str | None = None

    @property
    def settled(self) -> bool:
        return self.status in SETTLED


@dataclass(frozen=True)
class Caller:
    """호출 하나가 요청마다 알리는 것이다. 호출을 시작할 때 턴 문맥에서 한 번 읽는다."""

    owner: str | None
    outside: bool

    @classmethod
    def of(cls, runtime: Any) -> Caller:
        return cls(
            owner=context_field(runtime, "owner_sub"),
            # 표시를 모르는 문맥은 바깥 내용을 가진 대화로 알린다
            outside=getattr(context_field(runtime, "outside"), "flagged", True),
        )


def _answer(response: httpx2.Response) -> CallAnswer | None:
    """답의 본문이다. JSON 이 아니거나(ValueError) 모양이 다르면 None 이다."""
    try:
        return CallAnswer.model_validate(response.json())
    except (ValueError, ValidationError):
        return None


def _refusal(response: httpx2.Response) -> tuple[str, str, int | None]:
    """플랫폼이 거절한 답의 (코드, 문장, 한도)다. 본문은 플랫폼 오류 꼴 `{detail: {code, detail,
    limit?}}` 이다. 그 꼴이 아니면 코드는 상태 코드다."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if not isinstance(detail, dict) or not isinstance(detail.get("code"), str):
        return f"http_{response.status_code}", "", None
    sentence = detail.get("detail")
    limit = detail.get("limit")
    return (
        detail["code"],
        sentence if isinstance(sentence, str) else "",
        limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
    )


@dataclass(frozen=True)
class CallSender:
    """호출받는 Agent 하나에게 가는 요청의 자격이다. target 은 턴의 위임이 토큰을 싣는 대상이다."""

    target: Target
    grants: TurnGrants
    instance_token: str | None

    def headers(self, tool: str, caller: Caller, *, started: bool) -> dict[str, str]:
        """요청 하나의 머리다. 턴의 위임이 이 대상에 토큰을 실었으면 그 지금 토큰이고(갱신이 바꾼
        토큰도 다음 요청부터 실린다) 끝났으면 `TurnTokenUnavailable` 이다. 아니면 인스턴스 토큰과
        세션, 턴, 소유자다. 누구의 턴인지 모르면 보내지 않는다. tool 은 문장에 싣는 Tool 이름이고
        started 는 그 호출이 이미 실행을 시작했는가다."""
        headers = {OUTSIDE_HEADER: "1" if caller.outside else "0"}
        calls = self.grants.current()
        if calls is not None and calls.credentials.holds(self.target):
            token = calls.credentials.token(self.target)
            if token is None:
                text = INTERRUPTED_MESSAGE if started else UNAVAILABLE_MESSAGE
                raise TurnTokenUnavailable(text.format(tool=tool))
            headers["Authorization"] = f"Bearer {token}"
            return headers
        turn = current_turn()
        if turn is None:
            raise TurnTokenUnavailable(UNKNOWN_TURN_MESSAGE)
        if self.instance_token:
            headers["Authorization"] = f"Bearer {self.instance_token}"
        headers[SESSION_HEADER] = turn.session_id
        headers[TURN_HEADER] = turn.turn_id
        if caller.owner:
            headers[OWNER_HEADER] = quote(caller.owner, safe="")
        return headers


async def refused(tool: str, response: httpx2.Response) -> AgentCallError:
    """플랫폼이 거절한 요청의 Tool 오류다. 코드와 플랫폼의 문장, 한도가 있으면 한도를 싣는다."""
    code, sentence, limit = _refusal(response)
    log.warning("%s 호출을 플랫폼이 거절했다: %s %s", tool, response.status_code, code)
    kind = REFUSAL_KINDS.get(response.status_code)
    if kind is not None:
        await signal(TOOL_REFUSED, {"tool": tool, "kind": kind})
    parts = [REFUSED_MESSAGE.format(tool=tool, code=code)]
    if sentence:
        parts.append(sentence)
    if limit is not None:
        parts.append(f"(limit: {limit})")
    return AgentCallError(" ".join(parts))


@dataclass
class AgentCall:
    """Agent 소스 하나의 호출이다."""

    name: str
    url: str
    sender: CallSender
    timeout: float
    http: Callable[[], httpx2.AsyncClient]
    poll_wait: int = POLL_WAIT_SECONDS
    poll_min_interval: float = POLL_MIN_INTERVAL_SECONDS
    retry_initial: float = POLL_RETRY_INITIAL_SECONDS

    async def run(self, message: str, runtime: Any) -> tuple[str, dict[str, Any]]:
        """실행을 시작하고 끝날 때까지 묻는다. (모델이 읽는 글, artifact)다."""
        caller = Caller.of(runtime)
        seconds = self._budget(runtime)
        ends_at = time.monotonic() + seconds
        run_id: str | None = None
        try:
            async with asyncio.timeout(seconds):
                answer = await self._start(message, caller)
                run_id = answer.run_id
                while not answer.settled:
                    answer = await self._poll(run_id, caller, ends_at)
        except TimeoutError:
            # ToolMessage 로 접히면 종류를 잃으므로 트레이서에 먼저 알린다
            await signal(TOOL_TIMEOUT, {"tool": self.name})
            raise AgentCallError(await self._timed_out(run_id, caller, seconds)) from None
        except asyncio.CancelledError:
            # 부르는 턴이 멈췄다. 위임 없는 턴은 플랫폼이 그것을 모르므로 여기서 취소를 보낸다
            if run_id is not None:
                with anyio.move_on_after(CANCEL_TIMEOUT_SECONDS, shield=True):
                    await self._cancel(run_id, caller, CANCEL_ENDED)
            raise
        return self._result(answer)

    def _budget(self, runtime: Any) -> float:
        """이 호출이 기다리는 시간(초)이다. 부르는 턴의 남은 시간이 더 짧으면 그 시간이다."""
        deadline = context_field(runtime, "deadline")
        if deadline is None:
            return self.timeout
        return max(0.0, min(self.timeout, deadline - time.monotonic()))

    def _headers(self, caller: Caller, *, started: bool) -> dict[str, str]:
        return self.sender.headers(self.name, caller, started=started)

    async def _start(self, message: str, caller: Caller) -> CallAnswer:
        """실행을 시작한다. 보내지 못했거나 플랫폼이 거절했으면 Tool 오류다. 다시 하지 않는다."""
        try:
            response = await self.http().post(
                self.url,
                json={"message": message},
                headers=self._headers(caller, started=False),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx2.HTTPError as e:
            log.warning("%s 호출을 플랫폼에 보내지 못했다: %s", self.name, type(e).__name__)
            raise AgentCallError(
                START_FAILED_MESSAGE.format(tool=self.name, error=type(e).__name__)
            ) from None
        if not response.is_success:
            raise await refused(self.name, response)
        answer = _answer(response)
        if answer is None:
            raise AgentCallError(BAD_ANSWER_MESSAGE.format(tool=self.name))
        return answer

    async def _poll(self, run_id: str, caller: Caller, ends_at: float) -> CallAnswer:
        """실행의 지금 상태를 묻는다. 연결 오류, 5xx, 429, 모양이 다른 답은 쉬었다 다시 묻는다.
        제한 시간은 부른 쪽의 `asyncio.timeout` 이 끊는다."""
        delay = self.retry_initial
        while True:
            began = time.monotonic()
            wait = max(0, min(self.poll_wait, int(ends_at - began)))
            answer = await self._poll_once(run_id, caller, wait)
            if answer is not None:
                if not answer.settled:
                    await asyncio.sleep(
                        max(0.0, self.poll_min_interval - (time.monotonic() - began))
                    )
                return answer
            await asyncio.sleep(delay)
            delay = min(delay * 2, POLL_RETRY_MAX_SECONDS)

    async def _poll_once(self, run_id: str, caller: Caller, wait: int) -> CallAnswer | None:
        """묻기 한 번이다. None 이면 다시 묻는다. 플랫폼이 거절했으면(다시 하지 않는 4xx) Tool
        오류다."""
        try:
            response = await self.http().get(
                f"{self.url}/runs/{run_id}",
                params={"wait": wait},
                headers=self._headers(caller, started=True),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx2.HTTPError as e:
            log.info("%s 호출의 실행 %s 를 묻지 못했다: %s", self.name, run_id, type(e).__name__)
            return None
        if response.is_success:
            return _answer(response)
        if 400 <= response.status_code < 500 and response.status_code not in RETRIED_STATUSES:
            raise await refused(self.name, response)
        return None

    async def _cancel(self, run_id: str, caller: Caller, reason: str) -> bool:
        """실행을 취소한다. 받아들여졌으면 참이다. 실패는 삼킨다. 실행의 제 한도와 위임의 끝이
        마지막 그물이다."""
        try:
            response = await self.http().post(
                f"{self.url}/runs/{run_id}/cancel",
                json={"reason": reason},
                headers=self._headers(caller, started=True),
                timeout=CANCEL_TIMEOUT_SECONDS,
            )
        except (httpx2.HTTPError, TurnTokenUnavailable) as e:
            log.info(
                "%s 호출의 실행 %s 를 취소하지 못했다: %s", self.name, run_id, type(e).__name__
            )
            return False
        return response.is_success

    async def _timed_out(self, run_id: str | None, caller: Caller, seconds: float) -> str:
        """제한 시간을 넘긴 호출의 문장이다. 시작된 실행은 취소하고 그 결과를 말한다."""
        text = TIMEOUT_MESSAGE.format(tool=self.name, seconds=seconds)
        if run_id is None:
            return text + TIMEOUT_NOT_STARTED
        if await self._cancel(run_id, caller, CANCEL_TIMEOUT):
            return text + TIMEOUT_CANCELLED
        return text + TIMEOUT_NOT_CANCELLED

    def _result(self, answer: CallAnswer) -> tuple[str, dict[str, Any]]:
        """끝난 실행의 Tool 결과다. 답 없이 끝났으면 그 코드의 Tool 오류다."""
        artifact = {"structured_content": {"run_id": answer.run_id, "status": answer.status}}
        if answer.status == SUCCEEDED:
            return answer.answer or EMPTY_ANSWER_MESSAGE, artifact
        if answer.status == WAITING_APPROVAL:
            return WAITING_MESSAGE, artifact
        raise AgentCallError(
            RUN_ENDED_MESSAGE.format(
                tool=self.name,
                status=answer.status,
                code=answer.code or answer.status,
                run_id=answer.run_id,
            )
        )


def tool_error(error: ToolException) -> str:
    """`handle_tool_error` 다. 문장은 이 모듈과 turn_grants.py 가 정한 것이라 그대로 모델에게
    간다."""
    return str(error)


def build_ask_tool(call: AgentCall, description: str) -> BaseTool:
    """Agent 소스 하나의 Tool 이다. ToolNode 가 `runtime` 에 턴 문맥을 넣어 주고 모델이 보는 인자는
    `message` 하나다."""

    async def ask(message: str, runtime: ToolRuntime) -> tuple[str, dict[str, Any]]:
        return await call.run(message, runtime)

    return StructuredTool.from_function(
        coroutine=ask,
        name=call.name,
        description=description + DESCRIPTION_SUFFIX,
        args_schema=AskArgs,
        handle_tool_error=tool_error,
        response_format="content_and_artifact",
    )


class AgentCalls:
    """agent.json 의 답을 기다리는 Agent 소스 전부의 Tool 이다. 기동 때 한 번 만들고 재컴파일마다
    같은 것을 넘긴다. 요청은 클라이언트 하나를 같이 쓰고, 작업을 맡기는 소스의 Tool 도 이 자격과
    클라이언트로 보낸다(`sender`, `client`)."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        grants: TurnGrants,
        instance_token: str | None,
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        self._http = http
        self._owns_http = http is None
        self._grants = grants
        self._instance_token = instance_token
        self.tools_by_ref: dict[str, list[BaseTool]] = {}
        for source in config.ask_sources:
            call = AgentCall(
                name=source["allow"][0],
                url=str(source["url"]).rstrip("/"),
                sender=self.sender(source),
                timeout=float(source["timeout_seconds"]),
                http=self.client,
            )
            self.tools_by_ref[source["ref"]] = [build_ask_tool(call, str(source["description"]))]

    def sender(self, source: dict[str, Any]) -> CallSender:
        """Agent 소스 하나에게 가는 요청의 자격이다."""
        target = (source["source"]["kind"], source["source"]["id"])
        return CallSender(target, self._grants, self._instance_token)

    def client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = httpx2.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        """직접 만든 클라이언트만 닫는다. 주입받은 것은 준 쪽이 닫는다."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
