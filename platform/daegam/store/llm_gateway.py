"""LLM 게이트웨이의 설치 값, 한도 설정과 상태 문서 (P38 3절·4절·7절·12절).

- `LlmGatewayImages`: `DAEGAM_LLM_GATEWAY_IMAGES` 는 게이트웨이 한 벌의 남의 이미지 넷이다.
  `DAEGAM_HOME_IMAGES` 와 같은 꼴이라 레지스트리가 없는 참조에는 `registry_prefix` 가 붙는다
  (`config/resolve.py` `with_registry`). 참조는 대감 차트의 값 `llmGateway.images` 이고 차트가 이
  키를 렌더한다. 사본을 쓰는 설치는 `scripts/lib/llm_gateway_images.py` 의 값 파일로 레지스트리
  사본을 가리킨다.
- 한도(Platform Settings 의 `llm_gateway` 절)는 `store/settings.py` `LlmGatewaySettings` 다.
- `LlmGatewayState`: 동기화가 틱마다 고치는 상태다. `settings` 컬렉션의 `_id: "llm_gateway"`
  문서이고 Platform Settings 문서와 따로다. 사람이 고치는 값이 아니기 때문이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase

from daegam.config.resolve import with_registry
from daegam.store.base import UtcDatetime
from daegam.store.settings import SETTINGS

# 이미지 참조는 digest 로 못 박는다. 게이트웨이가 모든 키를 들기 때문이다 (P38 3절)
_PINNED = r"^[^\s@$]+@sha256:[0-9a-f]{64}$"
STATE_DOC_ID = "llm_gateway"


class LlmGatewayImages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_prefix: str | None = None
    pull_secret: str | None = None
    pull_policy: Literal["IfNotPresent", "Always", "Never"] = "IfNotPresent"
    gateway_image: str = Field(pattern=_PINNED)
    ratelimit_image: str = Field(pattern=_PINNED)
    redis_image: str = Field(pattern=_PINNED)
    collector_image: str = Field(pattern=_PINNED)

    def ref(self, image: str) -> str:
        """레지스트리 접두가 붙은 참조."""
        return with_registry(image, self.registry_prefix)


class PodRev(BaseModel):
    """게이트웨이 파드 하나가 알린 리비전과 그 값을 처음 알린 때."""

    rev: str | None = None
    since: UtcDatetime


class ProviderSync(BaseModel):
    """provider 하나의 거절."""

    # 렌더하지 않은 까닭(SSRF 검사, 키 없음). 있으면 그 provider 는 준비되지 않는다
    rejected: str | None = None


class LlmGatewayState(BaseModel):
    """동기화의 상태 문서 (P38 4절)."""

    model_config = ConfigDict(extra="ignore")

    # 끈 뒤 객체를 다 지운 다음 처음 렌더한 때(초 단위). 리스너가 이보다 앞에 낸 토큰을 거절한다
    activated_at: UtcDatetime | None = None
    # 리비전 하나가 처음 모든 파드에 반영된 때. 없으면 아직 아무것도 열리지 않았다
    ready_at: UtcDatetime | None = None
    rendered: bool = False
    desired_rev: str | None = None
    desired_at: UtcDatetime | None = None
    desired_providers: list[str] = Field(default_factory=list)
    applied_rev: str | None = None
    # 채점 호출의 목적 속성(`daegam.caller.purpose`, P45 11절)을 싣는 설정으로 뜬 파드가 모두 반영한
    # 리비전. 시작할 때만 읽는 설정이라 반영과 함께 파드의 설정 판(`config-rev` 어노테이션)도 본다.
    # applied_rev 와 같을 때만 채점이 게이트웨이 길을 쓴다. 이 칸을 모르는 앞 판이 다른 리비전을
    # 반영하면 둘이 갈라진다
    purpose_rev: str | None = None
    # 반영된 리비전의 provider. 지워지거나 게이트웨이를 끌 때까지 남는다
    providers_ready: list[str] = Field(default_factory=list)
    # 파드가 버린 원하는 리비전. 그동안 설정 Secret 은 마지막 좋은 리비전으로 되돌려 둔다
    rejected_rev: str | None = None
    rejected_at: UtcDatetime | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    pods: dict[str, PodRev] = Field(default_factory=dict)
    # provider slug 마다. 이름에는 점이 들 수 있어 Mongo 의 칸 이름으로 쓰지 않는다
    providers: dict[str, ProviderSync] = Field(default_factory=dict)
    # provider slug → 키 저장 경로가 사본에 키를 넣은 때. 그 경로만 `$set` 으로 쓰고 틱의 `put` 은
    # 건드리지 않는다. 틱이 문서를 다시 써도 사이에 적은 시각이 사라지지 않는다
    key_synced: dict[str, UtcDatetime] = Field(default_factory=dict)
    limits_rev: str | None = None
    revocations: int = 0
    # 반영된 리비전이 실은 jti. 원하는 리비전의 jti 가운데 여기 없는 것이 아직 먹지 않은 회수다
    applied_revoked: list[str] = Field(default_factory=list)
    revocations_pending: int = 0
    # 수집기가 지금 든 수집 토큰의 만료 (8절)
    ingest_expires_at: UtcDatetime | None = None
    synced_at: UtcDatetime | None = None


class MongoLlmGatewayStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SETTINGS]

    async def init_schema(self) -> None:
        return None

    async def get(self) -> LlmGatewayState:
        doc = await self.coll.find_one({"_id": STATE_DOC_ID})
        return LlmGatewayState.model_validate(doc or {})

    async def put(self, state: LlmGatewayState) -> None:
        """동기화가 리스 안에서만 쓴다. `key_synced` 밖의 칸을 모두 바꾼다."""
        body: dict[str, Any] = state.model_dump(exclude={"key_synced"})
        await self.coll.update_one({"_id": STATE_DOC_ID}, {"$set": body}, upsert=True)

    async def key_synced(self, slug: str, at: datetime) -> None:
        """키 저장 경로(`sync_provider`)가 그 provider 의 키 동기화 시각만 적는다."""
        await self.coll.update_one(
            {"_id": STATE_DOC_ID},
            {"$set": {f"key_synced.{slug}": at}},
            upsert=True,
        )
