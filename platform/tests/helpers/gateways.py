"""Gateway 를 만들고 배포하는 테스트가 함께 쓰는 도우미 (D42). 백엔드는 tests/helpers/hub.py 의
`hub_env` 가 깐 허브 항목(calculator·orders)이다."""

from __future__ import annotations

from daegam.services.gateway_plan import surface_forbids
from daegam.store.base import new_id
from daegam.store.gateways import GatewayBackendSnapshot, GatewayK8s, GatewaySurface
from tests.helpers.consts import DEFAULT_NS, GV, GW
from tests.helpers.docs import gateway_doc

# 표면을 끈 리비전이 vMCP Cedar 에 더하는 forbid (P50 13절). 새 Gateway 는 둘 다 끈다
SURFACE_OFF = surface_forbids(GatewaySurface())
FORBID_RESOURCES, FORBID_PROMPTS = SURFACE_OFF
# 도구 호출만 받는 소유자 정책 한 문장
CALL_ONLY = 'permit(principal, action == Action::"call_tool", resource);'


def create_gateway(client, items, name="a", backends=None, *, headers=None, **extra):
    """정의만 만든다 (draft). 응답 그대로. backends 를 주지 않으면 허브 항목 items 의 calculator 의
    Add 와 orders 다."""
    backends = (
        backends
        if backends is not None
        else [
            {"hub_item_id": items["calculator"]["id"], "tools": ["Add"]},
            {"hub_item_id": items["orders"]["id"]},
        ]
    )
    return client.post(GW, json={"name": name, "backends": backends, **extra}, headers=headers)


def deploy_gateway(client, gateway_id, *, headers=None, **body):
    return client.post(f"{GW}/{gateway_id}/deploy", json=body, headers=headers)


def gateway_up(client, items, name="a", backends=None, *, deploy=None, **extra) -> dict:
    """만들고 배포한다. 배포 응답 JSON."""
    r = create_gateway(client, items, name=name, backends=backends, **extra)
    assert r.status_code == 201, r.text
    d = deploy_gateway(client, r.json()["id"], **(deploy or {}))
    assert d.status_code == 201, d.text
    return d.json()


def vmcp_of(fake, name):
    """기본 네임스페이스에 렌더된 VirtualMCPServer. 없으면 None."""
    return fake.get(GV, "VirtualMCPServer", name, DEFAULT_NS)


def cedar_policies(cr: dict, documents: list[dict] | None = None, fake=None) -> list[str]:
    """vMCP 나 노출 앞단이 쓰는 Cedar 정책. 인라인이면 그것이고, MCPAuthzConfig 를 참조하면(P50 8절)
    documents(미리보기의 묶음)나 fake 클러스터에서 그 설정을 찾는다."""
    spec = cr["spec"]
    auth = spec.get("incomingAuth", spec)
    if "authzConfig" in auth:
        return auth["authzConfig"]["inline"]["policies"]
    name = auth["authzConfigRef"]["name"]
    if documents is not None:
        config = next(d for d in documents if d["kind"] == "MCPAuthzConfig")
    else:
        config = fake.get(GV, "MCPAuthzConfig", name, cr["metadata"]["namespace"])
    assert config["metadata"]["name"] == name
    return config["spec"]["config"]["policies"]


def failure_mode(cr) -> str:
    """렌더된 VirtualMCPServer 의 부분 실패 모드."""
    return cr["spec"]["config"]["operational"]["failureHandling"]["partialFailureMode"]


def backend_snapshot(name: str, kind: str, names: list[str], **extra) -> GatewayBackendSnapshot:
    """Gateway 배포 스냅샷의 백엔드 하나. names 는 광고하는 도구 이름(`tool_names`)이고 extra 는 그
    밖의 칸(`tools` 필터 등)이다. workload 는 `gw-<name>` 이고 hub_item_id 는 `hi-<name>` 이다."""
    return GatewayBackendSnapshot(
        hub_item_id=extra.pop("hub_item_id", f"hi-{name}"),
        hub_item_name=name,
        kind=kind,
        workload=extra.pop("workload", f"gw-{name}"),
        url=extra.pop("url", "http://backend"),
        tool_names=names,
        **extra,
    )


def seed_deployed_backends(
    mongo, gateway_id: str, snapshots: list[GatewayBackendSnapshot], resolution: str = "priority"
) -> None:
    """Mongo 의 Gateway 문서에 배포 스냅샷의 백엔드를 둔다. 서비스를 거치지 않고 소스의 Tool 을
    바꾼다."""
    mongo["gateways"].update_one(
        {"_id": gateway_id},
        {
            "$set": {
                "deployed_backends": [s.model_dump() for s in snapshots],
                "deployed_conflict_resolution": resolution,
            }
        },
    )


# 계산기(remote)와 플랫폼 web-fetch 를 둔 Gateway 의 배포 스냅샷
WEB_BACKENDS = [
    backend_snapshot("spike", "remote", ["Add"]),
    backend_snapshot("web-fetch", "platform", ["web_fetch"]),
]


def seed_search_gateway(mongo, tenant: str = "default", k8s_name: str = "gw-a") -> str:
    """플랫폼 web-search 백엔드를 둔 배포된 Gateway 를 Mongo 에 넣고 그 id 를 돌려준다. 검색
    엔드포인트는 토큰의 `svc:vmcp:<k8s_name>` 으로 이 문서를 찾는다."""
    doc = gateway_doc(
        new_id(),
        tenant=tenant,
        name=k8s_name.removeprefix("gw-"),
        state="deployed",
        k8s=GatewayK8s(name=k8s_name, namespace=f"daegam-{tenant}", group=k8s_name),
        deployed_backends=[backend_snapshot("web-search", "platform", ["web_search"])],
    )
    mongo["gateways"].insert_one(doc.to_doc())
    return doc.id
