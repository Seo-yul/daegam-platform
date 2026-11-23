"""P55 5절: image 항목의 컨테이너 입력(stdio, args, env, Secret env)과 그 렌더, 항목마다의 Secret 과
그 장부, 값 바꾸기와 누가 바꾸는가, 공유 거절, 카탈로그 출처, stdio 항목의 점검."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pymongo.errors import DuplicateKeyError

from daegam.auth.principal import Membership, Principal
from daegam.k8s.client import ClusterUnreachable
from daegam.k8s.labels import LABEL_PLATFORM_ID
from daegam.k8s.templates.vmcp import SECRET_ENV_ANNOTATION
from daegam.mcp_client import AuthRefused
from daegam.secrets import K8sSecretStore
from daegam.services.hub import NOT_ANSWERING, STDIO_BACKOFF_FIRST, HubError
from daegam.services.hub_catalog import CatalogServer, snapshot
from daegam.services.hub_env import check_catalog_image
from daegam.store.base import utcnow
from daegam.store.hub import HubSource, secret_env_name
from tests.helpers.auth import as_principal
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import DEFAULT_NS, GV, HUB
from tests.helpers.http import detail_code
from tests.helpers.workers import run_data_plane_auth

TOKEN = "s3cr3t-p55-token-value"
OTHER = "an0ther-p55-secret"
CR = "hub-tools"


@pytest.fixture
def cluster(client):
    return default_cluster(client)


def image_body(name: str = "tools", **extra) -> dict:
    return {
        "name": name,
        "kind": "image",
        "image": "ghcr.io/acme/tools:1",
        "stdio": True,
        "args": ["--read-only"],
        "env": [{"name": "LOG_LEVEL", "value": "info"}, {"name": "HOME", "value": "/tmp"}],
        "secret_env": [{"name": "API_TOKEN", "value": TOKEN}, {"name": "API_SECRET"}],
        **extra,
    }


def server_cr(fake, name: str = CR) -> dict:
    return fake.get(GV, "MCPServer", name, DEFAULT_NS)


def secret_of(fake, item_id: str) -> dict[str, str] | None:
    return K8sSecretStore(fake).get(secret_env_name(item_id), namespace=DEFAULT_NS)


def edge(fake_clusters):
    return fake_clusters.by_name["edge"]


def test_an_image_item_renders_its_container_and_keeps_values_out_of_documents(
    client, cluster, fake_clusters, mongo
):
    """stdio 컨테이너는 transport 를 프록시가 내고, args·env 는 spec 에, 값이 든 Secret env 이름은
    백엔드 컨테이너의 secretKeyRef 로 간다. 쓸 수 있는 `/tmp` 와 판 어노테이션이 붙는다. 값은 응답,
    문서, 매니페스트 어디에도 없다."""
    r = client.post(HUB, json=image_body())
    assert r.status_code == 201, r.text
    item = r.json()
    assert TOKEN not in r.text
    assert item["source"]["stdio"] is True and item["source"]["transport"] == "streamable-http"
    assert item["source"]["secret_env"] == [
        {"name": "API_TOKEN", "set": True},
        {"name": "API_SECRET", "set": False},
    ]
    assert item["created_by"] and item["secret_env_rev"] == 1
    assert item["permissions"]["replace_secret_env"] is True

    spec = server_cr(edge(fake_clusters))["spec"]
    assert (spec["transport"], spec["proxyMode"]) == ("stdio", "streamable-http")
    assert spec["args"] == ["--read-only"]
    assert spec["env"] == [
        {"name": "LOG_LEVEL", "value": "info"},
        {"name": "HOME", "value": "/tmp"},
    ]
    pod = spec["podTemplateSpec"]
    mcp = pod["spec"]["containers"][0]
    name = secret_env_name(item["id"])
    assert mcp["env"] == [
        {"name": "API_TOKEN", "valueFrom": {"secretKeyRef": {"name": name, "key": "API_TOKEN"}}}
    ]
    assert mcp["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert pod["spec"]["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
    assert pod["metadata"]["annotations"] == {SECRET_ENV_ANNOTATION: "1"}

    assert secret_of(edge(fake_clusters), item["id"]) == {"API_TOKEN": TOKEN}
    labels = edge(fake_clusters).get("v1", "Secret", name, DEFAULT_NS)["metadata"]["labels"]
    assert labels[LABEL_PLATFORM_ID] and labels["daegam.devops.ai.kr/id"] == item["id"]
    row = mongo.hub_env_secrets.find_one({"_id": item["id"]})
    assert (row["cluster_id"], row["namespace"]) == (item["cluster_id"], DEFAULT_NS)

    doc = mongo.hub_items.find_one({"_id": item["id"]})
    assert TOKEN not in json.dumps(doc, default=str)
    manifest = client.get(f"{HUB}/{item['id']}/manifest")
    assert manifest.status_code == 200 and TOKEN not in manifest.text
    assert TOKEN not in client.get(f"{HUB}/{item['id']}").text


def test_an_http_image_without_new_fields_renders_as_before(client, cluster, fake_clusters):
    """새 칸을 쓰지 않는 항목의 렌더에는 새 칸이 하나도 없다."""
    r = client.post(HUB, json={"name": "plain", "kind": "image", "image": "ghcr.io/a/b:1"})
    assert r.status_code == 201, r.text
    spec = server_cr(edge(fake_clusters), "hub-plain")["spec"]
    assert spec["transport"] == "streamable-http"
    assert not {"proxyMode", "args", "env"} & set(spec)
    assert "volumes" not in spec["podTemplateSpec"]["spec"]
    assert "annotations" not in spec["podTemplateSpec"]["metadata"]
    for key in ("stdio", "args", "env", "secret_env", "catalog"):
        assert key not in r.json()["source"]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"port": 8080}, "invalid_value"),
        ({"env": [{"name": "LD_PRELOAD", "value": "/x.so"}]}, "env_reserved"),
        ({"env": [{"name": "http_proxy", "value": "http://p"}]}, "env_reserved"),
        ({"env": [{"name": "DAEGAM_TOKEN", "value": "x"}]}, "env_reserved"),
        ({"env": [{"name": "MCP_PORT", "value": "1"}]}, "env_reserved"),
        ({"secret_env": [{"name": "NODE_OPTIONS", "value": TOKEN}]}, "env_reserved"),
        ({"env": [{"name": "GIT_SSH_COMMAND", "value": "sh -c x"}]}, "env_reserved"),
        ({"env": [{"name": "GIT_CONFIG_KEY_0", "value": "core.pager"}]}, "env_reserved"),
        ({"env": [{"name": "npm_config_registry", "value": "http://r"}]}, "env_reserved"),
        ({"env": [{"name": "OPENSSL_CONF", "value": "/tmp/x"}]}, "env_reserved"),
        ({"env": [{"name": "PIP_INDEX_URL", "value": "http://r"}]}, "env_reserved"),
        ({"env": [{"name": "LOG_LEVEL", "value": "a\nb"}]}, "invalid_value"),
        ({"env": [{"name": "API_TOKEN", "value": "x"}]}, "invalid_value"),
        ({"env": [{"name": "1BAD", "value": "x"}]}, "invalid_value"),
    ],
)
def test_container_inputs_are_checked_at_creation(client, cluster, mongo, extra, code):
    r = client.post(HUB, json=image_body(**extra))
    assert detail_code(r) == (422, code), r.text
    assert TOKEN not in r.text
    assert mongo.hub_items.count_documents({}) == 0


def test_container_inputs_are_for_image_items_only(client, cluster):
    entry = {"kind": "entry", "url": "https://mcp.example.com/mcp", "transport": "streamable-http"}
    r = client.post(HUB, json={"name": "r", **entry, "stdio": True})
    assert detail_code(r) == (422, "invalid_value")


def test_a_secret_value_never_comes_back_in_an_error(client, cluster):
    big = "x" * (40 * 1024)
    body = image_body(secret_env=[{"name": "API_TOKEN", "value": big}])
    r = client.post(HUB, json=body)
    assert detail_code(r) == (422, "invalid_value") and big not in r.text
    # 요청 검증의 422 는 어느 라우트든 입력값을 싣지 않는다 (T77)
    r = client.post(HUB, json={**image_body(), "secret_env": [{"name": "A", "value": 5}]})
    assert r.status_code == 422 and "input" not in r.text


@pytest.mark.parametrize("action", ["view", "consume", "manage"])
def test_an_item_with_secret_env_is_not_opened_to_other_tenants(client, cluster, mongo, action):
    opened = {action: {"tenants": ["*"]}}
    r = client.post(HUB, json=image_body(access=opened))
    assert detail_code(r) == (422, "secret_not_shareable")
    item = client.post(HUB, json=image_body(name=f"t-{action}")).json()
    r = client.put(f"{HUB}/{item['id']}/access", json=opened)
    assert detail_code(r) == (422, "secret_not_shareable")
    # 테넌트 안의 정책은 된다
    inside = {action: {"roles": ["developer"]}}
    assert client.put(f"{HUB}/{item['id']}/access", json=inside).status_code == 200


def test_replacing_values_patches_only_the_names_sent(client, cluster, fake_clusters, mongo):
    """보낸 이름만 바뀌고 다른 키는 그대로다. 판이 올라 백엔드 파드의 어노테이션이 바뀌고, 누가
    언제 바꿨는지 남는다. 모르는 이름은 422 다."""
    item = client.post(HUB, json=image_body()).json()
    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_SECRET": OTHER}})
    assert r.status_code == 200, r.text
    assert OTHER not in r.text and TOKEN not in r.text
    out = r.json()
    assert out["secret_env_rev"] == 2 and out["secret_env_updated_by"]
    assert out["source"]["secret_env"] == [
        {"name": "API_TOKEN", "set": True},
        {"name": "API_SECRET", "set": True},
    ]
    assert secret_of(edge(fake_clusters), item["id"]) == {"API_TOKEN": TOKEN, "API_SECRET": OTHER}
    pod = server_cr(edge(fake_clusters))["spec"]["podTemplateSpec"]
    assert pod["metadata"]["annotations"] == {SECRET_ENV_ANNOTATION: "2"}
    assert [e["name"] for e in pod["spec"]["containers"][0]["env"]] == ["API_TOKEN", "API_SECRET"]
    assert mongo.hub_items.find_one({"_id": item["id"]}).get("claim") is None
    # 바꾸기가 지금 판을 적었으니 재적용이 같은 렌더를 한 번 더 하지 않는다
    assert item["id"] not in run_data_plane_auth(client)["reapplied"]

    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"NOPE": "x"}})
    assert detail_code(r) == (422, "invalid_value")
    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": 5}})
    assert r.status_code == 422 and "input" not in r.text
    plain = client.post(HUB, json={"name": "plain", "kind": "image", "image": "a/b:1"}).json()
    r = client.put(f"{HUB}/{plain['id']}/secret-env", json={"values": {"A": "x"}})
    assert detail_code(r) == (422, "invalid_kind")


def test_a_missing_secret_needs_every_name(client, cluster, fake_clusters):
    """값 없이 만든 항목에는 Secret 이 없다. 일부 이름만 보내면 409 secret_missing 이고 아무것도
    쓰지 않으며, 이름을 모두 보내면 만든다."""
    body = image_body(secret_env=[{"name": "API_TOKEN"}, {"name": "API_SECRET"}])
    item = client.post(HUB, json=body).json()
    assert secret_of(edge(fake_clusters), item["id"]) is None
    assert (
        "env"
        not in server_cr(edge(fake_clusters))["spec"]["podTemplateSpec"]["spec"]["containers"][0]
    )
    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": TOKEN}})
    assert detail_code(r) == (409, "secret_missing")
    assert client.get(f"{HUB}/{item['id']}").json()["secret_env_rev"] == 1
    values = {"API_TOKEN": TOKEN, "API_SECRET": OTHER}
    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": values})
    assert r.status_code == 200, r.text
    assert secret_of(edge(fake_clusters), item["id"]) == values


def test_only_the_creator_and_tenant_admins_replace_values(client, cluster):
    with as_principal(client, "dev-a", "developer"):
        item = client.post(HUB, json=image_body()).json()
        assert item["created_by"] == "dev-a"
        put = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": "a"}})
        assert put.status_code == 200
    with as_principal(client, "dev-b", "developer"):
        assert (
            client.get(f"{HUB}/{item['id']}").json()["permissions"]["replace_secret_env"] is False
        )
        put = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": "b"}})
        assert detail_code(put) == (403, "forbidden")
    with as_principal(client, "dev-a", "viewer"):
        # 만든 사람이 hub:write 를 잃었다
        assert (
            client.get(f"{HUB}/{item['id']}").json()["permissions"]["replace_secret_env"] is False
        )
    with as_principal(client, "admin-c", "tenant-admin"):
        put = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": "c"}})
        assert put.status_code == 200


def test_a_failed_apply_is_retried_by_the_reapply(
    client, cluster, fake_clusters, mongo, monkeypatch
):
    """판은 적용보다 먼저 적힌다. 적용이 실패해도 기대 판이 적힌 판과 달라 재적용이 새 판으로 다시
    렌더한다. 시작한 쓰기가 실패한 클레임은 기한까지 남아 그동안 삭제가 409 다."""
    item = client.post(HUB, json=image_body()).json()
    hub = client.app.state.hub_service
    real = hub._apply_crs

    async def unreachable(_item, **_kw):
        raise ClusterUnreachable("edge", "gone")

    monkeypatch.setattr(hub, "_apply_crs", unreachable)
    r = client.put(f"{HUB}/{item['id']}/secret-env", json={"values": {"API_TOKEN": OTHER}})
    assert detail_code(r) == (502, "cluster_unreachable")
    doc = mongo.hub_items.find_one({"_id": item["id"]})
    assert doc["secret_env_rev"] == 2 and doc["claim"]
    assert detail_code(client.delete(f"{HUB}/{item['id']}")) == (409, "revision_conflict")

    monkeypatch.setattr(hub, "_apply_crs", real)
    assert item["id"] in run_data_plane_auth(client)["reapplied"]
    pod = server_cr(edge(fake_clusters))["spec"]["podTemplateSpec"]
    assert pod["metadata"]["annotations"] == {SECRET_ENV_ANNOTATION: "2"}


def test_delete_removes_objects_then_the_secret_then_the_document(
    client, cluster, fake_clusters, mongo, monkeypatch
):
    item = client.post(HUB, json=image_body()).json()
    fake = edge(fake_clusters)
    hub = client.app.state.hub_service
    real_delete = hub.store.delete
    secret_when_doc_went: list[bool] = []

    async def delete(item_id):
        # 이벤트 루프 위라 fake 의 메서드를 부르지 않고 객체 표를 본다
        key = ("v1", "Secret", DEFAULT_NS, secret_env_name(item_id))
        secret_when_doc_went.append(key in fake.objects)
        return await real_delete(item_id)

    monkeypatch.setattr(hub.store, "delete", delete)
    start = len(fake.deleted)
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    kinds = [kind for _api, kind, _ns, _name in fake.deleted[start:]]
    assert kinds[0] == "MCPServer" and kinds[-1] == "Secret"
    # 문서는 Secret 이 지워진 뒤에 지운다
    assert secret_when_doc_went == [False]
    assert secret_of(fake, item["id"]) is None
    assert mongo.hub_items.find_one({"_id": item["id"]}) is None
    assert mongo.hub_env_secrets.count_documents({"_id": item["id"]}) == 0


def test_a_name_conflict_after_the_secret_write_removes_the_secret(
    client, cluster, fake_clusters, monkeypatch
):
    hub = client.app.state.hub_service
    written: list[str] = []
    real = hub.secret_envs.create

    async def create(item, values, kube):
        written.append(item.id)
        await real(item, values, kube)

    async def duplicate(_item):
        raise DuplicateKeyError("E11000 duplicate key")

    monkeypatch.setattr(hub.secret_envs, "create", create)
    monkeypatch.setattr(hub.store, "create", duplicate)
    r = client.post(HUB, json=image_body())
    assert detail_code(r) == (409, "name_conflict")
    assert written and secret_of(edge(fake_clusters), written[0]) is None


def test_the_sweep_deletes_secret_env_secrets_whose_item_is_gone(
    client, cluster, fake_clusters, mongo, monkeypatch
):
    """플랫폼은 테넌트 네임스페이스의 Secret 목록을 볼 수 없어 장부로 찾는다. 막 쓴 행과 살아 있는
    항목의 행은 둔다. 지우지 못한 행 하나는 그 행만 남고 정리는 이어 간다."""
    live = client.post(HUB, json=image_body()).json()
    hub = client.app.state.hub_service
    fake = edge(fake_clusters)
    real = hub.secret_envs._delete_row

    async def delete_row(row):
        if row.id == "bad-1":
            raise PermissionError("forbidden")
        return await real(row)

    monkeypatch.setattr(hub.secret_envs, "_delete_row", delete_row)
    old = utcnow() - timedelta(hours=1)
    for orphan in ("bad-1", "gone-1", "fresh-1"):
        K8sSecretStore(fake).put(secret_env_name(orphan), {"A": "x"}, namespace=DEFAULT_NS)
        client.portal.call(
            lambda o=orphan: hub.secret_envs.ledger.note_write(
                o,
                utcnow() if o == "fresh-1" else old,
                cluster_id=live["cluster_id"],
                namespace=DEFAULT_NS,
            )
        )
    mongo.hub_env_secrets.update_one({"_id": live["id"]}, {"$set": {"written_at": old}})
    assert client.portal.call(hub.secret_envs.sweep, None) == {"dropped": ["gone-1"]}
    assert secret_of(fake, "gone-1") is None
    assert secret_of(fake, "fresh-1") is not None
    assert secret_of(fake, live["id"]) is not None
    assert secret_of(fake, "bad-1") is not None
    assert {d["_id"] for d in mongo.hub_env_secrets.find()} == {live["id"], "fresh-1", "bad-1"}
    assert mongo.hub_env_secrets.find_one({"_id": "bad-1"})["swept_at"] is not None


def test_a_replacement_read_before_a_delete_does_not_recreate_the_secret(
    client, cluster, fake_clusters, mongo
):
    """삭제가 먼저 시작되면, 그 전에 항목을 읽은 바꾸기는 클레임을 잡지 못해 409 이고 Secret 을 다시
    만들지 않는다."""
    item = client.post(HUB, json=image_body()).json()
    hub = client.app.state.hub_service
    stale = client.portal.call(hub.store.get_unscoped, item["id"])
    fake = edge(fake_clusters)
    assert client.delete(f"{HUB}/{item['id']}").status_code == 200
    assert secret_of(fake, item["id"]) is None
    # 삭제가 시작되고 아직 문서가 남은 순간이다
    mongo.hub_items.insert_one({**stale.to_doc(), "state": "deleting"})
    admin = Principal(sub="admin", memberships=[Membership(tenant="default", role="tenant-admin")])
    with pytest.raises(HubError) as e:
        client.portal.call(
            lambda: hub.set_secret_env(
                stale, {"API_TOKEN": OTHER}, principal=admin, tenant="default"
            )
        )
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    assert secret_of(fake, item["id"]) is None
    assert mongo.hub_items.find_one({"_id": item["id"]}).get("claim") is None


def test_a_catalog_image_waits_for_the_hub_image_settings(client, cluster):
    r = client.post(HUB, json={**image_body(), "catalog": "fetch"})
    assert detail_code(r) == (422, "hub_images_unset")


def test_a_catalog_remote_must_match_the_snapshot(client, cluster):
    """카탈로그의 원격 서버는 entry 항목으로 가져온다. remote 항목으로는 가져오지 못한다."""
    cat = snapshot()
    plain = next(
        s
        for s in cat.servers
        if s.kind == "remote"
        and s.transport != "sse"
        and not s.needs_login
        and not s.needs_credentials
    )
    body = {"name": "from-cat", "kind": "entry", "url": plain.url, "transport": plain.transport}
    r = client.post(HUB, json={**body, "kind": "remote", "catalog": plain.name})
    assert detail_code(r) == (422, "invalid_kind"), r.text
    r = client.post(HUB, json={**body, "catalog": plain.name})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "entry" and r.json()["k8s"]["kind"] is None
    assert r.json()["source"]["catalog"] == {"server": plain.name, "release": cat.release}
    other = {**body, "name": "wrong", "url": "https://other.example.com/mcp"}
    assert detail_code(client.post(HUB, json={**other, "catalog": plain.name})) == (
        422,
        "invalid_value",
    )
    login = next(s for s in cat.servers if s.needs_login)
    r = client.post(
        HUB,
        json={
            "name": "login",
            "kind": "entry",
            "url": login.url,
            "transport": login.transport,
            "catalog": login.name,
        },
    )
    assert detail_code(r) == (422, "invalid_value")


def test_a_catalog_remote_that_speaks_only_sse_is_refused(client, cluster):
    """SSE 로만 말하는 카탈로그 원격은 항목이 적은 전송과 상관없이 가져오지 못한다 (P54b 15절 3)."""
    sse = next(
        s
        for s in snapshot().servers
        if s.kind == "remote" and s.transport == "sse" and not s.needs_login
    )
    for transport in ("sse", "streamable-http"):
        body = {"name": f"sse-{transport}", "kind": "entry", "url": sse.url}
        r = client.post(HUB, json={**body, "transport": transport, "catalog": sse.name})
        assert detail_code(r) == (422, "outside_sse_unsupported"), r.text


# -- stdio 점검 --------------------------------------------------------------------


class Lister:
    def __init__(self) -> None:
        self.fail = True

    async def __call__(self, _client, _url, _headers=None):
        if self.fail:
            raise RuntimeError("no answer")
        return ["echo"]


def _stdio_pods(fake, *, backend: str = "Running", owner_rs: str = f"{CR}-abc") -> dict:
    """프록시 Deployment, 백엔드 파드, 프록시 파드를 둔다. 프록시 파드 객체다."""
    fake.objects[("apps/v1", "Deployment", DEFAULT_NS, CR)] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": CR,
            "namespace": DEFAULT_NS,
            "ownerReferences": [{"kind": "MCPServer", "name": CR, "controller": True}],
        },
    }
    fake.add_pod(DEFAULT_NS, f"{CR}-0", {"daegam.devops.ai.kr/mcp-backend": CR}, phase=backend)
    proxy = fake.add_pod(
        DEFAULT_NS,
        f"{CR}-abc-xyz",
        {
            "app.kubernetes.io/name": "mcpserver",
            "app.kubernetes.io/instance": CR,
            "pod-template-hash": "abc",
        },
        uid="proxy-uid-1",
    )
    proxy["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": owner_rs, "controller": True}
    ]
    return proxy


def _ready(fake) -> None:
    fake.set_status(
        GV,
        "MCPServer",
        CR,
        DEFAULT_NS,
        {"phase": "Ready", "url": f"http://mcp-{CR}-proxy.{DEFAULT_NS}.svc:8080/mcp"},
    )


def test_a_stdio_item_that_stops_answering_gets_its_proxy_pod_recreated(
    client, cluster, fake_clusters, mongo
):
    """백엔드 파드가 Running 인데 tools/list 가 실패하면 상태가 not_answering 이 된다. 실패가 한 틱
    넘게 이어지면 프록시 파드를 그 uid 로 지운다. 백오프 안에서는 다시 지우지 않고, 지나면 두 배로
    늘린다. 답이 돌아오면 칸이 지워진다."""
    item = client.post(HUB, json=image_body()).json()
    fake = edge(fake_clusters)
    _ready(fake)
    _stdio_pods(fake)
    hub = client.app.state.hub_service
    lister = Lister()
    hub.tool_lister = lister
    first = utcnow()

    # 처음 본 실패는 적기만 한다
    assert client.portal.call(hub.probe_stdio, first) == {}
    assert not [k for k in fake.deleted if k[1] == "Pod"]
    status = client.get(f"{HUB}/{item['id']}").json()["status"]
    assert (status["phase"], status["ready"]) == (NOT_ANSWERING, False)

    now = first + timedelta(seconds=60)
    assert client.portal.call(hub.probe_stdio, now) == {"restarted": [item["id"]]}
    assert ("v1", "Pod", DEFAULT_NS, f"{CR}-abc-xyz") in fake.deleted
    probe = mongo.hub_items.find_one({"_id": item["id"]})["stdio_probe"]
    assert probe["deletes"] == 1

    _stdio_pods(fake)
    assert client.portal.call(hub.probe_stdio, now + timedelta(seconds=60)) == {}
    later = now + STDIO_BACKOFF_FIRST + timedelta(seconds=1)
    assert client.portal.call(hub.probe_stdio, later) == {"restarted": [item["id"]]}
    probe = mongo.hub_items.find_one({"_id": item["id"]})["stdio_probe"]
    assert probe["deletes"] == 2

    lister.fail = False
    assert client.portal.call(hub.probe_stdio, later) == {}
    assert "stdio_probe" not in mongo.hub_items.find_one({"_id": item["id"]})
    assert client.get(f"{HUB}/{item['id']}").json()["status"]["phase"] == "Ready"


@pytest.mark.parametrize(
    ("backend", "owner_rs"),
    [("Pending", f"{CR}-abc"), ("Running", "someone-else-abc")],
)
def test_the_probe_leaves_pods_it_cannot_tie_to_the_item(
    client, cluster, fake_clusters, mongo, backend, owner_rs
):
    """백엔드가 뜨지 않았으면 다른 문제라 손대지 않고, 소유 사슬이 그 항목의 프록시 Deployment 에
    닿지 않는 파드는 지우지 않는다."""
    item = client.post(HUB, json=image_body()).json()
    fake = edge(fake_clusters)
    _ready(fake)
    _stdio_pods(fake, backend=backend, owner_rs=owner_rs)
    hub = client.app.state.hub_service
    hub.tool_lister = Lister()
    now = utcnow()
    for at in (now, now + timedelta(minutes=5)):
        assert client.portal.call(hub.probe_stdio, at) == {}
    assert not [k for k in fake.deleted if k[1] == "Pod"]
    # 백엔드가 Running 이면 실패는 적혔다. 점검이 항목을 건너뛴 것이 아니다
    probe = mongo.hub_items.find_one({"_id": item["id"]}).get("stdio_probe")
    assert (probe is not None) == (backend == "Running")


def test_a_refused_probe_is_not_counted(client, cluster, fake_clusters, mongo):
    """401·403 은 프록시가 답한 것이라 stdio 프로세스의 일로 세지 않는다."""
    item = client.post(HUB, json=image_body()).json()
    fake = edge(fake_clusters)
    _ready(fake)
    _stdio_pods(fake)
    hub = client.app.state.hub_service

    async def refusing(_client, _url, _headers=None):
        raise AuthRefused(403)

    hub.tool_lister = refusing
    now = utcnow()
    for at in (now, now + timedelta(minutes=5)):
        assert client.portal.call(hub.probe_stdio, at) == {}
    assert mongo.hub_items.find_one({"_id": item["id"]}).get("stdio_probe") is None
    assert not [k for k in fake.deleted if k[1] == "Pod"]


def test_a_failing_item_does_not_stop_the_probe(client, cluster, fake_clusters, mongo, monkeypatch):
    """항목 하나의 오류(예를 들어 감싸지 않은 Forbidden)는 그 항목만 건너뛰고 다음 항목을 본다."""
    broken = client.post(HUB, json=image_body(name="broken")).json()
    item = client.post(HUB, json=image_body()).json()
    fake = edge(fake_clusters)
    _ready(fake)
    _stdio_pods(fake)
    hub = client.app.state.hub_service
    hub.tool_lister = Lister()
    real = hub._probe_one

    async def probe_one(it, now):
        if it.id == broken["id"]:
            raise PermissionError("forbidden")
        return await real(it, now)

    monkeypatch.setattr(hub, "_probe_one", probe_one)
    assert client.portal.call(hub.probe_stdio, utcnow()) == {}
    assert mongo.hub_items.find_one({"_id": item["id"]}).get("stdio_probe") is not None


# -- 카탈로그 image 의 규칙 (B3 의 허용이 부른다) ------------------------------------------


def _catalog_server(**fields):
    base = {
        "name": "srv",
        "title": "Srv",
        "kind": "image",
        "transport": "streamable-http",
        "image": {"repository": "ghcr.io/a/srv", "tag": "1"},
        "stdio": True,
        "args": ["--ro"],
        "env": [
            {"name": "MODE"},
            {"name": "TOKEN", "secret": True, "required": True},
        ],
    }
    return CatalogServer.model_validate({**base, **fields})


def _source(**fields):
    base = {
        "image": "x",
        "stdio": True,
        "args": ["--ro"],
        "env": [{"name": "MODE", "value": "a"}],
        "secret_env": [{"name": "TOKEN"}],
    }
    return HubSource.model_validate({**base, **fields})


def _check(server, source, *, values=None, admin=False):
    check_catalog_image(
        server,
        source,
        values=values if values is not None else {"TOKEN": "t"},
        platform_admin=admin,
        error_cls=HubError,
    )


def _code(fn) -> tuple[int, str]:
    with pytest.raises(HubError) as e:
        fn()
    return e.value.status, e.value.code


def test_catalog_images_run_with_the_snapshot_arguments_and_declared_env() -> None:
    """카탈로그 서버의 image 항목은 스냅숏의 인자로 돈다. 스냅숏 그대로면 누구나, 다르면
    platform-admin 만이다. 스냅숏에 없거나 인자에 변수가 있는 서버는 platform-admin 만 만든다. 비밀
    변수와 꼭 필요한 변수의 규칙은 누구에게나 같다."""
    server = _catalog_server()
    _check(server, _source())
    assert _code(lambda: _check(None, _source())) == (403, "catalog_server_admin_only")
    _check(None, _source(), admin=True)
    varied = _catalog_server(args=["--key", "{KEY}"], args_variables=True)
    assert _code(lambda: _check(varied, _source())) == (403, "catalog_server_admin_only")
    other_args = _source(args=["--write"])
    assert _code(lambda: _check(server, other_args)) == (403, "catalog_settings_admin_only")
    _check(server, other_args, admin=True)
    undeclared = _source(env=[{"name": "HOME", "value": "/tmp"}])
    assert _code(lambda: _check(server, undeclared)) == (403, "catalog_settings_admin_only")
    plain_secret = _source(env=[{"name": "TOKEN", "value": "t"}], secret_env=None)
    assert _code(lambda: _check(server, plain_secret, admin=True)) == (422, "env_must_be_secret")
    assert _code(lambda: _check(server, _source(), values={}, admin=True)) == (
        422,
        "env_required_missing",
    )
