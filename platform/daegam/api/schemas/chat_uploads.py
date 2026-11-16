"""채팅 첨부 업로드의 응답 (P36 9절, D61). 업로드 한 건은 store 모델에서 본문과 주인을 뺀 것이다."""

from __future__ import annotations

from pydantic import BaseModel

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.store.base import UtcDatetime
from daegam.store.chat_uploads import ChatUpload, UploadKind


class AgentUploadOut(BaseModel):
    """`POST /agents/{id}/uploads` 의 201. 보낼 때 `id` 만 `attachments` 에 싣는다. 문서면 뽑은
    글의 글자 수(`text_chars`)와 쪽·시트 수, 50만 자에서 잘렸는지가 있고 이미지면 비어 있다.
    `expires_at` 까지 보내지 않으면 사라진다."""

    model_config = REQUIRED_DEFAULTS

    id: str
    name: str
    kind: UploadKind
    format: str
    bytes: int
    text_chars: int | None = None
    pages: int | None = None
    sheets: int | None = None
    truncated: bool = False
    expires_at: UtcDatetime

    @classmethod
    def of(cls, upload: ChatUpload) -> AgentUploadOut:
        return cls.model_validate(upload.model_dump(include=set(cls.model_fields)))


__all__ = ["AgentUploadOut"]
