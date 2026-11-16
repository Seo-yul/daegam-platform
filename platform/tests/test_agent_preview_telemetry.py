"""미리 보기의 이벤트와 원장 행은 원본 Agent 의 것으로 들고 미리 보기 표시를 단다 (P44b 9절, D107).

수집 경로는 미리 보기의 인스턴스 토큰을 받아 이벤트를 원본의 id 로 넣고 `preview_id` 를 단다. 원장
행의 부른 쪽도 원본이고 `source.preview` 가 미리 보기다. 그래서 원본의 이벤트 목록, 트레이스, Agent
마다의 사용량이 미리 보기의 턴을 품고 원본의 보관 기한을 따른다. 수집 기록(파드가 밀어 보내는가)은
미리 보기 자신의 id 라 수집기가 그 파드의 로그를 다시 읽지 않는다. 밀어 보내지 않는 파드의 로그도
수집기가 같은 표시로 넣는다.
"""

from __future__ import annotations

from tests.helpers.agent_runtime import deployed_agent, token_of
from tests.helpers.auth import bearer
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.previews import deployed_preview, plain_spec
from tests.helpers.telemetry import EVENTS, TRACE, envelope, model_call, run_event
from tests.helpers.workers import audit_collector


def test_pushed_events_of_a_preview_are_recorded_under_the_source(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    events = [run_event("turn_start", 1.0), model_call(2.0, "a1b2c3d4e5f60718")]
    body = envelope(preview.deployed_revision, events)
    r = client.post(EVENTS, json=body, headers=bearer(token_of(agent.fake, preview)))
    assert r.status_code == 200 and r.json()["accepted"] == 2, r.text

    rows = list(mongo["agent_events"].find())
    assert {(r["instance_id"], r["preview_id"]) for r in rows} == {(agent.id, preview.id)}
    assert {r["instance"]["id"] for r in rows} == {agent.id}
    assert {r["instance"]["name"] for r in rows} == {"support"}
    (row,) = list(mongo["usage_records"].find())
    assert row["source"] == {
        "kind": "agent",
        "id": agent.id,
        "name": "support",
        "preview": preview.id,
    }
    # 파드가 밀어 보낸다는 기록은 미리 보기 자신의 것이다
    (stats,) = list(mongo["agent_telemetry"].find())
    assert stats["instance_id"] == preview.id

    listed = client.get(f"{agent.url}/events")
    assert listed.status_code == 200, listed.text
    assert {e["preview_id"] for e in listed.json()} == {preview.id}
    trace = client.get(f"{agent.url}/traces/{TRACE}")
    assert trace.status_code == 200, trace.text
    assert trace.json()["preview_id"] == preview.id
    # 원본의 이벤트에는 표시가 없다
    plain = [run_event("turn_start", 3.0, trace_id="0af7651916cd43dd8448eb211c80319c")]
    body = envelope(agent.doc["deployed_revision"], plain)
    assert client.post(EVENTS, json=body, headers=agent.auth).status_code == 200
    own = mongo["agent_events"].find_one({"trace_id": "0af7651916cd43dd8448eb211c80319c"})
    assert own["instance_id"] == agent.id and "preview_id" not in own


AGENT_TURN_END = (
    '{"daegam_event": "turn_end", "id": "e-1", "ts": "2026-09-06T01:02:03.456+00:00", '
    '"instance": {"id": "x", "name": "x"}, "session_id": "s1", "user_sub": "u1", '
    '"turn": 2, "status": "done", "duration_ms": 12.5}'
)


def test_logged_events_of_a_preview_pod_are_recorded_under_the_source(client, fake_clusters, mongo):
    """밀어 보내지 않는 미리 보기 파드의 줄도 원본의 이벤트로 들고 같은 표시를 단다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    preview = deployed_preview(client, agent)
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": preview.id,
        "daegam.devops.ai.kr/tenant": "default",
        "app.kubernetes.io/managed-by": "daegam",
    }
    agent.fake.add_pod(DEFAULT_NS, "agent-preview-abc", labels, uid="uid-p")
    agent.fake.add_pod_log(DEFAULT_NS, "agent-preview-abc", AGENT_TURN_END)
    col = audit_collector(client, agents=client.app.state.agents)
    assert client.portal.call(col.run_once)["events"] == 1
    (row,) = list(mongo["agent_events"].find())
    assert (row["instance_id"], row["preview_id"]) == (agent.id, preview.id)
    assert row["instance"] == {"id": agent.id, "name": "support"}
