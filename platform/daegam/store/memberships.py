"""테넌트 소속과 Okta 그룹 매핑 (P6 F2).

memberships   {sub, tenant, role, source: okta_group|manual, updated_at}  (sub, tenant) 유니크
group_mappings {group, tenant, role}                                       (group, tenant) 유니크
group_mapping_state {_id: "group_mappings", revision, mappings, unrecorded}
platform-admin 은 tenant="*" 로 표현한다. 로그인(첫 요청)마다 토큰의 groups 를 group_mappings 로
풀어 okta_group 소속을 동기화한다. manual 소속은 그룹과 무관하게 유지되고, 같은 (sub, tenant) 에
둘 다 있으면 manual 이 이긴다.

**매핑의 리비전 (P53b 6.3절).** `group_mapping_state` 는 매핑 표가 바뀔 때마다 오르는 리비전과
그 리비전의 표(`mappings`)를 든다. 표가 리비전의 것과 다르면 `advance_mappings` 가 리비전을 올리고,
매핑이 빠졌거나 역할이 내려간 테넌트를 `unrecorded` 에 더한다. 사람 기록이 있는 사람의 소속 줄은
그 기록의 그룹으로 다시 풀고(`services/memberships.py`), 기록이 없는 사람은 `unrecorded` 의
테넌트의 okta_group 줄만 잃는다. 리비전과 표를 한 문서에 두므로 표를 바꾼 요청이 리비전을 올리기
전에 멈춰도 다음 `advance_mappings` 가 바뀐 표를 알아본다. 2.0.0-rc1 의 레플리카가 바꾼 표도
같다. 매핑과 소속 줄의 문서에는 새 필드를 두지 않는다. rc1 의 모델이 남는 필드를 받지 않는다.

표는 차이만 쓴다. 넓히는 쪽(새 매핑, 오른 역할)을 먼저 쓰고 좁히는 쪽(빠진 매핑, 내려간 역할)을
뒤에 쓴다. 그래서 쓰는 중에 표를 읽은 쪽(사람의 요청, 다시 풀기)은 끝난 표보다 넓은 것만 본다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.auth import rbac
from daegam.store.base import UtcDatetime, insert_new, utcnow

logger = logging.getLogger("daegam.memberships")

MEMBERSHIPS = "memberships"
GROUP_MAPPINGS = "group_mappings"
GROUP_MAPPING_STATE = "group_mapping_state"
_STATE_ID = "group_mappings"

Source = Literal["okta_group", "manual"]
OKTA_GROUP: Source = "okta_group"


class Membership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub: str
    tenant: str
    role: str
    source: Source = "manual"
    updated_at: UtcDatetime = Field(default_factory=utcnow)


class GroupMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    tenant: str
    role: str


class MappingState(BaseModel):
    """매핑 표의 리비전과 그 리비전의 표, 기록 없는 사람에게서 아직 줄을 지우지 않은 테넌트."""

    model_config = ConfigDict(extra="ignore")

    revision: int = 0
    mappings: list[GroupMapping] = Field(default_factory=list)
    unrecorded: list[str] = Field(default_factory=list)


def _strip(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def _roles(mappings: Iterable[GroupMapping]) -> dict[tuple[str, str], str]:
    return {(m.group, m.tenant): m.role for m in mappings}


def lowered_tenants(old: Iterable[GroupMapping], new: Iterable[GroupMapping]) -> list[str]:
    """old 에서 new 로 가며 매핑이 빠졌거나 역할이 내려간 테넌트. `*` 도 든다."""
    now = _roles(new)
    return sorted(
        {
            m.tenant
            for m in old
            if (m.group, m.tenant) not in now or rbac.role_below(now[m.group, m.tenant], m.role)
        }
    )


def desired_rows(groups: Iterable[str], mappings: Iterable[GroupMapping]) -> dict[str, str]:
    """groups 가 mappings 로 받는 (테넌트 → 역할). 한 테넌트에 여럿이면 높은 역할이다."""
    held = set(groups)
    desired: dict[str, str] = {}
    for m in mappings:
        if m.group in held:
            desired[m.tenant] = rbac.merge_role(m.role, desired.get(m.tenant))
    return desired


class MongoMembershipStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[MEMBERSHIPS]
        self.mappings = db[GROUP_MAPPINGS]
        self.state = db[GROUP_MAPPING_STATE]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("sub", ASCENDING), ("tenant", ASCENDING)], unique=True, name="uq_sub_tenant"
        )
        await self.coll.create_index([("tenant", ASCENDING)], name="ix_tenant")
        await self.mappings.create_index(
            [("group", ASCENDING), ("tenant", ASCENDING)], unique=True, name="uq_group_tenant"
        )

    # -- memberships ---------------------------------------------------------

    async def list_for_sub(self, sub: str) -> list[Membership]:
        cursor = self.coll.find({"sub": sub}).sort("tenant", ASCENDING)
        return [Membership.model_validate(_strip(d)) async for d in cursor]

    async def tenants_of(self, sub: str) -> list[str]:
        """sub 의 소속 줄이 있는 테넌트. platform-admin 의 `*` 줄도 그대로 있다. 개인 토큰의 소속
        판정(`rbac.holds_tenant`)이 이것을 받는다."""
        return [d["tenant"] async for d in self.coll.find({"sub": sub}, {"tenant": 1})]

    async def role_in(self, sub: str, tenant: str) -> str | None:
        """sub 의 그 테넌트 역할. 소속 줄이 없으면 None 이다. 사람의 토큰이 싣는 역할이다 (P52
        3절)."""
        doc = await self.coll.find_one({"sub": sub, "tenant": tenant}, {"role": 1})
        return doc.get("role") if doc else None

    async def list_for_tenant(self, tenant: str) -> list[Membership]:
        cursor = self.coll.find({"tenant": tenant}).sort("sub", ASCENDING)
        return [Membership.model_validate(_strip(d)) async for d in cursor]

    async def upsert(self, m: Membership) -> None:
        await self.coll.update_one(
            {"sub": m.sub, "tenant": m.tenant}, {"$set": m.model_dump()}, upsert=True
        )

    async def replace_manual(self, tenant: str, items: list[tuple[str, str]]) -> list[Membership]:
        """테넌트의 manual 소속을 items(sub, role) 로 통째로 바꾼다. okta_group 소속은 건드리지
        않되, 같은 sub 이 목록에 있으면 manual 로 바뀐다(manual 이 이긴다)."""
        keep = {sub for sub, _ in items}
        await self.coll.delete_many(
            {"tenant": tenant, "source": "manual", "sub": {"$nin": list(keep)}}
        )
        for sub, role in items:
            await self.upsert(Membership(sub=sub, tenant=tenant, role=role, source="manual"))
        return await self.list_for_tenant(tenant)

    async def sync_groups(
        self, sub: str, groups: list[str], mappings: list[GroupMapping] | None = None
    ) -> list[Membership]:
        """토큰의 groups 를 group_mappings 로 풀어 okta_group 소속을 맞추고 전체 소속을 준다.
        mappings 를 주면 저장된 표 대신 그 표로 푼다. 리비전 하나의 표로 다시 풀 때다."""
        if mappings is None:
            mappings = await self._matching(groups)
        desired = desired_rows(groups, mappings)
        existing = await self.list_for_sub(sub)
        by_tenant = {m.tenant: m for m in existing}
        changed = False
        for tenant, role in desired.items():
            cur = by_tenant.get(tenant)
            if cur is not None and cur.source == "manual":
                continue  # manual 이 이긴다
            if cur is not None and cur.role == role:
                continue
            await self.upsert(Membership(sub=sub, tenant=tenant, role=role, source=OKTA_GROUP))
            changed = True
        stale = [m.tenant for m in existing if m.source == OKTA_GROUP and m.tenant not in desired]
        if stale:
            await self.coll.delete_many(
                {"sub": sub, "source": OKTA_GROUP, "tenant": {"$in": stale}}
            )
            changed = True
        return await self.list_for_sub(sub) if changed else existing

    # -- group mappings ------------------------------------------------------

    async def set_platform_admin(self, sub: str, on: bool) -> None:
        """platform-admin 의 진실은 `*` 줄 하나다 (D40). 켜면 manual 로 넣고 끄면 지운다."""
        if on:
            await self.upsert(
                Membership(
                    sub=sub, tenant=rbac.ALL_TENANTS, role=rbac.PLATFORM_ADMIN, source="manual"
                )
            )
        else:
            await self.coll.delete_one({"sub": sub, "tenant": rbac.ALL_TENANTS})

    async def delete_all_for_sub(self, sub: str) -> int:
        """사용자를 지울 때 소속도 함께 지운다 (D40)."""
        r = await self.coll.delete_many({"sub": sub})
        return r.deleted_count

    async def has_platform_admin_mapping(self) -> bool:
        """표에 platform-admin 매핑이 하나라도 있는가.

        부트스트랩 그룹은 이것이 거짓일 때만 힘을 갖는다."""
        return (
            await self.mappings.find_one(
                {"tenant": rbac.ALL_TENANTS, "role": rbac.PLATFORM_ADMIN}, {"_id": 1}
            )
            is not None
        )

    async def _matching(self, groups: list[str]) -> list[GroupMapping]:
        if not groups:
            return []
        cursor = self.mappings.find({"group": {"$in": groups}})
        return [GroupMapping.model_validate(_strip(d)) async for d in cursor]

    async def list_mappings(self) -> list[GroupMapping]:
        cursor = self.mappings.find({}).sort([("group", ASCENDING), ("tenant", ASCENDING)])
        return [GroupMapping.model_validate(_strip(d)) async for d in cursor]

    async def replace_mappings(self, items: list[GroupMapping]) -> list[GroupMapping]:
        """표를 items 로 바꾼다. 모듈 머리글처럼 넓히는 쪽을 먼저, 좁히는 쪽을 뒤에 쓴다."""
        old, new = _roles(await self.list_mappings()), _roles(items)
        widen = [k for k, role in new.items() if k not in old or rbac.role_below(old[k], role)]
        narrow = [k for k, role in new.items() if k in old and rbac.role_below(role, old[k])]
        for group, tenant in widen + narrow:
            await self.mappings.update_one(
                {"group": group, "tenant": tenant},
                {"$set": {"role": new[group, tenant]}},
                upsert=True,
            )
        removed = [{"group": g, "tenant": t} for g, t in old if (g, t) not in new]
        if removed:
            await self.mappings.delete_many({"$or": removed})
        return await self.list_mappings()

    # -- 매핑의 리비전 (P53b 6.3절) ---------------------------------------------

    async def mapping_state(self) -> MappingState | None:
        doc = await self.state.find_one({"_id": _STATE_ID})
        return MappingState.model_validate(doc) if doc is not None else None

    async def advance_mappings(self, *, allow_empty: bool = False) -> MappingState:
        """표가 마지막 리비전의 것과 다르면 리비전을 올린다. 빠졌거나 역할이 내려간 테넌트를
        `unrecorded` 에 더한다. 상태가 아직 없으면 지금 표를 리비전 0 으로 적는다. 그 표는 이미
        모두에게 풀린 것으로 본다. 지금 상태를 돌려준다.

        빈 표는 allow_empty 일 때만 리비전이 된다. 플랫폼 관리자가 표를 비운 요청뿐이다. 그 밖의
        쪽(리컨실, 요청이 바꾸기 전에 맞추는 것)이 빈 표를 읽은 것은 2.0.0-rc1 레플리카의 교체가
        지우고 넣는 사이일 수 있다. 그것을 리비전으로 삼으면 모두의 줄을 지운다."""
        table = await self.list_mappings()
        state = await self.mapping_state()
        if state is None:
            first = MappingState(mappings=table)
            await insert_new(self.state, [{"_id": _STATE_ID, **first.model_dump()}])
        elif not table and state.mappings and not allow_empty:
            logger.warning(
                "group mappings: the table reads empty while revision %d holds %d mappings; "
                "not advancing (only an admin's PUT may empty it)",
                state.revision,
                len(state.mappings),
            )
        elif _roles(table) != _roles(state.mappings):
            # 리비전을 조건으로 건다. 둘이 함께 올리면 하나만 지나고 다른 쪽은 다시 읽는다
            await self.state.update_one(
                {"_id": _STATE_ID, "revision": state.revision},
                {
                    "$set": {
                        "revision": state.revision + 1,
                        "mappings": [m.model_dump() for m in table],
                    },
                    "$addToSet": {"unrecorded": {"$each": lowered_tenants(state.mappings, table)}},
                },
            )
        return await self.mapping_state() or MappingState(mappings=table)

    async def mapping_revision(self) -> int:
        """지금 매핑의 리비전. 상태가 없으면 0 이다."""
        doc = await self.state.find_one({"_id": _STATE_ID}, {"revision": 1})
        return int(doc.get("revision", 0)) if doc else 0

    async def finish_unrecorded(self, tenant: str, revision: int) -> None:
        """기록 없는 사람에게서 tenant 의 줄을 다 지웠다. 그사이 리비전이 오르지 않았을 때만 뺀다.
        올랐으면 다음 차례가 다시 본다."""
        await self.state.update_one(
            {"_id": _STATE_ID, "revision": revision}, {"$pull": {"unrecorded": tenant}}
        )

    async def okta_subs(self, tenant: str) -> list[str]:
        """tenant 에 okta_group 소속 줄이 있는 sub."""
        cursor = self.coll.find({"tenant": tenant, "source": OKTA_GROUP}, {"sub": 1})
        return sorted({d["sub"] async for d in cursor})

    async def drop_okta(self, sub: str, tenant: str) -> str | None:
        """sub 의 tenant okta_group 줄을 지우고 그 역할을 돌려준다. 없었으면 None 이다. manual 줄은
        남는다."""
        doc = await self.coll.find_one_and_delete(
            {"sub": sub, "tenant": tenant, "source": OKTA_GROUP}, {"role": 1}
        )
        return doc.get("role") if doc else None
