"""모델 없이 플랫폼이 보는 확인 항목 (P45 11절).

실행기가 지켜 둔 스트림(턴마다 끝난 꼴, Tool 호출과 그 인자, 최종 답)으로 사례의 확인 항목을 본다.

- `expect_done`(기본으로 켜져 있다): 사례의 모든 턴이 `done` 으로 끝났다.
- `tool_called`: 그 이름의 Tool 이 불렸고, 주면 인자가 주어진 값을 담는다. 값마다 인자의 같은 키가
  같거나, 둘 다 글이면 인자의 글이 그 글을 품는다. Tool 이 실제로 돌았는지는 따지지 않는다. 가상
  결과를 받거나 거부된 쓰기도 모델이 부른 것이다.
- `tool_not_called`: 그 이름의 Tool 이 불리지 않았다.
- `answer_contains`·`answer_not_contains`: 최종 답이 그 글을 품는다(품지 않는다).
- `answer_matches`: 최종 답이 그 정규식에 맞는다. 사람이 쓴 식이라 Agent 의 `pii_mask` 식과 같은
  시간 한도의 `regex` 엔진으로 돌리고(`pii.PATTERN_TIMEOUT_SECONDS`), 한도를 넘거나 식을 읽지 못하면
  지나지 못한다.

모르는 종류(뒤 판이 더한 것)는 지나지 못한 것으로 센다. 사례는 `done` 밖의 확인 항목이 하나라도 있을
때만 배포 게이트에 센다(`counted`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import regex

from daegam.services.evaluation import pii
from daegam.store.evaluation import CaseCheck, CaseContent
from daegam.store.evaluation_runs import CheckVerdict, ToolCallRecord

DONE = "done"


@dataclass(frozen=True)
class Observed:
    """확인 항목이 보는 것. turns 는 턴마다 끝난 꼴(`done` 이나 `error:<code>`)이다."""

    turns: Sequence[str]
    calls: Sequence[ToolCallRecord]
    answer: str


def _arg_holds(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, str) and isinstance(wanted, str):
        return wanted in actual
    return bool(actual == wanted)


def _called(check: CaseCheck, calls: Sequence[ToolCallRecord]) -> bool:
    for call in calls:
        if call.name != check.tool:
            continue
        if all(k in call.args and _arg_holds(call.args[k], v) for k, v in check.args.items()):
            return True
    return False


def _matches(pattern: str, text: str) -> tuple[bool, str | None]:
    try:
        found = regex.search(pattern, text, timeout=pii.PATTERN_TIMEOUT_SECONDS)
    except TimeoutError:
        return False, "the pattern did not finish within its time limit"
    except regex.error:
        return False, "the pattern does not compile"
    return found is not None, None if found is not None else "the answer does not match"


def check_one(check: CaseCheck, seen: Observed) -> CheckVerdict:
    kind = check.kind
    text = check.text or ""
    if kind == "tool_called":
        ok = _called(check, seen.calls)
        gap = None if ok else f"{check.tool} was not called with the given arguments"
    elif kind == "tool_not_called":
        ok = not any(c.name == check.tool for c in seen.calls)
        gap = None if ok else f"{check.tool} was called"
    elif kind == "answer_contains":
        ok = text in seen.answer
        gap = None if ok else "the answer does not contain the text"
    elif kind == "answer_not_contains":
        ok = text not in seen.answer
        gap = None if ok else "the answer contains the text"
    elif kind == "answer_matches":
        ok, gap = _matches(text, seen.answer)
    else:
        ok, gap = False, f"unknown check {kind}"
    return CheckVerdict(kind=kind, passed=ok, gap=gap)


def check_all(content: CaseContent, seen: Observed) -> list[CheckVerdict]:
    """사례의 확인 항목마다의 판정이다. `expect_done` 이 켜져 있으면 맨 앞이다."""
    out: list[CheckVerdict] = []
    if content.expect_done:
        ended = [t for t in seen.turns if t != DONE]
        gap = None
        if not seen.turns:
            gap = "no turn ran"
        elif ended:
            gap = f"a turn ended with {ended[0]}"
        out.append(CheckVerdict(kind=DONE, passed=gap is None, gap=gap))
    out += [check_one(c, seen) for c in content.checks]
    return out


def counted(content: CaseContent) -> bool:
    """배포 게이트가 세는 사례인가. `done` 밖의 확인 항목이 있어야 한다 (11절)."""
    return bool(content.checks)


__all__ = ["DONE", "Observed", "check_all", "check_one", "counted"]
