"""다른 Agent 를 Tool 로 부르는 기계 경로 (P46 6.3절, `api/agent_calls.py`). 앱에 붙은 경로의 꼴만
본다. OpenAPI 밖이고, 본문과 `wait` 의 한도가 있고, 감사 미들웨어가 아니라 경로가 행을 쓴다. 판단은
`tests/test_agent_call_runs.py` 가 본다.
"""

from __future__ import annotations

import pytest

from daegam.api.agent_calls import AGENT_CALL_BODY_LIMIT
from daegam.audit.store import AUDIT_EVENTS
from daegam.urls import AGENT_CALL_PATHS
from tests.helpers.http import api_client, detail_code, make_app

CALL = "/internal/agent-calls/ag-billing"
BEARER = {"Authorization": "Bearer not-a-token"}


@pytest.fixture
def client(settings, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets, runs_workers_enabled=False)
    with api_client(app) as c:
        yield c


def test_the_three_routes_are_served_outside_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "agent-calls" in p]
    assert detail_code(client.post(CALL, json={"message": "hi"})) == (401, "unauthorized")
    answers = [
        client.post(CALL, json={"message": "hi"}, headers=BEARER),
        client.get(f"{CALL}/runs/r1", params={"wait": 0}, headers=BEARER),
        client.post(f"{CALL}/runs/r1/cancel", json={"reason": "caller_ended"}, headers=BEARER),
    ]
    assert [detail_code(r) for r in answers] == [(401, "unauthorized")] * 3


def test_the_routes_bound_the_body_the_wait_and_the_cancel_reason(client):
    limit = AGENT_CALL_BODY_LIMIT[1]
    big = client.post(CALL, content=b"x" * (limit + 1), headers=BEARER)
    assert detail_code(big) == (413, "payload_too_large")
    assert client.post(CALL, json={"message": ""}, headers=BEARER).status_code == 422
    long_wait = client.get(f"{CALL}/runs/r1", params={"wait": 26}, headers=BEARER)
    assert long_wait.status_code == 422
    reason = client.post(f"{CALL}/runs/r1/cancel", json={"reason": "stopped"}, headers=BEARER)
    assert reason.status_code == 422


def test_each_request_leaves_the_routes_own_audit_row(client, mongo):
    client.post(CALL, json={"message": "hi"}, headers=BEARER)
    client.get(f"{CALL}/runs/r1", headers=BEARER)
    rows = list(mongo[AUDIT_EVENTS].find({"resource": "agent-calls"}))
    assert sorted((r["action"], r["status"], r["error_code"]) for r in rows) == [
        ("agent_call_poll", 401, "unauthorized"),
        ("agent_call_start", 401, "unauthorized"),
    ]
    assert rows[0]["path_params"]["agent_id"] == "ag-billing"
    # 감사 미들웨어는 이 경로를 따로 적지 않는다
    assert mongo[AUDIT_EVENTS].count_documents({"path": {"$regex": "agent-calls"}}) == 2


def test_the_calls_switch_turns_the_routes_off_and_leaves_the_rest(
    settings, fake_clusters, fake_secrets
):
    app = make_app(settings, fake_clusters, fake_secrets, agent_calls_enabled=False)
    with api_client(app) as c:
        assert detail_code(c.post(CALL, json={"message": "hi"}, headers=BEARER)) == (
            409,
            "agent_calls_disabled",
        )
        assert c.get("/agents").status_code == 200


@pytest.mark.parametrize(
    ("path", "matches"),
    [
        ("/api/internal/agent-calls/a1", True),
        ("/api/internal/agent-calls/a1/runs/r1", True),
        ("/api/internal/agent-calls/a1/runs/r1/cancel", True),
        ("/api/internal/agent-calls/a1/runs/r1/other", False),
        ("/api/agents/a1/runs/r1/cancel", False),
        ("/x/api/internal/agent-calls/a1", False),
    ],
)
def test_the_path_pattern_matches_the_three_routes_only(path, matches):
    assert bool(AGENT_CALL_PATHS.match(path)) is matches
