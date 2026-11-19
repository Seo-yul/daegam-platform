"""노출한 MCP Server 의 vMCP 앞단과 옛 앞단에서 넘겨받기 (P54 5·6·7절, D105).

vMCP 의 게이트(`VMCP_GATES`)를 지나는 클러스터에서 노출한 서버 S 의 앞단은 백엔드 하나의 vMCP 다.
같은 문서(S)가 렌더하고 S 의 MCPServer 가 소유하며 이름은 모두 `front-<k8s.name>` 이다
(`store/exposure.py` `front_name_for`). Ingress·host 선점·인증서·토큰 Secret 은 옛 앞단
(MCPRemoteProxy `expose-<k8s.name>`)의 것을 그대로 쓴다.

- Ingress 가 가리키는 앞단을 먼저 적용하고 그 실패는 S 의 렌더를 실패로 만든다. S 가 뒤따른다.
  다른 앞단의 몫은 S 뒤에 오고 그 실패는 아무것도 실패로 만들지 않는다(`ExposureFronts.finish`).
  그래서 vMCP 앞단을 적용하지 못하는 동안에도 인증과 규칙의 변경이 도는 옛 앞단에 닿는다.
- vMCP 앞단의 Entry 이름은 Gateway 의 Entry 이름과 같은 선점 기록에 S 의 것으로 적고, 적용 전에
  살아 있는 Entry 의 라벨을 본다(`GatewayEntries.claimed_names`, `require_own_names`, D82).
- 게이트를 지나지 못한 클러스터에서 이미 도는 앞단은 오늘의 옛 앞단으로 렌더하고 vMCP 앞단은
  지운다. 새 노출은 그 게이트의 422 로 거절한다(`require_vmcp_front`). 그 꼴로 만들면 보호가 빠지기
  때문이다(D69).
- 옛 앞단이 도는 서버는 S 자신의 렌더가 넘겨받기의 단계(`Step`)를 따라 넘겨받는다. 단계는
  클러스터의 것(옛 앞단, vMCP 앞단의 준비와 지금 백엔드 상태, Ingress 의 대상과 어노테이션)에서
  읽고 저장하지 않는다(`due_step`). 재적용 틱은 클러스터마다 옛 앞단을 한 번에 나열해 그 단계를
  S 의 기대 판에 싣고(`ExposureFronts.observe`, `expected_mark`), 렌더는 닿은 단계를 적는다.
  그래서 vMCP 앞단이 `Pending` 에 머문 서버는 클러스터가 바뀔 때까지 틱마다 다시 렌더되지 않는다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.gates import VMCP_GATES, Gate, cluster_gate, gate_error
from daegam.k8s.labels import LABEL_ID, LABEL_RESOURCE
from daegam.k8s.merge import EXPOSE_PROXY_LOCKED_PATHS, locked_path_strings, vmcp_locked_paths
from daegam.k8s.portforward import bearer_service_client
from daegam.k8s.templates import (
    INGRESS_API,
    TOOLHIVE_API,
    VMCP_PORT,
    proxy_service_url,
    vmcp_service_name,
)
from daegam.k8s.templates.authz import AUTHZ_CONFIG_KIND, authz_config_name, authz_config_ref
from daegam.k8s.templates.common import generation_observed, rolled_out
from daegam.k8s.templates.exposure import EXPOSE_PROXY_PREFIX, expose_proxy_name
from daegam.k8s.templates.svc_token import (
    SVC_TOKEN_REV_ANNOTATION,
    expose_svc_name,
    render_external_auth_config,
)
from daegam.k8s.templates.vmcp import (
    GROUP_ENTRY_KINDS,
    anonymous_incoming_auth,
    anonymous_policies,
    render_entry,
    render_group,
    render_vmcp,
    vmcp_incoming_auth,
)
from daegam.services.advanced_edit import api_error_message
from daegam.services.authz_configs import apply_in_order, authz_verdict
from daegam.services.data_plane_auth import front_rev
from daegam.services.exposure import (
    RESOURCE_EXPOSE_PROXY,
    RESOURCE_MCP_FRONT,
    ExposureReading,
    FrontInputs,
    front_authz_config,
    front_token_secret,
    mcp_expose_docs,
    mcp_fronts,
    mcp_route_docs,
    proxy_front_docs,
    proxy_front_refs,
    serving_front,
    vmcp_front_refs,
)
from daegam.services.gateway_entries import GatewayEntries
from daegam.services.gateway_timeouts import longest_backend_timeout
from daegam.services.instance_base import delete_labeled
from daegam.services.instance_status import read_deployment
from daegam.services.proxy_accounts import drop_runner_binding
from daegam.services.tool_rules import coded_warning
from daegam.store.auth_reapply import next_retry
from daegam.store.base import iso_utc, parse_iso_utc, utcnow
from daegam.store.clusters import Cluster
from daegam.store.exposure import front_name_for

logger = logging.getLogger("daegam.exposure_front")

# vMCP 앞단의 백엔드 실패 판정. 백엔드가 하나라 화면 배지만 가르고 동작은 같다
FRONT_FAILURE_MODE = "best_effort"
# 오퍼레이터가 지금 스펙을 받아들여 파드가 도는 vMCP 의 phase. Failed·Pending 은 검증에 실패했거나
# 그룹이 준비되지 않았거나 준비된 파드가 없다 (ToolHive 0.51.2 virtualmcpserver_controller)
SERVING_PHASES = frozenset({"Ready", "Degraded"})


def front_gate(cluster: Cluster) -> Gate | None:
    """vMCP 앞단의 게이트 가운데 그 클러스터가 처음으로 지나지 못한 것. 모두 지나면 None 이다."""
    return next(
        (gate for f in VMCP_GATES if not (gate := cluster_gate(cluster, f)).ok),
        None,
    )


def vmcp_front_ok(cluster: Cluster) -> bool:
    return front_gate(cluster) is None


def require_vmcp_front(cluster: Cluster, error_cls: type[ServiceError]) -> None:
    """새 노출의 검사. 클러스터가 vMCP 앞단의 게이트 하나라도 지나지 못하면 그 게이트를 `feature`
    에 담은 422 다 (P54 5절)."""
    gate = front_gate(cluster)
    if gate is not None:
        raise error_cls(422, **gate_error(gate))


def vmcp_front_docs(inputs: FrontInputs) -> list[dict[str, Any]]:
    """vMCP 앞단 묶음 (P54 5절). MCPGroup, 헤더 주입 MCPExternalAuthConfig, MCPServerEntry,
    Cedar 를 든 MCPAuthzConfig, VirtualMCPServer 순이고 모두 `front-<k8s.name>` 이다. Entry 는
    서버의 프록시 Service 를 감싸고, vMCP 는 그 Entry 하나를 거르지 않고 접두사 없이(priority)
    낸다. 토큰 Secret 은 옛 앞단과 함께 쓴다(`front_token_secret`)."""
    name = front_name_for(inputs.k8s_name)
    common = {"tenant": inputs.tenant, "namespace": inputs.namespace, "owner": inputs.owner}
    group = render_group(
        **common,
        instance_id=inputs.instance_id,
        name=name,
        description=f"daegam exposure front of {inputs.k8s_name} ({inputs.tenant})",
        resource=RESOURCE_MCP_FRONT,
    )
    auth = render_external_auth_config(
        **common,
        name=name,
        secret_name=expose_svc_name(inputs.k8s_name),
        mode="header",
        resource=RESOURCE_MCP_FRONT,
        id=inputs.instance_id,
    )
    entry = render_entry(
        **common,
        instance_id=inputs.instance_id,
        name=name,
        group=name,
        url=proxy_service_url(inputs.k8s_name, inputs.namespace),
        transport="streamable-http",
        hub_item_id=None,
        external_auth_config=name,
        resource=RESOURCE_MCP_FRONT,
    )
    policies = list(inputs.policies) if inputs.oidc_ref else anonymous_policies()
    authz = front_authz_config(inputs, name, RESOURCE_MCP_FRONT, policies)
    return [group, auth, entry, authz, _render_front_vmcp(inputs, name, policies)]


def _render_front_vmcp(inputs: FrontInputs, name: str, policies: list[str]) -> dict[str, Any]:
    """VirtualMCPServer `front-<k8s.name>`. 들어오는 인증은 옛 앞단의 것(사람 참조와 Cedar, header
    모드면 anonymous 와 permit-all)이고 Cedar 는 `<name>-authz` 를 참조한다. 감사, 서버의 가장 긴
    backend 타임아웃에 틈을 더한 호출 타임아웃(Gateway 백엔드의 상한 없이), 토큰 판 어노테이션,
    레플리카 하나와 프록시의 자원 요청이다. 서버의 배포 리비전 라벨은 CR 에만 달아 서버를 다시
    배포해도 앞단의 파드가 굴러가지 않는다. 표면 forbid 는 없다."""
    ref = authz_config_ref(name)
    if inputs.oidc_ref:
        incoming = vmcp_incoming_auth(
            oidc_ref=inputs.oidc_ref,
            policies=policies,
            groups_claim=inputs.groups_claim,
            authz_ref=ref,
            roles_claim=inputs.roles_claim,
        )
    else:
        incoming = anonymous_incoming_auth(policies, authz_ref=ref)
    vmcp = render_vmcp(
        tenant=inputs.tenant,
        instance_id=inputs.instance_id,
        profile_id=None,
        revision=inputs.revision,
        name=name,
        namespace=inputs.namespace,
        group=name,
        backends=[{"workload": name}],
        failure_mode=FRONT_FAILURE_MODE,
        effective=inputs.effective,
        conflict="priority",
        incoming_auth=incoming,
        pod_annotations={SVC_TOKEN_REV_ANNOTATION: inputs.svc_token_rev},
        audit=True,
        timeouts={name: f"{inputs.timeout_seconds}s"},
        static_backends={name: name},
        resource=RESOURCE_MCP_FRONT,
        template_revision=False,
    )
    if inputs.owner:
        vmcp["metadata"]["ownerReferences"] = [inputs.owner]
    return vmcp


class Step(StrEnum):
    """옛 앞단을 넘겨받는 단계 (P54 6절). 렌더가 닿은 단계를 서버의 판에 표시로 싣는다
    (`front_rev`). switch 와 missing 은 렌더가 적지 않는다. 그 렌더는 drain 이나 wait·held 에
    닿는다. missing 은 vMCP 앞단의 VirtualMCPServer 가 없는 서버이고 렌더가 다시 만든다."""

    WAIT = "wait"
    SWITCH = "switch"
    DRAIN = "drain"
    DELETE = "delete"
    HELD = "held"
    MISSING = "missing"


# 판에 실릴 수 있는 넘겨받기 표시. None 은 넘겨받기가 없다는 뜻이다
FRONT_MARKS: tuple[Step | None, ...] = (None, *Step)
# 옛 앞단이 Ingress 를 쥔 단계
OLD_FRONT_STEPS = frozenset({Step.WAIT, Step.SWITCH, Step.MISSING})
# Ingress 를 vMCP 앞단으로 바꾼 때 (P54 6절). drain 이 이 때부터 잰다
SWITCHED_AT_ANNOTATION = "daegam.devops.ai.kr/front-switched-at"
# vMCP 가 백엔드마다 지금 상태를 토큰 없이 답하는 경로와 한 번 읽는 상한(초)
BACKENDS_HEALTH_PATH = "/api/backends/health"
HEALTH_TIMEOUT_SECONDS = 2.0
# vMCP 가 라우팅하는 백엔드 상태 (ToolHive 0.53 `health.ShouldAdvertise`)
ROUTABLE = frozenset({"healthy", "degraded"})
# vMCP 앞단의 Deployment 가 Available 인 채 이만큼 지나도 백엔드를 라우팅하지 않으면 held 다
HELD_AFTER = timedelta(minutes=10)
# 옛 앞단이 아직 Ingress 를 쥔 서버의 상태 경고 (P54 11절)
FRONT_HANDOVER_PENDING = "front_handover_pending"
# 경고의 까닭. 화면이 `(reason: <까닭>)` 을 이 꼴로만 읽는다 (ui/src/lib/warningText.ts `rawValue`)
REASON_TOKEN = re.compile(r"[A-Za-z_]+")
FRONT_HANDOVER_TEXT = (
    "the external address still goes through the previous front proxy, because the new vMCP "
    "front cannot take over yet"
)


@dataclass(frozen=True)
class FrontState:
    """클러스터에서 읽은 두 앞단. old 는 지워지는 중이 아닌 옛 앞단 MCPRemoteProxy 가 있는가다.
    나머지는 옛 앞단이 있을 때만 읽는다. on_vmcp 는 Ingress 가 vMCP 앞단의 Service 로 가는가,
    switched 는 Ingress 의 `SWITCHED_AT_ANNOTATION` 값 그대로, vmcp 와 deployment 는 vMCP 앞단의
    VirtualMCPServer 와 Deployment 다."""

    old: bool
    on_vmcp: bool = False
    switched: str | None = None
    vmcp: dict[str, Any] | None = None
    deployment: dict[str, Any] | None = None

    @property
    def switched_at(self) -> datetime | None:
        return parse_iso_utc(self.switched)


def live(obj: dict[str, Any] | None) -> bool:
    """객체가 있고 지워지는 중이 아닌가."""
    return obj is not None and not obj["metadata"].get("deletionTimestamp")


async def read_front_state(client: KubeClient, k8s_name: str, namespace: str) -> FrontState:
    proxy = await run_in_threadpool(
        client.get, TOOLHIVE_API, "MCPRemoteProxy", expose_proxy_name(k8s_name), namespace
    )
    if not live(proxy):
        return FrontState(old=False)
    return await handover_state(client, k8s_name, namespace)


async def handover_state(client: KubeClient, k8s_name: str, namespace: str) -> FrontState:
    """옛 앞단이 있는 서버의 넘겨받기 재료. Ingress, vMCP 앞단의 CR 과 Deployment 를 읽는다."""
    front = front_name_for(k8s_name)
    ingress = await run_in_threadpool(client.get, INGRESS_API, "Ingress", k8s_name, namespace)
    vmcp = await run_in_threadpool(client.get, TOOLHIVE_API, "VirtualMCPServer", front, namespace)
    annotations = ((ingress or {}).get("metadata") or {}).get("annotations") or {}
    return FrontState(
        old=True,
        on_vmcp=serving_front(ingress, k8s_name)[0] == "VirtualMCPServer",
        switched=annotations.get(SWITCHED_AT_ANNOTATION),
        vmcp=vmcp,
        deployment=await read_deployment(client, front, namespace),
    )


def front_caught_up(vmcp: dict[str, Any] | None, deployment: dict[str, Any] | None) -> bool:
    """vMCP 앞단이 지금 스펙으로 다 굴러갔는가. 오퍼레이터가 VirtualMCPServer 의 지금 세대를 받아
    파드가 돌고(phase 가 Ready 나 Degraded), 그 Deployment 의 롤아웃이 끝났다. 앞단의 파드
    템플릿에는 리비전 라벨이 없어 Gateway 의 `vmcp_caught_up` 과 달리 CR 의 세대를 본다. 검증에
    실패한 스펙도 세대를 받으므로 phase 를 함께 본다."""
    if vmcp is None or not generation_observed(vmcp):
        return False
    return (vmcp.get("status") or {}).get("phase") in SERVING_PHASES and rolled_out(deployment)


def due_step(state: FrontState, *, drain: timedelta, now: datetime) -> Step | None:
    """클러스터가 보이는 넘겨받기의 단계 (P54 6절 표). 옛 앞단이 없으면 None 이다. Ingress 가 vMCP
    앞단에 있으면 바꾼 때가 drain 보다 젊을 때 drain, 아니면 delete 다. 아니면 vMCP 앞단의
    VirtualMCPServer 가 없을 때 missing, 그 앞단이 지금 스펙으로 다 굴러갔을 때(`front_caught_up`)
    switch, 그 밖은 wait 다."""
    if not state.old:
        return None
    if state.on_vmcp:
        switched = state.switched_at
        return Step.DRAIN if switched is not None and now - switched < drain else Step.DELETE
    if state.vmcp is None:
        return Step.MISSING
    if front_caught_up(state.vmcp, state.deployment):
        return Step.SWITCH
    return Step.WAIT


def serves_vmcp(step: Step | None) -> bool:
    """그 단계에서 Ingress 가 vMCP 앞단을 가리키는가. 옛 앞단이 쥔 단계(wait·switch·missing)가
    아니면 그렇다."""
    return step not in OLD_FRONT_STEPS


def available_for(deployment: dict[str, Any] | None, now: datetime) -> timedelta:
    """Deployment 가 Available 인 채 지난 시간(그 조건의 lastTransitionTime 부터). 아니면 0 이다."""
    for cond in ((deployment or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Available" and cond.get("status") == "True":
            since = parse_iso_utc(cond.get("lastTransitionTime"))
            return now - since if since is not None else timedelta(0)
    return timedelta(0)


async def backend_health(client: KubeClient, namespace: str, front: str) -> str | None:
    """vMCP 앞단이 지금 서버의 백엔드를 보는 상태 (`GET /api/backends/health`, F0.3). 라우팅이 읽는
    상태 감시와 같은 값이다. 2초 안에 읽지 못하면 None 이다. `status.discoveredBackends` 는 30초마다
    쓰는 사본이라 읽지 않는다."""
    try:
        async with bearer_service_client(
            client,
            namespace,
            vmcp_service_name(front),
            VMCP_PORT,
            timeout=HEALTH_TIMEOUT_SECONDS,
        ) as (hc, base_url):
            r = await hc.get(f"{base_url}{BACKENDS_HEALTH_PATH}")
        body = r.json() if r.status_code == 200 else None
    except Exception as e:  # noqa: BLE001 - 읽지 못하면 다음 단계에서 다시 읽는다
        logger.info("backend health of %s/%s unread: %s", namespace, front, e)
        return None
    return backend_status(body, front)


def backend_status(body: Any, workload: str) -> str | None:
    """`/api/backends/health` 본문에서 그 백엔드의 상태. 백엔드가 하나뿐이면 키와 상관없이 그것이다.
    읽을 수 없는 모양이면 None 이다."""
    backends = body.get("backends") if isinstance(body, dict) else None
    if not isinstance(backends, dict):
        return None
    row = backends.get(workload)
    if row is None and len(backends) == 1:
        row = next(iter(backends.values()))
    status = row.get("Status", row.get("status")) if isinstance(row, dict) else None
    return status if isinstance(status, str) else None


async def front_specs(client: KubeClient, front: str, namespace: str) -> tuple[Any, Any] | None:
    """vMCP 앞단의 VirtualMCPServer 와 그 Cedar 설정의 spec. 둘 가운데 하나가 바뀌면 앞단의 파드가
    굴러간다(Cedar 는 오퍼레이터가 설정의 해시로 굴린다). 읽지 못하면 None 이다."""
    try:
        vmcp = await run_in_threadpool(
            client.get, TOOLHIVE_API, "VirtualMCPServer", front, namespace
        )
        authz = await run_in_threadpool(
            client.get, TOOLHIVE_API, AUTHZ_CONFIG_KIND, authz_config_name(front), namespace
        )
    except Exception as e:  # noqa: BLE001 - 읽지 못하면 넘기지 않고 기다린다
        logger.info("vMCP front %s/%s unread: %s", namespace, front, e)
        return None
    return (vmcp or {}).get("spec"), (authz or {}).get("spec")


@dataclass(frozen=True)
class FrontPass:
    """한 렌더가 고른 앞단. step 이 None 이면 다른 앞단의 몫이 없다(옛 앞단이 없거나 vMCP 앞단의
    게이트를 지나지 못한 클러스터)."""

    inputs: FrontInputs
    step: Step | None = None
    state: FrontState = FrontState(old=False)


@dataclass(frozen=True)
class FrontView:
    """재적용 틱이 클러스터 하나에서 읽은 넘겨받기의 재료 (P54 6절). states 는 옛 앞단이 있는
    서버마다(네임스페이스, k8s 이름)의 것이다. ok 는 그 클러스터가 vMCP 앞단의 게이트를
    지나는가다."""

    ok: bool
    states: dict[tuple[str, str], FrontState]
    now: datetime

    def due(self, doc: Any) -> Step | None:
        """노출한 서버 doc 의 단계. 넘겨받을 것이 없으면 None 이다. drain 은 렌더의 것과 같이 배포한
        리비전의 가장 긴 backend 타임아웃이다(`FrontInputs.drain`)."""
        if not self.ok or not doc.exposure.enabled or doc.k8s is None:
            return None
        state = self.states.get((doc.k8s.namespace, doc.k8s.name))
        if state is None:
            return None
        rev = doc.deployed
        drain = timedelta(seconds=longest_backend_timeout(rev)) if rev else timedelta(0)
        return due_step(state, drain=drain, now=self.now)


def held_until(doc: Any) -> datetime | None:
    """held 를 적은 렌더가 다시 보기를 미루는 끝. 그 렌더를 저장한 때(`updated_at`)에 재적용의 실패
    백오프(5분에서 60분, `next_retry`)를 더한다. 재적용 시각(`auth_retry_at`)을 쓰지 않으므로 그
    사이의 인증 변경은 곧바로 다시 렌더된다. 실패 수가 없으면 None 이다."""
    if doc.auth_failures <= 0:
        return None
    return next_retry(doc.updated_at, doc.auth_failures - 1)[0]


def still_held(doc: Any, now: datetime) -> bool:
    until = held_until(doc)
    return until is not None and now < until


def stamped_mark(auth_rev: str | None, unfronted: str | None) -> Step | None:
    """문서에 적힌 판이 실은 넘겨받기 표시. unfronted 는 표시를 얹기 전의 기대 판이다. 그 위의 어느
    표시와도 맞지 않으면(인증 입력이 바뀌었다) None 이다."""
    return next((m for m in FRONT_MARKS if front_rev(unfronted, m) == auth_rev), None)


class ExposureFronts:
    """노출한 MCP Server 의 앞단 적용과 넘겨받기 (P54 5·6절). `MCPServerService` 가 생성자에서
    만든다. entries 는 Gateway 의 Entry 이름과 같은 선점 기록을 쓰는 그 서비스의 것이다."""

    def __init__(
        self,
        entries: GatewayEntries,
        error_cls: type[ServiceError],
        conflict: Callable[..., ServiceError],
    ):
        self.entries = entries
        self.error_cls = error_cls
        # 서비스의 409 k8s_name_conflict 생성 (`BaseInstanceService._name_conflict`)
        self.conflict = conflict
        # 재적용 틱이 클러스터마다 읽은 넘겨받기의 재료 (`observe`). 기대 판이 읽는다
        self._views: dict[str, FrontView] = {}

    # -- 렌더 --------------------------------------------------------------------------------

    async def serve(
        self,
        doc: Any,
        client: KubeClient,
        cluster: Cluster,
        inputs: FrontInputs,
        *,
        svc_token: str | None,
        wait: float,
    ) -> FrontPass:
        """토큰 Secret 과 Ingress 가 가리킬 앞단, Ingress·NetworkPolicy 를 적용한다. 실패는 S 의
        렌더를 실패로 만든다. 게이트를 지나지 못한 클러스터는 오늘의 옛 앞단이고, 이미 돌던 vMCP
        앞단은 지운다(`drop_vmcp`). 옛 앞단이 도는 서버는 넘겨받기의 단계(`due_step`)가 Ingress 의
        앞단을 정한다. wait·switch·missing 은 옛 앞단이고 drain 은 바꾼 때의 어노테이션을 그대로 둔
        vMCP 앞단, delete 는 그 어노테이션을 뺀 vMCP 앞단이다. NetworkPolicy 는 두 종류의 앞단
        파드를 함께 고른다."""
        if not vmcp_front_ok(cluster):
            await self._apply(client, mcp_expose_docs(inputs, svc_token=svc_token), wait)
            await self._soft(doc, self.drop_vmcp(doc, client, inputs), "was not removed")
            return FrontPass(inputs)
        secret = [front_token_secret(inputs, svc_token)] if svc_token else []
        step, state = await self._due(client, inputs)
        route = self._route(inputs, step, state.switched if step is Step.DRAIN else None)
        if not serves_vmcp(step):
            await self._apply(client, [*secret, *proxy_front_docs(inputs), *route], wait)
            return FrontPass(inputs, step, state)
        await self._apply(client, secret, wait)
        await self.apply_vmcp(doc, client, inputs, wait)
        await self._apply(client, route, wait)
        return FrontPass(inputs, step, state)

    async def finish(
        self, doc: Any, client: KubeClient, front: FrontPass, *, wait: float
    ) -> Step | None:
        """S 뒤의 다른 앞단의 몫과 넘겨받기의 다음 단계. 실패해도 S 의 렌더를 실패로 만들지 않는다.
        렌더가 닿은 단계를 돌려준다 (P54 6절 표의 마지막 칸). 다른 앞단의 몫이 실패하면 held 다.
        wait 와 missing 은 vMCP 앞단을 적용하고 기다린다."""
        step, inputs = front.step, front.inputs
        if step is None:
            return None
        if step is Step.DRAIN:
            # 바꾸기 전에 들어온 호출이 옛 앞단에서 끝나도록 그 앞단을 그대로 렌더한다
            await self._soft(doc, self._apply(client, proxy_front_docs(inputs), wait))
            return Step.DRAIN
        if step is Step.DELETE:
            failed = await self._soft(doc, self._delete_old(client, inputs))
            return Step.HELD if failed else None
        if step is Step.SWITCH:
            return await self._switch(doc, client, front, wait)
        if await self._soft(doc, self.apply_vmcp(doc, client, inputs, wait)):
            return Step.HELD
        return Step.WAIT

    async def apply_vmcp(
        self, doc: Any, client: KubeClient, inputs: FrontInputs, wait: float
    ) -> None:
        """vMCP 앞단 묶음을 적용한다. Entry 이름을 S 의 것으로 선점하고 살아 있는 Entry 와 그룹의
        라벨을 본 뒤이고, Cedar 설정은 받아들여질 때까지 기다린 뒤 vMCP 로 간다. 이름을 남이 쥐고
        있으면 409 k8s_name_conflict 이고 이 호출이 새로 넣은 선점은 지운다. force SSA 는 남의
        객체도 덮어쓰고 그 소유자를 S 로 바꾸기 때문이다."""
        name = front_name_for(inputs.k8s_name)
        async with self.entries.claimed_names(doc, [name]):
            await self.entries.require_own_names(doc, client, [name])
            owner = await group_owner(client, name, inputs.namespace)
            if owner not in (None, doc.id):
                holder = f"document {owner}" if owner else "an object the platform did not label"
                raise self.conflict("MCPGroup", inputs.namespace, name, holder)
        await self._apply(client, vmcp_front_docs(inputs), wait)

    async def drop_vmcp(self, doc: Any, client: KubeClient, inputs: FrontInputs) -> None:
        """vMCP 앞단을 지우고 그 Entry 이름을 놓는다. VirtualMCPServer, Cedar 설정, 헤더 주입 설정은
        이름으로, Entry 와 그룹은 S 의 라벨을 단 것만 지운다. 게이트를 지나지 못한 클러스터에서는
        그 앞단을 다시 렌더하지 않으므로, 두면 옛 Cedar 와 앞단 토큰으로 계속 돈다 (P54 5절)."""
        for api_version, kind, name in vmcp_front_refs(inputs.k8s_name):
            await run_in_threadpool(client.delete, api_version, kind, name, inputs.namespace)
        await delete_labeled(client, GROUP_ENTRY_KINDS, inputs.namespace, doc.id)
        await self.entries.release_except(doc, ())

    async def _switch(self, doc: Any, client: KubeClient, front: FrontPass, wait: float) -> Step:
        """vMCP 앞단을 다시 적용하고, 그 적용이 앞단을 바꾸지 않았으며 앞단이 서버의 백엔드를
        라우팅하면 Ingress 를 그리로 돌리고 그 때를 적는다(drain). 한 방향이다. 적용이 앞단의
        VirtualMCPServer 나 Cedar 설정을 바꿨으면 새 파드가 첫 상태 확인을 지날 때까지 wait 다.
        라우팅하지 않거나 읽지 못하면 wait 이고, Deployment 가 Available 인 채 10분이 지났으면
        held 다. 옛 앞단은 그동안 그대로 돈다."""
        inputs = front.inputs
        name = front_name_for(inputs.k8s_name)
        before = await front_specs(client, name, inputs.namespace)
        if await self._soft(doc, self.apply_vmcp(doc, client, inputs, wait)):
            return Step.HELD
        if before is None or await front_specs(client, name, inputs.namespace) != before:
            return Step.WAIT
        status = await backend_health(client, inputs.namespace, name)
        now = utcnow()
        if status in ROUTABLE:
            route = self._route(inputs, Step.DRAIN, iso_utc(now))
            if not await self._soft(doc, self._apply(client, route, wait)):
                logger.info("mcp server %s: Ingress switched to %s", doc.id, name)
                return Step.DRAIN
        held = available_for(front.state.deployment, now) >= HELD_AFTER
        return Step.HELD if held else Step.WAIT

    async def _delete_old(self, client: KubeClient, inputs: FrontInputs) -> None:
        """옛 앞단의 MCPRemoteProxy, Cedar 설정, bearer 설정과 P50 앞에 남은 러너 RoleBinding 을
        지운다. 토큰 Secret 은 vMCP 앞단이 쓰므로 둔다."""
        for api_version, kind, name in proxy_front_refs(inputs.k8s_name):
            await run_in_threadpool(client.delete, api_version, kind, name, inputs.namespace)
        await drop_runner_binding(client, expose_proxy_name(inputs.k8s_name), inputs.namespace)

    @staticmethod
    def _route(
        inputs: FrontInputs, step: Step | None, switched: str | None
    ) -> list[dict[str, Any]]:
        """그 단계의 Ingress·NetworkPolicy. switched 는 Ingress 에 싣는 바꾼 때다. drain 은 읽은 값
        그대로나 이 렌더가 바꾼 때이고, delete 와 넘겨받기가 없는 서버는 싣지 않는다."""
        return mcp_route_docs(
            inputs,
            to_vmcp=serves_vmcp(step),
            fronts=mcp_fronts(inputs.k8s_name, vmcp=True),
            annotations={SWITCHED_AT_ANNOTATION: switched} if switched else None,
        )

    async def _due(self, client: KubeClient, inputs: FrontInputs) -> tuple[Step | None, FrontState]:
        """렌더가 지금 클러스터에서 읽은 넘겨받기의 단계와 그 재료."""
        state = await read_front_state(client, inputs.k8s_name, inputs.namespace)
        return due_step(state, drain=inputs.drain, now=utcnow()), state

    async def rendered(
        self, client: KubeClient, cluster: Cluster, inputs: FrontInputs
    ) -> list[dict[str, Any]]:
        """manifest 가 보이는 노출 묶음(토큰 Secret 없이). 두 앞단이 함께 있는 동안은 둘 다다 (P54
        11절)."""
        if not vmcp_front_ok(cluster):
            return mcp_expose_docs(inputs)
        step, state = await self._due(client, inputs)
        return self._docs(inputs, step, state)

    def unread(self, cluster: Cluster, inputs: FrontInputs) -> list[dict[str, Any]]:
        """클러스터를 읽지 못한 manifest 의 노출 묶음. 두 앞단이 함께 있고 옛 앞단이 Ingress 를 쥔
        꼴(wait)이다."""
        if not vmcp_front_ok(cluster):
            return mcp_expose_docs(inputs)
        return self._docs(inputs, Step.WAIT, FrontState(old=True))

    def _docs(
        self, inputs: FrontInputs, step: Step | None, state: FrontState
    ) -> list[dict[str, Any]]:
        proxy = proxy_front_docs(inputs) if state.old else []
        switched = state.switched if step is Step.DRAIN else None
        return [*proxy, *vmcp_front_docs(inputs), *self._route(inputs, step, switched)]

    # -- 재적용의 기대 판 -----------------------------------------------------------------------

    async def observe(self, cluster: Cluster, client: KubeClient, now: datetime) -> FrontView:
        """재적용 틱이 클러스터마다 부른다. 옛 앞단을 한 번에 나열하고 그 서버마다 Ingress 와 vMCP
        앞단의 VirtualMCPServer, Deployment 를 읽어 둔다. 그 클러스터의 기대 판(`expected_mark`)이
        읽는다. Ingress 는 나열 권한이 없어 옛 앞단이 있는 서버의 것만 이름으로 읽는다. 넘겨받기가
        끝난 클러스터는 나열 하나로 끝난다."""
        ok = vmcp_front_ok(cluster)
        states: dict[tuple[str, str], FrontState] = {}
        if ok:
            for meta in await run_in_threadpool(old_fronts, client):
                k8s_name = meta["name"].removeprefix(EXPOSE_PROXY_PREFIX)
                states[(meta["namespace"], k8s_name)] = await handover_state(
                    client, k8s_name, meta["namespace"]
                )
        view = self._views[cluster.id] = FrontView(ok, states, now)
        return view

    def forget(self, cluster_id: str) -> None:
        """그 클러스터를 읽지 못한 틱. 기대 판은 적힌 표시를 그대로 기대한다."""
        self._views.pop(cluster_id, None)

    def expected_mark(self, doc: Any, unfronted: str | None) -> Step | None:
        """기대 판에 얹을 넘겨받기 표시 (P54 6절). 클러스터가 보이는 단계이고, 렌더가 held 를 적은
        서버는 재적용의 실패 백오프가 지날 때까지 held 다(`held_until`). 이번 틱에 그 클러스터를
        읽지 못했으면 적힌 표시 그대로라 그 서버를 다시 렌더하지 않는다."""
        stamped = stamped_mark(doc.auth_rev, unfronted)
        view = self._views.get(doc.cluster_id)
        if view is None:
            return stamped
        due = view.due(doc)
        if due is not None and stamped is Step.HELD and still_held(doc, view.now):
            return Step.HELD
        return due

    def counted_step(self, doc: Any) -> Step | None:
        """상태 API 가 세는 그 서버의 단계. 클러스터가 보이는 단계이고, 재적용이 미룬 동안은
        held 다. 넘겨받을 것이 없거나 이번 틱에 그 클러스터를 읽지 못했으면 None 이다."""
        view = self._views.get(doc.cluster_id)
        due = view.due(doc) if view is not None else None
        if due is not None and view is not None:
            if doc.waiting(view.now) or still_held(doc, view.now):
                return Step.HELD
        return due

    # -- 상태 --------------------------------------------------------------------------------

    async def pending_reason(
        self, doc: Any, client: KubeClient, cluster: Cluster, reading: ExposureReading
    ) -> str | None:
        """옛 앞단이 아직 Ingress 를 쥔 서버에서 vMCP 앞단이 넘겨받지 못한 까닭 (P54 11절). reading
        은 상태 조회가 읽은 Ingress 의 앞단이다(`read_exposure`). 이름을 남이 쥐었으면 name_taken,
        vMCP 앞단이 없으면 Missing, Cedar 설정이 거절되거나 아직 받아들여지지 않았으면
        authz_invalid·authz_not_ready 이고, 그 밖은 vMCP 앞단의 phase 다. Ready 인데 백엔드를
        라우팅하지 않으면 그 백엔드 상태다. 넘겨받을 것이 없으면 None 이다. name_taken 은 같은
        실패의 코드 k8s_name_conflict 를 뜻한다. 화면의 까닭 읽기(`REASON_TOKEN`)가 숫자를 받지
        않아서다."""
        if not vmcp_front_ok(cluster) or reading.front is None:
            return None
        if reading.front[0] != "MCPRemoteProxy" or not live(reading.front_cr):
            return None
        namespace = doc.k8s.namespace
        name = front_name_for(doc.k8s.name)
        if await self._name_taken(doc, client, name):
            return "name_taken"
        vmcp = await run_in_threadpool(
            client.get, TOOLHIVE_API, "VirtualMCPServer", name, namespace
        )
        if vmcp is None:
            return "Missing"
        authz = await run_in_threadpool(
            client.get, TOOLHIVE_API, AUTHZ_CONFIG_KIND, authz_config_name(name), namespace
        )
        valid, _message = authz_verdict(authz)
        if valid is not True:
            return "authz_invalid" if valid is False else "authz_not_ready"
        return vmcp_reason(vmcp, name)

    async def _name_taken(self, doc: Any, client: KubeClient, name: str) -> bool:
        """vMCP 앞단의 Entry 이름을 다른 문서가 선점했거나 그 라벨로 쓰고 있는가, 그 이름의 그룹이
        남의 것인가."""
        holder = await self.entries.holder(doc, name)
        if holder is not None and holder.resource_id != doc.id:
            return True
        if await self.entries.foreign_names(doc, client, [name]):
            return True
        return await group_owner(client, name, doc.k8s.namespace) not in (None, doc.id)

    # -- 도우미 ------------------------------------------------------------------------------

    async def _apply(self, client: KubeClient, docs: list[dict[str, Any]], wait: float) -> None:
        await apply_in_order(client, docs, self.error_cls, wait=wait)

    @staticmethod
    async def _soft(doc: Any, work: Any, failed: str = "was not applied") -> str | None:
        """다른 앞단의 몫을 돌리고 실패의 까닭을 돌려준다. 서비스 오류는 그 코드이고 그 밖은 API
        서버의 글이다. 성공하면 None 이다. failed 는 로그에 적는 실패의 꼴이다."""
        try:
            await work
        except ServiceError as e:
            reason = e.code
        except Exception as e:  # noqa: BLE001 - 다른 앞단의 실패는 S 의 렌더를 막지 않는다
            reason = api_error_message(e)
        else:
            return None
        logger.warning("mcp server %s: the other exposure front %s: %s", doc.id, failed, reason)
        return reason


def vmcp_reason(vmcp: dict[str, Any], workload: str) -> str:
    """vMCP 앞단의 phase. Ready 인데 그 CR 이 본 백엔드 상태가 라우팅하는 것이 아니면 그 상태다.
    상태 조회의 힌트일 뿐이고 넘겨받기는 그 Service 의 지금 상태를 읽는다(`backend_health`)."""
    st = vmcp.get("status") or {}
    phase = st.get("phase") or "Pending"
    seen = next((b for b in st.get("discoveredBackends") or [] if b.get("name") == workload), {})
    status = seen.get("status")
    return status if phase == "Ready" and status and status not in ROUTABLE else phase


def handover_warning(reason: str) -> str:
    """옛 앞단이 아직 Ingress 를 쥔 서버의 상태 경고 (P54 11절). 끝의 `(reason: <까닭>)` 이 vMCP
    앞단의 phase 나 그 몫이 실패한 까닭의 코드이고, 화면이 그대로 문장에 넣는다. 영문자와 밑줄 밖의
    글이 든 값(ToolHive 가 낸 모르는 phase)은 Unknown 이다."""
    token = reason if REASON_TOKEN.fullmatch(reason) else "Unknown"
    return coded_warning(FRONT_HANDOVER_PENDING, FRONT_HANDOVER_TEXT, f"reason: {token}")


async def group_owner(client: KubeClient, name: str, namespace: str) -> str | None:
    """그 이름의 살아 있는 MCPGroup 이 단 라벨 daegam.devops.ai.kr/id 의 값. 그룹이 없으면 None
    이다. vMCP 앞단의 그룹 이름은 `front-mcp-<name>` 이라는 테넌트의 그룹과 겹칠 수 있다 (P54 5절,
    `names.py` `group_name_for`)."""
    live_group = await run_in_threadpool(client.get, TOOLHIVE_API, "MCPGroup", name, namespace)
    if live_group is None:
        return None
    return ((live_group.get("metadata") or {}).get("labels") or {}).get(LABEL_ID, "")


def old_fronts(client: KubeClient) -> list[dict[str, Any]]:
    """클러스터의 지워지는 중이 아닌 옛 앞단의 metadata. 노출 앞단의 리소스 값으로 고른다."""
    listed = client.list_metadata(
        TOOLHIVE_API, "MCPRemoteProxy", labels={LABEL_RESOURCE: RESOURCE_EXPOSE_PROXY}
    )
    return [i["metadata"] for i in listed if live(i)]


def front_locked_paths(kind: str) -> list[str]:
    """노출 묶음의 문서가 manifest 에 보이는 잠긴 경로. 옛 앞단은 인증 경로가, vMCP 앞단은 노출한
    vMCP 의 목록이 잠기고 나머지는 전부 잠긴다 (P54 11절)."""
    if kind == "MCPRemoteProxy":
        return locked_path_strings(EXPOSE_PROXY_LOCKED_PATHS)
    if kind == "VirtualMCPServer":
        return locked_path_strings(vmcp_locked_paths(exposed=True))
    return ["*"]
