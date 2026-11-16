"""게이트웨이 사용량 수집 (P37 8절): 서비스 토큰이 클러스터를 정하고, 부른 쪽 속성이 테넌트를
정하며, 다시 보내도 한 행이고, 회수한 토큰은 401 이고, OTLP 의 부분 성공으로 답한다."""

from __future__ import annotations

import gzip
import json
import time
from typing import Any

import jwt
import pytest

from daegam.auth.signer import CONTROL_PLANE_AUDIENCE, TELEMETRY_AUDIENCE
from daegam.services.telemetry import gateway_usage
from daegam.services.telemetry.otlp_json import encode_attributes
from daegam.store.usage import UsageJoin, UsageSource
from daegam.urls import TELEMETRY_OTLP_PATHS
from tests.helpers.auth import bearer
from tests.helpers.clusters import register_cluster
from tests.helpers.http import detail_code

TRACES = TELEMETRY_OTLP_PATHS["traces"]
LOGS = TELEMETRY_OTLP_PATHS["logs"]
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALL_SPAN = "00f067aa0ba902b7"
END_NS = str(int(time.time() * 1e9))
START_NS = str(int(time.time() * 1e9) - 250_000_000)


def _caller(tenant: str = "default", kind: str = "agent", caller: str = "ag-1") -> dict[str, Any]:
    return {
        "daegam.caller.tenant": tenant,
        "daegam.caller.kind": kind,
        "daegam.caller.id": caller,
    }


def _span(span_id: str = "a1b2c3d4e5f60718", **attrs: Any) -> dict[str, Any]:
    values = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 500,
        "daegam.provider": "openai-main",
        "daegam.traceparent": f"00-{TRACE}-{CALL_SPAN}-01",
        **_caller(),
        **attrs,
    }
    return {
        "traceId": "11111111111111111111111111111111",
        "spanId": span_id,
        "parentSpanId": "2222222222222222",
        "startTimeUnixNano": START_NS,
        "endTimeUnixNano": END_NS,
        "attributes": encode_attributes(values),
    }


def _traces(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def _token(client, cluster_id: str, *, aud: str = TELEMETRY_AUDIENCE, **claims: Any) -> str:
    issuer = client.app.state.internal_issuer
    token = issuer.mint(
        sub=f"svc:llm-gateway:{cluster_id}", tenant="ignored", groups=[], audiences=[aud]
    )
    if not claims:
        return token
    decoded = jwt.decode(token, options={"verify_signature": False})
    return issuer.signer.sign({**decoded, **claims})


@pytest.fixture
def gateway(client, fake_clusters) -> tuple[str, dict[str, str]]:
    cluster_id = register_cluster(client, "edge")
    return cluster_id, bearer(_token(client, cluster_id))


def _records(mongo) -> list[dict[str, Any]]:
    return list(mongo["usage_records"].find())


def test_a_span_becomes_one_row_and_a_resend_stays_one(client, mongo, gateway):
    cluster_id, auth = gateway
    body = _traces(_span(), _span("ffffffffffffffff", **{"gen_ai.operation.name": "router"}))
    r = client.post(TRACES, json=body, headers=auth)
    assert r.status_code == 200, r.text
    # 게이트웨이 자신의 span 은 거절로 세지 않는다
    assert r.json() == {}
    again = client.post(TRACES, json=body, headers=auth)
    assert again.status_code == 200 and again.json() == {}
    (row,) = _records(mongo)
    assert row["_id"] == f"{TRACE}-{CALL_SPAN}" and row["trace_id"] == TRACE
    assert (row["writer"], row["route"], row["joined"]) == ("gateway", "gateway", False)
    assert row["tenant"] == "default" and row["source"]["kind"] == "agent"
    assert row["source"]["id"] == "ag-1"
    assert row["provider"] == {"kind": "openai", "name": "openai-main"}
    assert row["usage"]["input_tokens"] == 1000 and row["estimated"] is True
    assert row["gateway"] == {"cluster_id": cluster_id, "record_id": "a1b2c3d4e5f60718"}
    assert 200 <= row["latency_ms"] <= 300 and row["cost_usd"] is not None
    assert mongo["usage_anomalies"].count_documents({}) == 0


def test_records_without_the_caller_are_refused_alone(client, mongo, gateway):
    _, auth = gateway
    lonely = _span("0000000000000002", **{"daegam.caller.tenant": None})
    stranger = _span("0000000000000003", **{"daegam.caller.kind": "platform"})
    huge = _span("0000000000000004", **{"gen_ai.usage.input_tokens": 10**13})
    body = _traces(_span(), lonely, stranger, huge)
    r = client.post(TRACES, json=body, headers=auth)
    assert r.status_code == 200
    partial = r.json()["partialSuccess"]
    # 메시지는 거절한 까닭마다 그 수다
    assert partial["rejectedSpans"] == "3"
    assert partial["errorMessage"] == (
        "2 records lack the caller attributes (daegam.caller.*); "
        "1 records carry values out of range or of the wrong type"
    )
    assert len(_records(mongo)) == 1


def test_a_grader_record_is_the_agents_until_its_join_marks_the_run(client, mongo, gateway):
    """채점 모델(`evaluator`)의 기록은 부른 쪽 id 가 원본 Agent 다. 잇기 전에는 그 Agent 의 행이고,
    플랫폼의 잇기 문서가 미리 보기 표시와 실행을 더한다 (P45 11절). 목적 없는 플랫폼 기록은 여전히
    거절한다."""
    _, auth = gateway
    purpose = {"daegam.caller.purpose": "evaluator"}
    graded = _span(**_caller(kind="platform", caller="ag-1"), **purpose)
    other = _span("0000000000000005", **_caller(kind="platform", caller="ag-1"))
    r = client.post(TRACES, json=_traces(graded, other), headers=auth)
    assert r.status_code == 200 and r.json()["partialSuccess"]["rejectedSpans"] == "1"
    (row,) = _records(mongo)
    assert row["source"] == {"kind": "agent", "id": "ag-1", "name": None}
    assert row["joined"] is False
    join = UsageJoin(
        id=f"{TRACE}-{CALL_SPAN}",
        tenant="default",
        source=UsageSource(kind="agent", id="ag-1", name="faq", preview="pv-1"),
        ref="run-1",
        user_sub="u1",
        trace_id=TRACE,
    )
    ledger = client.app.state.usage_ledger
    client.portal.call(ledger.join, join)
    (row,) = _records(mongo)
    assert row["joined"] is True and row["ref"] == "run-1"
    assert row["source"] == {"kind": "agent", "id": "ag-1", "name": "faq", "preview": "pv-1"}


def test_without_our_header_the_parent_span_or_a_gateway_id_names_the_call(client, mongo, gateway):
    _, auth = gateway
    continued = _span("0000000000000004", **{"daegam.traceparent": None})
    alone = _span("0000000000000005", **{"daegam.traceparent": None})
    alone.pop("parentSpanId")
    client.post(TRACES, json=_traces(continued, alone), headers=auth)
    ids = {r["_id"]: r.get("trace_id") for r in _records(mongo)}
    assert ids == {
        "11111111111111111111111111111111-2222222222222222": "11111111111111111111111111111111",
        "gw:11111111111111111111111111111111-0000000000000005": None,
    }


def test_logs_and_gzip_are_taken_and_protobuf_is_not(client, mongo, gateway):
    _, auth = gateway
    record = {
        "timeUnixNano": END_NS,
        "attributes": encode_attributes(
            {
                "gen_ai.operation.name": "embeddings",
                "gen_ai.provider.name": "openai",
                "gen_ai.request.model": "text-embedding-3-small",
                "gen_ai.usage.input_tokens": 300,
                "http.request.id": "req-1",
                **_caller(kind="platform", caller="knowledge_indexer"),
            }
        ),
    }
    body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}).encode()
    headers = {**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"}
    r = client.post(LOGS, content=gzip.compress(body), headers=headers)
    assert r.status_code == 200, r.text
    (row,) = _records(mongo)
    assert row["operation"] == "embeddings" and row["source"]["kind"] == "knowledge_indexer"
    assert row["_id"].startswith("gw:") and row["gateway"]["record_id"] == "req-1"
    proto = {**auth, "Content-Type": "application/x-protobuf"}
    r = client.post(LOGS, content=b"\x0a\x00", headers=proto)
    assert detail_code(r) == (415, "unsupported_media_type")
    bomb = {**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"}
    r = client.post(LOGS, content=gzip.compress(b" " * (5 << 20)), headers=bomb)
    assert detail_code(r) == (413, "too_large")


def test_only_a_gateway_token_of_a_registered_cluster_passes(client, gateway):
    cluster_id, _ = gateway
    body = _traces(_span())

    def status(token: str) -> int:
        return client.post(TRACES, json=body, headers=bearer(token)).status_code

    assert status(_token(client, cluster_id, aud=CONTROL_PLANE_AUDIENCE)) == 401
    assert status(_token(client, "no-such-cluster")) == 401
    assert status(_token(client, cluster_id, sub=f"svc:vmcp:{cluster_id}")) == 401
    assert status("not-a-token") == 401
    assert client.post(TRACES, json=body).status_code == 401


def test_revoking_stops_older_tokens(client, gateway):
    cluster_id, _ = gateway
    old = bearer(_token(client, cluster_id, iat=int(time.time()) - 60))
    assert client.post(TRACES, json=_traces(_span()), headers=old).status_code == 200
    path = "/admin/telemetry/gateway-tokens/revoke"
    r = client.post(path, json={"cluster_id": cluster_id})
    assert r.status_code == 200, r.text
    assert r.json()["cluster_id"] == cluster_id and r.json()["not_before"]
    r = client.post(TRACES, json=_traces(_span()), headers=old)
    assert r.status_code == 401
    # 회수 뒤에 낸 토큰은 지나간다. 회수 시각은 초를 버려 같은 초의 새 토큰도 지나간다
    fresh = bearer(_token(client, cluster_id))
    assert client.post(TRACES, json=_traces(_span()), headers=fresh).status_code == 200
    r = client.post(path, json={"cluster_id": "no-such-cluster"})
    assert detail_code(r) == (422, "unknown_cluster")


def test_limits_hold_per_gateway(client, gateway, monkeypatch):
    _, auth = gateway
    # 두 수집 입구가 한 슬롯 묶음을 함께 쓴다
    state = client.app.state
    assert state.telemetry_ingest._slots is state.gateway_usage._slots
    monkeypatch.setattr(gateway_usage, "MAX_RECORDS", 2)
    r = client.post(TRACES, json=_traces(_span(), _span(), _span()), headers=auth)
    assert detail_code(r) == (422, "invalid_value")
    buckets = client.app.state.gateway_usage._buckets
    monkeypatch.setattr(buckets, "rate", 1.0)
    monkeypatch.setattr(buckets, "burst", 1.0)
    buckets.clear()
    assert client.post(TRACES, json=_traces(), headers=auth).status_code == 200
    r = client.post(TRACES, json=_traces(), headers=auth)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
