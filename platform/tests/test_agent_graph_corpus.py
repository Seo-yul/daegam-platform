"""캔버스와 서버가 같은 그래프를 같이 거부하는지 보는 묶음의 서버 쪽이다 (P77, D89).

묶음은 `tests/fixtures/agent-graph-corpus.json` 하나이고 캔버스 쪽은
`ui/src/test/agent-graph-corpus.test.ts` 가 같은 파일을 `validateGraph` 로 돌린다. 여기는 중립
문맥을 `GraphContext` 로 바꿔 `validate` 를 돌리고, 이슈를 문장 없이
(code, level, node_id, edge_id) 집합으로 견준다. 서버가 내는 code 마다 케이스가 하나는
있어야 한다.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest

from daegam.services import agent_graph
from daegam.services.agent_graph import GraphContext
from daegam.services.agent_skills import PinnedSkill
from daegam.store.agents import AgentSpec
from daegam.store.data_services import DataService
from daegam.store.settings import LLMProvider
from daegam.store.tenants import AgentOAuth, TenantSettings
from tests.helpers.agents import CTX_TENANT, TOKEN_URL, kb
from tests.helpers.backend_apis import FIXTURES
from tests.helpers.docs import data_service_doc, gateway_doc, mcp_server_doc
from tests.helpers.source import graph_issue_codes

CORPUS_PATH = FIXTURES / "agent-graph-corpus.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
TENANT = CTX_TENANT

IssueKey = tuple[str, str, str | None, str | None]


def build_graph(patch: dict[str, Any]) -> dict[str, Any]:
    """base 를 케이스의 graph 로 고친다. 순서와 규칙은 묶음 파일의 about 이 정한다."""
    base = copy.deepcopy(CORPUS["base"])
    nodes = base["nodes"] + copy.deepcopy(patch.get("add_nodes", []))
    edges = base["edges"] + copy.deepcopy(patch.get("add_edges", []))
    dropped = set(patch.get("drop_nodes", []))
    nodes = [n for n in nodes if n["id"] not in dropped]
    dropped_edges = set(patch.get("drop_edges", []))
    edges = [
        e
        for e in edges
        if e["source"] not in dropped
        and e["target"] not in dropped
        and e["id"] not in dropped_edges
    ]
    merge = patch.get("data", {})
    for node in nodes:
        data = {**CORPUS["defaults"][node["type"]], **node.get("data", {})}
        node["data"] = {**data, **merge.get(node["id"], {})}
    return {"nodes": nodes, "edges": edges}


def build_context(ctx: dict[str, Any]) -> GraphContext:
    """중립 문맥을 서버 문맥으로 옮긴다. 키는 묶음 파일의 base_context 와 같다."""
    providers = {
        p["name"]: LLMProvider(
            name=p["name"],
            kind=p["kind"],
            api_key_secret="llm-key" if p.get("secret_set", p["has_key"]) else None,
        )
        for p in ctx["providers"]
    }
    keyed = {p["name"] for p in ctx["providers"] if p["has_key"]}
    oauth = ctx["agent_oauth"]
    settings = TenantSettings(
        tenant=TENANT,
        agent_client_ids=list(ctx["agent_client_ids"]),
        agent_oauth=AgentOAuth(
            client_id=oauth["client_id"],
            secret_name="agent-oauth",
            token_url=TOKEN_URL,
            scope="mcp",
        )
        if oauth
        else None,
    )
    kbs = {
        k["id"]: kb(
            k["id"],
            k["name"],
            provider=k["provider"],
            indexed=k.get("indexed", 0),
            failed=k.get("failed", 0),
            deleting=k.get("deleting", False),
        )
        for k in ctx["knowledge_bases"]
    }
    return GraphContext(
        providers=providers,
        has_key=lambda p: p.name in keyed,
        token_auth=ctx["auth_mode"] != "header",
        auth_mode=ctx["auth_mode"],
        tenant_settings=settings,
        gateway_instances={
            g["id"]: gateway_doc(g["id"], tenant=TENANT, state=g["state"]) for g in ctx["gateways"]
        },
        mcp_instances={
            m["id"]: mcp_server_doc(m["id"], tenant=TENANT, state=m["state"])
            for m in ctx["mcp_servers"]
        },
        data_services={d["id"]: _database(d) for d in ctx["data_services"]},
        knowledge_bases=kbs,
        skill_revisions={
            (s["id"], s["revision"]): PinnedSkill(
                id=s["id"],
                name=s["name"],
                revision=s["revision"],
                latest=s["latest"],
                digest="corpus",
                bytes=s["bytes"],
                scripts=s["scripts"],
            )
            for s in ctx["skill_revisions"]
        },
    )


def _database(d: dict[str, Any]) -> DataService:
    """문맥의 Database. 저장 모델이 받지 않는 종류(mysql 같은)는 검증 없이 덮어 쓴다. 지금은 저장될
    수 없지만 두 검사기 모두 `invalid_data_service_kind` 로 막는 규칙을 갖고 있어서다."""
    fields = {"id": d["id"], "tenant": TENANT, "db": d.get("db")}
    doc = data_service_doc(
        d["name"],
        kind=d["kind"] if d["kind"] in ("postgres", "redis") else "postgres",
        capabilities=d.get("capabilities", {}),
        **fields,
    )
    return doc.model_copy(update={"kind": d["kind"]})


def issue_key(issue: dict[str, Any]) -> IssueKey:
    """견줄 모양. 기대값에 level 이 없으면 error 다."""
    return (
        issue["code"],
        issue.get("level", "error"),
        issue.get("node_id"),
        issue.get("edge_id"),
    )


def corpus_spec(case: dict[str, Any]) -> tuple[dict[str, Any], GraphContext]:
    """케이스의 spec(graph·service)과 서버 문맥이다. 평가 컴파일의 테스트도 쓴다
    (test_evaluation_holds.py)."""
    service = {**CORPUS["base_service"], **case.get("service", {})}
    spec = {"graph": build_graph(case.get("graph", {})), "service": service}
    return spec, build_context({**CORPUS["base_context"], **case.get("context", {})})


def server_issues(case: dict[str, Any]) -> Counter[IssueKey]:
    spec, ctx = corpus_spec(case)
    issues = agent_graph.validate(AgentSpec.model_validate(spec), ctx)
    return Counter(issue_key(i.to_dict()) for i in issues)


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["name"])
def test_server_matches_the_corpus(case: dict[str, Any]) -> None:
    expected = Counter(issue_key(i) for i in case["expect"])
    assert server_issues(case) == expected


def test_server_only_cases_say_why() -> None:
    for case in CORPUS["cases"]:
        if "server_only" in case:
            assert case["server_only"].strip(), f"{case['name']}: server_only 에 이유가 없다"


def test_every_server_code_has_a_case() -> None:
    """서버가 내는 code 마다 두 쪽이 다 도는 케이스가 있어야 한다. 새 규칙이 묶음 없이 들어오지
    못하게 한다. server_only 케이스는 캔버스가 건너뛰므로 세지 않는다."""
    covered = {
        i["code"] for case in CORPUS["cases"] if "server_only" not in case for i in case["expect"]
    }
    assert sorted(graph_issue_codes() - covered) == []
