"""사람의 저장과 편집 클레임의 공통 조건 (P68 2절, `daegam.store.base`).

`seq` 는 사람의 저장마다 하나 오르고, 서버가 읽은 값일 때만 다음 저장이 맞는다. 비밀을 먼저 쓰는
저장은 클레임을 잡고, 살아 있는 동안 다른 저장은 맞지 않는다. 마지막 저장은 클레임 id 와 클레임이
둔 `seq` 가 모두 맞을 때만 지나간다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ConfigDict

from daegam.store.base import (
    Document,
    SeqModel,
    UtcDatetime,
    finish_edit_claim,
    save_by_seq,
    set_fields_guarded,
    set_fields_where,
    take_edit_claim,
    utcnow,
)
from tests.helpers.aio import run_db


class Item(Document):
    id: str
    name: str = ""
    verified_at: str | None = None


async def _read(coll) -> Item:
    return Item.from_doc(await coll.find_one({"_id": "i1"}))


class Prefs(SeqModel):
    """`upsert_by_seq` 를 쓰는 싱글턴 설정 문서의 꼴."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    size: int = 0
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None


def test_seq_is_store_side_and_old_documents_read_as_zero(settings):
    async def go(db):
        coll = db["p68_seq"]
        await coll.delete_many({})
        await coll.insert_one({"_id": "i1", "name": "a"})
        old = await _read(coll)
        assert old._seq == 0 and "seq" not in old.model_dump()
        # 필드가 없는 옛 문서와 0 이 든 문서 모두 0 을 읽은 저장과 맞는다
        assert await save_by_seq(coll, old, {"name": "b"}) and old._seq == 1
        stale = Item.from_doc({"_id": "i1", "name": "a"})
        assert not await save_by_seq(coll, stale, {"name": "c"})
        fresh = await _read(coll)
        assert fresh.name == "b" and fresh._seq == 1
        await coll.insert_one({"_id": "i2", "seq": 0})
        assert await set_fields_guarded(coll, "i2", {"name": "z"}, expect_seq=0, raise_seq=True)

    run_db(settings, go)


def test_a_live_claim_shuts_out_other_saves_and_the_final_save_needs_its_seq(settings):
    async def go(db):
        coll = db["p68_claim"]
        await coll.delete_many({})
        await coll.insert_one({"_id": "i1", "name": "a", "verified_at": "t"})
        mine = await _read(coll)
        other = await _read(coll)
        claim = await take_edit_claim(coll, mine, timedelta(minutes=1), {"verified_at": None})
        assert claim is not None and mine._seq == 1
        # 클레임을 잡은 뒤 읽은 저장도, 다른 클레임도 맞지 않는다
        after = await _read(coll)
        assert after.verified_at is None and after._seq == 1
        assert not await save_by_seq(coll, after, {"name": "x"})
        assert await take_edit_claim(coll, after, timedelta(minutes=1)) is None
        assert not await save_by_seq(coll, other, {"name": "x"})
        assert await finish_edit_claim(coll, mine, claim, {"name": "secret-changed"})
        done = await coll.find_one({"_id": "i1"})
        assert done["seq"] == 2 and "claim" not in done and done["name"] == "secret-changed"

    run_db(settings, go)


def test_a_late_final_save_misses_once_another_save_went_through(settings):
    async def go(db):
        coll = db["p68_late"]
        await coll.delete_many({})
        await coll.insert_one({"_id": "i1", "name": "a"})
        mine = await _read(coll)
        claim = await take_edit_claim(coll, mine, timedelta(minutes=1))
        # 기한이 지났다
        await coll.update_one({"_id": "i1"}, {"$set": {"claim.until": utcnow() - timedelta(1)}})
        other = await _read(coll)
        assert await save_by_seq(coll, other, {"name": "other"})
        assert not await finish_edit_claim(coll, mine, claim, {"name": "late"})
        assert (await _read(coll)).name == "other"

    run_db(settings, go)


def test_an_empty_write_is_refused():
    """쓸 것이 없는 조건부 저장(빈 fields, seq 올리기·지우기 없음)은 ValueError 다."""
    with pytest.raises(ValueError, match="nothing to write"):
        asyncio.run(set_fields_guarded(None, "a", {}))
    with pytest.raises(ValueError, match="nothing to write"):
        asyncio.run(set_fields_where(None, "a", {}))
