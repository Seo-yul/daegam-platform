"""Agent 답변이 인용한 Knowledge 문서의 원문 (P36 7절, D61).

일반 문서 API 는 메타데이터만 주고 Knowledge Base 문서를 통째 내려받는 라우트도 없다. 그래서 그
세션의 답이 인용한 문서만, 가드레일이 없을 때만 원문을 연다.

- 번호 `n` 은 런타임 세션 상세의 전사에서 찾는다. 도구 호출 카드의 `citations` 칸 가운데 번호가 같은
  `kind: knowledge` 항목이다. 같은 번호가 두 카드에 있으면 뒤의 카드가 이긴다. 화면의 번호 표
  (`ui/src/lib/citations.ts` 의 `citationTable`)와 같은 규칙이라 작은 창의 문서와 내려받는
  원문이 같다. 전사는 런타임이 쓴 것이라 그대로 믿지 않는다.
- 항목의 `kb.id` 가 배포된 리비전의 knowledge 노드가 가리키는 Knowledge Base 이고 같은 테넌트의
  것이며 지우는 중이 아니어야 한다. 문서는 그 Knowledge Base 에 있고 지운 상태(`deleting`·
  `purging`)가 아니며 본문이 있어야 한다. 주인이 지운 문서는 워커가 본문을 걷기 전이어도 열지
  않는다. 하나라도 아니면 404 citation_not_found 이고 어느 것이 틀렸는지는 가르지 않는다.
- 배포된 리비전에 도구나 출력 위치의 `pii_mask`·`deny_pattern` 가드레일이 하나라도 있으면 403
  source_masked 다. 주인이 도구 결과나 답에 건 가림이 원문 한 번으로 무의미해지지 않게 한다.
  메인에 붙지 않은 노드와 모양이 틀린 노드도 센다. 판정이 넓어도 막는 쪽으로만 틀린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from daegam.errors import ServiceError
from daegam.services.agent_graph import RULE_INJECTION_CHECK, references_knowledge_base
from daegam.services.agent_runtime import transcript_items
from daegam.store.agents import Agent, AgentSpec
from daegam.store.knowledge import REMOVED_STATES, KnowledgeFiles, MongoKnowledgeStore

CITATION_NOT_FOUND = "citation_not_found"
SOURCE_MASKED = "source_masked"
KIND_KNOWLEDGE = "knowledge"
# 원문을 열어도 가림이 무의미해지지 않는 위치와 규칙이다. 입력 위치의 규칙은 사용자의 말에만
# 걸리고 max_length 는 글을 가리지 않는다. injection_check 는 결과를 막을 뿐 가리지 않고, 막힌
# 결과는 인용 출처도 비운다(P43 7절). 그 밖의 값(pii_mask·deny_pattern, 모르는 값)은 가리는 쪽으로
# 센다
OPEN_POSITIONS = frozenset({"input"})
OPEN_RULES = frozenset({"max_length", RULE_INJECTION_CHECK})


class CitationError(ServiceError):
    """인용 원문의 오류. 전역 핸들러가 플랫폼 형식으로 낸다."""


@dataclass(frozen=True)
class CitedSource:
    """내려받을 원문이다. name 은 Knowledge Base 문서의 이름이다."""

    name: str
    body: bytes


def cited_item(session: Any, n: int) -> dict[str, Any] | None:
    """전사의 도구 호출 카드에서 번호 n 의 Knowledge 출처다. 같은 번호가 여럿이면 마지막 것이고
    없으면 None 이다."""
    found: dict[str, Any] | None = None
    for item in transcript_items(session):
        if item.get("kind") != "tool_call" or not isinstance(item.get("citations"), list):
            continue
        for cited in item["citations"]:
            if isinstance(cited, dict) and cited.get("n") == n:
                found = cited
    return found if found is not None and found.get("kind") == KIND_KNOWLEDGE else None


def masks_sources(spec: AgentSpec) -> bool:
    """이 리비전이 도구 결과나 답을 가리는가. 원문을 열면 그 가림이 무의미해진다.

    상태 조회마다 불리고 상태 조회는 예외를 던지지 않으므로 노드를 다시 검증하지 않고 원시 data
    만 읽는다. 검증 규칙이 바뀌기 전에 배포된 리비전도 그대로 지나간다."""
    return any(node.type == "guardrail" and _masks(node.data) for node in spec.graph.nodes)


def _masks(data: dict[str, Any]) -> bool:
    """가드레일 노드 하나가 가리는가. 규칙 목록이 없거나 모르는 규칙이 든 노드처럼 가리는지 알 수
    없는 노드는 가리는 쪽으로 센다. 원문이 열리는 쪽으로 틀리지 않게 하려는 것이다."""
    if data.get("position") in OPEN_POSITIONS:
        return False
    rules = data.get("rules")
    if not isinstance(rules, list):
        return True
    return any(not isinstance(r, dict) or r.get("kind") not in OPEN_RULES for r in rules)


def _kb_id(cited: dict[str, Any] | None) -> str | None:
    """출처 항목의 Knowledge Base id 다. 모양이 틀리면 None 이다."""
    kb = cited.get("kb") if cited is not None else None
    kb_id = kb.get("id") if isinstance(kb, dict) else None
    return kb_id if isinstance(kb_id, str) else None


def _not_found(n: int) -> CitationError:
    return CitationError(404, CITATION_NOT_FOUND, f"citation {n} has no source document")


class CitationSources:
    """인용 번호가 가리키는 Knowledge 문서의 원문을 찾는다."""

    def __init__(self, knowledge: MongoKnowledgeStore, files: KnowledgeFiles) -> None:
        self.knowledge = knowledge
        self.files = files

    async def source(self, doc: Agent, session: Any, n: int) -> CitedSource:
        """세션 상세(session)의 전사에서 번호 n 의 원문이다. 모듈 머리의 규칙대로 404·403 이다."""
        cited = cited_item(session, n)
        kb_id = _kb_id(cited)
        revision = doc.deployed
        if (
            cited is None
            or kb_id is None
            or revision is None
            or not references_knowledge_base(revision, kb_id)
        ):
            raise _not_found(n)
        kb = await self.knowledge.get(kb_id, tenant=doc.tenant)
        if kb is None or kb.deleting_at is not None:
            raise _not_found(n)
        if masks_sources(revision):
            raise CitationError(
                403, SOURCE_MASKED, "the agent masks tool results or answers; sources stay closed"
            )
        document = await self.knowledge.get_document(str(cited.get("doc_id")), kb_id=kb.id)
        if document is None or document.state in REMOVED_STATES:
            raise _not_found(n)
        body = await self.files.get(kb.id, document.id)
        if body is None:
            raise _not_found(n)
        return CitedSource(name=document.name, body=body)
