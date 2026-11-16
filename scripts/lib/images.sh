# shellcheck shell=bash
# 제품 이미지 구성요소 표 (D53). push-images.sh·image-report.sh·resolve-images.sh 가 `. "$HERE/lib/images.sh"` 로 읽는다.
# 세 스크립트는 구성요소와 이미지 이름을 이 표에서만 읽는다. scripts/build-stamp.sh 의 경로 표는 다른 데이터라 따로 있다.

# 인자 없이 부르면 이 일곱 전부다
# shellcheck disable=SC2034  # 읽는 스크립트가 쓴다
COMPONENTS="platform ui runtime agent mock-oidc web-tools sandbox"

# spec 구성요소  이미지 이름, 빌드 컨텍스트, Dockerfile, 판 번호를 한 줄로 찍는다. 모르는 구성요소면 1
spec() {
  case $1 in
    platform) echo "daegam-platform . platform/Dockerfile ${PLATFORM_VERSION:-0.1.0}" ;;
    ui) echo "daegam-ui ui ui/Dockerfile ${UI_VERSION:-0.1.0}" ;;
    runtime) echo "daegam-mcp-runtime mcp-runtime mcp-runtime/Dockerfile ${RUNTIME_VERSION:-0.1.0}" ;;
    agent) echo "daegam-agent-runtime agent-runtime agent-runtime/Dockerfile ${AGENT_VERSION:-0.1.0}" ;;
    mock-oidc) echo "daegam-mock-oidc deploy/dev/mock-oidc deploy/dev/mock-oidc/Dockerfile ${MOCK_OIDC_VERSION:-0.1.0}" ;;
    web-tools) echo "daegam-web-tools web-tools web-tools/Dockerfile ${WEB_TOOLS_VERSION:-0.1.0}" ;;
    sandbox) echo "daegam-sandbox sandbox sandbox/Dockerfile ${SANDBOX_VERSION:-0.1.0}" ;;
    *) return 1 ;;
  esac
}

# dockerfile_of 이미지이름  그 이미지를 굽는 Dockerfile 경로를 찍는다. 모르는 이름이면 1 이다
dockerfile_of() {
  local c name _ctx dockerfile _version
  for c in $COMPONENTS; do
    read -r name _ctx dockerfile _version <<<"$(spec "$c")"
    if [ "$name" = "$1" ]; then
      printf '%s\n' "$dockerfile"
      return 0
    fi
  done
  return 1
}

# image_ref 구성요소  올린 이미지를 `이름:도장` 으로 찍는다(도장은 scripts/build-stamp.sh). 모르는 구성요소이거나 도장이
# -dirty 면 까닭을 표준 오류에 찍고 1 이다. -dirty 도장은 어느 코드인지 말하지 못해 push-images 가 올리지 않는다
image_ref() {
  local me line stamp
  me="$(basename "$0" .sh)"
  line="$(spec "$1")" || { echo "$me: 모르는 구성요소 '$1' ($COMPONENTS)" >&2; return 1; }
  stamp="$(sh "$(dirname "${BASH_SOURCE[0]}")/../build-stamp.sh" "$1")" || return 1
  case $stamp in
    *-dirty)
      echo "$me: $1 에 커밋 안 된 변경이 있다(도장 $stamp). 커밋하고 make push-images 로 올린 뒤 다시 한다" >&2
      return 1
      ;;
  esac
  printf '%s:%s\n' "${line%% *}" "$stamp"
}
