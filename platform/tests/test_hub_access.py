"""P9 F2 (D23): 허브 항목 접근 정책 access. 판정 표, validate_access 표, 목록 필터·상세 404,
Gateway Profile 저장의 422 두 종, hub_access_revoked 경고와 재동기화 동결."""

from __future__ import annotations

import pytest

from daegam.auth.hub_access import (
    ACTIONS,
    Access,
    AccessError,
    Subjects,
    can,
    permissions_for,
    scope_of,
    tenant_in_boundary,
    validate_access,
)
from daegam.auth.principal import Membership, Principal
from daegam.store.hub import HubItem, HubK8s, HubSource
from tests.helpers.auth import bearer, map_groups
from tests.helpers.backend_apis import CALC_ENDPOINT, uploaded_backend
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import C_DEV_MAPPINGS, EXT_URL, GV, GW, HUB, MS, OTHER_NS, T2
from tests.helpers.http import detail_code
from tests.helpers.hub import (
    SHARED_ACCESS,
    clone_name_of,
    gateway_on_remote_clone,
    hub_env,
    shared_env,
)
from tests.helpers.mcp_servers import clone_and_deploy
from tests.helpers.workers import run_clone_sweep, run_data_plane_auth


def _item(tenant="a", kind="remote", access=None) -> HubItem:
    return HubItem(
        tenant=tenant,
        name="x",
        kind=kind,
        source=HubSource(url=EXT_URL, transport="sse"),
        cluster_id="c1",
        k8s=HubK8s(kind=None, name="hub-x", namespace=f"daegam-{tenant}"),
        access=Access.model_validate(access or {}),
    )


def _p(*memberships: tuple[str, str], groups: list[str] | None = None) -> Principal:
    return Principal(
        sub="u",
        groups=groups or [],
        memberships=[Membership(t, r) for t, r in memberships],
    )


ADMIN = _p(("*", "platform-admin"))


# -- 판정 표 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("access", "principal", "tenant", "expected"),
    [
        # platform-admin 은 주체 조건을 건너뛴다. 경계(요청 테넌트)는 그대로 본다
        ({}, ADMIN, "a", (True, True, True)),
        ({"manage": {"roles": ["tenant-admin"]}}, ADMIN, "a", (True, True, True)),
        ({}, ADMIN, "zzz", (False, False, False)),
        ({"view": {"tenants": ["zzz"]}}, ADMIN, "zzz", (True, False, False)),
        ({"view": {"tenants": ["*"]}}, ADMIN, "zzz", (True, False, False)),
        # 기본값: viewer 는 view 만, developer 는 셋 다
        ({}, _p(("a", "viewer")), "a", (True, False, False)),
        ({}, _p(("a", "developer")), "a", (True, True, True)),
        ({}, _p(("a", "tenant-admin")), "a", (True, True, True)),
        # 다른 테넌트에는 없는 항목
        ({}, _p(("b", "developer")), "b", (False, False, False)),
        # view 를 developer 이상으로 좁히면 viewer 에게 안 보인다
        ({"view": {"roles": ["developer"]}}, _p(("a", "viewer")), "a", (False, False, False)),
        ({"view": {"roles": ["developer"]}}, _p(("a", "developer")), "a", (True, True, True)),
        # groups 만 있으면 role 기본이 꺼진다: 그룹 소속 viewer 의 consume 이 참
        (
            {"consume": {"groups": ["g-pay"]}},
            _p(("a", "viewer"), groups=["g-pay"]),
            "a",
            (True, True, False),
        ),
        ({"consume": {"groups": ["g-pay"]}}, _p(("a", "viewer")), "a", (True, False, False)),
        # 그룹만 둔 consume 은 developer 라도 그룹이 없으면 거짓. manage 로 consume 이 포함된다
        (
            {"consume": {"groups": ["g-pay"]}, "manage": {"roles": ["tenant-admin"]}},
            _p(("a", "developer")),
            "a",
            (True, False, False),
        ),
        (
            {"consume": {"groups": ["g-pay"]}, "manage": {"roles": ["tenant-admin"]}},
            _p(("a", "tenant-admin")),
            "a",
            (True, True, True),
        ),
        # tenants: [b] 로 b 의 developer 허용, c 거부
        ({"view": {"tenants": ["b"]}}, _p(("b", "developer")), "b", (True, False, False)),
        ({"consume": {"tenants": ["b"]}}, _p(("b", "developer")), "b", (True, True, False)),
        ({"consume": {"tenants": ["b"]}}, _p(("b", "viewer")), "b", (False, False, False)),
        ({"consume": {"tenants": ["b"]}}, _p(("c", "developer")), "c", (False, False, False)),
        # "*"
        ({"view": {"tenants": ["*"]}}, _p(("c", "viewer")), "c", (True, False, False)),
        (SHARED_ACCESS, _p(("c", "developer")), "c", (True, True, False)),
        (SHARED_ACCESS, _p(("a", "tenant-admin")), "a", (True, True, False)),
        # manage 위임: b 의 developer 가 셋 다
        ({"manage": {"tenants": ["b"]}}, _p(("b", "developer")), "b", (True, True, True)),
        # 요청 테넌트가 다르면 소속 role 은 뜻이 없다 (a 의 developer 가 b 문맥으로)
        ({}, _p(("a", "developer")), "b", (False, False, False)),
    ],
)
def test_can_table(access, principal, tenant, expected):
    item = _item(access=access)
    got = tuple(can(principal, tenant, item, a) for a in ("view", "consume", "manage"))
    assert got == expected
    p = permissions_for(principal, tenant, item)
    assert (p.view, p.consume, p.manage) == expected


def test_implication_and_boundary_helpers():
    item = _item(access={"manage": {"tenants": ["b"]}})
    assert tenant_in_boundary("b", item, "view") and tenant_in_boundary("b", item, "consume")
    assert tenant_in_boundary("a", item, "manage") and not tenant_in_boundary("c", item, "view")
    assert scope_of(_item()) == "tenant"
    assert scope_of(_item(access={"view": {"tenants": ["*"]}})) == "shared"
    assert scope_of(_item(access={"consume": {"tenants": ["b"]}})) == "shared"
    # manage 만 다른 테넌트에 열린 것은 scope 파생에 들어가지 않는다 (view·consume 기준)
    assert scope_of(_item(access={"manage": {"tenants": ["b"]}})) == "tenant"
    assert scope_of(_item(access={"view": {"tenants": ["a"]}})) == "tenant"


# -- validate_access 표 -------------------------------------------------------------------


DEV_A = _p(("a", "developer"))


def _va(new, current=None, *, principal=DEV_A, cross_tenant=ACTIONS, share_scope="hub:share"):
    return validate_access(
        Access.model_validate(new),
        current=Access.model_validate(current or {}),
        owner_tenant="a",
        cross_tenant=cross_tenant,
        share_scope=share_scope,
        principal=principal,
    )


def test_validate_access_boundary_diff():
    # 경계 확장은 hub:share (platform-admin) 이 없으면 403
    for new in ({"view": {"tenants": ["*"]}}, {"consume": {"tenants": ["b"]}}):
        with pytest.raises(AccessError) as e:
            _va(new)
        assert e.value.status == 403 and e.value.extra["scope"] == "hub:share"
    assert _va({"view": {"tenants": ["*"]}}, principal=ADMIN).view.tenants == ["*"]
    # 유지·축소·roles/groups 변경은 manage 만으로 (developer)
    cur = {"view": {"tenants": ["*"]}, "consume": {"tenants": ["*"]}}
    out = _va({"view": {"tenants": ["*"]}, "consume": {"tenants": ["b"], "groups": ["g"]}}, cur)
    assert out.consume.tenants == ["b"] and out.consume.groups == ["g"]
    out = _va({"view": {"tenants": ["b", "c"]}}, {"view": {"tenants": ["b", "c", "d"]}})
    assert out.view.tenants == ["b", "c"]
    # 하나를 빼고 하나를 더하면 더한 쪽 때문에 확장
    with pytest.raises(AccessError) as e:
        _va({"view": {"tenants": ["b", "e"]}}, {"view": {"tenants": ["b", "c"]}})
    assert e.value.extra["actions"] == ["view"]
    # 특정 목록에서 "*" 로 넓히면 확장이고, "*" 에서 특정 목록으로 줄이면 축소다
    with pytest.raises(AccessError):
        _va({"view": {"tenants": ["*"]}}, {"view": {"tenants": ["b"]}})
    assert _va({"view": {"tenants": ["b"]}}, {"view": {"tenants": ["*"]}}).view.tenants == ["b"]
    # 경계 확장에 드는 scope 는 부르는 쪽이 정한다. platform-admin 이라도 그 scope 가 없으면 403
    with pytest.raises(AccessError) as e:
        _va({"view": {"tenants": ["*"]}}, principal=ADMIN, share_scope="nothing:share")
    assert e.value.status == 403 and e.value.extra["scope"] == "nothing:share"
    # 소유 테넌트 자신은 경계 확장이 아니다. 중복·공백은 정리된다
    out = _va({"view": {"tenants": ["a", " a ", ""], "roles": ["viewer", "viewer"]}})
    assert out.view.tenants == ["a"] and out.view.roles == ["viewer"]


def test_validate_access_roles_and_cross_tenant():
    with pytest.raises(AccessError) as e:
        _va({"manage": {"roles": ["owner"]}})
    assert e.value.status == 422 and e.value.code == "invalid_role"
    assert e.value.extra == {"action": "manage", "roles": ["owner"]}
    # cross_tenant 가 비면 세 동작 모두 다른 테넌트 불가 (platform-admin 이라도)
    for action in ("view", "consume", "manage"):
        with pytest.raises(AccessError) as e:
            _va({action: {"tenants": ["*"]}}, principal=ADMIN, cross_tenant=())
        assert e.value.code == "cross_tenant_unsupported" and e.value.extra["action"] == action
    assert _va({"view": {"roles": ["developer"]}}, cross_tenant=()).view.roles == ["developer"]
    assert _va({"view": {"tenants": ["b"]}}, principal=ADMIN).view.tenants == ["b"]
    # 일부 동작만 열면 나머지 동작의 다른 테넌트는 그 동작 이름의 코드다(Skill 의 manage). 정규화한
    # 테넌트로 보므로 공백만 다른 주인 테넌트는 통과한다
    shared = ("view", "consume")
    with pytest.raises(AccessError) as e:
        _va({"manage": {"tenants": [" b ", "a"]}}, principal=ADMIN, cross_tenant=shared)
    assert e.value.code == "cross_tenant_manage_unsupported" and e.value.extra["tenants"] == ["b"]
    assert _va({"manage": {"tenants": [" a "]}}, cross_tenant=shared).manage.tenants == ["a"]


# -- API (oidc 모드) --------------------------------------------------------------------


@pytest.fixture
def env(oidc_client, mongo, token):
    """oidc 플랫폼 + 매핑(C_DEV_MAPPINGS) + 기본 클러스터."""
    map_groups(mongo, C_DEV_MAPPINGS)
    default_cluster(oidc_client, headers=bearer(token("root", "daegam-admins")))
    return oidc_client


def _entry(client, headers, name="org-entry", **extra):
    r = client.post(
        HUB,
        headers=headers,
        json={
            "name": name,
            "kind": "entry",
            "url": EXT_URL,
            "transport": "streamable-http",
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _profile(client, headers, item_id, name="gw"):
    return client.post(
        GW, headers=headers, json={"name": name, "backends": [{"hub_item_id": item_id}]}
    )


def test_default_policy_list_filter_and_detail_404(env, token):
    client = env
    dev_a, dev_b = bearer(token("dev-a", "daegam-a-dev")), bearer(token("dev-b", "daegam-b-dev"))
    view_a = bearer(token("view-a", "daegam-a-view"))
    item = _entry(client, dev_a)
    assert item["scope"] == "tenant" and item["access"] == Access().model_dump()
    assert item["permissions"] == {
        "view": True,
        "consume": True,
        "manage": True,
        "replace_header": False,
        "replace_secret_env": False,
    }
    assert item["access_updated_by"] is None
    # b 의 developer 에게는 없는 항목이다: 목록에 없고 상세·manifest 는 404, 백엔드로는 422
    assert client.get(HUB, headers=dev_b).json() == []
    assert client.get(f"{HUB}/{item['id']}", headers=dev_b).status_code == 404
    assert client.get(f"{HUB}/{item['id']}/manifest", headers=dev_b).status_code == 404
    r = _profile(client, dev_b, item["id"])
    assert detail_code(r) == (422, "unknown_hub_item")
    # a 의 viewer 는 보되 고르거나 관리하지 못한다
    rows = client.get(HUB, headers=view_a).json()
    assert [i["id"] for i in rows] == [item["id"]]
    assert rows[0]["permissions"] == {
        "view": True,
        "consume": False,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }
    r = client.delete(f"{HUB}/{item['id']}", headers=view_a)
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "hub:write"
    # view 를 developer 이상으로 좁히면 viewer 에게도 없는 항목이다
    r = client.put(
        f"{HUB}/{item['id']}/access", headers=dev_a, json={"view": {"roles": ["developer"]}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_updated_by"] == "dev-a" and r.json()["access_updated_at"]
    assert client.get(HUB, headers=view_a).json() == []
    assert client.get(f"{HUB}/{item['id']}", headers=view_a).status_code == 404
    # tenant-admin 이 manage 를 tenant-admin 으로 올리면 developer 는 403 forbidden(action)
    ta = bearer(token("admin-a", "daegam-a-admin"))
    r = client.put(
        f"{HUB}/{item['id']}/access", headers=ta, json={"manage": {"roles": ["tenant-admin"]}}
    )
    assert r.status_code == 200, r.text
    r = client.put(f"{HUB}/{item['id']}/access", headers=dev_a, json={})
    assert r.status_code == 403 and r.json()["detail"] == {
        "code": "forbidden",
        "detail": "manage permission on this hub item required",
        "action": "manage",
    }
    assert client.delete(f"{HUB}/{item['id']}", headers=dev_a).status_code == 403
    assert client.post(f"{HUB}/{item['id']}/refresh-tools", headers=dev_a).status_code == 403
    # manage 는 consume 을 포함하므로 tenant-admin 은 고를 수 있고, developer 는 기본 consume 으로
    got = client.get(f"{HUB}/{item['id']}", headers=dev_a).json()["permissions"]
    assert got == {
        "view": True,
        "consume": True,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }


def test_boundary_widening_needs_hub_share_and_narrowing_is_manage(env, token):
    client = env
    dev_a = bearer(token("dev-a", "daegam-a-dev"))
    dev_b, dev_c = bearer(token("dev-b", "daegam-b-dev")), bearer(token("dev-c", "daegam-c-dev"))
    admin_a = bearer(token("root", "daegam-admins"), tenant="a")
    item = _entry(client, dev_a)
    # 생성 시 경계 확장도 hub:share
    r = client.post(
        HUB,
        headers=dev_a,
        json={
            "name": "w",
            "kind": "entry",
            "url": EXT_URL,
            "transport": "streamable-http",
            "access": SHARED_ACCESS,
        },
    )
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "hub:share"
    r = client.put(f"{HUB}/{item['id']}/access", headers=dev_a, json={"view": {"tenants": ["*"]}})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "hub:share"
    assert r.json()["detail"]["actions"] == ["view"]
    # platform-admin 이 연다: 모두 보고 b 만 고른다
    r = client.put(
        f"{HUB}/{item['id']}/access",
        headers=admin_a,
        json={"view": {"tenants": ["*"]}, "consume": {"tenants": ["b"]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "shared" and r.json()["access_updated_by"] == "root"
    b_row = client.get(f"{HUB}/{item['id']}", headers=dev_b).json()
    assert b_row["permissions"] == {
        "view": True,
        "consume": True,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }
    c_row = client.get(f"{HUB}/{item['id']}", headers=dev_c).json()
    assert c_row["permissions"] == {
        "view": True,
        "consume": False,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }
    r = _profile(client, dev_c, item["id"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "hub_item_not_consumable"
    assert r.json()["detail"]["hub_item_id"] == item["id"]
    assert _profile(client, dev_b, item["id"]).status_code == 201
    # 소유 테넌트 developer 가 경계를 유지하며 그룹으로 좁힌다 (manage 만으로 200)
    r = client.put(
        f"{HUB}/{item['id']}/access",
        headers=dev_a,
        json={"view": {"tenants": ["*"]}, "consume": {"tenants": ["b"], "groups": ["g-pay"]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access"]["consume"] == {"tenants": ["b"], "roles": [], "groups": ["g-pay"]}
    # 그룹이 없는 b 의 developer 는 이제 못 고르고, g-pay 토큰은 통과
    r = _profile(client, dev_b, item["id"], name="gw2")
    assert detail_code(r) == (422, "hub_item_not_consumable")
    pay_b = bearer(token("dev-b", "daegam-b-dev", "g-pay"))
    assert _profile(client, pay_b, item["id"], name="gw2").status_code == 201
    # 경계에 새 테넌트를 더하는 것은 다시 hub:share. "*" 를 특정 목록으로 좁히는 것은 manage
    r = client.put(
        f"{HUB}/{item['id']}/access",
        headers=dev_a,
        json={"view": {"tenants": ["*"]}, "consume": {"tenants": ["b", "c"]}},
    )
    assert r.status_code == 403 and r.json()["detail"]["actions"] == ["consume"]
    # kind 는 다른 테넌트에 열 수 없는 kind 의 오류에만 실린다
    assert "kind" not in r.json()["detail"]
    r = client.put(f"{HUB}/{item['id']}/access", headers=dev_a, json={"view": {"tenants": ["b"]}})
    assert r.status_code == 200 and r.json()["access"]["view"]["tenants"] == ["b"]
    assert client.get(HUB, headers=dev_c).json() == []
    # 잘못된 role 은 422
    r = client.put(f"{HUB}/{item['id']}/access", headers=dev_a, json={"view": {"roles": ["god"]}})
    assert detail_code(r) == (422, "invalid_role")
    assert "kind" not in r.json()["detail"]
    # manage 를 b 에 위임하면 b 의 developer 가 지울 수 있다
    r = client.put(
        f"{HUB}/{item['id']}/access", headers=admin_a, json={"manage": {"tenants": ["b"]}}
    )
    assert r.status_code == 200
    r = client.delete(f"{HUB}/{item['id']}", headers=dev_b)
    assert detail_code(r) == (409, "in_use")


def test_publish_access_cross_tenant_unsupported(client, fake_clusters):
    """internal·published 게시 본문의 access 는 다른 테넌트를 둘 수 없다 (header 모드 = admin)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = clone_and_deploy(client, _mp(client), name="calc-2").json()
    r = client.post(
        f"{MS}/{inst['id']}/publish", json={"access": {"consume": {"tenants": ["other"]}}}
    )
    assert detail_code(r) == (422, "cross_tenant_unsupported")
    assert r.json()["detail"]["kind"] == "internal"
    r = client.post(
        f"{MS}/{inst['id']}/publish", json={"access": {"view": {"roles": ["developer"]}}}
    )
    assert r.status_code == 201, r.text
    assert r.json()["access"]["view"]["roles"] == ["developer"]
    assert r.json()["access_updated_by"] == "header" and r.json()["scope"] == "tenant"
    # gateway instance 게시
    r = client.post(
        GW,
        json={"name": "gw", "backends": [{"hub_item_id": items["calculator"]["id"]}]},
    )
    assert r.status_code == 201, r.text
    r = client.post(f"{GW}/{r.json()['id']}/deploy", json={})
    assert r.status_code == 201, r.text
    gi = r.json()["id"]
    r = client.post(f"{GW}/{gi}/publish", json={"access": {"view": {"tenants": ["*"]}}})
    assert detail_code(r) == (422, "cross_tenant_unsupported")
    assert r.json()["detail"]["kind"] == "published"
    r = client.post(f"{GW}/{gi}/publish", json={"access": {"manage": {"roles": ["tenant-admin"]}}})
    assert r.status_code == 201, r.text
    assert r.json()["access"]["manage"]["roles"] == ["tenant-admin"]
    # internal 항목에 PUT 으로도 마찬가지
    hub_id = client.get(f"{MS}/{inst['id']}").json()["hub_item"]["id"]
    r = client.put(f"{HUB}/{hub_id}/access", json={"view": {"tenants": ["*"]}})
    assert detail_code(r) == (422, "cross_tenant_unsupported")


def _mp(client):
    """`hub_env` 가 만든 calc-mcp 정의(draft). 새 배포를 하나 더 만들 때 복제 원본이다."""
    return next(p for p in client.get(MS).json() if p["name"] == "calc-mcp")


def test_revoked_boundary_warns_and_freezes_the_clone(client, fake_clusters, mongo):
    """consume 경계에서 빠진 테넌트의 복제본은 남고 경고가 뜬다. 원본을 고쳐도 복제본은 마지막
    원본으로 남고(D23), 경계에 다시 들어오면 원본을 다시 받는다 (P50 1절의 복제본 기록)."""
    cid, items, shared, fake = shared_env(client, fake_clusters)
    inst = gateway_on_remote_clone(client, shared)
    name = clone_name_of(shared)
    assert client.get(f"{GW}/{inst['id']}", headers=T2).json()["status"]["warnings"] == []
    # 경계에서 other 를 뺀다 (빈 정책 = 소유 테넌트만). 복제본은 남고 경고가 뜬다
    r = client.put(f"{HUB}/{shared['id']}/access", json={})
    assert r.status_code == 200 and r.json()["scope"] == "tenant"
    st = client.get(f"{GW}/{inst['id']}", headers=T2).json()["status"]
    assert st["warnings"] == ["hub_access_revoked: org-tools"]
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is not None
    # 원본이 바뀌어도 동결된 복제본에는 밀어 넣지 않는다. 스윕이 동결을 표시한다
    mongo["hub_items"].update_one(
        {"_id": shared["id"]}, {"$set": {"source.url": "https://mcp2.example.com/mcp"}}
    )
    assert run_data_plane_auth(client)["reapplied"] == []
    run_clone_sweep(client)
    assert mongo["shared_clones"].find_one({"source_item_id": shared["id"]})["frozen"] is True
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS)["spec"]["remoteUrl"] == EXT_URL
    # 경계에 다시 들어오면 재개된다
    r = client.put(f"{HUB}/{shared['id']}/access", json={"consume": {"tenants": ["other"]}})
    assert r.status_code == 200 and r.json()["scope"] == "shared"
    assert len(run_data_plane_auth(client)["reapplied"]) == 1
    assert (
        fake.get(GV, "MCPRemoteProxy", name, OTHER_NS)["spec"]["remoteUrl"]
        == "https://mcp2.example.com/mcp"
    )
    assert client.get(f"{GW}/{inst['id']}", headers=T2).json()["status"]["warnings"] == []
    # 원본이 사라지면 복제본은 마지막 원본으로 남고, 스윕이 기록을 동결로 표시한다
    mongo["hub_items"].delete_one({"_id": shared["id"]})
    reapply = run_data_plane_auth(client)
    assert reapply["failed"] == [] and reapply["reapplied"] == []
    run_clone_sweep(client)
    record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})
    assert record["frozen"] is True
    assert record["source"]["source"]["url"] == "https://mcp2.example.com/mcp"
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is not None


def test_subjects_model_rejects_unknown_fields():
    with pytest.raises(ValueError):
        Subjects.model_validate({"tenant": "a"})
    with pytest.raises(ValueError):
        Access.model_validate({"read": {}})


def test_mcp_server_delete_needs_manage_on_its_hub_item(env, token):
    """서버를 지우면 게시 항목도 함께 지워지므로 그 항목의 manage 를 해제와 같이 본다. manage 가
    없는 developer 는 mcp:deploy 가 있어도 403 이고 서버와 항목이 그대로다."""
    client = env
    dev_a, ta = bearer(token("dev-a", "daegam-a-dev")), bearer(token("admin-a", "daegam-a-admin"))
    inst, item = _published_with_manage_narrowed(client, dev_a, ta)
    r = client.delete(f"{MS}/{inst['id']}", headers=dev_a)
    assert r.status_code == 403
    assert r.json() == client.delete(f"{HUB}/{item['id']}", headers=dev_a).json()
    # 서버 응답의 hub_item.permissions 도 같은 판정이다
    hub_item = client.get(f"{MS}/{inst['id']}", headers=dev_a).json()["hub_item"]
    assert hub_item["id"] == item["id"]
    assert hub_item["permissions"] == {"view": True, "consume": True, "manage": False}
    # manage 를 가진 tenant-admin 은 지우고 항목도 함께 사라진다
    assert client.delete(f"{MS}/{inst['id']}", headers=ta).status_code == 200
    assert client.get(f"{HUB}/{item['id']}", headers=ta).status_code == 404


def _published_with_manage_narrowed(client, dev_a, ta):
    """developer 가 배포·게시한 서버와 그 항목. 항목의 manage 는 tenant-admin 으로 좁혔다."""
    _cid, _bp, _ds, mp = _setup_as(client, dev_a)
    inst = _create_as(client, dev_a, mp)
    item = client.post(f"{MS}/{inst['id']}/publish", headers=dev_a, json={}).json()
    r = client.put(
        f"{HUB}/{item['id']}/access", headers=ta, json={"manage": {"roles": ["tenant-admin"]}}
    )
    assert r.status_code == 200, r.text
    return inst, item


def _setup_as(client, headers):
    """`mcp_server_env` 와 같되 oidc 헤더로. 클러스터는 env 가 만들었다."""
    bp = uploaded_backend(client, endpoint=CALC_ENDPOINT, headers=headers)
    r = client.post(
        MS,
        headers=headers,
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
        },
    )
    assert r.status_code == 201, r.text
    return None, bp, None, r.json()


def _create_as(client, headers, mp, name="calc-1"):
    r = clone_and_deploy(client, mp, name, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()
