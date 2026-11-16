#!/usr/bin/env bash
# 정식 릴리스 태그를 후보의 이미지에 붙인다 (D66, 릴리스 B 3단계). 새로 굽지 않는다. 후보 태그가 가리키는 digest 마다
# 공개 키로 확인되는 서명과 그 이미지 이름·도장이 있는지 본 뒤, 같은 digest 에 정식 태그를 붙이고 다시 읽어 같은지 본다.
#   scripts/promote-images.sh <후보 태그> <정식 태그>
# 이 스크립트가 없던 때의 후보도 올릴 수 있도록, 스크립트는 지금 체크아웃에서 돌고 도장(scripts/build-stamp.sh)만 후보
# 커밋의 임시 worktree 에서 구한다. 공개 키는 지금 체크아웃에 커밋된 deploy/cosign.pub 이다.
# 서명 키는 쓰지 않는다. 서명은 digest 에 붙어 있으므로 태그를 더해도 그대로 남는다.
# 환경: REGISTRY(설치 값 파일의 REGISTRY), COMPONENTS(scripts/lib/images.sh 의 일곱), COSIGN_PUB(deploy/cosign.pub)
# 0 은 일곱이 모두 붙었다는 뜻이고, 하나라도 확인이나 붙이기가 실패하면 그 자리에서 1 이다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 레지스트리(호스트/프로젝트)는 환경의 REGISTRY 이고, 없으면 설치 값 파일(scripts/lib/hosts.sh 의 install_env_file)의 값이다
. "$HERE/lib/hosts.sh"
REGISTRY="${REGISTRY:-$(install_env_value REGISTRY)}"
[ -n "$REGISTRY" ] || { echo "REGISTRY 가 비었다 (환경이나 설치 값 파일의 REGISTRY)" >&2; exit 1; }
# COMPONENTS·spec 는 여기에 있다
. "$HERE/lib/images.sh"
# verify_ready·verify_image 는 여기에 있다 (resolve-images.sh 가 하는 확인과 같다)
. "$HERE/lib/signing.sh"
# die 는 여기에 있다
. "$HERE/lib/die.sh"

RC="${1:-}"
RELEASE="${2:-}"
[[ $RC =~ ^[0-9]+\.[0-9]+\.[0-9]+-rc[0-9]+$ ]] || die "후보 태그는 X.Y.Z-rcN 꼴이어야 한다: '$RC'"
[[ $RELEASE =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "정식 태그는 X.Y.Z 꼴이어야 한다: '$RELEASE'"
[ "${RC%-rc*}" = "$RELEASE" ] || die "$RC 는 $RELEASE 의 후보가 아니다"
git -C "$ROOT" rev-parse -q --verify "refs/tags/$RC" >/dev/null || die "태그 $RC 가 이 저장소에 없다"

verify_ready || exit 1

# 도장은 후보 커밋의 트리에서 구한다. 그 트리의 build-stamp.sh 를 쓴다
RC_TREE="$(mktemp -d)"
trap 'git -C "$ROOT" worktree remove --force "$RC_TREE" >/dev/null 2>&1 || rm -rf "$RC_TREE"' EXIT
git -C "$ROOT" worktree add --quiet --detach "$RC_TREE" "refs/tags/$RC"

digest_of() { docker buildx imagetools inspect "$1" --format '{{json .Manifest.Digest}}' | tr -d '"'; }

for c in $COMPONENTS; do
  read -r name _ <<<"$(spec "$c")"
  stamp="$(sh "$RC_TREE/scripts/build-stamp.sh" "$c")"
  digest="$(digest_of "$REGISTRY/$name:$RC")" || die "$name:$RC 이 레지스트리에 없다"
  [ -n "$digest" ] || die "$name:$RC 의 digest 를 읽지 못했다"
  # 태그가 아니라 digest 로 확인하고 붙인다. 확인과 붙이기 사이에 후보 태그가 움직여도 확인한 것만 붙는다
  verify_image "$REGISTRY/$name@$digest" "$name" "$stamp" || die "$name@$digest 의 서명이 $name·$stamp 로 확인되지 않는다"
  docker buildx imagetools create -t "$REGISTRY/$name:$RELEASE" "$REGISTRY/$name@$digest"
  got="$(digest_of "$REGISTRY/$name:$RELEASE")"
  [ "$got" = "$digest" ] || die "$name:$RELEASE 가 $got 을 가리킨다 ($digest 이어야 한다)"
  echo ">> $name:$RELEASE = $digest (stamp $stamp)"
done
echo "promote-images: PASS ($RC -> $RELEASE)"
