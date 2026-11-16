# shellcheck shell=bash
# e2e 게이트가 함께 쓰는 보고 어휘. `. "$HERE/lib/e2e_report.sh"` 로 읽는다. finish 가 lib/e2e_exit.sh 의
# gate_done 을 부르므로 그 파일을 먼저 읽어 둔다(게이트는 맨 앞에서 읽는다).
#
# 종료 코드 계약 (이 저장소의 e2e 스크립트 전부에 해당한다. 여기 한 곳에만 적는다)
#   0      E2E PASS         모든 STEP 이 통과했다
#   1      E2E FAIL         실패한 STEP 이 있다. set -e 가 멈춘 명령처럼 그 밖의 실패도 모두 1 이다
#   2      E2E INCOMPLETE   실패는 없지만 건너뛴 STEP 이 있다
#   128+N  E2E INTERRUPTED  신호 N(INT 130·TERM 143·HUP 129)으로 멈췄다. 아무것도 증명하지 않았다
# 이 밖의 코드는 나가지 않는다. 그것을 지키는 것이 lib/e2e_exit.sh 다(P64). Makefile 과 CI 는 0 만 초록으로 본다.
#
# 실패를 다루는 길이 둘이고 이름이 곧 뜻이다.
#   record_fail STEP MSG  실패를 세고 다음 STEP 으로 간다. 끝의 finish 가 그 수를 종료 코드로 옮긴다
#   die STEP MSG [BODY]   마지막 응답 본문(BODY 를 주면 그것)을 stderr 에 찍고 그 자리에서 끝낸다.
#                         stderr 라 $( ) 안에서 불러도 줄은 보인다(끝나는 것은 그 서브셸뿐이다)
# 건너뛰는 길도 둘이다.
#   skip STEP MSG         돌아야 했는데 돌지 못했다. 세어서 종료 코드 2 를 만든다
#   cond_skip STEP MSG    조건(모드·키·대상)이 안 맞아 돌지 않는 STEP 이다. 실패도 미완도 아니라 세지 않는다
# 첫 실패에서 바로 멈추는 게이트(e2e_gateway.sh·e2e_mcp_auth.sh)도 이 파일을 읽어 die 와 finish 를
# 쓴다. 실패를 셀 일이 없을 뿐이라 종료 코드는 0 아니면 1 이다. STEP 줄을 찍지 않는 e2e_okta.sh 는
# 까닭 한 줄짜리 die(lib/die.sh)를 쓴다.
#
# die 와 finish 는 정리 함수가 도는 동안(lib/e2e_exit.sh 의 E2E_CLEANING) 끝내지 않고 줄만 찍고 돌아온다.
# 정리 단계의 실패가 게이트의 종료 코드를 바꾸지 않게 하기 위해서다.
#
# 출력 모양 `STEP <n> PASS|SKIP|FAIL: <msg>` 는 문서(docs/guide/e2e.md)와
# 사람이 grep 하므로 바꾸지 않는다. pass 는 MSG 를 빼면 `STEP <n> PASS` 만 찍는다. 읽는 쪽은 마지막 응답 본문을
# $WORK/body 에 두고, 다른 본문을 보일 때는 die 의 셋째 인자로 넘긴다.

FAILED=0
SKIPPED=0

pass() { echo "STEP $1 PASS${2:+: $2}"; }
skip() { echo "STEP $1 SKIP: $2"; SKIPPED=$((SKIPPED + 1)); }
cond_skip() { echo "STEP $1 SKIP (conditional): $2"; }
record_fail() { echo "STEP $1 FAIL: $2"; FAILED=$((FAILED + 1)); }

die() { # STEP MSG [BODY]
  if [ "${E2E_CLEANING:-0}" = 1 ]; then
    echo "!! cleanup: STEP $1: $2" >&2
    return 1
  fi
  {
    record_fail "$1" "$2"
    echo "--- last response body:"
    if [ $# -ge 3 ]; then
      printf '%s\n' "$3"
    elif [ -n "${WORK:-}" ]; then
      cat "$WORK/body" 2>/dev/null || true
    fi
    echo
  } >&2
  finish
}

finish() {
  [ "${E2E_CLEANING:-0}" != 1 ] || return 0
  # gate_done 은 lib/e2e_exit.sh 에 있다. 게이트가 끝까지 왔다는 표시라 그것 없이 0 으로 나가면 실패가 된다
  gate_done
  echo
  if [ "$FAILED" -gt 0 ]; then echo "E2E FAIL ($FAILED failed, $SKIPPED skipped)"; exit 1; fi
  if [ "$SKIPPED" -gt 0 ]; then echo "E2E INCOMPLETE ($SKIPPED steps skipped)"; exit 2; fi
  echo "E2E PASS"; exit 0
}
