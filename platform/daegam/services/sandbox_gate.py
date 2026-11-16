"""코드 실행 샌드박스의 게이트와 개발용 우회 (P40 8절, D95).

게이트는 네 가지가 다 맞을 때 열린다. 판정이 통과했고 무효로 읽히지 않는다(`unchecked`). 샌드박스가
다른 워크로드와 노드를 함께 쓰면 관리자가 허용했다(`unaccepted`). 플랫폼의 권한과 PriorityClass 가
있다(`rights`). 지금 센 클러스터 용량이 1 이상이다(`capacity`). 꽉 찬 클러스터도 용량은 그대로라
게이트는 열려 있고, 열기가 429 `cluster` 로 거절한다. 닫힌 게이트는 배포와 열기가 409
`sandbox_gate_closed` 와 그 까닭으로 답한다.

개발용 우회(`DAEGAM_SANDBOX_ALLOW_UNCHECKED`)는 home 클러스터에서만 든다. 판정과 허용을 보지 않고,
권한과 용량은 그대로 본다. 그렇게 연 기록은 `unchecked` 이고 자체 점검은 끝나지 않고 알리기만 한다.
"""

from __future__ import annotations

from typing import Literal

from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.templates.sandbox import verdict_void_reason
from daegam.store.clusters import Cluster

GateReason = Literal["unchecked", "unaccepted", "rights", "capacity"]


class SandboxGateClosed(ServiceError):
    """409 `sandbox_gate_closed`. reason 은 `GateReason` 하나다."""

    def __init__(self, reason: GateReason, detail: str | None = None):
        super().__init__(
            409,
            "sandbox_gate_closed",
            detail or f"code execution is unavailable on this cluster: {reason}",
            reason=reason,
        )
        self.reason = reason


def override_active(settings: Settings, cluster: Cluster) -> bool:
    """개발용 우회가 이 클러스터에 드는가. home 클러스터뿐이다."""
    return settings.sandbox_allow_unchecked and cluster.is_home


def verdict_holds(cluster: Cluster, *, image_id: str | None = None) -> bool:
    """판정이 통과했고 무효로 읽히지 않는다. image_id 는 열린 파드가 알린 이미지 id 다."""
    check = cluster.sandbox.check
    return check.passed and verdict_void_reason(cluster, image_id=image_id) is None


def record_holds(
    cluster: Cluster, *, unchecked: bool, image_id: str | None, override: bool
) -> bool:
    """열린 샌드박스를 계속 써도 되는가. 판정이 그 파드의 이미지 id 로도 무효로 읽히지 않거나,
    우회로 연 기록이고 우회가 아직 든다. 열린 기록에 답하는 열기·찾기와 스윕이 같은 규칙을 쓴다."""
    if unchecked and override:
        return True
    return verdict_holds(cluster, image_id=image_id)


def gate_reason(cluster: Cluster, *, capacity: int, override: bool) -> GateReason | None:
    """게이트가 닫힌 까닭. 열렸으면 None 이다. capacity 는 지금 센 클러스터 용량이다
    (`k8s/sandbox_capacity.py` `node_capacity` 의 total). 까닭은 고칠 차례대로 본다. 권한이
    없으면 점검도 돌지 못하고, 점검이 없으면 허용할 것이 없다."""
    if not cluster.gates.sandbox_rights:
        return "rights"
    if not override:
        if not verdict_holds(cluster):
            return "unchecked"
        check = cluster.sandbox.check
        if check.shared_nodes and check.accepted_at is None:
            return "unaccepted"
    if capacity < 1:
        return "capacity"
    return None


def require_open(cluster: Cluster, *, capacity: int, override: bool) -> None:
    """게이트가 닫혔으면 409 `sandbox_gate_closed` 다."""
    reason = gate_reason(cluster, capacity=capacity, override=override)
    if reason is not None:
        raise SandboxGateClosed(reason)
