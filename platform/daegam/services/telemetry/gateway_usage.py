"""LLM 게이트웨이의 사용량 수집 (P37 8절, D62). 게이트웨이 경로를 지난 호출은 게이트웨이 기록이 원장
행이다.

- 게이트웨이가 OTLP/HTTP JSON 으로 보낸다(`/api/telemetry/v1/traces`, `/v1/logs`). protobuf 는
  415 다
- 인증은 플랫폼이 서명한 서비스 토큰이다. `aud` 는 `daegam-telemetry`, `sub` 는
  `svc:llm-gateway:<클러스터 id>` 이고 등록된 클러스터여야 한다. `tenant` 클레임은 읽지 않는다.
  클러스터마다의 `not_before`(30초 기억)보다 먼저 낸 토큰은 401 이다
- 기록마다 부른 쪽 속성(`daegam.caller.tenant`·`kind`·`id`)이 테넌트와 부른 쪽이다. 없으면 그 기록만
  거절한다. `gen_ai.operation.name` 이 chat·embeddings·text_completion·generate_content 인 기록만
  받는다
- 호출 id 는 `daegam.traceparent` 를 먼저 읽고, 없으면 span 의 `traceId`·`parentSpanId` 다. 둘 다
  없으면 `gw:` id 다. 행은 `writer: gateway`, `joined: false` 로 들어가고 잇기 문서가 있으면
  이어진다(ledger.py)
- 상한은 기록 5,000개와 게이트웨이마다 초당 20 요청이다. 답은 OTLP 의 부분 성공이다
- `daegam.provider` 는 게이트웨이 경로의 provider slug 다. 등록된 provider 의 이름으로 되돌린다.
  v1.5.0 의 span 은 위의 오류에 오류 상태를 싣지 않으므로 `http.status` 가 400 이상이면 `error`
  다 (P38 8절)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from pymongo.errors import PyMongoError

from daegam import llm_http
from daegam.auth.local import LocalVerifier
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import InternalIssuer, service_sub
from daegam.auth.signer import TELEMETRY_AUDIENCE
from daegam.errors import ServiceError
from daegam.names import provider_slug
from daegam.services.llm_endpoint import EVALUATOR
from daegam.services.telemetry import otlp_json
from daegam.services.telemetry.ingest import (
    CONCURRENCY,
    RETRY_AFTER_SECONDS,
    TS_FUTURE,
    TS_PAST,
    IngestError,
    KeyedBuckets,
    busy,
    ingest_slot,
)
from daegam.services.usage.ledger import UsageLedger
from daegam.store.base import error_kind, utcnow
from daegam.store.clusters import MongoClusterStore
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.telemetry import MongoTelemetryIngestStore
from daegam.store.usage import TokenUsage, UsageProvider, UsageRecord, UsageSource
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.telemetry.gateway")

GATEWAY_SUB_PREFIX = service_sub("llm-gateway", "")
# 요청 수를 세는 버킷은 게이트웨이(클러스터)마다 하나이고 한 시간 오지 않으면 잊는다
BUCKET_IDLE_SECONDS = 3600.0
BUCKET_ENTRIES = 1000
MAX_RECORDS = 5000
REQUESTS_PER_SECOND = 20.0
NOT_BEFORE_CACHE_SECONDS = 30.0
RECEIVED_WRITE_SECONDS = 30.0
# 받는 호출 종류 → 원장의 operation
OPERATIONS = {
    "chat": "chat",
    "text_completion": "chat",
    "generate_content": "chat",
    "embeddings": "embeddings",
}
PLATFORM_SOURCES = frozenset({"assistant", "spec_generator", "knowledge_indexer"})
# 캐시·추론 몫의 속성. 새 이름과 옛 이름(관례의 옛 판, agentgateway 의 접근 로그)을 다 읽는다
CACHE_READ = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cached_tokens")
CACHE_WRITE = ("gen_ai.usage.cache_write.input_tokens", "gen_ai.usage.cache_creation.input_tokens")
REASONING = ("gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens")
# span 의 오류 상태. OTLP JSON 은 enum 을 수로 싣지만 이름으로 싣는 내보내기도 있다
SPAN_STATUS_ERROR = (2, "STATUS_CODE_ERROR")
# agentgateway v1.5.0 의 span 은 위의 오류에도 오류 상태가 없다. 응답 상태로 가른다 (P38 8절)
HTTP_STATUS = "http.status"
HTTP_ERROR_FROM = 400
# log 기록에서 기록 id 로 읽는 속성. 앞의 것이 먼저다
LOG_RECORD_IDS = ("log.record.uid", "http.request.id", "request.id")
TRACES = "traces"
# 부분 성공으로 거절한 까닭과 그 메시지다
REFUSED_CALLER = "caller"
REFUSED_VALUES = "values"
REFUSED_WRITE = "write"
REFUSED_REASONS = {
    REFUSED_CALLER: "records lack the caller attributes (daegam.caller.*)",
    REFUSED_VALUES: "records carry values out of range or of the wrong type",
    REFUSED_WRITE: "records could not be written to the ledger",
}
LOGS = "logs"


class UnknownCluster(ServiceError):
    """회수할 클러스터가 등록되지 않았다. 422 unknown_cluster 다."""

    def __init__(self, cluster_id: str):
        super().__init__(422, "unknown_cluster", f"cluster {cluster_id!r} is not registered")


class GatewayUsageIngest:
    def __init__(
        self,
        issuer: InternalIssuer,
        clusters: MongoClusterStore,
        revocations: MongoTelemetryIngestStore,
        ledger: UsageLedger,
        *,
        slots: asyncio.Semaphore | None = None,
        clock: Any = time.monotonic,
        provider_names: Callable[[], Awaitable[dict[str, str]]] | None = None,
    ):
        self.issuer = issuer
        # provider slug → 등록된 이름 (P38 8절). 없으면 slug 그대로다
        self.provider_names = provider_names
        self.clusters = clusters
        self.revocations = revocations
        self.ledger = ledger
        self._not_before: TTLCache[str, tuple[datetime | None]] = TTLCache(
            NOT_BEFORE_CACHE_SECONDS, clock=clock
        )
        self._received: TTLCache[str, bool] = TTLCache(RECEIVED_WRITE_SECONDS, clock=clock)
        self._buckets = KeyedBuckets(
            REQUESTS_PER_SECOND,
            REQUESTS_PER_SECOND,
            idle_seconds=BUCKET_IDLE_SECONDS,
            max_entries=BUCKET_ENTRIES,
            clock=clock,
        )
        # 런타임 이벤트 수집 경로와 같은 자리 수다(레플리카마다 32)
        self._slots = slots or asyncio.Semaphore(CONCURRENCY)

    async def admit(self, token: str) -> str:
        """토큰을 검증하고 그 게이트웨이의 요청 수를 센다. 본문을 풀기 전에 부른다. 클러스터 id
        다."""
        cluster_id = await self.authenticate(token)
        self._admit(cluster_id)
        await self._note_received(cluster_id)
        return cluster_id

    async def _note_received(self, cluster_id: str) -> None:
        """게이트웨이 상태의 마지막 수신 (P38 11절). 30초에 한 번까지만 쓴다. 실패는 수집을 막지
        않는다."""
        if self._received.get(cluster_id) is not None:
            return
        self._received.put(cluster_id, True)
        try:
            await self.revocations.received(cluster_id, utcnow())
        except PyMongoError as e:
            logger.warning("gateway usage: receipt not recorded: %s", error_kind(e))

    async def ingest(self, cluster_id: str, raw: bytes, kind: str) -> dict[str, Any]:
        """admit 을 지난 요청의 본문. 기록마다 행으로 넣고, 넣지 못한 기록은 그것만 부분 성공의
        거절로 센다. 저장소가 흔들리면 503 이라 게이트웨이가 다시 보낸다(다시 보내도 한 번이다)."""
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise IngestError(422, "invalid_json", "the body is not JSON") from e
        records = list(otlp_json.spans(body) if kind == TRACES else otlp_json.log_records(body))
        if len(records) > MAX_RECORDS:
            raise IngestError(422, "invalid_value", f"at most {MAX_RECORDS} records")
        refused: Counter[str] = Counter()
        names = await self.provider_names() if self.provider_names is not None else {}
        async with ingest_slot(self._slots):
            for record in records:
                try:
                    row = gateway_row(record, kind, cluster_id, names)
                except ValidationError:
                    refused[REFUSED_VALUES] += 1
                    continue
                if row is None:
                    refused[REFUSED_CALLER] += record_is_usage(record)
                    continue
                try:
                    await self.ledger.write(row)
                except PyMongoError as e:
                    logger.warning("gateway usage: store failed: %s", error_kind(e))
                    raise busy("could not store the usage records") from e
                except Exception as e:  # noqa: BLE001 - 그 기록만 거절이다
                    logger.warning("gateway usage: record %s refused: %s", row.id, error_kind(e))
                    refused[REFUSED_WRITE] += 1
        return partial_success(kind, refused)

    async def authenticate(self, token: str) -> str:
        """서비스 토큰을 검증하고 그 게이트웨이의 클러스터 id 를 돌려준다. 틀리면 401 이다."""
        verifier = LocalVerifier(
            self.issuer.signer,
            issuer=self.issuer.issuer,
            audience=TELEMETRY_AUDIENCE,
            required=("exp", "iat", "sub"),
        )
        try:
            claims = verifier.verify(token)
        except AuthError as e:
            raise _unauthorized() from e
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.startswith(GATEWAY_SUB_PREFIX):
            raise _unauthorized()
        cluster_id = sub.removeprefix(GATEWAY_SUB_PREFIX)
        if not cluster_id or await self.clusters.get(cluster_id) is None:
            raise _unauthorized()
        not_before = await self._not_before_of(cluster_id)
        if not_before is not None and claims["iat"] < not_before.replace(tzinfo=UTC).timestamp():
            raise _unauthorized()
        return cluster_id

    async def _not_before_of(self, cluster_id: str) -> datetime | None:
        """그 클러스터의 회수 시각(naive UTC). 30초 기억하므로 회수는 30초 안에 먹는다."""
        hit = self._not_before.get(cluster_id)
        if hit is None:
            hit = (await self.revocations.not_before(cluster_id),)
            self._not_before.put(cluster_id, hit)
        return hit[0]

    async def revoke(self, cluster_id: str) -> datetime:
        """그 클러스터의 지금까지의 토큰을 무른다. 이 레플리카에는 곧바로, 다른 레플리카에는 30초
        안에 먹는다. `iat` 이 초 단위라 초를 버린 시각을 둔다. 그래야 같은 초에 새로 낸 토큰이
        지나간다."""
        if await self.clusters.get(cluster_id) is None:
            raise UnknownCluster(cluster_id)
        at = utcnow().replace(microsecond=0)
        await self.revocations.revoke(cluster_id, at)
        self._not_before.pop(cluster_id)
        return at

    def _admit(self, cluster_id: str) -> None:
        if not self._buckets.take(cluster_id):
            raise IngestError(
                429,
                "telemetry_rate_limited",
                "too many usage requests from this gateway",
                retry_after=RETRY_AFTER_SECONDS,
            )


def record_is_usage(record: dict[str, Any]) -> bool:
    """받는 호출 종류의 기록인가. 다른 기록(게이트웨이 자신의 span)은 거절로 세지 않는다."""
    return record["_attrs"].get("gen_ai.operation.name") in OPERATIONS


def gateway_row(
    record: dict[str, Any], kind: str, cluster_id: str, names: Mapping[str, str] | None = None
) -> UsageRecord | None:
    """OTLP 기록 하나의 원장 행. 받지 않는 기록이나 부른 쪽 속성이 없는 기록은 None 이다. names 는
    provider slug → 이름이다."""
    attrs = record["_attrs"]
    operation = OPERATIONS.get(attrs.get("gen_ai.operation.name"))
    tenant, source = attrs.get("daegam.caller.tenant"), _source(attrs)
    if operation is None or not isinstance(tenant, str) or not tenant or source is None:
        return None
    end = otlp_json.nanos(record.get("endTimeUnixNano") or record.get("timeUnixNano"))
    start = otlp_json.nanos(record.get("startTimeUnixNano"))
    now = utcnow()
    ts = end or otlp_json.nanos(record.get("observedTimeUnixNano"))
    if ts is None or not now - TS_PAST <= ts <= now + TS_FUTURE:
        ts = now
    latency = (end - start).total_seconds() * 1000 if end and start else None
    record_id = _record_id(record, kind)
    call_id, trace_id = _call_id(record, record_id)
    usage, estimated = _usage(attrs)
    status = (record.get("status") or {}).get("code")
    http_status = attrs.get(HTTP_STATUS)
    failed = (
        status in SPAN_STATUS_ERROR
        or attrs.get("error.type")
        or (isinstance(http_status, int) and http_status >= HTTP_ERROR_FROM)
    )
    provider = _text(attrs.get("daegam.provider"))
    provider_kind = attrs.get("gen_ai.provider.name")
    return UsageRecord(
        id=call_id,
        ts=ts,
        writer="gateway",
        route="gateway",
        tenant=tenant,
        source=source,
        trace_id=trace_id,
        joined=False,
        provider=UsageProvider(
            kind=provider_kind if isinstance(provider_kind, str) else "unknown",
            name=(names or {}).get(provider, provider) if provider else None,
        ),
        model=_text(attrs.get("gen_ai.request.model")) or "unknown",
        response_model=_text(attrs.get("gen_ai.response.model")),
        operation=operation,
        usage=usage,
        estimated=estimated,
        status="error" if failed else "ok",
        latency_ms=latency if latency is not None and latency >= 0 else None,
        record_id=record_id,
        gateway={"cluster_id": cluster_id, "record_id": record_id},
    )


def partial_success(kind: str, refused: Counter[str]) -> dict[str, Any]:
    """OTLP 의 부분 성공 답이다. 다 받았으면 비어 있다. 메시지는 거절한 까닭마다 그 수다."""
    total = sum(refused.values())
    if not total:
        return {}
    field = "rejectedSpans" if kind == TRACES else "rejectedLogRecords"
    message = "; ".join(f"{n} {REFUSED_REASONS[reason]}" for reason, n in refused.items() if n)
    return {"partialSuccess": {field: str(total), "errorMessage": message}}


def _source(attrs: dict[str, Any]) -> UsageSource | None:
    """기록의 부른 쪽. 채점 모델의 기록은 잇기 전의 임시 부른 쪽이 원본 Agent 이고, 플랫폼의 잇기
    문서가 미리 보기 표시와 실행을 더한다 (P45 11절)."""
    kind, caller = attrs.get("daegam.caller.kind"), _text(attrs.get("daegam.caller.id"))
    if caller is None:
        return None
    if kind == "agent":
        return UsageSource(kind="agent", id=caller)
    # 평가의 채점 모델(P45 11절)의 기록은 부른 쪽 id 가 원본 Agent 다
    if kind == "platform" and attrs.get("daegam.caller.purpose") == EVALUATOR:
        return UsageSource(kind="agent", id=caller)
    if kind == "platform" and caller in PLATFORM_SOURCES:
        return UsageSource(kind=caller)  # type: ignore[arg-type]
    return None


def _call_id(record: dict[str, Any], record_id: str) -> tuple[str, str | None]:
    """(호출 id, trace id). 우리 헤더를 옮긴 속성이 먼저이고, 없으면 게이트웨이가 우리 trace 를
    이었다고 보고 span 의 부모를 쓴다. 둘 다 없으면 우리 trace 가 아니므로 `gw:` id 이고 trace 가
    없다."""
    if call := llm_http.parse_traceparent(record["_attrs"].get("daegam.traceparent")):
        return call.id, call.trace_id
    trace_id = _lower(record.get("traceId"))
    if trace_id and (parent := _lower(record.get("parentSpanId"))):
        return f"{trace_id}-{parent}", trace_id
    if trace_id and (span_id := _lower(record.get("spanId"))):
        return f"gw:{trace_id}-{span_id}", None
    return f"gw:{hashlib.sha256(record_id.encode()).hexdigest()[:32]}", None


def _record_id(record: dict[str, Any], kind: str) -> str:
    """기록 자신의 id(span 은 spanId, log 는 기록 id 나 요청 id). 다시 보냄과 같은 호출의 다른
    기록을 가른다. 없으면 기록 내용의 해시라 다시 보내도 같다."""
    attrs = record["_attrs"]
    names = [attrs.get(n) for n in LOG_RECORD_IDS]
    candidates = [record.get("spanId")] if kind == TRACES else names
    if found := next((c for c in map(_text, candidates) if c), None):
        return found
    raw = json.dumps({k: v for k, v in record.items() if k != "_attrs"}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _usage(attrs: dict[str, Any]) -> tuple[TokenUsage, bool]:
    """(사용량, 추정인가). 캐시 몫이 없으면 입력 전부를 입력 가격으로 매기고 추정이라 표시한다."""
    cache_read = _first(attrs, CACHE_READ)
    cache_write = _first(attrs, CACHE_WRITE)
    usage = TokenUsage(
        input_tokens=_count(attrs.get("gen_ai.usage.input_tokens")),
        output_tokens=_count(attrs.get("gen_ai.usage.output_tokens")),
        cache_read_tokens=_count(cache_read),
        cache_write_tokens=_count(cache_write),
        reasoning_tokens=_count(_first(attrs, REASONING)),
    )
    return usage, cache_read is None and cache_write is None


def _first(attrs: dict[str, Any], names: tuple[str, ...]) -> Any:
    return next((attrs[n] for n in names if attrs.get(n) is not None), None)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _lower(value: Any) -> str | None:
    """OTLP JSON 의 16진 id 는 소문자가 관례다. 대문자로 온 것도 같은 호출로 읽는다."""
    text = _text(value)
    return text.lower() if text else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _unauthorized() -> IngestError:
    return IngestError(401, "unauthorized", "invalid gateway token")


def provider_names(
    platform_settings: MongoPlatformSettingsStore,
) -> Callable[[], Awaitable[dict[str, str]]]:
    """수집 요청마다 provider slug → 이름 표를 읽는 함수. 게이트웨이 경로의 헤더는 slug 다."""

    async def names() -> dict[str, str]:
        ps = await platform_settings.get()
        return {provider_slug(p.name): p.name for p in ps.llm_providers}

    return names
