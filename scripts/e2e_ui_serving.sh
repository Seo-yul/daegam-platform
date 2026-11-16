#!/usr/bin/env bash
# P30 (D52) UI 컨테이너 e2e. make image-platform image-ui 와 make deploy-local 뒤에 돈다.
#
#   make e2e-ui-serving
# Env: KUBE_CONTEXT(orbstack) PLATFORM_NS(daegam-system) E2E_UI_PORT·E2E_API_PORT(port-forward, lib/e2e_ports.sh)
#      E2E_UI_HOST(deploy/dev/values-local.yaml 의 host, lib/hosts.sh) TRAEFIK_NS·TRAEFIK_SVC(traefik) E2E_TRAEFIK_PORT(LoadBalancer IP 가 없을 때의 port-forward)
#      E2E_EXPECT_UI_BUILD   서빙돼야 하는 UI 빌드. 비우면 0.1.0+$(scripts/build-stamp.sh ui)
#      E2E_EXPECT_API_BUILD  서빙돼야 하는 플랫폼 빌드. 비우면 0.1.0+$(scripts/build-stamp.sh platform)
#
# 두 갈래다. 직결 갈래(STEP 1~3)는 svc/daegam-ui 로 port-forward 해 Ingress 를 지나지 않고 본다. Traefik 은 클라이언트의
# X-Forwarded-Proto 를 덮어쓰고 경로의 `..` 를 먼저 접으므로 HSTS·정규화·경로 규칙은 여기서만 볼 수 있다. Traefik
# 갈래(STEP 4~7)는 브라우저가 밟는 길(로컬 호스트, curl --resolve)로 본다. Traefik Service 에 LoadBalancer IP 가 없는
# 클러스터(설치본 등)에서는 그 Service 로 port-forward 하고 Host 헤더를 붙여 같은 것을 본다. 그런 클러스터에서 STEP 6(orbstack
# 전용 절차)과 STEP 7(header 배포본 전용)은 조건이 안 맞아 돌지 않는 것이라 세지 않는다(SKIP conditional).
#   STEP 1  빌드: /build.json 의 ui_build 가 기대 빌드다(배포한 것이 지금 코드인지)
#   STEP 2  정적 규칙(D20·D52): / 와 딥링크는 index.html·no-cache, index.html 이 가리키는 에셋은 200·immutable·
#           application/javascript, 없는 에셋과 /assets/ 디렉터리는 404 이고 immutable 이 없다, POST / 405, HEAD / 200,
#           /assets/../index.html 은 no-cache, 루트 밖으로 나가는 경로는 400
#   STEP 3  보안 헤더: 200·404·405·400 모두 고정 헤더 다섯, CSP 는 같은 모드의 플랫폼 CSP 와 같다(플랫폼도
#           port-forward 로 본다), HSTS 는 X-Forwarded-Proto 첫 값이 https 일 때만, Server 헤더에 판 번호가 없다
#   STEP 4  경로 분할: /api/version 은 플랫폼 JSON 이고 api_build 가 기대 빌드이며 쿠키를 주지 않는다, /build.json 은
#           UI, /healthz 와 /.well-known/openid-configuration 은 플랫폼 JSON, /api/nope 는 플랫폼 JSON 404, /apiary 는 UI 의
#           index.html, / 는 sticky 쿠키 daegam_ui 를 준다(HttpOnly, SameSite=Lax, 공개 주소가 https 일 때만 Secure)
#   STEP 5  화면(/)과 API(/api/version)의 CSP 가 Traefik 을 지나서도 같다
#   STEP 6  UI 만 바꾼 배포(make image-ui UI_STAMP=e2e-<시각> 뒤 make deploy-ui-local)가 플랫폼 파드의 UID·재시작 수와
#           api_build 를 그대로 두고, /build.json 이 새 도장이다. 그 동안 새로 연 탭을 두 브라우저로 흉내 낸다. 매번 쿠키
#           없이 오는 브라우저와 쿠키를 들고 계속 오는 브라우저다. 탭은 / 를 받고 받은 쿠키로 index.html 이 가리키는 진입
#           번들을 부른다. / 의 200 아닌 응답과 새 브라우저 탭의 진입 번들 실패가 0개여야 한다(D52). 쿠키를 든 브라우저의
#           진입 번들은 붙은 파드가 Traefik 에서 빠지는 순간 두 요청 사이에 걸리면 다른 파드로 가므로 세지 않고 잰다.
#           되돌리기는 EXIT trap 이 적어 둔 태그로 한다(다시 굽지 않는다). orbstack 밖에서는 SKIP 이다
#   STEP 7  인증 모드 전환(lib/e2e_auth_mode.sh): oidc 로 두면 화면과 API 의 CSP 가 mock-oidc origin 을 열고 플랫폼이
#           무토큰 401, header 로 되돌리면 IdP 가 빠진다. 되돌린 뒤에는 플랫폼이 데이터 플레인 인증을 다시 적용할 때까지
#           기다린다(P32). 배포본이 header 가 아니거나 mock-oidc 가 없으면 SKIP 이다
#
# 출력과 종료 코드는 lib/e2e_report.sh 의 계약을 따른다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/hosts.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
UI_PORT="${E2E_UI_PORT:-$PORT_UI_DIRECT}"
API_PORT="${E2E_API_PORT:-$PORT_UI_API}"
EXPECT_UI="${E2E_EXPECT_UI_BUILD:-0.1.0+$(sh "$ROOT/scripts/build-stamp.sh" ui)}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-ui.XXXXXX")"
K=(kubectl --context "$CTX")
PF_UI=""; PF_API=""; PF_TR=""
TR_PORT="${E2E_TRAEFIK_PORT:-$PORT_UI_TRAEFIK}"
UI_VARIANT=0; ORIG_IMAGE=""; ORIG_TAG=""; ORIG_LOCAL_ID=""; VARIANT_REF=""; LOOP_PID=""

. "$HERE/lib/e2e_report.sh"
. "$HERE/lib/e2e_json.sh"
# 응답 헤더 값을 읽는 hdr NAME 과 본문을 읽는 jq_ EXPR 은 여기에 있다. req 가 헤더를 그 기본 파일 $WORK/hdr 에,
# 본문을 $WORK/body 에 남긴다
. "$HERE/lib/e2e_api.sh"
# start_pf·stop_pf·wait_port (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
# roll_control_plane·platform_config (K·PLATFORM_NS 를 쓴다)
. "$HERE/lib/e2e_control_plane.sh"
# switch_auth_mode·restore_auth_mode·mock_oidc_deployed (STEP 7 이 바꾸고 되돌린다. 도중에 죽으면
# cleanup 이 되돌린다). 되돌린 뒤의 기다림은 API port-forward 로 하므로 cleanup 은 그것을 닫기 전에 되돌린다
. "$HERE/lib/e2e_auth_mode.sh"

# ui_pods_on IMAGE  내려가는 중인 것까지 daegam-ui 파드가 전부 IMAGE 를 돌 때까지 60초 기다린다. rollout status 는 옛 파드가
# preStop 으로 내려가는 동안에도 끝나고, 그 사이에는 옛 파드가 Traefik 뒤에서 아직 답한다
ui_pods_on() {
  local got
  for _ in $(seq 1 60); do
    got="$("${K[@]}" -n "$PLATFORM_NS" get pod -l app=daegam-ui \
      -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' 2>/dev/null | sort -u)"
    [ "$got" = "$1" ] && return 0
    sleep 1
  done
  return 1
}

# STEP 6 이 올린 시험용 UI 를 적어 둔 태그로 되돌린다. 다시 굽지 않는다. 작업 트리가 배포 뒤에 바뀌었으면 다시 구운 것은
# 원래 이미지가 아니기 때문이다. 로컬 daegam-ui:0.1.0 은 굽기 전에 가리키던 이미지로 돌려놓는다. 두면 다음
# deploy-ui-local 이 시험용 이미지를 올린다. 배포본이 아니라 굽기 전 값인 것은 사용자가 구워만 두고 아직 올리지 않은
# 이미지일 수 있어서다. 시험용 이미지는 그것을 돌던 컨테이너가 다 지워진 뒤에야 지울 수 있다
restore_ui() {
  [ "$UI_VARIANT" = 1 ] || return 0
  UI_VARIANT=0
  echo ">> restoring daegam-ui to $ORIG_IMAGE"
  make -C "$ROOT" --no-print-directory deploy-ui-local UI_TAG="$ORIG_TAG" >/dev/null 2>&1 \
    || echo "!! restore failed. run: make deploy-ui-local UI_TAG=$ORIG_TAG"
  if [ -n "$ORIG_LOCAL_ID" ]; then
    docker tag "$ORIG_LOCAL_ID" daegam-ui:0.1.0 2>/dev/null \
      || echo "!! could not point daegam-ui:0.1.0 back at $ORIG_LOCAL_ID"
  else
    # 굽기 전에는 로컬 daegam-ui:0.1.0 이 없었다
    docker rmi daegam-ui:0.1.0 >/dev/null 2>&1 || true
  fi
  ui_pods_on "$ORIG_IMAGE" || echo "!! daegam-ui pods did not settle on $ORIG_IMAGE"
  if [ -n "$VARIANT_REF" ] && [ "$VARIANT_REF" != "${ORIG_IMAGE##*/}" ]; then
    for _ in $(seq 1 10); do docker rmi "$VARIANT_REF" >/dev/null 2>&1 && break; sleep 2; done
    if docker image inspect "$VARIANT_REF" >/dev/null 2>&1; then echo "!! could not remove $VARIANT_REF (docker rmi $VARIANT_REF)"; fi
  fi
}

cleanup() {
  if [ -n "$LOOP_PID" ]; then kill "$LOOP_PID" 2>/dev/null || true; fi
  restore_ui
  restore_auth_mode
  [ -n "$PF_UI" ] && stop_pf "$PF_UI" "$UI_PORT"
  [ -n "$PF_API" ] && stop_pf "$PF_API" "$API_PORT"
  [ -n "$PF_TR" ] && stop_pf "$PF_TR" "$TR_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# req BASE METHOD PATH [curl 인자...]  마지막 응답: 코드는 $CODE, 헤더는 $WORK/hdr, 본문은 $WORK/body
req() {
  local base=$1 method=$2 path=$3
  shift 3
  CODE="$(curl -sS -o "$WORK/body" -D "$WORK/hdr" -w '%{http_code}' --max-time 20 -X "$method" "$@" "$base$path" 2>/dev/null)" || CODE=000
}
FIXED="x-content-type-options x-frame-options referrer-policy permissions-policy content-security-policy"
has_fixed() { # has_fixed LABEL  마지막 응답에 고정 헤더 다섯이 다 있는가
  local h
  for h in $FIXED; do [ -n "$(hdr "$h")" ] || die "$STEP" "$1: no $h header"; done
}

kill_stale_pf "$UI_PORT" "$API_PORT" "$TR_PORT"
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-ui --timeout=60s >/dev/null \
  || die 0 "deploy/daegam-ui is not rolled out (make image-ui && make deploy-ui-local)"
start_pf "$PLATFORM_NS" svc/daegam-ui "${UI_PORT}:8080"; PF_UI=$PF_PID
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID
wait_port "$UI_PORT" || die 0 "port-forward svc/daegam-ui :$UI_PORT did not open"
wait_port "$API_PORT" || die 0 "port-forward svc/daegam-platform :$API_PORT did not open"
UI="http://127.0.0.1:$UI_PORT"
BASE="http://127.0.0.1:$API_PORT"
# lib/e2e_auth_mode.sh 가 상태 API 를 부르는 뿌리다
API="$BASE/api"

# ---------- 1. 빌드 ----------
STEP=1
req "$UI" GET /build.json
[ "$CODE" = 200 ] || die 1 "/build.json -> $CODE"
got="$(jq_ "d.get('ui_build')" || true)"
[ "$got" = "$EXPECT_UI" ] || die 1 "/build.json ui_build=$got, expected $EXPECT_UI (make image-ui, then make deploy-ui-local)"
pass 1 "daegam-ui serves $EXPECT_UI"

# ---------- 2. 정적 규칙 ----------
STEP=2
req "$UI" GET /
[ "$CODE" = 200 ] && [[ "$(hdr content-type)" == text/html* ]] && grep -q 'id="root"' "$WORK/body" \
  || die 2 "/ -> $CODE $(hdr content-type)"
[ "$(hdr cache-control)" = no-cache ] || die 2 "/ Cache-Control=$(hdr cache-control), expected no-cache"
ASSET="$(grep -o '/assets/[^"]*\.js' "$WORK/body" | head -1)"
[ -n "$ASSET" ] || die 2 "index.html does not reference an /assets/*.js bundle"
req "$UI" GET /mcp-servers/abc
[ "$CODE" = 200 ] && grep -q 'id="root"' "$WORK/body" && [ "$(hdr cache-control)" = no-cache ] \
  || die 2 "deep link /mcp-servers/abc -> $CODE (expected the index.html fallback)"
req "$UI" GET "$ASSET"
[ "$CODE" = 200 ] && [[ "$(hdr cache-control)" == *immutable* ]] && [[ "$(hdr content-type)" == application/javascript* ]] \
  || die 2 "$ASSET -> $CODE Cache-Control=$(hdr cache-control) Content-Type=$(hdr content-type)"
for p in /assets/e2e-missing-0000.js /assets/; do
  req "$UI" GET "$p"
  [ "$CODE" = 404 ] || die 2 "$p -> $CODE (expected 404, not the index.html fallback)"
  [[ "$(hdr cache-control)" != *immutable* ]] || die 2 "$p 404 carries Cache-Control $(hdr cache-control)"
done
req "$UI" POST /
[ "$CODE" = 405 ] || die 2 "POST / -> $CODE (expected 405)"
req "$UI" HEAD / -I
[ "$CODE" = 200 ] || die 2 "HEAD / -> $CODE"
req "$UI" GET /assets/../index.html --path-as-is
[ "$CODE" = 200 ] && [ "$(hdr cache-control)" = no-cache ] || die 2 "/assets/../index.html -> $CODE Cache-Control=$(hdr cache-control)"
req "$UI" GET /%2e%2e/%2e%2e/etc/passwd --path-as-is
[ "$CODE" = 400 ] || die 2 "/%2e%2e/%2e%2e/etc/passwd -> $CODE (expected 400)"
pass 2 "index.html and deep links no-cache; $ASSET immutable javascript; missing asset and /assets/ 404; POST 405; HEAD 200; dot segments normalized; escaping path 400"

# ---------- 3. 보안 헤더 ----------
STEP=3
req "$UI" GET /; has_fixed "GET / 200"; UI_CSP="$(hdr content-security-policy)"
server="$(hdr server)"
[[ "$server" != *[0-9]* ]] || die 3 "Server header carries a version: $server"
req "$UI" GET /assets/e2e-missing-0000.js; has_fixed "missing asset 404"
req "$UI" POST /; has_fixed "POST / 405"
req "$UI" GET /%2e%2e/%2e%2e/etc/passwd --path-as-is; has_fixed "escaping path 400"
req "$BASE" GET /api/version
API_CSP="$(hdr content-security-policy)"
[ -n "$UI_CSP" ] && [ "$UI_CSP" = "$API_CSP" ] || die 3 "CSP differs. daegam-ui: $UI_CSP | platform: $API_CSP"
req "$UI" GET /
[ -z "$(hdr strict-transport-security)" ] || die 3 "HSTS on plain http: $(hdr strict-transport-security)"
for xfp in https 'HTTPS, http' ' https '; do
  req "$UI" GET / -H "X-Forwarded-Proto: $xfp"
  [ "$(hdr strict-transport-security)" = "max-age=15552000; includeSubDomains" ] \
    || die 3 "X-Forwarded-Proto '$xfp' -> HSTS '$(hdr strict-transport-security)'"
done
req "$UI" GET / -H 'X-Forwarded-Proto: http, https'
[ -z "$(hdr strict-transport-security)" ] || die 3 "HSTS with X-Forwarded-Proto 'http, https' (only the first value counts)"
pass 3 "fixed headers on 200·404·405·400; daegam-ui CSP equals the platform CSP; HSTS only when the first X-Forwarded-Proto is https; no Server version"

# =====================================================================================================
# Traefik 갈래. 브라우저가 밟는 길(로컬 호스트)로 본다. curl --resolve 로 Traefik 의 LoadBalancer IP 에 못 박으므로
# /etc/hosts 는 필요 없다.
# =====================================================================================================
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
HOST="${E2E_UI_HOST:-$(local_host)}"
"${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o name >/dev/null 2>&1 \
  || die 4 "no Service $TRAEFIK_NS/$TRAEFIK_SVC (is Traefik installed? k3s 는 TRAEFIK_NS=kube-system)"
LB_IP="$("${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
EDGE="http://$HOST"
EXPECT_API="${E2E_EXPECT_API_BUILD:-0.1.0+$(sh "$ROOT/scripts/build-stamp.sh" platform)}"
# tr_req 는 Traefik 을 지나는 요청이다. 코드·헤더·본문은 req 와 같은 자리에 남는다
if [ -n "$LB_IP" ]; then
  tr_req() { req "$EDGE" "$@" --resolve "$HOST:80:$LB_IP"; }
elif [ "$CTX" != orbstack ]; then
  # LoadBalancer 가 영원히 pending 인 클러스터다. Service 의 80 으로 붙고 Host 헤더로 호스트 규칙을 맞춘다
  start_pf "$TRAEFIK_NS" "svc/$TRAEFIK_SVC" "${TR_PORT}:80"; PF_TR=$PF_PID
  wait_port "$TR_PORT" || die 4 "port-forward svc/$TRAEFIK_SVC :$TR_PORT did not open"
  tr_req() { req "http://127.0.0.1:$TR_PORT" "$@" -H "Host: $HOST"; }
  echo ">> Traefik has no LoadBalancer IP; port-forward svc/$TRAEFIK_SVC :$TR_PORT with Host: $HOST"
else
  die 4 "Service $TRAEFIK_NS/$TRAEFIK_SVC has no LoadBalancer IP on orbstack"
fi
# 조건이 안 맞아 돌지 않는 STEP. orbstack 밖에서는 설계상 돌지 않는 것이라 세지 않는다
skip_here() { if [ "$CTX" = orbstack ]; then skip "$1" "$2"; else echo "STEP $1 SKIP (conditional): $2"; fi; }
# edge_ui_build WANT  Traefik 너머 /build.json 의 ui_build 가 WANT 가 될 때까지 10초 기다린다. 파드가 바뀐 뒤 Traefik 이
# 엔드포인트를 다시 읽는 틈을 넘기려는 것이다
edge_ui_build() {
  for _ in $(seq 1 10); do
    tr_req GET /build.json
    [ "$CODE" = 200 ] && [ "$(jq_ "d.get('ui_build')" || true)" = "$1" ] && return 0
    sleep 1
  done
  return 1
}

# ---------- 5. 라우팅 ----------
STEP=4
backends="$("${K[@]}" -n "$PLATFORM_NS" get ingress daegam-platform -o jsonpath='{range .spec.rules[0].http.paths[*]}{.path}={.backend.service.name} {end}')"
[[ "$backends" == *"/=daegam-ui"* ]] || die 4 "Ingress daegam-platform does not route / to daegam-ui: $backends"
tr_req GET /api/version
[ "$CODE" = 200 ] && [[ "$(hdr content-type)" == application/json* ]] || die 4 "/api/version -> $CODE $(hdr content-type)"
got="$(jq_ "d.get('api_build')" || true)"
[ "$got" = "$EXPECT_API" ] || die 4 "/api/version api_build=$got, expected $EXPECT_API (make image-platform, then make deploy-local)"
# sticky 쿠키는 UI Service 에만 건다. 플랫폼 경로는 파드를 가리지 않는다
[ -z "$(hdr set-cookie)" ] || die 4 "/api/version sets a cookie: $(hdr set-cookie)"
tr_req GET /build.json
[ "$(jq_ "d.get('ui_build')")" = "$EXPECT_UI" ] || die 4 "/build.json through Traefik is not $EXPECT_UI"
tr_req GET /healthz
[ "$CODE" = 200 ] && [ "$(jq_ "d.get('status')")" = ok ] || die 4 "/healthz -> $CODE (expected the platform probe JSON, not the UI shell)"
tr_req GET /.well-known/openid-configuration
[ "$CODE" = 200 ] && [ -n "$(jq_ "d.get('issuer') or ''")" ] || die 4 "/.well-known/openid-configuration -> $CODE (expected the platform discovery)"
tr_req GET /api/nope
[ "$CODE" = 404 ] && [ "$(jq_ "d['detail']['code']")" = not_found ] || die 4 "/api/nope -> $CODE (expected the platform JSON 404)"
# Traefik 의 Prefix 는 글자 단위라 Ingress 경로를 /api/ 로 적었다. /apiary 같은 UI 경로는 플랫폼이 먹지 않는다
tr_req GET /apiary
[ "$CODE" = 200 ] && grep -q 'id="root"' "$WORK/body" || die 4 "/apiary -> $CODE (expected the UI shell, the /api/ prefix is too wide)"
# 쿠키 없는 요청에 Traefik 이 UI 파드를 고르며 sticky 쿠키를 준다(D52). Secure 는 공개 주소가 https 일 때만이다. http 인
# 벌에 붙으면 브라우저가 쿠키를 돌려보내지 않아 붙지 않는다
tr_req GET /
cookie="$(hdr set-cookie)"
[[ "$cookie" == daegam_ui=* && "$cookie" == *"; HttpOnly"* && "$cookie" == *"; SameSite=Lax"* ]] \
  || die 4 "/ Set-Cookie '$cookie' (expected the daegam_ui sticky cookie with HttpOnly and SameSite=Lax)"
public="$(platform_config DAEGAM_PUBLIC_URL)" || die 4 "could not read DAEGAM_PUBLIC_URL from ConfigMap $AUTH_MODE_CONFIGMAP"
if [[ "$public" == https://* ]]; then
  [[ "$cookie" == *"; Secure"* ]] || die 4 "public URL $public is https but the sticky cookie is not Secure: $cookie"
else
  [[ "$cookie" != *"; Secure"* ]] || die 4 "public URL $public is not https but the sticky cookie is Secure: $cookie"
fi
pass 4 "Traefik routes /api/·/.well-known/·/healthz to the platform ($EXPECT_API) and the rest to daegam-ui ($EXPECT_UI); / sets the sticky cookie ${cookie%%=*} for $public, the API sets none"

# ---------- 6. 화면과 API 의 CSP ----------
STEP=5
tr_req GET /
[ "$CODE" = 200 ] && grep -q 'id="root"' "$WORK/body" || die 5 "/ through Traefik -> $CODE"
edge_ui_csp="$(hdr content-security-policy)"
tr_req GET /api/version
[ -n "$edge_ui_csp" ] && [ "$edge_ui_csp" = "$(hdr content-security-policy)" ] || die 5 "CSP differs through Traefik. ui: $edge_ui_csp | api: $(hdr content-security-policy)"
pass 5 "the page and the API carry the same CSP through Traefik"

# ---------- 7. UI 만 바꾼 배포는 플랫폼을 건드리지 않는다 ----------
STEP=6
platform_pods() {
  "${K[@]}" -n "$PLATFORM_NS" get pod -l app=daegam-platform \
    -o jsonpath='{range .items[*]}{.metadata.uid}={.status.containerStatuses[0].restartCount}{"\n"}{end}' | sort
}
if [ "$CTX" != orbstack ]; then
  # 다른 컨텍스트의 노드는 로컬 도커의 새 dev-<id> 태그를 당기지 못해 maxUnavailable 0 아래에서 롤아웃이 멈춘다
  skip_here 6 "context $CTX is not orbstack: kubelet cannot see local images, the UI-only deploy procedure runs only on orbstack"
else
  ORIG_IMAGE="$("${K[@]}" -n "$PLATFORM_NS" get deploy daegam-ui -o jsonpath='{.spec.template.spec.containers[0].image}')" \
    || die 6 "could not read the deployed daegam-ui image"
  ORIG_TAG="${ORIG_IMAGE##*:}"
  ORIG_LOCAL_ID="$(docker image inspect --format '{{.Id}}' daegam-ui:0.1.0 2>/dev/null || true)"
  tr_req GET /
  ORIG_ENTRY="$(grep -o '/assets/[^"]*\.js' "$WORK/body" | head -1 || true)"
  VARIANT_STAMP="e2e-$(date +%s)"
  echo ">> building a UI image stamped $VARIANT_STAMP (current $ORIG_IMAGE)"
  # 굽기가 로컬 daegam-ui:0.1.0 을 옮기므로 그 전에 되돌리기를 켠다
  UI_VARIANT=1
  make -C "$ROOT" --no-print-directory image-ui UI_STAMP="$VARIANT_STAMP" > "$WORK/build.log" 2>&1 \
    || { tail -20 "$WORK/build.log"; die 6 "make image-ui UI_STAMP=$VARIANT_STAMP failed"; }
  VARIANT_REF="daegam-ui:dev-$(docker image inspect --format '{{.Id}}' daegam-ui:0.1.0 | sed 's/^sha256://' | cut -c1-12)" \
    || die 6 "could not read the image make image-ui just built"
  BEFORE_PLATFORM="$(platform_pods)" || die 6 "could not list the platform pods"
  tr_req GET /api/version
  BEFORE_API="$(jq_ "d.get('api_build')")" || die 6 "/api/version -> $CODE before the deploy"
  : > "$WORK/codes"; : > "$WORK/entry_codes"; : > "$WORK/kept_entry_codes"; rm -f "$WORK/jar.kept"
  # open_tab JAR ENTRY_CODES  새로 연 탭 하나다. / 를 받고 index.html 이 가리키는 진입 번들을 부른다. 두 요청 모두 JAR 의
  # 쿠키를 보내고 받은 쿠키를 JAR 에 둔다. / 의 코드는 $WORK/codes 에, 번들의 코드는 ENTRY_CODES 에 적는다. 연결이
  # 실패해도 -w 가 000 을 적는다
  open_tab() {
    local entry
    rm -f "$WORK/tab.html"
    curl -s -o "$WORK/tab.html" -w '%{http_code}\n' -b "$1" -c "$1" --resolve "$HOST:80:$LB_IP" --max-time 5 "$EDGE/" \
      >> "$WORK/codes" 2>/dev/null || true
    entry="$(grep -o '/assets/[^"]*\.js' "$WORK/tab.html" 2>/dev/null | head -1 || true)"
    [ -z "$entry" ] || curl -s -o /dev/null -w '%{http_code}\n' -b "$1" -c "$1" --resolve "$HOST:80:$LB_IP" --max-time 5 \
      "$EDGE$entry" >> "$2" 2>/dev/null || true
  }
  # 롤링 중에는 두 빌드의 파드가 함께 돈다. 쿠키 없이 오는 새 브라우저의 탭과 쿠키를 들고 계속 오는 브라우저의 탭을
  # 번갈아 연다(D52)
  ( while :; do
      rm -f "$WORK/jar.new"
      open_tab "$WORK/jar.new" "$WORK/entry_codes"
      open_tab "$WORK/jar.kept" "$WORK/kept_entry_codes"
      sleep 0.2
    done ) 8>&- 9>&- &
  LOOP_PID=$!
  make -C "$ROOT" --no-print-directory deploy-ui-local > "$WORK/deploy.log" 2>&1 \
    || { tail -20 "$WORK/deploy.log"; die 6 "make deploy-ui-local failed"; }
  NEW_IMAGE="$("${K[@]}" -n "$PLATFORM_NS" get deploy daegam-ui -o jsonpath='{.spec.template.spec.containers[0].image}')" \
    || die 6 "could not read the deployed daegam-ui image"
  # 차트는 레지스트리 접두(values-local.yaml 의 docker.io/library)를 붙여 렌더하므로 마지막 마디를 맞댄다
  [ "${NEW_IMAGE##*/}" = "$VARIANT_REF" ] || die 6 "deploy-ui-local deployed $NEW_IMAGE, expected $VARIANT_REF"
  # 옛 파드가 preStop 을 마치고 지워질 때까지 부른다. 쿠키로 붙은 브라우저가 다른 파드로 옮기는 것이 그 안에 있다
  ui_pods_on "$NEW_IMAGE" || die 6 "old daegam-ui pods were still around 60s after deploy-ui-local"
  sleep 1
  kill "$LOOP_PID" 2>/dev/null || true; wait "$LOOP_PID" 2>/dev/null || true; LOOP_PID=""
  edge_ui_build "0.1.0+$VARIANT_STAMP" || die 6 "/build.json is not the new stamp after deploy-ui-local"
  [ "$(platform_pods)" = "$BEFORE_PLATFORM" ] || die 6 "platform pods changed during a UI-only deploy: before [$BEFORE_PLATFORM] after [$(platform_pods)]"
  tr_req GET /api/version
  [ "$(jq_ "d.get('api_build')")" = "$BEFORE_API" ] || die 6 "api_build changed during a UI-only deploy"
  total="$(wc -l < "$WORK/codes" | tr -d ' ')"
  bad="$(grep -vc '^200$' "$WORK/codes" || true)"
  [ "$total" -ge 10 ] || die 6 "only $total requests during the deploy (the probe loop did not run)"
  [ "$bad" = 0 ] || die 6 "$bad of $total requests to / during the UI-only deploy were not 200: $(grep -v '^200$' "$WORK/codes" | sort | uniq -c | tr '\n' ' ')"
  tr_req GET /
  new_entry="$(grep -o '/assets/[^"]*\.js' "$WORK/body" | head -1 || true)"
  # 진입 번들 이름이 두 빌드에서 같으면 어느 파드든 그 번들을 가져 아래 수가 롤링 창을 보지 못한다
  [ -n "$new_entry" ] && [ "$new_entry" != "$ORIG_ENTRY" ] \
    || die 6 "the entry bundle did not change between the builds ($ORIG_ENTRY -> $new_entry), so the rolling window is not observable"
  # 새 브라우저의 탭은 sticky 쿠키로 index.html 과 진입 번들을 같은 파드에서 받는다(D52)
  entries="$(wc -l < "$WORK/entry_codes" | tr -d ' ')"
  entry_bad="$(grep -vc '^200$' "$WORK/entry_codes" || true)"
  [ "$entries" -ge 5 ] || die 6 "only $entries fresh tabs reached the entry bundle during the deploy"
  [ "$entry_bad" = 0 ] || die 6 "$entry_bad of $entries fresh tabs did not get their entry bundle during the UI-only deploy: $(grep -v '^200$' "$WORK/entry_codes" | sort | uniq -c | tr '\n' ' ')(sticky cookie on Service daegam-ui, D52)"
  # 쿠키를 든 브라우저의 탭은 붙은 파드가 Traefik 에서 빠지는 순간 두 요청 사이에 걸리면 번들이 다른 빌드의 파드로 간다.
  # 세지 않고 잰다
  kept="$(wc -l < "$WORK/kept_entry_codes" | tr -d ' ')"
  kept_bad="$(grep -vc '^200$' "$WORK/kept_entry_codes" || true)"
  restore_ui
  edge_ui_build "$EXPECT_UI" || die 6 "restoring $ORIG_IMAGE did not bring $EXPECT_UI back"
  pass 6 "make image-ui deploy-ui-local served 0.1.0+$VARIANT_STAMP with platform pods and api_build untouched; / answered 200 to all $total requests; fresh tabs got the entry bundle ($ORIG_ENTRY -> $new_entry) in all $entries; tabs of a browser holding the cookie missed it in $kept_bad of $kept (measured, not counted); restored $ORIG_IMAGE"
fi

# ---------- 8. 인증 모드 전환 ----------
STEP=7
if ! mock_oidc_deployed; then
  skip_here 7 "mock-oidc is not deployed in $PLATFORM_NS (kubectl apply -f deploy/dev/mock-oidc.yaml)"
  finish
fi
if [ "$(platform_config DAEGAM_AUTH_MODE)" != header ]; then
  skip_here 7 "the deployment is not in header mode (ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE); STEP 7 switches header, oidc, header"
  finish
fi
IDP="$(platform_config DAEGAM_OIDC_ISSUER | sed -E 's|^([A-Za-z]+://[^/?#]+).*|\1|')"
switch_auth_mode oidc || die 7 "switching ConfigMap $AUTH_MODE_CONFIGMAP to oidc and rolling the control plane failed"
tr_req GET /
csp="$(hdr content-security-policy)"
[[ "$csp" == *"connect-src 'self' $IDP;"* && "$csp" == *"frame-src 'self' $IDP;"* ]] || die 7 "oidc mode: UI CSP does not open $IDP: $csp"
[[ "$csp" == *"frame-ancestors 'none';"* && "$(hdr x-frame-options)" = DENY ]] || die 7 "oidc mode: / may be framed: $csp"
# silent renew iframe 이 여는 /callback 만 같은 origin 의 프레임을 허락한다 (T13)
tr_req GET '/callback?code=x&state=y'
[[ "$(hdr content-security-policy)" == *"frame-ancestors 'self';"* && "$(hdr x-frame-options)" = SAMEORIGIN ]] \
  || die 7 "oidc mode: /callback does not allow same-origin frames: $(hdr content-security-policy)"
tr_req GET /callback/
[ "$(hdr x-frame-options)" = DENY ] || die 7 "oidc mode: /callback/ may be framed"
tr_req GET /api/clusters
[ "$CODE" = 401 ] || die 7 "oidc mode: GET /api/clusters without a token -> $CODE (expected 401: did the platform roll?)"
[ "$(hdr content-security-policy)" = "$csp" ] || die 7 "oidc mode: the platform CSP differs from the UI CSP"
# header 로 되돌리고 플랫폼이 소비자를 다시 적용할 때까지 기다린다. 다음 e2e 가 굴러가는 소비자와 겹치지 않게 한다
restore_auth_mode
tr_req GET /
[[ "$(hdr content-security-policy)" == *"frame-src 'none';"* ]] || die 7 "after restore: UI CSP still opens the IdP"
tr_req GET /api/clusters
[ "$CODE" = 200 ] || die 7 "after restore: GET /api/clusters -> $CODE (expected 200 in header mode)"
pass 7 "oidc: UI and platform CSP both open $IDP and the platform answers 401 without a token; back to header, the IdP is gone again"

finish
