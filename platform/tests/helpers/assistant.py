"""비서 API(세션·턴·결정)를 부르는 테스트가 함께 쓰는 도우미.

스텁 provider 로 비서를 켜고, 스텁이 따를 steps 메시지를 만들고, 턴과 결정을 보내 SSE 를 읽고,
세션 문서와 리스를 본다. Gate 는 도구 호출을 잡아 두어 백그라운드 턴을 멈춘다.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from daegam.auth.principal import header_principal
from tests.helpers.backend_apis import fixture_bytes
from tests.helpers.consts import A

STUB = {"name": "local-stub", "kind": "stub"}
WSDL = fixture_bytes("calculator.wsdl").decode("utf-8")


def enable(client, *, providers=(STUB,), defaults=None, headers=None, **assistant) -> None:
    body: dict[str, Any] = {
        "llm_providers": list(providers),
        "assistant": {"enabled": True, **assistant},
    }
    if defaults is not None:
        body["defaults"] = defaults
    r = client.patch("/settings", json=body, headers=headers or {})
    assert r.status_code == 200, r.text


def new_session(client, title: str | None = None, headers=None) -> str:
    r = client.post(f"{A}/sessions", json={"title": title} if title else {}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def parse_sse(text: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((event, data))
    return out


def names(events: list[tuple[str, Any]]) -> list[str]:
    return [e for e, _ in events]


def steps(*items: dict[str, Any]) -> str:
    return json.dumps({"steps": list(items)})


def tool(tool_name: str, /, **args: Any) -> dict[str, Any]:
    return {"tool": tool_name, "args": args}


def say(text: str) -> dict[str, Any]:
    return {"say": text}


def turn(client, sid: str, message: str, *, context=None, headers=None):
    body: dict[str, Any] = {"message": message}
    if context is not None:
        body["context"] = context
    r = client.post(f"{A}/sessions/{sid}/turns", json=body, headers=headers or {})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return parse_sse(r.text)


def decide(client, sid: str, decisions: list[dict[str, Any]], headers=None):
    return client.post(
        f"{A}/sessions/{sid}/decisions", json={"decisions": decisions}, headers=headers or {}
    )


def decided(client, sid: str, decisions: list[dict[str, Any]], headers=None):
    r = decide(client, sid, decisions, headers)
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def approve_all(client, sid: str, headers=None):
    pending = client.get(f"{A}/sessions/{sid}", headers=headers or {}).json()["pending"]
    assert pending is not None and pending["kind"] == "confirm"
    return decided(
        client, sid, [{"id": c["id"], "type": "approve"} for c in pending["calls"]], headers
    )


def session_doc(mongo, sid: str) -> dict[str, Any]:
    return mongo["assistant_sessions"].find_one({"_id": sid})


def assistant_leases(mongo) -> list[str]:
    return sorted(
        d["_id"] for d in mongo["reconcile_leases"].find({"_id": {"$regex": "^assistant-"}})
    )


def await_task(client, task):
    async def wait():
        return await asyncio.wait_for(asyncio.shield(task), 30)

    return client.portal.call(wait)


def start_via_service(client, sid: str, message: str):
    svc = client.app.state.assistant
    return client.portal.call(
        lambda: svc.start_turn(
            sid,
            tenant="default",
            principal=header_principal(),
            message=message,
            ui_context=None,
            request_id="req-1",
        )
    )


class Gate:
    """실행기를 잡아 두는 문. open 전까지 도구 호출이 멈춘다."""

    def __init__(self, svc) -> None:
        self.svc = svc
        self.original = svc.executor.call
        self.event: asyncio.Event | None = None
        self.entered = 0

    def install(self) -> None:
        gate = self

        async def call(*a, **kw):
            if gate.event is None:
                gate.event = asyncio.Event()
            gate.entered += 1
            await gate.event.wait()
            return await gate.original(*a, **kw)

        self.svc.executor.call = call

    def wait_entered(self, timeout: float = 5.0) -> None:
        """도구 호출이 문에 이를 때까지 기다린다. 턴은 백그라운드에서 돌므로 곧바로 멈추면 도구 호출
        전에 끊길 수 있다."""
        deadline = time.monotonic() + timeout
        while self.entered == 0:
            assert time.monotonic() < deadline, "턴이 도구 호출까지 가지 않았다"
            time.sleep(0.01)

    def open(self, client) -> None:
        def _open():
            if self.event is None:
                self.event = asyncio.Event()
            self.event.set()

        client.portal.call(_open)

    def restore(self) -> None:
        self.svc.executor.call = self.original
