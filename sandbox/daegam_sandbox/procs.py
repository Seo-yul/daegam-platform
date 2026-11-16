"""명령 실행, 거둠과 쓸기 (P40 3절).

- 명령은 `/workspace` 에서 `/bin/sh -c` 로 자기 프로세스 무리를 이끌고 돈다. `RLIMIT_NPROC` 256 과
  `RLIMIT_FSIZE` 512 MiB 를 걸고, 출력은 표준 출력과 표준 오류를 한 줄기로 1 MiB 까지 싣는다. 시간이
  다 되거나, 부른 쪽이 연결을 끊거나, `/cancel` 이 오면 무리를 통째로 죽인다. 명령이 끝나도 무리를
  죽인다.
- 거둠은 하나다. SIGCHLD 마다 끝난 자식을 모두 거두고, 명령의 우두머리면 그 명령에 자기 끝 상태를
  준다. 떼어 낸 프로세스(고아)는 PID 1 인 서버로 돌아오므로 거두고 버린다. 리눅스에서는 우두머리를
  거두기 전에(waitid WNOWAIT) 무리를 죽인다. 우두머리가 좀비인 동안은 그 무리 번호가 다른
  프로세스에게 가지 않는다.
- 쓸기: 도는 명령이 0 이 될 때와 `/cancel` 에서 서버 밖의 모든 프로세스를 죽인다. 그래야 떼어 낸
  프로세스가 명령보다 오래 살지 않는다. 쓸기와 명령 시작은 자물쇠 하나를 함께 쓰므로 쓸기 중에
  들어온 명령은 쓸기가 끝난 뒤에 시작한다.

스레드 없음: 서버는 명령마다 fork 한다(Popen 의 preexec_fn). 스레드가 있는 프로세스의 fork 는
자식에서 잠금을 쥔 채 멈출 수 있으므로 서버는 스레드를 만들지 않는다. 스레드 풀(run_in_executor,
이름 풀이, to_thread)도 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from daegam_sandbox.config import Layout, command_env

DEFAULT_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT = 1 << 20
NPROC_LIMIT = 256
# 파일 하나의 크기. 작업 공간과 /tmp 는 메모리 emptyDir 이라 그 크기가 이미 단단한
# 한도다. 이 값은 그 밖의 쓸 수 있는 파일, 곧 노드 디스크의 /dev/termination-log 를 막는다.
# 컨테이너가 다시 뜰 때마다 새 파일이라 다시 뜬 수만큼 곱해진다(F2b 의 스윕이 여러 번 다시
# 뜬 샌드박스를 닫는다). 데이터 작업의 파일과 내려받기 한도(10 MiB)에 넉넉한 128 MiB 다
FSIZE_LIMIT = 128 << 20
# 무리를 죽인 뒤에도 떼어 낸 프로세스가 출력 파이프를 쥐고 있으면 이만큼만 더 읽고 닫는다
DRAIN_GRACE = 1.0
# 우두머리를 거두기 전에 무리를 죽일 수 있는가(리눅스). 없으면 거둔 뒤에 죽인다
HAS_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")

log = logging.getLogger("daegam_sandbox")


def _copy_new(src: Path, dst: Path) -> None:
    """src 를 새 파일 dst 로 복사한다. 있으면 두고 링크는 따르지 않는다. shutil 은 쓰지 않는다. 그
    모듈이 zlib·bz2·lzma 를 싣기 때문이다(서버가 싣는 모듈은 tests/test_imports.py 가 지킨다)."""
    data = src.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dst, flags, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class Refused(Exception):
    """뒤 점검이 실패해 명령을 받지 않는다."""


class SpawnFailed(Exception):
    """명령을 띄우지 못했다."""


def clamp_timeout(value: object) -> float:
    """명령의 timeout(초)을 1~300 으로 자른다. 없으면 60 이다. 숫자가 아니면 ValueError 다."""
    if value is None:
        return DEFAULT_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        raise ValueError("timeout 은 초다")
    return float(min(max(value, MIN_TIMEOUT), MAX_TIMEOUT))


@dataclass
class _Output:
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    truncated: bool = False

    def add(self, chunk: bytes) -> None:
        room = MAX_OUTPUT - self.size
        if room > 0:
            self.chunks.append(chunk[:room])
            self.size += min(len(chunk), room)
        if len(chunk) > room:
            self.truncated = True

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", "replace")


@dataclass
class _Command:
    proc: subprocess.Popen
    exited: asyncio.Future
    stop: asyncio.Event


def _descendants(me: int) -> list[int]:
    """리눅스의 /proc 에서 me 의 자손. 다른 OS 에서는 빈 목록이다(시험 전용 길)."""
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 1 and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(name))
    found, todo = [], [me]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


class Runner:
    def __init__(
        self,
        layout: Layout,
        *,
        nproc_limit: int = NPROC_LIMIT,
        fsize_limit: int = FSIZE_LIMIT,
        font_cache: Path | None = None,
        sweep_pause: float = 0.0,
    ) -> None:
        self.layout = layout
        self.env = command_env(layout)
        self.nproc_limit = nproc_limit
        self.fsize_limit = fsize_limit
        self.font_cache = font_cache
        # 시험 전용: 쓸기가 자물쇠를 쥔 채 이만큼 쉰다. 쓸기 중에 들어온 명령을 재려고 둔다
        self.sweep_pause = sweep_pause
        self.lock = asyncio.Lock()
        self.commands: dict[int, _Command] = {}
        self.refusing = False

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.add_signal_handler(signal.SIGCHLD, self.reap)

    # ---- 거둠 ----

    def reap(self) -> None:
        while True:
            try:
                if HAS_WAITID:
                    info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if info is None:
                        return
                    if info.si_pid in self.commands:
                        _kill_group(info.si_pid)
                    pid, status = os.waitpid(info.si_pid, 0)
                else:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        return
            except ChildProcessError:
                return
            cmd = self.commands.get(pid)
            if cmd is None:
                continue
            code = os.waitstatus_to_exitcode(status)
            # Popen 이 다음 Popen 에서 이 pid 를 다시 waitpid 하지 않게 끝났다고 적는다
            cmd.proc.returncode = code
            if not cmd.exited.done():
                cmd.exited.set_result(code)

    # ---- 쓸기 ----

    def kill_others(self) -> None:
        """서버 밖의 모든 프로세스를 죽인다. PID 1 이면 kill(-1) 이 컨테이너의 PID 이름공간 안에서
        자기를 뺀 모두에 닿는다. PID 1 이 아니면(시험) kill(-1) 은 이 사용자의 모든 프로세스에
        닿으므로 쓰지 않고 서버의 자손과 명령 무리만 죽인다."""
        if os.getpid() == 1:
            for _ in range(2):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(-1, signal.SIGKILL)
            return
        for _ in range(2):
            for pid in _descendants(os.getpid()):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        for pid in self.commands:
            _kill_group(pid)

    async def sweep(self, *, force: bool = False) -> None:
        async with self.lock:
            if self.commands and not force:
                return
            if self.sweep_pause:
                await asyncio.sleep(self.sweep_pause)
            self.kill_others()

    async def cancel(self) -> int:
        running = list(self.commands.values())
        for cmd in running:
            cmd.stop.set()
            _kill_group(cmd.proc.pid)
        await self.sweep(force=True)
        return len(running)

    # ---- 명령 ----

    def _limits(self) -> None:
        # fork 뒤 exec 전에 자식에서 돈다. 서버가 무시하는 SIGINT 는 명령에서 기본으로
        # 되돌린다. 무시는 exec 를 지나 물려지기 때문이다. 코어 파일은 쓰지 않는다
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        if self.nproc_limit:
            resource.setrlimit(resource.RLIMIT_NPROC, (self.nproc_limit, self.nproc_limit))
        resource.setrlimit(resource.RLIMIT_FSIZE, (self.fsize_limit, self.fsize_limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def _prepare_home(self) -> None:
        """명령의 HOME 과 MPLCONFIGDIR 을 둔다. 이미지에 미리 만든 matplotlib 글꼴 캐시를 복사해
        첫 import 가 캐시를 다시 만들지 않게 한다. 명령이 지웠으면 다음 명령 전에 다시 둔다."""
        try:
            self.layout.workspace.mkdir(parents=True, exist_ok=True)
            self.layout.mplconfig.mkdir(parents=True, exist_ok=True)
            if self.font_cache is not None and self.font_cache.is_dir():
                for src in self.font_cache.iterdir():
                    dst = self.layout.mplconfig / src.name
                    if src.is_file() and not dst.exists():
                        _copy_new(src, dst)
        except OSError as e:
            log.warning("command home not prepared: %s", e)

    async def run(
        self, command: str, timeout: float, disconnected: asyncio.Future | None = None
    ) -> dict:
        loop = asyncio.get_running_loop()
        async with self.lock:
            if self.refusing:
                raise Refused()
            self._prepare_home()
            try:
                proc = subprocess.Popen(
                    ["/bin/sh", "-c", command],
                    cwd=self.layout.workspace,
                    env=self.env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    process_group=0,
                    preexec_fn=self._limits,
                    close_fds=True,
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise SpawnFailed(str(e)) from e
            cmd = _Command(proc, loop.create_future(), asyncio.Event())
            self.commands[proc.pid] = cmd
        try:
            return await self._wait(cmd, timeout, disconnected)
        finally:
            # 기다리다 예외로 빠졌으면 명령이 아직 돌 수 있다. 무리를 죽이고, 우두머리는 거둠이
            # 버린다
            if not cmd.exited.done():
                _kill_group(proc.pid)
            self.commands.pop(proc.pid, None)
            if not self.commands:
                await self.sweep()

    async def _wait(
        self, cmd: _Command, timeout: float, disconnected: asyncio.Future | None
    ) -> dict:
        loop = asyncio.get_running_loop()
        out = _Output()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), cmd.proc.stdout
        )
        collector = asyncio.ensure_future(_collect(reader, out))
        stop_wait = asyncio.ensure_future(cmd.stop.wait())
        waits: set[asyncio.Future] = {cmd.exited, stop_wait}
        if disconnected is not None:
            waits.add(disconnected)
        timed_out = cancelled = gone = False
        deadline = loop.time() + timeout
        try:
            while not cmd.exited.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                done, _ = await asyncio.wait(
                    waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if cmd.exited in done:
                    break
                if stop_wait in done:
                    cancelled = True
                    break
                if disconnected is not None and disconnected in done:
                    waits.discard(disconnected)
                    if disconnected.exception() is not None or disconnected.result() == b"":
                        gone = True
                        break
                    # 응답 전에 다음 요청이 왔다. 끊긴 것은 아니다
            if not (timed_out or gone) and cmd.stop.is_set():
                # cancel 은 표시를 세운 뒤 무리를 죽인다. 바쁜 루프에서는 죽음이 표시보다
                # 먼저 보일 수 있으므로 표시가 섰으면 취소다
                cancelled = True
            if not cmd.exited.done():
                _kill_group(cmd.proc.pid)
            code = await cmd.exited
            if not HAS_WAITID:
                _kill_group(cmd.proc.pid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(collector), DRAIN_GRACE)
        finally:
            stop_wait.cancel()
            transport.close()
            collector.cancel()
        if gone:
            log.info("command stopped: the caller disconnected")
        return {
            "output": out.text(),
            "exit_code": 128 - code if code < 0 else code,
            "truncated": out.truncated,
            "timed_out": timed_out,
            "cancelled": cancelled,
        }


async def _collect(reader: asyncio.StreamReader, out: _Output) -> None:
    while chunk := await reader.read(1 << 16):
        out.add(chunk)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
