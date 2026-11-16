#!/usr/bin/env bash
# 대감 차트의 제품 이미지 여섯을 레지스트리의 digest 로 고정하고 서명을 확인한 값 파일을 쓴다 (P85 6절, D53, D66).
#
#   scripts/resolve-images.sh --arch <arm64|amd64|…> --out <값 파일> [--registry <호스트/프로젝트>] [--key <공개 키>]
#   DRY_RUN=1 scripts/resolve-images.sh …   레지스트리와 cosign 에 닿지 않고 digest 없는 도장 태그만 쓴다
#
# 이미지는 이 checkout 의 도장 태그(lib/images.sh 의 image_ref, 도장은 scripts/build-stamp.sh)다. -dirty 도장은 어느 코드인지
# 말하지 못하므로 거절한다. 태그마다 레지스트리의 index 를 받아 노드 아키텍처(--arch, 기본값 없음)의 판이 있는지와 응답의
# digest 가 본문의 sha256 인지 보고(lib/image_pin.py) `이름:도장@sha256:…` 으로 고정한다. 그 digest 마다 공개 키(--key, 기본
# deploy/cosign.pub)로 확인되는 서명이 있고 서명에 실린 이미지 이름과 도장이 그 태그와 같아야 한다(lib/signing.sh 의
# verify_image). 하나라도 안 되면 그 이미지를 찍고 값 파일을 쓰지 않은 채 멈춘다.
# 값 파일(JSON)은 images.registry 와 구성요소마다 images.<구성요소>.tag·digest 다. helm upgrade 의 -f 로 준다. digest 가
# 있는 이미지는 차트가 IfNotPresent 로 당기므로 노드가 캐시한 옛 이미지를 쓰지 않는다. 코드가 그대로인 다시 굽기가 같은 도장
# 태그를 새 digest 로 덮어도 파드 템플릿이 바뀌어 롤아웃되고, 태그가 지워져도 노드는 digest 로 당긴다.
# 레지스트리는 --registry, 환경의 REGISTRY, 설치 값 파일의 REGISTRY 차례다. 자격 증명은 docker login 이 둔 것이고 읽기는
# lib/registry_auth.py 가 한다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/die.sh"

usage() {
  echo "usage: $0 --arch <arm64|amd64|…> --out <values file> [--registry <host/project>] [--key <public key>]" >&2
  exit 2
}

REGISTRY_ARG="" ARCH="" OUT=""
while [ "$#" -gt 0 ]; do
  case $1 in
    --registry | --arch | --out | --key) [ "$#" -ge 2 ] || usage ;;
    *) usage ;;
  esac
  case $1 in
    --registry) REGISTRY_ARG="$2" ;;
    --arch) ARCH="$2" ;;
    --out) OUT="$2" ;;
    # lib/signing.sh 의 signing_pub 이 읽는다
    --key) export COSIGN_PUB="$2" ;;
  esac
  shift 2
done
[ -n "$OUT" ] || usage
# 노드가 당길 판이다. 기본값을 두지 않는다. 잘못 고르면 노드가 당기지 못하는 digest 를 렌더하기 때문이다
[[ $ARCH =~ ^[a-z0-9]+$ ]] || die "--arch 는 노드의 아키텍처다(arm64, amd64 같은 소문자 한 낱말). 받은 값 '$ARCH'"
if [ -n "$REGISTRY_ARG" ]; then
  REGISTRY="$REGISTRY_ARG"
elif [ -z "${REGISTRY:-}" ]; then
  . "$HERE/lib/hosts.sh"
  REGISTRY="$(install_env_value REGISTRY)" || die "레지스트리가 없다 (--registry, REGISTRY 나 설치 값 파일의 REGISTRY)"
fi
[ -n "$REGISTRY" ] || die "레지스트리가 비었다 (--registry, REGISTRY 나 설치 값 파일의 REGISTRY)"

# 차트의 이미지 값 이름(values.yaml 의 images.<이름>)과 lib/images.sh 의 구성요소. mock-oidc 는 oidc e2e 의 시험용 IdP 라 없다
VALUE_KEYS="platform ui runtime agent webTools sandbox"
COMPONENTS_OF="platform ui runtime agent web-tools sandbox"
. "$HERE/lib/images.sh"
REFS=()
for c in $COMPONENTS_OF; do
  ref="$(image_ref "$c")" || exit 1
  REFS+=("$ref")
done

if [ "${DRY_RUN:-0}" = 1 ]; then
  PINNED="${REFS[*]}"
else
  # verify_ready(cosign 과 공개 키 확인), verify_image(digest 의 서명 검증), signing_pub(공개 키 경로)는 여기에 있다
  . "$HERE/lib/signing.sh"
  # 서명을 검증하지 못할 것이면 레지스트리에 묻기 전에 멈춘다
  verify_ready || exit 1
  PINNED="$(PYTHONPATH="$HERE/lib" python3 - "$REGISTRY" "$ARCH" "${REFS[@]}" <<'PY'
import sys, urllib.error

from image_pin import fetch_index, pin
from registry_auth import Registry

registry, arch, refs = sys.argv[1], sys.argv[2], sys.argv[3:]
reg = Registry(registry)
pinned, bad = [], []
for ref in refs:
    name, tag = ref.split(":")
    try:
        pinned.append(pin(ref, *fetch_index(reg, name, tag), arch))
    except urllib.error.HTTPError as e:
        bad.append(f"{ref} ({e.code})")
    except (urllib.error.URLError, ValueError) as e:
        bad.append(f"{ref} ({e})")
    else:
        print(f"   {registry}/{pinned[-1]} {arch} ok", file=sys.stderr)
if bad:
    sys.exit("resolve-images.sh: FAIL: " + ", ".join(bad))
print(" ".join(pinned))
PY
)" || die "레지스트리에서 이미지를 고정하지 못했다(위 줄). 로그인이 없으면 docker login, 이미지가 없으면 make push-images 를 먼저 한다"
  # 고정한 digest 마다 우리 키의 서명을 확인한다(D66). 서명에 실린 이미지 이름과 도장도 이 태그와 같아야 한다. 레지스트리에서
  # 누가 이미지를 바꿔치기했거나 우리가 서명한 다른 이미지를 이 태그 뒤에 옮겨 놓았으면 확인되지 않는다
  for ref in $PINNED; do
    tagged="${ref%@*}"
    verify_image "$REGISTRY/$ref" "${tagged%%:*}" "${tagged#*:}" \
      || die "$REGISTRY/$ref 의 서명을 공개 키($(signing_pub))로 확인하지 못했다(위 줄). 값 파일을 쓰지 않았다. 직접 올린 이미지라면 make push-images 로 다시 올려 서명하고, 그렇지 않다면 누군가 레지스트리에서 바꾼 이미지다"
    echo "   $REGISTRY/$ref 서명 ok" >&2
  done
fi

# 값 파일은 다 확인한 뒤 한 번에 쓴다. 멈춘 실행이 반쯤 쓴 파일을 남기지 않는다
# shellcheck disable=SC2086 # 이름 목록을 낱말로 나눠 넘긴다
python3 - "$OUT" "$REGISTRY" "$VALUE_KEYS" $PINNED <<'PY'
import json, os, sys

out, registry, keys, refs = sys.argv[1], sys.argv[2], sys.argv[3].split(), sys.argv[4:]
images = {"registry": registry}
for key, ref in zip(keys, refs):
    tagged, _, digest = ref.partition("@")
    images[key] = {"tag": tagged.partition(":")[2], **({"digest": digest} if digest else {})}
tmp = f"{out}.tmp"
with open(tmp, "w") as f:
    json.dump({"images": images}, f, indent=1, sort_keys=True)
    f.write("\n")
os.replace(tmp, out)
PY
echo ">> $OUT: $REGISTRY, $ARCH$([ "${DRY_RUN:-0}" != 1 ] || echo ", DRY_RUN 이라 digest 없는 도장 태그")" >&2
