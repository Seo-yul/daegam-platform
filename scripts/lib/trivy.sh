# shellcheck shell=bash
# 고정한 Trivy 를 부른다 (D55). deps-audit.sh 와 base-scan.sh 가 `. "$HERE/lib/trivy.sh"` 로 읽는다.
#
# 설치된 trivy 가 고정한 판(TRIVY_VERSION)이면 그것을, 아니면 고정한 컨테이너 이미지(TRIVY_IMAGE)를 쓴다. 판이 다른
# trivy 는 다른 판정 규칙과 DB 모양을 가질 수 있어 쓰지 않는다. 취약점 DB 는 TRIVY_CACHE_DIR(~/.cache/trivy)에 둔다.
# DAEGAM_CA_FILE(사내 방화벽 CA 묶음)이 있으면 Trivy 가 DB 와 이미지를 받을 때 믿는다.
# 판을 올리면 https://github.com/aquasecurity/trivy/releases 의 판과 그 이미지의 index digest 를 함께 바꾼다
TRIVY_VERSION="0.76.2"
TRIVY_IMAGE="ghcr.io/aquasecurity/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"

# trivy_run 작업디렉터리 trivy인자...  작업 디렉터리에서 Trivy 를 돌린다. 인자의 경로는 그 디렉터리 기준 상대 경로로
# 준다. 컨테이너는 그 디렉터리를 /work 에 읽기 전용으로 붙이고 거기서 돈다
trivy_run() {
  local dir="$1" cache="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy}" run
  shift
  mkdir -p "$cache"
  if command -v trivy >/dev/null 2>&1 \
    && [ "$(trivy --version 2>/dev/null | sed -n 's/^Version: //p' | head -1)" = "$TRIVY_VERSION" ]; then
    if [ -n "${DAEGAM_CA_FILE:-}" ]; then
      (cd "$dir" && SSL_CERT_FILE="$DAEGAM_CA_FILE" trivy "$@" --cache-dir "$cache")
    else
      (cd "$dir" && trivy "$@" --cache-dir "$cache")
    fi
    return
  fi
  run=(docker run --rm -w /work -v "$dir:/work:ro" -v "$cache:/cache")
  [ -z "${DAEGAM_CA_FILE:-}" ] || run+=(-v "$DAEGAM_CA_FILE:/daegam-ca.pem:ro" -e SSL_CERT_FILE=/daegam-ca.pem)
  "${run[@]}" "$TRIVY_IMAGE" "$@" --cache-dir /cache
}
