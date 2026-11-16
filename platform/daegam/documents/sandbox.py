"""하위 프로세스를 가두고, 띄우고, 거두는 자리 (D61). 서버와 자기 점검이 같은 함수를 쓴다.

모든 테넌트의 문서가 `daegam-docparse` 를 지나므로 파서 취약점 한 번이 테넌트 사이 유출이 되지 않게
요청마다 새 하위 프로세스(`worker`)에서 뽑는다.

- 띄우기(`spawn`): `sys.executable -I -m daegam.documents.worker` 를 비밀 없는 env, `close_fds`, 새
  세션으로 띄운다. `-I` 라 `PYTHON*` env, 사용자 site, 작업 디렉터리가 sys.path 에 들지 않는다. 같은
  UID 가 쓸 수 있는 `/tmp` 에 무엇을 심어도 다음 하위 프로세스가 읽지 않는다. 표준 오류는 버린다.
  문서 조각이 든 추적이 서버 로그로 새지 않게 하려는 것이다. 표준 입력과 출력은 파이프가 아니라
  소켓 쌍의 한 끝이다. 새 하위 프로세스는 가두기 전까지 dumpable 이라, 파이프였다면 파서 취약점으로
  먼저 뚫린 다른 하위 프로세스가 `/proc/<pid>/fd/0`·`1` 로 그 파이프를 다시 열어 다음 문서와 결과를
  가로챌 수 있다. 소켓은 그 길로 열리지 않는다(ENXIO).
- 가두기(`confine`): 하위 프로세스가 문서를 받기 전에 스스로 건다. `RLIMIT_AS` 768MiB, `RLIMIT_CPU`
  30초, `RLIMIT_NOFILE` 64, `RLIMIT_NPROC` 1 이고 마지막 것이 fork 와 스레드 만들기를 막는다.
  `RLIMIT_NPROC` 은 capabilities 가 없는 루트 아닌 프로세스에만 걸리므로 파드 보안 값이 그 전제다.
  `prctl(PR_SET_DUMPABLE, 0)` 은 같은 UID 가 `/proc/<pid>` 의 `fd`·`environ`·`mem` 을 읽거나 ptrace
  하는 길을 막는다. Linux 에서는 하나라도 실패하면 멈추고, 개발 호스트(macOS)는 `RLIMIT_AS` 와
  `prctl` 이 없어 되는 것만 건다.
- 읽기(`read_document`): 하위 프로세스는 표준 입력을 select 로 기다린 뒤에만 읽는다. macOS 에서는
  막히는 read 에서 잠든 하위 프로세스가 서버가 보낸 문서와 EOF 를 보고도 드물게 깨지 않는다.
- 주고받기(`run_worker`): 하위 프로세스가 준비 바이트를 낸 뒤에야 요청 한 줄과 문서를 표준 입력에
  쓴다. 그래서 가두기 전의 틈에는 문서가 없다. 결과는 표준 출력의 JSON 한 덩이이고 64MiB 를 넘으면
  읽기를 멈춘다. 30초가 넘으면 `extract_timeout`, 하위 프로세스가 죽거나(메모리 한도 포함) 모양이
  틀린 답을 내면 `extract_failed` 다.
- 거두기(`kill_group`): 끝나거나 시간이 넘으면 프로세스 그룹째 SIGKILL 이다. 가두기를 빠져나간
  자손이 있어도 함께 죽는다.

서버도 기동할 때 `set_non_dumpable` 을 부른다. 토큰은 서버의 env 에만 있고, 하위 프로세스는 서버의
`environ` 도 다른 추출의 입출력도 읽지 못한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import resource
import select
import signal
import socket
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from daegam.documents.formats import MIB
from daegam.documents.result import ExtractError

READY = b"\x01"
WORKER_ARGV = (sys.executable, "-I", "-m", "daegam.documents.worker")
# 하위 프로세스의 env. 서버의 env(토큰)를 물려주지 않는다
WORKER_ENV = {"LANG": "C.UTF-8"}
EXTRACT_TIMEOUT = 30.0
MAX_RESULT_BYTES = 64 * MIB
LIMITS = (
    (resource.RLIMIT_AS, 768 * MIB),
    (resource.RLIMIT_CPU, 30),
    (resource.RLIMIT_NOFILE, 64),
    (resource.RLIMIT_NPROC, 1),
)
# 가두기를 모두 강제하는 운영체제. 그 밖(개발 호스트)은 되는 것만 건다
STRICT = sys.platform.startswith("linux")
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
_READ_CHUNK = 64 * 1024
# 하위 프로세스가 표준 입력에 읽을 것이 왔는지 다시 보는 간격(초)
READ_POLL_SECONDS = 0.1


# -- 가두기 ---------------------------------------------------------------------------


def _prctl(option: int, value: int = 0) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(option, value, 0, 0, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}) failed")
    return result


def set_non_dumpable() -> None:
    """같은 UID 가 이 프로세스의 `/proc` 를 읽거나 ptrace 하지 못하게 한다. Linux 에서만 건다."""
    if STRICT:
        _prctl(PR_SET_DUMPABLE, 0)


def dumpable() -> int | None:
    """지금의 dumpable 값. Linux 가 아니면 None 이다."""
    return _prctl(PR_GET_DUMPABLE) if STRICT else None


def confine() -> None:
    """하위 프로세스가 문서를 받기 전에 스스로를 가둔다. 낮춘 한도는 다시 높이지 못한다."""
    for limit, value in LIMITS:
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            if STRICT:
                raise
    set_non_dumpable()


# -- 하위 프로세스의 읽기 ------------------------------------------------------------------


def wait_readable(stdin: BinaryIO) -> None:
    """읽을 것이나 EOF 가 올 때까지 select 로 기다린다. 막히는 read 에서 잠들지 않으려는 것이다.
    macOS 에서는 소켓 표준 입력의 막히는 read 에서 잠든 프로세스가, 서버가 문서를 보내고 쓰기를
    닫은 뒤에도 깨지 않는 일이 부하가 걸리면 드물게 있다. select 는 같은 소켓에서 바로 깨고, 놓쳐도
    `READ_POLL_SECONDS` 마다 다시 본다."""
    while not select.select([stdin], [], [], READ_POLL_SECONDS)[0]:
        pass


def read_document(stdin: BinaryIO, limit: int) -> bytes:
    """요청 줄 뒤의 문서를 EOF 까지, 많아야 limit 바이트 읽는다. `read1` 은 readline 이 버퍼에 먼저
    읽어 둔 앞부분부터 돌려주고, 버퍼가 비었을 때만 소켓을 한 번 읽는다."""
    chunks: list[bytes] = []
    size = 0
    while size < limit:
        wait_readable(stdin)
        chunk = stdin.read1(limit - size)  # type: ignore[attr-defined]  # BufferedReader 다
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


# -- 띄우기와 거두기 ---------------------------------------------------------------------


@dataclass
class Worker:
    """띄운 하위 프로세스와, 그 표준 입출력인 소켓의 서버 쪽 끝이다."""

    proc: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @property
    def pid(self) -> int:
        return self.proc.pid


async def spawn(argv: tuple[str, ...] = WORKER_ARGV) -> Worker:
    """하위 프로세스를 띄운다. 표준 입력과 출력은 소켓 쌍의 한 끝이고 다른 끝을 서버가 쥔다."""
    ours, theirs = socket.socketpair()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=theirs.fileno(),
            stdout=theirs.fileno(),
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(WORKER_ENV),
            close_fds=True,
            start_new_session=True,
        )
    except BaseException:
        ours.close()
        raise
    finally:
        # 하위 프로세스가 물려받았으니 서버 쪽 사본을 닫아야 그것이 끝날 때 읽기가 EOF 를 본다
        theirs.close()
    reader, writer = await asyncio.open_connection(sock=ours)
    return Worker(proc, reader, writer)


async def kill_group(worker: Worker) -> None:
    """프로세스 그룹째 SIGKILL 하고 거둔 뒤 소켓을 닫는다. 이미 없으면 조용히 넘어간다."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(worker.pid, signal.SIGKILL)
    await worker.proc.wait()
    worker.writer.close()
    with contextlib.suppress(OSError):
        await worker.writer.wait_closed()


def group_alive(pgid: int) -> bool:
    """그 프로세스 그룹에 프로세스가 남아 있는가."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def send_request(worker: Worker, request: dict[str, Any], data: bytes) -> None:
    """준비 바이트를 받은 뒤에야 요청 한 줄과 문서를 쓰고 쓰는 쪽을 닫는다."""
    if await worker.reader.read(1) != READY:
        raise ExtractError("extract_failed", "the worker exited before it was ready")
    try:
        worker.writer.write(json.dumps(request).encode() + b"\n" + data)
        await worker.writer.drain()
        worker.writer.write_eof()
    except (BrokenPipeError, ConnectionResetError) as e:
        raise ExtractError("extract_failed", "the worker closed its input") from e


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise ExtractError("extract_failed", "the extraction result is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _reply(out: bytes) -> dict[str, Any]:
    """하위 프로세스의 답. `{"ok": true, "result": {...}}` 이거나
    `{"ok": false, "code", "detail"}` 다."""
    try:
        reply = json.loads(out)
    except ValueError as e:
        raise ExtractError("extract_failed", "the worker answered with malformed JSON") from e
    if not isinstance(reply, dict):
        raise ExtractError("extract_failed", "the worker answered with malformed JSON")
    if reply.get("ok") is True and isinstance(reply.get("result"), dict):
        return reply["result"]
    raise ExtractError(str(reply.get("code") or "extract_failed"), str(reply.get("detail") or ""))


async def _exchange(worker: Worker, request: dict[str, Any], data: bytes) -> dict:
    await send_request(worker, request, data)
    out = await _read_bounded(worker.reader, MAX_RESULT_BYTES)
    if (code := await worker.proc.wait()) != 0:
        raise ExtractError("extract_failed", f"the worker exited with {code}")
    return _reply(out)


async def run_worker(
    request: dict[str, Any],
    data: bytes = b"",
    *,
    timeout: float = EXTRACT_TIMEOUT,
    argv: tuple[str, ...] = WORKER_ARGV,
) -> dict[str, Any]:
    """하위 프로세스 하나로 요청 하나를 돌린다. 끝나면 그 그룹을 거둔다."""
    worker = await spawn(argv)
    try:
        async with asyncio.timeout(timeout):
            return await _exchange(worker, request, data)
    except TimeoutError as e:
        raise ExtractError("extract_timeout", f"the extraction took more than {timeout:g}s") from e
    finally:
        await kill_group(worker)


__all__ = [
    "EXTRACT_TIMEOUT",
    "READY",
    "READ_POLL_SECONDS",
    "WORKER_ARGV",
    "WORKER_ENV",
    "Worker",
    "confine",
    "dumpable",
    "group_alive",
    "kill_group",
    "read_document",
    "run_worker",
    "send_request",
    "set_non_dumpable",
    "spawn",
    "wait_readable",
]
