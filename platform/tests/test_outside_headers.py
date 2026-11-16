"""P86 6.1절: entry 항목의 고정 헤더. 만들 때 고르는 인증, 이름·값·URL 의 검사, 관리 Secret 과 그
정리, 값 바꾸기와 누가 바꾸는가, 앞 판이 읽는 꼴. A3: 키가 필요한 카탈로그 원격을 그 헤더로
가져오기."""

from __future__ import annotations

import pytest

from daegam.services.hub import refuse_newer
from daegam.services.hub_catalog import snapshot
from daegam.services.outside_headers import (
    MAX_VALUE_BYTES,
    VALUE_KEY,
    managed_labels,
    managed_name,
)
from daegam.store.hub import ITEM_KEYS, HubItem, unknown_keys
from tests.helpers.auth import as_principal
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import DEFAULT_NS, HUB, OTHER_NS
from tests.helpers.http import detail_code

OUTSIDE_URL = "https://mcp.example.com/mcp"
SECRET = "Bearer p86-test-secret-ab12"


class Lister:
    """허브의 Tool 목록 대역. 받은 헤더를 남기고 answer 대로 답한다."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.answer: Exception | None = None

    async def __call__(self, _client, url, headers=None):
        self.seen.append(dict(headers or {}))
        if self.answer is not None:
            raise self.answer
        return ["echo_header"]


def header_body(name: str = "outside", **auth: str) -> dict:
    return {
        "name": name,
        "kind": "entry",
        "url": OUTSIDE_URL,
        "transport": "streamable-http",
        "auth": {"kind": "header", "header_name": "Authorization", "header_value": SECRET, **auth},
    }


def managed(client, item_id: str) -> dict[str, str] | None:
    store = client.app.state.cluster_service.secret_store()
    return store.get(managed_name(item_id), namespace=client.app.state.settings.namespace)


def raw_item(mongo, item_id: str) -> dict:
    return mongo.hub_items.find_one({"_id": item_id})


@pytest.fixture
def cluster(client):
    return default_cluster(client)


@pytest.fixture(autouse=True)
def lister(client):
    """만들기와 값 바꾸기가 헤더를 실어 부르는 Tool 목록의 대역. 밖으로 나가지 않는다."""
    fake = Lister()
    client.app.state.hub_service.tool_lister = fake
    return fake


def test_a_header_item_keeps_its_value_in_a_managed_secret_only(client, cluster, mongo):
    r = client.post(HUB, json=header_body())
    assert r.status_code == 201, r.text
    item = r.json()
    assert SECRET not in r.text
    assert item["auth"]["kind"] == "header" and item["auth"]["header_name"] == "Authorization"
    assert item["auth"]["set"] is True and item["auth"]["created_by"]
    assert managed(client, item["id"]) == {VALUE_KEY: SECRET}
    store = client.app.state.cluster_service.secret_store()
    ns = client.app.state.settings.namespace
    assert store.labels[(ns, managed_name(item["id"]))] == managed_labels(item["id"])
    # Mongo 에는 값이 없다
    assert SECRET not in str(raw_item(mongo, item["id"]))
    detail = client.get(f"{HUB}/{item['id']}")
    assert detail.status_code == 200 and SECRET not in detail.text


def test_items_without_auth_store_no_auth_field(client, cluster, mongo):
    r = client.post(
        HUB, json={"name": "plain", "kind": "entry", "url": OUTSIDE_URL, "transport": "sse"}
    )
    assert r.status_code == 201, r.text
    assert "auth" not in raw_item(mongo, r.json()["id"])
    r = client.post(
        HUB,
        json={**header_body("plain2"), "auth": {"kind": "none"}},
    )
    assert r.status_code == 201, r.text
    assert "auth" not in raw_item(mongo, r.json()["id"])


def header(name: str, value: str) -> dict:
    return {"auth": {"kind": "header", "header_name": name, "header_value": value}}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"url": "http://mcp.example.com/mcp"}, "outside_auth_not_https"),
        (
            {"url": "http://mcp-x.daegam-other.svc.cluster.local:8080/mcp"},
            "outside_auth_not_https",
        ),
        ({"auth": {"kind": "oauth"}}, "outside_oauth_unavailable"),
        (header("Mcp-Session-Id", "p86-v"), "outside_header_reserved"),
        (header("x-forwarded-host", "p86-v"), "outside_header_reserved"),
        (header("host", "p86-v"), "outside_header_reserved"),
        (header("bad name", "p86-v"), "invalid_value"),
        (header("X-Key", "a\nb"), "outside_header_value_invalid"),
        (header("X-Key", "  "), "outside_header_value_invalid"),
        (header("X-Key", "abc\u00e9"), "outside_header_value_invalid"),
        (header("X-Key", "a\u0000b"), "outside_header_value_invalid"),
        (
            {"url": f"https://mcp-x.{OTHER_NS}.svc.cluster.local:8443/mcp"},
            "outside_auth_not_https",
        ),
        ({"url": "https://user:pw@mcp.example.com/mcp"}, "invalid_value"),
        (header("X-Key", "k" * (MAX_VALUE_BYTES + 1)), "outside_header_value_invalid"),
        ({"kind": "remote"}, "invalid_value"),
    ],
)
def test_header_items_are_checked_before_anything_is_stored(client, cluster, mongo, body, code):
    sent = {**header_body(), **body}
    r = client.post(HUB, json=sent)
    assert detail_code(r) == (422, code), r.text
    value = (sent.get("auth") or {}).get("header_value")
    if value and value.strip():
        assert value not in r.text
    assert mongo.hub_items.count_documents({"name": "outside"}) == 0
    store = client.app.state.cluster_service.secret_store()
    assert not [n for _ns, n in store.secrets if n.startswith("daegam-outside-header-")]


def test_a_service_url_in_the_tenants_namespace_needs_no_https(client, cluster):
    url = f"http://mcp-x.{DEFAULT_NS}.svc.cluster.local:8080/mcp"
    r = client.post(HUB, json={**header_body(), "url": url})
    assert r.status_code == 201, r.text
    r = client.post(HUB, json={**header_body("tls"), "url": url.replace("http:", "https:")})
    assert r.status_code == 201, r.text
    # tab 은 field-value 의 글자다
    r = client.post(HUB, json={**header_body("tab"), **header("X-Key", "a\tb")})
    assert r.status_code == 201, r.text


def test_the_platform_calls_an_in_cluster_url_with_its_scheme(monkeypatch):
    """https Service 주소는 https 로 부른다 (S2). 헤더를 실으면 리다이렉트를 따라가지 않는다."""
    import asyncio
    import contextlib

    import httpx2

    from daegam.services import hub as hub_module

    seen: list = []

    @contextlib.asynccontextmanager
    async def fake_client(client, namespace, service, port, *, headers, timeout, scheme):
        seen.append((scheme, dict(headers or {})))
        async with httpx2.AsyncClient() as hc:
            yield hc, f"{scheme}://{service}.{namespace}.svc:{port}"
            seen.append(hc.follow_redirects)

    class _Res:
        tools = ["echo_header"]

    async def fake_list(url, *, timeout, http_client=None, headers=None):
        seen.append(url)
        return _Res()

    import daegam.mcp_client as mcp_client

    monkeypatch.setattr(hub_module, "bearer_service_client", fake_client)
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    url = f"https://mcp-x.{DEFAULT_NS}.svc.cluster.local:8443/mcp"
    names = asyncio.run(hub_module._default_tool_lister(object(), url, {"X-Key": "k"}))
    assert names == ["echo_header"]
    assert seen == [
        ("https", {"X-Key": "k"}),
        f"https://mcp-x.{DEFAULT_NS}.svc:8443/mcp",
        False,
    ]


def test_a_validation_error_does_not_echo_the_header_value(client, cluster):
    """FastAPI 의 422 는 본문 전체를 `input` 으로 싣는다. 헤더 값을 받는 라우트도 모든 라우트처럼
    그것을 뺀다 (security-invariants 1절)."""
    body = header_body()
    del body["name"]
    r = client.post(HUB, json=body)
    assert r.status_code == 422 and SECRET not in r.text, r.text
    item = client.post(HUB, json=header_body()).json()
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": SECRET, "extra": SECRET})
    assert r.status_code == 422 and SECRET not in r.text, r.text


def test_replacing_the_value_raises_the_header_revision(client, cluster, mongo):
    item = client.post(HUB, json=header_body()).json()
    rev = item["auth"]["rev"]
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "Bearer new-value"})
    assert r.status_code == 200, r.text
    assert "new-value" not in r.text
    assert r.json()["auth"]["rev"] != rev
    assert managed(client, item["id"]) == {VALUE_KEY: "Bearer new-value"}
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "a\u0007b"})
    assert detail_code(r) == (422, "outside_header_value_invalid") and "a\u0007b" not in r.text
    plain = client.post(
        HUB, json={"name": "plain", "kind": "entry", "url": OUTSIDE_URL, "transport": "sse"}
    ).json()
    r = client.put(f"{HUB}/{plain['id']}/auth/header", json={"value": "x"})
    assert detail_code(r) == (422, "invalid_kind")


def test_only_the_creator_and_tenant_admins_replace_the_value(client, cluster):
    with as_principal(client, "dev-a", "developer"):
        item = client.post(HUB, json=header_body()).json()
        assert item["auth"]["created_by"] == "dev-a"
        assert item["permissions"]["replace_header"] is True
        assert (
            client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "v1"}).status_code == 200
        )
    with as_principal(client, "dev-b", "developer"):
        assert client.get(f"{HUB}/{item['id']}").json()["permissions"]["replace_header"] is False
        r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "v2"})
        assert detail_code(r) == (403, "forbidden")
    with as_principal(client, "viewer-d", "viewer"):
        assert client.get(f"{HUB}/{item['id']}").json()["permissions"]["replace_header"] is False
    with as_principal(client, "admin-c", "tenant-admin"):
        assert client.get(f"{HUB}/{item['id']}").json()["permissions"]["replace_header"] is True
        assert (
            client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "v3"}).status_code == 200
        )
    assert managed(client, item["id"]) == {VALUE_KEY: "v3"}


def test_deleting_the_item_deletes_its_managed_secret(client, cluster):
    item = client.post(HUB, json=header_body()).json()
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert managed(client, item["id"]) is None


def test_the_sweep_deletes_managed_secrets_whose_item_is_gone(client, cluster, mongo):
    """플랫폼은 Secret 의 목록을 볼 수 없어(rbac.yaml) 장부로 찾는다. 앞 판이 항목을 지우면 Secret
    과 행이 남는다. 뒤 판이 더한 종류의 항목은 있는 것으로 세고, 막 쓰기 시작한 행은 두며, 정리 중에
    다시 쓰기 시작한 행은 지우지 않는다."""
    from datetime import timedelta

    from daegam.store.base import utcnow

    live = client.post(HUB, json=header_body()).json()
    store = client.app.state.cluster_service.secret_store()
    ns = client.app.state.settings.namespace
    hub = client.app.state.hub_service
    ledger = hub.outside_headers.ledger
    old = utcnow() - timedelta(hours=1)
    for orphan in ("gone-1", "newer-1", "fresh-1"):
        store.put(
            managed_name(orphan), {VALUE_KEY: "x"}, namespace=ns, labels=managed_labels(orphan)
        )
        client.portal.call(ledger.note_write, orphan, utcnow() if orphan == "fresh-1" else old)
    mongo.hub_items.insert_one({"_id": "newer-1", "kind": "future", "tenant": "default"})
    mongo.outside_header_secrets.update_one({"_id": live["id"]}, {"$set": {"written_at": old}})
    out = client.portal.call(hub.outside_headers.sweep, None)
    assert out == {"dropped": ["gone-1"]}
    assert managed(client, live["id"]) is not None
    assert store.get(managed_name("gone-1"), namespace=ns) is None
    assert store.get(managed_name("newer-1"), namespace=ns) is not None
    assert store.get(managed_name("fresh-1"), namespace=ns) is not None
    assert {d["_id"] for d in mongo.outside_header_secrets.find()} == {
        live["id"],
        "newer-1",
        "fresh-1",
    }
    # 지우면 행도 간다
    assert client.delete(f"{HUB}/{live['id']}").status_code == 200
    assert mongo.outside_header_secrets.count_documents({"_id": live["id"]}) == 0


def test_the_sweep_reaches_an_orphan_behind_more_live_rows_than_its_limit(
    client, cluster, mongo, monkeypatch
):
    """한 틱이 보는 수보다 살아 있는 행이 많아도, 남긴 행에 본 시각을 적으므로 그 뒤의 남은 Secret
    에 다음 틱의 차례가 온다."""
    from datetime import timedelta

    from daegam.store import secret_ledger
    from daegam.store.base import utcnow

    monkeypatch.setattr(secret_ledger, "SWEEP_LIMIT", 3)
    store = client.app.state.cluster_service.secret_store()
    ns = client.app.state.settings.namespace
    hub = client.app.state.hub_service
    old = utcnow() - timedelta(hours=2)
    live = [f"live-{i}" for i in range(5)]
    for n, item_id in enumerate([*live, "orphan-1"]):
        store.put(managed_name(item_id), {VALUE_KEY: "x"}, namespace=ns, labels={})
        client.portal.call(
            hub.outside_headers.ledger.note_write, item_id, old + timedelta(seconds=n)
        )
    mongo.hub_items.insert_many(
        [{"_id": i, "kind": "entry", "tenant": "default", "name": i} for i in live]
    )
    dropped: list[str] = []
    for tick in range(3):
        now = utcnow() + timedelta(seconds=tick)
        dropped += client.portal.call(hub.outside_headers.sweep, now).get("dropped", [])
    assert dropped == ["orphan-1"]
    assert store.get(managed_name("orphan-1"), namespace=ns) is None
    assert all(store.get(managed_name(i), namespace=ns) is not None for i in live)
    assert mongo.outside_header_secrets.count_documents({"swept_at": None}) == 0


def test_releases_before_this_phase_hold_header_items_and_later_kinds_are_held_here(
    client, cluster, mongo
):
    """앞 판(P55 B1 부터)은 `auth` 를 모르는 칸으로 읽어 항목을 렌더하지도 다른 테넌트에 열지도
    않는다 (9절). 이 판은 뒤 판의 인증 종류(P86b 의 oauth)를 든 항목을 그렇게 다룬다."""
    item = client.post(HUB, json=header_body()).json()
    doc = raw_item(mongo, item["id"])
    assert unknown_keys(doc, ITEM_KEYS - {"auth"}) == ("auth",)
    later = HubItem.from_doc({**doc, "auth": {**doc["auth"], "kind": "oauth", "client": "c"}})
    assert later.newer_fields == ("auth.kind",)
    with pytest.raises(Exception) as e:
        refuse_newer(later)
    assert getattr(e.value, "code", None) == "hub_item_newer"
    # 모르는 칸을 든 채 다시 쓰여도 그 칸을 잃지 않고, 응답은 아는 칸만 싣는다
    assert later.to_doc()["auth"]["client"] == "c"
    mongo.hub_items.update_one({"_id": item["id"]}, {"$set": {"auth.client": "c"}})
    assert "client" not in client.get(f"{HUB}/{item['id']}").json()["auth"]


# -- 카탈로그 원격 (A3) ------------------------------------------------------------------


def catalog_body(server, name: str = "from-cat", **extra) -> dict:
    return {
        "name": name,
        "kind": "entry",
        "url": server.url,
        "transport": server.transport,
        "catalog": server.name,
        **extra,
    }


def test_a_catalog_remote_that_needs_a_key_is_imported_with_its_header(client, cluster, lister):
    """자격 증명이 비밀 헤더 하나뿐인 카탈로그 원격은 그 헤더를 싣는 header entry 로 가져온다.
    이름은 대소문자를 가리지 않고, 사람이 적은 이름 그대로 싣는다."""
    cat = snapshot()
    keyed = next(s for s in cat.servers if s.import_header)
    assert keyed.needs_credentials and keyed.url is not None
    typed = keyed.import_header.upper()
    r = client.post(HUB, json=catalog_body(keyed, **header(typed, SECRET)))
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["source"]["catalog"] == {"server": keyed.name, "release": cat.release}
    assert item["auth"]["header_name"] == typed and SECRET not in r.text
    assert managed(client, item["id"]) == {VALUE_KEY: SECRET}
    assert lister.seen == [{typed: SECRET}]


def test_catalog_remotes_one_header_cannot_carry_stay_refused(client, cluster, mongo):
    """키가 필요한 원격을 헤더 없이, 다른 헤더로, remote 항목으로 가져올 수 없다. 로그인이나 주소의
    비밀 변수나 헤더 둘 이상이 필요한 원격은 헤더를 골라도 가져올 수 없다. 아무것도 저장하지
    않는다."""
    cat = snapshot()
    keyed = next(s for s in cat.servers if s.import_header)
    assert keyed.import_header is not None
    login = next(s for s in cat.servers if s.needs_login)
    url_secret = next(s for s in cat.servers if s.url_secret_variables)
    two = next(s for s in cat.servers if len([h for h in s.headers if h.required]) > 1)
    first = next(h.name for h in two.headers if h.required)
    refused = [
        catalog_body(keyed),
        catalog_body(keyed, **header("X-Other-Key", SECRET)),
        {**catalog_body(keyed), "kind": "remote"},
        catalog_body(login, **header(keyed.import_header, SECRET)),
        catalog_body(url_secret, **header(keyed.import_header, SECRET)),
        catalog_body(two, **header(first, SECRET)),
    ]
    for body in refused:
        r = client.post(HUB, json=body)
        assert detail_code(r) == (422, "invalid_value"), (body["catalog"], r.text)
        assert SECRET not in r.text
    assert mongo.hub_items.count_documents({"name": "from-cat"}) == 0
    store = client.app.state.cluster_service.secret_store()
    assert not [n for _ns, n in store.secrets if n.startswith("daegam-outside-header-")]


# -- 플랫폼 자신의 호출과 값 확인 (6.3·6.4절) -----------------------------------------------------


def test_the_platform_sends_the_header_and_returns_the_check_with_the_save(client, cluster, lister):
    from daegam.mcp_client import AuthRefused

    item = client.post(HUB, json=header_body()).json()
    assert lister.seen == [{"Authorization": SECRET}]
    assert item["auth"]["check"]["outcome"] == "ok"
    lister.answer = AuthRefused(401)
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "Bearer wrong"})
    assert r.status_code == 200 and r.json()["auth"]["check"]["outcome"] == "header_refused"
    assert lister.seen[-1] == {"Authorization": "Bearer wrong"}
    warnings = client.get(f"{HUB}/{item['id']}").json()["status"]["warnings"]
    assert any(w.startswith("outside_header_refused:") for w in warnings)
    lister.answer = ConnectionError("down")
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "Bearer other"})
    assert r.json()["auth"]["check"]["outcome"] == "unreachable"
    assert not client.get(f"{HUB}/{item['id']}").json()["status"]["warnings"]


def test_refresh_tools_updates_the_check(client, cluster, lister):
    from daegam.mcp_client import AuthRefused

    item = client.post(HUB, json=header_body()).json()
    lister.answer = AuthRefused(403)
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert detail_code(r) == (502, "tools_list_failed") and SECRET not in r.text
    assert client.get(f"{HUB}/{item['id']}").json()["auth"]["check"]["outcome"] == "header_refused"
    lister.answer = None
    r = client.post(f"{HUB}/{item['id']}/refresh-tools")
    assert r.status_code == 200 and r.json()["auth"]["check"]["outcome"] == "ok"
    assert r.json()["tool_names"] == ["echo_header"]


def test_items_without_a_header_send_none(client, cluster, lister):
    plain = client.post(
        HUB, json={"name": "plain", "kind": "entry", "url": OUTSIDE_URL, "transport": "sse"}
    ).json()
    assert client.post(f"{HUB}/{plain['id']}/refresh-tools").status_code == 200
    assert lister.seen == [{}]


def test_narrowing_a_header_item_is_audited_as_dropping_copies(client, cluster, mongo):
    from tests.helpers.hub import SHARED_ACCESS

    item = client.post(HUB, json={**header_body(), "access": SHARED_ACCESS}).json()
    assert client.put(f"{HUB}/{item['id']}/access", json=SHARED_ACCESS).status_code == 200
    narrowed = {**SHARED_ACCESS, "consume": {"tenants": []}, "view": {"tenants": []}}
    assert client.put(f"{HUB}/{item['id']}/access", json=narrowed).status_code == 200
    actions = [
        e["action"]
        for e in mongo.audit_events.find({"path": f"/api/hub/{item['id']}/access"}).sort("ts", 1)
    ]
    assert actions == ["set_access", "set_access_drop_header_copies"]
    # 값은 감사에 없다
    assert SECRET not in str(list(mongo.audit_events.find({})))


def test_list_tools_reports_a_refused_header_and_keeps_it_on_the_url(monkeypatch):
    """MCP 클라이언트는 401 의 상태 코드를 내주지 않는다. 응답 훅이 재서 `AuthRefused` 다. 헤더를
    실어 만든 클라이언트는 리다이렉트를 따라가지 않는다. Kubernetes API 서버가 낸 401(`Status`
    본문)은 서버가 헤더를 거절한 것이 아니다."""
    import asyncio

    import httpx2

    from daegam import mcp_client

    body: dict = {"error": "invalid_token"}

    def refuse(_request):
        return httpx2.Response(401, json=body)

    made: list[httpx2.AsyncClient] = []

    def factory(headers=None, timeout=None, auth=None):
        c = httpx2.AsyncClient(
            transport=httpx2.MockTransport(refuse), headers=headers, follow_redirects=True
        )
        made.append(c)
        return c

    monkeypatch.setattr(mcp_client, "create_mcp_http_client", factory)

    async def run():
        return await mcp_client.list_tools(
            "https://mcp.example.com/mcp", timeout=5, headers={"X-API-Key": "k"}
        )

    with pytest.raises(mcp_client.AuthRefused) as e:
        asyncio.run(run())
    assert e.value.status == 401
    assert made and made[0].follow_redirects is False
    assert made[0].headers["X-API-Key"] == "k"
    body.clear()
    body.update({"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 401})
    with pytest.raises(Exception) as e2:
        asyncio.run(run())
    assert not isinstance(e2.value, mcp_client.AuthRefused)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({"consume": {"tenants": ["a"]}}, {"consume": {"tenants": []}}, True),
        ({"consume": {"tenants": ["a"]}}, {"consume": {"tenants": ["*"]}}, False),
        ({"consume": {"tenants": ["*"]}}, {"consume": {"tenants": ["a"]}}, True),
        ({"consume": {"tenants": ["*"]}}, {"consume": {"tenants": ["*"]}}, False),
        ({"consume": {"tenants": []}}, {"consume": {"tenants": ["a"]}}, False),
        # manage 는 consume 을 품는다. 주인 테넌트는 경계에서 빠지지 않는다
        ({"manage": {"tenants": ["a"]}}, {"consume": {"tenants": ["a"]}}, False),
        ({"consume": {"tenants": ["default"]}}, {"consume": {"tenants": []}}, False),
    ],
)
def test_narrowed_tells_removed_tenants(before, after, expected):
    from daegam.auth.hub_access import Access, narrowed

    got = narrowed(
        Access.model_validate(before), Access.model_validate(after), "default", "consume"
    )
    assert got is expected


def test_no_log_record_carries_the_header_value(
    client, cluster, lister, caplog, monkeypatch, fake_clusters
):
    """만들기, 값 바꾸기, Tool 새로고침의 실패(401 과 연결 오류), 배포의 도달 확인 실패, 사본 적용의
    실패와 정리의 어느 기록에도 값이 없다. 예외 문맥(exc_info)의 글도 본다."""
    import logging

    import httpx2

    from daegam.mcp_client import AuthRefused
    from tests.helpers.gateways import create_gateway, deploy_gateway

    caplog.set_level(logging.DEBUG)
    item = client.post(HUB, json=header_body()).json()
    client.put(f"{HUB}/{item['id']}/auth/header", json={"value": SECRET})
    for answer in (AuthRefused(401), httpx2.ConnectError("refused")):
        lister.answer = answer
        client.post(f"{HUB}/{item['id']}/refresh-tools")
    r = create_gateway(client, None, name="gw-log", backends=[{"hub_item_id": item["id"]}])
    deploy_gateway(client, r.json()["id"])
    lister.answer = None
    fake = fake_clusters.by_name["edge"]
    applied = fake.apply

    def broken_apply(doc, *a, **k):
        if doc.get("kind") == "Secret":
            raise RuntimeError(f"apply failed for {doc['metadata']['name']}")
        return applied(doc, *a, **k)

    monkeypatch.setattr(fake, "apply", broken_apply)
    deploy_gateway(client, r.json()["id"])
    client.portal.call(client.app.state.hub_service.outside_headers.sweep, None)
    texts = [rec.getMessage() for rec in caplog.records]
    texts += [
        logging.Formatter().formatException(rec.exc_info) for rec in caplog.records if rec.exc_info
    ]
    assert not [t for t in texts if SECRET in t]
