"""웹훅 트리거 Secret 의 쓸기와 되돌리기 (P41 4·12절). 장부와 트리거 스토어를 메모리 가짜로, Secret
저장소를 인메모리 대역으로 바꿔 DB 와 클러스터 없이 돈다. Secret 을 다루는 것은 진짜
`TriggerSecrets` 다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from daegam.services.runs.trigger_secrets import (
    PREVIOUS,
    ROTATION_SETTLE,
    TriggerKeys,
    TriggerSecrets,
    fingerprint,
    new_secret,
    secret_name,
)
from daegam.services.runs.trigger_sweep import SWEEP_EVERY, TriggerSecretSweep, stale_keys
from daegam.store.base import new_id, utcnow
from daegam.store.hook_secrets import HookSecret
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.runs import FakeAgent, FakeAgents, FakeHookSecrets, FakeTriggers, trigger_of

NS = "daegam-system"
NOW = utcnow().replace(microsecond=0)
SETTLED = NOW - ROTATION_SETTLE - timedelta(minutes=1)
# Secret 이름은 Agent id 의 꼴(UUID)만 받는다
AG, GONE, AG_A, AG_B = new_id(), new_id(), new_id(), new_id()


class Rig:
    def __init__(self, *triggers: Any, agents: FakeAgents | None = None) -> None:
        self.store = FakeSecretStore()
        self.ledger = FakeHookSecrets()
        self.secrets = TriggerSecrets(lambda: self.store, namespace=NS, ledger=self.ledger)  # type: ignore[arg-type]
        self.triggers = FakeTriggers(*triggers)
        self.sweeper = TriggerSecretSweep(
            agents or FakeAgents(FakeAgent(id=AG)),  # type: ignore[arg-type]
            self.triggers,  # type: ignore[arg-type]
            self.secrets,
        )

    def secret(self, agent_id: str = AG, **data: str) -> None:
        """장부의 행과 Secret. 행은 자리를 잡은 뒤다."""
        self.store.secrets[(NS, secret_name(agent_id))] = dict(data)
        self.ledger.rows[agent_id] = HookSecret(id=agent_id, written_at=SETTLED)

    def keys(self, agent_id: str = AG) -> dict[str, str] | None:
        return self.store.secrets.get((NS, secret_name(agent_id)))

    def sweep(self, now=NOW) -> dict[str, list[str]]:
        return asyncio.run(self.sweeper.sweep(now))


# -- 뺄 키 -------------------------------------------------------------------------------------


def test_keys_without_a_record_and_previous_secrets_past_their_day_are_stale():
    held, old, current = new_secret(), new_secret(), new_secret()
    live = trigger_of(
        secret_fp=fingerprint(current),
        previous_fp=fingerprint(held),
        previous_until=NOW + timedelta(hours=1),
    )
    over = trigger_of(
        secret_fp=fingerprint(current),
        previous_fp=fingerprint(old),
        previous_until=NOW - timedelta(seconds=1),
    )
    undone = trigger_of(secret_fp=fingerprint(held))
    data = {
        live.id: current,
        live.id + PREVIOUS: held,
        over.id: current,
        over.id + PREVIOUS: old,
        # Secret 에 쓴 뒤 기록을 되돌린 새로 만들기다. 앞 비밀이 보내는 쪽의 비밀이다
        undone.id: new_secret(),
        undone.id + PREVIOUS: held,
        "gone": current,
        "gone" + PREVIOUS: held,
    }
    triggers = {t.id: t for t in (live, over, undone)}
    assert sorted(stale_keys(data, triggers, NOW)) == sorted(
        [over.id + PREVIOUS, "gone", "gone" + PREVIOUS]
    )


# -- 쓸기 --------------------------------------------------------------------------------------


def test_the_sweep_drops_stale_keys_and_keeps_the_rest():
    current = new_secret()
    t = trigger_of(agent_id=AG, secret_fp=fingerprint(current))
    rig = Rig(t)
    rig.secret(**{t.id: current, "orphan": new_secret()})
    assert rig.sweep() == {"dropped": [f"{AG}/orphan"]}
    assert rig.keys() == {t.id: current}
    # 한 시간 안에는 다시 보지 않는다
    assert rig.sweep(NOW + SWEEP_EVERY - timedelta(minutes=1)) == {}


def test_a_secret_written_lately_is_left_alone():
    rig = Rig()
    rig.secret(orphan=new_secret())
    rig.ledger.rows[AG].written_at = NOW - timedelta(minutes=1)
    assert rig.sweep() == {}
    assert "orphan" in rig.keys()  # type: ignore[operator]


def test_a_write_that_starts_while_the_sweep_reads_keeps_its_key():
    rig = Rig()
    rig.secret(orphan=new_secret())
    read = rig.secrets.all_keys

    async def written_meanwhile(agent_id: str) -> dict[str, str] | None:
        data = await read(agent_id)
        await rig.ledger.note_write(agent_id, NOW)
        return data

    rig.secrets.all_keys = written_meanwhile  # type: ignore[method-assign]
    assert rig.sweep() == {}
    assert "orphan" in rig.keys()  # type: ignore[operator]


def test_a_key_whose_value_changed_after_the_read_is_kept():
    rig = Rig()
    rig.secret(orphan=new_secret())
    read = rig.secrets.all_keys
    rewritten = new_secret()

    async def changed_meanwhile(agent_id: str) -> dict[str, str] | None:
        data = await read(agent_id)
        rig.store.secrets[(NS, secret_name(agent_id))]["orphan"] = rewritten
        return data

    rig.secrets.all_keys = changed_meanwhile  # type: ignore[method-assign]
    assert rig.sweep() == {}
    assert rig.keys() == {"orphan": rewritten}


def test_the_secret_of_a_deleted_agent_is_deleted_with_its_row():
    rig = Rig(agents=FakeAgents(FakeAgent(id=AG), gone=True))
    rig.secret(GONE, key=new_secret())
    assert rig.sweep() == {"deleted": [GONE]}
    assert rig.keys(GONE) is None and rig.ledger.rows == {}


def test_a_write_after_the_agent_went_keeps_the_row_for_the_next_sweep():
    rig = Rig(agents=FakeAgents(FakeAgent(id=AG), gone=True))
    rig.secret(GONE, key=new_secret())
    drop = rig.secrets.drop_agent

    async def written_meanwhile(agent_id: str) -> None:
        await drop(agent_id)
        await rig.ledger.note_write(agent_id, NOW)

    rig.secrets.drop_agent = written_meanwhile  # type: ignore[method-assign]
    assert rig.sweep() == {}
    assert GONE in rig.ledger.rows


def test_one_agent_that_fails_does_not_stop_the_others():
    a, b = FakeAgent(id=AG_A), FakeAgent(id=AG_B)
    rig = Rig(agents=FakeAgents(by_id={AG_A: a, AG_B: b}, gone=True))
    rig.secret(AG_A, orphan=new_secret())
    rig.secret(AG_B, orphan=new_secret())
    read = rig.secrets.all_keys

    async def broken_for_a(agent_id: str) -> dict[str, str] | None:
        if agent_id == AG_A:
            raise RuntimeError("cluster down")
        return await read(agent_id)

    rig.secrets.all_keys = broken_for_a  # type: ignore[method-assign]
    assert rig.sweep() == {"dropped": [f"{AG_B}/orphan"], "failed": [AG_A]}
    # 실패한 행도 한 시간 뒤에 다시 본다
    assert rig.ledger.rows[AG_A].swept_at == NOW


def test_without_a_secret_store_the_sweep_does_nothing():
    rig = Rig()
    rig.secret(orphan=new_secret())
    rig.sweeper.secrets = TriggerSecrets(lambda: None, namespace=NS, ledger=rig.ledger)  # type: ignore[arg-type]
    assert rig.sweep() == {}


def test_writing_a_trigger_secret_notes_the_agent_first():
    rig = Rig()
    seen: list[dict[str, str] | None] = []
    note = rig.ledger.note_write

    async def noted(agent_id: str, now: Any) -> None:
        seen.append(rig.keys(agent_id))
        await note(agent_id, now)

    rig.ledger.note_write = noted  # type: ignore[method-assign]
    asyncio.run(rig.secrets.put(AG, "t1", new_secret(), None))
    assert seen == [None] and rig.ledger.writes == [AG]
    assert "t1" in rig.keys()  # type: ignore[operator]


def test_a_secret_name_takes_only_an_agent_id():
    assert secret_name(AG) == f"daegam-hooks-{AG}"
    for bad in ("ag-1", "../x", AG.upper(), ""):
        try:
            secret_name(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# -- 실패한 새로 만들기의 되돌리기 ---------------------------------------------------------------


def _restore(rig: Rig, written: TriggerKeys, back: TriggerKeys) -> bool:
    return asyncio.run(rig.secrets.restore(AG, "t1", written, back))


def test_a_restore_puts_the_senders_secret_back_while_the_secret_is_what_the_rotation_wrote():
    rig = Rig()
    new, kept, before = new_secret(), new_secret(), new_secret()
    rig.secret(**{"t1": new, "t1" + PREVIOUS: kept, "other": "x"})
    assert _restore(rig, TriggerKeys(new, kept), TriggerKeys(kept, before))
    assert rig.keys() == {"t1": kept, "t1" + PREVIOUS: before, "other": "x"}


def test_a_restore_leaves_a_later_rotation_alone():
    rig = Rig()
    new, kept, later = new_secret(), new_secret(), new_secret()
    rig.secret(**{"t1": later, "t1" + PREVIOUS: new})
    assert not _restore(rig, TriggerKeys(new, kept), TriggerKeys(kept, None))
    assert rig.keys() == {"t1": later, "t1" + PREVIOUS: new}


def test_a_restore_without_an_earlier_previous_removes_the_previous_key():
    rig = Rig()
    new, kept = new_secret(), new_secret()
    rig.secret(**{"t1": new, "t1" + PREVIOUS: kept})
    assert _restore(rig, TriggerKeys(new, kept), TriggerKeys(kept, None))
    assert rig.keys() == {"t1": kept}
