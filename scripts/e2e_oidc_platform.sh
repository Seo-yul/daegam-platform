#!/usr/bin/env bash
# shellcheck disable=SC2015  # "A && B || die" 는 A 나 B 가 실패하면 멈춘다는 뜻으로 쓴다
# P53b (D104) oidc 모드에서 데이터 플레인 발급자를 플랫폼으로 바꾼 클러스터 게이트. header 모드로 깐 개발 설치(make
# deploy-local, mock-oidc 파드, make lab-up 의 mock-soap, Traefik)에서 STEP 0 이 ConfigMap 을 oidc 와
# DAEGAM_DATA_PLANE_ISSUER=platform 으로 바꾸고 EXIT trap 이 모든 키를 되돌린다(scripts/lib/e2e_auth_mode.sh). 사람은
# mock-oidc 의 토큰으로 제어면에 들어오고, 최근 로그인은 mock-oidc 가 낸 ID 토큰의 auth_time 이다
# (docs/plan/phase-53b-one-issuer.md 8절의 게이트).
#
#   STEP 0  switch_auth_mode oidc DAEGAM_DATA_PLANE_ISSUER=platform DAEGAM_OAUTH_ALLOW_HTTP=true
#           DAEGAM_OIDC_CLIENT_ID=<UI 클라이언트> DAEGAM_TURN_TOKENS_ENABLED=true. 제어면을 굴린다
#   STEP 1  관리자(mock-oidc boot-admin, aud daegam-api)가 들어오고 재적용이 끝난다. /auth/config 가 data_plane_issuer
#           platform 과 깨어 있는 OAuth 서버를 말한다. 플랫폼을 믿는 동안 테넌트 T 의 mcp_group 을 다른 테넌트 X 의
#           고정 그룹으로 두면 422 이고(P53b 6.10절), 사용자 정의 그룹(oidcp-team)으로 둔다. Group Mapping 셋(T
#           viewer·developer·carol 의 viewer, X viewer)을 더하고, 스텁 provider 를 둔다
#   STEP 2  calculator MCP Server[Add] 를 노출해(앞단) 게시하고, 그것을 쓰는 Gateway 를 노출 배포한다. 규칙 Gateway 는
#           그룹 규칙(Add 는 IdP 그룹 oidcp-eng 만)으로 저장만 한다. daegam-oidc 가 플랫폼 발급자와 클러스터 안 JWKS 를
#           이름 대고 client id 가 없으며, vMCP 의 Cedar 가 daegam-T 와 이 Gateway 에 묶인 위임 permit 을 싣고 저장된
#           mcp_group(oidcp-team)을 싣지 않는다
#   STEP 3  IdP 토큰은 vMCP 와 앞단에서 401 이고 API 에서는 200 이다
#   STEP 4  사람 넷(alice: developer·oidcp-eng, bob: viewer, carol: viewer, xavier: X)이 API 를 불러 사람 기록을 만든다.
#           오래된 로그인(auth_time 한 시간 전)의 개인 토큰 발급은 403 reauth_required 이고, 새 로그인이면 201 이다.
#           alice 의 토큰은 iss 공개 주소, groups [daegam-T, daegam-group:T:oidcp-eng], tv 없음이다(저장된 mcp_group 과
#           X 의 그룹은 싣지 않는다). 관리자가 새 로그인으로 서비스 어카운트 둘(developer, viewer)을 만들고 그 토큰을 낸다
#           (sub sa:<id>, groups [daegam-T])
#   STEP 5  JWKS 캐시를 기다린 뒤 규칙 Gateway 를 배포하고 vMCP·앞단 파드를 다시 띄운다. 발급자 전환의 상태가
#           pending 0 이 되고 다시 띄운 vMCP 와 앞단을 확인된 검증기로 든다. alice 의 토큰은 Gateway·앞단에서 허용, xavier 의 X 토큰은 거부다
#   STEP 6  그룹 규칙: alice 허용, bob 거부. 역할 규칙(developer 이상)으로 바꿔 다시 배포하면 developer 서비스
#           어카운트 허용, viewer 서비스 어카운트 거부다
#   STEP 7  로그인 연결: 스크립트 클라이언트(deploy/dev/lab/mcp_oauth_client.py)가 발견·등록·인가하고, bob 이 IdP 토큰과
#           새 로그인의 ID 토큰으로 허용하고, 코드를 바꿔 Gateway 의 Add 를 부른다(7)
#   STEP 8  carol 의 Group Mapping 을 지우면 carol 의 개인 토큰이 곧바로 회수되고(membership), 다시 띄운 vMCP 가 그
#           토큰을 401 로 거절한다
#   STEP 9  Agent: 규칙 Gateway 의 규칙을 이 Agent 만으로 바꿔 다시 배포하고, 그 Gateway 를 도구 소스로 쓰는 Agent 를
#           노출 배포한다. alice 의 채팅 Add(3,4)=7 의 Gateway 감사 기록이 sub=alice, act=svc:agent:<k8s 이름> 이다.
#           실행을 켠 배포본이면 alice 의 지금 실행도 그렇다(실행은 사람의 이름으로 돈다)
#   STEP 9c (P46) 그 Agent 를 호출의 메시지를 사람이 쓴 것으로 다루게 허브에 게시하고 그것을 Tool 로 붙인 Agent 를 배포한다. alice 가 부르는 Agent 와 대화해
#           ask_* 로 Add(3,4)를 시키면 호출받은 Agent 의 실행이 alice 의 이름으로 돌고, 그 Add 의 Gateway 감사 기록이
#           sub=alice, act=svc:agent:<호출받은 Agent 의 k8s 이름> 이다. 끝나면 부르는 Agent 를 지우고 게시를 해제한다.
#           실행을 끈 배포본이면 조건부로 건너뛴다
#   STEP 10 노출 Agent 는 두 발급자를 알린 이미지로 돌고, 멤버 bob 의 플랫폼 토큰과 IdP 토큰을 모두 받는다
#   STEP 11 alice 의 일정을 만든 뒤 mongosh 로 alice 의 사람 기록을 긴 한도 밖으로 옮기면 접근 정리가 alice 의 개인
#           토큰과 로그인 연결을 회수하고(unconfirmed), 그 일정이 울리면 실행이 시작하며 일정을 멈춘다
#   STEP 12 F0 4번의 막대 구성(Gateway 셋, 앞단 하나, Gateway 소스 Agent 둘)을 채우려고 Gateway 하나와 그것을 쓰는
#           Agent 하나를 더 배포한다. Tenant Settings 에 agent_oauth(mock-oidc 의 agent-dev)를 두고 규칙 Gateway 의 규칙을
#           걷은 뒤 idp 로 되돌린다.
#           바꾼 때부터 5초마다 vMCP·앞단·노출 Agent·Agent 의 채팅을 불러 수렴까지의 초와 그 사이의 답을 찍는다(F0 4번,
#           막대 E2E_OIDCP_SWITCH_BAR 초, 기본 300). IdP 토큰이 Gateway 를 열고 플랫폼 토큰은 401 이며, OAuth 서버가 잠들고
#           bob 의 개인 토큰과 로그인 연결은 살아 있다
#   STEP 13 platform 으로 다시 바꿔 같은 것을 잰다. IdP 토큰은 401, bob 의 개인 토큰이 Gateway·앞단·노출 Agent 를 다시
#           열고 pending 이 0 이며, vMCP·앞단·노출 Agent 가 바꾼 뒤 확인된 검증기로 든다
#   정리    만든 것을 지우고 Group Mapping·Tenant Settings·스텁 provider 를 되돌린 뒤 모드를 되돌려 재적용을 기다린다
#
# make e2e-oidc-platform 이고 k3d 의 auth 조각에서 돈다(상한은 scripts/lib/e2e_select.py). macOS 에는 timeout 이 없으므로 기다림은 모두 폴링이다. 출력과 종료 코드는 lib/e2e_report.sh 의
# 계약이다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/e2e_json.sh"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
TENANT="${E2E_TENANT:-e2e-oidcp}"
OTHER="${E2E_OTHER_TENANT:-e2e-oidcp-x}"
T_GROUP="daegam-$TENANT"; X_GROUP="daegam-$OTHER"
API_PORT="${E2E_API_PORT:-$PORT_OIDC_PLATFORM_API}"
OIDC_PORT="${E2E_OIDC_PORT:-$PORT_OIDC_PLATFORM_OIDC}"
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_OIDC_PLATFORM_VMCP}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
OIDC="http://127.0.0.1:${OIDC_PORT}"
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
# UI 의 PKCE 클라이언트(DAEGAM_OIDC_CLIENT_ID). 최근 로그인의 ID 토큰은 이것을 audience 로 싣는다 (P53b 6.4절)
UI_CLIENT="daegam-ui-e2e"
# IdP 그룹. MEMBERS·DEVS·CAROL 은 Group Mapping 이 T 의 역할로, X_MEMBERS 는 X 의 viewer 로 잇고 ENG 는 규칙이 이름 댄다
G_MEMBERS="oidcp-members"; G_DEVS="oidcp-devs"; G_CAROL="oidcp-carol"; G_ENG="oidcp-eng"; G_X="oidcp-x"
ALICE="oidcp-alice"; BOB="oidcp-bob"; CAROL="oidcp-carol"; XAVIER="oidcp-xavier"; ADMIN_SUB="boot-admin"
BP_NAME="oidcp-calc"; MS_NAME="oidcp-ms"; GW_NAME="oidcp-gw"; RULES_NAME="oidcp-rules"; AG_NAME="oidcp-agent"
GW3_NAME="oidcp-gw3"; AG2_NAME="oidcp-agent2"; DS_NAME="oidcp-redis"
# STEP 9c 의 부르는 Agent 와 게시한 AG_NAME 이 캔버스에서 내는 Tool 이름(ask_ 뒤에 항목 이름의 - 를 _ 로 바꿔 붙인다)
CALLER_NAME="oidcp-caller"; ASK_TOOL="ask_oidcp_agent"
# 플랫폼을 믿는 동안 T 가 저장하는 사용자 정의 mcp_group. 렌더는 그것을 쓰지 않는다
CUSTOM_GROUP="oidcp-team"
MS_HOST="${E2E_OIDCP_MS_HOST:-oidcp-ms.e2e.local}"; GW_HOST="${E2E_OIDCP_GW_HOST:-oidcp-gw.e2e.local}"
AG_HOST="${E2E_OIDCP_AGENT_HOST:-oidcp-agent.e2e.local}"
STUB="stub-oidcp"
TOOL="Add"; TOOL_ARGS='{"a": 3, "b": 4}'; TOOL_EXPECT=7; TOKEN_DAYS=7
# 긴 한도 밖으로 옮기는 날 수. 기본 긴 한도(7일)보다 하루 더 앞이다
AGED_DAYS="${E2E_OIDCP_AGED_DAYS:-8}"
# 접근 정리(리컨실 틱)가 회수하고 멈추기까지의 한도
SWEEP_LIMIT="${E2E_OIDCP_SWEEP_LIMIT:-240}"
REDIRECT_URI="http://127.0.0.1/callback"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-oidc-platform.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                 # 관리자 Bearer 헤더. STEP 1 이 넣는다 (lib/e2e_api.sh 의 api 가 싣는다)
ADMIN_TOKEN=""; PF_API=""; PF_OIDC=""; PF_VMCP=""
NS=""; GW_URL=""; GW_PIN=""; MS_URL=""; MS_PIN=""; VMCP_SEL=""; FRONT_SEL=""
RULES_ID=""; AG_ID=""; DS_ID=""; HOME_ID=""; SA_DEV_ID=""; SA_VIEW_ID=""; SCHEDULE_ID=""; GW3_ID=""; AG2_ID=""; CALLER_ID=""
TS_CHANGED=0; TARGETS_MADE=0

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$OIDC_PORT" "$VMCP_PORT"
# roll_control_plane·platform_config·config_patches, switch_auth_mode·restore_auth_mode·wait_auth_reapplied·user_token
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_auth_mode.sh"
. "$HERE/lib/e2e_report.sh"
# as_person 은 여기에 있다
. "$HERE/lib/e2e_api.sh"
. "$HERE/lib/e2e_calc.sh"
# lab Redis 를 Database 로 등록하는 lab_database 는 여기에 있다 (STEP 9 의 Agent 의 memory)
. "$HERE/lib/e2e_lab.sh"
. "$HERE/lib/e2e_sse.sh"
. "$HERE/lib/e2e_group_mappings.sh"
. "$HERE/lib/e2e_turn_tokens.sh"
# issue·issue_ok·JWKS_CACHE_WAIT 와 sa_create·sa_issue·sa_claims_ok
. "$HERE/lib/e2e_personal_tokens.sh"
. "$HERE/lib/e2e_service_accounts.sh"
. "$HERE/lib/e2e_runs.sh"
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-oidc-platform"
. "$HERE/lib/mcp_rpc.sh"
. "$HERE/lib/e2e_mcp_access.sh"
OAUTH_CLIENT=(uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/mcp_oauth_client.py")
RESOLVE=()

teardown() {
  [ ${#AUTH[@]} -gt 0 ] || return 0
  [ -z "$SCHEDULE_ID" ] || [ -z "$AG_ID" ] || api_delete "/agents/$AG_ID/schedules/$SCHEDULE_ID"
  # 부르는 Agent 를 먼저 지운다. AG_ID 를 지우면 그 게시 항목도 지워진다
  [ -z "$CALLER_ID" ] || api_delete "/agents/$CALLER_ID"
  [ -z "$AG_ID" ] || api_delete "/agents/$AG_ID"
  [ -z "$AG2_ID" ] || api_delete "/agents/$AG2_ID"
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  [ -z "$GW3_ID" ] || api_delete "/gateways/$GW3_ID"
  [ -z "$RULES_ID" ] || api_delete "/gateways/$RULES_ID"
  [ "$TARGETS_MADE" = 0 ] || calc_teardown
  [ -z "$SA_DEV_ID" ] || api_delete "/tenants/$TENANT/service-accounts/$SA_DEV_ID"
  [ -z "$SA_VIEW_ID" ] || api_delete "/tenants/$TENANT/service-accounts/$SA_VIEW_ID"
  group_mappings_restore
  if [ "$TS_CHANGED" = 1 ]; then
    local ts='{"mcp_group": null, "agent_oauth": null, "agent_client_ids": []}'
    [ "$(api PATCH "/tenants/$TENANT/settings" "$ts")" = 200 ] \
      || echo "!! Tenant Settings $TENANT were not cleared: PATCH $API/tenants/$TENANT/settings $ts"
    [ -z "$NS" ] || "${K[@]}" -n "$NS" delete secret oidcp-agent-oauth --ignore-not-found >/dev/null 2>&1 || true
  fi
  drop_stub_provider "$STUB" || true
}

cleanup() {
  teardown
  # 되돌린 뒤 재적용을 기다리는 호출이 API port-forward 로 가므로 그것을 닫기 전에 되돌린다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  stop_pf "$PF_OIDC" "$OIDC_PORT"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# idp_token SUB GROUPS_JSON [AUD] -> mock-oidc 가 서명한 IdP 액세스 토큰. AUD 의 기본은 제어면(daegam-api)이다
idp_token() { user_token "{\"sub\": \"$1\", \"groups\": $2, \"aud\": \"${3:-daegam-api}\"}"; }

# fresh_id SUB [AUTH_OFFSET] -> UI 클라이언트를 audience 로 실은 SUB 의 ID 토큰. auth_time 은 지금에서 AUTH_OFFSET 초(기본 0)
# 옮긴 때다
fresh_id() { id_token "$1" "$UI_CLIENT" "${2:-0}"; }

# claims_ok STEP TOKEN EXPR -> 토큰 클레임 c 에서 파이썬 식 EXPR 이 참이 아니면 클레임을 찍고 die
claims_ok() {
  local c
  c="$(jwt_json "$2" 1)"
  [ "$(python3 -c 'import json, sys; c = json.loads(sys.argv[1]); print(bool(eval(sys.argv[2])))' "$c" "$3")" = True ] \
    || die "$1" "token claims $c do not hold: $3"
}

# revoked COLLECTION ID -> 그 문서가 회수됐으면 "<rc1 이 아는 까닭> <새 까닭>" 을 찍는다(새 까닭이 없으면 None).
# 회수되지 않았으면 빈 줄이다. 토큰은 revoke_reason·revoke_cause, 연결은 revoked_reason·revoked_cause 다
revoked() {
  local fields='{revoked_at: 1, revoke_reason: 1, revoke_cause: 1, revoked_reason: 1, revoked_cause: 1}'
  mongo_doc "$1" "$2" "$fields" 2>/dev/null | python3 -c 'import json, sys
d = json.loads(sys.stdin.read() or "null") or {}
if d.get("revoked_at"):
    print(d.get("revoke_reason") or d.get("revoked_reason"), d.get("revoke_cause") or d.get("revoked_cause"))' || true
}

# wait_revoked STEP COLLECTION ID WANT -> revoked 가 WANT 일 때까지 5초마다 SWEEP_LIMIT 초 동안 본다
wait_revoked() {
  local got="" i
  for ((i = 0; i < SWEEP_LIMIT / 5; i++)); do
    got="$(revoked "$2" "$3")"
    [ "$got" = "$4" ] && return 0
    sleep 5
  done
  die "$1" "$2 $3 is revoked as '$got', want '$4'"
}

# rules_body [RULE_JSON] -> 규칙 Gateway 의 저장 본문. Add 의 규칙이 RULE_JSON 이고, 비면 규칙이 없다
rules_body() {
  python3 -c 'import json, sys
rules = {"Add": json.loads(sys.argv[3])} if sys.argv[3] else {}
print(json.dumps({"name": sys.argv[1], "backends": [{"hub_item_id": sys.argv[2], "tools": ["Add"], "tool_rules": rules}]}))' \
    "$RULES_NAME" "$CALC_HUB_ID" "${1:-}"
}
# rules_put STEP RULE_JSON -> 규칙 Gateway 의 Add 규칙을 RULE_JSON 으로 저장한다(배포는 하지 않는다)
rules_put() {
  [ "$(api PUT "/gateways/$RULES_ID" "$(rules_body "$2")")" = 200 ] || die "$1" "PUT /gateways/$RULES_ID with rule $2 -> $(cat "$WORK/body")"
}

# rules_vmcp STEP -> 규칙 Gateway 의 vMCP 에 port-forward 한다. RULES_URL·RULES_PIN 을 남긴다
rules_vmcp() {
  local k8s ns
  [ "$(api GET "/gateways/$RULES_ID")" = 200 ] || die "$1" "GET /gateways/$RULES_ID failed"
  k8s="$(jq_ 'd["k8s"]["name"]')"; ns="$(jq_ 'd["k8s"]["namespace"]')"
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  start_pf "$ns" "svc/vmcp-$k8s" "${VMCP_PORT}:4483"; PF_VMCP=$PF_PID
  wait_port "$VMCP_PORT" || die "$1" "port-forward to svc/vmcp-$k8s did not open :$VMCP_PORT"
  RULES_URL="http://127.0.0.1:${VMCP_PORT}/mcp"; RULES_PIN="127.0.0.1:${VMCP_PORT}:127.0.0.1"
}

# switch_pending -> GET /settings/data-plane-auth 의 data_plane_issuer.pending 을 찍는다(없으면 빈 줄)
switch_pending() {
  [ "$(api GET /settings/data-plane-auth)" = 200 ] || return 1
  jq_ "(d.get('data_plane_issuer') or {}).get('pending', '')"
}

# wait_switched STEP -> 발급자 전환의 pending 이 0 이 될 때까지 5초마다 본다(READY_TIMEOUT 초)
wait_switched() {
  local p="" i
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    p="$(switch_pending || true)"
    [ "$p" = 0 ] && return 0
    sleep 5
  done
  die "$1" "data_plane_issuer.pending is '$p' after ${READY_TIMEOUT}s: $(jq_ 'json.dumps(d.get("data_plane_issuer"))')"
}

# wait_verified STEP SINCE KIND/NAME... -> 발급자 전환의 상태가 그 검증기들을 모두 SINCE(클러스터 시각) 뒤에 확인된 것으로
# 들 때까지 5초마다 본다(READY_TIMEOUT 초). 확인은 파드가 바뀌면 다시 하므로 SINCE 뒤의 확인은 지금 파드의 것이다
wait_verified() {
  local step="$1" since="$2" ok="" i
  shift 2
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    if [ "$(api GET /settings/data-plane-auth)" = 200 ]; then
      ok="$(python3 - "$WORK/body" "$since" "$@" <<'PY'
import datetime as t, json, sys
d = json.load(open(sys.argv[1]))
since = t.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
seen = {
    f"{v['kind']}/{v['name']}"
    for c in (d.get("data_plane_issuer") or {}).get("clusters", [])
    for v in c.get("verified_verifiers", [])
    if t.datetime.fromisoformat(v["at"].replace("Z", "+00:00")) >= since
}
print(all(w in seen for w in sys.argv[3:]))
PY
)"
      [ "$ok" = True ] && return 0
    fi
    sleep 5
  done
  die "$step" "the switch status does not list $* as verified after $since: $(jq_ 'json.dumps((d.get("data_plane_issuer") or {}).get("clusters"))')"
}

# mongo_doc COLLECTION ID FIELDS_JS -> 플랫폼 MongoDB 의 그 문서에서 FIELDS 를 JSON 으로 찍는다
mongo_doc() {
  platform_mongo "print(JSON.stringify(db.$1.findOne({_id: $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2")}, $3)))"
}

echo ">> context=$CTX tenant T=$TENANT other X=$OTHER api=$API oidc=$OIDC"

# ---------- 0. oidc 와 플랫폼 발급자 (cleanup 이 되돌린다) ----------
switch_auth_mode oidc DAEGAM_DATA_PLANE_ISSUER=platform DAEGAM_OAUTH_ALLOW_HTTP=true \
  "DAEGAM_OIDC_CLIENT_ID=$UI_CLIENT" DAEGAM_TURN_TOKENS_ENABLED=true \
  || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to oidc with the platform issuer failed"
pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=oidc (was ${AUTH_MODE_PREV:-unset}), DAEGAM_DATA_PLANE_ISSUER=platform, DAEGAM_OAUTH_ALLOW_HTTP=true, DAEGAM_OIDC_CLIENT_ID=$UI_CLIENT, turn tokens on; control plane rolled"

# ---------- 1. 관리자, 재적용, 테넌트, 매핑 ----------
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward svc/daegam-platform did not open :$API_PORT"
start_pf "$PLATFORM_NS" svc/mock-oidc "${OIDC_PORT}:8080"; PF_OIDC=$PF_PID
wait_port "$OIDC_PORT" || die 1 "port-forward svc/mock-oidc did not open :$OIDC_PORT"
ADMIN_TOKEN="$(idp_token "$ADMIN_SUB" '["platform-admins"]')" || die 1 "admin token from $OIDC failed"
AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
[ "$(api GET /me)" = 200 ] && [ "$(jq_ 'd["is_platform_admin"]')" = True ] || die 1 "GET /me as $ADMIN_SUB -> $(cat "$WORK/body")"
wait_auth_reapplied "$ADMIN_TOKEN" || die 1 "data-plane auth was not re-applied after the switch"
[ "$(api GET /auth/config)" = 200 ] || die 1 "GET /auth/config failed"
[ "$(jq_ 'd["data_plane_issuer"], d["oauth"]["enabled"], d["oauth"]["dormant"]')" = "platform True False" ] \
  || die 1 "auth config: $(cat "$WORK/body") (want data_plane_issuer platform and an awake OAuth server)"
PUBLIC_URL="$(platform_config DAEGAM_PUBLIC_URL)" || die 1 "reading DAEGAM_PUBLIC_URL failed"
[ -n "$PUBLIC_URL" ] || die 1 "ConfigMap $AUTH_MODE_CONFIGMAP has no DAEGAM_PUBLIC_URL"
TS_CHANGED=1
code="$(api PATCH "/tenants/$TENANT/settings" "{\"mcp_group\": \"$X_GROUP\"}")"
[ "$code" = 422 ] && [ "$(jq_ "(d.get('detail') or {}).get('code')")" = invalid_value ] \
  || die 1 "PATCH /tenants/$TENANT/settings mcp_group=$X_GROUP -> $code $(cat "$WORK/body") (want 422 invalid_value)"
[ "$(api PATCH "/tenants/$TENANT/settings" "{\"mcp_group\": \"$CUSTOM_GROUP\"}")" = 200 ] \
  || die 1 "PATCH /tenants/$TENANT/settings mcp_group=$CUSTOM_GROUP -> $(cat "$WORK/body")"
code="$(api GET /group-mappings)"; [ "$code" = 200 ] || die 1 "GET /group-mappings -> $code"
cp "$WORK/body" "$WORK/mappings.orig.json"
MAPPINGS="$(python3 - "$WORK/mappings.orig.json" "$TENANT" "$OTHER" "$G_MEMBERS" "$G_DEVS" "$G_CAROL" "$G_X" <<'PY'
import json, sys
rows, t, x, members, devs, carol, gx = json.load(open(sys.argv[1])), *sys.argv[2:8]
added = [
    {"group": members, "tenant": t, "role": "viewer"},
    {"group": devs, "tenant": t, "role": "developer"},
    {"group": carol, "tenant": t, "role": "viewer"},
    {"group": gx, "tenant": x, "role": "viewer"},
]
keys = {(m["group"], m["tenant"]) for m in added}
print(json.dumps([{k: m[k] for k in ("group", "tenant", "role")} for m in rows if (m["group"], m["tenant"]) not in keys] + added))
PY
)" || die 1 "building the group mappings failed"
MAPPINGS_PUT=1
[ "$(api PUT /group-mappings "$MAPPINGS")" = 200 ] || die 1 "PUT /group-mappings -> $(cat "$WORK/body")"
ensure_stub_provider 1 "$STUB"
pass 1 "admin $ADMIN_SUB in; reapply settled; auth config says data_plane_issuer platform with an awake OAuth server; $TENANT mcp_group=$X_GROUP (another tenant's fixed group) -> 422, $CUSTOM_GROUP stored; mappings $G_MEMBERS/$G_DEVS/$G_CAROL -> $TENANT, $G_X -> $OTHER; stub provider $STUB"

# ---------- 2. 대상과 렌더 ----------
traefik_ip 2
TARGETS_MADE=1
calc_mcp_server 2 "$BP_NAME" "$MS_NAME" "$(exposure "$MS_HOST")"
calc_publish 2
NS="$CALC_NS"
api GET "/mcp-servers/$CALC_MS_ID" >/dev/null
MS_URL="$(jq_ 'd["status"]["external_url"]')"; MS_PIN="$(pin "$MS_URL" "$LB_IP")"
# 규칙 Gateway 는 저장만 한다. 개인 토큰은 규칙이 이름 댄 IdP 그룹을 싣고, 저장한 정의도 이름으로 센다 (P53b 6.6절)
create_fresh 2 /gateways "$RULES_NAME" "$(rules_body "{\"callers\": {\"groups\": [\"$G_ENG\"]}}")"
RULES_ID="$CREATED_ID"
calc_gateway 2 "$GW_NAME" "$(exposure "$GW_HOST")"
GW_URL="$(jq_ 'd["status"]["external_url"]')"; GW_PIN="$(pin "$GW_URL" "$LB_IP")"
VMCP_SEL="app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$CALC_GW_K8S_NAME"
FRONT_SEL="$(front_sel "$CALC_MS_K8S_NAME")"
"${K[@]}" -n "$NS" get mcpoidcconfig daegam-oidc -o json >"$WORK/oidc.json" || die 2 "MCPOIDCConfig daegam-oidc missing in $NS"
OIDC_NOTE="$(json_of "$WORK/oidc.json" 'd["spec"]["inline"].get("issuer"), d["spec"]["inline"].get("jwksUrl", "").endswith("/.well-known/jwks.json"), "clientId" in d["spec"]["inline"]')"
[ "$OIDC_NOTE" = "$PUBLIC_URL True False" ] || die 2 "daegam-oidc in $NS: $(json_of "$WORK/oidc.json" 'json.dumps(d["spec"]["inline"])') (want issuer $PUBLIC_URL, the in-cluster JWKS and no clientId)"
"${K[@]}" -n "$NS" get virtualmcpserver "$CALC_GW_K8S_NAME" -o json >"$WORK/vmcp.json"
ref="$(json_of "$WORK/vmcp.json" '(d["spec"]["incomingAuth"].get("authzConfigRef") or {}).get("name") or ""')"
if [ -n "$ref" ]; then
  "${K[@]}" -n "$NS" get mcpauthzconfig "$ref" -o json >"$WORK/authz.json"
  POLICIES="$(json_of "$WORK/authz.json" '"\n".join(d["spec"]["config"]["policies"])')"
else
  POLICIES="$(json_of "$WORK/vmcp.json" '"\n".join(d["spec"]["incomingAuth"]["authzConfig"]["inline"]["policies"])')"
fi
echo "$POLICIES" | grep -qF "THVGroup::\"$T_GROUP\"" || die 2 "vMCP policies lack $T_GROUP: $POLICIES"
if echo "$POLICIES" | grep -qF "THVGroup::\"$CUSTOM_GROUP\""; then die 2 "vMCP policies carry the stored mcp_group $CUSTOM_GROUP: $POLICIES"; fi
echo "$POLICIES" | grep -qF "claim_bound_to == \"gateway:$CALC_GW_ID\"" || die 2 "vMCP policies lack the delegation permit of gateway:$CALC_GW_ID: $POLICIES"
pass 2 "front $MS_URL and Gateway $GW_URL deployed; rules Gateway $RULES_NAME saved; daegam-oidc names $PUBLIC_URL with the in-cluster JWKS and no clientId; the vMCP admits $T_GROUP (not the stored $CUSTOM_GROUP) and binds delegation tokens to gateway:$CALC_GW_ID"

# ---------- 3. IdP 토큰 ----------
IDP_DP="$(idp_token "$BOB" "[\"$T_GROUP\"]" daegam-mcp)" || die 3 "IdP data-plane token failed"
unauthenticated "$GW_URL" "$GW_PIN" "$IDP_DP" || die 3 "an IdP token was not refused with 401 at $GW_URL"
unauthenticated "$MS_URL" "$MS_PIN" "$IDP_DP" || die 3 "an IdP token was not refused with 401 at $MS_URL"
[ "$(api_as "$(idp_token "$BOB" "[\"$G_MEMBERS\"]")" GET /me)" = 200 ] || die 3 "an IdP token at the API -> $(cat "$WORK/body")"
pass 3 "an IdP token (aud daegam-mcp, groups [$T_GROUP]) gets 401 at $GW_URL and $MS_URL; an IdP token at the API gets 200"

# ---------- 4. 사람 기록, 최근 로그인, 개인 토큰, 서비스 어카운트 ----------
ALICE_CP="$(idp_token "$ALICE" "[\"$G_DEVS\", \"$G_ENG\"]")"
BOB_CP="$(idp_token "$BOB" "[\"$G_MEMBERS\"]")"
CAROL_CP="$(idp_token "$CAROL" "[\"$G_CAROL\"]")"
XAVIER_CP="$(idp_token "$XAVIER" "[\"$G_X\"]")"
for who in ALICE_CP BOB_CP CAROL_CP XAVIER_CP; do
  [ "$(api_as "${!who}" GET /me)" = 200 ] || die 4 "GET /me with $who -> $(cat "$WORK/body")"
done
code="$(issue "$ALICE_CP" "$TENANT" "oidcp stale" "" "$(fresh_id "$ALICE" -3600)")"
[ "$code" = 403 ] && [ "$(jq_ "(d.get('detail') or {}).get('code')")" = reauth_required ] \
  || die 4 "issuing after an hour-old sign-in -> $code $(cat "$WORK/body") (want 403 reauth_required)"
issue_ok 4 "$ALICE_CP" "$TENANT" "oidcp alice" "$(fresh_id "$ALICE")"; ALICE_PAT="$ISSUED"; ALICE_PAT_ID="$ISSUED_ID"
claims_ok 4 "$ALICE_PAT" "c['iss'] == '$PUBLIC_URL' and c['sub'] == '$ALICE' and sorted(c['groups']) == sorted(['$T_GROUP', 'daegam-group:$TENANT:$G_ENG']) and 'tv' not in c and '$X_GROUP' not in c['groups'] and '$CUSTOM_GROUP' not in c['groups']"
issue_ok 4 "$BOB_CP" "$TENANT" "oidcp bob" "$(fresh_id "$BOB")"; BOB_PAT="$ISSUED"; BOB_PAT_ID="$ISSUED_ID"
issue_ok 4 "$CAROL_CP" "$TENANT" "oidcp carol" "$(fresh_id "$CAROL")"; CAROL_PAT="$ISSUED"; CAROL_PAT_ID="$ISSUED_ID"
issue_ok 4 "$XAVIER_CP" "$OTHER" "oidcp xavier" "$(fresh_id "$XAVIER")"; XAVIER_PAT="$ISSUED"
claims_ok 4 "$XAVIER_PAT" "c['groups'] == ['$X_GROUP']"
sa_create 4 "$TENANT" oidcp-ci-dev developer; SA_DEV_ID="$SA_ID"
sa_create 4 "$TENANT" oidcp-ci-view viewer; SA_VIEW_ID="$SA_ID"
sa_issue 4 "$TENANT" "$SA_DEV_ID" "oidcp ci dev" id_token "$(fresh_id "$ADMIN_SUB")"; SA_DEV_TOK="$SA_TOKEN"
sa_issue 4 "$TENANT" "$SA_VIEW_ID" "oidcp ci view" id_token "$(fresh_id "$ADMIN_SUB")"; SA_VIEW_TOK="$SA_TOKEN"
sa_claims_ok 4 "$SA_DEV_TOK" "$SA_DEV_ID" "$T_GROUP"
pass 4 "person records for four people; an hour-old sign-in -> 403 reauth_required; a fresh sign-in issued tokens for alice (groups [$T_GROUP, daegam-group:$TENANT:$G_ENG], no tv, neither $X_GROUP nor the stored $CUSTOM_GROUP), bob, carol and xavier ($OTHER); service accounts oidcp-ci-dev and oidcp-ci-view got tokens (sub sa:<id>, groups [$T_GROUP])"

# ---------- 5. 검증기가 플랫폼 토큰을 받는다 ----------
# 발급한 슬롯이 플랫폼 레플리카의 JWKS 캐시에 오른 뒤 굴린다. 새 파드는 첫 요청에서 그 JWKS 를 받는다
sleep "$JWKS_CACHE_WAIT"
deploy_ready 5 /gateways "$RULES_ID" '{}'
RESTARTED_AT="$(cluster_now)"
restart_pods "$VMCP_SEL" "$FRONT_SEL" || die 5 "restarting the vMCP and front pods failed"
wait_switched 5
wait_verified 5 "$RESTARTED_AT" "VirtualMCPServer/$CALC_GW_K8S_NAME" "VirtualMCPServer/front-$CALC_MS_K8S_NAME"
allowed "$GW_URL" "$GW_PIN" "$ALICE_PAT" || die 5 "alice's token at $GW_URL: $MCP_NOTE"
allowed "$MS_URL" "$MS_PIN" "$ALICE_PAT" || die 5 "alice's token at $MS_URL: $MCP_NOTE"
denied "$GW_URL" "$GW_PIN" "$XAVIER_PAT" || die 5 "xavier's $OTHER token at $GW_URL was not denied: $MCP_NOTE"
pass 5 "data_plane_issuer.pending reached 0; alice's personal token calls Add at $GW_URL and $MS_URL; xavier's $OTHER token gets an empty list and 403"

# ---------- 6. 그룹 규칙과 역할 규칙 ----------
rules_vmcp 6
allowed "$RULES_URL" "$RULES_PIN" "$ALICE_PAT" || die 6 "alice (in $G_ENG) through the group rule: $MCP_NOTE"
denied "$RULES_URL" "$RULES_PIN" "$BOB_PAT" || die 6 "bob (not in $G_ENG) was not refused by the group rule: $MCP_NOTE"
rules_put 6 '{"callers": {"roles": ["developer"]}}'
[ "$(api POST "/gateways/$RULES_ID/redeploy" '{}')" = 200 ] || die 6 "redeploying $RULES_NAME with the role rule failed"
wait_ready "/gateways/$RULES_ID" 6
rules_vmcp 6
allowed "$RULES_URL" "$RULES_PIN" "$SA_DEV_TOK" || die 6 "the developer service account through the role rule: $MCP_NOTE"
denied "$RULES_URL" "$RULES_PIN" "$SA_VIEW_TOK" || die 6 "the viewer service account was not refused by the role rule: $MCP_NOTE"
pass 6 "group rule ($G_ENG): alice allowed, bob refused; role rule (developer and up): the developer service account allowed, the viewer one refused"

# ---------- 7. 로그인 연결 ----------
RESOLVE=(--resolve "$GW_PIN" --resolve "$(pin "$PUBLIC_URL" "$LB_IP")")
# oauth CONN COMMAND... -> 스크립트 클라이언트를 연결 CONN 의 상태 파일로 부른다. 요약은 $WORK/body 다
oauth() {
  local conn="$1"
  shift
  "${OAUTH_CLIENT[@]}" ${RESOLVE[@]+"${RESOLVE[@]}"} --state "$WORK/$conn.json" "$@" >"$WORK/body"
}
# connect STEP CONN SUB CONTROL_TOKEN -> 발견과 등록을 마친 base 에서 연결 CONN 을 열어 SUB 가 허용하고 코드를 바꾼다.
# 허용은 그 사람의 IdP 토큰과 새 로그인의 ID 토큰을 싣는다 (P53b 6.4절)
connect() {
  cp "$WORK/base.json" "$WORK/$2.json"
  oauth "$2" authorize && [ "$(jq_ 'd["status"], d["landing"]["request"]')" = "302 True" ] \
    || die "$1" "authorize ($2): $(cat "$WORK/body")"
  MCP_OAUTH_BEARER="$4" MCP_OAUTH_ID_TOKEN="$(fresh_id "$3")" oauth "$2" approve --user "$3" \
    && [ "$(jq_ 'd["status"]')" = 200 ] || die "$1" "approve ($2) as $3: $(cat "$WORK/body")"
  oauth "$2" token && [ "$(jq_ 'd["status"]')" = 200 ] || die "$1" "code exchange ($2): $(cat "$WORK/body")"
}
oauth base discover "$GW_URL" || die 7 "discover at $GW_URL: $(cat "$WORK/body")"
[ "$(jq_ "d['prm'].get('authorization_servers') == ['$PUBLIC_URL']")" = True ] || die 7 "PRM: $(cat "$WORK/body")"
oauth base register --redirect-uri "$REDIRECT_URI" && [ "$(jq_ 'd["status"]')" = 201 ] || die 7 "DCR: $(cat "$WORK/body")"
connect 7 bob "$BOB" "$BOB_CP"
oauth bob call --tool "$TOOL" --args "$TOOL_ARGS" && [ "$(jq_ "'$TOOL_EXPECT' in json.dumps(d['call']['content'])")" = True ] \
  || die 7 "bob's login connection at $GW_URL: $(cat "$WORK/body")"
connect 7 alice "$ALICE" "$ALICE_CP"
# 연결의 id 는 그 접근 토큰의 sid 다 (e2e_oauth 의 sid_of)
ALICE_GRANT="$(jwt_json "$(json_of "$WORK/alice.json" 'd.get("access_token") or ""')" 1 | json_of - 'd["sid"]')" \
  || die 7 "alice's connection has no sid"
pass 7 "login connections: discover, DCR, authorize; bob and alice approved with an IdP token and a fresh ID token; bob's access token calls Add at $GW_URL -> $TOOL_EXPECT"

# ---------- 8. Group Mapping 을 지우면 곧바로 회수된다 ----------
python3 -c 'import json, sys
print(json.dumps([m for m in json.loads(sys.argv[1]) if m["group"] != sys.argv[2]]))' "$MAPPINGS" "$G_CAROL" >"$WORK/mappings.nocarol.json"
[ "$(api PUT /group-mappings "$(cat "$WORK/mappings.nocarol.json")")" = 200 ] || die 8 "PUT /group-mappings without $G_CAROL failed"
platform_mongo_here || die 8 "the platform MongoDB pod mongodb-0 is not in $PLATFORM_NS"
wait_revoked 8 personal_tokens "$CAROL_PAT_ID" "membership None"
sleep "$JWKS_CACHE_WAIT"
restart_pods "$VMCP_SEL" || die 8 "restarting the vMCP pods failed"
unauthenticated "$GW_URL" "$GW_PIN" "$CAROL_PAT" || die 8 "the new vMCP pod still accepts carol's revoked token"
pass 8 "removing the mapping $G_CAROL revoked carol's personal token at once (membership); the restarted vMCP answers 401"

# ---------- 9. Agent 와 사람의 이름 ----------
code="$(api GET /agents/templates)"; [ "$code" = 200 ] || die 9 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
# 자동 실행은 memory 가 있는 Agent 만 받는다(409 runs_need_memory). lab Redis 를 체크포인터로 둔다
code="$(api GET /clusters)"; [ "$code" = 200 ] || die 9 "GET /clusters -> $code"
HOME_ID="$(find_id home)"; [ -n "$HOME_ID" ] || die 9 "no home cluster"
lab_database 9 redis "$DS_NAME" "$DS_NAME"
DS_ID="$DATA_SERVICE_ID"
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "발급자 전환 에이전트" \
  --provider "$STUB" --source "gateway_instance:$RULES_ID" --allow "$TOOL" --ds-id "$DS_ID"
save_doc 9 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
rules_put 9 "{\"callers\": {\"agents\": [\"$AG_ID\"]}}"
[ "$(api POST "/gateways/$RULES_ID/redeploy" '{}')" = 200 ] || die 9 "redeploying $RULES_NAME with the Agent rule failed"
wait_ready "/gateways/$RULES_ID" 9
AG_EXPOSE="{\"exposure\": {\"enabled\": true, \"host\": \"$AG_HOST\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"none\"}, \"cors_origins\": [], \"allowed_groups\": []}}"
deploy_ready 9 /agents "$AG_ID" "$AG_EXPOSE"
AG_K8S="$(jq_ 'd["k8s"]["name"]')"; AG_CLUSTER="$(jq_ 'd["cluster_id"]')"; AG_NS="$(jq_ 'd["k8s"]["namespace"]')"
# 첫 배포는 이미지의 기록이 없어 IdP 하나로 렌더되고, 파드의 init 이 two_issuers 를 알린 뒤의 재적용이 두 발급자로 다시
# 렌더해 파드를 굴린다. 그 교체가 끝난 뒤에 부른다. 아니면 채팅과 실행이 교체 중인 파드에 걸리고 STEP 10 이 옛 렌더를 본다
idp_env=""
for ((i = 0; i < 48; i++)); do
  idp_env="$("${K[@]}" -n "$AG_NS" get deploy "$AG_K8S" -o jsonpath='{..env[?(@.name=="DAEGAM_IDP_ISSUER")].value}' 2>/dev/null || true)"
  [ -n "$idp_env" ] && break; sleep 5
done
[ -n "$idp_env" ] || die 9 "the reapply did not render $AG_NS/$AG_K8S with the IdP beside the platform (no DAEGAM_IDP_ISSUER) within 240s"
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout=180s >/dev/null || die 9 "the two-issuer rollout of $AG_NS/$AG_K8S did not finish"
wait_gateway_gate 9 "$AG_CLUSTER"
BEFORE="$(cluster_now)"
code="$(as_person "$ALICE_CP" chat "/agents/$AG_ID/chat" "$(msg "$(stub_call "$TOOL" "$TOOL_ARGS")")")"
AFTER="$(cluster_now)"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || die 9 "alice's chat -> $code $(cat "$WORK/sse")"
sse_ 'json.dumps(first("tool_result"))' | grep -q "$TOOL_EXPECT" || die 9 "alice's Add without $TOOL_EXPECT: $(cat "$WORK/sse")"
audit_expect 9 "$BEFORE" "chat|$BEFORE|$AFTER|$RULES_ID|$TOOL|$ALICE|svc:agent:$AG_K8S|1"
RUN_NOTE="runs are off in this install"
if runs_on; then
  RUN_BEFORE="$(cluster_now)"
  as_person "$ALICE_CP" run_now 9 "$AG_ID" "$(stub_call "$TOOL" "$TOOL_ARGS")"
  run_wait 9 "$AG_ID" "$RUN_ID" "$RUN_TERMINAL"
  [ "$RUN_STATUS" = succeeded ] || die 9 "alice's run ended $RUN_STATUS: $(cat "$WORK/run.json")"
  RUN_AFTER="$(cluster_now)"
  audit_expect 9 "$RUN_BEFORE" "run|$RUN_BEFORE|$RUN_AFTER|$RULES_ID|$TOOL|$ALICE|svc:agent:$AG_K8S|1"
  RUN_NOTE="alice's run $RUN_ID succeeded and its Add carried sub=$ALICE, act=svc:agent:$AG_K8S"
else
  cond_skip 9r "$RUN_NOTE"
fi
pass 9 "rules Gateway admits Agent $AG_ID only; alice's chat Add=$TOOL_EXPECT carried sub=$ALICE, act=svc:agent:$AG_K8S; $RUN_NOTE"

# ---------- 9c. 다른 Agent 가 Tool 로 부른 호출의 사람 (P46) ----------
# 호출 하나는 호출받은 Agent 의 실행이다. 실행을 끈 배포본에는 호출도 없다
if runs_on; then
  code="$(api POST "/agents/$AG_ID/publish" '{"description": "Adds two numbers.", "calls_trusted": true}')"
  case "$code" in 200 | 201) AG_ITEM="$(jq_ 'd["id"]')" ;; *) die 9c "publishing $AG_NAME -> $code" ;; esac
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/caller.json" --name "$CALLER_NAME" --display "발급자 전환 호출 에이전트" \
    --provider "$STUB" --agent-source "$AG_ITEM:$ASK_TOOL"
  save_doc 9c /agents "$CALLER_NAME" "$(cat "$WORK/caller.json")"
  CALLER_ID="$SAVED_ID"
  deploy_ready 9c /agents "$CALLER_ID" '{}'
  require_features 9c agent_tools
  CALL_BEFORE="$(cluster_now)"
  code="$(as_person "$ALICE_CP" chat "/agents/$CALLER_ID/chat" "$(msg "$(ask_call "$ASK_TOOL" "$(stub_call "$TOOL" "$TOOL_ARGS")")")")"
  CALL_AFTER="$(cluster_now)"
  [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || die 9c "alice's chat with $CALLER_NAME -> $code $(cat "$WORK/sse")"
  [ "$(sse_ '(lambda r: (r["name"], r["status"]))(first("tool_result"))')" = "$ASK_TOOL success" ] \
    || die 9c "$ASK_TOOL did not return the answer of $AG_NAME: $(cat "$WORK/sse")"
  call_run 9c "$AG_ID" "$(sse_ 'first("done")["session_id"]')"
  CALL_RUN="$RUN_ID"
  run_wait 9c "$AG_ID" "$CALL_RUN" "$RUN_TERMINAL"
  [ "$RUN_STATUS $(run_field 'd["source"], d["acts_as"]["sub"], d["caller"]["agent_id"]')" = "succeeded agent $ALICE $CALLER_ID" ] \
    || die 9c "the called run $CALL_RUN: $(cat "$WORK/run.json") (want succeeded, source agent, as $ALICE, caller $CALLER_ID)"
  audit_expect 9c "$CALL_BEFORE" "call|$CALL_BEFORE|$CALL_AFTER|$RULES_ID|$TOOL|$ALICE|svc:agent:$AG_K8S|1"
  # 뒤 단계가 그 Agent 를 그대로 쓰게 부르는 Agent 를 지우고 게시를 해제한다
  api_delete "/agents/$CALLER_ID"
  wait_gone "/agents/$CALLER_ID" 120 || die 9c "Agent $CALLER_NAME ($CALLER_ID) was not deleted"
  CALLER_ID=""
  [ "$(api DELETE "/agents/$AG_ID/publish")" = 200 ] || die 9c "unpublishing $AG_NAME failed"
  pass 9c "$CALLER_NAME called published $AG_NAME for alice; run $CALL_RUN ran as $ALICE and its Add carried sub=$ALICE, act=svc:agent:$AG_K8S"
else
  cond_skip 9c "$RUN_NOTE, and a call is a run"
fi

# ---------- 10. 노출 Agent 의 두 발급자 ----------
[ "$(api GET "/agents/$AG_ID")" = 200 ] || die 10 "GET /agents/$AG_ID failed"
[ "$(jq_ "'two_issuers' in ((d.get('status') or {}).get('runtime') or {}).get('features', [])")" = True ] \
  || die 10 "the Agent's runtime does not report two_issuers: $(jq_ 'json.dumps((d.get("status") or {}).get("runtime"))')"
ext_sessions() { # TOKEN -> 노출 host 의 GET /sessions 상태 코드
  curl -sS -o "$WORK/ext.json" -w '%{http_code}' --resolve "$AG_HOST:80:$LB_IP" --max-time 30 \
    -H "Authorization: Bearer $1" "http://$AG_HOST/sessions?limit=1" 2>/dev/null || echo 000
}
code=""
for ((i = 0; i < 24; i++)); do
  code="$(ext_sessions "$BOB_PAT")"; [ "$code" = 200 ] && break; sleep 5
done
[ "$code" = 200 ] || die 10 "bob's platform token at http://$AG_HOST/sessions -> $code $(cat "$WORK/ext.json")"
code="$(ext_sessions "$IDP_DP")"
[ "$code" = 200 ] || die 10 "an IdP token at http://$AG_HOST/sessions -> $code $(cat "$WORK/ext.json")"
pass 10 "the exposed Agent reports two_issuers and admits bob's platform token and an IdP token"

# ---------- 11. 긴 한도 밖의 사람 ----------
code="$(api_as "$ALICE_CP" POST "/agents/$AG_ID/schedules" "$(schedule_body '0 9 * * *' 'oidcp aged')")"
[ "$code" = 201 ] || die 11 "alice's schedule -> $code $(cat "$WORK/body")"
SCHEDULE_ID="$(jq_ 'd["id"]')"
platform_mongo_here || die 11 "the platform MongoDB pod mongodb-0 is not in $PLATFORM_NS"
aged="$(platform_mongo "print(db.idp_people.updateOne({_id: '$ALICE'}, {\$set: {confirmed_at: new Date(Date.now() - $AGED_DAYS * 86400 * 1000)}}).matchedCount)")"
[ "$aged" = 1 ] || die 11 "aging alice's person record matched $aged documents"
# 회수는 접근 정리(리컨실 틱)가 한다. 일정은 그 실행이 시작할 때 멈추므로 다음 발사 틱이 쏘게 당긴다 (P53b 6.3절)
wait_revoked 11 personal_tokens "$ALICE_PAT_ID" "membership unconfirmed"
wait_revoked 11 oauth_grants "$ALICE_GRANT" "membership unconfirmed"
run_schedule_due "$SCHEDULE_ID" || die 11 "moving alice's schedule $SCHEDULE_ID to now failed"
paused=""
for ((i = 0; i < SWEEP_LIMIT / 10; i++)); do
  [ "$(api GET "/agents/$AG_ID/schedules")" = 200 ] \
    && paused="$(jq_ "next((f\"{s['paused']} {s['paused_reason']} {s['paused_cause']}\" for s in d['schedules'] if s['id'] == '$SCHEDULE_ID'), '')")"
  [ "$paused" = "True acts_as_lost_access unconfirmed" ] && break
  sleep 10
done
[ "$paused" = "True acts_as_lost_access unconfirmed" ] || die 11 "alice's schedule after aging her record: '$paused'"
pass 11 "aging alice's person record by $AGED_DAYS days revoked her personal token and login connection (unconfirmed) and paused her schedule when it fired"

# ---------- 12·13. 되돌리고 다시 바꾼다 (F0 4번의 시간 재기) ----------
# 두 방향마다 ConfigMap 을 바꾼 때부터 5초마다 한 번씩 본다. vMCP(Gateway)에서 IdP 토큰과 bob 의 개인 토큰, 노출 앞단과
# 노출 Agent 에서 개인 토큰, Gateway 소스를 쓰는 Agent 의 채팅(관리자, Add), 발급자 전환의 pending 이다. 모두가 그 방향의
# 기대값이 되면 수렴이고 그때까지의 초와 그 사이의 답을 찍는다. 수렴은 SWITCH_BAR 초 안이어야 한다(F0 4번의 막대).
# 채팅이 실패한 첫 때와 마지막 때도 찍는다(Agent 의 Gateway 호출이 끊긴 시간). 앞단과 vMCP 는 오퍼레이터가 굴리고,
# T76 의 검증기는 JWKS 치유가 다시 띄운다. 이 단계는 파드를 손으로 굴리지 않는다
SWITCH_BAR="${E2E_OIDCP_SWITCH_BAR:-300}"
mcp_once() { # URL PIN TOKEN -> list(Add 가 보인다)·empty·401·그 밖의 코드
  local code
  code="$(mcp_ingress "$1" "$2" tools/list "" "$3")"
  if [ "$code" = 200 ]; then
    [ "$(rpc_ "any(t['name'].endswith('$TOOL') for t in ((d or {}).get('result') or {}).get('tools', []))")" = True ] \
      && echo list || echo empty
  else
    echo "$code"
  fi
}
chat_once() { # -> ok 이면 관리자의 채팅이 Add 결과 7 을 받았다
  local code
  code="$(CHAT_MAX_TIME=60 SSE_OUT="$WORK/timing.sse" chat "/agents/$AG_ID/chat" "$(msg "$(stub_call "$TOOL" "$TOOL_ARGS")")")"
  if [ "$code" = 200 ] && sse_ 'json.dumps(first("tool_result"))' "$WORK/timing.sse" 2>/dev/null | grep -q "$TOOL_EXPECT"; then
    echo ok
  else
    echo "fail:$code"
  fi
}
# 수렴하지 못했을 때 정리 앞에 남긴다. 정리가 파드를 지우므로 워크플로의 진단 단계는 이것을 보지 못한다. 테넌트
# 네임스페이스의 파드와, 준비되지 않은 Agent 파드마다 init 이 받은 Tool 소스의 인증 종류, init 과 메인 컨테이너가 끝난
# 까닭과 마지막 로그다. 값(토큰, 비밀)은 찍지 않는다
switch_report() {
  local pod
  echo "--- pods in $AG_NS (switch did not converge)"
  "${K[@]}" -n "$AG_NS" get pods -o wide 2>&1 || true
  for pod in $("${K[@]}" -n "$AG_NS" get pods -l daegam.devops.ai.kr/resource=agent-instance \
    -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==false)]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
    echo "--- $AG_NS/$pod"
    "${K[@]}" -n "$AG_NS" get pod "$pod" -o jsonpath='{range .status.initContainerStatuses[*]}init {.name} restarts={.restartCount} {.lastState.terminated.message}{"\n"}{end}{range .status.containerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} {.lastState.terminated.message}{"\n"}{end}' 2>&1 || true
    "${K[@]}" -n "$AG_NS" get pod "$pod" -o jsonpath='{range .spec.initContainers[*].env[?(@.name=="DAEGAM_AGENT_CONFIG_URL")]}init url {.value}{"\n"}{end}' 2>&1 || true
    "${K[@]}" -n "$AG_NS" logs "$pod" -c agent --previous --tail=40 2>&1 || "${K[@]}" -n "$AG_NS" logs "$pod" -c agent --tail=40 2>&1 || true
  done
}

# timed_switch STEP VALUE WANT -> 발급자를 VALUE 로 바꾸고 수렴까지 잰다. WANT 는 "idp_gw pat_gw pat_front pat_agent chat"
# 의 기대값이다. 수렴의 초는 SWITCH_SECONDS, 채팅이 끊긴 초는 CHAT_DOWN 에 남는다
timed_switch() {
  local step="$1" value="$2" want="$3" t0 now got pending first_fail="" last_fail="" line
  : >"$WORK/timeline-$value"
  "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge \
    -p "{\"data\": {\"DAEGAM_DATA_PLANE_ISSUER\": \"$value\"}}" >/dev/null || die "$step" "patching DAEGAM_DATA_PLANE_ISSUER=$value failed"
  t0=$(date +%s)
  "${K[@]}" -n "$PLATFORM_NS" rollout restart deploy/daegam-platform >/dev/null || die "$step" "rolling the platform failed"
  SWITCH_SECONDS=""
  while :; do
    now=$(($(date +%s) - t0))
    [ "$now" -le $((SWITCH_BAR + 120)) ] || break
    wait_http "$BASE/healthz" 3 >/dev/null 2>&1 || { echo "$now api-down" >>"$WORK/timeline-$value"; sleep 5; continue; }
    got="$(mcp_once "$GW_URL" "$GW_PIN" "$IDP_DP") $(mcp_once "$GW_URL" "$GW_PIN" "$BOB_PAT") $(mcp_once "$MS_URL" "$MS_PIN" "$BOB_PAT")"
    got="$got $(ext_sessions "$BOB_PAT") $(chat_once)"
    pending="$(switch_pending 2>/dev/null || echo '?')"
    line="$now $got pending=$pending"
    echo "$line" >>"$WORK/timeline-$value"
    case "$got" in *fail:*) first_fail="${first_fail:-$now}"; last_fail="$now" ;; esac
    if [ "$got" = "$want" ] && { [ "$pending" = 0 ] || [ "$value" = idp ]; }; then SWITCH_SECONDS="$now"; break; fi
    sleep 5
  done
  echo "  timeline ($value; seconds idp_gw pat_gw pat_front pat_agent chat pending):"
  sed 's/^/    /' "$WORK/timeline-$value"
  CHAT_DOWN="${first_fail:+${first_fail}s..${last_fail}s}"
  [ -n "$SWITCH_SECONDS" ] || { switch_report; die "$step" "switching to $value did not converge to '$want' within $((SWITCH_BAR + 120))s"; }
  [ "$SWITCH_SECONDS" -le "$SWITCH_BAR" ] || die "$step" "switching to $value converged after ${SWITCH_SECONDS}s, over the bar of ${SWITCH_BAR}s"
}

# F0 4번의 막대 구성(Gateway 셋, 앞단 하나, Gateway 소스 Agent 둘)을 채운다. 더하는 Agent 는 노출하지 않는다
save_doc 12 /gateways "$GW3_NAME" "{\"name\": \"$GW3_NAME\", \"backends\": [{\"hub_item_id\": \"$CALC_HUB_ID\", \"tools\": [\"Add\"]}]}"
GW3_ID="$SAVED_ID"
deploy_ready 12 /gateways "$GW3_ID" '{}'
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent2.json" --name "$AG2_NAME" --display "oidcp agent 2" \
  --provider "$STUB" --source "gateway_instance:$GW3_ID" --allow "$TOOL"
save_doc 12 /agents "$AG2_NAME" "$(cat "$WORK/agent2.json")"
AG2_ID="$SAVED_ID"
deploy_ready 12 /agents "$AG2_ID" '{}'
# 되돌린 동안 Agent 는 Tenant Settings 의 client credentials 로 부른다(mock-oidc 의 agent-dev). 그 비밀은 테넌트
# 네임스페이스의 Secret 이다. 규칙 Gateway 의 규칙은 걷는다. idp 에서는 Gateway 규칙이 Agent 를 받지 않는다(6.9절)
"${K[@]}" -n "$NS" create secret generic oidcp-agent-oauth --from-literal=client_secret=agent-dev-secret \
  --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null || die 12 "creating Secret $NS/oidcp-agent-oauth failed"
OAUTH_BODY='{"mcp_group": null, "agent_client_ids": ["agent-dev"], "agent_oauth": {"client_id": "agent-dev", "secret_name": "oidcp-agent-oauth", "token_url": "http://mock-oidc.daegam-system.svc:8080/token", "scope": "mcp.invoke"}}'
[ "$(api PATCH "/tenants/$TENANT/settings" "$OAUTH_BODY")" = 200 ] || die 12 "PATCH Tenant Settings agent_oauth -> $(cat "$WORK/body")"
[ "$(api PUT "/gateways/$RULES_ID" "$(rules_body)")" = 200 ] || die 12 "clearing the rules of $RULES_NAME failed: $(cat "$WORK/body")"
[ "$(api POST "/gateways/$RULES_ID/redeploy" '{}')" = 200 ] || die 12 "redeploying $RULES_NAME without rules failed"
wait_ready "/gateways/$RULES_ID" 12
# Ready 는 오퍼레이터가 vMCP 를 교체하기 전에도 나온다. Tenant Settings 와 규칙을 잇달아 바꿔 렌더가 두 번 나가므로, 교체가
# 끝난 뒤에 부른다. 아니면 Agent 의 Add 가 지워지는 옛 파드에 걸려 MCPError 로 끝난다
wait_gateway_gate 12 "$AG_CLUSTER"
[ "$(chat_once)" = ok ] || die 12 "the Agent's chat before switching back: $(cat "$WORK/timing.sse")"
timed_switch 12 idp "list 401 401 401 ok"
wait_auth_reapplied "$ADMIN_TOKEN" "$(date -u +%Y-%m-%dT%H:%M:%S)" || die 12 "data-plane auth was not re-applied under idp"
IDP_SECONDS="$SWITCH_SECONDS"; IDP_CHAT_DOWN="${CHAT_DOWN:-none}"
[ "$(api GET /auth/config)" = 200 ] && [ "$(jq_ 'd["data_plane_issuer"], d["oauth"]["dormant"]')" = "idp True" ] \
  || die 12 "auth config after switching back: $(cat "$WORK/body")"
# 되돌려도 회수되지 않았는지를 본다. k3d 에서는 서명 키 슬롯이 STEP 0 에서 만들어져 그 20분의 데우기(ready_at)가 끝나기
# 전이라 상태가 pending 이다. pending 은 회수가 아니고 시각으로만 정해지므로 active 와 함께 받는다
[ "$(api_as "$BOB_CP" GET /me/tokens)" = 200 ] \
  && case "$(jq_ "next((t['status'] for t in d if t['token_id'] == '$BOB_PAT_ID'), '')")" in active|pending) true ;; *) false ;; esac \
  || die 12 "bob's personal token after switching back: $(cat "$WORK/body")"
[ "$(api_as "$BOB_CP" GET /me/oauth-grants)" = 200 ] && [ "$(jq_ "[g['status'] for g in d]")" = "['active']" ] \
  || die 12 "bob's login connection after switching back: $(cat "$WORK/body")"
pass 12 "switching to idp converged in ${IDP_SECONDS}s (bar ${SWITCH_BAR}s): IdP tokens open $GW_URL, platform tokens get 401 at the vMCP, the front and the exposed Agent, the Agent's chat calls Add again (down ${IDP_CHAT_DOWN}); the OAuth server is dormant; bob's personal token is not revoked and his login connection stays active"

SWITCHED_AT="$(cluster_now)"
timed_switch 13 platform "401 list list 200 ok"
wait_auth_reapplied "$ADMIN_TOKEN" "$(date -u +%Y-%m-%dT%H:%M:%S)" || die 13 "data-plane auth was not re-applied under platform"
wait_verified 13 "$SWITCHED_AT" "VirtualMCPServer/$CALC_GW_K8S_NAME" "VirtualMCPServer/front-$CALC_MS_K8S_NAME" "Agent/$AG_K8S"
pass 13 "switching to platform again converged in ${SWITCH_SECONDS}s (bar ${SWITCH_BAR}s, pending 0; the vMCP, the front and the exposed Agent verified after the switch): IdP tokens get 401 at $GW_URL, bob's personal token opens $GW_URL, the front and the exposed Agent again, the Agent's chat calls Add again (down ${CHAT_DOWN:-none})"

finish
