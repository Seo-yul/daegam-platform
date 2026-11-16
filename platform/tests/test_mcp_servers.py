"""MCP Server (D42) 의 정의 쪽을 본다.

만들기·검증·리비전·복제와 draft 삭제, profile.json 과 스펙 토큰의 scope, backends 복수화(D19)를
든다. 정의와 배포가 한 리소스다. 배포(렌더·Cluster 설정·바인딩·실패)는 test_mcp_servers_deploy.py,
배포한 뒤(상태·재배포·해제·삭제·경합·참조 가드)는 test_mcp_servers_lifecycle.py 가 본다.
"""

from __future__ import annotations

import pytest

from daegam.services.toolnames import InvalidToolName, fastmcp_normalize, validate_tool_name
from tests.helpers.auth import bearer
from tests.helpers.backend_apis import fixture_bytes, uploaded_backend
from tests.helpers.clusters import register_cluster
from tests.helpers.consts import BP, MS, T2
from tests.helpers.data_services import created_database
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import create_server


def _token(client, bp_id, scope=None, **kw):
    body = {"label": "inst", **kw}
    if scope is not None:
        body["scope"] = scope
    r = client.post(f"{BP}/{bp_id}/tokens", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# -- 정의: 만들기·조회·검증 ------------------------------------------------------------


def test_create_is_a_draft_with_no_deployment_side(client, mongo):
    bp = uploaded_backend(client)
    r = create_server(
        client,
        bp,
        tools=["Add"],
        tool_overrides={"Add": {"name": "add_numbers", "description": "Adds"}},
        runtime={"replicas": 2, "resources": {"limits": {"cpu": "1"}}},
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "draft" and p["deployed_revision"] is None
    assert p["revision"] == 1 and p["revisions"] == [1]
    assert p["cluster_id"] is None and p["k8s"] is None and p["status"] is None
    assert p["effective_config"] is None and p["hub_item"] is None
    assert p["exposure"]["enabled"] is False
    assert p["backends"] == [
        {
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
            "tool_overrides": {"Add": {"name": "add_numbers", "description": "Adds"}},
            "timeout_seconds": 30,
            "tool_rules": {},
        }
    ]
    assert "tools" not in p and "backend_profile_id" not in p
    assert p["runtime"] == {"image": None, "replicas": 2, "resources": {"limits": {"cpu": "1"}}}
    assert p["bindings"] == [] and p["updated_by"] == "header"
    assert client.get(f"{MS}/{p['id']}").json()["name"] == "calc-mcp"
    # 목록은 draft 를 포함한다 (스토어 기본은 제외라 여기서 단정한다)
    assert [x["id"] for x in client.get(MS).json()] == [p["id"]]
    assert client.get(MS, headers=T2).json() == []
    doc = mongo["mcp_servers"].find_one({"_id": p["id"]})
    assert doc["state"] == "draft" and doc["revision"] == 1 and len(doc["revisions"]) == 1
    assert doc["revisions"][0]["backends"][0]["tools"] == ["Add"]
    assert "deleted_at" not in doc
    # 배포 쪽 동작은 draft 에서 409 not_deployed
    for r in (
        client.get(f"{MS}/{p['id']}/manifest"),
        client.post(f"{MS}/{p['id']}/redeploy"),
        client.post(f"{MS}/{p['id']}/undeploy"),
        client.patch(f"{MS}/{p['id']}", json={"replicas": 2}),
        client.put(f"{MS}/{p['id']}/overrides", json={"patch": {}}),
        client.post(f"{MS}/{p['id']}/drift/reset"),
        client.post(f"{MS}/{p['id']}/publish", json={}),
    ):
        assert detail_code(r) == (409, "not_deployed"), r.text
    assert client.delete(f"{MS}/{p['id']}/publish").status_code == 404


def test_subset_validation(client):
    bp = uploaded_backend(client)  # tool_names == [Add, Divide]
    r = create_server(client, bp, tools=["Add", "Multiply"])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_tools"
    assert r.json()["detail"]["unknown"] == ["Multiply"]
    assert r.json()["detail"]["available"] == ["Add", "Divide"]
    r = create_server(client, bp, tool_overrides={"Nope": {"name": "x"}})
    assert detail_code(r) == (422, "unknown_tools")
    r = create_server(client, bp, tools=["Add", "Add"])
    assert detail_code(r) == (422, "duplicate_tools")
    r = create_server(client, bp, spec_version=9)
    assert detail_code(r) == (422, "unknown_spec_version")
    r = create_server(client, {"id": "nope"})
    assert detail_code(r) == (422, "unknown_backend_api")
    # 다른 테넌트의 Backend API 은 보이지 않는다
    r = client.post(
        MS,
        json={"name": "x", "backend_profile_id": bp["id"], "spec_version": 1},
        headers=T2,
    )
    assert detail_code(r) == (422, "unknown_backend_api")
    # 빈 tools 는 전부 노출 = 허용
    assert create_server(client, bp, name="all").status_code == 201


def test_validate_tool_name_matches_runtime_rules():
    assert validate_tool_name("Add") == "Add"
    assert validate_tool_name("a_b_1") == "a_b_1"
    for bad in ["a-b", "_a", "a_", "a__b", "a" * 57, "é", "has space", ""]:
        with pytest.raises(InvalidToolName):
            validate_tool_name(bad)
    assert fastmcp_normalize("Get User-Info.v2") == "Get_User_Info_v2"
    assert fastmcp_normalize("__x__") == "x"
    assert len(fastmcp_normalize("a" * 100)) == 56


def test_bindings_validation(client, fake_clusters):
    bp = uploaded_backend(client)
    # 클러스터 둘, Data Service 둘 (서로 다른 클러스터)
    c1 = register_cluster(client, "c1")
    c2 = register_cluster(client, "c2")
    ds1 = created_database(client, "r1", "redis", cluster_id=c1)
    ds2 = created_database(client, "r2", "redis", cluster_id=c2)
    ds3 = created_database(client, "r3", cluster_id=c1)

    ok = create_server(
        client,
        bp,
        bindings=[
            {"data_service_id": ds1["id"], "env_prefix": "CACHE"},
            {"data_service_id": ds3["id"], "env_prefix": "DB"},
        ],
    )
    assert ok.status_code == 201, ok.text
    r = create_server(
        client,
        bp,
        name="mix",
        bindings=[
            {"data_service_id": ds1["id"], "env_prefix": "A"},
            {"data_service_id": ds2["id"], "env_prefix": "B"},
        ],
    )
    assert detail_code(r) == (422, "cluster_mismatch")
    r = create_server(
        client,
        bp,
        name="dup",
        bindings=[
            {"data_service_id": ds1["id"], "env_prefix": "A"},
            {"data_service_id": ds3["id"], "env_prefix": "A"},
        ],
    )
    assert detail_code(r) == (422, "duplicate_env_prefix")
    r = create_server(
        client, bp, name="unk", bindings=[{"data_service_id": "nope", "env_prefix": "A"}]
    )
    assert detail_code(r) == (422, "unknown_data_service")
    r = create_server(
        client, bp, name="badp", bindings=[{"data_service_id": ds1["id"], "env_prefix": "bad"}]
    )
    assert r.status_code == 422
    # 다른 테넌트의 Data Service 는 보이지 않는다
    other = T2
    bp2 = uploaded_backend(client, headers=other)
    r = client.post(
        MS,
        json={
            "name": "x",
            "backend_profile_id": bp2["id"],
            "spec_version": 1,
            "bindings": [{"data_service_id": ds1["id"], "env_prefix": "A"}],
        },
        headers=other,
    )
    assert detail_code(r) == (422, "unknown_data_service")


def test_put_creates_revision_with_snapshot(client):
    bp = uploaded_backend(client)
    p = create_server(client, bp, tools=["Add"]).json()
    r = client.put(
        f"{MS}/{p['id']}",
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "tool_overrides": {"Divide": {"description": "div"}},
            "runtime": {"replicas": 3},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["revisions"] == [1, 2]
    assert r.json()["state"] == "draft" and r.json()["deployed_revision"] is None
    assert r.json()["backends"][0]["tools"] == ["Add", "Divide"]
    r1 = client.get(f"{MS}/{p['id']}/revisions/1").json()
    r2 = client.get(f"{MS}/{p['id']}/revisions/2").json()
    assert r1["backends"][0]["tools"] == ["Add"] and r1["revision"] == 1
    assert r1["runtime"]["replicas"] == 1
    assert r2["backends"][0]["tools"] == ["Add", "Divide"] and r2["runtime"]["replicas"] == 3
    assert [x["revision"] for x in client.get(f"{MS}/{p['id']}/revisions").json()] == [1, 2]
    assert client.get(f"{MS}/{p['id']}/revisions/3").status_code == 404
    # 검증 실패한 PUT 은 리비전을 만들지 않는다
    r = client.put(
        f"{MS}/{p['id']}",
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Nope"],
        },
    )
    assert r.status_code == 422
    assert client.get(f"{MS}/{p['id']}").json()["revision"] == 2
    # 이름은 바꿀 수 없다 (D24). 다른 이름은 422 name_immutable 이고 리비전도 늘지 않는다
    create_server(client, bp, name="taken")
    r = client.put(
        f"{MS}/{p['id']}", json={"name": "taken", "backend_profile_id": bp["id"], "spec_version": 1}
    )
    assert detail_code(r) == (422, "name_immutable")
    r = client.put(
        f"{MS}/{p['id']}",
        json={"name": "renamed", "backend_profile_id": bp["id"], "spec_version": 1},
    )
    assert detail_code(r) == (422, "name_immutable")
    assert client.get(f"{MS}/{p['id']}").json()["revision"] == 2
    # 새 스펙 버전으로 핀 변경 (같은 이름, display_name 은 바뀐다)
    modified = fixture_bytes("calculator.wsdl").replace(b"Adds two", b"Sums two")
    v2 = uploaded_backend(client, body=modified)
    assert v2["version"] == 2
    r = client.put(
        f"{MS}/{p['id']}",
        json={
            "name": "calc-mcp",
            "display_name": "계산기",
            "backend_profile_id": bp["id"],
            "spec_version": 2,
        },
    )
    assert r.status_code == 200 and r.json()["revision"] == 3
    assert (r.json()["name"], r.json()["display_name"]) == ("calc-mcp", "계산기")
    assert r.json()["backends"][0]["spec_version"] == 2


def test_delete_draft_and_backend_api_in_use(client):
    bp = uploaded_backend(client)
    p = create_server(client, bp).json()
    r = client.delete(f"{BP}/{bp['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "in_use"
    assert r.json()["detail"]["mcp_server_ids"] == [p["id"]]
    assert client.delete(f"{MS}/{p['id']}", headers=T2).status_code == 404
    r = client.delete(f"{MS}/{p['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": p["id"],
        "deleted": True,
        "state_before": "draft",
        "revoked_token_ids": [],
        "k8s": None,
    }
    assert client.get(f"{MS}/{p['id']}").status_code == 404
    assert client.delete(f"{MS}/{p['id']}").status_code == 404
    assert client.delete(f"{BP}/{bp['id']}").status_code == 200


# -- profile.json ---------------------------------------------------------------


def test_profile_json_with_scoped_token(client):
    bp = uploaded_backend(
        client, endpoint="http://legacy:8080/calc", auth="basic", credential_secret="calc-creds"
    )
    p = create_server(
        client,
        bp,
        tools=["Add"],
        tool_overrides={"Add": {"name": "add_numbers", "description": "Adds"}},
    ).json()
    url = f"{MS}/{p['id']}/revisions/1/profile.json"

    assert client.get(url).status_code == 401
    tok = _token(client, bp["id"], scope=[f"mcp-profile:{p['id']}"])
    assert tok["scope"] == [f"mcp-profile:{p['id']}"]
    auth = bearer(tok["token"])
    r = client.get(url, headers=auth)  # 테넌트 헤더 없음
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tools": ["Add"],
        "tool_overrides": {"Add": {"name": "add_numbers", "description": "Adds"}},
        "endpoint": "http://legacy:8080/calc",
        "auth_kind": "basic",
        "soap": True,
    }
    assert "calc-creds" not in r.text
    assert client.get(f"{MS}/{p['id']}/revisions/2/profile.json", headers=auth).status_code == 404

    # scope 없는 토큰, 다른 문서 scope, 다른 Backend API 토큰, 회수된 토큰 → 401
    for bad in (_token(client, bp["id"]), _token(client, bp["id"], scope=["mcp-profile:other"])):
        assert client.get(url, headers=bearer(bad["token"])).status_code == 401
    bp2 = uploaded_backend(client, name="orders", fixture="orders.wsdl")
    t2 = _token(client, bp2["id"], scope=[f"mcp-profile:{p['id']}"])
    assert client.get(url, headers=bearer(t2["token"])).status_code == 401
    client.delete(f"{BP}/{bp['id']}/tokens/{tok['token_id']}")
    assert client.get(url, headers=auth).status_code == 401
    assert client.get(f"{MS}/nope/revisions/1/profile.json", headers=auth).status_code == 401

    # 토큰 목록에 scope 가 보이고, 토큰 원문은 없다
    listed = client.get(f"{BP}/{bp['id']}/tokens").json()
    assert listed[0]["scope"] == [f"mcp-profile:{p['id']}"] and "token" not in listed[0]


def test_token_scope_input_forms(client):
    bp = uploaded_backend(client)
    r = client.post(
        f"{BP}/{bp['id']}/tokens",
        data={"label": "f", "scope": "mcp-profile:a, mcp-profile:b,mcp-profile:a"},
    )
    assert r.status_code == 201 and r.json()["scope"] == ["mcp-profile:a", "mcp-profile:b"]
    r = client.post(f"{BP}/{bp['id']}/tokens", json={"scope": "mcp-profile:x"})
    assert r.status_code == 201 and r.json()["scope"] == ["mcp-profile:x"]
    r = client.post(f"{BP}/{bp['id']}/tokens", json={"scope": ["bad scope"]})
    assert detail_code(r) == (422, "invalid_scope")
    r = client.post(f"{BP}/{bp['id']}/tokens", json={"scope": [1]})
    assert r.status_code == 422


def test_profile_json_rest_and_wsse(client):
    bp = uploaded_backend(
        client, name="pets", fixture="legacy-swagger.json", endpoint="https://api.pets/v1"
    )
    p = create_server(client, bp, name="pets-mcp", tools=["getPet"]).json()
    tok = _token(client, bp["id"], scope=[f"mcp-profile:{p['id']}"])
    auth = bearer(tok["token"])
    r = client.get(f"{MS}/{p['id']}/revisions/1/profile.json", headers=auth)
    assert r.status_code == 200
    assert r.json()["soap"] is False and r.json()["auth_kind"] == "none"
    assert r.json()["endpoint"] == "https://api.pets/v1"
    # REST 인데 wsse 로 바꾸면 렌더링이 422
    assert (
        client.patch(
            f"{BP}/{bp['id']}", json={"auth": "wsse", "credential_secret": "c"}
        ).status_code
        == 200
    )
    r = client.get(f"{MS}/{p['id']}/revisions/1/profile.json", headers=auth)
    assert detail_code(r) == (422, "invalid_auth")


# -- backends 복수화 (D19) -------------------------------------------------------


def test_backends_and_legacy_fields_are_exclusive(client):
    bp = uploaded_backend(client)
    r = client.post(
        MS,
        json={
            "name": "x",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "backends": [{"backend_profile_id": bp["id"], "spec_version": 1}],
        },
    )
    assert detail_code(r) == (422, "invalid_backends")
    r = client.post(MS, json={"name": "x", "backends": []})
    assert detail_code(r) == (422, "invalid_backends")
    r = client.post(MS, json={"name": "x"})
    assert detail_code(r) == (422, "invalid_backends")


def test_profile_json_multi_backend(client):
    bp1 = uploaded_backend(
        client, endpoint="http://legacy:8080/calc", auth="basic", credential_secret="calc-creds"
    )
    bp2 = uploaded_backend(
        client, name="pets", fixture="legacy-swagger.json", endpoint="https://api.pets"
    )
    p = client.post(
        MS,
        json={
            "name": "combo",
            "backends": [
                {
                    "backend_profile_id": bp1["id"],
                    "spec_version": 1,
                    "tools": ["Add"],
                    "tool_overrides": {"Add": {"name": "add_numbers"}},
                },
                {"backend_profile_id": bp2["id"], "spec_version": 1, "tools": ["getPet"]},
            ],
        },
    ).json()
    url = f"{MS}/{p['id']}/revisions/1/profile.json"
    # 어느 backend 의 토큰이든 scope 만 맞으면 통과한다
    r = None
    for bp in (bp1, bp2):
        tok = _token(client, bp["id"], scope=[f"mcp-profile:{p['id']}"])
        r = client.get(url, headers=bearer(tok["token"]))
        assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "backends": [
            {
                "spec": "openapi-0.yaml",
                "tools": ["Add"],
                "tool_overrides": {"Add": {"name": "add_numbers"}},
                "endpoint": "http://legacy:8080/calc",
                "auth_kind": "basic",
                "soap": True,
                "timeout_seconds": 30,
            },
            {
                "spec": "openapi-1.yaml",
                "tools": ["getPet"],
                "tool_overrides": {},
                "endpoint": "https://api.pets",
                "auth_kind": "none",
                "soap": False,
                "timeout_seconds": 30,
            },
        ]
    }
    # scope 없는 토큰은 여전히 401
    bad = _token(client, bp1["id"])
    assert client.get(url, headers=bearer(bad["token"])).status_code == 401
