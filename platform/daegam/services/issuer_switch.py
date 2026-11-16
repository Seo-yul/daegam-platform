"""데이터 플레인 발급자 전환의 상태와 준비 확인 (P53b 6.2·6.9·6.10절).

oidc 모드에서 `DAEGAM_DATA_PLANE_ISSUER` 를 platform 으로 바꾸면 다음 재적용 틱이 모든
네임스페이스의 daegam-oidc 를 플랫폼으로 바꾸고, ToolHive 오퍼레이터가 그것을 참조하는 vMCP 와 노출
앞단을 굴리며, 소비자 문서가 재적용의 속도로 다시 렌더된다(`services/data_plane_auth.py`
`switch_rank`). 이 모듈은 그 진행을 세고, 전환을 막을 것을 기동에서 본다.

- **기동의 막음**(`held_settings`). 저장된 mcp_group 이 다른 테넌트의 고정 그룹이나 그 밖의
  `daegam-` 이름인 테넌트가 있으면 platform 을 청해도 데이터 플레인은 idp 로 남는다. 옛 Cedar 가
  그 이름을 싣는 동안 그 이름의 플랫폼 토큰이 이 테넌트의 Gateway 에 들어오기 때문이다. 상태가 그
  테넌트들을 늘어놓고, 관리자가 고친 뒤 플랫폼을 다시 띄우면 바뀐다.
- **준비 확인**(리컨실 단계 `probe`). 데이터 플레인이 플랫폼을 믿는 동안 틱마다 daegam-oidc 를 쓰는
  vMCP 와 노출 앞단(`jwks_heal.platform_verifiers`)과, 두 발급자를 알린 이미지의 노출
  Agent(`/metrics`, 외부 호출자는 인증 뒤 403 이다)를 플랫폼 토큰
  (`SvcTokens.platform_gateway_token`)으로 불러 본다. 401 이 아니면 그 검증기는 확인된 것이고, 그
  파드들이 그대로인 동안 다시 부르지 않는다. 파드가 바뀌면(다시 뜸, 굴림) 다시 부른다. 401 이거나
  아직 주소가 없으면 기다리는 것으로 남는다. 플랫폼 JWKS 를 받지 못하는 틱은 부르지
  않는다(`jwks_heal.jwks_served`). 결과는 `auth_reapply` 컬렉션의 문서 `issuer_switch` 하나에
  클러스터마다 적는다. IdP 를 믿으면 그 문서를 비우므로 다시 platform 으로 바꾸면 처음부터
  확인한다.
- **상태**(`status`, `GET /api/settings/data-plane-auth` 의 `data_plane_issuer`). oidc 모드에서만
  있다. 설정과 실제로 믿는 발급자, 막은 까닭, 클러스터마다 daegam-oidc 가 플랫폼을 이름 대는
  네임스페이스와 아직 IdP 인 것, 다시 렌더를 기다리는 문서(막힌 것 포함), 확인된 검증기와 아직
  기다리는 검증기, `pending`(기다리는 문서와 검증기와 아직 확인하지 못한 클러스터의 수)이다.
  그리고 IdP 토큰의 가장 긴 수명과 대화형 한도, 플랫폼 JWKS 에 닿지 못할 원격 클러스터, 전환이
  바꾸거나 열어 두는 것(6.9·6.10절)이다. 마지막 목록은 전환 전에 읽으라고 두 값에서 모두 낸다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from daegam.auth.cedar import (
    CLIENT_ID_CLAIM,
    claim_values,
    holds_permit,
    named_groups,
    policy_code,
    reads_claim,
)
from daegam.auth.rbac import IDP_GROUP_PREFIX, PLATFORM_GROUP_HEAD, ROLE_GROUP_PREFIX
from daegam.config import (
    DATA_PLANE_ISSUER_IDP,
    DATA_PLANE_ISSUER_PLATFORM,
    DEFAULT_GROUPS_CLAIM,
    Settings,
)
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import (
    LABEL_APP_INSTANCE,
    LABEL_APP_NAME,
    LABEL_ID,
    LABEL_PLATFORM_ID,
    LABEL_RESOURCE,
    stable_hash,
)
from daegam.k8s.templates.agent import AGENT_PORT
from daegam.k8s.templates.agent import RESOURCE as AGENT_RESOURCE
from daegam.k8s.templates.common import OIDC_CONFIG_NAME, TOOLHIVE_API
from daegam.services import agent_graph
from daegam.services.agent_features import TWO_ISSUERS
from daegam.services.claims import tick_item
from daegam.services.data_plane_auth import EXPOSED_AGENT_STATES, Behind
from daegam.services.jwks_heal import (
    PROBE_CONCURRENCY,
    Verifier,
    jwks_served,
    platform_verifiers,
    probe_verifier,
)
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import RESOURCE_OIDC_CONFIG, TenancyService
from daegam.services.turn_gates import doc_row, owner_authz
from daegam.store.auth_reapply import KIND_AGENT, KIND_GATEWAY
from daegam.store.base import UtcDatetime
from daegam.store.clusters import Cluster
from daegam.store.exposure import exposure_enabled
from daegam.store.hub import PUBLISHED_KIND
from daegam.store.idp_people import MongoIdpPeopleStore
from daegam.store.tenants import MongoTenantSettingsStore, default_mcp_group

logger = logging.getLogger("daegam.issuer_switch")

# `auth_reapply` 컬렉션에서 이 상태가 쓰는 문서 id
STATE_ID = "issuer_switch"
# IdP 토큰 수명을 보는 사람 기록의 창 (6.3절, 지난 하루에 확인된 기록)
TOKEN_LIFE_WINDOW = timedelta(days=1)
# 노출 Agent 를 불러 보는 경로. 외부 호출자는 인증을 지난 뒤 403 이고 아무것도 읽지 않는다
AGENT_PROBE_PATH = "/metrics"
AGENT_KIND = "Agent"
# 전환을 막은 까닭 (`held_settings`)
HELD_FOREIGN_MCP_GROUP = "foreign_mcp_group"


# -- 기동의 막음 --------------------------------------------------------------------------------


async def held_settings(settings: Settings, tenants: MongoTenantSettingsStore) -> Settings:
    """platform 을 청한 oidc 배포에서, 저장된 mcp_group 이 다른 테넌트의 고정 그룹이나 그 밖의
    `daegam-` 이름인 테넌트가 있으면 데이터 플레인을 idp 로 남긴 설정을 준다 (P53b 6.10절). 플랫폼이
    서비스를 짓기 전에 한 번 부른다. 그 밖에는 settings 그대로다."""
    if not (settings.idp_login and settings.data_plane_issuer == DATA_PLANE_ISSUER_PLATFORM):
        return settings
    held = await tenants.foreign_mcp_groups()
    if not held:
        return settings
    logger.warning(
        "DAEGAM_DATA_PLANE_ISSUER=platform is held at idp: the stored mcp_group of %s names "
        "another tenant's fixed group or another daegam- name; clear it in Tenant Settings and "
        "restart the platform",
        ", ".join(f"{t} ({g})" for t, g in sorted(held.items())),
    )
    return replace(settings, data_plane_issuer_held_by=tuple(sorted(held)))


# -- 정책 글 (순수) ---------------------------------------------------------------------------


def owner_policies(doc: Any) -> list[str]:
    """Gateway 의 소유자가 쓴 Cedar. 배포한 리비전의 authz_policy 와, Advanced 편집에 저장된
    incomingAuth 의 인라인 정책이다."""
    rev = getattr(doc, "deployed", None)
    out = [rev.authz_policy] if rev is not None and rev.authz_policy else []
    authz = owner_authz(doc)
    inline = authz.get("inline") if isinstance(authz, dict) else None
    policies = inline.get("policies") if isinstance(inline, dict) else None
    if isinstance(policies, list):
        out.extend(p for p in policies if isinstance(p, str))
    return out


def code_names_idp(code: Sequence[str], groups_claim: str, client_id_claim: str) -> bool:
    """`cedar.policy_code` 의 문장이 IdP 그룹이나 IdP 의 클레임으로 호출자를 가리는가. 플랫폼 토큰은
    IdP 그룹을 테넌트 이름을 붙인 꼴로만 `groups` 에 싣고 client id 와 IdP 의 그룹 클레임은 싣지
    않으므로, 그런 조건은 전환 뒤 맞지 않는다 (6.6절). THVGroup 이름과 `groups` 클레임과 견주는
    글자 가운데 `daegam-` 이 아닌 것, client id 클레임(설정한 것과 cid)의 읽기, groups 가 아닌 IdP
    그룹 클레임의 읽기를 본다."""
    groups = named_groups(code) | claim_values(code, DEFAULT_GROUPS_CLAIM)
    if any(not g.startswith(PLATFORM_GROUP_HEAD) for g in groups):
        return True
    claims = {client_id_claim, CLIENT_ID_CLAIM}
    if groups_claim != DEFAULT_GROUPS_CLAIM:
        claims.add(groups_claim)
    return reads_claim(code, claims)


def names_idp(doc: Any, groups_claim: str, client_id_claim: str) -> bool:
    """Gateway 의 소유자 Cedar 가 IdP 의 이름으로 호출자를 가리는가 (`idp_names_in_policy`)."""
    return code_names_idp(policy_code(owner_policies(doc)), groups_claim, client_id_claim)


def foreign_platform_groups(tenant: str, groups: Iterable[str]) -> list[str]:
    """allowed_groups 가운데 다른 테넌트의 플랫폼 그룹(고정 그룹, `daegam-group:`·`daegam-role:`
    꼴). 그 테넌트의 플랫폼 토큰이 이 노출 Agent 에 들어온다 (6.10절)."""
    fixed = default_mcp_group(tenant)
    qualified = (f"{IDP_GROUP_PREFIX}{tenant}:", f"{ROLE_GROUP_PREFIX}{tenant}:")
    return [
        g
        for g in groups
        if g.startswith(PLATFORM_GROUP_HEAD) and g != fixed and not g.startswith(qualified)
    ]


def _row(kind: str, doc: Any) -> dict[str, Any]:
    return {"kind": kind, **doc_row(doc)}


# -- 확인의 기록 --------------------------------------------------------------------------------


class Waiting(BaseModel):
    """아직 플랫폼 토큰을 받지 못한 검증기 하나. refused 는 마지막으로 불렀을 때 401 이었는가이고,
    거짓이면 아직 부르지 못했다(주소가 없거나 파드가 뜨는 중)."""

    kind: str
    name: str
    namespace: str
    tenant: str
    since: UtcDatetime
    refused: bool = False
    last_at: UtcDatetime | None = None


class Verified(BaseModel):
    """플랫폼 토큰을 받은 검증기 하나. pods 는 그때 그 검증기의 파드들(uid)의 해시이고, 바뀌면 다시
    부른다."""

    kind: str
    name: str
    namespace: str
    at: UtcDatetime
    pods: str


class ClusterSwitch(BaseModel):
    """클러스터 하나의 확인. passed 는 확인된 검증기의 key → 그 기록이다."""

    passed: dict[str, Verified] = Field(default_factory=dict)
    waiting: dict[str, Waiting] = Field(default_factory=dict)
    platform_namespaces: list[str] = Field(default_factory=list)
    idp_namespaces: list[str] = Field(default_factory=list)
    probed_at: UtcDatetime | None = None
    error: str | None = None


class SwitchState(BaseModel):
    """마지막 확인. issuer 는 그 확인을 한 때의 설정이다."""

    issuer: str = DATA_PLANE_ISSUER_IDP
    clusters: dict[str, ClusterSwitch] = Field(default_factory=dict)


class MongoIssuerSwitchStore:
    """`auth_reapply` 컬렉션의 문서 하나. 그 컬렉션의 다른 문서(클러스터 상태, 요약, 게이트)와 id 가
    겹치지 않고, 앞 릴리스는 이 id 를 읽지 않는다."""

    def __init__(self, coll: Any):
        self.coll = coll

    async def get(self) -> SwitchState:
        doc = await self.coll.find_one({"_id": STATE_ID})
        if doc is None:
            return SwitchState()
        doc.pop("_id", None)
        return SwitchState.model_validate(doc)

    async def put(self, state: SwitchState) -> None:
        await self.coll.replace_one({"_id": STATE_ID}, state.model_dump(), upsert=True)


def _pod_hashes(client: KubeClient, kinds: Iterable[str]) -> dict[tuple[str, str, str], str]:
    """(종류, 네임스페이스, 이름) → 지금 그 검증기의 지지 않은 파드들(uid)의 해시. ToolHive 검증기는
    `app.kubernetes.io/name`(종류의 소문자)과 `app.kubernetes.io/instance`(CR 이름)로, 노출 Agent 는
    리소스 라벨과 문서 id 로 찾는다. Agent 의 이름 자리는 문서 id 다. threadpool 에서 돈다."""
    uids: dict[tuple[str, str, str], list[str]] = {}
    lists = [
        (kind, LABEL_APP_INSTANCE, client.list("v1", "Pod", labels={LABEL_APP_NAME: kind.lower()}))
        for kind in kinds
        if kind != AGENT_KIND
    ]
    lists.append(
        (AGENT_KIND, LABEL_ID, client.list("v1", "Pod", labels={LABEL_RESOURCE: AGENT_RESOURCE}))
    )
    for kind, name_label, pods in lists:
        for pod in pods:
            meta = pod.get("metadata") or {}
            if meta.get("deletionTimestamp"):
                continue
            labels = meta.get("labels") or {}
            key = (kind, str(meta.get("namespace")), str(labels.get(name_label)))
            uids.setdefault(key, []).append(str(meta.get("uid")))
    return {key: stable_hash(sorted(found), 16) for key, found in uids.items()}


# -- 리컨실 단계와 상태 -------------------------------------------------------------------------


class IssuerSwitch:
    """발급자 전환의 준비 확인(리컨실 단계)과 상태."""

    def __init__(
        self,
        tenancy: TenancyService,
        svc_tokens: SvcTokens,
        settings: Settings,
        store: MongoIssuerSwitchStore,
        *,
        agents: Any,
        people: MongoIdpPeopleStore | None = None,
    ) -> None:
        self.tenancy = tenancy
        self.svc_tokens = svc_tokens
        self.settings = settings
        self.store = store
        # 노출 Agent 를 클러스터마다 읽는다(`live_on_cluster`)
        self.agents = agents
        self.people = people

    async def probe(self, now: datetime) -> dict[str, list[str]]:
        """리컨실 단계. 플랫폼을 믿는 oidc 모드에서 확인되지 않았거나 파드가 바뀐 검증기를 불러
        본다. IdP 를 믿으면 기록을 비운다. 결과는 새로 확인된 것(`passed`)과 거절한 것(`refused`)
        이고, 플랫폼 JWKS 를 받지 못한 틱은 `skipped` 가 `jwks` 하나다."""
        settings = self.settings
        if not settings.idp_login:
            return {}
        state = await self.store.get()
        if not settings.oidc_trusts_platform:
            if state.issuer != DATA_PLANE_ISSUER_IDP or state.clusters:
                await self.store.put(SwitchState())
            return {}
        if state.issuer != DATA_PLANE_ISSUER_PLATFORM:
            state = SwitchState(issuer=DATA_PLANE_ISSUER_PLATFORM)
        if not self.svc_tokens.available:
            return {}
        jwks = settings.human_jwks_url
        if jwks is None or not await jwks_served(jwks):
            return {"passed": [], "refused": [], "skipped": ["jwks"]}
        install_id = await self.tenancy.platform_settings.install_id()
        clusters = await self.tenancy.clusters.list()
        passed: list[str] = []
        refused: list[str] = []
        for cluster in clusters:
            await tick_item()
            current = state.clusters.setdefault(cluster.id, ClusterSwitch())
            try:
                client = await run_in_threadpool(self.tenancy.cluster_service.client_for, cluster)
                found = await self._verifiers(client, cluster, install_id)
                kinds = {v.kind for v in found}
                pods = await run_in_threadpool(_pod_hashes, client, kinds)
                configs = await run_in_threadpool(_oidc_configs, client, install_id)
            except Exception as e:  # noqa: BLE001 - 한 클러스터가 다른 클러스터를 막지 않는다
                current.error = str(e) or type(e).__name__
                continue
            await self._probe_cluster(client, cluster, current, found, pods, now, passed, refused)
            issuer = settings.data_plane_issuer_url
            current.platform_namespaces = sorted(ns for ns, i in configs.items() if i == issuer)
            current.idp_namespaces = sorted(ns for ns, i in configs.items() if i != issuer)
            current.probed_at, current.error = now, None
        known = {c.id for c in clusters}
        state.clusters = {k: v for k, v in state.clusters.items() if k in known}
        await self.store.put(state)
        return {"passed": passed, "refused": refused}

    async def _verifiers(
        self, client: KubeClient, cluster: Cluster, install_id: str
    ) -> list[Verifier]:
        """daegam-oidc 를 쓰는 vMCP 와 노출 앞단(주소가 아직 없는 것까지), 두 발급자를 알린 이미지의
        노출 Agent 다. Agent 의 이름 자리는 k8s 이름이고 key 는 문서 id 로 끝난다."""
        names = frozenset({OIDC_CONFIG_NAME})
        found = await run_in_threadpool(
            lambda: platform_verifiers(client, install_id, names, with_unaddressed=True)
        )
        for doc in await self.agents.live_on_cluster(cluster.id):
            if not (exposure_enabled(doc) and doc.state in EXPOSED_AGENT_STATES):
                continue
            if doc.k8s is None or not doc.image_reports(TWO_ISSUERS):
                continue
            ns, name = doc.k8s.namespace, doc.k8s.name
            found.append(
                Verifier(
                    kind=AGENT_KIND,
                    name=name,
                    namespace=ns,
                    tenant=doc.tenant,
                    key=f"{AGENT_KIND}/{ns}/{name}/{doc.id}",
                    url=f"http://{name}.{ns}.svc.cluster.local:{AGENT_PORT}{AGENT_PROBE_PATH}",
                    replicas=1,
                )
            )
        return found

    async def _probe_cluster(
        self,
        client: KubeClient,
        cluster: Cluster,
        current: ClusterSwitch,
        found: list[Verifier],
        pods: Mapping[tuple[str, str, str], str],
        now: datetime,
        passed: list[str],
        refused: list[str],
    ) -> None:
        """클러스터 하나의 검증기 가운데 확인되지 않았거나 파드가 바뀐 것을 부르고 기록을 고친다.
        주소가 없는 것은 부르지 않고 기다리는 것으로 둔다. 사라진 검증기의 기록은 지운다."""
        keys = {v.key for v in found}
        current.passed = {k: p for k, p in current.passed.items() if k in keys}
        current.waiting = {k: w for k, w in current.waiting.items() if k in keys}

        def pods_of(v: Verifier) -> str:
            name = v.key.rsplit("/", 1)[-1] if v.kind == AGENT_KIND else v.name
            return pods.get((v.kind, v.namespace, name), "")

        due: list[Verifier] = []
        for v in found:
            seen = current.passed.get(v.key)
            if seen is not None and seen.pods == pods_of(v):
                continue
            current.passed.pop(v.key, None)
            if v.url:
                due.append(v)
            else:
                current.waiting.setdefault(v.key, self._waiting(v, now))
        gate = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(v: Verifier) -> tuple[int, str] | None:
            async with gate:
                await tick_item()
                token = await self.svc_tokens.platform_gateway_token(v.tenant)
                return await probe_verifier(client, str(v.url), token)

        answers = await asyncio.gather(*(one(v) for v in due))
        for v, answer in zip(due, answers, strict=True):
            label = f"{cluster.name}/{v.namespace}/{v.kind}/{v.name}"
            if answer is not None and answer[0] != 401:
                current.passed[v.key] = Verified(
                    kind=v.kind, name=v.name, namespace=v.namespace, at=now, pods=pods_of(v)
                )
                current.waiting.pop(v.key, None)
                passed.append(label)
                continue
            row = current.waiting.get(v.key) or self._waiting(v, now)
            if answer is not None:
                row.refused, row.last_at = True, now
                refused.append(label)
            current.waiting[v.key] = row

    @staticmethod
    def _waiting(v: Verifier, now: datetime) -> Waiting:
        return Waiting(kind=v.kind, name=v.name, namespace=v.namespace, tenant=v.tenant, since=now)

    async def status(
        self,
        clusters: Sequence[Cluster],
        docs: Mapping[str, Sequence[tuple[str, Any]]],
        behind: Mapping[str, Sequence[Behind]],
        now: datetime,
    ) -> dict[str, Any] | None:
        """`data_plane_issuer` 의 본문. oidc 모드가 아니면 None 이다. docs 는 클러스터마다 살아 있는
        문서의 (종류, 문서)이고, behind 는 그 가운데 판이 달라 다시 렌더를 기다리는 소비자다. 요청
        오류로 막힌 것도 든다. 옛 렌더로 도는 동안은 전환이 끝나지 않았기 때문이다."""
        settings = self.settings
        if not settings.idp_login:
            return None
        trusted = settings.oidc_trusts_platform
        state = await self.store.get() if trusted else SwitchState()
        probed = state.issuer == DATA_PLANE_ISSUER_PLATFORM
        rows: list[dict[str, Any]] = []
        pending = 0
        for cluster in clusters if trusted else ():
            current = state.clusters.get(cluster.id) if probed else None
            waiting = (
                [w.model_dump(mode="json") for w in current.waiting.values()] if current else []
            )
            verified = (
                [
                    p.model_dump(mode="json", exclude={"pods"})
                    for p in sorted(current.passed.values(), key=lambda p: p.at)
                ]
                if current
                else []
            )
            pending_docs = [
                {**_row(b.kind, b.doc), "blocked": b.blocked} for b in behind.get(cluster.id, ())
            ]
            unprobed = current is None or current.probed_at is None
            pending += len(pending_docs) + len(waiting) + int(unprobed)
            rows.append(
                {
                    "cluster_id": cluster.id,
                    "probed_at": current.probed_at if current else None,
                    "error": current.error if current else None,
                    "platform_namespaces": current.platform_namespaces if current else [],
                    "idp_namespaces": current.idp_namespaces if current else [],
                    "pending_documents": pending_docs,
                    "waiting_verifiers": waiting,
                    "verified_verifiers": verified,
                }
            )
        every = [(kind, d) for cluster_docs in docs.values() for kind, d in cluster_docs]
        foreign = await self.tenancy.tenant_settings.foreign_mcp_groups()
        return {
            "setting": settings.data_plane_issuer,
            "trusted": DATA_PLANE_ISSUER_PLATFORM if trusted else DATA_PLANE_ISSUER_IDP,
            "error": HELD_FOREIGN_MCP_GROUP if settings.data_plane_issuer_held_by else None,
            "pending": pending,
            "clusters": rows,
            "idp_token_life": await self._token_life(now),
            "jwks_unreachable_clusters": [
                c.id for c in clusters if settings.cluster_local_platform_issuer and not c.is_home
            ],
            "tenants_foreign_mcp_group": [
                {"tenant": t, "mcp_group": g} for t, g in sorted(foreign.items())
            ],
            **await self._opening(every),
        }

    async def _token_life(self, now: datetime) -> dict[str, Any]:
        """지난 하루에 확인된 사람 기록의 가장 긴 IdP 토큰 수명과 그것이 넘지 말아야 할 값(대화형
        한도에서 위임 토큰 한 수명을 뺀 것, 6.3절)이다. 넘으면 warn 이다."""
        largest = None
        if self.people is not None:
            largest = await self.people.largest_token_life(now - TOKEN_LIFE_WINDOW)
        limit = self.settings.turn_owner_confirm_seconds - self.settings.turn_token_ttl_seconds
        return {
            "largest_seconds": largest,
            "limit_seconds": limit,
            "warn": largest is not None and largest >= limit,
        }

    async def _opening(self, docs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        """전환이 바꾸거나 열어 두는 것 (6.9·6.10절)."""
        settings = self.settings
        idp_names: list[dict[str, Any]] = []
        permits: list[dict[str, Any]] = []
        foreign: list[dict[str, Any]] = []
        open_agents: list[dict[str, Any]] = []
        one_issuer: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for kind, doc in docs:
            if kind == KIND_GATEWAY and doc.deployed is not None:
                # IdP 를 믿는 동안 게시된 백엔드는 이 Gateway 를 거절한다(401). 플랫폼을 믿을 때
                # 배포한 것이 남았다 (P57 6.1절)
                if not settings.oidc_trusts_platform and any(
                    b.kind == PUBLISHED_KIND for b in doc.deployed_backends
                ):
                    refused.append(_row(kind, doc))
                code = policy_code(owner_policies(doc))
                claims = (settings.oidc_groups_claim, settings.oidc_client_id_claim)
                if code_names_idp(code, *claims):
                    idp_names.append(_row(kind, doc))
                if holds_permit(code):
                    permits.append(_row(kind, doc))
            if kind != KIND_AGENT:
                continue
            if doc.deployed is not None and agent_graph.uses_gateway_source(doc.deployed):
                sources.append(_row(kind, doc))
            if not (exposure_enabled(doc) and doc.state in EXPOSED_AGENT_STATES):
                continue
            groups = doc.exposure.allowed_groups
            if not groups:
                open_agents.append(_row(kind, doc))
            elif named := foreign_platform_groups(doc.tenant, groups):
                foreign.append({**_row(kind, doc), "groups": named})
            if not doc.image_reports(TWO_ISSUERS):
                one_issuer.append(_row(kind, doc))
        return {
            "gateways_idp_names": idp_names,
            "gateways_permit": permits,
            "exposed_agents_foreign_groups": foreign,
            "exposed_agents_open": open_agents,
            "exposed_agents_one_issuer": one_issuer,
            "tenants_agent_client_ids": await self.tenancy.tenant_settings.with_agent_client_ids(),
            "agents_gateway_sources": sources,
            "gateways_published_refused": refused,
        }


def _oidc_configs(client: KubeClient, install_id: str) -> dict[str, str | None]:
    """이 설치의 네임스페이스마다 사람 토큰용 MCPOIDCConfig(daegam-oidc)가 이름 대는 발급자."""
    out: dict[str, str | None] = {}
    labels = {LABEL_RESOURCE: RESOURCE_OIDC_CONFIG}
    for obj in client.list(TOOLHIVE_API, "MCPOIDCConfig", labels=labels):
        meta = obj.get("metadata") or {}
        if meta.get("name") != OIDC_CONFIG_NAME:
            continue
        if (meta.get("labels") or {}).get(LABEL_PLATFORM_ID) not in (install_id, None):
            continue
        inline = ((obj.get("spec") or {}).get("inline")) or {}
        out[str(meta.get("namespace") or "")] = inline.get("issuer")
    return out
