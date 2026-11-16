"""MCP Server (D42) 의 배포를 본다.

MCPServer CR·토큰 Secret 렌더, Cluster 설정이 매니페스트로 가는 것, replicas 와 세션 저장소, 바인딩,
이름·핀 검증, 실패한 배포, backend 가 여럿인 배포(D19)를 든다.

준비(`mcp_server_env`)와 복제 뒤 배포(`clone_and_deploy`)는 tests/helpers/mcp_servers.py 에 있다. 옛
"프로필 하나에 인스턴스 여럿" 자리가 복제로 옮겨 간 모양이다(D42).
"""

from __future__ import annotations

from tests.helpers.clusters import register_cluster
from tests.helpers.consts import DEFAULT_NS, GV, MS, T2
from tests.helpers.http import deployed, detail_code
from tests.helpers.mcp_servers import (
    clone_and_deploy,
    mcp_server_env,
)


def test_validation_conflicts_and_pins(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    assert clone_and_deploy(client, ms).status_code == 201
    r = clone_and_deploy(client, ms)
    assert detail_code(r) == (409, "name_conflict")
    # 새 규칙은 대문자·언더스코어를 받지 않는다 (D24). 규칙 안에서는 k8s_name 이 항등이라 다른
    # 이름이 같은 CR 이름을 갖는 일이 없다
    r = clone_and_deploy(client, ms, name="Calc_1")
    assert detail_code(r) == (422, "invalid_name")
    r = clone_and_deploy(client, ms, name="bad name")
    assert detail_code(r) == (422, "invalid_name")
    assert client.post(f"{MS}/nope/deploy", json={}).status_code == 404
    r = clone_and_deploy(client, ms, name="x", cluster_id="nope")
    assert detail_code(r) == (422, "unknown_cluster")
    # 다른 테넌트에서는 문서가 안 보인다
    r = client.post(f"{MS}/{ms['id']}/deploy", json={}, headers=T2)
    assert r.status_code == 404

    # 배포는 현재 리비전을 핀한다. 저장하면 리비전이 늘고 핀은 그대로다
    inst = client.get(f"{MS}/{next(d['id'] for d in deployed(client, MS))}").json()
    client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
        },
    )
    got = client.get(f"{MS}/{inst['id']}").json()
    assert (got["revision"], got["deployed_revision"], got["state"]) == (2, 1, "deployed")
    # 새로 배포하는 복제본은 복제 시점의 현재 리비전(r2 의 정의)을 r1 로 핀한다
    r = clone_and_deploy(client, ms, name="latest")
    assert r.status_code == 201 and r.json()["deployed_revision"] == 1
    script = fake_clusters.by_name["edge"].get(GV, "MCPServer", "mcp-latest", DEFAULT_NS)["spec"][
        "podTemplateSpec"
    ]["spec"]["initContainers"][0]["args"][0]
    assert f"/mcp-servers/{r.json()['id']}/revisions/1/profile.json" in script

    # 배포된 문서가 있으면 클러스터 삭제 409
    r = client.delete(f"/clusters/{cid}")
    assert detail_code(r) == (409, "instances_exist")


def test_binding_cluster_mismatch_at_deploy_time(client, fake_clusters):
    cid, bp, ds, ms = mcp_server_env(client)
    other = register_cluster(client, "c2")
    r = clone_and_deploy(client, ms, cluster_id=other)
    assert detail_code(r) == (422, "cluster_mismatch")
    assert deployed(client, MS) == []
