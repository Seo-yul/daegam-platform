"""reconcile 루프 (F7b).

다단계 배포·해제·삭제 도중 파드가 죽어 중간 상태에 멈춘 문서를 다시 몰아간다. MCP Server 가 기본이고
Gateway(`gateways`)와 Agent(`agents`) 는 같은 리스 안에서 뒤이어 처리한다.

셋 다 상태가 갈래를 정한다. `deploying`·`failed` 는 provision, `undeploying` 은 undeploy,
`deleting` 은 delete 다. `failed` 를 다시 모는 것이 배포 실패의 자동 재시도다 (D42).

그 뒤로 같은 리스 안에서 단계들이 차례로 돈다. 클러스터 점검(클러스터마다 한 시간에 한 번, P50 2절:
preflight 와 ToolHive CR 의 Strict dry run. 한 틱의 시간을 넘기면 남은 클러스터는 다음 틱이 잇는다),
공유 복제본 스윕(P50 1절), 서비스 토큰 스윕(한 시간에 한 번, D41), 인자 조건의 기록(P52 4절),
프록시를 두던 허브 항목 옮기기(P54b 7절), 인증 재적용(P32 8절), 그리고 배선이 `steps` 로 넘긴
단계(접근 정리와 개인 토큰 키 pool 채우기, P32 4절·7절, 기동 뒤 15분 동안 JWKS 를 받지 못한 검증기
다시 띄우기, T24)다.
한 단계의 예외는 로그로 접히고 다음 단계를 막지 않는다. 결과 키는 `<단계 이름>_<단계가 낸 키>` 다.

모든 단계가 SSA·결정적 이름이라 재실행이 안전하다. 레플리카가 여럿이면 `reconcile_leases` 컬렉션의
findOneAndUpdate 리스로 한 파드만 돈다. 틱은 항목마다·단계 사이·끝에서 그 리스를 늘리고 잃으면
멈추며, 문서 몰아가기는 한 틱에 30초를 쓰고 연결할 수 없다고 답한 클러스터는 그 틱에 다시 묻지
않는다 (P67).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient, is_toolhive, is_unreachable_error
from daegam.services.advanced_edit import api_error_message
from daegam.services.budget import Budget
from daegam.services.claims import TickHold, driving, enter_tick, leave_tick, tick_item
from daegam.services.instance_base import RECONCILE_STATES
from daegam.services.mcp_servers import MCPServerService
from daegam.services.periodic import cancel_task, run_periodic
from daegam.store.base import utcnow
from daegam.store.clusters import STRICT_FAILURES_KEPT, Cluster, StrictApplyCheck
from daegam.store.leases import Lease, LeaseStop
from daegam.store.mcp_servers import MongoMCPServerStore

if TYPE_CHECKING:
    from daegam.services.agents import AgentService
    from daegam.services.clusters import ClusterService
    from daegam.services.data_plane_auth import DataPlaneAuth
    from daegam.services.gateways import GatewayService
    from daegam.services.svc_token_sweep import SvcTokenSweeper
    from daegam.store.agents import MongoAgentStore
    from daegam.store.gateways import MongoGatewayStore

logger = logging.getLogger("daegam.reconcile")

# 리스 문서의 id 다. 리소스 이름이 MCP Server 로 바뀌어도 그대로 둔다. 바꾸면 롤링 업데이트 중 옛
# 파드와 새 파드가 서로 다른 리스를 잡아 둘 다 돈다
LEASE_NAME = "mcp-instances"

# 상태 → 결과 목록 키. 어떤 동작을 부를지는 서비스의 `drive` 가 상태로 고른다
_RESULT_KEY: dict[str, str] = {
    "deploying": "created",
    "failed": "created",
    "undeploying": "undeployed",
    "deleting": "deleted",
}

# 문서를 다시 몰아가다 만나도 실패로 세지 않는 코드. 남이 클레임을 쥐었거나 읽은 뒤 문서가 바뀌었다
_SKIPPED_CODES: frozenset[str] = frozenset({"in_progress", "state_conflict"})

# 멈춘 문서를 몰아가는 데 한 틱이 쓰는 시간(초). 넘기면 남은 문서는 다음 틱이 잇는다 (P67 2절)
DRIVE_SECONDS_PER_TICK = 30.0

# 다음 동작이 저장된 overrides 를 클러스터로 보내는 상태. 해제·삭제 중인 문서는 보내지 않으므로
# overrides 감사가 세지 않는다
_APPLYING_STATES: frozenset[str] = frozenset({"deploying", "deployed", "failed"})

# Strict dry run 이 보는 편집 대상 kind (P50 2절). 저장된 overrides 가 얹히는 ToolHive CR 이다.
# Agent 의 편집 대상은 Deployment 라 ToolHive CR 의 Strict 적용과 상관이 없다. dry run 이 보지 않은
# kind 는 Strict 로 적용하지 않는다(`ClusterClient.strict_kinds`)
_STRICT_KINDS: frozenset[str] = frozenset({"MCPServer", "VirtualMCPServer"})

# 클러스터 점검이 한 틱에 쓰는 시간(초). 리컨실 리스(`Lease` 의 ttl) 안에서 다른 단계의 몫을 남긴다.
# 시간 안에 다 보지 못한 클러스터는 점검 시각이 그대로라 다음 틱이 먼저 잇는다
CLUSTER_CHECK_SECONDS_PER_TICK = 20.0


class _DryRun(Enum):
    """Strict 판정이 아닌 dry run 의 결과. 연결할 수 없으면 그 클러스터를 멈추고, Strict 가 아니어도
    거부되면(이름 공간이 없음, 권한, 충돌) 그 문서는 판정에 넣지 않는다."""

    UNREACHABLE = "unreachable"
    SKIPPED = "skipped"


# 리스 안의 단계 하나. 틱 시각을 받아 결과 목록들을 돌려준다
Step = Callable[[datetime], Awaitable[dict[str, list[str]]]]


def interval_due(last: datetime | None, now: datetime, interval: float) -> bool:
    """틱보다 드문 단계(한 시간에 한 번)가 돌 때인가. 아직 한 번도 돌지 않았으면 돈다."""
    return last is None or (now - last).total_seconds() >= interval


async def run_step(name: str, step: Step, now: datetime) -> dict[str, list[str]]:
    """단계 하나를 돌린다. 예외는 로그로 접고 `failed: [<name>_error]` 로 돌려준다."""
    try:
        return await step(now)
    except Exception as e:  # noqa: BLE001 - 한 단계의 실패가 루프와 다른 단계를 죽이면 안 된다
        logger.exception("reconcile %s: %s", name, e)
        return {"failed": [f"{name}_error"]}


class Reconciler:
    def __init__(
        self,
        instances: MongoMCPServerStore,
        service: MCPServerService,
        lease: Lease,
        *,
        interval: float = 60.0,
        stale_after: timedelta = timedelta(minutes=2),
        clone_sweep: Callable[[], Awaitable[dict[str, list[str]]]] | None = None,
        gateways: tuple[MongoGatewayStore, GatewayService] | None = None,
        agents: tuple[MongoAgentStore, AgentService] | None = None,
        svc_token_sweeper: SvcTokenSweeper | None = None,
        svc_sweep_interval: float = 3600.0,
        auth_reapply: DataPlaneAuth | None = None,
        cluster_service: ClusterService | None = None,
        cluster_check_interval: float = 3600.0,
        condition_sweep: Step | None = None,
        entry_conversion: Step | None = None,
        steps: Sequence[tuple[str, Step]] = (),
    ):
        self.instances = instances
        self.service = service
        self.lease = lease
        self.interval = interval
        self.stale_after = stale_after
        # P50 1절: 공유 복제본 스윕 (`SharedClones.sweep`)
        self.clone_sweep = clone_sweep
        # D42: Gateway 도 MCP Server 와 같은 세 갈래로 다시 몰아간다
        self.gateways = gateways
        # D26: Agent 도 같은 루프에서 provision/delete 를 다시 몰아간다
        self.agents = agents
        # D41: 소비자의 서비스 토큰을 만료 6개월 전에 갈아 끼우는 스윕. 한 시간에 한 번, 리스 안에서
        self.svc_token_sweeper = svc_token_sweeper
        self.svc_sweep_interval = svc_sweep_interval
        self._last_svc_sweep: datetime | None = None
        # P32 8절: 인증 설정이 바뀌면 MCPOIDCConfig 와 소비자를 다시 적용한다. 틱마다, 리스 안에서
        self.auth_reapply = auth_reapply
        # P50 2절: 클러스터 점검(preflight, ToolHive CR 의 Strict dry run). 클러스터마다 한 시간에
        # 한 번, 리스 안에서. 틱마다 점검할 때가 된 클러스터를 이른 것부터 본다
        self.cluster_service = cluster_service
        self.cluster_check_interval = cluster_check_interval
        # P52 4절: 인자 조건이 있는 서버의 파드가 조건을 지키는지 적는다. 인증 재적용보다 먼저 돌아,
        # 기록이 바뀐 서버를 같은 틱에 다시 렌더한다
        self.condition_sweep = condition_sweep
        # P54b 7절: 프록시를 두던 허브 항목을 Gateway 의 Entry 가 주소를 직접 부르는 꼴로 옮긴다
        # (`EntryConversion.run`). 인증 재적용보다 먼저 돌아, 스냅샷을 고쳐 쓴 Gateway 를 같은 틱에
        # 다시 렌더한다
        self.entry_conversion = entry_conversion
        # 그 밖의 단계. 이름과 함수이고 위의 단계 뒤에 적힌 순서대로 돈다
        self.extra_steps = list(steps)
        # 지난 틱에 잠금 표가 거부한 배포본과 렌더하지 못해 판정하지 않은 배포본. 목록이 바뀔 때만
        # 로그를 남기려고 들고 있는다
        self._refused_overrides: set[str] = set()
        self._unrendered_overrides: set[str] = set()
        # 지난 틱에도 이 파드가 리스를 쥐고 있었는가. 새로 잡은 틱은 파드의 기억을 버린다
        self._holding = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.runs = 0
        self.last_result: dict[str, list[str]] = self._empty()

    @staticmethod
    def _empty() -> dict[str, list[str]]:
        return {"created": [], "undeployed": [], "deleted": [], "failed": []}

    async def run_once(self, now: datetime | None = None) -> dict[str, list[str]]:
        """리스를 잡으면 stale 문서를 처리하고 단계들을 돌린다. 못 잡으면 빈 결과. Agent 의 id 도
        같은 created/deleted/failed 목록에 들어간다."""
        now = now or utcnow()
        result = self._empty()
        if not await self.lease.acquire(LEASE_NAME, now=now):
            self._holding = False
            return result
        if not self._holding and self.svc_token_sweeper is not None:
            # 리스를 새로 잡았다. 그 사이 다른 파드가 스윕을 이었을 수 있으므로 이 파드가 기억한
            # 이어 돌 자리를 버린다
            self.svc_token_sweeper.resume_after = None
        self._holding = True
        self.runs += 1
        # 틱 동안 리스를 늘린다: 항목마다, 단계 사이, 문서의 체크포인트, 클러스터 요청 앞(10초가
        # 지났을 때), 틱의 끝. 잃으면 LeaseStop 이 단계의 처리기를 그대로 지나 여기로 온다 (P67 2절)
        hold = TickHold(self.lease, LEASE_NAME)
        token = enter_tick(hold)
        try:
            await self._drive_stale(now, result)
            for name, step in self._steps():
                await hold.item()
                for key, ids in (await run_step(name, step, now)).items():
                    result[f"{name}_{key}"] = ids
            await hold.renew()
            hold.raise_if_lost()
        except LeaseStop as stop:
            if stop.name != LEASE_NAME:
                raise
            self._holding = False
            result["stopped"] = ["lease_lost"]
        finally:
            leave_tick(token)
        self.last_result = result
        return result

    async def _drive_stale(self, now: datetime, result: dict[str, list[str]]) -> None:
        """멈춘 문서를 오래된 것부터 몰아간다. 이 틱의 시간(`DRIVE_SECONDS_PER_TICK`)을 넘기면 남은
        문서는 다음 틱이 잇는다. 연결할 수 없다고 답한 클러스터의 남은 문서는 이 틱에 묻지 않는다
        (P67 2절)."""
        budget = Budget(DRIVE_SECONDS_PER_TICK)
        down: set[str] = set()
        for inst, service in await self._stale_targets(now - self.stale_after):
            await tick_item()
            if budget.out_of_time():
                result.setdefault("deferred", []).append(inst.id)
                continue
            if inst.cluster_id in down:
                result.setdefault("skipped", []).append(inst.id)
                continue
            if await self._drive(inst, service, result):
                down.add(inst.cluster_id)

    def _pairs(self) -> list[tuple[Any, Any]]:
        """(스토어, 서비스) 셋. MCP Server, Gateway, Agent 순이다."""
        pairs: list[tuple[Any, Any]] = [(self.instances, self.service)]
        return pairs + [pair for pair in (self.gateways, self.agents) if pair is not None]

    async def _stale_targets(self, stale_before: datetime) -> list[tuple[Any, Any]]:
        """중간 상태에 멈춘 (문서, 서비스). 세 종류를 합쳐 `updated_at` 이 이른 것부터다. 종류마다
        따로 두면 늘 실패하는 MCP Server 가 틱의 시간을 다 써 Gateway·Agent 가 밀린다."""
        targets: list[tuple[Any, Any]] = []
        for store, service in self._pairs():
            stale = await store.stale(list(RECONCILE_STATES), stale_before)
            targets.extend((doc, service) for doc in stale)
        targets.sort(key=lambda pair: pair[0].updated_at)
        return targets

    async def _drive(self, inst: Any, service: Any, result: dict[str, list[str]]) -> bool:
        """문서 하나를 몰아간다. 그 클러스터에 연결할 수 없다고 답했으면 참이다. 몰아가는 동안 틱의
        리스를 잃어도 문서는 자기 클레임으로 마치고, 틱은 그 뒤의 항목 확인에서 멈춘다."""
        key = _RESULT_KEY[inst.state]
        try:
            with driving():
                # 남이 쥔 클레임은 기다리지 않는다. 일을 홀더에게 남겼으면 거짓이다 (P66 2.6절)
                done = await service.drive(inst)
            if not done:
                result.setdefault("skipped", []).append(inst.id)
                return False
            result[key].append(inst.id)
        except ServiceError as e:
            if e.code in _SKIPPED_CODES:
                # 남이 그 문서의 클레임을 쥐고 있거나(in_progress) 읽은 뒤 문서가 바뀌었다
                # (state_conflict). 실패가 아니고 다음 틱이 다시 읽는다 (P66 2.6절)
                result.setdefault("skipped", []).append(inst.id)
                return False
            # MCP·Gateway·Agent 가 각자 오류 클래스를 쓴다. 공용 기반을 잡아야 종류가
            # 늘어도 정상적인 재시도가 스택 트레이스로 찍히지 않는다
            logger.warning("reconcile %s (%s): %s", inst.id, inst.state, e.detail)
            result["failed"].append(inst.id)
            # 5xx 전부가 아니라 연결할 수 없다는 답만 그 클러스터를 멈춘다. 매니페스트 하나의 거부나
            # RBAC 403 의 502 k8s_error 는 그 문서 하나의 일이다
            return e.code == "cluster_unreachable"
        except Exception as e:  # noqa: BLE001 - 한 문서의 실패가 루프를 죽이면 안 된다
            logger.exception("reconcile %s (%s): %s", inst.id, inst.state, e)
            result["failed"].append(inst.id)
        return False

    def _steps(self) -> list[tuple[str, Step]]:
        """리스 안에서 stale 문서 뒤에 도는 단계. 이름이 결과 키의 접두어다."""
        steps: list[tuple[str, Step]] = []
        if self.cluster_service is not None:
            # 게이트가 보는 판·권한을 같은 틱의 렌더보다 먼저 맞춘다
            steps.append(("clusters", self._check_clusters))
        if self.clone_sweep is not None:
            # 기록이 없는 복제본에 기록을 만들고, 옛 이름에서 옮기며, 쓰지 않는 복제본을 지운다
            sweep = self.clone_sweep
            steps.append(("shared", lambda _now: sweep()))
        if self.svc_token_sweeper is not None:
            sweeper = self.svc_token_sweeper
            steps.append(("svc_tokens", lambda now: self._sweep_svc_tokens(sweeper, now)))
        if self.condition_sweep is not None:
            steps.append(("conditions", self.condition_sweep))
        if self.entry_conversion is not None:
            steps.append(("conversion", self.entry_conversion))
        if self.auth_reapply is not None:
            steps.append(("auth", self.auth_reapply.run))
        steps.append(("overrides", self._audit_stored_overrides))
        return steps + self.extra_steps

    async def _audit_stored_overrides(self, _now: datetime) -> dict[str, list[str]]:
        """저장된 overrides 가 지금의 잠금 표에 어긋나는 배포본을 센다 (`check_stored`). 해제·삭제
        중인 문서는 세지 않는다(`_APPLYING_STATES`).

        그 검사는 대개 문서와 잠금 표만 보므로 클러스터·DB 왕복이 없다. 부모를 지우는 overrides 만
        적용 경로처럼 렌더로 보고(`render_if_wiping`), 렌더하지 못하면 렌더 없이 본다. 렌더 없는
        검사는 렌더와 함께 보는 검사보다 넓게 막으므로 그것이 지나면 그대로 지난다. 막히면 렌더가
        있어야 가릴 수 있으므로 판정하지 않고 따로 센다. 적용도 같은 렌더 오류로 멈추므로 잠금 표를
        탓할 일이 아니다. 적용 순간에만 드러나는 것을 기다리지 않고, 두 목록이 각각 바뀔 때 이유와
        함께 로그 한 줄을 남긴다. 둘 다 비면 결과 키도 없다(스윕과 같다)."""
        refused: dict[str, str] = {}
        unrendered: dict[str, str] = {}
        for store, service in self._pairs():
            for doc in await store.live():
                await tick_item()
                if doc.state not in _APPLYING_STATES:
                    continue
                try:
                    rendered = await service.editor.render_if_wiping(doc)
                except Exception as e:  # noqa: BLE001 - 한 문서의 렌더 실패가 감사를 멈추면 안 된다
                    try:
                        service.editor.check_stored(doc, None)
                    except ServiceError:
                        unrendered[doc.id] = type(e).__name__
                    continue
                try:
                    service.editor.check_stored(doc, rendered)
                except ServiceError as e:
                    refused[doc.id] = str(e.extra.get("path", e.code))
        self._refused_overrides = _warn_on_change(
            self._refused_overrides,
            refused,
            "stored overrides are refused by the current lock table",
        )
        self._unrendered_overrides = _warn_on_change(
            self._unrendered_overrides, unrendered, "stored overrides are not judged; render failed"
        )
        return {
            key: list(ids) for key, ids in (("refused", refused), ("unrendered", unrendered)) if ids
        }

    async def _sweep_svc_tokens(
        self, sweeper: SvcTokenSweeper, now: datetime
    ) -> dict[str, list[str]]:
        """한 시간에 한 번만 돈다. 때가 아니면 결과 키가 없다."""
        if not interval_due(self._last_svc_sweep, now, self.svc_sweep_interval):
            return {}
        try:
            result = await sweeper.run(now=now)
        except Exception:
            # 실패한 스윕을 틱마다 되풀이하지 않는다. 리스를 잃은 멈춤(LeaseStop)은 여기 걸리지
            # 않으므로 그 시간은 다음 홀더가 센다
            self._last_svc_sweep = now
            raise
        # 이 틱의 시간에 끝까지 못 돌았으면(`resume_after`) 다음 틱이 이어 돈다. 끝까지 돈 뒤에야
        # 한 시간을 센다
        if "resume_after" not in result:
            self._last_svc_sweep = now
        return result

    async def _check_clusters(self, now: datetime) -> dict[str, list[str]]:
        """점검할 때가 된 클러스터를 마지막 점검이 이른 것부터 본다 (P50 2절). preflight 를 다시
        돌려 저장하고, 살아 있는 문서의 ToolHive CR 을 Strict dry run 으로 보내 클러스터마다 Strict
        로 적용할 kind 를 정한다. 이 틱의 시간(`CLUSTER_CHECK_SECONDS_PER_TICK`)을 넘기면 남은
        클러스터는 다음 틱이 잇는다. 때가 된 클러스터가 없으면 결과 키가 없다."""
        service = self.cluster_service
        if service is None:
            return {}
        due = [
            (last, cluster)
            for cluster in await service.store.list()
            if (last := _last_checked(cluster)) is None
            or interval_due(last, now, self.cluster_check_interval)
        ]
        if not due:
            return {}
        due.sort(key=lambda pair: (pair[0] is not None, pair[0] or datetime.min))
        budget = Budget(CLUSTER_CHECK_SECONDS_PER_TICK)
        result: dict[str, list[str]] = {
            "preflight_checked": [],
            "preflight_failed": [],
            "strict_refused": [],
        }
        for _last, cluster in due:
            await tick_item()
            if budget.out_of_time():
                result.setdefault("deferred", []).append(cluster.name)
                continue
            preflight = await service.preflight(cluster)
            result["preflight_checked"].append(cluster.name)
            if not preflight.ok:
                result["preflight_failed"].append(cluster.name)
            check = await self._strict_check(cluster, budget)
            if check is None:
                continue
            await service.record_strict_apply(cluster, check)
            result["strict_refused"].extend(f"{cluster.name}: {why}" for why in check.failures)
        return result

    async def _strict_check(self, cluster: Cluster, budget: Budget) -> StrictApplyCheck | None:
        """클러스터 하나의 Strict dry run. 저장된 overrides 를 얹은 최종본을 보낸다(적용 경로와 같은
        `stored_final`). 렌더하지 못했거나 잠금 표가 거부한 문서는 overrides 감사가 세므로 건너뛴다.
        Strict 가 아니면 통과하는 문서만 거부로 센다. 클러스터에 연결할 수 없거나 이 틱의 시간이
        다하면 None 을 돌려주어 지난 판정을 그대로 둔다."""
        clusters = self.cluster_service
        if clusters is None:
            return None
        try:
            client = await run_in_threadpool(clusters.client_for, cluster)
        except Exception as e:  # noqa: BLE001 - 연결할 수 없는 클러스터는 다음 점검이 다시 본다
            logger.debug("strict dry run skipped cluster %s: %s", cluster.name, e)
            return None
        checked: Counter[str] = Counter()
        refused_kinds: set[str] = set()
        failures: list[str] = []
        for store, service in self._pairs():
            if service.editor.kind not in _STRICT_KINDS:
                continue
            for doc in await store.live_on_cluster(cluster.id):
                await tick_item()
                if budget.out_of_time():
                    logger.info("strict dry run of cluster %s resumes next tick", cluster.name)
                    return None
                if doc.state not in _APPLYING_STATES:
                    continue
                try:
                    final = service.editor.stored_final(doc, await service.editor.render(doc))
                except Exception:  # noqa: BLE001, S112 - 렌더·잠금 거부는 overrides 감사의 몫이다
                    continue
                if not is_toolhive(final):
                    continue
                verdict = await _strict_verdict(client, final)
                if verdict is _DryRun.UNREACHABLE:
                    logger.debug("strict dry run stopped on cluster %s", cluster.name)
                    return None
                if verdict is _DryRun.SKIPPED:
                    continue
                kind = final["kind"]
                checked[kind] += 1
                if verdict is not None:
                    meta = final.get("metadata") or {}
                    refused_kinds.add(kind)
                    failures.append(f"{kind} {meta.get('namespace')}/{meta.get('name')}: {verdict}")
        total = sum(checked.values())
        return StrictApplyCheck(
            ok=total > 0 and not failures,
            checked=total,
            refused=len(failures),
            kinds=sorted(kind for kind in checked if kind not in refused_kinds),
            failures=failures[:STRICT_FAILURES_KEPT],
        )

    async def _tick(self) -> None:
        """루프의 한 회차. run_once 가 잡지 못한 LeaseStop 이 루프를 끝내지 않게 한다. run_periodic
        은 Exception 만 잡는다."""
        try:
            await self.run_once()
        except LeaseStop as stop:
            logger.warning("reconcile tick stopped by lease %s", stop.name)

    async def _loop(self) -> None:
        await run_periodic(
            self._tick,
            interval=self.interval,
            stop=self._stop,
            log=logger,
            label="reconcile loop",
        )

    def start(self) -> None:
        if self._task is None:
            # 이벤트는 처음 기다린 루프에 묶인다. 앱을 다시 띄우면 새 루프이므로 새로 만든다
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="daegam-reconcile")

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None
        await self.lease.release(LEASE_NAME)


def _last_checked(cluster: Cluster) -> datetime | None:
    """클러스터의 마지막 점검 시각. preflight 와 Strict dry run 가운데 이른 쪽이고, 어느 하나가
    아직 없으면 None 이라 바로 점검한다(이 판으로 처음 뜬 뒤의 클러스터가 그렇다)."""
    preflight = cluster.status.preflight
    strict = cluster.gates.strict_apply
    if preflight is None or strict is None:
        return None
    return min(preflight.checked_at, strict.checked_at)


async def _strict_verdict(client: KubeClient, final: dict[str, Any]) -> str | None | _DryRun:
    """문서 하나의 Strict 판정. None 이면 통과, 글이면 Strict 만 거부한 이유다. Strict 가 아니어도
    거부되면 Strict 의 문제가 아니라 `SKIPPED` 이고, 연결할 수 없으면 `UNREACHABLE` 이다."""
    try:
        await run_in_threadpool(client.apply, final, dry_run=True, field_validation="Strict")
        return None
    except Exception as e:  # noqa: BLE001 - kubernetes ApiException 등
        if is_unreachable_error(e):
            return _DryRun.UNREACHABLE
        why = api_error_message(e)
    try:
        await run_in_threadpool(client.apply, final, dry_run=True, field_validation="Ignore")
    except Exception as e:  # noqa: BLE001
        return _DryRun.UNREACHABLE if is_unreachable_error(e) else _DryRun.SKIPPED
    return why


def _warn_on_change(before: set[str], now: dict[str, str], message: str) -> set[str]:
    """now 는 문서 id 마다 이유를 둔 표다. 목록이 지난 틱과 달라졌고 비어 있지 않을 때만 이유와
    함께 경고 한 줄을 남긴다. 다음 틱과 견줄 목록을 돌려준다."""
    if now and set(now) != before:
        logger.warning("%s: %s", message, ", ".join(f"{i}({why})" for i, why in now.items()))
    return set(now)
