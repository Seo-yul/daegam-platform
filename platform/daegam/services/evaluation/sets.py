"""평가 세트와 사례의 저장 (P45 7절·8절).

세트는 Agent 하나의 것이고 이름, 채점 모델(없어도 된다), 반복 수(1에서 3)를 가진다. 사례 수와 반복
수를 곱한 값은 100 이하이고 넘는 저장은 422 `evaluation_set_full` 이다. 채점 기준이 있는 사례가 든
세트는 채점 모델이 있어야 하므로, 채점 모델 없는 세트에 기준이 있는 사례를 넣거나 기준이 있는 사례가
든 세트의 채점 모델을 빼는 저장은 422 `invalid_value` 다. 사례마다 쓰거나 마지막으로 고친 사람이
남는다. Agent 를 지우면 그 세트와 사례도 지운다(`forget_agent`).

저장은 사례의 모든 칸에서 개인정보로 보이는 것을 찾는다(`pii.scan_case`). 대화에서 복사한 사례는
하나라도 있으면 422 `evaluation_case_pii` 이고(`acknowledgeable: false`), 그 평가가 지금도 동의할
때만 저장하며(아니면 409 `evaluation_consent_missing`) 평가의 기한을 자기 TTL 로 가진다. 손으로
쓴 사례는 요청이 확인(`pii_acknowledged`)을 실을 때까지 같은 422 에 `acknowledgeable: true` 이고,
확인을 받으면 저장하며 확인한 사람과 때를 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from daegam.auth.principal import Principal
from daegam.services.evaluation import pii
from daegam.services.evaluation.cases import case_digest, digests_of, set_digest
from daegam.services.evaluation.copies import EvaluationCopies
from daegam.services.evaluation.errors import EvaluationError
from daegam.services.evaluation.inputs import BASELINE_REPEATS
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.evaluation import (
    MAX_CASE_RUNS,
    CaseContent,
    CaseOrigin,
    EvaluationCase,
    EvaluationSet,
    MongoEvaluationStore,
    PiiAck,
)
from daegam.store.evaluation_runs import Grader

EVALUATION_SET_FULL = "evaluation_set_full"
EVALUATION_CASE_PII = "evaluation_case_pii"
EVALUATION_SET_IN_GATE = "evaluation_set_in_gate"


@dataclass(frozen=True)
class SetInput:
    name: str
    grader: Grader | None
    repeats: int


@dataclass(frozen=True)
class CaseInput:
    """저장할 사례. origin 은 대화에서 복사한 사례의 출처이고 만들 때만 받는다.
    pii_acknowledged 는 손으로 쓴 사례의 개인정보 경고를 확인했다는 표시다."""

    content: CaseContent
    origin: CaseOrigin | None = None
    pii_acknowledged: bool = False


def set_full() -> EvaluationError:
    return EvaluationError(
        422,
        EVALUATION_SET_FULL,
        f"a set's cases times its repeats can be at most {MAX_CASE_RUNS}",
        max=MAX_CASE_RUNS,
    )


def check_baseline_repeats(repeats: int, *, field: str) -> None:
    """배포 게이트가 기준선과 견주는 세트는 반복이 `BASELINE_REPEATS` 이상이다. 한 번의 아쉬운 답이
    배포를 막지 않게 하려는 것이다 (12절)."""
    if repeats < BASELINE_REPEATS:
        raise EvaluationError(
            422,
            "invalid_value",
            f"the deploy gate compares cases with their baseline only for a set that repeats each "
            f"case at least {BASELINE_REPEATS} times",
            field=field,
        )


def _check_grader(grader: Grader | None, content: CaseContent) -> None:
    """채점 기준이 있는 사례는 채점 모델이 있는 세트에만 든다."""
    if content.criteria and grader is None:
        raise EvaluationError(
            422, "invalid_value", "a set whose cases have criteria needs a grader", field="grader"
        )


def not_found(what: str, item_id: str) -> EvaluationError:
    return EvaluationError(404, "not_found", f"{what} {item_id} not found")


def _pii(findings: list[pii.Finding], *, acknowledgeable: bool) -> EvaluationError:
    return EvaluationError(
        422,
        EVALUATION_CASE_PII,
        "the case holds text that looks like personal information",
        matches=[f.to_dict() for f in findings],
        acknowledgeable=acknowledgeable,
    )


class EvaluationSets:
    def __init__(self, store: MongoEvaluationStore, copies: EvaluationCopies):
        self.store = store
        self.copies = copies

    # -- 세트 ---------------------------------------------------------------------------

    async def sets(self, agent: Agent) -> list[tuple[EvaluationSet, str]]:
        """그 Agent 의 세트와 세트마다의 지문. 만든 차례다."""
        items = await self.store.sets_of(agent.tenant, agent.id)
        digests = await self.store.case_digests(agent.tenant, [s.id for s in items])
        return [(s, set_digest(s, digests.get(s.id, []))) for s in items]

    async def get_set(self, agent: Agent, set_id: str) -> EvaluationSet:
        item = await self.store.get_set(agent.tenant, agent.id, set_id)
        if item is None:
            raise not_found("evaluation set", set_id)
        return item

    async def with_digest(self, item: EvaluationSet) -> tuple[EvaluationSet, str]:
        digests = await self.store.case_digests(item.tenant, [item.id])
        return item, set_digest(item, digests.get(item.id, []))

    async def create_set(
        self, agent: Agent, value: SetInput, principal: Principal
    ) -> EvaluationSet:
        item = EvaluationSet(
            tenant=agent.tenant,
            agent_id=agent.id,
            name=value.name,
            grader=value.grader,
            repeats=value.repeats,
            created_by=principal.sub,
            updated_by=principal.sub,
        )
        return await self.store.create_set(item)

    async def update_set(
        self, agent: Agent, set_id: str, value: SetInput, principal: Principal
    ) -> EvaluationSet:
        item = await self.get_set(agent, set_id)
        gate = agent.evaluation_gate
        if gate is not None and gate.set_id == set_id and gate.no_worse_cases:
            check_baseline_repeats(value.repeats, field="repeats")
        if value.grader is None and item.grader is not None:
            for case in await self.store.cases_of(agent.tenant, [set_id]):
                _check_grader(None, case.content)
        item.name, item.grader, item.repeats = value.name, value.grader, value.repeats
        item.updated_at, item.updated_by = utcnow(), principal.sub
        if not await self.store.update_set(item):
            # 쓰지 못한 까닭이 상한인지, 그 사이 지워진 세트인지(404) 다시 읽어 가른다
            await self.get_set(agent, set_id)
            raise set_full()
        return item

    async def delete_set(self, agent: Agent, set_id: str) -> None:
        """세트와 그 사례를 지운다. 배포 게이트가 읽는 세트는 409 `evaluation_set_in_gate` 다
        (P45 12절)."""
        gate = agent.evaluation_gate
        if gate is not None and gate.set_id == set_id:
            raise EvaluationError(
                409,
                EVALUATION_SET_IN_GATE,
                "the deploy gate reads this set; turn the gate off or point it at another set",
            )
        if not await self.store.delete_set(agent.tenant, agent.id, set_id):
            raise not_found("evaluation set", set_id)

    # -- 사례 ---------------------------------------------------------------------------

    async def cases(
        self, agent: Agent, set_id: str
    ) -> tuple[EvaluationSet, list[EvaluationCase], str]:
        """세트, 보여 줄 사례, 세트의 지문. 그 평가가 이제 동의하지 않는 복사본은 정리가 지우기
        전에도 보이지 않는다. 지문은 저장된 모든 사례로 짓는다."""
        item = await self.get_set(agent, set_id)
        cases = await self.store.cases_of(agent.tenant, [set_id])
        digest = set_digest(item, digests_of(cases))
        consenting = await self.copies.consenting(
            [c.origin.rating_id for c in cases if c.origin is not None]
        )
        shown = [c for c in cases if c.origin is None or c.origin.rating_id in consenting]
        return item, shown, digest

    async def _case_in(self, item: EvaluationSet, case_id: str) -> EvaluationCase:
        found = await self.store.get_case(item.tenant, item.id, case_id)
        if found is None:
            raise not_found("evaluation case", case_id)
        return found

    async def _screen(
        self, agent: Agent, value: CaseInput, origin: CaseOrigin | None, principal: Principal
    ) -> tuple[PiiAck | None, datetime | None]:
        """저장 앞의 개인정보 검사와 복사본의 동의 검사. (손으로 쓴 사례의 확인, 복사본의 기한)
        이다."""
        findings = pii.scan_case(value.content, pii.agent_patterns(agent))
        if origin is not None:
            if findings:
                raise _pii(findings, acknowledgeable=False)
            rating = await self.copies.consented(agent, origin)
            return None, rating.expires_at
        if findings and not value.pii_acknowledged:
            raise _pii(findings, acknowledgeable=True)
        return (PiiAck(by=principal.sub, at=utcnow()) if findings else None), None

    async def create_case(
        self, agent: Agent, set_id: str, value: CaseInput, principal: Principal
    ) -> EvaluationCase:
        """사례 하나를 넣는다. 세트의 자리를 먼저 잡고 넣으며, 넣기가 실패하면 자리를 놓는다.
        복사본은 넣은 뒤 동의를 한 번 더 본다. 그 사이 동의가 끝났으면 넣은 것을 지우고 409
        `evaluation_consent_missing` 이다."""
        item = await self.get_set(agent, set_id)
        content = value.content
        _check_grader(item.grader, content)
        ack, expires_at = await self._screen(agent, value, value.origin, principal)
        if not await self.store.reserve_case(item, principal.sub):
            # 자리를 못 잡은 까닭이 상한인지, 그 사이 지워진 세트인지(404) 다시 읽어 가른다
            await self.get_set(agent, set_id)
            raise set_full()
        case = EvaluationCase(
            tenant=agent.tenant,
            agent_id=agent.id,
            set_id=set_id,
            content=content,
            digest=case_digest(content),
            origin=value.origin,
            expires_at=expires_at,
            pii_ack=ack,
            created_by=principal.sub,
            updated_by=principal.sub,
        )
        try:
            await self.store.insert_case(case)
        except BaseException:
            await self.store.release_case(set_id)
            raise
        if value.origin is not None:
            try:
                await self.copies.consented(agent, value.origin)
            except EvaluationError:
                await self.store.delete_case(agent.tenant, set_id, case.id)
                raise
        return case

    async def update_case(
        self, agent: Agent, set_id: str, case_id: str, value: CaseInput, principal: Principal
    ) -> EvaluationCase:
        """사례의 내용을 바꾼다. 복사한 사례는 출처를 그대로 두고 저장할 때마다 동의를 다시 본다."""
        item = await self.get_set(agent, set_id)
        content = value.content
        _check_grader(item.grader, content)
        case = await self._case_in(item, case_id)
        case.pii_ack, case.expires_at = await self._screen(agent, value, case.origin, principal)
        case.content, case.digest = content, case_digest(content)
        case.updated_at, case.updated_by = utcnow(), principal.sub
        if not await self.store.update_case(case):
            raise not_found("evaluation case", case_id)
        return case

    async def delete_case(self, agent: Agent, set_id: str, case_id: str) -> None:
        await self.get_set(agent, set_id)
        if not await self.store.delete_case(agent.tenant, set_id, case_id):
            raise not_found("evaluation case", case_id)

    # -- 지우기 ------------------------------------------------------------------------

    async def forget_agent(self, tenant: str, agent_id: str) -> int:
        """Agent 를 지울 때 그 세트와 사례를 지운다 (7절)."""
        return await self.store.forget_agent(tenant, agent_id)
