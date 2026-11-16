#!/usr/bin/env bash
# make e2e-install-extra 의 몸통 (docs/guide/e2e.md 16절). 릴리스 후보 태그의 e2e: 줄이 고른 설치본 게이트를
# scripts/lib/e2e_select.py 의 차례로 하나씩 돈다. 릴리스 절차에서는 make e2e-install 다음에 돈다.
#   E2E_TAG            후보 태그. 이 체크아웃에 있는 서명된 태그의 메시지에서 e2e: 줄을 읽는다
#   E2E_INSTALL_EXTRA  비우지 않으면 태그 대신 이 이름들(공백으로 나눈다, all 도 된다)을 돈다
#   E2E_INSTALL_USER   관리자. 비밀번호는 e2e-install 과 같이 E2E_INSTALL_PASSWORD 나 키체인 항목 daegam-e2e-install 이다
# 나머지 값(INSTALL_CONTEXT, DAEGAM_HOST, TRAEFIK_NAMESPACE)은 설치 값 파일(~/.config/daegam/install.env)이 기본이고 환경이 이긴다.
# e2e-ui-serving 은 설치본의 Traefik 네임스페이스와 공개 호스트로, 나머지는 E2E_TARGET=install 로 부른다. 하나가 실패해도
# 나머지를 돌고, 실패한 이름을 찍은 뒤 1 로 끝난다. 게이트마다 걸린 초를 찍고 끝에 모아 찍는다. 앞뒤로 설치 값 파일의
# E2E_WATCH_RESTARTS 가 이름 붙인 파드(예: mongodb-0)의 재시작 수를 찍고, 늘었으면 경고한다. 비면 보지 않는다.
# 게이트가 끝나지 못하고 죽었을 때 되돌릴 것은 릴리스 절차가 다룬다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# die MSG 는 여기에 있다
. "$HERE/lib/die.sh"
# env_defaults 는 여기에 있다
. "$HERE/lib/hosts.sh"
install_env_defaults
[ -n "${E2E_INSTALL_USER:-}" ] || die "set E2E_INSTALL_USER (the e2e admin; deploy/install.env.example lists it)"

rc=0
gates="$(install_extra_gates)" || rc=$?
case $rc in
  0) ;;
  2) die "set E2E_TAG=<candidate tag> (or E2E_INSTALL_EXTRA=\"e2e-pat ...\")" ;;
  *) die "the candidate's e2e line could not be read (the line above says why)" ;;
esac
if [ -z "$gates" ]; then
  echo ">> e2e-install-extra: no install gate is named, nothing to run"
  exit 0
fi
# 게이트는 이번 실행의 다른 게이트가 다시 보는 단계를 건너뛴다(e2e_oauth.sh STEP 16). 그 게이트들을 이 이름으로 넘긴다. 게이트를
# 하나만 따로 돌리면 이 값이 없어 모든 단계를 돈다
export E2E_EXTRA_RUN="$gates"

K=(kubectl --context "$INSTALL_CONTEXT" -n daegam-system)
WATCHED="${E2E_WATCH_RESTARTS:-}"
# pod_restarts -> E2E_WATCH_RESTARTS 의 파드마다 `이름=첫 컨테이너의 재시작 수`(없으면 ?)를 공백으로 이어 찍는다
pod_restarts() {
  local pod out=""
  for pod in $WATCHED; do
    out="$out $pod=$("${K[@]}" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")"
  done
  printf '%s\n' "${out# }"
}
before="$(pod_restarts)"
echo ">> e2e-install-extra: $gates${WATCHED:+ (restarts: $before)}"

failed=""; times=""
for g in $gates; do
  echo ">> $g (install)"
  start=$SECONDS; verdict=PASS
  case "$g" in
    e2e-ui-serving) args=(KUBE_CONTEXT="$INSTALL_CONTEXT" TRAEFIK_NS="${TRAEFIK_NAMESPACE:-}" E2E_UI_HOST="$DAEGAM_HOST") ;;
    *) args=(E2E_TARGET=install INSTALL_CONTEXT="$INSTALL_CONTEXT" E2E_INSTALL_USER="$E2E_INSTALL_USER") ;;
  esac
  make --no-print-directory -C "$HERE/.." "$g" "${args[@]}" || { failed="$failed $g"; verdict=FAIL; }
  took=$((SECONDS - start))
  echo ">> $g $verdict in $took s"
  times="$times $g=${took}s"
done
echo ">> e2e-install-extra times:$times"

if [ -n "$WATCHED" ]; then
  after="$(pod_restarts)"
  echo ">> restarts: $before before, $after after"
  [ "$before" = "$after" ] || echo "!! a watched pod restarted during the run ($WATCHED); read its last state before the next candidate" >&2
fi
if [ -n "$failed" ]; then
  echo ">> e2e-install-extra FAIL:$failed"
  exit 1
fi
echo ">> e2e-install-extra PASS"
