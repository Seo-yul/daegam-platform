"""URL 판정 (코어). 라우터·서비스·설정이 같은 규칙으로 http(s) 주소를 가른다.

클러스터 API 는 정규식으로, Backend API 는 urlsplit 으로 따로 보고 있었다. 정규식은 끝을 고정하지
않아 공백이 든 값도 통과시켰다. 오류 코드와 문구는 부르는 쪽마다 달라서 여기는 참·거짓만 준다.
origin 판정은 CSP(security_headers)와 공개 주소 설정(config)이 함께 쓴다. 허브 항목 주소의
갈래(`outside_address`)는 클라이언트 설정 내보내기가 쓴다.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlsplit

import idna

# 공개 URL fetch 의 주소 규칙은 daegam 을 import 하지 않는 코어 모듈이다(웹 Tool 이 바이트 사본을
# 들고 간다). 차단 표는 그곳 하나에 있고 여기는 그 표를 읽는다
from daegam.urlfetch import (
    BLOCKED_HOSTS,
    BLOCKED_SUFFIXES,
    BlockedURL,
    a_label,
    is_public_address,
)

# 인증 모드와 발급자·공개 주소 값의 앞뒤에서 지우는 공백. ASCII 공백 여섯(C 로캘의 [:space:])뿐이다.
# str.strip() 은 NBSP 같은 유니코드 공백까지 지워, 같은 키를 읽는 UI 컨테이너의 셸
# (ui/docker/daegam-csp.sh)과 판단이 갈린다(D52)
ENV_WHITESPACE = " \t\n\r\x0b\x0c"

# 모든 API 라우터의 접두사(D52). 렌더된 CR 이 플랫폼 API 주소를 담아야 해서 k8s 계층도 읽으므로
# 코어인 여기 둔다
API_PREFIX = "/api"
# 런타임 이벤트 수집 경로 (P37 3절). 라우트와 Agent 렌더의 `DAEGAM_EVENTS_URL` 이 같은 값을 쓴다
TELEMETRY_EVENTS_PATH = "/telemetry/events"
# 게이트웨이 사용량 수집의 OTLP 경로 (P37 8절). 내보내기 주소 `{platform}/api/telemetry` 에 OTLP 의
# 관례대로 `/v1/traces`·`/v1/logs` 가 붙는다
TELEMETRY_OTLP_PATHS = {"traces": "/telemetry/v1/traces", "logs": "/telemetry/v1/logs"}
# 기계가 부르는 수집 경로 모두다. 감사 미들웨어가 빼고 본문 상한이 따로 건다
TELEMETRY_PATHS = (TELEMETRY_EVENTS_PATH, *TELEMETRY_OTLP_PATHS.values())


def exact_api_paths(paths: Iterable[str]) -> re.Pattern[str]:
    """API 접두사를 붙인 paths 와 통째로만 맞는 정규식이다. 같은 끝을 가진 다른 경로는 맞지
    않는다."""
    return re.compile(rf"^{re.escape(API_PREFIX)}({'|'.join(map(re.escape, paths))})$")


# 턴 위임의 기계 경로 (P53 4절). 런타임이 인스턴스 토큰과 위임의 비밀로 위임을 갱신하고 끝낸다.
# 위임이 런타임에 주는 `renew_url` 이 `TURN_GRANT_PATH` 이고 그 아래에 둘이 붙는다
TURN_GRANT_PATH = "/agents/{agent_id}/turn-grants/{grant_id}"
TURN_GRANT_ROUTES = {"renew": f"{TURN_GRANT_PATH}/renew", "end": f"{TURN_GRANT_PATH}/end"}
# 두 경로와 통째로만 맞는 정규식이다. 감사 미들웨어가 빼고 본문 상한이 따로 건다
TURN_GRANT_PATHS = re.compile(
    rf"^{re.escape(API_PREFIX)}/agents/[^/]+/turn-grants/[^/]+/(renew|end)$"
)


# 코드 실행 샌드박스의 기계 경로 (P40 5절). 런타임이 인스턴스 토큰으로 세션의 샌드박스를 열고 찾고
# 쓰기를 알리고 닫는다. 감사 미들웨어가 빼고 본문 상한이 따로 건다
SANDBOX_PATH = "/agents/{agent_id}/sandboxes"
SANDBOX_ROUTES = {
    "open": SANDBOX_PATH,
    "touch": f"{SANDBOX_PATH}/touch",
    "close": f"{SANDBOX_PATH}/close",
}
SANDBOX_PATHS = re.compile(rf"^{re.escape(API_PREFIX)}/agents/[^/]+/sandboxes(/touch|/close)?$")


# 다른 Agent 를 Tool 로 부르는 기계 경로 (P46 6.3절). 부르는 Agent 의 런타임이 호출받는 Agent 의
# id 로 실행을 시작하고, 그 아래의 runs/{run_id} 와 runs/{run_id}/cancel 로 묻고 취소한다. 컴파일이
# 이 경로를 agent.json 의 Agent 소스에 싣는다
AGENT_CALL_PATH = "/internal/agent-calls/{agent_id}"
AGENT_CALL_ROUTES = {
    "start": AGENT_CALL_PATH,
    "poll": f"{AGENT_CALL_PATH}/runs/{{run_id}}",
    "cancel": f"{AGENT_CALL_PATH}/runs/{{run_id}}/cancel",
}
# 세 경로와 통째로만 맞는 정규식이다. 감사 미들웨어가 빼고(경로가 스스로 감사 행을 쓴다) 본문
# 상한이 따로 건다
AGENT_CALL_PATHS = re.compile(
    rf"^{re.escape(API_PREFIX)}/internal/agent-calls/[^/]+(/runs/[^/]+(/cancel)?)?$"
)


# 다른 Agent 에게 맡긴 작업의 기계 경로 (P46 6.7절). Agent Protocol 의 일부이고, threads.create 의
# 본문이 비어 있어 경로가 호출받는 Agent 를 댄다. 부르는 Agent 의 런타임이 thread 를 만들고 그
# 아래에서 실행을 시작하고 읽고 취소한다
AGENT_PROTOCOL_PATH = "/internal/agent-protocol/{agent_id}"
_THREAD_PATH = f"{AGENT_PROTOCOL_PATH}/threads/{{thread_id}}"
AGENT_PROTOCOL_ROUTES = {
    "create_thread": f"{AGENT_PROTOCOL_PATH}/threads",
    "get_thread": _THREAD_PATH,
    "create_run": f"{_THREAD_PATH}/runs",
    "get_run": f"{_THREAD_PATH}/runs/{{run_id}}",
    "cancel_run": f"{_THREAD_PATH}/runs/{{run_id}}/cancel",
}
# 다섯 경로와 통째로만 맞는 정규식이다. 호출 경로처럼 감사 미들웨어가 빼고 본문 상한이 따로 건다
AGENT_PROTOCOL_PATHS = re.compile(
    rf"^{re.escape(API_PREFIX)}/internal/agent-protocol/[^/]+/threads"
    r"(/[^/]+(/runs(/[^/]+(/cancel)?)?)?)?$"
)


# 웹훅 트리거의 공개 경로 (P41 4절). 다른 시스템이 Bearer 없이 서명으로 부른다. 감사 미들웨어가
# 빼고(경로가 스스로 감사 행을 쓴다) 본문 상한은 경로가 읽으며 센다
HOOK_ROUTE = "/hooks/{trigger_id}"
HOOK_PATHS = re.compile(rf"^{re.escape(API_PREFIX)}/hooks/[^/]+$")


def hook_path(trigger_id: str) -> str:
    """트리거의 호출 경로. 화면이 공개 주소 앞에 붙인다."""
    return API_PREFIX + HOOK_ROUTE.format(trigger_id=trigger_id)


# Teams 봇의 메시지 엔드포인트 (P47 6.1절). Microsoft 의 Bot Connector 가 Bearer 토큰으로 부른다.
# 감사 미들웨어가 빼고(경로가 스스로 감사 행을 쓴다) 본문 상한은 경로가 읽으며 센다
TEAMS_MESSAGES_ROUTE = "/channels/teams/messages"
TEAMS_MESSAGES_PATH = API_PREFIX + TEAMS_MESSAGES_ROUTE
TEAMS_MESSAGES_PATHS = exact_api_paths([TEAMS_MESSAGES_ROUTE])


# 어떤 주소로도 가지 않는 호스트 이름은 공개 URL fetch 의 `BLOCKED_HOSTS` 그대로다
# 클러스터 안의 이름. 보내는 쪽 주소(OTLP 내보내기)만 이 호스트로 평문 http 를 받는다
IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")


def host_of(hostname: str) -> str:
    """검사와 오류 글이 쓰는 호스트 이름. 소문자이고 끝의 점을 뗀다(완전한 이름도 같은 호스트다)."""
    return hostname.lower().rstrip(".")


# 호스트의 마지막 라벨이 숫자나 16진 수다. 정식 IPv4 가 아닌데 이렇게 끝나면 `127.1`·`2130706433`·
# `0x7f000001` 같은 줄임 꼴이다. 클라이언트가 루프백으로 읽을 수 있어 받지 않는다. 최상위 도메인은
# 숫자로만 되지 않는다
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def export_url_problem(url: str) -> str | None:
    """플랫폼이 보내는 쪽 주소(OTLP 내보내기)를 저장할 때의 검사다. 맞으면 None 이고 아니면 그
    까닭이다.

    https 만 받고, 클러스터 안의 `*.svc`·`*.svc.cluster.local` 은 평문 http 도 받는다. 루프백,
    링크로컬(메타데이터 169.254.169.254), 메타데이터 이름은 https 여도 거절한다. 사용자 정보와 쿼리,
    프래그먼트, 틀린 포트, 숫자 호스트의 줄임 꼴은 받지 않는다. 이름은 풀지 않는다. 클러스터 안의
    이름과 사내 수집기는 저장할 때 풀리지 않을 수 있고, 보낼 때의 DNS 는 저장 때와 다를 수 있다."""
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
    except ValueError:
        return "the URL cannot be parsed"
    if not hostname:
        return "the URL has no host"
    if parts.username or parts.password:
        return "the URL must not carry credentials"
    if parts.query or parts.fragment:
        return "the URL must not carry a query or fragment"
    try:
        port = parts.port
    except ValueError:
        return "the URL has an invalid port"
    if port == 0:
        return "the URL has an invalid port"
    host = host_of(hostname)
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]) and not _ipv4(host):
        return f"host {host!r} is a numeric shorthand, not a full address"
    if parts.scheme == "http" and host.endswith(IN_CLUSTER_SUFFIXES):
        return None
    if parts.scheme != "https":
        return "only https, or plain http to an in-cluster *.svc host"
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        return f"host {host!r} is not allowed"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return f"address {host} is loopback or link-local"
    return None


def _ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


# 플랫폼 web_search 의 MCP 엔드포인트 (P39b 12절). 허브 항목 `web-search` 의 주소가 이 경로다
WEB_SEARCH_MCP_PATH = f"{API_PREFIX}/internal/mcp/web-search"

# origin 은 CSP host-part 문법의 허용 목록이다(D52). scheme 은 http·https, 호스트는 [A-Za-z0-9-]
# 라벨을 . 으로 이은 것, 포트는 1~5자리 숫자다. 사용자 정보·공백·줄바꿈·따옴표·세미콜론·$·*·쉼표·%
# 가 든 값은 origin 이 아니다. 세미콜론과 쉼표는 CSP 지시어와 정책을 새로 열고 * 는 모든 호스트를
# 연다. urlsplit 을 쓰지 않는 것은 그것이 탭·CR·LF 를 조용히 지우고 netloc 에 무엇이든 받기
# 때문이다. IPv6 literal 은 CSP 문법에 자리가 없다. 같은 규칙이 ui/docker/daegam-csp.sh 에 있고
# tests/test_ui_csp_parity.py 가 둘을 맞댄다
_ORIGIN = re.compile(
    r"(https?)://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)(:[0-9]{1,5})?(?:[/?#].*)?",
    re.ASCII | re.IGNORECASE | re.DOTALL,
)


def is_http_url(value: str) -> bool:
    """scheme 이 http·https 이고 호스트가 있으며 공백이 없는 주소인가."""
    if any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:  # 닫히지 않은 IPv6 괄호 같은 것
        return False
    return parts.scheme in ("http", "https") and bool(host)


def origin_of(url: str | None) -> str | None:
    """`https://idp.example.com/realms/x` 의 `https://idp.example.com`.

    앞뒤 ASCII 공백(ENV_WHITESPACE)을 지운 값이 위 꼴이 아니면 None 이다. scheme 은 소문자로
    맞춘다."""
    if not url:
        return None
    m = _ORIGIN.fullmatch(url.strip(ENV_WHITESPACE))
    if m is None:
        return None
    scheme, host, port = m.groups()
    return f"{scheme.lower()}://{host}{port or ''}"


DEFAULT_PORTS = {"http": 80, "https": 443}


def url_parts(url: str) -> tuple[str, str, int, list[str]] | None:
    """(scheme, 소문자 호스트, 포트, 경로 조각). 포트가 없으면 scheme 의 기본 포트다. http(s)
    주소가 아니거나 포트가 틀렸으면 None 이다."""
    if not is_http_url(url):
        return None
    parts = urlsplit(url)
    try:
        port = parts.port or DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    return parts.scheme, (parts.hostname or "").lower(), port, segments


def under_base(url: str | None, base: str | None) -> bool:
    """url 이 base 아래인가 (P37 18절). scheme·호스트·포트가 같고 경로가 조각 경계에서 base 의
    경로로 시작할 때만 참이다. 글자 앞머리로 보면 `https://gw.example.com` 이
    `https://gw.example.com.evil.io` 를 삼켜 그 provider 의 호출을 게이트웨이 경로로 셀 것이다."""
    if not url or not base:
        return False
    got, want = url_parts(url), url_parts(base)
    if got is None or want is None:
        return False
    return got[:3] == want[:3] and got[3][: len(want[3])] == want[3]


# 허브 항목 주소의 갈래 (P55a 1절). outside 는 클러스터 밖에서 닿는 이름이나 공인 literal 이다.
# private 는 사설망 주소로, 사람이 고를 때만 클라이언트 설정에 들어간다. local 은 내보내지 않는
# 주소이고 credentials 는 자격 증명이 들어 있을 수 있는 주소다
AddressClass = Literal["outside", "private", "local", "credentials"]
EXPORTED_CLASSES: frozenset[str] = frozenset({"outside", "private"})

# 이름 접미사의 갈래. 공개 URL fetch 의 `BLOCKED_SUFFIXES` 를 둘로 나눈다. VPN 너머에서 흔히 닿는
# 둘이 private 이고 나머지가 local 이다. local 쪽을 먼저 보므로 `.cluster.local` 이 `.local` 보다
# 앞선다
PRIVATE_SUFFIXES = (".internal", ".local")
LOCAL_SUFFIXES = tuple(s for s in BLOCKED_SUFFIXES if s not in PRIVATE_SUFFIXES)
# local 로 가르는 이름. 공개 URL fetch 의 차단 이름에 접미사 표가 잡지 못하는 둘을 더한다.
# `cluster.local` 은 `.cluster.local` 에 걸리지 않고 `.local` 로 private 이 되며,
# `localhost.localdomain` 은 루프백의 흔한 별칭이다
LOCAL_NAMES = BLOCKED_HOSTS | {"cluster.local", "localhost.localdomain"}
# 사설망 literal (P55a). RFC 1918 과 CGNAT 다. VPN 너머의 사내 서버가 흔히 이
# 주소에 있다
PRIVATE_V4: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
# authority 의 끝. RFC 3986 의 구분자다. WHATWG 가 끝으로 읽는 `\` 는 authority 안에 남겨 거절한다
_AUTHORITY_END = re.compile(r"[/?#]")


def outside_address(url: str) -> AddressClass:
    """등록한 주소가 클러스터 밖에서 닿는 주소인가 (P55a 1절).

    클라이언트가 받는 것은 등록한 문자열 그대로라 갈래도 그 문자열을 말한다. 그래서 먼저 등록한
    문자열을 본다. 공백과 제어 글자가 든 값은 주소가 아니라 local 이다. 어디든 `$` 가 있으면
    credentials 다. Claude Code 는 `url` 의 `${VAR}` 를 펼치므로 그런 주소를 그대로 내보내면 그
    사람의 env 값(토큰)이 남의 서버로 간다. authority 에 `\\` 가 있으면 local 이다. WHATWG 파서는
    그 자리에서 authority 를 끝내므로 Python 과 클라이언트가 다른 호스트를 읽는다. `@` 가 있으면
    credentials, `%` 가 있으면 local, 쿼리가 있으면 credentials 다.

    그다음 호스트를 A-label 로 바꾸고(`a_label`) `origin_of` 문법을 대고, 숫자 호스트의 줄임 꼴을
    `export_url_problem` 처럼 거절한다. 문법이 거절하는 것(IPv6 literal, 끝에 점이 붙은 이름)은
    local 이다. 사설망 literal 과 `.internal`·`.local` 은 private, 공인 literal 이 아닌 나머지
    literal, `LOCAL_NAMES`(메타데이터 이름, `localhost` 와 그 별칭, `cluster.local`), 라벨
    하나짜리 이름, `.svc`·`.cluster.local`·`.localhost` 는 local, 그 밖은 outside 다. 이름은 풀지
    않는다. 사설 주소로 풀리는 이름(`my-mcp.my-ns` 같은 두 라벨 클러스터 이름도)은 접미사로 가를 수
    없어 outside 로 남는다."""
    if any(c.isspace() or not c.isprintable() for c in url):
        return "local"
    if "$" in url:
        return "credentials"
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "local"
    authority = _AUTHORITY_END.split(rest, maxsplit=1)[0]
    if "\\" in authority:
        return "local"
    if "@" in authority:
        return "credentials"
    if "%" in authority:
        return "local"
    tail = rest[len(authority) :]
    query, fragment = tail.find("?"), tail.find("#")
    if query >= 0 and (fragment < 0 or query < fragment):
        return "credentials"
    return _host_class(scheme, authority)


def _host_class(scheme: str, authority: str) -> AddressClass:
    """`\\`·`@`·`%` 가 없는 authority 의 갈래. `outside_address` 의 둘째 단계다."""
    host, colon, port = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if colon and not (port.isascii() and port.isdigit() and 0 < int(port) <= 65535):
        return "local"
    if host.startswith("[") or _ends_in_dot(host):
        return "local"
    try:
        name = a_label(host)
    except BlockedURL:
        return "local"
    if origin_of(f"{scheme}://{name}{colon}{port}") is None:
        return "local"
    if _NUMERIC_LABEL.fullmatch(name.rsplit(".", 1)[-1]) and not _ipv4(name):
        return "local"
    if _ipv4(name):
        ip = ipaddress.IPv4Address(name)
        if any(ip in net for net in PRIVATE_V4):
            return "private"
        return "outside" if is_public_address(ip) else "local"
    if name in LOCAL_NAMES or "." not in name or name.endswith(LOCAL_SUFFIXES):
        return "local"
    return "private" if name.endswith(PRIVATE_SUFFIXES) else "outside"


def _ends_in_dot(host: str) -> bool:
    """이름이 점으로 끝나는가. UTS 46 이 점으로 바꾸는 전각·반각 마침표도 센다. `a_label` 은 끝의
    점을 떼므로 그 전에 본다. 바꿀 수 없는 이름은 `a_label` 이 거절한다."""
    if host.isascii():
        return host.endswith(".")
    try:
        return idna.uts46_remap(host, std3_rules=False, transitional=False).endswith(".")
    except idna.IDNAError:
        return False
