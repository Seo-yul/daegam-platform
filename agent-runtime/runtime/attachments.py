"""채팅이 받는 첨부다(P36 9절, D61). 형식과 상한, 입구 검사, 저장, 모델에게 보일 사람 메시지를
한 곳에서 정한다. 저장소와 모델 요청에 이미지를 채우는 일은 uploads.py 에 있다.

`/chat` 본문의 `attachments` 는 글 첨부 `{kind: "text", name, format, text, sha256?, pages?,
sheets?, truncated?, original?}` 와 이미지 `{kind: "image", name, media_type, data}`(base64)다.
플랫폼은 문서에서 뽑은 글과 화면이 다시 구운 이미지를 싣고, 외부 호출자도 같은 모양을 보낸다. 이미지
매직 바이트와 크기 검사는 런타임에만 있다.

코드 실행 Agent(P40 6절)면 플랫폼이 문서의 원본 바이트도 `original`(base64, 10MiB 까지)로 싣는다.
원본은 같은 이름의 `original` 첨부로 글 바로 뒤에 저장되고, 샌드박스의 `/workspace/uploads/<이름>`
으로만 올라간다(sandbox.py). 원본은 세션 상한 안에 들 때만 저장한다. 넘으면 글만 남기고 안내 줄이
그것을 말한다. 플랫폼이 한 턴의 원본 상한으로 싣지 않은 원본은 `original_dropped: true` 로 오고
안내 줄이 같은 말을 한다. 코드 실행이 꺼진 Agent 는 두 칸을 버린다.

`GET /metrics` 의 `chat` 칸이 `chat_report` 이고 플랫폼은 그것을 상태의 `status.chat` 으로
넘긴다. 화면은 이 값으로 입력칸이 받을 형식과 상한을 정한다. 배포마다 달라지는 사실은 셋이다.

- 문서는 파일 도구로 읽으므로 메인 에이전트의 `builtin_tools.filesystem` 이 켜져 있어야 받는다.
- 이미지는 메인 모델 프로필의 `image_inputs` 가 명시적으로 거짓이 아니면 받는다. deepagents 의
  `UnsupportedContentMiddleware` 와 같은 판정이라 프로필에 칸이 없으면 받는 것으로 본다.
- 세션에 쌓이는 첨부 바이트 합은 세션 색인이 사는 저장소를 따른다. Postgres 는 8MiB 이고, 파드
  RAM 을 나눠 쓰는 Redis 와 memory 노드가 없는 파드 안 저장은 2MiB 다.

입구 검사(`check_attachments`)는 요청 검사 뒤, 세션을 읽기 전에 돈다. 개수(422
too_many_attachments), 받는 종류인가(422 attachments_need_filesystem·images_unsupported), 첨부마다의
모양(422 invalid_attachment), 이 요청만으로 세션 상한을 넘는가(413 attachments_too_large), 글
첨부의 입력 가드레일 차례다. 가드레일은 `deny_pattern` 과 `pii_mask` 막기만 걸고 위반에 첨부
이름이 붙는다. 입력 길이(`max_input_chars`)와 가드레일 `max_length` 는 사용자가 친 말의 상한이라
걸지 않는다. 다시 생성과 고쳐 보내기도 다시 싣는 질문의 글 첨부를 같은 가드레일 검사
(`check_attachment_guard`)로 다시 본다(regenerate.py).

저장(`receive_attachments`)은 턴 리스 안에서 한다. 세션의 기존 첨부와 합쳐 세션 상한을 다시 보고,
겹치는 이름에 ` (2)` 를 붙여 `/uploads/` 경로를 정한 뒤 색인의 `upload_bytes` 를 올리고 저장소에
넣는다. 한 묶음은 같은 시각과 받은 차례(`seq`)를 달아 저장소가 넣은 차례대로 읽는다.
`upload_bytes` 는 호출자가 적은 값이 아니라 저장한 바이트(글의 UTF-8 길이, 이미지는 푼 길이)다.
턴을 여는 사람 메시지는 친 글 끝에 영어 고정 안내(`[Attached files]`)를 붙이고 참조를 단다. 원래
글 형식은 `/uploads/<이름>`, 뽑은 글은 `/uploads/<이름>.txt` 로 보인다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Annotated, Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from runtime import stream
from runtime.auth import Caller
from runtime.config import AgentConfig
from runtime.guards import Violation
from runtime.image_header import read_header
from runtime.sessions import SessionRecord, memory_kind, utcnow
from runtime.state import RuntimeState
from runtime.turn_gate import check_input_guard, rejected
from runtime.turn_record import clean_title, human_message_id
from runtime.uploads import (
    KIND_IMAGE,
    KIND_ORIGINAL,
    KIND_TEXT,
    UPLOADS_ROUTE,
    StoredUpload,
    TurnUploads,
    message_refs,
    ref_kwargs,
)
from runtime.web_guard import attachments_limit_fetch

MIB = 1024 * 1024
MAX_ATTACHMENTS_PER_TURN = 5
MAX_IMAGES_PER_TURN = 4
MAX_SESSION_IMAGES = 8
MAX_IMAGE_BYTES = 2 * MIB
MAX_IMAGE_SIDE = 8000
"""이미지 가로·세로의 상한(px)이다. 화면은 긴 변 1568px 로 다시 구워 올린다."""
MAX_TEXT_CHARS = 500_000
"""글 첨부 하나의 글자 수 상한이다. 플랫폼이 뽑은 글을 같은 값에서 자른다."""
MAX_NAME_CHARS = 120
# 이름을 줄일 때 남기는 확장자의 길이 상한이다. 넘으면 확장자가 아닌 것으로 본다
MAX_EXTENSION_CHARS = 16
MAX_CHAT_BODY_BYTES = 32 * MIB
"""`/chat` 본문의 상한이다(main.py 의 `BodyLimitMiddleware`). 한 턴의 첨부는 다섯 개이고 그 가운데
이미지는 넷까지라, 가장 큰 본문은 글 다섯이거나 이미지 넷과 글 하나다. 글 하나는 한 글자가 JSON
이스케이프로 6바이트여도 약 2.9MiB 이고 이미지 하나는 base64 로 약 2.7MiB 라, 어느 쪽이든 친 글까지
더해 16MiB 안에 든다. 코드 실행 Agent 는 문서의 원본을 더 싣는데, 플랫폼이 한 턴의 원본을 합쳐
8MiB(가장 큰 세션 상한)까지만 싣고 base64 로 약 10.7MiB 라 글 다섯과 더해도 이 상한 안이다."""
MAX_ORIGINAL_BYTES = 10 * MIB
"""문서 원본 하나의 상한이다(P40 6절). 플랫폼의 문서 업로드 상한과 같다."""
# 세션 하나의 첨부 바이트 합이다. 저장소 종류(`memory_kind`)마다 다르고 표에 없으면 기본값이다
SESSION_BYTES_BY_STORE: dict[str, int] = {"postgres": 8 * MIB}
DEFAULT_SESSION_BYTES = 2 * MIB

# 글 첨부의 형식이다. 플랫폼 `daegam/documents/formats.py` 와 같은 이름이고 파리티 테스트가 두 쪽을
# 비교한다. 원래 글 형식은 첨부 이름 그대로의 경로, 문서에서 뽑은 글은 이름 뒤에 `.txt` 를 붙인
# 경로로 보인다
TEXT_FORMATS = ("text", "markdown", "csv", "tsv", "json")
# 문서에서 뽑은 글의 형식과, 안내 줄에서 그 원본을 부르는 영어 이름이다
EXTRACTED_FORMATS = {
    "html": "an HTML page",
    "pdf": "PDF",
    "docx": "a Word document",
    "pptx": "a PowerPoint presentation",
    "xlsx": "a spreadsheet",
    "hwpx": "a Hangul document",
}
EXTRACTED_SUFFIX = ".txt"
# 받는 이미지의 media_type 과 헤더가 말하는 형식이다(image_header.py)
IMAGE_MEDIA_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# 턴을 여는 사람 메시지 끝의 영어 고정 안내다. 첨부 이름에는 줄바꿈이 없어 이 표지가 이름 안에서
# 생기지 않으므로, 마지막 표지 뒤가 언제나 안내다
NOTICE_MARK = "\n\n[Attached files]\n"
NOTICE_READ_HINT = "Read the files with read_file. Long files take offset and limit."
IMAGE_NOTICE = "image attached"

INVALID_ATTACHMENT = "invalid_attachment"
# 이미지 상한을 base64 로 담은 글자 수다. 풀기 전에 이 길이로 먼저 거른다
_MAX_IMAGE_BASE64_CHARS = 4 * -(-MAX_IMAGE_BYTES // 3)
_MAX_ORIGINAL_BASE64_CHARS = 4 * -(-MAX_ORIGINAL_BYTES // 3)
# 안내 줄이 원본을 말하는 꼬리다. 원본이 세션 상한에 들지 않아 저장하지 않았으면 그것을 말한다
ORIGINAL_NOTICE = "commands read the original file at {path}"
ORIGINAL_DROPPED = "the original file was not kept: the conversation's attachment limit is full"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_PATH_SEPARATORS = re.compile(r"[/\\]")


class TextAttachment(BaseModel):
    kind: Literal["text"]
    name: str
    # 플랫폼 형식 이름이다(TEXT_FORMATS, EXTRACTED_FORMATS)
    format: str
    text: str
    # 원본 문서의 sha256 hex 다. 없으면 글의 UTF-8 바이트로 센다
    sha256: str | None = None
    pages: int | None = Field(default=None, ge=0)
    sheets: int | None = Field(default=None, ge=0)
    truncated: bool = False
    # 문서 원본 바이트의 base64 다. 코드 실행 Agent 에만 플랫폼이 싣는다(P40 6절)
    original: str | None = None
    # 원본이 있었지만 플랫폼이 한 턴의 원본 상한(8MiB)으로 싣지 않았다. 안내 줄이 글만 남았다고
    # 말한다
    original_dropped: bool = False


class ImageAttachment(BaseModel):
    kind: Literal["image"]
    name: str
    media_type: str
    # 이미지 바이트의 base64 다
    data: str


AttachmentIn = TextAttachment | ImageAttachment
ChatAttachment = Annotated[AttachmentIn, Field(discriminator="kind")]


class _Invalid(Exception):
    """첨부 하나의 모양이 틀렸다. 입구가 422 invalid_attachment 로 옮긴다."""


def accepts_documents(config: AgentConfig) -> bool:
    """문서 첨부를 받는가. 첨부 글은 `/uploads/` 의 파일이라 파일 도구가 있어야 읽는다."""
    return config.filesystem


def accepts_images(model: BaseChatModel) -> bool:
    """메인 모델이 이미지 입력을 받는가. 프로필이 명시적으로 막을 때만 거짓이다."""
    profile = model.profile if isinstance(model.profile, dict) else {}
    return profile.get("image_inputs") is not False


def session_bytes_limit(config: AgentConfig) -> int:
    """세션 하나에 쌓을 수 있는 첨부 바이트 합이다."""
    return SESSION_BYTES_BY_STORE.get(memory_kind(config), DEFAULT_SESSION_BYTES)


def chat_report(config: AgentConfig, model: BaseChatModel) -> dict[str, Any]:
    """`/metrics` 의 `chat` 칸이다. 메인 에이전트와 메인 모델만 본다."""
    return {
        "documents": accepts_documents(config),
        "images": accepts_images(model),
        # 그래프에 fetch 가 있고 데이터 Tool 이 없으면 첨부가 fetch 를 허용 목록으로 좁힌다(D64).
        # 채팅 입력이 파일을 붙일 때 그것을 알린다
        "attachments_limit_fetch": attachments_limit_fetch(config),
        "limits": {
            "max_attachments_per_turn": MAX_ATTACHMENTS_PER_TURN,
            "max_images_per_turn": MAX_IMAGES_PER_TURN,
            "max_session_images": MAX_SESSION_IMAGES,
            "max_image_bytes": MAX_IMAGE_BYTES,
            "max_session_bytes": session_bytes_limit(config),
        },
    }


# --- 입구 검사 -----------------------------------------------------------------------------


def check_attachments(
    state: RuntimeState,
    caller: Caller,
    items: Sequence[AttachmentIn],
    session_id: str | None,
) -> tuple[list[StoredUpload], list[Violation]]:
    """세션을 읽기 전의 첨부 검사다. 검사를 지난 첨부와 SSE 앞머리에 실을 flag 위반을 돌려준다.
    차례는 모듈 머리에 적었다."""
    if not items:
        return [], []
    who = caller.sub or None
    _check_counts(state, who, items)
    _check_accepted(state, who, items)
    keeps_originals = state.config.code_execution is not None
    uploads = [
        upload
        for index, item in enumerate(items, start=1)
        for upload in _checked(state, who, index, item, keeps_originals=keeps_originals)
    ]
    check_session_quota(
        state, used_bytes=0, used_images=0, incoming=without_originals(uploads), user_sub=who
    )
    return uploads, check_attachment_guard(state, caller, uploads, session_id)


def without_originals(uploads: Sequence[StoredUpload]) -> list[StoredUpload]:
    """원본을 뺀 첨부다. 원본은 세션 상한에 들 때만 저장하므로 상한 검사에서 따로 본다."""
    return [upload for upload in uploads if upload.kind != KIND_ORIGINAL]


def check_attachment_guard(
    state: RuntimeState,
    caller: Caller,
    uploads: Sequence[StoredUpload],
    session_id: str | None,
) -> list[Violation]:
    """글 첨부마다 입력 가드레일로 검사한다. block 이면 첨부 이름을 단 422 이고, 돌려주는 것은
    SSE 앞머리에 실을 flag 위반이다. `/chat` 의 입구와 다시 생성(regenerate.py)이 같이 쓴다."""
    return [
        violation
        for upload in uploads
        if upload.kind == KIND_TEXT
        for violation in check_input_guard(
            state, caller, upload.text, session_id, attachment=upload.name
        )
    ]


def _check_counts(state: RuntimeState, user_sub: str | None, items: Sequence[AttachmentIn]) -> None:
    images = sum(1 for item in items if item.kind == KIND_IMAGE)
    if len(items) > MAX_ATTACHMENTS_PER_TURN or images > MAX_IMAGES_PER_TURN:
        raise rejected(
            state,
            422,
            "too_many_attachments",
            f"한 턴에 첨부는 {MAX_ATTACHMENTS_PER_TURN}개, 그 가운데 이미지는 "
            f"{MAX_IMAGES_PER_TURN}개까지다",
            user_sub=user_sub,
        )


def _check_accepted(
    state: RuntimeState, user_sub: str | None, items: Sequence[AttachmentIn]
) -> None:
    kinds = {item.kind for item in items}
    if KIND_TEXT in kinds and not accepts_documents(state.config):
        raise rejected(
            state,
            422,
            "attachments_need_filesystem",
            "이 에이전트는 파일 Tool 이 꺼져 있어 문서 첨부를 읽지 못한다",
            user_sub=user_sub,
        )
    if KIND_IMAGE in kinds and not accepts_images(state.agent.model):
        raise rejected(
            state,
            422,
            "images_unsupported",
            "이 에이전트의 모델은 이미지를 받지 않는다",
            user_sub=user_sub,
        )


def _checked(
    state: RuntimeState,
    user_sub: str | None,
    index: int,
    item: AttachmentIn,
    *,
    keeps_originals: bool = False,
) -> list[StoredUpload]:
    """첨부 하나를 검사하고 저장할 모양으로 옮긴다. 원본을 받는 Agent 의 글 첨부는 그 원본이 뒤에
    붙는다. 틀리면 몇 번째 첨부인지 말하는 422 다."""
    try:
        name = attachment_name(item.name)
        if isinstance(item, TextAttachment):
            text = _text_upload(item, name)
            if keeps_originals and item.original is not None:
                return [text, _original_upload(item.original, name, item.format)]
            if keeps_originals and item.original_dropped:
                # 빈 원본 자리다. 저장하지 않고 `fitting_originals` 가 빼서 안내 줄이 말한다
                return [text, _upload(KIND_ORIGINAL, name, b"", _EMPTY_SHA256, format=item.format)]
            return [text]
        return [_image_upload(item, name)]
    except _Invalid as e:
        message = f"첨부 {index}: {e}"
        raise rejected(state, 422, INVALID_ATTACHMENT, message, user_sub=user_sub) from e


def attachment_name(raw: str) -> str:
    """첨부 이름이다. 경로 조각을 떼고 제목처럼 정리한 뒤(turn_record.clean_title) 120자로 줄인다.
    빈 이름, `.`·`..`, NUL 은 받지 않는다."""
    if "\x00" in raw:
        raise _Invalid("이름에 NUL 이 있다")
    name = clean_title(_PATH_SEPARATORS.split(raw)[-1])
    if name in ("", ".", ".."):
        raise _Invalid("이름이 비었거나 . 이나 .. 이다")
    return _shortened(name)


def _shortened(name: str, mark: str = "") -> str:
    """이름의 앞부분 뒤에 mark(겹치는 이름의 ` (2)`)를 붙인 이름이다. 120자를 넘으면 앞부분을
    줄이고 확장자는 남긴다."""
    stem, extension = _split_extension(name)
    room = MAX_NAME_CHARS - len(mark) - len(extension)
    if len(stem) > room:
        stem = stem[:room].rstrip()
    return stem + mark + extension


def _split_extension(name: str) -> tuple[str, str]:
    """이름을 앞부분과 확장자(`.` 포함)로 가른다. 앞부분이 비었거나 `.` 뒤가 16자를 넘으면 확장자가
    없는 것으로 본다."""
    stem, dot, extension = name.rpartition(".")
    if not stem or len(extension) > MAX_EXTENSION_CHARS:
        return name, ""
    return stem, dot + extension


def _text_upload(item: TextAttachment, name: str) -> StoredUpload:
    if item.format not in TEXT_FORMATS and item.format not in EXTRACTED_FORMATS:
        raise _Invalid(f"모르는 형식 {item.format!r} 다")
    if not item.text:
        raise _Invalid("글이 비었다")
    if len(item.text) > MAX_TEXT_CHARS:
        raise _Invalid(f"글이 {MAX_TEXT_CHARS:,}자를 넘는다")
    content = item.text.encode("utf-8")
    sha256 = item.sha256 if item.sha256 is not None else hashlib.sha256(content).hexdigest()
    if not _SHA256_RE.fullmatch(sha256):
        raise _Invalid("sha256 은 소문자 hex 64자다")
    return _upload(
        KIND_TEXT,
        name,
        content,
        sha256,
        format=item.format,
        pages=item.pages,
        sheets=item.sheets,
        truncated=item.truncated,
    )


def _image_upload(item: ImageAttachment, name: str) -> StoredUpload:
    expected = IMAGE_MEDIA_TYPES.get(item.media_type)
    if expected is None:
        raise _Invalid(f"이미지 형식은 {', '.join(IMAGE_MEDIA_TYPES)} 가운데 하나다")
    content = _decoded_image(item.data)
    header = read_header(content)
    if header is None or header.format != expected:
        raise _Invalid(f"이미지 바이트가 {item.media_type} 가 아니다")
    if not (0 < header.width <= MAX_IMAGE_SIDE and 0 < header.height <= MAX_IMAGE_SIDE):
        raise _Invalid(f"이미지 가로와 세로는 1~{MAX_IMAGE_SIDE}px 여야 한다")
    sha256 = hashlib.sha256(content).hexdigest()
    return _upload(KIND_IMAGE, name, content, sha256, media_type=item.media_type)


def _original_upload(data: str, name: str, fmt: str) -> StoredUpload:
    """문서 원본이다. 경로는 원래 이름 그대로의 `/uploads/<이름>` 이다."""
    if len(data) > _MAX_ORIGINAL_BASE64_CHARS:
        raise _Invalid(f"원본이 {MAX_ORIGINAL_BYTES // MIB}MiB 를 넘는다")
    try:
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Invalid("원본이 base64 가 아니다") from e
    if not content or len(content) > MAX_ORIGINAL_BYTES:
        raise _Invalid(f"원본은 비지 않은 {MAX_ORIGINAL_BYTES // MIB}MiB 이하여야 한다")
    sha256 = hashlib.sha256(content).hexdigest()
    return _upload(KIND_ORIGINAL, name, content, sha256, format=fmt)


def _decoded_image(data: str) -> bytes:
    """base64 를 푼 이미지 바이트다. 풀기 전에 길이로 상한을 먼저 본다."""
    if len(data) > _MAX_IMAGE_BASE64_CHARS:
        raise _Invalid(f"이미지가 {MAX_IMAGE_BYTES // MIB}MiB 를 넘는다")
    try:
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Invalid("이미지 data 가 base64 가 아니다") from e
    if len(content) > MAX_IMAGE_BYTES:
        raise _Invalid(f"이미지가 {MAX_IMAGE_BYTES // MIB}MiB 를 넘는다")
    return content


def _upload(kind: str, name: str, content: bytes, sha256: str, **extra: Any) -> StoredUpload:
    """저장할 첨부 하나다. 겹치는 이름과 받은 시각·차례는 저장할 때 다시 정한다(`_stored_batch`)."""
    return StoredUpload(
        upload_id=uuid.uuid4().hex,
        kind=kind,
        name=name,
        path=upload_path(name, extra.get("format"), kind),
        size=len(content),
        sha256=sha256,
        content=content,
        created_at=utcnow(),
        **extra,
    )


def upload_path(name: str, fmt: str | None, kind: str = KIND_TEXT) -> str:
    """`/uploads/` 에서의 경로다. 문서에서 뽑은 글이면 이름 뒤에 `.txt` 가 붙는다. 원본은 이름
    그대로다."""
    suffix = EXTRACTED_SUFFIX if kind == KIND_TEXT and fmt in EXTRACTED_FORMATS else ""
    return f"{UPLOADS_ROUTE}{name}{suffix}"


def check_session_quota(
    state: RuntimeState,
    *,
    used_bytes: int,
    used_images: int,
    incoming: Sequence[StoredUpload],
    user_sub: str | None,
) -> None:
    """세션의 첨부 바이트 합과 이미지 수를 넘으면 413 이다. 입구는 이 요청만으로, 리스 안의 저장은
    세션에 이미 있는 것과 합쳐 본다."""
    limit = session_bytes_limit(state.config)
    added = sum(upload.size for upload in incoming)
    images = used_images + sum(1 for upload in incoming if upload.kind == KIND_IMAGE)
    if used_bytes + added <= limit and images <= MAX_SESSION_IMAGES:
        return
    raise rejected(
        state,
        413,
        "attachments_too_large",
        f"세션의 첨부는 {limit // MIB}MiB, 이미지는 {MAX_SESSION_IMAGES}개까지다. "
        "새 대화를 열어 보내야 한다",
        user_sub=user_sub,
    )


# --- 저장과 턴의 입력 ----------------------------------------------------------------------


async def receive_attachments(
    state: RuntimeState,
    record: SessionRecord,
    turn: stream.TurnState,
    text: str,
    incoming: Sequence[StoredUpload],
    turn_id: str,
    *,
    token: str,
) -> tuple[SessionRecord, HumanMessage]:
    """리스 안에서 첨부를 세션에 저장하고 턴을 여는 사람 메시지를 만든다. 턴 문맥의 첨부도 채운다.

    색인의 `upload_bytes` 를 먼저 올린다. 저장소에 넣다 실패해도 합이 실제보다 크게 남을 뿐이라
    상한이 느슨해지지 않는다.
    """
    sid = record.session_id
    existing = await state.store.uploads.read(sid)
    images = sum(1 for upload in existing if upload.kind == KIND_IMAGE)
    plain = without_originals(incoming)
    check_session_quota(
        state,
        used_bytes=record.upload_bytes,
        used_images=images,
        incoming=plain,
        user_sub=record.user_sub,
    )
    room = session_bytes_limit(state.config) - record.upload_bytes - sum(u.size for u in plain)
    fitted, dropped = fitting_originals(incoming, room)
    stored = _stored_batch(fitted, existing)
    total = record.upload_bytes + sum(upload.size for upload in stored)
    # 턴 자신의 쓰기라 리스의 토큰을 건다(P79 2절)
    updated = await state.store.index.update_held(sid, token, upload_bytes=total)
    await state.store.uploads.add(sid, stored)
    turn.uploads = TurnUploads(files=[*existing, *stored], received=len(stored))
    return updated or record, attached_message(text, stored, turn_id, dropped=dropped)


def fitting_originals(
    incoming: Sequence[StoredUpload], room: int
) -> tuple[list[StoredUpload], set[str]]:
    """세션 상한의 남은 몫(room)에 드는 원본만 남긴 묶음과, 원본을 뺀 글 첨부의 upload_id 다.
    플랫폼이 싣지 않은 원본(빈 자리)도 뺀다. 받은 차례대로 넣는다. 원본은 늘 제 글 첨부 바로 뒤에
    온다(`_checked`). 원본을 뺀 글 첨부는 안내 줄이 그것을 말한다."""
    out: list[StoredUpload] = []
    dropped: set[str] = set()
    for upload in incoming:
        if upload.kind == KIND_ORIGINAL:
            # 빈 원본은 플랫폼이 싣지 않은 원본의 자리다(`original_dropped`)
            if not upload.content or upload.size > room:
                if out:
                    dropped.add(out[-1].upload_id)
                continue
            room -= upload.size
        out.append(upload)
    return out, dropped


def _stored_batch(
    incoming: Sequence[StoredUpload], existing: Sequence[StoredUpload]
) -> list[StoredUpload]:
    """세션에 넣을 이번 묶음이다. 세션의 기존 첨부와 이번 첨부끼리 이름이나 경로가 겹치면 ` (2)`
    부터 붙여 가르고, 붙인 뒤에도 120자 안이다. 원본은 바로 앞 글 첨부의 이름을 따른다. 묶음 전체에
    같은 시각과 받은 차례(`seq`)를 달아 저장소가 (시각, 차례)로 읽어도 넣은 차례가 바뀌지 않는다."""
    received_at = utcnow()
    names = {upload.name for upload in existing if upload.kind != KIND_ORIGINAL}
    paths = {upload.path for upload in existing if upload.kind != KIND_ORIGINAL}
    out: list[StoredUpload] = []
    for seq, upload in enumerate(incoming):
        if upload.kind == KIND_ORIGINAL:
            name = out[-1].name if out else upload.name
        else:
            name, n = upload.name, 1
            while name in names or upload_path(name, upload.format) in paths:
                n += 1
                name = _shortened(upload.name, f" ({n})")
        stored = replace(
            upload,
            name=name,
            path=upload_path(name, upload.format, upload.kind),
            created_at=received_at,
            seq=seq,
        )
        if upload.kind != KIND_ORIGINAL:
            names.add(stored.name)
            paths.add(stored.path)
        out.append(stored)
    return out


def attached_message(
    text: str,
    uploads: Sequence[StoredUpload],
    turn_id: str,
    *,
    dropped: set[str] | frozenset[str] = frozenset(),
) -> HumanMessage:
    """첨부를 단 턴의 사람 메시지다. 친 글 끝에 안내를 붙이고 참조(upload_id)를 단다. dropped 는
    원본을 남기지 못한 글 첨부다."""
    return HumanMessage(
        content=text + NOTICE_MARK + attachment_notice(uploads, dropped=dropped),
        id=human_message_id(turn_id),
        additional_kwargs=ref_kwargs([upload.upload_id for upload in uploads]),
    )


def attachment_notice(
    uploads: Sequence[StoredUpload], *, dropped: set[str] | frozenset[str] = frozenset()
) -> str:
    """안내 줄이다. 첨부마다 한 줄이고 글 첨부가 있으면 읽는 법을 덧붙인다. 원본을 남긴 글 첨부는
    원본의 자리를, 원본을 남기지 못한 글 첨부(dropped)는 그것을 덧붙인다(코드 실행 Agent)."""
    kept = {upload.name for upload in uploads if upload.kind == KIND_ORIGINAL}
    lines = [
        _notice_line(upload, original=_original_fact(upload, kept, dropped))
        for upload in uploads
        if upload.kind != KIND_ORIGINAL
    ]
    if any(upload.kind == KIND_TEXT for upload in uploads):
        lines.append(NOTICE_READ_HINT)
    return "\n".join(lines)


def _original_fact(
    upload: StoredUpload, kept: set[str], dropped: set[str] | frozenset[str]
) -> str | None:
    if upload.kind != KIND_TEXT:
        return None
    if upload.name in kept:
        return ORIGINAL_NOTICE.format(path=f"{UPLOADS_ROUTE}{upload.name}")
    return ORIGINAL_DROPPED if upload.upload_id in dropped else None


def _notice_line(upload: StoredUpload, *, original: str | None = None) -> str:
    if upload.kind == KIND_IMAGE:
        return f"- {upload.name}: {IMAGE_NOTICE}"
    source = EXTRACTED_FORMATS.get(upload.format or "")
    facts = [f"text extracted from {source}" if source else "text file"]
    if upload.pages is not None:
        facts.append(_count(upload.pages, "page"))
    if upload.sheets is not None:
        facts.append(_count(upload.sheets, "sheet"))
    chars = f"{len(upload.text):,} characters"
    facts.append(f"truncated to {chars}" if upload.truncated else chars)
    if original is not None:
        facts.append(original)
    return f"- {upload.name}: {upload.path} ({', '.join(facts)})"


def _count(n: int, unit: str) -> str:
    return f"{n:,} {unit}" if n == 1 else f"{n:,} {unit}s"


def _split_notice(message: HumanMessage) -> tuple[str, str]:
    """친 글과 안내(표지 포함)다. 참조가 없는 메시지는 안내가 없다."""
    text = message.text
    if message_refs(message):
        typed, mark, notice = text.rpartition(NOTICE_MARK)
        if mark:
            return typed, mark + notice
    return text, ""


def typed_text(message: HumanMessage) -> str:
    """사용자가 친 글이다. 전사의 말풍선, 관측 이벤트, 다시 생성의 입력 검사가 이 글을 본다."""
    return _split_notice(message)[0]


def notice_of(message: HumanMessage) -> str:
    """메시지 끝의 첨부 안내다. 고쳐 보내기가 바꾼 글 뒤에 다시 붙인다."""
    return _split_notice(message)[1]


def turn_attachments(turn: stream.TurnState, turn_input: Any) -> list[StoredUpload]:
    """턴을 연 사람 메시지가 단 첨부다. 재개 턴과 첨부 없는 글이면 빈 목록이다. 원본은 그 글 첨부의
    사본이라 전사와 관측 이벤트에 따로 싣지 않는다."""
    return without_originals(turn.uploads.select(message_refs(turn_input)))


def observed(
    uploads: Sequence[StoredUpload], *, body: Callable[[Any], Any]
) -> list[dict[str, Any]] | None:
    """`turn_start` 의 `attachments` 칸이다. 크기와 해시를 싣고, 이름은 본문처럼
    body(`EventLog.body` 로 record_bodies 일 때만 pii_mask 를 지난 값, 아니면 None)를 지난 값이 있을
    때만 싣는다. 첨부가 없으면 None 이라 칸이 빠진다."""
    if not uploads:
        return None
    out: list[dict[str, Any]] = []
    for upload in uploads:
        item: dict[str, Any] = {"kind": upload.kind, "bytes": upload.size, "sha256": upload.sha256}
        if (name := body(upload.name)) is not None:
            item["name"] = name
        out.append(item)
    return out


async def session_uploads(state: RuntimeState, record: SessionRecord) -> list[StoredUpload]:
    """세션의 첨부다. 첨부를 저장한 적 없는 세션(`upload_bytes` 0)은 저장소를 읽지 않는다. 턴
    문맥, 다시 생성의 첨부 검사(regenerate.py), 세션 파일 목록(session_files.py)이 같이 쓴다."""
    if not record.upload_bytes:
        return []
    return await state.store.uploads.read(record.session_id)


async def load_turn_uploads(
    state: RuntimeState, record: SessionRecord, turn: stream.TurnState
) -> None:
    """턴이 볼 세션의 첨부를 저장소에서 한 번 읽어 턴 문맥에 둔다. 이번 요청이 받은 첨부를 저장할
    때나 다시 생성이 질문의 첨부를 검사할 때 이미 채웠으면 읽지 않는다."""
    if not turn.uploads.files and (files := await session_uploads(state, record)):
        turn.uploads = TurnUploads(files=files)
