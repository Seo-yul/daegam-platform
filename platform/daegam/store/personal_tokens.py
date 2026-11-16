"""개인 MCP 토큰 (P32 5절).

personal_tokens {_id: token_id(uuid hex, 토큰의 jti), sub, username, tenant, group, role?,
                 idp_groups?, label, kid, iss, tv, created_at, expires_at, ready_at,
                 revoke_reason, revoke_cause?, revoke_requested_at, revoked_at, revoked_by,
                 purge_at}

토큰 평문도 해시도 저장하지 않는다. 회수가 저장된 값과 맞대는 일이 아니라 그 토큰의 서명 키 슬롯
(`pat_keys`)을 JWKS 에서 빼는 것이기 때문이다. `group` 과 `iss` 는 발급 때의 테넌트 MCP 그룹과 사람
발급자이고, `tv` 는 발급이 비밀번호를 확인할 때 읽은 사용자 문서의 `token_version` 이다. 접근 정리가
이 셋을 지금 값과 맞대 놓친 회수를 잡는다. oidc 모드의 토큰은 `tv` 가 0 이고, `idp_groups` 는 그
토큰이 테넌트 이름을 붙여 싣는 IdP 그룹의 원래 이름이다 (P53b 6.6절). 서비스 어카운트의 토큰도
여기 있고 `sub` 가 `sa:<계정 id>` 다 (6.7절).

2.0.0-rc1 이 모르는 회수 까닭(`RevokeCause`)은 `revoke_cause` 에 두고 `revoke_reason` 에는 rc1 이
아는 사유를 함께 적는다. rc1 으로 되돌린 플랫폼도 이 문서를 읽을 수 있게 하려는 것이다 (P53b
6.5절).

회수는 세 걸음이다. 요청(`revoke_reason`·`revoke_requested_at`·`revoked_by`)을 적고, 슬롯을
지우고, `revoked_at` 을 찍는다. 요청이 적혔고 `revoked_at` 이 없는 동안이 `revoking` 이다.
`purge_at` 은 만료나 회수 뒤 30일이고 TTL 인덱스가 그때 지운다. 그동안 목록에 만료됨·회수됨으로
남는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, cast

from pydantic import Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_ttl_index,
    find_for_sub,
    ids_for_sub,
    revoke_once,
)

PERSONAL_TOKENS = "personal_tokens"
# 만료나 회수 뒤 목록에 남는 기간
PURGE_AFTER = timedelta(days=30)

# 개인 토큰과 OAuth 연결(P33 12절)이 함께 쓰는 회수 사유. 본인·관리자의 회수와, 훅과 접근
# 정리가 같은 사건에서 둘을 함께 끊는 계정·발급자·서명 키 사건이다. 연결의 사유는
# `store/oauth_grants.py` 가 이것에 제 것을 더한다
SharedRevokeReason = Literal[
    "user",
    "admin",
    "disabled",
    "deleted",
    "password",
    "membership",
    "issuer_changed",
    "key_rotated",
]
# API 와 화면이 아는 개인 토큰의 회수 사유. role_changed 는 개인 토큰만의 사유다 (P52 3절). OAuth
# 접근 토큰은 15분 수명이 역할 변경의 한도다. role_changed 는 rc1 이 모르므로 문서에는 까닭으로 둔다
RevokeReason = Literal[SharedRevokeReason, "key_missing", "role_changed"]
# 2.0.0-rc1 이 모르는 회수 까닭. 역할이 내려갔다(P52 3절), 사람의 마지막 IdP 확인이 긴 한도를
# 넘었다, 토큰이 실은 IdP 그룹이 사람 기록에서 빠졌다, 토큰의 그룹이 그 테넌트의 고정 그룹이
# 아니다, 서비스 어카운트를 껐다 (P53b 6.3·6.6·6.7절)
RevokeCause = Literal[
    "role_changed", "unconfirmed", "groups_changed", "wrong_group", "accounts_off"
]
# 회수를 적는 쪽이 넘기는 값. API 가 아는 사유이거나 rc1 이 모르는 까닭이다
Revocation = RevokeReason | RevokeCause
# 회수를 적는 쪽이 쓰는 이름. 타입을 Literal 에 묶어 틀린 글자가 목록 응답 검증까지 가지 않게 한다
USER: RevokeReason = "user"
ADMIN: RevokeReason = "admin"
DISABLED: RevokeReason = "disabled"
DELETED: RevokeReason = "deleted"
PASSWORD: RevokeReason = "password"
MEMBERSHIP: RevokeReason = "membership"
ISSUER_CHANGED: RevokeReason = "issuer_changed"
KEY_ROTATED: RevokeReason = "key_rotated"
KEY_MISSING: RevokeReason = "key_missing"
ROLE_CHANGED: RevokeReason = "role_changed"
UNCONFIRMED: RevokeCause = "unconfirmed"
GROUPS_CHANGED: RevokeCause = "groups_changed"
WRONG_GROUP: RevokeCause = "wrong_group"
ACCOUNTS_OFF: RevokeCause = "accounts_off"
# rc1 이 모르는 까닭마다 `revoke_reason` 에 함께 적는 rc1 의 사유. 사람, 역할, 그룹의 까닭은
# 소속을 잃은 것으로, 서비스 어카운트를 끈 것은 관리자의 회수로 읽힌다
CAUSE_REASONS: dict[RevokeCause, SharedRevokeReason] = {
    "role_changed": MEMBERSHIP,
    UNCONFIRMED: MEMBERSHIP,
    GROUPS_CHANGED: MEMBERSHIP,
    WRONG_GROUP: MEMBERSHIP,
    ACCOUNTS_OFF: ADMIN,
}


def stored_reason(why: str) -> tuple[str, RevokeCause | None]:
    """문서에 적는 (rc1 이 아는 사유, 까닭). rc1 이 아는 사유면 그대로이고 까닭은 None 이다. 개인
    토큰의 사유와 연결의 사유(`store/oauth_grants.py`)가 함께 쓰므로 글자를 받는다. 까닭이 갈음하는
    사유는 둘이 함께 아는 것(`SharedRevokeReason`)이다."""
    if why in CAUSE_REASONS:
        cause = cast(RevokeCause, why)
        return CAUSE_REASONS[cause], cause
    return why, None


TokenStatus = Literal["active", "pending", "revoking", "revoked", "expired"]


class PersonalToken(Document):
    id: str
    sub: str
    username: str
    tenant: str
    group: str
    # 발급 때의 그 테넌트 역할 (P52 3절). 이 칸 전에 낸 토큰은 None 이고 역할 규칙을 지나지 못한다
    role: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 토큰이 `daegam-group:<tenant>:<group>` 으로 싣는 IdP 그룹의 원래 이름 (P53b 6.6절)
    idp_groups: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    label: str
    kid: str
    iss: str
    tv: int
    created_at: UtcDatetime
    expires_at: UtcDatetime
    # 따뜻하지 않은 슬롯을 잡았으면 쓸 수 있는 시각 (P32 4절)
    ready_at: UtcDatetime | None = None
    revoke_reason: RevokeReason | None = None
    # rc1 이 모르는 까닭. 있으면 화면은 revoke_reason 대신 이것을 보인다
    revoke_cause: RevokeCause | None = Field(default=None, exclude_if=lambda v: v is None)
    revoke_requested_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None
    revoked_by: str | None = None
    # 발급 때는 만료 뒤 30일이고 회수를 요청하면 그때부터 30일로 당겨진다
    purge_at: UtcDatetime

    @property
    def shown_reason(self) -> Revocation | None:
        """목록 응답의 회수 사유. rc1 이 모르는 까닭이 있으면 그것이고, 없으면 rc1 의 사유다."""
        return self.revoke_cause or self.revoke_reason

    def status(self, now: datetime) -> TokenStatus:
        if self.revoked_at is not None:
            return "revoked"
        if self.revoke_requested_at is not None:
            return "revoking"
        if self.expires_at <= now:
            return "expired"
        if self.ready_at is not None and self.ready_at > now:
            return "pending"
        return "active"


def _live(now: datetime) -> dict[str, Any]:
    """회수 요청이 없고 만료하지 않은 토큰. 상한이 세고 훅과 접근 정리가 회수하는 것이다.
    `revoked_at` 조건은 요청 조건에 이미 들어 있지만 `ix_revoked_expires` 를 타게 적는다."""
    return {"revoked_at": None, "revoke_requested_at": None, "expires_at": {"$gt": now}}


def _but(query: dict[str, Any], exclude: str | None) -> dict[str, Any]:
    """query 에서 토큰 exclude 를 뺀다. 발급이 문서를 넣은 뒤 자기를 빼고 상한을 다시 센다."""
    return query if exclude is None else {**query, "_id": {"$ne": exclude}}


class MongoPersonalTokenStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[PERSONAL_TOKENS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("sub", ASCENDING), ("created_at", DESCENDING)], name="ix_sub_created"
        )
        await self.coll.create_index([("tenant", ASCENDING)], name="ix_tenant")
        # 접근 정리가 회수되지 않은 활성 토큰을 읽는다
        await self.coll.create_index(
            [("revoked_at", ASCENDING), ("expires_at", ASCENDING)], name="ix_revoked_expires"
        )
        await self.coll.create_index([("kid", ASCENDING)], unique=True, name="uq_kid")
        await ensure_ttl_index(self.coll, "purge_at", 0, "ttl_purge_at")

    async def insert(self, token: PersonalToken) -> None:
        await self.coll.insert_one(token.to_doc())

    async def delete(self, token_id: str) -> None:
        """평문이 나가기 전에 되돌린 발급만 지운다. 나간 토큰은 회수해 목록에 남긴다."""
        await self.coll.delete_one({"_id": token_id})

    async def get(self, token_id: str) -> PersonalToken | None:
        doc = await self.coll.find_one({"_id": token_id})
        return PersonalToken.from_doc(doc) if doc else None

    async def list_for_sub(self, sub: str) -> list[PersonalToken]:
        return await find_for_sub(self.coll, PersonalToken, sub)

    async def list_for_subs(self, subs: list[str]) -> dict[str, list[PersonalToken]]:
        """subs 마다의 토큰, 새것부터. 한 번에 읽는다. 서비스 어카운트의 목록이 쓴다."""
        found: dict[str, list[PersonalToken]] = {sub: [] for sub in subs}
        cursor = self.coll.find({"sub": {"$in": subs}}).sort("created_at", DESCENDING)
        async for d in cursor:
            found[d["sub"]].append(PersonalToken.from_doc(d))
        return found

    async def count_live(
        self, now: datetime, *, sub: str | None = None, exclude: str | None = None
    ) -> int:
        query = _live(now) if sub is None else {**_live(now), "sub": sub}
        return await self.coll.count_documents(_but(query, exclude))

    async def created_since(
        self, sub: str, since: datetime, *, exclude: str | None = None
    ) -> list[datetime]:
        """since 뒤에 sub 이 발급한 토큰의 발급 시각, 오래된 것부터."""
        query = _but({"sub": sub, "created_at": {"$gte": since}}, exclude)
        cursor = self.coll.find(query, {"created_at": 1})
        return [d["created_at"] async for d in cursor.sort("created_at", ASCENDING)]

    async def live_ids(self, sub: str, now: datetime, *, tenant: str | None = None) -> list[str]:
        """회수 요청이 없고 만료하지 않은 sub 의 토큰 id. tenant 를 주면 그 테넌트의 것만이다."""
        return await ids_for_sub(self.coll, _live(now), sub, tenant=tenant)

    async def live(self, now: datetime) -> list[PersonalToken]:
        """회수 요청이 없고 만료하지 않은 모든 토큰. 접근 정리가 지금 값과 맞댄다."""
        return [PersonalToken.from_doc(d) async for d in self.coll.find(_live(now))]

    async def revoking(self) -> list[PersonalToken]:
        """회수 요청이 적혔고 `revoked_at` 이 아직 없는 토큰."""
        cursor = self.coll.find({"revoked_at": None, "revoke_requested_at": {"$ne": None}})
        return [PersonalToken.from_doc(d) async for d in cursor]

    async def request_revoke(
        self, ids: list[str], *, reason: Revocation, by: str, now: datetime
    ) -> list[PersonalToken]:
        """회수의 첫 걸음. ids 가운데 `revoked_at` 이 없는 토큰에 요청을 적고 그 토큰들을
        돌려준다. 이미 요청이 적힌 토큰은 첫 사유를 덮지 않고 함께 돌려준다. 남은 걸음을 끝내야
        하기 때문이다. rc1 이 모르는 까닭은 `stored_reason` 대로 둘로 나눠 적는다."""
        cursor = self.coll.find({"_id": {"$in": ids}, "revoked_at": None})
        tokens = [PersonalToken.from_doc(d) async for d in cursor]
        if tokens:
            stored, cause = stored_reason(reason)
            fields: dict[str, Any] = {
                "revoke_reason": stored,
                "revoke_requested_at": now,
                "revoked_by": by,
            }
            if cause is not None:
                fields["revoke_cause"] = cause
            await self.coll.update_many(
                {"_id": {"$in": [t.id for t in tokens]}, "revoke_requested_at": None},
                {"$set": fields, "$min": {"purge_at": now + PURGE_AFTER}},
            )
        return tokens

    async def mark_revoked(self, token_id: str) -> bool:
        """회수의 셋째 걸음. 처음 찍은 `revoked_at` 은 덮지 않는다."""
        return await revoke_once(self.coll, {"_id": token_id})

    async def revoked_flags(self, ids: list[str]) -> dict[str, bool]:
        """있는 토큰 id → 회수됐는가. 없는 id 는 빠진다. 접근 정리가 주인 없는 슬롯을 가른다."""
        cursor = self.coll.find({"_id": {"$in": ids}}, {"revoked_at": 1})
        return {d["_id"]: d.get("revoked_at") is not None async for d in cursor}
