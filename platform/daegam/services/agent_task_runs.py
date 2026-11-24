"""다른 Agent 에게 맡긴 작업 (P46 6.7절). 작업 하나는 호출받는 Agent 의 세션 하나이고, 그 실행들은
아무도 기다리지 않는 Agent 호출이다.

부르는 Agent 의 런타임이 Agent Protocol 의 다섯 경로(`api/agent_protocol.py`)로 작업을 만들고 실행을
시작하고 읽고 취소한다. 누구의 요청인지, 사슬, 한 턴의 호출 수, 상한, 속도, 믿지 않는 입력, 감사는
기다리는 호출과 같은 코드다(`services/agent_call_runs.py`). 여기에는 작업에만 있는 것을 둔다.

- **묶음.** `threads.create` 가 작업을 테넌트, 호출받는 Agent, 부르는 Agent, 그 사람에 묶는다
  (`store/agent_tasks.py`). 나머지 네 경로는 넷이 모두 맞는 요청만 받고 그 밖에는 404 다. 다른
  작업의 실행 id 도 404 다. 토큰은 경로가 댄 Agent 를 대상으로 낸 것만 받으므로(`caller`), 다른
  Agent 의 경로에서는 401 이다.
- **실행의 시작.** `runs.create` 는 `assistant_id` 가 경로의 Agent 가 아니면 404 다. 호출의 확인을
  시작마다 다시 지나고, 작업의 세션에서 돌 실행 기록을 넣은 뒤 그 작업의 앞 실행이 아직 열려 있으면
  `caller_updated` 로 멈춘다. 시작 워커의 한 걸음을 그 자리에서 한 번 돌리고 실행 id 를 곧바로
  답한다. 앞 턴이 끝나지 않았거나 Agent 를 깨우는 중이면 시작 워커의 틱이 잇는다. 승인을 기다리는
  작업은 세션이 새 턴을 받지 않으므로 409 `agent_task_waiting` 으로 거절한다.
- **끝.** 작업의 실행은 부른 턴이 끝나도 돈다. `runs.cancel` 이 열린 실행을 `caller_cancelled` 로
  멈추고, 그 밖에는 실행 자신의 한도와 중지가 끝낸다.
- **상태.** `runs.get` 은 실행의 상태를 Agent Protocol 의 다섯 값으로만 답한다(`protocol_status`).
  `cancelled` 와 `error` 는 실행의 코드와 문장을 `error` 한 줄에 싣는다. `interrupted` 와 플랫폼의
  상태 이름은 답하지 않는다. 부르는 쪽이 앞의 것은 끝난 것으로, 뒤의 것은 답 없는 끝나지 않은 것으로
  읽기 때문이다.
- **답.** `threads.get` 은 작업 세션의 마지막 답을 메시지 하나로 준다. 끝난 실행이 있는데 전사를
  읽지 못했으면 빈 답 대신 503 `agent_task_answer_unread` 다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daegam.errors import retry_after_header
from daegam.services.agent_call_audit import AuditedRoute, Seen
from daegam.services.agent_call_runs import (
    CALLER_ENDED,
    CALLER_TIMEOUT,
    AgentCallError,
    AgentCallRuns,
    CallCredentials,
    CallingTurn,
    TaskTurn,
    not_found,
)
from daegam.services.runs.person import ACTS_AS_UNAVAILABLE, ACTS_AS_UNCONFIRMED, LOST_ACCESS
from daegam.services.runs.service import (
    AGENT_CALL_BUSY,
    AGENT_DELETED,
    AGENT_UNDEPLOYED,
)
from daegam.services.runs.start import RUN_LOST
from daegam.services.runs.watch import DEADLINE_PASSED
from daegam.store.agent_runs import OPEN_STATUSES, STOPPED, AgentRun
from daegam.store.agent_tasks import AgentTaskThread, MongoAgentTaskThreadStore
from daegam.store.base import utcnow
from daegam.urls import AGENT_PROTOCOL_PATH

# Agent Protocol 의 실행 상태다. 부르는 런타임의 작업 Tool 이 이 다섯만 읽는다
PENDING, RUNNING, SUCCESS, CANCELLED, ERROR = "pending", "running", "success", "cancelled", "error"
# 실행의 상태 → Agent Protocol 의 상태. 표에 없는 상태는 `error` 다
PROTOCOL_STATUS = {
    "starting": PENDING,
    "waiting_approval": PENDING,
    "running": RUNNING,
    "succeeded": SUCCESS,
    "cancelled": CANCELLED,
}
# `error` 칸을 싣는 상태
WITH_ERROR = frozenset({CANCELLED, ERROR})
# 작업의 실행을 부른 Agent 가 끝낸 까닭. 취소와, 새 지시가 앞 실행을 갈음한 것이다
CALLER_CANCELLED = "caller_cancelled"
CALLER_UPDATED = "caller_updated"
TASK_WAITING = "agent_task_waiting"
ANSWER_UNREAD = "agent_task_answer_unread"
# 전사를 읽지 못한 답을 다시 물을 때까지의 시간
ANSWER_RETRY_AFTER = timedelta(seconds=2)
# 답의 메시지 종류. LangGraph 의 모델 메시지다
ANSWER_MESSAGE_TYPE = "ai"
# 실행의 코드마다 부르는 모델이 읽을 문장이다. 표에 없는 코드는 `_FALLBACK_SENTENCE` 다
ERROR_SENTENCES = {
    STOPPED: "a person stopped the run",
    CALLER_CANCELLED: "the calling agent cancelled the task",
    CALLER_UPDATED: "a newer instruction from the calling agent replaced this run",
    CALLER_ENDED: "the calling turn ended before the run finished",
    CALLER_TIMEOUT: "the calling agent stopped waiting for the run",
    LOST_ACCESS: "the person this task runs as lost access",
    ACTS_AS_UNCONFIRMED: "the person this task runs as has been away past the sign-in bound",
    ACTS_AS_UNAVAILABLE: "the task could not run as the calling person",
    AGENT_UNDEPLOYED: "the called agent was undeployed",
    AGENT_DELETED: "the called agent was deleted",
    AGENT_CALL_BUSY: "the called agent is at its limit of calls from other agents",
    DEADLINE_PASSED: "nobody decided the approval before its deadline",
    RUN_LOST: "the run was lost before it reported its end",
}
_FALLBACK_SENTENCE = "the run ended as {status}"

# 경로마다의 감사 행이다. 호출의 행처럼 호출받는 Agent 의 경로를 적고 작업과 실행은 칸이 싣는다
_PATH = AGENT_PROTOCOL_PATH
CREATE_THREAD = AuditedRoute("agent_task_thread_create", path=_PATH)
GET_THREAD = AuditedRoute("agent_task_thread_get", method="GET", path=_PATH)
CREATE_RUN = AuditedRoute("agent_task_run_create", path=_PATH)
GET_RUN = AuditedRoute("agent_task_run_get", method="GET", path=_PATH)
CANCEL_RUN = AuditedRoute("agent_task_run_cancel", path=_PATH, ok_status=202)


def protocol_status(status: str) -> str:
    """실행의 상태를 Agent Protocol 의 상태로 옮긴다."""
    return PROTOCOL_STATUS.get(status, ERROR)


def error_text(run: AgentRun) -> str:
    """끝난 실행의 코드와 문장 한 줄이다. 코드가 없는 실행은 상태가 코드다."""
    code = run.reason or run.status
    sentence = ERROR_SENTENCES.get(code) or _FALLBACK_SENTENCE.format(status=run.status)
    return f"{code}: {sentence}"


def _session_of(runs: list[AgentRun]) -> AgentRun | None:
    """작업의 세션을 적은 가장 늦은 실행이다. 아직 어느 실행도 턴을 열지 못했으면 None 이다."""
    return next((r for r in runs if r.session_id is not None), None)


class AgentTaskRuns:
    def __init__(self, calls: AgentCallRuns, threads: MongoAgentTaskThreadStore):
        self.calls = calls
        self.threads = threads
        self.runs = calls.runs

    # -- 경로 다섯 ---------------------------------------------------------------------------

    async def create_thread(self, called_id: str, creds: CallCredentials) -> dict[str, Any]:
        """작업을 만들어 부르는 쪽에 묶는다. 호출받는 Agent 가 지금 부를 수 없는 Agent 면 호출의
        거절이다."""
        seen = Seen()

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self.calls.caller(called_id, creds)
            await self.calls.called(caller, called_id)
            thread = await self.threads.create(
                tenant=caller.agent.tenant,
                agent_id=called_id,
                caller_agent_id=caller.agent.acting_id,
                sub=caller.acts_as.sub,
                now=utcnow(),
            )
            seen.thread_id = thread.id
            return {"thread_id": thread.id}

        return await self.calls.audit.audited(CREATE_THREAD, called_id, seen, work)

    async def get_thread(
        self, called_id: str, thread_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        """작업의 마지막 답을 `values.messages` 의 메시지 하나로 준다. 아직 답이 없으면 빈
        목록이다."""
        seen = Seen(thread_id=thread_id)

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self.calls.caller(called_id, creds)
            thread = await self._thread(caller, called_id, thread_id)
            text = await self._answer(thread)
            messages = [] if text is None else [{"type": ANSWER_MESSAGE_TYPE, "content": text}]
            return {"thread_id": thread.id, "values": {"messages": messages}}

        return await self.calls.audit.audited(GET_THREAD, called_id, seen, work)

    async def create_run(
        self,
        called_id: str,
        thread_id: str,
        creds: CallCredentials,
        *,
        assistant_id: str,
        message: str,
    ) -> dict[str, Any]:
        """작업의 다음 실행을 시작하고 그 id 를 곧바로 답한다. 앞 실행이 아직 열려 있으면 멈춘다."""
        seen = Seen(thread_id=thread_id)

        async def work() -> dict[str, Any]:
            caller = seen.caller = await self.calls.caller(called_id, creds)
            thread = await self._thread(caller, called_id, thread_id)
            if assistant_id != called_id:
                raise not_found()
            called, item = await self.calls.called(caller, called_id)
            earlier = await self.runs.for_thread(thread.tenant, thread.agent_id, thread.id)
            if any(r.status == "waiting_approval" for r in earlier):
                raise AgentCallError(
                    409, TASK_WAITING, "this task waits for an approval and takes no new run"
                )
            held = _session_of(earlier)
            task = TaskTurn(thread.id, held.session_id if held is not None else None)
            run = await self.calls.admit(caller, called, item, creds, message, task)
            seen.run_id = run.id
            await self.threads.touch(thread.id, utcnow())
            await self._stop_open(earlier, CALLER_UPDATED)
            # 한 걸음만 돌린다. 미뤄진 시작은 틱이 잇고, 부른 쪽은 id 를 받아야 읽고 취소한다
            await self.calls.start_inline(run, self.calls.clock())
            fresh = await self.runs.get(run.tenant, run.agent_id, run.id)
            seen.status = (fresh or run).status
            return self._run_out(thread, fresh or run)

        return await self.calls.audit.audited(CREATE_RUN, called_id, seen, work)

    async def get_run(
        self, called_id: str, thread_id: str, run_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        seen = Seen(run_id=run_id, thread_id=thread_id)

        async def work() -> dict[str, Any]:
            thread, run = await self._bound_run(seen, called_id, thread_id, run_id, creds)
            return self._run_out(thread, run)

        return await self.calls.audit.audited(GET_RUN, called_id, seen, work)

    async def cancel_run(
        self, called_id: str, thread_id: str, run_id: str, creds: CallCredentials
    ) -> dict[str, Any]:
        """열린 실행을 멈춘다. 이미 끝난 실행은 그대로 둔다. 답은 본문이 없다."""
        seen = Seen(run_id=run_id, thread_id=thread_id)

        async def work() -> dict[str, Any]:
            _, run = await self._bound_run(seen, called_id, thread_id, run_id, creds)
            await self._stop_open([run], CALLER_CANCELLED)
            return {}

        return await self.calls.audit.audited(CANCEL_RUN, called_id, seen, work)

    # -- 묶음 --------------------------------------------------------------------------------

    async def _thread(self, caller: CallingTurn, called_id: str, thread_id: str) -> AgentTaskThread:
        """이 요청에 묶인 작업이다. 테넌트, 호출받는 Agent, 부르는 Agent, 사람 가운데 하나라도
        다르면 404 다."""
        thread = await self.threads.get_bound(
            thread_id,
            tenant=caller.agent.tenant,
            agent_id=called_id,
            caller_agent_id=caller.agent.acting_id,
            sub=caller.acts_as.sub,
        )
        if thread is None:
            raise not_found()
        return thread

    async def _bound_run(
        self, seen: Seen, called_id: str, thread_id: str, run_id: str, creds: CallCredentials
    ) -> tuple[AgentTaskThread, AgentRun]:
        """이 요청에 묶인 작업과 그 작업의 실행이다. 다른 작업의 실행은 404 다."""
        caller = seen.caller = await self.calls.caller(called_id, creds)
        thread = await self._thread(caller, called_id, thread_id)
        run = await self.runs.get(thread.tenant, thread.agent_id, run_id)
        if run is None or run.caller is None or run.caller.thread_id != thread.id:
            raise not_found()
        seen.status = run.status
        return thread, run

    # -- 실행과 답 ----------------------------------------------------------------------------

    async def _stop_open(self, runs: list[AgentRun], reason: str) -> None:
        for run in runs:
            if run.status in OPEN_STATUSES:
                await self.calls.run_service.stop(run, reason)

    @staticmethod
    def _run_out(thread: AgentTaskThread, run: AgentRun) -> dict[str, Any]:
        status = protocol_status(run.status)
        out = {"run_id": run.id, "thread_id": thread.id, "status": status}
        if status in WITH_ERROR:
            out["error"] = error_text(run)
        return out

    async def _answer(self, thread: AgentTaskThread) -> str | None:
        """작업 세션의 마지막 답이다. 아직 끝난 실행이 없으면 None 이다."""
        runs = await self.runs.for_thread(thread.tenant, thread.agent_id, thread.id)
        held = _session_of(runs)
        if held is None:
            return None
        called = await self.calls.agents.get(thread.agent_id, tenant=thread.tenant)
        text = await self.calls.answer_text(called, held)
        if text is None and any(r.status == "succeeded" for r in runs):
            raise AgentCallError(
                503,
                ANSWER_UNREAD,
                "the task's answer could not be read yet",
                headers=retry_after_header(ANSWER_RETRY_AFTER),
            )
        return text


__all__ = [
    "CALLER_CANCELLED",
    "CALLER_UPDATED",
    "PROTOCOL_STATUS",
    "AgentTaskRuns",
    "error_text",
    "protocol_status",
]
