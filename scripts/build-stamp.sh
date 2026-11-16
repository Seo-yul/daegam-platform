#!/bin/sh
# 구성요소 빌드 도장 (D52, P82). 그 구성요소 경로들의 커밋된 트리(HEAD:<경로>)에서 나온 8자 해시이고, 그 경로에
# 커밋 안 된 변경이 있으면 -dirty 가 붙는다. 변경은 git status 로 보아 추적하지 않는 새 파일도 센다. 경로마다의 내용에서
# 나오므로 UI 만 바꾼 커밋이 플랫폼 도장을 바꾸지 않고, 같은 코드는 이력이 다른 저장소(원본 저장소와 그 내보내기)에서도
# 같은 도장이다. 그래서 UI 만 바꾼 뒤 플랫폼 이미지를 다시 구워도 캐시로 같은 이미지가 나오고, 플랫폼만 바꾼 뒤 열린 탭이
# 괜히 "오래된 탭" 이 되지 않으며, 공개 저장소가 구운 이미지를 다른 checkout 의 resolve-images.sh 가 같은 도장으로 찾는다.
#   scripts/build-stamp.sh platform    platform/, agent-runtime/schema/, web-tools/schema/ (플랫폼 이미지가 담는 것)
#   scripts/build-stamp.sh ui          ui/
#   scripts/build-stamp.sh runtime     mcp-runtime/
#   scripts/build-stamp.sh agent       agent-runtime/
#   scripts/build-stamp.sh mock-oidc   deploy/dev/mock-oidc/
#   scripts/build-stamp.sh web-tools   web-tools/
#   scripts/build-stamp.sh sandbox     sandbox/
# Makefile(image-platform·image-ui), scripts/push-images.sh, scripts/e2e_ui_serving.sh, scripts/e2e_install.sh 가 이것을 부른다.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
case "${1-}" in
  platform) set -- platform agent-runtime/schema web-tools/schema ;;
  ui) set -- ui ;;
  runtime) set -- mcp-runtime ;;
  agent) set -- agent-runtime ;;
  mock-oidc) set -- deploy/dev/mock-oidc ;;
  web-tools) set -- web-tools ;;
  sandbox) set -- sandbox ;;
  *)
    echo "usage: $0 platform|ui|runtime|agent|mock-oidc|web-tools|sandbox" >&2
    exit 2
    ;;
esac
# 경로가 하나면 그 트리 id 의 앞 8자이고, 여럿이면 트리 id 목록의 해시 앞 8자다. git 의 짧은 해시는 저장소의 객체 수에
# 따라 길어질 수 있어 쓰지 않고 앞 8자를 잘라 어느 클론에서도 같게 한다. ui/vite.config.ts 의 commitSha 가 ui 를 같은
# 규칙으로 구한다
trees=""
for p in "$@"; do
  tree=$(git -C "$root" rev-parse --verify -q "HEAD:$p") || {
    echo "build-stamp: HEAD has no $p" >&2
    exit 1
  }
  trees="$trees$tree
"
done
if [ $# -eq 1 ]; then
  sha=$(printf '%s' "$trees" | cut -c1-8)
else
  sha=$(printf '%s' "$trees" | git -C "$root" hash-object --stdin | cut -c1-8)
fi
[ -n "$sha" ] || {
  echo "build-stamp: could not hash the trees of $*" >&2
  exit 1
}
if [ -n "$(git -C "$root" status --porcelain -- "$@")" ]; then
  sha="$sha-dirty"
fi
printf '%s\n' "$sha"
