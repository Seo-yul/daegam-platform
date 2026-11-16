"""테넌트 네임스페이스의 NetworkPolicy 묶음 (T67, T75).

MCPServer 의 백엔드 파드는 그 네임스페이스의 MCPServer 프록시 파드에서만 들어온다. 테넌트 ingress
정책은 그 파드를 빼고 백엔드 정책만 그 파드를 고른다. 파드의 라벨은 ToolHive 0.53.0 의 소스대로
짓는다. 프록시 파드는 오퍼레이터의 라벨에 `podTemplateMetadataOverrides` 의 라벨을 더하고(겹치면
오퍼레이터의 것), 백엔드 파드는 podTemplateSpec 의 라벨에 프록시 러너의 라벨을 덮는다. 인증 재적용의
스윕은 묶음의 판이 다른 네임스페이스에 묶음을 다시 둔다.

egress 정책은 메타데이터 주소만 막는다. 클러스터의 스위치가 켜져 있고 정책 컨트롤러가 확인된 것일
때만 두고, 관리자가 egress 정책을 둔 네임스페이스에는 두지 않는다. 플랫폼이 한 네임스페이스에 두는
정책을 모두 렌더해, 나가는 길을 이미 좁힌 파드(샌드박스, 프로브, 웹 fetch 백엔드)를 그 정책이 고르지
않는 것을 본다. 고르면 합집합이 그 파드의 나가는 길을 연다.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from daegam.config import settings_from_env
from daegam.k8s.labels import APP_MCPSERVER, LABEL_APP_NAME, LABEL_MCP_BACKEND, LABEL_RESOURCE
from daegam.k8s.sandbox_policy import selector_matches
from daegam.k8s.templates.agent import render_agent_deployment, render_agent_policy, selector_labels
from daegam.k8s.templates.exposure import render_expose_policy
from daegam.k8s.templates.sandbox import (
    ISOLATION_POLICY_NAME,
    render_agent_sandbox_policy,
    render_probe_pod,
    sandbox_labels,
)
from daegam.k8s.templates.tenant_policies import (
    BACKEND_POLICY_NAME,
    EGRESS_POLICY_NAME,
    METADATA_BLOCK_SKIPPED,
    METADATA_BLOCK_UNSUPPORTED,
    METADATA_EXCEPT,
    egress_policy_wanted,
    metadata_block_warnings,
)
from daegam.k8s.templates.vmcp import render_image_server
from daegam.k8s.templates.web_tools import FETCH_LABEL, render_fetch_clone
from daegam.services.tenancy import (
    ANNOTATION_NAMESPACE_POLICIES,
    NETWORK_POLICY_NAME,
    ensure_namespace_policies,
    foreign_egress_policies,
    live_namespace_policies,
    namespace_policies,
    namespace_policies_stamp,
    oidc_config_manifests,
    policy_stamp_of,
)
from daegam.store.clusters import Preflight, home_env_fields
from daegam.store.settings import WebFetchSettings
from tests.helpers.agent_runtime import minimal_agent_doc
from tests.helpers.clusters import post_cluster
from tests.helpers.consts import DEFAULT_NS, GV, NP_API
from tests.helpers.docs import cluster_doc, effective_config
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import run_data_plane_auth

NS = "daegam-acme"


def _operator_pods(cr: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """오퍼레이터와 프록시 러너가 MCPServer 하나에서 띄우는 (프록시 파드, 백엔드 파드)의 라벨.

    ToolHive 0.53.0 소스의 `labelsForMCPServer`·`DeployWorkload` 를 읽은 대로 짓는다. 소스를 읽은
    것을 옮겨 적었을 뿐이라 라벨이 파드에 실제로 닿는다는 증거가 아니다. 그 증거는 클러스터의
    `make e2e-install` STEP 5n 이다. 프록시 파드는 오퍼레이터의 라벨뿐이고, 백엔드 파드는
    podTemplateSpec 의 라벨에 프록시 러너의 라벨을 덮는다."""
    name, spec = cr["metadata"]["name"], cr["spec"]
    proxy = {
        "app": "mcpserver",
        "app.kubernetes.io/name": "mcpserver",
        "app.kubernetes.io/instance": name,
        "toolhive": "true",
        "toolhive-name": name,
    }
    template = spec["podTemplateSpec"]["metadata"]["labels"]
    backend = {**template, "app": name, "toolhive": "true", "toolhive-name": name}
    return proxy, backend


def _image_server(name: str = "fetch") -> dict[str, Any]:
    return render_image_server(
        tenant="acme",
        item_id="hi-1",
        name=name,
        namespace=NS,
        group="g",
        image="ghcr.io/x/fetch:1",
        transport="streamable-http",
        port=8000,
        tool_config=None,
    )


def _fetch_docs() -> list[dict[str, Any]]:
    return render_fetch_clone(
        tenant="acme",
        item_id="hi-2",
        name="platform-web-fetch",
        namespace=NS,
        group="g",
        image="daegam-web-tools:0.1.0",
        fetch=WebFetchSettings(),
        ca_bundle=None,
        effective=None,
        blocked=["169.254.0.0/16"],
    )


def _fetch_clone() -> dict[str, Any]:
    return next(d for d in _fetch_docs() if d["kind"] == "MCPServer")


def _agent_pod() -> dict[str, str]:
    deployment = render_agent_deployment(
        minimal_agent_doc(),
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )
    return deployment["spec"]["template"]["metadata"]["labels"]


def _pods() -> dict[str, dict[str, str]]:
    """테넌트 네임스페이스에 서는 파드의 라벨. vMCP·원격 프록시는 오퍼레이터의 라벨뿐이다."""
    image_proxy, image_backend = _operator_pods(_image_server())
    fetch_proxy, fetch_backend = _operator_pods(_fetch_clone())
    agent = minimal_agent_doc()
    return {
        "image proxy": image_proxy,
        "image backend": image_backend,
        "fetch proxy": fetch_proxy,
        "fetch backend": fetch_backend,
        "vmcp": {"app.kubernetes.io/name": "virtualmcpserver", "app.kubernetes.io/instance": "g"},
        "remote proxy": {"app.kubernetes.io/name": "mcpremoteproxy", "toolhive": "true"},
        "agent": _agent_pod(),
        "sandbox": sandbox_labels(
            tenant="acme", agent_id=agent.id, sandbox_id="sb-1", platform_id="install"
        ),
        "probe": render_probe_pod(
            cluster=cluster_doc(),
            name="sbx-probe-1",
            namespace=NS,
            node=None,
            targets=[{"kind": "tcp", "host": "10.0.0.1", "port": 80}],
            tenant="acme",
            platform_id="install",
        )["metadata"]["labels"],
        "unlabeled": {"app": "web"},
    }


def _namespace_render() -> list[dict[str, Any]]:
    """플랫폼이 한 테넌트 네임스페이스에 두는 NetworkPolicy 전부. 묶음, Agent 의 정책과 샌드박스
    정책, MCP Server 앞단과 Agent 의 노출 정책, 웹 fetch 복제본의 정책이다."""
    agent = minimal_agent_doc()
    expose = {"tenant": "acme", "namespace": NS, "controller_namespace": "traefik"}
    return [
        *namespace_policies("acme", NS, "daegam-system", egress=True),
        render_agent_policy(agent, "daegam-system"),
        render_agent_sandbox_policy(agent),
        render_expose_policy(
            instance_id="ms-1",
            resource="mcp-instance-expose-policy",
            name="daegam-expose-mcp-calc",
            front_name="expose-mcp-calc",
            front_kind="mcpremoteproxy",
            **expose,
        ),
        render_expose_policy(
            instance_id=agent.id,
            resource="agent-expose-policy",
            name="daegam-expose-agent",
            pod_selector=selector_labels(agent),
            **expose,
        ),
        next(d for d in _fetch_docs() if d["kind"] == "NetworkPolicy"),
    ]


def _policy(policies: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(p for p in policies if p["metadata"]["name"] == name)


def _selecting(policies: list[dict[str, Any]], labels: dict[str, str], kind: str) -> list[str]:
    return [
        p["metadata"]["name"]
        for p in policies
        if kind in p["spec"]["policyTypes"] and selector_matches(p["spec"]["podSelector"], labels)
    ]


def _admits(policy: dict[str, Any], labels: dict[str, str]) -> bool:
    """정책의 ingress 출처 가운데 같은 네임스페이스의 podSelector 가 labels 에 맞는 것이 있는가."""
    return any(
        "namespaceSelector" not in peer and selector_matches(peer.get("podSelector"), labels)
        for rule in policy["spec"].get("ingress") or []
        for peer in rule.get("from") or []
    )


# -- 렌더 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("render", [_image_server, _fetch_clone], ids=["image", "fetch"])
def test_only_the_backend_pod_carries_the_platform_label(render) -> None:
    """프록시 파드는 오퍼레이터의 라벨로 가린다. 플랫폼은 프록시 파드에 라벨을 더하지 않는다."""
    cr = render()
    proxy, backend = _operator_pods(cr)
    assert backend[LABEL_MCP_BACKEND] == cr["metadata"]["name"]
    assert LABEL_MCP_BACKEND not in proxy and proxy[LABEL_APP_NAME] == APP_MCPSERVER
    assert "resourceOverrides" not in cr["spec"]


def test_the_fetch_backend_keeps_its_web_tool_label() -> None:
    _proxy, backend = _operator_pods(_fetch_clone())
    assert backend[FETCH_LABEL] == "fetch"


def test_an_mcp_server_deploy_labels_its_backend_and_ensures_the_policy_set(
    client, fake_clusters
) -> None:
    _cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    cr = fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)
    proxy, backend = _operator_pods(cr)
    assert backend[LABEL_MCP_BACKEND] == "mcp-calc-1" and LABEL_MCP_BACKEND not in proxy
    names = [p["metadata"]["name"] for p in fake.find("NetworkPolicy")]
    assert {ISOLATION_POLICY_NAME, BACKEND_POLICY_NAME, NETWORK_POLICY_NAME} <= set(names)
    # 스위치는 기본으로 꺼져 있다 (T75)
    assert EGRESS_POLICY_NAME not in names
    tenant = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, DEFAULT_NS)
    want = namespace_policies("default", DEFAULT_NS, "daegam-system")
    assert tenant["metadata"]["annotations"][ANNOTATION_NAMESPACE_POLICIES] == (
        namespace_policies_stamp(want)
    )


# -- 정책 묶음 -------------------------------------------------------------------------------


def test_backend_pods_admit_only_mcpserver_proxies() -> None:
    policies = namespace_policies("acme", NS, "daegam-system")
    pods = _pods()
    for name in ("image backend", "fetch backend"):
        assert _selecting(policies, pods[name], "Ingress") == [BACKEND_POLICY_NAME], name
    backend = _policy(policies, BACKEND_POLICY_NAME)
    admitted = sorted(name for name, labels in pods.items() if _admits(backend, labels))
    assert admitted == ["fetch proxy", "image proxy"]
    # 출처는 같은 네임스페이스뿐이다. 플랫폼 네임스페이스와 테넌트의 다른 네임스페이스는 들지 않는다
    peers = [peer for rule in backend["spec"]["ingress"] for peer in rule["from"]]
    assert all("namespaceSelector" not in peer and "ipBlock" not in peer for peer in peers)


def test_every_other_pod_keeps_the_tenant_policy() -> None:
    policies = namespace_policies("acme", NS, "daegam-system")
    for name, labels in _pods().items():
        if name.endswith("backend") or name in ("sandbox", "probe"):
            continue
        assert _selecting(policies, labels, "Ingress") == [NETWORK_POLICY_NAME], name


def test_no_pod_is_left_without_an_ingress_policy() -> None:
    """어느 ingress 정책도 고르지 않은 파드는 다 열린다. 묶음과 Agent 의 정책을 함께 두면 모든
    파드가 하나 이상의 ingress 정책에 든다."""
    policies = [
        *namespace_policies("acme", NS, "daegam-system"),
        render_agent_sandbox_policy(minimal_agent_doc()),
    ]
    for name, labels in _pods().items():
        assert _selecting(policies, labels, "Ingress"), name


def _stamps(fake: FakeClusterClient) -> dict[str, str | None]:
    return {ns: policy_stamp_of(p) for ns, p in live_namespace_policies(fake).items()}


def test_the_narrow_policies_go_first() -> None:
    fake = FakeClusterClient()
    assert ensure_namespace_policies(fake, "acme", NS, "daegam-system", egress=True)
    assert [o["metadata"]["name"] for o in fake.applied] == [
        ISOLATION_POLICY_NAME,
        BACKEND_POLICY_NAME,
        NETWORK_POLICY_NAME,
        EGRESS_POLICY_NAME,
    ]
    want = namespace_policies("acme", NS, "daegam-system", egress=True)
    assert _stamps(fake) == {NS: namespace_policies_stamp(want)}


def test_the_stamp_follows_the_set() -> None:
    one = namespace_policies_stamp(namespace_policies("acme", NS, "daegam-system"))
    assert one == namespace_policies_stamp(namespace_policies("acme", NS, "daegam-system"))
    assert one != namespace_policies_stamp(namespace_policies("acme", NS, "other-platform"))


# -- 옛 네임스페이스 ------------------------------------------------------------------------


def test_the_sweep_brings_an_old_namespace_the_policy_set(client, fake_clusters) -> None:
    """이 판 전의 렌더(백엔드 정책 없음, 백엔드를 빼지 않은 테넌트 정책, 판 없음)를 둔
    네임스페이스는 배포 없이 다음 재적용 틱에 묶음을 받는다. 판이 같으면 다시 쓰지 않는다."""
    _cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    fake.delete(NP_API, "NetworkPolicy", BACKEND_POLICY_NAME, DEFAULT_NS)
    old = fake.objects[(NP_API, "NetworkPolicy", DEFAULT_NS, NETWORK_POLICY_NAME)]
    old["metadata"].pop("annotations", None)
    old["spec"]["podSelector"] = {
        "matchExpressions": [{"key": LABEL_RESOURCE, "operator": "NotIn", "values": ["sandbox"]}]
    }
    assert _stamps(fake) == {DEFAULT_NS: None}

    run_data_plane_auth(client)
    want = namespace_policies("default", DEFAULT_NS, "daegam-system")
    assert _stamps(fake) == {DEFAULT_NS: namespace_policies_stamp(want)}
    assert fake.get(NP_API, "NetworkPolicy", BACKEND_POLICY_NAME, DEFAULT_NS) is not None
    tenant = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, DEFAULT_NS)
    assert (
        tenant["spec"]["podSelector"] == _policy(want, NETWORK_POLICY_NAME)["spec"]["podSelector"]
    )

    applied = len(fake.applied)
    run_data_plane_auth(client)
    assert not [o for o in fake.applied[applied:] if o["kind"] == "NetworkPolicy"]


def _stale_policies(fake: FakeClusterClient, ns: str = DEFAULT_NS) -> None:
    """그 네임스페이스의 테넌트 정책에서 묶음의 판을 지워 이 판 전의 렌더처럼 둔다."""
    fake.objects[(NP_API, "NetworkPolicy", ns, NETWORK_POLICY_NAME)]["metadata"].pop("annotations")


def test_a_namespace_whose_policies_fail_backs_off(client, fake_clusters, monkeypatch) -> None:
    """묶음을 두지 못한 네임스페이스는 그 네임스페이스만 백오프로 미룬다. 설정이 맞아도 그 백오프는
    남고, 미뤄 둔 동안에는 다시 두지 않는다."""
    cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    _stale_policies(fake)
    tries: list[str] = []
    apply = fake.apply

    def refusing(obj, *args, **kw):
        if obj.get("kind") == "NetworkPolicy":
            tries.append(obj["metadata"]["name"])
            raise RuntimeError("networkpolicies is forbidden")
        return apply(obj, *args, **kw)

    monkeypatch.setattr(fake, "apply", refusing)
    run_data_plane_auth(client)
    state = client.portal.call(client.app.state.data_plane_auth.store.cluster, cid)
    assert state.namespace_retries[DEFAULT_NS].failures == 1
    assert "namespace policies not ensured" in (state.last_error or "")
    assert tries
    tries.clear()
    run_data_plane_auth(client)
    state = client.portal.call(client.app.state.data_plane_auth.store.cluster, cid)
    assert tries == [] and state.namespace_retries[DEFAULT_NS].failures == 1


def test_the_sweep_leaves_reserved_namespaces_alone(client, fake_clusters) -> None:
    """이 설치의 OIDC 설정이 예약된 네임스페이스에 있어도 그곳에 NetworkPolicy 를 두지 않는다."""
    _cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    install_id = client.portal.call(client.app.state.platform_settings.install_id)
    for config in oidc_config_manifests(
        "default", "kube-public", install_id, internal_issuer="http://issuer"
    ):
        fake.apply(config)
    run_data_plane_auth(client)
    assert fake.find("NetworkPolicy", "kube-public") == []


# -- 나가는 길 (T75) ------------------------------------------------------------------------


def _ip_allowed(policy: dict[str, Any], ip: str) -> bool:
    """정책의 egress ipBlock 가운데 ip 를 담고 except 로 빼지 않은 것이 있는가."""
    address = ipaddress.ip_address(ip)
    for rule in policy["spec"]["egress"]:
        for peer in rule["to"]:
            block = peer.get("ipBlock")
            if block is None or address.version != ipaddress.ip_network(block["cidr"]).version:
                continue
            excepted = any(address in ipaddress.ip_network(e) for e in block.get("except") or [])
            if address in ipaddress.ip_network(block["cidr"]) and not excepted:
                return True
    return False


def test_no_pod_whose_egress_is_restricted_gains_egress() -> None:
    """나가는 길을 좁힌 정책이 고르는 파드는 egress 정책이 고르지 않는다. 고르면 합집합이 그 파드의
    나가는 길을 연다."""
    policies = _namespace_render()
    guard = _policy(policies, EGRESS_POLICY_NAME)
    restricting = [p for p in policies if "Egress" in p["spec"]["policyTypes"] and p is not guard]
    restricted = {
        name
        for name, labels in _pods().items()
        if any(selector_matches(p["spec"]["podSelector"], labels) for p in restricting)
    }
    # 시험이 빈 집합을 보지 않는다
    assert restricted == {"sandbox", "probe", "fetch backend"}
    for name in restricted:
        assert not selector_matches(guard["spec"]["podSelector"], _pods()[name]), name


def test_the_egress_policy_selects_every_other_pod() -> None:
    guard = _policy(_namespace_render(), EGRESS_POLICY_NAME)
    for name, labels in _pods().items():
        if name in ("sandbox", "probe", "fetch backend"):
            continue
        assert selector_matches(guard["spec"]["podSelector"], labels), name


def test_the_egress_policy_closes_only_the_metadata_address() -> None:
    guard = _policy(
        namespace_policies("acme", NS, "daegam-system", egress=True), EGRESS_POLICY_NAME
    )
    assert guard["spec"]["policyTypes"] == ["Egress"]
    assert METADATA_EXCEPT == ("169.254.169.254/32", "fd00:ec2::254/128")
    for closed in ("169.254.169.254", "fd00:ec2::254"):
        assert not _ip_allowed(guard, closed), closed
    # NodeLocal DNS 같은 다른 링크로컬 주소, 노드·파드 주소, 공인 주소는 열려 있다
    for open_ in ("169.254.20.10", "169.254.169.253", "10.43.0.10", "192.168.1.5", "1.1.1.1"):
        assert _ip_allowed(guard, open_), open_
    assert _ip_allowed(guard, "2001:db8::1") and _ip_allowed(guard, "fd00:ec2::253")
    # 클러스터 안의 파드는 ipBlock 이 파드 주소에 맞는지와 상관없이 열린다
    assert {"namespaceSelector": {}} in guard["spec"]["egress"][0]["to"]
    assert "ports" not in guard["spec"]["egress"][0]


@pytest.mark.parametrize(
    ("switch", "controller", "wanted"),
    [
        (False, "kube-system/calico-node", False),
        (True, "kube-system/calico-node", True),
        (True, "k3s (embedded kube-router)", True),
        (True, "kube-system/antrea-agent", True),
        (True, "kube-system/kube-router", True),
        (True, "kube-system/cilium", False),
        (True, "DAEGAM_HOME_NETWORK_POLICY=enforced", False),
        (True, None, False),
    ],
)
def test_the_egress_policy_needs_the_switch_and_a_known_controller(
    switch, controller, wanted
) -> None:
    """스위치를 켜고 컨트롤러가 확인된 것일 때만 둔다. 다른 컨트롤러(Cilium, GKE 의 anetd)나 모르는
    컨트롤러에서는 이 정책이 파드의 API 서버 접속까지 막을 수 있어 두지 않고 경고한다."""
    cluster = cluster_doc(block_metadata=switch)
    if controller is not None:
        cluster.status.preflight = Preflight(ok=True, policy_controller=controller)
    assert egress_policy_wanted(cluster) is wanted
    warnings = metadata_block_warnings(cluster)
    if switch and not wanted:
        assert len(warnings) == 1 and warnings[0].startswith(f"{METADATA_BLOCK_UNSUPPORTED}: ")
    else:
        assert warnings == []


def test_a_cluster_without_the_egress_policy_loses_one_it_had() -> None:
    fake = FakeClusterClient()
    ensure_namespace_policies(fake, "acme", NS, "daegam-system", egress=True)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, NS) is not None
    assert not ensure_namespace_policies(fake, "acme", NS, "daegam-system")
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, NS) is None
    without = namespace_policies("acme", NS, "daegam-system")
    assert EGRESS_POLICY_NAME not in [p["metadata"]["name"] for p in without]
    # 판이 달라 스윕이 그 클러스터의 네임스페이스에 다시 둔다
    assert _stamps(fake) == {NS: namespace_policies_stamp(without)}
    assert namespace_policies_stamp(without) != namespace_policies_stamp(
        namespace_policies("acme", NS, "daegam-system", egress=True)
    )


def _admin_egress_policy(ns: str) -> dict[str, Any]:
    """관리자가 둔 egress 정책. 플랫폼의 라벨이 없다."""
    return {
        "apiVersion": NP_API,
        "kind": "NetworkPolicy",
        "metadata": {"name": "lock-egress", "namespace": ns},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": []},
    }


def test_a_namespace_with_an_admin_egress_policy_gets_none(caplog) -> None:
    """NetworkPolicy 는 허용을 더하기만 하므로, 메타데이터 주소만 빼고 다 여는 정책이 관리자의
    egress 제한을 지운다. 그런 네임스페이스에는 두지 않고, 앞서 둔 것은 지우고, 로그로 알린다.
    플랫폼이 둔 egress 정책(샌드박스 격리, 웹 fetch)은 관리자의 것으로 세지 않는다."""
    fake = FakeClusterClient()
    ensure_namespace_policies(fake, "acme", NS, "daegam-system", egress=True)
    fake.apply(next(d for d in _fetch_docs() if d["kind"] == "NetworkPolicy"))
    assert foreign_egress_policies(fake.find("NetworkPolicy", NS)) == []
    fake.apply(_admin_egress_policy(NS))
    assert foreign_egress_policies(fake.find("NetworkPolicy", NS)) == ["lock-egress"]
    assert not ensure_namespace_policies(fake, "acme", NS, "daegam-system", egress=True)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, NS) is None
    assert any("lock-egress" in m and NS in m for m in caplog.messages)


def _calico(client, fake: FakeClusterClient, cid: str) -> None:
    """그 클러스터가 Calico 를 쓰는 것으로 다시 점검한다."""
    fake.objects[("apps/v1", "DaemonSet", "kube-system", "calico-node")] = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "calico-node", "namespace": "kube-system"},
    }
    assert client.post(f"/clusters/{cid}/preflight").status_code == 200


def test_the_switch_puts_the_egress_policy_in_place_on_the_next_tick(client, fake_clusters):
    cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    _calico(client, fake, cid)
    r = client.patch(f"/clusters/{cid}", json={"block_metadata": True})
    assert r.status_code == 200 and r.json()["block_metadata"] is True
    assert r.json()["warnings"] == []
    run_data_plane_auth(client)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, DEFAULT_NS) is not None
    # 관리자가 egress 정책을 두면 다음 틱이 그 네임스페이스에서 거두고 Cluster 경고에 이름을 싣는다
    fake.apply(_admin_egress_policy(DEFAULT_NS))
    run_data_plane_auth(client)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, DEFAULT_NS) is None
    cluster = client.get(f"/clusters/{cid}").json()
    assert cluster["status"]["metadata_skipped"] == [DEFAULT_NS]
    skipped = [w for w in cluster["warnings"] if w.startswith(f"{METADATA_BLOCK_SKIPPED}: ")]
    assert len(skipped) == 1 and skipped[0].endswith(f": {DEFAULT_NS}")
    fake.delete(NP_API, "NetworkPolicy", "lock-egress", DEFAULT_NS)
    run_data_plane_auth(client)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, DEFAULT_NS) is not None
    cluster = client.get(f"/clusters/{cid}").json()
    assert cluster["status"]["metadata_skipped"] == [] and cluster["warnings"] == []
    # 끄면 다음 틱이 지운다
    assert client.patch(f"/clusters/{cid}", json={"block_metadata": False}).status_code == 200
    run_data_plane_auth(client)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, DEFAULT_NS) is None


def test_a_create_without_the_switch_leaves_it_off(client) -> None:
    """화면의 등록은 스위치를 켤 때만 보낸다. 보내지 않으면 꺼져 있다 (T75)."""
    assert post_cluster(client, "edge").json()["block_metadata"] is False
    assert post_cluster(client, "edge2", block_metadata=True).json()["block_metadata"] is True


def test_an_unknown_controller_gets_no_egress_policy_and_a_warning(client, fake_clusters):
    """FakeClusterClient 에는 정책 컨트롤러가 없다. 스위치를 켜도 정책을 두지 않고 까닭을
    경고한다."""
    cid, _bp, _ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    r = client.patch(f"/clusters/{cid}", json={"block_metadata": True})
    assert r.status_code == 200
    assert [w for w in r.json()["warnings"] if w.startswith(METADATA_BLOCK_UNSUPPORTED)]
    run_data_plane_auth(client)
    assert fake.get(NP_API, "NetworkPolicy", EGRESS_POLICY_NAME, DEFAULT_NS) is None


def test_a_failed_controller_scan_keeps_the_controller_it_saw(client, fake_clusters, monkeypatch):
    """워크로드 목록을 읽지 못한 점검은 컨트롤러를 None 으로 적지 않는다. 한 번의 실패로 egress
    정책이 지워지지 않게 하려는 것이다."""
    cid, _bp, _ds, _ms = mcp_server_env(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    _calico(client, fake, cid)
    listing = fake.list

    def no_workloads(api_version, kind, *args, **kw):
        if kind in ("DaemonSet", "Deployment"):
            raise RuntimeError("daemonsets is forbidden")
        return listing(api_version, kind, *args, **kw)

    monkeypatch.setattr(fake, "list", no_workloads)
    r = client.post(f"/clusters/{cid}/preflight")
    assert r.status_code == 200
    preflight = r.json()["preflight"]
    assert preflight["policy_controller"] == "kube-system/calico-node"
    assert preflight["network_policy"] == "enforced"


def test_the_home_switch_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_HOME_BLOCK_METADATA", "true")
    assert settings_from_env().home_block_metadata is True
    monkeypatch.setenv("DAEGAM_HOME_BLOCK_METADATA", "")
    assert settings_from_env().home_block_metadata is None
    monkeypatch.setenv("DAEGAM_HOME_BLOCK_METADATA", "yes")
    with pytest.raises(ValueError):
        settings_from_env()
    assert home_env_fields("http://p", block_metadata=True)["block_metadata"] is True
    assert "block_metadata" not in home_env_fields("http://p")
