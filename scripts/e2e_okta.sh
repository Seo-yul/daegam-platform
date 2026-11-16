#!/usr/bin/env bash
# P6 게이트 11번: 실제 Okta 실연동 검증 (스펙 phase-6 "Okta 실연동" 절).
#
#   DAEGAM_OKTA_E2E_ISSUER       예: https://<org>.okta.com/oauth2/<authServerId>
#   DAEGAM_OKTA_E2E_CLIENT_ID    client credentials 서비스 앱
#   DAEGAM_OKTA_E2E_CLIENT_SECRET
#   DAEGAM_OKTA_E2E_AUDIENCE     선택, 기본 daegam-api (authorization server 의 audience)
#   DAEGAM_OKTA_E2E_SCOPE        선택, 기본 daegam.api (Okta custom auth server 는 scope 필수)
#
# 셋 중 하나라도 없으면 "SKIP (no Okta env)" 를 내고 0 으로 끝난다 (CI 에서 무해).
# 있으면: 로컬 uvicorn 을 oidc 모드(issuer=Okta, 전용 Mongo DB)로 띄우고
#   (1) 토큰 없이 GET /me -> 401
#   (2) client_credentials 토큰으로 GET /me -> 200, subject_type == "client"
#   (3) GET /auth/config 의 auth_mode/issuer 가 Okta 와 일치
#   (4) 만료 토큰 검증은 스킵 (짧은 만료 토큰을 못 만든다)
# Mongo 는 make db-up 이 띄운 로컬(기본 mongodb://localhost:27017)을 쓴다.
set -euo pipefail

ISSUER="${DAEGAM_OKTA_E2E_ISSUER:-}"
CLIENT_ID="${DAEGAM_OKTA_E2E_CLIENT_ID:-}"
CLIENT_SECRET="${DAEGAM_OKTA_E2E_CLIENT_SECRET:-}"
AUDIENCE="${DAEGAM_OKTA_E2E_AUDIENCE:-daegam-api}"
SCOPE="${DAEGAM_OKTA_E2E_SCOPE:-daegam.api}"

if [ -z "$ISSUER" ] || [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  echo "SKIP (no Okta env): set DAEGAM_OKTA_E2E_ISSUER / DAEGAM_OKTA_E2E_CLIENT_ID / DAEGAM_OKTA_E2E_CLIENT_SECRET"
  exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "$HERE/../platform" && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
# port_free·uv_python·start_uvicorn·wait_healthy·stop_server·drop_mongo_db 는 여기에 있다
. "$HERE/lib/e2e_local_platform.sh"
PORT="${E2E_OKTA_PORT:-$PORT_OKTA_API}"
BASE="http://127.0.0.1:$PORT"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
MONGO_URL="${DAEGAM_MONGO_URL:-mongodb://localhost:27017}"
MONGO_DB="daegam_okta_e2e_$(date +%s)_$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-okta.XXXXXX")"
LOG="$WORK/uvicorn.log"
SERVER_PID=""
PY=""

cleanup() {
  local rc="$1"
  stop_server "$SERVER_PID"
  if [ $rc -ne 0 ]; then echo "!! e2e-okta failed (rc=$rc). uvicorn log:" >&2; tail -n 30 "$LOG" >&2 || true; fi
  drop_mongo_db "$PY" "$MONGO_URL" "$MONGO_DB" || true
  rm -rf "$WORK"
}
on_exit cleanup

# 이 게이트는 STEP 라벨 없이 첫 실패에서 끝낸다 (종료 코드 계약은 scripts/lib/e2e_report.sh 주석). die MSG 는 여기에 있다
. "$HERE/lib/die.sh"
step() { echo ">> $*"; }
jget() { json_of "$1" "$2"; }

# --- Okta 에서 토큰 -----------------------------------------------------------
step "discovery: $ISSUER/.well-known/openid-configuration"
curl -fsS "$ISSUER/.well-known/openid-configuration" -o "$WORK/disc.json" || die "discovery unreachable"
TOKEN_EP="$(jget "$WORK/disc.json" 'd["token_endpoint"]')"
[ -n "$TOKEN_EP" ] || die "no token_endpoint in discovery"
step "client_credentials token from $TOKEN_EP (scope=$SCOPE)"
code="$(curl -sS -o "$WORK/token.json" -w '%{http_code}' -u "$CLIENT_ID:$CLIENT_SECRET" \
  -d "grant_type=client_credentials&scope=$SCOPE" "$TOKEN_EP")"
[ "$code" = "200" ] || { cat "$WORK/token.json" >&2; die "token endpoint -> $code"; }
TOKEN="$(jget "$WORK/token.json" 'd["access_token"]')"
[ -n "$TOKEN" ] || die "no access_token in response"
step "token issued ($(jget "$WORK/token.json" 'd.get("expires_in")')s, scope=$(jget "$WORK/token.json" 'd.get("scope")'))"

# --- 플랫폼을 oidc 모드로 -----------------------------------------------------
port_free "$PORT" || die "port $PORT is already in use"
PY="$(uv_python "$PLATFORM_DIR")" || die "uv sync or venv python failed in $PLATFORM_DIR"
step "starting uvicorn on :$PORT (oidc, issuer=$ISSUER, MONGO_DB=$MONGO_DB)"
start_uvicorn "$PLATFORM_DIR" "$PY" daegam.main:app "$PORT" "$LOG" \
  DAEGAM_MONGO_URL="$MONGO_URL" DAEGAM_MONGO_DB="$MONGO_DB" \
  DAEGAM_AUTH_MODE=oidc DAEGAM_OIDC_ISSUER="$ISSUER" DAEGAM_OIDC_AUDIENCE="$AUDIENCE"
SERVER_PID=$UVICORN_PID
wait_healthy "$BASE/healthz" "$SERVER_PID" 60 0.5 || die "server did not become healthy (uvicorn log below)"

# --- 검사 ---------------------------------------------------------------------
code="$(curl -sS -o "$WORK/me401.json" -w '%{http_code}' "$API/me")"
[ "$code" = "401" ] || die "(1) GET /me without token -> $code (want 401)"
step "(1) PASS: no token -> 401"

code="$(curl -sS -o "$WORK/me.json" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$API/me")"
[ "$code" = "200" ] || { cat "$WORK/me.json" >&2; die "(2) GET /me with token -> $code"; }
SUBJECT_TYPE="$(jget "$WORK/me.json" 'd["subject_type"]')"
[ "$SUBJECT_TYPE" = "client" ] || die "(2) subject_type=$SUBJECT_TYPE (want client)"
step "(2) PASS: GET /me 200, sub=$(jget "$WORK/me.json" 'd["sub"]') subject_type=client"

curl -sS "$API/auth/config" -o "$WORK/cfg.json"
MODE="$(jget "$WORK/cfg.json" 'd["auth_mode"]')"; GOT_ISSUER="$(jget "$WORK/cfg.json" 'd.get("issuer")')"
[ "$MODE" = "oidc" ] || die "(3) auth_mode=$MODE (want oidc)"
[ "$GOT_ISSUER" = "$ISSUER" ] || die "(3) issuer=$GOT_ISSUER (want $ISSUER)"
step "(3) PASS: /auth/config auth_mode=oidc issuer matches"
step "(4) SKIP: expiry check (cannot mint a short-lived Okta token)"

gate_done
echo "e2e-okta: PASS"
