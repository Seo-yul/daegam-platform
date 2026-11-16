"""가격표와 원장 (P37 6절·7절, D62): 규칙 고르기, 금액, 한 호출 한 행, 잇기 문서, 건강 사건."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from daegam.services.usage.ledger import UsageLedger
from daegam.services.usage.pricing import PriceBook, cost_of, match_rule
from daegam.store.pricing import PriceRule, PricingTable, default_rules
from daegam.store.usage import MongoUsageStore, TokenUsage, UsageJoin, UsageSource
from tests.helpers.usage import GATEWAY, run_ledger, usage_row

AT = datetime(2026, 10, 1, 12, 0)


def _rule(model: str, **kw) -> PriceRule:
    return PriceRule(
        id=kw.pop("id", model),
        provider_kind=kw.pop("provider_kind", "openai"),
        model=model,
        input=kw.pop("input", 1.0),
        output=kw.pop("output", 2.0),
        effective_from=kw.pop("effective_from", date(2026, 1, 1)),
        **kw,
    )


def test_the_most_specific_rule_wins() -> None:
    table = PricingTable(
        rules=[
            _rule("gpt-5*", id="prefix"),
            _rule("gpt-5-mini", id="exact"),
            _rule(
                "gpt-5-mini", id="named", provider_name="router", effective_from=date(2026, 2, 1)
            ),
            _rule("gpt-5-mini", id="future", effective_from=date(2027, 1, 1), provider_name="x"),
        ]
    )
    pick = lambda name, model: match_rule(table, "openai", name, model, AT, defaults=())  # noqa: E731
    assert pick(None, "gpt-5-mini").id == "exact"
    assert pick("router", "gpt-5-mini").id == "named"  # provider 이름이 있는 규칙이 먼저
    assert pick(None, "gpt-5-nano").id == "prefix"
    assert pick("x", "gpt-5-mini").id == "exact"  # 아직 효력이 없다
    assert pick(None, "claude") is None


def test_an_admin_rule_beats_a_default_of_the_same_specificity() -> None:
    default = default_rules()
    gpt4o = next(r for r in default if r.model == "gpt-4o")
    admin = _rule("gpt-4o", id="admin", effective_from=gpt4o.effective_from)
    rule = match_rule(PricingTable(rules=[admin]), "openai", None, "gpt-4o", AT)
    assert rule.id == "admin"
    assert match_rule(PricingTable(), "openai", None, "gpt-4o", AT).id == gpt4o.id
    # 기본 가격은 모두 출처와 확인 날짜를 든다
    assert all(r.source.startswith("https://") and r.checked_at for r in default)


def test_cost_prices_cache_tokens_apart() -> None:
    rule = _rule("m", input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    usage = TokenUsage(
        input_tokens=1_000_000,
        cache_read_tokens=400_000,
        cache_write_tokens=100_000,
        output_tokens=200_000,
    )
    # 캐시를 뺀 50만 x3 + 40만 x0.3 + 10만 x3.75 + 20만 x15
    assert cost_of(rule, usage, "chat") == pytest.approx(1.5 + 0.12 + 0.375 + 3.0)
    # cache 값이 비면 input 가격이다
    assert cost_of(_rule("m", input=2.0, output=0), usage, "chat") == pytest.approx(2.0)
    search = _rule("brave", provider_kind="web_search", request=0.005)
    assert cost_of(search, TokenUsage(requests=4), "web_search") == pytest.approx(0.02)
    assert (
        cost_of(_rule("brave", provider_kind="web_search"), TokenUsage(requests=4), "web_search")
        is None
    )


# Claude 모델 id 와 그 기본 가격 (input, output, cache_read). 날짜가 붙은 id 와 앞머리가 겹치는
# id 를 함께 둔다. 출처는 https://platform.claude.com/docs/en/about-claude/pricing (2026-11-16)
CLAUDE_PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.2),
    "claude-fable-5-2-20261101": (8.0, 40.0, 0.2),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-fable-5-20260101": (10.0, 50.0, 1.0),  # 날짜 붙은 Fable 5 가 5.2 로 매겨지지 않는다
    "claude-mythos-5-2": (8.0, 40.0, 0.2),
    "claude-mythos-5-2-20261101": (8.0, 40.0, 0.2),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-mythos-5-20260401": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.2),
    "claude-opus-5": (5.0, 25.0, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 0.5),
    "claude-opus-4-7": (5.0, 25.0, 0.5),
    "claude-opus-4-6": (5.0, 25.0, 0.5),
    "claude-opus-4-5-20251101": (5.0, 25.0, 0.5),
    "claude-opus-4-1-20250805": (15.0, 75.0, 1.5),
    "claude-opus-4-20250514": (15.0, 75.0, 1.5),
    "claude-sonnet-5-5": (2.0, 10.0, 0.1),
    "claude-sonnet-5": (2.0, 10.0, 0.2),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3),
    "claude-sonnet-4-5-20250929": (3.0, 15.0, 0.3),
    "claude-sonnet-4-20250514": (3.0, 15.0, 0.3),
    "claude-haiku-5-5": (0.1, 0.5, 0.01),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.1),
}


@pytest.mark.parametrize("model", sorted(CLAUDE_PRICES))
def test_each_claude_id_gets_its_own_default_price(model: str) -> None:
    rule = match_rule(PricingTable(), "anthropic", None, model, AT)
    assert rule is not None
    assert (rule.input, rule.output, rule.cache_read) == CLAUDE_PRICES[model]
    # 5분 캐시 쓰기는 input 의 1.25 배다
    assert rule.cache_write == pytest.approx(rule.input * 1.25)


class _Rules:
    def __init__(self, *rules: PriceRule) -> None:
        self.table = PricingTable(rules=list(rules))

    async def get(self) -> PricingTable:
        return self.table


def _haiku_price(book: PriceBook, usage: TokenUsage):
    return asyncio.run(
        book.price(
            kind="anthropic",
            provider_name=None,
            model="claude-haiku-5-5",
            at=AT,
            usage=usage,
            operation="chat",
        )
    )


def test_a_long_haiku_prompt_pays_the_tier_prices_for_the_whole_request() -> None:
    book = PriceBook(_Rules())  # type: ignore[arg-type]
    # 문턱(10만)까지는 낮은 값이다
    cost, applied = _haiku_price(book, TokenUsage(input_tokens=100_000, output_tokens=1_000))
    assert cost == pytest.approx((100_000 * 0.1 + 1_000 * 0.5) / 1_000_000)
    assert (applied.input, applied.output) == (0.1, 0.5)
    # 캐시 읽기까지 세어 문턱보다 많으면 요청 전체가 높은 값이다
    long = TokenUsage(input_tokens=100_001, cache_read_tokens=80_000, output_tokens=1_000)
    cost, applied = _haiku_price(book, long)
    assert cost == pytest.approx((20_001 * 0.5 + 80_000 * 0.05 + 1_000 * 2.5) / 1_000_000)
    assert (applied.input, applied.output, applied.cache_read, applied.cache_write) == (
        0.5,
        2.5,
        0.05,
        0.625,
    )
    assert applied.rule_id == "default:anthropic::claude-haiku-5-5*:2024-01-01"


def test_an_admin_rule_with_a_tier_prices_long_prompts_at_its_tier() -> None:
    # 화면에서 기본 가격을 복사해 값을 고친 규칙: tier 를 그대로 들고 기본 가격을 이긴다
    default = next(r for r in default_rules() if r.model == "claude-haiku-5-5*")
    admin = PriceRule.model_validate(
        {
            **default.model_dump(include=set(PriceRule.model_fields)),
            "id": "negotiated",
            "input": 0.09,
            "tier": {**default.tier.model_dump(), "input": 0.45},
        }
    )
    book = PriceBook(_Rules(admin))  # type: ignore[arg-type]
    cost, applied = _haiku_price(book, TokenUsage(input_tokens=150_000))
    assert applied.rule_id == "negotiated"
    assert cost == pytest.approx(150_000 * 0.45 / 1_000_000)
    cost, applied = _haiku_price(book, TokenUsage(input_tokens=1_000))
    assert cost == pytest.approx(1_000 * 0.09 / 1_000_000)


def test_one_call_is_one_row_and_resends_count_as_duplicates(settings, mongo) -> None:
    """다시 보냄은 먼저 든 행이 남는다. 쓴 쪽이 다르면 사건을 적고 게이트웨이 행이 남으며, 부른 쪽
    정보는 런타임 행에서 잇는다."""
    gateway_usage = TokenUsage(input_tokens=2000, output_tokens=500)

    async def go(ledger: UsageLedger, _rollup, store: MongoUsageStore):
        first = await ledger.write(usage_row("tr-sp1", ref="s1"))
        again = await ledger.write(usage_row("tr-sp1", ref="s1"))
        source = UsageSource(kind="agent", id="a1", name="faq")
        gateway = usage_row("tr-sp1", writer=GATEWAY, record_id="g1", usage=gateway_usage)
        other = await ledger.write(gateway.model_copy(update={"source": source, "ref": None}))
        # 런타임 행이 나중에 와도 게이트웨이 행이 남는다
        await ledger.write(usage_row("tr-sp2", writer=GATEWAY, record_id="g2"))
        await ledger.write(usage_row("tr-sp2", ref="s2", usage=gateway_usage))
        rows = [await store.get_record(c) for c in ("tr-sp1", "tr-sp2")]
        return first, again, other, *rows

    first, again, other, row, later = run_ledger(settings, go)
    assert (first, again, other) == ("inserted", "duplicate", "writer_conflict")
    assert (row.writer, row.usage.input_tokens, row.joined, row.ref) == (GATEWAY, 2000, True, "s1")
    assert row.cost_usd == pytest.approx((2000 * 2.5 + 500 * 10.0) / 1_000_000)
    assert row.price.rule_id is not None
    assert (later.writer, later.usage.input_tokens, later.ref) == (GATEWAY, 1000, "s2")
    kinds = [d["kind"] for d in mongo["usage_anomalies"].find()]
    assert kinds == ["writer_conflicts", "writer_conflicts"]


def test_a_second_gateway_record_for_one_call_is_an_anomaly(settings, mongo) -> None:
    async def go(ledger: UsageLedger, _rollup, _store):
        a = await ledger.write(usage_row("c1", writer=GATEWAY, record_id="g1"))
        b = await ledger.write(usage_row("c1", writer=GATEWAY, record_id="g1"))
        c = await ledger.write(usage_row("c1", writer=GATEWAY, record_id="g2"))
        return a, b, c

    assert run_ledger(settings, go) == ("inserted", "duplicate", "gateway_extra")
    assert [d["kind"] for d in mongo["usage_anomalies"].find()] == ["gateway_extra_records"]


def test_a_join_naming_another_tenant_is_not_joined(settings, mongo) -> None:
    async def go(ledger: UsageLedger, _rollup, store: MongoUsageStore):
        await ledger.join(
            UsageJoin(id="c4", tenant="other", source=UsageSource(kind="agent", id="x"))
        )
        await ledger.write(usage_row("c4", writer=GATEWAY, record_id="g"))
        return await store.get_record("c4")

    row = run_ledger(settings, go)
    assert not row.joined and row.source.id == "a1"
    assert [d["kind"] for d in mongo["usage_anomalies"].find()] == ["join_mismatch"]


class _CrossedStore(MongoUsageStore):
    """두 쪽의 읽기를 서로 기다리게 한다. 게이트웨이 행과 잇기 문서가 동시에 와서 어느 쪽도 상대의
    쓰기 전에 읽지 못하는 엇갈림을 만든다."""

    barrier: asyncio.Barrier

    async def get_join(self, call_id: str) -> UsageJoin | None:
        await self.barrier.wait()
        return await super().get_join(call_id)

    async def get_record(self, record_id: str):
        await self.barrier.wait()
        return await super().get_record(record_id)


def test_a_gateway_row_and_its_join_arriving_together_still_join(settings, mongo) -> None:
    """두 쪽 모두 자기 문서를 쓴 뒤에 읽으므로, 읽기가 엇갈려도 행은 이어진다."""
    join = UsageJoin(id="c8", tenant="t1", source=UsageSource(kind="assistant", id="daegam"))

    async def go(ledger: UsageLedger, _rollup, store: MongoUsageStore):
        _CrossedStore.barrier = asyncio.Barrier(2)
        await asyncio.wait_for(
            asyncio.gather(
                ledger.write(usage_row("c8", writer=GATEWAY, record_id="g")),
                ledger.join(join),
            ),
            timeout=10,
        )
        return await MongoUsageStore.get_record(store, "c8")

    row = run_ledger(settings, go, store_cls=_CrossedStore)
    assert row.joined and row.source.kind == "assistant"
    assert mongo["usage_joins"].find_one({"_id": "c8"})["resolved"] is True
