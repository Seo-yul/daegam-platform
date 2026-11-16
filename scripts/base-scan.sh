#!/usr/bin/env bash
# 제품 이미지 바탕을 고정한 digest 그대로 고정한 Trivy 로 본다 (D55). 릴리스 전 점검이고 이미지를 굽지 않는다.
#
#   make base-scan
#
# 제품 Dockerfile 일곱의 마지막 단계 바탕(distroless cc-debian13, nginx-unprivileged, python:3.12-slim-trixie,
# debian:trixie-slim)을 `trivy image --vex` 로 보고 이미지 보고의 규칙(수용 목록에 없는 CRITICAL 이면 실패, HIGH 는 센다)으로
# 판정한다. 수용 목록은 바탕을 쓰는 Dockerfile 의 범위마다 하나다(debian:trixie-slim 은 코드 실행 작업 공간 하나만 써서
# security/vex/sandbox.openvex.json, 나머지는 base-images.openvex.json). ui·mock-oidc·sandbox 는 굽기의 마지막 단계가 OS
# 패키지를 올리므로 고친 판이 있는 패키지는 빼고 센다. nginx.org 에서 온 nginx 와
# nginx-module-* 는 apk upgrade 가 닿지 않아 그대로 센다. CPython 이미지 다섯의 묶인
# 라이브러리는 지금 Dockerfile 의 핀으로 python.org 와 NVD 에 묻는다(이미지 보고의 bundled). 그래서 make base-pins 로
# 고정을 옮긴 뒤, 그리고 후보마다 태그 전에 돌린다. 판정은 lib/trivy_judge.py 의 base 가 한다. Trivy 는 lib/trivy.sh 가
# 부르고 바탕은 linux/arm64 를 본다(이미지가 도는 곳의 아키텍처, images.md 3절).
# Env: TRIVY_CACHE_DIR(~/.cache/trivy) DAEGAM_CA_FILE(사내 방화벽 CA 묶음. Trivy 와 python.org·NVD 조회가 믿는다)
# 필요: 고정한 판의 trivy 또는 docker.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VEX_DIR="security/vex"
BUILDS="security/cpython-builds.json"
. "$HERE/lib/images.sh"
. "$HERE/lib/trivy.sh"

# 줄에 저장소 기준 경로가 찍히도록 뿌리에서 돈다
cd "$ROOT"
dockerfiles=()
for c in $COMPONENTS; do
  read -r _name _ctx dockerfile _version <<<"$(spec "$c")"
  dockerfiles+=("$dockerfile")
done
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$ROOT/$VEX_DIR"/*.openvex.json "$work/"

# 바탕마다 그것을 쓰는 Dockerfile 범위의 수용 목록을 Trivy 에 준다. 판정은 trivy_judge.py 의 base 가 범위마다 다시 한다
scans=()
i=0
while read -r ref upgrades vexes; do
  i=$((i + 1))
  vex_args=()
  IFS=, read -r -a listed <<<"$vexes"
  for v in "${listed[@]}"; do vex_args+=(--vex "$(basename "$v")"); done
  echo ">> Trivy $TRIVY_VERSION 로 바탕을 본다: $ref (${listed[*]})"
  trivy_run "$work" image --platform linux/arm64 --scanners vuln --severity "HIGH,CRITICAL" --format json --quiet \
    "${vex_args[@]}" --show-suppressed "$ref" >"$work/base-$i.json"
  scans+=("$ref=$work/base-$i.json=$upgrades")
done < <(PYTHONPATH="$HERE/lib" python3 "$HERE/lib/trivy_judge.py" bases "${dockerfiles[@]}")
PYTHONPATH="$HERE/lib" python3 "$HERE/lib/trivy_judge.py" base "$BUILDS" "${scans[@]}" -- "${dockerfiles[@]}"
