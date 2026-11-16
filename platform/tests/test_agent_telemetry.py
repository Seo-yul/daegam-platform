"""Agent 지표·턴·트레이스·이벤트 (P37 11절·12절): 저장소가 모든 레플리카의 수를 세고, 런타임 몫은
지금 값이며, 턴과 트레이스는 소유자나 approver 만 본다."""

from __future__ import annotations

from datetime import timedelta
from typing import get_args

from daegam.api.schemas.agent_telemetry import MetricsWindow, TurnResultsOut
from daegam.auth import rbac
from daegam.services.telemetry import traces
from daegam.services.telemetry.agent_metrics import TURN_RESULTS, WINDOWS
from daegam.services.telemetry.traces import visible_event
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.auth import as_principal
from tests.helpers.telemetry import RUN, TRACE, TRACE2
from tests.helpers.telemetry import model_call as _model
from tests.helpers.telemetry import push_events as _push
from tests.helpers.telemetry import run_event as _event
from tests.helpers.telemetry import tool_exec as _tool
from tests.helpers.telemetry import turn_end as _end


def _a_run(client, agent) -> None:
    """턴 t1 의 실행 둘(승인 대기, 재개 뒤 끝)과 다른 세션의 여러 결과다."""
    _push(
        client,
        agent,
        [
            _event("turn_start", 0, span_id=RUN),
            _model(1, "a000000000000001", "error", error_type="APIError"),
            _event("model_retry", 1.1, attempt=1, model="gpt-4o"),
            _model(2, "a000000000000002"),
            _event("model_fallback", 2.1, **{"from": "gpt-4o", "to": "gpt-4o-mini"}),
            _tool(3, "c000000000000001"),
            _tool(3.5, "c000000000000002", "error"),
            _end(4, "pending", 300.0),
            _event("turn_start", 10, trace_id=TRACE2, span_id=RUN, resumed=True),
            _tool(11, "c000000000000003", trace_id=TRACE2),
            _end(12, "done", 100.0, trace_id=TRACE2),
            _end(20, "cancelled", 5000.0, session_id="s2", turn_id="t9", trace_id=None),
            _end(21, "model_error", 50.0, session_id="s2", turn_id="t8", trace_id=None),
            _event("rejected", 22, code="max_turns", session_id="s2", trace_id=None),
            _event("rate_limited", 23, session_id=None, trace_id=None, limit=2, count=3),
        ],
    )


def test_metrics_count_every_replica_from_the_store(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    _a_run(client, agent)
    r = client.get(f"{agent.url}/metrics")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["window"] == "1h" and m["runtime_error"] is None
    assert m["sessions"] == {"active": 2, "pending": 1, "closed": 5}
    assert m["turns"]["pending"] == 1 and m["turns"]["done"] == 1
    # 모르는 오류 코드는 turn_failed 다. 멈춘 턴은 오류율의 분모와 지연에 들지 않는다
    assert m["turns"]["turn_failed"] == 1 and m["turns"]["cancelled"] == 1
    assert m["errors"] == {"turns": 1, "error_rate": 0.3333}
    assert m["latency_ms"]["max"] == 300.0
    assert m["rejected"] == {"total": 2, "by_code": {"max_turns": 1, "rate_limited": 1}}
    model = m["model"]
    assert (model["calls"], model["errors"], model["retries"], model["fallbacks"]) == (2, 1, 1, 1)
    assert model["by_model"][0]["model"] == "gpt-4o" and model["p95_ms"] == 200.0
    tools = m["tool_calls"]
    assert (tools["calls"], tools["errors"]) == (3, 1) and tools["by_tool"][0]["tool"] == "Add"
    assert m["usage"]["input_tokens"] == 1000 and m["cost"]["usd"] > 0
    telemetry = m["telemetry"]
    assert telemetry["mode"] == "push" and [p["uid"] for p in telemetry["pods"]] == ["uid-1"]
    assert telemetry["dropped"] == {"cost": 0, "other": 3}
    assert client.get(f"{agent.url}/metrics", params={"tz": "Mars/Base"}).status_code == 422


def test_a_window_with_pods_that_pushed_is_not_reported_as_no_events(client, fake_clusters, mongo):
    """창 안에 밀어 보낸 파드가 표에 있으면, 최근 5분에 보낸 것이 없어도 수집 방식은 push 다."""
    agent = deployed_agent(client, fake_clusters, mongo)
    _push(client, agent, [_event("turn_start", 0, span_id=RUN)])
    mongo["agent_telemetry"].update_many(
        {}, {"$set": {"last_at": utcnow() - timedelta(minutes=30)}}
    )
    telemetry = client.get(f"{agent.url}/metrics").json()["telemetry"]
    assert telemetry["mode"] == "push" and len(telemetry["pods"]) == 1


def test_metric_schemas_name_what_the_service_counts() -> None:
    assert set(TurnResultsOut.model_fields) == set(TURN_RESULTS)
    assert set(get_args(MetricsWindow)) == set(WINDOWS)


def test_metrics_are_remembered_for_a_moment(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    before = client.get(f"{agent.url}/metrics").json()
    _a_run(client, agent)
    assert client.get(f"{agent.url}/metrics").json() == before
    assert client.get(f"{agent.url}/metrics", params={"window": "24h"}).json()["turns"]["done"] == 1


def test_turns_and_traces_belong_to_the_owner(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    _a_run(client, agent)
    url = agent.url
    turns = client.get(f"{url}/sessions/s1/turns").json()["turns"]
    (turn,) = turns
    assert turn["turn_id"] == "t1" and [r["status"] for r in turn["runs"]] == ["pending", "done"]
    assert [r["resumed"] for r in turn["runs"]] == [False, True]
    assert turn["usage"]["input_tokens"] == 200 and turn["cost_usd"] > 0

    trace = client.get(f"{url}/traces/{TRACE}").json()
    assert trace["root"]["kind"] == "run" and trace["root"]["status"] == "pending"
    kinds = [(s["kind"], s["status"]) for s in trace["spans"]]
    assert kinds.count(("model", "ok")) == 1 and kinds.count(("tool", "error")) == 1
    model = next(s for s in trace["spans"] if s["kind"] == "model" and s["status"] == "ok")
    assert model["cost_usd"] > 0 and model["ledger"]["writer"] == "runtime"
    assert [p["event"] for p in trace["points"]] == ["model_retry", "model_fallback"]
    assert trace["totals"]["cost_usd"] == model["cost_usd"] and not trace["truncated"]
    assert client.get(f"{url}/traces/{'f' * 32}").status_code == 404

    with as_principal(client, "alice", rbac.VIEWER):
        assert client.get(f"{url}/sessions/s1/turns").status_code == 200
        assert client.get(f"{url}/traces/{TRACE2}").status_code == 200
    with as_principal(client, "bob", rbac.VIEWER):
        r = client.get(f"{url}/sessions/s1/turns")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_session_owner"
        assert client.get(f"{url}/traces/{TRACE}").status_code == 403
        # 소유자를 아직 정할 수 없는 세션과 실행은 404 다. 트레이스 창이 다시 읽는다
        assert client.get(f"{url}/sessions/none/turns").status_code == 404
        assert client.get(f"{url}/traces/{'f' * 32}").status_code == 404


def test_a_trace_keeps_its_run_span_and_names_the_cap(client, fake_clusters, mongo, monkeypatch):
    """run span 은 이벤트 상한에 잘리지 않고 넘은 상한을 알린다. 호출이 모두 가격 규칙이 없으면
    턴과 트레이스의 금액은 0 이 아니라 null 이다."""
    monkeypatch.setattr(traces, "MAX_TRACE_EVENTS", 3)
    agent = deployed_agent(client, fake_clusters, mongo)
    calls = [{**_model(1 + i / 10, f"a00000000000000{i}"), "model": "house-x"} for i in range(5)]
    _push(
        client, agent, [_event("turn_start", 0, span_id=RUN), *calls, _end(4, "turn_failed", 9.0)]
    )
    trace = client.get(f"{agent.url}/traces/{TRACE}").json()
    assert trace["root"]["kind"] == "run" and trace["root"]["error"] is True
    assert (trace["truncated_by"], trace["limit"], len(trace["spans"])) == ("events", 3, 4)
    assert not any(s["error"] for s in trace["spans"][1:])
    assert trace["totals"]["cost_usd"] is None and trace["totals"]["unpriced_calls"] == 5
    (turn,) = client.get(f"{agent.url}/sessions/s1/turns").json()["turns"]
    assert turn["cost_usd"] is None


def test_sessions_carry_their_cost_and_events_filter_by_trace(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo)
    _a_run(client, agent)
    (row,) = client.get(f"{agent.url}/sessions").json()["sessions"]
    assert row["session_id"] == "s1" and row["cost_usd"] > 0
    got = client.get(f"{agent.url}/events", params={"trace_id": TRACE2}).json()
    assert {e["event"] for e in got} == {"turn_start", "tool_exec", "turn_end"}
    assert client.get(f"{agent.url}/events", params={"event": "turn_end"}).json()[0]["span_id"]


def test_bodies_are_only_for_approvers() -> None:
    row = {"event": "turn_end", "body": {"text": "비밀"}, "data": {"status": "done"}}
    assert visible_event(row, bodies=True) == row
    assert visible_event(row, bodies=False) == {"event": "turn_end", "data": {"status": "done"}}


def test_a_repaired_run_without_its_turn_start_is_still_a_run() -> None:
    """런타임이 되살린 실행(P79)은 turn_start 없이 turn_end 하나만 올 수 있다. 시도 기록 뒤 턴
    태스크 전에 리스를 잃은 요청이 그렇다. 그 turn_end 도 실행 하나이고 상태가 그대로 보인다."""
    now = utcnow()
    end = {
        "event": "turn_end",
        "ts": now,
        "start": now,
        "turn": 2,
        "turn_id": "t2",
        "trace_id": TRACE2,
        "status": "turn_aborted",
        "data": {"repaired": True, "code": "turn_aborted"},
    }
    turn = traces._turn("t2", [end], None)
    assert [(r["trace_id"], r["status"], r["duration_ms"]) for r in turn["runs"]] == [
        (TRACE2, "turn_aborted", None)
    ]
    assert turn["turn"] == 2
