"""플랫폼 호출 자리의 원장 (P37 7절): 직접 경로의 행, 게이트웨이 경로의 잇기 문서, 비서 턴."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from daegam.llm_http import current_call
from daegam.services.usage.ledger import CallSite, UsageLedger
from daegam.store.settings import LLMProvider
from daegam.store.usage import UsageSource
from tests.helpers.usage import run_ledger

GW = "http://llm-gw.gw.svc.cluster.local:8080"


def _site(base_url: str | None = None) -> CallSite:
    return CallSite(
        tenant="t1",
        source=UsageSource(kind="spec_generator", id="generate", name="generate"),
        provider=LLMProvider(name="p", kind="openai", base_url=base_url),
        model="gpt-4o",
        ref="job-1",
        user_sub="u1",
    )


def _run(settings, body):
    async def go(ledger: UsageLedger, _rollup, _store):
        return await body(ledger)

    return run_ledger(settings, go, gateway_url=f"{GW}/v1")


def test_a_direct_call_leaves_one_priced_row(settings, mongo) -> None:
    site = _site()

    async def body(ledger: UsageLedger):
        ids = []
        for _ in range(2):  # 같은 일의 두 호출은 trace 하나에 id 둘이다
            async with ledger.call(site) as meter:
                ids.append(current_call().id)
                meter.report({"input_tokens": 100, "output_tokens": 10})
        return ids

    ids = _run(settings, body)
    rows = list(mongo["usage_records"].find())
    assert sorted(r["_id"] for r in rows) == sorted(ids) and len(set(ids)) == 2
    assert {r["trace_id"] for r in rows} == {site.trace_id}
    row = rows[0]
    assert row["writer"] == "platform" and row["route"] == "direct"
    assert row["source"]["kind"] == "spec_generator" and row["ref"] == "job-1"
    assert row["user_sub"] == "u1" and row["cost_usd"] > 0 and row["latency_ms"] >= 0


def test_the_route_follows_the_address_the_caller_chose(settings, mongo) -> None:
    """플랫폼 호출자가 고른 길(P38 6절)의 주소가 경로를 정한다. provider 의 주소가 직접이어도
    게이트웨이 경로로 부른 호출은 행 대신 잇기 문서다."""
    site = dataclasses.replace(_site(), base_url=f"{GW}/v1/p/p/v1")

    async def body(ledger: UsageLedger):
        assert ledger.route_of(site) == "gateway" and ledger.route_of(_site()) == "direct"
        async with ledger.call(site) as meter:
            meter.report({"input_tokens": 1})

    _run(settings, body)
    assert mongo["usage_records"].count_documents({}) == 0
    assert mongo["usage_joins"].count_documents({}) == 1


def test_a_gateway_call_leaves_a_join_and_cut_or_failed_ones_are_marked(settings, mongo) -> None:
    site = _site(f"{GW}/v1/openai")

    async def body(ledger: UsageLedger):
        async with ledger.call(site) as meter:
            meter.report({"input_tokens": 1})
        with pytest.raises(RuntimeError):
            async with ledger.call(site):
                raise RuntimeError("gateway refused")

        async def cut():
            async with ledger.call(site):
                await asyncio.sleep(10)

        task = asyncio.create_task(cut())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(settings, body)
    assert mongo["usage_records"].count_documents({}) == 0
    joins = list(mongo["usage_joins"].find())
    assert sorted((j["cancelled"], j["failed"]) for j in joins) == [
        (False, False),
        (False, True),
        (True, False),
    ]
    assert all(j["ref"] == "job-1" and not j["resolved"] for j in joins)
