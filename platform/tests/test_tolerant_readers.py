"""저장된 문서가 모르는 칸을 견디는가 (P61 1절).

다음 판은 클러스터 기본 Redis, 배포 시점 설정 스냅샷(`effective_config`), MCP Server 배포 스냅샷의
바인딩에 칸을 더할 수 있다(P61 B2 는 `tls` 를 더했다). 롤아웃 중의 옛 레플리카와 되돌린 판이 그
문서를 읽고 다시 저장하므로, 이 판은 모르는 칸이 있어도 읽고 다시 렌더하며 저장할 때 그 칸을 지우지
않는다. API 본문은 오타를 잡으려고 여전히 모르는 칸을 거절한다. 여기서는 다음 판의 칸을 `acl` 로
흉내 낸다.
"""

from __future__ import annotations

from daegam.config.resolve import EffectiveConfig
from daegam.services.data_plane_auth import KIND_MCP_SERVER
from daegam.store.clusters import DefaultRedis
from daegam.store.mcp_servers import BindingInputs, MCPServerInputs
from tests.helpers.clusters import post_cluster
from tests.helpers.consts import DEFAULT_NS, GV, MS
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env, replicated_server
from tests.helpers.workers import raise_epoch, run_data_plane_auth

# 다음 판이 적을 칸의 흉내다
LATER = {"user": "tenant-a"}
TLS = {"port": 6380, "ca_secret": "ds-x"}


def test_a_cluster_keeps_its_closed_mark_and_later_redis_keys(client, mongo):
    """닫힌 기록(P61 5절 5단계)은 읽고 다른 칸을 고쳐 저장해도 참을 지킨다. 다음 판이 적은
    `default_redis` 의 칸이 있어도 클러스터를 읽고, 다른 칸을 고쳐 저장해도 그 칸이 남는다
    (`update` 는 `default_redis` 를 통째로 쓴다)."""
    open_id = post_cluster(client, default_redis={"address": "redis:6379"}).json()["id"]
    mongo["clusters"].update_one({"_id": open_id}, {"$set": {"default_redis.acl": LATER}})
    r = client.get(f"/clusters/{open_id}")
    assert r.status_code == 200, r.text
    assert r.json()["default_redis"]["address"] == "redis:6379"
    r = client.patch(f"/clusters/{open_id}", json={"display_name": "Edge 1"})
    assert r.status_code == 200, r.text
    stored = mongo["clusters"].find_one({"_id": open_id})["default_redis"]
    assert stored == {
        "address": "redis:6379",
        "password_secret": None,
        "tls": None,
        "plain_closed": False,
        "acl": LATER,
    }
    tls = {"port": 6380, "ca": "PEM"}
    cid = post_cluster(client, "edge2", default_redis={"address": "redis:6379"}).json()["id"]
    closed = {"address": "redis:6379", "tls": tls, "plain_closed": True}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"default_redis": closed}})

    r = client.get(f"/clusters/{cid}")
    assert r.status_code == 200 and r.json()["default_redis"]["plain_closed"] is True
    r = client.patch(f"/clusters/{cid}", json={"display_name": "Edge 2"})
    assert r.status_code == 200, r.text
    stored = mongo["clusters"].find_one({"_id": cid})["default_redis"]
    assert stored["plain_closed"] is True


def test_the_stored_models_keep_what_they_do_not_know():
    redis = DefaultRedis.model_validate(
        {"address": "r:1", "acl": LATER, "tls": {"port": 6380, "ca": "PEM", "acl": LATER}}
    )
    assert redis.model_dump()["acl"] == LATER and redis.model_dump()["tls"]["acl"] == LATER
    binding = BindingInputs.model_validate(
        {
            "data_service_id": "ds1",
            "name": "cache",
            "cluster_id": "c1",
            "kind": "redis",
            "host": "redis",
            "port": 6379,
            "secret_name": "ds-ds1",
            "tls": {**TLS, "url": "rediss://redis:6380"},
        }
    )
    assert binding.model_dump()["tls"]["url"] == "rediss://redis:6380"
    # 배포 스냅샷 전체도 바인딩과 최상위의 모르는 칸을 남긴다(통째로 저장되므로)
    inputs = MCPServerInputs.model_validate(
        {"backends": [], "bindings": [binding.model_dump()], "later_field": 1}
    )
    dumped = inputs.model_dump()
    assert dumped["later_field"] == 1 and dumped["bindings"][0]["tls"]["port"] == 6380


def test_a_deployed_server_with_later_snapshot_fields_rerenders_and_keeps_them(
    client, fake_clusters, mongo, monkeypatch
):
    """배포 시점 설정의 기본 Redis·최상위와 배포 스냅샷의 바인딩에 다음 판의 칸이 있어도 다시
    렌더하기가 지금과 같은 CR 을 내고, 저장된 문서에서 그 칸을 지우지 않는다."""
    _cid, bp, ds, ms = mcp_server_env(client)
    replicated_server(
        client, bp, ms, bindings=[{"data_service_id": ds["id"], "env_prefix": "CACHE"}]
    )
    inst = clone_and_deploy(client, ms).json()
    edge = fake_clusters.by_name["edge"]
    before = edge.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)["spec"]
    later = {
        "effective_config.default_redis": {"address": "redis:6379", "acl": LATER},
        "effective_config.later_field": {"x": 1},
        "deployed_inputs.bindings.0.acl": LATER,
    }
    mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": later})
    stored = mongo["mcp_servers"].find_one({"_id": inst["id"]})
    EffectiveConfig.model_validate(stored["effective_config"])

    raise_epoch(monkeypatch, KIND_MCP_SERVER)
    result = run_data_plane_auth(client)
    assert inst["id"] in result["reapplied"] and result["failed"] == []
    assert edge.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)["spec"] == before
    assert client.get(f"{MS}/{inst['id']}").status_code == 200
