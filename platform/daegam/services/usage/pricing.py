"""가격 매기기 (P37 6절). 금액은 모두 이 한 표에서 나온다. 게이트웨이 행도 같다.

호출의 규칙은 (kind, provider 이름, 모델, 시각)에 맞는 것들 가운데 첫째다. 순서는 provider 이름이
있는 것, 정확한 이름, 더 긴 앞머리, 관리자 규칙, 더 늦은 `effective_from` 이다. 그래서 관리자의
짧은 앞머리는 더 긴 기본 앞머리에 진다. 규칙에 긴 프롬프트의 값이 있으면 문턱보다 많은 요청은 그
값으로 매긴다(`priced_rule`). 맞는 규칙이 없으면
금액은 0 이 아니라 None 이다. 합계는 매긴 행만 더하고 매기지 못한 호출 수를 따로 센다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from daegam.store.pricing import MongoPricingStore, PriceRule, PricingTable, default_rules
from daegam.store.usage import AppliedPrice, TokenUsage
from daegam.ttl_cache import TTLCache

# 표를 다시 읽기까지의 초. 고친 가격은 이만큼 뒤 들어오는 행부터 매겨진다
PRICE_CACHE_SECONDS = 15.0
PER_MILLION = 1_000_000


@dataclass(frozen=True)
class _Candidate:
    rule: PriceRule
    admin: bool

    def order(self) -> tuple[bool, bool, int, bool, date]:
        r = self.rule
        prefix_len = len(r.model) - 1 if r.is_prefix else len(r.model)
        return (
            r.provider_name is not None,
            not r.is_prefix,
            prefix_len,
            self.admin,
            r.effective_from,
        )


def match_rule(
    table: PricingTable,
    kind: str,
    provider_name: str | None,
    model: str,
    at: datetime,
    *,
    defaults: Iterable[PriceRule] | None = None,
) -> PriceRule | None:
    """호출 하나의 규칙. 없으면 None 이다."""
    day = at.date()
    candidates = [
        _Candidate(rule, admin)
        for rules, admin in ((table.rules, True), (_or_defaults(defaults), False))
        for rule in rules
        if rule.provider_kind == kind
        and rule.provider_name in (None, provider_name)
        and rule.matches_model(model)
        and rule.effective_from <= day
    ]
    if not candidates:
        return None
    return max(candidates, key=_Candidate.order).rule


def _or_defaults(defaults: Iterable[PriceRule] | None) -> Iterable[PriceRule]:
    """기본 가격. 빈 목록을 주면(테스트) 기본 가격 없이 매긴다."""
    return default_rules() if defaults is None else defaults


def priced_rule(rule: PriceRule, usage: TokenUsage) -> PriceRule:
    """요청에 매길 값. 규칙의 긴 프롬프트 값(`tier`)은 요청의 input 토큰이 그 문턱보다 많을 때
    요청 전체에 붙는다. 규칙 id 는 그대로라 행이 어느 규칙으로 매겨졌는지는 같다."""
    if rule.tier is None:
        return rule
    if usage.input_tokens <= rule.tier.above_input_tokens:
        return rule
    return rule.model_copy(update=rule.tier.model_dump(exclude={"above_input_tokens"}))


def cost_of(rule: PriceRule, usage: TokenUsage, operation: str) -> float | None:
    """규칙으로 매긴 달러. web_search 는 요청당 값만 읽고 그것이 없는 규칙이면 None 이다."""
    if operation == "web_search" or rule.provider_kind == "web_search":
        return None if rule.request is None else usage.requests * rule.request
    cache_read = rule.input if rule.cache_read is None else rule.cache_read
    cache_write = rule.input if rule.cache_write is None else rule.cache_write
    uncached = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    total = (
        uncached * rule.input
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
        + usage.output_tokens * rule.output
    )
    return total / PER_MILLION


# 행에 함께 남기는 규칙의 값
APPLIED_FIELDS = frozenset({"input", "output", "cache_read", "cache_write", "request"})


def applied(rule: PriceRule, version: int) -> AppliedPrice:
    return AppliedPrice(rule_id=rule.id, version=version, **rule.model_dump(include=APPLIED_FIELDS))


class PriceBook:
    """가격표를 짧게 기억하고 호출에 값을 매긴다. 레플리카마다 따로 든다."""

    def __init__(self, store: MongoPricingStore, *, cache_seconds: float = PRICE_CACHE_SECONDS):
        self.store = store
        self._cache: TTLCache[str, PricingTable] = TTLCache(cache_seconds)

    async def table(self) -> PricingTable:
        """매길 때 쓰는 표. 짧게 기억한 것이다."""
        table = self._cache.get("table")
        if table is None:
            table = await self.read()
            self._cache.put("table", table)
        return table

    async def read(self) -> PricingTable:
        """저장된 표 그대로. 화면과 다시 계산이 읽는다."""
        return await self.store.get()

    async def replace(
        self, rules: list[PriceRule], *, expected_version: int, updated_by: str | None
    ) -> PricingTable | None:
        """표 전체를 바꾼다(`MongoPricingStore.replace`). 바꾼 레플리카는 곧바로 새 표로 매긴다."""
        table = await self.store.replace(
            rules, expected_version=expected_version, updated_by=updated_by
        )
        if table is not None:
            self._cache.clear()
        return table

    async def price(
        self,
        *,
        kind: str,
        provider_name: str | None,
        model: str,
        at: datetime,
        usage: TokenUsage,
        operation: str,
        table: PricingTable | None = None,
    ) -> tuple[float | None, AppliedPrice | None]:
        """(달러, 매긴 규칙). 맞는 규칙이 없거나 규칙이 값을 주지 않으면 달러는 None 이다."""
        table = table if table is not None else await self.table()
        rule = match_rule(table, kind, provider_name, model, at)
        if rule is None:
            return None, None
        rule = priced_rule(rule, usage)
        return cost_of(rule, usage, operation), applied(rule, table.version)
