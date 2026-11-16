"""가드레일 deny_pattern 의 모양 검사 (`daegam/regex_safety.py`)."""

from __future__ import annotations

import pytest

from daegam.regex_safety import MAX_REGEX_LENGTH, check_regex, has_nested_repetition


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+",
        r"(\w+\s?)+$",
        r"^(([a-z])+.)+[A-Z]([a-z])+$",
        r"(x*)*",
        r"(a{2,}){3,}",
        r"(?:[a-z]+)+",
        r"((a|b)+)*c",
        r"(a?){5}",
    ],
)
def test_nested_repetition_is_rejected(pattern):
    """(a+)+ 꼴은 맞지 않는 입력에서 되돌아가기가 지수로 늘어 채팅 한 줄로 에이전트 파드를
    세운다. Python re 에 시간 제한이 없으므로 저장 단계에서 모양을 거절한다."""
    with pytest.raises(ValueError, match="repeats a group"):
        check_regex(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"\b(?:ssn|password|비밀번호)\b",
        r"^\d{3}-\d{2}-\d{4}$",
        r"(?i)drop\s+table",
        r"(?:ab|a)+b",
        r"[a-z]+@[a-z]+\.[a-z]{2,}",
        r"(\d+)-(\d+)",
        r"\(\d+\)+",  # 이스케이프된 괄호는 그룹이 아니다
        r"[(+)]+",  # 문자 클래스 안의 괄호와 더하기는 문법이 아니다
        r"(?P<word>\w+) \1",
    ],
)
def test_ordinary_patterns_pass(pattern):
    check_regex(pattern)


def test_length_cap_and_syntax():
    with pytest.raises(ValueError, match="longer than"):
        check_regex("a" * (MAX_REGEX_LENGTH + 1))
    with pytest.raises(ValueError, match="invalid regex"):
        check_regex("(")


@pytest.mark.parametrize(
    ("pattern", "nested"),
    [
        ("[a-z]+", False),
        (r"\d+(x)", False),
        ("(?:a|b)*", False),
        ("[]a]+", False),  # 맨 앞의 ] 는 클래스의 글자다
        ("[^]]*", False),
        (r"[\]]+(b)", False),
        (r"(\[)+", False),  # 이스케이프된 [ 는 클래스를 열지 않는다
        ("([ab]+)*", True),
        (r"(\w+)+", True),
        ("(?:[^]x]+)+", True),
        (r"(a\d?)+", True),
    ],
)
def test_nested_repetition_scanner_finds_the_end_of_each_atom(pattern, nested):
    """`has_nested_repetition` 은 원자(이스케이프·문자 클래스·글자)의 끝을 찾은 뒤 붙은 반복을 한
    자리에서 본다. 원자마다 끝을 찾는 규칙을 이 표가 고정한다. 거절 문구는 위 테스트가 본다."""
    assert has_nested_repetition(pattern) is nested
