#!/usr/bin/env bash
# P1 spike gates 1-5 (docs/archive/plan/phase-1-spike.md F5) + P3 F1 gate 6 (profile
# subset: Divide is not served). Run from repo root or
# anywhere; expects the manifests to be applied already (`make lab-up`).
set -euo pipefail

NS="${SPIKE_NS:-daegam-spike}"
CTX="${KUBE_CONTEXT:-orbstack}"
LOCAL_PORT="${LOCAL_PORT:-18081}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# MCP 호출은 platform 의 daegam.mcp_client 를 감싼 CLI 로 한다 (구현은 한 벌)
MCP_CLIENT=(uv run --quiet --project "$ROOT/platform" python "$HERE/mcp_client.py")
K=(kubectl --context "$CTX" -n "$NS")
MCP_URL="http://localhost:${LOCAL_PORT}/mcp"
PF_PID=""

cleanup() {
  if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

dump() {
  echo
  echo "===== DUMP: mcpserver calculator -o yaml ====="
  "${K[@]}" get mcpserver calculator -o yaml 2>&1 || true
  echo "===== DUMP: pods ====="
  "${K[@]}" get pods -o wide 2>&1 || true
  echo "===== DUMP: describe pod calculator-0 ====="
  "${K[@]}" describe pod calculator-0 2>&1 || true
  echo "===== DUMP: logs deploy/calculator (proxyrunner) ====="
  "${K[@]}" logs deploy/calculator --tail=200 2>&1 || true
  echo "===== DUMP: logs calculator-0 -c fetch-spec ====="
  "${K[@]}" logs calculator-0 -c fetch-spec --tail=100 2>&1 || true
  echo "===== DUMP: logs calculator-0 -c mcp ====="
  "${K[@]}" logs calculator-0 -c mcp --tail=200 2>&1 || true
  echo "===== DUMP: logs deploy/stub-spec-server ====="
  "${K[@]}" logs deploy/stub-spec-server --tail=50 2>&1 || true
  echo "===== DUMP: logs deploy/mock-soap ====="
  "${K[@]}" logs deploy/mock-soap --tail=50 2>&1 || true
}

app_logs() { # app label, lines per pod -> the logs of every pod with that label
  # lab-up 이 만든 직후 재시작해 두 파드가 같은 초에 생기므로 생성 시각으로 하나를 고르면 요청을 받은 파드를 놓친다
  "${K[@]}" logs -l "app=$1" --tail="$2" --max-log-requests=10
}

pass() { echo "GATE $1 PASS: $2"; }
fail() { echo "GATE $1 FAIL: $2"; dump; exit 1; }

echo ">> context=$CTX namespace=$NS"

# ---------- gate 1: MCPServer phase Ready ----------
echo ">> waiting for supporting deployments"
"${K[@]}" rollout status deploy/stub-spec-server --timeout=120s
"${K[@]}" rollout status deploy/mock-soap --timeout=120s

# 이미 떠 있던 MCPServer 의 spec(이미지)을 바꾼 직후에는 phase 가 잠시 예전 Ready 로 남아 있다가
# 롤링 중 Failed 를 거쳐 다시 Ready 가 된다 (README 발견 6). 그래서 phase 만 보지 않고
# calculator-0 파드가 spec.image 와 같은 이미지로 Ready 인지도 함께 기다린다.
want_image="$("${K[@]}" get mcpserver calculator -o jsonpath='{.spec.image}')"
echo ">> waiting up to ${READY_TIMEOUT}s for mcpserver/calculator phase=Ready with pod image ${want_image}"
phase=""; pod_image=""; pod_ready=""
for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
  phase="$("${K[@]}" get mcpserver calculator -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  pod_image="$("${K[@]}" get pod calculator-0 -o jsonpath='{.spec.containers[?(@.name=="mcp")].image}' 2>/dev/null || true)"
  pod_ready="$("${K[@]}" get pod calculator-0 -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [ "$phase" = "Ready" ] && [ "$pod_image" = "$want_image" ] && [ "$pod_ready" = "True" ]; then break; fi
  sleep 5
done
if [ "$phase" = "Ready" ] && [ "$pod_image" = "$want_image" ] && [ "$pod_ready" = "True" ]; then
  pass 1 "mcpserver/calculator phase=Ready url=$("${K[@]}" get mcpserver calculator -o jsonpath='{.status.url}')"
else
  fail 1 "phase='${phase}' pod.image='${pod_image}' pod.ready='${pod_ready}' after ${READY_TIMEOUT}s"
fi

# ---------- gate 2: initContainer + emptyDir survived, init Completed ----------
pod_json="$("${K[@]}" get pod calculator-0 -o json)"
g2="$(python3 - "$pod_json" <<'PY'
import json, sys
pod = json.loads(sys.argv[1])
spec, status = pod["spec"], pod["status"]
inits = [c for c in spec.get("initContainers", []) if c["name"] == "fetch-spec"]
vols = {v["name"]: v for v in spec.get("volumes", [])}
mcp = [c for c in spec["containers"] if c["name"] == "mcp"]
problems = []
if not inits:
    problems.append("initContainer fetch-spec missing")
if "spec" not in vols or "emptyDir" not in vols["spec"]:
    problems.append("volume spec/emptyDir missing")
if "tmp" not in vols or "emptyDir" not in vols["tmp"]:
    problems.append("volume tmp/emptyDir missing")
if not mcp:
    problems.append("container mcp missing")
else:
    mounts = {m["name"]: m["mountPath"] for m in mcp[0].get("volumeMounts", [])}
    if mounts.get("spec") != "/config":
        problems.append(f"mcp volumeMounts lost: {mounts}")
    if mcp[0].get("imagePullPolicy") != "IfNotPresent":
        problems.append(f"mcp imagePullPolicy={mcp[0].get('imagePullPolicy')}")
ist = [s for s in status.get("initContainerStatuses", []) if s["name"] == "fetch-spec"]
term = (ist[0].get("state", {}).get("terminated") or {}) if ist else {}
if term.get("reason") != "Completed":
    problems.append(f"fetch-spec state={ist[0].get('state') if ist else None}")
sc = mcp[0].get("securityContext", {}) if mcp else {}
print("OK" if not problems else "; ".join(problems))
print(f"info: mcp.image={mcp[0]['image'] if mcp else None} pullPolicy={mcp[0].get('imagePullPolicy') if mcp else None} "
      f"readOnlyRootFilesystem={sc.get('readOnlyRootFilesystem')} runAsUser={sc.get('runAsUser')} "
      f"init.exitCode={term.get('exitCode')} restarts={ist[0].get('restartCount') if ist else None}")
PY
)"
g2_line="$(echo "$g2" | head -1)"
echo "$g2" | tail -n +2
if [ "$g2_line" = "OK" ]; then
  pass 2 "initContainer fetch-spec Completed, emptyDir volumes and mcp volumeMounts preserved"
else
  fail 2 "$g2_line"
fi

# ---------- gate 3: spec fetched with token; never served without ----------
stub_logs="$(app_logs stub-spec-server 500)"
echo "$stub_logs" | grep -E 'auth=' | sed 's/^/  stub: /' || true
if ! echo "$stub_logs" | grep -qE 'GET /specs/calculator/v1/openapi.yaml auth=present status=200'; then
  fail 3 "no 'auth=present status=200' spec fetch in stub-spec-server logs"
fi
if ! echo "$stub_logs" | grep -qE 'GET /specs/calculator/v1/profile.json auth=present status=200'; then
  fail 3 "no 'auth=present status=200' profile fetch in stub-spec-server logs"
fi
if echo "$stub_logs" | grep -E 'auth=(missing|invalid)' | grep -q 'status=200'; then
  fail 3 "spec was served with auth=missing/invalid"
fi
pass 3 "spec and profile fetched with Bearer token; no unauthenticated 200"

# ---------- gate 4: initialize + tools/list through the proxy ----------
start_pf() {
  if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; fi
  "${K[@]}" port-forward svc/mcp-calculator-proxy "${LOCAL_PORT}:8080" >/dev/null 2>&1 &
  PF_PID=$!
  for ((j = 0; j < 30; j++)); do
    if (echo >"/dev/tcp/127.0.0.1/${LOCAL_PORT}") 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}
echo ">> port-forward svc/mcp-calculator-proxy ${LOCAL_PORT}:8080"
start_pf || true

# calculator-0 을 다시 만들면 proxyrunner 컨테이너가 재시작한다(restartCount 가 는다). 그 순간
# 기존 port-forward 는 조용히 끊기므로 실패할 때마다 port-forward 를 다시 열고 최대 30초(2초 간격) 재시도한다.
list_out=""; list_ok=0
for ((i = 0; i < 15; i++)); do
  if list_out="$("${MCP_CLIENT[@]}" list "$MCP_URL" 2>&1)"; then
    list_ok=1; break
  fi
  echo "  tools/list not ready yet (attempt $((i + 1))/15), re-opening port-forward and retrying in 2s"
  sleep 2
  start_pf || true
done
if [ "$list_ok" != 1 ]; then echo "$list_out"; fail 4 "MCP initialize/tools/list failed after 30s"; fi
echo "  tools/list: $list_out"
# profile.json says tools: [Add], so the runtime must not even create Divide
if echo "$list_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["tools"] == ["Add"] else 1)'; then
  pass 4 "tools/list is exactly [Add] (Divide excluded by profile.json)"
else
  fail 4 "tools/list is not exactly [Add]"
fi

# ---------- gate 5: tools/call Add {a:3,b:4} -> 7, hits mock-soap ----------
call_out="$("${MCP_CLIENT[@]}" call "$MCP_URL" Add '{"a": 3, "b": 4}' 2>&1)" || {
  echo "$call_out"; fail 5 "tools/call Add failed"; }
echo "  tools/call: $call_out"
if ! echo "$call_out" | python3 -c '
import json, sys
d = json.load(sys.stdin)["call"]
blob = json.dumps(d["structuredContent"]) + " " + " ".join(c or "" for c in d["content"])
sys.exit(0 if (not d["isError"] and "7" in blob) else 1)'; then
  fail 5 "Add(3,4) did not return 7"
fi
soap_logs="$(app_logs mock-soap 200)"
echo "$soap_logs" | grep -E 'POST /calc' | sed 's/^/  soap: /' || true
if ! echo "$soap_logs" | grep -qE 'POST /calc'; then
  fail 5 "mock-soap has no POST /calc log line"
fi
pass 5 "Add(3,4)=7 and mock-soap received POST /calc"

# ---------- gate 6: tools/call Divide is rejected as unknown tool (P3 F1) ----------
div_out="$("${MCP_CLIENT[@]}" call "$MCP_URL" Divide '{"numerator": 1, "denominator": 2}' 2>&1)" && {
  echo "$div_out"; fail 6 "tools/call Divide succeeded but must be rejected"; }
echo "  tools/call: $div_out"
if ! echo "$div_out" | python3 -c '
import json, sys
d = json.load(sys.stdin)["call"]
blob = " ".join(c or "" for c in d["content"]).lower()
sys.exit(0 if (d["isError"] and "unknown tool" in blob) else 1)'; then
  fail 6 "Divide was not rejected as unknown tool"
fi
soap_calls="$(app_logs mock-soap 200 | grep -c 'POST /calc' || true)"
prev_calls="$(echo "$soap_logs" | grep -c 'POST /calc' || true)"
if [ "$soap_calls" != "$prev_calls" ]; then
  fail 6 "mock-soap received a request for Divide (POST /calc count $prev_calls -> $soap_calls)"
fi
pass 6 "Divide rejected as unknown tool; mock-soap received no extra POST /calc"

echo
echo "ALL GATES PASS"
