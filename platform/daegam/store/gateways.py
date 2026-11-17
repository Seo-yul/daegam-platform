"""Gateway (D42). 허브 항목을 골라 만든 툴 조합 엔드포인트의 정의와 vMCP 배포가 한 문서에 산다.

정의 쪽은 `spec`·`revision`·`revisions` 다. 고칠 때마다 리비전이 늘고 이전 리비전은 `revisions[]` 에
불변 스냅샷으로 남는다. 배포 쪽은 `state`·`deployed_revision`·`cluster_id`·`k8s`·
`deployed_backends`·`deployed_conflict_resolution`·`tool_overrides`·`effective_config`·`overrides`
다. `draft` 에서는 배포 쪽이 비어 있고 `exposure` 만 늘 있다. 배포 설정이자 복제가 받는 값이기
때문이다.

배포 쪽 스냅샷은 정의 쪽과 이름이 겹치므로 `deployed_` 를 앞에 붙인다. `spec.backends` 는 사용자가
고른 허브 항목이고 `deployed_backends` 는 배포 시점에 해석한 Entry·URL 이다.
`spec.conflict_resolution` 은 `auto` 일 수 있고 `deployed_conflict_resolution` 은 스냅샷에서 계산한
실제 모드다.

옛 인스턴스 스토어의 프로토콜(`live`·`live_on_cluster`·`live_by_k8s_name`·`list`·`get`)을 지키고,
그 메서드는 `draft` 를 뺀다. `draft` 는 `k8s`·`cluster_id` 가 없어 그 자리에 나오면 `hosts_in_use`
같은 소비자가 터진다. 화면 목록만 `include_draft=True` 로 전부 본다.

허브 `source.gateway_instance_id` 와 Agent 그래프의 소스 id 는 문서의 `_id` 다. `k8s.name` 은
`gw-<name>` 이다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING

from daegam.store.auth_reapply import AUTH_FIELDS, ConsumerAuthState, DeployRender
from daegam.store.base import (
    Document,
    TimeoutSeconds,
    UtcDatetime,
    new_id,
    utcnow,
)
from daegam.store.exposure import Exposure
from daegam.store.instances import DEPLOYED_ONLY, NOT_DRAFT, MongoInstanceStore
from daegam.store.tool_rules import GatewayToolRule, ToolCallers

GATEWAYS = "gateways"

GatewayState = Literal["draft", "deploying", "deployed", "failed", "undeploying", "deleting"]
# vMCP 가 답하는 상태. 재배포 중(`deploying`)에도 옛 파드가 답한다
SERVING_STATES: frozenset[str] = frozenset({"deployed", "deploying"})
# 배포 스냅샷의 Entry 이름을 쥐는 상태. 실패한 문서는 적용한 이름만 선점으로 쥔다 (D82)
ENTRY_HOLDING_STATES: frozenset[str] = frozenset(
    {"deploying", "deployed", "undeploying", "deleting"}
)
ConflictResolution = Literal["auto", "priority", "prefix"]
FailureMode = Literal["fail", "best_effort"]


class GatewayBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hub_item_id: str
    tools: list[str] = Field(default_factory=list)  # 비어 있으면 전체
    # vMCP 가 이 백엔드로 가는 호출을 끊는 시간(초) (P50 7절). 없으면 배포가 기본값을 정한다
    # (`services/gateway_timeouts.py` `default_timeout`)
    timeout_seconds: TimeoutSeconds | None = None
    # 이 백엔드가 광고하는 원래 이름마다의 Tool 규칙 (P52 1절). 키는 (workload, 원래 이름) 의 뒤
    # 절반이고 workload 는 이 백엔드다. 비어 있으면 저장하지 않는다
    tool_rules: dict[str, GatewayToolRule] = Field(default_factory=dict, exclude_if=lambda v: not v)


class GatewayRuntime(BaseModel):
    """vMCP replicas 와 리소스 요청/제한 (설정 계층 4단계)."""

    model_config = ConfigDict(extra="forbid")

    replicas: int = Field(default=1, ge=1, le=10)
    resources: dict[str, Any] | None = None


class GatewaySurface(BaseModel):
    """vMCP 가 백엔드의 resources·prompts 를 내보이는가 (P50 13절). 끈 쪽은 Cedar forbid 로 막아
    목록도 비고 읽기도 거부된다. vMCP 의 인가기는 Gateway 하나에 하나라 백엔드마다가 아니다."""

    model_config = ConfigDict(extra="forbid")

    resources: bool = False
    prompts: bool = False


class GatewaySpec(BaseModel):
    """리비전마다 스냅샷되는 부분."""

    model_config = ConfigDict(extra="ignore")

    backends: list[GatewayBackend] = Field(default_factory=list)
    # auto: 모든 백엔드의 tool_names 를 알면 priority, 하나라도 모르면 prefix. 명시하면 그대로
    conflict_resolution: ConflictResolution = "auto"
    failure_mode: FailureMode = "best_effort"
    # Cedar 정책 한 줄 오버라이드 (테넌트 기본 정책 목록에 추가된다)
    authz_policy: str | None = None
    runtime: GatewayRuntime = Field(default_factory=GatewayRuntime)
    # 새 Gateway 는 둘 다 끈 채로 시작한다(`api/gateways.py` `_spec`)
    surface: GatewaySurface = Field(default_factory=GatewaySurface)
    # ToolHive 의 code mode (P56 6.2절). 광고하는 Tool 이 모두 읽기일 때만 켠다
    # (`services/gateway_code_mode.py`). 이 칸을 모르는 앞 판은 읽을 때 버려 code mode 를 렌더하지
    # 않는다
    code_mode: bool = False


class GatewayRevision(GatewaySpec):
    revision: int
    created_at: UtcDatetime = Field(default_factory=utcnow)


class GatewayK8s(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str  # VirtualMCPServer 이름. Service 는 vmcp-<name>:4483
    namespace: str  # 배포 네임스페이스
    group: str  # 이 Gateway 전용 MCPGroup (= name)


class GatewayBackendSnapshot(BaseModel):
    """배포 시점에 해석한 백엔드 래퍼. Entry 이름(workload)과 remoteUrl 을 고정한다. 이 판이 모르는
    칸(2.0.0-rc1 이 적은 `timeout_moved` 등)은 읽을 때 버린다."""

    model_config = ConfigDict(extra="ignore")

    hub_item_id: str
    hub_item_name: str
    kind: str
    workload: str  # MCPServerEntry 이름 `<gateway>-<item>` = vMCP workload
    url: str
    transport: str = "streamable-http"
    tools: list[str] = Field(default_factory=list)  # 필터 (비면 전체)
    tool_names: list[str] = Field(default_factory=list)  # 항목이 아는 툴 (충돌 계산용)
    # scope=shared 인 다른 테넌트 항목이면: 원본 id 와 이 테넌트 네임스페이스의 복제본
    shared_source_id: str | None = None
    clone_kind: str | None = None  # MCPRemoteProxy | MCPServer
    # 복제본 CR 이름 (`services/hub.py` `shared_clone_name`). url 은 복제본 프록시를 가리킨다
    clone_name: str | None = None
    # 배포 때 정한 호출 타임아웃(초) (P50 7절). 비어 있으면 렌더가 고정값을 쓴다
    # (`services/gateway_timeouts.py` `snapshot_timeout`)
    timeout_seconds: int | None = Field(default=None, ge=1)
    # 서비스 토큰을 받는 백엔드(internal·image)가 부르는 우리 서버의 id (P50 9절). internal 이면
    # MCP Server 문서, 이 테넌트의 image 면 그 항목, 다른 테넌트의 image 면 공유 복제본 기록이다.
    # 그 Entry 의 토큰이 이 서버의 audience 를 싣는다. 비어 있으면 배포 때 서버를 찾지 못한 것이고,
    # 다시 렌더하기가 지금 서버로 채운다(`GatewayTokens.resolve_servers`)
    server_id: str | None = None
    # 그 서버(internal·image)의 Tool 규칙 지문 (P52 2절). Entry 토큰이 `rules` 로 싣고 토큰 객체
    # 이름의 꼬리가 된다. 규칙이 없는 서버는 None 이고 저장하지 않는다
    rules_digest: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 게시된 Gateway(published) 백엔드가 묶인 토큰을 받으면 그 Gateway 문서의 id 그대로다 (P57
    # 6.1절). 토큰의 `bound_to` 클레임(`gateway:<id>`)이 아니다. 데이터 플레인이 플랫폼을 믿고
    # 고정할 이름이 있을 때만 있다. 토큰 객체(`entry_token_objects`)와 스윕이 이것으로 그 Entry 를
    # 센다
    bound_gateway_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 그 게시된 Gateway 가 저장한 규칙 출처를 이 렌더가 읽은 것 (P57 6.2절). rules_digest 와 한 번에
    # 읽었으므로 토큰의 지문이 컴파일한 규칙보다 새롭지 않다. 이름은 게시된 Gateway 의 광고 이름이다
    published_sources: list[dict[str, ToolCallers]] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    # entry 항목의 인증 종류 (P86 6.2절). 인증이 있는 항목만 싣는다(header, P86b 의 oauth). 렌더는
    # 항목을 읽기 전에 이것으로 헤더 사본을 받을 Entry 를 고른다
    auth_kind: str | None = Field(default=None, exclude_if=lambda v: v is None)

    def exposed_names(self) -> list[str]:
        """항목이 아는 툴 가운데 필터를 지나는 것. 필터가 비면 전부다. tool_names 를 모르면 빈
        목록이다."""
        return [t for t in self.tool_names if not self.tools or t in self.tools]


class PublishedRender(BaseModel):
    """게시된 Gateway 가 마지막으로 적용한 렌더의 소비자 쪽 사실 (P57 6.2절). consumers 는 그 렌더가
    이름 댄 소비자 vMCP 의 서비스 신원이고, sources 는 그 렌더가 컴파일한 규칙 출처(자기 Gateway
    규칙과 백엔드의 규칙을 자기 광고 이름으로 편 것)이며 digest 는 그 지문이다. 소비자의 렌더가
    sources 와 digest 를 한 번에 읽는다."""

    model_config = ConfigDict(extra="ignore")

    consumers: list[str] = Field(default_factory=list)
    sources: list[dict[str, ToolCallers]] = Field(default_factory=list)
    digest: str | None = None


class Gateway(Document, ConsumerAuthState):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    # 정의 쪽
    spec: GatewaySpec
    revision: int = 1
    revisions: list[GatewayRevision] = Field(default_factory=list)
    # 배포 쪽. draft 에서는 비어 있다
    state: GatewayState = "draft"
    deployed_revision: int | None = None
    cluster_id: str | None = None
    k8s: GatewayK8s | None = None
    deployed_backends: list[GatewayBackendSnapshot] = Field(default_factory=list)
    # 스냅샷에서 계산한 실제 모드. 정의 쪽 spec.conflict_resolution 은 auto 일 수 있다
    deployed_conflict_resolution: Literal["priority", "prefix"] = "priority"
    tool_overrides: dict[str, dict[str, str]] = Field(
        default_factory=dict
    )  # workload → {tool: name}
    exposure: Exposure = Field(default_factory=Exposure)  # 외부 노출 설정. 늘 있다
    k8s_uid: str | None = None  # vMCP uid (ownerReference 용)
    # 서비스 토큰 판. 파드 템플릿 어노테이션이 이 값을 담아 토큰이 바뀌면 vMCP 가 굴러간다 (D41)
    svc_token_rev: str = "0"
    # 적용한 vMCP 의 토큰이 자기 audience 를 싣는 서버의 id (P50 9절). 인증 재적용은 서버를 그
    # audience 로 옮기기 전에 기다릴 소비자를 이것으로 가린다
    svc_token_servers: list[str] = Field(default_factory=list)
    effective_config: dict[str, Any] | None = None  # 배포 시점의 resolve_effective 스냅샷 (불변)
    overrides: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    last_spec_change: UtcDatetime = Field(default_factory=utcnow)
    # `deploying` 으로 들어간 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째).
    # `updated_at` 은 리컨실이 재시도마다 밀어 올려 경과 시간이 0 으로 돌아가므로 쓸 수 없다
    deploying_since: UtcDatetime | None = None
    # 배포가 도는 동안 돌 수 있는 렌더의 번호 (P53 3.5절). 턴 위임 게이트가 deploying·failed 문서를
    # 이것으로 읽는다. 해제가 비운다
    deploy_render: DeployRender | None = None
    # 게시된 Gateway 로서 적용한 렌더 (P57 6.2절). 게시하지 않았고 묶인 토큰도 남지 않았으면 None
    published_render: PublishedRender | None = None
    # 이 Gateway 에 묶여 나간 토큰의 가장 늦은 만료 (P57 6.2절). 소비자의 렌더가 토큰을 Secret 에
    # 넣기 전에 원자적 최댓값으로 쓴다(`MongoGatewayStore.raise_bound_until`). 정의 쪽과 배포 쪽
    # 저장 밖이라 옛 사본의 저장이 되돌리지 못한다
    bound_until: UtcDatetime | None = None
    # 마지막으로 성공한 vMCP 적용이 code mode 를 실었는가 (P56 6.3절). 정책 설정은 vMCP 보다 먼저
    # 적용되므로, 참인 동안의 렌더는 code mode 를 빼더라도 힌트 forbid 를 둔다. 메트릭 보고가
    # 스크립트 Tool 을 이름에 넣을지도 이것으로 정한다
    code_mode_applied: bool = False
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    def get_revision(self, n: int) -> GatewayRevision | None:
        for r in self.revisions:
            if r.revision == n:
                return r
        return None

    @property
    def current(self) -> GatewayRevision:
        rev = self.get_revision(self.revision)
        assert rev is not None
        return rev

    @property
    def deployed(self) -> GatewayRevision | None:
        """핀된 리비전. draft 면 None."""
        if self.deployed_revision is None:
            return None
        return self.get_revision(self.deployed_revision)

    @property
    def is_deployed(self) -> bool:
        """draft 가 아니다. 클러스터에 무엇인가 있거나 있으려 한다."""
        return self.state != "draft"


# 저장 단위. D42 가 문서를 정의 쪽과 배포 쪽으로 가른 것이 그대로 저장 단위다. 정의 저장은 정의 쪽
# 필드만, 배포 쪽 저장(상태 전이 포함)은 배포 쪽 필드만 $set 한다. 그래야 낡은 스냅샷을 든 저장이
# 남의 쪽을 되돌리지 못한다(PUT 이 도는 중인 배포를 draft 로, 스윕이 끝난 해제를 deployed 로).
# 공통 필드는 만들 때만 쓴다. updated_at 은 어느 쪽 저장이든 찍는다
SHARED_FIELDS: frozenset[str] = frozenset({"_id", "tenant", "name", "created_at"})
DEFINITION_FIELDS: frozenset[str] = frozenset(
    {"display_name", "spec", "revision", "revisions", "updated_by", "updated_at"}
)
DEPLOYMENT_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "deployed_revision",
        "cluster_id",
        "k8s",
        "deployed_backends",
        "deployed_conflict_resolution",
        "tool_overrides",
        "exposure",
        "k8s_uid",
        "svc_token_rev",
        "svc_token_servers",
        "effective_config",
        "overrides",
        "last_error",
        "last_spec_change",
        "deploying_since",
        "deploy_render",
        "published_render",
        "code_mode_applied",
        "updated_at",
        *AUTH_FIELDS,  # 인증 재적용의 칸 (P32 8절)
    }
)
# 어느 저장 단위에도 들지 않고 자기 조건부 갱신만 쓰는 칸. 묶인 토큰의 가장 늦은 만료(P57 6.2절)이고
# `raise_bound_until` 의 $max 만 쓴다
OWN_UPDATE_FIELDS: frozenset[str] = frozenset({"bound_until"})


class MongoGatewayStore(MongoInstanceStore[Gateway]):
    """Gateway 의 스토어. 세 종류가 함께 쓰는 질의는 `MongoInstanceStore` 에 있고 여기는 이 종류의
    질의와 인덱스만 둔다 (P71 2.3절)."""

    async def with_header_backends(self) -> list[Gateway]:
        """배포 스냅샷에 고정 헤더를 싣는 백엔드가 있는 draft 가 아닌 Gateway (P86 6.2절). 모든
        테넌트다."""
        return await self._many(
            self._scoped({"deployed_backends.auth_kind": "header", **NOT_DRAFT})
        )

    model = Gateway
    collection = GATEWAYS
    transition_fields = DEPLOYMENT_FIELDS

    async def init_schema(self) -> None:
        await super().init_schema()
        await self.coll.create_index([("spec.backends.hub_item_id", ASCENDING)], name="ix_hub_item")
        # 게시된 Gateway 의 소비자 집합을 재적용 틱마다 읽는다(`deployed_over`, P57 6.2절)
        await self.coll.create_index(
            [("deployed_backends.hub_item_id", ASCENDING)], name="ix_deployed_hub_item"
        )

    async def serving_by_k8s_name(self, tenant: str, name: str) -> Gateway | None:
        """그 테넌트에서 vMCP 이름이 name 이고 vMCP 가 도는(`deployed`·`deploying`) Gateway 다(검색
        엔드포인트가 토큰의 `svc:vmcp:<이름>` 으로 찾는다, P39b 12절). 재배포 중에도 옛 vMCP 가
        답하므로 `deploying` 을 받는다. 그 밖의 상태는 없는 것과 같다. Gateway 의 k8s 이름은
        이름에서 나오므로 테넌트 안에서 하나다."""
        doc = await self.coll.find_one(
            {"tenant": tenant, "k8s.name": name, "state": {"$in": list(SERVING_STATES)}}
        )
        return Gateway.from_doc(doc) if doc else None

    async def holding_entries(
        self, cluster_id: str, namespace: str, names: Collection[str], *, exclude_id: str
    ) -> Gateway | None:
        """그 클러스터·네임스페이스에서 Entry 이름을 쥐는 상태(`ENTRY_HOLDING_STATES`)의 문서
        가운데 배포 스냅샷의 Entry 이름(`deployed_backends.workload`)이 names 와 하나라도 겹치는 것
        하나다. exclude_id 는 빼고, 리비전 이력과 overrides 는 읽지 않는다. Entry 는 네임스페이스
        안에서 이름이 하나뿐이므로 겹치면 그 문서가 이미 그 이름을 쓰고 있다."""
        if not names:
            return None
        doc = await self.coll.find_one(
            {
                "_id": {"$ne": exclude_id},
                "cluster_id": cluster_id,
                "k8s.namespace": namespace,
                "deployed_backends.workload": {"$in": list(names)},
                "state": {"$in": list(ENTRY_HOLDING_STATES)},
            },
            DEPLOYED_ONLY,
        )
        return self._one(doc)

    async def entry_names_in_use(self) -> set[tuple[str, str, str, str]]:
        """배포 쪽 문서의 배포 스냅샷이 쓰는 Entry 이름. (문서 id, 클러스터, 네임스페이스, Entry
        이름) 이고, Entry 이름 선점의 쓸기가 틱마다 읽으므로 그 칸만 읽는다."""
        cursor = self.coll.find(
            NOT_DRAFT, {"cluster_id": 1, "k8s.namespace": 1, "deployed_backends.workload": 1}
        )
        return {
            (d["_id"], d.get("cluster_id"), (d.get("k8s") or {}).get("namespace"), b["workload"])
            for d in await cursor.to_list()
            for b in d.get("deployed_backends") or []
        }

    async def raise_bound_until(self, gateway_id: str, until: datetime) -> None:
        """게시된 Gateway 의 `bound_until` 을 until 로 올린다. 이미 더 늦으면 그대로다(`$max`). 두
        소비자가 함께 토큰을 내도 앞으로만 간다 (P57 6.2절)."""
        await self.coll.update_one({"_id": gateway_id}, {"$max": {"bound_until": until}})

    async def deployed_over(self, hub_item_ids: Collection[str]) -> list[Gateway]:
        """배포 스냅샷(`deployed_backends`)이 그 허브 항목 가운데 하나를 쓰는 draft 아닌 문서. 배포
        중, 실패, 내리는 중의 문서도 마지막 렌더로 돌 수 있으므로 든다. 정의만 쓰는 문서는 들지
        않는다 (P57 6.2절)."""
        if not hub_item_ids:
            return []
        return await self._many(
            {"deployed_backends.hub_item_id": {"$in": list(hub_item_ids)}, **NOT_DRAFT}
        )

    async def using_hub_item(self, hub_item_id: str) -> list[Gateway]:
        """이 허브 항목을 쓰는 문서. 정의 쪽(`spec.backends`)과 배포 스냅샷(`deployed_backends`)을
        함께 본다. draft 정의도 항목을 붙들고, 배포된 vMCP 는 정의를 그 사이 고쳤더라도 스냅샷이
        가리키는 항목을 실제로 쓰고 있다."""
        return await self._many(
            {
                "$or": [
                    {"spec.backends.hub_item_id": hub_item_id},
                    {"deployed_backends.hub_item_id": hub_item_id},
                ]
            }
        )
