"""스토어 모델 문서를 만든다. 테스트는 이 문서를 Mongo 에 바로 넣거나 스토어·서비스에 넘긴다.

키워드로 받은 모델 필드는 기본값을 그대로 덮는다. MCP Server·Gateway·Agent 는 테넌트 acme 의
문서이고 spec 이 리비전 1 하나다. 렌더 함수에 넘기는 유효 설정(`effective_config`)도 테넌트 acme 의
것이다. `pinned` 를 주면 배포가 핀한 옛 리비전 1 과 현재 리비전 2 를
갖는다. draft 가 아니면 cluster 의 테넌트 네임스페이스 `daegam-<tenant>` 에 배포된 모양이다.
`INSTANCE_KINDS` 는 세 리소스마다 API 경로·컬렉션·빌더·scope 이름을 든다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from daegam.config.resolve import EffectiveConfig, EffectiveImages, EffectiveNamespace
from daegam.services.instance_status import ROLLING_GRACE
from daegam.store.agents import Agent, AgentK8s, AgentSpec, k8s_name_for
from daegam.store.agents import token_secret_name_for as agent_token_secret
from daegam.store.base import utcnow
from daegam.store.clusters import Cluster, ClusterConnection, ClusterIngress, ClusterScheduling
from daegam.store.data_services import DataService
from daegam.store.exposure import AgentExposure, Exposure
from daegam.store.gateways import Gateway, GatewayK8s, GatewaySpec
from daegam.store.knowledge import Embedding, KnowledgeBase
from daegam.store.mcp_servers import InstanceK8s, MCPServer, MCPServerSpec
from daegam.store.mcp_servers import token_secret_name_for as server_token_secret
from tests.helpers.consts import ACME_NS, AG, GW, ISSUER, MS, TIME_MARGIN

TENANT = "acme"
# 백엔드는 하나 이상이어야 하므로 MCP Server 의 기본 spec 은 Backend API 하나를 쓴다
MCP_SPEC: dict[str, Any] = {"backends": [{"backend_profile_id": "bp-1", "spec_version": 1}]}


def _instance[D](
    model: Callable[..., D],
    spec_model: type[BaseModel],
    k8s_of: Callable[[str, str, str], BaseModel],
    doc_id: str,
    spec: dict[str, Any],
    /,
    *,
    pinned: dict[str, Any] | None = None,
    tenant: str = TENANT,
    state: str = "draft",
    cluster: str = "c1",
    **fields: Any,
) -> D:
    """세 인스턴스 문서의 공통 모양. `k8s_of(id, name, tenant)` 가 배포 이름을 만들고 fields 는
    모델 필드(`k8s` 포함)를 그대로 덮는다."""
    name = fields.pop("name", doc_id)
    current = spec_model.model_validate(spec)
    specs = [current] if pinned is None else [spec_model.model_validate(pinned), current]
    doc: dict[str, Any] = {
        "id": doc_id,
        "tenant": tenant,
        "name": name,
        "spec": current,
        "revision": len(specs),
        "revisions": [{**s.model_dump(), "revision": n} for n, s in enumerate(specs, 1)],
        "state": state,
    }
    if pinned is not None:
        doc["deployed_revision"] = 1
    if state != "draft":
        doc.setdefault("deployed_revision", doc["revision"])
        doc.update(cluster_id=cluster, k8s=k8s_of(doc_id, name, tenant), effective_config={})
    return model(**{**doc, **fields})


def _namespace(tenant: str) -> str:
    return f"daegam-{tenant}"


def _mcp_server_k8s(sid: str, name: str, tenant: str) -> InstanceK8s:
    return InstanceK8s(
        name=f"mcp-{name}",
        namespace=_namespace(tenant),
        group=tenant,
        token_secret=server_token_secret(sid),
    )


def _gateway_k8s(_gid: str, name: str, tenant: str) -> GatewayK8s:
    return GatewayK8s(name=f"gw-{name}", namespace=_namespace(tenant), group=f"gw-{name}")


def _agent_k8s(aid: str, name: str, tenant: str) -> AgentK8s:
    return AgentK8s(
        name=k8s_name_for(name), namespace=_namespace(tenant), token_secret=agent_token_secret(aid)
    )


def mcp_server_doc(
    sid: str = "mcp-1", *, spec: dict[str, Any] | None = None, **kw: Any
) -> MCPServer:
    """spec 이 없으면 `MCP_SPEC` 이다. kw 는 `_instance` 의 키워드와 모델 필드다."""
    return _instance(MCPServer, MCPServerSpec, _mcp_server_k8s, sid, spec or MCP_SPEC, **kw)


def gateway_doc(gid: str = "gw-1", *, spec: dict[str, Any] | None = None, **kw: Any) -> Gateway:
    """spec 이 없으면 백엔드가 없는 Gateway 다. kw 는 `_instance` 의 키워드와 모델 필드다."""
    return _instance(Gateway, GatewaySpec, _gateway_k8s, gid, spec or {}, **kw)


def agent_doc(aid: str = "ag-1", *, spec: dict[str, Any] | None = None, **kw: Any) -> Agent:
    """spec 이 없으면 노드가 없는 그래프다. kw 는 `_instance` 의 키워드와 모델 필드다."""
    return _instance(Agent, AgentSpec, _agent_k8s, aid, spec or {}, **kw)


@dataclass(frozen=True)
class InstanceKind:
    """배포하는 리소스 셋(MCP Server·Gateway·Agent, D42) 가운데 하나다. 테스트가 리소스마다 달리
    쓰는 값을 든다."""

    name: str
    # 제어면 API 경로
    prefix: str
    # Mongo 컬렉션이자 app.state 의 저장소 이름
    collection: str
    # 이 모듈의 문서 빌더
    build: Callable[..., Any]
    # 노출 모델
    exposure: type[BaseModel]
    # scope 의 리소스 이름. `<scope>:write`·`<scope>:deploy` 로 쓴다
    scope: str


# 이름 → 리소스. 세 리소스로 한 번씩 도는 테스트가 이 표를 쓴다
INSTANCE_KINDS: dict[str, InstanceKind] = {
    kind.name: kind
    for kind in (
        InstanceKind("mcp_server", MS, "mcp_servers", mcp_server_doc, Exposure, "mcp"),
        InstanceKind("gateway", GW, "gateways", gateway_doc, Exposure, "gateway"),
        InstanceKind("agent", AG, "agents", agent_doc, AgentExposure, "agent"),
    )
}


def agent_refs_spec(
    *,
    source: tuple[str, str] | None = None,
    kb_id: str | None = None,
    ds_id: str | None = None,
    skill: tuple[str, int] | None = None,
) -> dict[str, Any]:
    """참조만 담은 Agent spec. 도구 소스 (kind, id)·Knowledge Base·Database 를 주면 그 노드가, skill
    (id, revision) 을 주면 메인 에이전트의 Skill 핀이 붙는다. 간선은 없다."""
    main: dict[str, Any] = {"role": "main", "name": "A"}
    if skill is not None:
        main["skills"] = [{"id": skill[0], "revision": skill[1]}]
    nodes: list[dict[str, Any]] = [
        {"id": "entry", "type": "entry", "data": {}},
        {"id": "main", "type": "agent", "data": main},
    ]
    if source is not None:
        nodes.append(
            {
                "id": "tools",
                "type": "tools",
                "data": {"source": {"kind": source[0], "id": source[1]}},
            }
        )
    if kb_id is not None:
        nodes.append({"id": "know", "type": "knowledge", "data": {"knowledge_base_id": kb_id}})
    if ds_id is not None:
        nodes.append(
            {"id": "mem", "type": "memory", "data": {"checkpointer": {"data_service_id": ds_id}}}
        )
    return {"graph": {"nodes": nodes, "edges": []}, "service": {}}


def knowledge_base_doc(
    name: str = "faq",
    *,
    tenant: str = "default",
    provider: str = "local-stub",
    model: str = "stub",
    dimensions: int = 64,
    **fields: Any,
) -> KnowledgeBase:
    """테넌트 default·Database ds-pg 의 Knowledge Base. 임베딩은 provider·model·dimensions 다."""
    embedding = Embedding(provider=provider, model=model, dimensions=dimensions)
    base = {"tenant": tenant, "name": name, "data_service_id": "ds-pg", "embedding": embedding}
    return KnowledgeBase(**{**base, **fields})


def data_service_doc(name: str = "pg", *, kind: str = "postgres", **fields: Any) -> DataService:
    """테넌트 t·클러스터 c 의 Database. postgres 면 database app·username u 로 접속한다."""
    base: dict[str, Any] = {
        "tenant": "t",
        "name": name,
        "cluster_id": "c",
        "kind": kind,
        "host": "h",
        "port": 5432 if kind == "postgres" else 6379,
        "secret_name": "s",
    }
    if kind == "postgres":
        base.update(database="app", username="u")
    return DataService(**{**base, **fields})


def cluster_doc(
    name: str = "home",
    *,
    mode: str = "in_cluster",
    secret_name: str | None = None,
    api_url: str | None = None,
    kubecontext: str | None = None,
    **fields: Any,
) -> Cluster:
    """mode 와 그 접속 정보로 붙는 클러스터. 기본은 플랫폼과 같은 클러스터(in_cluster)이고 플랫폼
    주소는 `ISSUER` 다."""
    connection = ClusterConnection(
        mode=mode, secret_name=secret_name, api_url=api_url, kubecontext=kubecontext
    )
    base = {"name": name, "connection": connection, "platform_internal_url": ISSUER}
    return Cluster(**{**base, **fields})


def effective_config(**fields: Any) -> EffectiveConfig:
    """테넌트 acme·클러스터 c1(edge)의 유효 설정. 스케줄링과 이미지 규칙이 모두 찬 모양이고
    fields 는 모델 필드를 그대로 덮는다."""
    base: dict[str, Any] = {
        "tenant": TENANT,
        "cluster_id": "c1",
        "cluster_name": "edge",
        "platform_internal_url": ISSUER,
        "namespace": EffectiveNamespace(name=ACME_NS),
        "images": EffectiveImages(
            runtime_image="daegam-mcp-runtime:0.1.0",
            agent_image="registry.local/daegam-agent-runtime:0.2.0",
            pull_secret="regcred",
        ),
        "scheduling": ClusterScheduling(
            resources={"limits": {"cpu": "1"}},
            node_selector={"pool": "agents"},
            tolerations=[{"key": "agents", "operator": "Exists"}],
            priority_class="low",
        ),
        "ingress": ClusterIngress(),
        "replicas": 2,
        "resources": {"limits": {"cpu": "1"}},
        "model": "m",
    }
    return EffectiveConfig(**{**base, **fields})


def age(
    mongo, collection: str, doc_id: str, *, by: timedelta = ROLLING_GRACE + TIME_MARGIN
) -> None:
    """문서의 마지막 spec 변경을 by 만큼 과거로 민다. 기본값은 스펙 변경 직후의 실패를 Rolling
    으로 보는 유예(`ROLLING_GRACE`)를 넘긴다."""
    mongo[collection].update_one({"_id": doc_id}, {"$set": {"last_spec_change": utcnow() - by}})


def ids(docs: Iterable[Any]) -> list[str]:
    """문서 id 를 정렬한 목록. 스토어 질의의 결과를 순서 없이 비교한다."""
    return sorted(d.id for d in docs)


def insert_docs(mongo, **docs: list[Any]) -> None:
    """컬렉션 이름 → 스토어 모델 목록을 Mongo 에 바로 넣는다."""
    for coll, items in docs.items():
        if items:
            mongo[coll].insert_many([d.to_doc() for d in items])


# -- 노출한 문서 ---------------------------------------------------------------------------------
# 다른 클러스터에서 살아 있는 노출을 Mongo 에 바로 넣어 만든다. 앞단이 TLS 를 끝내는 노출이라 공개
# 주소가 https 다. 노출 host 검사와 OAuth 의 대상(resource) 풀기가 쓴다


def edge_exposure(host: str) -> dict:
    return {"enabled": True, "host": host, "ingress_class": "traefik", "tls": {"mode": "edge"}}


def seed_exposed(mongo, kind: str, doc_id: str, host: str, *, cluster: str = "c1") -> None:
    """kind(`INSTANCE_KINDS` 의 열쇠)의 배포된 문서 doc_id 를 host 로 노출한 모양으로 넣는다."""
    instance = INSTANCE_KINDS[kind]
    doc = instance.build(doc_id, state="deployed", cluster=cluster)
    doc.exposure = instance.exposure(**edge_exposure(host))
    mongo[instance.collection].insert_one(doc.to_doc())


def seed_gateway(mongo, gid: str, host: str, *, cluster: str = "c1") -> None:
    seed_exposed(mongo, "gateway", gid, host, cluster=cluster)


def seed_server(mongo, sid: str, host: str, *, cluster: str = "c1") -> None:
    seed_exposed(mongo, "mcp_server", sid, host, cluster=cluster)


def seed_agent(mongo, aid: str, host: str, *, cluster: str = "c1") -> None:
    seed_exposed(mongo, "agent", aid, host, cluster=cluster)
