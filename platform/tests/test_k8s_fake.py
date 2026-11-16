import pytest

from daegam.k8s.client import AccessDenied
from daegam.k8s.labels import daegam_labels, ensure_labels, label_selector
from daegam.secrets import K8sSecretStore
from tests.helpers.fake_cluster import FakeClusterClient, _resource_name


def _cm(name, ns="ns", data=None):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": ns},
        "data": data or {},
    }


def test_apply_get_delete_list_roundtrip():
    fake = FakeClusterClient()
    r = fake.apply(_cm("a", data={"k": "1"}))
    assert r["metadata"]["generation"] == 1 and r["metadata"]["uid"]
    r2 = fake.apply(_cm("a", data={"k": "2"}))
    assert r2["metadata"]["generation"] == 2 and r2["metadata"]["uid"] == r["metadata"]["uid"]
    assert fake.get("v1", "ConfigMap", "a", "ns")["data"] == {"k": "2"}
    assert fake.get("v1", "ConfigMap", "a", "other") is None
    fake.apply(ensure_labels(_cm("b"), tenant="t1", resource="cm", id="x"))
    assert [o["metadata"]["name"] for o in fake.list("v1", "ConfigMap", "ns")] == ["a", "b"]
    assert [
        o["metadata"]["name"]
        for o in fake.list("v1", "ConfigMap", "ns", labels={"daegam.devops.ai.kr/tenant": "t1"})
    ] == ["b"]
    assert fake.list("v1", "ConfigMap", "ns", labels={"daegam.devops.ai.kr/tenant": "t2"}) == []
    assert fake.delete("v1", "ConfigMap", "a", "ns") is True
    assert fake.delete("v1", "ConfigMap", "a", "ns") is False
    assert fake.get("v1", "ConfigMap", "a", "ns") is None
    assert len(fake.applied) == 3


def test_dry_run_does_not_persist():
    fake = FakeClusterClient()
    r = fake.dry_run_apply(_cm("d"))
    assert r["metadata"]["name"] == "d"
    assert fake.get("v1", "ConfigMap", "d", "ns") is None


def test_status_is_server_owned_and_injectable():
    fake = FakeClusterClient()
    obj = {
        "apiVersion": "toolhive.stacklok.dev/v1beta1",
        "kind": "MCPServer",
        "metadata": {"name": "m", "namespace": "ns"},
        "spec": {"image": "x"},
    }
    fake.apply(obj)
    fake.set_status(
        "toolhive.stacklok.dev/v1beta1",
        "MCPServer",
        "m",
        "ns",
        {"phase": "Ready", "url": "http://m"},
    )
    assert (
        fake.get("toolhive.stacklok.dev/v1beta1", "MCPServer", "m", "ns")["status"]["phase"]
        == "Ready"
    )
    # 다시 apply 해도 status 는 살아 있다
    fake.apply({**obj, "spec": {"image": "y"}})
    got = fake.get("toolhive.stacklok.dev/v1beta1", "MCPServer", "m", "ns")
    assert got["status"]["phase"] == "Ready" and got["spec"]["image"] == "y"
    # 반환값은 복사본이라 외부 수정이 저장소에 닿지 않는다
    got["spec"]["image"] = "z"
    assert fake.get("toolhive.stacklok.dev/v1beta1", "MCPServer", "m", "ns")["spec"]["image"] == "y"


def test_labels_helper():
    labels = daegam_labels(tenant="acme", resource="mcp-instance", id="i1")
    assert labels == {
        "daegam.devops.ai.kr/tenant": "acme",
        "daegam.devops.ai.kr/resource": "mcp-instance",
        "daegam.devops.ai.kr/id": "i1",
        "app.kubernetes.io/managed-by": "daegam",
    }
    obj = ensure_labels(
        {"metadata": {"labels": {"keep": "me"}}}, tenant="acme", resource="r", id="i"
    )
    assert obj["metadata"]["labels"]["keep"] == "me"
    assert obj["metadata"]["labels"]["daegam.devops.ai.kr/id"] == "i"
    assert label_selector({"b": "2", "a": "1"}) == "a=1,b=2"


def test_k8s_secret_store_over_fake_client():
    fake = FakeClusterClient()
    ss = K8sSecretStore(fake)
    ss.put(
        "cluster-1",
        {"kubeconfig": "k: v\n"},
        namespace="daegam-system",
        labels={"daegam.devops.ai.kr/id": "1"},
    )
    raw = fake.get("v1", "Secret", "cluster-1", "daegam-system")
    assert raw["type"] == "Opaque"
    assert raw["data"]["kubeconfig"] == "azogdgo="  # base64
    assert raw["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "daegam"
    assert ss.get("cluster-1", namespace="daegam-system") == {"kubeconfig": "k: v\n"}
    assert ss.get("cluster-1", namespace="other") is None
    assert ss.delete("cluster-1", namespace="daegam-system") is True
    assert ss.get("cluster-1", namespace="daegam-system") is None


def test_can_i_and_discover_defaults():
    # 클러스터 전체 Secret 권한은 지금의 RBAC 파일처럼 기본이 없음이다 (P63)
    assert FakeClusterClient().can_i("create", "secrets") is False
    assert FakeClusterClient().can_i("create", "secrets", namespace="ns") is True
    fake = FakeClusterClient(denied={("", "secrets", "delete")}, cluster_wide_secrets=True)
    assert fake.can_i("create", "secrets") is True
    assert fake.can_i("delete", "secrets") is False
    fake = FakeClusterClient()
    assert "mcpservers" in fake.discover("toolhive.stacklok.dev/v1beta1")
    assert fake.discover("nope/v1") == []


def test_resource_names_follow_kubernetes_plurals():
    assert _resource_name("IngressClass") == "ingressclasses"
    assert _resource_name("Secret") == "secrets"
    assert _resource_name("NetworkPolicy") == "networkpolicies"
    assert _resource_name("MCPRemoteProxy") == "mcpremoteproxies"
    assert _resource_name("MCPServerEntry") == "mcpserverentries"
    # 모음 뒤의 y 는 그대로 s 만 붙는다
    assert _resource_name("Gateway") == "gateways"


def test_denied_get_and_list_raise_access_denied_by_real_resource_name():
    """denied 는 API 서버의 리소스 이름(복수형)으로 적는다. y 로 끝나는 kind 도 걸린다."""
    fake = FakeClusterClient(
        denied={
            ("networking.k8s.io", "networkpolicies", "get"),
            ("networking.k8s.io", "networkpolicies", "list"),
        }
    )
    with pytest.raises(AccessDenied) as e:
        fake.get("networking.k8s.io/v1", "NetworkPolicy", "p", "ns")
    assert e.value.missing == "networkpolicies:get@ns"
    with pytest.raises(AccessDenied) as e:
        fake.list_metadata("networking.k8s.io/v1", "NetworkPolicy", "ns")
    assert e.value.missing == "networkpolicies:list@ns"
    assert fake.get("v1", "Secret", "s", "ns") is None
