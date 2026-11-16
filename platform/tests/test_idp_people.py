"""oidc 모드의 사람 기록 (P53b 6.3절, `store/idp_people.py`).

기록은 플랫폼이 검증한 IdP 액세스 토큰에서만 배운다. `confirmed_at` 은 가장 큰 `iat` 이고 뒤로
가지 않으며, groups 클레임이 없는 토큰은 저장된 그룹을 건드리지 않는다. 기록은 지우지 않고 긴
한도와 30일 뒤에 그룹과 이메일만 비운다. 요청 경로(`_oidc_principal`)와 Settings 의 두 규칙도
여기서 본다.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import timedelta
from typing import Any

from daegam.store.base import from_epoch, utcnow
from daegam.store.idp_people import CLEAR_AFTER, IDP_PEOPLE, MongoIdpPeopleStore
from tests.helpers.aio import run_store
from tests.helpers.auth import bearer

WEEK = timedelta(days=7)
T0 = 1_800_000_000  # 2027-01-15. 시험의 기준 iat 다


def _observe(settings, *tokens: dict[str, Any]) -> None:
    """tokens 를 차례로 적는다. 각 토큰은 observe 의 인자에 sub 를 더한 것이다."""

    async def go(store: MongoIdpPeopleStore) -> None:
        for t in tokens:
            kw = {
                "exp": t["iat"] + 300 if t.get("iat") else None,
                "username": "alice",
                "email": None,
                "groups": None,
                **t,
            }
            await store.observe(kw.pop("sub", "u1"), **kw)

    run_store(settings, MongoIdpPeopleStore, go)


def _doc(mongo, sub: str = "u1") -> dict[str, Any] | None:
    return mongo[IDP_PEOPLE].find_one({"_id": sub})


def test_the_first_token_makes_the_record_from_the_idp_time(settings, mongo):
    _observe(settings, {"iat": T0, "exp": T0 + 3600, "email": "a@x", "groups": ["b", "a", "b"]})
    doc = _doc(mongo)
    assert doc["confirmed_at"] == from_epoch(T0) and doc["token_life"] == 3600
    assert (doc["username"], doc["email"], doc["groups"]) == ("alice", "a@x", ["a", "b"])
    # 새 기록은 매핑 리비전 0 에서 시작한다. 다시 풀 사람을 리비전 색인으로 찾는다
    assert doc["mapping_rev"] == 0 and "cleared_at" not in doc


def test_a_newer_iat_confirms_only_a_minute_later(settings, mongo):
    """같은 토큰을 다시 쓰거나 1분 안에 새로 받은 토큰은 확인을 다시 적지 않는다."""
    _observe(settings, {"iat": T0, "groups": ["a"]})
    before = _doc(mongo)
    _observe(settings, {"iat": T0, "groups": ["a"]}, {"iat": T0 + 59, "groups": ["a"]})
    assert _doc(mongo) == before
    _observe(settings, {"iat": T0 + 60, "exp": T0 + 660, "groups": ["a"]})
    assert _doc(mongo)["confirmed_at"] == from_epoch(T0 + 60)


def test_confirmed_at_never_moves_back_and_old_groups_never_return(settings, mongo):
    _observe(settings, {"iat": T0 + 600, "groups": ["new"]})
    _observe(settings, {"iat": T0, "groups": ["old"]})
    doc = _doc(mongo)
    assert doc["confirmed_at"] == from_epoch(T0 + 600) and doc["groups"] == ["new"]


def test_changed_groups_are_written_within_the_minute(settings, mongo):
    _observe(settings, {"iat": T0, "groups": ["a"]}, {"iat": T0 + 10, "groups": ["a", "c"]})
    doc = _doc(mongo)
    assert doc["groups"] == ["a", "c"] and doc["confirmed_at"] == from_epoch(T0)


def test_a_token_without_groups_leaves_them_and_one_without_iat_writes_nothing(settings, mongo):
    """groups 클레임이 없는 토큰은 그룹을 모르는 것이다. 처음부터 그런 토큰만 본 사람은 None 이고,
    그룹을 본 사람은 그대로다. iat 가 없는 토큰은 무엇도 보증하지 않는다."""
    _observe(settings, {"iat": T0, "groups": None})
    assert _doc(mongo)["groups"] is None
    _observe(settings, {"iat": T0 + 60, "groups": ["a"]}, {"iat": T0 + 120, "groups": None})
    doc = _doc(mongo)
    assert doc["groups"] == ["a"] and doc["confirmed_at"] == from_epoch(T0 + 120)
    _observe(settings, {"sub": "u2", "iat": None, "groups": ["a"]})
    assert _doc(mongo, "u2") is None


def test_a_cleared_record_stays_and_comes_back_on_the_next_confirmation(settings, mongo):
    """지금의 긴 한도와 30일이 지나면 그룹(모르는 것, None)과 이메일을 비우고 기록은 남긴다. 한도를
    늘리면 늘린 한도로 센다. 다시 확인되면 돌아오고, 그 토큰에 그룹이 없으면 그룹은 모른다."""
    old = int(time.time()) - int((WEEK + CLEAR_AFTER).total_seconds()) - 60
    _observe(settings, {"iat": old, "email": "a@x", "groups": ["a"]})
    _observe(settings, {"sub": "u2", "iat": int(time.time()), "groups": ["b"]})

    def clear(bound: timedelta) -> dict[str, list[str]]:
        async def go(store: MongoIdpPeopleStore) -> dict[str, list[str]]:
            return await store.clear_expired(utcnow(), long_bound=bound)

        return run_store(settings, MongoIdpPeopleStore, go)

    assert clear(timedelta(days=60)) == {}
    assert clear(WEEK) == {"cleared": ["u1"]}
    doc = _doc(mongo)
    assert (doc["groups"], doc["email"]) == (None, None) and doc["cleared_at"] is not None
    assert clear(WEEK) == {}
    _observe(settings, {"iat": int(time.time()), "groups": None})
    doc = _doc(mongo)
    assert doc["groups"] is None and "cleared_at" not in doc


def test_each_oidc_request_records_the_person(oidc, token, mongo):
    """요청 경로가 기록을 적는다. 이름은 preferred_username, 없으면 이메일이다."""
    t = token("p1", "daegam-a-dev", email="p1@x", extra={"preferred_username": "pat"})
    assert oidc.get("/me", headers=bearer(t)).status_code == 200
    doc = _doc(mongo, "p1")
    assert (doc["username"], doc["email"], doc["groups"]) == ("pat", "p1@x", ["daegam-a-dev"])
    assert abs(doc["confirmed_at"] - utcnow()) < timedelta(seconds=5)
    assert doc["token_life"] == 600


def test_local_auth_config_keeps_its_values(local_client):
    """local 모드에도 인가 서버의 잠듦이 있고(늘 거짓) oidc 의 값은 없다. oidc 모드의 값 전체는
    test_auth_api.py 가 본다."""
    local = local_client.get("/auth/config").json()
    assert local["oauth"] == {"enabled": False, "dormant": False}
    assert "data_plane_issuer" not in local and "idp_confirm_seconds" not in local


def test_settings_split_minting_from_trust(settings):
    """플랫폼은 local 과 oidc 모드에서 사람 토큰을 내고, 데이터 플레인은 local 모드에서만 그것을
    믿는다(P53b 6.1절). oidc 모드의 사람 발급자는 local 처럼 공개 주소다. 인가 서버는 oidc 모드에서
    꺼지지 않으면 잠든다."""
    public = "https://daegam.example"
    oidc = dataclasses.replace(
        settings,
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_audience="api",
        public_url=public,
        data_plane_issuer="idp",
    )
    local = dataclasses.replace(oidc, auth_mode="local")
    header = dataclasses.replace(oidc, auth_mode="header")
    assert [s.platform_mints_human_tokens for s in (local, oidc, header)] == [True, True, False]
    assert [s.data_plane_trusts_platform for s in (local, oidc, header)] == [True, False, False]
    assert oidc.platform_human_issuer == local.platform_human_issuer == public
    assert header.platform_human_issuer == header.platform_issuer_url
    assert (oidc.oauth_off_reason, oidc.oauth_dormant, oidc.oauth_enabled) == (None, True, False)
    assert (local.oauth_dormant, local.oauth_enabled) == (False, True)
    off = dataclasses.replace(oidc, oauth_switch=False)
    assert off.oauth_off_reason is not None and not off.oauth_dormant
    assert header.oauth_off_reason is not None and not header.oauth_dormant
