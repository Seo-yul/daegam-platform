"""home 클러스터 설정의 환경 변수화와 자원 검증 (D54).

DAEGAM_HOME_IMAGES·_RESOURCES·_LIMIT_RANGE·_INGRESS_TLS·_NETWORK_POLICY 는 기동마다 home 에
덮어쓰고, 틀린 값은 기동을 거절한다. 리소스가 준 자원은 클러스터 기본값을 키 단위로 덮는다.
"""

from __future__ import annotations

import json

import pytest

from daegam.config import settings_from_env
from daegam.config.resolve import ProfileRuntime, resolve_effective
from daegam.k8s.resources import (
    InvalidResources,
    merge_resources,
    validate_complete_resources,
    validate_limit_range,
)
from daegam.store.clusters import ClusterScheduling
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import TenantSettings
from tests.helpers.docs import cluster_doc
from tests.helpers.http import api_client, make_app

RES = {"requests": {"cpu": "50m", "memory": "192Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}}
LR = {
    "limits": [
        {
            "type": "Container",
            "defaultRequest": {"cpu": "20m", "memory": "32Mi"},
            "default": {"cpu": "500m", "memory": "256Mi"},
        }
    ]
}
IMAGES = {"registry_prefix": "registry.example/daegam", "runtime_image": "daegam-mcp-runtime:abc"}


# -- 자원 검증 ----------------------------------------------------------------------------


def test_complete_resources_accepts_kubernetes_quantities():
    assert validate_complete_resources(RES, where="x") == RES
    # 1 과 1000m 은 같은 quantity 다
    same = {"requests": {"cpu": "1000m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    assert validate_complete_resources(same, where="x") == same


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"request": RES["requests"], "limits": RES["limits"]}, "exactly the keys"),
        ({"requests": RES["requests"]}, "exactly the keys"),
        ({"requests": {"cpu": "50m"}, "limits": RES["limits"]}, "missing memory"),
        ({"requests": {"cpu": 1, "memory": "1Gi"}, "limits": RES["limits"]}, "quantity string"),
        ({"requests": {"cpu": "50x", "memory": "1Gi"}, "limits": RES["limits"]}, "not a Kube"),
        (
            {"requests": {"cpu": "2", "memory": "192Mi"}, "limits": RES["limits"]},
            "exceeds",
        ),
    ],
)
def test_complete_resources_refuses_what_admission_or_toolhive_would(value, message):
    """오타 키, 빠진 자원, 숫자 quantity(ToolHive CRD 는 문자열만 받는다), requests 가 limits 보다
    큰 값."""
    with pytest.raises(ValueError, match=message):
        validate_complete_resources(value, where="DAEGAM_HOME_RESOURCES")


def test_limit_range_validation():
    assert validate_limit_range(LR, where="x") == LR
    bad_type = {"limits": [{"default": {"cpu": "1"}}]}
    with pytest.raises(ValueError, match="needs a type"):
        validate_limit_range(bad_type, where="x")
    inverted = {
        "limits": [
            {"type": "Container", "defaultRequest": {"cpu": "1"}, "default": {"cpu": "500m"}}
        ]
    }
    with pytest.raises(ValueError, match="exceeds"):
        validate_limit_range(inverted, where="x")
    with pytest.raises(ValueError, match="not a LimitRange field"):
        validate_limit_range({"limits": [{"type": "Container", "defaults": {}}]}, where="x")
    with pytest.raises(ValueError, match="non-empty"):
        validate_limit_range({"limits": []}, where="x")


def test_merge_resources_overrides_per_key():
    """일부만 준 값이 기본값의 나머지 한도를 잃지 않는다."""
    assert merge_resources(RES, {"limits": {"cpu": "2"}}) == {
        "requests": RES["requests"],
        "limits": {"cpu": "2", "memory": "768Mi"},
    }
    assert merge_resources(RES, None) == RES and merge_resources(None, RES) == RES
    assert merge_resources(None, None) is None


def test_resolve_merges_profile_over_cluster_default_and_refuses_inverted():
    cluster = cluster_doc(scheduling=ClusterScheduling(resources=RES))
    eff = resolve_effective(
        PlatformSettings(),
        cluster,
        TenantSettings(tenant="t"),
        ProfileRuntime(resources={"limits": {"memory": "1Gi"}}),
    )
    assert eff.resources == {"requests": RES["requests"], "limits": {"cpu": "1", "memory": "1Gi"}}
    with pytest.raises(InvalidResources) as e:
        resolve_effective(
            PlatformSettings(),
            cluster,
            TenantSettings(tenant="t"),
            ProfileRuntime(resources={"limits": {"memory": "64Mi"}}),
        )
    assert e.value.status == 422 and e.value.code == "invalid_resources"


# -- 환경 변수 --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for key in (
        "DAEGAM_HOME_IMAGES",
        "DAEGAM_HOME_RESOURCES",
        "DAEGAM_HOME_LIMIT_RANGE",
        "DAEGAM_HOME_INGRESS_TLS",
        "DAEGAM_HOME_INGRESS_CLASS",
        "DAEGAM_HOME_INGRESS_ENABLED",
        "DAEGAM_HOME_NETWORK_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DAEGAM_AUTH_MODE", "header")
    return monkeypatch


def test_env_parses_home_settings(env):
    env.setenv("DAEGAM_HOME_IMAGES", json.dumps(IMAGES))
    env.setenv("DAEGAM_HOME_RESOURCES", json.dumps(RES))
    env.setenv("DAEGAM_HOME_LIMIT_RANGE", json.dumps(LR))
    env.setenv("DAEGAM_HOME_INGRESS_TLS", "edge")
    env.setenv("DAEGAM_HOME_NETWORK_POLICY", "Enforced")
    s = settings_from_env()
    assert s.home_images == IMAGES and s.home_resources == RES and s.home_limit_range == LR
    assert s.home_network_policy == "enforced"
    # TLS 기본값만 준 배포가 노출을 켜지 않는다
    assert s.home_ingress == {"tls": "edge"}
    env.setenv("DAEGAM_HOME_INGRESS_CLASS", "traefik")
    assert settings_from_env().home_ingress == {
        "class_name": "traefik",
        "tls": "edge",
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("DAEGAM_HOME_RESOURCES", "{not json", "not valid JSON"),
        ("DAEGAM_HOME_RESOURCES", json.dumps({"request": {}}), "exactly the keys"),
        ("DAEGAM_HOME_LIMIT_RANGE", json.dumps({"limits": [{}]}), "needs a type"),
        ("DAEGAM_HOME_IMAGES", json.dumps({"registry": "x"}), "DAEGAM_HOME_IMAGES"),
        ("DAEGAM_HOME_IMAGES", json.dumps(["x"]), "JSON object"),
        ("DAEGAM_HOME_INGRESS_TLS", "https", "DAEGAM_HOME_INGRESS_TLS"),
        ("DAEGAM_HOME_NETWORK_POLICY", "maybe", "DAEGAM_HOME_NETWORK_POLICY"),
    ],
)
def test_env_refuses_bad_home_settings(env, key, value, message):
    """자원 값이 빠지거나 틀린 파드가 클러스터 규칙에 막혀서야 드러나는 것보다 기동 거절이 낫다."""
    env.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        settings_from_env()


# -- home 문서와 API ------------------------------------------------------------------------


def test_home_images_resources_limit_range_are_reapplied(settings, fake_clusters, fake_secrets):
    """처음 만들 때도($setOnInsert 와 경로가 겹치지 않는다) 다시 뜰 때도 환경 값이 home 에 들어가고,
    환경이 정하지 않은 필드는 화면에서 고친 값이 남는다. 환경이 정한 필드를 다른 값으로 바꾸면 409
    다."""

    def app():
        return make_app(
            settings,
            fake_clusters,
            fake_secrets,
            home_kubecontext="fake-ctx",
            home_ingress={"tls": "edge"},
            home_images=IMAGES,
            home_resources=RES,
            home_limit_range=LR,
            home_network_policy="enforced",
        )

    with api_client(app()) as c:
        home = c.get("/clusters").json()[0]
        assert home["images"] == {
            "registry_prefix": "registry.example/daegam",
            "pull_secret": None,
            "pull_policy": "IfNotPresent",
            "runtime_image": "daegam-mcp-runtime:abc",
            "agent_image": None,
        }
        assert home["scheduling"]["resources"] == RES
        assert home["namespaces"]["limit_range"] == LR
        assert home["ingress"]["tls"] == "edge" and home["ingress"]["enabled"] is False
        assert home["env_managed"] == [
            "images.registry_prefix",
            "images.runtime_image",
            "ingress.tls",
            "namespaces.limit_range",
            "platform_internal_url",
            "scheduling.resources",
        ]
        # 실측 결과를 적은 값이 탐지를 이긴다. 못 박는 것은 판정뿐이고 컨트롤러는 탐지한 것이다(T75)
        pf = home["status"]["preflight"]
        assert pf["network_policy"] == "enforced"
        assert pf["policy_controller"] is None
        # NetworkPolicy 경고는 없다
        assert home["warnings"] == []
        # 환경이 정하지 않은 pull_secret 은 카드 전체를 보내도 고칠 수 있다
        r = c.patch(
            f"/clusters/{home['id']}", json={"images": {**home["images"], "pull_secret": "rc"}}
        )
        assert r.status_code == 200, r.text
        for body, field in (
            ({"images": {**home["images"], "registry_prefix": "x/y"}}, "images.registry_prefix"),
            ({"scheduling": {**home["scheduling"], "resources": None}}, "scheduling.resources"),
            ({"namespaces": {**home["namespaces"], "limit_range": None}}, "namespaces.limit_range"),
            ({"platform_internal_url": "http://elsewhere:9000"}, "platform_internal_url"),
        ):
            r = c.patch(f"/clusters/{home['id']}", json=body)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["fields"] == [field]
    with api_client(app()) as c:
        home = c.get("/clusters").json()[0]
        assert home["images"]["pull_secret"] == "rc"
        assert home["images"]["registry_prefix"] == "registry.example/daegam"
        assert home["scheduling"]["resources"] == RES


def test_a_pinned_home_does_not_make_its_controller_known(settings, fake_clusters, fake_secrets):
    """DAEGAM_HOME_NETWORK_POLICY 가 못 박는 것은 집행 판정뿐이다 (T75). 탐지한 컨트롤러가 없으면
    메타데이터 차단을 켜도 egress 정책을 두지 않고 까닭을 경고한다."""
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        home_kubecontext="fake-ctx",
        home_network_policy="enforced",
        home_block_metadata=True,
    )
    with api_client(app) as c:
        home = c.get("/clusters").json()[0]
        assert home["block_metadata"] is True and "block_metadata" in home["env_managed"]
        assert home["status"]["preflight"]["network_policy"] == "enforced"
        assert [w for w in home["warnings"] if w.startswith("metadata_block_unsupported")]
