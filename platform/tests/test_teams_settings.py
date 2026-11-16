"""P47 6.1절: Platform Settings 의 Teams 절, 관리 Secret, 앱 패키지, 관리자의 연결 끊기.

`channels` 는 저장된 문서의 모르는 칸을 버리고 읽고 칸마다 따로 쓴다. 그래서 뒤 판이 더한 채널이
있어도 이 판이 설정을 읽고, 이 판의 저장이 그 칸을 지우지 않는다. PATCH 입력은 모르는 칸을
거절한다. Teams 를 켜는 저장은 설정이 다 있고 사람을 확인하는 모드여야 하며, A2 전에는 409
`teams_not_ready` 다. App ID 나 Entra 테넌트 id 가 바뀌면 모든 연결이 끊긴다.
"""

from __future__ import annotations

import dataclasses
import io
import json
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.helpers.consts import PLATFORM_NS, PUBLIC_URL
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.teams import APP_ID, CLIENT_SECRET, TENANT_ID, configure_teams, plant_link

OTHER_APP = "11111111-2222-3333-4444-555555555555"


def teams_of(c: TestClient) -> dict:
    return c.get("/settings").json()["channels"]["teams"]


def test_the_teams_section_defaults_off(local_admin):
    assert teams_of(local_admin) == {
        "enabled": False,
        "app_id": None,
        "entra_tenant_id": None,
        "secret_set": False,
    }


def test_patch_sets_ids_and_refuses_unknown_keys(local_admin):
    body = {"channels": {"teams": {"app_id": APP_ID, "entra_tenant_id": TENANT_ID}}}
    assert local_admin.patch("/settings", json=body).status_code == 200
    got = teams_of(local_admin)
    assert (got["app_id"], got["entra_tenant_id"], got["secret_set"]) == (APP_ID, TENANT_ID, False)
    for bad in (
        {"channels": {"teams": {"secret_set": True}}},
        {"channels": {"slack": {}}},
        {"channels": {"teams": {"app_id": "not-a-guid"}}},
        {"channels": {"teams": {"enabled": None}}},
    ):
        assert local_admin.patch("/settings", json=bad).status_code == 422, bad


def test_turning_teams_on_waits_for_configuration_and_a2(local_admin, mongo, fake_secrets):
    on = {"channels": {"teams": {"enabled": True}}}
    assert detail_code(local_admin.patch("/settings", json=on)) == (422, "teams_not_configured")
    configure_teams(mongo, fake_secrets)
    assert detail_code(local_admin.patch("/settings", json=on)) == (409, "teams_not_ready")
    assert teams_of(local_admin)["enabled"] is False


def test_turning_teams_on_needs_token_auth(client, mongo, fake_secrets):
    configure_teams(mongo, fake_secrets)
    on = {"channels": {"teams": {"enabled": True}}}
    assert detail_code(client.patch("/settings", json=on)) == (422, "channels_need_token_auth")


def test_stored_keys_of_a_later_release_are_read_and_kept(local_admin, mongo):
    later = {
        "teams": {"enabled": False, "app_id": APP_ID, "refusal": "gcc"},
        "slack": {"enabled": True, "team_id": "T1"},
    }
    mongo["settings"].update_one({"_id": "platform"}, {"$set": {"channels": later}}, upsert=True)
    assert teams_of(local_admin)["app_id"] == APP_ID
    body = {"channels": {"teams": {"entra_tenant_id": TENANT_ID}}}
    assert local_admin.patch("/settings", json=body).status_code == 200
    stored = mongo["settings"].find_one({"_id": "platform"})["channels"]
    assert stored["slack"] == later["slack"]
    assert stored["teams"]["refusal"] == "gcc" and stored["teams"]["entra_tenant_id"] == TENANT_ID


def test_other_sections_save_without_touching_teams(local_admin, mongo, fake_secrets):
    configure_teams(mongo, fake_secrets)
    r = local_admin.patch("/settings", json={"assistant": {"enabled": True}})
    assert r.status_code == 200 and r.json()["channels"]["teams"]["secret_set"] is True


def test_the_client_secret_goes_into_the_managed_secret_only(local_admin, fake_secrets, mongo):
    r = local_admin.put(
        "/settings/channels/teams/client-secret", json={"client_secret": CLIENT_SECRET}
    )
    assert r.status_code == 200 and r.json() == {"secret_set": True}
    stored = fake_secrets.get("daegam-channel-teams", namespace=PLATFORM_NS)
    assert stored == {"client_secret": CLIENT_SECRET}
    assert teams_of(local_admin)["secret_set"] is True
    assert CLIENT_SECRET not in repr(list(mongo["audit_events"].find({})))
    assert CLIENT_SECRET not in repr(mongo["settings"].find_one({"_id": "platform"}))
    bad = {"client_secret": "two words"}
    assert detail_code(local_admin.put("/settings/channels/teams/client-secret", json=bad)) == (
        422,
        "invalid_value",
    )


def test_changing_the_app_id_or_tenant_cuts_every_link(local_admin, mongo, fake_secrets):
    configure_teams(mongo, fake_secrets)
    plant_link(mongo, "u1")
    plant_link(mongo, "u2")
    body = {"channels": {"teams": {"enabled": False}}}
    assert local_admin.patch("/settings", json=body).status_code == 200
    assert mongo["channel_links"].count_documents({}) == 2
    body = {"channels": {"teams": {"app_id": OTHER_APP}}}
    assert local_admin.patch("/settings", json=body).status_code == 200
    assert mongo["channel_links"].count_documents({}) == 0
    row = mongo["audit_events"].find_one({"action": "teams_links_cut"})
    assert row["error_code"] == "bot_changed"


def test_admins_list_and_cut_links(local_admin, mongo):
    first = plant_link(mongo, "u1")
    plant_link(mongo, "u2")
    rows = local_admin.get("/settings/channels/teams/links").json()
    assert {r["username"] for r in rows} == {"u1", "u2"}
    assert local_admin.delete(f"/settings/channels/teams/links/{first}").json() == {"cut": 1}
    assert detail_code(local_admin.delete(f"/settings/channels/teams/links/{first}")) == (
        404,
        "not_found",
    )
    assert local_admin.delete("/settings/channels/teams/links").json() == {"cut": 1}


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def public_admin(local_settings, mongo, fake_clusters, fake_secrets):
    from tests.helpers.auth import admin_headers

    settings = dataclasses.replace(local_settings, public_url=PUBLIC_URL)
    with api_client(make_app(settings, fake_clusters, fake_secrets)) as c:
        c.headers.update(admin_headers(c, mongo))
        yield c


def test_the_app_package_holds_the_bot_and_the_host(public_admin, mongo, fake_secrets):
    path = "/settings/channels/teams/app-package"
    assert detail_code(public_admin.get(path)) == (422, "teams_not_configured")
    configure_teams(mongo, fake_secrets, secret=False)
    r = public_admin.get(path)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["color.png", "en-us.json", "manifest.json", "outline.png"]
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["id"] == APP_ID and manifest["bots"][0]["botId"] == APP_ID
    assert manifest["bots"][0]["scopes"] == ["personal"]
    assert manifest["validDomains"] == ["daegam.example.com"]
    assert manifest["name"]["short"] == "대감"
    titles = [c["title"] for c in manifest["bots"][0]["commandLists"][0]["commands"]]
    assert titles == ["연결", "연결 끊기", "에이전트", "새 대화", "중지", "도움말"]
    en = json.loads(z.read("en-us.json"))
    assert en["name.short"] == "Daegam"
    assert en["bots[0].commandLists[0].commands[0].title"] == "link"
    assert _png_size(z.read("color.png")) == (192, 192)
    assert _png_size(z.read("outline.png")) == (32, 32)


def test_the_app_package_needs_an_https_public_url(local_admin, mongo, fake_secrets):
    configure_teams(mongo, fake_secrets)
    r = local_admin.get("/settings/channels/teams/app-package")
    assert detail_code(r) == (422, "teams_not_configured")
