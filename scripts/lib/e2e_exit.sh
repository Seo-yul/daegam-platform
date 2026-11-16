# shellcheck shell=bash
# e2e 게이트가 끝나는 길 한 벌(P64). 모든 scripts/e2e_*.sh 가 맨 앞에서 `. "$HERE/lib/e2e_exit.sh"` 로 읽고,
# 정리 함수를 만든 자리에서 `on_exit cleanup` 으로 등록한다. 게이트는 trap 을 스스로 걸지 않는다.
#
# on_exit FN
#   FN 을 정리 함수로 등록하고 INT·TERM·HUP·EXIT trap 을 건다. 정리 함수는 게이트가 나가려던 종료 코드를 첫 인자로
#   받는다(실패했을 때만 로그를 찍는 게이트가 쓴다).
#
# gate_done
#   게이트가 끝까지 왔다고 적는다. lib/e2e_report.sh 의 finish 가 부르고, finish 를 쓰지 않는 게이트는 마지막 줄을
#   찍기 전에 부른다. 이것 없이 0 이나 2 로 나가면 실패다. bash 3.2 는 set -eu 에서 정의되지 않은 변수로 멈춘
#   스크립트의 EXIT trap 에 $? 0 을 주고 0 으로 끝내고, set -e 로 멈춘 명령의 코드가 2 일 수도 있어서(grep 이 없는
#   파일을 읽을 때), 코드만 보고는 끝까지 온 것과 중간에 멈춘 것을 가를 수 없다.
#
# gate_incomplete MSG
#   돌 수 없는 조건(도구가 없다 등)을 stderr 에 찍고 2(E2E INCOMPLETE)로 끝낸다.
#
# 끝나는 길
#   - INT·TERM·HUP 을 받으면 `E2E INTERRUPTED (SIG<name>)` 을 찍고 128+신호 번호(130·143·129)로 나간다.
#   - EXIT 에서 정리 함수가 꼭 한 번 돈다. 게이트의 셸에서 돌아 게이트가 띄운 프로세스를 wait 할 수 있다
#     (lib/e2e_local_platform.sh 의 stop_server). set +e +u 로 돌고, 도는 동안 die·finish 는 줄만 찍고 돌아온다.
#     정리 함수에는 exit 를 두지 않는다. 그래서 정리 단계의 실패는 경고 줄일 뿐 종료 코드를 바꾸지 않는다.
#     정리 함수는 게이트가 나갈 코드를 첫 인자로 받는다.
#   - 종료 코드는 계약(lib/e2e_report.sh 머리말) 안의 것만 나간다. 1 은 그대로, 0·2 는 gate_done 뒤에만 그대로,
#     신호는 128+N, 그 밖의 코드(set -e 가 멈춘 명령의 코드, 예를 들어 curl 의 28)와 gate_done 없는 0·2 는
#     `E2E FAIL (exit N)` 을 찍고 1 이다.
#   - 신호는 받은 수로 다룬다. 프로세스 그룹에 간 신호(Ctrl-C, timeout, make 가 넘긴 TERM)는 전경 자식을 먼저
#     죽여 set -e 가 EXIT 로 들어간 뒤에 trap 이 돌 수 있다. 그래서 정리 중에 온 첫 신호도 그냥 첫 신호다.
#     기록하고 `E2E INTERRUPTED` 를 찍고 정리를 이어 가며, 정리가 끝나면 그 신호의 코드로 나간다(PASS 를 이미
#     찍었어도). 첫 신호와 같은 신호가 E2E_SIGNAL_REPEAT_SECONDS(기본 5초) 안에 또 오면(make 가 한 번 더 넘기는 TERM) 무시한다. 그 뒤에 온
#     신호만 정리를 멈추고 그 신호의 코드로 바로 나간다. trap 보다 먼저 set -e 가 129·130·143 으로 들어오면
#     (신호에 죽은 전경 자식의 코드다) 그것을 첫 신호로 적는다.
#     정리는 첫 신호의 trap 밖(다음 명령 직전의 DEBUG trap 이나 EXIT trap)에서 돈다. bash 는 한 신호의 trap 이
#     도는 동안 같은 신호의 trap 을 돌리지 않으므로, 그래야 그 시간 뒤의 같은 신호도 정리를 멈춘다.
#
# curl
#   이 파일을 읽은 게이트의 curl 은 아래 함수다. 연결 제한(E2E_CURL_CONNECT_TIMEOUT, 기본 5초)과 전체 제한
#   (E2E_CURL_MAX_TIME, 기본 120초)을 부른 쪽 인자 앞에 붙인다. curl 은 같은 옵션의 마지막 값을 쓰므로 스스로
#   --max-time 을 주는 호출(스트림, 긴 기다림)은 제 값을 쓴다. kubectl exec … -- curl 처럼 파드 안의 curl 은 상관없다.
#
# bash 3.2 (macOS /bin/bash)에서 돈다. 남은 한계 둘.
#   - bash 3.2 는 그룹 SIGINT 에 죽은 전경 명령의 실패를 게이트가 무시하는 자리(`… || true`)에서 드물게 SIGINT 를
#     버리고 다음 명령으로 간다(60번에 2번 잰 것, bash 5.3 은 0번).
#   - 게이트의 pid 에만 보낸 SIGINT 는 전경 자식이 도는 동안이면 bash 가 버린다. 자식이 SIGINT 를 받지 않고 제대로
#     끝나면 bash 는 자식이 그것을 처리했다고 본다(wait-and-cooperative-exit). Ctrl-C 처럼 그룹에 보내거나 TERM 을 쓴다.

E2E_CLEANUP=""    # on_exit 가 등록한 정리 함수 이름
E2E_SIGNAL=""     # 받은 첫 신호 이름. 비었으면 신호로 끝나는 것이 아니다
E2E_SIGNAL_CODE=0 # 첫 신호의 종료 코드(128+N)
E2E_SIGNAL_AT=0   # 첫 신호를 받은 때(bash 의 SECONDS)
# 같은 신호의 사본으로 보는 시간(초). SECONDS 는 정수라 실제 간격이 1초 짧게 잴 수 있어 이 값까지 포함한다.
# 부하가 큰 머신에서는 make 가 넘기는 TERM 이 1~2초 늦게 와서 2초로는 모자랐다
E2E_SIGNAL_REPEAT_SECONDS="${E2E_SIGNAL_REPEAT_SECONDS:-5}"
E2E_CLEANING=0    # 정리 함수가 도는 동안 1. lib/e2e_report.sh 의 die·finish 와 lib/die.sh 의 die 가 읽는다
E2E_DONE=0        # gate_done 이 1 로 둔다

gate_done() { E2E_DONE=1; }

gate_incomplete() { # MSG
  echo "$1" >&2
  gate_done
  exit 2
}

curl() {
  command curl --connect-timeout "${E2E_CURL_CONNECT_TIMEOUT:-5}" --max-time "${E2E_CURL_MAX_TIME:-120}" "$@"
}

on_exit() { # FN
  E2E_CLEANUP="$1"
  # 게이트의 stdout·stderr 를 8·9 에 둔다. trap 은 그때 돌던 명령의 리디렉션 안에서 돈다(bash 3.2·5.3 모두).
  # 예를 들어 `curl … >/dev/null 2>&1` 이 Ctrl-C 에 끝나면 trap 의 출력과 정리 함수의 출력이 /dev/null 로 간다.
  # 그래서 _e2e_signal 과 _e2e_exit 는 이 둘로 쓴다
  exec 8>&1 9>&2
  trap '_e2e_signal INT 130' INT
  trap '_e2e_signal TERM 143' TERM
  trap '_e2e_signal HUP 129' HUP
  trap _e2e_exit EXIT
}

_e2e_record_signal() { # NAME CODE
  E2E_SIGNAL="$1"
  E2E_SIGNAL_CODE="$2"
  E2E_SIGNAL_AT=$SECONDS
  echo
  echo "E2E INTERRUPTED (SIG$1)"
}

_e2e_exiting() { # 이미 EXIT 처리 안이면 0. trap 은 _e2e_exit 의 첫 줄보다 먼저 돌 수도 있어 호출 스택도 본다
  [ "$E2E_CLEANING" = 1 ] && return 0
  case " ${FUNCNAME[*]} " in *" _e2e_exit "*) return 0 ;; esac
  return 1
}

_e2e_signal() { # NAME CODE
  # fork 한 자식이 exec 하기 전에 그룹 신호를 받으면 물려받은 이 trap 이 자식에서 돈다. 그 자식이 정리하고 130 으로
  # 끝나면 게이트의 셸은 자식이 신호를 처리했다고 보고 제 trap 을 돌리지 않은 채 다음 명령으로 간다. 그래서 자식이면
  # 그 신호의 기본 동작으로 죽어 셸이 제 trap 을 돌게 한다. bash 3.2 에는 BASHPID 가 없어 sh 의 PPID 로 제 pid 를 안다.
  # 파일은 mktemp 로 만들어 부모와 자식이 겹치지 않는다. pid 를 알지 못하면 게이트의 셸로 보고 평소대로 간다
  local me="" self
  if self="$(mktemp "${TMPDIR:-/tmp}/daegam-e2e-self.XXXXXX" 2>/dev/null)"; then
    if sh -c 'echo "$PPID"' >"$self" 2>/dev/null; then read -r me <"$self" || true; fi
    rm -f "$self"
  fi
  if [ -n "$me" ] && [ "$me" != "$$" ]; then
    trap - "$1"
    kill -"$1" "$me"
    return 0
  fi
  if [ -z "$E2E_SIGNAL" ]; then
    _e2e_record_signal "$1" "$2"
    # 이미 나가는 중이면 정리를 이어 간다. _e2e_exit 가 끝에 이 신호의 코드로 나간다
    if _e2e_exiting; then return 0; fi
    # 여기서는 exit 도 정리도 하지 않고 다음 명령 직전(DEBUG trap)에 정리하고 나간다. set -e 가 막 EXIT trap 을
    # 시작했고 그 첫 명령보다 이 trap 이 먼저 도는 경우에는 여기서 exit 하면 정리가 돌지 않는다. 또 정리를 이 trap
    # 안에서 돌리면 bash 는 같은 신호의 trap 을 이 trap 이 끝날 때까지 돌리지 않아, 그 시간 뒤의 같은 신호로도 정리를
    # 멈출 수 없다. set -T 는 함수 안의 다음 명령에도 DEBUG 를 건다
    set -T
    trap _e2e_after_signal DEBUG
    return 0
  fi
  # 같은 신호가 곧바로 한 번 더 온 것(make 가 넘긴 TERM 의 사본)은 새 신호가 아니다
  if [ "$1" = "$E2E_SIGNAL" ] && [ $((SECONDS - E2E_SIGNAL_AT)) -le "$E2E_SIGNAL_REPEAT_SECONDS" ]; then return 0; fi
  if _e2e_exiting; then
    echo "!! SIG$1 during cleanup: cleanup stopped, what it had not undone yet is left in place" >&2
    exit "$2"
  fi
  return 0
} >&8 2>&9

_e2e_after_signal() { # 첫 신호 뒤 다음 명령 직전에 DEBUG trap 으로 불린다
  if _e2e_exiting; then return 0; fi
  # set -T 라 신호 trap 의 남은 명령(return)에도 불린다. 신호 trap 을 벗어난 뒤의 명령에서만 움직인다
  case " ${FUNCNAME[*]} " in *" _e2e_signal "*) return 0 ;; esac
  # exit 하지 않고 여기서 정리하고 나간다. 다음 명령이 막 시작한 EXIT trap 의 첫 명령일 수 있는데, 그것을
  # 알아볼 길이 없고 EXIT trap 안에서 exit 하면 정리가 돌지 않는다. 이 trap 은 DEBUG 라 정리 중에 오는 신호의
  # trap 은 bash 가 막지 않는다
  trap - DEBUG
  trap - EXIT
  _e2e_exit
}

_e2e_exit() {
  local rc=$? code=1
  E2E_CLEANING=1
  trap - DEBUG
  set +e +u +T
  # 그룹 신호에 전경 자식이 먼저 죽어 set -e 로 들어온 것이다. 미뤄진 trap 은 곧 같은 신호의 사본으로 온다
  if [ -z "$E2E_SIGNAL" ]; then
    case "$rc" in
      129) _e2e_record_signal HUP 129 ;;
      130) _e2e_record_signal INT 130 ;;
      143) _e2e_record_signal TERM 143 ;;
    esac
  fi
  if [ -n "$E2E_SIGNAL" ]; then
    code="$E2E_SIGNAL_CODE"
  elif [ "$rc" = 1 ]; then
    code=1
  elif [ "$E2E_DONE" = 1 ] && { [ "$rc" = 0 ] || [ "$rc" = 2 ]; }; then
    code="$rc"
  elif [ "$rc" = 0 ]; then
    echo "E2E FAIL (exit 0 before the gate's end: an unbound variable under set -eu ends bash 3.2 with 0)"
  else
    echo "E2E FAIL (exit $rc)"
  fi
  if [ -n "$E2E_CLEANUP" ]; then "$E2E_CLEANUP" "$code"; fi
  # 정리 중에 첫 신호가 왔으면 그 신호의 코드로 나간다
  if [ -n "$E2E_SIGNAL" ]; then exit "$E2E_SIGNAL_CODE"; fi
  exit "$code"
} >&8 2>&9
