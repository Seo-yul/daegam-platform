"""P36 F5: Agent 답변의 피드백(`api/agent_feedback.py`, `services/agent_feedback.py`).

가짜 런타임의 세션 상세에 표본 전사(`agent-runtime/schema/fixtures/chat/turn.json`)를 실어
본다(`test_agent_chat_api.with_transcript`). 만들기·고치기·거두기마다 사람과 의견이 없는 `feedback`
이벤트 하나가 남는 것, 소유자 아닌 사람과 없는 턴, viewer 의 목록·내보내기·이벤트 목록 403,
테넌트 격리, 보존 기한, 같은 시각 행의 쪽 넘김, 지우기 세 규칙과 수집기 훅(밖의 글이 섞인 줄
포함), 내보내기 줄 모양과 감사를 든다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from pymongo.errors import AutoReconnect, OperationFailure

from daegam.services import agent_feedback
from daegam.services.audit_collector import AuditCollector
from daegam.store.agent_feedback import AgentFeedback
from daegam.store.base import parse_iso_utc, utcnow
from tests.helpers.agent_runtime import DeployedAgent, deployed_agent
from tests.helpers.agents import agent_env, agent_fixture, create_agent, template_for
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, T2
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.runtime import with_transcript
from tests.helpers.workers import audit_collector

TURN_FIXTURE: dict[str, Any] = agent_fixture("chat/turn.json")
DONE = TURN_FIXTURE["events"]["done"]
TURN_ID = DONE["turn_id"]
ERROR_TURN_ID = TURN_FIXTURE["events"]["error"]["turn_id"]
DOWN = {"rating": "down", "tags": ["inaccurate", "incomplete"], "comment": "  기한이 틀렸다  "}
EXPORT_KEYS = {
    "schema",
    "id",
    "tenant",
    "agent",
    "revision",
    "session_id",
    "turn_id",
    "turn",
    "rating",
    "tags",
    "comment",
    "ephemeral",
    "superseded",
    "created_at",
    "updated_at",
}


def _agent(client, fake_clusters, mongo, *, features: str | None = None) -> DeployedAgent:
    """배포한 Agent 다. 그 가짜 런타임의 세션 상세가 표본 턴의 전사를 싣는다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=features)
    with_transcript(agent.runtime, TURN_FIXTURE["transcript"])
    return agent


def _turn_url(agent_id: str, sid: str = "s1", turn_id: str = TURN_ID) -> str:
    return f"{AG}/{agent_id}/sessions/{sid}/turns/{turn_id}/feedback"


def _events(mongo) -> list[dict[str, Any]]:
    return list(mongo["agent_events"].find({"event": "feedback"}).sort("ts", 1))


def test_owner_rates_revises_and_withdraws_with_one_event_each(client, fake_clusters, mongo):
    """만들기·고치기·거두기마다 이벤트가 하나씩이고 바뀔 때마다 새 id 다. 이벤트에는 사람과 의견이
    없고 테넌트·인스턴스는 Agent 문서의 것이다. 거두면 rating 이 null 이다. 고쳐도 문서 id 와 만든
    시각은 그대로이고 보존 기한은 다시 잰다."""
    agent = _agent(client, fake_clusters, mongo)
    url = _turn_url(agent.id)

    r = client.put(url, json=DOWN)
    assert r.status_code == 200, r.text
    made = r.json()
    assert (made["turn"], made["rating"], made["user_sub"]) == (DONE["turn"], "down", "header")
    assert made["tags"] == ["inaccurate", "incomplete"] and made["comment"] == "기한이 틀렸다"
    assert made["superseded"] is False and made["revision"] == 1
    created, expires = parse_iso_utc(made["created_at"]), parse_iso_utc(made["expires_at"])
    assert expires - created == timedelta(days=client.app.state.settings.feedback_retention_days)
    # 런타임은 호출자 sub 로, 승인자 헤더 없이 부른다
    call = [c for c in agent.runtime.state.calls if c["path"] == "/sessions/s1"][-1]
    assert "x-daegam-approver" not in call["headers"]

    r = client.put(url, json={"rating": "up", "ephemeral": True})
    assert r.status_code == 200, r.text
    revised = r.json()
    assert (revised["id"], revised["created_at"]) == (made["id"], made["created_at"])
    assert revised["tags"] == [] and revised["comment"] is None and revised["ephemeral"] is True
    assert parse_iso_utc(revised["expires_at"]) >= expires

    mine = client.get(f"{AG}/{agent.id}/sessions/s1/feedback").json()["items"]
    assert [(f["turn_id"], f["rating"]) for f in mine] == [(TURN_ID, "up")]

    assert client.delete(url).status_code == 204
    days = client.app.state.settings.feedback_retention_days
    empty = {"items": [], "retention_days": days}
    assert client.get(f"{AG}/{agent.id}/sessions/s1/feedback").json() == empty
    r = client.delete(url)
    assert detail_code(r) == (404, "feedback_not_found")

    events = _events(mongo)
    assert [e["data"]["rating"] for e in events] == ["down", "up", None]
    assert len({e["_id"] for e in events}) == 3
    for e in events:
        # 수집한 런타임 이벤트와 같은 모양이다. 사람과 의견은 어느 칸에도 없다
        assert (e["tenant"], e["instance_id"], e["session_id"]) == ("default", agent.id, "s1")
        assert (e["data"]["source"], e["data"]["turn_id"]) == ("platform", TURN_ID)
        assert e["user_sub"] is None and "comment" not in e
        assert not {"user_sub", "comment"} & e["data"].keys()
    assert [e["data"]["tags"] for e in events] == [["inaccurate", "incomplete"], [], []]


def test_session_list_carries_the_installed_retention(settings, mongo, fake_clusters, fake_secrets):
    """세션의 내 평가 목록은 설치의 보존 기간을 싣는다. 동의 안내(P45 8절)가 세션에 아직 평가가
    없어도 기본값이 아닌 설치의 값을 적고, 그 날 수가 평가가 실제로 받는 만료와 같다."""
    app = make_app(settings, fake_clusters, fake_secrets, feedback_retention_days=45)
    with api_client(app) as c:
        agent = _agent(c, fake_clusters, mongo)
        mine = f"{AG}/{agent.id}/sessions/s1/feedback"
        assert c.get(mine).json() == {"items": [], "retention_days": 45}

        made = c.put(_turn_url(agent.id), json={"rating": "up"}).json()
        kept = parse_iso_utc(made["expires_at"]) - parse_iso_utc(made["updated_at"])
        assert kept == timedelta(days=45)
        assert c.get(mine).json()["retention_days"] == 45


def test_only_the_owner_rates_a_finished_turn(client, fake_clusters, mongo):
    """남의 세션은 승인자라도 403 이다. 전사에 그 turn_id 로 끝난 턴이 없으면(오류로 끝난 턴, 없는
    턴) 404 turn_not_found 이고 없는 세션은 런타임의 404 그대로다. 어느 쪽도 이벤트를 남기지
    않는다."""
    agent = _agent(client, fake_clusters, mongo)
    cases = (
        (_turn_url(agent.id, sid="other"), 403, "not_session_owner"),
        (_turn_url(agent.id, turn_id=ERROR_TURN_ID), 404, "turn_not_found"),
        (_turn_url(agent.id, turn_id="nope"), 404, "turn_not_found"),
        (_turn_url(agent.id, sid="missing"), 404, "session_not_found"),
        (_turn_url("nope"), 404, "not_found"),
    )
    for url, status, code in cases:
        r = client.put(url, json={"rating": "up"})
        assert r.status_code == status, r.text
        assert r.json()["detail"]["code"] == code
    assert _events(mongo) == []
    assert mongo["agent_feedback"].count_documents({}) == 0


def test_feedback_is_scoped_to_the_tenant_and_agent(client, fake_clusters, mongo):
    """다른 테넌트에서는 Agent 부터 404 이고, 저장소 질의도 테넌트와 Agent 로 거른다."""
    agent = _agent(client, fake_clusters, mongo)
    assert client.put(_turn_url(agent.id), json={"rating": "up"}).status_code == 200
    for path in (f"{agent.id}/feedback", f"{agent.id}/sessions/s1/feedback"):
        assert client.get(f"{AG}/{path}", headers=T2).status_code == 404
    assert client.put(_turn_url(agent.id), json={"rating": "up"}, headers=T2).status_code == 404

    store = client.app.state.agent_feedback_service.store
    assert client.portal.call(lambda: store.page("other", agent.id, limit=10)) == []
    assert client.portal.call(lambda: store.page("default", "another", limit=10)) == []
    assert len(client.portal.call(lambda: store.page("default", agent.id, limit=10))) == 1


def _seed(client, agent_id: str, rows: list[dict[str, Any]]) -> None:
    """저장소에 바로 평가를 넣는다. 목록과 내보내기는 런타임을 부르지 않는다."""
    store = client.app.state.agent_feedback_service.store
    now = utcnow()
    for i, row in enumerate(rows):
        at = now - timedelta(days=row.pop("age_days", 0), minutes=i)
        fb = AgentFeedback(
            tenant="default",
            agent_id=agent_id,
            session_id=row.pop("session_id", "s1"),
            turn_id=f"t{i}",
            turn=i + 1,
            user_sub=row.pop("user_sub", "u1"),
            rating=row.pop("rating", "up"),
            created_at=at,
            updated_at=at,
            expires_at=at + timedelta(days=180),
            **row,
        )
        client.portal.call(store.upsert, fb)


def test_approver_list_filters_pages_and_counts_the_last_week(client, fake_clusters, mongo):
    """승인자 목록은 새것부터이고 평가·이유로 거르며 `next_before` 로 다음 쪽을 넘긴다. 지난 7일
    수는 거르개와 상관없다. 다시 생성으로 바뀐 턴의 평가도 표시를 달고 남는다."""
    agent = _agent(client, fake_clusters, mongo)
    _seed(
        client,
        agent.id,
        [
            {"rating": "down", "tags": ["unsafe"], "superseded": True},
            {"rating": "up", "user_sub": "u2"},
            {"rating": "down", "tags": ["inaccurate"], "comment": "틀렸다"},
            {"rating": "up", "age_days": 10},
        ],
    )
    url = f"{AG}/{agent.id}/feedback"
    body = client.get(url).json()
    assert [f["turn_id"] for f in body["items"]] == ["t0", "t1", "t2", "t3"]
    assert body["last_7_days"] == {"up": 1, "down": 2} and body["next_before"] is None
    assert body["items"][0]["superseded"] is True and body["items"][1]["user_sub"] == "u2"

    downs = client.get(url, params={"rating": "down"}).json()["items"]
    assert [f["turn_id"] for f in downs] == ["t0", "t2"]
    unsafe = client.get(url, params={"tag": "unsafe"}).json()["items"]
    assert [f["turn_id"] for f in unsafe] == ["t0"]

    first = client.get(url, params={"limit": 2}).json()
    assert [f["turn_id"] for f in first["items"]] == ["t0", "t1"]
    rest = client.get(url, params={"limit": 2, "before": first["next_before"]}).json()
    assert [f["turn_id"] for f in rest["items"]] == ["t2", "t3"]

    for bad in ({"limit": 201}, {"rating": "meh"}, {"tag": "rude"}, {"before": "nope"}):
        assert client.get(url, params=bad).status_code == 422, bad


def test_export_lines_carry_no_person_and_the_read_is_audited(client, fake_clusters, mongo):
    """내보내기는 평가 한 건이 한 줄인 NDJSON 이고 사람 식별자가 없다. 헤더는 내려받기 다섯이고
    남의 평가를 밖으로 내는 읽기라 감사에 남는다."""
    agent = _agent(client, fake_clusters, mongo)
    _seed(client, agent.id, [{"rating": "down", "tags": ["unsafe"]}, {"rating": "up"}])
    mongo["audit_events"].delete_many({})

    r = client.get(f"{AG}/{agent.id}/feedback/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/x-ndjson"
    assert r.headers["content-disposition"].startswith('attachment; filename="feedback-support')
    assert r.headers["x-content-type-options"] == "nosniff"
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [line["turn_id"] for line in lines] == ["t1", "t0"]
    for line in lines:
        assert set(line) == EXPORT_KEYS
        assert line["schema"] == "daegam.feedback/1"
        assert line["agent"] == {"id": agent.id, "name": "support"}
    assert "u1" not in r.text

    downs = client.get(f"{AG}/{agent.id}/feedback/export", params={"rating": "down"}).text
    assert [json.loads(line)["turn_id"] for line in downs.splitlines()] == ["t0"]

    rows = list(mongo["audit_events"].find({"method": "GET"}))
    assert [r["action"] for r in rows] == ["export_agent_feedback"] * 2
    assert rows[0]["resource_id"] == agent.id


def test_export_range_and_line_caps(client, fake_clusters, mongo, monkeypatch):
    """구간은 90일까지이고 거꾸로면 잘못된 값이다. 줄이 상한을 넘으면 받지 않고 구간을 줄이라고
    한다."""
    agent = _agent(client, fake_clusters, mongo)
    url = f"{AG}/{agent.id}/feedback/export"
    for since, until, code in (
        ("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "export_range_too_long"),
        ("2026-06-01T00:00:00Z", "2026-01-01T00:00:00Z", "invalid_value"),
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "invalid_value"),
    ):
        r = client.get(url, params={"since": since, "until": until})
        assert detail_code(r) == (422, code), (since, until)
    r = client.get(url, params={"since": "2026-01-01T00:00:00Z", "until": "2026-01-31T00:00:00Z"})
    assert r.status_code == 200 and r.text == ""
    assert 'feedback-support-20260101-20260131.ndjson"' in r.headers["content-disposition"]

    monkeypatch.setattr(agent_feedback, "EXPORT_MAX_LINES", 1)
    _seed(client, agent.id, [{}, {}])
    r = client.get(url)
    assert detail_code(r) == (422, "export_too_large")


def test_viewer_cannot_list_or_export(oidc, token):
    """목록과 내보내기는 agent:approve 다. viewer 는 agent:chat 만 있어 자기 평가만 다룬다. 같은
    목록의 turn_start 가 user_sub 를 실어 평가한 사람이 드러나므로 이벤트 목록도 audit:read 가 없는
    viewer 에게는 403 이다(P37 12절)."""
    viewer = bearer(token("v1", "daegam-a-view"), tenant="a")
    for path in (f"{AG}/nope/feedback", f"{AG}/nope/feedback/export"):
        r = oidc.get(path, headers=viewer)
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "agent:approve", path
    r = oidc.get(f"{AG}/nope/events", headers=viewer, params={"event": "feedback"})
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "audit:read"
    # 자기 평가는 scope 를 지나 없는 Agent 의 404 로 걸린다
    turn = f"{AG}/nope/sessions/s1/turns/t1/feedback"
    assert oidc.put(turn, headers=viewer, json={"rating": "up"}).status_code == 404
    assert oidc.delete(turn, headers=viewer).status_code == 404
    assert oidc.get(f"{AG}/nope/sessions/s1/feedback", headers=viewer).status_code == 404


def test_a_failed_feedback_delete_leaves_the_agent_for_reconcile(
    client, fake_clusters, mongo, monkeypatch
):
    """배포된 Agent 의 피드백 지우기가 실패하면 문서가 deleting 으로 남아 리컨실이 다시 지운다.
    문서를 먼저 지우면 평가를 가리킬 길이 사라진다."""
    agent = _agent(client, fake_clusters, mongo)
    _seed(client, agent.id, [{}])
    store = client.app.state.agent_feedback_service.store
    delete_agent = store.delete_agent

    async def fail(*_args: Any) -> int:
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(store, "delete_agent", fail)
    assert client.delete(f"{AG}/{agent.id}").status_code == 502
    left = mongo["agents"].find_one({"_id": agent.id})
    assert left is not None and left["state"] == "deleting"
    monkeypatch.setattr(store, "delete_agent", delete_agent)
    assert client.delete(f"{AG}/{agent.id}").status_code == 200
    assert mongo["agent_feedback"].count_documents({}) == 0


def test_a_failed_feedback_delete_still_finishes_a_draft_delete(
    client, fake_clusters, mongo, monkeypatch
):
    """draft 는 문서를 먼저 지우므로 피드백 지우기가 실패해도 삭제는 끝난다. 실패를 올리면 화면은
    500 을 받고 다시 해도 404 다. 남은 평가는 보존 기한이 지나면 TTL 이 걷는다."""
    _cid, ds, gw = agent_env(client, mongo)
    made = create_agent(client, template_for(gw, ds)).json()
    _seed(client, made["id"], [{}])
    store = client.app.state.agent_feedback_service.store

    async def fail(*_args: Any) -> int:
        raise AutoReconnect("mongo is down")

    monkeypatch.setattr(store, "delete_agent", fail)
    assert client.delete(f"{AG}/{made['id']}").status_code == 200
    assert mongo["agents"].find_one({"_id": made["id"]}) is None
    assert mongo["agent_feedback"].count_documents({"agent_id": made["id"]}) == 1


AGENT_POD = "agent-support-7f9-abc"


def _agent_pod(fake_clusters, agent_id: str) -> Callable[..., None]:
    """이 Agent 의 파드 하나를 두고 그 파드 로그에 줄을 더하는 함수 `log(line, ts=지금)` 을 준다."""
    fake = fake_clusters.by_name["edge"]
    labels = {
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": agent_id,
        "daegam.devops.ai.kr/tenant": "default",
    }
    fake.add_pod(DEFAULT_NS, AGENT_POD, labels, uid="uid-1")

    def log(line: str, ts: datetime | None = None) -> None:
        fake.add_pod_log(DEFAULT_NS, AGENT_POD, line, ts=ts or utcnow())

    return log


def _hooked_collector(client) -> AuditCollector:
    """앱이 띄우는 것처럼 평가 훅을 단 수집기다(TelemetryWriter 가 훅을 부른다)."""
    return audit_collector(client, agents=client.app.state.agents)


def _runtime_line(event: str, sid: Any, **extra: Any) -> str:
    return json.dumps(
        {"daegam_event": event, "ts": utcnow().isoformat() + "Z", "session_id": sid, **extra}
    )


def test_collector_hook_applies_external_deletes_and_regenerations(client, fake_clusters, mongo):
    """외부 경로의 일은 수집기 훅이 안다. `session_deleted` 는 그 세션의 평가를 지우고
    `turn_end.replaced_turn_id` 는 그 턴의 평가에 superseded 를 단다. 같은 줄을 다시 읽어도 결과가
    같다."""
    agent = _agent(client, fake_clusters, mongo)
    _seed(client, agent.id, [{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s2"}])
    log = _agent_pod(fake_clusters, agent.id)
    now = utcnow()
    for line in (
        _runtime_line("session_deleted", "s1", user_sub="u1"),
        _runtime_line("turn_end", "s2", turn=2, replaced_turn_id="t1"),
        _runtime_line("turn_end", "s2", turn=3),
    ):
        log(line, now)
    col = _hooked_collector(client)

    def state() -> list[tuple[str, str, bool]]:
        rows = mongo["agent_feedback"].find().sort("turn_id", 1)
        return [(d["session_id"], d["turn_id"], d["superseded"]) for d in rows]

    assert client.portal.call(col.run_once)["events"] == 3
    assert state() == [("s2", "t1", True), ("s2", "t2", False)]
    assert client.portal.call(col.run_once)["events"] == 0
    assert state() == [("s2", "t1", True), ("s2", "t2", False)]


def test_collector_hook_skips_forged_lines_and_keeps_collecting(
    client, fake_clusters, mongo, monkeypatch
):
    """파드 로그에는 밖의 글이 섞일 수 있다. 글자가 아닌 `session_id`·`replaced_turn_id` 는 질의에
    넣지 않으므로 평가가 지워지거나 표시되지 않는다. 저장소가 거절하는 줄 하나가 수집을 멈추지도
    않는다."""
    agent = _agent(client, fake_clusters, mongo)
    _seed(client, agent.id, [{"session_id": "s1"}, {"session_id": "s2"}])
    log = _agent_pod(fake_clusters, agent.id)
    log(_runtime_line("session_deleted", {"$ne": None}))
    log(_runtime_line("session_deleted", ["s1"]))
    log(_runtime_line("turn_end", "s1", turn=2, replaced_turn_id={"$ne": None}))
    col = _hooked_collector(client)
    assert client.portal.call(col.run_once)["events"] == 3
    assert mongo["agent_feedback"].count_documents({"superseded": False}) == 2

    async def refuse(*_args: Any) -> int:
        raise OperationFailure("unknown operator")

    monkeypatch.setattr(client.app.state.agent_feedback_service.store, "delete_session", refuse)
    log(_runtime_line("session_deleted", "s1"))
    assert client.portal.call(col.run_once)["events"] == 1
    log(_runtime_line("session_deleted", "s2"))
    assert client.portal.call(col.run_once)["events"] == 1
    assert mongo["agent_feedback"].count_documents({}) == 2


@pytest.mark.parametrize(
    ("session", "turn"),
    [
        ({"transcript": {"items": TURN_FIXTURE["transcript"]}}, DONE["turn"]),
        ({"transcript": {"items": [{"kind": "done", "turn_id": TURN_ID}]}}, None),
        ({"transcript": None}, None),
        ({}, None),
        (None, None),
    ],
)
def test_done_turn_reads_only_finished_turns(session: Any, turn: int | None) -> None:
    """턴 번호는 전사의 `done` 에서만 읽는다. 옛 전사와 모양이 다른 본문은 없는 턴이다."""
    assert agent_feedback.done_turn(session, TURN_ID) == turn
