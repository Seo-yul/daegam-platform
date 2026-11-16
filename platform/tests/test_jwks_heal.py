"""T24: 플랫폼보다 먼저 떠 JWKS 를 받지 못한 검증기를 다시 띄운다 (`services/jwks_heal.py`).

검증기 고르기와 잠김 판정은 순수 함수로 본다. 리컨실 단계는 노출한 MCP Server 하나(내부 MCPServer 와
노출 앞단)와 그것을 쓰는 Gateway 를 띄운 가짜 클러스터에서 부른다. 가짜 데이터 플레인은 ToolHive
검증기처럼 MCP 의 Accept 가 없는 요청을 406 으로 끊고, 잠겼을 때의 401 을 흉내 낸다. 플랫폼 자신의
JWKS 를 받는지는 테스트가 정한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from daegam.auth.signer import server_audience
from daegam.k8s.client import RESTARTED_AT
from daegam.services import jwks_heal
from daegam.services.jwks_heal import (
    RESTART_COOLDOWN,
    JwksHeal,
    jwks_locked,
    pick_verifiers,
    platform_config_names,
)
from daegam.services.tenancy import (
    INTERNAL_OIDC_CONFIG_NAME,
    OIDC_CONFIG_NAME,
    TOOLHIVE_API,
    oidc_config_manifest,
)
from daegam.store.base import utcnow
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.exposure import front_and_gateway
from tests.helpers.fake_cluster import FakeClusterClient

LOCKED = (
    'Bearer realm="http://daegam-platform.daegam-system.svc:9000", error="invalid_token", '
    'error_description="failed to parse token: token is unverifiable: error while executing '
    'keyfunc: failed to lookup JWKS: resource not ready"'
)
UNKNOWN_KID = 'Bearer error="invalid_token", error_description="key ID abc not found in JWKS"'
INTERNAL = frozenset({INTERNAL_OIDC_CONFIG_NAME})
BOTH = frozenset({INTERNAL_OIDC_CONFIG_NAME, OIDC_CONFIG_NAME})


def test_only_a_failed_key_lookup_counts_as_locked() -> None:
    """잠김은 401 이면서 키를 조회하지 못한 것이다. 키 묶음에 없는 kid(IdP 를 쓰는 검증기에 플랫폼
    토큰을 보낸 것), 등록 실패, audience 거절, 통과는 잠김이 아니다."""
    assert jwks_locked(401, LOCKED)
    assert not jwks_locked(401, 'Bearer error_description="JWKS registration failed: x"')
    assert not jwks_locked(401, UNKNOWN_KID)
    assert not jwks_locked(401, 'Bearer error_description="invalid audience"')
    assert not jwks_locked(406, LOCKED)
    assert not jwks_locked(200, "")


def _cr(
    name: str, ref: str | None, *, ns: str = "ns", url: str | None = "http://x.ns.svc:8080/mcp"
):
    return {
        "metadata": {"name": name, "namespace": ns, "uid": f"uid-{name}"},
        "spec": {"oidcConfigRef": {"name": ref}, "replicas": 2} if ref else {},
        "status": {"url": url} if url else {},
    }


def test_verifiers_are_the_crs_in_our_namespaces_that_use_the_platform_jwks(
    offline_settings,
) -> None:
    """서비스 토큰용 MCPOIDCConfig 를 쓰는 CR 은 언제나, 사람 토큰용은 플랫폼이 그 발급자인 local
    모드에서만 고른다. 이 설치의 네임스페이스 밖이거나 주소가 아직 없는 CR 은 뺀다. vMCP 의
    status.url 에는 경로가 없어 `/mcp` 를 붙인다."""
    assert platform_config_names(replace(offline_settings, auth_mode="header")) == INTERNAL
    assert platform_config_names(replace(offline_settings, auth_mode="local")) == BOTH
    objs = [
        _cr("internal", INTERNAL_OIDC_CONFIG_NAME),
        _cr("human", OIDC_CONFIG_NAME),
        _cr("anonymous", None),
        _cr("starting", INTERNAL_OIDC_CONFIG_NAME, url=None),
        _cr("theirs", INTERNAL_OIDC_CONFIG_NAME, ns="other"),
    ]
    ref = ("spec", "oidcConfigRef", "name")
    tenants = {"ns": "t"}
    header = pick_verifiers("MCPServer", ref, objs, INTERNAL, tenants)
    assert [v.name for v in header] == ["internal"]
    assert (header[0].tenant, header[0].replicas) == ("t", 2)
    local = pick_verifiers("MCPServer", ref, objs, BOTH, tenants)
    assert [v.name for v in local] == ["internal", "human"]
    vmcp = {
        "metadata": {"name": "gw", "namespace": "ns", "uid": "uid-gw"},
        "spec": {"incomingAuth": {"oidcConfigRef": {"name": OIDC_CONFIG_NAME}}},
        "status": {"url": "http://vmcp-gw.ns.svc.cluster.local:4483"},
    }
    incoming = ("spec", "incomingAuth", "oidcConfigRef", "name")
    (gateway,) = pick_verifiers("VirtualMCPServer", incoming, [vmcp], BOTH, tenants)
    assert gateway.url == "http://vmcp-gw.ns.svc.cluster.local:4483/mcp"


def _verifier_app(answers: dict[str, Any]) -> Any:
    """ToolHive 검증기를 흉내 내는 ASGI 앱이다. MCP 의 Accept(text/event-stream)가 없으면 인증보다
    먼저 406 이다(vMCP). `answers["challenge"]` 가 있으면 그 이유의 401 이고 없으면 200 이다. 받은
    경로와 Authorization 을 남긴다."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        headers = dict(scope["headers"])
        answers["calls"].append((scope["path"], headers.get(b"authorization", b"").decode()))
        challenge = answers.get("challenge")
        if b"text/event-stream" not in headers.get(b"accept", b""):
            status, extra = 406, []
        elif challenge:
            status, extra = 401, [(b"www-authenticate", challenge.encode())]
        else:
            status, extra = 200, []
        await send({"type": "http.response.start", "status": status, "headers": extra})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


def _heal(client, monkeypatch, *, served: bool = True) -> JwksHeal:
    """배선이 만든 단계다. 플랫폼 자신의 JWKS 를 받는지는 served 로 정한다."""
    heal = client.app.state.jwks_heal
    assert dict(client.app.state.reconciler.extra_steps)["jwks_heal"] == heal.sweep

    async def jwks_served() -> bool:
        return served

    monkeypatch.setattr(heal, "_jwks_served", jwks_served)
    return heal


def _locked_server(client, fake_clusters, answers: dict[str, Any]) -> tuple[str, Any]:
    """노출한 MCP Server(내부 MCPServer)와 Gateway 를 띄우고 프록시 Deployment 를 둔다."""
    front, _gateway, fake = front_and_gateway(client, fake_clusters)
    name = front["k8s"]["name"]
    fake.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": DEFAULT_NS},
        }
    )
    fake.service_app = _verifier_app(answers)
    return name, fake


def test_a_locked_verifier_is_restarted_then_left_alone_while_it_rolls_and_once_healthy(
    client, fake_clusters, mongo, monkeypatch
) -> None:
    """잠긴 내부 MCPServer 는 그 Deployment 를 다시 띄운다. 쿨다운 동안은 부르지 않고, 지나서
    정상이면 그 뒤로 부르지 않는다. header 모드라 사람 토큰용(Gateway·노출 앞단)은 부르지 않는다.
    플랫폼 자신의 JWKS 를 받지 못하는 틱은 아무것도 부르지 않는다. 그때 부르면 프로브가 첫 토큰이
    되어 멀쩡한 검증기를 잠근다."""
    answers: dict[str, Any] = {"calls": [], "challenge": LOCKED}
    name, fake = _locked_server(client, fake_clusters, answers)
    now = utcnow()
    heal = _heal(client, monkeypatch, served=False)
    assert client.portal.call(heal.sweep, now) == {"restarted": [], "skipped": ["jwks"]}
    assert answers["calls"] == [] and fake.restarts == []
    heal = _heal(client, monkeypatch)

    assert client.portal.call(heal.sweep, now) == {
        "restarted": [f"edge/{DEFAULT_NS}/MCPServer/{name}"],
        "skipped": [],
    }
    assert fake.restarts == [(DEFAULT_NS, name)]
    deployment = fake.get("apps/v1", "Deployment", name, DEFAULT_NS)
    assert RESTARTED_AT in deployment["spec"]["template"]["metadata"]["annotations"]
    ((path, auth),) = answers["calls"]
    assert path == "/mcp" and auth.startswith("Bearer ")

    # 롤아웃 중에는 잠긴 옛 파드가 Service 뒤에 남아도 다시 굴리지 않는다
    assert client.portal.call(heal.sweep, now + timedelta(seconds=60))["restarted"] == []
    assert len(answers["calls"]) == 1
    answers["challenge"] = None
    later = now + RESTART_COOLDOWN
    assert client.portal.call(heal.sweep, later)["restarted"] == []
    assert len(answers["calls"]) == 2
    assert client.portal.call(heal.sweep, later)["restarted"] == []
    assert len(answers["calls"]) == 2  # 확인한 검증기는 다시 부르지 않는다
    assert fake.restarts == [(DEFAULT_NS, name)]


def test_a_locked_gateway_is_found_through_its_accept_check(offline_settings) -> None:
    """local 모드의 vMCP 는 인증보다 앞에서 Accept 를 보므로 프로브가 MCP 의 Accept 를 실어야 잠김에
    닿지 않고 인증까지 간다. status.url 에 경로가 없어도 `/mcp` 로 부른다."""
    fake = FakeClusterClient()
    install = "install-1"
    fake.apply(oidc_config_manifest("t", "ns", "http://platform", None, install_id=install))
    fake.apply(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "VirtualMCPServer",
            "metadata": {"name": "gw", "namespace": "ns"},
            "spec": {"incomingAuth": {"oidcConfigRef": {"name": OIDC_CONFIG_NAME}}},
            "status": {"url": "http://vmcp-gw.ns.svc.cluster.local:4483"},
        }
    )
    answers: dict[str, Any] = {"calls": [], "challenge": LOCKED}
    fake.service_app = _verifier_app(answers)
    (gateway,) = jwks_heal.platform_verifiers(fake, install, BOTH)

    class Tokens:
        async def platform_token(self, tenant: str, server_id: str | None = None) -> str:
            return f"token-{tenant}"

    heal = JwksHeal(None, Tokens(), replace(offline_settings, auth_mode="local"))  # type: ignore[arg-type]
    assert asyncio.run(heal._probe(fake, gateway)) == "locked"
    assert answers["calls"] == [("/mcp", "Bearer token-t")]


def test_a_switched_server_is_probed_with_its_own_audience(offline_settings) -> None:
    """자기 audience 로 옮겨 간 내부 MCPServer(P50 9절)는 CR 의 audience 에서 읽은 서버 id 로 낸
    토큰으로 부른다. 그래서 잠기지 않은 검증기는 옮겨 가기 전처럼 토큰을 받는다. audience 가 맞지
    않아 거절돼도 키를 찾은 뒤의 거절이라 잠긴 것이 아니다."""
    fake = FakeClusterClient()
    install = "install-1"
    fake.apply(oidc_config_manifest("t", "ns", "http://platform", None, install_id=install))
    fake.apply(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPServer",
            "metadata": {"name": "calc", "namespace": "ns"},
            "spec": {
                "oidcConfigRef": {
                    "name": INTERNAL_OIDC_CONFIG_NAME,
                    "audience": server_audience("t", "s1"),
                }
            },
            "status": {"url": "http://mcp-calc-proxy.ns.svc.cluster.local:8080/mcp"},
        }
    )
    answers: dict[str, Any] = {"calls": []}
    fake.service_app = _verifier_app(answers)
    (server,) = jwks_heal.platform_verifiers(fake, install, platform_config_names(offline_settings))
    asked: list[tuple[str, str | None]] = []

    class Tokens:
        async def platform_token(self, tenant: str, server_id: str | None = None) -> str:
            asked.append((tenant, server_id))
            return f"token-{tenant}"

    heal = JwksHeal(None, Tokens(), offline_settings)  # type: ignore[arg-type]
    assert asyncio.run(heal._probe(fake, server)) == "ok"
    assert asked == [("t", "s1")]
    answers["challenge"] = 'Bearer error="invalid_token", error_description="invalid audience"'
    assert asyncio.run(heal._probe(fake, server)) == "ok"
