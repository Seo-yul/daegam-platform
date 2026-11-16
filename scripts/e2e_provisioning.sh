#!/usr/bin/env bash
# P44 (D103) 쓰지 않는 Agent 를 0 으로 줄이고 다음 턴에 깨우는 e2e 이고, P44b (D107) 의 미리 보기 단계(STEP 13~17)가 뒤에
# 붙는다. 로컬 클러스터(태그의 클러스터 e2e 잡이 만든 k3d 의 provisioning 조각, deploy-local·lab-up 이후)만 대상이다.
#
#   make e2e-provisioning                              게이트 전부. STEP 10 이 깨우기 E2E_PROVISIONING_WAKES 번(기본 10)의
#                                                      시간을 잰다. 0 이면 STEP 10 을 조건부로 건너뛴다
#   make e2e-provisioning E2E_PROVISIONING_TIMING=only STEP 0·1 과 시간 재기(STEP 10)만 돈다. F0.2 의 k3d 읽기다
#
# STEP 0 이 플랫폼 ConfigMap 을 local 모드(노출한 Agent 는 local·oidc 모드에서만 배포된다), 쉬는 창 120초, 매일 깨우기의
# hold 60초로 바꾸고 제어면을 굴린다. 매일 깨우기의 주기(DAEGAM_AGENT_HOUSEKEEPING_HOURS)와 플랫폼 스위치
# (DAEGAM_AGENT_SCALE_TO_ZERO)는 그 값을 기억해 두려고 기본값으로 함께 적고, STEP 11·12 가 바꾼다. EXIT trap 이 성공, 실패,
# 끊김 어느 쪽이든 만든 것을 지운 뒤 바꾼 키를 모두 되돌리고(창은 0 으로 돌아간다) 제어면을 굴린다. ConfigMap 을 고치고
# 모드를 바꾸므로 다른 게이트와 클러스터를 나누지 않는다(scripts/lib/e2e_select.py 의 provisioning 조각).
#
# Agent 다섯은 모두 스텁 모델이고 scale_to_zero 를 켜서 배포한다. A 는 lab Postgres memory 와 승인을 묻는 calculator Add,
# 세션 TTL 300초다(STEP 2 의 보류 세션이 잠든 뒤에 TTL 을 넘긴다). B 는 lab Redis memory 와 같은 Tool, 세션 TTL 60초다(STEP
# 11 의 보류 세션이 매일 깨우기 전에 TTL 을 넘긴다). 잠들 수 없는 셋은 memory 가 없는 N, Advanced 가 spec.replicas 를 정한 R,
# 노출한 X 다. 끝나야 하는 메시지는 모두 {"sleep": 0} 을 싣고, 조각이 없는 메시지는 스텁이 Add 를 불러 승인을 기다린다
# (agent-runtime/runtime/stub.py). 단계는 다음과 같다:
#   STEP 0   ConfigMap 이 header 모드이고 창이 0 이거나 없다(앞 실행이 남긴 값이면 바꾸기 전에 멈춘다). local 모드와 창·hold 를
#            넣고 제어면을 굴린다
#   STEP 1   관리자 로그인과 재적용, 스텁 provider, lab Postgres·Redis Database, calculator MCP Server, Agent 다섯이 Ready 다.
#            R 에 spec.replicas 덮어쓰기를 둔다. 상태의 sleep 이 창 120초이고 A·B 는 잠들 수 있으며 N·R·X 는
#            no_memory·replicas_override·exposed 다
#   STEP 2   A 에 보류 세션 하나를 열면 창이 지난 뒤 워커가 재운다. Deployment 가 0 이고 상태가 Sleeping, 까닭 idle, 보류 승인
#            1(잠들 때 남긴 수)이다
#   STEP 3   창 두 번 동안 5초마다 Approvals 탭의 두 목록(status=pending, 그리고 runs=only·all_users)과 상태를 읽으면 목록은
#            409 agent_asleep 이고 상태는 Sleeping 그대로이며 since 가 바뀌지 않는다. 끝에도 Deployment 가 0 이다
#   STEP 4   A 에 채팅하면 503 agent_waking 과 Retry-After 이고 상태는 Waking 이다. Retry-After 마다 다시 보내면 done 으로 끝난다
#   STEP 5   깬 직후 STEP 2 의 보류 세션(잠든 동안 TTL 이 지났다)에 결정하면 410 이다
#   STEP 6   1초마다 새 세션의 채팅을 보내는 동안 잠들기 라우트를 20번 부르면 채팅은 모두 done 이나 503 agent_waking 이고
#            turn_aborted 는 없다. 잠들기는 200 이거나 409(agent_busy·state_conflict·in_progress) 또는 502
#            runtime_unreachable 이고, 200 과 503 agent_waking 이 적어도 하나씩 있다
#   STEP 7   A 를 다시 재우고 파드가 다 사라진 뒤, 플랫폼 파드에서 A 의 Service 를 부르면 2초 안에 연결 거부로 실패한다(F0.4)
#   STEP 8   창 두 번 넘게 쉰 N·R·X 는 파드가 그대로이고 상태가 Ready·awake 와 제 까닭이며, 잠들기 라우트는 409
#            agent_sleep_ineligible 과 같은 까닭이다
#   STEP 9   잠든 A 에 spec.replicas 덮어쓰기를 저장하면 같은 저장에서 깨어(까닭 ineligible, replicas_override) 다시 Ready 다.
#            덮어쓰기를 지우면 다시 잠들 수 있다
#   STEP 10  B 를 E2E_PROVISIONING_WAKES 번 재우고 파드가 다 사라진 뒤 채팅을 보내, 보낸 때부터 agent_waking 의 다시 보내기를
#            지나 200 답의 첫 바이트까지를 잰다. p50 과 p95 를 찍는다(막대는 없다. F0.2 의 막대는 실제 설치에서 본다)
#   STEP 11  B 에 보류 세션 하나를 열고 바로 재운 뒤 매일 깨우기 주기를 0.05시간으로 두고 플랫폼을 굴리면, 워커가 B 를
#            까닭 housekeeping 으로 깨웠다가 hold 뒤에 다시 재운다. 다시 잠든 B 의 보류 승인은 0 이다(TTL 이 지난 그 세션을
#            깬 파드의 시작 정리가 닫았다)
#   STEP 12  A·B 를 재운 뒤 플랫폼 스위치를 끄고 굴리면 둘 다 깨어 Ready 이고 상태의 까닭이 setting_off 다
#   STEP 13  calculator 의 Add 에 A 를 이름 댄 Tool 규칙을 두고 가라앉힌 뒤, 관리자(테넌트의 developer 로 둔다. 위임은
#            소속 행이 있는 사람에게만 열린다)가 A 의 미리 보기를 띄우면 파드가 Ready 이고 미리 보기 아래 채팅이 done 이다. 그
#            id 로 부른 GET /agents/{id} 는 404 이고 Agent 목록에 없다. 토큰 Secret 의 calculator 토큰은 A 의 id 와 주체를 싣고
#            미리 보기의 끝 + 10분 안에 만료하며, calculator 프록시가 그 토큰을 받아 Add 를 부르게 한다. 규칙이 이름 대지 않은
#            Agent 의 토큰은 Tool 을 보지 못하고 부르지도 못한다
#   STEP 14  미리 보기에서 Add 를 부르면 승인을 기다리고, 그 결정(approve)이 Add 를 불러 결과 7 로 끝난다. Add 의 규칙은 A 만
#            이름 대므로 그 호출은 규칙이 원본으로서 받아들인 것이다
#   STEP 15  테넌트의 developer 인 다른 사람은 그 id 와 A 의 내 미리 보기에서 404 를 받고, 열린 미리 보기 목록에서 그것을 보고
#            끝낸다. 시작한 사람의 GET /preview 는 404 이고 까닭이 ended_by_other 이며 미리 보기의 객체가 사라진다
#   STEP 16  Add 의 규칙을 걷고 calculator 를 게시해 그 Gateway 를 띄운 뒤, A 에 Tool 소스가 그 Gateway 인 리비전을 저장하고
#            미리 보기를 띄운다. 토큰 Secret 의 gateway_token 은 A 의 id 와 주체를 싣고 끝 + 10분 안에 만료하며, vMCP 가 그
#            토큰을 받아 Add 를 부르게 한다
#   STEP 17  STEP 16 의 미리 보기의 마지막 활동과 첫 시작을 MongoDB 에서 31분 앞으로 당기면 끝내기 워커가 쓰지 않은 끝으로
#            끝낸다. 문서와 객체가 사라지고 GET /preview 의 까닭이 idle 이다. 30분을 기다리지 않으려고 시계 대신 기록을
#            당긴다. 플랫폼 MongoDB 가 클러스터 밖이면 조건부로 건너뛴다. 토큰이 끝 + 10분 뒤에 거절되는 것은 보지 않는다
#            (2시간 10분을 기다려야 한다). STEP 13 과 16 이 그 만료 시각을 보고, 거절은 플랫폼 단위 테스트의 몫이다
#   정리     Agent 다섯(노출한 X 를 header 로 되돌리기 전에 지운다. 원본을 지우면 그 미리 보기도 끝난다), Gateway 와
#            MCP Server, Database 둘, B 의 Redis 키와 인덱스, 넣은 스텁 provider, 다른 사람을 지우고 관리자의 소속을 되돌린 뒤 ConfigMap 과
#            Secret 을 되돌리고 제어면을 굴리고 재적용을 기다린다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다. macOS 에는 timeout 이 없어 기다림은
# 모두 폴링이다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/e2e_ports.sh"
# json_of 는 여기에 있다
. "$HERE/lib/e2e_json.sh"

[ "${E2E_TARGET:-orbstack}" = orbstack ] \
  || { echo "e2e-provisioning 은 로컬 클러스터만 대상이다 (got E2E_TARGET=$E2E_TARGET)"; exit 1; }
# lib/e2e_local_auth.sh 의 local_admin_session 이 로컬 클러스터 갈래를 이 이름으로 가른다
TARGET=orbstack
CTX="${KUBE_CONTEXT:-orbstack}"
TENANT="${E2E_TENANT:-prov}"
API_PORT="${E2E_API_PORT:-$PORT_PROVISIONING_API}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
WAKES="${E2E_PROVISIONING_WAKES:-10}"
TIMING="${E2E_PROVISIONING_TIMING:-}"
case "$TIMING" in '' | only) ;; *) echo "E2E_PROVISIONING_TIMING 은 비거나 only 다 (got $TIMING)"; exit 1 ;; esac
case "$WAKES" in '' | *[!0-9]*) echo "E2E_PROVISIONING_WAKES 는 0 이상의 정수다 (got $WAKES)"; exit 1 ;; esac
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"

# 게이트 동안의 설정 (P44 14절). 창은 하한인 120초, hold 는 하한인 60초다. 매일 깨우기의 주기는 STEP 11 만 짧게 둔다. 주기의
# 절반 안에서 Agent 마다 당기므로 짧은 주기를 처음부터 두면 STEP 3 의 두 창 동안 매일 깨우기가 A 를 깨울 수 있다
WINDOW=120
HOLD=60
HOUSEKEEPING_HOURS=0.05
# 워커는 30초마다 돈다. 창이 지난 뒤 한 틱과 드레인, 0 으로 줄이기의 패치에 여유를 더해 기다린다
SLEEP_WAIT=$((WINDOW + 120))
# 깨우기 한 번을 기다리는 한도. 플랫폼은 준비된 파드 없이 5분이 지난 깨우기를 awake 로 끝낸다(WAKE_LIMIT)
WAKE_WAIT=300
# 잠들기 라우트를 다시 부르는 한도. 깨우는 중(state_conflict)이나 턴이 리스를 쥔 동안(agent_busy) 다시 부른다
SLEEP_ROUTE_WAIT=300
# 매일 깨우기가 B 를 깨웠다 다시 재우기까지. 주기 180초, 한 틱, 파드 시작, hold, 한 틱과 드레인에 여유를 더했다
HOUSEKEEPING_WAIT=600
# 0 인 Agent 의 Service 를 부른 호출이 실패해야 하는 시간 (F0.4)
SERVICE_FAIL_MS=2000
# STEP 6 의 잠들기 라우트 횟수
SLEEP_CALLS=20

STUB_PROVIDER="stub-prov-e2e"
BP_NAME="calc-prov"; MS_NAME="calc-prov-e2e"
PG_DS_NAME="pg-prov-e2e"; RDS_NAME="redis-prov-e2e"
A_NAME="prov-a-e2e"; B_NAME="prov-b-e2e"; N_NAME="prov-nomem-e2e"; R_NAME="prov-replicas-e2e"; X_NAME="prov-exposed-e2e"
A_TTL=300; B_TTL=60
EXT_HOST="${E2E_PROVISIONING_HOST:-prov-e2e.e2e.local}"
CORS_ORIGIN="https://app.e2e.local"
# 끝나야 하는 메시지. 스텁은 sleep 조각이 있으면 Tool 없이 글로 답한다
DONE_MSG='{"sleep": 0} provisioning e2e'
# 승인을 기다리는 메시지. 조각이 없으면 스텁이 첫 MCP Tool(Add)을 부르고 승인 규칙이 묻는다
ASK_MSG="provisioning e2e asks for Add"
REPLICAS_PATCH='{"patch": {"spec": {"replicas": 1}}}'
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-provisioning.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=(); ADMIN_TOKEN=""
PF_API=""; STORM_PID=""
HOME_ID=""; PG_DS_ID=""; RDS_ID=""
A_ID=""; B_ID=""; N_ID=""; R_ID=""; X_ID=""
A_NS=""; A_K8S=""; B_NS=""; B_K8S=""
SP_SID=""; B_SIDS=""
WAKE_TRIES=0; WAKE_DEADLINE=0; WAKE_MS=0
# 미리 보기 (P44b). 다른 사람은 테넌트의 developer 다. 미리 보기 토큰은 끝 + 10분까지 산다(PREVIEW_SLACK)
DEV_USER="prov-preview-dev-e2e"; DEV_UID=""; DEV_TOKEN=""
PV_ID=""; PV_SID=""; TOKEN=""; TOKEN_SPARE=0
# 미리 보기의 토큰과 규칙을 calculator 프록시에 바로 보는 port-forward 다. 규칙이 이름 대지 않는 Agent 의 주체다
CALC_PORT="${E2E_CALC_PORT:-$PORT_PROVISIONING_CALC}"; PF_CALC=""
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_PROVISIONING_VMCP}"; PF_VMCP=""
GW_NAME="calc-prov-gw-e2e"
STRANGER_SUB="svc:agent:agent-e2e-unnamed"
PREVIEW_SLACK=600
# 끝내기 워커는 30초마다 돈다. 한 틱과 Agent 삭제에 여유를 더해 기다린다
PREVIEW_END_WAIT=180

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$CALC_PORT" "$VMCP_PORT"
# pass·skip·cond_skip·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·hdr·api_delete·wait_ready·wait_gone·save_doc·deploy_doc·error_code·sleep_route·ensure_stub_provider·remove_providers 는
# 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·now_ms 는 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab_database·rforget 은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# calc_mcp_server·calc_publish·calc_gateway·calc_teardown 은 여기에 있다
. "$HERE/lib/e2e_calc.sh"
# platform_config·restart_platform·agent_pods·AGENT_POD_SELECTOR·AUTH_MODE_CONFIGMAP 은 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# switch_auth_mode·restore_auth_mode 는 여기에 있다
. "$HERE/lib/e2e_auth_mode.sh"
# LOCAL_BOOT_PW·local_login_any·local_admin_session·local_fresh_member·local_set_member 는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# rules_save·rules_redeploy·rules_settled 는 여기에 있다 (Tool 규칙, P52)
. "$HERE/lib/e2e_tool_rules.sh"
# platform_mongo_here·platform_mongo·turn_member_developer·turn_member_restore 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# svc_token·mcp_admits·mcp_refuses 는 여기에 있다
MCP_CLIENT_NAME="e2e-provisioning"
. "$HERE/lib/mcp_rpc.sh"

cleanup() {
  if [ -n "$STORM_PID" ]; then
    touch "$WORK/storm.stop"
    kill "$STORM_PID" 2>/dev/null || true
  fi
  # 노출한 X 는 header 모드로 되돌리기 전에 지운다. 되돌린 뒤의 재적용은 header 모드에서 노출한 Agent 를 다시 적용하지
  # 못해 기다림이 끝나지 않는다. 나머지도 local 모드의 관리자 토큰이 있을 때 지운다
  local id
  for id in "$X_ID" "$A_ID" "$B_ID" "$N_ID" "$R_ID"; do
    [ -z "$id" ] || api_delete "/agents/$id"
  done
  if [ -n "$X_ID" ]; then
    wait_gone "/agents/$X_ID" 180 || echo "!! Agent $X_NAME ($X_ID) may be left in tenant $TENANT"
  fi
  calc_teardown
  [ -z "$RDS_ID" ] || api_delete "/data-services/$RDS_ID"
  [ -z "$PG_DS_ID" ] || api_delete "/data-services/$PG_DS_ID"
  # Agent 삭제는 Redis 키를 지우지 않는다(D33). 세션마다의 쓰기 레지스트리는 인스턴스 접두어 밖이라 세션 id 로 걷는다
  # shellcheck disable=SC2086  # 세션 id 여럿을 낱말로 나눠 넘긴다
  [ -z "$B_ID" ] || rforget "$B_ID" $B_SIDS
  [ "${STUB_PROVIDER_ADDED:-0}" = 0 ] || remove_providers "$STUB_PROVIDER"
  [ -z "$DEV_UID" ] || api_delete "/users/$DEV_UID"
  turn_member_restore || true
  stop_pf "$PF_CALC" "$CALC_PORT"
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  # 모드와 창·hold·주기·스위치를 STEP 0 앞의 값으로 되돌리고 제어면을 굴린 뒤 재적용을 기다린다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# --- 상태 읽기 -------------------------------------------------------------------------------------------------
# view ID EXPR -> GET /agents/ID 에서 파이썬 식 EXPR 을 찍는다. s 는 status, z 는 status.sleep 이다. 200 이 아니면 그 코드를
# 찍는다. 본문은 $WORK/body 다
view() {
  local code
  code="$(api GET "/agents/$1")"
  [ "$code" = 200 ] || { echo "GET /agents/$1 -> $code"; return 0; }
  python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("status") or {}
z = s.get("sleep") or {}
v = eval(sys.argv[2])
print(*v) if isinstance(v, tuple) else print(v)' "$WORK/body" "$2"
}
# wait_view STEP ID SECONDS EXPR WANT -> view 가 WANT 를 찍을 때까지 5초마다 본다. 넘기면 die STEP 이다
wait_view() {
  local got="" deadline=$((SECONDS + $3))
  while [ "$SECONDS" -lt "$deadline" ]; do
    got="$(view "$2" "$4")"
    [ "$got" = "$5" ] && return 0
    sleep 5
  done
  die "$1" "agent $2: '$got' after $3s, want '$5' ($4)"
}
# replicas NS NAME -> Deployment 의 "spec.replicas readyReplicas". 준비된 파드가 없으면 둘째는 0 이다
replicas() {
  "${K[@]}" -n "$1" get deploy "$2" -o jsonpath='{.spec.replicas} {.status.readyReplicas}' 2>/dev/null \
    | awk '{ print $1, ($2 == "" ? 0 : $2) }'
}
# wait_no_pods STEP NS ID -> 그 Agent 의 파드가 지워지는 중인 것까지 다 사라질 때까지 POD_SETTLE_SECONDS 초 동안 1초마다
# 본다. 상태가 Sleeping 이 된 직후에는 ReplicaSet 이 아직 파드에 삭제 표시를 하지 않았을 수 있어, 살아 있는 파드와 내려가는
# 파드가 함께 없어야 끝난다
wait_no_pods() {
  local left="" i
  for ((i = 0; i < POD_SETTLE_SECONDS; i++)); do
    left="$("${K[@]}" -n "$2" get pods -l "$AGENT_POD_SELECTOR$3" -o name 2>/dev/null || echo "?")"
    [ -z "$left" ] && return 0
    sleep 1
  done
  die "$1" "pods of agent $3 still there after ${POD_SETTLE_SECONDS}s: $left"
}

# --- 잠들기와 깨우기 (sleep_route·error_code 는 lib/e2e_api.sh 에 있다) ---------------------------------------
# waking_pause STEP CODE -> 채팅의 답(CODE, 본문 $WORK/sse, 머리 $WORK/hdr)이 503 agent_waking 이면 Retry-After 초를 쉬고
# WAKE_TRIES 를 센다. 아니거나 WAKE_DEADLINE 을 넘겼으면 die STEP 이다
waking_pause() {
  local retry
  if [ "$2" != 503 ] || [ "$(error_code "$WORK/sse")" != agent_waking ]; then
    cp "$WORK/sse" "$WORK/body"
    die "$1" "a chat while waking -> $2 $(error_code "$WORK/sse") (want 200, or 503 agent_waking)"
  fi
  retry="$(hdr Retry-After)"
  case "$retry" in '' | *[!0-9]*) die "$1" "503 agent_waking without a Retry-After of whole seconds: '$retry'" "$(cat "$WORK/sse")" ;; esac
  WAKE_TRIES=$((WAKE_TRIES + 1))
  [ "$SECONDS" -lt "$WAKE_DEADLINE" ] || die "$1" "the Agent did not wake within ${WAKE_WAIT}s ($WAKE_TRIES answers agent_waking)"
  sleep "$retry"
}
# chat_awake STEP ID TEXT -> 새 세션의 채팅을 200 이 될 때까지 보낸다. 503 agent_waking 이면 Retry-After 마다 다시 보낸다.
# 스트림은 $WORK/sse, 머리는 $WORK/hdr 이고 503 의 수가 WAKE_TRIES 다. 세션 id 를 CHAT_SID 에 남긴다
chat_awake() {
  local code body
  body="$(msg "$3")"
  WAKE_TRIES=0; WAKE_DEADLINE=$((SECONDS + WAKE_WAIT))
  while :; do
    code="$(chat "/agents/$2/chat" "$body" -D "$WORK/hdr")"
    [ "$code" = 200 ] && break
    waking_pause "$1" "$code"
  done
  CHAT_SID="$(hdr X-Daegam-Session-Id)"
  [ -n "$CHAT_SID" ] || die "$1" "no X-Daegam-Session-Id on the chat of agent $2" "$(cat "$WORK/hdr")"
}
# ended_done STEP LABEL -> $WORK/sse 가 done 으로 끝나야 한다
ended_done() {
  [ "$(sse_ 'names[-1] if names else ""')" = "done" ] \
    || { cp "$WORK/sse" "$WORK/body"; die "$1" "$2 ended with $(sse_ 'names'), want done"; }
}
# timed_wake STEP ID -> 새 세션의 끝나는 채팅을 보낸 때부터 agent_waking 의 다시 보내기를 지나 200 답의 첫 바이트까지를
# WAKE_MS 에 남긴다(ms). 스트림은 done 으로 끝나야 한다
timed_wake() {
  local t0 start out code first body
  body="$(msg "$DONE_MSG")"
  WAKE_TRIES=0; WAKE_DEADLINE=$((SECONDS + WAKE_WAIT))
  t0="$(now_ms)"
  while :; do
    start="$(now_ms)"
    # curl 은 마지막 -w 를 쓴다. chat 의 상태 코드 뒤에 첫 바이트까지의 초를 붙인다
    out="$(CHAT_MAX_TIME=120 chat "/agents/$2/chat" "$body" -D "$WORK/hdr" -w '%{http_code} %{time_starttransfer}')"
    code="${out%% *}"
    [ "$code" = 200 ] && break
    waking_pause "$1" "$code"
  done
  first="$(awk -v s="${out##* }" 'BEGIN { printf "%d", s * 1000 }')"
  WAKE_MS=$((start - t0 + first))
  ended_done "$1" "the timed chat"
  B_SIDS="$B_SIDS $(hdr X-Daegam-Session-Id)"
}

# --- 설정 바꾸기 -----------------------------------------------------------------------------------------------
# config_set STEP KEY=VALUE -> ConfigMap 의 그 키를 바꾸고 플랫폼을 굴린 뒤 다시 로그인한다. 키는 STEP 0 의 switch_auth_mode 가
# 바꾸기 전 값을 기억한 것이라 정리가 되돌린다
config_set() {
  local key="${2%%=*}" value="${2#*=}" patch
  # config_patches 의 첫 줄이 그 키를 바꾸는 merge patch 다(lib/e2e_control_plane.sh)
  patch="$(config_patches "$2" | sed -n 1p)"
  [ -n "$patch" ] || die "$1" "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p "$patch" >/dev/null \
    || die "$1" "patching ConfigMap $AUTH_MODE_CONFIGMAP $key=$value failed"
  restart_platform || die "$1" "the platform did not roll after $key=$value"
  restart_pf "$API_PORT"
  wait_http "$BASE/healthz" || die "$1" "the platform does not answer after the roll"
  admin_login "$1"
}
# admin_login STEP -> local 모드의 첫 관리자로 들어가 ADMIN_TOKEN·AUTH 를 둔다(lib/e2e_local_auth.sh local_admin_token)
admin_login() {
  local_admin_token "$1"
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
}

# --- Agent 저장 본문 (lib/agent_body.py) ---------------------------------------------------------------------------
agent_save() { # STEP NAME DISPLAY [FLAG...] -> SAVED_ID
  local step="$1" name="$2" title="$3"
  shift 3
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent-$name.json" --name "$name" --display "$title" \
    --provider "$STUB_PROVIDER" "$@"
  save_doc "$step" /agents "$name" "$(cat "$WORK/agent-$name.json")"
}
exposed_deploy() { # -> 노출을 켠 X 의 배포 본문
  python3 -c 'import json, sys
print(json.dumps({"cluster_id": sys.argv[1], "scale_to_zero": True, "exposure": {"enabled": True, "host": sys.argv[2],
  "ingress_class": sys.argv[3], "tls": {"mode": "none"}, "cors_origins": [sys.argv[4]], "allowed_groups": []}}))' \
    "$HOME_ID" "$EXT_HOST" "$INGRESS_CLASS" "$CORS_ORIGIN"
}

# --- 미리 보기 (P44b) -------------------------------------------------------------------------------------------
# preview_view EXPR -> 관리자의 GET /agents/A/preview 에서 파이썬 식 EXPR 을 찍는다(d 는 본문). 200 이 아니면 "코드 오류코드
# 끝난까닭" 이다(없는 것은 None)
preview_view() {
  local code
  code="$(api GET "/agents/$A_ID/preview")"
  if [ "$code" != 200 ]; then
    echo "$code $(jq_ '(d.get("detail") or {}).get("code"), ((d.get("detail") or {}).get("ended") or {}).get("reason")' 2>/dev/null || true)"
    return 0
  fi
  jq_ "$1"
}
# wait_preview STEP SECONDS EXPR WANT -> preview_view EXPR 가 WANT 를 찍을 때까지 5초마다 본다. 넘기면 die STEP 이다
wait_preview() {
  local got="" deadline=$((SECONDS + $2))
  while :; do
    got="$(preview_view "$3")"
    [ "$got" = "$4" ] && return 0
    [ "$SECONDS" -lt "$deadline" ] || die "$1" "the admin's preview of A after $2s: '$got' (want '$4')"
    sleep 5
  done
}
# preview_start STEP -> 관리자의 A 미리 보기를 띄워 PV_ID 에 남기고 파드가 Ready 일 때까지 READY_TIMEOUT 초 기다린다
preview_start() {
  local code
  code="$(api POST "/agents/$A_ID/preview")"
  [ "$code" = 202 ] || die "$1" "POST /agents/$A_ID/preview -> $code: $(cat "$WORK/body")"
  PV_ID="$(jq_ 'd["id"]')"
  wait_preview "$1" "$READY_TIMEOUT" 'd["id"], d["state"], d["phase"]' "$PV_ID deployed Ready"
}
# preview_left ID -> 그 미리 보기의 Deployment·Service·NetworkPolicy·파드(id 라벨)와 토큰 Secret 가운데 남은 것의 이름을 찍는다
preview_left() {
  "${K[@]}" -n "$A_NS" get deploy,svc,networkpolicy,pods -l "daegam.devops.ai.kr/id=$1" -o name 2>/dev/null || echo "?"
  "${K[@]}" -n "$A_NS" get secret "agent-inst-$1-token" -o name 2>/dev/null || true
}
# preview_gone STEP ID -> preview_left 가 빌 때까지 1초마다 POD_SETTLE_SECONDS 초 본다. 넘기면 die STEP 이다
preview_gone() {
  local left="" i
  for ((i = 0; i < POD_SETTLE_SECONDS; i++)); do
    left="$(preview_left "$2" | tr '\n' ' ')"
    [ -z "${left// /}" ] && return 0
    sleep 1
  done
  die "$1" "objects of the preview $2 still there after ${POD_SETTLE_SECONDS}s: $left"
}
# preview_token STEP KEY -> 관리자의 미리 보기(PV_ID)의 토큰 Secret 에서 KEY 의 토큰을 TOKEN 에 남기고, 그것이 A 의 id 와 주체를
# 싣고 미리 보기의 끝 + PREVIEW_SLACK 초(+5초) 안에 만료하는지 본다. 끝까지 남은 초를 TOKEN_SPARE 에 남긴다. 아니면 die STEP
preview_token() {
  local claims ends agent_id sub exp
  TOKEN="$(secret_field "$A_NS" "agent-inst-$PV_ID-token" "$2")" || die "$1" "reading $2 from Secret agent-inst-$PV_ID-token failed"
  [ -n "$TOKEN" ] || die "$1" "Secret agent-inst-$PV_ID-token has no $2"
  claims="$(jwt_json "$TOKEN" 1 | json_of - 'd.get("agent_id"), d.get("sub"), d.get("exp")')"
  ends="$(epoch_of "$(preview_view 'd["ends_at"]')")"
  read -r agent_id sub exp <<<"$claims"
  { [ "$agent_id" = "$A_ID" ] && [ "$sub" = "svc:agent:$A_K8S" ]; } \
    || die "$1" "the preview's $2 carries agent_id '$agent_id' and sub '$sub' (want $A_ID and svc:agent:$A_K8S)"
  [ "$exp" -le $((ends + PREVIEW_SLACK + 5)) ] || die "$1" "the preview's $2 expires at $exp, after its end $ends + ${PREVIEW_SLACK}s"
  TOKEN_SPARE=$((ends + PREVIEW_SLACK - exp))
}

# --- 단계 ------------------------------------------------------------------------------------------------------
step_0() {
  local mode window
  mode="$(platform_config DAEGAM_AUTH_MODE)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  window="$(platform_config DAEGAM_AGENT_SLEEP_AFTER_SECONDS)"
  # 앞 실행이 되돌리지 못한 값이면 되돌릴 값을 모른다
  { [ "${mode:-header}" = header ] && [ "${window:-0}" = 0 ]; } \
    || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP has DAEGAM_AUTH_MODE='$mode' DAEGAM_AGENT_SLEEP_AFTER_SECONDS='$window' (left by an earlier run?): set header mode and the window 0, roll the control plane and run again"
  switch_auth_mode local "DAEGAM_AGENT_SLEEP_AFTER_SECONDS=$WINDOW" "DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS=$HOLD" \
    DAEGAM_AGENT_HOUSEKEEPING_HOURS=24 DAEGAM_AGENT_SCALE_TO_ZERO=true \
    || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to local mode and the sleep window and rolling the control plane failed"
  pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=local (was ${AUTH_MODE_PREV:-unset}), window ${WINDOW}s, hold ${HOLD}s; platform and UI rolled out"
}

step_1() {
  local code
  local_admin_session 1
  code="$(api GET /clusters)"
  [ "$code" = 200 ] || die 1 "GET /clusters -> $code"
  HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home" and c["status"]["preflight"]["ok"]), "")')"
  [ -n "$HOME_ID" ] || die 1 "cluster home missing or preflight not ok"
  ensure_stub_provider 1 "$STUB_PROVIDER"
  lab_database 1 postgres "$PG_DS_NAME" "휴면 e2e DB"
  PG_DS_ID="$DATA_SERVICE_ID"
  lab_database 1 redis "$RDS_NAME" "휴면 e2e 레디스"
  RDS_ID="$DATA_SERVICE_ID"
  calc_mcp_server 1 "$BP_NAME" "$MS_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
  code="$(api GET /agents/templates)"
  [ "$code" = 200 ] || die 1 "GET /agents/templates -> $code"
  cp "$WORK/body" "$WORK/templates.json"
  agent_save 1 "$A_NAME" "휴면 e2e A" --ms-id "$CALC_MS_ID" --approve --ds-id "$PG_DS_ID" --session-ttl "$A_TTL"
  A_ID="$SAVED_ID"
  agent_save 1 "$B_NAME" "휴면 e2e B" --ms-id "$CALC_MS_ID" --approve --ds-id "$RDS_ID" --session-ttl "$B_TTL"
  B_ID="$SAVED_ID"
  agent_save 1 "$N_NAME" "휴면 e2e 기억 없음"
  N_ID="$SAVED_ID"
  agent_save 1 "$R_NAME" "휴면 e2e 레플리카" --ds-id "$PG_DS_ID"
  R_ID="$SAVED_ID"
  agent_save 1 "$X_NAME" "휴면 e2e 노출" --ds-id "$PG_DS_ID"
  X_ID="$SAVED_ID"
  local on="{\"cluster_id\": \"$HOME_ID\", \"scale_to_zero\": true}" id
  for id in "$A_ID" "$B_ID" "$N_ID" "$R_ID"; do deploy_doc 1 /agents "$id" "$on"; done
  deploy_doc 1 /agents "$X_ID" "$(exposed_deploy)"
  echo ">> waiting up to ${READY_TIMEOUT}s for each Agent to be Ready"
  # 창이 짧아 A·B 는 다른 Agent 를 기다리는 동안 잠들 수 있다. 그래서 Ready 는 잠들 수 없는 셋만 기다린다
  for id in "$R_ID" "$N_ID" "$X_ID"; do wait_ready "/agents/$id" 1; done
  # R 은 덮어쓰기 전까지 잠들 수 있다. 그 사이 잠들었으면 이 저장이 깨운다(STEP 9 와 같은 길)
  code="$(api PUT "/agents/$R_ID/overrides" "$REPLICAS_PATCH")"
  [ "$code" = 200 ] || die 1 "PUT /agents/$R_ID/overrides (spec.replicas) -> $code"
  wait_ready "/agents/$R_ID" 1
  wait_view 1 "$A_ID" "$READY_TIMEOUT" 's.get("phase") in ("Ready", "Sleeping")' True
  wait_view 1 "$B_ID" "$READY_TIMEOUT" 's.get("phase") in ("Ready", "Sleeping")' True
  local reasons
  reasons="$(for id in "$A_ID" "$B_ID" "$N_ID" "$R_ID" "$X_ID"; do view "$id" 'z.get("ineligible"), z.get("window_seconds")'; done | tr '\n' ' ')"
  [ "$reasons" = "None $WINDOW None $WINDOW no_memory $WINDOW replicas_override $WINDOW exposed $WINDOW " ] \
    || die 1 "sleep.ineligible and window_seconds of A B N R X: '$reasons' (want None None no_memory replicas_override exposed, window $WINDOW)"
  A_NS="$(view "$A_ID" 'd["k8s"]["namespace"]')"; A_K8S="$(view "$A_ID" 'd["k8s"]["name"]')"
  B_NS="$(view "$B_ID" 'd["k8s"]["namespace"]')"; B_K8S="$(view "$B_ID" 'd["k8s"]["name"]')"
  IDLE_FROM=$SECONDS
  pass 1 "admin signed in, stub provider $STUB_PROVIDER, Databases $PG_DS_NAME and $RDS_NAME, MCP Server $MS_NAME [Add]; Agents A $A_ID, B $B_ID, N $N_ID, R $R_ID (spec.replicas override), X $X_ID (exposed $EXT_HOST) deployed with scale_to_zero; ineligible A/B none, N no_memory, R replicas_override, X exposed; window ${WINDOW}s"
}

step_2() {
  local got deadline
  chat_awake 2 "$A_ID" "$ASK_MSG"
  [ "$(sse_ '"interrupt" in names')" = True ] || { cp "$WORK/sse" "$WORK/body"; die 2 "the Add call did not wait for approval: $(sse_ 'names')"; }
  SP_SID="$CHAT_SID"
  echo ">> session $SP_SID waits for approval; waiting up to ${SLEEP_WAIT}s for the sleep worker to put A to sleep"
  deadline=$((SECONDS + SLEEP_WAIT))
  while :; do
    got="$(view "$A_ID" 's.get("phase"), z.get("state"), z.get("reason"), s.get("pending_approvals")')"
    [ "$got" = "Sleeping asleep idle 1" ] && break
    [ "$SECONDS" -lt "$deadline" ] || die 2 "agent A after ${SLEEP_WAIT}s: '$got' (want Sleeping asleep idle 1)"
    sleep 5
  done
  A_SINCE="$(view "$A_ID" 'z.get("since")')"
  got="$(replicas "$A_NS" "$A_K8S")"
  [ "${got%% *}" = 0 ] || die 2 "deploy/$A_K8S spec.replicas is ${got%% *} while A sleeps"
  pass 2 "A slept by itself after the ${WINDOW}s window (reason idle, since $A_SINCE); deploy/$A_K8S at 0; status Sleeping with the saved pending approvals 1 (session $SP_SID)"
}

step_3() {
  local end=$((SECONDS + 2 * WINDOW)) polls=0 code got
  while [ "$SECONDS" -lt "$end" ]; do
    for query in "status=pending" "status=pending&runs=only&all_users=true"; do
      code="$(api GET "/agents/$A_ID/sessions?$query")"
      [ "$code $(error_code "$WORK/body")" = "409 agent_asleep" ] \
        || die 3 "GET /agents/$A_ID/sessions?$query on a sleeping Agent -> $code (want 409 agent_asleep)"
    done
    got="$(view "$A_ID" 's.get("phase"), z.get("since")')"
    [ "$got" = "Sleeping $A_SINCE" ] || die 3 "agent A status during the reads: '$got' (want Sleeping $A_SINCE)"
    polls=$((polls + 1))
    sleep 5
  done
  got="$(replicas "$A_NS" "$A_K8S")"
  [ "$got" = "0 0" ] || die 3 "deploy/$A_K8S after the reads: replicas and ready '$got' (want 0 0)"
  [ -z "$(agent_pods "$A_NS" "$A_ID")" ] || die 3 "agent A runs a pod after the reads"
  pass 3 "$polls rounds over $((2 * WINDOW))s of the Approvals tab's two lists (409 agent_asleep) and the status (Sleeping, since unchanged); A still at 0"
}

step_4() {
  local code started=$SECONDS phase
  code="$(chat "/agents/$A_ID/chat" "$(msg "$DONE_MSG")" -D "$WORK/hdr")"
  { [ "$code" = 503 ] && [ "$(error_code "$WORK/sse")" = agent_waking ] && [ -n "$(hdr Retry-After)" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 4 "the first chat to the sleeping A -> $code (want 503 agent_waking with Retry-After)"; }
  phase="$(view "$A_ID" 's.get("phase"), z.get("state"), z.get("reason")')"
  [ "$phase" = "Waking waking turn" ] || die 4 "agent A after the first chat: '$phase' (want Waking waking turn)"
  chat_awake 4 "$A_ID" "$DONE_MSG"
  ended_done 4 "the chat after the wake"
  pass 4 "the first chat answered 503 agent_waking with Retry-After $(hdr Retry-After)s and A read Waking; resent after each Retry-After it ended done after $WAKE_TRIES more agent_waking answers, $((SECONDS - started))s in all"
}

step_5() {
  local code
  code="$(SSE_OUT="$WORK/decision.out" chat "/agents/$A_ID/sessions/$SP_SID/decisions" '{"decisions": [{"type": "approve"}]}')"
  [ "$code" = 410 ] || { cp "$WORK/decision.out" "$WORK/body"; die 5 "a decision on session $SP_SID past its ${A_TTL}s TTL right after the wake -> $code (want 410)"; }
  pass 5 "the decision on $SP_SID, whose ${A_TTL}s TTL passed while A slept, answered 410 $(error_code "$WORK/decision.out") right after the wake"
}

# storm ID -> $WORK/storm.stop 이 생길 때까지 1초마다 새 세션의 끝나는 채팅을 뒤에서 보낸다. 답의 코드와 본문은
# $WORK/storm/<n>.code·.out 이다. 멈추면 보낸 것이 끝나기를 기다린다
storm() {
  local i=0
  while [ ! -e "$WORK/storm.stop" ]; do
    i=$((i + 1))
    (CHAT_MAX_TIME=60 SSE_OUT="$WORK/storm/$i.out" chat "/agents/$1/chat" "$(msg "$DONE_MSG")" >"$WORK/storm/$i.code") &
    sleep 1
  done
  wait
}

step_6() {
  local i code why sleeps="" f n out last s ended=0 waking=0 slept=0 bad=""
  mkdir -p "$WORK/storm"
  rm -f "$WORK/storm.stop"
  # 뒤의 채팅은 게이트의 출력(8·9)을 쥐지 않는다(lib/e2e_exit.sh)
  storm "$A_ID" 8>&- 9>&- &
  STORM_PID=$!
  for ((i = 0; i < SLEEP_CALLS; i++)); do
    code="$(api POST "/agents/$A_ID/sleep")"
    why=""; [ "$code" = 200 ] || why="$(error_code "$WORK/body")"
    sleeps="$sleeps $code${why:+:$why}"
    sleep 1
  done
  # 마지막 잠들기 뒤에도 채팅을 몇 번 더 보내 그것이 깨우는 답을 본다
  sleep 3
  touch "$WORK/storm.stop"
  wait "$STORM_PID" || true
  STORM_PID=""
  # 채팅마다 .code 와 .out 이 있다. 200 은 done 으로, 503 은 agent_waking 으로 끝나야 한다
  for f in "$WORK"/storm/*.code; do
    [ -e "$f" ] || continue
    n="${f%.code}"; code="$(cat "$f")"; out="$n.out"
    [ -f "$out" ] || : >"$out"
    case "$code" in
      200)
        last="$(sse_ 'names[-1] if names else ""' "$out" 2>/dev/null || true)"
        if [ "$last" = "done" ]; then ended=$((ended + 1)); else bad="$bad | chat ${n##*/}: 200 ended '$last'"; fi ;;
      503)
        why="$(error_code "$out")"
        if [ "$why" = agent_waking ]; then waking=$((waking + 1)); else bad="$bad | chat ${n##*/}: 503 $why"; fi ;;
      *) bad="$bad | chat ${n##*/}: $code $(head -c 160 "$out")" ;;
    esac
  done
  for s in $sleeps; do
    case "$s" in
      200) slept=$((slept + 1)) ;;
      409:agent_busy | 409:state_conflict | 409:in_progress | 502:runtime_unreachable) ;;
      *) bad="$bad | sleep $s" ;;
    esac
  done
  [ "$slept" -gt 0 ] || bad="$bad | no sleep call answered 200"
  [ "$ended" -gt 0 ] && [ "$waking" -gt 0 ] \
    || bad="$bad | want at least one done and one agent_waking chat (done $ended, agent_waking $waking)"
  [ -z "$bad" ] || die 6 "chats racing the sleep route:${bad# |}" "sleep answers:$sleeps"
  pass 6 "$((ended + waking)) chats sent every second while the sleep route ran $SLEEP_CALLS times: $ended ended done, $waking answered 503 agent_waking, none turn_aborted; sleep answers:$sleeps ($slept slept)"
}

step_7() {
  local out ms
  sleep_route 7 "$A_ID"
  wait_no_pods 7 "$A_NS" "$A_ID"
  out="$("${K[@]}" -n "$PLATFORM_NS" exec deploy/daegam-platform -c platform -- python -c '
import sys, time, urllib.request
t = time.monotonic()
try:
    urllib.request.urlopen(sys.argv[1], timeout=10)
    print("answered", int((time.monotonic() - t) * 1000), "-")
except Exception as e:
    why = getattr(e, "reason", e)
    print("failed", int((time.monotonic() - t) * 1000), type(why).__name__)
' "http://$A_K8S.$A_NS.svc:8080/healthz" 2>"$WORK/exec.err" || true)"
  read -r verdict ms err <<<"$out"
  [ "$verdict" = failed ] || die 7 "a call to Service $A_K8S.$A_NS at 0 replicas: '$out' (want a failure)" "$(cat "$WORK/exec.err")"
  # 이름을 풀지 못한 실패(gaierror)도 빨리 끝나므로 까닭을 본다. 엔드포인트 없는 Service 는 연결을 거부한다
  case "$err" in
    ConnectionRefusedError | ConnectionResetError) ;;
    *) die 7 "a call to Service $A_K8S.$A_NS at 0 replicas failed with $err (want a refused connection)" "$out" ;;
  esac
  [ "$ms" -lt "$SERVICE_FAIL_MS" ] || die 7 "a call to Service $A_K8S.$A_NS at 0 replicas failed after ${ms}ms (want under ${SERVICE_FAIL_MS}ms)"
  pass 7 "A slept again through the route; with no pod left a call from the platform pod to Service $A_K8S.$A_NS:8080 failed in ${ms}ms ($err)"
}

step_8() {
  local id want got code idle=$((SECONDS - IDLE_FROM)) notes=""
  # 셋이 잠들 수 있었다면 잠들었을 만큼 쉬었다. 모자라면 채운다
  [ "$idle" -ge $((2 * WINDOW)) ] || sleep $((2 * WINDOW - idle))
  for pair in "$N_ID:no_memory" "$R_ID:replicas_override" "$X_ID:exposed"; do
    id="${pair%%:*}"; want="${pair#*:}"
    got="$(view "$id" 's.get("phase"), z.get("state"), z.get("ineligible")')"
    [ "$got" = "Ready awake $want" ] || die 8 "agent $id after $((SECONDS - IDLE_FROM))s idle: '$got' (want Ready awake $want)"
    got="$(replicas "$(view "$id" 'd["k8s"]["namespace"]')" "$(view "$id" 'd["k8s"]["name"]')")"
    [ "$got" = "1 1" ] || die 8 "agent $id Deployment replicas and ready '$got' (want 1 1)"
    code="$(api POST "/agents/$id/sleep")"
    [ "$code $(error_code "$WORK/body") $(jq_ '(d.get("detail") or {}).get("ineligible")' || true)" = "409 agent_sleep_ineligible $want" ] \
      || die 8 "POST /agents/$id/sleep -> $code (want 409 agent_sleep_ineligible $want)"
    notes="$notes $want"
  done
  pass 8 "after $((SECONDS - IDLE_FROM))s idle N, R and X still run one ready pod each and read Ready awake with$notes; the sleep route answers 409 agent_sleep_ineligible with the same reason"
}

step_9() {
  local code got
  code="$(api PUT "/agents/$A_ID/overrides" "$REPLICAS_PATCH")"
  [ "$code" = 200 ] || die 9 "PUT /agents/$A_ID/overrides (spec.replicas) on the sleeping A -> $code"
  got="$(view "$A_ID" 'z.get("state"), z.get("reason"), z.get("ineligible")')"
  [ "$got" = "awake ineligible replicas_override" ] || die 9 "agent A right after the override: '$got' (want awake ineligible replicas_override)"
  wait_ready "/agents/$A_ID" 9
  got="$(replicas "$A_NS" "$A_K8S")"
  [ "$got" = "1 1" ] || die 9 "deploy/$A_K8S after the override: '$got' (want 1 1)"
  code="$(api PUT "/agents/$A_ID/overrides" '{"patch": {}}')"
  [ "$code" = 200 ] || die 9 "PUT /agents/$A_ID/overrides (cleared) -> $code"
  got="$(view "$A_ID" 'z.get("ineligible")')"
  [ "$got" = None ] || die 9 "agent A after clearing the override reads ineligible '$got' (want None)"
  pass 9 "saving a spec.replicas override on the sleeping A woke it in the same save (awake, reason ineligible, replicas_override) and it is Ready on one pod; clearing the override makes it eligible again"
}

step_10() {
  local i stats
  if [ "$WAKES" = 0 ]; then
    cond_skip 10 "E2E_PROVISIONING_WAKES=0"
    return 0
  fi
  : >"$WORK/wakes"
  for ((i = 1; i <= WAKES; i++)); do
    sleep_route 10 "$B_ID"
    wait_no_pods 10 "$B_NS" "$B_ID"
    timed_wake 10 "$B_ID"
    echo ">> wake $i/$WAKES: ${WAKE_MS}ms to the first byte of the answer ($WAKE_TRIES agent_waking answers)"
    echo "$WAKE_MS" >>"$WORK/wakes"
  done
  stats="$(python3 -c 'import math, sys
ms = sorted(int(x) for x in open(sys.argv[1]) if x.strip())
rank = lambda p: ms[max(0, math.ceil(p * len(ms)) - 1)]
print(rank(0.5), rank(0.95), ms[0], ms[-1])' "$WORK/wakes")"
  read -r p50 p95 low high <<<"$stats"
  echo "TIMING wake_to_first_answer_ms n=$WAKES p50=$p50 p95=$p95 min=$low max=$high"
  pass 10 "$WAKES wakes of B (Redis memory) from no pod to the first byte of the answer: p50 ${p50}ms, p95 ${p95}ms (min $low, max $high)"
}

step_11() {
  local before got deadline seen=0 since
  chat_awake 11 "$B_ID" "$ASK_MSG"
  [ "$(sse_ '"interrupt" in names')" = True ] || { cp "$WORK/sse" "$WORK/body"; die 11 "the Add call on B did not wait for approval: $(sse_ 'names')"; }
  B_SIDS="$B_SIDS $CHAT_SID"
  sleep_route 11 "$B_ID"
  before="$(view "$B_ID" 's.get("pending_approvals"), z.get("since")')"
  [ "${before%% *}" -ge 1 ] || die 11 "B slept with pending approvals '${before%% *}' (want at least the session $CHAT_SID)"
  since="${before#* }"
  config_set 11 "DAEGAM_AGENT_HOUSEKEEPING_HOURS=$HOUSEKEEPING_HOURS"
  echo ">> waiting up to ${HOUSEKEEPING_WAIT}s for the daily wake of B and its sleep after the ${HOLD}s hold"
  deadline=$((SECONDS + HOUSEKEEPING_WAIT))
  while :; do
    got="$(view "$B_ID" 'z.get("state"), z.get("reason"), z.get("since"), s.get("pending_approvals")')"
    case "$got" in
      "waking housekeeping "* | "awake housekeeping "*) seen=1 ;;
      "asleep housekeeping "*) [ "$seen" = 1 ] && [ "$(echo "$got" | awk '{ print $3 }')" != "$since" ] && break ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || die 11 "agent B after ${HOUSEKEEPING_WAIT}s: '$got' (seen awake for housekeeping: $seen)"
    sleep 5
  done
  [ "$(echo "$got" | awk '{ print $4 }')" = 0 ] \
    || die 11 "B slept again after the daily wake with pending approvals $(echo "$got" | awk '{ print $4 }') (want 0: the start sweep closes the session past its ${B_TTL}s TTL)"
  got="$(replicas "$B_NS" "$B_K8S")"
  [ "${got%% *}" = 0 ] || die 11 "deploy/$B_K8S spec.replicas is ${got%% *} after the daily wake ended"
  pass 11 "B slept with ${before%% *} pending approval; with the period at ${HOUSEKEEPING_HOURS}h the worker woke it for housekeeping and put it back to sleep after the ${HOLD}s hold; the session past its ${B_TTL}s TTL no longer counts (pending 0) and deploy/$B_K8S is at 0"
}

step_12() {
  local id got
  sleep_route 12 "$A_ID"
  sleep_route 12 "$B_ID"
  config_set 12 DAEGAM_AGENT_SCALE_TO_ZERO=false
  for id in "$A_ID" "$B_ID"; do
    wait_view 12 "$id" "$WAKE_WAIT" 's.get("phase"), z.get("state"), z.get("ineligible")' "Ready awake setting_off"
  done
  got="$(replicas "$A_NS" "$A_K8S") $(replicas "$B_NS" "$B_K8S")"
  [ "$got" = "1 1 1 1" ] || die 12 "deploy/$A_K8S and deploy/$B_K8S replicas and ready '$got' (want 1 1 1 1)"
  pass 12 "with A and B asleep, turning DAEGAM_AGENT_SCALE_TO_ZERO off woke both; each runs one ready pod and reads Ready awake setting_off"
}

step_13() {
  local code src stranger
  rules_save 13 "$CALC_MS_ID" "{\"Add\": {\"callers\": {\"agents\": [\"$A_ID\"]}}}"
  rules_redeploy 13 "$CALC_MS_ID"
  rules_settled 13 "$CALC_MS_ID"
  turn_member_developer 13
  preview_start 13
  code="$(chat "/agents/$A_ID/preview/chat" "$(msg "$DONE_MSG")")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 13 "POST /agents/$A_ID/preview/chat -> $code"; }
  ended_done 13 "the preview's chat"
  code="$(api GET "/agents/$PV_ID")"
  [ "$code" = 404 ] || die 13 "GET /agents/$PV_ID (the preview's id) by its owner -> $code (want 404)"
  code="$(api GET /agents)"
  [ "$code" = 200 ] || die 13 "GET /agents -> $code"
  [ "$(jq_ "'$PV_ID' in [a['id'] for a in d]")" = False ] \
    || die 13 "the Agent list holds the preview $PV_ID"
  # 미리 보기의 calculator 토큰은 원본의 id 와 주체를 싣고 끝 + 10분 안에 만료한다 (P44b 8절, 9절)
  src="$(mcp_token_env "$CALC_MS_ID")"
  preview_token 13 "$src"
  # 열려 있는 동안 calculator 프록시는 그 토큰을 받는다. 규칙이 이름 대지 않은 Agent 의 토큰은 Tool 을 보지 못한다
  start_pf "$CALC_NS" "svc/mcp-$CALC_MS_K8S_NAME-proxy" "${CALC_PORT}:8080"
  PF_CALC=$PF_PID
  wait_port "$CALC_PORT" || die 13 "port-forward to svc/mcp-$CALC_MS_K8S_NAME-proxy did not open :$CALC_PORT"
  mcp_admits 13 "$TOKEN" "the preview's $src" "http://127.0.0.1:${CALC_PORT}/mcp"
  stranger="$(svc_token "$CALC_MS_ID" --sub "$STRANGER_SUB" --agent-id e2e-unnamed)" || die 13 "svc_token.py (unnamed Agent) failed"
  mcp_refuses 13 "$stranger" "an Agent the rule does not name" "http://127.0.0.1:${CALC_PORT}/mcp"
  stop_pf "$PF_CALC" "$CALC_PORT"; PF_CALC=""
  pass 13 "with Add's rule naming A, the preview $PV_ID of A ran on its own pod and answered done; GET /agents/$PV_ID is 404 for its owner and the Agent list leaves it out; its calculator token carries A's id and subject, expires ${TOKEN_SPARE}s before its end + ${PREVIEW_SLACK}s and is admitted by the calculator proxy, which refuses an Agent the rule does not name"
}

step_14() {
  local code
  code="$(chat "/agents/$A_ID/preview/chat" "$(msg "$ASK_MSG")" -D "$WORK/hdr")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 14 "POST /agents/$A_ID/preview/chat (Add) -> $code"; }
  [ "$(sse_ '"interrupt" in names')" = True ] || { cp "$WORK/sse" "$WORK/body"; die 14 "the preview's Add call did not wait for approval: $(sse_ 'names')"; }
  PV_SID="$(hdr X-Daegam-Session-Id)"
  [ -n "$PV_SID" ] || die 14 "no X-Daegam-Session-Id on the preview's chat" "$(cat "$WORK/hdr")"
  code="$(chat "/agents/$A_ID/preview/sessions/$PV_SID/decisions" '{"decisions": [{"type": "approve"}]}')"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 14 "POST /agents/$A_ID/preview/sessions/$PV_SID/decisions -> $code"; }
  { [ "$(sse_ 'first("tool_result")["status"]')" = success ] && sse_ 'first("tool_result")["content"]' | grep -q 7; } \
    || die 14 "the approved Add of the preview: $(sse_ 'names') tool_result=$(sse_ 'first("tool_result")')"
  ended_done 14 "the preview's decision"
  pass 14 "the preview's Add waited for approval in session $PV_SID; the decision called Add, which the rule naming only A admitted, and the turn ended done with 7"
}

step_15() {
  local code got
  DEV_UID="$(local_fresh_member "$DEV_USER" "$TENANT")" || die 15 "creating $DEV_USER in $TENANT failed"
  local_set_member "$TENANT" "$DEV_UID" developer || die 15 "making $DEV_USER a developer of $TENANT failed"
  DEV_TOKEN="$(local_login_any "$DEV_USER" "$LOCAL_PW")"
  [ -n "$DEV_TOKEN" ] || die 15 "login as $DEV_USER failed"
  code="$(api_as "$DEV_TOKEN" GET "/agents/$PV_ID")"
  [ "$code" = 404 ] || die 15 "GET /agents/$PV_ID (the preview's id) by $DEV_USER -> $code (want 404)"
  code="$(api_as "$DEV_TOKEN" GET "/agents/$A_ID/preview")"
  [ "$code $(error_code "$WORK/body")" = "404 preview_not_found" ] \
    || die 15 "GET /agents/$A_ID/preview by $DEV_USER, who opened none -> $code $(error_code "$WORK/body") (want 404 preview_not_found)"
  code="$(api_as "$DEV_TOKEN" GET "/agents/$A_ID/previews")"
  [ "$code" = 200 ] || die 15 "GET /agents/$A_ID/previews by $DEV_USER -> $code"
  got="$(jq_ "next(([p['owner'], p['revision']] for p in d['previews'] if p['id'] == '$PV_ID'), None)")"
  [ "$got" = "['$ADMIN_SUB', $(view "$A_ID" 'd["revision"]')]" ] \
    || die 15 "$DEV_USER's list of A's previews shows $PV_ID as $got (want owner $ADMIN_SUB and A's revision)"
  code="$(api_as "$DEV_TOKEN" DELETE "/agents/$A_ID/previews/$PV_ID")"
  [ "$code" = 200 ] || die 15 "DELETE /agents/$A_ID/previews/$PV_ID by $DEV_USER -> $code: $(cat "$WORK/body")"
  # 다른 작업이 문서를 쥐고 있었으면(ended false) 리컨실이 마저 지운다. 그동안은 끝나는 중(deleting)으로 읽힌다
  wait_preview 15 "$PREVIEW_END_WAIT" 'd["id"], d["state"]' "404 preview_not_found ended_by_other"
  preview_gone 15 "$PV_ID"
  pass 15 "$DEV_USER (developer) got 404 for the preview's id and for a preview of its own, listed $PV_ID under owner $ADMIN_SUB and ended it; the owner reads ended_by_other and the preview's objects are gone"
}

step_16() {
  local gw_ns
  # Gateway 의 백엔드 호출은 Gateway 의 신원이라 A 만 이름 댄 규칙이 Add 를 가린다. 규칙을 걷은 뒤에 묶는다
  rules_save 16 "$CALC_MS_ID" '{}'
  rules_redeploy 16 "$CALC_MS_ID"
  rules_settled 16 "$CALC_MS_ID"
  calc_publish 16
  calc_gateway 16 "$GW_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
  gw_ns="$(jq_ 'd["k8s"]["namespace"]')"
  # 미리 보기는 저장한 최신 리비전을 돈다. A 의 배포는 그대로 두고 Tool 소스가 Gateway 인 리비전만 저장한다
  agent_save 16 "$A_NAME" "휴면 e2e A" --source "gateway_instance:$CALC_GW_ID" --ds-id "$PG_DS_ID" --session-ttl "$A_TTL"
  preview_start 16
  preview_token 16 gateway_token
  start_pf "$gw_ns" "svc/vmcp-$CALC_GW_K8S_NAME" "${VMCP_PORT}:4483"
  PF_VMCP=$PF_PID
  wait_port "$VMCP_PORT" || die 16 "port-forward to svc/vmcp-$CALC_GW_K8S_NAME did not open :$VMCP_PORT"
  mcp_admits 16 "$TOKEN" "the preview's gateway_token" "http://127.0.0.1:${VMCP_PORT}/mcp"
  stop_pf "$PF_VMCP" "$VMCP_PORT"; PF_VMCP=""
  pass 16 "with Add's rule cleared, Gateway $GW_NAME over the published calculator is A's Tool source in a saved revision; its preview $PV_ID holds a gateway_token that carries A's id and subject, expires ${TOKEN_SPARE}s before its end + ${PREVIEW_SLACK}s and is admitted by the vMCP, which calls $MCP_ADMITTED_TOOL"
}

step_17() {
  local got deadline back
  if ! platform_mongo_here; then
    cond_skip 17 "the platform's MongoDB is outside the cluster (no pod mongodb-0 in $PLATFORM_NS)"
    return 0
  fi
  # 마지막 활동은 시작과 턴이 적는다. 둘 다 31분 앞으로 당기면 쓰지 않은 끝(30분)이 지났고 처음 시작 2시간 뒤는 아직이다
  back="$(platform_mongo "const at = new Date(Date.now() - 31 * 60 * 1000);
print(db.agents.updateOne({_id: '$PV_ID', preview: {\$ne: null}}, {\$set: {'sleep.activity': at, 'preview.started_at': at}}).matchedCount)")" \
    || die 17 "moving the preview's activity back in MongoDB failed"
  [ "$back" = 1 ] || die 17 "moving the activity of the preview $PV_ID back matched '$back' documents (want 1)"
  echo ">> waiting up to ${PREVIEW_END_WAIT}s for the preview worker to end $PV_ID as idle"
  deadline=$((SECONDS + PREVIEW_END_WAIT))
  while :; do
    got="$(platform_mongo "print(db.agents.countDocuments({_id: '$PV_ID'}))" || echo "?")"
    [ "$got" = 0 ] && break
    [ "$SECONDS" -lt "$deadline" ] || die 17 "the preview $PV_ID is still in agents after ${PREVIEW_END_WAIT}s ($got documents)"
    sleep 5
  done
  got="$(preview_view 'd["id"]')"
  [ "$got" = "404 preview_not_found idle" ] || die 17 "the owner's GET /agents/$A_ID/preview after the idle end: '$got' (want 404 preview_not_found idle)"
  preview_gone 17 "$PV_ID"
  pass 17 "with its last activity moved 31 minutes back the preview worker ended $PV_ID: its document and objects are gone and the owner reads idle"
}

echo ">> context=$CTX platform_ns=$PLATFORM_NS tenant=$TENANT api=$API wakes=$WAKES timing=${TIMING:-gate}"
step_0
step_1
if [ "$TIMING" = only ]; then
  for n in 2 3 4 5 6 7 8 9; do cond_skip "$n" "E2E_PROVISIONING_TIMING=only"; done
  step_10
  for n in 11 12 13 14 15 16 17; do cond_skip "$n" "E2E_PROVISIONING_TIMING=only"; done
  finish
fi
step_2
step_3
step_4
step_5
step_6
step_7
step_8
step_9
step_10
step_11
step_12
step_13
step_14
step_15
step_16
step_17
finish
