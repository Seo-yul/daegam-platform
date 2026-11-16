"""플랫폼의 서명기와 디스커버리 문서 (D40, D41).

플랫폼은 모든 인증 모드에서 토큰 발급자다. local 모드에서는 사람의 토큰을, 모든 모드에서 내부 MCP
홉의 서비스 토큰을 RS256 으로 서명하고 `/.well-known/openid-configuration` 과
`/.well-known/jwks.json` 을 낸다. 데이터 플레인(런타임 검증기·vMCP·노출 앞단·내부 MCPServer)은
기존 OIDC 경로로 이 토큰을 검증하되, JWKS 는 디스커버리가 아니라 설정으로 받은 클러스터 안 주소
(`jwks_url`)에서 받는다 (P32 1절). 디스커버리 문서는 밖의 클라이언트만 읽는다. 플랫폼이 사람
토큰을 내는 모드(local, oidc)의 JWKS 에는 개인 토큰의 슬롯 키(ES256)가 더해지고, 그 키는 이 서명
키에서 유도한다(auth/pat_keys.py).

키 쌍은 플랫폼 네임스페이스 Secret `daegam-local-signing`(`private.pem`) 에 있다. 없으면 만들어
둔다. 레플리카 둘이 동시에 처음 뜨면 둘 다 만들지만 Secret 은 create-if-absent 로 쓰므로 먼저 쓴
쪽이 이기고 진 쪽은 저장된 키를 읽어 쓴다. `kid` 는 공개키의 해시라 같은 키면 어느 레플리카에서나
같다.

세 갈래의 audience 는 겹치지 않는다. 제어면 `daegam-api`, 데이터 플레인 `mcp_audience`(기본
`daegam-mcp`), 내부 홉 `daegam-internal:<tenant>`(테넌트마다 다르고 그것이 테넌트 격리다). 겹치면 한
토큰이 두 경계를 지나므로 Settings 가 기동에서 거절한다. 내부 홉에는 서버 하나만 받는
`daegam-internal:<tenant>:<서버 id>` 도 있다 (P50 9절, `server_audience`).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from daegam.auth.rbac import ROLES_CLAIM, idp_group, role_group

if TYPE_CHECKING:
    from daegam.secrets import SecretStore

SIGNING_SECRET = "daegam-local-signing"
PRIVATE_KEY_FIELD = "private.pem"
ALG = "RS256"
ACCESS_TOKEN_TYP = "at+jwt"
CONTROL_PLANE_AUDIENCE = "daegam-api"
INTERNAL_AUDIENCE_PREFIX = "daegam-internal:"
# LLM 게이트웨이가 사용량을 보낼 때의 서비스 토큰 audience (P37 8절)
TELEMETRY_AUDIENCE = "daegam-telemetry"
# LLM 게이트웨이가 받는 부르는 쪽 토큰의 audience (P38 5절). 다른 어느 경계도 이 값을 받지 않고
# 게이트웨이는 이 값만 받는다
LLM_AUDIENCE = "daegam-llm"
# `/.well-known/jwks.json` 의 캐시 헤더. 회수 한도의 계약이다 (P32 3절). ToolHive 는 max-age 를
# 15분 아래로 내리지 않으므로 이 값이면 다음 조회가 15분 뒤다. 긴 max-age 나 Expires 만 한도를
# 늘리므로 그 둘을 내지 않는다
JWKS_CACHE_CONTROL = "public, max-age=300"
JWKS_PATH = "/.well-known/jwks.json"
# 서비스 토큰 검증기(daegam-internal-oidc)가 JWKS 주소에 붙이는 쿼리 `use=service` (P32 1절). 새
# 경로가 아니라 쿼리인 것은 롤링 중 옛 판의 파드도 같은 경로에 200 으로 답하게 하려는 것이다
SERVICE_JWKS_USE = "service"
SERVICE_JWKS_QUERY = f"use={SERVICE_JWKS_USE}"
# 데이터 플레인 사람 토큰(개인 토큰, OAuth 접근 토큰)의 `scope` 이고, OAuth 가 켜졌을 때 Gateway·
# 노출 앞단의 참조가 광고하는 scope 다 (P33 13절). ToolHive 는 검사하지 않고 경계는 aud 와 groups 다
SCOPE_MCP = "mcp"


def registered_claims(
    *, issuer: str, audience: str | list[str], sub: str, ttl: int
) -> dict[str, Any]:
    """플랫폼이 내는 모든 토큰의 등록 클레임(iss·aud·sub·iat·nbf·exp·jti). 사람 토큰과 서비스
    토큰이 이 위에 자기 필드를 얹는다. OAuth 접근 토큰의 `aud` 는 공유 audience 와 리소스 둘이다
    (P33 10절)."""
    now = int(time.time())
    return {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }


def tenant_token_claims(
    *,
    issuer: str,
    audience: str | list[str],
    sub: str,
    ttl: int,
    tenant: str,
    group: str,
    username: str | None,
    role: str | None = None,
    idp_groups: Sequence[str] = (),
) -> dict[str, Any]:
    """한 테넌트에 묶인 데이터 플레인 토큰 클레임. 개인 토큰(P32 5절), 서비스 어카운트의
    토큰(P53b 6.7절), OAuth 접근 토큰(P33 10절)이 이 위에 선다. `tv` 가 없고 `aud` 가 데이터 플레인
    값이라 제어면이 거절하고, `cid` 가 없어 `agent_client_ids` 의 Cedar 정책에 걸리지 않는다.
    `groups` 는 그 테넌트의 그룹과, oidc 모드면 사람이 가진 IdP 그룹 가운데 그 테넌트가 이름 댄
    것(idp_groups, 원래 이름)을 `daegam-group:<tenant>:<group>` 꼴로 싣는다 (P53b 6.6절).
    username 이 없으면(서비스 어카운트, 이름 클레임이 없는 IdP 토큰) `preferred_username` 을 싣지
    않는다. role 이 있으면 `daegam_roles` 에 그 테넌트의 역할 그룹 하나를 싣는다 (P52 3절)."""
    claims = {
        **registered_claims(issuer=issuer, audience=audience, sub=sub, ttl=ttl),
        "tenant": tenant,
        "groups": [group, *(idp_group(tenant, g) for g in idp_groups)],
        "scope": SCOPE_MCP,
    }
    if username:
        claims["preferred_username"] = username
    if role:
        claims[ROLES_CLAIM] = [role_group(tenant, role)]
    return claims


def jwks_url(issuer: str, *, service: bool = False) -> str:
    """발급자 아래의 JWKS 주소. service 면 서비스 토큰용 모양(`?use=service`)이다 (P32 1절)."""
    url = f"{issuer}{JWKS_PATH}"
    return f"{url}?{SERVICE_JWKS_QUERY}" if service else url


def public_jwk(
    algorithm: type[RSAAlgorithm] | type[ECAlgorithm], public_key: Any, *, kid: str, alg: str
) -> dict[str, Any]:
    """JWKS 에 싣는 공개키 하나. 런타임 검증기는 `use` 로 거르므로(`oidc.py` `_refresh_keys`) 모든
    키가 `use: sig` 다."""
    jwk = algorithm.to_jwk(public_key, as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": alg})
    return jwk


def internal_audience(tenant: str) -> str:
    """내부 MCPServer 가 검증하는 audience. 테넌트마다 다르다 (D41)."""
    if not tenant:
        raise ValueError("internal audience needs a tenant")
    return f"{INTERNAL_AUDIENCE_PREFIX}{tenant}"


def server_audience(tenant: str, server_id: str) -> str:
    """내부 서버 하나만 받는 audience `daegam-internal:<tenant>:<서버 id>` (P50 9절). id 는 MCP
    Server 문서, 허브 `image` 항목, 공유 복제본 기록의 id 라 같은 이름으로 다시 만든 서버도 옛
    서버의 토큰을 받지 않는다."""
    if not server_id:
        raise ValueError("server audience needs a server id")
    return f"{internal_audience(tenant)}:{server_id}"


# 플랫폼 web_fetch 복제본이 받는 audience 의 끝 (P39b 13절). 우리 서버 id 는 uuid 라 겹치지 않는다
FETCH_SERVER_ID = "web-fetch"


def fetch_audience(tenant: str) -> str:
    """플랫폼 web_fetch 복제본 하나만 받는 audience `daegam-internal:<tenant>:web-fetch`. 다른 내부
    MCPServer 는 모두 이 audience 를 거절하므로, 파서가 뚫려 그 토큰을 쥐어도 테넌트의 다른 서버를
    부르지 못한다 (P39b 13절)."""
    return server_audience(tenant, FETCH_SERVER_ID)


# 플랫폼 web_search 엔드포인트가 받는 audience 의 끝 (P39b 12절)
SEARCH_SERVER_ID = "web-search"


def search_audience(tenant: str) -> str:
    """플랫폼 web_search 엔드포인트만 받는 audience `daegam-internal:<tenant>:web-search`. 검색
    Entry 의 토큰은 이것 하나라 테넌트의 다른 내부 서버를 열지 못하고, 다른 Entry 의 토큰은 검색을
    열지 못한다."""
    return server_audience(tenant, SEARCH_SERVER_ID)


# 플랫폼 웹 Tool 의 audience 끝. 우리 MCPServer 문서의 id 가 아니다
PLATFORM_SERVER_IDS = frozenset({FETCH_SERVER_ID, SEARCH_SERVER_ID})


def audience_server_id(tenant: str, audience: str | None) -> str | None:
    """`server_audience` 를 거꾸로 읽는다. audience 가 그 테넌트의 서버 하나의 것이면 그 서버 id
    이고, 테넌트 audience 나 다른 audience 면 None 이다."""
    prefix = f"{internal_audience(tenant)}:"
    if audience and audience.startswith(prefix) and len(audience) > len(prefix):
        return audience[len(prefix) :]
    return None


class PlatformSigner:
    def __init__(self, private_pem: str):
        self.private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = hashlib.sha256(der).hexdigest()[:16]

    @classmethod
    def generate(cls) -> PlatformSigner:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cls(pem.decode())

    @classmethod
    def load_or_create(cls, secrets: SecretStore, *, namespace: str) -> PlatformSigner:
        data = secrets.get(SIGNING_SECRET, namespace=namespace)
        pem = (data or {}).get(PRIVATE_KEY_FIELD)
        if pem:
            return cls(pem)
        fresh = cls.generate()
        # create-if-absent 다. SSA 로 쓰면 레플리카 둘이 서로 다른 키를 쓰고 마지막 쓰기가 남아 파드
        # 수명 내내 갈린다. 먼저 만든 쪽이 이기고 진 쪽은 저장된 키를 읽어 쓴다
        created = secrets.create(
            SIGNING_SECRET,
            {PRIVATE_KEY_FIELD: fresh.private_pem()},
            namespace=namespace,
            labels={"daegam.devops.ai.kr/resource": "local-signing"},
        )
        if created:
            return fresh
        stored = (secrets.get(SIGNING_SECRET, namespace=namespace) or {}).get(PRIVATE_KEY_FIELD)
        if not stored:
            raise RuntimeError(f"secret {SIGNING_SECRET} exists but has no {PRIVATE_KEY_FIELD}")
        return cls(stored)

    def private_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def derive(self, salt: bytes, info: bytes, length: int) -> bytes:
        """서명 키(PKCS8 DER)를 재료로 한 HKDF-SHA256. 서명 키를 가진 쪽만 같은 값을 다시 만든다.
        개인 토큰의 슬롯 키가 이것으로 `kid` 에서 나온다 (P32 4절). 쓰임마다 salt 를 달리한다."""
        ikm = self.private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)

    def public_key(self) -> Any:
        return self.private_key.public_key()

    def jwks(self) -> dict[str, Any]:
        return {"keys": [public_jwk(RSAAlgorithm, self.public_key(), kid=self.kid, alg=ALG)]}

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims,
            self.private_key,
            algorithm=ALG,
            headers={"kid": self.kid, "typ": ACCESS_TOKEN_TYP},
        )


def discovery_document(
    issuer: str, authorization_server: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`/.well-known/openid-configuration`. 검증기들이 보는 것은 issuer 와 jwks_uri 다.

    OAuth 인가 서버가 켜졌으면 `authorization_server`(RFC 8414 메타데이터, `api/oauth.py`)의 필드를
    모두 얹는다. OIDC 문서로 가는 MCP 클라이언트도 `code_challenge_methods_supported` 가 없으면
    멈춰야 하기 때문이다 (P33 2절). 꺼졌으면 인가·토큰 엔드포인트가 없으므로 적지 않는다. 남이
    기계로 읽는 문서에 "라이브러리가 넘어지지 않게" 틀린 값을 채우지 않는다. 나머지 필드는 사실
    그대로다."""
    return {
        "issuer": issuer,
        "jwks_uri": jwks_url(issuer),
        "response_types_supported": [],
        **(authorization_server or {}),
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [ALG],
        "claims_supported": [
            "sub",
            "aud",
            "iss",
            "exp",
            "iat",
            "preferred_username",
            "email",
            "name",
            "tenant",
            "groups",
        ],
    }
