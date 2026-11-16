"""런타임 이벤트 수집 경로 `POST /api/telemetry/events` 의 판단 (P37 3절, D62).

- **토큰이 인스턴스를 정한다.** 경로도 봉투도 인스턴스를 말하지 않는다. 제시된 토큰의 `hash_token`
  을 유일 색인으로 찾고, 회수되지 않았고 scope 에 봉투의 리비전이 있을 때 통과한다.
  테넌트·클러스터는 그 Agent 문서에서 오고 이벤트의 `profile`·`instance`·`tenant` 는 읽지 않는다.
  토큰은 본문을 읽기 전에 찾는다. 찾은 토큰은 30초 기억해 회수는 30초 안에 먹고, 없거나 회수된
  토큰의 해시도 30초(10,000개) 기억한다. scope 에 리비전이 없으면 롤아웃 동안 넓어졌을 수 있어
  토큰을 다시 읽는다. 그 요청도 그 Agent 의 버킷에서 센다. 틀린 토큰은 모두 같은 401 이다
- 인증이 동시 처리 자리를 잡기 전이다. 인스턴스마다 파드 안의 토큰 버킷(초당 10 요청, 몰아서 20,
  분당 6,000 이벤트)을 넘으면 429 `telemetry_rate_limited`, 0.5초 안에 자리(레플리카마다 32)를 못
  잡거나 Mongo 쓰기가 실패하면 503 `telemetry_busy` 다. 둘 다 `Retry-After` 를 단다
- 상한: 이벤트 500개, 이벤트 하나 64 KiB, 식별 글자 256자, 본문 칸 8,000자, `data` 깊이 5와 키
  100개. 키 꼴(`KEY`)에 맞지 않는 키는 버려 Mongo 연산자나 경로로 읽히는 키가 없다. 받은 시각보다
  24시간 앞이거나 5분 뒤인 `ts` 는 받은 시각으로 바꾸고 `ts_clamped` 를 단다. 상한을 넘은 이벤트는
  그것만 거절한다(`rejected[{id, code}]`)
- 다시 보내도 늘 안전하다. 이벤트 `id` 로 하나씩 넣으므로 다시 온 이벤트는 `duplicates` 다
- 미리 보기(P44b 9절)의 토큰이면 이벤트를 원본 Agent 의 것으로 넣고 `preview_id` 를 단다. 그래서
  원본의 이벤트·트레이스·원장에 들고 원본의 보관 기한을 따른다. 이벤트의 `instance` 도 원본의 id 와
  이름이다. 버킷과 수집 기록(파드가 밀어 보내는가)은 미리 보기 자신의 것이고, 실행의 상태 옮기기는
  부르지 않는다(미리 보기에는 실행이 없다)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from daegam.errors import ServiceError, retry_after_header
from daegam.services.telemetry.normalize import (
    AGENT_EVENT_KEY,
    BODY_KEYS,
    PLATFORM_EVENTS,
    SOURCE_PUSH,
    event_document,
    numbers_fit,
    preview_mark,
)
from daegam.services.telemetry.otlp_json import is_span_id, is_trace_id
from daegam.services.telemetry.writer import TelemetryWriter
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, scope_for
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import error_kind, hash_token, parse_iso_utc, utcnow
from daegam.store.telemetry import MongoAgentTelemetryStore
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.telemetry.ingest")

SCHEMA_VERSION = 1
MAX_EVENTS = 500
# 봉투의 칸이다. 런타임 shipper 의 `_envelope` 가 같은 칸을 싣는다(test_telemetry_parity). sent_at
# 은 읽지 않는다
ENVELOPE_KEYS = (
    "schema_version",
    "revision",
    "pod",
    "boot_id",
    "sent_at",
    "dropped",
    "counters",
    "events",
)
MAX_EVENT_BYTES = 64 * 1024
MAX_ID_CHARS = 256
MAX_BODY_CHARS = 8000
MAX_DEPTH = 5
MAX_KEYS = 100
KEY = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
TS_PAST = timedelta(hours=24)
TS_FUTURE = timedelta(minutes=5)
AUTH_CACHE_SECONDS = 30.0
REFUSED_ENTRIES = 10_000
CONCURRENCY = 32
SLOT_WAIT_SECONDS = 0.5
REQUESTS_PER_SECOND = 10.0
REQUEST_BURST = 20.0
EVENTS_PER_MINUTE = 6000.0
RETRY_AFTER_SECONDS = 1
# 인스턴스마다의 버킷은 한 시간 오지 않으면 잊는다
LIMITS_IDLE_SECONDS = 3600.0
LIMITS_ENTRIES = 10_000
# 거절의 경고 줄은 Agent 마다 분에 한 번이다
WARN_EVERY_SECONDS = 60.0
# 이벤트 하나의 거절 코드다
REJECT_SHAPE = "invalid_event"
REJECT_SIZE = "too_large"
REJECT_RESERVED = "reserved_event"


class IngestError(ServiceError):
    """요청 전체의 거절. 앱의 전역 처리기가 그 상태와 코드로 답하고 되밀기는 `Retry-After` 를
    단다."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        headers = retry_after_header(timedelta(seconds=retry_after)) if retry_after else None
        super().__init__(status, code, message, headers=headers)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Source:
    """토큰이 정한 인스턴스다. 미리 보기면 agent_id 는 원본의 것이고 preview_id 가 미리 보기
    자신이다 (P44b 9절)."""

    agent_id: str
    tenant: str
    cluster_id: str | None
    namespace: str | None
    preview_id: str | None = None
    # 이벤트 문서가 덮어쓰는 미리 보기의 칸(`preview_mark`). 미리 보기가 아니면 비었다
    mark: Mapping[str, Any] = field(default_factory=dict)

    @property
    def instance_id(self) -> str:
        """이벤트를 보낸 문서 자신. 버킷과 수집 기록이 쓴다."""
        return self.preview_id or self.agent_id


@dataclass(frozen=True)
class _Passed:
    """찾은 토큰. 그 Agent 와 리비전 scope 다."""

    source: Source
    instance_id: str
    scope: frozenset[str]


@dataclass
class Bucket:
    """토큰 버킷 하나. 비율만큼 차오르고 burst 를 넘지 않는다."""

    rate: float
    burst: float
    tokens: float
    at: float

    def take(self, n: float, now: float) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens < n:
            return False
        self.tokens -= n
        return True


class KeyedBuckets:
    """열쇠(Agent, 클러스터)마다의 토큰 버킷. 처음 보면 가득 찬 버킷을 세우고, 쓸 때마다 다시 넣어
    한가한 동안(idle_seconds)만 잊는다. 잊으면 버킷이 가득 찬 채로 다시 선다."""

    def __init__(
        self,
        rate: float,
        burst: float,
        *,
        idle_seconds: float,
        max_entries: int,
        clock: Callable[[], float],
    ):
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._buckets: TTLCache[str, Bucket] = TTLCache(
            idle_seconds, clock=clock, max_entries=max_entries
        )

    def take(self, key: str, n: float = 1) -> bool:
        now = self._clock()
        bucket = self._buckets.get(key) or Bucket(self.rate, self.burst, self.burst, now)
        self._buckets.put(key, bucket)
        return bucket.take(n, now)

    def clear(self) -> None:
        """모든 버킷을 잊는다. 다음 요청부터 가득 찬 버킷이 선다."""
        self._buckets.clear()


@dataclass
class Outcome:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # 저장한(또는 이미 있던) 이벤트 문서. `on_written` 이 받는다
    written: list[dict[str, Any]] = field(default_factory=list)


# 이벤트를 저장한 뒤 부르는 것. (Agent id, 테넌트, 문서) 이고 실패를 스스로 삼킨다
WrittenHook = Callable[[str, str, list[dict[str, Any]]], Awaitable[None]]


class TelemetryIngest:
    def __init__(
        self,
        tokens: MongoAgentInstanceTokenStore,
        agents: MongoAgentStore,
        writer: TelemetryWriter,
        stats: MongoAgentTelemetryStore,
        *,
        slots: asyncio.Semaphore | None = None,
        clock: Any = time.monotonic,
        on_written: WrittenHook | None = None,
    ):
        self.tokens = tokens
        # 실행의 상태를 턴 이벤트로 옮긴다 (P41 8절)
        self.on_written = on_written
        # 토큰이 미리 보기의 것일 수 있다 (P44b 9절)
        self.agents = agents.with_previews()
        self.writer = writer
        self.stats = stats
        self._passed: TTLCache[str, _Passed] = TTLCache(AUTH_CACHE_SECONDS, clock=clock)
        self._refused: TTLCache[str, bool] = TTLCache(
            AUTH_CACHE_SECONDS, clock=clock, max_entries=REFUSED_ENTRIES
        )
        self._warned: TTLCache[str, bool] = TTLCache(WARN_EVERY_SECONDS, clock=clock)
        self._requests = KeyedBuckets(
            REQUESTS_PER_SECOND,
            REQUEST_BURST,
            idle_seconds=LIMITS_IDLE_SECONDS,
            max_entries=LIMITS_ENTRIES,
            clock=clock,
        )
        self._events = KeyedBuckets(
            EVENTS_PER_MINUTE / 60,
            EVENTS_PER_MINUTE,
            idle_seconds=LIMITS_IDLE_SECONDS,
            max_entries=LIMITS_ENTRIES,
            clock=clock,
        )
        self._slots = slots or asyncio.Semaphore(CONCURRENCY)

    async def ingest(self, token: str | None, raw: bytes) -> dict[str, Any]:
        digest, passed = await self._token(token)
        envelope = _envelope(raw)
        self._admit(passed.source.instance_id, len(envelope["events"]))
        source = await self._revision(digest, passed, envelope["revision"])
        async with ingest_slot(self._slots):
            outcome = await self._write(source, envelope)
        if self.on_written is not None and outcome.written and source.preview_id is None:
            await self.on_written(source.agent_id, source.tenant, outcome.written)
        if outcome.rejected:
            self._warn(source.instance_id, outcome.rejected)
        return {
            "accepted": outcome.accepted,
            "duplicates": outcome.duplicates,
            "rejected": outcome.rejected,
        }

    async def _token(self, token: str | None) -> tuple[str, _Passed]:
        """제시된 토큰의 (해시, 찾은 토큰). 본문을 읽기 전에 부른다. 틀리면 모두 같은 401 이다."""
        if not token:
            raise _unauthorized()
        digest = hash_token(token)
        if self._refused.get(digest):
            raise _unauthorized()
        if (known := self._passed.get(digest)) is None:
            known = await self._lookup(digest)
        return digest, known

    async def _lookup(self, digest: str) -> _Passed:
        found = await self.tokens.get_by_hash(digest)
        agent = None
        if found is not None and found.active:
            agent = await self.agents.get(found.agent_instance_id)
        if agent is not None and agent.preview is not None:
            # 자격이 끝난 미리 보기의 토큰은 지우기 전이라도 받지 않는다 (P44b 8절)
            agent = agent if utcnow() < agent.preview.credentials_end else None
        if found is None or agent is None:
            self._refused.put(digest, True)
            raise _unauthorized()
        known = _Passed(_source_of(agent), found.agent_instance_id, frozenset(found.scope))
        self._passed.put(digest, known)
        return known

    async def _revision(self, digest: str, passed: _Passed, revision: int) -> Source:
        """봉투의 리비전이 토큰의 scope 에 있어야 한다. 없으면 롤아웃 동안 넓어졌을 수 있어 한 번
        다시 읽는다."""
        if scope_for(passed.instance_id, revision) in passed.scope:
            return passed.source
        self._passed.pop(digest)
        fresh = await self._lookup(digest)
        if scope_for(fresh.instance_id, revision) not in fresh.scope:
            raise _unauthorized()
        return fresh.source

    def _admit(self, agent_id: str, events: int) -> None:
        if not (self._requests.take(agent_id) and self._events.take(agent_id, events)):
            raise IngestError(
                429,
                "telemetry_rate_limited",
                "too many telemetry requests or events for this agent",
                retry_after=RETRY_AFTER_SECONDS,
            )

    async def _write(self, source: Source, envelope: dict[str, Any]) -> Outcome:
        now = utcnow()
        pod = envelope["pod"]
        outcome = Outcome()
        docs = []
        for event in envelope["events"]:
            doc, code = _accept(event, now)
            if doc is None:
                outcome.rejected.append({"id": _event_id(event), "code": code})
                continue
            docs.append(
                {
                    **doc,
                    "tenant": source.tenant,
                    "instance_id": source.agent_id,
                    "cluster_id": source.cluster_id,
                    "namespace": source.namespace,
                    "pod": pod["name"],
                    "pod_uid": pod["uid"],
                    "boot_id": envelope["boot_id"],
                    "collected_at": now,
                    **source.mark,
                }
            )
        try:
            inserted = await self.writer.write(docs)
            await self.stats.record(
                instance_id=source.instance_id,
                tenant=source.tenant,
                pod_uid=pod["uid"],
                pod_name=pod["name"],
                boot_id=envelope["boot_id"],
                counts={
                    "received": len(docs),
                    "duplicates": len(docs) - inserted,
                    "refused": len(outcome.rejected),
                },
                reported=envelope["reported"],
                now=now,
            )
        except Exception as e:  # noqa: BLE001 - 저장소가 흔들리면 보낸 쪽이 다시 보낸다
            logger.warning(
                "telemetry: write failed for agent %s: %s", source.agent_id, error_kind(e)
            )
            raise busy("could not store the events") from e
        outcome.accepted = inserted
        outcome.duplicates = len(docs) - inserted
        outcome.written = docs
        return outcome

    def _warn(self, agent_id: str, rejected: list[dict[str, Any]]) -> None:
        """거절의 경고 줄. Agent 마다 분에 한 번이고 토큰 조각이나 본문은 싣지 않는다."""
        if self._warned.get(agent_id):
            return
        self._warned.put(agent_id, True)
        codes = sorted({r["code"] for r in rejected})
        logger.warning(
            "telemetry: agent %s sent %d refused events %s", agent_id, len(rejected), codes
        )


def _envelope(raw: bytes) -> dict[str, Any]:
    """봉투를 읽고 검사한다. 깊이가 지나친 JSON 도 422 다."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise IngestError(422, "invalid_json", "the body is not JSON") from e
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        raise _invalid("schema_version must be 1")
    revision, events, pod = body.get("revision"), body.get("events"), body.get("pod")
    if not isinstance(revision, int) or revision < 1:
        raise _invalid("revision must be a positive integer")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise _invalid(f"events must be a list of at most {MAX_EVENTS}")
    if not isinstance(pod, dict) or not all(_short(pod.get(k)) for k in ("name", "uid")):
        raise _invalid("pod needs a name and a uid")
    boot_id = body.get("boot_id")
    if not _short(boot_id):
        raise _invalid("boot_id is required")
    return {
        "revision": revision,
        "events": events,
        "pod": {"name": pod["name"], "uid": pod["uid"]},
        "boot_id": boot_id,
        "reported": _reported(body),
    }


def _reported(body: dict[str, Any]) -> dict[str, int]:
    """봉투가 알린 이 부트의 누적. 버린 수와 correlation_reuse 다."""
    dropped = body.get("dropped") if isinstance(body.get("dropped"), dict) else {}
    counters = body.get("counters") if isinstance(body.get("counters"), dict) else {}
    values = {
        "dropped_cost": dropped.get("cost"),
        "dropped_other": dropped.get("other"),
        "correlation_reuse": counters.get("correlation_reuse"),
    }
    return {k: v for k, v in values.items() if isinstance(v, int) and v >= 0}


def _accept(event: Any, now: datetime) -> tuple[dict[str, Any] | None, str]:
    """이벤트 하나를 받는다. (문서, 거절 코드).

    크기는 저장할 글(UTF-8 바이트)로 잰다. 상한을 넘으면 본문 칸을 빼고 `truncated` 를 단다.
    그래도 넘으면 거절이다. 본문 밖의 글은 256자에서, 본문 글은 8,000자에서 자르고, 목록은 100개에서
    자르며 `truncated` 를 단다. 64비트 정수 밖의 수와 NaN·Infinity 는 그 이벤트의 거절이다
    (`numbers_fit`)."""
    if not isinstance(event, dict) or not _short(event.get("id")):
        return None, REJECT_SHAPE
    if event.get(AGENT_EVENT_KEY) in PLATFORM_EVENTS:
        return None, REJECT_RESERVED
    cut = _Cut()
    clean = _clean(event, depth=0, cut=cut)
    if not isinstance(clean, dict) or not numbers_fit(clean):
        return None, REJECT_SHAPE
    clean = _trim_strings(clean, cut)
    if _size(clean) > MAX_EVENT_BYTES:
        clean = {k: v for k, v in clean.items() if k not in BODY_KEYS}
        cut.hit = True
        if _size(clean) > MAX_EVENT_BYTES:
            return None, REJECT_SIZE
    if cut.hit:
        clean["truncated"] = True
    doc = event_document(clean, source=SOURCE_PUSH)
    if doc is None or not _ids_ok(doc):
        return None, REJECT_SHAPE
    ts = parse_iso_utc(event.get("ts"))
    if ts is None or not now - TS_PAST <= ts <= now + TS_FUTURE:
        doc["ts"], doc["ts_clamped"] = now, True
    return doc, ""


@dataclass
class _Cut:
    """이벤트를 받으며 잘라낸 것이 있는가."""

    hit: bool = False


def _clean(value: Any, *, depth: int, cut: _Cut) -> Any:
    """키 꼴에 맞지 않는 키를 버린다. 깊이를 넘는 값은 None 이고 100개를 넘는 목록은 자른다."""
    if isinstance(value, dict):
        if depth >= MAX_DEPTH or len(value) > MAX_KEYS:
            return None
        return {
            k: _clean(v, depth=depth + 1, cut=cut)
            for k, v in value.items()
            if KEY.fullmatch(str(k))
        }
    if isinstance(value, list):
        if depth >= MAX_DEPTH:
            return None
        if len(value) > MAX_KEYS:
            cut.hit = True
        return [_clean(v, depth=depth + 1, cut=cut) for v in value[:MAX_KEYS]]
    return value


def _trim_strings(event: dict[str, Any], cut: _Cut) -> dict[str, Any]:
    """맨 위의 글을 자른다. 본문 칸은 8,000자, 그 밖의 칸은 256자다."""
    out: dict[str, Any] = {}
    for key, value in event.items():
        limit = MAX_BODY_CHARS if key in BODY_KEYS else MAX_ID_CHARS
        if isinstance(value, str) and len(value) > limit:
            value = value[:limit]
            cut.hit = True
        out[key] = value
    return out


def _size(event: dict[str, Any]) -> int:
    """저장할 글의 바이트. 한글은 세 바이트다."""
    return len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))


def _ids_ok(doc: dict[str, Any]) -> bool:
    """trace·span id 는 W3C 의 소문자 16진 글자다. 없는 것은 괜찮다(옛 런타임, 점 이벤트)."""
    checks = (("trace_id", is_trace_id), ("span_id", is_span_id), ("parent_span_id", is_span_id))
    return all(doc.get(k) is None or ok(doc[k]) for k, ok in checks)


def _short(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS


def _event_id(event: Any) -> str | None:
    value = event.get("id") if isinstance(event, dict) else None
    return value if _short(value) else None


def _source_of(agent: Agent) -> Source:
    """토큰의 Agent 가 정한 인스턴스. 미리 보기는 원본의 것으로 넣는다 (P44b 9절)."""
    k8s = agent.k8s
    return Source(
        agent_id=agent.acting_id,
        tenant=agent.tenant,
        cluster_id=agent.cluster_id,
        namespace=getattr(k8s, "namespace", None) if k8s is not None else None,
        preview_id=agent.id if agent.preview is not None else None,
        mark=preview_mark(agent),
    )


def _unauthorized() -> IngestError:
    return IngestError(401, "unauthorized", "invalid or revoked token")


def _invalid(message: str) -> IngestError:
    return IngestError(422, "invalid_value", message)


def busy(message: str) -> IngestError:
    return IngestError(503, "telemetry_busy", message, retry_after=RETRY_AFTER_SECONDS)


@asynccontextmanager
async def ingest_slot(slots: asyncio.Semaphore) -> AsyncIterator[None]:
    """수집 자리 하나. 0.5초 안에 자리가 나지 않으면 503 telemetry_busy 다. 런타임 이벤트와
    게이트웨이 사용량 수집이 같은 자리 수를 나눠 쓴다."""
    try:
        await asyncio.wait_for(slots.acquire(), SLOT_WAIT_SECONDS)
    except TimeoutError as e:
        raise busy("no free ingest slot") from e
    try:
        yield
    finally:
        slots.release()
