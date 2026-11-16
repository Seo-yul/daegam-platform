"""그래프 안(미들웨어·훅)에서 런타임으로 보내는 신호다.

langgraph 의 custom 스트림(`get_stream_writer`)에 `{"daegam": "<종류>", ...}` 를 쓰면 stream.py 가
SSE 이벤트나 turn 상태로 옮기고 main.py 가 stdout 관측 이벤트로 남긴다. 그래프 밖(runnable
컨텍스트가 없는 곳)에서 부르면 조용히 버린다.
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer

CUSTOM_KEY = "daegam"

GUARDRAIL = "guardrail"
BUDGET = "budget"
MODEL_FALLBACK = "model_fallback"
MODEL_RETRY = "model_retry"
# 웹 가드가 거절한 호출이다(web_guard.py). stdout 관측에만 남는다
WEB_GUARD = "web_guard"
# 판정 모델이 판정하지 못한 바깥 결과다(judge.py). stdout 관측에만 남는다
INJECTION_CHECK = "injection_check"
# 한 실행의 Tool 호출 한도에 닿았다(tool_limit.py). SSE 로도 나가 채팅이 알림을 그린다
TOOL_LIMIT = "tool_limit"


def emit(kind: str, **fields: Any) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({CUSTOM_KEY: kind, **{k: v for k, v in fields.items() if v is not None}})


def parse(data: Any) -> tuple[str, dict[str, Any]] | None:
    """custom 스트림 조각이 우리 신호면 (종류, 나머지 필드) 를 돌려준다."""
    if not isinstance(data, dict) or not isinstance(data.get(CUSTOM_KEY), str):
        return None
    return data[CUSTOM_KEY], {k: v for k, v in data.items() if k != CUSTOM_KEY}
