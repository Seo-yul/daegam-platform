"""세션 파일의 목록과 내려받기다(P36 10절, runtime/session_files.py·downloads.py).

에이전트가 write_file 로 쓴 파일과 업로드한 첨부가 라우트로 가른 `origin` 으로 목록에 오는 것,
deepagents 내부 경로가 빠지는 것, utf-8·base64·옛 줄 목록 값을 바이트로 내는 것, 경로 검사
(`..`·`%2e%2e`·NUL·긴 경로)와 없는 키, 10MiB 와 500개 상한, 닫힌 세션의 410, 승인자 헤더와 소유자가
아닌 프록시 호출의 403, 헤더 다섯이 플랫폼과 같은 표본에 맞는 것, CORS 가 `Content-Disposition` 을
드러내는 것을 본다. 실제 Postgres·Redis 에서는 체크포인트의 파일과 첨부 저장소를 함께 읽는다.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any

import httpx
import pytest

from runtime import stream
from runtime.downloads import download_headers, download_name
from runtime.hitl import APPROVER_HEADER
from runtime.session_files import SESSION_FILE_MAX_BYTES, SESSION_FILES_MAX
from runtime.sessions import utcnow
from runtime.state import Settings
from tests.app_fixtures import BAD_KEYS, ORIGIN, sid_of, status_and_code, tool_message
from tests.attachment_fixtures import TEXT, document, image, png, send
from tests.conftest import RunningApp, load_fixture, run_app
from tests.store_fixtures import store_for

ALICE = {"user_sub": "alice"}
BOB = {"user_sub": "bob"}
REPORT = "/reports/요약.md"
REPORT_TEXT = "# 요약\n환불은 7일 안에 한다."
UPLOADED = "/uploads/보고서.pdf.txt"
NOW = "2026-10-03T00:00:00+00:00"
HEADER_CASES: list[dict[str, Any]] = load_fixture("chat/download-headers.json")["cases"]


async def _write_turn(running: RunningApp) -> str:
    """보고서 PDF 의 글을 첨부하고 스텁이 write_file 로 REPORT 를 쓰는 첫 턴이다. 세션 id 다."""
    message = tool_message("write_file", file_path=REPORT, content=REPORT_TEXT)
    response, sse = await send(running, [document(text=TEXT)], message)
    assert response.status_code == 200, response.text
    return sid_of(sse)


def _value(content: Any, encoding: str = "utf-8") -> dict[str, Any]:
    return {"content": content, "encoding": encoding, "created_at": NOW, "modified_at": NOW}


async def _put_files(running: RunningApp, sid: str, files: dict[str, Any]) -> None:
    """체크포인트의 files 채널에 값을 바로 넣는다. 파일 도구가 쓰지 않는 모양(base64, 옛 줄 목록,
    내부 경로, 큰 파일)을 만든다."""
    await running.runtime.agent.graph.aupdate_state(stream.thread_config(sid), {"files": files})


async def _listed(running: RunningApp, sid: str) -> list[dict[str, Any]]:
    response = await running.client.get(f"/sessions/{sid}/files", params=ALICE)
    assert response.status_code == 200, response.text
    return response.json()["files"]


async def _download(
    running: RunningApp,
    sid: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    query = ALICE if params is None else params
    return await running.client.get(f"/sessions/{sid}/files/{path}", params=query, headers=headers)


async def test_uploads_and_agent_files_are_listed_by_their_route(files_doc) -> None:
    """`/uploads/` 아래는 업로드한 파일(`upload`), 나머지는 에이전트가 쓴 파일(`agent`)이다.
    deepagents 의 큰 결과와 대화 기록 경로는 사용자가 받을 파일이 아니라 빠진다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        internal = {
            "/large_tool_results/call-1": _value("큰 결과"),
            "/conversation_history/s.md": _value("밀려난 대화"),
        }
        await _put_files(running, sid, internal)

        upload, agent = await _listed(running, sid)
        assert (upload["path"], upload["origin"]) == (UPLOADED, "upload")
        assert upload["size"] == len(TEXT.encode("utf-8")) and upload["modified_at"]
        assert (agent["path"], agent["origin"]) == (REPORT, "agent")
        assert agent["size"] == len(REPORT_TEXT.encode("utf-8")) and agent["modified_at"]
        assert set(agent) == {"path", "size", "modified_at", "origin"}


async def test_files_come_back_as_bytes_with_the_download_headers(files_doc) -> None:
    """에이전트 파일과 업로드한 글·이미지가 저장한 바이트 그대로 오고, 헤더 다섯은 형식을 짐작하지
    못하는 첨부다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        response = await _download(running, sid, REPORT.lstrip("/"))
        assert response.status_code == 200, response.text
        assert response.content == REPORT_TEXT.encode("utf-8")
        expected = {k.lower(): v for k, v in download_headers(REPORT).items()}
        assert {k: response.headers.get(k) for k in expected} == expected

        response = await _download(running, sid, UPLOADED.lstrip("/"))
        assert response.content == TEXT.encode("utf-8")
        await send(running, [image()], "그림을 봐 줘", session_id=sid)
        response = await _download(running, sid, "uploads/화면.png")
        assert response.status_code == 200 and response.content == png()
        assert "filename*=UTF-8''%ED%99%94%EB%A9%B4.png" in response.headers["content-disposition"]


async def test_base64_values_become_bytes(files_doc) -> None:
    """base64 값은 푼다. 풀 수 없는 값은 목록에도 없고 받으면 404 다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        files = {
            "/logo.png": _value(base64.b64encode(png()).decode(), "base64"),
            "/broken.bin": _value("!!", "base64"),
        }
        await _put_files(running, sid, files)

        sizes = {row["path"]: row["size"] for row in await _listed(running, sid)}
        assert sizes["/logo.png"] == len(png()) and "/broken.bin" not in sizes
        assert (await _download(running, sid, "logo.png")).content == png()
        assert status_and_code(await _download(running, sid, "broken.bin")) == (
            404,
            "file_not_found",
        )


@pytest.mark.parametrize("path", BAD_KEYS)
async def test_dot_segments_nul_and_long_paths_are_refused(files_doc, path: str) -> None:
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        assert status_and_code(await _download(running, sid, path)) == (422, "invalid_path")


async def test_missing_and_internal_paths_are_not_found(files_doc) -> None:
    """경로는 글자 그대로 맞춰 찾는다. 없는 파일, 내부 경로, 다른 이름의 첨부는 404 다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        await _put_files(running, sid, {"/large_tool_results/call-1": _value("큰 결과")})
        for path in ("nope.md", "large_tool_results/call-1", "uploads/보고서.pdf", "reports"):
            assert status_and_code(await _download(running, sid, path)) == (
                404,
                "file_not_found",
            ), path


async def test_size_and_count_limits(files_doc) -> None:
    """10MiB 를 넘는 파일은 크기와 함께 목록에 오고 받으면 413 이다. 목록은 500개에서 자른다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        await _put_files(running, sid, {"/big.txt": _value("a" * (SESSION_FILE_MAX_BYTES + 1))})
        sizes = {row["path"]: row["size"] for row in await _listed(running, sid)}
        assert sizes["/big.txt"] == SESSION_FILE_MAX_BYTES + 1
        assert status_and_code(await _download(running, sid, "big.txt")) == (413, "file_too_large")

        many = {f"/notes/{i:03d}.md": _value("x") for i in range(SESSION_FILES_MAX)}
        await _put_files(running, sid, many)
        rows = await _listed(running, sid)
        assert len(rows) == SESSION_FILES_MAX and rows[0]["path"] == UPLOADED


async def test_closed_sessions_answer_410(files_doc) -> None:
    """닫히면 체크포인트와 첨부가 함께 지워졌으므로 목록과 내려받기 모두 410 이다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        assert await running.runtime.store.close_expired(utcnow() + timedelta(days=1)) == [sid]
        listed = await running.client.get(f"/sessions/{sid}/files", params=ALICE)
        assert status_and_code(listed) == (410, "session_closed")
        assert status_and_code(await _download(running, sid, REPORT.lstrip("/"))) == (
            410,
            "session_closed",
        )


async def test_only_the_owner_or_a_marked_approver_reads(files_doc) -> None:
    """세션 상세와 달리 프록시 경로도 런타임이 쿼리의 주체를 대조한다. 다른 사람과 주체 없는
    호출은 403 이고, 플랫폼이 승인자로 표시한 호출은 남의 세션도 읽는다. 없는 세션은 404 다."""
    async with run_app(files_doc) as running:
        sid = await _write_turn(running)
        path = REPORT.lstrip("/")
        for params in (BOB, {}):
            listed = await running.client.get(f"/sessions/{sid}/files", params=params)
            assert status_and_code(listed) == (403, "not_session_owner")
            response = await _download(running, sid, path, params=params)
            assert status_and_code(response) == (403, "not_session_owner")

        approver = {APPROVER_HEADER: "1"}
        listed = await running.client.get(f"/sessions/{sid}/files", params=BOB, headers=approver)
        assert listed.status_code == 200 and len(listed.json()["files"]) == 2
        response = await _download(running, sid, path, params=BOB, headers=approver)
        assert response.content == REPORT_TEXT.encode("utf-8")
        missing = await running.client.get("/sessions/nope/files", params=ALICE)
        assert status_and_code(missing) == (404, "session_not_found")


@pytest.mark.parametrize("case", HEADER_CASES, ids=[c["filename"] for c in HEADER_CASES])
def test_headers_match_the_platform_fixture(case: dict[str, Any]) -> None:
    """플랫폼 `api/_download.py` 와 같은 표본과 비교한다. 외부 호출자가 직접 받든 플랫폼을 거치든
    같은 헤더다."""
    assert download_name(case["path"]) == case["filename"]
    headers = download_headers(case["path"], media_type=case["media_type"])
    assert {k.lower(): v for k, v in headers.items()} == case["headers"]


async def test_cors_exposes_the_download_name(files_doc) -> None:
    """브라우저 클라이언트가 저장 이름을 읽도록 `Content-Disposition` 을 드러낸다."""
    settings = Settings(cors_origins=(ORIGIN,))
    async with run_app(files_doc, settings=settings) as running:
        sid = await _write_turn(running)
        response = await _download(running, sid, REPORT.lstrip("/"), headers={"Origin": ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
        exposed = response.headers["Access-Control-Expose-Headers"].lower().split(",")
        assert "content-disposition" in {h.strip() for h in exposed}


@pytest.mark.parametrize("kind", ["postgres", "redis"])
async def test_real_stores_read_checkpoint_files_and_uploads(files_doc, kind) -> None:
    """체크포인트의 `files`(50 갱신마다 통째 스냅숏인 DeltaChannel)와 첨부 저장소를 실제 저장소에서
    다시 읽어도 같은 목록과 바이트다."""
    async with store_for(kind, files_doc) as store, run_app(files_doc, store=store) as running:
        sid = await _write_turn(running)
        try:
            paths = [(row["path"], row["origin"]) for row in await _listed(running, sid)]
            assert paths == [(UPLOADED, "upload"), (REPORT, "agent")]
            response = await _download(running, sid, REPORT.lstrip("/"))
            assert response.content == REPORT_TEXT.encode("utf-8")
            response = await _download(running, sid, UPLOADED.lstrip("/"))
            assert response.content == TEXT.encode("utf-8")
        finally:
            await running.client.delete(f"/sessions/{sid}", params=ALICE)
