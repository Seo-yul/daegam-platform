#!/usr/bin/env bash
# 제품 이미지가 싣는 의존(파이썬 uv.lock 여섯, ui 의 package-lock.json)을 고정한 Trivy 로 본다 (D55). 릴리스 전 점검이다.
#
#   make deps-audit
#
# 고친 판이 나온 HIGH·CRITICAL 과 수용 목록에 없는 CRITICAL 이 있으면 실패한다. 이미지 보고(make image-report)는 태그를
# 민 뒤에 도므로, 의존을 올리면 없어지는 발견은 태그 전에 여기서 막아 후보 번호를 버리지 않는다. 수용 목록은 이미지
# 보고와 같은 security/vex/ 의 목록이고 lock 마다 그 범위의 것으로만 판정한다(sandbox/uv.lock 은 sandbox.openvex.json,
# 나머지는 base-images.openvex.json, lib/image_report.py 의 VEX_FILES). Trivy 는 한 번에 모든 lock 을 보므로 두 목록을
# 다 --vex 로 주고, 숨긴 것은 --show-suppressed 로 돌아와 판정이 lock 의 목록으로 다시 한다(pkg:pypi, pkg:npm purl).
# 표(lib/images.sh)의 Dockerfile 옆에 uv.lock 이나 package-lock.json 이 있는 구성요소를 본다. lock 과 그 짝(pyproject.toml,
# package.json)만 임시 디렉터리에 옮겨 체크아웃의 .venv·node_modules 를 읽지 않게 한다. Trivy 는 lock 의 개발 의존(uv 의
# dev 묶음, npm 의 devDependencies)을 세지 않는다. 판정은 lib/trivy_judge.py 의 deps 가 이미지 보고의 Judge 로 한다.
# Trivy 가 lock 하나라도 읽지 못하면 실패다. Trivy 는 lib/trivy.sh 가 부른다.
# Env: TRIVY_CACHE_DIR(~/.cache/trivy) DAEGAM_CA_FILE(사내 방화벽 CA 묶음)
# 필요: 고정한 판의 trivy 또는 docker.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VEX_DIR="security/vex"
# COMPONENTS 와 spec(구성요소 -> 이미지 이름, 컨텍스트, Dockerfile, 판)은 여기에 있다
. "$HERE/lib/images.sh"
# trivy_run 은 여기에 있다
. "$HERE/lib/trivy.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
locks=()
for c in $COMPONENTS; do
  read -r _name _ctx dockerfile _version <<<"$(spec "$c")"
  dir="$(dirname "$dockerfile")"
  for pair in "uv.lock pyproject.toml" "package-lock.json package.json"; do
    read -r lock manifest <<<"$pair"
    [ -f "$ROOT/$dir/$lock" ] || continue
    mkdir -p "$work/src/$dir"
    cp "$ROOT/$dir/$lock" "$ROOT/$dir/$manifest" "$work/src/$dir/"
    locks+=("$dir/$lock")
  done
done
vex_args=()
for f in "$ROOT/$VEX_DIR"/*.openvex.json; do
  cp "$f" "$work/"
  vex_args+=(--vex "$(basename "$f")")
done

echo ">> Trivy $TRIVY_VERSION 로 lock ${#locks[@]} 개를 본다: ${locks[*]}"
trivy_run "$work" fs --scanners vuln --severity "HIGH,CRITICAL" --format json --quiet \
  "${vex_args[@]}" --show-suppressed src >"$work/out.json"
cd "$ROOT"
PYTHONPATH="$HERE/lib" python3 "$HERE/lib/trivy_judge.py" deps "$work/out.json" "${locks[@]}"
