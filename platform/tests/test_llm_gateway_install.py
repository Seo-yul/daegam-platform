"""`make llm-gateway-uninstall` 의 점검 (P38 3절).

scripts/llm-gateway-install.sh 를 가짜 kubectl 로 돌린다. 가짜는 받은 인자를 적고 환경 값으로
답한다. 네임스페이스 이름은 무엇을 지우기 전에 보고, 거두기는 플랫폼 ConfigMap 이 없으면 멈춘다.
네임스페이스와 Role 을 만드는 것은 대감 차트다(test_chart_gateway.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers.consts import REPO

SCRIPT = REPO / "scripts" / "llm-gateway-install.sh"
KUBECTL = """#!/usr/bin/env bash
echo "$*" >> "$KUBECTL_LOG"
case "$*" in
  *"get namespace"*component*) printf '%s' "${MARK:-}" ;;
  *"get configmap"*"-o name"*) printf '%s' "${CM:-}" ;;
  *"get configmap"*DAEGAM_LLM_GATEWAY*) printf '%s' "${MODE:-}" ;;
  *"get pods"*) printf '%s' "${PODS:-}" ;;
esac
"""


def _run(tmp_path: Path, command: str, **env: str) -> tuple[int, list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(KUBECTL)
    kubectl.chmod(0o755)
    log = tmp_path / "kubectl.log"
    log.write_text("")
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "KUBECTL_LOG": str(log),
        **env,
    }
    out = subprocess.run(
        ["bash", str(SCRIPT), command], env=full, capture_output=True, text=True, check=False
    )
    return out.returncode, [line for line in log.read_text().splitlines() if line], out.stderr


@pytest.mark.parametrize(
    "namespace",
    [
        "daegam-system",
        "default",
        "kube-llm",
        "daegam-probe",
        "toolhive-system",
        "Bad_NS",
        "ops-system",
    ],
)
def test_a_reserved_or_malformed_namespace_is_refused_before_any_call(tmp_path, namespace):
    """거두기가 플랫폼이나 쿠버네티스의 네임스페이스를 지우지 않게 한다."""
    code, calls, err = _run(
        tmp_path, "uninstall", LLM_GATEWAY_NS=namespace, PLATFORM_NS="ops-system"
    )
    assert code == 1 and calls == [], (calls, err)
    assert "LLM_GATEWAY_NS" in err


def test_uninstall_uses_the_install_context_when_given(tmp_path):
    code, calls, _ = _run(
        tmp_path,
        "uninstall",
        INSTALL_CONTEXT="install-x",
        KUBE_CONTEXT="orbstack",
        MARK="llm-gateway",
        CM="configmap/x",
        MODE="off",
    )
    assert code == 0 and calls and all(c.startswith("--context install-x ") for c in calls)


def test_uninstall_refuses_without_the_platform_configmap(tmp_path):
    """틀린 PLATFORM_NS 나 컨텍스트는 꺼진 것과 다르다."""
    code, calls, err = _run(tmp_path, "uninstall", MARK="llm-gateway", CM="")
    assert code == 1 and not [c for c in calls if "delete" in c]
    assert "daegam-platform-config" in err


@pytest.mark.parametrize(("mode", "pods"), [("agentgateway", ""), ("off", "daegam-acme/agent-x ")])
def test_uninstall_refuses_while_the_gateway_is_on_or_called(tmp_path, mode, pods):
    code, calls, _ = _run(
        tmp_path, "uninstall", MARK="llm-gateway", CM="configmap/x", MODE=mode, PODS=pods
    )
    assert code == 1 and not [c for c in calls if "delete" in c]


def test_uninstall_deletes_the_marked_namespace_when_off(tmp_path):
    code, calls, err = _run(tmp_path, "uninstall", MARK="llm-gateway", CM="configmap/x", MODE="off")
    assert code == 0, err
    assert calls[-1].startswith("--context orbstack delete namespace daegam-llm")
