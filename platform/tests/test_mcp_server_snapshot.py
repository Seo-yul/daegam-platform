"""MCP Server 의 배포 스냅샷과 제자리 재렌더 (P50 1절).

배포는 MCPServer CR 이 Backend API·Database 에서 받은 값을 `deployed_inputs` 에 적고, 렌더 번호가
오른 재렌더는 현재 공유 문서 대신 그 스냅샷으로 CR 을 다시 적용한다. 노출한 서버는 앞단을 먼저 다시
적용하므로 MCPServer 단계가 막혀도 인증 변경이 앞단에 반영된다. 파드가 다시 뜰 때 받는
profile.json 은 지금 Backend API 로 만들어지지만, 런타임은 스냅샷으로 렌더한 endpoint·auth env 를
먼저 읽으므로 backend 수와 상관없이 배포 때의 값으로 부른다.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from daegam.k8s.templates.mcpserver import (
    SINGLE_AUTH_ENV,
    SINGLE_ENDPOINT_ENV,
    SINGLE_USERNAME_ENV,
    backend_env,
)
from daegam.services.data_plane_auth import KIND_MCP_SERVER
from tests.helpers.auth import bearer
from tests.helpers.backend_apis import CALC_ENDPOINT
from tests.helpers.consts import BP, DEFAULT_NS, GV, MS
from tests.helpers.exposure import cluster_with_ingress, prep_ingress
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import cedar_policies
from tests.helpers.http import api_client
from tests.helpers.mcp_servers import (
    clone_and_deploy,
    mcp_server_env,
)
from tests.helpers.workers import raise_epoch, run_data_plane_auth

MOVED_ENDPOINT = "https://calc.moved.example/api"


def _fake(fake_clusters) -> FakeClusterClient:
    return fake_clusters.by_name["edge"]


def _cr(fake: FakeClusterClient, name: str) -> dict | None:
    return fake.get(GV, "MCPServer", name, DEFAULT_NS)


def _env(cr: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in cr["spec"].get("env", [])}


def _stored(mongo, server_id: str) -> dict:
    return mongo["mcp_servers"].find_one({"_id": server_id})


def _exposed(client, fake_clusters) -> tuple[dict, FakeClusterClient]:
    """노출한 MCP Server 하나. 앞단 MCPRemoteProxy 가 인증 입력을 싣는다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client)
    fake = _fake(fake_clusters)
    prep_ingress(fake)
    r = clone_and_deploy(
        client, mp, name="calc-1", exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert r.status_code == 201, r.text
    return r.json(), fake


def _kinds_since(fake: FakeClusterClient, start: int) -> list[str]:
    return [o["kind"] for o in fake.applied[start:]]


def _restarted_pod(
    client, fake: FakeClusterClient, inst: dict, index: int
) -> tuple[str, str | None]:
    """다시 뜬 파드의 런타임이 index 번째 backend 를 부르는 (endpoint, auth). initContainer 가
    profile.json 을 지금 받고, 런타임은 env 를 그것보다 먼저 읽는다(mcp-runtime `build_server`·
    `_backend_options`). auth 를 걸지 않으면 None 이다."""
    env = _env(_cr(fake, inst["k8s"]["name"]))
    tokens = fake.get("v1", "Secret", f"inst-{inst['id']}-token", DEFAULT_NS)["stringData"]
    r = client.get(
        f"{MS}/{inst['id']}/revisions/{inst['deployed_revision']}/profile.json",
        headers=bearer(tokens.get("token") or tokens["token-0"]),
    )
    assert r.status_code == 200, r.text
    profile = r.json()
    if "backends" in profile:
        entry = profile["backends"][index]
        endpoint_env, auth_env = backend_env(index, "endpoint"), backend_env(index, "auth")
    else:
        entry, endpoint_env, auth_env = profile, SINGLE_ENDPOINT_ENV, SINGLE_AUTH_ENV
    profile_auth = None if entry["auth_kind"] == "none" else entry["auth_kind"]
    return env.get(endpoint_env) or entry["endpoint"], env.get(auth_env) or profile_auth


def test_pod_restart_keeps_the_deployed_auth_after_it_is_removed(client, fake_clusters):
    """배포 뒤 Backend API 의 auth 를 basic 에서 none 으로 바꾸고 자격증명 Secret 을 지웠다.
    profile.json 은 none 을 싣지만 파드가 다시 떠도 배포 때의 basic 과 그 Secret 으로 부른다."""
    _cid, bp, _ds, ms = mcp_server_env(client, ds=False, backend_auth="basic")
    inst = clone_and_deploy(client, ms).json()
    removed = {"auth": "none", "credential_secret": None}
    assert client.patch(f"{BP}/{bp['id']}", json=removed).status_code == 200
    fake = _fake(fake_clusters)
    assert _restarted_pod(client, fake, inst, 0) == (CALC_ENDPOINT, "basic")
    secrets = _cr(fake, inst["k8s"]["name"])["spec"]["secrets"]
    assert {s["targetEnvName"]: s["name"] for s in secrets}[SINGLE_USERNAME_ENV] == "calc-creds"


def test_a_snapshot_of_an_earlier_deployment_blocks_the_rerender(
    client, fake_clusters, mongo, monkeypatch
):
    """스냅샷의 `deploying_since` 가 문서의 것과 다르면 그 스냅샷은 앞 배포의 것이다. 렌더 번호가
    오른 재렌더는 그 스냅샷으로 CR 을 되돌리지 않고 integrity_error 로 막혀 재배포를 기다린다."""
    _cid, _bp, _ds, ms = mcp_server_env(client, backend_auth="basic")
    inst = clone_and_deploy(client, ms).json()
    fake = _fake(fake_clusters)
    before = copy.deepcopy(_cr(fake, inst["k8s"]["name"]))
    since = _stored(mongo, inst["id"])["deploying_since"] + timedelta(minutes=1)
    mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": {"deploying_since": since}})

    raise_epoch(monkeypatch, KIND_MCP_SERVER)
    start = len(fake.applied)
    assert run_data_plane_auth(client)["blocked"] == [inst["id"]]
    assert _stored(mongo, inst["id"])["auth_blocked"]["code"] == "integrity_error"
    assert "MCPServer" not in _kinds_since(fake, start)
    assert _cr(fake, inst["k8s"]["name"]) == before


def _front_after_a_local_reapply(client, local_app, fake, inst, client_id: str) -> dict:
    """agent_client_ids 를 client_id 로 바꾸고 local 모드의 앱으로 재적용한다. 사람 토큰을 받는
    모드라 앞단이 Cedar 에 그 id 를 싣는다. 이 틱에 다시 적용한 앞단 하나를 돌려준다. 문서는
    MCPServer 단계에서 막히고 MCPServer 는 적용하지 않는다."""
    patch = {"agent_client_ids": [client_id]}
    assert client.patch("/tenants/default/settings", json=patch).status_code == 200
    start = len(fake.applied)
    with api_client(local_app) as local:
        assert run_data_plane_auth(local)["blocked"] == [inst["id"]]
    fronts = [o for o in fake.applied[start:] if o["kind"] == "VirtualMCPServer"]
    assert len(fronts) == 1 and "MCPServer" not in _kinds_since(fake, start)
    return fronts[0]


def test_an_auth_change_reaches_the_front_when_stored_overrides_are_refused(
    client, fake_clusters, mongo, local_app
):
    """저장된 overrides 가 지금 잠긴 경로(spec.image)를 건드려 MCPServer 단계가 막혀도 앞단은
    바뀐 agent_client_ids 로 다시 적용되고, 문서는 locked_field 로 막힌다."""
    inst, fake = _exposed(client, fake_clusters)
    mongo["mcp_servers"].update_one(
        {"_id": inst["id"]}, {"$set": {"overrides": {"spec": {"image": "evil:1"}}}}
    )
    front = _front_after_a_local_reapply(client, local_app, fake, inst, "bot-2")
    assert any("bot-2" in p for p in cedar_policies(front, fake=fake))
    assert _stored(mongo, inst["id"])["auth_blocked"]["code"] == "locked_field"
