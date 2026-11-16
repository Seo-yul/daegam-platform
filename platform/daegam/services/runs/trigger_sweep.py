"""웹훅 트리거 Secret 의 쓸기 (P41 4·12절). 리컨실 리스 안의 단계 하나다.

플랫폼은 Secret 목록을 보지 않으므로 장부(`store/hook_secrets.py`)의 Agent 마다 한 시간에 한 번
그 Secret 을 이름으로 읽는다.

- Agent 가 없으면 그 Secret 을 지우고 장부의 행을 지운다. Agent 삭제가 Secret 을 지우지 못했을 때다.
- Agent 가 있으면 남은 키를 뺀다. 기록이 없는 트리거의 키(만들다 멈췄거나, 지울 때 빼지 못했거나,
  지운 뒤에 새로 만들기가 다시 쓴 키)와, 받는 동안이 지난 앞 비밀(`<id>.previous`)이다. 앞 비밀이
  기록의 지금 지문이면 Secret 에 쓴 뒤 기록을 되돌린 새로 만들기가 남긴 보내는 쪽의 비밀이라 둔다.
  Agent 가 있는 동안 Secret 자체와 장부의 행은 지우지 않는다.

쓰기를 시작한 지(`written_at`) `ROTATION_SETTLE` 이 지나지 않은 Agent 는 찾지 않는다. 트리거를
만드는 요청은 Secret 에 쓴 뒤 기록을 넣으므로 그 사이에 본 키를 남은 키로 잘못 보지 않게 한다.
Secret 을 읽은 뒤 기록을 읽고, 키를 빼기 전에 `written_at` 이 그대로인지 다시 본다. 키는 읽은 값
그대로일 때만 빼므로(JSON Patch 의 test) 그 사이에 새로 만들기가 쓴 값은 남는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from daegam.services.budget import Budget
from daegam.services.claims import tick_item
from daegam.services.runs.trigger_secrets import (
    PREVIOUS,
    ROTATION_SETTLE,
    TriggerSecrets,
    fingerprint,
)
from daegam.store.agent_triggers import AgentTrigger, MongoAgentTriggerStore
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.hook_secrets import HookSecret

logger = logging.getLogger("daegam.runs.trigger_sweep")

# 한 Agent 의 Secret 을 다시 보는 사이
SWEEP_EVERY = timedelta(hours=1)
# 한 틱에 보는 행의 수와 시간(초). 못 본 행은 다음 틱이 잇는다
ROWS_PER_TICK = 100
SECONDS_PER_TICK = 10.0


def stale_keys(data: dict[str, str], triggers: dict[str, AgentTrigger], now: datetime) -> list[str]:
    """Secret 에서 뺄 키. 기록이 없는 트리거의 키와 받는 동안이 지난 앞 비밀이다."""
    out: list[str] = []
    for key, value in data.items():
        previous = key.endswith(PREVIOUS)
        trigger = triggers.get(key.removesuffix(PREVIOUS))
        if trigger is None:
            out.append(key)
        elif previous and not _still_held(trigger, fingerprint(value), now):
            out.append(key)
    return out


def _still_held(trigger: AgentTrigger, fp: str, now: datetime) -> bool:
    """앞 비밀이 아직 받아지거나 보내는 쪽이 쥔 값인가."""
    if fp == trigger.secret_fp:
        return True
    return (
        fp == trigger.previous_fp
        and trigger.previous_until is not None
        and trigger.previous_until > now
    )


def _failed(row: HookSecret, error: Exception, result: dict[str, list[str]]) -> None:
    logger.warning("trigger secret sweep skipped agent %s: %s", row.id, error)
    result.setdefault("failed", []).append(row.id)


class TriggerSecretSweep:
    def __init__(
        self,
        agents: MongoAgentStore,
        triggers: MongoAgentTriggerStore,
        secrets: TriggerSecrets,
    ):
        self.agents = agents
        self.triggers = triggers
        self.secrets = secrets
        self.ledger = secrets.ledger

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """쓸 때가 된 행을 본다. 결과는 뺀 키(`dropped`, `<Agent id>/<키>`), 지운 Secret
        (`deleted`), 보지 못한 행(`failed`, `deferred`)이다. 볼 것이 없으면 결과 키가 없다."""
        if not self.secrets.available:
            return {}
        due = await self.ledger.due(now - SWEEP_EVERY, now - ROTATION_SETTLE, ROWS_PER_TICK)
        if not due:
            return {}
        agents = await self.agents.get_many({row.id for row in due})
        budget = Budget(SECONDS_PER_TICK)
        result: dict[str, list[str]] = {"dropped": [], "deleted": []}
        for row in due:
            await tick_item()
            if budget.out_of_time():
                result.setdefault("deferred", []).append(row.id)
                continue
            await self._sweep_row(row, agents.get(row.id), now, result)
        return {key: ids for key, ids in result.items() if ids}

    async def _sweep_row(
        self, row: HookSecret, agent: Agent | None, now: datetime, result: dict[str, list[str]]
    ) -> None:
        if agent is None:
            # 사라진 Agent 다. 일부러 쓸었다고 적지 않는다. 행을 지웠으면 적을 행이 없고,
            # 남겼으면(그사이 쓰기가 시작됐다) 그 쓰기가 자리를 잡은 뒤 바로 다시 보아야 한다.
            # 실패했을 때만 한 시간 뒤에 다시 보도록 적는다
            try:
                if await self._delete(row):
                    result["deleted"].append(row.id)
            except Exception as e:  # noqa: BLE001 - 한 Agent 의 실패가 다른 Agent 를 막지 않는다
                _failed(row, e, result)
                await self.ledger.mark_swept(row.id, now)
        else:
            try:
                dropped = await self._drop_stale(agent, row, now)
                result["dropped"].extend(f"{agent.id}/{key}" for key in dropped)
            except Exception as e:  # noqa: BLE001 - 한 Agent 의 실패가 다른 Agent 를 막지 않는다
                _failed(row, e, result)
            # 실패한 행도 한 시간 뒤에 다시 본다
            await self.ledger.mark_swept(row.id, now)

    async def _delete(self, row: HookSecret) -> bool:
        """사라진 Agent 의 Secret 과 행을 지운다. 그사이 쓰기를 시작했으면 행을 남겨 다음에 다시
        본다."""
        await self.secrets.drop_agent(row.id)
        return await self.ledger.forget(row.id, row.written_at)

    async def _drop_stale(self, agent: Agent, row: HookSecret, now: datetime) -> list[str]:
        data = await self.secrets.all_keys(agent.id)
        if not data:
            return []
        triggers = {t.id: t for t in await self.triggers.list_for_agent(agent.tenant, agent.id)}
        stale = stale_keys(data, triggers, now)
        if not stale:
            return []
        current = await self.ledger.get(row.id)
        if current is None or current.written_at != row.written_at:
            # 읽는 사이 누가 쓰기를 시작했다. 다음 바퀴에 본다
            return []
        if not await self.secrets.drop_if_unchanged(agent.id, {k: data[k] for k in stale}):
            # 읽은 뒤에 값이 바뀌었다. 다음 바퀴에 본다
            return []
        return stale


__all__ = ["ROWS_PER_TICK", "SWEEP_EVERY", "TriggerSecretSweep", "stale_keys"]
