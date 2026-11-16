from daegam.config.resolve import ProfileRuntime, resolve_effective
from daegam.store.clusters import ClusterImages, ClusterNamespaces, ClusterScheduling
from daegam.store.settings import PlatformSettings
from daegam.store.tenants import Binding, TenantNamespace, TenantSettings
from tests.helpers.consts import ISSUER
from tests.helpers.docs import cluster_doc


def test_platform_defaults_only():
    cluster = cluster_doc()
    eff = resolve_effective(PlatformSettings(), cluster, TenantSettings(tenant="acme"))
    assert eff.images.runtime_image == "daegam-mcp-runtime:0.1.0"
    assert eff.images.agent_image == "daegam-agent-runtime:0.1.0"
    assert eff.namespace.name == "daegam-acme"
    assert eff.namespace.labels == {}
    assert eff.replicas == 1
    assert eff.resources is None
    assert eff.bindings == []
    assert eff.model == "claude-opus-5"
    assert eff.platform_internal_url == ISSUER
    # 결과는 스냅샷이다. 만든 뒤 클러스터 문서가 바뀌어도 그대로다
    cluster.images.runtime_image = "rt:v2"
    assert eff.images.runtime_image == "daegam-mcp-runtime:0.1.0"


def test_cluster_overrides_platform():
    settings = PlatformSettings(
        default_runtime_image="rt:platform", namespace_template="p-{tenant}"
    )
    cluster = cluster_doc(
        images=ClusterImages(
            runtime_image="rt:cluster",
            registry_prefix="registry.local/daegam",
            pull_policy="Always",
            pull_secret="regcred",
        ),
        namespaces=ClusterNamespaces(
            template="c-{tenant}",
            labels={"env": "cluster"},
            quota={"pods": "10"},
            limit_range={"limits": []},
        ),
        scheduling=ClusterScheduling(
            resources={"limits": {"cpu": "1"}}, node_selector={"pool": "a"}
        ),
    )
    eff = resolve_effective(settings, cluster, TenantSettings(tenant="acme"))
    assert eff.images.runtime_image == "registry.local/daegam/rt:cluster"
    assert eff.images.agent_image == "registry.local/daegam/daegam-agent-runtime:0.1.0"
    assert eff.images.pull_policy == "Always"
    assert eff.images.pull_secret == "regcred"
    assert eff.namespace.name == "c-acme"
    assert eff.namespace.labels == {"env": "cluster"}
    assert eff.namespace.quota == {"pods": "10"}
    assert eff.namespace.limit_range == {"limits": []}
    assert eff.resources == {"limits": {"cpu": "1"}}
    assert eff.scheduling.node_selector == {"pool": "a"}


def test_tenant_overrides_cluster():
    cluster = cluster_doc(
        namespaces=ClusterNamespaces(
            template="c-{tenant}", labels={"env": "cluster", "team": "x"}, quota={"pods": "10"}
        ),
    )
    tenant = TenantSettings(
        tenant="acme",
        namespace=TenantNamespace(name="acme-custom", labels={"team": "acme"}),
        quota={"pods": "3"},
        default_bindings=[Binding(data_service_id="ds1", env_prefix="CACHE")],
    )
    eff = resolve_effective(PlatformSettings(), cluster, tenant)
    assert eff.namespace.name == "acme-custom"
    assert eff.namespace.labels == {"env": "cluster", "team": "acme"}  # 테넌트 라벨이 이긴다
    assert eff.namespace.quota == {"pods": "3"}
    assert [b.env_prefix for b in eff.bindings] == ["CACHE"]


def test_profile_overrides_everything():
    settings = PlatformSettings(default_runtime_image="rt:platform")
    cluster = cluster_doc(
        images=ClusterImages(runtime_image="rt:cluster"),
        scheduling=ClusterScheduling(resources={"limits": {"cpu": "1"}}),
    )
    tenant = TenantSettings(
        tenant="acme", default_bindings=[Binding(data_service_id="ds1", env_prefix="CACHE")]
    )
    profile = ProfileRuntime(
        image="rt:profile",
        replicas=3,
        resources={"limits": {"cpu": "2"}},
        bindings=[Binding(data_service_id="ds2", env_prefix="DB")],
    )
    eff = resolve_effective(settings, cluster, tenant, profile)
    assert eff.images.runtime_image == "rt:profile"
    assert eff.replicas == 3
    assert eff.resources == {"limits": {"cpu": "2"}}
    assert [b.env_prefix for b in eff.bindings] == ["DB"]


def test_registry_prefix_not_applied_to_qualified_images():
    cluster = cluster_doc(
        images=ClusterImages(registry_prefix="reg.local/x", runtime_image="ghcr.io/a/b:1")
    )
    eff = resolve_effective(PlatformSettings(), cluster, TenantSettings(tenant="t"))
    assert eff.images.runtime_image == "ghcr.io/a/b:1"
    cluster = cluster_doc(
        images=ClusterImages(registry_prefix="reg.local/x", runtime_image="localhost:5000/b:1")
    )
    eff = resolve_effective(PlatformSettings(), cluster, TenantSettings(tenant="t"))
    assert eff.images.runtime_image == "localhost:5000/b:1"


def test_registry_prefix_keeps_a_pinned_digest():
    """고정한 배포는 `이름:도장@sha256:…` 을 준다(D54, resolve-images.sh). 접두사만 붙고 digest 는
    그대로다."""
    digest = "sha256:" + "a" * 64
    cluster = cluster_doc(
        images=ClusterImages(
            registry_prefix="registry.example/daegam",
            runtime_image=f"daegam-mcp-runtime:abc@{digest}",
            agent_image=f"ghcr.io/a/agent:1@{digest}",
        )
    )
    eff = resolve_effective(PlatformSettings(), cluster, TenantSettings(tenant="t"))
    assert eff.images.runtime_image == f"registry.example/daegam/daegam-mcp-runtime:abc@{digest}"
    assert eff.images.agent_image == f"ghcr.io/a/agent:1@{digest}"
