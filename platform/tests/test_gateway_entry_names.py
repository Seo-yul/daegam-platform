"""Gateway 의 MCPServerEntry 이름이 누구의 것인지 (D82).

Entry 이름은 Gateway 와 허브 항목의 이름을 하이픈으로 이은 것이라 Gateway `shop` 의 항목 `a-orders`
와 Gateway `shop-a` 의 항목 `orders` 가 둘 다 `shop-a-orders` 다. 배포 전 검사, 이름의 선점과 그
놓기(해제·삭제·실패·재배포·쓸기), 적용 직전 막이, 자기 Entry 만 지우는 재배포, 남의 Entry 를
건너뛰는 다시 렌더하기와 공유 복제본 옮기기를 본다. 스토어 질의는 test_gateways_store.py 가 본다.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

from daegam.auth.principal import header_principal
from daegam.k8s.templates import render_entry
from daegam.services.gateway_entries import GatewayEntries
from daegam.services.gateways import GatewayError
from daegam.store.base import utcnow
from daegam.store.gateway_entry_names import EntryNameClaim, entry_claim_id
from tests.helpers.consts import DEFAULT_NS, GV, GW
from tests.helpers.gateways import create_gateway, deploy_gateway, gateway_up, vmcp_of
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env

# 아래 두 Gateway 가 함께 만드는 Entry 이름
CLASH_ENTRY = "shop-a-orders"
ENTRY_NAMES = "gateway_entry_names"


def _clashing_env(client, fake_clusters, mongo):
    """기본 테넌트 env 에서 calculator 항목의 이름을 `a-orders` 로 바꾼다. Gateway `shop` 의 그
    항목과 Gateway `shop-a` 의 orders 는 Entry 이름이 둘 다 `CLASH_ENTRY` 다. (calculator 백엔드,
    orders 백엔드, 가짜)."""
    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    calc_id, orders_id = items["calculator"]["id"], items["orders"]["id"]
    mongo["hub_items"].update_one({"_id": calc_id}, {"$set": {"name": "a-orders"}})
    return {"hub_item_id": calc_id}, {"hub_item_id": orders_id}, fake


def _foreign_entry(fake, owner_id: str) -> None:
    """`CLASH_ENTRY` 를 다른 문서 owner_id 의 Gateway `shop-a` 것으로 적용해 둔다. 선점이 생기기
    전에 이름을 가져간 Gateway 나, 지운 Gateway 의 라벨이 남은 Entry 의 자리다."""
    fake.apply(
        render_entry(
            tenant="default",
            instance_id=owner_id,
            name=CLASH_ENTRY,
            namespace=DEFAULT_NS,
            group="gw-shop-a",
            url="http://mcp-mcp-orders-proxy.daegam-default.svc.cluster.local:8080/mcp",
            transport="streamable-http",
            hub_item_id="hi-orders",
        )
    )


def _claims(mongo) -> dict[str, str]:
    """Entry 이름 → 그 이름을 선점한 Gateway id."""
    return {d["name"]: d["resource_id"] for d in mongo[ENTRY_NAMES].find()}


def _entry(fake, name: str = CLASH_ENTRY) -> dict:
    return fake.get(GV, "MCPServerEntry", name, DEFAULT_NS)


async def _nothing(*_args, **_kwargs) -> None:
    return None


@contextlib.asynccontextmanager
async def _unclaimed(_self, _doc) -> AsyncIterator[None]:
    yield


# -- 배포 -----------------------------------------------------------------------------------


def test_two_gateways_in_one_namespace_keep_their_own_entries(client, fake_clusters, mongo):
    """두 번째 배포는 409 k8s_name_conflict 이고 아무것도 적용하지 않아 첫 Gateway 의 Entry 는
    그대로다. 응답의 gateway_ids 가 막는 Gateway 를 가리키고 정의는 draft 로 남는다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    shop = gateway_up(client, None, name="shop", backends=[calc])
    assert [b["workload"] for b in shop["deployed_backends"]] == [CLASH_ENTRY]
    assert _claims(mongo) == {CLASH_ENTRY: shop["id"]}

    shop_a = create_gateway(client, None, name="shop-a", backends=[orders]).json()["id"]
    r = deploy_gateway(client, shop_a)
    assert detail_code(r) == (409, "k8s_name_conflict")
    detail = r.json()["detail"]
    assert detail["k8s_name"] == CLASH_ENTRY and detail["namespace"] == DEFAULT_NS
    assert detail["gateway_ids"] == [shop["id"]] and "gateway shop" in detail["detail"]
    entry = _entry(fake)
    assert entry["spec"]["groupRef"] == {"name": "gw-shop"}
    assert entry["metadata"]["labels"]["daegam.devops.ai.kr/id"] == shop["id"]
    assert vmcp_of(fake, "gw-shop-a") is None
    assert client.get(f"{GW}/{shop_a}").json()["state"] == "draft"
    assert _claims(mongo) == {CLASH_ENTRY: shop["id"]}


def test_a_deploy_that_passes_both_reads_stops_at_the_claim(
    client, fake_clusters, mongo, monkeypatch
):
    """함께 돈 두 배포는 배포 전 검사와 적용 직전 막이를 함께 지날 수 있다. 그때도 선점이 하나만
    지나가게 해 뒤의 배포는 저장하기 전에 409 이고 Entry 는 먼저 선점한 Gateway 의 것이다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    shop = gateway_up(client, None, name="shop", backends=[calc])
    shop_a = create_gateway(client, None, name="shop-a", backends=[orders]).json()["id"]
    with monkeypatch.context() as m:
        m.setattr(GatewayEntries, "require_free", _nothing)
        m.setattr(GatewayEntries, "require_own", _nothing)
        r = deploy_gateway(client, shop_a)
    assert detail_code(r) == (409, "k8s_name_conflict")
    assert r.json()["detail"]["gateway_ids"] == [shop["id"]]
    assert _entry(fake)["metadata"]["labels"]["daegam.devops.ai.kr/id"] == shop["id"]
    assert client.get(f"{GW}/{shop_a}").json()["state"] == "draft"
    assert _claims(mongo) == {CLASH_ENTRY: shop["id"]}


def test_two_provisions_at_once_apply_one_entry(client, fake_clusters, mongo, monkeypatch):
    """선점 없이 deploying 에 든 두 Gateway(선점이 생기기 전의 배포)의 provision 이 함께 돌아, 둘
    다 Entry 를 읽은 뒤에 적용하려 한다. provision 이 적용 전에 다시 선점하므로 하나만 적용하고
    다른 하나는 409 로 실패한다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    svc, store = client.app.state.gateway_service, client.app.state.gateways
    ids = [
        create_gateway(client, None, name=name, backends=[backend]).json()["id"]
        for name, backend in (("shop", calc), ("shop-a", orders))
    ]

    async def deploy_unclaimed() -> None:
        for gid in ids:
            await svc.deploy(await store.get(gid), cluster_id=None, principal=header_principal())

    with monkeypatch.context() as m:
        m.setattr(GatewayEntries, "require_free", _nothing)
        m.setattr(GatewayEntries, "claimed", _unclaimed)
        client.portal.call(deploy_unclaimed)
    assert _claims(mongo) == {}
    read_own = GatewayEntries.require_own

    async def race() -> list:
        listed: list[str] = []
        both_listed = asyncio.Event()

        async def require_own(self, doc, kube) -> None:
            # 읽은 뒤 상대도 읽을 때까지 적용하지 않는다. 상대가 선점에서 멈추면 잠시 뒤 지나간다
            await read_own(self, doc, kube)
            listed.append(doc.id)
            if len(listed) == len(ids):
                both_listed.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_listed.wait(), 0.5)

        monkeypatch.setattr(GatewayEntries, "require_own", require_own)
        docs = [await store.get(gid) for gid in ids]
        return await asyncio.gather(*(svc.provision(d) for d in docs), return_exceptions=True)

    results = client.portal.call(race)
    errors = [e for e in results if isinstance(e, GatewayError)]
    winners = [d.id for d in results if not isinstance(d, BaseException)]
    assert len(winners) == 1 and len(errors) == 1, results
    assert errors[0].code == "k8s_name_conflict" and errors[0].extra["gateway_ids"] == winners
    assert _entry(fake)["metadata"]["labels"]["daegam.devops.ai.kr/id"] == winners[0]
    assert _claims(mongo) == {CLASH_ENTRY: winners[0]}
    (loser,) = set(ids) - set(winners)
    assert client.get(f"{GW}/{loser}").json()["state"] == "failed"


def test_a_claim_lost_to_a_holder_that_let_go_is_tried_again(
    client, fake_clusters, mongo, monkeypatch
):
    """선점에 진 뒤 기록을 읽기 전에 쥔 쪽이 놓았으면 한 번 더 선점해 본다. 누가 쥐었는지 모르는
    409 로 끝내지 않는다."""
    calc, _orders, _fake = _clashing_env(client, fake_clusters, mongo)
    names = client.app.state.gateway_service.entries.names
    claim = names.claim
    lost: list[str] = []

    async def lose_once(key, **fields):
        if not lost:
            lost.append(key)
            return None
        return await claim(key, **fields)

    monkeypatch.setattr(names, "claim", lose_once)
    shop = gateway_up(client, None, name="shop", backends=[calc])
    assert len(lost) == 1
    assert _claims(mongo) == {CLASH_ENTRY: shop["id"]}


def test_deploy_does_not_overwrite_an_entry_of_another_document(client, fake_clusters, mongo):
    """선점 기록 없이 클러스터에만 있는 남의 Entry(지운 Gateway 의 라벨이 남은 것 포함)는
    덮어쓰지 않는다. 적용 직전에 409 로 멈춰 아무것도 쓰지 않고, 문서는 failed 에 이유를 남기며
    적용하지 못한 이름의 선점은 놓는다."""
    calc, _orders, fake = _clashing_env(client, fake_clusters, mongo)
    _foreign_entry(fake, "other-gw")
    shop = create_gateway(client, None, name="shop", backends=[calc]).json()["id"]
    r = deploy_gateway(client, shop)
    assert detail_code(r) == (409, "k8s_name_conflict")
    detail = r.json()["detail"]
    assert detail["k8s_name"] == CLASH_ENTRY and detail["gateway_ids"] == ["other-gw"]
    assert "document other-gw" in detail["detail"]
    entry = _entry(fake)
    assert entry["spec"]["groupRef"] == {"name": "gw-shop-a"}
    assert entry["metadata"]["labels"]["daegam.devops.ai.kr/id"] == "other-gw"
    assert vmcp_of(fake, "gw-shop") is None
    assert fake.get(GV, "MCPGroup", "gw-shop", DEFAULT_NS) is None
    doc = client.get(f"{GW}/{shop}").json()
    assert doc["state"] == "failed"
    assert doc["last_error"].startswith(f"k8s_name_conflict: MCPServerEntry {DEFAULT_NS}/")
    assert _claims(mongo) == {}


# -- 실패한 배포의 선점 --------------------------------------------------------------------------


def test_a_failed_deploy_does_not_block_the_owner(client, fake_clusters, mongo, monkeypatch):
    """적용하지 못하고 실패한 배포는 그 이름의 선점을 놓고, 배포 전 검사는 실패한 문서를 세지
    않는다. 그 이름을 쓰는 Gateway 의 재배포가 실패한 문서에 막히지 않는다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    owner = gateway_up(client, None, name="shop-a", backends=[orders])
    # 선점이 생기기 전에 배포된 Gateway 라 기록이 없다
    mongo[ENTRY_NAMES].delete_many({})
    shop = create_gateway(client, None, name="shop", backends=[calc]).json()["id"]
    with monkeypatch.context() as m:
        # 함께 돈 배포처럼 배포 전 검사를 지나 적용 직전 막이에 걸린다
        m.setattr(GatewayEntries, "require_free", _nothing)
        r = deploy_gateway(client, shop)
    assert detail_code(r) == (409, "k8s_name_conflict")
    assert r.json()["detail"]["gateway_ids"] == [owner["id"]]
    assert client.get(f"{GW}/{shop}").json()["state"] == "failed"
    assert _claims(mongo) == {}
    r = client.post(f"{GW}/{owner['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert _claims(mongo) == {CLASH_ENTRY: owner["id"]}
    assert _entry(fake)["metadata"]["labels"]["daegam.devops.ai.kr/id"] == owner["id"]


def test_a_failed_deploy_keeps_the_names_it_applied(client, fake_clusters, mongo):
    """Entry 를 적용한 뒤에 실패한 배포는 그 이름의 선점을 쥔 채 둔다. 다른 Gateway 의 배포는 그
    선점에 막히고 gateway_ids 가 실패한 Gateway 를 가리킨다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    fake.denied.add(("toolhive.stacklok.dev", "virtualmcpservers", "apply"))
    shop = create_gateway(client, None, name="shop", backends=[calc]).json()["id"]
    r = deploy_gateway(client, shop)
    assert r.status_code == 502, r.text
    assert client.get(f"{GW}/{shop}").json()["state"] == "failed"
    assert _entry(fake)["metadata"]["labels"]["daegam.devops.ai.kr/id"] == shop
    assert _claims(mongo) == {CLASH_ENTRY: shop}
    fake.denied.clear()
    shop_a = create_gateway(client, None, name="shop-a", backends=[orders]).json()["id"]
    r = deploy_gateway(client, shop_a)
    assert detail_code(r) == (409, "k8s_name_conflict")
    assert r.json()["detail"]["gateway_ids"] == [shop]
    assert client.get(f"{GW}/{shop_a}").json()["state"] == "draft"


# -- 재배포·해제·삭제·쓸기 ------------------------------------------------------------------------


def test_redeploy_refuses_an_entry_name_of_another_gateway(client, fake_clusters, mongo):
    """재배포도 다른 Gateway 가 쓰는 Entry 이름이면 409 k8s_name_conflict 다. 핀과 두 Gateway 의
    Entry 는 그대로다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    owner = gateway_up(client, None, name="shop-a", backends=[orders])
    shop = gateway_up(client, None, name="shop", backends=[orders])
    r = client.put(f"{GW}/{shop['id']}", json={"name": "shop", "backends": [calc]})
    assert r.status_code == 200, r.text
    r = client.post(f"{GW}/{shop['id']}/redeploy")
    assert detail_code(r) == (409, "k8s_name_conflict")
    assert r.json()["detail"]["k8s_name"] == CLASH_ENTRY
    assert r.json()["detail"]["gateway_ids"] == [owner["id"]]
    doc = client.get(f"{GW}/{shop['id']}").json()
    assert doc["state"] == "deployed" and doc["deployed_revision"] == 1
    entries = {
        e["metadata"]["name"]: e["spec"]["groupRef"]["name"]
        for e in fake.find("MCPServerEntry", DEFAULT_NS)
    }
    assert entries == {CLASH_ENTRY: "gw-shop-a", "shop-orders": "gw-shop"}
    assert _claims(mongo) == {CLASH_ENTRY: owner["id"], "shop-orders": shop["id"]}


def test_redeploy_deletes_only_its_own_stale_entries(client, fake_clusters, mongo):
    """재배포가 빼는 Entry 는 이 문서의 라벨이 붙은 것만 지우고, 뺀 이름의 선점은 놓는다. 선점이
    생기기 전에 다른 Gateway 가 같은 이름으로 가져간 Entry 는 그대로 둔다."""
    calc, orders, fake = _clashing_env(client, fake_clusters, mongo)
    shop = gateway_up(client, None, name="shop", backends=[calc, orders])
    _foreign_entry(fake, "other-gw")
    r = client.put(f"{GW}/{shop['id']}", json={"name": "shop", "backends": [orders]})
    assert r.status_code == 200, r.text
    r = client.post(f"{GW}/{shop['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert [b["workload"] for b in r.json()["deployed_backends"]] == ["shop-orders"]
    entry = _entry(fake)
    assert entry["metadata"]["labels"]["daegam.devops.ai.kr/id"] == "other-gw"
    assert entry["spec"]["groupRef"] == {"name": "gw-shop-a"}
    assert _claims(mongo) == {"shop-orders": shop["id"]}


def test_sweep_releases_names_no_gateway_uses(client, fake_clusters, mongo):
    """리컨실의 쓸기는 유예가 지난 선점 가운데 그 Gateway 가 더는 쓰지 않는 이름의 것만 지운다.
    전이를 저장하기 전에 죽은 배포가 남긴 기록이 그렇다. 쓰는 이름과 유예 안의 기록은 남는다."""
    calc, orders, _fake = _clashing_env(client, fake_clusters, mongo)
    shop = gateway_up(client, None, name="shop", backends=[orders])
    old = utcnow() - timedelta(minutes=10)
    for name, owner, at in (("leaked", "gone-gw", old), ("young", "gone-gw", utcnow())):
        claim = EntryNameClaim(
            id=entry_claim_id(shop["cluster_id"], DEFAULT_NS, name),
            resource_id=owner,
            tenant="default",
            cluster_id=shop["cluster_id"],
            namespace=DEFAULT_NS,
            name=name,
            claimed_at=at,
        )
        mongo[ENTRY_NAMES].insert_one(claim.to_doc())
    mongo[ENTRY_NAMES].update_one({"resource_id": shop["id"]}, {"$set": {"claimed_at": old}})
    svc = client.app.state.gateway_service
    result = client.portal.call(svc.entries.sweep, utcnow())
    assert result == {"released": [entry_claim_id(shop["cluster_id"], DEFAULT_NS, "leaked")]}
    assert _claims(mongo) == {"shop-orders": shop["id"], "young": "gone-gw"}
