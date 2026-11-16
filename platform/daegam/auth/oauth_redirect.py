"""OAuth 돌아갈 곳(redirect URI)의 규칙 (P33 5절).

돌아갈 곳은 loopback 만 받고 DCR 과 CIMD 가 같다. 스킴은 `http`, 호스트는 `127.0.0.1`·`[::1]`·
`localhost` 이며 셋은 서로 같게 보고 포트를 보지 않는다(RFC 8252 7.3). 스킴·경로·쿼리는 글자 그대로
같아야 하고 조각(`#`)이 있으면 거절한다. 코드가 인가 링크를 연 사람의 컴퓨터로만 가게 하려는 것이다.

urlsplit 을 쓰지 않고 글자로 가른다. urlsplit 은 탭·CR·LF 를 조용히 지우고 스킴을 소문자로 바꿔,
맞댄 값과 브라우저가 실제로 가는 값이 달라질 수 있기 때문이다(`daegam/urls.py` 와 같은 이유).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlencode

# 돌아갈 곳 하나의 길이 상한. DCR 등록과 인가 요청이 같은 값을 쓴다 (17절)
MAX_REDIRECT_URI_CHARS = 200
LOOPBACK_SCHEME = "http://"
# 호스트 자리가 loopback 셋 가운데 하나와 선택 포트뿐인 꼴. 사용자 정보(`@`)가 끼면 맞지 않는다
_LOOPBACK_NETLOC = re.compile(
    r"(?:127\.0\.0\.1|\[::1\]|localhost)(?::(?P<port>[0-9]{1,5}))?", re.ASCII | re.IGNORECASE
)
# 포트가 있으면 이 범위여야 한다. 밖의 값은 브라우저가 갈 수 없고 urlsplit 의 port 가 ValueError 다
MAX_PORT = 65535


def loopback_key(uri: str) -> str | None:
    """loopback 돌아갈 곳이면 포트를 뺀 맞대기 열쇠(경로와 쿼리 글자)를, 아니면 None 을 준다."""
    if len(uri) > MAX_REDIRECT_URI_CHARS or not uri.startswith(LOOPBACK_SCHEME) or "#" in uri:
        return None
    if not uri.isprintable() or any(c.isspace() for c in uri):
        return None
    rest = uri[len(LOOPBACK_SCHEME) :]
    cut = next((i for i, c in enumerate(rest) if c in "/?"), len(rest))
    netloc = _LOOPBACK_NETLOC.fullmatch(rest[:cut])
    if netloc is None or not _port_in_range(netloc["port"]):
        return None
    return rest[cut:]


def _port_in_range(raw: str | None) -> bool:
    return raw is None or 1 <= int(raw) <= MAX_PORT


def loopback_only(uris: Iterable[str]) -> list[str]:
    """loopback 돌아갈 곳만 처음 순서대로 남긴다. 겹친 값은 한 번이다."""
    return list(dict.fromkeys(u for u in uris if loopback_key(u) is not None))


def redirect_matches(registered: Iterable[str], requested: str) -> bool:
    """요청의 돌아갈 곳이 등록된 값 하나와 포트만 빼고 같은가."""
    key = loopback_key(requested)
    return key is not None and any(loopback_key(r) == key for r in registered)


def redirect_with(uri: str, params: Mapping[str, str | None]) -> str:
    """맞댄 돌아갈 곳에 인가 응답의 인자를 URL 인코딩해 붙인다. 값이 None 인 인자는 뺀다. 등록된
    값에 쿼리가 있으면 `&` 로 잇는다 (RFC 6749 3.1.2)."""
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"
