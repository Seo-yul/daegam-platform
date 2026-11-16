"""원장 테스트의 행과 실행 (P37). 원장·롤업·플랫폼 호출 자리 테스트가 같이 쓴다."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from daegam.config import Settings
from daegam.services.usage.ledger import UsageLedger
from daegam.services.usage.pricing import PriceBook
from daegam.services.usage.rollup import UsageRollup
from daegam.store.base import utcnow
from daegam.store.leases import Lease
from daegam.store.pricing import MongoPricingStore
from daegam.store.usage import (
    MongoUsageStore,
    TokenUsage,
    UsageProvider,
    UsageRecord,
    UsageSource,
)
from tests.helpers.aio import run_stores

# 게이트웨이 행은 잇기 정보 없이 들어오고, 그 밖의 행은 부른 쪽이 쓴 것이라 이어져 있다
GATEWAY = "gateway"


def usage_row(
    call_id: str,
    *,
    writer: str = "runtime",
    ts: datetime | None = None,
    tenant: str = "t1",
    record_id: str | None = None,
    **kw: Any,
) -> UsageRecord:
    """원장 행 하나. gpt-4o 의 입력 1000·출력 500 토큰이 기본이다."""
    return UsageRecord(
        id=call_id,
        ts=ts or utcnow(),
        writer=writer,
        route="gateway" if writer == GATEWAY else "direct",
        tenant=tenant,
        source=kw.pop("source", UsageSource(kind="agent", id="a1", name="faq")),
        user_sub=kw.pop("user_sub", "u1"),
        provider=UsageProvider(kind="openai", name="p"),
        model=kw.pop("model", "gpt-4o"),
        usage=kw.pop("usage", TokenUsage(input_tokens=1000, output_tokens=500)),
        record_id=record_id or call_id,
        joined=writer != GATEWAY,
        **kw,
    )


def run_ledger[R](
    settings: Settings,
    body: Callable[[UsageLedger, UsageRollup, MongoUsageStore], Awaitable[R]],
    *,
    gateway_url: str | None = None,
    store_cls: Callable[[Any], MongoUsageStore] = MongoUsageStore,
) -> R:
    """원장·롤업·원장 스토어로 body 를 한 번 돌린다. 가격표는 기억하지 않아 고친 값이 곧바로
    쓰인다. 롤업의 리스 홀더는 앱과 같은 파드 id 라 앱이 떠 있어도 틱이 돈다."""

    async def go(usage: MongoUsageStore, pricing: MongoPricingStore) -> R:
        prices = PriceBook(pricing, cache_seconds=0)
        rollup = UsageRollup(usage, prices, Lease(usage.db, settings.pod_id), interval=60)
        ledger = UsageLedger(usage, prices, gateway_url=gateway_url)
        return await body(ledger, rollup, usage)

    return run_stores(settings, (store_cls, MongoPricingStore), go)
