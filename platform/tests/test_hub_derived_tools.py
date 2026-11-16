"""허브 항목의 도구 이름은 원본을 따라간다 (D49).

`internal`·`published` 의 `tool_names` 는 저장된 사본이 아니라 원본에서 계산한 값이다. 그래서 원본이
바뀌면 사본을 맞추는 절차 없이도 따라가고, 맞추는 절차가 실패해도 어긋나지 않는다.

이 파일은 P29 F1 의 재현에서 시작했다. 고치기 전에는 세 테스트가 모두 "어긋난다" 를 단정했고
통과했다. 지금은 기대값이 뒤집혀 있다.
"""

from __future__ import annotations

from tests.helpers.consts import GW, HUB, MS
from tests.helpers.gateways import gateway_up
from tests.helpers.hub import hub_env
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env


def test_published_item_follows_the_gateway_after_redeploy(client, fake_clusters, mongo):
    """게시된 Gateway 를 재배포하면 published 항목이 따라간다.

    전에는 게시 시점 값으로 굳었다. `_check_published` 가 막는 것은 undeploy 와 delete 뿐이라
    재배포는 그냥 통과하는데, 재배포 경로에 허브 갱신이 아예 없었다.
    """
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="a")
    pub = client.post(f"{GW}/{inst['id']}/publish", json={}).json()
    assert client.get(f"{HUB}/{pub['id']}").json()["tool_names"] == [
        "Add",
        "CreateOrder",
        "GetOrder",
    ]

    r = client.put(
        f"{GW}/{inst['id']}",
        json={
            "name": "a",
            "backends": [{"hub_item_id": items["calculator"]["id"], "tools": ["Divide"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert client.post(f"{GW}/{inst['id']}/redeploy").status_code == 200

    assert client.get(f"{HUB}/{pub['id']}").json()["tool_names"] == ["Divide"]


def test_internal_item_follows_even_when_the_old_sync_path_throws(
    client, fake_clusters, mongo, monkeypatch
):
    """빠른 경로가 죽어도 어긋나지 않는다. 맞출 사본이 없기 때문이다.

    전에는 `_hub_sync` 가 예외를 삼키고 경고만 남겨서 재배포가 200 인 채로 사본이 옛것으로 남았다.
    """
    from daegam.services.hub import HubService

    cid, bp, ds, mp = mcp_server_env(client)
    inst = clone_and_deploy(client, mp).json()
    pub = client.post(f"{MS}/{inst['id']}/publish", json={}).json()
    assert client.get(f"{HUB}/{pub['id']}").json()["tool_names"] == ["Add"]

    r = client.put(
        f"{MS}/{inst['id']}",
        json={
            "name": "calc-1",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    assert r.status_code == 200, r.text

    # `_hub_sync` 가 실제로 부르고 예외를 삼키는 자리다. 죽은 메서드를 가로채면 아무것도 막지 못해
    # 이 테스트가 늘 통과했다
    async def boom(self, doc):
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(HubService, "sync_instance", boom)
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 200

    assert client.get(f"{HUB}/{pub['id']}").json()["tool_names"] == ["Add", "Divide"]


def test_a_gateway_left_behind_is_stopped_loudly_at_the_next_redeploy(client, fake_clusters, mongo):
    """원본이 바뀌어 없는 도구를 가리키게 된 Gateway 는 재배포에서 422 로 막힌다.

    끝단의 정합을 맡는 자리다. 배포 스냅샷은 적용한 CR 과 짝인 기록이라 얼어 있고, 그 어긋남은
    조용히 고쳐지는 대신 이름과 대안을 말하는 오류로 드러난다.
    """
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    calc_item = items["calculator"]
    gw = gateway_up(
        client, items, name="a", backends=[{"hub_item_id": calc_item["id"], "tools": ["Add"]}]
    )

    r = client.put(
        f"{MS}/{calc['id']}",
        json={
            "name": "calculator",
            "backend_profile_id": calc["backends"][0]["backend_profile_id"],
            "spec_version": 1,
            "tools": ["Divide"],
        },
    )
    assert r.status_code == 200, r.text
    assert client.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    assert client.get(f"{HUB}/{calc_item['id']}").json()["tool_names"] == ["Divide"]

    r = client.post(f"{GW}/{gw['id']}/redeploy")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "unknown_tools"
    assert detail["unknown"] == ["Add"] and detail["available"] == ["Divide"]


def test_refresh_tools_is_refused_for_derived_kinds(client, fake_clusters):
    """`internal`·`published` 는 라이브 값을 저장할 자리가 없다 (D49).

    저장하면 연산자가 새 리비전을 아직 안 굴린 과도기의 값이 굳어 멀쩡한 Gateway 정의를 422 로
    막는다. 라이브를 보는 일은 도구 탭이 저장 없이 한다 (D48).
    """
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    r = client.post(f"{HUB}/{items['calculator']['id']}/refresh-tools")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "derived_tools"

    # 남의 서버를 가리키는 종류는 그대로 된다
    async def lister(_client, _url, bearer=None):
        return ["Echo"]

    client.app.state.hub_service.tool_lister = lister
    assert client.post(f"{HUB}/{remote['id']}/refresh-tools").status_code in (200, 409)
