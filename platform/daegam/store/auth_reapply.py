"""인증 재적용의 기록 (P32 8절, P50 1절). 소비자 문서의 다섯 칸과 `auth_reapply` 컬렉션이다.

소비자 문서(Gateway·MCP Server·Agent·허브 항목·공유 복제본 기록)는 `ConsumerAuthState` 의 다섯 칸을
함께 갖는다. 판은 두 절반이다. `auth_rev` 는 렌더한 인증 입력의 판(공유 복제본 기록이면 렌더한 원본
정의의 판)이고 `render_epoch` 는 렌더한 종류의 렌더 번호다. 나머지 셋은 재적용 실패의 기록이다. 판의
계산과 재적용은 `services/data_plane_auth.py` 가 한다. 클러스터와 소비자가 함께 쓰는 재시도 간격과,
인증 재적용과 서비스 토큰 스윕이 함께 보는 살아 있는 상태도 여기 둔다.

`auth_reapply` 컬렉션에는 문서가 세 꼴로 산다. 틱의 요약 `summary` 하나, 클러스터마다의 드리프트
상태 `cluster:<id>`, 클러스터마다의 턴 위임 게이트 판정을 모은 `turn_gates` 하나다. 리스가 레플리카
사이를 옮겨 다녀도 Mongo 의 문서가 답이므로 상태 API 와 위임 발급이 이것을 그대로 읽는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import UtcDatetime

# 재적용이 다루는 문서의 종류. 세 인스턴스 서비스와 허브 항목 재적용의 `auth_kind` 이고 상태 API
# 의 `kind` 다. 이름 계획(`services/gateway_plan.py`)처럼 재적용 서비스를 읽지 않는 모듈도 쓰므로
# 이 저장소 모듈에 둔다
KIND_GATEWAY = "gateway"
KIND_MCP_SERVER = "mcp_server"
KIND_AGENT = "agent"
KIND_HUB_ITEM = "hub_item"
KIND_CLONE = "shared_clone"

AUTH_REAPPLY = "auth_reapply"
SUMMARY_ID = "summary"
CLUSTER_PREFIX = "cluster:"
TURN_GATES_ID = "turn_gates"

# 어느 종류의 렌더 번호와도 다른 값. 문서에 적으면 다음 틱이 그 문서를 다시 렌더한다. 이 판의
# 코드는 인스턴스 문서에 이 값 대신 바꾼 번호를 담는 표시(`stale_mark`)를 쓰고, 공유 복제본 기록만
# 이 값을 그대로 쓴다
STALE_EPOCH = -1
# 바꾼 번호를 담는 표시의 범위 (P53 3.5절). 게이트를 빠뜨린 번호(`render_epoch` 의 음수)보다 훨씬
# 아래라 겹치지 않는다. 표시는 바꾼 번호에서 STALE_MARK_SHIFT 를 뺀 값이고 [LOW, HIGH) 에 든다
STALE_MARK_HIGH = -(1 << 40)
STALE_MARK_LOW = -(3 << 40)
STALE_MARK_SHIFT = 2 << 40
# 위임 묶임(permit·forbid)을 렌더한 Gateway 번호의 표시 (P53 3.5절). local 모드의 Gateway 렌더만
# 단다. 번호에서 BOUND_SHIFT 를 뺀 값이고 [LOW, HIGH) 에 든다. 다시 렌더 표시를 붙여도 표시의 범위
# 안으로 옮겨 갈 뿐이라 함께 읽힌다(`k8s/gates.py` `unpack_epoch`). 이 표시가 없는 번호는 옛 코드의
# 것이든 다른 모드의 것이든 묶임이 없는 렌더다
BOUND_MARK_HIGH = -(3 << 36)
BOUND_MARK_LOW = -(5 << 36)
BOUND_SHIFT = 1 << 38


def is_stale_mark(epoch: int) -> bool:
    """바꾼 번호를 담는 표시인가."""
    return STALE_MARK_LOW <= epoch < STALE_MARK_HIGH


def is_stale_epoch(epoch: int) -> bool:
    """다시 렌더하라는 표시인가. 옛 코드가 쓰는 STALE_EPOCH 와 이 판의 표시 둘 다다."""
    return epoch == STALE_EPOCH or is_stale_mark(epoch)


def stale_mark(epoch: int) -> int:
    """epoch 를 바꾸는 표시. 이미 표시면 그대로다. 마지막으로 적용한 렌더는 그대로 돌기 때문이다."""
    return epoch if is_stale_mark(epoch) else epoch - STALE_MARK_SHIFT


def is_bound_mark(epoch: int) -> bool:
    """위임 묶임을 렌더한 번호의 표시인가. 다시 렌더 표시를 먼저 푼 번호(`rendered_epoch`)를
    본다."""
    return BOUND_MARK_LOW <= epoch < BOUND_MARK_HIGH


def rendered_epoch(epoch: int) -> int:
    """문서에 적힌 번호가 가리키는 적용한 렌더의 번호. 표시면 그 표시가 바꾼 번호이고, 아니면
    그대로다. 옛 코드의 STALE_EPOCH 는 바꾼 번호를 모르므로 그대로 STALE_EPOCH 다."""
    return epoch + STALE_MARK_SHIFT if is_stale_mark(epoch) else epoch


def stale_mark_expr() -> dict[str, Any]:
    """`stale_mark` 의 집계 식. 표시를 저장된 번호에서 같은 조건부 갱신 안에서 만들어, 읽은 뒤 다른
    레플리카가 렌더했어도 그 렌더의 번호를 담는다. 번호가 없는 문서는 0 으로 읽는다."""
    epoch = {"$ifNull": ["$render_epoch", 0]}
    marked = {"$and": [{"$gte": [epoch, STALE_MARK_LOW]}, {"$lt": [epoch, STALE_MARK_HIGH]}]}
    return {"$cond": [marked, epoch, {"$subtract": [epoch, STALE_MARK_SHIFT]}]}


# 살아 있는 소비자의 상태. 리소스 셋이 다 합쳐져 살아 있는 상태는 `deployed` 하나다 (D42).
# 배포 경로가 도는 중(deploying)이거나 내리는 중인 문서는 인증 재적용과 서비스 토큰 스윕이 손대지
# 않는다. 그 사이에 객체를 적용하면 배포 경로 밖에서 객체가 생긴다. deploying·failed 는 리컨실의
# provision 이 다시 배포하며 판을 적는다
LIVE_STATES: frozenset[str] = frozenset({"deployed"})
# 실패한 클러스터·소비자의 재시도 간격. 실패할 때마다 두 배이고 60분에서 멈춘다. 지수의 상한은
# 5분 × 2^5 가 이미 60분을 넘으므로 그 위로 올릴 까닭이 없다
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_MAX = timedelta(minutes=60)
BACKOFF_MAX_DOUBLINGS = 5


def next_retry(now: datetime, failures: int) -> tuple[datetime, int]:
    """앞서 failures 번 실패한 뒤 한 번 더 실패했을 때의 (재시도 시각, 실패 수). 첫 실패는 5분
    뒤다."""
    delay = min(BACKOFF_BASE * 2 ** min(failures, BACKOFF_MAX_DOUBLINGS), BACKOFF_MAX)
    return now + delay, failures + 1


def _waiting(retry_at: datetime | None, now: datetime) -> bool:
    return retry_at is not None and retry_at > now


class AuthBlocked(BaseModel):
    """요청 오류로 막힌 재적용. 기대 판의 두 절반(`rev`·`epoch`) 가운데 하나가 바뀌거나 그 문서가
    바뀔 때까지 건너뛴다. `detail` 은 상태 API 가 보이는 이유다(잠긴 overrides 경로 등).

    `spec_change` 는 막은 시도가 본 문서의 `last_spec_change` 다. Advanced 편집 저장과 재배포가 그
    값을 바꾸므로 값이 다르면 막은 뒤 문서가 바뀐 것이고 다시 시도한다. 그래서 틱이 옛 overrides 로
    실패해 저장보다 늦게 막힘을 적어도 다음 틱이 한 번 더 시도한다. 저장은 막힘을 비우기도 한다
    (`BaseInstanceService._save_overrides`). 이 칸이 없는 기록은 한 번 더 시도한 뒤 다시 적힌다."""

    # 인증 절반. 인증 소비자가 아닌 문서(노출하지 않은 MCP Server 등)는 빈 문자열이다. 이전
    # 릴리스의 모델이 문자열만 읽으므로 None 으로 적지 않는다
    rev: str
    code: str
    spec_change: UtcDatetime | None = None
    epoch: int = 0
    detail: str | None = None


class ConsumerAuthState(BaseModel):
    """소비자 문서가 함께 갖는 재적용 칸. 세 인스턴스 모델과 허브 항목, 공유 복제본 기록이
    상속한다."""

    auth_rev: str | None = None
    # 렌더한 종류의 렌더 번호 (P50 1절). 번호를 들이기 전 문서는 0 이라 번호 0 과 같다
    render_epoch: int = 0
    auth_retry_at: UtcDatetime | None = None
    auth_failures: int = 0
    auth_blocked: AuthBlocked | None = None

    def mark_rendered(self, rev: str | None, epoch: int) -> None:
        """지금 판으로 렌더했다. 실패 기록을 비운다."""
        self.auth_rev = rev
        self.render_epoch = epoch
        self.auth_retry_at = None
        self.auth_failures = 0
        self.auth_blocked = None

    def waiting(self, now: datetime) -> bool:
        return _waiting(self.auth_retry_at, now)

    def backoff(self, now: datetime) -> None:
        """재적용 실패 한 번을 적고 다음 시도를 미룬다."""
        self.auth_retry_at, self.auth_failures = next_retry(now, self.auth_failures)


#: 소비자 문서의 배포 쪽 저장 단위에 더하는 칸 (D42 의 DEPLOYMENT_FIELDS)
AUTH_FIELDS: frozenset[str] = frozenset(ConsumerAuthState.model_fields)

# 서버가 자기 audience 로 옮겨 간 까닭. 이 판은 처음부터 자기 audience 로 만든 서버에만 적는다
# (new_server). 그 서버의 토큰을 든 옛 소비자가 없기 때문이다. 2.0.0-rc1 은 소비자를 기다려 옮긴
# 기록에 consumers_ready·seven_days 를 적으므로 그 값도 읽는다
AudienceSwitchReason = Literal["consumers_ready", "seven_days", "new_server"]


class AudienceSwitch(BaseModel):
    """내부 서버(MCP Server, 허브 `image` 항목, 공유 복제본 기록)가 테넌트 audience 를 놓고 자기
    audience 로 검증하는 기록 (P50 9절, G9 2단계). `switched_at` 이 있으면 그 서버의 렌더는
    `daegam-internal:<tenant>:<서버 id>` 만 받는다. 서버를 만들 때 적는다(`new_server_switch`).
    다른 릴리스가 적은 키(2.0.0-rc1 의 `pending_since`·`seen_at`·`waiting` 등)도 읽고 그대로 두도록
    모르는 키를 받는다."""

    model_config = ConfigDict(extra="allow")

    switched_at: UtcDatetime | None = None
    reason: AudienceSwitchReason | None = None
    # 자기 audience 로 검증하는 렌더가 처음 적용된 것을 재적용 틱이 본 때 (P53 2절). 그 뒤의 다른
    # 인증 입력 변경(Redis CA 회전 등)에도 그대로 둔다. 턴 위임 토큰은 이것이 있는 서버에만 간다
    applied_at: UtcDatetime | None = None


def switch_applied(switch: AudienceSwitch | None) -> bool:
    """자기 audience 로 검증하는 렌더가 적용됐는가 (P53 2절)."""
    return switch is not None and switch.applied_at is not None


def own_audience(switch: AudienceSwitch | None) -> bool:
    """서버가 자기 audience 로 옮겨 갔는가."""
    return switch is not None and switch.switched_at is not None


def switched_server_id(switch: AudienceSwitch | None, server_id: str) -> str | None:
    """렌더가 검증할 audience 의 서버 id. 자기 audience 로 옮겨 간 서버면 server_id 이고, 아니면
    None 이라 테넌트 audience 로 렌더한다."""
    return server_id if own_audience(switch) else None


def new_server_switch(now: datetime) -> AudienceSwitch:
    """새 서버의 기록. 그 서버의 토큰을 든 소비자는 모두 서버 뒤에 생기고 두 audience 를 실으므로
    처음부터 자기 audience 로 검증한다."""
    return AudienceSwitch(switched_at=now, reason="new_server")


def new_image_switch(source_kind: str, now: datetime) -> AudienceSwitch | None:
    """새 허브 항목이나 복제본 기록의 audience 기록. 서비스 토큰을 검증하는 것은 `image` 뿐이라
    그것만 처음부터 옮겨 가 있고, 그 밖의 종류는 기록이 없다."""
    return new_server_switch(now) if source_kind == "image" else None


class DeployRender(BaseModel):
    """배포가 도는 동안(deploying·failed) 그 문서에서 돌 수 있는 렌더의 번호 (P53 3.5절). 렌더
    번호는 provision 이 끝나야 적히므로 그 사이의 문서는 이것으로 읽는다. since 는 이 기록을 적은
    배포의 `deploying_since` 이고 문서의 것과 다르면(옛 코드의 재시도 등) 이 기록은 없는 것이다.
    epoch 는 이 배포가 적용하는 렌더의 번호이고 provision 이 렌더 앞에 적는다. prior 는 이 배포
    앞에 적용돼 아직 돌 수 있는 렌더 가운데 가장 낮은 번호이고, 앞 렌더가 없으면(draft 에서 배포)
    None 이다. Gateway 와 Agent 만 적는다."""

    model_config = ConfigDict(extra="ignore")

    since: UtcDatetime
    epoch: int | None = None
    prior: int | None = None


# 턴 위임의 Gateway 게이트를 닫은 까닭 (P53 3.5절). 묶임을 싣지 않은 렌더로 돌 수 있다, 플랫폼이
# 짓지 않는 Cedar 로 돈다, 노출한 Gateway 의 클러스터가 ToolHive 0.53 아래다, 노출 Agent 의 렌더가
# allowed_groups 를 채우기 전의 것이다
HoldReason = Literal[
    "no_binding", "owner_cedar", "exposed_toolhive_too_old", "exposed_agent_groups"
]


class TurnGateHolder(BaseModel):
    """Gateway 게이트를 닫은 문서 하나. kind 는 재적용 종류 이름(`gateway`·`agent`)이다."""

    model_config = ConfigDict(extra="ignore")

    kind: str
    id: str
    name: str
    tenant: str
    cluster_id: str
    reason: HoldReason


class ClusterTurnGates(BaseModel):
    """클러스터 하나의 턴 위임 게이트 판정 (P53 3.5절). 재적용 틱이 모든 클러스터의 것을 함께
    적는다. version_ok 는 ToolHive 판 검사, gateway_gate_open 은 Gateway 게이트가 열렸는가,
    holders 는 그 게이트를 닫은 문서다. local 은 local 모드에서 판정했는가다. 다른 모드의 판정은
    묶임을 보지 않으므로 모드가 바뀐 뒤에는 쓰지 않는다(`services/turn_gates.py`
    `TurnGates.fresh`)."""

    model_config = ConfigDict(extra="ignore")

    cluster_id: str
    version_ok: bool
    gateway_gate_open: bool
    verdict_at: UtcDatetime
    local: bool = False
    holders: list[TurnGateHolder] = Field(default_factory=list)


class NamespaceRetry(BaseModel):
    """네임스페이스 하나의 보장 실패 기록."""

    model_config = ConfigDict(extra="ignore")

    retry_at: UtcDatetime
    failures: int


class ClusterAuthState(BaseModel):
    """클러스터 하나의 네임스페이스 객체 드리프트. 스윕이 끝날 때마다 통째로 다시 쓴다."""

    model_config = ConfigDict(extra="ignore")

    cluster_id: str
    drifted: list[str] = Field(default_factory=list)  # 다시 보장하지 못한 네임스페이스
    unadopted: list[str] = Field(default_factory=list)  # 입양하지 않은 네임스페이스
    # 테넌트 객체(RoleBinding·ServiceAccount)를 아직 맞추지 못한 네임스페이스. 문서를 막지 않는다
    objects_pending: list[str] = Field(default_factory=list)
    # 원격 프록시의 ServiceAccount 가 Secret 이나 ConfigMap 을 읽을 수 있는 네임스페이스 (P50
    # 10절). 테넌트 객체를 맞출 때 LocalSubjectAccessReview 로 묻는다. 있으면 누군가 그 계정에 Role
    # 을 묶었다
    proxy_account_reads: list[str] = Field(default_factory=list)
    # 마지막 스윕이 클러스터 자체에서 멈췄다(클라이언트·목록 실패). 클러스터 단위 백오프는 이때만
    # 걸고, 네임스페이스 하나의 실패는 그 네임스페이스만 미룬다(`namespace_retries`)
    unreachable: bool = False
    namespace_retries: dict[str, NamespaceRetry] = Field(default_factory=dict)
    retry_at: UtcDatetime | None = None
    failures: int = 0
    last_error: str | None = None

    def admits(self, namespace: str) -> bool:
        """이 네임스페이스의 문서를 다시 렌더해도 되는가 (P50 1절). MCPOIDCConfig 가 먼저 맞춰져야
        하므로 이번 틱에 그 설정을 맞추지 못한 네임스페이스의 문서는 기다리고, 다른 네임스페이스는
        간다. 클러스터에 연결하지 못했으면 이 클러스터의 문서는 하나도 다시 렌더하지 않는다. 테넌트
        객체가 아직 맞춰지지 않은 네임스페이스(`objects_pending`)는 막지 않는다."""
        return not self.unreachable and namespace not in self.drifted

    def namespace_waiting(self, namespace: str, now: datetime) -> bool:
        retry = self.namespace_retries.get(namespace)
        return retry is not None and _waiting(retry.retry_at, now)

    def namespace_backoff(self, namespace: str, now: datetime) -> None:
        """네임스페이스 하나의 실패를 적고 그 네임스페이스만 다음 보장을 미룬다."""
        retry = self.namespace_retries.get(namespace)
        retry_at, failures = next_retry(now, retry.failures if retry else 0)
        self.namespace_retries[namespace] = NamespaceRetry(retry_at=retry_at, failures=failures)

    def namespace_settled(self, namespace: str) -> None:
        self.namespace_retries.pop(namespace, None)

    def waiting(self, now: datetime) -> bool:
        return _waiting(self.retry_at, now)

    def backing_off(self, now: datetime) -> bool:
        """연결하지 못해 미뤄 둔 클러스터인가. 그동안은 이 클러스터를 읽지 않는다."""
        return self.unreachable and self.waiting(now)

    def backoff(self, now: datetime) -> None:
        """스윕 실패 한 번을 적고 다음 스윕을 미룬다."""
        self.retry_at, self.failures = next_retry(now, self.failures)


class AuthSummary(BaseModel):
    """틱의 요약. 게이트와 e2e 는 `pending=0 failed=0` 과 `tick_at` 을 본다."""

    model_config = ConfigDict(extra="ignore")

    pending: int = 0
    failed: int = 0
    blocked: int = 0
    tick_at: UtcDatetime | None = None


class MongoAuthReapplyStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AUTH_REAPPLY]

    async def init_schema(self) -> None:
        return None

    async def summary(self) -> AuthSummary | None:
        doc = await self.coll.find_one({"_id": SUMMARY_ID})
        return AuthSummary.model_validate(doc) if doc else None

    async def put_summary(self, summary: AuthSummary) -> None:
        await self._put(SUMMARY_ID, summary.model_dump())

    async def cluster(self, cluster_id: str) -> ClusterAuthState:
        doc = await self.coll.find_one({"_id": f"{CLUSTER_PREFIX}{cluster_id}"})
        return ClusterAuthState.model_validate(doc or {"cluster_id": cluster_id})

    async def put_cluster(self, state: ClusterAuthState) -> None:
        await self._put(f"{CLUSTER_PREFIX}{state.cluster_id}", state.model_dump())

    async def clusters(self) -> list[ClusterAuthState]:
        cursor = self.coll.find({"_id": {"$regex": f"^{CLUSTER_PREFIX}"}}).sort("_id", 1)
        return [ClusterAuthState.model_validate(d) for d in await cursor.to_list()]

    async def backing_off(self, now: datetime) -> set[str]:
        """인증 재적용이 연결하지 못해 미뤄 둔 클러스터의 id. 리컨실의 다른 단계도 그동안 그
        클러스터를 건너뛴다."""
        return {s.cluster_id for s in await self.clusters() if s.backing_off(now)}

    async def turn_gates(self) -> list[ClusterTurnGates]:
        doc = await self.coll.find_one({"_id": TURN_GATES_ID})
        return [ClusterTurnGates.model_validate(c) for c in (doc or {}).get("clusters", [])]

    async def put_turn_gates(self, gates: list[ClusterTurnGates]) -> None:
        """모든 클러스터의 판정을 한 문서로 통째로 쓴다. 등록에서 빠진 클러스터는 함께 빠진다."""
        await self._put(TURN_GATES_ID, {"clusters": [g.model_dump() for g in gates]})

    async def retain_clusters(self, cluster_ids: list[str]) -> None:
        """등록에서 빠진 클러스터의 문서를 지운다."""
        keep = [f"{CLUSTER_PREFIX}{c}" for c in cluster_ids]
        await self.coll.delete_many({"_id": {"$regex": f"^{CLUSTER_PREFIX}", "$nin": keep}})

    async def _put(self, doc_id: str, fields: dict[str, Any]) -> None:
        await self.coll.replace_one({"_id": doc_id}, fields, upsert=True)
