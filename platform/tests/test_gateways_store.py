"""Gateway 스토어 (D42) 만의 것. 세 인스턴스 스토어가 함께 지키는 계약(draft 빼기, 조건부 저장,
저장 단위)은 test_instance_stores.py 가 본다.

정의 쪽 질의(`using_hub_item`)는 draft 도 본다.
"""

from __future__ import annotations

from typing import Any

from daegam.store.gateways import Gateway, GatewayBackendSnapshot, MongoGatewayStore
from tests.helpers.aio import run_store
from tests.helpers.docs import gateway_doc

SPEC = {"backends": [{"hub_item_id": "hub-1", "tools": ["Add"]}], "failure_mode": "fail"}


def _gateway(gid: str, *, hub_item: str = "hub-1", **kw: Any) -> Gateway:
    """허브 항목 하나를 쓰는 Gateway. 배포된 것은 배포 스냅샷도 그 항목을 가리킨다."""
    g = gateway_doc(gid, spec={**SPEC, "backends": [{"hub_item_id": hub_item}]}, **kw)
    if g.is_deployed:
        g.deployed_backends = [
            GatewayBackendSnapshot(
                hub_item_id=hub_item,
                hub_item_name="item",
                kind="internal",
                workload=f"gw-{g.name}-item",
                url="http://mcp-item.daegam-acme.svc:8080/mcp",
            )
        ]
    return g


def test_holding_entries_is_another_gateway_that_holds_the_name(settings):
    """배포 스냅샷의 Entry 이름이 names 와 겹치고 그 이름을 쥐는 상태(deploying·deployed·
    undeploying·deleting)인 다른 문서 하나다. 자기 자신, draft, 실패한 문서(적용한 이름은 선점이
    막는다), 다른 클러스터·네임스페이스의 문서와 겹치지 않는 이름은 None 이다. 리비전 이력은 읽지
    않는다."""

    def holding(gid: str, state: str, workload: str) -> Gateway:
        g = _gateway(gid, state="deployed")
        g.deployed_backends[0].workload = workload
        g.state = state
        return g

    async def go(store):
        for gid, state in (("draft-1", "draft"), ("failed-1", "failed")):
            await store.create(holding(gid, state, "shop-a-orders"))
        ns = _gateway("x", state="deployed").k8s.namespace
        not_held = await store.holding_entries("c1", ns, {"shop-a-orders"}, exclude_id="mine")
        await store.create(holding("held", "deployed", "shop-a-orders"))
        await store.create(holding("leaving", "undeploying", "shop-b-orders"))
        found = [
            await store.holding_entries("c1", ns, {"shop-a-orders", "x"}, exclude_id="mine"),
            await store.holding_entries("c1", ns, {"shop-b-orders"}, exclude_id="mine"),
        ]
        misses = [
            await store.holding_entries("c1", ns, {"shop-a-orders"}, exclude_id="held"),
            await store.holding_entries("c2", ns, {"shop-a-orders"}, exclude_id="mine"),
            await store.holding_entries("c1", "daegam-other", {"shop-a-orders"}, exclude_id="m"),
            await store.holding_entries("c1", ns, {"x"}, exclude_id="mine"),
            await store.holding_entries("c1", ns, set(), exclude_id="mine"),
        ]
        return not_held, found, misses

    not_held, found, misses = run_store(settings, MongoGatewayStore, go)
    assert not_held is None
    assert [g.id for g in found] == ["held", "leaving"] and found[0].revisions == []
    assert misses == [None] * 5
