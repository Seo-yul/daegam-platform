"""리소스 참조 (D43 셋째). `ReferenceService` 가 종류마다 무엇을 세는지, 가드 표의 칸이 옛
스토어 질의와 같은 답을 내는지, 가드 칸 고르기(`GuardField`·`blocking_fields`·`first_blocking`)를
본다.

문서는 스토어 모델로 만들어 동기 pymongo 로 넣고, 서비스만 비동기로 부른다. 클러스터에는 닿지
않으므로 fake 가 필요 없다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.services.references import (
    CURRENT,
    DEPLOYED,
    KNOWLEDGE_BASE_DELETE,
    GuardField,
    Reference,
    ReferenceService,
    blocking_fields,
    first_blocking,
)
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.backend_apis import BackendAPI, BackendAPIToken, MongoBackendAPIStore
from daegam.store.data_services import DataService, MongoDataServiceStore
from daegam.store.gateways import Gateway, GatewayBackendSnapshot, MongoGatewayStore
from daegam.store.hub import HubItem, HubK8s, HubSource, MongoHubStore
from daegam.store.knowledge import MongoKnowledgeStore
from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore
from daegam.store.tenants import Binding
from tests.helpers.aio import run_db
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.consts import BP, OTHER, T2, TENANT_HEADER
from tests.helpers.docs import (
    TENANT,
    agent_doc,
    agent_refs_spec,
    data_service_doc,
    gateway_doc,
    insert_docs,
    knowledge_base_doc,
    mcp_server_doc,
)

# -- 문서 만들기 -------------------------------------------------------------------------


def _backend_api(api_id: str, *, tenant: str = TENANT) -> BackendAPI:
    return BackendAPI(id=api_id, tenant=tenant, name=api_id, display_name=api_id)


def _mcp_spec(api_id: str, *, ds_id: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"backends": [{"backend_profile_id": api_id, "spec_version": 1}]}
    if ds_id is not None:
        spec["bindings"] = [{"data_service_id": ds_id, "env_prefix": "DAEGAM_DB_0"}]
    return spec


def _mcp_server(
    sid: str,
    *,
    api_id: str = "api-1",
    deployed_api_id: str | None = None,
    ds_id: str | None = None,
    **kw: Any,
) -> MCPServer:
    """현재 리비전은 `api_id`, 핀한 리비전은 `deployed_api_id`(없으면 핀 없음)를 쓴다."""
    pinned = None if deployed_api_id is None else _mcp_spec(deployed_api_id, ds_id=ds_id)
    doc = mcp_server_doc(sid, spec=_mcp_spec(api_id, ds_id=ds_id), pinned=pinned, **kw)
    if doc.is_deployed and ds_id is not None:
        doc.effective_config = {"bindings": [{"data_service_id": ds_id}]}
    return doc


def _gateway(
    gid: str, *, spec_item: str | None = None, deployed_item: str | None = None, **kw: Any
) -> Gateway:
    """정의는 `spec_item`, 배포 스냅샷은 `deployed_item` 허브 항목을 가리킨다."""
    spec = {"backends": [{"hub_item_id": spec_item}] if spec_item else []}
    doc = gateway_doc(gid, spec=spec, **kw)
    if deployed_item is not None:
        doc.deployed_backends = [
            GatewayBackendSnapshot(
                hub_item_id=deployed_item,
                hub_item_name=deployed_item,
                kind="internal",
                workload=f"{gid}-{deployed_item}",
                url="http://x/mcp",
            )
        ]
    return doc


def _agent(
    aid: str,
    *,
    current: dict[str, Any] | None = None,
    binding_ds: str | None = None,
    **kw: Any,
) -> Agent:
    """현재 리비전은 `current`, 배포가 핀한 리비전은 `pinned`(없으면 current 와 같다)다."""
    doc = agent_doc(aid, spec=current or agent_refs_spec(), **kw)
    if doc.is_deployed and binding_ds is not None:
        doc.bindings = [Binding(data_service_id=binding_ds, env_prefix="DAEGAM_DB_0")]
    return doc


def _hub_item(
    item_id: str,
    *,
    kind: str = "internal",
    instance_id: str | None = None,
    gateway_instance_id: str | None = None,
    agent_id: str | None = None,
    tenant: str = TENANT,
) -> HubItem:
    return HubItem(
        id=item_id,
        tenant=tenant,
        name=item_id,
        display_name=item_id,
        kind=kind,
        source=HubSource(
            instance_id=instance_id, gateway_instance_id=gateway_instance_id, agent_id=agent_id
        ),
        cluster_id="c1",
        k8s=HubK8s(kind="MCPServer", name=item_id, namespace="daegam-acme"),
    )


def _data_service(ds_id: str, *, cluster: str = "c1") -> DataService:
    return data_service_doc(ds_id, id=ds_id, tenant=TENANT, cluster_id=cluster)


# -- 넣기와 부르기 ------------------------------------------------------------------------


def _call(settings, fn):
    """ReferenceService 를 만들어 `fn(service)` 를 한 번 돌린다."""

    async def go(db):
        service = ReferenceService(
            MongoBackendAPIStore(settings.mongo_url, settings.mongo_db),
            MongoMCPServerStore(db),
            MongoGatewayStore(db),
            MongoAgentStore(db),
            MongoHubStore(db),
            MongoKnowledgeStore(db),
            MongoDataServiceStore(db),
        )
        try:
            return await fn(service)
        finally:
            await service.backend_apis.close()

    return run_db(settings, go)


def _used_by(settings, kind: str, rid: str, *, tenant: str | None = TENANT) -> list[dict[str, Any]]:
    refs = _call(settings, lambda s: s.used_by(kind, rid, tenant=tenant))
    return [r.model_dump() for r in refs]


def _pairs(refs: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(r["kind"], r["id"], r["revision"]) for r in refs]


# -- 종류별 -------------------------------------------------------------------------------


def test_backend_api_counts_mcp_servers_and_active_tokens(settings, mongo):
    """현재 스펙이 가리키면 current, 핀한 리비전도 가리키면 both 다. 회수한 토큰은 세지 않는다."""
    insert_docs(
        mongo,
        backend_apis=[_backend_api("api-1")],
        mcp_servers=[
            _mcp_server("ms-draft", api_id="api-1"),
            _mcp_server("ms-live", api_id="api-1", deployed_api_id="api-1", state="deployed"),
            _mcp_server("ms-moved", api_id="api-1", deployed_api_id="api-2", state="deployed"),
            _mcp_server("ms-other", api_id="api-9"),
        ],
    )
    mongo["backend_api_tokens"].insert_many(
        [
            BackendAPIToken(
                id="t-live", backend_profile_id="api-1", token_sha256="x", label="ci"
            ).to_doc(),
            BackendAPIToken(
                id="t-gone", backend_profile_id="api-1", token_sha256="y", revoked_at=None
            ).to_doc(),
        ]
    )
    mongo["backend_api_tokens"].update_one({"_id": "t-gone"}, {"$set": {"revoked_at": "now"}})

    refs = _used_by(settings, "backend-api", "api-1")
    assert _pairs(refs) == [
        ("mcp-server", "ms-draft", "current"),
        ("mcp-server", "ms-live", "both"),
        ("mcp-server", "ms-moved", "current"),
        ("token", "t-live", "current"),
    ]
    assert refs[0]["display_name"] == "ms-draft" and refs[0]["state"] == "draft"
    assert refs[3]["name"] == "ci" and refs[3]["state"] is None


def test_gateway_counts_agents_and_published_item(settings, mongo):
    """게시 항목은 배포가 있어야 생기므로 배포 쪽이다."""
    insert_docs(
        mongo,
        gateways=[_gateway("gw-1", state="deployed")],
        hub_items=[_hub_item("hi-pub", kind="published", gateway_instance_id="gw-1")],
        agents=[
            _agent(
                "ag-1",
                current=agent_refs_spec(source=("gateway_instance", "gw-1")),
                pinned=agent_refs_spec(source=("gateway_instance", "gw-1")),
                state="deployed",
            )
        ],
    )
    assert _pairs(_used_by(settings, "gateway", "gw-1")) == [
        ("agent", "ag-1", "both"),
        ("hub-item", "hi-pub", "deployed"),
    ]


def test_data_service_counts_mcp_agents_and_knowledge_bases(settings, mongo):
    insert_docs(
        mongo,
        mcp_servers=[_mcp_server("ms-1", ds_id="ds-1", state="deployed")],
        agents=[
            _agent(
                "ag-1", current=agent_refs_spec(ds_id="ds-1"), state="deployed", binding_ds="ds-1"
            )
        ],
        knowledge_bases=[
            knowledge_base_doc("kb-1", id="kb-1", tenant=TENANT, data_service_id="ds-1")
        ],
        data_services=[_data_service("ds-1")],
    )
    assert sorted(_pairs(_used_by(settings, "data-service", "ds-1"))) == [
        ("agent", "ag-1", "both"),
        ("knowledge-base", "kb-1", "current"),
        ("mcp-server", "ms-1", "both"),
    ]


def test_skill_rows_carry_current_and_deployed_pins(settings, mongo):
    """정의와 배포가 핀한 리비전을 따로 읽어 `pins` 에 싣는다. 옛 리비전에만 남은 핀은 세지
    않는다."""
    old_only = _agent(
        "ag-old", current=agent_refs_spec(), pinned=agent_refs_spec(skill=("sk-1", 1))
    )
    old_only.deployed_revision = None
    insert_docs(
        mongo,
        agents=[
            _agent(
                "ag-both",
                current=agent_refs_spec(skill=("sk-1", 3)),
                pinned=agent_refs_spec(skill=("sk-1", 2)),
                state="deployed",
            ),
            _agent("ag-current", current=agent_refs_spec(skill=("sk-1", 3))),
            _agent(
                "ag-pinned",
                current=agent_refs_spec(),
                pinned=agent_refs_spec(skill=("sk-1", 1)),
                state="deployed",
            ),
            old_only,
            _agent("ag-x", current=agent_refs_spec(skill=("sk-1", 3)), tenant=OTHER),
            _agent("ag-other-skill", current=agent_refs_spec(skill=("sk-2", 1))),
        ],
    )
    rows = sorted(_used_by(settings, "skill", "sk-1"), key=lambda r: r["id"])
    assert [(r["id"], r["revision"], r["pins"]) for r in rows] == [
        ("ag-both", "both", {"current": 3, "deployed": 2}),
        ("ag-current", "current", {"current": 3, "deployed": None}),
        ("ag-pinned", "deployed", {"current": None, "deployed": 1}),
    ]
    # 가드는 테넌트를 거르지 않는다
    assert {r["id"] for r in _used_by(settings, "skill", "sk-1", tenant=None)} == {
        "ag-both",
        "ag-current",
        "ag-pinned",
        "ag-x",
    }


def test_cluster_answers_only_the_request_tenant(settings, mongo):
    """클러스터는 플랫폼 리소스지만 references 는 요청 테넌트의 것만 답한다 (D43)."""
    insert_docs(
        mongo,
        mcp_servers=[
            _mcp_server("ms-1", state="deployed"),
            _mcp_server("ms-draft"),
            _mcp_server("ms-x", tenant=OTHER, state="deployed"),
            _mcp_server("ms-c2", state="deployed", cluster="c2"),
        ],
        gateways=[_gateway("gw-1", state="deployed")],
        agents=[_agent("ag-1", state="deployed")],
        data_services=[_data_service("ds-1"), _data_service("ds-c2", cluster="c2")],
    )
    assert _pairs(_used_by(settings, "cluster", "c1")) == [
        ("mcp-server", "ms-1", "deployed"),
        ("gateway", "gw-1", "deployed"),
        ("agent", "ag-1", "deployed"),
        ("data-service", "ds-1", "current"),
    ]
    assert _pairs(_used_by(settings, "cluster", "c1", tenant=OTHER)) == [
        ("mcp-server", "ms-x", "deployed")
    ]


def test_an_unpublished_agent_has_no_users(settings, mongo):
    insert_docs(mongo, agents=[_agent("ag-1")])
    assert _used_by(settings, "agent", "ag-1") == []


def test_a_published_agent_counts_its_item_and_the_agents_that_call_it(settings, mongo):
    """게시 항목은 배포 쪽이고, 그 항목을 Tool 소스로 쓰는 Agent 는 항목을 거친 간접 참조다. 항목의
    참조는 같은 Agent 를 직접 참조로 센다. 다른 종류의 소스가 같은 id 를 가리키는 것은 세지
    않는다."""
    calls = agent_refs_spec(source=("agent", "hi-billing"))
    insert_docs(
        mongo,
        hub_items=[_hub_item("hi-billing", kind="agent", agent_id="ag-billing")],
        agents=[
            _agent("ag-billing", state="deployed"),
            _agent("ag-both", current=calls, pinned=calls, state="deployed"),
            _agent("ag-draft", current=calls),
            _agent("ag-x", current=calls, tenant=OTHER),
            _agent("ag-gw", current=agent_refs_spec(source=("gateway_instance", "hi-billing"))),
        ],
    )
    used = _used_by(settings, "agent", "ag-billing")
    assert _pairs(used) == [
        ("hub-item", "hi-billing", "deployed"),
        ("agent", "ag-both", "both"),
        ("agent", "ag-draft", "current"),
    ]
    assert [r["via"] for r in used] == [None, "hub-item", "hub-item"]
    item_users = _used_by(settings, "hub-item", "hi-billing")
    assert _pairs(item_users) == [("agent", "ag-both", "both"), ("agent", "ag-draft", "current")]
    assert [r["via"] for r in item_users] == [None, None]
    assert _pairs(_used_by(settings, "hub-item", "hi-billing", tenant=OTHER)) == [
        ("agent", "ag-x", "current")
    ]
    assert _used_by(settings, "agent", "ag-both") == []


def test_tenant_filters_the_answer_but_not_the_guard(settings, mongo):
    """다른 테넌트의 Gateway 는 응답에서 빠지지만 삭제 가드는 그것도 막는다."""
    insert_docs(mongo, gateways=[_gateway("gw-mine"), _gateway("gw-theirs", tenant=OTHER)])
    mongo["gateways"].update_many({}, {"$set": {"spec.backends": [{"hub_item_id": "hi-1"}]}})

    assert _pairs(_used_by(settings, "hub-item", "hi-1")) == [("gateway", "gw-mine", "current")]
    assert _call(settings, lambda s: s.gateway_ids_using_hub_item("hi-1")) == [
        "gw-mine",
        "gw-theirs",
    ]


# -- 가드 목록 ----------------------------------------------------------------------------


def test_the_hub_item_guard_lists_the_gateways_using_it(settings, mongo):
    """`gateway_ids_using_hub_item` 은 정의나 배포 스냅샷이 항목을 쓰는 Gateway 를 낸다."""
    insert_docs(
        mongo,
        gateways=[
            _gateway("gw-def", spec_item="hi-1"),
            _gateway("gw-snap", deployed_item="hi-1", state="deployed"),
            _gateway("gw-none", spec_item="hi-9"),
        ],
    )

    async def ids(service: ReferenceService) -> list[str]:
        return await service.gateway_ids_using_hub_item("hi-1")

    assert _call(settings, ids) == ["gw-def", "gw-snap"]


def test_the_knowledge_base_guards_list_the_agents_using_it(settings, mongo):
    """정의 쪽 가드는 현재 정의가 쓰는 Agent 를, 배포 쪽 가드는 핀한 리비전이 쓰는 Agent 를 낸다."""
    insert_docs(
        mongo,
        agents=[
            _agent("ag-current", current=agent_refs_spec(kb_id="kb-1")),
            _agent(
                "ag-pinned",
                current=agent_refs_spec(),
                pinned=agent_refs_spec(kb_id="kb-1"),
                state="deployed",
            ),
            _agent(
                "ag-gone", current=agent_refs_spec(), pinned=agent_refs_spec(), state="deployed"
            ),
        ],
    )

    async def guards(service: ReferenceService) -> tuple[list[str], list[str]]:
        refs = await service.used_by("knowledge-base", "kb-1", tenant=None)
        defined, deployed = (field.ids(refs) for field in KNOWLEDGE_BASE_DELETE)
        return defined, deployed

    assert _call(settings, guards) == (["ag-current"], ["ag-pinned"])


# -- 라우트 -------------------------------------------------------------------------------


def test_references_route_shape_and_404(client):
    """`GET /{id}/references` 는 읽기 scope 와 404 를 자기 리소스의 GET 과 똑같이 본다."""
    pid = uploaded_backend(client)["id"]

    assert client.get(f"{BP}/{pid}/references").json() == {"used_by": []}
    tok = client.post(f"{BP}/{pid}/tokens", json={"label": "ci"}).json()["token_id"]
    body = client.get(f"{BP}/{pid}/references").json()
    assert body["used_by"] == [
        {
            "kind": "token",
            "id": tok,
            "name": "ci",
            "display_name": "ci",
            "state": None,
            "revision": "current",
            "via": None,
            "pins": None,
        }
    ]
    assert client.get(f"{BP}/nope/references").status_code == 404
    # 다른 테넌트에는 없는 리소스다
    assert client.get(f"{BP}/{pid}/references", headers=T2).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/mcp-servers/nope/references",
        "/gateways/nope/references",
        "/agents/nope/references",
        "/data-services/nope/references",
        "/knowledge-bases/nope/references",
        "/hub/nope/references",
        "/skills/nope/references",
    ],
)
def test_every_router_has_the_route(client, path):
    assert client.get(path).status_code == 404, path


def test_cluster_references_route(client, mongo):
    """클러스터 라우트는 테넌트 헤더를 받아 그 테넌트의 배포만 답한다."""
    insert_docs(
        mongo,
        mcp_servers=[
            _mcp_server("ms-1", state="deployed"),
            _mcp_server("ms-x", tenant=OTHER, state="deployed"),
        ],
    )
    mongo["clusters"].insert_one(
        {
            "_id": "c1",
            "name": "c1",
            "display_name": "c1",
            "connection": {"mode": "kubeconfig", "secret_name": "cl-c1"},
            "platform_internal_url": "http://daegam.daegam-system.svc:8080",
        }
    )
    mine = {TENANT_HEADER: TENANT}
    body = client.get("/clusters/c1/references", headers=mine).json()
    assert [(r["kind"], r["id"]) for r in body["used_by"]] == [("mcp-server", "ms-1")]
    other = client.get("/clusters/c1/references", headers=T2).json()
    assert [r["id"] for r in other["used_by"]] == ["ms-x"]
    assert client.get("/clusters/nope/references", headers=mine).status_code == 404


# -- 가드 칸 고르기 -------------------------------------------------------------------------


def _r(kind: Any, ref_id: str, revision: Any) -> Reference:
    return Reference(kind=kind, id=ref_id, name=ref_id, display_name=ref_id, revision=revision)


GUARD_REFS = [
    _r("agent", "ag-cur", "current"),
    _r("agent", "ag-both", "both"),
    _r("agent", "ag-dep", "deployed"),
    _r("gateway", "gw-1", "deployed"),
]


@pytest.mark.parametrize(
    ("field", "ids"),
    [
        (GuardField("agent_ids", "agent", CURRENT), ["ag-cur", "ag-both"]),
        (GuardField("deployed_agent_ids", "agent", DEPLOYED), ["ag-both", "ag-dep"]),
        (GuardField("agent_ids", "agent"), ["ag-cur", "ag-both", "ag-dep"]),
        (GuardField("gateway_ids", "gateway", CURRENT), []),
        (GuardField("mcp_server_ids", "mcp-server"), []),
    ],
)
def test_guard_field_ids_keep_kind_revision_and_order(field: GuardField, ids: list[str]) -> None:
    assert field.ids(GUARD_REFS) == ids


def test_blocking_fields_and_first_blocking_follow_the_table_order() -> None:
    table = (
        GuardField("mcp_server_ids", "mcp-server", detail="servers"),
        GuardField("deployed_agent_ids", "agent", DEPLOYED, "deployed agents"),
        GuardField("gateway_ids", "gateway", detail="gateways"),
    )
    assert blocking_fields(GUARD_REFS, table) == {
        "deployed_agent_ids": ["ag-both", "ag-dep"],
        "gateway_ids": ["gw-1"],
    }
    hit = first_blocking(GUARD_REFS, table)
    assert hit is not None and hit[0].detail == "deployed agents"
    assert hit[1] == ["ag-both", "ag-dep"]
    assert blocking_fields([], table) == {} and first_blocking([], table) is None
