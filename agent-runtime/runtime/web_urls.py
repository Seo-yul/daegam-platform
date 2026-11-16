"""웹 가드가 보는 URL 의 규칙이다(P39b 14절 2부). 표 시험은 `tests/test_web_guard.py` 가 든다.

- 뽑기는 `https?://` 부터 공백, `<>"'` 나 역따옴표 앞까지이고, 그것을 ASCII 밖의 첫 글자에서 자른
  것도 낸다. 끝의 `.,;:!?` 와 짝 없는 닫는 괄호는 뗀다.
- 맞추기는 scheme·호스트 소문자, 호스트는 IDNA 2008 A-label, 기본 포트와 조각을 빼고 빈 경로는 `/`,
  퍼센트 인코딩은 대문자, 예약하지 않은 글자는 풀고 ASCII 밖의 글자는 UTF-8 로 인코딩한다(RFC 3986
  6.2.2).
- 도메인은 레이블 경계로 맞춘다. 플랫폼 `services/domains.py` 와 같은 규칙이다(파리티 시험이 맞대
  본다).
- 웹 결과의 출처 URL 은 구조화 결과와 글 속 링크 가운데 모델이 받은 글에 그대로 있는 것이다.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import idna
from langchain_core.messages import ToolMessage

MAX_WEB_URLS = 2000
"""`web_urls` 가 들고 있는 URL 의 수다. 넘으면 오래된 것부터 버린다."""

_URL_RUN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?"
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _trim(candidate: str) -> str:
    """끝의 문장 부호와 짝 없는 닫는 괄호를 뗀다."""
    while candidate:
        last = candidate[-1]
        if last in _TRAILING:
            candidate = candidate[:-1]
        elif last in _CLOSERS and candidate.count(last) > candidate.count(_CLOSERS[last]):
            candidate = candidate[:-1]
        else:
            break
    return candidate


def _ascii_prefix(text: str) -> str:
    """ASCII 밖의 첫 글자 앞까지다. `https://a.com에서` 같이 조사가 붙은 URL 을 위한 것이다."""
    for index, char in enumerate(text):
        if ord(char) > 127:
            return text[:index]
    return text


def extract_urls(text: str) -> list[str]:
    """글에 나온 http(s) URL 이다. `https?://` 부터 공백, `<>"'` 나 역따옴표 앞까지이고, 그것을
    ASCII
    밖의 첫 글자에서 자른 것도 낸다. 끝의 `.,;:!?` 와 짝 없는 닫는 괄호는 뗀다."""
    out: list[str] = []
    for match in _URL_RUN.finditer(text):
        raw = match.group(0)
        for candidate in (raw, _ascii_prefix(raw)):
            trimmed = _trim(candidate)
            if trimmed and trimmed not in out:
                out.append(trimmed)
    return out


def _part(text: str) -> str:
    """경로와 질의 문자열을 RFC 3986 6.2.2 로 맞춘다. ASCII 밖의 글자는 UTF-8 퍼센트 인코딩, 퍼센트
    인코딩은 대문자 16진, 예약하지 않은 글자는 푼다. 나머지는 적힌 그대로다."""
    encoded = "".join(char if 32 < ord(char) < 127 else quote(char, safe="") for char in text)

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    return _PERCENT.sub(fix, encoded)


def a_label(host: str) -> str | None:
    """호스트의 소문자 A-label 이다(IDNA 2008, UTS 46 대응). 바꿀 수 없으면 None 이다. UTS 46 이
    전각·반각 마침표를 점으로 바꾸므로 끝의 점은 바꾼 뒤에도 뗀다."""
    host = host.rstrip(".").lower()
    if not host:
        return None
    if host.isascii():
        return host
    try:
        label = idna.encode(host, uts46=True).decode("ascii").rstrip(".")
    except idna.IDNAError:
        return None
    return label or None


def normalize_url(raw: str) -> str | None:
    """비교할 URL 이다. scheme·호스트는 소문자, 호스트는 A-label, 기본 포트와 조각은 빼고 빈 경로는
    `/` 다. http(s) 가 아니거나 호스트가 없거나 포트가 틀리면 None 이다."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    hostname = parts.hostname
    host = f"[{hostname}]" if ":" in hostname else a_label(hostname)
    if host is None:
        return None
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{userinfo}@{host}" if userinfo else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, _part(parts.path) or "/", _part(parts.query), ""))


def host_of(url: str) -> str:
    """normalize_url 이 낸 URL 의 호스트다."""
    return urlsplit(url).hostname or ""


def domain_matches(host: str, domains: Sequence[str]) -> bool:
    """호스트가 목록의 도메인이거나 그 아래인가. 레이블 경계로만 맞춘다."""
    return any(host == d or host.endswith("." + d) for d in domains)


def add_urls(left: list[str] | None, right: list[str] | None) -> list[str]:
    """두 URL 목록을 차례를 지켜 겹침 없이 잇는다. 서브에이전트가 받은 목록을 통째로 돌려줘도
    같다."""
    merged = list(left or [])
    seen = set(merged)
    for url in right or []:
        if url not in seen:
            merged.append(url)
            seen.add(url)
    return merged


def add_recent_urls(left: list[str] | None, right: list[str] | None) -> list[str]:
    """add_urls 에서 오래된 것부터 MAX_WEB_URLS 넘는 것을 버린다."""
    return add_urls(left, right)[-MAX_WEB_URLS:]


def structured_content(artifact: Any) -> Mapping[str, Any]:
    """MCP 결과 artifact 의 structuredContent 다(langchain.mcp `MCPToolArtifact`). 없으면 빈
    것이다."""
    if isinstance(artifact, Mapping):
        value = artifact.get("structured_content")
        if isinstance(value, Mapping):
            return value
    return {}


def result_urls(message: ToolMessage) -> list[str]:
    """모델이 받은 웹 결과에서 출처가 될 URL 이다. 구조화 결과의 url·final_url·redirect_to 와 검색
    결과의 url, 그리고 글 속 링크다. 글에 그대로 있는 것만 친다."""
    text = message.text
    data = structured_content(message.artifact)
    candidates = [data.get(key) for key in ("url", "final_url", "redirect_to")]
    results = data.get("results")
    if isinstance(results, list):
        candidates.extend(r.get("url") for r in results if isinstance(r, Mapping))
    raws = [c for c in candidates if isinstance(c, str) and c in text]
    raws.extend(extract_urls(text))
    return add_urls([], [u for raw in raws if (u := normalize_url(raw)) is not None])
