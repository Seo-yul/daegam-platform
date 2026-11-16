"""Data Service 응답. 본문은 store 모델 DataService 그대로다(기본값 필드도 required 로 문서화)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse
from daegam.api.schemas.tenancy import PlainRedisConsumerOut
from daegam.store.data_services import DataService


class DataServiceOut(DataService):
    model_config = REQUIRED_DEFAULTS


class DataServiceCreated(DataServiceOut):
    """POST 응답에만 namespace(테넌트 네임스페이스) 가 붙는다."""

    namespace: str


class DataServiceDeleteResponse(DeleteResponse):
    secret_name: str


class DataServiceVerifyResponse(BaseModel):
    """ok 면 {id, ok, verified_at, detail, extensions?|redis?}, 아니면 {id, ok, error, detail}.
    extensions 는 postgres 만 `{vector: {installed: "0.8.6"|null, available: bool}}` (D32).
    redis 는 redis 만 `{version: "8.2.1"|null, modules: [name, ...]|null, checkpointer:
    "ready"|"modules_missing"|"unknown"}` 이고 version·modules 는 INFO·MODULE LIST 권한이 없으면
    null 이다. checkpointer 는 문서의 `capabilities.checkpointer` 에도 남는다 (D33)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    ok: bool
    verified_at: str | None = None
    error: str | None = None
    detail: str | None = None
    extensions: dict[str, Any] | None = None
    redis: dict[str, Any] | None = None


class DataServicePlainConsumers(BaseModel):
    """`GET /data-services/{id}/plain-consumers` (P61 5절 4단계). TLS 끝점이 있는 Redis 에 아직 평문
    포트로 붙는 이 테넌트의 Agent 와 MCP Server 다. TLS 끝점이 없거나 Redis 가 아니면 비어 있다."""

    model_config = ConfigDict(extra="forbid")

    consumers: list[PlainRedisConsumerOut]
