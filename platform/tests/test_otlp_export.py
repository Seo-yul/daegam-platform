"""OTLP 내보내기 (P37 15절): 설정의 검사, 표본, 대기열, 실행의 span 모양, 실패의 물러남과 버림."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from daegam import urlfetch, urls
from daegam.services.telemetry import otlp_export, otlp_json
from daegam.services.telemetry.otlp_export import sampled
from daegam.store.base import utcnow
from daegam.store.observability import OtlpSettings
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.http import detail_code
from tests.helpers.telemetry import (
    RUN,
    TRACE,
    TRACE2,
    model_call,
    push_events,
    run_event,
    tool_exec,
    turn_end,
)

ENDPOINT = "https://otel.example.com/v1/traces"
HEADERS_SECRET = "daegam-otlp-headers"


class Collector:
    """OTLP 받는 쪽 자리. 답할 상태를 차례로 내고 받은 요청을 기억한다."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200, json={})

    def body(self, n: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[n].content)


OTLP = "/settings/observability/otlp"


def _enable(client, **otlp: Any) -> None:
    r = client.put(OTLP, json={"enabled": True, "endpoint": ENDPOINT, **otlp})
    assert r.status_code == 200, r.text


def _export(client, collector: Collector):
    export = client.app.state.otlp_export
    export.forget_config()
    export._http = httpx.AsyncClient(transport=httpx.MockTransport(collector))
    return export


def _ready_now(mongo) -> None:
    mongo["otlp_outbox"].update_many({}, {"$set": {"ready_at": utcnow() - timedelta(seconds=1)}})


def _a_run(
    client, agent, *, trace: str = TRACE, status: str = "done", at: float = 0, **end: Any
) -> None:
    """실행 하나. at 초만큼 늦게 시작한다."""
    push_events(
        client,
        agent,
        [
            run_event("turn_start", at, trace_id=trace, span_id=RUN, message=None),
            model_call(
                at + 1, "a000000000000001", trace_id=trace, ttft_ms=300, finish_reason="stop"
            ),
            run_event("model_retry", at + 1.1, trace_id=trace, attempt=1, model="gpt-4o"),
            tool_exec(at + 2, "c000000000000001", trace_id=trace, call_id="call-1"),
            turn_end(at + 3, status, 3000.0, trace_id=trace, **end),
        ],
    )


def test_the_endpoint_and_ratio_are_checked(client):
    def patch(otlp: dict[str, Any]):
        return client.put(OTLP, json=otlp)

    refused = (
        "http://otel.example.com/v1/traces",
        "https://localhost/v1/traces",
        "https://169.254.169.254/v1/traces",
        "https://[::1]/v1/traces",
        "https://otel.example.com/v1/traces?token=x",
        "https://user@otel.example.com/v1/traces",
        # 숫자 호스트의 줄임 꼴과 틀린 포트
        "https://127.1/v1/traces",
        "https://2130706433/v1/traces",
        "https://0x7f000001/v1/traces",
        "https://0177.0.0.1/v1/traces",
        "https://otel.example.com:99999/v1/traces",
        "https://otel.example.com:0/v1/traces",
    )
    for endpoint in refused:
        assert detail_code(patch({"endpoint": endpoint}))[0] == 422, endpoint
    assert detail_code(patch({"enabled": True}))[0] == 422
    assert detail_code(patch({"endpoint": ENDPOINT, "sample_ratio": 2}))[0] == 422
    # 헤더 Secret 은 daegam-otlp- 로 시작한다. 서명 키 같은 플랫폼의 다른 Secret 을 내보내지 못한다
    assert detail_code(patch({"headers_secret": "daegam-signing-key"}))[0] == 422
    in_cluster = "http://otel-collector.observability.svc.cluster.local:4318/v1/traces"
    r = patch({"enabled": True, "endpoint": in_cluster, "headers_secret": HEADERS_SECRET})
    assert r.status_code == 200, r.text
    assert r.json()["headers_secret"] == HEADERS_SECRET
    assert client.get(OTLP).json() == r.json()
    # 끝에 점이 붙은 완전한 이름도 같은 호스트다
    assert patch({"endpoint": "http://otel.observability.svc.:4318/v1/traces"}).status_code == 200
    # 비서의 설정 도구가 쓰는 PATCH 로는 바꾸지 못한다
    r = client.patch("/settings", json={"observability": {"otlp": {}}})
    assert r.status_code == 422


def test_urls_reads_the_url_fetch_tables():
    """보내는 쪽 주소의 검사(urls)는 공개 URL fetch(urlfetch)의 차단 이름을 그대로 쓴다. 허브 항목
    주소의 두 접미사 갈래(P55a 1절)는 urlfetch 의 차단 접미사를 남김없이 나눈다."""
    assert urls.BLOCKED_HOSTS is urlfetch.BLOCKED_HOSTS
    assert set(urls.PRIVATE_SUFFIXES) <= set(urlfetch.BLOCKED_SUFFIXES)
    suffixes = urls.LOCAL_SUFFIXES + urls.PRIVATE_SUFFIXES
    assert sorted(suffixes) == sorted(urlfetch.BLOCKED_SUFFIXES)
    assert urls.export_url_problem("https://203.0.113.7:4318/v1/traces") is None


def test_otlp_settings_live_in_their_own_document(client, mongo):
    """OTLP 설정은 Platform Settings 와 따로인 문서다 (P37 15절)."""
    _enable(client)
    assert client.patch("/settings", json={"default_model": "m"}).status_code == 200
    assert "observability" not in mongo["settings"].find_one({"_id": "platform"})
    assert mongo["settings"].find_one({"_id": "observability"})["otlp"]["enabled"] is True


def test_sampling_keeps_errors_and_decides_a_trace_alike():
    none = OtlpSettings(sample_ratio=0)
    assert sampled(TRACE, "turn_failed", none) and not sampled(TRACE, "done", none)
    assert not sampled(TRACE, "turn_failed", OtlpSettings(sample_ratio=0, always_errors=False))
    assert sampled(TRACE, "done", OtlpSettings(sample_ratio=1))
    half = OtlpSettings(sample_ratio=0.5)
    low, high = "0" * 16 + "1" * 16, "0" * 16 + "f" * 16
    assert sampled(low, "done", half) and not sampled(high, "done", half)


def test_a_run_goes_out_as_genai_spans_without_people(client, fake_clusters, mongo, fake_secrets):
    fake_secrets.put(HEADERS_SECRET, {"Authorization": "Bearer otel"}, namespace=PLATFORM_NS)
    _enable(client, headers_secret=HEADERS_SECRET)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector()
    export = _export(client, collector)
    _a_run(client, agent)
    (queued,) = mongo["otlp_outbox"].find()
    assert queued["_id"] == f"{agent.id}:{TRACE}" and queued["ready_at"] > utcnow()
    # 늦은 span 을 기다리는 10초 동안은 보내지 않는다
    client.portal.call(export.tick)
    assert collector.requests == []
    _ready_now(mongo)
    client.portal.call(export.tick)
    (request,) = collector.requests
    assert request.headers["authorization"] == "Bearer otel"
    (resource,) = collector.body()["resourceSpans"]
    resource_attrs = {a["key"]: a["value"] for a in resource["resource"]["attributes"]}
    assert resource_attrs["service.name"]["stringValue"] == agent.doc["k8s"]["name"]
    assert resource_attrs["service.namespace"]["stringValue"] == "default"
    spans = {s["name"].split(" ")[0]: s for s in resource["scopeSpans"][0]["spans"]}
    assert set(spans) == {"invoke_agent", "chat", "execute_tool"}
    root, chat, tool = spans["invoke_agent"], spans["chat"], spans["execute_tool"]
    assert root["spanId"] == RUN and "parentSpanId" not in root
    assert chat["parentSpanId"] == RUN and chat["kind"] == 3
    chat_attrs = {a["key"]: a["value"] for a in chat["attributes"]}
    assert chat_attrs["gen_ai.provider.name"]["stringValue"] == "openai"
    assert chat_attrs["gen_ai.response.time_to_first_chunk"]["doubleValue"] == 0.3
    assert chat_attrs["daegam.cost.usd"]["doubleValue"] > 0
    tool_attrs = {a["key"]: a["value"] for a in tool["attributes"]}
    assert tool_attrs["gen_ai.tool.type"]["stringValue"] == "extension"
    assert [e["name"] for e in root.get("events", [])] == ["model_retry"]
    # 사람을 가리키는 값은 내보내지 않는다
    assert "alice" not in request.content.decode()
    # 보낸 실행은 TTL 까지 남아 늦게 온 같은 turn_end 가 다시 넣지 못한다
    (sent,) = mongo["otlp_outbox"].find()
    assert sent["sent_at"] and "pending" not in sent
    push_events(client, agent, [turn_end(3, "done", 3000.0)])
    _ready_now(mongo)
    client.portal.call(export.tick)
    assert len(collector.requests) == 1
    status = client.get("/settings/observability/otlp/status").json()
    assert status["last_success"] and status["queued"] == 0


def test_failures_back_off_then_drop(client, fake_clusters, mongo):
    _enable(client)
    agent = deployed_agent(client, fake_clusters, mongo)
    export = _export(client, Collector(503, 503))
    _a_run(client, agent, status="turn_failed", error="boom")
    _ready_now(mongo)
    client.portal.call(export.tick)
    (queued,) = mongo["otlp_outbox"].find()
    assert queued["attempts"] == 1 and queued["ready_at"] > utcnow()
    mongo["otlp_outbox"].update_one({}, {"$set": {"attempts": otlp_export.MAX_FAILURES}})
    _ready_now(mongo)
    client.portal.call(export.tick)
    assert mongo["otlp_outbox"].count_documents({}) == 0
    status = client.get("/settings/observability/otlp/status").json()
    assert (status["failures"], status["dropped"], status["last_success"]) == (2, 1, None)


def test_a_resumed_run_links_its_earlier_run_and_bodies_need_both_switches(
    client, fake_clusters, mongo
):
    _enable(client, capture_content=True)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector()
    export = _export(client, collector)
    revision = agent.doc["deployed_revision"]
    mongo["agents"].update_one(
        {"_id": agent.id, "revisions.revision": revision},
        {"$set": {"revisions.$.service.observability.record_bodies": True}},
    )
    _a_run(client, agent, status="pending")
    _a_run(client, agent, trace=TRACE2, at=10, text="결과: 7", profile={"revision": revision})
    # 리비전을 모르는 실행의 본문은 보내지 않는다
    _a_run(client, agent, trace="3" * 32, at=20, text="결과: 8")
    _ready_now(mongo)
    client.portal.call(export.tick)
    resources = collector.body()["resourceSpans"]
    roots = {
        s["traceId"]: s
        for r in resources
        for s in r["scopeSpans"][0]["spans"]
        if s["name"].startswith("invoke_agent")
    }
    assert roots[TRACE2]["links"] == [{"traceId": TRACE, "spanId": RUN}]
    assert roots[TRACE]["links"] == []
    # 본문은 capture_content 와 그 실행 리비전의 record_bodies 가 함께 켜졌을 때만 간다
    attrs = {a["key"] for a in roots[TRACE2]["attributes"]}
    assert "gen_ai.output.messages" in attrs and "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in {a["key"] for a in roots["3" * 32]["attributes"]}


def test_one_broken_run_does_not_stop_the_others(client, fake_clusters, mongo, monkeypatch):
    """깨진 문서(모양이 틀린 칸, 16진 글자가 아닌 id)의 실행은 그것만 버리고 센다 (보안 B1)."""
    _enable(client)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector()
    export = _export(client, collector)
    _a_run(client, agent)
    _a_run(client, agent, trace=TRACE2, at=10)
    # 저장소에 깨진 모양을 직접 둔다. 수집 경로는 이런 문서를 넣지 않는다
    mongo["agent_events"].update_many(
        {"trace_id": TRACE, "event": "model_call"},
        {"$set": {"duration_ms": "slow", "data.provider": "openai", "data.usage": [1, 2]}},
    )
    mongo["agent_events"].update_many(
        {"trace_id": TRACE2, "event": "turn_end"}, {"$set": {"span_id": "NOT-HEX"}}
    )
    _ready_now(mongo)
    client.portal.call(export.tick)
    (resource,) = collector.body()["resourceSpans"]
    names = sorted(s["name"].split(" ")[0] for s in resource["scopeSpans"][0]["spans"])
    assert names == ["chat", "execute_tool", "invoke_agent"]
    status = client.get(f"{OTLP}/status").json()
    assert status["dropped"] == 1 and status["queued"] == 0


def test_numbers_that_are_not_json_do_not_stop_the_batch(client, fake_clusters, mongo, monkeypatch):
    """NaN·Infinity 와 넘치는 길이는 싣지 않거나 자른다. 그래도 표준 JSON 이 아닌 실행은 그것만
    버리고 같은 묶음의 다른 실행은 나간다 (확인 A3 변형)."""
    _enable(client)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector()
    export = _export(client, collector)
    _a_run(client, agent)
    _a_run(client, agent, trace=TRACE2, at=10)
    mongo["agent_events"].update_many(
        {"trace_id": TRACE, "event": "model_call"},
        {"$set": {"duration_ms": 1e15, "data.ttft_ms": float("nan")}},
    )
    real = otlp_json.resource_spans

    def poisoned(rows, **kwargs):
        out = real(rows, **kwargs)
        if out is not None and rows[0].get("trace_id") == TRACE2:
            out["resource"]["attributes"].append(
                {"key": "x", "value": {"doubleValue": float("nan")}}
            )
        return out

    monkeypatch.setattr(otlp_json, "resource_spans", poisoned)
    _ready_now(mongo)
    client.portal.call(export.tick)
    (resource,) = collector.body()["resourceSpans"]
    chat = next(s for s in resource["scopeSpans"][0]["spans"] if s["name"].startswith("chat"))
    assert int(chat["endTimeUnixNano"]) - int(chat["startTimeUnixNano"]) == 86_400 * 10**9
    assert client.get(f"{OTLP}/status").json()["dropped"] == 1


def test_values_that_are_not_finite_are_left_out() -> None:
    attrs = otlp_json.encode_attributes({"a": float("nan"), "b": [1.5, float("inf")], "c": 2})
    assert attrs == [
        {"key": "b", "value": {"arrayValue": {"values": [{"doubleValue": 1.5}]}}},
        {"key": "c", "value": {"intValue": "2"}},
    ]


def test_a_413_splits_the_batch_and_a_secret_error_backs_off(
    client, fake_clusters, mongo, fake_secrets
):
    _enable(client)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector(413, 200, 200)
    export = _export(client, collector)
    _a_run(client, agent)
    _a_run(client, agent, trace=TRACE2, at=10)
    _ready_now(mongo)
    client.portal.call(export.tick)
    assert [len(json.loads(r.content)["resourceSpans"]) for r in collector.requests] == [2, 1, 1]
    assert mongo["otlp_outbox"].count_documents({"pending": True}) == 0

    def broken(*_args, **_kwargs):
        raise RuntimeError("secret store down")

    fake_secrets.get = broken
    _enable(client, headers_secret=HEADERS_SECRET)
    export.forget_config()
    _a_run(client, agent, trace="1" * 32, at=20)
    _ready_now(mongo)
    client.portal.call(export.tick)
    (queued,) = mongo["otlp_outbox"].find({"pending": True})
    assert queued["attempts"] == 1 and len(collector.requests) == 3


def test_a_secret_cannot_set_the_headers_the_platform_owns(
    client, fake_clusters, mongo, fake_secrets
):
    fake_secrets.put(HEADERS_SECRET, {"Host": "metadata.internal"}, namespace=PLATFORM_NS)
    _enable(client, headers_secret=HEADERS_SECRET)
    agent = deployed_agent(client, fake_clusters, mongo)
    collector = Collector()
    export = _export(client, collector)
    _a_run(client, agent)
    _ready_now(mongo)
    client.portal.call(export.tick)
    assert collector.requests == [] and client.get(f"{OTLP}/status").json()["failures"] == 1
