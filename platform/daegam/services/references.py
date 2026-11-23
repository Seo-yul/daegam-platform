"""리소스 참조 (D43 셋째). "이것을 쓰는 것" 을 한 서비스가 답한다.

삭제·배포 해제 가드가 도메인마다 따로 세던 것(`gateways_using_hub_item`,
`referencing_knowledge_base`, 라우터에 박힌 Backend API 가드)을 여기로 모은다. 저장하지 않고 요청
때 스토어에서 센다. 그래야 리비전마다 무효화를 관리하지 않는다.

항목마다 `revision` 이 붙는다. `current` 는 현재 리비전(draft 포함)이 붙드는 것, `deployed` 는
배포가 핀한 리비전이 붙드는 것, `both` 는 둘 다다. 삭제 가드는 앞의 둘을, 배포 해제 가드는 뒤의
둘을 본다. 문서 상태(`state`)는 이 둘을 대신하지 못한다. 배포된 Agent 가 핀한 리비전으로는 그것을
안 쓰는 경우가 있기 때문이다.

세는 재료는 지금 가드가 쓰는 스토어 질의 그대로다. 그래서 가드를 이 서비스로 옮겨도 판정이 바뀌지
않는다. 질의가 현재 리비전만 보는 자리(도구 소스)는 배포 쪽도 그 문서 안에서만 읽는다.

`tenant` 가 None 이면 거르지 않는다. 가드가 그렇게 부른다. 다른 테넌트의 참조도 삭제를 막아야 하기
때문이다. Cluster 는 반대다. 삭제 가드는 플랫폼 전체를 보는 `cluster_service.instance_checker` 로
남고, 여기서는 요청 테넌트가 그 클러스터에 배포한 것만 답한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from daegam.errors import ServiceError
from daegam.services.agent_graph import references_knowledge_base, references_tool_source
from daegam.services.agent_skills import skill_pins
from daegam.store.agents import MongoAgentStore
from daegam.store.backend_apis import BackendAPIStore
from daegam.store.data_services import MongoDataServiceStore
from daegam.store.gateways import Gateway, MongoGatewayStore
from daegam.store.hub import MongoHubStore
from daegam.store.knowledge import MongoKnowledgeStore
from daegam.store.mcp_servers import MongoMCPServerStore

# 참조하는 쪽의 종류. `token` 만 화면에서 링크가 아니라 개수다
ReferenceKind = Literal[
    "backend-api",
    "mcp-server",
    "gateway",
    "agent",
    "data-service",
    "knowledge-base",
    "hub-item",
    "token",
]
# 참조 대상의 종류. 라우터 아홉이 이 값을 넘긴다
TargetKind = Literal[
    "backend-api",
    "mcp-server",
    "gateway",
    "agent",
    "data-service",
    "knowledge-base",
    "hub-item",
    "cluster",
    "skill",
]
RevisionRef = Literal["current", "deployed", "both"]


class PinnedRevisions(BaseModel):
    """Agent 가 이 Skill 의 어느 리비전을 핀했는가 (D64). 현재 리비전 쪽과 배포 쪽이고 붙들지 않는
    쪽은 null 이다. 화면이 "정의 r3, 배포 r2" 와 뒤처짐 배지를 그린다."""

    model_config = ConfigDict(extra="forbid")

    current: int | None = None
    deployed: int | None = None


class Reference(BaseModel):
    """참조 한 줄. 종류와 무관하게 모양이 하나다 (D43)."""

    model_config = ConfigDict(extra="forbid")

    kind: ReferenceKind
    id: str
    name: str
    display_name: str
    # 문서 상태. Backend API·Database·읽기 토큰처럼 상태가 없는 것은 null 이다
    state: str | None = None
    revision: RevisionRef
    # 간접 참조일 때 거친 것 (Gateway 가 허브 항목을 거쳐 MCP Server 를 쓰면 "hub-item")
    via: str | None = None
    # Skill 의 참조에만 있다. 나머지는 null 이다
    pins: PinnedRevisions | None = None


class _Collected:
    """같은 문서가 현재와 배포 양쪽에서 걸리면 `both` 로 합친다. 처음 본 순서를 지킨다.

    순서가 곧 가드의 id 순서라 스토어 질의가 주는 차례를 그대로 둔다."""

    def __init__(self) -> None:
        self._order: list[tuple[str, str]] = []
        self._by_key: dict[tuple[str, str], Reference] = {}

    def add(self, ref: Reference) -> None:
        key = (ref.kind, ref.id)
        seen = self._by_key.get(key)
        if seen is None:
            self._order.append(key)
            self._by_key[key] = ref
            return
        if seen.revision != ref.revision:
            self._by_key[key] = seen.model_copy(update={"revision": "both"})

    def all(self) -> list[Reference]:
        return [self._by_key[k] for k in self._order]


# 가드 칸이 보는 리비전 (P75 2절). 정의 쪽은 현재 리비전(draft 포함)이 붙드는 것, 배포 쪽은 배포가
# 핀한 리비전이 붙드는 것이다. 둘 다 붙들면 `both` 라 양쪽에 다 든다
CURRENT: frozenset[RevisionRef] = frozenset({"current", "both"})
DEPLOYED: frozenset[RevisionRef] = frozenset({"deployed", "both"})
ANY_REVISION: frozenset[RevisionRef] = frozenset({"current", "deployed", "both"})


@dataclass(frozen=True)
class GuardField:
    """삭제·배포 해제 가드 응답의 extra 칸 하나 (P75 2절). 그 칸에 드는 참조의 종류와 리비전이다.
    `detail` 은 처음 걸린 칸 하나로 답하는 가드가 그 칸의 문장으로 쓴다."""

    key: str
    kind: ReferenceKind
    revisions: frozenset[RevisionRef] = ANY_REVISION
    detail: str = ""

    def ids(self, refs: Sequence[Reference]) -> list[str]:
        return [r.id for r in refs if r.kind == self.kind and r.revision in self.revisions]


def blocking_fields(
    refs: Sequence[Reference], fields: Sequence[GuardField]
) -> dict[str, list[str]]:
    """찾은 것이 있는 칸 전부. 칸 순서를 지킨다. 비면 지울 수 있다."""
    out: dict[str, list[str]] = {}
    for field in fields:
        ids = field.ids(refs)
        if ids:
            out[field.key] = ids
    return out


def first_blocking(
    refs: Sequence[Reference], fields: Sequence[GuardField]
) -> tuple[GuardField, list[str]] | None:
    """칸 순서대로 처음 걸린 칸과 그 id. 아무것도 없으면 None 이다."""
    for field in fields:
        ids = field.ids(refs)
        if ids:
            return field, ids
    return None


# -- 가드 표 (P75 1절 표) ------------------------------------------------------------------
# 리소스마다 삭제·배포 해제를 막는 칸이다. 칸마다 참조 종류와 리비전이 다르고, 화면은 칸 이름으로
# 읽는다. 처음 걸린 칸 하나로 답하는 표는 칸의 `detail` 이 문장이다.

MCP_SERVER_IN_USE: tuple[GuardField, ...] = (
    # 허브 항목을 거쳐 쓰는 Gateway 는 정의든 배포 스냅샷이든 막는다
    GuardField("gateway_ids", "gateway"),
    GuardField("agent_ids", "agent", CURRENT),
)
# 허브 게시는 참조가 아니라 `GatewayService._check_published` 가 본다
GATEWAY_IN_USE: tuple[GuardField, ...] = (GuardField("agent_ids", "agent", CURRENT),)
DATA_SERVICE_DELETE: tuple[GuardField, ...] = (
    GuardField(
        "deployed_mcp_server_ids",
        "mcp-server",
        DEPLOYED,
        "data service is bound by deployed MCP servers",
    ),
    GuardField("mcp_server_ids", "mcp-server", CURRENT, "data service is bound by MCP servers"),
    GuardField("deployed_agent_ids", "agent", DEPLOYED, "data service is bound by deployed agents"),
    # Agent (D26): memory 노드가 이 Database 를 체크포인터로 쓰는 정의
    GuardField("agent_ids", "agent", CURRENT, "data service is used as the memory of agents"),
    # Knowledge (D32): 벡터 테이블을 이 Database 에 둔 Knowledge Base
    GuardField(
        "knowledge_base_ids",
        "knowledge-base",
        detail="data service holds the vector tables of knowledge bases",
    ),
)
BACKEND_API_DELETE: tuple[GuardField, ...] = (
    GuardField("mcp_server_ids", "mcp-server", CURRENT, "backend api is referenced by MCP servers"),
)
# Backend API 의 살아 있는 읽기 토큰. force 로 넘길 수 있어 표와 따로 본다
TOKEN_GUARD = GuardField(
    "token_ids",
    "token",
    detail="backend api has active read tokens; revoke them or pass ?force=true",
)
# 게시한 Agent 의 항목을 Tool 로 쓰는 Agent 는 막지 않는다. 주인은 언제든 게시를 거둔다 (P46 6.1절)
HUB_ITEM_DELETE: tuple[GuardField, ...] = (GuardField("gateway_ids", "gateway"),)
# tools 노드가 게시한 Agent 의 허브 항목을 가리키는 소스 종류 (P46 6.2절). 소스 id 는 항목 id 다
AGENT_SOURCE = "agent"
# Knowledge Base 와 Skill 은 두 칸을 늘 함께 싣는다
KNOWLEDGE_BASE_DELETE: tuple[GuardField, ...] = (
    GuardField("agent_ids", "agent", CURRENT),
    GuardField("deployed_agent_ids", "agent", DEPLOYED),
)
SKILL_DELETE = KNOWLEDGE_BASE_DELETE


class ReferenceService:
    """리소스 하나를 쓰는 것을 센다. 스토어만 보고 클러스터에는 닿지 않는다."""

    def __init__(
        self,
        backend_apis: BackendAPIStore,
        mcp_servers: MongoMCPServerStore,
        gateways: MongoGatewayStore,
        agents: MongoAgentStore,
        hub_items: MongoHubStore,
        knowledge: MongoKnowledgeStore,
        data_services: MongoDataServiceStore,
    ):
        self.backend_apis = backend_apis
        self.mcp_servers = mcp_servers
        self.gateways = gateways
        self.agents = agents
        self.hub_items = hub_items
        self.knowledge = knowledge
        self.data_services = data_services

    async def used_by(self, kind: TargetKind, id: str, *, tenant: str | None) -> list[Reference]:
        """`kind` 의 `id` 를 쓰는 것. `tenant` 가 있으면 그 테넌트의 것만 담는다."""
        out = _Collected()
        if kind == "backend-api":
            await self._backend_api(out, id, tenant)
        elif kind == "mcp-server":
            await self._mcp_server(out, id, tenant)
        elif kind == "gateway":
            await self._gateway(out, id, tenant)
        elif kind == "data-service":
            await self._data_service(out, id, tenant)
        elif kind == "knowledge-base":
            await self._knowledge_base(out, id, tenant)
        elif kind == "hub-item":
            await self._hub_item(out, id, tenant)
        elif kind == "cluster":
            await self._cluster(out, id, tenant)
        elif kind == "skill":
            await self._skill(out, id, tenant)
        elif kind == "agent":
            await self._agent(out, id, tenant)
        return out.all()

    # -- 가드 ---------------------------------------------------------------------------
    # 가드는 테넌트를 거르지 않는다. 다른 테넌트의 참조도 삭제를 막아야 해서다

    async def refuse_first(
        self,
        kind: TargetKind,
        id: str,
        fields: Sequence[GuardField],
        error: type[ServiceError],
    ) -> list[Reference]:
        """처음 걸린 칸 하나로 409 `in_use` 를 낸다. 문장은 그 칸의 `detail` 이다. 걸린 것이 없으면
        센 참조를 돌려준다(다른 칸을 따로 보는 가드가 다시 세지 않게)."""
        refs = await self.used_by(kind, id, tenant=None)
        hit = first_blocking(refs, fields)
        if hit is not None:
            field, ids = hit
            raise error(409, "in_use", field.detail, **{field.key: ids})
        return refs

    async def refuse_any(
        self,
        kind: TargetKind,
        id: str,
        fields: Sequence[GuardField],
        error: type[ServiceError],
        detail: str,
        *,
        every_key: bool = False,
    ) -> list[Reference]:
        """걸린 칸 전부로 409 `in_use` 를 낸다. `every_key` 면 빈 칸도 싣는다. 걸린 것이 없으면 센
        참조를 돌려준다."""
        refs = await self.used_by(kind, id, tenant=None)
        found = blocking_fields(refs, fields)
        if found:
            extra = {f.key: f.ids(refs) for f in fields} if every_key else found
            raise error(409, "in_use", detail, **extra)
        return refs

    async def gateway_ids_using_hub_item(self, hub_item_id: str) -> list[str]:
        """허브 항목을 쓰는 Gateway. 정의가 붙드는 것과 배포 스냅샷이 가리키는 것을 함께 센다.
        배포된 vMCP 는 정의를 그 사이 고쳤더라도 스냅샷이 가리키는 항목을 실제로 쓰고 있다.
        MCP Server 응답의 게시 항목 요약이 읽는다."""
        refs = await self.used_by("hub-item", hub_item_id, tenant=None)
        return HUB_ITEM_DELETE[0].ids(refs)

    # -- 종류별 ---------------------------------------------------------------------

    async def _backend_api(self, out: _Collected, api_id: str, tenant: str | None) -> None:
        # 현재 스펙이 가리키는 MCP Server (draft 포함). 핀한 리비전도 가리키면 both 다
        for doc in await self.mcp_servers.using_backend_api(api_id):
            if _skip(doc.tenant, tenant):
                continue
            pinned = doc.deployed
            deployed = pinned is not None and any(
                b.backend_profile_id == api_id for b in pinned.backends
            )
            out.add(_ref("mcp-server", doc, "both" if deployed else "current"))
        # 살아 있는 읽기 토큰. 이름이 없을 수 있어 화면은 개수로만 말한다 (링크도 없다)
        for tok in await self.backend_apis.list_tokens(api_id, active_only=True):
            out.add(
                Reference(
                    kind="token",
                    id=tok.id,
                    name=tok.label or "",
                    display_name=tok.label or "",
                    revision="current",
                )
            )

    async def _mcp_server(self, out: _Collected, server_id: str, tenant: str | None) -> None:
        item = await self.hub_items.get_by_instance(server_id)
        if item is not None:
            if not _skip(item.tenant, tenant):
                out.add(_ref("hub-item", item, "current"))
            # 허브 항목을 거쳐 이 MCP Server 를 쓰는 Gateway (간접 참조)
            for gw in await self.gateways.using_hub_item(item.id):
                if _skip(gw.tenant, tenant):
                    continue
                out.add(_ref("gateway", gw, _gateway_revision(gw, item.id), via="hub-item"))
        await self._tool_source(out, "mcp_instance", server_id, tenant)

    async def _gateway(self, out: _Collected, gateway_id: str, tenant: str | None) -> None:
        await self._tool_source(out, "gateway_instance", gateway_id, tenant)
        # Gateway 가 허브에 올린 published 항목. 배포가 있어야 생기므로 배포 쪽이다
        for item in await self.hub_items.published_of_instance(gateway_id):
            if _skip(item.tenant, tenant):
                continue
            out.add(_ref("hub-item", item, "deployed"))

    async def _hub_item(self, out: _Collected, item_id: str, tenant: str | None) -> None:
        for gw in await self.gateways.using_hub_item(item_id):
            if _skip(gw.tenant, tenant):
                continue
            out.add(_ref("gateway", gw, _gateway_revision(gw, item_id)))
        # 게시한 Agent 의 항목을 Tool 로 쓰는 Agent (P46 6.1절). 다른 종류의 항목에는 없다
        await self._tool_source(out, AGENT_SOURCE, item_id, tenant)

    async def _agent(self, out: _Collected, agent_id: str, tenant: str | None) -> None:
        """이 Agent 를 게시한 허브 항목과, 그 항목을 거쳐 이 Agent 를 Tool 로 쓰는 Agent (간접
        참조). 게시는 배포가 있어야 하므로 배포 쪽이다."""
        item = await self.hub_items.get_by_agent(agent_id)
        if item is None:
            return
        if not _skip(item.tenant, tenant):
            out.add(_ref("hub-item", item, "deployed"))
        await self._tool_source(out, AGENT_SOURCE, item.id, tenant, via="hub-item")

    async def _data_service(self, out: _Collected, ds_id: str, tenant: str | None) -> None:
        for doc in await self.mcp_servers.bound_to_data_service(ds_id):
            if not _skip(doc.tenant, tenant):
                out.add(_ref("mcp-server", doc, "current"))
        for doc in await self.mcp_servers.live_binding_data_service(ds_id):
            if not _skip(doc.tenant, tenant):
                out.add(_ref("mcp-server", doc, "deployed"))
        for agent in await self.agents.referencing_data_service(ds_id):
            if not _skip(agent.tenant, tenant):
                out.add(_ref("agent", agent, "current"))
        for agent in await self.agents.live_binding_data_service(ds_id):
            if not _skip(agent.tenant, tenant):
                out.add(_ref("agent", agent, "deployed"))
        # 벡터 테이블을 이 Database 에 둔 Knowledge Base
        for kb in await self.knowledge.list_by_data_service(ds_id):
            if _skip(kb.tenant, tenant):
                continue
            out.add(_ref("knowledge-base", kb, "current"))

    async def _knowledge_base(self, out: _Collected, kb_id: str, tenant: str | None) -> None:
        for agent in await self.agents.referencing_knowledge_base(kb_id):
            if not _skip(agent.tenant, tenant):
                out.add(_ref("agent", agent, "current"))
        # 배포가 핀한 리비전은 현재 리비전과 다를 수 있어 문서에서 다시 읽는다
        for agent in await self.agents.revisions_referencing_knowledge_base(kb_id):
            if _skip(agent.tenant, tenant):
                continue
            pinned = agent.deployed
            if pinned is not None and references_knowledge_base(pinned, kb_id):
                out.add(_ref("agent", agent, "deployed"))

    async def _skill(self, out: _Collected, skill_id: str, tenant: str | None) -> None:
        """이 Skill 을 핀한 Agent. 현재 리비전과 배포가 핀한 리비전을 문서 안에서 따로 읽는다."""
        for agent in await self.agents.referencing_skill(skill_id):
            if _skip(agent.tenant, tenant):
                continue
            pinned = agent.deployed
            pins = PinnedRevisions(
                current=skill_pins(agent.spec).get(skill_id),
                deployed=skill_pins(pinned).get(skill_id) if pinned is not None else None,
            )
            if pins.current is not None or pins.deployed is not None:
                ref = _revision_ref(pins.current is not None, pins.deployed is not None)
                out.add(_ref("agent", agent, ref, pins=pins))

    async def _cluster(self, out: _Collected, cluster_id: str, tenant: str | None) -> None:
        for doc in await self.mcp_servers.live_on_cluster(cluster_id):
            if not _skip(doc.tenant, tenant):
                out.add(_ref("mcp-server", doc, "deployed"))
        for gw in await self.gateways.live_on_cluster(cluster_id):
            if not _skip(gw.tenant, tenant):
                out.add(_ref("gateway", gw, "deployed"))
        for agent in await self.agents.live_on_cluster(cluster_id):
            if not _skip(agent.tenant, tenant):
                out.add(_ref("agent", agent, "deployed"))
        # Database 는 배포물이 아니라 접속 정보라 테넌트 목록에서 골라 낸다
        if tenant is not None:
            for ds in await self.data_services.list(tenant):
                if ds.cluster_id != cluster_id:
                    continue
                out.add(
                    Reference(
                        kind="data-service",
                        id=ds.id,
                        name=ds.name,
                        display_name=ds.display_name,
                        revision="current",
                    )
                )

    async def _tool_source(
        self,
        out: _Collected,
        source_kind: str,
        source_id: str,
        tenant: str | None,
        *,
        via: str | None = None,
    ) -> None:
        """도구 소스로 이것을 쓰는 Agent. 질의는 현재 리비전을 보므로 배포 쪽은 그 문서가 핀한
        리비전에서 다시 읽는다 (지금의 `in_use` 가드와 같은 범위다). via 는 간접 참조가 거친
        것이다."""
        for agent in await self.agents.referencing_tool_source(source_kind, source_id):
            if _skip(agent.tenant, tenant):
                continue
            pinned = agent.deployed
            deployed = pinned is not None and references_tool_source(pinned, source_kind, source_id)
            out.add(_ref("agent", agent, "both" if deployed else "current", via=via))


def _skip(doc_tenant: str, tenant: str | None) -> bool:
    return tenant is not None and doc_tenant != tenant


def _gateway_revision(gw: Gateway, hub_item_id: str) -> RevisionRef:
    """Gateway 가 허브 항목을 어느 쪽으로 붙드는가. 정의(`spec.backends`)와 배포
    스냅샷(`deployed_backends`)을 따로 본다."""
    current = any(b.hub_item_id == hub_item_id for b in gw.spec.backends)
    deployed = any(b.hub_item_id == hub_item_id for b in gw.deployed_backends)
    return _revision_ref(current, deployed)


def _revision_ref(current: bool, deployed: bool) -> RevisionRef:
    """정의와 배포 가운데 어느 쪽이 붙드는가. 둘 다 아니면 부르지 않는다."""
    if current and deployed:
        return "both"
    return "current" if current else "deployed"


class _Referable(Protocol):
    """참조 한 줄로 옮기는 문서. MCP Server·Gateway·Agent·허브 항목·Knowledge Base 가 이 모양이다.
    상태가 없는 Database·읽기 토큰은 `Reference` 를 바로 만든다."""

    id: str
    name: str
    display_name: str
    state: str


def _ref(
    kind: ReferenceKind,
    doc: _Referable,
    revision: RevisionRef,
    *,
    via: str | None = None,
    pins: PinnedRevisions | None = None,
) -> Reference:
    return Reference(
        kind=kind,
        id=doc.id,
        name=doc.name,
        display_name=doc.display_name,
        state=doc.state,
        revision=revision,
        via=via,
        pins=pins,
    )


__all__ = [
    "Reference",
    "ReferenceKind",
    "ReferenceService",
    "RevisionRef",
    "PinnedRevisions",
    "TargetKind",
]
