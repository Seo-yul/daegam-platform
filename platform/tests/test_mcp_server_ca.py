"""관리자가 준 CA 묶음(`network.ca_bundle`)이 MCP Server 의 mcp 컨테이너에 닿는 길을 본다
(D88).

배포가 그 값을 배포 스냅샷(`effective_config.ca_bundle`)에 적고 MCPServer env `CA_ENV` 로
싣는다. 설정을 바꿔도 다음 배포까지는 스냅샷의 값이다. API 는 PEM 대신 지문을 내고, 상태 API 는
지금 설정과 다른 CA 로 도는 서버를 센다. mcp-runtime 이 같은 env 이름으로 읽는다.
"""

from __future__ import annotations

import ast
import base64
from pathlib import Path

from daegam.config.resolve import resolve_effective
from daegam.k8s.templates import mcpserver
from daegam.pem import ca_sha256
from daegam.services.data_plane_auth import KIND_MCP_SERVER
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import TenantSettings
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.docs import cluster_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.source import MCP_RUNTIME, find_assignment, parse_python
from tests.helpers.tls import ca_pem
from tests.helpers.workers import raise_epoch, run_data_plane_auth


def _ca_env(fake: FakeClusterClient, name: str) -> str | None:
    """MCPServer env 의 CA 를 풀어 PEM 으로 준다. 없으면 None 이다."""
    cr = fake.get(GV, "MCPServer", name, DEFAULT_NS)
    assert cr is not None
    value = {e["name"]: e.get("value") for e in cr["spec"].get("env", [])}.get(mcpserver.CA_ENV)
    return base64.b64decode(value).decode() if value else None


def _set_ca(client, pem: str | None) -> None:
    r = client.patch("/settings", json={"network": {"ca_bundle": pem}})
    assert r.status_code == 200, r.text


def _stale_ca(client) -> int:
    r = client.get("/settings/data-plane-auth")
    assert r.status_code == 200, r.text
    return r.json()["mcp_servers_stale_ca"]


def test_the_ca_reaches_the_server_at_its_deploy_and_leaves_at_its_redeploy(
    client, fake_clusters, mongo, monkeypatch
):
    _set_ca(client, ca_pem())
    *_, ms = mcp_server_env(client, backend_auth="basic")
    r = clone_and_deploy(client, ms)
    assert r.status_code == 201, r.text
    inst = r.json()
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    name = inst["k8s"]["name"]
    assert _ca_env(fake, name) == ca_pem()
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})["effective_config"]
    assert stored["ca_bundle"] == ca_pem()
    # 응답은 PEM 대신 지문이다
    assert "ca_bundle" not in inst["effective_config"]
    assert inst["effective_config"]["ca_bundle_sha256"] == ca_sha256(ca_pem())
    preview = client.post(f"{MS}/{inst['id']}/revisions/1/preview", json={}).json()
    assert preview["effective_config"]["ca_bundle_sha256"] == ca_sha256(ca_pem())
    assert _stale_ca(client) == 0

    # 설정을 지워도 다시 렌더는 배포가 적은 CA 를 싣고, 상태 API 가 그 서버를 센다. 재배포가 지운다
    _set_ca(client, None)
    raise_epoch(monkeypatch, KIND_MCP_SERVER)
    assert inst["id"] in run_data_plane_auth(client)["reapplied"]
    assert _ca_env(fake, name) == ca_pem()
    assert _stale_ca(client) == 1
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert _ca_env(fake, name) is None
    assert r.json()["effective_config"]["ca_bundle_sha256"] is None
    assert _stale_ca(client) == 0


def test_only_mcp_servers_record_the_ca() -> None:
    """Agent·Gateway 가 함께 쓰는 설정 합성은 CA 를 적지 않는다. 그 스냅샷이 쓰지 않는 PEM 을 들지
    않게 MCP Server 의 배포만 적는다(`MCPServerService._effective`)."""
    settings = PlatformSettings(network={"ca_bundle": ca_pem()})
    effective = resolve_effective(settings, cluster_doc(), TenantSettings(tenant="acme"))
    assert effective.ca_bundle is None


def test_the_ca_env_name_matches_the_runtime() -> None:
    platform_copy, runtime_copy = Path(mcpserver.__file__), MCP_RUNTIME / "main.py"
    platform = find_assignment(parse_python(platform_copy), "CA_ENV").value
    runtime = find_assignment(parse_python(runtime_copy), "CA_ENV").value
    assert ast.dump(platform) == ast.dump(runtime), (
        f"CA_ENV 가 {platform_copy} 와 {runtime_copy} 에서 다르다. 두 곳을 같이 고친다"
    )
