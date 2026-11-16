"""허브 항목 재적용 (P50 1절). 자기 CR 을 둔 항목(remote·image·proxy 게시)이 렌더 번호로 제자리에서
다시 렌더되고, CR 이 없는 항목(entry·internal)은 그 종류에 들지 않는다. 실패는 백오프로 미뤄지고
요청 오류는 이유와 함께 막히며, 상태 API 가 종류 `hub_item` 으로 보인다.

리컨실 틱은 부르지 않고 `DataPlaneAuth.run` 을 직접 부른다 (tests/test_data_plane_auth.py 와 같다).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from daegam.services.data_plane_auth import KIND_HUB_ITEM, RENDER_EPOCHS
from daegam.services.hub import HubError
from daegam.store.base import utcnow
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import HUB
from tests.helpers.hub import SPIKE_URL
from tests.helpers.workers import raise_epoch, run_data_plane_auth

# 지금 코드의 허브 항목 렌더 번호. 테스트가 올리기 전에 읽는다
BASE = RENDER_EPOCHS[KIND_HUB_ITEM]


@pytest.fixture
def epoch_raised(monkeypatch):
    """허브 항목 종류의 렌더 번호를 올린 코드. 같은 사전을 `services/hub.py` 도 읽는다."""
    raise_epoch(monkeypatch, KIND_HUB_ITEM)


def _image_and_remote(client) -> tuple[dict, dict]:
    default_cluster(client)
    image = client.post(
        HUB,
        json={"name": "fetch", "kind": "image", "image": "ghcr.io/x/fetch:1", "port": 8000},
    )
    assert image.status_code == 201, image.text
    remote = client.post(HUB, json={"name": "calc-remote", "kind": "remote", "url": SPIKE_URL})
    assert remote.status_code == 201, remote.text
    return image.json(), remote.json()


def _stored(mongo, item_id: str) -> dict:
    return mongo["hub_items"].find_one({"_id": item_id})


def test_failure_backs_off_and_render_error_blocks_with_detail(
    client, fake_clusters, mongo, monkeypatch
):
    """만든 항목은 지금 렌더 번호라 다시 렌더할 것이 없다. 번호가 오른 뒤의 적용 실패는 백오프로
    미뤄져 다음 시각까지 오지 않는다. 요청 오류(422)는 이유와 함께 막히고 상태 API 가 종류 hub_item
    으로 보인다. 막힌 항목은 수정되면 다시 시도되고, 사람의 저장(접근 정책)은 재적용 칸을 되돌리지
    않는다."""
    image, remote = _image_and_remote(client)
    assert run_data_plane_auth(client)["reapplied"] == []
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    hub = client.app.state.hub_service
    calls: list[str] = []
    original = hub.rerender

    async def rerender(item):
        calls.append(item.id)
        if item.id == image["id"]:
            raise RuntimeError("cluster hiccup")
        raise HubError(422, "invalid_manifest", "image is not allowed here")

    monkeypatch.setattr(hub, "rerender", rerender)
    now = utcnow()
    result = run_data_plane_auth(client, now=now)
    assert result["failed"] == [image["id"]] and result["blocked"] == [remote["id"]]
    failed = _stored(mongo, image["id"])
    assert failed["auth_failures"] == 1 and failed["auth_retry_at"] > now
    blocked = _stored(mongo, remote["id"])["auth_blocked"]
    assert blocked["code"] == "invalid_manifest"
    assert blocked["detail"] == "image is not allowed here"
    # 허브 항목에는 인증 입력이 없어 판의 인증 절반이 비어 있다
    assert blocked["epoch"] == BASE + 1 and not blocked["rev"]

    body = client.get("/settings/data-plane-auth").json()
    rows = [r for r in body["blocked"] if r["kind"] == KIND_HUB_ITEM]
    assert [(r["id"], r["code"], r["detail"]) for r in rows] == [
        (remote["id"], "invalid_manifest", "image is not allowed here")
    ]
    counts = body["kinds"][remote["cluster_id"]][KIND_HUB_ITEM]
    assert counts["pending"] == 1 and counts["blocked"] == 1

    # 백오프 안에서는 어느 것도 다시 오지 않는다
    calls.clear()
    assert run_data_plane_auth(client, now=now + timedelta(minutes=1))["reapplied"] == []
    assert calls == []
    # 백오프가 지나면 실패한 항목만 다시 오고, 막힌 항목은 수정된 뒤에 다시 온다
    monkeypatch.setattr(hub, "rerender", original)
    result = run_data_plane_auth(client, now=now + timedelta(minutes=6))
    assert result["reapplied"] == [image["id"]]
    # 항목이 수정되면(수정 시각이 바뀌면) 막힘이 풀린다
    edited = {"updated_at": now + timedelta(minutes=6)}
    assert client.portal.call(hub.store.set_fields, remote["id"], edited)
    result = run_data_plane_auth(client, now=now + timedelta(minutes=7))
    assert result["reapplied"] == [remote["id"]]
    assert _stored(mongo, remote["id"])["auth_blocked"] is None
    held = client.portal.call(hub.store.get_unscoped, remote["id"])
    held.render_epoch = BASE
    held.access_updated_by = "someone"
    assert client.portal.call(hub.store.save_access, held)
    doc = _stored(mongo, remote["id"])
    assert doc["access_updated_by"] == "someone" and doc["render_epoch"] == BASE + 1


def test_rerender_leaves_items_that_left_created(client, fake_clusters, mongo, epoch_raised):
    """다시 읽은 항목이 created 가 아니면 적용하지 않고 False 다. 지워진 항목도 같다."""
    image, _remote = _image_and_remote(client)
    hub = client.app.state.hub_service
    fake = fake_clusters.by_name["edge"]
    item = client.portal.call(hub.store.get_unscoped, image["id"])
    client.portal.call(hub.store.set_fields, image["id"], {"state": "deleting"})
    start = len(fake.applied)
    assert client.portal.call(hub.rerender, item) is False
    client.portal.call(hub.store.delete, image["id"])
    assert client.portal.call(hub.rerender, item) is False
    assert fake.applied[start:] == []
