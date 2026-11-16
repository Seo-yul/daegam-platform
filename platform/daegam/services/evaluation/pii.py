"""평가 사례의 개인정보 찾기와 자리표 (P45 8절, F0.6).

찾는 것은 이메일 주소, 전화번호(`+82`, 지역번호와 그 괄호, 띄어쓰기와 점과 대시, 전각 숫자),
주민등록번호와 외국인등록번호(가린 꼴 포함), 카드 번호, 그리고 Agent 의 최신 리비전에 있는
`pii_mask` 가드레일의 `regex` 규칙이다. RFC 2606 의 예약 도메인(example.com 같은 것, `.test`·
`.example`·`.invalid`·`.localhost`) 아래의 주소는 남긴다. 대표번호(`1588-1234` 처럼 0 이나 +82 로
시작하지 않는 8자리)는 전화번호로 보지 않는다. 내장 패턴은 ASCII 글자로만 경계를 보므로 한글 조사가
붙은 주소("hong@corp.com으로")와 한글 바로 뒤의 주소도 찾는다.

Agent 의 정규식은 사람이 쓴 식이라 되돌아가기가 길어질 수 있다. 그래서 시간 한도가 있는 `regex`
엔진으로 칸마다 `PATTERN_TIMEOUT_SECONDS` 안에서만 돌리고, 넘거나 칸이 `PATTERN_MAX_CHARS` 보다 길면
그 칸 전부를 찾은 것으로 본다. 복사는 그 칸을 통째로 가리고, 복사본의 저장은 거절하며, 손으로 쓴
사례는 확인을 받는다.

복사할 때는 찾은 것을 `[phone]` 처럼 종류를 담은 자리표로 바꾸고(`replace`), 저장할 때는 사례의 모든
칸(질문, 참조 답, 채점 기준, 확인 항목, 가상 결과)에서 찾아 자리를 알린다(`scan_case`). 전각 숫자와
기호는 한 글자를 한 글자로 바꿔 읽으므로 찾은 자리가 원문의 자리와 같다.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import regex

from daegam.store.agents import Agent
from daegam.store.evaluation import CaseContent

EMAIL = "email"
PHONE = "phone"
ID_NUMBER = "id_number"
CARD_NUMBER = "card_number"
# Agent 의 `pii_mask` 가드레일이 정한 정규식에 맞은 것, 또는 그 식을 끝까지 돌리지 못한 칸
MASKED = "masked"

# Agent 정규식 한 번의 시간 한도와 그 식이 읽는 칸의 길이 상한. 길이 상한은 사례에서 가장 긴 칸인
# 참조 답의 상한이다
PATTERN_TIMEOUT_SECONDS = 0.25
PATTERN_MAX_CHARS = 20000

# 전각 숫자와 기호, 여러 대시를 반각으로. 한 글자가 한 글자라 자리가 그대로다
_WIDE = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
    | dict.fromkeys(("－", "‐", "‒", "–", "—", "−", "﹣"), "-")
    | {"＋": "+", "．": ".", "　": " ", "（": "(", "）": ")"}
)
# 번호 토막 사이. 띄어쓰기만, 점이나 대시 하나, 그 앞뒤의 띄어쓰기다
_SEP = r"(?: *[.\-] *| ?)"
# 앞뒤 경계는 ASCII 글자로만 본다. 한글 조사가 붙은 주소와 한글 바로 뒤의 주소를 놓치지 않는다
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![A-Za-z0-9-])"
)
# 앞뒤가 숫자가 아닌 자리라 긴 번호의 한 토막을 따로 잡지 않는다
_CARD = re.compile(
    r"(?<![0-9])(?:[0-9]{4}[ \-]?){3}[0-9]{4}(?![0-9])"
    r"|(?<![0-9])[0-9]{4}[ \-]?[0-9]{6}[ \-]?[0-9]{5}(?![0-9])"
)
# 생년월일 여섯 자리와 뒤 일곱 자리. 뒤 자리는 성별 자리 하나 뒤를 가렸을 수도, 다 가렸을 수도 있다
_ID = re.compile(r"(?<![0-9])[0-9]{6} *-? *(?:[1-8][0-9*xX●]{6}|[*xX●]{7})(?![0-9*xX●])")
# 휴대전화, 서울(02), 지역번호(0xx), 인터넷 전화(070). 0 이나 +82 로 시작하고 지역번호에 괄호를 쓸
# 수 있다
_PHONE = re.compile(
    rf"(?<![0-9+(])(?:\+82{_SEP}\(?0?|\(?0)(?:1[016789]|2|[3-6][1-5]|70)\)?{_SEP}"
    rf"[0-9]{{3,4}}{_SEP}[0-9]{{4}}(?![0-9])"
)
_RESERVED_SLD = ("example.com", "example.net", "example.org")
_RESERVED_TLD = ("test", "example", "invalid", "localhost")


@dataclass(frozen=True)
class Found:
    """찾은 것 하나. start·end 는 그 칸의 글자 자리다."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class Finding:
    """사례의 어느 칸(place)에서 찾았는가. place 는 `messages[0]`, `reference`,
    `checks[1].text`, `tool_results["Add"]` 꼴이다."""

    kind: str
    place: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _reserved(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    if domain.rsplit(".", 1)[-1] in _RESERVED_TLD:
        return True
    return any(domain == d or domain.endswith(f".{d}") for d in _RESERVED_SLD)


def agent_patterns(agent: Agent) -> list[regex.Pattern[str]]:
    """Agent 의 최신 리비전에 있는 `pii_mask` 가드레일의 정규식. 저장할 때 모양 검사를 지난
    식이다(`regex_safety`). 꼴이 맞지 않는 규칙과 읽지 못하는 식은 건너뛴다."""
    found: list[regex.Pattern[str]] = []
    for node in agent.current.graph.nodes:
        rules = node.data.get("rules") if node.type == "guardrail" else None
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, Mapping) or rule.get("kind") != "pii_mask":
                continue
            value = rule.get("value")
            pattern = value.get("regex") if isinstance(value, Mapping) else None
            if isinstance(pattern, str) and pattern:
                try:
                    found.append(regex.compile(pattern))
                except regex.error:
                    continue
    return found


def _agent_matches(text: str, patterns: list[regex.Pattern[str]]) -> list[tuple[int, int]]:
    """Agent 의 식이 맞은 자리. 칸이 `PATTERN_MAX_CHARS` 보다 길거나 한 식이 시간 한도를 넘으면 칸
    전부다."""
    whole = [(0, len(text))]
    if len(text) > PATTERN_MAX_CHARS:
        return whole
    spans: list[tuple[int, int]] = []
    for rx in patterns:
        try:
            found = list(rx.finditer(text, timeout=PATTERN_TIMEOUT_SECONDS))
        except TimeoutError:
            return whole
        spans += [m.span() for m in found if m.end() > m.start()]
    return spans


def find(text: str, extra: Iterable[regex.Pattern[str]] = ()) -> list[Found]:
    """겹치지 않는 찾은 것들. 이메일, 카드 번호, 등록번호, 전화번호, Agent 의 식 차례로 먼저 찾은
    것이 자리를 가진다. Agent 의 식이 칸 전부를 가리면 그 칸은 그것 하나다."""
    patterns = list(extra)
    agent = _agent_matches(text, patterns) if patterns and text else []
    if agent == [(0, len(text))]:
        return [Found(MASKED, 0, len(text))]
    plain = text.translate(_WIDE)
    taken: list[Found] = []

    def take(kind: str, start: int, end: int) -> None:
        if end > start and not any(start < f.end and f.start < end for f in taken):
            taken.append(Found(kind, start, end))

    for m in _EMAIL.finditer(plain):
        if not _reserved(m.group(1)):
            take(EMAIL, m.start(), m.end())
    for kind, rx in ((CARD_NUMBER, _CARD), (ID_NUMBER, _ID), (PHONE, _PHONE)):
        for m in rx.finditer(plain):
            take(kind, m.start(), m.end())
    for start, end in agent:
        take(MASKED, start, end)
    return sorted(taken, key=lambda f: f.start)


def replace(text: str, extra: Iterable[regex.Pattern[str]] = ()) -> tuple[str, Counter[str]]:
    """찾은 것을 `[종류]` 자리표로 바꾼 글과 종류마다 바꾼 수."""
    out: list[str] = []
    counts: Counter[str] = Counter()
    at = 0
    for f in find(text, extra):
        out += [text[at : f.start], f"[{f.kind}]"]
        counts[f.kind] += 1
        at = f.end
    out.append(text[at:])
    return "".join(out), counts


def _fields(content: CaseContent) -> Iterable[tuple[str, str]]:
    """사례의 글 칸과 그 자리 이름. 확인 항목의 Tool 이름과 인자(JSON 글), 가상 결과의 Tool 이름도
    든다."""
    for i, text in enumerate(content.messages):
        yield f"messages[{i}]", text
    if content.reference is not None:
        yield "reference", content.reference.text
    for i, text in enumerate(content.criteria):
        yield f"criteria[{i}]", text
    for i, check in enumerate(content.checks):
        if check.tool is not None:
            yield f"checks[{i}].tool", check.tool
        if check.text is not None:
            yield f"checks[{i}].text", check.text
        if check.args:
            yield f"checks[{i}].args", json.dumps(check.args, ensure_ascii=False, sort_keys=True)
    for i, (name, text) in enumerate(content.tool_results.items()):
        yield f"tool_results.names[{i}]", name
        yield f"tool_results[{json.dumps(name, ensure_ascii=False)}]", text


def scan_case(content: CaseContent, extra: Iterable[regex.Pattern[str]] = ()) -> list[Finding]:
    """사례의 모든 칸에서 찾은 것. 복사한 사례는 하나라도 있으면 저장하지 않고, 손으로 쓴 사례는
    확인을 받고 저장한다 (8절)."""
    patterns = list(extra)
    return [
        Finding(f.kind, place, f.start, f.end)
        for place, text in _fields(content)
        for f in find(text, patterns)
    ]
