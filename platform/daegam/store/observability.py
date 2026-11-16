"""관측 설정 (P37 15절). `settings` 컬렉션의 `_id: "observability"` 문서다.

Platform Settings 문서(`_id: "platform"`) 밖에 두는 것은 되돌리기 때문이다. 앞선 판(rc5)의
`PlatformSettings` 는 모르는 칸을 거절하므로(extra forbid) 그 문서에 칸을 더하면 되돌린 플랫폼이
설정을 읽지 못한다. 가격표(`pricing`)와 게이트웨이 토큰 회수 표(`telemetry_ingest`)도 같은 까닭으로
따로인 문서다. 저장은 사람의 저장(P68)이다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import SeqModel, UtcDatetime, from_stored, upsert_by_seq, utcnow
from daegam.store.settings import SETTINGS
from daegam.urls import export_url_problem

OBSERVABILITY_DOC_ID = "observability"
# 헤더 Secret 이름의 접두어. 플랫폼 네임스페이스의 다른 Secret(서명 키, provider 키)을 헤더로
# 내보내지 못하게 한다
OTLP_SECRET_PREFIX = "daegam-otlp-"


class OtlpSettings(BaseModel):
    """OTLP 내보내기. 플랫폼이 이벤트 저장소에서 내보내므로 설치에 하나다. 헤더는 플랫폼
    네임스페이스의 Secret 이름(`daegam-otlp-` 로 시작한다)만 두고 값은 Secret 에만 있다. TLS 검증은
    끄지 못한다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    endpoint: str | None = Field(default=None, max_length=2048)
    headers_secret: str | None = Field(
        default=None, pattern=rf"^{OTLP_SECRET_PREFIX}[a-z0-9]([-a-z0-9.]{{0,239}}[a-z0-9])?$"
    )
    sample_ratio: float = Field(default=1.0, ge=0, le=1)
    always_errors: bool = True
    capture_content: bool = False

    @model_validator(mode="after")
    def _check_endpoint(self) -> OtlpSettings:
        if self.endpoint is not None and (problem := export_url_problem(self.endpoint)):
            raise ValueError(f"endpoint: {problem}")
        if self.enabled and not self.endpoint:
            raise ValueError("an enabled OTLP export needs an endpoint")
        return self


class ObservabilitySettings(SeqModel):
    """관측 설정 문서. 지금은 OTLP 내보내기 하나다."""

    model_config = ConfigDict(extra="forbid")

    otlp: OtlpSettings = Field(default_factory=OtlpSettings)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None


class MongoObservabilityStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SETTINGS]

    async def init_schema(self) -> None:
        return None

    async def get(self) -> ObservabilitySettings:
        doc = await self.coll.find_one({"_id": OBSERVABILITY_DOC_ID})
        return ObservabilitySettings() if doc is None else from_stored(ObservabilitySettings, doc)

    async def update(
        self,
        changes: dict[str, Any],
        *,
        updated_by: str | None = None,
        base: ObservabilitySettings | None = None,
    ) -> ObservabilitySettings | None:
        """사람의 저장 (P68 2절). base(없으면 지금 읽은 값)를 읽은 `seq` 일 때만 맞는다. 그사이 다른
        저장이 지나갔으면 None 이다."""
        current = base if base is not None else await self.get()
        doc = await upsert_by_seq(self.coll, OBSERVABILITY_DOC_ID, current, changes, updated_by)
        return None if doc is None else from_stored(ObservabilitySettings, doc)
