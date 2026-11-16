"""Platform Settings 의 `web_tools.fetch` 를 env 로 받는다 (P39b 13절). 플랫폼이 복제본 MCPServer 의
`spec.env` 에 넣고, 값이 바뀌면 `daegam.devops.ai.kr/web-config` 해시가 바뀌어 파드가 다시 뜬다.

상한은 설정이 넘을 수 없는 값이다. 설정 검사(플랫폼)가 같은 상한을 지키지만, 여기서도 다시 자른다.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
from dataclasses import dataclass, field

from webtools import VERSION

# 플랫폼 `WebFetchSettings` 의 상한과 같다(`platform/tests/test_web_tools_parity.py` 가 맞대 본다)
MAX_BYTES_CAP = 10 * 1024 * 1024
TIMEOUT_CAP = 60.0
MAX_CHARS_CAP = 100_000
MAX_REDIRECTS_CAP = 5
PER_MINUTE_CAP = 6000
MAX_PORTS = 10
MAX_CIDRS = 32
# 관리자가 준 CA 묶음(network.ca_bundle, PEM 의 base64)이다. 공개 인증서라 env 로 오고, ToolHive 가
# 줄바꿈이 든 env 값을 거절하므로 base64 다
CA_ENV = "WEB_FETCH_CA_B64"
PEM_CERTIFICATE = "-----BEGIN CERTIFICATE-----"

log = logging.getLogger("webtools.settings")


def _int(env: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(name, default))
    except ValueError:
        value = default
    return max(low, min(high, value))


def _list(env: dict[str, str], name: str) -> list[str]:
    return [v.strip() for v in env.get(name, "").split(",") if v.strip()]


@dataclass(frozen=True)
class FetchSettings:
    max_bytes: int = 2 * 1024 * 1024
    max_chars: int = 20_000
    timeout_seconds: float = 15.0
    max_redirects: int = 5
    allowed_ports: frozenset[int] = frozenset({80, 443})
    blocked_suffixes: tuple[str, ...] = ()
    blocked_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    respect_robots: bool = True
    user_agent_contact: str | None = None
    per_minute: int = 60
    # 파드 하나가 한꺼번에 가져오는 수다. 설정으로 바꾸지 않는다
    concurrency: int = 8
    ca_bundle: str | None = field(default=None, repr=False)

    @property
    def user_agent(self) -> str:
        """기본은 우리 도구 이름과 판뿐이다. 관리자가 연락처를 주었을 때만 덧붙인다."""
        base = f"daegam-web-fetch/{VERSION}"
        return f"{base} (+{self.user_agent_contact})" if self.user_agent_contact else base


def _ca(value: str) -> str | None:
    """CA env 의 PEM 이다. 없으면 None 이다. base64 가 아니거나 풀어도 PEM 인증서가 아니면 경고를
    한 번 남기고 None 이다. 그러면 certifi 만으로 가져오고, 사내 방화벽 뒤라면 TLS 가 실패한다."""
    if not value:
        return None
    try:
        pem = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pem = ""
    if PEM_CERTIFICATE not in pem:
        log.warning(
            "%s is not a base64-encoded PEM certificate bundle; fetching without the extra CA",
            CA_ENV,
        )
        return None
    return pem


def load(env: dict[str, str] | None = None) -> FetchSettings:
    """플랫폼이 렌더한 `WEB_FETCH_*` 를 읽는다(`platform/daegam/k8s/templates/web_tools.py`
    `fetch_env`). 없거나 틀린 값은 `FetchSettings` 의 기본값이고 상한 밖은 상한으로 자른다."""
    env = dict(os.environ if env is None else env)
    d = FetchSettings()
    ports = []
    for value in _list(env, "WEB_FETCH_ALLOWED_PORTS")[:MAX_PORTS]:
        if value.isdigit() and 1 <= int(value) <= 65535:
            ports.append(int(value))
    cidrs = []
    for value in _list(env, "WEB_FETCH_BLOCKED_CIDRS")[:MAX_CIDRS]:
        try:
            cidrs.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return FetchSettings(
        max_bytes=_int(env, "WEB_FETCH_MAX_BYTES", d.max_bytes, 1024, MAX_BYTES_CAP),
        max_chars=_int(env, "WEB_FETCH_MAX_CHARS", d.max_chars, 500, MAX_CHARS_CAP),
        timeout_seconds=float(
            _int(env, "WEB_FETCH_TIMEOUT_SECONDS", int(d.timeout_seconds), 1, int(TIMEOUT_CAP))
        ),
        max_redirects=_int(env, "WEB_FETCH_MAX_REDIRECTS", d.max_redirects, 0, MAX_REDIRECTS_CAP),
        allowed_ports=frozenset(ports) or d.allowed_ports,
        blocked_suffixes=tuple(
            s.lower().strip(".") for s in _list(env, "WEB_FETCH_BLOCKED_SUFFIXES")
        ),
        blocked_cidrs=tuple(cidrs),
        respect_robots=env.get("WEB_FETCH_RESPECT_ROBOTS", "true").lower() != "false",
        user_agent_contact=env.get("WEB_FETCH_USER_AGENT_CONTACT") or None,
        per_minute=_int(env, "WEB_FETCH_PER_MINUTE", d.per_minute, 1, PER_MINUTE_CAP),
        ca_bundle=_ca(env.get(CA_ENV, "")),
    )
