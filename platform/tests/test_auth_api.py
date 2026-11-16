"""P6 F1 보강: oidc 모드의 GET /auth/config, GET /me, GET /tenants. header 모드의 /auth/config 는
test_oidc_config.py 가 본다."""

from __future__ import annotations

import asyncio

import httpx

from daegam.auth import rbac
from tests.helpers.auth import bearer
from tests.helpers.consts import OIDC_AUDIENCE, OIDC_ISSUER
from tests.helpers.http import api_client, make_app

# -- oidc 모드 -------------------------------------------------------------------


def test_oidc_me_reflects_memberships(oidc_b_viewer, token, mongo):
    # 설정은 토큰 없이 읽힌다. UI 가 로그인 전에 이 모양을 읽는다
    assert oidc_b_viewer.get("/auth/config").json() == {
        "auth_mode": "oidc",
        "issuer": OIDC_ISSUER,
        "client_id": None,  # conftest 의 oidc_settings 는 client_id 를 안 준다
        "audience": OIDC_AUDIENCE,
        "scopes": ["openid", "profile", "email", "groups"],
        "groups_claim": "groups",
        "mcp_audience": "daegam-mcp",
        # 플랫폼이 사람 토큰을 내는 모드의 값과 P53b 의 값이다 (test_idp_people.py)
        "personal_tokens": {"max_days": 365},
        "oauth": {"enabled": False, "dormant": False},
        "data_plane_issuer": "idp",
        "idp_confirm_seconds": 604800,
        "recent_sign_in_seconds": 600,
    }
    tok = token("u1", "daegam-a-dev", "daegam-b-view", "unmapped", email="u1@example.com")
    me = oidc_b_viewer.get("/me", headers=bearer(tok)).json()
    assert me["sub"] == "u1" and me["email"] == "u1@example.com"
    assert me["groups"] == ["daegam-a-dev", "daegam-b-view", "unmapped"]
    assert me["client_id"] is None and me["subject_type"] == "user"
    assert me["is_platform_admin"] is False
    assert me["memberships"] == [
        {"tenant": "a", "role": "developer", "source": "okta_group"},
        {"tenant": "b", "role": "viewer", "source": "okta_group"},
    ]
    assert me["scopes_by_tenant"] == {
        "a": sorted(rbac.scopes_for_role("developer")),
        "b": sorted(rbac.scopes_for_role("viewer")),
    }
    assert oidc_b_viewer.get("/tenants", headers=bearer(tok)).json() == [
        {"tenant": "a", "role": "developer"},
        {"tenant": "b", "role": "viewer"},
    ]
    # manual 소속이 섞여도 같은 형태. tenant_settings 에 있는 다른 테넌트는 안 보인다
    mongo["memberships"].insert_one(
        {"sub": "u1", "tenant": "c", "role": "tenant-admin", "source": "manual"}
    )
    mongo["tenant_settings"].insert_one({"_id": "hidden", "tenant": "hidden"})
    assert oidc_b_viewer.get("/tenants", headers=bearer(tok)).json() == [
        {"tenant": "a", "role": "developer"},
        {"tenant": "b", "role": "viewer"},
        {"tenant": "c", "role": "tenant-admin"},
    ]
    # client credentials 토큰은 client 주체이고 어느 테넌트의 소속도 없다
    svc = bearer(token("svc-1", extra={"cid": "svc-1", "scp": ["daegam.api"]}))
    me = oidc_b_viewer.get("/me", headers=svc).json()
    assert me["client_id"] == "svc-1" and me["subject_type"] == "client"
    assert me["memberships"] == [] and me["scopes_by_tenant"] == {}
    assert oidc_b_viewer.get("/tenants", headers=svc).json() == []


def test_oidc_platform_admin_sees_every_tenant(oidc_b_viewer, token):
    pa = bearer(token("root", "daegam-admins", "daegam-a-dev"))
    assert (
        oidc_b_viewer.patch("/tenants/acme/settings", headers=pa, json={"quota": {}}).status_code
        == 200
    )
    me = oidc_b_viewer.get("/me", headers=pa).json()
    assert me["is_platform_admin"] is True
    assert me["memberships"][0] == {"tenant": "*", "role": "platform-admin", "source": "okta_group"}
    assert me["scopes_by_tenant"]["a"] == sorted(rbac.SCOPES)  # "*" 가 a 에도 적용된다
    assert oidc_b_viewer.get("/tenants", headers=pa).json() == [
        {"tenant": "*", "role": "platform-admin"},
        {"tenant": "a", "role": "developer"},
        {"tenant": "acme", "role": "platform-admin"},
    ]


# -- 부트스트랩 관리자 그룹 ---------------------------------------------------------


def test_bootstrap_admin_group(
    oidc_settings, mongo, fake_clusters, fake_secrets, oidc_transport, token
):
    """매핑이 하나도 없어도 DAEGAM_BOOTSTRAP_ADMIN_GROUP 그룹이면 platform-admin 이 된다."""
    http = httpx.AsyncClient(transport=oidc_transport, base_url=OIDC_ISSUER)
    app = make_app(
        oidc_settings,
        fake_clusters,
        fake_secrets,
        oidc_http_client=http,
        bootstrap_admin_group="platform-admins",
    )
    with api_client(app) as c:
        boot = bearer(token("root", "platform-admins"))
        me = c.get("/me", headers=boot).json()
        assert me["is_platform_admin"] is True
        assert me["memberships"] == [
            {"tenant": "*", "role": "platform-admin", "source": "bootstrap"}
        ]
        # DB 에는 저장되지 않는다 (그룹에서 빠지면 즉시 소멸)
        assert mongo["memberships"].count_documents({"sub": "root"}) == 0
        # 이 권한으로 최초 매핑을 넣을 수 있다
        r = c.put(
            "/group-mappings",
            headers=boot,
            json=[{"group": "daegam-a-dev", "tenant": "a", "role": "developer"}],
        )
        assert r.status_code == 200, r.text
        # 부트스트랩 그룹이 없는 사용자는 여전히 아무것도 아니다
        nobody = bearer(token("u2", "something-else"))
        assert c.get("/group-mappings", headers=nobody).status_code == 403
        # 첫 platform-admin 매핑을 저장하면 부트스트랩 그룹은 힘을 잃는다. 그렇지 않으면 환경 변수
        # 하나가 매핑 화면에 보이지도, 거둘 수도 없는 관리자를 영원히 만든다
        r = c.put(
            "/group-mappings",
            headers=boot,
            json=[
                {"group": "daegam-a-dev", "tenant": "a", "role": "developer"},
                {"group": "real-admins", "tenant": "*", "role": "platform-admin"},
            ],
        )
        assert r.status_code == 200, r.text
        assert c.get("/me", headers=boot).json()["is_platform_admin"] is False
        real = bearer(token("root2", "real-admins"))
        assert c.get("/me", headers=real).json()["is_platform_admin"] is True
    asyncio.run(http.aclose())
