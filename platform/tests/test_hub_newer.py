"""뒤 판이 쓴 허브 항목 (P55 9절, B1 부터).

이 판은 뒤 판이 더하는 칸(출처의 칸과 최상위의 칸, 이름은 이 시험의 것이다)을 든 문서를 읽고 목록과
상세에 보이며, 이 판이 아는 칸을 쓰는 저장이 그 칸을 지우지 않는다. 그런 항목은 렌더(적용·재적용·
매니페스트), 접근 정책 저장, Tool 규칙 저장, 복제본의 배포와 재적용이 모두 422 `hub_item_newer` 로
멈춘다. 저장 쪽 칸(`seq`)과 옛 칸(`scope`)은 뒤 판의 칸이 아니다. 새 칸이 없는 항목의 문서와
복제본 판은 B1 전과 바이트까지 같다. B2 가 더한 칸(stdio, args, env, Secret env, catalog,
stdio_probe)은 이 판이 아는 칸이다(`tests/test_hub_image_items.py`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from daegam.services.data_plane_auth import KIND_CLONE, KIND_HUB_ITEM
from daegam.services.hub import HubError, clone_source, clone_source_hash, item_from_clone_source
from daegam.store import hub as hub_store
from daegam.store.base import fields_of
from daegam.store.hub import HubItem, HubK8s, HubSource, secret_env_name
from daegam.store.tool_rules import ItemToolRule, ToolCallers
from tests.helpers.clusters import default_cluster, revoke_secret_rights
from tests.helpers.consts import DEFAULT_NS, GV, HUB, OTHER_NS
from tests.helpers.http import detail_code
from tests.helpers.hub import clone_name_of, other_tenant_gateway, shared_image_env
from tests.helpers.workers import raise_epoch, run_data_plane_auth

# 뒤 판이 image 항목의 출처에 더할 수 있는 칸. 이름은 이 시험의 것이다. 다른 테넌트에 열 수 있다
LATER_SOURCE = {"later_source_mark": {"at": None}}
# 이 판(B2)이 아는, Secret env 를 든 항목의 칸 (P55 5절)
SECRET_SOURCE = {"secret_env": [{"name": "API_TOKEN", "set": True}]}
SECRET_TOP = {
    "created_by": "u-dev",
    "secret_env_rev": 2,
    "secret_env_updated_by": "u-dev",
    "secret_env_updated_at": datetime(2026, 11, 9, 12, 0, 0),
}
# 뒤 판이 더할 수 있는 최상위 칸. 이름은 이 시험의 것이다
LATER_TOP = {"later_mark": {"at": None}}
NEWER_FIELDS = ("later_mark", "source.later_source_mark")


def _image(client, fake_clusters) -> dict:
    """기본 테넌트의 Ready image 항목 하나 (도구 필터 fetch)."""
    default_cluster(client)
    r = client.post(
        HUB,
        json={
            "name": "fetch",
            "kind": "image",
            "image": "ghcr.io/x/fetch:1",
            "tools_filter": ["fetch"],
        },
    )
    assert r.status_code == 201, r.text
    fake_clusters.by_name["edge"].set_status(
        GV,
        "MCPServer",
        "hub-fetch",
        DEFAULT_NS,
        {"phase": "Ready", "url": "http://mcp-hub-fetch-proxy.daegam-default.svc:8080/mcp"},
    )
    return r.json()


def _make_newer(mongo, item_id: str, *, secret: bool = True) -> None:
    """뒤 판이 쓴 것처럼 새 칸을 더한다. secret 이면 이 판이 아는 Secret env 칸도 더한다. 거짓이면
    다른 테넌트에 열 수 있는 칸만이다."""
    fields = {f"source.{k}": v for k, v in LATER_SOURCE.items()}
    fields.update(LATER_TOP)
    if secret:
        fields.update({f"source.{k}": v for k, v in SECRET_SOURCE.items()})
        fields.update(SECRET_TOP)
    mongo["hub_items"].update_one({"_id": item_id}, {"$set": fields})


def _new_keys(doc: dict) -> dict:
    return {
        "source": {k: doc["source"][k] for k in doc["source"] if k not in HubSource.model_fields},
        "top": {k: doc[k] for k in LATER_TOP if k in doc},
    }


def test_a_newer_item_is_listed_and_shown_and_writes_keep_its_keys(client, fake_clusters, mongo):
    """목록과 상세가 그대로 보이고 응답은 이 판의 출처 칸만 싣는다. 이 판의 저장(도구 새로 고침,
    출처 칸 쓰기)은 새 칸을 지우지 않는다."""
    item = _image(client, fake_clusters)
    _make_newer(mongo, item["id"])
    kept = _new_keys(mongo["hub_items"].find_one({"_id": item["id"]}))
    assert len(kept["source"]) == 1 and len(kept["top"]) == 1

    rows = {i["id"]: i for i in client.get(HUB).json()}
    detail = client.get(f"{HUB}/{item['id']}")
    assert detail.status_code == 200
    for out in (rows[item["id"]], detail.json()):
        assert not set(LATER_SOURCE) & set(out["source"])
        assert not set(LATER_TOP) & set(out)

    hub = client.app.state.hub_service
    stored = client.portal.call(hub.store.get_unscoped, item["id"])
    assert stored.newer_fields == NEWER_FIELDS

    async def lister(_client, url, bearer=None):
        return ["fetch", "search"]

    hub.tool_lister = lister
    assert client.post(f"{HUB}/{item['id']}/refresh-tools").status_code == 200
    assert client.portal.call(hub.store.set_fields, item["id"], fields_of(stored, {"source"}))
    doc = mongo["hub_items"].find_one({"_id": item["id"]})
    assert doc["tool_names"] == ["fetch", "search"]
    assert _new_keys(doc) == kept


def test_a_newer_item_is_not_rendered_reapplied_or_opened(
    client, fake_clusters, mongo, monkeypatch
):
    """매니페스트, 접근 정책 저장, Tool 규칙 저장이 422 hub_item_newer 이고 아무것도 쓰지 않는다.
    렌더 번호가 올라도 재적용은 그 항목을 같은 코드로 막고 클러스터에 적용하지 않는다."""
    item = _image(client, fake_clusters)
    _make_newer(mongo, item["id"])
    before = mongo["hub_items"].find_one({"_id": item["id"]})

    r = client.get(f"{HUB}/{item['id']}/manifest")
    assert detail_code(r) == (422, "hub_item_newer")
    assert r.json()["detail"]["hub_item_id"] == item["id"]
    assert r.json()["detail"]["fields"] == list(NEWER_FIELDS)
    opened = {"view": {"tenants": ["*"]}, "consume": {"tenants": ["*"]}}
    assert detail_code(client.put(f"{HUB}/{item['id']}/access", json=opened)) == (
        422,
        "hub_item_newer",
    )
    rules = {"rules": {"fetch": {"callers": {"roles": ["developer"]}}}}
    r = client.put(f"{HUB}/{item['id']}/tool-rules", json=rules)
    assert detail_code(r) == (422, "hub_item_newer")
    assert mongo["hub_items"].find_one({"_id": item["id"]}) == before

    fake = fake_clusters.by_name["edge"]
    applied = len(fake.applied)
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    assert run_data_plane_auth(client)["blocked"] == [item["id"]]
    assert fake.applied[applied:] == []
    blocked = mongo["hub_items"].find_one({"_id": item["id"]})["auth_blocked"]
    assert blocked["code"] == "hub_item_newer"


def test_storage_and_retired_keys_do_not_make_an_item_newer(
    client, fake_clusters, mongo, monkeypatch
):
    """`seq`(P68)와 D23 전의 `scope` 는 모델 칸이 아니어도 뒤 판의 칸이 아니다. 그 항목은 렌더되고
    재적용되며 접근 정책을 저장한다."""
    item = _image(client, fake_clusters)
    mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": {"seq": 4, "scope": "tenant"}})
    hub = client.app.state.hub_service
    assert client.portal.call(hub.store.get_unscoped, item["id"]).newer_fields == ()
    assert client.get(f"{HUB}/{item['id']}/manifest").status_code == 200
    assert client.put(f"{HUB}/{item['id']}/access", json={}).status_code == 200
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    assert run_data_plane_auth(client)["reapplied"] == [item["id"]]


def _fixed_item(*, rules: bool) -> HubItem:
    when = datetime(2026, 11, 9, 12, 0, 0)
    return HubItem(
        id="00000000-0000-4000-8000-000000000055",
        tenant="default",
        name="org-img",
        kind="image",
        source=HubSource(
            image="ghcr.io/acme/tools:1",
            transport="streamable-http",
            port=8000,
            tools_filter=["Echo"],
        ),
        cluster_id="c1",
        k8s=HubK8s(
            kind="MCPServer",
            name="hub-org-img",
            namespace=DEFAULT_NS,
            group="default",
            tool_config="hub-org-img-tools",
        ),
        tool_names=["Echo"],
        tool_rules={"Echo": ItemToolRule(callers=ToolCallers(roles=["developer"]))}
        if rules
        else {},
        rules_tool_names=["Echo"] if rules else [],
        created_at=when,
        updated_at=when,
    )


@pytest.mark.parametrize(
    ("rules", "doc_hash", "clone_hash"),
    [
        (False, "c04edb1207f5d5b5", "fc279af0f77b5c37"),
        (True, "056459447f966474", "feb7301bab514aa6"),
    ],
)
def test_documents_and_clone_hashes_without_new_fields_stay_byte_identical(
    rules, doc_hash, clone_hash
):
    """새 칸이 없는 항목의 저장 문서와 복제본 판은 B1 전 코드가 낸 값 그대로다. 그래서 올리는 동안
    아무것도 다시 렌더되지 않는다 (9절 3번). 읽고 다시 쓴 문서도 같다."""
    item = _fixed_item(rules=rules)
    doc = item.to_doc()
    dumped = json.dumps(doc, sort_keys=True, default=str).encode()
    assert hashlib.sha256(dumped).hexdigest()[:16] == doc_hash
    assert clone_source_hash(clone_source(item)) == clone_hash
    read = HubItem.from_doc(doc)
    assert read.newer_fields == () and read.to_doc() == doc


def test_a_clone_source_with_unknown_keys_is_refused_not_trimmed():
    """복제본 기록의 원본 정의나 그 출처에 이 판이 모르는 칸이 있으면 버리지 않고 멈춘다. 최상위의
    칸은 원본 정의에 칸을 더한 뒤 판이 렌더한 동결 기록의 꼴이다. 이 판의 `clone_source` 는 그런
    정의를 내지 않는다(살아 있는 항목의 최상위 칸은 아래 Gateway 테스트가 본다)."""
    source = clone_source(_fixed_item(rules=True))
    assert item_from_clone_source(source).source.tools_filter == ["Echo"]
    for newer, field in (
        ({**source, "source": {**source["source"], **LATER_SOURCE}}, "source.later_source_mark"),
        ({**source, "later_mark": 1}, "later_mark"),
    ):
        with pytest.raises(HubError) as e:
            item_from_clone_source(newer)
        assert (e.value.status, e.value.code) == (422, "hub_item_newer")
        assert e.value.extra == {"hub_item_id": source["id"], "fields": [field]}


def test_a_newer_shared_image_is_held_on_deploy_and_on_the_clone_reapply(
    client, fake_clusters, mongo, monkeypatch
):
    """다른 테넌트의 Gateway 가 뒤 판의 image 항목을 쓰면 배포가 복제본을 적용하지 않고 기록도
    만들지 않는다. 이미 복제본이 있으면 원본이 바뀐 것을 재적용이 같은 코드로 막는다. 경계 밖으로
    나가 동결된 기록이 뒤 판의 정의를 들면 재배포와 재적용이 모두 멈춘다."""
    shared, fake = shared_image_env(client, fake_clusters)
    name = clone_name_of(shared)
    _profile, r = other_tenant_gateway(client, shared, name="gw-i", tools=["Echo"])
    assert r.status_code == 201, r.text
    gateway = r.json()
    clone = fake.get(GV, "MCPServer", name, OTHER_NS)
    record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})

    # 살아 있는 원본: 재적용이 원본 항목과 복제본 기록을 막고 복제본은 그대로다
    _make_newer(mongo, shared["id"], secret=False)
    assert sorted(run_data_plane_auth(client)["blocked"]) == sorted([shared["id"], record["_id"]])
    assert fake.get(GV, "MCPServer", name, OTHER_NS) == clone
    held = mongo["shared_clones"].find_one({"_id": record["_id"]})
    assert held["auth_blocked"]["code"] == "hub_item_newer"
    assert "later_source_mark" not in held["source"]["source"]

    # 새 배포: 복제본도 기록도 만들지 않는다
    mongo["shared_clones"].delete_many({})
    fake.delete(GV, "MCPServer", name, OTHER_NS)
    _profile, r = other_tenant_gateway(client, shared, name="gw-j", tools=["Echo"])
    assert detail_code(r) == (422, "hub_item_newer")
    assert fake.get(GV, "MCPServer", name, OTHER_NS) is None
    assert mongo["shared_clones"].count_documents({}) == 0

    # 동결된 기록: 뒤 판이 렌더한 정의를 든 기록이다
    mongo["hub_items"].update_one(
        {"_id": shared["id"]},
        {
            "$set": {"access": {}},
            "$unset": {**{f"source.{k}": "" for k in LATER_SOURCE}, **dict.fromkeys(LATER_TOP, "")},
        },
    )
    newer_source = {**held["source"], "source": {**held["source"]["source"], **LATER_SOURCE}}
    mongo["shared_clones"].insert_one(
        {**held, "source": newer_source, "frozen": True, "auth_blocked": None}
    )
    raise_epoch(monkeypatch, KIND_CLONE)
    assert run_data_plane_auth(client)["blocked"] == [record["_id"]]
    assert fake.get(GV, "MCPServer", name, OTHER_NS) is None
    portal, state = client.portal, client.app.state
    mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": {"state": "failed"}})
    doc = portal.call(state.gateways.get, gateway["id"])
    with pytest.raises(HubError) as e:
        portal.call(state.gateway_service.provision, doc)
    assert e.value.code == "hub_item_newer"
    assert fake.get(GV, "MCPServer", name, OTHER_NS) is None


def _secret_env(fake, item_id: str) -> str:
    """항목의 Secret env 값 Secret 을 둔다. 그 이름이다."""
    name = secret_env_name(item_id)
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": DEFAULT_NS},
            "stringData": {"API_TOKEN": "t0ken"},
        }
    )
    return name


def _no_render(client, monkeypatch) -> None:
    async def render(_item):
        raise AssertionError("a delete does not render")

    monkeypatch.setattr(client.app.state.hub_service, "render", render)


def test_a_newer_item_is_deleted_from_its_stored_names_with_its_secret(
    client, fake_clusters, mongo, monkeypatch
):
    """뒤 버전의 칸을 든 image 항목도 렌더하지 않고 저장된 이름으로 지운다. 서버, 울타리 설정, 도구
    필터, Secret env 의 Secret(접두어와 항목 id), 문서 순이다."""
    item = _image(client, fake_clusters)
    fake = fake_clusters.by_name["edge"]
    secret = _secret_env(fake, item["id"])
    assert secret == f"hub-env-{item['id']}"
    _make_newer(mongo, item["id"])
    _no_render(client, monkeypatch)
    start = len(fake.deleted)
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert [(kind, name) for _api, kind, _ns, name in fake.deleted[start:]] == [
        ("MCPServer", "hub-fetch"),
        ("MCPAuthzConfig", "hub-fetch-authz"),
        ("MCPToolConfig", "hub-fetch-tools"),
        ("Secret", secret),
    ]
    assert mongo["hub_items"].find_one({"_id": item["id"]}) is None


def test_an_item_this_version_knows_is_deleted_without_its_secret_rule(
    client, fake_clusters, mongo, monkeypatch
):
    """Secret env 가 없는 항목의 삭제도 렌더하지 않고, 그 이름의 Secret 에는 손대지 않는다. 그
    항목은 그 Secret 을 만들지 않았다."""
    item = _image(client, fake_clusters)
    fake = fake_clusters.by_name["edge"]
    secret = _secret_env(fake, item["id"])
    _no_render(client, monkeypatch)
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert fake.get("v1", "Secret", secret, DEFAULT_NS) is not None
    assert fake.get(GV, "MCPServer", "hub-fetch", DEFAULT_NS) is None


def test_a_newer_item_is_not_deleted_where_tenant_secrets_cannot_be_written(
    client, fake_clusters, mongo
):
    """테넌트 Secret 을 쓸 수 없는 클러스터에서는 뒤 버전의 image 항목을 지우기 전에 422 다. 상태도
    객체도 그대로라 Secret 이 홀로 남지 않는다."""
    item = _image(client, fake_clusters)
    fake = fake_clusters.by_name["edge"]
    secret = _secret_env(fake, item["id"])
    _make_newer(mongo, item["id"])
    revoke_secret_rights(mongo, item["cluster_id"])
    r = client.delete(f"{HUB}/{item['id']}")
    assert detail_code(r) == (422, "cluster_rights_missing")
    assert mongo["hub_items"].find_one({"_id": item["id"]})["state"] == "created"
    assert fake.get(GV, "MCPServer", "hub-fetch", DEFAULT_NS) is not None
    assert fake.get("v1", "Secret", secret, DEFAULT_NS) is not None


def _knowing(monkeypatch, *keys: str) -> None:
    """뒤 판처럼 keys 를 아는 판으로 둔다. 항목을 읽는 `from_doc` 이 이 표를 본다."""
    monkeypatch.setattr(hub_store, "ITEM_KEYS", hub_store.ITEM_KEYS | set(keys))


def test_a_hold_lasts_only_while_the_item_holds_keys_this_version_does_not_know(
    client, fake_clusters, mongo, monkeypatch
):
    """뒤 판의 칸을 든 항목은 렌더 번호가 그대로여도 기대 판이 달라져 한 번 시도되고 막힌다. 그 칸을
    아는 판(뒤 판으로 올린 플랫폼)에서는 기대 판에 표시가 없어 막힘이 풀리고, 판이 맞으므로 다시
    렌더할 것도 없다 (P55 9절 2번)."""
    item = _image(client, fake_clusters)
    mongo["hub_items"].update_one({"_id": item["id"]}, {"$set": LATER_TOP})
    assert run_data_plane_auth(client)["blocked"] == [item["id"]]
    with monkeypatch.context() as m:
        _knowing(m, "later_mark")
        result = run_data_plane_auth(client)
        assert result["blocked"] == [] and result["reapplied"] == []
        body = client.get("/settings/data-plane-auth").json()
        assert [r["id"] for r in body["blocked"]] == []


def test_a_top_level_newer_key_on_a_shared_image_stops_its_clones(
    client, fake_clusters, mongo, monkeypatch
):
    """원본 정의에 싣지 않는 최상위 칸만 뒤 판의 것이어도 복제본은 그 항목으로 렌더하지 않는다.
    재적용이 기록을 막고, 그 칸을 아는 판에서는 막힘이 풀리며 판이 맞아 할 일이 없다. 새 배포는
    기록을 만들기 전에 422 다. 항목은 실제 문서를 `from_doc` 으로 읽은 것이다."""
    shared, fake = shared_image_env(client, fake_clusters)
    name = clone_name_of(shared)
    _profile, r = other_tenant_gateway(client, shared, name="gw-i", tools=["Echo"])
    assert r.status_code == 201, r.text
    record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})
    clone = fake.get(GV, "MCPServer", name, OTHER_NS)
    mongo["hub_items"].update_one({"_id": shared["id"]}, {"$set": LATER_TOP})
    hub = client.app.state.hub_service
    assert client.portal.call(hub.store.get_unscoped, shared["id"]).newer_fields == ("later_mark",)

    assert sorted(run_data_plane_auth(client)["blocked"]) == sorted([shared["id"], record["_id"]])
    assert fake.get(GV, "MCPServer", name, OTHER_NS) == clone
    with monkeypatch.context() as m:
        _knowing(m, "later_mark")
        result = run_data_plane_auth(client)
        assert result["blocked"] == [] and result["reapplied"] == []

    mongo["shared_clones"].delete_many({})
    fake.delete(GV, "MCPServer", name, OTHER_NS)
    _profile, r = other_tenant_gateway(client, shared, name="gw-j", tools=["Echo"])
    assert detail_code(r) == (422, "hub_item_newer")
    assert r.json()["detail"]["fields"] == ["later_mark"]
    assert fake.get(GV, "MCPServer", name, OTHER_NS) is None
    assert mongo["shared_clones"].count_documents({}) == 0


def test_a_newer_image_without_secret_env_deletes_without_tenant_secret_rights(
    client, fake_clusters, mongo
):
    """Secret env(`source.secret_env`)가 없는 뒤 판의 image 항목은 Secret 을 갖지 않으므로, 테넌트
    Secret 을 쓸 수 없는 클러스터에서도 지워지고 그 이름의 Secret 에는 손대지 않는다."""
    item = _image(client, fake_clusters)
    fake = fake_clusters.by_name["edge"]
    secret = _secret_env(fake, item["id"])
    _make_newer(mongo, item["id"], secret=False)
    revoke_secret_rights(mongo, item["cluster_id"])
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert fake.get(GV, "MCPServer", "hub-fetch", DEFAULT_NS) is None
    assert fake.get("v1", "Secret", secret, DEFAULT_NS) is not None


def test_the_platform_item_sync_keeps_source_keys_a_newer_version_wrote(client, mongo):
    """플랫폼 항목의 주소를 맞추는 저장은 출처에 이 판의 칸만 덮고 뒤 판이 쓴 칸을 남긴다."""
    web = {"search": {"enabled": True, "provider": "stub"}}
    assert client.patch("/settings", json={"web_tools": web}).status_code == 200
    query = {"kind": "platform", "name": "web-search"}
    url = mongo["hub_items"].find_one(query)["source"]["url"]
    stale = {"source.url": "http://old.example/mcp", "source.region": "eu"}
    mongo["hub_items"].update_one(query, {"$set": stale})
    client.portal.call(client.app.state.platform_tools.sync)
    source = mongo["hub_items"].find_one(query)["source"]
    assert source["url"] == url and source["region"] == "eu"
