"""문서 하나를 뽑는 하위 프로세스 (D61). `daegam-docparse` 서버가 요청마다 하나씩 띄운다.

차례가 계약이다. 먼저 스스로를 가두고(`sandbox.confine`), 준비 바이트 하나를 표준 출력에 낸 뒤에야
표준 입력에서 요청 한 줄과 문서를 읽는다. 결과는 표준 출력의 JSON 한 덩이다. 파서는 import 할 때
읽어 두므로 가둔 뒤에 새 파일을 많이 열지 않는다.

표준 입력과 출력은 서버가 쥔 소켓 쌍의 한 끝이다(`sandbox.spawn`). 파일처럼 읽고 쓰되, 읽기 전에는
select 로 읽을 것을 기다린다(`sandbox.read_document`).

할 일(`task`)은 셋이다. `extract` 가 본래 일이고 `probe`·`hold` 는 자기 점검(`selfcheck`)이 쓴다.
요청 줄은 서버가 만들고 클라이언트는 형식과 목적만 고르므로 밖에서 `probe`·`hold` 를 부르는 길은
없다.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from daegam.documents.extract import extract
from daegam.documents.formats import MAX_DOCUMENT_BYTES
from daegam.documents.result import ExtractError
from daegam.documents.sandbox import READY, confine, dumpable, read_document, wait_readable

MAX_REQUEST_LINE = 4096
# `hold` 가 서버의 kill 을 기다리는 시간. 그보다 먼저 끝나면 그룹 죽이기를 확인하지 못한다
HOLD_SECONDS = 600.0
HELD = b"H"
# 이웃 하위 프로세스에서 열어 보는 자리. 표준 입력과 출력이다
STDIO_FDS = (0, 1)


def _extract(request: dict[str, Any], stdin: BinaryIO) -> dict[str, Any]:
    data = read_document(stdin, MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ExtractError("extract_failed", "the document is larger than the limit")
    return extract(str(request.get("format")), data, purpose=str(request.get("purpose"))).to_dict()


def _try_fork(linger: float) -> bool:
    """fork 가 되는가. 되면 자식은 `linger` 초 살다 끝난다."""
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid == 0:
        time.sleep(linger)
        os._exit(0)
    return True


def _open_access(path: str) -> str:
    """다른 프로세스의 `/proc/<pid>/...` 자리를 열 수 있는가. opened·denied·no_device·missing 이다.
    no_device 는 소켓이라 그 길로 열리지 않은 것이다(ENXIO). 권한은 열 때 보므로 읽지 않는다."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError:
        return "denied"
    except OSError as e:
        return "no_device" if e.errno == errno.ENXIO else "missing"
    os.close(fd)
    return "opened"


def _probe(request: dict[str, Any], _stdin: BinaryIO) -> dict[str, Any]:
    """가둔 뒤의 사실. 부모와 서버(기본은 컨테이너의 PID 1)의 environ 과 이웃 하위 프로세스
    (`sibling_pid`)의 표준 입출력을 열어 보고 fork 를 시도한다."""
    targets = {os.getppid(), int(request.get("server_pid") or 1)} - {os.getpid()}
    forked = _try_fork(0)
    if forked:
        os.wait()
    return {
        "fork_blocked": not forked,
        "dumpable": dumpable(),
        "environ": {str(pid): _open_access(f"/proc/{pid}/environ") for pid in sorted(targets)},
        "sibling_stdio": _stdio_access(int(request.get("sibling_pid") or 0)),
        "env_keys": sorted(os.environ),
    }


def _stdio_access(pid: int) -> dict[str, str]:
    """이웃 하위 프로세스의 표준 입출력을 열 수 있는가. 이웃이 없으면(0) 빈 dict 다."""
    if not pid:
        return {}
    return {str(fd): _open_access(f"/proc/{pid}/fd/{fd}") for fd in STDIO_FDS}


def _hold(_request: dict[str, Any], _stdin: BinaryIO) -> dict[str, Any]:
    """자손을 남기려 해 보고 멈춰 선다. 서버가 그룹째 죽일 때까지 돌아오지 않는다."""
    _try_fork(HOLD_SECONDS)
    sys.stdout.buffer.write(HELD)
    sys.stdout.buffer.flush()
    time.sleep(HOLD_SECONDS)
    return {}


TASKS: dict[str, Callable[[dict[str, Any], BinaryIO], dict[str, Any]]] = {
    "extract": _extract,
    "probe": _probe,
    "hold": _hold,
}


def _answer(stdin: BinaryIO) -> dict[str, Any]:
    wait_readable(stdin)
    try:
        request = json.loads(stdin.readline(MAX_REQUEST_LINE))
        task = TASKS[request["task"]]
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "code": "extract_failed", "detail": "malformed worker request"}
    try:
        return {"ok": True, "result": task(request, stdin)}
    except ExtractError as e:
        return {"ok": False, "code": e.code, "detail": e.detail}


def main() -> int:
    confine()
    out = sys.stdout.buffer
    out.write(READY)
    out.flush()
    out.write(json.dumps(_answer(sys.stdin.buffer)).encode())
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
