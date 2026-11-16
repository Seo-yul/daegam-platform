#!/usr/bin/env bash
# P45 (D109) 평가와 되돌리기 e2e. 로컬 클러스터(태그의 클러스터 e2e 잡이 만든 k3d 의 provisioning 조각, deploy-local·lab-up
# 이후)만 대상이다. header 모드이고 모델은 스텁이다(ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true). 평가는 켜져 있어야 한다
# (DAEGAM_EVALUATIONS_ENABLED 가 false 가 아니다). 게이트는 ConfigMap 을 고치지 않는다.
#
#   make e2e-evaluation
#
# Agent 하나가 Tool 소스 둘을 쓴다. lab calculator(mock-soap)의 Add 는 OpenAPI 의 POST 라 쓰기 Tool 이고, lab petstore
# (mock-petstore)의 getPetById 는 GET 이라 읽기 Tool 이다. 스텁 모델은 메시지 안의 {"tool": ..., "args": ...} 조각으로 Tool 을
# 부르고, Tool 결과를 받으면 "결과: <내용>" 으로 끝낸다(agent-runtime/runtime/stub.py). 채점 모델도 스텁 provider 라 모델을
# 부르지 않고 답에 기준의 글이 들어 있으면 지난 것으로 본다(P45 11절). 단계는 다음과 같다:
#   STEP 1   플랫폼에 닿고 header 모드이며 home 클러스터의 preflight 가 ok 다
#   STEP 2   스텁 provider, calculator MCP Server(Add), petstore Backend API 와 MCP Server(getPetById)가 Ready 다
#   STEP 3   Agent 의 r1 을 배포하고 r2(바뀐 시스템 프롬프트)를 재배포한 뒤 r1 로 되돌리면 새 리비전 r3 이 r1 과 같은 내용의
#            지문을 갖고 배포되며, 배포 기록의 마지막 줄이 rollback(restored_from 1, revision 3)이다. 배포된 내용(r1 의 내용)으로
#            다시 되돌리면 409 nothing_to_roll_back 이다
#   STEP 4   세트(스텁 채점 모델, 반복 1)에 사례 셋을 넣는다. A 는 Add 를 가상 결과와 함께 부르고, B 는 Add 를 가상 결과 없이
#            부르며 지나지 못할 확인 항목 하나를 함께 갖고, C 는 읽기 Tool getPetById 를 부른다
#   STEP 5   최신 리비전을 평가하는 실행이 끝나면 mock-soap 은 Add 의 호출(POST /calc)을 받지 않았고 mock-petstore 는 getPetById
#            (GET /v2/pet/1)를 받았다. A 의 Add 는 simulated 이고 답에 가상 결과가 있다. B 의 Add 는 rejected 이고 답에 거부의 글이
#            있으며 B 는 지나지 못한다. C 의 getPetById 는 ran 이고 답에 doggie 가 있다. 실행이 header 모드를 적고 통과율이 2/3 이다
#   STEP 6   게이트(통과율 60%)를 켜고 r4 를 저장하면 그 재배포는 409 evaluation_required(no_run)다. r4 를 평가해 지난 뒤의
#            재배포는 배포되고 기록이 그 실행을 적는다. 레플리카 수만 바꾼 재배포는 게이트를 거치지 않는다. r5 를 저장하고 까닭을
#            적어 평가 없이 재배포하면 배포되고 기록이 그 까닭과 사람을 적는다
#   STEP 7   전화번호가 든 질문의 턴에 동의 없이 좋아요를 남기면 복사가 409 evaluation_consent_missing 이다. 동의를 켜면 복사본의
#            질문이 [phone] 을 갖고 번호는 없으며, 출처와 함께 저장된다. 그 사례를 넣은 실행이 끝난 뒤 좋아요를 거두면 사례와 그
#            결과가 지워지고 판정은 남는다
#   정리     게이트를 끄고 Agent(세트, 사례, 실행이 함께 지워진다), MCP Server 둘, Backend API 둘, 넣은 스텁 provider 를 지운다
#
# 실행을 기다리는 폴링은 감사에 남지 않는 실행 목록(GET .../runs)을 읽고, 감사에 남는 실행 하나의 읽기는 끝난 뒤 한 번만 한다.
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
  || { echo "e2e-evaluation 은 로컬 클러스터만 대상이다 (got E2E_TARGET=$E2E_TARGET)"; exit 1; }
CTX="${KUBE_CONTEXT:-orbstack}"
TENANT="${E2E_TENANT:-evaluation}"
API_PORT="${E2E_API_PORT:-$PORT_EVALUATION_API}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
# 실행 하나를 기다리는 한도(초). 미리 보기 파드가 뜨고 사례 셋이나 넷을 돌고 채점하는 시간에 여유를 더했다
RUN_WAIT="${E2E_EVALUATION_RUN_WAIT:-600}"

STUB_PROVIDER="stub-eval-e2e"
STUB_MODEL="stub"
BP_NAME="calc-eval"; MS_NAME="calc-eval-e2e"
PET_BP_NAME="pets-eval"; PET_MS_NAME="pets-eval-e2e"
PET_SPEC="$ROOT/platform/tests/fixtures/petstore-swagger2.json"
PET_ENDPOINT="http://mock-petstore.${SPIKE_NS}.svc:18081/v2"
AG_NAME="eval-e2e"; AG_DISPLAY="평가 에이전트 e2e"
SET_NAME="eval-e2e-set"
SIMULATED="SIMULATED-SEVEN"
SKIP_REASON="e2e evaluation skip"
# 전화번호가 든 질문. 스텁은 sleep 조각이 있으면 Tool 없이 글로 답한다
PHONE="010-2345-6789"
PHONE_MSG="{\"sleep\": 0} 제 연락처는 $PHONE 입니다. 환불해 주세요."
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-evaluation.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # header 모드라 Bearer 가 없다 (lib/e2e_api.sh 가 읽는다)
PF_API=""
HOME_ID=""; MS_ID=""; PET_BP_ID=""; PET_MS_ID=""; AG_ID=""; SET_ID=""; GATE_ON=0

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"
# pass·skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·api_delete·wait_gone·wait_ready·save_doc·create_fresh·deploy_ready·error_code·upload_backend_api·ensure_stub_provider·
# remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# calc_mcp_server·calc_teardown 은 여기에 있다
. "$HERE/lib/e2e_calc.sh"

cleanup() {
  # 게이트는 Agent 문서의 칸이라 Agent 와 함께 사라지지만, Agent 를 지우지 못했을 때 남은 게이트가 다음 실행을 막지 않게 먼저 끈다
  [ "$GATE_ON" = 0 ] || [ -z "$AG_ID" ] || api PUT "/agents/$AG_ID/evaluation-gate" null >/dev/null || true
  # Agent 를 지우면 그 세트, 사례, 실행도 지워진다 (P45 7절)
  if [ -n "$AG_ID" ]; then
    api_delete "/agents/$AG_ID"
    wait_gone "/agents/$AG_ID" 180 || echo "!! Agent $AG_NAME ($AG_ID) may be left in tenant $TENANT"
  fi
  [ -z "$PET_MS_ID" ] || api_delete "/mcp-servers/$PET_MS_ID"
  calc_teardown
  [ -z "$PET_BP_ID" ] || api_delete "/backend-apis/$PET_BP_ID"
  [ -z "$CALC_BP_ID" ] || api_delete "/backend-apis/$CALC_BP_ID"
  [ "${STUB_PROVIDER_ADDED:-0}" = 0 ] || remove_providers "$STUB_PROVIDER"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# 그 lab 파드의 로그에서 PATTERN 이 든 줄의 수를 LOG_COUNT 에 남긴다. 파드가 다시 떴으면 앞 실행의 줄은 없다. 로그를 읽지
# 못하면 die STEP 이다. 빈 수끼리 견주면 "쓰기가 돌지 않았다" 가 거짓으로 지나기 때문이다
LOG_COUNT=""
log_count() { # STEP DEPLOYMENT PATTERN
  "${K[@]}" -n "$SPIKE_NS" logs "deploy/$2" --tail=-1 >"$WORK/lab.log" 2>"$WORK/lab.err" \
    || die "$1" "kubectl logs deploy/$2 -n $SPIKE_NS failed: $(cat "$WORK/lab.err")"
  LOG_COUNT="$(grep -c -- "$3" "$WORK/lab.log" || true)"
  case "$LOG_COUNT" in '' | *[!0-9]*) die "$1" "could not count '$3' in the logs of deploy/$2 (got '$LOG_COUNT')" ;; esac
}

# Agent 의 저장 본문을 찍는다. 템플릿의 첫 그래프에서 메모리를 빼고(lib/agent_body.py), Tool 노드 둘(calculator 의 Add, petstore
# 의 getPetById)을 승인 없이 메인 에이전트에 잇는다. PROMPT 는 메인 에이전트의 시스템 프롬프트다. 리비전마다 이것만 바꾼다
agent_body() { # PROMPT
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "$AG_DISPLAY" \
    --provider "$STUB_PROVIDER" --tools "mcp_instance:$MS_ID:Add" --tools "mcp_instance:$PET_MS_ID:getPetById" --system-prompt "$1"
  cat "$WORK/agent.json"
}

# 새 리비전을 저장한다. PUT 이 200 이고 리비전이 하나 늘어야 한다. 끝나면 $WORK/body 가 PUT 의 답이다
save_revision() { # STEP PROMPT
  local code
  code="$(api PUT "/agents/$AG_ID" "$(agent_body "$2")")"
  [ "$code" = 200 ] && [ "$(jq_ 'd["revision_created"]')" = True ] || die "$1" "PUT /agents/$AG_ID (new revision) -> $code"
}

# 그 세트로 실행을 시작하고 끝날 때까지 기다린다. 시작은 지금 세트의 지문을 싣는다(GET .../runs 의 review). REVISION 이 비면
# 저장한 최신 리비전이다. RUN_ID 를 남기고, 끝나면 $WORK/body 가 실행 하나의 GET 응답이다. finished 가 아니면 die STEP 이다
RUN_ID=""
run_set() { # STEP [REVISION]
  local step="$1" digest code body state deadline
  code="$(api GET "/agents/$AG_ID/evaluation-sets/$SET_ID/runs")"
  [ "$code" = 200 ] || die "$step" "GET runs -> $code"
  digest="$(jq_ 'd["review"]["set_digest"]')"
  body="$(python3 -c 'import json,sys; d={"set_digest": sys.argv[1]}; d.update({"revision": int(sys.argv[2])} if sys.argv[2] else {}); print(json.dumps(d))' "$digest" "${2:-}")"
  code="$(api POST "/agents/$AG_ID/evaluation-sets/$SET_ID/runs" "$body")"
  [ "$code" = 202 ] || die "$step" "POST runs -> $code $(error_code "$WORK/body")"
  RUN_ID="$(jq_ 'd["id"]')"
  echo ">> waiting up to ${RUN_WAIT}s for evaluation run $RUN_ID"
  deadline=$((SECONDS + RUN_WAIT))
  # 실행 하나의 읽기는 감사에 남으므로 목록에서 상태를 본다
  while :; do
    code="$(api GET "/agents/$AG_ID/evaluation-sets/$SET_ID/runs")"
    [ "$code" = 200 ] || die "$step" "GET runs -> $code"
    state="$(jq_ "next((r['state'] for r in d['items'] if r['id'] == '$RUN_ID'), 'missing')")"
    case "$state" in
      finished) break ;;
      error | cancelled) die "$step" "run $RUN_ID ended $state ($(jq_ "next(r['reason'] for r in d['items'] if r['id'] == '$RUN_ID')"))" ;;
      missing) die "$step" "run $RUN_ID is not in the set's runs" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || die "$step" "run $RUN_ID still $state after ${RUN_WAIT}s"
    sleep 5
  done
  code="$(api GET "/agents/$AG_ID/evaluation-sets/$SET_ID/runs/$RUN_ID")"
  [ "$code" = 200 ] || die "$step" "GET run $RUN_ID -> $code"
}

# 실행 하나의 결과에서 사례 CASE_ID 의 식 EXPR 을 찍는다. r 는 그 사례의 결과(반복 0)이고 없으면 None 이다. 식이 튜플이면
# json_of 처럼 공백으로 이어 찍는다
result_of() { # CASE_ID EXPR
  jq_ "next(((lambda r: ($2))(r) for r in d['results'] if r['case_id'] == '$1' and r['repeat'] == 0), None)"
}

# 사례 하나를 넣고 그 id 를 CASE_ID 에 남긴다
CASE_ID=""
add_case() { # STEP BODY
  local code
  code="$(api POST "/agents/$AG_ID/evaluation-sets/$SET_ID/cases" "$2")"
  [ "$code" = 201 ] || die "$1" "POST cases -> $code $(error_code "$WORK/body")"
  CASE_ID="$(jq_ 'd["id"]')"
}

# 마지막 배포 기록 줄의 식 EXPR 을 찍는다. r 은 그 줄이다. Agent 를 다시 읽으므로 끝나면 $WORK/body 가 그 문서다
last_row() { # EXPR
  api GET "/agents/$AG_ID" >/dev/null
  jq_ "(lambda r: ($1))(d['deploy_history'][-1])"
}

# ---------- 1. platform reachable ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = 200 ] || die 1 "GET /healthz -> $code"
code="$(api GET /clusters)"
[ "$code" = 200 ] || die 1 "GET /clusters -> $code (header mode expected)"
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"] == "home"), "")')"
[ -n "$HOME_ID" ] && [ "$HOME_OK" = True ] || die 1 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
pass 1 "platform reachable :$API_PORT, header mode, cluster home id=$HOME_ID preflight ok"

# ---------- 2. stub provider, calculator (write Tool Add), petstore (read Tool getPetById) ----------
ensure_stub_provider 2 "$STUB_PROVIDER"
calc_mcp_server 2 "$BP_NAME" "$MS_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
MS_ID="$CALC_MS_ID"
upload_backend_api 2 "$PET_BP_NAME" "$PET_SPEC"
PET_BP_ID="$BACKEND_API_ID"
code="$(api PATCH "/backend-apis/$PET_BP_ID" "{\"endpoint\": \"$PET_ENDPOINT\"}")"
[ "$code" = 200 ] || die 2 "PATCH /backend-apis/$PET_BP_ID endpoint -> $code"
save_doc 2 /mcp-servers "$PET_MS_NAME" \
  "{\"name\": \"$PET_MS_NAME\", \"backend_profile_id\": \"$PET_BP_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": [\"getPetById\"]}"
PET_MS_ID="$SAVED_ID"
deploy_ready 2 /mcp-servers "$PET_MS_ID" "{\"cluster_id\": \"$HOME_ID\"}"
pass 2 "provider $STUB_PROVIDER (added this run: $STUB_PROVIDER_ADDED), calculator $MS_NAME and petstore $PET_MS_NAME Ready"

# ---------- 3. rollback makes a new revision with the old content and a history row ----------
code="$(api GET /agents/templates)"
[ "$code" = 200 ] || die 3 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
# 앞 실행이 남긴 같은 이름의 Agent 는 지우고 새로 만든다. 리비전 번호와 배포 기록이 r1 에서 시작해야 한다
create_fresh 3 /agents "$AG_NAME" "$(agent_body "평가 e2e 의 첫 리비전이다.")"
AG_ID="$CREATED_ID"
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_ID r1"
deploy_ready 3 /agents "$AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
save_revision 3 "평가 e2e 의 둘째 리비전이다."
code="$(api POST "/agents/$AG_ID/redeploy" '{}')"
[ "$code" = 200 ] || die 3 "redeploy r2 -> $code"
wait_ready "/agents/$AG_ID" 3
code="$(api POST "/agents/$AG_ID/rollback" '{"revision": 1}')"
[ "$code" = 200 ] || die 3 "rollback to r1 -> $code $(error_code "$WORK/body")"
[ "$(jq_ 'd["revision"], d["deployed_revision"]')" = "3 3" ] || die 3 "rollback revisions: $(jq_ 'd["revision"], d["deployed_revision"]')"
[ "$(last_row 'r["action"], r["restored_from"], r["revision"]')" = "rollback 1 3" ] || die 3 "last history row: $(jq_ "d['deploy_history'][-1]")"
code="$(api GET "/agents/$AG_ID/revisions")"
[ "$code" = 200 ] || die 3 "GET revisions -> $code"
[ "$(jq_ '[r["spec_digest"] for r in d if r["revision"] == 1] == [r["spec_digest"] for r in d if r["revision"] == 3] != [r["spec_digest"] for r in d if r["revision"] == 2]')" = True ] \
  || die 3 "r3 does not carry r1's content digest: $(jq_ '[(r["revision"], r["spec_digest"]) for r in d]')"
wait_ready "/agents/$AG_ID" 3
code="$(api POST "/agents/$AG_ID/rollback" '{"revision": 1}')"
[ "$code" = 409 ] && [ "$(error_code "$WORK/body")" = nothing_to_roll_back ] || die 3 "rollback to the deployed content -> $code $(error_code "$WORK/body")"
pass 3 "rollback to r1 saved r3 with r1's digest and deployed it (history: rollback restored_from 1); rolling back to the deployed content -> 409 nothing_to_roll_back"

# ---------- 4. a set with a simulated write, a rejected write and a read ----------
code="$(api POST "/agents/$AG_ID/evaluation-sets" "{\"name\": \"$SET_NAME\", \"grader\": {\"provider\": \"$STUB_PROVIDER\", \"model\": \"$STUB_MODEL\"}, \"repeats\": 1}")"
[ "$code" = 201 ] || die 4 "POST evaluation-sets -> $code $(error_code "$WORK/body")"
SET_ID="$(jq_ 'd["id"]')"
CASE_BODY="$(python3 - "$(stub_call Add '{"a": 3, "b": 4}')" "$(stub_call Add '{"a": 1, "b": 1}')" "$(stub_call getPetById '{"petId": 1}')" "$SIMULATED" <<'PY'
import json, sys
simulated, rejected, read, result = sys.argv[1:5]
def case(message, checks, **extra):
    return {"content": {"messages": [message], "checks": checks, **extra}}
print(json.dumps(case(simulated, [
    {"kind": "tool_called", "tool": "Add", "args": {"a": 3}},
    {"kind": "answer_contains", "text": result},
], tool_results={"Add": result}, criteria=[result])))
# 가상 결과가 없어 거부된다. 둘째 확인 항목은 쓴 대로 지나지 못한다
print(json.dumps(case(rejected, [
    {"kind": "tool_called", "tool": "Add"},
    {"kind": "answer_contains", "text": "not called"},
    {"kind": "answer_contains", "text": result},
])))
print(json.dumps(case(read, [
    {"kind": "tool_called", "tool": "getPetById"},
    {"kind": "answer_contains", "text": "doggie"},
    {"kind": "tool_not_called", "tool": "Add"},
])))
PY
)"
CASES=""
while IFS= read -r body; do
  add_case 4 "$body"
  CASES="$CASES $CASE_ID"
done <<EOF
$CASE_BODY
EOF
read -r CASE_SIM CASE_REJ CASE_READ <<EOF
$CASES
EOF
[ -n "$CASE_READ" ] || die 4 "the three cases were not all saved: [$CASES]"
pass 4 "set $SET_NAME ($SET_ID) with cases simulated=$CASE_SIM rejected=$CASE_REJ read=$CASE_READ"

# ---------- 5. a run on the latest revision: writes never run, reads do ----------
log_count 5 mock-soap 'POST /calc'; SOAP_BEFORE="$LOG_COUNT"
log_count 5 mock-petstore 'GET /v2/pet/1 '; PET_BEFORE="$LOG_COUNT"
run_set 5
FIRST_RUN="$RUN_ID"
log_count 5 mock-soap 'POST /calc'; SOAP_AFTER="$LOG_COUNT"
log_count 5 mock-petstore 'GET /v2/pet/1 '; PET_AFTER="$LOG_COUNT"
[ "$SOAP_AFTER" = "$SOAP_BEFORE" ] || die 5 "mock-soap received $((SOAP_AFTER - SOAP_BEFORE)) POST /calc during the run (want none)"
[ "$PET_AFTER" -gt "$PET_BEFORE" ] || die 5 "mock-petstore received no GET /v2/pet/1 during the run"
[ "$(jq_ 'd["revision"], d["header_mode"], d["content_hidden"]')" = "3 True False" ] || die 5 "run: $(jq_ 'd["revision"], d["header_mode"], d["content_hidden"]')"
[ "$(result_of "$CASE_SIM" "[(c['name'], c['answer']) for c in r['tool_calls']], '$SIMULATED' in r['answer']")" = "[('Add', 'simulated')] True" ] \
  || die 5 "simulated case: $(result_of "$CASE_SIM" 'r')"
[ "$(result_of "$CASE_REJ" "[(c['name'], c['answer']) for c in r['tool_calls']], 'not called' in r['answer']")" = "[('Add', 'rejected')] True" ] \
  || die 5 "rejected case: $(result_of "$CASE_REJ" 'r')"
[ "$(result_of "$CASE_READ" "[(c['name'], c['answer']) for c in r['tool_calls']], 'doggie' in r['answer']")" = "[('getPetById', 'ran')] True" ] \
  || die 5 "read case: $(result_of "$CASE_READ" 'r')"
# 확인 항목은 쓴 대로 지나고 지나지 못한다. B 의 셋째(가상 결과의 글)만 지나지 못한다
[ "$(result_of "$CASE_REJ" "[c['passed'] for c in r['checks'] if c['kind'] != 'done']")" = "[True, True, False]" ] \
  || die 5 "rejected case checks: $(result_of "$CASE_REJ" "r['checks']")"
[ "$(jq_ "sorted((v['case_id'], v['checks_passed']) for v in d['verdicts']) == sorted([('$CASE_SIM', True), ('$CASE_REJ', False), ('$CASE_READ', True)])")" = True ] \
  || die 5 "verdicts: $(jq_ 'd["verdicts"]')"
[ "$(jq_ 'd["totals"]["counted"], d["totals"]["passed"]')" = "3 2" ] || die 5 "totals: $(jq_ 'd["totals"]')"
pass 5 "run $FIRST_RUN on r3: POST /calc ${SOAP_BEFORE}->${SOAP_AFTER} (none), GET /v2/pet/1 ${PET_BEFORE}->${PET_AFTER}; Add simulated and rejected, getPetById ran; 2 of 3 passed"

# ---------- 6. the gate: refused without a run, admitted with one, a replica change passes, a skip keeps its reason ----------
code="$(api PUT "/agents/$AG_ID/evaluation-gate" "{\"set_id\": \"$SET_ID\", \"min_pass_rate\": 0.6, \"no_worse_cases\": false, \"max_age_days\": 7}")"
[ "$code" = 200 ] && [ "$(jq_ 'd["gate"]["set_id"]')" = "$SET_ID" ] || die 6 "PUT evaluation-gate -> $code $(cat "$WORK/body")"
GATE_ON=1
save_revision 6 "평가 e2e 의 넷째 리비전이다."
code="$(api POST "/agents/$AG_ID/redeploy" '{}')"
[ "$code" = 409 ] && [ "$(error_code "$WORK/body")" = evaluation_required ] && [ "$(jq_ 'd["detail"]["reason"]')" = no_run ] \
  || die 6 "redeploy of new content without a run -> $code $(cat "$WORK/body")"
run_set 6
GATE_RUN="$RUN_ID"
[ "$(jq_ 'd["revision"]')" = 4 ] || die 6 "the gate run evaluated r$(jq_ 'd["revision"]') (want r4)"
code="$(api POST "/agents/$AG_ID/redeploy" '{}')"
[ "$code" = 200 ] || die 6 "redeploy after a passed run -> $code $(error_code "$WORK/body")"
[ "$(last_row 'r["action"], r["revision"], r["evaluation_run"]')" = "redeploy 4 $GATE_RUN" ] || die 6 "history after the admitted deploy: $(jq_ "d['deploy_history'][-1]")"
wait_ready "/agents/$AG_ID" 6
code="$(api POST "/agents/$AG_ID/redeploy" '{"replicas": 2}')"
[ "$code" = 200 ] || die 6 "redeploy that changes only replicas -> $code $(error_code "$WORK/body")"
[ "$(last_row 'r["revision"], r["evaluation_run"], r["skipped_reason"]')" = "4 None None" ] || die 6 "history after the replica change: $(jq_ "d['deploy_history'][-1]")"
code="$(api POST "/agents/$AG_ID/redeploy" '{"replicas": 1}')"
[ "$code" = 200 ] || die 6 "redeploy back to one replica -> $code"
wait_ready "/agents/$AG_ID" 6
save_revision 6 "평가 e2e 의 다섯째 리비전이다."
code="$(api POST "/agents/$AG_ID/redeploy" "{\"skip_evaluation\": {\"reason\": \"$SKIP_REASON\"}}")"
[ "$code" = 200 ] || die 6 "redeploy with a skip -> $code $(error_code "$WORK/body")"
[ "$(last_row 'r["revision"], r["skipped_reason"], bool(r["by"])')" = "5 $SKIP_REASON True" ] || die 6 "history after the skip: $(jq_ "d['deploy_history'][-1]")"
wait_ready "/agents/$AG_ID" 6
pass 6 "gate on: new content without a run -> 409 evaluation_required (no_run); after run $GATE_RUN passed, r4 deployed and the history names it; replicas-only redeploy passed; skip deployed r5 and kept its reason"

# ---------- 7. a copy needs consent, replaces the phone number, and goes with the rating ----------
code="$(chat "/agents/$AG_ID/chat" "$(msg "$PHONE_MSG")")"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 7 "chat -> $code"; }
SID="$(sse_ 'first("done")["session_id"]')"; TID="$(sse_ 'first("done")["turn_id"]')"
FEEDBACK="/agents/$AG_ID/sessions/$SID/turns/$TID/feedback"
FROM_TURN="{\"session_id\": \"$SID\", \"turn_id\": \"$TID\"}"
code="$(api PUT "$FEEDBACK" '{"rating": "up"}')"
[ "$code" = 200 ] || die 7 "PUT feedback -> $code"
code="$(api POST "/agents/$AG_ID/evaluation-sets/$SET_ID/cases/from-turn" "$FROM_TURN")"
[ "$code" = 409 ] && [ "$(error_code "$WORK/body")" = evaluation_consent_missing ] || die 7 "copy without consent -> $code $(error_code "$WORK/body")"
code="$(api PUT "$FEEDBACK" '{"rating": "up", "evaluation_use": true}')"
[ "$code" = 200 ] && [ "$(jq_ 'd["evaluation_use"]')" = True ] || die 7 "PUT feedback with consent -> $code"
code="$(api POST "/agents/$AG_ID/evaluation-sets/$SET_ID/cases/from-turn" "$FROM_TURN")"
[ "$code" = 200 ] || die 7 "copy with consent -> $code $(error_code "$WORK/body")"
[ "$(jq_ "'[phone]' in d['content']['messages'][0], '$PHONE' in json.dumps(d['content'], ensure_ascii=False), d['replaced'].get('phone')")" = "True False 1" ] \
  || die 7 "the copy kept the phone number: $(jq_ 'd["content"]["messages"], d["replaced"]')"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps({"content": d["content"], "origin": d["origin"]}, ensure_ascii=False))' "$WORK/body" >"$WORK/copy.json"
add_case 7 "@$WORK/copy.json"
COPIED="$CASE_ID"
run_set 7
COPY_RUN="$RUN_ID"
[ "$(jq_ "any(r['case_id'] == '$COPIED' for r in d['results'])")" = True ] || die 7 "run $COPY_RUN has no result of the copied case"
code="$(api DELETE "$FEEDBACK")"
[ "$code" = 204 ] || die 7 "DELETE feedback -> $code"
code="$(api GET "/agents/$AG_ID/evaluation-sets/$SET_ID/cases")"
[ "$code" = 200 ] && [ "$(jq_ "any(c['id'] == '$COPIED' for c in d['items'])")" = False ] || die 7 "the copied case is still there after the rating was withdrawn"
code="$(api GET "/agents/$AG_ID/evaluation-sets/$SET_ID/runs/$COPY_RUN")"
[ "$code" = 200 ] || die 7 "GET run $COPY_RUN -> $code"
[ "$(jq_ "any(r['case_id'] == '$COPIED' for r in d['results']), any(v['case_id'] == '$COPIED' for v in d['verdicts'])")" = "False True" ] \
  || die 7 "after the withdrawal the run still has the copied case's results or lost its verdict"
pass 7 "copy refused without consent (409), saved with it ([phone] in place of the number); withdrawing the rating deleted case $COPIED and its results, its verdict stays"

finish
