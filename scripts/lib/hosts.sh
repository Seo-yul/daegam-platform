# shellcheck shell=bash
# 설치 벌의 플랫폼 호스트를 읽는다 (D58). `. "$HERE/lib/hosts.sh"` 로 읽는다. 호스트 글자는 설치 값 파일(저장소 밖)과
# 로컬 차트 값 deploy/dev/values-local.yaml 에만 두고 스크립트에는 적지 않는다(platform/tests/test_host_literals.py).

# env_value FILE KEY  env 파일의 KEY 값이다. 없으면 빈 줄이다. export 하지 않는다
env_value() {
  sed -n "s/^$2=//p" "$1"
}

# env_host FILE  env 파일의 DAEGAM_HOST 값이다. 없으면 빈 줄이다. bash 가 아닌 셸(zsh)도 부른다
env_host() {
  env_value "$1" DAEGAM_HOST
}

# local_host  LOCAL_HOST 가 있으면 그 값이고 없으면 로컬 차트 값(deploy/dev/values-local.yaml)의 맨 위 host 줄이다. 환경의
# DAEGAM_HOST 는 보지 않는다. 설치 값을 export 한 셸에서도 로컬 배포가 공개 호스트를 가져가지 않게 하려는 것이다.
# Makefile 의 UI_FULL_HOST 가 이것을 부른다
local_host() {
  printf '%s\n' "${LOCAL_HOST:-$(sed -n 's/^host: *//p' "$(dirname "${BASH_SOURCE[0]}")/../../deploy/dev/values-local.yaml")}"
}

# env_defaults FILE  env 파일의 값을 export 하되 이미 환경에 비어 있지 않은 값이 있으면 그것을 둔다. 설치 값 파일의
# 규칙이다(e2e_install·e2e_pat 과 설치 값 파일을 읽는 배포). 주석 줄과 빈 줄은 건너뛴다
env_defaults() {
  local key value
  while IFS='=' read -r key value; do
    case $key in '' | \#*) continue ;; esac
    if [ -z "${!key:-}" ]; then export "$key=$value"; fi
  done <"$1"
}

# install_env_file  설치한 인스턴스의 설치 값 파일 경로다. 설치 값(호스트, 레지스트리, 컨텍스트)은 저장소에 두지
# 않으므로 파일은 저장소 밖에 있다. INSTALL_ENV_FILE 이 있으면 그 파일이고, 없으면 사용자 설정 폴더의
# daegam/install.env 다. 어느 checkout 과 worktree 에서 돌아도 같은 파일을 읽는다. 없으면 예시 파일을 알려 주고 실패한다
install_env_file() {
  local file="${INSTALL_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/daegam/install.env}"
  [ -f "$file" ] && { printf '%s\n' "$file"; return 0; }
  echo "설치 값 파일 $file 가 없다: deploy/install.env.example 을 그 자리에 복사해 채운다 (INSTALL_ENV_FILE 로 다른 경로를 줄 수 있다)" >&2
  return 1
}

# install_env_defaults  install_env_file 의 값을 env_defaults 로 읽는다. 파일이 없으면 실패한다. KUBECONFIG 가 없고
# INSTALL_KUBECONFIG 가 있으면 KUBECONFIG 로 export 해 kubectl 이 그 파일을 읽는다. 옛 파일에 없는 키의 기본값은 읽는
# 쪽이 정한다(install_flag 의 DEFAULT)
install_env_defaults() {
  local file
  file="$(install_env_file)" || return 1
  env_defaults "$file"
  if [ -z "${KUBECONFIG:-}" ] && [ -n "${INSTALL_KUBECONFIG:-}" ]; then export KUBECONFIG="$INSTALL_KUBECONFIG"; fi
}

# install_flag KEY DEFAULT  설치 값 KEY(환경의 값, install_env_defaults 가 파일에서 채운다)의 true·false 를 찍는다. 비면
# DEFAULT 다. 다른 값이면 까닭을 stderr 에 찍고 1 이다
install_flag() {
  local value="${!1:-$2}"
  case "$value" in
    true | false) printf '%s\n' "$value" ;;
    *) echo "$1 는 true 나 false 다 (got $value)" >&2; return 1 ;;
  esac
}

# install_count KEY  설치 값 KEY 의 1 에서 99 사이의 수를 찍는다. 비면 빈 줄이다. 다른 값이면 까닭을 stderr 에 찍고 1 이다
install_count() {
  local value="${!1:-}"
  case "$value" in
    '' | [1-9] | [1-9][0-9]) printf '%s\n' "$value" ;;
    *) echo "$1 는 비우거나 1 에서 99 사이의 수다 (got $value)" >&2; return 1 ;;
  esac
}

# install_env_value KEY  install_env_file 의 KEY 값이다. export 하지 않는다. 파일이 없으면 실패하고 키가 없으면 빈 줄이다
install_env_value() {
  local file
  file="$(install_env_file)" || return 1
  env_value "$file" "$1"
}

# install_extra_gates  make e2e-install-extra 가 이 후보에 도는 설치본 게이트를 그 차례로, 공백으로 나눈 한 줄로 찍는다
# (scripts/lib/e2e_select.py install). E2E_INSTALL_EXTRA 가 있으면 그 이름들(all 도 된다)이고, 없으면 이 checkout 에 있는
# annotated 태그 E2E_TAG 메시지의 e2e: 줄이다. 둘 다 없으면 2 다. 태그가 annotated 태그가 아니거나 모르는 이름이면 까닭을
# stderr 에 찍고 1 이다
install_extra_gates() {
  local root message
  root="$(dirname "${BASH_SOURCE[0]}")/../.."
  if [ -n "${E2E_INSTALL_EXTRA:-}" ]; then
    message="e2e: $E2E_INSTALL_EXTRA"
  elif [ -n "${E2E_TAG:-}" ]; then
    [ "$(git -C "$root" cat-file -t "refs/tags/$E2E_TAG" 2>/dev/null)" = tag ] \
      || { echo "$E2E_TAG is not an annotated tag here" >&2; return 1; }
    message="$(git -C "$root" cat-file tag "$E2E_TAG")"
  else
    return 2
  fi
  python3 "$root/scripts/lib/e2e_select.py" install <<<"$message" \
    || { echo "the e2e line names a gate that does not exist" >&2; return 1; }
}

# install_extra_runs GATE  이 후보의 make e2e-install-extra 가 GATE 를 돌면 0 이다(install_extra_gates). 돌지 않거나 후보를
# 모르면(E2E_TAG 도 E2E_INSTALL_EXTRA 도 없다) 1 이고, 태그나 그 줄을 읽지 못하면 2 다. make e2e-install 이 이것으로 같은
# 후보의 e2e-runs 가 다시 보는 STEP 8r 을 건너뛴다
install_extra_runs() {
  local gates rc=0
  gates="$(install_extra_gates)" || rc=$?
  case $rc in 0) ;; 2) return 1 ;; *) return 2 ;; esac
  case " $gates " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}
