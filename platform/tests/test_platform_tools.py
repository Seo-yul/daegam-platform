"""플랫폼 웹 Tool 의 허브 항목과 Gateway (P39b 13절).

두 항목은 주인 테넌트도 클러스터도 없다. 켜면 생기고, 모든 테넌트가 보고 Gateway 에 넣는다. manage
경계는 비어 platform-admin 만 관리한다. fetch 는 Gateway 의 네임스페이스에 비밀 없는 복제본이 서고,
그 토큰은 fetch audience 하나뿐이다. search 는 플랫폼 주소를 가리키고 search audience 토큰만 받는다.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from daegam import urlfetch
from daegam.auth.signer import fetch_audience, search_audience
from daegam.errors import ServiceError
from daegam.k8s.labels import LABEL_MCP_BACKEND
from daegam.k8s.templates.svc_token import entry_token_objects, existing_svc_token
from daegam.k8s.templates.web_tools import (
    CA_ENV,
    FETCH_LABEL,
    FETCH_POLICY_NAME,
    GLOBAL_V6_BLOCK,
    WEB_CONFIG_ANNOTATION,
    render_fetch_clone,
)
from daegam.services.platform_tools import FETCH_CLONE_NAME, _item, check_backends
from daegam.store.hub import HubItem, HubK8s
from daegam.store.settings import PlatformSettings, WebFetchSettings
from daegam.urls import WEB_SEARCH_MCP_PATH
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB
from tests.helpers.gateways import create_gateway, gateway_up
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env
from tests.helpers.svc_tokens import aud_of


def _turn_on(client, **web) -> dict[str, dict]:
    body = {
        "fetch": {"enabled": True, **web.pop("fetch", {})},
        "search": {"enabled": True, "provider": "stub", **web.pop("search", {})},
        **web,
    }
    r = client.patch("/settings", json={"web_tools": body})
    assert r.status_code == 200, r.text
    return {i["name"]: i for i in client.get(HUB).json() if i["kind"] == "platform"}


def _secret_token(fake, name: str) -> str:
    token = existing_svc_token(fake, name, DEFAULT_NS)
    assert token
    return token


def test_turning_the_tools_on_creates_two_ownerless_items(client):
    assert [i for i in client.get(HUB).json() if i["kind"] == "platform"] == []
    items = _turn_on(client)
    assert set(items) == {"web-fetch", "web-search"}
    search = items["web-search"]
    assert search["tenant"] == "" and search["cluster_id"] == ""
    assert search["source"]["url"].endswith(WEB_SEARCH_MCP_PATH)
    assert items["web-fetch"]["tool_names"] == ["web_fetch"]
    assert search["tool_names"] == ["web_search"]
    assert search["access"]["consume"]["tenants"] == ["*"]
    assert search["access"]["manage"] == {"tenants": [], "roles": [], "groups": []}
    tools = client.get(f"{HUB}/{items['web-fetch']['id']}/tools").json()
    assert tools["listed"] and tools["tools"][0]["input_schema"]["required"] == ["url"]


def test_platform_items_cannot_be_deleted_or_refreshed(client):
    items = _turn_on(client)
    fetch = items["web-fetch"]["id"]
    assert detail_code(client.delete(f"{HUB}/{fetch}")) == (422, "platform_item")
    assert detail_code(client.post(f"{HUB}/{fetch}/refresh-tools")) == (422, "platform_item")


def test_a_gateway_takes_both_tools_beside_a_cluster_item(client, fake_clusters):
    _cid, hub_items, *_rest, fake = hub_env(client, fake_clusters)
    items = _turn_on(client)
    backends = [
        {"hub_item_id": hub_items["calculator"]["id"], "tools": ["Add"]},
        {"hub_item_id": items["web-search"]["id"]},
        {"hub_item_id": items["web-fetch"]["id"]},
    ]
    gateway = gateway_up(client, hub_items, name="web", backends=backends)
    snaps = {b["hub_item_name"]: b for b in gateway["deployed_backends"]}
    assert snaps["web-search"]["url"].endswith(WEB_SEARCH_MCP_PATH)
    assert snaps["web-fetch"]["clone_name"] == FETCH_CLONE_NAME
    # 복제본: 비밀 없는 우리 이미지, fetch audience 만 받는 검증, 나가는 길만 좁히는 정책
    clone = fake.get(GV, "MCPServer", FETCH_CLONE_NAME, DEFAULT_NS)
    spec = clone["spec"]
    assert spec["image"].startswith("daegam-web-tools:")
    assert spec["oidcConfigRef"]["audience"] == fetch_audience("default")
    pod = spec["podTemplateSpec"]
    # 백엔드 라벨은 백엔드 정책이 고른다 (T67)
    assert pod["metadata"]["labels"] == {FETCH_LABEL: "fetch", LABEL_MCP_BACKEND: FETCH_CLONE_NAME}
    assert WEB_CONFIG_ANNOTATION in pod["metadata"]["annotations"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert not any("secretKeyRef" in str(e) for e in spec.get("env", []))
    policy = fake.get("networking.k8s.io/v1", "NetworkPolicy", FETCH_POLICY_NAME, DEFAULT_NS)
    assert policy["spec"]["policyTypes"] == ["Egress"]
    blocks = [
        to["ipBlock"] for rule in policy["spec"]["egress"] for to in rule["to"] if "ipBlock" in to
    ]
    assert "169.254.0.0/16" in blocks[0]["except"] and blocks[1]["cidr"] == "2000::/3"
    # Entry 의 토큰: fetch 는 fetch audience 하나, search 는 search audience 하나
    objects = entry_token_objects(
        gateway["k8s"]["name"],
        [type("B", (), b) for b in gateway["deployed_backends"]],
    )
    by_name = {b["hub_item_name"]: b["workload"] for b in gateway["deployed_backends"]}
    assert aud_of(_secret_token(fake, objects[by_name["web-fetch"]])) == fetch_audience("default")
    assert aud_of(_secret_token(fake, objects[by_name["web-search"]])) == search_audience("default")


def test_an_exposed_gateway_refuses_platform_tools_unless_allowed():
    """노출한 Gateway 의 백엔드(게시 항목 아래까지 따라 내려간 것)에 플랫폼 Tool 이 있으면 422 다.
    관리자가 바깥 클라이언트를 허용하면 들어간다. 배포·재배포·Basic 편집이 이 검사를 부른다."""
    search = _item("web-search", "http://issuer")
    calc = HubItem(
        tenant="default",
        name="calc",
        display_name="calc",
        kind="remote",
        cluster_id="c1",
        k8s=HubK8s(kind="MCPRemoteProxy", name="calc", namespace="ns"),
        tool_names=["Add"],
    )
    items = {calc.id: calc}

    def check(settings: PlatformSettings, reachable) -> None:
        asyncio.run(
            check_backends(
                items,
                exposed=True,
                settings=settings,
                reachable=reachable,
                names={calc.id: ["Add"]},
                error_cls=ServiceError,
            )
        )

    with pytest.raises(ServiceError) as ei:
        check(PlatformSettings(), [calc, search])
    assert ei.value.code == "platform_tool_not_exposable"
    allowed = PlatformSettings.model_validate({"web_tools": {"external_clients": True}})
    check(allowed, [calc, search])
    check(PlatformSettings(), [calc])


def test_web_tools_behind_a_published_item_need_every_backend_name():
    """게시 항목 아래에서 닿는 플랫폼 Tool 도 웹 백엔드다. 그 Gateway 는 모든 백엔드의 이름을 알아야
    한다."""
    search = _item("web-search", "http://issuer")
    pub = _item("web-fetch", "http://issuer").model_copy(update={"kind": "published"})
    with pytest.raises(ServiceError) as ei:
        asyncio.run(
            check_backends(
                {pub.id: pub},
                exposed=False,
                settings=PlatformSettings(),
                reachable=[pub, search],
                names={},
                error_cls=ServiceError,
            )
        )
    assert (ei.value.code, ei.value.extra["items"]) == ("web_gateway_unknown_names", ["web-fetch"])


def test_web_backends_need_every_backend_name(client, fake_clusters):
    _cid, hub_items, _calc, _orders, remote, _fake = hub_env(client, fake_clusters)
    items = _turn_on(client)
    backends = [{"hub_item_id": remote["id"]}, {"hub_item_id": items["web-fetch"]["id"]}]
    r = create_gateway(client, hub_items, name="web", backends=backends)
    assert detail_code(r) == (422, "web_gateway_unknown_names")


def test_turning_fetch_off_removes_its_clones(client, fake_clusters, mongo):
    _cid, hub_items, *_rest, fake = hub_env(client, fake_clusters)
    items = _turn_on(client)
    backends = [{"hub_item_id": hub_items["calculator"]["id"], "tools": ["Add"]}]
    backends.append({"hub_item_id": items["web-fetch"]["id"]})
    gateway = gateway_up(client, hub_items, name="web", backends=backends)
    assert fake.get(GV, "MCPServer", FETCH_CLONE_NAME, DEFAULT_NS) is not None
    r = client.patch("/settings", json={"web_tools": {"fetch": {"enabled": False}}})
    assert r.status_code == 200
    swept = client.portal.call(client.app.state.platform_tools.sweep)
    assert swept["deleted"] == [f"{DEFAULT_NS}/{FETCH_CLONE_NAME}"]
    assert fake.get(GV, "MCPServer", FETCH_CLONE_NAME, DEFAULT_NS) is None
    assert fake.get("networking.k8s.io/v1", "NetworkPolicy", FETCH_POLICY_NAME, DEFAULT_NS) is None
    redeploy = client.post(f"{GW}/{gateway['id']}/redeploy")
    assert detail_code(redeploy) == (409, "backends_not_ready")
    fetch = client.portal.call(client.app.state.hub_items.get_platform, "web-fetch")
    clones = client.app.state.shared_clones
    with pytest.raises(ServiceError) as ei:
        client.portal.call(
            lambda: clones.apply(
                fetch,
                fake,
                cluster_id=gateway["cluster_id"],
                namespace=DEFAULT_NS,
                tenant="default",
                error_cls=ServiceError,
            )
        )
    assert (ei.value.status, ei.value.code) == (409, "platform_tool_disabled")


def test_turning_fetch_on_again_restores_the_clones(client, fake_clusters):
    """끈 동안 지운 복제본은 다시 켜면 리컨실 단계가 그것을 쓰는 배포된 Gateway 마다 되살린다."""
    _cid, hub_items, *_rest, fake = hub_env(client, fake_clusters)
    items = _turn_on(client)
    backends = [{"hub_item_id": items["web-fetch"]["id"]}]
    gateway_up(client, hub_items, name="web", backends=backends)
    sweep = client.app.state.platform_tools.sweep
    client.patch("/settings", json={"web_tools": {"fetch": {"enabled": False}}})
    assert client.portal.call(sweep)["deleted"] == [f"{DEFAULT_NS}/{FETCH_CLONE_NAME}"]
    client.patch("/settings", json={"web_tools": {"fetch": {"enabled": True}}})
    assert client.portal.call(sweep) == {"restored": [f"{DEFAULT_NS}/{FETCH_CLONE_NAME}"]}
    assert fake.get(GV, "MCPServer", FETCH_CLONE_NAME, DEFAULT_NS) is not None
    assert client.portal.call(sweep) == {}


def test_an_exposed_gateway_warns_once_external_clients_are_off(client, fake_clusters, mongo):
    """바깥 클라이언트 허용을 끈 뒤에도 노출한 Gateway 는 돈다. 상태가 `platform_tool_exposed` 로
    다음 재배포가 거절된다고 알린다."""
    _cid, hub_items, *_rest = hub_env(client, fake_clusters)
    items = _turn_on(client)
    gateway = gateway_up(
        client, hub_items, name="web", backends=[{"hub_item_id": items["web-fetch"]["id"]}]
    )
    mongo["gateways"].update_one(
        {"_id": gateway["id"]},
        {"$set": {"exposure.enabled": True, "exposure.host": "web.example.com"}},
    )

    def warned() -> bool:
        warnings = client.get(f"{GW}/{gateway['id']}").json()["status"]["warnings"]
        return any(w.startswith("platform_tool_exposed:") for w in warnings)

    assert warned()
    client.patch("/settings", json={"web_tools": {"external_clients": True}})
    assert not warned()


def test_a_tenant_item_named_like_a_platform_item_cannot_sit_beside_it(
    client, fake_clusters, mongo
):
    """Entry 이름이 `<gateway>-<항목 이름>` 이라 테넌트의 `web-fetch` 와 플랫폼 web-fetch 는 한
    Gateway 에 함께 둘 수 없다."""
    _cid, hub_items, *_rest = hub_env(client, fake_clusters)
    items = _turn_on(client)
    calc = hub_items["calculator"]
    mongo["hub_items"].update_one({"_id": calc["id"]}, {"$set": {"name": "web-fetch"}})
    backends = [{"hub_item_id": calc["id"]}, {"hub_item_id": items["web-fetch"]["id"]}]
    r = create_gateway(client, hub_items, name="web", backends=backends)
    assert detail_code(r) == (422, "duplicate_backend_name")
    # Entry 이름은 DNS 이름으로 고치고 63자에서 자른 뒤에 견준다. `web_fetch` 도, 긴 Gateway 이름
    # 뒤에서 잘려 같아지는 두 이름도 겹친다
    mongo["hub_items"].update_one({"_id": calc["id"]}, {"$set": {"name": "web_fetch"}})
    r = create_gateway(client, hub_items, name="web", backends=backends)
    assert detail_code(r) == (422, "duplicate_backend_name")
    assert r.json()["detail"]["names"] == ["web-fetch", "web_fetch"]
    orders = hub_items["orders"]
    mongo["hub_items"].update_one({"_id": calc["id"]}, {"$set": {"name": "x" * 58 + "-a"}})
    mongo["hub_items"].update_one({"_id": orders["id"]}, {"$set": {"name": "x" * 58 + "-b"}})
    pair = [{"hub_item_id": calc["id"]}, {"hub_item_id": orders["id"]}]
    assert create_gateway(client, hub_items, name="w", backends=pair).status_code == 201
    r = create_gateway(client, hub_items, name="long", backends=pair)
    assert detail_code(r) == (422, "duplicate_backend_name")


def test_the_egress_v6_block_is_the_fetch_rule() -> None:
    """NetworkPolicy 가 여는 IPv6 블록은 urlfetch 가 공개로 보는 범위와 같다."""
    assert GLOBAL_V6_BLOCK == str(urlfetch.GLOBAL_V6)


def test_the_fetch_clone_takes_the_ca_as_env_and_writes_no_configmap() -> None:
    """관리자가 준 CA 는 env 로 간다. 플랫폼은 테넌트 네임스페이스에 ConfigMap 을 쓸 권한이 없다."""
    docs = render_fetch_clone(
        tenant="t",
        item_id="hi",
        name=FETCH_CLONE_NAME,
        namespace="ns",
        group="g",
        image="daegam-web-tools:0.1.0",
        fetch=WebFetchSettings(),
        ca_bundle="-----BEGIN CERTIFICATE-----",
        effective=None,
        blocked=[],
    )
    assert [d["kind"] for d in docs] == ["NetworkPolicy", "MCPServer"]
    env = {e["name"]: e["value"] for e in docs[1]["spec"]["env"]}
    assert base64.b64decode(env[CA_ENV]).decode() == "-----BEGIN CERTIFICATE-----"
