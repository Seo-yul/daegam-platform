"""런타임 이벤트 수집의 파드별 수 `agent_telemetry` (P37 3절, D62).

파드 uid 마다 문서 하나다. 마지막으로 받은 시각, 받은·다시 온·거절한 이벤트 수와, 봉투가 알린 버린
수와 `correlation_reuse` 를 든다. 뒤의 둘은 파드가 뜬 뒤(부트)의 누적이라 부트 id 마다 가장 큰 값을
둔다. 지표 탭과 수집기(밀어 보내는 파드의 로그는 읽지 않는다)가 읽는다. 토큰을 가진 쪽은 어느 파드
이름이든 댈 수 있으므로 인스턴스 하나는 하루 안에 받은 파드 문서를 `MAX_PODS` 까지 두고, 그 밖의
uid 는 `overflow` 문서 하나에 센다. 하루 넘게 조용한 파드(지나간 롤아웃)와 `overflow` 문서는 상한에
들지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import ensure_ttl_index
from daegam.store.settings import SETTINGS

AGENT_TELEMETRY = "agent_telemetry"
# 마지막으로 받은 뒤 이만큼 지난 파드 문서는 지운다 (P37 16절)
POD_RETENTION = timedelta(days=7)
# 이만큼 안에 밀어 보낸 파드는 밀어 보내는 파드다. 수집기는 그 파드의 로그를 읽지 않고(P37 4절) 지표
# 탭은 수집을 push 로 보인다(11절)
PUSH_FRESHNESS = timedelta(minutes=5)
MAX_PODS = 50
# 이보다 오래 받지 못한 파드 문서는 상한에 들지 않는다
POD_CAP_WINDOW = timedelta(days=1)
OVERFLOW = "overflow"
# 부트 id 는 문서의 칸 이름이 되므로 이 글자만 남긴다
_BOOT_KEY = re.compile(r"[^A-Za-z0-9_-]")


class MongoAgentTelemetryStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_TELEMETRY]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("instance_id", ASCENDING), ("last_at", DESCENDING)], name="ix_instance_last"
        )
        await self.coll.create_index(
            [("pod_uid", ASCENDING), ("last_at", DESCENDING)], name="ix_pod_last"
        )
        await ensure_ttl_index(
            self.coll, "last_at", int(POD_RETENTION.total_seconds()), "ttl_last_at"
        )

    async def record(
        self,
        *,
        instance_id: str,
        tenant: str,
        pod_uid: str,
        pod_name: str,
        boot_id: str,
        counts: dict[str, int],
        reported: dict[str, int],
        now: datetime,
    ) -> None:
        """요청 하나의 수를 더한다. counts 는 이번 요청의 받은·다시 온·거절한 수이고, reported 는
        봉투가 알린 이 부트의 누적(버린 수, correlation_reuse)이다."""
        doc_id = f"{instance_id}:{pod_uid}"
        if await self.coll.find_one({"_id": doc_id}, {"_id": 1}) is None:
            live = {
                "instance_id": instance_id,
                "pod_uid": {"$ne": OVERFLOW},
                "last_at": {"$gte": now - POD_CAP_WINDOW},
            }
            if await self.coll.count_documents(live) >= MAX_PODS:
                doc_id, pod_uid, pod_name = f"{instance_id}:{OVERFLOW}", OVERFLOW, OVERFLOW
        boot = _BOOT_KEY.sub("_", boot_id)[:64] or "unknown"
        await self.coll.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "instance_id": instance_id,
                    "tenant": tenant,
                    "pod_uid": pod_uid,
                    "pod_name": pod_name,
                    "boot_id": boot_id,
                    "last_at": now,
                },
                "$inc": {k: int(v) for k, v in counts.items()},
                "$max": {f"boots.{boot}.{k}": int(v) for k, v in reported.items()},
            },
            upsert=True,
        )

    async def pods(self, instance_id: str, *, since: datetime) -> list[dict[str, Any]]:
        """since 뒤에 받은 파드 문서, 최근 것부터."""
        q = {"instance_id": instance_id, "last_at": {"$gte": since}}
        cursor = self.coll.find(q).sort("last_at", DESCENDING)
        return [d async for d in cursor]

    async def pushed_since(self, pods: list[tuple[str, str]], since: datetime) -> set[str]:
        """(인스턴스 id, 파드 uid) 가운데 since 뒤에 그 인스턴스의 토큰으로 밀어 보낸 파드 uid. 문서
        id 로 맞추므로 다른 Agent 의 토큰이 남의 파드 uid 를 대도 그 파드의 로그 읽기를 멈추지
        못한다."""
        ids = [f"{instance_id}:{uid}" for instance_id, uid in pods]
        if not ids:
            return set()
        cursor = self.coll.find({"_id": {"$in": ids}, "last_at": {"$gte": since}}, {"pod_uid": 1})
        return {d["pod_uid"] async for d in cursor}


TELEMETRY_INGEST_DOC_ID = "telemetry_ingest"


class MongoTelemetryIngestStore:
    """게이트웨이 사용량 수집의 회수 표 (P37 8절). `settings` 컬렉션의 `_id: "telemetry_ingest"`
    문서이고 클러스터마다 `not_before` 를 둔다. 그보다 먼저 낸 토큰은 받지 않는다."""

    def __init__(self, db: AsyncDatabase):
        self.coll = db[SETTINGS]

    async def init_schema(self) -> None:
        return None

    async def not_before(self, cluster_id: str) -> datetime | None:
        doc = await self.coll.find_one({"_id": TELEMETRY_INGEST_DOC_ID}, {"not_before": 1})
        value = ((doc or {}).get("not_before") or {}).get(cluster_id)
        return value if isinstance(value, datetime) else None

    async def received(self, cluster_id: str, at: datetime) -> None:
        """그 게이트웨이의 요청을 받은 때 (P38 11절). 수집 경로가 30초에 한 번까지 쓴다."""
        await self.coll.update_one(
            {"_id": TELEMETRY_INGEST_DOC_ID},
            {"$set": {f"received.{cluster_id}": at}},
            upsert=True,
        )

    async def received_at(self, cluster_id: str) -> datetime | None:
        doc = await self.coll.find_one({"_id": TELEMETRY_INGEST_DOC_ID}, {"received": 1})
        value = ((doc or {}).get("received") or {}).get(cluster_id)
        return value if isinstance(value, datetime) else None

    async def revoke(self, cluster_id: str, at: datetime) -> None:
        await self.coll.update_one(
            {"_id": TELEMETRY_INGEST_DOC_ID},
            {"$set": {f"not_before.{cluster_id}": at}},
            upsert=True,
        )
