"""다른 Agent 에게 맡긴 작업의 판단 (P46 6.7절, `services/agent_task_runs.py`).

호출 경로의 시험(`tests/test_agent_call_runs.py`)과 같은 틀 위에 작업의 묶음을 더 세운다. 누구의
요청인지, 사슬, 상한, 속도는 그 시험이 보고, 여기는 작업에만 있는 것을 본다. 묶음과 404, 아무도
기다리지 않는 실행, 앞 실행을 멈추고 같은 세션에서 잇는 다음 실행, 상태의 옮김, 마지막 답, 취소다.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import HTTPException

from daegam.config import Settings
from daegam.services import agent_call_runs
from daegam.services.agent_call_runs import MAX_CALLS_PER_TURN, CallCredentials
from daegam.services.agent_features import AGENT_TASKS
from daegam.services.agent_task_runs import (
    CALLER_CANCELLED,
    CALLER_UPDATED,
    AgentTaskRuns,
    error_text,
    protocol_status,
)
from daegam.services.runs.events import RunEvents
from daegam.services.runs.start import TASK_TURN_DEFER
from daegam.store.agent_runs import ActsAs, AgentRun, MongoAgentRunStore, RunCaller, new_run
from daegam.store.agent_tasks import AgentTaskThread, MongoAgentTaskThreadStore
from daegam.store.base import utcnow
from daegam.store.turn_grants import GrantTarget, MongoTurnGrantStore, TurnGrant
from tests.helpers.aio import run_stores
from tests.helpers.runs import FakeUsers, alice, person_check
from tests.test_agent_call_runs import (
    ANSWER,
    CALLED,
    CALLING,
    MESSAGE,
    REFUSALS,
    TENANT,
    THIRD,
    CallAgent,
    Rig,
    _local,
    _refused,
    _uses,
    token,
)

OTHER = "ag-other"
STORES = (MongoTurnGrantStore, MongoAgentRunStore, MongoAgentTaskThreadStore)


class TaskRig(Rig):
    """호출의 틀에 작업의 서비스를 더한 것이다."""

    def __init__(
        self,
        settings: Settings,
        grants: MongoTurnGrantStore,
        runs: MongoAgentRunStore,
        threads: MongoAgentTaskThreadStore,
    ) -> None:
        super().__init__(settings, grants, runs)
        # 호출받는 Agent 의 이미지가 작업 세션을 안다
        self.agents.by_id[CALLED].features = (AGENT_TASKS,)
        self.threads = threads
        self.tasks = AgentTaskRuns(self.calls, threads)

    async def thread(self, grant: TurnGrant, called: str = CALLED) -> str:
        made = await self.tasks.create_thread(called, CallCredentials(token(grant, called)))
        return made["thread_id"]

    async def run(self, grant: TurnGrant, thread_id: str, **creds: Any) -> dict[str, Any]:
        return await self.tasks.create_run(
            CALLED,
            thread_id,
            CallCredentials(token(grant), **creds),
            assistant_id=CALLED,
            message=MESSAGE,
        )

    async def end_turn(self, run_id: str, status: str = "done") -> None:  # type: ignore[override]
        """런타임이 그 실행의 턴이 status 로 끝났다고 알린 이벤트다."""
        event = {
            "_id": f"ev-{run_id}",
            "event": "turn_end",
            "status": status,
            "session_id": "s1",
            "ts": utcnow(),
            "data": {"run_id": run_id},
        }
        await RunEvents(self.runs, self.agents).apply(CALLED, TENANT, [event])  # type: ignore[arg-type]

    async def task_run(self, thread_id: str, status: str, **fields: Any) -> AgentRun:
        """그 작업의 실행 기록 하나를 status 로 넣는다."""
        run = new_run(
            tenant=TENANT,
            agent_id=CALLED,
            source="agent",
            acts_as=ActsAs(sub="alice", username="alice", tv=3),
            now=utcnow(),
            session_id="s1",
            turn_id="t1",
            caller=RunCaller(agent_id=CALLING, chain=[CALLING], thread_id=thread_id),
        ).model_copy(update={"status": status, **fields})
        await self.runs.insert(run)
        return run


def _go(settings: Settings, body: Any) -> Any:
    async def with_rig(*stores: Any) -> Any:
        return await body(TaskRig(_local(settings), *stores))

    return run_stores(_local(settings), STORES, with_rig)


def _creds(grant: TurnGrant, called: str = CALLED) -> CallCredentials:
    return CallCredentials(token(grant, called))


@pytest.fixture(autouse=True)
def no_session_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """세션이 아직 붙지 않은 위임을 다시 읽기 전에 기다리지 않는다."""
    monkeypatch.setattr(agent_call_runs, "SESSION_ATTACH_WAIT_SECONDS", 0.0)


async def _all_refused(rig: TaskRig, thread_id: str, run_id: str, creds: CallCredentials) -> list:
    """그 자격으로 작업의 네 경로를 불렀을 때의 거절이다."""
    tasks, called = rig.tasks, CALLED
    requests = (
        lambda: tasks.get_thread(called, thread_id, creds),
        lambda: tasks.get_run(called, thread_id, run_id, creds),
        lambda: tasks.cancel_run(called, thread_id, run_id, creds),
        lambda: tasks.create_run(called, thread_id, creds, assistant_id=called, message=MESSAGE),
    )
    refusals = []
    for request in requests:
        with pytest.raises(REFUSALS) as e:
            await request()
        refusals.append(_refused(e))
    return refusals


# -- 묶음: 만든 쪽만 읽고 잇고 취소한다 ------------------------------------------------------


def test_a_thread_is_bound_to_its_tenant_agents_and_person(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        stored = await rig.threads.coll.find_one({"_id": thread_id})
        bound = {k: stored[k] for k in ("tenant", "agent_id", "caller_agent_id", "sub")}
        assert bound == {
            "tenant": TENANT,
            "agent_id": CALLED,
            "caller_agent_id": CALLING,
            "sub": "alice",
        }
        row = rig.audit.events[-1]
        assert (row.action, row.status, row.sub) == ("agent_task_thread_create", 200, "alice")
        assert row.path_params["thread_id"] == thread_id
        assert row.path.endswith(f"/internal/agent-protocol/{CALLED}")

    _go(settings, body)


def test_another_person_gets_404_on_every_route(settings):
    async def body(rig: TaskRig) -> None:
        bob = alice(id="bob", username="bob")
        person = person_check(
            rig.settings,
            FakeUsers(alice(), bob),
            ("alice", TENANT, "viewer"),
            ("bob", TENANT, "viewer"),
        )
        rig.service.person = rig.calls.person = person
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        run = await rig.run(grant, thread_id)
        other = await rig.grant("g-bob", sub="bob", username="bob")
        refusals = await _all_refused(rig, thread_id, run["run_id"], _creds(other))
        assert refusals == [(404, "not_found")] * 4
        # 그 사람의 요청은 실행을 건드리지 않았다
        assert (await rig.stored(run["run_id"])).stop_requested_at is None

    _go(settings, body)


def test_another_calling_agent_gets_404_on_every_route(settings):
    async def body(rig: TaskRig) -> None:
        rig.agents.by_id[OTHER] = CallAgent(id=OTHER, name="other", **_uses(CALLED))
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        run = await rig.run(grant, thread_id)
        other = await rig.grant("g-other", agent_id=OTHER)
        refusals = await _all_refused(rig, thread_id, run["run_id"], _creds(other))
        assert refusals == [(404, "not_found")] * 4

    _go(settings, body)


def test_another_called_agents_path_does_not_open_the_thread(settings):
    """다른 Agent 의 경로에서는 그 Agent 의 토큰으로도 404 이고, 이 Agent 의 토큰은 401 이다."""

    async def body(rig: TaskRig) -> None:
        rig.agents.by_id[CALLING] = CallAgent(id=CALLING, name="support", **_uses(CALLED, THIRD))
        targets = [GrantTarget(kind="agent", id=CALLED), GrantTarget(kind="agent", id=THIRD)]
        grant = await rig.grant(targets=targets)
        thread_id = await rig.thread(grant)
        run = await rig.run(grant, thread_id)
        tasks = rig.tasks
        for creds, want in ((_creds(grant, THIRD), 404), (_creds(grant, CALLED), 401)):
            with pytest.raises(REFUSALS) as e:
                await tasks.get_thread(THIRD, thread_id, creds)
            assert _refused(e)[0] == want
            with pytest.raises(REFUSALS) as e:
                await tasks.get_run(THIRD, thread_id, run["run_id"], creds)
            assert _refused(e)[0] == want
            with pytest.raises(REFUSALS) as e:
                await tasks.cancel_run(THIRD, thread_id, run["run_id"], creds)
            assert _refused(e)[0] == want

    _go(settings, body)


def test_another_tenants_thread_and_another_threads_run_are_404(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        now = utcnow()
        foreign = AgentTaskThread(
            id="th-foreign",
            tenant="other",
            agent_id=CALLED,
            caller_agent_id=CALLING,
            sub="alice",
            created_at=now,
            expires_at=now,
        )
        await rig.threads.coll.insert_one(foreign.to_doc())
        with pytest.raises(REFUSALS) as e:
            await rig.tasks.get_thread(CALLED, foreign.id, _creds(grant))
        assert _refused(e) == (404, "not_found")
        mine, second = await rig.thread(grant), await rig.thread(grant)
        run = await rig.run(grant, mine)
        with pytest.raises(REFUSALS) as e:
            await rig.tasks.get_run(CALLED, second, run["run_id"], _creds(grant))
        assert _refused(e) == (404, "not_found")
        # 기다리는 호출의 경로도 작업의 실행을 읽지 않는다
        with pytest.raises(REFUSALS) as e:
            await rig.calls.poll(CALLED, run["run_id"], _creds(grant), 0)
        assert _refused(e) == (404, "not_found")

    _go(settings, body)


def test_the_routes_need_the_call_credentials(settings):
    async def body(rig: TaskRig) -> None:
        with pytest.raises(HTTPException) as e:
            await rig.tasks.create_thread(CALLED, CallCredentials(None))
        assert e.value.status_code == 401
        # 토큰 인증 모드에서 인스턴스 토큰은 작업도 열지 못한다
        with pytest.raises(REFUSALS) as e:
            await rig.tasks.create_thread(CALLED, CallCredentials("instance-token-calling"))
        assert _refused(e) == (403, "acts_as_unavailable")
        off = dataclasses.replace(rig.settings, agent_calls_enabled=False)
        rig.calls.settings = off
        grant = await rig.grant()
        with pytest.raises(REFUSALS) as e:
            await rig.thread(grant)
        assert _refused(e) == (409, "agent_calls_disabled")
        assert await rig.threads.coll.count_documents({}) == 0

    _go(settings, body)


# -- 실행의 시작 ---------------------------------------------------------------------------


def test_a_run_starts_at_once_and_nobody_awaits_it(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        answer = await rig.run(grant, thread_id)
        assert answer == {"run_id": answer["run_id"], "thread_id": thread_id, "status": "running"}
        run = await rig.stored(answer["run_id"])
        caller = run.caller
        assert caller is not None and run.source == "agent"
        assert (caller.thread_id, caller.agent_id, caller.chain) == (thread_id, CALLING, [CALLING])
        # 부른 턴의 위임과 기다리는 끝을 적지 않아 그 턴이 끝나도 돈다
        assert (caller.grant_id, caller.timeout_at, caller.long_bound) == (None, None, False)
        sent = rig.proxy.calls[0]
        assert (sent["task"], sent["session_id"], sent["run_id"]) == (True, None, run.id)
        assert rig.opened.opened[0]["trigger"] == "agent"
        await rig.calls.caller_ended(grant.id)
        await rig.grants.coll.update_one({"_id": grant.id}, {"$set": {"ended_at": utcnow()}})
        assert await rig.calls.sweep() == 0
        assert (await rig.stored(run.id)).status == "running"
        row = rig.audit.events[-1]
        assert (row.action, row.resource_id, row.path_params["run_status"]) == (
            "agent_task_run_create",
            run.id,
            "running",
        )

    _go(settings, body)


def test_each_start_counts_against_the_calling_turn_and_the_chain(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        for _ in range(MAX_CALLS_PER_TURN):
            await rig.run(grant, await rig.thread(grant))
        with pytest.raises(REFUSALS) as e:
            await rig.run(grant, thread_id)
        assert _refused(e) == (429, "agent_call_fanout")
        # 호출 수를 다 쓴 턴은 작업 기록도 남기지 못한다
        threads = await rig.threads.coll.count_documents({})
        with pytest.raises(REFUSALS) as e:
            await rig.thread(grant)
        assert _refused(e) == (429, "agent_call_fanout")
        assert await rig.threads.coll.count_documents({}) == threads
        # 사슬에 호출받는 Agent 가 이미 있는 턴은 작업도 만들지 못한다
        calling_run = new_run(
            tenant=TENANT,
            agent_id=CALLING,
            source="agent",
            acts_as=ActsAs(sub="alice"),
            now=utcnow(),
            caller=RunCaller(agent_id=CALLED, chain=[CALLED]),
        )
        await rig.runs.insert(calling_run)
        looped = await rig.grant("g-loop", run_id=calling_run.id)
        with pytest.raises(REFUSALS) as e:
            await rig.thread(looped)
        assert _refused(e) == (409, "agent_call_cycle")

    _go(settings, body)


def test_the_assistant_must_be_the_paths_agent(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        with pytest.raises(REFUSALS) as e:
            await rig.tasks.create_run(
                CALLED, thread_id, _creds(grant), assistant_id=THIRD, message=MESSAGE
            )
        assert _refused(e) == (404, "not_found")
        assert await rig.runs.for_thread(TENANT, CALLED, thread_id) == []

    _go(settings, body)


def test_the_next_run_stops_the_running_turn_and_follows_in_the_same_session(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        first = await rig.run(grant, thread_id)
        second = await rig.run(grant, thread_id)
        assert second["status"] == "pending" and second["run_id"] != first["run_id"]
        stopped = await rig.stored(first["run_id"])
        assert (stopped.status, stopped.stop_reason) == ("running", CALLER_UPDATED)
        assert [r.id for r in rig.turns.stopped] == [first["run_id"]]
        # 앞 턴이 끝나기 전에는 런타임을 부르지 않고 잠깐 미룬다
        waiting = await rig.stored(second["run_id"])
        assert (waiting.status, waiting.session_id, len(rig.proxy.calls)) == ("starting", "s1", 1)
        assert waiting.lease is not None
        assert waiting.lease.until - utcnow() <= TASK_TURN_DEFER
        await rig.end_turn(first["run_id"], "cancelled")
        ended = await rig.tasks.get_run(CALLED, thread_id, first["run_id"], _creds(grant))
        assert ended["status"] == "cancelled" and ended["error"].startswith(f"{CALLER_UPDATED}: ")
        await rig.starter.start_run(waiting, utcnow())
        sent = rig.proxy.calls[1]
        assert (sent["task"], sent["session_id"], sent["run_id"]) == (
            True,
            "s1",
            second["run_id"],
        )
        assert (await rig.stored(second["run_id"])).status == "running"

    _go(settings, body)


def test_a_task_waiting_for_an_approval_takes_no_new_run(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        waiting = await rig.task_run(thread_id, "waiting_approval")
        with pytest.raises(REFUSALS) as e:
            await rig.run(grant, thread_id)
        assert _refused(e) == (409, "agent_task_waiting")
        assert (await rig.stored(waiting.id)).status == "waiting_approval"
        read = await rig.tasks.get_run(CALLED, thread_id, waiting.id, _creds(grant))
        assert read == {"run_id": waiting.id, "thread_id": thread_id, "status": "pending"}

    _go(settings, body)


def test_a_task_whose_approval_ended_undecided_takes_no_new_run(settings):
    """승인을 기다리던 실행을 취소하면 런타임의 세션은 승인 대기로 남는다. 다음 실행은 기록도 호출
    수도 남기지 않고 거절된다."""

    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        waiting = await rig.task_run(thread_id, "waiting_approval")
        await rig.tasks.cancel_run(CALLED, thread_id, waiting.id, _creds(grant))
        assert (await rig.stored(waiting.id)).status == "cancelled"
        assert rig.audit.events[-1].path_params["run_status"] == "cancelled"

        async def pending(agent: Any, session_id: str, **_: Any) -> tuple[int, Any]:
            items = [{"kind": "user", "text": MESSAGE}, {"kind": "interrupt"}]
            return 200, {"session": {"running": False}, "transcript": {"items": items}}

        rig.proxy.session = pending  # type: ignore[method-assign]
        with pytest.raises(REFUSALS) as e:
            await rig.run(grant, thread_id)
        assert _refused(e) == (409, "agent_task_closed")
        assert [r.id for r in await rig.runs.for_thread(TENANT, CALLED, thread_id)] == [waiting.id]
        counted = await rig.grants.get(grant.id)
        assert counted is not None and counted.calls == 0 and rig.proxy.calls == []

    _go(settings, body)


def test_a_cancelled_run_that_left_no_approval_takes_the_next_run(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        await rig.task_run(thread_id, "cancelled")
        answer = await rig.run(grant, thread_id)
        assert answer["status"] == "running"
        assert rig.proxy.calls[0]["session_id"] == "s1"

    _go(settings, body)


def test_a_called_image_that_takes_no_tasks_refuses_the_routes(settings):
    """저장과 배포의 확인 뒤에 호출받는 Agent 를 옛 이미지로 다시 배포했다."""

    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        rig.agents.by_id[CALLED].features = ()
        with pytest.raises(REFUSALS) as e:
            await rig.thread(grant)
        assert _refused(e) == (409, "runtime_feature_missing")
        with pytest.raises(REFUSALS) as e:
            await rig.run(grant, thread_id)
        assert _refused(e) == (409, "runtime_feature_missing")
        assert await rig.threads.coll.count_documents({}) == 1
        assert await rig.runs.for_thread(TENANT, CALLED, thread_id) == []

    _go(settings, body)


def test_a_session_still_running_its_turn_makes_the_start_retry(settings):
    """앞 턴이 끝났다고 알린 뒤에도 런타임이 그 턴이 돈다고 답하면 작업의 실행은 다시 한다."""

    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        await rig.task_run(thread_id, "cancelled")

        async def busy(agent: Any, **kwargs: Any) -> Any:
            rig.proxy.calls.append(kwargs)
            return rig.proxy.answer(409, {}, b'{"code": "turn_in_progress", "message": "x"}')

        rig.proxy.chat = busy  # type: ignore[method-assign]
        answer = await rig.run(grant, thread_id)
        run = await rig.stored(answer["run_id"])
        assert (answer["status"], run.status, run.attempts) == ("pending", "starting", 1)

    _go(settings, body)


# -- 상태와 답 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "want"),
    [
        ("starting", "pending"),
        ("waiting_approval", "pending"),
        ("running", "running"),
        ("succeeded", "success"),
        ("cancelled", "cancelled"),
        ("failed", "error"),
        ("refused", "error"),
        ("skipped", "error"),
        ("a_later_status", "error"),
    ],
)
def test_run_statuses_map_to_the_five_protocol_values(status, want):
    assert protocol_status(status) == want


def test_an_ended_run_carries_its_code_and_sentence_in_error(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        lost = await rig.task_run(thread_id, "cancelled", reason="acts_as_lost_access")
        failed = await rig.task_run(thread_id, "failed", reason="some_runtime_code")
        done = await rig.task_run(thread_id, "succeeded")
        read = {
            run.id: await rig.tasks.get_run(CALLED, thread_id, run.id, _creds(grant))
            for run in (lost, failed, done)
        }
        assert read[lost.id]["status"] == "cancelled"
        assert read[lost.id]["error"] == (
            "acts_as_lost_access: the person this task runs as lost access"
        )
        assert read[failed.id]["status"] == "error"
        assert read[failed.id]["error"] == "some_runtime_code: the run ended as failed"
        assert read[done.id] == {"run_id": done.id, "thread_id": thread_id, "status": "success"}
        assert error_text(done.model_copy(update={"status": "refused"})) == (
            "refused: the run ended as refused"
        )
        # 승인 대기로 남은 세션이 거절한 실행은 새 작업을 시작하라고 말한다
        stuck = done.model_copy(update={"status": "failed", "reason": "session_pending"})
        assert error_text(stuck).endswith("start a new task")

    _go(settings, body)


def test_the_thread_returns_the_last_answer_as_its_only_message(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        empty = await rig.tasks.get_thread(CALLED, thread_id, _creds(grant))
        assert empty == {"thread_id": thread_id, "values": {"messages": []}}
        run = await rig.run(grant, thread_id)
        await rig.end_turn(run["run_id"])
        read = await rig.tasks.get_thread(CALLED, thread_id, _creds(grant))
        assert read["values"]["messages"] == [{"type": "ai", "content": ANSWER}]

        async def unread(agent: Any, session_id: str, **_: Any) -> tuple[int, Any]:
            return 503, None

        rig.proxy.session = unread  # type: ignore[method-assign]
        with pytest.raises(REFUSALS) as e:
            await rig.tasks.get_thread(CALLED, thread_id, _creds(grant))
        assert _refused(e) == (503, "agent_task_answer_unread")
        assert e.value.headers == {"Retry-After": "2"}

    _go(settings, body)


# -- 취소 ----------------------------------------------------------------------------------


def test_cancel_stops_an_open_run_and_leaves_an_ended_one(settings):
    async def body(rig: TaskRig) -> None:
        grant = await rig.grant()
        thread_id = await rig.thread(grant)
        run = await rig.run(grant, thread_id)
        assert await rig.tasks.cancel_run(CALLED, thread_id, run["run_id"], _creds(grant)) == {}
        marked = await rig.stored(run["run_id"])
        assert (marked.status, marked.stop_reason) == ("running", CALLER_CANCELLED)
        assert [r.id for r in rig.turns.stopped] == [run["run_id"]]
        assert rig.audit.events[-1].status == 202
        await rig.end_turn(run["run_id"], "cancelled")
        read = await rig.tasks.get_run(CALLED, thread_id, run["run_id"], _creds(grant))
        assert read["status"] == "cancelled"
        assert read["error"] == f"{CALLER_CANCELLED}: the calling agent cancelled the task"
        done = await rig.task_run(thread_id, "succeeded")
        await rig.tasks.cancel_run(CALLED, thread_id, done.id, _creds(grant))
        assert (await rig.stored(done.id)).status == "succeeded"

    _go(settings, body)


# -- 세션 하나의 실행 여럿 -------------------------------------------------------------------


def test_a_task_sessions_run_is_its_latest(settings):
    """결정 경로와 승인자의 목록이 세션으로 찾는 실행은 그 세션의 가장 늦은 실행이다."""

    async def body(rig: TaskRig) -> None:
        older = await rig.task_run("th-1", "cancelled")
        newer = await rig.task_run("th-1", "waiting_approval")
        assert older.created_at <= newer.created_at
        found = await rig.runs.for_session(TENANT, CALLED, "s1")
        assert found is not None and found.id == newer.id
        many = await rig.runs.for_sessions(TENANT, CALLED, ["s1"])
        assert many["s1"].id == newer.id
        assert [r.id for r in await rig.runs.for_thread(TENANT, CALLED, "th-1")] == [
            newer.id,
            older.id,
        ]

    _go(settings, body)
