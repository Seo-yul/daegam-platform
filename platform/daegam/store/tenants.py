"""Tenant Settings (`_id` = tenant). 설정 계층의 3단계다."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from daegam.auth.rbac import PLATFORM_GROUP_HEAD
from daegam.config import Settings
from daegam.store.base import SeqModel, UtcDatetime, from_stored, upsert_by_seq, utcnow

TENANT_SETTINGS = "tenant_settings"

# k8s 네임스페이스 이름 = DNS 라벨. 플랫폼·k8s 네임스페이스를 피하는 검사는 설정이 필요해
# services.tenancy.check_namespace_name 이 한다
NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def default_mcp_group(tenant: str) -> str:
    return f"daegam-{tenant}"


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_service_id: str
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class TenantNamespace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    labels: dict[str, str] | None = None

    @field_validator("name")
    @classmethod
    def _dns_label(cls, v: str | None) -> str | None:
        # 이 이름은 그대로 Namespace 매니페스트와 RoleBinding 에 들어간다. 모양이 틀리면 저장
        # 단계에서 막는다. 빈 문자열은 "정하지 않음"이라 None 으로 접는다
        if not v:
            return None
        if not NAMESPACE_RE.fullmatch(v):
            raise ValueError(f"invalid namespace name {v!r}: must be a DNS label")
        return v


class AgentOAuth(BaseModel):
    """에이전트 런타임의 client credentials (P6 F4b). Okta(또는 mock OIDC) 앱 등록은 수동이고,
    secret_name 은 테넌트 네임스페이스의 Secret(키 client_secret) 이름이다. Agent(D26) 의 validate
    가 oidc 모드의 Gateway Tool 소스에 이것을 요구하고, compile 이 client_id·token_url·scope 를
    agent.json 의 tools[].auth 로 펼친다(비밀은 env 이름 DAEGAM_AGENT_CLIENT_SECRET 뿐이다).
    vMCP Cedar 의 cid 정책은 agent_oauth 가 아니라 agent_client_ids 가 정한다 (인가와 자격 분리)."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    secret_name: str = Field(min_length=1)  # 테넌트 네임스페이스 Secret, 키 client_secret
    token_url: str = Field(min_length=1)
    scope: str = Field(min_length=1)  # Okta custom authorization server 는 scope 필수


class TenantSettings(SeqModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    default_cluster_id: str | None = None
    allowed_cluster_ids: list[str] | None = None
    namespace: TenantNamespace | None = None
    quota: dict[str, Any] | None = None  # ResourceQuota spec.hard
    default_bindings: list[Binding] | None = None
    mcp_group: str | None = None  # Okta 그룹 = Cedar THVGroup 이름. None 이면 daegam-<tenant>
    # 에이전트 client credentials 앱의 client id 들 (Okta 등록은 수동, Cedar 의 client id 정책에
    # 들어간다)
    agent_client_ids: list[str] = Field(default_factory=list)
    agent_oauth: AgentOAuth | None = None
    # 기본 네임스페이스 외에 인스턴스를 둘 수 있는 네임스페이스 (D5·D22). platform-admin
    # settings:write 전용이고 저장 시점에 플랫폼이 각 네임스페이스를 보장한다
    allowed_namespaces: list[str] = Field(default_factory=list)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None


def human_mcp_group(settings: Settings, ts: TenantSettings) -> str:
    """테넌트의 MCP 그룹 (P32 2절). 내부 서버에 가는 서비스 토큰의 groups 가 이 값을 쓴다. local
    은 플랫폼이 토큰에 싣고 Cedar 에 쓰는 내부 이름이라 daegam-<tenant> 로 고정한다. 바꿀 수 있으면
    다른 테넌트의 그룹을 가져와 그 Gateway 를 열 수 있어서다. 저장된 mcp_group 은 local 이 아닌
    모드에서 쓰인다. oidc 는 IdP 그룹을 매핑하고, header 는 서비스 토큰의 groups 에만 싣는다.
    데이터 플레인 렌더의 그룹은 `data_plane_group` 이고, 플랫폼이 사람 경계에 내는 토큰은 늘
    `default_mcp_group` 이다 (P53b 6.1절)."""
    if settings.auth_mode == "local" or not ts.mcp_group:
        return default_mcp_group(ts.tenant)
    return ts.mcp_group


def data_plane_group(settings: Settings, ts: TenantSettings) -> str:
    """데이터 플레인 렌더(Gateway·노출 앞단 Cedar 의 THVGroup, 노출 Agent 의 빈 allowed_groups
    채우기)가 쓰는 테넌트 그룹 (P53b 6.1절). 데이터 플레인이 플랫폼을 믿으면 플랫폼이 토큰에 싣는
    daegam-<tenant> 이고, 저장된 mcp_group 은 IdP 를 믿는 oidc 모드에서만 읽는다. 다른 테넌트의
    고정 그룹을 적은 mcp_group 이 그 테넌트의 토큰에 이 테넌트의 Gateway 를 열지 않게 한다."""
    if settings.data_plane_trusts_platform:
        return default_mcp_group(ts.tenant)
    return human_mcp_group(settings, ts)


class MongoTenantSettingsStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[TENANT_SETTINGS]

    async def init_schema(self) -> None:
        return None

    async def list_tenants(self) -> list[str]:
        """설정 문서가 있는 테넌트 이름(정렬). platform-admin 의 테넌트 선택기가 쓴다."""
        return sorted(await self.coll.distinct("_id"))

    async def custom_mcp_groups(self) -> dict[str, str]:
        """저장된 mcp_group 이 기본값(daegam-<tenant>)과 다른 테넌트 → 그 그룹. local 모드는 그 값을
        쓰지 않으므로 기동 경고가 이것을 알린다."""
        cursor = self.coll.find({"mcp_group": {"$nin": [None, ""]}}, {"mcp_group": 1})
        return {
            d["_id"]: d["mcp_group"]
            for d in await cursor.to_list()
            if d["mcp_group"] != default_mcp_group(d["_id"])
        }

    async def foreign_mcp_groups(self) -> dict[str, str]:
        """저장된 mcp_group 이 다른 테넌트의 고정 그룹이나 그 밖의 `daegam-` 이름인 테넌트 → 그
        그룹. 그동안 데이터 플레인은 플랫폼으로 바뀌지 않는다 (P53b 6.10절). 옛 Cedar 가 그 이름을
        싣는 동안 그 이름의 플랫폼 토큰이 이 테넌트의 Gateway 에 들어오기 때문이다."""
        custom = await self.custom_mcp_groups()
        return {t: g for t, g in custom.items() if g.startswith(PLATFORM_GROUP_HEAD)}

    async def with_agent_client_ids(self) -> list[str]:
        """agent_client_ids 가 하나라도 있는 테넌트 이름(정렬). 한 번의 조회다."""
        cursor = self.coll.find({"agent_client_ids.0": {"$exists": True}}, {"_id": 1})
        return sorted(d["_id"] for d in await cursor.to_list())

    async def get(self, tenant: str) -> TenantSettings:
        doc = await self.coll.find_one({"_id": tenant})
        if doc is None:
            return TenantSettings(tenant=tenant)
        return from_stored(TenantSettings, doc)

    async def update(
        self,
        tenant: str,
        changes: dict[str, Any],
        *,
        updated_by: str | None = None,
        base: TenantSettings | None = None,
    ) -> TenantSettings | None:
        """사람의 저장 (P68 2절). base(없으면 지금 읽은 값) 위에 changes 를 얹어 검증하고 보낸
        절만 쓴다. 그사이 다른 저장이 지나갔으면 None 이다."""
        current = base if base is not None else await self.get(tenant)
        doc = await upsert_by_seq(self.coll, tenant, current, changes, updated_by)
        return None if doc is None else from_stored(TenantSettings, doc)
