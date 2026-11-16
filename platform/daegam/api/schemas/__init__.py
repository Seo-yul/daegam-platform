"""API 응답 모델. 라우터의 `_out()` 결과와 필드가 정확히 같아야 한다 (UI·e2e 스크립트가 의존).

`response_model=` 로만 붙는다. 핸들러는 예전처럼 dict 를 돌려주고 FastAPI 가 이 모델로 검증한다.
openapi-typescript 가 여기 모델을 `components.schemas` 로 뽑아 ui/src/api/schema.d.ts 를 만든다.

규칙 둘:

1. `_out()` 이 store 문서를 그대로 내보내면 store 모델을 상속한다(`ClusterOut(Cluster)`,
   `KnowledgeBaseOut(KnowledgeBase)`, `DataServiceOut(DataService)`,
   `HubItemOut(HubItemBase)`). `_out()` 이 모양을 바꾸면(현재 리비전을 펼치고 `revisions` 를 번호
   목록으로 접는 정의 쪽) 필드를 다시 적는다(`MCPServerOut`, `AgentProfileOut`,
   `GatewayProfileOut`).
2. 응답 전용 모델은 `model_config = REQUIRED_DEFAULTS` 를 붙인다. 요청 본문과 공유하는 모델
   (`ClusterImages` 등)에는 붙이지 않는다. -Input/-Output 으로 갈라지면 UI 가 쓰는 스키마 이름이
   바뀐다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 응답 전용 모델의 공통 설정. 기본값 있는 필드도 required 로 문서화한다(응답에는 항상 들어
# 있으므로). 여섯 파일이 각자 선언하던 것을 여기 하나로 모은다. 하나만 고치면 schema.d.ts 가
# 조용히 갈라진다.
REQUIRED_DEFAULTS = ConfigDict(json_schema_serialization_defaults_required=True)


class ErrorDetail(BaseModel):
    """`HTTPException(detail={"code", "detail", ...})`. 코드별 부가 필드(tenants 등)는 extra."""

    model_config = ConfigDict(extra="allow")

    code: str
    detail: str


class ErrorResponse(BaseModel):
    """오류 본문. 본문 검증 실패(FastAPI 기본 422)는 detail 이 목록이다."""

    detail: ErrorDetail | list[dict[str, Any]]


class DeleteResponse(BaseModel):
    id: str
    deleted: bool


class VersionInfo(BaseModel):
    """API 버전과 이 플랫폼 이미지의 빌드(`<version>+<도장>`, 로컬 프로세스면 null). UI 빌드는
    daegam-ui 의 /build.json 이 따로 낸다(D52)."""

    api_version: str
    api_build: str | None = None
    # 이 이미지를 낸 릴리스 태그(1.0.0-rc3 등). 릴리스로 구운 이미지에만 있다. 정식 릴리스는 마지막
    # 후보의 이미지에 태그만 더하므로 그 후보의 태그가 남는다 (docs/guide/images.md 2절)
    release: str | None = None


def _err(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


# 라우터 공통 오류 문서. `APIRouter(responses=ERROR_RESPONSES)`
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: _err("잘못된 테넌트 이름 또는 테넌트 미선택(tenant_required)"),
    401: _err("토큰 없음·검증 실패"),
    403: _err("소속 아님(not_member) 또는 scope 부족(forbidden)"),
    404: _err("없는 리소스"),
    409: _err("이름 충돌·참조 중·상태 충돌"),
    422: _err("입력 검증 실패 {code, detail} 또는 본문 스키마 오류(목록)"),
    502: _err("클러스터 접속 실패"),
    503: _err("IdP·home 클러스터 사용 불가"),
}


def too_large_response(description: str) -> dict[int | str, dict[str, Any]]:
    """업로드 라우트가 더하는 413 `too_large` 문서 (P74 2.3절)."""
    return {413: _err(f"{description} (too_large)")}


__all__ = [
    "ERROR_RESPONSES",
    "REQUIRED_DEFAULTS",
    "DeleteResponse",
    "ErrorDetail",
    "ErrorResponse",
    "Field",
    "RulesNotice",
    "VersionInfo",
    "too_large_response",
]


class RulesNotice(BaseModel):
    """규칙 저장의 알림 (P52 1절). opened 는 callers 를 모두 지워 이 테넌트의 모든 멤버와
    Agent 에게 열리는 Tool 의 키이고, rolling_agents 는 토큰을 다시 받으려고 한 번 다시 뜨는
    Agent 다."""

    model_config = REQUIRED_DEFAULTS

    opened: list[str]
    rolling_agents: list[dict[str, str]]
