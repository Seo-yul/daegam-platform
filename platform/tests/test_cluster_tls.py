"""token 모드 클러스터는 CA 없이 붙지 않는다 (api/clusters.py, k8s/client.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from daegam.k8s.client import ClusterClient, ClusterUnreachable
from tests.helpers.clusters import post_cluster
from tests.helpers.docs import cluster_doc
from tests.helpers.http import detail_code


def _post_token_cluster(client, **connection):
    """token 모드 클러스터 edge-tls 를 등록한 응답. connection 이 접속 필드를 더한다."""
    token = {"api_url": "https://10.0.0.2:6443", "sa_token": "T", **connection}
    return post_cluster(client, "edge-tls", connection=token)


def test_registering_a_token_cluster_needs_a_ca_bundle(client, fake_secrets):
    """ca_bundle 이 없으면 검증을 끄는 길밖에 없고, 그러면 API 서버 자리에 선 누구에게나 SA 토큰을
    준다. 등록에서 막는다. 받은 토큰과 CA 는 플랫폼 네임스페이스 Secret 에만 있고 응답에 없다."""
    r = _post_token_cluster(client)
    assert r.status_code == 422, r.text
    r = _post_token_cluster(client, ca_bundle="   ")
    assert detail_code(r) == (422, "invalid_value")
    ca = "-----BEGIN CERTIFICATE-----\nx\n"
    r = _post_token_cluster(client, ca_bundle=ca)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["connection"]["mode"] == "token"
    assert c["connection"]["api_url"] == "https://10.0.0.2:6443"
    secret = fake_secrets.get(c["connection"]["secret_name"], namespace="daegam-system")
    assert secret == {"token": "T", "ca_bundle": ca}
    assert "sa_token" not in str(client.get(f"/clusters/{c['id']}").json())


def test_client_never_skips_tls_verification():
    """손으로 만든 Secret 에 ca_bundle 이 빠져 있어도 verify_ssl=False 로 물러나지 않는다. 그
    ClusterUnreachable 은 다시 감싸지 않고 그대로 나간다."""

    class _Store:
        def get(self, name, *, namespace):
            return {"token": "t"}

    cluster = cluster_doc("edge", mode="token", api_url="https://edge.example", secret_name="s")
    with patch("daegam.k8s.client.DynamicClient") as dynamic:
        with pytest.raises(ClusterUnreachable, match="^token mode needs 'ca_bundle'"):
            ClusterClient.from_cluster(cluster, _Store(), platform_namespace="daegam-system")
    dynamic.assert_not_called()
