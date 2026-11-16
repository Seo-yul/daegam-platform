"""웹훅 트리거의 호출 (P41 4절).

`POST /api/hooks/{trigger_id}` 는 Bearer 없이 Standard Webhooks 서명으로 부른다. 이 차례로 본다.

1. 트리거를 찾는다. 없는 id 는 잠시 기억해 다시 찾지 않는다. 최근에 찾은 적이 없는 id 의 찾기는
   넉넉한 전체 예산 하나를 쓰고, 예산이 비면 429 `hook_rate_limited` 다.
2. 본문을 읽으며 64 KiB 에서 끊는다. 없는 트리거는 본문을 읽지 않는다.
3. 서명. `webhook-signature` 의 `v1,<base64>` 항목마다 `"<id>.<timestamp>.<body>"` 의
   HMAC-SHA256 과 `hmac.compare_digest` 로 견주고, 지금 비밀과 받는 동안의 앞 비밀을 모두 본다. 비밀
   값은 지문으로 기억하고 Secret 에 없는 지문도 잠시 기억해 위조한 호출마다 Secret 을 읽지 않는다.
   Secret 을 읽지 못하면 503 이다.
4. 타임스탬프가 5분 안이어야 한다.

없는 트리거, 본문 상한, 서명, 타임스탬프에서 막히면 모두 같은 401 `hook_signature_invalid` 다. 막힌
호출은 트리거마다의 실패 버킷(없는 id 는 한 버킷을 함께 쓴다)에서 세고, 버킷이 비면 401 대신 429
다. 서명이 맞은 호출만 트리거의 버킷(1분에 60번)에서 센다. 그래서 위조한 호출이 트리거의 버킷을
비우지 못한다. 버킷은 있는 트리거의 id 로만 세우고 크기를 정해 두어 오래 안 쓴 것부터
잊는다(레플리카마다).

그 뒤로는 같은 delivery id(`webhook-id`)의 호출이 이미 있으면 그 실행 id 로 202 이고, 멈췄거나
만료된 트리거는 409 `trigger_paused`, 틀이 풀리지 않으면 422 `hook_template_unresolved`, 메시지가
16 KiB 나 핀된 리비전의 입력 한도를 넘으면 413 `hook_message_too_long`, Agent 의 상한이면 429
`agent_run_limit` 이고 실행을 넣지 않는다. 아니면 실행 기록을 넣고 202 `{run_id}` 다. 런타임은
기다리지 않는다.

감사는 경로가 스스로 쓴다. 막은 호출은 Agent 마다(모르는 id 는 한 칸) 1분에 한 행을 두고 수를 센다.
받아들인 호출은 실행 id 와 함께 한 행이다. 본문은 어디에도 남기지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any, NoReturn

from daegam.audit.store import AuditEvent, MongoAuditStore
from daegam.services.runs.errors import RunError
from daegam.services.runs.hook_template import MessageTooLong, render_message
from daegam.services.runs.service import RunService, input_limit
from daegam.services.runs.trigger_secrets import fingerprint, key_bytes
from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.store.agent_runs import input_digest, new_run
from daegam.store.agent_triggers import AgentTrigger
from daegam.store.agents import MongoAgentStore
from daegam.store.base import new_id, utcnow
from daegam.ttl_cache import TTLCache
from daegam.urls import hook_path

logger = logging.getLogger("daegam.runs.webhooks")

BODY_CAP = 64 * 1024
TOLERANCE = timedelta(minutes=5)
MESSAGE_CAP_BYTES = 16 * 1024
# 트리거마다 서명이 맞은 호출과 막힌 호출의 버킷 (4절)
CALLS_PER_MINUTE = 60.0
FAILURES_PER_MINUTE = 60.0
UNKNOWN_BUCKET = "unknown"
LIMITS_ENTRIES = 10_000
LIMITS_IDLE_SECONDS = 600.0
# 최근에 찾지 않은 id 의 찾기를 묶는 전체 예산. 1초에 한 번쯤 오는 위조로는 비지 않는다
LOOKUPS_PER_SECOND = 20.0
LOOKUP_BURST = 600.0
LOOKUP_BUCKET = "lookups"
# 없는 트리거 id 를 기억하는 동안과 수
NEGATIVE_SECONDS = 60.0
NEGATIVE_ENTRIES = 10_000
# 비밀 값을 지문으로 기억하는 동안과 수. 비밀을 바꾸면 지문이 바뀐다
KEY_SECONDS = 3600.0
KEY_ENTRIES = 10_000
# Secret 에 값이 없는 지문을 기억하는 동안. 새로 만들기가 Secret 에 쓰는 사이에 생긴다
ABSENT_KEY_SECONDS = 10.0
MAX_DELIVERY_ID = 256
# 감사 행의 주체
HOOKS_SUB = "system:hooks"

Reader = Callable[[], Awaitable[bytes | None]]


def _refused() -> RunError:
    return RunError(401, "hook_signature_invalid", "the call is not signed by this trigger")


def _rate_limited() -> RunError:
    return RunError(
        429, "hook_rate_limited", "too many calls for this trigger", headers={"Retry-After": "1"}
    )


def _too_long(limit: int) -> RunError:
    return RunError(
        413,
        "hook_message_too_long",
        f"the message must fit {MESSAGE_CAP_BYTES} bytes and {limit} characters",
        limit=limit,
    )


def signature_ok(keys: list[bytes], headers: Mapping[str, str], body: bytes, now: float) -> bool:
    """Standard Webhooks 서명과 타임스탬프. 모든 항목을 모든 키와 견줘 맞은 자리가 시간에 드러나지
    않게 한다. ASCII 가 아닌 항목은 맞을 수 없으므로 견주지 않는다."""
    msg_id = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    if not msg_id or len(msg_id) > MAX_DELIVERY_ID or not (stamp.isascii() and stamp.isdigit()):
        return False
    signed = f"{msg_id}.{stamp}.".encode() + body
    offered = [
        part.split(",", 1)[1].encode()
        for part in headers.get("webhook-signature", "").split()
        if part.startswith("v1,") and part.isascii()
    ]
    good = False
    for key in keys:
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
        for sig in offered:
            good |= hmac.compare_digest(expected, sig)
    return good and abs(now - int(stamp)) <= TOLERANCE.total_seconds()


class WebhookReceiver:
    def __init__(
        self,
        service: RunService,
        agents: MongoAgentStore,
        audit: MongoAuditStore | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.service = service
        self.agents = agents
        self.secrets = service.trigger_secrets
        self.audit = audit
        self._wall = wall

        def buckets(per_minute: float) -> KeyedBuckets:
            return KeyedBuckets(
                per_minute / 60,
                per_minute,
                idle_seconds=LIMITS_IDLE_SECONDS,
                max_entries=LIMITS_ENTRIES,
                clock=clock,
            )

        self._calls = buckets(CALLS_PER_MINUTE)
        self._failures = buckets(FAILURES_PER_MINUTE)
        self._lookups = KeyedBuckets(
            LOOKUPS_PER_SECOND, LOOKUP_BURST, idle_seconds=60.0, max_entries=1, clock=clock
        )
        # 최근에 찾은 트리거 id. 찾기 예산을 쓰지 않는다
        self._known: TTLCache[str, bool] = TTLCache(
            LIMITS_IDLE_SECONDS, clock=clock, max_entries=LIMITS_ENTRIES
        )
        self._unknown: TTLCache[str, bool] = TTLCache(
            NEGATIVE_SECONDS, clock=clock, max_entries=NEGATIVE_ENTRIES
        )
        self._keys: TTLCache[str, bytes] = TTLCache(
            KEY_SECONDS, clock=clock, max_entries=KEY_ENTRIES
        )
        self._absent: TTLCache[str, bool] = TTLCache(
            ABSENT_KEY_SECONDS, clock=clock, max_entries=KEY_ENTRIES
        )

    async def receive(self, trigger_id: str, headers: Mapping[str, str], read_body: Reader) -> str:
        """호출 하나를 받아 실행 id 를 돌려준다. 막으면 RunError 다. read_body 는 상한을 넘으면
        None 인 본문이고 찾은 트리거에만 부른다."""
        trigger = await self._lookup(trigger_id)
        if trigger is None:
            await self._refuse(None)
        body = await read_body()
        if body is None or not signature_ok(
            await self._signing_keys(trigger), headers, body, self._wall()
        ):
            await self._refuse(trigger)
        if not self._calls.take(trigger.id):
            raise _rate_limited()
        return await self._accept(trigger, headers, body)

    async def _lookup(self, trigger_id: str) -> AgentTrigger | None:
        if self._unknown.get(trigger_id):
            return None
        if not self._known.get(trigger_id) and not self._lookups.take(LOOKUP_BUCKET):
            raise _rate_limited()
        trigger = await self.service.triggers.get_by_id(trigger_id)
        if trigger is None:
            self._unknown.put(trigger_id, True)
            return None
        self._known.put(trigger_id, True)
        return trigger

    async def _refuse(self, trigger: AgentTrigger | None) -> NoReturn:
        """막은 호출. 실패 버킷이 비었으면 감사에 쓰지 않고 429 다. 아니면 Agent 마다(모르는 id 는
        한 칸) 1분에 한 행에 세고 401 이다."""
        if not self._failures.take(trigger.id if trigger is not None else UNKNOWN_BUCKET):
            raise _rate_limited()
        minute = utcnow().replace(second=0, microsecond=0)
        scope = trigger.agent_id if trigger is not None else UNKNOWN_BUCKET
        event = self._event(
            trigger,
            "hook_refused",
            401,
            id=f"hook-refused:{scope}:{minute.isoformat()}",
            ts=minute,
            resource_id=trigger.agent_id if trigger is not None else None,
            error_code="hook_signature_invalid",
        )
        await self._audit(event, counted=True)
        raise _refused()

    async def _signing_keys(self, trigger: AgentTrigger) -> list[bytes]:
        """지금 비밀과 받는 동안의 앞 비밀의 키. 기억한 것이 없는 지문이 있을 때만 Secret 을 읽고,
        읽어도 없던 지문은 잠시 기억한다."""
        wanted = [trigger.secret_fp]
        if trigger.previous_fp and trigger.previous_until and trigger.previous_until > utcnow():
            wanted.append(trigger.previous_fp)
        missing = [fp for fp in wanted if self._keys.get(fp) is None and not self._absent.get(fp)]
        if missing:
            try:
                read = await self.secrets.read(trigger.agent_id, trigger.id)
            except Exception as e:
                logger.warning("trigger %s: its secret could not be read: %s", trigger.id, e)
                raise RunError(
                    503, "hook_secret_unavailable", "the trigger's secret cannot be read"
                ) from e
            for value in (read.current, read.previous):
                if value is not None:
                    self._keys.put(fingerprint(value), key_bytes(value))
            for fp in missing:
                if self._keys.get(fp) is None:
                    self._absent.put(fp, True)
        return [key for fp in wanted if (key := self._keys.get(fp)) is not None]

    async def _accept(self, trigger: AgentTrigger, headers: Mapping[str, str], body: bytes) -> str:
        delivery_id = headers["webhook-id"]
        runs = self.service.runs
        earlier = await runs.find_delivery(trigger.id, delivery_id)
        if earlier is not None:
            return earlier.id
        now = utcnow()
        if trigger.paused or trigger.expires_at <= now:
            raise RunError(409, "trigger_paused", "this trigger is paused or expired")
        agent = await self.agents.get(trigger.agent_id, tenant=trigger.tenant)
        if agent is None or agent.deployed is None:
            raise RunError(409, "not_deployed", "the trigger's agent is not deployed")
        limit = input_limit(agent.deployed)
        try:
            message = render_message(trigger.template, body, MESSAGE_CAP_BYTES)
        except MessageTooLong:
            raise _too_long(limit) from None
        if message is None:
            raise RunError(422, "hook_template_unresolved", "the message template does not resolve")
        if len(message) > limit:
            raise _too_long(limit)
        error = await self.service.cap_error(trigger.tenant, trigger.agent_id, now)
        if error is not None:
            raise error
        size, digest = input_digest(message)
        run = new_run(
            tenant=trigger.tenant,
            agent_id=trigger.agent_id,
            source=self.service.trigger_kind.source,
            acts_as=trigger.acts_as,
            now=now,
            source_id=trigger.id,
            delivery_id=delivery_id,
            message=message,
            input_bytes=size,
            input_sha256=digest,
        )
        if not await runs.insert(run):
            # 같은 호출이 방금 들어왔다
            raced = await runs.find_delivery(trigger.id, delivery_id)
            return raced.id if raced is not None else run.id
        path_params = {"trigger_id": trigger.id}
        await self._audit(
            self._event(trigger, "hook_accepted", 202, resource_id=run.id, path_params=path_params)
        )
        return run.id

    def _event(
        self, trigger: AgentTrigger | None, action: str, status: int, **fields: Any
    ) -> AuditEvent:
        """경로의 감사 행. fields 는 행마다 다른 칸이다."""
        return AuditEvent(
            sub=HOOKS_SUB,
            subject_type="system",
            tenant=trigger.tenant if trigger is not None else None,
            action=action,
            resource=self.service.trigger_kind.resource,
            method="POST",
            path=hook_path(trigger.id if trigger is not None else "*"),
            request_id=new_id(),
            status=status,
            result="ok" if status < 400 else "error",
            **fields,
        )

    async def _audit(self, event: AuditEvent, *, counted: bool = False) -> None:
        """감사 행을 쓴다. 막혀도 답은 같으므로 경고만 남긴다."""
        if self.audit is None:
            return
        try:
            await (self.audit.count if counted else self.audit.record)(event)
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 답은 같다
            logger.warning("webhook audit row %s not written: %s", event.action, e)


__all__ = ["BODY_CAP", "Reader", "WebhookReceiver", "signature_ok"]
