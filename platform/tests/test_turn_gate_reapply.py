"""턴 위임 게이트의 재료를 적는 자리 (P53 2·3.5·7절).

배포는 그 배포가 적용하는 렌더의 번호와 앞에 돌던 렌더의 번호를 적고(`DeployRender`), 다시 렌더
표시는 저장된 번호를 담으며, 재적용 틱은 자기 audience 를 처음 적용한 서버에 그 때를 적고
클러스터마다 Gateway 게이트를 판정한다. 상태 API 는 그 판정과 게이트를 막는 문서를 보인다."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from daegam.k8s.client import ClusterUnreachable
from daegam.k8s.gates import pack_epoch
from daegam.k8s.templates.agent import FEATURES_HEADER, IMAGE_HEADER
from daegam.services.agent_status import TURN_TOKENS_UNSUPPORTED_WARNING
from daegam.services.data_plane_auth import KIND_GATEWAY, RENDER_EPOCHS
from daegam.services.turn_gates import HOLD_NO_BINDING, carries_binding
from daegam.store.auth_reapply import (
    TURN_GATES_ID,
    is_stale_mark,
    rendered_epoch,
)
from daegam.store.base import utcnow
from daegam.store.gateways import MongoGatewayStore
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import deploy_spec, seed_tool_server, template_for, with_tool_server
from tests.helpers.aio import run_store
from tests.helpers.auth import admin_headers
from tests.helpers.consts import GW, IMAGE, TENANT_HEADER
from tests.helpers.docs import gateway_doc
from tests.helpers.exposure import front_and_gateway
from tests.helpers.gateways import create_gateway, deploy_gateway, gateway_up
from tests.helpers.hub import hub_env
from tests.helpers.workers import run_data_plane_auth


def _gateway(mongo, gid: str) -> dict:
    return mongo["gateways"].find_one({"_id": gid})


def _verdict(mongo, cluster_id: str) -> dict:
    doc = mongo["auth_reapply"].find_one({"_id": TURN_GATES_ID})
    (verdict,) = [c for c in doc["clusters"] if c["cluster_id"] == cluster_id]
    return verdict


def _turn_tokens(client) -> dict:
    r = client.get("/settings/data-plane-auth")
    assert r.status_code == 200, r.text
    return r.json()["turn_tokens"]


# -- 배포의 기록 -----------------------------------------------------------------------------------


def test_a_failed_redeploy_of_an_old_render_holds_its_cluster_gate(
    local_admin, fake_clusters, mongo, monkeypatch
):
    """적용 전에 실패한 재배포는 앞의 렌더로 돌 수 있다. 그 렌더가 묶임이 없으면 failed 문서도
    클러스터의 게이트를 닫는다. 실패한 첫 배포는 이 코드의 렌더만 돌므로 닫지 않는다."""
    client = local_admin
    from daegam.k8s.templates.svc_token import TokenNameCollision

    cid, items, *_rest = hub_env(client, fake_clusters)
    gw = gateway_up(client, items, name="a")
    mongo["gateways"].update_one({"_id": gw["id"]}, {"$set": {"render_epoch": 1}})

    def collide(k8s_name, backends):
        raise TokenNameCollision("a-calculator", "a-orders", f"{k8s_name}-svc")

    monkeypatch.setattr("daegam.services.gateways.entry_token_objects", collide)
    monkeypatch.setattr("daegam.services.gateway_tokens.entry_token_objects", collide)
    assert client.post(f"{GW}/{gw['id']}/redeploy").status_code == 422
    first = create_gateway(client, items, name="b").json()
    assert deploy_gateway(client, first["id"]).status_code == 422
    assert {_gateway(mongo, g)["state"] for g in (gw["id"], first["id"])} == {"failed"}

    run_data_plane_auth(client)
    verdict = _verdict(mongo, cid)
    assert not verdict["gateway_gate_open"]
    assert [(h["id"], h["reason"]) for h in verdict["holders"]] == [(gw["id"], HOLD_NO_BINDING)]


# -- 다시 렌더 표시 --------------------------------------------------------------------------------


def test_a_stale_writer_encodes_the_epoch_stored_when_it_writes(settings):
    """표시는 쓰는 갱신 안에서 저장된 번호로 만든다. 읽은 뒤 다른 레플리카가 렌더해 번호가
    바뀌었어도 그 번호를 담고, 이미 표시면 그대로 둔다. 조건이 맞지 않으면 쓰지 않는다."""

    async def go(store: MongoGatewayStore):
        await store.create(gateway_doc("gw-1", state="deployed", render_epoch=1))
        await store.coll.update_one({"_id": "gw-1"}, {"$set": {"render_epoch": 3}})
        assert await store.mark_stale("gw-1", expect_state={"deployed"})
        first = (await store.get("gw-1")).render_epoch
        assert await store.mark_stale("gw-1", {"last_error": None}, expect_state={"deployed"})
        again = await store.get("gw-1")
        refused = await store.mark_stale(
            "gw-1", expect_state={"deployed"}, expect_overrides={"spec": {}}
        )
        return first, again, refused

    first, again, refused = run_store(settings, MongoGatewayStore, go)
    assert is_stale_mark(first) and rendered_epoch(first) == 3
    assert again.render_epoch == first and again.last_error is None
    assert not refused


# -- 틱의 판정과 상태 ------------------------------------------------------------------------------


def test_the_tick_judges_each_cluster_and_the_status_shows_it(local_admin, fake_clusters, mongo):
    """묶임을 실은 Gateway 만 있으면 게이트가 열린다. 번호 2 의 렌더로 돌면 닫히고 그 Gateway
    가 까닭과 함께 나온다. 상태 API 는 판정의 때와 젊은지를 보인다. 리컨실 틱이 넘기는 때는 틱의
    시작이라 앞 단계가 예산을 쓴 만큼 늙었으므로, 판정은 재적용 단계가 문서를 읽기 시작한 벽시계로
    적는다."""
    client = local_admin
    _front, gateway, _fake = front_and_gateway(client, fake_clusters)
    cid = gateway["cluster_id"]
    before = _turn_tokens(client)
    (row,) = [c for c in before["clusters"] if c["cluster_id"] == cid]
    assert row["verdict_at"] is None and not row["verdict_fresh"]
    assert not row["gateway_gate_open"]

    started = utcnow().replace(tzinfo=None) - timedelta(seconds=1)
    run_data_plane_auth(client, now=utcnow() - timedelta(seconds=100))
    assert _verdict(mongo, cid)["gateway_gate_open"]
    stamps = [
        c["verdict_at"] for c in mongo["auth_reapply"].find_one({"_id": TURN_GATES_ID})["clusters"]
    ]
    assert stamps and all(at.replace(tzinfo=None) >= started for at in stamps)
    (row,) = [c for c in _turn_tokens(client)["clusters"] if c["cluster_id"] == cid]
    assert row["gateway_gate_open"] and row["verdict_fresh"] and row["holders"] == []

    # 해제가 멈춘 Gateway 는 다시 렌더되지 않고 적힌 렌더로 돈다
    stuck = {"render_epoch": 2, "state": "undeploying"}
    mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": stuck})
    run_data_plane_auth(client)
    (row,) = [c for c in _turn_tokens(client)["clusters"] if c["cluster_id"] == cid]
    assert not row["gateway_gate_open"]
    assert [(h["kind"], h["id"], h["reason"]) for h in row["holders"]] == [
        ("gateway", gateway["id"], HOLD_NO_BINDING)
    ]


def test_a_render_from_another_mode_is_rendered_again_once_in_local_mode(
    local_admin, fake_clusters, mongo
):
    """다른 모드에서 적은 번호 3(묶임 없음)의 Gateway 는 묶임을 싣지 않는다. local 모드의 틱이 한 번
    다시 렌더해 표시를 단 번호로 바꾸고, 그 뒤의 틱은 다시 렌더하지 않는다."""
    client = local_admin
    _front, gateway, _fake = front_and_gateway(client, fake_clusters)
    bound = pack_epoch(RENDER_EPOCHS[KIND_GATEWAY], bound=True)
    assert _gateway(mongo, gateway["id"])["render_epoch"] == bound
    mongo["gateways"].update_one(
        {"_id": gateway["id"]}, {"$set": {"render_epoch": RENDER_EPOCHS[KIND_GATEWAY]}}
    )
    doc = client.portal.call(client.app.state.gateway_service.instances.get, gateway["id"])
    assert not carries_binding(doc)
    assert gateway["id"] in run_data_plane_auth(client)["reapplied"]
    assert _gateway(mongo, gateway["id"])["render_epoch"] == bound
    assert gateway["id"] not in run_data_plane_auth(client)["reapplied"]
    assert _verdict(mongo, gateway["cluster_id"])["gateway_gate_open"]


def _status_warnings(client, url: str) -> list[str]:
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()["status"]["warnings"]


def test_the_status_lists_agents_whose_runtime_lacks_turn_tokens(
    local_client, fake_clusters, mongo
):
    """토큰 인증 모드에서 Tool 소스를 쓰는 Agent 가운데 런타임 기록에 `turn_tokens` 가 없는 것이다.
    기록이 없는 Agent 도 든다. 다시 배포해 그 이름을 알린 런타임은 빠진다."""
    client = local_client
    client.headers.update({**admin_headers(client, mongo), TENANT_HEADER: "default"})
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, features="telemetry")
    rows = _turn_tokens(client)["agents_without_turn_tokens"]
    assert [r["id"] for r in rows] == [agent.id]
    # 기능을 알린 기록에 그 이름이 없으니 Agent 의 상태도 알린다 (P53 7절)
    assert TURN_TOKENS_UNSUPPORTED_WARNING in _status_warnings(client, agent.url)
    headers = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: "telemetry,turn_tokens"}
    assert client.get(f"{agent.url}/agent.json", headers=headers).status_code == 200
    assert _turn_tokens(client)["agents_without_turn_tokens"] == []
    assert TURN_TOKENS_UNSUPPORTED_WARNING not in _status_warnings(client, agent.url)


def test_in_oidc_mode_only_an_mcp_server_source_lacks_turn_tokens(
    local_client, fake_clusters, mongo, monkeypatch
):
    """oidc 모드의 Gateway 소스는 Agent 의 자격으로 부르므로 턴 토큰이 필요 없다. MCP Server 소스만
    런타임 기록에 `turn_tokens` 가 없을 때 든다. 같은 두 Agent 를 local 모드로 읽으면 둘 다 든다."""
    client = local_client
    client.headers.update({**admin_headers(client, mongo), TENANT_HEADER: "default"})
    gateway_agent = deployed_agent(
        client, fake_clusters, mongo, runtime=False, features="telemetry"
    )
    server = seed_tool_server(mongo, cluster_id=gateway_agent.cluster_id, name="orders")
    spec = with_tool_server(template_for(gateway_agent.gw, gateway_agent.ds), server)
    _p, mcp_agent = deploy_spec(client, spec, name="orders-agent")

    def listed() -> set[str]:
        return {r["id"] for r in _turn_tokens(client)["agents_without_turn_tokens"]}

    assert listed() == {gateway_agent.doc["id"], mcp_agent["id"]}
    auth = client.app.state.data_plane_auth
    oidc = dataclasses.replace(
        auth.settings, auth_mode="oidc", oidc_issuer="https://idp.example", data_plane_issuer="idp"
    )
    monkeypatch.setattr(type(auth), "settings", property(lambda _self: oidc))
    assert listed() == {mcp_agent["id"]}


# -- 자기 audience 의 적용 -------------------------------------------------------------------------


def test_an_unreachable_cluster_keeps_its_stored_verdict_inputs(
    local_admin, fake_clusters, mongo, monkeypatch
):
    """판정은 저장된 문서만 읽는다. 클러스터에 연결할 수 없어도 그 클러스터의 Gateway 는 적힌 번호로
    센다. 그 틱은 그 클러스터의 문서를 다시 렌더하지 않는다."""
    client = local_admin
    _front, gateway, _fake = front_and_gateway(client, fake_clusters)
    mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": {"render_epoch": 2}})

    def unreachable(cluster):
        raise ClusterUnreachable("connection refused")

    cluster_service = client.app.state.data_plane_auth.tenancy.cluster_service
    monkeypatch.setattr(cluster_service, "client_for", unreachable)
    assert gateway["id"] not in run_data_plane_auth(client)["reapplied"]
    verdict = _verdict(mongo, gateway["cluster_id"])
    assert [h["reason"] for h in verdict["holders"]] == [HOLD_NO_BINDING]


def test_a_switched_server_gets_applied_at_once_its_render_carries_the_audience(
    client, fake_clusters, mongo, monkeypatch
):
    """자기 audience 로 렌더된 서버의 기록에 `applied_at` 이 없으면 다음 틱이 지금 렌더를 보고 그
    때를 적는다. 아직 그 audience 로 렌더되지 않은 서버는 그 렌더가 보일 때까지 적히지 않는다."""
    front, _gateway, _fake = front_and_gateway(client, fake_clusters)
    mongo["mcp_servers"].update_one(
        {"_id": front["id"]}, {"$unset": {"audience_switch.applied_at": ""}}
    )
    run_data_plane_auth(client)
    switch = mongo["mcp_servers"].find_one({"_id": front["id"]})["audience_switch"]
    assert switch["applied_at"] is not None

    mongo["mcp_servers"].update_one(
        {"_id": front["id"]},
        {"$unset": {"audience_switch.applied_at": ""}, "$set": {"auth_rev": "before-switch"}},
    )

    async def no_render(consumers, tick):
        return None

    monkeypatch.setattr(client.app.state.data_plane_auth, "_reapply", no_render)
    run_data_plane_auth(client)
    monkeypatch.undo()
    switch = mongo["mcp_servers"].find_one({"_id": front["id"]})["audience_switch"]
    assert switch.get("applied_at") is None
    run_data_plane_auth(client)
    switch = mongo["mcp_servers"].find_one({"_id": front["id"]})["audience_switch"]
    assert switch["applied_at"] is not None
