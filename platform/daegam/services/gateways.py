"""Gateway 서비스 (D42). 정의(저장·리비전·복제·미리보기)와 vMCP 배포(배포·재배포·해제·삭제·상태·
게시·편집)가 한 문서 위에서 돈다.

CR 이름은 문서의 `k8s.name`(`gw-<name>`)이다. CR 정리·Entry 조회는 모든 CR 의
`daegam.devops.ai.kr/id: <문서 id>` 라벨로 한다.

상태 여섯 가운데 동작이 되는 상태는 플랜의 상태 표를 따른다. 상태 전이는 스토어의 조건부 갱신
(`transition`)이라 두 요청이 겹치면 하나는 409 state_conflict 다. `record_failure` 가 `failed` 로
옮기는 것은 배포 경로(`deploying`)에서만이다. 해제·삭제 도중의 실패는 `undeploying`·`deleting` 에
`last_error` 로 남고 리컨실이 같은 갈래를 다시 몬다.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.audit.store import MongoAuditStore
from daegam.auth.cedar import (
    GATEWAY_TARGET,
    READ_ONLY_HINT_FORBID,
    consumer_forbid,
    consumer_tools_only,
    delegation_forbid,
    delegation_permit,
    holds_forbid,
)
from daegam.auth.hub_access import (
    Access,
)
from daegam.auth.principal import Principal
from daegam.auth.rbac import ROLES_CLAIM
from daegam.auth.rule_cedar import closing_forbid, consumer_clause, published_fence
from daegam.config.resolve import EffectiveConfig, ProfileRuntime, resolve_effective
from daegam.errors import ServiceError
from daegam.k8s.client import AccessDenied, ClusterUnreachable, KubeClient
from daegam.k8s.gates import Feature, passing_features
from daegam.k8s.labels import stable_hash
from daegam.k8s.merge import locked_path_strings, vmcp_locked_paths
from daegam.k8s.templates import (
    TOOLHIVE_API,
    anonymous_incoming_auth,
    proxy_service_url,
    render_entry,
    render_group,
    render_vmcp,
    vmcp_incoming_auth,
    vmcp_owner_ref,
)
from daegam.k8s.templates.authz import (
    authz_config_ref,
    render_authz_config,
)
from daegam.k8s.templates.common import rolled_out
from daegam.k8s.templates.svc_token import (
    OUTSIDE_HEADER_REV_ANNOTATION,
    RESOURCE_GATEWAY_AUTHZ,
    RESOURCE_GATEWAY_SVC_TOKEN,
    SVC_TOKEN_REV_ANNOTATION,
    TOKEN_BACKEND_KINDS,
    entry_header_objects,
    entry_token_objects,
    render_external_auth_config,
    render_svc_token_secret,
)
from daegam.k8s.templates.vmcp import (
    GROUP_ENTRY_KINDS,
    anonymous_policies,
    published_pins,
    vmcp_cluster_url,
    vmcp_policies,
)
from daegam.services.advanced_edit import AdvancedEditor
from daegam.services.advertised_names import gateway_exposed_name, plan_conflicts
from daegam.services.authz_configs import (
    REAPPLY_AUTHZ_WAIT_SECONDS,
    apply_docs,
    delete_authz_config,
)
from daegam.services.claims import Claims
from daegam.services.clusters import ClusterService
from daegam.services.data_plane_auth import (
    KIND_GATEWAY,
    marked_rev,
    rules_rev,
)
from daegam.services.exposure import (
    ExposureInput,
    ExposureValidationMixin,
    delete_expose_objects,
    exposure_from_input,
    exposure_to_input,
    gateway_expose_docs,
)
from daegam.services.gateway_code_mode import (
    CodeModeState,
    closed_warning,
    code_mode_toolhive_ok,
    hidden_warning,
)
from daegam.services.gateway_entries import GatewayEntries
from daegam.services.gateway_plan import backend_server_id as plan_server_id
from daegam.services.gateway_plan import (
    backend_snapshot_of,
    gateway_k8s_name,
    spec_of_revision,
    surface_forbids,
    tool_names_of,
    vmcp_locked_paths_for,
)
from daegam.services.gateway_status import GatewayStatusView
from daegam.services.gateway_timeouts import (
    call_timeout,
    default_timeout,
    rendered_timeouts,
)
from daegam.services.gateway_tokens import GatewayTokens
from daegam.services.gateway_validation import GatewayValidator
from daegam.services.hub import HubError, HubService, shared_clone_name
from daegam.services.hub_tools import origin_server
from daegam.services.instance_base import (
    EDIT_STATES,
    IN_PROGRESS,
    BaseInstanceService,
    delete_labeled,
)
from daegam.services.instance_status import read_deployment
from daegam.services.issuer_switch import names_idp
from daegam.services.outside_headers import EntryHeaders, HeaderPlan
from daegam.services.published_backends import (
    PublishedBackends,
    as_rules,
    caller_sources,
    fence_names,
)
from daegam.services.references import GATEWAY_IN_USE, ReferenceService
from daegam.services.rule_compile import (
    RuleScope,
    RuleScopes,
    roles_claimed,
    ruled_names,
    rules_digest,
    server_rules_by_name,
    source_forbids,
    sources_digest,
)
from daegam.services.shared_clones import SharedClones
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import TenancyService
from daegam.services.tool_effects import SourceTools, ToolEffectLoader
from daegam.services.tool_rules import (
    RULES_DIGEST_BEHIND,
    RULES_NOT_ENFORCED,
    RULES_OWNER_CEDAR_CLOSED,
    RULES_PENDING,
    RULES_TOOL_MISSING,
    RULES_TOOLHIVE_OLD,
    RuleNotice,
    gateway_holds_rules,
    gateway_rule_keys,
    normalized_gateway_rules,
    notice_of,
    rolling_agents,
    rule_warning,
    rules_toolhive_ok,
)
from daegam.services.turn_gates import owner_cedar
from daegam.store.agents import MongoAgentStore
from daegam.store.exposure_hosts import MongoExposureHostStore
from daegam.store.gateway_entry_names import MongoEntryNameStore
from daegam.store.gateways import (
    DEFINITION_FIELDS,
    DEPLOYMENT_FIELDS,
    Gateway,
    GatewayBackendSnapshot,
    GatewayK8s,
    GatewayRevision,
    GatewaySpec,
    MongoGatewayStore,
    PublishedRender,
)
from daegam.store.hub import KEPT_CLONE_KINDS, PUBLISHED_KIND, HubItem, replicated
from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore
from daegam.store.tool_rules import AnyRule, has_caller_rule, holds_rules
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.gateways")


# 백엔드 하나의 (스냅샷, 규칙 지문의 서버 id, 원래 이름의 규칙) (P52 2절)
BackendRules = tuple[GatewayBackendSnapshot, str, dict[str, AnyRule]]
# 플랫폼이 code mode Gateway 의 vMCP 를 나열한 이름을 기억하는 시간(초)과 Gateway 수 (P56 6.3절).
# 상태 읽기와 목록이 Gateway 마다 나열을 기다리지 않게 한다
VMCP_LISTING_SECONDS = 60.0
VMCP_LISTING_ENTRIES = 1000


class GatewayError(ServiceError):
    pass


def backend_server_id(item: HubItem, tenant: str) -> str | None:
    """`gateway_plan.backend_server_id` 를 이 서비스의 오류로 부른다. Entry 토큰이 같은 꼴로
    받는다(`GatewayTokens`)."""
    return plan_server_id(item, tenant, GatewayError)


class GatewayService(ExposureValidationMixin, BaseInstanceService[Gateway]):
    # Tool 규칙을 컴파일할 테넌트의 사실 (P52 2절). wiring 이 만든 뒤에 단다. 없으면 규칙을
    # 렌더하지 않는다
    rule_scopes: RuleScopes | None = None
    late_hooks: ClassVar[frozenset[str]] = frozenset({"rule_scopes"})
    error_cls = GatewayError
    profile_kind = "gateway"
    claim_kind = "gateway"
    auth_kind = KIND_GATEWAY
    definition_fields = DEFINITION_FIELDS
    deployment_fields = DEPLOYMENT_FIELDS
    k8s_api = TOOLHIVE_API
    k8s_kind = "VirtualMCPServer"
    records_deploy_render = True
    # 다시 렌더하기가 스냅샷으로 이름 계획을 다시 세워 함께 저장한다 (P50 1절, G4). 백엔드
    # 스냅샷은 비어 있던 서버 id 를 채운 것을 저장한다 (9절)
    # 게시된 Gateway 는 그 렌더가 이름 댄 소비자와 규칙 출처를 함께 저장한다 (P57 6.2절)
    consumer_fields = frozenset(
        {
            "deployed_conflict_resolution",
            "tool_overrides",
            "deployed_backends",
            "published_render",
            # 적용한 vMCP 가 code mode 를 실었는가 (P56 6.3절)
            "code_mode_applied",
        }
    )

    def __init__(
        self,
        gateways: MongoGatewayStore,
        hub: HubService,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        references: ReferenceService,
        *,
        mcp_instances: MongoMCPServerStore,
        agents: MongoAgentStore,
        audit: MongoAuditStore,
        shared_clones: SharedClones,
        svc_tokens: SvcTokens,
        claims: Claims,
        exposure_hosts: MongoExposureHostStore,
        entry_names: MongoEntryNameStore,
        tool_effects: ToolEffectLoader,
    ):
        self.gateways = gateways
        # 배포 해제·삭제 가드가 참조를 센다 (P75 2절)
        self.references = references
        # BaseInstanceService 가 쓰는 이름. 합친 스토어가 옛 인스턴스 스토어 프로토콜을 지킨다
        self.instances = gateways
        self.hub = hub
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        # 내부 홉의 서비스 토큰 발급 창구 (D41). wiring 이 준다
        self.svc_tokens = svc_tokens
        # code mode 를 켠 Gateway 마다 플랫폼이 vMCP 를 나열한 이름 (P56 6.3절, `_vmcp_listing`)
        self._listings: TTLCache[str, tuple[frozenset[str] | None]] = TTLCache(
            VMCP_LISTING_SECONDS, max_entries=VMCP_LISTING_ENTRIES
        )
        # P8: host_conflict 검사는 MCP Server 와 Agent 의 host 도 본다(D27, D42). 참조 가드는
        # ReferenceService 가 센다
        self.mcp_instances = mcp_instances
        self.agents = agents
        # 저장된 incomingAuth overrides 를 옮긴 감사 행을 쓴다 (P50 8절)
        self.audit = audit
        # 공유 복제본의 기록과 적용 (P50 1절)
        self.shared_clones = shared_clones
        # 문서 하나의 클러스터 작업을 묶는 클레임 (P66)
        self.claims = claims
        # 공개 host 의 선점 기록 (P70). 노출을 저장하기 전에 host 를 잡는다
        self.exposure_hosts = exposure_hosts
        # Entry 이름의 배포 전 검사·선점·적용 직전 막이 (D82)
        self.entries = GatewayEntries(
            names=entry_names,
            gateways=gateways,
            servers=mcp_instances,
            k8s=self._k8s,
            conflict=self._name_conflict,
        )
        # 상태 훅과 Entry 토큰 객체 (P72 2.2절)
        self.status_view = GatewayStatusView(
            hub_items=hub.store,
            token_auth=lambda: self.token_auth,
            turn_tokens_on=self._turn_tokens_on,
            pinned=self._pinned,
            k8s=self._k8s,
            with_pod_waiting=self._with_pod_waiting,
            web_warnings=lambda doc: self.validator.exposed_web_warning(doc),
            refused_digests=hub.refused_digests,
            idp_names=self._idp_names,
        )
        self.validator = GatewayValidator(
            hub=hub,
            gateways=gateways,
            agents=agents,
            tool_effects=tool_effects,
            token_auth=lambda: self.token_auth,
            error_cls=GatewayError,
            auth_mode=lambda: self.cluster_service.settings.auth_mode,
            rule_scopes=lambda: self.rule_scopes,
            agents_refused=lambda: self.cluster_service.settings.data_plane_trusts_idp,
            platform_trusted=lambda: self.cluster_service.settings.data_plane_trusts_platform,
        )
        # 게시된 Gateway 를 백엔드로 쓰는 두 쪽 (P57 6.1·6.2절)
        self.published = PublishedBackends(
            gateways=gateways,
            hub_items=hub.store,
            trusted=lambda: self.cluster_service.settings.data_plane_trusts_platform,
        )
        # 외부 서버 고정 헤더의 사본 (P86 6.2절)
        self.entry_headers = EntryHeaders(hub_items=hub.store, secrets=hub.outside_headers)
        self.entry_tokens = GatewayTokens(
            svc_tokens=svc_tokens,
            gateways=gateways,
            hub_items=hub.store,
            clone_records=shared_clones.store,
            k8s=self._k8s,
            server_id=backend_server_id,
        )
        # Advanced 편집 (F7a 재사용). 노출된 vMCP 는 incomingAuth 경로까지 잠긴다
        self.editor: AdvancedEditor[Gateway] = AdvancedEditor(
            kind="VirtualMCPServer",
            render=self.render,
            client=self.client_for_doc,
            save=self._save_overrides,
            manifest=self.manifest,
            locked=vmcp_locked_paths_for,
            error_cls=GatewayError,
        )

    # -- 훅 ------------------------------------------------------------------------

    def auth_boundary(self) -> str:
        """응답의 읽기 전용 인증 경계. 플랫폼 auth_mode 가 정한다.

        발급자 있는 토큰 인증이면 그 모드 이름(oidc·local), 아니면 anonymous 다."""
        return self.cluster_service.settings.auth_mode if self.token_auth else "anonymous"

    def _idp_names(self, doc: Gateway) -> bool:
        """oidc 모드에서 이 Gateway 의 소유자 Cedar 가 IdP 의 이름으로 호출자를 가리는가
        (`idp_names_in_policy`, P53b 6.9절). 발급자의 두 값에서 모두 본다."""
        settings = self.cluster_service.settings
        if not settings.idp_login:
            return False
        return names_idp(doc, settings.oidc_groups_claim, settings.oidc_client_id_claim)

    def _turn_tokens_on(self) -> bool:
        """Gateway 가 턴 위임 토큰을 받는가. 위임이 켜졌고 데이터 플레인이 플랫폼을 믿는다 (P53b
        6.1절)."""
        settings = self.cluster_service.settings
        return settings.turn_tokens_enabled and settings.data_plane_trusts_platform

    async def _check_in_use(self, doc: Gateway, action: str) -> None:
        """이 문서를 도구 소스로 쓰는 Agent 가 있으면 내리거나 지울 수 없다. 테넌트를 거르지
        않는다. 다른 테넌트의 참조도 막아야 해서다."""
        await self.references.refuse_any(
            "gateway",
            doc.id,
            GATEWAY_IN_USE,
            GatewayError,
            f"gateway {doc.name} is referenced by other resources; cannot {action}",
        )

    async def _check_published(self, doc: Gateway, action: str) -> None:
        published = await self.hub.store.published_of_instance(doc.id)
        if published:
            raise GatewayError(
                409,
                "published",
                f"gateway {doc.name} is published to the hub; unpublish first (DELETE /hub/{{id}})"
                f" before {action}",
                hub_item_ids=[p.id for p in published],
            )

    # -- 렌더링 ----------------------------------------------------------------------

    async def _incoming_auth(
        self,
        doc: Gateway,
        rev: GatewayRevision,
        features: frozenset[Feature],
        owner: dict[str, Any] | None,
        *,
        code_mode: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """(vMCP incomingAuth, 정책을 실은 MCPAuthzConfig). 토큰 인증 모드면 oidcConfigRef +
        Cedar, header 모드면 anonymous 다. 둘 다 리비전이 끈 표면을 Cedar forbid 로 막는다 (P50
        13절). code_mode 면 맨 뒤에 힌트 forbid 를 더한다 (P56 6.3절). 노출했으면
        oidcConfigRef.resourceUrl 에 external_url 을 넣는다 (D22). 입력은 소비자 판과 같은 함수에서
        온다 (P32 8절).

        G8 게이트를 지난 클러스터는 정책을 `<vMCP 이름>-authz` 에 싣고 참조한다(설정이 None 이
        아니다). 게이트를 지나지 못하면 오늘의 꼴인 인라인이다. CRD 가 authzConfig 와 authzConfigRef
        를 함께 받지 않기 때문이다 (P50 8절 표). 데이터 플레인이 플랫폼을 믿는 모드(local, platform
        인 oidc)에서는 이 Gateway 에 묶인 위임 토큰을 받고 그 밖의 위임 토큰을 막는다 (P53 3.1절,
        P53b 6.1절). IdP 를 믿는 oidc 모드의 그룹 클레임 이름은 인라인이면 authzConfig 에, 참조면 그
        설정에 싣는다(`ConsumerAuth.groups_claim`)."""
        forbids = surface_forbids(rev.surface)
        auth = await self._consumer_auth(doc.tenant)
        target = GATEWAY_TARGET.format(doc.id)
        every, _backends = await self._rule_sources(doc, rev)
        consumer = await self._published_render(doc, every)
        rules, roles = await self._rule_policies(doc, rev, loaded=every, consumer=consumer)
        forbids += rules
        if auth.binds_delegation:
            forbids += published_fence(fence_names(doc), doc.tenant, auth.tenant_group)
        if doc.published_render is not None:
            # 소비자 집합 밖의 vMCP 토큰을 거절하고, 소비자에게는 Tool 호출만 연다 (P57 6.1·6.2절)
            forbids += [
                consumer_forbid(target, doc.published_render.consumers),
                consumer_tools_only(target),
            ]
        roles_claim = ROLES_CLAIM if roles else None
        permit = None
        if auth.token_auth and auth.binds_delegation:
            permit = delegation_permit(target, doc.tenant)
            forbids.append(delegation_forbid(target, doc.tenant))
        if code_mode:
            # 힌트 forbid 는 다른 forbid 뒤에 둔다 (P56 6.3절 둘째 층)
            forbids.append(READ_ONLY_HINT_FORBID)
        if auth.token_auth:
            policies = vmcp_policies(
                tenant_group=auth.tenant_group,
                agent_client_ids=list(auth.agent_client_ids),
                client_id_claim=auth.client_id_claim,
                extra_policy=rev.authz_policy,
                forbids=forbids,
                delegation=permit,
            )
        else:
            policies = anonymous_policies(forbids)
        k8s = self._k8s(doc)
        by_ref = Feature.AUTHZ_REF in features
        ref = authz_config_ref(k8s.name) if by_ref else None
        if auth.token_auth:
            incoming = vmcp_incoming_auth(
                oidc_ref=auth.oidc_ref(doc.exposure.external_url),
                policies=policies,
                groups_claim=auth.groups_claim,
                authz_ref=ref,
                roles_claim=roles_claim,
            )
        else:
            incoming = anonymous_incoming_auth(policies, authz_ref=ref)
        if not by_ref:
            return incoming, None
        config = render_authz_config(
            cr_name=k8s.name,
            namespace=k8s.namespace,
            policies=policies,
            tenant=doc.tenant,
            resource=RESOURCE_GATEWAY_AUTHZ,
            id=doc.id,
            groups_claim=auth.groups_claim,
            owner=owner,
            roles_claim=roles_claim,
        )
        return incoming, config

    # -- 게시된 백엔드 (P57 6.1·6.2절) --------------------------------------------------------

    async def _published_render(self, doc: Gateway, every: list[dict[str, AnyRule]]) -> str | None:
        """이 렌더가 게시된 Gateway 로서 적는 사실. 소비자 집합과 규칙 출처, 그 지문을 문서에
        적고(`published_render`, 배포 쪽 저장이 함께 쓴다) 규칙 있는 Tool 마다 더할 소비자 절을
        돌려준다. 플랫폼을 믿지 않거나, 게시하지 않았고 묶인 토큰도 살아 있을 수 없으면 None 을
        적는다. 그래서 header 모드로 바꾼 뒤의 렌더에는 소비자 forbid 가 남지 않는다."""
        plan = await self.published.plan(doc)
        if plan is None:
            doc.published_render = None
            return None
        sources = caller_sources(every)
        scope = await self._rule_scope(doc)
        digest = sources_digest(doc.id, as_rules(sources), scope) if scope else None
        doc.published_render = PublishedRender(
            consumers=list(plan.consumers), sources=sources, digest=digest
        )
        return consumer_clause(GATEWAY_TARGET.format(doc.id), doc.tenant, plan.consumers, digest)

    async def consumer_notice(self, doc: Gateway, before: GatewaySpec) -> list[str]:
        """이 저장이 소유자 Cedar 에 forbid 문장을 처음 더해, 다음 배포에서 호출이 닫힐 Gateway 의
        이름 (6.2절). 이 Gateway 의 게시 항목을 배포한 백엔드로 쓰는 Gateway 를 위로 따라간
        것이다(`GatewayValidator.consumers`). 플랫폼을 믿지 않으면 빈 목록이다."""
        if not self.published.trusted():
            return []
        if not holds_forbid(doc.current.authz_policy) or holds_forbid(before.authz_policy):
            return []
        above = await self.validator.consumers(doc.id) - {doc.id}
        return sorted(gw.name for gw in (await self.gateways.get_many(above)).values())

    # -- Tool 규칙 (P52 2절) ----------------------------------------------------------------

    async def _backend_rules(self, b: GatewayBackendSnapshot) -> tuple[str, dict[str, AnyRule]]:
        """백엔드의 서버나 항목이 든 규칙을 그 백엔드가 광고하는 원래 이름으로 편 것과 지문의 서버
        id. internal 은 MCP Server 가 배포한 리비전의 규칙(컨테이너 이름), image 는 항목의 규칙이다.
        그 밖의 종류는 규칙이 없다."""
        item = await self.hub.store.get_unscoped(b.hub_item_id)
        if item is None:
            return b.hub_item_id, {}
        if b.kind == "image":
            return item.id, dict(item.tool_rules)
        if b.kind == "internal" and item.source.instance_id:
            server = await self.mcp_instances.get(item.source.instance_id)
            pinned = None
            if server is not None and server.deployed_revision is not None:
                pinned = server.get_revision(server.deployed_revision)
            if pinned is not None:
                return server.id, server_rules_by_name(pinned)
        return b.hub_item_id, {}

    async def _status_fields(self, doc: Gateway, observed_at: Any) -> dict[str, Any]:
        out = await super()._status_fields(doc, observed_at)
        try:
            out["warnings"].extend(await self._rule_warnings(doc))
            out["warnings"].extend(await self.published.warnings(doc))
            out["warnings"].extend(await self._header_warnings(doc))
            out["warnings"].extend(await self._code_mode_warnings(doc))
        except ServiceError:
            # 클러스터 문서가 없으면 상태가 그 줄을 따로 낸다. 규칙 경고는 보탤 것이 없다
            pass
        return out

    async def _rule_warnings(self, doc: Gateway) -> list[str]:
        """이 Gateway 의 Tool 규칙이 낳는 경고 (P52 7절). header 모드, 0.53 아래 클러스터, 소유자
        Cedar, 서버의 바뀐 규칙을 아직 싣지 못한 Entry, 백엔드가 더 이상 내놓지 않는 규칙의 Tool."""
        if doc.state == "draft" or doc.k8s is None:
            return []
        rev = self._pinned(doc)
        every, backends = await self._rule_sources(doc, rev)
        sources = [s for s in every if holds_rules(s.values())]
        out: list[str] = []
        if sources and not self.token_auth:
            out.append(rule_warning(RULES_NOT_ENFORCED))
        cluster = await self._cluster_of(doc)
        if sources and not rules_toolhive_ok(cluster):
            out.append(rule_warning(RULES_TOOLHIVE_OLD))
        if owner_cedar(doc) and any(ruled_names(s) for s in every[1:]):
            out.append(rule_warning(RULES_OWNER_CEDAR_CLOSED))
        if sources and self.token_auth:
            # 새 규칙은 오퍼레이터가 vMCP 를 굴린 뒤에 선다(F0 8번). 그 전에는 앞 규칙이 선다
            k8s = self._k8s(doc)
            try:
                client = await self.client_for_doc(doc)
                deployment = await read_deployment(client, k8s.name, k8s.namespace)
            except ServiceError:
                deployment = None
            if deployment is not None and not rolled_out(deployment):
                out.append(rule_warning(RULES_PENDING))
        # 백엔드 서버에 규칙이 있거나 스냅샷이 지문을 들 때만 테넌트의 사실을 읽는다
        digested = any(
            ruled_names(r) or b.rules_digest or b.bound_gateway_id for b, _sid, r in backends
        )
        scope = await self._rule_scope(doc, cached=True) if digested else None
        if scope is not None and not owner_cedar(doc):
            behind = [
                b.hub_item_name
                for b, server_id, rules in backends
                if b.kind in TOKEN_BACKEND_KINDS
                and rules_digest(server_id, rules, scope) != b.rules_digest
            ]
            behind += [
                b.hub_item_name
                for b, _sid, _rules in backends
                if b.bound_gateway_id and await self.published.current_digest(b) != b.rules_digest
            ]
            behind.sort()
            if behind:
                out.append(rule_warning(RULES_DIGEST_BEHIND, ", ".join(behind)))
        # 규칙을 둔 이름 가운데 백엔드 항목이 지금 내놓지 않는 것. forbid 는 그대로 남는다
        own = {b.hub_item_id: b.tool_rules for b in rev.backends if b.tool_rules}
        if own:
            items = await self.hub.store.get_many_unscoped(list(own))
            resolved = await self.hub.resolve_origins(list(items.values()))
            missing = sorted(
                f"{item.name}/{name}"
                for item_id, item in items.items()
                if (names := tool_names_of(item, resolved))
                for name in own[item_id]
                if name not in names
            )
            if missing:
                out.append(rule_warning(RULES_TOOL_MISSING, ", ".join(missing)))
        return out

    async def want_rev(self, doc: Gateway, ts: Any) -> str | None:
        """인증 입력의 소비자 판에 Tool 규칙의 지문을 얹은 것 (P52 2절, `rules_rev`). 지문은 그
        vMCP 가 지금 컴파일할 규칙과 백엔드 서버의 규칙 지문이다. 서버의 규칙이나 Group Mapping,
        이름 댄 Agent 가 바뀌면 다음 틱에 다시 렌더된다."""
        rev = rules_rev(await super().want_rev(doc, ts), await self._rules_mark(doc))
        if doc.state == "draft" or doc.k8s is None:
            return rev
        # 외부 서버 헤더의 판과 consume 경계 (P86 6.2절), code mode 판정의 지문 (P56 6.3절). 그런
        # 백엔드나 code mode 가 없으면 표시가 없다
        return marked_rev(
            rev,
            **await self.published.marks(doc),
            **await self.entry_headers.marks(doc),
            **await self._code_mode_marks(doc),
        )

    async def _rules_mark(self, doc: Gateway) -> str | None:
        """vMCP 가 지금 컴파일할 규칙과 백엔드 서버 규칙 지문의 지문. 어느 출처도 규칙이 없으면
        테넌트의 사실을 읽기 전에 None 이다. 재적용 틱이 Gateway 마다 묻으므로 백엔드 문서는 한
        번씩만 읽고 테넌트의 사실은 잠깐 기억한 것을 쓴다."""
        if doc.state == "draft" or doc.k8s is None:
            return None
        rev = self._pinned(doc)
        sources, backends = await self._rule_sources(doc, rev)
        ruled = any(holds_rules(s.values()) for s in sources)
        bound = await self.published.bound(doc)
        if not ruled and not bound and not any(ruled_names(r) for _b, _sid, r in backends):
            return None
        forbids, roles = await self._rule_policies(doc, rev, loaded=sources, cached=True)
        scope = await self._rule_scope(doc, cached=True)
        digests: list[str | None] = []
        if scope is not None and not owner_cedar(doc):
            for b, server_id, rules in backends:
                if b.kind in TOKEN_BACKEND_KINDS:
                    digests.append(rules_digest(server_id, rules, scope))
                elif b.kind == PUBLISHED_KIND:
                    # 게시된 Gateway 가 새 지문을 저장하면 다음 틱에 이 Gateway 도 다시 렌더된다.
                    # 사슬 위의 Gateway 는 바로 아래가 다시 렌더된 뒤에 차례로 따라온다
                    digests.append(await self.published.current_digest(b))
        if not forbids and not any(digests):
            return None
        return stable_hash([forbids, roles, digests], 16)

    async def resolve_digests(self, doc: Gateway) -> None:
        """토큰을 받는 백엔드마다 그 서버의 규칙 지문을 스냅샷에 적는다 (P52 2절). Entry 토큰이
        그 지문을 싣고 토큰 객체 이름이 지문을 따른다. 소유자 Cedar 가 있는 Gateway 는 마지막
        렌더로 돌아 서버의 규칙을 실을 수 없으므로 지문을 받지 않는다(`rules_owner_cedar_closed`).
        header 모드와 0.53 아래 클러스터는 서버가 규칙을 렌더하지 않으므로 지문이 없다."""
        scope = await self._rule_scope(doc)
        for b in doc.deployed_backends:
            b.rules_digest = None
            if scope is None or b.kind not in TOKEN_BACKEND_KINDS or owner_cedar(doc):
                continue
            server_id, rules = await self._backend_rules(b)
            b.rules_digest = rules_digest(server_id, rules, scope)
        # 게시된 백엔드는 묶인 Gateway 와, 같은 조건에서 그 Gateway 가 저장한 출처와 지문이다
        # (P57 6.2절)
        await self.published.resolve(doc, compile_rules=scope is not None and not owner_cedar(doc))

    async def _rule_scope(self, doc: Gateway, *, cached: bool = False) -> RuleScope | None:
        """규칙을 컴파일할 사실. 컴파일하지 않을 곳(wiring 없음, header 모드, 0.53 아래)은 None
        이다."""
        if self.rule_scopes is None:
            return None
        scope = await self.rule_scopes.scope(doc.tenant, cached=cached)
        if not scope.enforces_callers or not rules_toolhive_ok(await self._cluster_of(doc)):
            return None
        return scope

    async def _rule_sources(
        self, doc: Gateway, rev: GatewayRevision
    ) -> tuple[list[dict[str, AnyRule]], list[BackendRules]]:
        """(이 vMCP 에 컴파일할 규칙 출처들, 백엔드마다 (스냅샷, 지문의 서버 id, 원래 이름의
        규칙)). 출처는 Gateway 의 규칙 하나와 백엔드 서버·항목마다 하나이고, 이름은 이 Gateway 가
        광고하는 이름이다(prefix 는 `<workload>_<tool>`, priority 는 충돌 override). 백엔드
        문서는 한 번씩만 읽는다."""
        resolution, overrides = doc.deployed_conflict_resolution, doc.tool_overrides
        own = {b.hub_item_id: b.tool_rules for b in rev.backends}
        gateway: dict[str, AnyRule] = {}
        sources: list[dict[str, AnyRule]] = []
        backends: list[BackendRules] = []
        for b in doc.deployed_backends:
            for name, rule in (own.get(b.hub_item_id) or {}).items():
                gateway[gateway_exposed_name(b.workload, name, resolution, overrides)] = rule
            if b.kind == PUBLISHED_KIND:
                # 게시된 Gateway 가 저장한 출처를 이 Gateway 의 이름으로 컴파일한다 (P57 6.2절).
                # 출처는 스냅샷 행이 지문과 함께 읽어 둔 것이다
                for source in as_rules(b.published_sources or []):
                    sources.append(
                        {
                            gateway_exposed_name(b.workload, n, resolution, overrides): r
                            for n, r in source.items()
                        }
                    )
                backends.append((b, b.bound_gateway_id or b.hub_item_id, {}))
                continue
            server_id, rules = await self._backend_rules(b)
            backends.append((b, server_id, rules))
            if rules:
                sources.append(
                    {
                        gateway_exposed_name(b.workload, n, resolution, overrides): r
                        for n, r in rules.items()
                    }
                )
        return [gateway, *sources], backends

    async def _rule_policies(
        self,
        doc: Gateway,
        rev: GatewayRevision,
        *,
        loaded: list[dict[str, AnyRule]] | None = None,
        cached: bool = False,
        consumer: str | None = None,
    ) -> tuple[list[str], bool]:
        """(규칙 forbid, 역할 절이 있는가). 출처마다 따로 forbid 를 짓으므로 출처가 AND 로
        합쳐진다. 0.53 아래(모르는 판 포함) 클러스터는 규칙이 있는 Tool 을 모두 닫는다 (1절). header
        모드는 누가 부를지의 규칙을 렌더하지 않는다. loaded 는 이미 읽은 출처다. consumer 는 게시된
        Gateway 가 규칙 있는 Tool 마다 더 받는 소비자 절이다 (P57 6.2절)."""
        if self.rule_scopes is None:
            return [], False
        every = loaded if loaded is not None else (await self._rule_sources(doc, rev))[0]
        sources = [s for s in every if holds_rules(s.values())]
        if not sources:
            return [], False
        scope = await self.rule_scopes.scope(doc.tenant, cached=cached)
        if not scope.enforces_callers:
            return [], False
        if not rules_toolhive_ok(await self._cluster_of(doc)):
            return closing_forbid(n for s in sources for n in ruled_names(s)), False
        forbids: list[str] = []
        for s in sources:
            for f in source_forbids(s, scope, "vmcp", consumer=consumer):
                if f not in forbids:
                    forbids.append(f)
        callers = [r.callers for s in sources for r in s.values() if has_caller_rule(r)]
        return forbids, roles_claimed(callers, scope)

    def _sibling_stores(self) -> list[Any]:
        return [self.gateways, self.mcp_instances, self.agents]

    def _owner(self, doc: Gateway) -> dict[str, Any] | None:
        """이 문서의 vMCP 를 가리키는 ownerReference. vMCP 를 아직 적용하지 않아 uid 가 없으면
        None 이다."""
        return vmcp_owner_ref(self._k8s(doc).name, doc.k8s_uid) if doc.k8s_uid else None

    def _expose_docs(self, doc: Gateway, effective: EffectiveConfig) -> list[dict[str, Any]]:
        k8s = self._k8s(doc)
        return gateway_expose_docs(
            tenant=doc.tenant,
            instance_id=doc.id,
            k8s_name=k8s.name,
            namespace=k8s.namespace,
            exposure=doc.exposure,
            effective=effective,
            owner=self._owner(doc),
        )

    async def _render_docs(
        self,
        doc: Gateway,
        rev: GatewayRevision,
        effective: EffectiveConfig,
        *,
        owner: dict[str, Any] | None = None,
        svc_tokens: dict[str, str] | None = None,
        definition_id: str | None = None,
        headers: HeaderPlan | None = None,
    ) -> list[dict[str, Any]]:
        """MCPGroup <k8s.name>, 토큰을 받는 Entry 마다 (서비스 토큰 Secret,) MCPExternalAuthConfig,
        백엔드마다 MCPServerEntry 래퍼, VirtualMCPServer 순이다. owner 가 있으면 그룹·Entry·토큰
        객체에 vMCP ownerReference 를 건다 (vMCP 삭제만으로 GC). svc_tokens(Entry 이름 → 토큰)가
        있으면(배포 경로) Secret 문서도 낸다. 편집·미리보기는 토큰 없이 렌더한다(D41).
        `definition_id` 는 라벨 daegam.devops.ai.kr/gateway-profile 의 값이고 미리보기의 가짜 문서만
        따로 준다. 감사처럼 클러스터 게이트가 있는 기능은 게이트를 지난 클러스터에서만 렌더한다 (P50
        2절). headers 는 적용하는 렌더가 미리 세운 외부 서버 헤더의 사본이고(P86 6.2절) 그때만 값의
        Secret 을 낸다. 없으면 지금 계획을 세워 값 없이 렌더한다(편집·미리보기·매니페스트)."""
        k8s = self._k8s(doc)
        features = passing_features(await self._cluster_of(doc))
        docs: list[dict[str, Any]] = [
            render_group(
                tenant=doc.tenant,
                instance_id=doc.id,
                name=k8s.group,
                namespace=k8s.namespace,
                description=f"daegam gateway {doc.name} ({doc.tenant})",
                owner=owner,
            )
        ]
        # 토큰을 받는 Entry 마다 토큰 하나가 그 Entry 가 부르는 서버 하나를 연다 (P50 9절)
        token_objects = entry_token_objects(k8s.name, doc.deployed_backends)
        for workload, svc_name in token_objects.items():
            token = (svc_tokens or {}).get(workload)
            if token:
                docs.append(
                    render_svc_token_secret(
                        name=svc_name,
                        namespace=k8s.namespace,
                        token=token,
                        tenant=doc.tenant,
                        resource=RESOURCE_GATEWAY_SVC_TOKEN,
                        id=doc.id,
                        owner=owner,
                    )
                )
            docs.append(
                render_external_auth_config(
                    name=svc_name,
                    namespace=k8s.namespace,
                    secret_name=svc_name,
                    mode="header",
                    tenant=doc.tenant,
                    resource=RESOURCE_GATEWAY_SVC_TOKEN,
                    id=doc.id,
                    owner=owner,
                )
            )
        # 외부 서버의 고정 헤더 사본 (P86 6.2절). 토큰 객체와 이름이 따로라 토큰을 다루는 코드가
        # 읽지 않는다
        header_values = headers is not None
        if headers is None:
            headers = await self.entry_headers.plan(doc, k8s.name)
        docs += self.entry_headers.docs(
            headers,
            namespace=k8s.namespace,
            tenant=doc.tenant,
            gateway_id=doc.id,
            owner=owner,
            values=header_values,
        )
        auth_configs = {**token_objects, **{w: c.name for w, c in headers.copies.items()}}
        for b in doc.deployed_backends:
            docs.append(
                render_entry(
                    tenant=doc.tenant,
                    instance_id=doc.id,
                    name=b.workload,
                    namespace=k8s.namespace,
                    group=k8s.group,
                    url=b.url,
                    transport=b.transport,
                    hub_item_id=b.hub_item_id,
                    owner=owner,
                    # 우리 MCPServer 와 플랫폼 웹 Tool 로 가는 래퍼에만 토큰을 단다
                    # (ENTRY_TOKEN_KINDS). 사용자가 넣은 주소(remote·entry)와 게시된 vMCP 에는
                    # 안 단다 (D41). header 항목의 entry 는 그 항목의 헤더 사본을 단다 (P86 6.2절)
                    external_auth_config=auth_configs.get(b.workload),
                )
            )
        backends = []
        ruled = {b.hub_item_id for b in rev.backends if holds_rules(b.tool_rules.values())}
        # code mode 는 이 렌더의 판정이 받아들일 때만 싣는다. 아니면 오늘의 렌더다. 힌트
        # forbid 는 code mode 를 실은 vMCP 가 도는 동안 남는다 (P56 6.3절, `_code_mode_render`)
        code_on, hint = await self._code_mode_render(doc, rev)
        for b in doc.deployed_backends:
            ov = doc.tool_overrides.get(b.workload) or {}
            # Gateway 규칙이 이름 댄 백엔드와 code mode 를 켠 Gateway 의 백엔드는 모두 골랐어도
            # 배포 스냅샷의 Tool 만 내놓는다. 백엔드가 이름을 바꾸거나 더한 Tool 은 다시 배포할
            # 때까지 나타나지 않는다 (P52 1절, P56 6.3절 첫째 층)
            names = (b.tools or b.tool_names) if b.hub_item_id in ruled or code_on else b.tools
            if b.kind == PUBLISHED_KIND:
                # 게시된 백엔드는 어느 인증 모드에서든 스냅샷의 이름에서 execute_tool_script 를 뺀
                # 것만 내놓는다. 그 스크립트 안의 호출은 이 Gateway 의 규칙을 지나지 않고, 다시
                # 광고하면 이 Gateway 의 code mode 와도 겹친다 (P57 6.2절, P56 6.3절)
                names = published_pins(b)
            backends.append(
                {
                    "workload": b.workload,
                    # vMCP 는 이름을 바꾼 뒤의 이름으로도 필터를 맞춰 보므로 바꾼 툴은 원래
                    # 이름과 새 이름을 함께 둔다. 어느 쪽 순서로 맞춰 봐도 맞다 (P50 6절)
                    "filter": [*names, *(ov[t] for t in names if t in ov)],
                    "overrides": {tool: {"name": new} for tool, new in ov.items()},
                }
            )
        incoming, authz_config = await self._incoming_auth(
            doc, rev, features, owner, code_mode=hint
        )
        if authz_config is not None:
            # 설정을 먼저 적용하고 받아들여지기를 기다린 뒤 vMCP 를 적용한다 (`_apply_docs`)
            docs.append(authz_config)
        docs.append(
            render_vmcp(
                tenant=doc.tenant,
                instance_id=doc.id,
                profile_id=definition_id or doc.id,
                revision=rev.revision,
                name=k8s.name,
                namespace=k8s.namespace,
                group=k8s.group,
                backends=backends,
                failure_mode=rev.failure_mode,
                effective=effective,
                conflict=doc.deployed_conflict_resolution,
                incoming_auth=incoming,
                replicas=rev.runtime.replicas,
                resources=rev.runtime.resources,
                pod_annotations={
                    SVC_TOKEN_REV_ANNOTATION: doc.svc_token_rev,
                    # 헤더 값을 바꾸면 파드가 굴러간다. 사본이 없는 vMCP 에는 달지 않는다
                    **(
                        {OUTSIDE_HEADER_REV_ANNOTATION: header_rev}
                        if (header_rev := headers.rev())
                        else {}
                    ),
                },
                audit=Feature.AUDIT in features,
                timeouts=rendered_timeouts(doc.deployed_backends, Feature.TIMEOUTS in features),
                # G5 정적 백엔드. 게이트를 지나지 못한 클러스터는 오늘의 꼴(discovered)이다
                # (P50 2절)
                static_backends=(auth_configs if Feature.STATIC_BACKENDS in features else None),
                code_mode=(
                    {"enabled": True, "toolCallTimeout": call_timeout(doc.deployed_backends)}
                    if code_on
                    else None
                ),
            )
        )
        return docs

    async def _default_timeout(self, item: HubItem, server: MCPServer | None) -> int:
        """정의가 정하지 않은 백엔드의 타임아웃. 내부 항목이면 그 MCP Server 의 backend 타임아웃을
        본다 (`services/gateway_timeouts.py` `default_timeout`). server 는 원본 해석이 읽어 둔
        문서이고, 없으면 여기서 읽는다."""
        if server is None and item.kind == "internal" and item.source.instance_id:
            server = await self.mcp_instances.get(item.source.instance_id)
        return default_timeout(item.kind, server)

    async def _snapshot_backends(
        self,
        gateway_name: str,
        rev: GatewayRevision,
        items: dict[str, HubItem],
        *,
        tenant: str,
        namespace: str,
        require_ready: bool,
    ) -> tuple[list[GatewayBackendSnapshot], list[dict[str, Any]]]:
        """백엔드 상태를 읽어 래퍼 URL 을 고정한다. require_ready 면 하나라도 안 Ready 일 때 409.
        Entry 이름(workload)은 `<gateway>-<hub item>` 이다. MCPServerEntry 는 네임스페이스 단위로
        이름이 유일하므로 허브 항목 이름만 쓰면 같은 백엔드를 쓰는 두 게이트웨이가 충돌한다.

        다른 테넌트의 image 항목은 이 테넌트 네임스페이스에 CR 을 복제하므로
        (P6 F3b, NetworkPolicy 가 타 네임스페이스 프록시 접근을 막는다) URL 을 복제본 프록시로
        고정하고 원본 id·복제본 이름을 스냅샷에 남긴다. entry 는 CR 이 없어 원본 URL 그대로이고,
        remote 항목의 행도 프록시를 떼지 못하는 것만 빼고 entry 행이다(`HubService.deploy_form`).
        프록시를 떼지 못하는 remote 항목의 행은 전에 적용한 프록시나 복제본을 그대로 가리킨다.
        플랫폼은 그 둘을 렌더하지 않는다. 그래서 다른 테넌트의 그런 항목은 이 네임스페이스에 전에
        만든 복제본이 있을 때만 쓰고, 없으면 422 다(`SharedClones.require_kept_clone`).
        consume 판정은 validate_spec 이 먼저 했다."""
        snapshots: list[GatewayBackendSnapshot] = []
        report: list[dict[str, Any]] = []
        ok = True
        resolved = await self.hub.resolve_origins(list(items.values()))
        for b in rev.backends:
            item = await self.hub.deploy_form(items[b.hub_item_id])
            # 이름·필터·종류·Entry 이름·서버는 저장 때의 code mode 판정과 같은 행에서 온다
            # (`backend_snapshot_of`). 스냅샷에 베끼는 값도 원본에서 계산한 것이다 (D49). 스냅샷
            # 자체는 적용한 CR 과 짝인 기록이라 그 뒤로 얼어 있는다
            row = backend_snapshot_of(
                gateway_name, b, item, resolved, tenant=tenant, error_cls=GatewayError
            )
            workload, tool_names = row.workload, row.tool_names
            server = origin_server(resolved, item)
            st = await self.hub.status(item, server=server)
            ready = bool(st.get("ready")) and not st.get("error")
            url = st.get("url") or self.hub.backend_url(item)
            if ready and require_ready and item.kind in ("entry", "published"):
                # CR 이 없는 항목은 phase 가 없다. tools/list 로 도달 여부를 본다
                try:
                    listed = await self.hub.reachable_tools(item, url)
                    if not tool_names:
                        tool_names = list(listed)
                except Exception as e:  # noqa: BLE001
                    ready = False
                    st["error"] = f"unreachable: {type(e).__name__}: {e}"
            ok = ok and ready
            report.append(
                {
                    "hub_item_id": item.id,
                    "name": item.name,
                    "workload": workload,
                    "phase": st.get("phase"),
                    "ready": ready,
                    "error": st.get("error"),
                }
            )
            shared_foreign = replicated(item, tenant)
            clone_name = clone_kind = None
            if shared_foreign and item.kind in KEPT_CLONE_KINDS:
                await self.shared_clones.require_kept_clone(
                    item, namespace=namespace, error_cls=GatewayError
                )
            if shared_foreign:
                clone_name = shared_clone_name(item)
                clone_kind = item.k8s.kind
                url = proxy_service_url(clone_name, namespace, remote=item.kind == "remote")
            snapshots.append(
                row.model_copy(
                    update={
                        "url": url,
                        "transport": item.source.transport or "streamable-http",
                        # 도달 확인이 알게 된 이름이다
                        "tool_names": tool_names,
                        "shared_source_id": item.id if shared_foreign else None,
                        "clone_kind": clone_kind,
                        "clone_name": clone_name,
                        "timeout_seconds": b.timeout_seconds
                        or await self._default_timeout(item, server),
                        # 인증이 있는 entry 항목의 종류 (P86 6.2절). 종류는 바뀌지 않는다
                        "auth_kind": item.auth.kind if item.kind == "entry" and item.auth else None,
                    }
                )
            )
        if require_ready and not ok:
            raise GatewayError(
                409,
                "backends_not_ready",
                "all backends must be Ready before deploying the vMCP",
                backends=report,
            )
        return snapshots, report

    async def _effective(self, tenant: str, cluster, rev: GatewayRevision) -> EffectiveConfig:
        return resolve_effective(
            await self.tenancy.platform_settings.get(),
            cluster,
            await self.tenancy.tenant_settings.get(tenant),
            ProfileRuntime(replicas=rev.runtime.replicas, resources=rev.runtime.resources),
        )

    def _plan(self, doc: Gateway, rev: GatewayRevision, warnings: list[str]) -> None:
        """스냅샷으로 충돌 계획을 세워 문서에 적고 경고를 합친다.

        두 진입점(정의 검증·스냅샷 계획)이 같은 상황을 볼 수 있어 이미 있는 경고는 빼고 더한다.
        문구는 `priority_unknown_warning` 한 곳에서 만든다."""
        doc.deployed_conflict_resolution, doc.tool_overrides, plan_warnings = plan_conflicts(
            doc.deployed_backends, rev.conflict_resolution
        )
        warnings.extend(w for w in plan_warnings if w not in warnings)

    # -- code mode (P56 6.3절) -----------------------------------------------------------
    # 판정과 422 는 `GatewayValidator` 의 것이다. 여기는 렌더, 렌더 표시, 상태 경고다

    async def _code_mode_state(self, doc: Gateway, rev: GatewayRevision) -> CodeModeState | None:
        """렌더할 리비전의 code mode 판정 (6.3절 셋째 층). 배포 스냅샷과 지금의 표시로 다시 판정하고
        클러스터의 ToolHive 판을 본다. 리비전이 code mode 를 켜지 않았으면 None 이다. 렌더, 렌더
        표시, 상태 경고가 함께 읽는다. 클러스터 문서를 읽지 못하면 판을 모르는 것과 같다. 표시를
        짓다가 그 문서의 재적용 틱이 멈추지 않게 하려는 것이다."""
        if not rev.code_mode:
            return None
        found, tools = await self.validator.code_mode_verdict(doc, doc)
        try:
            toolhive = code_mode_toolhive_ok(await self._cluster_of(doc))
        except ServiceError:
            toolhive = False
        return CodeModeState(found, toolhive, tools)

    async def _code_mode_render(self, doc: Gateway, rev: GatewayRevision) -> tuple[bool, bool]:
        """(codeMode 를 싣는가, 힌트 forbid 를 싣는가). 정책 설정은 vMCP 보다 먼저 적용되고
        받아들여지기를 기다리므로(`apply_docs`), code mode 를 빼는 렌더도 마지막으로 성공한 vMCP
        적용이 code mode 를 실었으면(`code_mode_applied`) 힌트 forbid 를 둔다. 그 vMCP 적용이
        성공한 뒤의 렌더가 forbid 를 뺀다. 그래서 code mode 를 든 vMCP 가 forbid 없는 정책으로 도는
        때가 없다."""
        state = await self._code_mode_state(doc, rev)
        on = state is not None and state.on
        return on, on or doc.code_mode_applied

    async def _code_mode_marks(self, doc: Gateway) -> dict[str, str]:
        """판에 얹는 code mode 의 표시. 판정의 지문과 까닭이라, 서버나 항목의 표시가 바뀌거나
        ToolHive 판이 바뀌면 다음 틱에 다시 렌더된다(P50 의 속도). 닫는 중(앞 적용이 code mode 를
        실었는데 이번 렌더가 뺀다)이면 그 표시가 붙어, vMCP 적용이 성공해 기록이 바뀐 뒤 한 번 더
        렌더되어 forbid 가 빠진다. code mode 를 켠 적 없는 Gateway 는 표시가 없어 판이 오늘과
        같다."""
        state = await self._code_mode_state(doc, self._pinned(doc))
        if state is None and not doc.code_mode_applied:
            return {}
        mark = state.mark() if state is not None else "off"
        if doc.code_mode_applied and not (state is not None and state.on):
            mark += ":closing"
        return {"code_mode": mark}

    async def _code_mode_warnings(self, doc: Gateway) -> list[str]:
        """code mode 의 상태 경고 (6.3절). 렌더가 code mode 를 뺐으면 그 까닭
        (`code_mode_closed`)이고, 켰으면 플랫폼이 vMCP 를 나열해 힌트 forbid 가 숨긴
        Tool(`code_mode_tools_hidden`)이다."""
        if doc.state == "draft" or doc.k8s is None:
            return []
        state = await self._code_mode_state(doc, self._pinned(doc))
        if state is None:
            return []
        if not state.on:
            return [closed_warning(state)]
        hidden = await self._hidden_by_hint(doc, state.tools)
        return [hidden_warning(hidden)] if hidden else []

    async def _hidden_by_hint(self, doc: Gateway, tools: SourceTools) -> list[str]:
        """광고할 이름 가운데 플랫폼이 vMCP 를 나열했을 때 없는 것. 플랫폼의 나열도 힌트 forbid 를
        만나므로, 서버가 `readOnlyHint: true` 를 알리지 않는 Tool 이 빠진다. 누가 부를지의 규칙이
        있는 이름은 플랫폼에게도 닫힐 수 있어 세지 않는다. 나열을 모르면 빈 목록이다
        (`_vmcp_listing`)."""
        listed = await self._vmcp_listing(doc)
        if listed is None:
            return []
        return [n for n in (tools.names or ()) if n not in listed and n not in tools.ruled]

    async def _vmcp_listing(self, doc: Gateway) -> frozenset[str] | None:
        """플랫폼이 이 Gateway 의 vMCP 를 나열한 이름. 상태 읽기마다 기다리지 않게 Gateway 마다
        `VMCP_LISTING_SECONDS` 동안 기억한다(실패도). header 모드의 vMCP 는 토큰 없이 받고, 토큰
        인증은 플랫폼이 vMCP 를 부르는 토큰(`SvcTokens.vmcp_bearer`)을 싣는다. 그 토큰을 낼 수
        없거나(IdP 를 믿는 oidc) 나열이 실패하면 None 이다. 토큰은 로그에 남기지 않는다."""
        if (hit := self._listings.get(doc.id)) is not None:
            return hit[0]
        listed: frozenset[str] | None = None
        try:
            headers: dict[str, str] = {}
            tokens = self.svc_tokens
            if self.token_auth:
                bearer = None
                if tokens is not None and tokens.available:
                    bearer = await tokens.vmcp_bearer(doc.tenant)
                if bearer is None:
                    raise LookupError("the platform cannot call this vMCP")
                headers = {"Authorization": f"Bearer {bearer}"}
            k8s = self._k8s(doc)
            client = await self.client_for_doc(doc)
            url = vmcp_cluster_url(k8s.name, k8s.namespace)
            listed = frozenset(await self.hub.tool_lister(client, url, headers))
        except Exception as e:  # noqa: BLE001 - 나열하지 못하면 경고를 보탤 수 없다
            logger.debug("could not list the vMCP of gateway %s: %s", doc.id, type(e).__name__)
        self._listings.put(doc.id, (listed,))
        return listed

    @staticmethod
    def _carries_code_mode(vmcp: dict[str, Any]) -> bool:
        """적용한 vMCP 가 code mode 를 실었는가."""
        return "codeMode" in ((vmcp.get("spec") or {}).get("config") or {})

    # -- 정의: 저장·리비전·복제·미리보기 ------------------------------------------------------

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        display_name: str,
        spec: GatewaySpec,
        principal: Principal,
        user_sub: str | None,
        exposure: ExposureInput | None = None,
    ) -> tuple[Gateway, list[str]]:
        """정의만 받아 draft r1 을 만든다. 클러스터는 건드리지 않는다. (문서, 검증 경고)."""
        if await self.gateways.get_by_name(tenant, name) is not None:
            raise GatewayError(409, "name_conflict", f"gateway {name!r} already exists")
        spec = normalized_gateway_rules(spec)
        items, warnings = await self.validator.validate_spec(
            tenant, spec, name=name, principal=principal
        )
        doc = Gateway(
            tenant=tenant,
            name=name,
            display_name=display_name,
            spec=spec,
            revision=1,
            revisions=[GatewayRevision(revision=1, **spec.model_dump())],
            exposure=exposure_from_input(exposure),
            updated_by=user_sub,
        )
        if spec.code_mode:
            sample = await self.validator.definition_sample(
                doc.id, tenant, name, doc.current, items
            )
            await self.validator.require_code_mode(doc, sample)
        try:
            await self.gateways.create(doc)
        except DuplicateKeyError as e:
            raise GatewayError(409, "name_conflict", f"gateway {name!r} already exists") from e
        return doc, warnings

    async def save(
        self,
        doc: Gateway,
        *,
        display_name: str,
        spec: GatewaySpec,
        principal: Principal,
        user_sub: str | None,
    ) -> tuple[Gateway, list[str]]:
        """전체 교체 = 새 리비전. 배포 핀(`deployed_revision`)과 상태는 그대로다. 진행 중에는
        막는다. (문서, 검증 경고)."""
        if doc.state in IN_PROGRESS:
            self._require(doc, frozenset(), action="save")
        spec = normalized_gateway_rules(spec)
        if gateway_holds_rules(spec) and owner_cedar(doc):
            # 소유자 Cedar 가 있는 Gateway 는 마지막 렌더로 돌아 규칙을 실을 수 없다 (P52 1절)
            raise GatewayError(
                422,
                "rules_owner_cedar",
                "this Gateway runs Cedar saved in its overrides, so Tool rules cannot apply",
            )
        items, warnings = await self.validator.validate_spec(
            doc.tenant,
            spec,
            name=doc.name,
            principal=principal,
            self_id=doc.id,
            before=spec_of_revision(doc.current),
        )
        if spec.code_mode:
            rev = GatewayRevision(revision=doc.revision + 1, **spec.model_dump())
            sample = await self.validator.definition_sample(
                doc.id, doc.tenant, doc.name, rev, items
            )
            await self.validator.require_code_mode(doc, sample)
        before = doc.revision
        doc.display_name = display_name
        doc.spec = spec
        doc.revision += 1
        doc.revisions.append(GatewayRevision(revision=doc.revision, **spec.model_dump()))
        doc.updated_by = user_sub
        await self._save_definition(doc, expect_revision=before, action="save")
        return doc, warnings

    async def rules_notice(self, doc: Gateway, before: GatewaySpec) -> RuleNotice:
        """저장 전의 정의 before 와 지금 리비전을 견준 알림 (P52 1절)."""
        after = gateway_rule_keys(spec_of_revision(doc.current))
        notice = notice_of(gateway_rule_keys(before), after)
        if self.cluster_service.settings.data_plane_trusts_platform:
            notice.rolling_agents = await rolling_agents(
                self.agents,
                self.rule_scopes,
                doc.tenant,
                after=after.values(),
                before=gateway_rule_keys(before).values(),
            )
        return notice

    async def clone(
        self,
        doc: Gateway,
        *,
        name: str,
        display_name: str,
        exposure: ExposureInput | None,
        principal: Principal,
        user_sub: str | None,
    ) -> tuple[Gateway, list[str]]:
        """현재 리비전의 정의를 베낀 draft r1. `exposure` 를 주면 그 값이 배포 설정으로 남는다.
        한 정의로 둘을 띄우는 자리를 대신한다 (D42)."""
        return await self.create(
            doc.tenant,
            name=name,
            display_name=display_name,
            spec=spec_of_revision(doc.current),
            principal=principal,
            user_sub=user_sub,
            exposure=exposure,
        )

    async def preview(
        self,
        doc: Gateway,
        rev: GatewayRevision,
        cluster_id: str | None,
        *,
        principal: Principal,
    ) -> dict[str, Any]:
        """이 리비전을 배포하면 생길 문서 묶음. apply 하지 않는다. draft 에서도 된다."""
        items, warnings = await self.validator.validate_spec(
            doc.tenant, spec_of_revision(rev), name=doc.name, principal=principal, self_id=doc.id
        )
        cluster = await self.tenancy.resolve_cluster(doc.tenant, cluster_id)
        effective = await self._effective(doc.tenant, cluster, rev)
        if doc.k8s is not None and doc.cluster_id == cluster.id:
            # 배포된 Gateway 는 재배포가 쓰는 네임스페이스로 본다. 복제본 검사가 재배포와 같아진다
            effective.namespace.name = doc.k8s.namespace
        name = gateway_k8s_name(doc.name)
        sample = Gateway(
            id=f"preview-{doc.id}",
            tenant=doc.tenant,
            name=doc.name,
            spec=spec_of_revision(rev),
            revision=rev.revision,
            revisions=[rev],
            state="deploying",
            deployed_revision=rev.revision,
            cluster_id=cluster.id,
            k8s=GatewayK8s(name=name, namespace=effective.namespace.name, group=name),
            effective_config=effective.model_dump(mode="json"),
        )
        sample.deployed_backends, report = await self._snapshot_backends(
            doc.name,
            rev,
            items,
            tenant=doc.tenant,
            namespace=effective.namespace.name,
            require_ready=False,
        )
        self._plan(sample, rev, warnings)
        await self.validator.require_code_mode(doc, sample)
        docs = await self._render_docs(sample, rev, effective, definition_id=doc.id)
        return {
            "cluster_id": cluster.id,
            "namespace": effective.namespace.name,
            "manifest": docs[-1],
            "documents": docs,
            "backends": report,
            "conflict_resolution": sample.deployed_conflict_resolution,
            "tool_overrides": sample.tool_overrides,
            "warnings": warnings,
            "locked": locked_path_strings(vmcp_locked_paths(False)),
        }

    # -- 배포 --------------------------------------------------------------------------

    async def deploy(
        self,
        doc: Gateway,
        *,
        cluster_id: str | None,
        namespace: str | None = None,
        exposure: ExposureInput | None = None,
        exposure_sent: bool = False,
        principal: Principal,
    ) -> tuple[Gateway, list[str]]:
        """검증과 백엔드 스냅샷을 거쳐 배포 쪽을 채워 `deploying` 으로 저장한다. 적용은
        `provision` 이다. (문서, warnings). `exposure` 를 보내지 않으면 문서에 남은 설정(복제가
        받은 것)을 쓴다."""
        self._require(doc, {"draft"}, action="deploy")
        tenant = doc.tenant
        rev = doc.current
        items, warnings = await self.validator.validate_spec(
            tenant, spec_of_revision(rev), name=doc.name, principal=principal, self_id=doc.id
        )
        cluster, ns_name = await self._resolve_target(tenant, cluster_id, namespace)
        self.validator.require_same_cluster(items, cluster.id)
        effective = await self._effective(tenant, cluster, rev)
        effective.namespace.name = ns_name
        snapshots, _report = await self._snapshot_backends(
            doc.name, rev, items, tenant=tenant, namespace=ns_name, require_ready=True
        )
        cr_name = gateway_k8s_name(doc.name)
        k8s = GatewayK8s(name=cr_name, namespace=ns_name, group=cr_name)
        await self._require_name_free(doc, cluster.id, k8s.namespace, k8s.name)
        await self.entries.require_free(doc, cluster.id, ns_name, snapshots)
        inp = exposure if exposure_sent else exposure_to_input(doc.exposure)
        exposure_snapshot, expose_warnings = await self._validate_exposure(
            inp, tenant=tenant, name=doc.name, cluster=cluster, namespace=ns_name
        )
        warnings += await self.validator.check_backends(
            items, exposed=exposure_snapshot.enabled, gateway=doc
        )
        warnings.extend(expose_warnings)
        doc.deployed_backends = snapshots
        self._stamp_deploying(
            doc,
            cluster=cluster,
            k8s=k8s,
            effective=effective,
            exposure=exposure_snapshot,
            revision=rev.revision,
        )
        self._plan(doc, rev, warnings)
        # `_stamp_deploying` 이 메모리에 얹은 노출을 판정이 읽는다
        await self.validator.require_code_mode(doc, doc, cluster=cluster)
        warnings += await self.validator.pinned_names_warning(doc)
        async with self._host_claimed(doc), self.entries.claimed(doc):
            await self._transition(doc, {"draft"}, action="deploy")
        return doc, warnings

    async def _provision(self, doc: Gateway) -> None:
        """적용이 실패하면 적용하지 못한 Entry 이름의 선점을 놓고 실패를 그대로 올린다. 클레임을
        잃은 멈춤(`LeaseStop`)은 실패가 아니라 놓지 않는다."""
        try:
            await self._provision_steps(doc)
        except Exception:
            await self.entries.release_unapplied(doc, self.client_for_doc)
            raise

    async def _provision_steps(self, doc: Gateway) -> None:
        cluster = await self._cluster_of(doc)
        k8s = self._k8s(doc)
        rev = self._pinned(doc)
        effective = EffectiveConfig.model_validate(doc.effective_config)
        # 저장된 overrides 가 지금 잠긴 경로를 건드리면 복제본·토큰을 만들기 전에 멈춘다
        await self.editor.check_stored_rendered(doc)
        await self.tenancy.ensure_namespace(doc.tenant, cluster, effective.namespace)
        client: KubeClient = await run_in_threadpool(self.cluster_service.client_for, cluster)
        # Entry 이름을 다시 선점하고(실패해서 놓은 이름을 리컨실이 다시 몰 때) 남의 Entry 를
        # 덮어쓰지 않는지 본다. 둘 다 무엇이든 쓰기 전이다 (D82)
        await self.entries.claim(doc)
        await self.entries.require_own(doc, client)
        # 다른 테넌트 shared 항목의 복제본을 먼저 만든다 (Entry 가 그 프록시를 가리킨다)
        await self.shared_clones.apply_for(doc, client, k8s=self._k8s(doc), error_cls=GatewayError)
        # 내부 백엔드에 낼 서비스 토큰 (D41). Entry 마다 그 서버의 audience 를 싣고(P50 9절),
        # 있으면 그대로, 없거나 audience 가 다르면 새로 낸다. 지금 렌더가 쓰지 않게 된 토큰 객체는
        # 옛 vMCP 파드가 아직 싣고 있을 수 있어 여기서 지우지 않는다 (`settle_token_objects`)
        await self.entry_tokens.resolve_servers(doc)
        await self.resolve_digests(doc)
        svc_tokens = await self.entry_tokens.issue(doc, client)
        # 외부 서버 헤더의 사본은 렌더할 때마다 관리 Secret 에서 베낀다 (P86 6.2절)
        headers = await self.entry_headers.plan(doc, k8s.name)
        docs = await self._render_docs(doc, rev, effective, svc_tokens=svc_tokens, headers=headers)
        applied = await apply_docs(
            client, docs, self.editor.stored_final(doc, docs[-1]), GatewayError
        )
        # vMCP 적용이 성공했다. 다음 렌더가 힌트 forbid 를 둘지 이것으로 정한다 (P56 6.3절)
        doc.code_mode_applied = self._carries_code_mode(docs[-1])
        uid = (applied.get("metadata") or {}).get("uid")
        doc.k8s_uid = uid
        if uid:
            # uid 를 얻었으니 그룹·Entry·토큰 객체에 ownerReference 를 얹어 재적용
            # (vMCP 삭제만으로 GC)
            owner = vmcp_owner_ref(k8s.name, uid)
            owned = await self._render_docs(
                doc, rev, effective, owner=owner, svc_tokens=svc_tokens, headers=headers
            )
            # 마지막 문서는 방금 적용한 vMCP 자신이다. 앞의 문서들은 내용이 같고 ownerReference 만
            # 더해지므로, MCPAuthzConfig 도 Valid 를 다시 기다리지 않고 그대로 적용한다
            for rendered in owned[:-1]:
                await run_in_threadpool(client.apply, rendered)
        await self._drop_left_out_headers(doc, client, headers)
        # 노출 (Ingress, NetworkPolicy). 껐으면 이름으로 정리한다
        if doc.exposure.enabled:
            await self._apply_exposure(doc, client, effective)
        else:
            await run_in_threadpool(
                delete_expose_objects, client, k8s.name, k8s.namespace, front_proxy=False
            )

    async def settle_token_objects(self, doc: Gateway, client: KubeClient) -> bool:
        """지금 렌더가 쓰지 않는 토큰 객체를 지운다 (`GatewayTokens.settle`). 서비스 토큰 스윕이
        부른다. 문서의 클레임을 쥐고 돌고, 남이 쥐고 있으면 건너뛴다 (P66 F3). client 는 스윕이
        목록의 사본으로 만든 것이라, 쥐며 다시 읽은 문서가 다른 클러스터로 옮겨 갔으면 건너뛴다."""
        listed = doc.cluster_id

        async def work() -> bool:
            if doc.cluster_id != listed:
                return False
            return await self.entry_tokens.settle(doc, client)

        return await self._claimed_or_skip(
            doc, self.live_states, action="settle token objects", work=work
        )

    async def _apply_consumer(self, doc: Gateway, client: KubeClient, *, rotate: bool) -> None:
        """토큰 Secret 과 그 판을 담은 vMCP, 노출했으면 노출 객체까지 배포와 같은 순서로 다시
        적용한다 (P50 1절). 저장된 overrides 가 거부되면 토큰을 갈기 전에 멈춘다. 새 토큰만
        Secret 에 들어가고 vMCP 는 굴러가지 않는 반쪽 회전을 남기지 않는다. 이름 계획은 배포
        스냅샷으로 다시 세운다 (G4). 스냅샷이 같으면 계획도 같다. 다른 문서의 라벨을 단 Entry 는
        건너뛴다(`skip_foreign`)."""
        self._plan(doc, self._pinned(doc), [])
        await self.editor.check_stored_rendered(doc)
        await self.entry_tokens.resolve_servers(doc)
        await self.resolve_digests(doc)
        svc_tokens = await self.entry_tokens.issue(doc, client, fresh=rotate)
        headers = await self.entry_headers.plan(doc, self._k8s(doc).name)
        docs = await self.entries.skip_foreign(
            doc, client, await self.render_docs(doc, svc_tokens=svc_tokens, headers=headers)
        )
        await apply_docs(
            client,
            docs,
            self.editor.stored_final(doc, docs[-1]),
            GatewayError,
            authz_wait=REAPPLY_AUTHZ_WAIT_SECONDS,
        )
        # 설정을 기다리다 멈추거나 vMCP 적용이 실패하면 여기 오지 않아 기록이 그대로다. 그러면 다음
        # 렌더도 힌트 forbid 를 둔다 (P56 6.3절)
        doc.code_mode_applied = self._carries_code_mode(docs[-1])
        await self._drop_left_out_headers(doc, client, headers)
        if doc.exposure.enabled:
            effective = EffectiveConfig.model_validate(doc.effective_config)
            await self._apply_exposure(doc, client, effective)

    async def _drop_left_out_headers(
        self, doc: Gateway, client: KubeClient, headers: HeaderPlan
    ) -> None:
        """적용한 렌더가 쓰지 않는 헤더 사본을 곧바로 지운다 (P86 6.2절, `EntryHeaders`)."""
        k8s = self._k8s(doc)
        await self.entry_headers.drop_left_out(
            client,
            k8s_name=k8s.name,
            namespace=k8s.namespace,
            plan=headers,
            labeled=await self.entry_tokens.labeled_objects(doc, client),
        )

    async def _header_warnings(self, doc: Gateway) -> list[str]:
        """`outside_header_missing` (P86 6.2절). home 클러스터에 연결하지 못해도 상태 GET 은
        실패하지 않는다."""
        if doc.state == "draft" or doc.k8s is None:
            return []
        try:
            return await self.entry_headers.warnings(doc)
        except (ClusterUnreachable, AccessDenied) as e:
            logger.warning("gateway %s: header values not checked: %s", doc.id, type(e).__name__)
            return []

    async def drop_stray_header_copies(self, _now: Any = None) -> dict[str, list[str]]:
        """배포 상태와 상관없이, 둘 수 없게 된 헤더 사본을 이름으로 지운다 (P86 6.2·6.5절). 항목이
        없어졌거나 그 Gateway 의 테넌트가 항목의 consume 경계 밖인 Entry 의 것이다. 배포된 Gateway
        는 재적용이 다시 렌더하며 지우지만, 실패했거나 배포 중이거나 내리는 중인 Gateway 는 재적용이
        렌더하지 않으므로 리컨실 틱마다 여기서 지운다. 한 Gateway 의 실패는 다른 Gateway 를 막지
        않는다."""
        dropped: list[str] = []
        for doc in await self.gateways.with_header_backends():
            if doc.k8s is None:
                continue
            try:
                names = await self.entry_headers.stray(doc, doc.k8s.name)
                if not names:
                    continue
                client = await self._cleanup_client(doc)
                if client is None:
                    continue
                await self.entry_tokens.drop_objects(client, doc.k8s.namespace, names)
                dropped.append(doc.id)
            except Exception as e:  # noqa: BLE001 - 다음 Gateway 로 간다
                logger.warning(
                    "gateway %s: stray header copies not dropped: %s", doc.id, type(e).__name__
                )
        return {"dropped": dropped} if dropped else {}

    async def _apply_exposure(
        self, doc: Gateway, client: KubeClient, effective: EffectiveConfig
    ) -> None:
        """노출 객체(Ingress, NetworkPolicy)를 적용한다. 배포와 다시 렌더하기가 함께 쓴다."""
        for rendered in self._expose_docs(doc, effective):
            await run_in_threadpool(client.apply, rendered)

    # -- 게시 ----------------------------------------------------------------------------

    async def publish(
        self,
        doc: Gateway,
        *,
        name: str | None = None,
        display_name: str | None = None,
        mode: str = "entry",
        access: Access | None = None,
        principal: Principal,
    ) -> HubItem:
        """이 문서의 vMCP 를 허브 published 항목으로 게시한다 (docs/guide/hub-gateway.md 6절).
        `deployed` 만 된다.
        CR 없이 URL 만 둔다 (소비자가 Entry 래퍼로 감싼다). mode 는 entry 뿐이다.
        이름 기본값은 문서 `name` 그대로, 표시 이름 기본값은 문서 `display_name` 이다 (D24).
        published 는 다른 테넌트에 열 수 없다 (422 cross_tenant_unsupported)."""
        self._require(doc, {"deployed"}, action="publish", outside="not_deployed")
        self._k8s(doc)
        if mode != "entry":
            raise GatewayError(422, "invalid_value", "mode must be entry")
        try:
            return await self.hub.publish_gateway(
                doc,
                name=name,
                display_name=display_name,
                access=access,
                principal=principal,
            )
        except HubError as e:
            raise GatewayError(e.status, e.code, e.detail, **e.extra) from e

    # -- 해제·삭제 ---------------------------------------------------------------------

    async def _delete_steps(self, doc: Gateway) -> None:
        """노출 객체, vMCP, 라벨로 남은 Entry·그룹, 서비스 토큰 객체, 옛 런타임 순으로 지운다.
        공유 복제본은 두 문서가 나눠 쓰므로 건드리지 않는다."""
        k8s = self._k8s(doc)
        client = await self._cleanup_client(doc)
        if client is None:
            return
        await run_in_threadpool(
            delete_expose_objects, client, k8s.name, k8s.namespace, front_proxy=False
        )
        await run_in_threadpool(
            client.delete, TOOLHIVE_API, "VirtualMCPServer", k8s.name, k8s.namespace
        )
        # Cedar 설정은 vMCP 보다 먼저 적용되므로, 첫 배포가 그 사이에 멈췄으면 소유자 없이 남는다.
        # 이름으로 지운다 (P50 8절)
        await delete_authz_config(client, k8s.name, k8s.namespace)
        # ownerReference 로 GC 되지만, daegam.devops.ai.kr/id 라벨로 남은 것을 명시적으로 지운다
        # (폴백)
        await delete_labeled(client, GROUP_ENTRY_KINDS, k8s.namespace, doc.id)
        # 토큰 객체는 vMCP 가 소유하지만, 첫 배포가 uid 를 얻기 전에 멈췄으면 소유자 없이 남는다.
        # 지금 스냅샷의 것과 라벨로 찾은 것(재배포가 뺀 Entry 의 것)을 이름으로 지운다
        names = set(entry_token_objects(k8s.name, doc.deployed_backends).values())
        names |= set(entry_header_objects(k8s.name, doc.deployed_backends).values())
        names |= await self.entry_tokens.labeled_objects(doc, client)
        await self.entry_tokens.drop_objects(client, k8s.namespace, sorted(names))

    async def _guard_undeploy(self, doc: Gateway) -> None:
        """게시돼 있거나 Agent 가 참조 중이면 409. 공유 복제본은 그대로 둔다."""
        if doc.state != "undeploying":
            await self._check_published(doc, "undeploy")
            await self._check_in_use(doc, "undeploy")

    async def _guard_delete(self, doc: Gateway) -> None:
        """게시돼 있으면 409 published, Agent 가 참조 중이면 409 in_use. draft 도 본다."""
        await self._check_published(doc, "delete")
        await self._check_in_use(doc, "delete")

    # -- 재배포 ------------------------------------------------------------------------

    async def redeploy(
        self, doc: Gateway, *, principal: Principal, claim_host: bool = False
    ) -> tuple[Gateway, list[str]]:
        """현재 리비전으로 핀을 옮기고 스냅샷을 다시 만든다. 네임스페이스는 문서 자신의
        `k8s.namespace` 다 (허용 목록에서 빠졌으면 409). (문서, warnings). claim_host 면 메모리의
        새 노출을 deploying 전이와 함께 저장하며 그 host 를 선점한다(Basic 편집, P70 2절). 문서의
        클레임을 쥐고 돈다. 남이 쥐고 있으면 409 in_progress 다 (P66 F3)."""
        self._require(doc, EDIT_STATES, action="redeploy")
        async with self._claim_scope(doc, EDIT_STATES, action="redeploy"):
            out = await self._redeploy_claimed(doc, principal=principal, claim_host=claim_host)
        return out

    async def _redeploy_claimed(
        self, doc: Gateway, *, principal: Principal, claim_host: bool
    ) -> tuple[Gateway, list[str]]:
        k8s = self._k8s(doc)
        rev = doc.current
        items, warnings = await self.validator.validate_spec(
            doc.tenant, spec_of_revision(rev), name=doc.name, principal=principal, self_id=doc.id
        )
        cluster = await self._writable_cluster_of(doc)
        self.validator.require_same_cluster(items, cluster.id)
        # 노출(Basic 편집이 메모리에 얹은 새 노출)과 웹 백엔드 규칙 (P39b 13절)
        warnings += await self.validator.check_backends(
            items, exposed=doc.exposure.enabled, gateway=doc
        )
        effective = await self._effective(doc.tenant, cluster, rev)
        await self._require_namespace_still_allowed(doc.tenant, cluster, k8s.namespace)
        effective.namespace.name = k8s.namespace
        snapshots, _report = await self._snapshot_backends(
            doc.name,
            rev,
            items,
            tenant=doc.tenant,
            namespace=k8s.namespace,
            require_ready=True,
        )
        await self.entries.require_free(doc, cluster.id, k8s.namespace, snapshots)
        old_workloads = {b.workload for b in doc.deployed_backends}
        if doc.overrides:
            probe = doc.model_copy(deep=True)
            probe.deployed_backends = snapshots
            self._plan(probe, rev, [])
            cr = (await self._render_docs(probe, rev, effective))[-1]
            await self._dry_run_overrides(doc, cr)
        doc.deployed_revision = rev.revision
        doc.deployed_backends = snapshots
        self._plan(doc, rev, warnings)
        await self.validator.require_code_mode(doc, doc, cluster=cluster)
        warnings += await self.validator.pinned_names_warning(doc)
        doc.effective_config = effective.model_dump(mode="json")
        self._start_deployment(doc)
        async with self._host_claimed(doc, claim=claim_host), self.entries.claimed(doc):
            await self._transition(doc, {"deployed", "failed"}, action="redeploy")
        self._place_claim(doc)
        await self._drop_stale_entries(doc, old_workloads)
        return await self.provision(doc), warnings

    async def _drop_stale_entries(self, doc: Gateway, old_workloads: set[str]) -> None:
        """재배포가 더 이상 쓰지 않는 Entry 래퍼 가운데 자기 것을 지우고, 쓰지 않는 이름의 선점을
        놓는다. 그 Entry 의 토큰 객체는 옛 vMCP 파드가 다 내려간 뒤에 서비스 토큰 스윕이 지운다
        (`settle_token_objects`)."""
        kept = {b.workload for b in doc.deployed_backends}
        if old_workloads - kept:
            await self.entries.delete_own(doc, await self.client_for_doc(doc), old_workloads - kept)
        await self.entries.release_except(doc, kept)

    async def set_exposure(
        self, doc: Gateway, exposure: ExposureInput | None, *, principal: Principal
    ) -> tuple[Gateway, list[str]]:
        """Basic 편집: 노출 설정을 바꾸고 재배포한다 (docs/design/resource-model.md 13절).
        정의 리비전은 바뀌지 않는다."""
        doc = await self._fresh(doc)
        self._require(doc, EDIT_STATES, action="edit exposure")
        # 새 노출을 메모리에 얹기 전에 클레임을 쥔다. 재배포가 그것을 물려받는다 (P66 F3)
        async with self._claim_scope(doc, EDIT_STATES, action="edit exposure"):
            out = await self._set_exposure_claimed(doc, exposure, principal=principal)
        return out

    async def _set_exposure_claimed(
        self, doc: Gateway, exposure: ExposureInput | None, *, principal: Principal
    ) -> tuple[Gateway, list[str]]:
        # 끝은 재배포라 노출을 저장하기 전에 쓸 수 있는 클러스터인지 본다 (P63 2절)
        cluster = await self._writable_cluster_of(doc)
        k8s = self._k8s(doc)
        doc.exposure, _warnings = await self._validate_exposure(
            exposure,
            tenant=doc.tenant,
            name=doc.name,
            cluster=cluster,
            namespace=k8s.namespace,
            exclude_id=doc.id,
        )
        # 노출 여부가 잠금 표를 고른다(`vmcp_locked_paths`). 새 노출 값으로 저장된 overrides 를 먼저
        # 보지 않으면 노출만 저장된 채 재배포가 422 로 멈춘다. 검사는 메모리의 새 exposure 를 읽는다
        await self.editor.check_stored_rendered(doc)
        # 새 노출은 따로 저장하지 않는다. 재배포가 deploying 으로 옮기는 같은 저장이 쓴다. 재배포가
        # 시작하지 못하면 아무것도 저장되지 않아 옛 host 를 놓지 않는다 (P70 2절)
        return await self.redeploy(doc, principal=principal, claim_host=True)

    # -- 편집 (Advanced) -----------------------------------------------------------------

    async def render_docs(
        self,
        doc: Gateway,
        *,
        svc_tokens: dict[str, str] | None = None,
        headers: HeaderPlan | None = None,
    ) -> list[dict[str, Any]]:
        """저장된 스냅샷으로 다시 렌더링한 문서 묶음. 편집·미리보기는 토큰 없이, 토큰 회전은 새
        토큰(Entry 이름 → 토큰)을 넘겨 Secret 까지 낸다. 헤더 사본의 Secret 은 미리 세운 계획을
        넘길 때만 낸다 (P86 6.2절)."""
        return await self._render_docs(
            doc,
            self._pinned(doc),
            EffectiveConfig.model_validate(doc.effective_config),
            owner=self._owner(doc),
            svc_tokens=svc_tokens,
            headers=headers,
        )

    async def manifest(self, doc: Gateway) -> dict[str, Any]:
        """문서 묶음: MCPGroup, MCPServerEntry 래퍼들, VirtualMCPServer, (노출이면) Ingress 와
        NetworkPolicy. overrides 는 vMCP 에만 얹고 나머지는 locked ["*"] 다. draft 면 409."""
        rendered_docs = await self.render_docs(doc)
        if doc.exposure.enabled:
            rendered_docs += self._expose_docs(
                doc, EffectiveConfig.model_validate(doc.effective_config)
            )
        return {"documents": [await self.editor.document(doc, r) for r in rendered_docs]}

    # Advanced 편집 셋(`set_overrides`·`drift_reset`·`drift_absorb`)은 기반이 맡는다. 다시 읽기와
    # 상태 검사가 세 도메인에서 같아 `edit_states`·`edit_outside` 만 다르다
