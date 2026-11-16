"""Gateway 상태 (P72 2.2절). 상태의 틀은 `instance_status.InstanceStatusMixin` 이고 여기는 Gateway
의 훅이다. vMCP CR 과 Entry 목록을 읽어 phase·배지·백엔드 행으로 옮긴다. `GatewayStatusView` 는
서비스가 생성자에서 만들어 가드와 클라이언트 도우미를 콜러블로 넘긴다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from starlette.concurrency import run_in_threadpool

from daegam.auth.cedar import holds_forbid
from daegam.auth.hub_access import revoked_lines
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import LABEL_ID, LABEL_PROFILE_REVISION
from daegam.k8s.templates import TOOLHIVE_API, vmcp_url
from daegam.k8s.templates.common import rolled_out, template_label
from daegam.services.exposure import status_base, unauthenticated_warnings
from daegam.services.hub_images import refused_backend_lines
from daegam.services.instance_status import (
    ROLLOUT_WATCH,
    changed_within,
    condition_rows,
    failed_condition_message,
    read_deployment,
)
from daegam.services.turn_gates import carries_binding, owner_cedar
from daegam.store.gateways import Gateway, GatewayRevision
from daegam.store.hub import MongoHubStore

# 상태 warnings[] 의 접두사. 백엔드 항목의 consume 경계에서 이 테넌트가 빠졌다 (D23)
HUB_ACCESS_REVOKED = "hub_access_revoked"

# 턴 위임 토큰(P53 7절)의 Gateway 경고 둘. 위임은 데이터 플레인이 플랫폼을 믿을 때만(local 모드와
# 플랫폼을 믿는 oidc 모드, P53b 6.1절) Gateway 에 토큰을 싣고 설정이 켜져 있어야 하므로 그때만
# 낸다. 소유자 Cedar 는 그 Gateway 에 토큰을 싣지 않는 까닭이고
# (3.1절), 그 클러스터의 Gateway 게이트도 닫는다(노출하면 모든 클러스터, 3.5절). forbid 는 막지
# 않지만 토큰에 그룹이 없어 그룹으로 가리는 forbid 가 Agent 의 호출을 거절한다(1절)
TURN_TOKENS_OWNER_CEDAR = "turn_tokens_owner_cedar"
TURN_TOKENS_AUTHZ_FORBID = "turn_tokens_authz_forbid"

TURN_TOKENS_OWNER_CEDAR_WARNING = (
    f"{TURN_TOKENS_OWNER_CEDAR}: Cedar saved in Advanced editing replaces the platform's policies, "
    "so Agents on this cluster (every cluster when exposed) call Gateways under their own name"
)
TURN_TOKENS_AUTHZ_FORBID_WARNING = (
    f"{TURN_TOKENS_AUTHZ_FORBID}: the authz policy holds a forbid statement; turn tokens carry no "
    "groups, so a forbid that screens users by group refuses Agent calls"
)


# oidc 모드의 Gateway 경고 (P53b 6.6·6.9절). 소유자 Cedar 가 IdP 그룹이나 client id 로 호출자를
# 가린다. 플랫폼 토큰은 IdP 그룹을 테넌트 이름을 붙인 꼴로만 싣고 client id 를 싣지 않으므로,
# 발급자를 플랫폼으로 바꾸면 그 조건이 맞지 않는다. 바꾸기 전에 고치도록 두 값에서 모두 낸다
IDP_NAMES_IN_POLICY = "idp_names_in_policy"

IDP_NAMES_IN_POLICY_WARNING = (
    f"{IDP_NAMES_IN_POLICY}: the authz policy screens callers by IdP group or client names, which "
    "platform tokens do not carry as written; move those conditions into Tool rules"
)


def turn_token_warnings(doc: Gateway, pinned: GatewayRevision) -> list[str]:
    """위임이 켜졌고 데이터 플레인이 플랫폼을 믿을 때 이 Gateway 가 내는 턴 위임 토큰 경고다
    (P53 7절). 순수
    함수다. forbid 경고는 이 Gateway 가 토큰을 받을 수 있을 때만 낸다. 소유자 Cedar 가 있거나
    묶임을 싣지 않은 Gateway 는 토큰을 받지 않으므로
    (`TurnGrantService._target_cluster` 와 같은 조건) 그 forbid 가 위임을 거절할 일이 없다."""
    out: list[str] = []
    if owner_cedar(doc):
        out.append(TURN_TOKENS_OWNER_CEDAR_WARNING)
    takes_tokens = not out and carries_binding(doc)
    if takes_tokens and holds_forbid(pinned.authz_policy):
        out.append(TURN_TOKENS_AUTHZ_FORBID_WARNING)
    return out


# Degraded 뱃지의 코드. 응답 스키마(api/schemas/gateways.py GatewayStatus.badge_code)도 이것을 쓴다
BadgeCode = Literal["all_backends_down", "some_backends_down"]


# Degraded 뱃지의 코드와 한국어. 화면은 코드로 화면 언어의 말을 그리고(P49 6절),
# 비서의 get_gateway·list_gateways 는 한국어 badge 를 읽는다
DEGRADED_BADGES: dict[BadgeCode, str] = {
    "all_backends_down": "모든 백엔드 불가",
    "some_backends_down": "일부 백엔드 불가",
}


def map_vmcp_phase(
    cr: dict[str, Any], *, expected_backends: int, recent: bool
) -> tuple[str, int | None, int, int | None]:
    """ToolHive VirtualMCPServer status → 플랫폼 phase. 순수 함수.
    (phase, backend_count, generation, observed_generation).

    recent 는 스펙 변경이 ROLLING_GRACE 안이라는 뜻이다. 그동안의 Failed·Degraded 와
    아직 안 맞는 backendCount 는 롤아웃 중으로 본다. MCP 의 `map_cr_status`·Agent 의
    `map_deployment_status` 와 같은 자리다."""
    meta = cr.get("metadata") or {}
    st = cr.get("status") or {}
    phase = st.get("phase") or "Pending"
    generation = meta.get("generation") or 0
    observed = st.get("observedGeneration")
    backend_count = st.get("backendCount")
    if observed is None and not st.get("phase"):
        phase = "Pending"  # 컨트롤러가 아직 status 를 쓰지 않았다
    elif observed is None or generation != observed:
        # phase 는 observedGeneration == generation 일 때만 믿는다
        # (롤아웃 직후 옛 파드 기준 Ready 가 남는다)
        phase = "Rolling"
    elif (
        phase == "Ready"
        and backend_count is not None
        and backend_count != expected_backends
        and recent
    ):
        phase = "Rolling"
    elif phase in ("Failed", "Degraded") and recent:
        phase = "Rolling"
    return phase, backend_count, generation, observed


def vmcp_caught_up(deployment: dict[str, Any], revision: int | None) -> bool:
    """vMCP Deployment 가 배포한 리비전으로 다 굴러갔는가. 파드 템플릿의
    `daegam.devops.ai.kr/profile-revision` 이 그 리비전이고 옛 파드가 남지 않아야 한다. 리비전이
    그대로인 렌더(인증 재적용)와 라벨이 없는 판은 롤아웃만 본다. MCP Server 프록시의
    `proxy_caught_up` 과 같은 자리다."""
    seen = template_label(deployment, LABEL_PROFILE_REVISION)
    if revision is not None and seen is not None and seen != str(revision):
        return False
    return rolled_out(deployment)


def degraded_badge_code(
    phase: str, failure_mode: str, *, all_down: bool = False
) -> BadgeCode | None:
    """화면 뱃지의 코드. best_effort 의 Degraded 는 서빙 중이지만 일부 백엔드가 빠져 있다는 뜻이다.
    백엔드가 모두 빠진 Degraded 는 failure_mode 와 상관없이 따로 알린다."""
    if phase != "Degraded":
        return None
    if all_down:
        return "all_backends_down"
    return "some_backends_down" if failure_mode == "best_effort" else None


def degraded_badge(phase: str, failure_mode: str, *, all_down: bool = False) -> str | None:
    """화면 뱃지의 한국어. `degraded_badge_code` 의 코드를 `DEGRADED_BADGES` 로 옮긴다."""
    code = degraded_badge_code(phase, failure_mode, all_down=all_down)
    return DEGRADED_BADGES[code] if code else None


def backend_rows(
    doc: Gateway, entries: list[dict[str, Any]], discovered: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """백엔드별 행. Entry phase(설정 검증)와 vMCP discoveredBackends(도달 가능성)를 합친다.
    Failed → invalid, Pending → pending, Valid+미발견 → discovering,
    Valid+발견 → 그 status 그대로."""
    entry_by_name = {(e.get("metadata") or {}).get("name"): e for e in entries}
    disc_by_name = {d.get("name"): d for d in discovered}
    rows = []
    for b in doc.deployed_backends:
        e = entry_by_name.get(b.workload)
        est = (e or {}).get("status") or {}
        phase = est.get("phase")
        d = disc_by_name.get(b.workload)
        row: dict[str, Any] = {
            "workload": b.workload,
            "hub_item_id": b.hub_item_id,
            "hub_item_name": b.hub_item_name,
            "kind": b.kind,
            "url": b.url,
            "entry_phase": phase,
            "status": None,
            "consecutive_failures": None,
            "message": None,
        }
        if e is None:
            row["status"] = "missing"
        elif phase == "Failed":
            row["status"] = "invalid"
            row["message"] = failed_condition_message(est.get("conditions") or [])
        elif phase == "Pending":
            row["status"] = "pending"
        elif d is None:
            row["status"] = "discovering"
        else:
            row["status"] = d.get("status") or "unknown"
            row["consecutive_failures"] = d.get("consecutiveFailures")
            row["message"] = d.get("message")
        rows.append(row)
    return rows


class GatewayStatusView:
    """Gateway 의 상태 훅 둘(fields·map)."""

    def __init__(
        self,
        *,
        hub_items: MongoHubStore,
        token_auth: Callable[[], bool],
        turn_tokens_on: Callable[[], bool],
        pinned: Callable[[Gateway], GatewayRevision],
        k8s: Callable[[Gateway], Any],
        with_pod_waiting: Callable[..., Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]],
        web_warnings: Callable[[Gateway], Awaitable[list[str]]],
        refused_digests: Callable[[], Awaitable[frozenset[str]]],
        idp_names: Callable[[Gateway], bool] = lambda _doc: False,
    ):
        self.hub_items = hub_items
        # 거부한 image digest (P55 6절). 배포한 백엔드가 들면 경고다
        self.refused_digests = refused_digests
        # 웹 Tool 의 상태 경고 (`GatewayValidator.exposed_web_warning`, P39b 13절)
        self.web_warnings = web_warnings
        self.token_auth = token_auth
        # 턴 위임이 켜졌고 데이터 플레인이 플랫폼을 믿는가. Gateway 소스가 위임 토큰을 받는 유일한
        # 경우다 (P53 2절, P53b 6.1절)
        self.turn_tokens_on = turn_tokens_on
        self.pinned = pinned
        self.k8s = k8s
        self.with_pod_waiting = with_pod_waiting
        # 소유자 Cedar 가 IdP 이름으로 가리는 oidc 모드의 Gateway 인가 (`idp_names_in_policy`)
        self.idp_names = idp_names

    async def _access_warnings(self, doc: Gateway) -> list[str]:
        """스냅샷 백엔드 중 이 테넌트가 consume 경계에서 빠진 항목 (D23)과 이미지가 거부된 항목
        (P55 6절)."""
        ids = [b.hub_item_id for b in doc.deployed_backends]
        found = await self.hub_items.get_many_unscoped(ids)
        items = [found[i] for i in ids if i in found]
        lines = revoked_lines(HUB_ACCESS_REVOKED, doc.tenant, items)
        if any(i.kind == "image" for i in items):
            lines += refused_backend_lines(items, await self.refused_digests())
        return lines

    async def fields(self, doc: Gateway, observed_at: datetime) -> dict[str, Any]:
        """vMCP CR + Entry 목록 + 노출 상태를 읽는 상태의 키와 읽기 전에 아는 경고(틀은 기반의
        `status`)."""
        out: dict[str, Any] = {
            **status_base(
                doc,
                observed_at,
                warnings=unauthenticated_warnings(doc.exposure, token_auth=self.token_auth()),
                error=None,
            ),
            "url": None,
            "backend_count": None,
            "backends": [],
        }
        out["warnings"].extend(await self._access_warnings(doc))
        out["warnings"].extend(await self.web_warnings(doc))
        pinned = self.pinned(doc)
        if self.turn_tokens_on():
            out["warnings"].extend(turn_token_warnings(doc, pinned))
        if self.idp_names(doc):
            out["warnings"].append(IDP_NAMES_IN_POLICY_WARNING)
        return out

    async def map(
        self,
        doc: Gateway,
        client: KubeClient,
        obj: dict[str, Any],
        out: dict[str, Any],
        observed_at: datetime,
        *,
        listing: bool,
    ) -> dict[str, Any]:
        """읽은 vMCP CR 을 phase·배지·백엔드 행으로 옮긴다. Entry 목록을 함께 읽어 스냅샷의
        백엔드마다 한 행을 만들고, Pending 이면 파드의 대기 이유를 싣는다. listing 은 보지
        않는다."""
        k8s = self.k8s(doc)
        st = obj.get("status") or {}
        expected = len(doc.deployed_backends)
        phase, backend_count, generation, observed = map_vmcp_phase(
            obj,
            expected_backends=expected,
            recent=changed_within(doc.last_spec_change, observed_at),
        )
        message = st.get("message")
        if phase == "Ready" and changed_within(doc.last_spec_change, observed_at, ROLLOUT_WATCH):
            # 스펙을 바꾼 직후에는 오퍼레이터가 observedGeneration 을 먼저 올리고 옛 파드 기준
            # Ready 를 그대로 둔다. vMCP Deployment 가 다 굴러갔는지 보고서야 Ready 라고 답한다
            deployment = await read_deployment(client, k8s.name, k8s.namespace)
            if deployment is not None and not vmcp_caught_up(deployment, doc.deployed_revision):
                phase = "Rolling"
                message = f"vMCP Deployment not yet rolled out to revision {doc.deployed_revision}"
        rev = doc.deployed
        failure_mode = rev.failure_mode if rev else "best_effort"
        # ToolHive 0.47 부터 vMCP 파드는 status.runtime 에만 쓰고 오퍼레이터가 윗단으로 옮겨 적는다.
        # 백엔드가 모두 죽어도 윗단 phase 는 Degraded 이고 런타임 판정(Failed)은 runtime 에만 남는다
        runtime = st.get("runtime") or {}
        all_down = runtime.get("phase") == "Failed" or (backend_count == 0 and expected > 0)
        badge = degraded_badge(phase, failure_mode, all_down=all_down)
        badge_code = degraded_badge_code(phase, failure_mode, all_down=all_down)
        try:
            entries = await run_in_threadpool(
                client.list, TOOLHIVE_API, "MCPServerEntry", k8s.namespace, {LABEL_ID: doc.id}
            )
        except Exception:  # noqa: BLE001 - 403·클러스터 흔들림. 행이 missing 으로 보일 뿐이다
            entries = []
        out.update(
            phase=phase,
            url=vmcp_url(st.get("url"), k8s.name, k8s.namespace),
            message=message,
            badge=badge,
            badge_code=badge_code,
            failure_mode=failure_mode,
            backend_count=backend_count,
            expected_backends=expected,
            # 런타임이 직접 쓴 목록이 먼저다. 윗단은 오퍼레이터 reconcile 한 번만큼 늦는다
            backends=backend_rows(
                doc,
                entries,
                runtime.get("discoveredBackends") or st.get("discoveredBackends") or [],
            ),
            conditions=condition_rows(st),
            generation=generation,
            observed_generation=observed,
        )
        out, _pods = await self.with_pod_waiting(doc, client, out)
        return out
