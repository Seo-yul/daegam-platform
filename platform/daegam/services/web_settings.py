"""Platform Settings 의 `web_tools` 와 `network` 검사 (P39b 15절).

모델(`store/settings.py`)이 범위를 보고, 여기는 값의 뜻을 본다. 이름 접미사는 소문자 DNS 글자이고
공개 접미사가 아니며(422 domain_is_public_suffix), 막는 CIDR 은 IPv4 이거나 `2000::/3` 안의 IPv6 다.
NetworkPolicy 의 `except` 는 그 블록의 `cidr` 안에서만 받으므로, 이미 막는 ULA 같은 값은 넣을 자리가
없어 거절한다. provider 주소는 https 이고 `searxng` 만 클러스터 안 http 도 받는다. CA 묶음은 PEM
인증서여야 한다(422 invalid_ca_bundle).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from daegam.errors import ServiceError
from daegam.pem import MAX_CA_PEM_BYTES, is_ca_pem
from daegam.services.domains import DomainError, check_domains
from daegam.store.settings import NetworkSettings, WebToolsSettings
from daegam.urlfetch import GLOBAL_V6

# 클러스터 안 주소로 보는 이름의 끝. searxng 만 이런 주소에 http 로 간다
IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")


class WebSettingsError(ServiceError):
    pass


def _invalid(detail: str) -> WebSettingsError:
    return WebSettingsError(422, "invalid_value", detail)


def check_cidrs(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError as e:
            raise _invalid(f"blocked_cidrs: {value!r} is not a CIDR") from e
        if net.version == 6 and not net.subnet_of(GLOBAL_V6):
            raise _invalid(f"blocked_cidrs: {value!r} is outside 2000::/3 and already blocked")
        out.append(str(net))
    return out


# User-Agent 머리에 들어가는 값이라 공백과 제어 문자를 받지 않는다
_HEADER_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")


def _https(value: str, field: str) -> None:
    if _HEADER_UNSAFE.search(value):
        raise _invalid(f"{field} must not contain spaces or control characters")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _invalid(f"{field} must be an https URL without credentials")


def check_endpoint(provider: str, endpoint: str | None) -> None:
    if endpoint is None:
        if provider == "searxng":
            raise _invalid("search.endpoint is required for searxng")
        return
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    in_cluster = host.endswith(IN_CLUSTER_SUFFIXES)
    if provider == "searxng" and parts.scheme == "http" and in_cluster:
        return
    _https(endpoint, "search.endpoint")


def check_ca_bundle(pem: str | None) -> str | None:
    """다듬은 PEM 이다. 비었으면 None 이다. PEM 인증서 블록만 든 값이어야 한다(`daegam/pem.py`
    `is_ca_pem`, Redis TLS 의 CA 와 같은 규칙)."""
    if not pem or not pem.strip():
        return None
    trimmed = is_ca_pem(pem)
    if trimmed is None:
        raise WebSettingsError(
            422,
            "invalid_ca_bundle",
            f"ca_bundle must be PEM certificates only, at most {MAX_CA_PEM_BYTES} bytes",
        )
    return trimmed


def check_web_tools(web: WebToolsSettings) -> WebToolsSettings:
    """검사하고 정규화한 사본(이름 접미사는 A-label, CIDR 은 정규형)을 돌려준다."""
    fetch = web.fetch
    try:
        suffixes = check_domains(fetch.blocked_host_suffixes, suffix=True)
    except DomainError as e:
        raise WebSettingsError(422, e.code, f"blocked_host_suffixes: {e.value!r}") from e
    cidrs = check_cidrs(fetch.blocked_cidrs)
    if fetch.user_agent_contact is not None:
        _https(fetch.user_agent_contact, "fetch.user_agent_contact")
    check_endpoint(web.search.provider, web.search.endpoint)
    return web.model_copy(
        update={
            "fetch": fetch.model_copy(
                update={"blocked_host_suffixes": suffixes, "blocked_cidrs": cidrs}
            )
        }
    )


def check_network(network: NetworkSettings) -> NetworkSettings:
    """검사하고 CA 를 다듬은 사본이다."""
    return network.model_copy(update={"ca_bundle": check_ca_bundle(network.ca_bundle)})
