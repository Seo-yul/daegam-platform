"""평가 정리 (P45 8절·16절). 리컨실 틱의 한 단계로 한 시간에 한 번, 리스 안에서 돈다.

1. 복사한 사례 가운데 그 평가가 없거나, 동의하지 않거나, 평가한 사람이 사라진 것을 지운다. 사람은
   local 모드면 사용자 문서가 없을 때, oidc 모드면 사람 기록이 없거나 비워졌을 때(`cleared_at`,
   사람 기록은 지우지 않는다) 사라진 것이다. header 모드에는 사람 기록이 없어 보지 않는다. 세션과
   Agent 의 삭제, oidc 모드에서 떠난 사람, 실패한 평가 라우트와 계정 훅이 남긴 복사본이 여기서
   잡힌다.
2. 없는 Agent 의 세트, 사례, 실행과 결과를 지운다. 이 판이 없는 판에서 지운 Agent 가 남긴
   것이다.
3. 세트가 없는 사례를 지운다. 세트를 지우는 동안 들어온 사례다.
4. 세트의 사례 수(`case_count`)를 실제 수에 맞춘다. TTL 이 지운 복사본은 자리를 돌려주지 않는다.
5. 사례가 없는 실행 결과를 지운다. 사례를 TTL 이 지웠거나, 실행이 사례를 확인한 뒤 결과를 쓰기
   전에 동의가 끝나 사례가 지워진 것이다(9절). 그래서 그런 결과는 한 시간 안에 사라진다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from daegam.services.reconcile import interval_due
from daegam.store.agent_feedback import MongoAgentFeedbackStore
from daegam.store.agents import MongoAgentStore
from daegam.store.evaluation import MongoEvaluationStore
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.users import MongoUserStore

SWEEP_SECONDS = 3600.0


class EvaluationSweep:
    def __init__(
        self,
        store: MongoEvaluationStore,
        ratings: MongoAgentFeedbackStore,
        agents: MongoAgentStore,
        *,
        users: MongoUserStore,
        people: MongoIdpPeopleStore,
        auth_mode: str,
        interval: float = SWEEP_SECONDS,
    ):
        self.store = store
        self.ratings = ratings
        self.agents = agents
        self.users = users
        self.people = people
        self.auth_mode = auth_mode
        self.interval = interval
        # 마지막으로 돈 때. 리스를 다른 파드가 잡으면 그 파드가 처음부터 센다
        self._last: datetime | None = None

    async def run(self, now: datetime) -> dict[str, list[str]]:
        """때가 아니면 결과 키가 없다. 지운 사례와 Agent, 다시 센 세트의 id 를 돌려준다."""
        if not interval_due(self._last, now, self.interval):
            return {}
        result = {
            "copies": await self._stale_copies(),
            "agents": await self._missing_agents(),
            "orphans": await self._orphans(),
            "recounted": await self.store.recount(now),
            "results": await self.store.orphan_results(),
        }
        # 끝까지 돈 뒤에만 한 시간을 센다. 실패한 정리는 다음 틱이 다시 돈다
        self._last = now
        return {key: ids for key, ids in result.items() if ids}

    async def _stale_copies(self) -> list[str]:
        copies = await self.store.copied_cases()
        ratings = await self.ratings.get_many(list({d["origin"]["rating_id"] for d in copies}))
        gone = await self._people_gone({r.user_sub for r in ratings.values()})
        stale = []
        for d in copies:
            rating = ratings.get(d["origin"]["rating_id"])
            if rating is None or not rating.evaluation_use or rating.user_sub in gone:
                stale.append(d)
        await self.store.delete_cases(stale)
        return [d["_id"] for d in stale]

    async def _people_gone(self, subs: Iterable[str]) -> set[str]:
        subs = set(subs)
        if self.auth_mode == "local":
            return subs - set(await self.users.get_many(subs))
        if self.auth_mode == "oidc":
            people = await self.people.get_many(subs)
            return {s for s in subs if s not in people or people[s].cleared_at is not None}
        return set()

    async def _missing_agents(self) -> list[str]:
        ids = await self.store.agent_ids()
        missing = sorted(ids - await self.agents.existing_ids(ids))
        await self.store.forget_agents(missing)
        return missing

    async def _orphans(self) -> list[str]:
        found = await self.store.cases_without_set()
        await self.store.delete_cases(found)
        return [d["_id"] for d in found]
