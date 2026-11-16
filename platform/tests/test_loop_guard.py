"""P69 (D81): K8s·Secret 저장소 대역은 이벤트 루프 스레드 위의 블로킹 호출을 거부한다."""

from __future__ import annotations

import asyncio

import pytest
from starlette.concurrency import run_in_threadpool

from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.loop_guard import BlockingCallOnLoop, allow_on_loop

SECRET = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "ns"}}


def test_a_direct_call_from_async_code_is_refused():
    fake, secrets = FakeClusterClient(), FakeSecretStore()

    async def direct() -> None:
        with pytest.raises(BlockingCallOnLoop, match="FakeClusterClient.apply"):
            fake.apply(SECRET)
        with pytest.raises(BlockingCallOnLoop, match="FakeSecretStore.get"):
            secrets.get("s", namespace="ns")

    asyncio.run(direct())


def test_the_threadpool_and_sync_code_pass():
    fake, secrets = FakeClusterClient(), FakeSecretStore()

    async def offloaded() -> None:
        await run_in_threadpool(fake.apply, SECRET)
        await run_in_threadpool(secrets.put, "s", {"k": "v"}, namespace="ns")

    asyncio.run(offloaded())
    assert fake.get("v1", "Secret", "s", "ns") is not None
    assert secrets.get("s", namespace="ns") == {"k": "v"}


def test_setup_and_non_io_members_pass_on_the_loop():
    """준비는 allow_on_loop 안에서 한다. I/O 가 없는 service_route 와 도우미는 그대로 된다."""
    fake = FakeClusterClient()

    async def setup() -> None:
        with allow_on_loop():
            fake.apply(SECRET)
        fake.service_route("ns", "svc", 8080)
        assert fake.find("Secret", "ns")
        with pytest.raises(BlockingCallOnLoop):
            fake.get("v1", "Secret", "s", "ns")

    asyncio.run(setup())


def test_a_subclass_override_stays_guarded():
    """테스트가 대역을 물려받아 메서드를 다시 정의해도 검사가 붙는다."""

    class Silent(FakeClusterClient):
        def get(self, *args, **kwargs):
            return None

    async def direct() -> None:
        with pytest.raises(BlockingCallOnLoop, match="Silent.get"):
            Silent().get("v1", "Secret", "s", "ns")

    asyncio.run(direct())
    assert Silent().get("v1", "Secret", "s", "ns") is None
