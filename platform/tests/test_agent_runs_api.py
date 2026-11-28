"""실행·스케줄 라우트 (P41 2·3·10절). 테스트 Mongo 가 있어야 한다. header 모드라 요청한 사람은
`header` 다. 전제 조건과 권한 판단의 갈래는 tests/test_runs_rules.py 가 DB 없이 본다."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Any

import pytest

from daegam.services.runs.service import MAX_RUNNING
from daegam.store.agent_runs import ActsAs, AgentRun, RunCaller, new_run
from daegam.store.base import utcnow
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import agent_env, create_agent, deploy_spec, template_for
from tests.helpers.consts import AG, TENANT_HEADER
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.runs import schedule_of, waiting_run_of
from tests.helpers.telemetry import RUN, model_call, push_events, run_event

SCHEDULE = {"cron": "0 9 * * 1-5", "time_zone": "Asia/Seoul", "message": "daily report"}


@pytest.fixture
def runs_client(settings, mongo, fake_clusters, fake_secrets):
    # 워커는 끈다. 라우트와 이벤트가 만든 기록을 시험이 보기 전에 워커가 옮기지 않게 한다
    app = make_app(
        settings, fake_clusters, fake_secrets, runs_enabled=True, runs_workers_enabled=False
    )
    with api_client(app) as c:
        yield c


def _spec(client, mongo, *, budget: int | None = 20_000) -> dict[str, Any]:
    _cid, ds, gw = agent_env(client, mongo)
    spec = template_for(gw, ds)
    spec["service"].setdefault("budget", {})["session_tokens"] = budget
    return spec


def _deployed(client, mongo, *, guarded: bool = False, **kw) -> str:
    """배포한 Agent 의 id. guarded 면 모든 Tool 에 승인 규칙을 달아 트리거 규칙을 지난다."""
    spec = _spec(client, mongo, **kw)
    if guarded:
        for node in spec["graph"]["nodes"]:
            if node["type"] == "tools":
                node["data"]["approval"] = {
                    t: ["approve", "reject"] for t in node["data"]["allow_tools"]
                }
    _p, inst = deploy_spec(client, spec)
    return inst["id"]


def _schedule(client, agent_id: str, **change) -> dict[str, Any]:
    r = client.post(f"{AG}/{agent_id}/schedules", json={**SCHEDULE, **change})
    assert r.status_code == 201, r.text
    return r.json()


def test_the_routes_are_hidden_and_no_worker_runs_when_runs_are_off(
    settings, mongo, fake_clusters, fake_secrets
):
    app = make_app(settings, fake_clusters, fake_secrets, runs_enabled=False)
    with api_client(app) as client:
        assert client.app.state.run_workers == ()
        agent_id = _deployed(client, mongo)
        for method, path in (
            ("GET", f"{AG}/{agent_id}/runs"),
            ("GET", f"{AG}/{agent_id}/schedules"),
            ("POST", f"{AG}/{agent_id}/runs"),
            ("GET", "/me/runs-as-me"),
        ):
            body = {"message": "hi"} if method == "POST" else None
            r = client.request(method, path, json=body)
            assert detail_code(r) == (404, "not_found"), (method, path, r.text)


def test_the_run_workers_run_only_with_both_switches(settings, mongo, fake_clusters, fake_secrets):
    for over, present in (
        ({"runs_enabled": True, "runs_workers_enabled": True}, True),
        ({"runs_enabled": False, "runs_workers_enabled": True}, False),
        ({"runs_enabled": True, "runs_workers_enabled": False}, False),
    ):
        with api_client(make_app(settings, fake_clusters, fake_secrets, **over)) as c:
            assert (len(c.app.state.run_workers) == 3) is present, over


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"cron": "*/2 * * * *"}, (422, "schedule_too_frequent")),
        ({"cron": "every day"}, (422, "schedule_invalid_cron")),
        ({"time_zone": "Mars/Olympus"}, (422, "unknown_time_zone")),
        ({"expires_in_days": 400}, (422, "invalid_expiry")),
    ],
)
def test_unusable_rules_are_refused(runs_client, mongo, change, expected):
    agent_id = _deployed(runs_client, mongo)
    r = runs_client.post(f"{AG}/{agent_id}/schedules", json={**SCHEDULE, **change})
    assert detail_code(r) == expected, r.text


def test_runs_need_a_session_budget(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo, budget=None)
    r = runs_client.post(f"{AG}/{agent_id}/schedules", json=SCHEDULE)
    assert detail_code(r) == (409, "run_budget_required"), r.text
    r = runs_client.post(f"{AG}/{agent_id}/runs", json={"message": "hi"})
    assert detail_code(r) == (409, "run_budget_required"), r.text


def test_a_change_and_a_resume_rebind_and_a_pause_keeps_the_binding(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    created = _schedule(runs_client, agent_id, expires_in_days=365)
    path = f"{AG}/{agent_id}/schedules/{created['id']}"
    coll = mongo["agent_schedules"]
    coll.update_one({"_id": created["id"]}, {"$set": {"acts_as.sub": "someone-else"}})
    r = runs_client.patch(path, json={"message": "weekly report"})
    assert r.status_code == 200, r.text
    assert (r.json()["acts_as"]["sub"], r.json()["message"]) == ("header", "weekly report")
    # 만료 길이를 보내지 않은 변경은 지난번에 고른 길이로 센다
    assert r.json()["expires_in_days"] == 365
    coll.update_one({"_id": created["id"]}, {"$set": {"acts_as.sub": "someone-else"}})
    paused = runs_client.post(f"{path}/pause").json()
    assert (paused["paused"], paused["paused_reason"]) == (True, "paused")
    assert paused["acts_as"]["sub"] == "someone-else"
    resumed = runs_client.post(f"{path}/resume").json()
    assert (resumed["paused"], resumed["paused_reason"]) == (False, None)
    assert resumed["acts_as"]["sub"] == "header"
    assert runs_client.delete(path).status_code == 204
    assert runs_client.get(f"{AG}/{agent_id}/schedules").json()["schedules"] == []


def test_an_empty_patch_and_a_blank_message_are_refused(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    created = _schedule(runs_client, agent_id)
    r = runs_client.patch(f"{AG}/{agent_id}/schedules/{created['id']}", json={})
    assert detail_code(r) == (422, "empty_patch"), r.text
    r = runs_client.post(f"{AG}/{agent_id}/runs", json={"message": "   "})
    assert r.status_code == 422, r.text


def test_run_now_records_a_starting_run_without_its_input_and_a_stop_ends_it(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    r = runs_client.post(f"{AG}/{agent_id}/runs", json={"message": "check the queue"})
    assert r.status_code == 202, r.text
    run = r.json()
    assert (run["source"], run["status"]) == ("manual", "starting")
    assert run["input_bytes"] == len(b"check the queue")
    assert "message" not in run and run["created_at"].endswith("+00:00")
    stopped = runs_client.post(f"{AG}/{agent_id}/runs/{run['id']}/cancel")
    assert stopped.status_code == 202, stopped.text
    assert (stopped.json()["status"], stopped.json()["reason"]) == ("cancelled", "stopped")
    assert mongo["agent_runs"].find_one({"_id": run["id"]})["message"] is None
    listed = runs_client.get(f"{AG}/{agent_id}/runs").json()["runs"]
    assert [x["id"] for x in listed] == [run["id"]]


def test_run_now_at_the_cap_answers_429(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    acts = ActsAs(sub="header")
    for _ in range(MAX_RUNNING):
        run = new_run(
            tenant="default", agent_id=agent_id, source="manual", acts_as=acts, now=utcnow()
        ).model_copy(update={"status": "running"})
        mongo["agent_runs"].insert_one(run.to_doc())
    r = runs_client.post(f"{AG}/{agent_id}/runs", json={"message": "hi"})
    assert detail_code(r) == (429, "agent_run_limit"), r.text
    assert r.headers.get("retry-after") == "60"


def test_another_agent_or_tenant_cannot_reach_a_schedule_or_run(runs_client, mongo):
    spec = _spec(runs_client, mongo)
    _p, inst = deploy_spec(runs_client, spec)
    agent_id = inst["id"]
    other_id = create_agent(runs_client, spec, "other").json()["id"]
    schedule = _schedule(runs_client, agent_id)
    run = runs_client.post(f"{AG}/{agent_id}/runs", json={"message": "hi"}).json()
    for path in (
        f"{AG}/{other_id}/schedules/{schedule['id']}/pause",
        f"{AG}/{other_id}/runs/{run['id']}/cancel",
    ):
        assert detail_code(runs_client.post(path)) == (404, "not_found"), path
    stranger = {TENANT_HEADER: "elsewhere"}
    for path in (
        f"{AG}/{agent_id}/schedules/{schedule['id']}/pause",
        f"{AG}/{agent_id}/runs/{run['id']}/cancel",
    ):
        assert runs_client.post(path, headers=stranger).status_code == 404, path
    assert runs_client.get(f"{AG}/{agent_id}/runs/{run['id']}").json()["status"] == "starting"


def test_unknown_runs_and_schedules_are_not_found(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    assert detail_code(runs_client.get(f"{AG}/{agent_id}/runs/nope")) == (404, "not_found")
    r = runs_client.post(f"{AG}/{agent_id}/schedules/nope/pause")
    assert detail_code(r) == (404, "not_found")


def test_the_run_list_and_read_carry_the_sessions_ledger_tokens_and_cost(
    runs_client, fake_clusters, mongo
):
    agent = deployed_agent(runs_client, fake_clusters, mongo)
    acts = ActsAs(sub="header")
    ran = new_run(
        tenant="default", agent_id=agent.id, source="manual", acts_as=acts, now=utcnow()
    ).model_copy(update={"status": "succeeded", "session_id": "s1"})
    unstarted = new_run(
        tenant="default", agent_id=agent.id, source="manual", acts_as=acts, now=utcnow()
    )
    for run in (ran, unstarted):
        mongo["agent_runs"].insert_one(run.to_doc())
    push_events(runs_client, agent, [model_call(1, "a000000000000001")])
    listed = {r["id"]: r for r in runs_client.get(f"{agent.url}/runs").json()["runs"]}
    got = listed[ran.id]
    assert got["usage"] == {"input_tokens": 1000, "output_tokens": 500}
    assert got["cost_usd"] > 0
    # 세션이 없는 실행은 원장 몫이 없다. 칸은 null 로 온다
    assert (listed[unstarted.id]["usage"], listed[unstarted.id]["cost_usd"]) == (None, None)
    read = runs_client.get(f"{agent.url}/runs/{ran.id}").json()
    assert (read["usage"], read["cost_usd"]) == (got["usage"], got["cost_usd"])


def test_the_turns_of_a_session_list_the_runs_they_called_with_their_usage(
    runs_client, fake_clusters, mongo
):
    """부른 턴의 트레이스 창이 읽는 목록이다 (P46 6.5절). 호출받은 Agent 의 원장에서 그 실행의 턴
    몫을 붙이고, 다른 세션이 부른 실행은 싣지 않는다."""
    agent = deployed_agent(runs_client, fake_clusters, mongo)

    made = iter(range(3))

    def called(session: str, turn: str, **fields: Any) -> AgentRun:
        caller = RunCaller(
            agent_id=agent.id, session_id=session, turn_id=turn, chain=[agent.id], **fields
        )
        run = new_run(
            tenant="default",
            agent_id=agent.id,
            source="agent",
            acts_as=ActsAs(sub="header"),
            # 목록은 만든 차례다. 같은 밀리초에 만든 기록은 차례가 없다
            now=utcnow() + timedelta(seconds=next(made)),
            caller=caller,
        )
        mongo["agent_runs"].insert_one(run.to_doc())
        return run

    ran = called("s1", "t1").model_copy(
        update={"status": "succeeded", "session_id": "s-called", "turn_id": "t-called"}
    )
    mongo["agent_runs"].replace_one({"_id": ran.id}, ran.to_doc())
    task = called("s1", "t1", thread_id="th-1")
    called("s-other", "t1")
    push_events(
        runs_client,
        agent,
        [
            run_event("turn_start", 0, span_id=RUN),
            model_call(1, "a000000000000001", session_id="s-called", turn_id="t-called"),
        ],
    )
    (turn,) = runs_client.get(f"{agent.url}/sessions/s1/turns").json()["turns"]
    first, second = turn["calls"]
    assert (first["run_id"], first["agent_id"], first["status"]) == (ran.id, agent.id, "succeeded")
    assert first["agent_name"] and not first["task"]
    assert first["usage"] == {"input_tokens": 1000, "output_tokens": 500} and first["cost_usd"] > 0
    # 아직 턴이 없는 실행은 원장 몫이 없다
    assert (second["run_id"], second["task"], second["usage"]) == (task.id, True, None)


def _waiting_run(mongo, agent_id: str, **fields: Any) -> str:
    """header 에게 묶여 세션 s1 에서 승인을 기다리는 실행을 넣는다."""
    run = waiting_run_of(agent_id=agent_id, acts_as=ActsAs(sub="header"), **fields)
    mongo["agent_runs"].insert_one(run.to_doc())
    return run.id


def test_a_decision_on_a_run_session_resumes_it_through_the_platform(
    runs_client, fake_clusters, mongo
):
    agent = deployed_agent(runs_client, fake_clusters, mongo)
    run_id = _waiting_run(mongo, agent.id)
    r = runs_client.post(
        f"{agent.url}/sessions/s1/decisions", json={"decisions": [{"type": "approve"}]}
    )
    assert r.status_code == 200, r.text
    (call,) = [c for c in agent.runtime.state.calls if c["path"].endswith("/decisions")]
    assert (call["body"]["run_id"], call["body"]["ephemeral"]) == (run_id, True)
    assert mongo["agent_runs"].find_one({"_id": run_id})["status"] == "running"


def test_a_decision_the_runtime_refuses_moves_the_run_back(runs_client, fake_clusters, mongo):
    agent = deployed_agent(runs_client, fake_clusters, mongo)
    run_id = _waiting_run(mongo, agent.id)
    two = {"decisions": [{"type": "approve"}, {"type": "approve"}]}
    r = runs_client.post(f"{agent.url}/sessions/s1/decisions", json=two)
    assert detail_code(r) == (422, "decision_count")
    assert mongo["agent_runs"].find_one({"_id": run_id})["status"] == "waiting_approval"


def test_a_decision_past_the_deadline_answers_run_closed_without_the_runtime(
    runs_client, fake_clusters, mongo
):
    agent = deployed_agent(runs_client, fake_clusters, mongo)
    run_id = _waiting_run(mongo, agent.id, deadline=utcnow() - timedelta(seconds=1))
    r = runs_client.post(
        f"{agent.url}/sessions/s1/decisions", json={"decisions": [{"type": "approve"}]}
    )
    assert detail_code(r) == (409, "run_closed")
    assert not [c for c in agent.runtime.state.calls if c["path"].endswith("/decisions")]
    assert mongo["agent_runs"].find_one({"_id": run_id})["status"] == "waiting_approval"


def test_the_approvers_inbox_lists_run_sessions_that_wait_with_their_deadline(
    runs_client, fake_clusters, mongo
):
    agent = deployed_agent(runs_client, fake_clusters, mongo)
    run_id = _waiting_run(mongo, agent.id)
    inbox = f"{agent.url}/sessions?all_users=true&status=pending&runs=only"
    r = runs_client.get(inbox)
    assert r.status_code == 200, r.text
    (row,) = r.json()["sessions"]
    assert (row["session_id"], row["run"]["id"], row["run"]["status"]) == (
        "s1",
        run_id,
        "waiting_approval",
    )
    assert row["run"]["deadline"].endswith("Z") or "+00:00" in row["run"]["deadline"]
    (call,) = [c for c in agent.runtime.state.calls if c["path"] == "/sessions"]
    assert call["params"]["runs"] == "only"
    # 더 기다리지 않는 실행의 세션은 받은 결정함에서 빠진다
    mongo["agent_runs"].update_one({"_id": run_id}, {"$set": {"status": "refused"}})
    assert runs_client.get(inbox).json()["sessions"] == []


def test_a_person_lists_and_pauses_what_runs_as_them_across_tenants(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo)
    schedule = _schedule(runs_client, agent_id)
    other = schedule_of(agent_id=agent_id, acts_as=ActsAs(sub="someone-else")).to_doc()
    mongo["agent_schedules"].insert_one(other)
    listed = runs_client.get("/me/runs-as-me").json()["schedules"]
    assert [(s["id"], s["tenant"], s["agent_name"]) for s in listed] == [
        (schedule["id"], "default", runs_client.get(f"{AG}/{agent_id}").json()["name"])
    ]
    paused = runs_client.post(f"/me/runs-as-me/schedules/{schedule['id']}/pause")
    assert paused.status_code == 200, paused.text
    assert (paused.json()["paused"], paused.json()["paused_reason"]) == (True, "paused")
    # 남에게 묶인 스케줄은 없는 것과 같다
    r = runs_client.post(f"/me/runs-as-me/schedules/{other['_id']}/pause")
    assert detail_code(r) == (404, "not_found")


TRIGGER = {"name": "deploys", "template": "Deployed {{payload./ref}}"}


def test_a_trigger_shows_its_secret_once_and_keeps_only_the_value_in_a_secret(
    runs_client, mongo, fake_secrets
):
    agent_id = _deployed(runs_client, mongo, guarded=True)
    r = runs_client.post(f"{AG}/{agent_id}/triggers", json=TRIGGER)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["secret"].startswith("whsec_")
    assert created["path"] == f"/api/hooks/{created['id']}"
    stored = mongo["agent_triggers"].find_one({"_id": created["id"]})
    assert "secret" not in stored and stored["secret_fp"] != created["secret"]
    (data,) = [d for (ns, name), d in fake_secrets.secrets.items() if name.endswith(agent_id)]
    assert data[created["id"]] == created["secret"]
    listed = runs_client.get(f"{AG}/{agent_id}/triggers").json()["triggers"]
    assert [t["id"] for t in listed] == [created["id"]] and "secret" not in listed[0]

    rotated = runs_client.post(f"{AG}/{agent_id}/triggers/{created['id']}/rotate-secret").json()
    assert rotated["secret"] != created["secret"] and rotated["previous_until"] is not None
    assert runs_client.delete(f"{AG}/{agent_id}/triggers/{created['id']}").status_code == 204
    (data,) = [d for (ns, name), d in fake_secrets.secrets.items() if name.endswith(agent_id)]
    assert created["id"] not in data
    me = runs_client.get("/me/runs-as-me").json()
    assert me["triggers"] == []


def _signed(secret: str, body: bytes, msg_id: str = "msg-1") -> dict[str, str]:
    stamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }


def test_a_signed_webhook_call_records_a_run_and_a_forged_one_is_refused(runs_client, mongo):
    agent_id = _deployed(runs_client, mongo, guarded=True)
    created = runs_client.post(f"{AG}/{agent_id}/triggers", json=TRIGGER).json()
    body = json.dumps({"ref": "main"}).encode()
    hook = f"/hooks/{created['id']}"
    r = runs_client.post(hook, content=body, headers=_signed(created["secret"], body))
    assert r.status_code == 202, r.text
    run = mongo["agent_runs"].find_one({"_id": r.json()["run_id"]})
    assert (run["source"], run["delivery_id"], run["status"]) == ("webhook", "msg-1", "starting")
    forged = runs_client.post(hook, content=body, headers=_signed("whsec_" + "A" * 44, body))
    assert detail_code(forged) == (401, "hook_signature_invalid")
    # 경로가 스스로 감사 행을 쓴다. 요청마다의 행은 없다
    assert mongo["audit_events"].count_documents({"path": "/api" + hook, "method": "POST"}) == 2
    assert {d["action"] for d in mongo["audit_events"].find({"path": "/api" + hook})} == {
        "hook_accepted",
        "hook_refused",
    }
