"""다른 Agent 에게 맡긴 작업의 기계 경로 (P46 6.7절, `api/agent_protocol.py`). 앱에 붙은 경로의 꼴을
본다. 부르는 런타임의 작업 Tool 이 보내는 요청 다섯을 글자 그대로 받고, OpenAPI 밖이고, 호출 경로와
같은 본문 상한이 있고, 감사 미들웨어가 아니라 경로가 행을 쓴다. 판단은
`tests/test_agent_task_runs.py` 가 본다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.api.agent_calls import AGENT_CALL_BODY_LIMIT
from daegam.api.agent_protocol import AGENT_PROTOCOL_BODY_LIMIT
from daegam.audit.store import AUDIT_EVENTS
from daegam.services.agent_call_runs import CallCredentials
from daegam.urls import AGENT_PROTOCOL_PATHS
from tests.helpers.http import api_client, detail_code, make_app

AGENT = "ag-billing"
BASE = f"/internal/agent-protocol/{AGENT}"
BEARER = {"Authorization": "Bearer not-a-token"}
DESCRIPTION = "look up the open invoices"
# 작업을 시작하는 Tool 이 싣는 실행의 본문이다. 잇는 Tool 은 여기에 `multitask_strategy` 를 더한다
RUN_BODY: dict[str, Any] = {
    "input": {"messages": [{"role": "user", "content": DESCRIPTION}]},
    "stream_mode": "values",
    "stream_subgraphs": False,
    "stream_resumable": False,
    "assistant_id": AGENT,
}
UPDATE_BODY = {**RUN_BODY, "multitask_strategy": "interrupt"}


class RecordingTasks:
    """경로가 넘긴 것을 적고 서비스의 답 꼴로 답하는 가짜다."""

    def __init__(self) -> None:
        self.seen: list[tuple[Any, ...]] = []

    def _note(self, name: str, *args: Any, creds: CallCredentials, **kwargs: Any) -> None:
        assert creds.bearer == "not-a-token"
        self.seen.append((name, *args, kwargs) if kwargs else (name, *args))

    async def create_thread(self, agent_id: str, creds: CallCredentials) -> dict[str, Any]:
        self._note("create_thread", agent_id, creds=creds)
        return {"thread_id": "th1"}

    async def get_thread(
        self, agent_id: str, thread_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        self._note("get_thread", agent_id, thread_id, creds=creds)
        messages = [{"type": "ai", "content": "three invoices are open"}]
        return {"thread_id": thread_id, "values": {"messages": messages}}

    async def create_run(
        self, agent_id: str, thread_id: str, creds: CallCredentials, **kwargs: Any
    ) -> dict[str, Any]:
        self._note("create_run", agent_id, thread_id, creds=creds, **kwargs)
        return {"run_id": "r1", "thread_id": thread_id, "status": "pending"}

    async def get_run(
        self, agent_id: str, thread_id: str, run_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        self._note("get_run", agent_id, thread_id, run_id, creds=creds)
        return {"run_id": run_id, "thread_id": thread_id, "status": "success"}

    async def cancel_run(
        self, agent_id: str, thread_id: str, run_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        self._note("cancel_run", agent_id, thread_id, run_id, creds=creds)
        return {}


@pytest.fixture
def client(settings, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets, runs_workers_enabled=False)
    with api_client(app) as c:
        yield c


@pytest.fixture
def tasks(client) -> RecordingTasks:
    recording = RecordingTasks()
    client.app.state.agent_tasks = recording
    return recording


def test_the_task_tools_requests_reach_the_service_as_sent(client, tasks):
    """작업 Tool 다섯이 보내는 요청 다섯이다. 시작은 thread 와 실행을, 확인은 실행과 (끝났으면)
    thread 를, 잇기는 실행을, 취소는 본문 없는 취소를 보낸다."""
    created = client.post(f"{BASE}/threads", json={}, headers=BEARER)
    assert (created.status_code, created.json()) == (200, {"thread_id": "th1"})
    started = client.post(f"{BASE}/threads/th1/runs", json=RUN_BODY, headers=BEARER)
    assert (started.status_code, started.json()["run_id"]) == (200, "r1")
    checked = client.get(f"{BASE}/threads/th1/runs/r1", headers=BEARER)
    assert (checked.status_code, checked.json()["status"]) == (200, "success")
    answer = client.get(f"{BASE}/threads/th1", headers=BEARER)
    assert answer.json()["values"]["messages"][-1]["content"] == "three invoices are open"
    updated = client.post(f"{BASE}/threads/th1/runs", json=UPDATE_BODY, headers=BEARER)
    assert updated.json()["run_id"] == "r1"
    cancelled = client.post(
        f"{BASE}/threads/th1/runs/r1/cancel",
        params={"wait": 0, "action": "interrupt"},
        headers=BEARER,
    )
    assert (cancelled.status_code, cancelled.content) == (202, b"")
    run = {"assistant_id": AGENT, "message": DESCRIPTION}
    assert tasks.seen == [
        ("create_thread", AGENT),
        ("create_run", AGENT, "th1", run),
        ("get_run", AGENT, "th1", "r1"),
        ("get_thread", AGENT, "th1"),
        ("create_run", AGENT, "th1", run),
        ("cancel_run", AGENT, "th1", "r1"),
    ]


def test_a_thread_is_created_without_a_body_too(client, tasks):
    assert client.post(f"{BASE}/threads", headers=BEARER).status_code == 200
    assert tasks.seen == [("create_thread", AGENT)]


def test_a_run_needs_its_assistant_and_one_message(client, tasks):
    for broken in (
        {k: v for k, v in RUN_BODY.items() if k != "assistant_id"},
        {**RUN_BODY, "input": {"messages": []}},
        {**RUN_BODY, "input": {"messages": [{"role": "user", "content": ""}]}},
        {**RUN_BODY, "input": {"messages": [{"role": "user", "content": [{"type": "text"}]}]}},
    ):
        refused = client.post(f"{BASE}/threads/th1/runs", json=broken, headers=BEARER)
        assert refused.status_code == 422, broken
    assert tasks.seen == []


def test_the_five_routes_are_served_outside_openapi_and_need_a_bearer(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "agent-protocol" in p]
    for headers in ({}, BEARER):
        answers = [
            client.post(f"{BASE}/threads", json={}, headers=headers),
            client.get(f"{BASE}/threads/th1", headers=headers),
            client.post(f"{BASE}/threads/th1/runs", json=RUN_BODY, headers=headers),
            client.get(f"{BASE}/threads/th1/runs/r1", headers=headers),
            client.post(f"{BASE}/threads/th1/runs/r1/cancel", headers=headers),
        ]
        assert [detail_code(r) for r in answers] == [(401, "unauthorized")] * 5


def test_the_routes_bound_the_body_like_a_call(client):
    assert AGENT_PROTOCOL_BODY_LIMIT[1] == AGENT_CALL_BODY_LIMIT[1]
    big = b"x" * (AGENT_PROTOCOL_BODY_LIMIT[1] + 1)
    refused = client.post(f"{BASE}/threads/th1/runs", content=big, headers=BEARER)
    assert detail_code(refused) == (413, "payload_too_large")


def test_each_request_leaves_the_routes_own_audit_row(client, mongo):
    client.post(f"{BASE}/threads", json={}, headers=BEARER)
    client.get(f"{BASE}/threads/th1/runs/r1", headers=BEARER)
    client.post(f"{BASE}/threads/th1/runs/r1/cancel", headers=BEARER)
    rows = list(mongo[AUDIT_EVENTS].find({"resource": "agent-calls"}))
    assert sorted((r["action"], r["method"], r["status"], r["error_code"]) for r in rows) == [
        ("agent_task_run_cancel", "POST", 401, "unauthorized"),
        ("agent_task_run_get", "GET", 401, "unauthorized"),
        ("agent_task_thread_create", "POST", 401, "unauthorized"),
    ]
    assert {r["path_params"]["agent_id"] for r in rows} == {AGENT}
    # 감사 미들웨어는 이 경로를 따로 적지 않는다
    assert mongo[AUDIT_EVENTS].count_documents({"path": {"$regex": "agent-protocol"}}) == 3


def test_the_calls_switch_turns_the_routes_off(settings, fake_clusters, fake_secrets):
    app = make_app(settings, fake_clusters, fake_secrets, agent_calls_enabled=False)
    with api_client(app) as c:
        refused = c.post(f"{BASE}/threads", json={}, headers=BEARER)
        assert detail_code(refused) == (409, "agent_calls_disabled")


@pytest.mark.parametrize(
    ("path", "matches"),
    [
        ("/api/internal/agent-protocol/a1/threads", True),
        ("/api/internal/agent-protocol/a1/threads/t1", True),
        ("/api/internal/agent-protocol/a1/threads/t1/runs", True),
        ("/api/internal/agent-protocol/a1/threads/t1/runs/r1", True),
        ("/api/internal/agent-protocol/a1/threads/t1/runs/r1/cancel", True),
        ("/api/internal/agent-protocol/a1", False),
        ("/api/internal/agent-protocol/a1/threads/t1/state", False),
        ("/api/internal/agent-protocol/a1/threads/t1/runs/r1/join", False),
        ("/api/internal/agent-calls/a1/threads", False),
        ("/x/api/internal/agent-protocol/a1/threads", False),
    ],
)
def test_the_path_pattern_matches_the_five_routes_only(path, matches):
    assert bool(AGENT_PROTOCOL_PATHS.match(path)) is matches
