"""MCP Server (D42). 정의와 배포가 한 문서에 산다.

정의 쪽은 `spec`·`revision`·`revisions` 다. 고칠 때마다 리비전이 늘고 이전 리비전은 `revisions[]` 에
불변 스냅샷으로 남는다. 배포 쪽은 `state`·`deployed_revision`·`cluster_id`·`k8s`·`effective_config`·
`overrides` 다. `draft` 에서는 배포 쪽이 비어 있고 `exposure` 만 늘 있다. 배포 설정이자 복제가
받는 값이기 때문이다.

옛 인스턴스 스토어의 프로토콜(`live_on_cluster`·`live_by_k8s_name`·`list`·`get`)을 지키고, 그
메서드는 `draft` 를 뺀다. `draft` 는 `k8s`·`cluster_id` 가 없어 그 자리에 나오면
`hosts_in_use` 같은 소비자가 터진다. 화면 목록만 `include_draft=True` 로 전부 본다.

저장은 `set_fields` 하나다. 문서 전체를 쓰는 저장은 없다. 정의 쪽(`DEFINITION_FIELDS`)은 `revision`
을, 배포 쪽(`DEPLOYMENT_FIELDS`, 전이 `transition` 포함)은 `state` 를 조건에 걸어 자기 쪽 필드만
`$set` 한다. 한 문서에 두 쪽이 살아서 낡은 스냅샷이 남의 쪽을 되돌리지 못하게 하는 규칙이다.
토큰 scope 문자열 `mcp-profile:<id>` 의 id 는 문서의 `_id` 다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pymongo import ASCENDING

from daegam.store.auth_reapply import AUTH_FIELDS, AudienceSwitch, ConsumerAuthState
from daegam.store.backend_apis import AuthKind
from daegam.store.base import (
    Document,
    TimeoutSeconds,
    UtcDatetime,
    new_id,
    utcnow,
)
from daegam.store.data_services import DataServiceKind, RedisTls
from daegam.store.exposure import Exposure, front_name_for
from daegam.store.instances import DEPLOYED_ONLY, NOT_DRAFT, MongoInstanceStore
from daegam.store.tenants import Binding
from daegam.store.tool_rules import ServerToolRule

MCP_SERVERS = "mcp_servers"
# backend 호출 타임아웃의 기본값(초). 런타임(spec2openapi `BridgeOptions.timeout`)의 기본값과 같아,
# 이 값이면 렌더가 타임아웃 env 를 내지 않는다
MCP_DEFAULT_TIMEOUT_SECONDS = 30

MCPServerState = Literal["draft", "deploying", "deployed", "failed", "undeploying", "deleting"]


class ToolOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None


class ProfileRuntimeSpec(BaseModel):
    """runtime 절. replicas 는 ToolHive spec.replicas(프록시)다."""

    model_config = ConfigDict(extra="forbid")

    image: str | None = None
    replicas: int = Field(default=1, ge=1, le=10)
    resources: dict[str, Any] | None = None


class BackendSelection(BaseModel):
    """노출하는 backend 하나. Backend API 의 버전 핀과 Tool 선택·오버라이드."""

    model_config = ConfigDict(extra="forbid")

    backend_profile_id: str
    spec_version: int = Field(ge=1)
    tools: list[str] = Field(default_factory=list)
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    # 이 backend 를 부르는 호출의 타임아웃(초) (P50 7절). 이 필드가 없던 리비전은 기본값으로 읽는다.
    # default_factory 라 API 스키마가 필수로 적지 않아, 화면이 보내지 않아도 된다
    timeout_seconds: TimeoutSeconds = Field(default_factory=lambda: MCP_DEFAULT_TIMEOUT_SECONDS)
    # operationId 마다의 Tool 규칙 (P52 1절). 비어 있으면 저장하지 않아, 규칙이 없는 리비전은 이
    # 칸이 생기기 전과 같다
    tool_rules: dict[str, ServerToolRule] = Field(default_factory=dict, exclude_if=lambda v: not v)


class MCPServerSpec(BaseModel):
    """리비전마다 스냅샷되는 부분. backends 는 1개 이상이고 순서가 의미를 가진다
    (스펙 파일 이름 openapi-<i>.yaml, 토큰 키 token-<i>)."""

    model_config = ConfigDict(extra="forbid")

    backends: list[BackendSelection] = Field(min_length=1)
    runtime: ProfileRuntimeSpec = Field(default_factory=ProfileRuntimeSpec)
    bindings: list[Binding] = Field(default_factory=list)


class MCPServerRevision(MCPServerSpec):
    revision: int
    created_at: UtcDatetime = Field(default_factory=utcnow)


class InstanceK8s(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    namespace: str
    group: str
    token_secret: str


class BackendInputs(BaseModel):
    """배포 때 MCPServer 렌더가 Backend API 에서 읽은 값 (P50 1절). 비밀 값은 없고 자격증명은
    Secret 이름뿐이다. 스냅샷의 순서는 핀된 리비전의 backends 와 같다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    endpoint: str | None = None
    auth: AuthKind = "none"
    credential_secret: str | None = None

    @property
    def has_credentials(self) -> bool:
        """자격증명 Secret 을 렌더하는 backend 인가. auth 가 none 이 아니고 Secret 이름이 있다."""
        return self.auth != "none" and bool(self.credential_secret)


class BindingInputs(BaseModel):
    """배포 때 MCPServer 렌더가 바인딩한 Database 에서 읽은 값 (P50 1절). 비밀번호는 Secret
    이름뿐이다. 리비전 바인딩이 가리키는 Database 마다 하나다. 뒤의 판이 더할 칸이 있어도 읽고
    그대로 다시 쓴다. 롤아웃 중의 옛 레플리카와 되돌린 판이 같은 스냅샷을 읽고
    저장한다."""

    model_config = ConfigDict(extra="allow")

    data_service_id: str
    name: str
    cluster_id: str
    kind: DataServiceKind
    host: str
    port: int
    database: str | None = None
    db: int | None = None
    username: str | None = None
    secret_name: str
    has_password: bool = False
    # Database 의 TLS 끝점 (P61). 이 스냅샷을 적은 배포가 이것으로 렌더했다
    tls: RedisTls | None = None
    # 그 배포 때 평문 포트가 닫혀 있었는가 (P61 5절 5단계). 다시 렌더가 배포 때의 판단을 따른다
    plain_closed: bool = False


class MCPServerInputs(BaseModel):
    """MCPServer CR 이 공유 문서(Backend API·Database)에서 받는 값의 배포 스냅샷 (P50 1절). 배포한
    문서의 재렌더는 현재 공유 문서 대신 이것을 읽는다. 그래서 공유 문서를 고쳐도 재배포하기 전에는
    돌고 있는 서버의 CR 에 반영되지 않는다. 파드가 뜰 때 받는 profile.json 은 현재 공유 문서를
    따른다.

    `deploying_since` 는 이 스냅샷을 적은 배포의 표시다. 문서의 `deploying_since` 와 다르면 그 뒤
    다른 배포가 있었던 것이라(이 칸을 모르는 옛 레플리카의 재배포 등) 없는 스냅샷으로 본다. 뒤의
    판이 더할 칸이 있어도 읽고 그대로 다시 쓴다(`BindingInputs` 와 같은 까닭, P61 1절)."""

    model_config = ConfigDict(extra="allow")

    backends: list[BackendInputs]
    bindings: list[BindingInputs] = Field(default_factory=list)
    deploying_since: UtcDatetime | None = None
    # 이 배포가 세션 저장소를 TLS 로 렌더했는가 (P61 3절). 다시 렌더는 이것을 따르고, 판이 0.52
    # 아래로 알려졌을 때만 평문으로 내린다. 이 칸 전의 스냅샷(None)은 평문이다
    session_tls: bool | None = None
    # backend id 마다 soap:header 값의 Secret 이름 (T60). `BackendInputs` 는 낯선 칸을 거절하는 옛
    # 판이 있어 그 안이 아니라 여기에 둔다. 비어 있으면 저장하지 않아, 그 칸을 쓰지 않는 스냅샷은
    # 이 칸이 생기기 전과 같다
    soap_headers_secrets: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)


class ConditionsRecord(BaseModel):
    """서버의 파드가 인자 조건을 지키는지의 기록 (P52 4절, `services/condition_records.py`).

    StatefulSet 의 모든 파드가 같은 이미지 digest(`image_id`)로 돌 때 플랫폼이 `svc:platform` 으로
    서버의 `initialize` 를 읽어 적는다. `image` 는 그 파드의 mcp 컨테이너 이미지 참조,
    `enforces` 는 그 이미지가 `capabilities.experimental.arg_conditions` 를 알렸는가,
    `unresolved` 는 그 이미지가 풀지 못해 닫은 Tool 이름마다의 경로, `revision` 은 읽을 때 모든
    파드가 돌던 리비전이다(배포한 리비전과 같을 때만 적는다). 뒤 판이 칸을 더해도 읽히게 모르는
    칸은 버린다."""

    model_config = ConfigDict(extra="ignore")

    image: str
    image_id: str
    enforces: bool
    revision: int
    unresolved: dict[str, list[str]] = Field(default_factory=dict)
    observed_at: UtcDatetime = Field(default_factory=utcnow)


class MCPServer(Document, ConsumerAuthState):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    # 정의 쪽
    spec: MCPServerSpec
    revision: int = 1
    revisions: list[MCPServerRevision] = Field(default_factory=list)
    # 배포 쪽. draft 에서는 비어 있다
    state: MCPServerState = "draft"
    deployed_revision: int | None = None
    cluster_id: str | None = None
    k8s: InstanceK8s | None = None
    spec_token_id: str | None = None  # backends[0] 의 토큰 (하위 호환·응답용)
    spec_token_ids: list[str] = Field(default_factory=list)  # 리비전 backends 순서와 정렬
    effective_config: dict[str, Any] | None = None  # 배포 시점의 resolve_effective 스냅샷 (불변)
    # CR 이 공유 문서에서 받은 값의 배포 스냅샷 (P50 1절). draft 에서는 비어 있다
    deployed_inputs: MCPServerInputs | None = None
    # 마지막 렌더가 MCP 파드에 단 Redis CA 의 출처와 지문(`templates/common.py`
    # `REDIS_CA_ANNOTATION`, P61). 재적용이 그 출처의 지금 CA 와 견주어 바뀌었으면 다시 렌더한다.
    # 지문 없이 렌더했으면 빈 값이고, None 이면 아직 렌더하지 않았다
    redis_ca: str | None = None
    # 외부 노출 설정 (P8, docs/design/resource-model.md 11절). 늘 있다
    exposure: Exposure = Field(default_factory=Exposure)
    k8s_uid: str | None = None  # MCPServer uid (앞단 프록시·Ingress·NetworkPolicy ownerReference)
    # 노출 앞단의 서비스 토큰 판. 앞단 파드 템플릿 어노테이션이 이 값을 담는다 (D41)
    svc_token_rev: str = "0"
    # 적용한 앞단의 토큰이 자기 audience 를 싣는 서버(이 문서)의 id (P50 9절). 노출하지 않으면 빈다
    svc_token_servers: list[str] = Field(default_factory=list)
    # 이 서버의 MCPServer 가 테넌트 audience 를 놓고 자기 audience 로 검증하게 된 기록 (P50 9절, G9
    # 2단계). 없으면 테넌트 audience 다. 생성과 인증 재적용만 쓴다(`SHARED_FIELDS`)
    audience_switch: AudienceSwitch | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)  # F7a Advanced 편집
    # 파드가 인자 조건을 지키는지의 기록 (P52 4절). 스윕만 쓰고 상태 전이는 쓰지 않는다
    # (`TRANSITION_FIELDS`). None 이면 모른다
    conditions_record: ConditionsRecord | None = None
    last_error: str | None = None
    last_spec_change: UtcDatetime = Field(default_factory=utcnow)
    # `deploying` 으로 들어간 시각. 화면이 여기서부터 흐른 시간을 재 진행 배너를 바꾼다 (D43 둘째).
    # `updated_at` 은 리컨실이 재시도마다 밀어 올려 경과 시간이 0 으로 돌아가므로 쓸 수 없다
    deploying_since: UtcDatetime | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None  # 마지막으로 만들거나 고친 주체의 sub (감사)

    def get_revision(self, n: int) -> MCPServerRevision | None:
        for r in self.revisions:
            if r.revision == n:
                return r
        return None

    # 이번 렌더 앞에 적은 판의 바탕(인증 입력의 판, `MCPServerService._stamp_auth`). 적용한 렌더가
    # 그 위에 Redis 와 audience 표시, 넘겨받기의 단계를 얹어 판을 다시 적는다. 저장하지 않는다
    _stamp_base: tuple[str | None] | None = PrivateAttr(default=None)
    # 판을 적기 전의 재적용 실패 수. 넘겨받기를 다시 미루는 렌더가 이어 센다 (P54 6절)
    _stamp_failures: int = PrivateAttr(default=0)

    def set_stamp_base(self, base: str | None, *, failures: int = 0) -> None:
        self._stamp_base = (base,)
        self._stamp_failures = failures

    @property
    def stamp_failures(self) -> int:
        return self._stamp_failures

    @property
    def stamp_pending(self) -> bool:
        """이번 동작이 렌더 앞에 판을 적었는가."""
        return self._stamp_base is not None

    @property
    def stamp_base(self) -> str | None:
        return self._stamp_base[0] if self._stamp_base is not None else None

    @property
    def current(self) -> MCPServerRevision:
        rev = self.get_revision(self.revision)
        assert rev is not None
        return rev

    @property
    def deployed(self) -> MCPServerRevision | None:
        """핀된 리비전. draft 면 None."""
        if self.deployed_revision is None:
            return None
        return self.get_revision(self.deployed_revision)

    @property
    def is_deployed(self) -> bool:
        """draft 가 아니다. 클러스터에 무엇인가 있거나 있으려 한다."""
        return self.state != "draft"

    @property
    def token_ids(self) -> list[str]:
        """발급한 스펙 토큰 id."""
        return list(self.spec_token_ids)


def token_secret_name_for(server_id: str) -> str:
    return f"inst-{server_id}-token"


# 저장 단위. D42 가 문서를 정의 쪽과 배포 쪽으로 가른 것이 그대로 저장 단위다. 정의 저장은 정의 쪽
# 필드만, 배포 쪽 저장(상태 전이 포함)은 배포 쪽 필드만 $set 한다. 그래야 낡은 스냅샷을 든 저장이
# 남의 쪽을 되돌리지 못한다(PUT 이 도는 중인 배포를 draft 로, 스윕이 끝난 해제를 deployed 로).
# 공통 필드는 만들 때만 쓴다. updated_at 은 어느 쪽 저장이든 찍는다. audience 기록도 만들 때 쓰고,
# 그 뒤로는 인증 재적용만 그 칸 하나를 따로 쓴다 (P50 9절). 배포 쪽 저장이 낡은 기록으로 되돌리지
# 않게 하려는 것이다
SHARED_FIELDS: frozenset[str] = frozenset(
    {"_id", "tenant", "name", "created_at", "audience_switch"}
)
DEFINITION_FIELDS: frozenset[str] = frozenset(
    {"display_name", "spec", "revision", "revisions", "updated_by", "updated_at"}
)
DEPLOYMENT_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "deployed_revision",
        "cluster_id",
        "k8s",
        "spec_token_id",
        "spec_token_ids",
        "effective_config",
        "deployed_inputs",
        "redis_ca",
        "exposure",
        "k8s_uid",
        "svc_token_rev",
        "svc_token_servers",
        "overrides",
        "conditions_record",
        "last_error",
        "last_spec_change",
        "deploying_since",
        "updated_at",
        *AUTH_FIELDS,  # 인증 재적용의 칸 (P32 8절)
    }
)
# 인자 조건의 기록(P52 4절). 배포 쪽 칸이지만 상태 전이는 쓰지 않는다. 요청이 시작할 때 읽은 문서로
# 전이하면 그 사이 스윕이 적은 기록을 옛 값으로 덮기 때문이다. 쓰는 곳은 `set_conditions_record`
# 하나다(스윕과 draft 에서의 배포)
CONDITIONS_RECORD = "conditions_record"
TRANSITION_FIELDS: frozenset[str] = DEPLOYMENT_FIELDS - {CONDITIONS_RECORD}


class MongoMCPServerStore(MongoInstanceStore[MCPServer]):
    """MCPServer 의 스토어. 세 종류가 함께 쓰는 질의는 `MongoInstanceStore` 에 있고 여기는 이 종류의
    질의와 인덱스만 둔다 (P71 2.3절)."""

    model = MCPServer
    collection = MCP_SERVERS
    transition_fields = TRANSITION_FIELDS

    async def init_schema(self) -> None:
        await super().init_schema()
        await self.coll.create_index(
            [("revisions.bindings.data_service_id", ASCENDING)], name="ix_binding_ds"
        )
        # 인덱스 이름은 `ix_backend_profile` 그대로다. 키가 `backend_profile_id` 로 남았고, 이름만
        # 바꾸면 그 인덱스가 이미 있는 배포본에서 Mongo 가 IndexOptionsConflict 로 거절해 기동이
        # 막힌다
        await self.coll.create_index(
            [("spec.backends.backend_profile_id", ASCENDING)], name="ix_backend_profile"
        )

    async def live_binding_data_service(
        self, ds_id: str, *, tenant: str | None = None, deployed_only: bool = False
    ) -> list[MCPServer]:
        """핀된 effective_config 가 이 Data Service 를 바인딩하는 배포 쪽 문서. deployed_only 면
        리비전 이력과 overrides 를 읽지 않는다(`DEPLOYED_ONLY`)."""
        projection = DEPLOYED_ONLY if deployed_only else None
        query = self._bound_to("effective_config.bindings.data_service_id", ds_id, tenant)
        return await self._many(query, projection=projection)

    async def set_conditions_record(
        self,
        server_id: str,
        record: ConditionsRecord | None,
        *,
        expect_revision: int | None = None,
        expect_deployment: datetime | None = None,
    ) -> bool:
        """인자 조건의 기록만 고친다 (P52 4절). 필드 하나만 `$set` 해 그 사이의 다른 쓰기를 덮지
        않는다. expect_revision·expect_deployment 를 주면 저장된 `deployed_revision`·
        `deploying_since` 가 그대로일 때만 쓴다. 기록을 읽은 배포가 지금 배포일 때만 쓰려는
        것이다. 쓰지 않았으면 False 다."""
        query: dict[str, Any] = {"_id": server_id}
        if expect_revision is not None:
            query["deployed_revision"] = expect_revision
        if expect_deployment is not None:
            query["deploying_since"] = expect_deployment
        value = record.model_dump(mode="python") if record is not None else None
        res = await self.coll.update_one(query, {"$set": {CONDITIONS_RECORD: value}})
        return res.matched_count == 1

    async def front_entries_in_use(self) -> set[tuple[str, str, str, str]]:
        """배포 쪽의 노출한 서버가 쥐는 vMCP 앞단의 Entry 이름 `front-<k8s.name>` (P54 5절). (문서
        id, 클러스터, 네임스페이스, Entry 이름) 이고 Entry 이름 선점의 쓸기가 Gateway 의 것과 함께
        읽는다 (`services/gateway_entries.py` `GatewayEntries.sweep`). 노출을 끄거나 내리거나 지우면
        빠지므로 쓸기가 그 선점을 놓는다."""
        cursor = self.coll.find(
            {"exposure.enabled": True, **NOT_DRAFT}, {"cluster_id": 1, "k8s": 1}
        )
        return {
            (d["_id"], d.get("cluster_id"), k8s["namespace"], front_name_for(k8s["name"]))
            for d in await cursor.to_list()
            if (k8s := d.get("k8s"))
        }

    async def live_with_rules(self) -> list[MCPServer]:
        """어느 리비전에든 Tool 규칙을 든 배포 쪽 문서다. 규칙이 없는 backend 는 그 칸을 저장하지
        않으므로(`BackendSelection.tool_rules`) 칸이 있는지로 고른다."""
        return await self._many({"revisions.backends.tool_rules": {"$exists": True}, **NOT_DRAFT})

    async def bound_to_data_service(self, ds_id: str) -> list[MCPServer]:
        """현재 리비전이 이 Data Service 를 바인딩하는 문서. draft 도 든다."""
        return await self._many({"spec.bindings.data_service_id": ds_id})

    async def using_backend_api(self, backend_profile_id: str) -> list[MCPServer]:
        """현재 스펙이 이 Backend API 을 참조하는 문서. draft 도 들고,
        단수 backend 시절 문서(spec.backend_profile_id)도 함께 찾는다."""
        return await self._many(
            {
                "$or": [
                    {"spec.backends.backend_profile_id": backend_profile_id},
                    {"spec.backend_profile_id": backend_profile_id},
                ]
            }
        )
