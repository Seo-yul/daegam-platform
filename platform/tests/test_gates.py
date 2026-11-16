"""클러스터 게이트 (P50 2절): 판은 preflight 가 읽은 값이 먼저이고 읽지 못했을 때만 관리자가 적은
값이다. 판을 모르면 어느 기능의 최소 판도 넘지 못한 것으로 보고, 권한이 필요한 기능은 클러스터
문서의 gates.tenant_object_rights 를 본다."""

from __future__ import annotations

import pytest

from daegam.errors import ServiceError
from daegam.k8s.gates import (
    REQUIREMENTS,
    Feature,
    cluster_gate,
    cluster_toolhive_version,
    gate_error,
    is_release_version,
    parse_version,
    passing_features,
    tenant_object_rights,
)
from daegam.k8s.preflight import TENANT_OBJECTS_CLUSTER_ROLE
from daegam.store.clusters import ClusterGates, ClusterStatus, Preflight
from tests.helpers.docs import cluster_doc


def _cluster(version: str | None = "0.51.2", *, rights: bool = True, recorded: str | None = None):
    preflight = Preflight(ok=True, toolhive_version=version)
    return cluster_doc(
        "edge",
        status=ClusterStatus(preflight=preflight),
        gates=ClusterGates(tenant_object_rights=rights),
        recorded_toolhive_version=recorded,
    )


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0.51.2", (0, 51, 2)),
        ("v0.51.10", (0, 51, 10)),
        ("0.52.0-rc1", (0, 52, 0)),
        ("1.2.3+build.7", (1, 2, 3)),
        (" 0.53.1 ", (0, 53, 1)),
        ("latest", None),
        ("0.51", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(raw, want):
    assert parse_version(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0.53.1", True),
        ("v0.53.1", True),
        ("0.52.0-rc1", True),
        ("0.53.1+build.2", True),
        (" 0.53.1 ", True),
        ("0.53.1abc", False),
        ("0.53.1.4", False),
        ("0.53", False),
        ("latest", False),
    ],
)
def test_a_recorded_version_must_be_a_release(raw, want):
    assert is_release_version(raw) is want


def test_versions_compare_by_number_not_by_text():
    # 글자로 견주면 0.51.10 이 0.51.2 보다 앞이다
    assert parse_version("0.51.10") > parse_version("0.51.2")
    assert cluster_gate(_cluster("0.51.10"), Feature.AUDIT).ok


def test_each_feature_has_a_requirement():
    assert set(REQUIREMENTS) == set(Feature)
    assert {f for f, r in REQUIREMENTS.items() if r.rights} == {
        Feature.AUTHZ_REF,
        Feature.ROLELESS_SA,
    }
    assert {f for f, r in REQUIREMENTS.items() if r.version == (0, 52, 0)} == {
        Feature.TENANT_FENCE,
        Feature.MULTI_KEY_SECRETS,
        Feature.SESSION_TLS,
    }


def test_recorded_version_fills_in_only_when_preflight_read_none():
    """오퍼레이터 이미지를 digest 로만 핀한 클러스터는 preflight 가 판을 읽지 못한다. 관리자가 적은
    판이 그 자리를 채운다. preflight 가 판을 읽었으면 그것이 이긴다. 오퍼레이터를 낮은 판으로 다시
    깔았는데 적어 둔 높은 판이 0.52 기능을 통과시키면 옛 CRD 가 그 필드를 버리기 때문이다."""
    pinned = _cluster(None, recorded="0.53.0")
    assert cluster_toolhive_version(pinned) == "0.53.0"
    assert cluster_gate(pinned, Feature.TENANT_FENCE).ok
    reinstalled = _cluster("0.51.2", recorded="0.53.1")
    assert cluster_toolhive_version(reinstalled) == "0.51.2"
    assert not cluster_gate(reinstalled, Feature.TENANT_FENCE).ok


def test_unknown_version_meets_no_minimum():
    for cluster in (_cluster(None), _cluster("latest"), cluster_doc("edge")):
        gate = cluster_gate(cluster, Feature.AUDIT)
        assert not gate.ok and gate.code == "toolhive_too_old"
    assert cluster_gate(_cluster(None), Feature.AUDIT).has == "an unknown ToolHive version"
    assert cluster_gate(_cluster("latest"), Feature.AUDIT).has == "ToolHive latest"


def test_older_version_fails_with_toolhive_too_old():
    gate = cluster_gate(_cluster("0.51.2"), Feature.MULTI_KEY_SECRETS)
    assert not gate.ok and gate.code == "toolhive_too_old"
    assert gate.needs == "ToolHive 0.52.0" and gate.has == "ToolHive 0.51.2"
    assert cluster_gate(_cluster("0.52.0"), Feature.MULTI_KEY_SECRETS).ok


def test_rights_gate_follows_the_stored_gates():
    assert tenant_object_rights(_cluster(rights=True))
    assert not tenant_object_rights(_cluster(rights=False))
    # 점검한 적이 없는 클러스터는 권한도 없다고 본다
    assert not tenant_object_rights(cluster_doc("edge"))
    gate = cluster_gate(_cluster(rights=False), Feature.AUTHZ_REF)
    assert not gate.ok and gate.code == "cluster_rights_missing"
    assert TENANT_OBJECTS_CLUSTER_ROLE in gate.needs and "without" in gate.has
    # 권한이 필요 없는 기능은 권한을 보지 않는다
    assert cluster_gate(_cluster(rights=False), Feature.STATIC_BACKENDS).ok
    # 판과 권한이 함께 모자라면 판의 오류다
    both = cluster_gate(_cluster("0.50.0", rights=False), Feature.ROLELESS_SA)
    assert both.code == "toolhive_too_old"


def test_passing_features_are_those_whose_gate_passes():
    """렌더가 한 번 계산하는 기능 집합. 0.51.2 에 권한이 없으면 판만 보는 기능만 지난다."""
    without_rights = passing_features(_cluster("0.51.2", rights=False))
    assert without_rights == {Feature.AUDIT, Feature.TIMEOUTS, Feature.STATIC_BACKENDS}
    assert passing_features(_cluster("0.52.0")) == set(Feature)
    assert passing_features(_cluster(None)) == frozenset()


def test_gate_error_is_a_service_error_body():
    gate = cluster_gate(_cluster(rights=False), Feature.ROLELESS_SA)
    body = gate_error(gate)
    assert body == {
        "code": "cluster_rights_missing",
        "detail": f"roleless_sa needs {gate.needs}; this cluster has {gate.has}",
        "feature": "roleless_sa",
        "needed": gate.needs,
        "found": gate.has,
    }
    err = ServiceError(422, **body)
    assert err.to_dict()["code"] == "cluster_rights_missing"
    assert err.to_dict()["feature"] == "roleless_sa"
    with pytest.raises(ValueError):
        gate_error(cluster_gate(_cluster(), Feature.AUDIT))
