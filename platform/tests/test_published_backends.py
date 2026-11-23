"""P57 A1: 토큰 인증에서 게시된 Gateway 를 백엔드로 쓴다
(docs/plan/phase-57-token-exchange.md 6.1·6.2·6.4절).

소비자 C 의 Entry 는 게시된 P 에 묶인 토큰을 싣고, P 의 렌더는 지금 소비자만 받으며, C 는 P 가
저장한 규칙 출처를 자기 이름으로 컴파일한다. 소유자 forbid, 스크립트 Tool, 모르는 이름은 닫힌
쪽으로 넘어진다. Cedar 는 ToolHive 의 principal 꼴로 평가한다(`tests/helpers/cedar_eval.py`).
"""

from __future__ import annotations

from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest

from daegam.auth.cedar import (
    consumer_forbid,
    consumer_tools_only,
    delegation_forbid,
    delegation_permit,
)
from daegam.auth.rbac import ROLES_CLAIM
from daegam.auth.rule_cedar import consumer_clause, published_fence
from daegam.k8s.templates.svc_token import entry_token_objects
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL, published_pins
from daegam.services.published_backends import (
    BOUND_LEEWAY,
    PUBLISHED_CONSUMER_PENDING,
    PUBLISHED_NAMES_UNKNOWN,
    PublishedBackends,
    consumer_sub,
)
from daegam.services.rule_compile import RuleScope, source_forbids
from daegam.services.svc_tokens import RENEW_BEFORE, bound_current
from daegam.store.base import utcnow
from daegam.store.gateways import (
    DEFINITION_FIELDS,
    DEPLOYMENT_FIELDS,
    Gateway,
    GatewayBackendSnapshot,
    GatewayK8s,
    GatewayRevision,
    GatewaySpec,
    PublishedRender,
)
from daegam.store.tool_rules import GatewayToolRule, ToolCallers
from tests.helpers.cedar_eval import allowed, evaluate
from tests.helpers.consts import DEFAULT_NS, GV, GW
from tests.helpers.gateways import cedar_policies, gateway_up, vmcp_of
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env, stored_proxy_publish
from tests.helpers.svc_tokens import claims_of, make_svc_tokens
from tests.helpers.workers import run_data_plane_auth

DEV = {"callers": {"roles": ["developer"]}}
OTHER_GROUP_PERMIT = 'permit(principal in THVGroup::"daegam-other", action, resource);'
NAMED_FORBID = 'forbid(principal, action, resource) when { principal.claim_sub == "x" };'


def _person(role: str, tenant: str = "default", **extra) -> dict:
    return {
        "sub": "u1",
        "tenant": tenant,
        "groups": [f"daegam-{tenant}"],
        ROLES_CLAIM: [f"daegam-role:{tenant}:{role}"],
        **extra,
    }


def _calc_only(items) -> list[dict]:
    return [{"hub_item_id": items["calculator"]["id"]}]


def _publish(client, fake, gateway: dict) -> dict:
    r = client.post(f"{GW}/{gateway['id']}/publish", json={})
    assert r.status_code == 201, r.text
    pub = r.json()
    status = {"phase": "Ready", "observedGeneration": 1}
    fake.set_status(GV, "VirtualMCPServer", gateway["k8s"]["name"], DEFAULT_NS, status)
    return pub


def _lister(client, names: list[str]) -> None:
    async def lister(_client, url, bearer=None):
        return list(names)

    client.app.state.hub_service.tool_lister = lister


def _env(client, fake_clusters, *, rules=None, c_policy=None):
    """게시된 P(calculator 위, rules 는 그 백엔드의 Gateway 규칙)와 그것을 쓰는 C(P 와 orders)."""
    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    backends = [{"hub_item_id": items["calculator"]["id"], "tool_rules": rules or {}}]
    p = gateway_up(client, items, name="p", backends=backends)
    pub = _publish(client, fake, p)
    _lister(client, pub["tool_names"])
    extra = {"authz_policy": c_policy} if c_policy else {}
    c = gateway_up(
        client,
        items,
        name="c",
        backends=[{"hub_item_id": pub["id"]}, {"hub_item_id": items["orders"]["id"]}],
        **extra,
    )
    return items, p, pub, c, fake


def _settle(client) -> None:
    """P 가 소비자를 이름 대고 C 가 P 의 새 지문을 싣기까지 재적용 두 틱."""
    run_data_plane_auth(client)
    run_data_plane_auth(client)


def _doc(client, gateway_id: str) -> dict:
    return client.get(f"{GW}/{gateway_id}").json()


def _row(gateway: dict, kind: str = "published") -> dict:
    return next(b for b in gateway["deployed_backends"] if b["kind"] == kind)


def _bound_token(fake, gateway: dict) -> str:
    row = SimpleNamespace(**_row(gateway))
    name = entry_token_objects(gateway["k8s"]["name"], [row])[row.workload]
    return fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["token"]


def _policies(fake, gateway: dict) -> list[str]:
    return cedar_policies(vmcp_of(fake, gateway["k8s"]["name"]), fake=fake)


def _stored(mongo, gateway_id: str) -> Gateway:
    return Gateway.from_doc(mongo["gateways"].find_one({"_id": gateway_id}))


# -- 6.1 묶인 토큰 ------------------------------------------------------------------------------


def test_the_bound_token_opens_only_the_published_gateway_and_only_for_a_consumer(
    local_admin, fake_clusters, mongo
):
    _items, p, _pub, c, fake = _env(local_admin, fake_clusters)
    _settle(local_admin)
    c = _doc(local_admin, c["id"])
    token = claims_of(_bound_token(fake, c))
    assert token["bound_to"] == f"gateway:{p['id']}" and token["sub"] == "svc:vmcp:gw-c"
    assert token["aud"] == "daegam-mcp" and token["tenant"] == "default"
    assert not {"groups", "cid", "act", "tv"} & set(token)
    assert token["exp"] - token["iat"] == 365 * 24 * 3600
    p_policies = _policies(fake, _doc(local_admin, p["id"]))
    assert allowed(p_policies, token, "Add")
    # P 의 렌더가 지금 소비자를 이름 댄다. 떠난 소비자의 토큰 사본은 받지 않는다
    assert consumer_forbid(f"gateway:{p['id']}", ["svc:vmcp:gw-c"]) in p_policies
    assert not allowed(p_policies, {**token, "sub": "svc:vmcp:gw-old"}, "Add")
    # 다른 Gateway 는 위임 forbid 로 거절한다. C 자신도 받지 않는다
    assert not allowed(_policies(fake, c), token, "orders_GetOrder")
    other = [delegation_permit("gateway:x", "default"), "permit(principal, action, resource);"]
    assert not allowed([*other, delegation_forbid("gateway:x", "default")], token, "Add")


def test_the_bound_token_is_written_after_bound_until_and_bound_until_only_moves_forward(
    local_admin, fake_clusters, mongo, monkeypatch
):
    items, p, pub, _c, fake = _env(local_admin, fake_clusters)
    seen = []
    store = local_admin.app.state.gateway_service.entry_tokens.gateways
    raise_until = store.raise_bound_until

    def c2_tokens() -> list[str]:
        return [o["kind"] for o in fake.applied if o["metadata"]["name"].startswith("svc-gw-c2")]

    async def spy(gateway_id, until):
        seen.append(c2_tokens())
        await raise_until(gateway_id, until)

    monkeypatch.setattr(store, "raise_bound_until", spy)
    c2 = gateway_up(local_admin, items, name="c2", backends=[{"hub_item_id": pub["id"]}])
    monkeypatch.undo()
    token = claims_of(_bound_token(fake, c2))
    # bound_until 을 올릴 때 C2 의 토큰 객체는 아직 없고, 그 뒤에 Secret 과 MCPExternalAuthConfig 가
    # 적용된다
    assert seen and seen[0] == []
    assert {"Secret", "MCPExternalAuthConfig"} <= set(c2_tokens())
    until = _stored(mongo, p["id"]).bound_until
    assert until is not None and int(until.replace(tzinfo=UTC).timestamp()) >= token["exp"] - 1
    # 앞으로만 간다. 옛 값으로 올려도, 배포 쪽 저장이 옛 사본으로 덮어도 그대로다
    local_admin.portal.call(store.raise_bound_until, p["id"], until - timedelta(days=30))
    assert _stored(mongo, p["id"]).bound_until == until
    assert "bound_until" not in DEPLOYMENT_FIELDS | DEFINITION_FIELDS
    old = _stored(mongo, p["id"])
    old.bound_until = None
    local_admin.portal.call(
        lambda: store.transition(old, expect={"deployed"})  # 배포 쪽 필드만 쓴다
    )
    assert _stored(mongo, p["id"]).bound_until == until


def test_bound_current_wants_the_key_issuer_audience_claims_and_six_months():
    tokens = make_svc_tokens()
    issuer = tokens.issuer
    now = utcnow()
    good = issuer.mint_bound(sub="svc:vmcp:gw-c", tenant="t", target="gateway:p", rules="r1")
    kw = {"sub": "svc:vmcp:gw-c", "tenant": "t", "gateway_id": "p", "rules": "r1", "now": now}
    assert bound_current(issuer, good, **kw)
    assert not bound_current(issuer, good, **{**kw, "rules": "r2"})
    assert not bound_current(issuer, good, **{**kw, "rules": None})
    assert not bound_current(issuer, good, **{**kw, "gateway_id": "q"})
    assert not bound_current(issuer, good, **{**kw, "sub": "svc:vmcp:gw-d"})
    assert not bound_current(issuer, good, **{**kw, "now": now + timedelta(days=200)})
    almost = now + timedelta(days=365) - RENEW_BEFORE - timedelta(minutes=1)
    assert bound_current(issuer, good, **{**kw, "now": almost})
    # 서비스 토큰과 gateway_token 의 모양은 맞지 않는다
    grouped = issuer.mint_gateway(sub="svc:vmcp:gw-c", tenant="t", groups=["daegam-t"])
    assert not bound_current(issuer, grouped, **kw)
    # 서명 키가 바뀌면 다시 낸다
    rotated = make_svc_tokens().issuer
    assert not bound_current(rotated, good, **kw)


def test_the_sweep_keeps_a_current_bound_token_and_renews_a_due_one(
    local_admin, fake_clusters, mongo
):
    _items, _p, _pub, c, fake = _env(local_admin, fake_clusters)
    _settle(local_admin)
    sweeper = local_admin.app.state.reconciler.svc_token_sweeper
    before = _bound_token(fake, _doc(local_admin, c["id"]))
    result = local_admin.portal.call(sweeper.run)
    assert c["id"] not in result["renewed"]
    assert _bound_token(fake, _doc(local_admin, c["id"])) == before
    later = utcnow() + timedelta(days=200)
    result = local_admin.portal.call(lambda: sweeper.run(now=later))
    assert c["id"] in result["renewed"]
    after = _bound_token(fake, _doc(local_admin, c["id"]))
    assert after != before and claims_of(after)["bound_to"] == claims_of(before)["bound_to"]


def test_an_entry_with_only_the_script_tool_gets_no_bound_token():
    row = GatewayBackendSnapshot(
        hub_item_id="h",
        hub_item_name="p",
        kind="published",
        workload="c-p",
        url="http://x",
        tool_names=[CODE_MODE_TOOL],
    )
    assert published_pins(row) == []
    assert entry_token_objects("gw-c", [row]) == {}
    row.bound_gateway_id = "p-id"
    assert list(entry_token_objects("gw-c", [row])) == ["c-p"]


# -- 6.2 소비자, 규칙, 울타리, filter ------------------------------------------------------------


def test_the_consumer_compiles_the_published_rules_and_carries_their_digest(
    local_admin, fake_clusters, mongo
):
    _items, p, _pub, c, fake = _env(local_admin, fake_clusters, rules={"Divide": DEV})
    _settle(local_admin)
    p_doc = _stored(mongo, p["id"])
    assert p_doc.published_render is not None
    digest = p_doc.published_render.digest
    assert digest and p_doc.published_render.consumers == ["svc:vmcp:gw-c"]
    c = _doc(local_admin, c["id"])
    token = claims_of(_bound_token(fake, c))
    assert _row(c)["rules_digest"] == token["rules"] == digest
    assert "published_sources" not in _row(c)
    # P 는 그 지문을 실은 소비자에게 규칙 Tool 을 연다. 다른 지문은 받지 않는다
    p_policies = _policies(fake, _doc(local_admin, p["id"]))
    assert allowed(p_policies, token, "Divide")
    assert not allowed(p_policies, {**token, "rules": "0" * 16}, "Divide")
    assert allowed(p_policies, {**token, "rules": "0" * 16}, "Add")
    # C 는 P 의 규칙을 사람에게 지킨다
    c_policies = _policies(fake, c)
    assert allowed(c_policies, _person("developer"), "Divide", role_claim=ROLES_CLAIM)
    assert not allowed(c_policies, _person("viewer"), "Divide", role_claim=ROLES_CLAIM)
    assert allowed(c_policies, _person("viewer"), "Add", role_claim=ROLES_CLAIM)


def test_a_new_digest_on_the_published_gateway_rerenders_the_gateways_above(
    local_admin, fake_clusters, mongo
):
    items, p, pub, c, fake = _env(local_admin, fake_clusters, rules={"Divide": DEV})
    _settle(local_admin)
    old = _stored(mongo, p["id"]).published_render.digest
    body = {
        "name": "p",
        "backends": [
            {"hub_item_id": items["calculator"]["id"], "tool_rules": {"Add": DEV, "Divide": DEV}}
        ],
    }
    assert local_admin.put(f"{GW}/{p['id']}", json=body).status_code == 200
    assert local_admin.post(f"{GW}/{p['id']}/redeploy").status_code == 200
    new = _stored(mongo, p["id"]).published_render.digest
    assert new and new != old
    warnings = _doc(local_admin, c["id"])["status"]["warnings"]
    assert any(w.startswith("rules_digest_behind") for w in warnings)
    assert c["id"] in run_data_plane_auth(local_admin)["reapplied"]
    c = _doc(local_admin, c["id"])
    assert claims_of(_bound_token(fake, c))["rules"] == new
    assert not allowed(_policies(fake, c), _person("viewer"), "Add", role_claim=ROLES_CLAIM)


def test_the_fence_keeps_published_tools_inside_the_consumers_tenant(
    local_admin, fake_clusters, mongo
):
    _items, _p, _pub, c, fake = _env(local_admin, fake_clusters, c_policy=OTHER_GROUP_PERMIT)
    policies = _policies(fake, _doc(local_admin, c["id"]))
    other = {"sub": "u2", "tenant": "other", "groups": ["daegam-other"]}
    assert allowed(policies, other, "orders_GetOrder") or allowed(policies, other, "GetOrder")
    assert not allowed(policies, other, "Add")
    no_tenant = {"sub": "u3", "groups": ["daegam-other"]}
    assert not allowed(policies, no_tenant, "Add")
    assert allowed(policies, {"sub": "u3", "groups": ["daegam-default", "daegam-other"]}, "Add")
    assert allowed(policies, _person("viewer"), "Add", role_claim=ROLES_CLAIM)


def test_every_published_entry_pins_its_names_without_the_script_tool(
    local_admin, fake_clusters, mongo
):
    _items, _p, pub, c, fake = _env(local_admin, fake_clusters)
    tools = vmcp_of(fake, "gw-c")["spec"]["config"]["aggregation"]["tools"]
    entry = next(t for t in tools if t["workload"] == _row(c)["workload"])
    assert sorted(entry["filter"]) == sorted(pub["tool_names"])
    assert CODE_MODE_TOOL not in entry["filter"]


def test_the_consumer_set_holds_live_snapshots_and_an_unpublished_gateway_waits_for_bound_until(
    local_admin, fake_clusters, mongo
):
    items, p, pub, c, fake = _env(local_admin, fake_clusters)
    # 정의만 쓰는 Gateway 는 들지 않는다
    r = local_admin.post(GW, json={"name": "draft", "backends": [{"hub_item_id": pub["id"]}]})
    assert r.status_code == 201, r.text
    service = local_admin.app.state.gateway_service
    p_doc = _stored(mongo, p["id"])
    plan = local_admin.portal.call(service.published.plan, p_doc)
    assert plan.consumers == ("svc:vmcp:gw-c",) and not plan.closed
    for state in ("deploying", "failed", "undeploying", "deleting"):
        mongo["gateways"].update_one({"_id": c["id"]}, {"$set": {"state": state}})
        plan = local_admin.portal.call(service.published.plan, p_doc)
        assert plan.consumers == ("svc:vmcp:gw-c",), state
    # 게시를 거두면 집합이 비지만 마지막 묶인 토큰이 끝날 때까지 forbid 가 남는다
    mongo["hub_items"].delete_one({"_id": pub["id"]})
    until = _stored(mongo, p["id"]).bound_until
    clock = {"now": until}
    # 같은 스토어로 시계만 바꾼다
    timed = PublishedBackends(
        gateways=service.gateways,
        hub_items=service.hub.store,
        trusted=lambda: True,
        now=lambda: clock["now"],
    )
    plan = local_admin.portal.call(timed.plan, _stored(mongo, p["id"]))
    assert plan is not None and plan.consumers == ()
    clock["now"] = until + BOUND_LEEWAY
    assert local_admin.portal.call(timed.plan, _stored(mongo, p["id"])) is None


def _codes(client, gateway_id: str) -> set[str]:
    return {w.split(":", 1)[0] for w in _doc(client, gateway_id)["status"]["warnings"]}


def test_a_new_consumer_warns_pending_until_the_published_gateway_names_it(
    local_admin, fake_clusters, mongo
):
    _items, _p, _pub, c, _fake = _env(local_admin, fake_clusters)
    assert PUBLISHED_CONSUMER_PENDING in _codes(local_admin, c["id"])
    _settle(local_admin)
    assert PUBLISHED_CONSUMER_PENDING not in _codes(local_admin, c["id"])


def test_a_published_row_without_pinned_names_warns_names_unknown(local_admin, mongo):
    """고정할 이름이 execute_tool_script 뿐인 게시된 행은 토큰을 받지 않고 경고한다 (6.2절)."""
    row = GatewayBackendSnapshot(
        hub_item_id="h",
        hub_item_name="pub",
        kind="published",
        workload="c-pub",
        url="http://x",
        tool_names=[CODE_MODE_TOOL],
    )
    doc = Gateway(
        tenant="default",
        name="c",
        display_name="c",
        spec=GatewaySpec(),
        revisions=[GatewayRevision(revision=1)],
        state="deployed",
        deployed_revision=1,
        k8s=GatewayK8s(name="gw-c", namespace="daegam-default", group="gw-c"),
        deployed_backends=[row],
    )
    published = local_admin.app.state.gateway_service.published
    warnings = local_admin.portal.call(published.warnings, doc)
    assert [w.split(":", 1)[0] for w in warnings] == [PUBLISHED_NAMES_UNKNOWN]
    assert warnings[0].endswith("(pub)")
    assert local_admin.portal.call(published.bound, doc) is False


def test_the_marks_follow_the_bound_token_and_the_consumer_set(local_admin, fake_clusters, mongo):
    _items, p, _pub, c, fake = _env(local_admin, fake_clusters)
    service = local_admin.app.state.gateway_service
    marks = local_admin.portal.call(service.published.marks, _stored(mongo, c["id"]))
    assert marks == {"bound": "1"}
    p_marks = local_admin.portal.call(service.published.marks, _stored(mongo, p["id"]))
    assert set(p_marks) == {"consumers"}
    # 소비자가 떠나면 표시가 바뀌어 P 가 다시 렌더된다
    run_data_plane_auth(local_admin)
    assert local_admin.delete(f"{GW}/{c['id']}").status_code == 200
    assert p["id"] in run_data_plane_auth(local_admin)["reapplied"]
    assert _stored(mongo, p["id"]).published_render.consumers == []


# -- 소유자 forbid, 스크립트 Tool, 모르는 이름, idp ---------------------------------------------


def test_a_published_gateway_with_an_owner_forbid_is_refused_and_closes_its_consumers(
    local_admin, fake_clusters, mongo
):
    items, p, pub, c, fake = _env(local_admin, fake_clusters)
    _settle(local_admin)
    body = {
        "name": "p",
        "backends": [{"hub_item_id": items["calculator"]["id"]}],
        "authz_policy": NAMED_FORBID,
    }
    r = local_admin.put(f"{GW}/{p['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["consumer_notice"] == {"closed": ["c"]}
    # forbid 를 이미 든 저장과 forbid 없는 저장은 알리지 않는다
    again = local_admin.put(f"{GW}/{p['id']}", json=body)
    assert again.status_code == 200 and "consumer_notice" not in again.json()
    assert local_admin.post(f"{GW}/{p['id']}/redeploy").status_code == 200
    # P 는 소비자 토큰을 모두 거절한다
    token = claims_of(_bound_token(fake, _doc(local_admin, c["id"])))
    assert not allowed(_policies(fake, _doc(local_admin, p["id"])), token, "Add")
    assert any(
        w.startswith("published_owner_forbid")
        for w in _doc(local_admin, p["id"])["status"]["warnings"]
    )
    assert any(
        w.startswith("published_backend_closed")
        for w in _doc(local_admin, c["id"])["status"]["warnings"]
    )
    r = local_admin.post(GW, json={"name": "c3", "backends": [{"hub_item_id": pub["id"]}]})
    assert detail_code(r) == (422, "published_owner_forbid")
    assert r.json()["detail"]["gateway"] == "p"


def test_the_script_tool_and_unknown_names_are_refused_at_save(local_admin, fake_clusters, mongo):
    _cid, items, _calc, _orders, _remote, fake = hub_env(local_admin, fake_clusters)
    p = gateway_up(
        local_admin, items, name="p", backends=[{"hub_item_id": items["calculator"]["id"]}]
    )
    pub = _publish(local_admin, fake, p)
    mongo["hub_items"].update_one(
        {"_id": pub["id"]}, {"$set": {"tool_names": ["Add", CODE_MODE_TOOL]}}
    )
    hub = local_admin.app.state.hub_service
    hub_tool_names, hub.tool_names = hub.tool_names, None  # 저장된 이름을 그대로 쓴다
    try:
        body = {"name": "c", "backends": [{"hub_item_id": pub["id"], "tools": [CODE_MODE_TOOL]}]}
        assert detail_code(local_admin.post(GW, json=body)) == (422, "published_script_tool")
        mongo["hub_items"].update_one({"_id": pub["id"]}, {"$set": {"tool_names": []}})
        body = {"name": "c", "backends": [{"hub_item_id": pub["id"]}]}
        assert detail_code(local_admin.post(GW, json=body)) == (422, "published_names_unknown")
    finally:
        hub.tool_names = hub_tool_names


def test_proxy_mode_items_stay_refused_under_token_auth(local_admin, fake_clusters, mongo):
    _cid, items, _calc, _orders, _remote, fake = hub_env(local_admin, fake_clusters)
    p = gateway_up(
        local_admin, items, name="p", backends=[{"hub_item_id": items["calculator"]["id"]}]
    )
    r = local_admin.post(f"{GW}/{p['id']}/publish", json={"name": "p-pub"})
    assert r.status_code == 201, r.text
    pub = stored_proxy_publish(local_admin, r.json())
    body = {"name": "c", "backends": [{"hub_item_id": pub["id"]}]}
    assert detail_code(local_admin.post(GW, json=body)) == (422, "published_proxy_unsupported")


def test_header_mode_keeps_an_anonymous_entry_and_no_marks(client, fake_clusters, mongo):
    _items, p, pub, c, fake = _env(client, fake_clusters)
    tools = vmcp_of(fake, "gw-c")["spec"]["config"]["aggregation"]["tools"]
    # header 모드에서도 게시된 백엔드는 스냅샷의 이름에서 execute_tool_script 를 뺀 것만 내놓는다
    # (P56 6.3절)
    row = _row(c)
    assert {"workload": row["workload"], "filter": published_pins(SimpleNamespace(**row))} in tools
    assert row.get("bound_to") is None
    service = client.app.state.gateway_service
    assert client.portal.call(service.published.marks, _stored(mongo, c["id"])) == {}
    assert client.portal.call(service.published.marks, _stored(mongo, p["id"])) == {}
    assert _stored(mongo, p["id"]).published_render is None
    assert not any("svc:vmcp" in policy for policy in _policies(fake, _doc(client, p["id"])))


def test_an_unpublished_gateway_renders_as_before(local_admin, fake_clusters, mongo):
    _cid, items, _calc, _orders, _remote, fake = hub_env(local_admin, fake_clusters)
    g = gateway_up(
        local_admin, items, name="g", backends=[{"hub_item_id": items["calculator"]["id"]}]
    )
    service = local_admin.app.state.gateway_service
    assert local_admin.portal.call(service.published.marks, _stored(mongo, g["id"])) == {}
    assert _stored(mongo, g["id"]).published_render is None
    assert not any("svc:vmcp" in policy for policy in _policies(fake, g))
    assert g["id"] not in run_data_plane_auth(local_admin)["reapplied"]


@pytest.mark.parametrize("model", [Gateway, GatewayBackendSnapshot, PublishedRender])
def test_gateway_documents_and_snapshot_rows_ignore_fields_they_do_not_know(model):
    """Gateway 문서와 스냅샷 행(2.0.0-rc1 다음 후보부터)은 모르는 칸을 버리고 읽는다. 이
    묶음이 쓰는 칸(`published_render`, `bound_until`, 행의 `bound_gateway_id`·`published_sources`)을
    그 릴리스들이 그렇게 읽는다는 근거가 이 설정이다 (9절)."""
    assert model.model_config.get("extra") == "ignore"


def test_the_consumer_sub_is_the_vmcp_service_identity():
    doc = SimpleNamespace(k8s=SimpleNamespace(name="gw-c"))
    assert consumer_sub(doc) == "svc:vmcp:gw-c"  # type: ignore[arg-type]
    assert consumer_sub(SimpleNamespace(k8s=None)) is None  # type: ignore[arg-type]


SHAPES = [
    {"sub": "svc:vmcp:gw-c", "tenant": "default", "bound_to": "gateway:p", "rules": "d1"},
    {"sub": "svc:vmcp:gw-old", "tenant": "default", "bound_to": "gateway:p", "rules": "d1"},
    {"sub": "alice", "tenant": "default", "act": "svc:vmcp:gw-c", "bound_to": "gateway:p"},
    {"sub": "alice", "tenant": "default", "act": {"sub": 7}},
    {"sub": "alice", "tenant": "other", "groups": ["daegam-other"]},
    {"sub": "lena", "groups": ["daegam-default"]},
    {"sub": "svc:platform", "tenant": "default", "groups": ["daegam-default"]},
]


def test_the_new_policies_evaluate_without_errors_for_every_token_shape():
    """새 정책은 ToolHive 가 받는 모든 토큰 꼴에서 오류 없이 평가된다(F0.2). act 가 글이어도
    그렇다. bundle B 의 act.sub 가지는 아직 없다."""
    consumer = consumer_clause("gateway:p", "default", ["svc:vmcp:gw-c"], "d1")
    scope = RuleScope(tenant="default", mode="local", agents={})
    rules = {"Divide": GatewayToolRule(callers=ToolCallers(roles=["developer"]))}
    policies = [
        "permit(principal, action, resource);",
        consumer_forbid("gateway:p", ["svc:vmcp:gw-c"]),
        consumer_forbid("gateway:q", []),
        consumer_tools_only("gateway:p"),
        *published_fence(["Add", "Divide"], "default", "daegam-default"),
        *source_forbids(rules, scope, "vmcp", consumer=consumer),
    ]
    for claims in SHAPES:
        for tool in ("Add", "Divide"):
            assert not evaluate(policies, claims, tool).errors, (claims, tool)
        for action in ("read_resource", "get_prompt"):
            assert not evaluate(policies, claims, "res", action=action).errors, (claims, action)


def test_a_published_gateway_serves_its_consumers_tools_only():
    """소비자의 묶인 토큰은 Tool 호출만 받는다. resources·prompts·completion 은 소비자를 거쳐
    나가지 않는다 (6.1절). 묶이지 않은 토큰과 다른 대상에 묶인 토큰은 이 forbid 에 걸리지
    않는다."""
    base = [
        "permit(principal, action, resource);",
        consumer_forbid("gateway:p", ["svc:vmcp:gw-c"]),
        consumer_tools_only("gateway:p"),
    ]
    consumer = SHAPES[0]
    assert evaluate(base, consumer, "Add").allowed
    for action in ("read_resource", "get_prompt"):
        assert not evaluate(base, consumer, "res", action=action).allowed
        person = {"sub": "alice", "tenant": "default", "groups": ["daegam-default"]}
        assert evaluate(base, person, "res", action=action).allowed
        other = {**consumer, "bound_to": "gateway:x"}
        assert evaluate(base, other, "res", action=action).allowed
    # 빈 집합(게시를 거둔 뒤, 소유자 forbid)에서도 Tool 밖은 닫혀 있다
    closed = [base[0], consumer_forbid("gateway:p", []), consumer_tools_only("gateway:p")]
    assert not evaluate(closed, consumer, "Add").allowed
    assert not evaluate(closed, consumer, "res", action="read_resource").allowed
