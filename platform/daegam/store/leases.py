"""이름 하나를 놓고 파드들이 다투는 리스다(D30 "공통은 아래 계층에").

레플리카가 여럿일 때 한 번에 하나만 돌아야 하는 일이 여럿이다. reconcile 루프(`mcp-instances`),
감사 수집기(`mcp-audit`), 비서의 턴(`assistant-session:{sid}`·`assistant-turn:{sub}`)이 그것이다.
셋이 같은 컬렉션(`reconcile_leases`)의 문서를 이름으로 나눠 쓴다. MCP Server·Gateway·Agent 문서
하나의 클러스터 작업을 묶는 클레임(`instance:<kind>:<id>`, P66 2.1절)과 Backend API 하나의 버전 쓰기
(`backend-api-version:<id>`)도 여기 산다. 만료 전에는 홀더만 갱신할 수 있고, 같은 홀더가 다시
`acquire` 하면 기한이 늘어난다.

컬렉션 이름은 reconcile 이 처음 쓰던 것을 그대로 둔다. 옮기면 기존 배포본의 리스가 한 번 겹친다.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import anyio
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import ensure_ttl_index, utcnow

logger = logging.getLogger("daegam.leases")

LEASES = "reconcile_leases"


# 리스 하나가 잡힌 채 버티는 시간. 리스 안에서 도는 단계가 이 시간을 넘기면 다른 레플리카가 리스를
# 가져간다
LEASE_TTL = timedelta(seconds=90)


class LeaseStop(BaseException):  # noqa: N818 - CancelledError 처럼 멈춤을 알리는 신호다
    """쥔 리스로 더 일할 수 없다는 신호 (P66 2.3절). `Exception` 이 아니라 `BaseException` 이라
    길 위의 `except Exception` 이 실패로 적지 않고 그대로 지나보낸다. 그 리스를 잡은 자리(클레임
    범위)만 잡는다. `name` 은 멈춘 리스의 이름이고 `outcome` 은 그 자리가 고를 결과다."""

    def __init__(self, name: str, outcome: object = None):
        super().__init__(name, outcome)
        self.name = name
        self.outcome = outcome


class Lease:
    """`reconcile_leases` 문서 하나를 놓고 파드들이 경쟁한다. 만료 전에는 홀더만 갱신할 수 있다."""

    def __init__(self, db: AsyncDatabase, holder: str, *, ttl: timedelta = LEASE_TTL):
        self.coll = db[LEASES]
        self.holder = holder
        self.ttl = ttl

    async def acquire(self, name: str, now: datetime | None = None) -> bool:
        now = now or utcnow()
        try:
            doc = await self.coll.find_one_and_update(
                {"_id": name, "$or": [{"expires_at": {"$lt": now}}, {"holder": self.holder}]},
                {"$set": {"holder": self.holder, "expires_at": now + self.ttl, "renewed_at": now}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # 다른 파드가 만료되지 않은 리스를 쥐고 있어 upsert 가 같은 _id 로 insert 를 시도했다
            return False
        return bool(doc) and doc.get("holder") == self.holder

    async def renew(self, name: str, now: datetime | None = None) -> bool:
        """아직 이 홀더의 리스일 때만 기한을 늘린다. `acquire` 와 달리 그 사이 다른 홀더가 잡았다가
        만료된 리스를 다시 가져오지 않는다. 그 사이 남이 일했다는 뜻이기 때문이다 (P66 2.2절)."""
        now = now or utcnow()
        res = await self.coll.update_one(
            {"_id": name, "holder": self.holder},
            {"$set": {"expires_at": now + self.ttl, "renewed_at": now}},
        )
        return res.matched_count == 1

    async def release(self, name: str) -> None:
        await self.coll.delete_one({"_id": name, "holder": self.holder})


def run_lease(db: AsyncDatabase, pod_id: str, *, ttl: timedelta = LEASE_TTL) -> Lease:
    """실행 하나의 리스. 홀더가 파드 id 와 난수라 같은 파드의 두 실행이 홀더를 나눠 쓰지 않는다.
    채팅 첨부 추출, 문서 클레임(P66), Backend API 의 버전 쓰기가 쓴다."""
    return Lease(db, f"{pod_id}/{uuid.uuid4().hex}", ttl=ttl)


@dataclass(frozen=True)
class HeldLease:
    """`holding_lease` 가 쥔 리스. 기한을 넘겼을 수 있는 일 앞에서 `renew` 로 아직 이 실행의
    리스인지 보고 기한을 늘린다."""

    lease: Lease
    name: str

    async def renew(self) -> bool:
        return await self.lease.renew(self.name)


@contextlib.asynccontextmanager
async def holding_lease(
    db: AsyncDatabase, pod_id: str, name: str, busy: Exception, *, ttl: timedelta = LEASE_TTL
) -> AsyncIterator[HeldLease]:
    """이 실행의 리스 name 을 쥔 범위. 다른 실행이 쥐고 있으면 busy 를 올린다. 나갈 때는 취소를
    막고 놓는다. 놓기가 실패해도 범위의 결과를 가리지 않도록 로그만 남긴다. 놓지 못한 리스는
    만료가 거둔다."""
    lease = run_lease(db, pod_id, ttl=ttl)
    if not await lease.acquire(name):
        raise busy
    try:
        yield HeldLease(lease, name)
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await lease.release(name)
            except Exception as e:  # noqa: BLE001 - 놓지 못한 리스는 만료가 거둔다
                logger.warning("lease %s: release failed: %s", name, e)


async def lease_held(db: AsyncDatabase, name: str, now: datetime | None = None) -> bool:
    """누군가 이 이름의 리스를 만료 전에 쥐고 있는가. 홀더가 누구인지는 보지 않는다."""
    doc = await db[LEASES].find_one(
        {"_id": name, "expires_at": {"$gt": now or utcnow()}}, {"_id": 1}
    )
    return doc is not None


async def init_lease_schema(db: AsyncDatabase) -> None:
    """만료된 리스 문서를 걷는 TTL 색인 (P66 2.4절). 만료된 리스는 이미 비어 있으므로 지워도 답이
    같다. `acquire` 는 upsert 이고 `lease_held` 는 `expires_at` 으로 거른다. 멈춘 파드가 남긴 클레임
    문서가 쌓이지 않게 한다."""
    await ensure_ttl_index(db[LEASES], "expires_at", 0, "ttl_expires_at")


__all__ = [
    "LEASES",
    "HeldLease",
    "Lease",
    "LeaseStop",
    "holding_lease",
    "init_lease_schema",
    "lease_held",
    "run_lease",
]
