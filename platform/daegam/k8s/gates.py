"""클러스터 게이트 (P50 2절).

기능마다 클러스터의 ToolHive 판과 플랫폼 권한을 요구한다(`REQUIREMENTS`). 판은 마지막 preflight 가
오퍼레이터 이미지에서 읽은 값이고, 읽지 못했을 때만 관리자가 적어 둔 값
(`Cluster.recorded_toolhive_version`)이다. 판을 모르면 어느 기능의 최소 판도 넘지 못한 것으로 본다.
권한은 `Cluster.gates.tenant_object_rights` 다.

이미 도는 객체는 그 기능을 뺀 대체 형태로 렌더한다. 대체 형태로는 보호가 빠지는 기능(G10)만 새
객체를 422 로 거절하고, 그 본문은 `gate_error` 가 만든다(명세 2절의 표).

테넌트 Secret 을 쓰는 길(P63)은 기능이 아니라 권한 하나를 본다. 어느 길로도 쓸 수 없다고 확인된
클러스터만 쓰기 전에 422 로 거절한다(`require_tenant_secrets`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from daegam.errors import ServiceError
from daegam.k8s.preflight import TENANT_OBJECTS_CLUSTER_ROLE
from daegam.store.auth_reapply import BOUND_SHIFT, STALE_EPOCH, is_bound_mark, rendered_epoch
from daegam.store.clusters import Cluster

__all__ = [
    "Feature",
    "Gate",
    "RenderStamp",
    "Requirement",
    "REQUIREMENTS",
    "cluster_gate",
    "cluster_toolhive_version",
    "gate_error",
    "is_release_version",
    "known_below",
    "pack_epoch",
    "parse_version",
    "passing_features",
    "require_tenant_secrets",
    "tenant_object_rights",
    "tenant_secret_refusal",
    "toolhive_at_least",
    "unpack_epoch",
]

Version = tuple[int, int, int]

# 앞의 v 와 뒤의 접미사(-rc1, +build)를 떼고 세 수만 본다. 이미지 태그는 접미사가 무엇이든 읽는다
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
# 관리자가 적는 판의 꼴. 세 수 뒤에는 -rc1·+build 같은 접미사만 온다
_RELEASE_RE = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


class Feature(StrEnum):
    """게이트를 지나야 렌더되는 기능. 값은 422 본문의 `feature` 다.

    뒤에만 더한다. 렌더 번호는 못 지난 게이트를 이 순서의 자리로 싣고(`services/data_plane_auth.py`
    `render_epoch`), 앞 판의 번호도 오늘의 목록으로 읽기 때문이다(`missed_gates`)."""

    AUDIT = "audit"  # G2 vMCP 감사
    TIMEOUTS = "timeouts"  # G6 operational.timeouts
    AUTHZ_REF = "authz_ref"  # G8 MCPAuthzConfig 참조
    STATIC_BACKENDS = "static_backends"  # G5 outgoingAuth source inline
    ROLELESS_SA = "roleless_sa"  # G10 원격 프록시의 spec.serviceAccount
    TENANT_FENCE = "tenant_fence"  # 내부 MCPServer 의 테넌트 울타리
    MULTI_KEY_SECRETS = "multi_key_secrets"  # spec.secrets 의 한 Secret 여러 키
    SESSION_TLS = "session_tls"  # MCPServer sessionStorage.tls (P61)


@dataclass(frozen=True)
class Requirement:
    version: Version
    rights: bool = False  # ClusterRole daegam-tenant-objects 를 묶을 수 있어야 한다


REQUIREMENTS: dict[Feature, Requirement] = {
    Feature.AUDIT: Requirement((0, 51, 2)),
    Feature.TIMEOUTS: Requirement((0, 51, 2)),
    Feature.AUTHZ_REF: Requirement((0, 51, 2), rights=True),
    Feature.STATIC_BACKENDS: Requirement((0, 51, 2)),
    Feature.ROLELESS_SA: Requirement((0, 51, 2), rights=True),
    Feature.TENANT_FENCE: Requirement((0, 52, 0)),
    Feature.MULTI_KEY_SECRETS: Requirement((0, 52, 0)),
    Feature.SESSION_TLS: Requirement((0, 52, 0)),
}


@dataclass(frozen=True)
class Gate:
    """한 클러스터에서 기능 하나의 판정. needs·has 는 사람이 읽는 글이고 422 본문에 그대로 간다."""

    feature: Feature
    ok: bool
    needs: str
    has: str
    # 지나지 못했을 때의 오류 코드. ok 면 None 이다
    code: str | None = None


def parse_version(value: str | None) -> Version | None:
    """`0.51.2`·`v0.51.10`·`0.52.0-rc1` 같은 판을 세 수로 읽는다. 읽지 못하면 None 이다."""
    m = _VERSION_RE.match((value or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_release_version(value: str) -> bool:
    """관리자가 적을 수 있는 판인가. `0.53.1abc`·`0.53.1.4` 처럼 세 수 뒤에 다른 글자가 붙으면
    아니다."""
    return _RELEASE_RE.fullmatch(value.strip()) is not None


def cluster_toolhive_version(cluster: Cluster) -> str | None:
    """게이트가 보는 판. preflight 가 오퍼레이터 이미지에서 읽은 값이 먼저이고, 읽지 못했을 때만
    관리자가 적은 값이다. 오퍼레이터를 다시 깔아 판이 내려가도 적어 둔 높은 판이 이기지 않는다."""
    preflight = cluster.status.preflight
    if preflight is not None and preflight.toolhive_version:
        return preflight.toolhive_version
    return cluster.recorded_toolhive_version


def toolhive_at_least(cluster: Cluster, version: Version) -> bool:
    """클러스터의 ToolHive 판이 version 이상이라고 알려졌는가. 판을 모르면 거짓이다."""
    found = parse_version(cluster_toolhive_version(cluster))
    return found is not None and found >= version


def tenant_object_rights(cluster: Cluster) -> bool:
    """마지막 점검이 ClusterRole daegam-tenant-objects 를 보았고 그것을 묶을 수 있었는가."""
    return cluster.gates.tenant_object_rights


def _format(version: Version) -> str:
    return ".".join(str(n) for n in version)


# 권한이 필요한 게이트가 요구하는 것. 422 본문의 needed 에 들어간다
TENANT_OBJECTS_RIGHTS = f"the ClusterRole {TENANT_OBJECTS_CLUSTER_ROLE} bound by the platform"


def rights_gate(feature: Feature, needs: str, has: str) -> Gate:
    """ClusterRole daegam-tenant-objects 를 묶을 권한이 없어 지나지 못한 게이트다. has 뒤에 그
    권한이 없다는 말이 붙는다."""
    return Gate(
        feature,
        False,
        needs,
        f"{has} without the {TENANT_OBJECTS_CLUSTER_ROLE} rights",
        code="cluster_rights_missing",
    )


def toolhive_has(cluster: Cluster) -> str:
    """게이트 본문의 has 에 쓰는 클러스터의 ToolHive 판."""
    raw = cluster_toolhive_version(cluster)
    return f"ToolHive {raw}" if raw else "an unknown ToolHive version"


def cluster_gate(cluster: Cluster, feature: Feature) -> Gate:
    """판을 먼저 보고 그다음 권한을 본다. 둘 다 모자라면 판의 오류다."""
    req = REQUIREMENTS[feature]
    needs = f"ToolHive {_format(req.version)}"
    if req.rights:
        needs += f" and {TENANT_OBJECTS_RIGHTS}"
    version_has = toolhive_has(cluster)
    if not toolhive_at_least(cluster, req.version):
        return Gate(feature, False, needs, version_has, code="toolhive_too_old")
    if req.rights and not tenant_object_rights(cluster):
        return rights_gate(feature, needs, version_has)
    return Gate(feature, True, needs, version_has)


def known_below(cluster: Cluster, feature: Feature) -> bool:
    """클러스터의 ToolHive 판이 그 기능의 최소 판보다 낮다고 알려졌는가. 판을 모르면 거짓이다.
    배포 때 정한 렌더를 다시 렌더가 따를지 볼 때 쓴다(모르는 판은 배포 때의 판단을 둔다)."""
    found = parse_version(cluster_toolhive_version(cluster))
    return found is not None and found < REQUIREMENTS[feature].version


def passing_features(cluster: Cluster) -> frozenset[Feature]:
    """이 클러스터에서 게이트를 지나는 기능. 렌더가 기능마다 대체 형태를 고를 때 한 번 계산한다."""
    return frozenset(f for f in Feature if cluster_gate(cluster, f).ok)


@dataclass(frozen=True)
class RenderStamp:
    """문서에 적힌 렌더 번호를 푼 것 (`unpack_epoch`). base 는 종류 번호
    (`services/data_plane_auth.py` `RENDER_EPOCHS` 의 값, 0 은 번호를 들이기 전의 렌더), missed 는
    그 렌더가 지나지 못한 게이트, bound 는 위임 묶임을 렌더했는가(local 모드의 Gateway 렌더, P53
    3.5절)다."""

    base: int
    missed: frozenset[Feature]
    bound: bool


def pack_epoch(base: int, missed: Iterable[Feature] = (), *, bound: bool = False) -> int:
    """렌더 번호 하나. 게이트를 모두 지났으면 base 그대로이고, 하나라도 못 지났으면 base 와 못
    지난 게이트의 조합마다 다른 음수다. 음수는 STALE_EPOCH(-1)보다 작아 그것과 겹치지 않는다. 못
    지난 게이트는 `Feature` 의 순서대로 아래 자리에 싣는다. 그래서 `Feature` 는 뒤에만 더한다.
    bound 면 위임 묶임의 표시(`BOUND_SHIFT`)를 단다."""
    missing = frozenset(missed)
    bits = sum(1 << i for i, f in enumerate(Feature) if f in missing)
    epoch = base if not bits else -(2 + (base << len(Feature)) + bits)
    return epoch - BOUND_SHIFT if bound else epoch


def unpack_epoch(epoch: int) -> RenderStamp | None:
    """적힌 번호가 가리키는 적용한 렌더. 다시 렌더 표시는 그 표시가 바꾼 번호로 읽는다(그 렌더가
    그대로 돌기 때문이다). 옛 코드의 STALE_EPOCH 는 무엇을 바꿨는지 몰라 None 이다. 앞 판의 번호도
    오늘의 `Feature` 목록으로 읽는다."""
    epoch = rendered_epoch(epoch)
    if epoch == STALE_EPOCH:
        return None
    bound = is_bound_mark(epoch)
    if bound:
        epoch += BOUND_SHIFT
    if epoch >= 0:
        return RenderStamp(epoch, frozenset(), bound)
    packed = -epoch - 2
    missed = frozenset(f for i, f in enumerate(Feature) if packed >> i & 1)
    return RenderStamp(packed >> len(Feature), missed, bound)


def gate_error(gate: Gate) -> dict[str, str]:
    """지나지 못한 게이트의 422 본문 재료. `ServiceError(422, **gate_error(gate))` 처럼 쓴다."""
    if gate.ok or gate.code is None:
        raise ValueError(f"gate {gate.feature} passed; there is no error to report")
    return {
        "code": gate.code,
        "detail": f"{gate.feature} needs {gate.needs}; this cluster has {gate.has}",
        "feature": str(gate.feature),
        "needed": gate.needs,
        "found": gate.has,
    }


# 테넌트 Secret 의 422 가 본문에 싣는 이름. 렌더 기능(`Feature`)이 아니라 쓰기 권한 하나다
TENANT_SECRETS = "tenant_secrets"


def tenant_secret_refusal(cluster: Cluster) -> dict[str, str] | None:
    """테넌트 Secret 을 쓰는 길을 막을 422 본문 (P63 2절). 마지막 preflight 가 어느 길로도 쓸 수
    없다고 정했을 때(`SecretRightsCheck.writable` 이 거짓)만이다. 클러스터 전체 Secret 권한이 없고,
    권한 게이트가 닫혔거나 daegam-tenant-objects 에 Secret 동사가 없을 때다. 판정하지 못한 점검과
    저장된 게이트의 기본값은 막지 않는다. 옛 ClusterRole 로 도는 클러스터는 지금처럼 쓰고 경고만
    받는다. 막을 일이 없으면 None 이다."""
    preflight = cluster.status.preflight
    rights = preflight.secret_rights if preflight is not None else None
    if rights is None or rights.writable is not False:
        return None
    needs = f"Secret rights cluster-wide or through the ClusterRole {TENANT_OBJECTS_CLUSTER_ROLE}"
    # 역할을 읽었다면 게이트가 열린 것이다. 역할이 거짓이면 Secret 동사가 없는 것이고, 아니면
    # 게이트가 닫힌 것이다
    has = (
        f"no cluster-wide Secret rights and {TENANT_OBJECTS_CLUSTER_ROLE} without the Secret verbs"
        if rights.role is False
        else f"no cluster-wide Secret rights and no {TENANT_OBJECTS_CLUSTER_ROLE} rights"
    )
    return {
        "code": "cluster_rights_missing",
        "detail": f"writing Secrets in tenant namespaces needs {needs}; this cluster has {has}",
        "feature": TENANT_SECRETS,
        "needed": needs,
        "found": has,
    }


def require_tenant_secrets(cluster: Cluster, error_cls: type[ServiceError]) -> None:
    """테넌트 Secret 을 쓰는 길의 첫 검사. 쓸 수 없는 클러스터면 아무것도 쓰기 전에 422 다."""
    refusal = tenant_secret_refusal(cluster)
    if refusal is not None:
        raise error_cls(422, **refusal)
