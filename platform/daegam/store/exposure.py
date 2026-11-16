"""외부 노출 스냅샷 (resource-model.md 11절, D22).

스토어가 없고 리소스 문서 안에 내장되는 모델이다. 자기 컬렉션이 없으므로 store/mcp_servers.py 와
store/gateways.py 의 모델이 `exposure` 필드로 품는다. MCP Server 는 draft 에서도 이 값을
갖는다(배포 설정이자 복제가 받는 값이다, D42).

MCP Server 와 Gateway 가 같은 모델을 쓴다. enabled 가 false 면 나머지는 비어 있고
Ingress·앞단 프록시·NetworkPolicy 를 만들지 않는다. Agent 는 `AgentExposure` 파생 모델을
쓴다(D27). 공용 모델은 extra=forbid 라 에이전트 전용 필드가 MCP·Gateway 본문에 새지 않는다."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# edge 는 Ingress 앞의 프록시가 TLS 를 끝낸다는 뜻이다. Ingress 에는 tls 가 없고 주소는
# https 다(D54)
TLSMode = Literal["none", "secret", "cert-manager", "edge"]
# 브라우저 origin. scheme + host(:port) 뿐이고 경로가 없다
CORS_ORIGIN_RE = re.compile(r"^https?://[^/]+$")


def normalize_cors_origin(raw: str) -> str:
    """소문자로 정규화한 origin. 형식이 아니면 ValueError 다 (호출부가 422 invalid_cors_origin 으로
    옮긴다)."""
    origin = (raw or "").strip().lower()
    if not CORS_ORIGIN_RE.fullmatch(origin):
        raise ValueError(f"cors origin {raw!r} must be <scheme>://<host>[:port] without a path")
    return origin


class ExposureTLS(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TLSMode = "none"
    secret_name: str | None = None  # secret: 배포 네임스페이스의 type=kubernetes.io/tls Secret
    issuer: str | None = None  # cert-manager: ClusterIssuer 이름. Secret 은 <k8s.name>-tls


class Exposure(BaseModel):
    """생성 폼에서 정한 노출 설정의 스냅샷. scheme 은 tls.mode 에서 파생된다 (입력받지 않는다)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str | None = None  # 소문자 DNS-1123 호스트명으로 정규화된 값
    ingress_class: str | None = None
    tls: ExposureTLS = Field(default_factory=ExposureTLS)

    @property
    def scheme(self) -> str:
        # none 만 평문이다. secret·cert-manager 는 Ingress 가, edge 는 그 앞이 TLS 를 끝낸다
        return "http" if self.tls.mode == "none" else "https"

    @property
    def external_url(self) -> str | None:
        if not self.enabled or not self.host:
            return None
        return f"{self.scheme}://{self.host}/mcp"


class AgentExposure(Exposure):
    """Agent 노출 스냅샷 (D27). 런타임이 OIDC 토큰을 직접 검증하므로 앞단 프록시가 없고,
    브라우저 origin 과 인가 그룹이 더 있다. external_url 은 엔드포인트가 여럿
    (/chat·/sessions·/memories)이라 경로 없는 `<scheme>://<host>` 다."""

    cors_origins: list[str] = Field(default_factory=list)  # 각각 소문자 <scheme>://<host>[:port]
    allowed_groups: list[str] = Field(default_factory=list)  # 비어 있으면 인증된 주체 전부

    @property
    def external_url(self) -> str | None:
        if not self.enabled or not self.host:
            return None
        return f"{self.scheme}://{self.host}"


def exposure_enabled(doc: Any) -> bool:
    """문서가 노출을 켰는가. 노출 칸이 없는 종류(허브 항목, 복제본 기록)는 아니다."""
    return bool(getattr(getattr(doc, "exposure", None), "enabled", False))


def tls_secret_name_for(k8s_name: str) -> str:
    """cert-manager 모드에서 플랫폼이 이름 짓고 cert-manager 가 만드는 Secret."""
    return f"{k8s_name}-tls"
