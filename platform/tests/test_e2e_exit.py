"""e2e 게이트가 끝나는 길(scripts/lib/e2e_exit.sh, P64)이 종료 코드 계약을 지키는지 본다.

신호로 멈춘 게이트가 0 으로 끝나면 make 와 릴리스 단계가 통과로 읽는다. 그래서 작은 스크립트에
그 도우미를 읽혀 macOS 의 /bin/bash(3.2)로 돌리고 정상 종료·실패·set -e 로 멈춤·신호·정리 중
실패에서 종료 코드와 정리 횟수를 본다. 프로세스 그룹에 간 신호(Ctrl-C, timeout, make 가 넘긴
TERM)는 전경 자식을 먼저 죽여 trap 이 정리 중에 돌 수 있으므로 그 길을 여러 번 되풀이해 본다.
게이트마다 trap 을 다시 걸거나 정리 함수에서 exit 하면 이 길을 비켜 가므로 그것도 글자로 막는다."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.consts import REPO

LIB = REPO / "scripts" / "lib"
GATES = sorted((REPO / "scripts").glob("e2e_*.sh"))
# 게이트가 도는 셸. #!/usr/bin/env bash 가 macOS 에서 먼저 찾는 것이 이것이다
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"
# bash 4 이상은 set -u 의 정의되지 않은 변수에 0 이 아니라 1 을 준다. 그 1 은 끝까지 온 코드처럼
# 보여 줄을 찍지 않는다. macOS 의 /bin/bash 는 3.2, 러너의 bash 는 5 다
BASH_MAJOR = int(
    subprocess.run([BASH, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True).stdout
)
# 신호를 받을 때까지 도는 몸통. sleep 이 짧아야 bash 가 trap 을 곧 돈다
LOOP = "while :; do sleep 0.1; done"
# 끝까지 돌면 DONE 을 찍는 느린 정리. 그룹 신호가 정리를 멈추지 않았는지 이 줄로 본다
SLOW_CLEANUP = "echo SLOW; sleep 0.5; echo DONE"
# 그룹 신호의 순서는 매번 달라 한 번으로는 경주를 잡지 못한다. 되풀이는 함께 띄운다
REPEATS = 8
# READY 뒤 전경 자식(sleep)이 뜰 때까지 기다리는 시간.
# 실제 게이트처럼 자식이 도는 중에 신호를 보낸다
CHILD_UP = 0.3


def _gate(tmp_path: Path, body: str, cleanup: str = ":") -> Path:
    """도우미를 읽고 정리 함수를 등록한 뒤 body 를 도는 스크립트. 정리 함수는 돌 때마다 한 줄을
    찍는다."""
    script = tmp_path / "gate.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'. "{LIB}/e2e_exit.sh"\n'
        f'. "{LIB}/e2e_report.sh"\n'
        'cleanup() { echo "CLEANUP rc=$1"; ' + cleanup + "; }\n"
        "on_exit cleanup\n"
        "echo READY\n" + body + "\n"
    )
    return script


def _run(script: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=30, env=env)


def _wait_for(proc: subprocess.Popen[str], line: str) -> str:
    """stdout 에 line 이 나올 때까지 읽고 읽은 것을 돌려준다. 그 전에 끝나면(EOF) 실패다."""
    assert proc.stdout is not None
    seen = ""
    while True:
        got = proc.stdout.readline()
        assert got, f"{line!r} 이 나오기 전에 끝났다: {seen!r}"
        seen += got
        if got.strip() == line:
            return seen


@pytest.fixture
def spawn() -> Iterator[object]:
    """새 세션(프로세스 그룹)으로 띄운다. 테스트가 어떻게 끝나든 그룹째 죽인다."""
    procs: list[subprocess.Popen[str]] = []

    def start(*cmd: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            list(cmd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=10)


def _group_signal_each(
    spawn, cmd: tuple[str, ...], sig: signal.Signals
) -> list[tuple[int, str, str]]:
    """cmd 를 REPEATS 개 함께 띄우고, 저마다 전경 자식이 도는 중에 그 그룹에 신호를 보낸다. 끝
    코드와 출력을 돌려준다. 함께 띄우므로 자식이 뜨기를 기다리는 시간과 느린 정리가 한 번만 든다."""
    procs = [spawn(*cmd) for _ in range(REPEATS)]
    for proc in procs:
        _wait_for(proc, "READY")
    time.sleep(CHILD_UP)
    for proc in procs:
        os.killpg(proc.pid, sig)
    ended = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        ended.append((proc.returncode, out, err))
    return ended


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("finish", 0),
        ("pass 1 ok; record_fail 2 boom; finish", 1),
        ("skip 1 later; finish", 2),
        ('die 3 "no answer"', 1),
        ("exit 1", 1),
        ('gate_incomplete "no kind"', 2),
    ],
)
def test_contract_codes_pass_through(tmp_path: Path, body: str, code: int) -> None:
    done = _run(_gate(tmp_path, body))
    assert done.returncode == code
    assert done.stdout.count("CLEANUP") == 1
    assert f"CLEANUP rc={code}" in done.stdout


@pytest.mark.parametrize("body", ["(exit 28)", "exit 2", 'echo "$NOT_SET_ANYWHERE"\nfinish'])
def test_codes_without_the_end_become_one(tmp_path: Path, body: str) -> None:
    # set -e 가 멈춘 명령의 코드(curl 의 28, 없는 파일을 읽은 grep 의 2)와 bash 3.2 가 set -eu 의
    # 정의되지 않은 변수에 주는 0 은 끝까지 오지 않은 것이라 1 이고 원래 코드를 찍는다
    done = _run(_gate(tmp_path, body))
    assert done.returncode == 1
    assert "CLEANUP rc=1" in done.stdout
    if "NOT_SET_ANYWHERE" not in body or BASH_MAJOR < 4:
        assert "E2E FAIL (exit " in done.stdout


def test_cleanup_cannot_change_the_code(tmp_path: Path) -> None:
    # 정리 중의 die·finish 는 줄만 찍고 돌아오고, 실패한 명령은 경고일 뿐이다
    cleanup = 'die 9 "restore failed"; false; finish'
    done = _run(_gate(tmp_path, "skip 1 later; finish", cleanup=cleanup))
    assert done.returncode == 2
    assert "!! cleanup: STEP 9: restore failed" in done.stderr


@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)]
)
def test_a_signal_ends_the_gate_as_interrupted(
    tmp_path: Path, spawn, sig: signal.Signals, code: int
) -> None:
    proc = spawn(BASH, str(_gate(tmp_path, LOOP)))
    _wait_for(proc, "READY")
    proc.send_signal(sig)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == code
    assert f"E2E INTERRUPTED (SIG{sig.name.removeprefix('SIG')})" in out
    assert out.count("CLEANUP") == 1
    assert f"CLEANUP rc={code}" in out


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
@pytest.mark.parametrize(
    "body",
    # 둘째는 출력을 버리는 명령이 도는 중이다(wait_healthy 의 curl 처럼).
    # trap 은 그 리디렉션 안에서 돈다
    ["sleep 30", "sleep 30 >/dev/null 2>&1\nfinish"],
    ids=["set-e", "redirected"],
)
def test_a_group_signal_lets_the_cleanup_finish(
    tmp_path: Path, spawn, sig: signal.Signals, code: int, body: str
) -> None:
    # 그룹 신호는 전경 sleep 을 먼저 죽여 set -e 가 EXIT 로 들어간 뒤 trap 이 돌기도 한다.
    # 어느 순서든 첫 신호라 정리는 끝까지 돌고 그 신호의 코드로 나간다
    script = _gate(tmp_path, body, cleanup=SLOW_CLEANUP)
    for returncode, out, err in _group_signal_each(spawn, (BASH, str(script)), sig):
        assert returncode == code, out + err
        assert "E2E INTERRUPTED" in out
        assert "DONE" in out
        assert "cleanup stopped" not in err


def test_make_forwarding_term_lets_the_cleanup_finish(tmp_path: Path, spawn) -> None:
    # make 는 그룹 TERM 을 받고 자식에게 TERM 을 한 번 더 넘긴다.
    # E2E_SIGNAL_REPEAT_SECONDS 안의 같은 신호는 하나다
    script = _gate(tmp_path, "sleep 30", cleanup=SLOW_CLEANUP)
    makefile = tmp_path / "Makefile"
    makefile.write_text(f"all:\n\t@{BASH} {script}\n")
    for returncode, out, err in _group_signal_each(
        spawn, ("make", "-s", "-f", str(makefile)), signal.SIGTERM
    ):
        assert returncode != 0
        assert "DONE" in out, out + err
        assert "cleanup stopped" not in err


def test_a_signal_during_cleanup_after_a_pass_is_still_an_interrupt(tmp_path: Path, spawn) -> None:
    proc = spawn(BASH, str(_gate(tmp_path, "finish", cleanup=SLOW_CLEANUP)))
    seen = _wait_for(proc, "SLOW")
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert "E2E PASS" in seen
    assert "DONE" in out
    assert proc.returncode == 143


def test_a_repeat_of_the_same_signal_is_ignored(tmp_path: Path, spawn) -> None:
    proc = spawn(BASH, str(_gate(tmp_path, LOOP, cleanup=SLOW_CLEANUP)))
    _wait_for(proc, "READY")
    proc.send_signal(signal.SIGTERM)
    _wait_for(proc, "SLOW")
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert "DONE" in out
    assert "cleanup stopped" not in err


def test_the_same_signal_after_the_repeat_window_stops_the_cleanup(tmp_path: Path, spawn) -> None:
    # 정리는 첫 신호의 trap 밖(EXIT trap)에서 돈다. 그래야 bash 가 같은 신호의 trap 을 다시 돌린다.
    # 창을 1초로 줄여 시험을 짧게 둔다. SECONDS 가 정수라 창 + 1초를 넘겨 기다린다
    env = {**os.environ, "E2E_SIGNAL_REPEAT_SECONDS": "1"}
    proc = spawn(BASH, str(_gate(tmp_path, LOOP, cleanup=f"echo SLOW; {LOOP}")), env=env)
    _wait_for(proc, "READY")
    proc.send_signal(signal.SIGTERM)
    _wait_for(proc, "SLOW")
    time.sleep(2.5)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert "cleanup stopped" in err


def test_a_later_different_signal_stops_the_cleanup(tmp_path: Path, spawn) -> None:
    proc = spawn(BASH, str(_gate(tmp_path, LOOP, cleanup=f"echo SLOW; {LOOP}")))
    _wait_for(proc, "READY")
    proc.send_signal(signal.SIGTERM)
    _wait_for(proc, "SLOW")
    time.sleep(0.2)
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert "cleanup stopped" in err


def test_curl_gets_default_limits_that_a_call_can_override(tmp_path: Path) -> None:
    # 진짜 curl 대신 받은 인자를 찍는 curl 을 PATH 앞에 둔다
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "curl").write_text('#!/bin/sh\necho "ARGS $*"\n')
    (stub / "curl").chmod(0o755)
    script = _gate(tmp_path, "curl -s http://x/a\ncurl --max-time 300 http://x/b\nfinish")
    done = _run(script, env={"PATH": f"{stub}:/usr/bin:/bin"})
    assert done.returncode == 0
    assert "ARGS --connect-timeout 5 --max-time 120 -s http://x/a" in done.stdout
    # curl 은 같은 옵션의 마지막 값을 쓰므로 부른 쪽의 --max-time 이 이긴다
    assert "ARGS --connect-timeout 5 --max-time 120 --max-time 300 http://x/b" in done.stdout


def _cleanup_body(text: str) -> str:
    match = re.search(r"^cleanup\(\) \{\n(.*?)^\}", text, re.M | re.S)
    assert match, "cleanup() 가 없다"
    return match.group(1)


@pytest.mark.parametrize("gate", GATES, ids=lambda p: p.name)
def test_every_gate_leaves_through_the_helper(gate: Path) -> None:
    text = gate.read_text()
    assert re.search(r'^\. "\$(?:HERE/lib|ROOT/scripts/lib)/e2e_exit\.sh"$', text, re.M)
    assert re.search(r"^on_exit cleanup$", text, re.M)
    # 게이트는 trap 을 스스로 걸지 않는다 (임시 디렉터리를 지우는 trap 도 정리 함수가 한다)
    assert not re.search(r"^\s*trap\b", text, re.M)
    assert not re.search(r"\bexit\b", _cleanup_body(text))
