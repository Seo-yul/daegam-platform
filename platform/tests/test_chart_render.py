"""대감 차트(charts/daegam)가 렌더하는 객체의 계약 (P85 3절~6절, 12절).

모두 `helm template` 과 ci/offline-values.yaml 로 클러스터 없이 렌더한다. 산 객체를 받아들이는
차트라 셀렉터·파드 템플릿 라벨처럼 움직이면 안 되는 필드와, 두 매니페스트 벌이 지키던 안전
규칙(자원, 파드 보안, NetworkPolicy, 프로브, preStop)을 본다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
import yaml

from daegam.config import _home_redis_tls_from_env, settings_from_env
from daegam.k8s.resources import validate_complete_resources, validate_limit_range
from daegam.k8s.templates.agent import PRESTOP_SECONDS
from tests.helpers.chart import (
    CHART,
    config_data,
    containers,
    named,
    pod_specs,
    pod_template,
    render,
    values_file,
)
from tests.helpers.consts import PLATFORM_NS, REPO

HOST = "daegam.example.com"
DIGEST = "sha256:" + "a" * 64
WORKLOADS = ("daegam-platform", "daegam-docparse", "daegam-ui")
# 파드 템플릿에 붙으면 차트 판이 바뀔 때마다 파드가 굴러가는 라벨
VERSION_LABELS = ("helm.sh/chart", "app.kubernetes.io/version")


def test_every_container_has_cpu_and_memory_requests_and_limits():
    for where, spec in pod_specs(render()):
        for c in containers(spec):
            validate_complete_resources(c.get("resources"), where=f"{where}:{c['name']}")


def test_pod_security_is_restricted_ready():
    """PSA restricted 가 요구하는 것을 모든 파드에 둔다. 함께 올리는 MongoDB 도 같다."""
    for where, spec in pod_specs(render()):
        pod = spec.get("securityContext") or {}
        assert pod.get("runAsNonRoot") is True, where
        assert pod.get("seccompProfile") == {"type": "RuntimeDefault"}, where
        for c in containers(spec):
            sc = c.get("securityContext") or {}
            assert sc.get("allowPrivilegeEscalation") is False, (where, c["name"])
            assert sc.get("capabilities") == {"drop": ["ALL"]}, (where, c["name"])


def test_the_release_namespace_limit_range_is_valid_and_off_by_default():
    assert [d for d in render() if d["kind"] == "LimitRange"] == []
    [lr] = [d for d in render(sets={"limitRange.enabled": True}) if d["kind"] == "LimitRange"]
    assert lr["metadata"] == lr["metadata"] | {"name": "daegam-defaults", "namespace": PLATFORM_NS}
    validate_limit_range(lr["spec"], where="limitRange")


def test_configmap_home_values_start_the_platform(tmp_path, bare_env):
    """home 값이 ConfigMap 의 DAEGAM_HOME_* 로 내려가고 플랫폼 설정이 그 값을 받는다."""
    resources = {
        "requests": {"cpu": "50m", "memory": "192Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }
    limit_range = {
        "limits": [
            {
                "type": "Container",
                "defaultRequest": {"cpu": "20m", "memory": "32Mi"},
                "default": {"cpu": "500m", "memory": "256Mi"},
            }
        ]
    }
    home = {
        "exposure": {
            "enabled": True,
            "className": "traefik",
            "controllerNamespace": "traefik",
            "domain": "apps.example.com",
            "tls": "cert-manager",
        },
        "networkPolicy": "enforced",
        "blockMetadata": True,
        "resources": resources,
        "limitRange": limit_range,
    }
    cm = config_data(render(values_file(tmp_path, {"home": home})))
    for key, value in cm.items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert settings.public_url == f"https://{HOST}"
    assert settings.home_ingress == {
        "enabled": True,
        "class_name": "traefik",
        "controller_namespace": "traefik",
        "domain": "apps.example.com",
        "reserved_hosts": [HOST],
        "allow_unauthenticated": False,
        "tls": "cert-manager",
    }
    assert settings.home_resources == resources
    assert settings.home_limit_range == limit_range
    assert settings.home_network_policy == "enforced"
    assert settings.home_block_metadata is True
    assert settings.home_images == {
        "registry_prefix": "registry.example.com/daegam",
        "pull_policy": "Always",
        "runtime_image": "daegam-mcp-runtime:0.1.0",
        "agent_image": "daegam-agent-runtime:0.1.0",
    }
    assert settings.web_tools_image == "daegam-web-tools:0.1.0"
    assert settings.home_sandbox_image == "daegam-sandbox:0.1.0"
    # 개발용 스위치는 차트 값이 없다(D25, D95)
    assert not settings.allow_stub_provider and not settings.sandbox_allow_unchecked


def test_the_default_configmap_leaves_screen_values_alone(bare_env):
    """기본값은 노출을 끄고 판정·메타데이터 차단·자원·LimitRange·함께 올린 Redis 를 적지 않는다
    (P85 4절). 적지 않은 것은 화면의 값이 그대로다."""
    cm = config_data(render())
    assert cm["DAEGAM_HOME_INGRESS_ENABLED"] == "false"
    for key in (
        "DAEGAM_HOME_BLOCK_METADATA",
        "DAEGAM_HOME_NETWORK_POLICY",
        "DAEGAM_HOME_RESOURCES",
        "DAEGAM_HOME_LIMIT_RANGE",
        "DAEGAM_HOME_REDIS_TLS",
        "DAEGAM_HOME_INGRESS_TLS",
        "DAEGAM_HOME_INGRESS_CLASS",
        "DAEGAM_OIDC_ISSUER",
        "DAEGAM_ALLOW_STUB_PROVIDER",
        "DAEGAM_SANDBOX_ALLOW_UNCHECKED",
    ):
        assert key not in cm, key
    assert cm["DAEGAM_AUTH_MODE"] == "local"
    for key, value in cm.items():
        bare_env.setenv(key, value)
    assert settings_from_env().auth_mode == "local"


@pytest.mark.parametrize(("value", "want"), [(True, "true"), (False, "false")])
def test_the_metadata_block_is_written_only_when_set(value, want):
    """true 와 false 는 기동마다 home 에 덮어쓰고, 비우면(기본값) 적지 않아 화면의 값을 둔다
    (T75)."""
    assert (
        config_data(render(sets={"home.blockMetadata": value}))["DAEGAM_HOME_BLOCK_METADATA"]
        == want
    )


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_public_url_ingress_host_and_reserved_host_come_from_one_host(scheme):
    """공개 주소, Ingress host, 예약 host 는 host 하나에서 나온다(D58)."""
    docs = render(sets={"scheme": scheme})
    cm = config_data(docs)
    ingress = named(docs, "Ingress", "daegam-platform")
    assert cm["DAEGAM_PUBLIC_URL"] == f"{scheme}://{HOST}"
    assert cm["DAEGAM_HOME_INGRESS_RESERVED_HOSTS"] == HOST
    assert [r["host"] for r in ingress["spec"]["rules"]] == [HOST]


def test_ingress_sends_api_paths_to_the_platform_and_the_rest_to_the_ui():
    """같은 호스트에서 경로로 가른다(D52). 끝의 / 는 Traefik 의 Prefix 가 글자 단위라서다."""
    [rule] = named(render(), "Ingress", "daegam-platform")["spec"]["rules"]
    routes = [
        (p["path"], p["pathType"], p["backend"]["service"]["name"], p["backend"]["service"]["port"])
        for p in rule["http"]["paths"]
    ]
    assert routes == [
        ("/api/", "Prefix", "daegam-platform", {"number": 9000}),
        ("/.well-known/", "Prefix", "daegam-platform", {"number": 9000}),
        ("/healthz", "Exact", "daegam-platform", {"number": 9000}),
        ("/", "Prefix", "daegam-ui", {"number": 8080}),
    ]


@pytest.mark.parametrize(
    ("tls", "spec_tls", "home_tls"),
    [
        ("none", None, None),
        ("edge", None, "edge"),
        ("daegam-tls", [{"hosts": [HOST], "secretName": "daegam-tls"}], None),
    ],
)
def test_ingress_tls_follows_the_tls_value(tls, spec_tls, home_tls):
    """edge 는 앞단 프록시가 TLS 를 끝내므로 Ingress 에 tls 가 없고 노출의 기본도 edge 다(D54)."""
    docs = render(sets={"ingress.tls": tls, "ingress.className": "traefik"})
    ingress = named(docs, "Ingress", "daegam-platform")
    assert ingress["spec"].get("tls") == spec_tls
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert config_data(docs).get("DAEGAM_HOME_INGRESS_TLS") == home_tls


def test_the_ingress_can_be_left_to_the_operator():
    assert [d for d in render(sets={"ingress.enabled": False}) if d["kind"] == "Ingress"] == []


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_ui_service_pins_a_browser_to_one_pod(scheme):
    """롤링 중 새 탭이 index.html 과 진입 번들을 같은 UI 파드에서 받게 Traefik sticky 쿠키를 건다
    (D52). Secure 는 https 일 때만이다. http 에 두면 브라우저가 쿠키를 돌려보내지 않는다."""
    svc = named(render(sets={"scheme": scheme}), "Service", "daegam-ui")
    prefix = "traefik.ingress.kubernetes.io/service.sticky.cookie"
    assert svc["metadata"]["annotations"] == {
        prefix: "true",
        f"{prefix}.name": "daegam_ui",
        f"{prefix}.httponly": "true",
        f"{prefix}.samesite": "lax",
        f"{prefix}.secure": "true" if scheme == "https" else "false",
    }
    off = named(render(sets={"ui.service.traefikStickyCookie": False}), "Service", "daegam-ui")
    assert "annotations" not in off["metadata"]


def test_ui_prestop_runs_the_drain_script_the_image_ships():
    """UI 파드의 preStop 은 이미지가 싣는 daegam-ui-drain 에 기다릴 초만 넘긴다 (D52)."""
    [ui] = pod_template(render(), "daegam-ui")["spec"]["containers"]
    script, *seconds = ui["lifecycle"]["preStop"]["exec"]["command"]
    assert f"docker/drain.sh {script}\n" in (REPO / "ui" / "Dockerfile").read_text()
    assert len(seconds) == 2 and all(s.isdigit() for s in seconds), seconds
    # kubelet 이 직접 부르는 httpGet 이라 preStop 이 도는 동안에도 준비 확인이 반영된다
    assert ui["readinessProbe"]["httpGet"] == {"path": "/ready", "port": "probe"}


@pytest.mark.parametrize("name", ["daegam-platform", "daegam-docparse"])
def test_platform_image_pods_wait_with_the_sleep_action(name):
    """플랫폼 이미지에는 셸도 sleep 도 없다(D55). preStop 은 쿠버네티스의 sleep 동작이고 기다리는
    초는 Agent 파드와 같다."""
    [container] = pod_template(render(), name)["spec"]["containers"]
    assert container["lifecycle"] == {"preStop": {"sleep": {"seconds": PRESTOP_SECONDS}}}
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}


def _token_ref(container: dict) -> dict:
    env = {e["name"]: e.get("valueFrom") for e in container.get("env", [])}
    return env["DAEGAM_DOCPARSE_TOKEN"]


def test_docparse_runs_the_platform_image_without_platform_secrets():
    """문서 파싱 서비스(D61)는 플랫폼 이미지를 명령만 바꿔 띄우고, SA 토큰과 플랫폼 설정 없이 따로
    둔 토큰 하나만 받는다. 플랫폼도 그 키만 같은 방식으로 받는다."""
    docs = render()
    spec = pod_template(docs, "daegam-docparse")["spec"]
    [docparse] = spec["containers"]
    [main] = pod_template(docs, "daegam-platform")["spec"]["containers"]
    assert docparse["image"] == main["image"]
    assert docparse["command"] == ["python", "-m", "daegam.documents.server"]
    assert spec["automountServiceAccountToken"] is False and "serviceAccountName" not in spec
    assert "envFrom" not in docparse
    assert [e["name"] for e in docparse["env"]] == ["DAEGAM_DOCPARSE_TOKEN"]
    ref = {"secretKeyRef": {"name": "daegam-docparse-token", "key": "token"}}
    assert _token_ref(docparse) == ref and _token_ref(main) == ref
    assert docparse["resources"]["limits"]["memory"] == "2Gi"
    assert docparse["securityContext"]["readOnlyRootFilesystem"] is True
    assert spec["securityContext"]["runAsUser"] != 0


def test_docparse_takes_only_platform_pods_and_cannot_go_out():
    policy = named(render(), "NetworkPolicy", "daegam-docparse")["spec"]
    assert policy["podSelector"] == {"matchLabels": {"app": "daegam-docparse"}}
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert "egress" not in policy  # 나가는 길은 모두 닫는다(DNS 포함)
    [rule] = policy["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"app": "daegam-platform"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 9100}]


def test_ui_cannot_go_out():
    policy = named(render(), "NetworkPolicy", "daegam-ui")["spec"]
    assert policy == {
        "podSelector": {"matchLabels": {"app": "daegam-ui"}},
        "policyTypes": ["Egress"],
    }


def test_pdbs_keep_one_pod_and_turn_off_per_workload():
    docs = render()
    for name in WORKLOADS:
        pdb = named(docs, "PodDisruptionBudget", name)["spec"]
        assert pdb == {"minAvailable": 1, "selector": {"matchLabels": {"app": name}}}
    off = render(sets={f"{k}.pdb.enabled": False for k in ("platform", "docparse", "ui")})
    assert [d for d in off if d["kind"] == "PodDisruptionBudget"] == []


def test_a_single_replica_gets_no_pdb():
    """파드 하나를 남기라는 PDB 는 그 파드 하나의 drain 을 막으므로 복제가 하나면 켜 두어도 두지
    않는다."""
    docs = render(sets={"docparse.replicas": 1})
    assert [d["metadata"]["name"] for d in docs if d["kind"] == "PodDisruptionBudget"] == [
        "daegam-platform",
        "daegam-ui",
    ]


# -- 움직이면 안 되는 필드 (P85 3절) -----------------------------------------------------------


@pytest.mark.parametrize("name", [*WORKLOADS, "mongodb"])
def test_selectors_and_pod_labels_are_the_live_ones(name):
    """산 객체를 받아들이므로 셀렉터와 파드 템플릿 라벨은 app 하나다. 차트 판이 바뀌어도 파드가
    굴러가지 않게 파드 템플릿에는 차트나 판 라벨이 없다. Helm 의 표준 라벨은 객체 메타데이터에만
    둔다."""
    docs = render()
    kind = "StatefulSet" if name == "mongodb" else "Deployment"
    workload = named(docs, kind, name)
    assert workload["spec"]["selector"] == {"matchLabels": {"app": name}}
    assert workload["spec"]["template"]["metadata"]["labels"] == {"app": name}
    assert workload["metadata"]["labels"]["app"] == name
    for key in VERSION_LABELS:
        assert key in workload["metadata"]["labels"], key
    assert named(docs, "Service", name)["spec"]["selector"] == {"app": name}


def test_services_stay_cluster_ip_without_a_fixed_address():
    for svc in (d for d in render() if d["kind"] == "Service"):
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP", svc["metadata"]["name"]
        assert "clusterIP" not in svc["spec"], svc["metadata"]["name"]


def test_priority_class_and_role_refs_keep_their_values():
    docs = render()
    pc = named(docs, "PriorityClass", "daegam-sandbox")
    assert (pc["value"], pc["preemptionPolicy"], pc["globalDefault"]) == (-10, "Never", False)
    refs = {
        (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d["roleRef"]
        for d in docs
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding")
    }
    group = "rbac.authorization.k8s.io"
    assert refs == {
        ("RoleBinding", PLATFORM_NS, "daegam-platform"): {
            "apiGroup": group,
            "kind": "Role",
            "name": "daegam-platform",
        },
        ("ClusterRoleBinding", None, "daegam-platform"): {
            "apiGroup": group,
            "kind": "ClusterRole",
            "name": "daegam-platform",
        },
        ("RoleBinding", "daegam-probe", "daegam-tenant-objects"): {
            "apiGroup": group,
            "kind": "ClusterRole",
            "name": "daegam-tenant-objects",
        },
    }
    probe = named(docs, "Namespace", "daegam-probe")["metadata"]["labels"]
    assert probe["pod-security.kubernetes.io/enforce"] == "restricted"


def _roles(docs: list[dict]) -> dict[str, list[dict]]:
    return {
        f"{d['kind']}/{d['metadata']['name']}": d["rules"]
        for d in docs
        if d["kind"] in ("ClusterRole", "Role")
    }


def test_roles_grant_no_wildcard_and_no_way_into_a_pod():
    """동사와 자원은 하나씩 적는다(`*` 없음). 어느 역할도 파드 안으로 들어가지 못한다(P40, D95)."""
    for where, rules in _roles(render()).items():
        for rule in rules:
            assert "*" not in rule["verbs"] and "*" not in rule["resources"], (where, rule)
            entry = {"pods/exec", "pods/attach", "pods/ephemeralcontainers"} & set(
                rule["resources"]
            )
            assert not entry, (where, rule)


def test_secret_verbs_are_exact_per_role():
    """Secret 은 클러스터 전체에 주지 않는다(P63, D75). 이름만 나열하는 것은 daegam-tls-reader
    하나이고 어느 역할도 watch 하지 않는다."""
    verbs = {
        where: sorted(v for r in rules if "secrets" in r["resources"] for v in r["verbs"])
        for where, rules in _roles(render()).items()
    }
    write = sorted(["get", "create", "update", "patch", "delete"])
    assert verbs == {
        "Role/daegam-platform": write,
        "ClusterRole/daegam-platform": [],
        "ClusterRole/daegam-tls-reader": ["list"],
        "ClusterRole/daegam-tenant-objects": write,
    }


# 워크로드마다 두 매니페스트 벌이 함께 지키던 계약: 포트, 설정을 읽는 자리, 볼륨, 보안 설정, 프로브
SLEEP_PROBES = {
    "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 5},
    "livenessProbe": {
        "httpGet": {"path": "/healthz", "port": "http"},
        "periodSeconds": 20,
        "initialDelaySeconds": 10,
    },
}
LOCKED = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
CONTRACT = {
    "daegam-platform": {
        "ports": [{"name": "http", "containerPort": 9000}],
        "envFrom": [
            {"configMapRef": {"name": "daegam-platform-config"}},
            {"secretRef": {"name": "daegam-platform-secrets"}},
        ],
        "env": ["DAEGAM_NAMESPACE", "DAEGAM_DOCPARSE_TOKEN"],
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
        "user": 1000,
        **SLEEP_PROBES,
    },
    "daegam-docparse": {
        "ports": [{"name": "http", "containerPort": 9100}],
        "envFrom": None,
        "env": ["DAEGAM_DOCPARSE_TOKEN"],
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}],
        "user": 1000,
        **SLEEP_PROBES,
    },
    "daegam-ui": {
        "ports": [
            {"name": "http", "containerPort": 8080},
            {"name": "probe", "containerPort": 8081},
        ],
        "envFrom": None,
        "env": ["DAEGAM_AUTH_MODE", "DAEGAM_OIDC_ISSUER"],
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        "volumes": [{"name": "tmp", "emptyDir": {}}],
        "user": 101,
        "readinessProbe": {
            "httpGet": {"path": "/ready", "port": "probe"},
            "periodSeconds": 1,
            "failureThreshold": 2,
        },
        "livenessProbe": {
            "httpGet": {"path": "/", "port": "http"},
            "periodSeconds": 20,
            "initialDelaySeconds": 5,
        },
    },
}


@pytest.mark.parametrize("name", CONTRACT)
def test_each_workload_keeps_its_contract(name):
    want = CONTRACT[name]
    spec = pod_template(render(), name)["spec"]
    [c] = spec["containers"]
    for key in ("ports", "envFrom", "volumeMounts", "readinessProbe", "livenessProbe"):
        assert c.get(key) == want[key], key
    assert [e["name"] for e in c["env"]] == want["env"]
    assert spec["volumes"] == want["volumes"]
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": want["user"],
        "runAsGroup": want["user"],
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert c["securityContext"] == LOCKED
    assert spec["enableServiceLinks"] is False


def test_mongodb_keeps_its_probe_and_security():
    """MongoDB 는 데이터 디렉터리에 써야 하므로 루트 파일 시스템만 쓰기를 연다."""
    sts = named(render(), "StatefulSet", "mongodb")["spec"]["template"]["spec"]
    [mongo] = sts["containers"]
    assert mongo["readinessProbe"]["periodSeconds"] == 10
    assert mongo["readinessProbe"]["timeoutSeconds"] == 5
    assert mongo["readinessProbe"]["exec"]["command"][:2] == ["sh", "-c"]
    assert "--authenticationDatabase admin" in mongo["readinessProbe"]["exec"]["command"][2]
    assert mongo["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert sts["securityContext"]["fsGroup"] == 999


def test_every_container_but_mongodb_has_a_read_only_root():
    for where, spec in pod_specs(render()):
        for c in containers(spec):
            want = where != "StatefulSet/mongodb"
            assert (c["securityContext"].get("readOnlyRootFilesystem") is True) is want, where


def test_only_the_platform_pod_mounts_a_service_account_token():
    """플랫폼만 쿠버네티스 API 를 부른다. 다른 파드는 SA 토큰을 받지 않는다."""
    mounts = {where: spec["automountServiceAccountToken"] for where, spec in pod_specs(render())}
    assert mounts == {
        "Deployment/daegam-platform": True,
        "Deployment/daegam-docparse": False,
        "Deployment/daegam-ui": False,
        "StatefulSet/mongodb": False,
    }


# -- 다시 뜨기 (P85 3절) -----------------------------------------------------------------------


def _annotations(docs: list[dict], name: str) -> dict[str, str]:
    return pod_template(docs, name)["metadata"].get("annotations") or {}


def test_a_config_change_rolls_the_platform_and_only_auth_keys_roll_the_ui():
    """플랫폼은 ConfigMap 의 체크섬을 기존 키 config-sha 로 싣는다. UI 는 자기가 읽는 두 키(인증
    모드, OIDC 발급자)의 체크섬만 싣는다."""
    base = render()
    exposed = render(sets={"home.exposure.enabled": True})
    oidc = render(
        sets={
            "auth.mode": "oidc",
            "auth.oidc.issuer": "https://idp.example.com",
            "auth.oidc.clientId": "daegam-ui",
        }
    )
    sha = "daegam.devops.ai.kr/config-sha"
    ui_sha = "daegam.devops.ai.kr/ui-config-sha"
    assert re.fullmatch(r"[0-9a-f]{16}", _annotations(base, "daegam-platform")[sha])
    assert (
        _annotations(exposed, "daegam-platform")[sha] != _annotations(base, "daegam-platform")[sha]
    )
    assert _annotations(exposed, "daegam-ui") == _annotations(base, "daegam-ui")
    assert _annotations(oidc, "daegam-ui")[ui_sha] != _annotations(base, "daegam-ui")[ui_sha]
    # existingSecret 의 토큰은 차트가 모르므로 토큰 체크섬은 없다. 바꾼 Secret 은 재시작으로 읽는다
    assert "daegam.devops.ai.kr/docparse-token-sha" not in _annotations(base, "daegam-platform")
    assert _annotations(base, "daegam-docparse") == {}


# -- 이미지 (P85 6절) --------------------------------------------------------------------------


def test_images_default_to_the_app_version_tag_and_always_pull():
    """판 태그는 make push-images 마다 옮겨 가므로 digest 가 없으면 늘 당긴다."""
    docs = render()
    for name, repo in (("daegam-platform", "daegam-platform"), ("daegam-ui", "daegam-ui")):
        [c] = pod_template(docs, name)["spec"]["containers"]
        assert c["image"] == f"registry.example.com/daegam/{repo}:0.1.0"
        assert c["imagePullPolicy"] == "Always"
    home = json.loads(config_data(docs)["DAEGAM_HOME_IMAGES"])
    assert home["pull_policy"] == "Always" and "pull_secret" not in home


def test_digests_pin_images_and_allow_the_cached_pull(tmp_path):
    images = {
        c: {"tag": f"{c.lower()}-stamp", "digest": DIGEST}
        for c in ("platform", "ui", "runtime", "agent", "webTools", "sandbox")
    }
    docs = render(values_file(tmp_path, {"images": images | {"pullSecret": "registry-pull"}}))
    [c] = pod_template(docs, "daegam-platform")["spec"]["containers"]
    assert c["image"] == f"registry.example.com/daegam/daegam-platform:platform-stamp@{DIGEST}"
    assert c["imagePullPolicy"] == "IfNotPresent"
    assert pod_template(docs, "daegam-ui")["spec"]["imagePullSecrets"] == [
        {"name": "registry-pull"}
    ]
    cm = config_data(docs)
    assert json.loads(cm["DAEGAM_HOME_IMAGES"]) == {
        "registry_prefix": "registry.example.com/daegam",
        "pull_policy": "IfNotPresent",
        "pull_secret": "registry-pull",
        "runtime_image": f"daegam-mcp-runtime:runtime-stamp@{DIGEST}",
        "agent_image": f"daegam-agent-runtime:agent-stamp@{DIGEST}",
    }
    assert cm["DAEGAM_WEB_TOOLS_IMAGE"] == f"daegam-web-tools:webtools-stamp@{DIGEST}"
    assert cm["DAEGAM_HOME_SANDBOX_IMAGE"] == f"daegam-sandbox:sandbox-stamp@{DIGEST}"


def test_one_unpinned_rendered_image_keeps_the_rendered_pods_pulling(tmp_path):
    """플랫폼이 렌더하는 파드는 pull policy 하나를 함께 쓰므로 넷이 모두 digest 일 때만
    IfNotPresent 다."""
    images = {c: {"digest": DIGEST} for c in ("runtime", "agent", "webTools")}
    home = config_data(render(values_file(tmp_path, {"images": images})))["DAEGAM_HOME_IMAGES"]
    assert json.loads(home)["pull_policy"] == "Always"


def _make_var(name: str) -> str:
    text = (REPO / "Makefile").read_text()
    return re.search(rf"^{name}\s*\?=\s*(\S+)", text, re.M).group(1)


def test_app_version_is_the_version_tag_push_images_moves():
    """기본 태그는 appVersion 이고, make push-images 가 늘 올리는 판 태그와 같아야 한다
    (P85 3절)."""
    app_version = yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"]
    for var in (
        "PLATFORM_VERSION",
        "UI_VERSION",
        "RUNTIME_VERSION",
        "AGENT_VERSION",
        "WEB_TOOLS_VERSION",
        "SANDBOX_VERSION",
    ):
        assert _make_var(var) == app_version, var


# -- Secret 과 MongoDB (P85 5절) ---------------------------------------------------------------


def test_existing_secrets_are_referenced_and_never_rendered(tmp_path):
    secrets = {k: {"existingSecret": f"my-{k}"} for k in ("platform", "docparse", "mongo")}
    docs = render(values_file(tmp_path, {"secrets": secrets}))
    assert [d for d in docs if d["kind"] == "Secret"] == []
    [main] = pod_template(docs, "daegam-platform")["spec"]["containers"]
    assert {"secretRef": {"name": "my-platform"}} in main["envFrom"]
    assert _token_ref(main)["secretKeyRef"]["name"] == "my-docparse"
    [mongo] = named(docs, "StatefulSet", "mongodb")["spec"]["template"]["spec"]["containers"]
    assert {e["valueFrom"]["secretKeyRef"]["name"] for e in mongo["env"]} == {"my-mongo"}


def test_bundled_mongodb_runs_with_auth_and_keeps_its_claim(tmp_path):
    docs = render(values_file(tmp_path, {"mongodb": {"storageClass": "local-path"}}))
    sts = named(docs, "StatefulSet", "mongodb")
    [mongo] = sts["spec"]["template"]["spec"]["containers"]
    assert mongo["args"] == ["--auth", "--bind_ip_all", "--wiredTigerCacheSizeGB", "0.25"]
    assert sts["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    [claim] = sts["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "data"  # PVC data-mongodb-0 의 이름이 여기서 나온다
    assert claim["spec"]["storageClassName"] == "local-path"
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert (
        "storageClassName"
        not in named(render(), "StatefulSet", "mongodb")["spec"]["volumeClaimTemplates"][0]["spec"]
    )


def test_an_external_mongodb_renders_no_store():
    docs = render(sets={"mongodb.bundled": False})
    assert not [d for d in docs if d["metadata"]["name"] == "mongodb"]


# -- 함께 올린 Redis (P61, P85 4절) -----------------------------------------------------------

# lib/redis_plain.sh 의 redis_plain_of 를 DAEGAM_HOME_REDIS_TLS 값 하나로 부른다
PLAIN_READER = f'. "{REPO / "scripts" / "lib" / "redis_plain.sh"}"; redis_plain_of "$VALUE"'


@pytest.mark.parametrize(("plain", "state"), [(True, "open"), (False, "closed")])
def test_the_shipped_redis_reads_back_like_the_wrapper_reads_it(
    tmp_path, monkeypatch, plain, state
):
    """home.shippedRedis 가 ConfigMap 에 실린 값을 플랫폼 설정과 lib/redis_plain.sh 가 같게 읽는다.
    그 Redis 를 두는 배포는 평문 포트의 상태를 이 값으로 읽어 다음 배포에 넘긴다."""
    shipped = {
        "hosts": ["redis.daegam-system", "redis.daegam-system.svc"],
        "port": 6379,
        "tls_port": 6380,
        "ca_configmap": "daegam-redis-ca",
        "plain": plain,
    }
    raw = config_data(render(values_file(tmp_path, {"home": {"shippedRedis": shipped}})))[
        "DAEGAM_HOME_REDIS_TLS"
    ]
    monkeypatch.setenv("DAEGAM_HOME_REDIS_TLS", raw)
    read = _home_redis_tls_from_env()
    assert read is not None and read.plain is plain and read.tls_port == 6380
    env = {"PATH": os.environ["PATH"], "VALUE": raw}
    out = subprocess.run(
        ["bash", "-c", PLAIN_READER], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == state
