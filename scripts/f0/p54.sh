#!/usr/bin/env bash
# P54 F0.1~F0.3 의 측정과 F1 리허설 (docs/plan/phase-54-fewer-proxies.md 12절). .github/workflows/p54-f0.yml 이
# k3d 에서 부른다. 이 파일과 scripts/f0 의 나머지, 그 워크플로는 F0 측정 동안만 둔다.
#
#   scripts/f0/p54.sh f0 [f0.1] [f0.2] [f0.3]    고른 항목을 잰다(없으면 셋 다). 워크플로는 항목마다 한 번씩 불러 한
#                                                항목의 실패가 다른 항목을 막지 않게 한다
#   scripts/f0/p54.sh f1 prepare|watch|check-new|check-old
#                                                F1 리허설의 한 단계. 워크플로가 두 설치를 오가며 부른다
#
# 전제. 태그의 클러스터 e2e 잡 core 조각처럼 만든 k3d(make operator-install·cert-manager-up·deploy-local·lab-up, Traefik
# traefik/traefik)와 mock-oidc(F0.1 의 oidc 행). f0 은 header 모드에서 시작하고 F0.1 의 local·oidc 행만 모드를 바꿨다가
# 되돌린다. 환경: KUBE_CONTEXT(k3d-daegam) F0_OUT F1_STATE(f1 만, 단계 사이의 상태) F0_SLOW(30초 넘는 호출의 초, 35)
# F0_DRAIN(F0.2 가 옛 앞단을 지우기 전에 기다리는 초, 60) F1_HANDOVER_TIMEOUT(check-new 가 앞단이 넘어가기를 기다리는 초,
# 1200) F1_READY_TIMEOUT(check-old 가 프록시 앞단을 기다리는 초, 600)
#
# F0.1  32자 이름의 MCP Server S1 을 f0-s1.e2e.local 로 노출하고(프록시 앞단), 그 앞단의 산 객체로 5절의 vMCP 앞단을 손으로
#       만들어 f0-s1-front.e2e.local 의 Ingress 뒤에 둔다(f0_tool front, 플랫폼의 렌더 함수). header 모드에서 ToolHive 가 만든
#       객체의 이름(vMCP 앞단과 mcp-·gw-·hub-·shared-·expose- CR, 모두 32자 이름)과 두 앞단 파드의 요청량·쉴 때 쓰는 양을
#       찍는다. local 모드(개인 토큰)와 oidc 모드(mock-oidc 토큰)에서 두 앞단을 같은 행(f0_tool probe)으로 재고 견준다. 모드를
#       바꾸면 플랫폼이 프록시 앞단을 다시 렌더하므로 vMCP 앞단은 모드마다 그 렌더에서 다시 만든다
# F0.2  cert-manager 로 노출한 S2 의 vMCP 앞단을 만들고, 앞단 둘을 고르는 NetworkPolicy 하나만 남긴 뒤(집행되는지 먼저 본다)
#       Ingress 의 backend 를 vMCP 앞단으로 바꾼다. 0.2초마다 새 세션으로 호출하며 옛 Service 의 마지막 답과 실패를 찍고,
#       F0_DRAIN 초 뒤 6절 delete 단계의 객체를 지우고 30초 더 부른다. Certificate 의 주인·일련번호와 CertificateRequest 를
#       앞뒤로 견준다. 바꾸기 전에 된 호출이 없거나 인증서가 준비되지 않으면 견주지 않고 멈춘다
# F0.3  S3 에 MCP Python SDK 의 streamable HTTP 세션(f0_tool session)을 열어 두고 Ingress 를 바꾼 뒤 같은 세션으로 다시
#       부른다. 본 오류와 SDK 가 스스로 새 세션을 여는지를 찍는다
# F1    12절의 리허설. prepare 는 이전 판의 설치에서 local 모드(DAEGAM_OAUTH_ALLOW_HTTP=true), 사람 하나, 개인 토큰,
#       TLS none·Secret·cert-manager 노출 셋과 노출마다의 로그인 연결을 만들고 기준값을 적는다. 스크립트 클라이언트가
#       https 노출의 자체 서명 인증서를 믿도록 Traefik 이 내는 두 인증서를 F1_STATE/ca.pem 에 모아 SSL_CERT_FILE 로 준다.
#       watch 는 노출마다 호출 루프를 뒤에서 돌리고 곧 시작할 설치의 시각을 그 판의 바꾼 때로 적는다. check-new 는 B1 의 설치에서 6절의 handover 가 끝나기(vMCP 앞단이 Ingress 에 서고 프록시가
#       지워지기)를 기다린 뒤, check-old 는 이전 판으로 되돌린 설치에서 프록시 앞단이 Ready 가 되기를 기다린 뒤 루프를
#       멈추고 실패한 호출, Tool 이름, 인증서, 개인 토큰의 호출, 로그인 연결의 갱신과 그 뒤 호출, 연결 상태를 찍는다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
. "$ROOT/scripts/lib/e2e_exit.sh"
. "$ROOT/scripts/lib/e2e_json.sh"
CTX="${KUBE_CONTEXT:-k3d-daegam}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-f0-p54.XXXXXX")"
# F0 도구의 포트. lib/e2e_ports.sh 의 표에 없는 자리이고 F0 도구와 함께 사라진다
API_PORT="${E2E_API_PORT:-19090}"; OIDC_PORT="${E2E_OIDC_PORT:-19091}"
TENANT="${F0_TENANT:-f0-front}"
OTHER="${F0_OTHER_TENANT:-f0-front-x}"
MCP_CLIENT_NAME="daegam-f0-p54"
. "$HERE/lib.sh"

SLOW="${F0_SLOW:-35}"
DRAIN="${F0_DRAIN:-60}"
# S 의 backend 타임아웃. 30초 넘는 호출이 서버 안에서 끊기지 않게 SLOW 보다 길다. vMCP 앞단은 5초를 더한다(5절)
SERVER_TIMEOUT=$((SLOW + 25))
ISSUER="${F0_ISSUER:-daegam-selfsigned}"
# 32자 이름 (names.md 2절의 한도). ToolHive 가 이 이름에서 만드는 객체를 본다
S1_NAME="f0-front-name-length-is-32-chars"; G32_NAME="f0-gateway-name-length-32-chars0"
R32_NAME="f0-remote-name-length-32-chars00"; GB_NAME="f0-front-other-tenant-gateway"
S1_HOST="f0-s1.e2e.local"; S1_FRONT_HOST="f0-s1-front.e2e.local"
S2_HOST="f0-s2.e2e.local"; S2_PROBE_HOST="f0-s2-probe.e2e.local"
S3_HOST="f0-s3.e2e.local"; S3_PROBE_HOST="f0-s3-probe.e2e.local"
MCP_GROUP="mcp-$TENANT"; T_GROUP="daegam-$TENANT"
SPIKE_URL="http://mcp-calculator-proxy.${SPIKE_NS}.svc.cluster.local:8080/mcp"
NS=""; S1_ID=""; S1_K8S=""

cleanup() {
  f0_restore_mode
  rm -rf "$WORK"
}
on_exit cleanup

command -v uv >/dev/null || gate_incomplete "uv is not installed (the F0 tool runs in the platform environment)"

# ---------- 공통: S 와 그 vMCP 앞단 ----------

# measured_server STEP NAME DEPLOY_JSON -> MS_ID·MS_K8S·NS. 시험 서버 REST 백엔드의 echo·slow·fail 셋을 내는 MCP Server
# (재는 S)를 저장하고 배포해 Ready 와 노출 앞단을 기다린다
measured_server() {
  local body
  body="{\"name\": \"$2\", \"backends\": [{\"backend_profile_id\": \"$BACKEND_API_ID\", \"spec_version\": $BACKEND_API_VERSION,
    \"tools\": [\"echo\", \"slow\", \"fail\"], \"timeout_seconds\": $SERVER_TIMEOUT}]}"
  save_doc "$1" /mcp-servers "$2" "$body"
  MS_ID="$SAVED_ID"
  deploy_ready "$1" /mcp-servers "$MS_ID" "$3"
  MS_K8S="$(jq_ 'd["k8s"]["name"]')"; NS="$(jq_ 'd["k8s"]["namespace"]')"
  f0_exposed "$1" "/mcp-servers/$MS_ID"
}

# exposure_tls HOST TLS_JSON -> 노출 배포 본문. lib/e2e_mcp_access.sh 의 exposure 에서 tls 만 다르다
exposure_tls() {
  echo "{\"exposure\": {\"enabled\": true, \"host\": \"$1\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": $2}}"
}
CERT_MANAGER_TLS="{\"mode\": \"cert-manager\", \"issuer\": \"$ISSUER\"}"

# cert_ready STEP MS_ID -> cert-manager 가 Certificate 를 내어 <k8s.name>-tls 를 만들 때까지 (e2e-gateway STEP 16 과 같다).
# 아니면 die STEP. 인증서 없이 견주면 앞뒤가 같은 "없음" 이라 바뀌지 않은 것으로 읽힌다
cert_ready() {
  local i
  for ((i = 0; i < 36; i++)); do
    [ "$(api GET "/mcp-servers/$2")" = 200 ] \
      && [ "$(jq_ '(d["status"].get("exposure") or {}).get("certificate_ready")')" = True ] && return 0
    sleep 5
  done
  die "$1" "certificate_ready never became true: $(jq_ 'd["status"].get("exposure")' 2>/dev/null || true)"
}

# vmcp_front STEP K8S -> S 의 산 프록시 앞단에서 vMCP 앞단(front-<k8s>)을 만들어 적용하고 그 Deployment 가 Ready 일 때까지
# 기다린다. MCPAuthzConfig 가 있으면 Valid 를 먼저 기다린다(5절). 다시 부르면 지금 앞단의 렌더로 덮는다
vmcp_front() {
  local step="$1" k8s="$2" front="front-$2" authz=() ref i valid=""
  "${K[@]}" -n "$NS" get mcpserver "$k8s" -o json >"$WORK/s.json" || die "$step" "MCPServer $k8s missing"
  "${K[@]}" -n "$NS" get mcpremoteproxy "expose-$k8s" -o json >"$WORK/p.json" || die "$step" "front MCPRemoteProxy expose-$k8s missing"
  ref="$(json_of "$WORK/p.json" '(d["spec"].get("authzConfigRef") or {}).get("name") or ""')"
  if [ -n "$ref" ]; then
    "${K[@]}" -n "$NS" get mcpauthzconfig "$ref" -o json >"$WORK/a.json" || die "$step" "MCPAuthzConfig $ref missing"
    authz=(--authz "$WORK/a.json")
  fi
  f0_tool front --proxy "$WORK/p.json" --server "$WORK/s.json" ${authz[@]+"${authz[@]}"} --timeout "$SERVER_TIMEOUT" \
    >"$F0_OUT/front-$k8s.json" || die "$step" "rendering the vMCP front failed"
  "${K[@]}" apply --server-side --force-conflicts --field-manager=daegam-f0 -f "$F0_OUT/front-$k8s.json" >/dev/null \
    || die "$step" "applying the vMCP front of $k8s failed"
  if [ -n "$ref" ]; then
    for ((i = 0; i < 60; i++)); do
      valid="$("${K[@]}" -n "$NS" get mcpauthzconfig "$front-authz" \
        -o jsonpath='{.status.conditions[?(@.type=="Valid")].status}' 2>/dev/null || true)"
      [ "$valid" = True ] && break
      sleep 2
    done
    [ "$valid" = True ] || die "$step" "MCPAuthzConfig $front-authz is not Valid"
  fi
  for ((i = 0; i < 60; i++)); do
    "${K[@]}" -n "$NS" get deploy "$front" -o name >/dev/null 2>&1 && break
    sleep 2
  done
  "${K[@]}" -n "$NS" rollout status "deploy/$front" --timeout=300s >/dev/null \
    || die "$step" "the vMCP front Deployment $front did not become ready"
}

# copy_expose_policy K8S NAME SELECTOR_JSON -> 플랫폼의 노출 정책(daegam-expose-<k8s>)과 같은 출처(Traefik)를 받되
# podSelector 만 다른 NetworkPolicy NAME. 플랫폼이 다시 렌더해도 건드리지 않는 이름이다
copy_expose_policy() {
  "${K[@]}" -n "$NS" get networkpolicy "daegam-expose-$1" -o json >"$WORK/np.json" || return 1
  python3 - "$WORK/np.json" "$2" "$3" <<'PY' | "${K[@]}" apply -f - >/dev/null
import json, sys
src, name, sel = json.load(open(sys.argv[1])), sys.argv[2], json.loads(sys.argv[3])
print(json.dumps({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
    "metadata": {"name": name, "namespace": src["metadata"]["namespace"]},
    "spec": {"podSelector": sel, "policyTypes": ["Ingress"], "ingress": src["spec"].get("ingress") or []}}))
PY
}

# front_policy K8S -> vMCP 앞단 파드만 받는 정책 f0-front-<k8s>
front_policy() {
  copy_expose_policy "$1" "f0-front-$1" \
    "{\"matchLabels\": {\"app.kubernetes.io/name\": \"virtualmcpserver\", \"app.kubernetes.io/instance\": \"front-$1\"}}"
}

# both_policy K8S -> 5절의 모양 그대로 앞단 두 kind 를 고르는 정책 f0-expose-both-<k8s>
both_policy() {
  copy_expose_policy "$1" "f0-expose-both-$1" "{\"matchExpressions\": [
    {\"key\": \"app.kubernetes.io/name\", \"operator\": \"In\", \"values\": [\"mcpremoteproxy\", \"virtualmcpserver\"]},
    {\"key\": \"app.kubernetes.io/instance\", \"operator\": \"In\", \"values\": [\"expose-$1\", \"front-$1\"]}]}"
}

# front_ingress K8S NAME HOST -> 플랫폼의 노출 Ingress 를 본떠 host 와 backend(vMCP 앞단 Service:4483)를 바꾼 Ingress NAME.
# TLS 와 어노테이션은 일부러 옮기지 않는다. cert-manager 어노테이션을 옮기면 Certificate 가 하나 더 생긴다
front_ingress() {
  "${K[@]}" -n "$NS" get ingress "$1" -o json >"$WORK/ing.json" || return 1
  python3 - "$WORK/ing.json" "$2" "$3" "vmcp-front-$1" <<'PY' | "${K[@]}" apply -f - >/dev/null
import json, sys
src, name, host, svc = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3], sys.argv[4]
rule = src["spec"]["rules"][0]
paths = [{"path": p["path"], "pathType": p["pathType"],
          "backend": {"service": {"name": svc, "port": {"number": 4483}}}} for p in rule["http"]["paths"]]
print(json.dumps({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
    "metadata": {"name": name, "namespace": src["metadata"]["namespace"]},
    "spec": {"ingressClassName": src["spec"]["ingressClassName"], "rules": [{"host": host, "http": {"paths": paths}}]}}))
PY
}

# switch_ingress K8S SERVICE PORT -> 노출 Ingress 의 모든 경로의 backend 를 SERVICE:PORT 로 바꾼다(6절 switch). 바꾼 때를
# 어노테이션에 적는다
switch_ingress() {
  local patch
  patch="$("${K[@]}" -n "$NS" get ingress "$1" -o json | python3 -c 'import json, sys
d, svc, port = json.load(sys.stdin), sys.argv[1], int(sys.argv[2])
ops = []
for r, rule in enumerate(d["spec"]["rules"]):
    for p, _ in enumerate(rule["http"]["paths"]):
        ops.append({"op": "replace", "path": f"/spec/rules/{r}/http/paths/{p}/backend",
                    "value": {"service": {"name": svc, "port": {"number": port}}}})
print(json.dumps(ops))' "$2" "$3")" || return 1
  "${K[@]}" -n "$NS" patch ingress "$1" --type=json -p "$patch" >/dev/null || return 1
  "${K[@]}" -n "$NS" annotate ingress "$1" --overwrite "daegam.devops.ai.kr/f0-switched-at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
}

# ingress_backend K8S -> 노출 Ingress 의 첫 경로가 가리키는 Service 이름
ingress_backend() {
  "${K[@]}" -n "$NS" get ingress "$1" -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}' 2>/dev/null
}

# server_name URL -> Traefik 을 거친 initialize 가 답한 serverInfo.name. 답하지 않으면 빈 줄
server_name() {
  local code
  code="$(rpc_curl "$1" "$(pin "$1" "$LB_IP")" -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"initialize\", \"params\": $INIT_PARAMS}")"
  [ "$code" = 200 ] || return 0
  rpc_extract
  rpc_ '((d or {}).get("result") or {}).get("serverInfo", {}).get("name") or ""' 2>/dev/null || true
}

# cert_state K8S HOST -> Certificate <k8s>-tls 의 주인(kind/name/uid), revision, 끝나는 때, Traefik 이 내는 인증서의
# 일련번호, CertificateRequest 이름을 한 줄씩 찍는다. Secret 은 읽지 않는다
cert_state() {
  local serial
  "${K[@]}" -n "$NS" get certificate "$1-tls" -o json >"$WORK/cert.json" || { echo "certificate $1-tls missing"; return 0; }
  python3 - "$WORK/cert.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
owners = ",".join("%s/%s/%s" % (o["kind"], o["name"], o["uid"]) for o in d["metadata"].get("ownerReferences") or [])
st = d.get("status") or {}
print("owner", owners or "-")
print("revision", st.get("revision"))
print("notAfter", st.get("notAfter"))
PY
  serial="$(openssl s_client -connect "$LB_IP:443" -servername "$2" </dev/null 2>/dev/null | openssl x509 -noout -serial 2>/dev/null || true)"
  echo "served ${serial:-none}"
  echo "requests $("${K[@]}" -n "$NS" get certificaterequests -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null)"
}

# ---------- F0.1 ----------

f0_1_header() {
  local s1_url front_url pub_id g32_id r32_id gb_id gb_ns r32_k8s g32_k8s gb_k8s code shared cr kind ns name
  measured_server "F0.1" "$S1_NAME" "$(exposure "$S1_HOST")"
  S1_ID="$MS_ID"; S1_K8S="$MS_K8S"
  vmcp_front "F0.1" "$S1_K8S"
  front_policy "$S1_K8S" || die F0.1 "the NetworkPolicy for the vMCP front of $S1_K8S failed"
  front_ingress "$S1_K8S" "f0-front-$S1_K8S" "$S1_FRONT_HOST" || die F0.1 "the Ingress of the vMCP front failed"
  s1_url="http://$S1_HOST/mcp"; front_url="http://$S1_FRONT_HOST/mcp"
  f0_note F0.1 "S1 $S1_K8S (name ${#S1_NAME} chars) in $NS: proxy front expose-$S1_K8S at $s1_url, vMCP front front-$S1_K8S at $front_url"

  # 다른 kind 의 32자 CR: Gateway(gw-), remote 항목(hub-), 다른 테넌트의 Gateway 가 쓰는 remote 항목의 복제본(shared-)
  code="$(api POST "/mcp-servers/$S1_ID/publish" '{}')"
  case "$code" in 200 | 201) pub_id="$(jq_ 'd["id"]')" ;; *) die F0.1 "publish $S1_NAME -> $code" ;; esac
  save_doc F0.1 /gateways "$G32_NAME" "{\"name\": \"$G32_NAME\", \"backends\": [{\"hub_item_id\": \"$pub_id\"}]}"
  g32_id="$SAVED_ID"; deploy_ready F0.1 /gateways "$g32_id"; g32_k8s="$(jq_ 'd["k8s"]["name"]')"
  hub_item F0.1 "$R32_NAME" remote "$SPIKE_URL" streamable-http "$(share_with "$OTHER")"
  [ -n "$ITEM_ID" ] || die F0.1 "POST /hub $R32_NAME -> $ITEM_CODE"
  [ "$ITEM_PHASE" = Ready ] || die F0.1 "the remote item $R32_NAME is $ITEM_PHASE, not Ready"
  r32_id="$ITEM_ID"
  [ "$(api GET "/hub/$r32_id")" = 200 ] || die F0.1 "GET /hub/$r32_id failed"
  r32_k8s="$(jq_ 'd["k8s"]["name"]')"
  TENANT="$OTHER" save_doc F0.1 /gateways "$GB_NAME" "{\"name\": \"$GB_NAME\", \"backends\": [{\"hub_item_id\": \"$r32_id\"}]}"
  gb_id="$SAVED_ID"
  TENANT="$OTHER" deploy_ready F0.1 /gateways "$gb_id"
  gb_k8s="$(jq_ 'd["k8s"]["name"]')"; gb_ns="$(jq_ 'd["k8s"]["namespace"]')"
  shared="$("${K[@]}" -n "$gb_ns" get mcpremoteproxy -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep '^shared-' | head -1 || true)"
  [ -n "$shared" ] || die F0.1 "no shared- clone in $gb_ns for $R32_NAME"
  : >"$WORK/crs.json"
  for cr in "mcpserver $NS $S1_K8S" "mcpremoteproxy $NS expose-$S1_K8S" "virtualmcpserver $NS front-$S1_K8S" \
    "virtualmcpserver $NS $g32_k8s" "mcpremoteproxy $NS $r32_k8s" "mcpremoteproxy $gb_ns $shared" "virtualmcpserver $gb_ns $gb_k8s"; do
    read -r kind ns name <<<"$cr"
    "${K[@]}" -n "$ns" get "$kind" "$name" -o json >>"$WORK/crs.json" || die F0.1 "$kind $ns/$name missing"
  done
  "${K[@]}" get deploy,statefulset,service,serviceaccount,role,rolebinding,configmap -n "$NS" -o json >"$WORK/objs.json"
  "${K[@]}" get deploy,statefulset,service,serviceaccount,role,rolebinding,configmap -n "$gb_ns" -o json >>"$WORK/objs.json"
  f0_tool derived --crs "$WORK/crs.json" --objects "$WORK/objs.json" >"$F0_OUT/f0.1-derived-names.md"
  f0_block F0.1 "$F0_OUT/f0.1-derived-names.md"

  # 쉴 때 쓰는 양. metrics-server 가 두 번 이상 긁도록 90초 쉰 뒤 본다
  sleep 90
  {
    echo "requests (pod container cpu memory):"
    "${K[@]}" -n "$NS" get pods -l "app.kubernetes.io/instance in (expose-$S1_K8S,front-$S1_K8S)" \
      -o jsonpath='{range .items[*]}{.metadata.name}{range .spec.containers[*]}{" "}{.name}{" "}{.resources.requests.cpu}{" "}{.resources.requests.memory}{end}{"\n"}{end}'
    echo "idle use (kubectl top, 90 s after the last call):"
    "${K[@]}" -n "$NS" top pod --containers -l "app.kubernetes.io/instance in (expose-$S1_K8S,front-$S1_K8S)" 2>&1 || true
  } >"$F0_OUT/f0.1-pod-use.txt"
  f0_block F0.1 "$F0_OUT/f0.1-pod-use.txt"
}

# f0_1_rows MODE -> 두 앞단을 같은 행으로 재고 견준다. F0_TOKEN·F0_REFUSED_TOKEN 은 부르는 쪽이 환경으로 준다
f0_1_rows() {
  local mode="$1"
  f0_tool probe --url "http://$S1_HOST/mcp" --ip "$LB_IP" --slow "$SLOW" --out "$F0_OUT/f0.1-$mode-proxy.json" \
    || die "F0.1-$mode" "probe of the proxy front failed"
  f0_tool probe --url "http://$S1_FRONT_HOST/mcp" --ip "$LB_IP" --slow "$SLOW" --out "$F0_OUT/f0.1-$mode-vmcp.json" \
    || die "F0.1-$mode" "probe of the vMCP front failed"
  local rc=0
  f0_tool compare "$F0_OUT/f0.1-$mode-proxy.json" "$F0_OUT/f0.1-$mode-vmcp.json" --names proxy vmcp \
    >"$F0_OUT/f0.1-$mode-compare.md" || rc=$?
  f0_block "F0.1 $mode" "$F0_OUT/f0.1-$mode-compare.md"
  # 프록시 앞단이 허용 토큰으로 답하지 않았으면 두 앞단이 같이 실패해도 "같다" 로 읽히므로 멈춘다
  [ "$rc" = 0 ] || die "F0.1-$mode" "the proxy front did not answer the allowed token (BASELINE INVALID above)"
  # vMCP 앞단이 Tool 을 프록시 앞단과 다르게 알리면 Tool 에 기대는 행(목록 항목, 호출, 30초 넘는 호출, 거절된 호출)은 앞단을
  # 재지 못한 것이다. 그 원인을 읽을 진단을 남기고 실패로 센다. 다른 모드의 행은 이어서 잰다
  local want got
  want="$(tool_names "$F0_OUT/f0.1-$mode-proxy.json")"
  got="$(tool_names "$F0_OUT/f0.1-$mode-vmcp.json")"
  if [ "$got" != "$want" ]; then
    vmcp_diag "F0.1 $mode" "$NS" "front-$S1_K8S" "app.kubernetes.io/instance=$S1_K8S"
    record_fail "F0.1-$mode" "the vMCP front lists [$got], the proxy front [$want]; see diag-front-$S1_K8S.txt"
  fi
}

# rebuild_front STEP -> 모드를 바꾼 뒤 플랫폼이 다시 렌더한 프록시 앞단을 기다리고 그것으로 vMCP 앞단을 다시 만든다.
# 두 앞단의 파드를 새로 띄워 새 JWKS 를 받게 한다(개인 토큰의 키, lib/e2e_personal_tokens.sh)
rebuild_front() {
  f0_exposed "$1" "/mcp-servers/$S1_ID"
  vmcp_front "$1" "$S1_K8S"
  restart_pods "app.kubernetes.io/name=mcpremoteproxy,app.kubernetes.io/instance=expose-$S1_K8S" \
    "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=front-$S1_K8S" \
    || die "$1" "restarting the front pods failed"
}

f0_1_local() {
  local u1 u2 c1 c2 tok1 tok2
  # 모드를 바꾸면 플랫폼 파드가 바뀐다. 옛 파드에 붙은 port-forward 를 닫고 local_admin_session 이 새로 연다
  stop_pf "$PF_API" "$API_PORT"; PF_API=""
  switch_auth_mode local || die F0.1-local "switching to local mode failed"
  MODE_SWITCHED=1
  local_admin_session F0.1-local
  u1="$(local_fresh_member f0-front-u1 "$TENANT")" || die F0.1-local "creating the member of $TENANT failed"
  u2="$(local_fresh_member f0-front-u2 "$OTHER")" || die F0.1-local "creating the member of $OTHER failed"
  c1="$(local_login_any f0-front-u1 "$LOCAL_PW")"; [ -n "$c1" ] || die F0.1-local "login as f0-front-u1 failed"
  c2="$(local_login_any f0-front-u2 "$LOCAL_PW")"; [ -n "$c2" ] || die F0.1-local "login as f0-front-u2 failed"
  issue_ok F0.1-local "$c1" "$TENANT" "f0 front"; tok1="$ISSUED"
  issue_ok F0.1-local "$c2" "$OTHER" "f0 front other"; tok2="$ISSUED"
  # 발급한 키가 JWKS 캐시에 오른 뒤 두 앞단의 파드를 새로 띄운다
  sleep "$JWKS_CACHE_WAIT"
  rebuild_front F0.1-local
  f0_note F0.1 "local mode: personal token of a $TENANT member (allowed, groups [$T_GROUP]) and of a $OTHER member (refused)"
  F0_TOKEN="$tok1" F0_REFUSED_TOKEN="$tok2" f0_1_rows local
  api_delete "/users/$u1"; api_delete "/users/$u2"
  f0_restore_mode
}

f0_1_oidc() {
  local tok1 tok2 i seen=""
  f0_oidc_session F0.1-oidc
  # 테넌트 그룹이 프록시 앞단의 Cedar 에 오를 때까지 기다린다. 오르지 않으면 허용 토큰이 두 앞단에서 함께 거부되어
  # 견줌이 "같다" 로 읽히므로 멈춘다
  for ((i = 0; i < 60; i++)); do
    if "${K[@]}" -n "$NS" get mcpauthzconfig "expose-$S1_K8S-authz" -o json 2>/dev/null \
      | grep -qF "THVGroup::\\\"$MCP_GROUP\\\""; then
      seen=1; break
    fi
    sleep 5
  done
  [ -n "$seen" ] || die F0.1-oidc "the group $MCP_GROUP never reached MCPAuthzConfig expose-$S1_K8S-authz"
  rebuild_front F0.1-oidc
  tok1="$(user_token "{\"sub\": \"f0-member\", \"groups\": [\"$MCP_GROUP\"]}")" || die F0.1-oidc "member token failed"
  tok2="$(user_token '{"sub": "f0-intruder", "groups": ["mcp-f0-other"]}')" || die F0.1-oidc "intruder token failed"
  f0_note F0.1 "oidc mode: mock-oidc token with groups [$MCP_GROUP] (allowed) and [mcp-f0-other] (refused)"
  F0_TOKEN="$tok1" F0_REFUSED_TOKEN="$tok2" f0_1_rows oidc
  f0_restore_mode
}

# front_lists_echo STEP HOST OUT VMCP -> 시험 Ingress HOST 로 vMCP 앞단(NS 의 VMCP)이 echo 를 알릴 때까지 5초 간격으로
# 12번 본다. 마지막 목록을 FRONT_TOOLS 에 남기고 echo 가 있으면 0 이다. 새 vMCP 파드의 첫 상태 확인이 실패하면 ToolHive
# 0.53 은 그 백엔드를 다음 확인(30초 뒤)까지 라우팅하지 않으므로, 바꾸기 전에 백엔드가 라우팅될 때까지 기다린다. 첫 번째와
# echo 가 보인 때(또는 마지막)의 status.discoveredBackends 를 결과 줄로 남긴다. B1 의 바꾸기 조건(healthy·degraded)이
# 실제 라우팅과 맞는지 그 줄로 본다
front_lists_echo() {
  local step="$1" host="$2" out="$3" vmcp="$4" i
  FRONT_TOOLS=""
  for ((i = 1; i <= 12; i++)); do
    # 지난 시도의 파일이 남아 있으면 이번 시도가 실패해도 그 목록을 읽는다
    rm -f "$out"
    f0_tool probe --url "http://$host/mcp" --ip "$LB_IP" --rows session --out "$out" >/dev/null 2>&1 || true
    FRONT_TOOLS="$(tool_names "$out")"
    case " $FRONT_TOOLS " in
      *" echo "*)
        f0_note "$step" "try $i of 12: vMCP front lists [$FRONT_TOOLS]; status.discoveredBackends $(front_backends "$vmcp")"
        return 0 ;;
    esac
    [ "$i" != 1 ] \
      || f0_note "$step" "try 1 of 12: vMCP front lists [$FRONT_TOOLS]; status.discoveredBackends $(front_backends "$vmcp")"
    sleep 5
  done
  f0_note "$step" "try 12 of 12: vMCP front lists [$FRONT_TOOLS]; status.discoveredBackends $(front_backends "$vmcp")"
  return 1
}

# front_backends VMCP -> NS 의 VirtualMCPServer VMCP 의 status.discoveredBackends 를 "이름=상태" 로 찍는다
front_backends() {
  "${K[@]}" -n "$NS" get virtualmcpserver "$1" \
    -o jsonpath='{range .status.discoveredBackends[*]}{.name}={.status} {end}' 2>/dev/null || echo "unreadable"
}

# ---------- F0.2 ----------

f0_2() {
  local k8s url code_blocked code_open code_proxy switch_at deleted_at proxy_svc policy rc=0 i name_old name_new
  local dropped cut since
  measured_server F0.2 f0-handover "$(exposure_tls "$S2_HOST" "$CERT_MANAGER_TLS")"
  k8s="$MS_K8S"; url="https://$S2_HOST/mcp"
  cert_ready F0.2 "$MS_ID"
  vmcp_front F0.2 "$k8s"
  proxy_svc="$(ingress_backend "$k8s")"
  # 집행 확인: vMCP 앞단만 가리키는 시험 Ingress 는 플랫폼의 정책(프록시만 고른다) 아래에서 닿지 않아야 하고, 두 kind 를
  # 고르는 정책을 둔 뒤 닿아야 한다. 그 뒤 플랫폼의 정책을 지우고 그 정책 하나로 프록시도 닿는지 본다
  front_ingress "$k8s" "f0-probe-$k8s" "$S2_PROBE_HOST" || die F0.2 "the probe Ingress failed"
  # Traefik 이 시험 host 의 라우트를 집을 때까지(404 가 아닐 때까지) 기다린다. 404 를 막힌 것으로 읽지 않는다
  for ((i = 0; i < 24; i++)); do
    code_blocked="$(traefik_status "http://$S2_PROBE_HOST/mcp")"
    [ "$code_blocked" != 404 ] && break
    sleep 5
  done
  [ "$code_blocked" != 404 ] || die F0.2 "Traefik never routed $S2_PROBE_HOST"
  [ "$code_blocked" != 200 ] \
    || die F0.2 "the vMCP front answered through Traefik under the platform's proxy-only policy, so NetworkPolicy is not enforced"
  both_policy "$k8s" || die F0.2 "the both-kinds NetworkPolicy failed"
  code_open="$(traefik_status "http://$S2_PROBE_HOST/mcp" 24)"
  # 옛 앞단과 새 앞단은 initialize 의 serverInfo.name 으로 가른다(loop, lag). 둘이 같으면 라우팅 지연을 잴 수 없다
  name_new="$(server_name "http://$S2_PROBE_HOST/mcp")"
  # 두 kind 를 고르는 정책 아래에서도 시험 Ingress 로 닿지 않으면 아래의 목록 확인이 아무것도 읽지 못한다
  [ "$code_open" = 200 ] \
    || die F0.2 "the vMCP front does not answer through Traefik with the both-kinds policy (proxy-only policy -> $code_blocked, both-kinds policy -> $code_open)"
  # 루프가 부르는 echo 를 vMCP 앞단이 바꾸기 전에 알려야 바꾼 뒤의 실패를 넘김의 실패로 읽을 수 있다
  if ! front_lists_echo F0.2 "$S2_PROBE_HOST" "$F0_OUT/f0.2-front-before.json" "front-$k8s"; then
    vmcp_diag F0.2 "$NS" "front-$k8s" "app.kubernetes.io/instance=$k8s"
    die F0.2 "the vMCP front does not list echo in 12 tries before the switch, so the handover cannot be measured; see diag-front-$k8s.txt"
  fi
  policy="$("${K[@]}" -n "$NS" get networkpolicy "f0-expose-both-$k8s" -o jsonpath='{.spec.podSelector}')"
  "${K[@]}" -n "$NS" delete networkpolicy "daegam-expose-$k8s" >/dev/null
  sleep 5
  code_proxy="$(traefik_status "$url" 24)"
  "${K[@]}" -n "$NS" delete ingress "f0-probe-$k8s" >/dev/null
  f0_note F0.2 "enforcement: vMCP front through Traefik under the platform's proxy-only policy -> $code_blocked; with the both-kinds policy -> $code_open; proxy front with only the both-kinds policy -> $code_proxy; selector $policy"
  [ "$code_proxy" = 200 ] || die F0.2 "the proxy front does not answer through Traefik ($code_proxy), so the handover cannot be measured"
  name_old="$(server_name "$url")"
  f0_note F0.2 "serverInfo.name: proxy front '$name_old', vMCP front '$name_new'"
  { [ -n "$name_old" ] && [ -n "$name_new" ] && [ "$name_old" != "$name_new" ]; } \
    || die F0.2 "the two fronts cannot be told apart by serverInfo.name ('$name_old', '$name_new')"

  cert_state "$k8s" "$S2_HOST" >"$F0_OUT/f0.2-cert-before.txt"
  { grep -q '^served serial=' "$F0_OUT/f0.2-cert-before.txt" && grep -qE '^revision [0-9]+$' "$F0_OUT/f0.2-cert-before.txt"; } \
    || die F0.2 "no issued certificate for $S2_HOST to compare: $(tr '\n' ' ' <"$F0_OUT/f0.2-cert-before.txt")"
  f0_loop_start "$WORK" f0.2 "$url" echo '{"text": "f0-loop"}'
  sleep 15
  switch_at="$(f0_now)"
  switch_ingress "$k8s" "vmcp-front-$k8s" 4483 || die F0.2 "patching the Ingress backend failed"
  f0_note F0.2 "Ingress $k8s backend $proxy_svc:8080 -> vmcp-front-$k8s:4483 at $switch_at"
  sleep "$DRAIN"
  deleted_at="$(f0_now)"
  # 6절 delete: 프록시 앞단, 그 MCPAuthzConfig, bearer 모드 MCPExternalAuthConfig. Secret 은 둔다
  "${K[@]}" -n "$NS" delete mcpremoteproxy "expose-$k8s" --wait=false >/dev/null || true
  "${K[@]}" -n "$NS" delete mcpauthzconfig "expose-$k8s-authz" --ignore-not-found --wait=false >/dev/null || true
  "${K[@]}" -n "$NS" delete mcpexternalauthconfig "svc-expose-$k8s" --ignore-not-found --wait=false >/dev/null || true
  sleep 30
  f0_loop_stop "$WORK" f0.2 "$switch_at" "$deleted_at" || rc=$?
  f0_block F0.2 "$F0_OUT/lag-f0.2.txt"
  [ "$rc" != 3 ] || die F0.2 "the call loop has no successful call before the switch (BASELINE INVALID above)"
  [ "$rc" = 0 ] || die F0.2 "the lag summary of the call loop failed (exit $rc), so the switch cannot be judged"
  # 12절의 막대(404, 503, 끊긴 호출)는 바꾼 뒤에 시작해 새 앞단에서 연 세션의 실패만 본다(f0_tool lag 의 dropped).
  # 바꾸기 전의 실패와 바꾸기에 걸친 세션의 404 는 F0.3 과 릴리스 노트의 몫이다. 하나라도 있으면 진단을 남기고 실패로 센다
  dropped="$(sed -n 's/^dropped calls after the switch: \([0-9][0-9]*\).*/\1/p' "$F0_OUT/lag-f0.2.txt" | head -n 1)"
  [ -n "$dropped" ] || die F0.2 "lag-f0.2.txt has no 'dropped calls after the switch' line"
  if [ "$dropped" != 0 ]; then
    vmcp_diag F0.2 "$NS" "front-$k8s" "app.kubernetes.io/instance=$k8s"
    record_fail F0.2 "$dropped calls dropped after the switch (lag-f0.2.txt)"
  fi
  # 바꾼 때 옛 앞단에서 연 세션의 호출이 답을 받지 못했거나 5xx 를 받은 것도 막대가 보는 끊긴 호출이다(lag 의 cut).
  # 옛 앞단 쪽의 실패라 vMCP 진단 대신 Traefik 의 로그 끝을 남긴다
  cut="$(sed -n 's/^calls cut at the switch: \([0-9][0-9]*\).*/\1/p' "$F0_OUT/lag-f0.2.txt" | head -n 1)"
  [ -n "$cut" ] || die F0.2 "lag-f0.2.txt has no 'calls cut at the switch' line"
  if [ "$cut" != 0 ]; then
    # 워크플로가 Traefik 의 접근 로그를 켠다(머리는 빼고). 바꾸기 5초 전부터 300줄로, 바꾼 때 앞뒤의 요청이 어느 백엔드로
    # 갔고 어떻게 끝났는지 본다. 루프는 세션마다 요청을 몇 개씩 보내므로 로그 끝만 보면 바꾼 때가 빠진다
    since="$(python3 -c 'import datetime, sys
print(datetime.datetime.fromtimestamp(float(sys.argv[1]) - 5, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$switch_at")"
    "${K[@]}" -n "$TRAEFIK_NS" logs "deploy/$TRAEFIK_SVC" --since-time="$since" 2>&1 | head -n 300 | f0_redact \
      >"$F0_OUT/diag-traefik.txt" || true
    f0_block F0.2 "$F0_OUT/diag-traefik.txt"
    record_fail F0.2 "$cut calls cut at the switch (lag-f0.2.txt, diag-traefik.txt)"
  fi
  cert_state "$k8s" "$S2_HOST" >"$F0_OUT/f0.2-cert-after.txt"
  f0_block "F0.2 before" "$F0_OUT/f0.2-cert-before.txt"
  f0_block "F0.2 after" "$F0_OUT/f0.2-cert-after.txt"
  if diff -q "$F0_OUT/f0.2-cert-before.txt" "$F0_OUT/f0.2-cert-after.txt" >/dev/null; then
    f0_note F0.2 "Certificate $k8s-tls: owner, revision, serial and CertificateRequests unchanged across the switch and the deletion"
  else
    f0_note F0.2 "Certificate $k8s-tls CHANGED: diff $(diff "$F0_OUT/f0.2-cert-before.txt" "$F0_OUT/f0.2-cert-after.txt" | tr '\n' ' ')"
  fi
}

# traefik_status URL [TRIES] -> 토큰 없는 initialize 가 Traefik 에서 받은 HTTP 코드. TRIES 번(5초 간격) 200 이 될 때까지
# 본다
traefik_status() {
  local code i tries="${2:-1}"
  for ((i = 0; i < tries; i++)); do
    code="$(rpc_curl "$1" "$(pin "$1" "$LB_IP")" --max-time 8 -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"initialize\", \"params\": $INIT_PARAMS}")"
    [ "$code" = 200 ] && break
    [ "$i" -lt $((tries - 1)) ] && sleep 5
  done
  echo "$code"
}

# ---------- F0.3 ----------

f0_3() {
  local k8s url pid old new="" name="" i
  measured_server F0.3 f0-session "$(exposure "$S3_HOST")"
  k8s="$MS_K8S"; url="http://$S3_HOST/mcp"
  vmcp_front F0.3 "$k8s"
  front_policy "$k8s" || die F0.3 "the NetworkPolicy for the vMCP front failed"
  old="$(server_name "$url")"
  [ -n "$old" ] || die F0.3 "the proxy front of $url does not answer initialize through Traefik"
  # 새 앞단의 serverInfo.name 을 시험 Ingress 로 먼저 읽는다. 바꾼 뒤 그 이름이 답하면 새 앞단이 받는 것이다
  front_ingress "$k8s" "f0-probe-$k8s" "$S3_PROBE_HOST" || die F0.3 "the probe Ingress failed"
  for ((i = 0; i < 24; i++)); do
    new="$(server_name "http://$S3_PROBE_HOST/mcp")"
    [ -n "$new" ] && break
    sleep 5
  done
  # 새 세션의 호출은 vMCP 앞단이 백엔드를 라우팅할 때만 SDK 의 동작을 잰다. F0.2 처럼 바꾸기 전에 echo 를 기다린다
  if ! front_lists_echo F0.3 "$S3_PROBE_HOST" "$F0_OUT/f0.3-front-before.json" "front-$k8s"; then
    vmcp_diag F0.3 "$NS" "front-$k8s" "app.kubernetes.io/instance=$k8s"
    die F0.3 "the vMCP front does not list echo in 12 tries before the switch, so the new session cannot be measured; see diag-front-$k8s.txt"
  fi
  "${K[@]}" -n "$NS" delete ingress "f0-probe-$k8s" >/dev/null
  f0_note F0.3 "serverInfo.name: proxy front '$old', vMCP front '$new'"
  { [ -n "$new" ] && [ "$new" != "$old" ]; } \
    || die F0.3 "the two fronts cannot be told apart by serverInfo.name ('$old', '${new:-no answer}')"
  rm -f "$WORK/ready" "$WORK/go"
  "${LAB_PY[@]}" "$HERE/f0_tool.py" session --url "$url" --ip "$LB_IP" --ready "$WORK/ready" --go "$WORK/go" \
    --out "$F0_OUT/f0.3-session.json" >"$F0_OUT/f0.3-session.txt" 2>&1 &
  pid=$!
  for ((i = 0; i < 120; i++)); do [ -e "$WORK/ready" ] && break; sleep 0.5; done
  if [ ! -e "$WORK/ready" ]; then
    kill "$pid" 2>/dev/null || true
    die F0.3 "the SDK session did not open within 60s: $(tr '\n' ' ' <"$F0_OUT/f0.3-session.txt")"
  fi
  switch_ingress "$k8s" "vmcp-front-$k8s" 4483 || die F0.3 "patching the Ingress backend failed"
  for ((i = 0; i < 120; i++)); do
    name="$(server_name "$url")"
    [ "$name" = "$new" ] && break
    sleep 0.5
  done
  # 새 앞단이 답하기 전에 같은 세션으로 부르면 그 답은 옛 앞단의 것이다
  if [ "$name" != "$new" ]; then
    touch "$WORK/go"
    wait "$pid" || true
    die F0.3 "Traefik never answered from the vMCP front within 60s (serverInfo ${name:-none})"
  fi
  touch "$WORK/go"
  wait "$pid" || true
  f0_block F0.3 "$F0_OUT/f0.3-session.txt"
  [ "$(json_of "$F0_OUT/f0.3-session.json" 'next((s.get("ok") for s in d["steps"] if s["step"] == "call before the switch"), False)')" = True ] \
    || die F0.3 "the session's call before the switch failed, so the steps after it say nothing about the switch"
  f0_note F0.3 "session opened on the proxy front (serverInfo $old); after the switch Traefik answered as $name; the same session called three times, then a new session"
  # 새 세션의 호출이 실패하면 그것은 SDK 의 동작이 아니라 vMCP 앞단의 실패다. 진단을 남기고 실패로 센다
  if [ "$(json_of "$F0_OUT/f0.3-session.json" 'next((s.get("ok") for s in d["steps"] if s["step"] == "new session: call"), False)')" != True ]; then
    vmcp_diag F0.3 "$NS" "front-$k8s" "app.kubernetes.io/instance=$k8s"
    record_fail F0.3 "a new session on the vMCP front could not call echo, so the vMCP front itself failed; see diag-front-$k8s.txt"
  fi
}

# ---------- F1 ----------

F1_TENANT="${F1_TENANT:-f1-front}"
F1_USER="f1-front-u1"
F1_HOSTS=(f1-none.e2e.local f1-secret.e2e.local f1-cm.e2e.local)
F1_NAMES=(f1-none f1-secret f1-cm)

# f1_oauth CONN COMMAND [ARGS...] -> 스크립트 클라이언트(deploy/dev/lab/mcp_oauth_client.py)를 연결 CONN 의 상태 파일로
# 부른다. 요약은 $WORK/body 다(비밀을 싣지 않는다). https 노출의 자체 서명 인증서는 F1_STATE/ca.pem 으로 믿는다
f1_oauth() {
  local conn="$1"
  shift
  SSL_CERT_FILE="$F1_STATE/ca.pem" "${LAB_PY[@]}" "$ROOT/deploy/dev/lab/mcp_oauth_client.py" \
    ${F1_RESOLVE[@]+"${F1_RESOLVE[@]}"} --state "$F1_STATE/oauth-$conn.json" "$@" >"$WORK/body"
}

# f1_consent CONN COMMAND -> 동의 화면의 호출(approve)을 F1_USER 로 한다. 비밀번호는 이 호출에만 환경으로 준다
f1_consent() { MCP_OAUTH_PASSWORD="$LOCAL_PW2" f1_oauth "$1" "$2" --user "$F1_USER"; }

f1_session() { # STEP -> 관리자 로그인과 공통 값. 설치가 local 모드여야 한다
  local h
  f1_state
  TENANT="$F1_TENANT"
  local_admin_session "$1"
  traefik_ip "$1"
  F1_RESOLVE=(--resolve "$(pin "$PUBLIC_URL" "$LB_IP")")
  for h in "${F1_HOSTS[@]}"; do F1_RESOLVE+=(--resolve "$h:80:$LB_IP" --resolve "$h:443:$LB_IP"); done
}

f1_url() { # INDEX
  case "${F1_NAMES[$1]}" in f1-none) echo "http://${F1_HOSTS[$1]}/mcp" ;; *) echo "https://${F1_HOSTS[$1]}/mcp" ;; esac
}

# f1_trust -> Traefik 이 https 노출 둘에 내는 인증서(자체 서명)를 F1_STATE/ca.pem 에 모은다. 스크립트 클라이언트의 TLS
# 검증(httpx2, SSL_CERT_FILE)이 그것을 믿는다. Secret 은 읽지 않는다
f1_trust() {
  local i pem
  : >"$F1_STATE/ca.pem"
  for i in "${!F1_NAMES[@]}"; do
    [ "${F1_NAMES[$i]}" != f1-none ] || continue
    pem="$(openssl s_client -connect "$LB_IP:443" -servername "${F1_HOSTS[$i]}" </dev/null 2>/dev/null | openssl x509 2>/dev/null)" \
      || die F1 "Traefik serves no certificate for ${F1_HOSTS[$i]}"
    printf '%s\n' "$pem" >>"$F1_STATE/ca.pem"
  done
}

f1_prepare() {
  local i c1 url body user
  f1_state
  switch_auth_mode local DAEGAM_OAUTH_ALLOW_HTTP=true || die F1 "switching to local mode failed"
  f1_session F1
  [ "$(jq_ '(d.get("oauth") or {}).get("enabled")')" = True ] || die F1 "OAuth is not enabled: $(cat "$WORK/body")"
  user="$(local_fresh_member "$F1_USER" "$F1_TENANT")" || die F1 "creating $F1_USER failed"
  f1_put user "$user"
  c1="$(local_login_any "$F1_USER" "$LOCAL_PW")"; [ -n "$c1" ] || die F1 "login as $F1_USER failed"
  issue_ok F1 "$c1" "$F1_TENANT" "f1 rehearsal"; f1_put pat "$ISSUED"
  sleep "$JWKS_CACHE_WAIT"
  for i in "${!F1_NAMES[@]}"; do
    case "${F1_NAMES[$i]}" in
      f1-none) body="$(exposure "${F1_HOSTS[$i]}")" ;;
      f1-secret)
        # TLS Secret 은 노출할 서버의 네임스페이스(테넌트의 것)에 먼저 둔다. f1-none 이 그 이름을 알려 준다
        openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=${F1_HOSTS[$i]}" \
          -addext "subjectAltName=DNS:${F1_HOSTS[$i]}" -keyout "$WORK/tls.key" -out "$WORK/tls.crt" 2>/dev/null
        "${K[@]}" -n "$CALC_NS" create secret tls f1-tls --cert "$WORK/tls.crt" --key "$WORK/tls.key" \
          --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
        body="$(exposure_tls "${F1_HOSTS[$i]}" '{"mode": "secret", "secret_name": "f1-tls"}')" ;;
      f1-cm) body="$(exposure_tls "${F1_HOSTS[$i]}" "$CERT_MANAGER_TLS")" ;;
    esac
    calc_mcp_server F1 f1-calc "${F1_NAMES[$i]}" "$body"
    NS="$CALC_NS"
    f0_exposed F1 "/mcp-servers/$CALC_MS_ID"
    [ "${F1_NAMES[$i]}" != f1-cm ] || cert_ready F1 "$CALC_MS_ID"
    f1_put "id-${F1_NAMES[$i]}" "$CALC_MS_ID"; f1_put "k8s-${F1_NAMES[$i]}" "$CALC_MS_K8S_NAME"; f1_put ns "$NS"
  done
  f1_trust
  for i in "${!F1_NAMES[@]}"; do
    url="$(f1_url "$i")"
    # 로그인 연결: 발견, DCR, PKCE 인가, 허용, 코드 교환 (e2e-oauth STEP 1~7 의 흐름)
    f1_oauth "${F1_NAMES[$i]}" discover "$url" || die F1 "discover $url: $(cat "$WORK/body")"
    f1_oauth "${F1_NAMES[$i]}" register --redirect-uri http://127.0.0.1/callback || die F1 "register for $url"
    f1_oauth "${F1_NAMES[$i]}" authorize || die F1 "authorize for $url"
    if ! f1_consent "${F1_NAMES[$i]}" approve || [ "$(jq_ 'd["status"]')" != 200 ]; then die F1 "approve for $url"; fi
    if ! f1_oauth "${F1_NAMES[$i]}" token || [ "$(jq_ 'd["status"]')" != 200 ]; then die F1 "code exchange for $url"; fi
  done
  f1_check prepare
}

# f1_watch -> 노출마다 호출 루프를 뒤에서 돌린다. 관리자 로그인 없이 Traefik IP 와 개인 토큰만 쓴다. 끝에 곧 시작할
# 설치의 시각을 적는다
f1_watch() {
  local i
  f1_state
  traefik_ip F1-watch
  f1_next_round
  for i in "${!F1_NAMES[@]}"; do
    F0_TOKEN="$(f1_get pat)" f0_loop_start "$F1_STATE" "${F1_NAMES[$i]}-r$F1_ROUND" "$(f1_url "$i")" Add '{"a": 3, "b": 4}'
  done
  f0_note F1 "round $F1_ROUND: call loops started for ${F1_NAMES[*]}"
  f1_mark_install
}

# f1_wait_front PHASE INDEX -> new 는 6절의 handover 가 끝나기(Ingress 가 vMCP 앞단에 서고 프록시가 없다)를, old 는 Ingress 가
# 프록시 앞단에 서고 그 프록시가 Ready 이기를 기다린다. 걸린 초를 찍는다. 상한을 넘기면 그때의 상태를 찍고 1 이다
f1_wait_front() {
  local phase="$1" k8s started=$SECONDS limit svc proxy
  k8s="$(f1_get "k8s-${F1_NAMES[$2]}")"
  if [ "$phase" = new ]; then limit="${F1_HANDOVER_TIMEOUT:-1200}"; else limit="${F1_READY_TIMEOUT:-600}"; fi
  while [ $((SECONDS - started)) -lt "$limit" ]; do
    svc="$(ingress_backend "$k8s")"
    proxy="$("${K[@]}" -n "$NS" get mcpremoteproxy "expose-$k8s" -o jsonpath='{.status.phase}' 2>/dev/null || echo absent)"
    if [ "$phase" = new ] && [ "$svc" = "vmcp-front-$k8s" ] && [ "$proxy" = absent ]; then
      echo "$((SECONDS - started))"; return 0
    fi
    if [ "$phase" = old ] && [ "$svc" = "mcp-expose-$k8s-remote-proxy" ] && [ "$proxy" = Ready ]; then
      echo "$((SECONDS - started))"; return 0
    fi
    sleep 5
  done
  echo "ingress=$svc proxy=$proxy after ${limit}s"
  return 1
}

# f1_check PHASE -> prepare 면 기준값을 적는다. new·old 면 앞단이 자리 잡기를 기다리고 루프를 멈춘 뒤(플랫폼 API 없이
# 한다) 관리자로 붙어 기준값과 견준다. check 의 실패는 세고(f1_fail) 다음 확인으로 간다
f1_check() {
  local phase="$1" i name k8s url pin_v tools waited grants pat refresh oauth
  NS="$(f1_get ns)"
  if [ "$phase" != prepare ]; then
    for i in "${!F1_NAMES[@]}"; do
      if waited="$(f1_wait_front "$phase" "$i")"; then
        f0_note F1 "$phase: ${F1_NAMES[$i]} front settled after ${waited}s of this check"
      else
        f0_note F1 "$phase: ${F1_NAMES[$i]} front DID NOT SETTLE: $waited"
        f1_fail "$phase" "${F1_NAMES[$i]} front did not settle: $waited"
      fi
    done
    f1_stop_loops "$phase" "${F1_NAMES[@]}"
    f1_session "F1-$phase"
    f0_note F1 "$phase: ConfigMap DAEGAM_AUTH_MODE=$(platform_config DAEGAM_AUTH_MODE) DAEGAM_OAUTH_ALLOW_HTTP=$(platform_config DAEGAM_OAUTH_ALLOW_HTTP)"
  fi
  for i in "${!F1_NAMES[@]}"; do
    name="${F1_NAMES[$i]}"; k8s="$(f1_get "k8s-$name")"; url="$(f1_url "$i")"; pin_v="$(pin "$url" "$LB_IP")"
    if [ "$(mcp_call "$url" "$pin_v" "$(f1_get pat)" tools/list)" = 200 ]; then
      tools="$(rpc_ 'sorted(t["name"] for t in d["result"]["tools"])')"
    else
      tools="FAILED"
      f1_fail "$phase" "$name: tools/list with the personal token at $url: $(cat "$WORK/rpc.raw" 2>/dev/null)"
    fi
    if [ "$phase" = prepare ]; then f1_put "tools-$name" "$tools"; fi
    [ "$tools" = "$(f1_get "tools-$name")" ] || f1_fail "$phase" "$name: tools $tools, at prepare $(f1_get "tools-$name")"
    TOOL=Add; TOOL_ARGS='{"a": 3, "b": 4}'; TOOL_EXPECT=7
    if allowed "$url" "$pin_v" "$(f1_get pat)"; then
      pat="Add=7"
    else
      pat="FAILED ($MCP_NOTE)"; f1_fail "$phase" "$name: personal token call: $MCP_NOTE"
    fi
    refresh="-"
    if [ "$phase" != prepare ]; then
      if f1_oauth "$name" refresh && [ "$(jq_ 'd["status"]')" = 200 ]; then
        refresh=200
      else
        refresh="FAILED $(cat "$WORK/body")"; f1_fail "$phase" "$name: login connection refresh: $(cat "$WORK/body")"
      fi
    fi
    if f1_oauth "$name" call --tool Add --args '{"a": 3, "b": 4}' && [ "$(jq_ "'7' in json.dumps(d['call']['content'])")" = True ]; then
      oauth="Add=7"
    else
      oauth="FAILED $(cat "$WORK/body")"; f1_fail "$phase" "$name: login connection call: $(cat "$WORK/body")"
    fi
    f0_note F1 "$phase $name ($k8s, ingress -> $(ingress_backend "$k8s")): tools $tools (prepare: $(f1_get "tools-$name")); personal token $pat; refresh $refresh; login connection call $oauth"
    if [ "$name" = f1-cm ]; then
      cert_state "$k8s" "${F1_HOSTS[$i]}" >"$F0_OUT/f1-$phase-cert.txt"
      if [ "$phase" = prepare ]; then
        grep -qE '^revision [0-9]+$' "$F0_OUT/f1-$phase-cert.txt" || die F1 "no issued certificate for ${F1_HOSTS[$i]}"
        cp "$F0_OUT/f1-$phase-cert.txt" "$F1_STATE/cert-prepare.txt"
      fi
      if diff -q "$F1_STATE/cert-prepare.txt" "$F0_OUT/f1-$phase-cert.txt" >/dev/null; then
        f0_note F1 "$phase $name: Certificate owner, revision, serial and requests as at prepare"
      else
        f0_note F1 "$phase $name: Certificate CHANGED: $(diff "$F1_STATE/cert-prepare.txt" "$F0_OUT/f1-$phase-cert.txt" | tr '\n' ' ')"
        f1_fail "$phase" "$name: the Certificate changed"
      fi
    fi
  done
  if [ "$(api GET "/users/$(f1_get user)/oauth-grants")" = 200 ]; then
    grants="$(jq_ '"; ".join("%s %s" % (g["status"], g.get("revoked_reason")) for g in d)')"
    f0_note F1 "$phase login connections of $F1_USER: $grants"
    case "$grants" in *revoked*) f1_fail "$phase" "a login connection was revoked: $grants" ;; esac
  else
    f1_fail "$phase" "GET /users/$(f1_get user)/oauth-grants failed"
  fi
}

# ---------- main ----------

mode="${1:-f0}"
shift || true
case "$mode" in
  f0)
    items=("$@"); [ ${#items[@]} -gt 0 ] || items=(f0.1 f0.2 f0.3)
    f0_header_connect F0
    traefik_ip F0
    f0_server_up F0
    f0_rest_backend F0 f0-rest
    for item in "${items[@]}"; do
      before="$FAILED"
      case "$item" in
        f0.1) f0_1_header; f0_1_local; f0_header_connect F0.1; f0_1_oidc; f0_header_connect F0.1 ;;
        f0.2) f0_2 ;;
        f0.3) f0_3 ;;
        *) die F0 "unknown item $item (f0.1 f0.2 f0.3)" ;;
      esac
      # 항목 안에서 실패로 센 행(record_fail)이 있으면 PASS 를 찍지 않는다. 종료 코드는 finish 가 정한다
      [ "$FAILED" != "$before" ] || pass "$(tr '[:lower:]' '[:upper:]' <<<"$item")" "results in $F0_OUT"
    done
    ;;
  f1)
    phase="${1:-}"
    case "$phase" in
      prepare) f1_prepare ;;
      watch) f1_watch ;;
      check-new) f1_check new ;;
      check-old) f1_check old ;;
      *) die F1 "usage: p54.sh f1 prepare|watch|check-new|check-old" ;;
    esac
    pass "F1 $phase" "results in $F0_OUT"
    ;;
  *) die F0 "usage: p54.sh f0 [f0.1 f0.2 f0.3] | f1 prepare|watch|check-new|check-old" ;;
esac
finish
