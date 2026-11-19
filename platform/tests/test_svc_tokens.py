"""내부 MCP 홉의 서비스 토큰: 소비자 객체 렌더, 발급 창구, 포트포워드 브리지 (D41)."""

from __future__ import annotations

import asyncio
import base64
import socket
from types import SimpleNamespace
from typing import Any

import httpx2
import jwt
import pytest

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import server_audience
from daegam.config import Settings
from daegam.k8s.client import ServiceRoute
from daegam.k8s.portforward import PortForwardError, TunnelBridge, ready_pod_for_service
from daegam.k8s.templates.svc_token import (
    existing_svc_token,
    expose_svc_name,
    render_external_auth_config,
    render_svc_token_secret,
    secret_value,
    svc_token_rev,
    vmcp_svc_name,
)
from daegam.services import hub as hub_module
from daegam.services.exposure import expose_object_refs
from daegam.services.svc_tokens import audiences_due, token_audiences
from daegam.store.tenants import TenantSettings
from tests.helpers.consts import ACME_NS
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.loop_guard import allow_on_loop
from tests.helpers.svc_tokens import aud_of, header_echo_server, make_svc_tokens

# -- 렌더 ---------------------------------------------------------------------------------


def test_consumer_objects_render():
    owner = {"apiVersion": "x/v1", "kind": "VirtualMCPServer", "name": "gw", "uid": "u"}
    secret = render_svc_token_secret(
        name=vmcp_svc_name("gw"),
        namespace=ACME_NS,
        token="t.o.k",
        tenant="acme",
        resource="gateway-svc-token",
        id="i-1",
        owner=owner,
    )
    assert secret["metadata"]["name"] == "svc-gw" and secret["type"] == "Opaque"
    assert secret["stringData"] == {"token": "t.o.k", "authorization": "Bearer t.o.k"}
    assert secret["metadata"]["ownerReferences"][0]["uid"] == "u"
    assert secret["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "gateway-svc-token"
    header = render_external_auth_config(
        name="svc-gw",
        namespace=ACME_NS,
        secret_name="svc-gw",
        mode="header",
        tenant="acme",
        resource="gateway-svc-token",
        id="i-1",
    )
    assert header["kind"] == "MCPExternalAuthConfig" and header["spec"] == {
        "type": "headerInjection",
        "headerInjection": {
            "headerName": "Authorization",
            "valueSecretRef": {"name": "svc-gw", "key": "authorization"},
        },
    }
    bearer = render_external_auth_config(
        name=expose_svc_name("mcp-a"),
        namespace=ACME_NS,
        secret_name="svc-expose-mcp-a",
        mode="bearer",
        tenant="acme",
        resource="mcp-expose-svc-token",
        id="i-2",
    )
    assert bearer["spec"] == {
        "type": "bearerToken",
        "bearerToken": {"tokenSecretRef": {"name": "svc-expose-mcp-a", "key": "token"}},
    }
    # 노출 해제는 두 앞단과 함께 토큰 객체도 이름으로 지운다 (P54 6절)
    kinds = [(k, n) for _api, k, n in expose_object_refs("mcp-a", front_proxy=True)]
    assert ("MCPExternalAuthConfig", "svc-expose-mcp-a") in kinds
    assert ("Secret", "svc-expose-mcp-a") in kinds
    assert {("VirtualMCPServer", "front-mcp-a"), ("MCPExternalAuthConfig", "front-mcp-a")} <= set(
        kinds
    )
    assert ("MCPAuthzConfig", "front-mcp-a-authz") in kinds
    assert all(k != "Secret" for _api, k, _n in expose_object_refs("mcp-a", front_proxy=False))


def test_secret_value_reads_both_shapes_and_rev_follows_jti():
    assert secret_value({"stringData": {"token": "abc"}}, "token") == "abc"
    encoded = base64.b64encode(b"xyz").decode()
    assert secret_value({"data": {"token": encoded}}, "token") == "xyz"
    assert secret_value({"data": {"token": "%%%"}}, "token") is None
    assert secret_value(None, "token") is None
    fake = FakeClusterClient()
    assert existing_svc_token(fake, "svc-gw", ACME_NS) is None
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "svc-gw", "namespace": ACME_NS},
            "stringData": {"token": "stored"},
        }
    )
    assert existing_svc_token(fake, "svc-gw", ACME_NS) == "stored"
    svc = make_svc_tokens()
    tok = svc.issuer.mint(sub="svc:vmcp:gw", tenant="acme", groups=[])
    jti = jwt.decode(tok, options={"verify_signature": False})["jti"]
    assert svc_token_rev(tok) == jti[:8] and svc_token_rev(None) == "0"
    assert svc_token_rev("garbage") == "0"
    # Agent 는 두 토큰의 판이다. gateway_token 이 없으면 mcp_token 하나의 판 그대로이고, 어느 쪽이
    # 바뀌어도 값이 바뀐다 (P32 8절)
    gw = svc.issuer.mint_gateway(sub="svc:agent:a", tenant="acme", groups=[])
    gw_jti = jwt.decode(gw, options={"verify_signature": False})["jti"]
    assert svc_token_rev(tok, None) == svc_token_rev(tok) == jti[:8]
    assert svc_token_rev(tok, gw) == f"{jti[:8]}-{gw_jti[:8]}"
    assert svc_token_rev(None, None) == "0"


# -- 발급 창구 ------------------------------------------------------------------------------


async def _test_svc_tokens_mint_ensure_and_platform_token():
    svc = make_svc_tokens(group="team-acme")
    fake = FakeClusterClient()
    first = await svc.ensure(
        fake, secret_name="svc-gw", namespace=ACME_NS, kind="vmcp", name="gw", tenant="acme"
    )
    claims = jwt.decode(first, options={"verify_signature": False})
    assert claims["sub"] == "svc:vmcp:gw" and claims["aud"] == "daegam-internal:acme"
    assert claims["groups"] == ["team-acme"] and claims["tenant"] == "acme"
    # Secret 에 있으면 그것을 다시 쓴다 (토큰이 굳는다)
    with allow_on_loop():
        fake.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "svc-gw", "namespace": ACME_NS},
                "stringData": {"token": first},
            }
        )
    again = await svc.ensure(
        fake, secret_name="svc-gw", namespace=ACME_NS, kind="vmcp", name="gw", tenant="acme"
    )
    assert again == first
    # 사람 경계(vMCP)에 가는 토큰은 저장된 mcp_group 이 아니라 고정 그룹을 싣는다. 내부 서버에 가는
    # 토큰(위의 ensure, 아래의 platform_token)은 그 그룹을 그대로 싣는다 (P53b 6.1절)
    gw = jwt.decode(
        await svc.mint_gateway(name="ai", tenant="acme"), options={"verify_signature": False}
    )
    assert (
        gw["aud"] == "daegam-mcp"
        and gw["sub"] == "svc:agent:ai"
        and gw["groups"] == ["daegam-acme"]
    )
    probe = jwt.decode(
        await svc.platform_gateway_token("acme"), options={"verify_signature": False}
    )
    assert probe["aud"] == "daegam-mcp" and probe["groups"] == ["daegam-acme"]
    plat = jwt.decode(await svc.platform_token("acme"), options={"verify_signature": False})
    assert plat["sub"] == "svc:platform" and plat["exp"] - plat["iat"] <= 300
    assert plat["groups"] == ["team-acme"]
    # 기본 그룹은 daegam-<tenant>
    assert (await make_svc_tokens().groups("acme")) == ["daegam-acme"]
    assert svc.available


TENANT_AUD = "daegam-internal:acme"
SERVER_AUD = "daegam-internal:acme:s1"


def test_server_audience_and_the_renewal_rule():
    """소비자가 서버 하나에 내는 토큰은 테넌트 audience 와 그 서버의 audience 를 함께 싣는다 (P50
    9절). 원하는 audience 가 빠졌거나, 그것과 물러나는 테넌트 audience 밖의 것을 실으면 갱신할
    때다. 서버를 모르는 토큰(Agent 의 옛 단일 토큰)은 테넌트 audience 하나다."""
    assert server_audience("acme", "s1") == SERVER_AUD
    with pytest.raises(ValueError):
        server_audience("acme", "")
    assert token_audiences("acme", None) == [TENANT_AUD]
    assert token_audiences("acme", "s1") == [TENANT_AUD, SERVER_AUD]
    issuer = make_svc_tokens().issuer

    def mint(audiences: list[str] | None) -> str:
        return issuer.mint(sub="svc:vmcp:gw", tenant="acme", groups=[], audiences=audiences)

    both, legacy = mint(token_audiences("acme", "s1")), mint(None)
    other, only_server = mint(token_audiences("acme", "s2")), mint([SERVER_AUD])
    assert aud_of(both) == [TENANT_AUD, SERVER_AUD] and aud_of(legacy) == TENANT_AUD
    peek = InternalIssuer.peek(both)
    assert peek is not None and peek.audiences == (TENANT_AUD, SERVER_AUD)
    assert InternalIssuer.peek(only_server).audiences == (SERVER_AUD,)
    # 서버 s1 을 여는 토큰
    assert not audiences_due(both, "acme", "s1")
    assert audiences_due(legacy, "acme", "s1")  # 서버 audience 가 빠졌다
    assert audiences_due(other, "acme", "s1")  # 다른 서버의 audience 를 싣는다
    assert audiences_due(only_server, "acme", "s1")  # 1단계의 서버는 아직 테넌트 audience 를 본다
    # 서버를 모르는 토큰
    assert not audiences_due(legacy, "acme", None)
    assert audiences_due(both, "acme", None)
    assert audiences_due(None, "acme", "s1") and audiences_due("garbage", "acme", None)


async def _test_ensure_renews_a_token_without_the_server_audience():
    svc = make_svc_tokens()
    fake = FakeClusterClient()

    def stored(token: str) -> None:
        with allow_on_loop():
            fake.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "svc-gw-1", "namespace": ACME_NS},
                    "stringData": {"token": token},
                }
            )

    legacy = await svc.mint(kind="vmcp", name="gw", tenant="acme")
    assert aud_of(legacy) == TENANT_AUD
    stored(legacy)
    kw: dict[str, Any] = {"secret_name": "svc-gw-1", "namespace": ACME_NS, "kind": "vmcp"}
    kw.update(name="gw", tenant="acme", server_id="s1")
    # Secret 의 토큰이 서버 audience 를 싣지 않으면 다시 쓰지 않고 새로 낸다
    renewed = await svc.ensure(fake, **kw)
    assert renewed != legacy and aud_of(renewed) == [TENANT_AUD, SERVER_AUD]
    stored(renewed)
    assert await svc.ensure(fake, **kw) == renewed
    # 플랫폼 자신의 tools/list 토큰도 같은 함수다
    assert aud_of(await svc.platform_token("acme", "s1")) == [TENANT_AUD, SERVER_AUD]
    assert aud_of(await svc.platform_token("acme")) == TENANT_AUD


def test_ensure_renews_a_token_without_the_server_audience():
    asyncio.run(_test_ensure_renews_a_token_without_the_server_audience())


# -- 포트포워드 브리지 --------------------------------------------------------------------------


async def _test_tunnel_bridge_carries_headers_and_opens_one_tunnel_per_connection():
    server, port = await header_echo_server()
    opened: list[int] = []
    closed: list[int] = []

    async def factory() -> tuple[socket.socket, Any]:
        s = socket.create_connection(("127.0.0.1", port))
        opened.append(s.fileno())
        return s, lambda: closed.append(1)

    try:
        async with TunnelBridge(factory) as base_url:
            async with httpx2.AsyncClient(base_url=base_url) as hc:
                r = await hc.post("/mcp", headers={"Authorization": "Bearer svc-token"}, json={})
                assert r.status_code == 200
                got = r.json()
                assert got["headers"]["authorization"] == "Bearer svc-token"
                assert got["request"].startswith("POST /mcp ")
                r2 = await hc.get("/x")
                assert r2.status_code == 200
        assert len(opened) == 2 and len(closed) == 2
    finally:
        server.close()
        await server.wait_closed()


def test_tunnel_bridge_does_not_wait_for_a_late_tunnel_and_closes_it():
    """호출자의 시한이 터널이 열리기 전에 끝나면 브리지는 그 터널을 기다리지 않고 닫힌다. 늦게
    열린 터널은 남기지 않고 바로 닫는다(T21 의 3초 상한)."""
    asyncio.run(_tunnel_bridge_does_not_wait_for_a_late_tunnel_and_closes_it())


async def _tunnel_bridge_does_not_wait_for_a_late_tunnel_and_closes_it():
    release = asyncio.Event()
    closed: list[str] = []
    sockets = socket.socketpair()

    async def slow_factory() -> tuple[socket.socket, Any]:
        await release.wait()
        return sockets[0], lambda: closed.append("tunnel")

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            async with TunnelBridge(slow_factory) as base_url:
                async with httpx2.AsyncClient(base_url=base_url) as hc:
                    await hc.get("/healthz")
    assert loop.time() - started < 1.0  # 닫기가 5초 wait_closed 를 기다리지 않는다
    release.set()
    for _ in range(100):
        if closed:
            break
        await asyncio.sleep(0.01)
    sockets[1].close()
    assert closed == ["tunnel"]
    assert sockets[0].fileno() == -1


def test_ready_pod_for_service_resolves_named_target_port():
    fake = FakeClusterClient()
    fake.apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "mcp-a-proxy", "namespace": ACME_NS},
            "spec": {
                "selector": {"app": "a"},
                "ports": [{"port": 8080, "targetPort": "http"}],
            },
        }
    )
    with pytest.raises(PortForwardError, match="no ready pod"):
        ready_pod_for_service(fake, ACME_NS, "mcp-a-proxy", 8080)
    for name, ready in (("a-0", "False"), ("a-1", "True")):
        fake.apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": ACME_NS, "labels": {"app": "a"}},
                "spec": {
                    "containers": [
                        {"name": "c", "ports": [{"name": "http", "containerPort": 9090}]}
                    ]
                },
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": ready}]},
            }
        )
    assert ready_pod_for_service(fake, ACME_NS, "mcp-a-proxy", 8080) == ("a-1", 9090)
    with pytest.raises(PortForwardError, match="not found"):
        ready_pod_for_service(fake, ACME_NS, "missing", 8080)


async def _test_default_tool_lister(monkeypatch):
    seen: list[tuple[str, str | None, Any]] = []

    class _Res:
        tools = ["Add"]

    async def fake_list_tools(url, *, timeout, http_client=None, headers=None):
        auth = http_client.headers.get("Authorization") if http_client else None
        key = http_client.headers.get("X-API-Key") if http_client else None
        seen.append((url, auth, headers or key or "x"))
        return _Res()

    import daegam.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)

    class _Client:
        def __init__(self, via: str):
            self.via = via

        def service_route(self, namespace, service, port, *, scheme="http"):
            return ServiceRoute(
                base_url=f"{scheme}://{service}.{namespace}.svc.cluster.local:{port}", via=self.via
            )

    url = f"http://mcp-a-proxy.{ACME_NS}.svc.cluster.local:8080/mcp"
    tok = {"Authorization": "Bearer tok"}
    # 직행: 헤더를 실은 채 Service 주소로
    assert await hub_module._default_tool_lister(_Client("direct"), url, tok) == ["Add"]
    assert seen[-1][0] == url and seen[-1][1] == "Bearer tok"
    # API 서버 프록시 + 토큰 없음: 예전 그대로 프록시 경로
    assert await hub_module._default_tool_lister(_Client("apiserver"), url, None) == ["Add"]
    assert seen[-1][1] is None
    # Authorization 이 아닌 헤더(외부 서버의 고정 헤더, P86 6.3절)는 프록시가 지우지 않아 그 경로다
    key = {"X-API-Key": "k1"}
    assert await hub_module._default_tool_lister(_Client("apiserver"), url, key) == ["Add"]
    assert seen[-1] == (url, None, "k1")

    # API 서버 프록시 + 토큰: 브리지로 간다 (Authorization 을 지우는 경로를 피한다)
    import contextlib

    import daegam.k8s.portforward as pf

    bridged: list[dict[str, str]] = []

    @contextlib.asynccontextmanager
    async def fake_portforward_client(
        client, namespace, service, port, *, headers, timeout, read_timeout=None
    ):
        bridged.append(dict(headers))
        async with httpx2.AsyncClient(base_url="http://127.0.0.1:1", headers=headers) as hc:
            yield hc

    monkeypatch.setattr(pf, "portforward_client", fake_portforward_client)
    assert await hub_module._default_tool_lister(_Client("apiserver"), url, tok) == ["Add"]
    assert bridged == [{"Authorization": "Bearer tok"}]
    assert seen[-1][0] == "http://127.0.0.1:1/mcp" and seen[-1][1] == "Bearer tok"
    # 외부 URL 은 그대로이고 헤더는 list_tools 가 만드는 클라이언트에 싣는다
    assert await hub_module._default_tool_lister(_Client("apiserver"), "https://ext/mcp", key)
    assert seen[-1] == ("https://ext/mcp", None, key)


def test_svc_tokens_mint_ensure_and_platform_token():
    asyncio.run(_test_svc_tokens_mint_ensure_and_platform_token())


async def _test_hub_bearer_follows_the_item_and_the_auth_mode():
    from daegam.store.hub import HubItem, HubK8s, HubSource

    def item(kind: str, cr: str | None, **fields: Any) -> HubItem:
        return HubItem(
            tenant="acme",
            name="x",
            display_name="x",
            kind=kind,
            cluster_id="c",
            k8s=HubK8s(kind=cr, name="x", namespace=ACME_NS),
            **fields,
        )

    def hub(settings: Settings) -> hub_module.HubService:
        # 외부 서버 헤더의 저장소(P86 6.1절)만 클러스터 서비스와 항목 저장소에서 읽는다
        cluster = SimpleNamespace(secret_store=lambda: None, settings=settings)
        tenancy = SimpleNamespace(platform_settings=SimpleNamespace(install_id=None))
        return hub_module.HubService(
            SimpleNamespace(existing_ids=None),
            tenancy,
            cluster,
            None,
            tool_names=None,
            svc_tokens=make_svc_tokens(settings=settings),
            header_ledger=None,
            env_ledger=None,
        )  # type: ignore[arg-type]

    local, oidc = hub(Settings(auth_mode="local")), hub(Settings())
    gateway = item("published", "VirtualMCPServer")
    # 프록시 없이 게시한 Gateway 는 vMCP 에 바로 묻는다. vMCP 는 사람 발급자의 aud: daegam-mcp
    # 토큰만 받고 Cedar 가 테넌트 그룹을 보므로 local 모드에서만 그 모양으로 낸다
    claims = jwt.decode(await local._bearer_for(gateway), options={"verify_signature": False})
    assert claims["sub"] == "svc:platform" and claims["aud"] == "daegam-mcp"
    assert claims["groups"] == ["daegam-acme"] and claims["exp"] - claims["iat"] <= 300
    assert await oidc._bearer_for(gateway) is None
    # proxy 모드 게시와 원격은 프록시가 받는다. 토큰을 싣지 않는다
    assert await local._bearer_for(item("published", "MCPRemoteProxy")) is None
    assert await local._bearer_for(item("remote", "MCPRemoteProxy")) is None
    # 우리 MCPServer 는 모드와 무관하게 서비스 토큰이고 그 서버의 audience 를 함께 싣는다 (P50
    # 9절). internal 은 항목의 MCP Server 가, image 는 항목 자신이 서버다
    source = HubSource(instance_id="mi-1")
    internal = await oidc._bearer_for(item("internal", "MCPServer", source=source))
    assert aud_of(internal) == [TENANT_AUD, "daegam-internal:acme:mi-1"]
    image = item("image", "MCPServer")
    assert aud_of(await local._bearer_for(image)) == [TENANT_AUD, f"{TENANT_AUD}:{image.id}"]


def test_hub_bearer_follows_the_item_and_the_auth_mode():
    asyncio.run(_test_hub_bearer_follows_the_item_and_the_auth_mode())


def test_tunnel_bridge_carries_headers_and_opens_one_tunnel_per_connection():
    asyncio.run(_test_tunnel_bridge_carries_headers_and_opens_one_tunnel_per_connection())


def test_default_tool_lister_picks_the_bridge_only_off_home_with_a_bearer(monkeypatch):
    asyncio.run(_test_default_tool_lister(monkeypatch))


# -- Gateway 소스의 인증 종류 (모드별) ---------------------------------------------------------


def test_tool_auth_follows_the_auth_mode():
    from daegam.services.agent_graph import GraphContext, _tool_auth, gateway_auth_kind
    from daegam.store.tenants import AgentOAuth

    oauth = AgentOAuth(
        client_id="agent", token_url="https://idp/token", scope="mcp", secret_name="agent-oauth"
    )

    def ctx(mode: str | None, token_auth: bool) -> GraphContext:
        return GraphContext(
            providers={},
            has_key=lambda p: True,
            token_auth=token_auth,
            auth_mode=mode,
            tenant_settings=TenantSettings(tenant="a", agent_oauth=oauth),
        )

    # MCP Instance 는 모드와 무관하게 플랫폼 서비스 토큰이다. 표를 모르는 파드는 옛 단일 토큰이다
    for mode in ("header", "local", "oidc"):
        assert _tool_auth("mcp_instance", ctx(mode, mode != "header"), "mi-1") == {
            "kind": "bearer",
            "secret_env": "DAEGAM_MCP_TOKEN",
        }
    assert gateway_auth_kind(ctx("header", False)) == "none"
    assert _tool_auth("gateway_instance", ctx("header", False), "gw-1") == {"kind": "none"}
    assert gateway_auth_kind(ctx("local", True)) == "bearer"
    assert _tool_auth("gateway_instance", ctx("local", True), "gw-1") == {
        "kind": "bearer",
        "secret_env": "DAEGAM_GATEWAY_TOKEN",
    }
    assert gateway_auth_kind(ctx("oidc", True)) == "oauth"
    oidc = _tool_auth("gateway_instance", ctx("oidc", True), "gw-1")
    assert oidc["kind"] == "oauth_client_credentials"
    # 표를 아는 파드는 소스마다 그 env 이고, 표에 없는 소스는 옛 단일 토큰이다. 그 env 는 모든
    # 템플릿에 있어 파드가 준비된 채로 남는다 (P50 9절)
    with_table = ctx("oidc", True)
    with_table.mcp_token_envs = {"mi-1": "DAEGAM_MCP_TOKEN_MI1"}
    assert _tool_auth("mcp_instance", with_table, "mi-1") == {
        "kind": "bearer",
        "secret_env": "DAEGAM_MCP_TOKEN_MI1",
    }
    assert _tool_auth("mcp_instance", with_table, "mi-2") == {
        "kind": "bearer",
        "secret_env": "DAEGAM_MCP_TOKEN",
    }
    # 옛 호출부(auth_mode 없음)는 token_auth 로 oidc 여부를 짐작한다
    assert (
        gateway_auth_kind(ctx(None, True)) == "oauth"
        and gateway_auth_kind(ctx(None, False)) == "none"
    )
