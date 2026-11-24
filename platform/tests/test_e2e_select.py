"""릴리스 후보 태그의 `e2e:` 줄이 고르는 게이트.

고르기는 scripts/lib/e2e_select.py 에 있고 e2e-cluster.yml 의 plan 잡과
scripts/run_e2e_install_extra.sh 가 태그 메시지를 넘긴다. 여기서는 서명 블록이 붙은 태그 메시지
꼴을 손으로 만들어 넘긴다.
"""

from __future__ import annotations

import io

import e2e_select
import pytest
import yaml
from e2e_select import (
    AFTER_PICKED,
    BEFORE_PICKED,
    COVERED_BY,
    DEFAULT_INSTALL,
    INSTALL,
    KILL_GRACE,
    NOT_BLOCKING_YET,
    OPTIONAL,
    chosen,
    describe,
    install,
    main,
    matrix,
)

from tests.helpers.consts import REPO

WORKFLOW = REPO / ".github" / "workflows" / "e2e-cluster.yml"

SIGNED = (
    "1.1.0-rc6\n\n{line}\n-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n"
)


def _shards(picked: set[str]) -> dict[str, dict]:
    return {m["shard"]: m for m in matrix(picked)["include"]}


def test_no_line_runs_only_the_blocking_gates() -> None:
    picked = chosen("1.1.0-rc6\n")
    assert picked == set()
    shards = _shards(picked)
    assert set(shards) == {"core", "agent"}
    assert all(
        m["blocking_gates"] == m["soft_gates"] == "" and m["netpol_minutes"] == 0
        for m in shards.values()
    )
    assert install(picked) == []
    assert describe(picked).splitlines()[-1] == "install e2e-install-extra: -"


def test_all_picks_every_gate_and_every_picked_gate_blocks() -> None:
    picked = chosen(SIGNED.format(line="e2e: all"))
    shards = _shards(picked)
    assert set(shards) == {"core", "agent", "auth", "sandbox", "provisioning", "exchange"}
    assert shards["auth"]["mock_oidc"] and shards["agent"]["ui"]
    assert shards["core"]["netpol_minutes"] == 15
    assert shards["core"]["blocking_gates"] == "e2e-llm-gateway:28"
    assert shards["agent"]["blocking_gates"].split()[0] == "e2e-agent-redis:10"
    assert shards["agent"]["blocking_gates"].split()[-1] == "e2e-ui-full:20"
    assert shards["auth"]["blocking_gates"].split()[-1] == "e2e-oidc-platform:28"
    # 고른 게이트는 모두 k3d 에서 통과한 적이 있어 막는다
    soft = {e.split(":")[0] for m in shards.values() for e in m["soft_gates"].split()}
    assert soft == set(NOT_BLOCKING_YET) == set()
    assert shards["provisioning"]["blocking_gates"] == "e2e-provisioning:30 e2e-evaluation:8"
    assert shards["sandbox"]["blocking_gates"] == "e2e-sandbox:10"
    assert shards["exchange"]["blocking_gates"] == "e2e-exchange:26"
    assert install(picked) == list(INSTALL)


def test_a_gate_not_yet_passed_goes_to_the_soft_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # 새로 더한 게이트는 처음 통과할 때까지 NOT_BLOCKING_YET 에 있고 soft_gates 로 간다
    monkeypatch.setattr(e2e_select, "NOT_BLOCKING_YET", ("e2e-ui-full",))
    agent = _shards(chosen("e2e: e2e-ui-full e2e-knowledge"))["agent"]
    assert agent["blocking_gates"] == "e2e-knowledge:6"
    assert agent["soft_gates"] == "e2e-ui-full:20" and agent["ui"]


def test_names_pick_their_shards_and_install_gates_in_run_order() -> None:
    picked = chosen("1.1.0-rc6\n\ne2e: e2e-oauth, e2e-agent-local\ne2e: e2e-web-tools e2e-agent")
    shards = _shards(picked)
    # e2e-agent-local 은 mock-oidc 가 필요 없다
    assert (
        shards["auth"]["blocking_gates"] == "e2e-agent-local:24" and not shards["auth"]["mock_oidc"]
    )
    assert shards["agent"]["blocking_gates"] == shards["agent"]["soft_gates"] == ""
    assert not shards["agent"]["ui"]
    assert install(picked) == ["e2e-web-tools", "e2e-oauth"]
    assert describe(picked).splitlines()[-1] == "install e2e-install-extra: e2e-web-tools e2e-oauth"


def test_the_llm_gateway_gate_runs_in_core_and_blocks() -> None:
    core = _shards(chosen("e2e: e2e-llm-gateway"))["core"]
    assert core["blocking_gates"] == "e2e-llm-gateway:28"
    assert core["soft_gates"] == ""


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e"]["steps"]


def test_every_shard_bakes_and_loads_the_agent_image() -> None:
    """core 조각도 끝의 차트 기본 값 설치에서 e2e-install STEP 8 이 Agent 를 띄운다 (P85 12절)."""
    steps = {s.get("name"): s for s in _steps()}
    assert "image-agent" in steps["build images"]["run"].split("targets=(", 1)[1].split(")", 1)[0]
    assert (
        "daegam-agent-runtime:0.1.0"
        in steps["import images"]["run"].split("images=(", 1)[1].split(")", 1)[0]
    )


def test_the_default_install_runs_last_in_core_within_its_minutes() -> None:
    """차트의 기본 값 설치 단계는 core 조각에서만, 고른 게이트와 진단 뒤, e2e-netpol 이 k3d
    클러스터를 지우기 전에 돈다. 단계마다의 timeout-minutes 합이 시간 예산의 DEFAULT_INSTALL
    이다."""
    steps = _steps()
    names = [s.get("name", "") for s in steps]
    chart = [s for s in steps if s.get("name", "").startswith("chart default-values")]
    timed = [s for s in chart if "timeout-minutes" in s]
    assert sum(s["timeout-minutes"] for s in timed) == DEFAULT_INSTALL["core"]
    assert all("matrix.shard == 'core'" in s["if"] or "steps.chart_" in s["if"] for s in chart)
    first, last = names.index(chart[0]["name"]), names.index(chart[-1]["name"])
    assert names.index("picked gates") < names.index("diagnostics") < first
    assert last < names.index("make e2e-netpol")


def test_a_covered_gate_runs_only_without_the_gate_that_covers_it() -> None:
    # e2e-assistant-oidc 가 header 모드 e2e-assistant 의 STEP 0~8 을 모두 돈다. 함께 고르면
    # (all 도) 덮는 쪽만 돈다
    assert COVERED_BY == {"e2e-assistant": "e2e-assistant-oidc"}
    known = {g for gates in OPTIONAL.values() for g, _ in gates}
    assert set(COVERED_BY) | set(COVERED_BY.values()) <= known
    for line in ("e2e: all", "e2e: e2e-assistant e2e-assistant-oidc"):
        picked = chosen(line)
        assert "e2e-assistant" not in picked and "e2e-assistant-oidc" in picked
        agent = _shards(picked)["agent"]
        assert "e2e-assistant:" not in agent["blocking_gates"] + agent["soft_gates"]
        assert "e2e-assistant" not in describe(picked).split()
    alone = _shards(chosen("e2e: e2e-assistant"))
    assert alone["agent"]["blocking_gates"] == "e2e-assistant:6" and "auth" not in alone


def test_only_the_picked_gates_skip_the_steps_the_gates_that_always_run_check() -> None:
    # 변형 게이트(e2e-agent-oidc 등)는 E2E_SHARED_STEPS=skip 일 때만 e2e-agent·e2e-chat 이 보는
    # 단계를 건너뛴다. 늘 도는 게이트의 단계와 손으로 돌린 게이트는 그 값을 받지 않는다
    steps = {s.get("name"): s for s in _steps()}
    assert steps["picked gates"]["env"]["E2E_SHARED_STEPS"] == "skip"
    others = [s for name, s in steps.items() if name != "picked gates"]
    assert not any("E2E_SHARED_STEPS" in str(s) for s in others)
    job = yaml.safe_load(WORKFLOW.read_text())
    assert "E2E_SHARED_STEPS" not in job.get("env", {}) | job["jobs"]["e2e"].get("env", {})


def test_an_unknown_name_is_refused() -> None:
    with pytest.raises(ValueError, match="e2e-agnet"):
        chosen("e2e: e2e-agnet")


def test_shard_time_caps_fit_the_job() -> None:
    # 고른 게이트가 모두 상한과 KILL 유예를 다 써도 조각이 잡의 timeout-minutes 안에 끝난다.
    # 막는 조각이 잡 시간에 걸리면 고른 게이트 탓에 candidate 가 멈춘다
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e"]
    for shard, gates in OPTIONAL.items():
        worst = BEFORE_PICKED[shard] + sum(m + KILL_GRACE[shard] for _, m in gates) + AFTER_PICKED
        worst += DEFAULT_INSTALL.get(shard, 0)
        assert worst < job["timeout-minutes"], (shard, worst)


def test_the_workflow_reads_the_kill_grace_from_the_matrix() -> None:
    text = WORKFLOW.read_text()
    assert text.count('timeout -k "${GRACE}m"') == 2
    assert {m["kill_grace"] for m in matrix(chosen("e2e: all"))["include"]} == {2, 6}


def test_the_workflow_reads_every_matrix_key() -> None:
    # 고르기가 낸 값을 워크플로가 읽지 않으면 그 게이트나 준비가 소리 없이 빠진다
    text = WORKFLOW.read_text()
    for shard in matrix(chosen("e2e: all"))["include"]:
        for key in shard:
            assert f"matrix.{key}" in text, key


def test_main_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("e2e: e2e-nope"))
    assert main(["e2e_select.py", "install"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("e2e: e2e-pat"))
    assert main(["e2e_select.py", "install"]) == 0
    assert capsys.readouterr().out == "e2e-pat\n"


def test_sandbox_runs_on_its_own_k3d_shard_and_on_the_install() -> None:
    # e2e-sandbox 는 ConfigMap 과 노드 메모리를 혼자 쓰므로 제 조각에서 돈다.
    # 샌드박스 이미지는 그 조각만 굽는다
    shards = _shards(chosen("e2e: e2e-sandbox"))
    assert set(shards) == {"core", "agent", "sandbox"}
    sandbox = shards["sandbox"]
    assert sandbox["sandbox_image"] and not sandbox["mock_oidc"]
    assert sandbox["blocking_gates"] == "e2e-sandbox:10" and sandbox["kill_grace"] == 6
    assert not any(m["sandbox_image"] for name, m in shards.items() if name != "sandbox")
    assert install(chosen("e2e: e2e-sandbox e2e-runs")) == ["e2e-runs", "e2e-sandbox"]


def test_provisioning_runs_alone_on_its_own_k3d_shard() -> None:
    # e2e-provisioning 은 local 모드와 쉬는 창을 ConfigMap 에서 바꾸므로 제 조각에서 혼자 돈다(P44
    # 17절). 설치본 게이트가 아니고 mock-oidc·UI·샌드박스 이미지가 필요 없다
    shards = _shards(chosen("e2e: e2e-provisioning"))
    assert set(shards) == {"core", "agent", "provisioning"}
    alone = shards["provisioning"]
    assert alone["blocking_gates"] == "e2e-provisioning:30" and alone["soft_gates"] == ""
    assert not (alone["mock_oidc"] or alone["ui"] or alone["sandbox_image"])
    assert alone["kill_grace"] == 6
    assert install(chosen("e2e: e2e-provisioning")) == []
    assert [g for g, _ in OPTIONAL["provisioning"]] == ["e2e-provisioning", "e2e-evaluation"]


def test_evaluation_runs_in_the_provisioning_shard() -> None:
    # e2e-evaluation(P45)은 평가 미리 보기를 띄우므로 미리 보기를 띄우는 e2e-provisioning 과
    # 한 조각에서 돈다. 고르지 않은 e2e-provisioning 은 돌지 않는다(P45 19절). 설치본 게이트가
    # 아니다
    shards = _shards(chosen("e2e: e2e-evaluation"))
    assert set(shards) == {"core", "agent", "provisioning"}
    alone = shards["provisioning"]
    assert alone["blocking_gates"] == "e2e-evaluation:8" and alone["soft_gates"] == ""
    assert not (alone["mock_oidc"] or alone["ui"] or alone["sandbox_image"])
    assert install(chosen("e2e: e2e-evaluation")) == []
    both = _shards(chosen("e2e: e2e-provisioning e2e-evaluation"))["provisioning"]
    assert both["blocking_gates"] == "e2e-provisioning:30 e2e-evaluation:8"


def test_exchange_runs_alone_on_its_own_k3d_shard() -> None:
    # e2e-exchange 는 local 모드로 바꾸고 게시된 Gateway 를 여러 번 다시 배포하므로 제 조각에서
    # 혼자 돈다(P57 8절). 설치본 게이트가 아니고 mock-oidc·UI·샌드박스 이미지가 필요 없다
    shards = _shards(chosen("e2e: e2e-exchange"))
    assert set(shards) == {"core", "agent", "exchange"}
    alone = shards["exchange"]
    assert alone["blocking_gates"] == "e2e-exchange:26" and alone["soft_gates"] == ""
    assert not (alone["mock_oidc"] or alone["ui"] or alone["sandbox_image"])
    assert alone["kill_grace"] == 6
    assert install(chosen("e2e: e2e-exchange")) == []
    assert [g for g, _ in OPTIONAL["exchange"]] == ["e2e-exchange"]


def test_agent_calls_runs_in_the_agent_shard_and_on_the_install() -> None:
    # e2e-agent-calls(P46)는 header 모드 그대로 도는 k3d 의 agent 조각과 설치본 둘에서 돈다. 모드를
    # 바꾸지 않으므로 auth 조각이 뜨지 않고 mock-oidc·UI·샌드박스 이미지도 필요 없다. k3d 에서
    # 통과했으므로 막는다. 설치본에서는 e2e-runs 뒤, e2e-sandbox 앞이다
    picked = chosen("e2e: e2e-agent-calls")
    shards = _shards(picked)
    assert set(shards) == {"core", "agent"}
    agent = shards["agent"]
    assert agent["blocking_gates"] == "e2e-agent-calls:10" and agent["soft_gates"] == ""
    assert not (agent["mock_oidc"] or agent["ui"] or agent["sandbox_image"])
    assert "e2e-agent-calls" not in NOT_BLOCKING_YET
    assert install(picked) == ["e2e-agent-calls"]
    assert install(chosen("e2e: e2e-sandbox e2e-agent-calls e2e-runs")) == [
        "e2e-runs",
        "e2e-agent-calls",
        "e2e-sandbox",
    ]
    assert "k3d agent: e2e-chat e2e-agent e2e-agent-calls" in describe(picked)


def test_runs_is_an_install_gate_only() -> None:
    # e2e-runs 는 설치본에서만 돈다. 고르면 e2e-install-extra 가 맨 끝에 돌고
    # k3d 조각에는 들지 않는다
    picked = chosen("1.1.0-rc10\n\ne2e: e2e-runs e2e-pat")
    assert install(picked) == ["e2e-pat", "e2e-runs"]
    assert all(
        "e2e-runs" not in m["blocking_gates"] + m["soft_gates"] for m in _shards(picked).values()
    )
    assert "e2e-runs" in install(chosen("e2e: all"))
