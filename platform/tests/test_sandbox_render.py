"""샌드박스 파드와 정책 셋의 렌더, 정책의 다시 읽기, hostUsers 다시 읽기 (P40 2절·4절)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from daegam.config import Settings
from daegam.config.resolve import EffectiveNamespace, image_digest
from daegam.k8s.labels import LABEL_AGENT, LABEL_ID, LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.sandbox_policy import check_namespace_policies, selector_matches
from daegam.k8s.templates.agent import (
    RESOURCE as AGENT_RESOURCE,
)
from daegam.k8s.templates.agent import (
    render_agent_deployment,
    render_agent_expose_docs,
    render_agent_policy,
    render_agent_service,
)
from daegam.k8s.templates.sandbox import (
    ISOLATION_POLICY_NAME,
    PROBE_NAMESPACE,
    agent_sandbox_policy_name,
    check_targets,
    host_users_kept,
    placement_fingerprint,
    render_agent_sandbox_policy,
    render_isolation_policy,
    render_sandbox_pod,
    sandbox_cpu_request,
    sandbox_image,
    sandbox_labels,
    sandbox_pod_name,
    verdict_void_reason,
)
from daegam.services.tenancy import (
    NETWORK_POLICY_NAME,
    TenancyError,
    check_namespace_name,
    ensure_namespace_objects,
    network_policy_manifest,
)
from daegam.store.base import utcnow
from daegam.store.clusters import (
    ClusterImages,
    ClusterSandbox,
    ClusterScheduling,
    ClusterStatus,
    Preflight,
    SandboxCheck,
)
from daegam.store.exposure import AgentExposure
from tests.helpers.agent_runtime import minimal_agent_doc
from tests.helpers.consts import NP_API
from tests.helpers.docs import cluster_doc, effective_config
from tests.helpers.fake_cluster import FakeClusterClient

PLATFORM_ID = "install-1"
SHA = "ab" * 32
TARGETS = [
    {"kind": "tcp", "host": "10.0.0.5", "port": 6443},
    {"kind": "dns", "host": "10.43.0.10", "port": 53, "timeout": 2},
    {"kind": "icmp", "host": "1.1.1.1"},
]
TOLERATION = {"key": "sandbox", "operator": "Exists", "effect": "NoSchedule"}


def _pod(cluster=None, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "cluster": cluster or cluster_doc(),
        "agent": minimal_agent_doc(),
        "owner_uid": "dep-uid",
        "session_id": "s-1",
        "generation": 1,
        "sandbox_id": "sbx-id-1",
        "node": "node-a",
        "token_sha256": SHA,
        "targets": TARGETS,
        "platform_id": PLATFORM_ID,
    }
    return render_sandbox_pod(**{**args, **over})


def test_a_pod_without_a_known_owner_carries_no_owner_reference():
    """Deployment 의 uid 를 모르면 ownerReference 를 두지 않는다. 없는 uid 를 가리키면 GC 가
    파드를 바로 지운다."""
    assert "ownerReferences" not in _pod(owner_uid=None)["metadata"]
    assert _pod()["metadata"]["ownerReferences"][0]["blockOwnerDeletion"] is False


def _labels() -> dict[str, str]:
    agent = minimal_agent_doc()
    return sandbox_labels(
        tenant=agent.tenant, agent_id=agent.id, sandbox_id="sbx-id-1", platform_id=PLATFORM_ID
    )


# -- 파드 ----------------------------------------------------------------------------------


def test_the_pod_render() -> None:
    cluster = cluster_doc(
        images=ClusterImages(pull_secret="pull", pull_policy="Always", registry_prefix="r.io/d"),
        scheduling=ClusterScheduling(
            node_selector={"pool": "general"}, tolerations=[TOLERATION], priority_class="high"
        ),
    )
    agent = minimal_agent_doc()
    pod = _pod(cluster)
    assert pod == {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": sandbox_pod_name(agent.id, "s-1", 1),
            "namespace": agent.k8s.namespace,
            "labels": {
                LABEL_RESOURCE: "sandbox",
                LABEL_ID: "sbx-id-1",
                LABEL_AGENT: agent.id,
                "daegam.devops.ai.kr/tenant": agent.tenant,
                LABEL_PLATFORM_ID: PLATFORM_ID,
                "app.kubernetes.io/managed-by": "daegam",
            },
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": agent.k8s.name,
                    "uid": "dep-uid",
                    "blockOwnerDeletion": False,
                    "controller": False,
                }
            ],
        },
        "spec": {
            "hostUsers": False,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["127.0.0.1"]},
            "restartPolicy": "OnFailure",
            "terminationGracePeriodSeconds": 1,
            "activeDeadlineSeconds": 86400,
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "sandbox",
                    "image": "r.io/d/daegam-sandbox:0.1.0",
                    "imagePullPolicy": "Always",
                    "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                    "env": [
                        {"name": "SANDBOX_TOKEN_SHA256", "value": SHA},
                        {
                            "name": "SANDBOX_TARGETS",
                            "value": json.dumps(
                                check_targets(TARGETS), sort_keys=True, separators=(",", ":")
                            ),
                        },
                    ],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "2Gi"},
                        "limits": {"cpu": "1", "memory": "2Gi"},
                    },
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "runAsNonRoot": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                        "readOnlyRootFilesystem": True,
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "periodSeconds": 1,
                        "timeoutSeconds": 1,
                        "failureThreshold": 3,
                    },
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": "/workspace"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "workspace", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
                {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"}},
            ],
            "imagePullSecrets": [{"name": "pull"}],
            "nodeSelector": {"pool": "general"},
            "tolerations": [TOLERATION],
            "priorityClassName": "daegam-sandbox",
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": ["node-a"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        },
    }
    # 토큰도 비밀도 없다. env 는 토큰의 sha256 과 점검 대상뿐이다
    assert "secretKeyRef" not in json.dumps(pod) and "envFrom" not in json.dumps(pod)


def test_the_sandbox_scheduling_wins_over_the_general_placement() -> None:
    own = {"node_selector": {"pool": "sandbox"}, "tolerations": [TOLERATION]}
    cluster = cluster_doc(
        scheduling=ClusterScheduling(node_selector={"pool": "general"}, priority_class="high"),
        sandbox=ClusterSandbox(scheduling=own),
    )
    spec = _pod(cluster)["spec"]
    assert (spec["nodeSelector"], spec["tolerations"]) == ({"pool": "sandbox"}, [TOLERATION])
    assert spec["priorityClassName"] == "daegam-sandbox"
    # 배치가 바뀌면 지문이 바뀐다. 판정은 그 지문을 맞대 무효로 읽힌다(8절)
    assert placement_fingerprint(cluster) != placement_fingerprint(cluster_doc())
    # 일반 배치만 있으면 그것이다
    general = cluster_doc(scheduling=ClusterScheduling(node_selector={"pool": "general"}))
    assert _pod(general)["spec"]["nodeSelector"] == {"pool": "general"}
    assert "nodeSelector" not in _pod()["spec"] and "tolerations" not in _pod()["spec"]


def test_the_pin_names_exactly_one_node_and_the_name_follows_the_generation() -> None:
    terms = _pod(node="n7")["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    pin = {"key": "kubernetes.io/hostname", "operator": "In", "values": ["n7"]}
    assert terms == [{"matchExpressions": [pin]}]
    with pytest.raises(ValueError, match="one node"):
        _pod(node="")
    first, again = sandbox_pod_name("a", "s", 1), sandbox_pod_name("a", "s", 1)
    assert first == again != sandbox_pod_name("a", "s", 2) != sandbox_pod_name("a", "t", 1)
    assert first.startswith("sbx-") and len(first) <= 63


def test_the_override_and_the_workspace_size_reach_the_pod() -> None:
    pod = _pod(unchecked=True, workspace_bytes=512 * 1024**2, cpu_request="50m")
    container = pod["spec"]["containers"][0]
    assert {"name": "SANDBOX_UNCHECKED", "value": "true"} in container["env"]
    assert container["resources"] == {
        "requests": {"cpu": "50m", "memory": "1536Mi"},
        "limits": {"cpu": "1", "memory": "1536Mi"},
    }
    assert pod["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] == "512Mi"
    assert "SANDBOX_UNCHECKED" not in json.dumps(_pod())


def test_the_image_comes_from_the_cluster_over_the_default() -> None:
    digest = "daegam-sandbox:abc@sha256:" + "c" * 64
    prefixed = cluster_doc(images=ClusterImages(registry_prefix="registry.example/daegam"))
    assert sandbox_image(prefixed) == "registry.example/daegam/daegam-sandbox:0.1.0"
    own = cluster_doc(
        images=ClusterImages(registry_prefix="registry.example/daegam"),
        sandbox=ClusterSandbox(image=digest),
    )
    assert sandbox_image(own) == f"registry.example/daegam/{digest}"
    hosted = cluster_doc(sandbox=ClusterSandbox(image=f"other.io/x/{digest}"))
    assert sandbox_image(hosted) == f"other.io/x/{digest}"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"kind": "http", "host": "10.0.0.1", "port": 80}, "kind"),
        ({"kind": "tcp", "host": "kubernetes.default", "port": 443}, "IP address"),
        ({"kind": "tcp", "host": "10.0.0.1"}, "port"),
        ({"kind": "dns", "host": "10.0.0.1", "port": True}, "port"),
        ({"kind": "tcp", "host": "10.0.0.1", "port": 1, "timeout": 11}, "timeout"),
        ({"kind": "icmp", "host": "1.1.1.1", "timeout": 0}, "timeout"),
    ],
)
def test_targets_are_checked_against_the_server_contract(target, message) -> None:
    with pytest.raises(ValueError, match=message):
        check_targets([target])
    with pytest.raises(ValueError, match="self-check targets"):
        check_targets([])
    with pytest.raises(ValueError, match="token_sha256"):
        _pod(token_sha256="AB" * 32)


def test_the_cpu_request_meets_the_namespace_minimum() -> None:
    assert sandbox_cpu_request(None) == "10m"
    limit_range = {"limits": [{"type": "Container", "min": {"cpu": "50m"}}, {"type": "Pod"}]}
    assert sandbox_cpu_request(limit_range) == "50m"
    assert sandbox_cpu_request({"limits": [{"type": "Container", "min": {"cpu": "5m"}}]}) == "10m"


def test_host_users_is_read_back() -> None:
    assert host_users_kept(_pod()) is True
    dropped = _pod()
    dropped["spec"].pop("hostUsers")
    assert host_users_kept(dropped) is False
    assert host_users_kept({"spec": {"hostUsers": True}}) is False


# -- 어느 셀렉터도 샌드박스를 고르지 않는다 -------------------------------------------------


def test_no_selector_that_finds_agent_pods_matches_a_sandbox_pod() -> None:
    """Deployment·Service·Agent 정책·노출 정책·테넌트 정책·감사 수집·인스턴스 상태·토큰 정리·
    port-forward(Service 의 selector)가 쓰는 셀렉터가 샌드박스 파드의 라벨에 맞지 않는다."""
    agent = minimal_agent_doc()
    labels = _pod()["metadata"]["labels"]
    deployment = render_agent_deployment(
        agent,
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )
    expose = render_agent_expose_docs(
        agent,
        AgentExposure(enabled=True, host="bot.example", ingress_class="traefik"),
        ingress=cluster_doc().ingress,
        owner_uid=None,
    )
    selectors = {
        "deployment": deployment["spec"]["selector"],
        "service": {"matchLabels": render_agent_service(agent)["spec"]["selector"]},
        "agent policy": render_agent_policy(agent, "daegam-system")["spec"]["podSelector"],
        "expose policy": next(d for d in expose if d["kind"] == "NetworkPolicy")["spec"][
            "podSelector"
        ],
        "tenant policy": network_policy_manifest(agent.tenant, "ns", "daegam-system")["spec"][
            "podSelector"
        ],
        # services/audit_collector.py, services/instance_status.py (인스턴스 id 하나로)
        "audit collector": {"matchLabels": {LABEL_RESOURCE: AGENT_RESOURCE}},
        "instance status": {"matchLabels": {LABEL_ID: agent.id}},
    }
    matched = [name for name, sel in selectors.items() if selector_matches(sel, labels)]
    assert matched == []
    # 그 셀렉터들은 Agent 파드는 고른다(셀렉터를 잘못 읽은 시험이 아니다)
    agent_pod = deployment["spec"]["template"]["metadata"]["labels"]
    assert selector_matches(selectors["deployment"], agent_pod)
    assert selector_matches(selectors["tenant policy"], agent_pod)


# -- 정책 셋 --------------------------------------------------------------------------------


def test_the_isolation_policy_carries_no_agent_id() -> None:
    policy = render_isolation_policy("acme", "daegam-acme")
    assert policy["spec"] == {
        "podSelector": {"matchLabels": {LABEL_RESOURCE: "sandbox"}},
        "policyTypes": ["Ingress", "Egress"],
    }
    labels = policy["metadata"]["labels"]
    assert labels[LABEL_ID] == "acme" and LABEL_AGENT not in labels
    # Agent 를 내릴 때의 라벨 지우기(`delete_labeled`)는 Agent id 로 고른다
    assert minimal_agent_doc().id not in json.dumps(policy)


def test_the_agent_policy_opens_8080_to_its_own_pods_only() -> None:
    agent = minimal_agent_doc()
    policy = render_agent_sandbox_policy(agent)
    assert policy["metadata"]["name"] == agent_sandbox_policy_name(agent.id)
    assert policy["metadata"]["labels"][LABEL_ID] == agent.id
    agent_pods = {LABEL_RESOURCE: AGENT_RESOURCE, LABEL_ID: agent.id}
    assert policy["spec"] == {
        "podSelector": {"matchLabels": {LABEL_RESOURCE: "sandbox", LABEL_AGENT: agent.id}},
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [{"podSelector": {"matchLabels": agent_pods}}],
                "ports": [{"protocol": "TCP", "port": 8080}],
            }
        ],
    }


def test_ensure_namespace_objects_applies_both_namespace_policies() -> None:
    fake = FakeClusterClient()
    ensure_namespace_objects(fake, "acme", EffectiveNamespace(name="daegam-acme"))
    kinds = [(o["kind"], o["metadata"]["name"]) for o in fake.applied]
    # 격리 정책이 먼저다. 테넌트 정책이 샌드박스를 빼는 사이에 샌드박스가 격리 없이 남지 않는다.
    # 사이의 것은 MCP 백엔드 정책이다 (T67)
    assert kinds[:4] == [
        ("Namespace", "daegam-acme"),
        ("NetworkPolicy", ISOLATION_POLICY_NAME),
        ("NetworkPolicy", "daegam-mcp-backend"),
        ("NetworkPolicy", NETWORK_POLICY_NAME),
    ]
    tenant = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, "daegam-acme")
    assert tenant["spec"]["podSelector"]["matchExpressions"][0] == {
        "key": LABEL_RESOURCE,
        "operator": "NotIn",
        "values": ["sandbox"],
    }
    assert fake.get(NP_API, "NetworkPolicy", ISOLATION_POLICY_NAME, "daegam-acme") is not None


def test_tenants_cannot_take_the_probe_namespace() -> None:
    with pytest.raises(TenancyError) as e:
        check_namespace_name(PROBE_NAMESPACE, reserved=Settings().reserved_namespaces)
    assert e.value.code == "namespace_reserved"


# -- 정책의 다시 읽기 --------------------------------------------------------------------------


def _namespace_policies(tenant: str = "acme") -> list[dict[str, Any]]:
    agent = minimal_agent_doc()
    return [
        network_policy_manifest(tenant, "daegam-acme", "daegam-system"),
        render_isolation_policy(tenant, "daegam-acme"),
        render_agent_sandbox_policy(agent),
    ]


def _policy(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {"metadata": {"name": name}, "spec": spec}


def _check(policies: list[dict[str, Any]]):
    return check_namespace_policies(policies, agent=minimal_agent_doc(), labels=_labels())


def test_the_rendered_policies_pass_the_read_back() -> None:
    assert _check(_namespace_policies()) is None
    # 다른 Agent 의 정책과 아무 파드도 고르지 않는 정책은 보지 않는다
    other = render_agent_sandbox_policy(minimal_agent_doc(id="ag-2"))
    unrelated = _policy("x", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{}]})
    assert _check([*_namespace_policies(), other, unrelated]) is None


def test_a_tenant_policy_with_an_extra_source_passes() -> None:
    policies = _namespace_policies()
    policies[0]["spec"]["ingress"][0]["from"].append({"ipBlock": {"cidr": "10.0.0.0/8"}})
    assert _check(policies) is None


def test_an_allow_all_policy_refuses_opens_in_that_namespace_only() -> None:
    allow_all = _policy(
        "allow-all", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "egress": [{}]}
    )
    refused = _check([*_namespace_policies(), allow_all])
    assert refused is not None and refused.policy == "allow-all"
    # 다른 네임스페이스의 정책 목록은 그대로 통과한다
    assert _check(_namespace_policies()) is None


@pytest.mark.parametrize(
    ("change", "policy"),
    [
        # 샌드박스를 빼지 않은 옛 테넌트 정책(되돌린 판이 다시 쓴 것)
        (lambda p: p[0]["spec"].update(podSelector={}), NETWORK_POLICY_NAME),
        (lambda p: p.pop(1), ISOLATION_POLICY_NAME),
        (lambda p: p[1]["spec"].update(policyTypes=["Ingress"]), ISOLATION_POLICY_NAME),
        (lambda p: p[1]["spec"].update(egress=[{}]), ISOLATION_POLICY_NAME),
        (
            lambda p: p[1]["spec"].update(podSelector={"matchLabels": {LABEL_AGENT: "x"}}),
            ISOLATION_POLICY_NAME,
        ),
        (
            lambda p: p[2]["spec"]["ingress"][0]["from"].append({"podSelector": {}}),
            "agent-policy",
        ),
        (
            lambda p: p[2]["spec"]["ingress"][0].update(ports=[{"protocol": "TCP", "port": 22}]),
            "agent-policy",
        ),
        (lambda p: p[2]["spec"].update(egress=[{}]), "agent-policy"),
    ],
)
def test_the_read_back_names_the_policy_that_breaks_isolation(change, policy) -> None:
    policies = _namespace_policies()
    change(policies)
    refused = _check(policies)
    want = agent_sandbox_policy_name(minimal_agent_doc().id) if policy == "agent-policy" else policy
    assert refused is not None and refused.policy == want


def test_selectors_follow_kubernetes_rules() -> None:
    labels = {"a": "1", "b": "2"}
    assert selector_matches({}, labels) and selector_matches(None, labels)
    assert selector_matches({"matchLabels": {"a": "1"}}, labels)
    assert not selector_matches({"matchLabels": {"a": "2"}}, labels)
    for op, values, want in (
        ("In", ["1", "3"], True),
        ("NotIn", ["1"], False),
        ("Exists", [], True),
        ("DoesNotExist", [], False),
        ("Gt", ["0"], False),
    ):
        expr = {"key": "a", "operator": op, "values": values}
        assert selector_matches({"matchExpressions": [expr]}, labels) is want, op
    absent = {"key": "c", "operator": "NotIn", "values": ["x"]}
    assert selector_matches({"matchExpressions": [absent]}, labels)


# -- 판정이 무효로 읽히는 때 (8절) -----------------------------------------------------------------

DIGEST = "sha256:" + "d" * 64


def _checked(**fields: Any):
    """점검이 통과한 클러스터. 판정은 그때의 배치·이미지·판·컨트롤러를 적었다."""
    cluster = cluster_doc(
        status=ClusterStatus(
            preflight=Preflight(ok=True, k8s_version="v1.36.4+k3s1", policy_controller="k3s")
        )
    )
    check = SandboxCheck(
        passed=True,
        checked_at=utcnow(),
        placement=placement_fingerprint(cluster),
        image=sandbox_image(cluster),
        image_id=f"docker.io/library/daegam-sandbox@{DIGEST}",
        k8s_version="v1.36.4+k3s1",
        policy_controller="k3s",
    )
    cluster.sandbox = ClusterSandbox(check=check.model_copy(update=fields))
    return cluster


def test_a_fresh_verdict_reads_as_valid() -> None:
    cluster = _checked()
    assert verdict_void_reason(cluster) is None
    assert verdict_void_reason(cluster, image_id=DIGEST) is None
    assert verdict_void_reason(cluster_doc()) is None  # 점검한 적이 없다


def test_the_verdict_is_void_when_the_image_it_checked_changes() -> None:
    """사람의 저장이 `sandbox.image` 를 바꾸거나, 열린 파드가 다른 이미지 id 로 뜨면(기본값처럼
    digest 없는 태그가 옮겨 갔을 때) 판정은 무효로 읽힌다."""
    saved = _checked()
    saved.sandbox.image = "daegam-sandbox:other"
    assert verdict_void_reason(saved) == "image"
    moved = "docker.io/library/daegam-sandbox@sha256:" + "e" * 64
    assert verdict_void_reason(_checked(), image_id=moved) == "image"
    assert verdict_void_reason(_checked(image_id=None), image_id=DIGEST) == "image"
    assert image_digest(f"registry.example/d/daegam-sandbox:abc@{DIGEST}") == DIGEST
    assert image_digest("daegam-sandbox:0.1.0") is None


def test_the_verdict_is_void_on_placement_version_controller_or_a_written_void() -> None:
    placed = _checked()
    placed.sandbox.scheduling.node_selector = {"pool": "sbx"}
    assert verdict_void_reason(placed) == "placement"
    upgraded = _checked()
    assert upgraded.status.preflight is not None
    upgraded.status.preflight.k8s_version = "v1.37.0+k3s1"
    assert verdict_void_reason(upgraded) == "k8s_version"
    swapped = _checked()
    assert swapped.status.preflight is not None
    swapped.status.preflight.policy_controller = "kube-system/cilium"
    assert verdict_void_reason(swapped) == "policy_controller"
    assert verdict_void_reason(_checked(passed=False, voided_at=utcnow())) == "voided"
