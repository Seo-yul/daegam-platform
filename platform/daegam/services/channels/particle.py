"""한국어 조사 고르기. `ui/src/lib/particle.ts` `particle` 의 Python 짝이다 (P47 6.9절).

봇의 글은 플랫폼이 짓는 서버 글이라(D67) 화면의 사전이 아니라 `texts.py` 에 있고, 자리표시자 뒤의
조사를 이것이 고른다. 규칙은 화면과 같다. 앞말의 끝소리에 받침이 있으면 앞 것(을·이·은·과·으로),
없으면 뒤 것(를·가·는·와·로)이다. 한글은 끝 음절의 종성, 숫자와 영문은 읽는 소리로 정한다. 숫자는
영·일·삼·육·칠·팔, 영문 끝 글자는 엘·엠·엔·알만 받침이 있고, 끝의 영문 낱말이 두 글자 이상이며
소문자 er·or 로 끝나면 단어로 읽어 모음으로 끝난다. "으로/로" 는 ㄹ 받침 뒤에서 "로" 다. 두 구현이
같은 낱말에 같은 답을 내는지는 `tests/test_channel_texts.py` 가 화면의 시험 사례로 본다.
"""

from __future__ import annotations

import re

_DIGIT_WITH_FINAL = frozenset("013678")
_LATIN_WITH_FINAL = frozenset("lmnr")
_DIGIT_WITH_RIEUL = frozenset("178")
_LATIN_WITH_RIEUL = frozenset("lr")
_HANGUL_RIEUL = 8
_TRAILING = re.compile(r"""[\s"'`)\]}>.,!?]+$""")
_LAST_LATIN_WORD = re.compile(r"[A-Za-z]+$")
_HANGUL_FIRST, _HANGUL_LAST = 0xAC00, 0xD7A3

PAIRS: dict[str, tuple[str, str]] = {
    "을/를": ("을", "를"),
    "이/가": ("이", "가"),
    "은/는": ("은", "는"),
    "과/와": ("과", "와"),
    "으로/로": ("으로", "로"),
}


def _trimmed(word: str) -> str:
    """끝의 공백·따옴표·괄호를 뗀 앞말."""
    return _TRAILING.sub("", word)


def _reads_as_vowel_word(word: str) -> bool:
    """끝의 영문 낱말이 소문자 er·or 로 끝나 단어로 읽으면 모음으로 끝나는가."""
    found = _LAST_LATIN_WORD.search(_trimmed(word))
    last = found.group(0) if found else ""
    return len(last) >= 2 and last.endswith(("er", "or"))


def _final(word: str) -> tuple[bool, bool]:
    """끝소리에 받침이 있는가, 그 받침이 ㄹ 인가. 판단할 글자가 없으면 둘 다 False 다."""
    trimmed = _trimmed(word)
    if not trimmed:
        return False, False
    last = trimmed[-1]
    code = ord(last)
    if _HANGUL_FIRST <= code <= _HANGUL_LAST:
        jong = (code - _HANGUL_FIRST) % 28
        return jong != 0, jong == _HANGUL_RIEUL
    if last.isascii() and last.isdigit():
        return last in _DIGIT_WITH_FINAL, last in _DIGIT_WITH_RIEUL
    if _reads_as_vowel_word(word):
        return False, False
    low = last.lower()
    return low in _LATIN_WITH_FINAL, low in _LATIN_WITH_RIEUL


def has_final_consonant(word: str) -> bool:
    """끝소리에 받침이 있는가."""
    return _final(word)[0]


def particle(word: str, pair: str) -> str:
    """앞말에 맞는 조사 하나. 앞말은 붙이지 않는다. 모르는 짝이면 KeyError 다."""
    with_final, without = PAIRS[pair]
    final, rieul = _final(word)
    if pair == "으로/로" and rieul:
        return without
    return with_final if final else without


__all__ = ["PAIRS", "has_final_consonant", "particle"]
