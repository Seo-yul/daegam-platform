"""Knowledge Base 응답 (D32). 본문은 store 모델 그대로이고 목록·상세는 `status` 집계를 싣는다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS, DeleteResponse
from daegam.store.knowledge import DocState, KnowledgeBase, KnowledgeDocument, KnowledgeStatus


class KnowledgeBaseOut(KnowledgeBase):
    """목록·상세·생성·수정 응답. `status` 는 문서 컬렉션 집계다."""

    model_config = REQUIRED_DEFAULTS

    status: KnowledgeStatus


class KnowledgeDocumentOut(KnowledgeDocument):
    model_config = REQUIRED_DEFAULTS


class KnowledgeBaseDeleteResponse(DeleteResponse):
    """`deleted` 가 거짓이면 테이블이 남았다(`warning: table_not_dropped`, 리소스는
    `orphaned_table`)."""

    table_dropped: bool
    warning: Literal["table_not_dropped"] | None = None


class KnowledgeDocumentDeleteResponse(BaseModel):
    """문서 삭제는 상태를 `deleting` 으로 바꾸는 것뿐이다. 행·본문·문서는 워커가 지운다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    state: DocState


class KnowledgeReindexResponse(BaseModel):
    """전체 다시 인덱싱. `pending` 은 되돌린 문서 수다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pending: int


__all__ = [
    "KnowledgeBaseDeleteResponse",
    "KnowledgeBaseOut",
    "KnowledgeDocumentDeleteResponse",
    "KnowledgeDocumentOut",
    "KnowledgeReindexResponse",
]
