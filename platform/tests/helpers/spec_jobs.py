"""Spec Generator job 을 만드는 테스트가 함께 쓰는 도우미 (D25, P2, P11).

job 은 API(`post_job`)로 만들거나 스토어에 바로 넣는다. 워커는 짧은 주기로 돌고(`worker`), 되먹임
루프는 가짜 provider 를 꽂은 runner 다(`loop_runner`). 앱 전체로 도는 job 은 워커를 켠 앱의
클라이언트(`worker_client`)로 만들고 끝날 때까지 기다린다(`wait_done`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from daegam.config import Settings
from daegam.services.llm_endpoint import Endpoint
from daegam.services.spec_generator.loop import SpecGeneratorRunner
from daegam.services.spec_generator.worker import SpecGeneratorWorker
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore
from daegam.store.spec_jobs import MongoSpecJobStore, SpecJobFiles
from tests.helpers.consts import JOBS, LLM_KEY_SECRET
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.http import api_client, make_app

# 키 Secret 은 conftest 의 fake_secrets 가 미리 넣어 둔다 (키 경로는 Secret 뿐이다, D25)
PROVIDERS = {
    "llm_providers": [{"name": "oai", "kind": "openai", "api_key_secret": LLM_KEY_SECRET}],
    "defaults": {"spec_generator": "oai"},
}
# 잡 스토어·파일과, 거기에 runner 가 provider 를 읽는 플랫폼 설정을 더한 것
STORES = (MongoSpecJobStore, SpecJobFiles)
LOOP_STORES = (*STORES, MongoPlatformSettingsStore)
# petstore-swagger2.json 의 getPetById 오퍼레이션을 가리키는 JSON Pointer
GET_PET = "/paths/~1pet~1{petId}/get"


def j(doc) -> str:
    """가짜 provider 가 돌려줄 응답 글. 문서를 JSON 으로 적는다."""
    return json.dumps(doc)


def post_job(client, headers=None, **body):
    return client.post(JOBS, json=body, headers=headers or {})


def refine_body(pid: str, **kw) -> dict:
    """Backend API pid 의 v1 을 설명 보강 프리셋으로 다듬는 refine job 본문."""
    return {
        "mode": "refine",
        "source": {"backend_profile_id": pid, "version": 1},
        "presets": ["enrich_descriptions"],
        **kw,
    }


def worker(store, files, runner, owner, **kw) -> SpecGeneratorWorker:
    kw.setdefault("lease_seconds", 5.0)
    kw.setdefault("ttl_seconds", 3600)
    return SpecGeneratorWorker(store, files, runner, owner=owner, interval=0.01, **kw)


class KeylessEndpoints:
    """Secret 저장소 없는 runner 의 길. 가짜 provider 는 키를 보지 않으므로 직접 경로를 키 없이
    돌려준다 (P38 6절 `LlmEndpoints`)."""

    async def resolve_endpoint(self, cfg: LLMProvider, *, tenant: str, purpose: str) -> Endpoint:
        return Endpoint(cfg.effective_base_url, "", None, "direct")


async def loop_runner(
    settings: Settings, ps: MongoPlatformSettingsStore, fake: FakeProvider, **runner_kw
) -> SpecGeneratorRunner:
    """provider 설정을 넣고 가짜 provider 를 꽂은 runner."""
    await ps.update(PROVIDERS)
    return SpecGeneratorRunner(
        ps,
        None,
        settings,
        provider_factory=lambda cfg, endpoint: fake,
        retry_delay=0.01,
        endpoints=KeylessEndpoints(),  # type: ignore[arg-type]
        **runner_kw,
    )


@contextmanager
def worker_client(
    settings: Settings, fake_clusters, fake_secrets, fake: FakeProvider, **kw: Any
) -> Iterator[TestClient]:
    """Spec Generator 워커를 짧은 주기로 켠 앱의 클라이언트. 앱이 만든 실제 runner 에 가짜 provider
    fake 만 꽂는다. kw 는 앱 설정의 필드다."""
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        spec_worker_enabled=True,
        spec_worker_interval=0.02,
        **kw,
    )
    with api_client(app) as client:
        app.state.spec_worker.runner.provider_factory = lambda cfg, endpoint: fake
        yield client


def wait_done(client, jid: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = client.get(f"{JOBS}/{jid}").json()
        if got["state"] in ("done", "failed"):
            return got
        time.sleep(0.05)
    raise AssertionError(f"job {jid} 이 끝나지 않았다: {got}")
