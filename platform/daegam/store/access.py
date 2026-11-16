"""접근 정책 `access`(D23)를 가진 문서의 저장 쪽 도우미. 허브 항목과 Skill 이 같은 정책을 쓴다.

판정은 `auth/hub_access.py` 가 하고 여기는 후보를 고르는 질의와 그 인덱스, 정책을 저장할 때 함께
적는 필드만 둔다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pymongo import ASCENDING

from daegam.auth.hub_access import ACTIONS, ALL_TENANTS, Access
from daegam.auth.principal import Principal
from daegam.store.base import utcnow

# 정책 저장이 쓰는 필드. 다른 저장과 겹쳐도 서로의 필드를 덮지 않게 이것만 쓸 수 있다
ACCESS_FIELDS = ("access", "access_updated_by", "access_updated_at", "updated_at")


class AccessStamped(Protocol):
    access: Access
    access_updated_by: str | None
    access_updated_at: datetime | None
    updated_at: datetime


def stamp_access(item: AccessStamped, access: Access, principal: Principal) -> None:
    """검증을 지난 정책을 item 에 적고 바꾼 주체와 시각을 남긴다. 저장은 부르는 쪽이 한다."""
    item.access = access
    item.access_updated_by = principal.sub
    item.access_updated_at = item.updated_at = utcnow()


async def create_access_indexes(coll: Any) -> None:
    """경계 `access.<동작>.tenants` 의 인덱스. 배열 필드는 인덱스 하나에 둘 이상 넣을 수 없어
    동작마다 따로 만든다."""
    for action in ACTIONS:
        await coll.create_index(
            [(f"access.{action}.tenants", ASCENDING)], name=f"ix_access_tenants_{action}"
        )


def boundary_candidates(tenant: str) -> list[dict[str, Any]]:
    """tenant 에게 보일 수 있는 문서의 `$or` 조건들. 자기 테넌트 것과, 어느 동작이든 경계에
    tenant 나 `*` 가 든 것이다. view 판정은 서비스가 한다."""
    wanted = {"$in": [tenant, ALL_TENANTS]}
    return [{"tenant": tenant}, *({f"access.{a}.tenants": wanted} for a in ACTIONS)]
