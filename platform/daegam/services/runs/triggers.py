"""웹훅 트리거의 관리 (P41 4·6·10절).

`agent:write` 와 `agent:chat` 이 트리거를 만들고 바꾸고 비밀을 새로 만들고 멈추고 다시 켜고 지운다.
이름만 바꾸는 것 말고는 모두 바꾼 사람에게 다시 묶고 만료를 새로 센다. 비밀은
만들 때와 새로 만들 때 한 번만 돌려준다. 새로 만들면 앞 비밀을 24시간 더 받는다.

Agent 는 실행의 전제 조건(10절)과 함께 트리거 규칙(`trigger_rule.py`)을 지날 때만 트리거를 가진다
(`RunService.check_trigger`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from daegam.auth.principal import Principal
from daegam.services.runs.errors import RunError
from daegam.services.runs.service import PAUSED_BY_HAND, SOURCE_DELETED, RunService, expiry
from daegam.services.runs.trigger_secrets import (
    ROTATION_GRACE,
    ROTATION_SETTLE,
    TriggerKeys,
    fingerprint,
    new_secret,
)
from daegam.store.agent_triggers import AgentTrigger
from daegam.store.agents import Agent
from daegam.store.base import new_id, utcnow

logger = logging.getLogger("daegam.runs.triggers")


@dataclass(frozen=True)
class TriggerInput:
    name: str
    template: str
    expires_in_days: int


def _secret_changed() -> RunError:
    return RunError(
        409,
        "state_conflict",
        "another rotation of this trigger is under way; reload the trigger and rotate again "
        "in a few minutes",
    )


def _kept(trigger: AgentTrigger, keys: TriggerKeys) -> str | None:
    """새로 만든 뒤 앞 비밀로 둘 값. 보내는 쪽이 쥔 비밀이다.

    보통은 지문이 기록과 같은 Secret 의 지금 비밀이다. 키가 없어진 트리거는 둘 것이 없고 새로
    만들기가 고친다. 기록과 Secret 사이에서 멈춘 새로 만들기는 둘을 어긋나게 남긴다. Secret 의 앞
    비밀이 기록의 지문이면 Secret 에 쓴 뒤 기록을 되돌린 것이라 그 값을 둔다. 그 밖의 어긋남은
    기록을 바꾼 지 `ROTATION_SETTLE` 이 지나면 버려진 새로 만들기로 보고 Secret 의 지금 비밀을 둔다.
    그보다 이르면 다른 새로 만들기가 Secret 에 쓰는 중일 수 있어 409 다."""
    current, previous = keys.current, keys.previous
    if current is None or fingerprint(current) == trigger.secret_fp:
        return current
    if previous is not None and fingerprint(previous) == trigger.secret_fp:
        return previous
    if utcnow() - trigger.updated_at >= ROTATION_SETTLE:
        return current
    raise _secret_changed()


class RunTriggers:
    def __init__(self, service: RunService):
        self.service = service
        self.store = service.triggers
        self.secrets = service.trigger_secrets

    async def create(
        self, agent: Agent, user: Principal, data: TriggerInput
    ) -> tuple[AgentTrigger, str]:
        """트리거와 그 비밀. 비밀은 여기서 한 번만 돌려준다. Secret 에 먼저 적고 기록을 넣으므로
        기록 없이 남은 키는 리컨실이 걷는다."""
        revision = self.service.check_trigger(agent)
        acts_as = await self.service.bind(user, agent.tenant)
        await self.service.require_room(self.store, agent)
        now = utcnow()
        secret = new_secret()
        trigger = AgentTrigger(
            id=new_id(),
            tenant=agent.tenant,
            agent_id=agent.id,
            acts_as=acts_as,
            revision=revision,
            name=data.name,
            template=data.template,
            secret_fp=fingerprint(secret),
            expires_at=expiry(data.expires_in_days, now),
            expires_in_days=data.expires_in_days,
            created_at=now,
            updated_at=now,
        )
        await self.secrets.put(agent.id, trigger.id, secret, None)
        await self.store.insert(trigger)
        return trigger, secret

    async def update(
        self,
        agent: Agent,
        trigger: AgentTrigger,
        user: Principal,
        *,
        name: str | None = None,
        template: str | None = None,
        expires_in_days: int | None = None,
    ) -> AgentTrigger:
        """보낸 칸만 바꾼다. 이름만 바꾸면 묶음은 그대로이고, 그 밖의 변경은 바꾼 사람에게 다시 묶고
        만료를 새로 센다."""
        fields: dict[str, object] = {"updated_at": utcnow()}
        if name is not None:
            fields["name"] = name
        if template is None and expires_in_days is None:
            return await self.service.save_source(self.store, trigger, fields)
        days = expires_in_days if expires_in_days is not None else trigger.expires_in_days
        fields.update(
            await self._rebind(agent, user),
            template=template if template is not None else trigger.template,
            expires_at=expiry(days, utcnow()),
            expires_in_days=days,
        )
        return await self.service.save_source(self.store, trigger, fields)

    async def rotate(
        self, agent: Agent, trigger: AgentTrigger, user: Principal
    ) -> tuple[AgentTrigger, str]:
        """비밀을 새로 만든다. 보내는 쪽이 쥔 비밀(`_kept`)을 앞 비밀로 24시간 더 받는다. 다시
        묶는다.

        기록을 그 지문에 걸어 Secret 보다 먼저 바꾸므로 함께 새로 만든 요청 가운데 하나만 Secret 에
        쓴다. Secret 에 쓰지 못하면 기록의 비밀 칸을 되돌린다(`_undo`). 그사이 지워졌으면 404 다."""
        fields = await self._rebind(agent, user)
        keys = await self.secrets.read(agent.id, trigger.id)
        kept = _kept(trigger, keys)
        secret = new_secret()
        now = utcnow()
        written: dict[str, object] = {
            "secret_fp": fingerprint(secret),
            "previous_fp": fingerprint(kept) if kept is not None else None,
            "previous_until": now + ROTATION_GRACE if kept is not None else None,
        }
        fields.update(written, expires_at=expiry(trigger.expires_in_days, now))
        # `_kept` 이 버려진 새로 만들기를 가리는 시각이다. 기록을 바꾸는 바로 앞에서 찍는다
        fields["updated_at"] = utcnow()
        rotated = await self.store.update(
            trigger.id, fields, match={"secret_fp": trigger.secret_fp}
        )
        if rotated is None:
            if await self.store.get(trigger.tenant, trigger.agent_id, trigger.id) is None:
                raise RunError(404, "not_found", f"trigger {trigger.id} not found")
            raise _secret_changed()
        try:
            await self.secrets.put(agent.id, trigger.id, secret, kept)
        except Exception:
            await self._undo(trigger, written, TriggerKeys(secret, kept), keys)
            raise
        return rotated, secret

    async def _undo(
        self,
        trigger: AgentTrigger,
        fields: dict[str, object],
        written: TriggerKeys,
        before: TriggerKeys,
    ) -> None:
        """Secret 에 쓰지 못한 새로 만들기의 비밀 칸을 되돌린다. 비밀 칸만 되돌리므로 그사이 다른
        사람이 다시 묶은 것은 그대로이고, 이 새로 만들기가 다시 묶은 것도 남는다. 기록이 그사이
        바뀌었으면(다른 새로 만들기, 삭제) 아무것도 하지 않는다.

        되돌리기를 쓰지 못했으면 Secret 이 아직 이 새로 만들기가 쓴 값일 때만(쓰기가 닿고도
        실패로 돌아온 때) 보내는 쪽의 비밀로 되돌린다. 그러면 다음 새로 만들기가 어긋남을 버려진
        것으로 보고 그 비밀을 앞 비밀로 둔다(`_kept`). 그사이 다른 새로 만들기가 Secret 에 썼으면
        그대로 둔다."""
        undo = trigger.model_dump(include=set(fields))
        try:
            await self.store.update(trigger.id, undo, match={"secret_fp": fields["secret_fp"]})
            return
        except Exception as e:  # noqa: BLE001 - Secret 을 되돌려 다음 새로 만들기가 고치게 한다
            logger.warning("trigger %s: a failed rotation was not undone: %s", trigger.id, e)
        if written.previous is None:
            # 보내는 쪽이 쥔 비밀이 없었다(키가 없어진 트리거)
            return
        back = TriggerKeys(written.previous, before.previous)
        try:
            if not await self.secrets.restore(trigger.agent_id, trigger.id, written, back):
                logger.warning(
                    "trigger %s: its secret was not restored; it changed after the failed rotation",
                    trigger.id,
                )
        except Exception as e:  # noqa: BLE001 - 앞 비밀은 previous 로 24시간 더 받는다
            logger.warning("trigger %s: its secret was not restored: %s", trigger.id, e)

    async def pause(self, trigger: AgentTrigger) -> AgentTrigger:
        return await self.service.pause_source(self.store, trigger, PAUSED_BY_HAND)

    async def resume(self, agent: Agent, trigger: AgentTrigger, user: Principal) -> AgentTrigger:
        """다시 켜는 사람에게 묶고 만료를 새로 센다."""
        fields = await self._rebind(agent, user)
        fields.update(
            paused=False,
            paused_reason=None,
            paused_cause=None,
            expires_at=expiry(trigger.expires_in_days, utcnow()),
        )
        return await self.service.save_source(self.store, trigger, fields)

    async def delete(self, trigger: AgentTrigger) -> None:
        """지우고 열린 실행을 중지처럼 끝내고 키를 뺀다. 키를 빼지 못하면 리컨실이 걷는다."""
        await self.service.end_open_for_source(trigger.id, SOURCE_DELETED, utcnow())
        await self.store.delete(trigger.id)
        try:
            await self.secrets.drop(trigger.agent_id, trigger.id)
        except Exception as e:  # noqa: BLE001 - 남은 키는 리컨실의 쓸기가 걷는다
            logger.warning("trigger %s: its key was not dropped: %s", trigger.id, e)

    async def _rebind(self, agent: Agent, user: Principal) -> dict[str, object]:
        """다시 묶는 변경의 칸. 전제 조건과 트리거 규칙을 다시 본다(`RunService.check_trigger`)."""
        revision = self.service.check_trigger(agent)
        acts_as = await self.service.bind(user, agent.tenant)
        return {"acts_as": acts_as.model_dump(), "revision": revision, "updated_at": utcnow()}


__all__ = ["RunTriggers", "TriggerInput"]
