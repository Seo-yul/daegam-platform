"""세션 토큰 예산이다(D26). service.budget.session_tokens 를 스레드 상태로 판정한다.

after_model 훅이 마지막 AIMessage 의 usage_metadata(input + output) 를 스레드 상태 daegam_tokens
에 누적한다. 체크포인트와 같이 저장되므로 파드가 바뀌어도 이어진다. 한도를 넘으면 custom 신호
budget{scope: session, action: turn_ended} 를 내고 jump_to=end 로 턴을 끝낸다. 런타임은 그것을
SSE error{code: budget_exceeded} 로 옮기고, 이후 /chat 은 색인의 누적 usage 로 429 를 낸다.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from runtime import signals

TOKENS_KEY = "daegam_tokens"
# budget 관측 이벤트의 scope·action 어휘다. 여기 것이 정본이고 stream.py 와 main.py 가 같이 쓴다
SCOPE_SESSION = "session"
SCOPE_DAILY = "daily"
ACTION_TURN_ENDED = "turn_ended"
ACTION_REJECTED = "rejected"


class BudgetState(AgentState[Any]):
    daegam_tokens: NotRequired[Annotated[int, PrivateStateAttr]]


def usage_tokens(meta: dict[str, Any] | None) -> int:
    if not meta:
        return 0
    return int(meta.get("input_tokens") or 0) + int(meta.get("output_tokens") or 0)


class SessionBudget(AgentMiddleware[BudgetState]):
    state_schema = BudgetState

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = int(limit)

    @property
    def name(self) -> str:
        return "DaegamSessionBudget"

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: BudgetState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        messages = state["messages"]
        last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last is None:
            return None
        spent = usage_tokens(last.usage_metadata)
        if spent == 0:
            return None
        used = int(state.get(TOKENS_KEY, 0) or 0) + spent
        if used > self.limit:
            signals.emit(
                signals.BUDGET,
                scope=SCOPE_SESSION,
                action=ACTION_TURN_ENDED,
                used=used,
                limit=self.limit,
            )
            return {TOKENS_KEY: used, "jump_to": "end"}
        return {TOKENS_KEY: used}

    @hook_config(can_jump_to=["end"])
    async def aafter_model(
        self, state: BudgetState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
