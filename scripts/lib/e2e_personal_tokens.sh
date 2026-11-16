# shellcheck shell=bash
# shellcheck disable=SC2034  # 남기는 전역과 한도는 이 파일을 읽는 스크립트가 읽는다
# 개인 MCP 토큰(D58)의 발급과 키 워밍업 기다림 한 벌. `. "$HERE/lib/e2e_personal_tokens.sh"` 로 읽는다.
# 부르는 쪽이 WORK·TOKEN_DAYS(발급하는 토큰의 날수)를 두고 e2e_json.sh·e2e_report.sh(die)·e2e_api.sh(api_as·jq_)를,
# 비밀번호로 낼 때는 e2e_local_auth.sh(LOCAL_PW2)도 먼저 읽는다 (함수는 부를 때 읽는다). e2e_pat, e2e_oauth,
# e2e_oidc_platform 이 쓴다.
#
# issue TOKEN TENANT LABEL PW [ID_TOKEN]
#   TOKEN(그 사람의 제어면 토큰)으로 TENANT 의 개인 토큰을 청하고 상태 코드를 찍는다. 본문은 $WORK/body 이고 평문 토큰은
#   token 칸이다. 테넌트 헤더를 토큰의 테넌트로 둔다. 비밀번호는 환경으로 파이썬에, 본문은 stdin 으로 curl 에 넘겨 어느
#   프로세스의 인자에도 남기지 않는다. oidc 모드는 PW 를 비우고 최근 로그인의 ID 토큰을 ID_TOKEN 으로 준다
#   (X-Daegam-Id-Token)
# issue_ok STEP TOKEN TENANT LABEL [ID_TOKEN]
#   바꾼 비밀번호(LOCAL_PW2)로, ID_TOKEN 이 있으면 그것으로 issue 한다. 평문은 ISSUED, id 는 ISSUED_ID 에 남기고 ready_at
#   은 $WORK/ready_at 에 모은다. 201 이 아니면 die STEP. 평문이 든 본문은 die 가 찍지 않게 지운다
# ready_wait
#   대상 파드를 굴리지 않을 때의 워밍업. 모은 ready_at 가운데 가장 늦은 것에 READY_SLACK 을 더한 때까지 남은 초(0 이상,
#   상한 READY_WAIT_MAX)를 찍는다. ready_at 은 서버가 낸 UTC 이고 시간대가 없으면 UTC 로 읽는다
#
# 개인 토큰은 토큰마다 키가 있어 검증기가 그 키를 JWKS 에서 받기 전에는 401 이다. orbstack 은 발급한 뒤 JWKS_CACHE_WAIT
# 만큼 기다려 대상을 배포하고(새 파드는 첫 요청에서 JWKS 를 받는다), 설치본은 E2E_POD_RESTART 가 true 면 같은 만큼 기다려
# 대상의 vMCP 파드를 다시 띄우고 false 면 대상을 굴리지 않으므로 ready_wait 만큼 기다린다.

JWKS_CACHE_WAIT=35     # 레플리카 JWKS 캐시 30초에 5초 여유
READY_SLACK=60         # 굴리지 않는 워밍업이 ready_at 뒤에 더 기다리는 초
READY_WAIT_MAX=1260    # 굴리지 않는 워밍업의 상한 21분(워밍업 20분 + 여유)

ISSUED=""; ISSUED_ID=""

issue() { # TOKEN TENANT LABEL PW [ID_TOKEN]
  local id_header=()
  [ -z "${5:-}" ] || id_header=(-H "X-Daegam-Id-Token: $5")
  E2E_PAT_PW="$4" python3 -c 'import json, os, sys, datetime as t
exp = t.datetime.now(t.timezone.utc) + t.timedelta(days=int(sys.argv[3]))
body = {"tenant": sys.argv[1], "label": sys.argv[2], "expires_at": exp.isoformat()}
if os.environ["E2E_PAT_PW"]:
    body["password"] = os.environ["E2E_PAT_PW"]
print(json.dumps(body))' \
    "$2" "$3" "$TOKEN_DAYS" | TENANT="$2" api_as "$1" POST /me/tokens @- ${id_header[@]+"${id_header[@]}"}
}

issue_ok() { # STEP TOKEN TENANT LABEL [ID_TOKEN]
  local code
  if [ -n "${5:-}" ]; then code="$(issue "$2" "$3" "$4" "" "$5")"; else code="$(issue "$2" "$3" "$4" "$LOCAL_PW2")"; fi
  [ "$code" = 201 ] || die "$1" "POST /me/tokens tenant=$3 -> $code $(cat "$WORK/body")"
  ISSUED="$(jq_ 'd["token"]')"; ISSUED_ID="$(jq_ 'd["token_id"]')"
  jq_ 'd.get("ready_at") or ""' >>"$WORK/ready_at"
  : >"$WORK/body"
}

ready_wait() {
  python3 - "$WORK/ready_at" "$READY_SLACK" "$READY_WAIT_MAX" <<'PY'
import datetime as t, sys
now = t.datetime.now(t.timezone.utc)
def at(s):
    v = t.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return v if v.tzinfo else v.replace(tzinfo=t.timezone.utc)
times = [at(s) for s in open(sys.argv[1]).read().split() if s]
left = max([(x - now).total_seconds() + int(sys.argv[2]) for x in times], default=0)
print(int(min(max(left, 0), int(sys.argv[3]))))
PY
}
