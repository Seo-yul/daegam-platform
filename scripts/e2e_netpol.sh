#!/usr/bin/env bash
# P6 F3a: prove that deploy/dev/tenant-networkpolicy.yaml is ENFORCED on a cluster with a
# policy engine. OrbStack (Flannel) does not enforce NetworkPolicy, so this builds a
# throwaway kind cluster with Calico and never touches the orbstack context (it uses a
# private kubeconfig file).
#
#   make e2e-netpol            create kind cluster daegam-netpol + Calico, run checks, delete
#   KEEP=1 make e2e-netpol     keep the cluster (kubeconfig path is printed)
# Env: CALICO_VERSION (default v3.30.3), KIND_NODE_IMAGE (optional pin), CLUSTER (daegam-netpol)
#
# The cluster has a control-plane AND a worker node, and the target pod is pinned to the worker,
# so the API server (host network on the control-plane) reaches it across nodes exactly like a
# remote cluster's services/proxy traffic does (D21). Checks 6/7 measure that path.
#
# P8 F7 exposure checks (7~10): the platform's `daegam-expose-<k8s.name>` NetworkPolicy is rendered
# by platform/daegam/k8s/templates/exposure.py:render_expose_policy (via `uv run`) and applied next to the
# tenant policy. A pod labelled like a ToolHive MCPRemoteProxy front pod
# (app.kubernetes.io/name=mcpremoteproxy, app.kubernetes.io/instance=expose-<k8s.name>) must be
# reachable from the ingress-controller namespace (np-ctl) and from nowhere else.
#
# P30 (D52) CHECK 12·13: 대감 차트(charts/daegam)가 렌더한 NetworkPolicy daegam-ui 를 helm template --show-only 로 떼어 와
# daegam-system 에 건다.
# app=daegam-ui 라벨을 단 파드(ui-curl)는 테넌트 파드에 이름으로도 IP 로도 닿지 못하고, 같은 네임스페이스의 다른 파드는
# 그대로 닿아야 한다.
#
# P39b CHECK 14~18: 플랫폼 코드가 렌더링한 NetworkPolicy daegam-web-fetch(k8s/templates/web_tools.py)를 np-a 에 건다.
# 라벨 daegam.devops.ai.kr/web-tool=fetch 를 단 파드는 kube-dns 로 이름을 풀고 공인 443 에 닿지만, 허용 포트 위의 파드 IP·노드 IP 와
# kubernetes.default.svc 에는 닿지 못한다. 라벨 없는 파드는 그대로다. kind 에는 169.254.169.254 에 답하는 것이 없어
# 메타데이터 주소는 렌더링한 except 에 그 범위가 있는지로 본다.
#
# T67 CHECK 19~21: 플랫폼 코드가 렌더링한 네임스페이스 정책 묶음(services/tenancy.py namespace_policies)을 np-a 에 건다.
# 백엔드 라벨 daegam.devops.ai.kr/mcp-backend 를 단 파드에는 오퍼레이터의 프록시 라벨 app.kubernetes.io/name=mcpserver 를 단 같은
# 네임스페이스 파드만 닿는다. 라벨 없는 파드와 daegam-system 은 닿지 못하고, 다른 파드는 테넌트 정책 그대로다. 백엔드의 kubelet 프로브는
# 노드에서 오므로 파드는 Ready 로 남는다.
#
# T75 CHECK 22~24: 클러스터의 스위치(block_metadata)를 켠 것처럼 egress 정책 daegam-tenant-egress 를 더한 묶음을 렌더해 건다
# (calico-node 는 확인된 컨트롤러다). 그 정책은 메타데이터 주소만 막는다. 그 정책이 고른 파드는 파드 IP·노드
# IP·API 서버에 그대로 닿고, 나가는 길을 이미 좁힌 fetch 파드는 고르지 않아 합집합이 그 파드를 열지 않는다. kind 에는
# 169.254.169.254 에 답하는 것이 없어 막는 주소는 렌더링한 except 로 본다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CLUSTER="${CLUSTER:-daegam-netpol}"
CALICO_VERSION="${CALICO_VERSION:-v3.30.3}"
CALICO_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-}"
KEEP="${KEEP:-0}"
POD_SUBNET="${POD_SUBNET:-10.244.0.0/16}"
POLICY="$ROOT/deploy/dev/tenant-networkpolicy.yaml"
# 노출 CHECK: 앞단 파드 흉내(라벨만 ToolHive 와 같다), 컨트롤러 네임스페이스, 렌더링한 expose 정책
FRONT_K8S_NAME="mcp-calc"                    # MCP Server 의 k8s.name (mcp- 접두사 포함)
FRONT_NAME="expose-${FRONT_K8S_NAME}"        # 앞단 MCPRemoteProxy CR 이름
EXPOSE_POLICY="daegam-expose-${FRONT_K8S_NAME}"
CTL_NS="np-ctl"                              # Ingress 컨트롤러 네임스페이스 역할
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-netpol.XXXXXX")"
KUBECONFIG_FILE="$WORK/kubeconfig"
K=(kubectl --kubeconfig "$KUBECONFIG_FILE")
FAILED=0

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo ">> KEEP=1: cluster '$CLUSTER' kept. kubeconfig: $KUBECONFIG_FILE"
    echo ">>   kubectl --kubeconfig $KUBECONFIG_FILE get pods -A ; kind delete cluster --name $CLUSTER"
  else
    echo ">> deleting kind cluster '$CLUSTER'"
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
on_exit cleanup

pass() { echo "CHECK $1 PASS: $2"; }
# CHECK 는 하나가 깨져도 끝까지 돌아야 정책의 전모가 보인다. 그래서 세고 계속한다 (종료 코드 계약은 lib/e2e_report.sh)
record_fail() { echo "CHECK $1 FAIL: $2"; FAILED=$((FAILED + 1)); }

# die MSG 는 여기에 있다
. "$HERE/lib/die.sh"

if ! command -v kind >/dev/null 2>&1; then
  gate_incomplete "kind is not installed. On macOS: brew install kind   (https://kind.sigs.k8s.io)"
fi
docker info >/dev/null 2>&1 || gate_incomplete "docker is not running"
command -v helm >/dev/null 2>&1 || gate_incomplete "helm is not installed. On macOS: brew install helm"

# CHECK 12·13 이 걸 UI 의 NetworkPolicy. 클러스터를 만들기 전에 찍어 빨리 멈춘다. 클러스터 없는 렌더라 Secret 을 모두
# existingSecret 으로 적은 ci/offline-values.yaml 로 찍는다 (P85 5절)
helm template daegam "$ROOT/charts/daegam" -n daegam-system -f "$ROOT/charts/daegam/ci/offline-values.yaml" \
  --show-only templates/ui.yaml >"$WORK/ui.yaml"
python3 - "$WORK/ui.yaml" "$WORK/ui-policy.yaml" <<'PY'
import sys
docs = ("\n" + open(sys.argv[1]).read()).split("\n---\n")
pol = [d for d in docs if "\nkind: NetworkPolicy\n" in "\n" + d + "\n"]
assert len(pol) == 1, "templates/ui.yaml of the chart must render exactly one NetworkPolicy"
open(sys.argv[2], "w").write(pol[0])
PY
echo ">> kind $(kind version | awk '{print $2}'), calico ${CALICO_VERSION}, cluster ${CLUSTER}"

# ---------- cluster with the default CNI disabled ----------
cat >"$WORK/kind.yaml" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${CLUSTER}
networking:
  disableDefaultCNI: true
  # NOT 192.168.0.0/16: on OrbStack docker the kind node network is 192.168.97.0/24 and an
  # overlapping pod CIDR breaks every cross-node path (measured: worker pods could not reach
  # the API server). kind's default range is safe.
  podSubnet: ${POD_SUBNET}
nodes:
  - role: control-plane
  - role: worker
YAML
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo ">> reusing existing kind cluster $CLUSTER"
  kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE"
else
  args=(create cluster --config "$WORK/kind.yaml" --kubeconfig "$KUBECONFIG_FILE" --wait 0s)
  [ -n "$KIND_NODE_IMAGE" ] && args+=(--image "$KIND_NODE_IMAGE")
  kind "${args[@]}"
fi

# ---------- Calico ----------
echo ">> installing Calico ${CALICO_VERSION}"
curl -fsSL "$CALICO_URL" -o "$WORK/calico.yaml"
# pin the IP pool to the kind podSubnet (the manifest's commented default is 192.168.0.0/16)
python3 - "$WORK/calico.yaml" "$POD_SUBNET" <<'PY'
import re, sys
p, cidr = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r'# - name: CALICO_IPV4POOL_CIDR\n\s*#   value: "[^"]*"',
           f'- name: CALICO_IPV4POOL_CIDR\n              value: "{cidr}"', s)
open(p, "w").write(s)
PY
grep -q "CALICO_IPV4POOL_CIDR" "$WORK/calico.yaml" || die "calico.yaml: CALICO_IPV4POOL_CIDR not patched"
"${K[@]}" apply -f "$WORK/calico.yaml" >/dev/null
"${K[@]}" -n kube-system rollout status daemonset/calico-node --timeout=300s
"${K[@]}" -n kube-system rollout status deploy/calico-kube-controllers --timeout=300s
"${K[@]}" wait node --all --for=condition=Ready --timeout=120s >/dev/null
"${K[@]}" -n kube-system rollout status deploy/coredns --timeout=120s
echo ">> policy engine: $("${K[@]}" -n kube-system get ds calico-node -o jsonpath='{.metadata.name} ready={.status.numberReady}')"

# ---------- expose policy rendered by the platform code (before the cluster, fails fast) ----------
(cd "$ROOT/platform" && uv run --quiet python - "$FRONT_K8S_NAME" "$FRONT_NAME" "$CTL_NS" "$WORK/expose-policy.json" <<'PY')
import json, sys
from daegam.k8s.templates import expose_policy_name, render_expose_policy
k8s_name, front, ctl_ns, out = sys.argv[1:5]
doc = render_expose_policy(
    tenant="np", instance_id="e2e-netpol", resource="mcp-instance-expose-policy",
    name=expose_policy_name(k8s_name), namespace="np-a",
    front_name=front, front_kind="mcpremoteproxy", controller_namespace=ctl_ns,
)
json.dump(doc, open(out, "w"), indent=1)
PY
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["metadata"]["name"]==sys.argv[2], d["metadata"]; print("  rendered", d["metadata"]["name"], "podSelector", d["spec"]["podSelector"]["matchLabels"], "from", d["spec"]["ingress"][0]["from"])' "$WORK/expose-policy.json" "$EXPOSE_POLICY"

# ---------- workloads ----------
for ns in np-a np-b daegam-system "$CTL_NS"; do
  "${K[@]}" create namespace "$ns" --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
done
# 앞단 파드 흉내: ToolHive MCPRemoteProxy 파드와 같은 라벨의 nginx (실측 라벨, orbstack ToolHive v0.51.2)
"${K[@]}" -n np-a apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: front, labels: {app.kubernetes.io/name: mcpremoteproxy, app.kubernetes.io/instance: ${FRONT_NAME}}}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: mcpremoteproxy, app.kubernetes.io/instance: ${FRONT_NAME}}}
  template:
    metadata: {labels: {app.kubernetes.io/name: mcpremoteproxy, app.kubernetes.io/instance: ${FRONT_NAME}}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${CLUSTER}-worker}
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports: [{containerPort: 80}]
          readinessProbe: {httpGet: {path: /, port: 80}, periodSeconds: 2}
---
apiVersion: v1
kind: Service
metadata: {name: mcp-${FRONT_NAME}-remote-proxy}
spec:
  selector: {app.kubernetes.io/name: mcpremoteproxy, app.kubernetes.io/instance: ${FRONT_NAME}}
  ports: [{port: 8080, targetPort: 80}]
YAML
"${K[@]}" -n np-a apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, labels: {app: web}}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      # pin to the worker: the API server proxy must cross nodes (D21 measurement)
      nodeSelector: {kubernetes.io/hostname: ${CLUSTER}-worker}
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports: [{containerPort: 80}]
          readinessProbe: {httpGet: {path: /, port: 80}, periodSeconds: 2}
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 80}]
YAML
for ns in np-a np-b daegam-system "$CTL_NS"; do
  "${K[@]}" -n "$ns" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: curl, labels: {app: curl}}
spec:
  containers:
    - name: curl
      image: curlimages/curl:8.14.1
      command: [sleep, infinity]
YAML
done
"${K[@]}" -n np-a rollout status deploy/web --timeout=180s
"${K[@]}" -n np-a rollout status deploy/front --timeout=180s
for ns in np-a np-b daegam-system "$CTL_NS"; do
  "${K[@]}" -n "$ns" wait pod/curl --for=condition=Ready --timeout=180s >/dev/null
done
FRONT_URL="http://mcp-${FRONT_NAME}-remote-proxy.np-a.svc:8080/"
WEB_IP="$("${K[@]}" -n np-a get pod -l app=web -o jsonpath='{.items[0].status.podIP}')"
WEB_NODE="$("${K[@]}" -n np-a get pod -l app=web -o jsonpath='{.items[0].spec.nodeName}')"
echo ">> web pod IP $WEB_IP on node $WEB_NODE, service web.np-a.svc"
"${K[@]}" get nodes -o wide | sed 's/^/   /'

# API server services/proxy -> web (what the platform does for a REMOTE cluster, D21).
# prints "200" when the proxied GET succeeds, otherwise the apiserver's error class.
probe_proxy() {
  local out
  if out="$("${K[@]}" get --raw /api/v1/namespaces/np-a/services/http:web:80/proxy/ --request-timeout=20s 2>&1)"; then
    echo 200
  else
    echo "$out" | grep -o 'ServiceUnavailable\|timeout\|refused\|Forbidden\|unreachable' | head -1 || echo "error"
  fi
}
# source IP the web pod saw on its last request (nginx access log)
last_client_ip() {
  "${K[@]}" -n np-a logs deploy/web --tail=1 2>/dev/null | awk '{print $1}'
}

# probe NS URL [POD] -> http code or 000 (timeout / refused). POD defaults to the curl pod
probe() {
  local out
  # curl writes 000 itself on timeout/refused (and exits non-zero); never double up
  out="$("${K[@]}" -n "$1" exec "${3:-curl}" -- curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$2" 2>/dev/null || true)"
  echo "${out:-000}"
}
# wait until probe NS URL returns the expected code (Calico takes a few seconds to program)
expect() { # label ns url want [pod]. label 은 부르는 줄을 읽기 쉽게 하려는 것이라 쓰지 않는다
  local ns="$2" url="$3" want="$4" pod="${5:-curl}" got=""
  for _ in $(seq 1 12); do
    got="$(probe "$ns" "$url" "$pod")"
    [ "$got" = "$want" ] && break
    sleep 5
  done
  echo "  $ns${5:+/$5} -> $url : $got (want $want)"
  [ "$got" = "$want" ]
}

# ---------- baseline: everything reachable ----------
echo ">> baseline (no policy)"
ok=1
expect base np-b http://web.np-a.svc/ 200 || ok=0
expect base np-a http://web.np-a.svc/ 200 || ok=0
expect base daegam-system http://web.np-a.svc/ 200 || ok=0
expect base "$CTL_NS" "$FRONT_URL" 200 || ok=0
[ "$ok" = 1 ] && pass 0 "without a policy np-a, np-b, daegam-system reach web.np-a.svc and $CTL_NS reaches the front pod" \
  || record_fail 0 "baseline connectivity broken (cluster/CNI problem, not the policy)"
got="$(probe_proxy)"; PROXY_SRC="$(last_client_ip)"
echo "  apiserver services/proxy -> web.np-a : $got (source IP seen by the pod: ${PROXY_SRC:-?})"
[ "$got" = 200 ] && pass 0b "apiserver services/proxy reaches web on the worker (cross-node)" \
  || record_fail 0b "apiserver services/proxy cannot reach web even without a policy: $got"

# ---------- apply the tenant policy to np-a ----------
echo ">> applying $POLICY to np-a"
"${K[@]}" -n np-a apply -f "$POLICY"
"${K[@]}" -n np-a get networkpolicy daegam-tenant-ingress -o jsonpath='  netpol: {.metadata.name} policyTypes={.spec.policyTypes} from={.spec.ingress[0].from}{"\n"}'

if expect 1 np-b http://web.np-a.svc/ 000; then pass 1 "np-b -> web.np-a.svc is blocked"; else record_fail 1 "np-b still reaches np-a (policy not enforced?)"; fi
if expect 2 np-b "http://${WEB_IP}/" 000; then pass 2 "np-b -> pod IP ${WEB_IP} is blocked"; else record_fail 2 "np-b still reaches the pod IP"; fi
if expect 3 np-a http://web.np-a.svc/ 200; then pass 3 "np-a -> web.np-a.svc (same namespace) allowed"; else record_fail 3 "same-namespace traffic blocked"; fi
if expect 4 daegam-system http://web.np-a.svc/ 200; then pass 4 "daegam-system -> web.np-a.svc allowed via namespaceSelector"; else record_fail 4 "daegam-system traffic blocked"; fi

# D21: the API server proxy comes from the control-plane host, not from a pod, so the
# namespaceSelector rules do not cover it. Measure what actually happens under Calico.
got="$(probe_proxy)"
echo "  apiserver services/proxy -> web.np-a (tenant policy applied) : $got"
if [ "$got" = 200 ]; then
  pass 6 "apiserver services/proxy still reaches web with the tenant policy (source ${PROXY_SRC:-?})"
else
  record_fail 6 "apiserver services/proxy is BLOCKED by the tenant policy: $got (source ${PROXY_SRC:-?})"
fi

# egress from np-a must stay open (no egress policy). kubernetes.default.svc answers 403
# over https without credentials; any HTTP answer (not 000) proves egress works.
if expect 5 np-a https://kubernetes.default.svc/ 403; then
  pass 5 "np-a egress (https://kubernetes.default.svc -> 403) still open"
else
  record_fail 5 "np-a egress blocked"
fi

# ---------- P8 F7: exposure policy daegam-expose-<k8s.name> ----------
# 테넌트 정책만 있으면 컨트롤러 네임스페이스도 막힌다. expose 정책을 얹으면 앞단 파드만 열리고
# 다른 네임스페이스와 같은 네임스페이스의 다른 파드(web)는 그대로다 (정책 union).
echo ">> tenant policy only: $CTL_NS must be blocked from the front pod"
if expect 7 "$CTL_NS" "$FRONT_URL" 000; then pass 7 "$CTL_NS -> front pod blocked by the tenant policy alone"; else record_fail 7 "$CTL_NS reaches the front pod without the expose policy"; fi
echo ">> applying rendered $EXPOSE_POLICY to np-a"
"${K[@]}" -n np-a apply -f "$WORK/expose-policy.json"
"${K[@]}" -n np-a get networkpolicy "$EXPOSE_POLICY" -o jsonpath='  netpol: {.metadata.name} podSelector={.spec.podSelector.matchLabels} from={.spec.ingress[0].from}{"\n"}'
if expect 8 "$CTL_NS" "$FRONT_URL" 200; then pass 8 "$CTL_NS -> front pod allowed once $EXPOSE_POLICY is applied"; else record_fail 8 "$CTL_NS still blocked with $EXPOSE_POLICY (selector/labels wrong?)"; fi
if expect 9 np-b "$FRONT_URL" 000; then pass 9 "np-b -> front pod still blocked"; else record_fail 9 "np-b reaches the front pod through the expose policy"; fi
if expect 10 "$CTL_NS" http://web.np-a.svc/ 000; then pass 10 "$CTL_NS -> web (not the front pod) still blocked"; else record_fail 10 "$CTL_NS reaches other pods in np-a (expose policy too wide)"; fi
"${K[@]}" -n np-a delete networkpolicy "$EXPOSE_POLICY" >/dev/null
if expect 11 "$CTL_NS" "$FRONT_URL" 000; then pass 11 "deleting $EXPOSE_POLICY closes $CTL_NS -> front pod again"; else record_fail 11 "$CTL_NS still reaches the front pod after deleting the expose policy"; fi

# ---------- P30 (D52): daegam-ui 파드의 egress 차단 ----------
# 테넌트 정책은 daegam-system 네임스페이스 전체를 받아들인다(CHECK 4). NetworkPolicy daegam-ui 가 app=daegam-ui 파드의 나가는
# 연결을 막아 가장 바깥에 노출된 nginx 가 테넌트 파드에 닿지 못하게 한다. 이 정책은 DNS 도 막으므로 이름과 IP 둘 다 본다
"${K[@]}" -n daegam-system apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: ui-curl, labels: {app: daegam-ui}}
spec:
  containers:
    - name: curl
      image: curlimages/curl:8.14.1
      command: [sleep, infinity]
YAML
"${K[@]}" -n daegam-system wait pod/ui-curl --for=condition=Ready --timeout=180s >/dev/null
# 정책 전에 닿는 것을 먼저 본다. 그래야 뒤의 000 이 정책 때문인 것을 안다
ui_base=1
expect 12 daegam-system http://web.np-a.svc/ 200 ui-curl || ui_base=0
echo ">> applying NetworkPolicy daegam-ui rendered from the chart"
"${K[@]}" apply -f "$WORK/ui-policy.yaml"
if [ "$ui_base" = 0 ]; then
  record_fail 12 "the app=daegam-ui pod could not reach the tenant pod even before the daegam-ui policy (baseline broken)"
elif expect 12 daegam-system http://web.np-a.svc/ 000 ui-curl && expect 12 daegam-system "http://${WEB_IP}/" 000 ui-curl; then
  pass 12 "an app=daegam-ui pod reaches the tenant pod without the daegam-ui policy and cannot by name or IP with it (egress denied)"
else
  record_fail 12 "an app=daegam-ui pod still reaches the tenant pod (daegam-ui egress policy not enforced?)"
fi
if expect 13 daegam-system http://web.np-a.svc/ 200; then
  pass 13 "other daegam-system pods still reach the tenant pod (the policy selects only app=daegam-ui)"
else
  record_fail 13 "the daegam-ui policy also blocks other daegam-system pods"
fi

# ---------- P39b: 웹 fetch 복제본의 나가는 길 ----------
# 기본 Platform Settings 로 렌더링한다. except 는 urlfetch 의 차단 표다
(cd "$ROOT/platform" && uv run --quiet python - "$WORK/fetch-policy.json" <<'PY')
import json, sys
from daegam.k8s.templates.web_tools import fetch_network_policy
from daegam.services.platform_tools import blocked_ranges
from daegam.store.settings import PlatformSettings
settings = PlatformSettings()
doc = fetch_network_policy(
    namespace="np-a", labels={}, fetch=settings.web_tools.fetch, blocked=blocked_ranges(settings)
)
json.dump(doc, open(sys.argv[1], "w"), indent=1)
PY
if python3 - "$WORK/fetch-policy.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
v4 = d["spec"]["egress"][1]["to"][0]["ipBlock"]
need = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"}
missing = need - set(v4["except"])
assert v4["cidr"] == "0.0.0.0/0" and not missing, missing
print("  rendered", d["metadata"]["name"], "ports", d["spec"]["egress"][1]["ports"])
PY
then pass 14 "the rendered daegam-web-fetch policy excepts private, CGNAT and link-local (metadata) ranges"
else record_fail 14 "the rendered daegam-web-fetch policy misses a private range"; fi
# 노드 IP 의 80 에 답하는 hostNetwork nginx 와, 라벨을 단 fetch 흉내 파드
WORKER_IP="$("${K[@]}" get node "${CLUSTER}-worker" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
"${K[@]}" -n np-b apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: node-web}
spec:
  hostNetwork: true
  nodeSelector: {kubernetes.io/hostname: ${CLUSTER}-worker}
  containers:
    - name: nginx
      image: nginx:1.27-alpine
      readinessProbe: {httpGet: {path: /, port: 80}, periodSeconds: 2}
YAML
"${K[@]}" -n np-a apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: fetch-curl, labels: {daegam.devops.ai.kr/web-tool: fetch}}
spec:
  nodeSelector: {kubernetes.io/hostname: ${CLUSTER}-worker}
  containers:
    - name: curl
      image: curlimages/curl:8.14.1
      command: [sleep, infinity]
YAML
"${K[@]}" -n np-b wait pod/node-web --for=condition=Ready --timeout=180s >/dev/null
"${K[@]}" -n np-a wait pod/fetch-curl --for=condition=Ready --timeout=180s >/dev/null
PUBLIC_URL="${NETPOL_PUBLIC_URL:-https://example.com/}"
# 정책 전에 닿는 것을 먼저 본다. 공인 주소에 닿지 않으면 인터넷이 없는 것이라 CHECK 17 은 SKIP 이다
fetch_base=1
expect 15 np-a "http://${WEB_IP}/" 200 fetch-curl || fetch_base=0
expect 15 np-a "http://${WORKER_IP}/" 200 fetch-curl || fetch_base=0
expect 15 np-a https://kubernetes.default.svc/ 403 fetch-curl || fetch_base=0
public_before="$(probe np-a "$PUBLIC_URL" fetch-curl)"
echo "  np-a/fetch-curl -> $PUBLIC_URL : $public_before (before the policy)"
echo ">> applying the rendered daegam-web-fetch policy to np-a"
"${K[@]}" -n np-a apply -f "$WORK/fetch-policy.json" >/dev/null
if [ "$fetch_base" = 0 ]; then
  record_fail 15 "the fetch pod could not reach the pod IP, node IP or API server even before the policy (baseline broken)"
elif expect 15 np-a "http://${WEB_IP}/" 000 fetch-curl && expect 15 np-a "http://${WORKER_IP}/" 000 fetch-curl \
  && expect 15 np-a https://kubernetes.default.svc/ 000 fetch-curl; then
  pass 15 "the fetch pod reaches the pod IP ${WEB_IP}, node IP ${WORKER_IP} and the API server without the policy and none of them with it"
else
  record_fail 15 "the fetch pod still reaches a private address (daegam-web-fetch not enforced?)"
fi
# 이름 풀이는 kube-dns 로 열려 있다. curl 의 종료 코드 6 은 이름을 못 푼 것이고 28 은 풀고 나서 연결이 막힌 것이다
dns_rc=0
"${K[@]}" -n np-a exec fetch-curl -- curl -s -o /dev/null --max-time 5 http://web.np-a.svc/ >/dev/null 2>&1 || dns_rc=$?
echo "  np-a/fetch-curl -> http://web.np-a.svc/ : curl exit $dns_rc (6 = no name, 28 = resolved and blocked)"
if [ "$dns_rc" = 28 ]; then pass 16 "the fetch pod resolves names through kube-dns and the resolved private address stays blocked"
else record_fail 16 "the fetch pod's name lookup or block is wrong (curl exit $dns_rc)"; fi
if [ "$public_before" = 000 ]; then
  echo "CHECK 17 SKIP: $PUBLIC_URL is not reachable from the cluster even without the policy (no internet)"
elif expect 17 np-a "$PUBLIC_URL" "$public_before" fetch-curl; then
  pass 17 "the fetch pod still reaches $PUBLIC_URL on 443 under the policy"
else
  record_fail 17 "the fetch policy blocks public 443"
fi
if expect 18 np-a "http://${WEB_IP}/" 200; then pass 18 "pods without the fetch label still reach the pod IP (the policy selects only daegam.devops.ai.kr/web-tool=fetch)"
else record_fail 18 "the fetch policy also blocks pods without the label"; fi

# ---------- T67: MCP 백엔드에는 같은 네임스페이스의 MCPServer 프록시만 ----------
# 같은 이름의 테넌트 정책(daegam-tenant-ingress)이 렌더링한 것으로 바뀌고 격리·백엔드 정책이 더해진다
# 스위치를 켜지 않은 묶음(CHECK 19~21)과 켠 묶음(CHECK 22~24)을 함께 렌더한다
(cd "$ROOT/platform" && uv run --quiet python - "$WORK/ns-policies.json" "$WORK/ns-policies-egress.json" <<'PY')
import json, sys
from daegam.services.tenancy import namespace_policies
for out, egress in ((sys.argv[1], False), (sys.argv[2], True)):
    items = namespace_policies("np", "np-a", "daegam-system", egress=egress)
    json.dump({"apiVersion": "v1", "kind": "List", "items": items}, open(out, "w"), indent=1)
PY
"${K[@]}" -n np-a apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: calc-backend}
spec:
  replicas: 1
  selector: {matchLabels: {app: calc-backend}}
  template:
    metadata: {labels: {app: calc-backend, daegam.devops.ai.kr/mcp-backend: calc}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${CLUSTER}-worker}
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports: [{containerPort: 80}]
          readinessProbe: {httpGet: {path: /, port: 80}, periodSeconds: 2}
---
apiVersion: v1
kind: Service
metadata: {name: mcp-calc}
spec:
  selector: {app: calc-backend}
  ports: [{port: 8000, targetPort: 80}]
---
apiVersion: v1
kind: Pod
metadata: {name: proxy-curl, labels: {app.kubernetes.io/name: mcpserver}}
spec:
  containers:
    - name: curl
      image: curlimages/curl:8.14.1
      command: [sleep, infinity]
YAML
"${K[@]}" -n np-a rollout status deploy/calc-backend --timeout=180s
"${K[@]}" -n np-a wait pod/proxy-curl --for=condition=Ready --timeout=180s >/dev/null
BACKEND_URL="http://mcp-calc.np-a.svc:8000/"
# 묶음 전에는 테넌트 정책이 백엔드도 골라 같은 네임스페이스와 daegam-system 이 닿는다
backend_base=1
expect 19 np-a "$BACKEND_URL" 200 || backend_base=0
expect 19 daegam-system "$BACKEND_URL" 200 || backend_base=0
echo ">> applying the rendered namespace policy set to np-a"
"${K[@]}" -n np-a apply -f "$WORK/ns-policies.json" >/dev/null
"${K[@]}" -n np-a get networkpolicy -o name | sed 's/^/   /'
if [ "$backend_base" = 0 ]; then
  record_fail 19 "np-a and daegam-system could not reach the backend pod even before the set (baseline broken)"
elif expect 19 np-a "$BACKEND_URL" 000 && expect 19 daegam-system "$BACKEND_URL" 000 \
  && expect 19 np-a "$BACKEND_URL" 200 proxy-curl; then
  pass 19 "the backend pod admits the proxy-labelled pod and neither an unlabelled np-a pod nor daegam-system"
else
  record_fail 19 "the backend pod is reachable around the proxy, or the proxy-labelled pod is blocked (daegam-mcp-backend not enforced?)"
fi
if expect 20 np-a http://web.np-a.svc/ 200 && expect 20 daegam-system http://web.np-a.svc/ 200 \
  && expect 20 np-b http://web.np-a.svc/ 000; then
  pass 20 "other np-a pods keep the tenant policy (np-a and daegam-system reach web, np-b does not)"
else
  record_fail 20 "the set changed what the tenant policy admits to pods without the backend label"
fi
sleep 10
backend_ready="$("${K[@]}" -n np-a get pod -l app=calc-backend -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}')"
if [ "$backend_ready" = True ]; then pass 21 "the backend pod stays Ready under the set (the kubelet probe comes from the node)"
else record_fail 21 "the backend pod lost Ready under the set (Ready=$backend_ready)"; fi

# ---------- T75: 묶음의 egress 정책은 메타데이터 주소만 막는다 ----------
echo ">> turning the metadata block on: applying the rendered set with daegam-tenant-egress to np-a"
"${K[@]}" -n np-a apply -f "$WORK/ns-policies-egress.json" >/dev/null
if expect 22 np-a "http://${WEB_IP}/" 200 && expect 22 np-a "http://${WORKER_IP}/" 200 \
  && expect 22 np-a https://kubernetes.default.svc/ 403 && expect 22 np-a http://web.np-a.svc/ 200; then
  pass 22 "a pod under daegam-tenant-egress still reaches a pod IP, the node IP ${WORKER_IP}, the API server and names through kube-dns"
else
  record_fail 22 "daegam-tenant-egress blocks more than the metadata address"
fi
if expect 23 np-a "http://${WEB_IP}/" 000 fetch-curl && expect 23 np-a "http://${WORKER_IP}/" 000 fetch-curl \
  && expect 23 np-a https://kubernetes.default.svc/ 000 fetch-curl; then
  pass 23 "the fetch pod stays narrowed: daegam-tenant-egress does not select it, so the union opens nothing"
else
  record_fail 23 "the fetch pod gained egress beside daegam-web-fetch (daegam-tenant-egress selects it?)"
fi
if python3 - "$WORK/ns-policies-egress.json" "$WORK/ns-policies.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
# 스위치를 켜지 않은 묶음에는 egress 정책이 없다
assert all(p["metadata"]["name"] != "daegam-tenant-egress" for p in json.load(open(sys.argv[2]))["items"])
guard = next(p for p in items if p["metadata"]["name"] == "daegam-tenant-egress")
blocks = [peer["ipBlock"] for peer in guard["spec"]["egress"][0]["to"] if "ipBlock" in peer]
excepts = sorted(e for b in blocks for e in b.get("except", []))
assert excepts == ["169.254.169.254/32", "fd00:ec2::254/128"], excepts
assert {"key": "daegam.devops.ai.kr/web-tool", "operator": "DoesNotExist"} in guard["spec"]["podSelector"]["matchExpressions"]
print("  rendered daegam-tenant-egress excepts", excepts)
PY
then pass 24 "the rendered daegam-tenant-egress excepts only the metadata addresses and leaves out fetch pods"
else record_fail 24 "the rendered daegam-tenant-egress is not the metadata-only policy"; fi

echo
if [ "$FAILED" -gt 0 ]; then echo "E2E-NETPOL FAIL ($FAILED checks)"; exit 1; fi
gate_done
echo "E2E-NETPOL PASS (calico ${CALICO_VERSION} enforces deploy/dev/tenant-networkpolicy.yaml, ${EXPOSE_POLICY}, NetworkPolicy daegam-ui, daegam-web-fetch and the namespace policy set with its egress policy)"
