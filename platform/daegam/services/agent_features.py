"""런타임 기능 이름과 이미지 기록 (D65, P35 3절).

agent.json 의 판 사다리는 "3" 에서 멈추고 새 런타임 기능은 이름으로 선언한다(D60 R3). 이름은 두
종류다. 문서 기능은 agent.json 의 선택 키 `features` 에 실리고, 런타임 능력은 런타임이 내는 API·
이벤트라 문서에 실리지 않는다. 두 목록은 `agent-runtime/runtime/features.py` 와 같고 파리티 테스트가
두 파일을 맞댄다.

이미지가 아는 이름의 원천은 그 이미지 자신이다. Agent 파드의 init 이 런타임 이미지로 돌며
`agent.json` 을 받을 때 머리 둘(`X-Daegam-Runtime-Features`·`X-Daegam-Runtime-Image`)을 싣고,
플랫폼은 꼴과 개수, 길이가 틀린 머리를 없는 것으로 보고 경고하며 아는 이름만 그 에이전트 문서의
`runtime` 에 적는다(`MongoAgentStore.record_runtime`). 기록은 에이전트마다 따로라 한 에이전트의
토큰으로 꾸민 이름이 다른 에이전트와 테넌트의 판정에 번지지 않는다.

문서 기능은 에이전트마다 두 범주로 센다. `required` 는 이미지가 모르면 이 에이전트가 돌 수 없는
기능(그래프가 쓰는 기능과 저장된 결정이 렌더를 바꾼 기능)이고, `attached` 는 플랫폼이 붙이는
기능이라 이미지가 모르면 빼도 돈다. 검사 시점은 셋이다. 배포·재배포는 같은 이미지의 기록이 있을
때만 `required` 가 빠지면 409 이고, init 받기는 머리의 이름으로 409 를 내며, 런타임 기동은 마지막
그물이다. 두 409 는 모두 `runtime_image_too_old` 다. 붙이는 차례는 허락된 기능을 정하고, 붙이는
함수가 그 기능의 칸만 싣고, 최종 문서를 한 번 검사하는 것이다.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from daegam.k8s.templates.agent import FEATURES_HEADER, IMAGE_HEADER
from daegam.services.agent_graph import (
    RULE_INJECTION_CHECK,
    CompileError,
    GraphContext,
    Issue,
    check_schema,
    code_execution_of,
    compiled_model_providers,
    issue_lines,
    uses_injection_check,
)
from daegam.services.agent_skills import attach_skills, skill_pins
from daegam.services.agent_tool_safety import attach_tool_safety
from daegam.services.agent_web import attach_web
from daegam.store.agents import Agent, AgentRuntime, AgentSpec
from daegam.store.base import utcnow
from daegam.store.settings import LLMProvider

# 진행 중 턴을 멈추는 런타임 API 다(D65). 플랫폼의 중지 라우트가 이 이름으로 가른다
CANCEL = "cancel"
# 세션 제목과 소유자의 이름 바꾸기·지우기·검색이다(P36). 세션 관리 라우트와 목록의 `q` 가 가른다
SESSION_ADMIN = "session_admin"
# 마지막 턴의 다시 생성과 고쳐 보내기다(P36). 다시 생성 라우트가 가른다
REGENERATE = "regenerate"
# 검색 결과의 인용 번호와 `citations` 이벤트다(P36). 인용 원문 라우트가 가른다
CITATIONS = "citations"
# 호출자 자신의 장기 기억 파일 목록·읽기·하나 지우기다(P36). `memories/me` 세 라우트가 가른다
MEMORY_FILES = "memory_files"
# `/chat` 본문의 첨부와 `done.attachments_received` 다(P36). 첨부가 든 턴을 보낼 때 가른다. 옛
# 런타임은 모르는 필드를 조용히 버리기 때문이다
ATTACHMENTS = "attachments"
# 세션 파일의 목록과 내려받기다(P36). 사용자가 업로드한 첨부와 에이전트가 파일 도구로 쓴 파일이고
# 세션 파일 두 라우트가 가른다
SESSION_FILES = "session_files"
# 모델 호출·도구 실행 스팬과 상관 헤더다(P37). 문서에 실리지 않는다. 이 이름이 없는 이미지는 지표
# 탭과 사용량 화면이 재배포를 권한다
TELEMETRY = "telemetry"
# provider 의 요청 본문 추가분 `provider.extra_body` 다(T19, OpenRouter 업체 선호). 플랫폼이
# provider 설정에서 붙이는 기능이라 `attached` 이고, 이미지가 모르면 빼도 OpenRouter 기본 업체로
# 돈다
PROVIDER_EXTRA_BODY = "provider_extra_body"
# Redis 바인딩의 TLS env(`<PREFIX>_TLS`·`<PREFIX>_CA_FILE`, `rediss://`)를 읽는다(P61). 플랫폼은
# 이 이름을 알린 이미지에 TLS 끝점을 렌더하고, 옛 이미지는 평문 포트를 계속 쓴다. 평문 포트가 닫힌
# Redis 는 이미지와 상관없이 TLS 끝점이고 이 이름을 요구한다(`requiring_redis_tls`)
REDIS_TLS = "redis_tls"
# 붙인 Skill 이다(D64). 문서에 최상위 `skills[]` 와 에이전트별 `skills` 가 실리고 init 단계가 파일을
# 받는다
SKILLS = "skills"
# provider 의 `stream_usage: false` 다(P37). stream_options 를 거절하는 호환 서버를 위해 끈 provider
# 에 플랫폼이 붙이는 기능이라 `attached` 이고, 이미지가 모르면 빼도 그 이미지는 stream_options 를
# 보내지 않으므로 해가 없다
PROVIDER_STREAM_USAGE = "provider_stream_usage"
# 턴 위임 토큰이다(P53 6절). 런타임이 `/chat`·결정·다시 생성 본문의 `turn_grant` 를 받아 그
# 턴의 Tool 을 사람의 토큰으로 부른다. 옛 이미지는 모르는 칸을 버리므로 플랫폼은 이 이름을 알린
# 이미지에만 위임을 싣는다
TURN_TOKENS = "turn_tokens"
# 요청과 떨어진 턴이다(P78, D92). 연결이 끊겨도 턴이 끝까지 돌고 세션 한 줄이 running 을 싣는다. 이
# 이름을 알린 이미지에만 진행 중 표시와 가벼운 읽기, 이름 준 중지를 쓴다
DETACHED_TURNS = "detached_turns"
# 거절된 Tool 호출이다(P52 5절). 도구 소스의 403·429 가 턴을 끝내지 않고 모델이 보는 Tool 오류가
# 된다. 이 이름이 없는 이미지가 규칙이 있는 Tool 을 쓰면 상태가 `runtime_tool_refusals` 를 낸다
TOOL_REFUSALS = "tool_refusals"
# 백그라운드 실행의 런타임 쪽이다(P41 9절). `/chat` 의 `run_id`·`untrusted`, 실행 세션의 규칙,
# `GET /sessions?runs=` 와 턴 이벤트의 `run_id` 다. 시작 워커는 이 이름이 아니라 답의
# `X-Daegam-Run-Id` 로 그 턴을 받은 파드가 이 판인지 본다
RUNS = "runs"
# Agent 를 0 으로 줄이기의 런타임 쪽이다(P44 7·11절). `POST`·`DELETE /drain` 과 표시가 선 동안의
# 503 `agent_draining`, 기동하자마자 도는 첫 sweep 과 장기 기억의 만료 정리, TTL 이 지난 세션을
# 닫힌 것으로 읽기다. 이 이름을 알린 이미지의 Agent 만 잠들 수 있다(eligibility `runtime_image`)
SCALE_TO_ZERO = "scale_to_zero"
# 노출한 Agent 의 두 발급자다(P53b 6.8절). 런타임이 데이터 플레인이 믿는 발급자 곁에 IdP 를 두고
# 토큰의 iss 로 골라 발급자마다의 계약으로 판정한다. 플랫폼을 믿는 oidc 배포는 이 이름을 알린
# 이미지의 노출 Agent 에만 플랫폼을 첫 발급자로 렌더하고 IdP 를 곁에 둔다. 옛 이미지는 IdP 만 받는다
TWO_ISSUERS = "two_issuers"
# 플랫폼 웹 Tool 의 안전 바닥이다(D64). 문서에 최상위 `web_data_tools` 와 소스별 `tools[].web` 이
# 실리고 런타임의 웹 가드가 그것을 본다
WEB_GUARD = "web_guard"
# Tool 결과의 바깥 표시와 그 뒤의 승인이다(P43). 문서에 소스별 `tools[].inside`·`tools[].read` 와
# Knowledge 항목별 `outside` 가 실린다. 모든 에이전트에 붙이는 기능이라 `attached` 이고, 웹 Tool 이
# 있는 그래프는 웹 내용 뒤의 쓰기를 이것이 물으므로 `required` 다
TOOL_SAFETY = "tool_safety"
# 코드 실행이다(P40, D95). 문서에 최상위 `code_execution: {approval}` 이 실리고 런타임의 파일
# 백엔드가 세션마다의 샌드박스가 된다. 그래프가 쓰는 기능이라 `required` 다
CODE_EXECUTION = "code_execution"
# LLM 게이트웨이 경로다(P38 6절). 그 경로의 문서는 provider 에 `gateway: true` 와 게이트웨이
# base_url 을 싣고, 렌더는 키 env 를 90일 토큰으로 바꾼다. 모르는 이미지는 토큰을 키로 보내므로
# 게이트웨이 경로의 문서에서 `required` 다. 직접 경로의 문서에는 이름을 싣지 않는다
LLM_GATEWAY = "llm_gateway"
# 바깥 결과의 판정 모델이다(P43 7절). 문서의 position tool 가드레일에 규칙 `injection_check` 가
# 판정 모델 객체를 싣는다. 그래프가 쓰는 기능이라 `required` 이고, 바깥 결과를 알아보는
# `tool_safety` 도 함께 `required` 다
INJECTION_CHECK = "injection_check"
# 상태가 파드 하나에 산다(P44b 7절). 늘 파드 하나로 도는 미리 보기 가운데 그 리비전이 원본에서
# 메인에 memory 노드를 가진 것의 문서에만 붙인다. 배포한 Agent 에는 붙이지 않는다. memory 노드가
# 없어도 런타임이 승인을 거절하지 않고 묻는다. 이름만 실리고 칸은 없다. `attached` 라 모르는
# 이미지에서는 빠지고, 그 미리 보기는 memory 노드가 없는 Agent 처럼 승인이 필요한 호출을 거절한다
IN_POD_STATE = "in_pod_state"

# agent-runtime/runtime/features.py 의 두 목록과 같다
DOCUMENT_FEATURES: tuple[str, ...] = (
    PROVIDER_EXTRA_BODY,
    SKILLS,
    PROVIDER_STREAM_USAGE,
    WEB_GUARD,
    TOOL_SAFETY,
    CODE_EXECUTION,
    LLM_GATEWAY,
    IN_POD_STATE,
    INJECTION_CHECK,
)
CAPABILITIES: tuple[str, ...] = (
    CANCEL,
    SESSION_ADMIN,
    REGENERATE,
    CITATIONS,
    MEMORY_FILES,
    ATTACHMENTS,
    SESSION_FILES,
    REDIS_TLS,
    TELEMETRY,
    TURN_TOKENS,
    DETACHED_TURNS,
    RUNS,
    TOOL_REFUSALS,
    SCALE_TO_ZERO,
    TWO_ISSUERS,
)
KNOWN: frozenset[str] = frozenset(DOCUMENT_FEATURES) | frozenset(CAPABILITIES)

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
NAME_RE = re.compile(NAME_PATTERN)

# 머리 상한. 넘거나 꼴이 틀리면 머리를 없는 것으로 보고 받기는 성공시킨다. 알리는 머리 하나 때문에
# 에이전트가 멈추지 않게 하려는 것이다
MAX_HEADER_BYTES = 4096
MAX_FEATURES = 64

# 오류·경고 코드. 화면의 errorText·warningText 가 같은 이름을 쓴다
RUNTIME_IMAGE_TOO_OLD = "runtime_image_too_old"
RUNTIME_FEATURE_MISSING = "runtime_feature_missing"
# 롤링 창이다(D61). 기록에는 이름이 있는데 그 이름을 모르는 옛 파드가 답했거나, 옛 파드가 조용히
# 버리는 요청을 보내려는데 phase 가 Ready 가 아니다
RUNTIME_ROLLING = "runtime_rolling"
RUNTIME_FEATURE_DROPPED = "runtime_feature_dropped"
RUNTIME_FEATURES_UNKNOWN = "runtime_features_unknown"
RUNTIME_FEATURES_INVALID = "runtime_features_invalid"


@dataclass(frozen=True)
class AgentFeatures:
    """에이전트 하나가 요구하는 문서 기능이다."""

    required: frozenset[str] = frozenset()
    attached: frozenset[str] = frozenset()

    @property
    def wanted(self) -> frozenset[str]:
        return self.required | self.attached


def agent_features(
    doc: Agent,
    rev: AgentSpec,
    providers: Mapping[str, LLMProvider],
    *,
    web: bool = False,
    in_pod_approvals: bool = False,
) -> AgentFeatures:
    """이 에이전트의 `required`·`attached` 를 센다. 그래프가 쓰는 기능과 저장된 결정이 렌더를 바꾼
    기능이 `required`, 플랫폼이 에이전트마다 붙이는 기능이 `attached` 다. Skill 을 붙인 그래프는
    `skills` 를, 핀한 Tool 에 웹 Tool 이 있는 그래프(web, `agent_web.uses_web`)는 `web_guard` 를
    요구하고, 웹 내용 뒤의 쓰기를 묻는 `tool_safety` 도 요구한다. 웹 Tool 은 바닥 없이 돌면 안
    되므로 빼지 않는다. 코드 실행을 켠 그래프는 `code_execution` 을 요구한다(P40 9절). 문서에
    실리는 모델의 provider 가운데
    업체 선호가 있는 것이 있으면 `provider_extra_body` 를, 스트림 사용량을 끈 openai 계열이 있으면
    `provider_stream_usage` 를 붙인다(P37). 세는 자리는 붙이는 함수가 칸을 싣는 자리와 같다
    (`compiled_model_providers`). `tool_safety` 는 웹 Tool 이 없는 그래프에도 붙인다(P43 8절).
    판정 모델을 붙인 그래프는 `injection_check` 와 `tool_safety` 를 요구한다(P43 7절).
    `in_pod_state` 는 in_pod_approvals 일 때만 붙인다. 서비스가 미리 보기마다 정한다(P44b 7절,
    `AgentService._preview_asks`)."""
    del doc
    required = {SKILLS} if skill_pins(rev) else set()
    if web:
        required |= {WEB_GUARD, TOOL_SAFETY}
    if code_execution_of(rev) is not None:
        required.add(CODE_EXECUTION)
    if uses_injection_check(rev):
        required |= {INJECTION_CHECK, TOOL_SAFETY}
    used = [p for n in compiled_model_providers(rev) if (p := providers.get(n)) is not None]
    attached = {
        PROVIDER_EXTRA_BODY: any(p.extra_body for p in used),
        PROVIDER_STREAM_USAGE: any(_streamless(p) for p in used),
        # 첨부는 어느 그래프에든 올 수 있으므로 모든 에이전트가 바깥 표시를 받는다(P43 8절)
        TOOL_SAFETY: TOOL_SAFETY not in required,
        IN_POD_STATE: in_pod_approvals,
    }
    return AgentFeatures(
        required=frozenset(required),
        attached=frozenset(name for name, on in attached.items() if on),
    )


def requiring_redis_tls(wanted: AgentFeatures) -> AgentFeatures:
    """평문 포트가 닫힌 Redis 를 바인딩한 에이전트의 요구 기능이다 (P61 5절 5단계). TLS 끝점밖에
    없어 `redis_tls` 를 모르는 이미지는 붙지 못하므로 그 이름을 `required` 에 더한다. 문서 기능이
    아니라서 문서의 `features` 에는 실리지 않는다(`attach_features`)."""
    return replace(wanted, required=wanted.required | {REDIS_TLS})


def requiring_llm_gateway(wanted: AgentFeatures) -> AgentFeatures:
    """게이트웨이 경로로 컴파일하는 문서의 요구 기능이다 (P38 6절). 렌더가 키 env 를 토큰으로
    바꿨으므로 `llm_gateway` 를 모르는 이미지는 init 받기에서 409 로 멈춰야 한다."""
    return replace(wanted, required=wanted.required | {LLM_GATEWAY})


def requiring_in_pod_state(wanted: AgentFeatures) -> AgentFeatures:
    """평가 미리 보기의 요구 기능이다 (P45 9절). 잡은 쓰기 Tool 을 파드 안의 승인에서 `respond` 로
    답하므로, 이 이름을 모르는 이미지는 init 받기에서 409 로 멈춰야 한다. 붙이는 쪽에서는 뺀다."""
    return replace(
        wanted,
        required=wanted.required | {IN_POD_STATE},
        attached=wanted.attached - {IN_POD_STATE},
    )


def _streamless(provider: LLMProvider) -> bool:
    """스트림 사용량을 묻지 않는 provider 인가. openai SDK 계열만 그 설정이 있다."""
    return provider.sdk_kind == "openai" and not provider.stream_usage


@dataclass(frozen=True)
class FeatureCheck:
    """요구 기능을 이미지가 아는 이름에 댄 결과다. `allowed` 가 붙이는 함수에 허락된 기능이다."""

    allowed: frozenset[str]
    missing: tuple[str, ...]
    dropped: tuple[str, ...]

    @property
    def warnings(self) -> list[Issue]:
        """빠진 `attached` 의 경고다. `runtime_feature_dropped` 하나로 모은다."""
        if not self.dropped:
            return []
        return [_warning(RUNTIME_FEATURE_DROPPED, f"dropped: {', '.join(self.dropped)}")]


def _warning(code: str, message: str) -> Issue:
    return Issue(code=code, message=message, level="warning")


def check_features(wanted: AgentFeatures, known: frozenset[str]) -> FeatureCheck:
    """`required` 가 빠지면 `missing`, `attached` 가 빠지면 빼서 `dropped` 다."""
    missing = tuple(sorted(wanted.required - known))
    dropped = tuple(sorted(wanted.attached - known))
    allowed = wanted.required | (wanted.attached & known)
    return FeatureCheck(allowed=frozenset(allowed), missing=missing, dropped=dropped)


def deploy_check(
    doc: Agent, image: str, wanted: AgentFeatures
) -> tuple[FeatureCheck | None, list[Issue]]:
    """배포·재배포 전 검사다(3절 표). 배포할 이미지의 기록이 있으면 그 이름에 대고, 없으면(첫 배포,
    이미지를 바꾼 배포) 판정을 init 에 맡긴다. 그때 경고는 `required` 가 있을 때만이다. `attached`
    는 init 이 이미지가 알린 이름으로 빼므로 배포 앞에 알릴 것이 없다(모든 에이전트에 붙는
    `tool_safety` 가 첫 배포마다 경고를 내지 않게). 요구 기능이 없으면 확인할 것이 없어 경고도 없다.
    (검사 결과 또는 None, 경고)."""
    if not wanted.wanted:
        return None, []
    record = doc.runtime_for(image)
    if record is None:
        if not wanted.required:
            return None, []
        unknown = _warning(
            RUNTIME_FEATURES_UNKNOWN,
            "the runtime image has not reported its features yet; the init container checks "
            "them when the pod starts",
        )
        return None, [unknown]
    check = check_features(wanted, frozenset(record.features))
    return check, check.warnings


def snapshot_allowed(doc: Agent, wanted: AgentFeatures) -> frozenset[str]:
    """스냅숏 이미지의 기록으로 허락된 문서 기능이다. provision 이 설정 지문에 넣는 agent.json 을
    이것으로 컴파일해 init 이 받을 문서와 같게 한다(P35 13절). 기록이 없으면(첫 배포, 이미지를 바꾼
    배포) 요구 기능 전부다."""
    record = doc.runtime_for_image
    if record is None:
        return wanted.wanted
    return check_features(wanted, frozenset(record.features)).allowed


def missing_detail(missing: tuple[str, ...]) -> str:
    return f"the runtime image does not know the required features: {', '.join(missing)}"


@dataclass(frozen=True)
class ReportedFeatures:
    """init 머리를 읽은 결과다. 이름 머리가 없거나 틀리면 `names` 는 빈 목록이다(옛 이미지와
    같다)."""

    image: str | None
    names: frozenset[str]
    warnings: tuple[Issue, ...] = ()


def _feature_header_names(raw: str) -> list[str] | None:
    """콤마로 나뉜 이름 머리를 푼다. 길이·개수·꼴이 틀리면 None 이다."""
    if len(raw.encode("utf-8")) > MAX_HEADER_BYTES:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if len(names) > MAX_FEATURES or any(not NAME_RE.match(name) for name in names):
        return None
    return names


def parse_reported(headers: Mapping[str, str]) -> ReportedFeatures:
    """머리 둘을 읽는다. 플랫폼이 아는 이름만 남기고 모르는 이름은 적지 않는다."""
    image = (headers.get(IMAGE_HEADER) or "").strip() or None
    raw = headers.get(FEATURES_HEADER)
    if raw is None:
        return ReportedFeatures(image=image, names=frozenset())
    names = _feature_header_names(raw)
    if names is None:
        warning = _warning(
            RUNTIME_FEATURES_INVALID, "the init container sent a malformed feature header"
        )
        return ReportedFeatures(image=image, names=frozenset(), warnings=(warning,))
    return ReportedFeatures(image=image, names=frozenset(names) & KNOWN)


def runtime_record(reported: ReportedFeatures, check: FeatureCheck) -> AgentRuntime | None:
    """문서에 적을 기록이다. 이미지 머리가 없으면(옛 curl 렌더) 어느 이미지의 것인지 몰라 적지
    않는다. 경고는 머리 읽기의 것과 `attached` 를 뺀 것이다."""
    if reported.image is None:
        return None
    return AgentRuntime(
        image=reported.image,
        features=sorted(reported.names),
        warnings=issue_lines([*reported.warnings, *check.warnings]),
        reported_at=utcnow(),
    )


def attach_features(document: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """허락된 문서 기능을 `features` 로 싣는다. 비면 키를 내지 않아 옛 이미지가 그대로 돈다. 뒤 판의
    붙이는 함수(칸을 더하는 것)는 여기서 `allowed` 를 보고 그 기능의 칸만 싣는다."""
    names = sorted(allowed & frozenset(DOCUMENT_FEATURES))
    if names:
        return {**document, "features": names}
    return document


def attach_provider_extra_body(
    document: dict[str, Any], providers: Mapping[str, LLMProvider]
) -> dict[str, Any]:
    """`provider_extra_body` 가 허락됐을 때의 붙이는 함수다. 모델 자리(`model_places`)의
    provider 에 그 provider 의 `extra_body` 를 싣는다. 업체 선호가 없는 provider 는 그대로 둔다.
    받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    for place, provider in _model_providers(final, providers):
        if provider.extra_body:
            place["extra_body"] = provider.extra_body
    return final


def attach_provider_stream_usage(
    document: dict[str, Any], providers: Mapping[str, LLMProvider]
) -> dict[str, Any]:
    """`provider_stream_usage` 가 허락됐을 때의 붙이는 함수다(P37). 모델 자리의 openai 계열
    provider 가운데 스트림 사용량을 끈 것에 `stream_usage: false` 를 싣는다. 허락되지 않은 이미지는
    stream_options 를 보내지 않으므로 싣지 않아도 해가 없다. 받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    for place, provider in _model_providers(final, providers):
        if _streamless(provider):
            place["stream_usage"] = False
    return final


def attach_llm_gateway(document: dict[str, Any]) -> dict[str, Any]:
    """`llm_gateway` 가 허락됐을 때의 붙이는 함수다 (P38 6절). 모델 자리와 knowledge 항목의 임베딩
    provider 가운데 stub 이 아닌 것에 표식 `gateway: true` 를 싣는다. 런타임은 그 호출을 게이트웨이
    경로로 적고 토큰을 Bearer 로 보낸다. 받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    places = [model["provider"] for model in model_places(final)]
    places += [item["embedding"]["provider"] for item in final.get("knowledge") or []]
    for place in places:
        if place["kind"] != "stub":
            place["gateway"] = True
    return final


def attach_code_execution(document: dict[str, Any], rev: AgentSpec) -> dict[str, Any]:
    """`code_execution` 이 허락됐을 때의 붙이는 함수다(P40 9절). 메인의 execute 승인 결정을 최상위
    `code_execution` 으로 싣는다. 런타임이 메인과 모든 서브에이전트의 `interrupt_on["execute"]` 로
    컴파일한다. 받은 문서는 바꾸지 않는다."""
    code = code_execution_of(rev)
    if code is None:
        return document
    return {**document, "code_execution": {"approval": list(code.approval)}}


def model_places(document: dict[str, Any]) -> list[dict[str, Any]]:
    """문서의 모델 자리(main.model·main.fallback·subagents[].model, 가드레일의 판정 모델)다.
    붙이는 함수가 provider 칸을 고칠 수 있게 문서 안의 객체 그대로다."""
    main = document["main"]
    models = [main["model"], main.get("fallback"), *(s["model"] for s in document["subagents"])]
    models += [
        rule["value"]["model"]
        for group in document.get("guardrails") or []
        for rule in group["rules"]
        if rule["kind"] == RULE_INJECTION_CHECK
    ]
    return [model for model in models if model is not None]


def _model_providers(
    document: dict[str, Any], providers: Mapping[str, LLMProvider]
) -> list[tuple[dict[str, Any], LLMProvider]]:
    """문서의 모델 자리(`model_places`)마다 (provider 칸, 등록된 provider). 등록에 없는 이름은
    뺀다."""
    return [
        (model["provider"], provider)
        for model in model_places(document)
        if (provider := providers.get(model["provider"]["name"]))
    ]


def finish_document(
    document: dict[str, Any], allowed: frozenset[str], ctx: GraphContext, rev: AgentSpec
) -> dict[str, Any]:
    """붙이는 차례의 끝이다. 허락된 기능의 칸만 붙이고, 기능 칸을 얹은 최종 문서를 스키마에 한 번
    검사한다. `ctx`·`rev` 는 그 문서를 컴파일한 문맥과 리비전이다."""
    final = document
    if PROVIDER_EXTRA_BODY in allowed:
        final = attach_provider_extra_body(final, ctx.providers)
    if PROVIDER_STREAM_USAGE in allowed:
        final = attach_provider_stream_usage(final, ctx.providers)
    if SKILLS in allowed:
        final = attach_skills(final, rev, ctx.skill_revisions)
    if WEB_GUARD in allowed:
        final = attach_web(final, rev, ctx)
    if TOOL_SAFETY in allowed:
        final = attach_tool_safety(final, rev, ctx)
    if CODE_EXECUTION in allowed:
        final = attach_code_execution(final, rev)
    if LLM_GATEWAY in allowed:
        final = attach_llm_gateway(final)
    final = attach_features(final, allowed)
    problems = check_schema(final)
    if problems:
        raise CompileError(problems)
    return final


def status_runtime(doc: Agent) -> tuple[dict[str, Any] | None, list[str]]:
    """상태 응답의 `runtime` 칸과 마지막 init 받기가 남긴 경고다. 같은 이미지의 기록만 보인다."""
    record = doc.runtime_for_image
    if record is None:
        return None, []
    return {"image": record.image, "features": list(record.features)}, list(record.warnings)
