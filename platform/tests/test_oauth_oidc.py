"""oidc 모드의 OAuth 인가 서버: 잠든 서버와 깨어난 서버 (P53b 6.4·6.5·6.6절).

oidc 모드에서 데이터 플레인이 아직 IdP 만 믿는 동안(`idp`) 인가 서버는 잠든다. 새 인가는
`temporarily_unavailable` 로 돌려보내고, 동의 허용은 503 이고, 토큰 엔드포인트는 코드를 쓰지도 갱신
토큰을 회전하지도 가족을 끊지도 않은 채 503 과 Retry-After 로 답한다. RFC 7009 회수와 화면의 연결
끊기는 그대로 된다. 데이터 플레인 발급자를 바꾸는 설정(F6) 전이라, 깨어난 서버는 인가 서버의 잠
표시를 끄고 본다. 그때 연결은 사람 기록과 ID 토큰의 최근 로그인으로 맺고, 접근 토큰은 고정 그룹과
규칙이 이름 댄 IdP 그룹을 갱신마다 새로 싣는다.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from daegam.auth import rbac
from tests.helpers.auth import bearer, map_groups, recent_id_token, with_ui_client
from tests.helpers.consts import PUBLIC_URL
from tests.helpers.docs import TENANT, gateway_doc, seed_gateway
from tests.helpers.http import detail_code
from tests.helpers.oauth import (
    GW_HOST,
    REDIRECT,
    REVOKE,
    authorize,
    consent,
    exchange,
    grant_of,
    handle_for,
    location,
    refresh,
    register,
    split,
)

DEV = "acme-dev"
OPS = "ops"


@pytest.fixture
def oidc_settings(oidc_settings):
    """공개 주소(https)가 있는 oidc 플랫폼. 인가 서버는 꺼지지 않았고 잠들어 있다."""
    return with_ui_client(oidc_settings)


@pytest.fixture
def c(oidc_client, mongo) -> TestClient:
    """acme 의 노출 Gateway 와 OPS 를 이름 댄 규칙, DEV 를 acme developer 로 매핑한 플랫폼."""
    map_groups(mongo, [{"group": DEV, "tenant": TENANT, "role": "developer"}])
    seed_gateway(mongo, "gw-1", GW_HOST)
    spec = {"backends": [{"hub_item_id": "h", "tool_rules": {"t": {"callers": {"groups": [OPS]}}}}]}
    mongo["gateways"].insert_one(gateway_doc("gw-r", spec=spec).to_doc())
    return oidc_client


@pytest.fixture
def cid(c) -> str:
    return register(c)


def person(token, *groups: str, sub: str = "p1", recent: bool = True) -> dict[str, str]:
    """IdP 액세스 토큰과, recent 면 방금 로그인한 ID 토큰을 실은 헤더."""
    headers = bearer(token(sub, *groups, email=f"{sub}@example.com"))
    return {**headers, **recent_id_token(token, sub)} if recent else headers


def awake(c: TestClient, on: bool = True) -> None:
    c.app.state.oauth_authorizer.dormant = not on


def code_for(c: TestClient, cid: str, headers: dict[str, str], **params: str) -> str:
    awake(c)
    r = consent(c, "approve", handle_for(c, cid, **params), headers)
    assert r.status_code == 200, r.text
    return split(r.json()["redirect_to"])[1]["code"]


def connected(c: TestClient, cid: str, headers: dict[str, str]) -> dict:
    r = exchange(c, cid, code_for(c, cid, headers))
    assert r.status_code == 200, r.text
    return r.json()


def claims_of(c: TestClient, body: dict) -> dict:
    signer = c.app.state.internal_issuer.signer
    return jwt.decode(
        body["access_token"],
        signer.public_key(),
        algorithms=["RS256"],
        audience="daegam-mcp",
        issuer=PUBLIC_URL,
    )


def test_an_awake_server_connects_the_person_of_the_record(c, cid, token, mongo):
    """허용은 ID 토큰의 최근 로그인을 본다. 연결의 sub 는 IdP 의 것이고 tv 는 0 이다. 접근 토큰은
    고정 그룹과 규칙이 이름 댄 IdP 그룹, 소속의 역할을 싣고 tv 가 없다. 사람 기록이 그룹을 잃으면
    다음 갱신의 토큰에서 빠진다."""
    awake(c)
    stale = consent(c, "approve", handle_for(c, cid), person(token, DEV, OPS, recent=False))
    assert detail_code(stale) == (403, "reauth_required")
    body = connected(c, cid, person(token, DEV, OPS))
    claims = claims_of(c, body)
    assert claims["sub"] == "p1" and claims["preferred_username"] == "p1@example.com"
    assert claims["groups"] == [f"daegam-{TENANT}", rbac.idp_group(TENANT, OPS)]
    assert claims["daegam_roles"] == [rbac.role_group(TENANT, "developer")]
    assert "tv" not in claims
    grant = grant_of(mongo, body)
    assert (grant["sub"], grant["tv"], grant["username"]) == ("p1", 0, "p1@example.com")
    assert c.get("/me", headers=person(token, DEV, recent=False)).status_code == 200
    r = refresh(c, cid, body["refresh_token"])
    assert r.status_code == 200, r.text
    assert claims_of(c, r.json())["groups"] == [f"daegam-{TENANT}"]


@pytest.mark.parametrize("sub", ["svc:vmcp:gw", "sa:acct-1"])
def test_service_and_account_subjects_cannot_connect(c, cid, token, sub):
    awake(c)
    headers = person(token, DEV, sub=sub)
    assert detail_code(consent(c, "approve", handle_for(c, cid), headers)) == (403, "forbidden")
    assert detail_code(c.get("/me/oauth-grants", headers=headers)) == (403, "forbidden")


def test_a_dormant_server_issues_nothing(c, cid, token, mongo):
    """새 인가는 돌아갈 곳으로 `temporarily_unavailable` 이고, 이미 만든 핸들의 허용은 503 이다.
    토큰 엔드포인트는 503 과 Retry-After 로 답하며 코드를 쓰지 않고 갱신 토큰을 회전하지도 가족을
    끊지도 않는다. 깨어나면 같은 코드와 갱신 토큰이 그대로 통한다. 메타데이터는 내지 않는다."""
    headers = person(token, DEV)
    body = connected(c, cid, headers)
    code = code_for(c, cid, headers, state="s2")
    handle = handle_for(c, cid, state="s3")
    awake(c, False)

    base, query = location(authorize(c, cid))
    assert base == REDIRECT
    assert query == {"error": "temporarily_unavailable", "state": "st-1", "iss": PUBLIC_URL}
    assert detail_code(consent(c, "approve", handle, headers)) == (503, "temporarily_unavailable")
    for r in (exchange(c, cid, code), refresh(c, cid, body["refresh_token"])):
        assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"
        assert int(r.headers["Retry-After"]) > 0 and r.headers["Cache-Control"] == "no-store"
    assert mongo["oauth_codes"].count_documents({"used_at": None}) == 1
    grant = grant_of(mongo, body)
    assert grant["revoked_at"] is None and grant["refresh_generation"] == 0

    root = TestClient(c.app)
    assert root.get("/.well-known/oauth-authorization-server").status_code == 404
    assert "authorization_endpoint" not in root.get("/.well-known/openid-configuration").json()

    awake(c)
    assert exchange(c, cid, code).status_code == 200
    assert refresh(c, cid, body["refresh_token"]).status_code == 200


def test_a_dormant_server_still_revokes_and_disconnects(c, cid, token, mongo):
    """잠든 동안에도 클라이언트의 RFC 7009 회수와 화면의 연결 목록과 끊기는 그대로 된다."""
    headers = person(token, DEV)
    first, second = connected(c, cid, headers), connected(c, cid, headers)
    awake(c, False)
    form = {"token": first["refresh_token"], "client_id": cid}
    assert c.post(REVOKE, data=form).status_code == 200
    assert grant_of(mongo, first)["revoked_reason"] == "client"
    listed = c.get("/me/oauth-grants", headers=headers)
    assert listed.status_code == 200 and len(listed.json()) == 2
    grant_id = grant_of(mongo, second)["_id"]
    assert c.delete(f"/me/oauth-grants/{grant_id}", headers=headers).status_code == 200
    assert grant_of(mongo, second)["revoked_reason"] == "user"


def test_admin_grant_routes_stay_local(c, token, mongo):
    """다른 사람의 연결을 보는 관리자 라우트는 사용자 문서가 있는 local 모드에만 있다."""
    map_groups(mongo, [{"group": "daegam-admins", "tenant": "*", "role": "platform-admin"}])
    root = bearer(token("root", "daegam-admins"))
    assert c.get("/users/p1/oauth-grants", headers=root).status_code == 404
    assert c.delete("/users/p1/oauth-grants/g1", headers=root).status_code == 404
