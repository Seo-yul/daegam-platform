"""P36 F6: 답이 인용한 Knowledge 문서의 원문(`services/agent_citations.py`,
`GET .../sessions/{sid}/citations/{n}/source`).

인용 표본 전사(`agent-runtime/schema/fixtures/chat/citations.json`)를 싣는 가짜 런타임과 표본의
Knowledge Base·문서·본문을 갖춘 배포로 본다. 인용된 문서만 열리는 것, 가림 가드레일이 있으면 닫히는
것, 지운 문서, 승인자 규칙과 감사, 그리고 판정이 원시 노드만 읽어 깨진 가드레일 리비전에서도 상태
조회와 목록이 200 인 것을 든다. 기록에 `citations` 가 없을 때의 409 는
test_agent_chat_api.py 의 능력 표가 든다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

from daegam.auth import rbac
from daegam.services.agent_citations import masks_sources
from daegam.services.agent_runtime import APPROVER_HEADER
from daegam.store.agents import Graph, GraphNode
from daegam.store.base import utcnow
from daegam.store.knowledge import KnowledgeDocument, Source
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import agent_fixture
from tests.helpers.auth import as_principal
from tests.helpers.consts import AG
from tests.helpers.docs import knowledge_base_doc
from tests.helpers.http import detail_code
from tests.helpers.runtime import with_transcript

CITATIONS_FIXTURE: dict[str, Any] = agent_fixture("chat/citations.json")
CITED = CITATIONS_FIXTURE["events"]["citations"]["items"]
KB_ID = CITED[0]["kb"]["id"]
SOURCE_BODY = "환불은 결제일로부터 7일 안에 신청한다.\n".encode()
CITE_FEATURES = "cancel,citations"


def _pin_nodes(mongo, agent_id: str, *nodes: dict[str, Any]) -> None:
    """배포된 리비전의 그래프에 노드를 더한다. 원문 판정은 배포된 리비전만 본다."""
    doc = mongo["agents"].find_one({"_id": agent_id})
    revision = doc["deployed_revision"]
    mongo["agents"].update_one(
        {"_id": agent_id, "revisions.revision": revision},
        {"$push": {"revisions.$.graph.nodes": {"$each": list(nodes)}}},
    )


def _knowledge_node(kb_id: str = KB_ID) -> dict[str, Any]:
    return {"id": f"kb-{kb_id[:6]}", "type": "knowledge", "data": {"knowledge_base_id": kb_id}}


def _guardrail_node(position: str, kind: str) -> dict[str, Any]:
    value = {"types": ["email"]} if kind == "pii_mask" else "비밀"
    data = {"position": position, "rules": [{"kind": kind, "value": value}]}
    return {"id": f"guard-{position}-{kind}", "type": "guardrail", "data": data}


def _cited_agent(client, fake_clusters, mongo):
    """인용 표본 전사를 싣는 런타임과, 표본의 Knowledge Base·문서·본문을 갖춘 배포다. 표본의 두 번째
    출처(URL 문서)는 본문이 없다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=CITE_FEATURES)
    with_transcript(agent.runtime, CITATIONS_FIXTURE["transcript"])
    mongo["knowledge_bases"].insert_one(knowledge_base_doc(id=KB_ID).to_doc())
    first = CITED[0]
    document = KnowledgeDocument(
        id=first["doc_id"],
        knowledge_base_id=KB_ID,
        tenant="default",
        name=first["title"],
        source=Source(kind="upload"),
        content_type="text/markdown",
    )
    mongo["knowledge_documents"].insert_one(document.to_doc())
    files = client.app.state.knowledge_files
    client.portal.call(files.put, KB_ID, document.id, SOURCE_BODY, "text/markdown")
    _pin_nodes(mongo, agent.id, _knowledge_node())
    return agent.runtime, agent.id


def _source_url(agent_id: str, n: int, sid: str = "s1") -> str:
    return f"{AG}/{agent_id}/sessions/{sid}/citations/{n}/source"


def test_citation_source_opens_only_what_the_session_cited(client, fake_clusters, mongo):
    """인용되지 않은 번호, 본문이 없는 문서, 배포된 리비전에 없는 Knowledge Base 를 꾸민 전사, 다른
    테넌트의 Knowledge Base 는 모두 404 citation_not_found 다. 주인이 지운 문서(`deleting`·
    `purging`)와 지우는 중인 Knowledge Base 도 워커가 본문을 걷기 전부터 같은 404 다."""
    runtime, agent_id = _cited_agent(client, fake_clusters, mongo)
    doc_id = CITED[0]["doc_id"]
    for state in ("deleting", "purging"):
        mongo["knowledge_documents"].update_one({"_id": doc_id}, {"$set": {"state": state}})
        assert detail_code(client.get(_source_url(agent_id, 1))) == (404, "citation_not_found"), (
            state
        )
    mongo["knowledge_documents"].update_one({"_id": doc_id}, {"$set": {"state": "indexed"}})
    assert client.get(_source_url(agent_id, 1)).status_code == 200
    mongo["knowledge_bases"].update_one({"_id": KB_ID}, {"$set": {"deleting_at": utcnow()}})
    assert detail_code(client.get(_source_url(agent_id, 1))) == (404, "citation_not_found")
    mongo["knowledge_bases"].update_one({"_id": KB_ID}, {"$unset": {"deleting_at": ""}})

    assert detail_code(client.get(_source_url(agent_id, 9))) == (404, "citation_not_found")
    assert detail_code(client.get(_source_url(agent_id, 2))) == (404, "citation_not_found")
    assert client.get(_source_url(agent_id, 0)).status_code == 422

    forged = json.loads(json.dumps(CITATIONS_FIXTURE["transcript"]))
    card = next(i for i in forged if i["kind"] == "tool_call")
    card["citations"][0]["kb"]["id"] = "f" * 32
    with_transcript(runtime, forged)
    assert detail_code(client.get(_source_url(agent_id, 1))) == (404, "citation_not_found")

    elsewhere = "e" * 32
    mongo["knowledge_bases"].insert_one(knowledge_base_doc(id=elsewhere, tenant="other").to_doc())
    _pin_nodes(mongo, agent_id, _knowledge_node(elsewhere))
    card["citations"][0]["kb"]["id"] = elsewhere
    assert detail_code(client.get(_source_url(agent_id, 1))) == (404, "citation_not_found")


def test_citation_source_stays_closed_when_the_agent_masks(client, fake_clusters, mongo):
    """도구나 출력 위치의 pii_mask·deny_pattern 가드레일이 있으면 403 source_masked 다. 입력 위치의
    규칙은 사용자의 말에만 걸리므로 막지 않는다. 검증 규칙이 바뀌기 전에 배포된 리비전(지금은
    거절하는 `(a+)+` deny_pattern)도 상태 조회와 목록이 200 이다. 목록은 Agent 마다 상태를 함께
    모으므로 하나가 터지면 테넌트 목록 전체가 500 이 된다."""
    _runtime, agent_id = _cited_agent(client, fake_clusters, mongo)
    _pin_nodes(mongo, agent_id, _guardrail_node("input", "pii_mask"))
    assert client.get(_source_url(agent_id, 1)).status_code == 200
    _pin_nodes(mongo, agent_id, _guardrail_node("output", "deny_pattern"))
    assert detail_code(client.get(_source_url(agent_id, 1))) == (403, "source_masked")
    broken = {"position": "output", "rules": [{"kind": "deny_pattern", "value": "(a+)+"}]}
    _pin_nodes(mongo, agent_id, {"id": "guard-old", "type": "guardrail", "data": broken})
    r = client.get(f"{AG}/{agent_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"]["sources_masked"] is True
    r = client.get(AG)
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()] == [agent_id]


def test_citation_source_of_another_session_needs_the_approver(client, fake_clusters, mongo):
    """인용된 Knowledge 문서의 원문을 내려받기 헤더 다섯으로 낸다. 남의 세션도 승인자는 받고 그때
    런타임에 승인자 헤더가 간다. 테넌트 문서를 밖으로 내는 읽기라 감사에 남는다. 승인 권한이 없는
    사람은 남의 세션의 원문을 받지 못하고, 자기 세션은 승인자 헤더 없이 부른다."""
    runtime, agent_id = _cited_agent(client, fake_clusters, mongo)
    mongo["audit_events"].delete_many({})
    r = client.get(_source_url(agent_id, 1))
    assert r.status_code == 200, r.text
    assert r.content == SOURCE_BODY
    assert r.headers["content-type"] == "application/octet-stream"
    name = quote(CITED[0]["title"], safe="")
    assert r.headers["content-disposition"].endswith(f"filename*=UTF-8''{name}")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert client.get(_source_url(agent_id, 1, sid="other")).status_code == 200
    assert runtime.state.calls[-1]["headers"][APPROVER_HEADER.lower()] == "1"
    rows = list(mongo["audit_events"].find({"method": "GET"}))
    assert [row["action"] for row in rows] == ["download_citation_source"] * 2
    assert rows[0]["resource_id"] == agent_id

    with as_principal(client, "header", rbac.VIEWER):
        other = client.get(_source_url(agent_id, 1, sid="other"))
        assert detail_code(other) == (403, "not_session_owner")
        assert client.get(_source_url(agent_id, 1)).status_code == 200
        assert APPROVER_HEADER.lower() not in runtime.state.calls[-1]["headers"]


@pytest.mark.parametrize(
    ("data", "masks"),
    [
        ({"position": "input", "rules": [{"kind": "pii_mask"}]}, False),
        ({"position": "tool", "rules": [{"kind": "max_length", "value": 9}]}, False),
        ({"position": "output", "rules": [{"kind": "deny_pattern", "value": "(a+)+"}]}, True),
        ({"position": "tool"}, True),
        ({"position": "output", "rules": "deny_pattern"}, True),
        ({"position": "output", "rules": ["deny_pattern"]}, True),
        ({"position": "tool", "rules": [{"kind": "later_kind"}]}, True),
        ({}, True),
    ],
)
def test_masks_sources_counts_unreadable_guardrails_as_masking(data, masks):
    """원시 data 만 읽고, 가리는지 알 수 없는 노드는 가리는 쪽으로 센다. 원문이 열리는 쪽으로
    틀리지 않는다."""
    graph = Graph(nodes=[GraphNode(id="guard", type="guardrail", data=data)])
    assert masks_sources(SimpleNamespace(graph=graph)) is masks
