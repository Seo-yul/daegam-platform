"""클러스터를 쓰는 테스트가 함께 쓰는 도우미.

`post_cluster` 는 API 로 fake 클러스터를 등록한 응답을 그대로 주므로 등록 규칙을 보는 테스트가 쓰고,
`register_cluster` 는 그 id 만 필요한 준비 단계가 쓴다. 본문은 `cluster_body` 가 만든다.
`default_cluster` 는 등록한 것을 플랫폼 기본으로 잡는다.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient

from tests.helpers.consts import KUBECONFIG


def record_toolhive(mongo, cluster_id: str, version: str | None) -> None:
    """클러스터의 preflight 가 읽은 ToolHive 판을 둔다. 판 게이트(P50 2절)가 이 값을 본다. None 은
    판을 모르는 클러스터다."""
    mongo["clusters"].update_one(
        {"_id": cluster_id}, {"$set": {"status.preflight.toolhive_version": version}}
    )


def downgrade_toolhive(mongo, cluster_id: str, version: str = "0.50.0") -> None:
    """클러스터의 preflight 가 읽은 ToolHive 판을 낮춰, 판 게이트를 지나지 못한 클러스터로 둔다
    (P50 2절)."""
    record_toolhive(mongo, cluster_id, version)


def revoke_tenant_rights(mongo, cluster_id: str) -> None:
    """클러스터의 preflight 가 ClusterRole daegam-tenant-objects 를 묶을 수 없었던 것으로 둔다.
    권한이 필요한 게이트(G8, G10)를 지나지 못한다 (P50 2절)."""
    gates = {"gates.tenant_object_rights": False}
    mongo["clusters"].update_one({"_id": cluster_id}, {"$set": gates})


def revoke_secret_rights(mongo, cluster_id: str) -> None:
    """클러스터의 preflight 가 클러스터 전체 Secret 권한도, ClusterRole daegam-tenant-objects 를
    묶을 권한도 보지 못한 것으로 둔다. 테넌트 Secret 을 쓰는 길이 422 다 (P63 2절)."""
    revoke_tenant_rights(mongo, cluster_id)
    rights = {"status.preflight.secret_rights": {"cluster_wide": False, "writable": False}}
    mongo["clusters"].update_one({"_id": cluster_id}, {"$set": rights})


# 등록 본문의 platform_internal_url 기본값. 테스트 클러스터는 fake 라 이 주소로 부르지 않는다
PLATFORM_URL = "http://daegam.example:9000"


def cluster_body(name: str = "edge", **fields: Any) -> dict[str, Any]:
    """클러스터 name 의 등록 본문. 접속은 kubeconfig(KUBECONFIG)이고 fields 가 본문의 필드
    (connection·platform_internal_url·ingress·namespaces·images 등)를 덮는다."""
    return {
        "name": name,
        "connection": {"kubeconfig": KUBECONFIG},
        "platform_internal_url": PLATFORM_URL,
        **fields,
    }


def post_cluster(
    client: TestClient,
    name: str = "edge",
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> httpx.Response:
    """클러스터 name 을 등록한 응답이다. fields 는 `cluster_body` 의 것과 같다."""
    return client.post("/clusters", json=cluster_body(name, **fields), headers=headers)


def register_cluster(
    client: TestClient,
    name: str = "edge",
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> str:
    """클러스터 name 을 등록하고 그 id 를 돌려준다. fields 는 `cluster_body` 의 것과 같다. 201 이
    아니면 그 자리에서 실패한다."""
    r = post_cluster(client, name, headers=headers, **fields)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def default_cluster(
    client: TestClient, *, headers: dict[str, str] | None = None, **fields: Any
) -> str:
    """클러스터 edge 를 등록해 플랫폼 기본으로 잡고 그 id 를 돌려준다. 테스트 앱에는 home 클러스터가
    없으므로 배포하는 테스트가 먼저 부른다. fields 는 `register_cluster` 의 것과 같다."""
    cid = register_cluster(client, headers=headers, **fields)
    r = client.patch("/settings", json={"default_cluster_id": cid}, headers=headers)
    assert r.status_code == 200, r.text
    return cid
