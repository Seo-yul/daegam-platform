from dataclasses import replace
from datetime import timedelta

from daegam.config import _home_ingress_from_env
from daegam.store.base import utcnow
from tests.helpers.clusters import post_cluster
from tests.helpers.docs import cluster_doc
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import api_client, detail_code, make_app


def test_create_validation_and_conflict(client):
    assert post_cluster(client).status_code == 201
    r = post_cluster(client)
    assert detail_code(r) == (409, "name_conflict")
    r = post_cluster(client, name="home")
    assert detail_code(r) == (422, "reserved_name")
    r = post_cluster(client, name="bad name")
    assert detail_code(r) == (422, "invalid_name")
    r = post_cluster(client, name="u", platform_internal_url="daegam:9000")
    assert detail_code(r) == (422, "invalid_value")
    # 끝을 고정하지 않던 정규식은 공백이 든 주소를 받았다
    r = post_cluster(client, name="u", platform_internal_url="http://daegam x:9000")
    assert detail_code(r) == (422, "invalid_value")
    r = post_cluster(client, name="e", connection={"kubeconfig": "  "})
    assert r.status_code == 422
    r = post_cluster(
        client, name="x", connection={"api_url": "https://h", "sa_token": "t", "kubeconfig": "k"}
    )
    assert r.status_code == 422


def test_failed_preflight_is_stored_but_cluster_registered(client, fake_clusters):
    fake_clusters.by_name["broken"] = FakeClusterClient(unreachable=True)
    r = post_cluster(client, name="broken")
    assert r.status_code == 201
    pf = r.json()["status"]["preflight"]
    assert pf["ok"] is False
    assert pf["missing"] == ["connect: fake cluster is unreachable"]
    assert client.get(f"/clusters/{r.json()['id']}").json()["status"]["preflight"]["ok"] is False

    fake_clusters.by_name["norbac"] = FakeClusterClient(
        denied={("toolhive.stacklok.dev", "mcpservers", "create")},
        toolhive_resources=["mcpservers", "mcpgroups"],
        operator_available=False,
    )
    pf = post_cluster(client, name="norbac").json()["status"]["preflight"]
    assert pf["ok"] is False
    assert "rbac: toolhive.stacklok.dev/mcpservers:create" in pf["missing"]
    assert "crd: toolhive.stacklok.dev/v1beta1/virtualmcpservers" in pf["missing"]
    assert any(m.startswith("operator:") for m in pf["missing"])
    denied = [x for x in pf["rbac"] if not x["allowed"]]
    assert denied == [
        {
            "group": "toolhive.stacklok.dev",
            "resource": "mcpservers",
            "verb": "create",
            "name": None,
            "namespace": None,
            "allowed": False,
        }
    ]


def test_preflight_rerun(client, fake_clusters):
    cid = post_cluster(client).json()["id"]
    fake_clusters.by_name["edge"].unreachable = True
    r = client.post(f"/clusters/{cid}/preflight")
    assert r.status_code == 200
    assert r.json()["preflight"]["ok"] is False
    assert client.get(f"/clusters/{cid}").json()["status"]["preflight"]["ok"] is False
    fake_clusters.by_name["edge"].unreachable = False
    assert client.post(f"/clusters/{cid}/preflight").json()["preflight"]["ok"] is True
    assert client.post("/clusters/nope/preflight").status_code == 404


def test_patch_updates_and_invalidates(client, fake_clusters, fake_secrets):
    cid = post_cluster(client).json()["id"]
    calls_before = len(fake_clusters.calls)
    r = client.patch(
        f"/clusters/{cid}",
        json={
            "name": "edge",
            "display_name": "Edge 2",
            "platform_internal_url": "http://daegam2.example:9000",
            "images": {"runtime_image": "rt:9", "pull_policy": "Always"},
            "scheduling": {"node_selector": {"pool": "gpu"}},
            "default_redis": {"address": "redis:6379"},
        },
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert (c["name"], c["display_name"]) == ("edge", "Edge 2")
    assert c["images"] == {
        "registry_prefix": None,
        "pull_secret": None,
        "pull_policy": "Always",
        "runtime_image": "rt:9",
        "agent_image": None,
    }
    assert c["scheduling"]["node_selector"] == {"pool": "gpu"}
    assert c["default_redis"] == {
        "address": "redis:6379",
        "password_secret": None,
        "tls": None,
        "plain_closed": False,
    }
    assert c["updated_at"] >= c["created_at"]
    # 접속 정보가 안 바뀌면 preflight 는 다시 돌지 않는다
    assert len(fake_clusters.calls) == calls_before
    # 무효화됐으니 다음 preflight 가 factory 를 다시 부른다
    client.post(f"/clusters/{cid}/preflight")
    assert len(fake_clusters.calls) == calls_before + 1

    # 접속 정보 교체 → Secret 갱신 + preflight 재실행
    r = client.patch(f"/clusters/{cid}", json={"connection": {"kubeconfig": "new: cfg\n"}})
    assert r.status_code == 200
    assert fake_secrets.get(f"cluster-{cid}", namespace="daegam-system") == {
        "kubeconfig": "new: cfg\n"
    }
    assert r.json()["status"]["preflight"]["ok"] is True

    assert client.patch(f"/clusters/{cid}", json={}).status_code == 422
    assert client.patch(f"/clusters/{cid}", json={"name": "home"}).status_code == 422
    # name 은 바꿀 수 없다 (D24). 같은 이름은 통과, 다른 이름은 422 name_immutable
    post_cluster(client, name="other")
    r = client.patch(f"/clusters/{cid}", json={"name": "other"})
    assert detail_code(r) == (422, "name_immutable")
    r = client.patch(f"/clusters/{cid}", json={"name": "edge", "display_name": "엣지"})
    assert r.status_code == 200 and (r.json()["name"], r.json()["display_name"]) == ("edge", "엣지")
    assert client.patch("/clusters/nope", json={"name": "x"}).status_code == 404


def test_delete_removes_secret(client, fake_secrets):
    cid = post_cluster(client).json()["id"]
    assert fake_secrets.get(f"cluster-{cid}", namespace="daegam-system") is not None
    r = client.delete(f"/clusters/{cid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert fake_secrets.get(f"cluster-{cid}", namespace="daegam-system") is None
    assert client.get(f"/clusters/{cid}").status_code == 404
    assert client.delete(f"/clusters/{cid}").status_code == 404


def test_no_home_cluster_without_secret_store(settings, mongo, fake_clusters):
    """home 모드도 없고 secret store 도 없으면 원격 클러스터 등록은 503 이다."""
    app = make_app(settings, fake_clusters, None)
    with api_client(app) as c:
        assert c.get("/clusters").json() == []
        r = post_cluster(c)
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "no_home_cluster"


def test_home_bootstrap_with_fake(settings, mongo, fake_clusters, fake_secrets):
    """DAEGAM_HOME_KUBECONTEXT 가 있으면 시작 시 home 이 upsert 되고 preflight 가 저장된다."""
    app = make_app(settings, fake_clusters, fake_secrets, home_kubecontext="fake-ctx")
    with api_client(app) as c:
        homes = c.get("/clusters").json()
        assert len(homes) == 1
        home = homes[0]
        assert home["name"] == "home" and home["home"] is True
        assert home["connection"] == {
            "mode": "kubecontext",
            "kubecontext": "fake-ctx",
            "secret_name": None,
            "api_url": None,
        }
        assert home["platform_internal_url"] == "http://host.internal:9000"
        assert home["status"]["preflight"]["ok"] is True
        assert home["status"]["preflight"]["toolhive_version"] == "0.53.0"
        # home 은 지우거나 접속 정보를 바꿀 수 없다
        assert c.delete(f"/clusters/{home['id']}").json()["detail"]["code"] == "home_cluster"
        r = c.patch(f"/clusters/{home['id']}", json={"connection": {"kubeconfig": "x"}})
        assert detail_code(r) == (422, "reserved_name")
        # 다른 설정은 바꿀 수 있다
        assert (
            c.patch(
                f"/clusters/{home['id']}", json={"images": {"runtime_image": "rt:h"}}
            ).status_code
            == 200
        )
        home_id = home["id"]
    # 두 번째 기동(레플리카)도 같은 문서를 유지한다
    app2 = make_app(settings, fake_clusters, fake_secrets, home_kubecontext="fake-ctx")
    with api_client(app2) as c:
        homes = c.get("/clusters").json()
        assert len(homes) == 1 and homes[0]["id"] == home_id
        assert homes[0]["images"]["runtime_image"] == "rt:h"  # setOnInsert 라 유지


def test_is_http_url():
    from daegam.urls import is_http_url

    for ok in ("http://a", "https://daegam.example:9000/x", "http://[::1]:80", "http://10.0.0.1"):
        assert is_http_url(ok), ok
    for bad in ("daegam:9000", "ftp://a", "http://", "http://:80", "http://a b", "http://[::1"):
        assert not is_http_url(bad), bad


def test_in_cluster_platform_url(offline_settings):
    s = replace(offline_settings, in_cluster=True, namespace="daegam-system")
    assert s.home_mode == "in_cluster"
    assert s.home_platform_internal_url() == "http://daegam-platform.daegam-system.svc:9000"
    s = replace(offline_settings, home_kubecontext="orbstack")
    assert s.home_platform_internal_url() == "http://host.internal:9000"
    s = replace(offline_settings, in_cluster=True, platform_internal_url="http://override:1")
    assert s.home_platform_internal_url() == "http://override:1"
    assert offline_settings.home_mode is None


# -- P8 F4: Cluster ingress 재정의 ---------------------------------------------------------


def test_ingress_roundtrip_and_old_documents(client, mongo):
    body = {
        "enabled": True,
        "allow_unauthenticated": True,
        "class_name": "nginx",
        "allowed_classes": ["nginx", "traefik"],
        "domain": "mcp.example.com",
        "tls": "cert-manager",
        "default_issuer": "letsencrypt",
        "annotations": {"nginx.ingress.kubernetes.io/proxy-buffering": "off"},
        "controller_namespace": "ingress-nginx",
        "controller_ip_blocks": ["10.0.0.0/24"],
        "reserved_hosts": ["daegam.com"],
    }
    r = post_cluster(client, name="ing", ingress=body)
    assert r.status_code == 201, r.text
    assert r.json()["ingress"] == body
    cid = r.json()["id"]
    r = client.patch(
        f"/clusters/{cid}", json={"ingress": {"enabled": True, "class_name": "traefik"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["ingress"] == {
        "enabled": True,
        "allow_unauthenticated": False,
        "class_name": "traefik",
        "allowed_classes": [],
        "domain": None,
        "tls": "none",
        "default_issuer": None,
        "annotations": None,
        "controller_namespace": None,
        "controller_ip_blocks": [],
        "reserved_hosts": [],
    }
    assert post_cluster(client, "bad", ingress={"tls": "letsencrypt"}).status_code == 422
    # 예전 문서(필드 4개)는 새 필드가 기본값으로 읽힌다
    mongo["clusters"].update_one(
        {"_id": cid},
        {
            "$set": {
                "ingress": {"enabled": True, "class_name": "nginx", "domain": None, "tls": "none"}
            }
        },
    )
    got = client.get(f"/clusters/{cid}").json()["ingress"]
    assert got["allow_unauthenticated"] is False and got["allowed_classes"] == []
    assert got["controller_namespace"] is None and got["reserved_hosts"] == []


def test_home_ingress_defaults_from_env_are_reapplied(settings, fake_clusters, fake_secrets):
    """DAEGAM_HOME_INGRESS_* (make deploy-local 의 configmap) 는 기동마다 home 의 ingress 에
    덮어쓰고, 환경에 없는 ingress 필드와 다른 설정은 화면에서 고친 값이 남는다. 환경이 정한 칸은
    응답의 env_managed 에 실리고 PATCH 로 다른 값을 주면 409 다(D54)."""
    env = {
        "class_name": "traefik",
        "controller_namespace": "traefik",
        "allow_unauthenticated": True,
        "reserved_hosts": ["daegam.com"],
        "enabled": True,
    }
    s = replace(settings, home_kubecontext="fake-ctx", home_ingress=env)
    app = make_app(s, fake_clusters, fake_secrets)
    with api_client(app) as c:
        home = c.get("/clusters").json()[0]
        assert home["home"] is True
        assert home["ingress"] == {
            "enabled": True,
            "allow_unauthenticated": True,
            "class_name": "traefik",
            "allowed_classes": [],
            "domain": None,
            "tls": "none",
            "default_issuer": None,
            "annotations": None,
            "controller_namespace": "traefik",
            "controller_ip_blocks": [],
            "reserved_hosts": ["daegam.com"],
        }
        assert home["env_managed"] == sorted(
            ["platform_internal_url"] + [f"ingress.{k}" for k in env]
        )
        # 환경이 정한 칸을 다른 값으로 바꾸면 다음 기동에 되돌아가므로 409 다
        r = c.patch(
            f"/clusters/{home['id']}",
            json={"ingress": {**home["ingress"], "domain": "local.test", "class_name": "nginx"}},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "env_managed"
        assert r.json()["detail"]["fields"] == ["ingress.class_name"]
        # 화면은 카드 전체를 보낸다. 같은 값이 실려 오고 환경이 정하지 않은 domain 만 바뀌면 받는다
        r = c.patch(
            f"/clusters/{home['id']}",
            json={"ingress": {**home["ingress"], "domain": "local.test"}},
        )
        assert r.status_code == 200, r.text
    with api_client(make_app(s, fake_clusters, fake_secrets)) as c:
        ing = c.get("/clusters").json()[0]["ingress"]
        assert (
            ing["class_name"] == "traefik" and ing["domain"] == "local.test"
        )  # env 가 다시 덮는다
    # 환경이 비어 있으면 아무것도 덮지 않는다
    with api_client(make_app(s, fake_clusters, fake_secrets, home_ingress={})) as c:
        ing = c.get("/clusters").json()[0]["ingress"]
        assert ing["class_name"] == "traefik" and ing["allow_unauthenticated"] is True
    # env 파서
    import os

    saved = {k: os.environ.get(k) for k in os.environ if k.startswith("DAEGAM_HOME_INGRESS_")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        assert _home_ingress_from_env() == {}
        os.environ["DAEGAM_HOME_INGRESS_CLASS"] = "traefik"
        os.environ["DAEGAM_HOME_INGRESS_RESERVED_HOSTS"] = "daegam.com, api.daegam.com"
        os.environ["DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED"] = "true"
        assert _home_ingress_from_env() == {
            "class_name": "traefik",
            "reserved_hosts": ["daegam.com", "api.daegam.com"],
            "allow_unauthenticated": True,
            "enabled": True,
        }
        os.environ["DAEGAM_HOME_INGRESS_ENABLED"] = "false"
        assert _home_ingress_from_env()["enabled"] is False
    finally:
        for k in (
            "DAEGAM_HOME_INGRESS_CLASS",
            "DAEGAM_HOME_INGRESS_RESERVED_HOSTS",
            "DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED",
            "DAEGAM_HOME_INGRESS_ENABLED",
        ):
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})


# -- token 모드 클라이언트의 임시 CA 파일 정리 -----------------------------------------------


def test_token_mode_client_cleans_up_its_temp_ca_file():
    """token 모드는 CA 를 임시 파일로 떨궈야 하는데, 그 파일이 남으면 안 된다.

    캐시 TTL 이 5분이라 클라이언트는 계속 다시 만들어진다. 정리하지 않으면 파드의 /tmp
    (emptyDir)에 하루 수백 개가 쌓이고 커넥션 풀도 함께 샌다.
    """
    import gc
    import glob
    import tempfile
    from unittest.mock import patch

    import pytest

    from daegam.k8s.client import ClusterClient, ClusterUnreachable

    class _Store:
        def get(self, name, *, namespace):
            return {"token": "t", "ca_bundle": "-----BEGIN CERTIFICATE-----\nx\n"}

    cluster = cluster_doc("edge", mode="token", api_url="https://edge.example", secret_name="s")

    def crt_files() -> set[str]:
        return set(glob.glob(f"{tempfile.gettempdir()}/*.crt"))

    before = crt_files()
    # 1) 생성이 실패해도(디스커버리 오류) 파일을 남기지 않는다
    with patch("daegam.k8s.client.DynamicClient", side_effect=RuntimeError("no api server")):
        with pytest.raises(ClusterUnreachable):
            ClusterClient.from_cluster(cluster, _Store(), platform_namespace="daegam-system")
    assert crt_files() - before == set(), "생성 실패가 임시 CA 파일을 남겼다"

    # 2) 성공한 클라이언트는 살아 있는 동안 파일을 들고 있다가, 사라지면 지운다
    with patch("daegam.k8s.client.DynamicClient"):
        cc = ClusterClient.from_cluster(cluster, _Store(), platform_namespace="daegam-system")
    leaked = crt_files() - before
    assert len(leaked) == 1, f"CA 파일이 만들어져야 한다: {leaked}"
    del cc
    gc.collect()
    assert crt_files() - before == set(), "클라이언트가 사라졌는데 임시 CA 파일이 남았다"


def test_patch_writes_only_what_it_was_sent_and_checks_the_seq(client, fake_clusters, mongo):
    """PATCH 는 보낸 칸만 서버가 읽은 seq 를 조건으로 쓴다. 그사이 점검이 쓴 status·gates 를
    되돌리지 않고, 그사이 다른 저장이 지나갔으면 409 revision_conflict 다. 점검 결과는 그것이
    읽은 seq 일 때만 남는다 (P68 2절)."""
    cid = post_cluster(client).json()["id"]
    store = client.app.state.clusters
    service = client.app.state.cluster_service
    stale = client.portal.call(store.get, cid)
    mongo["clusters"].update_one(
        {"_id": cid}, {"$set": {"status.preflight.missing": ["meanwhile"]}}
    )
    stale.display_name = "Edge 2"
    assert client.portal.call(store.save_edit, stale, {"display_name"})
    doc = mongo["clusters"].find_one({"_id": cid})
    assert doc["status"]["preflight"]["missing"] == ["meanwhile"] and doc["seq"] == 1
    older = client.portal.call(store.get, cid)
    assert client.patch(f"/clusters/{cid}", json={"display_name": "Edge 3"}).status_code == 200
    older.display_name = "lost"
    assert not client.portal.call(store.save_edit, older, {"display_name"})
    # 읽은 뒤 사람의 저장이 지나간 점검은 결과를 남기지 않는다
    client.portal.call(service.preflight, older)
    assert mongo["clusters"].find_one({"_id": cid})["status"]["preflight"]["missing"] == [
        "meanwhile"
    ]


def test_a_connection_change_claims_first_and_its_own_preflight_is_stored(
    client, fake_clusters, fake_secrets, mongo
):
    """접속 정보를 바꾸는 PATCH 는 클레임을 잡고 Secret 을 쓴 뒤 저장한다. 마지막 저장이 올린 seq 를
    들고 도는 PATCH 자신의 점검은 저장된다. 클레임이 살아 있으면 다른 PATCH 는 409 다."""
    cid = post_cluster(client).json()["id"]
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"status.preflight.missing": ["old"]}})
    r = client.patch(f"/clusters/{cid}", json={"connection": {"kubeconfig": "new: cfg\n"}})
    assert r.status_code == 200, r.text
    doc = mongo["clusters"].find_one({"_id": cid})
    assert doc["seq"] == 2 and "claim" not in doc
    # 비밀은 플랫폼 네임스페이스 Secret 에만 있고 Mongo 에는 이름만 있다
    assert "new: cfg" not in str(doc) and doc["connection"]["secret_name"] == f"cluster-{cid}"
    stored = doc["status"]["preflight"]
    assert stored["missing"] == r.json()["status"]["preflight"]["missing"] != ["old"]
    claim = {"id": "other", "until": utcnow() + timedelta(minutes=1)}
    mongo["clusters"].update_one({"_id": cid}, {"$set": {"claim": claim}})
    r = client.patch(f"/clusters/{cid}", json={"connection": {"kubeconfig": "x: y\n"}})
    assert detail_code(r) == (409, "revision_conflict")
    assert fake_secrets.get(f"cluster-{cid}", namespace="daegam-system") == {
        "kubeconfig": "new: cfg\n"
    }


def test_home_upsert_raises_the_seq_only_when_a_value_changes(settings):
    """기동의 home upsert 는 환경 값이 바뀔 때만 seq 를 올린다. 기동 전에 읽은 PATCH 가 바뀐 값을
    되돌리지 않게 하고, 바뀐 것이 없으면 PATCH 를 헛되이 막지 않는다 (P68 2절)."""
    from daegam.store.clusters import ClusterConnection, MongoClusterStore
    from tests.helpers.aio import run_store

    async def go(store):
        await store.coll.delete_many({})
        conn = ClusterConnection(mode="kubecontext", kubecontext="ctx")
        env = {"platform_internal_url": "http://a:9000", "ingress.class_name": "traefik"}
        first = await store.upsert_home(conn, env)
        again = await store.upsert_home(conn, env)
        moved = await store.upsert_home(conn, {**env, "ingress.class_name": "nginx"})
        return first._seq, again._seq, moved._seq

    assert run_store(settings, MongoClusterStore, go) == (0, 0, 1)
