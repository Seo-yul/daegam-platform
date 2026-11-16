#!/usr/bin/env bash
# 제품 이미지를 arm64 로 구워 레지스트리에 올리고 그 digest 에 서명한다 (D53, D66). 로컬 도커의 태그는 건드리지
# 않는다. 다 올린 뒤 서명과 레지스트리가 잰 취약점을 scripts/image-report.sh 로 보인다(D55). 수용 목록에 없는 CRITICAL 이
# 있거나 스캔 결과가 없는 이미지가 있으면 1 로 끝난다.
#
#   make push-images                            일곱 전부 (platform ui runtime agent mock-oidc web-tools sandbox)
#   make push-images PUSH_IMAGES="ui runtime"   고른 것만
#   make push-images RELEASE=1.0.0-rc1          릴리스 굽기. 체크아웃한 커밋에 그 태그가 있어야 하고, 이미지마다 그 태그도 붙인다
# Env: REGISTRY(설치 값 파일의 REGISTRY) PUSH_PLATFORMS(linux/arm64) PUSH_BUILDER(daegam-push)
#      PUSH_TAG_SUFFIX  태그마다 붙일 꼬리(-arm64, -amd64). 아키텍처마다 다른 실행기에서 구워 한 인덱스로 묶을 때만
#      준다(release-images 의 주석으로 둔 amd64 잡). 주면 취약점 보고는 묶는 쪽이 하므로 여기서는 하지 않는다
#      PLATFORM_VERSION·UI_VERSION·RUNTIME_VERSION·AGENT_VERSION·MOCK_OIDC_VERSION·WEB_TOOLS_VERSION·SANDBOX_VERSION(0.1.0)
#      COSIGN_KEY(~/.config/daegam/cosign.key)  서명 키 파일
#      COSIGN_PUB(deploy/cosign.pub)  서명을 확인할 공개 키
#      COSIGN_PASSWORD  터미널 없이 돌릴 때만 주는 서명 키의 비밀번호. 터미널이 있으면 화면에 보이지 않게 묻는다
#      ALLOW_DIRTY=1  커밋 안 된 변경이 있어도 올린다. 그 도장은 어느 코드인지 말하지 못하므로 기본은 거절이다
#      SKIP_IMAGE_REPORT=1  올리고 서명한 뒤의 서명·취약점 보고(image-report.sh)를 경고 한 줄과 함께 건너뛴다. 그 보고는
#      레지스트리가 잰 결과를 zot 의 search 확장(/v2/_zot/ext/search)으로 읽으므로 다른 레지스트리에서만 쓴다
#      RELEASE  릴리스 태그(X.Y.Z, X.Y.Z-rcN). 체크아웃한 커밋에 그 git 태그가 없거나 ALLOW_DIRTY 와 함께면 거절한다
#      DAEGAM_CA_FILE  사내 방화벽 CA 파일. 있으면 굽기에 빌드 비밀 daegam_ca 로 넘겨 받기 단계만 그 CA 를 믿는다
#
# 서명(D66)은 태그가 아니라 굽기가 올린 digest 에 하고, 여러 플랫폼 인덱스면 플랫폼마다의 이미지에도 한다. 서명에는 이미지
# 이름과 도장을 싣는다. 키 파일이나 비밀번호가 없거나, 비밀번호가 틀리거나, 키가 공개 키와 한 쌍이 아니면 아무것도 올리기
# 전에 멈춘다. 서명이 실패하면 이미지가 이미 올라갔어도 1 로 끝난다. 키와 비밀번호를 다루는 일은 scripts/lib/signing.sh 가
# 한다.
#
# 태그는 둘이고, RELEASE 를 주면 셋이다. 판 번호(0.1.0)는 매니페스트와 Cluster 의 기본 이미지가 가리키는 태그다.
# 도장(scripts/build-stamp.sh, 그 구성요소의 커밋된 트리에서 나온다)은 어느 코드인지 말하는 태그다. 릴리스 태그(1.0.0,
# 1.0.0-rc1)는 어느 릴리스가 그 이미지를 냈는지 말한다(docs/plan/agents.md "Branches and releases"). 릴리스 태그는 RELEASE 로
# 청할 때만 붙인다. 태그가 붙은 커밋에서 보안 판 때문에 다시 구워도 배포하지 않은 digest 로 릴리스 태그가 옮겨 가지 않게
# 하려는 것이다. 굽기마다
# 베이스를 새로 받으므로(--pull) 코드가 그대로여도 다시 구우면 어느 태그든 새 digest 를 가리킬 수 있다. 그래서 태그는 사람이
# 읽는 이름이고 배포는 digest 로 고정한다(scripts/resolve-images.sh). 노드는 캐시한 태그를 다시 당기지 않으므로 태그만 쓰는 배포는
# pull_policy 를 Always 로 둔다.
#
# 자격 증명은 docker login 이 둔 자리(macOS 키체인)를 쓴다. 이 스크립트와 저장소에는 비밀번호가 없다.
#   docker login <레지스트리 호스트> -u <계정>
# 플랫폼 하나로 구워도 인덱스(항목 하나)로 올린다(BUILDKIT_MULTI_PLATFORM). resolve-images.sh 의 digest 고정
# (lib/image_pin.py)과 정식판 승격(promote-images.sh)이 인덱스를 읽고, amd64 를 켜면 두 인덱스를 한 인덱스로 묶기
# 때문이다. 인덱스와 metadata-file 을 내는 굽기는 도커 엔진의 기본 빌더로 하지 못해 docker-container 드라이버
# 빌더(PUSH_BUILDER)를 쓴다. 없으면 만든다. 그 빌더는 로컬 이미지 캐시를 보지 못해 처음 한 번은 베이스 이미지부터 받는다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 레지스트리(호스트/프로젝트)는 환경의 REGISTRY 이고, 없으면 설치 값 파일(scripts/lib/hosts.sh 의 install_env_file)의 값이다
. "$HERE/lib/hosts.sh"
REGISTRY="${REGISTRY:-$(install_env_value REGISTRY)}"
[ -n "$REGISTRY" ] || { echo "REGISTRY 가 비었다 (환경이나 설치 값 파일의 REGISTRY)" >&2; exit 1; }
# 기본은 arm64 다. 다른 아키텍처의 노드면 PUSH_PLATFORMS 로 준다. 그 아키텍처를 에뮬레이션으로 굽지는 않는다
PLATFORMS="${PUSH_PLATFORMS:-linux/arm64}"
SUFFIX="${PUSH_TAG_SUFFIX:-}"
BUILDER="${PUSH_BUILDER:-daegam-push}"
HOST="${REGISTRY%%/*}"
# COMPONENTS 와 spec(이미지 이름·빌드 컨텍스트·Dockerfile·판 번호 표)은 여기에 있다
. "$HERE/lib/images.sh"
# signing_ready(공개 키, 서명 키, 비밀번호 확인)와 sign_image(digest 서명)는 여기에 있다
. "$HERE/lib/signing.sh"

# shellcheck disable=SC2086  # 구성요소 이름을 낱말로 나눠 인자로 편다
[ "$#" -gt 0 ] || set -- $COMPONENTS
for c in "$@"; do
  spec "$c" >/dev/null || { echo "push-images: 모르는 구성요소 '$c' ($COMPONENTS)" >&2; exit 2; }
done
# 릴리스 태그를 잘못 청했으면 비밀번호를 묻기 전에 멈춘다
release="${RELEASE:-}"
if [ -n "$release" ]; then
  [[ $release =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]] \
    || { echo "push-images: RELEASE 값($release)이 X.Y.Z 나 X.Y.Z-rcN 꼴이 아니다" >&2; exit 2; }
  [ "$(git -C "$ROOT" rev-parse -q --verify "refs/tags/$release^{commit}")" = "$(git -C "$ROOT" rev-parse HEAD)" ] \
    || { echo "push-images: 체크아웃한 커밋에 릴리스 태그($release)가 없다. 릴리스 태그를 먼저 찍는다" >&2; exit 2; }
  [ "${ALLOW_DIRTY:-0}" != 1 ] \
    || { echo "push-images: 릴리스 굽기는 커밋 안 된 변경을 담지 않는다. ALLOW_DIRTY 를 빼고 다시 돌린다" >&2; exit 2; }
  echo ">> 릴리스 태그($release)를 올리는 이미지마다 붙인다"
fi
[[ $SUFFIX =~ ^(-[a-z0-9]+)?$ ]] || { echo "push-images: PUSH_TAG_SUFFIX 값($SUFFIX)이 -arm64 같은 꼴이 아니다" >&2; exit 2; }
if [ -n "${DAEGAM_CA_FILE:-}" ] && { [ ! -f "$DAEGAM_CA_FILE" ] || [ ! -s "$DAEGAM_CA_FILE" ] || [ ! -r "$DAEGAM_CA_FILE" ]; }; then
  echo "push-images: DAEGAM_CA_FILE($DAEGAM_CA_FILE)이 읽을 수 있는 비어 있지 않은 파일이 아니다. 경로를 고치거나 비운다" >&2
  exit 2
fi
# 서명하지 못할 것이면 아무것도 굽거나 올리기 전에 멈춘다. 비밀번호는 여기서 한 번 묻는다
signing_ready || exit 1

docker info >/dev/null 2>&1 || { echo "push-images: 도커 엔진이 없다 (orb start)" >&2; exit 1; }
# 로그인 여부만 본다. 자격 증명 값은 읽지 않는다
python3 - "$HOST" <<'PY' || { echo "push-images: $HOST 에 로그인한 적이 없다. docker login $HOST -u <계정> 을 먼저 한다" >&2; exit 1; }
import json, os, sys
cfg = json.load(open(os.path.expanduser("~/.docker/config.json")))
sys.exit(0 if sys.argv[1] in (cfg.get("auths") or {}) or sys.argv[1] in (cfg.get("credHelpers") or {}) else 1)
PY

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo ">> creating buildx builder $BUILDER (docker-container driver)"
  docker buildx create --name "$BUILDER" --driver docker-container --bootstrap >/dev/null
fi

# 구성요소마다의 굽기 결과(--metadata-file)를 둘 곳. 끝나면 지운다
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-push.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

pushed=()
for c in "$@"; do
  read -r name ctx dockerfile version <<<"$(spec "$c")"
  stamp="$(sh "$ROOT/scripts/build-stamp.sh" "$c")"
  tags=("$version" "$stamp")
  case $stamp in
    *-dirty)
      if [ "${ALLOW_DIRTY:-0}" != 1 ]; then
        echo "push-images: $c 에 커밋 안 된 변경이 있다(도장 $stamp). 커밋한 뒤 올리거나 ALLOW_DIRTY=1 로 연다" >&2
        exit 1
      fi
      ;;
  esac
  [ -z "$release" ] || tags+=("$release")
  [ -z "$SUFFIX" ] || tags=("${tags[@]/%/$SUFFIX}")
  # provenance 증명은 끈다. 켜 두면 인덱스에 unknown/unknown 항목이 붙어 오래된 도구가 헷갈린다. --pull 은 FROM 을 빌더
  # 캐시가 아닌 레지스트리에서 확인하게 한다. 바탕은 Dockerfile 의 FROM 이 digest 로 고정하므로(D55) 이 확인으로 내용이
  # 바뀌지는 않는다. 패키지 관리자가 없는 distroless 의 파이썬 이미지 넷(platform·runtime·agent·web-tools)의 OS 패키지는
  # make base-pins 가 고정을 옮길 때만 바뀐다. ui·mock-oidc·sandbox 는 마지막 단계가 OS 패키지를 올리므로 고정이 그대로여도
  # 날마다 바뀔 수 있다(아래 OS_PATCH_DATE)
  args=(--builder "$BUILDER" --platform "$PLATFORMS" -f "$ROOT/$dockerfile" --provenance=false --pull
    --build-arg BUILDKIT_MULTI_PLATFORM=1 --metadata-file "$WORK/$c.json" --push)
  for t in "${tags[@]}"; do args+=(-t "$REGISTRY/$name:$t"); done
  # 도장을 화면이 읽는 것은 플랫폼(/api/version)과 UI(/build.json) 둘뿐이다
  case $c in platform | ui) args+=(--build-arg "GIT_SHA=$stamp" --build-arg "RELEASE=$release") ;; esac
  # OS_PATCH_DATE 는 OS 보안 판을 받는 층(ui 의 apk upgrade, mock-oidc 와 sandbox 의 apt-get upgrade)의 캐시를 하루에 한 번
  # 버린다. 베이스가 그대로여도 그 층은 캐시되기 때문이다. 같은 날 나온 보안 판을 바로 받으려면 OS_PATCH_DATE 에 다른 값을
  # 넘긴다. 그 층이 있는 이미지는 이 셋뿐이다
  case $c in ui | mock-oidc | sandbox) args+=(--build-arg "OS_PATCH_DATE=${OS_PATCH_DATE:-$(date -u +%F)}") ;; esac
  # 사내 방화벽 CA (선택). 받기 단계만 빌드 비밀로 믿고 이미지 층에는 남지 않는다(docs/guide/images.md 3절)
  [ -z "${DAEGAM_CA_FILE:-}" ] || args+=(--secret "id=daegam_ca,src=$DAEGAM_CA_FILE")
  echo ">> $c: $REGISTRY/$name:{$(IFS=,; echo "${tags[*]}")} ($PLATFORMS)"
  docker buildx build "${args[@]}" "$ROOT/$ctx" \
    || { echo ">> $c 굽기·올리기 실패. 5초 뒤 한 번 더"; sleep 5; docker buildx build "${args[@]}" "$ROOT/$ctx"; }
  # 태그는 다음 굽기가 덮으므로 이번 굽기가 올린 digest 에 서명한다. 굽기 결과의 containerimage.digest 이고 여러 플랫폼이면
  # 인덱스의 digest 다. 서명에 이미지 이름과 도장을 실어, 이 이미지를 다른 이미지나 다른 도장의 태그 뒤에 두면 검증되지 않게 한다
  digest="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$WORK/$c.json")"
  echo ">> $c: 서명 $REGISTRY/$name@$digest"
  sign_image "$REGISTRY/$name@$digest" "$name" "$stamp" \
    || { echo "push-images: $c 서명 실패(위 줄). 이미지는 올라갔지만 서명이 다 붙지 않았다. 까닭을 고친 뒤 다시 올린다" >&2; exit 1; }
  pushed+=("$REGISTRY/$name:$stamp$SUFFIX")
done

echo
echo ">> 올린 이미지 (도장 태그의 플랫폼)"
for ref in "${pushed[@]}"; do
  platforms="$(docker buildx imagetools inspect "$ref" --format '{{range .Manifest.Manifests}}{{if ne .Platform.OS "unknown"}}{{.Platform.OS}}/{{.Platform.Architecture}} {{end}}{{end}}')"
  echo "   $ref  $platforms"
done

# 서명과 레지스트리가 잰 취약점을 본다(D55, D66). 수용 목록에 없는 CRITICAL 이나 스캔 결과가 없는 이미지가 있으면 올린 뒤라도
# 실패로 끝내 알린다. HIGH 는 끝 줄에 세어 보이기만 한다. 막을 것은 릴리스 전 미리 보기가 먼저 찾는다
echo
if [ "${SKIP_IMAGE_REPORT:-0}" = 1 ]; then
  echo "!! push-images: SKIP_IMAGE_REPORT=1 이라 서명과 취약점 보고를 건너뛰었다. 이미지는 올리고 서명했다. 취약점은 레지스트리의 스캐너로 따로 본다" >&2
  exit 0
fi
echo ">> 서명과 취약점 (zot 레지스트리는 올라온 이미지를 비동기로 잰다)"
if [ -n "$SUFFIX" ]; then
  echo ">> 아키텍처마다의 태그($SUFFIX)라 보고는 한 인덱스로 묶은 뒤에 한다"
  exit 0
fi
REGISTRY="$REGISTRY" bash "$ROOT/scripts/image-report.sh" "$@"
