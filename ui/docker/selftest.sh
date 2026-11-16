#!/bin/sh
# 이미지 빌드 때 이미지의 busybox 셸로 daegam-csp 와 기동 거절의 대표 사례를 돌린다 (D52). 파이썬과의 대칭은
# platform/tests/test_ui_csp_parity.py 가 개발기 셸로 전부 본다. 여기서는 이미지 안의 셸이 같은 결과를 내는지,
# start.sh 가 daegam-csp 의 거절을 기동 거절로 잇는지, 내려가는 중 표시를 두고 보고 지우는 세 파일이 맞물리는지를
# 본다. 하나라도 어긋나면 빌드가 멈춘다.
set -eu
fail() {
  echo "daegam-ui selftest: $*" >&2
  exit 1
}
csp() { env -i PATH="$PATH" "$@" /usr/local/bin/daegam-csp; }
refused() { ! env -i PATH="$PATH" "$@" /usr/local/bin/daegam-csp >/dev/null 2>&1; }
nl='
'
nbsp=$(printf '\302\240')

out=$(csp DAEGAM_AUTH_MODE=header DAEGAM_OIDC_ISSUER=https://idp.example.com)
case $out in *"connect-src 'self'; "*"frame-src 'none'; "*) ;; *) fail "header 는 IdP 를 열지 않는다: $out" ;; esac

out=$(csp DAEGAM_OIDC_ISSUER=' https://IdP.example.com:8443/realms/x?y#z ')
case $out in
  *"connect-src 'self' https://IdP.example.com:8443; "*"frame-src 'self' https://IdP.example.com:8443; "*"form-action 'self' https://IdP.example.com:8443; "*) ;;
  *) fail "모드 키가 없으면 oidc 이고 origin 만 연다: $out" ;;
esac

out=$(env -i PATH="$PATH" DAEGAM_AUTH_MODE=oidc DAEGAM_OIDC_ISSUER=https://idp.example.com /usr/local/bin/daegam-csp --callback)
case $out in *"frame-ancestors 'self'; "*) ;; *) fail "--callback 은 같은 origin 의 프레임만 연다: $out" ;; esac
case $(csp DAEGAM_AUTH_MODE=oidc DAEGAM_OIDC_ISSUER=https://idp.example.com) in
  *"frame-ancestors 'none'; "*) ;;
  *) fail "화면 CSP 는 프레임을 막는다" ;;
esac

out=$(csp DAEGAM_AUTH_MODE=' OIDC ' DAEGAM_OIDC_ISSUER=HTTPS://idp.example.com)
case $out in *"connect-src 'self' https://idp.example.com; "*) ;; *) fail "모드는 공백·대소문자를 무시하고 scheme 은 소문자다: $out" ;; esac

out=$(csp DAEGAM_AUTH_MODE=local 'DAEGAM_OIDC_ISSUER=https://idp;script-src *')
case $out in *"frame-src 'none'; "*"form-action 'self'; "*) ;; *) fail "local 은 발급자를 보지 않는다: $out" ;; esac

out=$(csp DAEGAM_AUTH_MODE=oidc DAEGAM_OIDC_ISSUER=' // ')
case $out in *"frame-src 'none'; "*) ;; *) fail "끝의 / 만 있는 발급자는 없는 것이다(플랫폼의 rstrip): $out" ;; esac

# NBSP 는 공백이 아니라 값의 일부다(파이썬 ENV_WHITESPACE 와 같다)
for issuer in 'https://idp;script-src *' 'https://*' 'https://[::1]:8443' "https://idp.example.com${nl}.evil" "https://idp.example.com${nbsp}"; do
  refused DAEGAM_AUTH_MODE=oidc "DAEGAM_OIDC_ISSUER=$issuer" || fail "oidc 인데 origin 이 아닌 발급자는 거절한다: $issuer"
done
refused DAEGAM_AUTH_MODE= || fail "빈 모드는 거절한다"
refused DAEGAM_AUTH_MODE=basic || fail "모르는 모드는 거절한다"
refused "DAEGAM_AUTH_MODE=oidc${nbsp}" || fail "NBSP 가 붙은 모드는 모르는 모드다"

if env -i PATH="$PATH" DAEGAM_AUTH_MODE=oidc 'DAEGAM_OIDC_ISSUER=https://user@idp' /usr/local/bin/daegam-ui-start --check >/dev/null 2>&1; then
  fail "start.sh 가 daegam-csp 의 거절을 기동 거절로 잇지 않는다"
fi

# 내려가는 중 표시는 세 파일이 같은 경로로 쓴다. preStop 의 daegam-ui-drain 이 두면 준비 확인(nginx.conf 의
# 8081 /ready)이 503 이고, 다시 뜬 컨테이너의 기동(start.sh)이 지운다. 지우지 않으면 준비 확인이 계속 503 이다
env -i PATH="$PATH" DAEGAM_AUTH_MODE=header /usr/local/bin/daegam-ui-start >/dev/null 2>&1 &
nginx_pid=$!
ready() { wget -q -O /dev/null http://127.0.0.1:8081/ready 2>/dev/null; }
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ! ready || break
  sleep 1
done
ready || fail "준비 확인(8081 /ready)이 기동 뒤 앱 껍데기를 내지 않는다"
/usr/local/bin/daegam-ui-drain 0 0
! ready || fail "daegam-ui-drain 이 둔 표시를 준비 확인이 보지 않는다"
kill "$nginx_pid"
wait "$nginx_pid" || true
env -i PATH="$PATH" DAEGAM_AUTH_MODE=header /usr/local/bin/daegam-ui-start --check >/dev/null 2>&1 \
  || fail "start.sh --check 가 header 모드에서 실패한다"
[ ! -e /tmp/daegam/draining ] || fail "start.sh 가 daegam-ui-drain 이 둔 표시를 지우지 않는다"
rm -rf /tmp/daegam /tmp/nginx

echo "daegam-ui selftest: ok"
