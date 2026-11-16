# shellcheck shell=bash
# 플랫폼 곁에 둔 Redis 의 평문 포트 6379 를 닫았는가 (P61 F4). 그 Redis 를 두는 배포가 대감 차트의 home.shippedRedis 로
# ConfigMap daegam-platform-config 의 DAEGAM_HOME_REDIS_TLS 에 "plain" 을 적고, 플랫폼은 같은 값을 읽어 기록에 닫힌 표시를
# 적는다(platform/daegam/services/shipped_redis.py mark_shipped_plain). 그 배포와 scripts/e2e_install.sh 가
# `. "$HERE/lib/redis_plain.sh"` 로 읽는다. ConfigMap 을 읽는 것은 부르는 쪽이다.

# redis_plain_of RAW  DAEGAM_HOME_REDIS_TLS 의 값 RAW 로 closed 나 open 을 찍는다. 플랫폼 설정(daegam.config
# _home_redis_tls_from_env)처럼 JSON 으로 읽는다. 값이 비면(첫 배포) open 이다. JSON 이 아니거나 plain 이 없거나
# 참·거짓이 아니면 실패한다
redis_plain_of() {
  [ -n "$1" ] || { echo open; return; }
  python3 -c '
import json, sys
plain = json.loads(sys.argv[1]).get("plain")
if not isinstance(plain, bool):
    sys.exit("plain must be true or false")
print("open" if plain else "closed")' "$1"
}
