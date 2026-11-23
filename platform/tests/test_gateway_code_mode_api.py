"""P56 F1: Gateway 의 `code_mode` 칸과 그 검사 (6.2절). 저장·미리보기·배포·재배포가 판정을 읽고,
배포는 ToolHive 0.53 을 요구한다. calculator 는 SOAP 서버라 Tool 이 모두 POST(쓰기)이므로, 읽기
Tool 은 서버 주인의 읽기 전용 표시로 만든다."""

from __future__ import annotations

from tests.helpers.clusters import record_toolhive
from tests.helpers.consts import DEFAULT_NS, GW, NP_API
from tests.helpers.exposure import INGRESS, prep_ingress
from tests.helpers.gateways import create_gateway
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env


def _mark(mongo, server_id: str, **marks: bool) -> None:
    """MCP Server 의 모든 리비전에 서버 주인의 읽기 전용 표시를 둔다 (P52 4절). 효과는 배포한
    리비전의 표시를 읽는다."""
    rules = {name: {"read_only": value} for name, value in marks.items()}
    mongo["mcp_servers"].update_one(
        {"_id": server_id}, {"$set": {"revisions.$[].backends.$[].tool_rules": rules}}
    )


def _env(client, fake_clusters, mongo):
    """(클러스터 id, 허브 항목, calculator 항목 id, calculator MCP Server id). Add 만 읽는다."""
    cid, items, calc, *_rest = hub_env(client, fake_clusters)
    _mark(mongo, calc["id"], Add=True, Divide=False)
    return cid, items, items["calculator"]["id"], calc["id"]


def _put(client, gid: str, backends: list[dict], **extra):
    return client.put(f"{GW}/{gid}", json={"name": "a", "backends": backends, **extra})


def test_code_mode_is_off_by_default_and_a_put_without_it_keeps_it(client, fake_clusters, mongo):
    _cid, items, calc, _server = _env(client, fake_clusters, mongo)
    r = create_gateway(client, items, backends=[{"hub_item_id": calc, "tools": ["Add"]}])
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json()["code_mode"] is False
    on = _put(client, gid, [{"hub_item_id": calc, "tools": ["Add"]}], code_mode=True)
    assert on.status_code == 200, on.text
    assert on.json()["code_mode"] is True
    kept = _put(client, gid, [{"hub_item_id": calc, "tools": ["Add"]}])
    assert kept.json()["code_mode"] is True
    assert mongo["gateways"].find_one({"_id": gid})["spec"]["code_mode"] is True
    off = _put(client, gid, [{"hub_item_id": calc, "tools": ["Add"]}], code_mode=False)
    assert off.json()["code_mode"] is False


def test_a_tool_that_writes_refuses_code_mode_with_its_name(client, fake_clusters, mongo):
    _cid, items, calc, _server = _env(client, fake_clusters, mongo)
    r = create_gateway(client, items, backends=[{"hub_item_id": calc}], code_mode=True)
    assert detail_code(r) == (422, "code_mode_not_read_only")
    assert r.json()["detail"]["names"] == {"Divide": "write"}


def test_a_backend_with_unknown_names_refuses_code_mode(client, fake_clusters, mongo):
    _cid, items, calc, _server = _env(client, fake_clusters, mongo)
    backends = [{"hub_item_id": calc, "tools": ["Add"]}, {"hub_item_id": items["spike"]["id"]}]
    r = create_gateway(client, items, backends=backends, code_mode=True)
    assert detail_code(r) == (422, "code_mode_unknown_names")
    assert r.json()["detail"]["backends"] == ["spike"]


def test_exposure_and_owner_cedar_refuse_code_mode(client, fake_clusters, mongo):
    _cid, items, calc, _server = _env(client, fake_clusters, mongo)
    backends = [{"hub_item_id": calc, "tools": ["Add"]}]
    gid = create_gateway(client, items, backends=backends).json()["id"]
    mongo["gateways"].update_one({"_id": gid}, {"$set": {"exposure.enabled": True}})
    assert detail_code(_put(client, gid, backends, code_mode=True)) == (422, "code_mode_exposed")
    mongo["gateways"].update_one({"_id": gid}, {"$set": {"exposure.enabled": False}})
    owner = {"spec": {"incomingAuth": {"authzConfig": {"inline": {"policies": []}}}}}
    mongo["gateways"].update_one({"_id": gid}, {"$set": {"overrides": owner}})
    r = _put(client, gid, backends, code_mode=True)
    assert detail_code(r) == (422, "code_mode_owner_cedar")


def test_preview_and_deploy_judge_the_marks_again(client, fake_clusters, mongo):
    cid, items, calc, server = _env(client, fake_clusters, mongo)
    backends = [{"hub_item_id": calc, "tools": ["Add"]}]
    r = create_gateway(client, items, backends=backends, code_mode=True)
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    _mark(mongo, server, Add=False)
    preview = client.get(f"{GW}/{gid}/revisions/1/preview")
    assert detail_code(preview) == (422, "code_mode_not_read_only")
    record_toolhive(mongo, cid, "0.53.0")
    deploy = client.post(f"{GW}/{gid}/deploy", json={})
    assert detail_code(deploy) == (422, "code_mode_not_read_only")


def test_deploying_code_mode_needs_toolhive_053(client, fake_clusters, mongo):
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    backends = [{"hub_item_id": calc, "tools": ["Add"]}]
    gid = create_gateway(client, items, backends=backends, code_mode=True).json()["id"]
    for version in (None, "0.52.3"):
        record_toolhive(mongo, cid, version)
        deploy = client.post(f"{GW}/{gid}/deploy", json={})
        assert detail_code(deploy) == (422, "code_mode_toolhive_old"), version
    record_toolhive(mongo, cid, "0.53.0")
    deploy = client.post(f"{GW}/{gid}/deploy", json={})
    assert deploy.status_code == 201, deploy.text
    assert deploy.json()["code_mode"] is True


def _deployed(client, mongo, cid, items, calc) -> str:
    """Add 만 고른 code mode Gateway 를 0.53 클러스터에 배포하고 그 id 를 돌려준다."""
    record_toolhive(mongo, cid, "0.53.0")
    backends = [{"hub_item_id": calc, "tools": ["Add"]}]
    gid = create_gateway(client, items, backends=backends, code_mode=True).json()["id"]
    deploy = client.post(f"{GW}/{gid}/deploy", json={})
    assert deploy.status_code == 201, deploy.text
    return gid


def test_turning_exposure_on_for_a_code_mode_gateway_is_refused_and_not_stored(
    client, fake_clusters, mongo
):
    cid, items, calc, _server = _env(client, fake_clusters, mongo)
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"ingress": INGRESS}})
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    gid = _deployed(client, mongo, cid, items, calc)
    r = client.patch(f"{GW}/{gid}", json={"exposure": {"enabled": True, "host": "gw.e2e.local"}})
    assert detail_code(r) == (422, "code_mode_exposed")
    stored = mongo["gateways"].find_one({"_id": gid})
    assert stored["exposure"]["enabled"] is False and stored["state"] == "deployed"
    assert fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is None


def test_a_redeploy_after_a_mark_turned_to_write_is_refused(client, fake_clusters, mongo):
    cid, items, calc, server = _env(client, fake_clusters, mongo)
    gid = _deployed(client, mongo, cid, items, calc)
    _mark(mongo, server, Add=False)
    r = client.post(f"{GW}/{gid}/redeploy", json={})
    assert detail_code(r) == (422, "code_mode_not_read_only")
    assert r.json()["detail"]["names"] == {"Add": "write"}
    assert mongo["gateways"].find_one({"_id": gid})["state"] == "deployed"


def test_a_clone_keeps_code_mode_and_is_judged_like_a_new_gateway(client, fake_clusters, mongo):
    _cid, items, calc, _server = _env(client, fake_clusters, mongo)
    backends = [{"hub_item_id": calc, "tools": ["Add"]}]
    gid = create_gateway(client, items, backends=backends, code_mode=True).json()["id"]
    clone = client.post(f"{GW}/{gid}/clone", json={"name": "b"})
    assert clone.status_code == 201, clone.text
    assert clone.json()["code_mode"] is True
    exposed = client.post(
        f"{GW}/{gid}/clone",
        json={"name": "c", "exposure": {"enabled": True, "host": "c.e2e.local"}},
    )
    assert detail_code(exposed) == (422, "code_mode_exposed")
