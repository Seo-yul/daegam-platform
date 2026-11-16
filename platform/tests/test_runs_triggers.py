"""웹훅 트리거의 관리와 트리거 규칙 (P41 4·6·10절). 스토어와 Secret 을 메모리 가짜로 바꿔 DB 없이
돈다."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from daegam.auth.principal import Principal
from daegam.services.runs.errors import RunError
from daegam.services.runs.trigger_rule import unguarded_tools
from daegam.services.runs.trigger_secrets import TriggerKeys, fingerprint, new_secret
from daegam.services.runs.triggers import ROTATION_SETTLE, RunTriggers, TriggerInput
from daegam.store.agent_runs import ActsAs
from daegam.store.agents import ToolPin
from daegam.store.base import utcnow
from tests.helpers.agents import template_spec
from tests.helpers.runs import (
    LOCAL,
    WRITES,
    FakeAgent,
    FakeRuns,
    FakeTriggers,
    FakeTriggerSecrets,
    FakeUsers,
    alice,
    guarded,
    person_check,
    run_of,
    service_of,
    trigger_of,
)

DATA = TriggerInput(name="deploys", template="Deployed: {{payload./ref}}", expires_in_days=30)
ALICE = Principal(sub="alice", raw={"tv": 3, "preferred_username": "alice"})


def rig():
    triggers, secrets, runs = FakeTriggers(), FakeTriggerSecrets(), FakeRuns()
    rows = (("alice", "default", "developer"), ("bob", "default", "developer"))
    person = person_check(LOCAL, FakeUsers(alice()), *rows)
    service = service_of(runs, settings=LOCAL, person=person, triggers=triggers, secrets=secrets)
    return RunTriggers(service), triggers, secrets, runs


# -- 트리거 규칙 -----------------------------------------------------------------


def test_a_writing_tool_without_an_approval_rule_blocks_a_trigger():
    assert unguarded_tools(FakeAgent(tool_pins=WRITES)) == ["tools-calculator/Add"]
    assert unguarded_tools(guarded()) == []


def test_a_read_effect_needs_no_approval():
    reads = {"tools-calculator": ToolPin(names=["Add"], effects={"Add": "read", "Divide": "write"})}
    assert unguarded_tools(FakeAgent(tool_pins=reads)) == []


def test_unknown_tool_names_block_a_trigger():
    spec = template_spec()
    for node in spec["graph"]["nodes"]:
        if node["id"] == "tools-calculator":
            node["data"]["allow_tools"] = []
    unknown = {"tools-calculator": ToolPin(names=None)}
    assert unguarded_tools(FakeAgent(spec=spec, tool_pins=unknown)) == [
        "tools-calculator: tool names unknown"
    ]


# -- 관리 --------------------------------------------------------------------------------------


def test_creating_a_trigger_binds_its_creator_and_shows_the_secret_once():
    manager, triggers, secrets, _ = rig()
    trigger, secret = asyncio.run(manager.create(guarded(), ALICE, DATA))
    assert secret.startswith("whsec_") and trigger.secret_fp == fingerprint(secret)
    assert trigger.acts_as == ActsAs(sub="alice", username="alice", tv=3)
    assert secrets.keys[("ag-1", trigger.id)].current == secret
    assert triggers.docs[trigger.id].template == DATA.template


def test_a_trigger_on_an_agent_with_unguarded_writes_is_refused():
    manager, triggers, secrets, _ = rig()
    with pytest.raises(RunError) as e:
        asyncio.run(manager.create(FakeAgent(tool_pins=WRITES), ALICE, DATA))
    assert (e.value.status, e.value.code) == (409, "trigger_needs_approval")
    assert triggers.docs == {} and secrets.keys == {}


def test_renaming_keeps_the_binding_and_other_changes_rebind():
    manager, triggers, _, _ = rig()
    trigger = trigger_of(acts_as=ActsAs(sub="bob"))
    triggers.docs[trigger.id] = trigger
    renamed = asyncio.run(manager.update(guarded(), trigger, ALICE, name="releases"))
    assert (renamed.name, renamed.acts_as.sub) == ("releases", "bob")
    changed = asyncio.run(manager.update(guarded(), trigger, ALICE, template="{{payload}}!"))
    assert (changed.template, changed.acts_as.sub) == ("{{payload}}!", "alice")


def test_rotating_keeps_the_old_secret_for_a_day_and_rebinds():
    manager, triggers, secrets, _ = rig()
    trigger, first = asyncio.run(manager.create(guarded(), ALICE, DATA))
    rotated, second = asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    keys = secrets.keys[("ag-1", trigger.id)]
    assert (keys.current, keys.previous) == (second, first)
    assert (rotated.secret_fp, rotated.previous_fp) == (fingerprint(second), fingerprint(first))
    assert rotated.previous_until is not None


def test_of_two_rotations_from_the_same_secret_only_one_writes_it():
    manager, triggers, secrets, _ = rig()
    trigger, _ = asyncio.run(manager.create(guarded(), ALICE, DATA))
    stale = trigger.model_copy()
    rotated, second = asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    with pytest.raises(RunError) as e:
        asyncio.run(manager.rotate(guarded(), stale, ALICE))
    assert (e.value.status, e.value.code) == (409, "state_conflict")
    # 기록과 Secret 이 같은 비밀을 가리킨다
    assert secrets.keys[("ag-1", trigger.id)].current == second
    assert triggers.docs[trigger.id].secret_fp == fingerprint(second)


def test_a_rotation_whose_secret_was_not_written_is_undone():
    manager, triggers, secrets, _ = rig()
    trigger, first = asyncio.run(manager.create(guarded(), ALICE, DATA))
    before = trigger.model_copy()

    async def broken_put(*_: object) -> None:
        raise RuntimeError("secret store down")

    secrets.put = broken_put  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        asyncio.run(manager.rotate(guarded(), before, ALICE))
    stored = triggers.docs[trigger.id]
    assert (stored.secret_fp, stored.previous_fp) == (fingerprint(first), None)
    assert secrets.keys[("ag-1", trigger.id)].current == first


def _half_rotated(updated_at):
    """기록은 바꿨지만 Secret 에는 쓰지 못하고 멈춘 새로 만들기가 남긴 트리거와 보내는 쪽의 비밀."""
    manager, triggers, secrets, _ = rig()
    trigger, held = asyncio.run(manager.create(guarded(), ALICE, DATA))
    lost = new_secret()
    trigger = trigger.model_copy(
        update={
            "secret_fp": fingerprint(lost),
            "previous_fp": fingerprint(held),
            "updated_at": updated_at,
        }
    )
    triggers.docs[trigger.id] = trigger
    return manager, triggers, secrets, trigger, held


def test_a_rotation_left_half_done_is_repaired_once_it_has_settled():
    settled = utcnow() - ROTATION_SETTLE - timedelta(minutes=1)
    manager, triggers, secrets, trigger, held = _half_rotated(settled)
    rotated, secret = asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    keys = secrets.keys[("ag-1", trigger.id)]
    # 보내는 쪽이 쥔 비밀을 24시간 더 받는다
    assert (keys.current, keys.previous) == (secret, held)
    assert (rotated.secret_fp, rotated.previous_fp) == (fingerprint(secret), fingerprint(held))


def test_a_rotation_that_may_still_be_writing_answers_409():
    manager, _, secrets, trigger, held = _half_rotated(utcnow())
    with pytest.raises(RunError) as e:
        asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    assert (e.value.status, e.value.code) == (409, "state_conflict")
    assert secrets.keys[("ag-1", trigger.id)].current == held


def test_a_rotation_whose_write_landed_but_was_undone_is_repaired_at_once():
    manager, triggers, secrets, _ = rig()
    trigger, held = asyncio.run(manager.create(guarded(), ALICE, DATA))
    secrets.keys[("ag-1", trigger.id)] = TriggerKeys(new_secret(), held)
    _, secret = asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    keys = secrets.keys[("ag-1", trigger.id)]
    assert (keys.current, keys.previous) == (secret, held)


def test_undoing_a_failed_rotation_leaves_a_concurrent_rebind_alone():
    manager, triggers, secrets, _ = rig()
    trigger, first = asyncio.run(manager.create(guarded(), ALICE, DATA))

    async def rebound_meanwhile(*_: object) -> None:
        triggers.docs[trigger.id].acts_as = ActsAs(sub="bob")
        raise RuntimeError("secret store down")

    secrets.put = rebound_meanwhile  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        asyncio.run(manager.rotate(guarded(), trigger.model_copy(), ALICE))
    stored = triggers.docs[trigger.id]
    assert (stored.secret_fp, stored.acts_as.sub) == (fingerprint(first), "bob")


def test_a_rotation_that_cannot_be_undone_puts_the_senders_secret_back():
    manager, triggers, secrets, _ = rig()
    trigger, held = asyncio.run(manager.create(guarded(), ALICE, DATA))
    put, update = secrets.put, triggers.update

    async def landed_but_raised(agent_id: str, tid: str, current: str, previous: object) -> None:
        await put(agent_id, tid, current, previous)  # type: ignore[arg-type]
        secrets.put = put  # type: ignore[method-assign]
        raise TimeoutError("write timed out")

    async def store_down(source_id: str, fields: dict, *, match: dict | None = None):
        if match and match["secret_fp"] != fingerprint(held):
            raise RuntimeError("mongo down")
        return await update(source_id, fields, match=match)

    secrets.put = landed_but_raised  # type: ignore[method-assign]
    triggers.update = store_down  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        asyncio.run(manager.rotate(guarded(), trigger.model_copy(), ALICE))
    assert secrets.keys[("ag-1", trigger.id)] == TriggerKeys(held, None)
    # 기록은 새 지문에 남았다. 자리를 잡은 뒤의 새로 만들기가 보내는 쪽의 비밀을 앞 비밀로 둔다
    triggers.update = update  # type: ignore[method-assign]
    stored = triggers.docs[trigger.id]
    stored.updated_at = utcnow() - ROTATION_SETTLE - timedelta(minutes=1)
    _, secret = asyncio.run(manager.rotate(guarded(), stored.model_copy(), ALICE))
    assert secrets.keys[("ag-1", trigger.id)] == TriggerKeys(secret, held)


def test_a_restore_never_overwrites_a_newer_rotation():
    manager, triggers, secrets, _ = rig()
    trigger, held = asyncio.run(manager.create(guarded(), ALICE, DATA))
    put, update = secrets.put, triggers.update
    later: list[str] = []

    async def landed_but_raised(agent_id: str, tid: str, current: str, previous: object) -> None:
        await put(agent_id, tid, current, previous)  # type: ignore[arg-type]
        secrets.put = put  # type: ignore[method-assign]
        raise TimeoutError("write timed out")

    async def undo_fails_after_another_rotation(
        source_id: str, fields: dict, *, match: dict | None = None
    ):
        if "acts_as" in fields:
            return await update(source_id, fields, match=match)
        # 첫 새로 만들기의 되돌리기다. 그 앞에 다른 새로 만들기가 깨끗이 끝났다
        triggers.update = update  # type: ignore[method-assign]
        stored = triggers.docs[trigger.id].model_copy()
        later.append((await manager.rotate(guarded(), stored, ALICE))[1])
        raise RuntimeError("mongo down")

    secrets.put = landed_but_raised  # type: ignore[method-assign]
    triggers.update = undo_fails_after_another_rotation  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        asyncio.run(manager.rotate(guarded(), trigger.model_copy(), ALICE))
    (second,) = later
    assert secrets.keys[("ag-1", trigger.id)].current == second
    assert triggers.docs[trigger.id].secret_fp == fingerprint(second)


def test_a_trigger_deleted_during_a_rotation_answers_404():
    manager, triggers, _, _ = rig()
    trigger, _ = asyncio.run(manager.create(guarded(), ALICE, DATA))
    del triggers.docs[trigger.id]
    with pytest.raises(RunError) as e:
        asyncio.run(manager.rotate(guarded(), trigger, ALICE))
    assert (e.value.status, e.value.code) == (404, "not_found")


def test_pausing_stops_the_open_runs_and_deleting_drops_the_key():
    manager, triggers, secrets, runs = rig()
    trigger, _ = asyncio.run(manager.create(guarded(), ALICE, DATA))
    waiting = runs.add(run_of(source="webhook", source_id=trigger.id, status="waiting_approval"))
    paused = asyncio.run(manager.pause(trigger))
    assert (paused.paused, waiting.status) == (True, "cancelled")
    asyncio.run(manager.delete(trigger))
    assert trigger.id not in triggers.docs and secrets.keys == {}


def test_a_key_that_cannot_be_dropped_leaves_the_delete_done():
    manager, triggers, secrets, _ = rig()
    trigger, _ = asyncio.run(manager.create(guarded(), ALICE, DATA))
    secrets.broken = True
    asyncio.run(manager.delete(trigger))
    assert trigger.id not in triggers.docs
