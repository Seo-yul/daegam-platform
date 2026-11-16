"""기계가 부르는 수집 경로 (P37 3절·8절, D62).

- `/telemetry/events`: Agent 런타임이 인스턴스 토큰으로 관측 이벤트를 밀어 보낸다. 본문 상한은
  1 MiB 다(`INGEST_BODY_LIMIT`). 판단은 `services/telemetry/ingest.py` 에 있다
- `/telemetry/v1/traces`·`/v1/logs`: LLM 게이트웨이가 서비스 토큰으로 OTLP/HTTP JSON 사용량을
  보낸다. 본문 상한은 4 MiB 다(`OTLP_BODY_LIMIT`). 판단은 `services/telemetry/gateway_usage.py` 에
  있다

사람도 비서도 부르지 않으므로 OpenAPI 에 싣지 않고, 몇 초마다 오는 기계의 쓰기라 감사에서도 뺀다
(`audit/middleware.py` 의 `UNAUDITED_PATHS`).
"""

from __future__ import annotations

import zlib
from typing import Any

from fastapi import APIRouter, Request

from daegam.api._common import not_found
from daegam.api.deps import bearer_token
from daegam.errors import error
from daegam.urls import TELEMETRY_EVENTS_PATH, TELEMETRY_OTLP_PATHS, exact_api_paths

router = APIRouter(include_in_schema=False)

# 수집 경로의 본문 상한. 앱 전체(업로드)보다 작다
INGEST_BODY_LIMIT = (exact_api_paths([TELEMETRY_EVENTS_PATH]), 1 << 20)
OTLP_BODY_LIMIT = (exact_api_paths(TELEMETRY_OTLP_PATHS.values()), 4 << 20)
JSON = "application/json"
GZIP = "gzip"


@router.post(TELEMETRY_EVENTS_PATH)
async def post_events(request: Request) -> dict[str, Any]:
    """`{accepted, duplicates, rejected: [{id, code}]}`. 상한을 넘은 이벤트는 그것만 거절한다.
    `DAEGAM_TELEMETRY_INGEST_ENABLED` 가 꺼져 있으면 404 다(P37 17절)."""
    if not request.app.state.settings.telemetry_ingest_enabled:
        raise not_found("telemetry ingest is off")
    token = bearer_token(request)
    return await request.app.state.telemetry_ingest.ingest(token, await request.body())


@router.post(TELEMETRY_OTLP_PATHS["traces"])
async def post_otlp_traces(request: Request) -> dict[str, Any]:
    """게이트웨이의 inference span. 답은 OTLP 의 `ExportTraceServiceResponse` 다."""
    return await _otlp(request, "traces")


@router.post(TELEMETRY_OTLP_PATHS["logs"])
async def post_otlp_logs(request: Request) -> dict[str, Any]:
    """게이트웨이의 접근 로그. 답은 OTLP 의 `ExportLogsServiceResponse` 다."""
    return await _otlp(request, "logs")


async def _otlp(request: Request, kind: str) -> dict[str, Any]:
    """JSON 만 받는다. protobuf 는 415 이고, 게이트웨이와 플랫폼 사이의 OTel Collector 가 JSON 으로
    바꾼다. 토큰을 확인하고 요청 수를 센 뒤에야 gzip 을 푼다."""
    token = bearer_token(request)
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != JSON:
        raise error(415, "unsupported_media_type", "only OTLP/HTTP JSON (application/json)")
    gateway = request.app.state.gateway_usage
    cluster_id = await gateway.admit(token)
    raw = _inflated(request.headers.get("content-encoding"), await request.body())
    return await gateway.ingest(cluster_id, raw, kind)


def _inflated(encoding: str | None, raw: bytes) -> bytes:
    """OTel Collector 의 otlp_http 내보내기는 기본이 gzip 이다. 풀어도 본문 상한을 넘지 않을 때만
    받는다."""
    if not encoding or encoding.strip().lower() == "identity":
        return raw
    if encoding.strip().lower() != GZIP:
        raise error(415, "unsupported_media_type", "only gzip or no content encoding")
    limit = OTLP_BODY_LIMIT[1]
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(raw, limit + 1)
    except zlib.error as e:
        raise error(422, "invalid_json", "the gzip body is broken") from e
    if len(out) > limit or inflater.unconsumed_tail:
        raise error(413, "too_large", f"the inflated body is over {limit} bytes")
    if not inflater.eof or inflater.unused_data:
        raise error(422, "invalid_json", "the gzip body is cut or has trailing data")
    return out
