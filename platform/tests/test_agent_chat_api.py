"""P36 F3·F4·F12: Agent 채팅의 세션 관리와 다시 생성, 세션 파일(`api/agent_chat.py`), 목록의 제목
검색.

가짜 런타임(tests/helpers/runtime.py 의 `fake_runtime`)에 세션 관리 두 라우트, 다시 생성, 세션 파일
두 라우트를 달아 본다. 세션 관리와 다시 생성의 주체가 언제나 호출자 sub 이고 승인자 헤더를 붙이지
않는 것, 목록의 `q` 를 넘기는 것을 든다. 세션 파일은 세션 상세와 같이 승인자 헤더를 붙이는 것,
런타임이 더한 칸을 그대로 넘기는 것, 세션 id 와 파일 경로를 조각마다 다시 인코딩하는 것을 본다.
런타임의 헤더를 버리고 다시 만드는 것과 런타임 오류의 형식은 test_agent_runtime_proxy.py 가 본다.

Agent 채팅 라우트 가운데 런타임 능력에 기대는 것(세션 관리와 제목 검색·다시 생성·세션 파일·내
기억·인용 원문·업로드)은 기록에 그 이름이 없으면 런타임을 부르지 않고 409 runtime_feature_missing
인 것을 능력 표 하나로 든다. 그 라우트를 모르는 옛 파드의 409 runtime_rolling 은 세션 관리로 본다.

인용 원문은 test_agent_citations.py, 내 기억은 test_agent_memories.py, 답변 피드백은
test_agent_feedback.py, 업로드는 test_chat_uploads.py 가 든다. 가짜 런타임을 꾸미는
도우미(`with_transcript`·`add_memories`·`runtime_refusal`)는 tests/helpers/runtime.py 에 있고 이
파일과 그 파일들이 함께 쓴다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from daegam.auth import rbac
from daegam.services.agent_features import (
    ATTACHMENTS,
    CANCEL,
    CAPABILITIES,
    CITATIONS,
    MEMORY_FILES,
    REGENERATE,
    RUNTIME_FEATURE_MISSING,
    RUNTIME_ROLLING,
    SESSION_ADMIN,
    SESSION_FILES,
)
from daegam.services.agent_runtime import APPROVER_HEADER, file_segments, session_path
from tests.helpers.agent_runtime import DeployedAgent, chat_upload, deployed_agent
from tests.helpers.auth import as_principal
from tests.helpers.consts import AG
from tests.helpers.http import detail_code
from tests.helpers.runtime import (
    ADMIN_FEATURES,
    FILES_FEATURES,
    add_memories,
    add_session_admin,
    add_session_files,
    record_call,
    runtime_refusal,
    sse,
)

REGENERATE_FEATURES = "cancel,session_admin,regenerate"


def _admin_calls(runtime: FastAPI) -> list[dict[str, Any]]:
    return [c for c in runtime.state.calls if c.get("method") in ("PATCH", "DELETE")]


def test_owner_renames_and_deletes_without_the_approver_header(client, fake_clusters, mongo):
    """주체는 호출자 sub 이고 agent:approve 가 있어도 승인자 헤더를 붙이지 않는다. 응답은 런타임의
    세션 한 줄과 지운 결과 그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=ADMIN_FEATURES)
    add_session_admin(agent.runtime)
    url = f"{agent.url}/sessions"

    r = client.patch(f"{url}/s1", json={"title": "  환불 기한  "})
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert (session["session_id"], session["user_sub"], session["title"]) == (
        "s1",
        "header",
        "환불 기한",
    )
    call = _admin_calls(agent.runtime)[-1]
    assert (call["method"], call["path"], call["params"]) == (
        "PATCH",
        "/sessions/s1",
        {"user_sub": "header"},
    )
    assert call["body"] == {"title": "  환불 기한  "}
    assert APPROVER_HEADER.lower() not in call["headers"]

    r = client.delete(f"{url}/s1")
    assert r.status_code == 200 and r.json() == {"session_id": "s1", "deleted": True}
    call = _admin_calls(agent.runtime)[-1]
    assert (call["method"], call["params"]) == ("DELETE", {"user_sub": "header"})
    assert APPROVER_HEADER.lower() not in call["headers"]

    # 본문 검사는 플랫폼이 한다
    assert client.patch(f"{url}/s1", json={"title": ""}).status_code == 422
    assert client.patch(f"{url}/s1", json={"title": "x", "extra": 1}).status_code == 422
    assert client.delete(f"{AG}/nope/sessions/s1").status_code == 404


def _add_regenerate(runtime: FastAPI) -> None:
    """가짜 런타임에 다시 생성을 단다. `other` 는 남의 세션, `pending` 은 승인 대기 세션, `empty` 는
    다시 만들 턴이 없는 세션이다."""
    refusals = {
        "other": (403, "not_session_owner", "세션 소유자가 아니다"),
        "pending": (409, "session_pending", "승인 대기 중인 세션이다"),
        "empty": (409, "nothing_to_regenerate", "다시 만들 턴이 없다"),
    }

    @runtime.post("/sessions/{sid}/regenerate")
    async def regenerate(sid: str, request: Request):
        body = await request.json()
        record_call(runtime, request, sid=sid, body=body)
        if sid in refusals:
            return runtime_refusal(*refusals[sid])

        async def events():
            yield sse("token", {"text": "다시 만든 답"})
            yield sse("done", {"session_id": sid, "turn": 2, "turn_id": "t-new", "tool_calls": 0})

        return StreamingResponse(events(), media_type="text/event-stream")


def _regenerate_calls(runtime: FastAPI) -> list[dict[str, Any]]:
    return [c for c in runtime.state.calls if c["path"].endswith("/regenerate")]


def test_owner_regenerates_and_edits_over_sse(client, fake_clusters, mongo):
    """주체는 호출자 sub 이고 승인자 헤더를 붙이지 않는다. 응답은 런타임의 SSE 그대로이고 고쳐
    보낸 글과 ephemeral 이 본문에 실린다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=REGENERATE_FEATURES)
    _add_regenerate(agent.runtime)
    url = f"{agent.url}/sessions/s1/regenerate"

    r = client.post(url, json={})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in r.text and '"turn_id": "t-new"' in r.text
    call = _regenerate_calls(agent.runtime)[-1]
    assert call["body"] == {"user_sub": "header", "message": None, "ephemeral": False}
    assert APPROVER_HEADER.lower() not in call["headers"]

    r = client.post(url, json={"message": "고친 질문", "ephemeral": True})
    assert r.status_code == 200, r.text
    assert _regenerate_calls(agent.runtime)[-1]["body"] == {
        "user_sub": "header",
        "message": "고친 질문",
        "ephemeral": True,
    }
    # 본문 검사는 플랫폼이 한다
    assert client.post(url, json={"message": ""}).status_code == 422
    assert client.post(url, json={"session_id": "s2"}).status_code == 422
    r = client.post(f"{agent.url}/sessions/%2E%2E/regenerate", json={})
    assert r.status_code == 200 and _regenerate_calls(agent.runtime)[-1]["sid"] == ".."


# --- 세션 파일 (F12) -------------------------------------------------------------------------


def _file_calls(runtime: FastAPI) -> list[dict[str, Any]]:
    return [c for c in runtime.state.calls if "/files" in c["path"]]


def _files_url(agent_id: str, sid: str = "s1", path: str | None = None) -> str:
    url = f"{AG}/{agent_id}/sessions/{sid}/files"
    return url if path is None else f"{url}/{path}"


def test_files_follow_the_session_detail_rules(client, fake_clusters, mongo):
    """주체는 호출자 sub 이고 agent:approve 가 있으면 승인자 헤더를 붙여 남의 세션도 읽는다.
    approve 가 없는 viewer 는 헤더 없이 가서 남의 세션이 런타임의 403 이다. 목록은 런타임이 더한
    칸까지 그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=FILES_FEATURES)
    add_session_files(agent.runtime)

    r = client.get(_files_url(agent.id, "other"))
    assert r.status_code == 200, r.text
    assert r.json()["later"] is True and r.json()["files"][0]["later"] is True
    assert [row["origin"] for row in r.json()["files"]] == ["upload", "agent"]
    assert client.get(_files_url(agent.id, "other", "reports/a.md")).status_code == 200
    for call in _file_calls(agent.runtime):
        assert call["params"] == {"user_sub": "header"}
        assert call["headers"][APPROVER_HEADER.lower()] == "1"

    with as_principal(client, "viewer-1", rbac.VIEWER):
        assert client.get(_files_url(agent.id)).status_code == 200
        assert _file_calls(agent.runtime)[-1]["params"] == {"user_sub": "viewer-1"}
        assert APPROVER_HEADER.lower() not in _file_calls(agent.runtime)[-1]["headers"]
        assert detail_code(client.get(_files_url(agent.id, "other"))) == (403, "not_session_owner")
        response = client.get(_files_url(agent.id, "other", "reports/a.md"))
        assert detail_code(response) == (403, "not_session_owner")


def test_file_path_is_re_encoded_segment_by_segment(client, fake_clusters, mongo):
    """파일 경로의 `/` 는 두고 조각마다 다시 인코딩한다. `..` 조각은 정규화되지 않은 채 런타임까지
    가서 422 invalid_path 이고 `?`·`#` 도 경로에 남는다. 세션 id 는 조각 하나로 다시 인코딩하고
    점도 바꿔 `..` 가 런타임의 다른 경로가 되지 않는다."""
    assert session_path("s1") == "/sessions/s1"
    assert session_path("a/b?c#d", "cancel") == "/sessions/a%2Fb%3Fc%23d/cancel"
    assert session_path("..") == "/sessions/%2E%2E"
    assert file_segments("reports/a.md") == "reports/a%2Emd"
    expected = "/sessions/s1/files/a%3Fb%23c/%2E%2E"
    assert session_path("s1", "files", file_segments("a?b#c/..")) == expected
    agent = deployed_agent(client, fake_clusters, mongo, features=FILES_FEATURES)
    add_session_files(agent.runtime)
    r = client.get(_files_url(agent.id, path="reports/%2E%2E/a.md"))
    assert detail_code(r) == (422, "invalid_path")
    assert _file_calls(agent.runtime)[-1]["file"] == "reports/../a.md"
    assert client.get(_files_url(agent.id, path="a%3Fb%23c.md")).status_code == 200
    assert _file_calls(agent.runtime)[-1]["file"] == "a?b#c.md"


# --- 능력 가르기와 롤링 창 (D65, D61) ------------------------------------------------------------

# 런타임 능력마다 그 능력에 기대는 라우트로 요청을 보내는 함수다. (client, Agent) → 응답
GatedRequest = Callable[[Any, DeployedAgent], httpx.Response]
GATED_REQUESTS: dict[str, tuple[GatedRequest, ...]] = {
    SESSION_ADMIN: (
        lambda c, a: c.patch(f"{a.url}/sessions/s1", json={"title": "새 이름"}),
        lambda c, a: c.delete(f"{a.url}/sessions/s1"),
        lambda c, a: c.get(f"{a.url}/sessions", params={"q": "환불"}),
    ),
    REGENERATE: (lambda c, a: c.post(f"{a.url}/sessions/s1/regenerate", json={}),),
    SESSION_FILES: (
        lambda c, a: c.get(_files_url(a.id)),
        lambda c, a: c.get(_files_url(a.id, path="a")),
    ),
    MEMORY_FILES: (
        lambda c, a: c.get(f"{a.url}/memories/me"),
        lambda c, a: c.get(f"{a.url}/memories/me/files/prefs.md"),
        lambda c, a: c.delete(f"{a.url}/memories/me/files/prefs.md"),
    ),
    CITATIONS: (lambda c, a: c.get(f"{a.url}/sessions/s1/citations/1/source"),),
    ATTACHMENTS: (lambda c, a: chat_upload(c, a.id, "메모.txt", b"hello"),),
}
# 그 라우트를 가짜 런타임에 다는 함수다. 가르기가 없으면 런타임이 답하고 호출을 남긴다. 인용
# 원문은 기본 라우트인 세션 상세를 읽고 업로드는 런타임을 부르지 않아 달 것이 없다
ROUTE_ADDERS: dict[str, Callable[[FastAPI], None]] = {
    SESSION_ADMIN: add_session_admin,
    REGENERATE: _add_regenerate,
    SESSION_FILES: add_session_files,
    MEMORY_FILES: add_memories,
}
# 옛 파드가 그 라우트를 몰라 FastAPI 기본 404·405 로 답하는 능력이다. 스트림과 내려받기의 롤링
# 응답은 test_agent_runtime_proxy.py 가 본다
ROLLING = (SESSION_ADMIN,)


def _gated(client, agent: DeployedAgent, feature: str) -> list[httpx.Response]:
    """feature 에 기대는 라우트로 요청을 모두 보낸 응답이다."""
    return [send(client, agent) for send in GATED_REQUESTS[feature]]


@pytest.mark.parametrize("feature", GATED_REQUESTS)
def test_routes_wait_for_their_capability_in_the_record(client, fake_clusters, mongo, feature):
    """기록에 그 이름이 없으면 라우트가 런타임을 부르지 않고 409 runtime_feature_missing 이다.
    기록에 다른 능력은 모두 있으므로 이름마다 따로 가르는 것까지 본다."""
    others = ",".join(name for name in CAPABILITIES if name != feature)
    agent = deployed_agent(client, fake_clusters, mongo, features=others)
    if feature in ROUTE_ADDERS:
        ROUTE_ADDERS[feature](agent.runtime)
    calls = len(agent.runtime.state.calls)
    for response in _gated(client, agent, feature):
        assert detail_code(response) == (409, RUNTIME_FEATURE_MISSING), response.text
        assert response.json()["detail"]["feature"] == feature
    assert len(agent.runtime.state.calls) == calls


@pytest.mark.parametrize("feature", ROLLING)
def test_a_pod_without_the_routes_answers_runtime_rolling(client, fake_clusters, mongo, feature):
    """기록에는 그 이름이 있는데 그 라우트를 모르는 옛 파드가 받으면 409 runtime_rolling 이다. 옛
    파드는 세션 목록의 제목 검색 q 도 모르고 버린다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=f"{CANCEL},{feature}")
    agent.runtime.state.echo_q = False
    for response in _gated(client, agent, feature):
        assert detail_code(response) == (409, RUNTIME_ROLLING), response.text
        assert response.json()["detail"]["feature"] == feature
