"""다른 Agent 에게 작업을 맡긴다(능력 agent_tasks 의 부르는 쪽).

agent.json 의 Agent 소스 가운데 `mode` 가 `task` 인 것 전부가 Tool 다섯 한 벌을 같이 쓴다. 맡기기
(`start_async_task`), 확인(`check_async_task`), 잇기(`update_async_task`), 취소
(`cancel_async_task`), 목록(`list_async_tasks`)이고, 모델은 맡길 Agent 를 `subagent_type` 으로
고른다. 이름, 인자, 설명, 답의 문장, 그래프 상태 `async_tasks` 는 deepagents 의 비동기 서브에이전트
Tool 과 같다. tests/test_agent_task_tools.py 가 잠긴 deepagents 와 이름, 인자, 설명을 맞댄다.

- **요청.** 소스의 `url` 이 호출받는 Agent 의 Agent Protocol 경로다. 맡기기는 `POST <url>/threads`
  (본문 `{}`) 뒤에 `POST <url>/threads/{thread_id}/runs`, 확인은 `GET .../runs/{run_id}` 와 끝났으면
  `GET <url>/threads/{thread_id}`, 잇기는 `multitask_strategy` 를 실은 실행 하나, 취소는 본문 없는
  `POST .../runs/{run_id}/cancel?wait=0&action=interrupt`, 목록은 끝나지 않은 작업마다
  `GET .../runs/{run_id}` 다. deepagents 의 Tool 은 턴의 토큰을 요청마다 실을 수 없어 쓰지 않는다.
  런타임의 클라이언트로 보내고 요청마다 `ask_*` Tool 과 같은 자격을 싣는다(agent_calls.py 의
  `CallSender`).
- **작업.** 작업 id 는 thread id 다. 작업은 이 세션의 상태에만 있어 모르는 id 는 요청을 보내지
  않는다. Tool 은 작업을 스스로 확인하지 않는다. 사람이 물을 때 모델이 확인한다.
- **상태.** `success`, `error`, `cancelled`, `timeout`, `interrupted` 는 끝난 것이라 목록이 다시
  읽지 않는다. `pending` 은 시작 전이거나 승인을 기다리는 것이라 다시 읽는다.
- **deepagents 와 다른 곳.** 플랫폼이 거절한 요청은 그 코드를 실은 Tool 오류다. 끝난 작업의 답을
  읽지 못하면 빈 결과가 아니라 Tool 오류다. `thread_id`, `run_id`, `status` 가 없는 답은 턴을
  실패시키지 않고 Tool 오류다. 목록의 `running` 거름은 `pending` 인 작업도 낸다. 확인은 `error` 칸을
  `cancelled` 에서도 읽는다. 플랫폼이 취소의 까닭을 거기에 싣는다. 확인의 결과는 ASCII 밖의 글자를
  그대로 둔 JSON 이다.
- **표시.** 문서의 항목이 다섯 이름을 `allow` 에, 읽기만 하는 둘을 `read` 에, 답을 읽어 오는
  `check_async_task` 를 뺀 넷을 `inside` 에 싣는다. 호출받은 Agent 의 답을 싣는 확인의 결과만 바깥
  내용이다(tool_safety.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx2
from deepagents.middleware.async_subagents import AsyncSubAgentState, AsyncTask
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from runtime.agent_calls import (
    BAD_ANSWER_MESSAGE,
    REQUEST_TIMEOUT_SECONDS,
    AgentCallError,
    AgentCalls,
    Caller,
    CallSender,
    refused,
    tool_error,
)
from runtime.config import AgentConfig

log = logging.getLogger("daegam.agent-runtime.agent-tasks")

START = "start_async_task"
CHECK = "check_async_task"
UPDATE = "update_async_task"
CANCEL = "cancel_async_task"
LIST = "list_async_tasks"
TASK_TOOLS = (START, CHECK, UPDATE, CANCEL, LIST)
# 그래프 상태에서 작업을 두는 칸이다. deepagents 의 것과 같다
STATE_KEY = "async_tasks"

# Agent Protocol 의 실행 상태다
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
ERROR = "error"
CANCELLED = "cancelled"
# 더 바뀌지 않는 상태다. 목록이 다시 읽지 않는다
FINAL_STATUSES = frozenset({CANCELLED, SUCCESS, ERROR, "timeout", "interrupted"})
# 답의 `error` 칸을 읽는 상태다
WITH_ERROR = frozenset({ERROR, CANCELLED})
# 목록의 거름 `running` 이 내는 상태다
UNFINISHED = frozenset({RUNNING, PENDING})
ALL = "all"

# 실행을 시작하는 본문이 메시지와 `assistant_id` 곁에 싣는 칸이다
RUN_OPTIONS: dict[str, Any] = {
    "stream_mode": "values",
    "stream_subgraphs": False,
    "stream_resumable": False,
}
# 잇는 실행이 더 싣는 칸이다. 돌고 있는 앞 실행을 멈추고 시작한다
INTERRUPT = {"multitask_strategy": "interrupt"}
CANCEL_QUERY = {"wait": 0, "action": "interrupt"}

# 아래 설명과 답의 문장은 deepagents 의 것 그대로다. 모델이 읽는다
START_DESCRIPTION = """Start an async subagent on a remote server. The subagent runs in the background and returns a task ID immediately.

Available async agent types:
{available_agents}

## Usage notes:
1. This tool launches a background task and returns immediately with a task ID. Report the task ID to the user and stop — do NOT immediately check status.
2. Use `check_async_task` only when the user asks for a status update or result.
3. Use `update_async_task` to send new instructions to a running task.
4. Multiple async subagents can run concurrently — launch several and let them run in the background.
5. The subagent runs on a remote server, so it has its own tools and capabilities."""  # noqa: E501
CHECK_DESCRIPTION = (
    "Check the status of an async subagent task. Returns the current status and, if complete, the "
    "result. Statuses shown earlier in the conversation are always stale, so call this to get the "
    "current status rather than reporting a status from a previous tool result."
)
UPDATE_DESCRIPTION = (
    "Send updated instructions to an async subagent. Interrupts the current run and starts "
    "a new one on the same thread, so the subagent sees the full conversation history plus "
    "your new message. The task_id remains the same."
)
CANCEL_DESCRIPTION = (
    "Cancel a running async subagent task. Use this to stop a task that is no longer needed."
)
LIST_DESCRIPTION = (
    "List tracked async subagent tasks with their current live statuses. "
    "By default shows all tasks. Use `status_filter` to narrow by status "
    "(e.g. 'running', 'success', 'error', 'cancelled'). "
    "Use `check_async_task` to get the full result of a specific completed task. "
    "Statuses shown earlier in the conversation are always stale, so call this to read current "
    "statuses rather than reporting one from a previous tool result."
)
TASK_ID_DESCRIPTION = "The exact task_id string returned by start_async_task. Pass it verbatim."
UNKNOWN_TYPE_MESSAGE = "Unknown async subagent type `{name}`. Available types: {allowed}"
UNTRACKED_MESSAGE = "No tracked task found for task_id: {task_id!r}"
STARTED_MESSAGE = "Launched async subagent. task_id: {task_id}"
UPDATED_MESSAGE = "Updated async subagent. task_id: {task_id}"
CANCELLED_MESSAGE = "Cancelled async subagent task: {task_id}"
NO_TASKS_MESSAGE = "No async subagent tasks tracked."
NO_OUTPUT_RESULT = "(completed with no output messages)"
NO_ERROR_DETAIL = "The async subagent encountered an error."

# 런타임의 문장이다. 다른 런타임 Tool 문구처럼 한다체다
SEND_FAILED_MESSAGE = "tool {tool} 의 요청을 플랫폼에 보내지 못했다 ({error})."
AGENT_GONE_MESSAGE = "작업 {task_id} 을 맡은 Agent {agent} 가 이 Agent 의 Tool 에 더는 없다."


class StartArgs(BaseModel):
    description: str = Field(
        description="A detailed description of the task for the async subagent to perform."
    )
    subagent_type: str = Field(
        description=(
            "The type of async subagent to use. Must be one of the available types listed in the "
            "tool description."
        )
    )


class TaskArgs(BaseModel):
    task_id: str = Field(description=TASK_ID_DESCRIPTION)


class UpdateArgs(BaseModel):
    task_id: str = Field(description=TASK_ID_DESCRIPTION)
    message: str = Field(description="Follow-up instructions or context to send to the subagent.")


class ListArgs(BaseModel):
    status_filter: Literal["running", "success", "error", "cancelled", "all"] | None = Field(
        default=None,
        description=(
            "Filter tasks by status. One of: 'running', 'success', 'error', 'cancelled', 'all'. "
            "Defaults to 'all'."
        ),
    )


class AsyncTaskState(AgentMiddleware):
    """작업 Tool 을 가진 에이전트의 그래프 상태에 `async_tasks` 를 둔다. 갱신은 작업 id 마다
    덮어쓴다."""

    state_schema = AsyncSubAgentState


@dataclass(frozen=True)
class TaskAgent:
    """작업을 맡길 수 있는 Agent 하나다. name 이 모델이 고르는 `subagent_type` 이다."""

    name: str
    description: str
    agent_id: str
    url: str
    sender: CallSender


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update(runtime: ToolRuntime, text: str, tasks: dict[str, AsyncTask]) -> Command:
    """Tool 결과의 글과 상태에 적을 작업이다."""
    message = ToolMessage(text, tool_call_id=runtime.tool_call_id)
    return Command(update={"messages": [message], STATE_KEY: tasks})


def _changed(task: AsyncTask, status: str, now: str, **changes: str) -> AsyncTask:
    """상태를 status 로 적은 작업이다. 상태가 바뀌었을 때만 바뀐 때를 고친다."""
    moved = {"last_updated_at": now} if status != task["status"] else {}
    return AsyncTask(**{**task, "status": status, **moved, **changes})


def _tracked(runtime: ToolRuntime) -> dict[str, AsyncTask]:
    return runtime.state.get(STATE_KEY) or {}


def _field(tool: str, body: dict[str, Any], key: str) -> str:
    """답의 글 칸이다. 없거나 글이 아니면 Tool 오류다."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise AgentCallError(BAD_ANSWER_MESSAGE.format(tool=tool))
    return value


def _last_answer(thread: dict[str, Any]) -> Any:
    """thread 의 마지막 메시지의 내용이다. 메시지가 없으면 그렇다는 문장이다."""
    values = thread.get("values")
    messages = values.get("messages") if isinstance(values, dict) else None
    if not messages:
        return NO_OUTPUT_RESULT
    last = messages[-1]
    return last.get("content", "") if isinstance(last, dict) else str(last)


@dataclass
class AgentTasks:
    """작업을 맡기는 소스 전부의 Tool 다섯이다."""

    agents: dict[str, TaskAgent]
    http: Callable[[], httpx2.AsyncClient]

    # -- 요청 --------------------------------------------------------------------------------

    async def _send(
        self,
        tool: str,
        agent: TaskAgent,
        method: str,
        path: str,
        caller: Caller,
        **request: Any,
    ) -> httpx2.Response:
        """요청 하나를 보낸다. 보내지 못했거나 플랫폼이 거절했으면 Tool 오류다."""
        try:
            response = await self.http().request(
                method,
                f"{agent.url}{path}",
                headers=agent.sender.headers(tool, caller, started=False),
                timeout=REQUEST_TIMEOUT_SECONDS,
                **request,
            )
        except httpx2.HTTPError as e:
            log.warning("%s 의 요청을 플랫폼에 보내지 못했다: %s", tool, type(e).__name__)
            raise AgentCallError(
                SEND_FAILED_MESSAGE.format(tool=tool, error=type(e).__name__)
            ) from None
        if not response.is_success:
            raise await refused(tool, response)
        return response

    async def _read(
        self,
        tool: str,
        agent: TaskAgent,
        method: str,
        path: str,
        caller: Caller,
        **request: Any,
    ) -> dict[str, Any]:
        """요청 하나의 답 본문이다. JSON 객체가 아니면 Tool 오류다."""
        response = await self._send(tool, agent, method, path, caller, **request)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AgentCallError(BAD_ANSWER_MESSAGE.format(tool=tool))
        return body

    async def _create_run(
        self, tool: str, agent: TaskAgent, thread_id: str, text: str, caller: Caller, **extra: Any
    ) -> str:
        """thread 에 실행 하나를 시작하고 그 id 를 낸다."""
        body = {
            "input": {"messages": [{"role": "user", "content": text}]},
            **RUN_OPTIONS,
            "assistant_id": agent.agent_id,
            **extra,
        }
        run = await self._read(tool, agent, "POST", f"/threads/{thread_id}/runs", caller, json=body)
        return _field(tool, run, "run_id")

    async def _run_status(
        self, tool: str, agent: TaskAgent, task: AsyncTask, caller: Caller
    ) -> dict[str, Any]:
        """작업의 지금 실행이다. `status` 가 없는 답은 Tool 오류다."""
        path = f"/threads/{task['thread_id']}/runs/{task['run_id']}"
        run = await self._read(tool, agent, "GET", path, caller)
        _field(tool, run, "status")
        return run

    def _agent_of(self, task: AsyncTask) -> TaskAgent:
        """작업을 맡은 Agent 다. 그 소스가 문서에서 빠졌으면 Tool 오류다."""
        agent = self.agents.get(task["agent_name"])
        if agent is None:
            raise AgentCallError(
                AGENT_GONE_MESSAGE.format(task_id=task["task_id"], agent=task["agent_name"])
            )
        return agent

    def _known(
        self, task_id: str, runtime: ToolRuntime
    ) -> tuple[AsyncTask, TaskAgent, Caller] | str:
        """이 세션이 맡긴 작업, 그 작업을 맡은 Agent, 부르는 쪽이다. 모르는 id 면 그렇다고 답할
        문장이다."""
        task = _tracked(runtime).get(task_id.strip())
        if not task:
            return UNTRACKED_MESSAGE.format(task_id=task_id)
        return task, self._agent_of(task), Caller.of(runtime)

    # -- Tool 다섯 ---------------------------------------------------------------------------

    async def start(
        self, description: str, subagent_type: str, runtime: ToolRuntime
    ) -> str | Command:
        agent = self.agents.get(subagent_type)
        if agent is None:
            allowed = ", ".join(f"`{name}`" for name in self.agents)
            return UNKNOWN_TYPE_MESSAGE.format(name=subagent_type, allowed=allowed)
        caller = Caller.of(runtime)
        thread = await self._read(START, agent, "POST", "/threads", caller, json={})
        task_id = _field(START, thread, "thread_id")
        run_id = await self._create_run(START, agent, task_id, description, caller)
        now = _now()
        task = AsyncTask(
            task_id=task_id,
            agent_name=agent.name,
            thread_id=task_id,
            run_id=run_id,
            status=RUNNING,
            created_at=now,
            last_checked_at=now,
            last_updated_at=now,
        )
        return _update(runtime, STARTED_MESSAGE.format(task_id=task_id), {task_id: task})

    async def check(self, task_id: str, runtime: ToolRuntime) -> str | Command:
        known = self._known(task_id, runtime)
        if isinstance(known, str):
            return known
        task, agent, caller = known
        run = await self._run_status(CHECK, agent, task, caller)
        status = run["status"]
        result: dict[str, Any] = {"status": status, "thread_id": task["thread_id"]}
        if status == SUCCESS:
            thread = await self._read(CHECK, agent, "GET", f"/threads/{task['thread_id']}", caller)
            result["result"] = _last_answer(thread)
        elif status in WITH_ERROR and (run.get("error") or status == ERROR):
            result["error"] = str(run.get("error") or NO_ERROR_DETAIL)
        now = _now()
        updated = _changed(task, status, now, last_checked_at=now)
        text = json.dumps(result, ensure_ascii=False)
        return _update(runtime, text, {task["task_id"]: updated})

    async def update(self, task_id: str, message: str, runtime: ToolRuntime) -> str | Command:
        known = self._known(task_id, runtime)
        if isinstance(known, str):
            return known
        task, agent, caller = known
        run_id = await self._create_run(
            UPDATE, agent, task["thread_id"], message, caller, **INTERRUPT
        )
        now = _now()
        updated = _changed(task, RUNNING, now, run_id=run_id, last_updated_at=now)
        text = UPDATED_MESSAGE.format(task_id=task["task_id"])
        return _update(runtime, text, {task["task_id"]: updated})

    async def cancel(self, task_id: str, runtime: ToolRuntime) -> str | Command:
        known = self._known(task_id, runtime)
        if isinstance(known, str):
            return known
        task, agent, caller = known
        path = f"/threads/{task['thread_id']}/runs/{task['run_id']}/cancel"
        await self._send(CANCEL, agent, "POST", path, caller, params=CANCEL_QUERY)
        now = _now()
        updated = _changed(task, CANCELLED, now, last_checked_at=now, last_updated_at=now)
        text = CANCELLED_MESSAGE.format(task_id=task["task_id"])
        return _update(runtime, text, {task["task_id"]: updated})

    async def list_tasks(
        self, runtime: ToolRuntime, status_filter: str | None = None
    ) -> str | Command:
        tasks = [task for task in _tracked(runtime).values() if _listed(task, status_filter)]
        if not tasks:
            return NO_TASKS_MESSAGE
        caller = Caller.of(runtime)
        statuses = await asyncio.gather(*(self._live_status(task, caller) for task in tasks))
        now = _now()
        entries: list[str] = []
        updated: dict[str, AsyncTask] = {}
        for task, status in zip(tasks, statuses, strict=True):
            entries.append(
                f"- task_id: {task['task_id']}  agent: {task['agent_name']}  status: {status}"
            )
            updated[task["task_id"]] = _changed(task, status, now, last_checked_at=now)
        text = f"{len(entries)} tracked task(s):\n" + "\n".join(entries)
        return _update(runtime, text, updated)

    async def _live_status(self, task: AsyncTask, caller: Caller) -> str:
        """목록이 보이는 상태다. 끝난 작업은 다시 읽지 않고, 읽지 못하면 적어 둔 상태다."""
        if task["status"] in FINAL_STATUSES:
            return task["status"]
        try:
            run = await self._run_status(LIST, self._agent_of(task), task, caller)
        except AgentCallError as e:
            log.warning("작업 %s 의 상태를 읽지 못해 적어 둔 것을 보인다: %s", task["task_id"], e)
            return task["status"]
        return run["status"]

    def tools(self) -> list[BaseTool]:
        """Tool 다섯이다. ToolNode 가 `runtime` 에 상태와 턴 문맥을 넣어 준다."""
        listing = "\n".join(f"- {a.name}: {a.description}" for a in self.agents.values())
        built = (
            (START, self.start, START_DESCRIPTION.format(available_agents=listing), StartArgs),
            (CHECK, self.check, CHECK_DESCRIPTION, TaskArgs),
            (UPDATE, self.update, UPDATE_DESCRIPTION, UpdateArgs),
            (CANCEL, self.cancel, CANCEL_DESCRIPTION, TaskArgs),
            (LIST, self.list_tasks, LIST_DESCRIPTION, ListArgs),
        )
        return [
            StructuredTool.from_function(
                coroutine=coroutine,
                name=name,
                description=description,
                args_schema=args,
                infer_schema=False,
                handle_tool_error=tool_error,
            )
            for name, coroutine, description, args in built
        ]


def _listed(task: AsyncTask, status_filter: str | None) -> bool:
    """목록의 거름이 그 작업을 내는가. 적어 둔 상태로 거른다. `running` 은 시작 전이거나 승인을
    기다리는 작업도 낸다."""
    if not status_filter or status_filter == ALL:
        return True
    if status_filter == RUNNING:
        return task["status"] in UNFINISHED
    return task["status"] == status_filter


def task_tools_by_ref(config: AgentConfig, calls: AgentCalls) -> dict[str, list[BaseTool]]:
    """작업을 맡기는 소스의 ref → Tool 이다. 메인이 가리키는 소스 전부가 Tool 다섯 한 벌을 같이
    쓰므로 그 가운데 첫 소스에 그 벌을 두고 나머지는 비운다. 메인이 가리키는 것이 없으면 모두
    빈다. 요청은 Agent 호출의 자격과 클라이언트로 보낸다."""
    by_ref: dict[str, list[BaseTool]] = {source["ref"]: [] for source in config.task_sources}
    sources = [s for s in config.task_sources if s["ref"] in config.main["tools"]]
    if not sources:
        return by_ref
    agents = {
        source["source"]["name"]: TaskAgent(
            name=source["source"]["name"],
            description=str(source["description"]),
            agent_id=source["source"]["id"],
            url=str(source["url"]).rstrip("/"),
            sender=calls.sender(source),
        )
        for source in sources
    }
    by_ref[sources[0]["ref"]] = AgentTasks(agents, calls.client).tools()
    return by_ref
