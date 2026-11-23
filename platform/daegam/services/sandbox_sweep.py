"""코드 실행 샌드박스의 스윕 (P40 5절·8절). 한 레플리카만 도는 루프다(리스 `sandbox-sweep`).

이 설치의 샌드박스(`daegam.devops.ai.kr/platform-id`)만 본다. 클러스터마다 한 번에 이렇게 한다.

- 파드가 없거나 끝난 열린 기록, `hostUsers` 를 잃은 파드의 기록을 닫는다(`lost`)
- 컨테이너가 3번 다시 뜬 샌드박스를 닫는다(`restarts`). 뜰 때마다 종료 로그가 노드 디스크에 남는다
- 87 로 끝난 샌드박스는 판정을 쓰기로 무효로 하고 닫는다. 88 은 그 파드만 닫는다
- 30분 동안 쓰이지 않은 세션 샌드박스를 회수한다(`idle`)
- 판정이 무효로 읽히는 클러스터의 열린 샌드박스를 닫는다(`void`). 열린 파드의 이미지 id 가 판정의
  것과 달라도 그렇다
- Agent 가 내려갔거나 지워진 샌드박스를 닫는다(`undeployed`)
- 닫힌 기록의 파드와 기록 없이 샌드박스 라벨을 단 파드를 지운다
- 실행이 끝났는데 남은 P41 실행의 샌드박스를 닫는다(`run_ended`). 실행 몫을 쥐고 있다
- 샌드박스가 있는 네임스페이스마다 정책을 성질로 다시 보고, 어긋나면 그 Agent 의 샌드박스를 닫는다
- 열린 샌드박스가 없는 이 설치 Agent 의 샌드박스 정책을 지운다
- 클러스터마다 더 하는 단계(`extra`)를 돈다. 클러스터 점검이 `shared_nodes` 를 다시 세고, 프로브를
  몰고, 멈춘 점검이 남긴 파드를 지우는 단계를 준다(`services/sandbox_check.py`)

모든 클러스터를 지난 뒤 오래 닫히는 중인 기록을 마저 닫고, 열린 기록에서 노드 슬롯을 다시 센다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import KubeClient
from daegam.k8s.labels import LABEL_ID, LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.sandbox_pods import (
    CREATE_WAIT,
    READY_WAIT,
    PodView,
    judge_open,
    judge_open_record,
    pod_view,
)
from daegam.k8s.sandbox_policy import check_agent_namespace
from daegam.k8s.templates.common import NETWORK_POLICY_API
from daegam.k8s.templates.sandbox import (
    LABEL_PROBE,
    RESOURCE_AGENT_SANDBOX_POLICY,
    SANDBOX_RESOURCE,
)
from daegam.services.claims import TickHold, leased_tick
from daegam.services.periodic import cancel_task, run_periodic
from daegam.services.sandbox_gate import override_active, record_holds, verdict_holds
from daegam.services.sandboxes import IDLE_RECLAIM, SandboxService
from daegam.store.agent_runs import OPEN_STATUSES
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import error_kind
from daegam.store.clusters import Cluster, MongoClusterStore
from daegam.store.leases import Lease
from daegam.store.sandboxes import SandboxRecord

logger = logging.getLogger("daegam.sandbox_sweep")

LEASE_NAME = "sandbox-sweep"
# 이만큼 닫히는 중인 기록은 닫던 쪽이 멈춘 것이다. 파드를 지우고 마저 닫는다. 수는 다시 세기가
# 맞춘다
STUCK_CLOSING = timedelta(minutes=5)
# 열기가 기다리는 시간을 넘긴 여는 중인 기록은 열던 레플리카가 멈춘 것이다
STALE_OPENING = CREATE_WAIT + READY_WAIT + timedelta(seconds=30)
# 점검의 샌드박스는 점검이 닫는다. 이만큼 남았으면 점검이 멈춘 것이다
STALE_CHECK = timedelta(minutes=15)
GONE_STATES = frozenset({"draft", "undeploying", "deleting"})

# 클러스터마다 더 하는 단계. (클러스터, 클라이언트, 지금)
ClusterStep = Callable[[Cluster, KubeClient, datetime], Awaitable[None]]


class SandboxSweep:
    def __init__(
        self,
        sandboxes: SandboxService,
        clusters: MongoClusterStore,
        agents: MongoAgentStore,
        lease: Lease,
        *,
        interval: float,
        extra: Sequence[ClusterStep] = (),
    ):
        self.sandboxes = sandboxes
        self.store = sandboxes.store
        self.clusters = clusters
        # 미리 보기의 샌드박스를 배포를 내린 Agent 의 것으로 닫지 않는다 (P44b 5절)
        self.agents = agents.with_previews()
        self.lease = lease
        self.interval = interval
        self.extra = list(extra)
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    # -- 수명 --------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name=f"daegam-{LEASE_NAME}")

    async def _loop(self) -> None:
        await run_periodic(
            self.tick,
            interval=self.interval,
            stop=self._stop,
            log=logger,
            label="sandbox sweep",
            delay_first=True,
        )

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None
        await self.lease.release(LEASE_NAME)

    async def tick(self) -> None:
        await leased_tick(self.lease, LEASE_NAME, self._run, log=logger, label="sandbox sweep")

    # -- 한 바퀴 -------------------------------------------------------------------------------

    async def _run(self, now: datetime, hold: TickHold) -> None:
        for cluster in await self.clusters.list():
            await hold.item()
            try:
                await self.sweep_cluster(cluster, now)
            except Exception as e:  # noqa: BLE001 - 한 클러스터가 다른 클러스터를 막지 않는다
                logger.warning(
                    "sandbox sweep of cluster %s failed: %s", cluster.name, error_kind(e)
                )
        await self.finish_stuck(now)
        if not await self.store.recount():
            logger.info("sandbox slots changed during the recount; the next sweep counts again")

    async def sweep_cluster(self, cluster: Cluster, now: datetime) -> None:
        # 클러스터를 부르기 전이다. 닿지 않는 클러스터와 뒤 단계의 실패가 이 시한을 미루지 않는다
        await self.sandboxes.void_late_first_checks(cluster, now)
        records = await self.store.live(cluster_id=cluster.id)
        try:
            client = await self.sandboxes.client_for(cluster)
        except Exception as e:  # noqa: BLE001 - 닿지 않는 클러스터는 다음 스윕이 본다
            logger.info("sandbox sweep: cluster %s unreachable: %s", cluster.name, error_kind(e))
            return
        platform_id = await self.sandboxes.install_id()
        selector = {LABEL_RESOURCE: SANDBOX_RESOURCE, LABEL_PLATFORM_ID: platform_id}
        pods = await run_in_threadpool(client.list, "v1", "Pod", None, selector)
        by_key = {
            ((p.get("metadata") or {}).get("namespace"), (p.get("metadata") or {}).get("name")): p
            for p in pods
        }
        override = override_active(self.sandboxes.settings, cluster)
        agents: dict[str, Agent | None] = {}
        kept: list[SandboxRecord] = []
        for rec in records:
            view = pod_view(by_key.get((rec.namespace, rec.pod)))
            reason = await self._close_reason(rec, view, cluster, now, override, agents)
            if reason is None:
                kept.append(rec)
                continue
            if reason == "void":
                await self.sandboxes.void(cluster, "self_check", view)
            elif reason == "first_check":
                await self.sandboxes.first_check_failed(rec, cluster, view)
                reason = "failed_self_check"
            if reason == "later_failed":
                await self.sandboxes.settle(rec, reason, cluster, client)
            elif reason == "idle":
                # 읽은 뒤에 쓰기 표시가 왔으면 회수하지 않는다
                await self.sandboxes.close(
                    rec, reason, client=client, untouched_before=now - IDLE_RECLAIM
                )
            else:
                await self.sandboxes.close(rec, reason, client=client)
        await self._delete_strays(cluster, client, pods)
        await self._read_back(kept, agents, client, platform_id)
        await self._delete_unused_policies(cluster, client, {r.agent_id for r in kept})
        for step in self.extra:
            try:
                await step(cluster, client, now)
            except Exception as e:  # noqa: BLE001 - 더한 단계 하나가 스윕을 멈추지 않는다
                logger.warning("sandbox sweep step on %s failed: %s", cluster.name, error_kind(e))

    async def _close_reason(
        self,
        rec: SandboxRecord,
        view: PodView,
        cluster: Cluster,
        now: datetime,
        override: bool,
        agents: dict[str, Agent | None],
    ) -> str | None:
        """기록을 닫을 까닭. 둘 것이면 None 이다. `void` 는 준비됐던 샌드박스의 87 이라 판정을
        쓰기로 무효로 하고, `first_check` 는 여는 중의 87 이라 프로브가 확인하게 한다."""
        if rec.kind == "check":
            return "check_stale" if now - rec.opened_at > STALE_CHECK else None
        if rec.agent_id not in agents:
            agents[rec.agent_id] = await self.agents.get(rec.agent_id)
        agent = agents[rec.agent_id]
        if agent is None or agent.state in GONE_STATES or agent.cluster_id != cluster.id:
            return "undeployed"
        if rec.run_id is not None and not await self._run_open(rec):
            # 실행이 끝났는데 남은 샌드박스는 실행 몫을 쥐고 있다 (7절)
            return "run_ended"
        if not rec.ready:
            judgement = judge_open(view, opened_at=rec.opened_at, now=now)
            if judgement.void:
                return "first_check"
            if judgement.fail is not None or now - rec.opened_at > STALE_OPENING:
                return "open_stale"
            return None
        action = judge_open_record(view)
        if action == "void":
            return "void"
        if action != "keep":
            return action
        image_id = view.image_id or rec.image_id
        if not record_holds(cluster, unchecked=rec.unchecked, image_id=image_id, override=override):
            if not rec.unchecked and verdict_holds(cluster):
                # 판정은 그대로인데 이 파드의 이미지 id 가 다르다. 게이트가 알도록 적는다
                await self.sandboxes.note_image(cluster, image_id)
            return "verdict_void"
        if rec.last_used_at < now - IDLE_RECLAIM:
            return "idle"
        return None

    async def _run_open(self, rec: SandboxRecord) -> bool:
        run = await self.sandboxes.runs.get(rec.tenant, rec.agent_id, rec.run_id or "")
        return run is not None and run.status in OPEN_STATUSES

    async def _delete_strays(
        self, cluster: Cluster, client: KubeClient, pods: list[dict[str, Any]]
    ) -> None:
        """닫힌 기록의 파드와 기록 없이 샌드박스 라벨을 단 파드를 지운다. 프로브는 프로브 단계가
        맡는다."""
        keys = [
            ((p.get("metadata") or {}).get("namespace"), (p.get("metadata") or {}).get("name"))
            for p in pods
        ]
        known = await self.store.latest_for_pods(cluster.id, keys)
        for pod in pods:
            meta = pod.get("metadata") or {}
            if LABEL_PROBE in (meta.get("labels") or {}):
                continue
            rec = known.get((meta.get("namespace"), meta.get("name")))
            if rec is not None and rec.state == "open":
                continue
            await run_in_threadpool(
                client.delete, "v1", "Pod", meta.get("name"), meta.get("namespace")
            )

    async def _read_back(
        self,
        kept: list[SandboxRecord],
        agents: dict[str, Agent | None],
        client: KubeClient,
        platform_id: str,
    ) -> None:
        """남은 샌드박스가 있는 네임스페이스마다 정책을 성질로 다시 본다 (4절). 어긋나면 그 Agent 의
        샌드박스를 닫는다."""
        by_ns: dict[str, list[SandboxRecord]] = {}
        for rec in kept:
            if rec.kind == "session":
                by_ns.setdefault(rec.namespace, []).append(rec)
        for ns, recs in by_ns.items():
            policies = await run_in_threadpool(client.list, NETWORK_POLICY_API, "NetworkPolicy", ns)
            for agent_id in sorted({r.agent_id for r in recs}):
                agent = agents.get(agent_id)
                if agent is None:
                    continue
                refusal = check_agent_namespace(policies, agent=agent, platform_id=platform_id)
                if refusal is None:
                    continue
                logger.warning(
                    "sandboxes of agent %s closed: NetworkPolicy %s: %s",
                    agent.id,
                    refusal.policy,
                    refusal.reason,
                )
                for rec in recs:
                    if rec.agent_id == agent_id:
                        await self.sandboxes.close(rec, "policy", client=client)

    async def _delete_unused_policies(
        self, cluster: Cluster, client: KubeClient, kept_agents: set[str]
    ) -> None:
        """내려갔거나 지워진 이 설치 Agent 가운데 열린 샌드박스가 없는 것의 샌드박스 정책을 지운다.
        Agent 를 내린 뒤 끝난 열기가 `daegam-sandbox-<Agent>` 를 다시 둘 수 있다. 배포된 Agent 의
        정책은 두는데, 열기가 기록을 넣기 전에 정책을 두므로 지우면 막 연 샌드박스에 닿지 못한다. 이
        설치에 기록이 남은 Agent 만 보므로 다른 설치의 정책은 건드리지 않는다."""
        gone: set[str] = set()
        for agent_id in await self.store.agents_with_records(cluster.id) - kept_agents:
            agent = await self.agents.get(agent_id)
            if agent is None or agent.state in GONE_STATES or agent.cluster_id != cluster.id:
                gone.add(agent_id)
        if not gone:
            return
        selector = {LABEL_RESOURCE: RESOURCE_AGENT_SANDBOX_POLICY}
        policies = await run_in_threadpool(
            client.list, NETWORK_POLICY_API, "NetworkPolicy", None, selector
        )
        for policy in policies:
            meta = policy.get("metadata") or {}
            agent_id = (meta.get("labels") or {}).get(LABEL_ID)
            if agent_id in gone:
                await run_in_threadpool(
                    client.delete,
                    NETWORK_POLICY_API,
                    "NetworkPolicy",
                    meta.get("name"),
                    meta.get("namespace"),
                )

    async def finish_stuck(self, now: datetime) -> None:
        """닫던 쪽이 멈춘 기록을 마저 닫는다. 슬롯 수는 다시 세기가 맞춘다."""
        for rec in await self.store.stuck_closing(now - STUCK_CLOSING):
            cluster = await self.clusters.get(rec.cluster_id)
            try:
                if cluster is not None:
                    client = await self.sandboxes.client_for(cluster)
                    await run_in_threadpool(client.delete, "v1", "Pod", rec.pod, rec.namespace)
            except Exception as e:  # noqa: BLE001 - 남은 파드는 다음 스윕이 지운다
                logger.info("sandbox %s: pod not deleted: %s", rec.id, error_kind(e))
            await self.store.finish_close(rec.id)


__all__ = ["LEASE_NAME", "ClusterStep", "SandboxSweep"]
