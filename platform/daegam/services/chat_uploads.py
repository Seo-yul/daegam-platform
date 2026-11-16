"""채팅 첨부의 업로드 (P36 9절, D61). 두 단계의 앞 단계인 업로드와, 턴에 싣고 지우는 일이다.

업로드는 파일 하나다. 형식은 문서 형식과 이미지 넷이고 문서는 10MiB, 이미지는 2MiB 를 넘으면 413
too_large 다(`documents/formats.py` 의 `accepted_format`). 본문이 문서 상한에 multipart 여유를 더한
것보다 크면 본문 상한 미들웨어(`body_limit.py`)가 읽기 전에 끊는다. 문서는 `services/docparse.py`
가 `purpose: "upload"` 로 뽑는다. 텍스트 형식은 플랫폼이 바로 풀고 나머지는 `daegam-docparse` 가
뽑으며, 글은 50만 자에서 잘려 `truncated` 로 알린다. 빈 글은 Knowledge 인덱서와 같은 422
empty_document 다(스캔 문서일 수 있다). 이미지는 풀지 않는다. 매직 바이트와 가로세로는 런타임만
본다(agent-runtime/runtime/attachments.py).

남용 상한은 셋이다. 사람마다 분당 20(429 upload_rate_limited, 형식·크기로 거절한 것과 추출에
실패한 것도 센다), 쓰지 않은 업로드 20(429 too_many_uploads), 동시 추출 하나(429 upload_busy)다.
동시 추출은 `store/leases.py` 의 이름 리스 `chat-upload:{sub}` 로 가른다. 비서의
`assistant-turn:{sub}` 와 같은 방식이라 레플리카가 여럿이어도 맞는다. 텍스트 형식과 이미지는
서비스를 부르지 않으므로 리스를 잡지 않는다.

코드 실행 Agent(P40 6절)에 올린 문서는 원본 바이트도 남기고, 턴에 실을 때 `original`(base64)로
함께 보낸다. 런타임이 그 원본을 작업 공간의 `/workspace/uploads/<이름>` 에 올린다. 한 턴의 원본은
합쳐 8MiB(런타임 세션 상한의 가장 큰 값)까지만 싣고, 넘는 문서는 글만 간다. 런타임의 `/chat` 본문
상한(32MiB) 안에 들게 하려는 것이다. 싣지 않은 원본은 `original_dropped: true` 로 알려 런타임의 안내
줄이 글만 남았다고 말하게 한다.

턴에 싣기(`resolve`)는 id 마다 테넌트·Agent·sub 와 만료를 본다. 하나라도 맞지 않으면 모두 같은
422 unknown_upload 이므로, 응답만으로 남의 업로드 id 가 있는지 알아낼 수 없다. 싣는 모양은
런타임 `/chat` 의 `attachments` 다. 런타임이 스트림을 열면 라우터가 `consume` 으로 지우고,
입구에서 거절되면 다시 보낼 수 있게 남긴다.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from daegam.documents.formats import (
    ATTACHMENT_FORMATS,
    CONTENT_TYPES,
    IMAGE_FORMATS,
    PARSED_FORMATS,
    FormatRefused,
    accepted_format,
)
from daegam.documents.result import UPLOAD, Extracted
from daegam.errors import ServiceError, retry_after_header
from daegam.services.docparse import CALL_TIMEOUT, DocparseClient
from daegam.services.sandboxes import code_execution_on
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.chat_uploads import DOCUMENT, IMAGE, ChatUpload, MongoChatUploadStore
from daegam.store.leases import holding_lease

logger = logging.getLogger("daegam.chat_uploads")

# 업로드가 쓰이지 않고 남는 시간이다. 지나면 TTL 이 지운다
UPLOAD_TTL = timedelta(hours=1)
MAX_UPLOADS_PER_MINUTE = 20
MAX_UNUSED_UPLOADS = 20
# 한 턴에 싣는 업로드 수의 상한이다. 런타임 attachments.py 의 MAX_ATTACHMENTS_PER_TURN 과 같다
MAX_ATTACHMENTS_PER_TURN = 5
# 한 턴에 싣는 문서 원본 바이트의 합이다(P40 6절). 런타임 세션 상한의 가장 큰 값(Postgres 8MiB)이다
MAX_ORIGINAL_BYTES_PER_TURN = 8 * 1024 * 1024
# 동시 추출을 가르는 이름 리스의 수명이다. 추출 호출 상한보다 길어야 도는 추출을 다른 요청이
# 앞지르지 못하고, 파드가 죽어 풀지 못한 리스도 이 시간 뒤에는 풀린다
EXTRACT_LEASE = timedelta(seconds=CALL_TIMEOUT + 15)


class ChatUploadError(ServiceError):
    """첨부 업로드의 오류. 전역 핸들러가 플랫폼 형식으로 낸다."""


def upload_lease_name(user_sub: str) -> str:
    return f"chat-upload:{user_sub}"


def runtime_attachment(
    upload: ChatUpload, *, original: bool = False, dropped: bool = False
) -> dict[str, Any]:
    """런타임 `/chat` 의 첨부 하나다. 문서는 글 첨부, 이미지는 base64 이미지다. original 이면 문서의
    원본도 싣고, dropped 면 원본이 있었지만 한 턴의 상한으로 싣지 않았다고 알린다(코드 실행 Agent,
    P40 6절). 런타임은 그때 안내 줄에 글만 남았다고 적는다."""
    if upload.kind == IMAGE:
        data = base64.b64encode(upload.content).decode("ascii")
        return {"kind": "image", "name": upload.name, "media_type": upload.media_type, "data": data}
    counts = {k: v for k in ("pages", "sheets") if (v := getattr(upload, k)) is not None}
    extra = {}
    if original and upload.original is not None:
        extra["original"] = base64.b64encode(upload.original).decode("ascii")
    elif dropped and upload.original is not None:
        extra["original_dropped"] = True
    return {
        "kind": "text",
        "name": upload.name,
        "format": upload.format,
        "text": upload.content.decode("utf-8"),
        "sha256": upload.sha256,
        "truncated": upload.truncated,
        **counts,
        **extra,
    }


class ChatUploadService:
    def __init__(
        self,
        store: MongoChatUploadStore,
        docparse: DocparseClient,
        db: AsyncDatabase,
        *,
        pod_id: str,
    ):
        self.store = store
        # 테스트가 가짜 서비스를 넣는 자리
        self.docparse = docparse
        self.db = db
        self.pod_id = pod_id

    async def upload(
        self, doc: Agent, *, user_sub: str, name: str, content_type: str | None, data: bytes
    ) -> ChatUpload:
        """파일 하나를 업로드한다. 남용 상한을 먼저 보고 그다음 형식과 크기를 본다. 거절한 시도도
        분당 칸에 세어야 큰 파일을 거듭 보내는 일이 상한에 걸린다. 문서면 글을 뽑아 둔다."""
        now = utcnow()
        await self._check_rate(user_sub, now)
        try:
            fmt = accepted_format(name, content_type, len(data), accept=ATTACHMENT_FORMATS)
        except FormatRefused as e:
            raise ChatUploadError(e.status, e.code, e.detail) from e
        if fmt in IMAGE_FORMATS:
            kind, content, facts = IMAGE, data, {}
        else:
            extracted = await self._extract(user_sub, fmt, data)
            kind, content, facts = DOCUMENT, extracted.text.encode("utf-8"), _text_facts(extracted)
        upload = ChatUpload(
            tenant=doc.tenant,
            agent_id=doc.id,
            user_sub=user_sub,
            name=name,
            kind=kind,
            format=fmt,
            media_type=CONTENT_TYPES[fmt],
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content=content,
            # 코드 실행 Agent 의 명령은 문서 원본을 읽는다(P40 6절)
            original=data if kind == DOCUMENT and code_execution_on(doc) else None,
            created_at=now,
            expires_at=now + UPLOAD_TTL,
            **facts,
        )
        await self.store.add(upload)
        return upload

    async def _check_rate(self, user_sub: str, now: datetime) -> None:
        """쓰지 않은 업로드 수를 먼저 보고, 그다음 이번 시도를 분당 칸에 더한다."""
        if await self.store.count_unused(user_sub, now) >= MAX_UNUSED_UPLOADS:
            raise ChatUploadError(
                429,
                "too_many_uploads",
                f"you have {MAX_UNUSED_UPLOADS} unsent uploads; send or remove some first",
            )
        attempts, window_ends = await self.store.count_attempt(user_sub, now)
        if attempts > MAX_UPLOADS_PER_MINUTE:
            raise ChatUploadError(
                429,
                "upload_rate_limited",
                f"more than {MAX_UPLOADS_PER_MINUTE} uploads in a minute",
                headers=retry_after_header(window_ends - now),
            )

    @contextlib.asynccontextmanager
    async def _one_extract(self, user_sub: str) -> AsyncIterator[None]:
        """그 사람의 동시 추출을 하나로 묶는다. 이미 도는 추출이 있으면 429 upload_busy 다."""
        busy = ChatUploadError(429, "upload_busy", "another upload of yours is being read")
        async with holding_lease(
            self.db, self.pod_id, upload_lease_name(user_sub), busy, ttl=EXTRACT_LEASE
        ):
            yield

    async def _extract(self, user_sub: str, fmt: str, data: bytes) -> Extracted:
        """문서의 글이다. 서비스를 부르는 형식만 동시 추출 하나에 묶는다. 빈 글은 422
        empty_document 다."""
        slot = self._one_extract(user_sub) if fmt in PARSED_FORMATS else contextlib.nullcontext()
        async with slot:
            extracted = await self.docparse.extract(fmt, data, purpose=UPLOAD)
        if not extracted.text.strip():
            raise ChatUploadError(422, "empty_document", "no text could be extracted")
        return extracted

    async def resolve(self, doc: Agent, user_sub: str, ids: Sequence[str]) -> list[dict[str, Any]]:
        """턴에 싣는 첨부를 런타임 모양으로, 보낸 차례대로 낸다. 하나라도 그 사람이 이 Agent 에
        업로드한 살아 있는 것이 아니면 422 unknown_upload 다."""
        found = await self.store.owned(
            ids, tenant=doc.tenant, agent_id=doc.id, user_sub=user_sub, now=utcnow()
        )
        by_id = {u.id: u for u in found}
        if any(i not in by_id for i in ids):
            raise ChatUploadError(
                422, "unknown_upload", "an attachment is unknown or has expired; upload it again"
            )
        executes = code_execution_on(doc)
        room = MAX_ORIGINAL_BYTES_PER_TURN if executes else 0
        out: list[dict[str, Any]] = []
        for upload in (by_id[i] for i in ids):
            size = len(upload.original or b"")
            fits = 0 < size <= room
            room -= size if fits else 0
            out.append(runtime_attachment(upload, original=fits, dropped=executes and not fits))
        return out

    async def consume(self, doc: Agent, user_sub: str, ids: Sequence[str]) -> None:
        """턴에 실린 업로드를 지운다. 런타임이 세션에 한 벌 두었으므로 더 남길 까닭이 없다. 이미
        열린 스트림을 이 실패로 끊지 않도록 Mongo 오류는 경고로 남기고, 남은 업로드는 TTL 이
        지운다."""
        if not ids:
            return
        try:
            await self.store.delete(ids, tenant=doc.tenant, agent_id=doc.id, user_sub=user_sub)
        except PyMongoError:
            logger.warning("sent uploads were not removed; the TTL will remove them", exc_info=True)

    async def remove(self, doc: Agent, user_sub: str, upload_id: str) -> None:
        """칩을 지운 자리다. 이미 실렸거나 만료됐거나 남의 것이어도 같은 결과라 아무것도 드러내지
        않는다."""
        await self.store.delete([upload_id], tenant=doc.tenant, agent_id=doc.id, user_sub=user_sub)


def _text_facts(extracted: Extracted) -> dict[str, Any]:
    return {
        "text_chars": len(extracted.text),
        "pages": extracted.pages,
        "sheets": extracted.sheets,
        "truncated": extracted.truncated,
    }


__all__ = [
    "EXTRACT_LEASE",
    "MAX_ATTACHMENTS_PER_TURN",
    "MAX_UNUSED_UPLOADS",
    "MAX_UPLOADS_PER_MINUTE",
    "UPLOAD_TTL",
    "ChatUploadError",
    "ChatUploadService",
    "runtime_attachment",
    "upload_lease_name",
]
