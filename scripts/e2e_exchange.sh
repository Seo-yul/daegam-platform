#!/usr/bin/env bash
# P57 bundle A (D110) 게시된 Gateway 를 토큰 인증의 백엔드로 쓰는 e2e 다. 로컬 클러스터(태그의 클러스터 e2e 잡이 만든 k3d 의
# exchange 조각, deploy-local·lab-up 이후)만 대상이다.
#
#   make e2e-exchange
#
# STEP 0 이 플랫폼 ConfigMap 을 local 모드로 바꾸고 제어면을 굴린다. EXIT trap 이 성공, 실패, 끊김 어느 쪽이든 만든 것을 지운
# 뒤 모드를 되돌리고 제어면을 굴린다. 모드를 바꾸므로 다른 게이트와 클러스터를 나누지 않는다(scripts/lib/e2e_select.py 의
# exchange 조각). 같은 테넌트의 사람 둘(developer DEV, viewer VIEW)과 다른 테넌트의 사람 FAR 의 개인 토큰으로 본다.
# calculator 의 Add(규칙 없음)와 Divide(P 의 Gateway 규칙)가 대상 Tool 이고, lab petstore 의 getPetById 는 C 자신의 백엔드다. 단계는 다음과 같다:
#   STEP 0  ConfigMap 이 header 모드다(앞 실행이 남긴 값이면 바꾸기 전에 멈춘다). local 모드로 바꾸고 제어면을 굴린다
#   STEP 1  관리자 로그인, 사람 셋과 개인 토큰, JWKS 캐시를 기다린 뒤 MCP Server 둘(calculator 의 Add·Divide, petstore 의
#           getPetById)의 배포와 게시
#   STEP 2  Gateway P(calculator, Divide 는 developer 이상)를 배포해 entry 모드로 게시하고, Gateway C(P 의 항목과
#           getPetById, 소유자 Cedar 가 FAR 의 테넌트 그룹을 받는다)를 배포한다. C 의 Entry 토큰은 P 에 묶이고(bound_to
#           gateway:<P>, groups·cid·act 없음) P 의 렌더가 C 를 이름 대면 DEV 가 C 를 거쳐 Add 와 Divide 를 부른다. P 의 감사는
#           C 의 vMCP 를, C 의 감사는 DEV 를 남긴다
#   STEP 3  VIEW 는 C 에서 Add 를 부르고, Divide 는 목록에 없고 부르지 못한다(P 의 규칙이 C 에서 선다)
#   STEP 4  사슬 C2 → P1 → P: P 의 항목을 쓰는 P1 을 게시하고 그 항목을 쓰는 C2 를 배포한다. DEV 는 C2 에서 Divide 를
#           부르고 VIEW 는 부르지 못한다
#   STEP 5  P 의 규칙을 viewer 이상으로 바꿔 P 를 다시 배포하면, 재적용이 C 와 C2 를 차례로 다시 렌더한 뒤 VIEW 가 둘에서
#           Divide 를 부른다. 사슬은 재적용 틱 하나에 한 층씩 따라온다
#   STEP 6  C 의 소유자 Cedar 가 받는 FAR 의 토큰은 C 의 getPetById 를 부르지만 P 의 Tool 은 목록에 없고 부르지 못한다
#   STEP 7  P 에 소유자 forbid 를 저장하면 답의 consumer_notice 가 C·P1·C2 를 이름 대고, P 를 다시 배포하면 C 를 거친 호출이
#           거절되며 C 는 published_backend_closed, P 는 published_owner_forbid 를 경고한다. 그 P 를 백엔드로 저장하면 422
#           published_owner_forbid 다. forbid 를 걷고 다시 배포하면 C 가 다시 부른다
#   STEP 8  C 의 목록에 execute_tool_script 가 없고, 게시된 P 의 그 이름을 고른 저장은 422 published_script_tool 이다. P 는
#           code mode 를 켜지 않아 그 Tool 을 내지 않는다
#   STEP 9  C 의 Secret 에서 읽은 묶인 토큰은 P 에서 받히고 P1 의 vMCP 는 거절한다. C 가 P 를 빼고 다시 배포한 뒤 P 가 다시
#           렌더되면 그 사본도 P 가 거절하고, C2·P1 을 지운 뒤 P 의 게시를 거두어도 거절한다
#   정리    Gateway 다섯, 게시 항목, MCP Server 둘, 사람 셋을 지우고 ConfigMap 과 Secret 을 되돌린 뒤 제어면을 굴리고 재적용을
#           기다린다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다. macOS 에는 timeout 이 없어 기다림은
# 모두 폴링이다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/e2e_ports.sh"
# json_of·jwt_json 은 여기에 있다
. "$HERE/lib/e2e_json.sh"

[ "${E2E_TARGET:-orbstack}" = orbstack ] \
  || { echo "e2e-exchange 는 로컬 클러스터만 대상이다 (got E2E_TARGET=$E2E_TARGET)"; exit 1; }
# lib/e2e_local_auth.sh 의 local_admin_session 이 로컬 클러스터 갈래를 이 이름으로 가른다
TARGET=orbstack
CTX="${KUBE_CONTEXT:-orbstack}"
TENANT="${E2E_TENANT:-e2e-exch}"
OTHER="${E2E_OTHER_TENANT:-e2e-exch-x}"
API_PORT="${E2E_API_PORT:-$PORT_EXCHANGE_API}"
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_EXCHANGE_VMCP}"
AUX_PORT="${E2E_AUX_PORT:-$PORT_EXCHANGE_AUX}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
# 게시된 P 의 렌더가 새 소비자를 이름 대거나, 새 지문이 사슬 위로 올라가기까지의 한도. 재적용은 60초마다 돌고 층마다 한
# 틱이 들며(6.2절), 그 렌더는 vMCP 를 굴린다
SETTLE_WAIT="${E2E_EXCHANGE_SETTLE:-600}"
TOKEN_DAYS=7
# 재적용 틱(60초)에 렌더의 여유를 더한 것
REAPPLY_GAP=90
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"

BP_NAME="exch-calc"; MS_NAME="exch-calc-ms"; PET_BP_NAME="exch-pets"; PET_NAME="exch-pets-ms"
P_NAME="exch-p"; C_NAME="exch-c"; P1_NAME="exch-p1"; C2_NAME="exch-c2"; C3_NAME="exch-c3"
P_PUB="exch-p-pub"; P1_PUB="exch-p1-pub"
DEV="exch-dev-e2e"; VIEW="exch-view-e2e"; FAR="exch-far-e2e"
NAMED_FORBID='forbid(principal, action, resource) when { principal has claim_sub && principal.claim_sub == "e2e-nobody" };'
# 본문과 인자는 늘 변수에 먼저 둔다. bash 3.2 는 겹친 "$(…"$(…)"…)" 안의 {…, …} 를 중괄호 확장으로 쪼갠다
# (lib/e2e_api.sh ensure_stub_provider 와 같은 까닭이다)
# Divide 의 인자 이름은 calculator 의 WSDL 을 따른다(numerator·denominator). 다른 이름이면 도구 오류라 refused 로 읽힌다
ADD_ARGS='{"a": 3, "b": 4}'; DIV_ARGS='{"numerator": 8, "denominator": 2}'; PET_ARGS='{"petId": 1}'
# C 자신의 백엔드인 lab petstore(make lab-up 의 mock-petstore)다. calculator 의 WSDL 에는 Add 와 Divide 뿐이다
PET_SPEC="$ROOT/platform/tests/fixtures/petstore-swagger2.json"
PET_ENDPOINT="http://mock-petstore.${SPIKE_NS}.svc:18081/v2"
OTHER_PERMIT="permit(principal in THVGroup::\"daegam-$OTHER\", action, resource);"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-exchange.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=(); ADMIN_TOKEN=""
PF_API=""; PF_VMCP=""; PF_AUX=""
HOME_ID=""; MS_ID=""; PET_ID=""; CALC_ITEM=""; PET_ITEM=""; ITEM_ID=""; BOUND_TOKEN=""; WAITED=0; GW_UP_ID=""
P_ID=""; C_ID=""; P1_ID=""; C2_ID=""; C3_ID=""; P_ITEM=""; P1_ITEM=""
DEV_ID=""; VIEW_ID=""; FAR_ID=""; TOK_DEV=""; TOK_VIEW=""; TOK_FAR=""
C_URL="http://127.0.0.1:${VMCP_PORT}/mcp"; AUX_URL="http://127.0.0.1:${AUX_PORT}/mcp"

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$VMCP_PORT" "$AUX_PORT"
# pass·skip·cond_skip·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·api_as·jq_·api_delete·wait_ready·wait_gone·save_doc·deploy_ready·create_fresh·error_code 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# calc_backend 는 여기에 있다
. "$HERE/lib/e2e_calc.sh"
# platform_config·restart_platform·AUTH_MODE_CONFIGMAP 은 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# switch_auth_mode·restore_auth_mode 는 여기에 있다
. "$HERE/lib/e2e_auth_mode.sh"
# LOCAL_BOOT_PW·LOCAL_PW·local_login_any·local_admin_session·local_fresh_member·local_set_member 는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# issue_ok·JWKS_CACHE_WAIT 은 여기에 있다
. "$HERE/lib/e2e_personal_tokens.sh"
# audit_expect·cluster_now 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# mcp·mcp_json 은 여기에 있다
MCP_CLIENT_NAME="e2e-exchange"
. "$HERE/lib/mcp_rpc.sh"

cleanup() {
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  stop_pf "$PF_AUX" "$AUX_PORT"
  # 게시 항목을 쓰는 Gateway 가 먼저다. 쓰는 Gateway 가 남으면 게시를 거둘 수 없고, 게시한 Gateway 는 지울 수 없다
  local id
  for id in "$C3_ID" "$C2_ID" "$C_ID"; do
    [ -z "$id" ] || api_delete "/gateways/$id"
  done
  [ -z "$P1_ITEM" ] || api_delete "/hub/$P1_ITEM"
  [ -z "$P1_ID" ] || api_delete "/gateways/$P1_ID"
  [ -z "$P_ITEM" ] || api_delete "/hub/$P_ITEM"
  [ -z "$P_ID" ] || api_delete "/gateways/$P_ID"
  for id in "$MS_ID" "$PET_ID"; do
    [ -z "$id" ] || api_delete "/mcp-servers/$id"
  done
  for id in "$DEV_ID" "$VIEW_ID" "$FAR_ID"; do
    [ -z "$id" ] || api_delete "/users/$id"
  done
  # 모드를 STEP 0 앞의 값으로 되돌리고 제어면을 굴린 뒤 재적용을 기다린다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# --- 도우미 ------------------------------------------------------------------------------------------------------
# gateway_body NAME BACKENDS_JSON [AUTHZ_POLICY] -> Gateway 정의 본문. BACKENDS_JSON 은 [{hub_item_id, tools, tool_rules}]
gateway_body() {
  python3 -c 'import json, sys
body = {"name": sys.argv[1], "backends": json.loads(sys.argv[2])}
if len(sys.argv) > 3 and sys.argv[3]:
    body["authz_policy"] = sys.argv[3]
print(json.dumps(body))' "$@"
}
# items_json [TOOLS_JSON] ITEM_ID... -> 허브 항목마다 백엔드 하나인 목록. 첫 인자가 [ 로 시작하면 모든 백엔드의 tools 다
items_json() {
  python3 -c 'import json, sys
args = sys.argv[1:]
tools = json.loads(args.pop(0)) if args and args[0].startswith("[") else None
print(json.dumps([{"hub_item_id": i, **({"tools": tools} if tools is not None else {})} for i in args]))' "$@"
}
# p_backends ROLE -> P 의 백엔드. calculator 의 Add·Divide 이고 Divide 는 ROLE 이상만 부른다
p_backends() {
  printf '[{"hub_item_id": "%s", "tools": ["Add", "Divide"], "tool_rules": {"Divide": {"callers": {"roles": ["%s"]}}}}]' \
    "$CALC_ITEM" "$1"
}
# gateway_up STEP NAME BODY -> 만들고 배포해 Ready 를 기다린다. id 를 GW_UP_ID 에 남긴다
gateway_up() {
  create_fresh "$1" /gateways "$2" "$3"
  GW_UP_ID="$CREATED_ID"
  deploy_ready "$1" /gateways "$GW_UP_ID" "{\"cluster_id\": \"$HOME_ID\"}"
}
# publish STEP PATH ID NAME_JSON -> 게시해 그 허브 항목 id 를 ITEM_ID 에 남긴다. 다시 게시하면 200(멱등)이다
publish() {
  local code
  code="$(api POST "$2/$3/publish" "$4")"
  case "$code" in 200 | 201) ITEM_ID="$(jq_ 'd["id"]')" ;; *) die "$1" "POST $2/$3/publish -> $code: $(cat "$WORK/body")" ;; esac
}
# vmcp_forward STEP GW_ID PORT -> 그 Gateway 의 vMCP 를 PORT 로 port-forward 한다. 루프 pid 는 PF_PID 다(lib/e2e_pf.sh)
vmcp_forward() {
  local k8s ns
  [ "$(api GET "/gateways/$2")" = 200 ] || die "$1" "GET /gateways/$2 failed"
  k8s="$(jq_ 'd["k8s"]["name"]')"; ns="$(jq_ 'd["k8s"]["namespace"]')"
  start_pf "$ns" "svc/vmcp-$k8s" "${3}:4483"
  wait_port "$3" || die "$1" "port-forward to svc/vmcp-$k8s did not open :$3"
}
# outcome TOKEN URL TOOL ARGS -> called·refused 를 찍는다. 부른 결과는 $WORK/mcp.json·mcp.err 다
outcome() {
  if [ "$(mcp --token "$1" call "$2" "$3" "$4")" = 0 ]; then echo called; else echo refused; fi
}
# listed TOKEN URL -> 그 토큰의 tools/list 이름을 공백으로 이어 찍는다. 부르지 못하면 "?" 다
listed() {
  [ "$(mcp --token "$1" list "$2")" = 0 ] || { echo "?"; return 0; }
  mcp_json '" ".join(sorted(d["tools"]))'
}
# wait_outcome STEP SECONDS TOKEN URL TOOL ARGS WANT WHAT -> outcome 이 WANT 가 될 때까지 10초마다 본다. 걸린 초를 WAITED 에
# 남기고, 넘기면 die STEP 이다
wait_outcome() {
  local start=$SECONDS got=""
  while :; do
    got="$(outcome "$3" "$4" "$5" "$6")"
    [ "$got" = "$7" ] && { WAITED=$((SECONDS - start)); return 0; }
    [ $((SECONDS - start)) -lt "$2" ] || die "$1" "$8: $5 $got after $2s (want $7): $(cat "$WORK/mcp.json" "$WORK/mcp.err" 2>/dev/null)"
    sleep 10
  done
}
# expect_list STEP TOKEN URL WANT_IN WANT_OUT WHAT -> 목록에 WANT_IN(공백으로 나눈 이름)이 모두 있고 WANT_OUT 이 하나도
# 없어야 한다
expect_list() {
  local seen name
  seen=" $(listed "$2" "$3") "
  for name in $4; do
    case "$seen" in *" $name "*) ;; *) die "$1" "$6 lists [$seen], missing $name" ;; esac
  done
  for name in $5; do
    case "$seen" in *" $name "*) die "$1" "$6 lists [$seen], including $name" ;; esac
  done
}
# bound_token STEP GW_ID TARGET_ID -> 그 Gateway 의 Entry 토큰 Secret 가운데 TARGET 에 묶인 토큰을 BOUND_TOKEN 에 남긴다
bound_token() {
  "${K[@]}" get secret -A -l "daegam.devops.ai.kr/resource=gateway-svc-token,daegam.devops.ai.kr/id=$2" -o json \
    >"$WORK/secrets.json" || die "$1" "listing the token Secrets of Gateway $2 failed"
  python3 - "$WORK/secrets.json" "gateway:$3" >"$WORK/bound.jwt" <<'PY' || die "$1" "Gateway $2 has no token bound to gateway:$3"
import base64, json, sys
target = sys.argv[2]
for s in json.load(open(sys.argv[1]))["items"]:
    raw = (s.get("data") or {}).get("token")
    if not raw:
        continue
    token = base64.b64decode(raw).decode()
    part = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    if claims.get("bound_to") == target:
        print(token)
        sys.exit(0)
sys.exit(1)
PY
  BOUND_TOKEN="$(cat "$WORK/bound.jwt")"
}
# warnings_of GW_ID -> 상태 경고의 코드를 공백으로 이어 찍는다. GET 이 200 이 아니면 "GET <코드>" 를 찍고 0 으로 끝난다.
# 기다림이 부르므로 set -e 아래에서 게이트를 끝내지 않는다
warnings_of() {
  local code expr='" ".join(sorted({w.split(":")[0] for w in (d.get("status") or {}).get("warnings", [])}))'
  code="$(api GET "/gateways/$1")"
  if [ "$code" != 200 ]; then
    echo "GET $code"
    return 0
  fi
  jq_ "$expr" || echo "unreadable"
}
# refused_at TOKEN URL -> 그 토큰의 tools/list 가 비고 Add 호출이 실패하면 0 이다(mcp_refuses 와 같은 판정, 기다림용)
refused_at() {
  [ "$(mcp --token "$1" list "$2")" = 0 ] && [ "$(mcp_json 'd["tools"]')" = "[]" ] \
    && [ "$(mcp --token "$1" call "$2" Add "$ADD_ARGS")" != 0 ]
}
# wait_warning STEP GW_ID CODE PRESENT -> 그 경고가 있거나(PRESENT=1) 없을 때까지 10초마다 SETTLE_WAIT 초 본다
wait_warning() {
  local start=$SECONDS seen=""
  while :; do
    seen=" $(warnings_of "$2") "
    case "$seen" in *" $3 "*) [ "$4" = 1 ] && return 0 ;; *) [ "$4" = 0 ] && return 0 ;; esac
    [ $((SECONDS - start)) -lt "$SETTLE_WAIT" ] || die "$1" "Gateway $2 warnings [$seen] after ${SETTLE_WAIT}s (want $3 present=$4)"
    sleep 10
  done
}
# redeploy STEP GW_ID -> 다시 배포해 Ready 를 기다린다
redeploy() {
  local code
  code="$(api POST "/gateways/$2/redeploy")"
  [ "$code" = 200 ] || die "$1" "POST /gateways/$2/redeploy -> $code: $(cat "$WORK/body")"
  wait_ready "/gateways/$2" "$1"
}

# --- 단계 --------------------------------------------------------------------------------------------------------
step_0() {
  local mode
  mode="$(platform_config DAEGAM_AUTH_MODE)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  [ "${mode:-header}" = header ] \
    || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP has DAEGAM_AUTH_MODE='$mode' (left by an earlier run?): set header mode, roll the control plane and run again"
  switch_auth_mode local || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to local mode and rolling the control plane failed"
  pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=local (was ${AUTH_MODE_PREV:-unset}); platform and UI rolled out"
}

step_1() {
  local code ctl
  local_admin_session 1
  code="$(api GET /clusters)"
  [ "$code" = 200 ] || die 1 "GET /clusters -> $code"
  HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home" and c["status"]["preflight"]["ok"]), "")')"
  [ -n "$HOME_ID" ] || die 1 "cluster home missing or preflight not ok"
  DEV_ID="$(local_fresh_member "$DEV" "$TENANT")" || die 1 "creating $DEV in $TENANT failed"
  local_set_member "$TENANT" "$DEV_ID" developer || die 1 "making $DEV a developer of $TENANT failed"
  VIEW_ID="$(local_fresh_member "$VIEW" "$TENANT")" || die 1 "creating $VIEW in $TENANT failed"
  FAR_ID="$(local_fresh_member "$FAR" "$OTHER")" || die 1 "creating $FAR in $OTHER failed"
  : >"$WORK/ready_at"
  ctl="$(local_login_any "$DEV" "$LOCAL_PW")"; [ -n "$ctl" ] || die 1 "login as $DEV failed"
  issue_ok 1 "$ctl" "$TENANT" "e2e-exchange dev"; TOK_DEV="$ISSUED"
  ctl="$(local_login_any "$VIEW" "$LOCAL_PW")"; [ -n "$ctl" ] || die 1 "login as $VIEW failed"
  issue_ok 1 "$ctl" "$TENANT" "e2e-exchange view"; TOK_VIEW="$ISSUED"
  ctl="$(local_login_any "$FAR" "$LOCAL_PW")"; [ -n "$ctl" ] || die 1 "login as $FAR failed"
  issue_ok 1 "$ctl" "$OTHER" "e2e-exchange far"; TOK_FAR="$ISSUED"
  # 발급한 슬롯이 JWKS 캐시에 오른 뒤 배포한다. 새 vMCP 파드는 첫 요청에서 그 JWKS 를 받는다
  sleep "$JWKS_CACHE_WAIT"
  calc_backend 1 "$BP_NAME"
  save_doc 1 /mcp-servers "$MS_NAME" \
    "{\"name\": \"$MS_NAME\", \"backend_profile_id\": \"$CALC_BP_ID\", \"spec_version\": 1, \"tools\": [\"Add\", \"Divide\"]}"
  MS_ID="$SAVED_ID"
  deploy_ready 1 /mcp-servers "$MS_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  upload_backend_api 1 "$PET_BP_NAME" "$PET_SPEC"
  code="$(api PATCH "/backend-apis/$BACKEND_API_ID" "{\"endpoint\": \"$PET_ENDPOINT\"}")"
  [ "$code" = 200 ] || die 1 "PATCH /backend-apis/$BACKEND_API_ID endpoint -> $code"
  save_doc 1 /mcp-servers "$PET_NAME" \
    "{\"name\": \"$PET_NAME\", \"backend_profile_id\": \"$BACKEND_API_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": [\"getPetById\"]}"
  PET_ID="$SAVED_ID"
  deploy_ready 1 /mcp-servers "$PET_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  publish 1 /mcp-servers "$MS_ID" '{}'; CALC_ITEM="$ITEM_ID"
  publish 1 /mcp-servers "$PET_ID" '{}'; PET_ITEM="$ITEM_ID"
  pass 1 "$DEV (developer) and $VIEW (viewer) of $TENANT, $FAR of $OTHER hold personal tokens; MCP Servers $MS_NAME (Add, Divide) and $PET_NAME (getPetById) deployed and published after the ${JWKS_CACHE_WAIT}s JWKS cache"
}

step_2() {
  local token since until backends body pub shape claims
  backends="$(p_backends developer)"
  body="$(gateway_body "$P_NAME" "$backends")"
  gateway_up 2 "$P_NAME" "$body"
  P_ID="$GW_UP_ID"
  pub="$(printf '{"name": "%s", "mode": "entry"}' "$P_PUB")"
  publish 2 /gateways "$P_ID" "$pub"; P_ITEM="$ITEM_ID"
  backends="$(items_json "$P_ITEM" "$PET_ITEM")"
  body="$(gateway_body "$C_NAME" "$backends" "$OTHER_PERMIT")"
  gateway_up 2 "$C_NAME" "$body"
  C_ID="$GW_UP_ID"
  bound_token 2 "$C_ID" "$P_ID"; token="$BOUND_TOKEN"
  shape="d['sub'] == 'svc:vmcp:gw-$C_NAME' and d['aud'] == 'daegam-mcp' and d['tenant'] == '$TENANT'"
  shape="$shape and not {'groups', 'cid', 'act', 'tv'} & set(d)"
  claims="$(jwt_json "$token" 1)"
  [ "$(json_of - "$shape" <<<"$claims")" = True ] || die 2 "C's bound token: $claims"
  vmcp_forward 2 "$C_ID" "$VMCP_PORT"; PF_VMCP="$PF_PID"
  # P 의 렌더가 C 를 소비자로 이름 댈 때까지 C 는 published_consumer_pending 을 경고하고 P 의 Tool 을 부르지 못한다
  wait_warning 2 "$C_ID" published_consumer_pending 0
  since="$(cluster_now)"
  wait_outcome 2 "$SETTLE_WAIT" "$TOK_DEV" "$C_URL" Add "$ADD_ARGS" called "$DEV through C"
  # P 가 C 를 이름 댄 렌더에서 규칙 지문이 처음 생긴다. C 는 다음 재적용 틱에 그 규칙을 컴파일하고 지문을 토큰에 실으며,
  # 그때까지 규칙 있는 Divide 는 C 에 없다(rules_digest_behind). 규칙 없는 Add 가 한 틱 먼저 열린다
  wait_outcome 2 "$SETTLE_WAIT" "$TOK_DEV" "$C_URL" Divide "$DIV_ARGS" called "$DEV through C after C applied P's rule"
  until="$(cluster_now)"
  audit_expect 2 "$since" \
    "p|$since|$until|$P_ID|Add|svc:vmcp:gw-$C_NAME|*|1" \
    "c|$since|$until|$C_ID|Add|$DEV_ID|-|1"
  pass 2 "C's Entry token is bound to gateway:$P_ID with no groups, cid or act; once P named C (no published_consumer_pending) $DEV called Add and Divide through C; P's audit records svc:vmcp:gw-$C_NAME and C's records $DEV"
}

step_3() {
  expect_list 3 "$TOK_VIEW" "$C_URL" "Add getPetById" "Divide" "$VIEW at C"
  [ "$(outcome "$TOK_VIEW" "$C_URL" Divide "$DIV_ARGS")" = refused ] || die 3 "$VIEW called Divide through C"
  [ "$(outcome "$TOK_VIEW" "$C_URL" Add "$ADD_ARGS")" = called ] || die 3 "$VIEW through C: Add refused: $(cat "$WORK/mcp.json" "$WORK/mcp.err" 2>/dev/null)"
  pass 3 "$VIEW sees Add and getPetById at C, not Divide, and its Divide call is refused: P's rule holds at C"
}

step_4() {
  local backends body pub
  backends="$(items_json "$P_ITEM")"
  body="$(gateway_body "$P1_NAME" "$backends")"
  gateway_up 4 "$P1_NAME" "$body"
  P1_ID="$GW_UP_ID"
  pub="$(printf '{"name": "%s", "mode": "entry"}' "$P1_PUB")"
  publish 4 /gateways "$P1_ID" "$pub"; P1_ITEM="$ITEM_ID"
  backends="$(items_json "$P1_ITEM")"
  body="$(gateway_body "$C2_NAME" "$backends")"
  gateway_up 4 "$C2_NAME" "$body"
  C2_ID="$GW_UP_ID"
  vmcp_forward 4 "$C2_ID" "$AUX_PORT"; PF_AUX="$PF_PID"
  wait_outcome 4 "$SETTLE_WAIT" "$TOK_DEV" "$AUX_URL" Divide "$DIV_ARGS" called "$DEV through C2 → P1 → P"
  expect_list 4 "$TOK_VIEW" "$AUX_URL" "Add" "Divide" "$VIEW at C2"
  [ "$(outcome "$TOK_VIEW" "$AUX_URL" Divide "$DIV_ARGS")" = refused ] || die 4 "$VIEW called Divide through C2"
  pass 4 "chain C2 → P1 → P: $DEV called Divide after ${WAITED}s; $VIEW sees Add, not Divide, and is refused: P's rule holds two levels up"
}

step_5() {
  local code at_c at_c2 backends body
  backends="$(p_backends viewer)"
  body="$(gateway_body "$P_NAME" "$backends")"
  code="$(api PUT "/gateways/$P_ID" "$body")"
  [ "$code" = 200 ] || die 5 "PUT /gateways/$P_ID with the viewer rule -> $code: $(cat "$WORK/body")"
  redeploy 5 "$P_ID"
  wait_outcome 5 "$SETTLE_WAIT" "$TOK_VIEW" "$C_URL" Divide "$DIV_ARGS" called "$VIEW through C after P's new rule"
  at_c="$WAITED"
  wait_outcome 5 "$SETTLE_WAIT" "$TOK_VIEW" "$AUX_URL" Divide "$DIV_ARGS" called "$VIEW through C2 after P's new rule"
  at_c2="$WAITED"
  wait_warning 5 "$C_ID" rules_digest_behind 0
  pass 5 "P's rule moved to viewer and P redeployed: $VIEW called Divide through C after ${at_c}s and through C2 ${at_c2}s later (one level per reapply tick)"
}

step_6() {
  expect_list 6 "$TOK_FAR" "$C_URL" "getPetById" "Add Divide" "$FAR ($OTHER) at C"
  [ "$(outcome "$TOK_FAR" "$C_URL" getPetById "$PET_ARGS")" = called ] || die 6 "$FAR through C: getPetById refused: $(cat "$WORK/mcp.json" "$WORK/mcp.err" 2>/dev/null)"
  [ "$(outcome "$TOK_FAR" "$C_URL" Add "$ADD_ARGS")" = refused ] || die 6 "$FAR called P's Add through C"
  pass 6 "C's owner Cedar admits $FAR of $OTHER: getPetById called, P's Add and Divide neither listed nor callable (C's tenant fence)"
}

step_7() {
  local code notice backends body
  backends="$(p_backends viewer)"
  body="$(gateway_body "$P_NAME" "$backends" "$NAMED_FORBID")"
  code="$(api PUT "/gateways/$P_ID" "$body")"
  [ "$code" = 200 ] || die 7 "PUT /gateways/$P_ID with a forbid -> $code: $(cat "$WORK/body")"
  notice="$(jq_ '" ".join(sorted((d.get("consumer_notice") or {}).get("closed") or []))')"
  [ "$notice" = "$C_NAME $C2_NAME $P1_NAME" ] || die 7 "consumer_notice names [$notice] (want $C_NAME $C2_NAME $P1_NAME)"
  redeploy 7 "$P_ID"
  wait_outcome 7 "$SETTLE_WAIT" "$TOK_DEV" "$C_URL" Add "$ADD_ARGS" refused "$DEV through C to P with a forbid"
  wait_warning 7 "$C_ID" published_backend_closed 1
  case " $(warnings_of "$P_ID") " in *" published_owner_forbid "*) ;; *) die 7 "P does not warn published_owner_forbid" ;; esac
  body="$(items_json "$P_ITEM")"
  body="$(gateway_body "$C3_NAME" "$body")"
  code="$(api POST /gateways "$body")"
  [ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = published_owner_forbid ] \
    || die 7 "saving a Gateway over P with a forbid -> $code $(error_code "$WORK/body") (want 422 published_owner_forbid)"
  body="$(gateway_body "$P_NAME" "$backends")"
  code="$(api PUT "/gateways/$P_ID" "$body")"
  [ "$code" = 200 ] || die 7 "PUT /gateways/$P_ID without the forbid -> $code"
  redeploy 7 "$P_ID"
  wait_outcome 7 "$SETTLE_WAIT" "$TOK_DEV" "$C_URL" Add "$ADD_ARGS" called "$DEV through C after the forbid went"
  pass 7 "P's forbid: consumer_notice named [$notice]; after the redeploy C's call was refused, C warned published_backend_closed and P published_owner_forbid; a new Gateway over P was 422 published_owner_forbid; without the forbid C called again"
}

step_8() {
  local code body
  expect_list 8 "$TOK_DEV" "$C_URL" "Add" "execute_tool_script" "$DEV at C"
  body="$(items_json '["execute_tool_script"]' "$P_ITEM")"
  body="$(gateway_body "$C3_NAME" "$body")"
  code="$(api POST /gateways "$body")"
  [ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = published_script_tool ] \
    || die 8 "selecting P's execute_tool_script -> $code $(error_code "$WORK/body") (want 422 published_script_tool)"
  pass 8 "C does not list execute_tool_script and a selection naming it is 422 published_script_tool (P runs without code mode)"
}

step_9() {
  local copy code body start
  bound_token 9 "$C_ID" "$P_ID"; copy="$BOUND_TOKEN"
  stop_pf "$PF_AUX" "$AUX_PORT"
  vmcp_forward 9 "$P1_ID" "$AUX_PORT"; PF_AUX="$PF_PID"
  mcp_refuses 9 "$copy" "C's token bound to P, at P1" "$AUX_URL"
  stop_pf "$PF_AUX" "$AUX_PORT"
  vmcp_forward 9 "$P_ID" "$AUX_PORT"; PF_AUX="$PF_PID"
  [ "$(outcome "$copy" "$AUX_URL" Add "$ADD_ARGS")" = called ] || die 9 "P refused C's live bound token: $(cat "$WORK/mcp.json" "$WORK/mcp.err" 2>/dev/null)"
  # C 가 P 를 빼면 P 의 다음 렌더가 그 사본을 닫는다
  body="$(items_json "$PET_ITEM")"
  body="$(gateway_body "$C_NAME" "$body" "$OTHER_PERMIT")"
  code="$(api PUT "/gateways/$C_ID" "$body")"
  [ "$code" = 200 ] || die 9 "PUT /gateways/$C_ID without P -> $code"
  redeploy 9 "$C_ID"
  start=$SECONDS
  until refused_at "$copy" "$AUX_URL"; do
    [ $((SECONDS - start)) -lt "$SETTLE_WAIT" ] || break
    sleep 10
  done
  WAITED=$((SECONDS - start))
  mcp_refuses 9 "$copy" "a copy of C's token after C dropped P, at P" "$AUX_URL"
  # 사슬과 게시를 걷어도 거절이 남는다. P 는 마지막 묶인 토큰이 끝날 때까지 빈 집합을 렌더한다
  api_delete "/gateways/$C2_ID"; wait_gone "/gateways/$C2_ID" || die 9 "Gateway $C2_NAME did not go"; C2_ID=""
  api_delete "/hub/$P1_ITEM"; P1_ITEM=""
  api_delete "/gateways/$P1_ID"; wait_gone "/gateways/$P1_ID" || die 9 "Gateway $P1_NAME did not go"; P1_ID=""
  code="$(api DELETE "/hub/$P_ITEM")"
  [ "$code" = 200 ] || die 9 "DELETE /hub/$P_ITEM (unpublish P) -> $code: $(cat "$WORK/body")"
  P_ITEM=""
  # 게시를 거두면 P 의 판이 바뀌지 않는다(소비자 집합은 이미 비었다). 재적용 틱 하나를 지나도 거절이 남는지 본다
  sleep "$REAPPLY_GAP"
  mcp_refuses 9 "$copy" "a copy of C's token after P was unpublished, at P" "$AUX_URL"
  pass 9 "C's bound token: refused at P1, admitted at P while C used P; after C dropped P the copy was refused after ${WAITED}s, and still after P was unpublished"
}

step_0
step_1
step_2
step_3
step_4
step_5
step_6
step_7
step_8
step_9
finish
