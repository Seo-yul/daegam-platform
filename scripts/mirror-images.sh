#!/usr/bin/env bash
# LLM 게이트웨이 한 벌의 남의 이미지를 설치 값의 레지스트리(REGISTRY)에 digest 그대로 옮긴다 (P38 3절, make mirror-images).
#
#   make mirror-images                        넷 다(gateway ratelimit redis collector)
#   make mirror-images MIRROR_IMAGES="gateway"  고른 것만(대소문자를 가리지 않는다)
#   DRY_RUN=1 make mirror-images              레지스트리에 닿지 않고 옮길 것만 찍는다
#
# 참조와 서명자는 대감 차트의 값(charts/daegam/values.yaml 의 llmGateway.images)이고 읽기는 lib/llm_gateway_images.py 가
# 플랫폼의 가상 환경(uv)에서 한다. 이미지마다 원본의 index digest 를
# `REGISTRY/third-party/<이름>:<태그>` 로 docker buildx imagetools create 가 옮기고(여러 아키텍처 index 와 digest 가 그대로다),
# 사본이 같은 digest 인지 다시 읽어 본다. 서명자가 있는 이미지(게이트웨이, 수집기)는 옮기기 전에 원본의 그 digest 를
# cosign verify 로 확인한다. 키 없는 서명이라 신원은 값의 signer, 발급자는 GitHub Actions 다. 사본은 digest 가 같아
# 같은 서명이 묶이므로 다시 확인하지 않는다(원본 저장소의 서명을 COSIGN_REPOSITORY 로 읽으면 사본에서도 확인된다). 서명이
# 없는 둘(ratelimit 의 커밋 태그, Redis 공식 이미지)은 digest 만이 못 박는다.
# 원본은 ghcr.io 에서 받는다(F0 3번). TLS 를 검사하는 프록시 뒤에서는 원본 레지스트리마다 결과가 달라 한 곳으로 고정한다.
# 게이트웨이의 태그는 AGENTGATEWAY_VERSION 과 같아야 한다. 판을 올릴 때는 차트 값과 Makefile 을 함께 바꾸고 F0 의
# 필수 항목을 다시 잰다. 자격 증명은 docker login 이 둔 키체인이다.
# Env: REGISTRY(설치 값 파일의 REGISTRY), AGENTGATEWAY_VERSION(Makefile), DRY_RUN
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VALUES="$ROOT/charts/daegam/values.yaml"
ISSUER="https://token.actions.githubusercontent.com"
. "$HERE/lib/hosts.sh"
. "$HERE/lib/die.sh"
REGISTRY="${REGISTRY:-$(install_env_value REGISTRY)}"
[ -n "$REGISTRY" ] || die "REGISTRY 가 비었다 (환경이나 설치 값 파일의 REGISTRY)"
VERSION="${AGENTGATEWAY_VERSION:?AGENTGATEWAY_VERSION 이 비었다 (Makefile 이 넘긴다)}"
[ "$#" -gt 0 ] || set -- gateway ratelimit redis collector
command -v uv >/dev/null || die "uv 가 없다. 차트 값의 llmGateway.images 를 플랫폼의 가상 환경에서 읽는다 (brew install uv)"

PLAN="$(uv run --quiet --project "$ROOT/platform" python "$HERE/lib/llm_gateway_images.py" plan "$VALUES" "$REGISTRY")" \
  || die "$VALUES 의 llmGateway.images 를 읽지 못했다"

# signed SIGNER REF  그 digest 에 신원 SIGNER 의 키 없는 서명이 있는지 본다
signed() {
  cosign verify --certificate-identity="$1" --certificate-oidc-issuer="$ISSUER" "$2" >/dev/null
}

# mirror NAME  이미지 하나를 옮긴다
mirror() {
  local key="$1" line _k source target signer repo digest got
  line="$(awk -v k="$key" '$1 == k' <<<"$PLAN")"
  [ -n "$line" ] || die "모르는 이미지 '$key' (gateway ratelimit redis collector)"
  read -r _k source target signer <<<"$line"
  repo="${source%:*@*}" digest="${source##*@}"
  if [ "$key" = gateway ] && [ "${target##*:}" != "$VERSION" ]; then
    die "차트 값의 게이트웨이 태그 ${target##*:} 가 AGENTGATEWAY_VERSION $VERSION 과 다르다. 둘을 함께 바꾼다"
  fi
  echo ">> $source -> $target"
  if [ "$signer" = - ]; then echo "   서명 없음: digest $digest 로만 못 박는다"; fi
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  if [ "$signer" != - ]; then
    signed "$signer" "$repo@$digest" || die "$repo@$digest 의 서명을 신원 $signer 로 확인하지 못했다. 옮기지 않았다"
    echo "   원본 서명 ok ($signer)"
  fi
  docker buildx imagetools create --tag "$target" "$repo@$digest" >/dev/null
  got="$(docker buildx imagetools inspect "$target" --format '{{.Manifest.Digest}}')"
  [ "$got" = "$digest" ] || die "$target 이 $got 이다(기대 $digest)"
  echo "   사본 digest ok"
}

[ "${DRY_RUN:-0}" = 1 ] || command -v cosign >/dev/null || die "cosign 이 없다 (brew install cosign)"
for name in "$@"; do
  mirror "$(tr '[:upper:]' '[:lower:]' <<<"$name")"
done
