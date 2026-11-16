"""`daegam-docparse` 의 가두기를 실제 파드에서 확인하는 자기 점검 (D61).

    kubectl exec deploy/daegam-docparse -- python -m daegam.documents.selfcheck [서버 pid]

서버와 같은 함수(`sandbox.spawn`·`run_worker`·`kill_group`)로 하위 프로세스를 띄워 다섯 칸을 JSON
한 줄로 내고, 모두 참이면 0 으로 끝난다.

- `fork_blocked`: 가둔 하위 프로세스의 fork 가 실패한다.
- `dumpable_zero`: 가둔 하위 프로세스의 dumpable 이 0 이다.
- `server_environ_unreadable`: 하위 프로세스가 부모(이 점검. 서버와 같은 함수로 dumpable 을
  내렸다)와 서버의 `environ` 을 읽지 못한다. 서버는 파드 안에서 PID 1 이고 인자로 바꿀 수 있다.
- `sibling_stdio_unopenable`: 가둔 하위 프로세스가 이웃 하위 프로세스의 `/proc/<pid>/fd/0`·`1` 을
  열지 못한다. 이웃은 같은 `spawn` 으로 띄우되 가두지 않아 dumpable 이다. 가두기 전의 새 하위
  프로세스와 같은 처지다. 입출력이 소켓이라 열리지 않아야 한다.
- `group_killed`: 멈춰 둔 하위 프로세스를 그룹째 죽인 뒤 그 그룹에 남은 프로세스가 없다.

같은 경우의 pytest 는 Linux 에서만 돈다. 개발 호스트(macOS)에는 `/proc` 와 `prctl` 이 없어 여기서
거짓이 나오는 것이 맞다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from daegam.documents.sandbox import (
    group_alive,
    kill_group,
    run_worker,
    send_request,
    set_non_dumpable,
    spawn,
)
from daegam.documents.worker import HELD

# 멈춰 둔 하위 프로세스가 자리를 잡기를 기다리는 상한(초)
HOLD_WAIT = 10.0
# 서버의 pid. 파드에서는 서버가 컨테이너의 첫 프로세스다
SERVER_PID = 1
# 가두기 전의 하위 프로세스를 흉내 내는 이웃. 가두지 않고 표준 입력을 기다린다
SIBLING_ARGV = (sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()")
# 이웃의 입출력을 열어 본 결과 가운데 막힌 것. 소켓이면 no_device, dumpable 이 아니면 denied 다
STDIO_CLOSED = frozenset({"no_device", "denied"})


async def _group_killed() -> bool:
    worker = await spawn()
    try:
        async with asyncio.timeout(HOLD_WAIT):
            await send_request(worker, {"task": "hold"}, b"")
            held = await worker.reader.read(1) == HELD
    finally:
        await kill_group(worker)
    return held and not group_alive(worker.pid)


async def _probe(server_pid: int) -> dict[str, Any]:
    """가둔 하위 프로세스의 사실. 이웃을 하나 띄워 둔 채 묻고 끝나면 이웃을 거둔다."""
    sibling = await spawn(SIBLING_ARGV)
    try:
        request = {"task": "probe", "server_pid": server_pid, "sibling_pid": sibling.pid}
        return await run_worker(request)
    finally:
        await kill_group(sibling)


async def check(server_pid: int = SERVER_PID) -> dict[str, bool]:
    probe = await _probe(server_pid)
    sibling = set(probe.get("sibling_stdio", {}).values())
    return {
        "fork_blocked": probe.get("fork_blocked") is True,
        "dumpable_zero": probe.get("dumpable") == 0,
        "server_environ_unreadable": set(probe.get("environ", {}).values()) == {"denied"},
        "sibling_stdio_unopenable": bool(sibling) and sibling <= STDIO_CLOSED,
        "group_killed": await _group_killed(),
    }


def main(argv: list[str]) -> int:
    set_non_dumpable()  # 서버가 기동할 때와 같은 자리에서 시작한다
    report = asyncio.run(check(int(argv[0]) if argv else SERVER_PID))
    print(json.dumps(report))
    return 0 if all(report.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
