"""MCP Hub 응답 (api/hub.py 의 _out 과 같은 필드)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas import REQUIRED_DEFAULTS, RulesNotice
from daegam.auth.hub_access import Access, Subjects
from daegam.services.hub_catalog import CatalogEnv, CatalogHeader, CatalogImage, CatalogServer
from daegam.store.hub import (
    HubAdmission,
    HubAuth,
    HubCatalogOrigin,
    HubEnvVar,
    HubItemBase,
    HubK8s,
    HubSecretEnvVar,
    HubSource,
)
from daegam.urls import AddressClass


class HubEnvVarOut(HubEnvVar):
    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")


class HubSecretEnvVarOut(HubSecretEnvVar):
    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")


class HubCatalogOriginOut(HubCatalogOrigin):
    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")


class HubAdmissionOut(HubAdmission):
    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")


def _absent(v: object) -> bool:
    return v is None


class HubSourceOut(HubSource):
    """응답의 출처. 저장된 출처가 든 뒤 판의 칸(P55 9절)은 이 판의 계약이 아니라서 버린다. 출처 안의
    목록과 객체도 응답 모델로 읽어, 뒤 판이 그 안에 더한 칸도 버린다."""

    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")

    env: list[HubEnvVarOut] | None = Field(default=None, exclude_if=_absent)
    secret_env: list[HubSecretEnvVarOut] | None = Field(default=None, exclude_if=_absent)
    catalog: HubCatalogOriginOut | None = Field(default=None, exclude_if=_absent)
    admission: HubAdmissionOut | None = Field(default=None, exclude_if=_absent)


class HubAuthOut(HubAuth):
    """응답의 인증 (P86 6.1절). 저장된 인증이 든 뒤 판의 칸은 이 판의 계약이 아니라서 버린다. 값은
    처음부터 없다."""

    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="ignore")


class HubK8sOut(HubK8s):
    model_config = REQUIRED_DEFAULTS


class SubjectsOut(Subjects):
    model_config = REQUIRED_DEFAULTS


class AccessOut(Access):
    """응답의 access (D23). 입력은 auth.hub_access.Access 그대로다 (필드 생략 가능)."""

    model_config = REQUIRED_DEFAULTS

    view: SubjectsOut
    consume: SubjectsOut
    manage: SubjectsOut


class HubStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None
    ready: bool
    url: str | None
    #: 사람이 MCP 클라이언트에 붙여 넣는 주소 (D50). 밖으로 열지 않았으면 null 이고 그때는 `url`
    #: (클러스터 안 주소)만 있다. remote·entry 는 등록한 주소가 outside·private 일 때만 있다
    external_url: str | None = None
    #: remote·entry 의 등록한 주소의 갈래 (P55a 1절). 주소가 없거나 우리가 만든 항목이면 null 이다
    address_class: AddressClass | None = None
    message: str | None
    observed_at: str
    error: str | None
    # 항목의 Tool 규칙 경고 (P52 7절). 규칙이 없으면 빈다
    warnings: list[str] = Field(default_factory=list)


class PermissionsOut(BaseModel):
    """요청 주체가 이 항목에 할 수 있는 동작 (D23). UI 가 버튼·선택 가능 여부를 이것으로 그린다."""

    model_config = ConfigDict(extra="forbid")

    view: bool
    consume: bool
    manage: bool


class HubItemPermissionsOut(PermissionsOut):
    """허브 항목의 동작에 헤더 값 바꾸기(P86 6.1절)와 Secret env 값 바꾸기(P55 5절)를 더한 것. 둘 다
    만든 사람과 tenant-admin 이다."""

    replace_header: bool
    replace_secret_env: bool


class HubItemOut(HubItemBase):
    """허브 항목 응답. 재적용 칸(P50 1절)은 재적용 단계의 기록이라 싣지 않으려고 `HubItem` 이 아니라
    `HubItemBase` 를 상속한다. 인스턴스 응답도 그 칸을 싣지 않고, 막힘과 대기는
    `GET /settings/data-plane-auth` 가 보인다."""

    model_config = REQUIRED_DEFAULTS

    source: HubSourceOut
    k8s: HubK8sOut
    status: dict[str, Any]  # 저장된 마지막 status 요약 (refresh-tools 가 채운다)
    access: AccessOut
    auth: HubAuthOut | None = None
    permissions: HubItemPermissionsOut
    # access 에서 파생한 읽기 전용 값. 다른 테넌트에 view 나 consume 이 열려 있으면 shared
    scope: Literal["tenant", "shared"]


class HubItemWithStatus(HubItemOut):
    status: HubStatus  # type: ignore[assignment]  # 조회 시 라이브 status


class HubToolRulesSaved(HubItemWithStatus):
    """image 항목의 규칙 저장의 답 (P52 1절). opened 는 callers 를 모두 지워 열리는 Tool 이다."""

    rules_notice: RulesNotice


class HubItemRef(BaseModel):
    """MCP Server 응답의 게시 항목 요약 (3절). `gateway_ids` 는 이 항목을 백엔드로 둔
    Gateway 다 (삭제 확인 대화상자가 "참조 중인 Gateway n개" 를 보인다)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    display_name: str
    permissions: PermissionsOut
    gateway_ids: list[str]


class HubManifestDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    name: str
    rendered: dict[str, Any]
    overrides: dict[str, Any]


class HubManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[HubManifestDocument]


class HubToolOut(BaseModel):
    """Tool 하나가 스스로 말하는 것 (D48). 저장된 값이 아니라 그때그때 물어본 값이다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None = None
    description: str | None = None
    #: MCP 가 준 JSON Schema 그대로. 너무 커서 덜어냈으면 null 이고 schema_omitted 가 true 다
    input_schema: dict[str, Any] | None = None
    schema_omitted: bool = False
    #: 결과에도 스키마가 있는가. 스키마 자체는 싣지 않는다
    output_schema: bool = False


class HubToolsResponse(BaseModel):
    """`listed` 가 false 면 서버에 물어보지 못한 것이고 `reason` 이 까닭이다. 그때 화면은
    저장된 `tool_names` 로 떨어진다. 못 물어본 것은 오류가 아니라 불편이라 200 으로 답한다.
    `truncated` 가 true 면 응답 상한에 걸려 뒤쪽 Tool 을 잘라낸 것이라 목록이 전부가 아니다."""

    model_config = ConfigDict(extra="forbid")

    tools: list[HubToolOut]
    listed: bool
    reason: str | None = None
    truncated: bool = False


class CatalogEnvOut(CatalogEnv):
    model_config = REQUIRED_DEFAULTS


class CatalogHeaderOut(CatalogHeader):
    model_config = REQUIRED_DEFAULTS


class CatalogImageOut(CatalogImage):
    model_config = REQUIRED_DEFAULTS


class HubCatalogServerOut(CatalogServer):
    """카탈로그 서버 하나 (P55 4절). mirrored_image 는 허브 레지스트리로 옮긴 참조이고 허브 이미지
    설정 전에는 null 이다. needs_login 과 needs_credentials 는 원격에 무엇이 필요한가이고 (7절),
    import_header 는 자격 증명이 필요한 원격을 가져올 때 entry 항목이 고정 헤더로 싣는 이름이다
    (P86 A3). 이름이 없는데 둘 중 하나가 참이면 가져올 수 없다. url_template 이 참인 원격은 두
    표시가 모두 거짓이어도 catalog 와 함께 만들 수 없다 (4절)."""

    model_config = REQUIRED_DEFAULTS

    image: CatalogImageOut | None = None
    env: list[CatalogEnvOut] = Field(default_factory=list)
    headers: list[CatalogHeaderOut] = Field(default_factory=list)
    mirrored_image: str | None


class HubCatalogResponse(BaseModel):
    """`GET /api/hub/catalog` (P55 4절). 이 플랫폼 판과 함께 실린 스냅숏 전체다. admission 은
    카탈로그 이미지를 등록할 수 있는가(허브 이미지 설정이 있는가)다."""

    model_config = REQUIRED_DEFAULTS

    catalog: str
    release: str
    last_updated: str | None
    license: str
    admission: bool
    servers: list[HubCatalogServerOut]
