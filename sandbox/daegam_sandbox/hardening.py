"""서버 프로세스 굳히기 (P40 3절). 리눅스에서만 걸고, 시험이 도는 다른 OS 에서는 건너뛴다.

- `prctl(PR_SET_DUMPABLE, 0)`: 같은 uid 로 도는 명령이 서버의 `/proc/1/environ`·`mem`·`fd` 를 읽거나
  ptrace 로 붙지 못하게 한다. 서버 environ 에서 지운 토큰 해시도 처음 env 자리에는 남아 있기
  때문이다. 명령은 execve 할 때 dumpable 이 다시 켜지므로 명령끼리는 영향이 없다.
- `prctl(PR_SET_CHILD_SUBREAPER, 1)`: PID 1 이 아닐 때(시험)만 건다. 명령이 떼어 낸 프로세스가
  서버로 돌아와 거둠과 쓸기가 파드에서와 같게 돈다. PID 1 은 이미 고아를 받는다.
"""

from __future__ import annotations

import ctypes
import os
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3
PR_SET_CHILD_SUBREAPER = 36

LINUX = sys.platform.startswith("linux")


def _prctl(option: int, value: int = 0) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(option, value, 0, 0, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}) failed")
    return result


def harden() -> None:
    """서버를 굳힌다. 리눅스에서 dumpable 을 끄지 못하면 OSError 로 시작을 멈춘다."""
    if not LINUX:
        return
    _prctl(PR_SET_DUMPABLE, 0)
    if os.getpid() != 1:
        _prctl(PR_SET_CHILD_SUBREAPER, 1)


def dumpable() -> int | None:
    """지금 dumpable 값. 리눅스가 아니면 None 이다."""
    return _prctl(PR_GET_DUMPABLE) if LINUX else None
