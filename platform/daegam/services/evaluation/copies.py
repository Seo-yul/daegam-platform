"""대화에서 복사한 평가 사례와 그 동의 (P45 8절).

- **복사** (`draft`). 평가를 받은 턴 하나의 사용자 질문과 그 답을 런타임 전사에서 읽어 저장하기 전의
  사례로 돌려준다. 앞선 질문은 복사하지 않는다. 답은 참조 답이고 평가가 좋아요면 좋은 답의 예다.
  개인정보로 보이는 것은 자리표로 바꾼다(`pii.replace`). 평가가 없거나 동의하지 않았으면 409
  `evaluation_consent_missing`, 평가가 다시 생성으로 걷혔거나 전사에 그 턴이 없으면 404
  `turn_not_found` 다. 자리표로 바꾼 질문이나 답이 사례의 칸보다 길면 422 `invalid_value` 다. 사람은
  담지 않고 출처(평가 id, 세션 id, 턴 id)만 담는다.
- **동의를 따라 움직이기.** 평가를 고치면 그 평가에서 복사한 사례의 TTL 을 평가의 기한으로 옮기고
  좋아요와 아쉬워요가 바뀌었으면 참조 답의 good 과 지문을 고치며,
  동의를 끄거나 평가를 거두면 복사본을 지운다(`rating_changed`, `ratings_gone`). 계정을 지우거나
  테넌트 소속을 잃은 사람의 동의를 거두고 복사본을 지운다(`withdraw_person`). 비밀번호 바꾸기와 계정
  막기는 동의를 거두지 않는다. 놓친 것은 평가 정리(`sweep.py`)가 잡는다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from daegam.services.agent_runtime import done_index, transcript_items
from daegam.services.evaluation import pii
from daegam.services.evaluation.cases import case_digest
from daegam.services.evaluation.errors import EvaluationError
from daegam.services.evaluation.inputs import MAX_MESSAGE_CHARS, MAX_REFERENCE_CHARS
from daegam.store.agent_feedback import AgentFeedback, MongoAgentFeedbackStore
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.evaluation import CaseContent, CaseOrigin, CaseReference, MongoEvaluationStore
from daegam.store.personal_tokens import DELETED, MEMBERSHIP

EVALUATION_CONSENT_MISSING = "evaluation_consent_missing"
TURN_NOT_FOUND = "turn_not_found"
# 동의를 거두는 계정 사건 (8절). 계정 삭제는 모든 테넌트, 소속을 잃으면 그 테넌트다. 비밀번호
# 바꾸기와 계정 막기는 거두지 않는다
CONSENT_ENDING: frozenset[str] = frozenset({DELETED, MEMBERSHIP})


def consent_missing() -> EvaluationError:
    return EvaluationError(
        409,
        EVALUATION_CONSENT_MISSING,
        "the person who chatted has not agreed to use this turn as an evaluation case",
    )


def _too_long(field: str, text: str, limit: int) -> None:
    """복사한 글이 사례의 칸보다 길면 422 `invalid_value` 다. 자리표가 글을 늘릴 수 있어 바꾼 뒤에도
    본다."""
    if len(text) > limit:
        raise EvaluationError(
            422,
            "invalid_value",
            f"the rated turn's {field} is longer than a case holds ({limit} characters)",
            field=field,
            max=limit,
        )


def rated_turn(items: list[dict[str, Any]], turn_id: str) -> tuple[str, str] | None:
    """전사에서 그 턴의 (사용자 질문, 답). 답은 메인 에이전트의 말(하위 에이전트의 이름표가 없는
    것)을 잇고, 없으면 그 턴의 모든 말을 잇는다. 인사말은 빼고, 턴이 없거나 질문이 없으면 None
    이다."""
    end = done_index(items, turn_id)
    if end is None:
        return None
    start = end
    while start > 0 and items[start - 1].get("kind") != "user":
        start -= 1
    if start == 0:
        return None
    question = items[start - 1].get("text")
    said = [
        it
        for it in items[start:end]
        if it.get("kind") == "assistant"
        and not it.get("greeting")
        and isinstance(it.get("text"), str)
    ]
    main = [it["text"] for it in said if not it.get("agent")] or [it["text"] for it in said]
    answer = "\n\n".join(t for t in main if t.strip())
    if not isinstance(question, str) or not question.strip():
        return None
    return question, answer


class EvaluationCopies:
    def __init__(self, store: MongoEvaluationStore, ratings: MongoAgentFeedbackStore):
        self.store = store
        self.ratings = ratings

    async def draft(
        self, agent: Agent, session: Any, *, session_id: str, turn_id: str
    ) -> dict[str, Any]:
        """저장하기 전의 사례. `replaced` 는 종류마다 자리표로 바꾼 수다."""
        rating = await self.ratings.for_turn(agent.tenant, agent.id, session_id, turn_id)
        if rating is None or not rating.evaluation_use:
            raise consent_missing()
        found = None if rating.superseded else rated_turn(transcript_items(session), turn_id)
        if found is None:
            raise EvaluationError(
                404, TURN_NOT_FOUND, f"session {session_id} has no rated turn {turn_id}"
            )
        _too_long("messages[0]", found[0], MAX_MESSAGE_CHARS)
        _too_long("reference", found[1], MAX_REFERENCE_CHARS)
        patterns = pii.agent_patterns(agent)
        question, q_counts = pii.replace(found[0], patterns)
        answer, a_counts = pii.replace(found[1], patterns)
        _too_long("messages[0]", question, MAX_MESSAGE_CHARS)
        _too_long("reference", answer, MAX_REFERENCE_CHARS)
        reference = CaseReference(text=answer, good=rating.rating == "up") if answer else None
        content = CaseContent(messages=[question], reference=reference)
        origin = CaseOrigin(rating_id=rating.id, session_id=session_id, turn_id=turn_id)
        replaced: Counter[str] = q_counts + a_counts
        return {
            "content": content.model_dump(),
            "origin": origin.model_dump(),
            "replaced": dict(replaced),
        }

    async def consented(self, agent: Agent, origin: CaseOrigin) -> AgentFeedback:
        """복사본을 저장해도 되는 평가. 그 Agent 의 그 턴의 평가이고 지금 동의하며 걷히지 않았어야
        한다. 아니면 409 `evaluation_consent_missing` 이다."""
        rating = (await self.ratings.get_many([origin.rating_id])).get(origin.rating_id)
        if (
            rating is None
            or (rating.tenant, rating.agent_id) != (agent.tenant, agent.id)
            or (rating.session_id, rating.turn_id) != (origin.session_id, origin.turn_id)
            or not rating.evaluation_use
            or rating.superseded
        ):
            raise consent_missing()
        return rating

    async def consenting(self, rating_ids: list[str]) -> set[str]:
        """rating_ids 가운데 지금 동의하는 평가의 id. 읽기 한 번이다."""
        ratings = await self.ratings.get_many(rating_ids)
        return {rid for rid, r in ratings.items() if r.evaluation_use}

    # -- 평가 라우트와 계정 훅 ----------------------------------------------------------

    async def rating_changed(self, fb: AgentFeedback) -> int:
        """평가를 고쳤다. 동의하면 복사본의 TTL 을 옮기고 참조 답의 good 을 평가에 맞추며, 아니면
        복사본을 지운다. good 이 바뀐 사례는 지문도 새로 짓는다."""
        if not fb.evaluation_use:
            return await self.store.delete_copies([fb.id])
        good = fb.rating == "up"
        for case in await self.store.copies_of(fb.id):
            reference = case.content.reference
            if reference is not None and reference.good != good:
                reference.good = good
                case.digest = case_digest(case.content)
                case.updated_at = utcnow()
                await self.store.update_case(case)
        return await self.store.move_copies(fb.id, fb.expires_at)

    async def ratings_gone(self, rating_ids: list[str]) -> int:
        return await self.store.delete_copies(rating_ids)

    async def withdraw_person(self, sub: str, tenant: str | None) -> int:
        """그 사람의 동의를 거두고 복사본을 지운다. tenant 가 없으면 모든 테넌트의 것이다."""
        ids = await self.ratings.withdraw_consents(sub, tenant)
        return await self.store.delete_copies(ids)
