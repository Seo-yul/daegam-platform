"""preflight 가 내는 게이트 재료: 테넌트 객체 권한과 오퍼레이터의 사용량 지표 env (P50 2절, G1). 둘
다 ok 에 영향을 주지 않고, 판정하지 못한 재료는 None 이라 저장된 값을 그대로 둔다."""

from __future__ import annotations

import pytest

from daegam.k8s.preflight import (
    RBAC_GROUP,
    SANDBOX_PRIORITY_CLASS,
    SANDBOX_VERBS,
    TENANT_OBJECT_VERBS,
    TENANT_OBJECTS_CLUSTER_ROLE,
    USAGE_METRICS_ENV,
    check_cluster,
    usage_metrics_enabled,
)
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.http import api_client, make_app

OPERATOR = ("apps/v1", "Deployment", "toolhive-system", "toolhive-operator")
GET_ROLE = (RBAC_GROUP, "clusterroles", "get", TENANT_OBJECTS_CLUSTER_ROLE)
BIND_ROLE = (RBAC_GROUP, "clusterroles", "bind", TENANT_OBJECTS_CLUSTER_ROLE)


def _with_operator_env(fake: FakeClusterClient, env: list[dict]) -> FakeClusterClient:
    fake.objects[OPERATOR]["spec"]["template"]["spec"]["containers"][0]["env"] = env
    return fake


def test_tenant_object_rights_need_the_role_and_both_verbs():
    pf, gates = check_cluster(FakeClusterClient())
    assert pf.ok and gates.tenant_object_rights is True
    checked = {(c.group, c.resource, c.verb, c.name): c.allowed for c in pf.rbac}
    for entry in TENANT_OBJECT_VERBS:
        assert checked[entry] is True
    # 옛 RBAC 로 등록한 클러스터: ClusterRole 이 없고 Secret 은 클러스터 전체로 받는다. 그래도 ok
    # 이고 missing 에 없다
    pf, gates = check_cluster(
        FakeClusterClient(tenant_objects_role=False, cluster_wide_secrets=True)
    )
    assert pf.ok and gates.tenant_object_rights is False and pf.missing == []


@pytest.mark.parametrize("denied", [BIND_ROLE, GET_ROLE])
def test_each_verb_is_needed(denied):
    """bind 가 없으면 묶을 수 없고, get 이 없으면 ClusterRole 이 있는지 볼 수 없다. 둘 다 판정한
    거부라 False 이고, Secret 을 클러스터 전체로 받는 옛 RBAC 의 클러스터면 ok 는 그대로다."""
    pf, gates = check_cluster(FakeClusterClient(denied={denied}, cluster_wide_secrets=True))
    assert pf.ok and gates.tenant_object_rights is False and pf.missing == []
    checked = {(c.group, c.resource, c.verb, c.name): c.allowed for c in pf.rbac}
    assert checked[denied] is False


def test_errors_leave_the_rights_undecided():
    """접근 검사나 ClusterRole 읽기가 오류로 끝나면 판정하지 못한 것이다(None). 저장된 값을 그대로
    두게 하려는 것이다."""

    class FlakyReview(FakeClusterClient):
        def can_i(self, verb, resource, group="", *, name=None, namespace=None):
            if name == TENANT_OBJECTS_CLUSTER_ROLE:
                raise RuntimeError("connection reset")
            return super().can_i(verb, resource, group, name=name, namespace=namespace)

    class FlakyRead(FakeClusterClient):
        def get(self, api_version, kind, name, namespace=None):
            if kind == "ClusterRole":
                raise RuntimeError("connection reset")
            return super().get(api_version, kind, name, namespace)

    for fake in (FlakyReview(), FlakyRead()):
        _pf, gates = check_cluster(fake)
        assert gates.tenant_object_rights is None


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ([], True),
        ([{"name": "OTHER", "value": "x"}], True),
        ([{"name": USAGE_METRICS_ENV, "value": "false"}], False),
        ([{"name": USAGE_METRICS_ENV, "value": " FALSE "}], False),
        ([{"name": USAGE_METRICS_ENV, "value": "true"}], True),
        ([{"name": USAGE_METRICS_ENV, "valueFrom": {"configMapKeyRef": {"name": "c"}}}], None),
    ],
)
def test_usage_metrics_follow_the_operator_env(env, want):
    assert usage_metrics_enabled([{"name": "manager", "env": env}]) is want
    pf, gates = check_cluster(_with_operator_env(FakeClusterClient(), env))
    assert gates.operator_read and gates.usage_metrics is want and pf.ok


def test_a_missing_operator_is_read_as_missing():
    """오퍼레이터가 없다는 답도 읽은 것이다. 판도 사용량 지표도 없다고 적는다."""
    pf, gates = check_cluster(FakeClusterClient(operator_image=None))
    assert gates.operator_read and gates.usage_metrics is None and not pf.ok


def test_an_unreachable_cluster_decides_nothing():
    pf, gates = check_cluster(FakeClusterClient(unreachable=True))
    assert not pf.ok and pf.missing[0].startswith("connect:")
    assert gates.tenant_object_rights is None and not gates.operator_read


# -- 샌드박스 권한 (P40 10절) ------------------------------------------------------------------


def test_sandbox_rights_need_the_verbs_the_pod_rule_and_the_priority_class():
    pf, gates = check_cluster(FakeClusterClient())
    assert pf.ok and gates.sandbox_rights is True
    checked = {(c.group, c.resource, c.verb, c.name): c.allowed for c in pf.rbac}
    for entry in SANDBOX_VERBS:
        name = entry[3] if len(entry) > 3 else None
        assert checked[(*entry[:3], name)] is True
    without_priority_class = FakeClusterClient()
    without_priority_class.objects.pop(
        ("scheduling.k8s.io/v1", "PriorityClass", None, SANDBOX_PRIORITY_CLASS)
    )
    for fake in (
        # 이 판의 RBAC 파일을 다시 적용하지 않은 클러스터
        FakeClusterClient(sandbox_rights=False),
        FakeClusterClient(denied={("", "nodes", "list")}),
        FakeClusterClient(
            denied={("scheduling.k8s.io", "priorityclasses", "get", SANDBOX_PRIORITY_CLASS)}
        ),
        FakeClusterClient(tenant_objects_role=False, cluster_wide_secrets=True),
        without_priority_class,
    ):
        pf, gates = check_cluster(fake)
        # 샌드박스 게이트만 닫히고 preflight 의 결과는 그대로다
        assert gates.sandbox_rights is False and pf.ok and pf.missing == []


def test_sandbox_rights_stay_undecided_on_errors():
    class FlakyNodes(FakeClusterClient):
        def can_i(self, verb, resource, group="", *, name=None, namespace=None):
            if resource == "nodes":
                raise RuntimeError("connection reset")
            return super().can_i(verb, resource, group, name=name, namespace=namespace)

    class FlakyPriorityClass(FakeClusterClient):
        def get(self, api_version, kind, name, namespace=None):
            if kind == "PriorityClass":
                raise RuntimeError("connection reset")
            return super().get(api_version, kind, name, namespace)

    for fake in (FlakyNodes(), FlakyPriorityClass()):
        _pf, gates = check_cluster(fake)
        assert gates.sandbox_rights is None


def test_preflight_stores_the_sandbox_rights(settings, fake_clusters, fake_secrets):
    with api_client(make_app(settings, fake_clusters, fake_secrets, home_kubecontext="c")) as c:
        assert c.get("/clusters").json()[0]["gates"]["sandbox_rights"] is True
    fake_clusters.by_name["home"] = FakeClusterClient(sandbox_rights=False)
    with api_client(make_app(settings, fake_clusters, fake_secrets, home_kubecontext="c")) as c:
        home = c.get("/clusters").json()[0]
        assert home["gates"]["sandbox_rights"] is False
        assert home["status"]["preflight"]["ok"] is True
