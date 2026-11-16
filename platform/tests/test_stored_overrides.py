"""잠금 표가 넓어지기 전에 저장된 overrides 가 지금 잠긴 경로를 건드리는 경우 (D27, D42).

overrides 는 저장할 때 그때의 잠금 표로 검사하므로, 잠금 표가 넓어지면 지금은 잠긴 경로를 건드리는
것이 남을 수 있다. 세 리소스(MCP Server·Gateway·Agent) 모두 적용 경로 어디서도 그것을 클러스터로
보내지 않는다. 재배포·드리프트 되돌리기는 클러스터를 건드리기 전에 422 locked_field 와 그 경로로
막히고, 리컨실의 provision 은 같은 이유를 last_error 에 남기며, 편집기는 그 줄을 잠긴 줄로 보이고,
그 줄을 뺀 저장은 지나가 다시 적용된다. 리소스마다 다른 것(배포, 서비스, 편집하는 클러스터 객체,
잠긴 줄)은 `Resource` 어댑터가 들고 API 경로와 컬렉션은 tests/helpers/docs.py 의 `INSTANCE_KINDS`
에서 온다. 서비스 단위의 흐름은 test_advanced_edit.py 가 본다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from daegam.errors import ServiceError
from daegam.k8s.merge import merge
from daegam.services.agents import AgentError
from daegam.services.gateways import GatewayError
from daegam.services.mcp_servers import MCPServerError
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.consts import GV
from tests.helpers.docs import INSTANCE_KINDS, InstanceKind
from tests.helpers.gateways import gateway_up
from tests.helpers.hub import hub_env
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

# 리소스를 배포하고 배포 응답을 낸다. (client, fake_clusters, mongo)
Deploy = Callable[[Any, Any, Any], dict[str, Any]]


def _mcp_server(client, fake_clusters, mongo) -> dict[str, Any]:
    _cid, _bp, _ds, mp = mcp_server_env(client)
    return clone_and_deploy(client, mp).json()


def _gateway(client, fake_clusters, mongo) -> dict[str, Any]:
    _cid, items, *_rest = hub_env(client, fake_clusters)
    return gateway_up(client, items, name="a")


def _agent(client, fake_clusters, mongo) -> dict[str, Any]:
    return deployed_agent(client, fake_clusters, mongo, runtime=False).doc


def _container_cpu(root: str, container: str) -> dict[str, Any]:
    """파드 템플릿(`root`)의 컨테이너 하나에 CPU 상한을 주는 overrides 다. 지금의 잠금 표로도
    저장할 수 있는 값이다."""
    limits = {"name": container, "resources": {"limits": {"cpu": "2"}}}
    return {"spec": {root: {"spec": {"containers": [limits]}}}}


@dataclass(frozen=True)
class Resource:
    """표의 한 행. 리소스 하나를 이 테스트에 맞추는 어댑터다."""

    # API 경로·컬렉션·이름을 든 리소스
    instance: InstanceKind
    # app.state 의 서비스 이름
    service: str
    # 그 서비스가 내는 오류
    error: type[ServiceError]
    # Advanced 편집이 고치는 클러스터 객체의 apiVersion·kind
    api_version: str
    kind: str
    deploy: Deploy
    # 지금의 잠금 표로도 저장할 수 있는 overrides
    saved: dict[str, Any]
    # 잠금 표가 넓어져 지금은 잠긴 줄과 그 경로
    line: dict[str, Any]
    path: str
    # 그 줄을 잠그는 잠금 표의 줄
    lock: str


# 사이드카의 hostPort 다. 지금 잠금 표는 이 경로를 잠근다
SIDECAR = {"name": "side", "image": "busybox", "ports": [{"containerPort": 80, "hostPort": 80}]}
MCP_SERVER = Resource(
    instance=INSTANCE_KINDS["mcp_server"],
    service="mcp_server_service",
    error=MCPServerError,
    api_version=GV,
    kind="MCPServer",
    deploy=_mcp_server,
    saved=_container_cpu("podTemplateSpec", "mcp"),
    line={"spec": {"serviceAccount": "toolhive-operator"}},
    path="spec.serviceAccount",
    lock="spec.serviceAccount",
)
GATEWAY = Resource(
    instance=INSTANCE_KINDS["gateway"],
    service="gateway_service",
    error=GatewayError,
    api_version=GV,
    kind="VirtualMCPServer",
    deploy=_gateway,
    saved={"spec": {"replicas": 2}},
    line={"spec": {"serviceAccount": "default"}},
    path="spec.serviceAccount",
    lock="spec.serviceAccount",
)
AGENT = Resource(
    instance=INSTANCE_KINDS["agent"],
    service="agent_service",
    error=AgentError,
    api_version="apps/v1",
    kind="Deployment",
    deploy=_agent,
    saved=_container_cpu("template", "agent"),
    line={"spec": {"template": {"spec": {"containers": [SIDECAR]}}}},
    path="spec.template.spec.containers[name=side].ports[containerPort=80].hostPort",
    lock="spec.template.spec.containers.*.ports.*.hostPort",
)
# 테스트마다 세 리소스로 한 번씩 돈다. 테스트 id 는 리소스 이름이다
RESOURCES = [pytest.param(res, id=res.instance.name) for res in (MCP_SERVER, GATEWAY, AGENT)]


@pytest.mark.parametrize("res", RESOURCES)
def test_stored_overrides_on_a_now_locked_path_never_reach_the_cluster(
    client, fake_clusters, mongo, res: Resource
):
    """재배포·드리프트 되돌리기는 422 locked_field 와 그 경로로 막히고 리비전과 상태는 그대로다.
    편집기는 그 줄을 얹어 보이고, 리컨실이 모는 failed 문서의 provision 은 같은 이유를 last_error
    에 남긴다. 클러스터 객체는 끝까지 그대로다. 그 줄을 뺀 저장은 지나가고 다시 적용된다."""
    doc = res.deploy(client, fake_clusters, mongo)
    url = f"{res.instance.prefix}/{doc['id']}"
    collection = mongo[res.instance.collection]
    fake = fake_clusters.by_name["edge"]

    def live() -> dict[str, Any] | None:
        return fake.get(res.api_version, res.kind, doc["k8s"]["name"], doc["k8s"]["namespace"])

    before = live()
    stale = merge(res.saved, res.line)
    collection.update_one({"_id": doc["id"]}, {"$set": {"overrides": stale}})

    for r in (client.post(f"{url}/redeploy"), client.post(f"{url}/drift/reset")):
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "locked_field"
        assert r.json()["detail"]["path"] == res.path
    assert live() == before
    got = client.get(url).json()
    assert got["state"] == "deployed" and got["deployed_revision"] == 1

    documents = client.get(f"{url}/manifest").json()["documents"]
    [edited] = [d for d in documents if d["kind"] == res.kind]
    assert merge(edited["final"], res.line) == edited["final"]
    assert res.lock in edited["locked"]
    assert edited["drift"] is None and edited["error"].startswith("locked_field: ")

    collection.update_one({"_id": doc["id"]}, {"$set": {"state": "failed"}})
    state = client.app.state
    stored = client.portal.call(getattr(state, res.instance.collection).get, doc["id"])
    with pytest.raises(res.error) as e:
        client.portal.call(getattr(state, res.service).provision, stored)
    assert e.value.code == "locked_field" and e.value.extra["path"] == res.path
    saved = collection.find_one({"_id": doc["id"]})
    assert saved["state"] == "failed" and saved["last_error"].startswith("locked_field: ")
    assert live() == before

    r = client.put(f"{url}/overrides", json={"patch": res.saved})
    assert r.status_code == 200, r.text
    assert client.post(f"{url}/drift/reset").status_code == 200
