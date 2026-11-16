"""채팅 첨부의 업로드 (P36 9절, D61). 컬렉션 `chat_uploads` 와 `chat_upload_rate`.

    chat_uploads {_id, tenant, agent_id, user_sub, name, kind, format, media_type, bytes, sha256,
                  text_chars, pages, sheets, truncated, content, created_at, expires_at}
    chat_upload_rate {_id: "<user_sub>:<분>", count, expires_at}

파일을 선택하는 순간 업로드하고 보낼 때는 id 만 싣는 두 단계의 앞 단계다. id 는 128비트 난수다.
`content` 는 문서에서 뽑은 글(UTF-8)이나 화면이 다시 구운 이미지 바이트이고 원본 문서는 두지
않는다. 본문까지 문서 하나라 16MB 한도 안이고 GridFS 를 쓰지 않으므로 TTL 이 통째로 지운다. 턴에
실리거나 칩을 지우면 그 자리에서 지운다.

색인은 TTL 과 `(user_sub, expires_at)` 이다. 뒤의 것으로 그 사람의 아직 만료되지 않은 업로드를
센다. 분당 상한은 지운 업로드와 추출에 실패한 업로드도 세야 하므로 문서 수가 아니라
`chat_upload_rate` 의 분 단위 칸에 더한다. 칸은 그 분이 지나면 TTL 이 지운다. 모든 질의는
사람(`user_sub`)으로 거르고, 업로드를 싣고 지우는 질의는 테넌트와 `agent_id` 로도 거른다.
"""

from __future__ import annotations

import secrets
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, count_in_window, ensure_ttl_index

CHAT_UPLOADS = "chat_uploads"
CHAT_UPLOAD_RATE = "chat_upload_rate"
# 업로드 id 의 난수 바이트 수(128비트)다. hex 로 32자다
UPLOAD_ID_BYTES = 16
# 분당 상한 칸의 폭과, 칸이 TTL 로 지워지기까지 더 남는 시간이다
RATE_WINDOW = timedelta(minutes=1)
RATE_KEEP = timedelta(minutes=1)

UploadKind = Literal["document", "image"]
DOCUMENT: UploadKind = "document"
IMAGE: UploadKind = "image"


def new_upload_id() -> str:
    return secrets.token_hex(UPLOAD_ID_BYTES)


class ChatUpload(Document):
    """업로드 하나. 문서면 `content` 가 뽑은 글의 UTF-8 이고 `text_chars`·`pages`·`sheets`·
    `truncated` 가 그 글의 사실이다. 이미지면 `content` 가 이미지 바이트이고 그 칸들은 비어 있다.
    `bytes` 와 `sha256` 은 업로드한 파일 그대로의 크기와 해시다. 코드 실행 Agent 의 문서는
    `original` 에 원본도 둔다."""

    id: str = Field(default_factory=new_upload_id)
    tenant: str
    agent_id: str
    user_sub: str
    name: str
    kind: UploadKind
    format: str
    media_type: str
    bytes: int
    sha256: str
    text_chars: int | None = None
    pages: int | None = None
    sheets: int | None = None
    truncated: bool = False
    content: bytes = Field(repr=False)
    # 코드 실행 Agent 에 올린 문서의 원본 바이트다(P40 6절). 런타임이 작업 공간에 올린다. 그 밖은
    # 없다
    original: bytes | None = Field(default=None, repr=False)
    created_at: UtcDatetime
    expires_at: UtcDatetime


def _owned_query(
    ids: Collection[str], *, tenant: str, agent_id: str, user_sub: str
) -> dict[str, Any]:
    """그 사람이 이 Agent 에 업로드한 것 가운데 ids 다."""
    return {"_id": {"$in": list(ids)}, "tenant": tenant, "agent_id": agent_id, "user_sub": user_sub}


class MongoChatUploadStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[CHAT_UPLOADS]
        self.rate = db[CHAT_UPLOAD_RATE]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("user_sub", ASCENDING), ("expires_at", ASCENDING)], name="ix_user_expires"
        )
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")
        await ensure_ttl_index(self.rate, "expires_at", 0, "ttl_expires_at")

    async def add(self, upload: ChatUpload) -> None:
        await self.coll.insert_one(upload.to_doc())

    async def count_attempt(self, user_sub: str, now: datetime) -> tuple[int, datetime]:
        """이번 분의 업로드 시도를 하나 더하고 더한 뒤의 수와 칸이 끝나는 시각을 돌려준다."""
        return await count_in_window(self.rate, user_sub, now, RATE_WINDOW, RATE_KEEP)

    async def count_unused(self, user_sub: str, now: datetime) -> int:
        """그 사람의 아직 쓰지 않은 업로드 수다. TTL 이 아직 지우지 않은 만료분은 세지 않는다."""
        return await self.coll.count_documents({"user_sub": user_sub, "expires_at": {"$gt": now}})

    async def owned(
        self,
        ids: Collection[str],
        *,
        tenant: str,
        agent_id: str,
        user_sub: str,
        now: datetime,
    ) -> list[ChatUpload]:
        """그 사람이 이 Agent 에 업로드했고 만료되지 않은 것만이다. 남의 id 와 없는 id 는 빠진다."""
        query = _owned_query(ids, tenant=tenant, agent_id=agent_id, user_sub=user_sub)
        cursor = self.coll.find({**query, "expires_at": {"$gt": now}})
        return [ChatUpload.from_doc(d) async for d in cursor]

    async def delete(
        self, ids: Collection[str], *, tenant: str, agent_id: str, user_sub: str
    ) -> int:
        """그 사람이 이 Agent 에 업로드한 것만 지운다. 지운 수다."""
        query = _owned_query(ids, tenant=tenant, agent_id=agent_id, user_sub=user_sub)
        return (await self.coll.delete_many(query)).deleted_count


__all__ = [
    "CHAT_UPLOADS",
    "CHAT_UPLOAD_RATE",
    "DOCUMENT",
    "IMAGE",
    "ChatUpload",
    "MongoChatUploadStore",
    "UploadKind",
    "new_upload_id",
]
