"""테넌트 멤버 교체와 Group Mapping 교체 (P32 7절, P53b 6.3절).

라우터(`api/members.py`)가 스토어를 바로 부르던 자리에 한 겹을 둔다. 바뀌기 전후의 소속을 비교해
테넌트에서 빠진 사람마다 회수 훅(`membership`)을 부른다. 그 사람의 그 테넌트 개인 토큰과 OAuth
연결이 회수되고 그 사람에게 묶인 실행 소스가 멈춘다. 역할이 낮아진 사람은 그 테넌트 개인 토큰만
회수한다(`role_changed`). 개인 토큰이 발급 때의 역할을 싣고 Tool 규칙이 그 역할을 보기
때문이다 (P52 3절). 역할이 오른 사람은 건드리지 않는다. platform-admin 의 `*` 줄은 테넌트가
아니므로(`rbac.holds_tenant`) 훅을 부르지 않는다.

**Group Mapping.** 플랫폼 관리자가 매핑 표를 바꾸면 요청이 리비전을 올리고(`advance_mappings`)
사람들의 okta_group 줄을 다시 푼다. 사람 기록(`idp_people`)이 있고 그룹을 아는 사람은 기록의 그룹을
그 리비전의 표로 다시 풀고 리비전을 찍는다. 기록이 없거나 그룹을 모르는 사람(업그레이드 뒤 아직
요청하지 않았거나 groups 클레임 없는 토큰만 보였다)은 매핑이 빠졌거나 역할이 내려간 테넌트의
okta_group 줄만 잃는다. 매핑을 더하거나 역할을 올리면 그들에게서 아무것도 지우지 않는다. 요청은
`REQUEST_BUDGET` 안에서 닿는 데까지 하고, 리컨실 단계(`finish_mappings`)가 리비전이 낮은 사람과
남은 테넌트를 틱마다 `STEP_BUDGET` 안에서 마저 한다. 요청이 시간을 넘기거나 레플리카가 멈춰도 옛
매핑에 남는 사람이 없다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from daegam.auth import rbac
from daegam.services.access_revocation import AccessRevoker
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.memberships import GroupMapping, MappingState, Membership, MongoMembershipStore
from daegam.store.personal_tokens import MEMBERSHIP, ROLE_CHANGED

# 매핑을 바꾼 요청이 직접 다시 푸는 시간(초). 넘기면 리컨실 단계가 마저 한다
REQUEST_BUDGET = 20.0
# 리컨실 단계 한 번이 다시 푸는 시간(초). 리스(90초) 안에 끝나야 한다
STEP_BUDGET = 20.0
# 한 번에 읽는 사람 기록 수
BATCH = 100
# 요청 없는 다시 풀기의 주체. 회수한 토큰의 revoked_by 다
MAPPINGS_SUB = "system:group-mappings"


class MembershipService:
    def __init__(
        self,
        store: MongoMembershipStore,
        revoker: AccessRevoker,
        people: MongoIdpPeopleStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.revoker = revoker
        self.people = people
        self.clock = clock

    async def replace_manual(
        self, tenant: str, items: list[tuple[str, str]], *, by: str
    ) -> list[Membership]:
        """테넌트의 manual 소속을 items(sub, role) 로 바꾸고 빠진 sub 의 그 테넌트 토큰을 회수한다.
        okta_group 소속이 남은 사람은 아직 소속이므로 빠진 것이 아니다."""
        before = {m.sub: m.role for m in await self.store.list_for_tenant(tenant)}
        rows = await self.store.replace_manual(tenant, items)
        after = {m.sub: m.role for m in rows}
        for sub in sorted(before.keys() | after.keys()):
            await self._changed(sub, tenant, before.get(sub), after.get(sub), by=by)
        return rows

    async def _changed(
        self, sub: str, tenant: str, before: str | None, after: str | None, *, by: str
    ) -> None:
        """sub 의 tenant 역할이 before 에서 after 로 바뀌었다. 빠졌으면 `membership`, 내려갔으면
        `role_changed` 훅이다. 접근 정리가 같은 사유로 놓친 것을 잡는다."""
        if before is None or tenant == rbac.ALL_TENANTS:
            return
        if after is None:
            await self.revoker.revoke_for(sub, MEMBERSHIP, by=by, tenant=tenant)
        elif rbac.role_below(after, before):
            await self.revoker.revoke_tokens_for(sub, ROLE_CHANGED, by=by, tenant=tenant)

    # -- Group Mapping (P53b 6.3절) ---------------------------------------------------

    async def replace_mappings(self, items: list[GroupMapping], *, by: str) -> list[GroupMapping]:
        """표를 items 로 바꾸고 닿는 데까지 사람들의 줄을 다시 푼다. 바꾸기 전에 상태를 맞춰 두어
        업그레이드 뒤 첫 교체도 바뀐 테넌트를 안다. 빈 표는 관리자가 비운 이 요청만 리비전으로
        삼는다."""
        await self.store.advance_mappings()
        table = await self.store.replace_mappings(items)
        await self._rederive(by=by, budget=REQUEST_BUDGET, allow_empty=not items)
        return table

    async def finish_mappings(self, _now: datetime) -> dict[str, list[str]]:
        """리컨실 단계. 요청이 끝내지 못한 다시 풀기를 마저 한다. 결과 키 `rederived` 는 줄이 바뀐
        사람, `dropped` 는 줄을 잃은 기록 없는 사람의 `<sub>/<tenant>` 다."""
        return await self._rederive(by=MAPPINGS_SUB, budget=STEP_BUDGET, allow_empty=False)

    async def _rederive(self, *, by: str, budget: float, allow_empty: bool) -> dict[str, list[str]]:
        """리비전 하나를 사람마다 다시 푼다. 사람마다 리비전이 그대로인지 다시 보고, 올랐으면
        멈춘다. 새 리비전은 다음 차례가 처음부터 푼다. 늦은 차례가 앞 리비전의 줄을 새 리비전으로
        찍힌 사람에게 덮어쓰지 않게 하려는 것이다."""
        deadline = self.clock() + budget
        state = await self.store.advance_mappings(allow_empty=allow_empty)
        rederived: list[str] = []
        # 리비전 0 은 업그레이드 때의 표라 다시 풀 것이 없다
        while state.revision > 0 and self._in_time(deadline):
            batch = await self.people.stale(state.revision, BATCH)
            if not batch:
                break
            for sub in batch:
                if not self._in_time(deadline) or not await self._current(state):
                    return _rederived(rederived, [])
                if await self._rederive_person(sub, state, by=by):
                    rederived.append(sub)
        dropped: list[str] = []
        for tenant in list(state.unrecorded):
            if not self._in_time(deadline) or not await self._current(state):
                break
            dropped += await self._drop_unrecorded(tenant, state, by=by)
        return _rederived(rederived, dropped)

    def _in_time(self, deadline: float) -> bool:
        return self.clock() < deadline

    async def _current(self, state: MappingState) -> bool:
        """state 가 여전히 지금 리비전인가."""
        return await self.store.mapping_revision() == state.revision

    async def _rederive_person(self, sub: str, state: MappingState, *, by: str) -> bool:
        """기록을 다시 읽어 지금의 그룹을 리비전의 표로 풀고 리비전을 찍는다. 그사이 다른 차례가
        이 리비전 이상으로 찍었으면 건너뛴다. 그룹을 모르는 기록(groups 클레임을 본 적이 없거나
        비웠다)은 줄을 두고 찍기만 한다. 그런 사람은 기록 없는 사람처럼 `unrecorded` 의 테넌트만
        잃는다. 줄이 바뀌었으면 참이다."""
        person = await self.people.get(sub)
        if person is None or person.mapping_rev >= state.revision:
            return False
        changed = False
        if person.groups is not None:
            before = {m.tenant: m.role for m in await self.store.list_for_sub(sub)}
            rows = await self.store.sync_groups(sub, person.groups, state.mappings)
            after = {m.tenant: m.role for m in rows}
            changed = before != after
            for tenant in sorted(before):
                await self._changed(sub, tenant, before[tenant], after.get(tenant), by=by)
        await self.people.stamp(sub, state.revision)
        return changed

    async def _drop_unrecorded(self, tenant: str, state: MappingState, *, by: str) -> list[str]:
        """tenant 에서 기록이 없거나 그룹을 모르는 사람의 okta_group 줄을 지우고 훅을 부른다.
        그룹을 아는 기록은 위에서 다시 풀었다."""
        subs = await self.store.okta_subs(tenant)
        known = await self.people.get_many(subs)
        dropped: list[str] = []
        for sub in subs:
            person = known.get(sub)
            if person is not None and person.groups is not None:
                continue
            role = await self.store.drop_okta(sub, tenant)
            if role is not None:
                dropped.append(f"{sub}/{tenant}")
                await self._changed(sub, tenant, role, None, by=by)
        await self.store.finish_unrecorded(tenant, state.revision)
        return dropped


def _rederived(rederived: list[str], dropped: list[str]) -> dict[str, list[str]]:
    """리컨실 단계의 결과. 빈 키는 뺀다."""
    result = {"rederived": rederived, "dropped": dropped}
    return {k: v for k, v in result.items() if v}
