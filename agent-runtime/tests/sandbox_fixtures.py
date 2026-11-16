"""코드 실행 시험의 가짜 파드와 가짜 플랫폼이다(P40 F4).

- FakePod: 샌드박스 서버(`sandbox/daegam_sandbox/server.py`)의 HTTP 모양을 흉내 내는 asyncio 서버다.
  연결마다 첫 요청이 `/proof` 였는지, 토큰이 실렸는지, 끊김을 언제 알았는지 적는다. `/execute` 는
  `echo <글>` 과 `sleep <초>`, `fail <코드>` 만 안다. 작업 공간은 dict 다.
- FakePlatform: 기계 경로(`platform/daegam/api/sandboxes.py`)의 FastAPI 흉내다. 세션마다 열린
  샌드박스 하나를 두고, 시험이 거절(`refuse`)이나 닫힘(`close_record`)을 끼운다. 받은 요청을 적는다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.conftest import serve

TOKEN = "sandbox-token"


@dataclass
class PodConnection:
    """연결 하나에서 본 것이다."""

    paths: list[str] = field(default_factory=list)
    authorized: list[bool] = field(default_factory=list)
    disconnected_at: float | None = None


@dataclass
class FakePod:
    token: str = TOKEN
    files: dict[str, bytes] = field(default_factory=dict)
    connections: list[PodConnection] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    # 틀린 증명을 답한다
    wrong_proof: bool = False
    # `/execute` 를 받고 답 없이 끊는다
    drop_execute: bool = False
    # 이 크기의 본문을 답한다(상한 시험)
    oversized: int = 0
    api: int = 1
    # 연결마다 먼저 부른다. 시험이 그사이 플랫폼의 기록을 바꾼다
    on_connect: Callable[[], None] | None = None
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    @property
    def address(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = PodConnection()
        self.connections.append(conn)
        if self.on_connect is not None:
            self.on_connect()
        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    break
                method, path, headers, body = request
                conn.paths.append(path)
                conn.authorized.append(headers.get("authorization") == f"Bearer {self.token}")
                answer = await self._answer(method, path, headers, body, reader, conn)
                if answer is None:
                    break
                status, payload = answer
                writer.write(_response(status, payload, self.oversized if path != "/proof" else 0))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            conn.disconnected_at = conn.disconnected_at or time.monotonic()
            writer.close()

    async def _answer(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any],
        reader: asyncio.StreamReader,
        conn: PodConnection,
    ) -> tuple[int, dict[str, Any]] | None:
        if path == "/proof":
            key = hashlib.sha256(self.token.encode()).digest()
            if self.wrong_proof:
                key = b"x" * 32
            mac = hmac.new(key, bytes.fromhex(body["nonce"]), hashlib.sha256).hexdigest()
            return 200, {"proof": mac}
        if path == "/healthz":
            return 200, {"ready": True, "api": self.api, "unchecked": False}
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"error": "unauthorized"}
        if path == "/execute":
            self.executed.append(body)
            if self.drop_execute:
                return None
            return await self._execute(body, reader, conn)
        if path == "/files/upload":
            results = []
            for item in body["files"]:
                self.files[item["path"]] = base64.b64decode(item["content_b64"])
                results.append({"path": item["path"], "error": None})
            return 200, {"results": results}
        if path == "/files/download":
            results = []
            for p in body["paths"]:
                data = self.files.get(p)
                results.append(
                    {
                        "path": p,
                        "content_b64": base64.b64encode(data).decode()
                        if data is not None
                        else None,
                        "error": None if data is not None else "file_not_found",
                    }
                )
            return 200, {"results": results}
        if path == "/files/list":
            excluded = tuple(f"/workspace/{name}/" for name in body.get("exclude", []))
            files = [
                {"path": p, "size": len(d), "modified": 1_700_000_000.0}
                for p, d in sorted(self.files.items())
                if p.startswith("/workspace/") and not p.startswith(excluded)
            ]
            return 200, {"files": files, "truncated": False}
        return 404, {"error": "not_found"}

    async def _execute(
        self, body: dict[str, Any], reader: asyncio.StreamReader, conn: PodConnection
    ) -> tuple[int, dict[str, Any]] | None:
        command = body["command"]
        word, _, rest = command.partition(" ")
        if word == "sleep":
            # 서버처럼 끊김을 지켜보며 기다린다
            try:
                async with asyncio.timeout(min(float(rest), float(body["timeout"]))):
                    if await reader.read(1) == b"":
                        conn.disconnected_at = time.monotonic()
                        return None
            except TimeoutError:
                return 200, _result("", 137, timed_out=True)
            return None
        if word == "fail":
            return 200, _result("", int(rest))
        if word == "echo":
            return 200, _result(rest + "\n", 0)
        # deepagents 가 파일 Tool 로 보내는 python 스크립트다. 찾을 것이 없다고 답한다
        return 200, _result(json.dumps({"error": "file_not_found"}), 0)


def _result(output: str, exit_code: int, *, timed_out: bool = False) -> dict[str, Any]:
    return {
        "output": output,
        "exit_code": exit_code,
        "truncated": False,
        "timed_out": timed_out,
        "cancelled": False,
    }


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], dict[str, Any]] | None:
    try:
        first = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError:
        return None
    method, path, _ = first.decode().split(" ", 2)
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readuntil(b"\r\n"))[:-2]
        if not line:
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    raw = await reader.readexactly(length) if length else b"{}"
    return method, path, headers, json.loads(raw or b"{}")


def _response(status: int, payload: dict[str, Any], oversized: int) -> bytes:
    body = json.dumps(payload).encode()
    if oversized:
        body = b" " * oversized + body
    head = (
        f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
    )
    return head.encode() + body


@dataclass
class SandboxRecord:
    session_id: str
    generation: int
    sandbox_id: str
    ready: bool = True
    restarts: int = 0
    last_termination: dict[str, Any] | None = None


@dataclass
class FakePlatform:
    """기계 경로의 흉내다. 세션마다 열린 기록 하나와 닫힌 까닭 하나를 둔다."""

    pod: FakePod
    open: dict[str, SandboxRecord] = field(default_factory=dict)
    closed_reason: dict[str, str] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # 다음 열기가 답할 거절 (상태, detail)
    refusal: tuple[int, dict[str, Any]] | None = None
    # 찾기마다 답하기 전에 부른다. 시험이 kubelet 처럼 늦게 기록을 바꾼다
    on_lookup: Callable[[], None] | None = None
    url: str = ""

    def calls(self, kind: str) -> list[dict[str, Any]]:
        return [body for name, body in self.requests if name == kind]

    def close_record(self, session_id: str, reason: str) -> None:
        self.open.pop(session_id, None)
        self.closed_reason[session_id] = reason

    def _answer(self, rec: SandboxRecord) -> dict[str, Any]:
        return {
            "sandbox_id": rec.sandbox_id,
            "generation": rec.generation,
            "ready": rec.ready,
            "address": self.pod.address,
            "token": self.pod.token,
            "unchecked": False,
            "restarts": rec.restarts,
            "last_termination": rec.last_termination,
        }

    def _not_found(self, session_id: str) -> JSONResponse:
        detail = {
            "code": "sandbox_not_found",
            "detail": "this session has no open sandbox",
            "closed_reason": self.closed_reason.get(session_id),
        }
        return JSONResponse({"detail": detail}, status_code=404)

    def app(self) -> FastAPI:
        app = FastAPI()
        base = "/api/agents/{agent_id}/sandboxes"

        @app.post(base)
        async def open_(agent_id: str, request: Request) -> Any:
            body = await request.json()
            self.requests.append(("open", body))
            if self.refusal is not None:
                status, detail = self.refusal
                self.refusal = None
                return JSONResponse({"detail": detail}, status_code=status)
            sid = body["session_id"]
            rec = self.open.get(sid)
            if rec is None:
                generation = self.generations.get(sid, 0) + 1
                self.generations[sid] = generation
                rec = SandboxRecord(sid, generation, f"sbx-{sid}-{generation}")
                self.open[sid] = rec
            return self._answer(rec)

        @app.get(base)
        async def lookup(agent_id: str, session_id: str) -> Any:
            self.requests.append(("lookup", {"session_id": session_id}))
            if self.on_lookup is not None:
                self.on_lookup()
            rec = self.open.get(session_id)
            if rec is None:
                return self._not_found(session_id)
            return self._answer(rec)

        @app.post(f"{base}/touch")
        async def touch(agent_id: str, request: Request) -> Any:
            body = await request.json()
            self.requests.append(("touch", body))
            rec = self.open.get(body["session_id"])
            if rec is None:
                return self._not_found(body["session_id"])
            return {"sandbox_id": rec.sandbox_id, "last_used_at": "2026-11-09T00:00:00Z"}

        @app.post(f"{base}/close")
        async def close(agent_id: str, request: Request) -> Any:
            body = await request.json()
            self.requests.append(("close", body))
            closed = self.open.pop(body["session_id"], None) is not None
            if closed:
                self.closed_reason[body["session_id"]] = body["reason"]
            return {"closed": closed}

        return app


@contextlib.asynccontextmanager
async def fake_sandbox() -> AsyncIterator[FakePlatform]:
    """가짜 파드와 그 파드를 내주는 가짜 플랫폼을 띄운다. `platform.url` 이 기계 경로의 주소다."""
    pod = FakePod()
    await pod.start()
    platform = FakePlatform(pod=pod)
    try:
        async with serve(platform.app()) as base_url:
            platform.url = f"{base_url}/api/agents/agent-1/sandboxes"
            yield platform
    finally:
        await pod.stop()


def code_doc(doc: dict[str, Any], approval: list[str] | None = None) -> dict[str, Any]:
    """문서에 코드 실행을 켠다. approval 이 있으면 execute 를 그 결정으로 묻는다."""
    features = sorted({*doc.get("features", []), "code_execution"})
    return {**doc, "features": features, "code_execution": {"approval": approval or []}}
