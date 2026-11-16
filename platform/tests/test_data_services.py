"""F4 Data Service. fake 클러스터로 Secret·Namespace·MCPGroup 생성을 확인한다.
verify 는 postgres 를 psycopg 목으로(D32), redis 를 redis-py 가짜 클라이언트로 본다(D33)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import ANY

import psycopg
import pytest
from redis.exceptions import AuthenticationError, NoPermissionError, RedisError, ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from daegam.secrets import K8sSecretStore
from daegam.services import data_services as data_services_service
from daegam.services.data_services import (
    ClusterMismatch,
    check_same_cluster,
    checkpointer_capability,
    data_service_password,
    verify_redis,
)
from daegam.services.tenancy import TenancyError, group_name_for, k8s_name
from daegam.store.data_services import DataService
from tests.helpers.clusters import default_cluster, register_cluster
from tests.helpers.consts import DS, GV, T2, TENANT_HEADER
from tests.helpers.data_services import FakeRedis
from tests.helpers.docs import data_service_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.fake_pg import EXT_AVAILABLE, EXT_MISSING, FakeConn
from tests.helpers.http import detail_code


def _bootstrap(client):
    """테넌트 네임스페이스에 라벨·쿼터·LimitRange 를 다는 기본 클러스터."""
    return default_cluster(
        client,
        namespaces={
            "labels": {"env": "test"},
            "quota": {"pods": "10"},
            "limit_range": {"limits": [{"type": "Container", "default": {"cpu": "500m"}}]},
        },
    )


def _create(client, name="cache", kind="redis", headers=None, **extra):
    body = {
        "name": name,
        "kind": kind,
        "host": "redis.svc",
        "port": 6379,
        "password": "s3cret",
        **extra,
    }
    if kind == "redis" and "db" not in body and "database" not in body:
        body["db"] = 0
    return client.post(DS, json=body, headers=headers or {})


def test_create_makes_namespace_group_and_secret(client, fake_clusters, mongo):
    cid = _bootstrap(client)
    r = _create(client, username="app")
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["cluster_id"] == cid
    assert ds["secret_name"] == f"ds-{ds['id']}"
    assert ds["namespace"] == "daegam-default"
    assert ds["username"] == "app"
    assert "password" not in ds and "s3cret" not in r.text

    fake = fake_clusters.by_name["edge"]
    ns = fake.get("v1", "Namespace", "daegam-default")
    assert ns["metadata"]["labels"]["env"] == "test"
    assert ns["metadata"]["labels"]["daegam.devops.ai.kr/tenant"] == "default"
    assert fake.get("v1", "ResourceQuota", "daegam-tenant-quota", "daegam-default")["spec"][
        "hard"
    ] == {"pods": "10"}
    assert (
        fake.get("v1", "LimitRange", "daegam-tenant-limits", "daegam-default")["spec"]["limits"][0][
            "type"
        ]
        == "Container"
    )
    group = fake.get(GV, "MCPGroup", "default", "daegam-default")
    assert group["spec"]["description"] == "daegam tenant default"
    assert group["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "tenant-group"
    secret = fake.get("v1", "Secret", f"ds-{ds['id']}", "daegam-default")
    assert secret["type"] == "Opaque"
    assert secret["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "data-service"
    import base64

    assert base64.b64decode(secret["data"]["password"]) == b"s3cret"
    assert base64.b64decode(secret["data"]["username"]) == b"app"

    doc = mongo["data_services"].find_one({"_id": ds["id"]})
    assert "s3cret" not in str(doc)
    assert doc["secret_name"] == f"ds-{ds['id']}"

    assert client.get(f"{DS}/{ds['id']}").json()["host"] == "redis.svc"
    assert [d["name"] for d in client.get(DS).json()] == ["cache"]
    assert client.get(DS, headers=T2).json() == []
    assert client.get(f"{DS}/{ds['id']}", headers=T2).status_code == 404
    # 둘째도 같은 네임스페이스·그룹을 쓰고, 다른 테넌트는 자기 네임스페이스·그룹을 만든다
    assert _create(client, "b").status_code == 201
    assert len(fake.find("Namespace")) == 1
    assert len(fake.find("MCPGroup")) == 1
    client.patch(
        "/tenants/other/settings", json={"namespace": {"name": "other-ns", "labels": {"team": "o"}}}
    )
    assert _create(client, "c", headers=T2).status_code == 201
    assert fake.get("v1", "Namespace", "other-ns")["metadata"]["labels"]["team"] == "o"
    assert fake.get(GV, "MCPGroup", "other", "other-ns") is not None


def test_cluster_resolution(client, fake_clusters):
    """명시한 것을 먼저 보고 없으면 테넌트 기본, 플랫폼 기본, home 순으로 내려간다.
    허용 목록도 본다."""
    # 아무 기본도 없고 home 도 없으면 422
    r = _create(client)
    assert detail_code(r) == (422, "no_cluster")
    cid = _bootstrap(client)
    cid2 = register_cluster(client, "second")
    client.patch(
        "/tenants/acme/settings", json={"default_cluster_id": cid2, "allowed_cluster_ids": [cid2]}
    )
    acme = {TENANT_HEADER: "acme"}
    ds = _create(client, "x", headers=acme).json()
    assert ds["cluster_id"] == cid2  # 테넌트 기본이 플랫폼 기본을 이긴다
    r = _create(client, "y", headers=acme, cluster_id=cid)
    assert detail_code(r) == (422, "cluster_not_allowed")
    r = _create(client, "z", cluster_id="nope")
    assert detail_code(r) == (422, "unknown_cluster")
    assert _create(client, "w", cluster_id=cid2).json()["cluster_id"] == cid2


def test_validation_and_conflict(client):
    _bootstrap(client)
    assert _create(client).status_code == 201
    r = _create(client)
    assert detail_code(r) == (409, "name_conflict")
    r = _create(client, "pg", kind="postgres", db=1)
    assert detail_code(r) == (422, "invalid_value")
    r = _create(client, "rd", kind="redis", database="x")
    assert r.status_code == 422
    r = _create(client, "bad name")
    assert detail_code(r) == (422, "invalid_name")
    # 무인증 Redis 는 password 없이 만들 수 있고, postgres 는 필수다
    r = _create(client, "np", password="")
    assert r.status_code == 201 and r.json()["has_password"] is False
    r = _create(client, "pgnp", kind="postgres", database="d", password="")
    assert detail_code(r) == (422, "invalid_value")
    r = client.post(
        DS, json={"name": "p", "kind": "postgres", "host": "h", "port": 70000, "password": "x"}
    )
    assert r.status_code == 422
    r = _create(client, "pg", kind="postgres", database="app", username="u")
    assert r.status_code == 201 and r.json()["database"] == "app"


def test_patch_checks_fields_in_the_old_order(client):
    """서비스로 옮긴 뒤에도 칸 검사 차례가 같다. 빈 host 가 port 의 null 보다 먼저다 (P75 2절)."""
    _bootstrap(client)
    ds = _create(client).json()
    r = client.patch(f"{DS}/{ds['id']}", json={"host": "", "port": None})
    assert detail_code(r) == (422, "invalid_value")
    assert r.json()["detail"]["detail"] == "host cannot be empty"
    r = client.patch(f"{DS}/{ds['id']}", json={"port": None})
    assert r.json()["detail"]["detail"] == "port cannot be null"


def test_patch_updates_secret_and_resets_verified(client, fake_clusters, mongo):
    _bootstrap(client)
    ds = _create(client, username="app").json()
    fake = fake_clusters.by_name["edge"]
    import base64

    def secret():
        s = fake.get("v1", "Secret", ds["secret_name"], "daegam-default")
        return {k: base64.b64decode(v).decode() for k, v in s["data"].items()}

    mongo["data_services"].update_one(
        {"_id": ds["id"]}, {"$set": {"verified_at": ds["created_at"]}}
    )
    r = client.patch(f"{DS}/{ds['id']}", json={"host": "redis2.svc", "port": 6380, "db": 2})
    assert r.status_code == 200, r.text
    assert (r.json()["host"], r.json()["port"], r.json()["db"]) == ("redis2.svc", 6380, 2)
    assert secret() == {"password": "s3cret", "username": "app"}  # 비밀은 그대로
    # 확인은 확인한 주소에만 해당한다. 주소가 바뀌면 지운다 (P68 2절)
    assert r.json()["verified_at"] is None
    stored = mongo["data_services"].find_one({"_id": ds["id"]})
    assert stored["seq"] == 1 and "claim" not in stored

    r = client.patch(f"{DS}/{ds['id']}", json={"password": "new-pass"})
    assert r.status_code == 200
    assert r.json()["verified_at"] is None
    assert secret() == {"password": "new-pass", "username": "app"}

    r = client.patch(f"{DS}/{ds['id']}", json={"username": None})
    assert r.status_code == 200
    assert secret() == {"password": "new-pass"}

    # name 은 바꿀 수 없다 (D24). 같은 이름은 통과, 다른 이름은 422 name_immutable
    r = client.patch(f"{DS}/{ds['id']}", json={"name": "cache2"})
    assert detail_code(r) == (422, "name_immutable")
    _create(client, "taken")
    r = client.patch(f"{DS}/{ds['id']}", json={"name": "taken"})
    assert detail_code(r) == (422, "name_immutable")
    r = client.patch(f"{DS}/{ds['id']}", json={"name": "cache", "display_name": "캐시"})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["display_name"]) == ("cache", "캐시")
    assert client.patch(f"{DS}/{ds['id']}", json={}).status_code == 422
    assert (
        client.patch(f"{DS}/{ds['id']}", json={"database": "x"}).status_code == 422
    )  # redis 에 database
    assert client.patch(f"{DS}/nope", json={"host": "h"}).status_code == 404


def test_unreachable_cluster_is_502(client, fake_clusters, mongo):
    """클러스터에 연결할 수 없으면 어느 단계에서든 502 cluster_unreachable 이다. 라우터가 잡지 않고
    daegam/main.py 의 전역 핸들러가 낸다. 삭제의 Secret 지우기는 예전에 잡는 자리가 없어 500
    이었다."""
    _bootstrap(client)
    ds = _create(client).json()
    fake_clusters.by_name["edge"].unreachable = True
    r = _create(client, "other")
    assert r.status_code == 502, r.text
    assert r.json()["detail"] == {"code": "cluster_unreachable", "detail": ANY}
    r = client.delete(f"{DS}/{ds['id']}")
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "cluster_unreachable"
    assert mongo["data_services"].find_one({"_id": ds["id"]}) is not None


def test_a_verify_across_a_host_change_stores_nothing(client, fake_clusters, mongo, monkeypatch):
    """읽은 뒤 주소가 바뀌었으면 verify 결과는 지금의 주소를 확인한 것이 아니다. 남기지 않고 409
    revision_conflict 다 (P68 2절)."""
    _bootstrap(client)
    ds = _create(client).json()

    async def verify_while_moved(_ds, _password, **_kw):
        mongo["data_services"].update_one(
            {"_id": ds["id"]}, {"$set": {"host": "moved.svc"}, "$inc": {"seq": 1}}
        )
        return data_services_service.VerifyResult(ok=True, redis={"checkpointer": "ready"})

    monkeypatch.setattr(data_services_service, "verify_connection_async", verify_while_moved)
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert detail_code(r) == (409, "revision_conflict")
    stored = mongo["data_services"].find_one({"_id": ds["id"]})
    assert stored["host"] == "moved.svc" and stored["verified_at"] is None


def test_a_credential_change_claims_the_database_first(client, fake_clusters, mongo, monkeypatch):
    """Secret 을 먼저 쓰는 저장은 클레임을 잡는다. 클레임이 살아 있는 동안 다른 저장은 Secret 을
    쓰기 전에 409 다. 기한이 지나 다른 저장이 지나갔으면 마지막 저장이 맞지 않고, Database 는
    확인되지 않은 채로 남는다 (P68 2절)."""
    _bootstrap(client)
    ds = _create(client, username="app").json()
    url = f"{DS}/{ds['id']}"
    puts: list[str] = []
    real_put = K8sSecretStore.put

    def put_during_other_save(self, name, data, **kw):
        puts.append(data.get("password", ""))
        # Secret 을 쓰는 동안 문서는 이 요청의 클레임을 들고 확인되지 않은 상태다
        held = mongo["data_services"].find_one({"_id": ds["id"]})
        assert held["claim"]["id"] != "someone-else" and held["verified_at"] is None
        return real_put(self, name, data, **kw)

    monkeypatch.setattr(K8sSecretStore, "put", put_during_other_save)
    claim = {"id": "someone-else", "until": data_services_service.utcnow() + timedelta(minutes=1)}
    mongo["data_services"].update_one({"_id": ds["id"]}, {"$set": {"claim": claim}})
    assert detail_code(client.patch(url, json={"password": "first"})) == (409, "revision_conflict")
    assert detail_code(client.patch(url, json={"display_name": "x"})) == (409, "revision_conflict")
    assert puts == []
    # 기한이 지난 클레임은 막지 않는다
    expired = {**claim, "until": data_services_service.utcnow() - timedelta(minutes=1)}
    mongo["data_services"].update_one({"_id": ds["id"]}, {"$set": {"claim": expired}})
    assert client.patch(url, json={"password": "first"}).status_code == 200
    stored = mongo["data_services"].find_one({"_id": ds["id"]})
    assert "claim" not in stored and stored["seq"] == 2 and stored["verified_at"] is None

    # 마지막 저장 전에 클레임이 지나고 다른 저장이 지나갔다
    mongo["data_services"].update_one(
        {"_id": ds["id"]}, {"$set": {"verified_at": data_services_service.utcnow()}}
    )

    def put_then_lose(self, name, data, **kw):
        real_put(self, name, data, **kw)
        mongo["data_services"].update_one(
            {"_id": ds["id"]},
            {"$set": {"claim.until": data_services_service.utcnow()}, "$inc": {"seq": 1}},
        )

    monkeypatch.setattr(K8sSecretStore, "put", put_then_lose)
    assert detail_code(client.patch(url, json={"password": "late"})) == (409, "revision_conflict")
    assert mongo["data_services"].find_one({"_id": ds["id"]})["verified_at"] is None


# -- verify ------------------------------------------------------------------


MODULES_8 = [
    {"name": "search", "ver": 81000, "path": "", "args": []},
    {"name": "ReJSON", "ver": 81000, "path": "", "args": []},
    {"name": "bf", "ver": 81000, "path": "", "args": []},
]
UNKNOWN_FT = ResponseError("ERR unknown command 'FT._LIST', with args beginning with: ")
UNKNOWN_JSON = ResponseError("ERR unknown command 'JSON.TYPE', with args beginning with: ")
NOPERM = NoPermissionError("NOPERM User u has no permissions to run the 'module|list' command")


def _verify(fake: FakeRedis, ds: DataService | None = None, password: str | None = None, **kw):
    ds = ds or data_service_doc(kind="redis")
    return asyncio.run(verify_redis(ds, password, client_factory=fake.factory, **kw))


def test_verify_redis_ready_with_module_list():
    """(a) PING 이 되고 MODULE LIST 에 search·ReJSON 이 있으면 ready. 클라이언트 인자는
    host·port·db·username·password 와 3초 타임아웃, decode_responses 다."""
    fake = FakeRedis(commands={"MODULE LIST": MODULES_8})
    r = _verify(fake, data_service_doc(kind="redis", db=2, username="u"), password="p")
    assert r.ok is True and r.error is None and r.detail == "h:6379/2: connected"
    assert r.extensions is None
    assert r.redis == {
        "version": "8.2.1",
        "modules": ["search", "ReJSON", "bf"],
        "checkpointer": "ready",
    }
    assert checkpointer_capability(r.redis) == "ready"
    assert fake.kwargs == {
        "host": "h",
        "port": 6379,
        "db": 2,
        "username": "u",
        "password": "p",
        "socket_connect_timeout": 3.0,
        "socket_timeout": 3.0,
        "decode_responses": True,
    }
    assert fake.calls == [("PING",), ("INFO", "server"), ("MODULE LIST",)]
    assert fake.closed
    # 무인증·db 없음이면 username·password 는 넘기지 않고 db 는 0 이다. 타임아웃은 인자로 바꾼다
    fake = FakeRedis(commands={"MODULE LIST": MODULES_8})
    r = _verify(fake, connect_timeout=1.5, socket_timeout=2.5)
    assert r.ok and r.detail == "h:6379/0: connected"
    assert "username" not in fake.kwargs and "password" not in fake.kwargs
    assert fake.kwargs["db"] == 0
    assert (fake.kwargs["socket_connect_timeout"], fake.kwargs["socket_timeout"]) == (1.5, 2.5)
    # 콜백을 거치지 않은 [key, value, ...] 쌍 목록도 읽는다
    raw = [["name", "search", "ver", 81000], ["name", "ReJSON", "ver", 81000]]
    r = _verify(FakeRedis(commands={"MODULE LIST": raw}))
    assert r.redis["modules"] == ["search", "ReJSON"] and r.redis["checkpointer"] == "ready"


def test_verify_redis_modules_missing_from_module_list():
    """(b) Redis 7 모양(MODULE LIST 가 비었거나 둘 중 하나가 없다)은 modules_missing. 판정(ok)은
    PING 하나라 그대로 참이다."""
    r = _verify(FakeRedis(info={"redis_version": "7.4.11"}, commands={"MODULE LIST": []}))
    assert r.ok is True
    assert r.redis == {"version": "7.4.11", "modules": [], "checkpointer": "modules_missing"}
    assert checkpointer_capability(r.redis) == "modules_missing"
    only_search = [{"name": "search", "ver": 81000}]
    r = _verify(FakeRedis(commands={"MODULE LIST": only_search}))
    assert r.redis["modules"] == ["search"] and r.redis["checkpointer"] == "modules_missing"


def test_verify_redis_fallback_probes_when_module_list_is_denied():
    """(c) MODULE LIST 가 NOPERM 이면 FT._LIST 와 JSON.TYPE 로 대신 본다. 예외가 없으면 ready 이고
    반환값은 비교하지 않는다(JSON.TYPE 은 키가 없어 [None]). modules 는 None 이다."""
    fake = FakeRedis(
        commands={"MODULE LIST": NOPERM, "FT._LIST": [], "JSON.TYPE __daegam_probe__": [None]}
    )
    r = _verify(fake)
    assert r.ok is True
    assert r.redis == {"version": "8.2.1", "modules": None, "checkpointer": "ready"}
    assert fake.calls == [
        ("PING",),
        ("INFO", "server"),
        ("MODULE LIST",),
        ("FT._LIST",),
        ("JSON.TYPE", "__daegam_probe__"),
    ]
    assert fake.closed
    # 관리형 Redis 가 MODULE 명령 자체를 막은 경우(NOPERM 이 아닌 ResponseError)도 폴백으로 간다
    fake = FakeRedis(commands={"FT._LIST": [], "JSON.TYPE __daegam_probe__": [None]})
    r = _verify(fake)
    assert r.ok is True and r.redis["checkpointer"] == "ready" and r.redis["modules"] is None


def test_verify_redis_fallback_unknown_command_is_modules_missing():
    """(d) 폴백 중 unknown command 가 있으면 modules_missing. 첫 명령에서 판가름나면 다음은 부르지
    않는다. FT._LIST 가 NOPERM 이어도 JSON.TYPE 이 unknown command 면 modules_missing 이다."""
    fake = FakeRedis(commands={"MODULE LIST": NOPERM, "FT._LIST": UNKNOWN_FT})
    r = _verify(fake)
    assert r.ok is True
    assert r.redis == {"version": "8.2.1", "modules": None, "checkpointer": "modules_missing"}
    assert fake.calls[-1] == ("FT._LIST",)
    fake = FakeRedis(
        commands={
            "MODULE LIST": NOPERM,
            "FT._LIST": NOPERM,
            "JSON.TYPE __daegam_probe__": UNKNOWN_JSON,
        }
    )
    assert _verify(fake).redis["checkpointer"] == "modules_missing"


def test_verify_redis_fallback_denied_is_unknown():
    """(e) 폴백까지 NOPERM 이면 unknown. ok 는 그대로 참이다."""
    fake = FakeRedis(
        commands={"MODULE LIST": NOPERM, "FT._LIST": NOPERM, "JSON.TYPE __daegam_probe__": NOPERM}
    )
    r = _verify(fake)
    assert r.ok is True
    assert r.redis == {"version": "8.2.1", "modules": None, "checkpointer": "unknown"}
    assert checkpointer_capability(r.redis) == "unknown"
    # 하나만 NOPERM 이고 하나는 되면 판단할 수 없어 unknown 이다
    fake = FakeRedis(
        commands={"MODULE LIST": NOPERM, "FT._LIST": NOPERM, "JSON.TYPE __daegam_probe__": [None]}
    )
    assert _verify(fake).redis["checkpointer"] == "unknown"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (
            AuthenticationError("AUTH <password> called without any password configured"),
            "auth_failed",
        ),
        (RedisTimeoutError("Timeout connecting to server"), "connect_failed"),
        (RedisConnectionError("Error 61 connecting to h:1. Connection refused."), "connect_failed"),
        (ResponseError("DB index is out of range"), "select_failed"),
        (ResponseError("NOAUTH Authentication required."), "ping_failed"),
        (RedisError("boom"), "io_error"),
    ],
    ids=["auth", "timeout", "connection", "select", "response", "redis_error"],
)
def test_verify_redis_failure_table(exc, code):
    """(f) PING 의 예외를 표대로 가른다. detail 은 host:port/db 와 코드만이고 서버 원문은 없다."""
    fake = FakeRedis(ping=exc, commands={"MODULE LIST": MODULES_8})
    r = _verify(fake, data_service_doc(kind="redis", db=3))
    assert r.ok is False and r.error == code
    assert r.detail == f"h:6379/3: {code}"
    assert str(exc) not in r.detail
    assert r.redis is None and checkpointer_capability(r.redis) is None
    assert fake.calls == [("PING",)] and fake.closed


def test_verify_redis_info_denied_leaves_version_null():
    """(g) INFO 만 NOPERM 이면 version 은 null 이고 모듈 판정은 그대로 간다."""
    fake = FakeRedis(info=NOPERM, commands={"MODULE LIST": MODULES_8})
    r = _verify(fake)
    assert r.ok is True
    assert r.redis == {
        "version": None,
        "modules": ["search", "ReJSON", "bf"],
        "checkpointer": "ready",
    }
    # 관리형 Redis 가 INFO 자체를 거부해도(NOPERM 이 아닌 ResponseError) PING 이 됐으니 통과이고
    # version 만 null 이다
    fake = FakeRedis(
        info=ResponseError("ERR unknown command 'info'"), commands={"MODULE LIST": MODULES_8}
    )
    r = _verify(fake)
    assert r.ok is True and r.redis["version"] is None and r.redis["checkpointer"] == "ready"
    # PING 뒤 연결이 끊기면(INFO 에서 ConnectionError) 표대로 connect_failed 다
    fake = FakeRedis(info=RedisConnectionError("Connection closed by server."))
    r = _verify(fake)
    assert r.ok is False and r.error == "connect_failed" and fake.closed


def test_verify_endpoint(client, monkeypatch):
    """redis verify 는 redis-py 로 PING 하고 응답 redis 와 문서 capabilities.checkpointer 를 남긴다.
    비밀번호는 data_service_password 가 테넌트 Secret 에서 읽어 클라이언트 인자로 준다."""
    _bootstrap(client)
    ds = _create(client).json()
    fakes: list[FakeRedis] = []

    def fake_redis(**kwargs):
        fake = FakeRedis(commands={"MODULE LIST": MODULES_8})
        fakes.append(fake)
        return fake.factory(**kwargs)

    monkeypatch.setattr(data_services_service, "AsyncRedis", fake_redis)
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["verified_at"]
    assert r.json()["detail"] == "redis.svc:6379/0: connected"
    assert r.json()["redis"] == {
        "version": "8.2.1",
        "modules": ["search", "ReJSON", "bf"],
        "checkpointer": "ready",
    }
    assert "extensions" not in r.json()  # redis 에는 없다
    assert fakes[0].kwargs["host"] == "redis.svc" and fakes[0].kwargs["port"] == 6379
    assert fakes[0].kwargs["password"] == "s3cret" and "username" not in fakes[0].kwargs
    assert "s3cret" not in r.text
    got = client.get(f"{DS}/{ds['id']}").json()
    assert got["verified_at"] is not None and got["capabilities"] == {"checkpointer": "ready"}

    # Redis 7 은 ok 이지만 checkpointer 가 modules_missing 으로 바뀐다
    def redis7(**kwargs):
        fake = FakeRedis(info={"redis_version": "7.4.11"}, commands={"MODULE LIST": []})
        fakes.append(fake)
        return fake.factory(**kwargs)

    monkeypatch.setattr(data_services_service, "AsyncRedis", redis7)
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["redis"] == {
        "version": "7.4.11",
        "modules": [],
        "checkpointer": "modules_missing",
    }
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {
        "checkpointer": "modules_missing"
    }

    def refuse(**kwargs):
        fake = FakeRedis(ping=RedisConnectionError("Error 61 connecting to redis.svc:6379."))
        fakes.append(fake)
        return fake.factory(**kwargs)

    monkeypatch.setattr(data_services_service, "AsyncRedis", refuse)
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.status_code == 200
    assert r.json() == {
        "id": ds["id"],
        "ok": False,
        "error": "connect_failed",
        "detail": "redis.svc:6379/0: connect_failed",
    }
    assert all(f.closed for f in fakes)
    # 실패해도 마지막 성공의 capabilities 는 남는다(postgres 의 pgvector 와 같다)
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {
        "checkpointer": "modules_missing"
    }


def test_verify_postgres_endpoint_records_pgvector(client, mongo, fake_pg):
    """postgres 는 psycopg 로 접속해 확장을 보고 응답 extensions 와 문서 capabilities 에 남긴다.
    비밀번호는 redis 와 같은 data_service_password 로 읽는다."""
    _bootstrap(client)
    ds = _create(
        client, "pg", kind="postgres", database="app", username="alice", password="pg-pass"
    ).json()
    assert ds["capabilities"] == {}
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["verified_at"]
    assert r.json()["extensions"] == {"vector": {"installed": "0.8.6", "available": True}}
    assert r.json()["detail"] == "redis.svc:6379: connected"
    conninfo, kwargs = fake_pg.calls[0]
    assert "password=pg-pass" in conninfo and "user=alice" in conninfo and "dbname=app" in conninfo
    assert kwargs["connect_timeout"] == 3 and kwargs["options"] == "-c statement_timeout=5000"
    assert "pg-pass" not in r.text
    got = client.get(f"{DS}/{ds['id']}").json()
    assert got["capabilities"] == {"pgvector": "installed"} and got["verified_at"]
    assert mongo["data_services"].find_one({"_id": ds["id"]})["capabilities"] == {
        "pgvector": "installed"
    }

    fake_pg.conn = FakeConn(EXT_AVAILABLE)
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.json()["extensions"] == {"vector": {"installed": None, "available": True}}
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {"pgvector": "available"}
    fake_pg.conn = FakeConn(EXT_MISSING)
    client.post(f"{DS}/{ds['id']}/verify")
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {"pgvector": "missing"}

    # 접속 오류는 코드와 host:port 만. libpq 원문(사용자명)은 응답에 없다
    fake_pg.error = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "alice"'
    )
    r = client.post(f"{DS}/{ds['id']}/verify")
    assert r.status_code == 200
    assert r.json() == {
        "id": ds["id"],
        "ok": False,
        "error": "auth_failed",
        "detail": "redis.svc:6379: auth_failed (OperationalError)",
    }
    assert "alice" not in r.json()["detail"]
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {"pgvector": "missing"}


def test_data_service_password_is_the_same_path_for_redis_and_postgres():
    """테넌트 네임스페이스 Secret ds-{id} 의 password 를 그 Database 의 클러스터 client 로
    읽는다."""
    k8s = FakeClusterClient()
    store = K8sSecretStore(k8s)
    store.put("ds-1", {"password": "pg-pass", "username": "u"}, namespace="daegam-t")
    store.put("ds-2", {"username": "u"}, namespace="daegam-t")
    cluster = SimpleNamespace(id="c")

    class Clusters:
        def __init__(self):
            self.seen = []

        async def get(self, cluster_id):
            self.seen.append(cluster_id)
            return cluster if cluster_id == "c" else None

    tenancy = SimpleNamespace(clusters=Clusters())

    async def namespace_for(tenant, c):
        assert c is cluster and tenant == "t"
        return SimpleNamespace(name="daegam-t")

    tenancy.namespace_for = namespace_for
    clusters = SimpleNamespace(client_for=lambda c: k8s)

    def password(**kw) -> str | None:
        return asyncio.run(data_service_password(data_service_doc(**kw), tenancy, clusters))

    assert password(secret_name="ds-1") == "pg-pass"
    assert password(kind="redis", secret_name="ds-1") == "pg-pass"
    # password 항목이 없으면(무인증 Redis) None, Secret 자체가 없어도 None
    assert password(kind="redis", secret_name="ds-2") is None
    assert password(kind="redis", secret_name="ds-9") is None
    assert tenancy.clusters.seen == ["c"] * 4
    with pytest.raises(TenancyError) as ei:
        password(kind="redis", cluster_id="nope")
    assert ei.value.code == "unknown_cluster" and ei.value.status == 422


# -- helpers -----------------------------------------------------------------


def test_check_same_cluster():
    a = data_service_doc("a", cluster_id="c1")
    b = data_service_doc("b", cluster_id="c1")
    c = data_service_doc("c", cluster_id="c2")
    assert check_same_cluster([], tenant="t") is None
    assert check_same_cluster([a, b], tenant="t") == "c1"
    assert check_same_cluster([a], tenant="t", cluster_id="c1") == "c1"
    try:
        check_same_cluster([a, c], tenant="t")
        raise AssertionError("expected mismatch")
    except ClusterMismatch as e:
        assert "cluster c2" in e.detail
    try:
        check_same_cluster([a], tenant="t", cluster_id="c9")
        raise AssertionError("expected mismatch")
    except ClusterMismatch:
        pass
    try:
        check_same_cluster([data_service_doc(tenant="other")], tenant="t")
        raise AssertionError("expected mismatch")
    except ClusterMismatch as e:
        assert "another tenant" in e.detail


def test_k8s_name():
    assert k8s_name("default") == "default"
    assert k8s_name("Acme.Corp_Team") == "acme-corp-team"
    assert k8s_name("--x--") == "x"
    assert k8s_name("a" * 80).endswith("a") and len(k8s_name("a" * 80)) == 63
    assert group_name_for("Team.One") == "team-one"


def test_an_unclear_secret_write_keeps_the_edit_claim(client, fake_clusters, mongo, monkeypatch):
    """Secret PUT 을 보내기 전에 실패하면 편집 클레임을 풀고 `verified_at` 을 되돌린다. PUT 이
    시작된 뒤의 실패(시한 초과)는 Secret 이 바뀌었는지 모르므로 클레임을 두어 기한이 지나게 한다
    (P68 검토)."""
    from daegam.k8s.client import ClusterTimeout

    _bootstrap(client)
    ds = _create(client, username="app").json()
    url = f"{DS}/{ds['id']}"
    verified = data_services_service.utcnow().replace(microsecond=0)
    mongo["data_services"].update_one({"_id": ds["id"]}, {"$set": {"verified_at": verified}})

    async def no_store(*_a, **_kw):
        raise data_services_service.DataServiceError(502, "cluster_unreachable", "down")

    with monkeypatch.context() as m:
        m.setattr(data_services_service, "secret_store_for", no_store)
        assert detail_code(client.patch(url, json={"password": "p2"})) == (
            502,
            "cluster_unreachable",
        )
    doc = mongo["data_services"].find_one({"_id": ds["id"]})
    assert "claim" not in doc and doc["verified_at"] == verified and doc["seq"] == 1

    def timed_out(self, name, data, **kw):
        raise ClusterTimeout("PUT secret timed out")

    monkeypatch.setattr(K8sSecretStore, "put", timed_out)
    r = client.patch(url, json={"password": "p3"})
    assert r.status_code >= 500
    doc = mongo["data_services"].find_one({"_id": ds["id"]})
    assert doc["claim"]["until"] > data_services_service.utcnow() and doc["verified_at"] is None
    assert detail_code(client.patch(url, json={"display_name": "x"})) == (409, "revision_conflict")
