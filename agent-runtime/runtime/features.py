"""이 런타임 이미지가 아는 기능 이름이다(D65).

agent.json 의 판 사다리는 "3" 에서 멈추고 새 런타임 기능은 이름으로 선언한다(D60 R3). 이름은
소문자 스네이크 하나이고 두 종류다. 문서 기능(DOCUMENT_FEATURES)은 agent.json 의 선택 키 `features`
에 실리는 것이고, 런타임 능력(CAPABILITIES)은 런타임이 내는 API·이벤트라 문서에 실리지 않는다.
KNOWN 은 둘의 합이다. 플랫폼의 `services/agent_features.py` 가 같은 두 목록을 두고 파리티 테스트가
두 파일을 맞댄다.

init 컨테이너의 받기 스크립트(플랫폼이 렌더한다)가 이 모듈만 읽어 이름 머리
`X-Daegam-Runtime-Features` 를 싣는다. 그래서 표준 라이브러리만 쓴다. 이 모듈이 없는 옛 이미지는
머리를 싣지 않고 플랫폼은 그것을 빈 목록으로 본다.
"""

from __future__ import annotations

import re
from typing import Any

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
NAME_RE = re.compile(NAME_PATTERN)

# 진행 중 턴을 멈추는 API `POST /sessions/{sid}/cancel` 이다(D65, routes.py)
CANCEL = "cancel"
# 세션 제목과 소유자의 이름 바꾸기·지우기·검색이다(P36, sessions_api.py 와 `GET /sessions?q=`)
SESSION_ADMIN = "session_admin"
# 마지막 턴의 다시 생성과 고쳐 보내기 `POST /sessions/{sid}/regenerate` 다(P36, regenerate.py)
REGENERATE = "regenerate"
# 검색 결과의 인용 번호(`cite`)와 SSE `citations` 이벤트, 전사 도구 카드의 `citations` 칸이다
# (P36, citations.py). 플랫폼의 인용 원문 라우트가 이 이름을 보고 런타임 전사를 읽는다
CITATIONS = "citations"
# 호출자 자신의 장기 기억 파일 목록·읽기·하나 지우기 `GET /memories`, `GET`·`DELETE
# /memories/files/{key}` 다(P36, memories_api.py)
MEMORY_FILES = "memory_files"
# `/chat` 본문의 `attachments`(문서 글과 이미지)와 `done.attachments_received` 다(P36,
# attachments.py·uploads.py). 옛 런타임은 모르는 필드를 버리므로 플랫폼이 이 이름을 보고 첨부를
# 싣는다
ATTACHMENTS = "attachments"
# 세션 파일의 목록과 내려받기 `GET /sessions/{sid}/files`, `GET /sessions/{sid}/files/{path}`
# 다(P36, session_files.py). 사용자가 업로드한 첨부와 에이전트가 파일 도구로 쓴 파일이다
SESSION_FILES = "session_files"
# Redis 바인딩의 `<PREFIX>_TLS`·`<PREFIX>_CA_FILE` 과 `rediss://` URL 을 읽어 TLS 로 붙는다(P61,
# dsn.py 의 resolve_redis). 플랫폼은 이 이름을 알린 이미지에 TLS 끝점을 렌더하고, 평문 포트가 닫힌
# Redis 는 이미지와 상관없이 TLS 끝점으로 렌더하며 이 이름을 요구한다
REDIS_TLS = "redis_tls"
# 모델 호출·도구 실행 스팬(`model_call`·`tool_exec`)과 상관 헤더다(P37, trace.py). 문서에 실리지
# 않고, 이 이름을 모르는 이미지는 플랫폼의 지표 탭과 사용량 화면이 재배포를 권한다
TELEMETRY = "telemetry"
# 모델 provider 의 `extra_body` 를 ChatOpenAI 로 넘긴다(T19, models.py). OpenRouter 업체 선호가
# 이것이다
PROVIDER_EXTRA_BODY = "provider_extra_body"
# 붙인 Skill 이다(P39, D64). 문서에 최상위 `skills[]` 와 에이전트별 `skills` 가 실리고, init 단계
# `init_steps/skills.py` 가 파일을 받아 두면 skills.py 가 `/skills/` 아래 읽기 전용으로 보인다
SKILLS = "skills"
# 꺼 둔 provider 의 `stream_usage: false` 다(P37). 그 provider 의 호출은 스트림 사용량을 묻지 않고
# 글자 수로 어림한다. 플랫폼이 provider 설정에서 붙이는 기능이라 모르는 이미지는 빼도 돈다
PROVIDER_STREAM_USAGE = "provider_stream_usage"
# 플랫폼 웹 Tool 의 안전 바닥이다(P39b, D64). 문서에 최상위 `web_data_tools` 와 소스별 `tools[].web`
# 이 실리고, web_guard.py 의 가드가 fetch 의 출처·막는 목록·허용 목록과 한도를 본다. 모르는
# 이미지에서 웹 Tool 이 바닥 없이 돌면 안 되므로 빼지 않는다
WEB_GUARD = "web_guard"
# Tool 결과의 바깥 표시와 그 뒤의 승인이다(P43, tool_safety.py). 문서에 소스별 `tools[].inside`·
# `tools[].read` 와 Knowledge 항목별 `outside` 가 실리고, 런타임이 바깥 결과를 감싸 세션에 표시하며
# 표시된 대화의 쓰기를 묻는다. 모르는 이미지에서 그 규칙 없이 돌면 안 되므로 빼지 않는다
TOOL_SAFETY = "tool_safety"

# 바깥 결과의 판정 모델이다(P43 7절, judge.py). 문서의 position tool 가드레일에 규칙
# `injection_check` 가 실리고, 메인과 서브에이전트마다 판정 모델 층이 바깥 결과를 모델보다 먼저
# 판정 모델에 보인다. 모르는 이미지는 그 규칙을 스키마 위반으로 보므로 빼지 않는다
INJECTION_CHECK = "injection_check"

# LLM 게이트웨이 경로다(P38 6절). 문서의 provider 에 `gateway: true` 가 실리고 base_url 이
# 게이트웨이 경로이며, 키 env 가 provider 키가 아니라 90일 토큰을 가리킨다. 모르는 이미지는 토큰을
# 키로 보내므로 빼지 않는다
LLM_GATEWAY = "llm_gateway"

# 상태가 파드 하나에 산다(P44b 7절, tool_safety.py). 플랫폼은 늘 파드 하나로 도는 미리 보기
# 가운데 원본에 memory 노드가 있던 것의 문서에만 이 이름을 싣는다. memory 노드가 없어도 그 파드의
# 상태가 Agent 의 상태 전부라 승인 대기를 같은 파드에서 재개할 수 있으므로, tool_safety 가 승인이
# 필요한 호출을 거절하지 않고 묻는다. 플랫폼은 이 이름을 모르는 이미지에는 싣지 않고, 그 미리
# 보기는 memory 노드가 없는 Agent 처럼 그 호출을 거절한다
IN_POD_STATE = "in_pod_state"

# 코드 실행이다(P40, D95, sandbox.py). 문서에 최상위 `code_execution: {approval}` 이 실리고, 파일
# 백엔드의 기본이 세션마다의 샌드박스가 되며 execute Tool 이 보인다. 모르는 이미지는 그 Agent 를
# 돌릴 수 없으므로 빼지 않는다
CODE_EXECUTION = "code_execution"

# 턴 위임 토큰(P53 6절, turn_grants.py)이다. `/chat`·결정·다시 생성 본문의 `turn_grant` 를 받아
# 그 턴의 Tool 을 사람의 토큰으로 부르고 갱신하며 끝낸다. 플랫폼은 이 이름을 알린 이미지에만 위임을
# 싣는다. 옛 이미지는 모르는 칸을 버리기 때문이다
TURN_TOKENS = "turn_tokens"

# 요청과 떨어진 턴이다(P78, D92, turn_task.py). 연결이 끊겨도 턴이 끝까지 돌고, 세션 한 줄의
# running, 가벼운 읽기(view=progress), 시도를 이름으로 멈추는 중지, 인스턴스의 모든 턴을 멈추는
# `/turns/cancel` 이 있다. 플랫폼은 이 이름을 알린 이미지에만 그것들을 쓰고, 응답마다의
# `X-Daegam-Detached-Turn` 으로 그 턴을 돌린 파드가 이 판인지 본다
DETACHED_TURNS = "detached_turns"

# 거절된 Tool 호출이다(P52 5절, mcp.py). 도구 소스의 403·429 가 턴을 끝내지 않고 모델이 보는 Tool
# 오류가 된다. 플랫폼은 이 이름이 없는 이미지가 규칙이 있는 Tool 을 쓰면 `runtime_tool_refusals` 를
# 경고한다
TOOL_REFUSALS = "tool_refusals"

# 백그라운드 실행의 런타임 쪽이다(P41 9절, routes.py·turn.py). `/chat` 이 플랫폼 경로에서만
# `run_id`·`untrusted` 를 받아 세션에 두고, 같은 run_id 의 둘째 `/chat` 을 409 `run_exists` 로
# 답하며, 실행 세션의 턴은 늘 임시로 돌고 승인 대기 없이 끝나면 세션을 닫는다.
# `GET /sessions?runs=` 와 턴 이벤트의 `run_id` 도 있다. 플랫폼의 시작 워커는 답의
# `X-Daegam-Run-Id` 로 이 판인지 본다
RUNS = "runs"

# Agent 를 0 으로 줄이기의 런타임 쪽이다(P44 7·11절). 인스턴스의 drain 표시를 세우고 지우는
# `POST`·`DELETE /drain` 과 세 색인 모두에서 표시가 선 동안 503 `agent_draining` 으로 물리는 리스
# 잡기, 기동하자마자 도는 첫 sweep 과 장기 기억의 만료 정리, TTL 이 지난 세션을 닫힌 것으로 읽기다.
# 플랫폼은 이 이름을 알린 이미지의 Agent 만 재운다. 요구하지 않고 알리기만 한다(R3)
SCALE_TO_ZERO = "scale_to_zero"

# 노출한 Agent 의 두 발급자다(P53b 6.8절, auth.py). 데이터 플레인이 믿는 발급자(`DAEGAM_OIDC_*`)
# 곁에 IdP(`DAEGAM_IDP_*`)를 두고 토큰의 iss 로 골라 발급자마다의 계약으로 판정한다. 플랫폼을 믿는
# oidc 배포는 이 이름을 알린 이미지의 노출 Agent 에만 플랫폼을 첫 발급자로 렌더하고 IdP 를 곁에
# 둔다. 옛 이미지는 IdP 만 받는다
TWO_ISSUERS = "two_issuers"

# 다른 Agent 를 Tool 로 부르기다(agent_calls.py). 문서의 `tools[]` 에 `source.kind` 가 `agent` 인
# 항목이 실리고, 런타임이 목록을 읽지 않고 그 항목으로 `ask_*` Tool 을 만들어 플랫폼의 호출 경로로
# 부른다. 모르는 이미지는 그 항목을 스키마 위반으로 보므로 빼지 않는다
AGENT_TOOLS = "agent_tools"

# agent.json 의 features[] 에 실리는 문서 기능
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
    AGENT_TOOLS,
)
# 문서에 실리지 않는 런타임 능력(API·이벤트)
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

# 문서의 features 에 모르는 이름이 있을 때의 오류 코드다. 플랫폼의 init 받기 409 와 같은 이름이라
# 어느 쪽이 막았든 같은 이유로 읽힌다
UNKNOWN_FEATURE_CODE = "runtime_image_too_old"


def unknown_features(doc: Any) -> list[str]:
    """문서의 `features` 가운데 이 이미지가 모르는 이름이다. 스키마 검사 전에 부르므로 문서 모양을
    믿지 않는다. 키가 없거나 목록이 아니면 빈 목록이다(모양은 스키마 검사가 가른다)."""
    if not isinstance(doc, dict):
        return []
    names = doc.get("features")
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if name not in KNOWN]


def runtime_report() -> dict[str, Any]:
    """`/healthz`·`/metrics` 가 싣는 `runtime` 칸이다. 확인용이고 판정은 init 받기 때의 기록으로
    한다."""
    return {"features": sorted(KNOWN)}
