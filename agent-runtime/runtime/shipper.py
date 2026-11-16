"""관측 이벤트를 플랫폼에 밀어 보낸다(P37 3절, D62).

`DAEGAM_EVENTS_URL` 과 인스턴스 토큰이 있을 때만 돈다. env 는 밀어 보내기만 가른다. `model_call`·
`tool_exec` 와 상관 헤더는 늘 만들어지고 stdout 에도 늘 쓰이므로, 밀어 보내지 않는 파드는 플랫폼
수집기가 로그에서 읽는다.

- 보내기는 채팅의 길 밖이다. `EventLog.emit` 은 stdout 에 쓰고 같은 객체를 큐에 넣고 돌아온다.
  네트워크를 기다리지 않고 스레드에서 불러도 된다. 기동 뒤의 백그라운드 작업 하나가 보낸다(boot.py).
  플랫폼이 멈춰도 기동과 채팅은 막히지 않는다
- 큐는 둘이다. 비용 큐(`model_call`, `turn_end`)를 먼저 보내고 나머지는 그 뒤다. 둘을 합쳐 8 MiB
  까지 두고, 넘치면 가장 오래된 이벤트를 버리고 큐마다 센다. 비용 이벤트의 자리는 나머지 큐에서 먼저
  낸다
- 보낼 것이 없어도 `HEARTBEAT_SECONDS`(60초)마다 빈 봉투를 보낸다. 플랫폼은 그것으로 이 파드를 밀어
  보내는 파드로 알고(수집기가 로그를 읽지 않는다) 마지막 수신 시각을 둔다. 한가한 Agent 가 밀어
  보내지 않는 파드로 보이지 않게 한다
- `DAEGAM_EVENTS_FLUSH_SECONDS`(2초)마다 또는 200개가 차면 보낸다. 요청은 한 번에 하나이고 10초 안에
  답해야 하며, 닫을 때는 남은 것을 5초 안에 보낸다. SIGKILL·OOM 은 마지막 몫을 잃고 그 몫은 stdout
  에만 있다
- 답: 200 은 보낸 것을 버리고, 답의 `rejected[]`(이벤트 하나씩의 거절)는 `rejected` 로 센다. 413 은
  묶음을 반으로 나누고, 하나여도 413 이면 `too_large` 로 버린다. 앞쪽이 들어간 뒤 뒤쪽이 막히면
  뒤쪽만 되돌린다. 422 는 묶음을 `rejected` 로 버린다. 깨진 묶음이 뒤를 막지 않게 한다.
  401·403·404(회수된 토큰, 수집 경로가 없는 옛 플랫폼)는 60초 기다린다. 429·503·그 밖의 5xx·연결
  오류는 1초에서 60초까지 흔들림을 더해 두 배씩 물러나고 `Retry-After` 를 따른다. 되돌린 묶음도 큐
  상한을 지킨다
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2

from runtime import llm_http

log = logging.getLogger("daegam.agent-runtime.shipper")

SCHEMA_VERSION = 1
COST_EVENTS = frozenset({"model_call", "turn_end"})
MAX_QUEUE_BYTES = 8 * 1024 * 1024
BATCH_EVENTS = 200
DEFAULT_FLUSH_SECONDS = 2.0
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_SECONDS = 5.0
AUTH_WAIT_SECONDS = 60.0
HEARTBEAT_SECONDS = 60.0
BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
COST = "cost"
OTHER = "other"


@dataclass
class _Item:
    event: dict[str, Any]
    size: int


class EventShipper:
    """두 큐와 보내는 작업 하나다. 모듈 머리의 까닭이다."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        revision: int,
        pod_name: str,
        pod_uid: str,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._revision = revision
        self._pod = {"name": pod_name, "uid": pod_uid}
        self._boot_id = uuid.uuid4().hex
        self._flush = flush_seconds
        self._client = client
        self._sleep = sleep
        self._queues: dict[str, deque[_Item]] = {COST: deque(), OTHER: deque()}
        self._bytes = 0
        self._lock = threading.Lock()
        self._backoff = BACKOFF_MIN_SECONDS
        # 200개가 차면 주기를 기다리지 않고 깨운다. 넣기는 다른 스레드일 수 있어 루프로 넘긴다
        self._full = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        # 401(인스턴스 토큰이 거절됐다)을 받을 때 부른다. 파드 종료 중이면 기다리는 턴을
        # 멈춘다(drain.py)
        self.on_unauthorized: Callable[[], None] | None = None
        self.sent = 0
        self.dropped = {COST: 0, OTHER: 0}
        self.too_large = 0
        self.rejected = 0
        self.last_success: str | None = None
        # 마지막으로 무엇이든(빈 봉투 포함) 보내 본 단조 시각이다. 닿지 못한 시도도 든다
        self._last_sent = time.monotonic()

    # -- 넣기 (어느 스레드에서든) -----------------------------------------------------------------

    def offer(self, event: dict[str, Any], size: int) -> None:
        """이벤트 하나를 큐에 넣는다. 넘치면 가장 오래된 것부터 버린다. 비용 이벤트는 나머지 큐를
        먼저 비우고, 나머지 이벤트는 비용 큐를 건드리지 않는다."""
        name = _queue_of(event)
        victims = (OTHER, COST) if name == COST else (OTHER,)
        with self._lock:
            for victim in victims:
                queue = self._queues[victim]
                while self._bytes + size > MAX_QUEUE_BYTES and queue:
                    self._bytes -= queue.popleft().size
                    self.dropped[victim] += 1
            if self._bytes + size > MAX_QUEUE_BYTES:
                self.dropped[name] += 1
                return
            self._queues[name].append(_Item(event, size))
            self._bytes += size
            full = self.queued >= BATCH_EVENTS
        if full and self._loop is not None:
            self._loop.call_soon_threadsafe(self._full.set)

    @property
    def queued(self) -> int:
        return len(self._queues[COST]) + len(self._queues[OTHER])

    def report(self) -> dict[str, Any]:
        """`/metrics` 의 `telemetry` 칸이다. `/metrics` 는 Service 뒤의 파드 하나 값이라
        `correlation_reuse` 는 봉투로도 보낸다."""
        return {
            "url": self.url,
            "sent": self.sent,
            "queued": self.queued,
            "dropped": dict(self.dropped),
            "too_large": self.too_large,
            "rejected": self.rejected,
            "last_success": self.last_success,
            "correlation_reuse": llm_http.CORRELATION.correlation_reuse,
        }

    # -- 보내기 ------------------------------------------------------------------------------

    async def run(self) -> None:
        """주기마다 보낸다. 한 바퀴가 무슨 예외로 끝나도 다음 바퀴로 간다. 취소되면 남은 것을 5초
        안에 보내고 끝난다."""
        self._loop = asyncio.get_running_loop()
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._full.wait(), self._flush)
                self._full.clear()
                try:
                    await self._drain()
                    if not self.queued and time.monotonic() - self._last_sent >= HEARTBEAT_SECONDS:
                        await self._heartbeat()
                except Exception:  # noqa: BLE001 - 보내기의 실패가 이 작업을 끝내면 다시 보내지 못한다
                    log.exception("events: the send loop failed; continuing")
        except asyncio.CancelledError:
            with anyio.move_on_after(CLOSE_SECONDS, shield=True):
                await self._drain(once=True)
            raise
        finally:
            if self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.aclose()

    async def _heartbeat(self) -> None:
        """빈 봉투 하나. 답은 보지 않는다. 다음 바퀴의 보내기가 같은 길로 물러난다."""
        await self._send([])

    async def _drain(self, *, once: bool = False) -> None:
        """큐가 빌 때까지 묶음을 보낸다. 다시 보내야 하면 물러난 뒤 다음 주기를 기다린다."""
        while self.queued:
            wait, unsent = await self._send(self._take(BATCH_EVENTS))
            if wait is not None:
                self._put_back(unsent)
                if once:
                    return
                await self._sleep(wait)
                return

    def _take(self, n: int) -> list[_Item]:
        """비용 큐부터 n 개를 꺼낸다."""
        out: list[_Item] = []
        with self._lock:
            for name in (COST, OTHER):
                queue = self._queues[name]
                while queue and len(out) < n:
                    item = queue.popleft()
                    self._bytes -= item.size
                    out.append(item)
        return out

    def _put_back(self, batch: list[_Item]) -> None:
        """보내지 못한 묶음을 큐 앞에 되돌린다. 차례는 그대로다. 그사이 들어온 것과 합쳐 상한을
        넘으면 넣기처럼 나머지 큐부터 가장 오래된 것을 버린다."""
        with self._lock:
            for item in reversed(batch):
                self._queues[_queue_of(item.event)].appendleft(item)
                self._bytes += item.size
            for victim in (OTHER, COST):
                queue = self._queues[victim]
                while self._bytes > MAX_QUEUE_BYTES and queue:
                    self._bytes -= queue.popleft().size
                    self.dropped[victim] += 1

    async def _send(self, batch: list[_Item]) -> tuple[float | None, list[_Item]]:
        """묶음 하나를 보낸다. 끝났으면(보냈거나 버렸으면) (None, []) 이고, 다시 보내야 하면 (기다릴
        초, 아직 보내지 못한 것)이다. 413 으로 나눠 보낸 앞쪽이 들어갔으면 뒤쪽만 남는다."""
        try:
            response = await self._http().post(
                self.url,
                json=self._envelope(batch),
                headers=self._headers,
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except httpx2.HTTPError as e:
            # 빈 봉투도 다음 주기가 아니라 60초 뒤에 다시 보낸다
            self._last_sent = time.monotonic()
            log.warning("events: could not reach the platform: %s", type(e).__name__)
            return self._next_backoff(None), batch
        self._last_sent = time.monotonic()
        status = response.status_code
        if status == 200:
            # 플랫폼이 이벤트 하나씩 거절한 것은 보낸 수가 아니라 거절 수다
            refused = _refused(response)
            self.sent += len(batch) - refused
            self.rejected += refused
            self.last_success = datetime.now(UTC).isoformat(timespec="seconds")
            self._backoff = BACKOFF_MIN_SECONDS
            return None, []
        if status == 413:
            return await self._split(batch)
        if status == 422:
            self.rejected += len(batch)
            log.warning("events: the platform refused a batch of %d", len(batch))
            return None, []
        if status in (401, 403, 404):
            log.warning("events: the platform answered %d; waiting", status)
            if status == 401 and self.on_unauthorized is not None:
                self.on_unauthorized()
            return AUTH_WAIT_SECONDS, batch
        return self._next_backoff(response.headers.get("retry-after")), batch

    async def _split(self, batch: list[_Item]) -> tuple[float | None, list[_Item]]:
        """413. 반으로 나눠 보내고 하나여도 크면 버린다. 앞쪽이 막히면 뒤쪽은 보내지 않고 함께
        남긴다."""
        if len(batch) <= 1:
            self.too_large += len(batch)
            return None, []
        half = len(batch) // 2
        first, rest = batch[:half], batch[half:]
        wait, unsent = await self._send(first)
        if wait is not None:
            return wait, unsent + rest
        return await self._send(rest)

    def _next_backoff(self, retry_after: str | None) -> float:
        wait = self._backoff + random.uniform(0, self._backoff / 2)
        self._backoff = min(BACKOFF_MAX_SECONDS, self._backoff * 2)
        if retry_after is not None:
            with contextlib.suppress(ValueError):
                wait = max(wait, min(BACKOFF_MAX_SECONDS, float(retry_after)))
        return wait

    def _envelope(self, batch: list[_Item]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": self._revision,
            "pod": self._pod,
            "boot_id": self._boot_id,
            "sent_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "dropped": dict(self.dropped),
            "counters": {"correlation_reuse": llm_http.CORRELATION.correlation_reuse},
            "events": [item.event for item in batch],
        }

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
        return self._client


def _refused(response: httpx2.Response) -> int:
    """200 답의 `rejected[]`(이벤트 하나씩의 거절) 수다. 코드는 경고 줄에 남긴다. 읽지 못하면 0
    이다."""
    try:
        body = response.json()
    except ValueError:
        return 0
    rejected = body.get("rejected") if isinstance(body, dict) else None
    if not isinstance(rejected, list) or not rejected:
        return 0
    codes = sorted({str(r.get("code")) for r in rejected if isinstance(r, dict)})
    log.warning("events: the platform refused %d events %s", len(rejected), codes)
    return len(rejected)


def _queue_of(event: dict[str, Any]) -> str:
    return COST if event.get("daegam_event") in COST_EVENTS else OTHER


def shipper_from_env(
    env: dict[str, str], *, token: str | None, revision: int
) -> EventShipper | None:
    """env 가 수집 주소와 파드 이름·uid 를 주고 토큰이 있을 때만 만든다. 없으면 stdout 에만 쓴다."""
    url = env.get("DAEGAM_EVENTS_URL")
    if not url or not token:
        return None
    raw = env.get("DAEGAM_EVENTS_FLUSH_SECONDS")
    try:
        flush = float(raw) if raw else DEFAULT_FLUSH_SECONDS
    except ValueError:
        flush = DEFAULT_FLUSH_SECONDS
    return EventShipper(
        url,
        token,
        revision=revision,
        pod_name=env.get("DAEGAM_POD_NAME") or env.get("HOSTNAME") or "unknown",
        pod_uid=env.get("DAEGAM_POD_UID") or "unknown",
        flush_seconds=max(0.05, flush),
    )


def event_size(text: str) -> int:
    """큐가 세는 이벤트 하나의 크기(바이트)다. stdout 에 쓴 줄과 같다."""
    return len(text.encode("utf-8"))
