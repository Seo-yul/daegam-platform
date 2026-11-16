#!/usr/bin/env bash
# P3 F8 e2e against the real (orbstack) cluster. Assumes `make deploy-local` is done
# and the lab backends (mock-soap, redis in daegam-spike) are up (`make lab-up`).
#
# Steps print `STEP n PASS|FAIL: ...`. 이 게이트에는 SKIP 경로가 없다 (조건이 안 맞으면 실패다).
# macOS has no `timeout`, so every wait is a polling loop.
#
# D24 (P10): 리소스마다 한글 `display_name` 을 보내 응답에 그대로 돌아오는지(STEP 3·5·6), CR 이름이
# `mcp-<name>` 그대로인지(STEP 6, kubectl) 를 함께 본다. 이름 규칙의 422(invalid_name·invalid_display_name·
# name_immutable)는 API 테스트가 지킨다(platform/tests/test_names.py, platform/tests/test_mcp_servers.py). STEP 11 은
# 비어 있다. 단계 번호를 다른 문서가 가리키므로 당기지 않는다.
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
TENANT="${E2E_TENANT:-e2e}"
API_PORT="${E2E_API_PORT:-$PORT_INSTANCE_API}"
MCP_PORT="${E2E_MCP_PORT:-$PORT_INSTANCE_MCP}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
BASE="http://127.0.0.1:${API_PORT}"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
FIXTURE="$ROOT/platform/tests/fixtures/calculator.wsdl"
SOAP_ENDPOINT="http://mock-soap.${SPIKE_NS}.svc:18080/calc"
BP_NAME="calc-e2e"; DS_NAME="redis-e2e"; MS_NAME="calc-e2e-server"; CLONE_NAME="calc-e2e-clone"
# STEP 13: 시험 CA 가 서명한 https mock-soap 과 그것을 부르는 Backend API·MCP Server (D88)
TLS_NAME="mock-soap-tls"; TLS_HOST="${TLS_NAME}.${SPIKE_NS}.svc"; TLS_ENDPOINT="https://${TLS_HOST}:18443/calc"
TLS_BP_NAME="calc-tls-e2e"; TLS_MS_NAME="calc-tls-e2e-server"
TLS_BP_ID=""; TLS_MS_ID=""; NETWORK_SAVED=""
# 표시 이름(D24). 한글을 그대로 보내고 그대로 받는다. K8s 이름은 위의 소문자·하이픈 값이다
BP_DISPLAY="계산기 SOAP"; MS_DISPLAY="계산기 서버"; CLONE_DISPLAY="계산기 복제본"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-inst.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()   # header 모드라 Bearer 가 없다 (lib/e2e_api.sh 가 읽는다)
PF_API=""; PF_MCP=""

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"

cleanup() {
  stop_pf "$PF_MCP" "$MCP_PORT"
  tls_cleanup
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}

# STEP 13 이 만든 것을 지우고 network.ca_bundle 을 실행 전 값으로 되돌린다. 멈춘 실행에서도 돈다
tls_cleanup() {
  [ -z "$TLS_MS_ID" ] || api_delete "/mcp-servers/$TLS_MS_ID"
  [ -z "$TLS_BP_ID" ] || api_delete "/backend-apis/$TLS_BP_ID"
  if [ -n "$NETWORK_SAVED" ]; then
    code="$(api PATCH /settings "@$WORK/network0.json")"
    [ "$code" = 200 ] || echo "!! restoring network.ca_bundle -> $code: $(cat "$WORK/body")" >&2
  fi
  "${K[@]}" -n "$SPIKE_NS" delete deploy,svc,secret "$TLS_NAME" --ignore-not-found >/dev/null 2>&1 || true
  TLS_MS_ID=""; TLS_BP_ID=""; NETWORK_SAVED=""
}
on_exit cleanup

# pass·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"

# api·jq_·find_id·wait_ready·clone_fresh·save_doc 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# lab Redis 를 Database 로 등록하는 lab_database 와 LAB_REDIS_HOST 는 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# mcp_status(initialize 한 번의 HTTP 상태)와 svc_token(D41 서비스 토큰)은 여기에 있다
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-instance"
. "$HERE/lib/mcp_rpc.sh"

echo ">> context=$CTX platform_ns=$PLATFORM_NS tenant=$TENANT api=$API"

# ---------- 1. platform reachable ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = "200" ] || die 1 "GET /healthz -> $code"
pass 1 "platform reachable via port-forward :$API_PORT ($(cat "$WORK/body"))"

# ---------- 2. home cluster preflight ok ----------
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 2 "GET /clusters -> $code"
# 없으면 바로 아래 줄이 스스로 다룬다
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"]=="home"), "")')"
TH_VER="$(jq_ 'next((c["status"]["preflight"].get("toolhive_version") for c in d if c["name"]=="home"), "")')"
[ -n "$HOME_ID" ] && [ "$HOME_OK" = "True" ] || die 2 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
pass 2 "cluster home id=$HOME_ID preflight ok toolhive=$TH_VER"

# ---------- 3. backend api (calculator) + endpoint ----------
code="$(api POST /backend-apis "" -F "name=$BP_NAME" -F "display_name=$BP_DISPLAY" -F "file=@$FIXTURE")"
[ "$code" = "201" ] || [ "$code" = "200" ] || die 3 "POST /backend-apis -> $code"
BP_ID="$(jq_ 'd["id"]')"; BP_VER="$(jq_ 'd["version"]')"; BP_TOOLS="$(jq_ 'd["tool_names"]')"
[ "$(jq_ 'd["name"], d["display_name"]')" = "$BP_NAME $BP_DISPLAY" ] || die 3 "names: $(jq_ 'd["name"], d["display_name"]') (expected $BP_NAME $BP_DISPLAY)"
code="$(api PATCH "/backend-apis/$BP_ID" "{\"endpoint\": \"$SOAP_ENDPOINT\"}")"
[ "$code" = "200" ] || die 3 "PATCH /backend-apis/$BP_ID endpoint -> $code"
[ "$(jq_ 'd["endpoint"]')" = "$SOAP_ENDPOINT" ] || die 3 "endpoint not stored"
[ "$(jq_ 'd["display_name"]')" = "$BP_DISPLAY" ] || die 3 "display_name lost on PATCH: $(jq_ 'd["display_name"]')"
pass 3 "backend api $BP_NAME ($BP_DISPLAY) id=$BP_ID version=$BP_VER tools=$BP_TOOLS endpoint=$SOAP_ENDPOINT"

# ---------- 4. data service (redis) + verify ----------
# lab Redis 는 --requirepass 로 뜬다. verify 가 실제로 붙으므로 비밀번호를 같이 등록하고, 재실행의 409 면 비밀번호를
# Secret 에 맞춘다 (lab_database). lab Redis 는 Redis 8 이라 체크포인터 확인도 통과한다
lab_database 4 redis "$DS_NAME" "$DS_NAME"
DS_ID="$DATA_SERVICE_ID"
pass 4 "data service $DS_NAME id=$DS_ID verify ok ($LAB_REDIS_HOST:6379)"

# ---------- 5. mcp server 정의 (tools [Add], CACHE binding) ----------
# 만들기의 기본은 저장이다. 여기서는 클러스터에 아무것도 생기지 않는다 (D42). 재실행의 409 는 같은 정의로 새 리비전을 쌓는다
body="{\"name\": \"$MS_NAME\", \"display_name\": \"$MS_DISPLAY\", \"backend_profile_id\": \"$BP_ID\", \"spec_version\": $BP_VER, \"tools\": [\"Add\"], \"bindings\": [{\"data_service_id\": \"$DS_ID\", \"env_prefix\": \"CACHE\"}]}"
save_doc 5 /mcp-servers "$MS_NAME" "$body"
MS_ID="$SAVED_ID"
MS_REV="$(jq_ 'd.get("revision")')"
TOOLS="$(jq_ '((d.get("spec") or d).get("backends") or [{}])[0].get("tools")')"
[ "$TOOLS" = "['Add']" ] || die 5 "mcp server tools=$TOOLS, expected ['Add']"
[ "$(jq_ 'd["name"], d["display_name"]')" = "$MS_NAME $MS_DISPLAY" ] || die 5 "names: $(jq_ 'd["name"], d["display_name"]') (expected $MS_NAME $MS_DISPLAY)"
pass 5 "mcp server $MS_NAME ($MS_DISPLAY) id=$MS_ID revision=$MS_REV tools=$TOOLS binding CACHE->$DS_ID"

# ---------- 6. deploy -> Ready ----------
# 배포 본문은 이름을 받지 않는다. 이름은 이미 문서의 것이다 (D42)
code="$(api POST "/mcp-servers/$MS_ID/deploy" "{\"cluster_id\": \"$HOME_ID\"}")"
if [ "$code" = "409" ]; then
  # 이전 실행이 남긴 배포다. 정의가 이 실행의 것과 같아 재배포할 것이 없으므로 그대로 이어간다.
  # 같은 선택을 한 scripts/e2e_agent.sh STEP 3 에 재배포가 부르는 롤링 때문이라는 까닭이 적혀 있다
  [ "$(jq_ "d['detail']['code']")" = "already_deployed" ] \
    || die 6 "409 but code=$(jq_ "d['detail']['code']"): $(cat "$WORK/body")"
  api GET "/mcp-servers/$MS_ID" >/dev/null
elif [ "$code" != "201" ] && [ "$code" != "202" ]; then
  die 6 "POST /mcp-servers/$MS_ID/deploy -> $code: $(cat "$WORK/body")"
fi
# 표시 이름은 그대로, CR 이름은 규칙 안의 name 에 접두어만 붙는다 (D24. k8s_name 이 항등)
[ "$(jq_ 'd["name"], d["display_name"], d["k8s"]["name"]')" = "$MS_NAME $MS_DISPLAY mcp-$MS_NAME" ] \
  || die 6 "names: $(jq_ 'd["name"], d["display_name"], d["k8s"]["name"]') (expected $MS_NAME $MS_DISPLAY mcp-$MS_NAME)"
echo ">> waiting up to ${READY_TIMEOUT}s for mcp server $MS_ID phase=Ready"
# Pending·Rolling·잠깐의 Failed 는 모두 기다린다. 끝나면 $WORK/body 가 Ready 인 문서다
wait_ready "/mcp-servers/$MS_ID" 6
[ "$(jq_ 'd["state"]')" = "deployed" ] || die 6 "state=$(jq_ 'd["state"]') (expected deployed)"
INST_URL="$(jq_ '(d.get("status") or {}).get("url")')"
K8S_NAME="$(jq_ '(d.get("k8s") or {}).get("name") or "mcp-'"$MS_NAME"'"')"
K8S_NS="$(jq_ '(d.get("k8s") or {}).get("namespace") or "daegam-'"$TENANT"'"')"
[ "$K8S_NAME" = "mcp-$MS_NAME" ] || die 6 "k8s.name=$K8S_NAME (expected mcp-$MS_NAME)"
"${K[@]}" -n "$K8S_NS" get mcpserver "$K8S_NAME" -o name >/dev/null || die 6 "MCPServer $K8S_NS/$K8S_NAME missing"
pass 6 "deployed $MS_NAME ($(jq_ 'd["display_name"]')) id=$MS_ID state=deployed Ready url=$INST_URL cr=$K8S_NS/$K8S_NAME"

# ---------- 7. tools/list == [Add], Add(3,4)=7, Divide unknown ----------
# status.url is http://<svc>.<ns>.svc.cluster.local:8080/mcp -> port-forward that Service
svc_host="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' "$INST_URL")"
svc="${svc_host%%.*}"; svc_ns="$(echo "$svc_host" | cut -d. -f2)"
[ -n "$svc" ] || { svc="mcp-${K8S_NAME}-proxy"; svc_ns="$K8S_NS"; }
start_pf "$svc_ns" "svc/$svc" "${MCP_PORT}:8080"
PF_MCP=$PF_PID
wait_port "$MCP_PORT" || die 7 "port-forward svc/$svc did not open :$MCP_PORT"
MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
# 무토큰은 401 이다 (D41). 그 뒤 세 호출은 서비스 토큰을 붙인다
code="$(mcp_status "$MCP_URL")"
[ "$code" = "401" ] || die 7 "initialize without a token -> $code (D41 requires 401)"
SVC_TOKEN="$(svc_token "$MS_ID")"; [ -n "$SVC_TOKEN" ] || die 7 "could not sign a service token"
mcp() { uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/mcp_client.py" --token "$SVC_TOKEN" "$@"; }
list_out="$(mcp list "$MCP_URL" 2>&1)" || { echo "$list_out"; die 7 "tools/list failed"; }
echo "  tools/list: $list_out"
echo "$list_out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["tools"]==["Add"] else 1)' \
  || die 7 "tools/list is not exactly [Add]"
call_out="$(mcp call "$MCP_URL" Add '{"a": 3, "b": 4}' 2>&1)" || { echo "$call_out"; die 7 "Add(3,4) failed"; }
echo "  tools/call: $call_out"
adds_to_7 "$call_out" || die 7 "Add(3,4) != 7"
div_out="$(mcp call "$MCP_URL" Divide '{"numerator": 1, "denominator": 2}' 2>&1)" && { echo "$div_out"; die 7 "Divide must be rejected"; }
echo "  tools/call: $div_out"
echo "$div_out" | python3 -c 'import json,sys; c=json.load(sys.stdin)["call"]; sys.exit(0 if (c["isError"] and "unknown tool" in " ".join(x or "" for x in c["content"]).lower()) else 1)' \
  || die 7 "Divide not rejected as unknown tool"
pass 7 "no-token 401; with the service token: tools/list == [Add], Add(3,4)=7, Divide -> unknown tool"

# ---------- 8. pod env has CACHE_URL ----------
env_names="$("${K[@]}" -n "$K8S_NS" get pod "${K8S_NAME}-0" -o jsonpath='{.spec.containers[?(@.name=="mcp")].env[*].name}' 2>/dev/null || true)"
echo "  env: $env_names"
case " $env_names " in
  *" CACHE_URL "*) pass 8 "pod ${K8S_NAME}-0 has CACHE_URL" ;;
  *) die 8 "CACHE_URL missing from pod ${K8S_NAME}-0 env" ;;
esac

# ---------- 9. undeploy -> draft (정의는 남는다), 다시 deploy -> Ready ----------
if [ -n "$PF_MCP" ]; then stop_pf "$PF_MCP" "$MCP_PORT"; PF_MCP=""; fi
code="$(api POST "/mcp-servers/$MS_ID/undeploy")"
[ "$code" = "200" ] || die 9 "POST /mcp-servers/$MS_ID/undeploy -> $code: $(cat "$WORK/body")"
# 남이 클레임을 쥐고 있었으면 undeploying 으로 답하고 홀더가 마친다 (P66 2.6절)
if [ "$(jq_ 'd["state"]')" = "undeploying" ]; then
  wait_draft "/mcp-servers/$MS_ID" || die 9 "deferred undeploy did not reach draft: $(cat "$WORK/body")"
fi
[ "$(jq_ 'd["state"]')" = "draft" ] || die 9 "state=$(jq_ 'd["state"]') (expected draft)"
# 해제는 클러스터만 비운다. 정의와 리비전은 그대로다 (D42)
[ "$(jq_ 'd["revision"]')" = "$MS_REV" ] || die 9 "revision changed on undeploy: $(jq_ 'd["revision"]')"
[ "$(jq_ 'd["k8s"]')" = "None" ] || die 9 "k8s not cleared on undeploy: $(jq_ 'd["k8s"]')"
gone=""
for ((i = 0; i < 24; i++)); do
  cr="$("${K[@]}" -n "$K8S_NS" get mcpserver "$K8S_NAME" -o name 2>/dev/null || true)"
  [ -z "$cr" ] && { gone=1; break; }
  sleep 5
done
[ -n "$gone" ] || die 9 "MCPServer $K8S_NS/$K8S_NAME still present after undeploy"
# 이름이 살아 있으므로 같은 문서를 그대로 다시 배포한다
code="$(api POST "/mcp-servers/$MS_ID/deploy" "{\"cluster_id\": \"$HOME_ID\"}")"
[ "$code" = "201" ] || [ "$code" = "202" ] || die 9 "deploy after undeploy -> $code: $(cat "$WORK/body")"
wait_ready "/mcp-servers/$MS_ID" 9
pass 9 "undeploy -> draft (정의·리비전 유지, CR 사라짐); deploy 다시 -> Ready"

# ---------- 10. clone (정의 복제) -> draft, 원본은 그대로, 삭제 -> 404 ----------
# 이전 실행이 남긴 복제본(409)은 지우고 다시 복제한다
clone_fresh 10 /mcp-servers "$MS_ID" "$CLONE_NAME" "$CLONE_DISPLAY"
CLONE_ID="$(jq_ 'd["id"]')"
[ "$CLONE_ID" != "$MS_ID" ] || die 10 "clone returned the source id"
# 복제본은 정의만 베낀 draft 다
[ "$(jq_ 'd["state"]')" = "draft" ] || die 10 "clone state=$(jq_ 'd["state"]') (expected draft)"
TOOLS="$(jq_ '((d.get("spec") or d).get("backends") or [{}])[0].get("tools")')"
[ "$TOOLS" = "['Add']" ] || die 10 "clone tools=$TOOLS, expected ['Add']"
# 원본의 배포는 건드리지 않는다
api GET "/mcp-servers/$MS_ID" >/dev/null
[ "$(jq_ 'd["state"]')" = "deployed" ] || die 10 "source state changed by clone: $(jq_ 'd["state"]')"
code="$(api DELETE "/mcp-servers/$CLONE_ID")"
case "$code" in 200|202|204) ;; *) die 10 "DELETE /mcp-servers/$CLONE_ID -> $code" ;; esac
code="$(api GET "/mcp-servers/$CLONE_ID")"
[ "$code" = "404" ] || die 10 "GET deleted clone -> $code (expected 404)"
pass 10 "clone $CLONE_NAME -> draft (원본은 deployed 유지); 삭제 -> 404"

# ---------- 12. delete -> CR and token secret gone, GET -> 404 ----------
code="$(api DELETE "/mcp-servers/$MS_ID")"
case "$code" in 200|202|204) ;; *) die 12 "DELETE /mcp-servers/$MS_ID -> $code" ;; esac
# 남이 클레임을 쥐고 있었으면 deleted 가 거짓이고 홀더가 마친다 (P66 2.6절)
deferred="$(jq_ 'd.get("deleted") is False' || true)"
gone=""
for ((i = 0; i < 24; i++)); do
  cr="$("${K[@]}" -n "$K8S_NS" get mcpserver "$K8S_NAME" -o name 2>/dev/null || true)"
  sec="$("${K[@]}" -n "$K8S_NS" get secret "inst-${MS_ID}-token" -o name 2>/dev/null || true)"
  if [ -z "$cr" ] && [ -z "$sec" ]; then gone=1; break; fi
  sleep 5
done
[ -n "$gone" ] || die 12 "still present after delete: cr='$cr' secret='$sec'"
# 삭제는 정의까지 지운다. 소프트 삭제가 아니므로 410 이 아니라 404 다 (D42)
if [ "$deferred" = "True" ]; then
  wait_gone "/mcp-servers/$MS_ID" || die 12 "deferred delete did not finish"
fi
code="$(api GET "/mcp-servers/$MS_ID")"
[ "$code" = "404" ] || die 12 "GET deleted mcp server -> $code (expected 404)"
pass 12 "삭제; MCPServer $K8S_NS/$K8S_NAME 과 secret inst-${MS_ID}-token 사라짐, GET -> 404"

# ---------- 13. 사설 CA 뒤의 https 백엔드 (D88) ----------
# 시험 CA 를 만들어 그 CA 가 서명한 https mock-soap(MOCK_SOAP_TLS_DIR)을 lab 네임스페이스에 띄우고, 그 CA 를 지금
# network.ca_bundle 뒤에 더한다. 그 뒤 배포한 MCP Server 는 배포 스냅샷의 CA 를 env DAEGAM_CA_B64 로 받아 https 로 부른다.
# 설정은 정리 단계(tls_cleanup)가 되돌린다. 이전 실행이 남긴 것은 먼저 지운다
"${K[@]}" -n "$SPIKE_NS" delete deploy,svc,secret "$TLS_NAME" --ignore-not-found >/dev/null
mkdir -p "$WORK/tls"
# 인증서 생성기는 플랫폼 시험이 TLS 를 볼 때 쓰는 것이다(platform/tests/helpers/tls.py). 엄격한 검사도 받는 인증서다
uv run --quiet --project "$ROOT/platform" python - "$ROOT/platform" "$WORK/tls" "$TLS_HOST" <<'PY' || die 13 "could not make the test CA"
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tests.helpers.tls import cert_bytes, key_bytes, new_ca, new_server  # noqa: E402

out = Path(sys.argv[2])
ca_key, ca = new_ca("daegam-e2e-private-ca", days=60)
key, cert = new_server(ca_key, ca, [sys.argv[3]], days=30)
(out / "ca.crt").write_bytes(cert_bytes(ca))
(out / "tls.crt").write_bytes(cert_bytes(cert))
(out / "tls.key").write_bytes(key_bytes(key))
PY
"${K[@]}" -n "$SPIKE_NS" create secret tls "$TLS_NAME" --cert="$WORK/tls/tls.crt" --key="$WORK/tls/tls.key" >/dev/null \
  || die 13 "could not create the Secret $SPIKE_NS/$TLS_NAME"
"${K[@]}" -n "$SPIKE_NS" apply -f - >/dev/null <<YAML || die 13 "could not apply $SPIKE_NS/$TLS_NAME"
apiVersion: apps/v1
kind: Deployment
metadata: {name: $TLS_NAME, labels: {app: $TLS_NAME}}
spec:
  replicas: 1
  selector: {matchLabels: {app: $TLS_NAME}}
  template:
    metadata: {labels: {app: $TLS_NAME}}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 1001, runAsGroup: 1001}
      containers:
        - name: soap
          image: daegam-mock-soap:dev
          imagePullPolicy: IfNotPresent
          env: [{name: MOCK_SOAP_TLS_DIR, value: /tls}]
          ports: [{name: https, containerPort: 18443}]
          readinessProbe: {tcpSocket: {port: https}, periodSeconds: 2}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true}
          volumeMounts: [{name: tls, mountPath: /tls, readOnly: true}]
      volumes: [{name: tls, secret: {secretName: $TLS_NAME}}]
---
apiVersion: v1
kind: Service
metadata: {name: $TLS_NAME}
spec:
  selector: {app: $TLS_NAME}
  ports: [{name: https, port: 18443, targetPort: https}]
YAML
"${K[@]}" -n "$SPIKE_NS" rollout status "deploy/$TLS_NAME" --timeout=120s >/dev/null \
  || die 13 "$SPIKE_NS/$TLS_NAME did not become ready"

code="$(api GET /settings)"
[ "$code" = 200 ] || die 13 "GET /settings -> $code"
jq_ 'json.dumps({"network": d["network"]})' >"$WORK/network0.json"
NETWORK_SAVED=1
python3 - "$WORK/network0.json" "$WORK/tls/ca.crt" >"$WORK/network.json" <<'PY'
import json, sys
bundle = json.load(open(sys.argv[1]))["network"].get("ca_bundle") or ""
print(json.dumps({"network": {"ca_bundle": bundle + open(sys.argv[2]).read()}}))
PY
code="$(api PATCH /settings "@$WORK/network.json")"
[ "$code" = 200 ] || die 13 "PATCH /settings network.ca_bundle -> $code: $(cat "$WORK/body")"

upload_backend_api 13 "$TLS_BP_NAME" "$FIXTURE"
TLS_BP_ID="$BACKEND_API_ID"; TLS_BP_VER="$BACKEND_API_VERSION"
code="$(api PATCH "/backend-apis/$TLS_BP_ID" "{\"endpoint\": \"$TLS_ENDPOINT\"}")"
[ "$code" = 200 ] || die 13 "PATCH /backend-apis/$TLS_BP_ID endpoint -> $code"
body="{\"name\": \"$TLS_MS_NAME\", \"backend_profile_id\": \"$TLS_BP_ID\", \"spec_version\": $TLS_BP_VER, \"tools\": [\"Add\"]}"
save_doc 13 /mcp-servers "$TLS_MS_NAME" "$body"
TLS_MS_ID="$SAVED_ID"
# 이전 실행이 남긴 배포는 재배포해 이 실행의 CA 를 스냅샷에 적는다(deploy_doc 의 409)
deploy_ready 13 /mcp-servers "$TLS_MS_ID" "{\"cluster_id\": \"$HOME_ID\"}"
TLS_SHA="$(jq_ 'd["effective_config"].get("ca_bundle_sha256") or ""')"
[ -n "$TLS_SHA" ] || die 13 "effective_config has no ca_bundle_sha256: $(jq_ 'd["effective_config"]')"
[ "$(jq_ '"ca_bundle" in d["effective_config"]')" = False ] || die 13 "the API returned the CA PEM"
TLS_URL="$(jq_ '(d.get("status") or {}).get("url")')"
TLS_K8S="$(jq_ 'd["k8s"]["name"]')"; TLS_NS="$(jq_ 'd["k8s"]["namespace"]')"
env_names="$("${K[@]}" -n "$TLS_NS" get pod "${TLS_K8S}-0" -o jsonpath='{.spec.containers[?(@.name=="mcp")].env[*].name}' 2>/dev/null || true)"
case " $env_names " in
  *" DAEGAM_CA_B64 "*) ;;
  *) die 13 "DAEGAM_CA_B64 missing from pod ${TLS_K8S}-0 env: $env_names" ;;
esac
svc_host="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' "$TLS_URL")"
svc="${svc_host%%.*}"; svc_ns="$(echo "$svc_host" | cut -d. -f2)"
[ -n "$svc" ] || { svc="mcp-${TLS_K8S}-proxy"; svc_ns="$TLS_NS"; }
start_pf "$svc_ns" "svc/$svc" "${MCP_PORT}:8080"
PF_MCP=$PF_PID
wait_port "$MCP_PORT" || die 13 "port-forward svc/$svc did not open :$MCP_PORT"
SVC_TOKEN="$(svc_token "$TLS_MS_ID")"; [ -n "$SVC_TOKEN" ] || die 13 "could not sign a service token"
call_out="$(mcp call "$MCP_URL" Add '{"a": 3, "b": 4}' 2>&1)" || { echo "$call_out"; die 13 "Add(3,4) over https failed"; }
echo "  tools/call: $call_out"
adds_to_7 "$call_out" || die 13 "Add(3,4) over https != 7"
stop_pf "$PF_MCP" "$MCP_PORT"; PF_MCP=""
tls_cleanup
pass 13 "https mock-soap behind a test CA: network.ca_bundle -> env DAEGAM_CA_B64 (sha256 ${TLS_SHA:0:16}), Add(3,4)=7 over $TLS_ENDPOINT; settings restored"

# ---------- 14. soap:header 값을 Backend API 의 Secret 에서 받는다 (T60) ----------
# lab mock-soap 의 /secured-calc 는 봉투 Header 의 AuthHeader/token 이 맞을 때만 답한다(이미지를 이 판의
# deploy/dev/lab/images/mock-soap/run.py 로 구워야 한다). 칸 없이 배포한 서버의 Add 는 Fault 이고,
# Secret 의 soap_headers 키를 가리킨 뒤 재배포하면 SPEC2OPENAPI_SOAP_HEADERS 로 받아 7 이다
HDR_FIXTURE="$ROOT/mcp-runtime/tests/fixtures/calculator-auth-header.wsdl"
HDR_ENDPOINT="http://mock-soap.${SPIKE_NS}.svc:18080/secured-calc"
HDR_BP="calc-hdr-e2e"; HDR_MS="calc-hdr-e2e-server"; HDR_SECRET="calc-hdr-e2e-soap"
upload_backend_api 14 "$HDR_BP" "$HDR_FIXTURE"
HDR_BP_ID="$BACKEND_API_ID"
code="$(api PATCH "/backend-apis/$HDR_BP_ID" "{\"endpoint\": \"$HDR_ENDPOINT\", \"soap_headers_secret\": null}")"
[ "$code" = "200" ] || die 14 "PATCH /backend-apis/$HDR_BP_ID -> $code: $(cat "$WORK/body")"
save_doc 14 /mcp-servers "$HDR_MS" "{\"name\": \"$HDR_MS\", \"backend_profile_id\": \"$HDR_BP_ID\", \"spec_version\": $BACKEND_API_VERSION, \"tools\": [\"Add\"]}"
HDR_MS_ID="$SAVED_ID"
deploy_ready 14 /mcp-servers "$HDR_MS_ID" "{\"cluster_id\": \"$HOME_ID\"}"
HDR_NS="$(jq_ 'd["k8s"]["namespace"]')"; HDR_CR="$(jq_ 'd["k8s"]["name"]')"
hdr_host="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' "$(jq_ '(d.get("status") or {}).get("url")')")"
SVC_TOKEN="$(svc_token "$HDR_MS_ID")"; [ -n "$SVC_TOKEN" ] || die 14 "could not sign a service token"
hdr_add() { # 프록시에 새로 붙어 Add(3,4) 를 한 번 부르고 답을 hdr_out 에 둔다. 재배포 뒤 옛 파드가 답하지 않도록 매번 붙는다
  start_pf "$(echo "$hdr_host" | cut -d. -f2)" "svc/${hdr_host%%.*}" "${MCP_PORT}:8080"
  PF_MCP=$PF_PID
  wait_port "$MCP_PORT" || die 14 "port-forward svc/${hdr_host%%.*} did not open :$MCP_PORT"
  hdr_out="$(mcp call "http://127.0.0.1:${MCP_PORT}/mcp" Add '{"a": 3, "b": 4}' 2>&1 || true)"
  stop_pf "$PF_MCP" "$MCP_PORT"; PF_MCP=""
}
hdr_add
echo "  without the header: $hdr_out"
# 이미지를 다시 굽지 않았으면 옛 mock 이 모르는 경로라 Fault 를 내므로, 그 Fault 의 글로 가른다
[ "$(json_of - 'd["call"]["isError"] and "AuthHeader token missing" in " ".join(x or "" for x in d["call"]["content"])' <<<"$hdr_out")" = True ] \
  || die 14 "Add without the soap:header must fail at the secured backend with its Fault (is the mock-soap image rebuilt?)"
"${K[@]}" -n "$HDR_NS" create secret generic "$HDR_SECRET" \
  --from-literal=soap_headers='{"auth": {"token": "e2e-header-token"}}' --dry-run=client -o yaml \
  | "${K[@]}" apply -f - >/dev/null
code="$(api PATCH "/backend-apis/$HDR_BP_ID" "{\"soap_headers_secret\": \"$HDR_SECRET\"}")"
[ "$code" = "200" ] && [ "$(jq_ 'd["soap_headers_secret"]')" = "$HDR_SECRET" ] \
  || die 14 "PATCH soap_headers_secret -> $code: $(cat "$WORK/body")"
code="$(api POST "/mcp-servers/$HDR_MS_ID/redeploy")"
[ "$code" = "200" ] || die 14 "POST /mcp-servers/$HDR_MS_ID/redeploy -> $code: $(cat "$WORK/body")"
wait_ready "/mcp-servers/$HDR_MS_ID" 14
target="$("${K[@]}" -n "$HDR_NS" get mcpserver "$HDR_CR" -o json | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
refs = [s for s in spec.get("secrets") or [] if s.get("targetEnvName") == "SPEC2OPENAPI_SOAP_HEADERS"]
refs += [e["valueFrom"]["secretKeyRef"] for c in spec["podTemplateSpec"]["spec"]["containers"]
         for e in c.get("env") or [] if e.get("name") == "SPEC2OPENAPI_SOAP_HEADERS"]
print(" ".join(r["name"] + "/" + r["key"] for r in refs))')"
[ "$target" = "$HDR_SECRET/soap_headers" ] || die 14 "SPEC2OPENAPI_SOAP_HEADERS comes from '$target' (expected $HDR_SECRET/soap_headers)"
# 새 파드가 뜨는 동안 옛 파드가 답할 수 있어 7 이 나올 때까지 다시 부른다
ok=""
for ((i = 0; i < 12; i++)); do
  hdr_add
  adds_to_7 "$hdr_out" 2>/dev/null && { ok=1; break; }
  sleep 5
done
[ -n "$ok" ] || die 14 "Add(3,4) with the soap:header from $HDR_SECRET did not return 7: $hdr_out"
api_delete "/mcp-servers/$HDR_MS_ID"
wait_gone "/mcp-servers/$HDR_MS_ID" || die 14 "mcp server $HDR_MS_ID not gone after delete"
api_delete "/backend-apis/$HDR_BP_ID"
"${K[@]}" -n "$HDR_NS" delete secret "$HDR_SECRET" --ignore-not-found >/dev/null
pass 14 "soap:header 없이 Add -> Fault; Secret $HDR_SECRET 의 soap_headers 를 가리키고 재배포 -> SPEC2OPENAPI_SOAP_HEADERS 로 받아 Add(3,4)=7"

finish
