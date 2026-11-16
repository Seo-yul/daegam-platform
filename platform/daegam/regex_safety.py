"""가드레일 정규식의 모양 검사 (D39). 저장 단계에서 길이, 문법, 겹친 반복을 본다.

실패는 `ValueError` 이고 pydantic 검증기 안에서 부르므로 그 문장이 이슈 메시지가 된다. 화면의 같은
검사(`ui/src/lib/agentGraph.ts` 의 `regexError`)가 문장과 순서를 그대로 따른다.
"""

from __future__ import annotations

import re

MAX_REGEX_LENGTH = 512
_QUANTIFIER = re.compile(r"(?:[*+?]|\{\d+(?:,\d*)?\})[?+]?")


def has_nested_repetition(pattern: str) -> bool:
    """반복되는 그룹 안에 또 반복이 있으면 (a+)+ 꼴이다. 맞지 않는 입력에서 되돌아가기가 지수로
    늘어 채팅 한 줄로 에이전트 파드를 세운다. 정규식은 프로필 작성자가 쓰고 입력은 대화 상대가
    주므로 둘이 만나면 그렇다. Python re 에는 시간 제한이 없어 저장 단계에서 모양을 거절한다.

    괄호를 스택으로 따라가며 각 그룹 안에 반복이 있는지 기억하고, 그룹이 닫힌 뒤 반복이 붙으면
    안에 반복이 있던 그룹인지 본다. 이스케이프와 문자 클래스는 문법이 아니라 건너뛴다. 겹치는
    대안 `(a|ab)+` 같은 꼴은 잡지 못한다. 완전한 판정이 아니라 흔한 모양을 막는 한 겹이다."""
    stack: list[bool] = []
    i = 0
    n = len(pattern)

    def quantifier_after(pos: int) -> int:
        m = _QUANTIFIER.match(pattern, pos)
        return len(m.group(0)) if m else 0

    while i < n:
        c = pattern[i]
        if c == "(":
            stack.append(False)
            i += 1
            continue
        if c == ")":
            inner = stack.pop() if stack else False
            i += 1
            q = quantifier_after(i)
            if q and inner:
                return True
            if stack and (q or inner):
                stack[-1] = True
            i += q
            continue
        # 원자 하나(이스케이프, 문자 클래스, 글자)의 끝으로 간 뒤 반복이 붙었으면 감싼 그룹에 적는다
        if c == "\\":
            i += 2
        elif c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
        else:
            i += 1
        q = quantifier_after(i)
        if q and stack:
            stack[-1] = True
        i += q
    return False


def check_regex(pattern: str) -> None:
    """길이 상한, 문법, 겹친 반복 순으로 보고 걸리면 `ValueError` 다."""
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ValueError(f"regex is longer than {MAX_REGEX_LENGTH} characters")
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid regex: {e}") from e
    if has_nested_repetition(pattern):
        raise ValueError(
            "regex repeats a group that itself contains a repetition, e.g. (a+)+; "
            "this backtracks exponentially on non-matching input. Rewrite it without nesting"
        )
