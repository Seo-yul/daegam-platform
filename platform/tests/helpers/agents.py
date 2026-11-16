"""Agent 그래프와 Agent API 를 쓰는 테스트가 함께 쓰는 도우미 (D26, D28, D32, D42).

그래프 쪽은 템플릿 "단일 상담 에이전트" 의 빈자리를 채운 spec(`template_spec`)과 노드 더하기,
검증·컴파일 문맥(`make_ctx`)과 그 결과 읽기다. 문맥은 테넌트
tenant-a 의 배포된 MCP Server·Gateway, Database 둘, provider 넷을 갖는다. knowledge 노드는
`with_knowledge` 가 붙이고 그 Knowledge Base 는 `ctx_with` 가 문맥에 넣는다. API 쪽은 header 모드
플랫폼에 클러스터·provider·Database·배포된 Gateway 문서를 깔고(`agent_env`), 그 문서로 빈자리를 채운
spec(`template_for` 등)으로 정의를 만들어(`create_agent`) 배포한다(`deploy_agent`, `deploy_spec`).
MCP Server 도구 소스는 `seed_tool_server` 가 깔고 `with_tool_server` 가 spec 에 단다.
키 Secret 이 따로인 provider 는 `add_provider` 가 더하고 그 openai 판이 `agent_env_with_openai` 다.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from daegam.services import agent_graph
from daegam.services.agent_graph import GraphContext
from daegam.services.agent_templates import templates
from daegam.services.tool_pins import apply_pins, pins_of
from daegam.store.agents import AgentSpec
from daegam.store.base import new_id, utcnow
from daegam.store.data_services import DataService
from daegam.store.gateways import GatewayK8s
from daegam.store.knowledge import KnowledgeBase, KnowledgeStatus
from daegam.store.mcp_servers import InstanceK8s
from daegam.store.settings import LLMProvider
from daegam.store.tenants import AgentOAuth, Binding, TenantSettings
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import (
    AG,
    DEFAULT_NS,
    ISSUER,
    LLM_KEY_SECRET,
    PLATFORM_NS,
    REPO,
    TENANT_HEADER,
)
from tests.helpers.data_services import created_database
from tests.helpers.docs import (
    agent_doc,
    data_service_doc,
    gateway_doc,
    knowledge_base_doc,
    mcp_server_doc,
)

TOKEN_URL = "http://mock-oidc.daegam-system.svc.cluster.local:8080/oauth2/token"
# 런타임과 함께 쓰는 agent.json·채팅 표본 (agent-runtime/schema/fixtures)
AGENT_FIXTURES = REPO / "agent-runtime" / "schema" / "fixtures"


def agent_fixture(name: str) -> dict[str, Any]:
    return json.loads((AGENT_FIXTURES / name).read_text(encoding="utf-8"))


# -- 그래프 --------------------------------------------------------------------------------------


def template_spec(
    *, provider: str = "local-stub", source_id: str = "gi-calc-0001", ds_id: str = "ds-pg"
) -> dict[str, Any]:
    """템플릿 "단일 상담 에이전트" 의 빈자리(provider·도구 소스·Database)를 채운 spec. provider 는
    메인이 붙인 모델 노드 `model-main` 에 있다(D28)."""
    t = templates()[0]
    graph = copy.deepcopy(t["graph"])
    node = {n["id"]: n for n in graph["nodes"]}
    node["model-main"]["data"]["provider"] = provider
    node["tools-calculator"]["data"]["source"]["id"] = source_id
    node["memory"]["data"]["checkpointer"]["data_service_id"] = ds_id
    return {"graph": graph, "service": copy.deepcopy(t["service"])}


def node_of(spec: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in spec["graph"]["nodes"] if n["id"] == node_id)


def template_for(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """도구 소스 Gateway 문서 gw 와 Database 문서 ds 로 빈자리를 채운 템플릿 spec. 메인 모델은
    local-stub 이다. `agent_env` 가 깐 env 로 배포할 spec 을 만든다."""
    return template_spec(source_id=gw["id"], ds_id=ds["id"])


def anthropic_for(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """`template_for` 의 메인 모델을 키 Secret 이 있는 local-anthropic 으로 바꾼 spec."""
    return template_spec(provider="local-anthropic", source_id=gw["id"], ds_id=ds["id"])


def anthropic_with_openai_fallback(gw: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """`anthropic_for` 의 메인 모델에 local-openai 폴백을 단 spec. 키 Secret 이 둘이라
    `agent_env_with_openai` 위에서 배포한다."""
    spec = anthropic_for(gw, ds)
    node_of(spec, "model-main")["data"]["fallback"] = {"provider": "local-openai"}
    return spec


def edge_of(spec: dict[str, Any], edge_id: str) -> dict[str, Any]:
    return next(e for e in spec["graph"]["edges"] if e["id"] == edge_id)


def model_node(node_id: str, provider: str = "local-stub", **data: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "model",
        "data": {"name": node_id, "provider": provider, **data},
    }


def add_sub_agent(
    spec: dict[str, Any],
    node_id: str,
    name: str,
    provider: str = "local-stub",
    *,
    model: str | None = None,
) -> None:
    """서브에이전트 노드를 더한다. model 을 주면 그 모델 노드를 공유하고 아니면 자기 모델 노드
    `model-<node_id>` 를 만든다. 위임 간선은 호출자가 잇는다."""
    spec["graph"]["nodes"].append(
        {
            "id": node_id,
            "type": "agent",
            "data": {
                "role": "sub",
                "name": name,
                "description": f"{name} 을 맡는다.",
                "system_prompt": "찾은 것만 말한다.",
            },
        }
    )
    if model is None:
        model = f"model-{node_id}"
        spec["graph"]["nodes"].append(model_node(model, provider))
    spec["graph"]["edges"].append(
        {"id": f"e-model-{node_id}", "source": node_id, "target": model, "kind": "model"}
    )


# 입구와 메인, 모델 노드만 있는 그래프 (minimal.json 의 spec)
MINIMAL_SPEC: dict[str, Any] = {
    "graph": {
        "nodes": [
            {
                "id": "entry",
                "type": "entry",
                "data": {
                    "session_ttl_seconds": 1800,
                    "max_turns": 20,
                    "max_concurrent_sessions": 10,
                    "max_input_chars": 2000,
                },
            },
            {
                "id": "main",
                "type": "agent",
                "data": {
                    "role": "main",
                    "name": "minimal",
                    "system_prompt": "짧고 정확하게 답한다.",
                    "max_tool_calls_per_turn": 1,
                    "turn_timeout_seconds": 60,
                    "retries": 0,
                },
            },
            {
                "id": "model",
                "type": "model",
                "data": {
                    "name": "claude",
                    "provider": "local-anthropic",
                    "max_output_tokens": 1024,
                    "reasoning": {"effort": "low"},
                },
            },
        ],
        "edges": [
            {"id": "e", "source": "entry", "target": "main", "kind": "entry"},
            {"id": "e-model", "source": "main", "target": "model", "kind": "model"},
        ],
    },
    "service": {},
}


# -- 검증·컴파일 문맥 ----------------------------------------------------------------------------

_PROVIDERS = {
    "local-stub": LLMProvider(name="local-stub", kind="stub"),
    "local-anthropic": LLMProvider(
        name="local-anthropic", kind="anthropic", api_key_secret=LLM_KEY_SECRET
    ),
    "local-openai": LLMProvider(
        name="local-openai",
        kind="openai",
        api_key_secret=LLM_KEY_SECRET,
        model="gpt-4o",
        base_url="https://gateway.example.com/v1",
    ),
    "nokey": LLMProvider(name="nokey", kind="openai"),
}


# 검증·컴파일 문맥의 테넌트. 문맥의 문서는 모두 이 테넌트·클러스터 c1 에 있다
CTX_TENANT = "tenant-a"


def _ctx_database(ds_id: str, name: str, **fields: Any) -> DataService:
    """문맥의 Database. Secret 이름은 플랫폼 규칙(`ds-<id>`)이고 접속 사용자는 비운다."""
    return data_service_doc(
        name,
        id=ds_id,
        tenant=CTX_TENANT,
        cluster_id="c1",
        secret_name=f"ds-{ds_id}",
        username=None,
        **fields,
    )


def make_ctx(
    *,
    oidc: bool = True,
    oauth: bool = True,
    client_ids: tuple[str, ...] = ("daegam-agent",),
    keyed: tuple[str, ...] = ("local-anthropic", "local-openai"),
    redis_checkpointer: str | None = None,
    mi_state: str = "deployed",
    gw_state: str = "deployed",
) -> GraphContext:
    """검증·컴파일 문맥. `redis_checkpointer` 는 redis Database `ds-redis` 의 verify 결과
    (`capabilities.checkpointer`)이고 None 이면 verify 전이다(D33). `mi_state`·`gw_state` 는
    MCP Server `mi-orders-0001`·Gateway `gi-calc-0001` 의 상태다. 도구 소스는 `deployed` 만
    받는다(D42)."""
    ts = TenantSettings(
        tenant=CTX_TENANT,
        agent_client_ids=list(client_ids),
        agent_oauth=AgentOAuth(
            client_id="daegam-agent", secret_name="agent-oauth", token_url=TOKEN_URL, scope="mcp"
        )
        if oauth
        else None,
    )
    gw = gateway_doc(
        "gi-calc-0001",
        tenant=CTX_TENANT,
        name="calc",
        state=gw_state,
        k8s=GatewayK8s(name="gw-calc", namespace=CTX_TENANT, group="gw-calc"),
    )
    mi = mcp_server_doc(
        "mi-orders-0001",
        spec={"backends": [{"backend_profile_id": "bp", "spec_version": 1}]},
        tenant=CTX_TENANT,
        name="orders",
        state=mi_state,
        k8s=InstanceK8s(
            name="mcp-orders", namespace=CTX_TENANT, group=CTX_TENANT, token_secret="t"
        ),
    )
    ds = _ctx_database("ds-pg", "pg", host="pg.svc", database="agent")
    redis = _ctx_database(
        "ds-redis",
        "cache",
        kind="redis",
        host="redis.svc",
        db=0,
        capabilities={"checkpointer": redis_checkpointer} if redis_checkpointer else {},
    )
    return GraphContext(
        providers=dict(_PROVIDERS),
        has_key=lambda p: p.name in keyed,
        token_auth=oidc,
        tenant_settings=ts,
        gateway_instances={gw.id: gw},
        mcp_instances={mi.id: mi},
        data_services={ds.id: ds, redis.id: redis},
    )


def issues_of(spec: dict[str, Any], ctx: GraphContext | None = None) -> list[dict[str, Any]]:
    return [
        i.to_dict() for i in agent_graph.validate(AgentSpec.model_validate(spec), ctx or make_ctx())
    ]


def codes(issues: list[dict[str, Any]], level: str = "error") -> set[str]:
    return {i["code"] for i in issues if i["level"] == level}


def compile_spec(
    spec: dict[str, Any],
    ctx: GraphContext | None = None,
    *,
    agent_id: str = "ai-support-0001",
    name: str = "support",
    bindings: list[Binding] | None = None,
) -> dict[str, Any]:
    """배포된 tenant-a 의 Agent 로 spec 을 agent.json 으로 컴파일한다. 정의와 실행이 한 문서라
    컴파일도 그 문서 하나만 받는다 (D42)."""
    if bindings is None:
        bindings = [Binding(data_service_id="ds-pg", env_prefix="DAEGAM_DB_0")]
    doc = agent_doc(
        agent_id, spec=spec, tenant=CTX_TENANT, name=name, state="deployed", bindings=bindings
    )
    return agent_graph.compile(doc, doc.current, ctx or make_ctx())


# -- knowledge 노드 ------------------------------------------------------------------------------

# knowledge-agent.json 의 base.id. 테이블 이름이 32 hex 이므로 그대로 쓴다
FAQ_ID = "3f2a9c1e5b7d4e8fa6c0b1d2e3f4a5b6"


def kb(
    kb_id: str,
    name: str = "faq",
    *,
    provider: str = "local-stub",
    ds_id: str = "ds-pg",
    dimensions: int = 64,
    indexed: int = 1,
    failed: int = 0,
    deleting: bool = False,
    status: bool = True,
    display_name: str | None = None,
) -> KnowledgeBase:
    """문맥에 넣을 tenant-a 의 Knowledge Base. 색인한 문서 수와 실패 수를 status 로 갖는다."""
    base = knowledge_base_doc(
        name,
        id=kb_id,
        tenant=CTX_TENANT,
        display_name=display_name or name,
        data_service_id=ds_id,
        provider=provider,
        model="stub" if provider == "local-stub" else "text-embedding-3-small",
        dimensions=dimensions,
        deleting_at=utcnow() if deleting else None,
    )
    if status:
        base.status = KnowledgeStatus(documents=indexed + failed, indexed=indexed, failed=failed)
    return base


def ctx_with(*kbs: KnowledgeBase, **kw: Any) -> GraphContext:
    """make_ctx + Knowledge Base 목록 + 두 번째 postgres Database `ds-pg2`."""
    ctx = make_ctx(**kw)
    ctx.knowledge_bases = {k.id: k for k in kbs}
    ctx.data_services["ds-pg2"] = _ctx_database("ds-pg2", "pg2", host="pg2.svc", database="kb")
    return ctx


def without_memory(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """spec(없으면 템플릿)의 사본에서 memory 노드와 그 간선을 뺀 것이다."""
    out = copy.deepcopy(spec if spec is not None else template_spec())
    out["graph"]["nodes"] = [n for n in out["graph"]["nodes"] if n["type"] != "memory"]
    out["graph"]["edges"] = [e for e in out["graph"]["edges"] if e["kind"] != "memory"]
    return out


def pinned_document(spec: dict[str, Any], ctx: GraphContext) -> tuple[AgentSpec, dict[str, Any]]:
    """배포처럼 Tool 핀을 박고 컴파일해 핀을 실은 문서다. 핀은 문맥에도 둔다. (리비전, 문서)."""
    rev = AgentSpec.model_validate(spec)
    ctx.tool_pins = pins_of(rev, ctx.source_tools)
    return rev, apply_pins(compile_spec(spec, ctx), rev, ctx.tool_pins, ctx.source_tools)


def knowledge_node(node_id: str = "kb-faq", kb_id: str = FAQ_ID, **data: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "knowledge", "data": {"knowledge_base_id": kb_id, **data}}


def attach(spec: dict[str, Any], agent_id: str, node_id: str, kind: str = "knowledge") -> None:
    spec["graph"]["edges"].append(
        {"id": f"e-{agent_id}-{node_id}", "source": agent_id, "target": node_id, "kind": kind}
    )


def with_knowledge(
    spec: dict[str, Any] | None = None,
    *,
    node_id: str = "kb-faq",
    kb_id: str = FAQ_ID,
    agent: str = "agent-main",
    **data: Any,
) -> dict[str, Any]:
    """템플릿(또는 spec)에 knowledge 노드 하나를 더해 에이전트에 붙인다."""
    spec = spec or template_spec()
    spec["graph"]["nodes"].append(knowledge_node(node_id, kb_id, **data))
    attach(spec, agent, node_id)
    return spec


# -- API (header 모드) ---------------------------------------------------------------------------


# agent_env 가 까는 provider 셋. nokey 는 키 Secret 이 없는 openai provider 다
BASE_PROVIDERS: list[dict[str, Any]] = [
    {"name": "local-stub", "kind": "stub"},
    {"name": "local-anthropic", "kind": "anthropic", "api_key_secret": LLM_KEY_SECRET},
    {"name": "nokey", "kind": "openai"},
]


def agent_env(client, mongo, *, tenant: str = "default", namespace: str = DEFAULT_NS):
    """클러스터(플랫폼 기본) + provider 3개(`BASE_PROVIDERS`) + postgres Database + 배포된 Gateway
    문서."""
    cid = default_cluster(client, platform_internal_url=ISSUER)
    r = client.patch("/settings", json={"llm_providers": BASE_PROVIDERS})
    assert r.status_code == 200, r.text
    ds = created_database(client, database="agent", headers={TENANT_HEADER: tenant})
    gw = seed_tool_gateway(mongo, tenant=tenant, cluster_id=cid, namespace=namespace)
    return cid, ds, gw


def add_provider(client, fake_secrets, provider: dict[str, Any], api_key: str) -> None:
    """`BASE_PROVIDERS` 에 provider 하나를 더한다. 키 api_key 는 provider 의 api_key_secret 이
    가리키는 따로 된 Secret 에 넣는다."""
    fake_secrets.put(provider["api_key_secret"], {"api_key": api_key}, namespace=PLATFORM_NS)
    r = client.patch("/settings", json={"llm_providers": [*BASE_PROVIDERS, provider]})
    assert r.status_code == 200, r.text


OPENAI_SECRET = "llm-keys-openai"
# base_url 이 있고 키 Secret 이 따로인 openai provider
OPENAI_PROVIDER = {
    "name": "local-openai",
    "kind": "openai",
    "api_key_secret": OPENAI_SECRET,
    "base_url": "https://gw.example.com/v1",
}


def agent_env_with_openai(client, mongo, fake_secrets):
    """agent_env + `OPENAI_PROVIDER`."""
    env = agent_env(client, mongo)
    add_provider(client, fake_secrets, OPENAI_PROVIDER, "sk-openai")
    return env


def seed_tool_gateway(
    mongo, *, tenant: str, cluster_id: str, namespace: str, name: str = "calc"
) -> dict:
    """Agent 의 도구 소스가 될 배포된 Gateway 문서를 Mongo 에 넣고 그 덤프를 돌려준다."""
    inst = gateway_doc(
        new_id(),
        tenant=tenant,
        name=name,
        state="deployed",
        cluster=cluster_id,
        k8s=GatewayK8s(name=f"gw-{name}", namespace=namespace, group=f"gw-{name}"),
    )
    mongo["gateways"].insert_one(inst.to_doc())
    return inst.model_dump()


def seed_tool_server(mongo, *, cluster_id: str, name: str, tenant: str = "default") -> str:
    """Agent 의 도구 소스가 될 배포된 MCP Server 문서를 Mongo 에 넣고 그 id 를 돌려준다.
    네임스페이스는 그 테넌트의 것이다."""
    inst = mcp_server_doc(
        new_id(),
        tenant=tenant,
        name=name,
        state="deployed",
        cluster=cluster_id,
        k8s=InstanceK8s(
            name=f"mcp-{name}",
            namespace=f"daegam-{tenant}",
            group=tenant,
            token_secret=f"mcp-{name}-token",
        ),
    )
    mongo["mcp_servers"].insert_one(inst.to_doc())
    return inst.id


def with_tool_server(spec: dict[str, Any], server_id: str) -> dict[str, Any]:
    """템플릿 spec 의 도구 소스를 MCP Server server_id 로 바꾼다."""
    source = {"kind": "mcp_instance", "id": server_id}
    node_of(spec, "tools-calculator")["data"]["source"] = source
    return spec


def agent_body(spec: dict[str, Any], name: str = "support", **extra) -> dict[str, Any]:
    return {"name": name, **spec, **extra}


def create_agent(client, spec: dict[str, Any], name: str = "support", **extra):
    """정의만 만든다 (draft). 응답 그대로."""
    return client.post(AG, json=agent_body(spec, name, **extra))


def deploy_agent(client, agent_id: str, **extra):
    """배포는 정의를 다시 받지 않는다. 클러스터 쪽 설정만 보낸다 (D42)."""
    return client.post(f"{AG}/{agent_id}/deploy", json=extra)


def deploy_spec(client, spec, name="support", **extra):
    """만들고 바로 배포한다. 합치기 전의 (프로필, 인스턴스) 자리에 (저장 직후, 배포 직후)가 온다.
    둘은 같은 문서라 id 도 같다."""
    p = create_agent(client, spec, name).json()
    assert "id" in p, p
    r = deploy_agent(client, p["id"], **extra)
    assert r.status_code == 201, r.text
    return p, r.json()
