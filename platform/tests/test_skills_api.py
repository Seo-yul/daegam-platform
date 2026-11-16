"""Skill API (D64, P39 3절). 경로 전부, 리비전의 동시 저장, `skill_unchanged`, 다른 테넌트
Agent 가 막는 삭제와 그 수, 할당량, view 없음 404·manage 없음 403, 경계 넓히기(`skill:share`)와
manage 로 되는 좁히기, manage 의 다른 테넌트 422, 복제와 그 출처(`cloned_from`), 내보내기의
내려받기 헤더와 감사, 참조의 `pins`, 감사에 본문이 없는 것을 본다. 형식 규칙 자체는
test_skill_format.py 가 본다.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from daegam.auth import rbac
from daegam.auth.hub_access import Access
from daegam.auth.principal import header_principal
from daegam.services.skills import ORPHAN_GRACE, SkillError, SkillService
from daegam.store.base import utcnow
from daegam.store.settings import MongoPlatformSettingsStore
from daegam.store.skills import MongoSkillStore
from daegam.units import KIB
from tests.helpers.aio import run_db
from tests.helpers.auth import as_principal
from tests.helpers.consts import OTHER, SK, T2, TENANT_HEADER
from tests.helpers.docs import TENANT, agent_doc, agent_refs_spec, insert_docs
from tests.helpers.documents import zip_of
from tests.helpers.http import detail_code
from tests.helpers.skills import created_skill, revise_skill, skill_files, skill_md

ACME = {TENANT_HEADER: TENANT}
# 감사에 남지 않아야 하는 본문 표지
MARKER = "marker-7f3a"


# -- 만들기·목록·상세 ---------------------------------------------------------------------


def test_create_reports_every_issue_and_stores_nothing(client, mongo):
    r = client.post(
        SK,
        json={
            "files": [
                {"path": "SKILL.md", "content": "# no frontmatter\n"},
                {"path": "notes.md", "content": "tag \U000e0041"},
                {"path": "../x.md", "content": "x"},
            ]
        },
    )
    assert detail_code(r) == (422, "invalid_skill")
    issues = r.json()["detail"]["issues"]
    assert sorted((i["path"], i["code"]) for i in issues) == [
        ("../x.md", "path_invalid"),
        ("SKILL.md", "frontmatter_missing"),
        ("notes.md", "invisible_chars"),
    ]
    assert mongo["skills"].count_documents({}) == 0
    assert mongo["skill_revisions"].count_documents({}) == 0


def test_name_conflict_leaves_no_orphan_revision(client, mongo):
    created_skill(client)
    r = client.post(SK, json={"files": skill_files()})
    assert detail_code(r) == (409, "name_conflict")
    assert mongo["skill_revisions"].count_documents({}) == 1
    # 다른 테넌트에는 같은 이름이 있어도 된다
    created_skill(client, headers=T2)


def test_validate_reports_warnings_and_digest(client):
    r = client.post(
        f"{SK}/validate",
        json={"files": skill_files(**{"scripts/x.py": "print(1)"})},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["frontmatter"]["name"] == "demo" and body["digest"].startswith("sha256:")
    assert [(i["code"], i["severity"]) for i in body["issues"]] == [
        ("skill_scripts_inert", "warning")
    ]
    r = client.post(f"{SK}/validate", json={"files": skill_files(), "name": "other"})
    body = r.json()
    assert body["frontmatter"] is None and body["digest"] is None
    assert [i["code"] for i in body["issues"]] == ["name_mismatch"]


# -- 가져오기 ------------------------------------------------------------------------------


def test_import_zip_and_single_skill_md(client):
    data = zip_of(
        {
            "demo/SKILL.md": skill_md(),
            "demo/references/a.md": "a",
            "__MACOSX/demo/._SKILL.md": b"\0",
        }
    )
    r = client.post(
        f"{SK}/import",
        files={"file": ("demo.zip", data, "application/zip")},
        data={"display_name": "데모"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["display_name"] == "데모" and body["skipped"] == ["__MACOSX/demo/._SKILL.md"]
    assert {f["path"] for f in body["latest"]["files"]} == {"SKILL.md", "references/a.md"}
    r = client.post(
        f"{SK}/import", files={"file": ("x.md", skill_md("solo").encode(), "text/markdown")}
    )
    assert r.status_code == 201 and r.json()["name"] == "solo" and r.json()["skipped"] == []


def test_import_limits_and_archive_errors(client):
    too_big = b"x" * (2 * KIB * KIB + 1)
    r = client.post(f"{SK}/import", files={"file": ("big.md", too_big, "text/markdown")})
    assert detail_code(r) == (413, "too_large")
    many = zip_of({f"f{n}.md": "x" for n in range(65)})
    r = client.post(f"{SK}/import", files={"file": ("many.zip", many, "application/zip")})
    assert detail_code(r) == (422, "invalid_skill")
    assert [i["code"] for i in r.json()["detail"]["issues"]] == ["archive_too_large"]
    r = client.post(f"{SK}/import", files={"file": ("bad.zip", b"PK\x03\x04junk", "x/y")})
    assert [i["code"] for i in r.json()["detail"]["issues"]] == ["archive_invalid"]


# -- 리비전 --------------------------------------------------------------------------------


def test_revisions_flow(client):
    skill = created_skill(client, **{"scripts/run.sh": "echo"})
    r = revise_skill(client, skill, "# Body v2\n", expect=1)
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["revision"] == 2 and v2["latest"]["digest"] != skill["latest"]["digest"]
    # 고치기 시작할 때 본 번호가 낡았다
    r = revise_skill(client, skill, "# Body v3\n", expect=1)
    assert detail_code(r) == (409, "revision_conflict")
    assert r.json()["detail"]["revision"] == 2
    # 마지막 리비전과 같은 내용
    r = revise_skill(client, skill, "# Body v2\n", expect=2)
    assert detail_code(r) == (422, "skill_unchanged")
    # 머리말 name 은 리소스 이름 그대로여야 한다
    r = client.post(
        f"{SK}/{skill['id']}/revisions",
        json={"files": skill_files("renamed"), "expect_revision": 2},
    )
    assert detail_code(r) == (422, "invalid_skill")

    metas = client.get(f"{SK}/{skill['id']}/revisions").json()
    assert [m["revision"] for m in metas] == [2, 1]
    assert all("content" not in f for m in metas for f in m["files"])
    assert metas[0]["created_by"] == "header" and metas[0]["frontmatter"]["name"] == "demo"
    r1 = client.get(f"{SK}/{skill['id']}/revisions/1").json()
    files = {f["path"]: f for f in r1["files"]}
    assert files["SKILL.md"]["content"] == skill_md() and files["scripts/run.sh"]["script"]
    assert client.get(f"{SK}/{skill['id']}/revisions/9").status_code == 404
    assert client.get(f"{SK}/{skill['id']}/revisions/0").status_code == 422


def test_revision_import_takes_expect_revision(client):
    skill = created_skill(client)
    data = zip_of({"SKILL.md": skill_md(body="# zip\n")})
    r = client.post(
        f"{SK}/{skill['id']}/revisions/import",
        files={"file": ("demo.zip", data, "application/zip")},
        data={"expect_revision": "1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2 and r.json()["skipped"] == []
    r = client.post(
        f"{SK}/{skill['id']}/revisions/import",
        files={"file": ("demo.zip", data, "application/zip")},
        data={"expect_revision": "1"},
    )
    assert detail_code(r) == (409, "revision_conflict")


def _with_service[T](settings, work: Callable[[SkillService], Awaitable[T]]) -> T:
    """서비스를 직접 부른다. 이 파일의 서비스 호출은 삭제를 부르지 않으므로 참조 세기가 없어도
    된다."""

    async def run(db) -> T:
        service = SkillService(MongoSkillStore(db), MongoPlatformSettingsStore(db), None)  # type: ignore[arg-type]
        return await work(service)

    return run_db(settings, run)


def test_concurrent_revisions_one_wins(client, settings, mongo):
    """둘 다 같은 번호를 보고 들어와도 번호 유일 인덱스가 하나만 들인다."""
    created = created_skill(client)

    async def race(service: SkillService) -> list[Any]:
        skill = await service.store.get_unscoped(created["id"])
        assert skill is not None
        return await asyncio.gather(
            *(
                service.add_revision(
                    skill,
                    [("SKILL.md", skill_md(body=f"# {n}\n"))],
                    expect_revision=1,
                    note=None,
                    principal=header_principal(),
                )
                for n in ("a", "b")
            ),
            return_exceptions=True,
        )

    results = _with_service(settings, race)
    errors = [r for r in results if isinstance(r, SkillError)]
    assert len(errors) == 1 and errors[0].code == "revision_conflict"
    assert mongo["skills"].find_one({"_id": created["id"]})["revision"] == 2
    assert mongo["skill_revisions"].count_documents({"skill_id": created["id"]}) == 2


def test_a_stopped_save_does_not_block_new_revisions(client, mongo):
    """넣고 올리기 전에 멈춘 저장이 남긴 r2 는 목록에 보이지 않고, 오래됐으면 다음 저장이 거둔다.
    막 생긴 r2 는 저장 중인 다른 요청의 것일 수 있어 409 다."""
    skill = created_skill(client)
    orphan = mongo["skill_revisions"].find_one({"skill_id": skill["id"], "revision": 1})
    orphan.update({"_id": "orphan-r2", "revision": 2, "created_at": utcnow()})
    mongo["skill_revisions"].insert_one(orphan)
    assert [r["revision"] for r in client.get(f"{SK}/{skill['id']}/revisions").json()] == [1]
    assert client.get(f"{SK}/{skill['id']}/revisions/2").status_code == 404
    r = revise_skill(client, skill, "# 둘째\n", 1)
    assert detail_code(r) == (409, "revision_conflict")

    stale = utcnow() - ORPHAN_GRACE - timedelta(seconds=1)
    mongo["skill_revisions"].update_one({"_id": "orphan-r2"}, {"$set": {"created_at": stale}})
    r = revise_skill(client, skill, "# 둘째\n", 1)
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2
    revs = list(mongo["skill_revisions"].find({"skill_id": skill["id"], "revision": 2}))
    assert len(revs) == 1 and revs[0]["_id"] != "orphan-r2"
    assert revs[0]["digest"] == r.json()["latest"]["digest"]


def test_rename_and_access_write_only_their_own_fields(client, settings, mongo):
    """두 저장이 겹쳐도 서로를 옛 값으로 되돌리지 않는다. 표시 이름 바꾸기는 그사이 바뀐 정책을,
    정책 저장은 그사이 바뀐 표시 이름을 덮지 않는다. 읽은 사본의 값을 바꿔 겹침을 흉내 낸다."""
    skill = created_skill(client)
    narrowed = Access.model_validate({"view": {"roles": ["developer"]}})

    async def overlap(service: SkillService) -> None:
        stale = await service.store.get_unscoped(skill["id"])
        assert stale is not None
        stale.access = narrowed
        await service.rename(stale, "새 이름", header_principal())
        fresh = await service.store.get_unscoped(skill["id"])
        assert fresh is not None and fresh.access == Access()
        fresh.display_name = "옛 이름"
        await service.set_access(fresh, narrowed, header_principal())

    _with_service(settings, overlap)
    doc = mongo["skills"].find_one({"_id": skill["id"]})
    assert doc["display_name"] == "새 이름"
    assert doc["access"]["view"]["roles"] == ["developer"]


# -- 고치기·내보내기·삭제 -------------------------------------------------------------------


def test_patch_display_name_only(client):
    skill = created_skill(client)
    r = client.patch(f"{SK}/{skill['id']}", json={"display_name": "  보고서 문체 "})
    assert r.status_code == 200 and r.json()["display_name"] == "보고서 문체"
    assert client.patch(f"{SK}/{skill['id']}", json={}).json()["detail"]["code"] == "empty_patch"
    r = client.patch(f"{SK}/{skill['id']}", json={"display_name": None})
    assert detail_code(r)[1] == "invalid_display_name"
    assert client.patch(f"{SK}/{skill['id']}", json={"name": "x"}).status_code == 422


def test_delete_guard_counts_agents_of_other_tenants(client, mongo):
    skill = created_skill(client, headers=ACME)
    sid = skill["id"]
    insert_docs(
        mongo,
        agents=[
            agent_doc("ag-draft", spec=agent_refs_spec(skill=(sid, 1))),
            agent_doc(
                "ag-live",
                spec=agent_refs_spec(),
                pinned=agent_refs_spec(skill=(sid, 1)),
                state="deployed",
            ),
            agent_doc("ag-theirs", spec=agent_refs_spec(skill=(sid, 1)), tenant=OTHER),
        ],
    )
    r = client.delete(f"{SK}/{sid}", headers=ACME)
    assert detail_code(r) == (409, "in_use")
    detail = r.json()["detail"]
    assert detail["agent_ids"] == ["ag-draft"] and detail["deployed_agent_ids"] == ["ag-live"]
    assert detail["other_tenant_agents"] == 1
    refs = client.get(f"{SK}/{sid}/references", headers=ACME).json()["used_by"]
    assert sorted(
        (r["id"], r["revision"], r["pins"]["current"], r["pins"]["deployed"]) for r in refs
    ) == [
        ("ag-draft", "current", 1, None),
        ("ag-live", "deployed", None, 1),
    ]
    mongo["agents"].delete_many({})
    r = client.delete(f"{SK}/{sid}", headers=ACME)
    assert r.status_code == 200 and r.json() == {"id": sid, "deleted": True}
    assert client.get(f"{SK}/{sid}", headers=ACME).status_code == 404
    assert mongo["skill_revisions"].count_documents({"skill_id": sid}) == 0


# -- 할당량 --------------------------------------------------------------------------------


def test_skill_count_limit(client):
    assert client.get("/settings").json()["skills"] == {
        "max_per_tenant": 200,
        "tenant_quota_mb": 64,
    }
    r = client.patch("/settings", json={"skills": {"max_per_tenant": 1, "tenant_quota_mb": 64}})
    assert r.status_code == 200, r.text
    created_skill(client)
    r = client.post(SK, json={"files": skill_files("second")})
    assert detail_code(r) == (409, "skill_limit")
    assert client.patch("/settings", json={"skills": None}).status_code == 422


def test_revision_bytes_quota(client):
    client.patch("/settings", json={"skills": {"max_per_tenant": 10, "tenant_quota_mb": 1}})
    big = {"a.md": "a" * 255 * KIB, "b.md": "b" * 255 * KIB}
    created_skill(client, "one", **big)
    created_skill(client, "two", **big)
    r = client.post(SK, json={"files": skill_files("three", **big)})
    assert detail_code(r) == (409, "skill_quota_exceeded")
    assert r.json()["detail"]["quota_bytes"] == KIB * KIB


# -- 접근 ----------------------------------------------------------------------------------


def _share(client, skill: dict, **access: Any):
    return client.put(f"{SK}/{skill['id']}/access", json=access)


def test_view_missing_is_404_and_manage_missing_is_403(client):
    skill = created_skill(client)
    # viewer 는 읽지만 쓰지 못한다
    with as_principal(client, "viewer", rbac.VIEWER):
        assert client.get(f"{SK}/{skill['id']}").status_code == 200
        r = client.post(SK, json={"files": skill_files("v")})
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "skill:write"
        assert client.post(f"{SK}/validate", json={"files": skill_files()}).status_code == 403
    with as_principal(client, "dev-other", rbac.DEVELOPER, tenant=OTHER):
        assert client.get(SK, headers=T2).json() == []
        assert client.get(f"{SK}/{skill['id']}", headers=T2).status_code == 404
    r = _share(client, skill, view={"tenants": [OTHER]}, consume={"tenants": [OTHER]})
    assert r.status_code == 200 and r.json()["scope"] == "shared"
    with as_principal(client, "viewer-other", rbac.VIEWER, tenant=OTHER):
        [row] = client.get(SK, headers=T2).json()
        assert row["tenant"] == "default"
        assert row["permissions"] == {"view": True, "consume": False, "manage": False}
    with as_principal(client, "dev-other", rbac.DEVELOPER, tenant=OTHER):
        row = client.get(f"{SK}/{skill['id']}", headers=T2).json()
        assert row["permissions"] == {"view": True, "consume": True, "manage": False}
        r = client.patch(f"{SK}/{skill['id']}", json={"display_name": "x"}, headers=T2)
        assert r.status_code == 403 and r.json()["detail"]["action"] == "manage"
        assert revise_skill(client, skill, "# x\n", expect=1, headers=T2).status_code == 403
        assert client.delete(f"{SK}/{skill['id']}", headers=T2).status_code == 403


def test_manage_stays_in_the_owner_tenant(client):
    skill = created_skill(client)
    for tenants in ([OTHER], ["*"]):
        r = _share(client, skill, manage={"tenants": tenants})
        assert detail_code(r) == (422, "cross_tenant_manage_unsupported"), tenants
    assert _share(client, skill, manage={"tenants": ["default"]}).status_code == 200
    # 넓히기는 skill:share 이고 좁히기는 manage 면 된다
    with as_principal(client, "dev", rbac.DEVELOPER):
        r = _share(client, skill, view={"tenants": [OTHER]})
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "skill:share"
    assert _share(client, skill, view={"tenants": [OTHER, "third"]}).status_code == 200
    with as_principal(client, "dev", rbac.DEVELOPER):
        r = _share(client, skill, view={"tenants": [OTHER]})
        assert r.status_code == 200, r.text
        assert r.json()["access"]["view"]["tenants"] == [OTHER]
        assert r.json()["access_updated_by"] == "dev"


def test_clone_needs_consume_and_renames_the_frontmatter(client):
    skill = created_skill(client, **{"references/a.md": "a"})
    revise_skill(client, skill, "# v2\n", expect=1)
    _share(client, skill, view={"tenants": [OTHER]})
    with as_principal(client, "dev-other", rbac.DEVELOPER, tenant=OTHER):
        r = client.post(f"{SK}/{skill['id']}/clone", json={"name": "copy"}, headers=T2)
        assert r.status_code == 403 and r.json()["detail"]["action"] == "consume"
    _share(client, skill, view={"tenants": [OTHER]}, consume={"tenants": [OTHER]})
    with as_principal(client, "dev-other", rbac.DEVELOPER, tenant=OTHER):
        r = client.post(
            f"{SK}/{skill['id']}/clone",
            json={"name": "copy", "display_name": "사본", "revision": 1},
            headers=T2,
        )
        assert r.status_code == 201, r.text
        copy = r.json()
        assert copy["tenant"] == OTHER and copy["name"] == "copy" and copy["revision"] == 1
        rev = client.get(f"{SK}/{copy['id']}/revisions/1", headers=T2).json()
        md = next(f["content"] for f in rev["files"] if f["path"] == "SKILL.md")
        assert md == skill_md("copy") and rev["note"] is None
        assert rev["cloned_from"] == {"tenant": "default", "name": "demo", "revision": 1}
        [meta] = client.get(f"{SK}/{copy['id']}/revisions", headers=T2).json()
        assert meta["cloned_from"] == rev["cloned_from"]
        assert {f["path"] for f in rev["files"]} == {"SKILL.md", "references/a.md"}
        # 고쳐 쓸 수 있는 자기 Skill 이다
        assert copy["permissions"]["manage"]
    r = client.post(f"{SK}/{skill['id']}/clone", json={"name": "Bad Name"})
    assert detail_code(r) == (422, "invalid_name")


# -- 감사 ----------------------------------------------------------------------------------


def test_export_is_audited_but_other_reads_are_not(client, mongo):
    """내보내기는 다른 테넌트에 공유된 Skill 도 밖으로 내므로 성공한 읽기라도 남긴다. 판정은 라우팅
    전이라 경로 모양으로 한다. 묶음은 내려받기 헤더로 나가고, 같은 리비전은 같은 바이트이며, 다시
    가져오면 마지막 리비전과 같다."""
    skill = created_skill(client, **{"references/a.md": "a"})
    mongo["audit_events"].delete_many({})
    r = client.get(f"{SK}/{skill['id']}/revisions/1/archive")
    assert r.status_code == 200
    assert client.get(f"{SK}/{skill['id']}/revisions/1").status_code == 200
    assert client.get(f"{SK}/{skill['id']}").status_code == 200
    rows = list(mongo["audit_events"].find())
    assert [(r["method"], r["action"], r["resource_id"]) for r in rows] == [
        ("GET", "export_skill_revision", skill["id"])
    ]
    assert r.headers["content-disposition"].startswith('attachment; filename="demo-r1.zip"')
    assert r.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert sorted(archive.namelist()) == ["demo/SKILL.md", "demo/references/a.md"]
    assert client.get(f"{SK}/{skill['id']}/revisions/1/archive").content == r.content
    back = client.post(
        f"{SK}/{skill['id']}/revisions/import",
        files={"file": ("demo-r1.zip", r.content, "application/zip")},
        data={"expect_revision": "1"},
    )
    assert detail_code(back) == (422, "skill_unchanged")


def test_audit_names_handlers_and_keeps_no_body(client, mongo):
    skill = created_skill(client, **{"notes.md": MARKER})
    client.post(
        f"{SK}/import",
        files={"file": ("x.md", skill_md("solo", body=MARKER).encode(), "text/markdown")},
    )
    revise_skill(client, skill, MARKER + "\n", expect=1)
    _share(client, skill, view={"tenants": [OTHER]})
    client.post(f"{SK}/{skill['id']}/clone", json={"name": "copy"})
    client.delete(f"{SK}/{skill['id']}")
    rows = list(mongo["audit_events"].find({"resource": "skills"}))
    assert {r["action"] for r in rows} == {
        "create_skill",
        "import_skill",
        "add_skill_revision",
        "set_skill_access",
        "clone_skill",
        "delete_skill",
    }
    assert MARKER not in json.dumps(rows, default=str)
