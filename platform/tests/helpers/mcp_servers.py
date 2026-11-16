"""MCP Server 를 만들고 배포하는 테스트가 함께 쓰는 도우미 (D42).

`mcp_server_env` 는 header 모드 플랫폼에 클러스터·Backend API·Database 와 draft 하나(calc-mcp)를
깐다. `replicated_server` 는 그 정의를 replicas 2 로 고쳐 세션 저장소를 쓰게 한다.
`clone_and_deploy` 는 그 정의를 이름을 바꿔 복제한 뒤 배포한다. 옛 "프로필 하나에 인스턴스 여럿"
자리가 복제로 옮겨 간 모양이다. `two_backend_server` 는 그 정의를 backend 둘로 고쳐 배포한다.
"""

from __future__ import annotations

from tests.helpers.backend_apis import CALC_ENDPOINT, uploaded_backend
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import BP, ISSUER, MS
from tests.helpers.data_services import created_database
from tests.helpers.http import detail_code


def soap_headers_of(client, bp: dict, secret: str) -> None:
    """Backend API bp 의 soap:header 값 Secret 을 정한다 (T60). 업로드 폼은 그 칸을 받지 않아
    PATCH 다."""
    r = client.patch(f"{BP}/{bp['id']}", json={"soap_headers_secret": secret})
    assert r.status_code == 200, r.text


def mcp_server_env(
    client,
    *,
    cluster_extra=None,
    ds=True,
    ds_password="s3cret",
    backend_auth=None,
    soap_headers_secret=None,
):
    """클러스터(플랫폼 기본), Backend API(calculator), Database(redis), MCP Server draft(calc-mcp,
    도구 [Add])를 깔고 넷을 그 순서로 돌려준다. soap_headers_secret 은 Backend API 의 soap:header
    값 Secret 이다."""
    cid = default_cluster(client, platform_internal_url=ISSUER, **(cluster_extra or {}))
    form = {"endpoint": CALC_ENDPOINT}
    if backend_auth:
        form.update(auth=backend_auth, credential_secret="calc-creds")
    bp = uploaded_backend(client, **form)
    if soap_headers_secret:
        soap_headers_of(client, bp, soap_headers_secret)
    bindings = []
    ds_doc = None
    if ds:
        ds_doc = created_database(
            client, "cache", "redis", db=2, username="app", password=ds_password or None
        )
        bindings = [{"data_service_id": ds_doc["id"], "env_prefix": "CACHE"}]
    r = client.post(
        MS,
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
            "bindings": bindings,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "draft"
    return cid, bp, ds_doc, r.json()


def replicated_server(client, bp, ms, *, bindings=None):
    """정의 `ms` 를 replicas 2 로 고친다. 세션 저장소가 필요해 bindings(없으면 비움)의 Redis 나
    클러스터 기본 Redis 를 쓴다. 고친 응답의 본문이다."""
    r = client.put(
        f"{MS}/{ms['id']}",
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
            "bindings": bindings or [],
            "runtime": {"replicas": 2},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def create_server(client, bp, name="calc-mcp", **extra):
    """Backend API bp 의 v1 로 정의만 만든다 (draft). 응답 그대로."""
    body = {"name": name, "backend_profile_id": bp["id"], "spec_version": 1, **extra}
    return client.post(MS, json=body)


def clone_and_deploy(client, ms, name="calc-1", *, headers=None, **extra):
    """정의 `ms` 를 `name` 으로 복제해 배포한다. 이름이 같으면 그 문서 자체를 배포한다.
    같은 이름의 draft 가 남아 있으면(앞선 배포가 실패한 복제본) 그것을 배포한다. 복제가 막히면
    (배포된 것과 이름 충돌 등) 그 응답을 그대로 돌려준다. 배포 본문은 cluster_id·namespace·
    exposure 다. display_name 은 복제가 받는다."""
    h = headers or {}
    if name != ms["name"]:
        clone_body = {"name": name}
        if "display_name" in extra:
            clone_body["display_name"] = extra.pop("display_name")
        r = client.post(f"{MS}/{ms['id']}/clone", json=clone_body, headers=h)
        if detail_code(r) == (409, "name_conflict"):
            left = next((d for d in client.get(MS, headers=h).json() if d["name"] == name), None)
            if left is None or left["state"] != "draft":
                return r
            target = left
        elif r.status_code != 201:
            return r
        else:
            target = r.json()
    else:
        target = ms
    return client.post(f"{MS}/{target['id']}/deploy", json=extra, headers=h)


# `two_backend_server` 가 더하는 둘째 Backend API 의 endpoint
PETS_ENDPOINT = "https://api.pets/v1"


def two_backend_server(client, ms, bp, *, name="combo-1", pets_soap_headers_secret=None, **pets):
    """정의 `ms` 를 backend 둘(`bp` 의 Add, 새 Backend API pets 의 getPet)로 고쳐 `name` 으로
    배포한다. pets 는 둘째 Backend API 의 업로드 칸(endpoint·auth·credential_secret)을 덮고,
    pets_soap_headers_secret 은 그 soap:header 값 Secret 이다. (배포 응답의 본문, pets 문서)다."""
    pets_bp = uploaded_backend(
        client, "pets", "legacy-swagger.json", **{"endpoint": PETS_ENDPOINT, **pets}
    )
    if pets_soap_headers_secret:
        soap_headers_of(client, pets_bp, pets_soap_headers_secret)
    backends = [
        {"backend_profile_id": bp["id"], "spec_version": 1, "tools": ["Add"]},
        {"backend_profile_id": pets_bp["id"], "spec_version": 1, "tools": ["getPet"]},
    ]
    r = client.put(f"{MS}/{ms['id']}", json={"name": ms["name"], "backends": backends})
    assert r.status_code == 200, r.text
    r = clone_and_deploy(client, ms, name=name)
    assert r.status_code == 201, r.text
    return r.json(), pets_bp
