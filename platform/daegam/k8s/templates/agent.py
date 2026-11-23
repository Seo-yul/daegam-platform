"""Agent 의 Deployment·Service·NetworkPolicy 와 Secret 렌더 (D26, resource-model.md 5절).

런타임은 `agent-runtime` 고정 이미지다. 설정 전달은 D4 와 같아서 initContainer 가 인스턴스
토큰으로 `GET /api/agents/{id}/agent.json` 을 받아 emptyDir `/config/agent.json` 에 두고,
메인 컨테이너는 그 파일과 env 만 읽는다. 비밀은 전부 Secret 참조(secretKeyRef)다.

initContainer 는 그 Agent 의 런타임 이미지로 돌며(D65) ENTRYPOINT 를 `python -c FETCH_SCRIPT` 로
덮는다. 스크립트는 기능에 따라 바뀌지 않는 일반형이다. 이미지가 `runtime.features.KNOWN` 을 알면
이름 머리 `X-Daegam-Runtime-Features` 를, env `DAEGAM_AGENT_IMAGE` 를 머리 `X-Daegam-Runtime-Image`
로 싣는다. P35 전 이미지는 그 모듈이 없어 이름 머리를 싣지 않는다. `agent.json` 은
`agent_json_url` 이 정한 주소(env `DAEGAM_AGENT_CONFIG_URL`)로 받고, 받기가 성공하면 문서
`features` 의 이름마다 이미지 안 `runtime.init_steps.<이름>` 이 있을 때 `run(config_dir, fetch)` 를
부른다. `fetch` 는 설정 경로의 뿌리(env `DAEGAM_CONFIG_BASE_URL`) 아래를 연다. 받기는 리다이렉트를
따르지 않는다(인스턴스 토큰은 플랫폼 소유 비밀이다). 받지 못하면 응답의 `detail.code` 를 표준 오류와
`/dev/termination-log` 에 쓰고 멈춘다. 준비 검사는 `/healthz?probe=ready` 라 모르는 기능을 받은
파드는 Ready 가 되지 않는다. MCPServer 의 init 은 curl 그대로다.

- provider 키: provision 이 플랫폼 네임스페이스의 `api_key_secret` 을 대상 네임스페이스
  `agent-<name>-llm-<provider>` 로 복사해 두고, 여기서는 그 이름을 provider 이름이 정하는 env
  `DAEGAM_LLM_<SLUG>_API_KEY`(`provider_key_env`, P35 12절)에 참조한다. 같은 kind 의 provider 둘도
  env 가 다르고 리비전의 provider 차례와 상관없다. 문서의 표 `llm_env{provider 이름: env 이름}` 이
  그 이름의 기록이라 렌더와 init 때의 컴파일이 같은 표를 읽는다(`key_env_of`). 렌더에
  `<KIND>_BASE_URL` 은 없다. 런타임은 `agent.json` 의 `base_url` 을 읽고, SDK 가 그 env 를 기본
  주소로 읽으면 base_url 없는 같은 kind 의 provider 가 남의 주소로 제 키를 보내기 때문이다
- 임베딩 provider 키(D32): knowledge 항목마다 같은 복사본 Secret 을 `DAEGAM_KNOWLEDGE_<n>_API_KEY`
  (n 은 agent.json `knowledge[]` 순서) 로 참조하고 `DAEGAM_KNOWLEDGE_<n>_BASE_URL` 은 base_url 이
  있을 때만 있다. stub 은 둘 다 없다. 채팅 provider 와 같은 kind 여도 env 이름이 겹치지 않는다
- LLM 게이트웨이 경로(P38 6절): 문서의 `llm_route` 가 gateway 면 키 env 이름은 그대로이고 인스턴스
  토큰 Secret 의 `llm_token` 을 가리킨다. `_BASE_URL` env 는 싣지 않는다. init 주소에 `?llm=gateway`
  를 붙이고 파드 템플릿에 라벨 `daegam.devops.ai.kr/llm-route: gateway` 를 단다. 직접 경로의 렌더는
  이 단계 전과 같다
- 인스턴스 토큰: initContainer 의 Bearer 이자 플랫폼과 런타임이 나눠 갖는 비밀
  `DAEGAM_INSTANCE_TOKEN`
- 기록 보존 기간(D46): 닫힌 대화의 전사가 남는 날 수 `DAEGAM_TRANSCRIPT_RETENTION_DAYS` 다. 값은
  플랫폼 ConfigMap 의 같은 이름 키에서 오고 그래프 스키마와 `agent.json` 에는 없다. 세션 TTL 은
  Chat 입구 노드가 정하는 다른 값이다
- agent_oauth: oidc 모드에서 Gateway 도구 소스를 쓸 때 client credentials. secret 은 Tenant Settings
  `agent_oauth.secret_name` 의 `client_secret` 키다
- 서비스 토큰(D41, P50 9절): MCP Server 소스마다 토큰 하나가 그 서버의 audience 를 싣고, 인스턴스
  토큰 Secret 의 키이자 env 인 `DAEGAM_MCP_TOKEN_<서버 id>`(이름 규칙은 `mcp_token_env`)로
  내려간다. 문서의 표(`Agent.mcp_token_tables`)가 그 env 들의 기록이고, init 은 env
  `DAEGAM_MCP_TOKEN_TABLE` 의 표 지문을 머리 `X-Daegam-Mcp-Token-Table` 로 보내 init 때의 컴파일이
  그 파드가 가진 env 로 소스마다의 토큰을 고른다. 키 `mcp_token`(env `DAEGAM_MCP_TOKEN`)은 테넌트
  audience 하나인 옛 단일 토큰이고, 표가 생기기 전 파드와 표에 없는 소스가 쓴다. local 모드에서
  Gateway 소스를 쓰면 키 `gateway_token` 이 `DAEGAM_GATEWAY_TOKEN` 으로 더 내려간다. 파드 템플릿
  어노테이션 `daegam.devops.ai.kr/svc-token-rev` 가 그 토큰들의 판이라 바뀌면 파드가 굴러간다
- 설정 지문(P35 13절): secretKeyRef 와 `agent.json` 은 컨테이너가 뜰 때만 읽히므로 같은 리비전의
  재배포는 그것만으로 굴러가지 않는다. provision 이 키를 복사하고 `agent.json` 을 컴파일한 뒤
  둘의 키 없는 지문을 문서 `config_digest` 로 남기고, 파드 템플릿 어노테이션
  `daegam.devops.ai.kr/config-sha` 는 그것을 인스턴스 토큰으로 건 HMAC 의 앞 16자다(`config_sha`).
  키·`agent.json`·인스턴스 토큰 가운데 하나라도 바뀌면 값이 바뀌어 파드가 굴러가고, 입력이 같으면
  굴러가지 않는다. Advanced 편집이 보는 값이라 키 없는 해시가 아니라 HMAC 이고, 토큰이 없는 확인용
  렌더는 "0" 을 싣는다
- Database 바인딩: MCP Server 와 같은 `binding_env_and_secrets` 규칙
  (`<PREFIX>_HOST/PORT/DATABASE/USER/URL`)을 그대로 쓰되 비밀번호는 secretKeyRef 이고,
  `<PREFIX>_URL` 은 K8s env 의존 확장
  `$(<PREFIX>_PASSWORD)` 로 자격을 품는다. 그 URL 은 비밀번호에 예약 문자가 있으면 깨지므로
  런타임(`agent-runtime/runtime/sessions.py`)은 개별 env 를 우선해 conninfo 를 조립하고 `_URL`
  은 개별 env 가 없을 때의 폴백이다. 접두어는 memory 가 `DAEGAM_DB_0`, knowledge 노드의 Database 가
  그 뒤 `DAEGAM_DB_<i>` 이고 같은 Database 는 한 번이다
- 외부 노출(D27): 노출을 켠 인스턴스는 Ingress(`AGENT_EXPOSE_PATHS` → `agent-<name>:8080`)와
  NetworkPolicy `daegam-expose-<name>` 을 더 갖고, 파드 env 로 `DAEGAM_OIDC_ISSUER`·
  `DAEGAM_OIDC_AUDIENCE`(데이터 플레인 audience)·`DAEGAM_CORS_ORIGINS`·`DAEGAM_ALLOWED_GROUPS`·
  `DAEGAM_AGENT_CLIENT_IDS` 를 받아 인바운드 토큰을 직접 검증한다. 플랫폼을 믿는 oidc 모드에서
  두 발급자를 알린 이미지는 플랫폼을 이 env 로, IdP 를 `DAEGAM_IDP_*` 로 받는다(P53b 6.8절). 두
  노출 객체의 ownerReference 는 `apps/v1 Deployment agent-<name>` 이다 (ToolHive CR 이 아니다)
- 턴 drain(P78 5절): provision 이 남긴 결정(`Agent.turn_drain_seconds`)을 env
  `DAEGAM_TURN_DRAIN_SECONDS` 와 `terminationGracePeriodSeconds`(preStop, drain, 마감 몫 45초, 여유
  10초의 합)로 싣는다. 결정이 없는 draft 의 렌더는 둘 다 싣지 않는다. grace 는 잠그지 않는다.
  Advanced overrides 가 grace 를 정하면 drain 을 그 안에 맞추므로(`fit_turn_drain`) grace 를 줄이면
  drain 이 줄거나 없어질 뿐 drain 도중에 SIGKILL 되지 않고, 늘리면 긴 턴을 그만큼 기다린다
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from daegam.auth.cedar import CLIENT_ID_CLAIM
from daegam.auth.rbac import PLATFORM_GROUP_HEAD, idp_group
from daegam.config.resolve import EffectiveConfig
from daegam.k8s.labels import (
    LABEL_ID,
    LABEL_PROFILE_REVISION,
    LABEL_RESOURCE,
    daegam_labels,
    keyed_hash,
    stable_hash,
)
from daegam.k8s.merge import DEPLOYMENT_POD, pod_spec
from daegam.k8s.templates.common import (
    CONFIG_MOUNT,
    NETWORK_POLICY_API,
    REDIS_CA_ANNOTATION,
    RenderError,
    binding_ca_volume,
    binding_env_and_secrets,
    binding_url,
    config_fetch_pod,
    named_item,
    opaque_secret,
    owner_ref,
    redis_ca_mark,
    secret_env,
    tenant_ingress_from,
    tls_endpoint,
)
from daegam.k8s.templates.exposure import (
    expose_policy_name,
    ingress_tls,
    render_expose_policy,
    render_ingress,
)
from daegam.k8s.templates.llm_gateway import LLM_ROUTE_LABEL
from daegam.k8s.templates.llm_gateway_config import gateway_provider
from daegam.k8s.templates.mcpserver import TOKEN_KEY, api_base
from daegam.k8s.templates.svc_token import SVC_TOKEN_REV_ANNOTATION
from daegam.names import provider_slug
from daegam.store.agents import Agent, McpTokenTable, SleepState
from daegam.store.clusters import ClusterIngress
from daegam.store.data_services import DataService
from daegam.store.exposure import AgentExposure
from daegam.store.settings import LLMProvider
from daegam.store.tenants import AgentOAuth
from daegam.urls import SANDBOX_PATH, TELEMETRY_EVENTS_PATH

AGENT_PORT = 8080
RESOURCE = "agent-instance"
RESOURCE_AGENT_INGRESS = "agent-instance-ingress"
RESOURCE_AGENT_POLICY = "agent-instance-expose-policy"
AGENT_CONTAINER = "agent"
INIT_CONTAINER = "fetch-agent-config"
CONFIG_VOLUME = "config"
CONFIG_FILE = "agent.json"
# init 의 받기 스크립트가 읽는 env (D65). 받을 파일 경로(런타임과 같은 env), 받을 `agent.json` 의
# 주소, 기능의 init 단계가 여는 설정 경로의 뿌리, 이 파드의 런타임 이미지 참조다
CONFIG_PATH_ENV = "DAEGAM_AGENT_CONFIG"
CONFIG_URL_ENV = "DAEGAM_AGENT_CONFIG_URL"
CONFIG_BASE_ENV = "DAEGAM_CONFIG_BASE_URL"
AGENT_IMAGE_ENV = "DAEGAM_AGENT_IMAGE"
# 플랫폼과 런타임이 나눠 갖는 인스턴스 토큰의 env. init 의 Bearer 이기도 하다
INSTANCE_TOKEN_ENV = "DAEGAM_INSTANCE_TOKEN"
# 런타임 이벤트 수집 (P37 3절). 수집 경로가 켜져 있으면 늘 싣는다
EVENTS_URL_ENV = "DAEGAM_EVENTS_URL"
POD_NAME_ENV = "DAEGAM_POD_NAME"
POD_UID_ENV = "DAEGAM_POD_UID"
# 코드 실행 Agent 가 세션의 샌드박스를 여는 기계 경로 (P40 5절, 런타임 `sandbox.SANDBOX_URL_ENV`).
# 코드 실행을 켠 리비전의 파드에만 싣는다
SANDBOX_URL_ENV = "DAEGAM_SANDBOX_URL"
# init 이 `agent.json` 을 받을 때 싣는 머리 둘 (D65). 플랫폼(`services/agent_features.py`)이 같은
# 상수로 읽는다
FEATURES_HEADER = "X-Daegam-Runtime-Features"
IMAGE_HEADER = "X-Daegam-Runtime-Image"
# init 이 자기 파드 템플릿의 MCP Server 토큰 env 표 지문을 담는 env 와 그것을 싣는 머리 (P50 9절).
# 플랫폼(`services/agents.py` `fetch_config`)이 같은 상수로 읽는다
MCP_TOKEN_TABLE_ENV = "DAEGAM_MCP_TOKEN_TABLE"
MCP_TOKEN_TABLE_HEADER = "X-Daegam-Mcp-Token-Table"
MCP_TOKEN_TABLE_DIGEST_LENGTH = 16
# MCP Server 소스마다의 토큰 env 와 Secret 키의 앞머리. 뒤에 서버 id 의 영문자와 숫자가 대문자로
# 온다(`mcp_token_env`)
MCP_TOKEN_ENV_PREFIX = "DAEGAM_MCP_TOKEN_"
# init 의 받기 한 번이 기다리는 상한(초)
FETCH_TIMEOUT_SECONDS = 30
# 살아 있음 검사 경로(runtime/routes.py HEALTHZ_PATH). 인스턴스 토큰을 실어 부르면 준비되지 않은
# 이유(`unready`)를 함께 준다(T21)
HEALTHZ_PATH = "/healthz"
# 준비 검사 경로. 런타임이 준비되지 않았으면 이 질의에만 503 이다(runtime/routes.py PROBE_READY)
READY_PROBE_PATH = f"{HEALTHZ_PATH}?probe=ready"
# 준비 검사의 첫 기다림·주기(초)와 실패 문턱 (P44 8절). 깨운 파드가 런타임이 준비된 뒤 2초 안에
# Service 에 들고, 저장소가 잠깐 흔들려도 Service 에서 빠지기까지는 지금처럼 30초쯤 걸린다
READY_PROBE_DELAY_SECONDS = 1
READY_PROBE_PERIOD_SECONDS = 2
READY_PROBE_FAILURES = 15
# SIGTERM 전에 preStop 에서 기다리는 초. 엔드포인트 제거가 퍼지는 동안에도 파드가 요청에 답한다
PRESTOP_SECONDS = 5
# 파드가 내려갈 때 런타임이 도는 턴을 기다리는 초를 받는 env (P78 5절, 런타임 `drain.DRAIN_ENV`)
TURN_DRAIN_ENV = "DAEGAM_TURN_DRAIN_SECONDS"
# drain 끝에 멈춘 턴이 마감하는 몫(런타임 `drain.SETTLE_SECONDS`)과 그 뒤의 여유다. grace 에서
# drain 을 뺀 몫은 preStop 과 이 둘의 합이다
TURN_SETTLE_SECONDS = 45
TURN_GRACE_SLACK_SECONDS = 10
TURN_GRACE_OVERHEAD = PRESTOP_SECONDS + TURN_SETTLE_SECONDS + TURN_GRACE_SLACK_SECONDS
# 쿠버네티스가 grace 를 정하지 않은 파드에 주는 값
DEFAULT_GRACE_SECONDS = 30
INIT_COMMAND: tuple[str, ...] = ("python", "-c")
LLM_SECRET_KEY = "api_key"
OAUTH_SECRET_KEY = "client_secret"
# 인스턴스 토큰 Secret 안의 서비스 토큰 키와 그것을 받는 env (D41). mcp_token 은 테넌트 audience
# 하나인 옛 단일 토큰이고, 표가 생기기 전 파드와 표에 없는 소스의 컴파일이 쓴다 (P50 9절)
MCP_TOKEN_KEY = "mcp_token"
GATEWAY_TOKEN_KEY = "gateway_token"
# LLM 게이트웨이를 부르는 90일 토큰 (P38 5절). 게이트웨이 경로의 파드만 키 env 로 받는다
LLM_TOKEN_KEY = "llm_token"
# init 주소의 경로 쿼리 (P38 6절). 없으면 직접 경로다
LLM_QUERY = "llm"
LLM_GATEWAY_ROUTE = "gateway"
# init 주소의 Gateway 소스 인증 쿼리. 그 템플릿이 env 로 실은 인증이다. bearer 는
# DAEGAM_GATEWAY_TOKEN, oauth 는 DAEGAM_AGENT_CLIENT_SECRET 이고, Gateway 소스를 인증 없이 부르는
# 템플릿에는 없다
GATEWAY_AUTH_QUERY = "gateway_auth"
GATEWAY_AUTH_BEARER = "bearer"
GATEWAY_AUTH_OAUTH = "oauth"
MCP_TOKEN_ENV = "DAEGAM_MCP_TOKEN"
GATEWAY_TOKEN_ENV = "DAEGAM_GATEWAY_TOKEN"
# agent_oauth 의 client secret 을 담는 env (docs/guide/agents.md 5절). 컴파일이 agent.json 에
# 이 이름을 싣는다
AGENT_CLIENT_SECRET_ENV = "DAEGAM_AGENT_CLIENT_SECRET"
# 노출 Agent 런타임이 발급자마다 읽는 env 의 머리 (런타임 `auth.OIDC_ENV`, `auth.IDP_ENV`). 첫째는
# 데이터 플레인이 믿는 발급자이고, 둘째는 플랫폼을 믿는 oidc 모드에서 두 발급자를 알린 이미지가
# 플랫폼 곁에 두는 IdP 다 (P53b 6.8절)
OIDC_ENV_HEAD = "DAEGAM_OIDC"
IDP_ENV_HEAD = "DAEGAM_IDP"
# 닫힌 대화의 기록이 남는 날 수 (D46). 런타임의 같은 이름 env 와 짝이고 기본값도 같다
TRANSCRIPT_RETENTION_ENV = "DAEGAM_TRANSCRIPT_RETENTION_DAYS"
DEFAULT_TRANSCRIPT_RETENTION_DAYS = 30
# provider 키 env 이름의 틀 (P35 12절). SLUG 는 관리 Secret 이름과 같은 `provider_slug` 를 대문자와
# 밑줄로 옮긴 것이다
PROVIDER_KEY_ENV_FORMAT = "DAEGAM_LLM_{slug}_API_KEY"
# 파드가 뜰 때 읽는 입력의 지문을 싣는 파드 템플릿 어노테이션 (P35 13절). 대감 차트의 플랫폼
# 파드가 ConfigMap 체크섬을 싣는 것과 같은 자리이고 길이도 같다. 값은 문서의 키 없는 지문
# (`config_digest`, sha256 전체)을 인스턴스 토큰으로 건 HMAC 의 앞자리이고, 토큰이 없는 확인용
# 렌더는 "0" 이다
CONFIG_SHA_ANNOTATION = "daegam.devops.ai.kr/config-sha"
CONFIG_SHA_LENGTH = 16
CONFIG_DIGEST_LENGTH = 64
NO_CONFIG_SHA = "0"
# memory·knowledge 노드의 Database 바인딩 env 접두어를 짓는 규칙. 이름을 짓는 곳과 읽는 곳이
# 문자열을 따로 쓰면 한쪽만 고쳐도 아무도 못 잡으므로 여기 하나만 둔다(resource-model.md 5절)
DB_PREFIX_FORMAT = "DAEGAM_DB_{index}"
LABEL_AGENT_PROFILE = "daegam.devops.ai.kr/agent-profile"
# Ingress 가 라우팅하는 경로. /memories 는 토큰 sub 의 장기 기억 보기와 하나 지우기다(P36 8절).
# /healthz·/metrics 는 인터넷에 나가지 않는다 (런타임의 축소 응답과 이중 방어다)
AGENT_EXPOSE_PATHS: tuple[str, ...] = ("/chat", "/sessions", "/memories")

_NAME_RE = re.compile(r"[^a-z0-9-]+")


def agent_config_base(platform_internal_url: str, agent_id: str) -> str:
    """이 에이전트의 설정 경로 뿌리다. 기능의 init 단계가 그 아래를 연다(D65)."""
    return f"{api_base(platform_internal_url)}/agents/{agent_id}"


def telemetry_env(platform_internal_url: str) -> list[dict[str, Any]]:
    """런타임이 관측 이벤트를 밀어 보낼 주소와 파드 이름·uid 다(P37 3절). 주소는 `agent.json` 과
    같은 뿌리라 원격 클러스터도 닿는다. 파드 이름·uid 는 downward API 에서 온다. 이 env 를 모르는 옛
    이미지는 무시하고 stdout 에만 쓴다."""
    url = f"{api_base(platform_internal_url)}{TELEMETRY_EVENTS_PATH}"
    return [
        {"name": EVENTS_URL_ENV, "value": url},
        {"name": POD_NAME_ENV, "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": POD_UID_ENV, "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
    ]


def sandbox_env(platform_internal_url: str, agent_id: str) -> dict[str, Any]:
    """런타임이 세션의 샌드박스를 열고 찾고 닫는 기계 경로다 (P40 5절). `agent.json` 과 같은 뿌리라
    원격 클러스터도 닿는다."""
    path = SANDBOX_PATH.format(agent_id=agent_id)
    return {"name": SANDBOX_URL_ENV, "value": f"{api_base(platform_internal_url)}{path}"}


def agent_json_url(
    platform_internal_url: str,
    agent_id: str,
    *,
    gateway: bool = False,
    gateway_auth: str | None = None,
) -> str:
    """init 이 받는 `agent.json` 의 주소다. 렌더가 이 값 하나를 init env 로 내리므로 주소에 무엇을
    더할 때는 여기만 고친다. 게이트웨이 경로의 템플릿은 `?llm=gateway` 를 붙인다 (P38 6절). 그
    템플릿의 파드는 다시 떠도 그 경로의 문서를 받는다. Gateway 소스의 인증을 env 로 실은 템플릿은
    그 종류를 `gateway_auth` 로 붙인다. 믿는 발급자를 바꾸는 동안 옛 모드의 플랫폼 파드가 받기에
    답해도 그 파드의 env 에 맞는 문서를 받는다."""
    url = f"{agent_config_base(platform_internal_url, agent_id)}/{CONFIG_FILE}"
    query = {LLM_QUERY: LLM_GATEWAY_ROUTE if gateway else None, GATEWAY_AUTH_QUERY: gateway_auth}
    asked = "&".join(f"{k}={v}" for k, v in query.items() if v)
    return f"{url}?{asked}" if asked else url


# init 컨테이너가 런타임 이미지 안에서 `python -c` 로 도는 받기 스크립트다(D65). 표준 라이브러리만
# 쓰고 받은 본문은 설정 볼륨에만 쓴다. 기능마다 바뀌지 않으므로 렌더는 이 글자 그대로를 싣는다.
# env·머리 이름과 시간 상한은 import 때 이 모듈의 상수로 한 번 채운다. 받기는 리다이렉트를 따르지
# 않는다. 설정 경로는 리다이렉트하지 않고 인스턴스 토큰은 플랫폼 소유 비밀이다. 플랫폼 오류 본문은
# `{"detail": {"code", "detail"}}` 이다(daegam/errors.py). 어느 실패든 `코드: 문구` 를
# termination log 에 남긴다. 연결과 읽기는 `unreachable`, 받은 문서가 JSON 객체가 아니면
# `agent_config_invalid`, 기능 단계가 스스로 남기지 못하고 멈추면 `init_step_failed` 다
_FETCH_TEMPLATE = string.Template(r"""
import importlib, importlib.util, json, os, sys, urllib.request
from http.client import HTTPException
from urllib.error import HTTPError, URLError

CONFIG_URL = os.environ["$url_env"]
BASE = os.environ["$base_env"]
OUT = os.environ["$path_env"]
CONFIG_DIR = os.path.dirname(OUT)
TERMINATION_LOG = "/dev/termination-log"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def headers():
    h = {"Authorization": "Bearer " + os.environ["$token_env"]}
    table = os.environ.get("$table_env")
    if table:
        h["$table_header"] = table
    try:
        from runtime.features import KNOWN
        h["$features_header"] = ",".join(sorted(KNOWN))
    except ImportError:
        pass
    image = os.environ.get("$image_env")
    if image:
        h["$image_header"] = image
    return h


def open_url(url):
    request = urllib.request.Request(url, headers=headers())
    return OPENER.open(request, timeout=$timeout)


def fetch(path):
    return open_url(BASE + path)


def stop(code, message):
    line = code + ": " + message
    sys.stderr.write(line + "\n")
    try:
        with open(TERMINATION_LOG, "w") as f:
            f.write(line)
    except OSError:
        pass
    sys.exit(1)


try:
    with open_url(CONFIG_URL) as response:
        body = response.read()
except HTTPError as e:
    code, message = "http_" + str(e.code), str(e)
    try:
        detail = json.loads(e.read()).get("detail") or {}
        code, message = detail.get("code") or code, detail.get("detail") or message
    except (OSError, HTTPException, ValueError, AttributeError):
        pass
    stop(code, message)
except URLError as e:
    stop("unreachable", str(e.reason))
except (OSError, HTTPException, ValueError) as e:
    # 읽다 끊긴 본문(IncompleteRead)은 OSError 가 아니라 HTTPException 이다
    stop("unreachable", repr(e))
try:
    features = json.loads(body).get("features") or []
except (ValueError, AttributeError) as e:
    stop("agent_config_invalid", repr(e))
with open(OUT, "wb") as f:
    f.write(body)
for name in features:
    module = "runtime.init_steps." + name
    try:
        found = importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        found = False
    if found:
        try:
            importlib.import_module(module).run(CONFIG_DIR, fetch)
        except Exception as e:
            stop("init_step_failed", name + ": " + repr(e))
""")
FETCH_SCRIPT = _FETCH_TEMPLATE.substitute(
    url_env=CONFIG_URL_ENV,
    base_env=CONFIG_BASE_ENV,
    path_env=CONFIG_PATH_ENV,
    token_env=INSTANCE_TOKEN_ENV,
    image_env=AGENT_IMAGE_ENV,
    features_header=FEATURES_HEADER,
    image_header=IMAGE_HEADER,
    table_env=MCP_TOKEN_TABLE_ENV,
    table_header=MCP_TOKEN_TABLE_HEADER,
    timeout=FETCH_TIMEOUT_SECONDS,
)


def mcp_token_env(server_id: str) -> str:
    """MCP Server 하나의 토큰 env 이자 Secret 키 (P50 9절). `DAEGAM_MCP_TOKEN_` 뒤에 서버 id 의
    ASCII 영문자와 숫자를 대문자로 붙이고 그 밖의 글자(대시)는 뺀다. UUID 인 id 는 16진수 32자가
    된다. agent.json 스키마의 env 이름 꼴(`^[A-Z][A-Z0-9_]*$`)과 Secret 키 꼴을 함께 지킨다."""
    digits = "".join(c for c in server_id.upper() if c.isascii() and c.isalnum())
    return f"{MCP_TOKEN_ENV_PREFIX}{digits}"


def mcp_token_table(server_ids: Iterable[str]) -> McpTokenTable:
    """MCP Server 소스들의 토큰 env 표와 그 지문 (P50 9절). 지문은 env 이름들로 정해 같은 소스면
    같은 값이고, 그 템플릿의 init 이 보내는 값이다."""
    envs = {sid: mcp_token_env(sid) for sid in sorted(set(server_ids))}
    digest = stable_hash(sorted(envs.values()), MCP_TOKEN_TABLE_DIGEST_LENGTH)
    return McpTokenTable(digest=digest, envs=envs)


def _dns_label(raw: str) -> str:
    s = _NAME_RE.sub("-", raw.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s) or "x"


def llm_secret_name_for(k8s_name: str, provider_name: str) -> str:
    """복사된 provider 키 Secret 이름 `agent-<name>-llm-<provider>`. provider 이름은 대문자·점을
    허용하므로 DNS 라벨로 낮춘다."""
    return f"{k8s_name}-llm-{_dns_label(provider_name)}"


def policy_name_for(k8s_name: str) -> str:
    return f"daegam-{k8s_name}"


def selector_labels(inst: Agent) -> dict[str, str]:
    """Deployment selector·Service selector·NetworkPolicy podSelector 가 공유하는 두 라벨."""
    return {LABEL_RESOURCE: RESOURCE, LABEL_ID: inst.id}


def config_digest(document: Mapping[str, Any], keys: Mapping[str, str]) -> str:
    """파드가 뜰 때 읽는 입력의 키 없는 지문 (P35 13절). 컴파일한 agent.json 과 복사본 Secret
    이름마다의 키 sha256 을 정렬한 JSON 으로 이어 sha256 한다. provision 이 문서 `config_digest`
    로 남기고 어노테이션은 `config_sha` 가 이것을 인스턴스 토큰으로 건 판이다."""
    return stable_hash({"agent": dict(document), "keys": dict(keys)}, CONFIG_DIGEST_LENGTH)


def config_sha(inst: Agent, instance_token: str | None) -> str:
    """파드 템플릿 어노테이션 `daegam.devops.ai.kr/config-sha` 의 값. 남긴 지문을 인스턴스 토큰으로
    건 HMAC 의 앞 16자라 키·agent.json·토큰 가운데 하나라도 바뀌면 바뀌고 같은 입력이면 같다. 토큰은
    플랫폼과 테넌트 Secret 밖으로 나가지 않으므로 Advanced 편집이 보는 이 값에 키의 흔적이 없다."""
    if not instance_token:
        return NO_CONFIG_SHA
    return keyed_hash(instance_token, inst.config_digest, CONFIG_SHA_LENGTH)


def _labels(inst: Agent) -> dict[str, str]:
    labels = daegam_labels(tenant=inst.tenant, resource=RESOURCE, id=inst.id)
    # 라벨 키와 resource 값은 그대로 둔다. 살아 있는 파드를 이 키로 거르기 때문이다 (D42).
    # 정의와 실행이 한 문서라 프로필 라벨의 값은 문서 id 이고 리비전은 배포가 핀한 것이다
    labels[LABEL_AGENT_PROFILE] = inst.id
    # 렌더는 배포 경로에서만 불리므로 핀이 있다. 없는 문서가 오면 "None" 을 라벨로 남기지 않는다
    if inst.deployed_revision is not None:
        labels[LABEL_PROFILE_REVISION] = str(inst.deployed_revision)
    return labels


# -- Secret ---------------------------------------------------------------------------


def render_agent_token_secret(
    inst: Agent,
    token: str,
    *,
    mcp_token: str,
    gateway_token: str | None = None,
    source_tokens: Mapping[str, str] | None = None,
    llm_token: str | None = None,
) -> dict[str, Any]:
    """인스턴스 토큰 Secret (render_token_secret 선례). 테넌트 네임스페이스에 둔다. 서비스 토큰
    (옛 단일 `mcp_token`, local 모드의 `gateway_token`, MCP Server 소스마다의 토큰, LLM 게이트웨이의
    `llm_token`)도 같은 Secret 의 키다 (D41, P50 9절, P38 5절). source_tokens 는 env 이름(= 키) →
    토큰이다."""
    data = {TOKEN_KEY: token, MCP_TOKEN_KEY: mcp_token}
    if gateway_token:
        data[GATEWAY_TOKEN_KEY] = gateway_token
    if llm_token:
        data[LLM_TOKEN_KEY] = llm_token
    data.update(source_tokens or {})
    return opaque_secret(
        inst.k8s.token_secret,
        inst.k8s.namespace,
        data,
        tenant=inst.tenant,
        resource="agent-instance-token",
        id=inst.id,
    )


def render_llm_secret(inst: Agent, provider_name: str, api_key: str) -> dict[str, Any]:
    """플랫폼 네임스페이스 provider 키의 복사본. 키 항목은 원본과 같은 `api_key` 다."""
    return opaque_secret(
        llm_secret_name_for(inst.k8s.name, provider_name),
        inst.k8s.namespace,
        {LLM_SECRET_KEY: api_key},
        tenant=inst.tenant,
        resource="agent-instance-llm",
        id=inst.id,
    )


# -- env -----------------------------------------------------------------------------


def provider_key_env(provider_name: str) -> str:
    """provider 이름이 정하는 파드의 키 env 이름 `DAEGAM_LLM_<SLUG>_API_KEY` (P35 12절). 이름이
    다른 provider 는 kind 가 같아도 env 가 다르고, 리비전의 provider 차례와 상관없다."""
    slug = provider_slug(provider_name).upper().replace("-", "_")
    return PROVIDER_KEY_ENV_FORMAT.format(slug=slug)


def llm_env_for(providers: Iterable[LLMProvider]) -> dict[str, str]:
    """provision 이 문서 `llm_env` 에 남기는 표 {provider 이름: env 이름}. 키가 있는 provider 만이라
    stub 은 없다."""
    return {p.name: provider_key_env(p.name) for p in providers if not p.is_stub}


def key_env_of(inst: Agent, provider: LLMProvider) -> str | None:
    """이 문서에서 provider 의 키 env 이름. 렌더와 컴파일이 같은 답을 읽는다. 표(`llm_env`)가 있으면
    그 이름이고 표에 없는 provider(핀을 옮긴 뒤 provision 전의 렌더)는 같은 규칙으로 짓는다. stub 은
    키가 없어 None 이다."""
    if provider.is_stub:
        return None
    return inst.llm_env.get(provider.name) or provider_key_env(provider.name)


@dataclass(frozen=True)
class ProviderBinding:
    """렌더와 컴파일이 함께 읽는 provider 하나의 자리 (P38 6절). env 는 키 env 이름(stub 은 None),
    base_url 은 런타임이 부를 주소다. 게이트웨이 경로면 주소가 게이트웨이 경로다."""

    env: str | None
    base_url: str | None


def provider_binding(
    inst: Agent, provider: LLMProvider, gateway_url: str | None = None
) -> ProviderBinding:
    """provider 하나의 자리. gateway_url 을 주면 게이트웨이 경로다. stub 은 늘 제 주소다."""
    base = provider.effective_base_url
    if gateway_url is not None and not provider.is_stub:
        base = gateway_provider(provider).base_url(gateway_url)
    return ProviderBinding(key_env_of(inst, provider), base)


def _key_ref(inst: Agent, env_key: str, provider: LLMProvider) -> dict[str, Any]:
    """키 env 하나. 직접 경로는 복사본 Secret, 게이트웨이 경로는 인스턴스 토큰 Secret 의 llm_token
    이다 (P38 6절)."""
    if inst.llm_route.gateway:
        return secret_env(env_key, inst.k8s.token_secret, LLM_TOKEN_KEY)
    return secret_env(env_key, llm_secret_name_for(inst.k8s.name, provider.name), LLM_SECRET_KEY)


def provider_env(inst: Agent, providers: list[LLMProvider]) -> list[dict[str, Any]]:
    """provider 마다 키 env(복사된 Secret 참조, 게이트웨이 경로는 토큰). 이름은 `key_env_of` 가
    문서의 표로 정하고 stub 은 없다. base URL env 는 없고 `agent.json` 이 base_url 을 싣는다."""
    env: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in providers:
        env_key = key_env_of(inst, p)
        if not env_key or env_key in seen:
            continue
        seen.add(env_key)
        env.append(_key_ref(inst, env_key, p))
    return env


def knowledge_key_env(n: int) -> str:
    """agent.json `knowledge[n].embedding.provider.env_key` 이자 파드 env 이름."""
    return f"DAEGAM_KNOWLEDGE_{n}_API_KEY"


def knowledge_env(inst: Agent, providers: list[tuple[int, LLMProvider]]) -> list[dict[str, Any]]:
    """knowledge 항목 (n, 임베딩 provider) 마다 `DAEGAM_KNOWLEDGE_<n>_API_KEY`(복사된 Secret 참조)
    와 base_url 이 있을 때의 `DAEGAM_KNOWLEDGE_<n>_BASE_URL`. stub 은 키가 없어 둘 다 없다. 같은
    provider 를 여러 항목이 쓰면 항목마다 env 가 있고 전부 같은 Secret 을 가리킨다."""
    env: list[dict[str, Any]] = []
    for n, p in providers:
        if p.is_stub:
            continue
        env.append(_key_ref(inst, knowledge_key_env(n), p))
        if p.base_url and not inst.llm_route.gateway:
            env.append({"name": f"DAEGAM_KNOWLEDGE_{n}_BASE_URL", "value": p.base_url})
    return env


def oauth_env(oauth: AgentOAuth) -> list[dict[str, Any]]:
    return [
        {"name": "DAEGAM_AGENT_CLIENT_ID", "value": oauth.client_id},
        {"name": "DAEGAM_AGENT_TOKEN_URL", "value": oauth.token_url},
        {"name": "DAEGAM_AGENT_SCOPE", "value": oauth.scope},
        secret_env(AGENT_CLIENT_SECRET_ENV, oauth.secret_name, OAUTH_SECRET_KEY),
    ]


def exposure_env(
    exposure: AgentExposure,
    *,
    oidc_issuer: str,
    oidc_audience: str,
    agent_client_ids: list[str],
    groups_claim: str | None = None,
    oidc_jwks_url: str | None = None,
    oidc_insecure_http: bool = False,
    default_group: str | None = None,
    client_id_claim: str = CLIENT_ID_CLAIM,
    allowed_groups: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """외부 노출 인스턴스의 런타임 검증·인가·CORS env 5종 (D27). 목록은 콤마로 잇고 비어 있으면 빈
    값이다(런타임이 "없음" 으로 읽는다). audience 는 제어면이 아니라 데이터 플레인 것이다.
    allowed_groups 가 있으면 노출 설정의 목록 대신 그것을 내린다(두 발급자의 플랫폼 검증기가 받는
    `platform_allowed_groups`).
    oidc_jwks_url 이 있으면(local 모드, P32 1절) 런타임이 디스커버리 없이 그 클러스터 안 주소에서
    JWKS 를 받는다. 발급자나 그 주소가 평문 http 인 플랫폼만 INSECURE_HTTP 를 함께 내려 준다.
    default_group 이 있으면(local 모드의 테넌트 그룹, P32 2절) 빈 allowed_groups 대신 그 하나를
    내린다. 저장된 노출 설정은 그대로 빈 목록이다. client_id_claim 이 기본값(cid)이 아니면(oidc
    모드의 IdP, Keycloak 은 azp) 런타임이 DAEGAM_AGENT_CLIENT_IDS 를 그 클레임으로 견주도록
    내린다."""
    groups = list(exposure.allowed_groups if allowed_groups is None else allowed_groups)
    if not groups and default_group:
        groups = [default_group]
    env = [
        {"name": f"{OIDC_ENV_HEAD}_ISSUER", "value": oidc_issuer},
        {"name": f"{OIDC_ENV_HEAD}_AUDIENCE", "value": oidc_audience},
        {"name": "DAEGAM_CORS_ORIGINS", "value": ",".join(exposure.cors_origins)},
        {"name": "DAEGAM_ALLOWED_GROUPS", "value": ",".join(groups)},
        {"name": "DAEGAM_AGENT_CLIENT_IDS", "value": ",".join(agent_client_ids)},
    ]
    # oidc 모드의 IdP 는 그룹을 groups 가 아닌 클레임에 실을 수 있다(`ConsumerAuth.groups_claim`)
    env += _verifier_tail(
        OIDC_ENV_HEAD,
        jwks_url=oidc_jwks_url,
        groups_claim=groups_claim,
        insecure_http=oidc_insecure_http,
        client_id_claim=client_id_claim,
    )
    return env


def _verifier_tail(
    head: str,
    *,
    jwks_url: str | None,
    groups_claim: str | None,
    insecure_http: bool,
    client_id_claim: str,
) -> list[dict[str, Any]]:
    """발급자 하나의 env 가운데 기본값이면 싣지 않는 것들. 이 순서로 싣는다."""
    env = []
    if jwks_url:
        env.append({"name": f"{head}_JWKS_URL", "value": jwks_url})
    if groups_claim:
        env.append({"name": f"{head}_GROUPS_CLAIM", "value": groups_claim})
    if insecure_http:
        env.append({"name": f"{head}_INSECURE_HTTP", "value": "true"})
    if client_id_claim != CLIENT_ID_CLAIM:
        env.append({"name": f"{head}_CLIENT_ID_CLAIM", "value": client_id_claim})
    return env


@dataclass(frozen=True)
class IdpSide:
    """데이터 플레인이 플랫폼을 믿는 oidc 모드에서 노출 Agent 가 플랫폼 곁에 두는 IdP 검증기의 설치
    입력 (P53b 6.8절, `data_plane_auth.idp_side`). IdP 를 믿을 때 노출 Agent 가 받는 값과 같다. 그룹
    클레임은 기본값(groups)이 아닐 때만 있다."""

    issuer: str
    insecure_http: bool
    groups_claim: str | None
    client_id_claim: str


@dataclass(frozen=True)
class IdpVerifier:
    """노출 Agent 하나가 플랫폼 검증기 곁에 두는 IdP 검증기. IdP 토큰은 IdP 를 믿을 때처럼 IdP 의
    클레임 이름, 저장된 IdP 그룹(`idp_allowed_groups`), 테넌트의 agent_client_ids 로 판정한다.
    audience 는 플랫폼 검증기와 같은 DAEGAM_OIDC_AUDIENCE 다."""

    side: IdpSide
    allowed_groups: tuple[str, ...]
    agent_client_ids: tuple[str, ...]


def platform_allowed_groups(tenant: str, allowed_groups: Iterable[str]) -> tuple[str, ...]:
    """플랫폼 검증기가 받는 그룹 (P53b 6.6·6.8절). `daegam-` 로 시작하는 항목은 플랫폼 그룹이라
    적힌 그대로이고, 그 밖의 항목은 IdP 그룹이라 플랫폼 토큰이 싣는 꼴
    `daegam-group:<tenant>:<group>` 이다. 빈 목록은 `exposure_env` 의 default_group 이 채운다."""
    return tuple(
        g if g.startswith(PLATFORM_GROUP_HEAD) else idp_group(tenant, g) for g in allowed_groups
    )


def idp_allowed_groups(allowed_groups: Sequence[str]) -> tuple[str, ...] | None:
    """IdP 검증기가 받는 그룹 (P53b 6.6·6.8절). `daegam-` 로 시작하는 플랫폼 그룹은 플랫폼 검증기의
    것이라 빼고, IdP 그룹은 저장된 그대로다. 저장된 목록에 IdP 그룹이 하나도 없으면 None 이고 그때는
    IdP 검증기를 두지 않는다. 빈 목록은 IdP 가 그 audience 로 서명한 모든 토큰을 받으므로, 플랫폼
    그룹으로만 좁힌 Agent 가 IdP 쪽에서 모두에게 열리게 되기 때문이다. 저장된 목록이 비면 D27 대로
    빈 목록이다."""
    groups = tuple(g for g in allowed_groups if not g.startswith(PLATFORM_GROUP_HEAD))
    if allowed_groups and not groups:
        return None
    return groups


def idp_verifier_env(verifier: IdpVerifier) -> list[dict[str, Any]]:
    """IdP 검증기의 env. 순서와 생략 규칙은 `exposure_env` 의 DAEGAM_OIDC_* 와 같다. IdP 는
    디스커버리로 JWKS 를 받으므로 JWKS 주소는 싣지 않는다."""
    side = verifier.side
    env = [
        {"name": f"{IDP_ENV_HEAD}_ISSUER", "value": side.issuer},
        {"name": f"{IDP_ENV_HEAD}_ALLOWED_GROUPS", "value": ",".join(verifier.allowed_groups)},
        {"name": f"{IDP_ENV_HEAD}_AGENT_CLIENT_IDS", "value": ",".join(verifier.agent_client_ids)},
    ]
    env += _verifier_tail(
        IDP_ENV_HEAD,
        jwks_url=None,
        groups_claim=side.groups_claim,
        insecure_http=side.insecure_http,
        client_id_claim=side.client_id_claim,
    )
    return env


def agent_binding_env(
    prefix: str, ds: DataService, *, allow_tls: bool = False
) -> list[dict[str, Any]]:
    """MCP Server 바인딩 렌더를 재사용한 컨테이너 env. 비밀번호는 secretKeyRef 로 `_URL` 앞에
    두고 `_URL` 이 `$(<PREFIX>_PASSWORD)` 로 그것을 품는다 (K8s 가 앞선 env 를 확장한다).
    allow_tls 는 런타임 이미지가 `redis_tls` 를 알린 Agent 만 참이다 (P61 3절). 평문 포트가 닫힌
    Redis 는 그와 상관없이 TLS 끝점이다(`tls_endpoint`)."""
    plain, secrets = binding_env_and_secrets(prefix, ds, allow_tls=allow_tls)
    url_env = f"{prefix}_URL"
    env: list[dict[str, Any]] = [e for e in plain if e["name"] != url_env]
    env.extend(secret_env(s["targetEnvName"], s["name"], s["key"]) for s in secrets)
    auth = (ds.username or "") + (f":$({prefix}_PASSWORD)" if ds.has_password else "")
    env.append({"name": url_env, "value": binding_url(ds, auth, tls_endpoint(ds, allow_tls))})
    return env


def db_env_prefix(index: int) -> str:
    """i 번째 Database 바인딩의 env 접두어. memory 가 0 이고 그 뒤가 knowledge 노드 순서다."""
    return DB_PREFIX_FORMAT.format(index=index)


# 인스턴스 bindings 에 그 Database 가 없을 때의 기본값이다
DEFAULT_DB_PREFIX = db_env_prefix(0)


def dsn_env(inst: Agent, data_service_id: str) -> str:
    """agent.json 이 가리키는 그 Database 의 DSN env 이름 `<PREFIX>_URL`. 컴파일이 memory 의
    체크포인터와 knowledge 의 벡터 테이블에 적는다."""
    for b in inst.bindings:
        if b.data_service_id == data_service_id:
            return f"{b.env_prefix}_URL"
    return f"{DEFAULT_DB_PREFIX}_URL"


# -- 턴 drain (P78 5절) -------------------------------------------------------------------------


def turn_grace(drain: int) -> int:
    """drain 만큼 기다리는 파드의 grace 다. preStop, drain, 마감 몫, 여유의 합이다."""
    return drain + TURN_GRACE_OVERHEAD


def drain_within(grace: Any) -> int:
    """grace 안에 드는 drain 이다. grace 가 없으면(overrides 가 지웠다) 쿠버네티스 기본값으로 본다.
    정수가 아니면 dry-run 이 거부할 값이라 drain 을 두지 않는다."""
    if grace is None:
        grace = DEFAULT_GRACE_SECONDS
    if not isinstance(grace, int) or isinstance(grace, bool):
        return 0
    return max(0, grace - TURN_GRACE_OVERHEAD)


def _grace_override(overrides: Mapping[str, Any]) -> tuple[bool, Any]:
    """overrides 가 파드의 grace 를 정했는가와 그 값이다. null 은 지운 것이다."""
    spec = pod_spec(overrides, DEPLOYMENT_POD)
    if spec is None or "terminationGracePeriodSeconds" not in spec:
        return False, None
    return True, spec["terminationGracePeriodSeconds"]


def turn_drain_of(inst: Agent) -> int | None:
    """렌더하는 drain 이다. provision 이 남긴 결정이 없으면(draft) None 이다. 저장된 overrides 가
    grace 를 정하면 그 grace 안에 맞춘 값이다."""
    if inst.turn_drain_seconds is None:
        return None
    overridden, grace = _grace_override(inst.overrides)
    return drain_within(grace) if overridden else inst.turn_drain_seconds


def fit_turn_drain(final: dict[str, Any]) -> dict[str, Any]:
    """overrides 를 얹은 Deployment 의 drain env 를 그 grace 에 맞춘다. Advanced 편집이 최종본을
    만들 때마다 부른다(`AdvancedEditor.finish`). 렌더는 저장된 overrides 의 grace 로 drain 을
    정하므로 새로 저장하는 overrides 가 grace 를 바꿀 때 여기서 맞춘다. drain env 가 없는
    렌더(결정이 없는 옛 문서)는 그대로 둔다."""
    pod = pod_spec(final, DEPLOYMENT_POD) or {}
    container = named_item(pod.get("containers"), AGENT_CONTAINER)
    drain = named_item(container.get("env"), TURN_DRAIN_ENV)
    if drain:
        drain["value"] = str(drain_within(pod.get("terminationGracePeriodSeconds")))
    return final


# -- Deployment·Service·NetworkPolicy ---------------------------------------------------------


def rendered_replicas(state: SleepState, effective: EffectiveConfig) -> int:
    """쉬는 상태 state 의 렌더가 주는 레플리카 수 (P44 9절). 잠들었으면(asleep) 0 이고 아니면 배포
    설정이다. provision, 재적용, 렌더 번호, 토큰 회전 어느 렌더도 잠든 Agent 를 깨우지 않고 템플릿만
    바꾼다. 레플리카 패치(`services/agent_sleep.py` `apply_replicas`)도 이것으로 센다. overrides 의
    `spec.replicas` 는 이 뒤에 얹혀 이기며, 그런 Agent 는 잠들 수 없다(6절)."""
    return 0 if state == "asleep" else effective.replicas


def render_agent_deployment(
    inst: Agent,
    effective: EffectiveConfig,
    *,
    providers: list[LLMProvider],
    data_services: dict[str, DataService],
    agent_oauth: AgentOAuth | None,
    oidc_issuer: str | None = None,
    oidc_jwks_url: str | None = None,
    oidc_audience: str | None = None,
    oidc_insecure_http: bool = False,
    agent_client_ids: list[str] | None = None,
    client_id_claim: str,
    groups_claim: str | None,
    default_group: str | None = None,
    allowed_groups: Sequence[str] | None = None,
    idp_verifier: IdpVerifier | None = None,
    knowledge_providers: list[tuple[int, LLMProvider]] | None = None,
    gateway_token: bool = False,
    transcript_retention_days: int = DEFAULT_TRANSCRIPT_RETENTION_DAYS,
    instance_token: str | None = None,
    redis_tls: bool = False,
    telemetry_ingest: bool = False,
    sandbox: bool = False,
) -> dict[str, Any]:
    """apps/v1 Deployment. providers 는 그래프가 쓰는 provider(메인·폴백·서브·임베딩) 순서,
    data_services 는 effective.bindings 의 data_service_id → DataService, agent_oauth 는 oidc
    모드에서 Gateway 도구 소스를 쓸 때만 준다. knowledge_providers 는 agent.json `knowledge[]` 의
    (순서 n, 임베딩 provider) 다. 노출을 켠 인스턴스는 oidc_issuer·oidc_audience 가 있어야 하고
    (없으면 RenderError unauthenticated_exposure) 검증·인가·CORS env 가 더 들어간다.
    default_group 은 빈 allowed_groups 를 채울 그룹이고 groups_claim 은 런타임이 그룹을 읽을
    클레임이다(`exposure_env`). allowed_groups 는 노출 설정의 목록 대신 내릴 목록이다.
    idp_verifier 가 있으면(플랫폼을 믿는 oidc 모드의 두 발급자 이미지, P53b 6.8절) 플랫폼 검증기
    곁에 그 검증기의 env 를 싣는다.
    transcript_retention_days 는 플랫폼 ConfigMap 이 정한 기록 보존 기간이다 (D46). 턴 drain 과
    grace 는 문서의 결정에서 온다(`turn_drain_of`, P78 5절).
    instance_token 은 설정 지문을 거는 인스턴스 토큰 평문이다(`config_sha`). 적용하는 렌더만 주고
    확인용 렌더는 비워 둔다. redis_tls 는 런타임 이미지가 `redis_tls` 를 알렸는가이고, 참이면 TLS
    끝점을 가진 Redis Database 바인딩이 그 끝점과 CA 를 쓴다 (P61 3절). 평문 포트가 닫힌 Redis 는
    언제나 그렇다. telemetry_ingest 는 런타임 이벤트 수집 경로가 켜졌는가이고, 참이면 그 주소와
    파드 이름·uid 를 싣는다 (P37 3절). sandbox 는 핀한 리비전이 코드 실행을 켰는가이고, 참이면
    샌드박스의 기계 경로를 싣는다 (P40 5절). 끈 Agent 는 예전 렌더 그대로다."""
    labels = _labels(inst)
    config_env = {"name": CONFIG_PATH_ENV, "value": f"{CONFIG_MOUNT}/{CONFIG_FILE}"}
    env: list[dict[str, Any]] = [
        config_env,
        {"name": "DAEGAM_TENANT", "value": inst.tenant},
        # 닫힌 대화의 기록이 남는 날 수 (D46). 세션 TTL 과 다른 값이고 플랫폼 설정에서 온다
        {
            "name": TRANSCRIPT_RETENTION_ENV,
            "value": str(max(0, transcript_retention_days)),
        },
        secret_env(INSTANCE_TOKEN_ENV, inst.k8s.token_secret, TOKEN_KEY),
        # MCP Server 를 부를 옛 단일 서비스 토큰 (D41). 표가 생기기 전 파드의 컴파일이 쓴다
        secret_env(MCP_TOKEN_ENV, inst.k8s.token_secret, MCP_TOKEN_KEY),
    ]
    # MCP Server 소스마다의 서비스 토큰 (P50 9절). 지금 템플릿의 표가 정한다
    table = inst.current_token_table
    if table is not None:
        env.extend(secret_env(e, inst.k8s.token_secret, e) for e in sorted(table.envs.values()))
    if gateway_token:
        # local 모드의 Gateway 소스: 플랫폼이 낸 데이터 플레인 토큰으로 vMCP 를 부른다
        env.append(secret_env(GATEWAY_TOKEN_ENV, inst.k8s.token_secret, GATEWAY_TOKEN_KEY))
    if inst.exposure.enabled:
        if not oidc_issuer or not oidc_audience:
            raise RenderError("unauthenticated_exposure", "Agent exposure requires oidc mode")
        env.extend(
            exposure_env(
                inst.exposure,
                oidc_issuer=oidc_issuer,
                oidc_audience=oidc_audience,
                agent_client_ids=list(agent_client_ids or []),
                groups_claim=groups_claim,
                oidc_jwks_url=oidc_jwks_url,
                oidc_insecure_http=oidc_insecure_http,
                default_group=default_group,
                client_id_claim=client_id_claim,
                allowed_groups=allowed_groups,
            )
        )
        if idp_verifier is not None:
            env.extend(idp_verifier_env(idp_verifier))
    env.extend(provider_env(inst, providers))
    env.extend(knowledge_env(inst, list(knowledge_providers or [])))
    if agent_oauth is not None:
        env.extend(oauth_env(agent_oauth))
    ca_volumes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ca_fingerprints: dict[str, str | None] = {}
    for b in effective.bindings:
        ds = data_services.get(b.data_service_id)
        if ds is None:
            raise RenderError("unknown_data_service", f"data service {b.data_service_id} missing")
        env.extend(agent_binding_env(b.env_prefix, ds, allow_tls=redis_tls))
        ca = binding_ca_volume(b.env_prefix, ds, allow_tls=redis_tls)
        if ca is not None and ds.tls is not None:
            ca_volumes.append(ca)
            ca_fingerprints[ds.id] = ds.tls.ca_sha256

    if telemetry_ingest:
        env.extend(telemetry_env(effective.platform_internal_url))
    if sandbox:
        env.append(sandbox_env(effective.platform_internal_url, inst.id))
    drain = turn_drain_of(inst)
    if drain is not None:
        # 파드가 내려갈 때 도는 턴을 기다리는 초 (P78 5절). grace 는 아래 파드 사양에 싣는다
        env.append({"name": TURN_DRAIN_ENV, "value": str(drain)})
    # 루트가 읽기 전용이라 라이브러리 캐시 자리를 못 박는다. tiktoken 의 기본값도 tempfile 이지만
    # 그 동작에 기대지 않는다 (OpenAI 토큰 계산 경로에서만 쓴다)
    env.append({"name": "TIKTOKEN_CACHE_DIR", "value": "/tmp/tiktoken"})

    agent_container: dict[str, Any] = {
        "name": AGENT_CONTAINER,
        "image": effective.images.agent_image,
        "imagePullPolicy": effective.images.pull_policy,
        "ports": [{"name": "http", "containerPort": AGENT_PORT}],
        "env": env,
        "readinessProbe": {
            "httpGet": {"path": READY_PROBE_PATH, "port": AGENT_PORT},
            "initialDelaySeconds": READY_PROBE_DELAY_SECONDS,
            "periodSeconds": READY_PROBE_PERIOD_SECONDS,
            "failureThreshold": READY_PROBE_FAILURES,
        },
        "livenessProbe": {
            "httpGet": {"path": HEALTHZ_PATH, "port": AGENT_PORT},
            "initialDelaySeconds": 30,
            "periodSeconds": 30,
            "failureThreshold": 6,
        },
        # exec 가 아니라 쿠버네티스의 sleep 동작이라 이미지에 sleep 이 없어도 된다
        "lifecycle": {"preStop": {"sleep": {"seconds": PRESTOP_SECONDS}}},
    }
    if effective.resources:
        agent_container["resources"] = effective.resources
    pod_spec = config_fetch_pod(
        init_name=INIT_CONTAINER,
        volume=CONFIG_VOLUME,
        main=agent_container,
        effective=effective,
        extra_volumes=tuple(ca_volumes),
        **_init_container_args(
            inst,
            effective,
            config_env,
            gateway_auth=template_gateway_auth(gateway_token, agent_oauth),
        ),
    )
    if inst.turn_drain_seconds is not None:
        # grace 는 결정의 drain 으로 정한다. overrides 가 grace 를 정하면 그 값이 덮고 drain 이
        # 맞춘다
        pod_spec["terminationGracePeriodSeconds"] = turn_grace(inst.turn_drain_seconds)
    # 파드를 굴리는 판. 서비스 토큰의 판(D41)은 리컨실이 토큰을 바꾸면, 설정 지문(P35 13절)은
    # 키·agent.json·인스턴스 토큰이 바뀌면, Redis CA 의 지문(P61)은 TLS 로 붙는 Database 의 CA 가
    # 바뀌면 값이 바뀐다
    annotations = {
        SVC_TOKEN_REV_ANNOTATION: inst.svc_token_rev,
        CONFIG_SHA_ANNOTATION: config_sha(inst, instance_token),
    }
    if (ca_mark := redis_ca_mark(ca_fingerprints)) is not None:
        annotations[REDIS_CA_ANNOTATION] = ca_mark
    pod_labels = dict(labels)
    if inst.llm_route.gateway:
        # 게이트웨이의 NetworkPolicy 와 설치 해제 검사가 읽는다 (P38 6절·10절)
        pod_labels[LLM_ROUTE_LABEL] = LLM_GATEWAY_ROUTE
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": inst.k8s.name, "namespace": inst.k8s.namespace, "labels": labels},
        "spec": {
            "replicas": rendered_replicas(inst.sleep.state, effective),
            "selector": {"matchLabels": selector_labels(inst)},
            "template": {
                "metadata": {"labels": pod_labels, "annotations": annotations},
                "spec": pod_spec,
            },
        },
    }


def template_gateway_auth(gateway_token: bool, agent_oauth: AgentOAuth | None) -> str | None:
    """이 템플릿이 env 로 실은 Gateway 소스 인증의 종류다. 플랫폼이 낸 토큰이면 bearer, client
    credentials 면 oauth 이고, 둘 다 싣지 않으면 None 이다."""
    if gateway_token:
        return GATEWAY_AUTH_BEARER
    return GATEWAY_AUTH_OAUTH if agent_oauth is not None else None


def _init_container_args(
    inst: Agent,
    effective: EffectiveConfig,
    config_env: dict[str, Any],
    *,
    gateway_auth: str | None = None,
) -> dict[str, Any]:
    """init 컨테이너 몫의 `config_fetch_pod` 인자다. init 은 이 Agent 의 런타임 이미지로 메인과 같은
    pull 정책으로 돈다(D65). 이미지가 아는 기능 이름의 원천이 그 이미지다. `agent.json` 은
    `agent_json_url` 의 주소로 받고 기능의 init 단계는 `agent_config_base` 아래를 연다."""
    platform_url = effective.platform_internal_url
    image = effective.images.agent_image
    init_env = [
        secret_env(INSTANCE_TOKEN_ENV, inst.k8s.token_secret, TOKEN_KEY),
        config_env,
        {
            "name": CONFIG_URL_ENV,
            "value": agent_json_url(
                platform_url, inst.id, gateway=inst.llm_route.gateway, gateway_auth=gateway_auth
            ),
        },
        {"name": CONFIG_BASE_ENV, "value": agent_config_base(platform_url, inst.id)},
        {"name": AGENT_IMAGE_ENV, "value": image},
    ]
    table = inst.current_token_table
    if table is not None:
        # 이 템플릿의 토큰 env 표 지문. init 이 받기에 실어 그 표로 컴파일한 agent.json 을 받는다
        init_env.append({"name": MCP_TOKEN_TABLE_ENV, "value": table.digest})
    return {
        "init_env": init_env,
        "script": FETCH_SCRIPT,
        "init_image": image,
        "init_command": INIT_COMMAND,
        "init_pull_policy": effective.images.pull_policy,
    }


def render_agent_service(inst: Agent) -> dict[str, Any]:
    """Service `agent-<name>:8080`. 플랫폼 프록시(D21)와 같은 네임스페이스만 부른다."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": inst.k8s.name,
            "namespace": inst.k8s.namespace,
            "labels": _labels(inst),
        },
        "spec": {
            "type": "ClusterIP",
            "selector": selector_labels(inst),
            "ports": [{"name": "http", "port": AGENT_PORT, "targetPort": AGENT_PORT}],
        },
    }


def agent_owner_ref(inst: Agent, uid: str) -> dict[str, Any]:
    """노출 객체(Ingress·NetworkPolicy)를 인스턴스 Deployment 에 종속시킨다. Deployment 가 지워지면
    GC 되고 삭제 단계가 이름으로 한 번 더 지운다."""
    return owner_ref("Deployment", inst.k8s.name, uid, api_version="apps/v1")


def render_agent_expose_docs(
    inst: Agent,
    exposure: AgentExposure,
    *,
    ingress: ClusterIngress,
    owner_uid: str | None,
) -> list[dict[str, Any]]:
    """외부 노출 문서 묶음 (D27). Ingress(`AGENT_EXPOSE_PATHS` → agent-<name>:8080,
    docs/guide/exposure.md 1절 TLS 규칙)와 NetworkPolicy `daegam-expose-<name>`(컨트롤러
    네임스페이스에서 에이전트 파드로)이고 앞단 프록시는 없다. ingress 는 effective_config 스냅샷의
    Cluster ingress(annotations·컨트롤러 위치)다."""
    owner = agent_owner_ref(inst, owner_uid) if owner_uid else None
    tls_secret, issuer = ingress_tls(exposure, inst.k8s.name)
    return [
        render_ingress(
            tenant=inst.tenant,
            instance_id=inst.id,
            resource=RESOURCE_AGENT_INGRESS,
            name=inst.k8s.name,
            namespace=inst.k8s.namespace,
            host=exposure.host or "",
            ingress_class=exposure.ingress_class or "",
            service_name=inst.k8s.name,
            port=AGENT_PORT,
            tls_secret=tls_secret,
            cluster_issuer=issuer,
            annotations=ingress.annotations,
            owner=owner,
            paths=AGENT_EXPOSE_PATHS,
        ),
        render_expose_policy(
            tenant=inst.tenant,
            instance_id=inst.id,
            resource=RESOURCE_AGENT_POLICY,
            name=expose_policy_name(inst.k8s.name),
            namespace=inst.k8s.namespace,
            controller_namespace=ingress.controller_namespace,
            controller_ip_blocks=ingress.controller_ip_blocks,
            owner=owner,
            pod_selector=selector_labels(inst),
        ),
    ]


def render_agent_policy(inst: Agent, platform_namespace: str) -> dict[str, Any]:
    """런타임 파드의 ingress 정책. 출처는 테넌트 정책과 같은 `tenant_ingress_from` 이다. 같은
    네임스페이스, 같은 테넌트 라벨의 네임스페이스, 플랫폼 네임스페이스(프록시)에서만 들어온다."""
    return {
        "apiVersion": NETWORK_POLICY_API,
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name_for(inst.k8s.name),
            "namespace": inst.k8s.namespace,
            "labels": _labels(inst),
        },
        "spec": {
            "podSelector": {"matchLabels": selector_labels(inst)},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": tenant_ingress_from(inst.tenant, platform_namespace)}],
        },
    }
