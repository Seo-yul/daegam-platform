import hashlib

import gridfs

from tests.helpers.auth import bearer
from tests.helpers.backend_apis import stored_body, uploaded_backend
from tests.helpers.consts import BP, T2


def _yaml_url(pid, v=1):
    return f"{BP}/{pid}/versions/{v}/openapi.yaml"


def test_revoked_token_rejected(client, mongo):
    pid = uploaded_backend(client, "a", "calculator.wsdl")["id"]
    issued = client.post(f"{BP}/{pid}/tokens").json()
    auth = bearer(issued["token"])
    # 다른 프로필의 토큰으로는 이 프로필의 명세를 읽지 못한다
    other = uploaded_backend(client, "b", "orders.wsdl")["id"]
    tok_other = client.post(f"{BP}/{other}/tokens").json()["token"]
    assert client.get(_yaml_url(pid), headers=bearer(tok_other)).status_code == 401
    assert client.get(_yaml_url(other), headers=bearer(tok_other)).status_code == 200
    # DB 에는 sha256 만 남는다
    [doc] = mongo["backend_api_tokens"].find({"backend_profile_id": pid})
    assert doc["token_sha256"] == hashlib.sha256(issued["token"].encode()).hexdigest()
    assert issued["token"] not in str(doc)
    # 테넌트 헤더 없이 Bearer 만으로 읽고, 토큰이 없으면 401 이다
    assert client.get(_yaml_url(pid)).status_code == 401
    assert client.get(_yaml_url(pid), headers=auth).status_code == 200

    r = client.delete(f"{BP}/{pid}/tokens/{issued['token_id']}")
    assert r.status_code == 200
    assert client.get(_yaml_url(pid), headers=auth).status_code == 401
    listed = client.get(f"{BP}/{pid}/tokens").json()
    assert listed[0]["active"] is False
    assert listed[0]["revoked_at"] is not None
    # 목록에는 sha256 도 원문도 없다
    assert "token" not in listed[0] and "token_sha256" not in listed[0]
    # 회수는 멱등이다
    assert client.delete(f"{BP}/{pid}/tokens/{issued['token_id']}").status_code == 200
    assert client.delete(f"{BP}/{pid}/tokens/missing").status_code == 404


def test_token_endpoints_are_tenant_scoped(client):
    pid = uploaded_backend(client)["id"]
    assert client.post(f"{BP}/{pid}/tokens", headers=T2).status_code == 404
    assert client.get(f"{BP}/{pid}/tokens", headers=T2).status_code == 404


def test_yaml_missing_version_404_after_auth(client):
    pid = uploaded_backend(client)["id"]
    token = client.post(f"{BP}/{pid}/tokens").json()["token"]
    r = client.get(_yaml_url(pid, 9), headers=bearer(token))
    assert r.status_code == 404


def test_yaml_integrity_error_when_body_tampered(client, mongo, caplog):
    pid = uploaded_backend(client)["id"]
    token = client.post(f"{BP}/{pid}/tokens").json()["token"]
    auth = bearer(token)
    assert client.get(_yaml_url(pid), headers=auth).status_code == 200

    # GridFS 본문을 직접 바꿔치기해 output_sha256과 어긋나게 만든다
    bucket = gridfs.GridFSBucket(mongo, bucket_name="spec_bodies")
    name = f"{pid}/1/openapi.yaml"
    original = stored_body(mongo, name)
    bucket.delete_by_name(name)
    bucket.upload_from_stream(name, original + b"\n# tampered\n")

    r = client.get(_yaml_url(pid), headers=auth)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "integrity_error"
    assert any("sha256 mismatch" in rec.message for rec in caplog.records)
    # 메타 조회는 영향 없다
    assert client.get(f"{BP}/{pid}/versions/1").status_code == 200
