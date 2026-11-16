"""`GET /{리소스}/{id}/references` 응답 (D43 셋째). 라우터 아홉이 이 한 모양을 쓴다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.services.references import PinnedRevisions, Reference


class PinnedRevisionsOut(PinnedRevisions):
    model_config = REQUIRED_DEFAULTS


class ReferenceOut(Reference):
    model_config = REQUIRED_DEFAULTS

    pins: PinnedRevisionsOut | None


class ReferencesResponse(BaseModel):
    """이 리소스를 쓰는 것. 아무것도 안 쓰면 빈 목록이다."""

    model_config = ConfigDict(extra="forbid")

    used_by: list[ReferenceOut]
