"""LLM 게이트웨이의 사용량 내보내기 (P38 8절).

수집기 렌더와 게이트웨이의 트레이싱 설정, 동기화가 내는 수집 토큰, 그리고 수집기가 바꾼 OTLP/HTTP
JSON 을 P37 의 수집 경로가 받는 계약을 본다. 계약의 본문은 collector-contrib 의 otlp_http
(`encoding: json`)가 내는 꼴이다. 64비트 정수는 글자이고 id 는 소문자 16진이며 resource 와 scope 가
있다.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import yaml

from daegam.auth.signer import TELEMETRY_AUDIENCE
from daegam.k8s.client import decode_secret_data
from daegam.k8s.templates import llm_gateway as tpl
from daegam.k8s.templates.llm_gateway_config import TRACE_FIELDS
from daegam.urls import TELEMETRY_OTLP_PATHS
from tests.helpers.aio import run_db
from tests.helpers.auth import bearer
from tests.helpers.clusters import register_cluster
from tests.helpers.llm_gateway import HOME_ID, NS, T0, gateway_namespace, make_rig
from tests.helpers.svc_tokens import claims_of
from tests.test_llm_gateway_render import IMAGES

TRACES = TELEMETRY_OTLP_PATHS["traces"]
INGEST = "http://daegam-platform.daegam-system.svc:9000/api/telemetry/v1/traces"


def _collector() -> list[dict[str, Any]]:
    config = tpl.collector_config(INGEST)
    return [
        *tpl.render_collector(IMAGES, namespace=NS, install_id="inst", config=config),
        tpl.collector_policy(
            namespace=NS, install_id="inst", ingest_namespace="daegam-system", ingest_port=9000
        ),
    ]


def test_the_collector_turns_protobuf_into_json_for_the_platform():
    configmap, deployment, service, policy = _collector()
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    exporter = config["exporters"]["otlp_http"]
    # 0.163 부터 otlphttp 는 옛 별칭이다(T83)
    assert "otlphttp" not in config["exporters"]
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["otlp_http"]
    assert exporter["traces_endpoint"] == INGEST and exporter["encoding"] == "json"
    assert exporter["auth"] == {"authenticator": "bearertokenauth"}
    assert exporter["sending_queue"]["enabled"] and exporter["retry_on_failure"]["enabled"]
    # 토큰은 파일에서 읽으므로 바꿔도 수집기가 구르지 않는다
    assert config["extensions"]["bearertokenauth"] == {"filename": "/token/token"}
    assert config["processors"]["filter/usage"]["traces"]["span"] == [
        'attributes["gen_ai.operation.name"] == nil'
    ]
    assert config["receivers"]["otlp"]["protocols"] == {"grpc": {"endpoint": "0.0.0.0:4317"}}
    spec = deployment["spec"]
    assert spec["replicas"] == 2 and spec["strategy"]["rollingUpdate"] == {"maxUnavailable": 0}
    pod = spec["template"]["spec"]
    assert (pod["securityContext"]["runAsUser"], pod["securityContext"]["runAsGroup"]) == (
        10001,
        10001,
    )
    assert {"name": "token", "secret": {"secretName": "llm-otel-token"}} in pod["volumes"]
    assert service["spec"]["ports"][0]["port"] == 4317
    rules = policy["spec"]
    assert rules["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 4317}]
    assert rules["egress"][1]["ports"] == [{"protocol": "TCP", "port": 9000}]


def test_only_the_collector_mounts_the_ingest_token():
    from tests.test_llm_gateway_render import _workloads

    for doc in [*_workloads(), *_collector()]:
        if doc["kind"] != "Deployment":
            continue
        volumes = json.dumps(doc["spec"]["template"]["spec"].get("volumes", []))
        assert ("llm-otel-token" in volumes) == (doc["metadata"]["name"] == "llm-otel")


def test_every_request_is_traced_with_the_caller_from_the_jwt(settings):
    """caller traceparent 의 sampled 표시(00)가 span 을 건너뛰게 하지 못하고, traceparent 는
    속성으로 읽지 않는다 (F0 7번)."""
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        config = json.loads(rig.config()["config.yaml"])
        tracing = config["config"]["tracing"]
        assert tracing["otlpEndpoint"] == f"http://llm-otel.{NS}.svc.cluster.local:4317"
        assert (tracing["randomSampling"], tracing["clientSampling"]) == ("true", "true")
        assert tracing["fields"]["add"] == TRACE_FIELDS
        assert "traceparent" not in json.dumps(tracing)
        # 수집기로 나가는 길이 게이트웨이 정책에 있다
        gw = rig.obj("networking.k8s.io/v1", "NetworkPolicy", tpl.GATEWAY)
        assert tpl.collector_egress() in gw["spec"]["egress"]

    run_db(settings, go)


def test_the_sync_mints_the_ingest_token_and_replaces_it(settings):
    fake = gateway_namespace()

    def token(rig) -> str:
        secret = rig.obj("v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET)
        return decode_secret_data(secret["data"])[tpl.COLLECTOR_TOKEN_KEY]

    async def go(db):
        rig = await make_rig(db, fake)
        rig.now[0] = T0.replace(year=2000)
        await rig.gw.sync()
        first = token(rig)
        claims = claims_of(first)
        assert claims["aud"] == TELEMETRY_AUDIENCE and claims["sub"] == f"svc:llm-gateway:{HOME_ID}"
        assert claims["tenant"] == "_llm-gateway" and claims["exp"] - claims["iat"] == 30 * 86400
        rig.now[0] = rig.now[0].replace(year=2000)
        await rig.gw.sync()
        assert token(rig) == first
        # 회수(P37 의 not_before)가 그 토큰보다 뒤면 새로 낸다
        rig.gw.telemetry = _NotBefore(at_after=claims["iat"] + 1)
        await rig.gw.sync()
        assert token(rig) != first

    run_db(settings, go)


class _NotBefore:
    def __init__(self, at_after: int):
        from datetime import UTC, datetime

        self.at = datetime.fromtimestamp(at_after, tz=UTC).replace(tzinfo=None)

    async def not_before(self, _cluster_id: str):
        return self.at


def test_the_ingest_token_is_replaced_after_fifteen_days(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        secret = rig.obj("v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET)
        first = decode_secret_data(secret["data"])[tpl.COLLECTOR_TOKEN_KEY]
        from datetime import UTC, datetime

        issued = datetime.fromtimestamp(claims_of(first)["iat"], tz=UTC).replace(tzinfo=None)
        rig.now[0] = issued + timedelta(days=15)
        await rig.gw.sync()
        secret = rig.obj("v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET)
        assert decode_secret_data(secret["data"])[tpl.COLLECTOR_TOKEN_KEY] != first

    run_db(settings, go)


# -- 계약: 수집기가 바꾼 JSON 을 P37 이 받는다 --------------------------------------------

CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _collector_json(*spans: dict[str, Any]) -> dict[str, Any]:
    """collector-contrib otlp_http(encoding: json)의 꼴. resource 에 서비스 이름이 있다."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "agentgateway")]},
                "scopeSpans": [{"scope": {"name": "agentgateway"}, "spans": list(spans)}],
            }
        ]
    }


def _span(span_id: str, *, parent: str | None, trace: str, status: int = 200, **extra: Any) -> dict:
    now = int(time.time() * 1e9)
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-opus-5-5",
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 300,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "http.status": status,
        "daegam.caller.tenant": "default",
        "daegam.caller.kind": "agent",
        "daegam.caller.id": "ag-1",
        "daegam.provider": "claude-main-876521",
        **extra,
    }
    span = {
        "traceId": trace,
        "spanId": span_id,
        "name": "POST /p/claude-main-876521/v1/messages",
        "kind": 2,
        "startTimeUnixNano": str(now - 300_000_000),
        "endTimeUnixNano": str(now),
        "attributes": [_attr(k, v) for k, v in attrs.items()],
        "status": {},
        "flags": 0,
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def test_the_platform_ingest_accepts_the_collector_json(client, mongo, fake_clusters):
    """caller 의 traceparent 를 sampled 00 으로 보낸 호출은 그 trace 를 잇고(호출 id 가 caller 의
    span), traceparent 없이 온 호출은 게이트웨이의 trace 라 `gw:` id 다. 위의 오류는
    `http.status` 로 error 이고, 경로 헤더의 slug 는 등록된 provider 이름으로 돌아간다."""
    provider = {"name": "Claude.Main", "kind": "anthropic", "api_key_secret": "k"}
    assert client.patch("/settings", json={"llm_providers": [provider]}).status_code == 200
    cluster_id = register_cluster(client, "edge")
    issuer = client.app.state.internal_issuer
    token = issuer.mint(
        sub=f"svc:llm-gateway:{cluster_id}",
        tenant="_llm-gateway",
        groups=[],
        audiences=[TELEMETRY_AUDIENCE],
    )
    body = _collector_json(
        _span("1111111111111111", parent=CALLER_SPAN, trace=CALLER_TRACE),
        _span("2222222222222222", parent=None, trace="a" * 32, status=502),
    )
    r = client.post(TRACES, json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json() == {}
    rows = {row["_id"]: row for row in mongo["usage_records"].find()}
    joined = rows[f"{CALLER_TRACE}-{CALLER_SPAN}"]
    assert joined["status"] == "ok" and joined["trace_id"] == CALLER_TRACE
    assert joined["provider"] == {"kind": "anthropic", "name": "Claude.Main"}
    assert joined["usage"]["cache_read_tokens"] == 1000 and joined["estimated"] is False
    [own] = [row for key, row in rows.items() if key.startswith("gw:")]
    assert own["status"] == "error" and own["trace_id"] is None


def test_the_status_shows_the_ingest_token_expiry_and_the_last_receipt(settings):
    """호출이 흐르기 전에 상태가 수집 토큰의 만료와 P37 수집 경로의 마지막 수신을 보인다 (11절)."""
    from datetime import UTC, datetime

    from daegam.store.telemetry import MongoTelemetryIngestStore

    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        telemetry = MongoTelemetryIngestStore(db)
        rig.gw.telemetry = telemetry
        await rig.gw.sync()
        status = await rig.gw.status()
        token = decode_secret_data(rig.obj("v1", "Secret", tpl.COLLECTOR_TOKEN_SECRET)["data"])
        exp = claims_of(token[tpl.COLLECTOR_TOKEN_KEY])["exp"]
        expires = datetime.fromtimestamp(exp, tz=UTC).replace(tzinfo=None)
        assert status["ingest_token_expires_at"] == expires
        assert status["ingest_received_at"] is None
        at = datetime(2026, 11, 9, 10, 0, 0)
        await telemetry.received(HOME_ID, at)
        assert (await rig.gw.status())["ingest_received_at"] == at

    run_db(settings, go)


def test_the_ingest_records_when_a_gateway_last_sent(client, mongo):
    cluster_id = register_cluster(client, "edge")
    token = client.app.state.internal_issuer.mint(
        sub=f"svc:llm-gateway:{cluster_id}",
        tenant="_llm-gateway",
        groups=[],
        audiences=[TELEMETRY_AUDIENCE],
    )
    assert client.post(TRACES, json=_collector_json(), headers=bearer(token)).status_code == 200
    doc = mongo["settings"].find_one({"_id": "telemetry_ingest"})
    assert cluster_id in doc["received"]
