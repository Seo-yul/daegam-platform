"""인가 요청과 동의 (P33 6절·7절·8절).

인가 요청은 아무것도 저장하지 않고 서명한 요청 핸들로 동의 화면에 간다. 확인하지 않은
client_id·redirect_uri 의 오류는 동의 화면이, 그 뒤의 오류는 redirect_uri 가 받는다. 동의의 허용은
최근 인증, 소속, 상한을 보고 처음으로 핸들 사용 기록과 인가 코드를 저장한다. 노출 리소스는 스토어
빌더로 Mongo 에 바로 넣는다(tests/helpers/docs.py 의 `seed_*`). 테넌트는 빌더의 것(acme)이다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from daegam.auth.oauth_keys import oauth_keys
from daegam.services.oauth.audit import OAuthAudit
from daegam.services.oauth.consent import CONSENT_LIMIT, Approver, token_iat_check
from daegam.services.oauth.tokens import ACCESS_TOKEN_SECONDS
from daegam.store.base import hash_token, utcnow
from daegam.store.leases import LEASES
from daegam.store.oauth_codes import CODE_TTL
from tests.helpers.auth import bearer, login
from tests.helpers.consts import PUBLIC_URL
from tests.helpers.docs import TENANT, seed_agent, seed_gateway, seed_server
from tests.helpers.http import detail_code
from tests.helpers.oauth import (
    CHALLENGE,
    CLAUDE,
    CLAUDE_DOC,
    CONSENT,
    GW_URL,
    OTHER_TEAM,
    REDIRECT,
    FakeWeb,
    Person,
    authorize,
    consent,
    handle_for,
    location,
    person,
    register,
    split,
    stale_headers,
)

# 인증 없이 몰아 보내는 인가 요청 수. 흔한 분당 요청 상한보다 크다
UNAUTHENTICATED_BURST = 200


def resealed(c: TestClient, handle: str, **changes) -> str:
    """핸들의 요청을 바꿔 플랫폼 키로 다시 서명한다."""
    request = c.app.state.oauth_authorizer.open_request(handle)
    seal = oauth_keys(c.app.state.internal_issuer.signer).request
    return dataclasses.replace(request, **changes).seal(seal)


def uses_of(mongo, handle_jti: str) -> list[dict]:
    return list(mongo["oauth_consent_uses"].find({"_id": handle_jti}))


@pytest.fixture
def outsider(oauth_client, oauth_admin, mongo) -> Person:
    return person(oauth_client, oauth_admin, mongo, "bob", OTHER_TEAM)


# -- 인가 요청 (6절) -----------------------------------------------------------------------------


def test_unverified_client_or_redirect_errors_stay_on_the_consent_screen(oauth_client, client_id):
    """확인하지 않은 client_id·redirect_uri 로는 어떤 오류도 보내지 않는다."""
    tampered = client_id[:-2] + ("AA" if not client_id.endswith("AA") else "BB")
    cases = [
        ({"client_id": tampered}, "invalid_client"),
        ({"client_id": None}, "invalid_client"),
        ({"redirect_uri": "https://evil.example/callback"}, "invalid_redirect_uri"),
        ({"redirect_uri": "http://localhost:53112/callback/abc"}, "invalid_redirect_uri"),
        ({"redirect_uri": None}, "invalid_redirect_uri"),
        # 범위 밖의 포트는 등록된 127.0.0.1 과 맞지 않는다. 동의 화면이 포트를 읽다 죽지 않는다
        ({"redirect_uri": "http://127.0.0.1:99999/callback"}, "invalid_redirect_uri"),
        # 돌아갈 곳을 맞대기 전이라 뒤의 오류도 동의 화면으로 간다
        (
            {"redirect_uri": "https://evil.example/cb", "response_type": "token"},
            "invalid_redirect_uri",
        ),
    ]
    for params, code in cases:
        cid = params.pop("client_id", client_id)
        assert location(authorize(oauth_client, cid, **params)) == (CONSENT, {"error": code})


def test_later_errors_go_to_the_redirect_uri_with_state_and_iss(oauth_client, client_id):
    cases = [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"scope": "mcp admin"}, "invalid_scope"),
        ({"resource": None}, "invalid_target"),
        ({"resource": "https://nope.example/mcp"}, "invalid_target"),
        ({"resource": f"{GW_URL}?x=1"}, "invalid_target"),
        ({"resource": f"{GW_URL}#frag"}, "invalid_target"),
        # 정규화한 뒤에도 포트, 평문, 사용자 정보가 다르면 다른 리소스다
        ({"resource": "https://gw.acme.example:8443/mcp"}, "invalid_target"),
        ({"resource": "http://gw.acme.example/mcp"}, "invalid_target"),
        ({"resource": "https://u@gw.acme.example/mcp"}, "invalid_target"),
        ({"request_uri": "urn:x"}, "invalid_request"),
        ({"prompt": "login none"}, "interaction_required"),
    ]
    for params, code in cases:
        base, query = location(authorize(oauth_client, client_id, **params))
        assert base == REDIRECT, (params, base)
        assert query == {"error": code, "state": "st-1", "iss": PUBLIC_URL}, params
    # resource 는 꼭 하나다
    _, query = location(authorize(oauth_client, client_id, ("resource", GW_URL)))
    assert query["error"] == "invalid_target"


def test_the_state_is_url_encoded_and_an_invalid_one_is_not_echoed(oauth_client, client_id):
    odd = "a b&c=d/é"
    _, query = location(authorize(oauth_client, client_id, state=odd, response_type="token"))
    assert query["state"] == odd
    long_state = "s" * 513
    _, query = location(authorize(oauth_client, client_id, state=long_state))
    assert query == {"error": "invalid_request", "iss": PUBLIC_URL}
    _, query = location(authorize(oauth_client, client_id, ("state", "again")))
    assert query == {"error": "invalid_request", "iss": PUBLIC_URL}
    # 512자까지는 받는다
    assert handle_for(oauth_client, client_id, state="s" * 512)


def test_a_host_live_on_two_clusters_is_rejected_with_a_warning(
    oauth_client, client_id, mongo, caplog
):
    """유일성이 넓어지기 전에 생긴 중복이다. 테넌트를 하나로 고를 수 없어 거절한다."""
    seed_gateway(mongo, "gw-a", "dup.acme.example", cluster="c1")
    seed_server(mongo, "mcp-b", "dup.acme.example", cluster="c2")
    with caplog.at_level(logging.WARNING, logger="daegam.oauth.authorize"):
        _, query = location(
            authorize(oauth_client, client_id, resource="https://dup.acme.example/mcp")
        )
    assert query["error"] == "invalid_target"
    assert any("gw-a" in m and "mcp-b" in m for m in caplog.messages), caplog.messages
    # 노출 에이전트는 PRM 을 내지 않는다. 일치가 Agent 하나면 invalid_target 이다
    seed_agent(mongo, "bot-1", "bot.acme.example")
    _, query = location(authorize(oauth_client, client_id, resource="https://bot.acme.example"))
    assert query["error"] == "invalid_target"


def test_authorization_requests_write_nothing(oauth_client, client_id, mongo):
    """인증 없는 엔드포인트는 상태를 두지 않고 상한도 걸지 않는다 (D59, 3절). X-Forwarded-For 를
    믿지 않으므로 어떤 상한이든 전역이 되어 누구나 채워 남의 인가를 막을 수 있기 때문이다. 그래서
    몰아 보낸 인가 요청도 하나같이 302 이고 아무것도 쓰지 않는다."""

    # 시험에서도 켜져 있는 주기 작업(사용량 롤업)이 틱마다 리스를 잡고 놓는다. 인가 요청과
    # 상관없는 쓰기라 세지 않는다
    def counts() -> dict[str, int]:
        names = [n for n in mongo.list_collection_names() if n != LEASES]
        return {n: mongo[n].count_documents({}) for n in names}

    before = counts()
    for i in range(UNAUTHENTICATED_BURST):
        assert authorize(oauth_client, client_id, state=f"s{i}").status_code == 302
    assert counts() == before


# -- 동의 (7절) ----------------------------------------------------------------------------------


def test_the_request_keeps_a_normalized_scope_and_resource_and_inspect_flags_lookalike_names(
    oauth_client, client_id, member, mongo, monkeypatch
):
    """인가 요청의 핸들은 openid 를 버린 scope, 등록한 루프백에 맞춘 리디렉트 주소, 정규화한
    리소스를 싣는다. 동의 화면의 inspect 는 클라이언트와 루프백 포트와 대상을 보이고, 알려진 이름을
    흉내 낸 DCR 이름은 정리한 이름으로 경고한다. CIMD 는 문서를 공개한 호스트만 말한다."""
    r = authorize(oauth_client, client_id, scope="openid mcp offline_access")
    assert r.headers["cache-control"] == "no-store"
    handle = handle_for(oauth_client, client_id, scope="openid mcp offline_access")
    assert handle.startswith("v1.")
    request = oauth_client.app.state.oauth_authorizer.open_request(handle)
    # openid 는 버리고, 등록한 127.0.0.1 에 localhost 의 다른 포트가 통한다
    assert (request.scope, request.redirect_uri, request.resource) == (
        "mcp offline_access",
        REDIRECT,
        GW_URL,
    )
    assert request.state == "st-1" and request.code_challenge == CHALLENGE
    assert request.exp - request.iat == 600 and len(request.jti) == 32
    # 비면 mcp 다
    unscoped = handle_for(oauth_client, client_id, scope=None)
    assert oauth_client.app.state.oauth_authorizer.open_request(unscoped).scope == "mcp"
    # 리소스는 정규화해 적는다
    shouted = handle_for(oauth_client, client_id, resource="HTTPS://GW.ACME.EXAMPLE:443/mcp/")
    assert oauth_client.app.state.oauth_authorizer.open_request(shouted).resource == GW_URL
    r = consent(oauth_client, "inspect", handle, member.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"] == {
        "name": "Script Client",
        "ref": body["client"]["ref"],
        "kind": "dcr",
        "host": None,
        "name_collides": False,
    }
    assert body["client"]["ref"].startswith("dcr:")
    assert body["redirect"] == {"host": "localhost", "port": 53112}
    assert body["member"] is True
    assert body["resource"] == {
        "url": GW_URL,
        "kind": "gateway",
        "name": "gw-1",
        "display_name": "gw-1",
        "tenant": TENANT,
    }
    assert body["scope"] == ["mcp", "offline_access"]
    assert body["access_token_seconds"] == ACCESS_TOKEN_SECONDS == 900
    assert body["grant_days"] == 30
    assert datetime.fromisoformat(body["expires_at"]) == datetime.fromtimestamp(request.exp, UTC)
    # 알려진 이름을 흉내 낸 DCR 이름은 정리한 이름으로 경고한다
    cid = register(oauth_client, "Claude\u200bCode helper")
    body = consent(oauth_client, "inspect", handle_for(oauth_client, cid), member.headers).json()
    assert body["client"]["name"] == "ClaudeCode helper" and body["client"]["name_collides"]
    # CIMD 는 신원이 아니라 문서를 공개한 호스트만 말한다. 알려진 이름이어도 경고 대상이 아니다
    seed_server(mongo, "mcp-1", "calc.acme.example")
    FakeWeb(monkeypatch).serve(CLAUDE, CLAUDE_DOC)
    handle = handle_for(oauth_client, CLAUDE, resource="https://calc.acme.example/mcp")
    body = consent(oauth_client, "inspect", handle, member.headers).json()
    assert body["client"] == {
        "name": "Claude Code",
        "ref": CLAUDE,
        "kind": "cimd",
        "host": "claude.ai",
        "name_collides": False,
    }
    assert body["resource"]["kind"] == "mcp_server"


def test_approve_issues_a_code_once_and_deny_answers_access_denied(
    oauth_client, client_id, member, mongo
):
    handle = handle_for(oauth_client, client_id)
    r = consent(oauth_client, "approve", handle, member.headers)
    assert r.status_code == 200, r.text
    base, query = split(r.json()["redirect_to"])
    code = query.pop("code")
    assert (base, query) == (REDIRECT, {"state": "st-1", "iss": PUBLIC_URL})
    # 평문은 응답에만 있고 sha256 만 저장한다
    stored = mongo["oauth_codes"].find_one({"_id": hash_token(code)})
    assert stored is not None and code not in str(mongo["oauth_codes"].find_one())
    user = mongo["users"].find_one({"_id": member.id})
    assert (stored["sub"], stored["tv"], stored["tenant"], stored["resource"]) == (
        member.id,
        user["token_version"],
        TENANT,
        GW_URL,
    )
    assert (stored["client_id"], stored["redirect_uri"], stored["code_challenge"]) == (
        client_id,
        REDIRECT,
        CHALLENGE,
    )
    assert stored["scope"] == "mcp offline_access" and stored["used_at"] is None
    assert stored["expires_at"] - stored["created_at"] == CODE_TTL
    assert stored["purge_at"] > stored["expires_at"]
    # 같은 핸들은 다시 쓰지 못한다
    for step in ("approve", "deny", "inspect"):
        r = consent(oauth_client, step, handle, member.headers)
        assert detail_code(r) == (409, "request_used"), step
    # 허용의 감사 행은 리소스의 테넌트로 남고, 쓰인 핸들의 허용은 오류 코드로 남는다
    rows = list(mongo["audit_events"].find({"action": "approve_consent"}).sort("ts", 1))
    assert [(e["tenant"], e["sub"], e["error_code"]) for e in rows] == [
        (TENANT, member.id, None),
        (None, member.id, "request_used"),
    ]
    assert code not in str(rows) and handle not in str(rows)
    # 거절
    other = handle_for(oauth_client, client_id, state="st-2")
    r = consent(oauth_client, "deny", other, member.headers)
    assert r.status_code == 200, r.text
    assert split(r.json()["redirect_to"]) == (
        REDIRECT,
        {"error": "access_denied", "state": "st-2", "iss": PUBLIC_URL},
    )
    assert mongo["oauth_codes"].count_documents({}) == 1
    # 사용 기록이 핸들보다 먼저 지워지면 같은 핸들을 다시 쓸 수 있으므로 핸들보다 오래 산다
    request = oauth_client.app.state.oauth_authorizer.open_request(other)
    (use,) = uses_of(mongo, request.jti)
    exp = datetime.fromtimestamp(request.exp, UTC).replace(tzinfo=None)
    assert use["decision"] == "deny" and use["sub"] == member.id
    assert use["expires_at"] > exp


def test_non_members_cannot_approve(oauth_client, client_id, outsider, oauth_admin, mongo):
    """다른 팀(`OTHER_TEAM`)의 멤버가 acme 의 리소스를 허용하면 403 이다. `*` 만 가진 사람도
    그렇다. 오류가 테넌트 이름을 싣지 않는다. 공개 주소만 있으면 누구나 핸들을 만들므로 확인
    화면도 소속이 아닌 사람에게 테넌트와 표시 이름을 주지 않는다."""
    handle = handle_for(oauth_client, client_id)
    for headers in (outsider.headers, oauth_admin):
        body = consent(oauth_client, "inspect", handle, headers).json()
        assert body["member"] is False
        assert body["resource"] == {
            "url": GW_URL,
            "kind": None,
            "name": None,
            "display_name": None,
            "tenant": None,
        }
        r = consent(oauth_client, "approve", handle, headers)
        assert detail_code(r) == (403, "not_a_member"), r.text
        assert TENANT not in r.text
    assert mongo["oauth_consent_uses"].count_documents({}) == 0
    assert mongo["oauth_codes"].count_documents({}) == 0


def test_approve_needs_a_sign_in_from_the_last_ten_minutes(oauth_client, client_id, member, mongo):
    """훔친 제어면 토큰만으로 긴 연결을 만들지 못한다. 오래된 토큰의 허용은 아무것도 쓰지 않고, 새로
    로그인한 토큰으로 같은 핸들을 허용하면 통과한다. 거절은 최근 인증을 묻지 않는다."""
    handle = handle_for(oauth_client, client_id)
    stale = stale_headers(oauth_client, member)
    assert consent(oauth_client, "inspect", handle, stale).status_code == 200
    r = consent(oauth_client, "approve", handle, stale)
    assert detail_code(r) == (403, "reauth_required"), r.text
    assert mongo["oauth_consent_uses"].count_documents({}) == 0
    assert mongo["oauth_codes"].count_documents({}) == 0
    fresh = bearer(login(oauth_client, member.username, member.password).json()["access_token"])
    assert consent(oauth_client, "approve", handle, fresh).status_code == 200
    other = handle_for(oauth_client, client_id)
    assert consent(oauth_client, "deny", other, stale).status_code == 200
    # 만료한 핸들은 다시 로그인해도 쓸 수 없으므로 최근 인증보다 먼저 410 이다
    expired = resealed(oauth_client, handle_for(oauth_client, client_id), exp=int(time.time()) - 1)
    r = consent(oauth_client, "approve", expired, stale)
    assert detail_code(r) == (410, "request_expired"), r.text


def test_parallel_approvals_cannot_pass_the_cap_together(oauth_client, client_id, member, mongo):
    """세기와 넣기가 한 걸음이 아니어도 동시에 온 허용이 함께 상한을 넘지 못한다. 걸린 허용의
    기록은 지워져 핸들을 쓰지 않는다."""
    seeded, room = CONSENT_LIMIT - 2, 2
    now = utcnow()
    recent = {"sub": member.id, "decision": "approve", "created_at": now}
    mongo["oauth_consent_uses"].insert_many(
        [{"_id": f"j{i}", **recent, "expires_at": now + timedelta(hours=1)} for i in range(seeded)]
    )
    handles = [handle_for(oauth_client, client_id, state=f"s{i}") for i in range(room + 3)]
    state = oauth_client.app.state
    user = oauth_client.portal.call(state.local_auth.users.get, member.id)
    approver = Approver(user.id, user.token_version, token_iat_check(time.time()))

    async def all_at_once():
        calls = (state.oauth_consent.approve(h, approver, OAuthAudit()) for h in handles)
        return await asyncio.gather(*calls, return_exceptions=True)

    results = oauth_client.portal.call(all_at_once)
    passed = [r for r in results if isinstance(r, str)]
    assert len(passed) <= room, results
    assert all(r.code == "consent_limit" for r in results if not isinstance(r, str)), results
    approvals = mongo["oauth_consent_uses"].count_documents({"decision": "approve"})
    assert approvals == seeded + len(passed)
    assert mongo["oauth_codes"].count_documents({}) == len(passed)


def test_tampered_and_expired_handles(oauth_client, client_id, member):
    handle = handle_for(oauth_client, client_id)
    prefix, body, mac = handle.split(".")
    flipped = f"{prefix}.{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{mac}"
    for step in ("inspect", "approve", "deny"):
        r = consent(oauth_client, step, flipped, member.headers)
        assert r.status_code == 404, (step, r.text)
    expired = resealed(oauth_client, handle, exp=int(time.time()) - 1)
    for step in ("inspect", "approve", "deny"):
        r = consent(oauth_client, step, expired, member.headers)
        assert detail_code(r) == (410, "request_expired"), step


def test_a_handle_whose_resource_stopped_being_exposed(oauth_client, client_id, member, mongo):
    handle = handle_for(oauth_client, client_id)
    mongo["gateways"].update_one({"_id": "gw-1"}, {"$set": {"exposure.enabled": False}})
    r = consent(oauth_client, "approve", handle, member.headers)
    assert detail_code(r) == (400, "invalid_target"), r.text
