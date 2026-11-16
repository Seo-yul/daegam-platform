"""첨부 저장소와 모델에게 가는 모양이다(P36 9절, D61).

첨부는 체크포인트에 넣지 않는다. 이미지를 사람 메시지에 넣으면 `messages` 채널의 50 갱신마다의
스냅숏과 다시 생성의 갈래마다 통째로 다시 적히고, 글을 `files` 에 넣어도 같다(F0 3번). 그래서
세션 색인과 같은 Database 에 첨부 저장소를 하나 두고 세션마다 한 벌만 담는다. Postgres 는
`daegam_uploads` 표, Redis 는 인스턴스 접두어 아래 `upload:{sid}` 해시, memory 노드가 없으면
파드 안 dict 이고 전사 저장소의 세 구현과 같은 모양이다. 런타임의 공용 Redis 클라이언트는
`decode_responses=True` 라 Redis 쪽은 디코드하지 않는 클라이언트를 따로 받아 본문을 바이트
그대로 담는다. base64 로 담으면 세션 상한의 1.455배가 든다(F0 4번).

체크포인트의 사람 메시지에는 안내 글과 참조(`additional_kwargs.daegam.attachments` 의
upload_id 목록)만 남는다. 턴이 열릴 때 저장소를 한 번 읽어 턴 문맥(`TurnContext.uploads`)에
두고 두 길로 보인다.

- 글 첨부는 `/uploads/` 라우트의 `UploadsBackend` 가 읽기 전용 파일로 보인다.
  ls·read·glob·grep 은 deepagents 의 `grep_matches_from_files`·`_glob_search_files` 를
  저장소의 글에 그대로 쓰고 쓰기 동작은 오류다. 도구 자리에서 먼저 막는 것은 permissions.py 의
  쓰기 막기 규칙이다.
- 이미지는 `AttachmentImages` 미들웨어가 모델을 부를 때마다 요청의 그 사람 메시지에 langchain
  표준 image 블록을 붙인다. 상태에는 붙지 않는다. 우리 미들웨어 목록의 끝, 곧 폴백·재시도의
  안쪽에 서서 시도마다 채우고, 그 안쪽의 deepagents `UnsupportedContentMiddleware` 가 실제로
  부르는 모델의 프로필로 받지 못하는 이미지를 자리표시로 바꾼다.

세션이 닫히거나(sweep) 지워질 때 체크포인트와 함께 지운다(sessions.py). 첨부를 받고 검사하는
일은 attachments.py 의 몫이다.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    _glob_search_files,
    grep_matches_from_files,
    slice_read_response,
)
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.runtime import get_runtime

from runtime import fence
from runtime.turn_context import context_field

UPLOADS_ROUTE = "/uploads/"
"""첨부 글이 보이는 경로 접두어다. 쓰기는 막혀 있어 이 아래는 모두 사용자가 올린 것이다."""

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_ORIGINAL = "original"
"""코드 실행 Agent 의 문서 원본이다(P40 6절). 같은 이름의 글 첨부 바로 뒤에 저장하고, 파일 Tool 에는
보이지 않으며 샌드박스의 `/workspace/uploads/<이름>` 으로만 올라간다(sandbox.py)."""

DAEGAM_KWARGS_KEY = "daegam"
ATTACHMENTS_KEY = "attachments"
"""사람 메시지의 `additional_kwargs.daegam.attachments` 다. 첨부 저장소의 upload_id 목록이다."""

READ_ONLY_ERROR = "Error: {path} is a read-only attachment uploaded by the user."
"""`/uploads/` 아래에 쓰거나 지우려 할 때 모델이 받는 문장이다."""

GLOB_NO_MATCH = "No files found"
"""deepagents `_glob_search_files` 가 찾은 것이 없을 때 돌려주는 글이다."""


@dataclass(frozen=True)
class StoredUpload:
    """첨부 저장소의 첨부 하나다. 글 첨부의 content 는 UTF-8 바이트, 이미지는 그 바이트다."""

    upload_id: str
    kind: str
    name: str
    path: str
    size: int
    """저장한 바이트 수다. 세션 합(`upload_bytes`)이 이 값을 더한다."""
    sha256: str
    content: bytes
    created_at: datetime
    seq: int = 0
    """한 묶음 안에서 받은 차례다. 한 묶음은 created_at 이 같아 저장소가 이 값으로 넣은 차례를
    지킨다(attachments.py)."""
    format: str | None = None
    media_type: str | None = None
    pages: int | None = None
    sheets: int | None = None
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def summary(self) -> dict[str, Any]:
        """전사의 사용자 항목에 싣는 모양이다. 본문은 싣지 않는다."""
        out: dict[str, Any] = {"name": self.name, "kind": self.kind, "bytes": self.size}
        for key in ("pages", "sheets"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.truncated:
            out["truncated"] = True
        return out

    def image_block(self) -> dict[str, Any]:
        """모델 요청에 붙이는 langchain 표준 image 블록이다."""
        data = base64.b64encode(self.content).decode("ascii")
        return {"type": "image", "base64": data, "mime_type": self.media_type}

    def to_meta(self) -> dict[str, Any]:
        """본문을 뺀 칸이다. Redis 해시의 메타 칸이 이것의 JSON 이다."""
        meta = asdict(self)
        del meta["content"]
        meta["created_at"] = self.created_at.isoformat()
        return meta

    @classmethod
    def from_meta(cls, meta: dict[str, Any], content: bytes) -> StoredUpload:
        fields = {**meta, "created_at": datetime.fromisoformat(meta["created_at"])}
        return cls(**fields, content=content)


def message_refs(message: Any) -> list[str]:
    """사람 메시지가 단 첨부 참조(upload_id)다. 참조가 없거나 사람 메시지가 아니면(글, 재개 명령)
    빈 목록이다."""
    if not isinstance(message, HumanMessage):
        return []
    refs = (message.additional_kwargs.get(DAEGAM_KWARGS_KEY) or {}).get(ATTACHMENTS_KEY)
    return [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []


def ref_kwargs(upload_ids: Sequence[str]) -> dict[str, Any]:
    """사람 메시지의 `additional_kwargs` 에 넣는 참조다."""
    return {DAEGAM_KWARGS_KEY: {ATTACHMENTS_KEY: list(upload_ids)}}


@dataclass
class TurnUploads:
    """한 턴이 보는 세션의 첨부다. 턴 문맥에 실려 `/uploads/` 백엔드와 이미지 채우기가 읽는다.

    received 는 이번 요청이 받아 저장한 첨부 수이고 `done.attachments_received` 가 된다.
    """

    files: list[StoredUpload] = field(default_factory=list)
    received: int = 0

    def select(self, upload_ids: Sequence[str]) -> list[StoredUpload]:
        """참조한 차례대로의 첨부다. 저장소에 없는 id 는 건너뛴다."""
        by_id = {upload.upload_id: upload for upload in self.files}
        return [by_id[ref] for ref in upload_ids if ref in by_id]

    def text_files(self) -> dict[str, FileData]:
        """`/uploads/` 백엔드가 보는 파일이다. 키는 라우트를 뗀 경로(`/보고서.pdf.txt`)다."""
        offset = len(UPLOADS_ROUTE) - 1
        return {
            upload.path[offset:]: {
                "content": upload.text,
                "encoding": "utf-8",
                "created_at": upload.created_at.isoformat(),
                "modified_at": upload.created_at.isoformat(),
            }
            for upload in self.files
            if upload.kind == KIND_TEXT
        }


def uploads_of(runtime: Any) -> TurnUploads:
    """턴 문맥에 실린 첨부다. 문맥 없이 그래프를 부른 자리(테스트)는 빈 것이다."""
    uploads = context_field(runtime, "uploads")
    return uploads if isinstance(uploads, TurnUploads) else TurnUploads()


def _turn_uploads() -> TurnUploads:
    """도구 호출 안에서 지금 턴의 첨부다. 그래프 밖에서 부르면 빈 것이다."""
    try:
        return uploads_of(get_runtime())
    except RuntimeError:
        return TurnUploads()


def _file_info(path: str, data: FileData) -> FileInfo:
    return {
        "path": path,
        "is_dir": False,
        "size": len(data["content"].encode("utf-8")),
        "modified_at": data.get("modified_at", ""),
    }


class UploadsBackend(BackendProtocol):
    """`/uploads/` 라우트의 읽기 전용 백엔드다. 턴 문맥의 글 첨부만 보인다.

    CompositeBackend 가 라우트를 떼고 부르므로 경로는 `/` 로 시작하는 파일 이름이다. 첨부 이름에는
    `/` 가 없어 아래 디렉터리가 없다. 쓰기·고치기·지우기·올리기는 모두 오류다.
    """

    def ls(self, path: str) -> LsResult:
        files = _turn_uploads().text_files()
        directory = path if path.endswith("/") else f"{path}/"
        entries = [_file_info(p, d) for p, d in files.items() if p.startswith(directory)]
        return LsResult(entries=sorted(entries, key=lambda e: e["path"]))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        data = _turn_uploads().text_files().get(file_path)
        if data is None:
            return ReadResult(error=f"File '{file_path}' not found")
        return slice_read_response(data, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        files = _turn_uploads().text_files()
        return grep_matches_from_files(files, pattern, path or "/", glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        files = _turn_uploads().text_files()
        try:
            found = _glob_search_files(files, pattern, path)
        except InvalidGlobPatternError as e:
            return GlobResult(error=str(e))
        if found == GLOB_NO_MATCH:
            return GlobResult(matches=[])
        return GlobResult(matches=[_file_info(p, files[p]) for p in found.split("\n")])

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_read_only(file_path))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=_read_only(file_path))

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_read_only(file_path))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    # 저장소를 다시 읽지 않고 턴 문맥만 보므로 스레드로 넘기지 않고 그대로 부른다
    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(file_path, offset, limit)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return self.grep(pattern, path, glob, max_count=max_count)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self.glob(pattern, path)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    async def adelete(self, file_path: str) -> DeleteResult:
        return self.delete(file_path)


def _read_only(path: str) -> str:
    return READ_ONLY_ERROR.format(path=f"{UPLOADS_ROUTE[:-1]}{path}")


def _content_blocks(content: str | list[Any]) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content)


def filled_message(message: AnyMessage, uploads: TurnUploads) -> AnyMessage:
    """참조를 단 사람 메시지에 이미지 블록을 붙이고 참조 칸을 뗀 사본이다. 아니면 그대로다."""
    refs = message_refs(message)
    if not refs:
        return message
    kwargs = {k: v for k, v in message.additional_kwargs.items() if k != DAEGAM_KWARGS_KEY}
    images = [u.image_block() for u in uploads.select(refs) if u.kind == KIND_IMAGE]
    update: dict[str, Any] = {"additional_kwargs": kwargs}
    if images:
        update["content"] = [*_content_blocks(message.content), *images]
    return message.model_copy(update=update)


def filled_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
    """첨부 이미지를 채운 모델 요청이다. 참조를 단 메시지가 없으면 그대로다."""
    uploads = uploads_of(request.runtime)
    messages = [filled_message(message, uploads) for message in request.messages]
    if all(a is b for a, b in zip(messages, request.messages, strict=True)):
        return request
    return request.override(messages=messages)


class AttachmentImages(AgentMiddleware):
    """모델을 부를 때마다 요청의 사람 메시지에 첨부 이미지를 채운다. 상태는 바꾸지 않는다."""

    @property
    def name(self) -> str:
        return "DaegamAttachmentImages"

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> Any:
        return handler(filled_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        return await handler(filled_request(request))


class UploadStore(Protocol):
    async def add(self, session_id: str, uploads: Sequence[StoredUpload]) -> None:
        """세션에 첨부를 더한다. 한 묶음은 함께 들어가거나 함께 빠진다."""
        ...

    async def read(self, session_id: str) -> list[StoredUpload]:
        """세션의 첨부 전부다. 넣은 차례다."""
        ...

    async def delete(self, session_id: str) -> None: ...


class MemoryUploads:
    """파드 하나 안에서만 보이는 첨부 저장소다. memory 노드가 없을 때 쓴다."""

    def __init__(self) -> None:
        self._rows: dict[str, list[StoredUpload]] = {}

    async def add(self, session_id: str, uploads: Sequence[StoredUpload]) -> None:
        self._rows.setdefault(session_id, []).extend(uploads)

    async def read(self, session_id: str) -> list[StoredUpload]:
        return list(self._rows.get(session_id, []))

    async def delete(self, session_id: str) -> None:
        self._rows.pop(session_id, None)


# 세션 색인의 setup 이 같은 DB 에 만든다(sessions.py). upload_id 는 128비트 난수라 인스턴스로 나누지
# 않아도 섞이지 않고, 읽는 길은 인스턴스로 걸러진 색인을 먼저 지난다
UPLOADS_DDL = """
CREATE TABLE IF NOT EXISTS daegam_uploads (
    upload_id  text PRIMARY KEY,
    session_id text NOT NULL,
    kind       text NOT NULL,
    name       text NOT NULL,
    path       text NOT NULL,
    format     text,
    media_type text,
    bytes      bigint NOT NULL,
    sha256     text NOT NULL,
    pages      integer,
    sheets     integer,
    truncated  boolean NOT NULL DEFAULT false,
    content    bytea NOT NULL,
    created_at timestamptz NOT NULL,
    seq        integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS daegam_uploads_session ON daegam_uploads (session_id);
"""
INSERT_UPLOAD_SQL = """
INSERT INTO daegam_uploads
  (upload_id, session_id, kind, name, path, format, media_type, bytes, sha256, pages, sheets,
   truncated, content, created_at, seq)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
READ_UPLOADS_SQL = (
    "SELECT * FROM daegam_uploads WHERE session_id = %s ORDER BY created_at, seq, upload_id"
)
DELETE_UPLOADS_SQL = "DELETE FROM daegam_uploads WHERE session_id = %s"


class PostgresUploads:
    """memory 의 Postgres 에 두는 첨부 저장소다. 세션 색인과 풀 하나를 나눠 쓴다."""

    def __init__(self, pool: Any) -> None:
        # psycopg_pool.AsyncConnectionPool 이다. 색인과 같은 이유로 Any 다
        self._pool = pool

    async def add(self, session_id: str, uploads: Sequence[StoredUpload]) -> None:
        if not uploads:
            return
        rows = [_insert_row(session_id, upload) for upload in uploads]
        # COMMIT 은 커서를 거치지 않으므로 트랜잭션을 통째로 턴의 펜스 마감 안에 두고, COMMIT 전에
        # 펜스가 섰으면 되돌린다(fence.py)
        async with (
            self._pool.connection() as conn,
            fence.bounded(),
            conn.transaction(),
            conn.cursor() as cur,
        ):
            await cur.executemany(INSERT_UPLOAD_SQL, rows)
            fence.check()

    async def read(self, session_id: str) -> list[StoredUpload]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(READ_UPLOADS_SQL, (session_id,))
            rows = await cur.fetchall()
        return [_row_to_upload(row) for row in rows]

    async def delete(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(DELETE_UPLOADS_SQL, (session_id,))


def _insert_row(session_id: str, upload: StoredUpload) -> tuple[Any, ...]:
    return (
        upload.upload_id,
        session_id,
        upload.kind,
        upload.name,
        upload.path,
        upload.format,
        upload.media_type,
        upload.size,
        upload.sha256,
        upload.pages,
        upload.sheets,
        upload.truncated,
        upload.content,
        upload.created_at,
        upload.seq,
    )


def _row_to_upload(row: dict[str, Any]) -> StoredUpload:
    """표의 한 줄이다. 칸 이름이 같고 `bytes` 열만 `size` 다."""
    fields = {key: row[key] for key in _ROW_FIELDS}
    return StoredUpload(**fields, size=int(row["bytes"]), content=bytes(row["content"]))


_ROW_FIELDS = tuple(
    name for name in StoredUpload.__dataclass_fields__ if name not in ("size", "content")
)


# Redis 해시의 칸 이름 접두어다. 첨부마다 메타(JSON)와 본문 두 칸이다
REDIS_META_FIELD = "meta:"
REDIS_DATA_FIELD = "data:"


class RedisUploads:
    """memory 의 Redis 에 두는 첨부 저장소다. 키는 접두어 아래 `upload:{sid}` 해시다.

    클라이언트는 디코드하지 않는 것이라 칸 이름과 값이 바이트로 온다. 더하기는 HSET 한 번이라
    한 묶음이 함께 들어간다.
    """

    def __init__(self, client: Any, prefix: str) -> None:
        # redis.asyncio.Redis(decode_responses=False) 다. 연 쪽(session_index_redis.py)이 닫는다
        self._client = client
        self._prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}upload:{session_id}"

    async def add(self, session_id: str, uploads: Sequence[StoredUpload]) -> None:
        if not uploads:
            return
        mapping: dict[str, bytes] = {}
        for upload in uploads:
            meta = json.dumps(upload.to_meta(), ensure_ascii=False)
            mapping[REDIS_META_FIELD + upload.upload_id] = meta.encode("utf-8")
            mapping[REDIS_DATA_FIELD + upload.upload_id] = upload.content
        await self._client.hset(self._key(session_id), mapping=mapping)

    async def read(self, session_id: str) -> list[StoredUpload]:
        raw = await self._client.hgetall(self._key(session_id))
        fields = {key.decode("utf-8"): value for key, value in raw.items()}
        uploads: list[StoredUpload] = []
        for name, meta in fields.items():
            if not name.startswith(REDIS_META_FIELD):
                continue
            content = fields.get(REDIS_DATA_FIELD + name[len(REDIS_META_FIELD) :])
            if content is not None:
                uploads.append(StoredUpload.from_meta(json.loads(meta), content))
        return sorted(uploads, key=lambda u: (u.created_at, u.seq, u.upload_id))

    async def delete(self, session_id: str) -> None:
        await self._client.delete(self._key(session_id))


__all__ = [
    "UPLOADS_DDL",
    "UPLOADS_ROUTE",
    "AttachmentImages",
    "MemoryUploads",
    "PostgresUploads",
    "RedisUploads",
    "StoredUpload",
    "TurnUploads",
    "UploadStore",
    "UploadsBackend",
    "message_refs",
    "ref_kwargs",
    "uploads_of",
]
