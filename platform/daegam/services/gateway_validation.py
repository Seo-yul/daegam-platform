"""Gateway 정의 검증 (P72 2.2절, D23). 백엔드 허브 항목을 그 사람이 보고 고를 수 있는지, 같은
클러스터인지, 고른 도구가 그 항목에 있는지, 허브 그래프에 순환이 없는지, authz_policy 가 Cedar
문장으로 파싱되는지 본다. 저장·배포·재배포·미리보기가 함께 부른다. `GatewayValidator` 는 서비스가
생성자에서 만든다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from daegam.auth.cedar import CedarSyntaxError
from daegam.auth.hub_access import can
from daegam.auth.principal import Principal
from daegam.config.resolve import image_digest
from daegam.errors import ServiceError
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.advertised_names import plan_conflicts
from daegam.services.cedar_check import CedarCheckUnavailable, parse_statements
from daegam.services.gateway_code_mode import (
    CodeModeVerdict,
    code_mode_verdict,
    toolhive_refusal,
)
from daegam.services.gateway_plan import (
    backend_snapshot_of,
    entry_name,
    priority_unknown_warning,
    spec_of_revision,
    tool_names_of,
    unknown_names_hint,
)
from daegam.services.hub import HubService, proxied
from daegam.services.platform_tools import check_backends, require_same_cluster
from daegam.services.published_backends import owner_forbids, published_source
from daegam.services.rule_compile import source_forbids
from daegam.services.tool_effects import SourceTools, ToolEffectLoader, without_script
from daegam.services.tool_pins import PINNED_AGENTS_KEEP_TOOLS, pin_states, pins_behind
from daegam.services.tool_rules import (
    ToolRuleError,
    check_callers,
    check_cedar_size,
    check_new_agents,
    check_rule_keys,
    gateway_rule_keys,
)
from daegam.services.turn_gates import owner_cedar
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.clusters import Cluster
from daegam.store.gateways import Gateway, GatewayRevision, GatewaySpec, MongoGatewayStore
from daegam.store.hub import AGENT_KIND, PLATFORM_KIND, PUBLISHED_KIND, HubItem

MAX_BACKENDS = 20  # 넘으면 경고 (vMCP 는 세션마다 백엔드 전부에 tools/list 를 보낸다)
# 상태 warnings[] 의 코드. 노출한 Gateway 가 바깥 클라이언트를 허용하지 않은 웹 Tool 에 닿는다
PLATFORM_TOOL_EXPOSED = "platform_tool_exposed"


class GatewayValidator:
    """정의 검증과 허브 그래프의 순환 검사."""

    def __init__(
        self,
        *,
        hub: HubService,
        gateways: MongoGatewayStore,
        agents: MongoAgentStore,
        tool_effects: ToolEffectLoader,
        token_auth: Callable[[], bool],
        error_cls: type[ServiceError],
        auth_mode: Callable[[], str] = lambda: "local",
        rule_scopes: Callable[[], Any] = lambda: None,
        agents_refused: Callable[[], bool] = lambda: False,
        platform_trusted: Callable[[], bool] = lambda: False,
    ):
        self.hub = hub
        self.gateways = gateways
        # 웹 백엔드를 더하는 배포가 핀 없는 Agent 를 찾는다 (P39b 14절 5부)
        self.agents = agents
        # 배포할 스냅샷의 이름을 게시 항목 아래까지 푼다 (`pinned_names_warning`)
        self.tool_effects = tool_effects
        self.token_auth = token_auth
        self.error_cls = error_cls
        # Tool 규칙의 그룹이 모드를 본다 (P52 1절)
        self.auth_mode = auth_mode
        # Gateway 규칙이 Agent 를 이름 댈 수 없는가. 데이터 플레인이 IdP 를 믿는 동안이다 (P53b
        # 6.9절, `Settings.data_plane_trusts_idp`)
        self.agents_refused = agents_refused
        # 규칙 Cedar 의 크기를 재는 테넌트의 사실 (P52 1절). 서비스가 만든 뒤에 단다
        self.rule_scopes = rule_scopes
        # 데이터 플레인이 플랫폼을 믿는가. 그때만 게시된 Gateway 가 토큰 인증의 백엔드다 (P57 6.1절)
        self.platform_trusted = platform_trusted

    async def check_authz_policy(self, spec: GatewaySpec) -> None:
        """authz_policy 의 문장마다 Cedar 로 파싱되는가 (T63, `services/cedar_check.py`). 하나라도
        파싱되지 않으면 422 authz_invalid 이고 path 는 authz_policy 다. 파서가 판정을 내지 못하면
        503 authz_not_ready 이고 저장하지 않는다."""
        if not spec.authz_policy:
            return
        try:
            await parse_statements(spec.authz_policy)
        except CedarSyntaxError as e:
            raise self.error_cls(
                422,
                "authz_invalid",
                f"authz_policy is not valid Cedar: {e}",
                path="authz_policy",
            ) from e
        except CedarCheckUnavailable as e:
            detail = f"authz_policy was not checked: {e}"
            raise self.error_cls(503, "authz_not_ready", detail) from e

    async def validate_spec(
        self,
        tenant: str,
        spec: GatewaySpec,
        *,
        name: str,
        principal: Principal,
        self_id: str | None = None,
        before: GatewaySpec | None = None,
    ) -> tuple[dict[str, HubItem], list[str]]:
        """backends 의 허브 항목을 principal 이 tenant 에서 볼 수 있고(view) 고를 수 있는지
        (consume), 같은 클러스터인지(클러스터 없는 플랫폼 항목은 빼고), tools ⊆ tool_names 인지.
        view 가 없는 항목은 없는 항목과 같은 422 unknown_hub_item 이고 consume 이 없으면 422
        hub_item_not_consumable 이다 (D23). 게시한 Agent 의 항목은 백엔드가 되지 못한다 (422
        agent_item_gateway_unsupported, P46 6.1절). 이미지가 거부된 image 항목은 422
        hub_backend_refused 다 (P55 6절). tool_names 가 비어 있으면 검증을 건너뛰고 경고를
        돌려준다. 토큰 인증의 published 항목은 `check_published` 가 본다 (P57 6.1·6.2절).
        authz_policy 의 문장 하나라도 파싱되지 않으면 422 authz_invalid 다 (T63). (items,
        warnings)"""
        if not spec.backends:
            raise self.error_cls(422, "no_backends", "gateway needs at least one backend")
        await self.check_authz_policy(spec)
        ids = [b.hub_item_id for b in spec.backends]
        if len(set(ids)) != len(ids):
            raise self.error_cls(
                422, "duplicate_backend", "backends must reference distinct hub items"
            )
        items = await self.hub.store.get_many_unscoped(ids)
        # internal·published 의 도구 이름은 저장된 사본이 아니라 원본에서 계산한 것이다 (D49).
        # 이 자리가 원본을 직접 보므로 어긋난 정의는 다음 재배포가 아니라 저장할 때 막힌다
        resolved = await self.hub.resolve_origins(list(items.values()))
        warnings: list[str] = []
        names: dict[str, list[str]] = {}
        # 이름을 모르는 백엔드는 아래에서 한 번만 세어 둔다. 같은 항목의 이름을 두 번 해석하면
        # 두 값이 갈릴 자리가 생긴다
        unknown_items: list[tuple[str, str | None]] = []
        images = any(i.kind == "image" for i in items.values())
        refused = await self.hub.refused_digests() if images else frozenset()
        for b in spec.backends:
            item = items.get(b.hub_item_id)
            if item is None or not can(principal, tenant, item, "view"):
                raise self.error_cls(422, "unknown_hub_item", f"hub item {b.hub_item_id} not found")
            if item.kind == AGENT_KIND:
                # 저장도 배포도 여기를 지난다. Gateway 에서 Agent 로 가는 호출에는 사람이 실리지
                # 않는다
                raise self.error_cls(
                    422,
                    "agent_item_gateway_unsupported",
                    f"{item.name}: an agent item cannot be a gateway backend; attach it as a "
                    "tool on an agent's canvas",
                    hub_item_id=item.id,
                )
            if not can(principal, tenant, item, "consume"):
                raise self.error_cls(
                    422,
                    "hub_item_not_consumable",
                    f"{item.name}: no permission to use this hub item as a backend",
                    hub_item_id=item.id,
                )
            if item.kind == "image" and image_digest(item.source.image) in refused:
                # 거부한 digest 는 새 리비전과 다음 배포·재배포에서 멈춘다 (P55 6절)
                raise self.error_cls(
                    422,
                    "hub_backend_refused",
                    f"{item.name}: a platform-admin refused this hub item's image",
                    hub_item_id=item.id,
                )
            names[item.id] = item_names = tool_names_of(item, resolved)
            if item.kind == PUBLISHED_KIND and self.token_auth():
                await self.check_published(item, b.tools, item_names)
            if not item_names:
                unknown_items.append((item.name, item.kind))
            if b.tools:
                if not item_names:
                    warnings.append(
                        f"{item.name}: tool_names unknown, tools filter not validated "
                        f"({unknown_names_hint([item.kind])})"
                    )
                else:
                    unknown = [t for t in b.tools if t not in item_names]
                    if unknown:
                        raise self.error_cls(
                            422,
                            "unknown_tools",
                            f"{item.name}: tools not offered: {', '.join(unknown)}",
                            unknown=unknown,
                            available=sorted(item_names),
                        )
        # Entry 이름은 `<gateway>-<hub item 이름>` 을 DNS 이름으로 고치고 63자에서 자른 것이다.
        # 이름이 같은 두 항목(테넌트의 `web-fetch` 와 플랫폼의 것)과, 고치거나 자른 뒤에 같아지는 두
        # 항목은 워크로드가 겹친다 (P39b 13절)
        workloads: dict[str, list[str]] = {}
        for b in spec.backends:
            item_name = items[b.hub_item_id].name
            workloads.setdefault(entry_name(name, item_name), []).append(item_name)
        twice = sorted(n for names in workloads.values() if len(names) > 1 for n in names)
        if twice:
            raise self.error_cls(
                422,
                "duplicate_backend_name",
                "backends must have hub item names that make distinct workloads; theirs collide: "
                + ", ".join(twice),
                names=twice,
            )
        if spec.conflict_resolution == "priority" and unknown_items:
            warnings.append(priority_unknown_warning(unknown_items))
        if len(spec.backends) > MAX_BACKENDS:
            warnings.append(
                f"{len(spec.backends)} backends (> {MAX_BACKENDS}): every session fans out "
                "tools/list to each backend; consider splitting the gateway"
            )
        await self.check_rules(tenant, spec, items, names, before=before)
        # 같은 클러스터와 웹 백엔드 규칙 (P39b 13절). 노출은 배포·재배포가 본다
        self.require_same_cluster(items, None)
        await self.check_backends(items, exposed=False, names=names)
        await self.check_cycle(tenant, items, self_id=self_id)
        return items, warnings

    async def check_published(
        self, item: HubItem, selected: list[str], item_names: list[str]
    ) -> None:
        """토큰 인증에서 게시된 Gateway 를 백엔드로 고를 수 있는가 (P57 6.1·6.2절). 데이터 플레인이
        IdP 를 믿으면 그 Gateway 가 IdP 토큰만 받으므로 422 published_oidc_unsupported 다. 플랫폼을
        믿으면 다음을 거절한다. proxy 모드 게시(P54b 가 바꾼다), 원본의 배포 리비전이 소유자 Cedar
        에 forbid 를 든 것(그 원본을 고칠 수 없으므로 원본의 이름을 싣는다), `execute_tool_script`
        를 고른 것, 이름을 모르는 것(고정할 filter 가 없다)."""
        if not self.platform_trusted():
            raise self.error_cls(
                422,
                "published_oidc_unsupported",
                f"{item.name}: while Gateways accept only IdP tokens, a published Gateway cannot "
                "be a backend",
                hub_item_id=item.id,
            )
        if proxied(item):
            raise self.error_cls(
                422,
                "published_proxy_unsupported",
                f"{item.name}: a Gateway published in proxy mode cannot be a backend under token "
                "auth; publish it in entry mode",
                hub_item_id=item.id,
            )
        source = await published_source(self.gateways, item)
        if source is not None and owner_forbids(source):
            raise self.error_cls(
                422,
                "published_owner_forbid",
                f"{item.name}: the published Gateway {source.name} holds a forbid statement in its "
                "Authz policy, which cannot screen people who come through another Gateway",
                hub_item_id=item.id,
                gateway=source.name,
            )
        if CODE_MODE_TOOL in selected:
            raise self.error_cls(
                422,
                "published_script_tool",
                f"{item.name}: a published Gateway's {CODE_MODE_TOOL} cannot be selected, since "
                "the calls inside a script skip this Gateway's Tool rules",
                hub_item_id=item.id,
            )
        if not item_names:
            raise self.error_cls(
                422,
                "published_names_unknown",
                f"{item.name}: the published Gateway's Tool names are unknown, so no filter can "
                "be pinned",
                hub_item_id=item.id,
            )

    # -- code mode (P56 6.2절) -----------------------------------------------------------

    async def definition_sample(
        self,
        sample_id: str,
        tenant: str,
        name: str,
        rev: GatewayRevision,
        items: dict[str, HubItem],
    ) -> Gateway:
        """저장 때 code mode 판정이 읽는 스냅샷. 배포 스냅샷과 같은 행(`backend_snapshot_of`)과
        충돌 계획으로 짓고, 클러스터의 상태(URL, Ready)는 읽지 않는다. 판정은 그것을 보지 않는다."""
        resolved = await self.hub.resolve_origins(list(items.values()))
        snapshots = [
            backend_snapshot_of(
                name,
                b,
                items[b.hub_item_id],
                resolved,
                tenant=tenant,
                error_cls=self.error_cls,
            )
            for b in rev.backends
        ]
        sample = Gateway(
            id=sample_id,
            tenant=tenant,
            name=name,
            spec=spec_of_revision(rev),
            revision=rev.revision,
            revisions=[rev],
            deployed_revision=rev.revision,
            deployed_backends=snapshots,
        )
        sample.deployed_conflict_resolution, sample.tool_overrides, _warnings = plan_conflicts(
            snapshots, rev.conflict_resolution
        )
        return sample

    async def code_mode_verdict(
        self, doc: Gateway, sample: Gateway
    ) -> tuple[CodeModeVerdict, SourceTools]:
        """(판정, 그 판정이 읽은 광고 이름과 효과). sample 은 판정할 스냅샷이고, 노출과 소유자
        Cedar 는 doc 에서 읽는다(배포·재배포는 메모리에 얹은 새 노출을 읽는다). 이 Gateway 가 자기
        렌더로 내는 스크립트는 빼고 판정한다(`without_script`)."""
        tools = without_script((await self.tool_effects.load({sample.id: sample}, {}))[sample.id])
        found = code_mode_verdict(
            tools,
            exposed=doc.exposure.enabled,
            owner_cedar=owner_cedar(doc),
            unknown_backends=[
                b.hub_item_name
                for b in sample.deployed_backends
                if not (b.exposed_names() or b.tools)
            ],
        )
        return found, tools

    async def require_code_mode(
        self, doc: Gateway, sample: Gateway, *, cluster: Cluster | None = None
    ) -> None:
        """code mode 를 켠 리비전을 판정이 거절하면 422 다. sample 은 그 리비전을 배포할(또는
        배포하는) 스냅샷이다. cluster 를 주면(배포·재배포) ToolHive 판도 본다."""
        rev = sample.deployed
        if rev is None or not rev.code_mode:
            return
        found, _tools = await self.code_mode_verdict(doc, sample)
        if not found.ok:
            raise self.error_cls(422, **found.error())
        refusal = toolhive_refusal(cluster) if cluster is not None else None
        if refusal is not None:
            raise self.error_cls(422, **refusal)

    async def check_rules(
        self,
        tenant: str,
        spec: GatewaySpec,
        items: dict[str, HubItem],
        names: dict[str, list[str]],
        *,
        before: GatewaySpec | None,
    ) -> None:
        """Gateway 의 Tool 규칙 (P52 1절). 키는 그 백엔드가 이 Gateway 에 내놓는 원래 이름이다
        (필터가 있으면 그 안, 없으면 항목이 아는 이름 전부). 데이터 플레인이 IdP 를 믿는 oidc
        모드에서는 Agent 를 받지 않는다(`rule_agent_oidc`). 이 저장이 더한 Agent 는 그 테넌트에
        있어야 한다."""
        oidc = self.auth_mode() == "oidc"
        refused = self.agents_refused()
        for b in spec.backends:
            if not b.tool_rules:
                continue
            item = items[b.hub_item_id]
            offered = b.tools or names.get(item.id, [])
            try:
                check_rule_keys(
                    b.tool_rules,
                    offered,
                    code="rule_names_unknown_gateway_tool",
                    hub_item_id=item.id,
                )
                for tool, rule in b.tool_rules.items():
                    if refused and rule.callers.agents:
                        raise ToolRuleError(
                            422,
                            "rule_agent_oidc",
                            f"{tool}: while Gateways accept only IdP tokens a Gateway rule "
                            "cannot name Agents",
                            hub_item_id=item.id,
                        )
                    check_callers(rule.callers, oidc=oidc, where=tool)
            except ToolRuleError as e:
                raise self.error_cls(e.status, e.code, e.detail, **e.extra) from e
        try:
            await check_new_agents(
                self.agents,
                tenant,
                after=gateway_rule_keys(spec).values(),
                before=gateway_rule_keys(before).values() if before is not None else (),
            )
            scopes = self.rule_scopes()
            if scopes is not None and any(b.tool_rules for b in spec.backends):
                # 이름은 가장 긴 접두사(`<workload>_`, 63자)를 붙인 것으로 잰다
                scope = await scopes.scope(tenant)
                padded = {
                    f"{'w' * 63}_{name}": rule
                    for b in spec.backends
                    for name, rule in b.tool_rules.items()
                }
                check_cedar_size(source_forbids(padded, scope, "vmcp"), object="vMCP")
        except ToolRuleError as e:
            raise self.error_cls(e.status, e.code, e.detail, **e.extra) from e

    def require_same_cluster(self, items: dict[str, HubItem], cluster_id: str | None) -> None:
        """`platform_tools.require_same_cluster` 를 이 Gateway 의 항목으로 부른다. 배포·재배포는
        대상 클러스터를 정한 바로 뒤에 부른다."""
        require_same_cluster(items, cluster_id, self.error_cls)

    async def check_backends(
        self,
        items: dict[str, HubItem],
        *,
        exposed: bool,
        names: dict[str, list[str]] | None = None,
        gateway: Gateway | None = None,
    ) -> list[str]:
        """`platform_tools.check_backends` 를 이 Gateway 의 항목과 거기서 닿는 항목(`reachable`)으로
        부른다. names 가 없으면 원본에서 푼다. 배포·재배포는 gateway 를 넘기고 웹 Tool 을 더하는
        규칙의 경고를 받는다(`_check_web_pins`)."""
        if names is None:
            resolved = await self.hub.resolve_origins(list(items.values()))
            names = {i.id: tool_names_of(i, resolved) for i in items.values()}
        reachable = await self.reachable(items)
        await check_backends(
            items,
            exposed=exposed,
            settings=await self.hub.tenancy.platform_settings.get(),
            reachable=reachable,
            names=names,
            error_cls=self.error_cls,
        )
        if gateway is None:
            return []
        return await self._check_web_pins(reachable, gateway)

    async def _check_web_pins(self, reachable: list[HubItem], gateway: Gateway) -> list[str]:
        """웹 Tool 에 새로 닿게 하는 배포·재배포다(P39b 14절 5부). 배포한 백엔드에서 닿던 플랫폼
        항목과 이번 백엔드에서 닿는 것을 견준다. 이 Gateway 와, 그 게시 항목을 백엔드로 배포한
        Gateway 를 위로 따라간 것(`consumers`)을 고른 Tool 없이 쓰는 배포된 Agent 가 핀이 없으면
        409 `web_backend_unpinned_agents`, 이름을 모를 때 핀했으면 409 `web_backend_unknown_pins`
        다. 둘 다 다음 다시 읽기에 웹 Tool 을 가드 없이 받기 때문이다. 앞의 것은 재배포하면 핀이
        생기고, 뒤의 것은 Tool 을 고르거나 Gateway 가 이름을 알게 된 뒤 재배포해야 한다. 모두 핀한
        Agent 는 다음 재배포에 받으므로 경고만 한다."""
        deployed = await self.hub.store.get_many_unscoped(
            [b.hub_item_id for b in gateway.deployed_backends]
        )
        before = {i.id for i in await self.reachable(deployed) if i.kind == PLATFORM_KIND}
        if not {i.id for i in reachable if i.kind == PLATFORM_KIND} - before:
            return []
        consumers = await self.consumers(gateway.id)
        await self._require_consumer_names(consumers - {gateway.id})
        users: dict[str, Agent] = {}
        for gateway_id in sorted(consumers):
            found = await self.agents.revisions_referencing_tool_source(
                "gateway_instance", gateway_id
            )
            users.update((a.id, a) for a in found)
        states = pin_states(users.values(), consumers)
        if states.missing:
            raise self.error_cls(
                409,
                "web_backend_unpinned_agents",
                "deployed agents use this gateway without tool pins; redeploy them first: "
                + ", ".join(states.missing),
                agent_ids=states.missing,
            )
        if states.unknown:
            raise self.error_cls(
                409,
                "web_backend_unknown_pins",
                "deployed agents pinned this gateway's tools while their names were unknown; "
                "select their tools, or deploy the gateway without web tools and redeploy them: "
                + ", ".join(states.unknown),
                agent_ids=states.unknown,
            )
        if not states.pinned:
            return []
        return [
            f"web_backend_pinned_agents: {len(states.pinned)} agent(s) get the web tools at their "
            f"next redeploy: {', '.join(states.pinned)}"
        ]

    async def _require_consumer_names(self, gateway_ids: set[str]) -> None:
        """이 Gateway 의 게시 항목을 배포한 백엔드로 쓰는 Gateway 도 새 웹 Tool 에 닿는다. 그
        Gateway 들도 모든 백엔드의 이름을 배포 스냅샷에 알아야 한다(`platform_tools.check_backends`
        의 422 `web_gateway_unknown_names` 와 같은 규칙). 모르는 백엔드가 있으면 이 배포를
        막는다."""
        unknown: list[str] = []
        for gw in (await self.gateways.get_many(gateway_ids)).values():
            unknown += [
                f"{gw.name}/{b.hub_item_name}"
                for b in gw.deployed_backends
                if not (b.exposed_names() or b.tools)
            ]
        if unknown:
            raise self.error_cls(
                422,
                "web_gateway_unknown_names",
                "gateways that use this gateway's published item would reach its web tools, so "
                "they must know every backend's tool names; refresh those backends and redeploy "
                "those gateways first: " + ", ".join(sorted(unknown)),
                items=sorted(unknown),
            )

    async def pinned_names_warning(self, gateway: Gateway) -> list[str]:
        """배포할 스냅샷의 이름이 이 Gateway 를 고른 Tool 없이 쓰는 Agent 의 핀과 다르면 경고다(P39b
        14절 5부). 그 Agent 는 재배포할 때까지 배포 때 핀한 Tool 을 쓴다. 게시 항목은 원본 Gateway
        까지 따라간다(`ToolEffectLoader`). 이름을 모르면 보지 않는다."""
        names = (await self.tool_effects.load({gateway.id: gateway}, {}))[gateway.id].names
        if names is None:
            return []
        users = await self.agents.revisions_referencing_tool_source("gateway_instance", gateway.id)
        behind = pins_behind(users, gateway.id, names)
        if not behind:
            return []
        return [
            f"{PINNED_AGENTS_KEEP_TOOLS}: {len(behind)} agent(s) keep the tools pinned at their "
            f"deploy until they are redeployed: {', '.join(behind)}"
        ]

    async def exposed_web_warning(self, gateway: Gateway) -> list[str]:
        """노출한 Gateway 가 플랫폼 웹 Tool 에 닿는데 관리자가 바깥 클라이언트를 허용하지 않은
        상태면 상태 경고 `platform_tool_exposed` 다(P39b 13절). 허용을 끈 뒤에도 그 Gateway 는 돌고,
        다음 재배포가 422 다."""
        if not gateway.exposure.enabled:
            return []
        if (await self.hub.tenancy.platform_settings.get()).web_tools.external_clients:
            return []
        items = await self.hub.store.get_many_unscoped(
            [b.hub_item_id for b in gateway.deployed_backends]
        )
        if not any(i.kind == PLATFORM_KIND for i in await self.reachable(items)):
            return []
        return [
            f"{PLATFORM_TOOL_EXPOSED}: the exposed gateway reaches platform web tools while "
            "external clients are not allowed; its next redeploy is refused"
        ]

    async def consumers(self, gateway_id: str) -> set[str]:
        """이 Gateway 와, 그 게시 항목을 배포한 백엔드로 쓰는 Gateway 를 위로 따라간 모든 Gateway
        의 id. 아래 Gateway 의 새 Tool 이 게시를 거쳐 그 Gateway 들에도 생긴다."""
        out = {gateway_id}
        stack = [gateway_id]
        while stack:
            items = await self.hub.store.published_of_instance(stack.pop())
            for gw in await self.gateways.deployed_over([i.id for i in items]):
                if gw.id not in out:
                    out.add(gw.id)
                    stack.append(gw.id)
        return out

    async def _below(self, item: HubItem) -> tuple[Gateway | None, dict[str, HubItem]]:
        """게시 항목 아래의 원본 Gateway 와 그 핀 리비전의 백엔드 항목. 게시가 아니면 (None, {})."""
        gw = await published_source(self.gateways, item)
        if gw is None:
            return None, {}
        rev = gw.deployed or gw.current
        return gw, await self.hub.store.get_many_unscoped([b.hub_item_id for b in rev.backends])

    async def reachable(self, items: dict[str, HubItem]) -> list[HubItem]:
        """items 와, 게시 항목을 원본 Gateway 의 백엔드까지 따라 내려가 닿는 모든 항목."""
        out: dict[str, HubItem] = {}
        stack = list(items.values())
        seen: set[str] = set()
        while stack:
            item = stack.pop()
            out.setdefault(item.id, item)
            gw, below = await self._below(item)
            if gw is None or gw.id in seen:
                continue
            seen.add(gw.id)
            stack.extend(below.values())
        return list(out.values())

    async def check_cycle(
        self, tenant: str, items: dict[str, HubItem], *, self_id: str | None = None
    ) -> None:
        """허브 그래프 DFS. published 항목에서 원본 Gateway 로, 다시 그 핀 리비전의 백엔드로 따라
        내려가다 자기 자신에 닿으면 422 cycle 이다. 같은 그룹 안의 자기 참조는 vMCP 를 무한
        재귀시킨다 (실측). 순환은 보이지 않는 항목도 따라가야 하므로 판정 없는 조회를 쓴다."""
        stack: list[tuple[HubItem, list[str]]] = [(i, [i.name]) for i in items.values()]
        seen: set[str] = set()
        while stack:
            item, path = stack.pop()
            gw, nxt = await self._below(item)
            if gw is None:
                continue
            if self_id and gw.id == self_id:
                raise self.error_cls(
                    422,
                    "cycle",
                    "backend chain reaches this gateway itself: " + " -> ".join(path + [gw.name]),
                    path=path + [gw.name],
                )
            if gw.id in seen:
                continue
            seen.add(gw.id)
            for n in nxt.values():
                stack.append((n, path + [gw.name, n.name]))
