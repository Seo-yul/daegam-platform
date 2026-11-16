"""설치본 e2e 의 설치 값 키와 노출한 Gateway 찾기 (P85 10절).

scripts/lib/hosts.sh 의 install_flag·install_count·install_env_defaults 와
scripts/lib/e2e_mcp_access.sh 의 install_gateway·skip_without_gateway 를 bash 로 부른다.
Gateway 찾기는 GET /gateways 를 가짜 api 로 바꿔 부르고, e2e_pat·e2e_oauth 는 그 결과로 단계를
조건부(cond_skip)로 건너뛸지 못 돈 것(skip)으로 건너뛸지 정한다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers.consts import REPO

LIB = REPO / "scripts" / "lib"


def _bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **env}
    return subprocess.run(
        ["bash", "-c", script], env=full, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("value", "out", "code"),
    [("", "false", 0), ("true", "true", 0), ("false", "false", 0), ("yes", "", 1), ("1", "", 1)],
)
def test_install_flag_takes_true_or_false_and_defaults_when_empty(value, out, code):
    r = _bash(f'. "{LIB / "hosts.sh"}"; install_flag E2E_POD_RESTART false', E2E_POD_RESTART=value)
    assert (r.returncode, r.stdout.strip()) == (code, out), r.stderr
    if code:
        assert "E2E_POD_RESTART" in r.stderr


@pytest.mark.parametrize(
    ("value", "code"),
    [("", 0), ("1", 0), ("99", 0), ("0", 1), ("100", 1), ("-1", 1), ("two", 1), ("01", 1)],
)
def test_install_count_takes_one_to_ninety_nine_or_nothing(value, code):
    r = _bash(
        f'. "{LIB / "hosts.sh"}"; install_count E2E_SANDBOX_CAPACITY', E2E_SANDBOX_CAPACITY=value
    )
    assert r.returncode == code, r.stderr
    assert r.stdout.strip() == ("" if code else value)
    if code:
        assert "E2E_SANDBOX_CAPACITY" in r.stderr


@pytest.mark.parametrize(
    ("preset", "want"), [("", "/k/install.yaml"), ("/k/mine.yaml", "/k/mine.yaml")]
)
def test_install_kubeconfig_becomes_kubeconfig_unless_one_is_set(tmp_path, preset, want):
    install = tmp_path / "install.env"
    install.write_text("INSTALL_CONTEXT=ctx\nINSTALL_KUBECONFIG=/k/install.yaml\n")
    env = {"INSTALL_ENV_FILE": str(install)}
    if preset:
        env["KUBECONFIG"] = preset
    r = _bash(f'. "{LIB / "hosts.sh"}"; install_env_defaults && echo "$KUBECONFIG"', **env)
    assert (r.returncode, r.stdout.strip()) == (0, want), r.stderr


def _extra(call: str, **env: str) -> subprocess.CompletedProcess[str]:
    return _bash(f'. "{LIB / "hosts.sh"}"; {call}', **env)


def test_install_extra_gates_lists_the_picked_install_gates_in_run_order():
    r = _extra("install_extra_gates", E2E_INSTALL_EXTRA="e2e-oauth e2e-agent e2e-pat")
    assert (r.returncode, r.stdout.strip()) == (0, "e2e-pat e2e-oauth"), r.stderr


@pytest.mark.parametrize(
    ("extra", "gate", "code"),
    [
        ("e2e-pat e2e-oauth", "e2e-pat", 0),
        ("e2e-oauth", "e2e-pat", 1),
        ("all", "e2e-runs", 0),
        ("e2e-agent e2e-runsx", "e2e-runs", 2),
        ("", "e2e-runs", 1),
    ],
)
def test_install_extra_runs_says_whether_the_candidate_runs_a_gate(extra, gate, code):
    """후보를 모르면(E2E_TAG 도 E2E_INSTALL_EXTRA 도 없다) 돌지 않는 것으로 본다. 모르는
    이름은 2 다."""
    r = _extra(f"install_extra_runs {gate}", E2E_INSTALL_EXTRA=extra)
    assert r.returncode == code, r.stderr
    if code == 2:
        assert "e2e-runsx" in r.stderr


def test_install_extra_gates_needs_an_annotated_tag():
    r = _extra("install_extra_gates")
    assert r.returncode == 2 and r.stdout == ""
    r = _extra("install_extra_runs e2e-runs", E2E_TAG="no-such-tag-e2e")
    assert r.returncode == 2
    assert "no-such-tag-e2e is not an annotated tag here" in r.stderr


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"EXPOSURE_DOMAIN": "ex.test"}, "mcp-gateway.ex.test"),
        ({"EXPOSURE_DOMAIN": "ex.test", "E2E_PAT_GW_HOST": "gw.ex.test"}, "gw.ex.test"),
        ({}, ""),
    ],
)
def test_the_exposed_gateway_host_defaults_to_the_exposure_domain(env, want):
    """e2e-pat 과 e2e-oauth 가 같은 규칙으로 host 를 정하므로 e2e-oauth 는 둘이 같은 Gateway
    인지 안다."""
    r = _bash(f'. "{LIB / "e2e_mcp_access.sh"}"; install_gateway_host E2E_PAT_GW_HOST', **env)
    assert (r.returncode, r.stdout.strip()) == (0, want), r.stderr


GATEWAY_SCRIPT = """
set -euo pipefail
. "$LIB/e2e_report.sh"
. "$LIB/e2e_json.sh"
. "$LIB/e2e_api.sh"
. "$LIB/e2e_mcp_access.sh"
api() { cp "$GATEWAYS" "$WORK/body"; echo 200; }
rc=0
install_gateway 3 E2E_PAT_GW_HOST "$GW_HOST" || rc=$?
echo "rc=$rc"
echo "id=$GW_ID pin=$GW_PIN"
[ "$rc" = 0 ] || skip_without_gateway "$rc" 3 4r
echo "skipped=$SKIPPED"
"""


def _find_gateway(tmp_path: Path, host: str, gateways: list[dict]) -> tuple[int, str]:
    body = tmp_path / "gateways.json"
    body.write_text(json.dumps(gateways))
    r = _bash(
        GATEWAY_SCRIPT,
        LIB=str(LIB),
        WORK=str(tmp_path),
        TENANT="default",
        GATEWAYS=str(body),
        GW_HOST=host,
    )
    assert r.returncode == 0, r.stderr
    return int(r.stdout.split("rc=")[1].split()[0]), r.stdout


def _gateway(url: str) -> dict:
    return {"id": "gw-1", "status": {"external_url": url}}


def test_no_named_host_skips_the_gateway_steps_conditionally(tmp_path):
    rc, out = _find_gateway(tmp_path, "", [_gateway("https://localhost/mcp")])
    assert rc == 1
    assert "STEP 3 SKIP (conditional): EXPOSURE_DOMAIN is empty and E2E_PAT_GW_HOST is unset" in out
    assert "STEP 4r SKIP (conditional)" in out and "skipped=0" in out


def test_a_named_host_without_a_gateway_leaves_the_run_incomplete(tmp_path):
    rc, out = _find_gateway(tmp_path, "localhost", [_gateway("https://other.example/mcp")])
    assert rc == 2
    assert "STEP 3 SKIP: no Gateway in default has external_url https://localhost/mcp" in out
    assert "set E2E_PAT_GW_HOST to the host of the install's exposed Gateway" in out
    # skip 는 세어서 게이트가 INCOMPLETE(2)로 끝난다
    assert "skipped=2" in out


def test_a_named_host_that_does_not_resolve_leaves_the_run_incomplete(tmp_path):
    rc, out = _find_gateway(tmp_path, "gw.invalid", [_gateway("https://gw.invalid/mcp")])
    assert rc == 2
    assert "STEP 3 SKIP: the Gateway's host gw.invalid does not resolve" in out
    assert "skipped=2" in out


def test_the_named_gateway_is_found_and_pinned(tmp_path):
    rc, out = _find_gateway(tmp_path, "localhost", [_gateway("https://localhost/mcp")])
    assert rc == 0
    assert "id=gw-1 pin=localhost:443:127.0.0.1" in out
    assert "SKIP" not in out
