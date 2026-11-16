#!/bin/sh
# daegam-ui 기동 (D52). CSP 를 설정 한 줄로 만들어 두고 nginx 를 앞으로 띄운다.
#
# 루트 파일 시스템은 읽기 전용이라 만든 파일은 /tmp(emptyDir) 에 둔다. CSP 는 daegam-csp 가 origin 꼴을 검사한 값만
# 담으므로 따옴표·$·세미콜론이 nginx 설정으로 새지 않는다. daegam-csp 가 실패하면(oidc 인데 발급자가 origin 이 아니다)
# 기동을 거절한다.
#   --check  설정만 만들고 nginx -t 로 문법을 본다. 이미지 빌드가 이것을 돌려 깨진 설정이면 이미지를 안 만든다
set -eu

mkdir -p /tmp/daegam /tmp/nginx
# 내려가는 중이라는 표시(preStop 의 daegam-ui-drain 이 둔다, D52)는 새로 뜬 컨테이너의 것이 아니다. /tmp 는
# 컨테이너가 다시 떠도 남는 emptyDir 이고 liveness 로 다시 뜰 때도 preStop 이 돌았으므로 지운다. 두면 준비 확인이
# 계속 503 이다
rm -f /tmp/daegam/draining
if ! csp=$(/usr/local/bin/daegam-csp) || ! callback_csp=$(/usr/local/bin/daegam-csp --callback); then
  echo "daegam-ui: 기동을 거절한다(위 daegam-csp 오류). ConfigMap daegam-platform-config 의 DAEGAM_OIDC_ISSUER 를 고친다" >&2
  exit 1
fi
# /callback 만 silent renew iframe 에 열린 CSP 를 받는다. 요청 줄의 원문으로 가른다(nginx.conf 의 $daegam_xfo 와 같은
# 까닭이다. try_files 가 안에서 /index.html 로 넘긴 뒤라 $uri 로는 가를 수 없다)
printf 'map $request_uri $daegam_csp {\n  default "%s";\n  "~^/callback(?:\\?|$)" "%s";\n}\n' "$csp" "$callback_csp" > /tmp/daegam/csp.conf
echo "daegam-ui: auth_mode=${DAEGAM_AUTH_MODE-(없음, oidc)} csp=$csp" >&2

if [ "${1-}" = "--check" ]; then
  exec nginx -t
fi
exec nginx -g 'daemon off;'
