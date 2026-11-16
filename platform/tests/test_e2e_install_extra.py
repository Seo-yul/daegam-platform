"""`make e2e-install-extra` 의 몸통 scripts/run_e2e_install_extra.sh (P85 10절).

가짜 make 와 kubectl 로 돌린다. 가짜 make 는 받은 인자를 적고 FAIL 에 든 게이트면 실패하며,
BUMP 가 있으면 그 파일의 재시작 수를 올린다. 가짜 kubectl 은 그 파일의 값을 재시작 수로 답한다.
값은 설치 값 파일(INSTALL_ENV_FILE)에서 읽는다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.helpers.consts import REPO

SCRIPT = REPO / "scripts" / "run_e2e_install_extra.sh"
MAKE = """#!/usr/bin/env bash
echo "$*" >> "$MAKE_LOG"
[ -z "${EXTRA_LOG:-}" ] || echo "$E2E_EXTRA_RUN" >> "$EXTRA_LOG"
[ -z "${BUMP:-}" ] || echo 1 > "$BUMP"
for g in ${FAIL:-}; do case " $* " in *" $g "*) exit 1 ;; esac; done
"""
KUBECTL = """#!/usr/bin/env bash
echo "$*" >> "$KUBECTL_LOG"
cat "$RESTARTS"
"""


def _run(tmp_path: Path, install: str, **env: str) -> tuple[int, list[str], list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("make", MAKE), ("kubectl", KUBECTL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    install_file = tmp_path / "install.env"
    install_file.write_text(install)
    restarts = tmp_path / "restarts"
    restarts.write_text("0\n")
    make_log, kubectl_log = tmp_path / "make.log", tmp_path / "kubectl.log"
    make_log.write_text("")
    kubectl_log.write_text("")
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "INSTALL_ENV_FILE": str(install_file),
        "MAKE_LOG": str(make_log),
        "KUBECTL_LOG": str(kubectl_log),
        "RESTARTS": str(restarts),
        **env,
    }
    out = subprocess.run(
        ["bash", str(SCRIPT)], env=full, capture_output=True, text=True, check=False
    )
    calls = [line for line in make_log.read_text().splitlines() if line]
    kubectl = [line for line in kubectl_log.read_text().splitlines() if line]
    return out.returncode, calls, kubectl, out.stdout + out.stderr


INSTALL = "INSTALL_CONTEXT=ctx-a\nDAEGAM_HOST=install.test\nE2E_INSTALL_USER=e2e-admin\n"


def test_the_picked_gates_run_in_order_against_the_install(tmp_path):
    code, calls, kubectl, out = _run(
        tmp_path, INSTALL, E2E_INSTALL_EXTRA="e2e-oauth e2e-ui-serving e2e-web-tools"
    )
    assert code == 0, out
    assert [c.split()[3] for c in calls] == ["e2e-web-tools", "e2e-ui-serving", "e2e-oauth"]
    assert calls[0].endswith(
        "e2e-web-tools E2E_TARGET=install INSTALL_CONTEXT=ctx-a E2E_INSTALL_USER=e2e-admin"
    )
    # e2e-ui-serving 은 설치본의 Traefik 네임스페이스(비면 그 게이트의 기본)와 공개 호스트로 간다
    assert calls[1].endswith(
        "e2e-ui-serving KUBE_CONTEXT=ctx-a TRAEFIK_NS= E2E_UI_HOST=install.test"
    )
    # 지켜볼 파드가 없으면 kubectl 을 부르지 않는다
    assert kubectl == [] and "restarts" not in out


def test_a_line_without_install_gates_runs_nothing(tmp_path):
    code, calls, _, out = _run(tmp_path, INSTALL, E2E_INSTALL_EXTRA="e2e-instance e2e-agent")
    assert code == 0 and calls == [], out
    assert "no install gate is named" in out


def test_a_failed_gate_does_not_stop_the_rest(tmp_path):
    code, calls, _, out = _run(
        tmp_path, INSTALL, E2E_INSTALL_EXTRA="e2e-skills e2e-pat", FAIL="e2e-skills"
    )
    assert code == 1
    assert [c.split()[3] for c in calls] == ["e2e-skills", "e2e-pat"]
    assert "e2e-install-extra FAIL: e2e-skills" in out


def test_the_watched_pods_warn_when_they_restart(tmp_path):
    install = INSTALL + "E2E_WATCH_RESTARTS=mongodb-0\n"
    code, _, kubectl, out = _run(
        tmp_path, install, E2E_INSTALL_EXTRA="e2e-pat", BUMP=str(tmp_path / "restarts")
    )
    assert code == 0, out
    assert len(kubectl) == 2 and all(
        "get pod mongodb-0" in c and "--context ctx-a" in c for c in kubectl
    )
    assert "restarts: mongodb-0=0 before, mongodb-0=1 after" in out
    assert "a watched pod restarted during the run" in out


def test_each_gate_reads_the_picked_names_of_the_run(tmp_path):
    """게이트는 이번 실행의 다른 게이트가 다시 보는 단계를 건너뛴다(e2e_oauth.sh STEP 16). 실행이
    도는 게이트를 E2E_EXTRA_RUN 으로 넘긴다. 게이트를 따로 돌리면 이 값이 없다."""
    extra = tmp_path / "extra.log"
    code, calls, _, out = _run(
        tmp_path, INSTALL, E2E_INSTALL_EXTRA="e2e-oauth, e2e-instance e2e-pat", EXTRA_LOG=str(extra)
    )
    assert code == 0, out
    assert [c.split()[3] for c in calls] == ["e2e-pat", "e2e-oauth"]
    assert extra.read_text().splitlines() == ["e2e-pat e2e-oauth"] * 2


def test_a_candidate_is_required(tmp_path):
    code, calls, _, out = _run(tmp_path, INSTALL)
    assert code == 1 and calls == []
    assert "set E2E_TAG=<candidate tag>" in out


def test_the_e2e_user_is_required(tmp_path):
    code, calls, _, out = _run(tmp_path, "INSTALL_CONTEXT=ctx-a\n", E2E_INSTALL_EXTRA="e2e-pat")
    assert code == 1 and calls == []
    assert "E2E_INSTALL_USER" in out
