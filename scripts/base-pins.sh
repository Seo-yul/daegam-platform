#!/usr/bin/env bash
# 제품 이미지 일곱의 바탕 고정을 옮긴다 (D55). Dockerfile 의 FROM 은 `이름:태그@sha256:<digest>` 꼴이고, 태그마다
# 레지스트리의 지금 index digest 를 물어 digest 를 그것으로 바꾼다. 판정과 쓰기는 lib/base_pins.py 가 한다.
#
#   make base-pins
#   make base-pins-check     쓰지 않고 지금 digest 와 다른 고정만 알린다. 있으면 1 이다(릴리스 전 점검)
#
# 굽기는 고정한 digest 만 받으므로 바탕이 다시 구워져도 이 스크립트를 돌려 커밋하기 전에는 distroless 이미지 넷의 OS
# 패키지가 바뀌지 않는다. ui·mock-oidc·sandbox 는 굽기가 OS 패키지를 올리므로 고정이 그대로여도 날마다 바뀔 수 있다.
# 릴리스 전 미리 보기(make image-report REPORT_TAG=...)나 make base-scan 이 바탕 패키지의 고칠 수 있는 발견을 보일 때,
# 그리고 정식 릴리스로 올릴 후보 전에 돌린다. 옮긴 바로 뒤 make base-scan 으로 새 바탕을 본다. 바뀐 Dockerfile 은 커밋해 시험과 후보가 같은 바탕으로 굽게
# 한다. 하나라도 묻지 못하면 아무 파일도 쓰지 않는다.
# 필요: docker buildx(imagetools inspect). docker login 이 둔 자격 증명이 있으면 그것으로, 없으면 익명으로 묻는다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# COMPONENTS 와 spec(구성요소 -> 이미지 이름, 컨텍스트, Dockerfile, 판)은 여기에 있다
. "$HERE/lib/images.sh"

files=()
for c in $COMPONENTS; do
  read -r _name _ctx dockerfile _version <<<"$(spec "$c")"
  files+=("$dockerfile")
done
cd "$ROOT"
python3 "$HERE/lib/base_pins.py" "$@" "${files[@]}"
