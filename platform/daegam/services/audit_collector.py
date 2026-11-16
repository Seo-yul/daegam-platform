"""MCP 계층 감사 수집 (P6 F5 b) 과 Agent 런타임 이벤트 수집 (D26).

ToolHive 는 `MCPServer.spec.audit.enabled` 일 때 프록시 파드 stdout 으로 감사 이벤트를 낸다
(파일 경로를 줄 수 없다). 플랫폼이 60초 주기로 테넌트 네임스페이스의 프록시 파드
(`toolhive=true` + `app.kubernetes.io/name` ∈ mcpserver | virtualmcpserver | mcpremoteproxy,
orbstack 의 실제 파드 라벨로 확인) 로그를 `pods/log` 로 읽어 `"audit_id"` 가 있는 줄만 JSON 으로
파싱해 `mcp_audit_events` 에 넣는다. `_id` 가 audit_id 라 중복은 무시된다.

Agent 런타임(`daegam.devops.ai.kr/resource=agent-instance` 파드의 `agent` 컨테이너)은 관측 이벤트를
stdout 에 `{"daegam_event": ...}` 한 줄씩 쓴다(agent-runtime/README.md). 같은 폴링이 그 줄을
`TelemetryWriter`(services/telemetry/writer.py)로 넣는다. 줄을 문서로 바꾸는 규칙은 런타임이 밀어
보낸 이벤트와 같다(services/telemetry/normalize.py). 이벤트의 `id` 가 `_id` 라 두 길로 온 같은
이벤트는 한 번만 들어가고, `id` 가 없는 옛 줄은 파드 uid 와 줄 원문의 해시라 겹쳐 읽은 줄이
걸러진다. 테넌트·인스턴스는 파드 라벨(`daegam.devops.ai.kr/tenant`·`daegam.devops.ai.kr/id`)에서
읽는다(P37 4절).

레플리카가 여럿이면 `store/leases.py` 의 `Lease`(`_id: "mcp-audit"`) 로 한 파드만 돈다.

실제 MCP 이벤트 모양 (ToolHive 0.46 spike 에서 확인, pkg/audit 는 0.51.2 도 같다). 스펙 초안의
camelCase(auditId, loggedAt) 가 아니라 snake_case 다. 둘 다 받는다.
    {"time": ..., "level": "AUDIT", "msg": "audit_event", "audit_id": "...",
     "type": "mcp_tool_call", "logged_at": "...", "outcome": "success", "component": "toolhive-api",
     "source": {"type": "network", "value": "127.0.0.1", "extra": {...}},
     "subjects": {"user": "Local User: local", "user_id": "local"},
     "target": {"endpoint": "/mcp", "method": "tools/call", "name": "Add", "type": "tool"},
     "metadata": {"extra": {"duration_ms": 42, "transport": "http"}}}
위임 토큰(턴 토큰, P53)으로 부른 호출이면 ToolHive 0.53 은 `act` 사슬을 최상위
`"delegation": {"chain": [{"sub": "svc:agent:<name>"}], "truncated": false, ...}` 로 적는다(맨
바깥 행위자가 앞이다). 다른 클레임은 남기지 않는다 (P53 F0 3번). 수집기는 행위자마다 `sub` 만,
상한까지 잘라 남긴다(`delegation_chain`).

알려진 유실: 파드별 커서는 마지막으로 본 이벤트 시각이고 pods/log 는 since_seconds(초 단위) 로만
자르므로 몇 초를 겹쳐 읽고 id 로 중복을 뺀다. 파드가 재시작되면(uid 변경) 처음부터 다시
읽지만, 이전 파드의 마지막 폴링 이후 줄과 kubelet 로그 로테이션으로 밀려난 줄은 잃는다.
한 번에 읽는 양은 max_bytes(기본 4 MiB) 까지다. 로그 수집기(Fluent Bit) 연동은 v2.
push 할 주소를 받은 Agent 파드가 첫 push 없이 STARTUP_GRACE 안에 사라지면 그 줄도 잃는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.audit.store import MongoAuditStore
from daegam.k8s.client import ClusterUnreachable, KubeClient
from daegam.k8s.labels import APP_MCPSERVER, LABEL_APP_NAME, LABEL_ID, LABEL_RESOURCE, LABEL_TENANT
from daegam.k8s.templates.agent import AGENT_CONTAINER, EVENTS_URL_ENV
from daegam.k8s.templates.agent import RESOURCE as AGENT_RESOURCE
from daegam.k8s.templates.common import named_item
from daegam.services.clusters import ClusterService
from daegam.services.periodic import cancel_task, run_periodic
from daegam.services.telemetry.normalize import json_object, parse_agent_line, preview_mark
from daegam.services.telemetry.writer import TelemetryWriter
from daegam.store.agents import MongoAgentStore
from daegam.store.base import error_kind, parse_iso_utc, utcnow
from daegam.store.clusters import MongoClusterStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.hub import MongoHubStore
from daegam.store.leases import Lease
from daegam.store.mcp_servers import MongoMCPServerStore
from daegam.store.telemetry import PUSH_FRESHNESS, MongoAgentTelemetryStore

logger = logging.getLogger("daegam.audit.collector")

LEASE_NAME = "mcp-audit"
PROXY_LABEL = LABEL_APP_NAME
PROXY_KINDS: frozenset[str] = frozenset({APP_MCPSERVER, "virtualmcpserver", "mcpremoteproxy"})
NAME_LABEL = "toolhive-name"
MAX_BYTES = 4 * 1024 * 1024
OVERLAP_SECONDS = 5
# 파드 하나의 묶음을 하나도 넣지 못한 연속 폴링 수. 넘으면 그 줄들을 건너뛰고 센다
STUCK_ATTEMPTS = 3
# agent 컨테이너가 뜬 뒤 첫 push 를 기다리는 시간. 런타임은 기동을 마친 뒤 인스턴스 토큰이
# 받아들여지기를 60초까지 기다리고 보낸다(runtime/shipper.py AUTH_WAIT_SECONDS)
STARTUP_GRACE = timedelta(seconds=120)
# 감사 이벤트의 위임 사슬에서 남기는 행위자 수와 행위자 sub 의 길이 상한. 넘으면 자르고
# `truncated` 를 켠다 (P53 7절)
MAX_DELEGATION_CHAIN = 8
MAX_DELEGATION_SUB = 256

# (tenant, instance_id) 를 파드에서 찾는 함수. 없으면 (None, None)
TenantOf = Callable[[dict[str, Any]], tuple[str | None, str | None]]


# 이벤트 시각 파서. 이 모듈과 테스트가 이 이름으로 부른다
parse_ts = parse_iso_utc


def _get(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
    return None


def delegation_chain(delegation: Any) -> dict[str, Any] | None:
    """감사 이벤트의 `delegation` 에서 남길 것. 행위자마다 `sub` 만이고(맨 바깥이 앞), 행위자
    수와 sub 길이를 상한에서 자른다. ToolHive 가 이미 잘랐거나 여기서 잘랐으면 `truncated` 다.
    사슬이 없으면 None 이고, sub 가 글이 아닌 행위자는 None 으로 남아 자리를 지킨다."""
    chain = delegation.get("chain") if isinstance(delegation, dict) else None
    if not isinstance(chain, list) or not chain:
        return None
    subs = [_get(link, "sub") for link in chain[:MAX_DELEGATION_CHAIN]]
    kept = [{"sub": s[:MAX_DELEGATION_SUB] if isinstance(s, str) and s else None} for s in subs]
    cut = len(chain) > MAX_DELEGATION_CHAIN or any(
        isinstance(s, str) and len(s) > MAX_DELEGATION_SUB for s in subs
    )
    return {"chain": kept, "truncated": delegation.get("truncated") is True or cut}


def parse_audit_line(line: str) -> dict[str, Any] | None:
    """감사 줄 하나 → 저장 문서. 감사 이벤트가 아니거나 깨졌으면 None."""
    if '"audit_id"' not in line and '"auditId"' not in line:
        return None
    d = json_object(line)
    if d is None:
        return None
    audit_id = _get(d, "audit_id", "auditId")
    if not isinstance(audit_id, str) or not audit_id:
        return None
    ts = parse_ts(_get(d, "logged_at", "loggedAt")) or parse_ts(d.get("time")) or utcnow()
    subjects = d.get("subjects") if isinstance(d.get("subjects"), dict) else {}
    delegation = delegation_chain(d.get("delegation"))
    target = d.get("target") if isinstance(d.get("target"), dict) else {}
    method = target.get("method")
    event_type = d.get("type")
    tool = None
    if event_type == "mcp_tool_call" or method == "tools/call" or target.get("type") == "tool":
        tool = target.get("name")
    return {
        "_id": audit_id,
        "ts": ts,
        "type": event_type,
        "outcome": d.get("outcome"),
        "component": d.get("component"),
        "sub": _get(subjects, "user_id", "userId"),
        "user": subjects.get("user"),
        "client_name": _get(subjects, "client_name", "clientName"),
        "method": method,
        "tool": tool,
        "target": target or None,
        "subjects": subjects or None,
        # 사람 대신 부른 Agent 는 사슬의 맨 바깥 행위자다 (P53 7절)
        "act": delegation["chain"][0]["sub"] if delegation else None,
        "delegation": delegation,
        "source": d.get("source"),
        "metadata": d.get("metadata"),
    }


def _parse_lines(
    text: str, parse: Callable[[str], dict[str, Any] | None]
) -> tuple[list[dict[str, Any]], datetime | None]:
    """로그 본문 → (문서들, 마지막 이벤트 시각). 줄 파서만 다르고 나머지는 두 종류가 같다."""
    docs: list[dict[str, Any]] = []
    last: datetime | None = None
    for line in text.splitlines():
        doc = parse(line)
        if doc is None:
            continue
        docs.append(doc)
        if last is None or doc["ts"] > last:
            last = doc["ts"]
    return docs, last


def parse_audit_log(text: str) -> tuple[list[dict[str, Any]], datetime | None]:
    """MCP 프록시 로그 본문 → (문서들, 마지막 이벤트 시각)."""
    return _parse_lines(text, parse_audit_line)


def parse_agent_log(text: str, *, salt: str = "") -> tuple[list[dict[str, Any]], datetime | None]:
    """Agent 런타임 로그 본문 → (문서들, 마지막 이벤트 시각)."""
    return _parse_lines(text, partial(parse_agent_line, salt=salt))


class AuditCollector:
    def __init__(
        self,
        store: MongoAuditStore,
        instances: MongoMCPServerStore,
        clusters: MongoClusterStore,
        cluster_service: ClusterService,
        lease: Lease,
        *,
        agents: MongoAgentStore | None = None,
        gateways: MongoGatewayStore | None = None,
        hub: MongoHubStore | None = None,
        telemetry: TelemetryWriter | None = None,
        pushes: MongoAgentTelemetryStore | None = None,
        interval: float = 60.0,
        max_bytes: int = MAX_BYTES,
        overlap_seconds: int = OVERLAP_SECONDS,
    ):
        self.store = store
        self.instances = instances
        # 미리 보기 파드의 네임스페이스도 읽는다 (P44b 9절)
        self.agents = agents.with_previews() if agents is not None else None
        # 회차마다 읽은 미리 보기의 표시 (미리 보기 id → `preview_mark`). 그 파드의 줄을 원본의
        # 이벤트로 넣는다. `targets` 가 회차마다 새로 채운다
        self._previews: dict[str, dict[str, Any]] = {}
        self.gateways = gateways
        self.hub = hub
        # Agent 이벤트를 넣는 곳 (P37 4절). 없으면 Agent 파드를 읽지 않는다
        self.telemetry = telemetry
        # 밀어 보내는 파드의 기록 (P37 4절). 최근에 밀어 보낸 파드의 로그는 읽지 않는다
        self.pushes = pushes
        # 넣지 못해 건너뛴 줄의 수. 그 줄 때문에 파드의 커서가 멈추지 않게 건너뛰고 센다
        self.bad_lines = 0
        # 파드마다 묶음을 하나도 넣지 못한 연속 횟수. STUCK_ATTEMPTS 번이면 그 줄들을 건너뛴다
        self._stuck: dict[str, int] = {}
        self.clusters = clusters
        self.cluster_service = cluster_service
        self.lease = lease
        self.interval = interval
        self.max_bytes = max_bytes
        self.overlap_seconds = overlap_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.runs = 0
        self.last_result: dict[str, Any] = {}

    # -- 대상 --------------------------------------------------------------------

    async def targets(self) -> list[tuple[Any, str, dict[str, tuple[str, str]]]]:
        """(cluster, namespace, {toolhive-name: (tenant, id)}) 목록. 살아 있는 MCP Server·
        Gateway(vMCP)·자기 CR 을 둔 허브 항목(원격 프록시 등)과 Agent 의 네임스페이스 기준이다
        (Agent 만 있는 네임스페이스는 빈 맵). id 는 그 문서의 id 다. 읽은 미리 보기의 표시
        (`_previews`)도 회차마다 새로 채운다. 그 파드의 줄은 원본의 이벤트로 든다 (P44b 9절)."""
        out: list[tuple[Any, str, dict[str, tuple[str, str]]]] = []
        previews: dict[str, dict[str, Any]] = {}
        for cluster in await self.clusters.list():
            by_ns: dict[str, dict[str, tuple[str, str]]] = {}
            # 해제가 배포 쪽을 비우는 사이의 문서는 아직 목록에 들지만 k8s 가 없다
            for inst in await self.instances.live_on_cluster(cluster.id):
                if inst.k8s is None:
                    continue
                by_ns.setdefault(inst.k8s.namespace, {})[inst.k8s.name] = (inst.tenant, inst.id)
            # vMCP 와 원격 프록시도 감사를 낸다. 맵에 없으면 테넌트 없이 저장돼
            # 테넌트 화면에 안 보인다
            if self.gateways is not None:
                for gw in await self.gateways.live_on_cluster(cluster.id):
                    if gw.k8s is None:
                        continue
                    by_ns.setdefault(gw.k8s.namespace, {})[gw.k8s.name] = (gw.tenant, gw.id)
            if self.hub is not None:
                for item in await self.hub.owning_cr_on_cluster(cluster.id):
                    names = by_ns.setdefault(item.k8s.namespace, {})
                    names.setdefault(item.k8s.name, (item.tenant, item.id))
            if self.agents is not None:
                for agent in await self.agents.live_on_cluster(cluster.id):
                    if agent.preview is not None:
                        previews[agent.id] = preview_mark(agent)
                    if agent.k8s is None:
                        continue
                    by_ns.setdefault(agent.k8s.namespace, {})
            for ns, names in sorted(by_ns.items()):
                out.append((cluster, ns, names))
        self._previews = previews
        return out

    # -- 한 바퀴 ------------------------------------------------------------------

    async def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        result: dict[str, Any] = {"namespaces": 0, "pods": 0, "events": 0, "errors": []}
        if not await self.lease.acquire(LEASE_NAME, now=now):
            result["skipped"] = "lease"
            return result
        self.runs += 1
        for cluster, namespace, names in await self.targets():
            try:
                client = await run_in_threadpool(self.cluster_service.client_for, cluster)
            except ClusterUnreachable as e:
                result["errors"].append(f"{cluster.name}: {e}")
                continue

            # 네임스페이스는 한 테넌트의 것이다(namespace_claimed). 맵에 없는 프록시(다른 테넌트가
            # 연 허브 항목의 복제본 등)는 그 테넌트로 적고 id 는 비운다
            owners = {tenant for tenant, _ in names.values()}
            ns_tenant = next(iter(owners)) if len(owners) == 1 else None

            def tenant_of(
                pod: dict[str, Any], _names=names, _owner=ns_tenant
            ) -> tuple[str | None, str | None]:
                labels = (pod.get("metadata") or {}).get("labels") or {}
                hit = _names.get(labels.get(NAME_LABEL, ""))
                return hit if hit else (_owner, None)

            try:
                r = await self.collect_namespace(
                    client, cluster.id, namespace, tenant_of=tenant_of, now=now
                )
            except ClusterUnreachable as e:
                result["errors"].append(f"{cluster.name}/{namespace}: {e}")
                continue
            except Exception as e:  # noqa: BLE001 - 한 네임스페이스의 실패가 루프를 죽이면 안 된다
                # 예외의 글은 남기지 않는다. Mongo 오류의 글에는 문서의 값이 들 수 있다
                logger.warning("audit collect %s/%s: %s", cluster.name, namespace, error_kind(e))
                result["errors"].append(f"{cluster.name}/{namespace}: {error_kind(e)}")
                continue
            result["namespaces"] += 1
            result["pods"] += r["pods"]
            result["events"] += r["events"]
        self.last_result = result
        return result

    async def _collect_pods(
        self,
        client: KubeClient,
        cluster_id: str,
        namespace: str,
        pods: list[dict[str, Any]],
        seen: set[str],
        now: datetime,
        *,
        container: str | None,
        parse: Callable[[str, str], tuple[list[dict[str, Any]], datetime | None]],
        save: Callable[[list[dict[str, Any]]], Awaitable[int]],
        stamp: Callable[[dict[str, Any], dict[str, str]], dict[str, Any] | None],
        save_retry: Callable[[list[dict[str, Any]]], Awaitable[int]] | None = None,
        pushing: frozenset[str] = frozenset(),
        settling: frozenset[str] = frozenset(),
    ) -> tuple[int, int]:
        """파드 목록을 커서부터 읽어 저장한다. (읽은 파드 수, 새로 넣은 이벤트 수).

        MCP 프록시와 Agent 런타임이 이 절차를 그대로 쓴다. `stamp(pod, labels)` 가 문서에
        붙일 출처 필드를 만들고, 이 파드를 건너뛰어야 하면 None 을 돌려준다. 커서 규칙
        (`seen`·`_advance`)을 한쪽만 고치면 로그를 잃으므로 여기 한 벌만 둔다.

        pushing 은 이벤트를 밀어 보내는 파드 uid 다. 그 로그는 읽지 않고 커서만 지금으로 둔다.
        그래야 밀어 보내기가 멈췄을 때 컨테이너가 뜬 때부터 다시 읽지 않는다. settling 은 첫
        push 를 기다리는 파드 uid 다. 읽지도 커서를 두지도 않아, 끝내 push 하지 않으면 다음에
        처음부터 읽는다. save_retry 는 묶음이 실패한 뒤 하나씩 넣을 때 save 대신 부른다."""
        read = 0
        events = 0
        skip = pushing | settling
        for pod in pods:
            meta = pod.get("metadata") or {}
            labels = meta.get("labels") or {}
            if meta.get("name") and meta.get("uid") in skip:
                key = f"{cluster_id}/{namespace}/{meta['name']}"
                seen.add(key)
                if meta["uid"] in pushing:
                    await self.store.set_cursor(key, since=now, pod_uid=meta["uid"])
                continue
            extra = stamp(pod, labels)
            if extra is None or not self._has_logs(pod):
                continue
            name = meta["name"]
            try:
                text, since, uid = await self._read(
                    client, cluster_id, namespace, pod, seen, now, container=container
                )
            except Exception as e:  # noqa: BLE001 - 파드 하나의 실패가 다른 파드의 수집을 막지 않는다
                logger.warning("collector: could not read pod %s: %s", name, error_kind(e))
                continue
            docs, last_ts = parse(text, uid or name)
            for d in docs:
                d.update(
                    cluster_id=cluster_id, namespace=namespace, pod=name, collected_at=now, **extra
                )
            key = f"{cluster_id}/{namespace}/{name}"
            try:
                events += await save(docs)
            except Exception as e:  # noqa: BLE001 - 넣지 못하는 줄이 그 파드의 커서를 막지 않게 한다
                logger.warning("collector: batch of pod %s failed: %s", name, error_kind(e))
                saved, bad = await _save_each(save_retry or save, docs)
                if saved is None:
                    # 하나도 넣지 못했다. 저장소가 흔들리면 다음 폴링이 다시 읽는다. 같은 줄이
                    # STUCK_ATTEMPTS 번 실패하면 그 줄의 문제로 보고 건너뛴다
                    self._stuck[key] = self._stuck.get(key, 0) + 1
                    if self._stuck[key] < STUCK_ATTEMPTS:
                        continue
                    logger.warning("collector: skipping %d lines of pod %s", bad, name)
                    saved = 0
                events += saved
                self.bad_lines += bad
            self._stuck.pop(key, None)
            await self._advance(cluster_id, namespace, name, uid, since, last_ts, now)
            read += 1
        return read, events

    async def collect_namespace(
        self,
        client: KubeClient,
        cluster_id: str,
        namespace: str,
        *,
        tenant_of: TenantOf | None = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """네임스페이스의 프록시 파드와 에이전트 파드 로그를 커서부터 읽어 저장한다. pods·events 는
        두 종류의 합이고 agent_pods·agent_events 가 그중 에이전트 몫이다."""
        now = now or utcnow()
        seen: set[str] = set()

        def proxy_stamp(pod: dict[str, Any], labels: dict[str, str]) -> dict[str, Any] | None:
            kind = labels.get(PROXY_LABEL)
            if kind not in PROXY_KINDS:
                return None
            tenant, instance_id = tenant_of(pod) if tenant_of else (None, None)
            return {"tenant": tenant, "instance_id": instance_id, "proxy_kind": kind}

        agent_pods = await run_in_threadpool(
            client.list, "v1", "Pod", namespace, {LABEL_RESOURCE: AGENT_RESOURCE}
        )
        pushing = await self._pushing(agent_pods, now)
        settling = self._settling(agent_pods, pushing, now)

        def agent_stamp(pod: dict[str, Any], labels: dict[str, str]) -> dict[str, Any] | None:
            instance_id = labels.get(LABEL_ID)
            if not instance_id or self.telemetry is None:
                return None
            # 미리 보기 파드의 줄은 원본의 이벤트다 (P44b 9절)
            stamp = {"tenant": labels.get(LABEL_TENANT), "instance_id": instance_id}
            return {**stamp, **self._previews.get(instance_id, {})}

        total_pods, total_events = await self._collect_pods(
            client,
            cluster_id,
            namespace,
            await run_in_threadpool(client.list, "v1", "Pod", namespace, {"toolhive": "true"}),
            seen,
            now,
            container=None,
            parse=lambda text, _salt: parse_audit_log(text),
            save=self.store.add_mcp_events,
            stamp=proxy_stamp,
            # 실패한 묶음이 이미 넣은 문서도 다시 셀 시간으로 적는다 (P51 3절)
            save_retry=partial(self.store.add_mcp_events, mark_all=True),
        )
        agent_read, agent_events = await self._collect_pods(
            client,
            cluster_id,
            namespace,
            agent_pods,
            seen,
            now,
            container=AGENT_CONTAINER,
            parse=lambda text, salt: parse_agent_log(text, salt=salt),
            save=self._save_agent_events,
            stamp=agent_stamp,
            pushing=pushing,
            settling=settling,
        )
        await self.store.delete_cursors_except(f"{cluster_id}/{namespace}/", seen)
        return {
            "pods": total_pods + agent_read,
            "events": total_events + agent_events,
            "agent_pods": agent_read,
            "agent_events": agent_events,
        }

    async def _pushing(self, pods: list[dict[str, Any]], now: datetime) -> frozenset[str]:
        """최근 `PUSH_FRESHNESS` 안에 이벤트를 밀어 보낸 파드 uid. 그 파드의 로그는 읽지 않는다.
        옛 이미지, 수집 경로가 꺼진 설치, 밀어 보내기가 막힌 파드는 예전처럼 읽는다."""
        if self.pushes is None:
            return frozenset()
        pairs = []
        for pod in pods:
            meta = pod.get("metadata") or {}
            instance_id = (meta.get("labels") or {}).get(LABEL_ID)
            if instance_id and meta.get("uid"):
                pairs.append((instance_id, meta["uid"]))
        return frozenset(await self.pushes.pushed_since(pairs, now - PUSH_FRESHNESS))

    @staticmethod
    def _settling(
        pods: list[dict[str, Any]], pushing: frozenset[str], now: datetime
    ) -> frozenset[str]:
        """push 할 주소를 받았고 agent 컨테이너가 뜬 지 `STARTUP_GRACE` 가 안 됐으며 아직 push 하지
        않은 파드 uid. 이때 로그를 읽으면 곧 push 로 올 이벤트가 로그 경로로 먼저 들어간다."""
        out = set()
        for pod in pods:
            uid = (pod.get("metadata") or {}).get("uid")
            if not uid or uid in pushing:
                continue
            spec = named_item((pod.get("spec") or {}).get("containers"), AGENT_CONTAINER)
            if not any(e.get("name") == EVENTS_URL_ENV for e in spec.get("env") or []):
                continue
            status = named_item((pod.get("status") or {}).get("containerStatuses"), AGENT_CONTAINER)
            running = (status.get("state") or {}).get("running") or {}
            started = parse_iso_utc(running.get("startedAt"))
            if started is not None and started > now - STARTUP_GRACE:
                out.add(uid)
        return frozenset(out)

    async def _save_agent_events(self, docs: list[dict[str, Any]]) -> int:
        """Agent 이벤트를 `TelemetryWriter` 로 넣는다. 원장과 훅은 새로 들어간 것만이 아니라 읽은
        것을 모두 받는다. 실패하면 커서가 나아가지 않아 다음 폴링이 같은 줄을 다시 읽는다."""
        assert self.telemetry is not None  # noqa: S101 - agent_stamp 가 없으면 부르지 않는다
        return await self.telemetry.write(docs)

    @staticmethod
    def _has_logs(pod: dict[str, Any]) -> bool:
        """Pending/ContainerCreating 은 로그가 없다. 이름 없는 파드도 건너뛴다."""
        if not (pod.get("metadata") or {}).get("name"):
            return False
        return (pod.get("status") or {}).get("phase") in (None, "Running", "Succeeded", "Failed")

    async def _read(
        self,
        client: KubeClient,
        cluster_id: str,
        namespace: str,
        pod: dict[str, Any],
        seen: set[str],
        now: datetime,
        *,
        container: str | None = None,
    ) -> tuple[str, datetime | None, str | None]:
        """커서부터(겹침 포함) 파드 로그를 읽는다. (본문, 커서 시각, 파드 uid)."""
        meta = pod.get("metadata") or {}
        name = meta["name"]
        key = f"{cluster_id}/{namespace}/{name}"
        seen.add(key)
        uid = meta.get("uid")
        cursor = await self.store.get_cursor(key)
        since: datetime | None = None
        if cursor and cursor.get("pod_uid") == uid and cursor.get("since"):
            since = cursor["since"]
        since_seconds = (
            None
            if since is None
            else max(1, int((now - since).total_seconds()) + self.overlap_seconds)
        )
        text = await run_in_threadpool(
            client.pod_log,
            name,
            namespace,
            since_seconds=since_seconds,
            container=container,
            limit_bytes=self.max_bytes,
        )
        return text, since, uid

    async def _advance(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        uid: str | None,
        since: datetime | None,
        last_ts: datetime | None,
        now: datetime,
    ) -> None:
        """마지막 이벤트 시각(없으면 이번 폴링 시각)을 커서로. 겹치는 몇 초는 id 가 거른다."""
        new_since = last_ts or since or now
        if last_ts is not None and since is not None and last_ts < since:
            new_since = since
        await self.store.set_cursor(
            f"{cluster_id}/{namespace}/{name}", since=new_since, pod_uid=uid
        )

    # -- 루프 -----------------------------------------------------------------------

    async def _loop(self) -> None:
        await run_periodic(
            self.run_once,
            interval=self.interval,
            stop=self._stop,
            log=logger,
            label="audit collector loop",
        )

    def start(self) -> None:
        if self._task is None:
            # 이벤트는 처음 기다린 루프에 묶인다. 앱을 다시 띄우면 새 루프이므로 새로 만든다
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="daegam-audit-collector")

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None
        await self.lease.release(LEASE_NAME)


async def _save_each(
    save: Callable[[list[dict[str, Any]]], Awaitable[int]], docs: list[dict[str, Any]]
) -> tuple[int | None, int]:
    """묶음이 실패했을 때 문서를 하나씩 넣는다. (넣은 수, 넣지 못한 수). 하나도 넣지 못했으면
    저장소가 흔들리는 것이라 넣은 수가 None 이고 커서는 나아가지 않는다."""
    saved, bad = 0, 0
    for doc in docs:
        try:
            saved += await save([doc])
        except Exception:  # noqa: BLE001 - 그 줄만 건너뛴다
            bad += 1
    if docs and bad == len(docs):
        return None, bad
    return saved, bad
