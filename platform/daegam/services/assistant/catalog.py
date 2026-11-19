"""비서 도구 표 (D31, P14 F1a).

표 한 줄이 플랫폼 API 동작 하나를 도구로 만든다. 이름·위험 등급·한 줄 설명·비밀 필드(JSON pointer)·
리소스 종류만 여기 적고, 인자 스키마는 `schema.py` 가 기동 시 받은 OpenAPI dict 에서 뽑는다. 본문을
raw 로 읽어 OpenAPI 에 requestBody 가 없는 동작은 `args_schema` 에 본문 스키마를 손으로 적는다(빈
properties 면 본문이 없다는 뜻이다). multipart 인 Backend API 생성도 손 스키마다.

표에 없는 동작은 도구가 아니다. 그 이유는 `EXCLUDED` 에 (method, path) 마다 적고, 테스트가
OpenAPI 의 모든 동작이 표 또는 제외 목록 중 정확히 하나에 있음을 확인한다. 비서 자신의 API
(`/api/assistant/` 아래)는 재귀이므로 접두사로 통째로 제외한다.

경로는 OpenAPI 문서의 키 그대로다(`/api` 접두사 포함). `daegam.urls.API_PREFIX` 를 읽지 않는 것은
services 가 api 계층을 import 하지 않게 하려는 것이다(D30). 어긋나면 기동 자기 점검
(`CatalogError`)이 잡는다.

클라이언트 전용 도구 넷(`navigate`·`read_guide`·`ask_user`·`fetch_url`)은 API 를 부르지 않으므로
method/path 없이 `local=True` 로 표에 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from daegam.store.agents import NODE_TYPES, EdgeKind

Risk = Literal["read", "write", "deploy", "destructive"]

# 표에 적을 수 있는 도구 이름. `<동사>_<리소스>` 꼴이라 소문자로 시작한다. MCP 프로필이 받는
# 도구 이름 규칙(`services/toolnames.TOOL_NAME_RE`)은 이것과 다른 규칙이고 대소문자를 허용한다
CATALOG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# resource 이벤트의 kind 와 UI 상세 경로. url 은 `/{복수형}/{id}` 다
RESOURCE_ROUTES: dict[str, str] = {
    "backend-api": "/backend-apis",
    "mcp-server": "/mcp-servers",
    "agent": "/agents",
    "data-service": "/data-services",
    "gateway": "/gateways",
    "hub": "/hub",
    "knowledge-base": "/knowledge-bases",
    "skill": "/skills",
}


class CatalogError(RuntimeError):
    """표 자체가 어긋났거나(이름 규칙·중복·제외 목록과 겹침) 표의 동작이 OpenAPI 에 없다.
    기동 자기 점검에서 올라온다."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    method: str | None
    path: str | None
    risk: Risk
    description: str
    secret_fields: tuple[str, ...] = ()
    args_schema: dict[str, Any] | None = None
    resource_kind: str | None = None
    local: bool = False
    # 도구 인자에서 빼는 본문 필드. 비서가 보내지 않으므로 PATCH 는 그 절을 그대로 둔다. 도구 정의는
    # 모델 호출마다 토큰으로 들어가므로 사람이 화면에서 정하는 큰 절은 싣지 않는다(D57)
    hidden_fields: tuple[str, ...] = ()
    # 제안 도구 (P48 6.1절). 등급과 상관없이 늘 사람의 결정을 기다리고(`loop.needs_confirm`), 한
    # 메시지에서 홀로 기다리며, 루프가 검사한 뒤에만 카드로 간다(`proposals.py`)
    proposal: bool = False
    # 클라이언트 전용 도구의 묶음. 없으면 기본 묶음이다. 플랫폼을 읽되 표의 동작 하나가 아닌 도구
    # (get_agent_definition)가 제 묶음에 든다
    group: str | None = None

    @property
    def key(self) -> tuple[str, str] | None:
        """OpenAPI 의 (METHOD, path). 클라이언트 전용 도구는 None."""
        if self.local or self.method is None or self.path is None:
            return None
        return (self.method.upper(), self.path)


def _obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _str(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


NO_BODY: dict[str, Any] = _obj({})

# verify·convert 는 multipart 또는 json {text, filename?} 을 받는다. 실행기는 json 으로 보낸다
SPEC_TEXT_BODY = _obj(
    {
        "text": _str(
            "스펙 원문(OpenAPI JSON·YAML 또는 WSDL). fetch_url 로 가져왔으면 그 결과의 ref 값"
            "(`$file:<이름>`)을 그대로 넣는다. 그러면 서버에 있는 본문 전체가 들어간다"
        ),
        "filename": _str("원본 파일 이름. 확장자가 형식 판별의 힌트다. 예: petstore.yaml"),
    },
    ("text",),
)

# Backend API 생성(multipart). 실행기가 spec_text 를 file 파트로, 나머지를 Form 으로 보낸다
BACKEND_PROFILE_CREATE = _obj(
    {
        "name": _str("K8s 이름 규칙의 이름. 같은 이름이 있으면 새 버전을 더한다"),
        "display_name": _str("표시 이름. 없으면 name"),
        "endpoint": _str("백엔드 서비스 URL"),
        "auth": _str("백엔드 인증 방식", enum=["none", "basic", "wsse"]),
        "credential_secret": _str("자격증명이 든 K8s Secret 이름. 값이 아니라 이름이다"),
        "spec_text": _str(
            "스펙 원문(OpenAPI JSON·YAML 또는 WSDL). fetch_url 로 가져왔으면 그 결과의 ref 값"
            "(`$file:<이름>`)을 그대로 넣는다. 그러면 서버에 있는 본문 전체가 들어가며, 이 호출은"
            " 사용자 승인 카드를 거친다"
        ),
        "filename": _str("원본 파일 이름. 확장자가 형식 판별의 힌트다. 예: petstore.yaml"),
    },
    ("name", "spec_text", "filename"),
)

NAVIGATE_ARGS = _obj({"to": _str("앱 안 경로. 예: /mcp-servers/<id>, /data-services/new")}, ("to",))
READ_GUIDE_ARGS = _obj({"topic": _str("안내문 주제 이름")}, ("topic",))
ASK_USER_ARGS = _obj(
    {
        "question": _str("사용자에게 묻는 질문"),
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "선택지. 있으면 화면이 버튼으로 그린다",
        },
        "field": _str("답이 채울 인자 이름"),
    },
    ("question",),
)
FETCH_URL_ARGS = _obj({"url": _str("공개 http(s) URL. 사설·클러스터 주소는 거부된다")}, ("url",))
AGENT_DEFINITION_ARGS = _obj(
    {"agent_id": {"type": "string"}, "node_id": {"type": "string"}}, ("agent_id",)
)

# Agent 바꾸기 제안의 인자 (P48 6.2절). 정의 전체가 아니라 저장된 리비전 위의 패치이고 플랫폼이 짜
# 맞춘다. 이름은 `agent_changes.PATCH_FIELDS` 이고 그 까닭도 거기 있다
_EDGE_REF = _obj(
    {
        "source": {"type": "string"},
        "target": {"type": "string"},
        "kind": {"type": "string", "enum": list(get_args(EdgeKind))},
    },
    ("source", "target", "kind"),
)
AGENT_PATCH = _obj(
    {
        "set_nodes": {
            "type": "array",
            "items": _obj(
                {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": list(NODE_TYPES)},
                    "data": {"type": "object"},
                },
                ("id", "type", "data"),
            ),
            "description": "더하거나 id 로 통째로 바꿀 노드",
        },
        "remove_nodes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "지울 노드 id. 그 간선도 빠진다",
        },
        "add_edges": {"type": "array", "items": _EDGE_REF},
        "remove_edges": {"type": "array", "items": _EDGE_REF},
        "set_service": {"type": "object", "description": "통째로 바꿀 service 칸"},
        "set_display_name": {"type": "string"},
    }
)

# API 경로 앞머리. 도구 표·제외 표·묶음 앞머리가 함께 쓴다
BP = "/api/backend-apis"
CL = "/api/clusters"
DS = "/api/data-services"
MS = "/api/mcp-servers"
HUB = "/api/hub"
GW = "/api/gateways"
AG = "/api/agents"
KB = "/api/knowledge-bases"
SK = "/api/skills"

# 도구 묶음 (D57). 모델에게는 기본 묶음(core)만 먼저 싣고, 나머지는 `use_tools` 로 연다. 도구 정의
# 80개를 호출마다 통째로 보내면 입력의 대부분이 매번 같은 설명서가 되기 때문이다. 묶음은 API 경로의
# 앞머리로 정해지므로(`group_of`) 표에 도구를 더해도 따로 적을 것이 없다. 설명은 시스템 프롬프트와
# `use_tools` 의 인자 설명에 그대로 쓰인다
CORE_GROUP = "core"
# 어느 앞머리에도 맞지 않는 도구. 늘 싣는다. 실제 표에는 없어야 한다(테스트가 지킨다)
OTHER_GROUP = "other"
TOOL_GROUPS: dict[str, str] = {
    "backend": "Backend API 등록·스펙 검증·버전·읽기 토큰",
    "cluster": "클러스터 목록·점검·노출 옵션",
    "data": "Database(Postgres·Redis) 등록·연결 확인",
    "mcp": "MCP Server 만들기·배포·허브 게시",
    "hub": "MCP Hub 항목(외부 MCP 등록·Tool 새로고침·접근 정책)",
    "gateway": "Gateway 만들기·배포·게시",
    "agent": "Agent 템플릿·그래프 검증·만들기·배포",
    "knowledge": "Knowledge Base 와 문서",
    "skill": "Skill 목록과 상세(읽기만)",
    "settings": "Platform·Tenant Settings, LLM provider 확인, 내 테넌트",
    "usage": "사용량·비용과 가격표 조회",
}
# 경로 앞머리 → 묶음
_GROUP_PREFIXES: tuple[tuple[str, str], ...] = (
    (BP, "backend"),
    (CL, "cluster"),
    (DS, "data"),
    (MS, "mcp"),
    (HUB, "hub"),
    (GW, "gateway"),
    (AG, "agent"),
    (KB, "knowledge"),
    (SK, "skill"),
    # 원장은 제어면의 일이라 묶음 하나다 (P37 14절). 가격표는 /api/settings 아래라 그 앞에 둔다
    # (`group_of` 는 처음 맞는 것을 고른다)
    ("/api/usage", "usage"),
    ("/api/admin/usage", "usage"),
    ("/api/settings/pricing", "usage"),
    ("/api/settings", "settings"),
    ("/api/tenants", "settings"),
)
USE_TOOLS_ARGS = _obj(
    {
        "groups": {
            "type": "array",
            "items": {"type": "string", "enum": list(TOOL_GROUPS)},
            "minItems": 1,
            "description": "열 묶음. "
            + " / ".join(f"{name}: {desc}" for name, desc in TOOL_GROUPS.items()),
        }
    },
    ("groups",),
)


def _t(
    name: str,
    method: str,
    path: str,
    risk: Risk,
    description: str,
    *,
    secret_fields: tuple[str, ...] = (),
    args_schema: dict[str, Any] | None = None,
    kind: str | None = None,
    hidden_fields: tuple[str, ...] = (),
    proposal: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        method=method,
        path=path,
        risk=risk,
        description=description,
        secret_fields=secret_fields,
        args_schema=args_schema,
        resource_kind=kind,
        hidden_fields=hidden_fields,
        proposal=proposal,
    )


def _local(
    name: str, description: str, args_schema: dict[str, Any], *, group: str | None = None
) -> ToolSpec:
    return ToolSpec(
        name=name,
        method=None,
        path=None,
        risk="read",
        description=description,
        args_schema=args_schema,
        local=True,
        group=group,
    )


# 배포 도구 셋(deploy_mcp_server·deploy_gateway·deploy_agent)이 설명 뒤에 붙이는 규칙
# 비서가 고치지 않는 Tool 규칙 (P52 1절). 누가 부를지는 사람이 화면에서 정한다. 보내지 않은 규칙은
# 저장된 것이 그대로 남으므로 비서의 정의 저장이 규칙을 지우지 않는다
_RULE_FIELDS = ("backends.tool_rules",)
# 비서가 아직 켜지 않는 Gateway 의 code mode (P56 6.2절). 화면이 켜는 A3 까지 감춘다. 보내지 않은
# 칸은 저장된 값이 그대로 남는다(`api/gateways.py` `_spec`)
_GATEWAY_HIDDEN = (*_RULE_FIELDS, "code_mode")
# 배포 게이트를 건너뛰는 본문 칸 (P45 12절). 비서는 게이트를 건너뛰지 않으므로, 새 내용은 지난 평가
# 실행이 있어야 비서의 배포 도구로 배포된다
SKIP_EVALUATION = "skip_evaluation"

# 승인 카드는 첫 문장만 보이므로(loop.py approval_summary) 이 안내는 모델만 읽는다. 비운 칸은 테넌트
# 기본값이다
_DEPLOY_HINT = "exposure·namespace·replicas 는 사용자가 요청했을 때만 넣는다."

TOOLS: tuple[ToolSpec, ...] = (
    # -- Backend API ------------------------------------------------------------------
    _t("list_backend_apis", "GET", BP, "read", "Backend API 목록.", kind="backend-api"),
    _t(
        "get_backend_api",
        "GET",
        f"{BP}/{{api_id}}",
        "read",
        "Backend API 상세와 최신 버전.",
        kind="backend-api",
    ),
    _t(
        "create_backend_api",
        "POST",
        BP,
        "write",
        "스펙 원문으로 Backend API 를 만들거나 새 버전을 더한다.",
        args_schema=BACKEND_PROFILE_CREATE,
        kind="backend-api",
    ),
    _t(
        "update_backend_api",
        "PATCH",
        f"{BP}/{{api_id}}",
        "write",
        "Backend API 의 접속 정보를 고친다.",
        kind="backend-api",
        # soap:header 값의 Secret(T60)은 드물게 쓰는 칸이고 도구 정의의 크기 상한을 넘기므로 비서
        # 도구에서 뺀다. API 로는 정할 수 있다
        hidden_fields=("soap_headers_secret",),
    ),
    _t(
        "delete_backend_api",
        "DELETE",
        f"{BP}/{{api_id}}",
        "destructive",
        "Backend API 를 지운다.",
        kind="backend-api",
    ),
    _t(
        "verify_spec",
        "POST",
        f"{BP}/verify",
        "read",
        "스펙 원문을 저장하지 않고 검증해 판정과 Tool 이름을 본다.",
        args_schema=SPEC_TEXT_BODY,
    ),
    _t("list_backend_versions", "GET", f"{BP}/{{api_id}}/versions", "read", "버전 목록."),
    _t(
        "get_backend_version",
        "GET",
        f"{BP}/{{api_id}}/versions/{{version}}",
        "read",
        "버전 상세(검증 보고서·Tool 이름).",
    ),
    _t(
        "list_backend_tokens",
        "GET",
        f"{BP}/{{api_id}}/tokens",
        "read",
        "스펙 읽기 토큰 목록(메타데이터만).",
    ),
    _t(
        "revoke_backend_token",
        "DELETE",
        f"{BP}/{{api_id}}/tokens/{{token_id}}",
        "destructive",
        "스펙 읽기 토큰을 폐기한다.",
    ),
    # -- Cluster (읽기와 점검만. 등록·수정·삭제는 제외 목록) ----------------------------------
    _t("list_clusters", "GET", CL, "read", "배포 대상 클러스터 목록."),
    _t("get_cluster", "GET", f"{CL}/{{cluster_id}}", "read", "클러스터 상세와 preflight 결과."),
    _t(
        "preflight_cluster",
        "POST",
        f"{CL}/{{cluster_id}}/preflight",
        "read",
        "클러스터 연결·권한·오퍼레이터를 점검한다.",
        args_schema=NO_BODY,
    ),
    _t(
        "cluster_exposure_options",
        "GET",
        f"{CL}/{{cluster_id}}/exposure-options",
        "read",
        "외부 노출에 쓸 수 있는 네임스페이스·IngressClass·TLS Secret·기본값.",
    ),
    # -- Database --------------------------------------------------------------------------
    _t(
        "list_data_services",
        "GET",
        DS,
        "read",
        "Database(PostgreSQL·Redis) 목록.",
        kind="data-service",
    ),
    _t("get_data_service", "GET", f"{DS}/{{ds_id}}", "read", "Database 상세.", kind="data-service"),
    _t(
        "create_data_service",
        "POST",
        DS,
        "write",
        "Database 를 등록한다. 비밀번호는 채팅으로 받지 않으므로 비워 두고 화면에서 넣게 안내한다.",
        secret_fields=("/password",),
        kind="data-service",
    ),
    _t(
        "update_data_service",
        "PATCH",
        f"{DS}/{{ds_id}}",
        "write",
        "Database 접속 정보를 고친다. 비밀번호는 채팅으로 받지 않는다.",
        secret_fields=("/password",),
        kind="data-service",
    ),
    _t(
        "delete_data_service",
        "DELETE",
        f"{DS}/{{ds_id}}",
        "destructive",
        "Database 등록을 지운다.",
        kind="data-service",
    ),
    _t(
        "verify_data_service",
        "POST",
        f"{DS}/{{ds_id}}/verify",
        "read",
        "Database 접속을 확인한다.",
        args_schema=NO_BODY,
        kind="data-service",
    ),
    _t(
        "list_data_service_plain_consumers",
        "GET",
        f"{DS}/{{ds_id}}/plain-consumers",
        "read",
        "TLS 끝점이 있는 Redis Database 에 아직 평문 포트로 접속하는 Agent·MCP Server 목록.",
        kind="data-service",
    ),
    # -- MCP Server (D42: 정의와 배포가 한 리소스다) --------------------------------------------
    _t(
        "list_mcp_servers",
        "GET",
        MS,
        "read",
        "MCP Server 목록과 상태. state 가 draft 면 저장만 된 것이고 deployed 면 떠 있는 것이다.",
        kind="mcp-server",
    ),
    _t(
        "get_mcp_server",
        "GET",
        f"{MS}/{{server_id}}",
        "read",
        "MCP Server 상세와 상태. revision 은 저장된 리비전, deployed_revision 은 배포된 것이다.",
        kind="mcp-server",
    ),
    _t(
        "create_mcp_server",
        "POST",
        MS,
        "write",
        "Backend API 의 Tool 을 선택해 MCP Server 를 만든다(저장만). 배포는 deploy_mcp_server 다.",
        kind="mcp-server",
        hidden_fields=_RULE_FIELDS,
    ),
    _t(
        "update_mcp_server",
        "PUT",
        f"{MS}/{{server_id}}",
        "write",
        "MCP Server 의 정의를 고쳐 새 리비전을 만든다. 배포된 것은 redeploy 해야 반영된다.",
        kind="mcp-server",
        hidden_fields=_RULE_FIELDS,
    ),
    _t(
        "clone_mcp_server",
        "POST",
        f"{MS}/{{server_id}}/clone",
        "write",
        "MCP Server 의 정의를 베껴 새 draft 를 만든다. exposure 를 주면 배포 설정으로 남는다.",
        kind="mcp-server",
    ),
    _t(
        "deploy_mcp_server",
        "POST",
        f"{MS}/{{server_id}}/deploy",
        "deploy",
        "draft 인 MCP Server 를 클러스터에 배포한다. " + _DEPLOY_HINT,
        kind="mcp-server",
    ),
    _t(
        "patch_mcp_server",
        "PATCH",
        f"{MS}/{{server_id}}",
        "deploy",
        "배포된 MCP Server 의 replicas·resources·image·bindings·노출을 바꾸고 재배포한다.",
        kind="mcp-server",
    ),
    _t(
        "redeploy_mcp_server",
        "POST",
        f"{MS}/{{server_id}}/redeploy",
        "deploy",
        "배포된 MCP Server 를 최신 리비전으로 재배포한다.",
        args_schema=NO_BODY,
        kind="mcp-server",
    ),
    _t(
        "undeploy_mcp_server",
        "POST",
        f"{MS}/{{server_id}}/undeploy",
        "deploy",
        "MCP Server 를 클러스터에서 내리고 정의는 남긴다(draft 로 돌아간다). 게시 중이면 안 된다.",
        args_schema=NO_BODY,
        kind="mcp-server",
    ),
    _t(
        "delete_mcp_server",
        "DELETE",
        f"{MS}/{{server_id}}",
        "destructive",
        "MCP Server 의 정의와 클러스터의 것을 모두 지운다.",
        kind="mcp-server",
    ),
    _t(
        "publish_mcp_server",
        "POST",
        f"{MS}/{{server_id}}/publish",
        "deploy",
        "배포된 MCP Server 를 허브에 게시한다.",
        kind="hub",
    ),
    _t(
        "unpublish_mcp_server",
        "DELETE",
        f"{MS}/{{server_id}}/publish",
        "destructive",
        "허브 게시를 내린다.",
    ),
    # -- Hub -------------------------------------------------------------------------------
    _t("list_hub_items", "GET", HUB, "read", "허브 항목 목록.", kind="hub"),
    _t("get_hub_item", "GET", f"{HUB}/{{item_id}}", "read", "허브 항목 상세.", kind="hub"),
    _t(
        "create_hub_item",
        "POST",
        HUB,
        "deploy",
        "외부 MCP 서버(remote·entry·image)를 허브에 등록한다. remote·entry 는 streamable-http "
        "서버만 받고 sse 는 거절한다. 인증과 컨테이너 입력은 화면에서 넣는다.",
        kind="hub",
        # 헤더 값과 Secret env 값은 비밀이다. 비서는 비밀을 묻지도 받지도 않는다 (D56, P86 6.1절,
        # P55 5절). 컨테이너 입력(stdio·args·env)과 카탈로그 가져오기는 변수마다 카탈로그의 설명을
        # 곁에 둔 화면과 승인(B3)을 거친다 (P55 8절)
        hidden_fields=("auth", "secret_env", "catalog", "stdio", "args", "env"),
    ),
    _t(
        "delete_hub_item",
        "DELETE",
        f"{HUB}/{{item_id}}",
        "destructive",
        "허브 항목을 지운다.",
        kind="hub",
    ),
    _t(
        "refresh_hub_tools",
        "POST",
        f"{HUB}/{{item_id}}/refresh-tools",
        "write",
        "허브 항목의 Tool 목록을 서버에서 다시 읽는다.",
        args_schema=NO_BODY,
        kind="hub",
    ),
    _t(
        "set_hub_access",
        "PUT",
        f"{HUB}/{{item_id}}/access",
        "deploy",
        "허브 항목의 접근 정책을 저장한다. 누가 이 서버를 쓸 수 있는지가 바뀌므로 승인 대상이다.",
        kind="hub",
    ),
    # -- Gateway (D42: 정의와 배포가 한 리소스) ---------------------------------------------
    _t("list_gateways", "GET", GW, "read", "Gateway 목록과 상태. draft 도 든다.", kind="gateway"),
    _t(
        "get_gateway",
        "GET",
        f"{GW}/{{gateway_id}}",
        "read",
        "Gateway 상세와 상태.",
        kind="gateway",
    ),
    _t(
        "create_gateway",
        "POST",
        GW,
        "write",
        "허브 항목을 묶어 Gateway 를 draft 로 만든다. 배포는 deploy_gateway 다.",
        kind="gateway",
        hidden_fields=_GATEWAY_HIDDEN,
    ),
    _t(
        "update_gateway",
        "PUT",
        f"{GW}/{{gateway_id}}",
        "write",
        "Gateway 정의를 고쳐 새 리비전을 만든다. 배포 핀은 그대로다.",
        kind="gateway",
        hidden_fields=_GATEWAY_HIDDEN,
    ),
    _t(
        "clone_gateway",
        "POST",
        f"{GW}/{{gateway_id}}/clone",
        "write",
        "Gateway 정의를 베껴 새 draft 를 만든다.",
        kind="gateway",
    ),
    _t(
        "deploy_gateway",
        "POST",
        f"{GW}/{{gateway_id}}/deploy",
        "deploy",
        "draft Gateway 를 클러스터에 vMCP 엔드포인트로 띄운다. " + _DEPLOY_HINT,
        kind="gateway",
    ),
    _t(
        "redeploy_gateway",
        "POST",
        f"{GW}/{{gateway_id}}/redeploy",
        "deploy",
        "배포된 Gateway 를 현재 리비전으로 재배포한다.",
        args_schema=NO_BODY,
        kind="gateway",
    ),
    _t(
        "undeploy_gateway",
        "POST",
        f"{GW}/{{gateway_id}}/undeploy",
        "deploy",
        "Gateway 를 클러스터에서 내리고 정의는 남긴다 (draft 로 돌아간다).",
        args_schema=NO_BODY,
        kind="gateway",
    ),
    _t(
        "update_gateway_exposure",
        "PATCH",
        f"{GW}/{{gateway_id}}",
        "deploy",
        "배포된 Gateway 의 노출을 바꾸고 재배포한다.",
        kind="gateway",
    ),
    _t(
        "delete_gateway",
        "DELETE",
        f"{GW}/{{gateway_id}}",
        "destructive",
        "Gateway 정의와 클러스터의 것을 모두 지운다.",
        kind="gateway",
    ),
    _t(
        "publish_gateway",
        "POST",
        f"{GW}/{{gateway_id}}/publish",
        "deploy",
        "배포된 Gateway 의 vMCP 를 허브에 게시한다.",
        kind="hub",
    ),
    # -- Agent (D42: 정의와 배포가 한 리소스다. 채팅·세션·결정은 제외 목록) ----------------------
    _t("list_agent_templates", "GET", f"{AG}/templates", "read", "Agent 캔버스 템플릿 목록."),
    _t(
        "validate_agent_graph",
        "POST",
        f"{AG}/validate",
        "read",
        "Agent 그래프를 검증한다.",
    ),
    _t(
        "list_agents",
        "GET",
        AG,
        "read",
        "Agent 목록과 상태. state 가 draft 면 저장만 된 것이고 deployed 면 떠 있는 것이다.",
        kind="agent",
    ),
    _t(
        "get_agent",
        "GET",
        f"{AG}/{{agent_id}}",
        "read",
        "Agent 상세와 상태. revision 은 저장된 리비전, deployed_revision 은 배포된 것이다.",
        kind="agent",
    ),
    # 패치할 정의를 읽는다 (P48 6.2절). 4KB 로 자르지 않고 64 KiB 까지 모델에 준다. 개요는 최신
    # 리비전의 것이고 노드 하나는 data 전체다. 루프가 플랫폼 앱을 사람의 신원으로 부른다
    _local(
        "get_agent_definition",
        "Agent 의 저장된 정의를 읽는다. node_id 가 없으면 개요, 있으면 그 노드의 data 전체다.",
        AGENT_DEFINITION_ARGS,
        group="agent",
    ),
    # Agent 정의는 제안이다 (P48 6.1절). 사람이 카드에서 고른 것만 저장되고 배치는 캔버스가 정한다
    _t(
        "create_agent",
        "POST",
        AG,
        "write",
        "새 Agent 정의를 제안한다. 사람이 고르면 draft 로 저장되고 배포는 deploy_agent 다.",
        kind="agent",
        # 배치는 캔버스가 정하고 평가 설정은 화면에서만 정한다(바꾸면 거절된다)
        hidden_fields=("layout", "service.evaluation"),
        proposal=True,
    ),
    _t(
        "update_agent",
        "PUT",
        f"{AG}/{{agent_id}}",
        "write",
        "저장된 Agent 정의를 바꾸자고 제안한다. 사람이 고르면 새 리비전이 되고 배포된 것은 "
        "redeploy 해야 반영된다.",
        args_schema=AGENT_PATCH,
        kind="agent",
        proposal=True,
    ),
    _t(
        "clone_agent",
        "POST",
        f"{AG}/{{agent_id}}/clone",
        "write",
        "Agent 의 정의를 베껴 새 draft 를 만든다.",
        kind="agent",
    ),
    _t(
        "deploy_agent",
        "POST",
        f"{AG}/{{agent_id}}/deploy",
        "deploy",
        "draft 인 Agent 를 클러스터에 배포한다. " + _DEPLOY_HINT,
        kind="agent",
        hidden_fields=(SKIP_EVALUATION,),
    ),
    _t(
        "redeploy_agent",
        "POST",
        f"{AG}/{{agent_id}}/redeploy",
        "deploy",
        "배포된 Agent 를 최신 리비전으로 재배포한다.",
        kind="agent",
        hidden_fields=(SKIP_EVALUATION,),
    ),
    _t(
        "undeploy_agent",
        "POST",
        f"{AG}/{{agent_id}}/undeploy",
        "deploy",
        "Agent 를 클러스터에서 내리고 정의는 남긴다(draft 로 돌아간다). 장기 기억은 남는다.",
        args_schema=NO_BODY,
        kind="agent",
    ),
    _t(
        "delete_agent",
        "DELETE",
        f"{AG}/{{agent_id}}",
        "destructive",
        "Agent 의 정의와 클러스터의 것을 모두 지운다. 장기 기억도 지운다.",
        kind="agent",
    ),
    # -- Knowledge Base (D32). multipart 업로드·문서 단위 reindex 는 제외 목록 --------------------
    _t(
        "list_knowledge_bases",
        "GET",
        KB,
        "read",
        "Knowledge Base 목록과 집계.",
        kind="knowledge-base",
    ),
    _t(
        "get_knowledge_base",
        "GET",
        f"{KB}/{{kb_id}}",
        "read",
        "Knowledge Base 상세와 집계.",
        kind="knowledge-base",
    ),
    _t(
        "create_knowledge_base",
        "POST",
        KB,
        "write",
        "postgres Database 와 임베딩 provider 로 Knowledge Base 를 만든다.",
        kind="knowledge-base",
    ),
    _t(
        "update_knowledge_base",
        "PATCH",
        f"{KB}/{{kb_id}}",
        "write",
        "Knowledge Base 의 표시 이름·chunking·limits 를 고친다.",
        kind="knowledge-base",
    ),
    _t(
        "delete_knowledge_base",
        "DELETE",
        f"{KB}/{{kb_id}}",
        "destructive",
        "Knowledge Base 와 벡터 테이블·문서를 지운다.",
        kind="knowledge-base",
    ),
    _t(
        "list_knowledge_documents",
        "GET",
        f"{KB}/{{kb_id}}/documents",
        "read",
        "Knowledge Base 의 문서 목록과 상태.",
        kind="knowledge-base",
    ),
    _t(
        "get_knowledge_document",
        "GET",
        f"{KB}/{{kb_id}}/documents/{{doc_id}}",
        "read",
        "문서 상세(상태·오류 코드).",
        kind="knowledge-base",
    ),
    _t(
        "add_knowledge_document_url",
        "POST",
        f"{KB}/{{kb_id}}/documents/url",
        "deploy",
        "공개 URL 문서를 Knowledge Base 에 더한다. 남의 문서를 플랫폼에 심는 일이라 승인을 거친다.",
        kind="knowledge-base",
    ),
    _t(
        "delete_knowledge_document",
        "DELETE",
        f"{KB}/{{kb_id}}/documents/{{doc_id}}",
        "destructive",
        "문서를 지운다(워커가 행·본문을 지운다).",
        kind="knowledge-base",
    ),
    _t(
        "reindex_knowledge_base",
        "POST",
        f"{KB}/{{kb_id}}/reindex",
        "write",
        "Knowledge Base 의 모든 문서를 다시 인덱싱한다.",
        args_schema=NO_BODY,
        kind="knowledge-base",
    ),
    # -- Skill (D64: 읽기만. 만들기와 리비전은 사람이 화면에서 한다) -------------------------------
    _t(
        "list_skills",
        "GET",
        SK,
        "read",
        "Skill 목록(이름·설명·최신 리비전). Agent 에 붙일 Skill 을 선택할 때 쓴다.",
        kind="skill",
    ),
    _t(
        "get_skill",
        "GET",
        f"{SK}/{{skill_id}}",
        "read",
        "Skill 상세(설명·최신 리비전·파일 목록). 파일 본문은 주지 않는다.",
        kind="skill",
    ),
    # -- Settings ----------------------------------------------------------------------------
    _t(
        "get_platform_settings", "GET", "/api/settings", "read", "플랫폼 설정(LLM provider·기본값)."
    ),
    _t(
        "update_platform_settings",
        "PATCH",
        "/api/settings",
        "destructive",
        "플랫폼 설정을 바꾼다.",
        # 웹 Tool 과 네트워크(CA) 절, LLM 게이트웨이의 토큰 한도는 사람이 Platform Settings
        # 화면에서 정한다(D64, P38 7절). 그 절들이 도구 정의의 크기 상한을 넘기므로 OTLP 설정처럼
        # 비서 도구에서 뺀다. Teams 절(P47 6.1절)은 App ID 를 바꾸면 모든 연결이 끊기므로 사람이
        # 화면에서 바꾼다. 허브 이미지 절(P55 6절)은 어느 이미지가 돌지를 정하는 키와 거부 목록이라
        # 사람이 화면에서 바꾼다
        hidden_fields=(
            "web_tools",
            "network",
            "llm_gateway",
            "default_web_tools_image",
            "channels",
            "hub_images",
        ),
    ),
    _t(
        "get_llm_gateway_status",
        "GET",
        "/api/settings/llm-gateway",
        "read",
        "LLM 게이트웨이 상태.",
    ),
    _t(
        "check_llm_provider",
        "POST",
        "/api/settings/llm-providers/{name}/check",
        "read",
        "LLM provider 의 키와 연결을 확인한다.",
        args_schema=NO_BODY,
    ),
    _t("list_my_tenants", "GET", "/api/tenants", "read", "내가 속한 테넌트와 역할."),
    # -- 사용량·가격표 (P37) ---------------------------------------------------------------
    _t(
        "get_price_table",
        "GET",
        "/api/settings/pricing",
        "read",
        "가격표(백만 토큰당 달러). 관리자 규칙과 기본 가격.",
    ),
    _t(
        "get_usage",
        "GET",
        "/api/usage",
        "read",
        "이 테넌트의 사용량과 비용. group 은 day·agent·source·user·model 이다.",
    ),
    _t(
        "get_platform_usage",
        "GET",
        "/api/admin/usage",
        "read",
        "플랫폼 전체의 사용량과 원장 건강. platform-admin 만.",
    ),
    _t(
        "get_reprice_status",
        "GET",
        "/api/usage/reprice",
        "read",
        "마지막 비용 다시 계산의 진행. platform-admin 만.",
    ),
    _t(
        "get_tenant_settings",
        "GET",
        "/api/tenants/{tenant}/settings",
        "read",
        "테넌트 설정(기본 클러스터·네임스페이스·바인딩).",
    ),
    _t(
        "update_tenant_settings",
        "PATCH",
        "/api/tenants/{tenant}/settings",
        "destructive",
        "테넌트 설정을 바꾼다.",
    ),
    # -- 클라이언트 전용 -----------------------------------------------------------------------
    _local("navigate", "화면을 앱 안 경로로 옮긴다. API 를 부르지 않는다.", NAVIGATE_ARGS),
    _local("read_guide", "플랫폼 안내문 한 편을 읽는다.", READ_GUIDE_ARGS),
    _local(
        "ask_user",
        "부족한 정보를 사용자에게 묻고 답을 기다린다. 비밀번호·토큰·API 키는 묻지 않는다.",
        ASK_USER_ARGS,
    ),
    _local(
        "use_tools",
        "Tool 묶음을 연다. 처음에는 기본 Tool 만 보인다. 할 일에 필요한 묶음을 한 번에 모두 연다. "
        "연 묶음은 이 대화가 이어지는 동안 열려 있다.",
        USE_TOOLS_ARGS,
    ),
    _local(
        "fetch_url",
        "공개 URL 의 본문(스펙 문서 등)을 데이터로 가져온다. 본문은 서버에만 남고 모델은 요약"
        "(ref·url·content_type·bytes·kind·format·operation_count)만 본다. 내용을 확인하려면 "
        "verify_spec 을 부른다. 결과의 ref(`$file:<이름>`)를 verify_spec.text 나 "
        "create_backend_api.spec_text 에 넣으면 본문 전체가 들어간다.",
        FETCH_URL_ARGS,
    ),
)

_MANIFEST = "렌더된 매니페스트는 Advanced 편집(overrides)과 짝인 화면 기능이다"
_DRIFT = (
    "drift reset·absorb 는 Advanced 편집과 같은 계열의 운영 기능이다. overrides 와 같은 이유로 뺀다"
)
_REVISION = (
    "리비전 이력·미리보기는 화면의 비교용이다. 상세가 현재 리비전 번호를 주므로 그것으로 충분하다"
)
_OVERRIDES = "임의 매니페스트라 env 값으로 비밀을 실을 수 있고 필드 규칙으로 못 잡는다"
_DATA_PLANE = "Agent 의 데이터 플레인이다. 비서는 제어면만 다룬다"
# P36 6절: 사람이 답변마다 남긴 평가와 의견이다. 비서가 대신 매기거나 남의 의견을 모델 문맥으로
# 옮기지 않는다
_FEEDBACK = "사람이 답변에 남긴 평가와 의견이다. 비서가 대신 매기거나 읽어 옮기지 않는다"
_INIT = "initContainer 가 토큰으로 읽는 파일이다"
_PLATFORM = "플랫폼 자체의 상태·설정이다. 화면이 기동 때 읽는다"
_REFINE = "다듬기(D25)는 사람이 화면의 diff 에서 골라 채택하는 것이 전제다"
_SPEC_JOBS = (
    "스펙 다듬기(D25)와 툴 오버라이드 제안(D37)의 job 은 사람이 화면의 diff 에서 골라 채택하는 "
    "것이 전제다"
)
_SUGGEST = (
    "툴 오버라이드 제안(D37)의 채택은 화면에서 제안을 하나씩 골라 리비전을 올리는 것이 전제다"
)
# D43: 도구 여덟이 한꺼번에 늘면 도구 목록과 프롬프트가 같이 바뀌고 P14 의 실측 범위가 넓어진다.
# 화면이 먼저 쓰고 나서 더할지 정한다
_REFERENCES = "참조 목록은 상세 화면과 삭제 대화상자가 먼저 쓴다. 비서 도구로는 두지 않는다"
# D64: Skill 은 붙인 모든 에이전트의 지시문이 된다. 비서가 쓰면 fetch_url 로 가져온 웹 글이
# 승인 없이 지시문이 되는 길이 생긴다. 본문은 다른 사람이 쓴 지시문이라 관리자의 비서 문맥에
# 싣지 않는다
_SKILL_WRITE = (
    "Skill 을 만들거나 고치는 동작이다. 웹에서 가져온 글이 승인 없이 지시문이 되지 않도록 사람이 "
    "화면에서 한다(D64)"
)
_SKILL_MANAGE = "Skill 관리(표시 이름·접근 정책·삭제)는 사람이 화면에서 한다(D64)"
_SKILL_BODY = (
    "Skill 본문은 남이 쓴 지시문이라 비서 문맥에 싣지 않는다(D64). 설명은 get_skill 이 준다"
)

AG_ITEM = f"{AG}/{{agent_id}}"
_RUNS = "백그라운드 실행. 바꾸는 사람이 묶인 사람이 되므로 비서가 대신 만들거나 켜지 않는다 (P41)"
_SLEEP = (
    "쓰지 않는 Agent 의 파드를 내리고 다시 띄운다. 턴과 화면이 부르고, 승인 카드의 새 문구 없이 "
    "비서 도구로 두지 않는다 (P44)"
)
_ROLLBACK = (
    "옛 리비전의 정의를 새 리비전으로 저장해 배포한다. 사람이 화면의 리비전 목록에서 고르고, 승인 "
    "카드의 새 문구 없이 비서 도구로 두지 않는다 (P45)"
)

EXCLUDED: dict[tuple[str, str], str] = {
    ("POST", f"{BP}/convert"): (
        "변환 결과가 남의 문서 원문 그대로라 모델 문맥에 들어간다. fetch_url 격리(D31)를 우회하는 "
        "유일한 경로였고, WSDL 은 create_backend_api 이 스스로 변환하므로 비서에게 필요 없다"
    ),
    ("PUT", "/api/settings/llm-providers/{name}/api-key"): (
        "비밀 값(API 키)을 받는 경로다. 비서는 비밀을 묻지도 받지도 않으므로 사람이 Platform "
        "Settings 화면에서 넣는다(D56)"
    ),
    ("PUT", f"{HUB}/{{item_id}}/secret-env"): (
        "비밀 값(image 항목의 Secret env)을 받는 경로다. 비서는 비밀을 묻지도 받지도 않으므로 "
        "사람이 허브 화면에서 넣는다(D56, P55 5절)"
    ),
    ("GET", f"{HUB}/catalog"): (
        "공개 카탈로그의 서버 설명은 남이 쓴 글이라 모델 문맥에 원문으로 들어간다. "
        "fetch_url 격리(D31)와 같은 이유로 뺀다. "
        "카탈로그 가져오기는 화면의 대화상자가 한다(P55 8절)"
    ),
    ("PUT", f"{HUB}/{{item_id}}/auth/header"): (
        "비밀 값(외부 MCP 서버의 헤더 값)을 받는 경로다. 비서는 비밀을 묻지도 받지도 않으므로 "
        "사람이 허브 화면에서 넣는다(D56, P86 6.1절)"
    ),
    ("POST", "/api/usage/reprice"): (
        "지난 행의 비용을 다시 매긴다. 과거 비용이 바뀌므로 사람이 화면에서 한다(P37 14절)"
    ),
    ("PUT", "/api/settings/pricing"): (
        "가격을 바꾸면 비용 보고가 바뀐다. 사람이 Platform Settings 화면에서 고친다(P37 14절)"
    ),
    ("POST", "/api/admin/telemetry/gateway-tokens/revoke"): (
        "사용량 수집의 자격 증명을 무른다. 유출에 대응하는 일이라 사람이 한다(P37 8절·14절)"
    ),
    ("GET", "/api/settings/observability/otlp"): (
        "OTLP 내보내기 설정(바깥 주소와 헤더 Secret 이름)이다. 사람이 Platform Settings 화면에서 "
        "보고 바꾸므로 비서 도구로 두지 않는다(P37 15절)"
    ),
    ("PUT", "/api/settings/observability/otlp"): (
        "런타임 기록이 나가는 곳(OTLP 주소와 헤더 Secret)을 바꾼다. 사람이 Platform Settings "
        "화면에서 바꾼다(P37 15절)"
    ),
    ("GET", "/api/settings/observability/otlp/status"): (
        "OTLP 내보내기의 운영 상태(마지막 성공, 실패, 버림)다. 설정 카드가 읽는 값이라 비서 도구로 "
        "두지 않는다(P37 15절)"
    ),
    ("PUT", "/api/settings/web-tools/search/api-key"): (
        "비밀 값(검색 provider 키)을 받는 경로다. 비서는 비밀을 묻지도 받지도 않으므로 사람이 "
        "Platform Settings 화면에서 넣는다(D56, D64)"
    ),
    ("POST", "/api/settings/web-tools/search/check"): (
        "설정 화면의 연결 확인이다. 검색 provider 에 실제 질의 하나를 보내 그 호출 몫을 쓰므로 "
        "사람이 화면에서 누른다(D64)"
    ),
    ("POST", f"{AG}/{{agent_id}}/llm-token/rotate"): (
        "LLM 게이트웨이 자격 증명의 교체다. 유출에 대응하는 일이라 사람이 Agent 화면에서 "
        "한다(P38 5절)"
    ),
    ("GET", "/api/settings/data-plane-auth"): (
        "인증 재적용의 진행과 설치 id 다(P32). 게이트와 e2e 가 관리자 토큰으로 읽는 운영 상태라 "
        "비서 도구로 두지 않는다"
    ),
    ("PUT", f"{MS}/{{server_id}}/overrides"): _OVERRIDES,
    ("PUT", f"{GW}/{{gateway_id}}/overrides"): _OVERRIDES,
    ("PUT", f"{AG}/{{agent_id}}/overrides"): _OVERRIDES,
    ("POST", CL): "kubeconfig·sa_token 값을 받는다. 채팅으로 클러스터를 등록하지 않는다",
    ("GET", f"{CL}/{{cluster_id}}/sandbox"): (
        "Cluster 화면의 코드 실행 카드가 읽는 운영 상태(게이트, 용량, 노드마다의 슬롯)다(P40 8절)"
    ),
    ("POST", f"{CL}/{{cluster_id}}/sandbox/check"): (
        "격리 점검은 클러스터에 파드를 띄우는 관리자 동작이다. 사람이 Cluster 화면에서 누른다"
        "(P40 8절)"
    ),
    ("POST", f"{CL}/{{cluster_id}}/sandbox/accept"): (
        "노드를 함께 쓰는 위험의 허용은 관리자가 스스로 하는 기록이다. 비서가 대신하지 않는다"
        "(P40 8절)"
    ),
    (
        "PATCH",
        f"{CL}/{{cluster_id}}",
    ): "kubeconfig·sa_token 값을 받는다. 채팅으로 클러스터를 고치지 않는다",
    (
        "DELETE",
        f"{CL}/{{cluster_id}}",
    ): "클러스터 등록과 짝인 관리자 동작이다. 채팅으로 다루지 않는다",
    (
        "GET",
        "/api/tenants/{tenant}/members",
    ): "멤버 role 은 관리자 화면의 일이다. 비서가 권한을 바꾸지 않는다",
    ("PUT", "/api/tenants/{tenant}/members"): "role 변경. 비서가 권한을 바꾸지 않는다",
    ("GET", "/api/group-mappings"): "그룹 매핑은 관리자 화면의 일이다. 비서가 권한을 바꾸지 않는다",
    ("PUT", "/api/group-mappings"): "그룹 매핑 변경. 비서가 권한을 바꾸지 않는다",
    ("POST", f"{AG}/{{agent_id}}/chat"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/sessions"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/pending"): _DATA_PLANE,
    ("POST", f"{AG}/{{agent_id}}/sessions/{{session_id}}/decisions"): _DATA_PLANE,
    ("POST", f"{AG}/{{agent_id}}/sessions/{{session_id}}/cancel"): _DATA_PLANE,
    ("POST", f"{AG}/{{agent_id}}/sleep"): _SLEEP,
    ("POST", f"{AG}/{{agent_id}}/wake"): _SLEEP,
    ("POST", f"{AG}/{{agent_id}}/rollback"): _ROLLBACK,
    ("PUT", f"{AG}/{{agent_id}}/evaluation-gate"): (
        "배포 게이트는 사람이 화면에서 정하는 안전 설정이다. 비서가 끄거나 느슨하게 하지 "
        "못한다 (P45)"
    ),
    # 사람의 대화를 지우거나 이름을 바꾼다. 프롬프트 주입 한 번으로 되돌릴 수 없는 일이 생긴다(P36)
    ("PATCH", f"{AG}/{{agent_id}}/sessions/{{session_id}}"): _DATA_PLANE,
    ("DELETE", f"{AG}/{{agent_id}}/sessions/{{session_id}}"): _DATA_PLANE,
    # 사람의 마지막 턴을 버리고 다시 만든다. 채팅과 같은 데이터 플레인이다(P36)
    ("POST", f"{AG}/{{agent_id}}/sessions/{{session_id}}/regenerate"): _DATA_PLANE,
    # 채팅에 붙일 파일을 업로드하고 지운다. 채팅과 같은 데이터 플레인이다(P36)
    ("POST", f"{AG}/{{agent_id}}/uploads"): _DATA_PLANE,
    ("DELETE", f"{AG}/{{agent_id}}/uploads/{{upload_id}}"): _DATA_PLANE,
    # 답이 인용한 테넌트 문서를 통째 본문으로 내려받는다. 모델 문맥에 원문을 옮기지 않는다(P36)
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/citations/{{n}}/source"): _DATA_PLANE,
    # 사람의 대화에 업로드한 파일과 에이전트가 쓴 파일이다. 통째 본문이라 모델 문맥에 옮기지
    # 않는다(P36)
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/files"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/files/{{path}}"): _DATA_PLANE,
    ("PUT", f"{AG}/{{agent_id}}/sessions/{{session_id}}/turns/{{turn_id}}/feedback"): _FEEDBACK,
    ("DELETE", f"{AG}/{{agent_id}}/sessions/{{session_id}}/turns/{{turn_id}}/feedback"): _FEEDBACK,
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/feedback"): _FEEDBACK,
    ("GET", f"{AG}/{{agent_id}}/feedback"): _FEEDBACK,
    ("GET", f"{AG}/{{agent_id}}/feedback/export"): _FEEDBACK,
    ("GET", f"{AG}/summary"): (
        "고르개 전용 가벼운 목록이다(D35). 비서는 상태가 붙은 list_agents 를 쓴다"
    ),
    ("DELETE", f"{AG}/{{agent_id}}/memories/{{user_sub}}"): (
        "사람 하나의 장기 기억을 지운다(D34). 되돌릴 수 없고 대상이 사용자라 채팅으로 다루지 않는다"
    ),
    # 사람의 장기 기억을 읽거나 지운다. 프롬프트 주입 한 번으로 되돌릴 수 없는 일이 생긴다(P36)
    ("GET", f"{AG}/{{agent_id}}/memories/me"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/memories/me/files/{{key}}"): _DATA_PLANE,
    ("DELETE", f"{AG}/{{agent_id}}/memories/me/files/{{key}}"): _DATA_PLANE,
    ("GET", f"{AG}/{{agent_id}}/metrics"): "런타임 관측이다. 비서는 제어면만 다룬다",
    ("GET", f"{MS}/{{server_id}}/metrics"): "런타임 관측이다. 비서는 제어면만 다룬다",
    ("GET", f"{GW}/{{gateway_id}}/metrics"): "런타임 관측이다. 비서는 제어면만 다룬다",
    ("GET", f"{AG}/{{agent_id}}/events"): "런타임 관측 이벤트 목록이다. 비서는 제어면만 다룬다",
    ("GET", f"{AG}/{{agent_id}}/sessions/{{session_id}}/turns"): (
        "런타임 관측(턴의 실행과 비용)이다. 비서는 제어면만 다룬다(P37 14절)"
    ),
    ("GET", f"{AG}/{{agent_id}}/traces/{{trace_id}}"): (
        "다른 사람의 Tool 결과가 그대로 든 런타임 관측이다. 가져온 자료를 그대로 넘기지 않는다는 "
        "D31 의 규칙에 어긋난다(P37 14절)"
    ),
    ("GET", f"{AG}/{{agent_id}}/agent.json"): _INIT,
    ("GET", f"{AG}/{{agent_id}}/skills/{{name}}"): _INIT,
    ("POST", f"{BP}/{{api_id}}/tokens"): "평문 토큰이 응답에 실려 세션 문서에 남는다(D14)",
    ("GET", "/api/spec-generator/jobs"): _SPEC_JOBS,
    ("POST", "/api/spec-generator/jobs"): _SPEC_JOBS,
    ("GET", "/api/spec-generator/jobs/{job_id}"): _SPEC_JOBS,
    ("DELETE", "/api/spec-generator/jobs/{job_id}"): _SPEC_JOBS,
    ("GET", f"{BP}/{{api_id}}/versions/{{version}}/openapi.yaml"): _INIT,
    ("GET", f"{MS}/{{server_id}}/revisions/{{n}}/profile.json"): _INIT,
    ("GET", "/api/auth/config"): "로그인 설정이다. 화면이 기동 때 읽는다",
    ("GET", "/api/me"): "턴 요청의 주체를 서비스가 이미 안다",
    ("GET", "/healthz"): _PLATFORM,
    ("GET", "/api/version"): _PLATFORM,
    ("GET", "/api/audit"): "감사 조회는 감사 화면의 일이다. 비서의 쓰기 호출이 여기 남는다",
    ("GET", "/api/audit/mcp"): "MCP 프록시 감사 조회는 감사 화면의 일이다",
    (
        "GET",
        f"{BP}/{{api_id}}/versions/{{version}}/spec",
    ): "변환된 문서 전체라 4KB 도구 결과에 담기지 않는다. 버전 상세가 tool_names 를 준다",
    (
        "GET",
        f"{BP}/{{api_id}}/versions/{{version}}/source",
    ): "올린 원본 문서 전체라 4KB 도구 결과에 담기지 않는다. 버전 상세가 tool_names 를 준다",
    ("POST", f"{BP}/{{api_id}}/versions/{{version}}/minify"): _REFINE,
    ("POST", f"{BP}/{{api_id}}/versions/{{version}}/apply-draft"): _REFINE,
    ("POST", f"{MS}/{{server_id}}/apply-draft"): _SUGGEST,
    ("GET", f"{MS}/{{server_id}}/revisions"): _REVISION,
    ("GET", f"{MS}/{{server_id}}/revisions/{{n}}"): _REVISION,
    ("POST", f"{MS}/{{server_id}}/revisions/{{n}}/preview"): _REVISION,
    ("GET", f"{GW}/{{gateway_id}}/revisions"): _REVISION,
    ("GET", f"{GW}/{{gateway_id}}/revisions/{{n}}"): _REVISION,
    ("GET", f"{GW}/{{gateway_id}}/revisions/{{n}}/preview"): _REVISION,
    ("GET", f"{AG}/{{agent_id}}/revisions"): _REVISION,
    ("GET", f"{AG}/{{agent_id}}/revisions/{{n}}"): _REVISION,
    ("GET", f"{MS}/{{server_id}}/manifest"): _MANIFEST,
    ("GET", f"{GW}/{{gateway_id}}/manifest"): _MANIFEST,
    ("GET", f"{AG}/{{agent_id}}/manifest"): _MANIFEST,
    ("GET", f"{HUB}/{{item_id}}/manifest"): _MANIFEST,
    ("GET", f"{HUB}/{{item_id}}/tools"): (
        "도구의 설명과 입력 스키마는 남의 MCP 서버가 쓴 글이라 모델 문맥에 원문으로 들어간다. "
        "fetch_url 격리(D31)와 같은 이유로 뺀다. Gateway 의 tools 필터는 이름으로 쓰므로 "
        "비서에게는 get_hub_item 의 tool_names 와 refresh_hub_tools 로 충분하다"
    ),
    ("POST", f"{MS}/{{server_id}}/drift/{{action}}"): _DRIFT,
    ("POST", f"{GW}/{{gateway_id}}/drift/{{action}}"): _DRIFT,
    ("POST", f"{AG}/{{agent_id}}/drift/{{action}}"): _DRIFT,
    ("POST", f"{KB}/{{kb_id}}/documents"): "본문 업로드. URL 문서로 같은 결과",
    ("POST", f"{KB}/{{kb_id}}/documents/{{doc_id}}/reindex"): "전체 reindex 로 충분",
    ("GET", f"{BP}/{{api_id}}/references"): _REFERENCES,
    ("GET", f"{MS}/{{server_id}}/references"): _REFERENCES,
    ("GET", f"{GW}/{{gateway_id}}/references"): _REFERENCES,
    ("GET", f"{AG}/{{agent_id}}/references"): _REFERENCES,
    ("GET", f"{DS}/{{ds_id}}/references"): _REFERENCES,
    ("GET", f"{KB}/{{kb_id}}/references"): _REFERENCES,
    ("GET", f"{HUB}/{{item_id}}/references"): _REFERENCES,
    ("GET", f"{CL}/{{cluster_id}}/references"): _REFERENCES,
    ("GET", f"{SK}/{{skill_id}}/references"): _REFERENCES,
    ("POST", SK): _SKILL_WRITE,
    ("POST", f"{SK}/import"): _SKILL_WRITE,
    ("POST", f"{SK}/validate"): (
        "Skill 을 저장하기 전에 화면이 부르는 검사다. 비서는 Skill 을 만들거나 고치지 않는다(D64)"
    ),
    ("PATCH", f"{SK}/{{skill_id}}"): _SKILL_MANAGE,
    ("DELETE", f"{SK}/{{skill_id}}"): _SKILL_MANAGE,
    ("PUT", f"{SK}/{{skill_id}}/access"): _SKILL_MANAGE,
    ("PUT", f"{HUB}/{{item_id}}/tool-rules"): (
        "Tool 규칙은 누가 부를 수 있는지를 정한다. 사람이 화면에서 정하고 비서는 바꾸지 않는다(P52)"
    ),
    ("POST", f"{SK}/{{skill_id}}/clone"): _SKILL_WRITE,
    ("POST", f"{SK}/{{skill_id}}/revisions"): _SKILL_WRITE,
    ("POST", f"{SK}/{{skill_id}}/revisions/import"): _SKILL_WRITE,
    ("GET", f"{SK}/{{skill_id}}/revisions"): _REVISION,
    ("GET", f"{SK}/{{skill_id}}/revisions/{{n}}"): _SKILL_BODY,
    ("GET", f"{SK}/{{skill_id}}/revisions/{{n}}/archive"): _SKILL_BODY,
    # 백그라운드 실행 (P41 2절). 스케줄을 만들거나 바꾸거나 다시 켜거나 지금 실행한 사람이 그
    # 실행의 묶인 사람이 되므로 비서의 사용자가 남의 일정을 자기 이름으로 떠맡는다. 기록은 화면이
    # 읽는다
    ("GET", f"{AG_ITEM}/runs"): _RUNS,
    ("GET", f"{AG_ITEM}/runs/{{run_id}}"): _RUNS,
    ("POST", f"{AG_ITEM}/runs"): _RUNS,
    ("POST", f"{AG_ITEM}/runs/{{run_id}}/cancel"): _RUNS,
    ("GET", f"{AG_ITEM}/schedules"): _RUNS,
    ("POST", f"{AG_ITEM}/schedules"): _RUNS,
    ("PATCH", f"{AG_ITEM}/schedules/{{schedule_id}}"): _RUNS,
    ("DELETE", f"{AG_ITEM}/schedules/{{schedule_id}}"): _RUNS,
    ("POST", f"{AG_ITEM}/schedules/{{schedule_id}}/pause"): _RUNS,
    ("POST", f"{AG_ITEM}/schedules/{{schedule_id}}/resume"): _RUNS,
    # 웹훅 트리거 (P41 4절). 만들기와 비밀 새로 만들기는 서명 비밀을 돌려준다(D31, D56 과 같다)
    ("GET", f"{AG_ITEM}/triggers"): _RUNS,
    ("POST", f"{AG_ITEM}/triggers"): _RUNS,
    ("PATCH", f"{AG_ITEM}/triggers/{{trigger_id}}"): _RUNS,
    ("DELETE", f"{AG_ITEM}/triggers/{{trigger_id}}"): _RUNS,
    ("POST", f"{AG_ITEM}/triggers/{{trigger_id}}/rotate-secret"): _RUNS,
    ("POST", f"{AG_ITEM}/triggers/{{trigger_id}}/pause"): _RUNS,
    ("POST", f"{AG_ITEM}/triggers/{{trigger_id}}/resume"): _RUNS,
}

# 경로 접두사로 제외하는 것. 비서 자신의 API 는 동작이 늘어도 전부 재귀이므로 접두사 하나로 막는다
EXCLUDED_PREFIXES: dict[str, str] = {
    "/api/assistant/": "재귀. 비서가 자기 자신을 부르지 않는다",
    # local 인증 모드 (D40). 비밀번호를 값으로 받고 계정·관리자 권한을 바꾸는 자리다. 비서는 비밀을
    # 채팅으로 받지 않고 권한을 바꾸지 않는다(위 members·group-mappings 와 같은 규칙)
    "/api/auth/": (
        "로그인·비밀번호·토큰 발급. 비밀을 채팅으로 받지 않고 비서가 자기 토큰을 만들지 않는다"
    ),
    "/api/users": "사용자 계정과 platform-admin 권한. 비서가 권한을 바꾸지 않는다",
    # 개인 MCP 토큰 (P32). 관리자 라우트는 위 /api/users 접두로 이미 빠진다
    "/api/me/tokens": (
        "개인 토큰 발급·회수. 평문 토큰과 비밀번호가 세션 문서와 모델 문맥에 남는다"
    ),
    # MCP 클라이언트의 OAuth 인가 서버 (P33). 클라이언트 라우트는 OpenAPI 에 없고 동의 셋만 있다
    "/api/oauth/": (
        "MCP 클라이언트 인가와 동의. 비서가 동의를 대신 누르면 프롬프트 주입 한 번으로 연결이 "
        "생긴다"
    ),
    # 관리자 라우트는 위 /api/users 접두로 이미 빠진다
    "/api/me/oauth-grants": "OAuth 연결 끊기. 사람이 화면에서 한다",
    # 서비스 어카운트 (P53b 6.7절). 권한을 싣는 계정과 평문 토큰이고 토큰을 내려면 다시 로그인한다
    "/api/tenants/{tenant}/service-accounts": (
        "서비스 어카운트와 그 토큰. 비서가 권한을 바꾸지 않고 평문 토큰이 모델 문맥에 남는다"
    ),
    # 백그라운드 실행 (P41 6절). 다른 실행 라우트와 같이 화면의 것이다
    "/api/me/runs-as-me": "나를 대신하는 실행의 목록과 멈춤. 사람이 계정 화면에서 한다 (P41)",
    # 기억한 승인 선택 (P43 5절). 비서가 지우면 사람이 고른 거부가 사라진다
    "/api/me/approval-choices": "기억한 승인 선택의 목록과 지우기. 사람이 계정 화면에서 한다",
    # Teams 채널 (P47 6.9절). 연결 코드는 다시 로그인하거나 비밀번호를 넣어 만들고, 봇의 Client
    # Secret 은 비밀 값이다
    "/api/me/channels": (
        "Teams 연결과 그 코드. 평문 코드와 비밀번호가 모델 문맥에 남으므로 사람이 계정 메뉴에서 "
        "한다"
    ),
    "/api/settings/hub-images/": (
        "허브 이미지의 읽기 자격 증명과 설정 카드의 계산한 값. 비밀 값을 받고 모든 테넌트의 항목을 "
        "실으므로 사람이 Platform Settings 화면에서 한다(P55 6절)"
    ),
    "/api/settings/channels/": (
        "Teams 봇의 Client Secret, 앱 패키지, 연결 끊기. 비밀 값을 받고 모든 사람의 연결을 끊을 수 "
        "있으므로 사람이 Platform Settings 화면에서 한다"
    ),
    # Agent 평가 (P45). 세트와 사례는 남의 대화에서 복사한 내용을 담고 사람이 화면에서 다룬다
    "/api/agents/{agent_id}/evaluation-sets": (
        "Agent 평가 세트와 사례. 남의 대화에서 복사한 질문과 답을 담으므로 모델 문맥에 옮기지 않고 "
        "사람이 Agent 화면에서 다룬다"
    ),
    # Agent 미리 보기 (P44b). `/preview` 와 `/previews` 둘 다 이 접두로 빠진다
    "/api/agents/{agent_id}/preview": (
        "Agent 미리 보기의 시작·대화·끝. 실제 Tool 을 사람으로 부르는 파드를 띄우므로 사람이 "
        "캔버스의 테스트 서랍에서 한다"
    ),
}


def group_of(spec: ToolSpec) -> str:
    """도구의 묶음 (D57). 클라이언트 전용 도구는 기본 묶음이고, API 도구는 경로 앞머리로 정한다.
    어느 앞머리에도 맞지 않으면 `other` 다. 루프가 늘 싣으므로 새 경로의 도구가 모델에게서
    사라지지는 않지만, 실제 표에서는 앞머리를 더해 제 묶음에 넣어야 한다."""
    if spec.local:
        return spec.group or CORE_GROUP
    for prefix, group in _GROUP_PREFIXES:
        if spec.path is not None and (spec.path == prefix or spec.path.startswith(prefix + "/")):
            return group
    return OTHER_GROUP


@dataclass(frozen=True)
class Catalog:
    """도구 표와 제외 목록. 만들 때 표 자체의 규칙을 검사한다."""

    tools: tuple[ToolSpec, ...] = TOOLS
    excluded: dict[tuple[str, str], str] = field(default_factory=lambda: dict(EXCLUDED))
    excluded_prefixes: dict[str, str] = field(default_factory=lambda: dict(EXCLUDED_PREFIXES))

    def __post_init__(self) -> None:
        names: set[str] = set()
        keys: set[tuple[str, str]] = set()
        for t in self.tools:
            if not CATALOG_NAME_RE.fullmatch(t.name):
                raise CatalogError(f"도구 이름이 규칙에 맞지 않는다: {t.name!r}")
            if t.name in names:
                raise CatalogError(f"도구 이름이 겹친다: {t.name!r}")
            names.add(t.name)
            if t.group is not None and not t.local:
                raise CatalogError(f"group 은 클라이언트 전용 도구만 정한다: {t.name}")
            if t.local:
                if t.args_schema is None:
                    raise CatalogError(
                        f"클라이언트 전용 도구는 인자 스키마가 있어야 한다: {t.name}"
                    )
                continue
            key = t.key
            if key is None:
                raise CatalogError(f"API 도구는 method 와 path 가 있어야 한다: {t.name}")
            if key in keys:
                raise CatalogError(f"같은 동작이 표에 두 번 있다: {key}")
            if self.exclusion_reason(*key) is not None:
                raise CatalogError(f"표와 제외 목록에 같이 있다: {key}")
            if t.resource_kind is not None and t.resource_kind not in RESOURCE_ROUTES:
                raise CatalogError(f"모르는 resource_kind {t.resource_kind!r}: {t.name}")
            keys.add(key)

    def get(self, name: str) -> ToolSpec | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    @property
    def api_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(t for t in self.tools if not t.local)

    @property
    def local_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(t for t in self.tools if t.local)

    def exclusion_reason(self, method: str, path: str) -> str | None:
        """제외 목록에 있으면 이유, 아니면 None."""
        reason = self.excluded.get((method.upper(), path))
        if reason is not None:
            return reason
        for prefix, why in self.excluded_prefixes.items():
            if path.startswith(prefix):
                return why
        return None


CATALOG = Catalog()

__all__ = [
    "CATALOG",
    "CATALOG_NAME_RE",
    "CORE_GROUP",
    "OTHER_GROUP",
    "TOOL_GROUPS",
    "EXCLUDED",
    "EXCLUDED_PREFIXES",
    "RESOURCE_ROUTES",
    "TOOLS",
    "Catalog",
    "CatalogError",
    "Risk",
    "ToolSpec",
    "group_of",
]
