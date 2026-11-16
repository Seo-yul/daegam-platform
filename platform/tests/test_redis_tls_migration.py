"""함께 올린 Redis 의 TLS 끝점 받기와 평문 소비자 세기 (P61 5절 3·4단계).

`DAEGAM_HOME_REDIS_TLS` 가 있으면 기동 때 그 Redis 를 가리키는 home 의 기록이 TLS 끝점을 한 번
얻고, 상태 API 와 Database 의 `plain-consumers` 는 TLS 끝점이 있는 Redis 에 아직 평문 포트로 붙는
소비자를 렌더가 한 것으로 센다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json

import pytest

from daegam import shipped_redis_check
from daegam.config import ShippedRedisTls, _home_redis_tls_from_env
from daegam.k8s.preflight import SHIPPED_REDIS_CERT_WARNING, cluster_warnings
from daegam.pem import ca_sha256
from daegam.secrets import K8sSecretStore
from daegam.services import data_services as data_services_module
from daegam.services.agent_features import FEATURES_HEADER, IMAGE_HEADER, REDIS_TLS
from daegam.services.redis_tls_consumers import (
    plain_close_blockers,
    plain_redis_consumers,
    shipped_at,
)
from daegam.services.shipped_redis import adopt_shipped_redis_tls, mark_shipped_plain
from daegam.services.shipped_redis_ca import read_shipped_ca
from daegam.store.clusters import DefaultRedis, Preflight
from tests.helpers.agent_runtime import deployed_agent, skip_start_check
from tests.helpers.agents import MINIMAL_SPEC
from tests.helpers.clusters import default_cluster, record_toolhive
from tests.helpers.consts import AG, DEFAULT_NS, DS, IMAGE
from tests.helpers.data_services import TLS_PORT, FakeRedis, post_database, tls_database
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env, replicated_server
from tests.helpers.tls import ca_pem, cert_bytes, new_ca, new_server
from tests.helpers.workers import run_data_plane_auth

PLATFORM_NS = "daegam-system"
CA_CONFIGMAP = "daegam-redis-ca"
SHIPPED = ShippedRedisTls(("redis.svc",), 6379, TLS_PORT, CA_CONFIGMAP)


def _plain(client, ds_id: str) -> list[dict]:
    r = client.get(f"{DS}/{ds_id}/plain-consumers")
    assert r.status_code == 200, r.text
    return r.json()["consumers"]


def _status(client) -> dict:
    r = client.get("/settings/data-plane-auth")
    assert r.status_code == 200, r.text
    return r.json()


# -- 평문 소비자 -------------------------------------------------------------------------------


def test_an_agent_counts_until_a_render_puts_its_binding_on_tls(client, fake_clusters, mongo):
    """TLS 끝점을 가진 Redis 를 바인딩한 Agent 는 렌더가 그것을 TLS 로 붙이기 전까지 평문
    소비자다. 이미지가 `redis_tls` 를 모르면 runtime_image, 알지만 아직 다시 렌더하지 않았으면
    binding 이고, 재적용이 다시 렌더하면 빠진다. TLS 끝점이 없는 Database 는 늘 비어 있다."""
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=lambda _gw, _ds: MINIMAL_SPEC, runtime=False
    )
    skip_start_check(client.app)
    ds = tls_database(client, "mem", db=2)
    plain_ds = post_database(client, "plain-mem", "redis").json()
    bindings = [
        {"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"},
        {"data_service_id": plain_ds["id"], "env_prefix": "DAEGAM_DB_1"},
    ]
    mongo["agents"].update_one(
        {"_id": agent.doc["id"]},
        {"$set": {"bindings": bindings, "effective_config.bindings": bindings}},
    )

    def row(reason: str) -> dict:
        return {
            "kind": "agent",
            "id": agent.doc["id"],
            "name": agent.doc["name"],
            "data_service_id": ds["id"],
            "reason": reason,
        }

    assert _plain(client, ds["id"]) == [row("runtime_image")]
    assert _plain(client, plain_ds["id"]) == []
    assert _status(client)["redis_plain_consumers"] == {"default": [row("runtime_image")]}

    headers = {**agent.auth, IMAGE_HEADER: IMAGE, FEATURES_HEADER: REDIS_TLS}
    assert client.get(f"{AG}/{agent.doc['id']}/agent.json", headers=headers).status_code == 200
    assert _plain(client, ds["id"]) == [row("binding")]
    run_data_plane_auth(client)
    assert mongo["agents"].find_one({"_id": agent.doc["id"]})["redis_tls_bindings"] == [ds["id"]]
    assert _plain(client, ds["id"]) == []
    assert _status(client)["redis_plain_consumers"] == {}


def test_mcp_servers_deployed_before_the_tls_endpoint_are_plain_consumers(
    client, fake_clusters, mongo
):
    """Database 가 TLS 끝점을 얻기 전에 배포한 서버는 바인딩과 세션 저장소가 평문이다(binding).
    0.52 아래에서 TLS 끝점과 함께 배포한 서버는 바인딩은 TLS 이고 세션 저장소만 평문이다
    (session_storage). 0.52 이상에서 배포한 서버는 빠진다."""
    cid, bp, _ds, ms = mcp_server_env(client)
    ds = post_database(client, "secure", "redis", db=2).json()
    replicated_server(
        client, bp, ms, bindings=[{"data_service_id": ds["id"], "env_prefix": "CACHE"}]
    )
    record_toolhive(mongo, cid, "0.51.2")
    before = clone_and_deploy(client, ms, name="calc-1").json()
    r = client.patch(f"{DS}/{ds['id']}", json={"tls": {"port": TLS_PORT, "ca": ca_pem()}})
    assert r.status_code == 200, r.text
    on_old = clone_and_deploy(client, ms, name="calc-2").json()
    record_toolhive(mongo, cid, "0.52.0")
    clone_and_deploy(client, ms, name="calc-3")

    rows = {(c["id"], c["reason"]) for c in _plain(client, ds["id"])}
    assert rows == {
        (before["id"], "binding"),
        (on_old["id"], "session_storage"),
    }


def test_a_session_storage_on_the_same_default_redis_counts_once_it_gains_tls(
    client, fake_clusters, mongo
):
    """세션 저장소가 클러스터 기본 Redis 인 서버는, 같은 주소의 기본 Redis 가 TLS 끝점을 얻으면
    data_service_id 없는 줄로 상태 API 에 나온다. 기본 Redis 가 다른 곳으로 바뀌었으면 세지
    않는다."""
    cid, bp, _ds, ms = mcp_server_env(
        client, cluster_extra={"default_redis": {"address": "redis.svc:6379"}}
    )
    replicated_server(client, bp, ms)
    inst = clone_and_deploy(client, ms, name="calc-1").json()
    assert _status(client)["redis_plain_consumers"] == {}
    tls = {"port": TLS_PORT, "ca": ca_pem()}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"default_redis.tls": tls}})
    assert _status(client)["redis_plain_consumers"] == {
        "default": [
            {
                "kind": "mcp_server",
                "id": inst["id"],
                "name": inst["name"],
                "data_service_id": None,
                "reason": "session_storage",
            }
        ]
    }
    moved = {"address": "other.svc:6379", "tls": tls}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"default_redis": moved}})
    assert _status(client)["redis_plain_consumers"] == {}


def _consumers(client, shipped_home: str | None = None) -> set[tuple[str | None, str]]:
    """모든 테넌트의 평문 소비자 (data_service_id, reason). shipped_home 을 주면 그 클러스터가
    `SHIPPED` 를 함께 올린 home 이다."""
    st = client.app.state
    home = client.portal.call(st.clusters.get, shipped_home) if shipped_home else None
    rows = client.portal.call(
        lambda: plain_redis_consumers(
            st.agents,
            st.mcp_servers,
            st.data_services,
            st.clusters,
            shipped=shipped_at(SHIPPED, home),
        )
    )
    return {(r.data_service_id, r.reason) for r in rows}


def test_default_redis_session_storage_counts_by_address_and_without_a_snapshot(
    client, fake_clusters, mongo
):
    """세션 저장소의 기본 Redis 는 주소를 대소문자와 끝의 점 없이 견준다. 관리자가 기본 Redis 를
    다른 곳으로 바꿔도 스냅샷 주소가 함께 올린 Redis 이면 센다. 배포 스냅샷이 없는 서버는 TLS
    끝점이 있는 기본 Redis 를 쓸 수 있으면 unknown 이다."""
    cid, bp, _ds, ms = mcp_server_env(
        client, cluster_extra={"default_redis": {"address": "redis.svc:6379"}}
    )
    replicated_server(client, bp, ms)
    inst = clone_and_deploy(client, ms, name="calc-1").json()
    tls = {"port": TLS_PORT, "ca": ca_pem()}
    aliased = {"address": "Redis.SVC.:6379", "tls": tls}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"default_redis": aliased}})
    assert _consumers(client) == {(None, "session_storage")}

    moved = {"address": "other.svc:6379", "tls": tls}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"default_redis": moved}})
    assert _consumers(client) == set()
    assert _consumers(client, shipped_home=cid) == {(None, "session_storage")}

    mongo["mcp_servers"].update_one({"_id": inst["id"]}, {"$set": {"deployed_inputs": None}})
    assert _consumers(client, shipped_home=cid) == {(None, "unknown")}


# -- 함께 올린 Redis 의 TLS 끝점 받기 -------------------------------------------------------------


def _publish(fake, ca: str, cert: str | None = None) -> None:
    """함께 올린 Redis 를 두는 배포가 쓰는 ConfigMap 의 자리."""
    data = {"ca.crt": ca, **({"tls.crt": cert} if cert else {})}
    fake.objects.pop(("v1", "ConfigMap", PLATFORM_NS, CA_CONFIGMAP), None)
    fake.create(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": CA_CONFIGMAP, "namespace": PLATFORM_NS},
            "data": data,
        }
    )


def _with_plain(settings, plain: bool):
    """함께 올린 Redis 의 평문 포트가 plain 인 설정."""
    return dataclasses.replace(settings, home_redis_tls=dataclasses.replace(SHIPPED, plain=plain))


def _blockers(client, home_id: str) -> list[str]:
    """그 클러스터를 home 으로 본 닫기 전 확인."""
    st = client.app.state
    return client.portal.call(
        plain_close_blockers,
        SHIPPED,
        client.portal.call(st.clusters.get, home_id),
        st.clusters,
        st.data_services,
        st.agents,
        st.mcp_servers,
    )


@pytest.fixture
def shipped(client, fake_clusters, monkeypatch):
    """edge 를 home 으로 보고 그 플랫폼 네임스페이스에 ConfigMap 을 둔다. verify 는 가짜
    Redis 다."""
    default_cluster(client)
    app = client.app
    service = app.state.cluster_service
    fake = fake_clusters.by_name["edge"]
    _publish(fake, ca_pem())
    edge = client.portal.call(app.state.clusters.get_by_name, "edge")

    async def home():
        return await app.state.clusters.get(edge.id)

    monkeypatch.setattr(service, "home", home)
    monkeypatch.setattr(service, "home_client", lambda: fake)
    redis = FakeRedis(commands={"MODULE LIST": []})
    monkeypatch.setattr(data_services_module, "AsyncRedis", redis.factory)
    settings = dataclasses.replace(service.settings, namespace=PLATFORM_NS, home_redis_tls=SHIPPED)

    def adopt() -> int:
        return client.portal.call(
            adopt_shipped_redis_tls, settings, service, app.state.data_service_service
        )

    def mark(plain: bool) -> int:
        with_plain = _with_plain(settings, plain)
        return client.portal.call(
            mark_shipped_plain, with_plain, service, app.state.data_service_service
        )

    def close() -> None:
        """플랫폼의 설정을 평문 포트가 닫힌 것으로 바꾼다. 저장이 그 자리에서 표시를 정한다."""
        monkeypatch.setattr(service, "settings", _with_plain(settings, False))

    return {
        "adopt": adopt,
        "mark": mark,
        "close": close,
        "blockers": lambda: _blockers(client, edge.id),
        "fake": fake,
        "secrets": K8sSecretStore(fake),
        "edge": edge,
        "redis": redis,
    }


def _ca_of(shipped, ds_id: str) -> str | None:
    data = shipped["secrets"].get(f"ds-{ds_id}-ca", namespace=DEFAULT_NS) or {}
    return data.get("ca.crt")


def test_an_admins_later_edits_are_kept(client, mongo, shipped):
    """관리자가 지운 TLS 끝점은 다시 더하지 않는다. 관리자가 바꾼 CA 는 함께 올린 CA 가 바뀌어도
    그대로다. 관리자가 미리 둔 끝점에는 처음부터 손대지 않는다."""
    removed = post_database(client, "removed", "redis").json()
    own = post_database(client, "own", "redis", port=6379).json()
    preset = tls_database(client, "preset", db=2)
    mongo["data_services"].update_one({"_id": preset["id"]}, {"$set": {"host": "redis.svc"}})
    assert shipped["adopt"]() == 2

    r = client.patch(f"{DS}/{removed['id']}", json={"tls": None})
    assert r.status_code == 200, r.text
    admin_ca = ca_pem("daegam-admin-ca")
    r = client.patch(f"{DS}/{own['id']}", json={"tls": {"port": TLS_PORT, "ca": admin_ca}})
    assert r.status_code == 200, r.text
    _publish(shipped["fake"], ca_pem("daegam-renewed-ca"))
    assert shipped["adopt"]() == 0
    assert mongo["data_services"].find_one({"_id": removed["id"]}).get("tls") is None
    assert _ca_of(shipped, own["id"]) == admin_ca.strip() + "\n"
    assert _ca_of(shipped, preset["id"]) == ca_pem().strip() + "\n"


def test_a_renewed_ca_reaches_the_records_that_hold_the_shipped_one(client, mongo, shipped):
    """그 Redis 의 호스트와 평문 포트를 가리키는 Database 만 TLS 끝점과 CA Secret 과 받은 기록을
    얻고 플랫폼이 그 끝점으로 확인한다. 다른 호스트는 그대로다. home 의 기본 Redis 가 그 Redis 를
    가리키면 그것도 TLS 끝점과 받은 기록을 얻는다. 함께 올린 CA 가 바뀌면 그때 받은 CA 를 그대로
    가진 기록은 새 CA 를 얻는다."""
    ds = post_database(client, "team", "redis").json()
    other = post_database(client, "elsewhere", "redis", host="other.svc").json()
    edge = shipped["edge"]
    mongo["clusters"].update_one(
        {"_id": edge.id}, {"$set": {"default_redis": {"address": "redis.svc:6379"}}}
    )
    assert shipped["adopt"]() == 2
    doc = mongo["data_services"].find_one({"_id": ds["id"]})
    fingerprint = ca_sha256(ca_pem())
    assert doc["tls"] == {
        "port": TLS_PORT,
        "ca_secret": f"ds-{ds['id']}-ca",
        "ca_sha256": fingerprint,
    }
    assert doc["shipped_tls"]["ca_sha256"] == fingerprint and doc["verified_at"] is not None
    assert shipped["redis"].kwargs["port"] == TLS_PORT and shipped["redis"].kwargs["ssl"]
    assert mongo["data_services"].find_one({"_id": other["id"]}).get("tls") is None
    stored = mongo["clusters"].find_one({"_id": edge.id})
    assert DefaultRedis.model_validate(stored["default_redis"]).tls_address() == (
        f"redis.svc:{TLS_PORT}"
    )
    assert stored["shipped_redis_tls"]["ca_sha256"]

    renewed = ca_pem("daegam-renewed-ca")
    _publish(shipped["fake"], renewed)
    assert shipped["adopt"]() == 2
    assert _ca_of(shipped, ds["id"]) == renewed.strip() + "\n"
    tls = mongo["clusters"].find_one({"_id": edge.id})["default_redis"]["tls"]
    assert tls["ca"] == renewed.strip() + "\n"


def test_a_failing_record_does_not_stop_the_others_or_the_start(
    client, mongo, shipped, fake_clusters
):
    """테넌트 네임스페이스가 사라져 한 Database 의 CA 를 쓰지 못해도 예외가 나가지 않고, 기본
    Redis 는 그대로 받는다. 그 Database 의 편집 클레임은 풀린다."""
    ds = post_database(client, "team", "redis").json()
    edge = shipped["edge"]
    mongo["clusters"].update_one(
        {"_id": edge.id}, {"$set": {"default_redis": {"address": "redis.svc:6379"}}}
    )
    shipped["fake"].drop_namespace(DEFAULT_NS)
    assert shipped["adopt"]() == 1
    doc = mongo["data_services"].find_one({"_id": ds["id"]})
    assert doc.get("tls") is None and doc.get("claim") is None


def test_no_configmap_adopts_nothing(client, mongo, shipped):
    post_database(client, "team", "redis")
    shipped["fake"].objects.pop(("v1", "ConfigMap", PLATFORM_NS, CA_CONFIGMAP), None)
    assert shipped["adopt"]() == 0


# -- 인증서 만료 ---------------------------------------------------------------------------------


def test_the_server_certificate_expiry_is_read_and_warned(shipped):
    """ConfigMap 의 tls.crt 가 끝나는 때를 읽는다. 30일 안이면 Cluster 경고가 붙는다."""
    ca_key, ca = new_ca("daegam-test-ca-exp", days=40)
    _key, cert = new_server(ca_key, ca, ["redis.svc"], days=10)
    _publish(shipped["fake"], ca_pem(), cert_bytes(cert).decode())
    found = shipped_call(shipped)
    assert found is not None
    assert found.expires_at == cert.not_valid_after_utc.replace(tzinfo=None)
    soon = Preflight(ok=True, shipped_redis_cert_expires_at=found.expires_at)
    assert SHIPPED_REDIS_CERT_WARNING in cluster_warnings(soon)
    # 공개 설치에는 없는 배포 명령, 스위치와 문서를 말하지 않는다 (P85 18절)
    for private in ("make ", "=1", "docs/"):
        assert private not in SHIPPED_REDIS_CERT_WARNING
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=200)
    assert SHIPPED_REDIS_CERT_WARNING not in cluster_warnings(
        Preflight(ok=True, shipped_redis_cert_expires_at=later)
    )


def shipped_call(shipped):
    return asyncio.run(read_shipped_ca(shipped["fake"], PLATFORM_NS, SHIPPED))


# -- 설정 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"hosts": [], "port": 6379, "tls_port": 6380, "ca_configmap": "c"}',
        '{"hosts": ["r"], "port": 6379, "tls_port": 6379, "ca_configmap": "c"}',
        '{"hosts": ["r"], "port": "x", "tls_port": 6380, "ca_configmap": "c"}',
        '{"hosts": ["r"]}',
        '{"hosts": ["r"], "port": 6379, "tls_port": 6380, "ca_configmap": "c"}',
        '{"hosts": ["r"], "port": 6379, "tls_port": 6380, "ca_configmap": "c", "plain": "no"}',
    ],
)
def test_a_wrong_shipped_redis_setting_stops_the_start(monkeypatch, raw):
    monkeypatch.setenv("DAEGAM_HOME_REDIS_TLS", raw)
    with pytest.raises(ValueError):
        _home_redis_tls_from_env()


def test_the_shipped_redis_setting_reads_its_names_in_lower_case(monkeypatch):
    monkeypatch.setenv(
        "DAEGAM_HOME_REDIS_TLS",
        '{"hosts": ["Redis.Svc", " "], "port": 6379, "tls_port": 6380, "ca_configmap": "c", '
        '"plain": true}',
    )
    assert _home_redis_tls_from_env() == ShippedRedisTls(("redis.svc",), 6379, 6380, "c")
    raw = '{"hosts": ["r"], "port": 6379, "tls_port": 6380, "ca_configmap": "c", "plain": false}'
    monkeypatch.setenv("DAEGAM_HOME_REDIS_TLS", raw)
    assert _home_redis_tls_from_env().plain is False


# -- 평문 포트 닫기 (5절 5단계) -----------------------------------------------------------------


def test_closing_the_plain_port_marks_the_shipped_records_and_reopening_clears_them(
    client, mongo, shipped
):
    """닫힌 설정으로 뜬 기동은 그 Redis 를 가리키고 TLS 끝점을 가진 Database 와 기본 Redis 에 닫힌
    표시를 적는다. `seq` 는 오르지 않는다. TLS 끝점이 없는 기록은 그대로고, 다른 주소에 남은 표시는
    지운다. 다시 열면 어느 주소의 표시도 지운다."""
    ours = post_database(client, "team", "redis").json()
    other = tls_database(client, "elsewhere", db=2)
    mongo["data_services"].update_one({"_id": other["id"]}, {"$set": {"host": "other.svc"}})
    assert shipped["adopt"]() == 1
    bare = post_database(client, "bare", "redis").json()
    edge = shipped["edge"].id
    default = {"address": "redis.svc:6379", "tls": {"port": TLS_PORT, "ca": ca_pem()}}
    assert client.patch(f"/clusters/{edge}", json={"default_redis": default}).status_code == 200
    seq = mongo["data_services"].find_one({"_id": ours["id"]})["seq"]
    mongo["data_services"].update_one({"_id": other["id"]}, {"$set": {"plain_closed": True}})

    assert shipped["mark"](False) == 3
    ds = mongo["data_services"].find_one({"_id": ours["id"]})
    assert ds["plain_closed"] is True and ds["seq"] == seq
    assert mongo["data_services"].find_one({"_id": other["id"]})["plain_closed"] is False
    assert mongo["data_services"].find_one({"_id": bare["id"]})["plain_closed"] is False
    assert mongo["clusters"].find_one({"_id": edge})["default_redis"]["plain_closed"] is True
    assert shipped["mark"](False) == 0

    assert shipped["mark"](True) == 2
    assert mongo["data_services"].find_one({"_id": ours["id"]})["plain_closed"] is False
    assert mongo["clusters"].find_one({"_id": edge})["default_redis"]["plain_closed"] is False


def test_saves_on_a_closed_shipped_redis_set_the_mark_at_once(client, mongo, shipped):
    """평문 포트를 닫은 플랫폼에서는 만들기·고치기·클러스터 저장이 그 자리에서 표시를 정한다. 그
    주소를 가리키는데 TLS 끝점이 없으면 422 다. 주소는 대소문자와 끝의 점을 가리지 않는다."""
    shipped["close"]()
    r = post_database(client, "bare", "redis")
    assert detail_code(r) == (422, "invalid_value")
    ds = tls_database(client, "team", host="Redis.SVC.")
    assert ds["plain_closed"] is True
    r = client.patch(f"{DS}/{ds['id']}", json={"tls": None})
    assert detail_code(r) == (422, "invalid_value")
    moved = client.patch(f"{DS}/{ds['id']}", json={"port": 7000}).json()
    assert moved["plain_closed"] is False
    assert mongo["data_services"].find_one({"_id": ds["id"]})["plain_closed"] is False
    back = client.patch(f"{DS}/{ds['id']}", json={"port": 6379}).json()
    assert back["plain_closed"] is True
    # 다른 칸의 저장은 표시를 지킨다
    assert client.patch(f"{DS}/{ds['id']}", json={"display_name": "Team"}).status_code == 200
    assert mongo["data_services"].find_one({"_id": ds["id"]})["plain_closed"] is True

    edge = shipped["edge"].id
    r = client.patch(f"/clusters/{edge}", json={"default_redis": {"address": "redis.svc:6379"}})
    assert detail_code(r) == (422, "invalid_value")
    default = {"address": "redis.svc:6379", "tls": {"port": TLS_PORT, "ca": ca_pem()}}
    r = client.patch(f"/clusters/{edge}", json={"default_redis": default})
    assert r.status_code == 200 and r.json()["default_redis"]["plain_closed"] is True


def test_the_close_check_names_what_would_break(client, fake_clusters, mongo, shipped):
    """닫기 전 확인은 그 Redis 를 가리키는데 TLS 끝점이 없는 Database 와 기본 Redis, 아직 평문으로
    붙는 소비자를 든다. 다른 Redis 의 소비자는 들지 않는다. 막는 것이 없으면 빈 목록이다."""
    bare = post_database(client, "bare", "redis").json()
    edge = shipped["edge"].id
    plain_default = {"address": "redis.svc:6379"}
    assert (
        client.patch(f"/clusters/{edge}", json={"default_redis": plain_default}).status_code == 200
    )
    assert shipped["blockers"]() == [
        "database default/bare has no TLS endpoint",
        "cluster edge default_redis has no TLS endpoint",
    ]
    client.delete(f"{DS}/{bare['id']}")
    default = {**plain_default, "tls": {"port": TLS_PORT, "ca": ca_pem()}}
    assert client.patch(f"/clusters/{edge}", json={"default_redis": default}).status_code == 200
    assert shipped["blockers"]() == []


def test_the_close_check_counts_plain_consumers_of_the_shipped_redis_only(
    client, fake_clusters, mongo
):
    """그 Redis 를 바인딩한 Agent 가 아직 평문으로 붙으면 막는다. 다른 Redis 의 소비자는 들지
    않는다."""
    agent = deployed_agent(
        client, fake_clusters, mongo, spec=lambda _gw, _ds: MINIMAL_SPEC, runtime=False
    )
    ours = tls_database(client, "team", db=2)
    mongo["data_services"].update_one({"_id": ours["id"]}, {"$set": {"port": 6379}})
    elsewhere = tls_database(client, "elsewhere", db=2)
    mongo["data_services"].update_one({"_id": elsewhere["id"]}, {"$set": {"host": "other.svc"}})
    bindings = [
        {"data_service_id": ours["id"], "env_prefix": "DAEGAM_DB_0"},
        {"data_service_id": elsewhere["id"], "env_prefix": "DAEGAM_DB_1"},
    ]
    mongo["agents"].update_one(
        {"_id": agent.doc["id"]}, {"$set": {"effective_config.bindings": bindings}}
    )
    assert _blockers(client, agent.cluster_id) == [
        f"agent default/{agent.doc['name']} still connects to the plain port (runtime_image)"
    ]


def test_the_close_check_command_prints_one_json_line(client, monkeypatch, capsys):
    """`python -m daegam.shipped_redis_check` 는 그 파드의 설정과 Mongo 로 확인해 JSON 한 줄을 찍고
    막는 것이 있으면 3 으로 끝난다."""
    settings = dataclasses.replace(client.app.state.cluster_service.settings, home_redis_tls=None)
    monkeypatch.setattr(shipped_redis_check, "settings_from_env", lambda: settings)
    assert asyncio.run(shipped_redis_check._main()) == shipped_redis_check.BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is False and "DAEGAM_HOME_REDIS_TLS" in out["blockers"][0]
