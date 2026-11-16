"""여러 테스트 모듈이 같은 뜻으로 쓰는 상수."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

# 저장소 뿌리. 화면·런타임 사본과 배포 파일을 읽는 테스트가 여기서 경로를 잇는다
REPO = Path(__file__).resolve().parents[3]

# 플랫폼의 클러스터 안 주소. local 모드의 서비스 토큰 발급자(platform_issuer)도 이 값이다 (D40·D41)
ISSUER = "http://daegam-platform.daegam-system.svc:9000"
# 플랫폼 JWKS 의 클러스터 안 주소. 경로는 공개 계약이라 글자로 적는다
PLATFORM_JWKS = f"{ISSUER}/.well-known/jwks.json"

# 플랫폼이 도는 네임스페이스. provider 키 Secret 이 여기 있다
PLATFORM_NS = "daegam-system"
# 가짜 클러스터의 SelfSubjectReview 가 돌려주는 플랫폼 자신. 플랫폼이 테넌트 네임스페이스에 두는
# RoleBinding 의 subject 다
PLATFORM_SUBJECT = {"kind": "ServiceAccount", "name": "daegam-platform", "namespace": PLATFORM_NS}
# LLM provider 키 Secret. 키 경로가 Secret 뿐이라(D25) job·Agent 를 만드는 테스트는 이 이름을
# api_key_secret 으로 쓴다. conftest 의 fake_secrets 가 미리 넣어 둔다
LLM_KEY_SECRET = "llm-keys"
# local 모드 플랫폼의 공개 주소 DAEGAM_PUBLIC_URL. local 에서는 사람 토큰의 발급자다 (P32 1절)
PUBLIC_URL = "https://daegam.example.com"

# oidc 모드 플랫폼이 믿는 mock OIDC(deploy/dev/mock-oidc)의 발급자와 audience
OIDC_ISSUER = "http://mock-oidc.test"
OIDC_AUDIENCE = "daegam-api"

# 제어면 API 경로. 테스트 클라이언트가 /api 에 뿌리내려 있어 논리 경로만 적는다
BP = "/backend-apis"
MS = "/mcp-servers"
GW = "/gateways"
AG = "/agents"
HUB = "/hub"
DS = "/data-services"
SK = "/skills"
KB = "/knowledge-bases"
A = "/assistant"
JOBS = "/spec-generator/jobs"

# header 모드가 테넌트를 읽는 요청 헤더의 이름과, 기본 테넌트(default)의 네임스페이스
TENANT_HEADER = "X-Daegam-Tenant"
DEFAULT_NS = "daegam-default"
# 다른 테넌트, 그 테넌트의 네임스페이스, 그 테넌트로 보내는 header 모드 요청 헤더
OTHER = "other"
OTHER_NS = "daegam-other"
T2 = {TENANT_HEADER: OTHER}
# 문서 빌더의 테넌트(acme)의 네임스페이스
ACME_NS = "daegam-acme"

# 클러스터 등록 본문의 kubeconfig. 테스트 클러스터는 fake 라 내용을 읽지 않는다
KUBECONFIG = "apiVersion: v1\nkind: Config\n"

# oidc 모드의 IdP 그룹 → 역할 매핑. 플랫폼 관리자와 테넌트 a 의 tenant-admin·developer 가 공통이다.
# GROUP_MAPPINGS 는 테넌트 a 의 viewer 와 테넌트 b 의 developer 를, B_VIEWER_MAPPINGS 는 테넌트 b
# 의 viewer 를 더한다. C_DEV_MAPPINGS 는 GROUP_MAPPINGS 에 테넌트 c 의 developer 를 더한다
_ADMIN_MAPPINGS = [
    {"group": "daegam-admins", "tenant": "*", "role": "platform-admin"},
    {"group": "daegam-a-admin", "tenant": "a", "role": "tenant-admin"},
    {"group": "daegam-a-dev", "tenant": "a", "role": "developer"},
]
GROUP_MAPPINGS = [
    *_ADMIN_MAPPINGS,
    {"group": "daegam-a-view", "tenant": "a", "role": "viewer"},
    {"group": "daegam-b-dev", "tenant": "b", "role": "developer"},
]
B_VIEWER_MAPPINGS = [
    *_ADMIN_MAPPINGS,
    {"group": "daegam-b-view", "tenant": "b", "role": "viewer"},
]
C_DEV_MAPPINGS = [
    *GROUP_MAPPINGS,
    {"group": "daegam-c-dev", "tenant": "c", "role": "developer"},
]

# K8s API 그룹/버전
GV = "toolhive.stacklok.dev/v1beta1"
NP_API = "networking.k8s.io/v1"

# Hub 원격 항목이 가리키는 바깥 MCP 주소
EXT_URL = "https://mcp.example.com/mcp"

# init 이 agent.json 을 받을 때 알리는 Agent 런타임 이미지 (D65)
IMAGE = "daegam-agent-runtime:0.1.0"

# 설치 벌의 값 파일. 저장소 기준 경로이고 플랫폼 호스트 글자는 이 둘에만 있다 (D54, D58).
# INSTALL_ENV 는 공개 스크립트가 읽는 설치 값 파일의 예시다(P85 10절). LOCAL_VALUES 는 make
# deploy-local 이 대감 차트에 주는 로컬 개발 클러스터의 값이다(P85 4절)
INSTALL_ENV = "deploy/install.env.example"
LOCAL_VALUES = "deploy/dev/values-local.yaml"

# 시각을 제품의 문턱(리컨실의 stale_after, 상태 매핑의 ROLLING_GRACE) 너머로 밀 때 더하는 여유
TIME_MARGIN = timedelta(seconds=1)
