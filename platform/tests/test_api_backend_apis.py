import asyncio
from datetime import timedelta

import gridfs
import pytest
import yaml
from pymongo.errors import DuplicateKeyError

from daegam.pipeline.convert import S2O_VERSION
from daegam.services import backend_apis as svc_module
from daegam.services.backend_apis import BackendAPIError, BackendAPIUpload, UploadConnection
from daegam.store.base import utcnow
from daegam.store.leases import LEASES
from tests.helpers.auth import bearer
from tests.helpers.backend_apis import (
    backend_version,
    fixture_bytes,
    stored_body,
    stored_files,
    upload_backend,
)
from tests.helpers.consts import BP, T2, TENANT_HEADER
from tests.helpers.http import detail_code


def test_upload_then_get(client):
    r = upload_backend(client, "calc", "calculator.wsdl")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "calc"
    assert body["version"] == 1
    assert body["kind"] == "soap"
    assert body["tool_count"] == 2
    assert body["tool_names"] == ["Add", "Divide"]
    assert body["report"]["ok"] is True
    assert body["report"]["summary"]["skip"] == 0
    assert body["endpoint"] is None
    assert body["auth"] == "none"
    assert body["credential_secret"] is None
    assert "spec_id" not in body
    pid = body["id"]

    r = client.get(f"{BP}/{pid}")
    assert r.status_code == 200
    assert r.json()["id"] == pid
    assert r.json()["latest_version"] == 1
    assert r.json()["tool_names"] == ["Add", "Divide"]

    r = client.get(BP)
    assert [p["id"] for p in r.json()] == [pid]

    r = client.get(f"{BP}/{pid}/versions")
    assert r.status_code == 200
    assert [v["version"] for v in r.json()] == [1]
    assert r.json()[0]["backend_profile_id"] == pid
    assert "results" not in r.json()[0]["report"]

    r = client.get(f"{BP}/{pid}/versions/1")
    assert r.status_code == 200
    meta = r.json()
    assert meta["report"]["complete"] is True
    assert isinstance(meta["report"]["results"], list)
    # 설치된 판을 그대로 싣는지 본다. 숫자를 박아 두면 상류를 올릴 때마다 이 단정이 깨진다
    assert meta["s2o_version"] == S2O_VERSION
    assert meta["source_sha256"] != meta["output_sha256"]
    assert meta["source_ext"] == "wsdl"
    assert [d["operation_id"] for d in meta["tool_details"]] == ["Add", "Divide"]
    assert all(d["summary"] or d["description"] for d in meta["tool_details"])

    assert client.get(f"{BP}/{pid}/versions/2").status_code == 404


def test_tenant_isolation(client):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    assert client.get(BP, headers=T2).json() == []
    assert client.get(f"{BP}/{pid}", headers=T2).status_code == 404
    assert client.get(f"{BP}/{pid}/versions", headers=T2).status_code == 404
    assert client.patch(f"{BP}/{pid}", json={"name": "x"}, headers=T2).status_code == 404
    assert client.delete(f"{BP}/{pid}", headers=T2).status_code == 404
    # 같은 이름을 다른 테넌트가 써도 별개 프로필이다
    r2 = upload_backend(client, "calc", "orders.wsdl", headers=T2)
    assert r2.status_code == 201
    assert r2.json()["id"] != pid
    assert [p["id"] for p in client.get(BP).json()] == [pid]


def test_invalid_name(client):
    r = upload_backend(client, "bad name!")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_name"


# -- endpoint / auth / credential_secret -----------------------------------


def test_patch_checks_fields_in_the_old_order(client):
    """서비스로 옮긴 뒤에도 칸 검사 차례가 같다. endpoint 가 auth 의 null 보다 먼저다 (P75 2절)."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    r = client.patch(f"{BP}/{pid}", json={"endpoint": "bad", "auth": None})
    assert detail_code(r) == (422, "invalid_endpoint")
    r = client.patch(f"{BP}/{pid}", json={"auth": None})
    assert detail_code(r) == (422, "invalid_auth")


def test_patch_cannot_rename(client):
    """name 은 생성 뒤 바꿀 수 없다 (D24). 이름을 바꾸려면 새로 만든다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    r = client.patch(f"{BP}/{pid}", json={"name": "calculator"})
    assert detail_code(r) == (422, "name_immutable")
    assert client.get(f"{BP}/{pid}").json()["name"] == "calc"
    # 같은 이름으로 재업로드하면 같은 프로필의 dedup
    assert upload_backend(client, "calc", "calculator.wsdl").status_code == 200
    # 다른 이름으로 올리면 새 프로필이 생긴다
    r = upload_backend(client, "calculator", "calculator.wsdl", display_name="계산기")
    assert r.status_code == 201
    assert r.json()["id"] != pid and r.json()["display_name"] == "계산기"


def test_patch_name_immutable(client):
    """name 은 생성 뒤 바꿀 수 없다 (D24). 다른 이름은 422 name_immutable, 같은 이름은 통과."""
    a = upload_backend(client, "a", "calculator.wsdl").json()["id"]
    upload_backend(client, "b", "orders.wsdl")
    r = client.patch(f"{BP}/{a}", json={"name": "b"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "name_immutable"
    assert client.get(f"{BP}/{a}").json()["name"] == "a"
    # 자기 이름을 다시 보내는 것은 통과한다
    assert client.patch(f"{BP}/{a}", json={"name": "a"}).status_code == 200
    # 다른 테넌트에만 있는 이름이라도 바꿀 수 없다
    upload_backend(client, "c", "calculator.wsdl", headers=T2)
    r = client.patch(f"{BP}/{a}", json={"name": "c"})
    assert detail_code(r) == (422, "name_immutable")
    # display_name 은 바뀐다
    r = client.patch(f"{BP}/{a}", json={"display_name": "  계산기 SOAP  "})
    assert r.status_code == 200 and r.json()["display_name"] == "계산기 SOAP"
    assert r.json()["name"] == "a"


def test_patch_validation(client):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    assert client.patch(f"{BP}/{pid}", json={}).json()["detail"]["code"] == "empty_patch"
    r = client.patch(f"{BP}/{pid}", json={"auth": "digest"})
    assert detail_code(r) == (422, "invalid_auth")
    r = client.patch(f"{BP}/{pid}", json={"endpoint": "ftp://x"})
    assert detail_code(r) == (422, "invalid_endpoint")
    r = client.patch(f"{BP}/{pid}", json={"endpoint": "http://a b/"})
    assert detail_code(r) == (422, "invalid_endpoint")
    r = client.patch(f"{BP}/{pid}", json={"name": "bad name"})
    assert detail_code(r) == (422, "name_immutable")
    r = client.patch(f"{BP}/{pid}", json={"display_name": "   "})
    assert detail_code(r) == (422, "invalid_display_name")
    r = client.patch(f"{BP}/{pid}", json={"auth": "basic"})
    assert detail_code(r) == (422, "credential_required")
    r = client.patch(f"{BP}/{pid}", json={"credential_secret": "Not_Valid"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_credential_secret"
    r = client.patch(f"{BP}/{pid}", json={"unknown": 1})
    assert r.status_code == 422
    # 실패한 PATCH는 아무것도 바꾸지 않는다
    got = client.get(f"{BP}/{pid}").json()
    assert (got["auth"], got["endpoint"], got["credential_secret"]) == ("none", None, None)


def test_upload_validation_of_connection_fields(client):
    r = upload_backend(client, "calc", "calculator.wsdl", auth="basic")
    assert detail_code(r) == (422, "credential_required")
    r = upload_backend(client, "calc", "calculator.wsdl", endpoint="not a url")
    assert detail_code(r) == (422, "invalid_endpoint")
    assert client.get(BP).json() == []


# -- spec (UI 문서 뷰어용 JSON) --------------------------------------------------


def test_spec_is_the_json_of_the_yaml_the_init_container_gets(client):
    """화면이 읽는 `/spec` 은 initContainer 가 받는 YAML 과 같은 문서의 JSON 이다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    tok = client.post(f"{BP}/{pid}/tokens").json()["token"]
    fresh = client.get(f"{BP}/{pid}/versions/1/openapi.yaml", headers=bearer(tok))
    assert fresh.status_code == 200, fresh.text
    spec = client.get(f"{BP}/{pid}/versions/1/spec")
    assert spec.headers["content-type"].startswith("application/json")
    assert yaml.safe_load(fresh.content) == spec.json()


def test_read_spec_404_for_missing_version_profile_or_other_tenant(client):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    assert client.get(f"{BP}/{pid}/versions/2/spec").status_code == 404
    assert client.get(f"{BP}/nope/versions/1/spec").status_code == 404
    r = client.get(f"{BP}/{pid}/versions/1/spec", headers=T2)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_read_spec_500_when_body_is_missing(client, mongo):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    bucket = gridfs.GridFSBucket(mongo, bucket_name="spec_bodies")
    file_id = mongo["spec_bodies.files"].find_one({"filename": f"{pid}/1/openapi.yaml"})["_id"]
    bucket.delete(file_id)
    r = client.get(f"{BP}/{pid}/versions/1/spec")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "integrity_error"


def test_read_spec_uses_user_auth_not_profile_tokens(oidc, token):
    """oidc 모드: 소속이 없는 사용자는 403, 다른 테넌트 소속은 404, 프로필 읽기 토큰은 401 이다."""
    dev_a = bearer(token("a1", "daegam-a-dev"))
    dev_b = bearer(token("b1", "daegam-b-dev"))
    nobody = bearer(token("n1"))
    pid = upload_backend(oidc, "calc", "calculator.wsdl", headers=dev_a).json()["id"]
    spec = f"{BP}/{pid}/versions/1/spec"

    assert oidc.get(spec, headers=dev_a).status_code == 200
    r = oidc.get(spec, headers=nobody)
    assert detail_code(r) == (403, "not_member")
    r = oidc.get(spec, headers={**dev_b, TENANT_HEADER: "a"})
    assert detail_code(r) == (403, "not_member")
    # b 소속으로 보면 a 의 프로필은 없는 것이다
    assert oidc.get(spec, headers=dev_b).status_code == 404
    # initContainer 용 프로필 토큰으로는 열리지 않는다 (그 토큰은 openapi.yaml 전용이다)
    tok = oidc.post(f"{BP}/{pid}/tokens", headers=dev_a).json()["token"]
    assert oidc.get(spec, headers=bearer(tok)).status_code == 401
    y = oidc.get(f"{BP}/{pid}/versions/1/openapi.yaml", headers=bearer(tok))
    assert y.status_code == 200
    assert oidc.get(spec, headers={}).status_code == 401


# -- source (편집 화면이 보여 주는 등록된 원본) ---------------------------------------


def test_read_source_404_for_missing_version_profile_or_other_tenant(client):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    # WSDL 로 올렸으면 변환본이 아니라 올린 WSDL 이 나온다
    got = client.get(f"{BP}/{pid}/versions/1/source").json()
    assert got["text"] == fixture_bytes("calculator.wsdl").decode("utf-8")
    assert got["source_ext"] == "wsdl" and got["source_format"] == "wsdl"
    assert client.get(f"{BP}/{pid}/versions/2/source").status_code == 404
    assert client.get(f"{BP}/nope/versions/1/source").status_code == 404
    r = client.get(f"{BP}/{pid}/versions/1/source", headers=T2)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_read_source_500_when_body_is_missing(client, mongo):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    bucket = gridfs.GridFSBucket(mongo, bucket_name="spec_bodies")
    file_id = mongo["spec_bodies.files"].find_one({"filename": f"{pid}/1/source.wsdl"})["_id"]
    bucket.delete(file_id)
    r = client.get(f"{BP}/{pid}/versions/1/source")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "integrity_error"


# -- delete ------------------------------------------------------------------


def test_delete_with_tokens_409_then_force(client, mongo):
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    t1 = client.post(f"{BP}/{pid}/tokens", json={"label": "a"}).json()["token_id"]
    t2 = client.post(f"{BP}/{pid}/tokens").json()["token_id"]

    r = client.delete(f"{BP}/{pid}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "tokens_active"
    assert sorted(r.json()["detail"]["token_ids"]) == sorted([t1, t2])
    assert client.get(f"{BP}/{pid}").status_code == 200

    r = client.delete(f"{BP}/{pid}?force=true")
    assert r.status_code == 200
    assert r.json()["id"] == pid
    assert sorted(r.json()["revoked_token_ids"]) == sorted([t1, t2])
    assert client.get(f"{BP}/{pid}").status_code == 404
    assert client.get(BP).json() == []
    # GridFS 본문과 chunk까지 지워진다
    assert stored_files(mongo, pid) == []
    assert mongo["spec_bodies.chunks"].count_documents({}) == 0


def test_patch_from_an_older_read_is_a_revision_conflict(client, mongo):
    """서버가 읽은 뒤 다른 저장이 지나갔으면 보낸 칸을 쓰지 않고 409 revision_conflict 다. 저장은
    보낸 칸만 쓴다 (P68 2절)."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    stale = client.portal.call(svc.store.get, pid)
    r = client.patch(f"{BP}/{pid}", json={"endpoint": "https://soap.example.com/a"})
    assert r.status_code == 200, r.text
    with pytest.raises(BackendAPIError) as e:
        client.portal.call(lambda: svc.update(stale, {"display_name": "B"}, user_sub="u"))
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    doc = mongo["backend_apis"].find_one({"_id": pid})
    assert doc["endpoint"] == "https://soap.example.com/a" and doc["display_name"] == "calc"
    assert doc["seq"] == 1


def _variant(word: bytes) -> bytes:
    """calculator.wsdl 의 한 문장을 바꾼 원본. 원본이 다르면 업로드마다 새 버전이 된다."""
    return fixture_bytes("calculator.wsdl").replace(b"Adds two", word)


async def _service_upload(svc, pid: str, body: bytes, *, endpoint: str | None = None):
    """calc 를 서비스로 바로 올린다. 포털 안에서 다른 업로드와 겹치게 부르는 시험이 쓴다."""
    tenant = (await svc.store.get(pid)).tenant
    conn = UploadConnection.of(endpoint, None, None)
    up = BackendAPIUpload("calc", "calc", False, conn, body, "calculator.wsdl")
    return await svc.upload(tenant, up, user_sub="u")


def _assert_v2_is(client, mongo, pid: str, version, body: bytes) -> None:
    """v2 가 body 의 버전이고, 두 본문이 기록된 해시와 맞아 그대로 읽힌다."""
    files = client.app.state.backend_api_service.files
    assert stored_body(mongo, f"{pid}/2/source.wsdl") == body
    assert len(stored_files(mongo, pid)) == 4
    assert client.portal.call(svc_module.verified_source_body, files, version) == body
    assert client.portal.call(svc_module.verified_spec_body, files, version).startswith(b"openapi")


def test_an_upload_after_the_latest_version_moved_is_a_revision_conflict(client):
    """업로드가 읽은 최신 버전이 본문을 쓰기 전에 바뀌었으면 새 버전을 만들지 않고 409
    revision_conflict 다. 그 업로드의 같은 원본 확인이 옛 버전을 보고 내린 판단이기 때문이다. 함께
    보낸 접속 정보도 바꾸지 않는다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    inspect = svc_module.inspect_and_accept

    async def run() -> None:
        async def slow_inspect(*a: object, **kw: object) -> object:
            # 이 업로드가 검사하는 사이 다른 업로드가 v2 를 만든다
            svc_module.inspect_and_accept = inspect
            await _service_upload(svc, pid, _variant(b"Sums two"))
            return await inspect(*a, **kw)

        svc_module.inspect_and_accept = slow_inspect
        try:
            await _service_upload(
                svc, pid, _variant(b"Plus two"), endpoint="https://soap.example.com/a"
            )
        finally:
            svc_module.inspect_and_accept = inspect

    with pytest.raises(BackendAPIError) as e:
        client.portal.call(run)
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    assert [v["version"] for v in client.get(f"{BP}/{pid}/versions").json()] == [1, 2]
    assert client.get(f"{BP}/{pid}").json()["endpoint"] is None


def test_an_upload_whose_lease_expired_leaves_the_next_version_alone(client, mongo, monkeypatch):
    """리스 안에서 최신 버전을 읽고 기한을 넘겨 멈춘 업로드는, 그 사이 리스를 이어받은 업로드가 넣은
    버전의 본문을 지우지 않고 409 revision_conflict 다. 본문 쓰기는 그 번호의 파일부터 지운다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    other = _variant(b"Sums two")
    latest = svc.store.latest_version
    reads = 0

    async def stall_after_read(profile_id: str) -> object:
        nonlocal reads
        reads += 1
        got = await latest(profile_id)
        if reads == 2:
            # 둘째 읽기가 리스 안의 읽기다. 그 뒤 기한을 넘겨 멈춘 사이 다른 업로드가 v2 를 넣는다
            await svc.db[LEASES].update_one(
                {"_id": svc_module.version_lease_name(pid)},
                {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}},
            )
            await _service_upload(svc, pid, other)
        return got

    monkeypatch.setattr(svc.store, "latest_version", stall_after_read)
    with pytest.raises(BackendAPIError) as e:
        client.portal.call(_service_upload, svc, pid, _variant(b"Plus two"))
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    monkeypatch.undo()
    version = client.portal.call(svc.store.get_version, pid, 2)
    _assert_v2_is(client, mongo, pid, version, other)


def test_an_edit_during_inspection_refuses_the_upload_before_anything_is_written(client, mongo):
    """업로드가 검사하는 사이 다른 저장(PATCH)이 지나가면 접속 정보 저장이 리스 안에서 엇갈려 409
    revision_conflict 다. 본문도 버전 기록도 쓰지 않고, 접속 정보는 PATCH 의 것만 남는다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    inspect = svc_module.inspect_and_accept

    async def run() -> None:
        async def slow_inspect(*a: object, **kw: object) -> object:
            fresh = await svc.store.get(pid)
            await svc.update(fresh, {"display_name": "B"}, user_sub="other")
            return await inspect(*a, **kw)

        svc_module.inspect_and_accept = slow_inspect
        try:
            await _service_upload(
                svc, pid, _variant(b"Plus two"), endpoint="https://soap.example.com/a"
            )
        finally:
            svc_module.inspect_and_accept = inspect

    with pytest.raises(BackendAPIError) as e:
        client.portal.call(run)
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    assert [v["version"] for v in client.get(f"{BP}/{pid}/versions").json()] == [1]
    assert len(stored_files(mongo, pid)) == 2
    got = client.get(f"{BP}/{pid}").json()
    assert (got["display_name"], got["endpoint"]) == ("B", None)


def test_a_writer_that_lost_the_lease_after_its_bodies_does_not_insert(client, mongo, monkeypatch):
    """본문을 쓴 뒤 리스가 만료돼 다른 업로드가 이어받았으면, 기록을 넣기 전에 늘려 보고 409
    revision_conflict 로 물러난다. 이어받은 업로드가 그 번호에 쓴 본문과 기록이 v2 가 된다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    first, second = _variant(b"Plus two"), _variant(b"Sums two")
    write = svc.files.write
    second_wrote, first_done = asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def hooked_write(profile_id: str, version: int, **kw: object) -> None:
        await write(profile_id, version, **kw)
        if not tasks:
            # 첫 업로드가 본문을 쓴 뒤 기한을 넘겨 멈춘 사이 둘째 업로드가 리스를 이어받아
            # 본문을 쓴다
            await svc.db[LEASES].update_one(
                {"_id": svc_module.version_lease_name(pid)},
                {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}},
            )
            tasks.append(asyncio.create_task(_service_upload(svc, pid, second)))
            await second_wrote.wait()
        else:
            # 둘째 업로드는 첫 업로드가 끝날 때까지 기록을 넣지 않는다
            second_wrote.set()
            await first_done.wait()

    monkeypatch.setattr(svc.files, "write", hooked_write)

    async def run() -> tuple[BaseException | None, object]:
        try:
            await _service_upload(svc, pid, first)
            err = None
        except BackendAPIError as e:
            err = e
        finally:
            first_done.set()
        return err, await tasks[0]

    err, (_profile, version, created) = client.portal.call(run)
    assert isinstance(err, BackendAPIError) and (err.status, err.code) == (409, "revision_conflict")
    assert created and version.version == 2
    monkeypatch.undo()
    _assert_v2_is(client, mongo, pid, version, second)


def test_a_version_inserted_without_the_lease_makes_the_insert_a_revision_conflict(
    client, monkeypatch
):
    """리스 없이 쓰는 옛 판 파드가 같은 번호를 먼저 넣었으면 넣기가 버전 색인에 부딪쳐 500 이 아니라
    409 revision_conflict 다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    write = svc.files.write

    async def older_pod_inserts(profile_id: str, version: int, **kw: object) -> None:
        await write(profile_id, version, **kw)
        await svc.store.add_version(backend_version(["Add"], bp=profile_id, version=version))

    monkeypatch.setattr(svc.files, "write", older_pod_inserts)
    with pytest.raises(BackendAPIError) as e:
        client.portal.call(_service_upload, svc, pid, _variant(b"Plus two"))
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    assert isinstance(e.value.__cause__, DuplicateKeyError)


def test_a_delete_during_a_version_write_is_a_revision_conflict(client, mongo):
    """버전을 쓰는 중인 Backend API 의 삭제는 409 revision_conflict 다. 겹치면 지운 문서 아래에 버전
    기록과 본문이 남는다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    held = {"_id": svc_module.version_lease_name(pid), "holder": "other-pod/upload"}
    mongo[LEASES].insert_one({**held, "expires_at": utcnow() + timedelta(minutes=1)})
    assert detail_code(client.delete(f"{BP}/{pid}")) == (409, "revision_conflict")
    assert client.get(f"{BP}/{pid}").status_code == 200

    mongo[LEASES].delete_one({"_id": held["_id"]})
    assert client.delete(f"{BP}/{pid}").status_code == 200
    assert mongo[LEASES].count_documents({"_id": held["_id"]}) == 0


def test_bodies_left_by_a_stopped_upload_go_before_the_number_is_reused(client, mongo):
    """본문만 쓰고 버전 기록을 넣기 전에 멈춘 업로드가 남긴 본문은, 다음 업로드가 같은 번호를 쓰기
    전에 지운다. 확장자가 다른 원본이 남아 있으면 원본 읽기가 그것을 집을 수 있다."""
    pid = upload_backend(client, "calc", "calculator.wsdl").json()["id"]
    svc = client.app.state.backend_api_service
    client.portal.call(
        lambda: svc.files.write(
            pid, 2, openapi_yaml="openapi: 3.0.0\n", source=b"{}", source_ext="json", kind="openapi"
        )
    )
    modified = _variant(b"Sums two")
    assert upload_backend(client, "calc", body=modified).status_code == 201
    names = sorted(f["filename"] for f in stored_files(mongo, pid) if f["metadata"]["version"] == 2)
    assert names == [f"{pid}/2/openapi.yaml", f"{pid}/2/source.wsdl"]
    assert client.get(f"{BP}/{pid}/versions/2/source").json()["text"] == modified.decode()
