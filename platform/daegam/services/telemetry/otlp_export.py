"""OTLP 내보내기 (P37 15절). 플랫폼이 이벤트 저장소에서 실행 단위로 내보낸다.

- 표본은 `turn_end` 가 들어올 때 정한다. 오류로 끝난 실행은 `always_errors` 면 늘 보내고, 나머지는
  trace id 의 끝 16자를 64비트 수로 읽어 `sample_ratio × 2^64` 보다 작으면 보낸다. 같은 trace 는 늘
  같게 정해진다. 보낼 실행은 `otlp_outbox` 에 받은 때에서 10초 뒤로 들어간다. 늦게 오는 span 을
  기다린다
- 내보내기 루프(리스 `otlp-export`, 5초마다)가 준비된 실행을 50개씩, 요청 하나에 4 MiB 까지 묶어
  보내고 맞으면 보냈다고 적는다(지우지 않고 TTL 까지 둔다). 413 이면 묶음을 반으로 나눈다. 실행
  하나를 만들지 못하면(Agent·`turn_end`·run span 이 없거나 문서가 깨졌다) 그것만 버리고 센다.
  실패하면 1초에서 5분까지 두 배씩 물러난다. 실패가 열 번을 넘거나 한 시간이 지나면 버리고 센다
- 본문은 `capture_content` 와 그 실행의 리비전(`turn_end.profile.revision`)의 `record_bodies` 가
  함께 켜졌을 때만 간다. 리비전을 모르면 보내지 않는다. 사람을 가리키는 값은 내보내지 않는다
- 같은 턴의 앞 실행이 있으면(승인 뒤 재개) 그 root 로 link 를 단다
- 헤더는 플랫폼 네임스페이스의 `daegam-otlp-` 로 시작하는 Secret 의 키·값이다. 요청의 틀을 바꾸는
  헤더(Host, Content-Length 같은)가 들어 있으면 그 Secret 을 쓰지 않고 실패로 센다. 요청은 플랫폼의
  `httpx` 로 보내고 TLS 검증은 늘 켜져 있다(D39). JSON 을 받지 않는 백엔드는 앞에 OTel Collector 를
  둔다
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool

from daegam.secrets import SecretStore
from daegam.services.claims import TickHold, leased_tick, tick_item
from daegam.services.periodic import cancel_task, run_periodic
from daegam.services.telemetry import otlp_json
from daegam.services.telemetry.normalize import TURN_END, TURN_START, run_failed
from daegam.services.telemetry.traces import MAX_TRACE_EVENTS, AgentTraces
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.leases import Lease
from daegam.store.observability import (
    OTLP_SECRET_PREFIX,
    MongoObservabilityStore,
    OtlpSettings,
)
from daegam.store.otlp import MongoOtlpStore
from daegam.store.usage import MongoUsageStore
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.telemetry.otlp")

LEASE_NAME = "otlp-export"
INTERVAL_SECONDS = 5.0
READY_DELAY = timedelta(seconds=10)
BATCH = 50
BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 300.0
MAX_FAILURES = 10
MAX_AGE = timedelta(hours=1)
SEND_TIMEOUT_SECONDS = 10.0
SETTINGS_CACHE_SECONDS = 10.0
TRACE_ID_HEX = 32
SAMPLE_SPACE = 1 << 64
# 한 요청에 싣는 실행의 크기 합(바이트). OTel Collector 의 기본 요청 상한 아래다
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# 헤더 Secret 이 정하지 못하는 헤더. 요청의 대상과 틀은 플랫폼이 정한다
FIXED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "proxy-connection",
    }
)
SEND_OK = "sent"
SEND_TOO_LARGE = "too_large"
SEND_FAILED = "failed"


class HeadersRefused(Exception):
    """헤더 Secret 을 쓰지 않는 까닭이다. 헤더 값은 싣지 않는다."""


@dataclass(frozen=True)
class _Built:
    """보낼 실행 하나와 그 JSON 크기."""

    item: dict[str, Any]
    spans: dict[str, Any]
    size: int


def _batches(built: list[_Built], cap: int) -> list[list[_Built]]:
    """크기 합이 cap 을 넘지 않게 나눈다. cap 보다 큰 실행 하나는 혼자 간다."""
    out: list[list[_Built]] = []
    size = 0
    for b in built:
        if out and size + b.size <= cap:
            out[-1].append(b)
            size += b.size
        else:
            out.append([b])
            size = b.size
    return out


def _records_bodies(agent: Agent, end: dict[str, Any] | None) -> bool:
    """그 실행의 리비전이 `record_bodies` 를 켰는가. 리비전은 `turn_end.profile.revision` 이고
    모르거나 지난 리비전이 남아 있지 않으면 끈 것이다."""
    profile = (end or {}).get("profile")
    number = profile.get("revision") if isinstance(profile, dict) else None
    revision = agent.get_revision(number) if isinstance(number, int) else None
    return revision is not None and revision.service.observability.record_bodies


def sampled(trace_id: str, status: str | None, otlp: OtlpSettings) -> bool:
    """이 실행을 보내는가. 같은 trace 는 늘 같게 정해진다."""
    if otlp.always_errors and run_failed(status):
        return True
    try:
        point = int(trace_id[-16:], 16)
    except ValueError:
        return False
    return point < otlp.sample_ratio * SAMPLE_SPACE


class OtlpExport:
    def __init__(
        self,
        store: MongoOtlpStore,
        events: MongoAgentEventStore,
        usage: MongoUsageStore,
        agents: MongoAgentStore,
        settings: MongoObservabilityStore,
        lease: Lease,
        *,
        secrets: Callable[[], SecretStore | None],
        namespace: str,
        http: httpx.AsyncClient | None = None,
    ):
        self.store = store
        self.events = events
        self.usage = usage
        self.traces = AgentTraces(events, usage)
        self.agents = agents
        self.settings = settings
        self.lease = lease
        self.secrets = secrets
        self.namespace = namespace
        self._http = http
        self._settings_cache: TTLCache[str, OtlpSettings] = TTLCache(SETTINGS_CACHE_SECONDS)
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    def forget_config(self) -> None:
        """설정을 바꾼 레플리카에서는 곧바로 먹게 한다. 다른 레플리카는 10초 안이다."""
        self._settings_cache.clear()

    async def config(self) -> OtlpSettings:
        hit = self._settings_cache.get("otlp")
        if hit is None:
            hit = (await self.settings.get()).otlp
            self._settings_cache.put("otlp", hit)
        return hit

    # -- 대기열에 넣기 (수집 경로와 수집기가 부른다) ------------------------------------------

    async def offer(self, docs: list[dict[str, Any]]) -> None:
        """들어온 이벤트 가운데 표본에 든 `turn_end` 의 실행을 대기열에 넣는다."""
        otlp = await self.config()
        if not otlp.enabled:
            return
        now = utcnow()
        queued = [
            {
                # 다른 Agent 가 같은 trace id 를 대어 이 실행의 자리를 먼저 차지하지 못한다
                "_id": f"{doc['instance_id']}:{doc['trace_id']}",
                "trace_id": doc["trace_id"],
                "instance_id": doc.get("instance_id"),
                "tenant": doc.get("tenant"),
                "received_at": now,
                "ready_at": now + READY_DELAY,
                "attempts": 0,
            }
            for doc in docs
            if doc.get("event") == TURN_END
            and isinstance(doc.get("trace_id"), str)
            and len(doc["trace_id"]) == TRACE_ID_HEX
            and isinstance(doc.get("instance_id"), str)
            and sampled(doc["trace_id"], doc.get("status"), otlp)
        ]
        await self.store.enqueue(queued)

    # -- 내보내기 루프 ---------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="daegam-otlp-export")

    async def _loop(self) -> None:
        await run_periodic(
            self.tick,
            interval=INTERVAL_SECONDS,
            stop=self._stop,
            log=logger,
            label="otlp export",
            delay_first=True,
        )

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None
        await self.lease.release(LEASE_NAME)
        if self._http is not None:
            await self._http.aclose()

    async def tick(self) -> None:
        await leased_tick(self.lease, LEASE_NAME, self._run, log=logger, label="otlp export")

    async def _run(self, now: datetime, _hold: TickHold) -> None:
        otlp = await self.config()
        if not otlp.enabled or not otlp.endpoint:
            # 끈 뒤에 남은 대기열은 보내지 않는다
            await self.store.clear()
            return
        built: list[_Built] = []
        dropped: list[str] = []
        for item in await self.store.ready(now, BATCH):
            await tick_item()
            try:
                spans = await self._resource_spans(item, otlp)
                # 보낼 글 그대로 잰다. 표준 JSON 이 아닌 값(NaN)이 있으면 이 실행만 버린다
                size = None if spans is None else len(json.dumps(spans, allow_nan=False))
            except Exception as e:  # noqa: BLE001 - 실행 하나의 깨진 문서가 다른 실행을 막으면 안 된다
                logger.warning(
                    "otlp export: could not build trace %s: %s", item["_id"], type(e).__name__
                )
                spans = size = None
            if spans is None or size is None:
                dropped.append(item["_id"])
                continue
            built.append(_Built(item, spans, size))
        if dropped:
            await self.store.remove(dropped)
            await self.store.record(dropped=len(dropped))
        for batch in _batches(built, MAX_REQUEST_BYTES):
            await tick_item()
            await self._deliver(otlp, batch, now)

    async def _deliver(self, otlp: OtlpSettings, batch: list[_Built], now: datetime) -> None:
        """묶음 하나를 보낸다. 413 이면 반으로 나누고, 하나여도 413 이면 버린다."""
        outcome = await self._send(otlp, {"resourceSpans": [b.spans for b in batch]})
        if outcome == SEND_OK:
            await self.store.mark_sent([b.item["_id"] for b in batch], now)
            await self.store.record(success_at=now)
        elif outcome == SEND_TOO_LARGE and len(batch) > 1:
            half = len(batch) // 2
            await self._deliver(otlp, batch[:half], now)
            await self._deliver(otlp, batch[half:], now)
        elif outcome == SEND_TOO_LARGE:
            await self.store.remove([batch[0].item["_id"]])
            await self.store.record(failures=1, dropped=1)
        else:
            await self._back_off([b.item for b in batch], now)

    async def status(self) -> dict[str, Any]:
        return await self.store.status()

    async def _resource_spans(
        self, item: dict[str, Any], otlp: OtlpSettings
    ) -> dict[str, Any] | None:
        """대기열의 실행 하나. 이벤트나 Agent 가 없어졌으면 None 이고 그 항목은 지운다."""
        agent = await self.agents.get(item["instance_id"])
        if agent is None:
            return None
        rows, _ = await self.traces.rows(agent, item["trace_id"])
        ledger = await self.usage.trace_records(agent.tenant, item["trace_id"], MAX_TRACE_EVENTS)
        end = next((r for r in rows if r.get("event") == TURN_END), None)
        return otlp_json.resource_spans(
            rows,
            agent=agent.model_dump(),
            costs={r.id: r.cost_usd for r in ledger},
            capture=otlp.capture_content and _records_bodies(agent, end),
            links=await self._earlier_runs(agent, rows),
        )

    async def _earlier_runs(
        self, agent: Agent, rows: list[dict[str, Any]]
    ) -> list[tuple[str, str]]:
        """같은 턴의 앞 실행 root (trace id, span id). 승인 뒤 재개한 실행이 앞 실행에 잇는다."""
        start = next((r for r in rows if r.get("event") == TURN_START), None)
        if start is None or not start.get("turn_id") or not start.get("session_id"):
            return []
        starts = await self.events.query(
            tenant=agent.tenant,
            instance_id=agent.id,
            session_id=start["session_id"],
            events=(TURN_START,),
            limit=MAX_TRACE_EVENTS,
        )
        return [
            (r["trace_id"], r["span_id"])
            for r in reversed(starts)
            if r.get("turn_id") == start["turn_id"]
            and r.get("trace_id") != start.get("trace_id")
            and r["ts"] < start["ts"]
            and isinstance(r.get("trace_id"), str)
            and isinstance(r.get("span_id"), str)
        ]

    async def _send(self, otlp: OtlpSettings, body: dict[str, Any]) -> str:
        """SEND_OK, SEND_TOO_LARGE 나 SEND_FAILED. 헤더 Secret 을 읽지 못한 것(저장소의 어떤
        예외든)도 SEND_FAILED 라 물러난다."""
        try:
            headers = {"Content-Type": "application/json", **await self._headers(otlp)}
            response = await self._client().post(
                str(otlp.endpoint), json=body, headers=headers, timeout=SEND_TIMEOUT_SECONDS
            )
        except Exception as e:  # noqa: BLE001 - 보내지 못한 까닭이 무엇이든 그 묶음은 물러난다
            reason = str(e) if isinstance(e, HeadersRefused) else type(e).__name__
            logger.warning("otlp export: could not send: %s", reason)
            return SEND_FAILED
        if response.status_code // 100 == 2:
            return SEND_OK
        logger.warning("otlp export: the endpoint answered %d", response.status_code)
        return SEND_TOO_LARGE if response.status_code == 413 else SEND_FAILED

    async def _headers(self, otlp: OtlpSettings) -> dict[str, str]:
        """헤더 Secret 의 키·값. 이름이 `daegam-otlp-` 로 시작하지 않거나, Secret 저장소나 Secret 이
        없거나, 요청의 틀을 바꾸는 헤더가 들어 있으면 보내지 않는다. 이름은 읽기 전에 본다."""
        name = otlp.headers_secret
        if name is None:
            return {}
        if not name.startswith(OTLP_SECRET_PREFIX):
            raise HeadersRefused(f"the headers Secret name must start with {OTLP_SECRET_PREFIX}")
        store = self.secrets()
        if store is None:
            raise HeadersRefused("no Secret store for the OTLP headers")
        data = await run_in_threadpool(store.get, name, namespace=self.namespace)
        if data is None:
            raise HeadersRefused(f"Secret {name} is missing")
        if fixed := sorted(k for k in data if k.lower() in FIXED_HEADERS):
            raise HeadersRefused(f"Secret {name} sets headers the platform owns: {fixed}")
        return dict(data)

    async def _back_off(self, items: list[dict[str, Any]], now: datetime) -> None:
        drop: list[str] = []
        for item in items:
            attempts = int(item.get("attempts") or 0) + 1
            if attempts > MAX_FAILURES or now - item["received_at"] > MAX_AGE:
                drop.append(item["_id"])
                continue
            wait = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * 2 ** (attempts - 1))
            await self.store.retry(item["_id"], attempts, now + timedelta(seconds=wait))
        await self.store.remove(drop)
        await self.store.record(failures=1, dropped=len(drop))
        if drop:
            logger.warning("otlp export: dropped %d traces after repeated failures", len(drop))

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
        return self._http
