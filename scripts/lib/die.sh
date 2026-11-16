# shellcheck shell=bash
# 까닭 한 줄을 찍고 끝내는 die(P64). STEP 줄을 찍지 않는 게이트(e2e_okta)와 도구 스크립트
# (operator-install·deploy-local·resolve-images·llm_provider_local)가 `. "$HERE/lib/die.sh"` 로 읽는다.
# STEP 을 받는 die 는 lib/e2e_report.sh 에 있다.
#
# die MSG...
#   `<스크립트 이름>: FAIL: MSG` 를 stderr 에 찍고 1 로 끝낸다. e2e 게이트의 정리 함수가 도는 동안
#   (lib/e2e_exit.sh 의 E2E_CLEANING)에는 끝내지 않고 1 을 돌려준다.
die() {
  echo "${0##*/}: FAIL: $*" >&2
  [ "${E2E_CLEANING:-0}" != 1 ] || return 1
  exit 1
}
