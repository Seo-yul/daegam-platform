"""D21: 제어면이 데이터 플레인 Service 에 닿는 경로. home 은 svc DNS 직행, 원격은 API 서버
services/proxy. 허브 tools/list 와 에이전트 chat 프록시가 이 경로를 쓴다."""

from __future__ import annotations

import asyncio

import pytest
from kubernetes import client as k8s

from daegam.k8s import client as kclient
from daegam.k8s.client import (
    ClusterClient,
    ClusterUnreachable,
    ServiceRoute,
    ServiceTimeout,
    apiserver_proxy_route,
    parse_service_url,
)
from daegam.k8s.preflight import REQUIRED_VERBS
from daegam.services import hub as hub_module
from tests.helpers.chart import render_template
from tests.helpers.docs import cluster_doc
from tests.helpers.fake_cluster import FakeClusterClient

# -- URL 해석 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "want"),
    [
        (
            "http://mcp-calc-proxy.daegam-acme.svc.cluster.local:8080/mcp",
            ("daegam-acme", "mcp-calc-proxy", 8080, "/mcp"),
        ),
        (
            "http://agent-x-runtime.daegam-acme.svc:8080/chat",
            ("daegam-acme", "agent-x-runtime", 8080, "/chat"),
        ),
        ("http://vmcp-a.ns.svc/mcp?x=1", ("ns", "vmcp-a", 80, "/mcp?x=1")),
        ("https://svc.ns.svc", ("ns", "svc", 443, "/")),
        ("https://mcp.example.com/mcp", None),
        ("http://10.0.0.5:8080/mcp", None),
        ("http://svc.ns.svc.other.local:8080/mcp", None),
        ("http://localhost:8080/mcp", None),
    ],
)
def test_parse_service_url(url, want):
    assert parse_service_url(url) == want


# -- API 서버 프록시 경로 ----------------------------------------------------------------------


def test_apiserver_route_with_bearer_token_and_ca():
    cfg = k8s.Configuration()
    cfg.host = "https://remote.example:6443/"
    cfg.api_key = {"authorization": "tok"}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    cfg.ssl_ca_cert = "/tmp/ca.crt"
    route = apiserver_proxy_route(cfg, "daegam-acme", "mcp-calc-proxy", 8080)
    assert route.via == "apiserver"
    assert route.base_url == (
        "https://remote.example:6443/api/v1/namespaces/daegam-acme/services/"
        "http:mcp-calc-proxy:8080/proxy"
    )
    assert route.url("/mcp") == route.base_url + "/mcp"
    assert route.url("") == route.base_url
    assert route.headers == {"Authorization": "Bearer tok"} and route.verify == "/tmp/ca.crt"
    assert route.cert is None


def test_apiserver_route_with_client_cert_and_insecure():
    cfg = k8s.Configuration()
    cfg.host = "https://127.0.0.1:26443"
    cfg.cert_file, cfg.key_file = "/tmp/c.crt", "/tmp/c.key"
    cfg.verify_ssl = False
    route = apiserver_proxy_route(cfg, "ns", "svc", 4483, scheme="https")
    assert route.headers == {} and route.verify is False
    assert route.cert == ("/tmp/c.crt", "/tmp/c.key")
    assert route.base_url.endswith("/services/https:svc:4483/proxy")


def test_apiserver_route_calls_refresh_hook():
    cfg = k8s.Configuration()
    cfg.host = "https://h"
    seen = []

    def hook(c):
        seen.append(c)
        c.api_key = {"authorization": "Bearer fresh"}

    cfg.refresh_api_key_hook = hook
    route = apiserver_proxy_route(cfg, "ns", "svc", 80)
    assert seen == [cfg] and route.headers["Authorization"] == "Bearer fresh"


def test_cluster_client_picks_route_by_mode(monkeypatch):
    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    cfg = k8s.Configuration()
    cfg.host = "https://remote:6443"
    cfg.api_key = {"authorization": "Bearer t"}
    api = k8s.ApiClient(cfg)
    home = ClusterClient(api, name="home", direct_services=True)
    r = home.service_route("daegam-acme", "agent-x-runtime", 8080)
    assert r.via == "direct" and r.headers == {}
    assert r.base_url == "http://agent-x-runtime.daegam-acme.svc.cluster.local:8080"
    remote = ClusterClient(api, name="edge", direct_services=False)
    r = remote.service_route("daegam-acme", "agent-x-runtime", 8080)
    assert r.via == "apiserver" and r.headers == {"Authorization": "Bearer t"}
    assert r.base_url == (
        "https://remote:6443/api/v1/namespaces/daegam-acme/services/http:agent-x-runtime:8080/proxy"
    )


def test_from_cluster_mode_to_direct(monkeypatch):
    """in_cluster 만 직행. kubecontext(로컬 개발)·kubeconfig·token 은 API 서버 프록시."""
    made = []

    class _Api:
        configuration = k8s.Configuration()

    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    monkeypatch.setattr(kclient.k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(kclient.k8s, "ApiClient", lambda *a, **k: _Api())
    monkeypatch.setattr(
        kclient.k8s_config, "new_client_from_config", lambda **k: made.append(k) or _Api()
    )
    monkeypatch.setattr(kclient.k8s_config, "new_client_from_config_dict", lambda cfg, **k: _Api())

    class _Secrets:
        def get(self, name, namespace=None):
            return {
                "kubeconfig": "apiVersion: v1\nkind: Config\n",
                "token": "t",
                "ca_bundle": "-----BEGIN CERTIFICATE-----\nx\n",
            }

    assert ClusterClient.from_cluster(cluster_doc(), None).direct_services is True
    cc = ClusterClient.from_cluster(cluster_doc(mode="kubecontext", kubecontext="orb"), None)
    assert cc.direct_services is False
    cc = ClusterClient.from_cluster(cluster_doc(mode="kubeconfig", secret_name="s"), _Secrets())
    assert cc.direct_services is False
    token = cluster_doc(mode="token", secret_name="s", api_url="https://r:6443")
    assert ClusterClient.from_cluster(token, _Secrets()).direct_services is False


def test_can_i_splits_subresource(monkeypatch):
    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    bodies = []

    class _Authz:
        def __init__(self, api):
            pass

        def create_self_subject_access_review(self, body):
            bodies.append(body.spec.resource_attributes)
            return k8s.V1SelfSubjectAccessReview(
                spec=body.spec, status=k8s.V1SubjectAccessReviewStatus(allowed=True)
            )

    monkeypatch.setattr(kclient.k8s, "AuthorizationV1Api", _Authz)
    cc = ClusterClient(k8s.ApiClient(k8s.Configuration()))
    assert cc.can_i("create", "services/proxy") is True
    assert cc.can_i("get", "services") is True
    assert (bodies[0].resource, bodies[0].subresource) == ("services", "proxy")
    assert (bodies[1].resource, bodies[1].subresource) == ("services", None)


# -- 요청 전송 ----------------------------------------------------------------------------------


def test_send_service_request_maps_errors(monkeypatch):
    import httpx2

    class _Client:
        def __init__(self, exc):
            self.exc = exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            raise self.exc

    route = ServiceRoute(base_url="http://svc.ns.svc.cluster.local:80", via="direct")
    monkeypatch.setattr(
        ServiceRoute, "http_client", lambda self, **k: _Client(httpx2.ReadTimeout("slow"))
    )
    with pytest.raises(ServiceTimeout):
        asyncio.run(kclient.send_service_request(route, "POST", "/chat", json={}))
    monkeypatch.setattr(
        ServiceRoute, "http_client", lambda self, **k: _Client(httpx2.ConnectError("refused"))
    )
    with pytest.raises(ClusterUnreachable) as ei:
        asyncio.run(kclient.send_service_request(route, "POST", "/chat", json={}))
    assert not isinstance(ei.value, ServiceTimeout)


def test_fake_service_request_records_and_uses_handler():
    fake = FakeClusterClient()
    r = asyncio.run(fake.service_request("POST", "ns", "svc", 8080, "/chat", json={"m": 1}))
    assert r.status == 502 and "no data plane" in r.json()["detail"]
    fake.service_handler = lambda method, ns, svc, port, path, body: (
        200,
        {"echo": body, "path": path},
        {"X-Test": "1"},
    )
    r = asyncio.run(fake.service_request("POST", "ns", "svc", 8080, "/chat", json={"m": 2}))
    assert r.status == 200 and r.json() == {"echo": {"m": 2}, "path": "/chat"}
    assert r.headers == {"X-Test": "1"}
    assert [c["json"] for c in fake.service_calls] == [{"m": 1}, {"m": 2}]
    assert fake.service_calls[0]["service"] == "svc" and fake.service_calls[0]["port"] == 8080
    assert fake.service_route("ns", "svc", 8080).base_url == "http://svc.ns.svc.cluster.local:8080"


# -- 허브 tools/list 가 경로를 탄다 -----------------------------------------------------------


def test_default_tool_lister_routes_service_urls(monkeypatch):
    calls = []

    async def fake_list_tools(url, *, timeout=None, http_client=None, headers=None):
        calls.append((url, http_client is not None))
        from daegam.mcp_client import ToolsResult

        return ToolsResult(server="s", protocol_version="p", tools=["Add"])

    import daegam.mcp_client

    monkeypatch.setattr(daegam.mcp_client, "list_tools", fake_list_tools)
    fake = FakeClusterClient()
    out = asyncio.run(
        hub_module._default_tool_lister(
            fake, "http://mcp-x-proxy.daegam-acme.svc.cluster.local:8080/mcp"
        )
    )
    assert out == ["Add"]
    # 외부 URL 은 그대로, 클라이언트 없이
    asyncio.run(hub_module._default_tool_lister(fake, "https://mcp.example.com/mcp"))
    assert calls == [
        ("http://mcp-x-proxy.daegam-acme.svc.cluster.local:8080/mcp", True),
        ("https://mcp.example.com/mcp", False),
    ]


# -- RBAC 매니페스트와 preflight 가 같은 권한을 말한다 ------------------------------------------


def _rules(docs: list[dict]) -> set[tuple[str, str, str]]:
    out = set()
    for doc in docs:
        if not doc or doc.get("kind") != "ClusterRole":
            continue
        for rule in doc["rules"]:
            for g in rule["apiGroups"]:
                for r in rule["resources"]:
                    for v in rule["verbs"]:
                        out.add((g, r, v))
    return out


@pytest.mark.parametrize("target", ["home", "remote"])
def test_rbac_manifests_cover_required_verbs(target):
    """home 과 원격 클러스터의 RBAC 는 대감 차트의 같은 규칙 목록이다 (P85 8절)."""
    granted = _rules(render_template("rbac.yaml", sets={"target": target}))
    # 이름 한정 항목(clusterroles bind daegam-tls-reader)은 (group, resource, verb) 로 본다.
    # resourceNames 까지 맞는지는 tests/test_namespaces.py 가 본다
    missing = [v for v in REQUIRED_VERBS if v[:3] not in granted]
    assert not missing, missing
    assert ("", "services/proxy", "get") in granted and ("", "services/proxy", "create") in granted
