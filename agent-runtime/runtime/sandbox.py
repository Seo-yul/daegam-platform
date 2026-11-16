"""코드 실행 샌드박스 백엔드다(P40 1절·5절·6절, D95).

기능 code_execution 이 든 문서면 파일 백엔드의 기본이 세션마다의 샌드박스 파드다(compile.py
`build_backend`). 파드는 플랫폼이 만든다. 런타임은 플랫폼의 기계 경로(파드 env `DAEGAM_SANDBOX_URL`,
인스턴스 토큰)로 세션의 샌드박스를 열고, 찾고, 쓰기를 알리고, 닫는다. 그 답의 주소와 토큰으로 파드의
서버(`sandbox/`)를 바로 부른다.

- **세션마다.** 백엔드 하나가 이 파드의 모든 세션과 두 그래프(임시 턴 그래프 포함)를 섬긴다. 상태에
  세션을 두지 않고 호출마다 실행 설정의 `thread_id` 로 캐시 `{세션 → SandboxRef}` 를 찾는다.
  서브에이전트도 같은 스레드다. 그래프 밖의 일(세션 파일, 닫기)은 세션 id 를 받는 `Sandboxes` 의
  메서드다. `id` 는 지금 세션의 샌드박스 id 이고 실행 밖에서는 오류다.
- **비동기와 멈춤.** 비동기 메서드만 쓴다. 동기 메서드는 오류다. 턴이 멈추면(중지, 파드 종료, 턴
  시한) 호출의 태스크가 취소되고 연결이 닫혀, 서버가 그 명령의 프로세스 묶음을 죽인다.
- **시한.** 명령의 timeout 은 300초와 턴의 남은 시간 가운데 작은 것으로 자르고(주지 않으면 60초),
  열기의 기다림은 턴의 남은 시간이다. execute 의 설명이 그것을 말한다(`EXECUTE_DESCRIPTION`).
- **믿지 않는다.** 파드의 답은 믿지 않는다. 연결은 호출마다 새로 열고(옛 주소의 연결을 다시 쓰지
  않는다), 토큰이나 데이터를 보내기 전에 `/proof` 로 증명을 받는다(토큰 sha256 으로 건 HMAC). 본문은
  호출마다의 상한까지만 읽고, 호출마다 시한을 두며, 모양을 확인하고, 끊긴 호출은 다시 하지 않는다.
  같은 이유로 연결이 끊기거나 증명이 틀리면 캐시를 버린다.
- **회수와 잃음.** 끊긴 호출은 찾기로 까닭을 읽는다. 기록이 닫혔으면 그 까닭(회수, 쉬어서 지움,
  잃음)의 문장이고, 컨테이너가 메모리로 죽었으면 "파일은 남았다" 다. 쓰기 표시나 찾기가 닫힌 기록을
  알리면 다음 호출은 돌지 않고 그 문장을 답하며, 그다음 호출이 새 세대를 연다. 같은 세션을 함께 열던
  다른 열기가 실패해 닫은 기록의 404 도 잃음과 같다. 모든 문장은 모델에게 가는 영어다.
- **넣기.** 샌드박스를 연 뒤와 첨부가 온 뒤의 첫 호출 전에 세션의 첨부를
  `/workspace/uploads/` 로 올린다. 원본이 있으면 원본을, 없으면 글이나 이미지를 올린다. Skill 은
  `/workspace/skills/` 로 한 번 올린다.
- **쓰기 표시.** 턴이 열릴 때와 턴이 도는 동안 1분마다 세션의 샌드박스를 플랫폼에 알린다
  (`Sandboxes.turn`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox
from langgraph.config import get_config
from langgraph.runtime import get_runtime

from runtime.config import AgentConfig, ConfigError
from runtime.turn_context import context_field
from runtime.uploads import KIND_IMAGE, KIND_ORIGINAL, KIND_TEXT, StoredUpload, TurnUploads

log = logging.getLogger("daegam.agent-runtime.sandbox")

SANDBOX_URL_ENV = "DAEGAM_SANDBOX_URL"
# 샌드박스 서버의 API 판이다(`sandbox/daegam_sandbox/__init__.py` API_VERSION)
SERVER_API = 1
WORKSPACE = "/workspace"
UPLOADS_DIR = f"{WORKSPACE}/uploads"
SKILLS_DIR = f"{WORKSPACE}/skills"
# 세션 파일 목록이 빼는 작업 공간 맨 위 이름이다. 둘 다 넣기가 올린 사본이다
# 넣기의 표지가 사는 곳이다. 작업 공간 안이라 세대마다 비어서 시작하고, 다른 레플리카와 다시 뜬
# 파드도 같은 표지를 본다
MARKER_DIR = f"{WORKSPACE}/.daegam/copied"
SKILLS_MARKER = "skills"
COPIED_DIRS = ("uploads", "skills", ".daegam")

DEFAULT_COMMAND_SECONDS = 60
MAX_COMMAND_SECONDS = 300
# 이보다 남은 시간이 짧으면 명령을 보내지 않는다
MIN_COMMAND_SECONDS = 1
# 명령 호출이 서버의 timeout 보다 더 기다리는 몫이다. 서버는 시한에 묶음을 죽이고 답한다
CALL_SLACK_SECONDS = 15.0
CONNECT_SECONDS = 5.0
FILE_CALL_SECONDS = 60.0
SMALL_CALL_SECONDS = 15.0
# 플랫폼 경로의 시한이다. 열기는 기다림에 이것을 더한다
PLATFORM_SECONDS = 15.0
TOUCH_INTERVAL_SECONDS = 60.0
# 끊긴 호출 뒤 찾기를 다시 하는 시간과 간격이다. kubelet 은 컨테이너가 죽은 것을 1초쯤 뒤에
# 알리므로, 바로 읽으면 다시 뜬 것도 메모리 초과도 아직 보이지 않는다. 호출의 남은 시간을 넘지
# 않는다
RESTART_WAIT_SECONDS = 4.0
RESTART_POLL_SECONDS = (0.25, 0.5, 1.0, 2.0)

MIB = 1024 * 1024
# 파드 답 본문의 상한이다. 명령 출력은 서버가 1 MiB 에서 자르고 JSON 이스케이프가 그것을 여섯 배까지
# 불린다. 파일 내려받기는 한 번에 32 MiB 이고 base64 가 4/3 배다
EXECUTE_BODY_LIMIT = 8 * MIB
FILES_BODY_LIMIT = 48 * MIB
LIST_BODY_LIMIT = 2 * MIB
SMALL_BODY_LIMIT = 64 * 1024
PLATFORM_BODY_LIMIT = 64 * 1024
# 답의 머리 줄 하나의 상한이다
HEAD_LINE_LIMIT = 8 * 1024
# 올리기 한 번의 상한이다. 서버의 본문 상한(48 MiB)을 base64 로 넘지 않는다
UPLOAD_BATCH_FILES = 64
UPLOAD_BATCH_BYTES = 24 * MIB

# 쓰던 작업 공간이 있었다는 뜻이 아닌 닫힘의 까닭이다. 열다 실패했거나 상한에 걸린 기록이다
NEVER_USED_PREFIXES = ("limit_", "failed", "conflict", "image_mismatch", "check_done")

PROMPT_LINE = (
    "\n\nYou have a workspace for this conversation. The file tools and execute use the same "
    f"files. Write files under {WORKSPACE}; every other path outside it is read-only. Files the "
    "user attached are in /uploads/: the file tools read their extracted text, and commands read "
    "the original files. The workspace has no network access, and it is deleted with its files "
    "after 30 minutes without use or when this conversation closes."
)
"""코드 실행이 켜진 메인과 모든 서브에이전트의 프롬프트에 붙는 고정 문구다(1절)."""

EXECUTE_DESCRIPTION = (
    "Runs a shell command in this conversation's workspace and returns combined stdout/stderr "
    "with the exit code (output over 1 MiB is truncated).\n\n"
    "Usage:\n"
    f"- The working directory is {WORKSPACE}. Write files there; the rest of the file system is "
    "read-only. Original attachments are in /uploads/ and skills in /skills/.\n"
    "- Python 3 with numpy, pandas, matplotlib, openpyxl, python-docx, pypdf and pillow is "
    "installed. There is no network access, so packages cannot be installed.\n"
    f"- Commands stop after {DEFAULT_COMMAND_SECONDS} seconds unless you pass timeout, which can "
    f"be at most {MAX_COMMAND_SECONDS} seconds and never more than the time left in this turn.\n"
    "- Use the grep and glob tools to search, and read_file rather than cat/head/tail."
)
"""execute 의 설명이다. HarnessProfile 의 설명 바꾸기로 기본 설명을 갈음한다(compile.py)."""

SYNC_REFUSED = "the sandbox backend is async only"

GONE_MESSAGES = {
    "idle": "The workspace was deleted after 30 minutes without use, together with its files.",
    "reclaimed": (
        "The workspace was reclaimed for another conversation of the same user, together with "
        "its files."
    ),
}
LOST_MESSAGE = "The workspace was lost, together with its files."
LOST_BECAUSE = "The workspace was lost ({reason}), together with its files."
NEXT_MESSAGE = (
    " The next file or execute call opens a new, empty workspace, and the files attached to this "
    "conversation are copied into it again."
)
OOM_KEPT = (
    "The workspace ran out of memory and restarted, which stopped the command. The files in "
    f"{WORKSPACE} are kept. Use less memory and try again."
)
RESTARTING = (
    f"The workspace is restarting. The files in {WORKSPACE} are kept. Try again in a moment."
)
BROKEN = (
    "The connection to the workspace broke during the call, so it may or may not have run. "
    "Check the files before you repeat it."
)
LIMIT_MESSAGES = {
    "person": (
        "This user already has the most workspaces open at once. Code execution is unavailable "
        "until another of the user's conversations frees its workspace."
    ),
    "agent": "This Agent already has the most workspaces open at once. Try again later.",
    "run": "This Agent's background runs already hold all their workspaces. Try again later.",
    "tenant": "This tenant already has the most workspaces open at once. Try again later.",
    "cluster": "The cluster has no room for another workspace right now. Try again later.",
}
GATE_MESSAGE = (
    "Code execution is unavailable on this cluster ({reason}). Tell the user; a platform admin "
    "has to fix it."
)
STARTING = "The workspace is still starting. Try the call again in a moment."
OPEN_FAILED = (
    "The workspace could not start ({reason}). Try again once; if it fails again, tell the user."
)
POLICY_MESSAGE = (
    "The workspace cannot open because the network policy {policy} in this namespace would let it "
    "reach the network. Tell the user; a platform admin has to fix it."
)
OFF_MESSAGE = "Code execution is off for this Agent."
BUSY = "There are too many workspace requests right now. Try again in a moment."
NO_TIME = "This turn has no time left to run a command."
UNAVAILABLE = "The workspace service is unavailable right now ({detail}). Try again in a moment."
REFUSING = (
    "The workspace stopped accepting commands because its isolation check failed, and it is "
    "being deleted with its files."
)
WRONG_SERVER = "The workspace runs a server this runtime does not support (API {api})."
REFUSED_CALL = "The workspace refused the call: {detail}"
SPAWN_FAILED = "The command could not start: {detail}"
TIMED_OUT = "\n[The command was stopped after {seconds:g} seconds]"
CANCELLED = "\n[The command was cancelled]"


class SandboxProblem(Exception):
    """호출이 돌지 못했거나 끝을 모른다. message 는 모델에게 Tool 오류로 가는 영어 문장이다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Broken(Exception):
    """파드와의 연결이 끊겼거나, 증명이 틀렸거나, 답의 모양이 틀렸다. 다시 하지 않는다."""


@dataclass(frozen=True)
class SandboxRef:
    """열린 샌드박스 하나다. 플랫폼의 열기·찾기 답에서 온다."""

    sandbox_id: str
    generation: int
    host: str
    port: int
    # 로그와 오류에 실리지 않게 repr 에서 뺀다(Security)
    token: str = field(repr=False)
    unchecked: bool = False
    # 이 답을 받을 때의 컨테이너 재시작 수. 끊긴 호출 뒤 다시 떴는지를 이것과 맞대 본다
    restarts: int = 0


@dataclass
class _Synced:
    """한 세대에 올린 것이다. 세대가 바뀌면 처음부터 다시 올린다."""

    generation: int
    uploads: set[str] = field(default_factory=set)
    skills: bool = False


def open_sandboxes(
    config: AgentConfig,
    url: str | None,
    token: str | None,
    *,
    skills_root: Path | None = None,
) -> Sandboxes | None:
    """코드 실행 문서의 샌드박스 클라이언트다. 꺼진 문서는 None 이다. 켜졌는데 메인 파일 Tool 이
    꺼졌거나(9절) 기계 경로의 주소가 없으면 ConfigError 로 기동을 막는다."""
    if config.code_execution is None:
        return None
    if not config.filesystem:
        raise ConfigError(
            "code_execution_filesystem_disabled",
            "code execution needs the main agent's builtin_tools.filesystem on",
        )
    if not url:
        raise ConfigError(
            "sandbox_url_missing", f"code execution is on but {SANDBOX_URL_ENV} is empty"
        )
    return Sandboxes(PlatformSandboxes(url, token), skills_root=skills_root)


def remaining(deadline: float | None) -> float:
    """턴의 남은 초다. 시한을 모르면(그래프 밖) 플랫폼의 상한까지 기다린다."""
    if deadline is None:
        return float(MAX_COMMAND_SECONDS)
    return max(0.0, deadline - time.monotonic())


def command_seconds(timeout: int | None, deadline: float | None) -> float:
    """명령의 시한이다(1절). 주지 않으면 60초, 0 이면 상한이고, 300초와 턴의 남은 시간에서 자른다.
    남은 시간이 1초보다 짧으면 보내지 않는다."""
    wanted = DEFAULT_COMMAND_SECONDS if timeout is None else (timeout or MAX_COMMAND_SECONDS)
    left = remaining(deadline)
    if left < MIN_COMMAND_SECONDS:
        raise SandboxProblem(NO_TIME)
    return float(max(MIN_COMMAND_SECONDS, min(wanted, MAX_COMMAND_SECONDS, left)))


def gone_message(reason: str | None) -> str | None:
    """닫힌 기록의 까닭에서 모델에게 줄 문장이다. 쓰던 작업 공간이 아니었으면(열다 실패, 상한)
    None 이다."""
    if not reason or reason.startswith(NEVER_USED_PREFIXES):
        return None
    head = GONE_MESSAGES.get(reason) or (
        LOST_MESSAGE if reason == "lost" else LOST_BECAUSE.format(reason=reason)
    )
    return head + NEXT_MESSAGE


# --- 플랫폼의 기계 경로 ----------------------------------------------------------------------


class _PlatformError(Exception):
    """플랫폼 경로의 거절이다. extra 는 본문 detail 의 칸 전부다(`cap`, `reason`,
    `closed_reason`)."""

    def __init__(self, status: int, code: str, extra: dict[str, Any]) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


class PlatformSandboxes:
    """플랫폼의 기계 경로(`api/sandboxes.py`)다. 인스턴스 토큰을 Bearer 로 싣는다."""

    def __init__(self, url: str, token: str | None, http: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = http
        self._owns_http = http is None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(
        self, method: str, path: str, *, timeout: float, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client().request(
                method, f"{self.url}{path}", headers=self._headers, timeout=timeout, **kwargs
            )
        except httpx.HTTPError as e:
            raise SandboxProblem(UNAVAILABLE.format(detail=type(e).__name__)) from None
        body = _platform_body(response)
        if response.status_code == 200:
            return body
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
        code = str(detail.get("code") or response.status_code)
        raise _PlatformError(response.status_code, code, detail)

    async def open(self, session_id: str, wait: float) -> dict[str, Any]:
        body = {"session_id": session_id, "wait_seconds": round(wait, 1)}
        return await self._send("POST", "", json=body, timeout=wait + PLATFORM_SECONDS)

    async def lookup(self, session_id: str) -> dict[str, Any]:
        params = {"session_id": session_id}
        return await self._send("GET", "", params=params, timeout=PLATFORM_SECONDS)

    async def touch(self, session_id: str) -> dict[str, Any]:
        body = {"session_id": session_id}
        return await self._send("POST", "/touch", json=body, timeout=PLATFORM_SECONDS)

    async def close(self, session_id: str, reason: str) -> dict[str, Any]:
        body = {"session_id": session_id, "reason": reason}
        return await self._send("POST", "/close", json=body, timeout=PLATFORM_SECONDS)


def _platform_body(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > PLATFORM_BODY_LIMIT:
        raise SandboxProblem(UNAVAILABLE.format(detail="oversized answer"))
    try:
        body = response.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def refusal_message(e: _PlatformError) -> str:
    """플랫폼의 거절을 모델에게 줄 문장으로 옮긴다(7절의 거절, 8절의 게이트)."""
    extra = e.extra
    if e.code == "sandbox_limit":
        return LIMIT_MESSAGES.get(str(extra.get("cap")), LIMIT_MESSAGES["cluster"])
    if e.code == "sandbox_gate_closed":
        return GATE_MESSAGE.format(reason=extra.get("reason") or "unchecked")
    if e.code == "sandbox_starting":
        return STARTING
    if e.code == "sandbox_open_failed":
        return OPEN_FAILED.format(reason=extra.get("reason") or "unknown")
    if e.code == "sandbox_namespace_policy":
        return POLICY_MESSAGE.format(policy=extra.get("policy") or "unknown")
    if e.code in ("sandbox_not_enabled", "not_deployed"):
        return OFF_MESSAGE
    if e.code == "sandbox_rate_limited":
        return BUSY
    if e.code == "sandbox_not_found":
        return gone_message(extra.get("closed_reason")) or gone_message("lost") or BROKEN
    return UNAVAILABLE.format(detail=e.code)


def _ref_of(answer: dict[str, Any]) -> SandboxRef | None:
    """열기·찾기 답의 샌드박스다. 준비되지 않았거나 모양이 틀리면 None 이다."""
    address = answer.get("address")
    token = answer.get("token")
    if not answer.get("ready") or not isinstance(address, str) or not isinstance(token, str):
        return None
    parts = urlsplit(address)
    if parts.scheme != "http" or not parts.hostname or parts.port is None:
        return None
    try:
        return SandboxRef(
            sandbox_id=str(answer["sandbox_id"]),
            generation=int(answer["generation"]),
            host=parts.hostname,
            port=parts.port,
            token=token,
            unchecked=bool(answer.get("unchecked")),
            restarts=int(answer.get("restarts") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _oom(answer: dict[str, Any]) -> bool:
    last = answer.get("last_termination")
    return isinstance(last, dict) and last.get("reason") == "OOMKilled"


# --- 파드의 서버 ---------------------------------------------------------------------------


class PodClient:
    """샌드박스 서버를 부르는 작은 HTTP/1.1 이다. 호출마다 연결을 새로 열어 증명부터 받는다.
    본문은 Content-Length 로만 받고 상한을 넘으면 읽지 않는다."""

    async def call(
        self,
        ref: SandboxRef,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        timeout: float,
        limit: int,
    ) -> tuple[int, dict[str, Any]]:
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ref.host, ref.port, limit=HEAD_LINE_LIMIT),
                    CONNECT_SECONDS,
                )
                try:
                    await _prove(reader, writer, ref)
                    return await _exchange(reader, writer, ref, method, path, body, limit=limit)
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        except (OSError, EOFError, TimeoutError, asyncio.LimitOverrunError, ValueError) as e:
            # 끊김, 시한, 넘친 머리 줄, 읽지 못한 답이다. 다시 하지 않는다
            raise _Broken(type(e).__name__) from None


async def _prove(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ref: SandboxRef
) -> None:
    """이 연결의 상대가 토큰의 해시를 가졌는지 본다. 틀리면 토큰을 보내지 않고 끊는다."""
    nonce = secrets.token_bytes(32)
    status, answer = await _exchange(
        reader, writer, None, "POST", "/proof", {"nonce": nonce.hex()}, limit=SMALL_BODY_LIMIT
    )
    key = hashlib.sha256(ref.token.encode("utf-8")).digest()
    expected = hmac.new(key, nonce, hashlib.sha256).hexdigest()
    proof = answer.get("proof")
    if status != 200 or not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
        raise _Broken("proof")


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ref: SandboxRef | None,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    limit: int,
) -> tuple[int, dict[str, Any]]:
    """요청 하나를 보내고 답 하나를 읽는다. ref 가 있으면 토큰을 싣는다."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    head = [
        f"{method} {path} HTTP/1.1",
        "Host: sandbox",
        "Content-Type: application/json",
        f"Content-Length: {len(data)}",
        "Connection: keep-alive",
    ]
    if ref is not None:
        head.append(f"Authorization: Bearer {ref.token}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
    await writer.drain()
    status_line = (await reader.readuntil(b"\r\n")).decode("latin-1").split(" ", 2)
    if len(status_line) < 2 or not status_line[1].isdigit():
        raise _Broken("status line")
    length: int | None = None
    while True:
        line = (await reader.readuntil(b"\r\n"))[:-2]
        if not line:
            break
        name, _, value = line.decode("latin-1").partition(":")
        if name.strip().lower() == "content-length":
            value = value.strip()
            if not value.isdigit() or len(value) > 12:
                raise _Broken("content-length")
            length = int(value)
    if length is None or length > limit:
        raise _Broken("body size")
    raw = await reader.readexactly(length)
    try:
        answer = json.loads(raw)
    except ValueError:
        raise _Broken("body") from None
    if not isinstance(answer, dict):
        raise _Broken("body")
    return int(status_line[1]), answer


# --- 세션마다의 샌드박스 ----------------------------------------------------------------------


class Sandboxes:
    """세션마다의 샌드박스와 그 호출이다. 런타임 상태(`RuntimeState.sandboxes`)에 하나 있고 백엔드와
    세션 파일, 닫기, 쓰기 표시가 함께 쓴다."""

    def __init__(
        self,
        platform: PlatformSandboxes,
        *,
        pod: PodClient | None = None,
        skills_root: Path | None = None,
        touch_interval: float = TOUCH_INTERVAL_SECONDS,
    ) -> None:
        self.platform = platform
        self.pod = pod or PodClient()
        self.skills_root = skills_root
        self.touch_interval = touch_interval
        self._refs: dict[str, SandboxRef] = {}
        self._synced: dict[str, _Synced] = {}
        # 쓰기 표시가 알린 닫힘이다. 다음 호출이 이 문장을 답하고 지운다
        self._gone: dict[str, str] = {}
        # 닫힘을 이미 알린 세션이다. 찾기가 같은 닫힘을 다시 읽어도 되풀이하지 않고 새 세대를 연다.
        # 새로 열면 지운다
        self._reported: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.platform.aclose()

    def cached(self, session_id: str) -> SandboxRef | None:
        return self._refs.get(session_id)

    def _forget(self, session_id: str, ref: SandboxRef | None = None) -> None:
        if ref is None or self._refs.get(session_id) is ref:
            self._refs.pop(session_id, None)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # -- 찾기와 열기 ------------------------------------------------------------------------

    async def ensure(self, session_id: str, *, deadline: float | None) -> SandboxRef:
        """세션의 샌드박스다. 캐시에 없으면 찾고, 없으면 연다. 닫힘을 알게 된 뒤의 첫 호출은 돌지
        않고 그 문장이다."""
        if (message := self._gone.pop(session_id, None)) is not None:
            raise SandboxProblem(message)
        if (ref := self._refs.get(session_id)) is not None:
            return ref
        async with self._lock(session_id):
            if (ref := self._refs.get(session_id)) is not None:
                return ref
            ref = await self._found(session_id)
            if ref is None:
                ref = await self._open(session_id, deadline)
            await self._check_server(ref)
            self._refs[session_id] = ref
            self._reported.discard(session_id)
            return ref

    def _report(self, session_id: str, reason: str | None) -> SandboxProblem | None:
        """닫힌 기록의 까닭을 한 번 알린다. 쓰던 작업 공간이 아니었거나 이미 알렸으면 None 이다."""
        message = gone_message(reason)
        if message is None or session_id in self._reported:
            return None
        self._reported.add(session_id)
        self._synced.pop(session_id, None)
        return SandboxProblem(message)

    async def _found(self, session_id: str) -> SandboxRef | None:
        """이미 열린 샌드박스(다른 레플리카가 열었거나 이 파드가 다시 뜨기 전에 연 것)다. 회수되거나
        잃은 것이면 그 문장을 한 번 알리고, 그다음 호출이 새 세대를 연다."""
        try:
            answer = await self.platform.lookup(session_id)
        except _PlatformError as e:
            if e.code != "sandbox_not_found":
                raise SandboxProblem(refusal_message(e)) from None
            if (problem := self._report(session_id, e.extra.get("closed_reason"))) is not None:
                raise problem from None
            return None
        return _ref_of(answer)

    async def _open(self, session_id: str, deadline: float | None) -> SandboxRef:
        wait = remaining(deadline)
        try:
            answer = await self.platform.open(session_id, wait)
        except _PlatformError as e:
            if e.code == "sandbox_not_found":
                # 함께 열던 다른 열기가 실패해 닫은 기록이다. 잃은 것으로 한 번 알리고, 그다음
                # 호출이 찾기에서 같은 닫힘을 다시 알리지 않고 새 세대를 연다
                problem = self._report(session_id, e.extra.get("closed_reason") or "lost")
                raise (problem or SandboxProblem(gone_message("lost") or BROKEN)) from None
            raise SandboxProblem(refusal_message(e)) from None
        ref = _ref_of(answer)
        if ref is None:
            raise SandboxProblem(OOM_KEPT if _oom(answer) else RESTARTING)
        return ref

    async def _check_server(self, ref: SandboxRef) -> None:
        """서버의 API 판을 본다(10절)."""
        try:
            status, answer = await self.pod.call(
                ref, "GET", "/healthz", None, timeout=SMALL_CALL_SECONDS, limit=SMALL_BODY_LIMIT
            )
        except _Broken:
            raise SandboxProblem(RESTARTING) from None
        if answer.get("api") != SERVER_API:
            raise SandboxProblem(WRONG_SERVER.format(api=answer.get("api")))
        if status != 200:
            raise SandboxProblem(RESTARTING)

    async def _why_broken(
        self,
        session_id: str,
        ref: SandboxRef,
        *,
        report: bool = True,
        deadline: float | None = None,
    ) -> SandboxProblem:
        """끊긴 호출의 까닭이다. 캐시를 버리고 찾기로 읽는다. 호출은 다시 하지 않는다. report 가
        거짓이면(세션 파일, 그래프 밖) 모델에게 한 번만 알릴 닫힘을 쓰지 않고 그냥 끊김이다.

        컨테이너가 죽은 바로 뒤의 찾기는 아직 준비됐고 재시작 수도 그대로라고 답한다(kubelet 이
        늦게 안다). 그래서 재시작 수가 늘 때까지 RESTART_WAIT_SECONDS 와 호출의 남은 시간 가운데
        짧은 동안 간격을 늘리며 다시 찾는다. 메모리 초과로 다시 떴으면 파일이 남았다고 알린다."""
        self._forget(session_id, ref)
        if not report:
            return SandboxProblem(BROKEN)
        end = time.monotonic() + min(RESTART_WAIT_SECONDS, remaining(deadline))
        naps = iter(RESTART_POLL_SECONDS)
        while True:
            try:
                answer = await self.platform.lookup(session_id)
            except _PlatformError as e:
                if e.code == "sandbox_not_found":
                    problem = self._report(session_id, e.extra.get("closed_reason") or "lost")
                    return problem or SandboxProblem(BROKEN)
                return SandboxProblem(BROKEN)
            except SandboxProblem:
                return SandboxProblem(BROKEN)
            restarted = int(answer.get("restarts") or 0) > ref.restarts
            nap = next(naps, RESTART_POLL_SECONDS[-1])
            if restarted or time.monotonic() + nap > end:
                break
            await asyncio.sleep(nap)
        # 다시 뜨기 전이어도 지금 상태가 메모리 초과로 끝난 것이면(준비되지 않음) 파일은 남는다
        if _oom(answer) and (restarted or answer.get("ready") is not True):
            return SandboxProblem(OOM_KEPT)
        return SandboxProblem(BROKEN)

    # -- 호출 -------------------------------------------------------------------------------

    async def _pod(
        self,
        session_id: str,
        ref: SandboxRef,
        path: str,
        body: dict[str, Any],
        *,
        timeout: float,
        limit: int,
        report: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        try:
            status, answer = await self.pod.call(
                ref, "POST", path, body, timeout=timeout, limit=limit
            )
        except _Broken:
            raise await self._why_broken(
                session_id, ref, report=report, deadline=deadline
            ) from None
        if status == 200:
            return answer
        error = str(answer.get("error") or status)
        if status == 503 and error == "refusing":
            self._forget(session_id, ref)
            raise SandboxProblem(REFUSING + NEXT_MESSAGE)
        if status == 503 and error == "spawn_failed":
            raise SandboxProblem(SPAWN_FAILED.format(detail=answer.get("detail") or error))
        if status == 400:
            raise SandboxProblem(REFUSED_CALL.format(detail=answer.get("detail") or error))
        raise await self._why_broken(session_id, ref, report=report, deadline=deadline)

    async def execute(
        self,
        session_id: str,
        command: str,
        *,
        timeout: int | None,
        deadline: float | None,
        uploads: TurnUploads | None = None,
    ) -> ExecuteResponse:
        ref = await self.ensure(session_id, deadline=deadline)
        await self._sync(session_id, ref, uploads)
        # 열기와 넣기가 쓴 시간을 뺀 남은 시간으로 자른다. 느린 당기기 뒤에도 턴 시한 대신 명령의
        # 시한으로 멈춘다
        seconds = command_seconds(timeout, deadline)
        answer = await self._pod(
            session_id,
            ref,
            "/execute",
            {"command": command, "timeout": seconds},
            timeout=seconds + CALL_SLACK_SECONDS,
            limit=EXECUTE_BODY_LIMIT,
            deadline=deadline,
        )
        output, exit_code = answer.get("output"), answer.get("exit_code")
        if not isinstance(output, str) or not isinstance(exit_code, int):
            raise SandboxProblem(BROKEN)
        if answer.get("timed_out") is True:
            output += TIMED_OUT.format(seconds=seconds)
        elif answer.get("cancelled") is True:
            output += CANCELLED
        return ExecuteResponse(
            output=output, exit_code=exit_code, truncated=answer.get("truncated") is True
        )

    async def upload(
        self,
        session_id: str,
        files: Sequence[tuple[str, bytes]],
        *,
        deadline: float | None,
        uploads: TurnUploads | None = None,
    ) -> list[FileUploadResponse]:
        ref = await self.ensure(session_id, deadline=deadline)
        await self._sync(session_id, ref, uploads)
        return await self._upload(session_id, ref, files)

    async def _upload(
        self, session_id: str, ref: SandboxRef, files: Sequence[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for batch in _batches(files):
            body = {
                "files": [
                    {"path": path, "content_b64": base64.b64encode(data).decode("ascii")}
                    for path, data in batch
                ]
            }
            answer = await self._pod(
                session_id,
                ref,
                "/files/upload",
                body,
                timeout=FILE_CALL_SECONDS,
                limit=SMALL_BODY_LIMIT * 4,
            )
            results = answer.get("results")
            if not isinstance(results, list) or len(results) != len(batch):
                raise SandboxProblem(BROKEN)
            out.extend(
                FileUploadResponse(path=path, error=_error_of(result))
                for (path, _), result in zip(batch, results, strict=True)
            )
        return out

    async def download(
        self,
        session_id: str,
        paths: Sequence[str],
        *,
        deadline: float | None,
        uploads: TurnUploads | None = None,
    ) -> list[FileDownloadResponse]:
        ref = await self.ensure(session_id, deadline=deadline)
        await self._sync(session_id, ref, uploads)
        return await self._download(session_id, ref, paths)

    async def _download(
        self, session_id: str, ref: SandboxRef, paths: Sequence[str], *, report: bool = True
    ) -> list[FileDownloadResponse]:
        answer = await self._pod(
            session_id,
            ref,
            "/files/download",
            {"paths": list(paths)},
            timeout=FILE_CALL_SECONDS,
            limit=FILES_BODY_LIMIT,
            report=report,
        )
        results = answer.get("results")
        if not isinstance(results, list) or len(results) != len(paths):
            raise SandboxProblem(BROKEN)
        out: list[FileDownloadResponse] = []
        for path, result in zip(paths, results, strict=True):
            error = _error_of(result)
            content = None
            if error is None:
                content = _decoded(result.get("content_b64") if isinstance(result, dict) else None)
                error = None if content is not None else "invalid_content"
            out.append(FileDownloadResponse(path=path, content=content, error=error))
        return out

    # -- 그래프 밖(세션 파일) ----------------------------------------------------------------

    async def _open_now(self, session_id: str) -> SandboxRef | None:
        """찾기만 한다. 열지 않는다(6절). 없거나 여는 중이면 None 이다."""
        try:
            return _ref_of(await self.platform.lookup(session_id))
        except (_PlatformError, SandboxProblem):
            return None

    async def list_files(self, session_id: str) -> list[dict[str, Any]]:
        """작업 공간의 파일이다. 넣기가 올린 `uploads/`·`skills/` 는 뺀다. 샌드박스가 없으면 빈
        목록이다."""
        ref = await self._open_now(session_id)
        if ref is None:
            return []
        try:
            status, answer = await self.pod.call(
                ref,
                "POST",
                "/files/list",
                {"exclude": list(COPIED_DIRS)},
                timeout=SMALL_CALL_SECONDS,
                limit=LIST_BODY_LIMIT,
            )
        except _Broken:
            self._forget(session_id, ref)
            return []
        files = answer.get("files") if status == 200 else None
        return [f for f in files if _listed(f)] if isinstance(files, list) else []

    async def read_file(self, session_id: str, path: str) -> FileDownloadResponse | None:
        """작업 공간의 파일 하나다. 샌드박스가 없으면 None 이다."""
        ref = await self._open_now(session_id)
        if ref is None:
            return None
        try:
            # 모델에게 한 번만 알릴 닫힘을 세션 파일이 쓰지 않는다
            return (await self._download(session_id, ref, [path], report=False))[0]
        except SandboxProblem:
            return None

    # -- 쓰기 표시와 닫기 ---------------------------------------------------------------------

    async def touch(self, session_id: str) -> None:
        """세션의 샌드박스에 쓰기를 알린다. 다른 레플리카가 연 것도 있으므로 캐시와 상관없이 부른다.
        열린 기록이 없다는 답이면 캐시를 버리고, 쓰던 작업 공간이 닫힌 것이면 다음 호출이 그 문장을
        답한다(5절). 샌드박스를 연 적 없는 세션은 닫힌 까닭이 없어 아무것도 하지 않는다."""
        try:
            await self.platform.touch(session_id)
        except _PlatformError as e:
            if e.code == "sandbox_not_found":
                cached = self._refs.get(session_id) is not None
                self._forget(session_id)
                reason = e.extra.get("closed_reason") or ("lost" if cached else None)
                problem = self._report(session_id, reason)
                if problem is not None:
                    self._gone[session_id] = problem.message
            else:
                log.info("sandbox touch for %s refused: %s", session_id, e.code)
        except SandboxProblem as e:
            log.info("sandbox touch for %s failed: %s", session_id, e.message)

    @contextlib.asynccontextmanager
    async def turn(self, session_id: str) -> AsyncIterator[None]:
        """턴이 리스를 쥔 동안 쓰기를 알린다. 열릴 때 한 번, 그 뒤 1분마다다. 결정을 기다리는 동안은
        리스가 없어 알리지 않는다."""

        async def loop() -> None:
            while True:
                await self.touch(session_id)
                await asyncio.sleep(self.touch_interval)

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self, session_id: str, reason: str) -> None:
        """세션이 닫히거나 지워지거나 실행이 끝나 샌드박스를 닫는다(5절). 실패하면 플랫폼의 회수가
        닫는다."""
        self._forget(session_id)
        self._synced.pop(session_id, None)
        self._gone.pop(session_id, None)
        self._reported.discard(session_id)
        self._locks.pop(session_id, None)
        try:
            await self.platform.close(session_id, reason)
        except (_PlatformError, SandboxProblem) as e:
            log.info("sandbox of %s not closed (%s): %s", session_id, reason, e)

    # -- 넣기 ------------------------------------------------------------------------------

    async def _sync(self, session_id: str, ref: SandboxRef, uploads: TurnUploads | None) -> None:
        """이 세대에 아직 올리지 않은 첨부와 Skill 을 올린다(6절). 올린 것은 작업 공간의 표지
        (`MARKER_DIR/<upload_id>`, `MARKER_DIR/skills`)로 남기고 올리기 전에 표지를 본다. 그래서
        다른 레플리카나 다시 뜬 파드가 모델이 고친 사본을 다시 덮지 않는다. 메모리의 `_Synced` 는
        표지를 다시 묻지 않게 하는 캐시일 뿐이다. 하나를 올리지 못해도 호출은 이어 간다. 모델은
        올리지 못한 파일을 보지 못할 뿐이고 다음 호출이 다시 올린다."""
        synced = self._synced.get(session_id)
        if synced is None or synced.generation != ref.generation:
            synced = self._synced[session_id] = _Synced(generation=ref.generation)
        every = uploads.files if uploads else []
        pending = {u.upload_id: u for u in every if u.upload_id not in synced.uploads}
        keys = list(pending)
        if not synced.skills and self.skills_root is not None:
            keys.append(SKILLS_MARKER)
        if not keys:
            synced.skills = True
            return
        done = await self._marked(session_id, ref, keys)
        copies: dict[str, list[tuple[str, bytes]]] = {}
        for key in keys:
            if key in done:
                continue
            if key == SKILLS_MARKER:
                # 16 MiB 까지 디스크를 읽으므로 이벤트 루프 밖에서 읽는다
                copies[key] = await anyio.to_thread.run_sync(skill_files, self.skills_root)
            else:
                copies[key] = workspace_copies([pending[key]], every)
        files = [item for key in copies for item in copies[key]]
        failed = set()
        if files:
            for result in await self._upload(session_id, ref, files):
                if result.error:
                    failed.add(result.path)
                    log.warning("sandbox copy of %s failed: %s", result.path, result.error)
        copied = [key for key, items in copies.items() if not any(p in failed for p, _ in items)]
        if copied:
            await self._upload(session_id, ref, [(_marker(key), b"") for key in copied])
        for key in [*done, *copied]:
            if key == SKILLS_MARKER:
                synced.skills = True
            else:
                synced.uploads.add(key)
        if self.skills_root is None:
            synced.skills = True

    async def _marked(self, session_id: str, ref: SandboxRef, keys: Sequence[str]) -> set[str]:
        """작업 공간에 표지가 있는 것이다. 내려받기 한 번이 64 개까지라 나눠 묻는다."""
        found: set[str] = set()
        for i in range(0, len(keys), UPLOAD_BATCH_FILES):
            batch = list(keys[i : i + UPLOAD_BATCH_FILES])
            results = await self._download(session_id, ref, [_marker(k) for k in batch])
            found.update(key for key, r in zip(batch, results, strict=True) if r.error is None)
        return found


def _marker(key: str) -> str:
    return f"{MARKER_DIR}/{key}"


def workspace_copies(
    fresh: Sequence[StoredUpload], every: Sequence[StoredUpload]
) -> list[tuple[str, bytes]]:
    """첨부의 작업 공간 사본이다. 원본과 이미지는 `/workspace/uploads/<이름>`, 글은 `/workspace`
    아래 그 경로(`/uploads/<이름>.txt` 나 원본이 없는 글 형식의 `/uploads/<이름>`)다. 같은 자리에
    원본이 있으면 원본이 이긴다."""
    originals = {f"{UPLOADS_DIR}/{u.name}" for u in every if u.kind == KIND_ORIGINAL}
    out: list[tuple[str, bytes]] = []
    for upload in fresh:
        if upload.kind in (KIND_ORIGINAL, KIND_IMAGE):
            out.append((f"{UPLOADS_DIR}/{upload.name}", upload.content))
        elif upload.kind == KIND_TEXT:
            target = f"{WORKSPACE}{upload.path}"
            if target not in originals:
                out.append((target, upload.content))
    return out


def skill_files(root: Path) -> list[tuple[str, bytes]]:
    """init 이 받아 둔 Skill 파일의 작업 공간 사본이다. 링크는 따라가지 않는다."""
    out: list[tuple[str, bytes]] = []
    if not root.is_dir():
        return out
    for here, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            path = Path(here) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            out.append((f"{SKILLS_DIR}/{rel}", path.read_bytes()))
    return out


def _batches(files: Sequence[tuple[str, bytes]]) -> list[list[tuple[str, bytes]]]:
    batches: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for item in files:
        if current and (
            len(current) >= UPLOAD_BATCH_FILES or size + len(item[1]) > UPLOAD_BATCH_BYTES
        ):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item[1])
    if current:
        batches.append(current)
    return batches


def _error_of(result: Any) -> str | None:
    if not isinstance(result, dict):
        return "invalid_answer"
    error = result.get("error")
    return str(error) if error else None


def _decoded(content: Any) -> bytes | None:
    if not isinstance(content, str):
        return None
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return None


def _listed(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and str(entry["path"]).startswith(f"{WORKSPACE}/")
        and isinstance(entry.get("size"), int)
    )


# --- deepagents 백엔드 ----------------------------------------------------------------------

# 파일 Tool 의 비동기 메서드가 부르는 aexecute 다. 참이면 문제를 문장 대신 예외로 올려 그 메서드가
# 제 결과의 error 로 옮긴다. execute Tool 은 거짓이라 문장을 출력으로 받는다
_FILE_OPERATION: ContextVar[bool] = ContextVar("daegam_sandbox_file_operation", default=False)


def _session_id() -> str:
    """지금 실행의 세션이다. 그래프 밖이면 RuntimeError 다."""
    config = get_config()
    session_id = (config.get("configurable") or {}).get("thread_id")
    if not session_id:
        raise RuntimeError("the sandbox backend runs only inside a turn")
    return str(session_id)


def _turn() -> tuple[float | None, TurnUploads | None]:
    """지금 턴의 시한과 첨부다. 턴 문맥이 없으면(테스트의 그래프) 둘 다 None 이다."""
    try:
        runtime = get_runtime()
    except RuntimeError:
        return None, None
    deadline = context_field(runtime, "deadline")
    uploads = context_field(runtime, "uploads")
    return (
        deadline if isinstance(deadline, float) else None,
        uploads if isinstance(uploads, TurnUploads) else None,
    )


class SandboxBackend(BaseSandbox):
    """세션마다의 샌드박스를 deepagents 의 실행 백엔드로 보인다. 비동기 메서드만 쓴다."""

    def __init__(self, sandboxes: Sandboxes) -> None:
        self.sandboxes = sandboxes

    @property
    def id(self) -> str:
        ref = self.sandboxes.cached(_session_id())
        if ref is None:
            raise RuntimeError("this session has no open sandbox")
        return ref.sandbox_id

    # 동기 호출은 받지 않는다. 그래프의 Tool 은 비동기로 돈다(1절)
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise RuntimeError(SYNC_REFUSED)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise RuntimeError(SYNC_REFUSED)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise RuntimeError(SYNC_REFUSED)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:  # noqa: ASYNC109
        deadline, uploads = _turn()
        try:
            return await self.sandboxes.execute(
                _session_id(), command, timeout=timeout, deadline=deadline, uploads=uploads
            )
        except SandboxProblem as e:
            if _FILE_OPERATION.get():
                raise
            return ExecuteResponse(output=f"Error: {e.message}")

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        deadline, uploads = _turn()
        try:
            return await self.sandboxes.upload(
                _session_id(), files, deadline=deadline, uploads=uploads
            )
        except SandboxProblem as e:
            if _FILE_OPERATION.get():
                raise
            return [FileUploadResponse(path=path, error=e.message) for path, _ in files]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        deadline, uploads = _turn()
        try:
            return await self.sandboxes.download(
                _session_id(), paths, deadline=deadline, uploads=uploads
            )
        except SandboxProblem as e:
            return [FileDownloadResponse(path=path, error=e.message) for path in paths]

    async def _file_operation[T](
        self, run: Callable[[], Awaitable[T]], failed: Callable[[str], T]
    ) -> T:
        token = _FILE_OPERATION.set(True)
        try:
            return await run()
        except SandboxProblem as e:
            return failed(e.message)
        finally:
            _FILE_OPERATION.reset(token)

    async def als(self, path: str) -> LsResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).als(path), lambda m: LsResult(error=m)
        )

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).aread(file_path, offset, limit),
            lambda m: ReadResult(error=m),
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).awrite(file_path, content),
            lambda m: WriteResult(error=m),
        )

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).aedit(
                file_path, old_string, new_string, replace_all
            ),
            lambda m: EditResult(error=m),
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).agrep(pattern, path, glob, max_count=max_count),
            lambda m: GrepResult(error=m),
        )

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await self._file_operation(
            lambda: super(SandboxBackend, self).aglob(pattern, path), lambda m: GlobResult(error=m)
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """`BaseSandbox.delete` 의 비동기판이다. 기본은 동기 메서드를 스레드로 부른다."""

        async def run() -> DeleteResult:
            quoted = shlex.quote(file_path)
            exists = await self.aexecute(f"test -e {quoted} || test -L {quoted}")
            if exists.exit_code is not None and exists.exit_code != 0:
                return DeleteResult(error=f"Error: '{file_path}' not found")
            result = await self.aexecute(f"rm -rf {quoted}")
            if result.exit_code == 0:
                return DeleteResult(path=file_path)
            detail = result.output.strip() or "unknown error"
            return DeleteResult(error=f"Error deleting file '{file_path}': {detail}")

        return await self._file_operation(run, lambda m: DeleteResult(error=m))


__all__ = [
    "EXECUTE_DESCRIPTION",
    "PROMPT_LINE",
    "SANDBOX_URL_ENV",
    "PlatformSandboxes",
    "PodClient",
    "SandboxBackend",
    "SandboxProblem",
    "SandboxRef",
    "Sandboxes",
    "open_sandboxes",
]
