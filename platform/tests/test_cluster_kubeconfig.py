"""등록한 kubeconfig 는 명령과 파일 참조를 싣지 못한다 (k8s/kubeconfig.py).

kubernetes 클라이언트는 `exec`·`auth-provider` 를 플랫폼 파드 안에서 명령으로 돌리고 `tokenFile`·
`client-certificate`·`client-key`·`certificate-authority` 를 파드의 파일에서 읽는다. 등록과 접속
정보 수정(api/clusters.py)은 422 로 막고, 그 전에 저장된 Secret 은 접속(k8s/client.py)에서 멈춘다.
열지 못하는 kubeconfig 도 두 자리에서 같이 멈추고, 오류에는 틀린 줄 대신 자리만 싣는다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml

from daegam.k8s.client import ClusterClient, ClusterUnreachable
from daegam.k8s.kubeconfig import forbidden_kubeconfig_paths
from daegam.store.clusters import Cluster
from tests.helpers.clusters import post_cluster
from tests.helpers.docs import cluster_doc

INLINE_USER = {"token": "T", "client-certificate-data": "Q0VSVA==", "client-key-data": "S0VZ"}
# 6행 26칸에서 문법이 깨진다. PyYAML 의 오류 글은 이 줄을 통째로 싣는다
SECRET = "S3cr3t-Pa55"
BROKEN = f"apiVersion: v1\nkind: Config\nusers:\n- name: u\n  user:\n    password: {SECRET}: w0rd\n"
# 펼치면 10^7 노드다. yamlsafe 의 상한(백만)을 넘는다
ALIAS_BOMB = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]\n" for i in range(6)
)
INLINE_CLUSTER = {"certificate-authority-data": "Q0E="}
# (경로, users[0].user 에 더할 것, clusters[0].cluster 에 더할 것)
FORBIDDEN = [
    ("users[0].user.exec", {"exec": {"command": "sh", "args": ["-c", "id"]}}, {}),
    (
        "users[0].user.auth-provider",
        {"auth-provider": {"name": "gcp", "config": {"cmd-path": "id"}}},
        {},
    ),
    (
        "users[0].user.tokenFile",
        {"tokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"},
        {},
    ),
    ("users[0].user.client-certificate", {"client-certificate": "/etc/passwd"}, {}),
    ("users[0].user.client-key", {"client-key": "/tmp/key.pem"}, {}),
    ("clusters[0].cluster.certificate-authority", {}, {"certificate-authority": "/etc/ssl/ca.crt"}),
]


def _kubeconfig(user: dict | None = None, cluster: dict | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "edge",
        "clusters": [
            {
                "name": "edge",
                "cluster": {"server": "https://10.0.0.3:6443", **INLINE_CLUSTER, **(cluster or {})},
            }
        ],
        "contexts": [{"name": "edge", "context": {"cluster": "edge", "user": "deployer"}}],
        "users": [{"name": "deployer", "user": {**INLINE_USER, **(user or {})}}],
    }


def _post_kubeconfig(client, kubeconfig: dict | str):
    """kubeconfig 로 클러스터 edge-kc 를 등록한 응답. dict 는 YAML 로 적어 보낸다."""
    text = kubeconfig if isinstance(kubeconfig, str) else yaml.safe_dump(kubeconfig)
    return post_cluster(client, "edge-kc", connection={"kubeconfig": text})


def _cluster() -> Cluster:
    return cluster_doc("edge", mode="kubeconfig", secret_name="s")


class _Store:
    """kubeconfig 하나를 담은 Secret 저장소."""

    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig

    def get(self, name, *, namespace):
        return {"kubeconfig": self.kubeconfig}


def test_forbidden_paths_name_each_entry():
    cfg = _kubeconfig()
    cfg["users"].append({"name": "second", "user": {"exec": {}, "tokenFile": "/t"}})
    assert forbidden_kubeconfig_paths(cfg) == ["users[1].user.exec", "users[1].user.tokenFile"]
    assert forbidden_kubeconfig_paths(_kubeconfig()) == []
    for path, user, cluster in FORBIDDEN:
        assert forbidden_kubeconfig_paths(_kubeconfig(user, cluster)) == [path]
    # 모양이 다르면 볼 자리가 없다. 그런 kubeconfig 는 접속에서 따로 실패한다
    for odd in ("text", None, {"users": "x"}, {"users": [None, {"user": "x"}]}):
        assert forbidden_kubeconfig_paths(odd) == []


def test_registering_rejects_commands_and_file_references(client, fake_secrets):
    """등록은 명령을 싣는 kubeconfig 를 422 로 막는다. 막는 자리마다의 판정은 위의 시험이 본다."""
    path, user, cluster = FORBIDDEN[0]
    r = _post_kubeconfig(client, _kubeconfig(user, cluster))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_value"
    assert detail["paths"] == [path]
    assert "daegam-deployer" in detail["detail"] and "target: remote" in detail["detail"]
    assert client.get("/clusters").json() == []


def test_inline_kubeconfig_is_accepted_and_patch_checks_the_same(client, fake_secrets):
    r = _post_kubeconfig(client, _kubeconfig())
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    stored = fake_secrets.get(f"cluster-{cid}", namespace="daegam-system")

    bad = yaml.safe_dump(_kubeconfig({"exec": {"command": "id"}}))
    r = client.patch(f"/clusters/{cid}", json={"connection": {"kubeconfig": bad}})
    assert r.status_code == 422 and r.json()["detail"]["paths"] == ["users[0].user.exec"]
    assert fake_secrets.get(f"cluster-{cid}", namespace="daegam-system") == stored


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        pytest.param(ALIAS_BOMB, "kubeconfig expands too far through aliases", id="alias-bomb"),
        pytest.param("just text", "kubeconfig is not a mapping", id="not-a-mapping"),
    ],
)
def test_registering_rejects_what_does_not_open(client, fake_secrets, text, detail):
    """열지 못하는 kubeconfig 는 등록되지 않는다. 오류 글이 틀린 줄 대신 자리만 싣는 것은 저장된
    Secret 의 시험이 본다. 그 줄의 비밀이 preflight 를 거쳐 cluster:read 를 가진 모든 테넌트
    viewer 와 로그로 나가기 때문이다."""
    r = _post_kubeconfig(client, text)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "invalid_yaml", "detail": detail}
    assert SECRET not in r.text
    assert client.get("/clusters").json() == []


def test_stored_secret_with_a_command_is_unreachable():
    """등록 검사 전에 저장된 Secret 이라도 클라이언트를 만들기 전에 멈춘다. exec 는 돌지 않는다."""
    store = _Store(yaml.safe_dump(_kubeconfig({"exec": {"command": "id"}})))
    with patch("daegam.k8s.client.k8s_config.new_client_from_config_dict") as load:
        with pytest.raises(ClusterUnreachable, match=r"users\[0\]\.user\.exec"):
            ClusterClient.from_cluster(_cluster(), store, platform_namespace="daegam-system")
    load.assert_not_called()


def test_stored_secret_that_does_not_open_names_only_the_position():
    """저장된 Secret 의 문법 오류도 자리만 알린다. 이 글이 preflight.missing 과 로그에 실린다."""
    with pytest.raises(ClusterUnreachable) as e:
        ClusterClient.from_cluster(_cluster(), _Store(BROKEN), platform_namespace="daegam-system")
    assert str(e.value) == "kubeconfig is not valid YAML at line 6, column 26"
    assert e.value.__cause__ is None
