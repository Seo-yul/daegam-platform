# shellcheck shell=bash
# local 인증 모드(D40)의 로그인과 사람 만들기 한 벌. `. "$HERE/lib/e2e_local_auth.sh"` 로 읽는다.
# 부르는 쪽이 API·WORK 를 두고 e2e_json.sh 를 먼저 읽는다. 사람과 소속을 다루는 함수는 부르는 쪽의 관리자 `api`
# (METHOD PATH [JSON] -> 상태 코드, 본문은 $WORK/body)를 부를 때 읽는다.
#
# 비밀번호 셋은 게이트마다 같아야 한다. 한 게이트가 첫 로그인에서 바꿔 둔 관리자 비밀번호로 다음 게이트가 들어가기
# 때문이다(e2e-agent-local, e2e-pat).
#
# local_login USER PW
#   제어면 access_token 을 찍는다. 서버가 비밀번호 바꾸기를 강제하면 LOCAL_PW2 로 바꾸고 새 토큰을 찍는다. 실패하면 빈 줄이다
# local_login_any USER PW
#   바꾼 비밀번호(LOCAL_PW2)로 먼저, 안 되면 PW 로 들어간다. 이전 실행이 바꿔 두었을 수 있다
# local_login_kept USER PW
#   비밀번호를 바꾸지 않는 로그인이다. 운영자가 만든 계정(설치본의 e2e 관리자)에 쓴다. 바꾸기가 남았거나 실패하면 까닭을
#   stderr 에 찍고 1 이다
# local_user_id USERNAME
#   사용자 id 를 찍는다. 없으면 빈 줄이다. local 에서는 토큰의 sub 가 이름이 아니라 이 id 다
# local_user_ensure USERNAME
#   POST /users 로 만들고(있으면 409 라 그대로 쓴다) id 를 찍는다. 찾지 못하면 1
# local_set_member TENANT SUB [ROLE]
#   TENANT 의 manual 소속에서 SUB 를 ROLE 로 둔다. ROLE 이 비면 뺀다. PUT 은 manual 소속 전체 교체라 나머지는 GET 한
#   그대로 합쳐 보낸다
# local_fresh_member USERNAME TENANT
#   같은 이름의 사람이 있으면 지우고(e2e_api.sh 의 api_delete) 새로 만들어 TENANT 의 viewer 로 넣은 뒤 id 를 찍는다. 이전
#   실행이 남긴 사람을 잇지 않아 시간당 발급 상한과 첫 로그인 비밀번호가 실행마다 같아진다. 실패하면 1
# local_data_token LOGIN_TOKEN
#   그 로그인 토큰으로 데이터 플레인 토큰(POST /auth/token, MCP audience, groups 는 소속 테넌트의 daegam-<테넌트>)을 찍는다.
#   실패하면 0 이 아니다
# local_user_token USERNAME TENANT
#   사람을 만들어 TENANT 의 viewer 로 넣고 로그인해 데이터 플레인 토큰(local_data_token)을 찍는다
# install_e2e_password USER
#   E2E_INSTALL_PASSWORD, 없으면 macOS 키체인 항목 daegam-e2e-install(계정 USER)의 값을 찍는다. 둘 다 없으면 까닭을 찍고 1
# install_run_passwords
#   LOCAL_PW·LOCAL_PW2 를 이 실행만 아는 값으로 바꾼다. 공개 주소의 설치본에 만드는 버리는 사용자가 저장소에 적힌
#   비밀번호를 쓰지 않게 한다. 관리자는 E2E_INSTALL_PASSWORD 나 키체인 값이다
# install_admin STEP
#   운영자가 만든 e2e 전용 platform-admin(E2E_INSTALL_USER)과 그 비밀번호를 USER_NAME·PASSWORD 에 둔다. 컨텍스트가
#   orbstack 이거나 둘 가운데 하나가 없으면 die STEP
# local_admin_token STEP
#   로컬 클러스터의 첫 관리자 admin 으로 들어가(local_login_any, 부트스트랩 비밀번호) ADMIN_TOKEN 을 둔다. 안 되면 die STEP.
#   부르는 쪽이 e2e_report.sh 를 읽는다 (local_admin_session, e2e-provisioning)
# local_admin_session STEP
#   플랫폼 API 를 port-forward 하고(PF_API) 관리자로 로그인해 ADMIN_TOKEN·AUTH 를 둔다. orbstack 은 부트스트랩 관리자(바꾼
#   비밀번호 먼저), install 은 install_admin 이 둔 계정이다. platform-admin 인지 보고, 재적용이 끝나기를 기다리고(SINCE), 공개
#   주소(PUBLIC_URL)와 /auth/config 의 local·발급자를 맞댄 뒤 MCP_AUDIENCE 를 둔다. /auth/config 본문은 $WORK/body 에
#   남는다. 어긋나면 die STEP. 부르는 쪽이 TARGET·CTX·STARTED_AT·PLATFORM_NS·API_PORT 를 두고 e2e_pf.sh·
#   e2e_control_plane.sh·e2e_auth_mode.sh·e2e_api.sh·e2e_report.sh 를 읽는다 (e2e-pat, e2e-oauth)
# platform_connect STEP
#   모드를 바꾸지 않는 게이트가 플랫폼에 붙는다 (e2e-telemetry, e2e-web-tools, e2e-skills). TARGET 이 install 이면 install_admin 이
#   둔 e2e 관리자로 BASE(공개 주소)에 로그인해 ADMIN_TOKEN·AUTH 를 둔다. 정리 단계가 viewer 로 바뀐 AUTH 를 관리자로
#   되돌릴 때 ADMIN_TOKEN 을 쓴다. orbstack 이면 플랫폼 롤아웃을 기다리고 svc/daegam-platform 을 API_PORT 로
#   port-forward 해 PF_API 를 둔다(header 모드라 AUTH 는 빈 배열 그대로다). 부르는 쪽이 TARGET·CTX·K·BASE·API_PORT·
#   PLATFORM_NS 를 두고 e2e_pf.sh·e2e_report.sh 를 읽는다

# shellcheck disable=SC2034  # LOCAL_BOOT_PW 는 e2e_auth_mode.sh 의 switch_auth_mode 가 Secret 에 넣는다
LOCAL_BOOT_PW="e2e-bootstrap-password-1234"   # local 로 바꿀 때 Secret 에 넣는 첫 관리자 비밀번호
LOCAL_PW="e2e-first-password-1234"            # 사람을 만들 때 주는 비밀번호
LOCAL_PW2="e2e-changed-password-1234"         # 첫 로그인에 바꾸는 값. 다음 실행은 이 값으로 먼저 들어간다

# _local_post PATH TOKEN KEY VALUE... -> 상태 코드. 응답은 $WORK/login.json. 본문은 파이썬이 만들어 비밀번호의 따옴표도
# 그대로 가고 curl 의 인자에 비밀번호가 남지 않는다
_local_post() {
  local path="$1" token="$2" auth=()
  shift 2
  [ -z "$token" ] || auth=(-H "Authorization: Bearer $token")
  python3 -c 'import json,sys; a = sys.argv[1:]; print(json.dumps(dict(zip(a[0::2], a[1::2]))))' "$@" \
    | curl -sS -o "$WORK/login.json" -w '%{http_code}' -X POST "$API$path" ${auth[@]+"${auth[@]}"} \
      -H 'Content-Type: application/json' -d @- || true
}

_login_field() { # KEY -> $WORK/login.json 의 그 값. 없으면 빈 줄
  json_of "$WORK/login.json" "d.get('$1') or ''"
}

local_login() { # USER PW
  local tok
  [ "$(_local_post /auth/login "" username "$1" password "$2")" = 200 ] || return 0
  tok="$(_login_field access_token)"
  if [ "$(_login_field must_change_password)" = True ]; then
    [ "$(_local_post /auth/password "$tok" current_password "$2" new_password "$LOCAL_PW2")" = 200 ] || return 0
    tok="$(_login_field access_token)"
  fi
  printf '%s' "$tok"
}

local_login_any() { # USER PW
  local tok
  tok="$(local_login "$1" "$LOCAL_PW2")"
  [ -n "$tok" ] || tok="$(local_login "$1" "$2")"
  printf '%s' "$tok"
}

local_login_kept() { # USER PW
  local code
  code="$(_local_post /auth/login "" username "$1" password "$2")"
  [ "$code" = 200 ] || { echo "login as $1 -> $code" >&2; return 1; }
  if [ "$(_login_field must_change_password)" = True ]; then
    echo "$1 는 비밀번호를 바꿔야 한다. 화면에서 한 번 로그인해 바꾸고 키체인 값을 고친다" >&2
    return 1
  fi
  _login_field access_token
}

local_user_id() { # USERNAME
  api GET /users >/dev/null
  json_of "$WORK/body" "next((x['id'] for x in d if x['username'] == '$1'), '')"
}

local_user_ensure() { # USERNAME
  local uid
  api POST /users "{\"username\": \"$1\", \"password\": \"$LOCAL_PW\", \"platform_admin\": false}" >/dev/null
  uid="$(local_user_id "$1")"
  [ -n "$uid" ] || { echo "user $1 not found after POST /users: $(cat "$WORK/body")" >&2; return 1; }
  printf '%s\n' "$uid"
}

local_set_member() { # TENANT SUB [ROLE]
  local tenant="$1" sub="$2" role="${3:-}" members
  [ "$(api GET "/tenants/$tenant/members")" = 200 ] || { echo "GET /tenants/$tenant/members: $(cat "$WORK/body")" >&2; return 1; }
  members="$(json_of "$WORK/body" "json.dumps([{'sub': m['sub'], 'role': m['role']} for m in d
    if m['source'] == 'manual' and m['sub'] != '$sub'] + ([{'sub': '$sub', 'role': '$role'}] if '$role' else []))")" || return 1
  [ "$(api PUT "/tenants/$tenant/members" "$members")" = 200 ] || { echo "PUT /tenants/$tenant/members: $(cat "$WORK/body")" >&2; return 1; }
}

local_fresh_member() { # USERNAME TENANT
  local old uid
  old="$(local_user_id "$1")"
  [ -z "$old" ] || api_delete "/users/$old"
  uid="$(local_user_ensure "$1")" || return 1
  local_set_member "$2" "$uid" viewer || return 1
  printf '%s\n' "$uid"
}

local_data_token() { # LOGIN_TOKEN
  curl -fsS -X POST "$API/auth/token" -H "Authorization: Bearer $1" | json_of - 'd["access_token"]'
}

local_user_token() { # USERNAME TENANT
  local uid tok
  uid="$(local_user_ensure "$1")" || return 1
  local_set_member "$2" "$uid" viewer || return 1
  tok="$(local_login_any "$1" "$LOCAL_PW")"
  [ -n "$tok" ] || { echo "login as $1 failed" >&2; return 1; }
  local_data_token "$tok"
}

install_e2e_password() { # USER
  local pw="${E2E_INSTALL_PASSWORD:-}"
  [ -n "$pw" ] || pw="$(security find-generic-password -s daegam-e2e-install -a "$1" -w 2>/dev/null || true)"
  [ -n "$pw" ] || { echo "E2E_INSTALL_PASSWORD 도 키체인 항목 daegam-e2e-install(계정 $1)도 없다" >&2; return 1; }
  printf '%s\n' "$pw"
}

install_run_passwords() {
  LOCAL_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  LOCAL_PW2="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
}

install_admin() { # STEP
  [ "$CTX" != orbstack ] || die "$1" "install 대상인데 컨텍스트가 orbstack 이다 (orbstack 은 다른 e2e 들이 본다)"
  USER_NAME="${E2E_INSTALL_USER:-}"
  [ -n "$USER_NAME" ] || die "$1" "E2E_INSTALL_USER 가 비었다 (운영자가 만든 e2e 전용 platform-admin 계정, 설치 값 파일 또는 환경)"
  PASSWORD="$(install_e2e_password "$USER_NAME")" || die "$1" "no password for $USER_NAME"
}

# 첫 기동이면 부트스트랩 비밀번호로 들어가 서버가 강제하는 비밀번호 바꾸기를 지난다. 이전 실행이 바꿨으면 그 값이다
local_admin_token() { # STEP
  ADMIN_TOKEN="$(local_login_any admin "$LOCAL_BOOT_PW")"
  [ -n "$ADMIN_TOKEN" ] || die "$1" "local admin login failed with both the bootstrap and the changed password"
}

local_admin_session() { # STEP
  local step="$1" code
  start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID
  wait_port "$API_PORT" || die "$step" "port-forward to svc/daegam-platform did not open :$API_PORT"
  if [ "$TARGET" = orbstack ]; then
    local_admin_token "$step"
    SINCE=""   # wait_auth_reapplied 가 모드를 바꾼 시각을 쓴다
  else
    ADMIN_TOKEN="$(local_login_kept "$USER_NAME" "$PASSWORD")" || die "$step" "login as $USER_NAME failed"
    unset PASSWORD
    SINCE="$STARTED_AT"   # 이 실행이 시작된 뒤의 틱이어야 리컨실이 돈다고 본다
  fi
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  code="$(api GET /me)"
  [ "$code" = 200 ] && [ "$(jq_ 'd["is_platform_admin"]')" = True ] || die "$step" "GET /me with the admin token -> $code"
  wait_auth_reapplied "$ADMIN_TOKEN" "$SINCE" || die "$step" "data-plane auth is not settled (pending=0 failed=0)"
  PUBLIC_URL="$(platform_config DAEGAM_PUBLIC_URL)" || die "$step" "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  PUBLIC_URL="${PUBLIC_URL%/}"
  [ -n "$PUBLIC_URL" ] || die "$step" "ConfigMap $AUTH_MODE_CONFIGMAP has no DAEGAM_PUBLIC_URL"
  curl -fsS "$API/auth/config" -o "$WORK/body" || die "$step" "GET /auth/config failed"
  [ "$(jq_ 'd["auth_mode"], d.get("issuer")')" = "local $PUBLIC_URL" ] \
    || die "$step" "auth config: $(cat "$WORK/body") (want local, issuer $PUBLIC_URL)"
  MCP_AUDIENCE="$(jq_ 'd["mcp_audience"]')"
}

platform_connect() { # STEP
  local step="$1"
  if [ "$TARGET" = install ]; then
    [ -n "$CTX" ] || die "$step" "INSTALL_CONTEXT 가 비었다 (설치 값 파일)"
    install_admin "$step"
    ADMIN_TOKEN="$(local_login_kept "$USER_NAME" "$PASSWORD")" || die "$step" "login as $USER_NAME at $BASE failed"
    unset PASSWORD
    AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  else
    "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null \
      || die "$step" "deploy/daegam-platform did not finish its rollout"
    start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
    PF_API=$PF_PID
    wait_port "$API_PORT" || die "$step" "port-forward to svc/daegam-platform did not open :$API_PORT"
  fi
}
