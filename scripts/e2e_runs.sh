#!/usr/bin/env bash
# P41 (D94) 자동 실행 e2e. Agent 가 사람 없이 도는 지금 실행, 일정, 서명한 웹훅과 그 승인·마감·사람 확인을 설치본에서
# 본다. 설치본만 대상이고, 플랫폼 곁에 둔 Redis 를 memory 로 쓴다(e2e_install STEP 4 와 같은 Redis). 릴리스 후보 태그의
# e2e: 줄이 고르면 make e2e-install-extra 가 e2e-install 다음에 돌린다.
#
#   make e2e-runs E2E_INSTALL_USER=<e2e 전용 관리자>
#
# 관리자 자격 증명은 e2e-install 과 같은 E2E_INSTALL_PASSWORD 나 키체인 항목 daegam-e2e-install 이다. 값은 설치 값 파일(~/.config/daegam/install.env)이 기본이고
# 환경이 이긴다. 플랫폼은 공개 주소(https://<DAEGAM_HOST>)로 부르고, 웹훅도 보내는 쪽처럼 그 주소로 부른다. 클러스터는 이미
# local 모드다. 테넌트 E2E_RUNS_TENANT(기본 e2e-runs)에 버리는 developer 둘(u1, u2)을 만든다. 두 사람의 비밀번호는 실행마다
# 새로 만든다(lib/e2e_local_auth.sh install_run_passwords). 실행 기록과 Agent·일정·트리거는 API 로, 일정을 당기는 것과 기록이
# 비밀을 품지 않는지는 플랫폼 MongoDB(mongodb-0)의 문서 몇 건으로 본다. 그 MongoDB 에 부하를 싣지 않는다. 시각(웹훅
# 타임스탬프, 감사의 since)은 이 컴퓨터가 아니라 클러스터의 시계로 잡는다(lib/e2e_turn_tokens.sh cluster_now).
#
# 기다림의 한도(초)는 E2E_RUNS_WAIT(실행 기록, 기본 240), E2E_RUNS_PERSON_WAIT(지금 실행이 사람 이름으로 돌기까지, 480),
# E2E_RUNS_AGENT_TLS_WAIT(memory 바인딩의 TLS 전환, 240)로 바꾼다.
#
# Gate:
#   STEP 0   관리자 로그인, 실행이 켜져 있다(GET /api/me/runs-as-me 200). ConfigMap 에 스텁 provider 허용을 이 게이트 동안만
#            두고(되돌리는 명령을 먼저 찍는다), ConfigMap 을 고친 뒤 플랫폼을 굴린다. 이미 열려 있으면 바꾸기 전에 실패한다
#   STEP 1   u1·u2 를 테넌트의 developer 로 두고, 스텁 provider, Redis Database(TLS), petstore Backend API 와 addPet 하나의
#            MCP Server, 실행의 전제를 갖춘 Agent(그 MCP Server 의 addPet 에 승인 규칙, Redis memory, 세션 예산, 세션 TTL 120초)를
#            배포한다. Agent 는 runs 를 알리고 memory 바인딩이 TLS 로 옮겨 간 뒤 Ready 다
#   STEP 2   u1 의 지금 실행이 succeeded 로 끝나고 u1 의 이름(acts_as person)으로 돈다. MCP Server 는 자기 audience 로 옮겨 간
#            뒤에야 위임 토큰을 받으므로, 그때까지 Agent 의 이름(no_turn_token)으로 돈 실행이나 시작이 잠깐 실패한 실행
#            (tools_unavailable, 5xx, runtime_unreachable)이면 30초마다 새로 넣는다. 기록은 입력 대신 크기만 내고, 실행 세션은
#            닫히며 u1 의 채팅 목록에 없고 runs=include 에만 있다
#   STEP 2s  (P44) Agent 의 scale_to_zero 를 켜고 잠들기 라우트로 재운 뒤(창이 0 이어도 된다) u1 의 지금 실행이 그 Agent 를
#            깨워 succeeded 로 끝난다. 끝나면 스위치를 다시 끈다. 설치본이 플랫폼 스위치(DAEGAM_AGENT_SCALE_TO_ZERO)를 꺼
#            두었으면 조건부로 건너뛴다
#   STEP 3   5분보다 잦은 규칙은 422 schedule_too_frequent, 읽지 못하는 규칙은 422 schedule_invalid_cron 이다. u1 의 일정
#            (매일 09:00 Asia/Seoul)의 next_run_at 을 MongoDB 에서 지금으로 당기면 그 울림이 기록 하나로 돌아 succeeded 가
#            된다. 한 틱 더 지나도 기록은 하나이고 next_run_at 이 다음 울림으로 넘어갔다
#   STEP 4   u1 의 웹훅 트리거는 만들 때 whsec_ 비밀을 한 번 보이고 목록은 비밀을 내지 않는다. Standard Webhooks 로 서명한
#            호출은 202 이고 같은 delivery id 의 재시도는 같은 run_id 다. 서명 없음, 5분 넘은 타임스탬프, 틀린 비밀, 받은
#            호출의 서명을 새 delivery id 로 다시 보낸 것은 모두 401 hook_signature_invalid 다. 웹훅 실행은 succeeded 다
#   STEP 5   승인 규칙의 Tool 을 부르는 u1 의 실행이 waiting_approval 에 선다. u2 의 받은 결정함 목록(runs=only, status=pending,
#            all_users)에 그 세션이 실행과 마감을 달고 있고, u2 의 채팅 목록에는 없다. u2 가 승인하면 succeeded 다
#   STEP 6   같은 실행을 아무도 결정하지 않으면 마감(마지막 턴 + TTL 120초 - 60초)에 refused(approval_deadline)이고, 그 뒤
#            u2 의 결정은 409 run_closed 다
#   STEP 7   u1 의 runs-as-me 가 그 일정과 트리거를 테넌트·Agent 이름과 함께 보인다. u2 는 u1 의 트리거를 멈추지 못하고(404),
#            u1 은 멈춘다. 멈춘 트리거에 새 delivery id 로 서명한 호출은 409 trigger_paused 다
#   STEP 8   u2 가 만든 일정은 u2 를 테넌트에서 빼면 곧바로 멈춘다(acts_as_lost_access, 원인 role)
#   STEP 9   u1 을 viewer 로 낮추면 지금 실행은 403 이고, u1 의 새 일정이 울리면 그 실행이 cancelled(acts_as_lost_access)로
#            끝나며 u1 의 일정들이 멈춘다(원인 role)
#   STEP 10  트리거의 비밀이 플랫폼 로그, Agent 로그, 감사 기록, MongoDB 의 트리거·실행·비밀 장부 문서 어디에도 없다. 감사에는
#            받은 호출(hook_accepted, 그 run_id)과 막은 호출(hook_refused, 수 4 이상)이 있다
#   정리     ConfigMap 을 먼저 되돌린다(굴리기 전까지 떠 있는 파드가 스텁 provider 를 걷을 수 있다). Agent(그 일정·트리거·웹훅
#            Secret 이 함께 지워진다)와 실행 세션의 Redis 키, MCP Server, Backend API, Database, 두 사람과 소속, 스텁 provider
#            (죽은 앞 실행이 남긴 것 포함)를 지운 뒤 플랫폼을 굴리고 ConfigMap 이 돌아갔는지 본다. 테넌트 네임스페이스는 다음
#            실행이 다시 쓴다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/e2e_json.sh"
. "$HERE/lib/hosts.sh"

TARGET="${E2E_TARGET:-install}"
[ "$TARGET" = install ] || { echo "e2e-runs 는 설치본만 대상이다 (E2E_TARGET=install, got $TARGET)"; exit 1; }
install_env_defaults
CTX="${INSTALL_CONTEXT:-}"
TENANT="${E2E_RUNS_TENANT:-e2e-runs}"
BASE="https://${DAEGAM_HOST:-}"
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"
SPEC="$ROOT/mcp-runtime/tests/fixtures/petstore.openapi.yaml"
U1="runs-u1-e2e"; U2="runs-u2-e2e"
STUB="e2e-runs-stub"; RDS_NAME="runs-redis-e2e"; BP_NAME="runs-petstore"; MS_NAME="runs-petstore"; AG_NAME="runs-e2e"
# 실행의 전제(P41 10절): 세션 예산. 마감 단계의 세션 TTL 은 Agent 하나로 모든 단계를 돌 만큼 길고 기다릴 만큼 짧다
BUDGET_TOKENS=200000; SESSION_TTL=120
# 승인을 묻는 Tool. addPet 은 POST 라 쓰기이고, 승인 규칙이 있어야 트리거를 둘 수 있다(P41 4절)
TOOL="addPet"
# 기다림의 한도와 간격(초). 실행 기록의 RUN_WAIT·RUN_POLL 은 lib/e2e_runs.sh 에 있다
# memory 바인딩이 TLS 로 옮겨 가기까지 (e2e_install STEP 8 과 같다)
RUN_TLS_WAIT="${E2E_RUNS_AGENT_TLS_WAIT:-240}"
# 지금 실행이 사람 이름으로 돌 때까지(MCP Server 의 audience 전환) 다시 넣는 한도와 간격
RUN_PERSON_WAIT="${E2E_RUNS_PERSON_WAIT:-480}"; RUN_PERSON_POLL=30
# 발사 워커는 15초마다 돈다. 울림을 쏜 뒤 한 틱이 더 지나도록 기다린다
RUN_FIRE_TICK_WAIT=20
# 런타임은 턴이 끝날 때 실행 세션을 닫고, 기록은 그보다 먼저 turn_end 이벤트로 끝날 수 있다. 닫힘이 보이기까지 1초씩 본다
RUN_CLOSE_TRIES=10
# 묶인 사람이 접근을 잃은 뒤 일정이 멈추기까지. 소속 훅과 시작 워커의 확인은 곧바로다
RUN_LOST_ACCESS_WAIT=30
# 마감은 마지막 턴에서 60초 뒤이고 지켜보기 워커는 15초마다 돈다. TTL 에 180초를 더하면 둘을 여유 있게 덮는다
RUN_DEADLINE_WAIT=$((SESSION_TTL + 180))
# 서명이 낡았다고 볼 만큼 지난 타임스탬프(초). 플랫폼의 허용은 5분이다
STALE_SECONDS=600
# 감사의 since 를 이 게이트의 시작보다 이만큼 앞에 둔다. 막은 호출은 분의 처음 시각으로 센다
AUDIT_PAD=60
# 잠들기 라우트를 다시 부르는 한도(초). 턴이 리스를 쥔 동안(agent_busy)이나 깨우는 중(state_conflict)이면 다시 부른다
SLEEP_ROUTE_WAIT=120
# 시작이 잠깐 실패한 실행의 까닭. 시작 워커가 다시 해도 실패했으면 STEP 2 가 새 실행을 넣는다(P41 5절)
START_TRANSIENT=" no_turn_token tools_unavailable http_502 http_503 http_504 runtime_unreachable "

WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-runs.XXXXXX")"
K=("${KUBECTL:-kubectl}" --context "$CTX")
AUTH=()
ADMIN_TOKEN=""; U1_TOKEN=""; U2_TOKEN=""
U1_ID=""; U2_ID=""
HOME_ID=""; RDS_ID=""; BP_ID=""; MS_ID=""; AG_ID=""; AG_NS=""; AG_K8S=""
TRIG_ID=""; HOOK_PATH=""; SECRET=""; HOOK_RUN=""; HOOK_TS=""
S1=""; S2=""; S3=""
START=""
STUB_BEFORE=""      # 바꾸기 전 DAEGAM_ALLOW_STUB_PROVIDER. 정리가 돌아갔는지 맞댄다
CONFIG_CHANGED=0    # ConfigMap 을 바꿨다

# wait_http 는 여기에 있다
. "$HERE/lib/e2e_pf.sh"
. "$HERE/lib/e2e_report.sh"
# api·api_as·as_person·jq_·find_id·api_delete·wait_ready·wait_gone·upload_backend_api·register_data_service·save_doc·
# deploy_doc·require_features·ensure_stub_provider·drop_stub_provider 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·msg·stub_call 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# platform_config·roll_platform_api·config_window_plan·config_window_apply·config_window_restore·AGENT_POD_SELECTOR
# 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# install_run_passwords·local_login_any·local_fresh_member·local_set_member·platform_connect 는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
install_run_passwords
# cluster_now·platform_mongo_here·platform_mongo 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# redis_ds_body·redis_agent_clean·wait_memory_tls 는 여기에 있다
. "$HERE/lib/e2e_redis.sh"
# RUN_TERMINAL·runs_on·run_now·run_wait·run_field·source_run·run_sessions·run_schedule_due·schedule_moved_on·
# audit_since·run_documents·secret_leaks·hook_audit 는 여기에 있다
. "$HERE/lib/e2e_runs.sh"

# hook_now STEP : 웹훅 타임스탬프를 클러스터의 시계로 HOOK_TS 에 둔다. 서명의 5분 허용은 플랫폼의 시계로 잰다
hook_now() {
  local now
  now="$(cluster_now)" || die "$1" "reading the platform pod's clock failed"
  HOOK_TS="$(epoch_of "$now")"
}

# hook_sign ID TS [KEY] : 트리거의 비밀(SECRET)로 $WORK/hook-body.json 에 Standard Webhooks 서명을 찍는다. KEY 를 주면 그
# 비밀이다. 비밀은 표준 입력으로 넘겨 프로세스 인자에 남기지 않는다
hook_sign() {
  printf '%s' "${3:-$SECRET}" | python3 -c 'import base64, hashlib, hmac, sys
secret = sys.stdin.read()
key = base64.b64decode(secret[len("whsec_"):])
signed = ("%s.%s." % (sys.argv[1], sys.argv[2])).encode() + open(sys.argv[3], "rb").read()
print(base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode())' "$1" "$2" "$WORK/hook-body.json"
}

# hook_post OUT ID TS [SIG] -> 상태 코드. $WORK/hook-body.json 을 트리거의 호출 주소로 보내고 답은 $WORK/OUT.json 에 둔다.
# SIG 가 비면 서명 머리를 싣지 않는다. 보내는 쪽처럼 인증 머리는 없다
hook_post() {
  local sig=()
  [ -z "${4:-}" ] || sig=(-H "webhook-signature: v1,$4")
  curl -sS -o "$WORK/$1.json" -w '%{http_code}' -X POST "$BASE$HOOK_PATH" -H "webhook-id: $2" \
    -H "webhook-timestamp: $3" ${sig[@]+"${sig[@]}"} -H 'Content-Type: application/json' \
    --data-binary "@$WORK/hook-body.json" || true
}

# refused_401 STEP WHAT CODE OUT : 그 호출이 401 hook_signature_invalid 여야 한다
refused_401() {
  [ "$3" = 401 ] && [ "$(error_code "$WORK/$4.json")" = hook_signature_invalid ] \
    || die "$1" "$2 -> $3 $(cat "$WORK/$4.json") (want 401 hook_signature_invalid)"
}

# schedule_state ID -> 관리자가 읽은 그 일정의 "paused paused_reason paused_cause"
schedule_state() {
  [ "$(api GET "/agents/$AG_ID/schedules")" = 200 ] || return 1
  jq_ "next((f\"{s['paused']} {s['paused_reason']} {s['paused_cause']}\" for s in d['schedules'] if s['id'] == '$1'), 'missing')"
}

# wait_lost_access STEP ID : 그 일정이 RUN_LOST_ACCESS_WAIT 초 안에 묶인 사람의 접근을 잃어(원인 role) 멈춰야 한다
wait_lost_access() {
  local state="" i
  for ((i = 0; i < RUN_LOST_ACCESS_WAIT / 3; i++)); do
    state="$(schedule_state "$2" || true)"
    [ "$state" = "True acts_as_lost_access role" ] && return 0
    sleep 3
  done
  die "$1" "schedule $2 is '$state', want 'True acts_as_lost_access role'"
}

# waiting_run STEP : u1 의 지금 실행이 승인 규칙의 Tool 을 불러 waiting_approval 에 서야 한다. 세션을 RUN_SID 에 남긴다
waiting_run() {
  as_person "$U1_TOKEN" run_now "$1" "$AG_ID" "$(stub_call "$TOOL" '{"name": "e2e-runs"}')"
  run_wait "$1" "$AG_ID" "$RUN_ID" "waiting_approval $RUN_TERMINAL"
  [ "$RUN_STATUS" = waiting_approval ] \
    || die "$1" "run $RUN_ID ended $RUN_STATUS ($(run_field 'd.get("reason")')) instead of waiting for approval" "$(cat "$WORK/run.json")"
  RUN_SID="$(run_field 'd["session_id"]')"
}

# 만든 것을 걷는다. 관리자 토큰이 없으면(로그인 전에 끝났으면) 걷을 것도 없다
teardown() {
  [ ${#AUTH[@]} -gt 0 ] || return 0
  local uid
  if [ -n "$AG_ID" ]; then
    # Agent 를 지우면 실행 목록이 404 라 실행 세션을 먼저 읽는다. Agent 삭제는 그 일정·트리거·웹훅 Secret 을 함께 지운다
    run_sessions "$AG_ID" || echo "!! the run sessions of $AG_ID could not be read; their write_keys_zset keys may be left"
    api_delete "/agents/$AG_ID"
    wait_gone "/agents/$AG_ID" 180 || echo "!! Agent $AG_NAME ($AG_ID) may be left in tenant $TENANT"
    if [ -n "$RDS_ID" ]; then
      redis_agent_clean "$AG_ID" ${RUN_SIDS[@]+"${RUN_SIDS[@]}"} \
        || echo "!! Redis keys and search indexes of daegam-agent:$AG_ID: and of its run sessions may be left"
    fi
  fi
  if [ -n "$MS_ID" ]; then
    api_delete "/mcp-servers/$MS_ID"
    wait_gone "/mcp-servers/$MS_ID" 180 || echo "!! MCP Server $MS_NAME ($MS_ID) may be left in tenant $TENANT"
  fi
  [ -z "$BP_ID" ] || api_delete "/backend-apis/$BP_ID"
  [ -z "$RDS_ID" ] || api_delete "/data-services/$RDS_ID"
  for uid in "$U1_ID" "$U2_ID"; do
    [ -n "$uid" ] || continue
    local_set_member "$TENANT" "$uid" "" || echo "!! the membership of $uid in $TENANT may be left"
    api_delete "/users/$uid"
  done
  # 죽은 앞 실행이 남긴 것도 걷는다. 스텁을 다시 막은 뒤에는 그것이 남은 목록을 저장하는 모든 Platform Settings 저장이
  # 거절된다
  drop_stub_provider "$STUB" || true
}

cleanup() {
  # ConfigMap 을 먼저 되돌린다. 굴리기 전까지 떠 있는 파드는 스텁을 받으므로 스텁 provider 를 걷는 저장이 된다
  config_window_restore || true
  teardown
  if [ "$CONFIG_ROLL_PENDING" = 1 ]; then
    if roll_platform_api; then
      CONFIG_ROLL_PENDING=0
      echo ">> ConfigMap $AUTH_MODE_CONFIGMAP restored, platform rolled"
    else
      echo "!! roll the platform by hand: kubectl --context $CTX -n $PLATFORM_NS rollout restart deploy/daegam-platform"
    fi
  fi
  if [ "$CONFIG_CHANGED" = 1 ] && [ "$(platform_config DAEGAM_ALLOW_STUB_PROVIDER || true)" != "$STUB_BEFORE" ]; then
    echo "!! ConfigMap $AUTH_MODE_CONFIGMAP is not back to DAEGAM_ALLOW_STUB_PROVIDER='$STUB_BEFORE'. $CONFIG_RESTORE_HINT"
  fi
  rm -rf "$WORK"
}
on_exit cleanup

echo ">> context=$CTX tenant=$TENANT api=$API"

# ---------- 0. 관리자, 실행 켜짐, 스텁 허용 ----------
platform_connect 0
runs_on || die 0 "GET /me/runs-as-me -> $(api GET /me/runs-as-me): runs are off in this deployment (DAEGAM_RUNS_ENABLED)"
platform_mongo_here || die 0 "no MongoDB pod mongodb-0 in $PLATFORM_NS, so the schedule cannot be made due and the documents cannot be read"
STUB_BEFORE="$(platform_config DAEGAM_ALLOW_STUB_PROVIDER)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
# 설치본은 스텁을 막는다(DAEGAM_ALLOW_STUB_PROVIDER 가 없거나 false 다). 이미 열려 있으면 앞 실행이 되돌리지 못한 것이라
# 되돌릴 값을 모른다
[ "$STUB_BEFORE" != true ] \
  || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP already has DAEGAM_ALLOW_STUB_PROVIDER=true (left by an earlier run?): set it back to false, roll the platform and run again"
config_window_plan DAEGAM_ALLOW_STUB_PROVIDER=true || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
CONFIG_CHANGED=1
echo ">> ConfigMap $AUTH_MODE_CONFIGMAP allows stub providers for this gate (restored on exit). If this run dies, $CONFIG_RESTORE_HINT"
config_window_apply || die 0 "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
roll_platform_api || die 0 "the platform did not come back after allowing stub providers"
START="$(cluster_now)" || die 0 "reading the platform pod's clock failed"
pass 0 "admin signed in at $BASE; runs on; stub providers allowed for this gate, platform rolled (start $START)"

# ---------- 1. 사람 둘과 실행의 전제를 갖춘 Agent ----------
U1_ID="$(local_fresh_member "$U1" "$TENANT")" || die 1 "creating $U1 in $TENANT failed"
U2_ID="$(local_fresh_member "$U2" "$TENANT")" || die 1 "creating $U2 in $TENANT failed"
for uid in "$U1_ID" "$U2_ID"; do
  local_set_member "$TENANT" "$uid" developer || die 1 "making $uid a developer of $TENANT failed"
done
# 첫 로그인이라 서버가 강제하는 비밀번호 바꾸기를 지난다
U1_TOKEN="$(local_login_any "$U1" "$LOCAL_PW")"; [ -n "$U1_TOKEN" ] || die 1 "login as $U1 failed"
U2_TOKEN="$(local_login_any "$U2" "$LOCAL_PW")"; [ -n "$U2_TOKEN" ] || die 1 "login as $U2 failed"
ensure_stub_provider 1 "$STUB"
code="$(api GET /clusters)"; [ "$code" = 200 ] || die 1 "GET /clusters -> $code"
HOME_ID="$(find_id home)"; [ -n "$HOME_ID" ] || die 1 "no home cluster"
# 본문을 변수에 먼저 둔다. register_data_service 의 인자 자리에서 만들면 실패해도 set -e 가 멈추지 않는다
ds_body="$(redis_ds_body "$RDS_NAME" "$HOME_ID")" || die 1 "reading the Redis Secret or CA failed"
register_data_service 1 "$RDS_NAME" "$ds_body"
unset ds_body
RDS_ID="$DATA_SERVICE_ID"
upload_backend_api 1 "$BP_NAME" "$SPEC" "자동 실행 펫스토어"
BP_ID="$BACKEND_API_ID"
save_doc 1 /mcp-servers "$MS_NAME" \
  "{\"name\": \"$MS_NAME\", \"display_name\": \"자동 실행 펫스토어\", \"backend_profile_id\": \"$BP_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": [\"$TOOL\"]}"
MS_ID="$SAVED_ID"
HOME_DEPLOY="{\"cluster_id\": \"$HOME_ID\"}"
deploy_doc 1 /mcp-servers "$MS_ID" "$HOME_DEPLOY"
echo ">> waiting up to ${READY_TIMEOUT}s for MCP Server $MS_NAME"
wait_ready "/mcp-servers/$MS_ID" 1
code="$(api GET /agents/templates)"; [ "$code" = 200 ] || die 1 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "자동 실행 에이전트" \
  --provider "$STUB" --source "mcp_instance:$MS_ID" --allow "$TOOL" --approval-for "$TOOL" --ds-id "$RDS_ID" \
  --session-tokens "$BUDGET_TOKENS" --session-ttl "$SESSION_TTL"
save_doc 1 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
deploy_doc 1 /agents "$AG_ID" "$HOME_DEPLOY"
echo ">> waiting up to ${READY_TIMEOUT}s for Agent $AG_NAME"
wait_ready "/agents/$AG_ID" 1
require_features 1 runs
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"
wait_memory_tls 1 "$AG_NS" "$AG_K8S" "$RUN_TLS_WAIT"
pass 1 "$U1 ($U1_ID) and $U2 ($U2_ID) are developers of $TENANT; Agent $AG_NAME ($AG_ID) Ready with runs [$FEATURES], $TOOL asks approval, memory Redis over TLS ($MEMORY_TLS_ENV), session budget $BUDGET_TOKENS, TTL ${SESSION_TTL}s"

# ---------- 2. 지금 실행 ----------
# 스텁 모델은 JSON 조각이 없으면 첫 MCP Tool 을 부른다. sleep 조각이 있으면 Tool 없이 글로 답하므로,
# 끝까지 done 으로 가야 하는 메시지는 모두 {"sleep": 0} 을 싣는다(agent-runtime/runtime/stub.py)
DONE_FRAGMENT='{"sleep": 0}'
RUN_TEXT="e2e-runs run now $DONE_FRAGMENT"
tries=0; deadline=$((SECONDS + RUN_PERSON_WAIT))
while :; do
  as_person "$U1_TOKEN" run_now 2 "$AG_ID" "$RUN_TEXT"
  tries=$((tries + 1))
  run_wait 2 "$AG_ID" "$RUN_ID" "$RUN_TERMINAL"
  kind="$(run_field 'd["acts_as"]["kind"]')"
  [ "$RUN_STATUS $kind" != "succeeded person" ] || break
  # Agent 의 이름으로 끝났으면 그 까닭(cause)을, 실패했으면 실패의 까닭(reason)을 본다
  if [ "$RUN_STATUS" = succeeded ]; then why="$(run_field 'd["acts_as"].get("cause")')"; else why="$(run_field 'd.get("reason")')"; fi
  case "$START_TRANSIENT" in
    *" $why "*) [ "$SECONDS" -lt "$deadline" ] \
      || die 2 "run now still $RUN_STATUS ($why) after $tries runs and ${RUN_PERSON_WAIT}s, want succeeded as the person $U1" "$(cat "$WORK/run.json")" ;;
    *) die 2 "run now $RUN_ID ended $RUN_STATUS as $kind ($why), want succeeded as the person $U1" "$(cat "$WORK/run.json")" ;;
  esac
  sleep "$RUN_PERSON_POLL"
done
RUN_SID="$(run_field 'd["session_id"]')"
[ "$(run_field "d['acts_as']['sub'], d['source'], 'message' in d, d['input_bytes']")" = "$U1_ID manual False ${#RUN_TEXT}" ] \
  || die 2 "run record: $(cat "$WORK/run.json") (want acts_as $U1_ID, source manual, no message, input_bytes ${#RUN_TEXT})"
closed=""
for ((i = 0; i < RUN_CLOSE_TRIES; i++)); do
  code="$(api_as "$U1_TOKEN" GET "/agents/$AG_ID/sessions/$RUN_SID")"
  closed="$code $(jq_ 'd.get("session", {}).get("status")' || true)"
  [ "$closed" = "200 closed" ] && break
  sleep 1
done
[ "$closed" = "200 closed" ] || die 2 "run session $RUN_SID: $closed (want 200 closed)"
code="$(api_as "$U1_TOKEN" GET "/agents/$AG_ID/sessions")"
[ "$code" = 200 ] && [ "$(jq_ "any(s['session_id'] == '$RUN_SID' for s in d['sessions'])")" = False ] \
  || die 2 "the run session $RUN_SID is in $U1's chat list ($code)"
code="$(api_as "$U1_TOKEN" GET "/agents/$AG_ID/sessions?runs=include")"
[ "$code" = 200 ] && [ "$(jq_ "any(s['session_id'] == '$RUN_SID' for s in d['sessions'])")" = True ] \
  || die 2 "the run session $RUN_SID is not in $U1's list with runs=include ($code)"
pass 2 "run now $RUN_ID succeeded as $U1 (person, after $tries runs); the record keeps $(run_field 'd["input_bytes"]') bytes and no message; session $RUN_SID closed, out of the chat list, in runs=include"

# ---------- 2s. 잠든 Agent 의 지금 실행 (P44 17절) ----------
# 다른 단계의 Agent 는 스위치를 끈 채 배포했다(lib/e2e_api.sh deploy_doc). 이 단계만 켰다가 다시 끈다
code="$(api POST "/agents/$AG_ID/redeploy" '{"scale_to_zero": true}')"
[ "$code" = 200 ] || die 2s "POST /agents/$AG_ID/redeploy (scale_to_zero on) -> $code"
wait_ready "/agents/$AG_ID" 2s
why="$(jq_ '((d.get("status") or {}).get("sleep") or {}).get("ineligible")')"
if [ "$why" = setting_off ]; then
  cond_skip 2s "the install turned scale to zero off (DAEGAM_AGENT_SCALE_TO_ZERO=false)"
else
  [ "$why" = None ] || die 2s "agent $AG_NAME cannot sleep with its switch on: ineligible $why"
  sleep_route 2s "$AG_ID"
  as_person "$U1_TOKEN" run_now 2s "$AG_ID" "$RUN_TEXT"
  run_wait 2s "$AG_ID" "$RUN_ID" "$RUN_TERMINAL"
  [ "$RUN_STATUS" = succeeded ] \
    || die 2s "run now on the sleeping Agent ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
  WOKEN_RUN="$RUN_ID"
  wait_ready "/agents/$AG_ID" 2s
  pass 2s "agent $AG_NAME slept through the route (status Sleeping); $U1's run now $WOKEN_RUN woke it and succeeded"
fi
code="$(api POST "/agents/$AG_ID/redeploy" '{"scale_to_zero": false}')"
[ "$code" = 200 ] || die 2s "POST /agents/$AG_ID/redeploy (scale_to_zero off) -> $code"
wait_ready "/agents/$AG_ID" 2s

# ---------- 3. 일정 ----------
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/schedules" "$(schedule_body '*/2 * * * *' 'too often')")"
[ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = schedule_too_frequent ] \
  || die 3 "a rule every 2 minutes -> $code (want 422 schedule_too_frequent)"
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/schedules" "$(schedule_body '61 * * * *' 'unreadable')")"
[ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = schedule_invalid_cron ] \
  || die 3 "an unreadable rule -> $code (want 422 schedule_invalid_cron)"
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/schedules" "$(schedule_body '0 9 * * *' "e2e-runs schedule $DONE_FRAGMENT")")"
[ "$code" = 201 ] || die 3 "POST /agents/$AG_ID/schedules -> $code"
S1="$(jq_ 'd["id"]')"
[ "$(jq_ "d['acts_as']['sub'], d['paused']")" = "$U1_ID False" ] || die 3 "schedule $S1 is not bound to $U1 or is paused"
run_schedule_due "$S1" || die 3 "making schedule $S1 due in MongoDB failed"
source_run 3 "$AG_ID" "$S1"
SLOT_RUN="$RUN_ID"
run_wait 3 "$AG_ID" "$SLOT_RUN" "$RUN_TERMINAL"
[ "$RUN_STATUS" = succeeded ] || die 3 "the slot run $SLOT_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
SLOT="$(run_field 'd["source"], d["scheduled_for"]')"
case "$SLOT" in "schedule "?*) ;; *) die 3 "the slot run $SLOT_RUN has source and slot '$SLOT'" ;; esac
# 발사 틱이 한 번 더 돈 뒤에도 그 일정의 기록은 하나다
sleep "$RUN_FIRE_TICK_WAIT"
code="$(api GET "/agents/$AG_ID/runs?limit=500")"; [ "$code" = 200 ] || die 3 "GET /agents/$AG_ID/runs -> $code"
n="$(jq_ "len([r for r in d['runs'] if r.get('source_id') == '$S1' and r['status'] != 'skipped'])")"
[ "$n" = 1 ] || die 3 "schedule $S1 has $n run records, want 1"
code="$(api GET "/agents/$AG_ID/schedules")"; [ "$code" = 200 ] || die 3 "GET /agents/$AG_ID/schedules -> $code"
schedule_moved_on "$WORK/body" "$S1" "$SLOT_RUN" \
  || die 3 "schedule $S1 did not move on to a later slot with last_run_id $SLOT_RUN"
pass 3 "too frequent 422, unreadable 422; schedule $S1 made due fired one record $SLOT_RUN (slot ${SLOT#schedule }) that succeeded; next_run_at moved on"

# ---------- 4. 웹훅 트리거 ----------
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/triggers" \
  '{"name": "e2e-runs hook", "template": "{\"sleep\": 0} e2e-runs webhook: {{payload./text}}", "expires_in_days": 1}')"
[ "$code" = 201 ] || die 4 "POST /agents/$AG_ID/triggers -> $code"
TRIG_ID="$(jq_ 'd["id"]')"; HOOK_PATH="$(jq_ 'd["path"]')"
SECRET="$(jq_ 'd["secret"]')"
# 만든 답의 본문은 비밀을 품으므로 지운다. 뒤의 die 가 그 본문을 찍지 않게 한다
: >"$WORK/body"
case "$SECRET" in whsec_?*) ;; *) die 4 "trigger $TRIG_ID answered no whsec_ secret" ;; esac
code="$(api_as "$U1_TOKEN" GET "/agents/$AG_ID/triggers")"
[ "$code" = 200 ] && [ "$(jq_ "any('secret' in t for t in d['triggers'])")" = False ] \
  || die 4 "the trigger list shows a secret or failed ($code)"
printf '{"text": "hello from e2e-runs"}' >"$WORK/hook-body.json"
# delivery id 는 실행마다 새것이다. 서명이 그 id 와 타임스탬프와 본문을 덮는다
hook_now 4
TS="$HOOK_TS"
SIG="$(hook_sign "e2e-runs-$TS-1" "$TS")"
code="$(hook_post hook1 "e2e-runs-$TS-1" "$TS" "$SIG")"
[ "$code" = 202 ] || die 4 "a signed call -> $code $(cat "$WORK/hook1.json")"
HOOK_RUN="$(json_of "$WORK/hook1.json" 'd["run_id"]')"
code="$(hook_post hook2 "e2e-runs-$TS-1" "$TS" "$SIG")"
[ "$code" = 202 ] && [ "$(json_of "$WORK/hook2.json" 'd["run_id"]')" = "$HOOK_RUN" ] \
  || die 4 "the same delivery again -> $code $(cat "$WORK/hook2.json") (want 202 with run $HOOK_RUN)"
code="$(hook_post nosig "e2e-runs-$TS-2" "$TS")"
refused_401 4 "a call without a signature" "$code" nosig
OLD=$((TS - STALE_SECONDS))
code="$(hook_post stale "e2e-runs-$TS-3" "$OLD" "$(hook_sign "e2e-runs-$TS-3" "$OLD")")"
refused_401 4 "a call signed ${STALE_SECONDS}s ago" "$code" stale
OTHER_KEY="whsec_$(python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
code="$(hook_post wrong "e2e-runs-$TS-4" "$TS" "$(hook_sign "e2e-runs-$TS-4" "$TS" "$OTHER_KEY")")"
refused_401 4 "a call signed with another secret" "$code" wrong
code="$(hook_post replay "e2e-runs-$TS-5" "$TS" "$SIG")"
refused_401 4 "the accepted call's signature under a new delivery id" "$code" replay
run_wait 4 "$AG_ID" "$HOOK_RUN" "$RUN_TERMINAL"
[ "$RUN_STATUS" = succeeded ] || die 4 "the webhook run $HOOK_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
[ "$(run_field "d['source'], d['source_id'], d['acts_as']['sub']")" = "webhook $TRIG_ID $U1_ID" ] \
  || die 4 "webhook run record: $(cat "$WORK/run.json")"
pass 4 "trigger $TRIG_ID showed a whsec_ secret once, the list shows none; signed call 202 run $HOOK_RUN, the same delivery the same run; no signature, stale, wrong secret and replay under a new id all 401 hook_signature_invalid; the webhook run succeeded as $U1"

# ---------- 5. 받은 결정함에서 둘째 developer 가 승인 ----------
waiting_run 5
WAIT_RUN="$RUN_ID"
code="$(api_as "$U2_TOKEN" GET "/agents/$AG_ID/sessions?runs=only&status=pending&all_users=true")"
[ "$code" = 200 ] || die 5 "$U2's inbox list -> $code"
row="$(jq_ "json.dumps(next((s.get('run') for s in d['sessions'] if s['session_id'] == '$RUN_SID'), None))")"
[ "$(json_of - "d is not None and d['id'] == '$WAIT_RUN' and d['status'] == 'waiting_approval' and bool(d.get('deadline'))" <<<"$row")" = True ] \
  || die 5 "$U2 does not see run $WAIT_RUN waiting with a deadline in the inbox: $row"
code="$(api_as "$U2_TOKEN" GET "/agents/$AG_ID/sessions?all_users=true")"
[ "$code" = 200 ] && [ "$(jq_ "any(s['session_id'] == '$RUN_SID' for s in d['sessions'])")" = False ] \
  || die 5 "the run session $RUN_SID is in $U2's chat list ($code)"
code="$(as_person "$U2_TOKEN" chat "/agents/$AG_ID/sessions/$RUN_SID/decisions" '{"decisions": [{"type": "approve"}]}')"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 5 "$U2's decision -> $code"; }
run_wait 5 "$AG_ID" "$WAIT_RUN" "$RUN_TERMINAL"
[ "$RUN_STATUS" = succeeded ] || die 5 "the approved run $WAIT_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
pass 5 "run $WAIT_RUN waited for approval; $U2 saw it in the inbox with deadline $(json_of - 'd["deadline"]' <<<"$row"), not in the chat list, and approved it; it succeeded"

# ---------- 6. 마감 ----------
waiting_run 6
LATE_RUN="$RUN_ID"; DEADLINE="$(run_field 'd["deadline"]')"
echo ">> run $LATE_RUN waits for approval until $DEADLINE"
run_wait 6 "$AG_ID" "$LATE_RUN" "$RUN_TERMINAL" "$RUN_DEADLINE_WAIT"
[ "$RUN_STATUS $(run_field 'd.get("reason")')" = "refused approval_deadline" ] \
  || die 6 "run $LATE_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")')), want refused (approval_deadline)" "$(cat "$WORK/run.json")"
code="$(SSE_OUT="$WORK/late.json" as_person "$U2_TOKEN" chat "/agents/$AG_ID/sessions/$RUN_SID/decisions" '{"decisions": [{"type": "approve"}]}')"
[ "$code" = 409 ] && [ "$(error_code "$WORK/late.json")" = run_closed ] \
  || die 6 "a decision after the deadline -> $code $(cat "$WORK/late.json") (want 409 run_closed)"
pass 6 "run $LATE_RUN refused at its deadline $DEADLINE (approval_deadline); $U2's later decision 409 run_closed"

# ---------- 7. 나를 대신하는 실행 ----------
code="$(api_as "$U1_TOKEN" GET /me/runs-as-me)"; [ "$code" = 200 ] || die 7 "$U1's runs-as-me -> $code"
[ "$(jq_ "any(s['id'] == '$S1' and s['tenant'] == '$TENANT' and s['agent_name'] == '$AG_NAME' for s in d['schedules']), any(t['id'] == '$TRIG_ID' and 'secret' not in t for t in d['triggers'])")" = "True True" ] \
  || die 7 "$U1's runs-as-me lacks schedule $S1 or trigger $TRIG_ID with tenant and Agent name"
code="$(api_as "$U2_TOKEN" POST "/me/runs-as-me/triggers/$TRIG_ID/pause")"
[ "$code" = 404 ] || die 7 "$U2 pausing $U1's trigger through runs-as-me -> $code (want 404)"
code="$(api_as "$U1_TOKEN" POST "/me/runs-as-me/triggers/$TRIG_ID/pause")"
[ "$code" = 200 ] && [ "$(jq_ 'd["paused"]')" = True ] || die 7 "$U1 pausing trigger $TRIG_ID through runs-as-me -> $code"
hook_now 7
code="$(hook_post paused "e2e-runs-$HOOK_TS-6" "$HOOK_TS" "$(hook_sign "e2e-runs-$HOOK_TS-6" "$HOOK_TS")")"
[ "$code" = 409 ] && [ "$(error_code "$WORK/paused.json")" = trigger_paused ] \
  || die 7 "a signed call to the paused trigger -> $code $(cat "$WORK/paused.json") (want 409 trigger_paused)"
pass 7 "$U1's runs-as-me lists schedule $S1 and trigger $TRIG_ID with tenant $TENANT and Agent $AG_NAME; $U2 cannot pause it (404), $U1 can; the paused trigger answers 409 trigger_paused"

# ---------- 8. 테넌트에서 빠진 사람 ----------
code="$(api_as "$U2_TOKEN" POST "/agents/$AG_ID/schedules" "$(schedule_body '0 10 * * *' 'e2e-runs u2')")"
[ "$code" = 201 ] && [ "$(jq_ 'd["acts_as"]["sub"]')" = "$U2_ID" ] || die 8 "$U2's schedule -> $code"
S2="$(jq_ 'd["id"]')"
local_set_member "$TENANT" "$U2_ID" "" || die 8 "removing $U2 from $TENANT failed"
wait_lost_access 8 "$S2"
pass 8 "removing $U2 from $TENANT paused $U2's schedule $S2 at once (acts_as_lost_access, role)"

# ---------- 9. 낮춘 사람 ----------
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/schedules" "$(schedule_body '0 11 * * *' 'e2e-runs demoted')")"
[ "$code" = 201 ] || die 9 "$U1's second schedule -> $code"
S3="$(jq_ 'd["id"]')"
local_set_member "$TENANT" "$U1_ID" viewer || die 9 "making $U1 a viewer of $TENANT failed"
code="$(api_as "$U1_TOKEN" POST "/agents/$AG_ID/runs" "$(msg "$RUN_TEXT")")"
[ "$code" = 403 ] || die 9 "run now by the viewer $U1 -> $code (want 403)"
run_schedule_due "$S3" || die 9 "making schedule $S3 due in MongoDB failed"
source_run 9 "$AG_ID" "$S3"
DEMOTED_RUN="$RUN_ID"
run_wait 9 "$AG_ID" "$DEMOTED_RUN" "$RUN_TERMINAL"
[ "$RUN_STATUS $(run_field 'd.get("reason")')" = "cancelled acts_as_lost_access" ] \
  || die 9 "the demoted person's run $DEMOTED_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")')), want cancelled (acts_as_lost_access)" "$(cat "$WORK/run.json")"
wait_lost_access 9 "$S3"
wait_lost_access 9 "$S1"
pass 9 "the viewer $U1 cannot run now (403); $U1's next start $DEMOTED_RUN was cancelled (acts_as_lost_access) and paused schedules $S3 and $S1 (role)"

# ---------- 10. 비밀이 남지 않는다 ----------
AUDIT_SINCE="$(audit_since "$START" "$AUDIT_PAD")"
"${K[@]}" -n "$PLATFORM_NS" logs -l app=daegam-platform --all-containers --tail=-1 --max-log-requests=10 >"$WORK/platform.log" \
  || die 10 "reading the platform logs failed"
"${K[@]}" -n "$AG_NS" logs -l "$AGENT_POD_SELECTOR$AG_ID" --all-containers --tail=-1 >"$WORK/agent.log" \
  || die 10 "reading the Agent's logs failed"
code="$(API_OUT="$WORK/audit.json" api GET "/audit?since=$AUDIT_SINCE&limit=1000")"
[ "$code" = 200 ] || die 10 "GET /audit -> $code"
run_documents "$TENANT" "$AG_ID" "$WORK/mongo.json" \
  || die 10 "reading the trigger, run and ledger documents failed" "$(cat "$WORK/mongo.json" 2>/dev/null || true)"
leaks="$(printf '%s' "$SECRET" | secret_leaks "$WORK/platform.log" "$WORK/agent.log" "$WORK/audit.json" "$WORK/mongo.json")"
[ -z "$leaks" ] || die 10 "the trigger's secret appears in: $leaks"
read -r accepted refused <<<"$(hook_audit "$WORK/audit.json" "$HOOK_RUN" "$TENANT" "$AG_ID")"
{ [ "$accepted" = True ] && [ "$refused" -ge 4 ]; } \
  || die 10 "audit since $AUDIT_SINCE: hook_accepted for $HOOK_RUN $accepted, hook_refused count $refused (want True and at least 4)"
pass 10 "the secret is in no platform log ($(wc -l <"$WORK/platform.log" | tr -d ' ') lines), Agent log, audit row ($(json_of "$WORK/audit.json" 'len(d)') since $AUDIT_SINCE) or trigger, run or ledger document; audit has hook_accepted for $HOOK_RUN and $refused refused calls"

finish
