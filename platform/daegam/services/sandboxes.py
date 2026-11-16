"""코드 실행 샌드박스를 열고 찾고 닫는다 (P40 5절·7절, D95).

런타임이 인스턴스 토큰으로 기계 경로를 부른다(`api/sandboxes.py`). 판단은 모두 여기 있다.

- **열기.** 세션의 첫 파일·`execute` 호출이 부른다. Agent 가 코드 실행을 켰고 내리는 중이 아니며,
  클러스터 게이트(8절)가 열렸고, 네임스페이스 정책이 성질대로이고(4절), 상한과 노드 슬롯을 조건부
  쓰기 하나로 잡았을 때(7절) 파드를 만들고 준비를 기다린다. 열린 기록이 있으면 그 샌드박스로 답한다.
  준비된 파드의 이미지 id 가 판정의 것과 다르면 판정이 무효로 읽히므로 닫고 409 다
- **찾기.** 열린 샌드박스로 답하고 만들지 않는다. 컨테이너의 마지막 끝남과 재시작 수를 함께 싣는다.
  없으면 404 와 그 세션의 마지막 기록이 닫힌 까닭이다
- **쓰기 표시(touch).** 턴 시작과 턴이 도는 동안 1분마다 온다. 열린 기록이 없으면 바로 404 다
- **닫기.** 세션이 닫히거나 지워지면, P41 실행이 끝나면 런타임이 부른다. Agent 를 내리거나 지우면
  플랫폼이 그 Agent 의 샌드박스를 모두 닫는다

사람의 몫이 찼으면 그 사람의 샌드박스 가운데 5분 넘게 쓰이지 않은 가장 오래된 것을 회수하고, 그런
것이 없을 때만 거절한다. 세션이 P41 실행의 것이면 사람의 몫 대신 Agent 의 실행 몫을 쓴다.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import anyio
from kubernetes.utils import parse_quantity
from starlette.concurrency import run_in_threadpool

from daegam.auth.sandbox_key import SandboxKey, token_sha256
from daegam.config import Settings
from daegam.errors import ServiceError, retry_after_header
from daegam.k8s.client import AccessDenied, KubeClient
from daegam.k8s.labels import LABEL_AGENT, LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.sandbox_capacity import (
    Capacity,
    ClusterLists,
    ListCache,
    eligible_for,
    limit_range_max,
    node_capacity,
    pick_node,
    read_lists,
)
from daegam.k8s.sandbox_pods import (
    PodView,
    judge_open,
    judge_open_record,
    pod_address,
    pod_view,
)
from daegam.k8s.sandbox_policy import check_agent_namespace
from daegam.k8s.sandbox_targets import build_targets, platform_address, target_env
from daegam.k8s.templates.common import NETWORK_POLICY_API
from daegam.k8s.templates.sandbox import (
    CPU_LIMIT,
    SANDBOX_RESOURCE,
    render_agent_sandbox_policy,
    render_sandbox_pod,
    sandbox_cpu_request,
    sandbox_memory_bytes,
    sandbox_placement,
    sandbox_pod_name,
)
from daegam.services.agent_graph import code_execution_of
from daegam.services.clusters import ClusterService
from daegam.services.sandbox_gate import (
    SandboxGateClosed,
    gate_reason,
    override_active,
    record_holds,
    require_open,
    verdict_holds,
)
from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.services.telemetry.normalize import (
    SANDBOX_CLOSED,
    SANDBOX_OPENED,
    SOURCE_PLATFORM,
    agent_event_fields,
    preview_mark,
)
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, pinned_agent
from daegam.store.agent_runs import OPEN_STATUSES, MongoAgentRunStore
from daegam.store.agent_session_owners import MongoAgentSessionOwnerStore
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.clusters import Cluster, MongoClusterStore, SandboxVoidReason
from daegam.store.sandboxes import CapName, MongoSandboxStore, SandboxRecord, SlotCaps
from daegam.store.settings import MongoPlatformSettingsStore

logger = logging.getLogger("daegam.sandboxes")

# 기계 경로의 Agent 마다 버킷 (5절). touch 가 세션마다 1분에 하나라 세션 수백 개도 들어간다
ROUTE_RATE = 10.0
ROUTE_BURST = 60.0
ROUTE_IDLE_SECONDS = 600.0
ROUTE_ENTRIES = 10_000
RETRY_AFTER = timedelta(seconds=1)
# 열기가 기다리는 가장 긴 시간. 120초(배치·만들기)와 30초(준비)다
MAX_WAIT_SECONDS = 150.0
POLL_SECONDS = 0.5
# 30분 동안 쓰이지 않은 샌드박스를 회수한다
IDLE_RECLAIM = timedelta(minutes=30)
# 노드 하나에 사용자 코드 없는 프로브는 10분에 하나다 (8절)
PROBE_QUIET = timedelta(minutes=10)
# 사람의 몫이 찼을 때 회수할 수 있는 샌드박스. 1분마다 touch 하므로 5분이면 어느 턴도 쥐지 않았다
PERSON_RECLAIM_AFTER = timedelta(minutes=5)
# 다른 레플리카가 같은 노드의 마지막 슬롯을 먼저 잡았을 때 다른 노드로 다시 하는 수
NODE_RETRIES = 3
# 런타임이 닫기에 싣는 까닭. 기계 경로의 본문(`api/sandboxes.py`)이 이것을 받는다
RuntimeCloseReason = Literal["session_closed", "session_deleted", "run_ended"]
# 코드 실행 Agent 의 상태 경고 (8절). 화면이 까닭을 문장 `(reason: <까닭>)` 에서 읽는다
GATE_CLOSED_WARNING = "sandbox_gate_closed"
OVERRIDE_WARNING = "sandbox_override"


class SandboxError(ServiceError):
    pass


def code_execution_on(agent: Agent) -> bool:
    """배포가 핀한 리비전의 메인 에이전트 노드가 코드 실행을 켰는가 (9절). 그래프의 칸
    (`agent_graph.code_execution_of`)을 읽는다."""
    rev = agent.deployed
    return rev is not None and code_execution_of(rev) is not None


def api_error_message(e: BaseException) -> str:
    """API 서버 거절의 Status message. `str(DynamicApiError)` 는 HTTP 머리와 본문을 통째로 실어
    런타임과 모델에게 가므로 쓰지 않는다."""
    if isinstance(e, AccessDenied):
        return f"forbidden: {e.missing}"
    body = getattr(e, "body", None)
    if body:
        try:
            message = json.loads(body).get("message")
        except (TypeError, ValueError, AttributeError):
            message = None
        if isinstance(message, str) and message:
            return message
    status = getattr(e, "status", None)
    if status:
        return f"{status} {getattr(e, 'reason', '') or ''}".strip()
    return type(e).__name__


def limit_error(cap: CapName) -> SandboxError:
    """429 `sandbox_limit`. cap 은 거절한 상한이다(7절)."""
    return SandboxError(
        429,
        "sandbox_limit",
        f"the {cap} limit on open sandboxes is reached",
        cap=cap,
        headers=retry_after_header(timedelta(seconds=30)),
    )


def open_failed(reason: str, detail: str | None = None) -> SandboxError:
    """503 `sandbox_open_failed`. 그 열기만 실패했고 아무것도 무효로 하지 않았다(87 은 따로)."""
    return SandboxError(
        503, "sandbox_open_failed", detail or f"the sandbox did not start: {reason}", reason=reason
    )


class SandboxService:
    """기계 경로와 Agent 의 해제·삭제, 스윕(`services/sandbox_sweep.py`)이 쓴다. 키는 기동이
    붙인다(`attach_key`)."""

    def __init__(
        self,
        store: MongoSandboxStore,
        *,
        agents: MongoAgentStore,
        tokens: MongoAgentInstanceTokenStore,
        clusters: MongoClusterStore,
        cluster_service: ClusterService,
        owners: MongoAgentSessionOwnerStore,
        runs: MongoAgentRunStore,
        events: MongoAgentEventStore,
        platform_settings: MongoPlatformSettingsStore,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        clock: Any = time.monotonic,
    ):
        self.store = store
        self.agents = agents
        self.tokens = tokens
        self.clusters = clusters
        self.cluster_service = cluster_service
        self.owners = owners
        self.runs = runs
        self.events = events
        self.platform_settings = platform_settings
        self.settings = settings
        self.poll_seconds = poll_seconds
        self.key: SandboxKey | None = None
        self.lists = ListCache()
        self._limits = KeyedBuckets(
            ROUTE_RATE,
            ROUTE_BURST,
            idle_seconds=ROUTE_IDLE_SECONDS,
            max_entries=ROUTE_ENTRIES,
            clock=clock,
        )
        self._install_id: str | None = None

    def attach_key(self, key: SandboxKey) -> None:
        self.key = key

    def token_for(self, sandbox_id: str) -> str:
        if self.key is None:
            raise SandboxError(503, "sandbox_unavailable", "the sandbox key is not loaded")
        return self.key.token(sandbox_id)

    async def install_id(self) -> str:
        if self._install_id is None:
            self._install_id = await self.platform_settings.install_id()
        return self._install_id

    # -- 클러스터 ----------------------------------------------------------------------------

    async def client_for(self, cluster: Cluster) -> KubeClient:
        return await run_in_threadpool(self.cluster_service.client_for, cluster)

    async def cluster_lists(
        self, cluster: Cluster, client: KubeClient, *, fresh: bool = False
    ) -> ClusterLists:
        """노드·파드·LimitRange·Service 목록. 60초까지 캐시한다. fresh 면 다시 읽는다. 읽을
        권한이 없으면(새 RBAC 를 적용하지 않은 원격 클러스터) 게이트가 `rights` 로 닫힌 것이다."""
        if fresh:
            self.lists.invalidate(cluster.id)
        try:
            return await run_in_threadpool(self.lists.get, cluster.id, lambda: read_lists(client))
        except AccessDenied as e:
            raise SandboxGateClosed(
                "rights", f"the platform cannot read the cluster's lists: {e.missing}"
            ) from None

    def capacity_of(self, lists: ClusterLists, cluster: Cluster, platform_id: str) -> Capacity:
        """지금의 클러스터 용량 (7절)."""
        placement = sandbox_placement(cluster)
        return node_capacity(
            lists.nodes,
            lists.pods,
            lists.limit_ranges,
            node_selector=placement["node_selector"],
            tolerations=placement["tolerations"],
            platform_id=platform_id,
            sandbox_bytes=sandbox_memory_bytes(self.settings.sandbox_workspace),
            reserve=self.settings.sandbox_node_reserve,
            max_sandboxes=self.settings.sandbox_max,
        )

    async def capacity(self, cluster: Cluster, client: KubeClient | None = None) -> Capacity:
        client = client or await self.client_for(cluster)
        lists = await self.cluster_lists(cluster, client)
        return self.capacity_of(lists, cluster, await self.install_id())

    def targets_for(
        self, cluster: Cluster, lists: ClusterLists, *, own_node: str | None
    ) -> list[dict[str, Any]]:
        """자체 점검 대상 (8절). 플랫폼의 주소는 home 에서만 싣는다."""
        eligible = eligible_for(cluster, lists.nodes)
        platform = (
            platform_address(cluster.platform_internal_url, lists.services)
            if cluster.is_home
            else None
        )
        targets = build_targets(
            eligible=eligible,
            pods=lists.pods,
            services=lists.services,
            platform=platform,
            own_node=own_node,
        )
        return target_env(targets)

    async def deploy_check(self, cluster: Cluster, namespace: str) -> None:
        """코드 실행 Agent 를 배포하기 전의 검사다 (7절·8절). 게이트가 닫혔으면 409
        `sandbox_gate_closed` 와 그 까닭이고, 네임스페이스 LimitRange 의 최대 메모리나 CPU 가
        샌드박스 하나의 한도를 담지 못하면 409 `sandbox_limit_range` 다. 그런 LimitRange 는 모든
        열기를 503 `refused` 로 막는다. 권한이 없으면 목록을 읽기 전에 `rights` 다."""
        if not cluster.gates.sandbox_rights:
            raise SandboxGateClosed("rights")
        client = await self.client_for(cluster)
        lists = await self.cluster_lists(cluster, client)
        capacity = self.capacity_of(lists, cluster, await self.install_id())
        override = override_active(self.settings, cluster)
        require_open(cluster, capacity=capacity.total, override=override)
        needs = {
            "memory": Decimal(sandbox_memory_bytes(self.settings.sandbox_workspace)),
            "cpu": parse_quantity(CPU_LIMIT),
        }
        for resource, need in needs.items():
            most = limit_range_max(lists.limit_ranges, namespace, resource)
            if most is not None and most < need:
                raise SandboxError(
                    409,
                    "sandbox_limit_range",
                    f"a LimitRange in namespace {namespace} caps {resource} at {most}, below the "
                    f"{need} a workspace needs",
                    namespace=namespace,
                    resource=resource,
                )

    async def gate_warnings(self, agent: Agent) -> list[str]:
        """코드 실행을 켠 살아 있는 Agent 의 상태 경고다 (8절). 게이트가 닫혔으면
        `sandbox_gate_closed` 와 그 까닭, 개발용 우회가 들면 `sandbox_override` 다. 클러스터를 읽지
        못하면 아무것도 싣지 않는다(상태 조회를 막지 않는다)."""
        if not code_execution_on(agent):
            return []
        cluster = await self.clusters.get(agent.cluster_id or "")
        if cluster is None:
            return []
        override = override_active(self.settings, cluster)
        reason: str | None = "rights"
        if cluster.gates.sandbox_rights:
            try:
                capacity = await self.capacity(cluster)
                reason = gate_reason(cluster, capacity=capacity.total, override=override)
            except SandboxGateClosed as e:
                reason = e.reason
            except Exception as e:  # noqa: BLE001 - 닿지 않는 클러스터는 경고를 싣지 않는다
                logger.info("sandbox gate of %s not read: %s", cluster.name, type(e).__name__)
                return []
        out: list[str] = []
        if reason is not None:
            out.append(
                f"{GATE_CLOSED_WARNING}: code execution is unavailable on this cluster "
                f"(reason: {reason})"
            )
        if override:
            out.append(
                f"{OVERRIDE_WARNING}: code execution runs without the isolation check by a "
                "development setting"
            )
        return out

    # -- 기계 경로 ---------------------------------------------------------------------------

    async def _agent(self, agent_id: str, presented: str) -> Agent:
        """인스턴스 토큰(401)과 Agent 마다의 버킷(429)이다."""
        agent = await pinned_agent(self.agents, self.tokens, agent_id, presented)
        if not self._limits.take(agent.id):
            raise SandboxError(
                429,
                "sandbox_rate_limited",
                "too many sandbox requests for this agent",
                headers=retry_after_header(RETRY_AFTER),
            )
        return agent

    async def _cluster(self, agent: Agent) -> Cluster:
        cluster = await self.clusters.get(agent.cluster_id or "")
        if cluster is None or agent.k8s is None:
            raise SandboxError(409, "not_deployed", f"agent {agent.id} is not deployed")
        return cluster

    async def open(
        self, agent_id: str, presented: str, session_id: str, *, wait: float | None = None
    ) -> dict[str, Any]:
        """세션의 샌드박스. 열린 것이 있으면 그것이고 없으면 연다. wait 는 부르는 쪽이 기다릴 수
        있는 초(턴의 남은 시간)다."""
        agent = await self._agent(agent_id, presented)
        if agent.state in ("draft", "undeploying", "deleting"):
            raise SandboxError(409, "not_deployed", f"agent {agent.id} is being taken down")
        if not code_execution_on(agent):
            raise SandboxError(
                409, "sandbox_not_enabled", "code execution is off for this agent's revision"
            )
        cluster = await self._cluster(agent)
        if not cluster.gates.sandbox_rights:
            # 목록을 읽기 전에 본다. 권한이 없는 클러스터의 목록 읽기는 403 이다
            raise SandboxGateClosed("rights")
        client = await self.client_for(cluster)
        budget = MAX_WAIT_SECONDS if wait is None else max(0.0, min(wait, MAX_WAIT_SECONDS))
        deadline = anyio.current_time() + budget
        found = await self.store.open_for(agent.id, session_id)
        rec = await self._still_held(found, cluster, client)
        if rec is None:
            rec = await self._start(agent, cluster, client, session_id)
        return await self._await_ready(rec, cluster, client, deadline)

    async def _still_held(
        self, rec: SandboxRecord | None, cluster: Cluster, client: KubeClient
    ) -> SandboxRecord | None:
        """열린 기록을 그대로 써도 되는가(`record_holds`, 스윕과 같은 규칙). 판정이 그 뒤 무효가
        됐으면(87, 프로브, 배치·이미지 바뀜) 닫고 None 이다. 다음 스윕까지 토큰을 내주지 않는다."""
        if rec is None:
            return None
        override = override_active(self.settings, cluster)
        if record_holds(cluster, unchecked=rec.unchecked, image_id=rec.image_id, override=override):
            return rec
        await self.close(rec, "verdict_void", client=client)
        return None

    async def lookup(self, agent_id: str, presented: str, session_id: str) -> dict[str, Any]:
        """열린 샌드박스. 만들지 않는다. 여는 중이면 `ready: false` 다."""
        agent = await self._agent(agent_id, presented)
        rec = await self.store.open_for(agent.id, session_id)
        if rec is None:
            raise await self._not_found(agent.id, session_id)
        cluster = await self._cluster(agent)
        client = await self.client_for(cluster)
        if await self._still_held(rec, cluster, client) is None:
            raise await self._not_found(agent.id, session_id)
        view = await self._view(client, rec)
        if not rec.ready:
            return {"sandbox_id": rec.id, "generation": rec.generation, "ready": False}
        action = judge_open_record(view)
        if action != "keep":
            await self.settle(rec, action, cluster, client)
            raise await self._not_found(agent.id, session_id)
        return self._answer(rec, view)

    async def touch(self, agent_id: str, presented: str, session_id: str) -> dict[str, Any]:
        agent = await self._agent(agent_id, presented)
        rec = await self.store.touch(agent.id, session_id)
        if rec is None:
            raise await self._not_found(agent.id, session_id)
        return {"sandbox_id": rec.id, "last_used_at": rec.last_used_at}

    async def close_session(
        self, agent_id: str, presented: str, session_id: str, reason: str
    ) -> dict[str, bool]:
        agent = await self._agent(agent_id, presented)
        rec = await self.store.open_for(agent.id, session_id)
        return {"closed": rec is not None and await self.close(rec, reason)}

    async def _not_found(self, agent_id: str, session_id: str) -> SandboxError:
        """404 `sandbox_not_found`. 그 세션의 마지막 기록이 닫힌 까닭(회수·잃음 등)을 싣는다."""
        last = await self.store.last_for(agent_id, session_id)
        return SandboxError(
            404,
            "sandbox_not_found",
            "this session has no open sandbox",
            closed_reason=last.close_reason if last is not None else None,
        )

    # -- 열기 --------------------------------------------------------------------------------

    async def _start(
        self, agent: Agent, cluster: Cluster, client: KubeClient, session_id: str
    ) -> SandboxRecord:
        platform_id = await self.install_id()
        lists = await self.cluster_lists(cluster, client)
        capacity = self.capacity_of(lists, cluster, platform_id)
        override = override_active(self.settings, cluster)
        require_open(cluster, capacity=capacity.total, override=override)
        assert agent.k8s is not None
        await self._check_policies(agent, client)
        person, run_id = await self._who(agent, session_id)
        last = await self.store.last_for(agent.id, session_id)
        generation = (last.generation + 1) if last is not None else 1
        rec = SandboxRecord(
            agent_id=agent.id,
            tenant=agent.tenant,
            cluster_id=cluster.id,
            namespace=agent.k8s.namespace,
            session_id=session_id,
            person=person,
            run_id=run_id,
            node="",
            generation=generation,
            pod=sandbox_pod_name(agent.id, session_id, generation),
            unchecked=override and not verdict_holds(cluster),
        )
        held = await self.reserve(rec, capacity)
        if held.id != rec.id:
            # 같은 세션의 다른 열기가 먼저 넣었다. 그 샌드박스를 기다린다
            return held
        try:
            await self._create_pod(held, agent, cluster, client, lists, platform_id)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.close(held, "failed", client=client)
            raise
        return held

    async def _check_policies(self, agent: Agent, client: KubeClient) -> None:
        """Agent 의 샌드박스 정책을 두고 네임스페이스의 정책을 성질로 본다 (4절). 어긋나면 503
        `sandbox_namespace_policy` 이고 판정은 무효로 하지 않는다."""
        assert agent.k8s is not None
        ns = agent.k8s.namespace
        await run_in_threadpool(client.apply, render_agent_sandbox_policy(agent))
        policies = await run_in_threadpool(client.list, NETWORK_POLICY_API, "NetworkPolicy", ns)
        refusal = check_agent_namespace(policies, agent=agent, platform_id=await self.install_id())
        if refusal is not None:
            raise SandboxError(
                503,
                "sandbox_namespace_policy",
                f"NetworkPolicy {refusal.policy}: {refusal.reason}",
                policy=refusal.policy,
            )

    async def _who(self, agent: Agent, session_id: str) -> tuple[str | None, str | None]:
        """(사람, 실행 id). 사람은 플랫폼의 세션 소유자 기록에서 오고, 세션이 열린 P41 실행의
        것이면 실행 id 다. 런타임이 보낸 값은 믿지 않는다."""
        owner = await self.owners.get(agent.id, session_id)
        run = await self.runs.for_session(agent.tenant, agent.id, session_id)
        run_id = run.id if run is not None and run.status in OPEN_STATUSES else None
        return (owner.sub if owner is not None else None), run_id

    def _caps(self, capacity: Capacity, node: str) -> SlotCaps:
        s = self.settings
        return SlotCaps(
            person=s.sandbox_per_person,
            agent=s.sandbox_per_agent,
            run=s.sandbox_runs_per_agent,
            tenant=s.sandbox_per_tenant,
            cluster=capacity.total,
            node=capacity.caps().get(node, 0),
        )

    async def reserve(self, rec: SandboxRecord, capacity: Capacity) -> SandboxRecord:
        """빈 슬롯이 가장 많은 노드를 골라 기록을 넣고 상한과 슬롯을 잡는다. 같은 세션의 열린
        기록이 이미 있으면 그것을 돌려준다. 다른 레플리카가 그 노드의 마지막 슬롯을 먼저 잡았으면
        다른 노드로 다시 한다. 사람의 몫이 찼으면 회수할 것을 하나 회수하고 다시 한다. 잡지 못하면
        429 `sandbox_limit` 이다."""
        caps = capacity.caps()
        node = pick_node(caps, await self.store.node_counts(rec.cluster_id, list(caps)))
        if node is None:
            raise limit_error("cluster")
        rec.node = node
        if not await self.store.insert(rec):
            existing = await self.store.open_for(rec.agent_id, rec.session_id)
            if existing is not None:
                return existing
            raise open_failed("conflict", "another open of this session just ended; try again")
        reclaimed = False
        for attempt in range(NODE_RETRIES + 2):
            refused = await self.store.reserve(rec, self._caps(capacity, rec.node))
            if refused is None:
                return rec
            if refused == "person" and not reclaimed and rec.person is not None:
                # 사람의 몫만 막을 때만 회수한다. 다른 상한도 막으면 회수해도 거절이다
                caps_now = self._caps(capacity, rec.node)
                if await self.store.refusing(rec, caps_now) == ["person"]:
                    reclaimed = await self._reclaim_for(rec.person)
                    if reclaimed:
                        continue
            if refused == "cluster" and attempt < NODE_RETRIES:
                used = await self.store.node_counts(rec.cluster_id, list(caps))
                other = pick_node(caps, used)
                if other is not None and other != rec.node:
                    rec.node = other
                    await self.store.set_node(rec.id, other)
                    continue
            await self.store.drop_unreserved(rec.id, f"limit_{refused}")
            raise limit_error(refused)
        await self.store.drop_unreserved(rec.id, "limit_cluster")
        raise limit_error("cluster")

    async def _reclaim_for(self, person: str) -> bool:
        """그 사람의 샌드박스 가운데 5분 넘게 쓰이지 않은 가장 오래된 것 하나를 회수한다."""
        cutoff = utcnow() - PERSON_RECLAIM_AFTER
        for old in await self.store.reclaimable(person, cutoff):
            # 읽은 뒤에 쓰기 표시가 왔으면 회수하지 않는다
            if await self.close(old, "reclaimed", untouched_before=cutoff):
                return True
        return False

    async def _create_pod(
        self,
        rec: SandboxRecord,
        agent: Agent,
        cluster: Cluster,
        client: KubeClient,
        lists: ClusterLists,
        platform_id: str,
    ) -> None:
        """파드를 만들고 `hostUsers: false` 가 남았는지 다시 읽는다(2절). ResourceQuota·
        LimitRange 의 거절은 503 과 그 까닭이다(7절)."""
        assert agent.k8s is not None
        limits = [
            item
            for lr in lists.limit_ranges
            if (lr.get("metadata") or {}).get("namespace") == rec.namespace
            for item in (lr.get("spec") or {}).get("limits") or []
        ]
        token = self.token_for(rec.id)
        pod = render_sandbox_pod(
            cluster=cluster,
            agent=agent,
            owner_uid=await self._owner_uid(agent, client),
            session_id=rec.session_id,
            generation=rec.generation,
            sandbox_id=rec.id,
            node=rec.node,
            token_sha256=token_sha256(token),
            targets=self.targets_for(cluster, lists, own_node=rec.node),
            platform_id=platform_id,
            unchecked=rec.unchecked,
            workspace_bytes=self.settings.sandbox_workspace,
            cpu_request=sandbox_cpu_request({"limits": limits}),
        )
        try:
            created = await run_in_threadpool(client.create, pod)
        except Exception as e:  # noqa: BLE001 - API 서버의 거절(쿼터·LimitRange·권한)이다
            message = api_error_message(e)
            raise open_failed(
                "refused", f"the API server refused the sandbox pod: {message}"
            ) from None
        if not created:
            raise open_failed("conflict", f"pod {rec.pod} already exists")
        back = await run_in_threadpool(client.get, "v1", "Pod", rec.pod, rec.namespace)
        if back is not None and not pod_view(back).host_users_kept:
            raise open_failed("host_users", "the API server dropped hostUsers: false")

    async def _owner_uid(self, agent: Agent, client: KubeClient) -> str | None:
        """Agent Deployment 의 uid. 문서에 없으면 Deployment 를 읽는다."""
        if agent.k8s_uid:
            return agent.k8s_uid
        assert agent.k8s is not None
        dep = await run_in_threadpool(
            client.get, "apps/v1", "Deployment", agent.k8s.name, agent.k8s.namespace
        )
        return ((dep or {}).get("metadata") or {}).get("uid")

    async def _view(self, client: KubeClient, rec: SandboxRecord) -> PodView:
        return pod_view(await run_in_threadpool(client.get, "v1", "Pod", rec.pod, rec.namespace))

    async def _await_ready(
        self, rec: SandboxRecord, cluster: Cluster, client: KubeClient, deadline: float
    ) -> dict[str, Any]:
        """파드가 준비될 때까지 기다린다. 이미 준비됐던 샌드박스면 스윕의 판단으로 본다. 기다릴
        시간이 다했는데 아직 여는 중이면 503 `sandbox_starting` 이고 기록은 남아 다음 호출이 이어
        기다린다."""
        while True:
            view = await self._view(client, rec)
            if rec.ready:
                action = judge_open_record(view)
                if action != "keep":
                    await self.settle(rec, action, cluster, client)
                    raise await self._not_found(rec.agent_id, rec.session_id)
                if view.pod_ip:
                    return self._answer(rec, view)
            else:
                judgement = judge_open(view, opened_at=rec.opened_at, now=utcnow())
                if judgement.ready and view.pod_ip:
                    return await self._ready(rec, cluster, client, view)
                if judgement.fail is not None:
                    if judgement.void:
                        await self.void(cluster, "self_check")
                    await self.close(rec, f"failed_{judgement.fail}", client=client)
                    raise open_failed(judgement.fail)
            if anyio.current_time() >= deadline:
                raise SandboxError(
                    503, "sandbox_starting", "the sandbox is still starting", sandbox_id=rec.id
                )
            await anyio.sleep(self.poll_seconds)
            fresh = await self.store.get(rec.id)
            if fresh is None or fresh.state != "open":
                raise await self._not_found(rec.agent_id, rec.session_id)
            rec = fresh

    async def _ready(
        self, rec: SandboxRecord, cluster: Cluster, client: KubeClient, view: PodView
    ) -> dict[str, Any]:
        """처음 준비된 파드. 이미지 id 가 판정의 것과 다르면 판정이 무효로 읽힌다(8절). 그 파드를
        닫고 409 `sandbox_gate_closed` `unchecked` 다."""
        unchecked = rec.unchecked
        if not unchecked and not verdict_holds(cluster, image_id=view.image_id):
            await self.note_image(cluster, view.image_id)
            if not override_active(self.settings, cluster):
                await self.close(rec, "image_mismatch", client=client)
                raise SandboxGateClosed(
                    "unchecked", "the sandbox runs an image the isolation check did not check"
                )
            # 개발용 우회가 드는 home 이면 점검 없이 연 것으로 쓴다
            unchecked = True
        marked, first = await self.store.mark_ready(rec.id, view.image_id, unchecked=unchecked)
        if marked is None:
            raise await self._not_found(rec.agent_id, rec.session_id)
        if first:
            await self._event(marked, SANDBOX_OPENED)
        return self._answer(marked, view)

    def _answer(self, rec: SandboxRecord, view: PodView) -> dict[str, Any]:
        last = None
        if view.exit_code is not None or view.terminated_reason:
            last = {"reason": view.terminated_reason, "exit_code": view.exit_code}
        return {
            "sandbox_id": rec.id,
            "generation": rec.generation,
            "ready": view.ready,
            "address": pod_address(view.pod_ip) if view.pod_ip else None,
            "token": self.token_for(rec.id),
            "unchecked": rec.unchecked,
            "restarts": view.restarts,
            "last_termination": last,
        }

    # -- 닫기 --------------------------------------------------------------------------------

    async def close(
        self,
        rec: SandboxRecord,
        reason: str,
        *,
        client: KubeClient | None = None,
        untouched_before: datetime | None = None,
    ) -> bool:
        """`closing` 으로 옮긴 쪽 하나가 파드를 지우고 슬롯을 돌려준 뒤 `closed` 로 옮긴다.
        파드를 지우지 못하면 스윕이 닫힌 기록의 파드를 지운다. 이미 닫히는 중이면 거짓이다.
        untouched_before 는 회수(idle, reclaimed)의 조건이다. 그때 뒤로 쓰였으면 닫지 않는다."""
        closing = await self.store.begin_close(rec.id, reason, untouched_before=untouched_before)
        if closing is None:
            return False
        try:
            if client is None:
                cluster = await self.clusters.get(rec.cluster_id)
                client = await self.client_for(cluster) if cluster is not None else None
            if client is not None:
                await run_in_threadpool(client.delete, "v1", "Pod", rec.pod, rec.namespace)
        except Exception as e:  # noqa: BLE001 - 남은 파드는 스윕이 지운다
            logger.warning("sandbox %s: pod not deleted on close: %s", rec.id, type(e).__name__)
        await self.store.release(closing)
        await self.store.finish_close(rec.id)
        await self._event(closing, SANDBOX_CLOSED, reason)
        return True

    async def close_agent(self, agent: Agent, reason: str) -> int:
        """Agent 의 샌드박스를 모두 닫는다(해제·삭제, 5절). 기록 없이 남은 그 Agent 의 샌드박스
        파드도 라벨로 지운다. 닫은 수다."""
        n = 0
        client: KubeClient | None = None
        cluster = await self.clusters.get(agent.cluster_id or "")
        if cluster is not None:
            try:
                client = await self.client_for(cluster)
            except Exception as e:  # noqa: BLE001 - 남은 파드는 스윕이 지운다
                logger.warning("agent %s: sandboxes closed without a cluster: %s", agent.id, e)
        for rec in await self.store.live(agent_id=agent.id):
            n += await self.close(rec, reason, client=client)
        if client is not None and agent.k8s is not None:
            labels = {
                LABEL_RESOURCE: SANDBOX_RESOURCE,
                LABEL_AGENT: agent.id,
                LABEL_PLATFORM_ID: await self.install_id(),
            }
            ns = agent.k8s.namespace
            try:
                pods = await run_in_threadpool(client.list, "v1", "Pod", ns, labels)
                # 사용자 코드 없는 프로브는 Agent 라벨이 없어 여기 들지 않는다
                for pod in pods:
                    if name := (pod.get("metadata") or {}).get("name"):
                        await run_in_threadpool(client.delete, "v1", "Pod", name, ns)
            except Exception as e:  # noqa: BLE001 - 남은 파드는 스윕이 지운다
                logger.warning("agent %s: sandbox pods not swept: %s", agent.id, type(e).__name__)
        return n

    async def note_image(self, cluster: Cluster, image_id: str | None) -> None:
        """열린 파드가 판정의 것과 다른 이미지 id 를 알렸다. 판정에 적어 게이트가 다음 열기에서
        파드를 만들기 전에 닫히게 한다(8절). 판정이 없으면 적을 것이 없다."""
        checked_at = cluster.sandbox.check.checked_at
        if image_id and checked_at is not None:
            if await self.clusters.note_sandbox_image(cluster.id, checked_at, image_id):
                logger.warning("cluster %s: a sandbox pod runs an unchecked image", cluster.name)
            cluster.sandbox.check.seen_image_id = cluster.sandbox.check.seen_image_id or image_id

    async def void(self, cluster: Cluster, reason: SandboxVoidReason) -> None:
        """판정을 쓰기로 무효로 한다 (8절). 87 로 끝난 첫 점검과 실패한 플랫폼 프로브만 부른다."""
        if await self.clusters.void_sandbox_check(cluster.id, reason):
            logger.warning("cluster %s: sandbox verdict voided (%s)", cluster.name, reason)

    async def settle(
        self, rec: SandboxRecord, action: str, cluster: Cluster, client: KubeClient
    ) -> None:
        """준비됐던 샌드박스를 스윕의 판단대로 닫는다. 87 은 판정을 무효로 하고, 88 은 그 노드에
        사용자 코드 없는 프로브를 바란다(8절). 88 은 명령이 닿을 수 있던 프로세스가 낸 것일 수
        있어 판정을 무효로 하지 않는다."""
        if action == "void":
            await self.void(cluster, "self_check")
            await self.close(rec, "self_check_failed", client=client)
        elif action == "later_failed":
            await self.close(rec, "later_check_failed", client=client)
            await self.store.want_probe(
                rec.cluster_id,
                rec.node,
                rec.namespace,
                rec.tenant,
                quiet_since=utcnow() - PROBE_QUIET,
            )
        elif action == "restarts":
            await self.close(rec, "restarts", client=client)
        elif action == "lost":
            await self.close(rec, "lost", client=client)

    async def _event(self, rec: SandboxRecord, name: str, reason: str | None = None) -> None:
        """`sandbox_opened`·`sandbox_closed` (D62). 세션과 사람을 싣는다. 점검의 샌드박스는 Agent 가
        없어 싣지 않는다. 쓰지 못해도 열기와 닫기는 그대로다."""
        if rec.kind != "session":
            return
        body: dict[str, Any] = {
            "source": SOURCE_PLATFORM,
            "session_id": rec.session_id,
            "user_sub": rec.person,
            "sandbox_id": rec.id,
            "generation": rec.generation,
            "node": rec.node,
            "run_id": rec.run_id,
            "unchecked": rec.unchecked,
        }
        if reason is not None:
            body["reason"] = reason
        doc = {
            "_id": uuid.uuid4().hex,
            "ts": utcnow(),
            "tenant": rec.tenant,
            "instance_id": rec.agent_id,
            "event": name,
            "source": SOURCE_PLATFORM,
            **agent_event_fields(body),
        }
        try:
            # 미리 보기의 샌드박스 이벤트는 원본의 것이고 미리 보기 표시를 단다 (P44b 9절)
            agent = await self.agents.with_previews().get(rec.agent_id)
            if agent is not None:
                doc.update(preview_mark(agent))
            await self.events.add([doc])
        except Exception as e:  # noqa: BLE001 - 이벤트가 열기·닫기를 막지 않는다
            logger.warning("sandbox %s: %s event not written: %s", rec.id, name, e)
