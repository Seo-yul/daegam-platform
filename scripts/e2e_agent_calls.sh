#!/usr/bin/env bash
# Agent 가 게시한 다른 Agent 를 Tool 로 부르는 호출의 e2e. 게시와 캔버스의 Agent 소스, 호출이 호출받은 Agent 의 실행으로 도는 것,
# 사슬의 거절, 부른 턴의 중지, 호출받은 Agent 의 승인, 실행 기록, 호출의 스위치를 본다. 대상은 둘이다.
#
#   make e2e-agent-calls                                               로컬 클러스터(태그의 클러스터 e2e 잡의 agent 조각)
#   make e2e-agent-calls E2E_TARGET=install E2E_INSTALL_USER=<관리자>   설치본(make e2e-install-extra 가 고르면 돈다)
#
# 로컬 클러스터는 header 모드 그대로 돈다(deploy-local 과 lab-up 뒤, lab 의 Redis 8 이 memory 다). header 모드의 호출은
# 호출받은 Agent 의 이름으로 돌므로 사람을 보는 단계(STEP 4 의 감사, STEP 6)는 조건부로 건너뛴다. 사슬(STEP 5)도 건너뛴다.
# header 모드의 호출은 늘 믿지 않는 입력이라 호출받은 Agent 의 다음 호출(쓰기 Tool)이 승인을 기다리기 때문이다. 설치본은 local
# 모드이고 플랫폼 곁의 Redis 를 memory 로 쓰며(e2e-runs 와 같다) 버리는 사람 둘(u1 developer, u2 viewer)을 테넌트에 만든다. 두
# 사람의 비밀번호는 실행마다 새로 만든다. 관리자 자격 증명은 e2e-install 과 같은 E2E_INSTALL_PASSWORD 나 키체인 항목
# daegam-e2e-install 이다.
#
# 모델은 스텁이다. 스텁은 메시지 안의 {"tool", "args"} 조각으로 Tool 을 부르므로, 부르는 Agent 에 보내는 메시지가 ask_* 호출을
# 싣고 그 message 가 호출받은 Agent 의 Tool 호출을 싣는다(agent-runtime/runtime/stub.py). Agent 는 넷이다. a 가 b 를, b 가
# Gateway 의 Tool 과 c 와 a 를, c 가 d 를 부른다. 넷 모두 호출의 메시지를 사람이 쓴 것으로 다루게(calls_trusted) 게시한다.
# 그래야 토큰 인증 모드에서 호출받은 Agent 의 다음 호출이 승인 없이 이어진다.
#
# 기다림의 한도(초)는 E2E_RUNS_WAIT(실행 기록, 기본 240), E2E_AGENT_CALLS_PERSON_WAIT(호출이 사람 이름으로 돌기까지, 480),
# E2E_AGENT_CALLS_TLS_WAIT(설치본의 memory 바인딩의 TLS 전환, 240)로 바꾼다.
#
# Gate:
#   STEP 0   플랫폼에 붙는다. 실행이 켜져 있고(GET /api/me/runs-as-me 200) 호출이 꺼져 있지 않다. ConfigMap 에 스텁 provider
#            허용을 이 게이트 동안만 두고(되돌리는 명령을 먼저 찍는다) 플랫폼을 굴린다. 설치본에 그 허용이 이미 열려 있으면
#            바꾸기 전에 실패한다
#   STEP 1   (설치본) u1·u2 를 테넌트에 둔다. 스텁 provider, Redis Database, petstore Backend API 와 그 MCP Server(get_pets,
#            addPet)를 배포해 게시하고 그것을 백엔드로 묶은 Gateway 를 배포한다
#   STEP 2   d, c, b, a 를 차례로 배포해 게시한다(memory, 세션 예산). c 는 d 를, b 는 Gateway 의 Tool(addPet 은 승인 규칙)과
#            c 를, a 는 b 를 Tool 로 붙인다. 그 뒤 b 에 a 를 더해 저장하면 경고 agent_call_cycle 이고 다시 배포한다. 런타임은
#            runs·turn_tokens·agent_tools 를 알리고, 허브는 넷을 종류 agent 로 보이며 b 의 항목의 참조에 a 가 있다
#   STEP 3   a 와의 대화에서 ask_calls_b 가 b 의 답을 돌려준다. b 의 실행 기록은 출처 agent, 부른 Agent a, 부른 세션이고
#            succeeded 다. 설치본에서는 u1 의 이름(acts_as person)으로 돈다. MCP Server 와 Gateway 가 위임 토큰을 받기 전의
#            실패(acts_as_unavailable 등)면 30초마다 새 턴으로 다시 본다
#   STEP 4   호출이 b 의 get_pets 까지 간다. 설치본은 Gateway 의 vMCP 감사에 그 호출이 u1(sub)과 b(act)로 남는다. 로컬은
#            감사의 사람을 조건부로 건너뛴다
#   STEP 5   (설치본) a → b → c 는 답이 돌아오고, a → b → a 는 agent_call_cycle, a → b → c → d 는 agent_call_too_deep 로
#            거절된다
#   STEP 6   (설치본) viewer u2 의 호출이 u2 의 이름으로 돈다
#   STEP 7   b 가 답하는 동안 a 의 턴을 중지하면 b 의 실행이 cancelled(caller_ended)로 끝난다
#   STEP 8   b 가 승인 규칙의 addPet 을 부르면 호출은 기다린다는 글로 곧 돌아오고 실행은 waiting_approval 로 받은 결정함에
#            선다. 승인하면 succeeded 다
#   STEP 9   b 의 실행 목록이 출처 agent 의 기록마다 부른 Agent 를 싣고, 끝난 호출의 기록에 원장의 토큰이 있다
#   STEP 10  d 의 게시를 해제하면 c 의 호출이 agent_source_unpublished 로 거절되고 c 의 상태가 같은 코드로 경고한다
#   STEP 11  호출을 끄면(DAEGAM_AGENT_CALLS_ENABLED=false) a 는 계속 대화하고 ask_calls_b 는 agent_calls_disabled 로 답하며
#            게시는 409 agent_calls_disabled 다
#   정리     ConfigMap 을 먼저 되돌린다. Agent 넷(게시 항목이 함께 지워진다)과 그 Redis 키, Gateway, MCP Server 의 게시와 그
#            서버, Backend API, Database, 설치본의 두 사람, 스텁 provider 를 지운 뒤 플랫폼을 굴린다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/e2e_json.sh"
. "$HERE/lib/hosts.sh"

TARGET="${E2E_TARGET:-orbstack}"
API_PORT="${E2E_API_PORT:-$PORT_AGENT_CALLS_API}"
case "$TARGET" in
  orbstack | local)
    TARGET=local
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-agent-calls}"
    BASE="http://127.0.0.1:${API_PORT}"; STUB="stub-e2e" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e-agent-calls}"
    BASE="https://${DAEGAM_HOST:-}"; STUB="e2e-agent-calls-stub" ;;
  *) echo "E2E_TARGET 은 orbstack(로컬 클러스터)이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"
SPEC="$ROOT/mcp-runtime/tests/fixtures/petstore.openapi.yaml"
U1="calls-u1-e2e"; U2="calls-u2-e2e"
DS_NAME="calls-redis-e2e"; BP_NAME="calls-petstore"; MS_NAME="calls-petstore"; GW_NAME="calls-gw"
NAME_A="calls-a"; NAME_B="calls-b"; NAME_C="calls-c"; NAME_D="calls-d"
# 게시한 항목이 캔버스에서 내는 Tool 이름이다. ask_ 뒤에 항목 이름의 - 를 _ 로 바꿔 붙인다
ASK_A="ask_calls_a"; ASK_B="ask_calls_b"; ASK_C="ask_calls_c"; ASK_D="ask_calls_d"
# b 가 Gateway 로 부르는 읽기 Tool 과 승인 규칙을 둔 쓰기 Tool
READ_TOOL="get_pets"; WRITE_TOOL="addPet"
# 호출의 전제인 세션 예산
BUDGET_TOKENS=200000
# 설치본의 memory 바인딩이 TLS 로 옮겨 가기까지 (e2e_runs 와 같다)
TLS_WAIT="${E2E_AGENT_CALLS_TLS_WAIT:-240}"
# 호출이 사람 이름으로 돌 때까지(MCP Server 와 Gateway 의 위임 토큰 전환) 다시 보는 한도와 간격
PERSON_WAIT="${E2E_AGENT_CALLS_PERSON_WAIT:-480}"; PERSON_POLL=30
# 호출의 시작이 잠깐 실패한 실행의 까닭. 그동안은 새 턴으로 다시 본다
START_TRANSIENT=" acts_as_unavailable no_turn_token tools_unavailable http_502 http_503 http_504 runtime_unreachable "
# 스텁이 답하기 전에 기다리는 초. 그동안 부른 턴을 중지한다(스텁의 상한은 90초다)
SLOW_SECONDS=60
# 부른 턴을 중지한 뒤 호출받은 실행이 취소되기까지(초). 위임의 끝이 곧바로 취소하고 지켜보기의 틱(15초)이 놓친 것을 거둔다
CANCEL_WAIT=90
# 원장의 토큰이 실행 기록에 붙기까지 다시 읽는 횟수(2초 간격)
USAGE_TRIES=30
# 런타임이 모델에게 주는 문장의 조각이다(agent-runtime/runtime/agent_calls.py WAITING_MESSAGE)
WAITING_TEXT="승인을 기다린다"
# 스텁이 Tool 없이 답하는 조각이다. 끝까지 done 으로 가야 하는 메시지가 싣는다
DONE_FRAGMENT='{"sleep": 0}'

WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-agent-calls.XXXXXX")"
K=("${KUBECTL:-kubectl}" --context "$CTX")
AUTH=()                            # header 모드는 Bearer 가 없다. 설치본은 로그인한 관리자다 (lib/e2e_api.sh 가 읽는다)
PF_API=""
ADMIN_TOKEN=""; U1_TOKEN=""; U2_TOKEN=""; U1_ID=""; U2_ID=""
HOME_ID=""; DS_ID=""; BP_ID=""; MS_ID=""; HMS=""; GW_ID=""
ID_A=""; ID_B=""; ID_C=""; ID_D=""; ITEM_A=""; ITEM_B=""; ITEM_C=""; ITEM_D=""
AG_NS=""; K8S_B=""
CALL_SID=""                        # ask_turn 이 남기는 부른 세션
STUB_BEFORE=""; CONFIG_CHANGED=0

# start_pf·stop_pf·restart_pf·kill_stale_pf·wait_port·wait_http 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"
. "$HERE/lib/e2e_report.sh"
# api·api_as·as_person·jq_·find_id·api_delete·wait_ready·wait_gone·error_code·upload_backend_api·register_data_service·
# save_doc·deploy_doc·require_features·ensure_stub_provider·drop_stub_provider·remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call·ask_call·chat_bg·bg_session_id·bg_stop 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab_database·rforget 은 여기에 있다 (로컬 클러스터의 Redis)
. "$HERE/lib/e2e_lab.sh"
# platform_config·restart_platform·roll_platform_api·config_window_*·wait_workloads_rolled 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# install_run_passwords·local_login_any·local_fresh_member·local_set_member·platform_connect 는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
[ "$TARGET" != install ] || install_run_passwords
# cluster_now·wait_gateway_gate·audit_expect 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# redis_ds_body·redis_agent_clean·wait_memory_tls 는 여기에 있다 (설치본의 Redis)
. "$HERE/lib/e2e_redis.sh"
# RUN_TERMINAL·runs_on·run_wait·run_field·call_run·run_sessions 는 여기에 있다
. "$HERE/lib/e2e_runs.sh"

# ask_turn STEP TOKEN MESSAGE : TOKEN 의 사람이 a 에게 MESSAGE 를 보내는 새 대화의 턴이다. 턴이 done 으로 끝나고 첫 Tool
# 결과가 ASK_B 의 것이어야 한다. 그 결과의 글(바깥 내용 감싸개 안)을 $WORK/answer 에, 상태를 TOOL_STATUS 에, 세션을 CALL_SID
# 에 남긴다
TOOL_STATUS=""
ask_turn() {
  local step="$1" token="$2" code
  code="$(as_person "$token" chat "/agents/$ID_A/chat" "$(msg "$3")")"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ 'first("tool_result")["name"]')" = "$ASK_B" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die "$step" "POST /agents/$ID_A/chat -> $code $(sse_ 'names'): no $ASK_B result and done"; }
  sse_ 'inner(first("tool_result")["content"])' >"$WORK/answer"
  TOOL_STATUS="$(sse_ 'first("tool_result")["status"]')"
  CALL_SID="$(sse_ 'first("done")["session_id"]')"
}

# answer_has TEXT : 마지막 ask_turn 의 Tool 결과에 TEXT 가 있으면 0 이다
answer_has() { grep -qF -- "$1" "$WORK/answer"; }

# agent_doc OUT NAME DISPLAY [FLAG...] : 스텁 모델, Redis memory, 세션 예산을 갖춘 Agent 의 저장 본문을 OUT 에 쓴다. FLAG 는
# lib/agent_body.py 의 것이다
agent_doc() {
  local out="$1" name="$2" display="$3"
  shift 3
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$out" --name "$name" --display "$display" --provider "$STUB" \
    --ds-id "$DS_ID" --session-tokens "$BUDGET_TOKENS" "$@"
}

# launch STEP NAME FILE : FILE 의 본문을 Agent NAME 으로 저장해 배포하고 Ready 를 기다린 뒤 id 를 LAUNCHED_ID 에 남긴다.
# 설치본은 memory 바인딩이 TLS 로 옮겨 가기를 기다린다. 끝나면 $WORK/body 가 그 문서의 GET 응답이다
LAUNCHED_ID=""
launch() {
  local step="$1" ns k8s
  save_doc "$step" /agents "$2" "$(cat "$3")"
  LAUNCHED_ID="$SAVED_ID"
  deploy_doc "$step" /agents "$LAUNCHED_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  echo ">> waiting up to ${READY_TIMEOUT}s for Agent $2"
  wait_ready "/agents/$LAUNCHED_ID" "$step"
  if [ "$TARGET" = install ]; then
    ns="$(jq_ 'd["k8s"]["namespace"]')"; k8s="$(jq_ 'd["k8s"]["name"]')"
    wait_memory_tls "$step" "$ns" "$k8s" "$TLS_WAIT"
    [ "$(api GET "/agents/$LAUNCHED_ID")" = 200 ] || die "$step" "GET /agents/$LAUNCHED_ID after the TLS move failed"
  fi
}

# publish STEP AGENT_ID DESCRIPTION : 그 Agent 를 호출의 메시지를 사람이 쓴 것으로 다루게 허브에 게시하고 항목의 id 를
# PUBLISHED_ITEM 에 남긴다. 재실행의 200 은 앞 실행이 남긴 항목이다
PUBLISHED_ITEM=""
publish() {
  local code body
  body="$(python3 -c 'import json, sys; print(json.dumps({"description": sys.argv[1], "calls_trusted": True}))' "$3")"
  code="$(api POST "/agents/$2/publish" "$body")"
  case "$code" in
    200 | 201) PUBLISHED_ITEM="$(jq_ 'd["id"]')" ;;
    *) die "$1" "POST /agents/$2/publish -> $code" ;;
  esac
  [ "$(jq_ 'd["kind"], d["source"].get("agent_id")')" = "agent $2" ] || die "$1" "the published item is not an agent item of $2"
}

# roll_platform : 플랫폼을 굴리고 API 가 답하기를 기다린다. 로컬은 port-forward 를 새 파드로 다시 잇는다. 실패하면 0 이 아니다
roll_platform() {
  if [ "$TARGET" = install ]; then roll_platform_api; return; fi
  restart_platform || return 1
  restart_pf "$API_PORT"
  wait_http "$API/version" 120 || { echo "!! the platform did not answer through the port-forward after the rollout"; return 1; }
}

# drop_agent ID NAME : Agent 를 지운다. 게시 항목이 함께 지워진다. 지우면 실행 목록이 404 라 실행 세션을 먼저 읽고, 지운 뒤
# 체크포인터의 키와 실행 세션의 접두어 밖 키를 걷는다(Agent 삭제는 Redis 를 건드리지 않는다)
drop_agent() {
  [ -n "$1" ] || return 0
  run_sessions "$1" || echo "!! the run sessions of $2 ($1) could not be read; their write_keys_zset keys may be left"
  api_delete "/agents/$1"
  wait_gone "/agents/$1" 180 || echo "!! Agent $2 ($1) may be left in tenant $TENANT"
  [ -n "$DS_ID" ] || return 0
  if [ "$TARGET" = install ]; then
    redis_agent_clean "$1" ${RUN_SIDS[@]+"${RUN_SIDS[@]}"} || echo "!! Redis keys of daegam-agent:$1: and of its run sessions may be left"
  else
    rforget "$1" ${RUN_SIDS[@]+"${RUN_SIDS[@]}"}
  fi
}

# 만든 것을 거꾸로 걷는다. 게시 항목을 Gateway 가, MCP Server 를 게시가 붙들고 있다
teardown() {
  local uid
  drop_agent "$ID_A" "$NAME_A"; drop_agent "$ID_B" "$NAME_B"; drop_agent "$ID_C" "$NAME_C"; drop_agent "$ID_D" "$NAME_D"
  if [ -n "$GW_ID" ]; then
    api_delete "/gateways/$GW_ID"
    wait_gone "/gateways/$GW_ID" 180 || echo "!! Gateway $GW_NAME ($GW_ID) may be left in tenant $TENANT"
  fi
  if [ -n "$MS_ID" ]; then
    [ -z "$HMS" ] || api_delete "/mcp-servers/$MS_ID/publish"
    api_delete "/mcp-servers/$MS_ID"
    wait_gone "/mcp-servers/$MS_ID" 180 || echo "!! MCP Server $MS_NAME ($MS_ID) may be left in tenant $TENANT"
  fi
  [ -z "$BP_ID" ] || api_delete "/backend-apis/$BP_ID"
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  for uid in "$U1_ID" "$U2_ID"; do
    [ -n "$uid" ] || continue
    local_set_member "$TENANT" "$uid" "" || echo "!! the membership of $uid in $TENANT may be left"
    api_delete "/users/$uid"
  done
  if [ "$TARGET" = install ]; then
    # 죽은 앞 실행이 남긴 것도 걷는다. 스텁을 다시 막은 뒤에는 그것이 남은 목록의 저장이 모두 거절된다
    drop_stub_provider "$STUB" || true
  elif [ "${STUB_PROVIDER_ADDED:-0}" = 1 ]; then
    remove_providers "$STUB"
  fi
}

cleanup() {
  bg_stop
  # ConfigMap 을 먼저 되돌린다. 굴리기 전까지 떠 있는 파드는 스텁을 받으므로 스텁 provider 를 걷는 저장이 된다
  config_window_restore || true
  # 설치본은 관리자 토큰이 없으면(로그인 전에 끝났으면) 걷을 것도 없다
  if [ "$TARGET" = local ] || [ ${#AUTH[@]} -gt 0 ]; then teardown; fi
  if [ "$CONFIG_ROLL_PENDING" = 1 ]; then
    if roll_platform; then
      CONFIG_ROLL_PENDING=0
      echo ">> ConfigMap $AUTH_MODE_CONFIGMAP restored, platform rolled"
    else
      echo "!! roll the platform by hand: kubectl --context $CTX -n $PLATFORM_NS rollout restart deploy/daegam-platform"
    fi
  fi
  if [ "$CONFIG_CHANGED" = 1 ] && [ "$TARGET" = install ] && [ "$(platform_config DAEGAM_ALLOW_STUB_PROVIDER || true)" != "$STUB_BEFORE" ]; then
    echo "!! ConfigMap $AUTH_MODE_CONFIGMAP is not back to DAEGAM_ALLOW_STUB_PROVIDER='$STUB_BEFORE'. $CONFIG_RESTORE_HINT"
  fi
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

echo ">> target=$TARGET context=$CTX tenant=$TENANT api=$API"

# ---------- 0. 플랫폼, 실행과 호출의 스위치, 스텁 허용 ----------
platform_connect 0
runs_on || die 0 "GET /me/runs-as-me -> $(api GET /me/runs-as-me): runs are off in this deployment (DAEGAM_RUNS_ENABLED), and a call is a run"
CALLS_BEFORE="$(platform_config DAEGAM_AGENT_CALLS_ENABLED)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
[ "$CALLS_BEFORE" != false ] || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP has DAEGAM_AGENT_CALLS_ENABLED=false: agent calls are off in this deployment"
STUB_BEFORE="$(platform_config DAEGAM_ALLOW_STUB_PROVIDER)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
if [ "$TARGET" = install ]; then
  # 설치본은 스텁을 막는다. 이미 열려 있으면 앞 실행이 되돌리지 못한 것이라 되돌릴 값을 모른다
  [ "$STUB_BEFORE" != true ] \
    || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP already has DAEGAM_ALLOW_STUB_PROVIDER=true (left by an earlier run?): set it back to false, roll the platform and run again"
  # 호출은 부른 턴의 위임으로 돈다. 위임을 끈 설치본은 모든 호출이 acts_as_unavailable 이다
  [ "$(platform_config DAEGAM_TURN_TOKENS_ENABLED)" != false ] \
    || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP has DAEGAM_TURN_TOKENS_ENABLED=false: calls need the turn's grant"
fi
# 창은 스텁 허용을 열고, STEP 11 이 끄는 호출의 스위치도 읽은 값으로 되돌린다
config_window_plan DAEGAM_ALLOW_STUB_PROVIDER=true "DAEGAM_AGENT_CALLS_ENABLED=${CALLS_BEFORE:-true}" \
  || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
CONFIG_CHANGED=1
echo ">> ConfigMap $AUTH_MODE_CONFIGMAP allows stub providers for this gate (restored on exit). If this run dies, $CONFIG_RESTORE_HINT"
if [ "$STUB_BEFORE" = true ]; then
  ROLLED="stub providers were already allowed"
else
  config_window_apply || die 0 "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
  roll_platform || die 0 "the platform did not come back after allowing stub providers"
  ROLLED="stub providers allowed for this gate, platform rolled"
fi
pass 0 "connected at $BASE ($TARGET); runs and agent calls on; $ROLLED"

# ---------- 1. 사람, 스텁 provider, Redis, MCP Server 와 Gateway ----------
if [ "$TARGET" = install ]; then
  U1_ID="$(local_fresh_member "$U1" "$TENANT")" || die 1 "creating $U1 in $TENANT failed"
  U2_ID="$(local_fresh_member "$U2" "$TENANT")" || die 1 "creating $U2 in $TENANT failed"
  local_set_member "$TENANT" "$U1_ID" developer || die 1 "making $U1 a developer of $TENANT failed"
  # 첫 로그인이라 서버가 강제하는 비밀번호 바꾸기를 지난다
  U1_TOKEN="$(local_login_any "$U1" "$LOCAL_PW")"; [ -n "$U1_TOKEN" ] || die 1 "login as $U1 failed"
  U2_TOKEN="$(local_login_any "$U2" "$LOCAL_PW")"; [ -n "$U2_TOKEN" ] || die 1 "login as $U2 failed"
fi
ensure_stub_provider 1 "$STUB"
code="$(api GET /clusters)"; [ "$code" = 200 ] || die 1 "GET /clusters -> $code"
HOME_ID="$(find_id home)"; [ -n "$HOME_ID" ] || die 1 "no home cluster"
HOME_DEPLOY="{\"cluster_id\": \"$HOME_ID\"}"
if [ "$TARGET" = install ]; then
  # 본문을 변수에 먼저 둔다. register_data_service 의 인자 자리에서 만들면 실패해도 set -e 가 멈추지 않는다
  ds_body="$(redis_ds_body "$DS_NAME" "$HOME_ID")" || die 1 "reading the Redis Secret or CA failed"
  register_data_service 1 "$DS_NAME" "$ds_body"
  unset ds_body
else
  lab_database 1 redis "$DS_NAME" "Agent 호출 Redis"
fi
DS_ID="$DATA_SERVICE_ID"
upload_backend_api 1 "$BP_NAME" "$SPEC" "Agent 호출 펫스토어"
BP_ID="$BACKEND_API_ID"
save_doc 1 /mcp-servers "$MS_NAME" \
  "{\"name\": \"$MS_NAME\", \"display_name\": \"Agent 호출 펫스토어\", \"backend_profile_id\": \"$BP_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": [\"$READ_TOOL\", \"$WRITE_TOOL\"]}"
MS_ID="$SAVED_ID"
deploy_doc 1 /mcp-servers "$MS_ID" "$HOME_DEPLOY"
echo ">> waiting up to ${READY_TIMEOUT}s for MCP Server $MS_NAME"
wait_ready "/mcp-servers/$MS_ID" 1
code="$(api POST "/mcp-servers/$MS_ID/publish" '{}')"
case "$code" in 200 | 201) HMS="$(jq_ 'd["id"]')" ;; *) die 1 "publish $MS_NAME -> $code" ;; esac
save_doc 1 /gateways "$GW_NAME" "{\"name\": \"$GW_NAME\", \"display_name\": \"Agent 호출 게이트웨이\", \"backends\": [{\"hub_item_id\": \"$HMS\"}]}"
GW_ID="$SAVED_ID"
deploy_doc 1 /gateways "$GW_ID" "$HOME_DEPLOY"
echo ">> waiting up to ${READY_TIMEOUT}s for Gateway $GW_NAME"
wait_ready "/gateways/$GW_ID" 1
pass 1 "stub provider $STUB, Redis Database $DS_NAME ($DS_ID), MCP Server $MS_NAME published as $HMS behind Gateway $GW_NAME ($GW_ID)${U1_ID:+; $U1 developer and $U2 viewer of $TENANT}"

# ---------- 2. Agent 넷을 배포해 게시하고 서로 Tool 로 붙인다 ----------
code="$(api GET /agents/templates)"; [ "$code" = 200 ] || die 2 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
agent_doc "$WORK/d.json" "$NAME_D" "호출 사슬 d"
launch 2 "$NAME_D" "$WORK/d.json"; ID_D="$LAUNCHED_ID"
require_features 2 runs turn_tokens agent_tools
publish 2 "$ID_D" "Answers the request it is given."; ITEM_D="$PUBLISHED_ITEM"
agent_doc "$WORK/c.json" "$NAME_C" "호출 사슬 c" --agent-source "$ITEM_D:$ASK_D"
launch 2 "$NAME_C" "$WORK/c.json"; ID_C="$LAUNCHED_ID"
publish 2 "$ID_C" "Passes a request on to d."; ITEM_C="$PUBLISHED_ITEM"
# b 의 Gateway Tool 노드가 첫 Tool 노드라 승인 규칙이 거기에 선다
B_FLAGS=(--source "gateway_instance:$GW_ID" --allow "$READ_TOOL,$WRITE_TOOL" --approval-for "$WRITE_TOOL" --agent-source "$ITEM_C:$ASK_C")
agent_doc "$WORK/b.json" "$NAME_B" "호출 사슬 b" "${B_FLAGS[@]}"
launch 2 "$NAME_B" "$WORK/b.json"; ID_B="$LAUNCHED_ID"
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; K8S_B="$(jq_ 'd["k8s"]["name"]')"
publish 2 "$ID_B" "Looks up and adds pets, and passes requests on to c."; ITEM_B="$PUBLISHED_ITEM"
agent_doc "$WORK/a.json" "$NAME_A" "호출 사슬 a" --agent-source "$ITEM_B:$ASK_B"
launch 2 "$NAME_A" "$WORK/a.json"; ID_A="$LAUNCHED_ID"
publish 2 "$ID_A" "Asks b."; ITEM_A="$PUBLISHED_ITEM"
# b 가 a 를 다시 부르게 한다. a 의 배포된 리비전이 b 로 돌아오므로 저장이 순환을 경고한다. 호출은 실행할 때 거절된다
agent_doc "$WORK/b2.json" "$NAME_B" "호출 사슬 b" "${B_FLAGS[@]}" --agent-source "$ITEM_A:$ASK_A"
save_doc 2 /agents "$NAME_B" "$(cat "$WORK/b2.json")"
[ "$(jq_ "any(i['code'] == 'agent_call_cycle' and i['level'] == 'warning' for i in d.get('issues') or [])")" = True ] \
  || die 2 "saving $NAME_B with $ASK_A did not warn agent_call_cycle"
deploy_doc 2 /agents "$ID_B" "$HOME_DEPLOY"
wait_ready "/agents/$ID_B" 2
code="$(api GET /hub)"; [ "$code" = 200 ] || die 2 "GET /hub -> $code"
[ "$(jq_ "sorted(i['name'] for i in d if i['kind'] == 'agent' and i['id'] in ('$ITEM_A', '$ITEM_B', '$ITEM_C', '$ITEM_D'))")" = "['$NAME_A', '$NAME_B', '$NAME_C', '$NAME_D']" ] \
  || die 2 "the hub does not list the four agent items"
code="$(api GET "/hub/$ITEM_B/references")"
[ "$code" = 200 ] && [ "$(jq_ "any(r['kind'] == 'agent' and r['id'] == '$ID_A' for r in d['used_by'])")" = True ] \
  || die 2 "the references of hub item $ITEM_B do not list Agent $NAME_A ($code)"
pass 2 "$NAME_D, $NAME_C, $NAME_B and $NAME_A deployed and published ($ITEM_D $ITEM_C $ITEM_B $ITEM_A) with [$FEATURES]; saving $NAME_B with $ASK_A warned agent_call_cycle; the hub lists four agent items and $ITEM_B is used by $NAME_A"

# ---------- 3. 호출이 호출받은 Agent 의 답을 돌려준다 ----------
tries=0; deadline=$((SECONDS + PERSON_WAIT)); why=""
while :; do
  ask_turn 3 "$U1_TOKEN" "$(ask_call "$ASK_B" "e2e-agent-calls hello $DONE_FRAGMENT")"
  tries=$((tries + 1))
  call_run 3 "$ID_B" "$CALL_SID"
  run_wait 3 "$ID_B" "$RUN_ID" "$RUN_TERMINAL"
  kind="$(run_field 'd["acts_as"]["kind"]')"
  # 설치본의 호출은 사람 이름으로 돈다. header 모드는 호출받은 Agent 의 이름이다
  if [ "$RUN_STATUS" = succeeded ] && { [ "$TARGET" = local ] || [ "$kind" = person ]; }; then break; fi
  if [ "$RUN_STATUS" = succeeded ]; then why="$(run_field 'd["acts_as"].get("cause")')"; else why="$(run_field 'd.get("reason")')"; fi
  case "$START_TRANSIENT" in
    *" $why "*) [ "$SECONDS" -lt "$deadline" ] \
      || die 3 "the call still ends $RUN_STATUS ($why) after $tries turns and ${PERSON_WAIT}s" "$(cat "$WORK/run.json")" ;;
    *) die 3 "the run $RUN_ID of $NAME_B ended $RUN_STATUS as $kind ($why)" "$(cat "$WORK/run.json")" ;;
  esac
  sleep "$PERSON_POLL"
done
{ [ "$TOOL_STATUS" = success ] && answer_has "기다린 뒤 답합니다"; } \
  || die 3 "$ASK_B answered $TOOL_STATUS without the answer of $NAME_B" "$(cat "$WORK/answer")"
[ "$(run_field "d['source'], d['caller']['agent_id'], d['caller']['session_id'], d['caller']['chain']")" = "agent $ID_A $CALL_SID ['$ID_A']" ] \
  || die 3 "run record: $(cat "$WORK/run.json") (want source agent, caller $ID_A, session $CALL_SID, chain [$ID_A])"
if [ "$TARGET" = install ]; then
  [ "$(run_field 'd["acts_as"]["sub"]')" = "$U1_ID" ] || die 3 "the run acts as $(run_field 'd["acts_as"]["sub"]'), want $U1 ($U1_ID)"
fi
FIRST_RUN="$RUN_ID"
pass 3 "$ASK_B returned the answer of $NAME_B (after $tries turns); run $FIRST_RUN has source agent, caller $NAME_A, session $CALL_SID and acts as $kind"

# ---------- 4. 호출받은 Agent 의 Tool 호출이 Gateway 에 닿는다 ----------
if [ "$TARGET" = install ]; then
  # 문이 열린 뒤에도 오퍼레이터가 vMCP 를 굴리는 중일 수 있고, 그동안의 호출은 곧 지워질 옛 파드가 받아 감사 줄을 잃는다
  TURN_GATE_TIMEOUT="${E2E_AGENT_CALLS_GATE_TIMEOUT:-600}" TURN_GATE_POLL=15 wait_gateway_gate 4 "$HOME_ID"
  wait_workloads_rolled "$AG_NS" || die 4 "workloads in $AG_NS were still rolling 300s after the Gateway gate opened"
fi
tries=0; deadline=$((SECONDS + PERSON_WAIT))
while :; do
  SINCE="$(cluster_now)" || die 4 "reading the platform pod's clock failed"
  ask_turn 4 "$U1_TOKEN" "$(ask_call "$ASK_B" "$(stub_call "$READ_TOOL" '{}')")"
  UNTIL="$(cluster_now)" || die 4 "reading the platform pod's clock failed"
  tries=$((tries + 1))
  call_run 4 "$ID_B" "$CALL_SID"
  run_wait 4 "$ID_B" "$RUN_ID" "$RUN_TERMINAL"
  [ "$RUN_STATUS" != succeeded ] || break
  why="$(run_field 'd.get("reason")')"
  case "$START_TRANSIENT" in
    *" $why "*) [ "$SECONDS" -lt "$deadline" ] \
      || die 4 "the call still ends $RUN_STATUS ($why) after $tries turns and ${PERSON_WAIT}s" "$(cat "$WORK/run.json")" ;;
    *) die 4 "the run $RUN_ID of $NAME_B ended $RUN_STATUS ($why)" "$(cat "$WORK/run.json")" ;;
  esac
  sleep "$PERSON_POLL"
done
# b 의 스텁은 Tool 결과를 "결과: <내용>" 으로 답한다. 그 글이 a 의 Tool 결과로 온다
{ [ "$TOOL_STATUS" = success ] && answer_has "결과: "; } \
  || die 4 "$ASK_B answered $TOOL_STATUS without the $READ_TOOL result of $NAME_B" "$(cat "$WORK/answer")"
if [ "$TARGET" = install ]; then
  # 거절한 호출도 sub·act 를 싣고 denied 로 남으므로 outcome success 만 센다
  audit_expect 4 "$SINCE" "gateway $READ_TOOL|$SINCE|$UNTIL|$GW_ID|$READ_TOOL|$U1_ID|svc:agent:$K8S_B|1|success"
  pass 4 "the call reached $READ_TOOL of $NAME_B (turn $tries); the vMCP audit of $GW_NAME has it with sub $U1 and act svc:agent:$K8S_B"
else
  pass 4 "the call reached $READ_TOOL of $NAME_B (turn $tries)"
  cond_skip 4p "header mode: a call runs as the called Agent, so the Gateway audit names no person"
fi

# ---------- 5. 사슬: 셋까지는 돌고, 순환과 넷째는 거절된다 ----------
if [ "$TARGET" = install ]; then
  ask_turn 5 "$U1_TOKEN" "$(ask_call "$ASK_B" "$(ask_call "$ASK_C" "e2e-agent-calls chain $DONE_FRAGMENT")")"
  { [ "$TOOL_STATUS" = success ] && answer_has "기다린 뒤 답합니다"; } \
    || die 5 "$NAME_A -> $NAME_B -> $NAME_C did not return the answer of $NAME_C ($TOOL_STATUS)" "$(cat "$WORK/answer")"
  call_run 5 "$ID_B" "$CALL_SID"
  run_wait 5 "$ID_B" "$RUN_ID" "$RUN_TERMINAL"
  B_SID="$(run_field 'd["session_id"]')"
  call_run 5 "$ID_C" "$B_SID"
  run_wait 5 "$ID_C" "$RUN_ID" "$RUN_TERMINAL"
  [ "$(run_field "d['status'], d['caller']['chain']")" = "succeeded ['$ID_A', '$ID_B']" ] \
    || die 5 "the run of $NAME_C: $(cat "$WORK/run.json") (want succeeded with chain [$ID_A, $ID_B])"
  ask_turn 5 "$U1_TOKEN" "$(ask_call "$ASK_B" "$(ask_call "$ASK_A" "e2e-agent-calls cycle")")"
  answer_has agent_call_cycle || die 5 "$NAME_A -> $NAME_B -> $NAME_A was not refused with agent_call_cycle" "$(cat "$WORK/answer")"
  ask_turn 5 "$U1_TOKEN" "$(ask_call "$ASK_B" "$(ask_call "$ASK_C" "$(ask_call "$ASK_D" "e2e-agent-calls deep")")")"
  answer_has agent_call_too_deep || die 5 "$NAME_A -> $NAME_B -> $NAME_C -> $NAME_D was not refused with agent_call_too_deep" "$(cat "$WORK/answer")"
  pass 5 "$NAME_A -> $NAME_B -> $NAME_C answered with chain [$NAME_A, $NAME_B]; -> $NAME_A refused agent_call_cycle; -> $NAME_D refused agent_call_too_deep"
else
  cond_skip 5 "header mode runs every call as untrusted input, so the next call of the called Agent (a write) waits for an approval"
fi

# ---------- 6. viewer 의 호출 ----------
if [ "$TARGET" = install ]; then
  ask_turn 6 "$U2_TOKEN" "$(ask_call "$ASK_B" "e2e-agent-calls viewer $DONE_FRAGMENT")"
  call_run 6 "$ID_B" "$CALL_SID"
  run_wait 6 "$ID_B" "$RUN_ID" "$RUN_TERMINAL"
  [ "$TOOL_STATUS $RUN_STATUS $(run_field 'd["acts_as"]["sub"]')" = "success succeeded $U2_ID" ] \
    || die 6 "the viewer's call: tool $TOOL_STATUS, run $RUN_STATUS ($(run_field 'd.get("reason")')) as $(run_field 'd["acts_as"]["sub"]'), want success, succeeded as $U2 ($U2_ID)" "$(cat "$WORK/run.json")"
  pass 6 "the viewer $U2 called $NAME_B through $NAME_A; run $RUN_ID succeeded as $U2"
else
  cond_skip 6 "header mode has no people to tell apart; the person rule runs on the install"
fi

# stop_trail: 중지가 호출받은 실행에 닿지 않았을 때 그 까닭을 가르게, 부른 Agent 의 파드와 플랫폼의 로그에서 취소와 호출 경로의
# 줄을 찍는다
stop_trail() {
  echo "  stop: the calling Agent's log (cancel, stop and call lines)"
  "${K[@]}" -n "$AG_NS" logs "deploy/agent-$NAME_A" --all-containers --tail=400 </dev/null 2>/dev/null \
    | grep -iE 'cancel|취소|멈|agent-calls|Traceback|Error' | tail -n 30 || true
  echo "  stop: the platform's log (call routes and session cancel)"
  "${K[@]}" -n "$PLATFORM_NS" logs deploy/daegam-platform --all-containers --tail=2000 </dev/null 2>/dev/null \
    | grep -E 'agent-calls|/cancel' | tail -n 30 || true
}

# ---------- 7. 부른 턴을 중지하면 호출받은 실행이 취소된다 ----------
as_person "$U1_TOKEN" chat_bg "/agents/$ID_A/chat" "$(msg "$(ask_call "$ASK_B" "e2e-agent-calls slow {\"sleep\": $SLOW_SECONDS}")")"
STOP_SID="$(bg_session_id)"
[ -n "$STOP_SID" ] || { bg_stop; die 7 "the turn to stop sent no X-Daegam-Session-Id: $(cat "$WORK/bg.hdr" 2>/dev/null)"; }
call_run 7 "$ID_B" "$STOP_SID" 60
STOP_RUN="$RUN_ID"
run_wait 7 "$ID_B" "$STOP_RUN" "running $RUN_TERMINAL" 60
[ "$RUN_STATUS" = running ] || { bg_stop; die 7 "the slow run $STOP_RUN is $RUN_STATUS before the stop, want running" "$(cat "$WORK/run.json")"; }
code="$(as_person "$U1_TOKEN" api POST "/agents/$ID_A/sessions/$STOP_SID/cancel")"
[ "$code" = 202 ] || { bg_stop; die 7 "POST /agents/$ID_A/sessions/$STOP_SID/cancel -> $code"; }
run_wait 7 "$ID_B" "$STOP_RUN" "$RUN_TERMINAL" "$CANCEL_WAIT"
bg_stop
[ "$RUN_STATUS $(run_field 'd.get("reason")')" = "cancelled caller_ended" ] \
  || { stop_trail; die 7 "the run $STOP_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")')), want cancelled (caller_ended)" "$(cat "$WORK/run.json")"; }
pass 7 "stopping the turn of $NAME_A in session $STOP_SID cancelled run $STOP_RUN of $NAME_B (caller_ended)"

# ---------- 8. 호출받은 Agent 의 승인 ----------
ask_turn 8 "$U1_TOKEN" "$(ask_call "$ASK_B" "$(stub_call "$WRITE_TOOL" '{"name": "e2e-agent-calls"}')")"
answer_has "$WAITING_TEXT" || die 8 "$ASK_B did not answer that $NAME_B waits for an approval ($TOOL_STATUS)" "$(cat "$WORK/answer")"
call_run 8 "$ID_B" "$CALL_SID"
WAIT_RUN="$RUN_ID"
run_wait 8 "$ID_B" "$WAIT_RUN" "waiting_approval $RUN_TERMINAL"
[ "$RUN_STATUS" = waiting_approval ] \
  || die 8 "run $WAIT_RUN is $RUN_STATUS ($(run_field 'd.get("reason")')), want waiting_approval" "$(cat "$WORK/run.json")"
WAIT_SID="$(run_field 'd["session_id"]')"
code="$(as_person "$U1_TOKEN" api GET "/agents/$ID_B/sessions?runs=only&status=pending&all_users=true")"
[ "$code" = 200 ] && [ "$(jq_ "any(s['session_id'] == '$WAIT_SID' and (s.get('run') or {}).get('id') == '$WAIT_RUN' for s in d['sessions'])")" = True ] \
  || die 8 "the inbox of $NAME_B does not list run $WAIT_RUN waiting in session $WAIT_SID ($code)"
code="$(as_person "$U1_TOKEN" chat "/agents/$ID_B/sessions/$WAIT_SID/decisions" '{"decisions": [{"type": "approve"}]}')"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 8 "the decision on run $WAIT_RUN -> $code"; }
run_wait 8 "$ID_B" "$WAIT_RUN" "$RUN_TERMINAL"
[ "$RUN_STATUS" = succeeded ] || die 8 "the approved run $WAIT_RUN ended $RUN_STATUS ($(run_field 'd.get("reason")'))" "$(cat "$WORK/run.json")"
pass 8 "$ASK_B returned the waiting text at once; run $WAIT_RUN waited in the inbox of $NAME_B and succeeded after the approval"

# ---------- 9. 실행 목록의 부른 Agent 와 토큰 ----------
usage=""
for ((i = 0; i < USAGE_TRIES; i++)); do
  code="$(api GET "/agents/$ID_B/runs/$FIRST_RUN")"
  [ "$code" = 200 ] || die 9 "GET /agents/$ID_B/runs/$FIRST_RUN -> $code"
  usage="$(jq_ '(lambda u: u["input_tokens"] + u["output_tokens"] if u else 0)(d.get("usage"))')"
  [ "${usage:-0}" -gt 0 ] && break
  sleep 2
done
[ "${usage:-0}" -gt 0 ] || die 9 "run $FIRST_RUN has no usage from the ledger after $((USAGE_TRIES * 2))s"
code="$(api GET "/agents/$ID_B/runs?limit=500")"; [ "$code" = 200 ] || die 9 "GET /agents/$ID_B/runs -> $code"
calls="$(jq_ "len([r for r in d['runs'] if r['source'] == 'agent'])")"
[ "$(jq_ "all((r.get('caller') or {}).get('agent_id') == '$ID_A' for r in d['runs'] if r['source'] == 'agent')")" = True ] \
  || die 9 "a run of $NAME_B with source agent does not name $NAME_A as its caller"
pass 9 "the runs of $NAME_B list $calls calls, each with caller $NAME_A; run $FIRST_RUN used $usage tokens"

# ---------- 10. 게시를 해제한 Agent ----------
code="$(api DELETE "/agents/$ID_D/publish")"; [ "$code" = 200 ] || die 10 "DELETE /agents/$ID_D/publish -> $code"
code="$(as_person "$U1_TOKEN" chat "/agents/$ID_C/chat" "$(msg "$(ask_call "$ASK_D" "e2e-agent-calls unpublished")")")"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 10 "POST /agents/$ID_C/chat -> $code $(sse_ 'names')"; }
[ "$(sse_ '(lambda r: r["name"] == "'"$ASK_D"'" and "agent_source_unpublished" in r["content"])(first("tool_result"))')" = True ] \
  || { cp "$WORK/sse" "$WORK/body"; die 10 "$ASK_D was not refused with agent_source_unpublished"; }
code="$(api GET "/agents/$ID_C")"
[ "$code" = 200 ] && [ "$(jq_ 'any(w.startswith("agent_source_unpublished") for w in (d.get("status") or {}).get("warnings") or [])')" = True ] \
  || die 10 "the status of $NAME_C does not warn agent_source_unpublished ($code)"
pass 10 "unpublishing $NAME_D refused the call of $NAME_C (agent_source_unpublished) and its status warns with the same code"

# ---------- 11. 호출을 끈 플랫폼 ----------
"${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p '{"data": {"DAEGAM_AGENT_CALLS_ENABLED": "false"}}' >/dev/null \
  || die 11 "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
roll_platform || die 11 "the platform did not come back after turning agent calls off"
code="$(as_person "$U1_TOKEN" chat "/agents/$ID_A/chat" "$(msg "e2e-agent-calls off $DONE_FRAGMENT")")"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 11 "$NAME_A does not chat with calls off: $code $(sse_ 'names')"; }
ask_turn 11 "$U1_TOKEN" "$(ask_call "$ASK_B" "e2e-agent-calls off $DONE_FRAGMENT")"
answer_has agent_calls_disabled || die 11 "$ASK_B did not answer agent_calls_disabled with calls off ($TOOL_STATUS)" "$(cat "$WORK/answer")"
code="$(api POST "/agents/$ID_D/publish" '{"description": "Answers the request it is given."}')"
[ "$code" = 409 ] && [ "$(error_code "$WORK/body")" = agent_calls_disabled ] \
  || die 11 "publishing $NAME_D with calls off -> $code (want 409 agent_calls_disabled)"
pass 11 "with DAEGAM_AGENT_CALLS_ENABLED=false $NAME_A still chats, $ASK_B answers agent_calls_disabled and publishing is 409 agent_calls_disabled"

finish
