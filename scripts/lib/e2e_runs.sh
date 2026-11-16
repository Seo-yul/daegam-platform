# shellcheck shell=bash
# 자동 실행(P41)을 넣고 그 기록을 기다리는 한 벌 (e2e_runs·e2e_install STEP 8r). `. "$HERE/lib/e2e_runs.sh"` 로 읽는다.
# 부르는 쪽이 e2e_json.sh(json_of)·e2e_api.sh(api·jq_)·e2e_sse.sh(msg)·e2e_report.sh(die)를 먼저 읽는다. 호출은 api 가
# 싣는 AUTH 의 사람으로 간다. 다른 사람으로 부를 때는 그 함수 안에서 `local AUTH=(...)` 로 바꾼다(bash 의 local 은 부른
# 함수에도 보인다). run_schedule_due·run_documents 를 부르는 쪽은 e2e_turn_tokens.sh(platform_mongo)도 읽는다.
#
# RUN_TERMINAL
#   더 움직이지 않는 상태들이다(P41 1절). run_wait 의 STATES 로 준다
# RUN_WAIT, RUN_POLL
#   run_wait·source_run 이 기다리는 한도와 간격(초)이다. 한도는 E2E_RUNS_WAIT 로 바꾼다(기본 240). 지켜보기 워커가 조용한
#   실행을 거두는 창(턴 시간 제한 120초 + 60초)보다 길어야, 그 워커가 끝낸 실행도 여기서 끝난 상태로 읽는다
# runs_on
#   GET /me/runs-as-me 가 200 이면 0 이다. 실행을 켜지 않은 배포(DAEGAM_RUNS_ENABLED)는 실행 라우트가 모두 404 다
# run_now STEP AGENT_ID TEXT
#   TEXT 로 지금 실행을 넣고(202) 그 기록의 id 를 RUN_ID 에 남긴다. 아니면 die STEP
# run_wait STEP AGENT_ID RUN_ID STATES [SECONDS]
#   기록의 상태가 STATES(공백으로 가른 이름) 가운데 하나가 될 때까지 RUN_POLL 초마다 본다(기본 RUN_WAIT 초). 기록은
#   $WORK/run.json 에, 상태는 RUN_STATUS 에 남는다. 넘기면 마지막 기록을 찍고 die STEP
# run_field EXPR
#   run_wait 이 남긴 기록($WORK/run.json)을 json_of 로 읽는다
# source_run STEP AGENT_ID SOURCE_ID [SECONDS]
#   그 스케줄이나 트리거의 기록(건너뛴 울림의 skipped 행은 빼고)이 생길 때까지 RUN_POLL 초마다 실행 기록을 본다(기본
#   RUN_WAIT 초). 가장 먼저 만든 것의 id 를 RUN_ID 에 남긴다. 넘기면 die STEP
# run_sessions AGENT_ID
#   그 Agent 의 실행 기록에 있는 세션 id 를 RUN_SIDS 배열에 남긴다. Agent 를 지우기 전에 부른다(지운 뒤에는 목록이 404 다).
#   읽지 못하면 RUN_SIDS 가 비고 1 이다
# run_schedule_due SCHEDULE_ID
#   그 스케줄의 next_run_at 을 지금으로 당긴다. 플랫폼 MongoDB 의 문서 한 건을 고치는 것이고, 5분 하한을 낮추는 대신이다
#   (P41 F7). 다음 발사 틱(15초마다)이 그 울림을 기록 하나로 쏘고, 시작 워커가 그것을 시작한다. 고친 문서가 한 건이 아니면
#   0 이 아니다
# schedule_moved_on FILE SCHEDULE_ID RUN_ID
#   GET .../schedules 본문 FILE 에서 그 일정의 next_run_at 이 지금보다 뒤이고 last_run_id 가 RUN_ID 면 0 이다
# audit_since ISO SECONDS
#   ISO 에서 SECONDS 초 앞의 시각을 GET /audit 의 since 꼴(초까지, Z)로 찍는다
# run_documents TENANT AGENT_ID OUT
#   그 Agent 의 트리거·실행 기록과 웹훅 비밀 장부 문서를 MongoDB 에서 읽어 OUT 에 JSON 한 덩이로 쓴다. 테넌트와 Agent 의
#   색인으로 찾는 몇 건이다. 읽지 못하면 0 이 아니다
# secret_leaks FILE...
#   표준 입력으로 받은 whsec_ 비밀의 키 부분이 든 FILE 들을 공백으로 이어 찍는다. 비밀은 프로세스 인자에 오르지 않는다
# hook_audit FILE RUN_ID TENANT AGENT_ID
#   GET /audit 본문 FILE 에서 "그 run_id 의 hook_accepted 가 있는가(True|False) 그 Agent 의 hook_refused 수" 를 찍는다.
#   막은 호출은 Agent 와 분마다 한 행에 세므로 행의 count 를 더한다
# schedule_body CRON MESSAGE
#   하루짜리 Asia/Seoul 일정의 만들기 본문을 찍는다

# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
RUN_TERMINAL="succeeded failed cancelled refused"
RUN_WAIT="${E2E_RUNS_WAIT:-240}"
RUN_POLL=2

runs_on() {
  [ "$(api GET /me/runs-as-me)" = 200 ]
}

# shellcheck disable=SC2034  # RUN_ID 는 부르는 쪽이 읽는다
RUN_ID=""
run_now() { # STEP AGENT_ID TEXT
  local code
  code="$(api POST "/agents/$2/runs" "$(msg "$3")")"
  [ "$code" = 202 ] || die "$1" "POST /agents/$2/runs -> $code"
  RUN_ID="$(jq_ 'd["id"]')"
}

# shellcheck disable=SC2034  # RUN_STATUS 는 부르는 쪽이 읽는다
RUN_STATUS=""
run_wait() { # STEP AGENT_ID RUN_ID STATES [SECONDS]
  local step="$1" path="/agents/$2/runs/$3" want=" $4 " code i
  RUN_STATUS=""
  for ((i = 0; i < ${5:-$RUN_WAIT} / RUN_POLL; i++)); do
    code="$(API_OUT="$WORK/run.json" api GET "$path")"
    if [ "$code" = 200 ]; then
      RUN_STATUS="$(run_field 'd["status"]')"
      case "$want" in *" $RUN_STATUS "*) return 0 ;; esac
    fi
    sleep "$RUN_POLL"
  done
  die "$step" "run $3 is '$RUN_STATUS' after ${5:-$RUN_WAIT}s (GET $path -> $code), want one of [$4]" "$(cat "$WORK/run.json")"
}

run_field() { json_of "$WORK/run.json" "$1"; } # EXPR

source_run() { # STEP AGENT_ID SOURCE_ID [SECONDS]
  local step="$1" code i
  RUN_ID=""
  for ((i = 0; i < ${4:-$RUN_WAIT} / RUN_POLL; i++)); do
    code="$(api GET "/agents/$2/runs?limit=500")"
    if [ "$code" = 200 ]; then
      RUN_ID="$(jq_ "next((r['id'] for r in sorted(d['runs'], key=lambda r: r['created_at']) if r.get('source_id') == '$3' and r['status'] != 'skipped'), '')")"
      [ -z "$RUN_ID" ] || return 0
    fi
    sleep "$RUN_POLL"
  done
  die "$step" "no run of source $3 within ${4:-$RUN_WAIT}s (GET /agents/$2/runs -> $code)"
}

RUN_SIDS=()
run_sessions() { # AGENT_ID
  RUN_SIDS=()
  [ "$(api GET "/agents/$1/runs?limit=500")" = 200 ] || return 1
  read -r -a RUN_SIDS <<<"$(jq_ '" ".join(sorted({r["session_id"] for r in d["runs"] if r.get("session_id")}))')"
}

run_schedule_due() { # SCHEDULE_ID
  local js
  js="$(python3 -c 'import json, sys
print("print(db.agent_schedules.updateOne(%s, {$set: {next_run_at: new Date()}}).matchedCount)" % json.dumps({"_id": sys.argv[1]}))' "$1")" || return 1
  [ "$(platform_mongo "$js")" = 1 ]
}

schedule_moved_on() { # FILE SCHEDULE_ID RUN_ID
  python3 -c 'import json, sys
from datetime import datetime, timezone
s = next((s for s in json.load(open(sys.argv[1]))["schedules"] if s["id"] == sys.argv[2]), None)
at = s and datetime.fromisoformat(s["next_run_at"].replace("Z", "+00:00"))
sys.exit(0 if s and at > datetime.now(timezone.utc) and s["last_run_id"] == sys.argv[3] else 1)' "$1" "$2" "$3"
}

audit_since() { # ISO SECONDS
  python3 -c 'import sys
from datetime import datetime, timedelta
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")) - timedelta(seconds=int(sys.argv[2]))
print(t.strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1" "$2"
}

run_documents() { # TENANT AGENT_ID OUT
  local js
  js="$(python3 -c 'import json, sys
q = json.dumps({"tenant": sys.argv[1], "agent_id": sys.argv[2]})
print("print(EJSON.stringify({triggers: db.agent_triggers.find(%s).toArray(), runs: db.agent_runs.find(%s).toArray(), "
      "ledger: db.agent_hook_secrets.find(%s).toArray()}))" % (q, q, json.dumps({"_id": sys.argv[2]})))' "$1" "$2")" || return 1
  platform_mongo "$js" >"$3" || return 1
  json_of "$3" 'len(d["triggers"]), len(d["runs"])' >/dev/null
}

secret_leaks() { # FILE...
  python3 -c 'import sys
key = sys.stdin.read()[len("whsec_"):]
print(" ".join(f for f in sys.argv[1:] if key and key in open(f, errors="replace").read()))' "$@"
}

schedule_body() { # CRON MESSAGE
  python3 -c 'import json, sys
print(json.dumps({"cron": sys.argv[1], "time_zone": "Asia/Seoul", "message": sys.argv[2], "expires_in_days": 1}))' "$1" "$2"
}

hook_audit() { # FILE RUN_ID TENANT AGENT_ID
  json_of "$1" "any(e['action'] == 'hook_accepted' and e.get('resource_id') == '$2' for e in d), sum(e.get('count') or 1 for e in d if e['action'] == 'hook_refused' and e.get('tenant') == '$3' and e.get('resource_id') == '$4')"
}
