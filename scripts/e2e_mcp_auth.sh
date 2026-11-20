#!/usr/bin/env bash
# P6 F4a/F4b + P8 F2·F7 클러스터 게이트 (oidc 모드). 전제: make deploy-local,
# mock-oidc 파드(daegam-system), P1 spike(mock-soap, daegam-spike), toolhive 오퍼레이터,
# Traefik(IngressClass traefik, LoadBalancer Service traefik/traefik. STEP 7 이 IP 를 읽어 curl --resolve 로 붙는다).
# 모드 전환은 STEP 1 이 scripts/lib/e2e_auth_mode.sh 로 한다. ConfigMap daegam-platform-config 의 DAEGAM_AUTH_MODE 를
# oidc 로, DAEGAM_DATA_PLANE_ISSUER 를 idp 로 바꾸고 플랫폼과 UI 를 굴리며(UI 의 CSP 도 모드를 따른다, D52),
# 플랫폼이 데이터 플레인 인증을 다시 적용할 때까지 기다린다(P32). EXIT trap 이 앞의 모드로 되돌리고 다시
# 기다린다.
#
#   STEP 1  oidc 로 전환 + 부트스트랩 admin 토큰으로 플랫폼 도달 + 재적용 대기 + 테넌트 설정(mcp_group·agent_client_ids)
#   STEP 2  calculator 백엔드 → MCP Server[Add] 저장 → 배포 → Ready → 수동 게시(POST /mcp-servers/{id}/publish)로
#           허브 internal 항목. 정리에서 문서 삭제가 항목을 함께 지우는 것을 본다
#   STEP 3  Gateway 저장 → 배포 → vMCP CR 이 oidc 로 렌더 + MCPOIDCConfig 존재 (응답 auth == oidc).
#           Entry 마다의 토큰이 테넌트 audience 와 그 MCP Server 의 audience 를 싣는다 (P50 9절)
#   STEP 4  vMCP: 무토큰 401(WWW-Authenticate) / 타 그룹 토큰 200+빈 목록+call denied / 테넌트 그룹 성공
#   STEP 5  client credentials 토큰(cid=agent-dev)으로 tools/call Add(3,4) -> 7 (cid 정책 통과)
#   STEP 6  agent_client_ids 에서 agent-dev 제거 + redeploy -> 같은 토큰의 tools/call 이 인가 거부
#   STEP 7  MCP Server 를 복제해 http://calc-auth.e2e.local/mcp 로 노출 배포 → 앞단은 그 서버 하나를 백엔드로 둔 vMCP
#           front-<k8s.name> 이고(P54 5절) incomingAuth oidc{daegam-oidc, daegam-mcp, resourceUrl=external_url}와 Cedar
#           참조로 Ready, Ingress 는 vmcp-front-<k8s.name>:4483 을 가리킨다. external_url 무토큰 401(WWW-Authenticate),
#           테넌트 그룹 토큰 tools/call Add(3,4) -> 7, 두 꼴의 RFC 9728 메타데이터는 200 이고 Ingress 밖 경로(/health)는
#           404, 앞단의 Entry 는 headerInjection 으로 Secret svc-expose-<k8s.name> 의 토큰을 보내고 그 토큰은 그 MCP Server
#           의 audience 를 함께 싣는다(P50 9절), 내부 프록시 경로(mcp-<k8s.name>-proxy:8080)는 무토큰과 사람 토큰을
#           401 로, 그 서버의 audience 를 실은 서비스 토큰을 200 으로 받는다.
#           턴 위임 토큰(P53 3.2절): oidc 의 앞단 정책에는 묶이지 않은 위임 토큰을 막는 forbid 가 없다(local 만 렌더한다).
#           그래도 위임 토큰의 모양(그룹 없이 act·bound_to·tenant)인 토큰은 그룹과 cid 로만 받는 바탕 정책이 거절하고,
#           act 를 실은 IdP 토큰은 테넌트 그룹이 있으면 전처럼 Add=7 이다
#   STEP 7b 새로 만든 MCP Server 는 처음부터 자기 audience(daegam-internal:<tenant>:<id>)로 검증한다(P50 9절, G9 2단계).
#           CR 의 oidcConfigRef.audience 가 그것이고, 테넌트 audience 하나인 서비스 토큰은 내부 프록시에서 401 이다.
#           1단계 렌더에서는 그 토큰이 200 이라 이 단계가 실패한다
#   STEP 7c ToolHive 0.52 이상이면 내부 MCPServer 는 테넌트 울타리(MCPAuthzConfig <CR>-authz)를 참조한다(P50 11절).
#           그 서버의 audience 를 실었어도 sub 가 svc: 가 아닌 토큰과 다른 테넌트의 클레임을 실은 토큰은 빈 목록과 거부를
#           받는다. STEP 4 의 vMCP tools/list 가 울타리를 건 서버 뒤에서 도는 것이 Modern 응답이 살아 있다는 확인이다.
#           플랫폼이 그 클러스터를 울타리 게이트 밖으로 보면(상태 API 의 fallbacks 에 tenant_fence) 조건부로 건너뛴다
#   STEP 7r Tool 규칙이 앞단을 지난다(P52 2절). 노출한 복제본의 Add 에 그룹 e2e-rules 만 부를 수 있다는 규칙을 둔 리비전을
#           재배포하면 앞단이 먼저 적용되고, 상태의 rules_front_rolling 이 앞단의 파드가 다 바뀐 뒤 사라진다.
#           그 뒤 앞단에서 테넌트 그룹과 e2e-rules 를 실은 IdP 토큰은 Add=7, 테넌트 그룹만 실은 토큰은 빈 목록과 403 이다.
#           내부 프록시의 서버는 그룹 절을 짓지 않으므로 그 서버의 audience 를 실은 서비스 토큰도 Add 를 보지 못한다
#   STEP 8  oidc 모드에서 게시(entry) 항목을 백엔드로 넣으면 422 published_oidc_unsupported
# 이 게이트는 vMCP 를 직접 부른다. Gateway 를 도구 소스로 쓰는 Agent 는 e2e_agent STEP 5g(header·local 모드)가 본다.
# macOS 에는 timeout 이 없으므로 전부 폴링 루프다.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
TENANT="${E2E_TENANT:-e2e-mcp}"
MCP_GROUP="mcp-${TENANT}"
API_PORT="${E2E_API_PORT:-$PORT_MCP_AUTH_API}"
OIDC_PORT="${E2E_OIDC_PORT:-$PORT_MCP_AUTH_OIDC}"
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_MCP_AUTH_VMCP}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
BASE="http://127.0.0.1:${API_PORT}"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
OIDC="http://127.0.0.1:${OIDC_PORT}"
# 정의와 배포가 한 문서다. MCP Server 하나, Gateway 하나가 각각 저장되고 배포된다 (D42)
BP_NAME="calc-auth"; MS_NAME="calc-auth"; GW_NAME="gw-auth"
AGENT_CLIENT_ID="agent-dev"; AGENT_CLIENT_SECRET="agent-dev-secret"
# STEP 7 노출. Traefik 의 LoadBalancer IP 로 host 를 못 박는다 (DNS 등록 없음)
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
EXPOSE_NAME="calc-auth-exposed"; EXPOSE_HOST="${E2E_MCP_HOST:-calc-auth.e2e.local}"
# STEP 7r 의 규칙이 부르게 하는 IdP 그룹 (P52 1절)
RULE_GROUP="e2e-rules"
PROXY_PORT="${E2E_PROXY_PORT:-$PORT_MCP_AUTH_PROXY}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-mcp-auth.XXXXXX")"
K=(kubectl --context "$CTX")
PFS=(); PFS_PORTS=()
AUTH=()   # 관리자 Bearer 헤더. STEP 1 이 부트스트랩 관리자 토큰을 넣는다 (lib/e2e_api.sh 의 api 가 싣는다)

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
# 이 게이트는 0.5초 간격 60회로 포트를 본다 (합계 30초). lib/e2e_pf.sh 의 wait_port 가 읽는다
# shellcheck disable=SC2034
PF_WAIT_TRIES=60
# shellcheck disable=SC2034
PF_WAIT_SLEEP=0.5

# roll_control_plane 과 switch_auth_mode·restore_auth_mode·wait_auth_reapplied·user_token(mock-oidc 토큰)은 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_auth_mode.sh"
# STEP 2·3 의 calculator 준비(calc_mcp_server·calc_publish·calc_gateway)는 여기에 있다. e2e_pat 과 함께 쓴다
# (AUTH·api·wait_ready·die 를 쓴다)
. "$HERE/lib/e2e_calc.sh"

cleanup() {
  # 되돌린 뒤 재적용을 기다리는 호출이 API port-forward 로 가므로 그것을 닫기 전에 되돌린다
  restore_auth_mode "${ADMIN_TOKEN:-}"
  for i in "${!PFS[@]}"; do stop_pf "${PFS[$i]}" "${PFS_PORTS[$i]}"; done
  rm -rf "$WORK"
}
on_exit cleanup

# pass·die·finish 는 여기에 있다. 이 게이트는 실패를 세지 않고 첫 실패에서 끝낸다(die). 종료 코드 계약도 거기 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·listed_id·hdr·api_delete·wait_ready·clone_fresh 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
pf() { start_pf "$1" "$2" "$3"; PFS+=("$PF_PID"); PFS_PORTS+=("${3%%:*}"); wait_port "${3%%:*}" || die 0 "port-forward $2 :$3 did not open"; }

# authz_policies KIND NS NAME: 그 CR 의 Cedar 정책을 한 줄에 하나씩 찍는다. vMCP 는 incomingAuth 아래, MCPServer 는
# spec 바로 아래에 있다. 참조(authzConfigRef)이면 그 MCPAuthzConfig 를 읽고(P50 8·11절, G8 게이트를 지난 클러스터),
# 아니면 인라인 정책이다. 정책이 없으면 아무것도 찍지 않는다
authz_policies() {
  local ref at='((d["spec"].get("incomingAuth") if d["kind"] == "VirtualMCPServer" else d["spec"]) or {})'
  "${K[@]}" -n "$2" get "$1" "$3" -o json >"$WORK/authz-cr.json"
  ref="$(json_of "$WORK/authz-cr.json" "($at.get('authzConfigRef') or {}).get('name') or ''")"
  if [ -n "$ref" ]; then
    "${K[@]}" -n "$2" get mcpauthzconfig "$ref" -o json >"$WORK/authz.json"
    json_of "$WORK/authz.json" '"\n".join(d["spec"]["config"]["policies"])'
  else
    json_of "$WORK/authz-cr.json" "'\\n'.join(($at.get('authzConfig') or {}).get('inline', {}).get('policies', []))"
  fi
}
# INIT_PARAMS·mcp_status·rpc_curl·rpc_extract·rpc_·svc_token·mcp_ingress 와 lab 클라이언트로 부르는 mcp·mcp_json·
# mcp_admits·mcp_refuses 는 여기에 있다 (WORK 를 쓴다)
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-mcp-auth"
. "$HERE/lib/mcp_rpc.sh"

echo ">> context=$CTX tenant=$TENANT mcp_group=$MCP_GROUP api=$API oidc=$OIDC"

# ---------- 1. 플랫폼(oidc 모드) + 테넌트 설정 ----------
# 이 게이트는 데이터 플레인이 IdP 를 믿는 oidc 를 보므로 발급자를 idp 로 못 박는다. 플랫폼을 믿는 oidc 는
# make e2e-oidc-platform 이 본다 (P53b 6.2절)
switch_auth_mode oidc DAEGAM_DATA_PLANE_ISSUER=idp \
  || die 1 "switching ConfigMap $AUTH_MODE_CONFIGMAP to oidc and rolling the control plane failed"
pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
pf "$PLATFORM_NS" svc/mock-oidc "${OIDC_PORT}:8080"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/backend-apis")"
[ "$code" = "401" ] || die 1 "expected 401 without token, got $code (auth_mode=oidc deployed?)"
ADMIN_TOKEN="$(user_token '{"sub": "boot-admin", "groups": ["platform-admins"], "aud": "daegam-api"}')"
AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
code="$(api GET /me)"
[ "$code" = "200" ] || die 1 "GET /me -> $code"
[ "$(jq_ 'd["is_platform_admin"]')" = "True" ] || die 1 "bootstrap admin is not platform-admin"
# 모드를 바꾸면 플랫폼이 MCPOIDCConfig 와 살아 있는 소비자를 다시 적용한다. 다 될 때까지 기다린다 (P32)
wait_auth_reapplied "$ADMIN_TOKEN" || die 1 "data-plane auth was not re-applied after switching to oidc"
body='{"mcp_group": "'"$MCP_GROUP"'", "agent_client_ids": ["'"$AGENT_CLIENT_ID"'"]}'
code="$(api PATCH "/tenants/$TENANT/settings" "$body")"
[ "$code" = "200" ] || die 1 "PATCH tenant settings -> $code"
pass 1 "oidc platform reachable (was ${AUTH_MODE_PREV:-unset}), data-plane auth re-applied; tenant $TENANT mcp_group=$MCP_GROUP agent_client_ids=[$AGENT_CLIENT_ID]"

# ---------- 2. backend -> mcp server 저장·배포 Ready -> hub internal ----------
calc_mcp_server 2 "$BP_NAME" "$MS_NAME" '{}'
calc_publish 2
MS_ID="$CALC_MS_ID"; NS="$CALC_NS"; HUB_ID="$CALC_HUB_ID"
pass 2 "mcp server $MS_NAME 저장·배포 Ready in ns=$NS, published by hand -> hub item=$HUB_ID ($CALC_PUBLISH_CODE)"

# ---------- 3. Gateway 저장 + 배포 -> vMCP oidc 렌더 ----------
calc_gateway 3 "$GW_NAME" '{}'
GW_ID="$CALC_GW_ID"; VMCP_NAME="$CALC_GW_K8S_NAME"
# auth 는 플랫폼 auth_mode 가 정하는 읽기 전용 값이다
[ "$(jq_ 'd["auth"]')" = "oidc" ] || die 3 "gateway auth != oidc: $(jq_ 'd["auth"]')"
"${K[@]}" -n "$NS" get virtualmcpserver "$VMCP_NAME" -o json >"$WORK/vmcp.json"
IN_TYPE="$(json_of "$WORK/vmcp.json" 'd["spec"]["incomingAuth"]["type"]')"
IN_REF="$(json_of "$WORK/vmcp.json" 'd["spec"]["incomingAuth"]["oidcConfigRef"]["name"], d["spec"]["incomingAuth"]["oidcConfigRef"]["audience"]')"
POLICIES="$(authz_policies virtualmcpserver "$NS" "$VMCP_NAME")"
[ "$IN_TYPE" = "oidc" ] || die 3 "vMCP incomingAuth.type=$IN_TYPE"
[ "$IN_REF" = "daegam-oidc daegam-mcp" ] || die 3 "oidcConfigRef=$IN_REF"
echo "$POLICIES" | grep -qF "THVGroup::\"$MCP_GROUP\"" || die 3 "group policy missing: $POLICIES"
echo "$POLICIES" | grep -qF "claim_cid == \"$AGENT_CLIENT_ID\"" || die 3 "cid policy missing: $POLICIES"
"${K[@]}" -n "$NS" get mcpoidcconfig daegam-oidc -o name >/dev/null || die 3 "MCPOIDCConfig daegam-oidc missing in $NS"
# 서버마다의 audience (P50 9절): Entry 는 자기 토큰 객체를 가리키고 그 토큰은 테넌트 audience 와 MCP Server $MS_ID
# 의 audience 를 함께 싣는다
code="$(api GET "/gateways/$GW_ID")"
[ "$code" = "200" ] || die 3 "GET /gateways/$GW_ID -> $code"
ENTRY="$(jq_ 'next(b["workload"] for b in d["deployed_backends"] if b["kind"] == "internal")')"
ENTRY_SVC="$("${K[@]}" -n "$NS" get mcpserverentry "$ENTRY" -o jsonpath='{.spec.externalAuthConfigRef.name}')"
[ -n "$ENTRY_SVC" ] && [ "$ENTRY_SVC" != "svc-$VMCP_NAME" ] || die 3 "Entry $ENTRY token config: '$ENTRY_SVC'"
ENTRY_AUD="$(token_aud "$NS" "$ENTRY_SVC")" || die 3 "Secret $ENTRY_SVC has no readable token"
[ "$ENTRY_AUD" = "[\"daegam-internal:$TENANT\", \"daegam-internal:$TENANT:$MS_ID\"]" ] || die 3 "Entry $ENTRY token aud: $ENTRY_AUD"
pass 3 "vMCP $VMCP_NAME rendered oidc (ref=daegam-oidc/daegam-mcp), policies=$POLICIES; Entry $ENTRY token $ENTRY_SVC aud=$ENTRY_AUD"

# ---------- 4. vMCP 게이트: 401 / 빈 목록+denied / 성공 ----------
pf "$NS" "svc/vmcp-$VMCP_NAME" "${VMCP_PORT}:4483"
VMCP_URL="http://127.0.0.1:${VMCP_PORT}/mcp"
code="$(mcp_status "$VMCP_URL" "" "$WORK/headers")"
[ "$code" = "401" ] || die 4 "no-token initialize -> $code (expected 401)"
[ -n "$(hdr www-authenticate "$WORK/headers")" ] || die 4 "401 without WWW-Authenticate"
OTHER_TOKEN="$(user_token '{"sub": "intruder", "groups": ["mcp-other-tenant"]}')"
mcp_refuses 4 "$OTHER_TOKEN" "other-group token" "$VMCP_URL"
grep -qi 'denied\|authorization' "$WORK/mcp.json" "$WORK/mcp.err" || die 4 "call failure without denial message"
GROUP_TOKEN="$(user_token '{"sub": "member", "groups": ["'"$MCP_GROUP"'"]}')"
mcp_admits 4 "$GROUP_TOKEN" "group token" "$VMCP_URL"
pass 4 "vMCP: no-token 401+WWW-Authenticate, other-group empty list + call denied, $MCP_GROUP token calls $MCP_ADMITTED_TOOL -> 7"

# ---------- 5. client credentials 토큰(cid=agent-dev)으로 tools/call 직접 호출 ----------
# 옛 STEP 5 는 에이전트 런타임이 이 토큰으로 vMCP 를 부르는 채팅이었다. Agent 가 보류되면서(D22)
# 소비자 자리에서 같은 토큰으로 tools/call 을 직접 한다. cid 정책이 통과시키는지가 게이트다.
cc_token() { # client credentials -> access_token (cid=agent-dev)
  curl -fsS -X POST "$OIDC/token" -u "$AGENT_CLIENT_ID:$AGENT_CLIENT_SECRET" \
    -d grant_type=client_credentials -d scope=mcp.invoke \
    | json_of - 'd["access_token"]'
}
CC_TOKEN="$(cc_token)" || die 5 "client credentials token for $AGENT_CLIENT_ID failed"
TOOL=""
for ((i = 0; i < 12; i++)); do  # 재실행이면 직전 STEP 6 이 뺀 cid 정책이 redeploy 로 돌아올 때까지 vMCP 파드가 늦을 수 있다
  rc="$(mcp --token "$CC_TOKEN" list "$VMCP_URL")"
  [ "$rc" = "0" ] || die 5 "cid tools/list rc=$rc: $(cat "$WORK/mcp.err")"
  TOOL="$(mcp_json 'next((t for t in d["tools"] if t.endswith("Add")), "")')"
  [ -n "$TOOL" ] && break; sleep 5
done
[ -n "$TOOL" ] || die 5 "cid token sees no Add tool: $(mcp_json 'd["tools"]')"
rc="$(mcp --token "$CC_TOKEN" call "$VMCP_URL" "$TOOL" '{"a": 3, "b": 4}')"
[ "$rc" = "0" ] || die 5 "cid call $TOOL rc=$rc: $(cat "$WORK/mcp.json" "$WORK/mcp.err")"
mcp_json 'json.dumps(d["call"]["content"])' | grep -q 7 || die 5 "cid call result without 7: $(cat "$WORK/mcp.json")"
pass 5 "client credentials token (cid=$AGENT_CLIENT_ID) calls $TOOL over OIDC vMCP -> 7"

# ---------- 6. cid 정책에서 agent-dev 제거 -> 같은 토큰의 tools/call 인가 거부 ----------
code="$(api PATCH "/tenants/$TENANT/settings" '{"agent_client_ids": []}')"
[ "$code" = "200" ] || die 6 "PATCH agent_client_ids [] -> $code"
code="$(api POST "/gateways/$GW_ID/redeploy")"
[ "$code" = "200" ] || die 6 "redeploy -> $code"
wait_ready "/gateways/$GW_ID" 6
POLICIES="$(authz_policies virtualmcpserver "$NS" "$VMCP_NAME")"
if echo "$POLICIES" | grep -q 'claim_cid'; then die 6 "cid policy still present after redeploy: $POLICIES"; fi
denied=""
for ((i = 0; i < 12; i++)); do  # vMCP 파드가 새 정책을 집어 들 때까지 재시도
  rc="$(mcp --token "$CC_TOKEN" call "$VMCP_URL" "$TOOL" '{"a": 3, "b": 4}')"
  if [ "$rc" != "0" ] || ! mcp_json 'json.dumps(d["call"]["content"])' | grep -q 7; then denied=1; break; fi
  sleep 5
done
[ -n "$denied" ] || die 6 "cid call still succeeds after removing $AGENT_CLIENT_ID from cid policy"
pass 6 "cid call denied after removing $AGENT_CLIENT_ID from cid policy (rc=$rc err=$(head -c 80 "$WORK/mcp.err" | tr '\n' ' '))"

# ---------- 7. 노출된 MCP Server: vMCP 앞단 oidc, 무토큰 401, 토큰 7, 내부 경로 무인증 ----------
LB_IP="$("${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
[ -n "$LB_IP" ] || die 7 "Service $TRAEFIK_NS/$TRAEFIK_SVC has no LoadBalancer IP (is Traefik installed?)"
# 한 문서는 배포를 하나만 가진다. 같은 정의를 노출로 한 벌 더 띄우려면 복제한 뒤 배포한다 (D42). 이전 실행이 남긴
# 복제본(409)은 지우고 다시 복제한다(lib/e2e_api.sh 머리말).
# 노출은 복제 본문이 아니라 배포 본문에 싣는다. 플랜 F5 의 "복제(exposure 포함)" 와 다른 자리다.
# 노출을 켠 배포를 서버가 받는지 거부하는지가 배포 호출에 그대로 드러나야 하고, e2e 넷이 같은 모양이 된다
clone_fresh 7 /mcp-servers "$MS_ID" "$EXPOSE_NAME"
MX_ID="$(jq_ 'd["id"]')"
# 노출 설정은 배포 본문이 받는다
body="{\"exposure\": {\"enabled\": true, \"host\": \"$EXPOSE_HOST\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"none\"}}}"
code="$(api POST "/mcp-servers/$MX_ID/deploy" "$body")"
[ "$code" = "201" ] || die 7 "POST /mcp-servers/$MX_ID/deploy $EXPOSE_NAME -> $code"
MX_NAME="$(jq_ 'd["k8s"]["name"]')"; EXT_URL="$(jq_ 'd["status"]["external_url"]')"
[ "$EXT_URL" = "http://$EXPOSE_HOST/mcp" ] || die 7 "external_url = $EXT_URL"
# warnings 에는 preflight 경고(orbstack 은 network_policy_unenforced)가 섞인다. 무인증 노출 경고만 없어야 한다
if jq_ 'd["warnings"]' | grep -q 'unauthenticated_exposure:'; then die 7 "oidc mode must not warn about unauthenticated exposure: $(jq_ 'd["warnings"]')"; fi
wait_ready "/mcp-servers/$MX_ID" 7
front=""
for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
  api GET "/mcp-servers/$MX_ID" >/dev/null
  front="$(jq_ 'json.dumps(d["status"]["exposure"])')"
  [ "$(jq_ '(d["status"].get("exposure") or {}).get("front_phase")')" = "Ready" ] && [ "$(jq_ '(d["status"].get("exposure") or {}).get("address_assigned")')" = "True" ] && break
  sleep 5
done
echo "  status.exposure: $front"
# 앞단은 이 서버 하나를 백엔드로 둔 vMCP front-<k8s.name> 이다 (P54 5절). vMCP 에는 trustProxyHeaders 가 없고 외부 주소는
# resourceUrl 이 정한다
FX="front-$MX_NAME"
"${K[@]}" -n "$NS" get virtualmcpserver "$FX" -o json >"$WORK/front.json" || die 7 "VirtualMCPServer $FX missing"
FRONT="$(json_of "$WORK/front.json" '(d["status"].get("phase"), d["spec"]["incomingAuth"]["type"],
  d["spec"]["incomingAuth"]["oidcConfigRef"]["name"], d["spec"]["incomingAuth"]["oidcConfigRef"]["audience"],
  d["spec"]["incomingAuth"]["oidcConfigRef"].get("resourceUrl"),
  "authz" if d["spec"]["incomingAuth"].get("authzConfig") or d["spec"]["incomingAuth"].get("authzConfigRef") else "no-authz")')"
[ "$FRONT" = "Ready oidc daegam-oidc daegam-mcp $EXT_URL authz" ] || die 7 "vMCP front spec/status: $FRONT"
backend="$("${K[@]}" -n "$NS" get ingress "$MX_NAME" -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.number}')" \
  || die 7 "Ingress $MX_NAME missing"
[ "$backend" = "vmcp-$FX:4483" ] || die 7 "Ingress $MX_NAME backend $backend (want vmcp-$FX:4483)"
# 앞단의 Entry 는 서비스 토큰을 headerInjection 으로 보낸다. 그 토큰은 이 MCP Server 하나를 연다 (P50 9절)
ENTRY_AUTH="$("${K[@]}" -n "$NS" get mcpserverentry "$FX" -o jsonpath='{.spec.externalAuthConfigRef.name}')" || die 7 "MCPServerEntry $FX missing"
[ "$ENTRY_AUTH" = "$FX" ] || die 7 "MCPServerEntry $FX sends auth config '$ENTRY_AUTH' (want $FX)"
INJECT="$("${K[@]}" -n "$NS" get mcpexternalauthconfig "$FX" -o jsonpath='{.spec.type} {.spec.headerInjection.headerName} {.spec.headerInjection.valueSecretRef.name}')" \
  || die 7 "MCPExternalAuthConfig $FX missing"
[ "$INJECT" = "headerInjection Authorization svc-expose-$MX_NAME" ] || die 7 "MCPExternalAuthConfig $FX: $INJECT"
FRONT_AUD="$(token_aud "$NS" "svc-expose-$MX_NAME")" || die 7 "Secret svc-expose-$MX_NAME has no readable token"
[ "$FRONT_AUD" = "[\"daegam-internal:$TENANT\", \"daegam-internal:$TENANT:$MX_ID\"]" ] || die 7 "front token aud: $FRONT_AUD"
code=""
for ((i = 0; i < 12; i++)); do  # Traefik 이 라우트를 집어 들 때까지 (404 → 401)
  code="$(rpc_curl "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"initialize\", \"params\": $INIT_PARAMS}")"
  [ "$code" = "401" ] && break; sleep 5
done
[ "$code" = "401" ] || die 7 "no-token initialize via $EXT_URL -> $code (expected 401): $(cat "$WORK/rpc.raw")"
WWW="$(hdr www-authenticate "$WORK/rpc.hdr")"
[ -n "$WWW" ] || die 7 "401 via ingress without WWW-Authenticate"
# 노출은 MCP 와 RFC 9728 메타데이터 경로만 연다 (P50 5절). 메타데이터는 두 꼴 모두 이 주소를 resource 로 답하고,
# 앞단의 /health 는 Traefik 의 404 다. 옛 렌더(Prefix /)에서는 /health 가 200 이다
for p in /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/mcp; do
  code="$(get_ingress "http://$EXPOSE_HOST$p" "$EXPOSE_HOST:80:$LB_IP")"
  [ "$code" = "200" ] || die 7 "GET http://$EXPOSE_HOST$p via Traefik -> $code (expected 200)"
  [ "$(json_of "$WORK/get.raw" 'd.get("resource")')" = "$EXT_URL" ] || die 7 "$p resource: $(cat "$WORK/get.raw")"
done
code="$(get_ingress "http://$EXPOSE_HOST/health" "$EXPOSE_HOST:80:$LB_IP")"
[ "$code" = "404" ] || die 7 "GET http://$EXPOSE_HOST/health via Traefik -> $code (only /mcp and the metadata path are routed)"
# 막 뜬 vMCP 앞단은 서버를 라우팅하기까지 ROUTE_WAIT 안에서 다시 본다(lib/mcp_rpc.sh)
code="$(ingress_until "$ADD_SEVEN" "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$GROUP_TOKEN")"
[ "$code" = "200" ] || die 7 "group token tools/call via $EXT_URL -> $code: $(cat "$WORK/rpc.raw")"
[ "$(rpc_ "$ADD_SEVEN")" = True ] || die 7 "Add via ingress without 7: $(cat "$WORK/rpc.json")"
# 타 그룹 토큰은 인증은 통과해도 Cedar 가 막는다 (HTTP 403 이거나 JSON-RPC error, 또는 isError 결과)
code="$(mcp_ingress "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$OTHER_TOKEN")"
if [ "$code" = "200" ] && [ "$(rpc_ '"ok" if (d or {}).get("result") and not d["result"].get("isError") else "denied"')" = "ok" ]; then
  die 7 "other-group token got a result via ingress: $(cat "$WORK/rpc.json")"
fi
OTHER_CODE="$code"
# 턴 위임 토큰 (P53 3.2절). 앞단의 forbid 는 local 모드에만 렌더된다. oidc 에서 위임 토큰의 모양을 막는 것은 바탕 정책이다
FRONT_POLICIES="$(authz_policies virtualmcpserver "$NS" "$FX")"
if echo "$FRONT_POLICIES" | grep -q claim_bound_to; then die 7 "the oidc front renders the delegation forbid (local mode only): $FRONT_POLICIES"; fi
BOUND_TOKEN="$(user_token '{"sub": "e2e-person", "extra": {"act": {"sub": "svc:agent:e2e"}, "bound_to": "mcp-server:'"$MX_ID"'", "tenant": "'"$TENANT"'"}}')" \
  || die 7 "a token shaped like a turn token from $OIDC/_test/token failed"
code="$(mcp_ingress "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$BOUND_TOKEN")"
if [ "$code" = "200" ] && [ "$(rpc_ '"ok" if (d or {}).get("result") and not d["result"].get("isError") else "denied"')" = "ok" ]; then
  die 7 "a token with bound_to and act and no groups got a result via the front: $(cat "$WORK/rpc.json")"
fi
BOUND_CODE="$code"
ACT_TOKEN="$(user_token '{"sub": "member-delegated", "groups": ["'"$MCP_GROUP"'"], "extra": {"act": {"sub": "e2e-idp-actor"}}}')" \
  || die 7 "an IdP token with act from $OIDC/_test/token failed"
code="$(mcp_ingress "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$ACT_TOKEN")"
{ [ "$code" = "200" ] && rpc_ 'json.dumps(d["result"]["content"])' | grep -q 7; } \
  || die 7 "an IdP token with act and $MCP_GROUP via the front -> $code (oidc admits it as before, P53 3.2): $(cat "$WORK/rpc.raw")"
# 내부 경로: MCPServer 의 프록시 Service 는 플랫폼이 낸 서비스 토큰을 그 서버의 audience 로 검증한다 (D41, P50 9절).
# 무토큰과 사람 토큰(aud daegam-mcp)은 401, 그 서버의 audience 를 실은 서비스 토큰은 200 이다
pf "$NS" "svc/mcp-$MX_NAME-proxy" "${PROXY_PORT}:8080"
code="$(mcp_status "http://127.0.0.1:${PROXY_PORT}/mcp")"
[ "$code" = "401" ] || die 7 "internal proxy initialize without token -> $code (D41 requires 401)"
code="$(mcp_status "http://127.0.0.1:${PROXY_PORT}/mcp" "$GROUP_TOKEN")"
[ "$code" = "401" ] || die 7 "internal proxy accepted a human token (aud daegam-mcp) -> $code; the internal audience must differ"
SVC_TOKEN="$(svc_token "$MX_ID")" || die 7 "svc_token.py failed"
rc="$(mcp --token "$SVC_TOKEN" list "http://127.0.0.1:${PROXY_PORT}/mcp")"
[ "$rc" = "0" ] || die 7 "internal proxy tools/list with service token rc=$rc: $(cat "$WORK/mcp.err")"
[ -n "$(mcp_json '[t for t in d["tools"] if t.endswith("Add")][0]')" ] || die 7 "internal proxy lists no Add: $(mcp_json 'd["tools"]')"
pass 7 "exposed $EXPOSE_NAME ($MX_NAME) $EXT_URL: vMCP front $FX Ready (oidcConfigRef daegam-oidc/daegam-mcp resourceUrl=$EXT_URL), Ingress -> $backend, Entry token by headerInjection of svc-expose-$MX_NAME, no-token 401 (WWW-Authenticate: $WWW), metadata 200, /health 404, group token Add -> 7, other-group -> $OTHER_CODE, internal mcp-$MX_NAME-proxy: no-token 401, human token 401, service token 200; turn-token shape (bound_to, act, no groups) -> $BOUND_CODE, IdP token with act and $MCP_GROUP -> 7, no delegation forbid on the oidc front"

# ---------- 7b. 새 서버는 자기 audience 로만 검증한다 (P50 9절, G9 2단계) ----------
# 복제본은 이 실행이 새로 만든 문서라 처음부터 옮겨 가 있다(clone_fresh). 1단계 렌더는 테넌트 audience 라 여기서 멈춘다
OWN_AUD="daegam-internal:$TENANT:$MX_ID"
aud="$("${K[@]}" -n "$NS" get mcpserver "$MX_NAME" -o jsonpath='{.spec.oidcConfigRef.audience}')"
[ "$aud" = "$OWN_AUD" ] || die 7b "MCPServer $NS/$MX_NAME verifies audience $aud (want its own $OWN_AUD)"
TENANT_TOKEN="$(svc_token)" || die 7b "svc_token.py failed"
code="$(mcp_status "http://127.0.0.1:${PROXY_PORT}/mcp" "$TENANT_TOKEN")"
[ "$code" = "401" ] || die 7b "internal proxy accepted a service token with the tenant audience alone -> $code (want 401)"
pass 7b "MCPServer $MX_NAME verifies $OWN_AUD; a service token with the tenant audience alone -> 401, with the server audience -> 200 (step 7)"

# ---------- 7c. 테넌트 울타리 (P50 11절) ----------
# 서비스 토큰도 위임 토큰도 아닌 토큰, 다른 테넌트의 클레임을 실은 토큰은 audience 가 맞아도 Cedar 가 막는다 (빈 목록과 거부).
# 건너뛸지는 플랫폼의 판정으로 정한다. 그 클러스터가 울타리 게이트를 지나지 못하면 상태 API 의 fallbacks 에 tenant_fence 가 있다
[ "$(api GET "/mcp-servers/$MX_ID")" = 200 ] || die 7c "GET /mcp-servers/$MX_ID failed"
MX_CLUSTER="$(jq_ 'd["cluster_id"]')"
[ "$(api GET /settings/data-plane-auth)" = 200 ] || die 7c "GET /settings/data-plane-auth failed"
if [ "$(jq_ "'tenant_fence' in d['fallbacks'].get('$MX_CLUSTER', {})")" = True ]; then
  cond_skip 7c "cluster $MX_CLUSTER fails the tenant_fence gate (fallbacks: $(jq_ "d['fallbacks'].get('$MX_CLUSTER')")), so internal MCPServers carry no fence"
else
  FENCE="$("${K[@]}" -n "$NS" get mcpserver "$MX_NAME" -o jsonpath='{.spec.authzConfigRef.name}')"
  [ "$FENCE" = "$MX_NAME-authz" ] || die 7c "MCPServer $NS/$MX_NAME references authz config '$FENCE' (want $MX_NAME-authz)"
  authz_policies mcpserver "$NS" "$MX_NAME" | grep -q "claim_tenant == \"$TENANT\"" \
    || die 7c "$FENCE does not fence tenant $TENANT: $(cat "$WORK/authz.json")"
  HUMAN_TOKEN="$(svc_token "$MX_ID" --human-sub user:e2e-fence)" || die 7c "svc_token.py --human-sub failed"
  mcp_refuses 7c "$HUMAN_TOKEN" "a token with the server audience and sub user:e2e-fence" "http://127.0.0.1:${PROXY_PORT}/mcp"
  STRANGER_TOKEN="$(svc_token "" --tenant e2e-stranger --audience "$OWN_AUD")" || die 7c "svc_token.py --tenant failed"
  mcp_refuses 7c "$STRANGER_TOKEN" "a service token of tenant e2e-stranger with the server audience" \
    "http://127.0.0.1:${PROXY_PORT}/mcp"
  pass 7c "MCPServer $MX_NAME references $FENCE; sub user:e2e-fence and tenant e2e-stranger get an empty list and a refused call, the tenant's service token lists Add (step 7) and the vMCP lists through it (step 4)"
fi

# ---------- 7r. Tool 규칙이 앞단을 지난다 (P52 2절) ----------
# 서버의 규칙은 그 서버의 리비전이다. 재배포는 앞단을 먼저 적용하고 Valid 를 기다린 뒤 서버를 적용한다.
# 앞단의 파드가 바뀌는 동안은 앞의 규칙이 적용되고 상태가 rules_front_rolling 을 낸다. 그것이 사라진 뒤에 부른다
[ "$(api GET "/mcp-servers/$MX_ID")" = 200 ] || die 7r "GET /mcp-servers/$MX_ID failed"
cp "$WORK/body" "$WORK/mx.json"
python3 "$HERE/lib/rules_body.py" "$WORK/mx.json" '{"Add": {"callers": {"groups": ["'"$RULE_GROUP"'"]}}}' >"$WORK/mx-rules.json" \
  || die 7r "building the rules body failed"
code="$(api PUT "/mcp-servers/$MX_ID" "$(cat "$WORK/mx-rules.json")")"
[ "$code" = 200 ] || die 7r "PUT /mcp-servers/$MX_ID with a group rule on Add -> $code: $(cat "$WORK/body")"
code="$(api POST "/mcp-servers/$MX_ID/redeploy" '{}')"
[ "$code" = 200 ] || die 7r "POST /mcp-servers/$MX_ID/redeploy -> $code: $(cat "$WORK/body")"
wait_ready "/mcp-servers/$MX_ID" 7r
rolling="(not read yet)"
for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
  api GET "/mcp-servers/$MX_ID" >/dev/null
  rolling="$(jq_ '[w for w in (d.get("status") or {}).get("warnings", []) if w.startswith(("rules_front_rolling", "rules_toolhive_old"))]')"
  [ "$(jq_ 'd["deployed_revision"] == d["revision"]')" = True ] && [ "$rolling" = "[]" ] && break
  sleep 5
done
[ "$rolling" = "[]" ] || die 7r "the front did not take the rule within ${READY_TIMEOUT}s: $rolling"
RULE_TOKEN="$(user_token '{"sub": "member-ruled", "groups": ["'"$MCP_GROUP"'", "'"$RULE_GROUP"'"]}')" \
  || die 7r "an IdP token with $MCP_GROUP and $RULE_GROUP from $OIDC/_test/token failed"
# 앞단의 새 파드는 첫 요청에서 JWKS 를 받고, 서버를 라우팅하기까지 ROUTE_WAIT 안에서 다시 본다(lib/mcp_rpc.sh)
code="$(ingress_until "$ADD_SEVEN" "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$RULE_TOKEN")"
{ [ "$code" = 200 ] && [ "$(rpc_ "$ADD_SEVEN")" = True ]; } \
  || die 7r "a token with $RULE_GROUP through the front -> $code: $(cat "$WORK/rpc.raw")"
code="$(mcp_ingress "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/list '{}' "$GROUP_TOKEN")"
{ [ "$code" = 200 ] && [ "$(rpc_ '((d or {}).get("result") or {}).get("tools")')" = "[]" ]; } \
  || die 7r "the tenant group alone still lists tools through the front -> $code: $(cat "$WORK/rpc.json")"
# 규칙이 막은 호출은 앞단의 403 이다(인가 거부). 다른 답이면 규칙이 아닌 무엇이 막은 것이다
code="$(mcp_ingress "$EXT_URL" "$EXPOSE_HOST:80:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}' "$GROUP_TOKEN")"
[ "$code" = 403 ] || die 7r "the tenant group alone calling Add through the front -> $code (want 403): $(cat "$WORK/rpc.json")"
RULE_DENIED="$code"
# 서버는 그룹 절을 짓지 않는다. 서비스 토큰의 그룹은 테넌트 그룹이라 그룹 절이 있었다면 열렸을 것이다 (2절)
SVC_TOKEN="$(svc_token "$MX_ID")" || die 7r "svc_token.py failed"
# 다시 배포가 서버의 파드를 바꿨다. STEP 7 이 연 kubectl 은 내려간 파드에 붙은 채 포트를 쥐고 있으므로 새 파드로 다시
# 열고(lib/e2e_pf.sh restart_pf), 토큰 없는 요청이 401 로 답할 때까지 기다린다
restart_pf "$PROXY_PORT"
code=""
for ((i = 0; i < 60; i++)); do
  code="$(mcp_status "http://127.0.0.1:${PROXY_PORT}/mcp" 2>/dev/null || true)"
  [ "$code" = 401 ] && break
  sleep 1
done
[ "$code" = 401 ] || die 7r "the internal proxy did not answer 401 without a token after the redeploy: $code"
rc="$(mcp --token "$SVC_TOKEN" list "http://127.0.0.1:${PROXY_PORT}/mcp")"
[ "$rc" = 0 ] || die 7r "internal proxy tools/list with a service token rc=$rc: $(cat "$WORK/mcp.err")"
[ "$(mcp_json '[t for t in d["tools"] if t.endswith("Add")]')" = "[]" ] \
  || die 7r "a service token sees the group-ruled Add at the server itself: $(mcp_json 'd["tools"]')"
pass 7r "rule on Add (group $RULE_GROUP) redeployed front first; rules_front_rolling cleared; through the front a token with $RULE_GROUP -> 7, the tenant group alone -> empty list and $RULE_DENIED; the server renders no group clause, so a service token does not see Add there"

# ---------- 8. oidc 모드에서 게시 항목을 백엔드로 -> 422 published_oidc_unsupported ----------
code="$(api POST "/gateways/$GW_ID/publish" '{"name": "gw-auth-pub"}')"
case "$code" in 201) PUB_ID="$(jq_ 'd["id"]')" ;; 409) listed_id 8 /hub gw-auth-pub; PUB_ID="$LISTED_ID" ;; *) die 8 "publish -> $code" ;; esac
code="$(api POST /gateways "{\"name\": \"gw-auth-consumer\", \"backends\": [{\"hub_item_id\": \"$PUB_ID\"}]}")"
[ "$code" = "422" ] || die 8 "published backend in oidc mode -> $code (expected 422)"
[ "$(jq_ "d['detail']['code']")" = "published_oidc_unsupported" ] || die 8 "code = $(jq_ "d['detail']['code']")"
api_delete "/hub/$PUB_ID"
pass 8 "published backend rejected in oidc mode with 422 published_oidc_unsupported"

# ---------- 정리 ----------
api_delete "/mcp-servers/$MX_ID"
# Gateway 를 먼저 지운다. 그것이 붙들고 있는 동안 MCP Server 삭제는 409 in_use 다 (D42)
api_delete "/gateways/$GW_ID"
api_delete "/mcp-servers/$MS_ID"
# 문서 삭제가 게시 항목을 함께 지운다 (docs/guide/hub-gateway.md 6절)
api GET /hub >/dev/null
[ "$(jq_ "[i['id'] for i in d if i['id']=='$HUB_ID']")" = "[]" ] || die 9 "hub item $HUB_ID survived deleting $MS_NAME"
gone=""
for ((i = 0; i < 24; i++)); do
  left="$("${K[@]}" -n "$NS" get "ingress/$MX_NAME" "networkpolicy/daegam-expose-$MX_NAME" "virtualmcpserver/front-$MX_NAME" \
    "mcpgroup/front-$MX_NAME" "mcpserverentry/front-$MX_NAME" "mcpauthzconfig/front-$MX_NAME-authz" \
    "mcpexternalauthconfig/front-$MX_NAME" -o name --ignore-not-found 2>/dev/null | wc -l | tr -d ' ')"
  [ "$left" = "0" ] && { gone=1; break; }; sleep 5
done
[ -n "$gone" ] || die 9 "exposure objects of $MX_NAME still present"
finish
