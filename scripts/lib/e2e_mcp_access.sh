# shellcheck shell=bash
# shellcheck disable=SC2034  # 남기는 전역과 한도는 이 파일을 읽는 스크립트가 읽는다
# 노출한 MCP 주소를 준비하고 토큰으로 불러 허용·거부·401 을 가르는 한 벌 (e2e_pat, e2e_oauth). `. "$HERE/lib/e2e_mcp_access.sh"`
# 로 읽는다. 부르는 쪽이 K·WORK·ROOT·CTX·NS·TENANT·PLATFORM_NS·READY_TIMEOUT·INGRESS_CLASS·TRAEFIK_NS·TRAEFIK_SVC 와 대상 도구
# TOOL(이름 끝)·TOOL_ARGS(비면 TOOL_FILL 로 채운다)·TOOL_EXPECT(결과에 있어야 할 글자)를 두고, e2e_json.sh·e2e_report.sh(die)·
# e2e_api.sh(api·jq_)·e2e_pf.sh·mcp_rpc.sh(mcp_status·mcp_ingress·rpc_·svc_token·ROUTE_WAIT)를 먼저 읽는다 (함수는 부를
# 때 읽는다).
#
# 노출 host 는 curl --resolve 로 Traefik IP(orbstack)나 DNS 로 푼 IP(설치본)에 못 박으므로 /etc/hosts 가 필요 없다. 허용은
# tools/list 에 대상 도구가 있고 그 tools/call 이 200 에 JSON-RPC 오류 없이 끝나는 것이다. 거부는 tools/list 가 200 에 빈
# 목록이고 tools/call 이 HTTP 403 인 것이다. Gateway 와 노출 MCP Server 의 앞단은 모두 vMCP 라 거부 문구가
# "call denied by authorization policy" 이지만, 옛 앞단(MCPRemoteProxy)이 남은 설치본은 "Unauthorized" 로 답하므로 상태
# 코드로만 가른다.
#
# traefik_ip STEP
#   orbstack Traefik(LoadBalancer Service TRAEFIK_NS/TRAEFIK_SVC)의 IP 를 LB_IP 에 남긴다. 없으면 die STEP
# url_host URL
#   URL 의 호스트를 찍는다
# pin URL IP
#   curl --resolve 값 host:port:IP 를 찍는다. 포트는 URL 에 없으면 스킴의 기본값이다
# exposure HOST
#   노출을 켠 배포 본문을 찍는다(TLS none, INGRESS_CLASS)
# public_gateway STEP URL
#   TENANT 에서 external_url 이 URL 인 Gateway 를 찾아 GW_ID 에, DNS 로 푼 --resolve 값을 GW_PIN 에 남긴다. 설치본에서는 새
#   노출 host 가 밖에서 닿는지 모르므로 운영자가 노출해 둔 Gateway 를 읽기만 한다. 그런 Gateway 가 없거나 그 host 가 풀리지
#   않으면 까닭을 GW_NOTE 에 남기고 1 이다. GET /gateways 가 실패하면 die STEP
# install_gateway_host HOST_VAR
#   설치본에서 게이트가 읽을 노출 Gateway 의 host 다. HOST_VAR(E2E_PAT_GW_HOST·E2E_OAUTH_GW_HOST)가 있으면 그 값이고 없으면
#   mcp-gateway.<EXPOSURE_DOMAIN> 이다. 둘 다 없으면 빈 줄이다
# install_gateway STEP HOST_VAR HOST
#   설치본이 노출해 둔 Gateway 를 https://HOST/mcp 에서 찾아 GW_URL 에 그 주소를 남긴다. HOST 가 비면(EXPOSURE_DOMAIN 도
#   HOST_VAR 도 없다) 까닭을 GW_NOTE 에 남기고 1, HOST 를 주었는데 그 Gateway 가 없거나 host 가 풀리지 않으면 GW_NOTE 에
#   HOST_VAR 를 적어 2 다. 찾으면 public_gateway 처럼 GW_ID·GW_PIN 을 남기고 0 이다
# skip_without_gateway RC STEP...
#   install_gateway 가 0 이 아닐 때 STEP 들을 건너뛴다. RC 1 은 조건부(cond_skip, 노출한 Gateway 를 정하지 않은 설치본)이고
#   RC 2 는 돌아야 했는데 못 돈 것(skip, 실행이 INCOMPLETE 로 끝난다)이다
# gateway_vmcp STEP GW_ID
#   그 Gateway 의 vMCP 가 도는 네임스페이스를 NS 에, vMCP 파드의 라벨을 VMCP_SEL 에 남긴다(restart_pods 가 쓴다). 문서를 읽지
#   못하거나 배포되지 않았으면 die STEP
# front_sel MS_K8S_NAME
#   노출한 MCP Server 의 앞단 파드 라벨을 찍는다(restart_pods 가 쓴다). 앞단은 그 서버 하나를 백엔드로 둔 vMCP
#   front-<k8s.name> 이다
# mcp_call URL PIN TOKEN METHOD [PARAMS]
#   마지막 요청의 HTTP 코드를 찍고 결과는 $WORK/rpc.json 에 둔다. 파드가 막 다시 떴거나 Traefik 이 라우트를 아직 모르면
#   (000·404·502·503) 5초씩 24번 다시 부른다
# tool_args NAME
#   tools/call 의 arguments 를 찍는다. TOOL_ARGS 가 있으면 그것이고, 없으면 방금 받은 tools/list 의 스키마를 TOOL_FILL 로
#   채운다(deploy/dev/lab/tool_fill.py). 채우지 못하면 1
# allowed URL PIN TOKEN / denied URL PIN TOKEN / unauthenticated URL PIN TOKEN
#   허용·거부·401 이면 0. allowed 는 찾은 도구 이름과 인자를 MCP_TOOL·MCP_ARGS 에, 아니면 까닭을 MCP_NOTE 에 남기고 denied
#   는 앞의 allowed 가 남긴 MCP_TOOL 을 부른다. unauthenticated 는 initialize 가 401 인 것이다. 허용돼야 할 토큰의
#   tools/list 에 대상 도구가 없으면 allowed 가 lib/mcp_rpc.sh 의 ROUTE_WAIT 초까지 다시 본다
# wait_unauthenticated STEP SINCE TOKEN
#   URLS·PINS 배열의 모든 주소가 TOKEN 을 401 로 거절할 때까지 REVOKE_POLL 초마다 본다. SINCE(epoch 초)부터 REVOKE_LIMIT 을
#   넘기면 die STEP 이다. 걸린 초를 REFUSED_AFTER 에 남긴다
# restart_pods SELECTOR...
#   NS 에서 그 라벨의 파드를 지우고 새 파드가 모두 Ready 일 때까지 기다린다. 오퍼레이터가 새로 띄운 검증기는 첫 요청에서
#   JWKS 를 받는다. 실패하면 1
# internal_hop STEP MS_K8S_NAME PORT MS_ID
#   D41 내부 홉. MCP Server 의 내부 프록시를 PORT 로 port-forward 하고(루프 pid 는 PF_PROXY, 닫는 것은 부르는 쪽의 정리다)
#   무토큰 401, lib/mcp_rpc.sh 의 svc_token 이 MS_ID 의 audience 를 실어 서명한 서비스 토큰 200 을 본다(P50 9절). 아니면
#   die STEP

# 회수한 토큰이 Gateway·노출 앞단에서 거절되기까지의 한도 16분과 그것을 다시 불러 보는 간격 (P32 3절). 설정이 아니라
# 한도 계산의 재료라 여기에 이름 붙여 둔다
REVOKE_LIMIT=960
REVOKE_POLL=30
# tools/call 인자를 이름으로 채우는 규칙 PATTERN=VALUE. TOOL_ARGS 가 없을 때(설치본 Gateway 의 공휴일 도구) 쓴다.
# e2e_oauth 는 같은 규칙을 스크립트 클라이언트의 --fill 로 넘긴다
TOOL_FILL=(year=2026 country=KR)

LB_IP=""; GW_ID=""; GW_PIN=""; GW_NOTE=""; REFUSED_AFTER=""
MCP_TOOL=""; MCP_ARGS=""; MCP_NOTE=""

traefik_ip() { # STEP
  LB_IP="$("${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  [ -n "$LB_IP" ] || die "$1" "Service $TRAEFIK_NS/$TRAEFIK_SVC has no LoadBalancer IP (is Traefik installed?)"
}

install_gateway_host() { # HOST_VAR
  printf '%s\n' "${!1:-${EXPOSURE_DOMAIN:+mcp-gateway.$EXPOSURE_DOMAIN}}"
}

install_gateway() { # STEP HOST_VAR HOST
  GW_URL="https://$3/mcp"
  if [ -z "$3" ]; then
    GW_NOTE="EXPOSURE_DOMAIN is empty and $2 is unset, so the install names no exposed Gateway"
    return 1
  fi
  public_gateway "$1" "$GW_URL" && return 0
  GW_NOTE="$GW_NOTE (set $2 to the host of the install's exposed Gateway, or expose one at mcp-gateway.<EXPOSURE_DOMAIN>)"
  return 2
}

skip_without_gateway() { # RC STEP...
  local rc="$1" step
  shift
  for step in "$@"; do
    if [ "$rc" = 1 ]; then cond_skip "$step" "$GW_NOTE"; else skip "$step" "$GW_NOTE"; fi
  done
}

gateway_vmcp() { # STEP GW_ID
  local k8s
  [ "$(api GET "/gateways/$2")" = 200 ] || die "$1" "GET /gateways/$2 failed"
  k8s="$(jq_ '(d.get("k8s") or {}).get("name") or ""')"; NS="$(jq_ '(d.get("k8s") or {}).get("namespace") or ""')"
  { [ -n "$k8s" ] && [ -n "$NS" ]; } || die "$1" "Gateway $2 has no deployed vMCP: $(jq_ 'd.get("k8s")')"
  VMCP_SEL="app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$k8s"
}

front_sel() { echo "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=front-$1"; } # MS_K8S_NAME

url_host() { python3 -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).hostname)' "$1"; } # URL

pin() { # URL IP
  python3 -c 'import sys, urllib.parse as u
p = u.urlsplit(sys.argv[1])
print("%s:%s:%s" % (p.hostname, p.port or (443 if p.scheme == "https" else 80), sys.argv[2]))' "$1" "$2"
}

exposure() { # HOST
  echo "{\"exposure\": {\"enabled\": true, \"host\": \"$1\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"none\"}}}"
}

public_gateway() { # STEP URL
  local step="$1" url="$2" host ip
  [ "$(api GET /gateways)" = 200 ] || die "$step" "GET /gateways in $TENANT failed"
  GW_ID="$(jq_ "next((g['id'] for g in d if (g.get('status') or {}).get('external_url') == '$url'), '')")"
  [ -n "$GW_ID" ] || { GW_NOTE="no Gateway in $TENANT has external_url $url"; return 1; }
  host="$(url_host "$url")"
  ip="$(python3 -c 'import socket, sys; print(socket.gethostbyname(sys.argv[1]))' "$host" 2>/dev/null)" \
    || { GW_NOTE="the Gateway's host $host does not resolve"; return 1; }
  GW_PIN="$(pin "$url" "$ip")"
}

mcp_call() { # URL PIN TOKEN METHOD [PARAMS]
  local code i
  for ((i = 0; i < 24; i++)); do
    code="$(mcp_ingress "$1" "$2" "$4" "${5:-}" "$3")"
    case "$code" in 000 | 404 | 502 | 503) sleep 5 ;; *) break ;; esac
  done
  echo "$code"
}

tool_args() { # NAME
  [ -z "$TOOL_ARGS" ] || { echo "$TOOL_ARGS"; return 0; }
  # 스크립트 클라이언트의 call --fill 과 같은 규칙 하나다
  python3 "$ROOT/deploy/dev/lab/tool_fill.py" "$WORK/rpc.json" "$1" "${TOOL_FILL[@]}"
}

allowed() { # URL PIN TOKEN
  local code i
  for ((i = 0; i <= ROUTE_WAIT / 5; i++)); do
    code="$(mcp_call "$1" "$2" "$3" tools/list)"
    [ "$code" = 200 ] || { MCP_NOTE="tools/list -> $code: $(cat "$WORK/rpc.raw")"; return 1; }
    MCP_TOOL="$(rpc_ "next((t['name'] for t in ((d or {}).get('result') or {}).get('tools', []) if t['name'].endswith('$TOOL')), '')")"
    [ -z "$MCP_TOOL" ] || break
    sleep 5
  done
  [ -n "$MCP_TOOL" ] || { MCP_NOTE="tools/list has no *$TOOL after ${ROUTE_WAIT}s: $(cat "$WORK/rpc.json")"; return 1; }
  MCP_ARGS="$(tool_args "$MCP_TOOL")" || { MCP_NOTE="no arguments for $MCP_TOOL"; return 1; }
  code="$(mcp_call "$1" "$2" "$3" tools/call "{\"name\": \"$MCP_TOOL\", \"arguments\": $MCP_ARGS}")"
  [ "$code" = 200 ] && [ "$(rpc_ "('ok' if d and not d.get('error') and not d['result'].get('isError')
    and '$TOOL_EXPECT' in json.dumps(d['result'].get('content')) else 'no')")" = ok ] \
    || { MCP_NOTE="tools/call $MCP_TOOL -> $code: $(cat "$WORK/rpc.json")"; return 1; }
}

denied() { # URL PIN TOKEN
  local code
  code="$(mcp_call "$1" "$2" "$3" tools/list)"
  [ "$code" = 200 ] && [ "$(rpc_ "((d or {}).get('result') or {}).get('tools')")" = "[]" ] \
    || { MCP_NOTE="tools/list -> $code: $(cat "$WORK/rpc.json")"; return 1; }
  code="$(mcp_call "$1" "$2" "$3" tools/call "{\"name\": \"$MCP_TOOL\", \"arguments\": $MCP_ARGS}")"
  [ "$code" = 403 ] || { MCP_NOTE="tools/call $MCP_TOOL -> $code: $(cat "$WORK/rpc.json")"; return 1; }
}

unauthenticated() { [ "$(mcp_call "$1" "$2" "$3" tools/list)" = 401 ]; } # URL PIN TOKEN

wait_unauthenticated() { # STEP SINCE TOKEN
  local step="$1" since="$2" token="$3" left=${#URLS[@]} elapsed i
  while [ "$left" -gt 0 ]; do
    elapsed=$(($(date +%s) - since))
    [ "$elapsed" -le "$REVOKE_LIMIT" ] || die "$step" "revoked token still accepted after ${elapsed}s (limit ${REVOKE_LIMIT}s)"
    sleep "$REVOKE_POLL"
    left=0
    for i in "${!URLS[@]}"; do unauthenticated "${URLS[$i]}" "${PINS[$i]}" "$token" || left=$((left + 1)); done
  done
  REFUSED_AFTER=$(($(date +%s) - since))
}

restart_pods() { # SELECTOR...
  local sel i
  for sel in "$@"; do "${K[@]}" -n "$NS" delete pod -l "$sel" --wait=true >/dev/null || return 1; done
  for sel in "$@"; do
    for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
      "${K[@]}" -n "$NS" get pod -l "$sel" -o json >"$WORK/pods.json" 2>/dev/null \
        && [ "$(json_of "$WORK/pods.json" "'ok' if d['items'] and all(not p['metadata'].get('deletionTimestamp') and any(
          c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', [])) for p in d['items']) else 'no'")" = ok ] \
        && continue 2
      sleep 5
    done
    return 1
  done
}

internal_hop() { # STEP MS_K8S_NAME PORT MS_ID
  local step="$1" svc="svc/mcp-$2-proxy" port="$3" url="http://127.0.0.1:$3/mcp" server="$4" code token
  start_pf "$NS" "$svc" "${port}:8080"; PF_PROXY=$PF_PID
  wait_port "$port" || die "$step" "port-forward to $svc did not open :$port"
  code="$(mcp_status "$url")"
  [ "$code" = 401 ] || die "$step" "internal proxy without a token -> $code (want 401)"
  token="$(svc_token "$server")" || die "$step" "svc_token.py failed"
  code="$(mcp_status "$url" "$token")"
  [ "$code" = 200 ] || die "$step" "internal proxy with a service token -> $code (want 200)"
}
