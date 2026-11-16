"""세션 파일의 목록과 내려받기다(P36 10절, D61).

  GET /sessions/{sid}/files               {files: [{path, size, modified_at, origin}]}
  GET /sessions/{sid}/files/{path:path}   파일 바이트(내려받기 헤더 다섯, downloads.py)

`origin` 은 경로가 가른다. `/uploads/` 아래는 사용자가 업로드해 첨부 저장소(uploads.py)에 한 벌
있는 파일이고(`upload`, 코드 실행 Agent 의 문서 원본은 뺀다), `/workspace/` 아래는 코드 실행
Agent 의 작업 공간 파일이며(`workspace`, P40 6절), 나머지는 에이전트가 파일 도구로 쓴 체크포인트의
`files` 채널이다(`agent`). 체크포인트의 파일은 값이 문자열이라 텍스트뿐이다. deepagents 가 큰 도구
결과와 요약이 밀어낸 대화를 두는 `/large_tool_results/`·`/conversation_history/` 는 내부 경로라
뺀다. 장기 기억(`/memories/`)은 체크포인트가 아니라 저장소에 있어 기억 라우트(memories_api.py)의
몫이다. 목록은 업로드한 파일을 넣은 차례로, 작업 공간 파일과 에이전트 파일을 경로순으로 싣고
500개에서 자른다.

작업 공간 파일은 샌드박스의 찾기와 `/files/list` 로 읽고 샌드박스를 열지 않는다. 샌드박스가 없거나
여는 중이면 목록에 없다. 넣기가 올린 `uploads/`·`skills/` 는 뺀다. 내려받기는 같은 헤더와
상한이고, 저장될 이름(`download_name`)으로 보아 열면 코드가 돌 수 있는 형식(downloads.py
`refused_download`)은 403 file_refused_executable 이다.

`{path}` 는 앞 `/` 를 뗀 파일 경로다. 경로는 체크포인트의 파일 목록과 첨부 저장소의 키에 글자
그대로 맞춰 찾으므로 디렉터리를 거슬러 오를 길이 없지만, `.`·`..` 조각, NUL, 512자 넘는 경로는
먼저 422 invalid_path 다(`checked_file_key`). 없으면 404 file_not_found, 10MiB 를 넘으면 413
file_too_large 다. base64 값은 풀고 deepagents 의 옛 줄 목록 모양은 잇는다. 닫힌 세션은 체크포인트와
첨부가 함께 지워졌으므로 둘 다 410 session_closed 다.

주체는 세션 소유자나 플랫폼이 표시한 승인자(`X-Daegam-Approver: 1`)다. 세션 상세와 달리 프록시
경로도 쿼리 `user_sub` 를 런타임이 직접 대조한다. 응답에 플랫폼이 소유자를 확인할 세션 한 줄이
없기 때문이다. 외부 경로는 approver 가 늘 거짓이라 소유자만이다. 자기 세션 파일 읽기는 세션
상세처럼 관측 이벤트를 남기지 않는다.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any

import anyio
from deepagents.backends.protocol import FileData
from deepagents.backends.utils import file_data_to_string
from fastapi import APIRouter
from starlette.responses import Response

from runtime import stream
from runtime.attachments import MIB, session_uploads
from runtime.auth import Caller, CallerDep
from runtime.downloads import download_headers, download_name, refused_download
from runtime.sandbox import WORKSPACE
from runtime.sessions import STATUS_CLOSED, SessionRecord
from runtime.state import Runtime, RuntimeState
from runtime.turn_gate import check_caller, checked_file_key, error, load_session, session_closed
from runtime.uploads import KIND_ORIGINAL, UPLOADS_ROUTE, StoredUpload

router = APIRouter()

# 목록이 한 번에 싣는 파일 수의 상한이다
SESSION_FILES_MAX = 500
# 내려받을 수 있는 파일 크기의 상한(바이트)이다
SESSION_FILE_MAX_BYTES = 10 * MIB
ORIGIN_UPLOAD = "upload"
ORIGIN_AGENT = "agent"
ORIGIN_WORKSPACE = "workspace"
WORKSPACE_ROUTE = f"{WORKSPACE}/"
# deepagents 가 체크포인트의 files 에 두는 내부 경로다. 사용자가 받을 파일이 아니다
INTERNAL_PREFIXES = ("/large_tool_results/", "/conversation_history/")
# 바이너리 파일 값의 encoding 이다. 글 파일은 "utf-8" 이다
BASE64_ENCODING = "base64"
# 작업 공간 파일의 거절 코드다(P40 6절)
FILE_REFUSED_EXECUTABLE = "file_refused_executable"


async def _readable_session(state: RuntimeState, caller: Caller, session_id: str) -> SessionRecord:
    """파일을 볼 세션 한 줄이다. 없으면 404, 소유자도 승인자도 아니면 403, 닫혔으면 410 이다."""
    record = await load_session(state, session_id)
    check_caller(caller, record, or_approver=True)
    if record.status == STATUS_CLOSED:
        raise session_closed()
    return record


async def _agent_files(state: RuntimeState, session_id: str) -> dict[str, FileData]:
    """체크포인트의 `files` 가운데 에이전트가 쓴 파일이다. 내부 경로와 `/uploads/` 는 뺀다."""
    snapshot = await state.agent.graph.aget_state(stream.thread_config(session_id))
    files = (snapshot.values or {}).get("files") or {}
    hidden = (UPLOADS_ROUTE, *INTERNAL_PREFIXES)
    return {
        path: data
        for path, data in files.items()
        if isinstance(data, dict) and not path.startswith(hidden)
    }


def _file_bytes(data: FileData) -> bytes | None:
    """파일 값의 바이트다. base64 면 풀고 옛 줄 목록 모양은 잇는다. 모양이 틀리면 None 이다."""
    try:
        text = file_data_to_string(data)
    except (KeyError, TypeError):
        return None
    if data.get("encoding") != BASE64_ENCODING:
        return text.encode("utf-8")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _listed_uploads(uploads: list[StoredUpload]) -> list[StoredUpload]:
    """목록에 싣는 첨부다. 문서 원본은 그 글 첨부의 사본이라 뺀다."""
    return [upload for upload in uploads if upload.kind != KIND_ORIGINAL]


def _upload_row(upload: StoredUpload) -> dict[str, Any]:
    return {
        "path": upload.path,
        "size": upload.size,
        "modified_at": upload.created_at.isoformat(),
        "origin": ORIGIN_UPLOAD,
    }


def _agent_rows(files: dict[str, FileData]) -> list[dict[str, Any]]:
    """에이전트 파일의 줄이다. 경로순이고 값의 모양이 틀린 파일은 뺀다."""
    rows: list[dict[str, Any]] = []
    for path in sorted(files):
        content = _file_bytes(files[path])
        if content is None:
            continue
        modified_at = files[path].get("modified_at") or None
        rows.append(
            {"path": path, "size": len(content), "modified_at": modified_at, "origin": ORIGIN_AGENT}
        )
    return rows


async def _file_content(state: RuntimeState, record: SessionRecord, path: str) -> bytes | None:
    """경로의 파일 바이트다. `/uploads/` 는 첨부 저장소, 나머지는 에이전트 파일이다. 없으면 None
    이다."""
    if path.startswith(UPLOADS_ROUTE):
        uploads = _listed_uploads(await session_uploads(state, record))
        return next((upload.content for upload in uploads if upload.path == path), None)
    data = (await _agent_files(state, record.session_id)).get(path)
    return _file_bytes(data) if data is not None else None


@router.get("/sessions/{session_id}/files")
async def list_session_files(session_id: str, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    record = await _readable_session(state, caller, session_id)
    uploads = [_upload_row(u) for u in _listed_uploads(await session_uploads(state, record))]
    agent = _agent_rows(await _agent_files(state, session_id))
    workspace = await _workspace_rows(state, session_id)
    return {"files": [*uploads, *workspace, *agent][:SESSION_FILES_MAX]}


async def _workspace_rows(state: RuntimeState, session_id: str) -> list[dict[str, Any]]:
    """작업 공간 파일의 줄이다. 코드 실행이 꺼졌거나 샌드박스가 없으면 비었다."""
    if state.sandboxes is None:
        return []
    rows: list[dict[str, Any]] = []
    for entry in await state.sandboxes.list_files(session_id):
        modified = entry.get("modified")
        rows.append(
            {
                "path": entry["path"],
                "size": entry["size"],
                "modified_at": _iso(modified),
                "origin": ORIGIN_WORKSPACE,
            }
        )
    return rows


def _iso(seconds: Any) -> str | None:
    if not isinstance(seconds, int | float):
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


async def _workspace_content(state: RuntimeState, session_id: str, path: str) -> bytes | None:
    """작업 공간 파일 하나의 바이트다. 없으면 None 이고, 10MiB 를 넘으면 413 이다."""
    if state.sandboxes is None:
        return None
    found = await state.sandboxes.read_file(session_id, path)
    if found is None or found.error in ("file_not_found", "is_directory", "invalid_path"):
        return None
    if found.error == "too_large":
        raise _too_large()
    return found.content if found.error is None else None


@router.get("/sessions/{session_id}/files/{path:path}")
async def download_session_file(
    session_id: str, path: str, caller: CallerDep, state: Runtime
) -> Response:
    record = await _readable_session(state, caller, session_id)
    file_path = checked_file_key(path)
    in_workspace = file_path.startswith(WORKSPACE_ROUTE)
    if in_workspace:
        content = await _workspace_content(state, session_id, file_path)
    else:
        content = await _file_content(state, record, file_path)
    if content is None:
        raise error(404, "file_not_found", "그 파일이 없다")
    if len(content) > SESSION_FILE_MAX_BYTES:
        raise _too_large()
    # 저장될 이름(`download_name`)으로 본다. 압축을 풀 수 있어 이벤트 루프 밖에서 본다
    if in_workspace and await anyio.to_thread.run_sync(
        refused_download, download_name(file_path), content
    ):
        raise error(
            403,
            FILE_REFUSED_EXECUTABLE,
            "열면 코드가 돌 수 있는 형식이라 작업 공간 파일을 보내지 않는다",
        )
    return Response(content, headers=download_headers(file_path))


def _too_large() -> Exception:
    return error(
        413, "file_too_large", f"파일이 {SESSION_FILE_MAX_BYTES} 바이트를 넘어 보내지 않는다"
    )
