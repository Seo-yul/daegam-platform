# shellcheck shell=bash
# 플랫폼을 클러스터 없이 로컬 uvicorn 으로 띄우는 게이트(e2e-okta)의 한 벌.
# `. "$HERE/lib/e2e_local_platform.sh"` 로 읽는다.
#
# 함수는 상태 코드만 돌려주고 스스로 멈추지 않는다. 게이트마다 die 모양(`die MSG` 와 `die STEP MSG`)이
# 달라서 실패를 어떻게 알릴지는 부르는 쪽이 정한다.

# port_free PORT: 그 포트에 LISTEN 중인 것(이전 실행이 남긴 서버)이 있으면 1
port_free() { ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# uv_python DIR: DIR 에서 uv sync 를 돌리고 venv python 의 절대 경로를 찍는다. sync 가 실패하거나 python 이 없으면 1
uv_python() {
  local py
  py="$(cd "$1" && uv sync --quiet >&2 && pwd)/.venv/bin/python" || return 1
  [ -x "$py" ] || return 1
  echo "$py"
}

# start_uvicorn DIR PY MODULE PORT LOG [VAR=VAL...]
#   DIR 에서 VAR=VAL 환경을 얹어 uvicorn MODULE 을 127.0.0.1:PORT 로 띄우고 pid 를 UVICORN_PID 에 남긴다.
#   서브셸이 exec 로 uvicorn 프로세스 자체가 되어야 kill 이 실제 서버에 닿는다 (env 도 exec 한다).
#   lib/e2e_exit.sh 가 둔 게이트의 출력(8·9)은 닫고 띄운다. 남은 서버가 붙들면 `| tee` 가 끝나지 않는다.
# shellcheck disable=SC2034  # UVICORN_PID 는 부르는 쪽이 SERVER_PID 로 옮겨 담는다
start_uvicorn() {
  local dir="$1" py="$2" module="$3" port="$4" log="$5"
  shift 5
  (cd "$dir" && exec env "$@" "$py" -m uvicorn "$module" --host 127.0.0.1 --port "$port" --log-level warning >"$log" 2>&1) 8>&- 9>&- &
  UVICORN_PID=$!
}

# wait_healthy URL PID TRIES NAP: URL 이 2xx 를 줄 때까지 NAP 초 간격으로 TRIES 번 본다.
# 서버(PID)가 먼저 죽거나 횟수를 다 쓰면 1 이다. macOS 에는 timeout 이 없어 폴링으로 기다린다.
wait_healthy() {
  local url="$1" pid="$2" tries="$3" nap="$4" i
  for ((i = 0; i < tries; i++)); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    kill -0 "$pid" 2>/dev/null || return 1
    sleep "$nap"
  done
  curl -fsS "$url" >/dev/null 2>&1
}

# stop_server PID: 살아 있으면 죽이고 끝날 때까지 기다린다. 비었거나 이미 죽었으면 할 일이 없다
stop_server() {
  { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; } || return 0
  kill "$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
}

# drop_mongo_db PY URL DB: 게이트가 쓴 임시 DB 를 지운다. 실패하면 1 이다.
# PY 가 아직 없으면(uv sync 전에 실패) 만든 DB 도 없으므로 할 일이 없다.
drop_mongo_db() {
  { [ -n "$1" ] && [ -x "$1" ]; } || return 0
  "$1" -c "import pymongo,sys; pymongo.MongoClient(sys.argv[1], serverSelectionTimeoutMS=3000).drop_database(sys.argv[2])" \
    "$2" "$3" 2>/dev/null
}
