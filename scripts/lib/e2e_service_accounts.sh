# shellcheck shell=bash
# shellcheck disable=SC2034  # 남기는 전역은 이 파일을 읽는 스크립트가 읽는다
# 서비스 어카운트(P53b 6.7절)의 만들기, 토큰 발급, 클레임 검사 한 벌 (e2e_agent, e2e_pat, e2e_oidc_platform).
# `. "$HERE/lib/e2e_service_accounts.sh"` 로 읽는다. 부르는 쪽이 WORK 를 두고 e2e_json.sh(json_of·jwt_json)·
# e2e_report.sh(die)·e2e_api.sh(api·api_delete·jq_)를 먼저 읽는다 (함수는 부를 때 읽는다). 호출은 api 가 싣는 AUTH 의
# 관리자로 간다. die 가 게이트를 멈춰야 하므로 sa_create·sa_issue 는 $( ) 안에서 부르지 않는다.
#
# sa_create STEP TENANT NAME ROLE
#   TENANT 에 ROLE 의 계정 NAME 을 만들고 id 를 SA_ID 에 남긴다. 앞 실행이 남긴 같은 이름은 지우고 다시 만든다
# sa_issue STEP TENANT ACCOUNT_ID LABEL password|id_token SECRET
#   그 계정의 토큰을 내고 평문을 SA_TOKEN 에 남긴다. local 모드는 관리자의 비밀번호(password), oidc 모드는 관리자가 새로
#   로그인한 ID 토큰(id_token, X-Daegam-Id-Token)으로 낸다. 비밀번호는 환경으로 파이썬에, 본문은 stdin 으로 curl 에
#   넘겨 어느 프로세스의 인자에도 남기지 않는다. ready_at 은 $WORK/ready_at 에 모은다. 평문이 든 본문은 지운다
# sa_claims_ok STEP TOKEN ACCOUNT_ID GROUP
#   토큰의 sub 가 sa:<ACCOUNT_ID>, groups 가 [GROUP] 이고 preferred_username 이 없지 않으면 클레임을 찍고 die STEP

SA_ID=""; SA_TOKEN=""

sa_create() { # STEP TENANT NAME ROLE
  local code left body="{\"name\": \"$3\", \"role\": \"$4\"}"
  code="$(TENANT="$2" api POST "/tenants/$2/service-accounts" "$body")"
  if [ "$code" = 409 ]; then
    [ "$(TENANT="$2" api GET "/tenants/$2/service-accounts")" = 200 ] || die "$1" "GET /tenants/$2/service-accounts failed"
    left="$(jq_ "next((a['account_id'] for a in d['accounts'] if a['name'] == '$3'), '')")"
    [ -z "$left" ] || TENANT="$2" api_delete "/tenants/$2/service-accounts/$left"
    code="$(TENANT="$2" api POST "/tenants/$2/service-accounts" "$body")"
  fi
  [ "$code" = 201 ] || die "$1" "POST /tenants/$2/service-accounts $3 -> $code $(cat "$WORK/body")"
  SA_ID="$(jq_ 'd["account_id"]')"
}

sa_issue() { # STEP TENANT ACCOUNT_ID LABEL password|id_token SECRET
  local code
  if [ "$5" = id_token ]; then
    code="$(TENANT="$2" api POST "/tenants/$2/service-accounts/$3/tokens" "{\"label\": \"$4\"}" -H "X-Daegam-Id-Token: $6")"
  else
    code="$(E2E_SA_PW="$6" python3 -c 'import json, os, sys; print(json.dumps({"label": sys.argv[1], "password": os.environ["E2E_SA_PW"]}))' "$4" \
      | TENANT="$2" api POST "/tenants/$2/service-accounts/$3/tokens" @-)"
  fi
  [ "$code" = 201 ] || die "$1" "issuing a token of service account $3 -> $code $(cat "$WORK/body")"
  SA_TOKEN="$(jq_ 'd["token"]')"
  jq_ 'd.get("ready_at") or ""' >>"$WORK/ready_at"
  : >"$WORK/body"
}

sa_claims_ok() { # STEP TOKEN ACCOUNT_ID GROUP
  [ "$(jwt_json "$2" 1 | json_of - "d['sub'] == 'sa:$3' and d['groups'] == ['$4'] and 'preferred_username' not in d")" = True ] \
    || die "$1" "service-account token claims: $(jwt_json "$2" 1)"
}
