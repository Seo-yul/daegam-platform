"""플랫폼이 entry 항목의 URL 을 직접 부를 때의 주소 규칙 (`services/entry_address.py`).

Tool 새로고침, Tools 탭, Gateway 배포의 도달 확인이 같은 규칙을 지난다. 공인 주소, 그 테넌트
네임스페이스의 Service, 관리자가 연 사설망만 부르고, 거절된 호출은 하지 않는다.
"""

from __future__ import annotations

import asyncio
import ipaddress

import httpx2
import pytest

from daegam.config import EntryPrivateAllow
from daegam.services import entry_address
from daegam.services import hub as hub_module
from daegam.services.entry_address import (
    SERVICE_NAMESPACE,
    CheckedURL,
    EntryAddresses,
    EntryAddressRefused,
    PinnedTransport,
)
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import HUB
from tests.helpers.gateways import create_gateway, deploy_gateway
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.oauth import PUBLIC_IP

PRIVATE_IP = "10.20.0.7"
OUTSIDE = "https://mcp.example.com/mcp"
OWN_SERVICE = "http://ext.daegam-default.svc.cluster.local:8080/mcp"
OTHER_SERVICE = "http://ext.daegam-other.svc:8080/mcp"
COMPANY = "http://mcp.corp.example:8080/mcp"
NO_ALLOW = EntryPrivateAllow()
ALLOW = EntryPrivateAllow(cidrs=(ipaddress.ip_network("10.20.0.0/16"),), suffixes=("lan.example",))


def _resolver(host: str) -> list[str]:
    """`corp.example` 과 `lan.example` 아래의 이름은 사설 주소로, 나머지는 공인 주소로 풀린다."""
    return [PRIVATE_IP] if host.endswith(("corp.example", "lan.example")) else [PUBLIC_IP]


async def _own_namespaces() -> list[str]:
    return ["daegam-default", "team-a"]


def _check(url: str, allow: EntryPrivateAllow = NO_ALLOW, resolver=_resolver) -> str:
    rules = EntryAddresses(allow)
    rules.resolver = resolver
    return asyncio.run(rules.check(url, _own_namespaces))


def _reason(url: str, allow: EntryPrivateAllow = NO_ALLOW, resolver=_resolver) -> str:
    with pytest.raises(EntryAddressRefused) as ei:
        _check(url, allow, resolver)
    assert (ei.value.status, ei.value.code) == (422, "entry_address_refused")
    assert ei.value.to_dict()["reason"] == ei.value.reason
    return ei.value.reason


def test_a_public_address_passes_with_the_addresses_it_was_checked_at():
    checked = _check(OUTSIDE)
    assert isinstance(checked, CheckedURL) and checked == OUTSIDE
    assert checked.addresses == (PUBLIC_IP,)


def test_a_service_passes_only_in_the_tenants_namespaces():
    """Service 주소는 이름을 풀지 않고 네임스페이스로 정한다. 길은 클러스터 클라이언트가 고르므로
    `CheckedURL` 이 아니다."""
    assert not isinstance(_check(OWN_SERVICE), CheckedURL)
    assert _check("http://ext.team-a.svc:8080/mcp") == "http://ext.team-a.svc:8080/mcp"
    assert _reason(OTHER_SERVICE) == SERVICE_NAMESPACE
    # 허용 목록은 다른 테넌트의 Service 를 열지 못한다
    assert _reason(OTHER_SERVICE, ALLOW) == SERVICE_NAMESPACE


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (COMPANY, "url_private"),
        ("http://10.20.0.7:8080/mcp", "url_private"),
        ("http://169.254.169.254/latest", "url_private"),
        ("http://localhost:8080/mcp", "url_private"),
        # Service 주소 꼴이 아닌 클러스터 안 이름이다
        ("http://ext.svc:8080/mcp", "url_private"),
        ("http://ext.daegam-other.svc.cluster.local.:8080/mcp", "url_private"),
        ("https://user:pw@mcp.example.com/mcp", "url_userinfo"),
        ("ftp://mcp.example.com/mcp", "url_scheme"),
    ],
)
def test_private_and_malformed_addresses_are_refused(url, reason):
    assert _reason(url) == reason


def test_the_allow_list_opens_private_addresses_by_cidr_and_by_suffix():
    assert _check(COMPANY, ALLOW).addresses == (PRIVATE_IP,)
    assert _check("http://10.20.0.7:8080/mcp", ALLOW) == "http://10.20.0.7:8080/mcp"
    only_suffix = EntryPrivateAllow(suffixes=("lan.example",))
    assert _check("http://mcp.lan.example/mcp", only_suffix).addresses == (PRIVATE_IP,)
    # 목록 밖의 사설 주소와 이름은 그대로 거절이다
    assert _reason("http://10.21.0.7:8080/mcp", ALLOW) == "url_private"
    assert _reason(COMPANY, only_suffix) == "url_private"
    assert _reason("http://notlan.example.internal/mcp", only_suffix) == "url_private"


@pytest.mark.parametrize(
    "url",
    [
        # 헤드리스 Service 의 파드와 파드 이름은 Service 주소 꼴이 아니다
        "http://pod-0.ext.daegam-other.svc.cluster.local:8080/mcp",
        "http://10-42-0-5.daegam-other.pod.cluster.local:8080/mcp",
        "http://pod-0.ext.daegam-other.svc:8080/mcp",
    ],
)
def test_the_allow_list_never_opens_in_cluster_names(url):
    """클러스터 안 이름을 덮는 접미사는 기동이 거절하지만, 목록에 들어 있더라도 Service 주소 꼴이
    아닌 클러스터 안 이름은 부르지 않는다."""
    wide = EntryPrivateAllow(
        cidrs=(ipaddress.ip_network("10.0.0.0/8"),), suffixes=("local", "svc", "cluster.local")
    )
    assert _reason(url, wide) == "url_private"


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_an_opened_suffix_does_not_open_loopback_or_link_local(address):
    """연 접미사 아래의 이름이 플랫폼 파드 자신이나 메타데이터 주소로 풀리면 거절이다. 그 주소를
    범위로 연 것은 받는다."""
    url = "http://mcp.lan.example/mcp"
    by_name = EntryPrivateAllow(suffixes=("lan.example",))
    assert _reason(url, by_name, resolver=lambda _host: [address]) == "url_private"
    by_range = EntryPrivateAllow(
        cidrs=(ipaddress.ip_network("127.0.0.0/8"),), suffixes=("lan.example",)
    )
    assert _check(url, by_range, resolver=lambda _host: ["127.0.0.1"]).addresses == ("127.0.0.1",)


# -- 못 박은 연결 ----------------------------------------------------------------------------


def test_the_pinned_transport_connects_to_the_checked_address_under_the_original_name():
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers["host"], request.extensions["sni_hostname"]))
        if request.url.host == "203.0.113.9":
            raise httpx2.ConnectError("refused", request=request)
        return httpx2.Response(200, text="ok")

    transport = PinnedTransport(
        "mcp.example.com", ["203.0.113.9", PUBLIC_IP], httpx2.MockTransport(handler)
    )

    async def run() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            assert (await client.post("https://mcp.example.com:8443/mcp")).text == "ok"
            # 다른 호스트로 가는 요청은 보내지 않는다
            with pytest.raises(httpx2.ConnectError):
                await client.get("https://other.example.com/mcp")

    asyncio.run(run())
    # 첫 주소가 연결되지 않아 다음 주소로 갔다. 이름은 Host 머리와 TLS 에만 남는다
    assert seen == [
        ("https://203.0.113.9:8443/mcp", "mcp.example.com:8443", "mcp.example.com"),
        (f"https://{PUBLIC_IP}:8443/mcp", "mcp.example.com:8443", "mcp.example.com"),
    ]


def test_the_default_lister_pins_a_checked_url(monkeypatch):
    calls: list[tuple] = []

    async def pinned(url, *, timeout, headers=None):
        calls.append((url, url.addresses, headers))
        return hub_module.ToolsResult(server=None, protocol_version=None, tools=["Ping"])

    monkeypatch.setattr(hub_module, "list_tools_pinned", pinned)
    url = CheckedURL.of(OUTSIDE, [PUBLIC_IP])
    names = asyncio.run(hub_module._default_tool_lister(object(), url, {"X-Key": "k"}))
    assert names == ["Ping"]
    assert calls == [(OUTSIDE, (PUBLIC_IP,), {"X-Key": "k"})]


def test_the_pinned_client_holds_the_headers_and_the_checked_addresses():
    async def run() -> None:
        url = CheckedURL.of(OUTSIDE, [PUBLIC_IP])
        async with entry_address.pinned_client(url, {"X-Key": "k"}) as client:
            transport = client._transport
            assert isinstance(transport, PinnedTransport)
            assert (transport.host, transport.addresses) == ("mcp.example.com", (PUBLIC_IP,))
            assert client.headers["x-key"] == "k" and client.follow_redirects is False

    asyncio.run(run())


# -- Tool 새로고침, Tools 탭, Gateway 배포 -----------------------------------------------------


def _entry(client, name: str, url: str) -> dict:
    r = client.post(
        HUB, json={"name": name, "kind": "entry", "url": url, "transport": "streamable-http"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _listers(client) -> list[str]:
    """두 조회기를 가짜로 바꾸고, 불린 URL 이 쌓이는 목록을 돌려준다."""
    calls: list[str] = []

    async def names(_client, url, headers=None):
        calls.append(url)
        return ["Ping"]

    async def schemas(_client, url, headers=None):
        calls.append(url)
        return hub_module.ToolsResult(server=None, protocol_version=None, tools=["Ping"])

    svc = client.app.state.hub_service
    svc.tool_lister, svc.schema_lister = names, schemas
    svc.entry_addresses.resolver = _resolver
    return calls


def test_refresh_tools_and_the_tools_tab_refuse_without_calling(client):
    default_cluster(client)
    calls = _listers(client)
    for name, url, reason in (
        ("company", COMPANY, "url_private"),
        ("neighbour", OTHER_SERVICE, SERVICE_NAMESPACE),
    ):
        item = _entry(client, name, url)
        r = client.post(f"{HUB}/{item['id']}/refresh-tools")
        assert detail_code(r) == (422, "entry_address_refused"), r.text
        assert r.json()["detail"]["reason"] == reason
        body = client.get(f"{HUB}/{item['id']}/tools").json()
        assert body["listed"] is False and body["tools"] == []
        assert body["reason"].startswith("entry_address_refused: ")
        assert body["reason"].endswith(f"({reason})")
    assert calls == []


def test_public_addresses_and_the_tenants_services_are_called(client):
    default_cluster(client)
    calls = _listers(client)
    for name, url in (("outside", OUTSIDE), ("own", OWN_SERVICE)):
        item = _entry(client, name, url)
        r = client.post(f"{HUB}/{item['id']}/refresh-tools")
        assert r.status_code == 200, r.text
        assert r.json()["tool_names"] == ["Ping"]
        assert client.get(f"{HUB}/{item['id']}/tools").json()["listed"] is True
    assert calls == [OUTSIDE, OUTSIDE, OWN_SERVICE, OWN_SERVICE]
    assert isinstance(calls[0], CheckedURL) and calls[0].addresses == (PUBLIC_IP,)
    assert not isinstance(calls[2], CheckedURL)


def test_a_listed_private_network_is_called(settings, mongo, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets, entry_private_allow=ALLOW)
    with api_client(app) as client:
        default_cluster(client)
        calls = _listers(client)
        item = _entry(client, "company", COMPANY)
        r = client.post(f"{HUB}/{item['id']}/refresh-tools")
        assert r.status_code == 200, r.text
        assert calls == [COMPANY] and calls[0].addresses == (PRIVATE_IP,)


def test_a_deploy_counts_a_refused_backend_ready_with_its_stored_tools(client):
    """거절된 URL 은 부르지 않는다. 백엔드는 저장된 Tool 이름으로 준비된 것으로 센다. 그래서 지금
    배포되는 Gateway 는 계속 배포된다."""
    default_cluster(client)
    calls = _listers(client)
    svc = client.app.state.hub_service
    item = _entry(client, "company", COMPANY)
    # 이름이 공인 주소로 풀리던 때에 읽어 둔 Tool 이름이다
    svc.entry_addresses.resolver = lambda host: [PUBLIC_IP]
    assert client.post(f"{HUB}/{item['id']}/refresh-tools").status_code == 200
    svc.entry_addresses.resolver = _resolver
    del calls[:]

    gw = create_gateway(client, {}, name="gw", backends=[{"hub_item_id": item["id"]}]).json()
    r = deploy_gateway(client, gw["id"])
    assert r.status_code == 201, r.text
    assert calls == []
    backend = r.json()["deployed_backends"][0]
    assert backend["url"] == COMPANY and backend["tool_names"] == ["Ping"]
