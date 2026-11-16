"""접근 정리의 oidc 모드와 서비스 어카운트, 잠든 인가 서버 (P53b 6.1·6.3·6.5·6.6·6.7절).

플랫폼은 oidc 모드에서도 사람 토큰을 내므로 접근 정리는 사람 발급자와 슬롯 pool 을 local 처럼 둔다.
사람은 사용자 문서 대신 사람 기록으로 보고, 마지막 IdP 확인이 긴 한도를 넘으면 `unconfirmed` 다.
토큰의 그룹은 늘 그 테넌트의 고정 그룹이어야 하고(`wrong_group`), 토큰이 실은 IdP 그룹은 사람
기록에 남아 있어야 한다(`groups_changed`). 서비스 어카운트의 토큰은 계정에 맞댄다. 2.0.0-rc1 이
모르는 까닭은 새 필드에 두고 rc1 이 아는 사유를 함께 적어 rc1 의 모델이 문서를 읽는다.

토큰과 연결은 발급 경로(B2) 없이 Mongo 에 바로 넣는다. 사람 기록과 소속 줄은 mock IdP 토큰의
요청이 만든다.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daegam.services.oauth.errors import OAuthError
from daegam.services.oauth.tokens import OAuthTokens
from daegam.store.base import utcnow
from daegam.store.oauth_grants import OAuthGrant
from daegam.store.pat_keys import POOL
from tests.helpers.access import TENANT, connect, cut, plant, revoked, sign_in, sweep
from tests.helpers.auth import bearer

OTHER = "b"


def revoker(c: TestClient) -> Any:
    return c.app.state.membership_service.revoker


def age(mongo, sub: str, by: timedelta) -> None:
    mongo["idp_people"].update_one({"_id": sub}, {"$set": {"confirmed_at": utcnow() - by}})


@pytest.fixture
def people(oidc, token):
    """테넌트 a 의 developer p1 과 테넌트 b 의 developer p2 가 로그인했다."""
    sign_in(oidc, token, "p1", "daegam-a-dev")
    sign_in(oidc, token, "p2", "daegam-b-dev")
    return oidc


def test_a_confirmed_member_keeps_tokens_and_the_long_bound_revokes_them(people, mongo):
    """플랫폼이 oidc 모드에서도 사람 토큰을 내므로 사람 발급자와 pool 은 그대로다. 마지막 IdP
    확인이 긴 한도를 넘으면 `unconfirmed` 이고 rc1 이 아는 `membership` 을 함께 적는다."""
    c = people
    kept = plant(c, mongo, "p1")
    pool = {"_id": "pat-pool000000000001", "master": c.app.state.internal_issuer.kid}
    mongo["pat_keys"].insert_one({**pool, "state": POOL, "created_at": utcnow()})
    assert sweep(c)["revoked"] == [] and revoked(mongo, kept) is None
    assert mongo["pat_keys"].find_one({"_id": pool["_id"]}) is not None
    age(mongo, "p1", timedelta(days=7, minutes=1))
    assert sweep(c)["revoked"] == [kept]
    assert revoked(mongo, kept) == ("membership", "unconfirmed")


def test_no_record_a_cleared_one_or_a_lost_row(people, mongo):
    """기록이 없거나 비운 사람은 `unconfirmed`, 소속 줄이 없는 테넌트는 `membership` 이다. 지운
    계정·막은 계정·비밀번호는 oidc 모드에 없다."""
    c = people
    ghost = plant(c, mongo, "ghost")
    mongo["idp_people"].update_one({"_id": "p2"}, {"$set": {"cleared_at": utcnow()}})
    cleared = plant(c, mongo, "p2", OTHER)
    elsewhere = plant(c, mongo, "p1", OTHER)
    sweep(c)
    assert revoked(mongo, ghost) == revoked(mongo, cleared) == ("membership", "unconfirmed")
    assert revoked(mongo, elsewhere) == ("membership", None)


def test_a_token_must_carry_its_tenants_group_and_groups_still_held(people, token, mongo):
    """토큰의 그룹이 테넌트의 고정 그룹이 아니면 `wrong_group` 이다. 토큰이 실은 IdP 그룹이 사람
    기록에서 빠졌으면 `groups_changed` 이고, 기록이 그룹을 모르면 보지 않는다."""
    c = people
    wrong = plant(c, mongo, "p1", group="daegam-b")
    held = plant(c, mongo, "p1", idp_groups=["daegam-a-dev"])
    gone = plant(c, mongo, "p1", idp_groups=["daegam-a-dev", "left"])
    sweep(c)
    assert revoked(mongo, wrong) == ("membership", "wrong_group")
    assert revoked(mongo, held) is None
    assert revoked(mongo, gone) == ("membership", "groups_changed")
    mongo["idp_people"].update_one({"_id": "p1"}, {"$set": {"groups": None}})
    unknown = plant(c, mongo, "p1", idp_groups=["left"])
    sweep(c)
    assert revoked(mongo, unknown) is None


def test_a_lowered_role_is_role_changed_beside_membership(people, mongo):
    c = people
    lowered = plant(c, mongo, "p1", role="tenant-admin")
    sweep(c)
    assert revoked(mongo, lowered) == ("membership", "role_changed")


class Accounts:
    """서비스 어카운트의 역할. 계정 id → (테넌트, 역할)."""

    def __init__(self, **roles: tuple[str, str]):
        self.roles = roles

    async def role_of(self, account_id: str, tenant: str) -> str | None:
        found = self.roles.get(account_id)
        return found[1] if found and found[0] == tenant else None


def test_service_account_tokens_answer_to_their_account(oidc, mongo):
    """`sa:` 토큰은 사람 대신 계정에 맞댄다. 계정이 없으면 `deleted`, 역할이 내려갔으면
    `role_changed`, 서비스 어카운트를 끄면 rc1 이 아는 `admin` 과 `accounts_off` 다. 소속 줄도
    사람 기록도 보지 않는다."""
    c = oidc
    orphan = plant(c, mongo, "sa:nobody")
    sweep(c)
    assert revoked(mongo, orphan) == ("deleted", None)
    revoker(c).accounts = Accounts(ci=(TENANT, "developer"), low=(TENANT, "viewer"))
    kept = plant(c, mongo, "sa:ci")
    lowered = plant(c, mongo, "sa:low")
    sweep(c)
    assert revoked(mongo, kept) is None
    assert revoked(mongo, lowered) == ("membership", "role_changed")
    revoker(c).accounts_off = True
    sweep(c)
    assert revoked(mongo, kept) == ("admin", "accounts_off")


def test_a_dormant_oauth_server_keeps_connections_and_an_off_one_cuts_them(people, mongo):
    """oidc 모드의 인가 서버는 꺼지지 않았으면 잠들었고, 잠든 서버의 연결은 끊지 않는다. 데이터
    플레인 발급자를 IdP 로 되돌려도 연결이 남게 하려는 것이다 (P53b 6.5절). 꺼진 서버는 끊는다.
    사람 판정은 토큰과 같다."""
    c = people
    # 시험의 oidc 플랫폼은 공개 주소가 없어 서버가 꺼졌다. 공개 주소가 있는 플랫폼처럼 잠든 서버로
    # 둔다 (`Settings.oauth_dormant` 는 test_idp_people.py 가 본다)
    revoker(c).oauth_off = False
    dormant = connect(c, mongo, "p1")
    unconfirmed = connect(c, mongo, "ghost")
    sweep(c)
    assert cut(mongo, dormant) is None
    assert cut(mongo, unconfirmed) == ("membership", "unconfirmed")
    revoker(c).oauth_off = True
    sweep(c)
    assert cut(mongo, dormant) == ("oauth_disabled", None)


def test_header_mode_prunes_the_pool_and_revokes_on_the_issuer(people, mongo):
    """플랫폼이 사람 토큰을 내지 않는 header 모드면 pool 을 지우고 남은 토큰은 발급자가 바뀐
    것이다. 데이터 플레인 발급자와는 무관하다."""
    c = people
    token_id = plant(c, mongo, "p1")
    pool = {"_id": "pat-pool000000000002", "master": c.app.state.internal_issuer.kid}
    mongo["pat_keys"].insert_one({**pool, "state": POOL, "created_at": utcnow()})
    revoker(c).mints = False
    result = sweep(c)
    assert pool["_id"] in result["removed"]
    assert revoked(mongo, token_id) == ("issuer_changed", None)


class _Exposed:
    """연결의 리소스가 테넌트 a 에 노출돼 있다고 답하는 인가 서버의 대역."""

    async def resource(self, _urls: list[str]) -> Any:
        return SimpleNamespace(tenant=TENANT)


def test_a_refresh_in_oidc_mode_answers_to_the_person_record(oidc, token, mongo):
    """oidc 모드의 연결 갱신은 사용자 문서가 아니라 사람 기록을 본다. 이름도 그 기록의 것이다.
    기록이 없으면 `deleted` 가 아니라 `unconfirmed` 로 끊는다 (P53b 6.5절)."""
    c = oidc
    signed_in = c.get("/me", headers=bearer(token("p9", "daegam-a-dev", email="p9@x")))
    assert signed_in.status_code == 200
    revoker(c).oauth_off = False
    tokens = OAuthTokens(
        _Exposed(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        revoker(c).grants,
        revoker(c),
        c.app.state.settings,
        None,  # type: ignore[arg-type]
    )

    def holder_of(grant_id: str) -> Any:
        grant = OAuthGrant.from_doc(mongo["oauth_grants"].find_one({"_id": grant_id}))
        return c.portal.call(tokens._grant_holder, grant).username

    assert holder_of(connect(c, mongo, "p9")) == "p9@x"
    ghost = connect(c, mongo, "ghost")
    with pytest.raises(OAuthError):
        holder_of(ghost)
    assert cut(mongo, ghost) == ("membership", "unconfirmed")
