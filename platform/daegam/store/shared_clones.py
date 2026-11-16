"""공유 복제본의 기록 (P50 1절, D23).

다른 테넌트가 연 shared 항목(remote·image)을 쓰는 Gateway 는 자기 네임스페이스에 복제본 CR 을 둔다.
기록은 (원본 항목, 클러스터, 네임스페이스)마다 하나이고, 마지막으로 렌더한 원본 정의와 이름을
든다. 재적용의 한 종류라 렌더 번호와 백오프·막힘 칸을 함께 갖는다. 판의 첫 절반(`auth_rev`)은 인증
입력이 아니라 렌더한 원본 정의의 판이다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.auth_reapply import AudienceSwitch, ConsumerAuthState
from daegam.store.base import Document, UtcDatetime, new_id, set_fields_where, utcnow

SHARED_CLONES = "shared_clones"
# 복제본 기록의 살아 있는 상태. 기록은 복제본이 있는 동안만 있다
CLONE_LIVE_STATES: frozenset[str] = frozenset({"created"})


class CloneK8s(BaseModel):
    """재적용이 문서마다 읽는 `k8s.namespace`."""

    namespace: str


class SharedClone(Document, ConsumerAuthState):
    id: str = Field(default_factory=new_id)
    source_item_id: str
    cluster_id: str
    namespace: str
    tenant: str  # 복제본을 쓰는 테넌트. 그 네임스페이스의 Gateway 에서 정한다
    name: str  # 복제본 CR 이름 (`services/hub.py` `shared_clone_name`)
    # 마지막으로 렌더한 원본 정의 (`services/hub.py` `clone_source`)
    source: dict[str, Any]
    # 쓰는 테넌트가 원본의 consume 경계 밖이거나 원본이 없다. 원본 정의를 더 받지 않는다 (D23)
    frozen: bool = False
    # image 복제본이 테넌트 audience 를 놓고 이 기록의 audience 로 검증하게 된 기록 (P50 9절, G9
    # 2단계). 없으면 테넌트 audience 다
    audience_switch: AudienceSwitch | None = None
    state: Literal["created"] = "created"
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)

    @property
    def k8s(self) -> CloneK8s:
        return CloneK8s(namespace=self.namespace)

    @property
    def last_spec_change(self) -> datetime:
        """재적용의 막힘(`AuthBlocked.spec_change`)이 맞대는 값. 렌더한 원본이 바뀔 때마다 바뀌는
        마지막 수정 시각이다. 허브 항목(`HubItem.last_spec_change`)과 같은 자리다."""
        return self.updated_at


class MongoSharedCloneStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SHARED_CLONES]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("source_item_id", ASCENDING), ("cluster_id", ASCENDING), ("namespace", ASCENDING)],
            unique=True,
            name="uq_source_place",
        )
        await self.coll.create_index([("cluster_id", ASCENDING)], name="ix_cluster")

    async def get(self, clone_id: str) -> SharedClone | None:
        doc = await self.coll.find_one({"_id": clone_id})
        return SharedClone.from_doc(doc) if doc else None

    async def at(self, source_item_id: str, cluster_id: str, namespace: str) -> SharedClone | None:
        doc = await self.coll.find_one(_place(source_item_id, cluster_id, namespace))
        return SharedClone.from_doc(doc) if doc else None

    async def of_source(self, source_item_id: str) -> list[SharedClone]:
        """원본 하나의 모든 복제본 기록. 꺼진 플랫폼 fetch 의 복제본을 지울 때 쓴다 (P39b 13절)."""
        cursor = self.coll.find({"source_item_id": source_item_id}).sort("_id", ASCENDING)
        return [SharedClone.from_doc(d) for d in await cursor.to_list()]

    async def live_on_cluster(self, cluster_id: str) -> list[SharedClone]:
        cursor = self.coll.find({"cluster_id": cluster_id}).sort("_id", ASCENDING)
        return [SharedClone.from_doc(d) for d in await cursor.to_list()]

    async def upsert(self, clone: SharedClone, fields: Collection[str]) -> SharedClone:
        """같은 자리의 기록이 있으면 fields 만 고치고, 없으면 clone 으로 만든다. 두 배포가 함께
        만들어도 고유 색인에 걸리지 않고 기록은 하나다. fields 에 정체 칸(`_id`, 자리, 이름)을 넣지
        않는다. 저장된 기록을 돌려준다."""
        doc = clone.to_doc()
        update: dict[str, Any] = {"$setOnInsert": {k: v for k, v in doc.items() if k not in fields}}
        if fields:
            update["$set"] = {k: v for k, v in doc.items() if k in fields}
        saved = await self.coll.find_one_and_update(
            _place(clone.source_item_id, clone.cluster_id, clone.namespace),
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return SharedClone.from_doc(saved)

    async def set_fields(
        self,
        clone_id: str,
        fields: dict[str, Any],
        *,
        expect_state: Collection[str] | None = None,
    ) -> bool:
        """fields 만 `$set` 한다. `expect_state` 가 있으면 저장된 `state` 가 그 안에 있을 때만
        쓴다. 조건에 안 맞거나 기록이 없으면 False 다."""
        return await set_fields_where(self.coll, clone_id, fields, expect_state=expect_state)

    async def delete_unchanged(self, clone_id: str, updated_at: datetime) -> bool:
        """기록을 읽은 뒤 아무도 고치지 않았을 때만 지운다. 그 사이 배포가 기록을 고쳤으면 False
        다."""
        res = await self.coll.delete_one({"_id": clone_id, "updated_at": updated_at})
        return res.deleted_count == 1


def _place(source_item_id: str, cluster_id: str, namespace: str) -> dict[str, str]:
    return {"source_item_id": source_item_id, "cluster_id": cluster_id, "namespace": namespace}
