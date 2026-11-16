"""외부 OIDC 인바운드(D27 F1): 테스트 안 RSA 키로 서명한 JWT 와 discovery·JWKS MockTransport
(idp_fixtures.py).

무토큰·제어면 aud·nbf 미래·모르는 kid 401, 재조회 최소 간격, JWKS 주소(DAEGAM_OIDC_JWKS_URL),
유효 토큰 채팅(세션 소유자 = sub), 본문 user_sub 무시, 타인 세션 403, /metrics 403, /healthz 축소,
allowed_groups·cid 인가, CORS, 비 ASCII Authorization 401, 인스턴스 토큰 경로 회귀.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from runtime.auth import IssuerSettings
from runtime.state import Settings
from tests.app_fixtures import INSTANCE_TOKEN, ORIGIN, bearer, decide, sid_of
from tests.conftest import chat, events_named, parse_sse, run_app
from tests.idp_fixtures import ISSUER, FakeIdp, Signer, external_app

CONTROL_PLANE_AUDIENCE = "daegam-api"


# -- 401 ------------------------------------------------------------------------


async def test_no_token_is_401_with_www_authenticate(support_doc) -> None:
    idp = FakeIdp(Signer())
    async with external_app(support_doc, idp) as running:
        response = await running.client.post("/chat", json={"message": "hi"})
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
        # JWT 모양이 아닌 토큰은 검증기로 가지 않고 unauthorized 로 끝난다(IdP 호출 없음)
        response = await running.client.get("/sessions", headers=bearer("stale-instance-token"))
        assert response.status_code == 401 and response.json()["code"] == "unauthorized"
        assert idp.calls == []


@pytest.mark.parametrize(
    ("claims", "code", "in_message"),
    [
        pytest.param(
            lambda now: {"aud": CONTROL_PLANE_AUDIENCE}, "invalid_token", "audience", id="aud"
        ),
        pytest.param(lambda now: {"nbf": now + 300}, "invalid_token", "", id="nbf-in-future"),
        pytest.param(
            lambda now: {"iat": now - 600, "exp": now - 120}, "token_expired", "", id="expired"
        ),
    ],
)
async def test_bad_claims_are_401(
    support_doc, claims: Callable[[int], dict[str, Any]], code: str, in_message: str
) -> None:
    """제어면 aud, 미래의 nbf, 지난 exp 는 서명이 맞아도 401 이다."""
    signer = Signer()
    async with external_app(support_doc, FakeIdp(signer)) as running:
        token = signer.token(**claims(int(time.time())))
        response = await running.client.get("/sessions", headers=bearer(token))
        assert response.status_code == 401 and response.json()["code"] == code
        assert in_message in response.json()["message"]


async def test_unknown_kid_is_401_and_refetch_is_rate_limited(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        # 첫 유효 토큰이 discovery·JWKS 를 한 번 받는다
        response = await running.client.get("/sessions", headers=bearer(signer.token()))
        assert response.status_code == 200
        assert idp.calls == ["/.well-known/openid-configuration", "/jwks"]

        # 모르는 kid: JWKS 를 한 번 다시 받고도 없으면 401
        other = Signer()
        response = await running.client.get("/sessions", headers=bearer(other.token()))
        assert response.status_code == 401 and response.json()["code"] == "invalid_token"
        assert idp.jwks_calls == 2

        # 30초 안의 두 번째 모르는 kid 는 IdP 를 다시 부르지 않는다
        response = await running.client.get("/sessions", headers=bearer(Signer().token()))
        assert response.status_code == 401
        assert idp.jwks_calls == 2

        # 캐시된 kid 는 그대로 통한다
        response = await running.client.get("/sessions", headers=bearer(signer.token()))
        assert response.status_code == 200 and idp.jwks_calls == 2


async def test_jwks_url_skips_discovery(support_doc) -> None:
    """local 모드의 플랫폼은 DAEGAM_OIDC_JWKS_URL 로 클러스터 안 JWKS 주소를 내려 준다. 런타임은
    디스커버리 없이 그 주소에서 받는다(P32 1절). 발급자와 origin 이 달라도 된다."""
    signer = Signer()
    idp = FakeIdp(signer)
    jwks_url = "http://daegam-platform.daegam-system.svc:9000/jwks"
    async with external_app(support_doc, idp, jwks_url=jwks_url) as running:
        response = await running.client.get("/sessions", headers=bearer(signer.token()))
        assert response.status_code == 200
        assert idp.calls == ["/jwks"]


def test_settings_read_the_jwks_url_from_env(monkeypatch) -> None:
    """JWKS 주소가 있는 첫 발급자는 플랫폼이라 위임 토큰을 이 Agent 에 묶는다 (P53 3.3절)."""
    monkeypatch.setenv("DAEGAM_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("DAEGAM_OIDC_JWKS_URL", "http://p.svc:9000/.well-known/jwks.json")
    oidc = Settings.from_env().oidc
    assert oidc is not None and oidc.jwks_url == "http://p.svc:9000/.well-known/jwks.json"
    assert oidc.is_platform
    monkeypatch.setenv("DAEGAM_OIDC_JWKS_URL", "")
    oidc = Settings.from_env().oidc
    assert oidc is not None and oidc.jwks_url is None and not oidc.is_platform


async def test_jwt_without_verifier_is_401(support_doc) -> None:
    """header 모드 배포처럼 검증기가 없으면 JWT 모양이어도 프록시 경로만 있다."""
    signer = Signer()
    async with run_app(support_doc, settings=Settings(instance_token=INSTANCE_TOKEN)) as running:
        response = await running.client.get("/sessions", headers=bearer(signer.token()))
        assert response.status_code == 401 and response.json()["code"] == "unauthorized"


async def test_non_ascii_authorization_is_401_not_500(support_doc) -> None:
    idp = FakeIdp(Signer())
    async with external_app(support_doc, idp) as running:
        raw = "Bearer s3crét-tökén".encode()
        response = await running.client.get("/sessions", headers={b"authorization": raw})
        assert response.status_code == 401 and response.json()["code"] == "unauthorized"
        # 인스턴스 토큰 경로도 그대로 산다
        response = await running.client.get("/sessions", headers=bearer(INSTANCE_TOKEN))
        assert response.status_code == 200


# -- 외부 경로의 주체 -------------------------------------------------------------


async def test_valid_token_chats_and_owns_session(support_doc, fake_mcp) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        token = signer.token(sub="alice")
        # 외부 본문에는 user_sub 가 없다
        response = await running.client.post(
            "/chat",
            json={"message": '{"tool": "Add", "args": {"a": 2, "b": 5}}'},
            headers=bearer(token),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        sse = parse_sse(response.text)
        done = events_named(sse, "done")[0]
        assert done["text"] == "결과: 7" and fake_mcp.calls["Add"] == 1
        sid = done["session_id"]

        # 세션 소유자는 토큰 sub 다
        listed = (await running.client.get("/sessions", headers=bearer(token))).json()
        assert [s["session_id"] for s in listed["sessions"]] == [sid]
        assert listed["sessions"][0]["user_sub"] == "alice"
        detail = (await running.client.get(f"/sessions/{sid}", headers=bearer(token))).json()
        assert detail["session"]["user_sub"] == "alice"
        start = running.observed("turn_start")[0]
        assert start["user_sub"] == "alice"

        # 같은 세션에 두 번째 턴
        response = await running.client.post(
            "/chat", json={"message": "다시", "session_id": sid}, headers=bearer(token)
        )
        assert response.status_code == 200
        assert events_named(parse_sse(response.text), "done")[0]["turn"] == 2


async def test_external_body_user_sub_is_ignored(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        token = signer.token(sub="alice")
        response, sse = await chat(running, "hi", user_sub="bob", headers=bearer(token))
        assert response.status_code == 200
        sid = sid_of(sse, "done")
        row = (await running.client.get(f"/sessions/{sid}", headers=bearer(token))).json()
        assert row["session"]["user_sub"] == "alice"
        # 레이트리밋·이벤트도 토큰 sub 다
        rate = running.observed("turn_start")[0]
        assert rate["user_sub"] == "alice"
        # bob 의 토큰으로는 그 세션이 보이지 않는다
        bob = signer.token(sub="bob")
        listed = (await running.client.get("/sessions", headers=bearer(bob))).json()
        assert listed == {"sessions": []}


async def test_external_sessions_list_is_forced_to_own_sub(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        alice = signer.token(sub="alice")
        _, sse = await chat(running, "hi", headers=bearer(alice))
        sid = sid_of(sse, "done")
        bob = signer.token(sub="bob")
        # user_sub=alice 를 쿼리로 넣어도 bob 은 자기 것만 본다
        listed = (
            await running.client.get("/sessions", params={"user_sub": "alice"}, headers=bearer(bob))
        ).json()
        assert listed == {"sessions": []}
        listed = (
            await running.client.get(
                "/sessions", params={"user_sub": "alice"}, headers=bearer(alice)
            )
        ).json()
        assert [s["session_id"] for s in listed["sessions"]] == [sid]


async def test_other_sub_gets_403_on_session_pending_and_decisions(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        alice = signer.token(sub="alice")
        _, sse = await chat(
            running, '{"tool": "Add", "args": {"a": 1, "b": 1}}', headers=bearer(alice)
        )
        sid = sid_of(sse, "interrupt")

        bob = signer.token(sub="bob")
        for path in (f"/sessions/{sid}", f"/sessions/{sid}/pending"):
            response = await running.client.get(path, headers=bearer(bob))
            assert response.status_code == 403, path
            assert response.json()["code"] == "not_session_owner"
        # 외부 경로에서는 X-Daegam-Approver 를 무시한다
        response = await running.client.post(
            f"/sessions/{sid}/decisions",
            json={"decisions": [{"type": "approve"}]},
            headers=bearer(bob, **{"X-Daegam-Approver": "1"}),
        )
        assert response.status_code == 403 and response.json()["code"] == "not_session_owner"
        response, _ = await chat(running, "hi", session_id=sid, headers=bearer(bob))
        assert response.status_code == 403
        assert fake_mcp.calls["Add"] == 0

        # 소유자는 pending 을 보고 결정한다
        pending = await running.client.get(f"/sessions/{sid}/pending", headers=bearer(alice))
        assert pending.status_code == 200 and pending.json()["user_sub"] == "alice"
        response = await running.client.post(
            f"/sessions/{sid}/decisions",
            json={"decisions": [{"type": "approve"}]},
            headers=bearer(alice),
        )
        assert response.status_code == 200 and fake_mcp.calls["Add"] == 1
        decision = running.observed("decision")[0]
        assert decision["user_sub"] == "alice" and decision["approver"] is False


async def test_external_metrics_is_403(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        response = await running.client.get("/metrics", headers=bearer(signer.token()))
        assert response.status_code == 403 and response.json()["code"] == "forbidden"
        assert (
            await running.client.get("/metrics", headers=bearer(INSTANCE_TOKEN))
        ).status_code == 200


async def test_healthz_is_reduced_without_instance_token(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        assert (await running.client.get("/healthz")).json() == {"status": "ok"}
        external = (await running.client.get("/healthz", headers=bearer(signer.token()))).json()
        assert external == {"status": "ok"}
        full = (await running.client.get("/healthz", headers=bearer(INSTANCE_TOKEN))).json()
        assert full["status"] == "ok" and full["tools"] == ["Add"] and "greeting" in full
        assert idp.calls == []


# -- 인가 -----------------------------------------------------------------------


async def test_allowed_groups(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(
        support_doc, idp, allowed_groups=("agent-users",), agent_client_ids=("svc-1",)
    ) as running:
        ok = signer.token(sub="alice", groups=["other", "agent-users"])
        assert (await running.client.get("/sessions", headers=bearer(ok))).status_code == 200
        denied = signer.token(sub="bob", groups=["other"])
        response = await running.client.get("/sessions", headers=bearer(denied))
        assert response.status_code == 403 and response.json()["code"] == "forbidden"
        response, _ = await chat(running, "hi", headers=bearer(signer.token(sub="carol")))
        assert response.status_code == 403
        # 등록된 client id 는 그룹 없이 통과한다
        svc = signer.token(sub="svc-1", cid="svc-1")
        assert (await running.client.get("/sessions", headers=bearer(svc))).status_code == 200
        unknown_cid = signer.token(sub="svc-2", cid="svc-2")
        assert (
            await running.client.get("/sessions", headers=bearer(unknown_cid))
        ).status_code == 403


async def test_client_id_is_read_from_the_configured_claim(support_doc) -> None:
    """IdP 가 client id 를 azp 로 실으면(Keycloak) 플랫폼이 그 이름을 내려 주고 런타임이 그것을
    읽는다."""
    signer = Signer()
    async with external_app(
        support_doc,
        FakeIdp(signer),
        allowed_groups=("agent-users",),
        agent_client_ids=("svc-1",),
        client_id_claim="azp",
    ) as running:
        svc = signer.token(sub="svc-1", azp="svc-1")
        assert (await running.client.get("/sessions", headers=bearer(svc))).status_code == 200
        # 다른 클레임에 같은 값을 실어도 통과하지 않는다
        cid_only = signer.token(sub="svc-1", cid="svc-1")
        assert (await running.client.get("/sessions", headers=bearer(cid_only))).status_code == 403


def test_settings_read_the_client_id_claim_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_OIDC_ISSUER", ISSUER)
    assert _oidc_settings().client_id_claim == "cid"
    monkeypatch.setenv("DAEGAM_OIDC_CLIENT_ID_CLAIM", "azp")
    assert _oidc_settings().client_id_claim == "azp"


async def test_empty_allowed_groups_admits_any_authenticated_sub(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp, agent_client_ids=("svc-1",)) as running:
        token = signer.token(sub="nobody", groups=[])
        assert (await running.client.get("/sessions", headers=bearer(token))).status_code == 200


async def test_groups_are_read_from_the_configured_claim(support_doc) -> None:
    """IdP 가 그룹을 realm_access.roles 에 실으면(Keycloak) 플랫폼이 그 이름을 내려 주고 런타임이
    그것을 읽는다. 맨 위 groups 에 같은 그룹을 실어도 판정에 들지 않는다. 그룹 값 앞의 / 는
    플랫폼처럼 벗긴다."""
    signer = Signer()
    async with external_app(
        support_doc,
        FakeIdp(signer),
        allowed_groups=("agent-users",),
        groups_claim="realm_access.roles",
    ) as running:
        member = signer.token(sub="alice", realm_access={"roles": ["agent-users"]})
        assert (await running.client.get("/sessions", headers=bearer(member))).status_code == 200
        full_path = signer.token(sub="carol", realm_access={"roles": ["/agent-users"]})
        response = await running.client.get("/sessions", headers=bearer(full_path))
        assert response.status_code == 200
        groups_only = signer.token(
            sub="bob", groups=["agent-users"], realm_access={"roles": ["other"]}
        )
        response = await running.client.get("/sessions", headers=bearer(groups_only))
        assert response.status_code == 403 and response.json()["code"] == "forbidden"
        # 설정한 클레임이 아예 없어도 맨 위 groups 로 물러나지 않는다(ToolHive 와 다른 점)
        without_claim = signer.token(sub="dave", groups=["agent-users"])
        response = await running.client.get("/sessions", headers=bearer(without_claim))
        assert response.status_code == 403 and response.json()["code"] == "forbidden"


def test_settings_read_the_groups_claim_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("DAEGAM_OIDC_GROUPS_CLAIM", raising=False)
    assert _oidc_settings().groups_claim == "groups"
    monkeypatch.setenv("DAEGAM_OIDC_GROUPS_CLAIM", "realm_access.roles")
    assert _oidc_settings().groups_claim == "realm_access.roles"


def _oidc_settings() -> IssuerSettings:
    oidc = Settings.from_env().oidc
    assert oidc is not None
    return oidc


# -- CORS -----------------------------------------------------------------------


async def test_cors_preflight_and_error_responses(support_doc) -> None:
    support_doc["entry"]["rate_limit"] = {"per_user_per_minute": 1}
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp, cors_origins=(ORIGIN,)) as running:
        preflight = await running.client.options(
            "/chat",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        assert preflight.status_code in (200, 204)
        assert preflight.headers["Access-Control-Allow-Origin"] == ORIGIN
        allow_methods = preflight.headers["Access-Control-Allow-Methods"]
        assert {"GET", "POST", "OPTIONS"} <= {m.strip() for m in allow_methods.split(",")}
        allow_headers = preflight.headers["Access-Control-Allow-Headers"].lower()
        assert "authorization" in allow_headers and "content-type" in allow_headers
        assert preflight.headers["Access-Control-Max-Age"] == "600"
        assert "Access-Control-Allow-Credentials" not in preflight.headers
        assert "origin" in preflight.headers.get("Vary", "").lower()

        # 다른 origin 의 preflight 는 허용되지 않는다
        other = await running.client.options(
            "/chat",
            headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
        )
        assert "Access-Control-Allow-Origin" not in other.headers

        token = signer.token(sub="alice")
        headers = bearer(token, Origin=ORIGIN)
        ok, _ = await chat(running, "hi", headers=headers)
        assert ok.status_code == 200 and ok.headers["Access-Control-Allow-Origin"] == ORIGIN
        # 세션 id 헤더는 브라우저가 첫 턴을 멈추는 데, 내려받기 이름은 세션 파일을 저장하는 데
        # 쓰므로 드러낸다(D65, P36)
        exposed = ok.headers["Access-Control-Expose-Headers"].lower().split(",")
        expected = {
            "retry-after",
            "x-daegam-session-id",
            "x-daegam-turn-id",
            "x-daegam-detached-turn",
            "content-disposition",
        }
        assert {h.strip() for h in exposed} == expected
        assert ok.headers["X-Daegam-Session-Id"]
        # 예외 핸들러가 만든 429 에도 Allow-Origin 과 Retry-After 노출이 붙는다
        limited, _ = await chat(running, "hi", headers=headers)
        assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
        assert limited.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert "origin" in limited.headers.get("Vary", "").lower()
        assert "retry-after" in limited.headers["Access-Control-Expose-Headers"].lower()
        assert "Retry-After" in limited.headers
        assert "Access-Control-Allow-Credentials" not in limited.headers
        # 401 도 마찬가지다
        denied = await running.client.get("/sessions", headers={"Origin": ORIGIN})
        assert denied.status_code == 401
        assert denied.headers["Access-Control-Allow-Origin"] == ORIGIN


async def test_no_cors_headers_when_origins_unset(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        response = await running.client.get(
            "/sessions", headers=bearer(signer.token(), Origin=ORIGIN)
        )
        assert response.status_code == 200
        assert "Access-Control-Allow-Origin" not in response.headers
        preflight = await running.client.options(
            "/chat", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}
        )
        assert "Access-Control-Allow-Origin" not in preflight.headers


# -- 인스턴스 토큰 경로 회귀 ----------------------------------------------------------


async def test_proxy_path_keeps_body_user_sub_and_approver(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        headers = bearer(INSTANCE_TOKEN)
        response, sse = await chat(
            running, '{"tool": "Add", "args": {"a": 1, "b": 1}}', user_sub="alice", headers=headers
        )
        assert response.status_code == 200
        sid = sid_of(sse, "interrupt")
        # 프록시 경로의 승인자 헤더는 그대로 산다
        approver = {**headers, "X-Daegam-Approver": "1"}
        response = await decide(
            running, sid, [{"type": "approve"}], user_sub="bob", headers=approver
        )
        assert response.status_code == 200 and fake_mcp.calls["Add"] == 1
        decision = running.observed("decision")[0]
        assert decision["user_sub"] == "bob" and decision["approver"] is True
        # 프록시 GET 은 쿼리로 다른 사용자를 봐도 막지 않는다(플랫폼이 검사한다)
        response = await running.client.get(
            f"/sessions/{sid}", params={"user_sub": "bob"}, headers=headers
        )
        assert response.status_code == 200
        assert idp.calls == []


async def test_proxy_path_requires_user_sub_in_body(support_doc) -> None:
    signer = Signer()
    idp = FakeIdp(signer)
    async with external_app(support_doc, idp) as running:
        headers = bearer(INSTANCE_TOKEN)
        response = await running.client.post("/chat", json={"message": "hi"}, headers=headers)
        assert response.status_code == 422 and response.json()["code"] == "user_sub_required"
        response = await running.client.post(
            "/chat", json={"message": "hi", "user_sub": ""}, headers=headers
        )
        assert response.status_code == 422 and response.json()["code"] == "user_sub_required"
        response = await running.client.post(
            "/sessions/x/decisions", json={"decisions": [{"type": "approve"}]}, headers=headers
        )
        assert response.status_code == 422 and response.json()["code"] == "user_sub_required"
        # 본문이 JSON 이 아니면 pydantic 의 422 다
        response = await running.client.post(
            "/chat", content=b"not json", headers={**headers, "content-type": "application/json"}
        )
        assert response.status_code == 422
