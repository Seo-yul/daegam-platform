"""이벤트 보내기(P37 3절): 두 큐와 상한, 비용 큐 먼저, 답마다의 처리, 닫을 때 남은 것 보내기."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2

from runtime import shipper as shipper_module
from runtime.shipper import AUTH_WAIT_SECONDS, EventShipper, shipper_from_env
from runtime.state import Settings
from tests.conftest import chat, run_app


def _event(name: str, n: int, size: int = 10) -> tuple[dict[str, Any], int]:
    return {"daegam_event": name, "id": f"e{n}", "n": n}, size


class Platform:
    """수집 경로 자리. 답할 상태를 차례로 내고 받은 봉투를 기억한다."""

    def __init__(self, *statuses: int, retry_after: str | None = None) -> None:
        self.statuses = list(statuses)
        self.retry_after = retry_after
        self.envelopes: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.envelopes.append(body)
        status = self.statuses.pop(0) if self.statuses else 200
        headers = {"retry-after": self.retry_after} if self.retry_after else {}
        # id 가 bad 로 시작하는 이벤트는 하나씩 거절한다
        refused = [
            {"id": e["id"], "code": "invalid_event"}
            for e in body["events"]
            if str(e.get("id")).startswith("bad")
        ]
        answer = {"accepted": len(body["events"]) - len(refused), "rejected": refused}
        return httpx2.Response(status, json=answer, headers=headers)


def _shipper(platform: Platform, waits: list[float]) -> EventShipper:
    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return EventShipper(
        "http://platform/api/telemetry/events",
        "tok",
        revision=3,
        pod_name="agent-faq-x",
        pod_uid="uid-1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(platform)),
        sleep=sleep,
    )


def test_queues_drop_their_oldest_and_cost_goes_first(monkeypatch) -> None:
    monkeypatch.setattr(shipper_module, "MAX_QUEUE_BYTES", 30)
    s = _shipper(Platform(), [])
    for n in range(3):
        s.offer(*_event("tool_exec", n))
    s.offer(*_event("tool_exec", 3))  # 넘친다. 가장 오래된 다른 이벤트를 버린다
    s.offer(*_event("model_call", 4, size=10))
    assert s.dropped == {"cost": 0, "other": 2} and s.queued == 3
    batch = s._take(10)
    assert [item.event["n"] for item in batch] == [4, 2, 3]


async def test_answers_decide_what_happens_to_a_batch() -> None:
    waits: list[float] = []
    # 200, 413 → 반씩(200, 413 하나는 버림), 422 는 버림
    platform = Platform(200, 413, 200, 413, 422)
    s = _shipper(platform, waits)
    s.offer(*_event("turn_end", 0))
    await s._drain()
    assert s.sent == 1
    for n in range(1, 3):
        s.offer(*_event("tool_exec", n))
    await s._drain()
    assert (s.sent, s.too_large) == (2, 1)
    s.offer(*_event("tool_exec", 9))
    await s._drain()
    assert s.rejected == 1 and s.queued == 0
    first = platform.envelopes[0]
    assert first["schema_version"] == 1 and first["revision"] == 3
    assert first["pod"] == {"name": "agent-faq-x", "uid": "uid-1"} and first["boot_id"]
    assert "correlation_reuse" in first["counters"] and waits == []


async def test_a_split_batch_resends_only_what_did_not_go(monkeypatch) -> None:
    """413 으로 나눈 앞쪽이 들어가고 뒤쪽이 막히면 뒤쪽만 되돌린다. 되돌릴 때도 큐 상한을 지킨다."""
    monkeypatch.setattr(shipper_module, "MAX_QUEUE_BYTES", 40)
    waits: list[float] = []
    s = _shipper(Platform(413, 200, 503), waits)
    for n in range(4):
        s.offer(*_event("tool_exec", n))
    await s._drain()
    assert s.sent == 2 and [i.event["n"] for i in s._take(10)] == [2, 3]
    s.offer(*_event("tool_exec", 7))
    s._put_back([_shipper_item(n) for n in range(4)])
    assert s.queued == 4 and s.dropped["other"] == 1


def _shipper_item(n: int) -> shipper_module._Item:
    return shipper_module._Item(*_event("tool_exec", n))


async def test_events_refused_one_by_one_count_as_rejected() -> None:
    s = _shipper(Platform(), [])
    s.offer(*_event("tool_exec", 0))
    s.offer({"daegam_event": "tool_exec", "id": "bad-1", "n": 1}, 10)
    await s._drain()
    assert (s.sent, s.rejected, s.queued) == (1, 1, 0)


async def test_a_heartbeat_that_cannot_reach_the_platform_waits_a_full_period() -> None:
    """닿지 못한 빈 봉투도 보내 본 시각을 남긴다. 그래야 다음 주기마다 다시 보내지 않는다."""

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    s = EventShipper(
        "http://platform/api/telemetry/events",
        "tok",
        revision=3,
        pod_name="agent-faq-x",
        pod_uid="uid-1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(down)),
    )
    s._last_sent = 0.0
    await s._heartbeat()
    assert s._last_sent > 0.0


async def test_refusals_wait_and_keep_the_batch() -> None:
    waits: list[float] = []
    s = _shipper(Platform(401, 503, 200, retry_after="7"), waits)
    s.offer(*_event("model_call", 0))
    await s._drain()
    assert waits == [AUTH_WAIT_SECONDS] and s.queued == 1
    await s._drain()
    assert waits[1] >= 7 and s.queued == 1  # Retry-After 를 따른다
    await s._drain()
    assert s.queued == 0 and s.sent == 1 and s.last_success


async def test_a_401_tells_the_drain_that_the_instance_token_was_refused() -> None:
    """401(회수된 인스턴스 토큰)을 받으면 on_unauthorized 를 부른다. 파드 종료 중이면 기다리던
    턴을 멈춘다(drain.py). 403·404 는 부르지 않는다."""
    waits: list[float] = []
    s = _shipper(Platform(401, 403, 200), waits)
    seen: list[str] = []
    s.on_unauthorized = lambda: seen.append("401")
    s.offer(*_event("model_call", 0))
    await s._drain()
    await s._drain()
    await s._drain()
    assert seen == ["401"] and s.queued == 0


async def test_closing_sends_what_is_left() -> None:
    platform = Platform()
    s = _shipper(platform, [])

    async def forever(_seconds: float) -> None:
        await asyncio.sleep(3600)

    s._sleep = forever
    task = asyncio.create_task(s.run())
    s.offer(*_event("turn_end", 0))
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert s.sent == 1 and len(platform.envelopes) == 1


def test_the_shipper_needs_an_address_and_a_token() -> None:
    assert shipper_from_env({}, token="t", revision=1) is None
    assert shipper_from_env({"DAEGAM_EVENTS_URL": "http://p/e"}, token=None, revision=1) is None
    env = {"DAEGAM_EVENTS_URL": "http://p/e", "DAEGAM_POD_UID": "u", "DAEGAM_POD_NAME": "n"}
    made = shipper_from_env({**env, "DAEGAM_EVENTS_FLUSH_SECONDS": "0.5"}, token="t", revision=2)
    assert made is not None and made._flush == 0.5 and made._pod == {"name": "n", "uid": "u"}


async def test_a_turn_queues_its_events_and_metrics_report_them(support_doc, monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_EVENTS_URL", "http://platform.invalid/api/telemetry/events")
    monkeypatch.setenv("DAEGAM_POD_UID", "uid-9")
    monkeypatch.setenv("DAEGAM_EVENTS_FLUSH_SECONDS", "3600")
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, settings=Settings(instance_token="tok")) as running:
        await chat(running, "hi", headers={"Authorization": "Bearer tok"})
        shipper = running.runtime.events.shipper
        names = [item.event["daegam_event"] for item in shipper._take(500)]
        metrics = (
            await running.client.get("/metrics", headers={"Authorization": "Bearer tok"})
        ).json()
    # 비용 이벤트가 먼저다
    assert names[:2] == ["model_call", "turn_end"] and "turn_start" in names
    assert metrics["telemetry"]["url"].endswith("/api/telemetry/events")


async def test_an_idle_shipper_sends_an_empty_envelope(monkeypatch) -> None:
    """보낼 것이 없어도 HEARTBEAT_SECONDS 마다 빈 봉투를 보낸다. 플랫폼이 이 파드를 밀어 보내는
    파드로 안다."""
    platform = Platform()
    s = _shipper(platform, [])
    s._flush = 0.01
    monkeypatch.setattr(shipper_module, "HEARTBEAT_SECONDS", 0.0)
    task = asyncio.create_task(s.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if platform.envelopes:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert platform.envelopes and platform.envelopes[0]["events"] == []
