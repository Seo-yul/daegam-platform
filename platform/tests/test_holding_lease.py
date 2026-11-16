"""`holding_lease` 가 쥔 리스를 놓는 자리.

나갈 때의 놓기는 취소를 막고 돈다. 놓기가 실패해도 범위의 결과를 가리지 않고 로그만 남긴다. 리스는
진짜 Mongo 다.
"""

from __future__ import annotations

import logging

import anyio
import pytest

from daegam.store.leases import LEASES, Lease, holding_lease


class Busy(Exception):
    pass


def _db(client):
    return client.app.state.backend_apis.db


def test_a_cancelled_scope_still_releases_the_lease(client, monkeypatch):
    db = _db(client)
    release = Lease.release

    async def release_after_a_checkpoint(self: Lease, name: str) -> None:
        # 취소가 닿는 자리. 막지 않으면 놓기 요청이 Mongo 에 가기 전에 취소된다
        await anyio.sleep(0)
        await release(self, name)

    monkeypatch.setattr(Lease, "release", release_after_a_checkpoint)

    async def run() -> None:
        with anyio.CancelScope() as scope:
            async with holding_lease(db, "pod-a", "demo", Busy()):
                scope.cancel()
                await anyio.sleep(5)

    client.portal.call(run)
    assert client.portal.call(db[LEASES].count_documents, {"_id": "demo"}) == 0


def test_a_failed_release_is_logged_and_keeps_the_result(client, monkeypatch, caplog):
    db = _db(client)

    async def contended() -> None:
        async with holding_lease(db, "pod-a", "demo", Busy()):
            async with holding_lease(db, "pod-b", "demo", Busy()):
                pass

    # 다른 곳이 쥔 리스는 받은 오류를 낸다
    with pytest.raises(Busy):
        client.portal.call(contended)

    async def broken_release(self: Lease, name: str) -> None:
        raise RuntimeError("mongo went away")

    async def run() -> str:
        async with holding_lease(db, "pod-a", "demo", Busy()):
            return "stored"

    monkeypatch.setattr(Lease, "release", broken_release)
    with caplog.at_level(logging.WARNING, logger="daegam.leases"):
        assert client.portal.call(run) == "stored"
    assert any("lease demo: release failed" in r.getMessage() for r in caplog.records)
