"""코드 실행 샌드박스의 클러스터 점검과 허용, `shared_nodes`, 사용자 코드 없는 프로브 (P40 8절).

**점검.** 플랫폼 관리자가 Cluster 화면에서 **격리 점검** 을 누르면 뒤에서 돈다. 클러스터마다 리스
`sandbox-check:<id>` 하나다.

1. 대상을 `daegam-probe` 의 양성 대조 파드(어느 egress 정책도 고르지 않는다)에서 같은 시한으로 잰다.
   닿은 것(맺어진 TCP, DNS 답, ICMP 응답)만 남긴다. 거절은 지킬 것이 없는 닫힌 포트이고, 대조도
   시간을 넘긴 것은 잴 수 없다. 클러스터 DNS 와 `kubernetes` Service 는 늘 남기고, 대조가 닿지
   못하면 점검을 하지 못한 것이다
2. 테넌트 네임스페이스가 지니는 정책(대역 테넌트)을 둔 `daegam-probe` 에 진짜 샌드박스를 연다.
   다른 열기처럼 슬롯을 잡고 자체 점검을 기다린다
3. Agent 가 아닌 클라이언트 파드에서, home 이면 플랫폼 네임스페이스(이 프로세스)에서도 8080 을
   두드린다
4. `shared_nodes` 와 클러스터 용량을 센다
5. 판정을 통째로 바꾼다. 앞의 허용과 무효는 함께 지워진다. 점검 파드를 지운다

점검을 하지 못하면(권한, preflight 없음, 당기기, 용량, 시한, 양성 대조) 판정은 앞의 것 그대로이고
실행 기록(`sandbox.run`)이 그 까닭을 든다.

**허용.** `shared_nodes` 가 참인 클러스터는 관리자가 그 위험을 허용해야 게이트가 열린다. 판정이
통과했고 관리자가 본 `checked_at` 이 지금의 것일 때만 적는다. 감사는 HTTP 감사가 남긴다.

**shared_nodes.** 들어갈 노드에 샌드박스도 DaemonSet 파드도 아닌 파드가 하나라도 돌면 참이다.
Advanced 편집이 Agent 와 MCP Server 를 어느 노드에든 둘 수 있으므로 스윕이 살아 있는 파드로 다시
센다. 참이 되면 게이트가 `unaccepted` 로 닫힌다. 무효가 아니다.

**프로브.** 88 로 끝난 샌드박스의 노드에서(그 네임스페이스) 사용자 코드 없는 프로브를 돈다. 한
노드에 한 번에 하나, 10분에 하나이고, 요청과 한도가 128 MiB 이며 상한에 세지 않는다. 87 이면
판정을 쓰기로 무효로 한다. 뜨지 못한 프로브는 다음 스윕이 다시 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

import anyio
from pymongo.asynchronous.database import AsyncDatabase
from starlette.concurrency import run_in_threadpool

from daegam.auth.sandbox_key import token_sha256
from daegam.config import Settings
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import LABEL_PLATFORM_ID, LABEL_RESOURCE
from daegam.k8s.sandbox_capacity import COUNTED_PHASES, eligible_for
from daegam.k8s.sandbox_pods import (
    CREATE_WAIT,
    EXIT_COULD_NOT_RUN,
    EXIT_FIRST_FAILED,
    READY_WAIT,
    TERMINAL_PHASES,
    judge_open,
    pod_view,
)
from daegam.k8s.sandbox_targets import (
    ALWAYS_KEPT,
    Target,
    build_targets,
    platform_address,
)
from daegam.k8s.templates.sandbox import (
    CHECK_HELPER_RESOURCE,
    PROBE_NAMESPACE,
    SANDBOX_CONTAINER,
    SANDBOX_PORT,
    SANDBOX_RESOURCE,
    placement_fingerprint,
    probe_pod_name,
    render_agent_sandbox_policy,
    render_check_helper_pod,
    render_isolation_policy,
    render_probe_pod,
    render_sandbox_pod,
    sandbox_image,
    sandbox_pod_name,
    verdict_void_reason,
)
from daegam.services.periodic import renewing_every
from daegam.services.sandboxes import SandboxError, SandboxService
from daegam.services.tenancy import network_policy_manifest
from daegam.store.agents import Agent, AgentK8s, AgentSpec
from daegam.store.base import error_kind, utcnow
from daegam.store.clusters import Cluster, MongoClusterStore, SandboxCheck, SandboxCheckRun
from daegam.store.leases import Lease, lease_held, run_lease
from daegam.store.sandboxes import ProbeEnd, SandboxRecord

logger = logging.getLogger("daegam.sandbox_check")

# 점검의 대역 테넌트와 Agent. `daegam-probe` 는 테넌트가 잡을 수 없다(`RESERVED_NAMESPACES`)
CHECK_TENANT = "daegam-probe"
CHECK_AGENT_ID = "sandbox-check"
CHECK_LEASE_PREFIX = "sandbox-check:"
CHECK_LEASE_SECONDS = 90.0
# 양성 대조와 클라이언트를 기다리는 시간. 첫 파드는 이미지를 당긴다
HELPER_WAIT_SECONDS = CREATE_WAIT.total_seconds() + 60.0
SANDBOX_WAIT_SECONDS = (CREATE_WAIT + READY_WAIT).total_seconds()
CONNECT_TIMEOUT = 2.0
# 클라이언트 모드가 연결을 시도했고 막혔다 (`sandbox/daegam_sandbox/__main__.py`)
EXIT_CLIENT_BLOCKED = 3
POLL_SECONDS = 1.0


def check_lease(cluster_id: str) -> str:
    return f"{CHECK_LEASE_PREFIX}{cluster_id}"


class CouldNotRun(Exception):  # noqa: N818 - 점검의 결과 하나다
    """점검을 하지 못했다. 글은 실행 기록의 error 로 간다."""


def check_agent(cluster: Cluster) -> Agent:
    """점검의 진짜 샌드박스를 여는 대역 Agent. Deployment 가 없어 ownerReference 를 두지 않는다."""
    return Agent(
        id=CHECK_AGENT_ID,
        tenant=CHECK_TENANT,
        name="sandbox-check",
        display_name="sandbox-check",
        spec=AgentSpec(),
        state="deployed",
        cluster_id=cluster.id,
        k8s=AgentK8s(name="agent-sandbox-check", namespace=PROBE_NAMESPACE, token_secret="none"),
    )


def shared_nodes(eligible: list[Any], pods: list[dict[str, Any]]) -> bool:
    """들어갈 노드에 샌드박스도 DaemonSet 파드도 아닌 파드가 도는가. 점검 자신의 파드는 세지 않는다.
    다른 설치의 샌드박스도 샌드박스다."""
    names = {str((n.get("metadata") or {}).get("name") or "") for n in eligible}
    for pod in pods:
        if (pod.get("status") or {}).get("phase") not in COUNTED_PHASES:
            continue
        if (pod.get("spec") or {}).get("nodeName") not in names:
            continue
        meta = pod.get("metadata") or {}
        if (meta.get("labels") or {}).get(LABEL_RESOURCE) in (
            SANDBOX_RESOURCE,
            CHECK_HELPER_RESOURCE,
        ):
            continue
        if any(o.get("kind") == "DaemonSet" for o in meta.get("ownerReferences") or []):
            continue
        return True
    return False


# home 의 플랫폼 네임스페이스에서 8080 을 두드린 결과. unreachable 은 닿을 길이 아예 없는 것이다
# (클러스터 밖의 플랫폼처럼). 막혔다는 증거가 아니므로 점검을 하지 못한 것이다
Knock = Literal["connected", "blocked", "unreachable"]
# 정책이 막은 것으로 읽는 연결 실패. k3s 의 컨트롤러는 거절하고 다른 CNI 는 버려 시간을 넘긴다
_BLOCKED_ERRNOS = frozenset({errno.ECONNREFUSED})


async def tcp_knock(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> Knock:
    """TCP 연결이 timeout 안에 맺어지는가. 거절과 시간 초과는 막힌 것이고, 그 밖의 OSError(닿을
    경로 없음 같은)는 잴 수 없는 것이다."""
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(host, port)
    except TimeoutError:
        return "blocked"
    except OSError as e:
        return "blocked" if e.errno in _BLOCKED_ERRNOS else "unreachable"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return "connected"


def _failed_kind(pod: dict[str, Any] | None) -> str:
    """87 로 끝난 점검의 종료 메시지가 말하는 실패. 보이려는 것이고 무엇이 실패했는지만 가른다.
    통과와 실패는 끝 코드가 정했다."""
    for cs in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
        if cs.get("name") != SANDBOX_CONTAINER:
            continue
        for state in (cs.get("state") or {}, cs.get("lastState") or {}):
            message = (state.get("terminated") or {}).get("message")
            if message:
                with contextlib.suppress(ValueError):
                    if json.loads(message).get("result") == "identity":
                        return "userns"
    return "network"


class SandboxChecks:
    """점검·허용·스윕의 두 단계. 점검은 앱 기동 동안 뒤의 태스크로 돈다."""

    def __init__(
        self,
        sandboxes: SandboxService,
        clusters: MongoClusterStore,
        db: AsyncDatabase,
        settings: Settings,
        *,
        poll_seconds: float = POLL_SECONDS,
        connect: Callable[[str, int], Awaitable[Knock]] = tcp_knock,
    ):
        self.sandboxes = sandboxes
        self.clusters = clusters
        self.db = db
        self.settings = settings
        self.poll_seconds = poll_seconds
        self.connect = connect
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- 점검 --------------------------------------------------------------------------------

    async def running(self, cluster_id: str) -> bool:
        return await lease_held(self.db, check_lease(cluster_id))

    async def start(self, cluster: Cluster, by: str) -> None:
        """점검을 뒤에서 돌린다. 이미 돌면 409 `sandbox_check_running` 이다."""
        lease = run_lease(self.db, self.settings.pod_id)
        name = check_lease(cluster.id)
        if not await lease.acquire(name):
            raise SandboxError(
                409, "sandbox_check_running", "an isolation check is already running here"
            )
        await self.clusters.set_sandbox_run(cluster.id, SandboxCheckRun(started_at=utcnow(), by=by))
        task = asyncio.create_task(self._held(cluster, by, lease, name), name=f"daegam-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _held(self, cluster: Cluster, by: str, lease: Lease, name: str) -> None:
        try:
            async with renewing_every(CHECK_LEASE_SECONDS, lambda: lease.renew(name)):
                await self.run(cluster, by)
        except Exception as e:  # noqa: BLE001 - 뒤의 태스크라 올릴 곳이 없다
            logger.warning("isolation check of %s failed: %s", cluster.name, error_kind(e))
        finally:
            with anyio.CancelScope(shield=True):
                await lease.release(name)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def run(self, cluster: Cluster, by: str) -> SandboxCheck | None:
        """점검 하나를 끝까지 돈다. 끝났으면 쓴 판정이고, 하지 못했으면 None 이며 실행 기록에 그
        까닭이 남는다."""
        started = utcnow()
        try:
            check = await self._check(cluster)
        except Exception as e:  # noqa: BLE001 - 하지 못한 점검은 그 까닭을 실행 기록에 남긴다
            if isinstance(e, CouldNotRun):
                error = str(e)
            elif isinstance(e, SandboxError):
                error = f"{e.code}: {e}"
            else:
                error = f"error: {error_kind(e)}"
            await self.clusters.set_sandbox_run(
                cluster.id,
                SandboxCheckRun(started_at=started, finished_at=utcnow(), by=by, error=error),
            )
            logger.warning("isolation check of %s could not run: %s", cluster.name, error)
            return None
        await self.clusters.set_sandbox_check(cluster.id, check)
        await self.clusters.set_sandbox_run(
            cluster.id, SandboxCheckRun(started_at=started, finished_at=utcnow(), by=by)
        )
        logger.info("isolation check of %s: passed=%s", cluster.name, check.passed)
        return check

    async def _check(self, cluster: Cluster) -> SandboxCheck:
        svc = self.sandboxes
        if not cluster.gates.sandbox_rights:
            raise CouldNotRun("rights: the platform lacks the sandbox rights or PriorityClass")
        preflight = cluster.status.preflight
        if preflight is None or not preflight.ok:
            raise CouldNotRun("preflight: run preflight first")
        try:
            client = await svc.client_for(cluster)
            platform_id = await svc.install_id()
            lists = await svc.cluster_lists(cluster, client, fresh=True)
        except Exception as e:  # noqa: BLE001 - 닿지 못한 클러스터는 점검하지 못한 것이다
            raise CouldNotRun(f"cluster: {error_kind(e)}") from None
        eligible = eligible_for(cluster, lists.nodes)
        if not eligible:
            raise CouldNotRun("nodes: no node is eligible for sandboxes")
        capacity = svc.capacity_of(lists, cluster, platform_id)
        platform = (
            platform_address(cluster.platform_internal_url, lists.services)
            if cluster.is_home
            else None
        )
        targets = build_targets(
            eligible=eligible, pods=lists.pods, services=lists.services, platform=platform
        )
        nonce = uuid.uuid4().hex[:10]
        kept, seconds = await self._control(cluster, client, targets, nonce, platform_id)
        userns, network, image_id = await self._real_sandbox(
            cluster, client, kept, capacity, nonce, platform_id
        )
        return SandboxCheck(
            passed=userns and network is True,
            userns=userns,
            network=network,
            targets=[{**t.env(), "role": t.role, "seconds": seconds.get(t.key)} for t in kept],
            shared_nodes=shared_nodes(eligible, lists.pods),
            capacity=capacity.total,
            placement=placement_fingerprint(cluster),
            image=sandbox_image(cluster),
            image_id=image_id,
            k8s_version=preflight.k8s_version,
            policy_controller=preflight.policy_controller,
            checked_at=utcnow(),
        )

    async def _control(
        self,
        cluster: Cluster,
        client: KubeClient,
        targets: list[Target],
        nonce: str,
        platform_id: str,
    ) -> tuple[list[Target], dict[Any, float | None]]:
        """양성 대조 (1단계). (남길 대상, 대상마다 대조의 연결 시간)."""
        pod = render_check_helper_pod(
            cluster=cluster,
            name=f"sbx-control-{nonce}",
            args=["control"],
            targets=[t.env() for t in targets],
            platform_id=platform_id,
        )
        exit_code, logs = await self._run_helper(client, pod)
        if exit_code != 0:
            raise CouldNotRun(f"control: the positive control exited {exit_code}")
        reached: dict[Any, float | None] = {}
        for line in logs.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("reached") is True:
                key = (item.get("kind"), item.get("host"), item.get("port"))
                reached[key] = item.get("seconds")
        kept = [t for t in targets if t.key in reached or t.role in ALWAYS_KEPT]
        missing = [f"{t.kind} {t.host}:{t.port}" for t in kept if t.key not in reached]
        if missing:
            raise CouldNotRun(f"control: the positive control did not reach {', '.join(missing)}")
        return kept, reached

    async def _run_helper(self, client: KubeClient, pod: dict[str, Any]) -> tuple[int | None, str]:
        """한 번짜리 파드를 돌리고 (끝 코드, 로그)다. 시한 안에 끝나지 않으면 점검을 하지 못한
        것이다. 파드는 늘 지운다."""
        name = pod["metadata"]["name"]
        ns = pod["metadata"]["namespace"]
        try:
            await run_in_threadpool(client.create, pod)
            deadline = anyio.current_time() + HELPER_WAIT_SECONDS
            while True:
                found = await run_in_threadpool(client.get, "v1", "Pod", name, ns)
                view = pod_view(found)
                if not view.exists:
                    raise CouldNotRun(f"{name}: the pod disappeared")
                if view.phase in TERMINAL_PHASES:
                    logs = await run_in_threadpool(client.pod_log, name, ns)
                    return view.exit_code, logs
                if anyio.current_time() >= deadline:
                    reason = view.waiting_reason or ("unscheduled" if not view.scheduled else "")
                    raise CouldNotRun(f"{name}: did not finish in time {reason}".strip())
                await anyio.sleep(self.poll_seconds)
        finally:
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                await run_in_threadpool(client.delete, "v1", "Pod", name, ns)

    async def _real_sandbox(
        self,
        cluster: Cluster,
        client: KubeClient,
        kept: list[Target],
        capacity: Any,
        nonce: str,
        platform_id: str,
    ) -> tuple[bool, bool | None, str | None]:
        """진짜 샌드박스 (2·3단계). (userns, network, 파드의 이미지 id). uid map 이 그대로이거나
        파드가 뜨지 못하면 네트워크는 재지 못해 None 이다."""
        svc = self.sandboxes
        agent = check_agent(cluster)
        for policy in (
            render_isolation_policy(CHECK_TENANT, PROBE_NAMESPACE),
            network_policy_manifest(CHECK_TENANT, PROBE_NAMESPACE, self.settings.namespace),
            render_agent_sandbox_policy(agent),
        ):
            await run_in_threadpool(client.apply, policy)
        session = f"check-{nonce}"
        rec = SandboxRecord(
            kind="check",
            agent_id=agent.id,
            tenant=CHECK_TENANT,
            cluster_id=cluster.id,
            namespace=PROBE_NAMESPACE,
            session_id=session,
            node="",
            pod=sandbox_pod_name(agent.id, session, 1),
        )
        try:
            rec = await svc.reserve(rec, capacity)
        except SandboxError as e:
            raise CouldNotRun(f"capacity: {e}") from None
        try:
            pod = render_sandbox_pod(
                cluster=cluster,
                agent=agent,
                owner_uid=None,
                session_id=session,
                generation=1,
                sandbox_id=rec.id,
                node=rec.node,
                token_sha256=token_sha256(svc.token_for(rec.id)),
                targets=[t.env() for t in kept],
                platform_id=platform_id,
                workspace_bytes=self.settings.sandbox_workspace,
            )
            try:
                await run_in_threadpool(client.create, pod)
            except Exception as e:  # noqa: BLE001 - API 서버의 거절이다
                raise CouldNotRun(f"sandbox: the API server refused the pod: {e}") from None
            view, found = await self._await_sandbox(client, rec)
            if not view.host_users_kept:
                # API 서버가 hostUsers 를 버렸다. user namespace 없는 파드다
                return False, None, None
            if view.ready:
                pass
            elif view.exit_code == EXIT_FIRST_FAILED:
                if _failed_kind(found) == "userns":
                    return False, None, view.image_id
                return True, False, view.image_id
            elif view.waiting_reason in ("CreateContainerError", "RunContainerError"):
                # user namespace 를 쓰는 파드가 시작되지 않는다
                return False, None, view.image_id
            elif view.exit_code == EXIT_COULD_NOT_RUN:
                raise CouldNotRun("sandbox: the self-check could not run (exit 86)")
            else:
                raise CouldNotRun(f"sandbox: not ready ({view.waiting_reason or view.phase})")
            network = not await self._reachable(cluster, client, rec, view, platform_id)
            return True, network, view.image_id
        finally:
            with anyio.CancelScope(shield=True):
                await svc.close(rec, "check_done", client=client)

    async def _await_sandbox(
        self, client: KubeClient, rec: SandboxRecord
    ) -> tuple[Any, dict[str, Any] | None]:
        """점검의 샌드박스를 열기와 같은 시한으로 기다린다. (모습, 파드). 시한을 넘기면 점검을 하지
        못한 것이다."""
        deadline = anyio.current_time() + SANDBOX_WAIT_SECONDS
        while True:
            found = await run_in_threadpool(client.get, "v1", "Pod", rec.pod, rec.namespace)
            view = pod_view(found)
            judgement = judge_open(view, opened_at=rec.opened_at, now=utcnow())
            if judgement.ready or judgement.fail is not None:
                return view, found
            if anyio.current_time() >= deadline:
                waiting = view.waiting_reason or "pending"
                raise CouldNotRun(f"sandbox: not ready in time ({waiting})")
            await anyio.sleep(self.poll_seconds)

    async def _reachable(
        self,
        cluster: Cluster,
        client: KubeClient,
        rec: SandboxRecord,
        view: Any,
        platform_id: str,
    ) -> bool:
        """8080 이 Agent 가 아닌 파드나 home 의 플랫폼 네임스페이스에서 열리는가 (3단계). 클라이언트
        파드의 0(연결됨)과 3(시도했고 막힘)만 증거다. 그 밖의 끝(1, OOM, 상태 없는 Failed)은
        점검을 하지 못한 것이다."""
        ip = view.pod_ip
        if not ip:
            raise CouldNotRun("sandbox: the pod has no IP")
        pod = render_check_helper_pod(
            cluster=cluster,
            name=f"sbx-client-{rec.id[:10]}",
            args=["client", ip, str(SANDBOX_PORT)],
            targets=None,
            platform_id=platform_id,
        )
        exit_code, _logs = await self._run_helper(client, pod)
        if exit_code == 0:
            return True
        if exit_code != EXIT_CLIENT_BLOCKED:
            raise CouldNotRun(f"client: the non-Agent client pod could not try (exit {exit_code})")
        if not cluster.is_home:
            return False
        knock = await self.connect(ip, SANDBOX_PORT)
        if knock == "unreachable":
            raise CouldNotRun("platform: the platform cannot reach the pod network at all")
        return knock == "connected"

    # -- 허용 --------------------------------------------------------------------------------

    async def accept(self, cluster: Cluster, by: str, checked_at: datetime) -> None:
        """노드를 함께 쓰는 위험의 허용 (P40 8절). 판정이 통과했고 무효로 읽히지 않으며 그
        `checked_at` 이 관리자가 본 값일 때만이다. 아니면 409 `sandbox_check_stale` 이다."""
        if verdict_void_reason(cluster) is not None or not await self.clusters.accept_sandbox(
            cluster.id, by, checked_at
        ):
            raise SandboxError(
                409,
                "sandbox_check_stale",
                "the isolation check you saw is no longer the current passed result",
            )

    # -- 스윕의 단계 ---------------------------------------------------------------------------

    async def shared_nodes_step(self, cluster: Cluster, client: KubeClient, now: datetime) -> None:
        """살아 있는 파드로 `shared_nodes` 를 다시 센다. 판정이 없으면 셀 것이 없다."""
        check = cluster.sandbox.check
        if check.checked_at is None or not check.passed:
            return
        lists = await self.sandboxes.cluster_lists(cluster, client)
        eligible = eligible_for(cluster, lists.nodes)
        shared = shared_nodes(eligible, lists.pods)
        if shared != check.shared_nodes:
            await self.clusters.set_sandbox_shared_nodes(cluster.id, shared)
            logger.info("cluster %s: shared_nodes is now %s", cluster.name, shared)

    async def helpers_step(self, cluster: Cluster, client: KubeClient, now: datetime) -> None:
        """멈춘 점검이 남긴 양성 대조·클라이언트 파드를 지운다. 점검이 도는 동안은 두지 않는다."""
        if await self.running(cluster.id):
            return
        selector = {
            LABEL_RESOURCE: CHECK_HELPER_RESOURCE,
            LABEL_PLATFORM_ID: await self.sandboxes.install_id(),
        }
        pods = await run_in_threadpool(client.list, "v1", "Pod", PROBE_NAMESPACE, selector)
        for pod in pods:
            name = (pod.get("metadata") or {}).get("name")
            if name:
                await run_in_threadpool(client.delete, "v1", "Pod", name, PROBE_NAMESPACE)

    async def probes_step(self, cluster: Cluster, client: KubeClient, now: datetime) -> None:
        """바라는 프로브를 띄우고 도는 프로브의 끝 코드를 읽는다. 여는 중의 샌드박스가 낸 87 을
        확인하는 프로브(`first_check`)의 결말은 끝낼 때의 문서로 정한다(`probe_end`). 시한을 넘긴
        것은 스윕이 클러스터를 부르기 전에 이미 끝냈고(`SandboxService.void_late_first_checks`),
        여기서는 그 파드를 지운다. 문서 하나의 실패가 다른 문서를 막지 않는다."""
        store = self.sandboxes.store
        for doc in await store.orphan_probes(cluster.id):
            try:
                pod, ns = doc["orphan_pod"], doc["namespace"]
                await run_in_threadpool(client.delete, "v1", "Pod", pod, ns)
                await store.orphan_deleted(doc["_id"], pod)
            except Exception as e:  # noqa: BLE001 - 다음 스윕이 다시 지운다
                logger.warning("probe pod cleanup on %s failed: %s", cluster.name, error_kind(e))
        for doc in await store.probes_of(cluster.id):
            try:
                await self._probe_doc(cluster, client, doc, now)
            except Exception as e:  # noqa: BLE001 - 한 프로브가 다른 프로브를 막지 않는다
                logger.warning(
                    "probe on node %s of %s failed: %s",
                    doc.get("node"),
                    cluster.name,
                    error_kind(e),
                )

    async def _probe_doc(
        self, cluster: Cluster, client: KubeClient, doc: dict[str, Any], now: datetime
    ) -> None:
        store = self.sandboxes.store
        key = doc["_id"]
        if doc.get("state") == "wanted":
            await self._start_probe(cluster, client, doc)
            return
        pod_name = doc.get("pod")
        found = None
        if pod_name:
            found = await run_in_threadpool(client.get, "v1", "Pod", pod_name, doc["namespace"])
        view = pod_view(found)
        if not view.exists:
            await store.probe_retry(key)
            return
        if view.phase in TERMINAL_PHASES:
            if view.phase == "Succeeded" or view.exit_code == 0:
                result = "passed"
            elif view.exit_code == EXIT_FIRST_FAILED:
                result = "failed"
            else:
                result = "could_not_run"
            end = await store.probe_end(key, result)
            if end is not None:
                await self._probe_ended(cluster, doc, result, end)
            await run_in_threadpool(client.delete, "v1", "Pod", pod_name, doc["namespace"])
            return
        started = doc.get("started_at")
        if view.running_since is None and started and now - started > CREATE_WAIT:
            # 시한 안에 뜨지 못했다. 다음 스윕이 다시 한다
            await run_in_threadpool(client.delete, "v1", "Pod", pod_name, doc["namespace"])
            await store.probe_retry(key)

    async def _probe_ended(
        self, cluster: Cluster, doc: dict[str, Any], result: str, end: ProbeEnd
    ) -> None:
        """끝난 프로브가 판정에 하는 일. 87 은 사용자 코드가 닿지 않은 파드의 실패라 언제나
        무효다. 87 을 확인하던 프로브는 돌지 못해도 무효이고(닫힌 쪽), 통과하면 판정을 그대로
        둔다. 그 87 보다 먼저 시작한 프로브(`end.again`)는 답이 아니라 새 프로브가 확인한다."""
        first = end.first_check
        if result == "failed":
            await self.sandboxes.void(cluster, "self_check" if first else "probe")
        elif first and end.again:
            logger.info(
                "cluster %s: the probe on node %s started before a first check failed there; "
                "a new probe confirms it",
                cluster.name,
                doc["node"],
            )
        elif first and result == "passed":
            logger.warning(
                "cluster %s: the probe on node %s passed, so the first check that "
                "failed there is not confirmed; it said: %s",
                cluster.name,
                doc["node"],
                first.get("message") or "(nothing)",
            )
        elif first:
            # 87 을 확인하지 못했다. 닫힌 쪽으로 정한다
            await self.sandboxes.void(cluster, "self_check")

    async def _start_probe(self, cluster: Cluster, client: KubeClient, doc: dict[str, Any]) -> None:
        svc = self.sandboxes
        lists = await svc.cluster_lists(cluster, client)
        name = probe_pod_name(cluster.id, doc["node"], uuid.uuid4().hex)
        pod = render_probe_pod(
            cluster=cluster,
            name=name,
            namespace=doc["namespace"],
            node=doc["node"],
            targets=svc.targets_for(cluster, lists, own_node=doc["node"]),
            tenant=doc.get("tenant") or "",
            platform_id=await svc.install_id(),
        )
        await run_in_threadpool(client.create, pod)
        await svc.store.probe_started(doc["_id"], name)
