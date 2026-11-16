"""가짜 agent-runtime ASGI 앱과 그 라우트를 꾸미는 도우미 (D21, D65).

`fake_runtime` 은 런타임의 API 표면을 흉내 내고 받은 호출을 `app.state.calls` 에 남긴다
(`record_call`). 배포한 Agent 의 fake 클러스터에 `service_app` 으로 붙여 플랫폼 프록시가 이 앱을
부르게 한다. 세션 관리·세션 파일·장기 기억·전사처럼 새 런타임만 아는 라우트는
`add_session_admin`·`add_session_files`·`add_memories`·`with_transcript` 가 앱에 단다. 런타임의 거절
응답은 `runtime_refusal` 이 만든다. 기능 이름(`ADMIN_FEATURES` 등)은 init 이 알린 런타임 능력의
기록이다.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from daegam.services.agent_features import CANCEL
from daegam.services.agent_runtime import APPROVER_HEADER


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# 가짜 런타임의 `/metrics` 가 싣는 파드 이름이다. K8s 가 HOSTNAME 으로 넣는 꼴이다
RUNTIME_POD = "agent-support-7d9c5f8b6-x2k4q"


def session_row(sid: str, owner: str, **extra: Any) -> dict[str, Any]:
    """런타임 `SessionRecord.to_dict` 가 내는 세션 한 줄이다(색인 내부값은 싣지 않는다)."""
    return {
        "session_id": sid,
        "tenant": None,
        "user_sub": owner,
        "created_at": "2026-10-03T00:00:00+00:00",
        "last_turn_at": "2026-10-03T00:05:00+00:00",
        "turns": 1,
        "usage_input": 10,
        "usage_output": 2,
        "status": "active",
        "pending_since": None,
        "instance_id": "inst-1",
        "title": "환불 문의",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        **extra,
    }


def runtime_refusal(
    status: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """런타임이 요청을 거절할 때 내는 오류 응답이다. extra 는 본문에 더 싣는 칸(가드레일 rule
    등)이다."""
    body = {"code": code, "message": message, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


def draining_refusal() -> JSONResponse:
    """드레인 표시가 선 동안 리스를 잡는 요청에 런타임이 내는 503 이다 (P44 7절)."""
    return runtime_refusal(
        503, "agent_draining", "이 인스턴스가 내려가는 중이다", headers={"Retry-After": "5"}
    )


def session_missing() -> JSONResponse:
    """없는 세션(`missing`)에 런타임이 내는 404 다."""
    return runtime_refusal(404, "session_not_found", "세션이 없다")


def not_owner() -> JSONResponse:
    """남의 세션(`other`)에 런타임이 내는 403 이다."""
    return runtime_refusal(403, "not_session_owner", "세션 소유자가 아니다")


def foreign(request: Request, sid: str) -> bool:
    """sid 가 남의 세션(`other`)이고 요청에 승인자 헤더가 없다. 런타임은 이때 403 이다."""
    return sid == "other" and request.headers.get(APPROVER_HEADER) != "1"


def record_call(app: FastAPI, request: Request, **extra: Any) -> dict[str, Any]:
    """받은 요청을 `app.state.calls` 에 남기고 그 기록을 돌려준다. extra 는 라우트마다 더 남기는
    칸(본문·세션 id·파일 경로 등)이다."""
    call = {
        "method": request.method,
        "path": request.url.path,
        "params": dict(request.query_params),
        "headers": dict(request.headers),
        **extra,
    }
    app.state.calls.append(call)
    return call


def fake_runtime(token: str, *, warnings: list[str] | None = None) -> FastAPI:
    """agent-runtime 의 API 표면을 흉내 낸다. 호출 기록은 app.state.calls 에 남는다.
    `app.state.chat` 을 채우면 `/metrics` 가 받는 첨부(`chat`, D61)를 싣고, 비워 두면 그 칸이 없는
    옛 런타임이다. `app.state.echo_q` 를 끄면 세션 목록이 제목 검색 q 를 모르고 버리는 옛 파드다."""
    app = FastAPI()
    app.state.calls = []
    app.state.warnings = warnings or []
    app.state.health = {"status": "ok"}
    app.state.chat = None
    # `/metrics` 가 답할 상태. 200 이 아니면 본문 없이 그 상태로 답한다(잠들 때 읽지 못한 수, P44)
    app.state.metrics_status = 200
    app.state.echo_q = True
    # `/turns/cancel` 이 차례로 답할 남은 턴 수(P78 5절). 다 쓰면 0 이다
    app.state.running = []
    # 드레인 표시의 id 와 `POST /drain` 이 답할 살아 있는 턴 리스 수(P44 7절). 표시가 있으면
    # 리스를 잡는 요청은 503 `agent_draining` 이다
    app.state.drain_mark = None
    app.state.drain_leases = 0

    def unauthorized(request: Request) -> JSONResponse | None:
        if request.headers.get("authorization") != f"Bearer {token}":
            return runtime_refusal(401, "unauthorized", "인스턴스 토큰이 필요하다")
        return None

    def draining() -> JSONResponse | None:
        """표시가 있으면 턴의 리스를 거절한다 (P44 7절)."""
        return draining_refusal() if app.state.drain_mark else None

    async def three_events(sid: str):
        yield sse("token", {"text": "3 + 4 는 "})
        yield sse("tool_call", {"id": "c1", "name": "Add", "args": {"a": 3, "b": 4}})
        yield sse(
            "done",
            {"session_id": sid, "turn": 1, "usage": {"total_tokens": 12}, "tool_calls": 1},
        )

    @app.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        record_call(app, request, body=body)
        if (denied := unauthorized(request)) is not None:
            return denied
        if (refused := draining()) is not None:
            return refused
        if body["message"] == "busy":
            return runtime_refusal(
                429,
                "max_concurrent_sessions",
                "동시에 열 수 있는 세션 2개가 모두 찼다",
                headers={"Retry-After": "60"},
            )
        return StreamingResponse(
            three_events(body.get("session_id") or "s-new"),
            media_type="text/event-stream",
            headers={"X-Runtime": "fake", "Cache-Control": "no-cache"},
        )

    @app.get("/sessions")
    async def sessions(request: Request):
        call = record_call(app, request)
        if (denied := unauthorized(request)) is not None:
            return denied
        owner = call["params"].get("user_sub") or "anyone"
        listed: dict[str, Any] = {"sessions": [session_row("s1", owner)]}
        if "q" in call["params"] and app.state.echo_q:
            listed["q"] = call["params"]["q"]
        return listed

    @app.get("/sessions/{sid}")
    async def session(sid: str, request: Request):
        record_call(app, request)
        if (denied := unauthorized(request)) is not None:
            return denied
        if sid == "missing":
            return session_missing()
        owner = "someone-else" if sid == "other" else "header"
        return {"session": session_row(sid, owner), "messages": [], "pending": True}

    @app.get("/sessions/{sid}/pending")
    async def pending(sid: str, request: Request):
        record_call(app, request)
        if (denied := unauthorized(request)) is not None:
            return denied
        return {
            "session_id": sid,
            "user_sub": "header",
            "action_requests": [{"name": "Divide", "args": {"a": 1, "b": 0}, "reason": "rule"}],
            "review_configs": [{"action_name": "Divide", "allowed_decisions": ["approve"]}],
        }

    @app.post("/sessions/{sid}/decisions")
    async def decisions(sid: str, request: Request):
        body = await request.json()
        record_call(app, request, body=body)
        if (denied := unauthorized(request)) is not None:
            return denied
        if (refused := draining()) is not None:
            return refused
        if len(body["decisions"]) != 1:
            return runtime_refusal(422, "decision_count", "결정 개수가 맞지 않는다")
        # 고치기 검사(D65)의 두 422 다. 이름이 다르면 edit_name_mismatch, 고친 인자가 도구
        # 가드레일에 걸리면 입력 가드와 같은 guardrail_blocked(rule 포함)이다
        edited = body["decisions"][0].get("edited_action")
        if edited and edited["name"] != "Divide":
            return runtime_refusal(422, "edit_name_mismatch", "edit 은 Divide 의 인자만 고친다")
        if edited and edited["args"].get("b") == 666:
            return runtime_refusal(422, "guardrail_blocked", "그 수는 안 된다", rule="deny_pattern")
        return StreamingResponse(three_events(sid), media_type="text/event-stream")

    @app.post("/sessions/{sid}/cancel", status_code=202)
    async def cancel(sid: str, request: Request):
        """진행 중 턴 멈추기(D65). 남의 세션은 승인자 헤더가 없으면 403 이고, `idle` 은 진행 중
        턴이 없는 세션, `missing` 은 없는 세션이다."""
        body = await request.json()
        record_call(app, request, body=body)
        if (denied := unauthorized(request)) is not None:
            return denied
        if sid == "missing":
            return session_missing()
        if foreign(request, sid):
            return not_owner()
        if sid == "idle":
            return runtime_refusal(409, "no_turn_in_progress", "진행 중인 턴이 없다")
        return {"session_id": sid}

    @app.post("/turns/cancel", status_code=202)
    async def cancel_all(request: Request):
        """인스턴스의 도는 턴을 모두 멈춘다(P78 5절). 아직 리스가 살아 있는 턴 수를 답한다."""
        body = await request.json()
        record_call(app, request, body=body)
        if (denied := unauthorized(request)) is not None:
            return denied
        return {"running": app.state.running.pop(0) if app.state.running else 0}

    @app.post("/drain")
    async def drain(request: Request):
        """드레인 표시 `mark` 를 세우고 살아 있는 턴 리스 수를 답한다 (P44 7절). 본문은
        /turns/cancel 처럼 플랫폼의 주체와 표시의 id 다."""
        body = await request.json()
        record_call(app, request, body=body)
        if (denied := unauthorized(request)) is not None:
            return denied
        app.state.drain_mark = body["mark"]
        return {"running": app.state.drain_leases}

    @app.delete("/drain", status_code=204)
    async def undrain(request: Request):
        """질의의 `mark` 와 같은 표시만 지운다. 다르거나 없어도 204 이고, mark 가 없으면 422 다."""
        call = record_call(app, request)
        if (denied := unauthorized(request)) is not None:
            return denied
        mark = call["params"].get("mark")
        if not mark:
            return runtime_refusal(422, "mark_required", "지울 표시의 mark 가 필요하다")
        if app.state.drain_mark == mark:
            app.state.drain_mark = None
        return Response(status_code=204)

    @app.get("/metrics")
    async def metrics(request: Request):
        record_call(app, request)
        if (denied := unauthorized(request)) is not None:
            return denied
        if app.state.metrics_status != 200:
            return Response(status_code=app.state.metrics_status)
        chat = {"chat": app.state.chat} if app.state.chat is not None else {}
        return {
            **chat,
            "pod": RUNTIME_POD,
            "sessions": {"active": 2, "pending": 1, "closed": 5},
            "totals": {"turns": 9, "usage_input": 100, "usage_output": 50},
            "turns": {"started": 9, "done": 8},
            "tool_calls": 3,
            "latency_ms": {"avg": 1.0, "p50": 1.0, "p95": 2.0, "max": 3.0},
            "tools": {
                "count": 2,
                "loaded_at": None,
                "last_error": None,
                "protocols": {"tools-calculator": "2026-07-28"},
            },
            "warnings": list(app.state.warnings),
            "runtime": {"features": [CANCEL]},
        }

    @app.get("/healthz")
    async def healthz(request: Request):
        """살아 있음 검사. 상세(`app.state.health`)는 인스턴스 토큰이 맞는 요청에만 준다(T21)."""
        record_call(app, request)
        if request.headers.get("authorization") != f"Bearer {token}":
            return {"status": app.state.health["status"]}
        return app.state.health

    return app


# -- 새 런타임의 라우트 --------------------------------------------------------------------------

# init 이 알린 능력. 세션 관리(이름 바꾸기·지우기)와 세션 파일을 아는 런타임이다
ADMIN_FEATURES = "cancel,session_admin"
FILES_FEATURES = "cancel,session_files"
# 호출자 자신의 장기 기억 파일을 아는 런타임이다
MEMORY_FEATURES = "cancel,memory_files"
# 채팅 첨부를 아는 런타임이다
ATTACHMENT_FEATURES = "cancel,attachments"


def drop_route(runtime: FastAPI, path: str) -> None:
    """가짜 런타임에서 path 의 라우트를 모두 뺀다. 그 경로를 모르는 옛 런타임 흉내이거나
    바꿔 달기 전이다."""
    runtime.router.routes = [r for r in runtime.router.routes if getattr(r, "path", "") != path]


def replace_get(runtime: FastAPI, path: str, handler: Any) -> None:
    """가짜 런타임의 GET path 를 handler 로 바꾼다."""
    drop_route(runtime, path)
    runtime.get(path)(handler)


def with_transcript(runtime: FastAPI, items: list[dict[str, Any]]) -> None:
    """세션 상세가 전사 items 를 싣게 바꾼다. `other` 는 남의 세션, `missing` 은 없는 세션이다."""

    async def session(sid: str, request: Request):
        record_call(runtime, request)
        if sid == "missing":
            return session_missing()
        owner = "someone-else" if sid == "other" else "header"
        transcript = {"items": items, "limit": 500}
        return {"session": session_row(sid, owner), "pending": False, "transcript": transcript}

    replace_get(runtime, "/sessions/{sid}", session)


def add_session_admin(runtime: FastAPI) -> None:
    """가짜 런타임에 이름 바꾸기와 지우기를 단다. `other` 는 남의 세션, `busy` 는 도는 턴이 있는
    세션, `missing` 은 없는 세션이다. 제목이 비면 런타임처럼 422 invalid_title 이다."""

    def refused(request: Request, sid: str, body: Any = None) -> JSONResponse | None:
        record_call(runtime, request, sid=sid, body=body)
        if sid == "missing":
            return session_missing()
        if sid == "other":
            return not_owner()
        return None

    @runtime.patch("/sessions/{sid}")
    async def rename(sid: str, request: Request):
        body = await request.json()
        if (denied := refused(request, sid, body)) is not None:
            return denied
        if not body["title"].strip():
            return runtime_refusal(422, "invalid_title", "제목은 1~100자다")
        owner = request.query_params["user_sub"]
        return {"session": session_row(sid, owner, title=body["title"].strip())}

    @runtime.delete("/sessions/{sid}")
    async def delete(sid: str, request: Request):
        if (denied := refused(request, sid)) is not None:
            return denied
        if runtime.state.drain_mark:
            # 세션 지우기도 리스를 잡으므로 드레인 표시가 물린다 (P44 7절)
            return draining_refusal()
        if sid == "busy":
            return runtime_refusal(409, "turn_in_progress", "이 세션의 턴이 아직 진행 중이다")
        return {"session_id": sid, "deleted": True}


# 세션 파일 목록의 두 줄. 업로드에서 뽑은 글과 에이전트가 쓴 파일이다
FILE_ROWS = [
    {
        "path": "/uploads/보고서.pdf.txt",
        "size": 12,
        "modified_at": "2026-10-03T00:00:00+00:00",
        "origin": "upload",
        "later": True,
    },
    {"path": "/reports/요약.md", "size": 5, "modified_at": None, "origin": "agent"},
]


# 런타임이 내려받기에 다는 헤더다. 플랫폼은 이것을 넘기지 않고 다시 만든다
RUNTIME_FILE_HEADERS = {
    "Content-Type": "text/html",
    "Content-Disposition": "inline",
    "X-Runtime": "1",
}


def add_session_files(runtime: FastAPI) -> None:
    """가짜 런타임에 세션 파일 두 라우트를 단다. 런타임처럼 승인자 헤더가 없으면 `other` 는 남의
    세션(403)이고, `closed` 는 닫힌 세션(410), `..` 조각은 422 invalid_path, `missing.md` 는 404
    file_not_found 다. 내려받기는 일부러 문서로 열리는 헤더를 단다."""

    def refused(request: Request, sid: str, path: str | None = None) -> JSONResponse | None:
        record_call(runtime, request, sid=sid, file=path)
        if foreign(request, sid):
            return not_owner()
        if sid == "closed":
            return runtime_refusal(410, "session_closed", "세션이 닫혔다")
        if path is not None and ".." in path.split("/"):
            return runtime_refusal(422, "invalid_path", "파일 경로에 '..' 조각이 있다")
        if path == "missing.md":
            return runtime_refusal(404, "file_not_found", "그 파일이 없다")
        return None

    @runtime.get("/sessions/{sid}/files")
    async def files(sid: str, request: Request):
        if (denied := refused(request, sid)) is not None:
            return denied
        return {"files": FILE_ROWS, "later": True}

    @runtime.get("/sessions/{sid}/files/{path:path}")
    async def file(sid: str, path: str, request: Request):
        if (denied := refused(request, sid, path)) is not None:
            return denied
        return Response(f"{path} 내용".encode(), headers=RUNTIME_FILE_HEADERS)


# 기억 파일 키마다 런타임이 내는 거절이다
MEMORY_REFUSALS: dict[str, tuple[int, str, str]] = {
    "missing.md": (404, "memory_not_found", "그 기억 파일이 없다"),
    "big.md": (413, "file_too_large", "기억 파일이 1048576 바이트를 넘는다"),
}


def add_memories(runtime: FastAPI) -> None:
    """가짜 런타임에 기억 세 라우트를 단다. 런타임처럼 쿼리의 sub 로 네임스페이스를 고르고, `..`
    조각은 422 invalid_path, `MEMORY_REFUSALS` 의 키는 그 오류다. `runtime.state.long_term` 을 끄면
    장기 기억이 꺼진 에이전트다."""
    runtime.state.long_term = True

    def refused(request: Request, key: str | None = None) -> JSONResponse | None:
        record_call(runtime, request, key=key)
        if not runtime.state.long_term:
            return runtime_refusal(404, "long_term_off", "이 에이전트는 장기 기억을 쓰지 않는다")
        if key is not None and ".." in key.split("/"):
            return runtime_refusal(422, "invalid_path", "기억 파일 경로에 '..' 조각이 있다")
        if key in MEMORY_REFUSALS:
            return runtime_refusal(*MEMORY_REFUSALS[key])
        return None

    @runtime.get("/memories")
    async def memories(request: Request):
        if (denied := refused(request)) is not None:
            return denied
        row = {
            "path": f"/memories/{request.query_params['user_sub']}.md",
            "size": 3,
            "created_at": "2026-10-03T00:00:00+00:00",
            "modified_at": "2026-10-03T00:05:00+00:00",
            "later": True,
        }
        return {"enabled": True, "retention_days": 30, "files": [row], "later": True}

    @runtime.get("/memories/files/{key:path}")
    async def read(key: str, request: Request):
        if (denied := refused(request, key)) is not None:
            return denied
        return {"path": f"/memories/{key}", "encoding": "utf-8", "content": "# 취향"}

    @runtime.delete("/memories/files/{key:path}")
    async def delete(key: str, request: Request):
        if (denied := refused(request, key)) is not None:
            return denied
        return {"path": f"/memories/{key}", "deleted": True}
