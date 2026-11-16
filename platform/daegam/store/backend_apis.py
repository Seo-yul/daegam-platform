"""Backend API 모델(BackendAPI, BackendAPIVersion, BackendAPIToken)과
BackendAPIStore 프로토콜, MongoDB 구현. 본문 파일은 files.py가 맡고 여기는 메타만 다룬다."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    fields_of,
    new_id,
    revoke_once,
    save_by_seq,
    utcnow,
)

AuthKind = Literal["none", "basic", "wsse"]
AUTH_KINDS: tuple[AuthKind, ...] = ("none", "basic", "wsse")


class BackendAPI(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    endpoint: str | None = None
    auth: AuthKind = "none"
    credential_secret: str | None = None  # K8s Secret 이름만. 값은 보관하지 않는다
    # WSDL 이 선언한 soap:header 의 값을 `soap_headers` 키(JSON 객체)에 담은 K8s Secret 이름. 값은
    # 보관하지 않는다 (T60)
    soap_headers_secret: str | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    @property
    def has_credentials(self) -> bool:
        """자격증명 Secret 을 렌더하는 backend 인가. auth 가 none 이 아니고 Secret 이름이 있다."""
        return self.auth != "none" and bool(self.credential_secret)


class ToolDetail(BaseModel):
    """OpenAPI 변환 결과에서 뽑은 Tool 별 원본 메타 (UI 노출용)."""

    model_config = ConfigDict(extra="ignore")

    operation_id: str
    summary: str | None = None
    description: str | None = None
    # OpenAPI 메서드(대문자). 변환할 때 변환 스펙에서 읽는다. 없으면 null 이고 그 Tool 을 write 로
    # 본다
    method: str | None = None
    # FastMCP 입력 스키마의 {점 경로: 형식} (P52 1절, `pipeline/tool_arguments.py`). 인자 조건의
    # 검증이 읽는다. 이 칸 전의 버전이나 FastMCP 가 읽지 못한 스펙은 None 이고 조건을 둘 수 없다
    arguments: dict[str, str] | None = None


class VersionOrigin(BaseModel):
    """다듬기(apply-draft)로 만든 버전의 출처 (D25). 업로드로 만든 버전은 origin 이 None 이다."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["refine", "minify"]
    from_version: int
    job_id: str | None = None  # refine 일 때 Spec Generator job
    presets: list[str] = Field(default_factory=list)  # refine 프리셋


class BackendAPIVersion(Document):
    backend_profile_id: str
    version: int
    kind: str
    source_format: str
    source_ext: str
    source_sha256: str
    output_sha256: str
    s2o_version: str
    tool_count: int
    tool_names: list[str] = Field(default_factory=list)
    # 예전 버전 문서에는 없다. 그 경우 빈 목록으로 읽힌다
    tool_details: list[ToolDetail] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)
    origin: VersionOrigin | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)


class BackendAPIToken(Document):
    id: str = Field(default_factory=new_id)
    backend_profile_id: str
    token_sha256: str
    label: str | None = None
    scope: list[str] = Field(
        default_factory=list
    )  # 예: "mcp-profile:{id}" → 그 profile.json 접근 허용
    created_at: UtcDatetime = Field(default_factory=utcnow)
    revoked_at: UtcDatetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@runtime_checkable
class BackendAPIStore(Protocol):
    async def init_schema(self) -> None: ...
    async def close(self) -> None: ...

    # profiles
    async def create(self, profile: BackendAPI) -> BackendAPI: ...
    async def save_edit(self, profile: BackendAPI, names: set[str]) -> bool: ...
    async def get(self, profile_id: str, tenant: str | None = None) -> BackendAPI | None: ...
    async def get_by_name(self, tenant: str, name: str) -> BackendAPI | None: ...
    async def list(self, tenant: str) -> list[BackendAPI]: ...
    async def delete(self, profile_id: str) -> None: ...

    # versions
    async def add_version(self, version: BackendAPIVersion) -> BackendAPIVersion: ...
    async def latest_version(self, profile_id: str) -> BackendAPIVersion | None: ...
    async def latest_versions(
        self, profile_ids: Collection[str]
    ) -> dict[str, BackendAPIVersion]: ...
    async def list_versions(self, profile_id: str) -> list[BackendAPIVersion]: ...
    async def get_version(self, profile_id: str, version: int) -> BackendAPIVersion | None: ...
    async def get_versions(
        self, refs: Collection[tuple[str, int]]
    ) -> dict[tuple[str, int], BackendAPIVersion]: ...

    # tokens
    async def create_token(
        self,
        profile_id: str,
        token_sha256: str,
        label: str | None,
        scope: list[str] | None = None,
    ) -> BackendAPIToken: ...
    async def get_token(self, profile_id: str, token_id: str) -> BackendAPIToken | None: ...
    async def list_tokens(
        self, profile_id: str, *, active_only: bool = False
    ) -> list[BackendAPIToken]: ...
    async def revoke_token(self, profile_id: str, token_id: str) -> bool: ...
    async def revoke_token_by_id(self, token_id: str) -> bool: ...
    async def revoke_tokens_labeled(self, label: str, scope: str) -> int: ...


APIS = "backend_apis"
VERSIONS = "backend_api_versions"
TOKENS = "backend_api_tokens"


class MongoBackendAPIStore:
    def __init__(self, mongo_url: str, db_name: str):
        self.client: AsyncMongoClient = AsyncMongoClient(
            mongo_url, uuidRepresentation="standard", serverSelectionTimeoutMS=5000
        )
        self.db: AsyncDatabase = self.client[db_name]
        self.apis = self.db[APIS]
        self.versions = self.db[VERSIONS]
        self.tokens = self.db[TOKENS]

    async def init_schema(self) -> None:
        await self.apis.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )
        await self.versions.create_index(
            [("backend_profile_id", ASCENDING), ("version", ASCENDING)],
            unique=True,
            name="uq_profile_version",
        )
        await self.tokens.create_index(
            [("token_sha256", ASCENDING)], unique=True, name="uq_token_sha256"
        )
        await self.tokens.create_index([("backend_profile_id", ASCENDING)], name="ix_profile_id")

    async def close(self) -> None:
        await self.client.close()

    async def drop_database(self) -> None:
        await self.client.drop_database(self.db.name)

    # -- profiles ----------------------------------------------------------

    async def create(self, profile: BackendAPI) -> BackendAPI:
        await self.apis.insert_one(profile.to_doc())
        return profile

    async def save_edit(self, profile: BackendAPI, names: set[str]) -> bool:
        """사람의 저장 (P68 2절). 서버가 읽은 `seq` 일 때만 names 칸만 쓰고 `seq` 를 올린다. 맞지
        않으면 False 다. 문서 전체를 쓰는 저장은 없다."""
        return await save_by_seq(self.apis, profile, fields_of(profile, names))

    async def get(self, profile_id: str, tenant: str | None = None) -> BackendAPI | None:
        query: dict[str, Any] = {"_id": profile_id}
        if tenant is not None:
            query["tenant"] = tenant
        doc = await self.apis.find_one(query)
        return BackendAPI.from_doc(doc) if doc else None

    async def get_by_name(self, tenant: str, name: str) -> BackendAPI | None:
        doc = await self.apis.find_one({"tenant": tenant, "name": name})
        return BackendAPI.from_doc(doc) if doc else None

    async def list(self, tenant: str) -> list[BackendAPI]:
        cursor = self.apis.find({"tenant": tenant}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [BackendAPI.from_doc(d) for d in await cursor.to_list()]

    async def delete(self, profile_id: str) -> None:
        await self.tokens.delete_many({"backend_profile_id": profile_id})
        await self.versions.delete_many({"backend_profile_id": profile_id})
        await self.apis.delete_one({"_id": profile_id})

    # -- versions ----------------------------------------------------------

    async def add_version(self, version: BackendAPIVersion) -> BackendAPIVersion:
        await self.versions.insert_one(version.to_doc())
        return version

    async def latest_version(self, profile_id: str) -> BackendAPIVersion | None:
        doc = await self.versions.find_one(
            {"backend_profile_id": profile_id}, sort=[("version", DESCENDING)]
        )
        return BackendAPIVersion.from_doc(doc) if doc else None

    async def latest_versions(self, profile_ids: Collection[str]) -> dict[str, BackendAPIVersion]:
        """profile_id → 가장 높은 버전. 목록 화면이 프로필마다 `latest_version` 을 부르면 N+1 이다.
        버전이 없는 프로필은 결과에 없다. 정렬은 uq_profile_version 색인을 탄다."""
        if not profile_ids:
            return {}
        cursor = await self.versions.aggregate(
            [
                {"$match": {"backend_profile_id": {"$in": list(profile_ids)}}},
                {"$sort": {"backend_profile_id": 1, "version": -1}},
                {"$group": {"_id": "$backend_profile_id", "doc": {"$first": "$$ROOT"}}},
            ]
        )
        return {d["_id"]: BackendAPIVersion.from_doc(d["doc"]) for d in await cursor.to_list()}

    async def list_versions(self, profile_id: str) -> list[BackendAPIVersion]:
        cursor = self.versions.find({"backend_profile_id": profile_id}).sort("version", ASCENDING)
        return [BackendAPIVersion.from_doc(d) for d in await cursor.to_list()]

    async def get_version(self, profile_id: str, version: int) -> BackendAPIVersion | None:
        doc = await self.versions.find_one({"backend_profile_id": profile_id, "version": version})
        return BackendAPIVersion.from_doc(doc) if doc else None

    async def get_versions(
        self, refs: Collection[tuple[str, int]]
    ) -> dict[tuple[str, int], BackendAPIVersion]:
        """(profile_id, version) → 버전 문서. 허브 항목 여럿의 도구 이름을 한 번에 계산할 때
        쓴다 (D49). 짝마다 `get_version` 을 부르면 N+1 이다."""
        if not refs:
            return {}
        cursor = self.versions.find(
            {"$or": [{"backend_profile_id": p, "version": v} for p, v in refs]}
        )
        out: dict[tuple[str, int], BackendAPIVersion] = {}
        for d in await cursor.to_list():
            out[(d["backend_profile_id"], d["version"])] = BackendAPIVersion.from_doc(d)
        return out

    # -- tokens ------------------------------------------------------------

    async def create_token(
        self,
        profile_id: str,
        token_sha256: str,
        label: str | None,
        scope: list[str] | None = None,
    ) -> BackendAPIToken:
        tok = BackendAPIToken(
            backend_profile_id=profile_id,
            token_sha256=token_sha256,
            label=label,
            scope=list(scope or []),
        )
        await self.tokens.insert_one(tok.to_doc())
        return tok

    async def get_token(self, profile_id: str, token_id: str) -> BackendAPIToken | None:
        doc = await self.tokens.find_one({"_id": token_id, "backend_profile_id": profile_id})
        return BackendAPIToken.from_doc(doc) if doc else None

    async def list_tokens(
        self, profile_id: str, *, active_only: bool = False
    ) -> list[BackendAPIToken]:
        query: dict[str, Any] = {"backend_profile_id": profile_id}
        if active_only:
            query["revoked_at"] = None
        cursor = self.tokens.find(query).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [BackendAPIToken.from_doc(d) for d in await cursor.to_list()]

    async def revoke_token(self, profile_id: str, token_id: str) -> bool:
        """프로필 소유 확인까지 하는 회수. 사용자가 부르는 경로(API)가 쓴다."""
        return await revoke_once(self.tokens, {"_id": token_id, "backend_profile_id": profile_id})

    async def revoke_token_by_id(self, token_id: str) -> bool:
        """토큰 id 만으로 회수한다. 인스턴스가 자기가 발급한 id 를 들고 정리할 때 쓴다.
        어느 Backend API 것인지 다시 맞출 필요가 없고, 맞추려다 어긋나면 회수가
        조용히 실패한다(리비전이 바뀌어 backends 목록이 달라진 경우)."""
        return await revoke_once(self.tokens, {"_id": token_id})

    async def revoke_tokens_labeled(self, label: str, scope: str) -> int:
        """라벨과 scope 가 둘 다 맞는 살아 있는 토큰을 회수한다. 인스턴스가 id 를 적기 전에 만든
        스펙 토큰까지 거둘 때 쓴다 (P66 2.5절). 라벨은 사용자가 만든 토큰에도 자유롭게 붙으므로
        scope 를 함께 본다. 회수한 수다."""
        res = await self.tokens.update_many(
            {"label": label, "scope": scope, "revoked_at": None},
            {"$set": {"revoked_at": utcnow()}},
        )
        return res.modified_count
