"""턴 위임 토큰의 게이트 (P53 3.5절).

게이트는 새 위임이 어디서 토큰을 낼 수 있는지 정한다. 갱신은 그 위임의 대상만 다시 보므로 여기를
거치지 않는다. 판정은 저장된 문서만 읽는다. 연결할 수 없는 클러스터의 Gateway 도 적힌 번호로 센다.

- 판 검사(`version_ok`): 클러스터의 ToolHive 가 0.53 이상이다. `Feature` 에 넣지 않는다. 넣으면
  `render_epoch` 가 못 지난 게이트를 싣는 자리가 바뀌어 대체 형태의 문서가 모두 다시 렌더된다.
- 묶임을 싣는가(`carries_binding`): 그 Gateway 에서 돌 수 있는 렌더가 모두 위임 묶임의 표시를 단
  Gateway 번호 3 이상이다. 표시는 플랫폼을 믿는 모드의 렌더만 단다(`k8s/gates.py` `pack_epoch`).
  다른 모드의 렌더, 번호 2 의 렌더, 옛 코드의 STALE_EPOCH 는 묶임이 없다. 다시 렌더 표시는
  그 표시가 바꾼 번호로, 배포가 도는 문서는 그 배포의 기록(`DeployRender`)으로 읽는다. 기록이
  없거나 다른 배포의 것이면 무엇이 도는지 모르므로 묶임이 없다.
- Gateway 게이트(`judge`): 클러스터마다, 데이터 플레인이 플랫폼을 믿는 모드(local, platform 인
  oidc, P53b 6.1절)의 재적용 틱이 적은 판정이 세 틱 간격보다 젊고 아무것도 닫지 않았으면 열린다.
  배포 상태(`DEPLOY_STATES`)의 모든 Gateway 를 센다. 실패했거나 배포·해제 중인 Gateway 도 마지막
  렌더로 돌 수 있기 때문이다. 다른 모드에서는 Gateway 대상에 토큰을 내지 않으므로 게이트가 닫혀 있고
  아무것도 세지 않는다. 저장된 판정의 `local` 은 플랫폼을 믿는 판정이라는 뜻이다.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from daegam.config import Settings
from daegam.k8s.gates import toolhive_at_least, unpack_epoch
from daegam.services.claims import same_ms
from daegam.store.auth_reapply import (
    KIND_AGENT,
    KIND_GATEWAY,
    STALE_EPOCH,
    ClusterTurnGates,
    HoldReason,
    MongoAuthReapplyStore,
    TurnGateHolder,
    switch_applied,
)
from daegam.store.clusters import Cluster
from daegam.store.exposure import exposure_enabled

__all__ = [
    "AGENT_GROUPS_EPOCH",
    "BINDING_EPOCH",
    "TurnGates",
    "audience_applied",
    "carries_binding",
    "doc_row",
    "judge",
    "live_epochs",
    "lowest_epoch",
    "owner_authz",
    "owner_cedar",
    "renders_from",
    "version_ok",
]

# F0 가 잰 판. 이 아래 클러스터는 모든 소스를 Agent 의 이름으로 부른다
TURN_TOKENS_VERSION = (0, 53, 0)
# 위임 묶임(permit·forbid)을 처음 렌더한 Gateway 번호와, 빈 allowed_groups 를 테넌트 그룹으로 채워
# 렌더한 첫 Agent 번호 (`services/data_plane_auth.py` `RENDER_EPOCHS`의 주석)
BINDING_EPOCH = 3
AGENT_GROUPS_EPOCH = 1
# 배포가 도는 상태. 이 문서는 그 배포의 기록으로 읽는다
IN_FLIGHT_STATES = frozenset({"deploying", "failed"})
# 내리는 상태. 배포를 한 번도 마치지 못한 문서(적힌 번호가 0)는 그 배포의 기록으로 읽는다
LEAVING_STATES = frozenset({"undeploying", "deleting"})
# 판정이 이 리컨실 간격 수에 틱 하나의 예산을 더한 것보다 오래되면 Gateway 소스의 새 위임은 503 이다
# (P53 2절). 판정은 재적용 단계가 문서를 읽기 시작한 때로 적히고(`DataPlaneAuth.run`), 다음 판정은
# 그 단계의 재렌더, 간격의 잠, 다음 틱의 멈춘 문서와 클러스터 점검, 그 틱의 재렌더를 지나서 적힌다.
# 그래서 간격만 세면 예산을 다 쓰는 건강한 틱에서도 판정이 낡아 보인다. 예산은 멈춘 문서 30초,
# 클러스터 점검 20초, 재렌더 30초다(`reconcile.DRIVE_SECONDS_PER_TICK`,
# `CLUSTER_CHECK_SECONDS_PER_TICK`, `data_plane_auth.REAPPLY_SECONDS_PER_TICK`). 그 모듈들이 이
# 모듈을 읽어 여기서는 합만 두고, 시험이 세 값의 합과 같은지 본다
GATE_VERDICT_INTERVALS = 3
TICK_BUDGET_SECONDS = 80.0

# 게이트를 닫은 까닭 (상태 API 의 holders[].reason)
HOLD_NO_BINDING: HoldReason = "no_binding"
HOLD_OWNER_CEDAR: HoldReason = "owner_cedar"
HOLD_EXPOSED_TOOLHIVE_TOO_OLD: HoldReason = "exposed_toolhive_too_old"
HOLD_EXPOSED_AGENT_GROUPS: HoldReason = "exposed_agent_groups"


def version_ok(cluster: Cluster) -> bool:
    """클러스터의 ToolHive 가 턴 위임 토큰을 F0 대로 다루는 판인가. 판을 모르면 아니다."""
    return toolhive_at_least(cluster, TURN_TOKENS_VERSION)


def live_epochs(kind: str, doc: Any) -> list[int]:
    """그 종류의 문서에서 지금 돌 수 있는 렌더의 번호들. draft 는 없다. 렌더 번호는 provision 이
    끝나야 적히므로 배포가 도는 문서는 그 배포의 기록(`DeployRender`)으로 읽는다. 기록이 지금 배포의
    것이면 기록의 번호 둘(적용하는 것과 앞의 것)이다. 내리는 문서는 적힌 번호이고, 배포를 한 번도
    마치지 못한 문서(적힌 번호가 0)만 그 배포의 기록으로 읽는다.

    Gateway 는 배포가 도는 문서에 맞는 기록이 없거나, 배포가 돌거나 내리는 문서의 기록이 다른 배포의
    것이면(되돌린 옛 코드의 재시도) 무엇이 도는지 모르므로 STALE_EPOCH 다. 옛 코드가 자기 렌더(묶임
    없음)를 적용하고 실패하면 이 판의 번호가 그대로 남기 때문이다. Agent 는 그때 적힌 번호다. Agent
    번호 1 이후의 렌더는 모두 allowed_groups 를 채우므로 옛 코드의 렌더도 같은 판정이다 (3.3절)."""
    if doc.state == "draft":
        return []
    record = getattr(doc, "deploy_render", None)
    voided = record is not None and not same_ms(record.since, doc.deploying_since)
    unknown = kind == KIND_GATEWAY
    if voided:
        record = None
    recorded = [] if record is None else [e for e in (record.epoch, record.prior) if e is not None]
    if doc.state in IN_FLIGHT_STATES:
        if record is not None:
            return recorded
        return [STALE_EPOCH] if unknown else [doc.render_epoch]
    if doc.state in LEAVING_STATES:
        if voided and unknown:
            return [STALE_EPOCH]
        if doc.render_epoch == 0 and record is not None:
            return recorded
    return [doc.render_epoch]


def _rank(epoch: int) -> tuple[int, bool]:
    """번호의 순서. 종류 번호가 먼저이고 같으면 묶임의 표시가 없는 쪽이 낮다. 무엇을 바꿨는지 모르는
    STALE_EPOCH 가 가장 낮다."""
    stamp = unpack_epoch(epoch)
    return (-1, False) if stamp is None else (stamp.base, stamp.bound)


def lowest_epoch(epochs: Iterable[int]) -> int | None:
    """가장 낮은 번호(`_rank`). 없으면 None 이다."""
    return min(epochs, key=_rank, default=None)


def renders_from(kind: str, doc: Any, base: int, *, bound: bool = False) -> bool:
    """그 종류의 문서에서 돌 수 있는 렌더가 모두 종류 번호 base 이상인가. bound 면 모두 위임 묶임의
    표시도 달았는가. 돌 렌더가 없으면 참이다."""
    for epoch in live_epochs(kind, doc):
        stamp = unpack_epoch(epoch)
        if stamp is None or stamp.base < base or (bound and not stamp.bound):
            return False
    return True


def carries_binding(doc: Any) -> bool:
    """Gateway 가 위임 묶임을 싣는가 (P53 3.5절)."""
    return renders_from(KIND_GATEWAY, doc, BINDING_EPOCH, bound=True)


def owner_authz(doc: Any) -> Any:
    """Gateway 의 저장된 overrides 에 있는 `spec.incomingAuth.authzConfig`. 없으면 None 이다."""
    spec = (getattr(doc, "overrides", None) or {}).get("spec")
    incoming = spec.get("incomingAuth") if isinstance(spec, dict) else None
    return incoming.get("authzConfig") if isinstance(incoming, dict) else None


def owner_cedar(doc: Any) -> bool:
    """Gateway 의 저장된 overrides 에 플랫폼이 짓지 않는 Cedar(`owner_authz`)가 있는가 (P53
    3.1절). 플랫폼의 forbid 를 갈음한다. Advanced 편집은 그 자리를 잠그므로 지금 저장으로는 생기지
    않고, 있으면 다시 렌더하기와 재배포가 locked_field 로 멈춘다."""
    return owner_authz(doc) is not None


def audience_applied(doc: Any) -> bool:
    """MCP Server 가 자기 audience 로 검증하는 렌더를 적용했는가 (P53 2절)."""
    return switch_applied(doc.audience_switch)


def doc_row(doc: Any) -> dict[str, Any]:
    """상태가 이름을 대는 문서 한 줄이다."""
    return {"id": doc.id, "name": doc.name, "tenant": doc.tenant, "cluster_id": doc.cluster_id}


def _hold_reason(kind: str, doc: Any, *, version: bool) -> HoldReason | None:
    """그 문서가 Gateway 게이트를 닫는 까닭. 닫지 않으면 None 이다. 플랫폼을 믿는 모드의 판정만
    부른다. 노출 Agent 는 그 모드에서만 렌더가 빈 allowed_groups 를 테넌트 그룹으로 채운다 (3.3절,
    P53b 6.8절의 플랫폼 검증기)."""
    if kind == KIND_GATEWAY:
        if owner_cedar(doc):
            return HOLD_OWNER_CEDAR
        if not carries_binding(doc):
            return HOLD_NO_BINDING
        if exposure_enabled(doc) and not version:
            return HOLD_EXPOSED_TOOLHIVE_TOO_OLD
    elif kind == KIND_AGENT and exposure_enabled(doc):
        if not renders_from(KIND_AGENT, doc, AGENT_GROUPS_EPOCH):
            return HOLD_EXPOSED_AGENT_GROUPS
    return None


def judge(
    clusters: list[Cluster],
    docs: Mapping[str, Iterable[tuple[str, Any]]],
    *,
    trusts_platform: bool,
    now: datetime,
) -> list[ClusterTurnGates]:
    """클러스터마다의 판정. docs 는 클러스터 id 마다 배포 상태 문서의 (종류, 문서)다. 노출하지 않은
    Gateway 는 자기 클러스터의 게이트만, 노출한 Gateway 와 노출 Agent 는 모든 클러스터의 게이트를
    닫는다. 노출 host 는 어느 클러스터의 Agent 에서나 닿기 때문이다. 데이터 플레인이 플랫폼을 믿지
    않으면 Gateway 대상에 토큰을 내지 않으므로 게이트를 닫고 아무것도 세지 않는다. 판정의 `local`
    칸(저장된 이름)은 플랫폼을 믿는 판정이라는 뜻이다."""
    versions = {c.id: version_ok(c) for c in clusters}
    everywhere: list[TurnGateHolder] = []
    own: dict[str, list[TurnGateHolder]] = defaultdict(list)
    for cluster in clusters if trusts_platform else ():
        for kind, doc in docs.get(cluster.id, ()):
            reason = _hold_reason(kind, doc, version=versions[cluster.id])
            if reason is not None:
                row = TurnGateHolder(kind=kind, reason=reason, **doc_row(doc))
                (everywhere if exposure_enabled(doc) else own[cluster.id]).append(row)
    return [
        ClusterTurnGates(
            cluster_id=c.id,
            version_ok=versions[c.id],
            gateway_gate_open=trusts_platform and not (own[c.id] or everywhere),
            verdict_at=now,
            local=trusts_platform,
            holders=[*own[c.id], *everywhere],
        )
        for c in clusters
    ]


class TurnGates:
    """재적용 틱이 적은 판정을 읽는다. 위임 발급과 상태 API 가 클러스터마다 묻는다."""

    def __init__(self, store: MongoAuthReapplyStore, settings: Settings):
        self.store = store
        self.settings = settings

    async def cluster(self, cluster_id: str) -> ClusterTurnGates | None:
        """그 클러스터의 마지막 판정. 첫 틱 전이거나 등록에서 빠진 클러스터면 None 이다."""
        return next((g for g in await self.store.turn_gates() if g.cluster_id == cluster_id), None)

    def fresh(self, gates: ClusterTurnGates | None, now: datetime) -> bool:
        """판정이 있고, 데이터 플레인이 지금 믿는 발급자로 내렸고(`local` 은 플랫폼을 믿는 판정이다,
        P53b 6.1절), 세 리컨실 간격과 틱 하나의 예산보다 젊은가. 아니면 리컨실이 멈췄거나 모드나
        발급자가 바뀐 뒤 아직 판정하지 않았다."""
        if gates is None or gates.local != self.settings.data_plane_trusts_platform:
            return False
        max_age = timedelta(
            seconds=GATE_VERDICT_INTERVALS * self.settings.reconcile_interval + TICK_BUDGET_SECONDS
        )
        return now - gates.verdict_at < max_age
