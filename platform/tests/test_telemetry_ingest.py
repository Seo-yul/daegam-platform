"""런타임 이벤트 수집 경로 (P37 3절): 토큰이 인스턴스를 정하고, 이벤트 하나씩 받거나 거절하며,
다시 보내도 한 번이고, 감사와 OpenAPI 에서 빠지고, 되밀기와 상한을 지킨다."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.store.telemetry import MAX_PODS
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.auth import bearer
from tests.helpers.http import detail_code
from tests.helpers.telemetry import EVENTS, envelope

JSON = "application/json"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _event(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "daegam_event": name,
        "id": uuid.uuid4().hex,
        "ts": "2099-01-01T00:00:00.000+00:00",
        "n": 3,
        "profile": {"id": "ap-forged", "name": "x", "revision": 99},
        "instance": {"id": "someone-else", "name": "faq"},
        "tenant": "other-tenant",
        "session_id": "s1",
        "user_sub": "alice",
        "turn_id": "t1",
        "trace_id": TRACE,
        **fields,
    }


def _model_call(span_id: str) -> dict[str, Any]:
    return _event(
        "model_call",
        span_id=span_id,
        duration_ms=120.0,
        operation="chat",
        provider={"name": "p", "kind": "openai"},
        route="direct",
        model="gpt-4o",
        status="ok",
        usage={"input_tokens": 100, "output_tokens": 10},
    )


def _revision(agent) -> int:
    return agent.doc["deployed_revision"]


def test_the_token_decides_the_instance_and_resends_count_once(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    events = [_event("turn_start"), _model_call("b7ad6b7169203331"), _event("tool_exec")]
    body = envelope(_revision(agent), events)
    r = client.post(EVENTS, json=body, headers=agent.auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
    again = client.post(EVENTS, json=body, headers=agent.auth).json()
    assert again == {"accepted": 0, "duplicates": 3, "rejected": []}
    rows = list(mongo["agent_events"].find())
    # 이벤트가 말한 테넌트·인스턴스는 읽지 않는다. 토큰의 Agent 문서가 정한다
    assert {(r["tenant"], r["instance_id"]) for r in rows} == {("default", agent.id)}
    assert {r["source"] for r in rows} == {"push"} and {r["pod_uid"] for r in rows} == {"uid-1"}
    # 미래의 시각은 받은 시각으로 바뀐다
    assert all(r["ts_clamped"] for r in rows)
    (row,) = list(mongo["usage_records"].find())
    assert row["writer"] == "runtime" and row["tenant"] == "default"
    (stats,) = list(mongo["agent_telemetry"].find())
    assert (stats["received"], stats["duplicates"], stats["refused"]) == (6, 3, 0)
    assert stats["boots"]["b1"] == {"dropped_other": 3, "correlation_reuse": 0, "dropped_cost": 0}
    # 기계의 쓰기라 감사에 남지 않는다
    assert mongo["audit_events"].count_documents({"path": "/api" + EVENTS}) == 0
    assert "/api" + EVENTS not in client.get("/openapi.json").json()["paths"]


def test_wrong_tokens_and_revisions_are_one_401(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    body = envelope(_revision(agent), [_event("turn_start")])
    assert detail_code(client.post(EVENTS, json=body, headers=bearer("nope"))) == (
        401,
        "unauthorized",
    )
    outside = envelope(_revision(agent) + 5, [_event("turn_start")])
    assert detail_code(client.post(EVENTS, json=outside, headers=agent.auth)) == (
        401,
        "unauthorized",
    )
    assert client.post(EVENTS, json=body).status_code == 401
    # 토큰은 본문을 읽기 전에 찾는다. 틀린 토큰에 깨진 본문은 422 가 아니라 401 이다
    broken = client.post(EVENTS, content=b"{", headers={**bearer("nope"), "content-type": JSON})
    assert broken.status_code == 401
    # 리비전 밖 요청도 그 Agent 의 버킷에서 센다
    codes = {client.post(EVENTS, json=outside, headers=agent.auth).status_code for _ in range(25)}
    assert codes == {401, 429}


def test_bad_events_are_refused_one_by_one(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    forged = _event("feedback", rating="up")
    huge = _event("turn_end", text="x" * 70_000)
    shaped = {"daegam_event": "turn_end"}
    keys = _event("guardrail", position="input", action="block", **{"$where": "1", "Bad": 1})
    body = envelope(_revision(agent), [forged, huge, shaped, keys])
    r = client.post(EVENTS, json=body, headers=agent.auth).json()
    # 너무 큰 본문은 거절하지 않고 잘라 받는다
    assert r["accepted"] == 2
    assert sorted(x["code"] for x in r["rejected"]) == ["invalid_event", "reserved_event"]
    doc = mongo["agent_events"].find_one({"event": "guardrail"})
    assert "$where" not in doc["data"] and "Bad" not in doc["data"]
    assert doc["rejected_code"] == "guardrail_blocked"
    cut = mongo["agent_events"].find_one({"event": "turn_end"})
    assert len(cut["body"]["text"]) == 8000 and cut["data"]["truncated"] is True
    assert mongo["agent_events"].count_documents({"event": "feedback"}) == 0


def test_oversized_events_lose_bodies_and_long_strings_are_cut(client, fake_clusters, mongo):
    """크기는 UTF-8 바이트로 잰다. 상한을 넘으면 본문을 빼고, 본문 밖의 긴 글은 256자에서 자른다.
    64비트 밖의 수는 그 이벤트의 거절이다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    wide = {f"k{i}": "가" * 250 for i in range(60)}
    heavy = _event("turn_end", text="나" * 7000, tool="t" * 300, nested=wide)
    huge_int = _event("turn_start", n_big=2**70)
    body = envelope(_revision(agent), [heavy, huge_int])
    r = client.post(EVENTS, json=body, headers=agent.auth).json()
    assert r["accepted"] == 1 and [x["code"] for x in r["rejected"]] == ["invalid_event"]
    doc = mongo["agent_events"].find_one({"event": "turn_end"})
    assert "body" not in doc and doc["data"]["truncated"] is True
    assert doc["tool"] == "t" * 256


def test_a_bucket_takes_its_burst_then_refills_at_its_rate() -> None:
    """열쇠마다 burst 만큼 받고, 그 뒤에는 시계가 간 만큼 rate 로 다시 찬다. 아래 경로 시험은 다시
    차는 속도를 0 으로 두므로 그 속도는 여기서 본다."""
    now = [0.0]
    buckets = KeyedBuckets(10.0, 20.0, idle_seconds=60, max_entries=10, clock=lambda: now[0])
    assert all(buckets.take("a") for _ in range(20)) and not buckets.take("a")
    now[0] += 0.1
    assert buckets.take("a") and not buckets.take("a")
    assert buckets.take("b")


def test_envelopes_bodies_and_rates_have_limits(client, fake_clusters, mongo, monkeypatch):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    # 요청 한도는 초당 10개씩 다시 찬다. 느린 러너에서 요청 하나가 0.1초를 넘으면 그사이에 찬
    # 몫으로 429 가 오지 않으므로(1.1.0-rc8 의 검사) 이 시험은 다시 차지 않는 버킷으로 버스트만 본다
    requests = client.app.state.telemetry_ingest._requests
    monkeypatch.setattr(requests, "rate", 0.0)
    requests.clear()
    headers = {**agent.auth, "content-type": "application/json"}
    assert detail_code(client.post(EVENTS, content=b"{", headers=headers)) == (422, "invalid_json")
    deep = "[" * 100_000 + "]" * 100_000
    assert detail_code(client.post(EVENTS, content=deep.encode(), headers=headers)) == (
        422,
        "invalid_json",
    )
    big = json.dumps(envelope(_revision(agent), [], padding="x" * (1 << 20))).encode()
    assert detail_code(client.post(EVENTS, content=big, headers=headers))[0] == 413
    body = envelope(_revision(agent), [])
    codes = [client.post(EVENTS, json=body, headers=agent.auth).status_code for _ in range(25)]
    assert codes[:20] == [200] * 20 and 429 in codes
    limited = client.post(EVENTS, json=body, headers=agent.auth)
    assert detail_code(limited) == (429, "telemetry_rate_limited")
    assert limited.headers["Retry-After"] == "1"


def test_the_route_is_404_when_ingest_is_off(client, fake_clusters, mongo, monkeypatch):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    off = dataclasses.replace(client.app.state.settings, telemetry_ingest_enabled=False)
    monkeypatch.setattr(client.app.state, "settings", off)
    body = envelope(_revision(agent), [_event("turn_start")])
    assert client.post(EVENTS, json=body, headers=agent.auth).status_code == 404


def test_the_pod_cap_counts_only_recent_pods(client, fake_clusters, mongo):
    """하루 넘게 조용한 파드와 overflow 문서는 50개 상한에 들지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    now = datetime.now(UTC)

    def pods(prefix: str, last_at: datetime) -> list[dict[str, Any]]:
        return [
            {"_id": f"{agent.id}:{prefix}{i}", "instance_id": agent.id, "pod_uid": f"{prefix}{i}"}
            | {"last_at": last_at}
            for i in range(MAX_PODS)
        ]

    coll = mongo["agent_telemetry"]
    coll.insert_many(pods("old", now - timedelta(days=2)))
    coll.insert_one({"_id": f"{agent.id}:overflow", "instance_id": agent.id, "pod_uid": "overflow"})
    client.post(EVENTS, json=envelope(_revision(agent), []), headers=agent.auth)
    assert coll.find_one({"_id": f"{agent.id}:uid-1"}) is not None
    coll.insert_many(pods("new", now))
    other = envelope(_revision(agent), [], pod={"name": "agent-faq-y", "uid": "uid-2"})
    client.post(EVENTS, json=other, headers=agent.auth)
    assert coll.find_one({"_id": f"{agent.id}:uid-2"}) is None
    assert coll.find_one({"_id": f"{agent.id}:overflow"})["pod_name"] == "overflow"


def test_ids_must_be_lowercase_w3c_hex(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    events = [
        _event("turn_start", trace_id=TRACE.upper()),
        _event("turn_start", span_id="xyz"),
        _event("turn_start", parent_span_id="0" * 15),
    ]
    r = client.post(EVENTS, json=envelope(_revision(agent), events), headers=agent.auth).json()
    assert [x["code"] for x in r["rejected"]] == ["invalid_event"] * 3
