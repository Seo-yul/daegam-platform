#!/usr/bin/env bash
# P56 F0.1·F0.4·F0.5(런타임 쪽)·F0.6 의 측정 (docs/plan/phase-56-code-mode.md 8절). .github/workflows/p56-f0.yml 이 k3d
# 에서 부른다. 이 파일과 p56_tool.py·p56_runtime.py, 그 워크플로는 F0 측정 동안만 둔다.
#
#   scripts/f0/p56.sh setup                   재는 Gateway 를 만들고 local 모드로 바꾼 뒤 Code mode 를 켠다. 다음 단계가
#                                             읽는 상태(id, 개인 토큰)는 F1_STATE 에 0600 으로 두고 올리지 않는다
#   scripts/f0/p56.sh f0.1|f0.4|f0.5|f0.6     한 항목을 잰다. 워크플로는 항목마다 한 번씩 불러 한 항목의 실패가 다른
#                                             항목을 막지 않게 한다. 메트릭의 기준이 다른 항목의 호출을 세지 않도록
#                                             F0.4 를 맨 앞에, 파드를 죽이는 F0.6 을 맨 뒤에 둔다
#
# 전제. 태그의 클러스터 e2e 잡처럼 만든 k3d(make operator-install·deploy-local, Traefik traefik/traefik)다. setup 은 header
# 모드에서 문서를 만들고 local 모드로 바꾼 뒤 그대로 둔다(잡의 클러스터는 버린다. 로컬에서 돌렸으면 손으로 되돌린다).
# 환경: KUBE_CONTEXT(k3d-daegam) F0_OUT F1_STATE F0_ROLLUP_WAIT(롤업을 기다리는 초, 600)
#
# 재는 대상. 시험 서버(f0_server.py)의 REST 백엔드에서 GET Tool 넷(echo·big·slow·fail)을 내는 MCP Server 를 게시하고, 그
# 넷을 고른 Gateway(백엔드 타임아웃 30초)를 배포해 entry 모드로 게시한다(svc:platform 의 tools/list 길). F2 가 아직 없으므로
# Code mode 는 6절이 정한 렌더를 산 객체에 얹는다. VirtualMCPServer 에 `spec.config.codeMode{enabled, toolCallTimeout}` 을
# 플랫폼처럼 server-side apply(Strict 검사, field manager daegam-f0)로 싣고, 6.3절의 hint forbid 를 Cedar 정책 끝에 더한다
# (authzConfigRef 면 그 MCPAuthzConfig, 아니면 인라인). 백엔드의 filter 는 Tool 을 고른 백엔드라 이미 렌더에 있다. 플랫폼이
# 다시 렌더하면 정책 목록을 되돌릴 수 있어 항목마다 먼저 보고 빠졌으면 다시 얹고 적는다. 플랫폼 밖의 클라이언트는
# f0-code.e2e.local 의 Ingress(vmcp-<k8s>:4483)와 Traefik 만 받는 NetworkPolicy 로 들어오고, 이 이름은 Traefik 의
# LoadBalancer IP 로 푼다(p56_tool.py --ip).
#
# F0.1  setup 이 적은 오퍼레이터의 답(Strict dry run, generation·observedGeneration, 조건, 오퍼레이터가 만든 ConfigMap 의
#       codeMode, vMCP 의 기동 로그)을 싣고, execute_tool_script 가 Modern 클라이언트(fastmcp auto, 사람의 개인 토큰),
#       Legacy 클라이언트(fastmcp legacy), 런타임의 클라이언트(p56_runtime.py)와 플랫폼의 svc:platform(게시 항목의 Tools
#       탭과 Tool 새로고침)에 보이는지 찍는다. 스크립트 하나가 inner call 로 echo 를 부르는지도 본다
# F0.4  롤업이 이 단계가 시작한 뒤의 수집까지 센 것을 기준으로 잡고(rolled_up_at 이 시작에서 수집 주기를 지났고
#       counted_from 이 비었다), inner call 셋(echo 둘, big 하나)의 스크립트 하나를 부른다. vMCP 와 MCP Server 프록시의 감사
#       이벤트(f0_tool audit)를 찍고, rolled_up_at 이 스크립트의 끝에서 수집 주기를 지날 때까지 기다려 Gateway·MCP Server
#       메트릭의 앞뒤를 견준다
# F0.5  런타임 쪽. 런타임의 클라이언트(p56_runtime.py)가 1분을 넘는 스크립트(slow 25초 셋을 차례로)를 런타임의 기다림
#       60초와 75초로 부르고 무엇을 보는지(답, 오류, 걸린 초) 찍는다
# F0.6  vMCP 파드를 새로 띄운 뒤 경우마다 노드의 파드 cgroup 을 0.25초마다 읽으며 스크립트를 부른다: 1 MiB 결과 열 개를
#       parallel 로 받아 길이만 돌려주는 것, 그 열 개를 그대로 돌려주는 것, 문자열 반복(384 MiB), 리스트 반복(원소
#       32 Mi 개), 1 MiB 문자열을 열 번 두 배로 키우는 것(1 GiB). 다른 사람의 세션이 그동안 0.2초마다 직접 호출한다
#       (f0_tool loop). 경우마다 스크립트의 답, 재시작 수와 마지막 종료 까닭, 메모리(쉴 때, 창 안의 최고, memory.peak, 한도,
#       oom_kill), 다시 답하기까지 걸린 초, 다른 세션이 본 실패를 찍는다. 앞 경우가 컨테이너를 재시작시켰으면 kubelet 의
#       backoff 를 지우려고 파드를 새로 띄운 뒤 다음 경우로 간다. 앞의 둘은 6.4절 기본값의 후보(두 배, 256 MiB 아래로는 가지
#       않는다)를 덧붙인다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
. "$ROOT/scripts/lib/e2e_exit.sh"
. "$ROOT/scripts/lib/e2e_json.sh"
CTX="${KUBE_CONTEXT:-k3d-daegam}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-f0-p56.XXXXXX")"
# F0 도구의 포트. lib/e2e_ports.sh 의 표에 없는 자리이고 F0 도구와 함께 사라진다
API_PORT="${E2E_API_PORT:-19092}"; OIDC_PORT="${E2E_OIDC_PORT:-19093}"
TENANT="${F0_TENANT:-f0-code}"
MCP_CLIENT_NAME="daegam-f0-p56"
. "$HERE/lib.sh"
# 단계 사이의 상태는 lib.sh 의 F1 상태 함수(f1_state·f1_get·f1_put)로 잇는다
F1_STATE="${F1_STATE:-${TMPDIR:-/tmp}/daegam-f0-p56-state}"

HOST="${F0_CODE_HOST:-f0-code.e2e.local}"
URL="http://$HOST/mcp"
REST_NAME=f0-code-rest; SERVER_NAME=f0-code-reads; GW_NAME=f0-code-gw
TOOLS='["echo", "big", "slow", "fail"]'
# Gateway 백엔드의 타임아웃. toolCallTimeout 은 6.4절대로 가장 긴 백엔드 타임아웃이고 60초가 상한이다
BACKEND_TIMEOUT=30
TOOL_CALL_TIMEOUT="$((BACKEND_TIMEOUT < 60 ? BACKEND_TIMEOUT : 60))s"
# 6.3절의 hint forbid. 글자 그대로다
HINT_FORBID='forbid(principal, action == Action::"call_tool", resource)
  unless { resource has readOnlyHint && resource.readOnlyHint == true };'
ROLLUP_WAIT="${F0_ROLLUP_WAIT:-600}"
# 수집기의 주기(DAEGAM_AUDIT_COLLECTOR_INTERVAL 의 기본값). 어떤 때에서 이만큼 지난 뒤 끝난 롤업 틱은 그때까지의 로그를
# 수집기가 읽은 뒤의 것이다
COLLECT_SECONDS="${F0_COLLECT_SECONDS:-60}"
GW_ID=""; GW_K8S=""; GW_NS=""; MS_ID=""; MS_K8S=""; MS_NS=""; PUB_ID=""; NS=""; VMCP_SEL=""

cleanup() {
  f0_restore_mode
  rm -rf "$WORK"
}
on_exit cleanup

command -v uv >/dev/null || gate_incomplete "uv is not installed (the F0 tools run in the platform and runtime environments)"

p56_tool() { "${LAB_PY[@]}" "$HERE/p56_tool.py" "$@"; }
p56_runtime() { uv run --quiet --project "$ROOT/agent-runtime" python "$HERE/p56_runtime.py" "$@"; }

# load_state STEP -> 상태를 읽어 전역에 두고, Gateway 의 vMCP 네임스페이스(NS, restart_pods 가 읽는다)와 파드 라벨
# (VMCP_SEL)은 lib/e2e_mcp_access.sh 의 gateway_vmcp 로 플랫폼에서 읽는다. 플랫폼에 붙은 뒤 부른다
load_state() {
  f1_state
  GW_ID="$(f1_get gw-id)"; GW_K8S="$(f1_get gw-k8s)"; GW_NS="$(f1_get gw-ns)"
  MS_ID="$(f1_get ms-id)"; MS_K8S="$(f1_get ms-k8s)"; MS_NS="$(f1_get ms-ns)"; PUB_ID="$(f1_get pub-id)"
  gateway_vmcp "$1" "$GW_ID"
}

# ---------- Code mode 를 얹는다 ----------

# policy_ref -> hint forbid 를 더할 객체 "KIND NAME POINTER". vMCP 가 MCPAuthzConfig 를 참조하면 그것이다(P50 8절)
policy_ref() {
  local ref
  "${K[@]}" -n "$GW_NS" get virtualmcpserver "$GW_K8S" -o json >"$WORK/vmcp.json" || return 1
  ref="$(json_of "$WORK/vmcp.json" '((d["spec"].get("incomingAuth") or {}).get("authzConfigRef") or {}).get("name") or ""')"
  if [ -n "$ref" ]; then
    echo "mcpauthzconfig $ref /spec/config/policies"
  else
    echo "virtualmcpserver $GW_K8S /spec/incomingAuth/authzConfig/inline/policies"
  fi
}

# hint_state KIND NAME POINTER -> yes(정책 끝에 있다)·no(없다)·missing(정책 목록이 없다)
hint_state() {
  "${K[@]}" -n "$GW_NS" get "$1" "$2" -o json >"$WORK/authz.json" || { echo missing; return 0; }
  HINT="$HINT_FORBID" python3 - "$WORK/authz.json" "$3" <<'PY'
import json, os, sys
node = json.load(open(sys.argv[1]))
for part in sys.argv[2].strip("/").split("/"):
    node = node.get(part) if isinstance(node, dict) else None
print("missing" if not isinstance(node, list) else ("yes" if os.environ["HINT"] in node else "no"))
PY
}

# code_mode_state -> "codeMode=<JSON> hint=<yes|no|missing> (<KIND>/<NAME>)"
code_mode_state() {
  local kind name ptr cm
  read -r kind name ptr <<<"$(policy_ref)"
  cm="$(json_of "$WORK/vmcp.json" 'json.dumps((d["spec"].get("config") or {}).get("codeMode"))')"
  echo "codeMode=$cm hint=$(hint_state "$kind" "$name" "$ptr") ($kind/$name)"
}

# code_mode_on STEP [FILE] -> codeMode 를 싣고 hint forbid 를 더한 뒤 오퍼레이터가 굴린 vMCP 를 기다리고 파드를 새로
# 띄운다(발급한 개인 토큰의 키를 새 파드가 JWKS 에서 받는다). 오퍼레이터의 답은 F0_OUT/FILE(기본 f0.1-operator.txt)에 남는다
code_mode_on() {
  local step="$1" out="$F0_OUT/${2:-f0.1-operator.txt}" api_version kind name ptr gen observed i patch
  "${K[@]}" -n "$GW_NS" get virtualmcpserver "$GW_K8S" -o json >"$WORK/vmcp.json" \
    || die "$step" "VirtualMCPServer $GW_NS/$GW_K8S missing"
  api_version="$(json_of "$WORK/vmcp.json" 'd["apiVersion"]')"
  gen="$(json_of "$WORK/vmcp.json" 'd["metadata"]["generation"]')"
  cat >"$WORK/codemode.yaml" <<EOF
apiVersion: $api_version
kind: VirtualMCPServer
metadata: {name: $GW_K8S, namespace: $GW_NS}
spec:
  config:
    codeMode: {enabled: true, toolCallTimeout: "$TOOL_CALL_TIMEOUT"}
EOF
  {
    echo "VirtualMCPServer $GW_NS/$GW_K8S ($api_version), generation $gen before"
    echo "applied: spec.config.codeMode {enabled: true, toolCallTimeout: $TOOL_CALL_TIMEOUT} (server-side apply, Strict)"
  } >"$out"
  if "${K[@]}" apply --server-side --field-manager=daegam-f0 --validate=strict --dry-run=server -o json \
    -f "$WORK/codemode.yaml" >"$WORK/dry.json" 2>"$WORK/dry.err"; then
    echo "strict dry run: accepted, stored codeMode $(json_of "$WORK/dry.json" 'json.dumps(d["spec"]["config"].get("codeMode"))')" >>"$out"
  else
    echo "strict dry run: REFUSED: $(head -c 600 "$WORK/dry.err" | f0_redact)" >>"$out"
    f0_block "$step" "$out"
    die "$step" "the API server refused spec.config.codeMode (recorded above)"
  fi
  "${K[@]}" apply --server-side --field-manager=daegam-f0 --validate=strict -f "$WORK/codemode.yaml" >/dev/null \
    || die "$step" "applying spec.config.codeMode failed"
  read -r kind name ptr <<<"$(policy_ref)" || die "$step" "reading the vMCP's Cedar reference failed"
  case "$(hint_state "$kind" "$name" "$ptr")" in
    yes) ;;
    no)
      patch="$(HINT="$HINT_FORBID" python3 -c 'import json, os, sys
print(json.dumps([{"op": "add", "path": sys.argv[1] + "/-", "value": os.environ["HINT"]}]))' "$ptr")"
      "${K[@]}" -n "$GW_NS" patch "$kind" "$name" --type=json -p "$patch" --field-manager=daegam-f0 >/dev/null \
        || die "$step" "adding the hint forbid to $kind/$name failed" ;;
    *) die "$step" "$kind/$name has no policy list at $ptr" ;;
  esac
  echo "hint forbid appended to $kind/$name $ptr" >>"$out"
  # 오퍼레이터가 새 generation 을 읽을 때까지 (status.observedGeneration 이 없으면 1분 뒤 그대로 적는다)
  for ((i = 0; i < 30; i++)); do
    "${K[@]}" -n "$GW_NS" get virtualmcpserver "$GW_K8S" -o json >"$WORK/vmcp.json" || { sleep 2; continue; }
    gen="$(json_of "$WORK/vmcp.json" 'd["metadata"]["generation"]')"
    observed="$(json_of "$WORK/vmcp.json" '(d.get("status") or {}).get("observedGeneration")')"
    [ "$observed" = "$gen" ] && break
    sleep 2
  done
  sleep 10
  "${K[@]}" -n "$GW_NS" rollout status "deploy/$GW_K8S" --timeout=300s >/dev/null \
    || die "$step" "the vMCP Deployment $GW_K8S did not roll out after code mode"
  restart_pods "$VMCP_SEL" || die "$step" "restarting the vMCP pods failed"
  {
    echo "after: generation $gen, observedGeneration $observed"
    json_of "$WORK/vmcp.json" '"phase %s; conditions: %s" % ((d.get("status") or {}).get("phase"), "; ".join(
      "%s=%s %s %s" % (c.get("type"), c.get("status"), c.get("reason") or "", (c.get("message") or "")[:160])
      for c in (d.get("status") or {}).get("conditions") or []))'
    echo "live: $(code_mode_state)"
    "${K[@]}" -n "$GW_NS" get configmap -o json >"$WORK/cms.json"
    p56_tool codemode "$WORK/cms.json" --owner "$GW_K8S"
    echo "vMCP log lines on code mode after the restart:"
    "${K[@]}" -n "$GW_NS" logs -l "$VMCP_SEL" --all-containers --tail=-1 2>/dev/null \
      | grep -iE 'code.?mode|execute_tool_script' | head -n 8 | cut -c1-300 | f0_redact || true
  } >>"$out"
  route_ready "$step"
}

# ensure_code_mode STEP -> 항목 앞에서 codeMode 와 hint forbid 가 아직 있는지 본다. 플랫폼이 다시 렌더해 빠졌으면 다시
# 얹고 그것을 적는다
ensure_code_mode() {
  local state
  state="$(code_mode_state)"
  case "$state" in
    *'"enabled": true'*"hint=yes"*) f0_note "$1" "code mode still on before the item: $state" ;;
    *) f0_note "$1" "code mode was no longer on before the item ($state), so it was applied again"
       code_mode_on "$1" "$1-reapplied.txt"
       f0_block "$1" "$F0_OUT/$1-reapplied.txt" ;;
  esac
}

# vmcp_route STEP -> f0-code.e2e.local 의 Ingress 와 Traefik 만 vMCP 파드에 들이는 정책. 플랫폼은 이 이름들을 렌더하지 않는다
vmcp_route() {
  "${K[@]}" apply -f - >/dev/null <<EOF || die "$1" "the Ingress or NetworkPolicy for $HOST failed"
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: f0-code-traefik, namespace: $GW_NS}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/name: virtualmcpserver, app.kubernetes.io/instance: $GW_K8S}}
  policyTypes: [Ingress]
  ingress: [{from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: $TRAEFIK_NS}}}]}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: f0-code, namespace: $GW_NS}
spec:
  ingressClassName: $INGRESS_CLASS
  rules:
    - host: $HOST
      http: {paths: [{path: /, pathType: Prefix, backend: {service: {name: vmcp-$GW_K8S, port: {number: 4483}}}}]}
EOF
  route_ready "$1"
}

# route_ready STEP -> Traefik 을 거친 토큰 없는 요청이 vMCP 의 401 을 받을 때까지(라우트가 없으면 404, 파드가 없으면 502·503)
route_ready() {
  local i code=""
  for ((i = 0; i < 90; i++)); do
    code="$(rpc_curl "$URL" "$(pin "$URL" "$LB_IP")" -d '{}')"
    [ "$code" = 401 ] && return 0
    sleep 2
  done
  die "$1" "$URL through Traefik answers $code without a token, not the vMCP's 401"
}

# ---------- setup ----------

setup() {
  local code pub_ms u1 u2 c1 c2
  f1_state
  f0_header_connect SETUP
  traefik_ip SETUP
  f0_server_up SETUP
  f0_rest_backend SETUP "$REST_NAME"
  save_doc SETUP /mcp-servers "$SERVER_NAME" "{\"name\": \"$SERVER_NAME\", \"backends\": [{\"backend_profile_id\":
    \"$BACKEND_API_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": $TOOLS, \"timeout_seconds\": $BACKEND_TIMEOUT}]}"
  MS_ID="$SAVED_ID"
  deploy_ready SETUP /mcp-servers "$MS_ID"
  MS_K8S="$(jq_ 'd["k8s"]["name"]')"; MS_NS="$(jq_ 'd["k8s"]["namespace"]')"
  code="$(api POST "/mcp-servers/$MS_ID/publish" '{}')"
  case "$code" in 200 | 201) pub_ms="$(jq_ 'd["id"]')" ;; *) die SETUP "publish $SERVER_NAME -> $code" ;; esac
  save_doc SETUP /gateways "$GW_NAME" "{\"name\": \"$GW_NAME\", \"backends\": [{\"hub_item_id\": \"$pub_ms\",
    \"tools\": $TOOLS, \"timeout_seconds\": $BACKEND_TIMEOUT}]}"
  GW_ID="$SAVED_ID"
  deploy_ready SETUP /gateways "$GW_ID"
  GW_K8S="$(jq_ 'd["k8s"]["name"]')"; GW_NS="$(jq_ 'd["k8s"]["namespace"]')"
  code="$(api POST "/gateways/$GW_ID/publish" '{}')"
  case "$code" in
    200 | 201) PUB_ID="$(jq_ 'd["id"]')" ;;
    409) listed_id SETUP /hub "$GW_NAME"; PUB_ID="$LISTED_ID" ;;
    *) die SETUP "publish $GW_NAME -> $code" ;;
  esac
  f1_put gw-id "$GW_ID"; f1_put gw-k8s "$GW_K8S"; f1_put gw-ns "$GW_NS"
  f1_put ms-id "$MS_ID"; f1_put ms-k8s "$MS_K8S"; f1_put ms-ns "$MS_NS"; f1_put pub-id "$PUB_ID"
  load_state SETUP

  # local 모드. 모드를 바꾸면 플랫폼 파드가 바뀐다. 옛 파드에 붙은 port-forward 를 닫고 local_admin_session 이 새로 연다.
  # MODE_SWITCHED 를 두지 않아 정리가 되돌리지 않는다(다음 단계가 이 모드를 쓴다)
  stop_pf "$PF_API" "$API_PORT"; PF_API=""
  switch_auth_mode local || die SETUP "switching to local mode failed"
  local_admin_session SETUP
  u1="$(local_fresh_member f0-code-u1 "$TENANT")" || die SETUP "creating the first member of $TENANT failed"
  u2="$(local_fresh_member f0-code-u2 "$TENANT")" || die SETUP "creating the second member of $TENANT failed"
  c1="$(local_login_any f0-code-u1 "$LOCAL_PW")"; [ -n "$c1" ] || die SETUP "login as f0-code-u1 failed"
  c2="$(local_login_any f0-code-u2 "$LOCAL_PW")"; [ -n "$c2" ] || die SETUP "login as f0-code-u2 failed"
  issue_ok SETUP "$c1" "$TENANT" "f0 code mode"; f1_put token "$ISSUED"
  issue_ok SETUP "$c2" "$TENANT" "f0 code mode other"; f1_put token-other "$ISSUED"
  # 발급한 키가 JWKS 캐시에 오른 뒤 vMCP 파드를 새로 띄운다(code_mode_on)
  sleep "$JWKS_CACHE_WAIT"
  vmcp_route SETUP
  code_mode_on SETUP
  f0_note SETUP "MCP Server $SERVER_NAME ($MS_NS/$MS_K8S) published; Gateway $GW_NAME ($GW_NS/$GW_K8S, tools $TOOLS, backend timeout ${BACKEND_TIMEOUT}s) published as hub item $PUB_ID; local mode; members f0-code-u1 ($u1) and f0-code-u2 ($u2) of $TENANT with personal tokens; $URL routed to vmcp-$GW_K8S:4483"
  f0_note SETUP "code mode: $(code_mode_state)"
}

# connect STEP -> 다음 단계가 local 모드의 플랫폼에 관리자로 다시 붙는다
connect() {
  local_admin_session "$1"
  load_state "$1"
  traefik_ip "$1"
}

# ---------- F0.1 ----------

f0_1() {
  local tools names
  f0_block F0.1 "$F0_OUT/f0.1-operator.txt"
  ensure_code_mode F0.1
  F0_TOKEN="$(f1_get token)" p56_tool listing --url "$URL" --ip "$LB_IP" --mode auto --out "$F0_OUT/f0.1-modern.json" \
    | f0_redact >"$WORK/line" || true
  f0_note F0.1 "$(cat "$WORK/line")"
  # 목록에 백엔드 Tool 이 하나도 없으면 vMCP 가 백엔드를 어떻게 보는지 남긴다(lib.sh 의 vmcp_diag)
  if [ -z "$(json_of "$F0_OUT/f0.1-modern.json" '" ".join(n for n in d.get("tools") or [] if n != "execute_tool_script")' \
    2>/dev/null || true)" ]; then
    vmcp_diag F0.1 "$GW_NS" "$GW_K8S" "app.kubernetes.io/instance=$MS_K8S" "$MS_NS"
  fi
  F0_TOKEN="$(f1_get token)" p56_tool listing --url "$URL" --ip "$LB_IP" --mode legacy --out "$F0_OUT/f0.1-legacy.json" \
    | f0_redact >"$WORK/line" || true
  f0_note F0.1 "$(cat "$WORK/line")"
  F0_TOKEN="$(f1_get token)" p56_runtime --url "$URL" --ip "$LB_IP" --code 'return 1' --out "$F0_OUT/f0.1-runtime.json" \
    2>&1 | f0_redact >"$WORK/line" || true
  f0_note F0.1 "$(tail -n 1 "$WORK/line")"
  # svc:platform: 게시 항목의 Tools 탭(그 자리의 tools/list)과 Tool 새로고침(저장하는 tools/list)
  API_OUT="$F0_OUT/f0.1-platform-tools.json" api GET "/hub/$PUB_ID/tools" >"$WORK/code"
  tools="$(json_of "$F0_OUT/f0.1-platform-tools.json" '"listed=%s reason=%s names=%s" % (d.get("listed"), d.get("reason"),
    [t["name"] for t in d.get("tools") or []])' 2>/dev/null || cat "$F0_OUT/f0.1-platform-tools.json")"
  f0_note F0.1 "svc:platform, GET /hub/$PUB_ID/tools -> $(cat "$WORK/code"): $tools"
  if [ "$(api POST "/hub/$PUB_ID/refresh-tools")" = 200 ]; then
    names="$(jq_ 'd.get("tool_names")')"
  else
    names="failed: $(head -c 300 "$WORK/body")"
  fi
  f0_note F0.1 "svc:platform, POST /hub/$PUB_ID/refresh-tools: tool_names $names"
  F0_TOKEN="$(f1_get token)" p56_tool script --url "$URL" --ip "$LB_IP" --label "f0.1 one inner call" \
    --code 'return call_tool("@echo@", text="f0-code")' --out "$F0_OUT/f0.1-script.jsonl" | f0_redact >"$WORK/line"
  f0_note F0.1 "$(cat "$WORK/line")"
}

# ---------- F0.4 ----------

# metrics PATH FILE -> P51 메트릭 응답을 FILE 에 둔다. 200 이 아니면 1
metrics() {
  [ "$(API_OUT="$2" api GET "$1/metrics?window=24h")" = 200 ]
}

# rolled AFTER -> Gateway 와 MCP Server 의 메트릭이 둘 다 AFTER(epoch 초) 뒤에 끝난 롤업 틱을 보이고 backfill 이 끝났을
# 때까지(source.rolled_up_at > AFTER, counted_from 이 비었다) 15초마다 다시 읽는다. 마지막으로 읽은 응답은
# $WORK/gw.json·$WORK/ms.json 이다. ROLLUP_WAIT 를 넘기면 1
rolled() {
  local i
  for ((i = 0; i < ROLLUP_WAIT / 15; i++)); do
    if metrics "/gateways/$GW_ID" "$WORK/gw.json" && metrics "/mcp-servers/$MS_ID" "$WORK/ms.json" \
      && p56_tool settled "$WORK/gw.json" --after "$1" >/dev/null \
      && p56_tool settled "$WORK/ms.json" --after "$1" >/dev/null; then
      return 0
    fi
    sleep 15
  done
  return 1
}

# rollup_state -> 마지막으로 읽은 두 응답의 source 를 한 줄로 찍는다
rollup_state() {
  echo "Gateway $(p56_tool settled "$WORK/gw.json" --after 0 || true); MCP Server $(p56_tool settled "$WORK/ms.json" --after 0 || true)"
}

f0_4() {
  local start since end gw_before ms_before gw_after ms_after
  start="$(date +%s)"
  ensure_code_mode F0.4
  # 기준은 이 단계가 시작한 뒤 수집기가 한 번 더 읽은 것까지 센 롤업이다. 앞 단계의 호출이 나중에 셈에 들어 스크립트의
  # 몫으로 보이지 않게 한다
  rolled $((start + COLLECT_SECONDS)) \
    || die F0.4 "the rollup did not settle after the step's start within ${ROLLUP_WAIT}s: $(rollup_state)"
  cp "$WORK/gw.json" "$F0_OUT/f0.4-gateway-before.json"; cp "$WORK/ms.json" "$F0_OUT/f0.4-server-before.json"
  f0_note F0.4 "baseline: $(rollup_state)"
  gw_before="$(json_of "$WORK/gw.json" 'd["totals"]["calls"]')"; ms_before="$(json_of "$WORK/ms.json" 'd["totals"]["calls"]')"
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sleep 2
  F0_TOKEN="$(f1_get token)" p56_tool script --url "$URL" --ip "$LB_IP" --label "f0.4 three inner calls" \
    --code 'a = call_tool("@echo@", text="f0-audit-1")
b = call_tool("@echo@", text="f0-audit-2")
c = call_tool("@big@", kib=1)
return [a, b, len(str(c))]' --out "$F0_OUT/f0.4-script.jsonl" | f0_redact >"$WORK/line"
  end="$(date +%s)"
  f0_note F0.4 "$(cat "$WORK/line")"
  sleep 10
  "${K[@]}" -n "$GW_NS" logs -l "$VMCP_SEL" --all-containers --prefix --tail=-1 --since-time="$since" >"$WORK/vmcp.log" 2>&1 || true
  "${K[@]}" -n "$MS_NS" logs -l "app.kubernetes.io/name=mcpserver,app.kubernetes.io/instance=$MS_K8S" --all-containers \
    --prefix --tail=-1 --since-time="$since" >"$WORK/proxy.log" 2>&1 || true
  f0_tool audit "$WORK/vmcp.log" --label vmcp | f0_redact >"$F0_OUT/f0.4-audit-vmcp.txt"
  f0_tool audit "$WORK/proxy.log" --label proxy | f0_redact >"$F0_OUT/f0.4-audit-proxy.txt"
  f0_block F0.4 "$F0_OUT/f0.4-audit-vmcp.txt"
  f0_block F0.4 "$F0_OUT/f0.4-audit-proxy.txt"
  # 수집기가 스크립트 뒤의 로그를 읽은 뒤에 끝난 롤업 틱까지
  if rolled $((end + COLLECT_SECONDS)); then
    f0_note F0.4 "rollup settled after the script: $(rollup_state)"
  else
    record_fail F0.4 "the rollup did not settle after the script within ${ROLLUP_WAIT}s ($(rollup_state)); the tables below show the last answers"
  fi
  cp "$WORK/gw.json" "$F0_OUT/f0.4-gateway-after.json"; cp "$WORK/ms.json" "$F0_OUT/f0.4-server-after.json"
  gw_after="$(json_of "$WORK/gw.json" 'd["totals"]["calls"]' 2>/dev/null || echo '?')"
  ms_after="$(json_of "$WORK/ms.json" 'd["totals"]["calls"]' 2>/dev/null || echo '?')"
  f0_note F0.4 "Gateway calls $gw_before -> $gw_after, MCP Server calls $ms_before -> $ms_after"
  p56_tool rollup "$F0_OUT/f0.4-gateway-before.json" "$F0_OUT/f0.4-gateway-after.json" >"$F0_OUT/f0.4-rollup-gateway.md"
  p56_tool rollup "$F0_OUT/f0.4-server-before.json" "$F0_OUT/f0.4-server-after.json" >"$F0_OUT/f0.4-rollup-server.md"
  f0_block F0.4 "$F0_OUT/f0.4-rollup-gateway.md"
  f0_block F0.4 "$F0_OUT/f0.4-rollup-server.md"
}

# ---------- F0.5 (런타임 쪽) ----------

# 런타임의 클라이언트가 1분을 넘는 스크립트를 런타임의 기본 기다림(60초)과 6.5절의 75초로 부른다. 스크립트는 slow 25초
# 셋을 차례로 부르므로 ToolHive 의 고정된 1분에 잘린다
f0_5() {
  local secs
  ensure_code_mode F0.5
  for secs in 60 75; do
    F0_TOKEN="$(f1_get token)" p56_runtime --url "$URL" --ip "$LB_IP" --seconds "$secs" \
      --code 'a = call_tool("@slow@", seconds=25)
b = call_tool("@slow@", seconds=25)
c = call_tool("@slow@", seconds=25)
return [a, b, c]' --out "$F0_OUT/f0.5-runtime-${secs}s.json" 2>&1 | f0_redact >"$WORK/line" || true
    f0_note F0.5 "runtime waits ${secs}s: $(tail -n 1 "$WORK/line")"
  done
}

# ---------- F0.6 ----------

# vmcp_pod -> "이름 uid 노드 재시작수 마지막종료" (파드가 하나다)
vmcp_pod() {
  "${K[@]}" -n "$GW_NS" get pod -l "$VMCP_SEL" -o json >"$WORK/pod.json"
  json_of "$WORK/pod.json" '(lambda p: (p["metadata"]["name"], p["metadata"]["uid"], p["spec"]["nodeName"],
    sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses") or []),
    "/".join("%s:%s" % ((c.get("lastState") or {}).get("terminated", {}).get("reason"),
      (c.get("lastState") or {}).get("terminated", {}).get("exitCode")) for c in p["status"].get("containerStatuses") or [])))(
    [p for p in d["items"] if not p["metadata"].get("deletionTimestamp")][0])'
}

# fresh_pod STEP -> vMCP 파드를 새로 띄우고 Traefik 을 거쳐 다시 닿을 때까지 기다린다
fresh_pod() {
  restart_pods "$VMCP_SEL" || die "$1" "restarting the vMCP pod failed"
  route_ready "$1"
  sleep 5
}

# run_case NAME SUGGEST CODE -> 한 경우를 재고 F0.6 결과 줄을 남긴다
run_case() {
  local name="$1" suggest="$2" code="$3" pod uid node restarts last t0 t1 tr ready_after memline others_line
  read -r pod uid node restarts last <<<"$(vmcp_pod)"
  if [ "$restarts" != 0 ]; then
    fresh_pod "F0.6-$name"
    read -r pod uid node restarts last <<<"$(vmcp_pod)"
  fi
  rm -f "$WORK/stop-mem"
  p56_tool sample --node "$node" --uid "$uid" --out "$F0_OUT/f0.6-$name-memory.jsonl" --stop "$WORK/stop-mem" \
    </dev/null >/dev/null 2>&1 8>&- 9>&- &
  local sampler=$!
  sleep 5
  t0="$(f0_now)"
  F0_TOKEN="$(f1_get token)" p56_tool script --url "$URL" --ip "$LB_IP" --label "f0.6 $name" --code "$code" \
    --timeout 90 --out "$F0_OUT/f0.6-scripts.jsonl" | f0_redact >"$WORK/line"
  t1="$(f0_now)"
  if F0_TOKEN="$(f1_get token)" p56_tool ready --url "$URL" --ip "$LB_IP" --since "$t0" --timeout 300 \
    | f0_redact >"$WORK/ready.json"; then
    tr="$(json_of "$WORK/ready.json" 'd["ready_at"]')"; ready_after="$(json_of "$WORK/ready.json" 'd["after"]')s"
  else
    tr="$(f0_now)"; ready_after="not within 300s ($(json_of "$WORK/ready.json" 'd.get("last")'))"
  fi
  sleep 3
  touch "$WORK/stop-mem"
  wait "$sampler" 2>/dev/null || true
  local before_restarts="$restarts"
  read -r pod uid node restarts last <<<"$(vmcp_pod)"
  memline="$(p56_tool memory --samples "$F0_OUT/f0.6-$name-memory.jsonl" --start "$t0" --end "$t1" ${suggest:+--suggest})"
  others_line="$(p56_tool others --loop "$F0_OUT/loop-others.jsonl" --start "$t0" --end "$tr")"
  f0_note F0.6 "$name: $(cat "$WORK/line")"
  f0_note F0.6 "$name: pod $pod restarts $before_restarts -> $restarts (last termination $last); memory $memline; answers a new session again after $ready_after from the script's start; other session: $others_line"
}

f0_6() {
  local first_at
  ensure_code_mode F0.6
  fresh_pod F0.6
  # 다른 사람(f0-code-u2)의 세션이 0.2초마다 새 세션으로 echo 를 직접 부른다. 기록은 F0_OUT/loop-others.jsonl
  F0_TOKEN="$(f1_get token-other)" f0_loop_start "$WORK" others "$URL" echo '{"text": "f0-other"}'
  sleep 10
  first_at="$(f0_now)"
  run_case fanout-sizes 1 'rs = parallel([lambda: call_tool("@big@", kib=1024) for i in range(10)])
return [len(str(r)) for r in rs]'
  run_case fanout-return 1 'return parallel([lambda: call_tool("@big@", kib=1024) for i in range(10)])'
  run_case string-repeat "" 's = "x" * (384 * 1024 * 1024)
return len(s)'
  run_case list-repeat "" 'l = [0] * (32 * 1024 * 1024)
return len(l)'
  run_case string-doubling "" 's = "x" * (1024 * 1024)
for i in range(10):
    s = s + s
return len(s)'
  f0_loop_stop "$WORK" others "$first_at"
  f0_block F0.6 "$F0_OUT/lag-others.txt"
}

# ---------- main ----------

item="${1:-}"
case "$item" in
  setup) setup; pass SETUP "results in $F0_OUT" ;;
  f0.1) connect F0.1; f0_1; pass F0.1 "results in $F0_OUT" ;;
  f0.4) connect F0.4; f0_4; pass F0.4 "results in $F0_OUT" ;;
  f0.5) connect F0.5; f0_5; pass F0.5 "results in $F0_OUT" ;;
  f0.6) connect F0.6; f0_6; pass F0.6 "results in $F0_OUT" ;;
  *) die F0 "usage: p56.sh setup | f0.1 | f0.4 | f0.5 | f0.6" ;;
esac
finish
