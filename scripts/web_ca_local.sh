#!/usr/bin/env bash
# 로컬 배포본의 Platform Settings `network.ca_bundle` 에 사내 방화벽 CA 를 넣는다 (P39b 11절, make web-ca-local).
# SSL 을 다시 서명하는 방화벽 뒤에서 플랫폼 web_fetch 복제본과 web_search 가 바깥 사이트를 믿게 한다. CA 파일은
# 저장소 밖(DAEGAM_CA_FILE)에 두고 설정 API 로만 보낸다. 인증은 header 모드(X-Daegam-Tenant) 전제이고, 토큰 모드
# 배포본이면 DAEGAM_TOKEN 을 Bearer 로 붙인다.
#   DAEGAM_CA_FILE=~/Downloads/corp-ca.pem make web-ca-local
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/hosts.sh"
# die MSG 는 여기에 있다
. "$HERE/lib/die.sh"
API="${DAEGAM_API:-http://$(local_host)/api}"
TENANT="${DAEGAM_TENANT:-default}"
CA_FILE="${DAEGAM_CA_FILE:-}"

[ -n "$CA_FILE" ] || die "DAEGAM_CA_FILE 에 CA 파일(PEM) 경로를 준다"
[ -s "$CA_FILE" ] || die "$CA_FILE 이 없거나 비었다"
grep -q -- "-----BEGIN CERTIFICATE-----" "$CA_FILE" || die "$CA_FILE 에 PEM 인증서가 없다"

curl_args=(-fsS -m 10 -H "X-Daegam-Tenant: $TENANT" -H "Content-Type: application/json")
[ -n "${DAEGAM_TOKEN:-}" ] && curl_args+=(-H "Authorization: Bearer $DAEGAM_TOKEN")

body="$(python3 -c 'import json, sys; print(json.dumps({"network": {"ca_bundle": open(sys.argv[1]).read()}}))' "$CA_FILE")"
curl "${curl_args[@]}" -X PATCH --data-binary @- -o /dev/null "$API/settings" <<<"$body" \
  || die "$API/settings 에 저장하지 못했다 (배포본이 떠 있는지, CA 가 PEM 인지 확인)"
echo ">> network.ca_bundle 을 $API/settings 에 넣었다. fetch 복제본은 재적용이 다시 렌더한다"
