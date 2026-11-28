#!/usr/bin/env bash
# P32 (D58) 개인 MCP 토큰 e2e. 사람이 화면에서 받는 개인 토큰이 밖의 주소에서 Gateway·노출 MCP Server 를 열고, 회수와
# 계정 사건이 그 토큰을 끊는지 본다. E2E_TARGET 이 orbstack(기본)이나 install 이다.
#
#   make e2e-pat                                          orbstack. STEP 0 이 local 모드로 바꾸고 EXIT trap 이 되돌린다
#   make e2e-pat E2E_PAT_KEEP_MODE=1                      모드와 대상 Gateway·MCP Server 를 남기고 되돌리는 명령을 찍는다.
#                                                         화면 확인(내 MCP 토큰, 연결 카드)을 이 상태에서 한다
#   make e2e-pat E2E_TARGET=install E2E_INSTALL_USER=<관리자>   설치본. 모드는 이미 local 이고 노출해 둔 Gateway 를 읽기만 한다
#
# 전제. orbstack 은 make deploy-local·lab-up(mock-soap)과 Traefik(LoadBalancer Service traefik/traefik)이다. 노출 host 는
# curl --resolve 로 Traefik IP 에 못 박으므로 /etc/hosts 가 필요 없다. 설치본은 운영자가 노출해 둔 Gateway(external_url 이
# https://mcp-gateway.<EXPOSURE_DOMAIN>/mcp 이거나 E2E_PAT_GW_HOST 의 것, 테넌트 E2E_PAT_TENANT 기본 default)다. 그 host 를 정하지
# 않았으면(EXPOSURE_DOMAIN 도 E2E_PAT_GW_HOST 도 없다) STEP 3 부터 조건부로 건너뛰고, 정했는데 그 Gateway 가 없으면 못 돈
# 것(SKIP, INCOMPLETE)으로 건너뛴다. 그 Gateway 의 파드는 굴리지 않는다. 새 키는 ready_at 까지 기다리고 회수는 30초마다 불러
# 잰다(E2E_PAT_WAIT=poll). 설치 값 파일의 E2E_POD_RESTART=true 면 orbstack 처럼 vMCP 파드를 다시 띄워 본다. 관리자
# 자격 증명은 e2e-install 과 같은 E2E_INSTALL_PASSWORD 나 키체인 항목 daegam-e2e-install 이다. 값은 설치 값
# 파일(~/.config/daegam/install.env)이 기본이고 환경이 이긴다.
#
# 두 갈래 모두 플랫폼 관리자로 버리는 사용자 둘을 만든다. u1 은 대상 테넌트 T 의 멤버, u2 는 다른 테넌트 X 의 멤버다. 새
# 사용자는 첫 로그인에서 강제 비밀번호 변경을 지난다(lib/e2e_local_auth.sh 의 local_login_any). install 갈래는 두 사용자의
# 비밀번호를 실행마다 새로 만들고 토큰을 하루짜리로 받는다. 공개 설치본에 저장소에 적힌 비밀번호의 계정이 생기지 않게 하고,
# 실행이 정리 전에 죽어도 남은 토큰이 하루 안에 끝나게 하려는 것이다. 허용과 거부의 판정은 lib/e2e_mcp_access.sh 에
# 있고 e2e_oauth 와 함께 쓴다.
#
# Gate:
#   STEP 0  (orbstack) switch_auth_mode local. ConfigMap 의 모드와 Secret 의 부트스트랩 비밀번호를 바꾸고 제어면을 굴린다
#   STEP 1  관리자 로그인. 상태 API 가 pending=0 failed=0, /auth/config 와 디스커버리의 issuer 가 DAEGAM_PUBLIC_URL, 이
#           설치의 라벨을 단 두 MCPOIDCConfig 가 모든 네임스페이스에서 클러스터 안 jwksUrl 에 Valid=True
#   STEP 2  u1 이 T 의 토큰을 받는다(ES256, kid pat-, iss 공개 주소, groups [daegam-T]). 틀린 비밀번호는 403
#           invalid_credentials, X 의 토큰은 403 not_a_member. u1 의 둘째 토큰(STEP 5 의 대조)과 u2 의 X 토큰도 받는다.
#           STEP 4r 이 쓸 T 의 developer u3 의 토큰도 여기서 받아 STEP 3 의 ready_at 기다림이 함께 덮게 한다
#   STEP 3  대상과 PRM. orbstack 은 레플리카 JWKS 캐시(30초)를 기다린 뒤 calculator MCP Server[Add] 를 pat-ms.e2e.local 로,
#           그것을 쓰는 Gateway 를 pat-gw.e2e.local 로 노출 배포한다. 새 파드는 첫 요청에서 JWKS 를 받으므로 ready_at 과
#           무관하게 새 키를 안다. 설치본은 external_url 이 공개 호스트인 Gateway 를 찾아 읽기만 한다. 토큰의 ready_at 이
#           미래면 그 시각에 60초를 더한 때까지 기다리고(최대 21분), E2E_POD_RESTART=true 면 그 대신 JWKS 캐시를 기다린 뒤
#           그 vMCP 파드를 다시 띄운다. PRM 의 authorization_servers 가 [DAEGAM_PUBLIC_URL],
#           resource 가 외부 주소다
#   STEP 4  u1 의 T 토큰은 허용, u2 의 X 토큰은 거부(orbstack 은 노출 MCP Server 앞단에서도). X 의 tenant-admin 이 된 u2 가
#           X 의 mcp_group 을 T 의 그룹으로 PATCH 하면 422
#   STEP 4s 서비스 어카운트(P53b 6.7절). STEP 2 에서 관리자가 T 에 viewer 계정을 만들고 자기 비밀번호로 낸 토큰(sub
#           sa:<id>, groups [daegam-T])이 대상에서 허용이다. 계정을 지우면 목록에서 사라진다
#   STEP 4r Tool 규칙의 역할(P52 2·3절). STEP 2 에서 T 의 developer 로 둔 u3 의 토큰과 viewer 인 u1 의 토큰이 daegam_roles 에
#           저마다의 역할 그룹(daegam-role:T:<role>)을 싣는다. TOOL 을 내는 허브 항목(orbstack 은 STEP 3 의 게시 항목, 설치본은
#           노출해 둔 Gateway 의 백엔드 항목) 하나에 "developer 이상만" 규칙을 둔 Gateway pat-rules 를 노출 없이 배포하고 vMCP
#           에 port-forward 로 붙는다. u3 의 토큰은 허용, u1 의 토큰은 빈 목록과 403 이다. u3 을 viewer 로 낮추면 그 토큰이
#           revoked·role_changed 다. Gateway 는 이 단계가 지운다
#   STEP 5  u1 이 첫 토큰을 회수한다. E2E_PAT_WAIT=restart(orbstack 과 E2E_POD_RESTART=true 인 설치본의 기본)는 30초 뒤
#           vMCP·앞단 파드를 지워 새 파드에서 회수한 토큰이 401 이고 둘째 토큰이 허용인 것을 함께 본다(401 이 키 제거
#           때문임을 대조로 가른다). poll(설치본의 기본)은 30초마다 불러 401 까지 걸린 시간을 찍고 16분 안이면 통과다
#   STEP 6  u1 의 셋째 토큰을 받은 뒤 T 에서 빼면 그 토큰이 revoked·membership 이다. 파드를 굴려도 되면 30초 뒤 vMCP 파드를
#           지워 401
#   STEP 7  u1 을 T 에 다시 넣고 넷째 토큰을 받은 뒤 막으면 그 토큰이 disabled 로 회수된다(STEP 6 이 u1 의 T 토큰을 모두
#           회수했다). u2 를 지우면 목록이 404 라 X 토큰의 kid 가 /.well-known/jwks.json 에서 2분 안에 빠지는 것으로 본다
#   STEP 8  (orbstack) 내부 홉은 그대로다. deploy/dev/lab/svc_token.py 의 서비스 토큰으로 내부 프록시가 200, 무토큰 401,
#           ?use=service 의 키는 플랫폼 키 하나다
#   STEP 9  (orbstack) 느린 배포 도중의 삭제 (P66). ToolHive 오퍼레이터를 1분쯤 0 으로 내리고 calculator MCP Server
#           하나를 pat-slow.e2e.local 로 노출 배포하면, 오퍼레이터가 서버의 테넌트 울타리 MCPAuthzConfig(0.52 이상)나 앞단의
#           것을 받아들이지 않으므로 배포가 그
#           기다림(AUTHZ_WAIT_SECONDS 30초)에 머문다. 그 사이 스펙 토큰이 살아 있는 것을 보고 문서를 지운 뒤 오퍼레이터를
#           되돌린다. 배포의 실행은 기다림 안의 체크포인트에서 문서가 지워지는 것을 보고 멈추므로 배포 요청은 409
#           state_conflict 로 답한다. 삭제는 그 실행이 지우기를 마친 뒤 답하거나 미룬 답(deleted false)을 준다. 문서가
#           사라지고, 그 문서의 라벨을 단 객체와 오퍼레이터가 만든 Deployment·StatefulSet·Service 가 남지 않고, 스펙
#           토큰이 모두 회수된 것을 본다. 끝으로 같은 host 로 새 문서를 배포해 201 이면 host 선점도 풀린 것이다(P70)
#   정리    STEP 9 가 오퍼레이터를 내린 채 끝났으면 되돌린다. 두 사용자를 지우고, E2E_PAT_KEEP_MODE=1 이 아니면 orbstack 의
#           대상을 지운 뒤 모드를 되돌려 재적용을 기다린다
#
# 노출 에이전트 런타임의 6분 한도는 test_auth_oidc.py 의 가짜 시계 테스트가, 다른 테넌트 토큰의 403 은 make e2e-agent-local
# STEP 13(c2) 가 본다. 출력과 종료 코드는 lib/e2e_report.sh 의 계약이다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표, json_of·jwt_json, env_defaults 는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/e2e_json.sh"
. "$HERE/lib/hosts.sh"

TARGET="${E2E_TARGET:-orbstack}"
POD_RESTART=true   # 대상 Gateway 의 파드를 지워 새 파드에서 볼 수 있는가. 설치본은 설치 값 파일의 E2E_POD_RESTART 다
GW_RC=0            # 설치본에서 노출한 Gateway 를 찾은 결과(lib/e2e_mcp_access.sh install_gateway). 0 이면 찾았다
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_PAT_TENANT:-e2e-pat}"
    TOOL="${E2E_PAT_TOOL:-Add}"; TOOL_ARGS='{"a": 3, "b": 4}'; TOOL_EXPECT=7; TOKEN_DAYS=7 ;;
  install)
    install_env_defaults
    # 운영자의 Gateway 는 E2E_POD_RESTART=true 일 때만 굴린다. 토큰은 서버가 받는 가장 짧은 하루다
    POD_RESTART="$(install_flag E2E_POD_RESTART false)" || exit 1
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_PAT_TENANT:-default}"
    TOOL="${E2E_PAT_TOOL:-getPublicHolidays}"; TOOL_ARGS=""; TOOL_EXPECT=""; TOKEN_DAYS=1 ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
if [ "$POD_RESTART" = true ]; then WAIT_MODE="${E2E_PAT_WAIT:-restart}"; else WAIT_MODE="${E2E_PAT_WAIT:-poll}"; fi
case "$WAIT_MODE" in restart | poll) ;; *) echo "E2E_PAT_WAIT 는 restart 나 poll 이다 (got $WAIT_MODE)"; exit 1 ;; esac
OTHER="${E2E_PAT_OTHER_TENANT:-e2e-pat-x}"
T_GROUP="daegam-$TENANT"   # local 모드에서 T 의 MCP 그룹은 이 이름으로 고정이다 (P32 2절)
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
API_PORT="${E2E_API_PORT:-$PORT_PAT_API}"
PROXY_PORT="${E2E_PROXY_PORT:-$PORT_PAT_PROXY}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
U1="pat-u1-e2e"; U2="pat-u2-e2e"; U3="pat-u3-e2e"
# STEP 4r 의 규칙 Gateway 와 그 vMCP port-forward (P52)
RULES_GW_NAME="pat-rules"; VMCP_PORT="${E2E_VMCP_PORT:-$PORT_PAT_VMCP}"
BP_NAME="pat-calc"; MS_NAME="pat-ms"; GW_NAME="pat-gw"
MS_HOST="${E2E_PAT_MS_HOST:-pat-ms.e2e.local}"; GW_HOST="${E2E_PAT_GW_HOST:-pat-gw.e2e.local}"
WRONG_PW="e2e-wrong-password-1234"
# STEP 9 의 느린 배포와 그것을 붙드는 ToolHive 오퍼레이터
SLOW_NAME="pat-slow"; SLOW_HOST="${E2E_PAT_SLOW_HOST:-pat-slow.e2e.local}"
OP_NS="${TOOLHIVE_NS:-toolhive-system}"; OP_DEPLOY="toolhive-operator"

# 한도와 기다림 (P32 3절·4절). 설정이 아니라 한도 계산의 재료라 여기에 이름 붙여 둔다. 회수 한도와 JWKS 기다림은
# lib/e2e_mcp_access.sh·lib/e2e_personal_tokens.sh 에 있다
JWKS_DROP_LIMIT=120    # 지운 사람의 토큰 키가 JWKS 에서 빠지기까지의 한도
HOOK_SETTLE=90         # 훅이 회수한 토큰이 revoked 로 보이기까지. 슬롯 삭제가 실패했으면 다음 리컨실 틱이 끝낸다
# STEP 9 의 한도. 서버의 테넌트 울타리 MCPAuthzConfig(0.52 이상)나 앞단의 것이 보이는 때가 플랫폼의
# 기다림(AUTHZ_WAIT_SECONDS 30초)이 시작하는 때다.
# 삭제는 그 30초 안에 가야 한다. 삭제는 홀더를 30초까지 기다리고, 미룬 일은 홀더나 다음 리컨실 틱이 끝낸다
SLOW_AUTHZ_SEEN=20     # 배포를 부른 뒤 그 설정이 적용되기까지의 한도(초)
SLOW_GONE=180          # 삭제 뒤 문서가 사라지기까지
SLOW_OBJECTS_GONE=120  # 문서가 사라진 뒤 그 객체가 모두 사라지기까지(소유자를 따르는 가비지 수집 포함)

WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-pat.XXXXXX")"
K=("${KUBECTL:-kubectl}" --context "$CTX")
AUTH=()                 # 관리자 Bearer 헤더 (lib/e2e_api.sh 의 api 와 lib/e2e_calc.sh 가 싣는다)
ADMIN_TOKEN=""; PF_API=""; PF_PROXY=""
U1_ID=""; U2_ID=""; U3_ID=""; U2_DELETED=0; TARGETS_MADE=0
SA_ID=""; SA_NAME="pat-ci-e2e"   # STEP 4s 의 서비스 어카운트
RULES_GW_ID=""; PF_VMCP=""
NS=""; GW_URL=""; MS_URL=""; MS_PIN=""; VMCP_SEL=""; FRONT_SEL=""
URLS=(); PINS=()        # 허용·거부·회수를 보는 외부 주소와 그 --resolve 값. orbstack 은 Gateway 와 노출 앞단, 설치본은 Gateway 하나다
SLOW_ID=""; SLOW_PID=""; SLOW_K8S=""; SLOW_NS=""
OP_UP=""                # 시작할 때 읽은 오퍼레이터의 레플리카 수(orbstack)
OP_REPLICAS=""          # STEP 9 가 내린 오퍼레이터의 원래 레플리카 수. 비어 있지 않으면 정리가 되돌린다

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$PROXY_PORT" "$VMCP_PORT"
# roll_control_plane·platform_config, switch_auth_mode·restore_auth_mode·auth_mode_restore_hint·wait_auth_reapplied
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_auth_mode.sh"
# local_login_any·local_user_ensure·local_set_member·install_admin·local_admin_session 과 비밀번호 셋
. "$HERE/lib/e2e_local_auth.sh"
[ "$TARGET" != install ] || install_run_passwords
# pass·skip·record_fail·die·finish 와 종료 코드 계약
. "$HERE/lib/e2e_report.sh"
# api·api_as·jq_·api_delete·wait_ready (P33 e2e_oauth 와 함께 쓴다)
. "$HERE/lib/e2e_api.sh"
# issue·issue_ok·ready_wait 와 JWKS 기다림 (e2e_oauth 와 함께 쓴다)
. "$HERE/lib/e2e_personal_tokens.sh"
. "$HERE/lib/e2e_service_accounts.sh"
# calc_mcp_server·calc_publish·calc_gateway (e2e_mcp_auth 와 함께 쓴다)
. "$HERE/lib/e2e_calc.sh"
# INIT_PARAMS·rpc_curl·mcp_status·mcp_ingress (WORK 를 쓴다)
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-pat"
. "$HERE/lib/mcp_rpc.sh"
# traefik_ip·pin·exposure·public_gateway·allowed·denied·unauthenticated·wait_unauthenticated·restart_pods·internal_hop
# (e2e_oauth 와 함께 쓴다)
. "$HERE/lib/e2e_mcp_access.sh"

# STEP 9 가 내린 오퍼레이터를 원래 레플리카 수로 되돌리고 롤아웃을 기다린다. 내리지 않았으면 할 일이 없다.
# 정리 단계에서도 부르므로 멈추지 않고, 실패하면 사람이 칠 명령을 찍고 1 이다
restore_operator() {
  [ -n "$OP_REPLICAS" ] || return 0
  if "${K[@]}" -n "$OP_NS" scale "deploy/$OP_DEPLOY" --replicas="$OP_REPLICAS" >/dev/null \
    && "${K[@]}" -n "$OP_NS" rollout status "deploy/$OP_DEPLOY" --timeout=180s >/dev/null; then
    OP_REPLICAS=""
    return 0
  fi
  echo "!! $OP_NS/$OP_DEPLOY is not back: kubectl --context $CTX -n $OP_NS scale deploy/$OP_DEPLOY --replicas=$OP_REPLICAS" >&2
  return 1
}

# 만든 것을 걷는다. 관리자 토큰이 없으면(로그인 전에 끝났으면) 걷을 것도 없다
teardown() {
  [ ${#AUTH[@]} -gt 0 ] || return 0
  [ -z "$SLOW_ID" ] || api_delete "/mcp-servers/$SLOW_ID"
  [ -z "$RULES_GW_ID" ] || api_delete "/gateways/$RULES_GW_ID"
  [ -z "$SA_ID" ] || api_delete "/tenants/$TENANT/service-accounts/$SA_ID"
  [ -z "$U3_ID" ] || api_delete "/users/$U3_ID"
  [ -z "$U1_ID" ] || api_delete "/users/$U1_ID"
  [ -z "$U2_ID" ] || [ "$U2_DELETED" = 1 ] || api_delete "/users/$U2_ID"
  if [ "$TARGETS_MADE" = 1 ] && [ "${E2E_PAT_KEEP_MODE:-0}" != 1 ]; then calc_teardown; fi
}

cleanup() {
  # STEP 9 가 도중에 끝났으면 뒤에서 기다리는 배포 요청(서브셸과 그 curl)을 끝내고 오퍼레이터를 되돌린다. 오퍼레이터가
  # 내려가 있으면 그것이 단 finalizer 를 지닌 CR 의 삭제가 끝나지 않는다
  if [ -n "$SLOW_PID" ]; then
    pkill -P "$SLOW_PID" 2>/dev/null || true
    kill "$SLOW_PID" 2>/dev/null || true
  fi
  restore_operator || true
  teardown
  # 되돌린 뒤 재적용을 기다리는 호출도 API port-forward 로 가므로 그것을 닫기 전에 되돌린다
  if [ "${E2E_PAT_KEEP_MODE:-0}" = 1 ]; then
    auth_mode_restore_hint
    [ -z "$AUTH_MODE_RESTORE" ] || echo "   관리자 admin 의 비밀번호는 scripts/lib/e2e_local_auth.sh 의 LOCAL_PW2 다"
  else
    restore_auth_mode "$ADMIN_TOKEN"
  fi
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  stop_pf "$PF_API" "$API_PORT"
  stop_pf "$PF_PROXY" "$PROXY_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# expect_error GOT STATUS CODE -> 0 이면 받은 상태 GOT 이 STATUS 이고 마지막 응답의 detail.code 가 CODE 다
expect_error() {
  [ "$1" = "$2" ] && [ "$(jq_ "(d.get('detail') or {}).get('code')")" = "$3" ]
}

# token_state UID TOKEN_ID -> 관리자 목록에서 본 "status revoke_reason". 없으면 빈 줄
token_state() {
  [ "$(api GET "/users/$1/tokens")" = 200 ] || return 1
  jq_ "next((f\"{t['status']} {t['revoke_reason']}\" for t in d if t['token_id'] == '$2'), '')"
}

# wait_revoked STEP UID TOKEN_ID REASON -> HOOK_SETTLE 초 안에 revoked REASON 이 아니면 die STEP
wait_revoked() {
  local state="" i
  for ((i = 0; i < HOOK_SETTLE / 5; i++)); do
    state="$(token_state "$2" "$3" || true)"
    [ "$state" = "revoked $4" ] && return 0
    sleep 5
  done
  die "$1" "token $3 is '$state', want 'revoked $4'"
}

# slow_tokens -> 느린 배포 문서(SLOW_ID)가 낸 스펙 토큰 가운데 살아 있는 것의 수. 목록을 읽지 못하면 1 이다
slow_tokens() {
  [ "$(api GET "/backend-apis/$CALC_BP_ID/tokens")" = 200 ] || return 1
  jq_ "len([t for t in d if t['label'] == 'instance:$SLOW_ID' and t['active']])"
}

# slow_objects -> 느린 배포 문서의 객체 가운데 남은 것을 한 줄에 하나 찍는다. 다 사라졌으면 아무것도 찍지 않는다.
# 플랫폼이 렌더하는 객체는 모두 daegam.devops.ai.kr/id 를 달므로 이름으로는 찾지 않는다. 앞단은 vMCP front-<k8s.name> 과
# 그 MCPGroup·MCPServerEntry 다. 오퍼레이터가 CR 에서 만드는 Deployment·StatefulSet·Service 는 CR 이름을 단 toolhive-name
# 라벨이나 app.kubernetes.io/instance 라벨로 찾는다. kubectl 이 실패하면 1 이다. 남은 것이 없다고 읽지 않는다
slow_objects() {
  "${K[@]}" -n "$SLOW_NS" get \
    mcpservers,virtualmcpservers,mcpgroups,mcpserverentries,mcpauthzconfigs,mcpexternalauthconfigs,ingresses,networkpolicies,secrets,configmaps,pods \
    -l "daegam.devops.ai.kr/id=$SLOW_ID" -o name 2>/dev/null || return 1
  "${K[@]}" -n "$SLOW_NS" get deploy,sts,svc -l "toolhive-name in ($SLOW_K8S,front-$SLOW_K8S)" -o name 2>/dev/null || return 1
  "${K[@]}" -n "$SLOW_NS" get deploy,svc -l "app.kubernetes.io/instance=front-$SLOW_K8S" -o name 2>/dev/null
}

# operator_pods -> ToolHive 오퍼레이터 파드를 한 줄에 하나 찍는다. kubectl 이 실패하면 1 이다
operator_pods() {
  "${K[@]}" -n "$OP_NS" get pods -l "app.kubernetes.io/name=$OP_DEPLOY" -o name 2>/dev/null
}

# jwks_has KID -> 0 이면 플랫폼 JWKS 에 그 kid 가 있다
jwks_has() {
  curl -fsS "$BASE/.well-known/jwks.json" -o "$WORK/jwks.json" \
    && [ "$(json_of "$WORK/jwks.json" "'$1' in [k['kid'] for k in d['keys']]")" = True ]
}

echo ">> target=$TARGET context=$CTX tenant T=$TENANT other X=$OTHER api=$API wait=$WAIT_MODE pod_restart=$POD_RESTART"
[ -n "$CTX" ] || die 0 "INSTALL_CONTEXT 가 비었다 (설치 값 파일 또는 make e2e-pat E2E_TARGET=install INSTALL_CONTEXT=...)"

# STEP 9 가 내릴 오퍼레이터의 레플리카 수. 도중에 죽은 이전 실행이 0 으로 남겼으면 모드를 바꾸기 전에 멈춘다
if [ "$TARGET" = orbstack ]; then
  OP_UP="$("${K[@]}" -n "$OP_NS" get "deploy/$OP_DEPLOY" -o jsonpath='{.spec.replicas}')" \
    || die 0 "reading deploy/$OP_DEPLOY in $OP_NS failed"
  [ "$OP_UP" -ge 1 ] \
    || die 0 "deploy/$OP_DEPLOY in $OP_NS has $OP_UP replicas: kubectl --context $CTX -n $OP_NS scale deploy/$OP_DEPLOY --replicas=1"
fi

# ---------- 0. local 모드 (orbstack 만. cleanup 이 되돌린다) ----------
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"
if [ "$TARGET" = orbstack ]; then
  switch_auth_mode local || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to local and rolling the control plane failed"
  pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=local (was ${AUTH_MODE_PREV:-unset}); bootstrap password in Secret; platform and UI rolled out"
else
  install_admin 0
  cond_skip 0 "the install is already in local mode; nothing to switch"
fi

# ---------- 1. 관리자, 재적용, 발급자, MCPOIDCConfig ----------
local_admin_session 1
curl -fsS "$BASE/.well-known/openid-configuration" -o "$WORK/body" || die 1 "GET /.well-known/openid-configuration failed"
[ "$(jq_ 'd["issuer"]')" = "$PUBLIC_URL" ] || die 1 "discovery issuer $(jq_ 'd["issuer"]') != DAEGAM_PUBLIC_URL $PUBLIC_URL"
[ "$(api GET /settings/data-plane-auth)" = 200 ] || die 1 "GET /settings/data-plane-auth failed"
INSTALL_ID="$(jq_ 'd["install_id"]')"
# 서비스 발급자는 svc_token.py 와 같은 규칙이다. ConfigMap 의 DAEGAM_PLATFORM_ISSUER, 없으면 클러스터 안 svc 주소
SVC_ISSUER="$(platform_config DAEGAM_PLATFORM_ISSUER)" || die 1 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
SVC_ISSUER="${SVC_ISSUER:-http://daegam-platform.$PLATFORM_NS.svc:9000}"
"${K[@]}" get mcpoidcconfig -A -l "daegam.devops.ai.kr/platform-id=$INSTALL_ID" -o json >"$WORK/oidc.json" \
  || die 1 "kubectl get mcpoidcconfig -l daegam.devops.ai.kr/platform-id=$INSTALL_ID failed"
OIDC_NOTE="$(python3 - "$WORK/oidc.json" "${SVC_ISSUER%/}" "$PUBLIC_URL" <<'PY'
import json, sys
items, svc, public = json.load(open(sys.argv[1]))["items"], sys.argv[2], sys.argv[3]
jwks = svc + "/.well-known/jwks.json"
want = {"tenant-oidc-config": (public, jwks), "tenant-internal-oidc-config": (svc, jwks + "?use=service")}
seen, bad = {k: 0 for k in want}, []
for o in items:
    kind = (o["metadata"].get("labels") or {}).get("daegam.devops.ai.kr/resource")
    if kind not in want:
        continue
    seen[kind] += 1
    inline = o["spec"].get("inline") or {}
    valid = next((c["status"] for c in (o.get("status") or {}).get("conditions", []) if c["type"] == "Valid"), None)
    if (inline.get("issuer"), inline.get("jwksUrl")) != want[kind] or valid != "True":
        where = o["metadata"]["namespace"] + "/" + o["metadata"]["name"]
        bad.append(f"{where} issuer={inline.get('issuer')} jwksUrl={inline.get('jwksUrl')} Valid={valid}")
if bad or not all(seen.values()):
    sys.exit("; ".join(bad) or f"missing labelled MCPOIDCConfig: {seen}")
print(f"{seen['tenant-oidc-config']} daegam-oidc (jwksUrl {jwks}), {seen['tenant-internal-oidc-config']} daegam-internal-oidc (?use=service)")
PY
)" || die 1 "MCPOIDCConfig of install $INSTALL_ID: see above"
pass 1 "admin signed in; data-plane auth settled; issuer $PUBLIC_URL (auth config, discovery); install $INSTALL_ID: $OIDC_NOTE, all Valid=True"

# ---------- 2. 사용자 둘과 토큰 ----------
U1_ID="$(local_fresh_member "$U1" "$TENANT")" || die 2 "creating $U1 in $TENANT failed"
U2_ID="$(local_fresh_member "$U2" "$OTHER")" || die 2 "creating $U2 in $OTHER failed"
# 첫 로그인이라 서버가 강제하는 비밀번호 바꾸기를 지난다. 발급은 바꾼 비밀번호(LOCAL_PW2)를 다시 받는다
U1_CTL="$(local_login_any "$U1" "$LOCAL_PW")"; [ -n "$U1_CTL" ] || die 2 "login as $U1 failed"
U2_CTL="$(local_login_any "$U2" "$LOCAL_PW")"; [ -n "$U2_CTL" ] || die 2 "login as $U2 failed"
: >"$WORK/ready_at"
issue_ok 2 "$U1_CTL" "$TENANT" "e2e-pat 1"; TOK1="$ISSUED"; TOK1_ID="$ISSUED_ID"
python3 - "$(jwt_json "$TOK1" 0)" "$(jwt_json "$TOK1" 1)" "$PUBLIC_URL" "$MCP_AUDIENCE" "$T_GROUP" <<'PY' || die 2 "token shape is wrong"
import json, sys
head, claims, iss, aud, group = json.loads(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
auds = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
problems = [p for p, ok in [
    (f"alg {head.get('alg')}", head.get("alg") == "ES256"),
    (f"kid {head.get('kid')}", str(head.get("kid", "")).startswith("pat-")),
    (f"iss {claims.get('iss')}", claims.get("iss") == iss),
    (f"aud {auds}", auds == [aud]),
    (f"groups {claims.get('groups')}", claims.get("groups") == [group]),
    ("tv present", "tv" not in claims),
] if not ok]
sys.exit("; ".join(problems)) if problems else None
PY
code="$(issue "$U1_CTL" "$TENANT" "e2e-pat wrong" "$WRONG_PW")"
expect_error "$code" 403 invalid_credentials || die 2 "wrong password -> $code $(cat "$WORK/body") (want 403 invalid_credentials)"
code="$(issue "$U1_CTL" "$OTHER" "e2e-pat other" "$LOCAL_PW2")"
expect_error "$code" 403 not_a_member || die 2 "token of $OTHER for $U1 -> $code $(cat "$WORK/body") (want 403 not_a_member)"
issue_ok 2 "$U1_CTL" "$TENANT" "e2e-pat 2"; TOK2="$ISSUED"
issue_ok 2 "$U2_CTL" "$OTHER" "e2e-pat x"; TOKX="$ISSUED"
TOKX_KID="$(jwt_json "$TOKX" 0 | json_of - 'd["kid"]')"
# STEP 4r 의 developer. 토큰은 지금 받아 STEP 3 의 ready_at 기다림이 함께 덮는다 (P52 3절)
U3_ID="$(local_fresh_member "$U3" "$TENANT")" || die 2 "creating $U3 in $TENANT failed"
local_set_member "$TENANT" "$U3_ID" developer || die 2 "making $U3 a developer of $TENANT failed"
U3_CTL="$(local_login_any "$U3" "$LOCAL_PW")"; [ -n "$U3_CTL" ] || die 2 "login as $U3 failed"
issue_ok 2 "$U3_CTL" "$TENANT" "e2e-pat rules"; TOK5="$ISSUED"; TOK5_ID="$ISSUED_ID"
# STEP 4s 의 서비스 어카운트(P53b 6.7절). 관리자가 자기 비밀번호로 낸다. 토큰은 지금 받아 STEP 3 의 기다림이 덮게 한다
sa_create 2 "$TENANT" "$SA_NAME" viewer
# 설치본에서는 로그인 뒤에 관리자 비밀번호를 지우므로(lib/e2e_local_auth.sh) 여기서 다시 읽는다
if [ "$TARGET" = install ]; then
  SA_PW="$(install_e2e_password "$USER_NAME")" || die 2 "no password for $USER_NAME"
else
  SA_PW="$LOCAL_PW2"
fi
sa_issue 2 "$TENANT" "$SA_ID" "e2e-pat ci" password "$SA_PW"; TOK_SA="$SA_TOKEN"
pass 2 "$U1 got a $TENANT token (ES256, kid pat-, iss $PUBLIC_URL, aud $MCP_AUDIENCE, groups [$T_GROUP]); wrong password 403 invalid_credentials; $OTHER 403 not_a_member; second $TENANT token, $U2's $OTHER token and developer $U3's $TENANT token issued"

# ---------- 3. 대상과 PRM ----------
if [ "$TARGET" = orbstack ]; then
  traefik_ip 3
  # 발급한 슬롯이 두 레플리카의 JWKS 캐시에 오른 뒤 배포한다. 새 파드는 첫 요청에서 그 JWKS 를 받는다
  sleep "$JWKS_CACHE_WAIT"
  TARGETS_MADE=1
  calc_mcp_server 3 "$BP_NAME" "$MS_NAME" "$(exposure "$MS_HOST")"
  calc_publish 3
  calc_gateway 3 "$GW_NAME" "$(exposure "$GW_HOST")"
  NS="$CALC_NS"
  GW_URL="$(jq_ 'd["status"]["external_url"]')"
  api GET "/mcp-servers/$CALC_MS_ID" >/dev/null
  MS_URL="$(jq_ 'd["status"]["external_url"]')"
  GW_PIN="$(pin "$GW_URL" "$LB_IP")"; MS_PIN="$(pin "$MS_URL" "$LB_IP")"
  URLS=("$GW_URL" "$MS_URL"); PINS=("$GW_PIN" "$MS_PIN")
  VMCP_SEL="app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$CALC_GW_K8S_NAME"
  FRONT_SEL="$(front_sel "$CALC_MS_K8S_NAME")"
  if [ "$DOC_REDEPLOYED" = 1 ]; then
    # 이전 실행이 남긴 배포를 이었다. 그 파드는 이 토큰들의 키를 모르는 JWKS 를 들고 있을 수 있으므로 새로 띄운다
    restart_pods "$VMCP_SEL" "$FRONT_SEL" || die 3 "restarting the vMCP and front pods left from an earlier run failed"
  fi
  TARGET_NOTE="deployed $MS_URL (vMCP front front-$CALC_MS_K8S_NAME) and $GW_URL (vMCP $CALC_GW_K8S_NAME) after the ${JWKS_CACHE_WAIT}s JWKS cache"
else
  # 노출해 둔 Gateway 의 host 는 E2E_PAT_GW_HOST 이고 없으면 EXPOSURE_DOMAIN 에서 나온다. 둘 다 없으면 조건부로, host 가
  # 있는데 그 Gateway 가 없으면 못 돈 것으로 건너뛴다. 허용·거부·회수와 Tool 규칙은 모두 그 Gateway 를 부른다
  install_gateway 3 E2E_PAT_GW_HOST "$(install_gateway_host E2E_PAT_GW_HOST)" || GW_RC=$?
  if [ "$GW_RC" != 0 ]; then
    skip_without_gateway "$GW_RC" 3 4 4s 4r 5 6 7
    cond_skip 8 "the internal hop is checked on orbstack only"
    cond_skip 9 "the slow-deploy delete scales the ToolHive operator and runs on orbstack only"
    finish
  fi
  URLS=("$GW_URL"); PINS=("$GW_PIN")
  # 파드를 굴릴 때만 그 Gateway 의 vMCP 를 찾는다(STEP 3·6 은 E2E_POD_RESTART, STEP 5 는 E2E_PAT_WAIT 가 정한다)
  if [ "$POD_RESTART" = true ] || [ "$WAIT_MODE" = restart ]; then gateway_vmcp 3 "$GW_ID"; fi
  if [ "$POD_RESTART" = true ]; then
    # 새 파드는 첫 요청에서 JWKS 를 받으므로 ready_at 을 기다리지 않는다. 슬롯이 레플리카의 JWKS 캐시에 오른 뒤 굴린다
    sleep "$JWKS_CACHE_WAIT"
    restart_pods "$VMCP_SEL" || die 3 "restarting the vMCP pods of Gateway $GW_ID failed"
    TARGET_NOTE="read Gateway $GW_ID at $GW_URL; restarted its vMCP pods after the ${JWKS_CACHE_WAIT}s JWKS cache"
  else
    waited="$(ready_wait)"
    if [ "$waited" -gt 0 ]; then
      echo ">> waiting ${waited}s for ready_at + ${READY_SLACK}s (the keys must be published before ToolHive refetches JWKS)"
      sleep "$waited"
    fi
    TARGET_NOTE="read Gateway $GW_ID at $GW_URL; waited ${waited}s for ready_at"
  fi
fi
PRM_URL="$(python3 -c 'import sys, urllib.parse as u
p = u.urlsplit(sys.argv[1]); print(f"{p.scheme}://{p.netloc}/.well-known/oauth-protected-resource{p.path}")' "$GW_URL")"
code=""
for ((i = 0; i < 24; i++)); do  # Traefik 이 라우트를 집어 들 때까지
  code="$(curl -sSk -o "$WORK/body" -w '%{http_code}' --resolve "$GW_PIN" --max-time 10 "$PRM_URL" || echo 000)"
  [ "$code" = 200 ] && break; sleep 5
done
[ "$code" = 200 ] || die 3 "GET $PRM_URL -> $code"
[ "$(jq_ "d['authorization_servers'] == ['$PUBLIC_URL'] and d['resource'] == '$GW_URL'")" = True ] \
  || die 3 "PRM $PRM_URL: $(cat "$WORK/body") (want authorization_servers [$PUBLIC_URL], resource $GW_URL)"
pass 3 "$TARGET_NOTE; PRM authorization_servers [$PUBLIC_URL], resource $GW_URL"

# ---------- 4. 허용·거부와 그룹 PATCH ----------
notes=""
for i in "${!URLS[@]}"; do
  allowed "${URLS[$i]}" "${PINS[$i]}" "$TOK1" || die 4 "$U1's $TENANT token at ${URLS[$i]}: $MCP_NOTE"
  denied "${URLS[$i]}" "${PINS[$i]}" "$TOKX" || die 4 "$U2's $OTHER token at ${URLS[$i]} was not denied: $MCP_NOTE"
  notes="$notes ${URLS[$i]}: $MCP_TOOL allowed for T, empty list + 403 for X;"
done
local_set_member "$OTHER" "$U2_ID" tenant-admin || die 4 "making $U2 tenant-admin of $OTHER failed"
code="$(TENANT="$OTHER" api_as "$U2_CTL" PATCH "/tenants/$OTHER/settings" "{\"mcp_group\": \"$T_GROUP\"}")"
expect_error "$code" 422 invalid_value || die 4 "PATCH /tenants/$OTHER/settings mcp_group by its tenant-admin -> $code $(cat "$WORK/body") (want 422 invalid_value)"
pass 4 "$notes $OTHER tenant-admin cannot take $T_GROUP (422 invalid_value)"

# ---------- 4s. 서비스 어카운트 (P53b 6.7절) ----------
sa_claims_ok 4s "$TOK_SA" "$SA_ID" "$T_GROUP"
for i in "${!URLS[@]}"; do
  allowed "${URLS[$i]}" "${PINS[$i]}" "$TOK_SA" || die 4s "the service-account token at ${URLS[$i]}: $MCP_NOTE"
done
api_delete "/tenants/$TENANT/service-accounts/$SA_ID"
[ "$(api GET "/tenants/$TENANT/service-accounts")" = 200 ] \
  && [ "$(jq_ "[a for a in d['accounts'] if a['account_id'] == '$SA_ID']")" = "[]" ] || die 4s "$SA_NAME is still listed after its delete"
SA_ID=""
pass 4s "service account $SA_NAME (viewer, issued with the admin's password): sub sa:<id>, groups [$T_GROUP], $MCP_TOOL allowed at every target; the account is gone after its delete"

# ---------- 4r. Tool 규칙의 역할 (P52 2·3절) ----------
# 사람의 토큰은 그 테넌트의 역할을 daegam_roles 에 싣고, vMCP 는 그것을 role_claim_name 으로 읽어 역할 절을 판정한다. 규칙은
# Gateway 자신의 것이라 그 Gateway 를 거친 호출에만 적용된다. 노출하지 않은 Gateway 라 vMCP 에 바로 붙는다
roles_of() { jwt_json "$1" 1 | json_of - 'json.dumps(d.get("daegam_roles"))'; } # TOKEN
[ "$(roles_of "$TOK5")" = "[\"daegam-role:$TENANT:developer\"]" ] || die 4r "$U3's token carries daegam_roles $(roles_of "$TOK5")"
[ "$(roles_of "$TOK2")" = "[\"daegam-role:$TENANT:viewer\"]" ] || die 4r "$U1's token carries daegam_roles $(roles_of "$TOK2")"
if [ "$TARGET" = orbstack ]; then RULES_SRC="$CALC_GW_ID"; else RULES_SRC="$GW_ID"; fi
[ "$(api GET "/gateways/$RULES_SRC")" = 200 ] || die 4r "GET /gateways/$RULES_SRC failed"
RULES_CLUSTER="$(jq_ 'd["cluster_id"]')"
RULES_ITEM="$(jq_ "next((b['hub_item_id'] for b in d['deployed_backends'] if '$TOOL' in (b.get('tools') or b.get('tool_names') or [])), '')")"
[ -n "$RULES_ITEM" ] || die 4r "no backend of Gateway $RULES_SRC offers $TOOL: $(jq_ 'd["deployed_backends"]')"
RULES_BODY="$(python3 -c 'import json, sys
name, item, tool = sys.argv[1:4]
rule = {"callers": {"roles": ["developer"]}}
print(json.dumps({"name": name, "backends": [{"hub_item_id": item, "tools": [tool], "tool_rules": {tool: rule}}]}))' \
  "$RULES_GW_NAME" "$RULES_ITEM" "$TOOL")"
create_fresh 4r /gateways "$RULES_GW_NAME" "$RULES_BODY"
RULES_GW_ID="$CREATED_ID"
deploy_ready 4r /gateways "$RULES_GW_ID" "{\"cluster_id\": \"$RULES_CLUSTER\"}"
RULES_K8S="$(jq_ 'd["k8s"]["name"]')"; RULES_NS="$(jq_ 'd["k8s"]["namespace"]')"
start_pf "$RULES_NS" "svc/vmcp-$RULES_K8S" "${VMCP_PORT}:4483"
PF_VMCP=$PF_PID
wait_port "$VMCP_PORT" || die 4r "port-forward to svc/vmcp-$RULES_K8S did not open :$VMCP_PORT"
RULES_URL="http://127.0.0.1:${VMCP_PORT}/mcp"; RULES_PIN="127.0.0.1:${VMCP_PORT}:127.0.0.1"
allowed "$RULES_URL" "$RULES_PIN" "$TOK5" || die 4r "developer $U3's token through the role rule: $MCP_NOTE"
denied "$RULES_URL" "$RULES_PIN" "$TOK2" || die 4r "viewer $U1's token was not refused by the role rule: $MCP_NOTE"
# 역할을 낮추면 그 사람의 그 테넌트 개인 토큰이 모두 회수된다. 토큰에 실린 역할은 낼 때의 것이기 때문이다 (3절)
local_set_member "$TENANT" "$U3_ID" viewer || die 4r "lowering $U3 to viewer failed"
wait_revoked 4r "$U3_ID" "$TOK5_ID" role_changed
stop_pf "$PF_VMCP" "$VMCP_PORT"; PF_VMCP=""
api_delete "/gateways/$RULES_GW_ID"
wait_gone "/gateways/$RULES_GW_ID" || die 4r "Gateway $RULES_GW_NAME is still there after its delete"
RULES_GW_ID=""
pass 4r "daegam_roles carries developer for $U3 and viewer for $U1; Gateway $RULES_GW_NAME (item $RULES_ITEM, $TOOL for developer and up): $U3 -> $MCP_TOOL allowed, $U1 -> empty list and 403; lowering $U3 to viewer revoked its token (role_changed); Gateway removed"

# ---------- 5. 회수 ----------
code="$(api_as "$U1_CTL" DELETE "/me/tokens/$TOK1_ID")"
[ "$code" = 200 ] && [ "$(jq_ 'd["revoked"]')" = True ] || die 5 "DELETE /me/tokens/$TOK1_ID -> $code"
REVOKED_AT=$(date +%s)   # 한도는 회수 요청이 끝난 때부터 잰다
wait_revoked 5 "$U1_ID" "$TOK1_ID" user
if [ "$WAIT_MODE" = restart ]; then
  sleep "$JWKS_CACHE_WAIT"
  # 설치본은 Gateway 하나라 앞단이 없다. 빈 라벨은 네임스페이스의 파드를 모두 고르므로 넘기지 않는다
  restart_pods "$VMCP_SEL" ${FRONT_SEL:+"$FRONT_SEL"} || die 5 "restarting the vMCP and front pods failed"
  for i in "${!URLS[@]}"; do
    unauthenticated "${URLS[$i]}" "${PINS[$i]}" "$TOK1" || die 5 "revoked token is still accepted by the new pod at ${URLS[$i]}"
    allowed "${URLS[$i]}" "${PINS[$i]}" "$TOK2" || die 5 "the unrevoked second token at ${URLS[$i]}: $MCP_NOTE"
  done
  pass 5 "revoked (status revoked user); after ${JWKS_CACHE_WAIT}s and new vMCP/front pods the revoked token is 401 and the second token is allowed"
else
  wait_unauthenticated 5 "$REVOKED_AT" "$TOK1"
  for i in "${!URLS[@]}"; do
    allowed "${URLS[$i]}" "${PINS[$i]}" "$TOK2" || die 5 "the unrevoked second token at ${URLS[$i]}: $MCP_NOTE"
  done
  echo "  revoke-to-401: ${REFUSED_AFTER}s (limit ${REVOKE_LIMIT}s)"
  pass 5 "revoked (status revoked user); the revoked token was refused after ${REFUSED_AFTER}s (<= ${REVOKE_LIMIT}s), the second token is allowed"
fi

# ---------- 6. 소속 해제 ----------
issue_ok 6 "$U1_CTL" "$TENANT" "e2e-pat 3"; TOK3="$ISSUED"; TOK3_ID="$ISSUED_ID"
local_set_member "$TENANT" "$U1_ID" || die 6 "removing $U1 from $TENANT failed"
wait_revoked 6 "$U1_ID" "$TOK3_ID" membership
if [ "$POD_RESTART" = true ]; then
  sleep "$JWKS_CACHE_WAIT"
  restart_pods "$VMCP_SEL" || die 6 "restarting the vMCP pod failed"
  unauthenticated "$GW_URL" "$GW_PIN" "$TOK3" || die 6 "the token of a removed member is still accepted by the new vMCP pod"
  pass 6 "removing $U1 from $TENANT revoked its token (membership); the new vMCP pod answers 401"
else
  pass 6 "removing $U1 from $TENANT revoked its token (membership)"
fi

# ---------- 7. 계정 막기와 삭제 ----------
local_set_member "$TENANT" "$U1_ID" viewer || die 7 "adding $U1 back to $TENANT failed"
issue_ok 7 "$U1_CTL" "$TENANT" "e2e-pat 4"; TOK4_ID="$ISSUED_ID"
[ "$(api PATCH "/users/$U1_ID" '{"disabled": true}')" = 200 ] || die 7 "PATCH /users/$U1_ID disabled -> $(cat "$WORK/body")"
wait_revoked 7 "$U1_ID" "$TOK4_ID" disabled
jwks_has "$TOKX_KID" || die 7 "the key $TOKX_KID of $U2's live token is not in JWKS"
[ "$(api DELETE "/users/$U2_ID")" = 200 ] || die 7 "DELETE /users/$U2_ID -> $(cat "$WORK/body")"
U2_DELETED=1
code="$(api GET "/users/$U2_ID/tokens")"
[ "$code" = 404 ] || die 7 "GET /users/$U2_ID/tokens after the delete -> $code (want 404)"
gone=""
for ((i = 0; i < JWKS_DROP_LIMIT / 10; i++)); do
  jwks_has "$TOKX_KID" || { gone=1; break; }
  sleep 10
done
[ -n "$gone" ] || die 7 "the key $TOKX_KID of the deleted $U2 is still in JWKS after ${JWKS_DROP_LIMIT}s"
pass 7 "disabling $U1 revoked its token (disabled); deleting $U2 made its list 404 and took $TOKX_KID out of JWKS"

# ---------- 8. 내부 홉 (orbstack 만) ----------
if [ "$TARGET" = orbstack ]; then
  internal_hop 8 "$CALC_MS_K8S_NAME" "$PROXY_PORT" "$CALC_MS_ID"
  PLATFORM_KID="$(jwt_json "$ADMIN_TOKEN" 0 | json_of - 'd["kid"]')"
  curl -fsS "$BASE/.well-known/jwks.json?use=service" -o "$WORK/body" || die 8 "GET /.well-known/jwks.json?use=service failed"
  [ "$(jq_ "[k['kid'] for k in d['keys']] == ['$PLATFORM_KID']")" = True ] || die 8 "?use=service keys: $(cat "$WORK/body") (want $PLATFORM_KID alone)"
  pass 8 "internal proxy mcp-$CALC_MS_K8S_NAME-proxy: no token 401, service token 200; ?use=service serves the platform key alone"
else
  cond_skip 8 "the internal hop is checked on orbstack only"
fi

# ---------- 9. 느린 배포 도중의 삭제 (orbstack 만, P66) ----------
if [ "$TARGET" = orbstack ]; then
  create_fresh 9 /mcp-servers "$SLOW_NAME" "$(calc_ms_body "$SLOW_NAME")"
  SLOW_ID="$CREATED_ID"
  OP_REPLICAS="$OP_UP"
  "${K[@]}" -n "$OP_NS" scale "deploy/$OP_DEPLOY" --replicas=0 >/dev/null || die 9 "scaling deploy/$OP_DEPLOY to 0 failed"
  echo ">> deploy/$OP_DEPLOY scaled to 0; if this run dies before its cleanup: kubectl --context $CTX -n $OP_NS scale deploy/$OP_DEPLOY --replicas=$OP_UP"
  left="(not read yet)"
  for ((i = 0; i < 24; i++)); do
    left="$(operator_pods)" || left="(kubectl failed)"
    [ -z "$left" ] && break
    sleep 5
  done
  [ -z "$left" ] || die 9 "the $OP_DEPLOY pods did not stop: $left"

  # 배포 요청은 provision 이 끝날 때까지 돌아오지 않으므로 뒤에서 부르고 본문은 따로 둔다
  (API_OUT="$WORK/slow-deploy" api POST "/mcp-servers/$SLOW_ID/deploy" "$(exposure "$SLOW_HOST")" >"$WORK/slow-deploy-code") &
  SLOW_PID=$!
  authz=""
  deadline=$((SECONDS + SLOW_AUTHZ_SEEN))
  while [ "$SECONDS" -lt "$deadline" ]; do
    authz="$("${K[@]}" get mcpauthzconfigs -A -l "daegam.devops.ai.kr/id=$SLOW_ID" -o name 2>/dev/null || true)"
    [ -n "$authz" ] && break
    kill -0 "$SLOW_PID" 2>/dev/null || break   # 배포가 이미 답했다
    sleep 0.5
  done
  [ -n "$authz" ] || die 9 "no MCPAuthzConfig of $SLOW_NAME within ${SLOW_AUTHZ_SEEN}s, so the deploy never waited for one (does the cluster pass the authz_ref gate?); deploy answered $(cat "$WORK/slow-deploy-code" 2>/dev/null)" \
    "$(cat "$WORK/slow-deploy" 2>/dev/null)"
  [ "$(api GET "/mcp-servers/$SLOW_ID")" = 200 ] && [ "$(jq_ 'd["state"]')" = deploying ] \
    || die 9 "$SLOW_NAME is not deploying while it waits for $authz"
  SLOW_K8S="$(jq_ 'd["k8s"]["name"]')"
  SLOW_NS="$(jq_ 'd["k8s"]["namespace"]')"
  before="$(slow_tokens)" || die 9 "GET /backend-apis/$CALC_BP_ID/tokens failed"
  [ "$before" -ge 1 ] || die 9 "$SLOW_NAME has no active spec token while it deploys"

  code="$(api DELETE "/mcp-servers/$SLOW_ID")"
  [ "$code" = 200 ] || die 9 "DELETE /mcp-servers/$SLOW_ID while its deploy waits -> $code"
  deleted="$(jq_ 'd["deleted"]')"
  restore_operator || die 9 "deploy/$OP_DEPLOY in $OP_NS did not come back"
  wait "$SLOW_PID" 2>/dev/null || true
  SLOW_PID=""
  # 배포의 실행은 기다림 안의 체크포인트(클러스터 호출마다, 10초가 지났으면)에서 지워지는 문서를 보고 멈춘다
  deploy_code="$(cat "$WORK/slow-deploy-code" 2>/dev/null || true)"
  [ "$deploy_code" = 409 ] && [ "$(json_of "$WORK/slow-deploy" "(d.get('detail') or {}).get('code')" || true)" = state_conflict ] \
    || die 9 "the deploy answered ${deploy_code:-nothing}, want 409 state_conflict" "$(cat "$WORK/slow-deploy" 2>/dev/null)"
  wait_gone "/mcp-servers/$SLOW_ID" "$SLOW_GONE" || die 9 "$SLOW_NAME is still there ${SLOW_GONE}s after the delete (deleted=$deleted)"
  left="(not read yet)"
  for ((i = 0; i < SLOW_OBJECTS_GONE / 5; i++)); do
    left="$(slow_objects)" || left="(kubectl failed)"
    [ -z "$left" ] && break
    sleep 5
  done
  [ -z "$left" ] || die 9 "objects of $SLOW_NAME left after the delete: $(echo "$left" | tr '\n' ' ')"
  after="$(slow_tokens)" || die 9 "GET /backend-apis/$CALC_BP_ID/tokens failed"
  [ "$after" = 0 ] || die 9 "$after spec tokens of $SLOW_NAME are still active after the delete"

  # 지운 문서가 공개 host 의 선점도 놓았다(P70). 같은 host 로 새 문서를 배포하면 409 host_conflict 가 아니다
  create_fresh 9 /mcp-servers "$SLOW_NAME" "$(calc_ms_body "$SLOW_NAME")"
  SLOW_ID="$CREATED_ID"
  code="$(api POST "/mcp-servers/$SLOW_ID/deploy" "$(exposure "$SLOW_HOST")")"
  [ "$code" = 201 ] || die 9 "deploying a fresh $SLOW_NAME at $SLOW_HOST after the delete -> $code (the host claim should be released)"
  api_delete "/mcp-servers/$SLOW_ID"
  wait_gone "/mcp-servers/$SLOW_ID" "$SLOW_GONE" || die 9 "the fresh $SLOW_NAME is still there ${SLOW_GONE}s after its delete"
  SLOW_ID=""
  pass 9 "with $OP_DEPLOY at 0 the exposed deploy of $SLOW_NAME waited on $authz; DELETE answered deleted=$deleted and the deploy 409 state_conflict; with the operator back the document is gone, nothing of it is left, its $before spec token(s) are revoked, and a fresh deploy at $SLOW_HOST is 201"
else
  cond_skip 9 "the slow-deploy delete scales the ToolHive operator and runs on orbstack only"
fi

finish
