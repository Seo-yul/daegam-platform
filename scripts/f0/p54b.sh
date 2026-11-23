#!/usr/bin/env bash
# P54b F0.4 의 측정과 F1 리허설 (docs/plan/phase-54b-remote-entries.md 11절). .github/workflows/p54-f0.yml 이
# k3d 에서 부른다. 이 파일과 scripts/f0 의 나머지, 그 워크플로는 F0 측정 동안만 둔다.
#
#   scripts/f0/p54b.sh f0                             F0.4 를 잰다
#   scripts/f0/p54b.sh f1 prepare|watch|check-new|check-old
#                                                     F1 리허설의 한 단계. 워크플로가 두 설치를 오가며 부른다
#
# 전제는 scripts/f0/p54.sh 와 같다(k3d, make deploy-local·lab-up, Traefik, ToolHive, mock-oidc). 환경: KUBE_CONTEXT
# (k3d-daegam) F0_OUT F1_STATE(f1 만) F1_CONVERT_TIMEOUT(check-new 가 변환을 기다리는 초, 1200)
#
# F0.4  oidc 모드에서 mock-oidc 토큰의 사람(f0-person)으로 잰다. 한 테넌트에 같은 서버를 가리키는 remote 항목(MCPRemoteProxy
#       를 거친다)과 entry 항목(Gateway 의 Entry 가 바로 부른다)을 서버마다 하나씩 둔다. 서버는 lab 의 spike 서버(daegam-spike
#       의 calculator MCPServer, streamable HTTP)와 시험 서버(scripts/f0/f0_server.py)의 streamable HTTP(:8080/mcp)와
#       SSE(:8081/sse)다. 모두 클러스터 안 주소라 Entry 에 allowPrivateEndpoint 가 선다. 항목마다 priority 모드의 Gateway
#       하나를 두고 그 vMCP 에 port-forward 로 붙어 같은 행(f0_tool probe)을 잰 뒤 remote 와 entry 를 견준다. 그 밖에
#       Entry 의 remoteUrl·transport·allowPrivateEndpoint, 감사 이벤트(vMCP 와 remote 프록시 파드의 로그), 항목마다의
#       refresh-tools 와 도구 탭의 라이브 목록(GET /hub/{id}/tools)을 웹 가져오기 주소 규칙의 갈래별로(다른 네임스페이스의 Service,
#       테넌트 네임스페이스의 Service, 클러스터 Service 가 아닌 사설 IP 둘) 찍는다. 끝으로 시험 서버를 0 으로 내린 동안
#       Gateway 의 상태 행과 vMCP 의 답, remote 항목의 상태를 찍고, 다시 올린 뒤 호출이 돌아오기까지의 초를 잰다
# F1    11절의 리허설(header 모드). prepare 는 이전 판의 설치에서 lab calculator 의 remote 항목 R 을 다른 테넌트에도
#       열고, R 을 쓰는 Gateway 를 R 의 테넌트(GA)와 다른 테넌트(GB, 복제본)에 두고, GA 를 proxy 모드로 게시한 항목을 쓰는
#       Gateway GC 를 둔다. 셋 다 노출해 Traefik 으로 부른다. watch 는 셋마다 호출 루프를 돌린다. check-new 는 B1 의
#       설치에서 hub-·shared- MCPRemoteProxy 가 모두 사라지기를 기다린 뒤, check-old 는 이전 판으로 되돌린 설치에서 루프를
#       멈추고 실패한 호출, Tool 이름, 호출, 항목의 kind 와 게시 방식, vMCP 의 감사 이벤트를 찍는다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
. "$ROOT/scripts/lib/e2e_exit.sh"
. "$ROOT/scripts/lib/e2e_json.sh"
CTX="${KUBE_CONTEXT:-k3d-daegam}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-f0-p54b.XXXXXX")"
# F0 도구의 포트. lib/e2e_ports.sh 의 표에 없는 자리이고 F0 도구와 함께 사라진다. vMCP 는 VMCP_PORT_BASE 부터 여섯이다
API_PORT="${E2E_API_PORT:-19092}"; OIDC_PORT="${E2E_OIDC_PORT:-19093}"; VMCP_PORT_BASE="${E2E_VMCP_PORT:-19094}"
TENANT="${F0_TENANT:-f0-entry}"
MCP_CLIENT_NAME="daegam-f0-p54b"
. "$HERE/lib.sh"

MCP_GROUP="mcp-$TENANT"
SPIKE_URL="http://mcp-calculator-proxy.${SPIKE_NS}.svc.cluster.local:8080/mcp"
# 서버마다의 URL 과 전송 (F0.4)
SERVERS=(spike http sse)
server_target() { # NAME -> URL TRANSPORT
  case "$1" in
    spike) echo "$SPIKE_URL streamable-http" ;;
    http) echo "$F0_HTTP/mcp streamable-http" ;;
    sse) echo "$F0_SSE/sse sse" ;;
  esac
}
KINDS=(remote entry)
PFS=(); PFS_PORTS=()
PERSON=""; INTRUDER=""   # mock-oidc 가 낸 사람(테넌트 그룹)과 다른 그룹의 토큰 (F0.4)

cleanup() {
  local i
  for i in "${!PFS[@]}"; do stop_pf "${PFS[$i]}" "${PFS_PORTS[$i]}"; done
  f0_restore_mode
  rm -rf "$WORK"
}
on_exit cleanup

command -v uv >/dev/null || gate_incomplete "uv is not installed (the F0 tool runs in the platform environment)"

# ---------- 공통 ----------

# gateway STEP NAME BACKEND_ITEM_ID [DEPLOY_JSON] -> GW_ID·GW_K8S·GW_NS·GW_PHASE. Ready 가 아니어도 멈추지 않고 그
# phase 를 남긴다(측정이다)
gateway() {
  local i code body="${4:-}"
  [ -n "$body" ] || body='{}'
  save_doc "$1" /gateways "$2" "{\"name\": \"$2\", \"conflict_resolution\": \"priority\", \"backends\": [{\"hub_item_id\": \"$3\"}]}"
  GW_ID="$SAVED_ID"; GW_PHASE=""
  code="$(api POST "/gateways/$GW_ID/deploy" "$body")"
  case "$code" in
    201) ;;
    409) [ "$(api POST "/gateways/$GW_ID/redeploy")" = 200 ] || { GW_PHASE="redeploy failed"; return 0; } ;;
    *) GW_PHASE="deploy -> $code $(jq_ '(d.get("detail") or {}).get("code")' 2>/dev/null || true)"; return 0 ;;
  esac
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    if [ "$(api GET "/gateways/$GW_ID")" = 200 ]; then
      GW_PHASE="$(jq_ '(d.get("status") or {}).get("phase")')"
      [ "$GW_PHASE" = Ready ] && break
    fi
    sleep 5
  done
  GW_K8S="$(jq_ '(d.get("k8s") or {}).get("name") or ""')"; GW_NS="$(jq_ '(d.get("k8s") or {}).get("namespace") or ""')"
}

# gw_pf INDEX K8S NS -> 그 vMCP 를 VMCP_PORT_BASE+INDEX 로 port-forward 하고 그 URL 을 PF_URL 에 남긴다. 정리가 닫도록
# PFS 에 더하므로 $( ) 안에서 부르지 않는다
gw_pf() {
  local port=$((VMCP_PORT_BASE + $1))
  start_pf "$3" "svc/vmcp-$2" "${port}:4483"; PFS+=("$PF_PID"); PFS_PORTS+=("$port")
  wait_port "$port" || die F0.4 "port-forward to vmcp-$2 did not open :$port"
  PF_URL="http://127.0.0.1:$port/mcp"
}

# ---------- F0.4 ----------

# pair_state LABEL -> 그 항목의 Gateway 가 어땠는지 한 마디. f0_4 안에서 부르고 그 labels·urls 를 읽는다(동적 범위)
pair_state() {
  local i
  for i in "${!labels[@]}"; do
    [ "${labels[$i]}" = "$1" ] || continue
    if [ -z "${urls[$i]}" ]; then
      echo "did not become Ready"
    elif [ ! -s "$F0_OUT/f0.4-$1.json" ]; then
      echo "was Ready but its probe failed"
    else
      echo "answered"
    fi
    return 0
  done
  echo "was not created"
}

f0_4() {
  local s k n idx url transport name item_ids=() gw_ids=() gw_k8s=() gw_ns=() urls=() labels=() code i
  local pod_ip cluster_ip t0 back tenant_ns spec r l proxy_name rc
  # oidc 모드와 사람의 토큰
  f0_oidc_session F0.4
  PERSON="$(user_token "{\"sub\": \"f0-person\", \"groups\": [\"$MCP_GROUP\"]}")" || die F0.4 "person token failed"
  INTRUDER="$(user_token '{"sub": "f0-intruder", "groups": ["mcp-f0-other"]}')" || die F0.4 "intruder token failed"

  # 항목과 Gateway: 서버마다 remote 와 entry
  idx=0
  for s in "${SERVERS[@]}"; do
    read -r url transport <<<"$(server_target "$s")"
    for k in "${KINDS[@]}"; do
      name="f0-$s-$k"
      hub_item F0.4 "$name" "$k" "$url" "$transport"
      if [ -z "$ITEM_ID" ]; then
        f0_note F0.4 "$name: POST /hub kind $k url $url transport $transport -> $ITEM_CODE $(cat "$WORK/body")"
        item_ids+=(""); gw_ids+=(""); gw_k8s+=(""); gw_ns+=(""); urls+=(""); labels+=("$name"); idx=$((idx + 1)); continue
      fi
      gateway F0.4 "f0g-$s-$k" "$ITEM_ID"
      f0_note F0.4 "$name ($k, $transport, $url):${ITEM_PHASE:+ item phase $ITEM_PHASE,} Gateway f0g-$s-$k phase $GW_PHASE"
      item_ids+=("$ITEM_ID"); gw_ids+=("$GW_ID"); gw_k8s+=("$GW_K8S"); gw_ns+=("$GW_NS"); labels+=("$name")
      PF_URL=""
      [ "$GW_PHASE" != Ready ] || gw_pf "$idx" "$GW_K8S" "$GW_NS"
      urls+=("$PF_URL")
      idx=$((idx + 1))
    done
  done

  # Entry 의 모양
  for i in "${!labels[@]}"; do
    [ -n "${gw_ns[$i]}" ] || continue
    "${K[@]}" -n "${gw_ns[$i]}" get mcpserverentry -l "daegam.devops.ai.kr/id=${gw_ids[$i]}" -o json >"$WORK/entries.json"
    f0_note F0.4 "${labels[$i]} Entry: $(json_of "$WORK/entries.json" '"; ".join("%s remoteUrl=%s transport=%s allowPrivateEndpoint=%s" % (e["metadata"]["name"], e["spec"].get("remoteUrl"), e["spec"].get("transport"), e["spec"].get("allowPrivateEndpoint")) for e in d["items"])')"
  done

  # 같은 행: 서버마다 remote 와 entry
  for i in "${!labels[@]}"; do
    [ -n "${urls[$i]}" ] || continue
    F0_TOKEN="$PERSON" F0_REFUSED_TOKEN="$INTRUDER" f0_tool probe --url "${urls[$i]}" --rows unauthenticated,session,refused \
      --out "$F0_OUT/f0.4-${labels[$i]}.json" || f0_note F0.4 "${labels[$i]}: probe failed"
    # Tool 을 하나도 알리지 않은 Gateway 는 그 백엔드를 어떻게 보는지 남긴다. 항목의 서버가 다른 네임스페이스면 그쪽 파드다
    if [ -z "$(tool_names "$F0_OUT/f0.4-${labels[$i]}.json")" ]; then
      case "${labels[$i]}" in
        f0-spike-*) vmcp_diag F0.4 "${gw_ns[$i]}" "${gw_k8s[$i]}" "app.kubernetes.io/instance=calculator" daegam-spike ;;
        *) vmcp_diag F0.4 "${gw_ns[$i]}" "${gw_k8s[$i]}" "app=f0-mcp" "$F0_NS" ;;
      esac
    fi
  done
  for s in "${SERVERS[@]}"; do
    if [ -s "$F0_OUT/f0.4-f0-$s-remote.json" ] && [ -s "$F0_OUT/f0.4-f0-$s-entry.json" ]; then
      rc=0
      f0_tool compare "$F0_OUT/f0.4-f0-$s-remote.json" "$F0_OUT/f0.4-f0-$s-entry.json" --names remote entry \
        >"$F0_OUT/f0.4-$s-compare.md" || rc=$?
      f0_block "F0.4 $s" "$F0_OUT/f0.4-$s-compare.md"
      # remote(기준)가 사람의 토큰으로 답하지 않았으면 이 서버의 견줌은 아무것도 말하지 않는다. 다른 서버는 잰다
      [ "$rc" = 0 ] || record_fail F0.4 "$s: COMPARISON INVALID, the remote item's Gateway f0g-$s-remote did not answer the person's token"
    elif [ -s "$F0_OUT/f0.4-f0-$s-remote.json" ]; then
      # 기준(remote)은 답했고 entry 쪽만 Ready 가 아니거나 답하지 않았다. P54b 11절 F0.4 의 막대가 보는 결과다(SSE 면
      # SSE remote 항목의 변환을 막는다). 잰 결과이므로 실패로 세지 않는다
      f0_note F0.4 "$s: MEASURED, the remote item's Gateway f0g-$s-remote answered and the entry item's Gateway f0g-$s-entry $(pair_state "f0-$s-entry")"
    else
      record_fail F0.4 "$s: no comparison, the remote item's Gateway f0g-$s-remote $(pair_state "f0-$s-remote") (the entry item's Gateway f0g-$s-entry $(pair_state "f0-$s-entry"))"
    fi
  done
  # SSE 항목의 실패가 시험 서버 쪽인지 가를 수 있게 시험 서버의 로그 끝을 남긴다
  f0_server_log F0.4

  # 감사 이벤트: vMCP 파드와 remote 항목의 프록시 파드
  sleep 5
  for i in "${!labels[@]}"; do
    [ -n "${gw_ns[$i]}" ] || continue
    "${K[@]}" -n "${gw_ns[$i]}" logs -l "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=${gw_k8s[$i]}" \
      --tail=-1 >"$WORK/vmcp.log" 2>/dev/null || true
    f0_tool audit "$WORK/vmcp.log" --label "vmcp ${labels[$i]}" >>"$F0_OUT/f0.4-audit.txt"
    case "${labels[$i]}" in
      *-remote)
        [ "$(api GET "/hub/${item_ids[$i]}")" = 200 ] || continue
        proxy_name="$(jq_ '(d.get("k8s") or {}).get("name") or ""')"
        "${K[@]}" -n "${gw_ns[$i]}" logs -l "app.kubernetes.io/name=mcpremoteproxy,app.kubernetes.io/instance=$proxy_name" \
          --tail=-1 >"$WORK/proxy.log" 2>/dev/null || true
        f0_tool audit "$WORK/proxy.log" --label "proxy ${labels[$i]} ($proxy_name)" >>"$F0_OUT/f0.4-audit.txt" ;;
    esac
  done
  f0_block F0.4 "$F0_OUT/f0.4-audit.txt"

  # refresh-tools 와 라이브 목록, 웹 가져오기 주소 규칙의 갈래. 테넌트 네임스페이스의 Service 는 셀렉터 없는 Service 와 그
  # EndpointSlice 가 시험 서버 파드를 가리킨다
  pod_ip="$("${K[@]}" -n "$F0_NS" get pods -l app=f0-mcp -o jsonpath='{.items[0].status.podIP}')"
  cluster_ip="$("${K[@]}" -n "$F0_NS" get svc f0-mcp -o jsonpath='{.spec.clusterIP}')"
  tenant_ns=""
  for n in "${gw_ns[@]}"; do [ -z "$n" ] || { tenant_ns="$n"; break; }; done
  [ -n "$tenant_ns" ] || die F0.4 "no Gateway was deployed, so the tenant namespace is unknown"
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata: {name: f0-alias, namespace: $tenant_ns}
spec: {ports: [{name: http, port: 8080, targetPort: 8080}]}
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata: {name: f0-alias-1, namespace: $tenant_ns, labels: {kubernetes.io/service-name: f0-alias}}
addressType: IPv4
ports: [{name: http, port: 8080, protocol: TCP}]
endpoints: [{addresses: ["$pod_ip"]}]
EOF
  for spec in "f0-addr-tenant-svc http://f0-alias.$tenant_ns.svc.cluster.local:8080/mcp" \
    "f0-addr-pod-ip http://$pod_ip:8080/mcp" "f0-addr-cluster-ip http://$cluster_ip:8080/mcp"; do
    read -r name url <<<"$spec"
    hub_item F0.4 "$name" entry "$url" streamable-http
    if [ -z "$ITEM_ID" ]; then
      f0_note F0.4 "$name ($url): POST /hub -> $ITEM_CODE"
      continue
    fi
    item_ids+=("$ITEM_ID"); labels+=("$name")
  done
  {
    echo "| item | refresh-tools | GET /hub/{id}/tools |"
    echo "|---|---|---|"
    for i in "${!labels[@]}"; do
      [ -n "${item_ids[$i]}" ] || continue
      code="$(api POST "/hub/${item_ids[$i]}/refresh-tools")"
      r="$code $(jq_ 'd.get("tool_names") or (d.get("detail") or {})' 2>/dev/null || true)"
      code="$(api GET "/hub/${item_ids[$i]}/tools")"
      l="$code $(jq_ '{"listed": d.get("listed"), "reason": d.get("reason"), "tools": sorted(t["name"] for t in d.get("tools") or [])}' 2>/dev/null || true)"
      echo "| ${labels[$i]} | $r | $l |"
    done
  } >"$F0_OUT/f0.4-refresh.md"
  f0_block F0.4 "$F0_OUT/f0.4-refresh.md"

  # 꺼진 서버: 시험 서버를 0 으로 내린 동안의 Gateway 상태 행과 vMCP 의 답, remote 항목의 상태
  f0_server_scale F0.4 0
  sleep 45
  for i in "${!labels[@]}"; do
    case "${labels[$i]}" in f0-http-* | f0-sse-*) ;; *) continue ;; esac
    [ -n "${gw_ids[$i]:-}" ] || continue
    api GET "/gateways/${gw_ids[$i]}" >/dev/null
    f0_note F0.4 "down ${labels[$i]}: Gateway phase $(jq_ '(d.get("status") or {}).get("phase")'), backends $(jq_ 'json.dumps((d.get("status") or {}).get("backends"))')"
    if [ "${labels[$i]%-remote}" != "${labels[$i]}" ]; then
      api GET "/hub/${item_ids[$i]}" >/dev/null
      f0_note F0.4 "down ${labels[$i]}: hub item status $(jq_ 'json.dumps(d.get("status"))')"
    fi
    [ -n "${urls[$i]}" ] || continue
    F0_TOKEN="$PERSON" f0_tool probe --url "${urls[$i]}" --rows session --out "$F0_OUT/f0.4-down-${labels[$i]}.json" >/dev/null \
      || true
    f0_note F0.4 "down ${labels[$i]}: $(json_of "$F0_OUT/f0.4-down-${labels[$i]}.json" 'json.dumps({k: d["rows"].get(k) for k in ("initialize", "tools/list", "tools/call add")})' 2>/dev/null || echo 'probe failed')"
  done
  f0_server_scale F0.4 1
  t0=$SECONDS
  for i in "${!labels[@]}"; do
    case "${labels[$i]}" in f0-http-* | f0-sse-*) ;; *) continue ;; esac
    [ -n "${urls[$i]}" ] || continue
    back="not within 180s"
    while [ $((SECONDS - t0)) -lt 180 ]; do
      if [ "$(mcp --token "$PERSON" call "${urls[$i]}" add '{"a": 3, "b": 4}')" = 0 ]; then
        back="$((SECONDS - t0))s after the scale-up"; break
      fi
      sleep 5
    done
    f0_note F0.4 "back ${labels[$i]}: add(3,4) answered $back"
  done
}

# ---------- F1 ----------

F1_A="${F1_TENANT:-f1-entry}"; F1_B="${F1_OTHER_TENANT:-f1-entry-x}"
F1_GWS=(ga gb gc)

f1_host() { echo "f1b-$1.e2e.local"; }

# f1_item_note PHASE ITEM -> 허브 항목 r(remote) 이나 p(proxy 게시)의 kind·k8s·source 를 적는다
f1_item_note() {
  TENANT="$F1_A" api GET "/hub/$(f1_get "item-$2")" >/dev/null
  f0_note F1 "$1 item $2: kind $(jq_ 'd["kind"]') k8s $(jq_ 'json.dumps(d.get("k8s"))') source $(jq_ 'json.dumps(d.get("source"))')"
}

f1_prepare() {
  local code r p g tenant spike_ip
  f1_state
  f0_header_connect F1
  traefik_ip F1
  TENANT="$F1_A"
  # 외부 서버 자리에 spike Service 의 IP 를 둔다. 다른 네임스페이스의 Service 이름은 플랫폼이 부르지 않아 변환이 보류된다
  spike_ip="$("${K[@]}" -n "$SPIKE_NS" get svc mcp-calculator-proxy -o jsonpath='{.spec.clusterIP}')"
  [ -n "$spike_ip" ] || die F1 "the spike Service has no ClusterIP"
  hub_item F1 f1b-calc remote "http://$spike_ip:8080/mcp" streamable-http "$(share_with "$F1_B")"
  [ -n "$ITEM_ID" ] || die F1 "the remote item f1b-calc -> $ITEM_CODE"
  [ "$ITEM_PHASE" = Ready ] || die F1 "the remote item f1b-calc is $ITEM_PHASE, not Ready"
  r="$ITEM_ID"; f1_put item-r "$r"
  for g in ga gb; do
    tenant="$F1_A"; [ "$g" = ga ] || tenant="$F1_B"
    TENANT="$tenant" gateway F1 "f1b-$g" "$r" "$(exposure "$(f1_host "$g")")"
    [ "$GW_PHASE" = Ready ] || die F1 "Gateway f1b-$g phase $GW_PHASE"
    f1_put "gw-$g" "$GW_ID"; f1_put "tenant-$g" "$tenant"; f1_put "ns-$g" "$GW_NS"
  done
  code="$(api POST "/gateways/$(f1_get gw-ga)/publish" '{"name": "f1b-ga-proxy", "mode": "proxy"}')"
  case "$code" in 201) p="$(jq_ 'd["id"]')" ;; 409) listed_id F1 /hub f1b-ga-proxy; p="$LISTED_ID" ;; *) die F1 "proxy publish -> $code" ;; esac
  wait_ready "/hub/$p" F1
  f1_put item-p "$p"
  gateway F1 f1b-gc "$p" "$(exposure "$(f1_host gc)")"
  [ "$GW_PHASE" = Ready ] || die F1 "Gateway f1b-gc phase $GW_PHASE"
  f1_put gw-gc "$GW_ID"; f1_put tenant-gc "$F1_A"; f1_put ns-gc "$GW_NS"
  f1_check prepare
}

# f1_watch -> Gateway 마다 호출 루프를 뒤에서 돌린다. 플랫폼 API 없이 Traefik IP 만 쓴다(header 모드라 토큰이 없다). 끝에
# 곧 시작할 설치의 시각을 적는다
f1_watch() {
  local g
  f1_state
  traefik_ip F1-watch
  f1_next_round
  for g in "${F1_GWS[@]}"; do
    f0_loop_start "$F1_STATE" "$g-r$F1_ROUND" "http://$(f1_host "$g")/mcp" Add '{"a": 3, "b": 4}'
  done
  f0_note F1 "round $F1_ROUND: call loops started for ${F1_GWS[*]}"
  f1_mark_install
}

# f1_proxies -> 두 테넌트 네임스페이스의 hub-·shared- MCPRemoteProxy 이름(한 줄에 하나)
f1_proxies() {
  local ns seen=""
  for ns in "$(f1_get ns-ga)" "$(f1_get ns-gb)"; do
    case " $seen " in *" $ns "*) continue ;; esac
    seen="$seen $ns"
    "${K[@]}" -n "$ns" get mcpremoteproxy -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null |
      grep -E '/(hub|shared)-' || true
  done
}

f1_check() {
  local phase="$1" g url tools item started=$SECONDS left call limit="${F1_CONVERT_TIMEOUT:-1200}"
  f1_state
  if [ "$phase" != prepare ]; then
    # 변환을 기다리고 루프를 멈추는 데까지는 플랫폼 API 를 쓰지 않는다
    if [ "$phase" = new ]; then
      left="$(f1_proxies)"
      while [ -n "$left" ] && [ $((SECONDS - started)) -lt "$limit" ]; do sleep 10; left="$(f1_proxies)"; done
      if [ -n "$left" ]; then
        f0_note F1 "new: conversion DID NOT FINISH within ${limit}s, hub-/shared- MCPRemoteProxy left: $(tr '\n' ' ' <<<"$left")"
        f1_fail "$phase" "the conversion did not finish within ${limit}s"
      else
        f0_note F1 "new: no hub-/shared- MCPRemoteProxy left after $((SECONDS - started))s"
      fi
    fi
    f1_stop_loops "$phase" "${F1_GWS[@]}"
    f0_header_connect "F1-$phase"
    traefik_ip "F1-$phase"
  fi
  for g in "${F1_GWS[@]}"; do
    url="http://$(f1_host "$g")/mcp"
    if [ "$(mcp_call "$url" "$(pin "$url" "$LB_IP")" "" tools/list)" = 200 ]; then
      tools="$(rpc_ 'sorted(t["name"] for t in d["result"]["tools"])')"
    else
      tools="FAILED"; f1_fail "$phase" "f1b-$g: tools/list at $url: $(cat "$WORK/rpc.raw" 2>/dev/null)"
    fi
    [ "$phase" != prepare ] || f1_put "tools-$g" "$tools"
    [ "$tools" = "$(f1_get "tools-$g")" ] || f1_fail "$phase" "f1b-$g: tools $tools, at prepare $(f1_get "tools-$g")"
    TOOL=Add; TOOL_ARGS='{"a": 3, "b": 4}'; TOOL_EXPECT=7
    if allowed "$url" "$(pin "$url" "$LB_IP")" ""; then
      call="Add=7"
    else
      call="FAILED ($MCP_NOTE)"; f1_fail "$phase" "f1b-$g: call: $MCP_NOTE"
    fi
    f0_note F1 "$phase f1b-$g: tools $tools (prepare: $(f1_get "tools-$g")); $call"
  done
  for item in r p; do f1_item_note "$phase" "$item"; done
  if [ "$phase" != prepare ]; then
    : >"$F0_OUT/f1b-$phase-audit.txt"
    for g in "${F1_GWS[@]}"; do
      TENANT="$(f1_get "tenant-$g")" api GET "/gateways/$(f1_get "gw-$g")" >/dev/null
      "${K[@]}" -n "$(f1_get "ns-$g")" logs -l "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$(jq_ 'd["k8s"]["name"]')" \
        --since=10m >"$WORK/vmcp.log" 2>/dev/null || true
      f0_tool audit "$WORK/vmcp.log" --label "vmcp f1b-$g" | tail -n 5 >>"$F0_OUT/f1b-$phase-audit.txt"
    done
    f0_block "F1 $phase" "$F0_OUT/f1b-$phase-audit.txt"
  fi
}

# ---------- main ----------

mode="${1:-f0}"
shift || true
case "$mode" in
  f0)
    f0_header_connect F0
    traefik_ip F0
    f0_server_up F0
    f0_4
    # 견줌이 무효인 서버가 있으면(record_fail) PASS 를 찍지 않는다. 다른 서버의 결과는 남는다
    [ "$FAILED" -gt 0 ] || pass F0.4 "results in $F0_OUT"
    ;;
  f1)
    phase="${1:-}"
    case "$phase" in
      prepare) f1_prepare ;;
      watch) f1_watch ;;
      check-new) f1_check new ;;
      check-old) f1_check old ;;
      *) die F1 "usage: p54b.sh f1 prepare|watch|check-new|check-old" ;;
    esac
    pass "F1 $phase" "results in $F0_OUT"
    ;;
  *) die F0 "usage: p54b.sh f0 | f1 prepare|watch|check-new|check-old" ;;
esac
finish
