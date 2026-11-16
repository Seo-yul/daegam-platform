#!/bin/sh
# daegam-ui 가 HTML 에 붙이는 Content-Security-Policy 한 줄을 찍는다 (D52).
#   daegam-csp             화면 응답의 CSP (frame-ancestors 'none')
#   daegam-csp --callback  /callback 응답의 CSP. silent renew iframe 이 그 페이지를 같은 origin 에서 연다
#                          (frame-ancestors 'self', 플랫폼 CALLBACK_FRAME_ANCESTORS)
#
# 플랫폼 platform/daegam/security_headers.py 의 content_security_policy 와 같은 값이어야 한다. 플랫폼은 API 응답에,
# 이 스크립트는 화면(HTML)에 붙인다. 같은지는 platform/tests/test_ui_csp_parity.py 가 개발기 셸로, 이미지 빌드의
# selftest.sh 가 이미지의 busybox 셸로 본다.
#
# 입력은 ConfigMap daegam-platform-config 의 두 키이고 읽는 법은 플랫폼 settings_from_env 와 같다.
#   DAEGAM_AUTH_MODE    키가 없을 때만 oidc. 앞뒤 공백을 지우고 소문자로 본다. oidc 일 때만 IdP 를 연다
#   DAEGAM_OIDC_ISSUER  앞뒤 공백과 끝의 / 를 지운 값에서 origin 을 뽑는다. 남는 것이 없으면 없는 것으로 본다
# 공백은 ASCII 여섯(C 로캘의 [:space:])이다. 플랫폼의 ENV_WHITESPACE 와 같다. NBSP 같은 유니코드 공백은 지우지 않는다
# 다음 둘은 까닭을 표준 오류에 적고 1 로 끝난다(start.sh 가 기동을 거절한다).
#   - 모드가 빈 값이거나 oidc·local·header 가 아니다. 플랫폼도 같은 값에서 기동을 거절한다
#   - 모드가 oidc 이고 발급자가 있는데 origin 이 아니다. IdP 없는 CSP 로 조용히 뜨면 브라우저 로그인만 막히고 원인은
#     어디에도 남지 않는다
set -eu
# 문자 범위([A-Za-z])를 로캘과 무관하게 ASCII 로 읽는다
LC_ALL=C
export LC_ALL

trim() {
  v=$1
  v=${v#"${v%%[![:space:]]*}"}
  v=${v%"${v##*[![:space:]]}"}
  printf '%s' "$v"
}

# origin 은 CSP host-part 문법의 허용 목록이다. scheme 은 http·https, 호스트는 [A-Za-z0-9-] 라벨을 . 으로 이은 것,
# 포트는 1~5자리 숫자다. 이 문자 밖(공백·줄바꿈·* , @ [ % 한글 등)이 하나라도 있으면 origin 이 아니다. 줄 단위 도구
# (grep)로 보지 않는다. 줄바꿈이 든 값이면 첫 줄만 맞아도 통과하기 때문이다. IPv6 literal 은 CSP 문법에 자리가 없다
valid_netloc() {
  n=$1
  case $n in
    '' | *[!A-Za-z0-9.:-]*) return 1 ;;
  esac
  host=${n%%:*}
  port=${n#"$host"}
  case $host in
    '' | .* | *. | *..*) return 1 ;;
  esac
  case $port in
    '') return 0 ;;
    :*) port=${port#:} ;;
  esac
  case $port in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "${#port}" -le 5 ]
}

origin_of() {
  url=$1
  case $url in
    *://*) ;;
    *) return 0 ;;
  esac
  scheme=$(printf '%s' "${url%%://*}" | tr 'A-Z' 'a-z')
  rest=${url#*://}
  netloc=${rest%%[/?#]*}
  case $scheme in
    http | https) ;;
    *) return 0 ;;
  esac
  if valid_netloc "$netloc"; then
    printf '%s://%s' "$scheme" "$netloc"
  fi
}

mode=$(trim "${DAEGAM_AUTH_MODE-oidc}" | tr 'A-Z' 'a-z')
case $mode in
  oidc | local | header) ;;
  *)
    echo "daegam-csp: DAEGAM_AUTH_MODE='$mode' 는 oidc·local·header 가 아니다. 플랫폼도 이 값으로는 뜨지 않는다" >&2
    exit 1
    ;;
esac
issuer=$(trim "${DAEGAM_OIDC_ISSUER-}")
# 플랫폼의 .rstrip("/") 와 같다. 끝의 / 만 있는 값은 발급자가 없는 것이다
issuer=${issuer%"${issuer##*[!/]}"}
idp=""
if [ "$mode" = "oidc" ] && [ -n "$issuer" ]; then
  idp=$(origin_of "$issuer")
  if [ -z "$idp" ]; then
    echo "daegam-csp: DAEGAM_AUTH_MODE 가 oidc 인데 DAEGAM_OIDC_ISSUER($issuer)에서 http(s)://호스트[:포트] origin 을 뽑지 못했다. 이대로 뜨면 CSP 가 IdP 를 막아 브라우저 로그인이 안 된다" >&2
    exit 1
  fi
fi

ancestors="'none'"
if [ "${1-}" = "--callback" ]; then
  ancestors="'self'"
fi
if [ -n "$idp" ]; then
  connect="'self' $idp"
  frame="'self' $idp"
  form="'self' $idp"
else
  connect="'self'"
  frame="'none'"
  form="'self'"
fi

printf "%s\n" "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data:; connect-src $connect; worker-src 'self' blob:; frame-src $frame; frame-ancestors $ancestors; base-uri 'self'; form-action $form; object-src 'none'"
