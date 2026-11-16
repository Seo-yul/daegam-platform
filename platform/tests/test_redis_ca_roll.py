"""Redis CA 가 바뀌면 그 CA 를 믿는 파드가 다시 뜬다 (P61 F3).

redis-py 와 ToolHive 프록시는 연결을 만들 때 읽은 CA 를 쥐고 있다. 그래서 렌더는 파드가 믿는 CA 의
지문을 `daegam.devops.ai.kr/redis-ca` 로 달고, 재적용 판의 Redis 표시가 그 지문이다. 여기서는
Database 와 클러스터 기본 Redis 의 CA 가 바뀌었을 때 재적용 틱이 소비자를 다시 렌더하는지, 서비스
토큰 회전이 그것을 가리지 않는지를 본다.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from daegam.k8s.templates.common import DEFAULT_REDIS_SOURCE, REDIS_CA_ANNOTATION
from daegam.pem import ca_sha256
from daegam.services import redis_tls_consumers
from daegam.services.agent_features import FEATURES_HEADER, IMAGE_HEADER, REDIS_TLS
from daegam.services.data_plane_auth import (
    KIND_MCP_SERVER,
    _Target,
    own_audience_rev,
    redis_tls_rev,
)
from daegam.services.redis_tls_consumers import RecentReads
from daegam.store.auth_reapply import AudienceSwitch
from tests.helpers.agent_runtime import (
    deployed_agent,
    skip_start_check,
)
from tests.helpers.agents import MINIMAL_SPEC
from tests.helpers.backend_apis import CALC_ENDPOINT, uploaded_backend
from tests.helpers.clusters import record_toolhive
from tests.helpers.consts import AG, DEFAULT_NS, DS, GV, IMAGE, MS
from tests.helpers.data_services import TLS_PORT, post_database, tls_database
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env, replicated_server
from tests.helpers.tls import ca_mark, ca_pem
from tests.helpers.workers import run_data_plane_auth

ROTATED = "daegam-rotated-ca"


@pytest.fixture(autouse=True)
def _no_read_cache(monkeypatch):
    """틱마다 Database 와 Secret 을 새로 읽는다. 캐시는 따로 본다."""
    monkeypatch.setattr(redis_tls_consumers, "RECENT_READ_SECONDS", 0.0)


def _tls_consumers(client, fake_clusters, mongo):
    """TLS Database 하나와 그것을 memory 로 쓰는 Agent, 바인딩하는 MCP Server(calc-1)."""
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=lambda _gw, _ds: MINIMAL_SPEC, runtime=False
    )
    skip_start_check(client.app)
    ds = tls_database(client, "mem", db=2, username="app")
    binding = {"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"}
    mongo["agents"].update_one(
        {"_id": agent.doc["id"]}, {"$set": {"effective_config.bindings": [binding]}}
    )
    headers = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: REDIS_TLS}
    assert client.get(f"{AG}/{agent.doc['id']}/agent.json", headers=headers).status_code == 200
    bp = uploaded_backend(client, endpoint=CALC_ENDPOINT)
    r = client.post(
        MS,
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    assert r.status_code == 201, r.text
    server = clone_and_deploy(client, r.json(), name="calc-1").json()
    run_data_plane_auth(client)
    return agent, ds, server


def _server_cr(fake_clusters) -> dict:
    return fake_clusters.by_name["edge"].get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)


def _server_mark(fake_clusters) -> str | None:
    meta = _server_cr(fake_clusters)["spec"]["podTemplateSpec"]["metadata"]
    return (meta.get("annotations") or {}).get(REDIS_CA_ANNOTATION)


def _rotate_ca(client, ds) -> str:
    rotated = ca_pem(ROTATED)
    r = client.patch(f"{DS}/{ds['id']}", json={"tls": {"port": TLS_PORT, "ca": rotated}})
    assert r.status_code == 200, r.text
    return rotated


def _mcp_service(client):
    return client.app.state.data_plane_auth.mcp_servers


def _rotate_svc_token(client, server_id: str) -> None:
    svc = _mcp_service(client)
    doc = client.portal.call(lambda: svc.instances.get(server_id))
    assert client.portal.call(lambda: svc.rotate_svc_token(doc))


def _default_redis_server(client, fake_clusters, mongo) -> tuple[str, str, dict]:
    """세션 저장소가 클러스터 기본 Redis 의 TLS 로 붙는 복제 서버(calc-1). 가짜 클러스터는 적용한
    Secret 을 쿠버네티스처럼 data 로 둔다. (클러스터 id, 서버 id, 기본 Redis 본문)이다."""
    cid, bp, _ds, ms = mcp_server_env(client, ds=False)
    fake_clusters.by_name["edge"].secret_string_data_to_data = True
    default = {"address": "redis.shared:6379", "tls": {"port": TLS_PORT, "ca": ca_pem()}}
    assert client.patch(f"/clusters/{cid}", json={"default_redis": default}).status_code == 200
    record_toolhive(mongo, cid, "0.52.0")
    replicated_server(client, bp, ms)
    sid = clone_and_deploy(client, ms, name="calc-1").json()["id"]
    run_data_plane_auth(client)
    assert _server_mark(fake_clusters) == ca_mark(DEFAULT_REDIS_SOURCE, ca_pem())
    assert run_data_plane_auth(client)["reapplied"] == []
    return cid, sid, default


def test_a_failed_namespace_secret_read_keeps_the_rendered_fingerprint(
    client, fake_clusters, mongo, monkeypatch
):
    """`daegam-redis-ca` 를 읽지 못하는 것(없는 것과 다르다)은 CA 가 바뀐 것이 아니다. 재적용은 다시
    렌더하지 않고, 다른 까닭의 렌더도 마지막 지문을 그대로 단다."""
    _cid, sid, _default = _default_redis_server(client, fake_clusters, mongo)
    fake = fake_clusters.by_name["edge"]
    real_get = fake.get

    def unreadable(api_version, kind, name, namespace=None):
        if kind == "Secret" and name == "daegam-redis-ca":
            raise RuntimeError("the API server timed out")
        return real_get(api_version, kind, name, namespace)

    monkeypatch.setattr(fake, "get", unreadable)
    assert sid not in run_data_plane_auth(client)["reapplied"]
    svc = _mcp_service(client)
    doc = client.portal.call(lambda: svc.instances.get(sid))
    assert client.portal.call(lambda: svc.reapply_auth(doc))
    assert _server_mark(fake_clusters) == ca_mark(DEFAULT_REDIS_SOURCE, ca_pem())
    monkeypatch.setattr(fake, "get", real_get)
    assert sid not in run_data_plane_auth(client)["reapplied"]


def test_the_start_corrects_and_fills_database_ca_fingerprints(client, fake_clusters, mongo):
    """플랫폼이 뜰 때 Database 의 지문을 CA Secret 의 `ca.crt` 와 맞춘다. 지문이 없거나(이 칸 전에
    저장한 CA) 다르면 고치고, 맞으면 그대로 둔다."""
    from tests.helpers.clusters import default_cluster

    default_cluster(client)
    ds = tls_database(client, "mem", db=2, username="app")
    service = client.app.state.data_service_service

    def sync() -> int:
        return client.portal.call(service.sync_ca_fingerprints)

    def stored() -> str | None:
        return mongo["data_services"].find_one({"_id": ds["id"]})["tls"].get("ca_sha256")

    assert sync() == 0
    mongo["data_services"].update_one({"_id": ds["id"]}, {"$unset": {"tls.ca_sha256": ""}})
    assert sync() == 1 and stored() == ca_sha256(ca_pem())
    mongo["data_services"].update_one({"_id": ds["id"]}, {"$set": {"tls.ca_sha256": "stale"}})
    assert sync() == 1 and stored() == ca_sha256(ca_pem())
    assert sync() == 0


def test_the_fingerprint_check_reads_only_ca_secrets_and_gives_up_a_failing_cluster(
    client, fake_clusters, mongo, monkeypatch
):
    """지문 맞추기는 CA Secret 만 읽는다. 한 클러스터에서 읽기가 실패하면 그 클러스터의 나머지
    Database 는 읽지 않는다. 닿지 않는 클러스터는 읽기마다 시한까지 기다리기 때문이다."""
    from tests.helpers.clusters import default_cluster

    default_cluster(client)
    tls = {"port": TLS_PORT, "ca": ca_pem()}
    first = post_database(client, "one", "redis", db=2, tls=tls).json()
    second = post_database(client, "two", "redis", db=2, tls=tls).json()
    mongo["data_services"].update_many({}, {"$unset": {"tls.ca_sha256": ""}})
    fake = fake_clusters.by_name["edge"]
    real_get = fake.get
    read: list[str] = []

    def reading(api_version, kind, name, namespace=None):
        if kind == "Secret":
            read.append(name)
        return real_get(api_version, kind, name, namespace)

    monkeypatch.setattr(fake, "get", reading)
    service = client.app.state.data_service_service
    assert client.portal.call(service.sync_ca_fingerprints) == 2
    assert sorted(read) == sorted([f"ds-{first['id']}-ca", f"ds-{second['id']}-ca"])

    def failing(api_version, kind, name, namespace=None):
        read.append(name)
        raise RuntimeError("the cluster cannot be reached")

    mongo["data_services"].update_many({}, {"$unset": {"tls.ca_sha256": ""}})
    read.clear()
    monkeypatch.setattr(fake, "get", failing)
    assert client.portal.call(service.sync_ca_fingerprints) == 0
    assert len(read) == 1


def test_a_slow_read_is_cached_from_when_it_ends(monkeypatch):
    """오래 걸린 읽기도 끝난 때부터 `RECENT_READ_SECONDS` 동안 다시 쓴다. 네임스페이스(열쇠)마다
    한 번 읽는다."""
    monkeypatch.setattr(redis_tls_consumers, "RECENT_READ_SECONDS", 5.0)
    clock = [100.0]
    monkeypatch.setattr(redis_tls_consumers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    reads: list[str] = []

    def slow(key: str):
        async def load() -> str:
            clock[0] += 6.0
            reads.append(key)
            return key

        return load

    async def go() -> None:
        recent = RecentReads()
        for key in ("ns-a", "ns-a", "ns-b", "ns-a"):
            await recent.get(key, slow(key))

    asyncio.run(go())
    assert reads == ["ns-a", "ns-b", "ns-a"]


def test_recent_reads_reuse_a_value_within_the_window(monkeypatch):
    """같은 열쇠는 `RECENT_READ_SECONDS` 안에 다시 읽지 않고, 지나면 다시 읽는다."""
    monkeypatch.setattr(redis_tls_consumers, "RECENT_READ_SECONDS", 60.0)
    reads: list[str] = []

    async def load() -> str:
        reads.append("x")
        return f"v{len(reads)}"

    async def go() -> list[str]:
        recent = RecentReads()
        first = [await recent.get("k", load), await recent.get("k", load)]
        monkeypatch.setattr(redis_tls_consumers, "RECENT_READ_SECONDS", 0.0)
        return [*first, await recent.get("k", load)]

    assert asyncio.run(go()) == ["v1", "v1", "v2"]


def _mcp_target(doc_rev, want, paced, switch=None) -> _Target:
    doc = SimpleNamespace(auth_rev=doc_rev, render_epoch=1, audience_switch=switch)
    service = SimpleNamespace(auth_kind=KIND_MCP_SERVER)
    return _Target(service, doc, want, 1, paced)  # type: ignore[arg-type]


def test_mcp_mark_differences_go_at_the_namespace_pace():
    """MCP Server 의 판은 Redis 표시 위에 audience 표시를 얹는다(`MCPServerService.want_rev`). CA
    가 바뀐 것은 천천히 가고, 인증 입력이 바뀐 것은 급하다."""
    now = datetime.datetime(2026, 10, 21)
    own = AudienceSwitch(switched_at=now, reason="new_server")

    def rev(base, ca, switch=None):
        return own_audience_rev(redis_tls_rev(base, ca), switch)

    def paced(base, recorded, switch=None):
        return frozenset(rev(base, mark, switch) for mark in (None, recorded))

    assert not _mcp_target(rev("b", "ca-1"), rev("b", "ca-2"), paced("b", "ca-1")).urgent
    assert not _mcp_target(rev("b", None), rev("b", "ca-1"), paced("b", None)).urgent
    # 자기 audience 로 검증하는 서버도 CA 표시만 다르면 천천히 간다
    switched = (rev("b", "ca-1", own), rev("b", "ca-2", own), paced("b", "ca-1", own), own)
    assert not _mcp_target(*switched).urgent
    assert _mcp_target(rev("old", "ca-1"), rev("new", "ca-1"), paced("new", "ca-1")).urgent
