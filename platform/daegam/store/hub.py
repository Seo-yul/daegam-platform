"""MCP Hub 항목 (resource-model.md 6절). 게이트웨이가 백엔드로 고를 수 있는 MCP 카탈로그.

조회는 판정 여부를 이름으로 드러낸다. `list_candidates`·`get_unscoped`·`get_many_unscoped` 는
`access` 를 판정하지 않으므로 주체가 있는 경로는 `HubService.visible`·`get_visible` 만 쓴다.

자기 CR 을 둔 항목은 재적용의 한 종류다 (P50 1절). 재적용 칸(`ConsumerAuthState`)은 재적용 단계와
`HubService.apply` 만 `set_fields` 로 쓴다. 문서 전체를 쓰는 저장은 없다 (P68 2절). 쓰는 쪽마다 자기
칸만 쓰므로 그 사이 시작한 다른 저장(정책·툴 목록 갱신)이 낡은 값을 되돌리지 않는다. 사람이 고치는
접근 정책만 `seq` 를 조건으로 쓴다(`save_access`).
문서의 칸은 `HubItemBase` 에 있고, 재적용 칸을 더한 것이 `HubItem` 이다. API 응답 모델은 재적용 칸을
싣지 않으려고 `HubItemBase` 를 상속한다.

뒤 판이 쓴 칸은 읽되 그대로 렌더하지 않는다 (P55 9절). 출처의 모르는 칸은 `HubSource` 가 들고
있어 칸을 쓰는 저장이 지우지 않는다. 최상위의 모르는 칸은 `from_doc` 이 `newer_fields` 에 적는다.
그런 항목을 렌더하거나 접근 정책을 바꾸는 자리는 `hub_item_newer` 로 멈춘다(`services/hub.py`).
뒤 판이 더한 종류(P46 의 `agent`)의 항목은 목록에서 빠지고(`_known`), id 나 이름으로 읽으면 422
`hub_item_newer` 다(`HubItemNewer`). 이 판은 그 항목을 보이지도 바꾸지도 않는다 (P46 A0).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any, ClassVar, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.auth.hub_access import Access
from daegam.errors import ServiceError
from daegam.store.access import boundary_candidates, create_access_indexes
from daegam.store.auth_reapply import AudienceSwitch, ConsumerAuthState
from daegam.store.base import (
    CLAIM_FIELD,
    SEQ_FIELD,
    Document,
    EditClaimStore,
    UtcDatetime,
    fields_of,
    new_id,
    save_by_seq,
    set_fields_guarded,
    set_fields_where,
    utcnow,
)
from daegam.store.tool_rules import ItemToolRule

HUB_ITEMS = "hub_items"

HubKind = Literal["internal", "remote", "entry", "image", "published", "platform"]
HUB_KINDS: frozenset[str] = frozenset(get_args(HubKind))
Transport = Literal["streamable-http", "sse"]
# 항목이 transport 를 적지 않았을 때 쓰는 값
DEFAULT_TRANSPORT: Transport = "streamable-http"

# 모델 칸이 아니어도 뒤 판이 쓴 칸으로 보지 않는 최상위 키 (P55 9절). 저장 쪽 칸(P68 의 `seq`·
# `claim`)과, 읽지 않고 남아 있는 옛 칸(D23 전의 `scope`)이다
STORAGE_KEYS: frozenset[str] = frozenset({"_id", SEQ_FIELD, CLAIM_FIELD})
RETIRED_KEYS: frozenset[str] = frozenset({"scope"})

# image 항목의 Secret env 값을 두는 플랫폼 관리 Secret 이름의 접두어 (P55 5절). 항목마다 하나이고
# 이름은 접두어와 항목 id 라, 항목을 렌더하지 않고도 지울 수 있다
SECRET_ENV_PREFIX = "hub-env-"


def secret_env_name(item_id: str) -> str:
    """항목 하나의 Secret env 값을 두는 Secret 이름 (P55 5절)."""
    return f"{SECRET_ENV_PREFIX}{item_id}"


# entry 항목의 인증 종류 (P86 6.1절). 만들 때 고르고 바꾸지 않는다. `oauth` 는 P86b 가 더한다
OUTSIDE_AUTH_KINDS: frozenset[str] = frozenset({"none", "header"})
# 플랫폼이 마지막으로 헤더를 실어 부른 답 (P86 6.3절). 401·403 은 header_refused 이고 그 밖의
# 실패는 unreachable 이다
CHECK_OK = "ok"
CHECK_REFUSED = "header_refused"
CHECK_UNREACHABLE = "unreachable"


class HubAuthCheck(BaseModel):
    """플랫폼이 헤더를 실어 마지막으로 부른 결과와 그 시각 (P86 6.3절)."""

    model_config = ConfigDict(extra="ignore")

    # 글자 그대로 읽는다. 뒤 판이 더한 답도 읽기를 멈추지 않는다
    outcome: str
    at: UtcDatetime


class HubAuth(BaseModel):
    """entry 항목의 인증 (P86 6.1절). 칸이 없는 항목은 `none` 이고 `none` 은 저장하지 않으므로, 인증
    없는 항목의 문서는 이 칸 전과 같다. 헤더 값은 여기 없다. 값은 플랫폼 네임스페이스의 관리
    Secret(`services/outside_headers.py` `managed_name`)에 있고 이 칸은 `set` 만 안다.

    kind 는 글자 그대로 읽는다. 뒤 판이 더한 종류(P86b 의 `oauth`)를 든 항목은 `HubItem.from_doc` 이
    뒤 판의 칸(`auth.kind`)으로 적어 렌더하지 않는다. 모르는 칸은 받아 들고 있어 이 칸을 쓰는
    저장이 지우지 않는다."""

    model_config = ConfigDict(extra="allow")

    kind: str
    header_name: str | None = None
    set: bool = False
    # 헤더 판. 값을 바꿀 때마다 새로 낸다. 소비자의 표시와 vMCP 파드 템플릿의 어노테이션이 이것을
    # 실어, 새 값이 소비자를 다시 렌더하고 파드를 굴린다 (6.2절). 값에서 짓지 않는다
    rev: str | None = None
    # 값을 바꿀 수 있는 만든 사람 (6.1절, P55 5절)
    created_by: str | None = None
    check: HubAuthCheck | None = None


def _unset(v: Any) -> bool:
    """출처와 항목의 새 칸은 비었으면 저장하지 않는다 (P55 5절). 그 칸을 쓰지 않는 항목의 문서와
    복제본 판이 그 칸 전과 바이트까지 같다."""
    return v is None or v is False


class HubEnvVar(BaseModel):
    """image 항목의 env 하나 (P55 5절). 비밀이 아니고, 그 항목을 볼 수 있는 모든 사람에게 보인다."""

    model_config = ConfigDict(extra="allow")

    name: str
    value: str


class HubSecretEnvVar(BaseModel):
    """image 항목의 Secret env 이름 하나 (P55 5절). 값은 항목마다의 Secret(`secret_env_name`)에만
    있고 이 칸은 값이 있는지(`set`)만 안다. 플랫폼만 쓴다."""

    model_config = ConfigDict(extra="allow")

    name: str
    set: bool = False


class HubCatalogOrigin(BaseModel):
    """항목이 온 카탈로그 서버와 그 스냅숏의 릴리스 (P55 5절). 플랫폼만 쓴다."""

    model_config = ConfigDict(extra="allow")

    server: str
    release: str


class HubAdmission(BaseModel):
    """image 항목을 허브 키가 허용한 기록 (P55 6절). 만들 때 서명을 모아 정한 것이고 플랫폼만 쓴다.
    provenance 는 모은 서명 전체로 정하고(하나라도 verified 면 verified), upstream·key·run 은 쓴
    서명 하나의 것이다. key 는 그 서명을 맞춘 공개 키의 지문(`sha256:<DER 의 sha256>`)이다. 이 판이
    모르는 칸은 들고 있다 (9절)."""

    model_config = ConfigDict(extra="allow")

    digest: str
    provenance: str
    upstream: str
    key: str
    # 서명에 적힌 실행 기록. `by`·`via` 와 job 이면 `url`·`commit`·`ref` 다
    run: dict[str, str] = Field(default_factory=dict)
    # 모은 서명의 수
    signatures: int
    at: UtcDatetime
    by: str


class StdioProbe(BaseModel):
    """stdio 항목의 Tool 목록 점검이 실패하기 시작한 때와 프록시 파드 지우기의 백오프 (P55 5절).
    점검이 다시 답을 받으면 칸이 지워진다. 플랫폼만 쓴다."""

    model_config = ConfigDict(extra="allow")

    failing_since: UtcDatetime
    last_delete: UtcDatetime | None = None
    next_delete: UtcDatetime | None = None
    deletes: int = 0


STDIO_PROBE_FIELD = "stdio_probe"


class HubSource(BaseModel):
    """kind 별 출처.
    remote/entry/published: url·transport, image: image·transport·port, internal: instance_id.
    image 는 컨테이너의 stdio·args·env·Secret env 이름을 더 들 수 있다 (P55 5절). transport 는
    늘 소비자가 말하는 전송이고, stdio 컨테이너는 프록시가 그 전송으로 낸다.

    모르는 칸은 받아 들고 있다 (P55 9절). 뒤 판이 쓴 출처를 읽어도 목록이 멈추지 않고, 출처를 쓰는
    저장이 그 칸을 지우지 않는다. 응답 모델(`HubSourceOut`)은 그 칸을 버린다. 요청은 라우터가 이
    모델을 칸마다 지어 받으므로 모르는 칸이 요청에서 들어오지 않는다."""

    model_config = ConfigDict(extra="allow")

    url: str | None = None
    transport: Transport | None = None
    image: str | None = None
    port: int | None = None  # image: 컨테이너 포트 (mcpPort). 없으면 8080
    instance_id: str | None = None
    mcp_profile_id: str | None = None
    tools_filter: list[str] | None = None  # image: MCPToolConfig.toolsFilter
    # published: 원본 Gateway
    gateway_instance_id: str | None = None
    # image 의 컨테이너 (P55 5절). 비었으면 저장하지 않는다(`_unset`). 앞 판(B1)은 이 칸을 모르는
    # 칸으로 읽어 그 항목을 렌더하지도 다른 테넌트에 열지도 않는다 (9절)
    stdio: bool | None = Field(default=None, exclude_if=_unset)
    args: list[str] | None = Field(default=None, exclude_if=_unset)
    env: list[HubEnvVar] | None = Field(default=None, exclude_if=_unset)
    secret_env: list[HubSecretEnvVar] | None = Field(default=None, exclude_if=_unset)
    # 카탈로그에서 온 항목 (P55 5절). image 는 서명이 그 서버를 이름 댈 때만 둔다. remote·entry 는
    # 주소와 전송이 스냅숏의 것과 같고, 그 서버에 로그인과 주소 템플릿이 필요 없으며, 자격 증명이
    # 필요하면 그 서버의 비밀 헤더 하나를 고정 헤더로 싣는 entry 일 때만 둔다 (P55 7절, P86 A3)
    catalog: HubCatalogOrigin | None = Field(default=None, exclude_if=_unset)
    # 허브 키가 허용한 image 항목 (P55 6절). 허브 이미지 설정이 켜진 뒤 만든 image 항목만 있고, 그
    # 항목의 image 는 digest 로 고정한 참조다
    admission: HubAdmission | None = Field(default=None, exclude_if=_unset)

    @property
    def writable_tmp(self) -> bool:
        """백엔드 컨테이너에 쓸 수 있는 `/tmp` 를 두는가 (P55 5절). stdio 나 카탈로그 항목이다."""
        return bool(self.stdio) or self.catalog is not None

    def secret_names(self, *, only_set: bool = False) -> list[str]:
        return [e.name for e in self.secret_env or [] if e.set or not only_set]


class HubK8s(BaseModel):
    """항목이 소유하거나 가리키는 CR. entry 는 CR 이 없다 (kind None).
    `name` 은 항목 자신의 CR 이름이다. Gateway 그룹의 Entry 래퍼 이름은 인스턴스 쪽에서 정한다."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = None  # MCPServer | MCPRemoteProxy | VirtualMCPServer | None(entry)
    name: str
    namespace: str
    group: str | None = None  # image/internal: 테넌트 그룹. remote/entry: 없음
    tool_config: str | None = None  # image 의 MCPToolConfig 이름


#: 원본이 우리 Mongo 안에 있어 `tool_names` 를 계산할 수 있는 종류 (D49). 나머지(`remote`·`entry`·
#: `image`)는 원본이 남의 서버라 저장된 값을 쓰고 `refresh-tools` 가 채운다. 이 둘에는 refresh-tools
#: 가 422 라, "새로고침하라" 고 안내하면 없는 버튼을 찾게 된다.
DERIVED_KINDS: tuple[str, ...] = ("internal", "published")

# 플랫폼 웹 Tool (P39b 13절). 주인 테넌트도 클러스터도 없고, Platform Settings 와 도구 계약이 정한다
PLATFORM_KIND = "platform"
# 배포된 Gateway 를 게시한 항목의 종류 (hub-gateway.md 6절)
PUBLISHED_KIND = "published"


def is_derived(kind: str | None) -> bool:
    return kind in DERIVED_KINDS


def clusterless(item: HubItemBase) -> bool:
    """어느 클러스터에도 매이지 않은 항목인가. Gateway 의 같은 클러스터 판정이 건너뛴다. 플랫폼
    fetch 는 Gateway 의 클러스터에 복제되고 search 는 플랫폼이 낸다."""
    return item.kind == PLATFORM_KIND


def other_cluster(items: Iterable[HubItemBase], cluster_id: str | None) -> HubItemBase | None:
    """cluster_id 와 다른 클러스터의 항목. cluster_id 가 None 이면 클러스터에 매인 첫 항목의 것을
    기준으로 한다. 클러스터 없는 항목은 보지 않는다. 같으면 None 이다."""
    for item in items:
        if clusterless(item):
            continue
        if cluster_id is None:
            cluster_id = item.cluster_id
        elif item.cluster_id != cluster_id:
            return item
    return None


# 다른 테넌트의 Gateway 가 자기 네임스페이스에 복제본을 두는 종류 (P50 1절)
CLONED_KINDS: tuple[str, ...] = ("image",)
# 전에 복제본을 두던 종류. 새 복제본은 만들지도 다시 렌더하지도 않는다. 남아 있는 복제본은 그 항목이
# entry 항목으로 옮겨 갈 때까지 그대로 쓰이고 스윕이 지운다 (`services/shared_clones.py`)
KEPT_CLONE_KINDS: tuple[str, ...] = ("remote",)


def replicated(item: HubItemBase, tenant: str) -> bool:
    """tenant 의 Gateway 가 그 네임스페이스의 복제본을 부르는 항목인가. 다른 테넌트의 image 와
    플랫폼 fetch(CR 이 있는 플랫폼 항목), 그리고 다른 테넌트의 remote 항목이다. Gateway 배포는
    프록시를 떼지 못하는 remote 항목만 remote 인 채로 넘기므로(`services/hub.py`
    `HubService.deploy_form`), 그 행은 전에 만든 복제본을 계속 부른다."""
    if item.kind == PLATFORM_KIND:
        return item.k8s.kind is not None
    return item.tenant != tenant and item.kind in (*CLONED_KINDS, *KEPT_CLONE_KINDS)


def as_entry[T: HubItemBase](item: T) -> T:
    """remote 항목을 entry 항목으로 본 사본. 같은 URL 이고 CR 이 없으며 전송이 비었으면
    `DEFAULT_TRANSPORT` 다. Gateway 가 그 주소를 자기 Entry 로 직접 부르므로 프록시도 복제본도 쓰지
    않는다. 다른 종류는 그대로 돌려준다."""
    if item.kind != "remote":
        return item
    transport = item.source.transport or DEFAULT_TRANSPORT
    return item.model_copy(
        update={
            "kind": "entry",
            "source": item.source.model_copy(update={"transport": transport}),
            "k8s": item.k8s.model_copy(update={"kind": None}),
        }
    )


# 재적용이 다시 렌더하는 살아 있는 상태 (P50 1절). `creating` 은 적용이 끝나지 않았고 `deleting` 은
# 내리는 중이다
HUB_LIVE_STATES: frozenset[str] = frozenset({"created"})
# 삭제가 시작되지 않은 상태. 적용·도구 새로 고침·접근 정책 저장은 이 상태에서만 쓴다 (P68 2절)
HUB_NOT_DELETING: frozenset[str] = frozenset({"creating", "created"})
# 사람이 고치는 칸 (P68 2절). 나머지는 플랫폼이 칸마다 쓴다
HUB_ACCESS_FIELDS: frozenset[str] = frozenset({"access", "access_updated_by", "access_updated_at"})


class HubItemBase(Document):
    """허브 항목 문서의 칸. 재적용 칸은 `HubItem` 이 더한다."""

    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    kind: HubKind
    source: HubSource = Field(default_factory=HubSource)
    cluster_id: str
    k8s: HubK8s
    tool_names: list[str] = Field(default_factory=list)
    tool_names_at: UtcDatetime | None = None
    # entry 항목의 인증 (P86 6.1절). 없으면 none 이다. 앞 판(P55 B1 부터)은 이 칸을 모르는 칸으로
    # 읽어 그 항목을 렌더하지도 다른 테넌트에 열지도 않는다 (9절)
    auth: HubAuth | None = Field(default=None, exclude_if=lambda v: v is None)
    # image 항목의 Tool 규칙과 그 규칙을 마지막으로 저장할 때의 Tool 목록 (P52 1절).
    # 규칙이 있는 항목은 tools_filter 가 없으면 rules_tool_names 로 toolsFilter 를 렌더한다. 비어
    # 있으면 저장하지 않는다
    tool_rules: dict[str, ItemToolRule] = Field(default_factory=dict, exclude_if=lambda v: not v)
    rules_tool_names: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    status: dict[str, Any] = Field(default_factory=dict)  # 마지막으로 본 CR status 요약
    # 접근 정책 (D23)
    access: Access = Field(default_factory=Access)
    access_updated_by: str | None = None
    access_updated_at: UtcDatetime | None = None
    # 옛 필드. 허브 항목에는 Advanced 편집도 잠금 표도 없어 쓰는 곳이 없고 적용하지도 않는다.
    # 매니페스트 응답이 아직 그대로 돌려주므로(api/hub.py) 지우지 않고 둔다
    overrides: dict[str, Any] = Field(default_factory=dict)
    state: Literal["creating", "created", "deleting"] = "created"
    last_error: str | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    # Secret env 를 든 image 항목만 갖는 칸 (P55 5절). 만든 사람은 그 값을 바꿀 수 있고, 판은 값을
    # 바꿀 때마다 오르며 파드 템플릿의 어노테이션이 되어 백엔드를 다시 띄운다. 값에서 짓지 않는다.
    # 다른 항목에는 없어서 앞 판(B1)이 이 판 뒤에 만든 항목을 모두 막지는 않는다
    created_by: str | None = Field(default=None, exclude_if=_unset)
    secret_env_rev: int | None = Field(default=None, exclude_if=_unset)
    secret_env_updated_by: str | None = Field(default=None, exclude_if=_unset)
    secret_env_updated_at: UtcDatetime | None = Field(default=None, exclude_if=_unset)


class HubItem(HubItemBase, ConsumerAuthState):
    """허브 항목. 자기 CR 을 둔 항목은 재적용의 한 종류라 재적용 칸을 함께 갖는다."""

    # image 항목의 MCPServer 가 테넌트 audience 를 놓고 자기 audience 로 검증하게 된 기록 (P50 9절,
    # G9 2단계). 없으면 테넌트 audience 다. 재적용 단계만 이 칸을 쓴다
    audience_switch: AudienceSwitch | None = None
    # stdio 항목의 점검 (P55 5절). 점검이 실패하는 동안만 있다. 점검 단계만 쓴다
    stdio_probe: StdioProbe | None = Field(default=None, exclude_if=_unset)
    # 저장된 문서에서 이 판이 모르는 칸 (P55 9절). `from_doc` 이 채운다. 모델 칸이 아니라 응답과
    # 저장에 나가지 않는다
    _newer_fields: tuple[str, ...] = PrivateAttr(default=())

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Self:
        item = super().from_doc(doc)
        newer = unknown_keys(doc, ITEM_KEYS)
        if item.auth is not None and item.auth.kind not in OUTSIDE_AUTH_KINDS:
            # 뒤 판의 인증 종류다 (P86b). 그 칸 없이 렌더하면 인증 없는 Entry 가 된다
            newer = (*newer, "auth.kind")
        item._newer_fields = newer
        return item

    @property
    def header_auth(self) -> HubAuth | None:
        """고정 헤더를 싣는 entry 항목의 인증 (P86 6.1절). 그 밖은 None 이다."""
        if self.kind == "entry" and self.auth is not None and self.auth.kind == "header":
            return self.auth
        return None

    @property
    def newer_fields(self) -> tuple[str, ...]:
        """뒤 판이 쓴 칸. 비어 있지 않으면 이 판은 항목을 렌더하지도 다른 테넌트에 열지도 않는다
        (P55 9절). 출처의 칸은 `source.<칸>` 이다."""
        return self._newer_fields

    @property
    def last_spec_change(self) -> datetime:
        """재적용의 막힘(`AuthBlocked.spec_change`)이 맞대는 값. 인스턴스의 `last_spec_change` 와
        같은 자리이고 항목에서는 마지막 수정 시각이다. 재적용 자신은 이 값을 바꾸지 않는다."""
        return self.updated_at

    @property
    def server_id(self) -> str | None:
        """이 항목으로 가는 서비스 토큰이 여는 우리 서버의 id (P50 9절). internal 은 그 MCP Server
        문서, image 는 항목 자신이다. 우리 MCPServer 가 아닌 종류는 None 이다."""
        if self.kind == "internal":
            return self.source.instance_id
        return self.id if self.kind == "image" else None


# 항목 문서에서 이 판이 아는 최상위 키
ITEM_KEYS: frozenset[str] = frozenset(HubItem.model_fields) | STORAGE_KEYS | RETIRED_KEYS


def unknown_keys(doc: Mapping[str, Any], known: Collection[str]) -> tuple[str, ...]:
    """doc 가운데 이 판이 모르는 키 (P55 9절). 최상위는 known 밖의 키이고, 출처(`source`)는
    `HubSource` 칸 밖의 키를 `source.<키>` 로 적는다. 항목 문서와 복제본 기록의 원본 정의가 함께
    쓴다."""
    out = [k for k in doc if k not in known]
    source = doc.get("source")
    if isinstance(source, Mapping):
        out += [f"source.{k}" for k in source if k not in HubSource.model_fields]
    return tuple(sorted(out))


def _known(docs: list[dict[str, Any]]) -> list[HubItem]:
    """모르는 종류(뒤 판이 더한 것)의 문서는 빼고 읽는다. 앞 판으로 되돌려도 목록이 500 이 되지
    않는다."""
    return [HubItem.from_doc(d) for d in docs if d.get("kind") in HUB_KINDS]


class HubItemNewer(ServiceError):
    """이 판이 모르는 종류(뒤 판이 더한 것, P46 의 `agent`)의 항목을 id 나 이름으로 읽었다 (P46
    A0). 앱의 전역 처리기가 422 `hub_item_newer` 로 답하므로 그 항목의 상세, 삭제, 접근 정책,
    Tool, 매니페스트와 그 이름을 쓰는 생성·게시가 모두 멈춘다. 칸이 아니라 종류라 `fields` 는
    `kind` 하나다.

    판정의 면(`HasAccess` 의 `tenant`·`access`)을 들고 있다. 주체가 있는 읽기
    (`HubService.get_visible`)는 view 를 먼저 보고, 볼 수 없는 사람에게는 이 오류 대신 404 로
    답한다. 정책을 이 판이 읽지 못하면 주인 테넌트만 보는 빈 정책이다. 이름으로 읽은 것(by_name)은
    부른 사람이 자기 테넌트의 이름을 댄 것이라 이름이 겹친다고만 말하고 항목 id 를 싣지 않는다."""

    def __init__(self, doc: Mapping[str, Any], *, by_name: bool = False):
        name, kind = doc.get("name"), doc.get("kind")
        self.tenant: str = str(doc.get("tenant") or "")
        try:
            self.access = Access.model_validate(doc.get("access") or {})
        except ValueError:
            self.access = Access()
        if by_name:
            super().__init__(
                422,
                "hub_item_newer",
                f"hub item {name!r} already exists with a kind this platform version does not "
                "know; choose another name or upgrade the platform",
                fields=["kind"],
            )
            return
        super().__init__(
            422,
            "hub_item_newer",
            f"hub item {name} is of kind {kind!r}, which this platform version does not know; "
            "upgrade the platform to use it",
            hub_item_id=doc.get("_id"),
            fields=["kind"],
        )


def _one(doc: dict[str, Any] | None, *, by_name: bool = False) -> HubItem | None:
    """문서 하나를 읽는다. 모르는 종류면 `HubItemNewer` 다. 앞 판으로 되돌려도 500 이 아니라 그
    항목이 뒤 판의 것이라고 답한다."""
    if doc is None:
        return None
    if doc.get("kind") not in HUB_KINDS:
        raise HubItemNewer(doc, by_name=by_name)
    return HubItem.from_doc(doc)


class MongoHubStore(EditClaimStore):
    """허브 항목. 편집 클레임(`EditClaimStore`)은 Secret env 값 바꾸기만 잡는다 (P55 5절). 클레임이
    사는 동안 접근 정책 저장과 삭제의 시작이 맞지 않고, 삭제가 시작된 항목에는 클레임을 잡지 않는다.
    그래서 삭제보다 먼저 읽은 바꾸기도 지운 Secret 을 다시 만들지 못한다."""

    claim_conditions: ClassVar[dict[str, Any]] = {"expect_state": HUB_NOT_DELETING}

    def __init__(self, db: AsyncDatabase):
        self.coll = db[HUB_ITEMS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )
        await self.coll.create_index([("source.instance_id", ASCENDING)], name="ix_instance")
        # 게시된 Gateway 의 렌더가 재적용 틱마다 자기 게시 항목을 찾는다 (P57 6.2절)
        await self.coll.create_index(
            [("source.gateway_instance_id", ASCENDING)], name="ix_gateway_instance"
        )
        await self.coll.create_index([("cluster_id", ASCENDING)], name="ix_cluster")
        await create_access_indexes(self.coll)

    async def create(self, item: HubItem) -> HubItem:
        await self.coll.insert_one(item.to_doc())
        return item

    async def get_unscoped(self, item_id: str) -> HubItem | None:
        """판정 없는 조회. 주체가 있는 경로는 HubService.get_visible 을 쓴다. 뒤 판이 더한 종류의
        항목이면 `HubItemNewer` 다."""
        return _one(await self.coll.find_one({"_id": item_id}))

    async def get_many_unscoped(self, ids: list[str]) -> dict[str, HubItem]:
        """판정 없는 일괄 조회 (순환 검사·상태 경고)."""
        if not ids:
            return {}
        cursor = self.coll.find({"_id": {"$in": ids}})
        return {i.id: i for i in _known(await cursor.to_list())}

    async def existing_ids(self, ids: list[str]) -> set[str]:
        """ids 가운데 문서가 있는 것. 종류를 거르지 않으므로 뒤 판이 더한 종류의 항목도 있는 것으로
        센다. 관리 Secret 의 정리가 쓴다 (P86 6.1절)."""
        if not ids:
            return set()
        cursor = self.coll.find({"_id": {"$in": ids}}, {"_id": 1})
        return {d["_id"] for d in await cursor.to_list()}

    async def get_by_name(self, tenant: str, name: str) -> HubItem | None:
        """이름 하나의 항목. 그 이름을 뒤 판이 더한 종류의 항목이 쥐었으면 `HubItemNewer` 라, 같은
        이름으로 만들거나 게시하는 요청도 422 다. 그 답은 항목 id 를 싣지 않는다."""
        return _one(await self.coll.find_one({"tenant": tenant, "name": name}), by_name=True)

    async def get_platform(self, name: str) -> HubItem | None:
        """플랫폼 항목(`web-fetch`·`web-search`) 하나."""
        return _one(await self.coll.find_one({"kind": PLATFORM_KIND, "name": name}))

    async def get_by_instance(self, instance_id: str) -> HubItem | None:
        return _one(await self.coll.find_one({"source.instance_id": instance_id}))

    async def by_instances(self, instance_ids: list[str]) -> dict[str, HubItem]:
        """MCP Server id 로 게시 항목을 찾는다. 목록 응답이 한 번에 읽는다 (N+1 회피)."""
        if not instance_ids:
            return {}
        cursor = self.coll.find({"kind": "internal", "source.instance_id": {"$in": instance_ids}})
        return {d["source"]["instance_id"]: HubItem.from_doc(d) for d in await cursor.to_list()}

    async def published_of_instance(self, gateway_instance_id: str) -> list[HubItem]:
        cursor = self.coll.find(
            {"kind": "published", "source.gateway_instance_id": gateway_instance_id}
        )
        return _known(await cursor.to_list())

    async def owning_cr_on_cluster(self, cluster_id: str) -> list[HubItem]:
        """그 클러스터에서 `k8s.kind` 가 있는 remote·image·published 항목. internal 의 CR 은 MCP
        Server 것이고 entry 는 CR 이 없다. published 는 proxy 모드(MCPRemoteProxy)뿐 아니라 entry
        모드도 든다. entry 모드의 `k8s` 는 그 Gateway 의 VirtualMCPServer 를 가리킨다. 감사
        수집기가 프록시 파드를 테넌트에 잇는 데 쓰고(Gateway 의 이름이 먼저 들어가 entry 모드 항목은
        그 이름을 바꾸지 않는다), 재적용은 자기 CR 을 둔 항목만 거른다(`services/hub.py`
        `owns_crs`)."""
        cursor = self.coll.find(
            {
                "cluster_id": cluster_id,
                "kind": {"$in": ["remote", "image", "published"]},
                "k8s.kind": {"$ne": None},
            }
        )
        return [HubItem.from_doc(d) for d in await cursor.to_list()]

    async def list_candidates(self, tenant: str) -> list[HubItem]:
        """tenant 에 보일 수 있는 후보: 자기 테넌트 항목, access 경계에 tenant 나 `*` 가 있는 항목,
        주인 없는 플랫폼 항목. 플랫폼 항목은 경계 밖에서도 platform-admin 에게 보이므로 늘 후보다.
        view 판정은 HubService.visible 이 한다."""
        platform = {"kind": PLATFORM_KIND}
        query = {"$or": [*boundary_candidates(tenant), platform]}
        cursor = self.coll.find(query).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return _known(await cursor.to_list())

    async def digest_pinned_images(self) -> list[HubItem]:
        """image 참조가 `@sha256:<hex>` 로 끝나는 항목 (P55 6절, 거부한 digest 의 쓰임). 거부 목록은
        부르는 쪽이 맞춘다. 목록 전체를 정규식 하나에 넣으면 수백 개에서 Mongo 의 정규식 상한을
        넘는다."""
        query = {"kind": "image", "source.image": {"$regex": "@sha256:[0-9a-f]{64}$"}}
        cursor = self.coll.find(query).sort([("tenant", ASCENDING), ("name", ASCENDING)])
        return [HubItem.from_doc(d) for d in await cursor.to_list()]

    async def save_access(self, item: HubItem) -> bool:
        """접근 정책 저장 (P68 2절). 서버가 읽은 `seq` 이고 삭제가 시작되지 않았을 때만 정책 칸만
        쓰고 `seq` 를 올린다. 맞지 않으면 False 다."""
        return await save_by_seq(
            self.coll, item, fields_of(item, HUB_ACCESS_FIELDS), expect_state=HUB_NOT_DELETING
        )

    async def set_fields(
        self,
        item_id: str,
        fields: dict[str, Any],
        *,
        expect_state: Collection[str] | None = None,
    ) -> bool:
        """fields 만 조건부로 `$set` 한다(`set_fields_where`). 문서 전체를 쓰는 저장은 없다. 쓰는
        쪽마다 자기 칸만 쓴다 (P68 2절)."""
        return await set_fields_where(self.coll, item_id, fields, expect_state=expect_state)

    async def start_delete(self, item: HubItem) -> bool:
        """삭제의 시작. 삭제가 시작되지 않았고 살아 있는 편집 클레임이 없을 때만
        `state`·`updated_at` 을 쓴다 (P68 2절, P55 5절). 맞지 않으면 False 다."""
        return await set_fields_guarded(
            self.coll,
            item.id,
            fields_of(item, {"state", "updated_at"}),
            expect_state=HUB_NOT_DELETING,
            unclaimed=True,
        )

    async def stdio_items(self) -> list[HubItem]:
        """만들어진 stdio image 항목 (P55 5절). 점검 단계가 쓴다."""
        cursor = self.coll.find({"kind": "image", "source.stdio": True, "state": "created"})
        return _known(await cursor.to_list())

    async def note_stdio_failing(self, item_id: str, probe: StdioProbe) -> bool:
        """점검이 처음 실패한 때를 적는다 (P55 5절). 칸이 없을 때만 쓴다."""
        return await set_fields_guarded(
            self.coll,
            item_id,
            {STDIO_PROBE_FIELD: probe.model_dump(mode="python")},
            expect_state=HUB_LIVE_STATES,
            also={STDIO_PROBE_FIELD: None},
        )

    async def claim_stdio_delete(
        self, item_id: str, expect_next: datetime | None, probe: StdioProbe
    ) -> bool:
        """프록시 파드 지우기를 잡는다 (P55 5절). 읽은 다음 허용 시각이 그대로일 때만 새 백오프를
        쓰므로 두 플랫폼 레플리카가 같은 파드를 두 번 지우지 않는다. 칸이 없으면 그 시각은 None
        이다."""
        return await set_fields_guarded(
            self.coll,
            item_id,
            {STDIO_PROBE_FIELD: probe.model_dump(mode="python")},
            expect_state=HUB_LIVE_STATES,
            also={f"{STDIO_PROBE_FIELD}.next_delete": expect_next},
        )

    async def clear_stdio_probe(self, item_id: str) -> None:
        """점검이 다시 답을 받았다. 칸을 지운다."""
        await self.coll.update_one({"_id": item_id}, {"$unset": {STDIO_PROBE_FIELD: ""}})

    async def delete(self, item_id: str) -> bool:
        res = await self.coll.delete_one({"_id": item_id})
        return res.deleted_count == 1
