"""OAuth 연결과 그 갱신 토큰 가족 (P33 11절).

oauth_grants {_id: grant_id, family_id(유일), sub, username, tv, client_id, client_ref, client_name,
              client_kind, tenant, resource, resource_kind, resource_id, scope, iss, signer_kid,
              refresh_sha256, refresh_generation, refresh_expires_at, prev_refresh_sha256,
              prev_rotated_at, created_at, last_refresh_at, expires_at, revoked_at, revoked_reason,
              revoked_cause?, purge_at}

연결 하나가 갱신 토큰의 가족 하나이고 가족은 연결 문서 안에 산다. 갱신 토큰은
`<family_id>.<secret>` 이고 평문은 응답에만 있다. 문서는 지금 비밀의 sha256 과 세대 번호, 유예를
위한 바로 앞 세대의 해시와 회전 시각만 든다. 쓰인 토큰의 목록은 없다. `family_id` 는 갱신 토큰
안에만 나타나는 난수라, 화면·감사·접근 토큰의 `sid` 에 보이는 연결 id 로는 가족을 찾지 못한다.

`tv` 는 인가 코드의 것(허용 때 읽은 `token_version`)이고 `iss`·`signer_kid` 는 연결을 만들 때의
사람 발급자와 플랫폼 kid 다. 갱신과 접근 정리가 이 셋을 지금 값과 맞댄다. `expires_at` 은 연결의
절대 만료이고 `refresh_expires_at` 은 이 연결로 토큰을 더 받을 수 있는 끝이다. 갱신 토큰 하나의
수명(14일)과 절대 만료 가운데 이른 쪽이고, 갱신 토큰을 내지 않는 연결이면 접근 토큰의 만료다. 그
시각을 넘기면 끝난 연결이다. `last_refresh_at` 은 마지막으로 토큰을 낸 시각이라 만들 때는
`created_at` 이다. 한 사람의 활성 연결 상한은 이 값이 가장 오래된 것부터 퇴출한다.

회수는 `revoked_at`·`revoked_reason` 을 찍고 두 해시를 지운다. 2.0.0-rc1 이 모르는 까닭은
개인 토큰처럼 `revoked_cause` 에 두고 `revoked_reason` 에 rc1 의 사유를 적는다 (P53b 6.5절).
`purge_at` 은 끝나거나 회수된 뒤 30일이고 TTL 인덱스가 그때 지운다. 그동안 목록에 끝남·끊김으로
남는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_ttl_index,
    find_for_sub,
    ids_for_sub,
    revoke_once,
    utcnow,
)
from daegam.store.personal_tokens import RevokeCause, SharedRevokeReason, stored_reason

OAUTH_GRANTS = "oauth_grants"
# 끝나거나 회수된 뒤 목록에 남는 기간
PURGE_AFTER = timedelta(days=30)

# 개인 토큰과 같은 사유(`SharedRevokeReason`)는 `store/personal_tokens.py` 의 상수를 그대로
# 쓴다. 뒤는 연결에만 있는 사유다
GrantRevokeReason = Literal[
    SharedRevokeReason,
    "client",
    "resource_gone",
    "oauth_disabled",
    "client_disallowed",
    "code_reuse",
    "refresh_reuse",
    "evicted",
]
# 회수를 적는 쪽이 넘기는 값. 연결의 사유이거나 rc1 이 모르는 까닭이다
GrantRevocation = GrantRevokeReason | RevokeCause
CLIENT: GrantRevokeReason = "client"  # 클라이언트가 RFC 7009 로 회수했다
RESOURCE_GONE: GrantRevokeReason = "resource_gone"
OAUTH_DISABLED: GrantRevokeReason = "oauth_disabled"
# CIMD client_id 가 허용 목록(DAEGAM_OAUTH_CIMD_CLIENTS)에서 빠졌다
CLIENT_DISALLOWED: GrantRevokeReason = "client_disallowed"
CODE_REUSE: GrantRevokeReason = "code_reuse"
REFRESH_REUSE: GrantRevokeReason = "refresh_reuse"
EVICTED: GrantRevokeReason = "evicted"

# 클라이언트의 갈래 (P33 4절). CIMD 는 허용 목록의 문서 URL 이 client_id 이고 DCR 은 서명한
# 등록 값이다
ClientKind = Literal["cimd", "dcr"]
CIMD: ClientKind = "cimd"
DCR: ClientKind = "dcr"
# 연결할 수 있는 노출 리소스의 종류. 데이터 플레인 소비자 종류(`services/data_plane_auth.py`)의
# 글자다
ResourceKind = Literal["gateway", "mcp_server"]

GrantStatus = Literal["active", "expired", "revoked"]
ACTIVE: GrantStatus = "active"


class OAuthGrant(Document):
    id: str
    family_id: str
    sub: str
    username: str
    tv: int
    client_id: str
    client_ref: str
    client_name: str
    client_kind: ClientKind
    tenant: str
    resource: str
    resource_kind: ResourceKind
    resource_id: str
    scope: str
    iss: str
    signer_kid: str
    # 갱신 토큰을 내지 않는 연결은 None 이다
    refresh_sha256: str | None = None
    refresh_generation: int = 0
    refresh_expires_at: UtcDatetime
    prev_refresh_sha256: str | None = None
    prev_rotated_at: UtcDatetime | None = None
    created_at: UtcDatetime
    last_refresh_at: UtcDatetime
    expires_at: UtcDatetime
    revoked_at: UtcDatetime | None = None
    revoked_reason: GrantRevokeReason | None = None
    revoked_cause: RevokeCause | None = Field(default=None, exclude_if=lambda v: v is None)
    purge_at: UtcDatetime

    @property
    def shown_reason(self) -> GrantRevocation | None:
        """목록 응답의 회수 사유. rc1 이 모르는 까닭이 있으면 그것이고, 없으면 rc1 의 사유다."""
        return self.revoked_cause or self.revoked_reason

    def status(self, now: datetime) -> GrantStatus:
        if self.revoked_at is not None:
            return "revoked"
        if self.refresh_expires_at <= now:
            return "expired"
        return ACTIVE


def _live(now: datetime) -> dict[str, Any]:
    """회수되지 않았고 끝나지 않은 연결."""
    return {"revoked_at": None, "refresh_expires_at": {"$gt": now}}


def _revocation(reason: GrantRevocation) -> dict[str, Any]:
    """`revoked_at` 과 함께 적는 필드. 두 해시를 지우고 30일 뒤에 지워지게 당긴다. rc1 이 모르는
    까닭은 `revoked_cause` 에 둔다."""
    stored, cause = stored_reason(reason)
    fields: dict[str, Any] = {
        "revoked_reason": stored,
        "refresh_sha256": None,
        "prev_refresh_sha256": None,
        "purge_at": utcnow() + PURGE_AFTER,
    }
    if cause is not None:
        fields["revoked_cause"] = cause
    return fields


class MongoOAuthGrantStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[OAUTH_GRANTS]

    async def init_schema(self) -> None:
        await self.coll.create_index([("family_id", ASCENDING)], unique=True, name="uq_family_id")
        await self.coll.create_index(
            [("sub", ASCENDING), ("created_at", DESCENDING)], name="ix_sub_created"
        )
        # 접근 정리가 회수되지 않은 활성 연결을 읽는다
        await self.coll.create_index(
            [("revoked_at", ASCENDING), ("refresh_expires_at", ASCENDING)],
            name="ix_revoked_refresh_expires",
        )
        await ensure_ttl_index(self.coll, "purge_at", 0, "ttl_purge_at")

    async def insert(self, grant: OAuthGrant) -> None:
        await self.coll.insert_one(grant.to_doc())

    async def get(self, grant_id: str) -> OAuthGrant | None:
        doc = await self.coll.find_one({"_id": grant_id})
        return OAuthGrant.from_doc(doc) if doc else None

    async def by_family(self, family_id: str) -> OAuthGrant | None:
        doc = await self.coll.find_one({"family_id": family_id})
        return OAuthGrant.from_doc(doc) if doc else None

    async def list_for_sub(self, sub: str) -> list[OAuthGrant]:
        return await find_for_sub(self.coll, OAuthGrant, sub)

    async def live(self, now: datetime) -> list[OAuthGrant]:
        """모든 활성 연결. 접근 정리가 지금 값과 맞댄다."""
        return [OAuthGrant.from_doc(d) async for d in self.coll.find(_live(now))]

    async def live_ids(self, sub: str, now: datetime, *, tenant: str | None = None) -> list[str]:
        """sub 의 활성 연결 id. tenant 를 주면 그 테넌트의 것만이다."""
        return await ids_for_sub(self.coll, _live(now), sub, tenant=tenant)

    async def beyond(self, sub: str, now: datetime, *, keep: int) -> list[str]:
        """sub 의 활성 연결 가운데 마지막 갱신이 새로운 keep 개를 뺀 나머지의 id."""
        cursor = self.coll.find({**_live(now), "sub": sub}, {"_id": 1})
        cursor = cursor.sort("last_refresh_at", DESCENDING).skip(keep)
        return [d["_id"] async for d in cursor]

    async def rotate(
        self, grant: OAuthGrant, digest: str, *, now: datetime, refresh_expires_at: datetime
    ) -> OAuthGrant | None:
        """지금 세대를 새 비밀로 바꾸고 세대를 하나 올린다. 조건이 세대와 지금 해시를 함께 보므로
        같은 토큰으로 온 두 요청 가운데 하나만 지난다. 진 쪽은 None 이다."""
        doc = await self.coll.find_one_and_update(
            {
                "_id": grant.id,
                "refresh_generation": grant.refresh_generation,
                "refresh_sha256": grant.refresh_sha256,
                "revoked_at": None,
            },
            {
                "$set": {
                    "refresh_sha256": digest,
                    "refresh_generation": grant.refresh_generation + 1,
                    "prev_refresh_sha256": grant.refresh_sha256,
                    "prev_rotated_at": now,
                    "last_refresh_at": now,
                    "refresh_expires_at": refresh_expires_at,
                    "purge_at": refresh_expires_at + PURGE_AFTER,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return OAuthGrant.from_doc(doc) if doc else None

    async def revoke(self, grant_id: str, reason: GrantRevocation) -> bool:
        """연결 하나를 회수한다. 이미 회수된 연결은 처음 사유를 덮지 않는다."""
        return await revoke_once(self.coll, {"_id": grant_id}, _revocation(reason))

    async def revoke_many(self, ids: list[str], reason: GrantRevocation) -> list[OAuthGrant]:
        """ids 가운데 아직 회수되지 않은 연결을 회수하고 그 연결들을 돌려준다."""
        if not ids:
            return []
        cursor = self.coll.find({"_id": {"$in": ids}, "revoked_at": None})
        grants = [OAuthGrant.from_doc(d) async for d in cursor]
        if grants:
            await self.coll.update_many(
                {"_id": {"$in": [g.id for g in grants]}, "revoked_at": None},
                {"$set": {"revoked_at": utcnow(), **_revocation(reason)}},
            )
        return grants
