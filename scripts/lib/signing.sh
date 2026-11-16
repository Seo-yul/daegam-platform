# shellcheck shell=bash
# 이미지 서명과 검증 (D66). push-images.sh 가 서명하고, resolve-images.sh 가 값 파일을 쓰기 전에 검증하고, image-report.sh 가 같은
# 검증으로 서명 칸을 채운다. 세 스크립트가 `. "$HERE/lib/signing.sh"` 로 읽는다.
# 개인 키는 비밀번호로 암호화한 cosign 키이고 올리는 컴퓨터의 SIGNING_KEY_DEFAULT 에 둔다(COSIGN_KEY 로 경로를 바꾼다). 이미지를
# 바꿔치기하려는 쪽이 노리는 곳이 레지스트리와 클러스터이므로 키는 그 둘에도 저장소에도 두지 않는다. 비밀번호는 어디에도
# 적지 않는다. 올릴 때마다 터미널에서 받아 cosign 을 부를 때만 넘긴다. 서명에는 이미지 이름과 도장을 함께 싣는다. 그래서
# 우리가 서명한 이미지라도 다른 이미지나 다른 도장의 태그 뒤에 옮겨 놓으면 검증되지 않는다. 서명은 공개 투명성 로그에
# 올리지 않는다. 내부 이미지의 이름이 공개 로그에 남기 때문이다. 그래서 검증도 그 로그를 보지 않는다. 공개 키는 저장소의
# SIGNING_PUB_DEFAULT 에 커밋하고 누구나 그것으로 검증한다(COSIGN_PUB 로 경로를 바꾼다).

# 메시지 앞에 붙이는 이름. 이 파일을 읽은 스크립트다
SIGNING_CALLER="$(basename "$0" .sh)"
# 저장소 뿌리와 그 안의 공개 키 경로. 기본 공개 키가 커밋된 그대로인지 git 으로 본다
SIGNING_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIGNING_PUB_PATH="deploy/cosign.pub"
# 개인 키의 기본 경로. 모드 600 으로 둔다
SIGNING_KEY_DEFAULT="$HOME/.config/daegam/cosign.key"
# 공개 키의 기본 경로
SIGNING_PUB_DEFAULT="$SIGNING_ROOT/$SIGNING_PUB_PATH"
# 서명에 싣는 주석의 키. 주석은 서명된 내용 안에 들어가고 검증이 같은 값을 요구한다
SIGNING_IMAGE_KEY="dev.daegam.image"
SIGNING_STAMP_KEY="dev.daegam.stamp"

# signing_key  쓸 개인 키 파일 경로를 찍는다
signing_key() {
  printf '%s\n' "${COSIGN_KEY:-$SIGNING_KEY_DEFAULT}"
}

# signing_pub  검증에 쓸 공개 키 파일 경로를 찍는다
signing_pub() {
  printf '%s\n' "${COSIGN_PUB:-$SIGNING_PUB_DEFAULT}"
}

# cosign_ready  cosign 이 없으면 설치하라고 표준 오류에 찍고 1 이다
cosign_ready() {
  command -v cosign >/dev/null || { echo "$SIGNING_CALLER: cosign 이 없다 (brew install cosign)" >&2; return 1; }
}

# pub_committed  기본 공개 키가 커밋된 그대로인지 본다. 추적되지 않거나 커밋 뒤에 바뀐 파일은 누가 둔 키인지 말하지 못하므로
# 믿지 않는다. 아니면 까닭을 표준 오류에 찍고 1 이다
pub_committed() {
  {
    git -C "$SIGNING_ROOT" ls-files --error-unmatch -- "$SIGNING_PUB_PATH" >/dev/null 2>&1 \
      && git -C "$SIGNING_ROOT" diff --quiet HEAD -- "$SIGNING_PUB_PATH"
  } || {
    echo "$SIGNING_CALLER: $SIGNING_PUB_PATH 이 커밋되지 않았거나 커밋 뒤에 바뀌었다. 키 쌍을 만들 때 나온 공개 키를 커밋한 그대로 쓴다" >&2
    return 1
  }
}

# verify_ready  검증할 수 있는지 레지스트리에 묻기 전에 본다. cosign 과 공개 키 파일이 있어야 하고, COSIGN_PUB 을 주지 않았으면
# 그 파일이 커밋된 그대로여야 한다. 준비되면 믿을 공개 키와 그 sha256 을 한 줄 찍고, 안 되면 무엇을 할지 표준 오류에 찍고 1 이다
verify_ready() {
  local pub
  pub="$(signing_pub)"
  cosign_ready || return 1
  [ -f "$pub" ] || {
    echo "$SIGNING_CALLER: 공개 키가 $pub 에 없다. 키 쌍을 만들 때 나온 공개 키를 그 경로에 커밋하거나 COSIGN_PUB 에 공개 키 파일 경로를 준다" >&2
    return 1
  }
  [ -n "${COSIGN_PUB:-}" ] || pub_committed || return 1
  echo ">> 서명을 확인할 공개 키 $pub (sha256 $(shasum -a 256 "$pub" | cut -c1-16))"
}

# signing_key_ready 키  개인 키 파일이 있고 그룹이나 다른 사용자가 읽을 수 없는지 본다. 읽을 수 있으면 암호화된 키를 가져가
# 비밀번호를 오프라인으로 맞혀 볼 수 있으므로 ssh 처럼 멈춘다. 안 되면 무엇을 할지 표준 오류에 찍고 1 이다
signing_key_ready() {
  [ -f "$1" ] || { echo "$SIGNING_CALLER: 서명 키 $1 가 없다. 그 경로에 키를 두거나 COSIGN_KEY 에 키 파일 경로를 준다" >&2; return 1; }
  [ -z "$(find -L "$1" \( -perm -004 -o -perm -040 \))" ] || {
    echo "$SIGNING_CALLER: 서명 키 $1 를 다른 사용자도 읽을 수 있다. chmod 600 $1 로 막은 뒤 다시 한다" >&2
    return 1
  }
}

# signing_password  서명 키 비밀번호를 SIGNING_PASSWORD 에 둔다. 환경 변수 COSIGN_PASSWORD 가 있으면 그것을 쓰고(터미널 없이
# 돌릴 때), 없으면 터미널에서 화면에 보이지 않게 묻는다. 받은 값은 내보내지 않고 cosign 을 부를 때만 넘긴다. 환경의
# COSIGN_PASSWORD 도 지워 docker·buildx·image-report 같은 다른 자식 프로세스가 물려받지 않게 한다. 받지 못하면 표준 오류에
# 찍고 1 이다
signing_password() {
  SIGNING_PASSWORD="${COSIGN_PASSWORD:-}"
  unset COSIGN_PASSWORD
  if [ -z "$SIGNING_PASSWORD" ] && { : </dev/tty; } 2>/dev/null; then
    printf '서명 키 %s 의 비밀번호: ' "$(signing_key)" >/dev/tty
    IFS= read -rs SIGNING_PASSWORD </dev/tty
    printf '\n' >/dev/tty
  fi
  [ -n "$SIGNING_PASSWORD" ] || {
    echo "$SIGNING_CALLER: 서명 키의 비밀번호를 받지 못했다. 터미널에서 make push-images 를 돌려 물을 때 치고, 터미널이 없으면 환경 변수 COSIGN_PASSWORD 로 준다" >&2
    return 1
  }
}

# signing_ready  서명할 수 있는지 올리기 전에 본다. 검증 준비(verify_ready), 개인 키 파일, 비밀번호를 차례로 보고, 그
# 비밀번호로 키를 열어 꺼낸 공개 키가 검증에 쓸 공개 키와 같은지 본다. 비밀번호가 틀렸거나 키 쌍이 어긋난 것을 첫 이미지를
# 올린 뒤에야 알지 않기 위해서다. 키 쌍이 어긋난 채 서명하면 어느 이미지도 검증되지 않는다. 안 되면 무엇을 할지 표준 오류에
# 찍고 1 이다
signing_ready() {
  local key pub derived
  key="$(signing_key)"
  pub="$(signing_pub)"
  verify_ready || return 1
  signing_key_ready "$key" || return 1
  signing_password || return 1
  derived="$(COSIGN_PASSWORD="$SIGNING_PASSWORD" cosign public-key --key "$key")" || {
    echo "$SIGNING_CALLER: 서명 키 $key 를 그 비밀번호로 열지 못했다(위 줄). 비밀번호가 맞는지 본다" >&2
    return 1
  }
  [ "$derived" = "$(cat "$pub")" ] || {
    echo "$SIGNING_CALLER: $pub 이 서명 키 $key 의 공개 키가 아니다. 같은 키 쌍의 공개 키를 커밋하거나 그 쌍의 개인 키를 COSIGN_KEY 로 준다" >&2
    return 1
  }
}

# sign_image 이미지@digest 이름 도장  그 digest 에 서명하고, 서명을 그 digest 를 가리키는 sigstore 번들(OCI referrer)로
# 레지스트리에 올린다. 이미지 이름과 도장을 주석으로 서명에 싣는다. 여러 아키텍처 인덱스면 플랫폼마다의 이미지에도 서명한다
# (--recursive). cosign 3 에서 투명성 로그를 끄려면 로그 서비스가 없는 서명 설정을 넘겨야 한다. 서비스 없이 만든 설정을
# 파일로 남기지 않고 그 자리에서 넘긴다. 비밀번호는 signing_ready 가 받아 둔 것이다
sign_image() {
  COSIGN_PASSWORD="$SIGNING_PASSWORD" cosign sign --key "$(signing_key)" --recursive -y \
    --signing-config <(cosign signing-config create --out /dev/stdout) \
    -a "$SIGNING_IMAGE_KEY=$2" -a "$SIGNING_STAMP_KEY=$3" "$1"
}

# verify_image 이미지 이름 [도장]  그 이미지에 공개 키로 확인되는 서명이 있고 서명에 실린 이미지 이름이 이름과 같은지 본다.
# 도장을 주면 도장도 같아야 한다. 태그면 cosign 이 지금 가리키는 digest 로 푼다. 인덱스의 digest 면 인덱스가 플랫폼마다의
# digest 를 담고 있으므로 그 서명 하나로 플랫폼 이미지까지 묶인다. 확인되면 아무것도 찍지 않고, 안 되면 cosign 이 댄 까닭을
# 표준 오류에 찍고 1 이다. 투명성 로그를 보지 않는다는 cosign 의 경고는 늘 나오므로 뺀다
verify_image() {
  local out
  local annotations=(-a "$SIGNING_IMAGE_KEY=$2")
  [ -z "${3:-}" ] || annotations+=(-a "$SIGNING_STAMP_KEY=$3")
  out="$(cosign verify --key "$(signing_pub)" --insecure-ignore-tlog=true "${annotations[@]}" "$1" 2>&1 >/dev/null)" || {
    printf '%s\n' "$out" | sed '/^WARNING: Skipping tlog verification/d' >&2
    return 1
  }
}
