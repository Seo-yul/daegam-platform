"""인용 번호 장부다(P36 7절, D61).

검색 도구는 결과마다 런타임이 세션 안에서 늘려 가는 번호(`cite`)를 붙여 모델에게 보인다. 번호는 세션
색인의 `cite_seq` 에서 이어지고, 한 턴 안의 같은 조각은 같은 번호다. 턴마다 1 부터 세면 지난 턴의
결과도 모델의 기록에 남아 같은 번호가 둘이 되고, 모델도 화면도 조각을 가르지 못한다. 장부는 턴
문맥(`long_term.TurnContext.citations`)에 실려 메인과 서브에이전트의 도구가 함께 쓰고, 턴이 끝나면
오류로 끝났어도 마지막 번호를 색인에 적는다(turn.py).

출처 정보는 모델이 보지 않는 도구 결과의 artifact(`{"citations": [...]}`)에 싣는다. artifact 에는
본문이 없다. 도구 가드레일의 PII 가림이 ToolMessage 본문만 고치기 때문이다. 스트림 번역기는
`status` 가 error 가 아닌 결과의 artifact 만 `citations` 이벤트로 내고(`emitted_citations`),
ToolGuard 는 결과를 막을 때 artifact 도 비운다(guards.py). 둘 중 하나가 어긋나도 막힌 결과의
제목과 doc_id 는 나가지 않는다.

원천은 Knowledge 검색(knowledge.py)과 웹 Tool(web_guard.py 의 `WebCitations`)이다. 웹 쪽은 ToolGuard
안쪽에 선 단계가 MCP 결과의 `structured_content` 를 읽어 번호를 달고 같은 artifact 에 `citations` 와
`untrusted` 를 더한다. 스트림은 그 종류를 결과의 tool_result 에 싣는다(P39b 14절 3부, P43
2절). 이 모듈은 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from runtime.turn_context import context_field

if TYPE_CHECKING:
    from runtime.sessions import SessionRecord

CITATIONS_KEY = "citations"
"""도구 결과 artifact 에서 인용 항목 목록을 담는 키다."""

UNTRUSTED_KEY = "untrusted"
"""도구 결과 artifact 에서 바깥 내용임을 표시하는 키다. 값은 종류 `web`(웹 Tool) 이나
`outside`(그 밖의 바깥 내용, outside.py)다. 옛 판이 쓴 웹 결과는 True 이고 `web` 으로 읽는다."""

KIND_KNOWLEDGE = "knowledge"
KIND_WEB = "web"
UNTRUSTED_WEB = KIND_WEB
UNTRUSTED_OUTSIDE = "outside"

CitationKey = tuple[Hashable, ...]
"""조각 하나를 가리키는 키다. Knowledge 는 (종류, kb id, doc id, 조각 번호)다."""


class CitationLedger:
    """한 턴의 인용 번호 장부다. `seq` 는 이 세션이 지금까지 나눠 준 마지막 번호다.

    도구 코루틴은 같은 이벤트 루프에서 돌고 번호를 정하는 동안 await 하지 않으므로 잠그지 않는다.
    """

    def __init__(self, seq: int = 0) -> None:
        self.seq = seq
        self._numbers: dict[CitationKey, int] = {}

    def preview(self, keys: Sequence[CitationKey]) -> list[int]:
        """keys 에 줄 번호다. 장부는 바꾸지 않는다. 이 턴에 이미 받은 조각은 그 번호이고 처음 보는
        조각은 차례로 새 번호이며, 목록 안에서 겹치는 조각도 같은 번호다."""
        fresh: dict[CitationKey, int] = {}
        numbers: list[int] = []
        for key in keys:
            n = self._numbers.get(key) or fresh.get(key)
            if n is None:
                n = fresh[key] = self.seq + len(fresh) + 1
            numbers.append(n)
        return numbers

    def assign(self, keys: Sequence[CitationKey]) -> list[int]:
        """keys 에 번호를 주고 장부에 적는다. 같은 keys 의 `preview` 와 같은 번호다. 결과를 크기에
        맞춰 줄인 뒤 남은 것만 적으면 번호가 비지 않는다."""
        numbers = self.preview(keys)
        for key, n in zip(keys, numbers, strict=True):
            self._numbers.setdefault(key, n)
        self.seq = max([self.seq, *numbers])
        return numbers


def ledger_for(record: SessionRecord) -> CitationLedger:
    """한 턴이 쓸 장부다. 세션 색인의 `cite_seq` 다음 번호부터 나눠 준다. 턴이 끝나면 색인에
    적을 칸은 `turn_record.cite_seq_fields` 가 정한다."""
    return CitationLedger(record.cite_seq)


def ledger_of(runtime: Any) -> CitationLedger:
    """도구가 쓸 장부다. 턴 문맥에 실려 온다. 문맥 없이 그래프를 부른 자리(테스트)는 이 호출만의
    장부라 번호가 1 부터다."""
    ledger = context_field(runtime, "citations")
    return ledger if isinstance(ledger, CitationLedger) else CitationLedger()


def artifact(items: list[dict[str, Any]]) -> dict[str, Any]:
    """도구 결과의 artifact 다. 모델은 이것을 보지 않는다."""
    return {CITATIONS_KEY: items}


def emitted_citations(status: str | None, value: Any) -> list[dict[str, Any]] | None:
    """`citations` 이벤트로 낼 항목이다. 오류 결과, 막힌 결과(status error), 항목이 없는 artifact 는
    None 이다."""
    if status == "error" or not isinstance(value, Mapping):
        return None
    items = value.get(CITATIONS_KEY)
    return list(items) if isinstance(items, list) and items else None


def marked_kind(value: Any) -> str | None:
    """artifact 가 단 바깥 내용의 종류다. 옛 판의 True 는 `web` 이다. 표시가 없으면 None 이다."""
    if not isinstance(value, Mapping):
        return None
    mark = value.get(UNTRUSTED_KEY)
    if mark is True or mark == UNTRUSTED_WEB:
        return UNTRUSTED_WEB
    if mark == UNTRUSTED_OUTSIDE:
        return UNTRUSTED_OUTSIDE
    return None


def untrusted_kind(status: str | None, value: Any) -> str | None:
    """`tool_result` 의 `untrusted` 에 실을 종류(`web`, `outside`)다. 오류 결과와 막힌 결과(artifact
    가 빈다)는 사람에게 보일 바깥 내용이 아니므로 None 이다. 세션의 표시는 그와 따로 선다
    (tool_safety.py)."""
    return None if status == "error" else marked_kind(value)
