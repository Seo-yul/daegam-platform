# shellcheck shell=bash
# 플랫폼 경로로 채팅 SSE 를 보내고 읽는 한 벌 (e2e_agent·e2e_assistant·e2e_chat·e2e_install·e2e_knowledge·e2e_telemetry).
# `. "$HERE/lib/e2e_sse.sh"` 로 읽는다. 부르는 쪽이 API·WORK·TENANT 와 AUTH(관리자 Bearer 헤더 배열. 빈 배열은 header 모드다)를 둔다.
#
# chat PATH JSON [CURL_ARG...]
#   PATH(/agents/{id}/chat, …/regenerate 처럼 SSE 를 내는 POST)에 JSON 을 보내고 상태 코드를 찍는다. 본문은 SSE_OUT(기본
#   $WORK/sse)에 남고, SSE 가 아닌 오류 응답(4xx JSON)도 같은 파일에 남는다. curl 의 --max-time 은 CHAT_MAX_TIME(기본
#   180)초다. JSON 이 @FILE 이면 그 파일을 보낸다. JSON 뒤의 인자는 curl 에 그대로 넘긴다(헤더를 더 붙일 때 쓴다).
#   배경으로 돌리는 턴은 `SSE_OUT=FILE chat …` 처럼 앞에 붙여 파일을 가른다
# sse_ EXPR [FILE]
#   FILE(기본 $WORK/sse)의 SSE 를 (event, data) 목록 events 로 풀어 파이썬 식 EXPR 을 찍는다. names 는 이벤트 이름 목록,
#   first(n) 은 그 이름의 첫 data, every(n) 은 그 이름의 data 목록, nth(n, i) 는 그 이름의 i 번째 data, inner(text) 는 바깥
#   내용 감싸개(<outside_content source="…">, P43) 안의 글이고 감싸지 않은 글은 그대로다. content(i) 는 i 번째 tool_result 의
#   content 를 감싸개 안에서 꺼내 JSON 으로 푼 것이다. 풀지 못하면 빈 줄이다
# msg TEXT [SESSION_ID [UPLOAD_ID...]]
#   /chat 본문 {message, session_id?, attachments?} 를 찍는다. 세션 id 를 주면 그 세션의 다음 턴이고, 빈 값이면 새 세션이다.
#   업로드 id 를 주면 그 업로드를 첨부로 싣는다(P36 9절)
# stub_call TOOL ARGS_JSON
#   스텁 모델이 부를 도구 조각 {"tool": TOOL, "args": ARGS} 를 찍는다. 스텁은 메시지 안의 이 조각으로 도구와 인자를 정한다
#   (agent-runtime/runtime/stub.py)
# task_call SUBAGENT DESCRIPTION
#   서브에이전트 SUBAGENT 에게 DESCRIPTION 을 맡기는 task 도구 조각을 찍는다. 서브에이전트의 스텁은 DESCRIPTION 을 사람의
#   말로 받으므로, 그 안에 stub_call 조각을 두면 서브에이전트가 그 도구를 부른다
# asked TOOL REASON [FILE]
#   FILE(기본 $WORK/sse)의 스트림이 TOOL 을 REASON(P43 의 이유 rule·outside)으로 묻는 interrupt 로 끝났으면 0 이다
#
# 뒤에서 여는 턴(P78, D92). 끊어도 턴은 끝까지 가므로 중지 API 로 멈추고 그 스트림의 끝을 본다(e2e_agent·e2e_telemetry)
# now_ms                              지금 시각(ms)을 찍는다
# sse_bg URL JSON [CURL_ARG...]       SSE 요청을 뒤에서 열고 curl pid 를 BG_PID 에 남긴다. 본문은 $WORK/bg.sse, 응답 머리는
#                                     $WORK/bg.hdr 다. 인증 머리는 부르는 쪽이 curl 인자로 준다
# sse_bg_as NAME URL JSON [CURL_ARG...]  sse_bg 와 같되 본문과 머리를 $WORK/NAME.sse·$WORK/NAME.hdr 에 두고 curl pid 를
#                                     BG_AS_PID 에 남긴다. 턴 여럿을 함께 열어 둘 때 쓴다(e2e_agent 10d)
# chat_bg PATH JSON                   플랫폼 경로(/api/agents/…)로 여는 sse_bg 다
# chat_bg_as NAME PATH JSON           플랫폼 경로로 여는 sse_bg_as 다
# bg_session_id [NAME]                뒤 스트림(기본 bg)의 응답 머리 X-Daegam-Session-Id 를 찍는다. 5초 안에 오지 않으면 빈 값이다
# bg_stop                             뒤 curl 을 죽여 연결을 끊는다. 이미 끝났으면 거두기만 한다
# ends_by PID DEADLINE_MS             그 curl 이 그 시각(now_ms) 전에 스스로 끝나면 0 이다. 넘기면 죽이고 1 이다. 어느 쪽이든 거둔다
# bg_ends_by DEADLINE_MS              뒤 curl(BG_PID)의 ends_by 다
# expect_stream_error STEP FILE CODE
#   FILE 의 SSE 마지막 이벤트가 error{code: CODE} 여야 한다. 아니면 그 본문을 $WORK/body 에 두고 die STEP 이다
# expect_cancelled_stream STEP CANCEL_MS
#   CANCEL_MS(now_ms)에 부른 중지 뒤 CANCEL_END_WITHIN(기본 3)초 안에 뒤 스트림이 끝나고 마지막 이벤트가
#   error{code: turn_cancelled} 여야 한다. 아니면 die STEP 이다. 끝나기까지 걸린 ms 를 ENDED_MS 에 남긴다

chat() { # PATH JSON [CURL_ARG...]
  local path="$1" body="$2"
  shift 2
  curl -sS -N --max-time "${CHAT_MAX_TIME:-180}" -o "${SSE_OUT:-$WORK/sse}" -w '%{http_code}' -X POST "$API$path" \
    ${AUTH[@]+"${AUTH[@]}"} -H "X-Daegam-Tenant: $TENANT" -H 'Content-Type: application/json' -d "$body" "$@" || true
}

sse_() { # EXPR [FILE]
  python3 -c '
import json, re, sys
events = []
for block in open(sys.argv[1], encoding="utf-8").read().split("\n\n"):
    ev, data = None, []
    for line in block.splitlines():
        if line.startswith("event: "): ev = line[7:]
        elif line.startswith("data: "): data.append(line[6:])
    if ev: events.append((ev, json.loads("\n".join(data)) if data else {}))
names = [e for e, _ in events]
def every(n): return [d for e, d in events if e == n]
def first(n): return next(iter(every(n)), None)
def nth(n, i): return every(n)[i]
def inner(text):
    m = re.fullmatch(r"<outside_content source=\"[^\"]*\">\n(.*)\n</outside_content>", text, re.DOTALL)
    return m.group(1) if m else text
def content(i): return json.loads(inner(nth("tool_result", i)["content"]))
v = eval(sys.argv[2])
print(*v) if isinstance(v, tuple) else print(v)
' "${2:-$WORK/sse}" "$1" 2>/dev/null || true
}

msg() { # TEXT [SESSION_ID [UPLOAD_ID...]]
  python3 -c 'import json,sys
text, sid, ids = sys.argv[1], (sys.argv[2:3] or [""])[0], sys.argv[3:]
print(json.dumps({"message": text, **({"session_id": sid} if sid else {}), **({"attachments": ids} if ids else {})}, ensure_ascii=False))' "$@"
}

stub_call() { # TOOL ARGS_JSON
  python3 -c 'import json,sys; print(json.dumps({"tool": sys.argv[1], "args": json.loads(sys.argv[2])}, ensure_ascii=False))' "$1" "$2"
}

task_call() { # SUBAGENT DESCRIPTION
  python3 -c 'import json,sys
print(json.dumps({"tool": "task", "args": {"description": sys.argv[2], "subagent_type": sys.argv[1]}}, ensure_ascii=False))' "$1" "$2"
}

asked() { # TOOL REASON [FILE]
  [ "$(sse_ 'names[-1]' "${3:-$WORK/sse}")" = interrupt ] \
    && [ "$(sse_ '(lambda a: (a["name"], a.get("reason")))(first("interrupt")["action_requests"][0])' "${3:-$WORK/sse}")" = "$1 $2" ]
}

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

sse_bg_as() { # NAME URL JSON [CURL_ARG...]
  local name="$1" url="$2" body="$3"; shift 3
  rm -f "$WORK/$name.sse" "$WORK/$name.hdr"
  curl -sS -N --max-time 120 -o "$WORK/$name.sse" -D "$WORK/$name.hdr" -X POST "$url" \
    -H 'Content-Type: application/json' -d "$body" "$@" 2>/dev/null 8>&- 9>&- &
  BG_AS_PID=$!
}

sse_bg() { # URL JSON [CURL_ARG...]
  sse_bg_as bg "$@"
  BG_PID=$BG_AS_PID
}

chat_bg_as() { sse_bg_as "$1" "$API$2" "$3" ${AUTH[@]+"${AUTH[@]}"} -H "X-Daegam-Tenant: $TENANT"; }

chat_bg() { sse_bg "$API$1" "$2" ${AUTH[@]+"${AUTH[@]}"} -H "X-Daegam-Tenant: $TENANT"; }

bg_session_id() { # [NAME]
  local i sid=""
  for ((i = 0; i < 25; i++)); do
    sid="$(hdr x-daegam-session-id "$WORK/${1:-bg}.hdr")"
    [ -z "$sid" ] || break
    sleep 0.2
  done
  echo "$sid"
}

bg_stop() {
  [ -n "${BG_PID:-}" ] || return 0
  kill "$BG_PID" 2>/dev/null || true
  wait "$BG_PID" 2>/dev/null || true
  BG_PID=""
}

ends_by() { # PID DEADLINE_MS
  while kill -0 "$1" 2>/dev/null; do
    if [ "$(now_ms)" -ge "$2" ]; then
      kill "$1" 2>/dev/null || true
      wait "$1" 2>/dev/null || true
      return 1
    fi
    sleep 0.1
  done
  wait "$1" 2>/dev/null || true
}

bg_ends_by() { # DEADLINE_MS
  local rc=0
  [ -z "${BG_PID:-}" ] || ends_by "$BG_PID" "$1" || rc=1
  BG_PID=""
  return "$rc"
}

expect_stream_error() { # STEP FILE CODE
  local step="$1" file="$2" code="$3"
  # 본문 없이 끝났으면 curl 이 파일을 만들지 않는다. 그때 sse_ 는 빈 줄이라 아래 단정이 까닭을 찍는다
  { [ "$(sse_ 'names[-1]' "$file")" = "error" ] && [ "$(sse_ 'first("error")["code"]' "$file")" = "$code" ]; } \
    || { cp "$file" "$WORK/body" 2>/dev/null || :; die "$step" "the stream must end with error{code: $code}: $(sse_ 'names' "$file")"; }
}

expect_cancelled_stream() { # STEP CANCEL_MS
  local step="$1" cancel_ms="$2" within="${CANCEL_END_WITHIN:-3}"
  bg_ends_by "$((cancel_ms + within * 1000))" \
    || die "$step" "the stream did not end within ${within}s of the cancel"
  # shellcheck disable=SC2034  # 부르는 쪽이 읽는다
  ENDED_MS=$(($(now_ms) - cancel_ms))
  expect_stream_error "$step" "$WORK/bg.sse" turn_cancelled
}
