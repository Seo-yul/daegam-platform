#!/usr/bin/env bash
# shellcheck disable=SC2329  # cleanup 과 그 안의 stop_pf·restore_settings 는 trap 으로만 불린다
# P14 (D31) 플랫폼 안의 청지기 e2e. 실제 (orbstack) 클러스터에서 stub provider 로 돈다.
# 전제: `make deploy-local`(header 모드, ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true, 비서 API 가 든 플랫폼 이미지)와
# `make lab-up`(mock-petstore 가 http://mock-petstore.<SPIKE_NS>.svc:18081/v2 에 뜬다).
#
# 한 세션에서 스텁 steps 메시지 셋과 승인 세 번으로 petstore 백엔드부터 Agent Ready 까지 만든다. 스텁 모델은
# 마지막 사용자 메시지의 {"steps": [...]} 를 순서대로 tool_call 로 바꾸고 "$<n>.<path>" 는 n 번째 step 의 도구 결과에서
# 뽑은 값이다(platform/daegam/services/assistant/stub.py). 도구 결과는 4KB 로 잘리므로 create 응답이 4KB 를 넘는 리소스는
# "$n.id" 참조가 깨진다. 그래서 Agent 그래프는 템플릿(한글 프롬프트가 길다) 대신 짧은 최소 그래프를 쓴다.
#
# Auth mode: E2E_AUTH_MODE=header (기본, `make e2e-assistant`) 는 STEP 0~8 을 테넌트 헤더로 돌고 STEP 9~11 은 조건부
# SKIP 이다. 주체가 하나라 남의 세션도 남의 권한도 만들 수 없기 때문이다. E2E_AUTH_MODE=oidc (`make e2e-assistant-oidc`)
# 는 STEP 0 에서 scripts/lib/e2e_auth_mode.sh 로 ConfigMap daegam-platform-config 의 DAEGAM_AUTH_MODE 를 oidc 로 바꾸고
# 플랫폼과 UI 를 굴린 뒤 플랫폼이 데이터 플레인 인증을 다시 적용할 때까지 기다린다(P32). STEP 0~8 은 부트스트랩 관리자
# 토큰(sub boot-admin, aud daegam-api)으로 돌리고 STEP 9~11 은 viewer·developer 토큰으로 돈다. EXIT trap 이 그룹 매핑과
# DAEGAM_AUTH_MODE 를 되돌리고 다시 기다린다.
#
# 게이트 (14단계):
#   STEP 0  platform port-forward, 모드 확인(header 면 무토큰 200, oidc 면 401 뒤 부트스트랩 토큰), home 클러스터
#           preflight ok. oidc 면 그룹 매핑을 GET 해 두고 viewer·developer 매핑을 더한다(PUT 이 표 전체를 갈아치운다).
#           이전 실행이 남긴 같은 이름의 리소스 정리
#   STEP 1  stub provider `stub-e2e` 등록(PATCH /settings, 멱등)과 assistant{enabled, confirm_deploy}·defaults.assistant.
#           EXIT 트랩이 원래 settings 로 되돌린다(원래 없던 provider 는 걷는다)
#   STEP 2  GET /api/assistant/config 가 enabled=true, provider=stub-e2e, confirm_deploy=true
#   STEP 3  세션 생성 → 턴 1: verify_spec → create_backend_api(spec_text, endpoint mock-petstore, auth none)
#           → create_mcp_server(backends[0].backend_profile_id="$1.id". 저장만 되므로 draft 다) →
#           deploy_mcp_server("$2.id"). SSE 가 tool_call·tool_result·resource 순서대로 오고
#           interrupt{kind confirm, deploy_mcp_server} 로 닫힌다 (저장은 write 라 그대로 돌고 배포만 승인을 받는다).
#           GET /sessions/{id} 가 state=pending 이고 pending.calls[0].id 가 있다
#   STEP 4  POST /decisions approve → tool_call/tool_result(deploy_mcp_server 201) → get_mcp_server → done.
#           MCP Server Ready 대기. 정의와 배포가 한 문서라 배포가 새 id 를 만들지 않는다
#   STEP 5  턴 2: validate_agent_graph → create_agent(tools source = STEP 4 의 MCP Server id. 앞 턴의 결과라 "$n"
#           으로 못 가져오므로 스크립트가 채운다). create_agent 는 Agent 정의의 제안이라(P48 6.1절) 플랫폼이 검사한 뒤
#           interrupt 로 기다린다. 요청은 제안 덩어리(kind create, 노드 넷 모두 더함, 먼저 볼 것에 새 프롬프트와 새 Tool
#           소스)와 인자의 예약 칸 _proposal 을 싣고, 지문 없는 승인은
#           409 proposal_changed 다. 지문을 실은 승인이 저장하고 → deploy_agent(승인 대기) → approve → navigate → done.
#           Agent Ready 대기
#   STEP 6  비밀 거부: create_data_service{password: "hunter2"} 한 step 과 navigate. tool_call 이벤트의 password 는 ***,
#           tool_result 는 error secret_in_chat(fields ["/password"]), ui navigate 가 오고, SSE 와 전사(사용자 메시지 항목은
#           제외. 사용자가 친 글은 그대로 남는다)에 hunter2 가 없다. Database 는 만들어지지 않고 감사에도 없다
#   STEP 7  감사: GET /api/audit?via=assistant/<sid> 가 쓰기 행 9개(verify_spec·upload_spec·create_server·deploy_server·
#           validate_agent_graph 셋(스텁의 검증, 제안의 검사, 승인 때의 다시 검사)·create_agent·deploy_agent. action 은 라우터
#           핸들러 함수 이름이라 이름을 바꾼 만큼 바뀌었다)이고 via·request_id(턴과 결정 요청의 X-Request-ID 뒤에
#           .n, 제안 검사의 검증은 .n-check)·sub 가 맞다. X-Daegam-Via: assistant/forged 를 붙인 외부 POST 의 감사 행은 via 가 null 이다
#   STEP 8  세션 삭제 뒤에도 STEP 7 의 감사 행이 그대로 남고 GET /sessions/{id} 는 404 다
#   STEP 9  (oidc) viewer 의 배포 도구는 403 이다. 띄울 draft 가 있어야 하므로 관리자가 STEP 4 의 서버를 복제해 두고
#           (한 문서는 배포를 하나만 가진다, D42) viewer 가 그것을 deploy_mcp_server 로 띄운다. 승인까지 가지만
#           tool_result 가 error{status 403, code forbidden, scope mcp:deploy} 이고 비서의 마지막 말이 그 코드를
#           싣는다. 복제본은 draft 그대로다
#   STEP 10 (oidc) 세션은 만든 사람만 본다. developer 의 세션이 viewer 에게는 404 이고 목록에도 없다(그 반대도)
#   STEP 11 (oidc) 교차 파드 결정 재개: 파드 A 에서 승인 대기가 된 턴을 파드 B 로 보낸 결정이 잇는다(파드 B 도 대기를
#           본다. 상태가 Mongo 에 있기 때문이다). 경합이 없는 검사다
#   STEP 12 (oidc) 교차 파드 리스: 파드 B 에서 도는 턴 동안 파드 A 로 보낸 같은 세션의 턴은 409 turn_in_progress(세션
#           리스), 같은 사용자의 다른 세션도 409(사용자 리스), 다른 사용자의 턴은 200 이다(전역 잠금이 아니다).
#           도는 턴을 잡아야 하는 검사라 창을 놓치면 더 긴 턴으로 세 번까지 다시 한다
#   STEP 13 Agent 정의 바꾸기 제안(P48 6.2·6.5·6.7절). 새 세션에서 STEP 5 의 Agent 를 바꾼다. 사람이 먼저 배치만 저장하고
#           (리비전 없음) 턴 하나가 사람 표시를 단 제안(person_marks_only), 모델 노드를 지운 제안(proposal_invalid),
#           메인의 시스템 프롬프트에 한 줄을 더한 제안을 보낸다. 앞의 둘은 tool_result 로 거절되고 셋째만 카드로 기다린다
#           (덩어리 kind update, 기준 리비전, 바뀐 노드와 줄 비교, system_prompt_changed). 그 사이 아무것도 저장되지 않는다.
#           GET /proposal 이 기준 위에 짜 맞춘 정의를 주고, 다른 지문의 승인은 409 proposal_changed 다. 지문을 실은 승인이
#           다음 리비전을 저장하고 배치가 그대로이며 감사 행은 그 세션의 via 를, 사람의 배치 저장은 via 없음을 싣는다.
#           다음 제안이 기다리는 동안 사람이 expect_revision 으로 저장하면 그 제안의 승인은 409 revision_conflict 결과이고
#           아무것도 저장하지 않는다. 지난 기준으로 한 사람의 저장도 409 revision_conflict{revision} 이다
#   cleanup 만든 agent·mcp server 둘·backend api(·data service)와 STEP 9~13 의
#           세션 삭제, settings·그룹 매핑·DAEGAM_AUTH_MODE 복원, port-forward 종료. KEEP=1 이면 리소스와 settings 를 남긴다
#
# 이 스크립트는 실제 provider(E2E_LLM_PROVIDER)를 받지 않는다.
# 단계는 `STEP n PASS|FAIL|SKIP: ...` 로 찍는다. SKIP 은 header 모드의 STEP 9~12 뿐이고 그 밖에는 조건이 안 맞으면 실패다.
# macOS 에는 timeout 이 없어 대기는 전부 폴링이다.
# 이름: 한글 display_name 을 보내고 K8s 이름은 소문자·하이픈 name 이다 (D24).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
TENANT="${E2E_TENANT:-assistant}"
API_PORT="${E2E_API_PORT:-$PORT_ASSISTANT_API}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
# 턴 SSE 의 curl --max-time. 기본 turn_timeout_seconds(300) 보다 길다
# shellcheck disable=SC2034  # lib/e2e_sse.sh 의 chat 이 읽는다
CHAT_MAX_TIME="${E2E_TURN_MAX:-360}"
KEEP="${KEEP:-0}"
BASE="http://127.0.0.1:${API_PORT}"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
FIXTURE="${E2E_SPEC_FILE:-$ROOT/mcp-runtime/tests/fixtures/petstore.openapi.yaml}"
FILENAME="$(basename "$FIXTURE")"
PETSTORE_ENDPOINT="http://mock-petstore.${SPIKE_NS}.svc:18081/v2"
PG_HOST="postgres.${SPIKE_NS}.svc"
STUB_PROVIDER="stub-e2e"
SECRET="hunter2"                  # STEP 6 이 채팅에 넣어 보는 비밀. 어디에도 남으면 안 된다(사용자 메시지 본문 제외)
# 도구 이름은 fixture 의 operationId 다 (get_pets·addPet·getPet·delete_pets_petId·uploadPhoto)
TOOLS_JSON='["get_pets", "getPet", "addPet"]'
BP_NAME="petstore-asst"; MS_NAME="petstore-asst-e2e"; DS_NAME="pg-asst-e2e"
# STEP 9 가 복제해 두고(viewer 는 배포가 403 이라 draft 로 남는다) STEP 11 이 교차 파드로 배포한다
MS2_NAME="petstore-asst-x"; MS2_DISPLAY="펫스토어 교차 파드"
AG_NAME="petstore-asst"
BP_DISPLAY="펫스토어 REST"; MS_DISPLAY="펫스토어 MCP 서버"; DS_DISPLAY="비서 e2e DB"
AG_DISPLAY="펫스토어 에이전트"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-assistant.XXXXXX")"
K=(kubectl --context "$CTX")
RUN_ID="$(date +%s)-$$"
PF_API=""
HOME_ID=""; SID=""; BP_ID=""; MS_ID=""; AG_ID=""; DS_ID=""
SETTINGS_PATCHED=0        # STEP 1 이 settings 를 바꿨으면 1. cleanup 이 $WORK/settings.orig.json 으로 되돌린다
PROVIDER_ADDED=0          # STEP 1 이 stub provider 를 새로 넣었으면 1. 복원이 다시 걷는다
SESSION_DELETED=0         # STEP 8 이 세션을 지웠으면 1

# -- oidc 모드 (T9) --------------------------------------------------------------------
AUTH_MODE="${E2E_AUTH_MODE:-header}"
OIDC_PORT="${E2E_OIDC_PORT:-$PORT_ASSISTANT_OIDC}"
POD_A_PORT="${E2E_POD_A_PORT:-$PORT_ASSISTANT_POD_A}"
POD_B_PORT="${E2E_POD_B_PORT:-$PORT_ASSISTANT_POD_B}"
OIDC="http://127.0.0.1:${OIDC_PORT}"
# 부트스트랩 관리자 그룹. deploy/dev/values-local.yaml 의 auth.bootstrapAdminGroup 과 같아야 한다
BOOT_GROUP="${E2E_BOOT_GROUP:-platform-admins}"
DEV_GROUP="daegam-${TENANT}-dev"; VIEW_GROUP="daegam-${TENANT}-view"
# STEP 0~8 의 기본 주체. header 모드면 빈 값이고 oidc 면 STEP 0 이 부트스트랩 관리자 토큰을 넣는다. api 는 AUTH 를, sse_post 는
# ADMIN_TOKEN 을 싣는다
ADMIN_TOKEN=""; AUTH=(); DEV_TOKEN=""; VIEW_TOKEN=""
EXPECT_SUB="header"       # 감사 행의 sub. oidc 면 부트스트랩 관리자의 sub 다
PF_OIDC=""; PF_POD_A=""; PF_POD_B=""
POD_A=""; POD_B=""; API_A=""; API_B=""
SID_VIEW=""; SID_DEV=""; SID_X=""; SID_X2=""   # STEP 9~11 이 만드는 세션. cleanup 이 지운다
SID_P=""                  # STEP 13 이 만드는 제안 세션. 관리자 주체의 것이라 cleanup 이 SID 와 같이 지운다
MS2_ID=""                 # STEP 9 가 복제하고 STEP 11 이 교차 파드로 배포하는 MCP Server

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$OIDC_PORT" "$POD_A_PORT" "$POD_B_PORT"
# roll_control_plane: 인증 모드를 바꾸면 플랫폼과 UI 를 함께 굴린다(UI 의 CSP 가 기동 때 모드를 읽는다, D52)
. "$HERE/lib/e2e_control_plane.sh"
# switch_auth_mode·restore_auth_mode·wait_auth_reapplied 는 여기에 있다 (STEP 0 이 바꾸고 cleanup 이 되돌린다)
. "$HERE/lib/e2e_auth_mode.sh"
# pass·skip·cond_skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"

# api·api_as·jq_·find_id·api_delete·wait_ready·wait_gone 은 여기에 있다. mock-oidc 토큰(user_token)은 lib/e2e_auth_mode.sh 에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_ 는 여기에 있다. 턴은 아래 sse_req 가 chat 으로 보낸다
. "$HERE/lib/e2e_sse.sh"
# group_mappings_add(STEP 0 이 더한다)·group_mappings_restore(cleanup 이 되돌린다)와 MAPPINGS_PUT 은 여기에 있다
. "$HERE/lib/e2e_group_mappings.sh"

# req BASE TOKEN METHOD PATH [JSON] -> TOKEN 으로 부르는 api 다. BASE 는 svc($API) 또는 파드 하나($API_A·$API_B, STEP 11)다.
# api 가 읽는 API 를 이 호출 동안만 바꾼다 (bash 의 local 은 부른 함수에도 보인다)
req() {
  local API="$1"
  shift
  api_as "$@"
}
# sse_req BASE TOKEN PATH json-body|@file [extra curl args...] -> TOKEN 으로 부르는 chat 이다. 상태 코드를 찍고 SSE 본문은
# SSE_OUT(기본 $WORK/sse)이다. 배경으로 돌릴 때는 `SSE_OUT=... sse_req ... &` 처럼 앞에 붙여 파일을 가른다 (STEP 12).
# chat 이 읽는 API·AUTH 를 이 호출 동안만 바꾼다 (req 와 같다). header 모드의 빈 토큰은 Authorization 을 싣지 않는다
sse_req() {
  local API="$1" AUTH=()
  [ -z "$2" ] || AUTH=(-H "Authorization: Bearer $2")
  shift 2
  chat "$@"
}
# sse_post PATH json-body|@file [extra curl args...] -> 상태 코드. SSE 본문은 $WORK/sse. 한 턴은 몇 초면 끝난다
sse_post() { sse_req "$API" "$ADMIN_TOKEN" "$@"; }
# 리소스 하나를 지우고(api_delete) WAIT 가 1 이면 사라질 때까지 기다린다. cleanup·preclean 용이라 die 하지 않는다
delete_path() { # PATH LABEL [wait 0|1]
  api_delete "$1"
  [ "${3:-0}" = 1 ] || return 0
  wait_gone "$1" || echo "!! $2: $1 still answers after the delete"
}
delete_named() { # LIST-PATH NAME LABEL [wait 0|1] -> 이름으로 찾아 지운다
  local list="$1" name="$2" label="$3" wait="${4:-0}" id code
  code="$(api GET "$list")" || true
  [ "$code" = "200" ] || return 0
  id="$(find_id "$name")"
  [ -n "$id" ] || return 0
  delete_path "$list/$id" "$label $name" "$wait"
}

# STEP 1 이 바꾼 Platform Settings 를 원래대로 (llm_providers·defaults·assistant 한 번에. 원래 없던 stub 은 목록에서 빠진다)
restore_settings() {
  [ "$SETTINGS_PATCHED" = 1 ] || return 0
  SETTINGS_PATCHED=0
  local patch code
  patch="$(python3 - "$WORK/settings.orig.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(json.dumps({"llm_providers": s["llm_providers"], "defaults": s["defaults"], "assistant": s["assistant"]}))
PY
)" || { echo "!! settings restore payload failed. Platform Settings 에서 provider ${STUB_PROVIDER}·assistant·defaults 를 손으로 되돌린다"; return 0; }
  code="$(api PATCH /settings "$patch")" || true
  if [ "$code" = "200" ]; then
    if [ "$PROVIDER_ADDED" = 1 ]; then local note="stub provider $STUB_PROVIDER removed"; else local note="stub provider $STUB_PROVIDER was there before, kept"; fi
    echo ">> Platform Settings restored: $note; assistant.enabled=$(jq_ 'd["assistant"]["enabled"]') defaults.assistant=$(jq_ 'd["defaults"]["assistant"]') providers=$(jq_ '[p["name"] for p in d["llm_providers"]]')"
  else
    echo "!! settings restore: PATCH /settings -> $code. Platform Settings 에서 provider ${STUB_PROVIDER}·assistant·defaults 를 손으로 되돌린다"
  fi
}

cleanup() {
  if [ "$KEEP" = 1 ]; then
    echo ">> KEEP=1: resources, session and Platform Settings are left in place"
    echo "   agent=${AG_ID:-none} mcp server=${MS_ID:-none} mcp server(step 9)=${MS2_ID:-none} backend=${BP_ID:-none} session=${SID:-none}"
    if [ "$SETTINGS_PATCHED" = 1 ]; then
      echo "   settings to restore (PATCH $API/settings):"
      python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print("   " + json.dumps({"llm_providers": s["llm_providers"], "defaults": s["defaults"], "assistant": s["assistant"]}, ensure_ascii=False))' "$WORK/settings.orig.json" 2>/dev/null || true
    fi
  else
    # 세션은 settings 를 되돌리기 전에 지운다 (assistant.enabled 가 꺼지면 세션 API 가 404 다)
    if [ -n "$SID" ] && [ "$SESSION_DELETED" != 1 ]; then delete_path "/assistant/sessions/$SID" "session"; fi
    if [ -n "$SID_P" ]; then delete_path "/assistant/sessions/$SID_P" "proposal session"; fi
    # STEP 9~11 의 세션은 만든 주체만 지울 수 있다. 관리자 토큰으로는 404 라 각자의 토큰으로 부른다
    if [ -n "$SID_VIEW" ]; then req "$API" "$VIEW_TOKEN" DELETE "/assistant/sessions/$SID_VIEW" >/dev/null 2>&1; fi
    for s in "$SID_DEV" "$SID_X" "$SID_X2"; do
      if [ -n "$s" ]; then req "$API" "$DEV_TOKEN" DELETE "/assistant/sessions/$s" >/dev/null 2>&1; fi
    done
    if [ -n "$AG_ID" ]; then delete_path "/agents/$AG_ID" "agent" 1; fi
    if [ -n "$MS2_ID" ]; then delete_path "/mcp-servers/$MS2_ID" "mcp server (step 9)" 1; fi
    if [ -n "$MS_ID" ]; then delete_path "/mcp-servers/$MS_ID" "mcp server" 1; fi
    if [ -n "$BP_ID" ]; then delete_path "/backend-apis/$BP_ID" "backend api"; fi
    if [ -n "$DS_ID" ]; then delete_path "/data-services/$DS_ID" "data service"; fi
    restore_settings
    group_mappings_restore
  fi
  # 여기까지는 지금 열린 경로와 지금 모드로 API 에 닿는다. 모드 복원과 port-forward 종료는 그다음이다. 되돌린 뒤
  # 재적용을 기다리는 호출도 API port-forward 로 간다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_API" "$API_PORT"
  if [ -n "$PF_OIDC" ]; then stop_pf "$PF_OIDC" "$OIDC_PORT"; fi
  if [ -n "$PF_POD_A" ]; then stop_pf "$PF_POD_A" "$POD_A_PORT"; fi
  if [ -n "$PF_POD_B" ]; then stop_pf "$PF_POD_B" "$POD_B_PORT"; fi
  rm -rf "$WORK"
}
on_exit cleanup

echo ">> context=$CTX platform_ns=$PLATFORM_NS spike_ns=$SPIKE_NS tenant=$TENANT api=$API keep=$KEEP spec=$FIXTURE"
[ -f "$FIXTURE" ] || die 0 "spec fixture $FIXTURE not found"

# ---------- 0. platform reachable + auth mode + home cluster; leftovers ----------
# oidc 모드면 배포본을 먼저 그 모드로 돌려 놓는다. cleanup 의 restore_auth_mode 가 원래 값으로 되돌린다
case "$AUTH_MODE" in
  header) ;;
  oidc)
    switch_auth_mode oidc || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to oidc and rolling the control plane failed" ;;
  *) die 0 "E2E_AUTH_MODE must be header or oidc (got $AUTH_MODE)" ;;
esac
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 0 "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = "200" ] || die 0 "GET /healthz -> $code"
# 배포본이 요청한 모드인지 본다. 무토큰 GET /clusters 는 header 모드면 200, oidc 모드면 401 이다
code="$(api GET /clusters)"
if [ "$AUTH_MODE" = "oidc" ]; then
  [ "$code" = "401" ] || die 0 "expected 401 without a token in oidc mode, got $code (did the rollout pick up DAEGAM_AUTH_MODE=oidc?)"
  start_pf "$PLATFORM_NS" svc/mock-oidc "${OIDC_PORT}:8080"
  PF_OIDC=$PF_PID
  wait_port "$OIDC_PORT" || die 0 "port-forward to svc/mock-oidc did not open :$OIDC_PORT"
  ADMIN_TOKEN="$(user_token '{"sub": "boot-admin", "groups": ["'"$BOOT_GROUP"'"], "aud": "daegam-api"}')" \
    || die 0 "bootstrap admin token from $OIDC/_test/token failed"
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN"); EXPECT_SUB="boot-admin"
  code="$(api GET /me)"
  [ "$code" = "200" ] && [ "$(jq_ 'd["is_platform_admin"]')" = "True" ] \
    || die 0 "GET /me with the bootstrap admin token -> $code $(cat "$WORK/body")"
  # 모드를 바꾸면 플랫폼이 MCPOIDCConfig 와 살아 있는 소비자를 다시 적용한다. 다 될 때까지 기다린다 (P32)
  wait_auth_reapplied "$ADMIN_TOKEN" || die 0 "data-plane auth was not re-applied after switching to oidc"
  # 그룹 매핑: 원래 표를 받아 두고 이 테넌트의 developer·viewer 를 더한다 (PUT 은 전체 교체다)
  group_mappings_add 0 "$TENANT" "$DEV_GROUP=developer" "$VIEW_GROUP=viewer"
  DEV_TOKEN="$(user_token '{"sub": "dev-e2e", "groups": ["'"$DEV_GROUP"'"], "aud": "daegam-api"}')" || die 0 "developer token failed"
  VIEW_TOKEN="$(user_token '{"sub": "view-e2e", "groups": ["'"$VIEW_GROUP"'"], "aud": "daegam-api"}')" || die 0 "viewer token failed"
  AUTH_NOTE="oidc (was ${AUTH_MODE_PREV:-unset}; no-token 401; data-plane auth re-applied; boot-admin, dev-e2e=$DEV_GROUP, view-e2e=$VIEW_GROUP)"
else
  [ "$code" = "200" ] || die 0 "GET /clusters -> $code (expected 200: header mode. ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE)"
  AUTH_NOTE="header (X-Daegam-Tenant only)"
fi
# 위의 oidc 분기가 $WORK/body 를 덮어썼다. 아래 HOME_ID·HOME_OK 는 이 응답에서 뽑는다
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 0 "GET /clusters -> $code"
# 없으면 바로 아래 줄이 스스로 다룬다
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"]=="home"), "")')"
{ [ -n "$HOME_ID" ] && [ "$HOME_OK" = "True" ]; } || die 0 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
api GET /version >/dev/null
VERSION="$(jq_ 'd.get("api_build") or d.get("api_version")')"
code="$(api GET /assistant/config)"
[ "$code" = "200" ] || die 0 "GET /assistant/config -> $code (the deployed platform image lacks the assistant API? rebuild with make image-platform && make deploy-local)"
# 이전 실행이 남긴 같은 이름의 리소스는 지운다. 스텁 steps 안의 create 가 409 name_conflict 를 받으면 "$n.id" 가 비어 뒤가 다 깨진다
delete_named /agents "$AG_NAME" "leftover agent" 1
delete_named /mcp-servers "$MS2_NAME" "leftover mcp server (step 9)" 1
delete_named /mcp-servers "$MS_NAME" "leftover mcp server" 1
delete_named /backend-apis "$BP_NAME" "leftover backend api"
delete_named /data-services "$DS_NAME" "leftover data service"
pass 0 "platform reachable :$API_PORT (version $VERSION), auth mode $AUTH_NOTE, cluster home id=$HOME_ID preflight ok; leftovers cleared"

# ---------- 1. settings: stub provider (idempotent) + assistant section + defaults.assistant ----------
code="$(api GET /settings)"
[ "$code" = "200" ] || die 1 "GET /settings -> $code"
[ "$(jq_ '"assistant" in d and "assistant" in d["defaults"]')" = "True" ] || die 1 "GET /settings has no assistant section (platform image predates P14 F1b)"
cp "$WORK/body" "$WORK/settings.orig.json"
ORIG_NOTE="$(jq_ '"assistant.enabled=%s confirm_deploy=%s defaults.assistant=%s providers=%s" % (d["assistant"]["enabled"], d["assistant"]["confirm_deploy"], d["defaults"]["assistant"], [p["name"] for p in d["llm_providers"]])')"
if [ "$(jq_ "[p['name'] for p in d['llm_providers'] if p['name']=='$STUB_PROVIDER' and p['kind']=='stub']")" = "['$STUB_PROVIDER']" ]; then
  HAS_STUB=1
else
  HAS_STUB=0
fi
patch="$(python3 - "$WORK/settings.orig.json" "$STUB_PROVIDER" "$HAS_STUB" <<'PY'
import json, sys
s = json.load(open(sys.argv[1])); name = sys.argv[2]; has_stub = sys.argv[3] == "1"
providers = s["llm_providers"]
if not has_stub:
    providers = [p for p in providers if p["name"] != name]
    providers.append({"name": name, "kind": "stub"})
defaults = dict(s["defaults"]); defaults["assistant"] = name
assistant = dict(s["assistant"]); assistant.update({"enabled": True, "confirm_deploy": True})
print(json.dumps({"llm_providers": providers, "defaults": defaults, "assistant": assistant}))
PY
)"
SETTINGS_PATCHED=1
PROVIDER_ADDED=$((1 - HAS_STUB))
code="$(api PATCH /settings "$patch")"
if [ "$code" = "422" ] && [ "$(jq_ "d['detail']['code']")" = "stub_provider_disallowed" ]; then
  die 1 "stub provider rejected: the deployment needs DAEGAM_ALLOW_STUB_PROVIDER=true in ConfigMap daegam-platform-config (platform.extraConfig of deploy/dev/values-local.yaml) and a rollout"
fi
[ "$code" = "200" ] || die 1 "PATCH /settings -> $code"
[ "$(jq_ "[p['kind'] for p in d['llm_providers'] if p['name']=='$STUB_PROVIDER'][0]")" = "stub" ] || die 1 "provider not stored: $(jq_ 'd["llm_providers"]')"
[ "$(jq_ 'd["defaults"]["assistant"], d["assistant"]["enabled"], d["assistant"]["confirm_deploy"]')" = "$STUB_PROVIDER True True" ] \
  || die 1 "settings after patch: defaults.assistant=$(jq_ 'd["defaults"]["assistant"]') assistant=$(jq_ 'd["assistant"]')"
LIMITS="$(jq_ 'd["assistant"]["max_tool_calls_per_turn"], d["assistant"]["turn_timeout_seconds"], d["assistant"]["effort"]')"
if [ "$HAS_STUB" = 1 ]; then PROVIDER_NOTE="already registered"; else PROVIDER_NOTE="registered (cleanup removes it)"; fi
pass 1 "provider $STUB_PROVIDER (kind stub) $PROVIDER_NOTE; assistant{enabled, confirm_deploy} on, defaults.assistant=$STUB_PROVIDER (limits/effort: $LIMITS). was: $ORIG_NOTE (restored on exit)"

# ---------- 2. config ----------
code="$(api GET /assistant/config)"
[ "$code" = "200" ] || die 2 "GET /assistant/config -> $code"
[ "$(jq_ 'd["enabled"], d["provider"], d["confirm_deploy"]')" = "True $STUB_PROVIDER True" ] || die 2 "config: $(cat "$WORK/body")"
[ "$(jq_ 'sorted(d["limits"].keys())')" = "['max_tool_calls_per_turn', 'turn_timeout_seconds']" ] || die 2 "config.limits: $(jq_ 'd["limits"]')"
pass 2 "GET /assistant/config enabled=true provider=$STUB_PROVIDER confirm_deploy=true limits=$(jq_ 'd["limits"]')"

# ---------- 3. session + turn 1: verify → backend api → mcp server 저장 → 배포 (interrupt) ----------
code="$(api POST /assistant/sessions '{"title": "petstore e2e"}')"
[ "$code" = "201" ] || die 3 "POST /assistant/sessions -> $code"
SID="$(jq_ 'd["id"]')"
[ -n "$SID" ] && [ "$SID" != "None" ] || die 3 "session id missing: $(cat "$WORK/body")"
python3 - "$FIXTURE" "$WORK/turn1.json" "$BP_NAME" "$BP_DISPLAY" "$PETSTORE_ENDPOINT" "$FILENAME" "$MS_NAME" "$MS_DISPLAY" "$HOME_ID" "$TOOLS_JSON" <<'PY'
import json, sys
spec = open(sys.argv[1], encoding="utf-8").read()
out, bp, bp_disp, endpoint, filename, ms, ms_disp, cluster, tools = sys.argv[2:11]
tools = json.loads(tools)
# 만들기는 저장이고 배포는 따로 누른다 (D42). 저장은 write 라 그대로 돌고 배포만 승인 대기가 된다
steps = [
    {"tool": "verify_spec", "args": {"text": spec, "filename": filename}},
    {"tool": "create_backend_api", "args": {
        "name": bp, "display_name": bp_disp, "endpoint": endpoint, "auth": "none",
        "spec_text": spec, "filename": filename}},
    {"tool": "create_mcp_server", "args": {
        "name": ms, "display_name": ms_disp,
        "backends": [{"backend_profile_id": "$1.id", "spec_version": "$1.version", "tools": tools}]}},
    {"tool": "deploy_mcp_server", "args": {"server_id": "$2.id", "cluster_id": cluster}},
    {"tool": "get_mcp_server", "args": {"server_id": "$3.id"}},
    {"say": "MCP Server $3.id 를 배포했다. 지금 상태는 $4.status.phase 다."},
]
body = {"message": json.dumps({"steps": steps}, ensure_ascii=False), "context": {"route": "/backend-apis"}}
json.dump(body, open(out, "w"), ensure_ascii=False)
PY
RID_T1="e2e-asst-$RUN_ID-t1"
code="$(sse_post "/assistant/sessions/$SID/turns" "@$WORK/turn1.json" -H "X-Request-ID: $RID_T1")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 3 "POST /turns -> $code"; }
NAMES="$(sse_ 'names')"
[ "$NAMES" = "['tool_call', 'tool_result', 'tool_call', 'tool_result', 'resource', 'tool_call', 'tool_result', 'resource', 'interrupt']" ] \
  || die 3 "event order: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ '[d["name"] for e, d in events if e == "tool_call"]')" = "['verify_spec', 'create_backend_api', 'create_mcp_server']" ] || die 3 "tool_call names: $(sse_ '[d["name"] for e, d in events if e == "tool_call"]')"
[ "$(sse_ '[d["status"] for e, d in events if e == "tool_result"]')" = "['success', 'success', 'success']" ] \
  || die 3 "tool_result statuses: $(sse_ '[(d["name"], d["status"], d["content"][:300]) for e, d in events if e == "tool_result"]')"
BP_ID="$(sse_ 'content(1)["id"]')"; BP_VER="$(sse_ 'content(1)["version"]')"
MS_ID="$(sse_ 'content(2)["id"]')"; MS_REV="$(sse_ 'content(2)["revision"]')"
{ [ -n "$BP_ID" ] && [ -n "$MS_ID" ]; } || die 3 "ids missing from tool results (4KB cut?): bp=$BP_ID ms=$MS_ID"
[ "$(sse_ 'content(1)["name"], content(2)["name"]')" = "$BP_NAME $MS_NAME" ] || die 3 "create results: $(sse_ 'content(1).get("name"), content(2).get("name")')"
# 만들기는 저장이라 아직 draft 다 (D42)
[ "$(sse_ 'content(2)["state"]')" = "draft" ] || die 3 "create_mcp_server state: $(sse_ 'content(2).get("state")') (expected draft)"
[ "$(sse_ '[(d["kind"], d["id"], d["name"]) for e, d in events if e == "resource"]')" = "[('backend-api', '$BP_ID', '$BP_NAME'), ('mcp-server', '$MS_ID', '$MS_NAME')]" ] \
  || die 3 "resource events: $(sse_ '[d for e, d in events if e == "resource"]')"
[ "$(sse_ 'first("interrupt")["session_id"], first("interrupt")["kind"], first("interrupt")["action_requests"][0]["name"]')" = "$SID confirm deploy_mcp_server" ] \
  || die 3 "interrupt: $(sse_ 'first("interrupt")')"
# 배포 본문은 이름을 받지 않는다. 승인 카드에 실리는 것은 server_id 와 cluster_id 다
[ "$(sse_ 'first("interrupt")["action_requests"][0]["args"]["server_id"], first("interrupt")["action_requests"][0]["args"]["cluster_id"]')" = "$MS_ID $HOME_ID" ] \
  || die 3 "interrupt args: $(sse_ 'first("interrupt")["action_requests"][0]["args"]')"
[ "$(sse_ 'first("interrupt")["review_configs"][0]["allowed_decisions"]')" = "['approve', 'reject']" ] || die 3 "review_configs: $(sse_ 'first("interrupt")["review_configs"]')"
code="$(api GET "/assistant/sessions/$SID")"
[ "$code" = "200" ] || die 3 "GET /assistant/sessions/$SID -> $code"
[ "$(jq_ 'd["state"], d["pending"]["kind"], d["pending"]["calls"][0]["name"], d["pending"]["calls"][0]["risk"], len(d["pending"]["calls"])')" = "pending confirm deploy_mcp_server deploy 1" ] \
  || die 3 "session after interrupt: state=$(jq_ 'd["state"]') pending=$(jq_ 'd["pending"]')"
CALL1="$(jq_ 'd["pending"]["calls"][0]["id"]')"
[ -n "$CALL1" ] && [ "$CALL1" != "None" ] || die 3 "pending.calls[0].id missing"
[ "$(jq_ 'd["transcript"][-1]["kind"], d["transcript"][-1]["resolved"], d["turns"], d["title"]')" = "interrupt False 1 petstore e2e" ] || die 3 "transcript/turns: $(jq_ '[it["kind"] for it in d["transcript"]], d["turns"], d["title"]')"
# 만들어진 것들을 API 로 한 번 더 본다 (비서가 부른 것은 곧 플랫폼 API 다). 도구 결과는 4KB 를 넘으면 중첩 키가
# 떨어지므로(tools.py truncate) 중첩 값은 상세 GET 으로 본다
code="$(api GET "/backend-apis/$BP_ID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["endpoint"], d["auth"], d["latest_version"]')" = "$PETSTORE_ENDPOINT none $BP_VER" ] || die 3 "backend api $BP_ID: $code $(cat "$WORK/body")"
code="$(api GET "/mcp-servers/$MS_ID")"
[ "$code" = "200" ] || die 3 "GET /mcp-servers/$MS_ID -> $code"
[ "$(jq_ 'd["backends"][0]["backend_profile_id"], d["backends"][0]["spec_version"], d["backends"][0]["tools"]')" = "$BP_ID $BP_VER $(python3 -c 'import json,sys; print(json.loads(sys.argv[1]))' "$TOOLS_JSON")" ] \
  || die 3 "mcp server backends: $(jq_ 'd["backends"]') (expected \$1.id/\$1.version substitution)"
pass 3 "session $SID; turn 1 $NAMES; backend $BP_NAME id=$BP_ID v$BP_VER endpoint=$PETSTORE_ENDPOINT; mcp server $MS_NAME id=$MS_ID rev=$MS_REV tools=$TOOLS_JSON draft 로 저장; interrupt confirm deploy_mcp_server -> pending call $CALL1"

# ---------- 4. approve → deploy_mcp_server 201 → get → done; Ready ----------
RID_D1="e2e-asst-$RUN_ID-d1"
code="$(sse_post "/assistant/sessions/$SID/decisions" "{\"decisions\": [{\"id\": \"$CALL1\", \"type\": \"approve\"}]}" -H "X-Request-ID: $RID_D1")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 4 "POST /decisions approve -> $code"; }
NAMES="$(sse_ 'names')"
[ "$NAMES" = "['tool_call', 'tool_result', 'resource', 'tool_call', 'tool_result', 'resource', 'token', 'done']" ] || die 4 "event order after approve: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ 'nth("tool_call", 0)["id"], nth("tool_call", 0)["name"], nth("tool_call", 1)["name"]')" = "$CALL1 deploy_mcp_server get_mcp_server" ] || die 4 "tool_calls after approve: $(sse_ '[d for e, d in events if e == "tool_call"]')"
[ "$(sse_ 'nth("tool_result", 0)["status"], nth("tool_result", 1)["status"]')" = "success success" ] || die 4 "tool_results after approve: $(sse_ '[(d["name"], d["status"], d["content"][:300]) for e, d in events if e == "tool_result"]')"
# 배포는 저장한 그 문서를 띄운다. 새 id 가 생기지 않는다 (D42)
[ "$(sse_ 'content(0)["id"]')" = "$MS_ID" ] || die 4 "deploy returned another id: $(sse_ 'content(0).get("id")') (expected $MS_ID)"
[ "$(sse_ 'content(0)["name"]')" = "$MS_NAME" ] || die 4 "deploy_mcp_server result: $(sse_ 'nth("tool_result", 0)["content"][:600]')"
[ "$(sse_ 'nth("resource", 0)["kind"], nth("resource", 0)["id"], nth("resource", 0)["url"]')" = "mcp-server $MS_ID /mcp-servers/$MS_ID" ] || die 4 "resource after approve: $(sse_ 'nth("resource", 0)')"
DONE1="$(sse_ 'first("done")["session_id"], first("done")["turn"], first("done")["tool_calls"]')"
[ "$DONE1" = "$SID 1 5" ] || die 4 "done: $(sse_ 'first("done")') (expected session $SID turn 1 tool_calls 5)"
DONE_TEXT="$(sse_ 'first("done")["text"]')"
case "$DONE_TEXT" in *"$MS_ID"*) ;; *) die 4 "done.text lacks the server id (\$3.id substitution): $DONE_TEXT" ;; esac
USAGE1="$(sse_ 'first("done")["usage"]')"
code="$(api GET "/assistant/sessions/$SID")"
[ "$(jq_ 'd["state"], d["pending"], all(it["resolved"] for it in d["transcript"] if it["kind"]=="interrupt"), d["transcript"][-1]["kind"]')" = "idle None True done" ] \
  || die 4 "session after approve: $(jq_ 'd["state"], d["pending"], [it["kind"] for it in d["transcript"]]')"
echo ">> waiting up to ${READY_TIMEOUT}s for mcp server $MS_ID phase=Ready"
wait_ready "/mcp-servers/$MS_ID" 4
MS_URL="$(jq_ '(d.get("status") or {}).get("url")')"
[ "$(jq_ 'd["k8s"]["name"], d["state"], d["deployed_revision"]')" = "mcp-$MS_NAME deployed $MS_REV" ] \
  || die 4 "mcp server $MS_ID: k8s=$(jq_ 'd["k8s"]') state=$(jq_ 'd["state"]') deployed_revision=$(jq_ 'd["deployed_revision"]')"
pass 4 "approve $CALL1 -> $NAMES; mcp server $MS_NAME id=$MS_ID (mcp-$MS_NAME) deployed r$MS_REV Ready url=$MS_URL; done turn=1 tool_calls=5 usage=$USAGE1 text='$DONE_TEXT'"

# ---------- 5. turn 2: validate → agent 제안 (interrupt) → approve → deploy (interrupt) → approve → navigate → done; Ready ----------
python3 - "$WORK/turn2.json" "$AG_NAME" "$AG_DISPLAY" "$STUB_PROVIDER" "$MS_ID" "$MS_NAME" "$HOME_ID" "$TOOLS_JSON" <<'PY'
import json, sys
out, ag, ag_disp, provider, ms_id, ms_name, cluster, tools = sys.argv[1:9]
tools = json.loads(tools)
# 최소 그래프. 템플릿(GET /agents/templates)은 한글 프롬프트·가드레일·메모리 노드까지 들어 create 응답이 4KB 에
# 가까워 "$1.id" 참조가 위험하다. 노드 data 의 나머지 필드는 agent_graph.py 의 기본값이다
graph = {
    "nodes": [
        {"id": "entry", "type": "entry", "data": {"greeting": "안녕하세요. 펫스토어 안내를 도와드립니다."}},
        {"id": "agent-main", "type": "agent", "data": {
            "role": "main", "name": "petstore", "description": "펫스토어 조회를 맡는 메인 에이전트다.",
            "system_prompt": "너는 펫스토어 상담 에이전트다. 반려동물 조회와 등록은 도구를 쓴다.", "language": "ko"}},
        {"id": "model-main", "type": "model", "data": {"name": "기본 모델", "provider": provider}},
        {"id": "tools-petstore", "type": "tools", "data": {
            "source": {"kind": "mcp_instance", "id": ms_id}, "allow_tools": tools}},
    ],
    "edges": [
        {"id": "e-entry", "source": "entry", "target": "agent-main", "kind": "entry"},
        {"id": "e-model", "source": "agent-main", "target": "model-main", "kind": "model"},
        {"id": "e-tools", "source": "agent-main", "target": "tools-petstore", "kind": "tools"},
    ],
}
service = {}
# 만들기는 제안이고 승인이 저장한다 (P48 6.1절). 배치는 캔버스가 정하므로 보내지 않는다. 이름은 여기서 정해지고 배포
# 본문은 agent_id 와 cluster_id 만 받는다 (D42). 쓰기 Tool 의 승인은 Database 가 있어야 하므로 이 최소 그래프에는 없다
steps = [
    {"tool": "validate_agent_graph", "args": {"graph": graph, "service": service}},
    {"tool": "create_agent", "args": {
        "name": ag, "display_name": ag_disp, "graph": graph, "service": service}},
    {"tool": "deploy_agent", "args": {"agent_id": "$1.id", "cluster_id": cluster}},
    {"tool": "navigate", "args": {"to": "/agents/$2.id"}},
    {"say": "Agent $2.id 를 배포했다. Ready 가 되면 화면에서 채팅할 수 있다."},
]
ctx = {"route": f"/mcp-servers/{ms_id}", "resource": {"kind": "mcp-server", "id": ms_id, "name": ms_name}}
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False), "context": ctx}, open(out, "w"), ensure_ascii=False)
PY
RID_T2="e2e-asst-$RUN_ID-t2"
code="$(sse_post "/assistant/sessions/$SID/turns" "@$WORK/turn2.json" -H "X-Request-ID: $RID_T2")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 5 "POST /turns (turn 2) -> $code"; }
NAMES="$(sse_ 'names')"
[ "$NAMES" = "['tool_call', 'tool_result', 'interrupt']" ] || die 5 "turn 2 event order: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ '[d["name"] for e, d in events if e == "tool_call"]')" = "['validate_agent_graph']" ] || die 5 "turn 2 tool_calls: $(sse_ '[d["name"] for e, d in events if e == "tool_call"]')"
[ "$(sse_ 'nth("tool_result", 0)["status"], content(0)["ok"], [i for i in content(0)["issues"] if i["level"] == "error"]')" = "success True []" ] \
  || die 5 "validate_agent_graph: $(sse_ 'nth("tool_result", 0)["content"][:600]')"
VAL_ISSUES="$(sse_ '[i["code"] for i in content(0)["issues"]]')"
# Agent 정의는 제안이다. 플랫폼이 검사한 뒤 카드의 덩어리와 예약 칸을 실어 기다린다 (P48 6.1절)
[ "$(sse_ 'first("interrupt")["kind"], first("interrupt")["action_requests"][0]["name"], first("interrupt")["action_requests"][0]["proposal"]["kind"], "layout" in first("interrupt")["action_requests"][0]["args"]')" = "confirm create_agent create False" ] \
  || die 5 "proposal interrupt: $(sse_ 'first("interrupt")')"
PROPOSAL_MARKS="$(sse_ '[m["code"] for m in first("interrupt")["action_requests"][0]["proposal"]["attention"]]')"
# 만들기는 모든 노드를 더한 것이고, 새 에이전트의 프롬프트와 새 Tool 소스를 먼저 볼 것으로 앞세운다 (P48 6.4절)
[ "$(sse_ '(lambda p: (sorted(n["id"] for n in p["changes"]["nodes"]["added"]), p["changes"]["nodes"]["changed"], p["changes"]["nodes"]["removed"], {"system_prompt_changed", "tool_source_added"} <= {m["code"] for m in p["attention"]}))(first("interrupt")["action_requests"][0]["proposal"])')" = "['agent-main', 'entry', 'model-main', 'tools-petstore'] [] [] True" ] \
  || die 5 "creation card: $(sse_ 'first("interrupt")["action_requests"][0]["proposal"]')"
code="$(api GET "/assistant/sessions/$SID")"
[ "$(jq_ 'd["state"], d["pending"]["kind"], d["pending"]["calls"][0]["name"]')" = "pending confirm create_agent" ] || die 5 "session after the proposal: $(jq_ 'd["state"], d["pending"]')"
CALL_P="$(jq_ 'd["pending"]["calls"][0]["id"]')"; DIGEST="$(jq_ 'd["pending"]["calls"][0]["args"]["_proposal"]["digest"]')"
[ -n "$DIGEST" ] || die 5 "the waiting proposal has no digest: $(jq_ 'd["pending"]')"
# 지문 없는 결정은 내용을 보지 않은 결정이라 409 다
code="$(api POST "/assistant/sessions/$SID/decisions" "{\"decisions\": [{\"id\": \"$CALL_P\", \"type\": \"approve\"}]}")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code']")" = "proposal_changed" ] || die 5 "approve without the digest -> $code $(cat "$WORK/body") (expected 409 proposal_changed)"
RID_DP="e2e-asst-$RUN_ID-dp"
code="$(sse_post "/assistant/sessions/$SID/decisions" "{\"decisions\": [{\"id\": \"$CALL_P\", \"type\": \"approve\", \"proposal_digest\": \"$DIGEST\"}]}" -H "X-Request-ID: $RID_DP")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 5 "POST /decisions approve (proposal) -> $code"; }
NAMES_P="$(sse_ 'names')"
[ "$NAMES_P" = "['tool_call', 'tool_result', 'resource', 'interrupt']" ] || die 5 "event order after the proposal approve: $NAMES_P $(cat "$WORK/sse")"
[ "$(sse_ 'nth("tool_call", 0)["id"], nth("tool_call", 0)["name"], "_proposal" in nth("tool_call", 0)["args"], nth("tool_result", 0)["status"]')" = "$CALL_P create_agent False success" ] \
  || die 5 "create_agent: $(sse_ 'nth("tool_result", 0)["content"][:600]')"
AG_ID="$(sse_ 'content(0)["id"]')"; AG_REV="$(sse_ 'content(0)["revision"]')"
[ -n "$AG_ID" ] || die 5 "agent id missing from the create result (4KB cut?): $(sse_ 'nth("tool_result", 0)["content"][:400]')"
# 만들기는 저장이라 아직 draft 다 (D42)
[ "$(sse_ 'content(0)["state"]')" = "draft" ] || die 5 "create_agent state: $(sse_ 'content(0).get("state")') (expected draft)"
[ "$(sse_ 'first("resource")["kind"], first("resource")["id"], first("resource")["name"]')" = "agent $AG_ID $AG_NAME" ] || die 5 "resource: $(sse_ 'first("resource")')"
# 배포 본문은 이름을 받지 않는다. 승인 카드에 실리는 것은 agent_id 와 cluster_id 다
[ "$(sse_ 'first("interrupt")["kind"], first("interrupt")["action_requests"][0]["name"], first("interrupt")["action_requests"][0]["args"]["agent_id"], first("interrupt")["action_requests"][0]["args"]["cluster_id"]')" = "confirm deploy_agent $AG_ID $HOME_ID" ] \
  || die 5 "interrupt: $(sse_ 'first("interrupt")')"
code="$(api GET "/assistant/sessions/$SID")"
[ "$(jq_ 'd["state"], d["pending"]["kind"], d["pending"]["calls"][0]["name"], d["turns"]')" = "pending confirm deploy_agent 2" ] || die 5 "session after turn 2: $(jq_ 'd["state"], d["pending"], d["turns"]')"
CALL2="$(jq_ 'd["pending"]["calls"][0]["id"]')"
RID_D2="e2e-asst-$RUN_ID-d2"
code="$(sse_post "/assistant/sessions/$SID/decisions" "{\"decisions\": [{\"id\": \"$CALL2\", \"type\": \"approve\"}]}" -H "X-Request-ID: $RID_D2")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 5 "POST /decisions approve (deploy) -> $code"; }
NAMES2="$(sse_ 'names')"
[ "$NAMES2" = "['tool_call', 'tool_result', 'resource', 'tool_call', 'tool_result', 'ui', 'token', 'done']" ] || die 5 "event order after approve: $NAMES2 $(cat "$WORK/sse")"
[ "$(sse_ 'nth("tool_call", 0)["id"], nth("tool_call", 0)["name"], nth("tool_result", 0)["status"], nth("tool_call", 1)["name"], nth("tool_result", 1)["status"]')" = "$CALL2 deploy_agent success navigate success" ] \
  || die 5 "calls after approve: $(sse_ '[(d["name"], d["status"], d["content"][:300]) for e, d in events if e == "tool_result"]')"
# 배포는 저장한 그 문서를 띄운다. 새 id 가 생기지 않는다 (D42)
[ "$(sse_ 'content(0)["id"]')" = "$AG_ID" ] || die 5 "deploy returned another id: $(sse_ 'content(0).get("id")') (expected $AG_ID)"
[ "$(sse_ 'content(0)["name"]')" = "$AG_NAME" ] || die 5 "deploy result: $(sse_ 'nth("tool_result", 0)["content"][:600]')"
[ "$(sse_ 'first("resource")["kind"], first("resource")["id"], first("resource")["url"]')" = "agent $AG_ID /agents/$AG_ID" ] || die 5 "resource after deploy: $(sse_ 'first("resource")')"
[ "$(sse_ 'first("ui")')" = "{'type': 'navigate', 'to': '/agents/$AG_ID'}" ] || die 5 "ui event: $(sse_ 'first("ui")') (expected navigate to /agents/$AG_ID)"
[ "$(sse_ 'first("done")["session_id"], first("done")["turn"], first("done")["tool_calls"]')" = "$SID 2 4" ] || die 5 "done: $(sse_ 'first("done")') (expected turn 2 tool_calls 4)"
DONE_TEXT="$(sse_ 'first("done")["text"]')"
case "$DONE_TEXT" in *"$AG_ID"*) ;; *) die 5 "done.text lacks the agent id: $DONE_TEXT" ;; esac
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_ID phase=Ready"
wait_ready "/agents/$AG_ID" 5
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_DEPLOYED_REV="$(jq_ 'd["deployed_revision"]')"; STATUS_WARN="$(jq_ 'd["status"]["warnings"]')"
[ "$(jq_ 'd["k8s"]["name"], d["state"], d["status"]["chat_url"]')" = "agent-$AG_NAME deployed /api/agents/$AG_ID/chat" ] \
  || die 5 "agent $AG_ID: $(jq_ 'd["k8s"], d["state"], d["status"].get("chat_url")')"
[ "$AG_DEPLOYED_REV" = "$AG_REV" ] || die 5 "deployed_revision=$AG_DEPLOYED_REV (saved revision $AG_REV)"
"${K[@]}" -n "$AG_NS" get deployment "agent-$AG_NAME" -o name >/dev/null || die 5 "Deployment $AG_NS/agent-$AG_NAME missing"
code="$(api GET "/assistant/sessions/$SID")"
[ "$(jq_ 'd["state"], d["pending"], d["turns"], d["transcript"][-1]["kind"]')" = "idle None 2 done" ] || die 5 "session after deploy: $(jq_ 'd["state"], d["pending"], d["turns"]')"
pass 5 "turn 2 $NAMES (validate ok issues=$VAL_ISSUES; create_agent proposal marks=$PROPOSAL_MARKS, approve without digest 409) -> approve $CALL_P with digest -> $NAMES_P (agent $AG_NAME id=$AG_ID rev=$AG_REV draft 로 저장) -> approve $CALL2 -> $NAMES2; agent $AG_NAME id=$AG_ID deployed Ready in $AG_NS rev=$AG_DEPLOYED_REV (status.warnings=$STATUS_WARN); ui navigate /agents/$AG_ID; done turn=2 tool_calls=4"

# ---------- 6. secret in chat → secret_in_chat + navigate; nothing stored, nothing created ----------
python3 - "$WORK/turn3.json" "$DS_NAME" "$DS_DISPLAY" "$PG_HOST" "$HOME_ID" "$SECRET" <<'PY'
import json, sys
out, ds, ds_disp, host, cluster, secret = sys.argv[1:7]
steps = [
    {"tool": "create_data_service", "args": {
        "name": ds, "display_name": ds_disp, "kind": "postgres", "host": host, "port": 5432,
        "database": "agent", "username": "agent", "password": secret, "cluster_id": cluster}},
    {"tool": "navigate", "args": {"to": "/data-services/new"}},
    {"say": "비밀번호는 채팅으로 받지 않는다. Database 등록 화면을 열었으니 거기서 마저 입력해 달라."},
]
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False), "context": {"route": "/data-services"}},
          open(out, "w"), ensure_ascii=False)
PY
RID_T3="e2e-asst-$RUN_ID-t3"
code="$(sse_post "/assistant/sessions/$SID/turns" "@$WORK/turn3.json" -H "X-Request-ID: $RID_T3")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 6 "POST /turns (secret) -> $code"; }
NAMES="$(sse_ 'names')"
[ "$NAMES" = "['tool_call', 'tool_result', 'tool_call', 'tool_result', 'ui', 'token', 'done']" ] || die 6 "event order: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ 'nth("tool_call", 0)["name"], nth("tool_call", 0)["args"]["password"], nth("tool_call", 0)["args"]["username"]')" = "create_data_service *** agent" ] \
  || die 6 "tool_call args are not masked: $(sse_ 'nth("tool_call", 0)["args"]')"
[ "$(sse_ 'nth("tool_result", 0)["status"], content(0)["code"], content(0)["fields"]')" = "error secret_in_chat ['/password']" ] \
  || die 6 "tool_result: $(sse_ 'nth("tool_result", 0)')"
[ "$(sse_ 'nth("tool_call", 1)["name"], nth("tool_result", 1)["status"], first("ui")')" = "navigate success {'type': 'navigate', 'to': '/data-services/new'}" ] \
  || die 6 "navigate: $(sse_ 'nth("tool_call", 1), nth("tool_result", 1), first("ui")')"
DONE_TEXT="$(sse_ 'first("done")["text"]')"
case "$DONE_TEXT" in "비밀번호는 채팅으로 받지 않는다."*) ;; *) die 6 "done.text: $DONE_TEXT" ;; esac
[ "$(sse_ 'first("done")["turn"], first("done")["tool_calls"]')" = "3 2" ] || die 6 "done: $(sse_ 'first("done")')"
if grep -q "$SECRET" "$WORK/sse"; then die 6 "the secret leaked into the SSE stream: $(grep -n "$SECRET" "$WORK/sse" | head -3)"; fi
code="$(api GET "/assistant/sessions/$SID")"
[ "$code" = "200" ] || die 6 "GET /assistant/sessions/$SID -> $code"
if ! TRANSCRIPT_NOTE="$(python3 - "$WORK/body" "$SECRET" <<'PY'
import json, sys
body = json.load(open(sys.argv[1])); secret = sys.argv[2]
items = body["transcript"]
# 사용자가 친 메시지(kind user)는 그대로 남는다. 그 밖(tool_call 인자·결과·assistant·resource·interrupt)에는 없어야 한다
leaks = [it["kind"] for it in items if it.get("kind") != "user" and secret in json.dumps(it, ensure_ascii=False)]
if leaks:
    print("secret found in transcript items: " + str(leaks)); sys.exit(1)
calls = [it for it in items if it.get("kind") == "tool_call" and it["call"]["name"] == "create_data_service"]
if not calls or calls[-1]["call"]["args"].get("password") != "***":
    print("create_data_service args are not masked in the transcript: " + json.dumps(calls[-1]["call"]["args"] if calls else None)); sys.exit(1)
if not calls[-1]["result"] or calls[-1]["result"]["status"] != "error":
    print("create_data_service result in the transcript: " + json.dumps(calls[-1]["result"])); sys.exit(1)
user_keeps = any(it.get("kind") == "user" and secret in it.get("text", "") for it in items)
print(f"transcript items={len(items)}, args masked ***, user message keeps the typed secret={user_keeps}")
PY
)"; then die 6 "$TRANSCRIPT_NOTE"; fi
[ "$(jq_ 'd["state"], d["pending"], d["turns"]')" = "idle None 3" ] || die 6 "session after secret turn: $(jq_ 'd["state"], d["pending"], d["turns"]')"
code="$(api GET /data-services)"
[ "$code" = "200" ] || die 6 "GET /data-services -> $code"
DS_LEFT="$(find_id "$DS_NAME")"
[ -z "$DS_LEFT" ] || { DS_ID="$DS_LEFT"; die 6 "data service $DS_NAME was created despite the secret (id=$DS_LEFT)"; }
code="$(api GET "/audit?via=assistant/$SID&action=create_data_service&limit=10")"
[ "$code" = "200" ] && [ "$(jq_ 'len(d)')" = "0" ] || die 6 "audit has a create_data_service row for this session (the API must not have been called): $(cat "$WORK/body")"
pass 6 "secret turn $NAMES: tool_call password=***, tool_result error secret_in_chat fields ['/password'], ui navigate /data-services/new, done.text='$DONE_TEXT'; no '$SECRET' in the SSE; $TRANSCRIPT_NOTE; no Database created, no audit row"

# ---------- 7. audit via=assistant/<sid>; forged X-Daegam-Via is ignored ----------
AUDIT_SUMMARY=""
for ((i = 0; i < 6; i++)); do   # 감사 기록은 응답 뒤에 쓰인다. 9행이 다 보일 때까지 잠깐 기다린다
  code="$(api GET "/audit?via=assistant/$SID&limit=200")"
  [ "$code" = "200" ] || die 7 "GET /audit?via=assistant/$SID -> $code"
  [ "$(jq_ 'len(d)')" -ge 9 ] && break
  sleep 2
done
if ! AUDIT_SUMMARY="$(python3 - "$WORK/body" "assistant/$SID" "$EXPECT_SUB" "$RID_T1" "$RID_D1" "$RID_T2" "$RID_DP" "$RID_D2" "$RID_T3" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1])); via = sys.argv[2]; expect_sub = sys.argv[3]
prefixes = set(sys.argv[4:])
# action 은 라우터 핸들러 함수 이름이다 (audit/middleware.py). D42 로 핸들러 이름이 바뀌었다. validate_agent_graph 는
# 스텁의 검증, 제안의 검사, 승인 때의 다시 검사로 셋이다 (P48 6.2절)
expected = sorted(["verify_spec", "upload_spec", "create_server", "deploy_server", "validate_agent_graph",
                   "validate_agent_graph", "validate_agent_graph", "create_agent", "deploy_agent"])
actions = sorted(r["action"] for r in rows)
bad = []
if actions != expected:
    bad.append(f"actions {actions} != {expected}")
if any(r.get("via") != via for r in rows):
    bad.append("via mismatch: " + str(sorted({str(r.get("via")) for r in rows})))
if any(r["result"] != "ok" for r in rows):
    bad.append("non-ok rows: " + str([(r["action"], r["status"], r.get("error_code")) for r in rows if r["result"] != "ok"]))
if any(r["request_id"].rsplit(".", 1)[0] not in prefixes or "." not in r["request_id"] for r in rows):
    bad.append("request_id is not '<turn X-Request-ID>.<n>': " + str([r["request_id"] for r in rows]))
if any(r.get("sub") != expect_sub for r in rows):
    bad.append(f"sub (expected {expect_sub!r}): " + str(sorted({str(r.get("sub")) for r in rows})))
if bad:
    print("; ".join(bad)); sys.exit(1)
print(" ".join(f'{r["action"]}({r["method"]} {r["status"]} {r["request_id"]})' for r in sorted(rows, key=lambda r: r["ts"])))
PY
)"; then die 7 "audit rows for via=assistant/$SID: $AUDIT_SUMMARY"; fi
N_VIA="$(jq_ 'len(d)')"
# 접두 일치 via=assistant/ 에도 이 세션의 행이 들어 있다
code="$(api GET "/audit?via=assistant/&limit=1000")"
[ "$code" = "200" ] && [ "$(jq_ "len([r for r in d if r.get('via')=='assistant/$SID'])")" = "$N_VIA" ] || die 7 "prefix filter via=assistant/ lost rows: $code"
# 위조: 헤더로 via 를 흉내 낸 외부 POST. 감사 행은 남되 via 가 null 이다 (state 만 읽는다)
RID_F="e2e-asst-$RUN_ID-forged"
code="$(api POST /backend-apis/verify "$(python3 -c 'import json,sys; print(json.dumps({"text": open(sys.argv[1], encoding="utf-8").read(), "filename": sys.argv[2]}))' "$FIXTURE" "$FILENAME")" \
  -H "X-Daegam-Via: assistant/forged" -H "X-Request-ID: $RID_F")"
[ "$code" = "200" ] || die 7 "POST /backend-apis/verify with X-Daegam-Via -> $code"
FORGED_VIA=""
for ((i = 0; i < 6; i++)); do
  code="$(api GET "/audit?action=verify_spec&limit=100")"
  [ "$code" = "200" ] || die 7 "GET /audit?action=verify_spec -> $code"
  FORGED_VIA="$(jq_ "[str(r.get('via')) for r in d if r['request_id']=='$RID_F']")"
  [ "$FORGED_VIA" != "[]" ] && break
  sleep 2
done
[ "$FORGED_VIA" = "['None']" ] || die 7 "forged request $RID_F audit via = $FORGED_VIA (expected [None]: the header must be ignored)"
code="$(api GET "/audit?via=assistant/forged&limit=10")"
[ "$code" = "200" ] && [ "$(jq_ 'len(d)')" = "0" ] || die 7 "via=assistant/forged matched rows: $(cat "$WORK/body")"
pass 7 "audit via=assistant/$SID has $N_VIA write rows (via·request_id·sub ok): $AUDIT_SUMMARY; forged X-Daegam-Via -> via null, via=assistant/forged matches nothing"

# ---------- 8. delete session → audit rows stay ----------
code="$(api DELETE "/assistant/sessions/$SID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["deleted"], d["id"]')" = "True $SID" ] || die 8 "DELETE /assistant/sessions/$SID -> $code $(cat "$WORK/body")"
SESSION_DELETED=1
code="$(api GET "/assistant/sessions/$SID")"
[ "$code" = "404" ] && [ "$(jq_ "d['detail']['code']")" = "session_not_found" ] || die 8 "GET deleted session -> $code (expected 404 session_not_found)"
code="$(api GET "/audit?via=assistant/$SID&limit=200")"
[ "$code" = "200" ] && [ "$(jq_ 'len(d)')" = "$N_VIA" ] || die 8 "audit rows after session delete: $(jq_ 'len(d)') (expected $N_VIA)"
[ "$(jq_ "sorted({r.get('via') for r in d})")" = "['assistant/$SID']" ] || die 8 "via after session delete: $(jq_ "sorted({r.get('via') for r in d})")"
pass 8 "session $SID deleted (404 session_not_found afterwards); audit still has $N_VIA rows with via=assistant/$SID"

# ---------- 9. (oidc) viewer 의 배포 도구는 403 이고 비서가 그렇게 말한다 ----------
if [ "$AUTH_MODE" != "oidc" ]; then
  cond_skip 9 "viewer 403 needs a second subject; header mode has one (make e2e-assistant-oidc)"
else
  code="$(req "$API" "$VIEW_TOKEN" GET /assistant/config)"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["enabled"]')" = "True" ]; } || die 9 "viewer GET /assistant/config -> $code $(cat "$WORK/body")"
  code="$(req "$API" "$VIEW_TOKEN" POST /assistant/sessions '{"title": "viewer e2e"}')"
  [ "$code" = "201" ] || die 9 "viewer POST /assistant/sessions -> $code $(cat "$WORK/body")"
  SID_VIEW="$(jq_ 'd["id"]')"
  # 배포 도구를 시험하려면 띄울 draft 가 있어야 한다. 한 문서는 배포를 하나만 가지므로 STEP 4 의 서버를
  # 관리자가 복제해 둔다 (D42). viewer 는 이 복제본을 배포하려다 403 을 받는다.
  # 이름 충돌은 STEP 0 의 preclean 이 이미 걷었으므로 여기서는 201 만 받는다
  code="$(api POST "/mcp-servers/$MS_ID/clone" "{\"name\": \"$MS2_NAME\", \"display_name\": \"$MS2_DISPLAY\"}")"
  [ "$code" = "201" ] || die 9 "POST /mcp-servers/$MS_ID/clone -> $code $(cat "$WORK/body")"
  MS2_ID="$(jq_ 'd["id"]')"
  { [ -n "$MS2_ID" ] && [ "$(jq_ 'd["state"]')" = "draft" ]; } \
    || die 9 "clone $MS2_NAME: id=$MS2_ID state=$(jq_ 'd["state"]') (expected draft)"
  python3 - "$WORK/turn9.json" "$MS2_ID" "$HOME_ID" <<'PY'
import json, sys
out, server_id, cluster = sys.argv[1:4]
steps = [
    {"tool": "deploy_mcp_server", "args": {"server_id": server_id, "cluster_id": cluster}},
    {"say": "배포 결과: $0.code"},
]
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False)}, open(out, "w"), ensure_ascii=False)
PY
  code="$(sse_req "$API" "$VIEW_TOKEN" "/assistant/sessions/$SID_VIEW/turns" "@$WORK/turn9.json")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 9 "viewer POST /turns -> $code"; }
  # 배포 도구라 실행 전에 승인 대기로 닫힌다. 권한은 그 뒤에 본다
  [ "$(sse_ 'names[-1], first("interrupt")["action_requests"][0]["name"]')" = "interrupt deploy_mcp_server" ] \
    || die 9 "viewer turn events: $(sse_ 'names')"
  code="$(req "$API" "$VIEW_TOKEN" GET "/assistant/sessions/$SID_VIEW")"
  [ "$code" = "200" ] || die 9 "viewer GET session -> $code"
  CALL_V="$(jq_ 'd["pending"]["calls"][0]["id"]')"
  code="$(sse_req "$API" "$VIEW_TOKEN" "/assistant/sessions/$SID_VIEW/decisions" "{\"decisions\": [{\"id\": \"$CALL_V\", \"type\": \"approve\"}]}")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 9 "viewer POST /decisions -> $code"; }
  [ "$(sse_ 'names')" = "['tool_call', 'tool_result', 'token', 'done']" ] || die 9 "viewer decision events: $(sse_ 'names')"
  [ "$(sse_ 'nth("tool_result", 0)["status"]')" = "error" ] || die 9 "viewer tool_result: $(sse_ 'nth("tool_result", 0)')"
  FORB="$(sse_ 'content(0)["status"], content(0)["code"], content(0)["scope"], content(0)["tenant"]')"
  [ "$FORB" = "403 forbidden mcp:deploy $TENANT" ] || die 9 "viewer tool_result content: $(sse_ 'nth("tool_result", 0)["content"][:400]')"
  DONE_V="$(sse_ 'first("done")["text"]')"
  case "$DONE_V" in *forbidden*) ;; *) die 9 "the assistant did not carry the error code into its answer: $DONE_V" ;; esac
  # 403 이므로 복제본은 배포되지 않았다 (정의는 관리자가 만든 것이라 그대로 있다)
  code="$(api GET "/mcp-servers/$MS2_ID")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "draft" ]; } \
    || die 9 "clone $MS2_NAME state=$(jq_ 'd["state"]') after the viewer's 403 (expected draft)"
  pass 9 "viewer(view-e2e, $VIEW_GROUP) deploy_mcp_server -> interrupt -> approve -> tool_result error ($FORB); done.text='$DONE_V'; 복제본 $MS2_NAME 은 draft 그대로"
fi

# ---------- 10. (oidc) 세션은 만든 사람만 본다 ----------
if [ "$AUTH_MODE" != "oidc" ]; then
  cond_skip 10 "session ownership needs a second subject; header mode has one (make e2e-assistant-oidc)"
else
  code="$(req "$API" "$DEV_TOKEN" POST /assistant/sessions '{"title": "dev e2e"}')"
  [ "$code" = "201" ] || die 10 "developer POST /assistant/sessions -> $code $(cat "$WORK/body")"
  SID_DEV="$(jq_ 'd["id"]')"
  code="$(req "$API" "$VIEW_TOKEN" GET "/assistant/sessions/$SID_DEV")"
  { [ "$code" = "404" ] && [ "$(jq_ "d['detail']['code']")" = "session_not_found" ]; } \
    || die 10 "viewer GET the developer's session -> $code $(cat "$WORK/body")"
  code="$(req "$API" "$DEV_TOKEN" GET "/assistant/sessions/$SID_VIEW")"
  [ "$code" = "404" ] || die 10 "developer GET the viewer's session -> $code"
  # 소유는 역할이 아니라 sub 다. 플랫폼 관리자에게도 남의 세션은 없다
  code="$(api GET "/assistant/sessions/$SID_DEV")"
  [ "$code" = "404" ] || die 10 "platform admin GET the developer's session -> $code (ownership is by sub, not by role)"
  code="$(req "$API" "$VIEW_TOKEN" GET /assistant/sessions)"
  [ "$code" = "200" ] || die 10 "viewer GET /assistant/sessions -> $code"
  VLIST="$(jq_ '[i["id"] for i in d["items"]]')"
  case "$VLIST" in *"$SID_DEV"*) die 10 "the viewer's session list contains the developer's session: $VLIST" ;; esac
  case "$VLIST" in *"$SID_VIEW"*) ;; *) die 10 "the viewer's session list lacks its own session $SID_VIEW: $VLIST" ;; esac
  pass 10 "sessions are per-sub: $SID_DEV (dev-e2e) is 404 for view-e2e and for boot-admin; the viewer's list is $VLIST"
fi

# ---------- 11. (oidc) 교차 파드: 파드 B 가 파드 A 의 대기를 잇고, 그 동안 리스가 파드 A 를 막는다 ----------
if [ "$AUTH_MODE" != "oidc" ]; then
  cond_skip 11 "cross-pod decision resume needs oidc mode (make e2e-assistant-oidc)"
else
  # Service port-forward 는 파드 하나에 붙으므로 파드 이름으로 둘을 따로 연다
  # 롤아웃 직후에는 죽는 중인 파드도 잠시 Running 이다. Ready 이고 지워지는 중이 아닌 것만 고른다
  # 줄마다 "이름 [삭제표시] Ready". 삭제표시가 없으면 그 자리가 비어 필드가 둘이다(jsonpath 는 없는
  # 필드를 빈 문자열로 찍지 필터로는 못 거른다)
  PODS="$("${K[@]}" -n "$PLATFORM_NS" get pods -l app=daegam-platform \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.deletionTimestamp}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' 2>/dev/null \
    | awk 'NF == 2 && $2 == "True" { print $1 }' | tr '\n' ' ')"
  POD_A="$(echo "$PODS" | awk '{print $1}')"; POD_B="$(echo "$PODS" | awk '{print $2}')"
  [ -n "$POD_B" ] || die 11 "platform has fewer than two Ready pods ($PODS). this step needs replicas 2"
  start_pf "$PLATFORM_NS" "pod/$POD_A" "${POD_A_PORT}:9000"; PF_POD_A=$PF_PID
  start_pf "$PLATFORM_NS" "pod/$POD_B" "${POD_B_PORT}:9000"; PF_POD_B=$PF_PID
  wait_port "$POD_A_PORT" || die 11 "port-forward to pod/$POD_A did not open :$POD_A_PORT"
  wait_port "$POD_B_PORT" || die 11 "port-forward to pod/$POD_B did not open :$POD_B_PORT"
  API_A="http://127.0.0.1:${POD_A_PORT}/api"; API_B="http://127.0.0.1:${POD_B_PORT}/api"
  code="$(req "$API_A" "$DEV_TOKEN" POST /assistant/sessions '{"title": "cross-pod e2e"}')"
  [ "$code" = "201" ] || die 11 "POST /assistant/sessions on pod A -> $code $(cat "$WORK/body")"
  SID_X="$(jq_ 'd["id"]')"
  code="$(req "$API_A" "$DEV_TOKEN" POST /assistant/sessions '{"title": "cross-pod e2e 2"}')"
  [ "$code" = "201" ] || die 11 "second session on pod A -> $code"
  SID_X2="$(jq_ 'd["id"]')"
  # 승인 뒤에 도는 구간을 몇 초로 늘린다(verify_spec 셋). 그 사이에 리스가 무엇을 막는지 본다.
  # 띄우는 것은 STEP 9 가 복제해 둔 draft 다 (viewer 의 403 으로 아직 배포되지 않았다)
  python3 - "$FIXTURE" "$WORK/turn11.json" "$MS2_ID" "$HOME_ID" "$FILENAME" <<'PY'
import json, sys
spec = open(sys.argv[1], encoding="utf-8").read()
out, server_id, cluster, filename = sys.argv[2:6]
verify = {"tool": "verify_spec", "args": {"text": spec, "filename": filename}}
steps = [
    {"tool": "deploy_mcp_server", "args": {"server_id": server_id, "cluster_id": cluster}},
    *([verify] * 6),
    {"say": "서버 $0.id 를 배포했다."},
]
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False)}, open(out, "w"), ensure_ascii=False)
PY
  code="$(sse_req "$API_A" "$DEV_TOKEN" "/assistant/sessions/$SID_X/turns" "@$WORK/turn11.json")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 11 "turn on pod A -> $code"; }
  [ "$(sse_ 'names[-1], first("interrupt")["action_requests"][0]["name"]')" = "interrupt deploy_mcp_server" ] \
    || die 11 "turn on pod A did not stop at the confirm: $(sse_ 'names')"
  # 대기는 파드 B 에서도 보인다. 상태가 파드가 아니라 Mongo 에 있기 때문이다
  code="$(req "$API_B" "$DEV_TOKEN" GET "/assistant/sessions/$SID_X")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "pending" ]; } || die 11 "pod B sees state $(jq_ 'd["state"]') (expected pending)"
  CALL_X="$(jq_ 'd["pending"]["calls"][0]["id"]')"
  # 결정을 파드 B 로 보낸다. 경합이 없는 검사라 앞에서 끝낸다(리스는 STEP 12 가 따로 본다)
  code="$(sse_req "$API_B" "$DEV_TOKEN" "/assistant/sessions/$SID_X/decisions" \
    "{\"decisions\": [{\"id\": \"$CALL_X\", \"type\": \"approve\"}]}")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 11 "decision on pod B -> $code"; }
  NAMES_B="$(sse_ 'names')"
  [ "$(sse_ 'names[-1], first("done")["session_id"]')" = "done $SID_X" ] || die 11 "events on pod B: $NAMES_B"
  # 배포는 STEP 9 의 복제본을 그대로 띄운다. 새 id 가 생기지 않는다 (D42)
  [ "$(sse_ 'content(0)["id"]')" = "$MS2_ID" ] \
    || die 11 "the resumed turn deployed another id: $(sse_ 'content(0).get("id")') (expected $MS2_ID)"
  # 끝난 상태도 두 파드가 같이 본다
  code="$(req "$API_A" "$DEV_TOKEN" GET "/assistant/sessions/$SID_X")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "idle" ]; } \
    || die 11 "pod A sees state $(jq_ 'd["state"]') after the turn pod B resumed (expected idle)"
  pass 11 "pod A($POD_A) stopped at the confirm and pod B($POD_B) resumed it ($NAMES_B, mcp server $MS2_ID deployed); pod A sees idle afterwards"
fi

# ---------- 12. (oidc) 교차 파드 리스: 파드 B 에서 도는 턴이 파드 A 를 막는다 ----------
if [ "$AUTH_MODE" != "oidc" ]; then
  cond_skip 12 "cross-pod lease needs oidc mode (make e2e-assistant-oidc)"
else
  # 도는 턴을 잡아야 하는 검사다. 창을 놓치면 더 긴 턴으로 다시 한다(놓친 것은 실패가 아니다)
  LEASE_SAME=""; LEASE_OTHER=""; OTHER_USER=""; SEEN=""; CAUGHT=0
  # 막히지 않은 확인 뒤 파드 B 의 턴이 끝나 가는 중이었는지 볼 때 기다리는 시간이다
  LEASE_SETTLE_SECONDS=2
  for attempt in 1 2 3; do
    python3 - "$FIXTURE" "$WORK/turn12.json" "$FILENAME" "$((20 * attempt))" <<'PY'
import json, sys
spec = open(sys.argv[1], encoding="utf-8").read()
out, filename, n = sys.argv[2:5]
verify = {"tool": "verify_spec", "args": {"text": spec, "filename": filename}}
steps = [*([verify] * int(n)), {"say": "ok"}]
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False)}, open(out, "w"), ensure_ascii=False)
PY
    SSE_OUT="$WORK/sse.bg" sse_req "$API_B" "$DEV_TOKEN" "/assistant/sessions/$SID_X/turns" \
      "@$WORK/turn12.json" >"$WORK/bg.code" 2>/dev/null 8>&- 9>&- &
    BG=$!
    RUNNING=0; LAST_STATE=""
    for ((i = 0; i < 400; i++)); do
      code="$(req "$API_A" "$DEV_TOKEN" GET "/assistant/sessions/$SID_X")" || true
      if [ "$code" = "200" ]; then
        LAST_STATE="$(jq_ 'd["state"]' || true)"
        # ${SEEN} 로 감싸는 것은 배열 첨자가 아니라 글자 그대로의 대괄호라는 표시다 (shellcheck SC1087)
        case "$SEEN" in *"[$LAST_STATE]"*) ;; *) SEEN="${SEEN}[$LAST_STATE]" ;; esac
        if [ "$LAST_STATE" = "running" ]; then RUNNING=1; break; fi
        # 몇 번을 봐도 idle 이면 이 시도는 이미 지나간 것이다
        if [ "$LAST_STATE" = "idle" ] && [ "$i" -gt 5 ]; then break; fi
      fi
      sleep 0.05
    done
    if [ "$RUNNING" = 1 ]; then
      # 막히지 않으면 응답이 SSE 라 JSON 이 아니다. 코드를 먼저 보고 409 일 때만 detail 을 푼다.
      # 200 이면 그 사이에 파드 B 의 턴이 끝난 것이라 실패가 아니라 놓친 것이다. 더 긴 턴으로 다시 한다
      code="$(req "$API_A" "$DEV_TOKEN" POST "/assistant/sessions/$SID_X/turns" '{"message": "hi"}')"
      case "$code" in
        409)
          LEASE_SAME="$code $(jq_ "d['detail']['code']")"
          code="$(req "$API_A" "$DEV_TOKEN" POST "/assistant/sessions/$SID_X2/turns" '{"message": "hi"}')"
          if [ "$code" != "409" ]; then
            # 두 확인 사이에 파드 B 의 턴이 끝났으면 놓친 것이다. 턴이 아직 돌고 있는데 막히지 않았을 때만 실패다
            sleep "$LEASE_SETTLE_SECONDS"
            if kill -0 "$BG" 2>/dev/null; then
              die 12 "another session of the same user -> $code (expected 409 turn_in_progress, user lease)"
            fi
            echo "   (attempt $attempt: pod B's turn ended between the two checks. retrying with a longer turn)"
          else
            LEASE_OTHER="$code $(jq_ "d['detail']['code']")"
            # 리스는 사용자별이지 전역 잠금이 아니다. 다른 사용자는 같은 순간에도 자기 턴을 돈다
            OTHER_USER="$(sse_req "$API_A" "$VIEW_TOKEN" "/assistant/sessions/$SID_VIEW/turns" \
              "$(python3 -c 'import json; print(json.dumps({"message": json.dumps({"steps": [{"say": "ok"}]}, ensure_ascii=False)}))')")"
            CAUGHT=$attempt
          fi ;;
        200) echo "   (attempt $attempt: pod B's turn ended before the check. retrying with a longer turn)" ;;
        *) die 12 "same session on pod A while pod B runs -> $code (expected 409 turn_in_progress, session lease)" ;;
      esac
    fi
    wait "$BG" || true
    BG_CODE="$(cat "$WORK/bg.code" 2>/dev/null || true)"
    [ "$BG_CODE" = "200" ] || { cp "$WORK/sse.bg" "$WORK/body" 2>/dev/null || true; die 12 "background turn on pod B (attempt $attempt) -> $BG_CODE"; }
    if [ "$CAUGHT" != 0 ]; then break; fi
  done
  [ "$CAUGHT" != 0 ] || die 12 "pod A never caught a turn running on pod B in three attempts. states seen: ${SEEN:-none}"
  [ "$LEASE_SAME" = "409 turn_in_progress" ] || die 12 "same session detail: $LEASE_SAME"
  [ "$LEASE_OTHER" = "409 turn_in_progress" ] || die 12 "other session detail: $LEASE_OTHER"
  [ "$OTHER_USER" = "200" ] || die 12 "a different user's turn was blocked too -> $OTHER_USER (the lease is per user, not a global lock)"
  code="$(req "$API_A" "$DEV_TOKEN" GET "/assistant/sessions/$SID_X")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "idle" ]; } || die 12 "state after the turn: $(jq_ 'd["state"]')"
  pass 12 "while pod B ran a turn (caught on attempt $CAUGHT, states seen $SEEN), pod A got '$LEASE_SAME' (session lease) and '$LEASE_OTHER' (user lease); another user got $OTHER_USER; leases released, state idle"
fi

# ---------- 13. Agent proposals: a change waits as a card; refused, invalid and stale ones save nothing ----------
# 바꾸기 제안 (P48 6.2·6.5·6.7절). 세션을 따로 열어 STEP 7 의 감사 행 수를 건드리지 않는다. 주체는 STEP 0~8 과 같다
code="$(api POST /assistant/sessions '{"title": "e2e proposals"}')"
[ "$code" = "201" ] || die 13 "POST /assistant/sessions (proposals) -> $code"
SID_P="$(jq_ 'd["id"]')"
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "200" ] || die 13 "GET /agents/$AG_ID -> $code"
cp "$WORK/body" "$WORK/agent0.json"
REV0="$(jq_ 'd["revision"]')"
# 사람이 배치만 저장한다. 리비전은 생기지 않고, 뒤의 승인된 바꾸기가 남는 노드의 이 자리를 지켜야 한다 (6.7절)
python3 - "$WORK/agent0.json" "$WORK/layout.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1]))
layout = {n["id"]: {"x": 100 + 320 * i, "y": 200 + 40 * i} for i, n in enumerate(a["graph"]["nodes"])}
json.dump({"name": a["name"], "display_name": a.get("display_name"), "graph": a["graph"], "service": a["service"], "layout": layout},
          open(sys.argv[2], "w"), ensure_ascii=False)
PY
RID_LAYOUT="e2e-asst-$RUN_ID-layout"
code="$(api PUT "/agents/$AG_ID" "@$WORK/layout.json" -H "X-Request-ID: $RID_LAYOUT")"
[ "$code" = "200" ] && [ "$(jq_ 'd["revision"], d["revision_created"]')" = "$REV0 False" ] || die 13 "layout-only PUT -> $code $(jq_ 'd.get("revision"), d.get("revision_created")') (expected $REV0 False)"
ADDED_LINE="환불 요청은 상담원에게 넘긴다."
python3 - "$WORK/agent0.json" "$WORK/turnp1.json" "$AG_ID" "$AG_NAME" "$ADDED_LINE" <<'PY'
import copy, json, sys
src, out, ag_id, ag, line = sys.argv[1:6]
nodes = {n["id"]: n for n in json.load(open(src))["graph"]["nodes"]}
# set_nodes 는 노드를 id 로 통째로 바꾸므로 읽은 data 전체를 고쳐 보낸다 (get_agent_definition 으로 읽는 것과 같다)
marked = copy.deepcopy(nodes["tools-petstore"])
marked["data"]["read_only"] = ["addPet"]
agent = copy.deepcopy(nodes["agent-main"])
agent["data"]["system_prompt"] = agent["data"]["system_prompt"] + "\n" + line
steps = [
    # read_only 는 사람이 캔버스에서 다는 표시다 (6.3절)
    {"tool": "update_agent", "args": {"agent_id": ag_id, "set_nodes": [marked]}},
    # 모델이 없는 에이전트는 검증 오류다 (model_count)
    {"tool": "update_agent", "args": {"agent_id": ag_id, "remove_nodes": ["model-main"]}},
    {"tool": "update_agent", "args": {"agent_id": ag_id, "set_nodes": [agent]}},
    {"say": "메인 에이전트의 프롬프트에 환불 안내 한 줄을 더했다."},
]
ctx = {"route": f"/agents/{ag_id}", "resource": {"kind": "agent", "id": ag_id, "name": ag}, "unsaved_edits": False}
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False), "context": ctx}, open(out, "w"), ensure_ascii=False)
PY
RID_P1="e2e-asst-$RUN_ID-p1"
code="$(sse_post "/assistant/sessions/$SID_P/turns" "@$WORK/turnp1.json" -H "X-Request-ID: $RID_P1")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 13 "POST /turns (proposals) -> $code"; }
NAMES_P1="$(sse_ 'names')"
[ "$NAMES_P1" = "['tool_call', 'tool_result', 'tool_call', 'tool_result', 'interrupt']" ] || die 13 "proposal turn event order: $NAMES_P1 $(cat "$WORK/sse")"
[ "$(sse_ 'nth("tool_result", 0)["status"], content(0)["code"], nth("tool_result", 1)["status"], content(1)["code"], "model_count" in [i["code"] for i in content(1)["issues"]]')" = "error person_marks_only error proposal_invalid True" ] \
  || die 13 "refused proposals: $(sse_ '[d["content"][:400] for d in every("tool_result")]')"
CARD_P1="$(sse_ '(lambda r, p: (r["name"], p["kind"], p["base_revision"], p["deployed_revision"], [n["id"] for n in p["changes"]["nodes"]["changed"]], "system_prompt" in p["changes"]["nodes"]["changed"][0]["fields"], [(d["node_id"], d["field"]) for d in p["changes"]["texts"]], "_proposal" in r["args"]))(first("interrupt")["action_requests"][0], first("interrupt")["action_requests"][0]["proposal"])')"
[ "$CARD_P1" = "update_agent update $REV0 $AG_DEPLOYED_REV ['agent-main'] True [('agent-main', 'system_prompt')] True" ] || die 13 "proposal card: $CARD_P1 $(sse_ 'first("interrupt")')"
[ "$(sse_ '("+ " + "'"$ADDED_LINE"'") in ["{} {}".format(l["op"], l["text"]) for l in first("interrupt")["action_requests"][0]["proposal"]["changes"]["texts"][0]["lines"]]')" = "True" ] \
  || die 13 "text diff: $(sse_ 'first("interrupt")["action_requests"][0]["proposal"]["changes"]["texts"]')"
MARKS_P1="$(sse_ '[m["code"] for m in first("interrupt")["action_requests"][0]["proposal"]["attention"]]')"
case "$MARKS_P1" in *system_prompt_changed*) ;; *) die 13 "attention marks: $MARKS_P1 (expected system_prompt_changed)" ;; esac
PROPOSAL_P1="$(sse_ 'first("interrupt")["action_requests"][0]["proposal"]["proposal_id"]')"
# 거절된 둘과 기다리는 하나 모두 아무것도 저장하지 않았다
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["revision"]')" = "$REV0" ] || die 13 "the agent moved before any decision: revision $(jq_ 'd.get("revision")') (expected $REV0)"
# 캔버스가 읽는 것: 기준 위에 짜 맞춘 정의와 최신 리비전
code="$(api GET "/assistant/sessions/$SID_P/proposal")"
[ "$code" = "200" ] || die 13 "GET /proposal -> $code $(cat "$WORK/body")"
[ "$(jq_ 'd["proposal_id"], d["kind"], d["base_revision"], d["latest_revision"], [n["id"] for n in d["graph"]["nodes"]] == [n["id"] for n in json.load(open("'"$WORK/agent0.json"'"))["graph"]["nodes"]], next(n for n in d["graph"]["nodes"] if n["id"] == "agent-main")["data"]["system_prompt"].endswith("'"$ADDED_LINE"'")')" = "$PROPOSAL_P1 update $REV0 $REV0 True True" ] \
  || die 13 "GET /proposal: $(jq_ 'd["proposal_id"], d["base_revision"], d["latest_revision"]')"
code="$(api GET "/assistant/sessions/$SID_P")"
CALL_P1="$(jq_ 'd["pending"]["calls"][0]["id"]')"; DIGEST_P1="$(jq_ 'd["pending"]["calls"][0]["args"]["_proposal"]["digest"]')"
[ -n "$DIGEST_P1" ] || die 13 "the waiting proposal has no digest: $(jq_ 'd["pending"]')"
# 다른 지문은 보지 않은 내용의 결정이다
code="$(api POST "/assistant/sessions/$SID_P/decisions" "{\"decisions\": [{\"id\": \"$CALL_P1\", \"type\": \"approve\", \"proposal_digest\": \"$DIGEST_P1-stale\"}]}")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code']")" = "proposal_changed" ] || die 13 "approve with another digest -> $code $(cat "$WORK/body") (expected 409 proposal_changed)"
RID_PA="e2e-asst-$RUN_ID-pa"
code="$(sse_post "/assistant/sessions/$SID_P/decisions" "{\"decisions\": [{\"id\": \"$CALL_P1\", \"type\": \"approve\", \"proposal_digest\": \"$DIGEST_P1\"}]}" -H "X-Request-ID: $RID_PA")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 13 "POST /decisions approve (change) -> $code"; }
[ "$(sse_ 'nth("tool_call", 0)["id"], nth("tool_call", 0)["name"], nth("tool_result", 0)["status"], content(0)["revision"], names[-1]')" = "$CALL_P1 update_agent success $((REV0 + 1)) done" ] \
  || die 13 "approved change: $(sse_ 'names') $(sse_ '[d["content"][:400] for d in every("tool_result")]')"
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "200" ] || die 13 "GET /agents/$AG_ID after the approval -> $code"
# 다음 리비전이고, 바꾼 프롬프트이며, 사람이 저장한 배치를 남는 노드 모두가 지킨다
[ "$(jq_ 'd["revision"], next(n for n in d["graph"]["nodes"] if n["id"] == "agent-main")["data"]["system_prompt"].endswith("'"$ADDED_LINE"'"), d["layout"] == json.load(open("'"$WORK/layout.json"'"))["layout"]')" = "$((REV0 + 1)) True True" ] \
  || die 13 "saved change: revision $(jq_ 'd["revision"]') layout $(jq_ 'd["layout"]')"
cp "$WORK/body" "$WORK/agent1.json"
# 승인된 저장은 그 세션의 via 이고, 사람의 배치 저장은 via 가 없다
AUDIT_P=""
for ((i = 0; i < 6; i++)); do
  code="$(api GET "/audit?action=put_agent&limit=200")"
  [ "$code" = "200" ] || die 13 "GET /audit?action=put_agent -> $code"
  AUDIT_P="$(jq_ "sorted((r['request_id'].split('.')[0] if r['request_id'].startswith('$RID_PA') else r['request_id'], str(r.get('via')), r['result']) for r in d if r['request_id'] == '$RID_LAYOUT' or r['request_id'].startswith('$RID_PA.'))")"
  [ "$AUDIT_P" = "[('$RID_LAYOUT', 'None', 'ok'), ('$RID_PA', 'assistant/$SID_P', 'ok')]" ] && break
  sleep 2
done
[ "$AUDIT_P" = "[('$RID_LAYOUT', 'None', 'ok'), ('$RID_PA', 'assistant/$SID_P', 'ok')]" ] || die 13 "put_agent audit rows: $AUDIT_P"
# 기준이 지난 제안: 다음 제안이 기다리는 동안 사람이 기준 리비전으로 저장한다
python3 - "$WORK/agent1.json" "$WORK/turnp2.json" "$WORK/person.json" "$AG_ID" <<'PY'
import copy, json, sys
src, turn, person, ag_id = sys.argv[1:5]
a = json.load(open(src))
nodes = {n["id"]: n for n in a["graph"]["nodes"]}
agent = copy.deepcopy(nodes["agent-main"])
agent["data"]["description"] = "펫스토어 조회와 환불 안내를 맡는 메인 에이전트다."
steps = [{"tool": "update_agent", "args": {"agent_id": ag_id, "set_nodes": [agent]}}, {"say": "설명을 고쳤다."}]
json.dump({"message": json.dumps({"steps": steps}, ensure_ascii=False)}, open(turn, "w"), ensure_ascii=False)
graph = copy.deepcopy(a["graph"])
for n in graph["nodes"]:
    if n["id"] == "entry":
        n["data"]["greeting"] = "안녕하세요. 펫스토어와 환불 안내를 도와드립니다."
json.dump({"name": a["name"], "display_name": a.get("display_name"), "graph": graph, "service": a["service"], "expect_revision": a["revision"]},
          open(person, "w"), ensure_ascii=False)
PY
code="$(sse_post "/assistant/sessions/$SID_P/turns" "@$WORK/turnp2.json")"
[ "$code" = "200" ] && [ "$(sse_ 'names[-1], first("interrupt")["action_requests"][0]["proposal"]["base_revision"]')" = "interrupt $((REV0 + 1))" ] \
  || { cp "$WORK/sse" "$WORK/body"; die 13 "second proposal -> $code $(sse_ 'names')"; }
code="$(api PUT "/agents/$AG_ID" "@$WORK/person.json")"
[ "$code" = "200" ] && [ "$(jq_ 'd["revision"]')" = "$((REV0 + 2))" ] || die 13 "the person's PUT with expect_revision $((REV0 + 1)) -> $code $(cat "$WORK/body")"
# 지난 기준으로 한 사람의 저장은 지금 번호를 실은 409 다 (캔버스의 저장과 같다)
code="$(api PUT "/agents/$AG_ID" "@$WORK/person.json")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code'], d['detail']['revision']")" = "revision_conflict $((REV0 + 2))" ] || die 13 "a PUT with a stale expect_revision -> $code $(cat "$WORK/body")"
code="$(api GET "/assistant/sessions/$SID_P/proposal")"
[ "$code" = "200" ] && [ "$(jq_ 'd["base_revision"], d["latest_revision"]')" = "$((REV0 + 1)) $((REV0 + 2))" ] || die 13 "GET /proposal after the person's save: $code $(cat "$WORK/body")"
code="$(api GET "/assistant/sessions/$SID_P")"
CALL_P2="$(jq_ 'd["pending"]["calls"][0]["id"]')"; DIGEST_P2="$(jq_ 'd["pending"]["calls"][0]["args"]["_proposal"]["digest"]')"
code="$(sse_post "/assistant/sessions/$SID_P/decisions" "{\"decisions\": [{\"id\": \"$CALL_P2\", \"type\": \"approve\", \"proposal_digest\": \"$DIGEST_P2\"}]}")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 13 "POST /decisions approve (stale base) -> $code"; }
[ "$(sse_ 'nth("tool_result", 0)["status"], content(0)["code"], content(0)["status"]')" = "error revision_conflict 409" ] \
  || die 13 "approving a stale base: $(sse_ '[d["content"][:400] for d in every("tool_result")]')"
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["revision"]')" = "$((REV0 + 2))" ] || die 13 "the stale approval saved something: revision $(jq_ 'd.get("revision")') (expected $((REV0 + 2)))"
pass 13 "session $SID_P: layout-only PUT kept r$REV0; turn $NAMES_P1 (person_marks_only, proposal_invalid with model_count, then a card: $CARD_P1, marks=$MARKS_P1); nothing saved before the decision; GET /proposal base=latest=r$REV0; another digest 409 proposal_changed; approve saved r$((REV0 + 1)) with the stored layout kept; put_agent audit $AUDIT_P; a second proposal on r$((REV0 + 1)) after the person's PUT (r$((REV0 + 2)), stale PUT 409 revision_conflict) answers revision_conflict and saves nothing"

finish
