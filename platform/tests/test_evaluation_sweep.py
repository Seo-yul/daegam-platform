"""P45 B2: 평가 정리와 계정 사건의 동의 거두기 (8절, 16절).

정리는 한 시간에 한 번 돌며 평가가 없거나 동의하지 않거나 평가한 사람이 사라진 복사본, 없는 Agent
의 세트와 사례, 세트가 없는 사례를 지우고 세트의 사례 수를 다시 센다. 사람이 사라진 것은 local 은
사용자 문서, oidc 는 사람 기록과 `cleared_at` 으로 보고 header 는 보지 않는다. 계정 삭제와 소속을
잃은 일은 동의를 거두고, 비밀번호 바꾸기와 계정 막기는 거두지 않는다. Mongo 는 진짜다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import anyio
import pytest

from daegam.services.evaluation.sweep import EvaluationSweep
from daegam.store.agent_feedback import AgentFeedback, MongoAgentFeedbackStore
from daegam.store.agents import MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.evaluation import (
    CaseContent,
    EvaluationSet,
    MongoEvaluationStore,
)
from daegam.store.idp_people import IdpPerson, MongoIdpPeopleStore
from daegam.store.personal_tokens import DELETED, DISABLED, MEMBERSHIP, PASSWORD
from daegam.store.users import MongoUserStore, User
from daegam.wiring import _lost_access_hook
from tests.helpers.aio import run_stores
from tests.helpers.docs import TENANT, agent_doc
from tests.helpers.evaluation import case_doc

STORES = (
    MongoEvaluationStore,
    MongoAgentFeedbackStore,
    MongoAgentStore,
    MongoUserStore,
    MongoIdpPeopleStore,
)
CONTENT = CaseContent(messages=["질문"])


def _rating(rid: str, sub: str, *, consent: bool = True) -> AgentFeedback:
    now = utcnow()
    return AgentFeedback(
        id=rid,
        tenant=TENANT,
        agent_id="ag-1",
        session_id="s1",
        turn_id=f"t-{rid}",
        turn=1,
        user_sub=sub,
        rating="up",
        evaluation_use=consent,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=180),
    )


def _case(cid: str, set_id: str, *, agent_id: str = "ag-1", rating: str | None = None):
    return case_doc(cid, set_id, agent_id, CONTENT, tenant=TENANT, rating=rating, by="u")


async def _seed_set(
    store: MongoEvaluationStore, set_id: str, agent_id: str, count: int, *, age: float = 120
) -> None:
    """age 초 전에 마지막으로 바뀐 세트다. 다시 세기는 1분이 지난 세트만 본다."""
    item = EvaluationSet(
        id=set_id,
        tenant=TENANT,
        agent_id=agent_id,
        name="n",
        case_count=count,
        created_by="u",
        updated_by="u",
        updated_at=utcnow() - timedelta(seconds=age),
    )
    await store.create_set(item)


def _sweep(stores: tuple[Any, ...], mode: str) -> EvaluationSweep:
    evaluation, ratings, agents, users, people = stores
    return EvaluationSweep(evaluation, ratings, agents, users=users, people=people, auth_mode=mode)


@pytest.mark.parametrize("mode", ["local", "oidc", "header"])
def test_the_sweep_deletes_copies_whose_consent_ended(settings, mode):
    async def go(*stores: Any) -> None:
        evaluation, ratings, agents, users, people = stores
        await agents.create(agent_doc("ag-1"))
        await _seed_set(evaluation, "s", "ag-1", 6)
        # 동의하는 사람, 동의를 끈 사람, 지운 평가, 사라진 사람, 비운 사람 기록, 손으로 쓴 사례
        for rid, sub, consent in (
            ("r-ok", "alive", True),
            ("r-off", "alive", False),
            ("r-gone-person", "gone", True),
            ("r-cleared", "cleared", True),
        ):
            await ratings.upsert(_rating(rid, sub, consent=consent))
        await users.create(User(id="alive", username="alive", password_hash="x"))
        await users.create(User(id="cleared", username="cleared", password_hash="x"))
        now = utcnow()
        await people.coll.insert_one(
            IdpPerson(id="alive", confirmed_at=now, token_life=300, updated_at=now).to_doc()
        )
        await people.coll.insert_one(
            IdpPerson(
                id="cleared", confirmed_at=now, token_life=300, updated_at=now, cleared_at=now
            ).to_doc()
        )
        for cid, rating in (
            ("c-ok", "r-ok"),
            ("c-off", "r-off"),
            ("c-no-rating", "r-missing"),
            ("c-gone", "r-gone-person"),
            ("c-cleared", "r-cleared"),
            ("c-hand", None),
        ):
            await evaluation.insert_case(_case(cid, "s", rating=rating))

        result = await _sweep(stores, mode).run(now)
        expected = {"c-off", "c-no-rating"}
        if mode == "local":
            expected |= {"c-gone"}
        if mode == "oidc":
            expected |= {"c-gone", "c-cleared"}
        assert set(result["copies"]) == expected
        left = {d["_id"] async for d in evaluation.cases.find({})}
        assert left == {"c-ok", "c-off", "c-no-rating", "c-gone", "c-cleared", "c-hand"} - expected
        stored = await evaluation.get_set(TENANT, "ag-1", "s")
        assert stored is not None and stored.case_count == len(left)

    run_stores(settings, STORES, go)


def test_the_sweep_runs_hourly_and_clears_what_deletes_left(settings):
    async def go(*stores: Any) -> None:
        evaluation, _ratings, agents, _users, _people = stores
        await agents.create(agent_doc("ag-1"))
        # 있는 Agent 의 세트(수가 틀렸다), 없는 Agent 의 세트, 세트 없는 사례
        await _seed_set(evaluation, "s-live", "ag-1", 5)
        await _seed_set(evaluation, "s-dead", "ag-gone", 1)
        await evaluation.insert_case(_case("c-live", "s-live"))
        await evaluation.insert_case(_case("c-dead", "s-dead", agent_id="ag-gone"))
        await evaluation.insert_case(_case("c-orphan", "s-deleted"))
        sweep = _sweep(stores, "header")
        now = utcnow()
        result = await sweep.run(now)
        assert result == {"agents": ["ag-gone"], "orphans": ["c-orphan"], "recounted": ["s-live"]}
        assert {d["_id"] async for d in evaluation.cases.find({})} == {"c-live"}
        assert {d["_id"] async for d in evaluation.sets.find({})} == {"s-live"}
        stored = await evaluation.get_set(TENANT, "ag-1", "s-live")
        assert stored is not None and stored.case_count == 1
        # 한 시간이 지나기 전에는 돌지 않는다
        await evaluation.insert_case(_case("c-orphan-2", "s-deleted"))
        assert await sweep.run(now + timedelta(minutes=59)) == {}
        assert await sweep.run(now + timedelta(hours=1)) == {"orphans": ["c-orphan-2"]}

    run_stores(settings, STORES, go)


def test_recount_skips_a_set_that_just_changed_and_a_failed_sweep_runs_again(settings):
    """방금 자리를 잡은 세트는 사례가 들어가기 전일 수 있어 다시 세지 않는다. 실패한 정리는 한
    시간을 세지 않아 다음 틱이 다시 돈다."""

    async def go(*stores: Any) -> None:
        evaluation = stores[0]
        await _seed_set(evaluation, "s-fresh", "ag-1", 3, age=5)
        await _seed_set(evaluation, "s-old", "ag-1", 3)
        now = utcnow()
        assert await evaluation.recount(now) == ["s-old"]
        fresh = await evaluation.get_set(TENANT, "ag-1", "s-fresh")
        assert fresh is not None and fresh.case_count == 3

        sweep = _sweep(stores, "header")
        recount = evaluation.recount

        async def broken(_now):
            raise RuntimeError("store down")

        evaluation.recount = broken
        with pytest.raises(RuntimeError):
            await sweep.run(now)
        assert sweep._last is None
        evaluation.recount = recount
        later = now + timedelta(minutes=1)
        assert await sweep.run(later) == {}
        assert sweep._last == later

    run_stores(settings, STORES, go)


@pytest.mark.parametrize(
    ("reason", "tenant", "withdrawn"),
    [
        (DELETED, None, True),
        (MEMBERSHIP, "t1", True),
        (PASSWORD, None, False),
        (DISABLED, None, False),
    ],
)
def test_account_events_end_consents_only_on_delete_or_lost_membership(reason, tenant, withdrawn):
    """훅의 앞 단계가 실패해도 동의 거두기는 돈다."""
    calls: list[tuple[str, str | None]] = []

    class Runs:
        async def on_lost_access(self, sub: str, reason: str, tenant: str | None) -> None:
            raise RuntimeError("runs failed")

    class Quiet:
        async def forget_person(self, sub: str, tenant: str | None) -> None: ...

        async def end_for_person(self, sub: str, tenant: str | None) -> None: ...

    class Copies:
        async def withdraw_person(self, sub: str, tenant: str | None) -> int:
            calls.append((sub, tenant))
            return 0

    class Channels:
        async def on_lost_access(self, sub: str, reason: str, tenant: str | None) -> None: ...

    hook = _lost_access_hook(Runs(), Quiet(), Quiet(), Copies(), Channels())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        anyio.run(hook, "u1", reason, tenant)
    assert calls == ([("u1", tenant)] if withdrawn else [])


def test_deleting_a_local_account_withdraws_its_consents(local_admin, mongo):
    """local 모드에서 계정을 지우면 훅이 그 사람의 동의를 거두고 복사본을 지운다."""
    r = local_admin.post("/users", json={"username": "kim", "password": "another password 1"})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    mongo["agent_feedback"].insert_one(_rating("r-kim", user_id).to_doc())
    item = EvaluationSet(
        id="s",
        tenant=TENANT,
        agent_id="ag-1",
        name="n",
        case_count=1,
        created_by="u",
        updated_by="u",
    )
    mongo["evaluation_sets"].insert_one(item.to_doc())
    mongo["evaluation_cases"].insert_one(_case("c-kim", "s", rating="r-kim").to_doc())
    r = local_admin.delete(f"/users/{user_id}")
    assert r.status_code == 200, r.text
    assert mongo["agent_feedback"].find_one({"_id": "r-kim"})["evaluation_use"] is False
    assert mongo["evaluation_cases"].find_one({"_id": "c-kim"}) is None
    assert mongo["evaluation_sets"].find_one({"_id": "s"})["case_count"] == 0
