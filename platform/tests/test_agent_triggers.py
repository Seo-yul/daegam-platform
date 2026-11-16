"""웹훅 트리거의 Mongo 스토어 (P41 4절). 테스트 Mongo 가 있어야 한다. 소스가 함께 쓰는 갈래는
tests/test_agent_runs.py 가 스케줄로 본다."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daegam.store.agent_runs import ActsAs, MongoAgentRunStore
from daegam.store.agent_triggers import AgentTrigger, MongoAgentTriggerStore
from daegam.store.base import new_id, utcnow
from daegam.store.hook_secrets import MongoHookSecretStore
from tests.helpers.aio import run_stores

NOW = utcnow().replace(second=0, microsecond=0)
STORES = [MongoAgentRunStore, MongoAgentTriggerStore]


def trigger_of(**fields: Any) -> AgentTrigger:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant="default",
        agent_id="ag-1",
        acts_as=ActsAs(sub="alice"),
        revision=1,
        name="deploys",
        template="{{payload}}",
        secret_fp="fp-1",
        expires_at=NOW + timedelta(days=90),
        created_at=NOW,
        updated_at=NOW,
    )
    return AgentTrigger(**{**base, **fields})


def test_a_trigger_is_found_by_its_id_alone_and_paused_like_any_source(settings):
    async def body(runs, triggers):
        mine, bobs = trigger_of(), trigger_of(acts_as=ActsAs(sub="bob"), tenant="other")
        for t in (mine, bobs):
            await triggers.insert(t)
        found = await triggers.get_by_id(bobs.id)
        paused = await triggers.pause_bound("alice", reason="r", cause="role", now=NOW)
        return found.tenant, paused, await triggers.list_bound("bob")

    tenant, paused, bound = run_stores(settings, STORES, body)
    assert tenant == "other" and list(paused.values()) == ["default"]
    assert [t.acts_as.sub for t in bound] == ["bob"]


def test_the_hook_secret_ledger_lists_due_rows_and_forgets_only_unchanged_ones(settings):
    fresh, swept, old = new_id(), new_id(), new_id()

    async def body(ledger):
        for agent_id in (fresh, swept, old):
            await ledger.note_write(agent_id, NOW)
        await ledger.mark_swept(swept, NOW)
        await ledger.mark_swept(old, NOW - timedelta(hours=2))
        due = [r.id for r in await ledger.due(NOW - timedelta(hours=1), NOW, 1000)]
        # 막 쓴 행은 아직 때가 아니다
        young = [r.id for r in await ledger.due(NOW, NOW - timedelta(seconds=1), 1000)]
        row = await ledger.get(old)
        # 그사이 쓰기를 시작했으면 지우지 않는다
        await ledger.note_write(old, NOW + timedelta(seconds=1))
        kept = await ledger.forget(old, row.written_at)
        gone = await ledger.forget(fresh, (await ledger.get(fresh)).written_at)
        return due, young, kept, gone, await ledger.get(old), await ledger.get(fresh)

    due, young, kept, gone, old_row, fresh_row = run_stores(settings, [MongoHookSecretStore], body)
    assert not {fresh, swept, old} & set(young)
    assert fresh in due and old in due and swept not in due
    # 아직 쓴 적이 없는 행이 먼저다
    assert due.index(fresh) < due.index(old)
    assert (kept, gone) == (False, True)
    assert old_row is not None and fresh_row is None
