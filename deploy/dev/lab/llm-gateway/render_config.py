"""P38 F0 실험용 agentgateway standalone 설정 렌더러다. 표준 라이브러리만 쓴다.

    python3 render_config.py SPEC.json > config.yaml

SPEC 은 P38 4절의 모양을 실험에 필요한 만큼만 담는다. 출력은 JSON 이고 YAML 로도 읽힌다.

    {
      "rev": "r1",                            # GET /_daegam/rev 가 돌려주는 값
      "issuer": "http://...", "jwks": {...},  # strict JWT, audience daegam-llm, 인라인 JWKS
      "revoked": ["jti", ...],                # CEL 의 jti 목록
      "listener_auth": true,                  # JWT·jti 목록을 리스너에 한 번만 둔다
                                              # (/_daegam/rev 는 15080 에 둔다)
      "buffer_bytes": 12582912,               # 요청 본문 버퍼(frontendPolicies.http.maxBufferSize)
      "max_connection_duration": "20s",       # 클라이언트 연결 수명(frontendPolicies.http)
      "tracing": {"endpoint": "http://...:4317", "fields": {"name": "CEL"}},
      "ratelimit": {"host": "llm-ratelimit:8081", "failure_mode": "failOpen"},
      "ca_file": "/ca/bundle.pem",            # 업스트림 TLS 를 이 CA 로 확인한다
      "tunnel": "http://proxy:3128",          # 업스트림 연결을 이 CONNECT 프록시로 낸다
      "providers": [
        {"slug": "echo-openai", "kind": "openai", "host": "llm-echo.ns.svc.cluster.local:8080",
         "path_prefix": "/v1", "tls": false, "embeddings": true,
         "backup": {"host": "...", "key": "echo-openai-b"}}
      ]
    }

키는 /config/key.<slug> 파일로만 읽는다(설정과 같은 Secret 볼륨). 렌더 결과에 `$` 가 있으면
멈춘다(P38 4절).
"""

from __future__ import annotations

import json
import sys
from typing import Any

AUDIENCE = "daegam-llm"
KEY_DIR = "/config"
REV_PORT = 15080
PROVIDER_HEADER = "x-daegam-provider"
FORMATS = {
    "/v1/chat/completions": "completions",
    "/v1/embeddings": "embeddings",
    "/v1/messages": "messages",
}


def provider_paths(p: dict[str, Any]) -> list[str]:
    if p["kind"] == "anthropic":
        return ["/v1/messages"]
    return ["/v1/chat/completions"] + (["/v1/embeddings"] if p.get("embeddings") else [])


def ai_provider(p: dict[str, Any], host: str, key: str) -> dict[str, Any]:
    kind = {"openai": "openAI", "anthropic": "anthropic"}[p["kind"]]
    out: dict[str, Any] = {
        "name": key,
        "provider": {kind: {}},
        "policies": {
            "backendAuth": {"key": {"file": f"{KEY_DIR}/key.{key}"}},
        },
    }
    if host:
        out["hostOverride"] = host
    # pathPrefix 가 없으면 들어온 경로(/p/<slug>/...)가 그대로 위로 간다. 있으면 그것에 형식의
    # 꼬리를 붙인다
    out["pathPrefix"] = p.get("path_prefix") or "/v1"
    tls = p.get("tls")
    if tls:
        out["policies"]["backendTLS"] = {"root": tls} if isinstance(tls, str) else {}
    if p.get("tunnel"):
        out["policies"]["backendTunnel"] = {"proxy": {"host": p["tunnel"]}, "mode": "connect"}
    return out


def backend(p: dict[str, Any]) -> dict[str, Any]:
    primary = ai_provider(p, p.get("host", ""), p["slug"])
    backup = p.get("backup")
    if not backup:
        return {"ai": primary}
    second = ai_provider(p, backup.get("host", ""), backup["key"])
    health = p.get("health") or {
        "unhealthyExpression": "response.code >= 500 || response.code == 429",
        "eviction": {"duration": "30s", "consecutiveFailures": 1},
    }
    for member in (primary, second):
        member["policies"]["health"] = health
    # groups 꼴은 name 등 다른 칸을 받지 않는다(LocalAIBackend 의 두 번째 꼴)
    return {"ai": {"groups": [{"providers": [primary]}, {"providers": [second]}]}}


def jwt_auth(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "strict",
        "issuer": spec["issuer"],
        "audiences": [AUDIENCE],
        "jwks": json.dumps(spec["jwks"]),
    }


def revoked_rule(revoked: list[str]) -> str:
    return "!(jwt.jti in [" + ", ".join(json.dumps(j) for j in revoked) + "])"


def authz(slug: str, revoked: list[str]) -> str:
    listed = ", ".join(json.dumps(j) for j in revoked)
    return f'"{slug}" in jwt.llm_providers && !(jwt.jti in [{listed}])'


def route(spec: dict[str, Any], p: dict[str, Any], path: str) -> dict[str, Any]:
    policies: dict[str, Any] = {"ai": {"routes": FORMATS}}
    if spec.get("provider_header"):
        # 기록의 daegam.provider 를 위한 경로 상수다. 부른 쪽이 보낸 같은 이름의 헤더는 덮어쓴다
        policies["requestHeaderModifier"] = {"set": {PROVIDER_HEADER: p["slug"]}}
    if spec.get("listener_auth"):
        # JWT 와 jti 목록은 리스너에 한 번만 있고, 경로에는 provider 확인만 남는다
        policies["authorization"] = {"rules": [{"allow": f'"{p["slug"]}" in jwt.llm_providers'}]}
    else:
        policies["jwtAuth"] = jwt_auth(spec)
        policies["authorization"] = {
            "rules": [{"allow": authz(p["slug"], spec.get("revoked", []))}]
        }
    if spec.get("ratelimit"):
        policies["remoteRateLimit"] = ratelimit(spec["ratelimit"], p["slug"])
    if p.get("retry"):
        policies["retry"] = p["retry"]
    if p.get("timeout"):
        policies["timeout"] = p["timeout"]
    return {
        "name": f"{p['slug']}{path.replace('/', '-')}",
        "matches": [{"path": {"exact": f"/p/{p['slug']}{path}"}, "method": "POST"}],
        "policies": policies,
        "backends": [backend(p)],
    }


def ratelimit(rl: dict[str, Any], slug: str) -> dict[str, Any]:
    return {
        "domain": "daegam-llm",
        "host": rl["host"],
        "failureMode": rl.get("failure_mode", "failOpen"),
        # 한도 서비스가 답하지 않으면(Redis 가 멈추면 그렇다) 기다리지 않고 failureMode 를 따른다
        "policies": {"http": {"requestTimeout": rl.get("timeout", "1s")}},
        "descriptors": [
            {"entries": [{"key": "tenant", "value": "jwt.tenant"}], "type": "tokens"},
            {"entries": [{"key": "provider", "value": json.dumps(slug)}], "type": "tokens"},
        ],
    }


def frontend_http(spec: dict[str, Any]) -> dict[str, Any]:
    http: dict[str, Any] = {"maxBufferSize": spec.get("buffer_bytes", 12 * 1024 * 1024)}
    # 클라이언트 연결의 수명 상한이다. 넘으면 진행 중인 요청이 끝난 뒤 닫는다(P38)
    if spec.get("max_connection_duration"):
        http["maxConnectionDuration"] = spec["max_connection_duration"]
    return http


def render(spec: dict[str, Any]) -> dict[str, Any]:
    routes = [route(spec, p, path) for p in spec["providers"] for path in provider_paths(p)]
    rev_route = {
        "name": "daegam-rev",
        "matches": [{"path": {"exact": "/_daegam/rev"}, "method": "GET"}],
        "policies": {"directResponse": {"status": 200, "body": spec["rev"]}},
    }
    # 리스너 이름이 없으면 리스너 정책이 같은 기본 이름의 다른 리스너에도 걸린다
    listener: dict[str, Any] = {"name": "llm", "protocol": "HTTP", "routes": routes}
    binds = [{"port": 8080, "listeners": [listener]}]
    if spec.get("listener_auth"):
        listener["policies"] = {
            "jwtAuth": jwt_auth(spec),
            "authorization": {"rules": [{"allow": revoked_rule(spec.get("revoked", []))}]},
        }
        # 리스너의 JWT 밖에 두려고 /_daegam/rev 는 다른 포트(파드 IP 로만 읽는다)에 둔다
        binds.append(
            {
                "port": REV_PORT,
                "listeners": [{"name": "rev", "protocol": "HTTP", "routes": [rev_route]}],
            }
        )
    else:
        routes.append(rev_route)
    # 종료 때 열린 연결을 기다리는 상한이다. 없으면 5초다(TERMINATION_GRACE_PERIOD_SECONDS env 도
    # 없을 때).
    # 기동 때만 읽으므로 바꾸면 파드를 굴린다
    config: dict[str, Any] = {"connectionTerminationDeadline": spec.get("drain", "320s")}
    if spec.get("tracing"):
        t = spec["tracing"]
        config["tracing"] = {
            "otlpEndpoint": t["endpoint"],
            "randomSampling": "true",
            # 부른 쪽 traceparent 의 sampled 표시와 상관없이 모든 요청을 기록한다
            "clientSampling": "true",
            "fields": {"add": t.get("fields", {})},
        }
    out: dict[str, Any] = {
        "config": config,
        "frontendPolicies": {"http": frontend_http(spec)},
        "binds": binds,
    }
    return out


def main() -> int:
    spec = json.loads(open(sys.argv[1]).read())
    text = json.dumps(render(spec), indent=1)
    if "$" in text:
        print("render: '$' in config", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
