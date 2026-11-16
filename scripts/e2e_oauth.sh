#!/usr/bin/env bash
# shellcheck disable=SC2015  # "A && B || die" 는 A 나 B 가 실패하면 멈춘다는 뜻으로 쓴다
# P33 (D59) MCP 클라이언트 OAuth e2e. local 모드의 플랫폼이 MCP 클라이언트의 인가 서버로서 401 에서 시작하는 표준 흐름을
# 지키고, 그 접근 토큰이 노출 Gateway 를 열며, 갱신 회전·재사용·회수·계정 사건이 연결을 끊는지 본다. 흐름은 스크립트
# 클라이언트 deploy/dev/lab/mcp_oauth_client.py 가 돌고 여기서는 판정한다. E2E_TARGET 이 orbstack(기본)이나 install 이다.
#
#   make e2e-oauth                                        orbstack(http, DCR). STEP 0 이 local 모드와 평문 허용 플래그를
#                                                         넣고 EXIT trap 이 성공·실패·중단 모두 되돌린다
#   make e2e-oauth E2E_TARGET=install E2E_INSTALL_USER=<관리자>   설치본(https, DCR 과 CIMD). 모드는 이미 local 이고
#                                                         노출해 둔 Gateway 를 읽기만 한다
#   make e2e-oauth ... E2E_OAUTH_WAIT=full                STEP 13 이 끊은 연결의 접근 토큰이 Gateway 에서 401 이 될
#                                                         때까지 30초마다 재고 16분 안이면 통과다. 기본(refresh)은
#                                                         갱신 거절과 연결 상태만 본다
#
# 전제는 e2e-pat 과 같다. orbstack 은 make deploy-local·lab-up(mock-soap)과 Traefik(LoadBalancer Service traefik/traefik)
# 이다. 플랫폼 호스트(DAEGAM_PUBLIC_URL)와 노출 host 는 스크립트 클라이언트의 --resolve 와 curl --resolve 로 Traefik IP 에
# 못 박으므로 /etc/hosts 가 필요 없다. 설치본은 운영자가 노출해 둔 Gateway(external_url 이
# https://mcp-gateway.<EXPOSURE_DOMAIN>/mcp 이거나 E2E_OAUTH_GW_HOST 의 것, 테넌트 E2E_OAUTH_TENANT 기본 default)다. 그 host 를
# 정하지 않았으면(EXPOSURE_DOMAIN 도 E2E_OAUTH_GW_HOST 도 없다) STEP 1 부터 조건부로 건너뛰고, 정했는데 그 Gateway 가 없으면
# 못 돈 것(SKIP, INCOMPLETE)으로 건너뛴다. STEP 16 은 개인 토큰의 새 키를 ready_at 까지 기다리고, 설치 값 파일의
# E2E_POD_RESTART=true 면 그 대신 그 Gateway 의 vMCP 파드를 다시 띄워 알린다. 관리자 자격 증명은 e2e-install 과 같은 E2E_INSTALL_PASSWORD 나 키체인 항목 daegam-e2e-install 이다. 값은 설치 값 파일(~/.config/daegam/install.env)이
# 기본이고 환경이 이긴다.
#
# 두 갈래 모두 플랫폼 관리자로 버리는 사용자 둘을 만든다. u1 은 대상 테넌트 T 의 멤버, u2 는 다른 테넌트 X 의 멤버다. 새
# 사용자는 첫 로그인에서 강제 비밀번호 변경을 지나고, install 갈래는 두 사용자의 비밀번호를 실행마다 새로 만든다(e2e-pat 과
# 같다). 허용은 최근 10분 안의 로그인을 요구하므로 스크립트 클라이언트가 동의 화면의 호출마다 그 자리에서 로그인한다.
# 연결 하나가 상태 파일 하나($WORK/<이름>.json)이고, 발견과 등록을 마친 base 를 복사해 새 연결을 연다. Gateway 호출의
# 허용 판정은 e2e-pat 과 같다(lib/e2e_mcp_access.sh). 접근 토큰은 플랫폼 RSA 키로 서명하므로 워밍업이 없다.
#
# Gate:
#   STEP 0  (orbstack) switch_auth_mode local DAEGAM_OAUTH_ALLOW_HTTP=true. 두 갈래 모두 관리자 로그인과 재적용,
#           /auth/config 의 issuer 와 oauth.enabled, 사용자 둘, STEP 16 이 쓸 u1 의 개인 토큰, 대상(orbstack 은
#           calculator MCP Server[Add] 와 그것을 쓰는 Gateway 를 oauth-gw.e2e.local 로 노출 배포, 설치본은 노출해 둔
#           Gateway 를 읽기만 한다)
#   STEP 1  토큰 없는 Gateway 호출이 401 이고 WWW-Authenticate 에 resource_metadata 와 scope="mcp" 가 있다. Gateway 가
#           다시 렌더되기까지 30초마다 최대 10분 본다
#   STEP 2  PRM 의 resource 가 외부 주소, authorization_servers 가 [DAEGAM_PUBLIC_URL], scopes_supported 가 ["mcp"] 다
#   STEP 3  인가 서버 메타데이터의 issuer 가 PRM 의 값과 글자 그대로 같고 S256·none·CIMD·iss 를 광고한다
#   STEP 4  Cursor 모양의 DCR(세 주소)이 loopback 하나만 남기고 같은 등록이 같은 client_id 를 받는다. 한 글자 바꾼
#           client_id 의 인가는 동의 화면의 invalid_client 다
#   STEP 5  resource 없는 인가와 모르는 resource 가 redirect_uri 로 error=invalid_target 과 iss 를 받는다
#   STEP 6  PKCE 인가, u1 의 inspect, 새로 로그인한 토큰의 허용이 redirect_to 에 code·state·iss 를 싣는다. 같은 핸들의
#           둘째 허용은 409 request_used 다
#   STEP 7  코드 교환(no-store, 갱신 토큰), 접근 토큰 클레임, Gateway 의 tools/list 와 tools/call(orbstack 은 Add(3,4)=7)
#   STEP 8  틀린 code_verifier 가 invalid_grant 이고 코드를 태운다. 쓴 코드의 재사용이 invalid_grant 이고 그 연결이
#           code_reuse 로 끊긴다
#   STEP 9  갱신 회전(가족 그대로, 비밀 바뀜), 다른 client_id 의 갱신은 invalid_grant 이고 연결은 산다. 10초 뒤 옛 갱신
#           토큰의 재사용이 가족을 refresh_reuse 로 끊는다
#   STEP 10 토큰 요청의 resource 를 발급자 주소로 바꿔도 aud 가 원래 리소스이고 그 토큰이 Gateway 에서 허용이다
#   STEP 11 u2 의 inspect 에는 공개 주소만 있고(테넌트·이름 없음) u2 의 허용은 403 not_a_member 다
#   STEP 12 등록한 http://127.0.0.1/callback 에 localhost:<다른 포트> 가 통하고, 경로가 다르면 동의 화면의
#           invalid_redirect_uri 다
#   STEP 13 RFC 7009 회수 뒤 갱신이 invalid_grant 이고 연결이 client 로 끊긴다. E2E_OAUTH_WAIT=full 이면 그 접근 토큰이
#           Gateway 에서 401 이 되기까지 잰다
#   STEP 15 (설치본만) Claude Code·VS Code 의 CIMD client_id 로 인가가 동의 화면에 닿고(플랫폼이 실제 문서를 받는다)
#           inspect 의 문서 호스트가 claude.ai·vscode.dev 다
#   STEP 16 STEP 0 의 개인 토큰으로 대상 도구의 tools/call 이 그대로 된다. orbstack 은 D41 내부 홉(무토큰 401, 서비스
#           토큰 200)도 본다. 설치본에서 make e2e-install-extra 의 이번 실행이 이 게이트 앞에서 e2e-pat 을 같은 노출 Gateway
#           에 대고 돌았으면(E2E_EXTRA_RUN) STEP 16 과 그 개인 토큰을 조건부로 건너뛴다. e2e-pat STEP 2~4 가 그 Gateway 에서
#           개인 토큰의 호출을 본다
#   STEP 14 u1 을 막으면 갱신이 바로 invalid_grant 이고 연결이 disabled 로 끊긴다. 맨 끝에 돈다. u1 을 막으면 그 사람의
#           로그인과 개인 토큰도 막혀 15·16 이 먼저여야 하기 때문이다
#   정리    두 사용자를 지우고 orbstack 의 대상을 지운 뒤 두 ConfigMap 키를 되돌려 재적용을 기다린다
#
# 출력과 종료 코드는 lib/e2e_report.sh 의 계약이다.
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
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_OAUTH_TENANT:-e2e-oauth}"
    TOOL="${E2E_OAUTH_TOOL:-Add}"; TOOL_ARGS='{"a": 3, "b": 4}'; TOOL_EXPECT=7; TOKEN_DAYS=7 ;;
  install)
    install_env_defaults
    # 운영자의 Gateway 는 E2E_POD_RESTART=true 일 때만 굴린다. 개인 토큰은 서버가 받는 가장 짧은 하루다
    POD_RESTART="$(install_flag E2E_POD_RESTART false)" || exit 1
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_OAUTH_TENANT:-default}"
    TOOL="${E2E_OAUTH_TOOL:-getPublicHolidays}"; TOOL_ARGS=""; TOOL_EXPECT=""; TOKEN_DAYS=1 ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
WAIT_MODE="${E2E_OAUTH_WAIT:-refresh}"
case "$WAIT_MODE" in refresh | full) ;; *) echo "E2E_OAUTH_WAIT 는 refresh 나 full 이다 (got $WAIT_MODE)"; exit 1 ;; esac
OTHER="${E2E_OAUTH_OTHER_TENANT:-e2e-oauth-x}"
T_GROUP="daegam-$TENANT"   # local 모드에서 T 의 MCP 그룹은 이 이름으로 고정이다 (P32 2절)
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
API_PORT="${E2E_API_PORT:-$PORT_OAUTH_API}"
PROXY_PORT="${E2E_PROXY_PORT:-$PORT_OAUTH_PROXY}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
U1="oauth-u1-e2e"; U2="oauth-u2-e2e"
BP_NAME="oauth-calc"; MS_NAME="oauth-ms"; GW_NAME="oauth-gw"
GW_HOST="${E2E_OAUTH_GW_HOST:-oauth-gw.e2e.local}"

# 클라이언트의 모양 (P33 클라이언트별 기대 동작). 스크립트 클라이언트는 포트 없는 loopback 을 등록하고 임의 포트로 온다.
# Cursor 는 DCR 로 세 주소를 보내고, Claude Code·VS Code 는 CIMD 다
REDIRECT_HOST="127.0.0.1"; REDIRECT_PATH="/callback"
REDIRECT_URI="http://${REDIRECT_HOST}${REDIRECT_PATH}"
OTHER_PORT=53112   # STEP 12. 등록한 127.0.0.1 에 localhost 와 이 포트로 온다
CURSOR_REDIRECT_URIS=(http://localhost:8787/callback cursor://anysphere.cursor-retrieval/oauth/callback
  https://www.cursor.com/oauth/callback)
CIMD_CLIENTS=(https://claude.ai/oauth/claude-code-client-metadata https://vscode.dev/oauth/client-metadata.json)
WRONG_VERIFIER="e2e-oauth-wrong-code-verifier-0123456789abcdef"   # 꼴은 맞고(43자 이상) 값은 틀린 PKCE verifier

# 한도와 기다림 (P33 11절·14절). 설정이 아니라 한도 계산의 재료라 여기에 이름 붙여 둔다. 회수 한도는
# lib/e2e_mcp_access.sh, 개인 토큰의 기다림은 lib/e2e_personal_tokens.sh 에 있다
DISCOVER_POLL=30       # Gateway 가 scope="mcp" 를 광고하기까지 다시 보는 간격
DISCOVER_LIMIT=600     # 그 상한 10분. 재적용은 소비자를 한 틱에 열 개씩 굴린다
REUSE_WAIT=11          # 갱신 토큰 재사용 유예 10초가 지나기를 기다리는 초

WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-oauth.XXXXXX")"
K=("${KUBECTL:-kubectl}" --context "$CTX")
AUTH=()                 # 관리자 Bearer 헤더 (lib/e2e_api.sh 의 api 와 lib/e2e_calc.sh 가 싣는다)
ADMIN_TOKEN=""; PF_API=""; PF_PROXY=""
U1_ID=""; U2_ID=""; TARGETS_MADE=0
NS=""; GW_URL=""; PUBLIC_URL=""; PAT=""
RESOLVE=()              # 스크립트 클라이언트의 --resolve. orbstack 은 플랫폼 호스트와 노출 host 를 Traefik IP 로, 설치본은 DNS 다
URLS=(); PINS=()        # wait_unauthenticated 가 보는 외부 주소와 그 --resolve 값. Gateway 하나다

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$PROXY_PORT"
# roll_control_plane·platform_config, switch_auth_mode·restore_auth_mode·wait_auth_reapplied
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_auth_mode.sh"
# local_login_any·local_fresh_member·install_admin·local_admin_session 과 비밀번호 셋
. "$HERE/lib/e2e_local_auth.sh"
[ "$TARGET" != install ] || install_run_passwords
# pass·skip·cond_skip·die·finish 와 종료 코드 계약
. "$HERE/lib/e2e_report.sh"
# api·api_as·jq_·api_delete (e2e_pat 과 함께 쓴다)
. "$HERE/lib/e2e_api.sh"
# issue_ok·ready_wait 와 JWKS 기다림 (e2e_pat 과 함께 쓴다)
. "$HERE/lib/e2e_personal_tokens.sh"
# calc_mcp_server·calc_publish·calc_gateway·calc_teardown (e2e_mcp_auth·e2e_pat 과 함께 쓴다)
. "$HERE/lib/e2e_calc.sh"
# INIT_PARAMS·rpc_curl·mcp_status·mcp_ingress (WORK 를 쓴다)
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-oauth"
. "$HERE/lib/mcp_rpc.sh"
# traefik_ip·pin·url_host·exposure·public_gateway·allowed·wait_unauthenticated·restart_pods·internal_hop 과 TOOL_FILL
# (e2e_pat 과 함께 쓴다)
. "$HERE/lib/e2e_mcp_access.sh"

# 설치본에서 make e2e-install-extra 의 이번 실행(scripts/run_e2e_install_extra.sh 가 E2E_EXTRA_RUN 에 그 게이트를 넘긴다)이 이
# 게이트 앞에서 e2e-pat 을 같은 노출 Gateway 에 대고 돌았으면 STEP 16 과 그것이 쓸 STEP 0 의 개인 토큰을 건너뛴다. e2e-pat
# STEP 2~4 가 그 Gateway 에서 개인 토큰의 tools/call 을 보므로 그 토큰의 ready_at 을 다시 기다리지 않는다. 이 게이트만 따로
# 돌리면(E2E_TAG 가 있어도) STEP 16 은 돈다
PAT_RAN=0
OAUTH_GW_HOST=""
if [ "$TARGET" = install ]; then
  OAUTH_GW_HOST="$(install_gateway_host E2E_OAUTH_GW_HOST)"
  case " ${E2E_EXTRA_RUN:-} " in
    *" e2e-pat "*)
      [ -z "$OAUTH_GW_HOST" ] || [ "$(install_gateway_host E2E_PAT_GW_HOST)" != "$OAUTH_GW_HOST" ] || PAT_RAN=1 ;;
  esac
fi

# 스크립트 클라이언트 call 의 인자. orbstack 은 TOOL_ARGS 그대로, 설치본은 bash 판정(tool_args)과 같은 TOOL_FILL 규칙이다
CALL_ARGS=()
if [ -n "$TOOL_ARGS" ]; then
  CALL_ARGS=(--args "$TOOL_ARGS")
else
  for rule in "${TOOL_FILL[@]}"; do CALL_ARGS+=(--fill "$rule"); done
fi
OAUTH_CLIENT=(uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/mcp_oauth_client.py")

# 만든 것을 걷는다. 관리자 토큰이 없으면(로그인 전에 끝났으면) 걷을 것도 없다. 사람을 지우면 그 연결과 개인 토큰도
# 끊긴다
teardown() {
  [ ${#AUTH[@]} -gt 0 ] || return 0
  [ -z "$U1_ID" ] || api_delete "/users/$U1_ID"
  [ -z "$U2_ID" ] || api_delete "/users/$U2_ID"
  if [ "$TARGETS_MADE" = 1 ]; then calc_teardown; fi
}

cleanup() {
  teardown
  # 되돌린 뒤 재적용을 기다리는 호출도 API port-forward 로 가므로 그것을 닫기 전에 되돌린다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_API" "$API_PORT"
  stop_pf "$PF_PROXY" "$PROXY_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# oauth CONN COMMAND [ARGS...] -> 스크립트 클라이언트를 연결 CONN 의 상태 파일로 부른다. 요약 JSON 은 $WORK/body 에
# 두고(jq_ 가 읽고 die 가 찍는다. 비밀은 싣지 않는다) 클라이언트의 종료 코드를 돌려준다
oauth() {
  local conn="$1"
  shift
  "${OAUTH_CLIENT[@]}" ${RESOLVE[@]+"${RESOLVE[@]}"} --state "$WORK/$conn.json" "$@" >"$WORK/body"
}

# consent CONN COMMAND USER -> 동의 화면의 호출(inspect·approve·deny)을 USER 로 한다. 비밀번호는 환경으로만 넘긴다
consent() { MCP_OAUTH_PASSWORD="$LOCAL_PW2" oauth "$1" "$2" --user "$3"; }

# new_conn CONN -> 발견(과 등록)을 마친 base 에서 연결 하나를 연다
new_conn() { cp "$WORK/base.json" "$WORK/$1.json"; }

# conn_get CONN KEY -> 상태 파일의 값. 없으면 빈 줄
conn_get() { json_of "$WORK/$1.json" "d.get('$2') or ''"; }

# claims CONN -> 그 연결의 접근 토큰 클레임 JSON. sid_of 는 그 sid(연결 id)다
claims() { jwt_json "$(conn_get "$1" access_token)" 1; }
sid_of() { claims "$1" | json_of - 'd["sid"]'; }

# answered STATUS [ERROR] -> 0 이면 마지막 요약의 status 가 STATUS 이고 error 가 ERROR(없으면 None)다
answered() { [ "$(jq_ 'd["status"], d.get("error")')" = "$1 ${2:-None}" ]; }

# redirected ERROR -> 0 이면 마지막 인가가 등록한 loopback 으로 error=ERROR 와 우리 state, 발급자 iss 를 싣고 갔다
redirected() {
  [ "$(jq_ 'd["landing"]["host"], d["landing"]["path"], d["error"], d["landing"]["state_ok"], d["landing"]["iss"]')" \
    = "$REDIRECT_HOST $REDIRECT_PATH $1 True $PUBLIC_URL" ]
}

# requested STEP CONN [authorize 인자...] -> CONN 으로 인가를 청해 동의 화면의 요청 핸들을 받는다. 아니면 die STEP
requested() {
  local step="$1" conn="$2"
  shift 2
  oauth "$conn" authorize "$@" \
    && [ "$(jq_ 'd["status"], d["landing"]["path"], d["landing"]["request"]')" = "302 /oauth/consent True" ] \
    || die "$step" "authorize ($conn) did not reach the consent screen with a request"
}

# approved STEP CONN -> 새 연결 CONN 의 인가를 u1 이 허용해 코드를 받는다. connect 는 코드 교환까지 하고 나머지 인자를
# token 에 넘긴다. 접근 토큰과 갱신 토큰은 CONN 의 상태 파일에 남는다
approved() {
  new_conn "$2"
  requested "$1" "$2"
  consent "$2" approve "$U1" && answered 200 || die "$1" "approve ($2) as $U1"
}
connect() {
  local step="$1" conn="$2"
  shift 2
  approved "$step" "$conn"
  oauth "$conn" token "$@" && answered 200 || die "$step" "code exchange ($conn)"
}

# expect_grant STEP GRANT_ID STATE -> 관리자 목록에서 본 u1 의 그 연결이 "status revoked_reason" 으로 STATE 가 아니면 die
expect_grant() {
  local state=""
  [ "$(api GET "/users/$U1_ID/oauth-grants")" = 200 ] \
    && state="$(jq_ "next((f\"{g['status']} {g['revoked_reason']}\" for g in d if g['grant_id'] == '$2'), '')")"
  [ "$state" = "$3" ] || die "$1" "grant $2 is '$state', want '$3'"
}

echo ">> target=$TARGET context=$CTX tenant T=$TENANT other X=$OTHER api=$API wait=$WAIT_MODE pod_restart=$POD_RESTART"
[ -n "$CTX" ] || die 0 "INSTALL_CONTEXT 가 비었다 (설치 값 파일 또는 make e2e-oauth E2E_TARGET=install INSTALL_CONTEXT=...)"

# ---------- 0. 모드, 관리자, 사용자, 대상 ----------
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"
if [ "$TARGET" = orbstack ]; then
  switch_auth_mode local DAEGAM_OAUTH_ALLOW_HTTP=true \
    || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to local with plain-http OAuth and rolling the control plane failed"
  MODE_NOTE="ConfigMap DAEGAM_AUTH_MODE=local (was ${AUTH_MODE_PREV:-unset}) and DAEGAM_OAUTH_ALLOW_HTTP=true, control plane rolled"
else
  install_admin 0
  MODE_NOTE="the install is already in local mode"
fi
local_admin_session 0
# local_admin_session 이 남긴 /auth/config 본문이다
[ "$(jq_ '(d.get("oauth") or {}).get("enabled")')" = True ] \
  || die 0 "auth config: $(cat "$WORK/body") (want oauth.enabled true)"

U1_ID="$(local_fresh_member "$U1" "$TENANT")" || die 0 "creating $U1 in $TENANT failed"
U2_ID="$(local_fresh_member "$U2" "$OTHER")" || die 0 "creating $U2 in $OTHER failed"
# 첫 로그인이라 서버가 강제하는 비밀번호 바꾸기를 지난다. 그 뒤로 스크립트 클라이언트는 바꾼 비밀번호(LOCAL_PW2)로 들어간다
U1_CTL="$(local_login_any "$U1" "$LOCAL_PW")"; [ -n "$U1_CTL" ] || die 0 "login as $U1 failed"
[ -n "$(local_login_any "$U2" "$LOCAL_PW")" ] || die 0 "login as $U2 failed"
: >"$WORK/ready_at"
PAT_NOTE=" (with a personal token)"
if [ "$PAT_RAN" = 1 ]; then
  PAT_NOTE=" (no personal token: e2e-pat ran before this gate at $OAUTH_GW_HOST)"
else
  issue_ok 0 "$U1_CTL" "$TENANT" "e2e-oauth"; PAT="$ISSUED"
fi

if [ "$TARGET" = orbstack ]; then
  traefik_ip 0
  # 개인 토큰의 키가 두 레플리카의 JWKS 캐시에 오른 뒤 배포한다. 새 파드는 첫 요청에서 그 JWKS 를 받는다
  sleep "$JWKS_CACHE_WAIT"
  TARGETS_MADE=1
  calc_mcp_server 0 "$BP_NAME" "$MS_NAME" '{}'
  calc_publish 0
  calc_gateway 0 "$GW_NAME" "$(exposure "$GW_HOST")"
  NS="$CALC_NS"
  GW_URL="$(jq_ 'd["status"]["external_url"]')"
  GW_PIN="$(pin "$GW_URL" "$LB_IP")"
  RESOLVE=(--resolve "$GW_PIN" --resolve "$(pin "$PUBLIC_URL" "$LB_IP")")
  if [ "$DOC_REDEPLOYED" = 1 ]; then
    # 이전 실행이 남긴 배포를 이었다. 그 파드는 개인 토큰의 키를 모르는 JWKS 를 들고 있을 수 있으므로 새로 띄운다
    restart_pods "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$CALC_GW_K8S_NAME" \
      || die 0 "restarting the vMCP pod left from an earlier run failed"
  fi
  TARGET_NOTE="deployed $GW_URL (vMCP $CALC_GW_K8S_NAME) over calculator $CALC_MS_K8S_NAME after the ${JWKS_CACHE_WAIT}s JWKS cache"
else
  # 노출해 둔 Gateway 의 host 는 E2E_OAUTH_GW_HOST 이고 없으면 EXPOSURE_DOMAIN 에서 나온다. 둘 다 없으면 조건부로, host 가
  # 있는데 그 Gateway 가 없으면 못 돈 것으로 STEP 1 부터 건너뛴다
  install_gateway 0 E2E_OAUTH_GW_HOST "$OAUTH_GW_HOST" || GW_RC=$?
  # STEP 16 이 파드를 굴릴 때만 그 Gateway 의 vMCP 를 찾는다
  if [ "$GW_RC" = 0 ] && [ "$POD_RESTART" = true ] && [ "$PAT_RAN" = 0 ]; then gateway_vmcp 0 "$GW_ID"; fi
  TARGET_NOTE="read Gateway $GW_ID at $GW_URL"
  [ "$GW_RC" = 0 ] || TARGET_NOTE="no exposed Gateway: $GW_NOTE"
fi
URLS=("$GW_URL"); PINS=("$GW_PIN")
pass 0 "$MODE_NOTE; admin signed in, data-plane auth settled, issuer $PUBLIC_URL with oauth.enabled; $U1 in $TENANT$PAT_NOTE and $U2 in $OTHER; $TARGET_NOTE"
if [ "$GW_RC" != 0 ]; then
  # 흐름은 모두 그 Gateway 의 401 과 PRM 에서 시작한다
  skip_without_gateway "$GW_RC" 1 2 3 4 5 6 7 8 9 10 11 12 13 15 16 14
  finish
fi

# ---------- 1·2·3. 401, PRM, 인가 서버 메타데이터 ----------
# 배포 직후에는 Gateway 가 다시 렌더되기까지 틱 몇 개가 걸리므로 scope="mcp" 가 광고될 때까지 본다
advertised() {
  oauth base discover "$GW_URL" \
    && [ "$(jq_ "d['challenge'].get('scope') == 'mcp' and d['prm'].get('scopes_supported') == ['mcp']")" = True ]
}
DISCOVER_FROM=$(date +%s)
until advertised; do
  [ $(($(date +%s) - DISCOVER_FROM)) -lt "$DISCOVER_LIMIT" ] \
    || die 1 "no 401 with resource_metadata and scope=\"mcp\" and a readable PRM and metadata at $GW_URL within ${DISCOVER_LIMIT}s"
  sleep "$DISCOVER_POLL"
done
pass 1 "tokenless initialize at $GW_URL -> 401 with resource_metadata=$(jq_ 'd["challenge"]["resource_metadata"]') and scope=\"mcp\""

[ "$(jq_ "d['prm'].get('resource') == '$GW_URL' and d['prm'].get('authorization_servers') == ['$PUBLIC_URL']")" = True ] \
  || die 2 "PRM $(jq_ 'json.dumps(d["prm"])') (want resource $GW_URL, authorization_servers [$PUBLIC_URL])"
pass 2 "PRM resource $GW_URL, authorization_servers [$PUBLIC_URL], scopes_supported [mcp]"

python3 - "$WORK/body" <<'PY' || die 3 "authorization server metadata: see above"
import json, sys
d = json.load(open(sys.argv[1]))
m, issuer = d["metadata"], d["prm"]["authorization_servers"][0]
problems = [p for p, ok in [
    (f"issuer {m.get('issuer')!r} is not the PRM authorization server {issuer!r}", m.get("issuer") == issuer),
    ("code_challenge_methods_supported lacks S256", "S256" in m.get("code_challenge_methods_supported", [])),
    ("token_endpoint_auth_methods_supported lacks none", "none" in m.get("token_endpoint_auth_methods_supported", [])),
    ("client_id_metadata_document_supported is not true", m.get("client_id_metadata_document_supported") is True),
    ("authorization_response_iss_parameter_supported is not true",
     m.get("authorization_response_iss_parameter_supported") is True),
    ("no registration_endpoint", bool(m.get("registration_endpoint"))),
] if not ok]
sys.exit("; ".join(problems)) if problems else None
PY
pass 3 "metadata issuer is the PRM authorization server $PUBLIC_URL; S256, none, CIMD and iss are advertised"

# ---------- 4. DCR ----------
cursor_register() {
  local args=() uri
  for uri in "${CURSOR_REDIRECT_URIS[@]}"; do args+=(--redirect-uri "$uri"); done
  oauth cursor register --name Cursor "${args[@]}"
}
new_conn cursor   # base 는 아직 발견만 했다
cursor_register \
  && [ "$(jq_ "d['status'], d['body']['redirect_uris'] == ['${CURSOR_REDIRECT_URIS[0]}'], d['body']['token_endpoint_auth_method']")" = "201 True none" ] \
  || die 4 "Cursor-shaped DCR (want 201, only ${CURSOR_REDIRECT_URIS[0]}, auth none)"
CURSOR_ID="$(jq_ 'd["body"]["client_id"]')"
cursor_register && [ "$(jq_ 'd["body"]["client_id"]')" = "$CURSOR_ID" ] || die 4 "the same registration got another client_id"
TAMPERED_ID="$(python3 -c 'import sys; s = sys.argv[1]; print(s[:-1] + ("B" if s[-1] == "A" else "A"))' "$CURSOR_ID")"
oauth cursor authorize --client-id "$TAMPERED_ID" \
  && [ "$(jq_ 'd["landing"]["path"], d["error"]')" = "/oauth/consent invalid_client" ] \
  || die 4 "authorize with a tampered client_id (want the consent screen with invalid_client)"
oauth base register --redirect-uri "$REDIRECT_URI" && answered 201 || die 4 "DCR of the e2e client with $REDIRECT_URI"
pass 4 "Cursor-shaped DCR kept only ${CURSOR_REDIRECT_URIS[0]} with auth none and the same registration got the same client_id; a one-character change lands on the consent screen with invalid_client; e2e client registered with $REDIRECT_URI"

# ---------- 5. resource 없음과 모르는 resource ----------
new_conn probe
oauth probe authorize --no-resource && redirected invalid_target || die 5 "authorize without resource (want invalid_target at $REDIRECT_URI)"
oauth probe authorize --resource "$GW_URL/unknown" && redirected invalid_target \
  || die 5 "authorize with an unknown resource (want invalid_target at $REDIRECT_URI)"
pass 5 "no resource and an unknown resource both go back to $REDIRECT_HOST$REDIRECT_PATH with error=invalid_target, our state and iss=$PUBLIC_URL"

# ---------- 6. 인가, inspect, 허용 ----------
new_conn c1
requested 6 c1
consent c1 inspect "$U1" && answered 200 || die 6 "inspect as $U1"
[ "$(jq_ "(d['body']['member'], d['body']['client']['kind'], d['body']['client']['name_collides'], d['body']['redirect']['host'],
  d['body']['resource']['url'], d['body']['resource']['tenant'], d['body']['resource']['kind'])")" \
  = "True dcr False $REDIRECT_HOST $GW_URL $TENANT gateway" ] || die 6 "inspect for $U1 (want member, dcr, $GW_URL in $TENANT)"
consent c1 approve "$U1" && answered 200 \
  && [ "$(jq_ "(d['body']['redirect']['host'], d['body']['redirect']['code'], d['body']['redirect']['state_ok'],
    d['body']['redirect']['iss'])")" = "$REDIRECT_HOST True True $PUBLIC_URL" ] \
  || die 6 "approve as $U1 (want redirect_to $REDIRECT_HOST with code, our state and iss)"
consent c1 approve "$U1" && answered 409 request_used || die 6 "the second approve of the same request (want 409 request_used)"
pass 6 "PKCE authorize reached the consent screen; inspect shows a DCR client for $GW_URL in $TENANT; approve with a fresh sign-in sent code, state and iss=$PUBLIC_URL to $REDIRECT_HOST; the second approve is 409 request_used"

# ---------- 7. 코드 교환과 Gateway 호출 ----------
oauth c1 token && answered 200 || die 7 "code exchange"
[ "$(jq_ 'd["cache_control"], d["body"]["token_type"], d["body"]["expires_in"], d["body"]["scope"], d["body"]["refresh_token"]')" \
  = "no-store Bearer 900 mcp offline_access True" ] || die 7 "token response (want no-store, Bearer, 900, mcp offline_access, a refresh token)"
python3 - "$(jwt_json "$(conn_get c1 access_token)" 0)" "$(claims c1)" "$PUBLIC_URL" "$MCP_AUDIENCE" "$GW_URL" "$T_GROUP" \
  "$TENANT" <<'PY' || die 7 "access token shape is wrong"
import json, sys
head, c = json.loads(sys.argv[1]), json.loads(sys.argv[2])
iss, aud, resource, group, tenant = sys.argv[3:8]
problems = [p for p, ok in [
    (f"alg {head.get('alg')}", head.get("alg") == "RS256"),
    (f"typ {head.get('typ')}", head.get("typ") == "at+jwt"),
    (f"iss {c.get('iss')}", c.get("iss") == iss),
    (f"aud {c.get('aud')}", c.get("aud") == [aud, resource]),
    (f"groups {c.get('groups')}", c.get("groups") == [group]),
    (f"tenant {c.get('tenant')}", c.get("tenant") == tenant),
    (f"scope {c.get('scope')}", c.get("scope") == "mcp"),
    (f"client_id {c.get('client_id')}", str(c.get("client_id", "")).startswith("dcr:")),
    ("no sid", bool(c.get("sid"))),
    (f"lifetime {c.get('exp', 0) - c.get('iat', 0)}", c.get("exp", 0) - c.get("iat", 0) == 900),
] if not ok]
sys.exit("; ".join(problems)) if problems else None
PY
SID1="$(sid_of c1)"
oauth c1 call --tool "$TOOL" ${CALL_ARGS[@]+"${CALL_ARGS[@]}"} \
  && [ "$(jq_ "'$TOOL_EXPECT' in json.dumps(d['call']['content'])")" = True ] \
  || die 7 "tools/list and tools/call of *$TOOL with the access token at $GW_URL"
CALLED="$(jq_ 'd["call"]["tool"]')"
pass 7 "code exchange gave a no-store Bearer (900s) and a refresh token; the RS256 at+jwt carries iss $PUBLIC_URL, aud [$MCP_AUDIENCE, $GW_URL], groups [$T_GROUP], sid; the script client listed and called $CALLED at $GW_URL"

# ---------- 8. 틀린 verifier 와 코드 재사용 ----------
approved 8 c2
oauth c2 token --verifier "$WRONG_VERIFIER" && answered 400 invalid_grant || die 8 "a wrong code_verifier (want invalid_grant)"
oauth c2 token && answered 400 invalid_grant || die 8 "the right code_verifier after a failed exchange (want invalid_grant, the code is burnt)"
connect 8 c3
SID3="$(sid_of c3)"
oauth c3 token && answered 400 invalid_grant || die 8 "reusing an exchanged code (want invalid_grant)"
expect_grant 8 "$SID3" "revoked code_reuse"
pass 8 "a wrong code_verifier is invalid_grant and burns the code; reusing an exchanged code is invalid_grant and cut its connection (revoked code_reuse)"

# ---------- 9. 갱신 회전과 재사용 ----------
RT1="$(conn_get c1 refresh_token)"
oauth c1 refresh && answered 200 || die 9 "refresh"
RT2="$(conn_get c1 refresh_token)"
[ "${RT1%%.*}" = "${RT2%%.*}" ] && [ "$RT1" != "$RT2" ] || die 9 "rotation must keep the family and change the secret"
[ "$(sid_of c1)" = "$SID1" ] || die 9 "the refreshed access token names another connection"
oauth c1 refresh --client-id "$CURSOR_ID" && answered 400 invalid_grant || die 9 "refresh with another client_id (want invalid_grant)"
expect_grant 9 "$SID1" "active None"
sleep "$REUSE_WAIT"
oauth c1 refresh --previous && answered 400 invalid_grant || die 9 "reusing the previous refresh token after ${REUSE_WAIT}s (want invalid_grant)"
expect_grant 9 "$SID1" "revoked refresh_reuse"
oauth c1 refresh && answered 400 invalid_grant || die 9 "the current refresh token of a cut family (want invalid_grant)"
pass 9 "refresh rotated the secret in the same family; another client_id is invalid_grant and the connection lives; the previous refresh token after ${REUSE_WAIT}s cut the family (revoked refresh_reuse)"

# ---------- 10. 토큰 요청의 resource ----------
connect 10 c4 --resource "$PUBLIC_URL"
[ "$(claims c4 | json_of - "d['aud'] == ['$MCP_AUDIENCE', '$GW_URL']")" = True ] \
  || die 10 "aud of a token requested with resource=$PUBLIC_URL: $(claims c4 | json_of - 'd["aud"]')"
AT4="$(conn_get c4 access_token)"; SID4="$(sid_of c4)"
allowed "$GW_URL" "$GW_PIN" "$AT4" || die 10 "the access token at $GW_URL: $MCP_NOTE"
pass 10 "resource=$PUBLIC_URL in the token request still gave aud [$MCP_AUDIENCE, $GW_URL]; $MCP_TOOL is allowed with it"

# ---------- 11. 소속이 아닌 사람 ----------
new_conn cx
requested 11 cx
consent cx inspect "$U2" && answered 200 || die 11 "inspect as $U2"
[ "$(jq_ "(d['body']['member'], d['body']['resource']['url'], d['body']['resource']['tenant'], d['body']['resource']['name'],
  d['body']['resource']['display_name'])")" = "False $GW_URL None None None" ] \
  || die 11 "inspect for a non-member (want the public address alone)"
consent cx approve "$U2" && answered 403 not_a_member || die 11 "approve as $U2 (want 403 not_a_member)"
pass 11 "inspect for $U2 of $OTHER carries $GW_URL alone (no tenant, no names); its approve is 403 not_a_member"

# ---------- 12. 돌아갈 곳 ----------
new_conn r
requested 12 r --redirect-uri "http://localhost:$OTHER_PORT$REDIRECT_PATH"
oauth r authorize --redirect-uri "http://$REDIRECT_HOST:$OTHER_PORT/elsewhere" \
  && [ "$(jq_ 'd["landing"]["path"], d["error"]')" = "/oauth/consent invalid_redirect_uri" ] \
  || die 12 "another path (want the consent screen with invalid_redirect_uri)"
pass 12 "the registered $REDIRECT_URI accepts http://localhost:$OTHER_PORT$REDIRECT_PATH; another path lands on the consent screen with invalid_redirect_uri"

# ---------- 13. 회수 ----------
oauth c4 revoke && answered 200 || die 13 "RFC 7009 revoke of the refresh token"
REVOKED_AT=$(date +%s)   # 한도는 회수 요청이 끝난 때부터 잰다
oauth c4 refresh && answered 400 invalid_grant || die 13 "refresh after the revoke (want invalid_grant)"
expect_grant 13 "$SID4" "revoked client"
if [ "$WAIT_MODE" = full ]; then
  wait_unauthenticated 13 "$REVOKED_AT" "$AT4"
  echo "  revoke-to-401: ${REFUSED_AFTER}s (limit ${REVOKE_LIMIT}s)"
  pass 13 "revoke answered 200; the next refresh is invalid_grant (revoked client); the access token was refused at $GW_URL after ${REFUSED_AFTER}s (<= ${REVOKE_LIMIT}s)"
else
  pass 13 "revoke answered 200; the next refresh is invalid_grant (revoked client). E2E_OAUTH_WAIT=full also times the access token at the Gateway"
fi

# ---------- 15. CIMD (설치본만) ----------
if [ "$TARGET" = install ]; then
  notes=""
  for url in "${CIMD_CLIENTS[@]}"; do
    new_conn cimd
    oauth cimd cimd "$url" || die 15 "reading the client metadata document $url"
    requested 15 cimd
    consent cimd inspect "$U1" && answered 200 || die 15 "inspect for $url"
    host="$(url_host "$url")"
    [ "$(jq_ "(d['body']['client']['kind'], d['body']['client']['ref'], d['body']['client']['host'])")" = "cimd $url $host" ] \
      || die 15 "inspect for $url (want cimd published by $host)"
    notes="$notes $host;"
  done
  pass 15 "CIMD client_ids reached the consent screen and inspect names the publishing host:$notes"
else
  cond_skip 15 "CIMD is checked on the install (the platform fetches the public client documents)"
fi

# ---------- 16. 개인 토큰과 내부 홉 ----------
# 설치본의 Gateway 는 STEP 0 의 개인 토큰 전에 떠 있었다. 굴려도 되면 새 파드가 첫 요청에서 JWKS 를 받고, 아니면 ready_at 이다
if [ "$PAT_RAN" = 1 ]; then
  cond_skip 16 "e2e-pat ran before this gate in this run and called the tool with a personal token at $OAUTH_GW_HOST (its STEP 2 to 4)"
else
  if [ "$TARGET" = install ] && [ "$POD_RESTART" = true ]; then
    restart_pods "$VMCP_SEL" || die 16 "restarting the vMCP pods of Gateway $GW_ID failed"
  elif [ "$TARGET" = install ]; then
    waited="$(ready_wait)"
    if [ "$waited" -gt 0 ]; then
      echo ">> waiting ${waited}s for ready_at + ${READY_SLACK}s (the key must be published before ToolHive refetches JWKS)"
      sleep "$waited"
    fi
  fi
  allowed "$GW_URL" "$GW_PIN" "$PAT" || die 16 "the personal token of $U1 at $GW_URL: $MCP_NOTE"
  HOP_NOTE=""
  if [ "$TARGET" = orbstack ]; then
    internal_hop 16 "$CALC_MS_K8S_NAME" "$PROXY_PORT" "$CALC_MS_ID"
    HOP_NOTE="; internal proxy mcp-$CALC_MS_K8S_NAME-proxy: no token 401, service token 200"
  fi
  pass 16 "the personal token of $U1 still calls $MCP_TOOL at $GW_URL$HOP_NOTE"
fi

# ---------- 14. 계정 막기 (맨 끝) ----------
connect 14 c5
SID5="$(sid_of c5)"
[ "$(api PATCH "/users/$U1_ID" '{"disabled": true}')" = 200 ] || die 14 "PATCH /users/$U1_ID disabled"
oauth c5 refresh && answered 400 invalid_grant || die 14 "refresh after disabling $U1 (want invalid_grant)"
expect_grant 14 "$SID5" "revoked disabled"
pass 14 "disabling $U1 made the next refresh invalid_grant at once (revoked disabled)"

finish
