#!/usr/bin/env bash
# P36 (D61) 채팅 경험 e2e. 장기 기억, knowledge 노드, 서브에이전트 하나를 단 에이전트를 실제 클러스터에 띄우고 플랫폼 경로로
# 돈다. 묶음 A 단계는 세션 관리, 다시 생성과 메시지 수정, 인용, 피드백, 기억 파일, 세션 삭제를 본다. 묶음 B 단계는 같은
# 에이전트에 문서와 이미지를 업로드해 첨부 턴, 세션 파일 다운로드, 상한 오류, 문서 파싱 서비스(daegam-docparse)의 격리를 본다.
#
#   make e2e-chat                                   memory 노드가 lab Postgres 다
#   make e2e-chat-redis                             memory 노드가 lab Redis 8 이다(E2E_MEMORY_KIND=redis). 다시 생성을 여러 번
#                                                   한 긴 세션을 지운 뒤 그 세션의 키가 하나도 남지 않는지 본다
#   make e2e-chat E2E_LLM_PROVIDER=<provider>       e2e-agent 에서와 같은 뜻이다. 등록된 provider 의 실제 모델로 돌고, 스텁이
#                                                   메시지 안 JSON 조각으로 도구를 고르는 데 기대는 단계만 건너뛰거나 느슨하게 본다.
#                                                   4 뒤의 첫 세션과 첨부를 실은 세션(8 의 S4, 10 의 S5)은 바깥 내용을 가진
#                                                   대화라(P43) 모델이 거기서 Add 를 부르면 승인 대기로 멈춘다. 스텁 길은 그
#                                                   자리에서 Add 를 부르지 않는다
#   make e2e-chat E2E_TARGET=install E2E_INSTALL_USER=<관리자>
#                                                   설치본 갈래다. 설치본은 스텁 provider 를 막으므로
#                                                   (DAEGAM_ALLOW_STUB_PROVIDER 가 없거나 false 다) 모델 없이 되는 것만 본다
#
# 전제는 make deploy-local(header 모드, ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true)과 make lab-up(mock-soap, pgvector 가 있는
# Postgres, Redis 8)이다. Knowledge Base 의 임베딩은 실제 모델로 돌 때도 스텁이다. header 모드의 주체는 하나라 남의 기억이
# 보이지 않는지는 런타임 Service 에 port-forward 해 인스턴스 토큰과 다른 user_sub 로 직접 묻는다. B 단계의 표본 파일은
# deploy/dev/lab/chat_samples.py 가 플랫폼 venv 로 만든다.
#
# 단계(차례대로 돈다). 4g 는 6 뒤다. 4g 의 재배포가 옛 파드를 내리면 수집기가 아직 읽지 않은 그 파드의 관측 이벤트를 잃으므로,
# 그 이벤트에 기대는 5 의 superseded 를 먼저 본다. B 단계는 6 과 4g 사이에 돌고, 4g 의 리비전이 파일 도구를 끄므로 그것에
# 기대는 10f 가 4g 뒤에 돈다. 분당 업로드 상한(429 upload_rate_limited)은 API 테스트가 지킨다
# (platform/tests/test_chat_uploads.py 의 test_per_minute_limit_counts_removed_and_failed_uploads). 단계는 다음과 같다:
#   STEP 1a  플랫폼에 연결되고 header 모드이며 home 클러스터 preflight 가 ok. stub provider 를 등록한다(없을 때만, EXIT 트랩이 걷는다)
#   STEP 1b  lab Postgres 를 Database 로 등록해 verify(pgvector). redis 면 lab Redis 도 등록해 verify(체크포인터 ready)
#   STEP 1c  calculator MCP Server[Add] 배포 Ready, Knowledge Base(스텁 임베딩) 하나에 문서 faq.md 하나를 올려 인덱싱
#   STEP 1   템플릿에 MCP Server 도구, memory(장기 기억 30일), knowledge 노드(search_faq), 서브에이전트 calc(같은 모델과 도구)를
#            달아 저장·검증·배포 Ready. 상태의 runtime.features 에 cancel·session_admin·regenerate·citations·memory_files·
#            attachments·session_files
#   STEP 2   첫 턴의 done.turn_id 가 32자 hex 이고 목록 제목이 첫 메시지의 첫 줄이다. 이름을 바꾸면 목록이 따르고, q(대소문자
#            무시)가 그 세션만 찾으며 받은 q 를 되돌려준다. 맞는 것이 없는 q 는 빈 목록이고 공백뿐인 제목은 422 invalid_title
#   STEP 3   첫 턴에 좋아요를 남긴 뒤 다시 생성하면 턴 번호가 같고 turn_id 가 새것이며 전사가 그 턴 하나로 접힌다. 바뀐 턴의
#            평가는 404 turn_not_found 다. 메시지 수정(message)은 전사와 대화의 마지막 질문을 바꾼다
#   STEP 3s  (스텁) 새 세션에서 앞 턴 뒤 서브에이전트를 부른 턴을 다시 생성하고 메시지 수정까지 해도 앞 턴이 대화와 전사에 남는다
#   STEP 4   검색 턴의 tool_result 바로 뒤에 citations 가 오고 항목이 이 Knowledge Base 의 문서이며 결과의 cite 와 번호가 같다.
#            결과는 바깥 내용으로 표시된다(untrusted outside, P43)
#            인용 원문은 200 이고 헤더 다섯이 표본(schema/fixtures/chat/download-headers.json)과 같고 본문이 올린 파일이다.
#            인용되지 않은 번호는 404 citation_not_found 이고 원문 다운로드가 감사에 남는다. 실제 모델이 검색하지 않으면 조건부 SKIP
#   STEP 5   아쉬워요와 이유·의견 저장, 내 평가 목록, 승인자 목록, NDJSON 내보내기(헤더 다섯, 줄마다 daegam.feedback/1, 사람
#            식별자 없음)와 그 감사 행, feedback 이벤트에 사람과 의견이 없는 것, 3 에서 바뀐 턴의 평가가 superseded 가 되는 것
#   STEP 6   (스텁) 새 세션이 /memories/ 에 파일 둘을 쓰고 내 기억 목록·읽기·하나 삭제가 맞으며, 다른 사람의 user_sub 로는 런타임이
#            그 파일을 보이지 않는다. D34 의 모두 삭제 뒤 목록이 빈다. 실제 모델이면 목록·다른 사람·모두 삭제만 본다
#   STEP 8   (B) 상태의 chat 이 문서와 이미지를 받고 세션 첨부 상한이 memory 의 값(Postgres 8MiB, Redis 2MiB)이다. docx·pdf·
#            xlsx·hwpx·txt·png 를 업로드하면 201 이고 문서는 뽑은 사실(pdf 2쪽, xlsx 2시트, 글자 수)이 있다. 문서 다섯을 실은
#            턴의 done.attachments_received 가 5 이고 전사 사용자 항목에 다섯의 이름이 있으며, 스텁 read_file 이
#            /uploads/<docx>.txt 에서 실행 표지를 읽는다. 이미지를 실은 다음 턴은 attachments_received 1 이고, 스텁 write_file 로
#            /uploads/ 에 쓰면 permission denied 이며 답에 이미지 하나를 받았다는 스텁의 문장이 붙는다
#   STEP 9   (B) 스텁 write_file 이 /reports/summary.md 를 쓴 뒤 세션 파일 목록이 업로드 여섯(origin upload, 보낸 차례)과 그
#            에이전트 파일 하나(origin agent)이고 deepagents 내부 경로가 없다. 에이전트 파일과 업로드한 txt 의 다운로드는 200 에
#            헤더 다섯이 표본과 같고 본문이 쓴 글·업로드한 바이트다. 없는 파일은 404 file_not_found 다
#   STEP 10  (B) 10MiB 를 1바이트 넘는 문서는 413 too_large, 이미 보낸 업로드를 다시 실으면 422 unknown_upload, 세션 첨부 상한을
#            넘게 싣는 턴은 413 attachments_too_large 다(Postgres 는 한 턴이 들어간 다음 턴, Redis 는 첫 턴). 거절된 업로드는
#            남으므로 지운다
#   STEP 10d (B) daegam-docparse 파드에 SA 토큰이 없고 env 에 플랫폼 비밀이 없으며, kubectl exec 로 부른 selfcheck 의 다섯 칸이
#            모두 참이다(lib/e2e_chat_files.sh 의 docparse_pod)
#   STEP 4g  (E2E_SHARED_STEPS=skip 이면 Postgres 만) 출력 위치 pii_mask 가드레일을 더하고 파일 도구와 장기 기억을 끈 리비전을 재배포하면 같은 인용 원문이 403
#            source_masked 다. 인용이 없으면(실제 모델) 재배포만 하고 조건부 SKIP
#   STEP 10f (B. E2E_SHARED_STEPS=skip 이면 Postgres 만) 그 리비전의 상태 chat.documents 가 거짓이고 문서를 실은 턴은 422 attachments_need_filesystem 이다. 업로드는
#            남으므로 지운다(204)
#   STEP 7   이번 실행이 연 세션을 모두 지운다. redis 면 먼저 새 세션에 턴과 다시 생성을 더해 긴 세션으로 만든다(첫 세션은 4 의
#            바깥 내용을 가져 그 뒤의 Add 가 묻는다). 지운 뒤 세션
#            상세가 404 이고 목록·내 평가·승인자 목록에 없으며 저장소(Postgres 행, Redis 키)에 그 세션의 것이 첨부까지 하나도 없다
#   cleanup  Agent, Knowledge Base, MCP Server, Database 를 지우고 redis 면 이 Agent 의 키와 인덱스, 이번 실행이 연 세션마다의
#            쓰기 레지스트리(write_keys_zset)를 걷는다. STEP 1a 가 넣은 stub provider 를 걷는다. 보내지 않은 업로드는 1시간
#            TTL 이 지운다
#
# install 갈래(E2E_TARGET=install)는 공개 주소 https://<DAEGAM_HOST> 의 Ingress 를 지난다. 컨텍스트와 호스트는 설치 값 파일이
# 기본이고, 관리자는 e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이며(docs/guide/e2e.md 16절) 테넌트는 E2E_TENANT, 기본 e2e 다:
#   STEP k1  10d 와 같은 daegam-docparse 파드 점검
#   STEP k2  종류 openai 인 provider 를 가짜 키로 넣고 템플릿에서 도구와 memory 를 뺀 Agent 를 배포한다. 모델을 부르지 않으므로
#            Ready 가 되고, runtime.features 에 attachments 가 있으며 상태의 chat 이 문서를 받는다
#   STEP k3  크기가 정확히 10MiB 인 docx 가 Ingress 를 지나 업로드되고 daegam-docparse 가 뽑아 50만 자에서 자른다(201,
#            truncated). 업로드는 지운다
#   STEP k4  피드백 내보내기가 200 이고 헤더 다섯이 표본과 같다
#   cleanup  Agent 와 provider(플랫폼이 관리하던 키 Secret 포함)를 지운다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다. macOS 에는 timeout 이 없어 기다림은
# 모두 폴링이다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of·providers_with·providers_without 은 여기에 있다
. "$HERE/lib/e2e_json.sh"
# env_defaults 는 여기에 있다. install 갈래가 설치 값 파일을 읽는다
. "$HERE/lib/hosts.sh"
TARGET="${E2E_TARGET:-orbstack}"
API_PORT="${E2E_API_PORT:-$PORT_CHAT_API}"
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-chat}"; BASE="http://127.0.0.1:${API_PORT}" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"; BASE="https://${DAEGAM_HOST:-}" ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
RT_PORT="${E2E_RUNTIME_PORT:-$PORT_CHAT_RUNTIME}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"   # 설치본의 첫 Agent 배포가 이미지를 당기면 더 걸린다. 그때는 READY_TIMEOUT=600 으로 준다
MEMORY_KIND="${E2E_MEMORY_KIND:-postgres}"
LLM_PROVIDER="${E2E_LLM_PROVIDER:-}"
API="$BASE/api"
STUB_PROVIDER="stub-e2e"
BP_NAME="calc-chat"; MS_NAME="calc-chat-e2e"
PG_DS_NAME="pg-chat-e2e"; RDS_NAME="redis-chat-e2e"
KB_NAME="faq-chat-e2e"; KB_TOOL="search_faq"
AG_NAME="chat-e2e"; AG_DISPLAY="채팅 경험 e2e"
SUB_NAME="calc"                    # 서브에이전트 이름. 스텁이 task 의 subagent_type 으로 댄다
OTHER_SUB="someone-else-e2e"       # 6 이 런타임에 직접 묻는 다른 사람
# 상태의 runtime.features 에 있어야 하는 런타임 능력이다(agent-runtime/runtime/features.py)
CAPABILITIES="cancel session_admin regenerate citations memory_files attachments session_files"
TURN_ID_RE='^[0-9a-f]{32}$'        # turn_id 는 uuid4 hex 32자다(runtime/turn_record.py)
FEEDBACK_SCHEMA="daegam.feedback/1"
EVENTS_WAIT=150                    # 수집기가 파드 로그를 읽는 주기(60초)를 두 번 넘게 기다린다
LONG_TURNS=5; LONG_REGENS=2        # 7 이 redis 에서 새 세션(S6)에 쌓는 턴 수와 턴마다의 다시 생성 횟수
RUN_TAG="$(date +%H%M%S)"          # 제목 검색이 이전 실행의 세션을 집지 않게 제목에 붙인다
MARK="E2E-$RUN_TAG"                # B 의 표본 글에 넣어 뽑은 글과 다운로드한 파일에서 찾는 실행 표지다
# B 의 상한. 값의 출처는 platform/daegam/documents/formats.py(문서 바이트)와 agent-runtime/runtime/attachments.py(뽑은 글,
# 한 턴의 첨부 수, 세션 첨부 바이트)다
MIB=$((1024 * 1024))
MAX_DOCUMENT_BYTES=$((10 * MIB))
MAX_TEXT_CHARS=500000
MAX_PER_TURN=5
IMAGES_SEEN="이미지 1개를 받았습니다"   # 스텁이 모델 요청의 이미지 수를 적는 문장이다(agent-runtime/runtime/stub.py)
WRITE_DENIED="permission denied for write"   # deepagents 가 막은 쓰기의 도구 결과다
AGENT_FILE="/reports/summary.md"; AGENT_TEXT="# 요약 $MARK"   # 9 에서 스텁이 쓰는 에이전트 파일이다
INSTALL_PROVIDER="e2e-chat-install-openai"; INSTALL_AG_NAME="chat-install-e2e"   # install 갈래가 만들고 지운다
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-chat.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # header 모드라 Bearer 가 없다 (lib/e2e_api.sh 가 읽는다)
PF_API=""; PF_RT=""
HOME_ID=""; PG_DS_ID=""; RDS_ID=""; MEM_DS_ID=""; KB_ID=""; DOC_ID=""; AG_ID=""
AG_NS=""; AG_K8S=""; AG_TOKEN_SECRET=""; ME_SUB=""
S1=""; S2=""; S3=""                # 2·3s·6 이 연 세션. 7 이 지운다
S4=""; S5=""                       # 8 과 10 이 첨부를 실어 연 세션. 7 이 지운다
S6=""                              # 7 이 redis 에서 키우는 긴 세션. 7 이 지운다
INSTALL_PROVIDER_ADDED=0

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$RT_PORT"
# pass·skip·cond_skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·api_delete·wait_ready·save_doc·deploy_ready·secret_field·kb_create_fresh·kb_upload·kb_wait_indexed·
# ensure_stub_provider·remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab Database 등록(lab_database)과 lab 안을 들여다보는 psql_·rcli·rcount·rforget 은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# calc_mcp_server·calc_teardown 은 여기에 있다
. "$HERE/lib/e2e_calc.sh"
# 표본 만들기(chat_samples), 업로드(upload·upload_ok), 내려받기(download·download_headers_off), daegam-docparse 파드
# 점검(docparse_pod)은 여기에 있다
. "$HERE/lib/e2e_chat_files.sh"
# 내려가는 파드가 다 지워질 때까지 기다리는 wait_pods_settled 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# install 갈래의 관리자(install_admin)와 바꾸지 않는 로그인(local_login_kept)은 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"

cleanup() {
  # Agent 삭제가 그 Agent 의 장기 기억과 피드백을 걷는다. Knowledge Base 와 Database 는 Agent 가 붙들고 있으면 409 라
  # api_delete 가 기다렸다 다시 지운다
  [ -z "$AG_ID" ] || api_delete "/agents/$AG_ID"
  [ -z "$KB_ID" ] || api_delete "/knowledge-bases/$KB_ID"
  calc_teardown
  [ -z "$RDS_ID" ] || api_delete "/data-services/$RDS_ID"
  [ -z "$PG_DS_ID" ] || api_delete "/data-services/$PG_DS_ID"
  # Agent 삭제는 Redis 키를 지우지 않는다(D33). 세션마다의 쓰기 레지스트리는 인스턴스 접두어 밖이라 세션 id 로 걷는다
  [ "$MEMORY_KIND" != redis ] || [ -z "$AG_ID" ] || [ "$TARGET" = install ] \
    || rforget "$AG_ID" ${S1:+"$S1"} ${S2:+"$S2"} ${S3:+"$S3"} ${S4:+"$S4"} ${S5:+"$S5"} ${S6:+"$S6"}
  [ "$STUB_PROVIDER_ADDED" = 0 ] || remove_providers "$STUB_PROVIDER"
  [ "$INSTALL_PROVIDER_ADDED" = 0 ] || remove_providers "$INSTALL_PROVIDER"
  stop_pf "$PF_RT" "$RT_PORT"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# --- 실제 모델과 스텁의 갈래 ----------------------------------------------------------------------------------
real_model() { [ -n "$LLM_PROVIDER" ]; }
# say STUB_TEXT REAL_TEXT -> 스텁이면 앞의 것(도구 조각), 실제 모델이면 뒤의 것(사람의 말)
say() { if real_model; then echo "$2"; else echo "$1"; fi; }
# stub_only STEP WHY -> 스텁이면 0. 실제 모델이면 그 단계를 조건부 SKIP 으로 찍고 1 이다
stub_only() {
  real_model || return 0
  cond_skip "$1" "$2 (E2E_LLM_PROVIDER=$LLM_PROVIDER)"
  return 1
}
# delegate_call ADD_ARGS_JSON -> 서브에이전트 calc 에게 Add 를 맡기는 task 도구 조각. 서브에이전트의 스텁은 설명 안의 조각으로
# Add 를 부른다
delegate_call() { task_call "$SUB_NAME" "$(stub_call Add "$1")"; }

# --- 응답 읽기 -----------------------------------------------------------------------------------------------
urlq() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# turn_done STEP LABEL CODE -> chat 이 찍은 상태 코드 CODE 가 200 이고 받은 스트림($WORK/sse)이 done 으로 끝나야 한다. 아니면
# die STEP 이다
turn_done() {
  [ "$3" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die "$1" "$2 -> $3"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die "$1" "$2 did not end with done: $(sse_ 'names')"; }
}
# session_view SID EXPR [ARG...] -> 세션 상세에서 파이썬 식 EXPR 을 찍는다. users 는 전사의 사용자 글, dones 는 전사 done 의
# turn_id, kinds 는 전사 항목의 종류, humans 는 체크포인트 대화의 사람 글(200자 요약)이고 args 는 ARG 들이다. 여러 줄 글은
# ARG 로 넘긴다. 상세가 200 이 아니면 그 코드를 찍는다
session_view() {
  local sid="$1" expr="$2" code
  shift 2
  code="$(api GET "/agents/$AG_ID/sessions/$sid")"
  [ "$code" = 200 ] || { echo "GET session -> $code"; return 0; }
  python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
items, args = d["transcript"]["items"], sys.argv[3:]
kinds = [x["kind"] for x in items]
users = [x.get("text") for x in items if x["kind"] == "user"]
dones = [x.get("turn_id") for x in items if x["kind"] == "done"]
humans = [m.get("text") for m in d.get("messages") or [] if m.get("role") == "human"]
print(eval(sys.argv[2]))' "$WORK/body" "$expr" "$@"
}
# listed_sessions [QUERY] -> 내 세션 목록의 session_id 를 공백으로 이어 찍는다. 본문은 $WORK/body 다
listed_sessions() {
  local code
  code="$(api GET "/agents/$AG_ID/sessions${1:+?$1}")"
  [ "$code" = 200 ] || { echo "GET sessions -> $code"; return 0; }
  jq_ '" ".join(s["session_id"] for s in d["sessions"])'
}
# audited ACTION PATH -> 그 동작으로 PATH 를 부른 성공 감사 행이 있으면 True
audited() {
  api GET "/audit?tenant=$TENANT&action=$1&limit=50" >/dev/null
  jq_ "any(e.get('path') == '$2' and e.get('status') == 200 for e in d)"
}
# stored_rows SID -> 저장소에 남은 그 세션의 것(Postgres 는 체크포인트 세 표·전사·색인·첨부 행, Redis 는 체크포인트·쓰기
# 레지스트리·전사·색인 키와 첨부 해시)의 수
stored_rows() {
  local sid="$1" p="daegam-agent:$AG_ID:"
  if [ "$MEMORY_KIND" = redis ]; then
    echo $(($(rcount "${p}checkpoint*:$sid:*") + $(rcount "write_keys_zset:$sid:*") \
      + $(rcount "${p}transcript:$sid") + $(rcount "${p}session:$sid") + $(rcount "${p}upload:$sid")))
  else
    psql_ "select (select count(*) from checkpoints where thread_id = '$sid')
      + (select count(*) from checkpoint_writes where thread_id = '$sid')
      + (select count(*) from checkpoint_blobs where thread_id = '$sid')
      + (select count(*) from daegam_transcripts where session_id = '$sid')
      + (select count(*) from daegam_sessions where session_id = '$sid')
      + (select count(*) from daegam_uploads where session_id = '$sid')"
  fi
}
# rt GET PATH -> 런타임에 인스턴스 토큰으로 직접 부른 상태 코드. 본문은 $WORK/body 다(프록시 경로라 쿼리 user_sub 가 주체다)
rt() {
  curl -sS -o "$WORK/body" -w '%{http_code}' -X "$1" "http://127.0.0.1:${RT_PORT}$2" -H "Authorization: Bearer $RT_TOKEN" || true
}

# --- Agent 저장 본문 (STEP 1·4g) -------------------------------------------------------------------------------
# lib/agent_body.py 가 템플릿을 채운다. 도구 노드는 MCP Server 의 Add 하나(승인 없음)이고 서브에이전트 calc 에도 잇는다. memory
# 노드는 MEM_DS_ID 와 장기 기억 30일이고 knowledge 노드는 5건을 찾는다. SECOND 가 1 이면 4g 의 둘째 리비전이다. 출력 위치의
# pii_mask 가드레일을 더하고 파일 도구를 끈다. 장기 기억은 파일 도구로 읽고 쓰므로 함께 끈다(long_term_filesystem_disabled)
agent_body() { # SECOND -> $WORK/agent.json
  local revision=(--long-term-days 30)
  if [ "$1" = 1 ]; then revision=(--second); fi
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "$AG_DISPLAY" \
    --provider "${LLM_PROVIDER:-$STUB_PROVIDER}" --ms-id "$CALC_MS_ID" --ds-id "$MEM_DS_ID" --kb-id "$KB_ID" \
    --kb-tool "$KB_TOOL" --sub-name "$SUB_NAME" --sub-tools --top-k 5 "${revision[@]}"
}

# --- install 갈래 (E2E_TARGET=install) -----------------------------------------------------------------------------------
# python3 "$WORK/install_agent.py" TEMPLATES OUT NAME DISPLAY PROVIDER
# 템플릿에서 도구 노드와 memory 노드를 빼고 모델 노드에 PROVIDER 를 넣는다. memory 가 없으면 분당 한도를 둘 수 없으므로 끈다
# (memory_required). 모델을 부르지 않으므로 가짜 키로도 Ready 가 된다
cat >"$WORK/install_agent.py" <<'PY'
import json, sys

templates, out, name, display, provider = sys.argv[1:6]
template = json.load(open(templates))[0]
graph = template["graph"]
dropped = {n["id"] for n in graph["nodes"] if n["type"] in ("tools", "memory")}
graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in dropped]
graph["edges"] = [e for e in graph["edges"] if e["target"] not in dropped]
for n in graph["nodes"]:
    if n["type"] == "model":
        n["data"]["provider"] = provider
    elif n["type"] == "entry":
        n["data"]["rate_limit"] = None
layout = {k: v for k, v in template["layout"].items() if k not in dropped}
body = {"name": name, "display_name": display, "graph": graph, "layout": layout, "service": template["service"]}
json.dump(body, open(out, "w"), ensure_ascii=False)
PY

install_steps() {
  local code token provider started took
  [ -n "$CTX" ] || die k1 "INSTALL_CONTEXT 가 비었다 (설치 값 파일 또는 make e2e-chat E2E_TARGET=install INSTALL_CONTEXT=...)"
  [ "$BASE" != "https://" ] || die k1 "DAEGAM_HOST 가 비었다 (설치 값 파일)"
  install_admin k1
  token="$(local_login_kept "$USER_NAME" "$PASSWORD")" || die k1 "login as $USER_NAME at $BASE failed"
  unset PASSWORD
  # shellcheck disable=SC2034  # lib/e2e_api.sh·lib/e2e_sse.sh 가 읽는다
  AUTH=(-H "Authorization: Bearer $token")

  # ---------- k1. 문서 파싱 서비스 파드 ----------
  docparse_pod k1
  pass k1 "$DOCPARSE_NOTE"

  # ---------- k2. 가짜 키 provider 와 도구·memory 없는 Agent ----------
  code="$(api GET /clusters)"
  [ "$code" = 200 ] || die k2 "GET /clusters -> $code"
  HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home" and c["status"]["preflight"]["ok"]), "")')"
  [ -n "$HOME_ID" ] || die k2 "cluster home missing or preflight not ok"
  code="$(api GET /settings)"
  [ "$code" = 200 ] || die k2 "GET /settings -> $code"
  # 본문을 먼저 변수에 둔다. bash 3.2 는 겹친 명령 치환 안의 \" 로 감싼 {…, …} 를 중괄호 확장으로 쪼갠다
  provider="$(printf '{"name": "%s", "kind": "openai", "model": "gpt-4o-mini"}' "$INSTALL_PROVIDER")"
  code="$(api PATCH /settings "$(providers_with "$WORK/body" "$provider")")"
  [ "$code" = 200 ] || die k2 "PATCH /settings (provider $INSTALL_PROVIDER) -> $code"
  INSTALL_PROVIDER_ADDED=1
  put_provider_key k2 "$INSTALL_PROVIDER" "sk-e2e-chat-install-not-a-real-key"
  code="$(api GET /agents/templates)"
  [ "$code" = 200 ] || die k2 "GET /agents/templates -> $code"
  python3 "$WORK/install_agent.py" "$WORK/body" "$WORK/agent.json" "$INSTALL_AG_NAME" "채팅 첨부 설치본 e2e" "$INSTALL_PROVIDER"
  save_doc k2 /agents "$INSTALL_AG_NAME" "$(cat "$WORK/agent.json")"
  AG_ID="$SAVED_ID"
  deploy_ready k2 /agents "$AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  require_features k2 attachments
  [ "$(jq_ '((d.get("status") or {}).get("chat") or {}).get("documents")')" = True ] \
    || die k2 "status.chat does not take documents: $(jq_ '(d.get("status") or {}).get("chat")')"
  pass k2 "provider $INSTALL_PROVIDER (openai, a fake key through PUT .../api-key) and agent $INSTALL_AG_NAME id=$AG_ID Ready without tools or memory; runtime.features [$FEATURES]; status.chat takes documents"

  # ---------- k3. Ingress 를 지나는 10MiB 업로드와 추출 ----------
  chat_samples docx "$WORK/big-10mib.docx" "$MARK" "$MAX_DOCUMENT_BYTES" || die k3 "making the 10MiB docx failed"
  started=$SECONDS
  upload_ok k3 "$AG_ID" "$WORK/big-10mib.docx" \
    "d['format'] == 'docx' and d['bytes'] == $MAX_DOCUMENT_BYTES and d['truncated'] and d['text_chars'] == $MAX_TEXT_CHARS"
  took=$((SECONDS - started))
  code="$(api DELETE "/agents/$AG_ID/uploads/$UPLOAD_ID")"
  [ "$code" = 204 ] || die k3 "DELETE /agents/$AG_ID/uploads/$UPLOAD_ID -> $code"
  pass k3 "a $MAX_DOCUMENT_BYTES-byte docx went through $BASE in ${took}s and daegam-docparse cut its text at $MAX_TEXT_CHARS characters (truncated); the upload was removed"

  # ---------- k4. 피드백 내보내기의 다운로드 헤더 ----------
  code="$(download "/agents/$AG_ID/feedback/export")"
  [ "$code" = 200 ] || die k4 "GET /agents/$AG_ID/feedback/export -> $code"
  OFF="$(download_headers_off application/x-ndjson "feedback-$INSTALL_AG_NAME.ndjson")"
  [ -z "$OFF" ] || die k4 "export headers differ from the sample: $OFF"
  pass k4 "feedback export through $BASE: 200 with the five sample headers"
}

echo ">> target=$TARGET context=$CTX platform_ns=$PLATFORM_NS tenant=$TENANT api=$API memory_kind=$MEMORY_KIND llm_provider=${LLM_PROVIDER:-(stub)}"
if [ "$TARGET" = install ]; then
  install_steps
  finish
fi
# 세션에 쌓이는 첨부 바이트의 상한이다(agent-runtime/runtime/attachments.py). 파드 RAM 을 나눠 쓰는 Redis 는 작다
case "$MEMORY_KIND" in
  postgres) SESSION_CAP=$((8 * MIB)) ;;
  redis) SESSION_CAP=$((2 * MIB)) ;;
  *) die 1a "E2E_MEMORY_KIND must be postgres or redis (got $MEMORY_KIND)" ;;
esac

# ---------- 1a. 플랫폼과 provider ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 1a "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(api GET /clusters)"
[ "$code" = 200 ] || die 1a "GET /clusters without a token -> $code (header mode expected)"
HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home" and c["status"]["preflight"]["ok"]), "")')"
[ -n "$HOME_ID" ] || die 1a "cluster home missing or preflight not ok"
api GET /me >/dev/null
ME_SUB="$(jq_ 'd["sub"]')"
ensure_stub_provider 1a "$STUB_PROVIDER"
if real_model; then
  [ "$(jq_ "[p['name'] for p in d['llm_providers'] if p['name'] == '$LLM_PROVIDER']")" = "['$LLM_PROVIDER']" ] \
    || die 1a "E2E_LLM_PROVIDER=$LLM_PROVIDER is not a registered provider (make llm-provider-local first)"
fi
pass 1a "platform :$API_PORT header mode (sub $ME_SUB), home id=$HOME_ID; stub provider $STUB_PROVIDER (added this run: $STUB_PROVIDER_ADDED); chat model ${LLM_PROVIDER:-$STUB_PROVIDER}"

# ---------- 1b. Database ----------
lab_database 1b postgres "$PG_DS_NAME" "채팅 e2e DB"
PG_DS_ID="$DATA_SERVICE_ID"; MEM_DS_ID="$PG_DS_ID"
if [ "$MEMORY_KIND" = redis ]; then
  lab_database 1b redis "$RDS_NAME" "채팅 e2e 레디스"
  RDS_ID="$DATA_SERVICE_ID"; MEM_DS_ID="$RDS_ID"
fi
pass 1b "lab postgres $PG_DS_NAME id=$PG_DS_ID verify ok (pgvector)${RDS_ID:+; lab redis $RDS_NAME id=$RDS_ID verify ok (checkpointer ready)}; memory on $MEMORY_KIND"

# ---------- 1c. MCP Server 와 Knowledge Base ----------
calc_mcp_server 1c "$BP_NAME" "$MS_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
kb_body="$(python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "data_service_id": sys.argv[2], "embedding": {"provider": sys.argv[3]}}))' \
  "$KB_NAME" "$PG_DS_ID" "$STUB_PROVIDER")"
kb_create_fresh 1c "$KB_NAME" "$kb_body"
printf '%s\n' "Petstore inventory: getInventory returns the current inventory counts grouped by status (available, pending, sold)." \
  >"$WORK/faq.md"
code="$(kb_upload "$KB_ID" "$WORK/faq.md")"
[ "$code" = 201 ] || die 1c "POST /knowledge-bases/$KB_ID/documents -> $code"
DOC_ID="$(jq_ 'd["id"]')"
kb_wait_indexed 1c "$KB_ID" "$DOC_ID"
pass 1c "mcp server $MS_NAME id=$CALC_MS_ID Ready [Add]; knowledge base $KB_NAME id=$KB_ID document faq.md id=$DOC_ID indexed"

# ---------- 1. Agent 배포와 런타임 능력 ----------
code="$(api GET /agents/templates)"
[ "$code" = 200 ] || die 1 "GET /agents/templates -> $code"
cp "$WORK/body" "$WORK/templates.json"
agent_body 0
save_doc 1 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
code="$(api POST /agents/validate "$(json_of "$WORK/agent.json" 'json.dumps({"graph": d["graph"], "service": d["service"]})')")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["ok"]')" = True ]; } || die 1 "validate -> $code $(cat "$WORK/body")"
deploy_ready 1 /agents "$AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"; AG_TOKEN_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
# 기록은 init 받기가 쓴다. Ready 가 되었으면 이미 있다
# shellcheck disable=SC2086  # 이름 여럿을 낱말로 나눠 넘긴다
require_features 1 $CAPABILITIES
pass 1 "agent $AG_NAME id=$AG_ID Ready in $AG_NS (memory $MEMORY_KIND + long-term 30d, knowledge $KB_TOOL, subagent $SUB_NAME); runtime.features [$FEATURES]"

# ---------- 2. turn_id, 제목, 이름 바꾸기, 검색 ----------
TITLE_LINE="E2E 환불 기한 문의 $RUN_TAG"
FIRST="$TITLE_LINE
$(say "$(stub_call Add '{"a": 1, "b": 2}')" "한 문장으로 짧게 답해 줘.")"
turn_done 2 "first chat" "$(chat "/agents/$AG_ID/chat" "$(msg "$FIRST")")"
S1="$(sse_ 'first("done")["session_id"]')"; T1="$(sse_ 'first("done")["turn_id"]')"
[ "$(sse_ 'first("done")["turn"]')" = 1 ] && [[ "$T1" =~ $TURN_ID_RE ]] || die 2 "done: $(sse_ 'first("done")')"
listed_sessions >/dev/null
[ "$(jq_ "[s.get('title') for s in d['sessions'] if s['session_id'] == '$S1']")" = "['$TITLE_LINE']" ] \
  || die 2 "list title of $S1 is not the first line '$TITLE_LINE': $(cat "$WORK/body")"
RENAMED="E2E 이름 바꾼 대화 $RUN_TAG"
code="$(api PATCH "/agents/$AG_ID/sessions/$S1" "$(python3 -c 'import json,sys; print(json.dumps({"title": sys.argv[1]}, ensure_ascii=False))' "$RENAMED")")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["session"]["title"]')" = "$RENAMED" ]; } || die 2 "PATCH title -> $code $(cat "$WORK/body")"
code="$(api PATCH "/agents/$AG_ID/sessions/$S1" '{"title": "   "}')"
{ [ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = invalid_title ]; } || die 2 "a blank title -> $code $(cat "$WORK/body") (expected 422 invalid_title)"
Q="$(echo "$RENAMED" | tr '[:upper:]' '[:lower:]')"   # 대소문자를 가리지 않는다
[ "$(listed_sessions "q=$(urlq "$Q")")" = "$S1" ] || die 2 "q='$Q' did not find exactly $S1: $(cat "$WORK/body")"
[ "$(jq_ 'd.get("q")')" = "$Q" ] || die 2 "the runtime did not echo q: $(cat "$WORK/body")"
[ -z "$(listed_sessions "q=$(urlq "없는 제목 $RUN_TAG")")" ] || die 2 "a q with no match listed sessions: $(cat "$WORK/body")"
pass 2 "session $S1 turn 1 turn_id=$T1; title '$TITLE_LINE' -> renamed '$RENAMED' (blank 422 invalid_title); q '$Q' finds it alone and is echoed"

# ---------- 3. 다시 생성과 메시지 수정 ----------
# 다시 생성 전에 첫 답변을 평가한다. 다시 생성이 그 턴을 바꾸면 수집기 훅이 superseded 를 단다(5 가 본다)
code="$(api PUT "/agents/$AG_ID/sessions/$S1/turns/$T1/feedback" '{"rating": "up"}')"
[ "$code" = 200 ] || die 3 "PUT feedback on $T1 -> $code $(cat "$WORK/body")"
turn_done 3 "regenerate" "$(chat "/agents/$AG_ID/sessions/$S1/regenerate" '{}')"
T1B="$(sse_ 'first("done")["turn_id"]')"
{ [ "$(sse_ 'first("done")["turn"]')" = 1 ] && [[ "$T1B" =~ $TURN_ID_RE ]] && [ "$T1B" != "$T1" ]; } \
  || die 3 "regenerate done must keep turn 1 with a new turn_id: $(sse_ 'first("done")')"
[ "$(sse_ '"greeting" in names')" = False ] || die 3 "regenerate sent a greeting: $(sse_ 'names')"
# 다시 생성이 남긴 rewind 표식은 읽을 때 접혀 응답에 없다
[ "$(session_view "$S1" 'users == args[:1] and dones == args[1:] and "rewind" not in kinds' "$FIRST" "$T1B")" = True ] \
  || die 3 "the transcript did not fold to the regenerated turn: $(session_view "$S1" 'kinds, dones')"
code="$(api PUT "/agents/$AG_ID/sessions/$S1/turns/$T1/feedback" '{"rating": "down"}')"
{ [ "$code" = 404 ] && [ "$(jq_ 'd["detail"]["code"]')" = turn_not_found ]; } \
  || die 3 "feedback on the replaced turn $T1 -> $code $(cat "$WORK/body") (expected 404 turn_not_found)"
EDITED="$TITLE_LINE (고침)
$(say "$(stub_call Add '{"a": 2, "b": 2}')" "두 문장으로 답해 줘.")"
turn_done 3 "regenerate with a message" "$(chat "/agents/$AG_ID/sessions/$S1/regenerate" "$(msg "$EDITED")")"
T1C="$(sse_ 'first("done")["turn_id"]')"
{ [ "$(sse_ 'first("done")["turn"]')" = 1 ] && [ "$T1C" != "$T1B" ]; } || die 3 "edited done: $(sse_ 'first("done")')"
[ "$(session_view "$S1" 'users == args[:1] and humans[-1:] == users and dones == args[1:]' "$EDITED" "$T1C")" = True ] \
  || die 3 "the edited question did not replace the last one: $(session_view "$S1" 'users, humans, dones')"
pass 3 "regenerate kept turn 1 ($T1 -> $T1B, no greeting) and folded the transcript; feedback on $T1 -> 404 turn_not_found; edit -> $T1C with the new question"

# ---------- 3s. 서브에이전트를 부른 턴의 다시 생성 ----------
# 서브에이전트는 부모 없는 input 체크포인트를 tools:<id> 네임스페이스에 남긴다. 다시 생성이 그것을 입구로 집으면 앞 턴까지
# 지운다(P36 5절, F0 1번). 스텁만 메시지 안 조각으로 task 를 부르므로 실제 모델에서는 건너뛴다
if stub_only 3s "the stub delegates to the subagent from a JSON fragment"; then
  PRE="앞 턴 $(stub_call Add '{"a": 1, "b": 1}')"
  turn_done 3s "the earlier turn" "$(chat "/agents/$AG_ID/chat" "$(msg "$PRE")")"
  S2="$(sse_ 'first("done")["session_id"]')"
  TASK="$(delegate_call '{"a": 1, "b": 2}')"
  turn_done 3s "the subagent turn" "$(chat "/agents/$AG_ID/chat" "$(msg "$TASK" "$S2")")"
  [ "$(sse_ 'any(c.get("agent") for c in every("tool_call"))')" = True ] \
    || die 3s "no tool_call came from inside the subagent: $(sse_ 'every("tool_call")')"
  turn_done 3s "regenerate the subagent turn" "$(chat "/agents/$AG_ID/sessions/$S2/regenerate" '{}')"
  [ "$(sse_ 'first("done")["turn"]')" = 2 ] || die 3s "regenerate done.turn: $(sse_ 'first("done")')"
  [ "$(session_view "$S2" 'humans == args' "$PRE" "$TASK")" = True ] \
    || die 3s "regenerating the subagent turn lost the earlier turn: $(session_view "$S2" humans)"
  TASK_EDIT="$(delegate_call '{"a": 3, "b": 4}')"
  turn_done 3s "edit the subagent turn" "$(chat "/agents/$AG_ID/sessions/$S2/regenerate" "$(msg "$TASK_EDIT")")"
  [ "$(session_view "$S2" 'humans == args and users == args and len(dones) == 2' "$PRE" "$TASK_EDIT")" = True ] \
    || die 3s "editing the subagent turn lost the earlier turn: $(session_view "$S2" 'humans, users, dones')"
  pass 3s "session $S2: regenerating and editing turn 2 that called subagent $SUB_NAME kept turn 1 in the conversation and the transcript"
fi

# ---------- 4. 인용 ----------
CITE_N=""
SEARCH="$(say "$(stub_call "$KB_TOOL" '{"query": "inventory"}')" \
  "$KB_TOOL 도구로 inventory 를 찾아 보고, 찾은 내용을 출처 번호와 함께 한 문장으로 답해 줘.")"
turn_done 4 "the search turn" "$(chat "/agents/$AG_ID/chat" "$(msg "$SEARCH" "$S1")")"
if real_model && [ "$(sse_ '"citations" in names')" = False ]; then
  cond_skip 4 "the model did not call $KB_TOOL, so there is no citation to check (E2E_LLM_PROVIDER=$LLM_PROVIDER)"
else
  # 검색 도구 결과 바로 뒤가 citations 이고 call_id 가 그 결과다. 실제 모델은 다른 도구를 먼저 부를 수 있어 차례로 찾는다
  R="$(sse_ "next((k for k, (e, d) in enumerate(events) if e == 'tool_result' and d['name'] == '$KB_TOOL'), '')")"
  [ -n "$R" ] || die 4 "no $KB_TOOL tool_result: $(sse_ 'names')"
  [ "$(sse_ "names[$R + 1] == 'citations' and events[$R + 1][1]['call_id'] == events[$R][1]['id']")" = True ] \
    || die 4 "citations must follow the $KB_TOOL tool_result: $(sse_ 'names')"
  ITEM="events[$R + 1][1]['items'][0]"
  ITEM_OK="$(sse_ "(lambda c: c['kind'] == 'knowledge' and c['kb'] == {'id': '$KB_ID', 'name': '$KB_NAME'} and c['doc_id'] == '$DOC_ID' and c['title'] == 'faq.md' and c['url'] is None)($ITEM)")"
  [ "$ITEM_OK" = True ] || die 4 "citation item: $(sse_ "$ITEM")"
  CITE_N="$(sse_ "${ITEM}['n']")"
  [ "$(sse_ "json.loads(inner(events[$R][1]['content']))['results'][0]['cite']")" = "$CITE_N" ] \
    || die 4 "the result cite is not the citation number $CITE_N: $(sse_ "events[$R][1]['content']")"
  # Knowledge 의 검색 결과는 기본으로 바깥 내용이다(P43 2절). 이 세션(S1)은 이제 바깥 내용을 가진 대화라 그 뒤의 쓰기는 묻는다
  [ "$(sse_ "events[$R][1].get('untrusted')")" = outside ] \
    || die 4 "the $KB_TOOL result is not marked outside: $(sse_ "events[$R][1]")"
  SOURCE_PATH="/agents/$AG_ID/sessions/$S1/citations/$CITE_N/source"
  code="$(download "$SOURCE_PATH")"
  [ "$code" = 200 ] || die 4 "GET citation source $CITE_N -> $code"
  OFF="$(download_headers_off application/octet-stream faq.md)"
  [ -z "$OFF" ] || die 4 "citation source headers differ from the sample: $OFF"
  cmp -s "$WORK/download" "$WORK/faq.md" || die 4 "the citation source is not the uploaded faq.md"
  code="$(download "/agents/$AG_ID/sessions/$S1/citations/$((CITE_N + 100))/source")"
  { [ "$code" = 404 ] && [ "$(jq_ 'd["detail"]["code"]')" = citation_not_found ]; } \
    || die 4 "an uncited number -> $code $(cat "$WORK/body") (expected 404 citation_not_found)"
  [ "$(audited download_citation_source "/api$SOURCE_PATH")" = True ] \
    || die 4 "no audit row for the citation source read /api$SOURCE_PATH"
  pass 4 "search turn: citations [$CITE_N] right after the tool_result (kb $KB_NAME, doc faq.md, marked outside) and the result cite matches; source 200 with the five sample headers and the uploaded bytes, audited; uncited -> 404 citation_not_found"
fi

# ---------- 5. 피드백 ----------
COMMENT="e2e 의견 $RUN_TAG"
code="$(api PUT "/agents/$AG_ID/sessions/$S1/turns/$T1C/feedback" \
  "$(python3 -c 'import json,sys; print(json.dumps({"rating": "down", "tags": ["inaccurate"], "comment": sys.argv[1]}, ensure_ascii=False))' "$COMMENT")")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["rating"], d["tags"]')" = "down ['inaccurate']" ]; } || die 5 "PUT feedback on $T1C -> $code $(cat "$WORK/body")"
code="$(api GET "/agents/$AG_ID/sessions/$S1/feedback")"
[ "$code" = 200 ] && [ "$(jq_ "sorted((f['turn_id'], f['rating']) for f in d['items']) == sorted([('$T1', 'up'), ('$T1C', 'down')])")" = True ] \
  || die 5 "my feedback in $S1 -> $code $(cat "$WORK/body")"
code="$(api GET "/agents/$AG_ID/feedback?rating=down")"
[ "$code" = 200 ] && [ "$(jq_ "any(f['turn_id'] == '$T1C' and f['comment'] == '$COMMENT' and f['user_sub'] == '$ME_SUB' for f in d['items']) and 'last_7_days' in d")" = True ] \
  || die 5 "the approver list lacks the down rating of $T1C: $code $(cat "$WORK/body")"
EXPORT_PATH="/agents/$AG_ID/feedback/export"
code="$(download "$EXPORT_PATH")"
[ "$code" = 200 ] || die 5 "GET feedback export -> $code"
OFF="$(download_headers_off application/x-ndjson "feedback-$AG_NAME.ndjson")"
[ -z "$OFF" ] || die 5 "export headers differ from the sample: $OFF"
EXPORT_OK="$(python3 -c 'import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
schema, turn_id, comment = sys.argv[2:5]
print(bool(rows) and all(r["schema"] == schema and "user_sub" not in r for r in rows)
      and any(r["turn_id"] == turn_id and r["rating"] == "down" and r["comment"] == comment for r in rows))' \
  "$WORK/download" "$FEEDBACK_SCHEMA" "$T1C" "$COMMENT")"
[ "$EXPORT_OK" = True ] || die 5 "export lines: $(head -c 600 "$WORK/download")"
[ "$(audited export_agent_feedback "/api$EXPORT_PATH")" = True ] || die 5 "no audit row for the export /api$EXPORT_PATH"
code="$(api GET "/agents/$AG_ID/events?event=feedback&session_id=$S1&limit=50")"
# 식은 작은따옴표 변수에 둔다. bash 3.2 는 `[` 에 바로 넘긴 겹친 명령 치환 안의 {…, …} 를 중괄호 확장으로 쪼갠다
EVENTS_OK='{(e.get("data") or {}).get("rating") for e in d} >= {"up", "down"} and not any(e.get("user_sub") or "comment" in e or {"user_sub", "comment"} & set(e.get("data") or {}) for e in d)'
[ "$code" = 200 ] && [ "$(jq_ "$EVENTS_OK")" = True ] \
  || die 5 "feedback events must carry ratings without the person or the comment: $code $(cat "$WORK/body")"
# 3 의 다시 생성이 낸 turn_end.replaced_turn_id 를 수집기가 읽으면 그 턴의 평가가 superseded 다
SUPERSEDED=""
for _ in $(seq 1 $((EVENTS_WAIT / 10))); do
  api GET "/agents/$AG_ID/feedback?limit=200" >/dev/null
  SUPERSEDED="$(jq_ "next((f['superseded'] for f in d['items'] if f['turn_id'] == '$T1'), None)")"
  [ "$SUPERSEDED" = True ] && break
  sleep 10
done
[ "$SUPERSEDED" = True ] || die 5 "the rating of the replaced turn $T1 is not superseded after ${EVENTS_WAIT}s (superseded=$SUPERSEDED)"
pass 5 "down + inaccurate + comment on $T1C; my list and the approver list; NDJSON export with the five headers, $FEEDBACK_SCHEMA lines and no person, audited; feedback events carry no person or comment; the rating of $T1 became superseded"

# ---------- 6. 기억 파일 ----------
MEM_LIST="/agents/$AG_ID/memories/me"
PREFS="prefs.md"; NOTES="notes/today.md"; MEM_TEXT="짧은 답을 좋아한다 $RUN_TAG"
if ! real_model; then
  for file in "$PREFS" "$NOTES"; do
    turn_done 6 "write_file /memories/$file" "$(chat "/agents/$AG_ID/chat" "$(msg "$(stub_call write_file \
      "$(python3 -c 'import json,sys; print(json.dumps({"file_path": sys.argv[1], "content": sys.argv[2]}, ensure_ascii=False))' \
        "/memories/$file" "$MEM_TEXT")")" ${S3:+"$S3"})")"
    [ "$(sse_ 'first("tool_result")["status"]')" = success ] || die 6 "write_file /memories/$file: $(sse_ 'first("tool_result")')"
    S3="$(sse_ 'first("done")["session_id"]')"
  done
fi
code="$(api GET "$MEM_LIST")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["enabled"], d["retention_days"]')" = "True 30" ]; } || die 6 "GET $MEM_LIST -> $code $(cat "$WORK/body")"
MEM_PATHS="$(jq_ '" ".join(sorted(f["path"] for f in d["files"]))')"
start_pf "$AG_NS" "svc/$AG_K8S" "${RT_PORT}:8080"
PF_RT=$PF_PID
wait_port "$RT_PORT" || die 6 "port-forward to svc/$AG_K8S did not open :$RT_PORT"
RT_TOKEN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" token)"
code="$(rt GET "/memories?user_sub=$OTHER_SUB")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["files"]')" = "[]" ]; } || die 6 "another person's memory list -> $code $(cat "$WORK/body") (expected no files)"
if ! real_model; then
  # 둘을 따로 본다. 한 패턴으로 이으면 두 경로 사이의 공백 하나를 앞 패턴이 먹어 뒤 패턴이 늘 어긋난다
  for file in "$NOTES" "$PREFS"; do
    case " $MEM_PATHS " in *" /memories/$file "*) ;; *) die 6 "my memory files lack /memories/$file: [$MEM_PATHS]" ;; esac
  done
  code="$(api GET "$MEM_LIST/files/$PREFS")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["path"]')" = "/memories/$PREFS" ] && [ "$(jq_ 'd["content"]')" = "$MEM_TEXT" ]; } \
    || die 6 "GET $MEM_LIST/files/$PREFS -> $code $(cat "$WORK/body")"
  code="$(rt GET "/memories/files/$PREFS?user_sub=$OTHER_SUB")"
  { [ "$code" = 404 ] && [ "$(jq_ 'd["code"]')" = memory_not_found ]; } \
    || die 6 "another person read my $PREFS -> $code $(cat "$WORK/body") (expected 404 memory_not_found)"
  code="$(api DELETE "$MEM_LIST/files/$PREFS")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["deleted"]')" = True ]; } || die 6 "DELETE $MEM_LIST/files/$PREFS -> $code $(cat "$WORK/body")"
  api GET "$MEM_LIST" >/dev/null
  [ "$(jq_ "(lambda paths: '/memories/$NOTES' in paths and '/memories/$PREFS' not in paths)([f['path'] for f in d['files']])")" = True ] \
    || die 6 "deleting $PREFS must leave $NOTES: $(cat "$WORK/body")"
  code="$(api GET "$MEM_LIST/files/$PREFS")"
  { [ "$code" = 404 ] && [ "$(jq_ 'd["detail"]["code"]')" = memory_not_found ]; } || die 6 "the deleted $PREFS -> $code $(cat "$WORK/body")"
fi
# 모두 삭제는 런타임 없이도 도는 D34 의 라우트에 내 sub 를 준다
code="$(api DELETE "/agents/$AG_ID/memories/$(urlq "$ME_SUB")")"
[ "$code" = 200 ] || die 6 "DELETE /agents/$AG_ID/memories/$ME_SUB -> $code $(cat "$WORK/body")"
DELETED_ALL="$(jq_ 'd["deleted"]')"
api GET "$MEM_LIST" >/dev/null
[ "$(jq_ 'd["files"]')" = "[]" ] || die 6 "memory files left after deleting all: $(cat "$WORK/body")"
if real_model; then
  pass 6 "memory list enabled (retention 30d, files [$MEM_PATHS]); another person sees none; delete all removed $DELETED_ALL (file writes need the stub)"
else
  pass 6 "session $S3 wrote /memories/$PREFS and /memories/$NOTES; read back; another person sees neither; deleting $PREFS left $NOTES; delete all removed $DELETED_ALL"
fi
stop_pf "$PF_RT" "$RT_PORT"; PF_RT=""

# ---------- 8. 첨부 업로드와 첨부 턴 (B) ----------
api GET "/agents/$AG_ID" >/dev/null
CHAT_FACTS="$(jq_ '(lambda c: (c.get("documents"), c.get("images"), (c.get("limits") or {}).get("max_session_bytes")))((d.get("status") or {}).get("chat") or {})')"
[ "$CHAT_FACTS" = "True True $SESSION_CAP" ] \
  || die 8 "status.chat documents·images·max_session_bytes: $CHAT_FACTS (want True True $SESSION_CAP)"
chat_samples samples "$WORK/samples" "$MARK" >"$WORK/sample-names" || die 8 "making the sample files failed"
DOC_IDS=""; DOC_NAMES=""; IMG_ID=""; IMG_NAME=""; DOCX=""; TXT=""
# 이름에 공백이 없다(deploy/dev/lab/chat_samples.py). 업로드는 그 파일이 찍은 차례로 한다
SAMPLE_NAMES="$(cat "$WORK/sample-names")"
for name in $SAMPLE_NAMES; do
  case "$name" in
    *.png) upload_ok 8 "$AG_ID" "$WORK/samples/$name" "d['kind'] == 'image' and d['format'] == 'png' and d['text_chars'] is None"
           IMG_ID="$UPLOAD_ID"; IMG_NAME="$name"; continue ;;
    *.pdf) facts="d['format'] == 'pdf' and d['pages'] == 2" ;;
    *.xlsx) facts="d['format'] == 'xlsx' and d['sheets'] == 2" ;;
    *.docx) facts="d['format'] == 'docx'"; DOCX="$name" ;;
    *.hwpx) facts="d['format'] == 'hwpx'" ;;
    *.txt) facts="d['format'] == 'text'"; TXT="$name" ;;
    *) die 8 "unexpected sample $name" ;;
  esac
  upload_ok 8 "$AG_ID" "$WORK/samples/$name" "d['kind'] == 'document' and d['text_chars'] > 0 and not d['truncated'] and $facts"
  DOC_IDS="$DOC_IDS $UPLOAD_ID"; DOC_NAMES="$DOC_NAMES $name"
done
DOC_COUNT="$(echo "$DOC_IDS" | wc -w | tr -d ' ')"
READ_PATH="/uploads/$DOCX.txt"   # daegam-docparse 가 뽑은 글은 원래 이름에 .txt 를 붙인 자리에 보인다
READ="$(say "$(stub_call read_file "$(printf '{"file_path": "%s"}' "$READ_PATH")")" "첨부한 계약서의 첫 줄을 그대로 옮겨 줘.")"
# shellcheck disable=SC2086  # 업로드 id 여럿을 낱말로 나눠 넘긴다
turn_done 8 "the documents turn" "$(chat "/agents/$AG_ID/chat" "$(msg "$READ" "" $DOC_IDS)")"
S4="$(sse_ 'first("done")["session_id"]')"
[ "$(sse_ 'first("done").get("attachments_received")')" = "$DOC_COUNT" ] \
  || die 8 "done.attachments_received of the documents turn: $(sse_ 'first("done")') (want $DOC_COUNT)"
if ! real_model; then
  [ "$(sse_ "(lambda r: r['status'] == 'success' and '$MARK' in r['content'])(first('tool_result'))")" = True ] \
    || die 8 "read_file $READ_PATH did not show the mark $MARK: $(sse_ 'first("tool_result")')"
fi
# shellcheck disable=SC2086  # 이름 여럿을 낱말로 나눠 넘긴다
[ "$(session_view "$S4" '[a["name"] for x in items if x["kind"] == "user" for a in x.get("attachments") or []] == args' $DOC_NAMES)" = True ] \
  || die 8 "the user item of the transcript lacks the attachment names [$DOC_NAMES]: $(cat "$WORK/body")"
WRITE="$(say "$(stub_call write_file "$(printf '{"file_path": "/uploads/%s", "content": "x"}' "$TXT")")" "첨부한 화면을 한 문장으로 설명해 줘.")"
turn_done 8 "the image turn" "$(chat "/agents/$AG_ID/chat" "$(msg "$WRITE" "$S4" "$IMG_ID")")"
[ "$(sse_ 'first("done").get("attachments_received")')" = 1 ] \
  || die 8 "done.attachments_received of the image turn: $(sse_ 'first("done")') (want 1)"
if ! real_model; then
  [ "$(sse_ "(lambda r: r['status'] == 'error' and '$WRITE_DENIED' in r['content'])(first('tool_result'))")" = True ] \
    || die 8 "write_file into /uploads/ was not refused: $(sse_ 'first("tool_result")')"
  [ "$(sse_ "'$IMAGES_SEEN' in first('done')['text']")" = True ] \
    || die 8 "the model request did not carry the image: $(sse_ 'first("done")')"
fi
pass 8 "status.chat takes documents and images (session cap $SESSION_CAP); uploaded$DOC_NAMES with extracted facts and $IMG_NAME; session $S4: $DOC_COUNT documents received$(real_model || echo ", read_file $READ_PATH shows $MARK"), then the image received$(real_model || echo " and seen by the model, write_file into /uploads/ refused")"

# ---------- 9. 세션 파일 목록과 다운로드 (B) ----------
WANT_AGENT="None"   # 실제 모델이 쓰는 파일은 정할 수 없으므로 에이전트 파일은 스텁에서만 본다
if ! real_model; then
  WRITE_MD="$(stub_call write_file "$(printf '{"file_path": "%s", "content": "%s"}' "$AGENT_FILE" "$AGENT_TEXT")")"
  turn_done 9 "write_file $AGENT_FILE" "$(chat "/agents/$AG_ID/chat" "$(msg "$WRITE_MD" "$S4")")"
  [ "$(sse_ 'first("tool_result")["status"]')" = success ] || die 9 "write_file $AGENT_FILE: $(sse_ 'first("tool_result")')"
  WANT_AGENT="['$AGENT_FILE']"
fi
FILES_PATH="/agents/$AG_ID/sessions/$S4/files"
code="$(api GET "$FILES_PATH")"
[ "$code" = 200 ] || die 9 "GET $FILES_PATH -> $code $(cat "$WORK/body")"
# shellcheck disable=SC2086  # 이름 여럿을 낱말로 나눠 넘긴다
FILES_OFF="$(python3 - "$WORK/body" "$WANT_AGENT" $DOC_NAMES "$IMG_NAME" <<'PY'
import ast, json, sys

rows, want_agent, names = json.load(open(sys.argv[1]))["files"], ast.literal_eval(sys.argv[2]), sys.argv[3:]
# 업로드는 보낸 차례다. 텍스트 형식과 이미지는 원래 이름 그대로, 뽑은 글은 .txt 를 붙인 자리다(agent-runtime/runtime/attachments.py)
want_uploads = [f"/uploads/{n}" + ("" if n.endswith((".txt", ".png")) else ".txt") for n in names]
uploads = [r["path"] for r in rows if r["origin"] == "upload"]
agent = [r["path"] for r in rows if r["origin"] == "agent"]
internal = [r["path"] for r in rows if r["path"].startswith(("/large_tool_results/", "/conversation_history/"))]
off = {k: v for k, v in {
    "uploads": None if uploads == want_uploads else {"got": uploads, "want": want_uploads},
    "agent": None if want_agent is None or agent == want_agent else {"got": agent, "want": want_agent},
    "internal": internal or None,
}.items() if v}
print(json.dumps(off, ensure_ascii=False) if off else "")
PY
)"
[ -z "$FILES_OFF" ] || die 9 "session files differ: $FILES_OFF"
if ! real_model; then
  code="$(download "$FILES_PATH$AGENT_FILE")"
  [ "$code" = 200 ] || die 9 "GET $FILES_PATH$AGENT_FILE -> $code"
  OFF="$(download_headers_off application/octet-stream "${AGENT_FILE##*/}")"
  [ -z "$OFF" ] || die 9 "agent file headers differ from the sample: $OFF"
  [ "$(cat "$WORK/download")" = "$AGENT_TEXT" ] || die 9 "the agent file is not what write_file wrote: $(cat "$WORK/download")"
fi
code="$(download "$FILES_PATH/uploads/$TXT")"
[ "$code" = 200 ] || die 9 "GET $FILES_PATH/uploads/$TXT -> $code"
OFF="$(download_headers_off application/octet-stream "$TXT")"
[ -z "$OFF" ] || die 9 "uploaded file headers differ from the sample: $OFF"
cmp -s "$WORK/download" "$WORK/samples/$TXT" || die 9 "the uploaded $TXT came back changed"
code="$(download "$FILES_PATH/reports/none-$RUN_TAG.md")"
{ [ "$code" = 404 ] && [ "$(jq_ 'd["detail"]["code"]')" = file_not_found ]; } \
  || die 9 "a missing file -> $code $(cat "$WORK/body") (expected 404 file_not_found)"
pass 9 "session $S4 files: the uploads in the order sent$(real_model || echo " and $AGENT_FILE"), no internal paths; downloads with the five sample headers and the written·uploaded bytes; a missing file -> 404 file_not_found"

# ---------- 10. 상한 오류 (B) ----------
head -c "$((MAX_DOCUMENT_BYTES + 1))" /dev/zero | tr '\0' a >"$WORK/too-large.txt"
code="$(upload "$AG_ID" "$WORK/too-large.txt")"
{ [ "$code" = 413 ] && [ "$(jq_ 'd["detail"]["code"]')" = too_large ]; } \
  || die 10 "a document of $((MAX_DOCUMENT_BYTES + 1)) bytes -> $code $(cat "$WORK/body") (expected 413 too_large)"
# 8 의 턴이 실은 업로드는 스트림이 열릴 때 지워졌다
# shellcheck disable=SC2086  # 첫 업로드 id 를 꺼낸다
set -- $DOC_IDS
code="$(chat "/agents/$AG_ID/chat" "$(msg "다시 보내기" "$S4" "$1")")"
cp "$WORK/sse" "$WORK/body"
{ [ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = unknown_upload ]; } \
  || die 10 "an upload already sent -> $code $(cat "$WORK/body") (expected 422 unknown_upload)"
# 한글은 한 자가 3바이트라 상한까지 뽑은 글 하나가 약 1.5MB 다. 세션 상한을 넘을 만큼 업로드해 한 턴에 다섯씩 싣는다
chat_samples hangul "$WORK/big.txt" "$MAX_TEXT_CHARS" || die 10 "making the big text failed"
BIG_BYTES="$(wc -c <"$WORK/big.txt" | tr -d ' ')"
BIG_COUNT=$((SESSION_CAP / BIG_BYTES + 1))
BIG_IDS=()
for i in $(seq 1 "$BIG_COUNT"); do
  upload_ok 10 "$AG_ID" "$WORK/big.txt" "d['text_chars'] == $MAX_TEXT_CHARS and not d['truncated']" "big-$i.txt"
  BIG_IDS+=("$UPLOAD_ID")
done
# 첨부를 저장한 세션은 바깥 내용을 가진 대화라(P43 3절) 스텁이 조각 없이 고르는 Add(규칙 없는 쓰기)는 묻는다. 그래서 도구
# 없이 답하는 sleep 조각을 보낸다
NO_TOOL_MSG='{"sleep": 1}'
SENT=0
while [ "$SENT" -lt "$BIG_COUNT" ]; do
  BATCH=("${BIG_IDS[@]:$SENT:$MAX_PER_TURN}")
  SENT=$((SENT + ${#BATCH[@]}))
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$(say "$NO_TOOL_MSG 상한 확인 $SENT" "도구를 쓰지 말고 확인이라고만 답해 줘.")" "$S5" "${BATCH[@]}")")"
  [ "$SENT" -lt "$BIG_COUNT" ] || break
  turn_done 10 "attachments below the session cap" "$code"
  S5="$(sse_ 'first("done")["session_id"]')"
done
cp "$WORK/sse" "$WORK/body"
{ [ "$code" = 413 ] && [ "$(jq_ 'd["detail"]["code"]')" = attachments_too_large ]; } \
  || die 10 "$SENT attachments of $BIG_BYTES bytes over a $SESSION_CAP-byte session cap -> $code $(cat "$WORK/body") (expected 413 attachments_too_large)"
# 입구에서 거절된 업로드는 다시 보낼 수 있게 남는다
for id in "${BATCH[@]}"; do api DELETE "/agents/$AG_ID/uploads/$id" >/dev/null; done
pass 10 "$((MAX_DOCUMENT_BYTES + 1)) bytes -> 413 too_large; a sent upload -> 422 unknown_upload; $BIG_COUNT texts of $BIG_BYTES bytes over the $SESSION_CAP-byte session cap -> 413 attachments_too_large on the turn that crosses it${S5:+ (session $S5)}"

# ---------- 10d. 문서 파싱 서비스 파드 (B) ----------
docparse_pod 10d
pass 10d "$DOCPARSE_NOTE"

# ---------- 4g. 가림 규칙과 파일 도구를 끈 리비전 ----------
# 10f 가 같은 리비전을 쓰므로 인용이 없어도 재배포한다. 가림 규칙과 파일 도구를 끈 에이전트의 문서 첨부는 memory 종류를
# 읽지 않으므로 E2E_SHARED_STEPS=skip(태그의 클러스터 e2e 잡이 고른 게이트에 준다)이면 Postgres 실행(make e2e-chat,
# 후보마다 돈다)만 돈다. 손으로 돌리면 둘 다 돈다
if [ "$MEMORY_KIND" = redis ] && [ "${E2E_SHARED_STEPS:-}" = skip ]; then
  cond_skip 4g "E2E_SHARED_STEPS=skip: the masked citation reads no memory kind; make e2e-chat checks it"
  cond_skip 10f "E2E_SHARED_STEPS=skip: a document to an agent without file tools reads no memory kind; make e2e-chat checks it"
else
  agent_body 1
  code="$(api PUT "/agents/$AG_ID" "$(cat "$WORK/agent.json")")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["revision_created"]')" = True ]; } || die 4g "PUT the second revision -> $code $(cat "$WORK/body")"
  code="$(api POST "/agents/$AG_ID/redeploy")"
  [ "$code" = 200 ] || die 4g "POST /agents/$AG_ID/redeploy -> $code $(cat "$WORK/body")"
  wait_ready "/agents/$AG_ID" 4g
  # 판정은 배포된 리비전으로 하므로 파드를 기다리지 않아도 되지만, 10f 의 상태와 7 의 요청이 아직 내려가는 중인 옛 파드로 가지
  # 않게 기다린다. rollout status 는 옛 파드가 preStop 으로 내려가는 동안에도 끝나고 그 사이 옛 파드가 옛 리비전으로 답하므로
  # (2026-10-03 실측: 파일 도구가 꺼진 뒤에도 문서 턴이 200 이었다) 내려가는 파드가 다 지워질 때까지 본다
  "${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null \
    || die 4g "deploy/$AG_K8S did not roll out after the redeploy"
  wait_pods_settled "$AG_NS" "daegam.devops.ai.kr/resource=agent-instance,daegam.devops.ai.kr/id=$AG_ID" \
    || die 4g "the old pods of $AG_K8S were still terminating after ${POD_SETTLE_SECONDS}s"
  if [ -z "$CITE_N" ]; then
    cond_skip 4g "no citation from step 4 (E2E_LLM_PROVIDER=$LLM_PROVIDER); redeployed for 10f only"
  else
    code="$(download "$SOURCE_PATH")"
    { [ "$code" = 403 ] && [ "$(jq_ 'd["detail"]["code"]')" = source_masked ]; } \
      || die 4g "citation source with an output pii_mask -> $code $(cat "$WORK/body") (expected 403 source_masked)"
    pass 4g "redeployed with an output pii_mask guardrail and the file tools off: citation source $CITE_N -> 403 source_masked"
  fi

  # ---------- 10f. 파일 도구가 꺼진 에이전트의 문서 첨부 (B) ----------
  api GET "/agents/$AG_ID" >/dev/null
  [ "$(jq_ '((d.get("status") or {}).get("chat") or {}).get("documents")')" = False ] \
    || die 10f "status.chat after turning the file tools off: $(jq_ '(d.get("status") or {}).get("chat")') (want documents false)"
  upload_ok 10f "$AG_ID" "$WORK/samples/$TXT" "d['kind'] == 'document'"
  code="$(chat "/agents/$AG_ID/chat" "$(msg "파일 도구 없이 문서 보내기" "" "$UPLOAD_ID")")"
  cp "$WORK/sse" "$WORK/body"
  { [ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = attachments_need_filesystem ]; } \
    || die 10f "a document to an agent without file tools -> $code $(cat "$WORK/body") (expected 422 attachments_need_filesystem)"
  # 입구에서 거절된 업로드는 남으므로 지운다. 입력칸에서 칩을 지운 자리다
  code="$(api DELETE "/agents/$AG_ID/uploads/$UPLOAD_ID")"
  [ "$code" = 204 ] || die 10f "DELETE /agents/$AG_ID/uploads/$UPLOAD_ID -> $code"
  pass 10f "file tools off: status.chat.documents false; a document turn -> 422 attachments_need_filesystem; the kept upload deleted (204)"
fi

# ---------- 7. 세션 삭제 ----------
LONG_NOTE=""
if [ "$MEMORY_KIND" = redis ]; then
  # 다시 생성마다 버린 갈래가 체크포인트에 남는다. 그런 갈래가 쌓인 긴 세션의 키가 지우기 뒤 하나도 남지 않아야 한다. 새
  # 세션에서 키운다. S1 은 4 의 검색 결과(바깥 내용)를 가진 대화라 그 뒤의 Add(규칙 없는 쓰기)가 묻기 때문이다(P43 4절)
  for i in $(seq 1 "$LONG_TURNS"); do
    # 인자는 printf 로 만든다. bash 3.2 는 겹친 명령 치환 안의 \" 로 감싼 {…, …} 를 중괄호 확장으로 쪼갠다
    turn_done 7 "long session turn $i" "$(chat "/agents/$AG_ID/chat" \
      "$(msg "$(say "$(stub_call Add "$(printf '{"a": %s, "b": %s}' "$i" "$i")")" "$i 더하기 $i 는?")" "$S6")")"
    S6="$(sse_ 'first("done")["session_id"]')"
    for _ in $(seq 1 "$LONG_REGENS"); do
      turn_done 7 "long session regenerate $i" "$(chat "/agents/$AG_ID/sessions/$S6/regenerate" '{}')"
    done
  done
  LONG_NOTE="; redis: $S6 first grew by $LONG_TURNS turns x $LONG_REGENS regenerations"
fi
DELETED=""
for sid in $S1 $S2 $S3 $S4 $S5 $S6; do
  before="$(stored_rows "$sid")"
  [ "${before:-0}" -gt 0 ] || die 7 "no stored rows for $sid before deleting (the storage query is wrong?)"
  code="$(api DELETE "/agents/$AG_ID/sessions/$sid")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd["session_id"], d["deleted"]')" = "$sid True" ]; } || die 7 "DELETE session $sid -> $code $(cat "$WORK/body")"
  code="$(api GET "/agents/$AG_ID/sessions/$sid")"
  { [ "$code" = 404 ] && [ "$(jq_ 'd["detail"]["code"]')" = session_not_found ]; } || die 7 "GET deleted session $sid -> $code"
  after="$(stored_rows "$sid")"
  [ "$after" = 0 ] || die 7 "$after stored rows of $sid left after deleting (before: $before)"
  DELETED="$DELETED $sid($before->0)"
done
LEFT="$(listed_sessions)"
for sid in $S1 $S2 $S3 $S4 $S5 $S6; do
  case " $LEFT " in *" $sid "*) die 7 "deleted session $sid is still listed" ;; esac
done
code="$(api GET "/agents/$AG_ID/sessions/$S1/feedback")"
{ [ "$code" = 200 ] && [ "$(jq_ 'd["items"]')" = "[]" ]; } || die 7 "my feedback in the deleted $S1 -> $code $(cat "$WORK/body")"
api GET "/agents/$AG_ID/feedback?limit=200" >/dev/null
[ "$(jq_ "[f['turn_id'] for f in d['items'] if f['session_id'] == '$S1']")" = "[]" ] \
  || die 7 "the approver list still has feedback of the deleted $S1: $(cat "$WORK/body")"
pass 7 "deleted sessions (stored rows with attachments, before->after):$DELETED; detail 404, not listed, their feedback gone$LONG_NOTE"

finish
