"""인가 서버 메타데이터 (P33 2절·15절).

`/.well-known/oauth-authorization-server` 는 RFC 8414 필드를 내고 `openid-configuration` 이 같은
필드에 OIDC 필드를 더해 싣는다. issuer 는 PRM 의 authorization_servers 가 될 글자(MCPOIDCConfig 의
issuer)와 같다. OAuth 가 꺼진 모드와 공개 주소에서는 메타데이터가 404 이고 디스커버리에 인가 서버
필드가 없다. 연결 수명 상한 자르기는 test_settings_env.py 가 본다.
"""

from __future__ import annotations

import dataclasses

import pytest

from daegam.api.oauth import authorization_server_metadata
from daegam.services.tenancy import oidc_kwargs
from tests.helpers.auth import admin_headers
from tests.helpers.http import ROOT_URL, api_client, detail_code, make_app
from tests.helpers.oauth import AUTHORIZE, CONSENT, REGISTER, REGISTERED

METADATA = f"{ROOT_URL}/.well-known/oauth-authorization-server"
DISCOVERY = f"{ROOT_URL}/.well-known/openid-configuration"
# OIDC 디스커버리에만 있는 필드. id_token 은 내지 않는다
OIDC_ONLY = {"subject_types_supported", "id_token_signing_alg_values_supported", "claims_supported"}


def expected(issuer: str) -> dict:
    """2절의 문서 그대로다."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/api/oauth/authorize",
        "token_endpoint": f"{issuer}/api/oauth/token",
        "registration_endpoint": f"{issuer}/api/oauth/register",
        "revocation_endpoint": f"{issuer}/api/oauth/revoke",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp", "offline_access"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def test_cimd_is_not_advertised_without_an_allow_list(offline_oauth_settings):
    """Claude·Codex 는 이 값이 참일 때만 CIMD 로 오고 아니면 DCR 로 간다 (2절)."""
    metadata = authorization_server_metadata(
        dataclasses.replace(offline_oauth_settings, oauth_cimd_clients=())
    )
    assert metadata is not None
    assert metadata["client_id_metadata_document_supported"] is False


OFF = {
    "no public url": {"public_url": None},
    "plain http": {"public_url": "http://daegam.local"},
    "switched off": {"oauth_switch": False},
    "header": {"auth_mode": "header"},
    # IdP 를 믿는 oidc 의 인가 서버는 잠들어 메타데이터를 내지 않는다 (P53b 6.5절)
    "oidc dormant": {
        "auth_mode": "oidc",
        "oidc_issuer": "https://idp.example",
        "data_plane_issuer": "idp",
    },
}


@pytest.mark.parametrize("off", OFF)
def test_nothing_is_advertised_when_oauth_is_off(offline_oauth_settings, off):
    assert (
        authorization_server_metadata(dataclasses.replace(offline_oauth_settings, **OFF[off]))
        is None
    )


def test_the_metadata_is_404_and_discovery_stays_as_before_when_off(
    oauth_settings, mongo, fake_clusters, fake_secrets
):
    """꺼지면 메타데이터가 404 이고 디스커버리는 P32 그대로다. 인가·동의·등록 라우트와 연결 목록
    라우트도 404 다 (15절). 어느 설정이 끄는지는 위의 함수 판정이 모드마다 본다."""
    s = dataclasses.replace(oauth_settings, **OFF["switched off"])
    app = make_app(s, fake_clusters, fake_secrets)
    with api_client(app) as c:
        r = c.get(METADATA)
        assert detail_code(r) == (404, "not_found"), r.text
        assert c.get(AUTHORIZE).status_code == 404
        for step in ("inspect", "approve", "deny"):
            assert c.post(f"{CONSENT}/{step}", json={"request": "x"}).status_code == 404, step
        r = c.post(REGISTER, json={"client_name": "Cursor", "redirect_uris": [REGISTERED]})
        assert detail_code(r) == (404, "not_found"), r.text
        assert app.state.oauth_clients is None
        admin = admin_headers(c, mongo)
        assert c.get("/me/oauth-grants", headers=admin).status_code == 404
        assert c.get("/users/x/oauth-grants", headers=admin).status_code == 404
        doc = c.get(DISCOVERY).json()
        # 연결 카드가 "로그인으로 연결" 을 보일지 이것으로 정한다 (16절·17절)
        assert c.get("/auth/config").json()["oauth"] == {"enabled": False, "dormant": False}
    assert doc["issuer"] == s.platform_human_issuer
    assert doc["response_types_supported"] == []
    assert not {
        "authorization_endpoint",
        "token_endpoint",
        "code_challenge_methods_supported",
    } & set(doc)


def test_the_plain_http_flag_advertises_plain_endpoints(
    oauth_settings, mongo, fake_clusters, fake_secrets
):
    """orbstack e2e 의 평문 허용 플래그. 문서의 주소가 그 평문 공개 주소다 (1절)."""
    plain = "http://daegam.local"
    app = make_app(
        oauth_settings, fake_clusters, fake_secrets, public_url=plain, oauth_allow_http=True
    )
    with api_client(app) as c:
        assert c.get(METADATA).json() == expected(plain)
        # ToolHive 는 PRM 의 authorization_servers 에 MCPOIDCConfig 의 issuer 를 적는다.
        # 클라이언트는 가져온 메타데이터의 issuer 가 그 글자와 다르면 쓰지 않는다 (2절)
        assert oidc_kwargs(c.app.state.settings)["oidc_issuer"] == plain
        # OIDC 문서로 가는 클라이언트도 code_challenge_methods_supported 가 없으면 멈춰야 하므로
        # 두 문서가 같은 필드를 싣는다
        doc = c.get(DISCOVERY).json()
        assert {k: doc[k] for k in expected(plain)} == expected(plain)
        assert set(doc) == set(expected(plain)) | OIDC_ONLY
        assert doc["id_token_signing_alg_values_supported"] == ["RS256"]
        assert c.get("/auth/config").json()["oauth"] == {"enabled": True, "dormant": False}
