"""샌드박스 게이트와 그 까닭, 개발용 우회와 그 가드, 자체 점검 대상, 파드의 판단 (P40 5절·8절).

모두 DB 없이 돈다.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import yaml

from daegam.config import Settings, settings_from_env
from daegam.k8s.sandbox_pods import PodView, judge_open, judge_open_record, pod_address
from daegam.k8s.sandbox_targets import ALWAYS_KEPT, build_targets, platform_address
from daegam.k8s.templates.sandbox import TARGETS_MAX
from daegam.main import create_app
from daegam.services.sandbox_gate import gate_reason, override_active
from daegam.services.telemetry.normalize import PLATFORM_EVENTS
from daegam.store.base import utcnow
from daegam.store.clusters import ClusterScheduling, SandboxScheduling
from tests.helpers.consts import LOCAL_VALUES, REPO
from tests.helpers.repo import repo_files, repo_lines
from tests.helpers.sandboxes import cluster, node, service

# -- 게이트 ------------------------------------------------------------------------------------


def test_an_open_gate_needs_rights_a_verdict_acceptance_and_capacity():
    assert gate_reason(cluster(), capacity=1, override=False) is None
    assert gate_reason(cluster(rights=False), capacity=1, override=False) == "rights"
    assert gate_reason(cluster(verdict=False), capacity=1, override=False) == "unchecked"
    assert gate_reason(cluster(shared=True), capacity=1, override=False) == "unaccepted"
    assert gate_reason(cluster(shared=True, accepted=True), capacity=1, override=False) is None
    assert gate_reason(cluster(), capacity=0, override=False) == "capacity"
    # 권한이 먼저다. 권한이 없으면 점검도 돌지 못한다
    assert gate_reason(cluster(rights=False, verdict=False), capacity=0, override=False) == "rights"


def test_a_verdict_that_reads_as_void_closes_the_gate_as_unchecked():
    c = cluster()
    c.sandbox.scheduling = SandboxScheduling(node_selector={"pool": "sandbox"})
    assert gate_reason(c, capacity=1, override=False) == "unchecked"
    c = cluster()
    c.scheduling = ClusterScheduling(node_selector={"pool": "agents"})
    assert gate_reason(c, capacity=1, override=False) == "unchecked"
    c = cluster()
    c.sandbox.image = "registry.example/daegam-sandbox:9.9.9"
    assert gate_reason(c, capacity=1, override=False) == "unchecked"
    c = cluster()
    assert c.status.preflight is not None
    c.status.preflight.k8s_version = "v1.99.0"
    assert gate_reason(c, capacity=1, override=False) == "unchecked"
    c = cluster()
    c.sandbox.check.voided_at = utcnow()
    assert gate_reason(c, capacity=1, override=False) == "unchecked"


def test_shared_nodes_turning_true_without_acceptance_is_unaccepted_not_a_void():
    c = cluster()
    c.sandbox.check.shared_nodes = True
    assert gate_reason(c, capacity=1, override=False) == "unaccepted"
    assert c.sandbox.check.voided_at is None and c.sandbox.check.passed


def test_the_override_skips_the_verdict_on_home_only_and_keeps_rights_and_capacity():
    on = Settings(sandbox_allow_unchecked=True)
    assert override_active(on, cluster("home", verdict=False))
    assert not override_active(on, cluster("edge", verdict=False))
    assert not override_active(Settings(), cluster("home", verdict=False))
    home = cluster("home", verdict=False)
    assert gate_reason(home, capacity=1, override=True) is None
    assert gate_reason(cluster("home", rights=False), capacity=1, override=True) == "rights"
    assert gate_reason(home, capacity=0, override=True) == "capacity"


# -- 개발용 우회의 가드 (P40 8절) -------------------------------------------------------------

SWITCH = "DAEGAM_SANDBOX_ALLOW_UNCHECKED"
TRUE_VALUE = re.compile(rf"{SWITCH}\s*[:=]\s*[\"']?(true|1|yes|on)\b", re.IGNORECASE)


def test_the_override_is_on_only_in_the_local_deploy_values():
    """로컬 차트 값(deploy/dev/values-local.yaml)만 켠다. 차트와 릴리스 산출물(배포 값, 이미지,
    워크플로, 배포 스크립트)에는 없다 (P85 4절). ops/ 가 있는 저장소에서는 운영자의 설치도
    본다."""
    local = yaml.safe_load((REPO / LOCAL_VALUES).read_text())["platform"]
    # e2e 가 게이트 동안 고치는 ConfigMap 키다. env 로 주면 envFrom 을 이겨 그 고침이 듣지 않는다
    assert local["extraConfig"][SWITCH] == "true", "로컬 배포본이 우회를 ConfigMap 에 켠다"
    assert SWITCH not in {e["name"] for e in local.get("extraEnv", [])}
    shipped = [
        p
        for p in repo_files()
        if p.startswith(
            ("charts/", "deploy/", "ops/", ".github/", "scripts/", "sandbox/", "ui/docker/")
        )
        or p.endswith("Dockerfile")
    ]
    offenders = [
        p for p, _n, line in repo_lines(shipped) if p != LOCAL_VALUES and TRUE_VALUE.search(line)
    ]
    assert offenders == []


def test_the_override_shows_in_healthz_and_in_the_start_log(offline_settings, caplog):
    import dataclasses

    from tests.helpers.http import startup_warnings

    def healthz(settings: Settings) -> dict:
        app = create_app(settings)
        route = next(r for r in app.routes if getattr(r, "path", "") == "/healthz")
        return route.endpoint()  # type: ignore[attr-defined]

    on = dataclasses.replace(offline_settings, sandbox_allow_unchecked=True)
    assert healthz(on)["sandbox_unchecked"] is True
    assert "sandbox_unchecked" not in healthz(offline_settings)
    warnings = startup_warnings(offline_settings, caplog, sandbox_allow_unchecked=True)
    assert any(SWITCH in w for w in warnings)


# -- 설정 --------------------------------------------------------------------------------------


def test_settings_read_caps_and_memory_quantities(bare_env):
    bare_env.setenv("DAEGAM_SANDBOX_PER_PERSON", "3")
    bare_env.setenv("DAEGAM_SANDBOX_MAX", "4")
    bare_env.setenv("DAEGAM_SANDBOX_NODE_RESERVE", "1536Mi")
    bare_env.setenv("DAEGAM_SANDBOX_WORKSPACE", "512Mi")
    bare_env.setenv("DAEGAM_SANDBOX_ALLOW_UNCHECKED", "true")
    s = settings_from_env()
    assert (s.sandbox_per_person, s.sandbox_max) == (3, 4)
    assert s.sandbox_node_reserve == 1536 * 1024**2 and s.sandbox_workspace == 512 * 1024**2
    assert s.sandbox_allow_unchecked
    bare_env.setenv("DAEGAM_SANDBOX_WORKSPACE", "not-a-size")
    assert settings_from_env().sandbox_workspace == Settings().sandbox_workspace


def test_settings_defaults_follow_the_sandbox_limits():
    s = Settings()
    assert (s.sandbox_per_person, s.sandbox_per_agent, s.sandbox_runs_per_agent) == (2, 5, 2)
    assert (s.sandbox_per_tenant, s.sandbox_max) == (10, 20)
    assert s.sandbox_node_reserve == 2 * 1024**3 and s.sandbox_workspace == 1024**3
    assert not s.sandbox_allow_unchecked


# -- 대상 --------------------------------------------------------------------------------------


def _keys(targets) -> set[tuple[str, str, int | None]]:
    return {t.key for t in targets}


def test_targets_come_from_the_lists_the_platform_reads():
    nodes = [node("n1", ip="10.0.0.1"), node("n2", ip="10.0.0.2")]
    services = [
        service("kube-system", "kube-dns", "10.43.0.10", 53),
        service("default", "kubernetes", "10.43.0.1", 443),
        {
            "metadata": {"name": "np", "namespace": "x"},
            "spec": {
                "type": "NodePort",
                "clusterIP": "10.43.0.99",
                "ports": [{"port": 80, "nodePort": 30080, "protocol": "TCP"}],
            },
        },
    ]
    pods = [
        {
            "metadata": {"name": "hp", "namespace": "x"},
            "spec": {
                "nodeName": "n1",
                "containers": [{"ports": [{"containerPort": 80, "hostPort": 8443}]}],
            },
        },
        {
            "metadata": {"name": "hn", "namespace": "x"},
            "spec": {
                "nodeName": "n2",
                "hostNetwork": True,
                "containers": [{"ports": [{"containerPort": 9100}]}],
            },
        },
        # 들어갈 노드가 아닌 곳의 hostPort 는 싣지 않는다
        {
            "metadata": {"name": "far", "namespace": "x"},
            "spec": {"nodeName": "n9", "containers": [{"ports": [{"hostPort": 7777}]}]},
        },
    ]
    targets = build_targets(
        eligible=nodes,
        pods=pods,
        services=services,
        platform=("10.43.0.50", 9000),
        own_node="n2",
    )
    keys = _keys(targets)
    for ip in ("10.0.0.1", "10.0.0.2"):
        for port in (22, 2379, 2380, 6443, 10250, 10255, 10256, 10010, 30080):
            assert ("tcp", ip, port) in keys
    assert ("tcp", "10.0.0.1", 8443) in keys and ("tcp", "10.0.0.2", 9100) in keys
    assert not any(port == 7777 for _, _, port in keys)
    assert {("dns", "10.43.0.10", 53), ("tcp", "10.43.0.10", 53), ("tcp", "10.43.0.1", 443)} <= keys
    assert {("tcp", "169.254.169.254", 80), ("tcp", "10.43.0.50", 9000)} <= keys
    assert {("tcp", "1.1.1.1", 443), ("dns", "8.8.8.8", 53), ("icmp", "1.1.1.1", None)} <= keys
    assert len(keys) == len(targets)


def test_targets_over_the_cap_keep_dns_api_and_the_own_node_first():
    nodes = [node(f"n{i}", ip=f"10.0.{i // 250}.{i % 250 + 1}") for i in range(80)]
    services = [
        service("kube-system", "kube-dns", "10.43.0.10", 53),
        service("default", "kubernetes", "10.43.0.1", 443),
    ]
    targets = build_targets(eligible=nodes, pods=[], services=services, own_node="n79")
    assert len(targets) == TARGETS_MAX
    roles = {t.role for t in targets}
    assert ALWAYS_KEPT <= roles
    assert sum(t.own_node for t in targets) == 8


def test_platform_address_resolves_a_service_name():
    services = [service("daegam-system", "daegam-platform", "10.43.0.50", 9000)]
    assert platform_address("http://daegam-platform.daegam-system.svc:9000", services) == (
        "10.43.0.50",
        9000,
    )
    assert platform_address("http://10.1.2.3:8080", []) == ("10.1.2.3", 8080)
    assert platform_address("https://platform.example.com", services) is None


# -- 파드의 판단 -------------------------------------------------------------------------------

OPENED = utcnow()


@pytest.mark.parametrize(
    ("view", "fail", "void"),
    [
        (PodView(exists=True, exit_code=87), "self_check", True),
        (PodView(exists=True, exit_code=86), "check_could_not_run", False),
        (
            PodView(exists=True, exit_code=137, terminated_reason="OOMKilled", restarts=1),
            "oom",
            False,
        ),
        (PodView(exists=True, host_users_kept=False), "host_users", False),
        (PodView(exists=True, waiting_reason="CreateContainerError"), "container_error", False),
    ],
)
def test_only_exit_87_voids_at_open(view, fail, void):
    judgement = judge_open(view, opened_at=OPENED, now=OPENED)
    assert (judgement.fail, judgement.void) == (fail, void)


def test_a_missing_pod_waits_for_its_creator_then_fails():
    """기록을 넣은 레플리카가 아직 파드를 만들지 않았을 수 있다."""
    gone = PodView(exists=False)
    assert judge_open(gone, opened_at=OPENED, now=OPENED + timedelta(seconds=10)).fail is None
    assert judge_open(gone, opened_at=OPENED, now=OPENED + timedelta(seconds=31)).fail == "pod_gone"


def test_open_waits_120s_while_unscheduled_then_30s_once_running():
    pending = PodView(exists=True, phase="Pending")
    assert judge_open(pending, opened_at=OPENED, now=OPENED + timedelta(seconds=119)).fail is None
    late = judge_open(pending, opened_at=OPENED, now=OPENED + timedelta(seconds=121))
    assert late.fail == "unscheduled" and not late.void
    pulling = PodView(exists=True, scheduled=True, waiting_reason="ImagePullBackOff")
    assert judge_open(pulling, opened_at=OPENED, now=OPENED + timedelta(seconds=121)).fail == "pull"
    started = OPENED + timedelta(seconds=60)
    running = PodView(exists=True, scheduled=True, running_since=started)
    assert judge_open(running, opened_at=OPENED, now=started + timedelta(seconds=29)).fail is None
    assert judge_open(running, opened_at=OPENED, now=started + timedelta(seconds=31)).fail == (
        "not_ready"
    )
    ready = PodView(exists=True, ready=True, running_since=started)
    assert judge_open(ready, opened_at=OPENED, now=started).ready


def test_the_sweep_keeps_an_oom_restart_and_closes_after_three():
    assert judge_open_record(PodView(exists=True, ready=True)) == "keep"
    oom = PodView(exists=True, exit_code=137, terminated_reason="OOMKilled", restarts=1)
    assert judge_open_record(oom) == "keep"
    assert judge_open_record(PodView(exists=True, exit_code=137, restarts=3)) == "restarts"
    assert judge_open_record(PodView(exists=True, exit_code=87, restarts=1)) == "void"
    assert judge_open_record(PodView(exists=True, exit_code=88, restarts=1)) == "later_failed"
    assert judge_open_record(PodView(exists=False)) == "lost"
    assert judge_open_record(PodView(exists=True, phase="Failed")) == "lost"


def test_pod_address_brackets_ipv6():
    assert pod_address("10.0.0.5") == "http://10.0.0.5:8080"
    assert pod_address("fd00::5") == "http://[fd00::5]:8080"


def test_the_sandbox_events_are_platform_only():
    assert {"sandbox_opened", "sandbox_closed"} <= PLATFORM_EVENTS
