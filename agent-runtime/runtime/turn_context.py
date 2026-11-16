"""턴 문맥 읽기. 턴마다 그래프에 넘기는 문맥(`astream(context=TurnContext)`, long_term.py)의 칸을
도구와 미들웨어가 같은 방법으로 읽는다.
"""

from __future__ import annotations

from typing import Any


def context_field(runtime: Any, name: str) -> Any:
    """그래프의 runtime 에 실린 턴 문맥의 name 칸이다. 문맥 없이 부른 자리(테스트)는 None 이다."""
    return getattr(getattr(runtime, "context", None), name, None)
