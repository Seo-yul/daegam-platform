# shellcheck shell=bash
# e2e 게이트의 port-forward 재연결 루프. `. "$HERE/lib/e2e_pf.sh"` 로 읽는다.
# 부르는 쪽이 kubectl 배열 K 와 작업 디렉터리 WORK 를 먼저 둔다 (함수는 부를 때 읽는다).
#
# 왜 재연결 루프인가. 배포 직후 Service 로 port-forward 하면 Terminating 중인 옛 파드에 붙었다가
# 조용히 끊긴다 (e2e-instance·ui-full 에서 실측). ui/e2e/full.spec.ts 의 재연결과 같은 취지로
# kubectl 이 죽으면 1초 뒤 다시 연다. start_pf 는 루프 pid 를 PF_PID 에 남기고,
# stop_pf 는 루프를 먼저 죽여 재생성을 막은 뒤 pid 파일의 kubectl 자식을 정리한다.

# 부르는 쪽이 이 pid 를 PFS 배열이나 PF_API 에 담아 두었다가 stop_pf 에 넘긴다
# shellcheck disable=SC2034
PF_PID=""

start_pf() { # NS TARGET LOCAL:REMOTE -> 루프 pid 를 PF_PID 에 남긴다
  local ns="$1" target="$2" ports="$3" pidfile="$WORK/pf-${3%%:*}.pid" stopfile="$WORK/pf-${3%%:*}.stop"
  rm -f "$stopfile"
  (
    # 부르는 스크립트의 set -e 를 물려받으면 kubectl 이 처음 0 이 아닌 값으로 끝날 때(파드가 내려가 끊기거나
    # restart_pf 가 죽일 때) wait 에서 루프째 죽어 다시 열지 못한다
    set +e
    # stop_pf 가 정지 플래그를 세우면 더 이상 재생성하지 않는다 (kill 직후 재생성 레이스 차단)
    while [ ! -e "$stopfile" ]; do
      "${K[@]}" -n "$ns" port-forward "$target" "$ports" >/dev/null 2>&1 &
      echo $! >"$pidfile"
      wait $! 2>/dev/null
      sleep 1
    done
  # 8·9 는 lib/e2e_exit.sh 가 둔 게이트의 출력이다. 남은 재연결 루프가 그것을 붙들면 `| tee` 가 끝나지 않는다
  ) >/dev/null 2>&1 8>&- 9>&- &
  PF_PID=$!
}

# kill_stale_pf PORT...: 이전 실행이 이 포트들에 남긴 port-forward 를 죽인다 (같은 포트를 잡고 조용히 끊긴 것들).
# 게이트마다 포트가 다르므로(lib/e2e_ports.sh) 자기 포트만 걷고 다른 게이트나 사람이 연 것은 그대로 둔다.
kill_stale_pf() {
  local p
  for p in "$@"; do pkill -f "port-forward .* ${p}:" 2>/dev/null || true; done
}

stop_pf() { # 루프 pid, LOCAL 포트
  local loop="${1:-}" port="${2:-}" pidfile="$WORK/pf-${2:-none}.pid" stopfile="$WORK/pf-${2:-none}.stop" i
  [ -n "$loop" ] || return 0
  touch "$stopfile"
  kill "$loop" 2>/dev/null || true
  wait "$loop" 2>/dev/null || true
  # pidfile 이 낡았을 수 있고(kill 과 기록 사이 재생성) kubectl 이 아직 LISTEN 전일 수 있다.
  # 명령행 패턴으로 잡되 사라질 때까지 잠깐 재시도한다.
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null || true
    [ -n "$port" ] && pkill -f "port-forward .* ${port}:" 2>/dev/null || true
    { [ -n "$port" ] && pgrep -f "port-forward .* ${port}:" >/dev/null 2>&1; } || break
    sleep 0.2
  done
  rm -f "$pidfile" "$stopfile"
  return 0
}

# restart_pf PORT: 루프가 띄운 kubectl 을 죽여 새 파드로 다시 열게 한다. 플랫폼을 굴린 뒤에 쓴다.
# 옛 kubectl 은 내려간 파드에 붙은 채 포트를 쥐고 있어 wait_port 는 통과하지만 요청은 빈 응답(curl 52)으로 끝난다
restart_pf() {
  local pidfile="$WORK/pf-$1.pid"
  [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null || true
}

# wait_http URL [TRIES]: URL 이 2xx 로 답할 때까지 1초 간격으로 기다린다(기본 60회). 포트가 열린 것만으로는
# 뒤의 파드가 답하는지 모르므로 롤아웃 뒤에는 wait_port 대신 이것을 본다
wait_http() {
  local url="$1" tries="${2:-60}" i
  for ((i = 0; i < tries; i++)); do
    curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# wait_port PORT [TRIES] [SLEEP]: 포트가 열릴 때까지 기다린다.
# 기본은 1초 간격 30회다. 폴링 간격이 다른 게이트는 PF_WAIT_TRIES·PF_WAIT_SLEEP 로 자기 값을 둔다
# (e2e_gateway.sh·e2e_mcp_auth.sh 는 0.5초 간격 60회). 어느 쪽이든 상한은 30초다.
wait_port() {
  local port="$1" tries="${2:-${PF_WAIT_TRIES:-30}}" nap="${3:-${PF_WAIT_SLEEP:-1}}" i
  for ((i = 0; i < tries; i++)); do
    (echo >"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return 0
    sleep "$nap"
  done
  return 1
}
