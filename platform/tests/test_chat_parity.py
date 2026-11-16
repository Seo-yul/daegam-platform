"""P36: 세션 제목 상한, 런타임 능력 이름, 피드백과 장기 기억의 이름·상한의 사본 파리티 (D61, D65).

세션 제목 상한의 원본은 런타임 `agent-runtime/runtime/turn_record.py` 다. 플랫폼
`api/schemas/agents.py` 는 이름 바꾸기 본문과 제목 검색 q 를 같은 값으로 거르고, 화면
`ui/src/lib/sessionTitle.ts` 는 입력칸 상한과 자동 제목을 같은 값으로 만든다. 입력칸 상한은
오류 사전도 읽어 `ui/src/lib/limits.ts` 에 있다. 화면 `ui/src/lib/runtimeFeatures.ts` 의
`RUNTIME_*` 는 런타임 능력 이름이라 런타임 `features.py` 의 `CAPABILITIES` 안에 있어야 한다.
어긋나면 화면이 서버가 거절할 글을 받거나 오지 않을 능력을 기다린다. 런타임 쪽이 원본이므로
플랫폼과 화면의 사본을 그 값에 맞춘다.

답변 피드백은 플랫폼에만 있어 의견 상한과 구간의 원본이 플랫폼 `store/agent_feedback.py`·
`services/agent_feedback.py` 이고, 화면 `ui/src/lib/feedback.ts` 의 입력칸 상한과 안내의 날 수가
같은 값이어야 한다. 내보내기 구간은 오류 사전도 읽어 `ui/src/lib/limits.ts` 에 있다. 수집기 훅이
읽는 런타임 관측 이벤트 이름과 `turn_end` 의 칸 이름은 런타임 `events.py` 가 원본이다. 어긋나면
훅이 평가를 지우지도 표시하지도 못하는데 아무도 모른다. 장기 기억의 뿌리 경로와 파일 내용 상한은
런타임 `long_term.py`·`memories_api.py` 가 원본이고 화면
`ui/src/lib/memories.ts` 가 사본을 둔다. 이 둘은 deepagents 를 import 하므로 싣지 않고 값의 식만
푼다. 채팅 첨부의 형식은 거꾸로 플랫폼 `daegam/documents/formats.py` 가 원본이다. 런타임
`attachments.py` 가 받는 글 형식과 이미지 종류, 글자 수 상한이 그 표와 같아야 플랫폼이 뽑은 글을
런타임이 거절하지 않고 `/uploads/` 경로 규칙(원래 글과 뽑은 글)도 같은 형식을 가른다. 한 턴의
첨부 수와 이미지 하나의 바이트 상한은 런타임이 원본이고, 플랫폼은 본문 검사와 업로드에서 먼저
거른다. 화면 `ui/src/lib/attachments.ts` 의 이미지 확장자와 상한 기본값도 같은 값이다. 세션 파일의
다운로드 상한과 업로드한 파일의 뿌리는 런타임 `session_files.py`·`uploads.py` 가 원본이고 화면
`ui/src/lib/sessionFiles.ts` 가 사본을 둔다.
"""

from __future__ import annotations

import re

from daegam.api.schemas import agents as schemas
from daegam.documents import formats
from daegam.documents.result import MAX_UPLOAD_CHARS
from daegam.services import agent_feedback, chat_uploads
from daegam.services.telemetry import normalize
from daegam.store.agent_feedback import MAX_COMMENT_CHARS
from tests.helpers.source import (
    AGENT_RUNTIME,
    UI_LIB,
    py_constant,
    read_source,
    runtime_module,
    ts_number,
    ts_string,
)

TURN_RECORD = AGENT_RUNTIME / "turn_record.py"
SESSION_TITLE_TS = UI_LIB / "sessionTitle.ts"
LIMITS_TS = UI_LIB / "limits.ts"
RUNTIME_FEATURES_TS = UI_LIB / "runtimeFeatures.ts"
FEEDBACK_TS = UI_LIB / "feedback.ts"
MEMORIES_TS = UI_LIB / "memories.ts"
RUNTIME_EVENTS = AGENT_RUNTIME / "events.py"
RUNTIME_MEMORIES_API = AGENT_RUNTIME / "memories_api.py"
RUNTIME_ATTACHMENTS = AGENT_RUNTIME / "attachments.py"
ATTACHMENTS_TS = UI_LIB / "attachments.ts"
RUNTIME_SESSION_FILES = AGENT_RUNTIME / "session_files.py"
RUNTIME_UPLOADS = AGENT_RUNTIME / "uploads.py"
SESSION_FILES_TS = UI_LIB / "sessionFiles.ts"
FIX = "런타임 turn_record.py·features.py 가 원본이다. 플랫폼과 화면의 사본을 그 값에 맞춘다"

# `export const RUNTIME_CANCEL = "cancel";` 처럼 런타임 능력 이름은 늘 따옴표 글자다
_RUNTIME_NAME_RE = re.compile(r'export const RUNTIME_[A-Z0-9_]+\s*=\s*"([^"]*)"\s*;')


def test_platform_title_limits_match_the_runtime() -> None:
    runtime = runtime_module(TURN_RECORD)
    assert schemas.SESSION_TITLE_MAX_CHARS == runtime.SESSION_TITLE_MAX_CHARS, FIX
    assert schemas.SESSION_TITLE_RAW_MAX_CHARS == runtime.SESSION_TITLE_RAW_MAX_CHARS, FIX


def test_screen_title_limits_match_the_runtime() -> None:
    runtime = runtime_module(TURN_RECORD)
    copies = ((LIMITS_TS, "SESSION_TITLE_MAX_CHARS"), (SESSION_TITLE_TS, "AUTO_TITLE_MAX_CHARS"))
    for path, name in copies:
        assert ts_number(path, name) == getattr(runtime, name), f"{name}: {FIX}"


def test_screen_capabilities_are_runtime_capabilities() -> None:
    """화면이 기다리는 이름은 모두 런타임이 아는 이름(능력과 문서 기능, `KNOWN`)이다. 이미지는 둘을
    함께 알리고 화면은 `tool_safety` 같은 문서 기능도 본다(P43). 런타임에만 있는 이름은 화면이 아직
    쓰지 않는 것이라 괜찮다."""
    names = _RUNTIME_NAME_RE.findall(read_source(RUNTIME_FEATURES_TS))
    assert names, f"{RUNTIME_FEATURES_TS} 에서 RUNTIME_* 를 찾지 못했다"
    known = runtime_module(AGENT_RUNTIME / "features.py").KNOWN
    assert set(names) <= set(known), FIX


def test_screen_feedback_comment_limit_matches_the_platform() -> None:
    """화면 입력칸이 서버가 거절할 길이의 의견을 받지 않는다."""
    assert ts_number(FEEDBACK_TS, "FEEDBACK_COMMENT_MAX_CHARS") == MAX_COMMENT_CHARS


def test_screen_feedback_windows_match_the_platform() -> None:
    """내보내기 구간과 탭 머리의 수가 세는 기간은 서버가 정한 날 수 그대로 말한다."""
    assert ts_number(LIMITS_TS, "FEEDBACK_EXPORT_MAX_DAYS") == agent_feedback.EXPORT_MAX_DAYS
    assert ts_number(FEEDBACK_TS, "FEEDBACK_RECENT_DAYS") == agent_feedback.RECENT_DAYS


def test_feedback_hook_reads_the_runtime_event_names() -> None:
    """수집기 훅과 TelemetryWriter 가 고르는 이벤트와 칸 이름이 런타임이 내는 이름이다."""
    for name in normalize.RUNTIME_EVENT_NAMES:
        assert getattr(normalize, name) == py_constant(RUNTIME_EVENTS, name), name
    body_keys = py_constant(RUNTIME_EVENTS, "BODY_FIELDS") + py_constant(
        RUNTIME_EVENTS, "NAME_FIELDS"
    )
    assert normalize.BODY_KEYS == body_keys


def test_screen_memory_root_and_file_limit_match_the_runtime() -> None:
    """화면이 경로에서 떼는 뿌리와 안내하는 내용 상한이 런타임의 값이다."""
    assert ts_string(MEMORIES_TS, "MEMORY_ROOT") == py_constant(
        AGENT_RUNTIME / "long_term.py", "MEMORY_ROUTE"
    )
    assert ts_number(MEMORIES_TS, "MEMORY_FILE_MAX_BYTES") == py_constant(
        RUNTIME_MEMORIES_API, "MEMORY_FILE_MAX_BYTES"
    )


def test_runtime_attachment_formats_match_the_platform() -> None:
    """런타임이 받는 글 형식(원래 글과 뽑은 글), 이미지 종류, 글자 수 상한이 플랫폼 형식 표의
    값이다."""
    assert set(py_constant(RUNTIME_ATTACHMENTS, "TEXT_FORMATS")) == formats.TEXT_FORMATS
    assert set(py_constant(RUNTIME_ATTACHMENTS, "EXTRACTED_FORMATS")) == formats.PARSED_FORMATS
    assert py_constant(RUNTIME_ATTACHMENTS, "IMAGE_MEDIA_TYPES") == {
        formats.CONTENT_TYPES[fmt]: fmt for fmt in formats.IMAGE_FORMATS
    }
    assert py_constant(RUNTIME_ATTACHMENTS, "MAX_TEXT_CHARS") == MAX_UPLOAD_CHARS


def test_turn_and_image_limits_match_the_runtime() -> None:
    """한 턴의 첨부 수와 이미지 하나의 바이트 상한이 런타임과 같다. 화면은 런타임이 상한을 알리지
    않을 때의 기본값으로 같은 수와 바이트를 쓴다."""
    max_turn = py_constant(RUNTIME_ATTACHMENTS, "MAX_ATTACHMENTS_PER_TURN")
    assert chat_uploads.MAX_ATTACHMENTS_PER_TURN == max_turn
    max_image_bytes = py_constant(RUNTIME_ATTACHMENTS, "MAX_IMAGE_BYTES")
    assert formats.MAX_IMAGE_BYTES == max_image_bytes
    assert ts_number(ATTACHMENTS_TS, "DEFAULT_MAX_IMAGE_BYTES") == max_image_bytes
    assert ts_number(ATTACHMENTS_TS, "DEFAULT_MAX_ATTACHMENTS") == max_turn
    images = py_constant(RUNTIME_ATTACHMENTS, "MAX_IMAGES_PER_TURN")
    assert ts_number(ATTACHMENTS_TS, "DEFAULT_MAX_IMAGES") == images


def test_screen_image_extensions_match_the_platform() -> None:
    """화면 입력칸이 받는 이미지 확장자가 플랫폼 형식 표의 것이다."""
    accept = ts_string(ATTACHMENTS_TS, "IMAGE_ACCEPT")
    assert tuple(sorted(accept.split(","))) == formats.IMAGE_EXTENSIONS


def test_screen_session_file_root_and_limit_match_the_runtime() -> None:
    """화면이 업로드한 파일을 가르는 뿌리와 외부 호출 카드가 안내하는 다운로드 상한이 런타임의
    값이다."""
    assert ts_string(SESSION_FILES_TS, "UPLOADS_ROOT") == py_constant(
        RUNTIME_UPLOADS, "UPLOADS_ROUTE"
    )
    assert ts_number(SESSION_FILES_TS, "SESSION_FILE_MAX_BYTES") == py_constant(
        RUNTIME_SESSION_FILES, "SESSION_FILE_MAX_BYTES"
    )
