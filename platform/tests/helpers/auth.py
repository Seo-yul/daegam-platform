"""제어면 인증을 거치는 테스트가 함께 쓰는 도우미 (D40, P32).

토큰을 단 헤더(`bearer`), local 모드의 로그인(`login`)과 첫 로그인 비밀번호 바꾸기 건너뛰기
(`settle`), 부트스트랩 관리자의 헤더(`admin_headers`), 플랫폼 JWKS 에서 개인 토큰의 슬롯 키 읽기
(`slot_keys`), oidc 모드의 mock OIDC 토큰(`user_token`)과 그룹 → 역할 매핑 넣기(`map_groups`),
header 모드에서 주체 하나만 바꾸기(`as_principal`)다. oidc 모드의 오래 가는 자격(P53b 6.4절)을
보는 시험은 UI 의 client id 와 공개 주소를 단 설정(`with_ui_client`)과 최근 로그인의 ID 토큰
(`recent_id_token`)을 쓴다. 픽스처는 conftest.py 가 이것으로 만든다.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient

from daegam.api.deps import ID_TOKEN_HEADER, require_user
from daegam.auth.principal import Membership, Principal
from daegam.config import Settings
from tests.helpers.consts import PUBLIC_URL, TENANT_HEADER

# local 인증 모드(D40) 플랫폼의 부트스트랩 관리자 비밀번호다
LOCAL_BOOT_PASSWORD = "bootstrap-password-1234"
# oidc 모드의 UI client id. ID 토큰의 audience 다 (P53b 6.4절)
UI_CLIENT = "daegam-ui"


def with_ui_client(settings: Settings) -> Settings:
    """UI 의 client id 와 공개 주소(https)가 있는 설정. 사람 발급자가 공개 주소이고 인가 서버는
    꺼지지 않는다. oidc 모드면 데이터 플레인이 아직 IdP 를 믿어 잠든다."""
    return dataclasses.replace(settings, oidc_client_id=UI_CLIENT, public_url=PUBLIC_URL)


def recent_id_token(token: Callable[..., str], sub: str, *, age: float = 30) -> dict[str, str]:
    """age 초 전에 IdP 에서 로그인한 sub 의 ID 토큰 헤더. token 은 conftest 의 `token` 픽스처다."""
    id_token = token(sub, aud=UI_CLIENT, extra={"auth_time": int(time.time() - age)})
    return {ID_TOKEN_HEADER: id_token}


def bearer(token: str, *, tenant: str | None = None, **headers: str) -> dict[str, str]:
    """token 을 단 헤더. tenant 를 주면 그 테넌트로 보내는 헤더(TENANT_HEADER)도 단다."""
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return {"Authorization": f"Bearer {token}", **headers}


def login(c: TestClient, username: str, password: str) -> httpx.Response:
    return c.post("/auth/login", json={"username": username, "password": password})


def settle(mongo, username: str) -> None:
    """부트스트랩 관리자와 새로 만든 사용자는 첫 로그인에 비밀번호를 바꿔야 한다(서버가 403 으로
    막는다). 그 규칙 자체를 보는 테스트가 아니면 여기서 그 단계를 건너뛴다."""
    mongo["users"].update_one({"username": username}, {"$set": {"must_change_password": False}})


def admin_headers(c: TestClient, mongo) -> dict[str, str]:
    """local 모드 부트스트랩 관리자의 제어면 헤더."""
    settle(mongo, "admin")
    r = login(c, "admin", LOCAL_BOOT_PASSWORD)
    assert r.status_code == 200, r.text
    return bearer(r.json()["access_token"])


def user_token(mock_client: TestClient, sub: str = "u1", **body) -> str:
    """mock OIDC(mock_client)가 서명한 사용자 토큰. body 는 발급 본문(groups·email·extra 등)이다."""
    r = mock_client.post("/_test/token", json={"sub": sub, **body})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def map_groups(mongo, rows: list[dict[str, str]]) -> None:
    """oidc 모드의 IdP 그룹 → 역할 매핑 rows 를 Mongo 에 바로 넣는다. (group, tenant) 는 유니크라
    이미 있는 짝은 role 만 덮는다. 그래서 매핑을 넣는 픽스처(`oidc`, `oidc_headers`)를 함께 써도
    부딪히지 않는다."""
    for m in rows:
        key = {"group": m["group"], "tenant": m["tenant"]}
        mongo["group_mappings"].update_one(key, {"$set": {"role": m["role"]}}, upsert=True)


def slot_keys(jwks: dict, platform_kid: str) -> list[dict]:
    """플랫폼 JWKS 의 모양을 보고 개인 토큰의 슬롯 키만 돌려준다. `kid` 가 플랫폼 kid 인 RS256 키가
    정확히 하나이고 나머지는 `pat-` 로 시작하는 ES256 키다 (P32 4절)."""
    keys = jwks["keys"]
    assert [k["alg"] for k in keys if k["kid"] == platform_kid] == ["RS256"], keys
    slots = [k for k in keys if k["kid"] != platform_kid]
    assert all(k["kid"].startswith("pat-") and k["alg"] == "ES256" for k in slots), keys
    return slots


@contextmanager
def as_principal(
    client: TestClient, sub: str, role: str, *, tenant: str = "default"
) -> Iterator[None]:
    """이 블록 안의 요청은 tenant(기본 테넌트)에서 그 role 을 가진 sub 가 보낸 것이다. header 모드
    클라이언트에서 주체 하나만 바꿔 볼 때 쓴다. 헤더는 달지 않으므로 다른 테넌트로 보낼 때는 요청에
    X-Daegam-Tenant 를 따로 넘긴다."""
    membership = Membership(tenant=tenant, role=role)
    principal = Principal(sub=sub, memberships=[membership])
    client.app.dependency_overrides[require_user] = lambda: principal
    try:
        yield
    finally:
        client.app.dependency_overrides.pop(require_user)
