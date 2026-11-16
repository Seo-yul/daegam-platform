"""원장에 쓰는 한 곳 (P37 7절, D62). 호출 하나는 한 쓰는 쪽이 한 행으로 남긴다.

게이트웨이를 거친 호출은 게이트웨이 기록이 행이고, 부른 쪽은 잇기 문서를 남긴다. 그 밖의 호출은
부른 쪽(런타임, 플랫폼)이 행을 쓴다. 두 쓰는 쪽이 같은 호출에 같은 `_id` 를 쓰므로 먼저 들어간
행이 늘 남는다. 같은 쓰는 쪽의 같은 기록이 다시 오면 다시 보냄이라 셀 것이 없고, 다른 쓰는 쪽이나
같은 호출의 다른 게이트웨이 기록은 0 이어야 하는 원장 건강 사건으로 한 번 적는다.

게이트웨이 행과 잇기 문서는 어느 쪽이 먼저 와도 이어진다. 두 쪽 모두 자기 문서를 먼저 쓰고 다른
쪽을 읽으므로, 둘이 엇갈려도 적어도 한쪽은 다른 쪽을 본다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio

from daegam import llm_http
from daegam.services.usage.pricing import PriceBook
from daegam.store.base import error_kind, utcnow
from daegam.store.hour_marks import hour_of
from daegam.store.settings import LLMProvider
from daegam.store.usage import (
    MongoUsageStore,
    Operation,
    TokenUsage,
    UsageJoin,
    UsageProvider,
    UsageRecord,
    UsageSource,
)
from daegam.urls import under_base

logger = logging.getLogger("daegam.usage.ledger")

# 호출이 끝난 뒤 행이나 잇기 문서를 쓰는 데 주는 초. 취소된 턴(비서의 멈춤)도 쓰기는 마친다
SETTLE_SECONDS = 5.0

WriteOutcome = Literal["inserted", "duplicate", "writer_conflict", "gateway_extra"]


@dataclass(frozen=True)
class CallSite:
    """한 일(비서 턴, spec job, 인덱싱 한 번)의 정해진 칸. 그 일의 호출이 모두 같은 trace 다."""

    tenant: str
    source: UsageSource
    provider: LLMProvider
    model: str
    operation: Operation = "chat"
    ref: str | None = None
    user_sub: str | None = None
    trace_id: str = field(default_factory=llm_http.new_trace_id)
    # 그 일의 호출이 가는 주소 (P38 6절 `resolve_endpoint`). 없으면 provider 의 주소다
    base_url: str | None = None


@dataclass
class CallMeter:
    """호출 하나의 계량. 부른 자리가 응답의 사용량을 `report` 로 알리고, 사용량을 받지 못한
    호출은 `estimate` 로 어림한다."""

    call: llm_http.CallId
    usage: TokenUsage | None = None
    estimated: bool = False
    response_model: str | None = None

    def report(
        self,
        counts: Mapping[str, int],
        *,
        estimated: bool = False,
        response_model: str | None = None,
    ) -> None:
        self.usage = TokenUsage(**{k: int(v) for k, v in counts.items()})
        self.estimated = estimated
        self.response_model = response_model

    def measure(self, prompt: Sequence[str], message: Any) -> Mapping[str, Any]:
        """langchain 응답 메시지(`AIMessage`) 하나의 사용량과 모델 이름을 알리고 부른 자리가 셀
        사용량을 돌려준다. 사용량이 없는 응답(스트림 사용량을 끈 호환 서버)은 글자 수로 어림해
        원장과 부른 자리에 함께 센다 (P37 2절). prompt 는 보낸 메시지의 글이다."""
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            return self.estimate(prompt, [message.text])
        meta = getattr(message, "response_metadata", None) or {}
        self.report(
            llm_http.usage_from_metadata(usage),
            response_model=meta.get("model_name") or meta.get("model"),
        )
        return usage

    def estimate(self, prompt: Sequence[str], output: Sequence[str]) -> dict[str, int]:
        """사용량 없이 끝난 호출(스트림 사용량을 끈 호환 서버, 가짜 모델)을 글자 수 규칙
        (`llm_http.estimate_tokens`)으로 어림해 알리고, 부른 자리가 세션에 더할 수를 돌려준다."""
        counts = {
            "input_tokens": llm_http.estimate_tokens(prompt),
            "output_tokens": llm_http.estimate_tokens(output),
        }
        self.report(counts, estimated=True)
        return counts


@asynccontextmanager
async def unmetered() -> AsyncIterator[CallMeter]:
    """원장 없이 부르는 자리(테스트). 상관 헤더는 그대로 싣는다."""
    with llm_http.call_scope() as call:
        yield CallMeter(call)


def metered(ledger: UsageLedger | None, site: CallSite) -> AbstractAsyncContextManager[CallMeter]:
    """호출 한 번의 계량. 비서·spec 생성기·인덱서가 함께 쓴다. 원장이 없으면(테스트) 남기지
    않는다."""
    return unmetered() if ledger is None else ledger.call(site)


class UsageLedger:
    def __init__(
        self, store: MongoUsageStore, prices: PriceBook, *, gateway_url: str | None = None
    ):
        self.store = store
        self.prices = prices
        self.gateway_url = gateway_url

    def route_of(self, site: CallSite) -> Literal["direct", "gateway"]:
        """플랫폼 호출의 경로. 부른 주소가 게이트웨이 주소 아래인가로만 정한다 (18절). 주소는
        호출자가 고른 길의 것이고(P38 6절), 없으면 provider 의 주소다."""
        url = site.base_url or site.provider.effective_base_url
        return "gateway" if under_base(url, self.gateway_url) else "direct"

    @asynccontextmanager
    async def call(self, site: CallSite) -> AsyncIterator[CallMeter]:
        """플랫폼의 호출 한 번(재시도 한 번도 하나다). 호출 id 를 두고 시간을 재며, 끝나면 직접
        경로는 행을, 게이트웨이 경로는 잇기 문서를 쓴다. 사용량 없이 실패한 직접 호출은 행을 남기지
        않는다. 게이트웨이 호출의 잇기 문서는 끊겼으면 `cancelled`, 실패했으면 `failed` 다."""
        started = time.monotonic()
        with llm_http.call_scope(site.trace_id) as call:
            meter = CallMeter(call)
            try:
                yield meter
            except BaseException as e:
                await self._settle(site, meter, started, failed=e)
                raise
            await self._settle(site, meter, started, failed=None)

    async def _settle(
        self, site: CallSite, meter: CallMeter, started: float, *, failed: BaseException | None
    ) -> None:
        cancelled = isinstance(failed, anyio.get_cancelled_exc_class())

        async def settle() -> None:
            if self.route_of(site) == "gateway":
                errored = failed is not None and not cancelled
                await self.join(_join_of(site, meter.call, cancelled=cancelled, failed=errored))
            elif failed is None and meter.usage is not None:
                await self.write(_row_of(site, meter, started))

        await _settled(meter.call.id, settle)

    async def record(self, row: UsageRecord) -> None:
        """부른 쪽이 다 채운 행 하나를 남긴다(웹 검색, P39b 12절). 호출이 끊겨도 마치고, 실패는
        로그에만 남는다."""
        await _settled(row.id, lambda: self.write(row))

    async def write(self, row: UsageRecord) -> WriteOutcome:
        """행 하나를 매겨 넣는다. 게이트웨이 행은 넣은 뒤 먼저 와 있던 잇기 문서로 잇는다."""
        cost, price = await self.prices.price(
            kind=row.provider.kind,
            provider_name=row.provider.name,
            model=row.model,
            at=row.ts,
            usage=row.usage,
            operation=row.operation,
        )
        row = row.model_copy(update={"cost_usd": cost, "price": price})
        if not await self.store.insert_record(row):
            return await self._collision(row)
        if row.writer == "gateway" and not row.joined:
            join = await self.store.get_join(row.id)
            if join is not None:
                await self._link(row, join)
        await self._touch(row)
        return "inserted"

    async def join(self, join: UsageJoin) -> None:
        """부른 쪽의 잇기 문서를 남긴다. 게이트웨이 행이 먼저 와 있으면 그 행을 채우고 그 시간을
        다시 센다."""
        await self.store.insert_join(join)
        row = await self.store.get_record(join.id)
        if row is not None and row.writer == "gateway" and await self._link(row, join):
            await self._touch(row)

    async def _link(self, row: UsageRecord, join: UsageJoin) -> bool:
        """게이트웨이 행에 잇기 정보를 채운다. 이번에 채웠으면 True 다. 테넌트의 권위는 게이트웨이가
        확인한 부른 쪽 속성이라 다른 테넌트를 말하는 잇기 문서는 잇지 않는다. 그래야 에이전트가 자기
        호출을 다른 테넌트의 세션에 붙이지 못한다. 두 쪽이 함께 이어도 채우기는 한 번이다."""
        if row.joined:
            await self.store.resolve_join(join.id)
            return False
        if row.tenant != join.tenant or _other_agent(row, join):
            await self.store.add_anomaly("join_mismatch", join.id, row.record_id)
            # 게이트웨이 기록은 왔으므로 gateway_missing 으로 다시 세지 않는다
            await self.store.resolve_join(join.id)
            return False
        linked = await self.store.join_record(join.id, join)
        await self.store.resolve_join(join.id)
        return linked

    async def _collision(self, row: UsageRecord) -> WriteOutcome:
        """같은 `_id` 가 이미 있다. 다시 보냄이면 먼저 들어간 행이 남는다. 쓴 쪽이 다르면 사건을
        적고 게이트웨이 행이 남는다. 게이트웨이의 토큰이 권위이고, 부른 쪽 정보는 런타임 행에서
        잇는다."""
        first = await self.store.get_record(row.id)
        if first is None:
            return "duplicate"
        if first.writer != row.writer:
            # 표시가 틀렸다(관리자가 플랫폼이 모르는 게이트웨이 주소를 적었다)
            await self.store.add_anomaly("writer_conflicts", row.id, row.record_id)
            logger.warning(
                "usage: %s wrote call %s that %s wrote first", row.writer, row.id, first.writer
            )
            gateway, runtime = (row, first) if row.writer == "gateway" else (first, row)
            if gateway is row:
                await self.store.replace_record(row)
                await self._touch(first)
            if await self._link(gateway, _caller_of(runtime)) or gateway is row:
                await self._touch(gateway)
            return "writer_conflict"
        if row.writer == "gateway" and first.record_id != row.record_id:
            await self.store.add_anomaly("gateway_extra_records", row.id, row.record_id)
            return "gateway_extra"
        return "duplicate"

    async def _touch(self, row: UsageRecord) -> None:
        """지난 시간의 행이면 그 시간을 다시 셀 시간으로 적는다. 롤업의 틱은 지난 틱 이후의
        시간만 바로 세므로, 시간이 넘어간 뒤 들어온 행의 시간을 놓치지 않게 한다 (10절)."""
        if row.ts < hour_of(utcnow()):
            await self.store.marks.mark(row.ts)


async def _settled(call_id: str, write: Callable[[], Awaitable[object]]) -> None:
    """원장 쓰기 하나를 SETTLE_SECONDS 안에서 마친다. 부른 쪽이 끊겨도 쓰기는 마치고, 쓰기의 실패는
    호출을 실패시키지 않는다."""
    with anyio.move_on_after(SETTLE_SECONDS, shield=True):
        try:
            await write()
        except Exception as e:  # noqa: BLE001 - 원장 쓰기의 실패가 호출을 실패시키면 안 된다
            logger.warning("usage: could not record call %s: %s", call_id, error_kind(e))


def _other_agent(row: UsageRecord, join: UsageJoin) -> bool:
    """게이트웨이가 확인한 부른 Agent 와 잇기 문서의 Agent 가 다르다."""
    agents = row.source.kind == "agent" and join.source.kind == "agent"
    return agents and row.source.id != join.source.id


def _caller_of(row: UsageRecord) -> UsageJoin:
    """런타임 행의 부른 쪽 정보. 게이트웨이 행과 부딪혔을 때 그 행을 잇는다."""
    return UsageJoin(
        id=row.id,
        tenant=row.tenant,
        source=row.source,
        ref=row.ref,
        turn_id=row.turn_id,
        user_sub=row.user_sub,
        trace_id=row.trace_id,
        ts=row.ts,
    )


def _row_of(site: CallSite, meter: CallMeter, started: float) -> UsageRecord:
    return UsageRecord(
        id=meter.call.id,
        ts=utcnow(),
        writer="platform",
        route="direct",
        tenant=site.tenant,
        source=site.source,
        ref=site.ref,
        user_sub=site.user_sub,
        trace_id=site.trace_id,
        provider=UsageProvider(kind=site.provider.sdk_kind, name=site.provider.name),
        model=site.model,
        response_model=meter.response_model,
        operation=site.operation,
        usage=meter.usage or TokenUsage(),
        estimated=meter.estimated,
        latency_ms=(time.monotonic() - started) * 1000,
        record_id=meter.call.id,
    )


def _join_of(site: CallSite, call: llm_http.CallId, *, cancelled: bool, failed: bool) -> UsageJoin:
    return UsageJoin(
        id=call.id,
        tenant=site.tenant,
        source=site.source,
        ref=site.ref,
        user_sub=site.user_sub,
        trace_id=site.trace_id,
        cancelled=cancelled,
        failed=failed,
    )
