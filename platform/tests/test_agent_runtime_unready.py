"""T21: 준비되지 않은 Agent 파드의 이유 (D21, `services/agent_runtime.py`).

Service 는 Ready 파드로만 가므로 파드로 바로 가는 클라이언트(`bearer_pod_client`), healthz 상세의
`unready` 읽기(`unready_text`·`AgentRuntimeProxy.unready_reason`), 그 이유를 상세 상태의 message 에
싣는 것을 든다. 런타임 프록시와 터널 경로는 test_agent_runtime_proxy.py 가 본다.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

import daegam.k8s.portforward as pf
from daegam.k8s.portforward import PortForwardError
from daegam.services.agent_runtime import unready_text
from tests.helpers.agent_runtime import (
    deployed_agent,
)
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.runtime import RUNTIME_POD, fake_runtime
from tests.helpers.svc_tokens import header_echo_server

UNREADY = {
    "code": "dependencies_unavailable",
    "message": "memory_unreachable: 세션 저장소(Redis)에 연결할 수 없다",
}


def test_bearer_pod_client_reaches_the_given_pod_by_ip_or_tunnel(monkeypatch) -> None:
    """Service 는 Ready 파드로만 가므로 준비되지 않은 파드에는 바로 닿는다. 직행이면 파드 IP 로,
    아니면 그 파드로 여는 터널로 간다. 둘 다 Bearer 를 싣고 터널은 Service 주소의 Host 를 싣는다."""
    asyncio.run(_bearer_pod_client_reaches_the_given_pod_by_ip_or_tunnel(monkeypatch))


async def _bearer_pod_client_reaches_the_given_pod_by_ip_or_tunnel(monkeypatch) -> None:
    server, port = await header_echo_server()
    tunnels: list[tuple[str, int]] = []

    def fake_open_tunnel(client, namespace, name, target):
        tunnels.append((name, target))
        return socket.create_connection(("127.0.0.1", port)), lambda: None

    monkeypatch.setattr(pf, "open_tunnel", fake_open_tunnel)
    fake = FakeClusterClient()
    pod = {"metadata": {"name": "agent-x-1"}, "status": {"podIP": "127.0.0.1"}}
    try:
        async with pf.bearer_pod_client(
            fake, DEFAULT_NS, "agent-x", pod, port, bearer="tok", timeout=2.0
        ) as (hc, base_url):
            assert base_url == f"http://127.0.0.1:{port}"
            direct = (await hc.get(f"{base_url}/healthz")).json()
        fake.service_via = "apiserver"
        async with pf.bearer_pod_client(
            fake, DEFAULT_NS, "agent-x", pod, 8080, bearer="tok", timeout=2.0
        ) as (hc, base_url):
            tunneled = (await hc.get(f"{base_url}/healthz")).json()
        no_ip = {"metadata": {"name": "agent-x-2"}, "status": {}}
        fake.service_via = "direct"
        with pytest.raises(PortForwardError, match="no IP"):
            async with pf.bearer_pod_client(
                fake, DEFAULT_NS, "agent-x", no_ip, 8080, bearer="t", timeout=1
            ):
                pass
    finally:
        server.close()
        await server.wait_closed()
    assert tunnels == [("agent-x-1", 8080)]
    for got in (direct, tunneled):
        assert got["request"].startswith("GET /healthz ")
        assert got["headers"]["authorization"] == "Bearer tok"
    assert tunneled["headers"]["host"] == f"agent-x.{DEFAULT_NS}.svc:8080"


def test_unready_text_reads_the_unready_field() -> None:
    """healthz 상세의 `unready` 를 `code: message` 로 옮긴다. 칸이 없거나 모양이 다르면
    None 이다."""
    assert unready_text({"status": "starting", "unready": UNREADY}) == (
        "dependencies_unavailable: memory_unreachable: 세션 저장소(Redis)에 연결할 수 없다"
    )
    assert unready_text({"status": "ok", "unready": {"code": "tools_unavailable"}}) == (
        "tools_unavailable"
    )
    assert unready_text({"status": "ok", "unready": None}) is None
    assert unready_text({"status": "ok"}) is None  # 옛 이미지
    assert unready_text(["not", "a", "dict"]) is None


def test_unready_reason_reads_the_pod_health_and_never_raises(
    client, fake_clusters, mongo, monkeypatch
):
    """준비되지 않은 파드의 healthz 를 인스턴스 토큰으로 읽는다. 토큰이 틀려 상세가 없거나 파드에
    연결할 수 없으면 None 이고 상태 조회를 막지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo)
    fake = agent.fake
    svc = client.app.state.agent_service
    doc = client.portal.call(svc.agents.get, agent.id)
    pod = {"metadata": {"name": RUNTIME_POD}, "status": {"podIP": "10.0.0.7"}}
    fake.service_app.state.health = {"status": "starting", "unready": UNREADY}
    assert client.portal.call(svc.proxy.unready_reason, doc, fake, pod) == (
        "dependencies_unavailable: memory_unreachable: 세션 저장소(Redis)에 연결할 수 없다"
    )
    calls = fake.service_app.state.calls
    assert [(c["path"], c["headers"]["authorization"]) for c in calls] == [
        ("/healthz", f"Bearer {agent.token}")
    ]
    fake.service_app = fake_runtime("other-token")
    fake.service_app.state.health = {"status": "starting", "unready": UNREADY}
    assert client.portal.call(svc.proxy.unready_reason, doc, fake, pod) is None
    fake.service_via = "apiserver"

    def broken_tunnel(*_args: Any) -> Any:
        raise PortForwardError("pods/portforward refused")

    monkeypatch.setattr(pf, "open_tunnel", broken_tunnel)
    assert client.portal.call(svc.proxy.unready_reason, doc, fake, pod) is None
