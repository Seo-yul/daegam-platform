"""MCP Server 서비스 (D42). 정의(저장·리비전·복제)와 배포(배포·재배포·해제·삭제·상태)가 한 문서
위에서 돈다. 모든 클러스터 단계가 SSA·결정적 이름이라 재실행이 안전하다.

상태 여섯 가운데 동작이 되는 상태는 플랜의 상태 표를 따른다. `record_failure` 가 `failed` 로 옮기는
것은 배포 경로(`deploying`)에서만이다. 해제·삭제 도중의 실패는 `undeploying`·`deleting` 에
`last_error` 로 남고 리컨실이 같은 갈래를 다시 몬다. 그 실패를 `failed` 로 옮기면 리컨실이 "다시
배포" 로 몰아 지우던 CR 을 되살린다.

상태를 바꾸는 저장은 전부 스토어의 조건부 갱신(`transition`)이다. 옛 모델은 배포가 새 문서의
insert 라 유니크 인덱스가 두 번째를 막았는데, 한 문서 위에서는 검사와 쓰기 사이의 경합을 그 저장이
막아야 한다. 0건이면 409 state_conflict 다.

저장은 자기 쪽 필드만 쓴다. 정의 저장(`_save_definition`)은 `spec`·`revision`·`revisions` 쪽만 옛
`revision` 을 조건으로, 배포 쪽 저장(`_transition`·`_save_deployment`)은 배포 필드만 `state` 를
조건으로 `$set` 한다. 한 문서에 두 쪽이 살므로 문서 전체를 쓰면 낡은 스냅샷이 남의 쪽을 되돌린다
(PUT 이 도는 중인 배포를 draft 로, 스윕이 끝난 해제를 deployed 로). 0건이면 409 다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.auth.hub_access import Access
from daegam.auth.principal import Principal
from daegam.auth.rbac import ROLES_CLAIM
from daegam.auth.rule_cedar import closing_forbid
from daegam.config.resolve import EffectiveConfig, ProfileRuntime, resolve_effective
from daegam.error_codes import status_of
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.gates import (
    TENANT_OBJECTS_RIGHTS,
    Feature,
    cluster_gate,
    gate_error,
    known_below,
    passing_features,
    rights_gate,
    tenant_secret_refusal,
    toolhive_has,
)
from daegam.k8s.labels import LABEL_ID, LABEL_PROFILE_REVISION, stable_hash
from daegam.k8s.merge import (
    EXPOSE_PROXY_LOCKED_PATHS,
    IGNORED_BY_OPERATOR,
    LOCKED_PATHS,
    locked_path_strings,
)
from daegam.k8s.templates import (
    TOOLHIVE_API,
    RenderError,
    owner_ref,
    render_mcpserver,
    render_token_secret,
)
from daegam.k8s.templates.authz import tenant_fence
from daegam.k8s.templates.common import (
    DEFAULT_REDIS_SOURCE,
    REDIS_CA_ANNOTATION,
    redis_ca_fingerprints,
    redis_ca_mark,
    redis_ca_sources,
    rolled_out,
    template_annotation,
)
from daegam.k8s.templates.exposure import expose_proxy_name
from daegam.k8s.templates.mcpserver import SESSION_REPLICAS, token_key
from daegam.k8s.templates.svc_token import expose_svc_name, secret_value, svc_token_rev
from daegam.pem import ca_sha256
from daegam.secrets import K8sSecretStore
from daegam.services.advanced_edit import AdvancedEditor
from daegam.services.authz_configs import (
    AUTHZ_WAIT_SECONDS,
    REAPPLY_AUTHZ_WAIT_SECONDS,
    apply_docs,
    apply_in_order,
    delete_authz_config,
)
from daegam.services.backend_apis import select_changes
from daegam.services.claims import Claims, checkpoint, note_issued
from daegam.services.clusters import ClusterService
from daegam.services.condition_records import (
    conditions_enforced,
    conditions_match,
    unresolved_conditions,
)
from daegam.services.data_plane_auth import (
    KIND_MCP_SERVER,
    own_audience_rev,
    redis_tls_rev,
    render_epoch,
    rules_rev,
)
from daegam.services.data_services import ClusterMismatch, check_same_cluster
from daegam.services.exposure import (
    ExposureInput,
    ExposureValidationMixin,
    delete_expose_objects,
    exposure_from_input,
    exposure_to_input,
    mcp_expose_docs,
    status_base,
    unauthenticated_warnings,
)
from daegam.services.hub import HubError, HubService
from daegam.services.instance_base import (
    EDIT_STATES,
    IN_PROGRESS,
    PROVISION_STATES,
    BaseInstanceService,
)
from daegam.services.instance_status import (
    ROLLOUT_WATCH,
    changed_within,
    condition_rows,
    failed_condition_message,
    read_deployment,
)
from daegam.services.mcp_inputs import (
    backend_apis_from,
    data_services_from,
    inputs_from_documents,
    session_tls_of,
    snapshot_fits,
)
from daegam.services.mcp_spec import ProfileValidationError, scope_for, validate_tools
from daegam.services.proxy_accounts import (
    proxy_service_account,
    require_proxy_account,
)
from daegam.services.redis_tls_consumers import RecentReads
from daegam.services.references import MCP_SERVER_IN_USE, ReferenceService
from daegam.services.rule_compile import (
    MARK_TTL_SECONDS,
    RuleScopes,
    closed_policies,
    earlier_names,
    roles_claimed,
    ruled_names,
    rules_digest,
    server_policies,
    server_rules_by_name,
    source_forbids,
)
from daegam.services.spec_generator import overrides, projection, surface
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import (
    TenancyService,
    default_redis_ca_ready,
    group_name_for,
    k8s_name,
)
from daegam.services.tool_rules import (
    CONDITIONS_CLOSED,
    CONDITIONS_UNRESOLVED,
    RULES_FRONT_ROLLING,
    RULES_GATEWAYS_BEHIND,
    RULES_NOT_ENFORCED,
    RULES_TOOLHIVE_OLD,
    RuleNotice,
    check_cedar_size,
    check_new_agents,
    normalized_server_rules,
    notice_of,
    require_rules_toolhive,
    rolling_agents,
    rule_warning,
    rules_toolhive_ok,
    server_holds_rules,
    server_rule_keys,
    validate_server_rules,
)
from daegam.services.turn_gates import owner_cedar
from daegam.store.agents import MongoAgentStore
from daegam.store.auth_reapply import new_server_switch
from daegam.store.backend_apis import BackendAPI, BackendAPIStore, BackendAPIVersion
from daegam.store.base import hash_token, issue_token, utcnow
from daegam.store.clusters import DEFAULT_REDIS_CA_SECRET, Cluster
from daegam.store.data_services import REDIS_CA_KEY, DataService, MongoDataServiceStore
from daegam.store.exposure_hosts import MongoExposureHostStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.hub import HubItem
from daegam.store.mcp_servers import (
    DEFINITION_FIELDS,
    DEPLOYMENT_FIELDS,
    BackendSelection,
    BindingInputs,
    InstanceK8s,
    MCPServer,
    MCPServerInputs,
    MCPServerRevision,
    MCPServerSpec,
    MongoMCPServerStore,
    ProfileRuntimeSpec,
    token_secret_name_for,
)
from daegam.store.tenants import Binding
from daegam.store.tool_rules import has_caller_rule
from daegam.ttl_cache import TTLCache

# 사용자 이미지 서버의 Redis 바인딩이 TLS 끝점으로 바뀐다는 배포 경고의 코드 (P61 3절)
REDIS_TLS_BINDING_WARNING = "redis_tls_binding"

logger = logging.getLogger("daegam.mcp_servers")


class MCPServerError(ServiceError):
    pass


def k8s_name_for(server_name: str) -> str:
    return k8s_name(server_name, prefix="mcp")


class MCPServerService(ExposureValidationMixin, BaseInstanceService[MCPServer]):
    error_cls = MCPServerError
    profile_kind = "mcp server"
    claim_kind = "mcp-server"
    auth_kind = KIND_MCP_SERVER
    definition_fields = DEFINITION_FIELDS
    deployment_fields = DEPLOYMENT_FIELDS
    # 렌더는 provision 안에서도 일어난다. RenderError 는 클러스터 탓이 아니라 스펙 탓이고 상태는
    # 오류 코드표에서 찾는다(대개 422, P74 2.2절)
    provision_code_only = (RenderError,)
    k8s_api = TOOLHIVE_API
    k8s_kind = "MCPServer"
    front_proxy = True
    # 재렌더가 판과 함께 저장하는 배포 쪽 필드. 스냅샷이 없던 문서는 재렌더가 채운 것을, MCPServer
    # 를 다시 만들었으면 새 uid 를, 파드가 믿는 Redis CA 가 바뀌었으면 그 지문을 남긴다 (P50 1절,
    # P61)
    consumer_fields = frozenset({"deployed_inputs", "k8s_uid", "redis_ca"})
    # Tool 규칙을 컴파일할 테넌트의 사실 (P52 2절). wiring 이 만든 뒤에 단다(세 서비스가 같은
    # 것을 나누고, Agent 서비스는 이 서비스보다 늦게 만들어진다). 없으면 규칙을 렌더하지 않는다
    rule_scopes: RuleScopes | None = None
    late_hooks: ClassVar[frozenset[str]] = frozenset({"rule_scopes"})
    # 재적용 틱의 규칙 표시를 문서마다 잠깐 기억한다 (`_rules_mark`)
    _marks: TTLCache[tuple[Any, ...], str] | None = None

    def __init__(
        self,
        servers: MongoMCPServerStore,
        backend: BackendAPIStore,
        data_services: MongoDataServiceStore,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        references: ReferenceService,
        *,
        hub: HubService,
        gateway_instances: MongoGatewayStore,
        agents: MongoAgentStore,
        svc_tokens: SvcTokens,
        claims: Claims,
        exposure_hosts: MongoExposureHostStore,
    ):
        self.servers = servers
        # 배포 해제·삭제 가드가 참조를 센다 (P75 2절)
        self.references = references
        # BaseInstanceService 가 쓰는 이름. 합친 스토어가 옛 인스턴스 스토어 프로토콜을 지킨다
        self.instances = servers
        self.backend = backend
        self.data_services = data_services
        # 재적용 틱이 서버마다 파드가 믿는 Redis 의 CA 를 본다. `_redis_ca` 가 쓴다
        self._recent = RecentReads()
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        # 노출 앞단이 안쪽 MCPServer 에 낼 서비스 토큰의 발급 창구 (D41). wiring 이 준다
        self.svc_tokens = svc_tokens
        # 게시·해제는 사용자가 부르고(D23), 재배포는 게시된 항목만 맞추며, 삭제는 항목을 지운다
        self.hub = hub
        # P8: host_conflict 검사는 Gateway 와 Agent 의 host 도 본다(D27, D42). 참조 가드는
        # ReferenceService 가 센다
        self.gateway_instances = gateway_instances
        self.agents = agents
        # 문서 하나의 클러스터 작업을 묶는 클레임 (P66)
        self.claims = claims
        # 공개 host 의 선점 기록 (P70). 노출을 저장하기 전에 host 를 잡는다
        self.exposure_hosts = exposure_hosts
        # Advanced 편집 (F7a). MCPServer 는 플랫폼 잠금에 proxyrunner 가 덮어쓰는 경로까지 막는다
        self.editor: AdvancedEditor[MCPServer] = AdvancedEditor(
            kind="MCPServer",
            render=self.render,
            client=self.client_for_doc,
            save=self._save_overrides,
            manifest=self.manifest,
            locked=lambda _inst: LOCKED_PATHS,
            ignored=IGNORED_BY_OPERATOR,
            error_cls=MCPServerError,
            apply=self._apply_edit,
        )

    # -- 정의: 검증·저장·리비전 ------------------------------------------------------------

    async def validate_spec(
        self, tenant: str, spec: MCPServerSpec, *, before: MCPServerSpec | None = None
    ) -> None:
        """backend 버전·툴 이름·Tool 규칙·바인딩(env_prefix 유일, 같은 클러스터)을 본다. 422 다.
        before 는 저장 전의 정의이고, 규칙이 더한 Agent id 만 그 테넌트에 있는지 본다 (P52 1절)."""
        _backends, versions = await self._load_backend_apis(tenant, spec.backends)
        try:
            validate_tools(spec, versions)
        except ProfileValidationError as e:
            raise MCPServerError(status_of(e.code, 422), e.code, e.detail, **e.extra) from e
        validate_server_rules(
            spec, versions, oidc=self.cluster_service.settings.auth_mode == "oidc"
        )
        await check_new_agents(
            self.agents,
            tenant,
            after=server_rule_keys(spec).values(),
            before=server_rule_keys(before).values() if before is not None else (),
        )
        if self.rule_scopes is not None and server_holds_rules(spec):
            # 컴파일할 Cedar 의 크기. 이름이 가장 긴 앞단과 서버 id 로 잰다
            scope = await self.rule_scopes.scope(tenant)
            rules = server_rules_by_name(spec)
            server = server_policies(spec, scope, server_id="x" * 36, front="x" * 63)
            check_cedar_size(server, object="MCPServer")
            check_cedar_size(source_forbids(rules, scope, "front"), object="front")
        _check_env_prefixes(spec.bindings)
        await self._load_bindings(tenant, spec.bindings)

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        display_name: str,
        spec: MCPServerSpec,
        user_sub: str | None,
        exposure: ExposureInput | None = None,
    ) -> MCPServer:
        """정의만 받아 draft r1 을 만든다. 클러스터는 건드리지 않는다."""
        if await self.servers.get_by_name(tenant, name) is not None:
            raise MCPServerError(409, "name_conflict", f"mcp server {name!r} already exists")
        spec = normalized_server_rules(spec)
        await self.validate_spec(tenant, spec)
        doc = MCPServer(
            tenant=tenant,
            name=name,
            display_name=display_name,
            spec=spec,
            revision=1,
            revisions=[MCPServerRevision(revision=1, **spec.model_dump())],
            exposure=exposure_from_input(exposure),
            updated_by=user_sub,
            # 새 id 라 옛 토큰을 든 소비자가 없다. 처음부터 자기 audience 로 검증한다 (P50 9절)
            audience_switch=new_server_switch(utcnow()),
        )
        try:
            await self.servers.create(doc)
        except DuplicateKeyError as e:
            raise MCPServerError(409, "name_conflict", f"mcp server {name!r} already exists") from e
        return doc

    async def _new_revision(
        self, doc: MCPServer, spec: MCPServerSpec, user_sub: str | None
    ) -> MCPServer:
        """리비전 +1 과 스냅샷. 배포 핀(`deployed_revision`)과 상태는 그대로다. 저장은 정의 쪽
        필드만이고 옛 revision 을 조건에 건다."""
        old = doc.revision
        doc.spec = spec
        doc.revision += 1
        doc.revisions.append(MCPServerRevision(revision=doc.revision, **spec.model_dump()))
        doc.updated_by = user_sub
        await self._save_definition(doc, expect_revision=old, action="save")
        return doc

    async def save(
        self, doc: MCPServer, *, display_name: str, spec: MCPServerSpec, user_sub: str | None
    ) -> MCPServer:
        """전체 교체 = 새 리비전. 이전 리비전은 revisions[] 에 그대로 남는다. 어느 상태에서든
        저장은 되지만 진행 중(`deploying` 등)에는 막는다."""
        if doc.state in IN_PROGRESS:
            self._require(doc, frozenset(), action="save")
        spec = normalized_server_rules(spec)
        await self.validate_spec(doc.tenant, spec, before=spec_of_revision(doc.current))
        doc.display_name = display_name
        return await self._new_revision(doc, spec, user_sub)

    async def rules_notice(self, doc: MCPServer, before: MCPServerSpec) -> RuleNotice:
        """저장 전의 정의 before 와 지금 리비전을 견준 알림 (P52 1절). callers 를 모두 지운 Tool
        은 모든 멤버와 Agent 에게 열린다."""
        after = server_rule_keys(spec_of_revision(doc.current))
        notice = notice_of(server_rule_keys(before), after)
        notice.rolling_agents = await rolling_agents(
            self.agents,
            self.rule_scopes,
            doc.tenant,
            after=after.values(),
            before=server_rule_keys(before).values(),
        )
        return notice

    async def _rule_inputs(
        self, doc: MCPServer, rev: MCPServerRevision, cluster: Cluster, *, cached: bool = False
    ) -> tuple[Any, dict[str, list[str]]] | None:
        """(테넌트의 사실, 앞 이름). 규칙이 없으면 None 이다. 앞 이름은 규칙이나 조건이 있는
        operationId 가 다른 리비전에서 다른 이름일 때만 파드를 읽어 정한다 (P52 2절). cached 면
        재적용 틱의 판 계산이라 테넌트의 사실을 잠깐 기억한 것으로 읽는다."""
        spec = spec_of_revision(rev)
        if self.rule_scopes is None or doc.k8s is None or not server_holds_rules(spec):
            return None
        scope = await self.rule_scopes.scope(doc.tenant, cached=cached)
        earlier = earlier_names(doc.revisions, rev, None)
        if earlier:
            earlier = earlier_names(doc.revisions, rev, await self._running_revisions(doc, cluster))
        return scope, earlier

    async def _rule_policies(
        self, doc: MCPServer, rev: MCPServerRevision, cluster: Cluster, *, cached: bool = False
    ) -> list[str]:
        """그 리비전의 Tool 규칙 forbid (P52 2절). 규칙이 없으면 빈 목록이다. 클러스터가 0.53
        아래이거나 판을 모르면 규칙과 조건이 있는 Tool(과 앞 이름)을 누구에게나 닫는다. 배포는
        거절되고 다시 렌더는 적용을 건너뛰지만, Advanced 저장이나 드리프트 되돌리기처럼 다른 길로
        렌더돼도 열리지 않게 한다. 이름을 바꾼 operationId 는 돌고 있는 백엔드 파드의 리비전이
        쓰던 이름도 닫는다."""
        inputs = await self._rule_inputs(doc, rev, cluster, cached=cached)
        if inputs is None or doc.k8s is None:
            return []
        scope, earlier = inputs
        spec = spec_of_revision(rev)
        if not rules_toolhive_ok(cluster):
            return closed_policies(server_rules_by_name(spec), scope, earlier=earlier)
        return server_policies(
            spec,
            scope,
            server_id=doc.id,
            front=doc.k8s.name if doc.exposure.enabled else None,
            earlier=earlier,
            # 조건이 있는 Tool 은 모든 파드가 조건을 지킨다는 기록이 있을 때만 연다 (P52 4절)
            conditions_known=conditions_enforced(doc),
        )

    async def _running_revisions(self, doc: MCPServer, cluster: Cluster) -> set[int] | None:
        """돌고 있는 백엔드 파드(Terminating 포함)의 리비전 라벨. 읽지 못하거나, 파드가 있는데 어느
        것도 그 라벨을 싣지 않으면 None 이고(모른다), 그때는 모든 리비전의 이름을 닫는다.
        ToolHive 가 podTemplateSpec 의 라벨을 백엔드 파드에 옮기는지는 클러스터에서 재지 않았다."""
        k8s = self._k8s(doc)
        try:
            client = await run_in_threadpool(self.cluster_service.client_for, cluster)
            pods = await run_in_threadpool(
                client.list, "v1", "Pod", k8s.namespace, {LABEL_ID: doc.id}
            )
        except Exception:  # noqa: BLE001 - 모르면 닫는 쪽이다
            logger.warning("could not read the pods of mcp server %s", doc.id, exc_info=True)
            return None
        out: set[int] = set()
        labeled = False
        for pod in pods:
            label = ((pod.get("metadata") or {}).get("labels") or {}).get(LABEL_PROFILE_REVISION)
            labeled = labeled or label is not None
            if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                continue
            if label and label.isdigit():
                out.add(int(label))
        if pods and not labeled:
            return None
        return out

    async def apply_draft(
        self,
        doc: MCPServer,
        *,
        job: Any,
        job_files: Any,
        accepted_ids: list[str],
        user_sub: str | None,
    ) -> tuple[MCPServer, bool]:
        """overrides job 의 changes 중 accepted_ids(비면 전부) 를 심어 새 리비전을 만든다 (D37).
        (문서, 새 리비전을 만들었는지).

        **비교도 심는 자리도 `doc.current` 다.** job 이 핀한 리비전이 아니다. 리비전은 `revisions[]`
        에 append 되는 불변 스냅샷이라, 핀한 리비전과 비교하면 영원히 같아서 낡은 채택 보호가 통째로
        사라진다.

        비교 범위는 `backends` 뿐이다. job 이 도는 사이 사용자가 replicas 나 바인딩만 바꿨으면
        거부하지 않고 그 편집을 살린 채 채택분을 얹는다. 새 spec 이 현재 리비전의 runtime·bindings
        를 그대로 이어받는 것도 그래서다."""
        if doc.state in IN_PROGRESS:
            self._require(doc, frozenset(), action="apply-draft")
        cur = doc.current
        original = overrides.fragment(overrides.spec_of(cur))
        if job.mode != "overrides":
            raise MCPServerError(422, "job_not_overrides", f"spec job {job.id} is mode {job.mode}")
        if job.state != "done" or not job.draft_ref:
            raise MCPServerError(422, "job_not_done", f"spec job {job.id} is {job.state}")
        source = job.source if isinstance(job.source, dict) else {}
        source_id = source.get("mcp_server_id") or source.get("mcp_profile_id")
        if source_id != doc.id:
            raise MCPServerError(
                422, "source_mismatch", f"spec job {job.id} is for {job.source}, not {doc.id}"
            )
        raw_in = await job_files.get(job.input_ref)
        raw_draft = await job_files.get(job.draft_ref)
        if raw_in is None or raw_draft is None:
            raise MCPServerError(422, "job_not_found", f"spec job {job.id} files are gone")
        seen = overrides.fragment(json.loads(raw_in.decode("utf-8")).get("spec") or {})
        if surface.diff(original, seen):
            raise MCPServerError(
                422,
                "source_mismatch",
                f"spec job {job.id} saw a different backends; the mcp server changed since",
            )
        draft = json.loads(raw_draft.decode("utf-8"))
        accepted = select_changes(projection.changes(original, draft), accepted_ids)
        if not accepted:
            raise MCPServerError(422, "no_changes", "nothing to apply")
        planted = projection.apply_changes(original, accepted)
        violations = surface.guard(original, planted, rules=surface.ALLOW_OVERRIDES)
        if violations:
            raise MCPServerError(
                422,
                "surface_changed",
                f"{len(violations)} change(s) outside tool_overrides name and description",
                violations=[v.to_dict() for v in violations[:50]],
            )
        spec = MCPServerSpec(
            backends=planted["backends"], runtime=cur.runtime, bindings=list(cur.bindings)
        )
        await self.validate_spec(doc.tenant, spec, before=spec_of_revision(cur))
        if spec.model_dump() == spec_of_revision(cur).model_dump():
            return doc, False
        return await self._new_revision(doc, spec, user_sub), True

    async def clone(
        self,
        doc: MCPServer,
        *,
        name: str,
        display_name: str,
        exposure: ExposureInput | None,
        user_sub: str | None,
    ) -> MCPServer:
        """현재 리비전의 정의를 베낀 draft r1. `exposure` 를 주면 그 값이 배포 설정으로 남는다.
        한 정의로 둘을 띄우는 자리를 대신한다 (D42)."""
        cur = doc.current
        return await self.create(
            doc.tenant,
            name=name,
            display_name=display_name,
            spec=spec_of_revision(cur),
            user_sub=user_sub,
            exposure=exposure,
        )

    async def _check_in_use(self, doc: MCPServer) -> None:
        """이 문서를 참조하는 것. 허브 항목을 거쳐 쓰는 Gateway 와 도구 소스로 쓰는 Agent 다. 걸린
        칸 전부로 답한다. 테넌트를 거르지 않는다. 다른 테넌트의 참조도 막아야 해서다."""
        await self.references.refuse_any(
            "mcp-server",
            doc.id,
            MCP_SERVER_IN_USE,
            MCPServerError,
            f"mcp server {doc.name} is referenced by other resources",
        )

    # -- 허브 게시 (docs/guide/hub-gateway.md 6절) ------------------------------------

    async def publish(
        self,
        doc: MCPServer,
        *,
        name: str | None = None,
        display_name: str | None = None,
        access: Access | None = None,
        principal: Principal,
    ):
        """허브에 `internal` 항목으로 게시한다. (항목, 새로 만들었는지). `deployed` 만 된다.
        `failed` 는 URL 이 없으므로 not_deployed 다."""
        self._require(doc, {"deployed"}, action="publish", outside="not_deployed")
        try:
            return await self.hub.publish_instance(
                doc,
                name=name,
                display_name=display_name,
                access=access,
                principal=principal,
            )
        except HubError as e:
            raise MCPServerError(e.status, e.code, e.detail, **e.extra) from e

    async def _hub_sync(self, doc: MCPServer) -> None:
        """재배포 뒤 게시된 항목의 CR 참조·클러스터를 맞춘다. 게시돼 있지 않으면 아무것도 안 한다.

        도구 이름은 여기서 맞추지 않는다. 저장된 사본이 아니라 원본에서 계산하는 값이라 맞출 것이
        없다 (D49). 예외를 삼키는 것은 그대로다. 허브 장부 갱신이 실패했다고 이미 성공한 클러스터
        배포를 되돌릴 수는 없고, 이제는 그것이 어긋남을 남기지도 않는다.
        """
        if self.hub is None:
            return
        try:
            await self.hub.sync_instance(doc)
        except Exception as e:  # noqa: BLE001 - 허브 갱신 실패가 재배포를 실패로 만들지 않는다
            logger.warning("hub sync for mcp server %s failed: %s", doc.id, e)

    async def hub_item(self, doc: MCPServer) -> HubItem | None:
        """doc 을 게시한 `internal` 항목. 게시돼 있지 않거나 허브가 없으면 None."""
        return None if self.hub is None else await self.hub.store.get_by_instance(doc.id)

    # -- 배포 ----------------------------------------------------------------------

    async def _load_backend_apis(
        self, tenant: str | None, selections: Sequence[BackendSelection]
    ) -> tuple[list[BackendAPI], list[BackendAPIVersion]]:
        """선택 순서대로 Backend API 와 핀된 버전을 읽는다. 없으면 422 다. 정의 검증과 배포 경로가
        같은 오류 코드·문구를 낸다."""
        profiles: list[BackendAPI] = []
        versions: list[BackendAPIVersion] = []
        for sel in selections:
            bp = await self.backend.get(sel.backend_profile_id, tenant=tenant)
            if bp is None:
                raise MCPServerError(
                    422,
                    "unknown_backend_api",
                    f"backend api {sel.backend_profile_id} not found",
                )
            version = await self.backend.get_version(bp.id, sel.spec_version)
            if version is None:
                raise MCPServerError(
                    422,
                    "unknown_spec_version",
                    f"backend api {bp.name} has no version {sel.spec_version}",
                )
            profiles.append(bp)
            versions.append(version)
        return profiles, versions

    async def _load_bindings(
        self, tenant: str, bindings: Sequence[Binding], cluster_id: str | None = None
    ) -> dict[str, DataService]:
        """바인딩한 Database 를 읽고 한 테넌트·한 클러스터인지 본다. cluster_id 를 주면 그
        클러스터여야 한다(배포 경로). 없으면 422 다."""
        services: dict[str, DataService] = {}
        for b in bindings:
            ds = await self.data_services.get(b.data_service_id, tenant=tenant)
            if ds is None:
                raise MCPServerError(
                    422, "unknown_data_service", f"data service {b.data_service_id} not found"
                )
            services[ds.id] = ds
        try:
            check_same_cluster(list(services.values()), tenant=tenant, cluster_id=cluster_id)
        except ClusterMismatch as e:
            raise MCPServerError(422, "cluster_mismatch", e.detail) from e
        return services

    def _sibling_stores(self) -> list[Any]:
        return [self.servers, self.gateway_instances, self.agents]

    async def _effective(self, tenant: str, cluster: Cluster, rev: MCPServerRevision):
        settings = await self.tenancy.platform_settings.get()
        effective = resolve_effective(
            settings,
            cluster,
            await self.tenancy.tenant_settings.get(tenant),
            ProfileRuntime(
                image=rev.runtime.image,
                replicas=rev.runtime.replicas,
                resources=rev.runtime.resources,
                bindings=rev.bindings,
            ),
        )
        # 설치 전체의 CA 묶음은 MCP Server 만 렌더하므로 이 스냅샷에만 적는다 (D88)
        effective.ca_bundle = settings.network.ca_bundle
        return effective

    async def deploy(
        self,
        doc: MCPServer,
        *,
        cluster_id: str | None,
        namespace: str | None = None,
        exposure: ExposureInput | None = None,
        exposure_sent: bool = False,
    ) -> tuple[MCPServer, list[str]]:
        """검증과 effective_config 스냅샷을 거쳐 배포 쪽을 채워 `deploying` 으로 저장한다. K8s 는
        읽기(노출 검증)만 한다. 적용은 `provision` 이다. (문서, warnings).

        `exposure` 를 보내지 않으면 문서에 남은 설정(복제가 받은 것)을 쓴다."""
        self._require(doc, {"draft"}, action="deploy")
        tenant = doc.tenant
        rev = doc.current
        cluster, ns_name = await self._resolve_target(tenant, cluster_id, namespace)
        if server_holds_rules(spec_of_revision(rev)):
            # 0.53 아래의 내부 서버에는 Cedar 가 없어 규칙을 지킬 수 없다 (P52 1절)
            require_rules_toolhive(cluster)
        services = await self._load_bindings(tenant, rev.bindings, cluster.id)
        backends, _versions = await self._load_backend_apis(tenant, rev.backends)

        effective = await self._effective(tenant, cluster, rev)
        # 배포 네임스페이스는 배포 본문의 namespace (허용 목록 안). 스냅샷도 그 이름을 갖는다
        effective.namespace.name = ns_name
        k8s = InstanceK8s(
            name=k8s_name_for(doc.name),
            namespace=ns_name,
            group=group_name_for(tenant),
            token_secret=token_secret_name_for(doc.id),
        )
        await self._require_name_free(doc, cluster.id, k8s.namespace, k8s.name)
        inp = exposure if exposure_sent else exposure_to_input(doc.exposure)
        exposure_snapshot, warnings = await self._validate_exposure(
            inp, tenant=tenant, name=doc.name, cluster=cluster, namespace=ns_name
        )
        if exposure_snapshot.enabled:
            # 새 노출 앞단은 역할 없는 ServiceAccount 로만 만든다 (P50 2절·10절)
            require_proxy_account(cluster, MCPServerError)
        self._stamp_deploying(
            doc,
            cluster=cluster,
            k8s=k8s,
            effective=effective,
            exposure=exposure_snapshot,
            revision=rev.revision,
        )
        # 첫 배포는 인자 조건을 지키는지 모른다. 해제하기 전 배포의 기록을 버린다 (P52 4절)
        if doc.conditions_record is not None:
            doc.conditions_record = None
            await self.servers.set_conditions_record(doc.id, None)
        # 렌더링이 되는지(예: redis_required) 저장 전에 확인한다
        _render_docs(
            doc,
            cluster,
            rev,
            backends,
            services,
            effective,
            session=session_options(cluster),
            redis_ca=await self._redis_ca_now(doc, rev, cluster),
            rules=await self._rule_policies(doc, rev, cluster),
        )
        warnings += custom_image_tls_warnings(rev, services)
        async with self._host_claimed(doc):
            await self._transition(doc, {"draft"}, action="deploy")
        return doc, warnings

    async def _provision(self, doc: MCPServer) -> None:
        tenant = doc.tenant
        context = await self._context(doc, from_documents=True)
        cluster, rev, backends, services, effective = context
        # 배포는 현재 공유 문서로 렌더하므로 그 값을 이 배포의 스냅샷으로 적는다. 저장은 provision
        # 끝의 전이가 한다. 실패한 배포는 저장하지 않으므로 앞 배포의 스냅샷이 남는다 (P50 1절)
        doc.deployed_inputs = inputs_from_documents(
            rev, backends, services, deploying_since=doc.deploying_since
        )
        k8s = self._k8s(doc)
        # 저장된 overrides 가 지금 잠긴 경로를 건드리면 토큰·Secret 을 만들기 전에 멈춘다
        await self.editor.check_stored_rendered(doc)

        # 1. 네임스페이스·MCPGroup·RoleBinding (effective 스냅샷의 네임스페이스 규칙과 같다)
        await self.tenancy.ensure_namespace(tenant, cluster, effective.namespace)
        client: KubeClient = await run_in_threadpool(self.cluster_service.client_for, cluster)

        # 2. 토큰 + Secret
        await self._ensure_spec_tokens(doc, client, rev, backends)

        # 3. 테넌트 울타리와 MCPServer. Advanced overrides 는 재렌더링 뒤에도 항상 다시 얹힌다 (맨
        # 앞에서 검사했다)
        docs = _render_docs(
            doc,
            cluster,
            rev,
            backends,
            services,
            effective,
            session=session_options(cluster),
            redis_ca=await self._redis_ca_now(doc, rev, cluster),
            rules=await self._rule_policies(doc, rev, cluster),
        )
        # 세션 저장소를 TLS 로 렌더했는지가 이 배포의 판단이다. 다시 렌더는 그것을 따른다 (P61)
        doc.deployed_inputs.session_tls = session_tls_of(docs[-1])
        # 4. 노출 (앞단 MCPRemoteProxy → Ingress → NetworkPolicy). MCPServer 가 이미 있으면 앞단을
        # 먼저 적용하고 받아들여지기를 기다린다. 앞단이 실패하면 서버의 Cedar 가 바뀌기 전에
        # 배포가 멈춘다 (P52 2절). 새 서버는 앞단 문서가 그 uid 를 써서 서버가
        # 먼저다. 껐으면 이름으로 정리한다
        front_first = doc.exposure.enabled and bool(doc.k8s_uid)
        if front_first:
            await self._apply_front(doc, client, effective, fresh=False)
        await self._apply_server(doc, client, docs)
        if doc.exposure.enabled and not front_first:
            await self._apply_front(doc, client, effective, fresh=False)
        elif not doc.exposure.enabled:
            await run_in_threadpool(
                delete_expose_objects, client, k8s.name, k8s.namespace, front_proxy=True
            )
            doc.svc_token_servers = []

    async def _ensure_spec_tokens(
        self,
        doc: MCPServer,
        client: KubeClient,
        rev: MCPServerRevision,
        backends: list[BackendAPI],
    ) -> None:
        """backend 마다 스펙 토큰 하나씩이고 backends 순서와 정렬된다. Secret 이 이미 있고 그 안의
        토큰이 문서가 가리키는 살아 있는 기록과 해시까지 맞으면 건너뛴다 (재실행 안전). 아니면 옛
        것을 회수하고 새로 내어 Secret 을 쓴다.

        id 를 저장한 뒤 Secret 을 쓰므로 그 사이에 멈추면 Secret 에는 회수된 옛 토큰이 남는다.
        기록만 보면 그 Secret 을 영영 건너뛰므로 해시를 견준다(Agent 의 `instance_token_current` 와
        같은 규칙, P66 2.7절). 순서를 바꿔 Secret 을 먼저 쓰면 저장 전에 멈췄을 때 새 토큰의 id 가
        문서에 없어 다음 실행이 회수하지 못하고 살아 있는 채 남으므로 이쪽을 골랐다."""
        k8s = self._k8s(doc)
        existing = await run_in_threadpool(
            client.get, "v1", "Secret", k8s.token_secret, k8s.namespace
        )
        token_ids = doc.token_ids
        tokens_alive = len(token_ids) == len(rev.backends)
        if tokens_alive:
            for i, (bp, tok_id) in enumerate(zip(backends, token_ids, strict=False)):
                tok = await self.backend.get_token(bp.id, tok_id)
                held = secret_value(existing, token_key(i, len(token_ids)))
                if tok is None or not tok.active or not held:
                    tokens_alive = False
                    break
                if hash_token(held) != tok.token_sha256:
                    tokens_alive = False
                    break
        if existing is not None and tokens_alive:
            return
        # 토큰을 회수하고 내기 전에 클레임을 확인한다. 클레임을 넘긴 실행이 새 홀더의 토큰을
        # 회수하거나 그 뒤에 토큰을 내지 않게 한다 (P66 2.2절)
        await checkpoint()
        # 옛 토큰은 id 만으로 회수한다. 리비전이 바뀌면 backends 목록이 달라져 짝짓기가
        # 어긋나고, 그러면 회수가 조용히 실패한 채 id 만 아래에서 덮여 영영 못 지운다
        await self._revoke_issued(token_ids)
        tokens: list[str] = []
        new_ids: list[str] = []
        for bp in backends:
            token, digest = issue_token()
            tok = await self.backend.create_token(
                bp.id, digest, f"instance:{doc.id}", [scope_for(doc.id)]
            )
            note_issued(tok.id)
            tokens.append(token)
            new_ids.append(tok.id)
        doc.spec_token_ids = new_ids
        doc.spec_token_id = new_ids[0]
        await self._save_deployment(
            doc,
            {"spec_token_ids", "spec_token_id"},
            expect_state={"deploying"},
            action="provision",
        )
        await run_in_threadpool(client.apply, render_token_secret(doc, tokens))

    async def _apply_consumer(self, doc: MCPServer, client: KubeClient, *, rotate: bool) -> None:
        """노출했으면 앞단을 먼저 다시 적용하고(회전이면 새 토큰으로), 회전이 아니면 MCPServer 를
        배포 스냅샷으로 다시 적용한다 (P50 1절). 인증 입력은 앞단이 실으므로 앞단이 먼저다.
        스냅샷이 지금 배포의 것이 아니면 500 integrity_error 로 막는다. MCPServer 단계(스냅샷 확인,
        저장된 overrides 의 검사, 적용)가 실패해도 앞단은 이미 새 인증으로 돌고, 실패는 그대로
        올라가 재적용이 이 문서를 막거나 미룬다. 회전은 앞단 토큰의 일이라 MCPServer 를 건드리지
        않는다. 스펙 토큰 Secret 은 평문이 발급 때만 있으므로 다시 쓰지 않는다."""
        effective = EffectiveConfig.model_validate(doc.effective_config)
        if doc.exposure.enabled:
            await self._apply_front(
                doc, client, effective, fresh=rotate, authz_wait=REAPPLY_AUTHZ_WAIT_SECONDS
            )
        if rotate:
            return
        if server_holds_rules(spec_of_revision(self._pinned(doc))) and not rules_toolhive_ok(
            await self._cluster_of(doc)
        ):
            # 0.53 아래(모르는 판 포함)에서는 규칙을 실을 수 없으므로 서버를 그대로 둔다 (P52 1절)
            logger.warning(
                "mcp server %s holds Tool rules on a cluster below ToolHive 0.53; "
                "its MCPServer keeps the spec it has (rules_toolhive_old)",
                doc.id,
            )
            return
        if not snapshot_fits(
            doc.deployed_inputs, self._pinned(doc), deploying_since=doc.deploying_since
        ):
            raise MCPServerError(
                500,
                "integrity_error",
                "the deploy snapshot is not the current deployment's; redeploy the server",
            )
        await self._apply_server(
            doc, client, await self.render_docs(doc), authz_wait=REAPPLY_AUTHZ_WAIT_SECONDS
        )

    async def _apply_server(
        self,
        doc: MCPServer,
        client: KubeClient,
        docs: list[dict[str, Any]],
        *,
        authz_wait: float = AUTHZ_WAIT_SECONDS,
    ) -> None:
        """`_render_docs` 묶음을 적용하고 MCPServer 의 uid 를 남긴다. MCPServer 에는 저장된
        overrides 를 얹고, 그것이 지금의 잠금 표에 어긋나면 아무것도 보내기 전에 422 locked_field 로
        멈춘다(`stored_final`). 테넌트 울타리 설정은 받아들여질 때까지 기다린 뒤 서버로 간다 (P50
        11절). 배포와 재렌더가 함께 쓴다."""
        final = self.editor.stored_final(doc, docs[-1])
        applied = await apply_docs(client, docs, final, MCPServerError, authz_wait=authz_wait)
        doc.k8s_uid = (applied.get("metadata") or {}).get("uid") or doc.k8s_uid
        if doc.stamp_pending:
            # 적용한 렌더의 표시로 판을 다시 적는다 (`_stamp_auth`)
            doc.auth_rev = _rendered_rev(doc)

    async def _apply_edit(self, doc: MCPServer, client: KubeClient, final: dict[str, Any]) -> None:
        """Advanced 저장과 드리프트 되돌리기·흡수의 적용 (`AdvancedEditor`). 게이트가 열린 뒤 아직
        재렌더되지 않은 서버라도 참조할 울타리 설정이 먼저 있게 한다 (P50 11절)."""
        await apply_docs(client, await self.render_docs(doc), final, MCPServerError)

    async def _apply_front(
        self,
        doc: MCPServer,
        client: KubeClient,
        effective: EffectiveConfig,
        *,
        fresh: bool,
        authz_wait: float = AUTHZ_WAIT_SECONDS,
    ) -> None:
        """노출 앞단의 서비스 토큰(fresh 면 새로, 아니면 Secret 의 것)과 그 판을 담은 앞단 객체를
        적용한다. 배포와 서비스 토큰 회전·인증 재적용이 함께 쓴다. 토큰은 이 MCP Server 하나의
        audience 를 싣는다 (P50 9절)."""
        k8s = self._k8s(doc)
        token = await self.svc_tokens.ensure(
            client,
            secret_name=expose_svc_name(k8s.name),
            namespace=k8s.namespace,
            kind="expose",
            name=k8s.name,
            tenant=doc.tenant,
            server_id=doc.id,
            fresh=fresh,
        )
        doc.svc_token_rev = svc_token_rev(token)
        doc.svc_token_servers = [doc.id]
        await apply_in_order(
            client,
            await self._expose_docs(doc, effective, svc_token=token),
            MCPServerError,
            wait=authz_wait,
        )

    async def want_rev(self, doc: MCPServer, ts: Any) -> str | None:
        """소비자 판에 파드가 믿는 Redis CA 의 지문(P61)과 자기 audience 로 옮겨 갔다는 표시(P50
        9절)를 더한다. 판을 적는 자리는 적용한 렌더의 것만 적으므로(`_stamp_auth`) 옮겨 간 서버와
        CA 가 바뀐 서버는 다시 렌더될 때까지 기대 판과 다르다. audience 표시를 바깥에 두어 옮겨 가는
        판이 CA 표시와 상관없이 알아보인다."""
        base = await self._base_rev(doc, ts)
        return own_audience_rev(redis_tls_rev(base, await self._redis_ca(doc)), doc.audience_switch)

    async def _base_rev(self, doc: MCPServer, ts: Any) -> str | None:
        """인증 입력의 소비자 판에 Tool 규칙의 지문을 얹은 것 (P52 2절, `rules_rev`)."""
        return rules_rev(await super().want_rev(doc, ts), await self._rules_mark(doc))

    async def _rules_mark(self, doc: MCPServer) -> str | None:
        """배포한 리비전이 규칙이나 조건을 들면 그 서버와 앞단이 지금 컴파일할 정책과 판정의
        지문이다. 없으면 None 이다."""
        if doc.state == "draft" or doc.k8s is None:
            return None
        rev = self._pinned(doc)
        if not server_holds_rules(spec_of_revision(rev)):
            return None
        # want_rev 와 paced_revs 가 한 틱에 함께 묻으므로 잠깐 기억한다. 조건의 기록이 바뀌면 렌더가
        # 달라지므로 그 판정도 키에 든다
        key = (doc.id, rev.revision, doc.state, doc.exposure.enabled, conditions_enforced(doc))
        if self._marks is None:
            self._marks = TTLCache(MARK_TTL_SECONDS, max_entries=4096)
        hit = self._marks.get(key)
        if hit is not None:
            return hit
        cluster = await self._cluster_of(doc)
        server = await self._rule_policies(doc, rev, cluster, cached=True)
        front = await self._front_rule_policies(doc, cluster, cached=True)
        mark = stable_hash([rules_toolhive_ok(cluster), server, front], 16)
        self._marks.put(key, mark)
        return mark

    async def paced_revs(self, doc: MCPServer, ts: Any) -> frozenset[str | None]:
        """Redis CA 표시만 다른 판. 표시가 없거나 마지막 렌더의 지문이다. CA 가 바뀐 것은 회수가
        아니라서 네임스페이스마다의 속도로 간다."""
        base = await self._base_rev(doc, ts)
        return frozenset(
            own_audience_rev(redis_tls_rev(base, mark), doc.audience_switch)
            for mark in (None, doc.redis_ca)
        )

    async def _stamp_auth(self, doc: MCPServer, *, rotate: bool = False) -> None:
        """서비스 토큰 회전은 앞단만 다시 쓰고 MCPServer 를 렌더하지 않으므로 마지막 렌더의 판을
        그대로 둔다. 회전이 지금의 CA 나 audience 표시를 적으면 다음 틱이 그 서버를 지금 것으로 보아
        MCPServer 가 옛 CA 와 옛 audience 에 남기 때문이다. 그 밖에는 렌더 앞의 인증 입력을 판의
        바탕으로 두고, 적용한 렌더의 Redis 와 audience 표시를 `_apply_server` 가 얹어 다시
        적는다."""
        if rotate:
            return
        ts = await self.tenancy.tenant_settings.get(doc.tenant)
        doc.set_stamp_base(await self._base_rev(doc, ts))
        passing = passing_features(await self._cluster_of(doc))
        doc.mark_rendered(_rendered_rev(doc), render_epoch(self.auth_kind, passing))

    async def _redis_ca(self, doc: MCPServer) -> str | None:
        """마지막 렌더가 TLS 로 붙인 Redis 들의 지금 CA 지문(`redis_ca_mark`, 출처는
        `_ca_sources`). 재적용 틱은 서버마다 이것을 보므로 같은 Database·클러스터·Secret 을 잠깐
        다시 읽지 않는다(`RecentReads`)."""
        sources = _ca_sources(doc)
        if not sources:
            return None
        fingerprints: dict[str, str | None] = {}
        for source in sources:
            if source == DEFAULT_REDIS_SOURCE:
                read, fingerprint = await self._recent.get(
                    ("redis-ca", doc.cluster_id, doc.k8s.namespace if doc.k8s else None),
                    lambda: self._namespace_ca(doc, None),
                )
                fingerprints[source] = fingerprint if read else _rendered_ca(doc, source)
            else:
                ds = await self._recent.database(self.data_services, source, doc.tenant)
                fingerprints[source] = _database_ca(ds)
        return redis_ca_mark(fingerprints)

    async def _redis_ca_now(
        self, doc: MCPServer, rev: MCPServerRevision, cluster: Cluster
    ) -> dict[str, str | None]:
        """렌더가 믿을 수 있는 Redis 의 지금 CA 지문(`render_mcpserver` 의 redis_ca). 리비전이
        바인딩한 Database 와 클러스터 기본 Redis 다."""
        out: dict[str, str | None] = {}
        for b in rev.bindings:
            ds = await self.data_services.get(b.data_service_id, tenant=doc.tenant)
            out[b.data_service_id] = _database_ca(ds)
        read, fingerprint = await self._namespace_ca(doc, cluster)
        out[DEFAULT_REDIS_SOURCE] = fingerprint if read else _rendered_ca(doc, DEFAULT_REDIS_SOURCE)
        return out

    async def _namespace_ca(
        self, doc: MCPServer, cluster: Cluster | None
    ) -> tuple[bool, str | None]:
        """세션 저장소가 믿는 기본 Redis CA 의 지문. 서버 네임스페이스의 Secret `daegam-redis-ca`
        가 지금 든 CA 다(`tenancy.default_redis_ca_secret`). 클러스터의 CA 가 바뀌어도 스윕이 그
        Secret 을 다시 쓰기 전에는 옛 지문이라, 다시 뜬 파드가 아직 옛 CA 를 붙일 때 다시 렌더하지
        않는다. cluster 가 None 이면 문서의 클러스터를 읽는다.

        (읽었는가, 지문)이다. Secret 이 없거나 기본 Redis 의 CA 가 네임스페이스에 놓이지 않으면
        (True, None) 이고, 읽기가 실패하면 (False, None) 이다. 부르는 쪽은 실패를 마지막 렌더의
        지문으로 바꾼다(`_rendered_ca`). 잠깐의 실패로 지문을 모른다고 렌더해 파드를 두 번 굴리지
        않으려는 것이다."""
        if doc.k8s is None:
            return True, None
        try:
            cluster = cluster or await self._cluster_of(doc)
            if not default_redis_ca_ready(cluster):
                return True, None
            client = await run_in_threadpool(self.cluster_service.client_for, cluster)
            data = await run_in_threadpool(
                K8sSecretStore(client).get, DEFAULT_REDIS_CA_SECRET, namespace=doc.k8s.namespace
            )
        except Exception as e:  # noqa: BLE001 - 렌더와 재적용은 마지막 렌더의 지문으로 간다
            logger.info("mcp server %s: default Redis CA unreadable: %s", doc.id, e)
            return False, None
        return True, ca_sha256((data or {}).get(REDIS_CA_KEY))

    async def _expose_docs(
        self, doc: MCPServer, effective: EffectiveConfig, *, svc_token: str | None = None
    ) -> list[dict[str, Any]]:
        """앞단 묶음. 인증 입력은 소비자 판과 같은 함수에서 온다 (P32 8절)."""
        k8s = self._k8s(doc)
        auth = await self._consumer_auth(doc.tenant)
        cluster = await self._cluster_of(doc)
        owner = owner_ref("MCPServer", k8s.name, doc.k8s_uid) if doc.k8s_uid else None
        rules, roles = await self._front_rule_policies(doc, cluster)
        return mcp_expose_docs(
            tenant=doc.tenant,
            instance_id=doc.id,
            k8s_name=k8s.name,
            namespace=k8s.namespace,
            exposure=doc.exposure,
            effective=effective,
            owner=owner,
            oidc_ref=auth.oidc_ref(doc.exposure.external_url) if auth.token_auth else None,
            tenant_group=auth.tenant_group,
            agent_client_ids=list(auth.agent_client_ids),
            client_id_claim=auth.client_id_claim,
            groups_claim=auth.groups_claim,
            svc_token=svc_token,
            svc_token_rev=doc.svc_token_rev,
            # 역할 없는 ServiceAccount 와 Cedar 참조 (P50 8·10절). 게이트를 지나지 못한 클러스터는
            # 오늘의 꼴이다
            service_account=proxy_service_account(cluster),
            authz_by_ref=Feature.AUTHZ_REF in passing_features(cluster),
            binds_delegation=auth.binds_delegation,
            rules=rules,
            roles_claim=ROLES_CLAIM if roles else None,
        )

    async def _front_rule_policies(
        self, doc: MCPServer, cluster: Cluster, *, cached: bool = False
    ) -> tuple[list[str], bool]:
        """노출 앞단의 Tool 규칙 forbid 와 역할 절이 있는가 (P52 2절). 앞단은 서버의 규칙을 사람
        절로 지킨다(Agent 절은 없다). 0.53 아래(모르는 판 포함)에서는 규칙이 있는 Tool 을 모두
        닫는다. header 모드는 누가 부를지의 규칙을 렌더하지 않는다."""
        if self.rule_scopes is None or doc.state == "draft":
            return [], False
        rev = self._pinned(doc)
        rules = server_rules_by_name(spec_of_revision(rev))
        names = ruled_names(rules)
        inputs = await self._rule_inputs(doc, rev, cluster, cached=cached) if names else None
        if inputs is None:
            return [], False
        scope, earlier = inputs
        if not scope.enforces_callers:
            return [], False
        if not rules_toolhive_ok(cluster):
            closed = [n for name in names for n in [name, *earlier.get(name, ())]]
            return closing_forbid(closed), False
        forbids = source_forbids(rules, scope, "front", names=earlier)
        callers = [rules[n].callers for n in names]
        return forbids, roles_claimed(callers, scope)

    # -- 상태 ------------------------------------------------------------------

    async def _status_fields(self, doc: MCPServer, observed_at: datetime) -> dict[str, Any]:
        """CR status 를 매 요청 조회해 매핑하는 상태의 키(틀은 기반의 `status`)."""
        warnings = unauthenticated_warnings(doc.exposure, token_auth=self.token_auth)
        try:
            warnings += await self._rule_warnings(doc)
        except ServiceError:
            # 클러스터 문서가 없으면 상태가 그 줄을 따로 낸다. 규칙 경고는 보탤 것이 없다
            pass
        return {
            **status_base(doc, observed_at, warnings=warnings, error=None),
            "url": None,
            "ready_replicas": None,
        }

    async def _rule_warnings(self, doc: MCPServer) -> list[str]:
        """배포한 리비전의 Tool 규칙이 낳는 경고 (P52 7절). header 모드는 누가 부를지의 규칙을
        지키지 않고, 조건이 있는 Tool 은 모든 파드가 조건을 지키는지 알 때까지 닫히며, 서버가 풀지
        못한 조건은 그 Tool 을 닫고, 0.53 아래 클러스터에서는 규칙을 실을 수 없다."""
        if doc.state == "draft":
            return []
        spec = spec_of_revision(self._pinned(doc))
        rules = [r for sel in spec.backends for r in sel.tool_rules.values()]
        out: list[str] = []
        if not self.token_auth and any(has_caller_rule(r) for r in rules):
            out.append(rule_warning(RULES_NOT_ENFORCED))
        if any(r.conditions for r in rules) and not conditions_enforced(doc):
            out.append(rule_warning(CONDITIONS_CLOSED))
        unresolved = unresolved_conditions(doc)
        if unresolved:
            out.append(rule_warning(CONDITIONS_UNRESOLVED, ", ".join(unresolved)))
        cluster = await self._cluster_of(doc)
        if server_holds_rules(spec) and not rules_toolhive_ok(cluster):
            out.append(rule_warning(RULES_TOOLHIVE_OLD))
        if not self.token_auth or not any(has_caller_rule(r) for r in rules):
            return out
        behind = await self._gateways_behind(doc, spec)
        if behind:
            out.append(rule_warning(RULES_GATEWAYS_BEHIND, ", ".join(behind)))
        if doc.exposure.enabled and doc.k8s is not None:
            try:
                client = await self.client_for_doc(doc)
            except ServiceError:
                client = None
            front = (
                await read_deployment(client, expose_proxy_name(doc.k8s.name), doc.k8s.namespace)
                if client is not None
                else None
            )
            if front is not None and not rolled_out(front):
                out.append(rule_warning(RULES_FRONT_ROLLING))
        return out

    async def _gateways_behind(self, doc: MCPServer, spec: MCPServerSpec) -> list[str]:
        """이 서버를 백엔드로 두고 그 Entry 토큰이 지금 규칙 지문을 싣지 않은 Gateway 의 이름
        (P52 2절). 그 Gateway 를 거친 규칙 있는 Tool 은 다시 렌더될 때까지 부를 수 없다."""
        if self.rule_scopes is None:
            return []
        scope = await self.rule_scopes.scope(doc.tenant, cached=True)
        if not scope.enforces_callers:
            return []
        digest = rules_digest(doc.id, server_rules_by_name(spec), scope)
        out = []
        for gateway in await self.gateway_instances.list(doc.tenant):
            if owner_cedar(gateway):
                continue
            for b in gateway.deployed_backends:
                if b.server_id == doc.id and b.rules_digest != digest:
                    out.append(gateway.name)
                    break
        return sorted(out)

    async def _status_map(
        self,
        doc: MCPServer,
        client: KubeClient,
        obj: dict[str, Any],
        out: dict[str, Any],
        observed_at: datetime,
        *,
        listing: bool,
    ) -> dict[str, Any]:
        """읽은 MCPServer CR 을 phase·message 로 옮긴다. 스펙을 바꾼 직후의 Ready 는 프록시
        Deployment 의 롤아웃까지 보고, Pending 이면 파드의 대기 이유를 싣는다."""
        k8s = self._k8s(doc)
        mapped = {**out, **map_cr_status(obj, doc.last_spec_change, observed_at)}
        if mapped["phase"] == "Ready" and changed_within(
            doc.last_spec_change, observed_at, ROLLOUT_WATCH
        ):
            # 스펙을 바꾼 직후에는 오퍼레이터가 observedGeneration 을 먼저 올리고 옛 파드 기준
            # Ready 를 그대로 둔다. 프록시 Deployment 가 다 굴러갔는지 보고서야 Ready 라고 답한다
            proxy = await read_deployment(client, k8s.name, k8s.namespace)
            mapped = {**out, **map_cr_status(obj, doc.last_spec_change, observed_at, proxy=proxy)}
        mapped, _pods = await self._with_pod_waiting(doc, client, mapped)
        return mapped

    async def item_status(
        self, item: HubItem, server: MCPServer | None = None
    ) -> dict[str, Any] | None:
        """internal 허브 항목의 상태. 그 원본 MCP Server 의 상태라 허브 화면과 Gateway 배포 검사가
        이 화면과 같은 phase 를 본다 (T55). 원본이 없거나 draft 면 None 이다. 허브 상태는 phase 만
        쓰므로 노출 상태는 읽지 않는다. 부르는 쪽이 원본 문서를 이미 읽었으면 server 로 받는다."""
        doc = server
        if doc is None and item.source.instance_id:
            doc = await self.instances.get(item.source.instance_id)
        return await self.status(doc, with_exposure=False) if doc is not None else None

    # -- 해제·삭제 ---------------------------------------------------------------

    async def _delete_steps(self, doc: MCPServer) -> None:
        # 1. 토큰 회수. 내리는 중이므로 파드 재시작이 401 로 실패해도 상관없다.
        # id 만으로 지운다. 리비전이 이미 없어도 회수는 되어야 한다
        await self._revoke_issued(doc.token_ids)
        k8s = self._k8s(doc)
        client = await self._cleanup_client(doc)
        if client is None:
            return
        # 2. 노출 객체 (ownerReference 로 GC 되지만 이름으로 한 번 더) → MCPServer
        await run_in_threadpool(
            delete_expose_objects, client, k8s.name, k8s.namespace, front_proxy=True
        )
        await run_in_threadpool(client.delete, TOOLHIVE_API, "MCPServer", k8s.name, k8s.namespace)
        # 테넌트 울타리 설정 (P50 11절). 소유자가 없어 이름으로 지운다. 게이트가 닫힌 뒤에도 남은
        # 것을 지우도록 렌더와 상관없이 지운다
        await delete_authz_config(client, k8s.name, k8s.namespace)
        # 3. 토큰 Secret
        await run_in_threadpool(client.delete, "v1", "Secret", k8s.token_secret, k8s.namespace)

    async def _revoke_unrecorded(self, doc: MCPServer) -> None:
        """문서에 id 를 적기 전에 만든 스펙 토큰까지, 이 인스턴스의 라벨과 scope 를 단 토큰을 모두
        회수한다 (P66 2.5절)."""
        await self.backend.revoke_tokens_labeled(f"instance:{doc.id}", scope_for(doc.id))

    async def _revoke_issued(self, ids: list[str]) -> None:
        """스펙 토큰을 id 만으로 회수한다. 어느 Backend API 것인지 다시 맞추지 않는다."""
        for tok_id in ids:
            await self.backend.revoke_token_by_id(tok_id)

    async def _guard_undeploy(self, doc: MCPServer) -> None:
        """게시돼 있으면 409 published, Agent 가 도구 소스로 참조 중이면 409 in_use (D42. 사용자가
        누르는 동작이라 파생물을 조용히 끊지 않는다). 게시는 다시 몰아가는 해제에도 본다."""
        if await self.hub_item(doc) is not None:
            raise MCPServerError(
                409, "published", f"mcp server {doc.name} is published to the hub; unpublish first"
            )
        if doc.state != "undeploying":
            await self._check_in_use(doc)

    async def _guard_delete(self, doc: MCPServer) -> None:
        """참조 중이면 draft 도 지우지 않는다(409 in_use). 게시는 막지 않고 삭제가 항목을
        정리한다."""
        await self._check_in_use(doc)

    async def _after_cluster_removed(self, doc: MCPServer) -> None:
        """게시한 허브 항목을 정리한다."""
        if self.hub is not None:
            await self.hub.unregister_internal(doc.id)

    # -- 재배포 ----------------------------------------------------------------

    async def redeploy(self, doc: MCPServer, *, claim_host: bool = False) -> MCPServer:
        """현재 리비전으로 핀을 옮기고 effective_config 를 다시 계산해 SSA 재적용한다.
        토큰은 유지한다. claim_host 면 메모리의 새 노출을 deploying 전이와 함께 저장하며 그 host 를
        선점한다(Basic 편집, P70 2절). 문서의 클레임을 쥐고 돈다. 남이 쥐고 있으면 409
        in_progress 다 (P66 F3)."""
        self._require(doc, EDIT_STATES, action="redeploy")
        async with self._claim_scope(doc, EDIT_STATES, action="redeploy"):
            out = await self._redeploy_claimed(doc, claim_host=claim_host)
        return out

    async def _redeploy_claimed(self, doc: MCPServer, *, claim_host: bool) -> MCPServer:
        tenant = doc.tenant
        rev = doc.current
        cluster = await self._writable_cluster_of(doc)
        if server_holds_rules(spec_of_revision(rev)):
            require_rules_toolhive(cluster)
        k8s = self._k8s(doc)
        services = await self._load_bindings(tenant, rev.bindings, cluster.id)
        backends, _versions = await self._load_backend_apis(tenant, rev.backends)
        effective = await self._effective(tenant, cluster, rev)
        await self._require_namespace_still_allowed(tenant, cluster, k8s.namespace)
        effective.namespace.name = k8s.namespace
        cr = _render_docs(
            doc,
            cluster,
            rev,
            backends,
            services,
            effective,
            session=session_options(cluster),
            redis_ca=await self._redis_ca_now(doc, rev, cluster),
        )[-1]
        if doc.overrides:
            await self._dry_run_overrides(doc, cr)
        record = doc.conditions_record
        if record is not None and not conditions_match(doc, record.revision, rev.revision):
            # 조건이 다른 리비전으로 옮기면 기록을 지운다. 기록의 리비전 뒤에 배포한 리비전이 모두
            # 같은 조건이게 하려는 것이다 (P52 4절, `services/condition_records.py`)
            doc.conditions_record = None
            await self.servers.set_conditions_record(doc.id, None)
        doc.deployed_revision = rev.revision
        doc.effective_config = effective.model_dump(mode="json")
        doc.state = "deploying"
        doc.deploying_since = utcnow()
        async with self._host_claimed(doc, claim=claim_host):
            await self._transition(doc, EDIT_STATES, action="redeploy")
        self._place_claim(doc)
        # 재배포의 적용 단계는 배포와 같다. 실패 대응·상태 전이도 provision 것을 그대로 쓴다
        doc = await self.provision(doc)
        await self._hub_sync(doc)
        return doc

    async def _context(self, doc: MCPServer, *, from_documents: bool = False):
        """배포가 핀한 렌더 재료. (클러스터, 리비전, Backend API, 바인딩 Database, effective).
        from_documents 가 아니고 지금 배포의 스냅샷(`deployed_inputs`, P50 1절)이 핀된 리비전에
        맞으면 Backend API·Database 를 현재 공유 문서 대신 스냅샷에서 짓는다. provision 은
        from_documents 로 현재 공유 문서를 읽고 그것을 새 스냅샷으로 적는다."""
        cluster = await self._cluster_of(doc)
        rev = self._pinned(doc)
        effective = EffectiveConfig.model_validate(doc.effective_config)
        inputs = doc.deployed_inputs
        if (
            not from_documents
            and inputs is not None
            and snapshot_fits(inputs, rev, deploying_since=doc.deploying_since)
        ):
            backends = backend_apis_from(inputs, doc.tenant)
            services = data_services_from(inputs, doc.tenant)
        else:
            backends, _versions = await self._load_backend_apis(doc.tenant, rev.backends)
            services = await self._load_bindings(doc.tenant, rev.bindings, cluster.id)
        return cluster, rev, backends, services, effective

    async def render_docs(self, doc: MCPServer) -> list[dict[str, Any]]:
        """배포가 핀한 재료로 다시 렌더링한 [테넌트 울타리 설정?, MCPServer] (overrides 전). 배포한
        문서는 지금 배포의 스냅샷을 읽고, provision 이 도는 상태(`PROVISION_STATES`)와 지금 배포의
        스냅샷이 없는 문서는 현재 공유 문서를 읽는다. 재렌더는 스냅샷이 지금 배포의 것인지 먼저
        본다(`_apply_consumer`)."""
        from_documents = doc.state in PROVISION_STATES
        cluster, rev, backends, services, effective = await self._context(
            doc, from_documents=from_documents
        )
        if from_documents:
            session = session_options(cluster)
        else:
            pinned = bool(doc.deployed_inputs and doc.deployed_inputs.session_tls)
            session = session_options(cluster, pinned=pinned)
        redis_ca = await self._redis_ca_now(doc, rev, cluster)
        return _render_docs(
            doc,
            cluster,
            rev,
            backends,
            services,
            effective,
            session=session,
            redis_ca=redis_ca,
            rules=await self._rule_policies(doc, rev, cluster),
        )

    # -- F7a: manifest / overrides / drift (services/advanced_edit.py) -------------------

    async def manifest(self, doc: MCPServer) -> dict[str, Any]:
        """{documents: [{kind, name, namespace, rendered, overrides, final, live, drift, ...}]}
        첫 문서가 MCPServer 다. 테넌트 울타리 설정과 노출 문서는 라이브만 읽고 drift 는 계산하지
        않는다. 울타리 설정은 통째로 잠긴다."""
        self._k8s(doc)
        rendered_docs = await self.render_docs(doc)
        docs = [await self.editor.document(doc, rendered_docs[-1])]
        for rendered in rendered_docs[:-1]:
            docs.append(await self.editor.document(doc, rendered, locked=["*"], drift=False))
        if doc.exposure.enabled:
            # 노출 문서: 앞단 프록시는 인증 경로가 잠기고 Ingress·NetworkPolicy 는 전부 잠긴다
            _cluster, _rev, _backends, _services, effective = await self._context(doc)
            for rendered in await self._expose_docs(doc, effective):
                is_proxy = rendered["kind"] == "MCPRemoteProxy"
                locked = locked_path_strings(EXPOSE_PROXY_LOCKED_PATHS) if is_proxy else ["*"]
                docs.append(await self.editor.document(doc, rendered, locked=locked, drift=False))
        return {"documents": docs}

    # -- F7a: Basic 편집 ---------------------------------------------------------------

    async def basic_patch(
        self,
        doc: MCPServer,
        *,
        replicas: int | None = None,
        resources: dict[str, Any] | None = None,
        image: str | None = None,
        bindings: list[Binding] | None = None,
        exposure: ExposureInput | None = None,
        sent: set[str] | None = None,
        user_sub: str | None = None,
    ) -> MCPServer:
        """폼 필드를 받아 새 리비전(정의 필드가 왔을 때)을 만들고 redeploy 한다. exposure 는
        정의가 아니라 배포 설정이라 리비전 없이 재배포로만 반영된다. sent 로 null
        지우기(resources/image)를 구분한다."""
        sent = sent or set()
        doc = await self._fresh(doc)
        self._require(doc, EDIT_STATES, action="edit")
        # 새 리비전과 노출을 메모리에 얹기 전에 클레임을 쥔다. 재배포가 그것을 물려받는다 (P66 F3)
        async with self._claim_scope(doc, EDIT_STATES, action="edit"):
            # 끝은 재배포라 노출을 저장하기 전에 쓸 수 있는 클러스터인지 본다 (P63 2절)
            cluster = await self._writable_cluster_of(doc)
            k8s = self._k8s(doc)
            if "exposure" in sent:
                was_exposed = doc.exposure.enabled
                doc.exposure, _warnings = await self._validate_exposure(
                    exposure,
                    tenant=doc.tenant,
                    name=doc.name,
                    cluster=cluster,
                    namespace=k8s.namespace,
                    exclude_id=doc.id,
                )
                if doc.exposure.enabled and not was_exposed:
                    # 이미 도는 앞단은 오늘의 꼴로 남고, 새 앞단만 거절한다 (P50 2절 표)
                    require_proxy_account(cluster, MCPServerError)
            # 새 노출은 따로 저장하지 않는다. 재배포가 deploying 으로 옮기는 같은 저장이 쓴다.
            # 재배포가 시작하지 못하면 아무것도 저장되지 않아 옛 host 를 놓지 않는다 (P70 2절)
            claim_host = "exposure" in sent
            if not sent - {"exposure"}:
                return await self.redeploy(doc, claim_host=claim_host)
            cur = doc.spec
            runtime = ProfileRuntimeSpec(
                image=image if "image" in sent else cur.runtime.image,
                replicas=replicas if replicas is not None else cur.runtime.replicas,
                resources=resources if "resources" in sent else cur.runtime.resources,
            )
            new_bindings = list(bindings) if bindings is not None else list(cur.bindings)
            _check_env_prefixes(new_bindings)
            spec = MCPServerSpec(backends=cur.backends, runtime=runtime, bindings=new_bindings)
            await self._load_bindings(doc.tenant, spec.bindings, cluster.id)
            await self._new_revision(doc, spec, user_sub)
            return await self.redeploy(doc, claim_host=claim_host)

        # -- F7a: 미리보기 -----------------------------------------------------------------

    async def preview(
        self, doc: MCPServer, rev: MCPServerRevision, cluster_id: str | None
    ) -> dict[str, Any]:
        """이 리비전을 배포하면 생길 MCPServer CR. apply 하지 않는다."""
        tenant = doc.tenant
        cluster = await self.tenancy.resolve_cluster(tenant, cluster_id)
        services = await self._load_bindings(tenant, rev.bindings, cluster.id)
        backends, _versions = await self._load_backend_apis(tenant, rev.backends)
        effective = await self._effective(tenant, cluster, rev)
        preview_id = f"preview-{doc.id}"
        sample = MCPServer(
            id=preview_id,
            audience_switch=doc.audience_switch,
            tenant=tenant,
            name=doc.name,
            spec=spec_of_revision(rev),
            revision=rev.revision,
            revisions=[rev],
            state="deploying",
            deployed_revision=rev.revision,
            cluster_id=cluster.id,
            k8s=InstanceK8s(
                name=k8s_name_for(doc.name),
                namespace=effective.namespace.name,
                group=group_name_for(tenant),
                token_secret=token_secret_name_for(preview_id),
            ),
            effective_config=effective.model_dump(mode="json"),
        )
        cr = _render_docs(
            sample,
            cluster,
            rev,
            backends,
            services,
            effective,
            server_id=doc.id,
            session=session_options(cluster),
            redis_ca=await self._redis_ca_now(sample, rev, cluster),
        )[-1]
        return {
            "cluster_id": cluster.id,
            "namespace": effective.namespace.name,
            "effective_config": sample.effective_config,
            "manifest": cr,
            "locked": locked_path_strings(),
        }


def spec_of_revision(rev: MCPServerRevision) -> MCPServerSpec:
    """리비전에서 spec 필드만. 채택 결과가 지금과 같은지 견줄 때와 새 spec 을 지을 때 쓴다."""
    return MCPServerSpec(
        backends=list(rev.backends), runtime=rev.runtime, bindings=list(rev.bindings)
    )


@dataclass(frozen=True)
class SessionOptions:
    """세션 저장소 TLS 의 렌더 인자 (P61 3절). `render_mcpserver` 의 같은 이름 인자다."""

    session_tls: bool
    default_redis_ca: bool

    def render_kwargs(self) -> dict[str, bool]:
        return {"session_tls": self.session_tls, "default_redis_ca": self.default_redis_ca}


def session_options(cluster: Cluster, *, pinned: bool | None = None) -> SessionOptions:
    """세션 저장소가 TLS 로 붙을지. 배포는(pinned 가 None) 지금 `session_tls` 게이트로 정하고 그
    판단을 스냅샷에 적는다. 다시 렌더는 그 판단(pinned)을 따르되 판이 0.52 아래로 알려졌을 때만
    평문으로 내린다. 판을 모르면 배포 때의 판단을 둔다. 기본 Redis 는 그 CA 가 테넌트
    네임스페이스에 놓일 때만 TLS 다(`default_redis_ca_ready`)."""
    if pinned is None:
        tls = Feature.SESSION_TLS in passing_features(cluster)
    else:
        tls = pinned and not known_below(cluster, Feature.SESSION_TLS)
    return SessionOptions(session_tls=tls, default_redis_ca=default_redis_ca_ready(cluster))


def _closed_session_redis(
    rev: MCPServerRevision,
    services: Mapping[str, DataService],
    effective: EffectiveConfig,
    session: SessionOptions,
) -> tuple[bool, bool]:
    """(세션 저장소를 둘 Redis 의 평문 포트가 닫혔는가, 그 Redis 의 CA 를 붙일 수 있는가). 세션
    저장소는 첫 Redis 바인딩이고 없으면 클러스터 기본 Redis 다(`_session_storage`). 기본 Redis 의 CA
    는 테넌트 네임스페이스에 놓일 때만 붙는다(`default_redis_ca`)."""
    for b in rev.bindings:
        ds = services.get(b.data_service_id)
        if ds is not None and ds.kind == "redis":
            return ds.tls_only, True
    default = effective.default_redis
    return default is not None and default.tls_only, session.default_redis_ca


def _closed_session_refusal(
    cluster: Cluster,
    rev: MCPServerRevision,
    services: Mapping[str, DataService],
    effective: EffectiveConfig,
    session: SessionOptions,
) -> dict[str, str] | None:
    """세션 저장소를 둘 Redis 의 평문 포트가 닫혔는데 TLS 로 렌더할 수 없으면 그 422 본문이다 (P61
    5절 5단계). `session_tls` 게이트를 지나지 못한 클러스터는 그 게이트의 본문이고, 기본 Redis 의
    CA 를 테넌트 네임스페이스에 둘 수 없는 클러스터는 권한의 본문이다. 다시 렌더는 바인딩과 기본
    Redis 의 닫힌 표시를 배포 스냅샷에서 읽으므로 배포 때의 판단을 따른다."""
    if effective.replicas < SESSION_REPLICAS:
        return None
    closed, ca_ready = _closed_session_redis(rev, services, effective, session)
    if not closed or (session.session_tls and ca_ready):
        return None
    if not session.session_tls:
        gate = cluster_gate(cluster, Feature.SESSION_TLS)
        return gate_error(gate) if not gate.ok else None
    rights = rights_gate(
        Feature.SESSION_TLS,
        f"the default Redis CA in tenant namespaces, placed through {TENANT_OBJECTS_RIGHTS}",
        toolhive_has(cluster),
    )
    return tenant_secret_refusal(cluster) or gate_error(rights)


def custom_image_tls_warnings(
    rev: MCPServerRevision,
    endpoints: Mapping[str, DataService | BindingInputs],
    before: MCPServerInputs | None = None,
) -> list[str]:
    """사용자 이미지로 도는 서버의 Redis 바인딩이 TLS 포트와 `rediss://` 로 바뀐다는 경고다.
    endpoints 는 이번 배포가 렌더한 Database(id 마다 문서나 스냅샷 바인딩)이고, before 는 앞 배포의
    스냅샷이다. 앞 배포가 이미 TLS 로 렌더한 바인딩은 경고하지 않는다. mcp-runtime 은 Redis 바인딩을
    읽지 않으므로 사용자 이미지만 해당한다."""
    if not rev.runtime.image:
        return []
    had = {b.data_service_id for b in (before.bindings if before else []) if b.tls is not None}
    return [
        f"{REDIS_TLS_BINDING_WARNING}: binding {b.env_prefix} uses the TLS port and rediss://; "
        f"the image must read {b.env_prefix}_TLS and {b.env_prefix}_CA_FILE"
        for b in rev.bindings
        if b.data_service_id not in had
        and (ds := endpoints.get(b.data_service_id)) is not None
        and ds.kind == "redis"
        and ds.tls is not None
    ]


def redeploy_warnings(doc: MCPServer, before: MCPServerInputs | None) -> list[str]:
    """재배포(Basic 편집 포함)의 경고. 이번 배포의 스냅샷과 앞 배포의 스냅샷을 견준다."""
    inputs = doc.deployed_inputs
    if inputs is None:
        return []
    endpoints = {b.data_service_id: b for b in inputs.bindings}
    return custom_image_tls_warnings(doc.current, endpoints, before)


def _rendered_rev(doc: MCPServer) -> str | None:
    """적용한 렌더의 판 첫 절반. 렌더 앞의 인증 입력(`MCPServer.stamp_base`)에 그 렌더가 단 CA
    지문(`redis_ca`)과 audience 표시를 얹는다."""
    return own_audience_rev(redis_tls_rev(doc.stamp_base, doc.redis_ca), doc.audience_switch)


def _ca_sources(doc: MCPServer) -> list[str]:
    """마지막 렌더가 TLS 로 붙인 Redis 의 출처. 그 렌더가 단 지문에서 읽는다. 다시 렌더는 바인딩을
    스냅샷에서 읽으므로 출처는 재배포 전까지 그대로다."""
    return redis_ca_sources(doc.redis_ca)


def _rendered_ca(doc: MCPServer, source: str) -> str | None:
    """마지막 렌더가 그 출처에 단 지문. 없으면 None 이다."""
    return redis_ca_fingerprints(doc.redis_ca).get(source)


def _database_ca(ds: DataService | None) -> str | None:
    """Database 의 CA 지문. 지문을 모르거나 TLS 끝점이 없으면 None 이다."""
    return ds.tls.ca_sha256 if ds is not None and ds.tls is not None else None


def _render_docs(
    doc: MCPServer,
    cluster: Cluster,
    rev: MCPServerRevision,
    backends: list[BackendAPI],
    services: dict[str, DataService],
    effective: EffectiveConfig,
    *,
    session: SessionOptions,
    redis_ca: Mapping[str, str | None],
    server_id: str | None = None,
    rules: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """[테넌트 울타리 설정?, 플랫폼 MCPServer CR]. 클러스터가 지나는 게이트를 한 번 계산해 둘에
    넘긴다 (P50 2·11절). 렌더 실패는 스펙 탓이라 422 다. redis_ca 는 지금 CA 지문이고
    (`_redis_ca_now`), 렌더가 파드에 단 지문을 문서에 적는다(`MCPServer.redis_ca`). rules 는 그
    서버의 Tool 규칙 forbid 이고 울타리에 붙는다 (P52 2절)."""
    refusal = _closed_session_refusal(cluster, rev, services, effective, session)
    if refusal is not None:
        raise MCPServerError(422, **refusal)
    features = passing_features(cluster)
    fence = (
        tenant_fence(
            features,
            cr_name=doc.k8s.name,
            namespace=doc.k8s.namespace,
            tenant=doc.tenant,
            id=doc.id,
            rules=rules,
        )
        if doc.k8s is not None
        else None
    )
    try:
        cr = render_mcpserver(
            doc,
            effective,
            rev,
            backends,
            cluster,
            services,
            features=features,
            fence=fence.spec if fence is not None else None,
            server_id=server_id,
            redis_ca=redis_ca,
            **session.render_kwargs(),
        )
    except RenderError as e:
        raise MCPServerError(status_of(e.code, 422), e.code, e.detail) from e
    # 지문이 없는 렌더는 빈 값으로 적는다. 2.0.0-rc1 의 `_ca_sources` 는 None 을 지문을 적기 전의
    # 렌더로 읽고 배포 스냅샷에서 출처를 고르므로, 그 판으로 되돌려도 같게 읽히게 한다
    pod = cr["spec"]["podTemplateSpec"]
    doc.redis_ca = template_annotation({"spec": {"template": pod}}, REDIS_CA_ANNOTATION) or ""
    if fence is not None and fence.config is not None:
        return [fence.config, cr]
    return [cr]


def _check_env_prefixes(bindings: Sequence[Binding]) -> None:
    """바인딩의 env_prefix 는 서로 달라야 한다. 422 duplicate_env_prefix."""
    prefixes = [b.env_prefix for b in bindings]
    if len(set(prefixes)) != len(prefixes):
        raise MCPServerError(422, "duplicate_env_prefix", "bindings must have distinct env_prefix")


# ToolHive 오퍼레이터가 프록시 Deployment 파드 템플릿에 남기는, 반영한 MCPServer 세대 (0.51.2 실측)
MCPSERVER_GENERATION_ANNOTATION = "toolhive.stacklok.dev/mcpserver-generation"


def proxy_caught_up(deployment: dict[str, Any], generation: int) -> bool:
    """프록시 Deployment 가 이 CR 세대로 다 굴러갔는가. 오퍼레이터는 파드 템플릿에 반영한 CR 세대를
    `MCPSERVER_GENERATION_ANNOTATION` 으로 남긴다. 그 어노테이션이 없는 판은 롤아웃만 본다."""
    seen = template_annotation(deployment, MCPSERVER_GENERATION_ANNOTATION)
    if seen is not None and seen != str(generation):
        return False
    return rolled_out(deployment)


def map_cr_status(
    cr: dict[str, Any],
    last_spec_change,
    observed_at,
    *,
    proxy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ToolHive MCPServer status → 플랫폼 phase. 순수 함수.

    proxy 는 스펙 변경 직후 Ready 를 확인하려고 읽은 프록시 Deployment 다. 그것이 아직 이 세대로
    다 굴러가지 않았으면 Rolling 이다. 오퍼레이터는 스펙을 받자마자 observedGeneration 을 올리지만
    phase 는 옛 파드 기준 Ready 로 두고, 그 뒤 롤아웃 동안 잠깐 Failed 를 낸다 (P50 e2e-gateway)."""
    meta = cr.get("metadata") or {}
    st = cr.get("status") or {}
    phase = st.get("phase") or "Pending"
    message = st.get("message")
    conditions = condition_rows(st)
    generation = meta.get("generation") or 0
    observed = st.get("observedGeneration")
    rolling = False
    if observed is not None and generation > observed:
        rolling = True
    elif phase == "Failed" and changed_within(last_spec_change, observed_at):
        rolling = True
    elif phase == "Ready" and proxy is not None and not proxy_caught_up(proxy, generation):
        rolling = True
        message = f"proxy Deployment not yet rolled out to spec generation {generation}"
    if rolling:
        message = message or f"spec generation {generation} not yet observed"
        phase = "Rolling"
    elif phase == "Failed" and not message:
        message = failed_condition_message(conditions)
    return {
        "phase": phase,
        "url": st.get("url"),
        "message": message,
        "conditions": conditions,
        "ready_replicas": st.get("readyReplicas"),
        "generation": generation,
        "observed_generation": observed,
    }


def version_refs_of(rev: MCPServerRevision) -> set[tuple[str, int]]:
    """이 리비전의 노출 이름을 계산하려면 읽어야 하는 (backend_profile_id, spec_version) 짝.

    `tools` 필터가 있는 backend 는 그 목록이 곧 이름이라 버전을 읽을 필요가 없다. 필터가 없을 때만
    백엔드가 무엇을 내는지 버전 문서에서 본다.
    """
    return {(sel.backend_profile_id, sel.spec_version) for sel in rev.backends if not sel.tools}
