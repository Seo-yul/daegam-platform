"""Teams 채널의 기록 (P47 6.2절·6.3절).

channel_links {_id, sub, username, tv, teams: {app_id, entra_tenant_id, aad_object_id, from_id,
               conversation_id, service_url, teams_name, address}, language, linked_at, ends_at,
               last_turn_at, replaced_name?, replaced_at?}
channel_link_codes {_id: sub, code_hmac, username, tv, created_at, expires_at, pending?}
channel_link_notices {_id: sub, teams_name, at, expires_at}
channel_conversations {_id: conversation id, noticed_at, updated_at}
channel_turns {_id, conversation_id, from_id, activity_id, kind, created_at, expires_at?}

- **연결.** Teams 사용자 하나와 사람 하나다. 두 유일 인덱스가 사람마다 Teams 사용자 하나, Teams
  사용자마다 사람 하나를 지킨다. Teams 사용자는 Entra 테넌트와 `aadObjectId` 로 가른다. 새 연결은 그
  사람의 것과 그 Teams 사용자의 것을 지우고 들어간다(`replace`). `ends_at` 은 연결할 때 정한 끝이고
  쓰여도 밀리지 않는다.
- **연결 코드.** 사람마다 열린 코드 하나다(`_id` 가 sub). 평문은 만든 응답에만 있고 여기는 플랫폼
  서명 키에서 만든 키의 HMAC 이다. 코드가 Teams 에서 쓰였는데 주소가 사람의 이메일과 다르면 그 Teams
  계정을 `pending` 에 두고 사람이 계정 메뉴에서 확인할 때까지 기다린다. TTL 이 코드의 기한에 지운다.
- **알림.** 그 사람이 연결한 Teams 계정을 다른 사람이 가져가 연결이 끊겼다는 표시다. 계정 메뉴가
  보여 주고, 그 사람의 다음 연결이나 연결 끊기가 지우며, TTL 이 30일 뒤에 지운다.
- **대화.** 아직 연결이 없는 대화에 고정 줄을 보낸 때다. 10분에 한 번만 보내는 조건부 쓰기가
  레플리카 여럿에서도 한 번을 지킨다. 봇을 지우면(`installationUpdate` remove) 지운다. 2단계(A2)가
  고른 Agent 와 세션을 더한다.
- **턴.** 받은 메시지 활동의 기록이다. 대화, `from.id`, 활동 id 의 유일 인덱스가 다시 온 활동을 한
  번만 받게 한다. 명령의 기록(`kind: command`)은 `expires_at` 에 TTL 이 지운다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index, new_id

CHANNEL_LINKS = "channel_links"
CHANNEL_LINK_CODES = "channel_link_codes"
CHANNEL_LINK_NOTICES = "channel_link_notices"
CHANNEL_CONVERSATIONS = "channel_conversations"
CHANNEL_TURNS = "channel_turns"
# 명령의 받은 기록을 두는 동안. Connector 가 같은 활동을 다시 보내는 동안보다 넉넉하다
COMMAND_RECORD_TTL = timedelta(days=1)
COMMAND = "command"
# 가져간 연결의 표시를 두는 동안
NOTICE_TTL = timedelta(days=30)
# 봇의 언어 (6.3절). 한국어가 기본이다
Language = Literal["ko", "en"]


class TeamsAccount(BaseModel):
    """연결이 든 Teams 쪽 값. 앞의 넷(테넌트, `aadObjectId`, `from.id`, 대화)이 연결의 네 값이고
    `service_url` 은 그 대화의 검증한 활동이 실은 주소다."""

    app_id: str
    entra_tenant_id: str
    aad_object_id: str
    from_id: str
    conversation_id: str
    service_url: str
    teams_name: str = ""
    address: str = ""


class ChannelLink(Document):
    id: str
    sub: str
    username: str
    # local 모드의 비밀번호 판(`token_version`). oidc 는 0 이다
    tv: int = 0
    teams: TeamsAccount
    language: Language = "ko"
    linked_at: UtcDatetime
    ends_at: UtcDatetime
    # 마지막 Teams 턴. 90일 동안 없으면 연결이 끊긴다 (6.3절)
    last_turn_at: UtcDatetime
    # 이 연결이 바꾼 앞 연결의 Teams 이름과 그때. 계정 메뉴가 보여 준다
    replaced_name: str | None = None
    replaced_at: UtcDatetime | None = None


class ChannelLinkCode(Document):
    id: str  # 코드를 만든 사람의 sub
    # 코드의 HMAC-SHA256 (hex)
    code_hmac: str
    username: str
    tv: int = 0
    created_at: UtcDatetime
    expires_at: UtcDatetime
    # 코드를 보낸 Teams 계정. 주소가 사람의 이메일과 달라 사람의 확인을 기다린다
    pending: TeamsAccount | None = None


def _teams_query(entra_tenant_id: str, aad_object_id: str) -> dict[str, Any]:
    return {"teams.entra_tenant_id": entra_tenant_id, "teams.aad_object_id": aad_object_id}


class LinkNotice(Document):
    """다른 사람이 가져간 연결의 표시. `teams_name` 은 가져간 Teams 계정의 이름이다."""

    id: str  # 연결을 잃은 사람의 sub
    teams_name: str
    at: UtcDatetime
    expires_at: UtcDatetime


class MongoChannelStore:
    def __init__(self, db: AsyncDatabase):
        self.links = db[CHANNEL_LINKS]
        self.codes = db[CHANNEL_LINK_CODES]
        self.notices = db[CHANNEL_LINK_NOTICES]
        self.conversations = db[CHANNEL_CONVERSATIONS]
        self.turns = db[CHANNEL_TURNS]

    async def init_schema(self) -> None:
        await self.links.create_index([("sub", ASCENDING)], unique=True, name="uq_sub")
        await self.links.create_index(
            [("teams.entra_tenant_id", ASCENDING), ("teams.aad_object_id", ASCENDING)],
            unique=True,
            name="uq_teams_user",
        )
        await self.links.create_index([("ends_at", ASCENDING)], name="ix_ends_at")
        await self.links.create_index([("last_turn_at", ASCENDING)], name="ix_last_turn_at")
        await self.codes.create_index([("code_hmac", ASCENDING)], unique=True, name="uq_code_hmac")
        await ensure_ttl_index(self.codes, "expires_at", 0, "ttl_expires_at")
        await ensure_ttl_index(self.notices, "expires_at", 0, "ttl_expires_at")
        await self.turns.create_index(
            [("conversation_id", ASCENDING), ("from_id", ASCENDING), ("activity_id", ASCENDING)],
            unique=True,
            name="uq_activity",
        )
        await ensure_ttl_index(self.turns, "expires_at", 0, "ttl_expires_at")

    # -- 연결 --------------------------------------------------------------------------------

    async def link_for_person(self, sub: str) -> ChannelLink | None:
        doc = await self.links.find_one({"sub": sub})
        return ChannelLink.from_doc(doc) if doc else None

    async def link_for_teams(self, entra_tenant_id: str, aad_object_id: str) -> ChannelLink | None:
        doc = await self.links.find_one(_teams_query(entra_tenant_id, aad_object_id))
        return ChannelLink.from_doc(doc) if doc else None

    async def list_links(self) -> list[ChannelLink]:
        return [ChannelLink.from_doc(d) async for d in self.links.find({}).sort("linked_at", -1)]

    async def replace_link(self, link: ChannelLink) -> list[ChannelLink]:
        """그 사람의 연결과 그 Teams 사용자의 연결을 지우고 link 를 넣는다. 지운 연결을 돌려준다.
        두 레플리카가 겹쳐 유일 인덱스에 걸리면 한 번 더 한다."""
        query = {
            "$or": [
                {"sub": link.sub},
                _teams_query(link.teams.entra_tenant_id, link.teams.aad_object_id),
            ]
        }
        removed: list[ChannelLink] = []
        for attempt in range(2):
            old = [ChannelLink.from_doc(d) async for d in self.links.find(query)]
            if old:
                await self.links.delete_many({"_id": {"$in": [o.id for o in old]}})
            removed += old
            try:
                await self.links.insert_one(link.to_doc())
                return removed
            except DuplicateKeyError:
                if attempt:
                    raise
        return removed

    async def delete_link(self, link_id: str) -> ChannelLink | None:
        doc = await self.links.find_one_and_delete({"_id": link_id})
        return ChannelLink.from_doc(doc) if doc else None

    async def delete_link_for_person(self, sub: str) -> ChannelLink | None:
        doc = await self.links.find_one_and_delete({"sub": sub})
        return ChannelLink.from_doc(doc) if doc else None

    async def delete_links(self, ids: list[str]) -> int:
        if not ids:
            return 0
        return (await self.links.delete_many({"_id": {"$in": ids}})).deleted_count

    async def set_language(self, sub: str, language: str) -> ChannelLink | None:
        doc = await self.links.find_one_and_update(
            {"sub": sub}, {"$set": {"language": language}}, return_document=ReturnDocument.AFTER
        )
        return ChannelLink.from_doc(doc) if doc else None

    async def due_links(self, now: datetime, idle_before: datetime) -> list[ChannelLink]:
        """끝이 지났거나 마지막 Teams 턴이 idle_before 보다 오랜 연결."""
        query = {"$or": [{"ends_at": {"$lte": now}}, {"last_turn_at": {"$lte": idle_before}}]}
        return [ChannelLink.from_doc(d) async for d in self.links.find(query)]

    # -- 연결 코드 ------------------------------------------------------------------------------

    async def put_code(self, code: ChannelLinkCode) -> None:
        """그 사람의 열린 코드를 이것으로 바꾼다. 앞 코드와 그 확인 대기도 사라진다."""
        await self.codes.replace_one({"_id": code.id}, code.to_doc(), upsert=True)

    async def code_for(self, sub: str, now: datetime) -> ChannelLinkCode | None:
        doc = await self.codes.find_one({"_id": sub, "expires_at": {"$gt": now}})
        return ChannelLinkCode.from_doc(doc) if doc else None

    async def take_code(self, digest: str, now: datetime) -> ChannelLinkCode | None:
        """확인 대기가 없는 살아 있는 코드를 지우고 돌려준다. 코드는 한 번만 쓰인다."""
        doc = await self.codes.find_one_and_delete(
            {"code_hmac": digest, "expires_at": {"$gt": now}, "pending": None}
        )
        return ChannelLinkCode.from_doc(doc) if doc else None

    async def hold_code(self, code: ChannelLinkCode) -> None:
        """`take_code` 로 꺼낸 코드를 확인 대기와 함께 되돌려 둔다. 그사이 그 사람이 새 코드를
        만들었으면 새 코드가 이긴다."""
        try:
            await self.codes.insert_one(code.to_doc())
        except DuplicateKeyError:
            return

    async def take_pending(self, sub: str, now: datetime) -> ChannelLinkCode | None:
        """확인을 기다리는 살아 있는 코드를 지우고 돌려준다."""
        doc = await self.codes.find_one_and_delete(
            {"_id": sub, "expires_at": {"$gt": now}, "pending": {"$ne": None}}
        )
        return ChannelLinkCode.from_doc(doc) if doc else None

    async def delete_code(self, sub: str) -> None:
        await self.codes.delete_one({"_id": sub})

    async def delete_all_codes(self) -> int:
        return (await self.codes.delete_many({})).deleted_count

    # -- 가져간 연결의 표시 ------------------------------------------------------------------------

    async def put_notice(self, sub: str, teams_name: str, now: datetime) -> None:
        notice = LinkNotice(id=sub, teams_name=teams_name, at=now, expires_at=now + NOTICE_TTL)
        await self.notices.replace_one({"_id": sub}, notice.to_doc(), upsert=True)

    async def notice_for(self, sub: str, now: datetime) -> LinkNotice | None:
        doc = await self.notices.find_one({"_id": sub, "expires_at": {"$gt": now}})
        return LinkNotice.from_doc(doc) if doc else None

    async def delete_notice(self, sub: str) -> None:
        await self.notices.delete_one({"_id": sub})

    # -- 대화와 받은 활동 ------------------------------------------------------------------------

    async def claim_notice(self, conversation_id: str, now: datetime, every: timedelta) -> bool:
        """연결 없는 대화에 고정 줄을 보내도 되는가. 마지막으로 보낸 때가 every 안이면 False 이고,
        아니면 지금을 찍고 True 다. 두 레플리카가 겹치면 한쪽만 True 다."""
        try:
            doc = await self.conversations.find_one_and_update(
                {"_id": conversation_id, "noticed_at": {"$not": {"$gt": now - every}}},
                {"$set": {"noticed_at": now, "updated_at": now}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return False
        return doc is not None

    async def forget_conversation(self, conversation_id: str) -> None:
        await self.conversations.delete_one({"_id": conversation_id})

    async def record_activity(
        self, conversation_id: str, from_id: str, activity_id: str, now: datetime
    ) -> bool:
        """받은 메시지 활동을 적는다. 같은 활동이 이미 있으면 False 다."""
        try:
            await self.turns.insert_one(
                {
                    "_id": new_id(),
                    "conversation_id": conversation_id,
                    "from_id": from_id,
                    "activity_id": activity_id,
                    "kind": COMMAND,
                    "created_at": now,
                    "expires_at": now + COMMAND_RECORD_TTL,
                }
            )
        except DuplicateKeyError:
            return False
        return True


__all__ = [
    "CHANNEL_CONVERSATIONS",
    "CHANNEL_LINKS",
    "CHANNEL_LINK_CODES",
    "CHANNEL_LINK_NOTICES",
    "CHANNEL_TURNS",
    "ChannelLink",
    "ChannelLinkCode",
    "Language",
    "LinkNotice",
    "MongoChannelStore",
    "TeamsAccount",
]
