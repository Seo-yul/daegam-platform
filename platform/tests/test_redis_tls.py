"""Redis 의 TLS 끝점 (P61 B2). Database 와 클러스터 기본 Redis 가 TLS 끝점과 CA 를 갖고, 플랫폼은
늘 그것으로 붙으며, MCP Server 의 바인딩은 배포 때, 세션 저장소는 ToolHive 0.52 이상에서 배포가 정한
대로, Agent 는 런타임 이미지가 `redis_tls` 를 알렸을 때 그 끝점을 쓴다. 실제 TLS Redis 에 붙는
확인은 `DAEGAM_TEST_REDIS_TLS_URL` 이 있을 때만 돈다."""

from __future__ import annotations

import asyncio
import os
import ssl
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from daegam.config.resolve import resolve_effective
from daegam.k8s.templates.agent import render_agent_deployment
from daegam.k8s.templates.common import REDIS_CA_ANNOTATION, REDIS_CA_MOUNT
from daegam.pem import ca_sha256, is_ca_pem
from daegam.secrets import K8sSecretStore
from daegam.services import agent_memory
from daegam.services.agent_memory import AgentMemories
from daegam.services.data_plane_auth import (
    KIND_AGENT,
    _Target,
    redis_tls_rev,
)
from daegam.services.data_services import (
    DataServiceSecrets,
    redis_client_kwargs,
    verify_redis,
)
from daegam.services.mcp_servers import session_options
from daegam.services.tenancy import tenant_objects_for
from daegam.store.clusters import ClusterGates, DefaultRedis, Preflight
from daegam.store.data_services import RedisTls
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import Binding, TenantSettings
from tests.helpers.agent_runtime import (
    env_map,
    minimal_agent_doc,
)
from tests.helpers.clusters import default_cluster, record_toolhive
from tests.helpers.consts import DEFAULT_NS, DS, GV
from tests.helpers.data_services import TLS_PORT, FakeRedis, post_database, tls_database
from tests.helpers.docs import cluster_doc, data_service_doc, effective_config
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env, replicated_server
from tests.helpers.tls import ca_pem, key_pem

TLS = RedisTls(port=TLS_PORT, ca_secret="ds-x-ca")


def _secret_data(fake, name: str, namespace: str = DEFAULT_NS) -> dict[str, str] | None:
    return K8sSecretStore(fake).get(name, namespace=namespace)


def _rendered_secret(fake, name: str, namespace: str = DEFAULT_NS) -> dict[str, str]:
    """플랫폼이 렌더해 적용한 Secret 의 stringData. fake 는 stringData 를 data 로 옮기지 않는다."""
    return fake.get("v1", "Secret", name, namespace)["stringData"]


# -- Database 의 TLS 끝점 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "tls", "detail"),
    [
        ("postgres", {"port": TLS_PORT, "ca": ca_pem()}, "tls is for redis"),
        ("redis", {"port": TLS_PORT, "ca": ca_pem() + key_pem()}, "without a private key"),
        ("redis", {"port": TLS_PORT}, "tls.ca is required"),
    ],
)
def test_a_tls_endpoint_needs_redis_and_a_ca_without_keys(client, fake_clusters, kind, tls, detail):
    default_cluster(client)
    r = post_database(client, "db", kind, tls=tls)
    assert detail_code(r) == (422, "invalid_value")
    assert detail in r.json()["detail"]["detail"]
    assert "PRIVATE KEY" not in r.text


def test_a_ca_pem_holds_certificates_only():
    """개인 키 같은 다른 블록이 하나라도 섞이면 CA 가 아니다. CA 는 문서·응답·테넌트 Secret 으로
    퍼지기 때문이다."""
    assert is_ca_pem(ca_pem()) == ca_pem().strip() + "\n"
    assert is_ca_pem(ca_pem() + ca_pem("second-ca")) is not None
    assert is_ca_pem(ca_pem() + key_pem()) is None
    assert is_ca_pem(key_pem()) is None
    assert is_ca_pem("") is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            ca_pem() + "-----BEGIN RSA private key-----\nAAAA\n-----END RSA private key-----",
            id="private-key-any-case",
        ),
        pytest.param(
            ca_pem().replace("BEGIN CERTIFICATE", "begin certificate"), id="lowercase-label"
        ),
        pytest.param("-" + ca_pem(), id="six-dashes"),
        pytest.param(ca_pem().replace("-----END", "----END"), id="four-dashes"),
        pytest.param(ca_pem().split("\n", 1)[1].rsplit("-----END", 1)[0], id="bare-base64-body"),
        pytest.param(ca_pem() + "trailing text", id="text-after-the-block"),
    ],
)
def test_a_malformed_or_mixed_pem_is_not_a_ca(value):
    """인증서 블록만, 머리와 꼬리의 대시 다섯과 대문자 라벨로 와야 한다. 개인 키 블록은 라벨의
    대소문자와 상관없이 거절한다."""
    assert is_ca_pem(value) is None


def test_tls_edits_keep_the_ca_until_the_database_goes(client, fake_clusters, mongo):
    """포트만 고치면 CA 는 그대로이고 확인이 지워진다. 새 CA 는 Secret 을 다시 쓴다. 끝점을
    지워도 CA Secret 은 Database 를 지울 때까지 남는다. 그 끝점으로 배포한 MCP Server 가 붙이기
    때문이다. 이 판이 모르는 TLS 칸은 포트를 고쳐도 남는다. CA 는 `ds-{id}-ca` 의 `ca.crt` 에 있고
    응답에는 PEM 대신 그 Secret 이름과 지문이 온다. 자격증명 Secret `ds-{id}` 에는 CA 가 없다."""
    default_cluster(client)
    ds = tls_database(client, db=2, username="app")
    url = f"{DS}/{ds['id']}"
    fake = fake_clusters.by_name["edge"]
    ca_secret = f"ds-{ds['id']}-ca"
    assert ds["tls"] == {"port": TLS_PORT, "ca_secret": ca_secret, "ca_sha256": ca_sha256(ca_pem())}
    assert "BEGIN CERTIFICATE" not in str(ds)
    assert _secret_data(fake, ca_secret) == {"ca.crt": ca_pem().strip() + "\n"}
    assert "ca.crt" not in _secret_data(fake, ds["secret_name"])
    mongo["data_services"].update_one(
        {"_id": ds["id"]}, {"$set": {"verified_at": "2026-10-21", "tls.later": 1}}
    )

    r = client.patch(url, json={"tls": {"port": 6390}})
    assert r.status_code == 200, r.text
    assert r.json()["tls"]["port"] == 6390 and r.json()["tls"]["ca_secret"] == ca_secret
    assert r.json()["verified_at"] is None
    assert mongo["data_services"].find_one({"_id": ds["id"]})["tls"]["later"] == 1
    assert _secret_data(fake, ca_secret)["ca.crt"].startswith("-----BEGIN CERTIFICATE-----")

    other = ca_pem("another-ca")
    assert client.patch(url, json={"tls": {"port": 6390, "ca": other}}).status_code == 200
    assert _secret_data(fake, ca_secret) == {"ca.crt": other.strip() + "\n"}

    r = client.patch(url, json={"tls": None})
    assert r.status_code == 200 and r.json()["tls"] is None
    assert _secret_data(fake, ca_secret) is not None

    assert client.delete(url).status_code == 200
    assert _secret_data(fake, ca_secret) is None
    assert _secret_data(fake, ds["secret_name"]) is None


# -- 플랫폼의 접속 ------------------------------------------------------------------------------


def test_the_platform_always_uses_the_tls_endpoint():
    """TLS 끝점이 있으면 그 포트에 TLS 로 붙고 서버 인증서와 호스트 이름을 CA 로 확인한다."""
    kwargs = redis_client_kwargs(data_service_doc(kind="redis", tls=TLS), "p", ca="PEM")
    assert kwargs["port"] == TLS_PORT
    assert kwargs["ssl"] is True and kwargs["ssl_check_hostname"] is True
    assert kwargs["ssl_cert_reqs"] == "required" and kwargs["ssl_ca_data"] == "PEM"
    plain = redis_client_kwargs(data_service_doc(kind="redis"), "p")
    assert plain["port"] == 6379 and "ssl" not in plain


def test_verify_refuses_without_a_ca_and_names_tls_failures():
    """CA 를 읽지 못하면 붙지 않는다. 인증서 확인이 실패하면 tls_failed 이고 평문으로 물러서지
    않는다."""
    ds = data_service_doc(kind="redis", tls=TLS)
    fake = FakeRedis()
    r = asyncio.run(verify_redis(ds, "p", ca=None, client_factory=fake.factory))
    assert (r.ok, r.error, r.detail) == (False, "ca_missing", f"h:{TLS_PORT}/0 (tls): ca_missing")
    assert fake.calls == []

    failure = RedisConnectionError("Error 1 connecting")
    failure.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    fake = FakeRedis(ping=failure)
    r = asyncio.run(verify_redis(ds, "p", ca="PEM", client_factory=fake.factory))
    assert (r.ok, r.error) == (False, "tls_failed")
    assert fake.kwargs["ssl_ca_data"] == "PEM"


class _ScanRedis:
    """장기 기억 지우기가 부르는 SCAN·DELETE 만 흉내 낸다. 만든 인자를 남긴다."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def factory(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def scan(self, cursor: int = 0, match: str = "*", count: int = 10):
        return 0, []

    async def delete(self, *keys: str) -> int:
        return 0

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("tls", [None, TLS])
def test_forgetting_an_agent_reaches_its_redis_store_with_or_without_tls(monkeypatch, tls):
    """삭제의 마지막 단계는 Redis 장기 기억에도 붙는다. TLS 끝점이 있으면 읽은 CA 로 붙는다."""
    ds = data_service_doc("mem", kind="redis", tls=tls)
    fake = _ScanRedis()
    monkeypatch.setattr(agent_memory, "AsyncRedis", fake.factory)
    memories = AgentMemories(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), error_cls=RuntimeError
    )

    async def store_of(_rev, _tenant):
        return ds

    async def secrets(_ds):
        return DataServiceSecrets("p", "PEM" if tls else None)

    monkeypatch.setattr(memories, "store_of", store_of)
    monkeypatch.setattr(memories, "_secrets", secrets)
    doc = minimal_agent_doc()
    asyncio.run(memories.forget(doc))
    assert fake.kwargs["port"] == (TLS_PORT if tls else 6379)
    assert fake.kwargs.get("ssl_ca_data") == ("PEM" if tls else None)


# -- MCP Server ---------------------------------------------------------------------------------


def _tls_warnings(r) -> list[str]:
    assert r.status_code in (200, 201), r.text
    return [w for w in r.json()["warnings"] if w.startswith("redis_tls_binding:")]


def test_a_running_custom_image_server_is_warned_when_its_binding_moves_at_redeploy(
    client, fake_clusters
):
    """도는 사용자 이미지 서버는 재배포나 Basic 편집에서 바인딩이 TLS 로 옮겨 간다. 앞 배포가
    평문이었던 바인딩만 경고하고, 이미 TLS 로 배포한 바인딩은 다시 경고하지 않는다."""
    _cid, bp, _ds, ms = mcp_server_env(client)
    plain = post_database(client, "later", "redis", db=2).json()
    r = client.put(
        f"/mcp-servers/{ms['id']}",
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add"],
            "bindings": [{"data_service_id": plain["id"], "env_prefix": "CACHE"}],
            "runtime": {"image": "registry.local/custom-mcp:1"},
        },
    )
    assert r.status_code == 200, r.text
    inst = clone_and_deploy(client, ms, name="calc-1").json()
    assert not [w for w in inst["warnings"] if w.startswith("redis_tls_binding:")]

    tls = {"tls": {"port": TLS_PORT, "ca": ca_pem()}}
    assert client.patch(f"{DS}/{plain['id']}", json=tls).status_code == 200
    assert _tls_warnings(client.post(f"/mcp-servers/{inst['id']}/redeploy")) != []
    assert _tls_warnings(client.post(f"/mcp-servers/{inst['id']}/redeploy")) == []

    other = post_database(client, "other", "redis", db=3, **tls).json()
    bindings = [{"data_service_id": other["id"], "env_prefix": "CACHE"}]
    r = client.patch(f"/mcp-servers/{inst['id']}", json={"bindings": bindings})
    assert len(_tls_warnings(r)) == 1


def test_the_session_storage_choice_is_pinned_at_deploy():
    """배포는 지금 게이트로 정한다. 다시 렌더는 배포의 판단을 따르고, 판이 0.52 아래로 알려졌을 때만
    평문으로 내린다. 판을 모르면 그대로 둔다. 0.51.2 에서 평문으로 배포한 서버는 다시 렌더로는 TLS
    로 옮기지 않는다."""
    old = cluster_doc()
    old.status.preflight = None
    old.recorded_toolhive_version = "0.51.2"
    new = cluster_doc()
    new.status.preflight = None
    new.recorded_toolhive_version = "0.53.0"
    unknown = cluster_doc()
    unknown.status.preflight = None
    assert session_options(new).session_tls is True
    assert session_options(old).session_tls is False
    assert session_options(new, pinned=True).session_tls is True
    assert session_options(unknown, pinned=True).session_tls is True
    assert session_options(old, pinned=True).session_tls is False
    assert session_options(new, pinned=False).session_tls is False


def test_default_redis_tls_reaches_session_storage_and_every_tenant_namespace(
    client, fake_clusters, mongo
):
    """클러스터 기본 Redis 의 CA 는 테넌트 네임스페이스마다 `daegam-redis-ca` 로 가고, 세션
    저장소는 0.52 이상에서 그 TLS 끝점으로 붙는다. PEM 인증서가 아닌 CA 는 그 칸 이름으로 422 다."""
    cid, bp, _ds, ms = mcp_server_env(client, ds=False)
    bad = {"address": "redis.shared:6379", "tls": {"port": TLS_PORT, "ca": ca_pem() + key_pem()}}
    r = client.patch(f"/clusters/{cid}", json={"default_redis": bad})
    assert detail_code(r) == (422, "invalid_value")
    assert "default_redis.tls.ca" in r.json()["detail"]["detail"]
    default = {
        "address": "redis.shared:6379",
        "password_secret": "redis-pass",
        "tls": {"port": TLS_PORT, "ca": ca_pem()},
    }
    assert client.patch(f"/clusters/{cid}", json={"default_redis": default}).status_code == 200
    record_toolhive(mongo, cid, "0.52.0")
    replicated_server(client, bp, ms)
    r = clone_and_deploy(client, ms, name="calc-1")
    assert r.status_code == 201, r.text
    fake = fake_clusters.by_name["edge"]
    storage = fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)["spec"]["sessionStorage"]
    assert storage["address"] == f"redis.shared:{TLS_PORT}"
    assert storage["tls"] == {"caCertSecretRef": {"name": "daegam-redis-ca", "key": "ca.crt"}}
    assert storage["passwordRef"] == {"name": "redis-pass", "key": "password"}
    assert _rendered_secret(fake, "daegam-redis-ca") == {"ca.crt": ca_pem().strip() + "\n"}


def test_new_snapshots_carry_only_the_tls_fields_this_release_knows():
    """클러스터 기록의 TLS 끝점에 뒤의 판이 더한 칸은 배포 스냅샷에 들지 않는다."""
    redis = DefaultRedis.model_validate(
        {"address": "r:6379", "tls": {"port": TLS_PORT, "ca": "PEM", "later": 1}, "later": 2}
    )
    cluster = cluster_doc(default_redis=redis)
    effective = resolve_effective(PlatformSettings(), cluster, TenantSettings(tenant="t"))
    assert effective.default_redis is not None
    assert effective.default_redis.model_dump() == {
        "address": "r:6379",
        "password_secret": None,
        "tls": {"port": TLS_PORT, "ca": "PEM"},
        "plain_closed": False,
    }
    assert effective.default_redis.tls_address() == f"r:{TLS_PORT}"


def test_tenant_objects_carry_the_default_redis_ca_only_where_tenant_secrets_are_written():
    """CA 가 객체 판에 들므로 바뀌면 스윕이 네임스페이스마다 다시 쓴다. TLS 끝점이 없거나,
    테넌트 객체를 두지 않는 클러스터이거나, 테넌트 Secret 을 쓸 수 없다고 확인된 클러스터에는 CA 가
    없다."""
    subject = {"kind": "ServiceAccount", "name": "daegam-platform", "namespace": "daegam-system"}
    rights = ClusterGates(tenant_object_rights=True)
    secure_redis = DefaultRedis.model_validate(
        {"address": "r:6379", "tls": {"port": TLS_PORT, "ca": "PEM"}}
    )
    plain = cluster_doc(default_redis=DefaultRedis(address="r:6379"), gates=rights)
    objects = tenant_objects_for(plain, "t", "daegam-t", subject)
    assert objects is not None
    assert [o["kind"] for o in objects] == ["RoleBinding", "ServiceAccount"]

    secure = cluster_doc(default_redis=secure_redis, gates=rights)
    objects = tenant_objects_for(secure, "t", "daegam-t", subject)
    assert objects is not None and objects[-1]["metadata"]["name"] == "daegam-redis-ca"
    assert objects[-1]["stringData"] == {"ca.crt": "PEM"}

    no_rights = cluster_doc(default_redis=secure_redis)
    assert tenant_objects_for(no_rights, "t", "daegam-t", subject) == []
    refused = cluster_doc(default_redis=secure_redis, gates=rights)
    refused.status.preflight = Preflight.model_validate(
        {"ok": True, "secret_rights": {"cluster_wide": False, "writable": False}}
    )
    objects = tenant_objects_for(refused, "t", "daegam-t", subject)
    assert objects is not None and "daegam-redis-ca" not in [o["metadata"]["name"] for o in objects]


# -- Agent --------------------------------------------------------------------------------------


def _agent_deployment(redis_tls: bool) -> dict:
    ds = data_service_doc(
        "mem",
        kind="redis",
        id="ds-r",
        tenant="acme",
        cluster_id="c1",
        host="redis.svc",
        db=1,
        username="app",
        secret_name="ds-ds-r",
        has_password=True,
        tls=RedisTls(port=TLS_PORT, ca_secret="ds-ds-r-ca"),
    )
    effective = effective_config(
        bindings=[Binding(data_service_id="ds-r", env_prefix="DAEGAM_DB_0")]
    )
    return render_agent_deployment(
        minimal_agent_doc(),
        effective,
        providers=[],
        data_services={"ds-r": ds},
        agent_oauth=None,
        redis_tls=redis_tls,
        client_id_claim="cid",
        groups_claim=None,
    )


def test_an_agent_uses_the_tls_endpoint_only_when_its_image_reports_redis_tls():
    """옛 이미지의 Agent 는 평문 포트를 계속 쓴다. `redis_tls` 를 알린 이미지는 TLS 끝점과 CA
    파일을 받고, URL 은 `rediss://` 다."""
    old = _agent_deployment(redis_tls=False)
    env = env_map(old["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_DB_0_PORT"] == "6379" and "DAEGAM_DB_0_TLS" not in env
    assert env["DAEGAM_DB_0_URL"].startswith("redis://app:$(DAEGAM_DB_0_PASSWORD)@redis.svc:6379")
    assert not [v for v in old["spec"]["template"]["spec"]["volumes"] if "secret" in v]

    new = _agent_deployment(redis_tls=True)
    container = new["spec"]["template"]["spec"]["containers"][0]
    env = env_map(container)
    assert env["DAEGAM_DB_0_PORT"] == str(TLS_PORT)
    assert env["DAEGAM_DB_0_TLS"] == "1"
    assert env["DAEGAM_DB_0_CA_FILE"] == f"{REDIS_CA_MOUNT}/DAEGAM_DB_0/ca.crt"
    assert env["DAEGAM_DB_0_URL"] == f"rediss://app:$(DAEGAM_DB_0_PASSWORD)@redis.svc:{TLS_PORT}/1"
    volume = next(v for v in new["spec"]["template"]["spec"]["volumes"] if "secret" in v)
    assert volume["secret"]["secretName"] == "ds-ds-r-ca"
    assert any(m["name"] == volume["name"] for m in container["volumeMounts"])
    # 파드는 그 CA 의 지문을 단다. 지문을 적기 전에 저장한 CA 는 unknown 이다. 평문 파드에는 없다
    assert new["spec"]["template"]["metadata"]["annotations"][REDIS_CA_ANNOTATION] == "ds-r=unknown"
    assert REDIS_CA_ANNOTATION not in old["spec"]["template"]["metadata"]["annotations"]


def _paced_target(doc_rev: str | None, want_base: str | None, *, want: str | None) -> _Target:
    """Agent 하나의 재적용 대상. want 는 지금 CA 표시이고 마지막 렌더의 표시는 "ca-1" 이다. paced 는
    `AgentService.paced_revs` 와 같은 꼴이다."""
    doc = SimpleNamespace(auth_rev=doc_rev, render_epoch=1, audience_switch=None)
    service = SimpleNamespace(auth_kind=KIND_AGENT)
    paced = frozenset({want_base, redis_tls_rev(want_base, "ca-1")})
    return _Target(service, doc, redis_tls_rev(want_base, want), 1, paced)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("doc_rev", "want_base", "want", "urgent"),
    [
        pytest.param("base", "base", "ca-1", False, id="tls-on"),
        pytest.param(redis_tls_rev("base", "ca-1"), "base", None, False, id="tls-off"),
        pytest.param(redis_tls_rev("base", "ca-1"), "base", "ca-2", False, id="ca-change"),
        pytest.param(None, None, "ca-1", False, id="non-consumer-gains-the-mark"),
        pytest.param("old", "new", None, True, id="auth-change"),
        pytest.param("old", "new", "ca-1", True, id="auth-change-and-tls-on"),
        pytest.param(redis_tls_rev("old", "ca-1"), "new", None, True, id="auth-change-and-tls-off"),
        pytest.param(redis_tls_rev("old", "ca-1"), "new", "ca-2", True, id="auth-and-ca-change"),
        pytest.param(None, "base", None, True, id="becomes-a-consumer"),
        pytest.param(
            redis_tls_rev(None, "ca-1"), "base", "ca-1", True, id="becomes-a-consumer-on-tls"
        ),
    ],
)
def test_only_mark_differences_go_at_the_namespace_pace(doc_rev, want_base, want, urgent):
    """표시만 다른 판(TLS 를 켜고 끄기, CA 가 바뀌기, 인증 입력이 없는 Agent 가 표시를 얻기)은
    회수가 아니라서 네임스페이스마다의 속도로 간다. 인증 입력이 바뀌었으면 표시가 함께 바뀌어도
    급하다."""
    assert _paced_target(doc_rev, want_base, want=want).urgent is urgent


def test_the_redis_tls_mark_changes_the_expected_revision_even_without_auth_inputs():
    """인증 소비자가 아니라 base 가 None 인 Agent 도 표시를 얹는다. CA 표시가 다르면 판이 다르다.
    표시를 모르는 이전 릴리스는 판이 달라 되돌려 렌더한다."""
    assert redis_tls_rev("abc", None) == "abc"
    assert redis_tls_rev(None, None) is None
    assert redis_tls_rev(None, "ca-1") is not None
    marked = redis_tls_rev("abc", "ca-1")
    assert marked not in (None, "abc") and marked == redis_tls_rev("abc", "ca-1")
    assert redis_tls_rev("abc", "ca-2") != marked


# -- 실제 TLS Redis ----------------------------------------------------------------------------

TLS_URL = os.environ.get("DAEGAM_TEST_REDIS_TLS_URL")
TLS_CA = os.environ.get("DAEGAM_TEST_REDIS_TLS_CA")


@pytest.mark.skipif(not (TLS_URL and TLS_CA), reason="DAEGAM_TEST_REDIS_TLS_URL·_CA 가 없다")
def test_verify_against_a_tls_redis():
    """TLS 끝점으로 PING 과 모듈 확인이 된다. 다른 CA 로는 tls_failed 다."""
    assert TLS_URL and TLS_CA
    parts = urlsplit(TLS_URL)
    ds = data_service_doc(
        kind="redis",
        host=parts.hostname,
        port=1,
        tls=RedisTls(port=parts.port or TLS_PORT, ca_secret="ds-x-ca"),
    )
    ca = Path(TLS_CA).read_text()
    ok = asyncio.run(verify_redis(ds, parts.password, ca=ca))
    assert ok.ok, ok.detail
    assert ok.redis is not None and ok.redis["checkpointer"] == "ready"
    wrong = asyncio.run(verify_redis(ds, parts.password, ca=ca_pem("wrong-ca")))
    assert (wrong.ok, wrong.error) == (False, "tls_failed")
