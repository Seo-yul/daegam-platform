"""P2 F10(a): Spec Generator job 문서·리스·워커 루프와 job API."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta

from daegam.services.spec_generator.worker import JobContext, Outcome
from daegam.store import spec_jobs
from daegam.store.base import utcnow
from daegam.store.spec_jobs import (
    Iteration,
    LeaseLost,
    MongoSpecJobStore,
    SpecJob,
    SpecJobFiles,
    input_name,
)
from tests.helpers.aio import run_stores
from tests.helpers.consts import JOBS, LLM_KEY_SECRET, PLATFORM_NS, T2
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.spec_jobs import PROVIDERS, STORES, post_job, worker

# -- 도우미 ---------------------------------------------------------------------


async def new_job(store: MongoSpecJobStore, files: SpecJobFiles, tenant="default") -> SpecJob:
    job = SpecJob(tenant=tenant, provider="oai", input_ref="")
    job.input_ref = await files.put(
        input_name(job.id), b'{"text": "hello"}', job_id=job.id, role="input"
    )
    await store.create(job)
    return job


@dataclass
class FakeRunner:
    """반복 하나를 기록하고 done. delay 로 오래 걸리는 척, fail 로 예외를 낸다."""

    delay: float = 0.0
    fail: bool = False
    runs: list[tuple[str, str, int, int]] = field(default_factory=list)

    async def run(self, ctx: JobContext) -> Outcome:
        job = ctx.job
        self.runs.append((job.id, ctx.owner, len(job.iterations), job.attempt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        n = len(job.iterations) + 1
        ref = await ctx.save_draft(n, json.dumps({"openapi": "3.0.3", "n": n}).encode())
        await ctx.push_iteration(
            Iteration(n=n, draft_ref=ref, usage={"input_tokens": 10, "output_tokens": 5})
        )
        return Outcome(state="done", draft_ref=ref, report={"ok": True}, verdict={"level": "ok"})


# -- 저장소·리스 ------------------------------------------------------------------


def test_acquire_only_queued_or_expired(settings, mongo):
    async def main(store, files):
        job = await new_job(store, files)
        got = await store.acquire("w1", 60)
        assert got is not None and got.id == job.id
        assert got.state == "running" and got.attempt == 1 and got.lease.owner == "w1"
        # 리스가 살아 있으니 다른 워커는 못 잡는다
        assert await store.acquire("w2", 60) is None
        # 리스를 만료시키면 잡힌다
        await store.coll.update_one({"_id": job.id}, {"$set": {"lease.until": utcnow()}})
        await asyncio.sleep(0.01)
        stolen = await store.acquire("w2", 60)
        assert stolen is not None and stolen.lease.owner == "w2" and stolen.attempt == 2
        # 옛 소유자의 쓰기는 전부 거부된다
        assert await store.renew(job.id, "w1", 60) is False
        try:
            await store.push_iteration(job.id, "w1", Iteration(n=1), usage_total={})
            raise AssertionError("expected LeaseLost")
        except LeaseLost:
            pass
        try:
            await store.finish(job.id, "w1", state="done", ttl_seconds=10)
            raise AssertionError("expected LeaseLost")
        except LeaseLost:
            pass
        # 새 소유자는 끝낼 수 있고, TTL 시각이 찍힌다
        done = await store.finish(job.id, "w2", state="done", ttl_seconds=100)
        assert done.state == "done" and done.lease is None
        assert 90 < (done.expires_at - utcnow()).total_seconds() <= 100
        assert await store.acquire("w3", 60) is None
        assert await store.count_active("default") == 0
        # 오래된 것부터 잡고, 활성 수와 읽기는 테넌트마다다
        a = await new_job(store, files)
        await asyncio.sleep(0.005)
        b = await new_job(store, files, tenant="other")
        assert await store.count_active("default") == 1
        assert await store.count_active("other") == 1
        assert (await store.acquire("w", 60)).id == a.id
        assert (await store.acquire("w", 60)).id == b.id
        assert await store.get(a.id, tenant="other") is None
        assert (await store.get(a.id, tenant="default")).state == "running"

    run_stores(settings, STORES, main)


# -- 워커 --------------------------------------------------------------------------


def test_runner_exception_marks_failed(settings, mongo):
    async def main(store, files):
        job = await new_job(store, files)
        assert await worker(store, files, FakeRunner(fail=True), "w1", ttl_seconds=50).tick() == 1
        got = await store.get(job.id)
        assert got.state == "failed"
        assert got.error["code"] == "runner_error" and "boom" in got.error["detail"]
        assert got.expires_at is not None and got.lease is None

    run_stores(settings, STORES, main)


def test_heartbeat_keeps_lease_while_running(settings, mongo, monkeypatch):
    """도는 동안 하트비트가 리스를 늘린다. 스토어에 시계를 주입하므로 실제로 기다리지 않는다. 원래
    리스가 지난 시각에 w2 가 잡으려 해도 늘어난 리스라 잡지 못한다."""
    lease = timedelta(seconds=0.3)
    clock = [utcnow()]
    monkeypatch.setattr(spec_jobs, "utcnow", lambda: clock[0])

    async def main(store, files):
        job = await new_job(store, files)
        runner = FakeRunner()
        w2 = worker(store, files, runner, "w2", lease_seconds=lease.total_seconds())
        late: list[int] = []

        class PastTheLease:
            """w1 이 job 을 쥔 채로 시계를 원래 리스 너머로 옮기고 w2 를 돌린다."""

            async def run(self, ctx: JobContext) -> Outcome:
                original = ctx.job.lease.until
                clock[0] += lease / 2  # 하트비트가 이 시각을 기준으로 리스를 늘린다
                deadline = time.monotonic() + 5
                while (await store.get(ctx.job.id)).lease.until <= original:
                    assert time.monotonic() < deadline, "하트비트가 리스를 늘리지 않았다"
                    await asyncio.sleep(0.01)
                clock[0] = original + lease / 4  # 원래 리스는 지났고 늘린 리스는 남았다
                late.append(await w2.tick())
                return await runner.run(ctx)

        w1 = worker(store, files, PastTheLease(), "w1", lease_seconds=lease.total_seconds())
        assert await w1.tick() == 1
        # runner 안의 단정이 실패하면 워커가 job 을 failed 로 접으므로 그 error 를 먼저 본다
        got = await store.get(job.id)
        assert got.state == "done", got.error
        assert late == [0]
        assert [r[1] for r in runner.runs] == ["w1"]

    run_stores(settings, STORES, main)


def test_lease_lost_mid_run_is_not_finished_twice(settings, mongo):
    async def main(store, files):
        job = await new_job(store, files)

        class Slow:
            async def run(self, ctx):
                # 도는 사이에 리스를 빼앗긴다
                await store.coll.update_one({"_id": ctx.job.id}, {"$set": {"lease.owner": "thief"}})
                await ctx.push_iteration(Iteration(n=1))  # LeaseLost
                raise AssertionError("unreachable")

        await worker(store, files, Slow(), "w1").tick()
        got = await store.get(job.id)
        assert got.state == "running" and got.lease.owner == "thief"
        assert got.iterations == []

    run_stores(settings, STORES, main)


# -- API ------------------------------------------------------------------------


def test_job_requires_provider(client):
    r = post_job(client, input={"text": "GET /pets returns a list"})
    assert detail_code(r) == (422, "no_provider")
    assert client.patch("/settings", json=PROVIDERS).status_code == 200
    r = post_job(client, input={"text": "x"}, provider="nope")
    assert detail_code(r) == (422, "unknown_provider")


def test_job_requires_provider_key(client, fake_secrets):
    """provider 는 있어도 키 경로(Secret)가 비어 있으면 422 provider_no_key. 응답에는 provider
    이름과 Secret 이름만 있고 키 값은 없다."""
    fake_secrets.put("half", {"a": "sk-leak-a", "b": "sk-leak-b"}, namespace=PLATFORM_NS)
    providers = [
        {"name": "nokey", "kind": "openai"},
        {"name": "ghost", "kind": "anthropic", "api_key_secret": "missing"},
        {"name": "half", "kind": "openai", "api_key_secret": "half"},
        {"name": "oai", "kind": "openai", "api_key_secret": LLM_KEY_SECRET},
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200
    for name, secret in [("nokey", None), ("ghost", "missing"), ("half", "half")]:
        r = post_job(client, input={"text": "x"}, provider=name)
        assert r.status_code == 422, r.text
        d = r.json()["detail"]
        assert d["code"] == "provider_no_key" and d["provider"] == name and d["secret"] == secret
        assert f"'{name}'" in d["detail"] and "sk-" not in r.text
    assert post_job(client, input={"text": "x"}, provider="oai").status_code == 202
    # 기본 provider(첫 항목)도 같은 검사를 거친다
    r = post_job(client, input={"text": "x"})
    assert r.status_code == 422 and r.json()["detail"]["provider"] == "nokey"


def test_job_requires_secret_store(settings, mongo, fake_clusters):
    """home 클러스터가 없어 Secret 저장소가 없으면 422 no_secret_store."""
    app = make_app(settings, fake_clusters, None)
    with api_client(app) as client:
        assert client.patch("/settings", json=PROVIDERS).status_code == 200
        r = post_job(client, input={"text": "x"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "no_secret_store"
        assert r.json()["detail"]["provider"] == "oai"


def test_job_lifecycle_queued(client, mongo):
    client.patch("/settings", json=PROVIDERS)
    r = post_job(client, input={"text": "GET /pets"}, hints={"title": "Pets"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["state"] == "queued" and body["provider"] == "oai"
    assert body["hints"] == {"title": "Pets"}
    assert body["iterations"] == [] and body["attempt"] == 0
    jid = body["id"]

    got = client.get(f"{JOBS}/{jid}").json()
    assert got["state"] == "queued" and got["draft"] is None
    assert [j["id"] for j in client.get(JOBS).json()] == [jid]
    # 입력은 문서가 아니라 GridFS 에 있다
    doc = mongo["spec_jobs"].find_one({"_id": jid})
    assert doc["input_ref"] == f"{jid}/input.json" and "text" not in json.dumps(doc, default=str)
    stored = mongo["spec_jobs.files"].find_one({"filename": f"{jid}/input.json"})
    assert stored["metadata"] == {"job_id": jid, "role": "input"}
    # 테넌트 격리
    assert client.get(f"{JOBS}/{jid}", headers=T2).status_code == 404
    assert client.get(JOBS, headers=T2).json() == []
    # 진행 중이면 못 지운다
    assert client.delete(f"{JOBS}/{jid}").status_code == 409
    mongo["spec_jobs"].update_one({"_id": jid}, {"$set": {"state": "failed"}})
    assert client.delete(f"{JOBS}/{jid}").status_code == 200
    assert client.get(f"{JOBS}/{jid}").status_code == 404
    assert mongo["spec_jobs.files"].count_documents({"metadata.job_id": jid}) == 0


def test_job_input_validation(client):
    client.patch("/settings", json=PROVIDERS)
    r = post_job(client, input={})
    assert detail_code(r) == (422, "empty_input")
    r = post_job(client, input={"text": "   "})
    assert detail_code(r) == (422, "empty_input")
    assert post_job(client, input={"bogus": 1}).status_code == 422
    r = post_job(client, input={"text": "x" * (256 * 1024 + 1)})
    assert detail_code(r) == (422, "input_too_large")
    for url in (
        "http://10.0.0.1/spec.yaml",
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:9000/",
        "http://api.default.svc/openapi.json",
        "http://x.cluster.local/",
        "ftp://93.184.216.34/x",
        "http://user:pw@93.184.216.34/x",
    ):
        r = post_job(client, input={"url": url})
        assert r.status_code == 422, url
        assert r.json()["detail"]["code"] == "url_blocked", url
    r = post_job(client, input={"url": "http://93.184.216.34/openapi.yaml"})
    assert r.status_code == 202, r.text
    r = post_job(client, input={"examples": [{"request": {"a": 1}, "response": {"b": 2}}]})
    assert r.status_code == 202, r.text


def test_job_concurrency_limit(client):
    client.patch("/settings", json=PROVIDERS)
    assert post_job(client, input={"text": "a"}).status_code == 202
    assert post_job(client, input={"text": "b"}).status_code == 202
    r = post_job(client, input={"text": "c"})
    assert detail_code(r) == (429, "too_many_jobs")
    # 다른 테넌트는 별도 상한
    assert post_job(client, input={"text": "d"}, headers=T2).status_code == 202


def test_settings_provider_validation(client):
    r = client.patch(
        "/settings",
        json={
            "llm_providers": [{"name": "a", "kind": "openai"}, {"name": "a", "kind": "anthropic"}]
        },
    )
    assert detail_code(r) == (422, "invalid_value")
    r = client.patch("/settings", json={"defaults": {"spec_generator": "ghost"}})
    assert r.status_code == 422
    r = client.patch("/settings", json={"llm_providers": [{"name": "a", "kind": "mistral"}]})
    assert r.status_code == 422
    r = client.patch(
        "/settings",
        json={
            "llm_providers": [
                {"name": "a", "kind": "anthropic", "api_key_secret": "llm-keys"},
                {"name": "gw", "kind": "openai", "model": "m", "base_url": "http://gw/v1"},
            ],
            "defaults": {"spec_generator": "gw"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["defaults"] == {"spec_generator": "gw", "assistant": None}
    assert r.json()["llm_providers"][0]["model"] is None
    assert client.get("/settings").json()["llm_providers"][1]["base_url"] == "http://gw/v1"
