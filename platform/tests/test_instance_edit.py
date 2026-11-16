"""F7a Instance 편집: manifest / overrides(Advanced) / PATCH(Basic) / preview / drift."""

from __future__ import annotations

import yaml

from tests.helpers.consts import MS
from tests.helpers.data_services import created_database
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import deployed, detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

CR_KEY = ("toolhive.stacklok.dev/v1beta1", "MCPServer", "mcp-calc-1", "daegam-default")


def _live(fake: FakeClusterClient):
    return fake.get(CR_KEY[0], CR_KEY[1], CR_KEY[2], CR_KEY[3])


def _mcp(cr):
    return [c for c in cr["spec"]["podTemplateSpec"]["spec"]["containers"] if c["name"] == "mcp"][0]


RES = {"limits": {"cpu": "500m", "memory": "256Mi"}}
RES_PATCH = {
    "spec": {"podTemplateSpec": {"spec": {"containers": [{"name": "mcp", "resources": RES}]}}}
}


def test_manifest_documents_shape(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    m = client.get(f"{MS}/{inst['id']}/manifest").json()
    # MCPServer 와 테넌트 울타리 설정(읽기만)이다 (P50 11절)
    assert list(m) == ["documents"] and len(m["documents"]) == 2
    d = m["documents"][0]
    assert (
        d["kind"] == "MCPServer"
        and d["name"] == "mcp-calc-1"
        and d["namespace"] == "daegam-default"
    )
    assert d["rendered"]["spec"]["image"] == "daegam-mcp-runtime:0.1.0"
    assert d["overrides"] == {} and d["final"] == d["rendered"]
    assert d["live"]["metadata"]["name"] == "mcp-calc-1"
    assert d["drift"] == [] and d["drift_managers"] == [] and d["error"] is None
    assert "spec.image" in d["locked"] and "metadata.labels[daegam.devops.ai.kr/*]" in d["locked"]
    assert "spec.podTemplateSpec.spec.containers[name=mcp].args" in d["ignored_by_operator"]
    # 클러스터 접속 불가면 live/drift 가 없고 error 가 있다
    fake_clusters.by_name["edge"].unreachable = True
    d = client.get(f"{MS}/{inst['id']}/manifest").json()["documents"][0]
    assert d["live"] is None and d["drift"] is None and d["error"].startswith("cluster_unreachable")


def test_overrides_from_full_yaml_diffs_against_rendered(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    d = client.get(f"{MS}/{inst['id']}/manifest").json()["documents"][0]
    edited = d["final"]
    _mcp(edited)["resources"] = RES
    edited["spec"]["maxRequestBodySize"] = 1048576
    edited["spec"]["sessionAffinity"] = "None"
    r = client.put(f"{MS}/{inst['id']}/overrides", json={"yaml": yaml.safe_dump(edited)})
    assert r.status_code == 200, r.text
    assert r.json()["overrides"] == {
        "spec": {
            "maxRequestBodySize": 1048576,
            "sessionAffinity": "None",
            "podTemplateSpec": {"spec": {"containers": [{"name": "mcp", "resources": RES}]}},
        }
    }
    r = client.put(f"{MS}/{inst['id']}/overrides", json={"manifest": edited})
    assert (
        r.status_code == 200
        and r.json()["overrides"] == client.get(f"{MS}/{inst['id']}").json()["overrides"]
    )
    # 잘못된 입력
    assert client.put(f"{MS}/{inst['id']}/overrides", json={}).status_code == 422
    assert (
        client.put(f"{MS}/{inst['id']}/overrides", json={"patch": {}, "yaml": "a: 1"}).status_code
        == 422
    )
    r = client.put(f"{MS}/{inst['id']}/overrides", json={"yaml": "- a\n- b"})
    assert detail_code(r) == (422, "invalid_yaml")
    r = client.put(f"{MS}/{inst['id']}/overrides", json={"yaml": "a: [unclosed"})
    assert detail_code(r) == (422, "invalid_yaml")


def test_overrides_survive_redeploy_and_block_when_invalid(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    fake = fake_clusters.by_name["edge"]
    assert client.put(f"{MS}/{inst['id']}/overrides", json={"patch": RES_PATCH}).status_code == 200
    # 재렌더링(이미지 변경 + 새 리비전) 뒤에도 overrides 가 얹힌다
    client.patch(f"/clusters/{cid}", json={"images": {"runtime_image": "mcp-runtime:2"}})
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert r.json()["overrides"] == RES_PATCH and r.json()["deployed_revision"] == 2
    live = _live(fake)
    assert live["spec"]["image"] == "mcp-runtime:2" and _mcp(live)["resources"] == RES
    assert (
        "revisions/2/profile.json"
        in live["spec"]["podTemplateSpec"]["spec"]["initContainers"][0]["args"][0]
    )
    # 새 렌더링에 overrides 가 안 맞으면 409 overrides_invalid 로 재배포를 막는다
    fake.reject_dry_run = lambda obj: (
        "resources.limits.cpu: bad" if _mcp(obj).get("resources") else None
    )
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "overrides_invalid"
    assert r.json()["detail"]["paths"][0].startswith(
        "spec.podTemplateSpec.spec.containers[name=mcp].resources"
    )
    assert client.get(f"{MS}/{inst['id']}").json()["deployed_revision"] == 2


def test_basic_patch_creates_revision_and_redeploys(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    fake = fake_clusters.by_name["edge"]
    pg = created_database(client, "db")
    r = client.patch(
        f"{MS}/{inst['id']}",
        json={
            "replicas": 2,
            "resources": {"limits": {"cpu": "1"}},
            "image": "custom:1",
            "bindings": [
                {"data_service_id": ds["id"], "env_prefix": "CACHE"},
                {"data_service_id": pg["id"], "env_prefix": "DB"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["deployed_revision"] == 2 and got["state"] == "deployed"
    assert got["effective_config"]["images"]["runtime_image"] == "custom:1"
    assert got["effective_config"]["replicas"] == 2 and got["effective_config"]["resources"] == {
        "limits": {"cpu": "1"}
    }
    prof = client.get(f"{MS}/{inst['id']}").json()
    assert prof["revision"] == 2 and prof["runtime"] == {
        "image": "custom:1",
        "replicas": 2,
        "resources": {"limits": {"cpu": "1"}},
    }
    assert [b["env_prefix"] for b in prof["bindings"]] == ["CACHE", "DB"]
    assert prof["backends"][0]["tools"] == ["Add"]  # 툴은 그대로
    live = _live(fake)
    assert live["spec"]["image"] == "custom:1" and live["spec"]["replicas"] == 2
    assert live["spec"]["sessionStorage"]["address"] == "redis.svc:6379"
    env = {e["name"] for e in live["spec"]["env"]}
    assert "DB_URL" in env and "CACHE_URL" in env
    # 부분: resources 만 null 로 지우기, 나머지 유지
    r = client.patch(f"{MS}/{inst['id']}", json={"resources": None})
    assert r.status_code == 200 and r.json()["deployed_revision"] == 3
    assert client.get(f"{MS}/{inst['id']}").json()["runtime"] == {
        "image": "custom:1",
        "replicas": 2,
        "resources": None,
    }
    # 검증
    assert client.patch(f"{MS}/{inst['id']}", json={}).status_code == 422
    r = client.patch(
        f"{MS}/{inst['id']}", json={"bindings": [{"data_service_id": "nope", "env_prefix": "X"}]}
    )
    assert detail_code(r) == (422, "unknown_data_service")
    r = client.patch(
        f"{MS}/{inst['id']}",
        json={
            "bindings": [
                {"data_service_id": ds["id"], "env_prefix": "A"},
                {"data_service_id": pg["id"], "env_prefix": "A"},
            ]
        },
    )
    assert detail_code(r) == (422, "duplicate_env_prefix")
    assert client.get(f"{MS}/{inst['id']}").json()["revision"] == 3  # 실패는 리비전을 안 만든다


def test_preview_renders_without_deploying(client, fake_clusters):
    cid, bp, ds, mp = mcp_server_env(client)
    r = client.post(f"{MS}/{mp['id']}/revisions/1/preview")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["cluster_id"] == cid and p["namespace"] == "daegam-default"
    cr = p["manifest"]
    assert cr["kind"] == "MCPServer" and cr["metadata"]["name"] == "mcp-calc-mcp"
    assert cr["spec"]["image"] == "daegam-mcp-runtime:0.1.0"
    assert (
        "revisions/1/profile.json"
        in cr["spec"]["podTemplateSpec"]["spec"]["initContainers"][0]["args"][0]
    )
    assert "spec.image" in p["locked"]
    assert deployed(client, MS) == []  # 배포되지 않았다
    assert (
        fake_clusters.by_name.get("edge") is None
        or fake_clusters.by_name["edge"].find("MCPServer") == []
    )
    assert client.post(f"{MS}/{mp['id']}/revisions/9/preview").status_code == 404
    r = client.post(f"{MS}/{mp['id']}/revisions/1/preview", params={"cluster_id": "nope"})
    assert detail_code(r) == (422, "unknown_cluster")
