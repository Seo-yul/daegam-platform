"""도메인 목록의 이름 규칙과 Public Suffix List (P39b 14·15절).

허용·차단 도메인 목록과 관리자가 막은 이름 접미사는 소문자 DNS 글자이고, IDNA 2008 A-label 로
적는다. 글자 규칙과 길이는 agent.json 계약의 `web_domains` 에서 읽는다. tools 노드의 목록은 라벨이
둘 이상이어야 한다(관리자의 막는 접미사는 `internal` 같은 한 라벨도 된다). 마지막 라벨이 숫자뿐인
값(IP 주소 모양)은 어느 목록에도 올릴 수 없다. 공개 접미사(`com`, `co.kr`, `github.io` 같은 것)는
누구나 그 아래 사이트와 요청 기록을 가질 수 있으므로 목록에 올릴 수 없다. 판정은 플랫폼 이미지에
묶은 Public Suffix List 사본 (`daegam/data/public_suffix_list.dat`, MPL 2.0)으로 하고, 실행 중에
받아 오지 않는다. ICANN 구역과 사설 구역을 함께 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import idna

from daegam.contracts import agent_schema

PSL_FILE = Path(__file__).resolve().parents[1] / "data" / "public_suffix_list.dat"
_ITEM = agent_schema()["$defs"]["web_domains"]["items"]
DOMAIN_RE = re.compile(_ITEM["pattern"])
DOMAIN_MAX: int = _ITEM["maxLength"]
# tools 노드의 목록 하나에 둘 수 있는 값의 수. 컴파일한 막는 목록은 관리자의 접미사(100 개까지)를
# 더하므로 계약의 maxItems 가 그 둘의 합이다
NODE_DOMAINS_MAX = 100


class DomainError(ValueError):
    """도메인 목록의 값이 규칙에 맞지 않는다. code 는 invalid_value 나 domain_is_public_suffix
    다."""

    def __init__(self, code: str, value: str) -> None:
        super().__init__(f"{code}: {value!r}")
        self.code = code
        self.value = value


def a_label(value: str) -> str:
    """소문자 A-label. 끝의 점은 뗀다. UTS 46 이 전각·반각 마침표를 점으로 바꾸므로 바꾼 뒤에도
    뗀다. 바꿀 수 없으면 invalid_value 다."""
    text = value.strip().rstrip(".").lower()
    try:
        return idna.encode(text, uts46=True).decode("ascii").rstrip(".") if text else ""
    except idna.IDNAError as e:
        raise DomainError("invalid_value", value) from e


@dataclass(frozen=True)
class SuffixRules:
    rules: frozenset[str]
    wildcards: frozenset[str]
    exceptions: frozenset[str]


@cache
def suffix_rules() -> SuffixRules:
    rules: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    for raw in PSL_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        line = line.split()[0]
        target = rules
        if line.startswith("!"):
            target, line = exceptions, line[1:]
        elif line.startswith("*."):
            target, line = wildcards, line[2:]
        try:
            target.add(a_label(line))
        except DomainError:
            continue
    return SuffixRules(frozenset(rules), frozenset(wildcards), frozenset(exceptions))


def is_public_suffix(domain: str) -> bool:
    """domain 자체가 공개 접미사인가. `*.ck` 같은 와일드카드 규칙의 한 칸 아래도 공개 접미사다
    (`!www.ck` 같은 예외는 빼고)."""
    rules = suffix_rules()
    if domain in rules.exceptions:
        return False
    if domain in rules.rules:
        return True
    parent = domain.partition(".")[2]
    return bool(parent) and parent in rules.wildcards


def check_domain(value: str, *, suffix: bool = False) -> str:
    """목록에 올릴 도메인 하나. A-label 로 바꾸고 이름 규칙과 공개 접미사를 본다. suffix 는
    관리자의 막는 접미사라 한 라벨도 된다."""
    domain = a_label(value)
    if not domain or len(domain) > DOMAIN_MAX or not DOMAIN_RE.fullmatch(domain):
        raise DomainError("invalid_value", value)
    labels = domain.split(".")
    if labels[-1].isdigit() or (len(labels) < 2 and not suffix):
        raise DomainError("invalid_value", value)
    if is_public_suffix(domain):
        raise DomainError("domain_is_public_suffix", value)
    return domain


def check_domains(values: list[str], *, suffix: bool = False) -> list[str]:
    """목록 전체. 중복은 한 번만 남긴다."""
    out: list[str] = []
    for value in values:
        domain = check_domain(value, suffix=suffix)
        if domain not in out:
            out.append(domain)
    return out
