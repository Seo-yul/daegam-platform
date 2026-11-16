"""Data Service (resource-model.md 7절).

비밀번호는 테넌트 네임스페이스 Secret `ds-{id}` 에만 둔다. Redis 의 TLS CA 는 따로 `ds-{id}-ca` 의
`ca.crt` 에 둔다 (P61 2절). 옛 판이 `ds-{id}` 를 비밀번호와 사용자 이름만으로 다시 쓰므로 한
Secret 에 두면 그 키가 사라진다.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.config import ShippedRedisTls
from daegam.store.base import (
    Document,
    EditClaimStore,
    UtcDatetime,
    new_id,
    set_fields_guarded,
    utcnow,
)

DATA_SERVICES = "data_services"

DataServiceKind = Literal["postgres", "redis"]


# CA Secret 안에서 PEM 을 담는 키. 클라이언트는 이 키 하나만 마운트한다
REDIS_CA_KEY = "ca.crt"


def secret_name_for(ds_id: str) -> str:
    return f"ds-{ds_id}"


def ca_secret_name_for(ds_id: str) -> str:
    """Redis TLS CA 의 Secret 이름 (P61 2절)."""
    return f"ds-{ds_id}-ca"


class RedisTls(BaseModel):
    """Redis 의 TLS 끝점 (P61 2절). 평문 `port` 와 함께 둘 다 열려 있는 동안 기록이 둘을 갖고
    소비자는 쓸 수 있는 쪽을 쓴다. CA PEM 은 `ca_secret` 의 `ca.crt` 에 있다. 뒤의 판이 더할 칸이
    있어도 읽고 그대로 다시 쓴다(배포 스냅샷 `BindingInputs` 가 이 모델을 싣는다)."""

    model_config = ConfigDict(extra="allow")

    port: int
    ca_secret: str
    # `ca.crt` 의 지문(`pem.ca_sha256`). CA 를 쓸 때마다 함께 적고, 바인딩한 소비자의 파드가
    # 이것으로 다시 뜬다(`templates/common.py` `REDIS_CA_ANNOTATION`). 이 칸이 생기기 전에 저장한
    # CA 는 None 이다
    ca_sha256: str | None = None


class ShippedTls(BaseModel):
    """배포가 함께 올린 Redis 의 TLS 끝점을 받은 기록 (P61 5절 3단계, `services/shipped_redis.py`).
    at 은 받은 때, ca_sha256 은 그때 받은 CA PEM 의 SHA-256 이다. 이 기록이 있는데 `tls` 가 없으면
    관리자가 지운 것이라 다시 더하지 않고, 저장된 CA 가 그때 받은 것과 다르면 관리자가 바꾼 것이라
    건드리지 않는다. 뒤의 판이 더할 칸이 있어도 읽고 그대로 다시 쓴다."""

    model_config = ConfigDict(extra="allow")

    at: UtcDatetime
    ca_sha256: str


class DataService(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    cluster_id: str
    kind: DataServiceKind
    host: str
    port: int
    database: str | None = None  # postgres
    db: int | None = None  # redis
    username: str | None = None
    secret_name: str
    # redis 만. 있으면 플랫폼은 늘 이 끝점으로 붙고, Agent·MCP Server 는 쓸 수 있을 때 이것을 쓴다
    tls: RedisTls | None = None
    # 함께 올린 Redis 의 TLS 끝점을 받은 기록. 플랫폼만 쓴다
    shipped_tls: ShippedTls | None = None
    # redis 만. 평문 포트가 닫혀 TLS 끝점만 답한다 (P61 5절 5단계). `port` 는 앞 판이 이 기록을
    # 읽을 수 있게 남지만 아무도 그리로 붙지 않는다. 함께 올린 Redis 에 대해 플랫폼이 기동 때
    # 쓰고(`services/shipped_redis.py`), 주소가 바뀌면 지운다. API 로는 쓰지 않는다
    plain_closed: bool = False
    has_password: bool = False  # Secret 에 password 키가 있는가 (무인증 Redis 는 False)
    # verify 가 알아낸 기능. postgres 는 `pgvector: installed|available|missing` (D32)
    capabilities: dict[str, str] = Field(default_factory=dict)
    verified_at: UtcDatetime | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    @property
    def has_redis_tls(self) -> bool:
        """TLS 끝점을 가진 Redis 인가."""
        return self.kind == "redis" and self.tls is not None

    @property
    def tls_only(self) -> bool:
        """평문 포트가 닫혀 TLS 끝점만 답하는 Redis 인가 (P61 5절 5단계)."""
        return self.has_redis_tls and self.plain_closed


class MongoDataServiceStore(EditClaimStore):
    def __init__(self, db: AsyncDatabase):
        self.coll = db[DATA_SERVICES]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )

    async def create(self, ds: DataService) -> DataService:
        await self.coll.insert_one(ds.to_doc())
        return ds

    async def get(self, ds_id: str, tenant: str | None = None) -> DataService | None:
        q: dict = {"_id": ds_id}
        if tenant is not None:
            q["tenant"] = tenant
        doc = await self.coll.find_one(q)
        return DataService.from_doc(doc) if doc else None

    async def get_by_name(self, tenant: str, name: str) -> DataService | None:
        doc = await self.coll.find_one({"tenant": tenant, "name": name})
        return DataService.from_doc(doc) if doc else None

    async def list(self, tenant: str) -> list[DataService]:
        cursor = self.coll.find({"tenant": tenant}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [DataService.from_doc(d) for d in await cursor.to_list()]

    async def redis_at(self, cluster_id: str, shipped: ShippedRedisTls) -> list[DataService]:
        """그 클러스터에서 함께 올린 Redis 의 평문 주소를 가리키는 Redis Database
        (`ShippedRedisTls.serves`)."""
        cursor = self.coll.find(
            {"cluster_id": cluster_id, "kind": "redis", "port": shipped.port}
        ).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        docs = [DataService.from_doc(d) for d in await cursor.to_list()]
        return [ds for ds in docs if shipped.serves(ds.host, ds.port)]

    async def set_plain_closed(self, ids: Collection[str]) -> int:
        """이 Database 들 가운데 TLS 끝점을 가진 것에 평문 포트가 닫힌 표시를 적는다. 바뀐 수다.
        사람이 고치는 칸이 아니라 `seq` 를 올리지 않는다."""
        res = await self.coll.update_many(
            {"_id": {"$in": list(ids)}, "tls": {"$ne": None}, "plain_closed": {"$ne": True}},
            {"$set": {"plain_closed": True}},
        )
        return res.modified_count

    async def clear_plain_closed(self, keep: Collection[str] = ()) -> int:
        """평문 포트가 닫힌 표시를 keep 밖의 모든 Database 에서 지운다. 바뀐 수다. 닫을 수 있는 것은
        함께 올린 Redis 하나라서 그 주소 밖의 표시는 남지 않는다."""
        res = await self.coll.update_many(
            {"plain_closed": True, "_id": {"$nin": list(keep)}}, {"$set": {"plain_closed": False}}
        )
        return res.modified_count

    async def redis_with_tls(self) -> list[DataService]:
        """TLS 끝점을 가진 Redis Database 모두."""
        cursor = self.coll.find({"kind": "redis", "tls": {"$ne": None}})
        return [DataService.from_doc(d) for d in await cursor.to_list()]

    async def set_ca_sha256(self, ds: DataService, ca_sha256: str) -> bool:
        """CA Secret 에서 읽은 CA 의 지문을 적는다. 읽은 뒤 그 사이 지문이 바뀌었으면(CA 를
        저장했으면) 쓰지 않는다. 사람이 고치는 칸이 아니라 `seq` 를 올리지 않는다."""
        stored = ds.tls.ca_sha256 if ds.tls is not None else None
        res = await self.coll.update_one(
            {"_id": ds.id, "tls": {"$ne": None}, "tls.ca_sha256": stored},
            {"$set": {"tls.ca_sha256": ca_sha256}},
        )
        return res.modified_count == 1

    async def store_result(self, ds: DataService, fields: dict[str, Any]) -> bool:
        """사람이 고친 칸에 기대는 플랫폼의 결과(verify, pgvector 캐시)를 쓴다. 읽은 `seq` 이고
        살아 있는 클레임이 없을 때만이며 `seq` 는 올리지 않는다 (P68 2절)."""
        return await set_fields_guarded(
            self.coll, ds.id, fields, expect_seq=ds._seq, unclaimed=True
        )

    async def delete(self, ds_id: str) -> bool:
        res = await self.coll.delete_one({"_id": ds_id})
        return res.deleted_count == 1
