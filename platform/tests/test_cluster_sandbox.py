"""Cluster 의 `sandbox` 칸과 그 쓰기 길 (P40 10절).

사람의 저장은 `sandbox.image`·`sandbox.scheduling` 만 점 경로로 쓰고 판정(`sandbox.check`)은 쓰지
않는다. 판정은 점검·무효·허용·스윕의 칸 쓰기만 쓴다. home 의 이미지는 `DAEGAM_HOME_SANDBOX_IMAGE`
에서 오고, 앞의 판이 같은 문서를 읽을 수 있어야 한다(호환 시험).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from daegam.config import settings_from_env
from daegam.store.base import to_millis, utcnow
from daegam.store.clusters import (
    CLUSTERS,
    HOME_CLUSTER_NAME,
    Cluster,
    ClusterConnection,
    ClusterImages,
    ClusterScheduling,
    MongoClusterStore,
    SandboxCheck,
    home_env_fields,
)
from tests.helpers.http import api_client, make_app
from tests.helpers.sandboxes import kubelet_for_check, node, service

IMAGE = "daegam-sandbox:abc12345@sha256:" + "a" * 64
# 1.1.0-rc4 의 Cluster 문서가 가진 최상위 칸. 그 판의 `Cluster` 의 칸과 `_id`, P68 의 seq 다
RC4_KEYS = {
    "_id",
    "name",
    "display_name",
    "connection",
    "platform_internal_url",
    "images",
    "namespaces",
    "ingress",
    "scheduling",
    "default_redis",
    "shipped_redis_tls",
    "recorded_toolhive_version",
    "gates",
    "status",
    "created_at",
    "updated_at",
    "updated_by",
    "seq",
}
TOLERATION = {"key": "sandbox", "operator": "Exists", "effect": "NoSchedule"}


def _with_store(settings, work: Callable[[MongoClusterStore], Awaitable[Any]]) -> Any:
    """테스트 DB 위의 클러스터 스토어로 work 를 돌린다."""

    async def go() -> Any:
        client: AsyncMongoClient = AsyncMongoClient(settings.mongo_url)
        try:
            return await work(MongoClusterStore(client[settings.mongo_db]))
        finally:
            await client.close()

    return asyncio.run(go())


def _cluster(name: str = "edge") -> Cluster:
    return Cluster(
        name=name,
        connection=ClusterConnection(mode="kubeconfig", secret_name=f"cluster-{name}"),
        platform_internal_url="http://daegam-platform.daegam-system.svc:9000",
    )


def _passed(**fields: Any) -> SandboxCheck:
    values = {
        "passed": True,
        "userns": True,
        "network": True,
        "shared_nodes": True,
        "capacity": 1,
        "placement": "p1",
        "checked_at": utcnow(),
    }
    return SandboxCheck(**{**values, **fields})


def _home_app(settings, fake_clusters, fake_secrets, **over: Any):
    return make_app(
        settings,
        fake_clusters,
        fake_secrets,
        home_kubecontext="fake-ctx",
        home_sandbox_image=IMAGE,
        **over,
    )


# -- 모델 ------------------------------------------------------------------------------------


def test_a_cluster_without_the_field_and_one_with_later_keys_both_read() -> None:
    """이 칸이 없던 문서는 기본값으로 읽고, 뒤의 판이 안쪽에 더한 칸은 버린다."""
    doc = _cluster().to_doc()
    doc.pop("sandbox")
    assert Cluster.from_doc(doc).sandbox.check.passed is False
    later = {
        "image": IMAGE,
        "later": 1,
        "scheduling": {"node_selector": {"a": "b"}, "later": 2},
        "check": {"passed": True, "later": 3},
    }
    sandbox = Cluster.from_doc({**doc, "sandbox": later}).sandbox
    assert (sandbox.image, sandbox.scheduling.node_selector) == (IMAGE, {"a": "b"})
    assert sandbox.check.passed is True


def test_home_env_maps_the_sandbox_image(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_AUTH_MODE", "header")
    monkeypatch.setenv("DAEGAM_HOME_SANDBOX_IMAGE", f"  {IMAGE} ")
    assert settings_from_env().home_sandbox_image == IMAGE
    monkeypatch.setenv("DAEGAM_HOME_SANDBOX_IMAGE", " ")
    assert settings_from_env().home_sandbox_image is None
    fields = home_env_fields("http://p", sandbox_image=IMAGE)
    assert fields == {"platform_internal_url": "http://p", "sandbox.image": IMAGE}
    assert "sandbox.image" not in home_env_fields("http://p")


# -- 사람의 저장 ---------------------------------------------------------------------------------


def test_the_human_save_writes_only_image_and_scheduling(
    settings, fake_clusters, fake_secrets, mongo
) -> None:
    with api_client(make_app(settings, fake_clusters, fake_secrets, home_kubecontext="c")) as c:
        home = c.get("/clusters").json()[0]
        assert home["sandbox"]["image"] is None and home["sandbox"]["check"]["passed"] is False
        _with_store(settings, lambda s: s.set_sandbox_check(home["id"], _passed()))
        url = f"/clusters/{home['id']}"
        sched = {"node_selector": {"pool": "sandbox"}, "tolerations": [TOLERATION]}
        r = c.patch(url, json={"sandbox": {"image": f" {IMAGE} ", "scheduling": sched}})
        assert r.status_code == 200, r.text
        assert r.json()["sandbox"]["image"] == IMAGE
        assert r.json()["sandbox"]["scheduling"] == sched
        # 판정은 그대로다
        assert r.json()["sandbox"]["check"]["passed"] is True
        # 보내지 않은 칸은 그대로이고, scheduling 의 null 은 빈 배치다
        r = c.patch(url, json={"sandbox": {"scheduling": None}})
        assert r.status_code == 200, r.text
        stored = mongo[CLUSTERS].find_one({"_id": home["id"]})["sandbox"]
        assert stored["image"] == IMAGE
        assert stored["scheduling"] == {"node_selector": None, "tolerations": None}
        assert stored["check"]["passed"] is True
        # 판정은 본문으로 받지 않는다. 모르는 칸도 거절한다
        for body in (
            {"sandbox": {"check": {"passed": True}}},
            {"sandbox": {"scheduling": {"node_selectors": {}}}},
        ):
            r = c.patch(url, json=body)
            assert r.status_code == 422, r.text
        # 만들 때도 받는다
        r = c.post(
            "/clusters",
            json={
                "name": "edge",
                "connection": {"api_url": "https://k:6443", "ca_bundle": "x", "sa_token": "t"},
                "platform_internal_url": "http://p:9000",
                "sandbox": {"image": IMAGE},
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["sandbox"]["image"] == IMAGE


def test_a_save_read_before_a_void_does_not_restore_the_verdict(settings) -> None:
    async def work(store: MongoClusterStore) -> Cluster | None:
        cluster = await store.create(_cluster())
        await store.set_sandbox_check(cluster.id, _passed())
        stale = await store.get(cluster.id)
        assert stale is not None and stale.sandbox.check.passed
        assert await store.void_sandbox_check(cluster.id, "self_check")
        stale.sandbox.image = IMAGE
        assert await store.save_edit(stale, {"sandbox.image", "updated_at"})
        return await store.get(cluster.id)

    fresh = _with_store(settings, work)
    assert fresh is not None and fresh.sandbox.image == IMAGE
    assert fresh.sandbox.check.void_reason == "self_check"
    assert fresh.sandbox.check.voided_at is not None


# -- 판정의 칸 쓰기 ------------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["create", "before_the_upgrade"])
def test_each_write_works_on_a_new_and_an_old_document(settings, mongo, origin) -> None:
    """create 가 만든 문서와 `sandbox` 칸이 없던 문서 모두에서 점 경로 쓰기가 듣는다."""
    cluster = _cluster()
    doc = cluster.to_doc()
    if origin == "before_the_upgrade":
        doc.pop("sandbox")
    mongo[CLUSTERS].insert_one(doc)
    check = _passed()

    async def work(store: MongoClusterStore) -> list[bool]:
        results = [
            await store.accept_sandbox(cluster.id, "admin", check.checked_at),  # 판정 전
        ]
        await store.set_sandbox_check(cluster.id, check)
        await store.set_sandbox_shared_nodes(cluster.id, False)
        results.append(await store.accept_sandbox(cluster.id, "admin", check.checked_at))
        results.append(await store.void_sandbox_check(cluster.id, "probe"))
        results.append(await store.void_sandbox_check(cluster.id, "self_check"))
        stale = await store.get(cluster.id)
        assert stale is not None
        stale.sandbox.image = IMAGE
        results.append(await store.save_edit(stale, {"sandbox.image"}))
        return results

    assert _with_store(settings, work) == [False, True, True, False, True]
    stored = mongo[CLUSTERS].find_one({"_id": cluster.id})["sandbox"]
    assert stored["image"] == IMAGE
    assert stored["check"]["shared_nodes"] is False
    # 무효는 passed 를 거짓으로 두고 처음 까닭을 남기며 허용을 지운다
    assert stored["check"]["passed"] is False and stored["check"]["voided_at"] is not None
    assert stored["check"]["void_reason"] == "probe"
    assert stored["check"]["accepted_by"] is None and stored["check"]["accepted_at"] is None


def test_acceptance_needs_the_checked_at_the_admin_saw_and_a_new_check_clears_it(
    settings, mongo
) -> None:
    cluster = _cluster()
    mongo[CLUSTERS].insert_one(cluster.to_doc())
    seen = utcnow().replace(microsecond=123456)

    async def work(store: MongoClusterStore) -> list[bool]:
        await store.set_sandbox_check(cluster.id, _passed(checked_at=seen))
        results = [
            await store.accept_sandbox(cluster.id, "admin", seen - timedelta(seconds=1)),
            # 화면이 본 값은 밀리초까지다. 마이크로초가 달라도 같은 판정이다
            await store.accept_sandbox(cluster.id, "admin", to_millis(seen)),
        ]
        await store.set_sandbox_check(cluster.id, _passed(passed=False, checked_at=utcnow()))
        results.append(await store.accept_sandbox(cluster.id, "admin", seen))
        return results

    assert _with_store(settings, work) == [False, True, False]
    check = mongo[CLUSTERS].find_one({"_id": cluster.id})["sandbox"]["check"]
    assert check["passed"] is False and check["accepted_by"] is None


def test_a_failed_verdict_is_not_voided(settings, mongo) -> None:
    cluster = _cluster()
    mongo[CLUSTERS].insert_one(cluster.to_doc())

    async def work(store: MongoClusterStore) -> bool:
        await store.set_sandbox_check(cluster.id, SandboxCheck(passed=False, checked_at=utcnow()))
        return await store.void_sandbox_check(cluster.id, "self_check")

    assert _with_store(settings, work) is False


# -- home 과 호환 ----------------------------------------------------------------------------


def test_home_takes_the_sandbox_image_and_locks_it(settings, fake_clusters, fake_secrets) -> None:
    with api_client(_home_app(settings, fake_clusters, fake_secrets)) as c:
        home = c.get("/clusters").json()[0]
        assert home["sandbox"]["image"] == IMAGE
        assert "sandbox.image" in home["env_managed"]
        url = f"/clusters/{home['id']}"
        r = c.patch(url, json={"sandbox": {"image": "daegam-sandbox:other"}})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["fields"] == ["sandbox.image"]
        # 같은 값과, image 를 보내지 않는 배치 저장은 받는다
        r = c.patch(url, json={"sandbox": {"image": IMAGE, "scheduling": {"tolerations": []}}})
        assert r.status_code == 200, r.text
        r = c.patch(url, json={"sandbox": {"scheduling": None}})
        assert r.status_code == 200, r.text


def test_compatibility_with_the_previous_release(settings, fake_clusters, fake_secrets, mongo):
    """home upsert(이미지), 사람의 저장, 점검, 허용, 무효, 스윕을 거친 문서를 앞의 판의 모델이
    읽는다. `images`·`scheduling` 은 바뀌지 않은 `ClusterImages`·`ClusterScheduling` 으로 검증되고
    최상위 칸은 1.1.0-rc4 의 것에 `sandbox` 하나를 더한 것이다 (10절)."""
    over = {
        "home_images": {"registry_prefix": "registry.example/daegam", "runtime_image": "rt:1"},
        "home_ingress": {"tls": "edge"},
    }
    with api_client(_home_app(settings, fake_clusters, fake_secrets, **over)) as c:
        home = c.get("/clusters").json()[0]
        r = c.patch(
            f"/clusters/{home['id']}",
            json={
                "images": {**home["images"], "pull_secret": "pull"},
                "scheduling": {**home["scheduling"], "priority_class": "low"},
                "sandbox": {"scheduling": {"node_selector": {"pool": "sbx"}}},
            },
        )
        assert r.status_code == 200, r.text
        # 진짜 점검과 허용, 스윕 한 바퀴 (P40 F3·F2b)
        fake = fake_clusters.by_name[HOME_CLUSTER_NAME]
        fake.objects[("v1", "Node", None, "n1")] = node("n1", labels={"pool": "sbx"})
        fake.apply(service("kube-system", "kube-dns", "10.43.0.10", 53))
        fake.apply(service("default", "kubernetes", "10.43.0.1", 443))
        kubelet_for_check(fake)
        state = c.app.state
        doc = c.portal.call(state.clusters.get, home["id"])
        ran = c.portal.call(state.sandbox_checks.run, doc, "root")
        assert ran is not None and ran.passed, c.get(f"/clusters/{home['id']}").json()["sandbox"]
        seen = c.get(f"/clusters/{home['id']}").json()["sandbox"]["check"]["checked_at"]
        assert (
            c.post(f"/clusters/{home['id']}/sandbox/accept", json={"checked_at": seen}).status_code
            == 200
        )
        c.portal.call(state.sandbox_sweep.tick)
    check = _passed()

    async def work(store: MongoClusterStore) -> None:
        await store.set_sandbox_check(home["id"], check)
        assert await store.accept_sandbox(home["id"], "admin", check.checked_at)
        assert await store.void_sandbox_check(home["id"], "self_check")
        await store.set_sandbox_shared_nodes(home["id"], True)

    _with_store(settings, work)
    # 다시 뜬 플랫폼이 home 을 다시 upsert 한다
    with api_client(_home_app(settings, fake_clusters, fake_secrets, **over)) as c:
        assert c.get("/clusters").json()[0]["sandbox"]["image"] == IMAGE
    raw = mongo[CLUSTERS].find_one({"name": HOME_CLUSTER_NAME})
    # block_metadata 는 메타데이터 차단의 스위치다 (T75). 최상위 칸이라 앞의 판은 모르고 지나간다
    assert set(raw) == RC4_KEYS | {"sandbox", "block_metadata"}
    ClusterImages.model_validate(raw["images"])
    ClusterScheduling.model_validate(raw["scheduling"])
    assert raw["images"]["pull_secret"] == "pull"
    assert raw["sandbox"]["image"] == IMAGE
    assert raw["sandbox"]["scheduling"]["node_selector"] == {"pool": "sbx"}
    assert raw["sandbox"]["check"]["void_reason"] == "self_check"
    assert raw["sandbox"]["check"]["shared_nodes"] is True
