"""자기 점검과 끝 코드 86·87·88, 종료 메시지, 점검 생략, SIGTERM (P40 3절, 8절)."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import IDENTITY_MAP, LINUX, closed_port, start, targets_env

from daegam_sandbox.config import Target
from daegam_sandbox.probes import probe
from daegam_sandbox.selfcheck import uid_map_is_identity


def listening() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock


def tcp(port: int, timeout: float = 0.5) -> dict:
    return {"kind": "tcp", "host": "127.0.0.1", "port": port, "timeout": timeout}


def test_the_uid_map_rule() -> None:
    assert uid_map_is_identity("0 0 4294967295\n") is True
    assert uid_map_is_identity("0 395575296 65536\n") is False
    # uid 하나라도 그대로 옮기면 그 uid 는 호스트의 uid 다
    assert uid_map_is_identity("0 100000 1000\n1000 1000 1\n") is True
    for bad in ("", "0 0\n", "a b c\n"):
        with pytest.raises(ValueError):
            uid_map_is_identity(bad)


@pytest.mark.parametrize(
    ("targets", "token_hash", "uid_map"),
    [
        (None, None, None),
        ("[]", None, None),
        ('[{"kind": "tcp", "host": "example.com", "port": 1}]', None, None),
        ("ok", "not-a-hash", None),
        ("ok", None, ""),
    ],
    ids=["no-targets", "empty-targets", "named-host", "bad-token-hash", "empty-uid-map"],
)
def test_a_check_that_cannot_run_exits_86(
    tmp_path: Path, targets: str | None, token_hash: str | None, uid_map: str | None
) -> None:
    sock = closed_port()
    if targets == "ok":
        targets = targets_env(tcp(sock.getsockname()[1]))
    kwargs = {} if uid_map is None else {"uid_map": uid_map}
    box = start(tmp_path, targets=targets, token_hash=token_hash, **kwargs)
    try:
        assert box.wait_exit() == 86
        assert json.loads(box.termination.read_text())["result"] == "error"
    finally:
        sock.close()
        box.close()


def test_an_identity_uid_map_exits_87(tmp_path: Path, blocked_targets: str) -> None:
    box = start(tmp_path, targets=blocked_targets, uid_map=IDENTITY_MAP)
    assert box.wait_exit() == 87
    assert json.loads(box.termination.read_text())["result"] == "identity"


def test_a_reached_target_exits_87_before_listening(tmp_path: Path) -> None:
    open_sock, shut = listening(), closed_port()
    targets = targets_env(tcp(shut.getsockname()[1]), tcp(open_sock.getsockname()[1]))
    box = start(tmp_path, targets=targets)
    try:
        assert box.wait_exit() == 87
        assert box.port == 0, "점검이 실패한 서버가 듣기 시작했다"
        message = json.loads(box.termination.read_text())
        assert message["result"] == "reached"
        assert message["reached"] == [f"tcp 127.0.0.1:{open_sock.getsockname()[1]}"]
    finally:
        open_sock.close()
        shut.close()


def test_a_later_failure_refuses_commands_and_exits_88(tmp_path: Path) -> None:
    # 처음에는 거절되던 포트가 나중에 듣기 시작한다
    sock = closed_port()
    targets = targets_env(tcp(sock.getsockname()[1]))
    box = start(tmp_path, targets=targets, extra_args=("--check-interval", "0.5"))
    try:
        assert box.execute("echo before")["output"] == "before\n"
        sock.listen(8)
        assert box.wait_exit() == 88
        assert json.loads(box.termination.read_text())["check"] == "later"
    finally:
        sock.close()
        box.close()


def test_a_termination_message_alone_decides_nothing(tmp_path: Path) -> None:
    """명령이 종료 메시지 파일에 통과를 적어도 끝 코드가 결과를 싣는다."""
    sock = closed_port()
    targets = targets_env(tcp(sock.getsockname()[1]))
    box = start(tmp_path, targets=targets, extra_args=("--check-interval", "0.5"))
    try:
        fake = json.dumps({"check": "later", "result": "passed"})
        box.execute(f"printf '%s' '{fake}' > {box.termination}")
        sock.listen(8)
        assert box.wait_exit() == 88
        assert json.loads(box.termination.read_text())["result"] == "reached"
    finally:
        sock.close()
        box.close()


def test_unchecked_reports_instead_of_exiting(tmp_path: Path) -> None:
    open_sock = listening()
    box = start(tmp_path, targets=targets_env(tcp(open_sock.getsockname()[1])), unchecked=True)
    try:
        assert box.proc.poll() is None
        status, body = box.call("/healthz", method="GET", token=None)
        assert (status, body["unchecked"]) == (200, True)
        assert json.loads(box.termination.read_text())["result"] == "reached"
        assert box.execute("echo ran")["output"] == "ran\n"
    finally:
        open_sock.close()
        box.close()


def test_sigterm_exits_0_at_once_and_ends_the_commands(
    tmp_path: Path, blocked_targets: str
) -> None:
    box = start(tmp_path, targets=blocked_targets)
    worker = threading.Thread(target=lambda: _quiet(box.execute, "echo $$ > pid; exec sleep 600"))
    worker.start()
    pid_file = box.workspace / "pid"
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    started = time.monotonic()
    box.proc.send_signal(signal.SIGTERM)
    assert box.wait_exit(timeout=5) == 0
    assert time.monotonic() - started < 2
    from conftest import gone

    assert gone(int(pid_file.read_text()))
    worker.join(timeout=5)
    box.close()


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:  # 서버가 끝나며 연결이 끊긴다
        pass


def test_nothing_loads_from_the_writable_mounts(tmp_path: Path, blocked_targets: str) -> None:
    """`python -I` 라 작업 디렉터리, PYTHONPATH, 쓰는 마운트의 모듈을 싣지 않는다."""
    marker = tmp_path / "loaded"
    plant = f"open({str(marker)!r}, 'a').write(__name__)\n"
    workspace = tmp_path / "root" / "workspace"
    for where in (workspace, tmp_path / "root" / "tmp"):
        where.mkdir(parents=True, exist_ok=True)
        for name in ("json.py", "asyncio.py", "socket.py", "sitecustomize.py"):
            (where / name).write_text(plant)
        (where / "daegam_sandbox").mkdir(exist_ok=True)
        (where / "daegam_sandbox" / "__init__.py").write_text(plant)
    box = start(
        tmp_path,
        targets=blocked_targets,
        extra_env={"PYTHONPATH": f"{workspace}:{tmp_path / 'root' / 'tmp'}"},
    )
    try:
        assert box.proc.poll() is None, box.log.read_text()
        box.execute("true")
        assert not marker.exists()
    finally:
        box.close()


def test_dns_targets_count_an_answer_as_reached() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def answer() -> None:
        data, peer = server.recvfrom(512)
        server.sendto(data[:2] + b"\x81\x83" + data[4:], peer)

    thread = threading.Thread(target=answer)
    thread.start()
    answered = asyncio.run(probe(Target("dns", "127.0.0.1", port, 2.0)))
    thread.join(timeout=5)
    server.close()
    assert answered.reached is True
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    quiet = asyncio.run(probe(Target("dns", "127.0.0.1", silent.getsockname()[1], 0.3)))
    silent.close()
    assert (quiet.reached, quiet.note) == (False, "timeout")


def test_icmp_targets_are_reached_or_skipped() -> None:
    """리눅스에서는 루프백이 답해야 한다. sandbox.yml 이 net.ipv4.ping_group_range 를 열어 특권 없는
    ICMP 소켓으로 echo 를 실제로 보낸다. 맥은 그 소켓을 열 수 없을 때 건너뛰고 적는다."""
    outcome = asyncio.run(probe(Target("icmp", "127.0.0.1", None, 1.0)))
    assert not outcome.error
    if LINUX:
        assert outcome.reached, outcome
    else:
        assert outcome.reached or outcome.skipped
