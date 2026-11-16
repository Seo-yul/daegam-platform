"""local 모드의 사용자 (D40).

users {_id, username, email, display_name, password_hash, disabled, must_change_password,
       token_version, created_at, updated_at, last_login_at}

`id` 는 토큰의 `sub` 이고 memberships.sub 과 같은 값이다. 이름을 바꿔도 변하지 않는 UUID 다.
`username` 은 소문자 DNS 라벨 꼴로 유일하다. `token_version` 은 비밀번호를 바꾸거나 계정을 막을 때
올라가고, 토큰의 `tv` 와 다르면 그 토큰은 죽는다. 비밀번호 해시는 여기 저장되지만 API 응답에는
나가지 않는다(`UserOut` 이 따로 있다).
"""

from __future__ import annotations

import re
from collections.abc import Collection

from pydantic import Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, new_id, utcnow

USERS = "users"
USERNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")


def normalize_username(raw: str) -> str:
    """사람이 적은 사용자 이름을 저장 꼴로 맞춘다. 로그인 잠금 키와 감사에 남는 이름이 같은 규칙을
    써야 해서 여기 하나만 둔다."""
    return raw.strip().lower()


class User(Document):
    id: str = Field(default_factory=new_id)
    username: str
    email: str | None = None
    display_name: str | None = None
    password_hash: str
    disabled: bool = False
    must_change_password: bool = False
    token_version: int = 0
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    last_login_at: UtcDatetime | None = None


class MongoUserStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[USERS]

    async def init_schema(self) -> None:
        await self.coll.create_index([("username", ASCENDING)], unique=True, name="uq_username")

    async def count(self) -> int:
        return await self.coll.count_documents({})

    async def create(self, user: User) -> User:
        """username 이 겹치면 pymongo DuplicateKeyError 가 그대로 올라온다.

        호출자가 409 로 바꾼다."""
        await self.coll.insert_one(user.to_doc())
        return user

    async def get(self, user_id: str) -> User | None:
        doc = await self.coll.find_one({"_id": user_id})
        return User.from_doc(doc) if doc else None

    async def get_many(self, ids: Collection[str]) -> dict[str, User]:
        """id → 사용자. 없는 id 는 결과에 없다. 사용량 보고가 줄마다 이름을 붙일 때 한 번에
        읽는다."""
        if not ids:
            return {}
        cursor = self.coll.find({"_id": {"$in": list(ids)}})
        return {d["_id"]: User.from_doc(d) for d in await cursor.to_list()}

    async def get_by_username(self, username: str) -> User | None:
        doc = await self.coll.find_one({"username": normalize_username(username)})
        return User.from_doc(doc) if doc else None

    async def list(self) -> list[User]:
        cursor = self.coll.find({}).sort("username", ASCENDING)
        return [User.from_doc(d) async for d in cursor]

    async def update(self, user_id: str, changes: dict) -> User | None:
        doc = await self.coll.find_one_and_update(
            {"_id": user_id},
            {"$set": {**changes, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return User.from_doc(doc) if doc else None

    async def set_password(
        self, user_id: str, password_hash: str, *, must_change: bool
    ) -> User | None:
        """비밀번호를 바꾸면 token_version 이 올라 이전 토큰이 모두 죽는다."""
        doc = await self.coll.find_one_and_update(
            {"_id": user_id},
            {
                "$set": {
                    "password_hash": password_hash,
                    "must_change_password": must_change,
                    "updated_at": utcnow(),
                },
                "$inc": {"token_version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return User.from_doc(doc) if doc else None

    async def set_disabled(self, user_id: str, disabled: bool) -> User | None:
        """막으면 token_version 도 올려 살아 있는 토큰을 즉시 끊는다. 다시 열 때는 올리지 않는다."""
        update: dict = {"$set": {"disabled": disabled, "updated_at": utcnow()}}
        if disabled:
            update["$inc"] = {"token_version": 1}
        doc = await self.coll.find_one_and_update(
            {"_id": user_id}, update, return_document=ReturnDocument.AFTER
        )
        return User.from_doc(doc) if doc else None

    async def record_login(self, user_id: str) -> None:
        await self.coll.update_one({"_id": user_id}, {"$set": {"last_login_at": utcnow()}})

    async def delete(self, user_id: str) -> bool:
        r = await self.coll.delete_one({"_id": user_id})
        return r.deleted_count == 1
