#!/usr/bin/env bash
# P31 (D54) 설치본 e2e. 공개 주소와 local 인증, 운영자가 만든 e2e 관리자가 있는 설치본에 대고 돈다(P85 10절). 로컬
# orbstack 은 다른 e2e 들이 본다.
#
#   make e2e-install INSTALL_CONTEXT=<컨텍스트> E2E_INSTALL_USER=<e2e 전용 관리자>
#   E2E_INSTALL_REBOOT=1 make e2e-install ...   재부팅 흉내(파드를 모두 지우고 돌아오는지)까지 본다
#
# 인증은 부트스트랩 관리자가 아니라 운영자가 한 번 만든 e2e 전용 platform-admin 계정이다. 비밀번호는
# E2E_INSTALL_PASSWORD 가 없으면 macOS 키체인 항목 daegam-e2e-install(계정 이름이 E2E_INSTALL_USER)에서 읽는다. 첫 로그인의
# 비밀번호 바꾸기는 사람이 화면에서 끝내 둔다(여기서는 바꾸지 않는다). 공개 서버의 상시 관리자 자격이라 쓰지 않을 때는
# 비활성으로 둔다. 값은 설치 값 파일(~/.config/daegam/install.env, 예시 deploy/install.env.example)이 기본이고 같은 이름의
# 환경 변수가 이긴다. E2E_REQUIRE_LIMITS 도 그 파일의 키다.
#
# Gate:
#   STEP 1   서빙 빌드가 이 checkout 의 도장과 같다 (scripts/build-stamp.sh platform·ui 와 svc/daegam-platform
#            /api/version, svc/daegam-ui /build.json). 배포한 커밋의 깨끗한 checkout 에서 돌린다.
#            로그인한 뒤 GET /api/settings/data-plane-auth 가 남은 재렌더 없음(pending 0)을 말할 때까지 기다린다(P50 2절).
#            새 플랫폼이 다시 렌더하는 객체가 굴러가는 사이에 뒤의 단계가 그것을 치지 않게 한다
#   STEP 2   daegam-system·toolhive-system 의 컨테이너마다 CPU·메모리 requests·limits 를 센다. E2E_REQUIRE_LIMITS=true 면
#            하나라도 빠지면 실패이고, 아니면(기본 false) 빠진 것을 찍기만 한다. 문서 파싱 서비스 daegam-docparse 의
#            docparse 컨테이너가 떠 있다(D61). NetworkPolicy 집행:
#            app=daegam-ui 라벨의 임시 curl 파드는 플랫폼 Service 에 닿지 못하고 라벨 없는 임시 파드는 닿는다. ConfigMap 은
#            고치지 않는다. 그 DAEGAM_HOME_NETWORK_POLICY 가 실측과 다르면 실패이고, 없으면(차트의 기본) 끝에 못 박을 값을 알린다
#   STEP 3   home Cluster: preflight ok, network_policy 가 못 박은 값이거나 (없으면) 실측에 맞는 탐지(enforced·presumed)다, ConfigMap 의 샌드박스 이미지(P40, 늘 있어야 한다)와
#            ConfigMap 에 있는 이미지·자원·LimitRange·노출 TLS 가 그 값과 같고(있는데 빈 키는 실패다) env_managed 에 그
#            필드들이 있다
#   STEP 4   설치본이 플랫폼 곁에 둔 저장소(플랫폼 네임스페이스의 StatefulSet redis·postgres). Redis 가 있으면 ConfigMap 에
#            DAEGAM_HOME_REDIS_TLS 가 있어야 하고, TLS 포트(6380)에서 답하고 그 MODULE LIST 에 search·ReJSON 이 있다. 평문
#            포트 6379 는 열려 있으면 답하고, 배포가 닫았으면(lib/redis_plain.sh) 연결을 거절한다 (P61 F4). Postgres 가
#            있으면 pg_available_extensions 에 vector. Redis 는 TLS 끝점(ConfigMap daegam-redis-ca 의 CA)과 함께, Postgres 는
#            TLS 없이 Database 로 등록하고 verify ok. 플랫폼은 TLS 끝점으로만 확인한다 (P61 F3). 둘 다 없으면 SKIP
#            conditional 이다
#   STEP 5   petstore 스펙 → Backend API → MCP Server [get_pets, getPet, addPet] 배포 Ready → 허브 게시 → 플랫폼 경유
#            tools/list(GET /hub/{id}/tools) 가 그 셋이다
#   STEP 5n  (NetworkPolicy 를 집행할 때) MCP 백엔드 파드에는 프록시만 들어온다 (T67). StatefulSet 파드가
#            daegam.devops.ai.kr/mcp-backend 라벨을 달고 프록시 파드는 달지 않으며, 라벨 없는 임시 파드는 백엔드 Service 에 닿지
#            못하고 프록시에는 닿으며, app.kubernetes.io/name=mcpserver 만 단 임시 파드는 백엔드에 닿는다
#   STEP 5e  (NetworkPolicy 를 집행하고 home 의 메타데이터 차단이 켜졌을 때) 테넌트 파드는 169.254.169.254 에 닿지 못한다 (T75).
#            플랫폼 네임스페이스의 임시 파드가 양성 대조이고, 테넌트 파드는 플랫폼 Service 와 이름으로 찾은 프록시에 그대로 닿는다.
#            스위치가 꺼졌거나 컨트롤러가 확인된 것이 아니어서 egress 정책이 없으면 건너뛴다
#   STEP 6   그 허브 항목을 백엔드로 Gateway 배포 Ready → 게시 → vMCP 경유 tools/list 가 같다
#   STEP 7   노출(edge). DAEGAM_HOME_INGRESS_ENABLED=true 인 배치에서만 돈다(꺼져 있으면 SKIP conditional). 앞단은 그 서버
#            하나를 백엔드로 둔 vMCP front-<k8s.name> 이고 Ready 다. Ingress 가 vmcp-front-<k8s.name>:4483 을 가리키고 옛
#            앞단(MCPRemoteProxy expose-<k8s.name>)은 없다. external_url 이 https, 끈다
#   STEP 8   provider(kind openai, 가짜 키 Secret) 등록 → petstore MCP Server 를 도구로 쓰는 Agent 배포 Ready. memory 는
#            STEP 4 의 Redis 이고, 그 Redis 가 없으면 memory 없이 배포한다. 파드 env 에 provider 이름에서 나온
#            DAEGAM_LLM_<SLUG>_API_KEY 가 있고 OPENAI_API_KEY·OPENAI_BASE_URL 이 없으며, 파드 템플릿에 설정 지문
#            daegam.devops.ai.kr/config-sha 가 있고 init 이 런타임 이미지로 돈다(P35). 채팅은 하지 않는다
#   STEP 8m  (STEP 4 의 Redis 가 있을 때) 런타임이 redis_tls 를 알리므로 재적용이 memory 바인딩을 TLS 끝점으로 다시
#            렌더하고(<접두어>_TLS=1), 그 Database 의 평문 소비자(GET /data-services/{id}/plain-consumers)가 없다 (P61 F3)
#   STEP 8t  턴 위임 토큰 (P53 8절). 위임은 기본으로 켜져 있고 이 단계가 후보마다 설치본에서 그것을 증명한다. 위임 기록을
#            플랫폼 MongoDB 파드 mongodb-0 에서 읽으므로 바깥 MongoDB 를 쓰는 설치본은 8t·8r 을 조건부로 건너뛴다. ConfigMap 에
#            DAEGAM_TURN_TOKENS_ENABLED=true(위임을 끈 배포가 아니면 바뀌는 것이 없다)와 스텁 provider 허용을 이 단계
#            동안만 두고(되돌리는 명령을 먼저 찍는다) 플랫폼을 굴린다. 스텁이 이미 열려 있으면 바꾸기
#            전에 실패한다. e2e 관리자를 $TENANT 의 developer 로 둔다(위임은 Agent 의 테넌트를 쥔 소속 행이 있는 사람에게만
#            열리고 platform-admin 의 * 행은 세지 않는다). 그 클러스터의 Gateway 게이트가 열리고 판정이 새것일 때까지
#            기다리고(못 열리면 붙든 문서와 까닭을 찍고 실패한다), 스텁 모델로 STEP 6 의 Gateway 와 STEP 5 의 MCP Server 를
#            저마다 소스로 get_pets 하나만 허락한 Agent 둘이 get_pets 를 부른다. 백엔드가 닿지 않으므로 호출은 프록시를 지나
#            브리지의 요청 오류로 끝나야 한다. 그 턴의 위임 기록(플랫폼 MongoDB)이 그 대상의 토큰을 실었고, 위임의 열린·끝난
#            시각 창에서 두 감사 기록이 outcome success 에 sub 가 e2e 관리자, act 가 그 Agent 의 서비스 신원이다. MCP Server 는
#            자기 audience 로 옮겨 가 그것이 적용된 뒤에야 토큰을 받으므로 위임에 그 대상이 들 때까지 새 턴으로 다시 본다.
#            끝에서 ConfigMap 을 먼저 되돌리고 Agent 둘·스텁 provider(앞 실행이 남긴 것 포함)·소속을 되돌린 뒤 플랫폼을 굴리고,
#            되돌아갔는지 확인한다. 되돌리지 못한 것은 표시가 남아 EXIT 트랩이 다시 한다(E2E_INSTALL_KEEP=1 이어도 되돌린다)
#   STEP 8r  지금 실행 (P41). 실행은 memory 가 있어야 하므로 STEP 4 의 Redis 가 없으면 SKIP conditional 이다. E2E_TAG 의 후보가
#            make e2e-install-extra 로 e2e-runs 를 돌면 그 STEP 0~2 가 같은 것을 보므로 그때도 SKIP conditional 이다. 8t 의 창 안에서
#            돈다(스텁 허용, e2e 관리자가 developer). 실행은 기본으로 켜져 있으므로 GET
#            /api/me/runs-as-me 가 200 이 아니면 8t 가 ConfigMap 을 바꾸기 전에 실패한다. 실행의 전제를 갖춘 Agent(스텁 모델, STEP 5 의
#            MCP Server 의 get_pets, STEP 4 의 Redis memory, 세션 예산)를 8t 의 Agent 와 함께 배포하고, memory 바인딩이 TLS 로
#            옮겨 간 뒤 e2e 관리자의 지금 실행이 succeeded 로 끝나고 그 사람의 이름(acts_as person)으로 돈다. 8t 가 그 MCP
#            Server 의 위임 토큰을 이미 보았으므로 실행도 위임을 받는다. Agent 와 실행 세션의 Redis 키는 8t 의 정리가 함께 걷는다
#   STEP R   (E2E_INSTALL_REBOOT=1) daegam-system·toolhive-system·테넌트 네임스페이스의 파드를 모두 지운다. daegam-docparse 와
#            설치본에 있는 저장소(MongoDB, STEP 4 의 Redis·Postgres)를 포함해 전부 Ready 로 돌아오고 tools/list 가 다시 된다
#   STEP 9   테넌트 네임스페이스의 모든 컨테이너(MCP·프록시·init·vMCP·앞단·Agent)에 CPU·메모리 requests·limits 가 있다.
#            5~8 이 저마다 배포 직후에도 본다
#   STEP 10  만든 것을 지운다(provider·가짜 키 Secret, Agent 체크포인터가 Redis 에 만든 인덱스·키 포함). 테넌트
#            네임스페이스의 CR·Agent Deployment 가 사라진다
#
# LimitRange 가 채운 컨테이너(파드 주석 kubernetes.io/limit-ranger)는 실패가 아니라 따로 찍는다. LimitRange 는 뒷받침이고
# 우리가 렌더하는 컨테이너가 스스로 값을 갖는 것은 렌더 테스트가 본다.
# 재실행: 만들기의 409 는 PUT 으로 덮고, 배포의 409 already_deployed 는 redeploy 로 잇는다(lib/e2e_api.sh 머리말).
# 실패로 끝나도 EXIT 트랩이 만든 것을 걷는다. E2E_INSTALL_KEEP=1 이면 남긴다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/hosts.sh"
install_env_defaults
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/e2e_json.sh"
# e2e 관리자와 그 비밀번호(install_admin)와 바꾸지 않는 로그인(local_login_kept)은 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# redis_plain_of 는 여기에 있다. ConfigMap 의 값으로 평문 포트를 닫았는지 가른다 (P61 F4)
. "$HERE/lib/redis_plain.sh"

CTX="${INSTALL_CONTEXT:-}"
NS=daegam-system
OP_NS="${TOOLHIVE_NS:-toolhive-system}"
TENANT="${E2E_TENANT:-e2e}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"
API_PORT="${E2E_API_PORT:-$PORT_INSTALL_API}"
UI_PORT="${E2E_UI_PORT:-$PORT_INSTALL_UI}"
# STEP 2 가 제어면 컨테이너의 requests·limits 를 요구하는가 (설치 값 파일의 E2E_REQUIRE_LIMITS)
REQUIRE_LIMITS="$(install_flag E2E_REQUIRE_LIMITS false)" || exit 1
# 테넌트 컨테이너에도 requests·limits 를 요구하는가. STEP 3 이 home 의 기본 자원을 보고 정한다
TENANT_LIMITS=false
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
CURL_IMAGE="curlimages/curl:8.14.1"
SPEC="$ROOT/mcp-runtime/tests/fixtures/petstore.openapi.yaml"
TOOLS_JSON='["get_pets", "getPet", "addPet"]'
TOOLS_WANT="['addPet', 'getPet', 'get_pets']"
BP_NAME="petstore-install"; MS_NAME="petstore-install"; GW_NAME="install-gw"; AG_NAME="install-agent"
RDS_NAME="redis-install"; PG_NAME="pg-install"
# STEP 4 가 본 설치본의 저장소. 1 이면 플랫폼 곁에 그 StatefulSet 이 있다(MongoDB 가 0 이면 바깥 MongoDB 다)
SHIPPED_REDIS=0; SHIPPED_PG=0; SHIPPED_MONGO=0
# STEP 8 이 Agent 의 memory 바인딩이 TLS 로 옮겨 가기를 기다리는 초. 재적용 틱이 한 번 돌 만큼이다
AGENT_TLS_WAIT="${E2E_INSTALL_AGENT_TLS_WAIT:-240}"
PROVIDER="e2e-install-openai"; PROVIDER_SECRET="daegam-llm-e2e-install"
# provider 이름에서 나온 키 env 이름 (P35 12절). 파드 템플릿의 설정 지문 이름은 lib/e2e_json.sh 에 있다
PROVIDER_ENV="DAEGAM_LLM_E2E_INSTALL_OPENAI_API_KEY"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-install.XXXXXX")"
K=("${KUBECTL:-kubectl}" --context "$CTX")
AUTH=()
PF_API=""; PF_UI=""
BP_ID=""; MS_ID=""; HMS=""; GW_ID=""; GWPUB=""; AG_ID=""; RDS_ID=""; PG_ID=""
TENANT_NS=""; PROVIDER_ADDED=0; SECRET_ADDED=0; TORN_DOWN=0; TEARDOWN_ERR=0
NP_PIN_NOTE=""
# --- 턴 위임 토큰 (STEP 8t). 이 단계가 끝에서 되돌리고, 도중에 실패하면 EXIT 트랩(drop_turn_check)이 되돌린다 ---
# 스텁 provider 이름은 이 게이트의 것이라 앞 실행이 남긴 것도 8t 가 걷는다
TT_STUB="e2e-install-stub"; TT_GW_AG="install-turn-gw"; TT_MS_AG="install-turn-ms"
# 위임에 대상이 들 때까지 새 턴으로 다시 보는 한도와 간격(초). MCP Server 의 audience 전환이 적용되는 재적용 틱 몇 번이다
TURN_TARGET_WAIT="${E2E_INSTALL_TURN_TARGET_WAIT:-480}"; TURN_TARGET_POLL=30
# petstore 백엔드(petstore.example.com)는 설치본에서 닿지 않는다. 프록시가 받은 호출은 브리지의 요청 오류로 끝나고 ToolHive 는
# 그것을 outcome success 로 감사한다. 거절한 호출은 denied 다(2026-10-26 OrbStack 실측)
TURN_BACKEND_ERROR="Request error"
TT_STARTED=0; TT_GW_AG_ID=""; TT_MS_AG_ID=""; ADMIN_SUB=""
# --- 지금 실행 (STEP 8r). 8t 의 창 안에서 돌고 8t 의 정리가 함께 걷는다. 실행의 전제인 세션 예산(P41 10절)이다 ---
TT_RUN_AG="install-run-now"; TT_RUN_BUDGET=200000
TT_RUN_AG_ID=""; TT_RUN_K8S=""; RUN_NOTE=""
# 8t 가 바꾼 ConfigMap 은 lib/e2e_control_plane.sh 의 창(CONFIG_RESTORE·CONFIG_RESTORE_HINT·CONFIG_ROLL_PENDING)이 되돌린다
# 8t 전 e2e 관리자의 $TENANT manual 역할과 그 표시 (lib/e2e_turn_tokens.sh turn_member_developer)
TURN_MEMBER_SET=0; TURN_ROLE_BEFORE=""

. "$HERE/lib/e2e_pf.sh"
. "$HERE/lib/e2e_report.sh"
# wait_no_terminating(롤아웃 뒤 옛 플랫폼 파드가 내려갈 때까지 기다린다)과 platform_config(플랫폼 ConfigMap 의 값)는
# 여기에 있다. 둘 다 PLATFORM_NS 를 쓴다
PLATFORM_NS="$NS"
. "$HERE/lib/e2e_control_plane.sh"
# api·jq_·find_id·listed_id·api_delete·wait_ready·wait_gone·upload_backend_api·register_data_service·
# save_doc·deploy_doc·require_features·ensure_stub_provider·drop_stub_provider·remove_providers 는 여기에 있다.
# wait_ready 는 플랫폼이 다시 뜨는 중(롤아웃·재부팅 흉내 뒤 port-forward 재연결)이면 기다린다
. "$HERE/lib/e2e_api.sh"
# wait_auth_reapplied(데이터 플레인 상태 API 가 남은 재렌더 없음을 말할 때까지 기다린다)는 여기에 있다. BASE 를 쓴다
. "$HERE/lib/e2e_auth_mode.sh"
# chat·msg·sse_·stub_call (STEP 8t 의 턴)은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# platform_mongo_here·turn_grants_of·turn_member_developer·turn_member_restore·wait_gateway_gate·audit_expect (STEP 8t)는
# 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# Redis 파드의 redis_exec(STEP 4)와 redis_agent_clean(정리), memory 바인딩의 TLS 기다림 wait_memory_tls(STEP 8·8r)는 여기에 있다
. "$HERE/lib/e2e_redis.sh"
# 지금 실행과 그 기록을 기다리는 RUN_TERMINAL·runs_on·run_now·run_wait·run_field (STEP 8r)는 여기에 있다
. "$HERE/lib/e2e_runs.sh"
# hub_tools HUB_ID LABEL -> HUB_TOOLS 에 플랫폼이 그 자리에서 한 tools/list 의 이름(정렬). 프록시가 막 떴으면 잠깐 못
# 물어본다. 값을 echo 로 돌려주지 않는 것은 $( ) 안의 die 가 부른 쪽을 멈추지 못해서다(lib/e2e_api.sh 의 도우미도 같다)
HUB_TOOLS=""
hub_tools() {
  local i code=""
  for ((i = 0; i < 24; i++)); do
    code="$(api GET "/hub/$1/tools" || true)"
    if [ "$code" = "200" ] && [ "$(jq_ 'd["listed"]')" = "True" ]; then HUB_TOOLS="$(jq_ 'sorted(t["name"] for t in d["tools"])')"; return 0; fi
    sleep 5
  done
  die "$2" "GET /hub/$1/tools -> $code, not listed: $(cat "$WORK/body")"
}
# check_resources NS -> 모든 파드의 init·본 컨테이너에 cpu·memory requests·limits 가 있는지. 한 줄 요약을 찍고 빠지면 1
check_resources() {
  "${K[@]}" -n "$1" get pods -o json | python3 -c '
import json, sys
pods = json.load(sys.stdin)["items"]
missing, defaulted, n = [], [], 0
for p in pods:
    if p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    name = p["metadata"]["name"]
    note = (p["metadata"].get("annotations") or {}).get("kubernetes.io/limit-ranger")
    if note:
        defaulted.append(f"{name}: {note}")
    for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]:
        n += 1
        r = c.get("resources") or {}
        gaps = [f"{part}.{res}" for part in ("requests", "limits") for res in ("cpu", "memory") if res not in (r.get(part) or {})]
        if gaps:
            cname = c["name"]
            missing.append(f"{name}/{cname} lacks {gaps}")
print(f"{len(pods)} pods, {n} containers" + (f"; LimitRange filled: {defaulted}" if defaulted else ""))
if missing:
    print("MISSING " + "; ".join(missing))
    sys.exit(1)
'
}

# tenant_resources STEP -> 테넌트 네임스페이스의 컨테이너를 check_resources 로 보고 한 줄 요약을 out 에 남긴다. home 에
# 기본 자원이 있는 설치(TENANT_LIMITS, STEP 3 이 정한다)에서는 빠진 컨테이너가 실패다
tenant_resources() {
  out="$(check_resources "$TENANT_NS")" && return 0
  [ "$TENANT_LIMITS" = true ] && { echo "$out"; die "$1" "$TENANT_NS 에 자원이 빠진 컨테이너가 있다"; }
  echo "$out"
  out="${out%%$'\n'*}; requests·limits missing in some containers, not required (home has no default resources)"
}

# 만든 것을 거꾸로 지운다. STEP 10 과 실패 뒤의 트랩이 함께 쓴다. 이미 없으면(404) 넘어간다
teardown() {
  [ "$TORN_DOWN" = 1 ] && return 0
  TORN_DOWN=1
  local path code
  # 거꾸로 지운다. 게시 항목을 Gateway 가, MCP Server 를 Agent·게시가 붙들고 있다
  for path in ${AG_ID:+"/agents/$AG_ID"} ${GWPUB:+"/hub/$GWPUB"} ${GW_ID:+"/gateways/$GW_ID"} \
    ${HMS:+"/mcp-servers/$MS_ID/publish"} ${MS_ID:+"/mcp-servers/$MS_ID"} ${BP_ID:+"/backend-apis/$BP_ID"} \
    ${RDS_ID:+"/data-services/$RDS_ID"} ${PG_ID:+"/data-services/$PG_ID"}; do
    code="$(api DELETE "$path" || true)"
    case "$code" in
      200 | 204 | 404) ;;
      *) echo "!! DELETE $path -> $code: $(cat "$WORK/body")"; TEARDOWN_ERR=$((TEARDOWN_ERR + 1)) ;;
    esac
  done
  # 체크포인터가 기동 때 만든 인덱스와 키를 이 인스턴스 접두어로만 걷는다. 이 Agent 는 채팅하지 않는다
  if [ -n "$AG_ID" ] && [ -n "$RDS_ID" ]; then
    redis_agent_clean "$AG_ID" \
      || { echo "!! Redis cleanup for daegam-agent:$AG_ID: failed"; TEARDOWN_ERR=$((TEARDOWN_ERR + 1)); }
  fi
  [ "$PROVIDER_ADDED" = 0 ] || remove_providers "$PROVIDER"
  if [ "$SECRET_ADDED" = 1 ]; then
    if "${K[@]}" -n "$NS" delete secret "$PROVIDER_SECRET" --ignore-not-found >/dev/null; then SECRET_ADDED=0; fi
  fi
  "${K[@]}" -n "$NS" delete pod e2e-np-open e2e-np-ui e2e-np-meta --ignore-not-found --wait=false >/dev/null 2>&1 || true
  # STEP 5n·5e 의 임시 파드. 그 단계가 도중에 끝나면 테넌트 네임스페이스에 남는다
  if [ -n "$TENANT_NS" ]; then
    "${K[@]}" -n "$TENANT_NS" delete pod e2e-np-member e2e-np-proxy e2e-np-egress --ignore-not-found --wait=false \
      >/dev/null 2>&1 || true
  fi
  return 0
}
# roll_platform_pf: 플랫폼을 다시 띄운 뒤(restart_platform) port-forward 를 새 파드로 다시 잇는다. ConfigMap 을 고친 뒤
# 쓴다(STEP 8t 와 트랩). 실패하면 0 이 아니다
roll_platform_pf() {
  restart_platform || return 1
  restart_pf "$API_PORT"
  wait_http "http://127.0.0.1:${API_PORT}/healthz" \
    || { echo "!! the platform did not answer through the port-forward after the rollout"; return 1; }
}

# STEP 8t 가 바꾼 것을 되돌린다. ConfigMap 을 먼저 되돌린다. 굴리기 전까지 떠 있는 파드는 옛 env 그대로라 뒤의 API 정리가
# 그대로 된다. 그다음 Agent 둘을 지우고, 이 게이트의 스텁 provider 가 남았으면 걷고(앞 실행이 남긴 것 포함, 스텁을 막은 뒤에는
# provider 를 고치는 모든 저장이 거절되기 때문이다), e2e 관리자의 $TENANT 소속을 8t 전으로 둔 뒤 플랫폼을 굴린다. 되돌리지
# 못한 것은 표시를 남겨 다음 부름(EXIT 트랩)이 다시 하고, 사람이 칠 명령을 찍은 뒤 1 이다
drop_turn_agent() { # ID -> 지워 404 가 되면 0
  api_delete "/agents/$1"
  wait_gone "/agents/$1" 60
}
drop_turn_check() {
  local errs=0
  config_window_restore || errs=$((errs + 1))
  if [ "$TT_STARTED" = 1 ] && [ ${#AUTH[@]} -gt 0 ]; then
    if [ -n "$TT_GW_AG_ID" ]; then
      if drop_turn_agent "$TT_GW_AG_ID"; then TT_GW_AG_ID=""; else errs=$((errs + 1)); fi
    fi
    if [ -n "$TT_MS_AG_ID" ]; then
      if drop_turn_agent "$TT_MS_AG_ID"; then TT_MS_AG_ID=""; else errs=$((errs + 1)); fi
    fi
    # 8r 의 Agent. 지우면 실행 목록이 404 라 실행 세션을 먼저 읽고, 지운 뒤 체크포인터의 키·검색 인덱스와 실행 세션의
    # 접두어 밖 키를 걷는다(Agent 삭제는 Redis 를 건드리지 않는다)
    if [ -n "$TT_RUN_AG_ID" ]; then
      run_sessions "$TT_RUN_AG_ID" || echo "!! the run sessions of $TT_RUN_AG_ID could not be read; their write_keys_zset keys may be left"
      if drop_turn_agent "$TT_RUN_AG_ID" && redis_agent_clean "$TT_RUN_AG_ID" ${RUN_SIDS[@]+"${RUN_SIDS[@]}"}; then
        TT_RUN_AG_ID=""
      else
        echo "!! the Agent $TT_RUN_AG ($TT_RUN_AG_ID) or its Redis keys may be left"; errs=$((errs + 1))
      fi
    fi
    drop_stub_provider "$TT_STUB" || errs=$((errs + 1))
    turn_member_restore || errs=$((errs + 1))
  fi
  if [ "$CONFIG_ROLL_PENDING" = 1 ]; then
    if roll_platform_pf; then
      CONFIG_ROLL_PENDING=0
      echo ">> ConfigMap $AUTH_MODE_CONFIGMAP restored, platform rolled"
    else
      echo "!! roll the platform by hand: kubectl --context $CTX -n $NS rollout restart deploy/daegam-platform"
      errs=$((errs + 1))
    fi
  fi
  [ "$errs" = 0 ]
}

cleanup() {
  # E2E_INSTALL_KEEP=1 이어도 8t 는 되돌린다. 공개 설치본에 위임 발급과 스텁 허용을 남기지 않는다
  drop_turn_check || true
  if [ "${E2E_INSTALL_KEEP:-0}" != 1 ] && [ ${#AUTH[@]} -gt 0 ]; then teardown; fi
  stop_pf "$PF_API" "$API_PORT"
  stop_pf "$PF_UI" "$UI_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

[ -n "$CTX" ] || die 0 "INSTALL_CONTEXT 가 비었다 (설치 값 파일 또는 make e2e-install INSTALL_CONTEXT=...)"
# 운영자가 만든 e2e 전용 관리자와 그 비밀번호를 USER_NAME·PASSWORD 에 둔다. orbstack 이면 멈춘다
install_admin 0
# 이 후보의 make e2e-install-extra 가 e2e-runs 를 돌면 STEP 8r 을 건너뛴다. e2e-runs STEP 0~2 가 같은 설치본에서 실행의 전제를
# 갖춘 Agent 와 TLS memory, 사람의 이름으로 끝나는 지금 실행을 본다. 후보를 모르면(E2E_TAG 가 없다) 8r 은 돈다
RUNS_FOLLOW=0; rc=0
install_extra_runs e2e-runs || rc=$?
case $rc in
  0) RUNS_FOLLOW=1 ;;
  1) ;;
  *) die 0 "reading the gates of make e2e-install-extra for E2E_TAG=${E2E_TAG:-} failed (the line above says why)" ;;
esac
echo ">> context=$CTX tenant=$TENANT api=$API user=$USER_NAME reboot=${E2E_INSTALL_REBOOT:-0} require_limits=$REQUIRE_LIMITS runs_follow=$RUNS_FOLLOW"

# ---------- 1. 서빙 빌드 == 이 checkout 의 도장 ----------
start_pf "$NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID
start_pf "$NS" svc/daegam-ui "${UI_PORT}:8080"; PF_UI=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward svc/daegam-platform did not open :$API_PORT"
wait_port "$UI_PORT" || die 1 "port-forward svc/daegam-ui did not open :$UI_PORT"
# 도장은 그 구성요소 경로의 커밋된 트리에서 나오므로 이미지의 태그(판 번호일 수 있다)가 아니라 checkout 과 맞댄다.
# 커밋 안 된 변경이 있으면 -dirty 라 어느 서빙 빌드와도 같지 않다
P_STAMP="$(sh "$ROOT/scripts/build-stamp.sh" platform)" || die 1 "scripts/build-stamp.sh platform failed"
U_STAMP="$(sh "$ROOT/scripts/build-stamp.sh" ui)" || die 1 "scripts/build-stamp.sh ui failed"
curl -fsS "$API/version" -o "$WORK/body" || die 1 "GET /api/version failed"
P_BUILD="$(jq_ 'd["api_build"]')"
curl -fsS "http://127.0.0.1:${UI_PORT}/build.json" -o "$WORK/body" || die 1 "GET /build.json failed"
U_BUILD="$(jq_ 'd["ui_build"]')"
case "$P_BUILD" in *"+$P_STAMP") ;; *) die 1 "platform api_build=$P_BUILD, this checkout's stamp $P_STAMP (run from a clean checkout of the deployed commit)" ;; esac
case "$U_BUILD" in *"+$U_STAMP") ;; *) die 1 "ui ui_build=$U_BUILD, this checkout's stamp $U_STAMP (run from a clean checkout of the deployed commit)" ;; esac
pass 1 "platform $P_BUILD, ui $U_BUILD (이 checkout 의 도장과 같다)"

# 로그인. 첫 로그인의 비밀번호 바꾸기가 남아 있으면 멈춘다(사람이 화면에서 끝낸다). 까닭은 local_login_kept 가 찍는다
TOKEN="$(local_login_kept "$USER_NAME" "$PASSWORD")" || die 1 "login as $USER_NAME failed"
AUTH=(-H "Authorization: Bearer $TOKEN")
# 이 시각 뒤의 틱이 남은 재렌더 없음을 말해야 한다. 앞의 틱은 옛 플랫폼의 것일 수 있다. 까닭은 wait_auth_reapplied 가 찍는다
wait_auth_reapplied "$TOKEN" "$(date -u +%Y-%m-%dT%H:%M:%S)" || die 1 "the data plane still shows pending re-renders"
echo ">> data plane: nothing pending"
unset TOKEN

# ---------- 2. 제어면 자원 + NetworkPolicy 집행 ----------
# requests·limits 를 요구하는지는 설치의 일이다(E2E_REQUIRE_LIMITS). 요구하지 않으면 빠진 것을 찍고 지나간다
LIMITS_NOTE="requests·limits complete"
for ns in "$NS" "$OP_NS"; do
  if out="$(check_resources "$ns")"; then
    echo "  $ns: $out"
  elif [ "$REQUIRE_LIMITS" = true ]; then
    echo "$out"; die 2 "$ns 에 자원이 빠진 컨테이너가 있다 (E2E_REQUIRE_LIMITS=true)"
  else
    echo "  $ns: $out"
    LIMITS_NOTE="requests·limits missing in some containers, not required (E2E_REQUIRE_LIMITS=false)"
  fi
done
# 위 검사가 문서 파싱 서비스의 컨테이너를 실제로 보았는지 따로 본다. 파드가 없으면 검사가 그 컨테이너를 지나친다
DOCPARSE_PODS="$("${K[@]}" -n "$NS" get pods -l app=daegam-docparse --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.metadata.name}/{.spec.containers[*].name} {end}')"
case " $DOCPARSE_PODS" in *"/docparse "*) ;; *) die 2 "Running 인 daegam-docparse 파드에 docparse 컨테이너가 없다: [$DOCPARSE_PODS]" ;; esac
PLATFORM_IP="$("${K[@]}" -n "$NS" get svc daegam-platform -o jsonpath='{.spec.clusterIP}')"
# 파드 IP 에 정책이 걸리기 전에 curl 이 나가지 않게 잠깐 쉰다. 라벨 달린 파드는 DNS 도 막히므로 ClusterIP 로 간다.
# app=daegam-ui 라벨은 Service daegam-ui 도 고르므로 늘 실패하는 readiness 로 엔드포인트에 들지 않게 한다.
# STEP 5n 도 이것으로 테넌트 네임스페이스에서 잰다
probe_pod() { # NAME KEY=VALUE NAMESPACE URL... -> URL 마다 로그의 http 코드 한 줄
  local name="$1" label="$2" ns="$3"
  shift 3
  python3 - "$name" "$label" "$ns" "$CURL_IMAGE" "$@" <<'PY' | "${K[@]}" apply -f - >/dev/null
import json, sys
name, label, ns, image = sys.argv[1:5]
key, _, value = label.partition("=")
curls = "; ".join(f"echo code=$(curl -s -m 5 -o /dev/null -w '%{{http_code}}' {url})" for url in sys.argv[5:])
print(json.dumps({
  "apiVersion": "v1", "kind": "Pod",
  "metadata": {"name": name, "namespace": ns, "labels": {key: value, "app.kubernetes.io/part-of": "daegam-e2e"}},
  "spec": {
    "restartPolicy": "Never", "automountServiceAccountToken": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 100, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{
      "name": "curl", "image": image, "imagePullPolicy": "IfNotPresent",
      "command": ["sh", "-c", f"sleep 8; {curls}"],
      "readinessProbe": {"exec": {"command": ["false"]}, "periodSeconds": 1},
      "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }],
  },
}))
PY
  local phase="" i
  for ((i = 0; i < 60; i++)); do
    phase="$("${K[@]}" -n "$ns" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    case "$phase" in Succeeded | Failed) break ;; esac
    sleep 2
  done
  "${K[@]}" -n "$ns" logs "$name" 2>/dev/null | sed -n 's/^code=//p'
  "${K[@]}" -n "$ns" delete pod "$name" --wait=false >/dev/null 2>&1 || true
}
"${K[@]}" -n "$NS" delete pod e2e-np-open e2e-np-ui --ignore-not-found >/dev/null
PLATFORM_HEALTHZ="http://$PLATFORM_IP:9000/healthz"
OPEN_CODE="$(probe_pod e2e-np-open app=daegam-e2e-probe "$NS" "$PLATFORM_HEALTHZ")"
UI_CODE="$(probe_pod e2e-np-ui app=daegam-ui "$NS" "$PLATFORM_HEALTHZ")"
[ "$OPEN_CODE" = "200" ] || die 2 "라벨 없는 임시 파드가 플랫폼에 닿지 못했다 (code=$OPEN_CODE). 정책과 무관한 연결 문제다"
if [ "$UI_CODE" = "000" ]; then MEASURED=enforced; else MEASURED=unenforced; fi
# 설치의 값(차트의 home.networkPolicy)이 ConfigMap 에 못 박은 판정이다. 게이트는
# 그것을 고치지 않는다. 못 박은 값이 실측과 다르면 실패이고, 비었으면(차트의 기본) 플랫폼의 탐지가 그대로 서고 끝에 못 박을
# 값을 알린다
PINNED="$(platform_config DAEGAM_HOME_NETWORK_POLICY)"
NP_BAD=""
[ "$MEASURED" = enforced ] \
  || NP_BAD="app=daegam-ui 파드가 플랫폼에 닿았다 (code=$UI_CODE). 이 클러스터는 NetworkPolicy 를 집행하지 않는다"
if [ -z "$PINNED" ]; then
  NP_PIN_NOTE="home 의 NetworkPolicy 판정은 탐지다. 실측으로 못 박으려면 설치의 값에 home.networkPolicy=$MEASURED 를 둔다"
elif [ "$PINNED" != "$MEASURED" ]; then
  NP_BAD="${NP_BAD:+$NP_BAD. }ConfigMap 의 DAEGAM_HOME_NETWORK_POLICY 는 '$PINNED' 인데 실측은 '$MEASURED' 다. 설치의 값(home.networkPolicy)을 $MEASURED 로 고치고 다시 배포한다"
fi
if [ -n "$NP_BAD" ]; then
  record_fail 2 "$NP_BAD"
else
  pass 2 "제어면 컨테이너 $LIMITS_NOTE (daegam-docparse: $DOCPARSE_PODS); NetworkPolicy 집행 (라벨 없는 파드 $OPEN_CODE, app=daegam-ui 파드 차단; ConfigMap 의 판정 '${PINNED:-없음, 탐지}')"
fi
# STEP 3 이 home 의 network_policy 로 받는 값(공백으로 나눈다). 못 박았으면 그 값이고, preflight 는 unenforced 를 unknown
# 으로 보인다(services/clusters.py ClusterService.preflight). 비었으면 탐지다. 정책 컨트롤러를 찾으면 enforced, k3s 면
# presumed 다(k8s/preflight.py)
case "$PINNED:$MEASURED" in
  enforced:*) WANT_NP=enforced ;;
  unenforced:*) WANT_NP=unknown ;;
  *:enforced) WANT_NP="enforced presumed" ;;
  *) WANT_NP="presumed unknown" ;;
esac

# ---------- 3. home Cluster ----------
HOME_ID=""
for ((i = 0; i < 30; i++)); do
  code="$(api GET /clusters || true)"
  [ "$code" = "200" ] && HOME_ID="$(find_id home)"
  [ -n "$HOME_ID" ] && break
  sleep 2
done
[ -n "$HOME_ID" ] || die 3 "cluster home missing"
code="$(api GET "/clusters/$HOME_ID")"; [ "$code" = "200" ] || die 3 "GET /clusters/$HOME_ID -> $code"
# 테넌트 컨테이너의 requests·limits 는 home 의 기본 자원(DAEGAM_HOME_RESOURCES)에서 온다. 설치가 그것을 두지 않았으면
# 플랫폼이 지은 컨테이너에 자원이 없는 것이 맞으므로 tenant_resources 가 빠진 것을 찍고 지나간다
[ "$(jq_ 'bool((d.get("scheduling") or {}).get("resources"))')" = True ] && TENANT_LIMITS=true
# 샌드박스 이미지(P40 10절)는 DAEGAM_HOME_IMAGES 밖의 따로인 env 이고 home 의 sandbox.image 로 간다. ConfigMap 을 한 번
# 읽어 맞댄다. 없는 키는 맞대지 않고, 있는데 빈 키는 실패이며, 샌드박스 이미지는 늘 있어야 한다. 맞댄 필드를 COMPARED 에 남긴다
"${K[@]}" -n "$NS" get configmap "$AUTH_MODE_CONFIGMAP" -o json >"$WORK/home-config.json" \
  || die 3 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
python3 - "$WORK/body" "$WANT_NP" "$WORK/home-config.json" >"$WORK/compared" <<'PY' || die 3 "home cluster does not match the ConfigMap"
import json, sys
home = json.load(open(sys.argv[1])); measured = sys.argv[2]
data = json.load(open(sys.argv[3])).get("data") or {}
bad, want = [], {"platform_internal_url"}
JSON_KEYS = ("DAEGAM_HOME_IMAGES", "DAEGAM_HOME_RESOURCES", "DAEGAM_HOME_LIMIT_RANGE")
for key in JSON_KEYS + ("DAEGAM_HOME_INGRESS_TLS",):
    if key in data and not data[key].strip():
        bad.append(f"ConfigMap {key} is present but empty")
images, resources, limit_range = (json.loads(data[k]) if (data.get(k) or "").strip() else None for k in JSON_KEYS)
sandbox_image = data.get("DAEGAM_HOME_SANDBOX_IMAGE", "")
ingress_tls = data.get("DAEGAM_HOME_INGRESS_TLS", "")
pf = home["status"]["preflight"]
if not pf["ok"]: bad.append(f"preflight not ok: missing={pf['missing']}")
if pf["network_policy"] not in measured.split(): bad.append(f"network_policy={pf['network_policy']} (want one of {measured})")
for k, v in (images or {}).items():
    want.add(f"images.{k}")
    if home["images"].get(k) != v: bad.append(f"images.{k}={home['images'].get(k)!r} (env {v!r})")
if resources is not None:
    want.add("scheduling.resources")
    if home["scheduling"]["resources"] != resources: bad.append(f"scheduling.resources={home['scheduling']['resources']}")
if limit_range is not None:
    want.add("namespaces.limit_range")
    if home["namespaces"]["limit_range"] != limit_range: bad.append(f"namespaces.limit_range={home['namespaces']['limit_range']}")
if ingress_tls:
    want.add("ingress.tls")
    if home["ingress"]["tls"] != ingress_tls: bad.append(f"ingress.tls={home['ingress']['tls']} (env {ingress_tls})")
if not sandbox_image: bad.append("ConfigMap DAEGAM_HOME_SANDBOX_IMAGE is empty")
want.add("sandbox.image")
if home["sandbox"]["image"] != sandbox_image: bad.append(f"sandbox.image={home['sandbox']['image']!r} (env {sandbox_image!r})")
if not want <= set(home["env_managed"]): bad.append(f"env_managed lacks {sorted(want - set(home['env_managed']))}")
for b in bad: print("  " + b, file=sys.stderr)
print(f"  k8s {pf['k8s_version']}, toolhive {pf['toolhive_version']}, policy {pf['policy_controller']}, warnings {home.get('warnings')}", file=sys.stderr)
print(pf["network_policy"], " ".join(sorted(want)))
sys.exit(1 if bad else 0)
PY
read -r HOME_NP COMPARED <"$WORK/compared"
pass 3 "home id=$HOME_ID preflight ok, network_policy=$HOME_NP (accepted: $WANT_NP); $COMPARED == ConfigMap and in env_managed"

# ---------- 4. 설치본이 플랫폼 곁에 둔 Redis·Postgres ----------
# 저장소는 플랫폼 네임스페이스의 StatefulSet redis·postgres·mongodb 다. 없는 것과 kubectl 이 실패한 것을 가른다.
# 그 Redis 를 둔 배포는 ConfigMap 의 DAEGAM_HOME_REDIS_TLS 에 TLS 끝점을 적어야 한다
shipped() { # NAME -> 1 이나 0 을 찍는다. kubectl 이 실패하면 0 이 아니다
  local found
  found="$("${K[@]}" -n "$NS" get statefulset "$1" --ignore-not-found -o name)" || return 1
  if [ -n "$found" ]; then echo 1; else echo 0; fi
}
SHIPPED_REDIS="$(shipped redis)" || die 4 "kubectl get statefulset redis in $NS failed"
SHIPPED_PG="$(shipped postgres)" || die 4 "kubectl get statefulset postgres in $NS failed"
SHIPPED_MONGO="$(shipped mongodb)" || die 4 "kubectl get statefulset mongodb in $NS failed"
REDIS_TLS_RAW="$(platform_config DAEGAM_HOME_REDIS_TLS)" || die 4 "ConfigMap $AUTH_MODE_CONFIGMAP 을 읽지 못했다"
[ "$SHIPPED_REDIS" = 0 ] || [ -n "$REDIS_TLS_RAW" ] \
  || die 4 "statefulset/redis is in $NS but ConfigMap $AUTH_MODE_CONFIGMAP has no DAEGAM_HOME_REDIS_TLS for its TLS endpoint"
if [ "$SHIPPED_REDIS" = 1 ]; then
  REDIS_PLAIN="$(redis_plain_of "$REDIS_TLS_RAW")" || die 4 "DAEGAM_HOME_REDIS_TLS 를 읽지 못했다: $REDIS_TLS_RAW"
  PLAIN_PING="$(redis_exec 'redis-cli -p 6379 ping 2>&1' || true)"
  case "$REDIS_PLAIN:$PLAIN_PING" in
    open:PONG) ;;
    open:*) die 4 "Redis 평문 포트 6379 가 답하지 않는다: $PLAIN_PING" ;;
    # 닫힌 포트는 연결을 거절해야 한다. kubectl exec 의 실패처럼 다른 까닭의 실패는 닫힌 것으로 치지 않는다
    closed:*"Connection refused"*) ;;
    closed:*) die 4 "Redis 평문 포트 6379 를 닫았는데 연결을 거절하지 않는다: $PLAIN_PING" ;;
  esac
  MODULES="$(redis_exec 'cli MODULE LIST' | tr '\n' ' ')"
  case " $MODULES " in *" search "*) ;; *) die 4 "Redis MODULE LIST 에 search 가 없다: $MODULES" ;; esac
  case " $MODULES " in *" ReJSON "*) ;; *) die 4 "Redis MODULE LIST 에 ReJSON 이 없다: $MODULES" ;; esac
  # 본문을 변수에 먼저 둔다. register_data_service 의 인자 자리에서 만들면 python 이 실패해도 set -e 가 멈추지 않는다
  ds_body="$(redis_ds_body "$RDS_NAME" "$HOME_ID")" || die 4 "reading the Redis Secret or CA failed"
  register_data_service 4 "$RDS_NAME" "$ds_body"
  unset ds_body
  RDS_ID="$DATA_SERVICE_ID"
  RDS_CKPT="$(jq_ '(d.get("redis") or {}).get("checkpointer")')"
  [ "$RDS_CKPT" = ready ] || die 4 "redis checkpointer=$RDS_CKPT"
  RDS_DETAIL="$(jq_ 'd.get("detail")')"
  case "$RDS_DETAIL" in *"(tls)"*) ;; *) die 4 "redis verify did not use the TLS endpoint: $RDS_DETAIL" ;; esac
  STORES_NOTE="Redis TLS 6380·평문 6379 $REDIS_PLAIN, modules [$MODULES] checkpointer ready over TLS ($RDS_ID, $RDS_DETAIL)"
else
  STORES_NOTE="no StatefulSet redis in $NS"
fi
if [ "$SHIPPED_PG" = 1 ]; then
  install_postgres_database 4 "$PG_NAME" "$NS" "$HOME_ID"
  PG_ID="$DATA_SERVICE_ID"
  STORES_NOTE="$STORES_NOTE; Postgres vector $PG_VECTOR ($PG_ID), verify ok"
else
  STORES_NOTE="$STORES_NOTE; no StatefulSet postgres in $NS"
fi
if [ "$SHIPPED_REDIS$SHIPPED_PG" = 00 ]; then
  cond_skip 4 "the install ships neither Redis nor Postgres next to the platform"
else
  pass 4 "$STORES_NOTE"
fi

# ---------- 5. petstore → MCP Server → 허브 tools/list ----------
upload_backend_api 5 "$BP_NAME" "$SPEC" "펫스토어 설치본"
BP_ID="$BACKEND_API_ID"; BP_VER="$BACKEND_API_VERSION"
# 재실행의 409 는 save_doc·deploy_doc 이 잇는다 (lib/e2e_api.sh 머리말)
save_doc 5 /mcp-servers "$MS_NAME" \
  "{\"name\": \"$MS_NAME\", \"display_name\": \"펫스토어 설치본\", \"backend_profile_id\": \"$BP_ID\", \"spec_version\": $BP_VER, \"tools\": $TOOLS_JSON}"
MS_ID="$SAVED_ID"
HOME_DEPLOY="{\"cluster_id\": \"$HOME_ID\"}"
deploy_doc 5 /mcp-servers "$MS_ID" "$HOME_DEPLOY"
echo ">> waiting up to ${READY_TIMEOUT}s for mcp server $MS_NAME (첫 배포는 이미지를 당긴다)"
wait_ready "/mcp-servers/$MS_ID" 5
TENANT_NS="$(jq_ 'd["k8s"]["namespace"]')"; MS_K8S="$(jq_ 'd["k8s"]["name"]')"
code="$(api POST "/mcp-servers/$MS_ID/publish" '{}')"
case "$code" in 200|201) HMS="$(jq_ 'd["id"]')" ;; *) die 5 "publish $MS_NAME -> $code" ;; esac
hub_tools "$HMS" 5; MS_TOOLS="$HUB_TOOLS"
[ "$MS_TOOLS" = "$TOOLS_WANT" ] || die 5 "tools/list $MS_TOOLS (want $TOOLS_WANT)"
tenant_resources 5
pass 5 "MCP Server $MS_NAME Ready in $TENANT_NS, 허브 게시 $HMS, 플랫폼 경유 tools/list == $MS_TOOLS; $out"

# ---------- 5n. MCP 백엔드 파드에는 프록시만 들어온다 (T67) ----------
# STEP 5 의 tools/list 가 프록시에서 백엔드로 가는 길을 이미 보았다. 여기서는 백엔드 파드의 라벨과, 라벨 없는 테넌트 파드가
# 백엔드에 닿지 못하고 프록시에는 닿는 것, 오퍼레이터의 프록시 라벨(app.kubernetes.io/name=mcpserver)만 단 파드는 백엔드에
# 닿는 것(양성 대조)을 본다
BACKEND_URL="http://mcp-$MS_K8S.$TENANT_NS.svc:8000/mcp"
PROXY_URL="http://mcp-$MS_K8S-proxy.$TENANT_NS.svc:8080/mcp"
if [ "$MEASURED" != enforced ]; then
  cond_skip 5n "이 클러스터는 NetworkPolicy 를 집행하지 않는다 (STEP 2)"
else
  BACKEND_PODS=" $("${K[@]}" -n "$TENANT_NS" get pods -l "daegam.devops.ai.kr/mcp-backend=$MS_K8S" \
    -o jsonpath='{.items[*].metadata.name}') "
  PROXY_PODS=" $("${K[@]}" -n "$TENANT_NS" get pods \
    -l "app.kubernetes.io/name=mcpserver,app.kubernetes.io/instance=$MS_K8S" -o jsonpath='{.items[*].metadata.name}') "
  case "$BACKEND_PODS" in *" $MS_K8S-0 "*) ;; *) die 5n "백엔드 라벨이 StatefulSet 파드 $MS_K8S-0 에 없다: [$BACKEND_PODS]" ;; esac
  [ -n "${PROXY_PODS// /}" ] || die 5n "app.kubernetes.io/name=mcpserver 를 단 $MS_K8S 프록시 파드가 없다"
  for pod in $PROXY_PODS; do
    case "$BACKEND_PODS" in *" $pod "*) die 5n "프록시 파드 $pod 에 백엔드 라벨이 있다" ;; esac
  done
  "${K[@]}" -n "$TENANT_NS" get networkpolicy daegam-mcp-backend >/dev/null || die 5n "$TENANT_NS 에 daegam-mcp-backend 가 없다"
  "${K[@]}" -n "$TENANT_NS" delete pod e2e-np-member e2e-np-proxy --ignore-not-found >/dev/null
  MEMBER_CODES="$(probe_pod e2e-np-member app=daegam-e2e-probe "$TENANT_NS" "$BACKEND_URL" "$PROXY_URL" | tr '\n' ' ')"
  PROXY_LIKE="$(probe_pod e2e-np-proxy app.kubernetes.io/name=mcpserver "$TENANT_NS" "$BACKEND_URL")"
  read -r MEMBER_BACKEND MEMBER_PROXY _ <<EOF
$MEMBER_CODES
EOF
  if [ "$MEMBER_BACKEND" = 000 ] && [ -n "$MEMBER_PROXY" ] && [ "$MEMBER_PROXY" != 000 ] \
    && [ -n "$PROXY_LIKE" ] && [ "$PROXY_LIKE" != 000 ]; then
    pass 5n "백엔드 $MS_K8S-0 은 프록시 라벨 파드만 받는다 (라벨 없는 파드: 백엔드 $MEMBER_BACKEND, 프록시 $MEMBER_PROXY; app.kubernetes.io/name=mcpserver 파드: 백엔드 $PROXY_LIKE)"
  else
    record_fail 5n "라벨 없는 파드의 백엔드 $MEMBER_BACKEND(want 000)·프록시 ${MEMBER_PROXY:-없음}(want != 000), app.kubernetes.io/name=mcpserver 파드의 백엔드 ${PROXY_LIKE:-없음}(want != 000)"
  fi
fi

# ---------- 5e. 테넌트 파드는 인스턴스 메타데이터 주소에 닿지 못한다 (T75) ----------
# egress 정책은 home 의 스위치(block_metadata, ConfigMap 의 DAEGAM_HOME_BLOCK_METADATA)가 켜지고 정책 컨트롤러가 확인된
# 것일 때만 있고, 관리자의 egress 정책이 있는 네임스페이스에는 없다. 없으면 그 까닭을 home 문서에서 읽어 건너뛰고, 까닭이
# 없는데 없으면 실패다. 플랫폼 네임스페이스의
# 파드(egress 정책이 없다)가 양성 대조다. 그 파드도 답을 받지 못하면 이 노드에는 메타데이터 서비스가 없는 것이라 건너뛴다.
# 테넌트 파드는 플랫폼 Service 와 이름으로 찾은 프록시에는 그대로 닿아야 한다
METADATA_URL="http://169.254.169.254/"
if [ "$MEASURED" != enforced ]; then
  cond_skip 5e "이 클러스터는 NetworkPolicy 를 집행하지 않는다 (STEP 2)"
elif ! "${K[@]}" -n "$TENANT_NS" get networkpolicy daegam-tenant-egress >/dev/null 2>&1; then
  code="$(api GET "/clusters/$HOME_ID")"
  [ "$code" = 200 ] || die 5e "GET /clusters/$HOME_ID -> $code"
  if [ "$(jq_ 'd["block_metadata"]')" != True ]; then
    cond_skip 5e "home 의 메타데이터 차단(block_metadata)이 꺼져 있어 $TENANT_NS 에 daegam-tenant-egress 가 없다"
  elif [ "$(jq_ 'any(w.startswith("metadata_block_unsupported") for w in d["warnings"])')" = True ]; then
    cond_skip 5e "home 의 정책 컨트롤러가 확인된 것이 아니어서 egress 정책을 두지 않는다 (metadata_block_unsupported)"
  elif [ "$(jq_ "'$TENANT_NS' in d['status'].get('metadata_skipped', [])")" = True ]; then
    cond_skip 5e "$TENANT_NS 에 관리자의 egress 정책이 있어 egress 정책을 두지 않는다 (metadata_block_skipped)"
  else
    die 5e "home 의 block_metadata 가 켜져 있고 컨트롤러도 확인됐는데 $TENANT_NS 에 daegam-tenant-egress 가 없다"
  fi
else
  "${K[@]}" -n "$NS" delete pod e2e-np-meta --ignore-not-found >/dev/null
  "${K[@]}" -n "$TENANT_NS" delete pod e2e-np-egress --ignore-not-found >/dev/null
  META_CONTROL="$(probe_pod e2e-np-meta app=daegam-e2e-probe "$NS" "$METADATA_URL")"
  EGRESS_CODES="$(probe_pod e2e-np-egress app=daegam-e2e-probe "$TENANT_NS" "$METADATA_URL" "$PLATFORM_HEALTHZ" "$PROXY_URL" | tr '\n' ' ')"
  read -r TENANT_META TENANT_PLATFORM TENANT_PROXY _ <<EOF
$EGRESS_CODES
EOF
  if [ -z "$META_CONTROL" ] || [ "$META_CONTROL" = 000 ]; then
    cond_skip 5e "$NS 의 파드도 $METADATA_URL 에서 답을 받지 못했다 (이 노드에는 메타데이터 서비스가 없다)"
  elif [ "$TENANT_META" = 000 ] && [ "$TENANT_PLATFORM" = 200 ] && [ -n "$TENANT_PROXY" ] && [ "$TENANT_PROXY" != 000 ]; then
    pass 5e "메타데이터 주소: $NS 파드 $META_CONTROL, 테넌트 파드 $TENANT_META; 테넌트 파드의 플랫폼 $TENANT_PLATFORM, 프록시 $TENANT_PROXY"
  else
    record_fail 5e "테넌트 파드의 메타데이터 ${TENANT_META:-없음}(want 000)·플랫폼 ${TENANT_PLATFORM:-없음}(want 200)·프록시 ${TENANT_PROXY:-없음}(want != 000), $NS 파드의 메타데이터 $META_CONTROL"
  fi
fi

# ---------- 6. Gateway → vMCP tools/list ----------
save_doc 6 /gateways "$GW_NAME" "{\"name\": \"$GW_NAME\", \"display_name\": \"설치본 게이트웨이\", \"backends\": [{\"hub_item_id\": \"$HMS\"}]}"
GW_ID="$SAVED_ID"
deploy_doc 6 /gateways "$GW_ID" "$HOME_DEPLOY"
wait_ready "/gateways/$GW_ID" 6
code="$(api POST "/gateways/$GW_ID/publish" '{}')"
case "$code" in 200|201) GWPUB="$(jq_ 'd["id"]')" ;; *) die 6 "publish $GW_NAME -> $code" ;; esac
hub_tools "$GWPUB" 6; GW_TOOLS="$HUB_TOOLS"
[ "$GW_TOOLS" = "$TOOLS_WANT" ] || die 6 "vMCP tools/list $GW_TOOLS (want $TOOLS_WANT)"
tenant_resources 6
pass 6 "Gateway $GW_NAME Ready, 게시 $GWPUB, vMCP 경유 tools/list == $GW_TOOLS; $out"

# ---------- 7. 노출 (edge) ----------
if [ "$(platform_config DAEGAM_HOME_INGRESS_ENABLED)" != true ]; then
  cond_skip 7 "DAEGAM_HOME_INGRESS_ENABLED 가 true 가 아니다 (인프라가 와일드카드 호스트를 열기 전이다)"
else
  code="$(api PATCH "/mcp-servers/$MS_ID" '{"exposure": {"enabled": true, "tls": {"mode": "edge"}}}')"
  [ "$code" = "200" ] || die 7 "PATCH exposure on -> $code"
  front=""
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    api GET "/mcp-servers/$MS_ID" >/dev/null
    front="$(jq_ '((d.get("status") or {}).get("exposure") or {}).get("front_phase")' || true)"
    [ "$front" = Ready ] && break
    sleep 5
  done
  [ "$front" = Ready ] || die 7 "front phase=$front after ${READY_TIMEOUT}s"
  EXT="$(jq_ 'd["status"]["external_url"]')"
  case "$EXT" in https://*) ;; *) die 7 "external_url=$EXT (edge 는 https 다)" ;; esac
  # 새로 켠 노출에는 옛 앞단이 없으므로 넘겨받기 없이 처음부터 vMCP 앞단이 Ingress 뒤에 선다 (P54 6절)
  "${K[@]}" -n "$TENANT_NS" get virtualmcpserver "front-$MS_K8S" -o name >/dev/null || die 7 "VirtualMCPServer front-$MS_K8S missing"
  backend="$("${K[@]}" -n "$TENANT_NS" get ingress "$MS_K8S" -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.number}')" \
    || die 7 "Ingress $MS_K8S missing"
  [ "$backend" = "vmcp-front-$MS_K8S:4483" ] || die 7 "Ingress $MS_K8S backend $backend (want vmcp-front-$MS_K8S:4483)"
  old="$("${K[@]}" -n "$TENANT_NS" get mcpremoteproxy "expose-$MS_K8S" -o name --ignore-not-found)" || die 7 "listing MCPRemoteProxy expose-$MS_K8S failed"
  [ -z "$old" ] || die 7 "a new exposure rendered the old front $old"
  tenant_resources 7
  code="$(api PATCH "/mcp-servers/$MS_ID" '{"exposure": {"enabled": false}}')"
  [ "$code" = "200" ] || die 7 "PATCH exposure off -> $code"
  pass 7 "edge 노출: vMCP 앞단 front-$MS_K8S Ready, Ingress -> $backend, 옛 앞단 없음, external_url=$EXT; 끔; $out"
fi

# ---------- 8. provider + Agent ----------
if ! "${K[@]}" -n "$NS" get secret "$PROVIDER_SECRET" >/dev/null 2>&1; then
  printf '%s' '{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "'"$PROVIDER_SECRET"'", "namespace": "'"$NS"'", "labels": {"app.kubernetes.io/part-of": "daegam-e2e"}}, "stringData": {"api_key": "sk-e2e-install-not-a-real-key"}}' \
    | "${K[@]}" create -f - >/dev/null
  SECRET_ADDED=1
fi
code="$(api GET /settings)"; [ "$code" = "200" ] || die 8 "GET /settings -> $code"
if [ "$(jq_ "any(p['name']=='$PROVIDER' for p in d['llm_providers'])")" != True ]; then
  patch="$(providers_with "$WORK/body" \
    "{\"name\": \"$PROVIDER\", \"kind\": \"openai\", \"model\": \"gpt-4o-mini\", \"api_key_secret\": \"$PROVIDER_SECRET\"}")"
  code="$(api PATCH /settings "$patch")"; [ "$code" = "200" ] || die 8 "PATCH /settings provider -> $code"
  PROVIDER_ADDED=1
fi
code="$(api GET /agents/templates)"; [ "$code" = "200" ] || die 8 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
# Tool 은 STEP 5 의 MCP Server 이고 memory 는 STEP 4 의 Redis 다. 그 Redis 가 없으면 memory 노드와, memory 가 있어야 하는
# 분당 한도(memory_required)를 뺀다(lib/agent_body.py)
if [ -n "$RDS_ID" ]; then MEMORY_ARGS=(--ds-id "$RDS_ID" --rate-limit); MEMORY_NOTE="memory Redis $RDS_NAME"; else MEMORY_ARGS=(); MEMORY_NOTE="no memory"; fi
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "설치본 에이전트" \
  --provider "$PROVIDER" --source "mcp_instance:$MS_ID" ${MEMORY_ARGS[@]+"${MEMORY_ARGS[@]}"}
save_doc 8 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
code="$(api POST /agents/validate "$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); print(json.dumps({"graph": b["graph"], "service": b["service"]}, ensure_ascii=False))' "$WORK/agent.json")")"
[ "$code" = "200" ] && [ "$(jq_ 'd["ok"]')" = True ] || die 8 "validate -> $code: $(cat "$WORK/body")"
deploy_doc 8 /agents "$AG_ID" "$HOME_DEPLOY"
wait_ready "/agents/$AG_ID" 8
AG_K8S="$(jq_ 'd["k8s"]["name"]')"
# 키 env 는 provider 이름에서 나오고 kind 의 옛 env 가 없다. 템플릿에 설정 지문이 있고 init 이 런타임 이미지로 돈다 (P35)
"${K[@]}" -n "$TENANT_NS" get deployment "$AG_K8S" -o json >"$WORK/agent-deploy.json"
AG_ENV="$(deployment_env "$WORK/agent-deploy.json" agent | tr '\n' ' ')"
case " $AG_ENV" in *" $PROVIDER_ENV=secret:"*) ;; *) die 8 "pod env lacks $PROVIDER_ENV: $AG_ENV" ;; esac
case " $AG_ENV" in *" OPENAI_API_KEY="* | *" OPENAI_BASE_URL="*) die 8 "pod env still has the kind-wide OPENAI_* env: $AG_ENV" ;; esac
AG_SHA="$(template_annotation "$WORK/agent-deploy.json" "$CONFIG_SHA_ANNOTATION")"
case "$AG_SHA" in '' | 0) die 8 "pod template lacks $CONFIG_SHA_ANNOTATION: '$AG_SHA'" ;; esac
[ "$(json_of "$WORK/agent-deploy.json" '[c["image"] for c in d["spec"]["template"]["spec"]["initContainers"]] == [c["image"] for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "agent"]')" = True ] \
  || die 8 "the init container does not run the runtime image of $AG_K8S"
tenant_resources 8
summary="provider $PROVIDER(openai, 가짜 키) + Agent $AG_NAME ($AG_K8S) Ready, $MEMORY_NOTE; env $PROVIDER_ENV, no OPENAI_*,"
pass 8 "$summary $CONFIG_SHA_ANNOTATION $AG_SHA, init runs the runtime image; $out"

# ---------- 8m. memory 의 Redis TLS (P61 F3) ----------
if [ -z "$RDS_ID" ]; then
  cond_skip 8m "the install ships no Redis next to the platform, so the Agent of STEP 8 has no memory"
else
  # 재적용이 memory 바인딩을 TLS 끝점으로 옮기고 다시 렌더한 파드가 TLS 로 Redis 에 붙어 Ready 가 되어야 한다
  wait_memory_tls 8m "$TENANT_NS" "$AG_K8S" "$AGENT_TLS_WAIT"
  AG_TLS_ENV="$MEMORY_TLS_ENV"
  # 그 파드는 믿는 CA 의 지문을 단다. CA 가 바뀌면 이 값이 바뀌어 다시 뜬다 (P61)
  AG_CA="$(python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))["spec"]["template"]["metadata"]
print((meta.get("annotations") or {}).get("daegam.devops.ai.kr/redis-ca", ""))
' "$WORK/agent-deploy.json")"
  # 지문은 ConfigMap daegam-redis-ca 의 ca.crt 를 플랫폼처럼 다듬은 값의 SHA-256 앞 16자다(pem.ca_sha256)
  WANT_CA="$("${K[@]}" -n "$NS" get configmap daegam-redis-ca -o 'jsonpath={.data.ca\.crt}' | python3 -c '
import hashlib, sys
print(hashlib.sha256((sys.stdin.read().strip() + "\n").encode()).hexdigest()[:16])')"
  case ",$AG_CA," in
    *",$RDS_ID=$WANT_CA,"*) ;;
    *) die 8m "the Agent pod template does not trust the shipped CA ($RDS_ID=$WANT_CA): '$AG_CA'" ;;
  esac
  code="$(api GET "/data-services/$RDS_ID/plain-consumers")"
  [ "$code" = 200 ] && [ "$(jq_ 'd["consumers"]')" = "[]" ] \
    || die 8m "plain consumers of $RDS_NAME: $code $(cat "$WORK/body")"
  pass 8m "memory Redis over TLS ($AG_TLS_ENV, daegam.devops.ai.kr/redis-ca $AG_CA, no plain consumer)"
fi

# ---------- 8t. 턴 위임 토큰 (P53 8절): 발급을 켠 채로 Agent 의 Tool 호출 감사에 사람과 Agent 가 함께 남는다 ----------
# turn_with_target STEP AGENT_ID KIND ID: 스텁이 get_pets 를 부르는 새 턴을 열고 그 턴의 위임 기록이 그 대상(KIND:ID)의
# 토큰을 실을 때까지 TURN_TARGET_POLL 초마다 새 턴으로 다시 본다. 턴마다 get_pets 가 프록시를 지나 서버에 닿았어야 한다
# (성공했거나 닿지 않는 백엔드의 요청 오류로 끝났다). 막 Ready 가 된 Agent 의 Service 가 아직 파드를 모르는 502·503 은
# 기다린다. TURN_TARGET_WAIT 초를 넘기면 마지막 위임 기록을 찍고 die STEP 이다. 그 대상의 토큰을 실은 위임의
# opened_at·ended_at 을 앞뒤 5초 넓혀 TURN_SINCE·TURN_UNTIL 에 남긴다. 감사 기록과 같은 클러스터의 시계라 이 맥과 노드의
# 시계가 어긋나도 창이 맞는다. 연 턴 수는 TURN_TRIES 다
turn_with_target() {
  local step="$1" agent="$2" target="$3:$4" code sid note="" result window deadline=$((SECONDS + TURN_TARGET_WAIT))
  TURN_TRIES=0
  while :; do
    code="$(chat "/agents/$agent/chat" "$(msg "$TURN_MSG")")"
    case "$code" in
      502 | 503)
        [ "$SECONDS" -lt "$deadline" ] || { cp "$WORK/sse" "$WORK/body"; die "$step" "POST /agents/$agent/chat -> $code until ${TURN_TARGET_WAIT}s"; }
        sleep 5
        continue ;;
    esac
    TURN_TRIES=$((TURN_TRIES + 1))
    result="$(sse_ 'json.dumps(first("tool_result"), ensure_ascii=False)')"
    { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] \
      && [ "$(sse_ '(lambda r: r is not None and r["name"] == "get_pets" and (r["status"] == "success" or "'"$TURN_BACKEND_ERROR"'" in r["content"]))(first("tool_result"))')" = True ]; } \
      || { cp "$WORK/sse" "$WORK/body"; die "$step" "POST /agents/$agent/chat -> $code $(sse_ 'names'): get_pets did not reach the server, tool_result=$result"; }
    sid="$(sse_ 'first("done")["session_id"]')"
    turn_grants_of "$agent" "$sid" "$WORK/grants.json" || die "$step" "reading the turn grants of session $sid failed"
    note="$(json_of "$WORK/grants.json" 'json.dumps([{k: g.get(k) for k in ("sub", "targets", "end_reason")} for g in d])')"
    window="$(grant_window "$WORK/grants.json" "target=$target" 5 180)" \
      || die "$step" "reading the grant's opened_at and ended_at failed: $note"
    if [ -n "$window" ]; then
      read -r TURN_SINCE TURN_UNTIL <<<"$window"
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] \
      || die "$step" "no grant of $agent carried a token for $target within ${TURN_TARGET_WAIT}s ($TURN_TRIES turns); last grants: $note"
    sleep "$TURN_TARGET_POLL"
  done
}
# 위임 기록은 플랫폼 MongoDB 파드 mongodb-0 에서 읽는다. 바깥 MongoDB 를 쓰는 설치본은 8t 와 그 창 안의 8r 을 건너뛴다
if [ "$SHIPPED_MONGO" = 0 ]; then
  cond_skip 8t "the install uses an external MongoDB, and the turn grants are read in the platform's MongoDB pod mongodb-0"
  cond_skip 8r "it runs inside 8t's window, which needs the platform's MongoDB pod"
else
  platform_mongo_here || die 8t "no MongoDB pod mongodb-0 in $NS, so the turn grants cannot be read"
  TT_TOKENS_BEFORE="$(platform_config DAEGAM_TURN_TOKENS_ENABLED)"; TT_STUB_BEFORE="$(platform_config DAEGAM_ALLOW_STUB_PROVIDER)"
  # 설치본은 스텁을 막는다(DAEGAM_ALLOW_STUB_PROVIDER 가 없거나 false 다). 이미 열려 있으면 앞 실행이 되돌리지 못한
  # 것이라 되돌릴 값을 모른다
  [ "$TT_STUB_BEFORE" != true ] \
    || die 8t "ConfigMap $AUTH_MODE_CONFIGMAP already has DAEGAM_ALLOW_STUB_PROVIDER=true (left by an earlier run?): set it back to false, roll the platform and run again"
  [ "$TT_TOKENS_BEFORE" != true ] \
    || echo "!! ConfigMap $AUTH_MODE_CONFIGMAP already has DAEGAM_TURN_TOKENS_ENABLED=true before 8t; it stays true after 8t"
  # 8r 의 실행은 기본으로 켜져 있다. 꺼진 배포는 ConfigMap 을 바꾸기 전에 멈춘다. 실행은 memory 가 있어야 하므로 8r 은
  # STEP 4 의 Redis 가 있을 때만 돌고, 뒤에 e2e-runs 가 돌면 돌지 않는다
  [ -z "$RDS_ID" ] || [ "$RUNS_FOLLOW" = 1 ] || runs_on || die 8r "GET /api/me/runs-as-me -> $(api GET /me/runs-as-me): runs are off in this deployment, but they are on by default (DAEGAM_RUNS_ENABLED); turn them on or check the deployment"
  config_window_plan DAEGAM_TURN_TOKENS_ENABLED=true DAEGAM_ALLOW_STUB_PROVIDER=true \
    || die 8t "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  TT_STARTED=1
  echo ">> ConfigMap $AUTH_MODE_CONFIGMAP turns minting and stub providers on for STEP 8t (restored on exit). If this run dies, $CONFIG_RESTORE_HINT"
  config_window_apply || die 8t "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
  TT_ROLLED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"
  roll_platform_pf || die 8t "the platform did not come back after turning turn tokens on"
  turn_member_developer 8t
  ensure_stub_provider 8t "$TT_STUB"
  code="$(api GET /agents/templates)"; [ "$code" = 200 ] || die 8t "GET /agents/templates -> $code"
  cp "$WORK/body" "$WORK/templates.json"
  # memory 없는 Agent 둘이다. Tool 노드는 그 소스의 get_pets 하나다
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/turn-gw.json" --name "$TT_GW_AG" --display "설치본 위임 Gateway" \
    --provider "$TT_STUB" --source "gateway_instance:$GW_ID" --allow get_pets
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/turn-ms.json" --name "$TT_MS_AG" --display "설치본 위임 MCP Server" \
    --provider "$TT_STUB" --source "mcp_instance:$MS_ID" --allow get_pets
  save_doc 8t /agents "$TT_GW_AG" "$(cat "$WORK/turn-gw.json")"; TT_GW_AG_ID="$SAVED_ID"
  save_doc 8t /agents "$TT_MS_AG" "$(cat "$WORK/turn-ms.json")"; TT_MS_AG_ID="$SAVED_ID"
  # 8r 의 Agent 는 실행의 전제(memory, 세션 예산)를 갖춘다. 셋을 먼저 배포하고 함께 기다린다
  if [ -n "$RDS_ID" ] && [ "$RUNS_FOLLOW" = 0 ]; then
    python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/run-now.json" --name "$TT_RUN_AG" --display "설치본 지금 실행" \
      --provider "$TT_STUB" --source "mcp_instance:$MS_ID" --allow get_pets --ds-id "$RDS_ID" --session-tokens "$TT_RUN_BUDGET"
    save_doc 8r /agents "$TT_RUN_AG" "$(cat "$WORK/run-now.json")"; TT_RUN_AG_ID="$SAVED_ID"
  fi
  deploy_doc 8t /agents "$TT_GW_AG_ID" "$HOME_DEPLOY"
  deploy_doc 8t /agents "$TT_MS_AG_ID" "$HOME_DEPLOY"
  [ -z "$TT_RUN_AG_ID" ] || deploy_doc 8r /agents "$TT_RUN_AG_ID" "$HOME_DEPLOY"
  wait_ready "/agents/$TT_GW_AG_ID" 8t; TT_GW_K8S="$(jq_ 'd["k8s"]["name"]')"
  wait_ready "/agents/$TT_MS_AG_ID" 8t; TT_MS_K8S="$(jq_ 'd["k8s"]["name"]')"
  if [ -n "$TT_RUN_AG_ID" ]; then
    wait_ready "/agents/$TT_RUN_AG_ID" 8r; TT_RUN_K8S="$(jq_ 'd["k8s"]["name"]')"
    require_features 8r runs
  fi
  # MongoDB 레플리카가 하나인 설치에도 상태 API 의 훑기를 몰지 않게 15초마다 본다. 재적용은 네임스페이스마다 분당 세 문서를 다시 렌더한다
  TURN_GATE_TIMEOUT="${E2E_INSTALL_TURN_GATE_TIMEOUT:-600}" TURN_GATE_POLL=15 wait_gateway_gate 8t "$HOME_ID"
  # 문이 열려도 Gateway 의 재렌더가 남아 있을 수 있다. 턴 뒤에 vMCP 파드가 바뀌면 수집 전의 감사 줄을 잃으므로
  # (services/audit_collector.py) 굴린 뒤의 틱이 남은 재렌더 없음을 말할 때까지 기다린다
  wait_auth_reapplied "${AUTH[1]#Authorization: Bearer }" "$TT_ROLLED_AT" || die 8t "the data plane still shows pending re-renders after turn tokens were turned on"
  TURN_MSG="$(stub_call get_pets '{}')"
  turn_with_target 8t "$TT_GW_AG_ID" gateway_instance "$GW_ID"
  GW_SINCE="$TURN_SINCE"; GW_UNTIL="$TURN_UNTIL"; GW_TRIES="$TURN_TRIES"
  turn_with_target 8t "$TT_MS_AG_ID" mcp_instance "$MS_ID"
  MS_SINCE="$TURN_SINCE"; MS_UNTIL="$TURN_UNTIL"; MS_TRIES="$TURN_TRIES"
  # 거절한 호출도 sub·act 를 싣고 denied 로 남으므로 outcome success 만 센다
  audit_expect 8t "$GW_SINCE" \
    "gateway get_pets|$GW_SINCE|$GW_UNTIL|$GW_ID|get_pets|$ADMIN_SUB|svc:agent:$TT_GW_K8S|1|success" \
    "mcp server get_pets|$MS_SINCE|$MS_UNTIL|$MS_ID|get_pets|$ADMIN_SUB|svc:agent:$TT_MS_K8S|1|success"
  # ---------- 8r. 지금 실행 (P41): 8t 의 창 안에서 e2e 관리자의 이름으로 끝까지 돈다 ----------
  if [ -n "$TT_RUN_AG_ID" ]; then
    wait_memory_tls 8r "$TENANT_NS" "$TT_RUN_K8S" "$AGENT_TLS_WAIT"
    run_now 8r "$TT_RUN_AG_ID" "e2e-install run now"
    run_wait 8r "$TT_RUN_AG_ID" "$RUN_ID" "$RUN_TERMINAL"
    [ "$RUN_STATUS" = succeeded ] \
      || die 8r "run now $RUN_ID ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
    [ "$(run_field "d['acts_as']['kind'], d['acts_as']['sub']")" = "person $ADMIN_SUB" ] \
      || die 8r "run now $RUN_ID did not act as $ADMIN_SUB" "$(cat "$WORK/run.json")"
    RUN_NOTE="run now $RUN_ID on $TT_RUN_AG succeeded as $ADMIN_SUB (person), memory Redis over TLS ($MEMORY_TLS_ENV)"
  fi
  drop_turn_check || die 8t "restoring what 8t changed failed (the !! lines above); the EXIT trap tries again"
  # 되돌린 것을 지금 상태로 확인한다
  code="$(api GET /settings)"
  { [ "$code" = 200 ] && [ "$(jq_ "any(p['name'] == '$TT_STUB' for p in d['llm_providers'])")" = False ]; } \
    || die 8t "the stub provider $TT_STUB is still in Platform Settings (GET /settings -> $code)"
  code="$(api GET "/tenants/$TENANT/members")"
  { [ "$code" = 200 ] && [ "$(jq_ "next((m['role'] for m in d if m['source'] == 'manual' and m['sub'] == '$ADMIN_SUB'), '')")" = "$TURN_ROLE_BEFORE" ]; } \
    || die 8t "the membership of $ADMIN_SUB in $TENANT is not back to '${TURN_ROLE_BEFORE:-none}' (GET /tenants/$TENANT/members -> $code)"
  { [ "$(platform_config DAEGAM_TURN_TOKENS_ENABLED)" = "$TT_TOKENS_BEFORE" ] && [ "$(platform_config DAEGAM_ALLOW_STUB_PROVIDER)" = "$TT_STUB_BEFORE" ]; } \
    || die 8t "ConfigMap $AUTH_MODE_CONFIGMAP is not back to DAEGAM_TURN_TOKENS_ENABLED='$TT_TOKENS_BEFORE' DAEGAM_ALLOW_STUB_PROVIDER='$TT_STUB_BEFORE'. $CONFIG_RESTORE_HINT"
  summary="turn tokens on for this step; Gateway gate of $HOME_ID open; grants carried gateway_instance:$GW_ID (turn $GW_TRIES)"
  summary+=" and mcp_instance:$MS_ID (turn $MS_TRIES); get_pets reached both servers (backend unreachable: request error);"
  summary+=" audit outcome success with sub=$ADMIN_SUB act=svc:agent:$TT_GW_K8S at the Gateway and act=svc:agent:$TT_MS_K8S at"
  pass 8t "$summary the MCP Server; agents, stub provider, membership and ConfigMap restored"
  if [ -n "$RUN_NOTE" ]; then
    pass 8r "$RUN_NOTE; its Agent and Redis keys removed with 8t's"
  elif [ -z "$RDS_ID" ]; then
    cond_skip 8r "runs need a memory node, and the install ships no Redis next to the platform"
  else
    cond_skip 8r "make e2e-install-extra runs e2e-runs for this candidate, whose STEP 0 to 2 check run now as a person"
  fi
fi

# ---------- R. 재부팅 흉내 ----------
if [ "${E2E_INSTALL_REBOOT:-0}" = 1 ]; then
  echo ">> deleting every pod in $NS, $OP_NS and $TENANT_NS"
  for ns in "$TENANT_NS" "$OP_NS" "$NS"; do "${K[@]}" -n "$ns" delete pods --all --wait=false >/dev/null; done
  "${K[@]}" -n "$OP_NS" rollout status deploy/toolhive-operator --timeout=600s >/dev/null || die R "toolhive-operator did not come back"
  # 설치본에 있는 저장소만 기다린다. STEP 4 가 MongoDB·Redis·Postgres 의 StatefulSet 을 보았다
  STORES=""
  [ "$SHIPPED_MONGO" = 0 ] || STORES="statefulset/mongodb"
  [ "$SHIPPED_REDIS" = 0 ] || STORES="$STORES statefulset/redis"
  [ "$SHIPPED_PG" = 0 ] || STORES="$STORES statefulset/postgres"
  for sts in $STORES; do
    "${K[@]}" -n "$NS" rollout status "$sts" --timeout=600s >/dev/null || die R "$sts did not come back"
  done
  for dep in daegam-docparse daegam-platform daegam-ui; do
    "${K[@]}" -n "$NS" rollout status "deploy/$dep" --timeout=600s >/dev/null || die R "deploy/$dep did not come back"
  done
  wait_no_terminating daegam-platform || die R "the old platform pod did not go away"
  restart_pf "$API_PORT"; restart_pf "$UI_PORT"
  wait_http "http://127.0.0.1:${API_PORT}/healthz" || die R "the platform did not answer through the port-forward"
  wait_ready "/mcp-servers/$MS_ID" R
  wait_ready "/gateways/$GW_ID" R
  wait_ready "/agents/$AG_ID" R
  hub_tools "$HMS" R; [ "$HUB_TOOLS" = "$TOOLS_WANT" ] || die R "MCP tools/list after the restart: $HUB_TOOLS"
  hub_tools "$GWPUB" R; [ "$HUB_TOOLS" = "$TOOLS_WANT" ] || die R "vMCP tools/list after the restart: $HUB_TOOLS"
  pass R "모든 파드를 지운 뒤 제어면·DB·오퍼레이터·MCP Server·Gateway·Agent 가 Ready, tools/list 둘 다 다시 된다"
fi

# ---------- 9. 테넌트 네임스페이스 자원 ----------
tenant_resources 9
pass 9 "$TENANT_NS: $out"

# ---------- 10. 정리 ----------
teardown
left=""
for ((i = 0; i < 60; i++)); do
  left="$("${K[@]}" -n "$TENANT_NS" get mcpservers,virtualmcpservers,mcpremoteproxies -o name 2>/dev/null | grep -E "$MS_NAME|$GW_NAME" || true)"
  left="$left$("${K[@]}" -n "$TENANT_NS" get deploy "$AG_K8S" -o name 2>/dev/null || true)"
  [ -z "$left" ] && break
  sleep 2
done
[ -z "$left" ] || die 10 "남은 객체: $left"
[ "$TEARDOWN_ERR" = 0 ] || die 10 "지우기 $TEARDOWN_ERR 건이 실패했다 (위 !! 줄)"
[ "$SECRET_ADDED" = 0 ] || die 10 "가짜 키 Secret 이 남았다"
# remove_providers 는 멈추지 않으므로 걷혔는지는 Platform Settings 에서 본다
if [ "$PROVIDER_ADDED" = 1 ]; then
  code="$(api GET /settings)"
  { [ "$code" = 200 ] && [ "$(jq_ "any(p['name'] == '$PROVIDER' for p in d['llm_providers'])")" = False ]; } \
    || die 10 "provider $PROVIDER 가 Platform Settings 에 남았다 (GET /settings -> $code)"
fi
pass 10 "만든 것을 지웠다. $TENANT_NS 의 CR·Agent Deployment 가 없고 provider·Secret 도 걷었다"

[ -z "$NP_PIN_NOTE" ] || echo "!! $NP_PIN_NOTE"
finish
