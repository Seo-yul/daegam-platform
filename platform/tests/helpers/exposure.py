"""외부 노출을 쓰는 테스트가 함께 쓰는 도우미 (D22, D27).

`cluster_with_ingress` 는 Ingress 를 켠 클러스터 위에 tests/helpers/mcp_servers.py 의
`mcp_server_env` 를 깐다. fake 클러스터에는 실제 클러스터에 미리 있어야 하는 IngressClass 와
Secret 을 심는다(`ingress_class`, `cluster_secret`, `prep_ingress`).
"""

from __future__ import annotations

from tests.helpers.consts import DEFAULT_NS, GV, HUB, MS, NP_API
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import gateway_up
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

# 클러스터 등록 본문의 ingress. 무인증 노출을 허용하는 traefik 이다
INGRESS = {
    "enabled": True,
    "allow_unauthenticated": True,
    "class_name": "traefik",
    "domain": "mcp.example.com",
    "controller_namespace": "traefik",
    "reserved_hosts": ["daegam.com"],
    "annotations": {"nginx.ingress.kubernetes.io/proxy-buffering": "off"},
}


def cluster_with_ingress(client, **ingress):
    """ingress 가 `INGRESS` 를 덮은 클러스터의 `mcp_server_env`. (cid, bp, ds, mp) 다."""
    cid, bp, ds, mp = mcp_server_env(client, cluster_extra={"ingress": {**INGRESS, **ingress}})
    return cid, bp, ds, mp


def ingress_class(fake: FakeClusterClient, name: str) -> None:
    fake.apply({"apiVersion": NP_API, "kind": "IngressClass", "metadata": {"name": name}})


def cluster_secret(fake: FakeClusterClient, ns: str, name: str, *, tls: bool) -> None:
    # 실제 클러스터처럼 Secret 은 있는 네임스페이스 안에만 있다
    if fake.get("v1", "Namespace", ns) is None:
        fake.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}})
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": ns},
            "type": "kubernetes.io/tls" if tls else "Opaque",
            "data": {"tls.key": "PRIVATE-KEY-MATERIAL", "tls.crt": "CERT"},
        }
    )


def prep_ingress(fake: FakeClusterClient) -> None:
    """IngressClass traefik·nginx 와, 기본 네임스페이스의 TLS Secret site-tls 와 Opaque Secret
    opaque."""
    ingress_class(fake, "traefik")
    ingress_class(fake, "nginx")
    cluster_secret(fake, DEFAULT_NS, "site-tls", tls=True)
    cluster_secret(fake, DEFAULT_NS, "opaque", tls=False)


def exposed_server(
    client, fake_clusters, name: str = "calc-1"
) -> tuple[dict, str, FakeClusterClient]:
    """Ingress 를 고를 수 있는 클러스터에 노출해 배포한 MCP Server 하나. (문서, 클러스터 id,
    가짜)."""
    cid, _bp, _ds, mp = cluster_with_ingress(client)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    r = clone_and_deploy(
        client, mp, name=name, exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert r.status_code == 201, r.text
    return r.json(), cid, fake


def front_and_gateway(client, fake_clusters) -> tuple[dict, dict, object]:
    """노출한 MCP Server(앞단) 하나와 그것을 백엔드로 쓰는 Gateway 하나. 둘 다 소비자다."""
    front, _cid, fake = exposed_server(client, fake_clusters)
    url = f"http://mcp-{front['k8s']['name']}-proxy.{DEFAULT_NS}.svc.cluster.local:8080/mcp"
    fake.set_status(
        GV, "MCPServer", front["k8s"]["name"], DEFAULT_NS, {"phase": "Ready", "url": url}
    )
    assert client.post(f"{MS}/{front['id']}/publish", json={}).status_code == 201
    item = client.get(HUB).json()[0]
    gateway = gateway_up(client, None, name="a", backends=[{"hub_item_id": item["id"]}])
    return front, gateway, fake
