# shellcheck shell=bash
# 플랫폼 API 를 부르는 한 벌 (e2e_agent·e2e_assistant·e2e_chat·e2e_gateway·e2e_install·e2e_instance·e2e_knowledge·e2e_mcp_auth·
# e2e_oauth·e2e_pat·e2e_ui_serving). `. "$HERE/lib/e2e_api.sh"` 로 읽는다.
# 부르는 쪽이 API·WORK·TENANT·READY_TIMEOUT 과 AUTH(관리자 Bearer 헤더 배열. 빈 배열은 header 모드다)를 두고 e2e_json.sh 를
# 먼저 읽는다. die(STEP MSG)는 e2e_report.sh 의 것이다. secret_field 를 부르는 쪽은 K(kubectl 배열)도 둔다. 읽을 때
# 요구하는 변수는 없다. curl 은 e2e_exit.sh 의 시간 제한을 붙인 함수다.
#
# api METHOD PATH [JSON [CURL_ARG...]]
#   상태 코드를 찍고 본문은 $WORK/body 에 둔다. API_OUT 을 주면 본문을 그 파일에 둔다(뒤에서 부르는 호출이 $WORK/body 를
#   다른 호출과 나눠 쓰지 않게 한다). AUTH 와 X-Daegam-Tenant: $TENANT 를 붙인다. 플랫폼에 연결하지 못하면 000 이다.
#   JSON 이 @- 면 본문을 stdin 에서 읽는다. 비밀번호가 든 본문을 curl 인자에 남기지 않을 때 쓴다. JSON 뒤의 인자는 curl 에
#   그대로 넘긴다. 헤더나 multipart 필드(-F)를 더 붙일 때 쓰고, 본문 없이 넘기려면 JSON 자리에 빈 문자열을 준다
# api_as TOKEN METHOD PATH [JSON [CURL_ARG...]]
#   같은 호출을 TOKEN 으로 한다. 사람 본인의 라우트(/me/tokens)를 그 사람의 토큰으로 부른다
# jq_ EXPR
#   $WORK/body 를 json_of 로 읽는다
# find_id NAME
#   $WORK/body 의 목록(배열이든 {items} 든)에서 name 이 NAME 인 항목의 id 를 찍는다. 없으면 빈 줄이다. 본문이 JSON 이
#   아니면 json_of 처럼 0 이 아니다
# listed_id STEP LIST_PATH NAME
#   GET LIST_PATH 목록에서 name 이 NAME 인 항목의 id 를 LISTED_ID 에 남긴다. 만들기가 409(이름 충돌)일 때 이전 실행이 남긴
#   것을 찾는 데 쓴다. 목록을 읽지 못하거나 그 이름이 없으면 die STEP 이다
# hdr NAME [FILE]
#   curl -D 로 받은 응답 헤더 파일 FILE(기본 $WORK/hdr)에서 헤더 NAME 의 첫 값을 찍는다. 이름은 대소문자를 가리지 않고
#   줄 끝 CR 은 뗀다. 없으면 빈 줄이다
# secret_field NS SECRET KEY
#   그 Secret 한 키의 값(base64 를 푼 것)을 찍는다
# token_aud NS SECRET [KEY]
#   그 Secret 의 서비스 토큰(키 KEY, 기본 token) aud 를 JSON 목록으로 찍는다. 서명은 보지 않는다 (P50 9절)
# api_delete PATH
#   지운다. 이미 없으면(404) 넘어가고, 앞에서 지운 것이 아직 붙들고 있으면(409) 5초씩 12번 다시 본다. 끝내 못 지우면
#   까닭을 stderr 에 찍고 넘어간다. 정리 단계에서 부르므로 멈추지 않는다
# wait_ready PATH LABEL
#   status.phase 가 Ready 일 때까지 5초마다 본다. 플랫폼이 다시 뜨는 중(000·502·503)이면 기다리고, 그 밖의 코드나
#   READY_TIMEOUT 초를 넘기면 die LABEL 이다
# wait_gone PATH [SECONDS [STOP_EXPR]]
#   GET PATH 가 404 가 될 때까지 5초마다 본다. 삭제는 정의까지 지우는 hard delete 라 지운 뒤 조회는 404 다(D42).
#   SECONDS(기본 WAIT_GONE_SECONDS)를 넘기면 1 이다. 플랫폼에 연결하지 못한 답(000)은 port-forward 가 다시 붙는 동안의
#   것일 수 있어 기다리되, WAIT_GONE_DOWN 번 잇달아 그러면 플랫폼이 내려간 것으로 보고 1 이다. STOP_EXPR 을 주면 200
#   본문에서 그 식이 True 일 때 기다려도 사라지지 않는 것으로 보고 2 다(지우기가 실패해 failed 로 남은 문서처럼).
#   STEP 과 정리 단계가 함께 쓰므로 die 하지 않는다. 끝나면 $WORK/body 가 마지막 GET 의 본문이다
# save_doc STEP PATH NAME BODY
#   PATH(/mcp-servers·/gateways·/agents)에 정의 BODY 를 저장하고 SAVED_ID 를 남긴다. 만들기는 저장이라 클러스터에는
#   아무것도 생기지 않는다(D42). 새로 만들었으면(201) SAVE_CREATED=1 이다. 재실행의 409 는 이름 충돌이라 그 이름의 문서에
#   같은 BODY 로 PUT 해 새 리비전을 쌓고 SAVE_CREATED=0 이다
# deploy_doc STEP PATH ID [BODY]
#   저장해 둔 정의를 BODY(기본 {}) 본문으로 배포한다. 배포 본문은 이름을 받지 않는다. 이름은 저장할 때 정해졌다(D42).
#   재실행의 409 already_deployed 는 지금 리비전으로 redeploy 하고 DOC_REDEPLOYED=1 을 남긴다. Ready 는 기다리지 않는다.
#   Agent(/agents)는 BODY 가 scale_to_zero 를 정하지 않으면 false 로 보내고 redeploy 에도 같은 값을 보낸다. 설치본의 쉬는
#   창이 다른 게이트의 Agent 를 재우지 않게 하고, 앞 실행이 남긴 문서의 설정도 그 값으로 맞춘다 (P44 17절)
# deploy_ready STEP PATH ID [BODY]
#   deploy_doc 뒤 Ready 를 기다린다. 끝나면 $WORK/body 가 그 문서의 GET 응답이다
# error_code FILE
#   그 답의 detail.code 를 찍는다. JSON 이 아니면 빈 줄이다
# sleep_route STEP ID [WAIT]
#   POST /agents/ID/sleep 가 200 일 때까지 부른다(P44 7절). 깨우는 중(409 state_conflict), 턴이 리스를 쥔 동안(409
#   agent_busy), 남이 클레임을 쥔 동안(409 in_progress) 은 5초 뒤 다시 부르고, WAIT 초(기본 SLEEP_ROUTE_WAIT, 없으면 300)를
#   넘기거나 다른 답이면 die STEP 이다. 그 뒤 상태가 Sleeping 이고 sleep.state 가 asleep 일 때까지 5초마다 60초 동안 본다.
#   끝나면 $WORK/body 가 그 문서의 GET 응답이다
# clone_fresh STEP PATH SRC_ID NAME [DISPLAY]
#   SRC_ID 를 NAME(표시 이름 DISPLAY)으로 복제한다. 끝나면 $WORK/body 가 201 복제 응답이다. 재실행의 409(이름 충돌)는
#   이전 실행의 복제본을 지우고 다시 복제한다. 복제본은 draft 라 배포는 부르는 쪽이 한다
# upload_backend_api STEP NAME FILE [DISPLAY]
#   스펙 FILE 을 Backend API NAME(표시 이름 DISPLAY)으로 올리고 BACKEND_API_ID 와 그 최신 버전 BACKEND_API_VERSION 을
#   남긴다. 재실행이면 같은 이름에 새 버전(200)이거나 409 이고, 409 면 그 이름의 문서를 잇는다 (e2e_install STEP 5,
#   install 의 e2e_web_tools STEP 3). 끝나면 $WORK/body 가 그 Backend API 의 GET 응답이다
# register_data_service STEP NAME BODY
#   POST /data-services 로 Database NAME 을 등록하고 verify 해 DATA_SERVICE_ID 를 남긴다. 재실행의 409 면 그 이름의 문서에
#   BODY 의 자격(username·password)만 PATCH 한다. 비밀번호가 바뀌었을 수 있어서다. 본문은 $WORK 의 파일로 보내 curl 의
#   인자에 비밀번호가 오르지 않고, 등록이 422 면 입력을 되풀이할 수 있는 응답 본문을 찍지 않는다. verify 가 200 ok 가
#   아니면 die STEP 이다. 끝나면 $WORK/body 가 verify 응답이다
# create_fresh STEP PATH NAME BODY
#   PATH 에 BODY 로 NAME 을 만들고 CREATED_ID 를 남긴다. 끝나면 $WORK/body 가 201 응답이다. 재실행의 409(이름 충돌)면 이전
#   실행이 남긴 그 이름의 문서를 지우고 사라지기(wait_gone)를 기다린 뒤 다시 만든다. 사라지지 않으면 die STEP 이다
# kb_create_fresh STEP NAME BODY
#   create_fresh 로 Knowledge Base NAME 을 만들고 KB_ID 를 남긴다. 임베딩은 바뀌지 않으므로 고쳐 쓰지 않는다
# kb_upload KB_ID FILE
#   Knowledge Base 에 문서 FILE 을 올리고 상태 코드를 찍는다. 본문은 $WORK/body 다
# kb_wait_indexed STEP KB_ID DOC_ID
#   그 문서가 indexed 가 될 때까지 KB_INDEX_POLL 초마다 KB_INDEX_WAIT 초 동안 본다. 문서가 failed 거나 GET 이 200 이
#   아니거나 시간을 넘기면 die STEP 이다. 끝나면 $WORK/body 가 그 문서다. die 가 부른 쪽을 멈춰야 하므로 $( ) 안에서
#   부르지 않는다
# ensure_stub_provider STEP NAME
#   Platform Settings 에 kind stub 인 provider NAME 이 없으면 넣고 STUB_PROVIDER_ADDED=1 을 남긴다. 배포본이 스텁을 허락하지
#   않으면(ConfigMap DAEGAM_ALLOW_STUB_PROVIDER) die STEP 이다. 끝나면 $WORK/body 가 Platform Settings 다
# drop_stub_provider NAME
#   Platform Settings 에 provider NAME 이 있으면 걷는다. 이 실행이 넣지 않았어도(죽은 앞 실행이 남겼어도) 걷는다. 걷은 뒤에도
#   남았거나 읽지 못하면 사람이 지울 자리를 찍고 1 이다. 정리 단계에서 부르므로 멈추지 않는다 (e2e_install 8t, e2e_runs)
# remove_providers NAME...
#   Platform Settings 에서 그 provider 들을 걷는다. 플랫폼이 관리하던 키 Secret 은 플랫폼이 함께 지운다(D56). 정리 단계에서
#   부르므로 멈추지 않고, 실패하면 사람이 지울 자리를 stderr 에 찍는다
# put_provider_key STEP NAME KEY
#   provider NAME 의 키를 KEY 로 넣는다(PUT /settings/llm-providers/{name}/api-key). 플랫폼이 관리 Secret 에 넣고 provider 의
#   api_key_secret 을 그 이름으로 맞춘다(D56). 200 이 아니면 die STEP 이다
# require_features STEP NAME...
#   $WORK/body(Agent 문서)의 상태 runtime.features(이미지가 알린 기능 이름, D65)에 NAME 이 모두 있어야 한다. 아니면 die
#   STEP 이다. 읽은 이름을 정렬해 공백으로 이은 것을 FEATURES 에 남긴다
# install_postgres_database STEP NAME NS CLUSTER_ID
#   설치본이 플랫폼 네임스페이스 NS 에 둔 pgvector Postgres(StatefulSet postgres, Service postgres, Secret
#   daegam-postgres-auth)를 Database NAME 으로 register_data_service 에 맡겨 등록하고 verify 한다(e2e_install STEP 4,
#   e2e_telemetry 와 install 의 e2e_skills·e2e_web_tools·e2e_sandbox·e2e_llm_gateway). vector 확장이 없으면 die STEP 이다.
#   PG_VECTOR(확장 판)와 DATA_SERVICE_ID 를 남긴다
#
# 값을 남기는 도우미(listed_id·save_doc·clone_fresh·create_fresh·register_data_service·kb_create_fresh 등)는 값을
# 찍지 않고 변수에 남긴다. $( ) 안에서 부르면 die 가 찍는 실패 메시지가 부른 쪽 변수로 들어가 사라지기 때문이다.
#
# 재실행의 409 를 원본과 복제본이 다르게 다룬다. 원본(save_doc)은 지우지 않고 PUT 으로 덮는다. 게시된 문서는 삭제가
# 409 in_use(게시 항목을 Gateway 가 붙든다)이고 해제도 409 published 라 지우는 길이 막히기 때문이다. 복제본(clone_fresh)은
# 게시되지 않아 409 가 이름 충돌뿐이므로 지우고 다시 복제하면 깨끗해진다. redeploy 는 처음 배포한 노출을 그대로 두고,
# 스펙이 같으면 파드도 그대로다. e2e_gateway 는 예외로 이전 실행의 Gateway 를 STEP 0 에서 게시 항목부터 지운다. 그
# Gateway 들이 STEP 3 이 비어 있다고 보는 게시 항목을 붙들기 때문이다.

api() { # METHOD PATH [JSON [CURL_ARG...]]
  local method="$1" path="$2" body="${3:-}" data=()
  shift 2
  [ $# -eq 0 ] || shift
  [ -z "$body" ] || data=(-H 'Content-Type: application/json' -d "$body")
  curl -sS -o "${API_OUT:-$WORK/body}" -w '%{http_code}' -X "$method" "$API$path" ${AUTH[@]+"${AUTH[@]}"} \
    -H "X-Daegam-Tenant: $TENANT" ${data[@]+"${data[@]}"} "$@" || true
}

api_as() { # TOKEN METHOD PATH [JSON [CURL_ARG...]]
  # api 가 읽는 AUTH 를 이 호출 동안만 바꾼다 (bash 의 local 은 부른 함수에도 보인다)
  local AUTH=(-H "Authorization: Bearer $1")
  shift
  api "$@"
}

jq_() { json_of "$WORK/body" "$1"; }

find_id() { # NAME
  jq_ "next((x['id'] for x in (d if isinstance(d, list) else d.get('items', [])) if x.get('name') == '$1'), '')"
}

LISTED_ID=""   # listed_id 가 방금 찾은 id
listed_id() { # STEP LIST_PATH NAME
  local code
  code="$(api GET "$2")"
  [ "$code" = 200 ] || die "$1" "GET $2 -> $code"
  LISTED_ID="$(find_id "$3")"
  [ -n "$LISTED_ID" ] || die "$1" "$3 conflicts (409) but is not listed in GET $2"
}

hdr() { # NAME [FILE]
  awk -v want="$1" 'BEGIN { want = tolower(want) }
    { sub(/\r$/, ""); i = index($0, ":") }
    i > 0 && tolower(substr($0, 1, i - 1)) == want { v = substr($0, i + 1); sub(/^[ \t]+/, "", v); print v; exit }' \
    "${2:-$WORK/hdr}" 2>/dev/null || true
}

secret_field() { "${K[@]}" -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; } # NS SECRET KEY

token_aud() { # NS SECRET [KEY]
  jwt_json "$(secret_field "$1" "$2" "${3:-token}")" 1 \
    | json_of - 'json.dumps(d["aud"] if isinstance(d["aud"], list) else [d["aud"]])'
}

api_delete() { # PATH
  local code i
  for ((i = 0; i < 12; i++)); do
    code="$(api DELETE "$1")"
    case "$code" in 200 | 204 | 404) return 0 ;; 409) sleep 5 ;; *) break ;; esac
  done
  echo "!! DELETE $1 -> $code: $(cat "$WORK/body")" >&2
}

wait_ready() { # PATH LABEL
  local path="$1" label="$2" phase="" code i
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    code="$(api GET "$path")"
    case "$code" in
      200) phase="$(jq_ '(d.get("status") or {}).get("phase")' || true)"; [ "$phase" = "Ready" ] && return 0 ;;
      000 | 502 | 503) phase="(platform $code)" ;;
      *) die "$label" "GET $path -> $code" ;;
    esac
    sleep 5
  done
  die "$label" "phase=$phase after ${READY_TIMEOUT}s"
}

WAIT_GONE_SECONDS=120   # wait_gone 의 기본 한도(초)다
WAIT_GONE_DOWN=3        # wait_gone 이 플랫폼이 내려갔다고 보는 잇단 000 의 수다. port-forward 재연결은 몇 초면 끝난다
wait_gone() { # PATH [SECONDS [STOP_EXPR]]
  local path="$1" max="${2:-$WAIT_GONE_SECONDS}" stop="${3:-}" down=0 i
  for ((i = 0; i < max / 5; i++)); do
    case "$(api GET "$path")" in
      404) return 0 ;;
      000) down=$((down + 1)); [ "$down" -lt "$WAIT_GONE_DOWN" ] || return 1 ;;
      200) down=0; [ -z "$stop" ] || [ "$(jq_ "$stop" || true)" != True ] || return 2 ;;
      *) down=0 ;;
    esac
    sleep 5
  done
  return 1
}

# 해제가 남이 쥔 클레임에 일을 남기면 응답은 undeploying 문서다 (P66 2.6절). 홀더가 마치면 draft 가
# 된다. 성공하면 $WORK/body 에 draft 문서가 남아 부르는 쪽이 이어서 읽는다
wait_draft() { # PATH [SECONDS]
  local path="$1" max="${2:-$WAIT_GONE_SECONDS}" i
  for ((i = 0; i < max / 5; i++)); do
    [ "$(api GET "$path")" = "200" ] && [ "$(jq_ 'd["state"]' || true)" = "draft" ] && return 0
    sleep 5
  done
  return 1
}

# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
SAVED_ID=""; SAVE_CREATED=0; DOC_REDEPLOYED=0

# shellcheck disable=SC2034  # SAVE_CREATED 는 부르는 쪽이 읽는다
save_doc() { # STEP PATH NAME BODY
  local step="$1" path="$2" code
  code="$(api POST "$path" "$4")"
  case "$code" in
    201) SAVED_ID="$(jq_ 'd["id"]')"; SAVE_CREATED=1 ;;
    409) listed_id "$step" "$path" "$3"; SAVED_ID="$LISTED_ID"; SAVE_CREATED=0
         code="$(api PUT "$path/$SAVED_ID" "$4")"
         [ "$code" = 200 ] || die "$step" "PUT $path/$SAVED_ID ($3) -> $code" ;;
    *) die "$step" "POST $path ($3) -> $code" ;;
  esac
}

deploy_doc() { # STEP PATH ID [BODY]
  local step="$1" path="$2" id="$3" body="${4:-}" again='' code
  [ -n "$body" ] || body='{}'
  if [ "$path" = /agents ]; then
    # 첫 줄은 배포 본문, 둘째 줄은 재실행의 redeploy 본문이다
    { read -r body; read -r again; } < <(python3 -c 'import json, sys
d = json.loads(sys.argv[1]); d.setdefault("scale_to_zero", False)
print(json.dumps(d)); print(json.dumps({"scale_to_zero": d["scale_to_zero"]}))' "$body") || true
    # 프로세스 치환 안의 python3 실패는 set -e 를 걸지 않고 본문을 비운다. 빈 본문을 보내기 전에 멈춘다
    [ -n "$body" ] && [ -n "$again" ] || die "$step" "deploy body for $path/$id is not JSON: ${4:-}"
  fi
  code="$(api POST "$path/$id/deploy" "$body")"
  case "$code" in
    201) ;;
    409) [ "$(jq_ "d['detail']['code']")" = already_deployed ] \
           || die "$step" "deploy $path/$id -> 409 $(jq_ "d['detail']['code']"): $(cat "$WORK/body")"
         code="$(api POST "$path/$id/redeploy" "$again")"
         [ "$code" = 200 ] || die "$step" "POST $path/$id/redeploy -> $code"
         # shellcheck disable=SC2034  # 부르는 쪽이 읽는다
         DOC_REDEPLOYED=1 ;;
    *) die "$step" "POST $path/$id/deploy -> $code" ;;
  esac
}

require_features() { # STEP NAME...
  local step="$1" name
  shift
  FEATURES="$(jq_ '" ".join(sorted(((d.get("status") or {}).get("runtime") or {}).get("features") or []))')"
  for name in "$@"; do
    case " $FEATURES " in *" $name "*) ;; *) die "$step" "status.runtime.features lacks $name: [$FEATURES]" ;; esac
  done
}

deploy_ready() { # STEP PATH ID [BODY]
  deploy_doc "$@"
  wait_ready "$2/$3" "$1"
  api GET "$2/$3" >/dev/null
}

error_code() { # FILE
  json_of "$1" "(d.get('detail') or {}).get('code')" 2>/dev/null || true
}

sleep_route() { # STEP ID [WAIT]
  local step="$1" id="$2" max="${3:-${SLEEP_ROUTE_WAIT:-300}}" code why="" seen="" deadline i
  deadline=$((SECONDS + max))
  while :; do
    code="$(api POST "/agents/$id/sleep")"
    [ "$code" = 200 ] && break
    why="$(error_code "$WORK/body")"
    case "$code $why" in
      "409 agent_busy" | "409 state_conflict" | "409 in_progress") ;;
      *) die "$step" "POST /agents/$id/sleep -> $code $why" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || die "$step" "POST /agents/$id/sleep still $code $why after ${max}s"
    sleep 5
  done
  for ((i = 0; i < 12; i++)); do
    code="$(api GET "/agents/$id")"
    seen="$code $(jq_ '(d.get("status") or {}).get("phase"), ((d.get("status") or {}).get("sleep") or {}).get("state")' || true)"
    [ "$seen" = "200 Sleeping asleep" ] && return 0
    sleep 5
  done
  die "$step" "agent $id after the sleep route: $seen (want 200 Sleeping asleep)"
}

CLONE_RETRIES=24   # clone_fresh 가 이전 복제본을 지운 뒤 다시 복제해 보는 횟수(5초 간격)다
clone_fresh() { # STEP PATH SRC_ID NAME [DISPLAY]
  local step="$1" path="$2" body code i
  body="{\"name\": \"$4\"}"
  [ -z "${5:-}" ] || body="{\"name\": \"$4\", \"display_name\": \"$5\"}"
  code="$(api POST "$path/$3/clone" "$body")"
  if [ "$code" = 409 ]; then
    listed_id "$step" "$path" "$4"
    api_delete "$path/$LISTED_ID"
    for ((i = 0; i < CLONE_RETRIES; i++)); do
      code="$(api POST "$path/$3/clone" "$body")"
      [ "$code" = 409 ] || break
      sleep 5
    done
  fi
  [ "$code" = 201 ] || die "$step" "POST $path/$3/clone $4 -> $code"
}

# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
BACKEND_API_ID=""; BACKEND_API_VERSION=""
# shellcheck disable=SC2034  # BACKEND_API_VERSION 은 부르는 쪽이 읽는다
upload_backend_api() { # STEP NAME FILE [DISPLAY]
  local step="$1" name="$2" code shown=()
  [ -z "${4:-}" ] || shown=(-F "display_name=$4")
  code="$(api POST /backend-apis "" -F "name=$name" ${shown[@]+"${shown[@]}"} -F "file=@$3")"
  case "$code" in
    200 | 201) BACKEND_API_ID="$(jq_ 'd["id"]')" ;;
    409) listed_id "$step" /backend-apis "$name"; BACKEND_API_ID="$LISTED_ID" ;;
    *) die "$step" "POST /backend-apis ($name) -> $code" ;;
  esac
  code="$(api GET "/backend-apis/$BACKEND_API_ID")"
  [ "$code" = 200 ] || die "$step" "GET /backend-apis/$BACKEND_API_ID -> $code"
  BACKEND_API_VERSION="$(jq_ 'd.get("version") or (d.get("versions") or [{}])[-1].get("version") or 1')"
}

DATA_SERVICE_ID=""   # register_data_service 가 방금 등록한 Database 의 id
# _ds_failed STEP MSG CODE : 등록이 실패했다. 422 는 검증 오류가 입력(비밀번호)을 되풀이할 수 있어 본문을 찍지 않는다
_ds_failed() {
  if [ "$3" = 422 ]; then die "$1" "$2" "(the 422 body is not shown: it may repeat the password)"; fi
  die "$1" "$2"
}
register_data_service() { # STEP NAME BODY
  local step="$1" name="$2" code
  # printf 는 셸 안의 명령이라 본문이 프로세스 인자에 오르지 않는다
  printf '%s' "$3" >"$WORK/data-service.json"
  code="$(api POST /data-services "@$WORK/data-service.json")"
  case "$code" in
    201) DATA_SERVICE_ID="$(jq_ 'd["id"]')" ;;
    409) listed_id "$step" /data-services "$name"; DATA_SERVICE_ID="$LISTED_ID"
         json_of "$WORK/data-service.json" 'json.dumps({k: d[k] for k in ("username", "password", "tls") if k in d})' \
           >"$WORK/data-service-patch.json"
         code="$(api PATCH "/data-services/$DATA_SERVICE_ID" "@$WORK/data-service-patch.json")"
         [ "$code" = 200 ] || _ds_failed "$step" "PATCH /data-services/$DATA_SERVICE_ID ($name) -> $code" "$code" ;;
    *) _ds_failed "$step" "POST /data-services ($name) -> $code" "$code" ;;
  esac
  rm -f "$WORK/data-service.json" "$WORK/data-service-patch.json"
  code="$(api POST "/data-services/$DATA_SERVICE_ID/verify")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd.get("ok")')" = True ]; } || die "$step" "verify $name -> $code $(cat "$WORK/body")"
}

CREATED_ID=""   # create_fresh 가 방금 만든 문서의 id
create_fresh() { # STEP PATH NAME BODY
  local step="$1" path="$2" code
  code="$(api POST "$path" "$4")"
  if [ "$code" = 409 ]; then
    listed_id "$step" "$path" "$3"
    api_delete "$path/$LISTED_ID"
    wait_gone "$path/$LISTED_ID" || die "$step" "$3 left by an earlier run did not go away"
    code="$(api POST "$path" "$4")"
  fi
  [ "$code" = 201 ] || die "$step" "POST $path ($3) -> $code"
  CREATED_ID="$(jq_ 'd["id"]')"
}

kb_create_fresh() { # STEP NAME BODY
  create_fresh "$1" /knowledge-bases "$2" "$3"
  # shellcheck disable=SC2034  # 부르는 쪽이 읽는다
  KB_ID="$CREATED_ID"
}

kb_upload() { api POST "/knowledge-bases/$1/documents" "" -F "file=@$2"; } # KB_ID FILE

KB_INDEX_WAIT=60; KB_INDEX_POLL=3   # kb_wait_indexed 의 한도와 간격(초)이다
kb_wait_indexed() { # STEP KB_ID DOC_ID
  local step="$1" path="/knowledge-bases/$2/documents/$3" code state="" i
  for ((i = 0; i < KB_INDEX_WAIT / KB_INDEX_POLL; i++)); do
    code="$(api GET "$path")"
    [ "$code" = 200 ] || die "$step" "GET $path -> $code"
    state="$(jq_ 'd["state"]')"
    case "$state" in
      indexed) return 0 ;;
      failed) die "$step" "document failed: $(jq_ 'd.get("error")')" ;;
    esac
    sleep "$KB_INDEX_POLL"
  done
  die "$step" "document state=$state after ${KB_INDEX_WAIT}s"
}

# shellcheck disable=SC2034  # 부르는 쪽의 정리 단계가 읽는다
STUB_PROVIDER_ADDED=0

ensure_stub_provider() { # STEP NAME
  local step="$1" name="$2" code provider
  code="$(api GET /settings)"
  [ "$code" = 200 ] || die "$step" "GET /settings -> $code"
  [ "$(jq_ "[p['name'] for p in d['llm_providers'] if p['name'] == '$name' and p['kind'] == 'stub']")" = "['$name']" ] && return 0
  # bash 3.2 는 겹친 "$(…"$(…)"…)" 안의 \" 로 감싼 {…, …} 를 중괄호 확장으로 쪼갠다. 그래서 본문을 먼저 변수에 둔다
  provider="$(printf '{"name": "%s", "kind": "stub"}' "$name")"
  code="$(api PATCH /settings "$(providers_with "$WORK/body" "$provider")")"
  if [ "$code" = 422 ] && [ "$(jq_ "d['detail']['code']")" = stub_provider_disallowed ]; then
    die "$step" "stub provider rejected: the deployment needs DAEGAM_ALLOW_STUB_PROVIDER=true in ConfigMap daegam-platform-config and a rollout"
  fi
  [ "$code" = 200 ] || die "$step" "PATCH /settings (stub provider $name) -> $code"
  # shellcheck disable=SC2034  # 부르는 쪽의 정리 단계가 읽는다
  STUB_PROVIDER_ADDED=1
}

drop_stub_provider() { # NAME
  local code
  code="$(api GET /settings)"
  if [ "$code" = 200 ] && [ "$(jq_ "any(p['name'] == '$1' for p in d['llm_providers'])")" = True ]; then
    remove_providers "$1"
    code="$(api GET /settings)"
  fi
  { [ "$code" = 200 ] && [ "$(jq_ "any(p['name'] == '$1' for p in d['llm_providers'])")" = False ]; } && return 0
  echo "!! the stub provider $1 may be left in Platform Settings (GET /settings -> $code); delete it there"
  return 1
}

remove_providers() { # NAME...
  local code patch
  code="$(api GET /settings)"
  [ "$code" = 200 ] || { echo "!! providers $* left behind: GET /settings -> $code. Platform Settings 에서 지운다" >&2; return 0; }
  patch="$(providers_without "$WORK/body" "$@")" || return 0
  code="$(api PATCH /settings "$patch")"
  if [ "$code" = 200 ]; then
    echo ">> providers $* removed from Platform Settings"
  else
    echo "!! providers $* left behind: PATCH /settings -> $code. Platform Settings 에서 지운다" >&2
  fi
}

put_provider_key() { # STEP NAME KEY
  local code
  code="$(api PUT "/settings/llm-providers/$2/api-key" "{\"api_key\": \"$3\"}")"
  [ "$code" = 200 ] || die "$1" "PUT /settings/llm-providers/$2/api-key -> $code"
}

# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
PG_VECTOR=""
install_postgres_database() { # STEP NAME NS CLUSTER_ID
  local step="$1" name="$2" ns="$3" body
  PG_VECTOR="$("${K[@]}" -n "$ns" exec statefulset/postgres -c postgres -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select default_version from pg_available_extensions where name = '"'"'vector'"'"'"')"
  [ -n "$PG_VECTOR" ] || die "$step" "Postgres 에 vector 확장이 없다"
  # 본문을 변수에 먼저 둔다. register_data_service 의 인자 자리에서 만들면 python 이 실패해도 set -e 가 멈추지 않는다
  body="$(python3 -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "display_name": "설치본 Postgres", "cluster_id": sys.argv[2], "kind": "postgres",
  "host": sys.argv[3], "port": 5432, "database": sys.argv[4], "username": sys.argv[5], "password": sys.argv[6]}))
' "$name" "$4" "postgres.$ns.svc" "$(secret_field "$ns" daegam-postgres-auth database)" \
    "$(secret_field "$ns" daegam-postgres-auth username)" "$(secret_field "$ns" daegam-postgres-auth password)")"
  register_data_service "$step" "$name" "$body"
}
