#!/usr/bin/env bash
# P40 (D95) 코드 실행 샌드박스 e2e. 클러스터의 격리 점검과 허용, Agent 가 세션마다 여는 작업 공간 파드에서 도는 execute,
# 작업 공간 파일의 내려받기와 그 거절, 막힌 네트워크, 멈춤과 시한, 메모리 초과, 회수와 유휴 회수, 네임스페이스 정책의 거절,
# 읽을 때의 무효와 개발용 우회, 명령 승인, 명령 도중의 배포 내리기를 실제 클러스터에서 본다.
#
#   make e2e-sandbox KUBE_CONTEXT=k3d-daegam
#       로컬 클러스터(릴리스 후보 태그의 클러스터 e2e 잡이 만든 k3d 의 sandbox 조각). make deploy-local 과 make lab-up
#       뒤이고 이미지 daegam-sandbox:0.1.0 이 노드에 있어야 한다. 헤더 모드, 스텁 provider, lab Postgres 다. 로컬
#       배포본은 개발용 우회(DAEGAM_SANDBOX_ALLOW_UNCHECKED)를 켜 두므로 STEP 0 이 이 게이트 동안 끄고, STEP 12 가 배포본의
#       값을 되돌려 우회의 길을 본다
#   make e2e-sandbox E2E_TARGET=install E2E_INSTALL_USER=<e2e 전용 관리자>
#       설치본(P85 10절), local 모드, 테넌트 e2e, 공개 주소. 설치 값 파일의 E2E_SANDBOX_CAPACITY 가 있으면 이 게이트 동안
#       클러스터 상한(DAEGAM_SANDBOX_MAX)을 그 값으로 둔다(노드가 작업 공간 하나만 담으면 1). 없으면 로컬처럼 노드가 담는
#       만큼이다. 스텁 provider 는 e2e-runs 처럼 이 게이트 동안만 ConfigMap 에 연다. memory 는 플랫폼
#       네임스페이스의 Postgres 다. 격리 점검과 허용은 플랫폼 관리자의 결정이라 이 갈래는 점검을 돌리지도 허용을 쓰지도
#       않는다. 관리자가 home 의 Cluster 화면에서 점검하고 허용해 둔 판정을 읽고, 없으면 아무것도 바꾸기 전에 멈춘다.
#       make e2e-install-extra 가 태그의 e2e: 줄이 고르면 돌린다
#
# 두 갈래 모두 기록을 플랫폼 MongoDB(mongodb-0)의 sandboxes 문서로 읽고, 회수와 유휴 회수는 그 문서의 last_used_at 을
# 당겨 30분과 5분을 기다리지 않는다(lib/e2e_turn_tokens.sh platform_mongo). STEP 0 이 ConfigMap 을 고치기 전에 되돌리는
# 명령을 찍고, 정리가 되돌린 뒤 플랫폼을 굴린다. 로컬 클러스터는 노드 하나에 플랫폼 두 벌과 daegam-docparse 두 벌이
# 메모리 한도를 거의 다 잡아 작업 공간이 하나도 들어가지 않으므로, 게이트 동안 둘을 한 벌씩으로 줄이고 작업 공간을
# 줄인다(E2E_SANDBOX_WORKSPACE, 기본 128 MiB, 예비 E2E_SANDBOX_RESERVE 기본 0). 정리가 벌 수를 되돌린다.
#
# Gate:
#   STEP 0   플랫폼에 붙는다(설치본은 관리자 로그인). 아무것도 바꾸기 전에 앞 실행이 남긴 Agent 를 지우고 그 작업 공간이
#            닫히기를 기다린 뒤, 열린 작업 공간이 하나도 없어야 한다. 설치본은 앞 실행이 남긴 ConfigMap 키가 없고 관리자가
#            점검해 허용한 판정이 서 있어야 한다. ConfigMap 창을 연다. 로컬은 우회를 끄고 작업 공간·예비·사람마다 상한 1·
#            스윕 10초, 설치본은 스텁 provider 와 사람마다 상한 1·스윕 10초에 E2E_SANDBOX_CAPACITY 가 있으면 클러스터
#            상한이다. 플랫폼을 굴린다
#   STEP 1   home 의 preflight 와 샌드박스 권한, 지금 센 용량 1 이상, 스텁 provider 와 memory Database
#   STEP 2   로컬: 격리 점검이 202, 도는 동안 둘째는 409 sandbox_check_running, 끝난 판정이 통과(user namespace,
#            네트워크, 남긴 대상, 이미지 id). 노드를 함께 쓰면 게이트가 unaccepted 로 닫혀 코드 실행 Agent 의 배포가 409
#            sandbox_gate_closed(unaccepted)이고, 낡은 checked_at 의 허용은 409 sandbox_check_stale, 맞는 허용 뒤 열린다.
#            설치본: 관리자의 판정이 통과했고 무효로 읽히지 않으며(함께 쓰면 허용됐으며) 게이트가 열렸다
#   STEP 3   코드 실행을 켠 Agent(스텁, memory, 파일 Tool, 서브에이전트, 턴 300초)가 Ready 이고 runtime.features 에
#            code_execution 이, 파드 env 에 DAEGAM_SANDBOX_URL 이 있다
#   STEP 4   xlsx 를 단 턴의 execute 가 /workspace/uploads 의 원본을 openpyxl 로 읽고 matplotlib 로 /workspace/chart.png 를
#            쓴다. 세션 파일 목록의 workspace 줄이고 D61 헤더 다섯과 PNG 바이트로 내려받힌다. 기록과 파드: 점검한 기록,
#            hostUsers false 를 되읽음, 샌드박스 라벨, 한 노드에 묶임, PriorityClass, SA 토큰 없음, Agent Deployment 소유
#   STEP 5   판정의 대상(양성 대조가 닿은 것과 클러스터 DNS·API)이 사용자 코드에서 하나도 닿지 않는다. 8080 은 Agent
#            파드에서만 열리고 Agent 가 아닌 파드(샌드박스 이미지의 client 모드, 3)와 플랫폼 파드에서는 막혔다
#   STEP 6   작업 공간에서 만든 ELF 와 .exe 를 담은 zip 은 403 file_refused_executable 이고 셸 스크립트는 내려받힌다
#   STEP 7   sleep 600 도중 중지 API 로 멈추면 스트림이 몇 초 안에 turn_cancelled 로 끝나고 다음 명령이 그 sleep 을
#            보지 않는다. timeout 3 의 sleep 30 은 3초에 멈춘 표시와 함께 끝난다
#   STEP 8   메모리 한도를 넘는 명령은 "ran out of memory ... are kept" 로 끝나고 컨테이너가 OOMKilled 로 한 번 다시 떴으며
#            앞서 쓴 파일이 남았다
#   STEP 9   둘째 세션. 용량은 Agent 파드가 선 뒤 목록 캐시(60초)가 지나 다시 읽는다. 둘이 들어가면 5분 넘게 쓰지
#            않은 첫 세션의 작업 공간을 사람마다 상한이 회수하고(reclaimed) 첫 세션의 다음 호출이 회수 문장이며, 둘째
#            세션을 지운 뒤 첫 세션이 연 새 세대에는 앞서 쓴 파일이 없고 첨부는 다시 들어 있다. 하나뿐이면(E2E_SANDBOX_CAPACITY=1) 첫
#            세션이 열린 동안 둘째의 호출이 상한 문장으로 거절되고, 첫 세션을 지우면 그 파드가 지워진다
#   STEP 10  30분 넘게 쓰지 않은 작업 공간을 스윕이 닫고(idle) 파드를 지우며 그 세션의 다음 호출이 유휴 회수 문장이다
#   STEP 11  네임스페이스에 모두 여는 NetworkPolicy 를 두면 새 열기가 그 정책 이름으로 거절되고 판정은 그대로다. 정책을
#            지우면 열리고, 세션을 지우면 그 파드가 지워진다
#   STEP 12  열린 작업 공간이 없을 때만(무효 판정은 스윕이 열린 것을 모두 닫는다) 샌드박스 배치를 바꾸면 판정이 읽을
#            때 무효(placement)라 게이트가 unchecked 로 닫히고 열기가 그 문장이다.
#            로컬은 배포본의 개발용 우회를 되돌리면 같은 열기가 unchecked 기록으로 열리고 Cluster·/healthz·Agent 경고가
#            우회를 말한다. 배포본이 우회를 켜지 않았으면 그 부분은 건너뛴다.
#            배치를 되돌리면 판정이 다시 선다
#   STEP 13  execute 승인을 켜 다시 배포하면 서브에이전트의 execute 가 승인 카드(execute, rule)로 멈춘다
#   STEP 14  메인의 execute(sleep 120)를 승인해 도는 동안 배포를 내리면 그 기록이 닫히고 파드가 지워진다
#   정리     만든 정책·client 파드·배치 변경을 되돌리고, ConfigMap 을 되돌린 뒤 Agent·Database·스텁 provider 를 지우고,
#            로컬은 벌 수를 되돌리며, 플랫폼을 굴리고 설치본은 ConfigMap 의 창 키가 돌아갔는지 본다. 로컬의 판정과 허용은
#            버리는 클러스터에 남고, 설치본의 판정과 허용은 관리자의 것 그대로다
#
# 서버의 한 줄 한 줄과 상한·회수·스윕의 경주는 단위 테스트가 지킨다(sandbox/tests/, platform/tests/test_sandbox*.py,
# agent-runtime/tests/test_sandbox.py). 이 게이트는 실제 파드·user namespace·정책 컨트롤러·런타임을 지나는 길만 본다.
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
# install_env_defaults 는 여기에 있다
. "$HERE/lib/hosts.sh"

TARGET="${E2E_TARGET:-orbstack}"
API_PORT="${E2E_API_PORT:-$PORT_SANDBOX_API}"
CAPACITY_CAP=""
case "$TARGET" in
  orbstack | local)
    TARGET=local
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-sandbox}"
    BASE="http://127.0.0.1:${API_PORT}"; PROVIDER="stub-e2e" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"
    BASE="https://${DAEGAM_HOST:-}"; PROVIDER="e2e-sandbox-stub"
    # 이 게이트 동안 둘 클러스터 상한(설치 값 파일의 E2E_SANDBOX_CAPACITY). 비면 두지 않고 노드가 담는 만큼이다
    CAPACITY_CAP="$(install_count E2E_SANDBOX_CAPACITY)" || exit 1 ;;
  *) echo "E2E_TARGET 은 orbstack(로컬 클러스터)이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
API="$BASE/api"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"
# 로컬 클러스터의 작업 공간 크기와 노드 예비(바이트). 메모리 한도는 작업 공간에 1 GiB 를 더한 값이다(P40 2절)
LOCAL_WORKSPACE="${E2E_SANDBOX_WORKSPACE:-$((128 * 1024 * 1024))}"
LOCAL_RESERVE="${E2E_SANDBOX_RESERVE:-0}"
# 격리 점검을 기다리는 초. 첫 샌드박스가 노드에서 이미지를 당긴다
CHECK_WAIT="${E2E_SANDBOX_CHECK_WAIT:-600}"
# 스윕 주기(초, 최솟값 10)와 스윕이 닫기를 기다리는 초
SWEEP_SECONDS=10; SWEEP_WAIT=90
# 파드가 지워지기를 기다리는 초. 샌드박스의 terminationGracePeriodSeconds 는 1 이다
POD_GONE_WAIT=90
DS_NAME="pg-sandbox-e2e"; DS_DISPLAY="코드 실행 기억 DB"
AG_NAME="sandbox-e2e"; AG_DISPLAY="코드 실행 e2e"
SUB_NAME="coder"                   # 서브에이전트 이름. 스텁이 task 의 subagent_type 으로 댄다
POLICY_NAME="sandbox-e2e-allow-all"   # STEP 11 이 두고 지우는 정책
CLIENT_POD="sandbox-e2e-client"       # STEP 5 의 Agent 가 아닌 파드
RUN_TAG="$(date +%H%M%S)"
MARK="E2E-SBX-$RUN_TAG"            # xlsx 표본의 글과 작업 공간 파일에 넣는 실행 표지
# 런타임이 모델에 주는 문장의 조각이다(agent-runtime/runtime/sandbox.py)
OOM_TEXT="ran out of memory"; KEPT_TEXT="are kept"
RECLAIM_TEXT="reclaimed for another conversation"; IDLE_TEXT="30 minutes without use"
LIMIT_TEXTS="most workspaces open at once|no room for another workspace"
# has_text 는 확장 정규식으로 찾으므로 괄호를 글자 그대로 찾게 막는다
GATE_TEXT="Code execution is unavailable on this cluster \\(unchecked\\)"
TIMED_TEXT="stopped after 3 seconds"
RESTART_TEXTS="restarting|$OOM_TEXT"
# 중지 뒤 스트림이 끝나기까지의 초(lib/e2e_sse.sh expect_cancelled_stream). 런타임이 작업 공간 연결도 닫는다
CANCEL_END_WITHIN="${CANCEL_END_WITHIN:-5}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-sandbox.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # 헤더 모드는 Bearer 가 없다. 설치본은 로그인한 관리자다 (lib/e2e_api.sh 가 읽는다)
PF_API=""
HOME_ID=""; DS_ID=""; AG_ID=""; AG_NS=""; AG_K8S=""
S1=""; S2=""; S3=""; S4=""; S5=""; S6=""; IDLE_SID=""
CAN_RECLAIM=0                      # 작업 공간 둘이 함께 들어가 사람마다 상한만 막을 수 있다
STUB_BEFORE=""; CONFIG_CHANGED=0
# 설치본에서 창이 더하는 키다. 앞 실행이 죽으며 남겼으면 다음 배포도 지우지 않는다(설치의 값에 없는 키다)
INSTALL_WINDOW_KEYS="DAEGAM_SANDBOX_PER_PERSON DAEGAM_SANDBOX_SWEEP_SECONDS${CAPACITY_CAP:+ DAEGAM_SANDBOX_MAX}"
READY_AT=0                         # Agent 가 Ready 가 된 때(SECONDS). 용량의 목록 캐시가 그 파드를 세기까지 기다린다
LIST_CACHE_SECONDS=65              # 플랫폼의 노드·파드 목록 캐시(60초)에 여유를 더했다
OVERRIDE_BEFORE=""                 # 로컬 배포본의 개발용 우회 값. STEP 12 가 그대로 되돌린다
POLICY_PLACED=0; CLIENT_PLACED=0
SCHED_RESTORE=""                   # STEP 12 가 바꾼 배치를 되돌리는 PATCH 본문. 비면 되돌릴 것이 없다
PLATFORM_REPLICAS=""; DOCPARSE_REPLICAS=""   # 로컬이 줄이기 전의 벌 수

# start_pf·stop_pf·restart_pf·wait_port·wait_http 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"
# pass·skip·cond_skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·api_delete·wait_ready·wait_gone·save_doc·deploy_ready·require_features·ensure_stub_provider·
# drop_stub_provider·remove_providers·install_postgres_database 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call·task_call·asked·now_ms·chat_bg·bg_stop·bg_ends_by·expect_cancelled_stream 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab Database 등록(lab_database)은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# 표본 만들기(chat_samples), 업로드(upload_ok), 내려받기(download·download_headers_off)는 여기에 있다
. "$HERE/lib/e2e_chat_files.sh"
# platform_config·restart_platform·roll_platform_api·config_window_*·agent_pods·wait_pods_settled 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# platform_connect(설치본은 관리자 로그인, 로컬은 port-forward)는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# platform_mongo_here·platform_mongo 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"

# exec_frag COMMAND [TIMEOUT]  스텁이 execute 를 부를 조각을 찍는다
exec_frag() {
  python3 -c 'import json, sys
args = {"command": sys.argv[1]}
if len(sys.argv) > 2:
    args["timeout"] = int(sys.argv[2])
print(json.dumps({"tool": "execute", "args": args}, ensure_ascii=False))' "$@"
}

# py_frag FILE [ARG...]  파이썬 파일 FILE 을 작업 공간에서 인자 ARG 로 도는 execute 조각을 찍는다
py_frag() {
  python3 - "$@" <<'PY'
import json, shlex, sys
src, args = open(sys.argv[1]).read(), sys.argv[2:]
command = "python3 - " + " ".join(shlex.quote(a) for a in args) + " <<'EOF'\n" + src + "\nEOF\n"
print(json.dumps({"tool": "execute", "args": {"command": command}}, ensure_ascii=False))
PY
}

# say STEP SID TEXT [UPLOAD_ID...]  한 턴을 보낸다. SID 가 비면 새 세션이다. done 으로 끝나야 한다. 세션을 TURN_SID, 첫 Tool
# 결과의 글(바깥 표시 안)과 상태를 TOOL_OUT·TOOL_STATUS 에 남긴다
say() {
  local step="$1" sid="$2" text="$3" code last
  shift 3
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$text" "$sid" "$@")")"
  last="$(sse_ 'names[-1]')"
  { [ "$code" = 200 ] && [ "$last" = "done" ]; } || { cp "$WORK/sse" "$WORK/body" 2>/dev/null || :; die "$step" "turn -> $code, last event '$last'"; }
  TURN_SID="$(sse_ 'first("done")["session_id"]')"
  TOOL_OUT="$(sse_ 'inner(first("tool_result")["content"])')"
  TOOL_STATUS="$(sse_ 'first("tool_result")["status"]')"
}

# has_text TEXT ALTERNATIVES  TEXT 에 | 로 가른 조각 가운데 하나가 있으면 0 이다
has_text() { printf '%s' "$1" | grep -E -q -- "$2"; }

# sandbox_record SID  그 세션의 가장 최근 샌드박스 기록을 $WORK/rec.json 에 둔다. 없으면 1
sandbox_record() {
  local js
  js="$(python3 -c 'import json, sys
q = {"agent_id": sys.argv[1], "session_id": sys.argv[2]}
print("print(JSON.stringify(db.sandboxes.find(%s).sort({opened_at: -1}).limit(1).toArray()[0] || null))" % json.dumps(q))' "$AG_ID" "$1")" || return 1
  platform_mongo "$js" >"$WORK/rec.json" || return 1
  [ "$(json_of "$WORK/rec.json" 'd is not None')" = True ]
}
rec_() { json_of "$WORK/rec.json" "$1"; }

# open_record STEP SID  그 세션의 열린 준비된 기록을 $WORK/rec.json 에 두고 SBX_POD·SBX_ID 에 남긴다. 아니면 die STEP
open_record() {
  sandbox_record "$2" || die "$1" "no sandbox record for session $2"
  [ "$(rec_ 'd["state"] == "open" and d.get("ready_at") is not None')" = True ] \
    || die "$1" "session $2 has no open, ready sandbox: $(cat "$WORK/rec.json")"
  SBX_POD="$(rec_ 'd["pod"]')"; SBX_ID="$(rec_ 'd["_id"]')"
}

# age_record ID MINUTES  열린 기록의 last_used_at 을 MINUTES 분 앞으로 당긴다. 고친 문서가 한 건이 아니면 1
age_record() {
  local js
  js="$(python3 -c 'import json, sys
print("print(db.sandboxes.updateOne(%s, {$set: {last_used_at: new Date(Date.now() - %d * 60000)}}).matchedCount)"
      % (json.dumps({"_id": sys.argv[1], "state": "open"}), int(sys.argv[2])))' "$1" "$2")" || return 1
  [ "$(platform_mongo "$js")" = 1 ]
}

# closed_within STEP SID SECONDS  그 세션의 기록이 SECONDS 안에 닫혀야 한다. 닫힌 까닭을 CLOSED_REASON 에 남긴다
closed_within() {
  local i
  for ((i = 0; i < $3; i += 3)); do
    if sandbox_record "$2" && [ "$(rec_ 'd["state"] != "open"')" = True ]; then
      CLOSED_REASON="$(rec_ 'd.get("close_reason")')"
      return 0
    fi
    sleep 3
  done
  die "$1" "the sandbox of session $2 is still open after $3 s: $(cat "$WORK/rec.json")"
}

# pod_gone STEP POD  에이전트 네임스페이스의 그 파드가 POD_GONE_WAIT 초 안에 사라져야 한다
pod_gone() {
  local i
  for ((i = 0; i < POD_GONE_WAIT; i += 3)); do
    "${K[@]}" -n "$AG_NS" get pod "$2" -o name >/dev/null 2>&1 || return 0
    sleep 3
  done
  die "$1" "sandbox pod $AG_NS/$2 is still there after $POD_GONE_WAIT s"
}

# delete_session STEP SID  세션을 지운다. 200 이어야 한다
delete_session() {
  local code
  code="$(api DELETE "/agents/$AG_ID/sessions/$2")"
  [ "$code" = 200 ] || die "$1" "DELETE session $2 -> $code"
}

# sandbox_status STEP  GET /clusters/{home}/sandbox 를 $WORK/body 에 둔다
sandbox_status() {
  local code
  code="$(api GET "/clusters/$HOME_ID/sandbox")"
  [ "$code" = 200 ] || die "$1" "GET /clusters/$HOME_ID/sandbox -> $code"
}

# open_report SID  작업 공간이 열리지 못한 까닭을 정리 앞에 남긴다. 실패한 파드는 플랫폼이 곧 지우고 정리가 플랫폼을
# 굴리므로 그 뒤에는 볼 수 없다. 그 세션의 기록, 클러스터의 판정, 플랫폼 기록의 샌드박스 줄(87 로 끝난 파드의 종료
# 메시지가 여기 있다)과 네임스페이스의 이벤트다
open_report() {
  {
    echo "--- sandbox open report (session $1)"
    if sandbox_record "$1"; then
      rec_ '{k: d.get(k) for k in ("state", "generation", "pod", "node", "opened_at", "ready_at", "closed_at", "close_reason")}'
    else
      echo "no sandbox record"
    fi
    api GET "/clusters/$HOME_ID/sandbox" >/dev/null && jq_ '{"gate": d.get("gate"), "check": d.get("check"), "capacity": d.get("capacity")}'
    "${K[@]}" -n "$PLATFORM_NS" logs deploy/daegam-platform -c platform --tail=2000 2>/dev/null | grep -i sandbox | tail -n 40
    "${K[@]}" -n "$AG_NS" get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 30
  } >&2 || :
}

# wait_gate_open STEP  게이트가 열리고 용량이 1 이상일 때까지 120초 본다. 노드·파드 목록은 60초 캐시라 다시 배포한 뒤의
# 겹친 파드가 그만큼 남아 있을 수 있다
wait_gate_open() {
  local i
  for ((i = 0; i < 40; i++)); do
    sandbox_status "$1"
    [ "$(jq_ 'd["gate"]["open"] and d["capacity"] >= 1')" = True ] && return 0
    sleep 3
  done
  die "$1" "the sandbox gate did not open: $(cat "$WORK/body")"
}

# roll_platform  플랫폼을 굴리고 API 가 답하기를 기다린다. 로컬은 port-forward 를 새 파드로 다시 잇는다. 옛 kubectl 은
# 내려간 파드에 붙은 채 포트를 쥐고 있어 요청이 빈 응답으로 끝난다(lib/e2e_pf.sh restart_pf). 실패하면 0 이 아니다
roll_platform() {
  if [ "$TARGET" = install ]; then roll_platform_api; return; fi
  restart_platform || return 1
  restart_pf "$API_PORT"
  wait_http "$API/version" 120 || { echo "!! the platform did not answer through the port-forward after the rollout"; return 1; }
}

# live_records AGENT_ID  그 Agent 의 열렸거나 닫히는 중인 샌드박스 기록 수를 찍는다
live_records() {
  local js
  js="$(python3 -c 'import json, sys
print("print(db.sandboxes.countDocuments(%s))" % json.dumps({"agent_id": sys.argv[1], "state": {"$in": ["open", "closing"]}}))' "$1")" || return 1
  platform_mongo "$js"
}

# drop_leftover_agent STEP  앞 실행이 남긴 Agent 를 지우고 그 작업 공간 기록이 모두 닫히기를 90초 기다린다. 그 기록이
# 슬롯을 쥐면 이 실행의 열기가 거절된다
drop_leftover_agent() {
  local code old i left=""
  code="$(api GET /agents)"; [ "$code" = 200 ] || die "$1" "GET /agents -> $code"
  old="$(find_id "$AG_NAME")"
  [ -n "$old" ] || return 0
  api_delete "/agents/$old"
  wait_gone "/agents/$old" 180 || die "$1" "the Agent $AG_NAME ($old) left by an earlier run did not go away"
  for ((i = 0; i < 90; i += 3)); do
    left="$(live_records "$old")" || die "$1" "reading the sandbox records of $old failed"
    [ "$left" = 0 ] && return 0
    sleep 3
  done
  die "$1" "$left sandbox records of the deleted Agent $AG_NAME ($old) are still open after 90 s"
}

# agent_body_to OUT [FLAG...]  이 게이트의 Agent 본문을 OUT 에 둔다(lib/agent_body.py)
agent_body_to() {
  local out="$1"
  shift
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$out" --name "$AG_NAME" --display "$AG_DISPLAY" \
    --provider "$PROVIDER" --ds-id "$DS_ID" --files --sub-name "$SUB_NAME" \
    --sub-description "작업 공간에서 명령을 도는 서브에이전트다." --turn-timeout 300 "$@"
}

# agent_pod  이 Agent 의 지워지는 중이 아닌 파드 하나의 이름을 찍는다
agent_pod() { agent_pods "$AG_NS" "$AG_ID" | head -n 1; }

# make_room  로컬 클러스터에서 플랫폼과 daegam-docparse 를 한 벌씩으로 줄인다. 앞의 벌 수를 남긴다
make_room() {
  PLATFORM_REPLICAS="$("${K[@]}" -n "$PLATFORM_NS" get deploy daegam-platform -o jsonpath='{.spec.replicas}')" || return 1
  DOCPARSE_REPLICAS="$("${K[@]}" -n "$PLATFORM_NS" get deploy daegam-docparse -o jsonpath='{.spec.replicas}')" || return 1
  "${K[@]}" -n "$PLATFORM_NS" scale deploy/daegam-docparse --replicas=1 >/dev/null || return 1
  "${K[@]}" -n "$PLATFORM_NS" scale deploy/daegam-platform --replicas=1 >/dev/null || return 1
  "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-docparse --timeout=180s >/dev/null || return 1
  wait_pods_settled "$PLATFORM_NS" app=daegam-docparse
}

# give_back_room  make_room 이 줄인 벌 수를 되돌린다. 플랫폼은 뒤의 굴리기가 기다린다
give_back_room() {
  [ -z "$DOCPARSE_REPLICAS" ] || "${K[@]}" -n "$PLATFORM_NS" scale deploy/daegam-docparse --replicas="$DOCPARSE_REPLICAS" >/dev/null \
    || echo "!! scale deploy/daegam-docparse back to $DOCPARSE_REPLICAS by hand"
  [ -z "$PLATFORM_REPLICAS" ] || "${K[@]}" -n "$PLATFORM_NS" scale deploy/daegam-platform --replicas="$PLATFORM_REPLICAS" >/dev/null \
    || echo "!! scale deploy/daegam-platform back to $PLATFORM_REPLICAS by hand"
  DOCPARSE_REPLICAS=""
  if [ -n "$PLATFORM_REPLICAS" ]; then PLATFORM_REPLICAS=""; CONFIG_ROLL_PENDING=1; fi
}

# 만든 것을 걷는다. 플랫폼에 붙기 전이면 걷을 것도 없다
teardown() {
  if [ -n "$AG_ID" ]; then
    # Agent 삭제가 그 작업 공간을 모두 닫는다
    api_delete "/agents/$AG_ID"
    wait_gone "/agents/$AG_ID" 180 || echo "!! Agent $AG_NAME ($AG_ID) may be left in tenant $TENANT"
  fi
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  if [ "$TARGET" = install ]; then
    # 죽은 앞 실행이 남긴 것도 걷는다. 스텁을 다시 막은 뒤에는 그것이 남은 목록의 저장이 모두 거절된다
    drop_stub_provider "$PROVIDER" || true
  elif [ "${STUB_PROVIDER_ADDED:-0}" = 1 ]; then
    remove_providers "$PROVIDER"
  fi
}

cleanup() {
  bg_stop
  if [ "$CLIENT_PLACED" = 1 ]; then
    "${K[@]}" -n "$AG_NS" delete pod "$CLIENT_POD" --ignore-not-found --wait=false >/dev/null 2>&1 \
      || echo "!! delete pod $AG_NS/$CLIENT_POD by hand"
  fi
  if [ "$POLICY_PLACED" = 1 ]; then
    "${K[@]}" -n "$AG_NS" delete networkpolicy "$POLICY_NAME" --ignore-not-found >/dev/null 2>&1 \
      || echo "!! delete networkpolicy $AG_NS/$POLICY_NAME by hand"
  fi
  if [ -n "$SCHED_RESTORE" ]; then
    [ "$(api PATCH "/clusters/$HOME_ID" "$SCHED_RESTORE")" = 200 ] \
      || echo "!! the home cluster's sandbox placement was not restored: PATCH /api/clusters/$HOME_ID $SCHED_RESTORE"
  fi
  # ConfigMap 을 먼저 되돌린다. 굴리기 전까지 떠 있는 파드는 스텁을 받으므로 스텁 provider 를 걷는 저장이 된다
  config_window_restore || true
  if [ "$TARGET" = local ] || [ ${#AUTH[@]} -gt 0 ]; then teardown; fi
  give_back_room
  if [ "$CONFIG_ROLL_PENDING" = 1 ]; then
    if roll_platform; then
      CONFIG_ROLL_PENDING=0
      echo ">> ConfigMap $AUTH_MODE_CONFIGMAP restored, platform rolled"
    else
      echo "!! roll the platform by hand: kubectl --context $CTX -n $PLATFORM_NS rollout restart deploy/daegam-platform"
    fi
  fi
  if [ "$CONFIG_CHANGED" = 1 ] && [ "$TARGET" = install ]; then
    local key
    [ "$(platform_config DAEGAM_ALLOW_STUB_PROVIDER || true)" = "$STUB_BEFORE" ] \
      || echo "!! ConfigMap $AUTH_MODE_CONFIGMAP is not back to DAEGAM_ALLOW_STUB_PROVIDER='$STUB_BEFORE'. $CONFIG_RESTORE_HINT"
    for key in $INSTALL_WINDOW_KEYS; do
      [ -z "$(platform_config "$key" || true)" ] || echo "!! ConfigMap $AUTH_MODE_CONFIGMAP still has $key. $CONFIG_RESTORE_HINT"
    done
  fi
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

echo ">> target=$TARGET context=$CTX tenant=$TENANT api=$API"

# ---------- 0. 플랫폼, 앞 실행이 남긴 것, ConfigMap 창 ----------
platform_connect 0
code="$(api GET /clusters)"; [ "$code" = 200 ] || die 0 "GET /clusters -> $code"
HOME_ID="$(find_id home)"; [ -n "$HOME_ID" ] || die 0 "no home cluster"
platform_mongo_here || die 0 "no MongoDB pod mongodb-0 in $PLATFORM_NS, so the sandbox records cannot be read"
# 앞 실행이 남긴 Agent 는 그 작업 공간이 슬롯을 쥐므로 먼저 지운다. 그 뒤에도 열린 것은 이 게이트의 것이 아니다
drop_leftover_agent 0
sandbox_status 0
OPEN_NOW="$(jq_ 'd["open_sandboxes"]')"
[ "$OPEN_NOW" = 0 ] \
  || die 0 "the cluster holds $OPEN_NOW open sandboxes that are not this gate's; it holds one at a time and stops before changing anything"
if [ "$TARGET" = install ]; then
  STUB_BEFORE="$(platform_config DAEGAM_ALLOW_STUB_PROVIDER)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  # 설치본은 스텁을 막는다(DAEGAM_ALLOW_STUB_PROVIDER 가 없거나 false 다). 이미 열려 있으면 앞 실행이 되돌리지 못한 것이라
  # 되돌릴 값을 모른다
  [ "$STUB_BEFORE" != true ] \
    || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP already has DAEGAM_ALLOW_STUB_PROVIDER=true (left by an earlier run?): set it back to false, roll the platform and run again"
  for key in $INSTALL_WINDOW_KEYS; do
    [ -z "$(platform_config "$key")" ] \
      || die 0 "ConfigMap $AUTH_MODE_CONFIGMAP already has $key (left by a killed run): remove it with a merge patch that sets it to null, roll the platform and run again"
  done
  [ "$(platform_config DAEGAM_SANDBOX_ALLOW_UNCHECKED)" != true ] \
    || die 0 "the install's ConfigMap turns the development override DAEGAM_SANDBOX_ALLOW_UNCHECKED on; it must stay off there"
  # 점검과 허용은 플랫폼 관리자의 결정이다. 이 게이트는 관리자가 남긴 판정을 읽기만 한다
  code="$(API_OUT="$WORK/cluster.json" api GET "/clusters/$HOME_ID")"; [ "$code" = 200 ] || die 0 "GET /clusters/$HOME_ID -> $code"
  ADMIN_VERDICT="$(json_of "$WORK/cluster.json" '(lambda s: (s["check"]["passed"], s.get("void_reason"), s["check"]["shared_nodes"], s["check"]["accepted_at"] is not None))(d["sandbox"])')"
  case "$ADMIN_VERDICT" in
    "True None True True" | "True None False "*) ;;
    *) die 0 "home has no isolation verdict a platform admin checked and accepted (passed, void, shared nodes, accepted: $ADMIN_VERDICT): a platform admin must run 격리 점검 on the home Cluster screen and accept the shared node first; this gate never does either on an install" ;;
  esac
  # 사람마다 상한 1 은 로컬과 같다. 노드가 작업 공간 하나만 담으면 설치 값 파일이 클러스터 상한도 준다
  WINDOW_ARGS=(DAEGAM_ALLOW_STUB_PROVIDER=true DAEGAM_SANDBOX_PER_PERSON=1 DAEGAM_SANDBOX_SWEEP_SECONDS="$SWEEP_SECONDS")
  [ -z "$CAPACITY_CAP" ] || WINDOW_ARGS+=(DAEGAM_SANDBOX_MAX="$CAPACITY_CAP")
  config_window_plan "${WINDOW_ARGS[@]}" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  WINDOW="stub providers, one sandbox per person${CAPACITY_CAP:+, at most $CAPACITY_CAP on the cluster}, a ${SWEEP_SECONDS}s sweep"
else
  OVERRIDE_BEFORE="$(platform_config DAEGAM_SANDBOX_ALLOW_UNCHECKED)" || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  config_window_plan DAEGAM_SANDBOX_ALLOW_UNCHECKED=false DAEGAM_SANDBOX_WORKSPACE="$LOCAL_WORKSPACE" \
    DAEGAM_SANDBOX_NODE_RESERVE="$LOCAL_RESERVE" DAEGAM_SANDBOX_PER_PERSON=1 DAEGAM_SANDBOX_SWEEP_SECONDS="$SWEEP_SECONDS" \
    || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  WINDOW="the override off, a $((LOCAL_WORKSPACE / 1024 / 1024)) MiB workspace, reserve $LOCAL_RESERVE, one sandbox per person, a ${SWEEP_SECONDS}s sweep"
fi
CONFIG_CHANGED=1
echo ">> ConfigMap $AUTH_MODE_CONFIGMAP for this gate: $WINDOW (restored on exit). If this run dies, $CONFIG_RESTORE_HINT"
if [ "$TARGET" = local ]; then
  make_room || die 0 "scaling daegam-platform and daegam-docparse to one replica failed"
fi
config_window_apply || die 0 "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
roll_platform || die 0 "the platform did not come back after the ConfigMap change"
pass 0 "platform at $BASE; no leftover Agent or open sandbox$([ "$TARGET" = install ] && echo "; the admin's verdict holds and is accepted"); ConfigMap window: $WINDOW; platform rolled$([ "$TARGET" = local ] && echo "; daegam-platform and daegam-docparse at one replica")"

# ---------- 1. 클러스터, 용량, provider, Database ----------
code="$(api POST "/clusters/$HOME_ID/preflight")"; [ "$code" = 200 ] || die 1 "POST preflight -> $code"
code="$(api GET "/clusters/$HOME_ID")"; [ "$code" = 200 ] || die 1 "GET /clusters/$HOME_ID -> $code"
[ "$(jq_ 'd["status"]["preflight"]["ok"] and d["gates"]["sandbox_rights"]')" = True ] \
  || die 1 "home preflight or sandbox rights missing: preflight ok $(jq_ 'd["status"]["preflight"]["ok"]'), gates $(jq_ 'd["gates"]')"
POLICY_CONTROLLER="$(jq_ 'd["status"]["preflight"].get("policy_controller")')"
sandbox_status 1
CAPACITY="$(jq_ 'd["capacity"]')"
NODES="$(jq_ '[(n["name"], n["cap"], n["allocatable"] >> 20, n["others"] >> 20) for n in d["nodes"]]')"
[ "$CAPACITY" -ge 1 ] || die 1 "the cluster holds no sandbox (capacity 0; nodes name, cap, allocatable MiB, others MiB: $NODES)"
ensure_stub_provider 1 "$PROVIDER"
if [ "$TARGET" = install ]; then
  install_postgres_database 1 "$DS_NAME" "$PLATFORM_NS" "$HOME_ID"
else
  lab_database 1 postgres "$DS_NAME" "$DS_DISPLAY"
fi
DS_ID="$DATA_SERVICE_ID"
pass 1 "home $HOME_ID preflight ok, sandbox rights, policy controller $POLICY_CONTROLLER; capacity $CAPACITY (nodes $NODES); provider $PROVIDER, Database $DS_NAME"

# ---------- 2. 격리 점검과 허용 ----------
code="$(api GET /agents/templates)"; [ "$code" = 200 ] || die 2 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
agent_body_to "$WORK/agent.json" --code-execution || die 2 "agent body"
save_doc 2 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
HOME_DEPLOY="{\"cluster_id\": \"$HOME_ID\"}"
if [ "$TARGET" = install ]; then
  # 관리자의 판정을 STEP 0 에서 읽었다. 그 뒤 바뀌지 않았는지 다시 읽는다
  code="$(API_OUT="$WORK/cluster.json" api GET "/clusters/$HOME_ID")"; [ "$code" = 200 ] || die 2 "GET /clusters/$HOME_ID -> $code"
  [ "$(json_of "$WORK/cluster.json" '(lambda s: s["check"]["passed"] and s.get("void_reason") is None)(d["sandbox"])')" = True ] \
    || die 2 "the admin's verdict no longer holds: $(json_of "$WORK/cluster.json" 'd["sandbox"]')"
  VERDICT="$(json_of "$WORK/cluster.json" '(lambda c: (c["userns"], c["network"], len(c["targets"]), c["shared_nodes"], c["checked_at"], c["accepted_by"]))(d["sandbox"]["check"])')"
  ACCEPT_NOTE="the platform admin's verdict (userns, network, kept targets, shared nodes, checked at, accepted by: $VERDICT); the gate neither checks nor accepts on an install"
else
  RUN_BEFORE="$(api GET "/clusters/$HOME_ID" >/dev/null; jq_ 'd["sandbox"]["run"].get("finished_at")')"
  code="$(api POST "/clusters/$HOME_ID/sandbox/check")"
  [ "$code" = 202 ] || die 2 "POST sandbox/check -> $code"
  code="$(api POST "/clusters/$HOME_ID/sandbox/check")"
  { [ "$code" = 409 ] && [ "$(jq_ 'd["detail"]["code"]')" = sandbox_check_running ]; } \
    || die 2 "a second check while one runs -> $code (want 409 sandbox_check_running)"
  echo ">> waiting up to ${CHECK_WAIT}s for the isolation check of home"
  for ((i = 0; i < CHECK_WAIT; i += 5)); do
    sleep 5
    sandbox_status 2
    [ "$(jq_ 'd["check_running"]')" = False ] || continue
    api GET "/clusters/$HOME_ID" >/dev/null
    [ "$(jq_ 'd["sandbox"]["run"].get("finished_at")')" != "$RUN_BEFORE" ] && break
  done
  code="$(API_OUT="$WORK/cluster.json" api GET "/clusters/$HOME_ID")"; [ "$code" = 200 ] || die 2 "GET /clusters/$HOME_ID -> $code"
  [ "$(json_of "$WORK/cluster.json" 'd["sandbox"]["run"].get("finished_at")')" != "$RUN_BEFORE" ] \
    || die 2 "the isolation check did not finish within ${CHECK_WAIT}s"
  RUN_ERROR="$(json_of "$WORK/cluster.json" 'd["sandbox"]["run"].get("error")')"
  [ "$RUN_ERROR" = None ] || die 2 "the isolation check could not run: $RUN_ERROR"
  VERDICT="$(json_of "$WORK/cluster.json" '(lambda c: (c["passed"], c["userns"], c["network"], len(c["targets"]), c["shared_nodes"], c["capacity"], bool(c["image_id"])))(d["sandbox"]["check"])')"
  case "$VERDICT" in
    "True True True "*" True") ;;
    *) die 2 "verdict passed, userns, network, kept targets, shared_nodes, capacity, image_id: $VERDICT" "$(cat "$WORK/cluster.json")" ;;
  esac
  CHECKED_AT="$(json_of "$WORK/cluster.json" 'd["sandbox"]["check"]["checked_at"]')"
  if [ "$(json_of "$WORK/cluster.json" 'd["sandbox"]["check"]["shared_nodes"]')" = True ]; then
    sandbox_status 2
    [ "$(jq_ 'd["gate"]')" = "{'open': False, 'reason': 'unaccepted'}" ] || die 2 "gate before acceptance: $(jq_ 'd["gate"]') (want unaccepted)"
    code="$(api POST "/agents/$AG_ID/deploy" "$HOME_DEPLOY")"
    { [ "$code" = 409 ] && [ "$(jq_ '(d["detail"]["code"], d["detail"].get("reason"))')" = "sandbox_gate_closed unaccepted" ]; } \
      || die 2 "deploying a code-execution Agent before acceptance -> $code (want 409 sandbox_gate_closed unaccepted)"
    STALE="$(python3 -c 'import sys; from datetime import datetime, timedelta
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print((t - timedelta(seconds=1)).isoformat())' "$CHECKED_AT")"
    code="$(api POST "/clusters/$HOME_ID/sandbox/accept" "{\"checked_at\": \"$STALE\"}")"
    { [ "$code" = 409 ] && [ "$(jq_ 'd["detail"]["code"]')" = sandbox_check_stale ]; } \
      || die 2 "accepting with an older checked_at -> $code (want 409 sandbox_check_stale)"
    # 로컬 클러스터는 버리는 클러스터라 허용을 이 게이트가 쓴다. 설치본에서는 쓰지 않는다
    code="$(api POST "/clusters/$HOME_ID/sandbox/accept" "{\"checked_at\": \"$CHECKED_AT\"}")"
    { [ "$code" = 200 ] && [ "$(jq_ 'd["sandbox"]["check"]["accepted_at"] is not None')" = True ]; } \
      || die 2 "accepting the shared nodes -> $code"
    ACCEPT_NOTE="isolation check passed ($VERDICT); a second start 409 sandbox_check_running; shared nodes: gate unaccepted, deploy 409 sandbox_gate_closed (unaccepted), a stale accept 409 sandbox_check_stale, accepted"
  else
    ACCEPT_NOTE="isolation check passed ($VERDICT); a second start 409 sandbox_check_running; sandboxes have nodes of their own, no acceptance needed"
  fi
fi
wait_gate_open 2
pass 2 "$ACCEPT_NOTE; policy controller $POLICY_CONTROLLER; gate open"

# ---------- 3. 코드 실행 Agent ----------
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_NAME ($AG_ID) phase=Ready"
deploy_ready 3 /agents "$AG_ID" "$HOME_DEPLOY"
READY_AT=$SECONDS
require_features 3 code_execution
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"
"${K[@]}" -n "$AG_NS" get deploy "$AG_K8S" -o json >"$WORK/deploy.json" || die 3 "no deploy/$AG_K8S in $AG_NS"
deployment_env "$WORK/deploy.json" agent | grep -q '^DAEGAM_SANDBOX_URL=.' || die 3 "the agent container has no DAEGAM_SANDBOX_URL"
# 죽은 앞 실행이 남긴 정책과 client 파드
"${K[@]}" -n "$AG_NS" delete networkpolicy "$POLICY_NAME" --ignore-not-found >/dev/null
"${K[@]}" -n "$AG_NS" delete pod "$CLIENT_POD" --ignore-not-found >/dev/null
pass 3 "agent $AG_NAME=$AG_ID Ready in $AG_NS; features $FEATURES; DAEGAM_SANDBOX_URL in the agent container"

# ---------- 4. xlsx 를 읽고 PNG 를 쓴다 ----------
chat_samples samples "$WORK/samples" "$MARK" >"$WORK/sample-names" || die 4 "making the sample files failed"
XLSX="$(grep '\.xlsx$' "$WORK/sample-names" | head -n 1)"
[ -n "$XLSX" ] || die 4 "no xlsx sample in $(tr '\n' ' ' <"$WORK/sample-names")"
upload_ok 4 "$AG_ID" "$WORK/samples/$XLSX" "d['kind'] == 'document' and d['format'] == 'xlsx'"
cat >"$WORK/chart.py" <<'PY'
import json, sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openpyxl

path, mark = sys.argv[1], sys.argv[2]
book = openpyxl.load_workbook(path, read_only=True)
cells = [str(v) for ws in book.worksheets for row in ws.iter_rows(values_only=True) for v in row if v is not None]
plt.bar(list(range(len(book.sheetnames))), [1] * len(book.sheetnames))
plt.savefig("/workspace/chart.png")
print("CHART " + json.dumps({"sheets": len(book.sheetnames), "mark": any(mark in c for c in cells)}))
PY
say 4 "" "$(py_frag "$WORK/chart.py" "/workspace/uploads/$XLSX" "$MARK")" "$UPLOAD_ID"
S1="$TURN_SID"
has_text "$TOOL_OUT" 'CHART \{"sheets": 2, "mark": true\}' || die 4 "the xlsx was not read by code: $TOOL_STATUS $TOOL_OUT"
open_record 4 "$S1"
[ "$(rec_ '(d["unchecked"], d["generation"])')" = "False 1" ] || die 4 "record: $(cat "$WORK/rec.json") (want a checked first generation)"
"${K[@]}" -n "$AG_NS" get pod "$SBX_POD" -o json >"$WORK/sbx.json" || die 4 "no pod $AG_NS/$SBX_POD"
POD_OFF="$(python3 - "$WORK/sbx.json" "$AG_ID" "$AG_K8S" <<'PY'
import json, sys
pod, agent_id, deploy = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
spec, labels = pod["spec"], pod["metadata"]["labels"]
terms = (((spec.get("affinity") or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {}).get("nodeSelectorTerms") or []
owners = [(o["kind"], o["name"]) for o in pod["metadata"].get("ownerReferences") or []]
want = {
    "hostUsers": (spec.get("hostUsers"), False),
    "automountServiceAccountToken": (spec.get("automountServiceAccountToken"), False),
    "priorityClassName": (spec.get("priorityClassName"), "daegam-sandbox"),
    "resource": (labels.get("daegam.devops.ai.kr/resource"), "sandbox"),
    "agent": (labels.get("daegam.devops.ai.kr/agent"), agent_id),
    "platform-id": (bool(labels.get("daegam.devops.ai.kr/platform-id")), True),
    "pinned": (len(terms) == 1 and len(terms[0].get("matchExpressions") or []) == 1, True),
    "owner": (owners, [("Deployment", deploy)]),
}
print(json.dumps({k: v[0] for k, v in want.items() if v[0] != v[1]}) if any(v[0] != v[1] for v in want.values()) else "")
PY
)"
[ -z "$POD_OFF" ] || die 4 "sandbox pod $SBX_POD differs: $POD_OFF"
FILES_PATH="/agents/$AG_ID/sessions/$S1/files"
code="$(api GET "$FILES_PATH")"; [ "$code" = 200 ] || die 4 "GET $FILES_PATH -> $code"
ROWS="$(jq_ '[(r["path"], r["origin"]) for r in d["files"] if r["path"].startswith("/workspace/")]')"
[ "$ROWS" = "[('/workspace/chart.png', 'workspace')]" ] || die 4 "workspace rows in the session files: $ROWS (want chart.png alone, no uploads)"
code="$(download "$FILES_PATH/workspace/chart.png")"
[ "$code" = 200 ] || die 4 "GET $FILES_PATH/workspace/chart.png -> $code"
OFF="$(download_headers_off application/octet-stream chart.png)"
[ -z "$OFF" ] || die 4 "chart.png headers differ from the sample: $OFF"
[ "$(head -c 8 "$WORK/download" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ] || die 4 "chart.png is not a PNG"
pass 4 "session $S1: code read $XLSX (2 sheets, mark found) and wrote chart.png, listed as workspace and downloaded with the five D61 headers; pod $SBX_POD hostUsers false, pinned, daegam-sandbox, no SA token, owned by $AG_K8S"

# ---------- 5. 막힌 네트워크와 8080 ----------
SBX_IP="$(json_of "$WORK/sbx.json" 'd["status"]["podIP"]')"
# 판정의 대상이다. 양성 대조가 닿은 것(거절된 연결은 막을 것이 없는 닫힌 포트라 남기지 않는다)과 클러스터 DNS·API 다
TARGETS="$(json_of "$WORK/cluster.json" 'json.dumps(d["sandbox"]["check"]["targets"])')"
[ "$(json_of - 'len(d)' <<<"$TARGETS")" -ge 1 ] || die 5 "the verdict kept no targets"
cat >"$WORK/reach.py" <<'PY'
import json, os, socket, struct, sys
from concurrent.futures import ThreadPoolExecutor

# 서버의 자체 점검과 같은 꼴이다(sandbox/daegam_sandbox/probes.py): tcp 는 연결, dns 는 UDP 질의 하나의 답,
# icmp 는 에코 하나의 답이 오면 닿은 것이다
QUERY = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack("!HH", 1, 1)


def echo(v6):
    packet = struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, os.getpid() & 0xFFFF, 1) + b"e2e"
    if v6:
        return packet
    data = packet + b"\0" * (len(packet) % 2)
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total = ~(total + (total >> 16)) & 0xFFFF
    return packet[:2] + struct.pack("!H", total) + packet[4:]


def reach(target):
    kind, host, port = target["kind"], target["host"], target.get("port")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        if kind == "tcp":
            socket.create_connection((host, port), timeout=2).close()
            return "reached"
        proto = 0 if kind == "dns" else (socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP)
        try:
            s = socket.socket(family, socket.SOCK_DGRAM, proto)
        except OSError as e:
            return "blocked no socket " + type(e).__name__
        s.settimeout(2)
        s.connect((host, port if kind == "dns" else 0))
        s.send(QUERY if kind == "dns" else echo(family == socket.AF_INET6))
        s.recv(512)
        return "reached"
    except OSError as e:
        return "blocked " + type(e).__name__


targets = json.loads(sys.argv[1])
with ThreadPoolExecutor(16) as pool:
    results = list(pool.map(reach, targets))
out = {f'{t["kind"]}:{t["host"]}:{t.get("port")} ({t.get("role")})': r for t, r in zip(targets, results)}
print("REACH " + json.dumps(out, sort_keys=True))
PY
say 5 "$S1" "$(py_frag "$WORK/reach.py" "$TARGETS")"
REACH="$(printf '%s\n' "$TOOL_OUT" | sed -n 's/^REACH //p')"
[ -n "$REACH" ] || die 5 "the reach script printed nothing: $TOOL_STATUS $TOOL_OUT"
REACHED="$(json_of - '[k for k, v in d.items() if not v.startswith("blocked")]' <<<"$REACH")"
[ "$REACHED" = "[]" ] || die 5 "user code reached $REACHED ($REACH)"
[ "$(json_of - 'len(d)' <<<"$REACH")" = "$(json_of - 'len(d)' <<<"$TARGETS")" ] \
  || die 5 "the reach script answered $(json_of - 'len(d)' <<<"$REACH") of $(json_of - 'len(d)' <<<"$TARGETS") targets: $REACH"
CONNECT='import socket, sys
try:
    socket.create_connection((sys.argv[1], 8080), timeout=3).close()
    print("reached")
except (ConnectionRefusedError, TimeoutError, socket.timeout) as e:
    print("blocked " + type(e).__name__)
except OSError as e:
    print("error " + type(e).__name__)'
FROM_AGENT="$("${K[@]}" -n "$AG_NS" exec "$(agent_pod)" -c agent -- python -c "$CONNECT" "$SBX_IP" 2>"$WORK/exec.err" || true)"
[ "$FROM_AGENT" = reached ] || die 5 "the Agent pod could not reach the sandbox on 8080: $FROM_AGENT $(cat "$WORK/exec.err")"
FROM_PLATFORM="$("${K[@]}" -n "$PLATFORM_NS" exec deploy/daegam-platform -c platform -- python -c "$CONNECT" "$SBX_IP" 2>"$WORK/exec.err" || true)"
case "$FROM_PLATFORM" in blocked*) ;; *) die 5 "8080 from the platform pod: $FROM_PLATFORM $(cat "$WORK/exec.err") (want blocked)" ;; esac
# Agent 가 아닌 파드: 샌드박스 이미지의 client 모드다. 3 이 막힘, 0 이 열림, 그 밖은 시도하지 못한 것이다(점검과 같은 규칙)
python3 - "$WORK/sbx.json" "$CLIENT_POD" "$SBX_IP" >"$WORK/client.json" <<'PY'
import json, sys
sbx, name, ip = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
box = sbx["spec"]["containers"][0]
spec = {
    "restartPolicy": "Never",
    "automountServiceAccountToken": False,
    "activeDeadlineSeconds": 120,
    "securityContext": sbx["spec"].get("securityContext") or {},
    "containers": [{
        "name": "client",
        "image": box["image"],
        "imagePullPolicy": box.get("imagePullPolicy", "IfNotPresent"),
        "args": ["client", ip, "8080"],
        "securityContext": box.get("securityContext") or {},
        "resources": {"requests": {"cpu": "10m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
    }],
}
if sbx["spec"].get("imagePullSecrets"):
    spec["imagePullSecrets"] = sbx["spec"]["imagePullSecrets"]
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "labels": {"app": name}}, "spec": spec}))
PY
CLIENT_PLACED=1
"${K[@]}" -n "$AG_NS" apply -f "$WORK/client.json" >/dev/null || die 5 "creating pod $CLIENT_POD failed"
CLIENT_EXIT=""
for ((i = 0; i < 120; i += 2)); do
  CLIENT_EXIT="$("${K[@]}" -n "$AG_NS" get pod "$CLIENT_POD" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || true)"
  [ -z "$CLIENT_EXIT" ] || break
  sleep 2
done
"${K[@]}" -n "$AG_NS" delete pod "$CLIENT_POD" --ignore-not-found --wait=false >/dev/null || true
CLIENT_PLACED=0
case "$CLIENT_EXIT" in
  3) ;;
  0) die 5 "a non-Agent pod reached the sandbox on 8080" ;;
  *) die 5 "the client pod could not try 8080 (exit '${CLIENT_EXIT:-none}')" ;;
esac
pass 5 "user code reached none of $(json_of - 'len(d)' <<<"$REACH") targets ($REACH); 8080 open to the Agent pod, blocked from the platform pod ($FROM_PLATFORM) and a non-Agent pod (client exit 3)"

# ---------- 6. 열면 코드가 돌 수 있는 파일의 거절 ----------
cat >"$WORK/files.py" <<'PY'
import shutil, zipfile

shutil.copyfile("/bin/true", "/workspace/tool.bin")
with zipfile.ZipFile("/workspace/bundle.zip", "w") as z:
    z.writestr("setup.exe", b"MZ" + bytes(62))
with open("/workspace/run.sh", "w") as f:
    f.write("#!/bin/sh\necho hi\n")
print("FILES ok")
PY
say 6 "$S1" "$(py_frag "$WORK/files.py")"
has_text "$TOOL_OUT" 'FILES ok' || die 6 "writing the files failed: $TOOL_STATUS $TOOL_OUT"
for name in tool.bin bundle.zip; do
  code="$(download "$FILES_PATH/workspace/$name")"
  { [ "$code" = 403 ] && [ "$(jq_ 'd["detail"]["code"]')" = file_refused_executable ]; } \
    || die 6 "GET .../workspace/$name -> $code $(head -c 300 "$WORK/body") (want 403 file_refused_executable)"
done
code="$(download "$FILES_PATH/workspace/run.sh")"
{ [ "$code" = 200 ] && grep -q 'echo hi' "$WORK/download"; } || die 6 "GET .../workspace/run.sh -> $code (want 200 and the script)"
pass 6 "tool.bin (ELF) and bundle.zip (holds setup.exe) -> 403 file_refused_executable; run.sh downloads"

# ---------- 7. 멈춤과 시한 ----------
cat >"$WORK/procs.py" <<'PY'
import json, os, time

time.sleep(1)
seen = []
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                seen.append(f.read().replace(b"\0", b" ").decode(errors="replace").strip())
        except OSError:
            pass
print("PROCS " + json.dumps(sorted(c for c in seen if c)))
PY
chat_bg "/agents/$AG_ID/chat" "$(msg "$(exec_frag 'sleep 600' 300)" "$S1")"
for ((i = 0; i < 40; i++)); do
  [ "$(sse_ '[d["name"] for e, d in events if e == "tool_call"]' "$WORK/bg.sse")" = "['execute']" ] && break
  sleep 0.5
done
[ "$(sse_ '[d["name"] for e, d in events if e == "tool_call"]' "$WORK/bg.sse")" = "['execute']" ] \
  || die 7 "the sleep 600 turn did not call execute: $(sse_ 'names' "$WORK/bg.sse")"
sleep 3
CANCEL_MS="$(now_ms)"
code="$(api POST "/agents/$AG_ID/sessions/$S1/cancel")"
[ "$code" = 202 ] || die 7 "POST /sessions/$S1/cancel -> $code"
expect_cancelled_stream 7 "$CANCEL_MS"
say 7 "$S1" "$(py_frag "$WORK/procs.py")"
FREED_MS=$(($(now_ms) - CANCEL_MS))
PROCS="$(printf '%s\n' "$TOOL_OUT" | sed -n 's/^PROCS //p')"
[ -n "$PROCS" ] || die 7 "the process list printed nothing: $TOOL_STATUS $TOOL_OUT"
case "$PROCS" in *"sleep 600"*) die 7 "sleep 600 still runs after the stop: $PROCS" ;; esac
START_MS="$(now_ms)"
say 7 "$S1" "$(exec_frag 'sleep 30' 3)"
TOOK_MS=$(($(now_ms) - START_MS))
has_text "$TOOL_OUT" "$TIMED_TEXT" || die 7 "sleep 30 with timeout 3: $TOOL_STATUS $TOOL_OUT"
[ "$TOOK_MS" -lt 20000 ] || die 7 "the timed-out turn took $TOOK_MS ms"
pass 7 "stop during sleep 600: the stream ended with turn_cancelled in ${ENDED_MS}ms and the next command saw no sleep ${FREED_MS}ms after the stop; timeout 3 stopped sleep 30 (turn ${TOOK_MS}ms)"

# ---------- 8. 메모리 초과 ----------
say 8 "$S1" "$(exec_frag "echo $MARK > /workspace/keep.txt && echo written")"
has_text "$TOOL_OUT" written || die 8 "writing keep.txt failed: $TOOL_STATUS $TOOL_OUT"
cat >"$WORK/hog.py" <<'PY'
chunks = []
while True:
    chunks.append(b"\xff" * (64 << 20))
PY
say 8 "$S1" "$(py_frag "$WORK/hog.py")"
# 끊긴 호출의 런타임은 재시작 수가 늘 때까지 잠깐 다시 찾으므로(agent-runtime/runtime/sandbox.py _why_broken) kubelet 이
# 늦게 알려도 메모리 문장이다
{ has_text "$TOOL_OUT" "$OOM_TEXT" && has_text "$TOOL_OUT" "$KEPT_TEXT"; } \
  || die 8 "the memory hog: $TOOL_STATUS $TOOL_OUT (want the out-of-memory message that the files are kept)"
RESTART=""
for ((i = 0; i < 60; i += 3)); do
  "${K[@]}" -n "$AG_NS" get pod "$SBX_POD" -o json >"$WORK/sbx.json" || die 8 "no pod $AG_NS/$SBX_POD after the hog"
  RESTART="$(json_of "$WORK/sbx.json" '(lambda s: (s["restartCount"], ((s.get("lastState") or {}).get("terminated") or {}).get("reason")))(d["status"]["containerStatuses"][0])')"
  [ "$RESTART" = "0 None" ] || break
  sleep 3
done
# 컨테이너가 다시 뜨지 않았으면 커널이 명령 하나만 죽인 것이다(cgroup v1 이나 singleProcessOOMKill)
[ "$RESTART" = "1 OOMKilled" ] || die 8 "restart count and last reason: $RESTART (want 1 OOMKilled)"
KEPT=""
for ((i = 0; i < 6; i++)); do
  say 8 "$S1" "$(exec_frag 'cat /workspace/keep.txt')"
  if has_text "$TOOL_OUT" "$MARK"; then KEPT=1; break; fi
  has_text "$TOOL_OUT" "$RESTART_TEXTS" || die 8 "reading keep.txt after the restart: $TOOL_STATUS $TOOL_OUT"
  sleep 5
done
[ -n "$KEPT" ] || die 8 "the workspace was still restarting after 30 s"
pass 8 "the memory hog got the out-of-memory message (files kept); the container restarted once (OOMKilled) and keep.txt was kept"

# ---------- 9. 둘째 세션 ----------
# 둘이 함께 들어가는지는 Agent 파드가 선 뒤에 센다. 플랫폼의 노드·파드 목록은 60초 캐시라 그 뒤에 다시 읽는다
WAITED=$((LIST_CACHE_SECONDS - (SECONDS - READY_AT)))
[ "$WAITED" -le 0 ] || sleep "$WAITED"
sandbox_status 9
CAPACITY="$(jq_ 'd["capacity"]')"
if [ "$CAPACITY" -ge 2 ]; then CAN_RECLAIM=1; fi
if [ "$CAN_RECLAIM" = 1 ]; then
  age_record "$SBX_ID" 6 || die 9 "making the sandbox of $S1 look 6 minutes unused failed"
  say 9 "" "$(exec_frag 'echo opened')"
  S2="$TURN_SID"
  { [ "$TOOL_STATUS" = success ] && has_text "$TOOL_OUT" opened; } || die 9 "the second session's first call: $TOOL_STATUS $TOOL_OUT"
  closed_within 9 "$S1" 30
  [ "$CLOSED_REASON" = reclaimed ] || die 9 "the first session's sandbox closed as '$CLOSED_REASON' (want reclaimed)"
  pod_gone 9 "$SBX_POD"
  say 9 "$S1" "$(exec_frag 'true')"
  has_text "$TOOL_OUT" "$RECLAIM_TEXT" || die 9 "the first session's next call: $TOOL_STATUS $TOOL_OUT (want the reclaim message)"
  # 회수된 작업 공간의 파일은 함께 사라진다. 둘째 세션을 닫아 사람의 몫을 비운 뒤 첫 세션이 새 세대를 연다
  delete_session 9 "$S2"
  closed_within 9 "$S2" 60
  say 9 "$S1" "$(exec_frag 'ls -a /workspace /workspace/uploads')"
  ! has_text "$TOOL_OUT" 'could not start' || open_report "$S1"
  [ "$TOOL_STATUS" = success ] || die 9 "the first session's new workspace: $TOOL_STATUS $TOOL_OUT"
  case "$TOOL_OUT" in *keep.txt* | *chart.png*) die 9 "the files of the reclaimed workspace came back: $TOOL_OUT" ;; esac
  has_text "$TOOL_OUT" "$XLSX" || die 9 "the new workspace lacks the attachment $XLSX: $TOOL_OUT"
  open_record 9 "$S1"
  [ "$(rec_ 'd["generation"]')" = 2 ] || die 9 "the first session's new sandbox is generation $(rec_ 'd["generation"]') (want 2)"
  IDLE_SID="$S1"
  SECOND_NOTE="capacity $CAPACITY: the person's cap reclaimed $S1 (6 minutes unused) for $S2, its pod went away and its next call got the reclaim message; its generation 2 holds the attachment again and none of the files written before"
else
  say 9 "" "$(exec_frag 'echo opened')"
  S2="$TURN_SID"
  has_text "$TOOL_OUT" "$LIMIT_TEXTS" || die 9 "the second session while the first holds the only slot: $TOOL_STATUS $TOOL_OUT (want a limit message)"
  open_record 9 "$S1"
  delete_session 9 "$S1"
  closed_within 9 "$S1" 60
  pod_gone 9 "$SBX_POD"
  say 9 "$S2" "$(exec_frag 'echo opened')"
  { [ "$TOOL_STATUS" = success ] && has_text "$TOOL_OUT" opened; } || die 9 "the second session after the first closed: $TOOL_STATUS $TOOL_OUT"
  IDLE_SID="$S2"
  SECOND_NOTE="capacity $CAPACITY${CAPACITY_CAP:+ under DAEGAM_SANDBOX_MAX=$CAPACITY_CAP}: the second session was refused while $S1 held the only slot; deleting $S1 closed it ($CLOSED_REASON) and deleted its pod, and the second session opened"
fi
pass 9 "$SECOND_NOTE"

# ---------- 10. 유휴 회수 ----------
open_record 10 "$IDLE_SID"
age_record "$SBX_ID" 31 || die 10 "making the sandbox of $IDLE_SID look 31 minutes unused failed"
closed_within 10 "$IDLE_SID" "$SWEEP_WAIT"
[ "$CLOSED_REASON" = idle ] || die 10 "the sweep closed $IDLE_SID as '$CLOSED_REASON' (want idle)"
pod_gone 10 "$SBX_POD"
say 10 "$IDLE_SID" "$(exec_frag 'true')"
has_text "$TOOL_OUT" "$IDLE_TEXT" || die 10 "the next call after the idle close: $TOOL_STATUS $TOOL_OUT (want the idle message)"
# STEP 9 가 다른 세션은 이미 지웠다
delete_session 10 "$IDLE_SID"
pass 10 "the sweep closed $IDLE_SID as idle within ${SWEEP_WAIT}s and deleted its pod; the next call got the idle message"

# ---------- 11. 네임스페이스 정책의 거절, 세션을 지우면 파드도 ----------
python3 -c 'import json, sys
print(json.dumps({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": sys.argv[1]},
                  "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [{}], "egress": [{}]}}))' \
  "$POLICY_NAME" >"$WORK/policy.json"
POLICY_PLACED=1
"${K[@]}" -n "$AG_NS" apply -f "$WORK/policy.json" >/dev/null || die 11 "creating NetworkPolicy $POLICY_NAME failed"
say 11 "" "$(exec_frag 'echo opened')"
S3="$TURN_SID"
has_text "$TOOL_OUT" "network policy $POLICY_NAME" || die 11 "an open beside an allow-all policy: $TOOL_STATUS $TOOL_OUT (want the policy message)"
api GET "/clusters/$HOME_ID" >/dev/null
[ "$(jq_ '(d["sandbox"]["check"]["passed"], d["sandbox"]["check"]["voided_at"])')" = "True None" ] \
  || die 11 "the policy refusal touched the verdict: $(jq_ 'd["sandbox"]["check"]')"
"${K[@]}" -n "$AG_NS" delete networkpolicy "$POLICY_NAME" >/dev/null || die 11 "deleting NetworkPolicy $POLICY_NAME failed"
POLICY_PLACED=0
say 11 "$S3" "$(exec_frag 'echo opened')"
{ [ "$TOOL_STATUS" = success ] && has_text "$TOOL_OUT" opened; } || die 11 "the open after removing the policy: $TOOL_STATUS $TOOL_OUT"
open_record 11 "$S3"
delete_session 11 "$S3"
closed_within 11 "$S3" 60
pod_gone 11 "$SBX_POD"
pass 11 "an allow-all policy refused the open by name and left the verdict passed; without it the open worked; deleting $S3 closed its sandbox ($CLOSED_REASON) and deleted pod $SBX_POD"

# ---------- 12. 읽을 때의 무효와 개발용 우회 ----------
api GET "/clusters/$HOME_ID" >/dev/null
SCHED="$(jq_ 'json.dumps(d["sandbox"]["scheduling"])')"
if [ "$(json_of - '"kubernetes.io/os" in (d.get("node_selector") or {})' <<<"$SCHED")" = True ]; then
  cond_skip 12 "the sandbox placement already selects kubernetes.io/os, so adding it would not change the placement"
else
  # 무효로 읽히는 판정이면 스윕이 열린 작업 공간을 모두 verdict_void 로 닫는다. 남의 것이 열려 있으면 바꾸지 않는다
  sandbox_status 12
  OPEN_NOW="$(jq_ 'd["open_sandboxes"]')"
  [ "$OPEN_NOW" = 0 ] || die 12 "$OPEN_NOW sandboxes are open; voiding the verdict would make the sweep close them, so the placement stays"
  MOVED="$(json_of - 'json.dumps({"sandbox": {"scheduling": dict(d, node_selector={**(d.get("node_selector") or {}), "kubernetes.io/os": "linux"})}})' <<<"$SCHED")"
  SCHED_RESTORE="$(json_of - 'json.dumps({"sandbox": {"scheduling": d}})' <<<"$SCHED")"
  code="$(api PATCH "/clusters/$HOME_ID" "$MOVED")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["sandbox"]["void_reason"]')" = placement ]; } \
    || die 12 "moving the sandbox placement -> $code void_reason $(jq_ 'd["sandbox"].get("void_reason")') (want placement)"
  sandbox_status 12
  [ "$(jq_ 'd["gate"]')" = "{'open': False, 'reason': 'unchecked'}" ] || die 12 "gate after the placement change: $(jq_ 'd["gate"]')"
  say 12 "" "$(exec_frag 'echo opened')"
  S4="$TURN_SID"
  has_text "$TOOL_OUT" "$GATE_TEXT" || die 12 "an open under a void verdict: $TOOL_STATUS $TOOL_OUT (want the gate message)"
  OVERRIDE_NOTE=""
  if [ "$TARGET" = local ] && [ "$OVERRIDE_BEFORE" = true ]; then
    # 배포본의 값으로 되돌린다. 정리가 창의 다른 값과 함께 다시 되돌린다. 값은 ConfigMap 에서 읽은 것이다
    BACK="$(python3 -c 'import json, sys; print(json.dumps({"data": {"DAEGAM_SANDBOX_ALLOW_UNCHECKED": sys.argv[1]}}))' "$OVERRIDE_BEFORE")"
    "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p "$BACK" >/dev/null \
      || die 12 "putting the deploy's override back failed"
    roll_platform || die 12 "the platform did not come back with the override on"
    curl -fsS "$BASE/healthz" -o "$WORK/body" || die 12 "GET /healthz failed"
    [ "$(jq_ 'd.get("sandbox_unchecked")')" = True ] || die 12 "/healthz does not show the override: $(cat "$WORK/body")"
    api GET "/clusters/$HOME_ID" >/dev/null
    [ "$(jq_ '(d["sandbox"]["override"], d["sandbox"]["void_reason"])')" = "True placement" ] \
      || die 12 "the cluster under the override: $(jq_ '(d["sandbox"]["override"], d["sandbox"]["void_reason"])')"
    say 12 "$S4" "$(exec_frag 'echo opened')"
    { [ "$TOOL_STATUS" = success ] && has_text "$TOOL_OUT" opened; } || die 12 "an open under the override: $TOOL_STATUS $TOOL_OUT"
    open_record 12 "$S4"
    [ "$(rec_ 'd["unchecked"]')" = True ] || die 12 "the record opened under the override is not unchecked"
    "${K[@]}" -n "$AG_NS" get pod "$SBX_POD" -o json >"$WORK/sbx.json" || die 12 "no pod $AG_NS/$SBX_POD"
    [ "$(json_of "$WORK/sbx.json" '[e.get("value") for e in d["spec"]["containers"][0].get("env") or [] if e["name"] == "SANDBOX_UNCHECKED"]')" = "['true']" ] \
      || die 12 "the pod opened under the override has no SANDBOX_UNCHECKED=true"
    api GET "/agents/$AG_ID" >/dev/null
    [ "$(jq_ '"sandbox_override" in json.dumps((d.get("status") or {}).get("warnings"))')" = True ] \
      || die 12 "the Agent does not warn sandbox_override: $(jq_ '(d.get("status") or {}).get("warnings")')"
    delete_session 12 "$S4"
    closed_within 12 "$S4" 60
    OVERRIDE_NOTE="; with the override on, /healthz and the Cluster show it, the open was unchecked (pod env SANDBOX_UNCHECKED) and the Agent warns sandbox_override"
  else
    delete_session 12 "$S4"
    [ "$TARGET" = install ] || OVERRIDE_NOTE="; the override part did not run, since this local deploy does not turn it on"
  fi
  code="$(api PATCH "/clusters/$HOME_ID" "$SCHED_RESTORE")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["sandbox"]["void_reason"]')" = None ]; } \
    || die 12 "restoring the sandbox placement -> $code void_reason $(jq_ 'd["sandbox"].get("void_reason")')"
  SCHED_RESTORE=""
  wait_gate_open 12
  pass 12 "a placement change voided the verdict at read time (placement), the gate closed with unchecked and the open got the gate message$OVERRIDE_NOTE; the old placement restored the verdict"
fi

# ---------- 13. 서브에이전트의 execute 승인 ----------
agent_body_to "$WORK/agent.json" --code-approval || die 13 "agent body"
save_doc 13 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
code="$(api POST "/agents/$AG_ID/redeploy")"; [ "$code" = 200 ] || die 13 "redeploy with the execute approval -> $code"
wait_ready "/agents/$AG_ID" 13
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout=300s >/dev/null || die 13 "rollout of $AG_K8S"
wait_pods_settled "$AG_NS" "$AGENT_POD_SELECTOR$AG_ID" || die 13 "old pods of $AG_K8S did not go away"
wait_gate_open 13
code="$(chat "/agents/$AG_ID/chat" "$(msg "$(task_call "$SUB_NAME" "$(exec_frag 'echo sub')")")")"
[ "$code" = 200 ] || die 13 "the subagent turn -> $code"
asked execute rule || { cp "$WORK/sse" "$WORK/body"; die 13 "the subagent's execute did not stop at an approval: $(sse_ 'names')"; }
S5="$(sse_ 'first("interrupt")["session_id"]')"
pass 13 "with code_execution.approval the subagent's execute in session $S5 stopped at an approval card (execute, rule)"

# ---------- 14. 명령 도중 배포 내리기 ----------
code="$(chat "/agents/$AG_ID/chat" "$(msg "$(exec_frag 'sleep 120' 150)")")"
[ "$code" = 200 ] || die 14 "the main agent's execute turn -> $code"
asked execute rule || { cp "$WORK/sse" "$WORK/body"; die 14 "the main agent's execute did not ask: $(sse_ 'names')"; }
S6="$(sse_ 'first("interrupt")["session_id"]')"
chat_bg "/agents/$AG_ID/sessions/$S6/decisions" '{"decisions": [{"type": "approve"}]}'
READY=""
for ((i = 0; i < 150; i += 2)); do
  if sandbox_record "$S6" && [ "$(rec_ 'd["state"] == "open" and d.get("ready_at") is not None')" = True ]; then READY=1; break; fi
  # 턴이 이미 끝났으면 더 기다리지 않는다. 명령이 돌지 못하고 Tool 이 곧바로 답한 것이다
  case "$(sse_ 'names[-1]' "$WORK/bg.sse")" in done|error) break ;; esac
  sleep 2
done
# 거절의 까닭은 Tool 의 답과 그 세션의 마지막 기록(닫힌 까닭)에 있다. 정리가 플랫폼을 굴려 그 로그는 남지 않는다
[ -n "$READY" ] || die 14 "the approved command did not open a sandbox: $(sse_ 'names' "$WORK/bg.sse"); tool_result $(sse_ 'first("tool_result")["status"]' "$WORK/bg.sse"): $(sse_ 'inner(first("tool_result")["content"])[:600]' "$WORK/bg.sse"); last event $(sse_ 'events[-1][1]' "$WORK/bg.sse"); record: $(sandbox_record "$S6" && rec_ '{k: d.get(k) for k in ("state", "generation", "pod", "opened_at", "ready_at", "closed_at", "close_reason", "person")}' || echo none)"
SBX_POD="$(rec_ 'd["pod"]')"
sleep 3
code="$(api POST "/agents/$AG_ID/undeploy")"; [ "$code" = 200 ] || die 14 "undeploy during the command -> $code"
closed_within 14 "$S6" 60
pod_gone 14 "$SBX_POD"
bg_stop
pass 14 "undeploy during sleep 120 closed the sandbox ($CLOSED_REASON) and deleted pod $SBX_POD"

finish
