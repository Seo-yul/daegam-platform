"""내부 MCP 홉의 서비스 토큰을 소비자 옆에 두는 객체 렌더 (D41).

토큰 하나는 서버 하나를 연다 (P50 9절). 노출 앞단은 Secret 하나와 MCPExternalAuthConfig 하나를,
Gateway 의 vMCP 는 토큰을 받는 Entry(internal·image)마다 둘을 같은 이름으로 둔다
(`entry_token_objects`). Secret 의 키 `token` 은 JWT 그대로이고 `authorization` 은
`Bearer <JWT>` 다. vMCP 는 headerInjection(키 `authorization`)으로, MCPRemoteProxy 는
bearerToken(키 `token`)으로 그 값을 안쪽 MCPServer 에 낸다. vMCP 의 outgoing 전략에는
bearer_token 이 없고 MCPRemoteProxy 는 둘 다 받는다(스파이크 2차).

둘 다 소비자 CR 을 ownerReference 로 가리켜 소비자가 지워지면 함께 지워진다. 오퍼레이터는 그 Secret
을 소비자 Deployment 의 secretKeyRef env 로 싣고 파드는 파드 수명 동안 다시 읽지 않으므로(실증)
토큰을 바꾸면 파드 템플릿 어노테이션 `daegam.devops.ai.kr/svc-token-rev` 를 올려 굴린다. 같은
까닭으로 지금 렌더가 쓰지 않게 된 Gateway 의 토큰 객체는 vMCP 가 다 굴러간 뒤에 서비스 토큰 스윕이
지운다 (`services/gateways.py` `GatewayService.settle_token_objects`).
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import Any, Literal

from daegam.auth.service_token import InternalIssuer
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import ensure_labels, stable_hash
from daegam.k8s.templates.common import TOOLHIVE_API, opaque_secret, with_owner

SVC_TOKEN_KEY = "token"
SVC_AUTHORIZATION_KEY = "authorization"
SVC_TOKEN_REV_ANNOTATION = "daegam.devops.ai.kr/svc-token-rev"
# 외부 서버 고정 헤더의 판들 (P86 6.2절). 사본이 있는 vMCP 의 파드 템플릿에만 붙는다
OUTSIDE_HEADER_REV_ANNOTATION = "daegam.devops.ai.kr/outside-header-rev"
RESOURCE_GATEWAY_SVC_TOKEN = "gateway-svc-token"
# Cedar 를 든 MCPAuthzConfig (P50 8절). vMCP 의 것과 노출 앞단의 것이다
RESOURCE_GATEWAY_AUTHZ = "gateway-authz"
RESOURCE_EXPOSE_AUTHZ = "expose-authz"
RESOURCE_EXPOSE_SVC_TOKEN = "mcp-expose-svc-token"

AuthMode = Literal["header", "bearer"]


# 서비스 토큰을 받는 백엔드·허브 항목 종류. 우리 MCPServer 로 가는 것만이다 (D41)
TOKEN_BACKEND_KINDS = frozenset({"internal", "image"})
# Entry 에 토큰 객체를 두는 종류. 플랫폼 웹 Tool 도 받는다 (P39b 13절). fetch 는 fetch audience,
# search 는 플랫폼이 받는 테넌트 audience 다. 서버 id 를 가진 우리 MCPServer 는 위의 둘뿐이다
ENTRY_TOKEN_KINDS = TOKEN_BACKEND_KINDS | {"platform"}
# Entry 이름 해시의 자릿수. Gateway 이름(`gw-` 와 32자) 뒤에 붙어도 63자 안이다
ENTRY_SVC_HASH_LENGTH = 12
# 규칙 지문 꼬리의 자릿수 (P52 2절). `svc-` 와 vMCP 이름(35자), Entry 해시 뒤에 붙어도 63자 안이다
RULES_SUFFIX_LENGTH = 8


class TokenNameCollision(ServiceError):
    """두 Entry 의 토큰 객체 이름이 같다. 한 토큰이 두 서버에 가게 되므로 렌더하지 않는다."""

    def __init__(self, first: str, second: str, name: str):
        super().__init__(
            422,
            "svc_token_name_collision",
            f"Entries {first} and {second} get the same service token object name {name}",
        )


def vmcp_svc_name(k8s_name: str) -> str:
    """Gateway 의 vMCP 가 쓰는 토큰 객체 이름의 앞머리 (`entry_token_objects`)."""
    return f"svc-{k8s_name}"


def entry_token_objects(k8s_name: str, backends: Iterable[Any]) -> dict[str, str]:
    """Gateway 의 vMCP 가 토큰을 받는 Entry(internal·image·platform, 그리고 묶인 토큰을 받는 게시된
    백엔드)마다 쓰는 Secret·MCPExternalAuthConfig 이름 (Entry 이름 → 객체 이름, P50 9절). 이름은
    `svc-<vMCP>-<Entry 이름 해시 12자>` 이고 그 Entry 의 토큰은 그 Entry 가 부르는 서버 하나의
    audience 를 싣는다. 게시된 백엔드는 스냅샷이 `bound_gateway_id` 를 들 때만이고 그 토큰은 그
    Gateway 에 묶인다 (P57 6.1절). 두 Entry 가 같은 이름이 되면 422 `svc_token_name_collision` 이다.
    backends 는 `kind`·`workload` 를 가진 스냅샷이다. 서버에 Tool 규칙이 있으면(`rules_digest`) 이름
    끝에 지문 8자가 붙는다 (P52 2절). 새 이름은 VirtualMCPServer spec 으로만 vMCP 에 닿으므로, 그
    spec 보다 먼저 실패한 렌더와 옛 파드는 옛 객체와 지문을 그대로 쓴다."""
    names: dict[str, str] = {}
    owners: dict[str, str] = {}
    for b in backends:
        if b.kind not in ENTRY_TOKEN_KINDS and not getattr(b, "bound_gateway_id", None):
            continue
        name = f"{vmcp_svc_name(k8s_name)}-{stable_hash(b.workload, ENTRY_SVC_HASH_LENGTH)}"
        digest = getattr(b, "rules_digest", None)
        if digest:
            name = f"{name}-{digest[:RULES_SUFFIX_LENGTH]}"
        owner = owners.setdefault(name, b.workload)
        if owner != b.workload:
            raise TokenNameCollision(owner, b.workload, name)
        names[b.workload] = name
    return names


def vmcp_header_copy_prefix(k8s_name: str) -> str:
    """Gateway 의 vMCP 가 두는 헤더 사본 객체 이름의 앞머리 (`entry_header_objects`)."""
    return f"hdr-{k8s_name}"


def entry_header_objects(k8s_name: str, backends: Iterable[Any]) -> dict[str, str]:
    """고정 헤더를 싣는 entry 백엔드(스냅샷의 `auth_kind` 가 header)마다 그 vMCP 가 두는 헤더 사본의
    Secret·MCPExternalAuthConfig 이름 (Entry 이름 → 객체 이름, P86 6.2절). 이름은
    `hdr-<vMCP>-<Entry 이름 해시 12자>` 다. 토큰 객체(`entry_token_objects`)와 이름이 달라 토큰을
    내거나 갱신하거나 읽는 코드(`GatewayTokens.issue`, `SvcTokens.ensure`, `svc_token_rev`, 스윕)가
    헤더 값을 읽지 않는다. 두 Entry 가 같은 이름이 되면 422 `svc_token_name_collision` 이다. 한 값이
    두 서버에 가게 되기 때문이다. backends 는 `workload`·`auth_kind` 를 가진 스냅샷이다."""
    names: dict[str, str] = {}
    owners: dict[str, str] = {}
    for b in backends:
        if getattr(b, "auth_kind", None) != "header":
            continue
        name = (
            f"{vmcp_header_copy_prefix(k8s_name)}-{stable_hash(b.workload, ENTRY_SVC_HASH_LENGTH)}"
        )
        owner = owners.setdefault(name, b.workload)
        if owner != b.workload:
            raise TokenNameCollision(owner, b.workload, name)
        names[b.workload] = name
    return names


def is_header_object(k8s_name: str, name: str) -> bool:
    return name.startswith(f"{vmcp_header_copy_prefix(k8s_name)}-")


def svc_token_refs(name: str) -> tuple[tuple[str, str, str], ...]:
    """토큰 객체 한 벌(같은 이름의 Secret 과 MCPExternalAuthConfig)의 (apiVersion, kind, 이름).
    Secret 이 먼저라, 지우다 멈춰도 남은 MCPExternalAuthConfig 가 다음 정리에 다시 보인다."""
    return (("v1", "Secret", name), (TOOLHIVE_API, "MCPExternalAuthConfig", name))


def expose_svc_name(k8s_name: str) -> str:
    """노출한 MCP Server 의 앞단이 쓰는 Secret·MCPExternalAuthConfig 이름."""
    return f"svc-expose-{k8s_name}"


def svc_token_rev(*tokens: str | None) -> str:
    """파드 템플릿 어노테이션 값. 토큰마다 jti 앞 8자를 `-` 로 잇는다. 어느 토큰이 바뀌어도 값이
    바뀐다. 없는 토큰은 건너뛰므로 토큰 하나의 판은 그 jti 앞 8자이고, 하나도 없으면 "0" 이다.
    Agent 는 mcp_token 과 gateway_token 두 토큰의 판을 쓴다 (P32 8절)."""
    return "-".join(_jti_rev(t) for t in tokens if t) or "0"


def _jti_rev(token: str) -> str:
    peek = InternalIssuer.peek(token)
    return (peek.jti or "0")[:8] if peek else "0"


def render_svc_token_secret(
    *,
    name: str,
    namespace: str,
    token: str,
    tenant: str,
    resource: str,
    id: str,
    owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return opaque_secret(
        name,
        namespace,
        {SVC_TOKEN_KEY: token, SVC_AUTHORIZATION_KEY: f"Bearer {token}"},
        tenant=tenant,
        resource=resource,
        id=id,
        owner=owner,
    )


def render_external_auth_config(
    *,
    name: str,
    namespace: str,
    secret_name: str,
    mode: AuthMode,
    tenant: str,
    resource: str,
    id: str,
    owner: dict[str, Any] | None = None,
    header_name: str = "Authorization",
    secret_key: str = SVC_AUTHORIZATION_KEY,
) -> dict[str, Any]:
    """`header` 는 vMCP(Entry 의 externalAuthConfigRef) 용, `bearer` 는 MCPRemoteProxy 용이다.
    `header` 는 Secret 의 secret_key 값을 header_name 헤더로 싣는다. 서비스 토큰은 기본값 그대로
    `Authorization` 과 키 `authorization` 이고, 외부 서버의 고정 헤더는 항목의 헤더 이름과 사본의
    키다 (P86 6.2절)."""
    if mode == "header":
        spec: dict[str, Any] = {
            "type": "headerInjection",
            "headerInjection": {
                "headerName": header_name,
                "valueSecretRef": {"name": secret_name, "key": secret_key},
            },
        }
    else:
        spec = {
            "type": "bearerToken",
            "bearerToken": {"tokenSecretRef": {"name": secret_name, "key": SVC_TOKEN_KEY}},
        }
    return with_owner(
        ensure_labels(
            {
                "apiVersion": TOOLHIVE_API,
                "kind": "MCPExternalAuthConfig",
                "metadata": {"name": name, "namespace": namespace},
                "spec": spec,
            },
            tenant=tenant,
            resource=resource,
            id=id,
        ),
        owner,
    )


def secret_value(secret: dict[str, Any] | None, key: str) -> str | None:
    """Secret 문서에서 키 하나. 서버가 준 `data`(base64)와 우리가 적용한 `stringData` 를 다 본다."""
    if not secret:
        return None
    plain = (secret.get("stringData") or {}).get(key)
    if plain:
        return str(plain)
    raw = (secret.get("data") or {}).get(key)
    if raw:
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def existing_svc_token(client: KubeClient, name: str, namespace: str) -> str | None:
    """소비자의 Secret 에 이미 있는 토큰. 재배포는 이것을 다시 써 토큰이 굳는다(갱신은 리컨실
    스윕의 몫이다)."""
    return secret_value(client.get("v1", "Secret", name, namespace), SVC_TOKEN_KEY)
