"""P2 F10(c): 되먹임 루프.
가짜 provider 로 되먹임·no_progress·budget·max_iterations·재개를 검증한다."""

from __future__ import annotations

import asyncio
import json

import pytest

from daegam import urlfetch
from daegam.services.llm_endpoint import Endpoint
from daegam.services.llm_providers import ProviderError
from daegam.services.spec_generator.loop import (
    build_first_prompt,
    check_draft,
    parse_draft,
)
from daegam.store.spec_jobs import Iteration, SpecJob, draft_name, input_name
from tests.helpers.aio import run_stores
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.spec_jobs import (
    LOOP_STORES,
    j,
    loop_runner,
    worker,
)

# -- 초안 픽스처 ------------------------------------------------------------------------


def _doc(ops: dict[str, dict]) -> dict:
    paths = {}
    for path, op in ops.items():
        paths[path] = {"get": {"responses": {"200": {"description": "OK"}}, **op}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0.0"},
        "servers": [{"url": "https://pets.example.com"}],
        "paths": paths,
    }


GOOD = _doc(
    {
        "/pets": {"operationId": "listPets", "description": "List all pets."},
        "/pets/{id}": {
            "operationId": "getPet",
            "description": "Get one pet.",
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
        },
    }
)
# operationId 가 툴 이름 규칙을 어기면 tool-name.safe fail
BAD_NAME = _doc({"/pets": {"operationId": "list pets!", "description": "List all pets."}})
# operationId 가 없으면 tool-name.present fail
BAD_MISSING = _doc({"/pets": {"description": "List all pets."}})
# 두 툴이 같은 이름이면 tool-name.unique fail
BAD_DUP = _doc(
    {
        "/a": {"operationId": "same", "description": "a"},
        "/b": {"operationId": "same", "description": "b"},
    }
)


# -- 도우미 -----------------------------------------------------------------------------


async def make_job(store, files, inp: dict, hints: dict | None = None) -> SpecJob:
    job = SpecJob(tenant="default", provider="oai", input_ref="", hints=hints or {})
    job.input_ref = await files.put(
        input_name(job.id), json.dumps(inp).encode(), job_id=job.id, role="input"
    )
    await store.create(job)
    return job


# -- 단위 -------------------------------------------------------------------------------


def test_parse_draft_variants():
    assert parse_draft('{"a": 1}') == {"a": 1}
    assert parse_draft('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_draft('Here you go:\n{"a": {"b": 2}}\nDone.') == {"a": {"b": 2}}
    for bad in ("", "[1, 2]", "not json at all", "{broken"):
        with pytest.raises(ValueError):
            parse_draft(bad)


def test_check_draft_levels():
    c = asyncio.run(check_draft(j(GOOD)))
    assert c.ok and c.error_ids == [] and c.findings == []
    assert c.report_summary["ok"] is True and c.report_summary["complete"] is True

    c = asyncio.run(check_draft(j(BAD_NAME)))
    assert not c.ok
    assert "tool-name.safe" in c.error_ids
    assert any(
        f["id"] == "tool-name.safe" and "paths./pets.get" in f["location"] for f in c.findings
    )

    c = asyncio.run(check_draft("nope"))
    assert not c.ok and c.error_ids == ["output.json"] and c.report is None

    # warn 만 있으면 통과 (F9 verdict 규칙과 같다)
    warn_only = _doc({"/pets": {"operationId": "listPets"}})  # description 없음 → warn
    c = asyncio.run(check_draft(j(warn_only)))
    assert c.ok and c.verdict.level == "warn"


def test_first_prompt_contents():
    p = build_first_prompt(
        {
            "text": "GET /pets lists pets",
            "examples": [{"request": {"id": 1}, "response": {"name": "rex"}}],
            "partial_spec": {"openapi": "3.0.3"},
            "url": "http://93.184.216.34/x",
        },
        {"title": "Pets", "base_url": "https://pets.example.com"},
        url_content="fetched body",
    )
    for needle in (
        "GET /pets lists pets",
        '"id": 1',
        '"name": "rex"',
        '"openapi": "3.0.3"',
        "https://pets.example.com",
        "Title: Pets",
        "fetched body",
        "http://93.184.216.34/x",
    ):
        assert needle in p


# -- 루프 시나리오 -----------------------------------------------------------------------


def test_different_errors_are_progress_until_max_iterations(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider([j(BAD_NAME), j(BAD_MISSING), j(BAD_DUP), j(BAD_NAME)])
        runner = await loop_runner(settings, ps, fake, max_iterations=3)
        job = await make_job(store, files, {"text": "x"})
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "failed" and got.error["code"] == "max_iterations"
        assert [it.n for it in got.iterations] == [1, 2, 3]
        assert len(fake.calls) == 3

    run_stores(settings, LOOP_STORES, main)


def test_budget(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider(
            [j(BAD_NAME), j(BAD_MISSING), j(GOOD)],
            usage_per_call={"input_tokens": 600, "output_tokens": 100},
        )
        runner = await loop_runner(settings, ps, fake, token_budget=1000)
        job = await make_job(store, files, {"text": "x"})
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "failed" and got.error["code"] == "budget"
        # 1회 700 < 1000 → 2회째 호출, 누적 1400 > 1000 → budget
        assert [it.n for it in got.iterations] == [1, 2]
        assert got.usage_total == {"input_tokens": 1200, "output_tokens": 200}

    run_stores(settings, LOOP_STORES, main)


def test_resume_after_worker_death_continues_feedback(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider([j(GOOD)])
        runner = await loop_runner(settings, ps, fake)
        job = await make_job(store, files, {"text": "x"})
        # 죽은 파드가 반복 1(실패 초안)을 남기고 리스가 만료됐다
        await store.acquire("dead", lease_seconds=-1)
        ref = await files.put(
            draft_name(job.id, 1), j(BAD_NAME).encode(), job_id=job.id, role="draft"
        )
        await store.push_iteration(
            job.id,
            "dead",
            Iteration(n=1, draft_ref=ref, failed_ids=["tool-name.safe"], usage={"input_tokens": 5}),
            usage_total={"input_tokens": 5},
        )
        assert await worker(store, files, runner, "w2").tick() == 1
        got = await store.get(job.id)
        assert got.state == "done" and got.attempt == 2
        assert [it.n for it in got.iterations] == [1, 2]
        # 새 워커는 입력 원문 대신 직전 초안 + 실패 항목으로 시작한다
        assert len(fake.calls) == 1
        fb = fake.calls[0]["messages"][0]["content"]
        assert "[tool-name.safe]" in fb and "list pets!" in fb
        assert got.usage_total == {"input_tokens": 1005, "output_tokens": 500}

    run_stores(settings, LOOP_STORES, main)


def test_resume_when_last_draft_already_passes(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider([j(BAD_NAME)])
        runner = await loop_runner(settings, ps, fake)
        job = await make_job(store, files, {"text": "x"})
        await store.acquire("dead", lease_seconds=-1)
        ref = await files.put(draft_name(job.id, 1), j(GOOD).encode(), job_id=job.id, role="draft")
        await store.push_iteration(
            job.id, "dead", Iteration(n=1, draft_ref=ref, failed_ids=[]), usage_total={}
        )
        await worker(store, files, runner, "w2").tick()
        got = await store.get(job.id)
        assert got.state == "done" and got.draft_ref == ref
        assert fake.calls == []  # LLM 을 다시 부르지 않는다

    run_stores(settings, LOOP_STORES, main)


def test_provider_retry_and_failure(settings, mongo):
    async def main(store, files, ps):
        retry = ProviderError("provider_rate_limited", "slow down", retryable=True)
        fake = FakeProvider([retry, j(GOOD)])
        runner = await loop_runner(settings, ps, fake)
        job = await make_job(store, files, {"text": "x"})
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "done" and len(fake.calls) == 2

        fatal = FakeProvider([ProviderError("auth", "bad key")])
        runner.provider_factory = lambda cfg, endpoint: fatal
        job2 = await make_job(store, files, {"text": "y"})
        await worker(store, files, runner, "w1").tick()
        got2 = await store.get(job2.id)
        assert got2.state == "failed" and got2.error == {"code": "auth", "detail": "bad key"}
        assert got2.iterations == []

    run_stores(settings, LOOP_STORES, main)


def test_a_retry_takes_the_route_of_its_own_attempt(settings, mongo):
    """길은 시도마다 고른다 (P38 6절). 게이트웨이에 연결하지 못해 다시 보내는 사이 게이트웨이를
    끄면 다음 시도는 직접 경로로 가서 job 이 끝난다."""

    async def main(store, files, ps):
        gateway_down = ProviderError("llm_gateway_unreachable", "no pod", retryable=True)
        fake = FakeProvider([j(GOOD)])
        routes = iter(["gateway", "direct"])
        built: list[str] = []

        class Switching:
            async def resolve_endpoint(self, cfg, *, tenant, purpose):
                route = next(routes)
                base = "http://llm-gateway/p/x/v1" if route == "gateway" else None
                return Endpoint(base, "", None, route)

        def factory(cfg, endpoint):
            built.append(endpoint.route)
            return FakeProvider([gateway_down]) if endpoint.gateway else fake

        runner = await loop_runner(settings, ps, fake)
        runner.endpoints = Switching()  # type: ignore[assignment]
        runner.provider_factory = factory
        job = await make_job(store, files, {"text": "x"})
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert built == ["gateway", "direct"] and len(fake.calls) == 1

    run_stores(settings, LOOP_STORES, main)


def test_url_input_is_fetched_and_blocked_urls_fail(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider([j(GOOD)])
        fetched: list[str] = []

        async def fetch(url: str) -> bytes:
            fetched.append(url)
            if "blocked" in url:
                raise urlfetch.BlockedURL("url_private", "resolved to 10.0.0.1")
            return b"paths:\n  /pets:\n    get: {}\n"

        runner = await loop_runner(settings, ps, fake, fetch=fetch)
        job = await make_job(store, files, {"url": "http://93.184.216.34/openapi.yaml"})
        await worker(store, files, runner, "w1").tick()
        assert (await store.get(job.id)).state == "done"
        assert fetched == ["http://93.184.216.34/openapi.yaml"]
        assert "paths:\n  /pets:" in fake.calls[0]["messages"][0]["content"]

        job2 = await make_job(store, files, {"url": "http://blocked.example.com/x"})
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job2.id)
        assert got.state == "failed" and got.error["code"] == "url_private"

    run_stores(settings, LOOP_STORES, main)


def test_missing_provider_fails_job(settings, mongo):
    async def main(store, files, ps):
        fake = FakeProvider([j(GOOD)])
        runner = await loop_runner(settings, ps, fake)
        job = SpecJob(tenant="default", provider="ghost", input_ref="")
        job.input_ref = await files.put(input_name(job.id), b"{}", job_id=job.id, role="input")
        await store.create(job)
        await worker(store, files, runner, "w1").tick()
        got = await store.get(job.id)
        assert got.state == "failed" and got.error["code"] == "no_provider"

    run_stores(settings, LOOP_STORES, main)


# -- API 끝까지 --------------------------------------------------------------------------
