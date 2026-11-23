"""LLM 게이트웨이의 렌더 (P38 4절·5절·7절·10절).

설정 파일은 v1.5.0 의 설정 스키마 사본(`fixtures/agentgateway-v1.5.0-config.schema.json`,
https://raw.githubusercontent.com/agentgateway/agentgateway/v1.5.0/schema/config.json 을 키 정렬로
줄인 것)과 맞대고, 고정한 이미지가 로컬 도커에 있으면 `--validate-only` 로도 본다. 이미지를 당기지는
않는다.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import time
from pathlib import Path

import jsonschema
import pytest
import yaml

from daegam.k8s.resources import validate_complete_resources
from daegam.k8s.templates import llm_gateway as tpl
from daegam.k8s.templates.common import RenderError
from daegam.k8s.templates.llm_gateway_config import (
    BUFFER_BYTES,
    PRESTOP_SECONDS,
    REVOCATION_WARN,
    TERMINATION_GRACE_SECONDS,
    config_text,
    gateway_provider,
    render_config,
    revocation_rule,
    tracing,
)
from daegam.services.llm_gateway import upstream_problem
from daegam.store.llm_gateway import LlmGatewayImages
from daegam.store.settings import LlmGatewayLimits, LLMProvider
from tests.helpers.consts import REPO

SCHEMA = REPO / "platform" / "tests" / "fixtures" / "agentgateway-v1.5.0-config.schema.json"
# 게이트웨이 이미지 넷의 참조는 대감 차트의 값이다 (P85 8절)
CHART_VALUES = REPO / "charts" / "daegam" / "values.yaml"
RESERVED = frozenset({"daegam-system", "daegam-llm"})
ISSUER = "http://daegam-platform.daegam-system.svc:9000"
JWKS = {
    "keys": [{"kty": "RSA", "n": "abc", "e": "AQAB", "kid": "k1", "use": "sig", "alg": "RS256"}]
}
DIGEST = "sha256:" + "b" * 64
IMAGES = LlmGatewayImages(
    registry_prefix="registry.example.com/daegam/third-party",
    gateway_image=f"agentgateway:v1.5.0@{DIGEST}",
    ratelimit_image=f"ratelimit:f4e76690@{DIGEST}",
    redis_image=f"redis:8.2@{DIGEST}",
    collector_image=f"opentelemetry-collector-contrib:0.163.2@{DIGEST}",
)


def _provider(name: str, kind: str, base_url: str | None = None) -> LLMProvider:
    return LLMProvider(name=name, kind=kind, api_key_secret=f"k-{name}", base_url=base_url)


PROVIDERS = [
    _provider("openai", "openai"),
    _provider("Claude.Main", "anthropic", "https://api.anthropic.com"),
    _provider("claude-proxy", "anthropic", "https://llm.example.com/anthropic"),
    _provider("echo", "openai", "http://llm-echo.daegam-e2e-llm.svc.cluster.local:8080/v1"),
    _provider("router", "openrouter"),
    _provider("glm", "zai"),
]


def _config(revoked: list[str] | None = None, *, ca: bool = False) -> dict:
    """게이트웨이 설정. 트레이싱(8절)도 싣는다."""
    return render_config(
        rev="r1",
        issuer=ISSUER,
        audience="daegam-llm",
        jwks=JWKS,
        activated_at=1_790_000_000,
        revoked=revoked or [],
        providers=[gateway_provider(p) for p in PROVIDERS],
        ratelimit_host="llm-ratelimit.daegam-llm.svc.cluster.local:8081",
        ca=ca,
        tracing=tracing("http://llm-otel.daegam-llm.svc.cluster.local:4317"),
    )


def _routes(config: dict) -> dict[str, dict]:
    [listener] = config["binds"][0]["listeners"]
    return {r["matches"][0]["path"]["exact"]: r for r in listener["routes"]}


# -- 경로와 backend -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "prefix", "host"),
    [
        ("openai", "/v1", None),
        # Anthropic SDK 는 base_url 뒤에 /v1/messages 를 붙인다
        ("Claude.Main", "/v1", "api.anthropic.com:443"),
        ("claude-proxy", "/anthropic/v1", "llm.example.com:443"),
        ("echo", "/v1", "llm-echo.daegam-e2e-llm.svc.cluster.local:8080"),
        ("router", "/api/v1", "openrouter.ai:443"),
        ("glm", "/api/paas/v4", "api.z.ai:443"),
    ],
)
def test_every_backend_strips_the_gateway_prefix(name, prefix, host):
    """pathPrefix 가 없으면 들어온 경로(/p/<slug>/…)가 그대로 위로 간다 (F0 5번). 위의 경로는
    접두에 형식의 꼬리를 붙인 것이다."""
    gp = gateway_provider(next(p for p in PROVIDERS if p.name == name))
    [route, *_] = [r for path, r in _routes(_config()).items() if path.startswith(f"/p/{gp.slug}/")]
    [backend] = route["backends"]
    assert backend["ai"]["pathPrefix"] == prefix
    assert backend["ai"].get("hostOverride") == host


def test_only_the_three_table_paths_open_with_post():
    paths = _routes(_config())
    assert set(paths) == {
        "/p/openai/v1/chat/completions",
        "/p/openai/v1/embeddings",
        "/p/claude-main-876521/v1/messages",
        "/p/claude-proxy/v1/messages",
        "/p/echo/v1/chat/completions",
        "/p/echo/v1/embeddings",
        "/p/router/v1/chat/completions",
        "/p/glm/v1/chat/completions",
    }
    for route in paths.values():
        assert route["matches"][0]["method"] == "POST"
        assert set(route["policies"]["ai"]["routes"].values()) == {
            "completions",
            "embeddings",
            "messages",
        }


def test_each_route_carries_its_provider_rule_limit_and_header():
    route = _routes(_config())["/p/router/v1/chat/completions"]
    policies = route["policies"]
    assert policies["authorization"] == {"rules": [{"allow": '"router" in jwt.llm_providers'}]}
    assert policies["requestHeaderModifier"] == {"set": {"x-daegam-provider": "router"}}
    limit = policies["remoteRateLimit"]
    assert limit["failureMode"] == "failOpen"
    assert limit["policies"] == {"http": {"requestTimeout": "1s"}}
    assert [d["entries"][0] for d in limit["descriptors"]] == [
        {"key": "tenant", "value": "jwt.tenant"},
        {"key": "provider", "value": '"router"'},
    ]
    backend = route["backends"][0]["ai"]
    assert backend["policies"]["backendAuth"] == {"key": {"file": "/config/key.router"}}
    assert backend["policies"]["backendTLS"] == {}


def test_tls_follows_the_scheme_and_the_admin_ca():
    plain = _routes(_config())["/p/echo/v1/chat/completions"]["backends"][0]["ai"]
    assert "backendTLS" not in plain["policies"]
    with_ca = _routes(_config(ca=True))
    assert with_ca["/p/router/v1/chat/completions"]["backends"][0]["ai"]["policies"][
        "backendTLS"
    ] == {"root": "/config/ca.pem"}
    # 기본 주소 provider 도 관리자 CA 를 믿는다
    assert with_ca["/p/openai/v1/embeddings"]["backends"][0]["ai"]["policies"]["backendTLS"]


# -- 리스너와 회수 --------------------------------------------------------------------------


def test_the_listener_checks_the_jwt_and_the_revocations_once():
    config = _config(["b2", "a1"])
    [listener] = config["binds"][0]["listeners"]
    assert listener["name"] == "llm"
    jwt_auth = listener["policies"]["jwtAuth"]
    assert jwt_auth["mode"] == "strict" and jwt_auth["issuer"] == ISSUER
    assert jwt_auth["audiences"] == ["daegam-llm"]
    assert json.loads(jwt_auth["jwks"]) == JWKS
    [rule] = listener["policies"]["authorization"]["rules"]
    assert rule["allow"] == 'jwt.iat >= 1790000000 && !(jwt.jti in {"a1": true, "b2": true})'
    # 경로에는 jti 목록이 없다
    for route in _routes(config).values():
        assert "jti" not in json.dumps(route)


def test_the_revision_port_answers_outside_the_jwt():
    config = _config()
    rev_bind = config["binds"][1]
    assert rev_bind["port"] == 15080
    [listener] = rev_bind["listeners"]
    assert listener["name"] == "rev" and "policies" not in listener
    [route] = listener["routes"]
    assert route["matches"] == [{"path": {"exact": "/_daegam/rev"}, "method": "GET"}]
    assert route["policies"] == {"directResponse": {"status": 200, "body": "r1"}}


def test_frontend_and_shutdown_constants():
    config = _config()
    assert config["frontendPolicies"]["http"] == {
        "maxBufferSize": BUFFER_BYTES,
        "maxConnectionDuration": "15s",
    }
    assert config["config"]["connectionTerminationDeadline"] == f"{PRESTOP_SECONDS}s"
    assert (PRESTOP_SECONDS, TERMINATION_GRACE_SECONDS) == (320, 330)


def test_a_dollar_anywhere_fails_the_render():
    """standalone 은 설정 파일의 `$VAR` 를 펼친다 (F0 13번)."""
    config = _config()
    config["binds"][0]["listeners"][0]["policies"]["jwtAuth"]["issuer"] = "http://x/$HOME"
    with pytest.raises(RenderError) as e:
        config_text(config)
    assert e.value.code == "llm_gateway_config_dollar"


def test_the_render_matches_the_pinned_schema():
    schema = json.loads(SCHEMA.read_text())
    config = json.loads(config_text(_config(["j1"], ca=True)))
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(config)]
    assert errors == []


def _pinned_image() -> str | None:
    """차트 값의 게이트웨이 이미지가 로컬 도커에 있으면 그 참조다. 당기지 않는다."""
    if shutil.which("docker") is None:
        return None
    ref = yaml.safe_load(CHART_VALUES.read_text())["llmGateway"]["images"]["gateway"]["image"]
    found = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
    return ref if found.returncode == 0 else None


def _validate(image: str, directory: Path) -> tuple[bool, str]:
    out = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{directory}:/config:ro",
            image,
            "-f",
            "/config/config.yaml",
            "--validate-only",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    return out.returncode == 0 and "Configuration is valid" in text, text


@pytest.mark.parametrize("entries", [0, REVOCATION_WARN])
def test_the_pinned_image_accepts_the_render(tmp_path, entries):
    """listener 의 CEL 두 규칙과 경로 규칙을 고정한 이미지가 받는다. 회수 목록이 경고 크기여도
    받는다. 이미지가 로컬에 없으면 건너뛴다."""
    image = _pinned_image()
    if image is None:
        pytest.skip("the pinned agentgateway image is not in the local docker")
    revoked = [f"{n:032x}" for n in range(entries)]
    text = config_text(_config(revoked, ca=True))
    (tmp_path / "config.yaml").write_text(text)
    for p in PROVIDERS:
        (tmp_path / gateway_provider(p).key_file).write_text("sk-test")
    (tmp_path / "ca.pem").write_text("")
    started = time.monotonic()
    ok, out = _validate(image, tmp_path)
    assert ok, out[-2000:]
    # 경고 크기의 목록도 Secret 하나(1 MiB)에 든다
    assert len(text.encode()) < 1024 * 1024 / 2, len(text)
    print(f"jtis={entries} bytes={len(text)} validate={time.monotonic() - started:.2f}s")


def test_the_pinned_image_refuses_a_broken_rule(tmp_path):
    image = _pinned_image()
    if image is None:
        pytest.skip("the pinned agentgateway image is not in the local docker")
    config = _config()
    config["binds"][0]["listeners"][0]["policies"]["authorization"]["rules"][0]["allow"] = "(("
    (tmp_path / "config.yaml").write_text(config_text(config))
    ok, out = _validate(image, tmp_path)
    assert not ok, out


def test_the_revocation_rule_is_a_map_lookup():
    assert revocation_rule(5, []) == "jwt.iat >= 5 && !(jwt.jti in {})"


# -- 주소 검사 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "kind", "problem"),
    [
        (None, "openai", None),
        ("https://api.example.com/v1", "openai", None),
        ("http://llm-echo.daegam-e2e-llm.svc.cluster.local:8080/v1", "openai", None),
        ("https://llm.team.svc/v1", "openai", None),
        ("http://api.example.com/v1", "openai", "only https"),
        ("https://u:p@api.example.com/v1", "openai", "credentials"),
        ("https://api.example.com/v1?x=1", "openai", "query"),
        ("https://api.example.com/v1/$HOME", "openai", "'$'"),
        ("http://daegam-platform.daegam-system.svc:9000/v1", "openai", "reserved"),
        ("http://llm-gateway.daegam-llm.svc:8080/v1", "openai", "reserved"),
        ("http://x.kube-system.svc/v1", "openai", "reserved"),
        ("http://svc.svc/v1", "openai", "<service>.<namespace>.svc"),
        ("https://10.0.0.5/v1", "openai", "not public"),
        ("https://169.254.169.254/v1", "openai", "link-local"),
        ("https://8.8.8.8/v1", "openai", None),
        ("https://api.example.com", "openai", "needs a path"),
        # 클러스터 DNS 의 검색 도메인으로 풀리는 짧은 이름
        ("https://daegam-platform.daegam-system/v1", "openai", "reserved"),
        ("https://llm-gateway.daegam-llm/v1", "openai", "reserved"),
        ("https://daegam-platform/v1", "openai", "single label"),
        ("https://x.kube-system.svc.cluster/v1", "openai", "reserved"),
        ("https://api.example.com", "anthropic", None),
    ],
)
def test_the_upstream_check(base_url, kind, problem):
    got = upstream_problem(_provider("p", kind, base_url), RESERVED)
    assert (got is None) if problem is None else (got is not None and problem in got), got


# -- 워크로드와 정책 -----------------------------------------------------------------------


def _workloads(pdb: bool = False) -> list[dict]:
    docs = [
        *tpl.render_policies(
            namespace="daegam-llm",
            platform_namespace="daegam-system",
            install_id="inst",
            gateway_egress=[],
            internal_upstreams=[("daegam-e2e-llm", 8080), ("daegam-e2e-llm", 8080)],
        ),
        *tpl.render_redis(IMAGES, namespace="daegam-llm", install_id="inst"),
        *tpl.render_ratelimit(IMAGES, namespace="daegam-llm", install_id="inst", config="x"),
        tpl.render_gateway(IMAGES, namespace="daegam-llm", install_id="inst", config_rev="c1"),
        tpl.render_service(tpl.GATEWAY, "daegam-llm", 8080, install_id="inst"),
    ]
    if pdb:
        docs.append(tpl.render_pdb("daegam-llm", install_id="inst"))
    return docs


def _pod_specs(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in docs
        if d["kind"] == "Deployment"
    }


def test_every_container_is_restricted_with_a_numeric_user_and_resources():
    """PodSecurity restricted 를 집행하므로 넷 모두 숫자 사용자로 뜬다 (F0 1번, D54)."""
    users = {"llm-gateway": 65532, "llm-ratelimit": 65532, "llm-redis": 999}
    requests_cpu = requests_mem = 0
    for name, spec in _pod_specs(_workloads()).items():
        sc = spec["securityContext"]
        assert sc["runAsNonRoot"] is True and sc["seccompProfile"] == {"type": "RuntimeDefault"}
        assert (sc["runAsUser"], sc["runAsGroup"]) == (users[name], users[name])
        assert spec["automountServiceAccountToken"] is False
        for c in spec["containers"]:
            validate_complete_resources(c["resources"], where=name)
            csc = c["securityContext"]
            assert csc["allowPrivilegeEscalation"] is False and csc["capabilities"] == {
                "drop": ["ALL"]
            }
            assert csc["readOnlyRootFilesystem"] is True
            assert c["image"].startswith("registry.example.com/daegam/third-party/")
            replicas = 2 if name == "llm-gateway" else 1
            requests_cpu += replicas * int(c["resources"]["requests"]["cpu"].rstrip("m"))
            requests_mem += replicas * int(c["resources"]["requests"]["memory"].rstrip("Mi"))
    # 수집기 두 레플리카(8절)와 함께 F0 12번의 상한 200m, 384Mi 안이다
    assert requests_cpu + 2 * 20 <= 200 and requests_mem + 2 * 64 <= 384


def test_the_gateway_deployment_rolls_without_cutting_streams():
    dep = next(
        d
        for d in _workloads()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "llm-gateway"
    )
    spec = dep["spec"]
    assert spec["replicas"] == 2
    assert spec["strategy"]["rollingUpdate"] == {"maxUnavailable": 0}
    pod = spec["template"]
    assert pod["metadata"]["annotations"] == {"daegam.devops.ai.kr/config-rev": "c1"}
    [c] = pod["spec"]["containers"]
    assert c["args"] == ["-f", "/config/config.yaml"]
    assert c["lifecycle"] == {"preStop": {"sleep": {"seconds": 320}}}
    assert pod["spec"]["terminationGracePeriodSeconds"] == 330
    assert c["readinessProbe"]["httpGet"] == {"path": "/healthz/ready", "port": 15021}
    assert c["resources"]["limits"]["memory"] == "512Mi"
    assert pod["spec"]["volumes"] == [
        {"name": "config", "secret": {"secretName": "llm-gateway-config"}}
    ]


def test_every_object_carries_the_install_marks_and_services_are_cluster_ip():
    docs = _workloads(pdb=True)
    for d in docs:
        labels = d["metadata"]["labels"]
        assert labels["daegam.devops.ai.kr/platform-id"] == "inst"
        assert "daegam.devops.ai.kr/tenant" not in labels
        if d["kind"] == "Service":
            assert d["spec"]["type"] == "ClusterIP"
    assert [d["kind"] for d in docs].count("PodDisruptionBudget") == 1
    assert "PodDisruptionBudget" not in [d["kind"] for d in _workloads()]


def _policy(docs: list[dict], name: str) -> dict:
    return next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == name)[
        "spec"
    ]


def test_the_gateway_takes_only_gateway_route_agents_and_the_platform():
    gw = _policy(_workloads(), "llm-gateway")
    agents, platform = gw["ingress"][0]["from"]
    assert agents["podSelector"] == {"matchLabels": {"daegam.devops.ai.kr/llm-route": "gateway"}}
    assert agents["namespaceSelector"] == {
        "matchExpressions": [{"key": "daegam.devops.ai.kr/tenant", "operator": "Exists"}]
    }
    assert platform == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "daegam-system"}}
    }
    assert gw["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert gw["ingress"][1] == {"from": [platform], "ports": [{"protocol": "TCP", "port": 15080}]}
    opened = {p["port"] for rule in gw["ingress"] for p in rule["ports"]}
    assert opened.isdisjoint({15000, 15020, 15021})


def test_the_gateway_goes_out_only_to_dns_the_limit_service_and_public_443():
    gw = _policy(_workloads(), "llm-gateway")
    dns, limit, public = gw["egress"]
    assert {p["port"] for p in dns["ports"]} == {53}
    assert limit["ports"] == [{"protocol": "TCP", "port": 8081}]
    v4 = public["to"][0]["ipBlock"]
    assert (
        v4["cidr"] == "0.0.0.0/0"
        and "169.254.0.0/16" in v4["except"]
        and "10.0.0.0/8" in v4["except"]
    )
    assert public["ports"] == [{"protocol": "TCP", "port": 443}]
    internal = _policy(_workloads(), "llm-internal-upstreams")
    assert internal["policyTypes"] == ["Egress"] and "ingress" not in internal
    assert internal["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "daegam-e2e-llm"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]


def test_every_except_of_a_policy_lies_strictly_inside_its_cidr():
    """API 서버는 `cidr` 의 진부분집합이 아닌 `except` 를 422 로 거절하고, 그러면 게이트웨이의
    객체가 하나도 적용되지 않는다."""
    blocks = [
        peer["ipBlock"]
        for doc in _workloads()
        if doc["kind"] == "NetworkPolicy"
        for rule in doc["spec"].get("egress", []) + doc["spec"].get("ingress", [])
        for peer in rule.get("to", []) + rule.get("from", [])
        if "ipBlock" in peer
    ]
    assert {b["cidr"] for b in blocks} >= {"0.0.0.0/0", "2000::/3"}
    for block in blocks:
        cidr = ipaddress.ip_network(block["cidr"])
        for net in map(ipaddress.ip_network, block.get("except", [])):
            assert net.version == cidr.version and net != cidr and net.subnet_of(cidr), (
                block["cidr"],
                str(net),
            )


def test_the_limit_service_and_redis_talk_only_to_each_other():
    docs = _workloads()
    rl, redis = _policy(docs, "llm-ratelimit"), _policy(docs, "llm-redis")
    assert rl["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "llm-gateway"}}}],
            "ports": [{"protocol": "TCP", "port": 8081}],
        }
    ]
    assert rl["egress"][1]["ports"] == [{"protocol": "TCP", "port": 6379}]
    assert redis["egress"] == [] and redis["policyTypes"] == ["Ingress", "Egress"]


def test_the_limit_config_has_the_default_the_overrides_and_rendered_providers():
    import yaml

    limits = LlmGatewayLimits(
        tenant_tokens_per_minute=1000, tenants={"acme": 50}, providers={"router": 400, "gone": 9}
    )
    config = yaml.safe_load(tpl.ratelimit_config(limits, {"router": "router"}))
    assert config["domain"] == "daegam-llm"
    assert config["descriptors"] == [
        {"key": "tenant", "rate_limit": {"unit": "minute", "requests_per_unit": 1000}},
        {
            "key": "tenant",
            "value": "acme",
            "rate_limit": {"unit": "minute", "requests_per_unit": 50},
        },
        {
            "key": "provider",
            "value": "router",
            "rate_limit": {"unit": "minute", "requests_per_unit": 400},
        },
    ]
