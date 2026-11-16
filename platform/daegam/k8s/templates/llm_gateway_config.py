"""agentgateway standalone 설정 파일의 렌더 (P38 4절·5절·7절·10절).

필드 이름은 F0 과 v1.5.0 의 설정 스키마에서 왔고, `platform/tests/fixtures` 의 스키마 사본과 고정한
이미지의 `--validate-only` 가 렌더를 맞대 본다(test_llm_gateway_render.py).

- 리스너 `llm`(8080) 하나가 JWT 검사와 회수 규칙을 모든 경로에 한 번 건다. jti 목록이 한 번만
  적힌다. 리스너 정책은 리스너 이름으로 붙으므로 모든 리스너에 이름이 있다
- 경로는 provider 마다 메서드와 경로가 정확히 같아야 열린다(`/p/<slug>/v1/chat/completions` 등).
  경로마다 provider 규칙, 토큰 한도, 경로 헤더(`x-daegam-provider`)를 싣고, AI backend 는
  `pathPrefix` 와 키 파일(`backendAuth.key.file`)을 쓴다
- 두 번째 bind(15080, 리스너 `rev`)는 `GET /_daegam/rev` 에 설정 리비전만 답한다. 리스너 JWT
  밖이고 파드 IP 로만 읽는다
- standalone 은 설정 파일을 셸처럼 펼친다(`$VAR`). 렌더에 `$` 가 하나라도 있으면 RenderError 다
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from daegam.k8s.templates.common import RenderError
from daegam.names import provider_slug
from daegam.store.settings import LLMProvider

LISTENER = "llm"
REV_LISTENER = "rev"
GATEWAY_PORT = 8080
REV_PORT = 15080
REV_PATH = "/_daegam/rev"
READY_PORT = 15021
CONFIG_DIR = "/config"
CONFIG_FILE = "config.yaml"
CA_FILE = "ca.pem"
PROVIDER_HEADER = "x-daegam-provider"
RATELIMIT_DOMAIN = "daegam-llm"
RATELIMIT_PORT = 8081
# P36 첨부가 든 요청은 약 11 MiB 다(D61). 넘으면 413 이다 (F0 19번)
BUFFER_BYTES = 12 * 1024 * 1024
# 종료 시간의 세 상수 (P38 10절). 연결 수명, 가장 긴 호출(스펙 생성기의 한 호출), 여유다. preStop 은
# 셋의 합이고 grace 는 그보다 10초 길다
MAX_CONNECTION_SECONDS = 15
LONGEST_CALL_SECONDS = 300
DRAIN_SLACK_SECONDS = 5
PRESTOP_SECONDS = MAX_CONNECTION_SECONDS + LONGEST_CALL_SECONDS + DRAIN_SLACK_SECONDS
TERMINATION_GRACE_SECONDS = PRESTOP_SECONDS + 10
# 한도 서비스가 답하지 않으면(Redis 가 멈추면 그렇다) 기다리지 않고 통과시킨다 (F0 8번)
RATELIMIT_TIMEOUT = "1s"
# 회수 목록이 이보다 크면 상태가 경고한다. 항목은 모두 렌더한다 (P38 5절)
REVOCATION_WARN = 5000
# Secret 하나의 상한
SECRET_MAX_BYTES = 1024 * 1024
# 형식 표. 경로의 꼬리와 agentgateway 의 형식이다. 목록에 없는 경로는 Completions 로 다뤄지므로 모든
# 경로를 적는다 (research 5절)
CHAT_PATH = "/v1/chat/completions"
EMBEDDINGS_PATH = "/v1/embeddings"
MESSAGES_PATH = "/v1/messages"
FORMATS = {CHAT_PATH: "completions", EMBEDDINGS_PATH: "embeddings", MESSAGES_PATH: "messages"}
# 트레이싱이 span 에 더하는 부른 쪽 속성 (8절). 테넌트와 부른 쪽은 게이트웨이가 검증한 JWT 에서,
# provider 는 경로가 위로 보내는 헤더에서 온다. CEL 에는 경로 이름이 없다 (F0 7번)
TRACE_FIELDS = {
    "daegam.caller.tenant": "jwt.tenant",
    "daegam.caller.kind": 'jwt.sub == "svc:platform" ? "platform" : "agent"',
    "daegam.caller.id": "has(jwt.agent_id) ? jwt.agent_id : jwt.purpose",
    # 플랫폼 호출의 목적. 채점 모델(`evaluator`)은 id 가 원본 Agent 이므로 이것으로 가른다
    # (P45 11절)
    "daegam.caller.purpose": 'has(jwt.purpose) ? jwt.purpose : ""',
    "daegam.provider": f'request.headers["{PROVIDER_HEADER}"]',
}


def tracing(endpoint: str) -> dict[str, Any]:
    """`config.tracing`. 모든 요청을 남긴다. 부른 쪽 traceparent 의 sampled 표시가 span 을 건너뛰게
    하지 못한다. traceparent 는 게이트웨이가 자기 span 으로 바꿔 쓰므로 속성으로 읽지 않고, P37 이
    span 의 traceId·parentSpanId 를 호출 id 로 쓴다 (F0 7번). 시작할 때만 읽는 칸이다."""
    return {
        "otlpEndpoint": endpoint,
        "randomSampling": "true",
        "clientSampling": "true",
        "fields": {"add": dict(TRACE_FIELDS)},
    }


# SDK 계열 → agentgateway 의 provider 이름
AI_PROVIDERS = {"openai": "openAI", "anthropic": "anthropic"}


@dataclass(frozen=True)
class GatewayProvider:
    """렌더할 provider 하나. slug 가 경로 접두와 키 파일과 경로 헤더다."""

    name: str
    slug: str
    family: str  # openai·anthropic
    host: str | None  # hostOverride(호스트:포트). 없으면 provider 의 기본 주소다
    path_prefix: str
    tls: bool
    embeddings: bool

    @property
    def paths(self) -> list[str]:
        if self.family == "anthropic":
            return [MESSAGES_PATH]
        return [CHAT_PATH] + ([EMBEDDINGS_PATH] if self.embeddings else [])

    @property
    def key_file(self) -> str:
        return f"key.{self.slug}"

    def base_url(self, gateway_url: str) -> str:
        """호출자가 받는 `base_url` (1절). SDK 의 경로가 뒤에 붙는다. Anthropic SDK 는 스스로
        `/v1/messages` 를 붙이므로 OpenAI 계열만 `/v1` 로 끝난다."""
        base = f"{gateway_url.rstrip('/')}/p/{self.slug}"
        return base if self.family == "anthropic" else f"{base}/v1"


def gateway_provider(p: LLMProvider) -> GatewayProvider:
    """LLMProvider 의 렌더 모양. 주소의 검사(SSRF)는 부르는 쪽이 먼저 한다. OpenAI 계열의 접두는
    base_url 의 경로(없으면 `/v1`)이고, Anthropic SDK 는 base_url 뒤에 `/v1/messages` 를 붙이므로
    anthropic 의 접두는 base_url 의 경로에 `/v1` 을 더한 것이다 (P38 4절)."""
    family = p.sdk_kind
    url = p.effective_base_url
    host, path, tls = None, "", True
    if url:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        name = parts.hostname or ""
        host = f"[{name}]:{port}" if ":" in name else f"{name}:{port}"
        path = parts.path.rstrip("/")
        tls = parts.scheme == "https"
    if family == "anthropic":
        prefix = f"{path}/v1"
    else:
        prefix = path if url else "/v1"
    return GatewayProvider(
        name=p.name,
        slug=provider_slug(p.name),
        family=family,
        host=host,
        path_prefix=prefix,
        tls=tls,
        embeddings=p.supports_embedding,
    )


def _backend(gp: GatewayProvider, *, ca: bool) -> dict[str, Any]:
    policies: dict[str, Any] = {"backendAuth": {"key": {"file": f"{CONFIG_DIR}/{gp.key_file}"}}}
    # 덮어쓴 호스트는 TLS 를 따로 켠다. 주어진 root 는 시스템 묶음을 갈음하므로 CA 파일은 certifi
    # 묶음에 관리자 CA 를 더한 것이다 (P38 10절)
    if gp.tls and (gp.host or ca):
        policies["backendTLS"] = {"root": f"{CONFIG_DIR}/{CA_FILE}"} if ca else {}
    ai: dict[str, Any] = {
        "name": gp.slug,
        "provider": {AI_PROVIDERS[gp.family]: {}},
        "pathPrefix": gp.path_prefix,
        "policies": policies,
    }
    if gp.host:
        ai["hostOverride"] = gp.host
    return {"ai": ai}


def _ratelimit(slug: str, host: str) -> dict[str, Any]:
    """전역 토큰 한도. 설명자는 테넌트(`jwt.tenant`)와 provider(경로의 CEL 문자열)다 (P38 7절)."""
    return {
        "domain": RATELIMIT_DOMAIN,
        "host": host,
        "failureMode": "failOpen",
        "policies": {"http": {"requestTimeout": RATELIMIT_TIMEOUT}},
        "descriptors": [
            {"entries": [{"key": "tenant", "value": "jwt.tenant"}], "type": "tokens"},
            {"entries": [{"key": "provider", "value": json.dumps(slug)}], "type": "tokens"},
        ],
    }


def _route(gp: GatewayProvider, path: str, *, ratelimit_host: str, ca: bool) -> dict[str, Any]:
    tail = path.rsplit("/", 1)[-1]
    return {
        "name": f"{gp.slug}-{tail}",
        "matches": [{"path": {"exact": f"/p/{gp.slug}{path}"}, "method": "POST"}],
        "policies": {
            "ai": {"routes": dict(FORMATS)},
            "authorization": {"rules": [{"allow": f"{json.dumps(gp.slug)} in jwt.llm_providers"}]},
            # 기록의 daegam.provider 다. 부른 쪽이 보낸 같은 이름의 헤더는 덮어쓴다. 트레이싱이 위로
            # 보내는 요청을 읽으므로 헤더는 provider 에게도 간다 (F0 7번)
            "requestHeaderModifier": {"set": {PROVIDER_HEADER: gp.slug}},
            "remoteRateLimit": _ratelimit(gp.slug, ratelimit_host),
        },
        "backends": [_backend(gp, ca=ca)],
    }


def revocation_rule(activated_at: int, revoked: list[str]) -> str:
    """리스너의 회수 규칙. 켠 뒤에 낸 토큰만 받고, 목록의 jti 는 막는다. 목록은 CEL map 이라 키
    찾기다 (P38 5절)."""
    listed = ", ".join(f"{json.dumps(j)}: true" for j in sorted(revoked))
    return f"jwt.iat >= {int(activated_at)} && !(jwt.jti in {{{listed}}})"


def render_config(
    *,
    rev: str,
    issuer: str,
    audience: str,
    jwks: dict[str, Any],
    activated_at: int,
    revoked: list[str],
    providers: list[GatewayProvider],
    ratelimit_host: str,
    ca: bool,
    tracing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """설정 dict. rev 는 `/_daegam/rev` 가 돌려줄 리비전이다. tracing 은 `config.tracing` 이다
    (8절)."""
    routes = [
        _route(gp, path, ratelimit_host=ratelimit_host, ca=ca)
        for gp in providers
        for path in gp.paths
    ]
    listener = {
        "name": LISTENER,
        "protocol": "HTTP",
        "policies": {
            # strict: issuer 와 audiences 는 v1.5 부터 둘 다 있어야 한다. JWKS 는 설정에 싣는다.
            # 주소로 받으면 15분 캐시되어 키를 바꾼 뒤 새 토큰을 거절한다 (P38 5절)
            "jwtAuth": {
                "mode": "strict",
                "issuer": issuer,
                "audiences": [audience],
                "jwks": json.dumps(jwks, sort_keys=True),
            },
            "authorization": {"rules": [{"allow": revocation_rule(activated_at, revoked)}]},
        },
        "routes": routes,
    }
    rev_route = {
        "name": "daegam-rev",
        "matches": [{"path": {"exact": REV_PATH}, "method": "GET"}],
        "policies": {"directResponse": {"status": 200, "body": rev}},
    }
    config: dict[str, Any] = {"connectionTerminationDeadline": f"{PRESTOP_SECONDS}s"}
    if tracing is not None:
        config["tracing"] = tracing
    return {
        "config": config,
        "frontendPolicies": {
            "http": {
                "maxBufferSize": BUFFER_BYTES,
                "maxConnectionDuration": f"{MAX_CONNECTION_SECONDS}s",
            }
        },
        "binds": [
            {"port": GATEWAY_PORT, "listeners": [listener]},
            {
                "port": REV_PORT,
                "listeners": [{"name": REV_LISTENER, "protocol": "HTTP", "routes": [rev_route]}],
            },
        ],
    }


def config_text(config: dict[str, Any]) -> str:
    """설정 파일의 글. JSON 이고 YAML 로도 읽힌다. `$` 가 있으면 standalone 이 env 참조로 펼쳐 값이
    바뀌거나 기동이 멈추므로 RenderError `llm_gateway_config_dollar` 다 (F0 13번)."""
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    if "$" in text:
        raise RenderError("llm_gateway_config_dollar", "the gateway config would contain '$'")
    return text
