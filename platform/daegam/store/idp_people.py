"""oidc 모드의 사람 기록 (P53b 6.3절).

idp_people {_id: IdP sub, username, email, groups, confirmed_at, token_life, cleared_at?,
            mapping_rev, updated_at}

플랫폼은 IdP 에 사람을 묻지 않는다. 사람이 가져온 토큰에서 IdP 가 그 사람을 마지막으로 보증한 때를
배울 뿐이다. 그래서 요청마다(`api/deps.py` `_oidc_principal`) 검증한 IdP 액세스 토큰을 이 기록에
적는다.

- `confirmed_at` 은 플랫폼이 검증한 그 사람의 IdP 액세스 토큰 가운데 가장 큰 `iat` 이다. 액세스
  토큰은 IdP 가 그것을 낸 때만 증명하므로 요청 시각이 아니라 IdP 의 시각이다. 뒤로 가지 않는다.
- `token_life` 는 그 토큰의 `exp - iat` 다. 대화형 한도가 IdP 의 토큰 수명보다 길어야 하므로 상태가
  이것을 본다.
- `username` 은 `preferred_username`, 없으면 이메일이다. `groups` 는 설정한 groups 클레임을 플랫폼이
  읽은 그대로이고 정렬해 둔다. groups 클레임이 없는 토큰만 본 사람은 None 이다(모른다). 그런
  토큰은 저장된 그룹을 건드리지 않는다. 소속 줄(`_oidc_principal`)과 같은 규칙이다.

**쓰기.** `iat` 가 저장된 것보다 1분 이상 크면 확인을 적고(`confirmed_at`, `token_life`, 이름,
이메일, 그룹), `iat` 가 저장된 것과 같거나 그보다 1분이 안 되게 크면 그룹이 바뀌었을 때만 그룹을
적는다. 조건부 쓰기라 `confirmed_at` 은 뒤로 가지 않는다. `iat` 가 없는 토큰은 아무것도 적지
않는다.

**매핑의 리비전.** `mapping_rev` 는 이 사람의 okta_group 소속 줄을 마지막으로 다시 푼 Group
Mapping 의 리비전이다(`store/memberships.py` `MappingState`). 매핑이 바뀌면 이보다 리비전이 낮은
사람의 줄을 그룹으로 다시 푼다. 기록을 만들 때 0 이다. 그룹을 모르는 기록(`groups` 가 None)은
다시 풀지 않고 기록 없는 사람처럼 빠진 테넌트의 줄만 잃는다.

**지우지 않는다.** 확인 뒤 지금의 긴 한도(`DAEGAM_IDP_CONFIRM_SECONDS`)와 30일이 지나면 `groups` 와
`email` 을 비우고(None, 모른다) `cleared_at` 을 찍는다. 기록 자체는 남겨 기록이 있었던 사람과 한
번도 없었던 사람을 가른다. 업그레이드 뒤 아직 요청하지 않은 사람(기록 없음)의 실행은 지금처럼 Agent
의 이름으로 돌고, 기록이 있었던 사람의 실행은 한도로 멈춘다. 그 사람이 다시 확인되면 비운 것이
돌아온다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, from_epoch, utcnow

IDP_PEOPLE = "idp_people"
# 같은 사람의 확인을 다시 적는 `iat` 의 간격. 사람이 요청할 때마다 쓰지 않으려는 것이다
CONFIRM_STEP = timedelta(minutes=1)
# 긴 한도가 지난 뒤 그룹과 이메일을 비우기까지
CLEAR_AFTER = timedelta(days=30)


class IdpPerson(Document):
    id: str
    username: str = ""
    email: str | None = None
    groups: list[str] | None = None
    confirmed_at: UtcDatetime
    token_life: int
    cleared_at: UtcDatetime | None = None
    mapping_rev: int = 0
    updated_at: UtcDatetime

    def confirmed_since(self, oldest: datetime) -> bool:
        """oldest 이후에 IdP 가 이 사람을 보증했는가. 비운 기록은 아니다."""
        return self.cleared_at is None and self.confirmed_at >= oldest


def _groups(groups: Iterable[str] | None) -> list[str] | None:
    return None if groups is None else sorted(set(groups))


class MongoIdpPeopleStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[IDP_PEOPLE]

    async def init_schema(self) -> None:
        # 비우기가 읽는다. TTL 이 아니다. 기록은 지우지 않는다
        await self.coll.create_index([("confirmed_at", ASCENDING)], name="ix_confirmed_at")
        # 매핑이 바뀐 뒤 다시 풀 사람을 찾는다
        await self.coll.create_index([("mapping_rev", ASCENDING)], name="ix_mapping_rev")

    async def get(self, sub: str) -> IdpPerson | None:
        doc = await self.coll.find_one({"_id": sub})
        return IdpPerson.from_doc(doc) if doc is not None else None

    async def get_many(self, subs: Iterable[str]) -> dict[str, IdpPerson]:
        """있는 기록만 sub → 기록이다."""
        cursor = self.coll.find({"_id": {"$in": list(subs)}})
        return {d["_id"]: IdpPerson.from_doc(d) async for d in cursor}

    async def largest_token_life(self, since: datetime) -> int | None:
        """since 이후에 확인된 기록 가운데 가장 긴 IdP 액세스 토큰 수명(초). 없으면 None 이다.
        발급자 전환의 상태가 대화형 한도와 견준다 (P53b 6.3·6.9절)."""
        cursor = (
            self.coll.find({"confirmed_at": {"$gte": since}}, {"token_life": 1})
            .sort("token_life", -1)
            .limit(1)
        )
        found = [d async for d in cursor]
        return int(found[0]["token_life"]) if found else None

    async def stale(self, revision: int, limit: int) -> list[str]:
        """소속 줄을 revision 보다 낮은 매핑으로 푼 기록의 sub. limit 개까지다. 부르는 쪽은 기록을
        다시 읽어 지금의 그룹으로 푼다."""
        cursor = self.coll.find({"mapping_rev": {"$lt": revision}}, {"_id": 1}).limit(limit)
        return [d["_id"] async for d in cursor]

    async def stamp(self, sub: str, revision: int) -> None:
        """sub 의 줄을 revision 의 매핑으로 다시 풀었다. 리비전은 뒤로 가지 않는다."""
        await self.coll.update_one({"_id": sub}, {"$max": {"mapping_rev": revision}})

    async def observe(
        self,
        sub: str,
        *,
        iat: int | None,
        exp: int | None,
        username: str,
        email: str | None,
        groups: list[str] | None,
    ) -> None:
        """검증한 IdP 액세스 토큰 하나를 적는다. groups 가 None 이면 그 토큰에 groups 클레임이
        없었다. 모듈 머리글의 쓰기 규칙을 따른다."""
        if iat is None:
            return
        issued = from_epoch(iat)
        groups = _groups(groups)
        stored = await self.coll.find_one({"_id": sub}, {"confirmed_at": 1, "groups": 1})
        if stored is None or stored["confirmed_at"] + CONFIRM_STEP <= issued:
            fields: dict[str, object] = {
                "confirmed_at": issued,
                "token_life": max(0, exp - iat) if exp is not None else 0,
                "username": username,
            }
            if email:
                fields["email"] = email
            if groups is not None:
                fields["groups"] = groups
            await self._confirm(sub, issued, fields)
        elif groups is not None and groups != stored.get("groups"):
            # 더 오래된 토큰의 그룹으로 되돌리지 않는다
            await self.coll.update_one(
                {"_id": sub, "confirmed_at": {"$lte": issued}},
                {"$set": {"groups": groups, "updated_at": utcnow()}},
            )

    async def _confirm(self, sub: str, issued: datetime, fields: dict[str, object]) -> None:
        """issued 의 확인(fields)을 적는다. 조건이 `confirmed_at` 을 보므로 더 새로운 확인을 덮지
        않고, 그 경합에서 진 쪽은 아무것도 바꾸지 않는다. 새 기록은 그룹을 모르는 채(fields 에
        그룹이 없으면)와 매핑 리비전 0 으로 시작한다."""
        fields["updated_at"] = utcnow()
        on_insert: dict[str, object] = {"mapping_rev": 0}
        if "groups" not in fields:
            on_insert["groups"] = None
        update = {"$set": fields, "$unset": {"cleared_at": ""}, "$setOnInsert": on_insert}
        try:
            await self.coll.update_one(
                {"_id": sub, "confirmed_at": {"$lte": issued - CONFIRM_STEP}}, update, upsert=True
            )
        except DuplicateKeyError:
            pass

    async def clear_expired(self, now: datetime, *, long_bound: timedelta) -> dict[str, list[str]]:
        """리컨실 단계. 마지막 확인에서 지금의 긴 한도와 30일이 지난 기록의 그룹과 이메일을 비운다.
        한도를 늘리면 늘린 한도로 센다. 결과 키 `cleared` 는 비운 기록의 sub 다."""
        query = {"confirmed_at": {"$lte": now - long_bound - CLEAR_AFTER}, "cleared_at": None}
        subs = [d["_id"] async for d in self.coll.find(query, {"_id": 1})]
        if subs:
            await self.coll.update_many(
                {**query, "_id": {"$in": subs}},
                {"$set": {"groups": None, "email": None, "cleared_at": now, "updated_at": now}},
            )
        return {"cleared": subs} if subs else {}
