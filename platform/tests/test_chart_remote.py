"""대감 차트의 target: remote (P85 8절).

플랫폼이 배포해 들어갈 원격 클러스터에 ServiceAccount daegam-deployer 와 그 토큰 Secret, 세
ClusterRole 과 바인딩, PriorityClass, daegam-probe 와 그 RoleBinding 만 렌더한다. 규칙 목록은
home 과 같은 하나다. preflight 의 REQUIRED_VERBS 와의 대조는 test_namespaces.py 와
test_service_route.py 가 두 target 으로 한다.
"""

from __future__ import annotations

import yaml

from tests.helpers.chart import (
    CHART,
    chart_copy,
    helm,
    live_cluster,
    named,
    render,
    render_error,
)
from tests.helpers.consts import PLATFORM_NS

REMOTE = {"target": "remote"}
ACCOUNT = {"kind": "ServiceAccount", "name": "daegam-deployer", "namespace": PLATFORM_NS}
# kubectl apply 로 만든 옛 원격 클러스터의 객체를 --take-ownership 으로 넘겨받으려면 이름과
# roleRef 가 같아야 한다. roleRef 는 바꾸지 못한다
REMOTE_OBJECTS = [
    ("ClusterRole", "daegam-deployer"),
    ("ClusterRole", "daegam-tenant-objects"),
    ("ClusterRole", "daegam-tls-reader"),
    ("ClusterRoleBinding", "daegam-deployer"),
    ("Namespace", "daegam-probe"),
    ("PriorityClass", "daegam-sandbox"),
    ("RoleBinding", "daegam-tenant-objects"),
    ("Secret", "daegam-deployer-token"),
    ("ServiceAccount", "daegam-deployer"),
]


def _remote(*args: str) -> list[dict]:
    """값 파일 없이 target: remote 만 준 렌더. host 도 images 도 없어도 된다."""
    out = helm("template", "daegam", str(CHART), "-n", PLATFORM_NS, "--set", "target=remote", *args)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_remote_renders_only_the_deployer_and_its_rights():
    docs = _remote()
    assert sorted((d["kind"], d["metadata"]["name"]) for d in docs) == REMOTE_OBJECTS
    # 플랫폼 한 벌의 값(offline-values 의 host, images, Secret)을 주어도 같은 것만 렌더한다
    assert sorted((d["kind"], d["metadata"]["name"]) for d in render(sets=REMOTE)) == REMOTE_OBJECTS


def test_the_token_secret_belongs_to_the_deployer():
    token = named(_remote(), "Secret", "daegam-deployer-token")
    assert token["type"] == "kubernetes.io/service-account-token"
    assert token["metadata"]["annotations"] == {
        "kubernetes.io/service-account.name": "daegam-deployer"
    }
    assert token["metadata"]["namespace"] == PLATFORM_NS and "data" not in token


def test_the_bindings_name_the_deployer():
    docs = _remote()
    binding = named(docs, "ClusterRoleBinding", "daegam-deployer")
    assert binding["subjects"] == [ACCOUNT]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "daegam-deployer",
    }
    probe = named(docs, "RoleBinding", "daegam-tenant-objects")
    assert probe["metadata"]["namespace"] == "daegam-probe" and probe["subjects"] == [ACCOUNT]


def test_both_targets_share_one_rule_list():
    home, remote = render(), _remote()

    def rules(docs: list[dict], role: str) -> list[dict]:
        return named(docs, "ClusterRole", role)["rules"]

    assert rules(remote, "daegam-deployer") == rules(home, "daegam-platform")
    for role in ("daegam-tls-reader", "daegam-tenant-objects"):
        assert rules(remote, role) == rules(home, role)
    assert named(remote, "PriorityClass", "daegam-sandbox") == named(
        home, "PriorityClass", "daegam-sandbox"
    )


def test_helm_lint_strict_passes_for_remote_without_a_values_file():
    out = helm("lint", "--strict", str(CHART), "-n", PLATFORM_NS, "--set", "target=remote")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[INFO] Fail:" not in out.stdout


def test_remote_still_installs_into_daegam_system():
    out = helm("template", "daegam", str(CHART), "-n", "elsewhere", "--set", "target=remote")
    assert out.returncode != 0 and "daegam-system" in out.stderr


def test_remote_is_refused_on_the_cluster_that_runs_the_platform(tmp_path):
    """플랫폼 ConfigMap 이 있는 클러스터에 remote 를 올리면 home 릴리스를 원격 계정만 남은 것으로
    갈아 끼운다. 산 객체를 읽는 규칙이라 시험만의 daegam.live 로 본다."""
    live = {
        ("ConfigMap", "daegam-platform-config"): {"metadata": {"name": "daegam-platform-config"}}
    }
    error = render_error(sets=REMOTE, chart=chart_copy(tmp_path / "home", live_cluster(live)))
    assert "this cluster runs the Daegam platform" in error and "target: remote" in error
    fresh = render(sets=REMOTE, chart=chart_copy(tmp_path / "fresh", live_cluster({})))
    assert sorted((d["kind"], d["metadata"]["name"]) for d in fresh) == REMOTE_OBJECTS
