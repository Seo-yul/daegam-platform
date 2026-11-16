"""개인 토큰의 서명 키 슬롯(store/pat_keys.py, auth/pat_keys.py)과 접근 정리의 슬롯 정리
(services/access_revocation.py) (P32 4절·7절). JWKS 의 두 모양과 503 은 test_signer.py 가, 토큰의
회수는 test_access_revocation.py 가 본다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import partial

import jwt
from fastapi.testclient import TestClient

from daegam.auth import pat_keys
from daegam.auth.pat_keys import JWKS_CACHE_SECONDS, PatKeyRing, slot_jwk, slot_private_key
from daegam.auth.signer import JWKS_PATH, PlatformSigner
from daegam.store.base import utcnow
from daegam.store.pat_keys import BOUND, POOL, MongoPatKeyStore
from tests.helpers.auth import slot_keys

KID = "pat-0123456789abcdef"
OLD_MASTER = "0000000000000000"


def _call(c: TestClient, fn, *args, **kw):
    return c.portal.call(partial(fn, *args, **kw))


def _slot(kid: str, master: str, *, state: str = POOL, age: timedelta = timedelta(0)) -> dict:
    return {"_id": kid, "master": master, "state": state, "created_at": utcnow() - age}


# -- 유도 ------------------------------------------------------------------------------------


def test_slot_key_is_derived_from_the_kid_and_the_signing_key():
    """같은 서명 키와 kid 면 어느 레플리카에서나 같은 키이고, 둘 중 하나가 다르면 다른 키다."""
    signer = PlatformSigner.generate()
    same = PlatformSigner(signer.private_pem())
    assert slot_jwk(signer, KID) == slot_jwk(same, KID)
    assert slot_jwk(signer, KID)["x"] != slot_jwk(signer, "pat-0123456789abcdee")["x"]
    assert slot_jwk(signer, KID)["x"] != slot_jwk(PlatformSigner.generate(), KID)["x"]
    # 쓰임마다 salt 가 다르면 같은 서명 키에서도 다른 재료가 나온다 (P33 의 HMAC 키)
    assert signer.derive(b"a", b"i", 32) != signer.derive(b"b", b"i", 32)
    # 그 키로 서명한 ES256 토큰이 JWKS 의 키로 검증된다. ToolHive 가 하는 그대로다
    token = jwt.encode(
        {"sub": "u-1"}, slot_private_key(signer, KID), algorithm="ES256", headers={"kid": KID}
    )
    key = jwt.PyJWK.from_dict(slot_jwk(signer, KID)).key
    assert jwt.decode(token, key, algorithms=["ES256"])["sub"] == "u-1"


def test_jwks_does_not_read_key_coordinates_from_mongo(local_root, mongo):
    """Mongo 에 쓸 수 있는 사람이 좌표를 심은 슬롯 문서를 넣어도 JWKS 의 키는 kid 에서 유도한
    키다. Mongo 쓰기 권한으로 토큰을 위조하는 길이 없다."""
    signer = local_root.app.state.internal_issuer.signer
    planted = {**_slot(KID, signer.kid), "kty": "EC", "x": "planted", "y": "planted"}
    mongo["pat_keys"].insert_one(planted)
    [slot] = slot_keys(local_root.get(JWKS_PATH).json(), signer.kid)
    assert slot == slot_jwk(signer, KID)


# -- pool 과 JWKS 캐시 ------------------------------------------------------------------------


def test_pool_is_topped_up_to_its_size(local_root, mongo):
    ring = local_root.app.state.pat_keys
    master = ring.issuer.kid
    created = _call(local_root, ring.fill_pool, utcnow())["created"]
    assert len(created) == ring.pool_size and all(k.startswith("pat-") for k in created)
    docs = list(mongo["pat_keys"].find())
    assert {(d["state"], d["master"]) for d in docs} == {(POOL, master)}
    assert _call(local_root, ring.fill_pool, utcnow()) == {"created": []}
    # 발급이 셋을 잡아 가면 셋을 새로 만든다. 다른 서명 키의 슬롯은 세지 않는다
    mongo["pat_keys"].update_many({"_id": {"$in": created[:3]}}, {"$set": {"state": BOUND}})
    mongo["pat_keys"].insert_one(_slot(KID, OLD_MASTER))
    assert len(_call(local_root, ring.fill_pool, utcnow())["created"]) == 3


def test_jwks_is_cached_for_30_seconds_per_replica(local_root, mongo):
    """회수는 슬롯 문서를 지우는 것이고, 레플리카의 캐시가 비면 JWKS 에서 빠진다 (P32 3절)."""
    wiring = local_root.app.state.pat_keys
    now = [0.0]
    ring = PatKeyRing(wiring.issuer, wiring.slots, mints=True, pool_size=2, clock=lambda: now[0])
    kid = ring.issuer.kid
    first = _call(local_root, ring.fill_pool, utcnow())["created"]
    assert len(slot_keys(_call(local_root, ring.jwks), kid)) == 2
    mongo["pat_keys"].delete_one({"_id": first[0]})
    now[0] = JWKS_CACHE_SECONDS - 1
    assert len(slot_keys(_call(local_root, ring.jwks), kid)) == 2
    now[0] = JWKS_CACHE_SECONDS
    assert {k["kid"] for k in slot_keys(_call(local_root, ring.jwks), kid)} == set(first[1:])


def test_concurrent_jwks_requests_assemble_once(local_root, monkeypatch):
    """캐시가 빈 순간 몰린 무인증 요청은 슬롯 읽기와 키 유도를 한 번만 하고 같은 묶음을 받는다."""
    wiring = local_root.app.state.pat_keys
    slots = MongoPatKeyStore(wiring.slots.coll.database)
    ring = PatKeyRing(wiring.issuer, slots, mints=True, pool_size=2)
    _call(local_root, ring.fill_pool, utcnow())
    reads: list[str] = []
    derived: list[str] = []
    kids, derive = slots.kids, pat_keys.slot_jwk

    async def counted_kids(master: str) -> list[str]:
        reads.append(master)
        await asyncio.sleep(0)  # 다른 요청이 끼어들 틈을 준다
        return await kids(master)

    def counted_derive(signer, kid):
        derived.append(kid)
        return derive(signer, kid)

    monkeypatch.setattr(slots, "kids", counted_kids)
    monkeypatch.setattr(pat_keys, "slot_jwk", counted_derive)

    async def burst() -> list[dict]:
        return await asyncio.gather(*(ring.jwks() for _ in range(5)))

    bundles = local_root.portal.call(burst)
    assert len(reads) == 1 and len(derived) == 2
    assert all(b is bundles[0] for b in bundles)


# -- 발급의 슬롯 잡기 -------------------------------------------------------------------------


def test_two_replicas_never_claim_the_same_slot(local_root, mongo):
    master = local_root.app.state.internal_issuer.kid
    for i in range(4):
        mongo["pat_keys"].insert_one(_slot(f"pat-{i:016x}", master, age=timedelta(minutes=30)))
    a = local_root.app.state.pat_keys.slots
    b = MongoPatKeyStore(a.coll.database)
    now = utcnow()

    async def race() -> list:
        claims = [
            store.claim(master, token_id=f"t{i}", expires_at=now + timedelta(days=1), now=now)
            for i, store in enumerate([a, b] * 4)
        ]
        return await asyncio.gather(*claims)

    kids = [slot.id for slot in local_root.portal.call(race)]
    assert len(set(kids)) == 8
    bound = {d["_id"]: d["token_id"] for d in mongo["pat_keys"].find({"state": BOUND})}
    assert set(bound) == set(kids) and len(set(bound.values())) == 8


# -- 접근 정리의 슬롯 정리 --------------------------------------------------------------------


def test_outside_local_mode_the_pool_is_swept_and_never_filled(client, mongo):
    """header·oidc 모드는 pool 을 채우지 않고 남은 pool 슬롯을 지운다. 그 모드의 JWKS 는 RSA 키
    하나다 (P32 9절)."""
    app = client.app
    master = app.state.internal_issuer.kid
    mongo["pat_keys"].insert_many([_slot(KID, master), _slot("pat-00000000000000b1", master)])
    result = client.portal.call(app.state.reconciler.run_once)
    assert sorted(result["access_removed"]) == sorted([KID, "pat-00000000000000b1"])
    assert "pat_pool_created" not in result
    assert mongo["pat_keys"].count_documents({}) == 0


def test_sweep_and_pool_wait_for_the_signing_key(local_root, mongo):
    """서명 키가 없으면 지금 kid 를 모르므로 두 단계 모두 건너뛴다."""
    mongo["pat_keys"].insert_one(_slot(KID, OLD_MASTER))
    local_root.app.state.internal_issuer._signer = None
    result = _call(local_root, local_root.app.state.reconciler.run_once)
    assert "access_removed" not in result and "pat_pool_created" not in result
    assert mongo["pat_keys"].count_documents({}) == 1
