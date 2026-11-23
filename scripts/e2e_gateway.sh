#!/usr/bin/env bash
# P8 F2·F7 e2e against the real (orbstack) cluster. Assumes `make deploy-local` (platform in-cluster,
# header mode, home cluster ingress{class traefik, allow_unauthenticated true}) and the P1 spike
# (mock-soap, the calculator MCP server in daegam-spike) are up. Traefik must be installed (IngressClass
# `traefik`, LoadBalancer Service traefik/traefik); the script reads its IP and pins hosts with
# `curl --resolve`, so the gate never touches /etc/hosts (STEP 9i's registry helper does, scripts/hub_e2e_registry.sh).
#
# Gate (STEP 1~18 + 4b·8c·8r·9e·9h·9i·16b, 10 은 비어 있다): MCP Servers calculator[Add, Divide] + orders 저장·배포, published to the hub
#   by hand (D23), hub entry (spike server) → Gateways A (calculator[Add] + orders all), B (calculator[Divide]) 저장·배포 →
#   A tools/list == [Add, CreateOrder, GetOrder], B == [Divide], no entry tools, no prefixes,
#   A Add(3,4)=7, B Add not found, A Subtract not found, CR names gw-<name> + daegam.devops.ai.kr/id labels,
#   redeploy keeps the name, publish → C consumes, 다른 테넌트의 Gateway 가 entry 항목을 자기 Entry 로 부른다,
#   exposure: A 의 복제본을 http 로, calculator 의 복제본을 https 로 (vMCP 앞단 뒤), 422 namespace_not_allowed,
#   메트릭은 거친 곳마다 한 번 세고 노출 앞단은 세지 않는다, delete cleans up, code mode Gateway, cleanup.
#   단계 번호는 다른 문서가 가리키므로 당기지 않는다. 순환 참조의 422 cycle 과 노출 host 중복의 409 host_conflict 는 API
#   테스트가 지킨다(platform/tests/test_gateways.py 의 test_cycle_rejected, platform/tests/test_exposure.py).
#
#   STEP 0   platform port-forward; 중간에 멈춘 실행이 남긴 Gateway(a·b·c·code·a-exposed·probe-gw)와 그 게시 항목, MCP
#            Server calc-read 정리
#   STEP 1   backend apis calculator + orders
#   STEP 2   mcp servers calculator + orders 저장 → 배포 → Ready
#   STEP 3   배포해도 허브에 자동 등재되지 않는다; POST /mcp-servers/{id}/publish x2 (201, hub_item on the
#            document, audit publish_server) + entry (spike server, CR 없음). 그 Service 가 테넌트의 네임스페이스가 아니라
#            refresh-tools 는 422 entry_address_refused(service_namespace)이고 Tools 탭은 listed false 다
#   STEP 4   gateways A, B 정의 저장 (draft); unpublishing a referenced item -> 409 in_use [A, B]
#   STEP 4b  GET /{리소스}/{id}/references (D43): 허브 항목은 A·B 를, calculator 는 그 항목과 A·B 를 답한다
#   STEP 5   gateways A, B 배포 → Ready
#   STEP 6   tools/list subsets
#   STEP 7   tools/call
#   STEP 8   CR names gw-<name>, daegam.devops.ai.kr/id labels on group·entries·vMCP, redeploy keeps the name
#   STEP 8r  header 모드의 Tool 규칙(P52 2절): B 의 Divide 에 "developer 이상만" 규칙을 둔 리비전을 재배포하면 B 의 상태가
#            rules_not_enforced 를 내고, header 모드는 부를 수 있는 대상을 렌더하지 않으므로 B 는 여전히 Divide 를 보인다.
#            규칙을 걷은 리비전을 재배포하면 경고가 사라진다
#   STEP 9   publish A → C consumes; C tools == A advertised; C Add == 7; A unaffected
#   STEP 9e  entry 항목의 주소를 Gateway 가 직접 부른다: 다른 테넌트의 Gateway eb 가 STEP 3 의 spike 항목을 자기
#            MCPServerEntry 로 부르고(Add == 7), 스냅샷 행에 복제본이 없고 두 네임스페이스에 hub-·shared- MCPRemoteProxy 가
#            없으며 remote 복제본 기록이 늘지 않는다. 플랫폼이 직접 부르는 refresh-tools 는 다른 테넌트 네임스페이스의
#            Service(service_namespace)와 DAEGAM_ENTRY_PRIVATE_ALLOW 밖의 사설 주소(url_private)를 422
#            entry_address_refused 로 거절한다
#   STEP 9h  외부 서버의 고정 헤더(P86): 테넌트 네임스페이스의 header-echo 에 header 항목을 등록, 틀린 값은 header_refused,
#            맞는 값이 Gateway h 를 거쳐 서버에 전달되고, 바꾼 값은 재적용 뒤에 전달되며, 다른 테넌트 Gateway 의 사본은
#            consume 에서 빼면 없어지고, 항목을 뺀 h 에는 사본이 없다
#   STEP 11  STEP 9 가 게시한 항목: refresh-tools 422 derived_tools(D49), live tools/list(GET /hub/{id}/tools) == A advertised
#   STEP 12  exposure prerequisites: Traefik IP, self-signed kubernetes.io/tls Secret,
#            exposure-options lists the Secret name and carries no Secret data
#   STEP 13  A 를 복제한 a-exposed 를 http://gw-a.e2e.local/mcp 로 노출 배포 → warnings[] unauthenticated_exposure,
#            Ingress address assigned, tools/list through Traefik == A subset, Ingress 는 /mcp 와
#            /.well-known/oauth-protected-resource 만 열어 vMCP 의 /status·/api/backends/health·/health 는 404
#   STEP 14  calculator 를 복제한 calc-exposed 를 TLS Secret 으로 노출 배포 → 앞단은 그 서버 하나를 백엔드로 둔 vMCP
#            front-<k8s.name> 이고(P54 5절) Ready, 옛 앞단 MCPRemoteProxy 와 넘겨받기 경고는 없다. 그 MCPGroup·MCPServerEntry·
#            MCPExternalAuthConfig·MCPAuthzConfig 가 리소스 값 mcp-instance-front 를 달고, Ingress 는 vmcp-front-<k8s.name>:4483,
#            노출 정책은 두 종류의 앞단 파드를 고른다. external_url https://calc.e2e.local/mcp, tools/list + Add(3,4)=7
#            through Traefik (-k)
#   STEP 15  422 namespace_not_allowed (MCP Server, Gateway). 네임스페이스는 배포 본문이 받으므로 draft 문서의
#            deploy 다. home 의 allow_unauthenticated 는 플랫폼 설정이 정하므로 PATCH 로 바꾸면 409 env_managed 다(D54).
#            무인증 노출 금지의 422 는 API 테스트가 지킨다
#   STEP 16  cert-manager path when a ClusterIssuer exists, otherwise SKIP + 422 cert_manager_unavailable
#   STEP 16b 24시간 메트릭(P51): STEP 7·9·14 가 부르기 전에 읽은 값보다 calculator 호출 +2, A Add +2 와 백엔드 없는
#            application_error +1, B 백엔드 없는 application_error +1, C Add +1 이상이다. 앞단을 거친 calc-exposed 의
#            Add 는 감사 기록이 vMCP 앞단(backend front-<k8s.name>)과 MCP Server 에 하나씩 있고 둘 다 그 서버의 것으로
#            매겨져도 정확히 +1 이다
#   STEP 17  delete the exposed documents → Ingress, NetworkPolicy, the vMCP front and its group·Entry·auth configs gone,
#            Traefik answers 404
#   STEP 8c  code mode(P56). STEP 17 뒤, 18 앞에 돈다: Tool 이 모두 읽기로 표시된 MCP Server calc-read 를 게시해 code mode
#            를 켠 Gateway code 를 배포하면 tools/list 에 execute_tool_script 가 있고, 스크립트 하나가 Add·Divide 의 결과를
#            함께 돌려준다. orders 의 CreateOrder(쓰기)를 더한 저장은 422 code_mode_not_read_only, 노출을 켜는 PATCH 는 422
#            code_mode_exposed 다. 서버의 새 리비전이 Divide 를 쓰기로 표시하면 재적용이 code mode 를 빼고 code_mode_closed 를
#            알리며, Divide 를 바로 부를 수 있다. Gateway 와 서버는 그 단계가 지운다
#   STEP 18  cleanup: 게시된 MCP Server 를 Gateway 가 붙들고 있으면 삭제가 409 in_use 이고, Gateway 를 먼저
#            지운 뒤 지우면 게시 항목이 함께 사라진다; CRs gone
#   STEP 9i  허브 이미지의 허용(P55). STEP 18 뒤에 돈다: TLS zot 에 미러하고 시험 키로 서명한 stdio 이미지가 Env·Secret env 와
#            함께 항목이 되어 Gateway g9i 가 그 Tool 을 내놓고 값을 돌려주며, 바꾼 Secret 값이 컨테이너에 전달된다. 서명 없는 사본,
#            다른 테넌트에 열기, 거부한 digest 의 새 항목과 재배포는 거절된다. E2E_HUB_DIR(scripts/hub_e2e_registry.sh)이 없으면
#            세는 SKIP 이고, 후보의 k3d 잡이 띄우려다 못 했으면(E2E_HUB_EXPECTED) 이 STEP 만 FAIL 이다
# D24 (P10): calculator·a 는 한글 display_name 으로 저장해 응답에 그대로 돌아오는지(STEP 2·4), CR 이름이
#   mcp-calculator·gw-a 그대로인지(STEP 2·8, kubectl), STEP 9 의 게시 본문을 비워 기본 이름이 문서 name 그대로이고
#   display_name 이 문서의 것을 물려받는지 본다.
# macOS has no `timeout`; every wait is a polling loop.
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
TENANT="${E2E_TENANT:-gateway}"
API_PORT="${E2E_API_PORT:-$PORT_GATEWAY_API}"
PF_PORT_BASE="${E2E_VMCP_PORT:-$PORT_GATEWAY_VMCP_BASE}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
# STEP 16b 가 메트릭이 늘기를 기다리는 한도(초)다. 수집기가 60초마다 로그를 읽고 롤업이 120초마다 다시 세므로 둘을
# 합한 3분에 여유를 둔다
METRICS_TIMEOUT="${METRICS_TIMEOUT:-300}"
# STEP 8c 가 code mode 가 닫히기를 기다리는 한도(초)다. 재적용은 60초마다 돌고, 닫는 데 두 번 렌더한다(codeMode 를 뺀 렌더,
# 그 vMCP 가 적용된 뒤 힌트 forbid 를 뺀 렌더). 서버가 다시 뜨는 시간과 여유를 더한다
CODE_CLOSE_TIMEOUT="${CODE_CLOSE_TIMEOUT:-420}"
BASE="http://127.0.0.1:${API_PORT}"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
FIX="$ROOT/platform/tests/fixtures"
SOAP_ENDPOINT="http://mock-soap.${SPIKE_NS}.svc:18080/calc"
ORDERS_ENDPOINT="http://mock-soap.${SPIKE_NS}.svc:18080/orders"
# entry 허브 항목으로 등록할 외부 MCP 서버다. lab 의 스파이크 calculator(Tool 은 Add 하나)이고, 테넌트 네임스페이스 밖의
# Service 라 STEP 3·9e 가 플랫폼의 주소 규칙도 이 주소로 본다
SPIKE_SVC="mcp-calculator-proxy"
SPIKE_URL="http://${SPIKE_SVC}.${SPIKE_NS}.svc.cluster.local:8080/mcp"
SPIKE_ITEM="spike-entry"
# STEP 9e·9h 가 Gateway 를 두는 다른 테넌트
TENANT_B="${TENANT}-b"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-gateway.XXXXXX")"
K=(kubectl --context "$CTX")
PY="$ROOT/platform/.venv/bin/python"
MCP="$PY $ROOT/deploy/dev/lab/mcp_client.py"
PF_API=""; PFS=(); PFS_PORTS=()
AUTH=()   # header 모드라 Bearer 가 없다 (lib/e2e_api.sh 가 읽는다)
# 노출 STEP (12~17). Traefik 의 LoadBalancer Service 와 e2e 전용 host. 어디에도 DNS 등록하지 않는다
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"
TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
GW_HOST="${E2E_GW_HOST:-gw-a.e2e.local}"
MCP_HOST="${E2E_MCP_HOST:-calc.e2e.local}"
TLS_SECRET="${E2E_TLS_SECRET:-e2e-gw-tls}"
# STEP 8c 의 code mode Gateway 와 그 백엔드 MCP Server, 노출을 켜 보는 host. STEP 0 이 이전 실행이 남긴 둘을 지운다
CODE_GW_NAME="code"; CODE_MS_NAME="calc-read"; CODE_HOST="code.e2e.local"
# STEP 15 가 Cluster ingress 를 PATCH 해 보기 전에 원본 JSON 을 두고 cleanup 이 되돌린다. 409 가 정상이지만
# 만에 하나 바뀌었으면 되돌린다
CLUSTER_ID=""; CLUSTER_INGRESS_ORIG=""
# STEP 9i 가 바꾸기 전의 network·hub_images 설정(PATCH 본문). cleanup 이 되돌린다
HUB_SETTINGS_ORIG=""

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
# 이 게이트는 0.5초 간격 60회로 포트를 본다 (합계 30초). lib/e2e_pf.sh 의 wait_port 가 읽는다
# shellcheck disable=SC2034
PF_WAIT_TRIES=60
# shellcheck disable=SC2034
PF_WAIT_SLEEP=0.5
restore_cluster_ingress() {
  # STEP 15 가 PATCH 해 본 ingress 를 원래 값으로 둔다. API port-forward 가 살아 있을 때 불러야 한다
  [ -n "$CLUSTER_INGRESS_ORIG" ] || return 0
  local code
  code="$(api PATCH "/clusters/$CLUSTER_ID" "{\"ingress\": $CLUSTER_INGRESS_ORIG}" 2>/dev/null)"
  echo ">> cluster $CLUSTER_ID ingress restored -> $code"
  CLUSTER_INGRESS_ORIG=""
}
restore_hub_settings() {
  # STEP 9i 가 넣은 허브 이미지 설정과 CA 를 처음 값으로 둔다. API port-forward 가 살아 있을 때 불러야 한다
  [ -n "$HUB_SETTINGS_ORIG" ] || return 0
  local code
  code="$(api PATCH /settings "$HUB_SETTINGS_ORIG" 2>/dev/null)"
  echo ">> hub image settings restored -> $code"
  HUB_SETTINGS_ORIG=""
}
cleanup() {
  restore_cluster_ingress
  restore_hub_settings
  for i in "${!PFS[@]}"; do stop_pf "${PFS[$i]}" "${PFS_PORTS[$i]}"; done
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup
# pass·die·finish 는 여기에 있다. 이 게이트는 실패를 세지 않고 첫 실패에서 끝낸다(die). 종료 코드 계약도 거기 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·listed_id·wait_ready·save_doc·deploy_doc·clone_fresh 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# wait_ready 가 남긴 본문($WORK/body)의 status.url
status_url() { jq_ '(d.get("status") or {}).get("url")'; }
wait_exposed() { # PATH LABEL front(0|1) -> 노출 상태가 준비될 때까지. front=1 이면 앞단 phase 도 Ready
  local path="$1" label="$2" front="$3" st="" i
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    api GET "$path" >/dev/null
    st="$(jq_ 'json.dumps((d.get("status") or {}).get("exposure"))')"
    if python3 -c "import json,sys; e=json.loads(sys.argv[1]) or {}; front=sys.argv[2]=='1'
ok = bool(e.get('address_assigned')) and (not front or e.get('front_phase')=='Ready')
sys.exit(0 if ok else 1)" "$st" "$front"; then EXPOSURE="$st"; return 0; fi
    sleep 5
  done
  die "$label" "exposure not ready after ${READY_TIMEOUT}s: $st"
}
# api 의 상태 코드를 가른다. 200 이면 0 이다. 플랫폼이 다시 뜨는 중(000·502·503)이면 1 이라 폴링이 다시 본다
# (wait_ready 와 같다). 그 밖의 코드는 die STEP 이다
answered() { # CODE STEP WHAT FILE
  case "$1" in
    200) ;;
    000 | 502 | 503) return 1 ;;
    *) die "$2" "$3 -> $1" "$(cat "$4")" ;;
  esac
}
# PATH(/mcp-servers/ID·/gateways/ID) 의 24시간 메트릭을 FILE 에 둔다. STEP 7·9·14 가 부르기 전에 기준값을 읽고 16b 가
# 다시 읽는다. 그 사이 STEP 들이 $WORK/body 를 쓰므로 파일을 따로 둔다
metrics_to() { # STEP PATH FILE -> answered 의 0·1
  answered "$(API_OUT="$3" api GET "$2/metrics?window=24h")" "$1" "GET $2/metrics" "$3"
}
# 기준값은 한 번만 읽으므로 플랫폼이 답하지 않아도 die STEP 이다
metrics_base() { metrics_to "$@" || die "$1" "GET $2/metrics: the platform did not answer"; } # STEP PATH FILE
# INIT_PARAMS·rpc_curl·rpc_extract·rpc_·mcp_ingress 는 여기에 있다 (WORK 를 쓴다)
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-gateway"
. "$HERE/lib/mcp_rpc.sh"
# STEP 8c 의 code_read_body·script_args·SCRIPT_TOOL 은 여기에 있다
. "$HERE/lib/e2e_code_mode.sh"
# platform_config (플랫폼 ConfigMap 의 값 읽기) 는 여기에 있다. STEP 9e 가 쓴다
. "$HERE/lib/e2e_control_plane.sh"
# entry 항목의 주소를 플랫폼이 부르지 않는지 본다 (STEP 3·9e). refresh-tools 는 422 entry_address_refused 이고 reason 이
# 걸린 규칙의 코드다. Tools 탭(GET /hub/{id}/tools)은 부르지 않고 listed false 와 그 이유로 답한다
entry_refused() { # STEP ITEM_ID REASON
  local code
  code="$(api POST "/hub/$2/refresh-tools")"
  [ "$code" = "422" ] && [ "$(jq_ "d['detail']['code'], d['detail'].get('reason')")" = "entry_address_refused $3" ] \
    || die "$1" "refresh-tools of hub item $2 -> $code (expected 422 entry_address_refused, reason $3): $(cat "$WORK/body")"
  code="$(api GET "/hub/$2/tools")"
  [ "$code" = "200" ] && [ "$(jq_ 'd["listed"], d["tools"]')" = "False []" ] \
    || die "$1" "GET /hub/$2/tools -> $code (expected listed false): $(cat "$WORK/body")"
  case "$(jq_ 'd["reason"]')" in
    "entry_address_refused: "*"($3)") ;;
    *) die "$1" "GET /hub/$2/tools reason: $(jq_ 'd["reason"]') (expected entry_address_refused ... ($3))" ;;
  esac
}
# 플랫폼 상태가 세는 remote 항목의 복제본 기록 수 (모든 클러스터의 합). 남은 것이 없는 클러스터는 상태에서 빠진다
# 값을 CLONE_RECORDS 에 남긴다 ($( ) 안에서 부르면 die 의 메시지가 사라진다)
CLONE_RECORDS=""
clone_records() { # STEP
  [ "$(api GET /settings/data-plane-auth)" = 200 ] || die "$1" "GET /settings/data-plane-auth failed"
  CLONE_RECORDS="$(jq_ 'sum(c["clones"] for c in d["entry_conversions"].values())')"
}
# A 가 광고하는 도구 셋 (sorted 의 repr). STEP 8·9·11 이 A·C·게시 항목의 목록을 이것과 견준다
A_TOOLS="['Add', 'CreateOrder', 'GetOrder']"
tools_sorted() { json_of - 'sorted(d["tools"])' <<<"$1"; }   # $MCP list 의 출력 -> 정렬한 도구 이름
rpc_tools() { rpc_ 'sorted(t["name"] for t in d["result"]["tools"])'; }

# ---------- 0. platform ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
# 죽은 실행(kill -9)은 정리를 못 하므로 그 port-forward 가 이 포트들에 남아 있을 수 있다
kill_stale_pf "$API_PORT" "$PF_PORT_BASE" "$((PF_PORT_BASE + 1))" "$((PF_PORT_BASE + 2))" "$((PF_PORT_BASE + 3))" "$((PF_PORT_BASE + 4))" \
  "$((PF_PORT_BASE + 5))"
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 0 "port-forward :$API_PORT did not open"
echo ">> context=$CTX tenant=$TENANT api=$API"
# 중간에 멈춘 실행은 Gateway(a·b·c·code, 복제본 a-exposed·probe-gw)와 A 를 게시한 허브 항목을 남긴다. 그것들이
# calculator 게시 항목을 붙들고 있으면 STEP 3 이 실패하므로 여기서 지운다. 각 Gateway 는 그것이 게시한 허브 항목을
# 지운 뒤에 지우고, C 는 A 의 게시 항목을 소비하므로 A 보다 먼저 지운다
for name in "$CODE_GW_NAME" c a-exposed probe-gw b a; do
  code="$(api GET /gateways)"; [ "$code" = 200 ] || die 0 "GET /gateways -> $code"
  id="$(find_id "$name")"; [ -n "$id" ] || continue
  code="$(api GET /hub)"; [ "$code" = 200 ] || die 0 "GET /hub -> $code"
  for item in $(jq_ "' '.join(i['id'] for i in d if (i.get('source') or {}).get('gateway_instance_id') == '$id')"); do
    api_delete "/hub/$item"
  done
  api_delete "/gateways/$id"
  wait_gone "/gateways/$id" || die 0 "gateway $name ($id) left by a previous run is still there"
  # 문서가 404 여도 CR 은 오퍼레이터와 GC 가 마저 지운다. 같은 CR 이름(gw-a 등)을 다시 만들기 전에 사라질 때까지 본다
  for ((i = 0; i < 24; i++)); do
    [ -z "$("${K[@]}" get virtualmcpservers,mcpgroups,mcpserverentries,ingresses -A -l "daegam.devops.ai.kr/id=$id" -o name 2>/dev/null)" ] && break
    sleep 5
  done
  [ "$i" -lt 24 ] || die 0 "CRs of gateway $name ($id) left by a previous run are still there"
  echo ">> removed gateway $name ($id) left by a previous run"
done
# STEP 8c 의 서버 calc-read 도 남는다. 게시된 채 남으면 STEP 3 의 허브 항목 목록이 어긋난다. 그 서버를 붙든 Gateway code 를
# 위에서 지웠으므로 지울 수 있고, 삭제가 게시 항목을 함께 지운다
code="$(api GET /mcp-servers)"; [ "$code" = 200 ] || die 0 "GET /mcp-servers -> $code"
id="$(find_id "$CODE_MS_NAME")"
if [ -n "$id" ]; then
  api_delete "/mcp-servers/$id"
  wait_gone "/mcp-servers/$id" || die 0 "mcp server $CODE_MS_NAME ($id) left by a previous run is still there"
  echo ">> removed mcp server $CODE_MS_NAME ($id) left by a previous run"
fi

# ---------- 1. backends: calculator + orders ----------
code="$(api POST /backend-apis "" -F "name=calc-gw" -F "endpoint=$SOAP_ENDPOINT" -F "file=@$FIX/calculator.wsdl")"
case "$code" in 200|201) ;; *) die 1 "backend calculator -> $code" ;; esac
BP1="$(jq_ 'd["id"]')"
code="$(api POST /backend-apis "" -F "name=orders-gw" -F "endpoint=$ORDERS_ENDPOINT" -F "file=@$FIX/orders.wsdl")"
case "$code" in 200|201) ;; *) die 1 "backend orders -> $code" ;; esac
BP2="$(jq_ 'd["id"]')"
pass 1 "backend apis calc=$BP1 orders=$BP2"

# ---------- 2. mcp servers: 저장 + 배포 ----------
# 만들기는 저장이다. 문서 하나가 정의와 배포를 함께 가지므로 프로필과 인스턴스를 따로 만들지 않는다 (D42)
#
# 재실행의 409 를 원본 문서와 복제본이 다르게 다룬다(lib/e2e_api.sh 머리말). 복제본(STEP 13·14·15·16)은 지우고
# 다시 복제하지만 원본 MCP Server(calculator·orders)는 PUT 으로 새 리비전을 쌓은 뒤 redeploy 한다. STEP 3 에서
# 허브에 게시되기 때문이다. 이전 실행의 Gateway 는 STEP 0 이 지우므로 a·b·c 는 늘 새로 만들어진다
mkserver() { # name bp tools-json [display_name] -> id. 저장하고 배포한다.
             # 새로 만들었으면 $WORK/created-<name> 을 남긴다 (409 재사용과 구분. 함수는 명령 치환 안에서 돈다)
  local display=""
  [ -n "${4:-}" ] && display=", \"display_name\": \"$4\""
  save_doc 2 /mcp-servers "$1" "{\"name\": \"$1\", \"backend_profile_id\": \"$2\", \"spec_version\": 1, \"tools\": $3$display}"
  [ "$SAVE_CREATED" = 0 ] || touch "$WORK/created-$1"
  deploy_doc 2 /mcp-servers "$SAVED_ID"
  echo "$SAVED_ID"
}
# 표시 이름(D24): 한글을 그대로 보내고 그대로 받는다. K8s 이름 calculator 는 CR 이름 mcp-calculator 가 된다
CALC_DISPLAY="계산기"
I1="$(mkserver calculator "$BP1" '["Add", "Divide"]' "$CALC_DISPLAY")"
I2="$(mkserver orders "$BP2" '[]')"
wait_ready "/mcp-servers/$I1" 2; wait_ready "/mcp-servers/$I2" 2
api GET "/mcp-servers/$I1" >/dev/null
I1_NS="$(jq_ 'd["k8s"]["namespace"]')"; I1_CR="$(jq_ 'd["k8s"]["name"]')"; I1_DISPLAY="$(jq_ 'd["display_name"]')"
[ "$I1_CR" = "mcp-calculator" ] || die 2 "calculator k8s.name=$I1_CR (expected mcp-calculator)"
"${K[@]}" -n "$I1_NS" get mcpserver "$I1_CR" -o name >/dev/null || die 2 "MCPServer $I1_NS/$I1_CR missing"
if [ -e "$WORK/created-calculator" ]; then
  [ "$I1_DISPLAY" = "$CALC_DISPLAY" ] || die 2 "calculator display_name=$I1_DISPLAY (expected $CALC_DISPLAY)"
else
  echo "  calculator reused from a previous run (display_name=$I1_DISPLAY not asserted)"
fi
api GET "/mcp-servers/$I2" >/dev/null
[ "$(jq_ 'd["display_name"]')" = "orders" ] || die 2 "orders display_name should default to name: $(jq_ 'd["display_name"]')"
pass 2 "mcp servers calculator=$I1 (display_name=$I1_DISPLAY, CR $I1_NS/$I1_CR) orders=$I2 (display_name defaults to name) 저장·배포 Ready"

# ---------- 3. hub: publish internal x2 (manual, D23) + entry (spike server) ----------
# 외부 MCP 서버는 entry 항목이다. URL 만 등록하고 CR 을 만들지 않으므로 기다릴 프록시가 없다. STEP 9e 의 다른 테넌트
# Gateway 가 쓰도록 그 테넌트에 연다. STEP 9e 가 견줄 remote 복제본 기록 수도 여기서 읽어 둔다
clone_records 3; CLONES0="$CLONE_RECORDS"
SPIKE_ACCESS="{\"view\": {\"tenants\": [\"$TENANT_B\"]}, \"consume\": {\"tenants\": [\"$TENANT_B\"]}}"
code="$(api POST /hub "{\"name\": \"$SPIKE_ITEM\", \"kind\": \"entry\", \"url\": \"$SPIKE_URL\", \"transport\": \"streamable-http\", \"access\": $SPIKE_ACCESS}")"
case "$code" in
  201) SPIKE="$(jq_ 'd["id"]')" ;;
  409)
    # 이전 실행이 남긴 항목이다. 접근 범위만 다시 맞춘다
    listed_id 3 /hub "$SPIKE_ITEM"; SPIKE="$LISTED_ID"
    code="$(api PUT "/hub/$SPIKE/access" "$SPIKE_ACCESS")"; [ "$code" = "200" ] || die 3 "PUT /hub/$SPIKE/access -> $code"
    ;;
  *) die 3 "POST /hub entry -> $code: $(cat "$WORK/body")" ;;
esac
api GET "/hub/$SPIKE" >/dev/null
[ "$(jq_ 'd["kind"], d["source"]["url"]')" = "entry $SPIKE_URL" ] || die 3 "spike item shape: $(cat "$WORK/body")"
# 배포해도 자동으로 허브에 오르지 않는다. hub_item 이 null 이면 허브에 internal 항목이 없고 게시 해제는
# 404 not_published 다. 재실행이면 이전 실행의 게시가 남아 있을 수 있어 publish 는 200(멱등)도 받는다
publish_inst() { # ID NAME -> hub item id
  local id="$1" name="$2" code
  api GET "/mcp-servers/$id" >/dev/null
  if [ "$(jq_ 'd["hub_item"]')" = "None" ]; then
    api GET /hub >/dev/null
    [ "$(jq_ "len([i for i in d if i['kind']=='internal' and i['source']['instance_id']=='$id'])")" = "0" ] \
      || die 3 "$name was auto-registered in the hub (must be manual)"
    code="$(api DELETE "/mcp-servers/$id/publish")"
    [ "$code" = "404" ] && [ "$(jq_ "d['detail']['code']")" = "not_published" ] || die 3 "unpublish before publish -> $code (expected 404 not_published)"
    code="$(api POST "/mcp-servers/$id/publish" '{}')"
    [ "$code" = "201" ] || die 3 "publish $name -> $code (expected 201)"
  else
    code="$(api POST "/mcp-servers/$id/publish" '{}')"
    [ "$code" = "200" ] || die 3 "re-publish $name -> $code (expected 200 idempotent)"
  fi
  [ "$(jq_ 'd["kind"], d["name"], d["source"]["instance_id"], d["scope"]')" = "internal $name $id tenant" ] || die 3 "published item shape: $(cat "$WORK/body")"
  [ "$(jq_ 'd["permissions"]')" = "{'view': True, 'consume': True, 'manage': True, 'replace_header': False, 'replace_secret_env': False}" ] || die 3 "permissions (header = platform-admin): $(jq_ 'd["permissions"]')"
  echo "$code" >"$WORK/pub-$name"   # pass 메시지용 (함수는 명령 치환 안에서 돈다)
  jq_ 'd["id"]'
}
HCALC="$(publish_inst "$I1" calculator)"
HORD="$(publish_inst "$I2" orders)"
PUB_CODES="$(cat "$WORK/pub-calculator")/$(cat "$WORK/pub-orders")"
api GET "/mcp-servers/$I1" >/dev/null
[ "$(jq_ 'd["hub_item"]["id"], d["hub_item"]["name"], d["hub_item"]["gateway_ids"]')" = "$HCALC calculator []" ] || die 3 "GET mcp server hub_item: $(jq_ 'd["hub_item"]')"
api GET /hub >/dev/null
[ "$(jq_ "sorted(i['name'] for i in d if i['kind']=='internal')")" = "['calculator', 'orders']" ] || die 3 "hub internal items: $(jq_ "[i['name'] for i in d if i['kind']=='internal']")"
CALC_WL="$(jq_ "[i['k8s']['name'] for i in d if i['id']=='$HCALC'][0]")"
ORD_WL="$(jq_ "[i['k8s']['name'] for i in d if i['id']=='$HORD'][0]")"
# 감사: 핸들러 이름 publish_server 가 mcp-servers 리소스로 남는다 (F3)
code="$(api GET "/audit?action=publish_server&resource=mcp-servers&tenant=$TENANT")"
[ "$code" = "200" ] || die 3 "GET /audit publish_server -> $code"
python3 - "$WORK/body" "$I1" "$I2" <<'PY' || die 3 "audit lacks publish_server for $I1/$I2"
import json, sys
events = json.load(open(sys.argv[1]))
ok = {e["resource_id"] for e in events if e["result"] == "ok"}
sys.exit(0 if set(sys.argv[2:]) <= ok else 1)
PY
# entry 항목은 프록시를 만들지 않는다. 그 주소는 테넌트의 네임스페이스가 아닌 $SPIKE_NS 의 Service 라(STEP 15 가 그
# 네임스페이스를 422 namespace_not_allowed 로 본다) 플랫폼은 그 주소를 부르지 않는다. 항목을 쓰는 Gateway 의 vMCP 는
# 자기 Entry 로 부른다 (STEP 9e)
left="$("${K[@]}" -n "$I1_NS" get mcpremoteproxy "hub-$SPIKE_ITEM" -o name --ignore-not-found)" || die 3 "listing MCPRemoteProxy hub-$SPIKE_ITEM failed"
[ -z "$left" ] || die 3 "the entry item got a proxy: $left"
entry_refused 3 "$SPIKE" service_namespace
pass 3 "no auto-registration; published calculator=$HCALC($CALC_WL) orders=$HORD($ORD_WL) ($PUB_CODES, hub_item on the document, audit publish_server x2); entry=$SPIKE ($SPIKE_URL) without a proxy, refresh-tools 422 entry_address_refused (service_namespace) and the tools tab listed nothing"

# ---------- 4. gateways A, B 정의 저장 ----------
# 저장만 한다. draft 정의도 허브 항목을 붙든다 (아래 unpublish 의 409 in_use)
mkgw() { # name backends-json [display_name] [label] -> id
  local display=""
  [ -n "${3:-}" ] && display=", \"display_name\": \"$3\""
  save_doc "${4:-4}" /gateways "$1" "{\"name\": \"$1\", \"backends\": $2$display}"
  echo "$SAVED_ID"
}
# 표시 이름(D24). CR 이름은 규칙 안의 name 에 접두어만 붙어 gw-a 가 된다
A_DISPLAY="게이트웨이 A"
A="$(mkgw a "[{\"hub_item_id\": \"$HCALC\", \"tools\": [\"Add\"]}, {\"hub_item_id\": \"$HORD\"}]" "$A_DISPLAY")"
B="$(mkgw b "[{\"hub_item_id\": \"$HCALC\", \"tools\": [\"Divide\"]}]")"
api GET "/gateways/$A" >/dev/null
[ "$(jq_ 'd["conflict_resolution"]')" = "auto" ] || die 4 "A conflict_resolution != auto: $(cat "$WORK/body")"
[ "$(jq_ 'd["auth"]')" = "anonymous" ] || die 4 "A auth != anonymous (header mode expected): $(cat "$WORK/body")"
# 참조 중인 게시 항목은 해제할 수 없다 (409 in_use, 참조 Gateway id 목록). MCP Server 응답도 같은 목록을 보인다
code="$(api DELETE "/mcp-servers/$I1/publish")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code']")" = "in_use" ] || die 4 "unpublish referenced calculator -> $code (expected 409 in_use): $(cat "$WORK/body")"
WANT_REFS="$(python3 -c 'import sys; print(sorted(sys.argv[1:]))' "$A" "$B")"
[ "$(jq_ "sorted(d['detail']['gateway_ids'])")" = "$WANT_REFS" ] || die 4 "in_use gateway_ids: $(jq_ "d['detail']['gateway_ids']")"
api GET "/mcp-servers/$I1" >/dev/null
[ "$(jq_ "sorted(d['hub_item']['gateway_ids'])")" = "$WANT_REFS" ] || die 4 "mcp server hub_item.gateway_ids: $(jq_ "d['hub_item']")"
pass 4 "gateways A=$A B=$B 정의 저장 (conflict_resolution=auto, auth=anonymous); unpublish calculator -> 409 in_use [A, B], hub_item.gateway_ids == [A, B]"

# ---------- 4b. references (D43) ----------
# 삭제·해제를 눌러 409 를 받아야 알 수 있던 것을 읽기 하나로 답한다. 허브 항목은 그것을 묶은
# Gateway 를, MCP Server 는 그 허브 항목과 (항목을 거쳐) 같은 Gateway 를 답한다. draft 정의도 센다
code="$(api GET "/hub/$HCALC/references")"
[ "$code" = "200" ] || die 4b "GET /hub/$HCALC/references -> $code"
[ "$(jq_ "sorted([r['id'] for r in d['used_by'] if r['kind']=='gateway'])")" = "$WANT_REFS" ] \
  || die 4b "hub item references: $(cat "$WORK/body")"
code="$(api GET "/mcp-servers/$I1/references")"
[ "$code" = "200" ] || die 4b "GET /mcp-servers/$I1/references -> $code"
[ "$(jq_ "sorted([r['id'] for r in d['used_by'] if r['kind']=='gateway'])")" = "$WANT_REFS" ] \
  || die 4b "mcp server references (gateway): $(cat "$WORK/body")"
[ "$(jq_ "[r['via'] for r in d['used_by'] if r['kind']=='gateway'][0]")" = "hub-item" ] \
  || die 4b "mcp server references via: $(cat "$WORK/body")"
[ "$(jq_ "[r['id'] for r in d['used_by'] if r['kind']=='hub-item']")" = "['$HCALC']" ] \
  || die 4b "mcp server references (hub-item): $(cat "$WORK/body")"
# 이름은 문서의 것이고 날 id 만 오지 않는다 (화면이 링크 이름으로 쓴다)
[ -n "$(jq_ "[r['name'] for r in d['used_by'] if r['kind']=='gateway'][0]")" ] \
  || die 4b "mcp server references name is empty: $(cat "$WORK/body")"
pass 4b "references: hub $HCALC -> gateways [A, B], mcp-server $I1 -> hub-item $HCALC + gateways [A, B] via hub-item"

# ---------- 5. gateways A, B 배포 -> Ready ----------
deploy_doc 5 /gateways "$A"
deploy_doc 5 /gateways "$B"
wait_ready "/gateways/$A" 5; URL_A="$(status_url)"
wait_ready "/gateways/$B" 5; URL_B="$(status_url)"
api GET "/gateways/$A" >/dev/null; NS="$(jq_ 'd["k8s"]["namespace"]')"; NA="$(jq_ 'd["k8s"]["name"]')"
[ "$(jq_ 'd["display_name"]')" = "$A_DISPLAY" ] || die 5 "A display_name=$(jq_ 'd["display_name"]') (expected $A_DISPLAY)"
# 배포 쪽 필드에는 접두가 붙는다. 정의는 backends·conflict_resolution, 배포 스냅샷은 deployed_* 다 (D42)
A_CALC="$(jq_ "[b['workload'] for b in d['deployed_backends'] if b['hub_item_id']=='$HCALC'][0]")"
A_ORD="$(jq_ "[b['workload'] for b in d['deployed_backends'] if b['hub_item_id']=='$HORD'][0]")"
[ "$(jq_ 'd["state"]')" = "deployed" ] || die 5 "A state=$(jq_ 'd["state"]') (expected deployed)"
[ "$(jq_ 'd["deployed_conflict_resolution"]')" = "priority" ] || die 5 "A deployed_conflict_resolution != priority: $(cat "$WORK/body")"
[ "$(jq_ 'd["exposure"]["enabled"]')" = "False" ] || die 5 "A exposure.enabled != false: $(cat "$WORK/body")"
api GET "/gateways/$B" >/dev/null; NB="$(jq_ 'd["k8s"]["name"]')"
B_CALC="$(jq_ "[b['workload'] for b in d['deployed_backends'] if b['hub_item_id']=='$HCALC'][0]")"
pass 5 "gateways A=$A (display_name=$A_DISPLAY; $URL_A; entries $A_CALC,$A_ORD) B=$B ($URL_B; entry $B_CALC) 배포 Ready"

# ---------- 6. tools/list subsets ----------
pf() { # svc port-local [STEP [NAMESPACE]]
  start_pf "${4:-$NS}" "svc/$1" "$2:4483"; PFS+=("$PF_PID"); PFS_PORTS+=("$2")
  wait_port "$2" || die "${3:-6}" "port-forward $1 :$2 did not open"; }
# list_redeployed PORT URL -> 다시 배포한 Gateway 의 tools/list. 다시 배포가 vMCP 파드를 바꾸면 먼저 연 kubectl 은 내려간
# 파드에 붙은 채 포트를 쥐고 있어 요청이 끊긴다. 새 파드로 다시 열고(lib/e2e_pf.sh restart_pf) 답할 때까지 다시 부른다
list_redeployed() {
  local out i
  restart_pf "$1"
  for ((i = 0; i < 30; i++)); do
    out="$($MCP list "$2" 2>"$WORK/list.err")" && { echo "$out"; return 0; }
    sleep 2
  done
  cat "$WORK/list.err" >&2
  return 1
}
PA=$PF_PORT_BASE; PB=$((PF_PORT_BASE + 1))
pf "vmcp-$NA" "$PA"; pf "vmcp-$NB" "$PB"
UA="http://127.0.0.1:$PA/mcp"; UB="http://127.0.0.1:$PB/mcp"
LA="$($MCP list "$UA")" || die 6 "tools/list A failed: $LA"
LB="$($MCP list "$UB")" || die 6 "tools/list B failed: $LB"
echo "  A: $LA"; echo "  B: $LB"
# priority 모드: 접두사 없이 Add / orders 툴들 / Divide. (충돌 툴만 <item>_<tool>. 여기서는 충돌 없음)
python3 - "$LA" "$LB" <<'PYEOF' || die 6 "tools/list subsets wrong"
import json, sys
a = json.loads(sys.argv[1])["tools"]; b = json.loads(sys.argv[2])["tools"]
assert "Add" in a and "Divide" not in a, a
assert "CreateOrder" in a and "GetOrder" in a, a
assert b == ["Divide"], b
assert not any("spike" in t or "_" in t for t in a + b), (a, b)   # entry 항목의 툴 없음, 접두사 없음
PYEOF
pass 6 "A tools == [Add, CreateOrder, GetOrder]; B tools == [Divide]; no entry tools, no prefixes"

# ---------- 7. calls ----------
# STEP 16b 가 늘어난 수를 보도록 부르기 전의 메트릭을 둔다. 이전 실행이 남긴 calculator 에는 그 실행의 호출이 담겨 있다.
# A·B 는 STEP 0 뒤에 새로 만든 문서라 0 이지만 같은 길로 읽는다
metrics_base 7 "/mcp-servers/$I1" "$WORK/m0-calc"
metrics_base 7 "/gateways/$A" "$WORK/m0-a"
metrics_base 7 "/gateways/$B" "$WORK/m0-b"
CALLS_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # STEP 8r 이 B 의 감사 기록을 찾기 시작할 시각이다
CA="$($MCP call "$UA" Add '{"a": 3, "b": 4}')" || die 7 "A Add failed: $CA"
call_result_is 7 "$CA" || die 7 "A Add != 7: $CA"
if CB="$($MCP call "$UB" Add '{"a": 3, "b": 4}' 2>&1)"; then die 7 "B Add must fail: $CB"; fi
echo "$CB" | grep -qi "not found" || die 7 "B Add error is not 'not found': $CB"
if CS="$($MCP call "$UA" Subtract '{"a": 3, "b": 4}' 2>&1)"; then die 7 "A Subtract must fail: $CS"; fi
echo "$CS" | grep -qi "not found" || die 7 "A Subtract error is not 'not found': $CS"
pass 7 "A Add(3,4)=7; B Add not found; A Subtract not found"

# ---------- 8. CR names gw-<name>, daegam.devops.ai.kr/id labels, redeploy keeps the name ----------
# 규칙 안의 name 에는 k8s_name 이 항등이라 정규화·접미사 없이 정확히 gw-<name> 이다 (D24)
[ "$NA" = "gw-a" ] && [ "$NB" = "gw-b" ] || die 8 "CR names must be exactly gw-<name>: A=$NA B=$NB"
"${K[@]}" -n "$NS" get virtualmcpserver "$NA" -o jsonpath='{.metadata.labels.daegam\.devops\.ai\.kr/id}' >"$WORK/body" || die 8 "vMCP $NA missing"
[ "$(cat "$WORK/body")" = "$A" ] || die 8 "vMCP $NA daegam.devops.ai.kr/id != $A"
"${K[@]}" -n "$NS" get mcpgroup "$NA" -o jsonpath='{.metadata.labels.daegam\.devops\.ai\.kr/id}' >"$WORK/body" || die 8 "MCPGroup $NA missing"
[ "$(cat "$WORK/body")" = "$A" ] || die 8 "MCPGroup $NA daegam.devops.ai.kr/id != $A"
n_entries="$("${K[@]}" -n "$NS" get mcpserverentries -l "daegam.devops.ai.kr/id=$A" -o name | wc -l | tr -d ' ')"
[ "$n_entries" = "2" ] || die 8 "expected 2 entries with daegam.devops.ai.kr/id=$A, got $n_entries"
# 라벨 값 gateway-instance 는 **일부러 옛 이름**이다. 살아 있는 CR 에 박혀 있어 D42 가 바꾸지 않기로 한
# 값이라 이관 누락이 아니다 (UI 도 같은 사정을 ui/src/pages/gateways/Detail.tsx 에 적어 두었다)
RES_LABEL="$("${K[@]}" -n "$NS" get virtualmcpserver "$NA" -o jsonpath='{.metadata.labels.daegam\.devops\.ai\.kr/resource}')"
[ "$RES_LABEL" = "gateway-instance" ] || die 8 "vMCP resource label = $RES_LABEL"
code="$(api POST "/gateways/$A/redeploy")"; [ "$code" = "200" ] || die 8 "redeploy A -> $code"
[ "$(jq_ 'd["k8s"]["name"]')" = "$NA" ] || die 8 "redeploy changed the CR name: $(jq_ 'd["k8s"]["name"]')"
wait_ready "/gateways/$A" 8
LA="$(list_redeployed "$PA" "$UA")" || die 8 "tools/list A after redeploy failed: $LA"
[ "$(tools_sorted "$LA")" = "$A_TOOLS" ] || die 8 "A tools changed after redeploy: $LA"
pass 8 "CRs named $NA, $NB with daegam.devops.ai.kr/id=$A (group, $n_entries entries, vMCP resource=$RES_LABEL); redeploy keeps the name"

# ---------- 8r. header 모드의 Tool 규칙은 집행되지 않고 Gateway 가 그것을 알린다 (P52 2절) ----------
# 규칙은 Gateway 정의의 일부라 새 리비전을 재배포해야 선다. header 모드는 누가 부를지의 규칙을 렌더하지 않는다. B 의 정의는
# 이름과 백엔드뿐이라(STEP 4) 같은 본문에 규칙만 더해 덮는다
b_body() { # TOOL_RULES_JSON
  echo "{\"name\": \"b\", \"backends\": [{\"hub_item_id\": \"$HCALC\", \"tools\": [\"Divide\"], \"tool_rules\": $1}]}"
}
b_rule_warnings() { # -> B 상태 경고 가운데 규칙 경고의 코드 목록
  api GET "/gateways/$B" >/dev/null
  jq_ '[w.split(":")[0] for w in (d.get("status") or {}).get("warnings", []) if w.startswith("rules_")]'
}
# 다시 배포는 B 의 vMCP 파드를 바꾼다. 수집기는 60초마다 파드 로그를 읽고 파드가 바뀌면 옛 파드의 읽지 않은 줄을 잃으므로
# (services/audit_collector.py), STEP 16b 가 세는 STEP 7 의 B Add 가 감사 기록에 들어온 뒤에 다시 배포한다
seen=0
for ((i = 0; i < 18; i++)); do
  code="$(API_OUT="$WORK/audit-b" api GET "/audit/mcp?type=mcp_tool_call&tool=Add&since=$CALLS_SINCE&limit=1000")"
  if [ "$code" = 200 ] && [ "$(python3 -c 'import json, sys
print(sum(1 for e in json.load(open(sys.argv[1])) if e.get("instance_id") == sys.argv[2]))' "$WORK/audit-b" "$B")" != 0 ]; then
    seen=1; break
  fi
  sleep 10
done
[ "$seen" = 1 ] || die 8r "the audit record of STEP 7's Add at B was not collected within 180s (GET /audit/mcp -> $code)"
code="$(api PUT "/gateways/$B" "$(b_body '{"Divide": {"callers": {"roles": ["developer"]}}}')")"
[ "$code" = "200" ] || die 8r "PUT /gateways/$B with a rule on Divide -> $code: $(cat "$WORK/body")"
code="$(api POST "/gateways/$B/redeploy")"; [ "$code" = "200" ] || die 8r "redeploy B -> $code"
wait_ready "/gateways/$B" 8r
warned="$(b_rule_warnings)"
[ "$warned" = "['rules_not_enforced']" ] || die 8r "B rule warnings in header mode: $warned (want ['rules_not_enforced'])"
LB="$(list_redeployed "$PB" "$UB")" || die 8r "tools/list B after the rule failed: $LB"
[ "$(tools_sorted "$LB")" = "$(tools_sorted '{"tools": ["Divide"]}')" ] || die 8r "B tools with an unenforced rule: $LB (want [Divide])"
code="$(api PUT "/gateways/$B" "$(b_body '{}')")"
[ "$code" = "200" ] || die 8r "PUT /gateways/$B without rules -> $code: $(cat "$WORK/body")"
code="$(api POST "/gateways/$B/redeploy")"; [ "$code" = "200" ] || die 8r "redeploy B -> $code"
wait_ready "/gateways/$B" 8r
[ "$(b_rule_warnings)" = "[]" ] || die 8r "B still warns after its rule was removed: $(b_rule_warnings)"
pass 8r "header mode: B with a rule on Divide warns rules_not_enforced and still lists [Divide]; removing the rule clears the warning"

# ---------- 9. publish A -> C consumes; tools/list == A advertised; Add == 7 ----------
# 본문을 비우면 게시 이름은 문서 name 그대로(a), display_name 은 문서의 것이다 (D24). A 는 STEP 0 뒤에 새로 만든
# 문서라 게시 항목도 새것이고, 같은 이름의 항목이 남아 있으면 409 name_conflict 로 멈춘다
code="$(api POST "/gateways/$A/publish" '{}')"
[ "$code" = "201" ] || die 9 "publish -> $code (expected 201): $(cat "$WORK/body")"
PUB="$(jq_ 'd["id"]')"
api GET "/hub/$PUB" >/dev/null
# source.gateway_instance_id 도 일부러 옛 이름이다. 저장된 허브 문서의 필드라 그대로 둔다 (STEP 8 주석)
[ "$(jq_ 'd["source"]["gateway_instance_id"]')" = "$A" ] || die 9 "published item source.gateway_instance_id != $A"
[ "$(jq_ 'd["name"], d["display_name"]')" = "a $A_DISPLAY" ] || die 9 "published defaults: $(jq_ 'd["name"], d["display_name"]') (expected 'a $A_DISPLAY')"
PUB_TOOLS="$(jq_ 'sorted(d["tool_names"])')"
[ "$PUB_TOOLS" = "$A_TOOLS" ] || die 9 "published tool_names snapshot wrong: $PUB_TOOLS"
C="$(mkgw c "[{\"hub_item_id\": \"$PUB\"}]" "" 9)"
metrics_base 9 "/gateways/$C" "$WORK/m0-c"   # STEP 16b 의 기준값 (STEP 7 의 주석)
deploy_doc 9 /gateways "$C"
wait_ready "/gateways/$C" 9
api GET "/gateways/$C" >/dev/null; NC="$(jq_ 'd["k8s"]["name"]')"
PC=$((PF_PORT_BASE + 2)); pf "vmcp-$NC" "$PC"
UC="http://127.0.0.1:$PC/mcp"
LC="$($MCP list "$UC")" || die 9 "tools/list C failed: $LC"
echo "  C: $LC"
# priority 모드: 중첩 접두사 없이 A 의 광고 이름 그대로다
[ "$(tools_sorted "$LC")" = "$A_TOOLS" ] || die 9 "C tools wrong: $LC"
CC="$($MCP call "$UC" Add '{"a": 3, "b": 4}')" || die 9 "C Add failed: $CC"
call_result_is 7 "$CC" || die 9 "C Add != 7: $CC"
LA2="$($MCP list "$UA")" || die 9 "A tools/list after publish failed"
[ "$(tools_sorted "$LA2")" = "$A_TOOLS" ] || die 9 "A affected by publish: $LA2"
pass 9 "published A as 'a' (default name = 문서 name, display_name=$A_DISPLAY) consumed by C; C tools == A advertised; C Add(3,4)=7; A unaffected"

# ---------- 9e. entry 항목: 다른 테넌트의 Gateway 가 자기 Entry 로 부른다 + 플랫폼의 주소 규칙 ----------
# entry 항목은 복제하지 않는다. 그 항목을 쓰는 Gateway 는 어느 테넌트에 있든 자기 MCPServerEntry 로 항목의 주소를 부른다.
# 플랫폼은 STEP 3 의 주소를 부르지 않으므로 항목의 Tool 이름을 모르고, 배포의 도달 확인도 부르지 않고 그 백엔드를 준비된
# 것으로 센다. 이름을 모르는 백엔드는 auto 모드에서 접두사가 붙으므로 priority 로 저장해 서버의 이름 Add 그대로 부른다.
# vMCP 포트는 뒤의 9h 와 같은 자리를 쓰므로 이 단계 안에서 닫는다
TENANT="$TENANT_B" api GET /gateways >/dev/null; old="$(find_id eb)"
[ -z "$old" ] || TENANT="$TENANT_B" api_delete "/gateways/$old" >/dev/null || true
TENANT="$TENANT_B" save_doc 9e /gateways eb "{\"name\": \"eb\", \"conflict_resolution\": \"priority\", \"backends\": [{\"hub_item_id\": \"$SPIKE\"}]}"
EB="$SAVED_ID"
TENANT="$TENANT_B" deploy_doc 9e /gateways "$EB"
TENANT="$TENANT_B" wait_ready "/gateways/$EB" 9e
TENANT="$TENANT_B" api GET "/gateways/$EB" >/dev/null
NSB="$(jq_ 'd["k8s"]["namespace"]')"; NEB="$(jq_ 'd["k8s"]["name"]')"
[ "$NSB" != "$NS" ] || die 9e "tenant $TENANT_B deployed into $TENANT's namespace $NS"
EB_ROW="$(jq_ "[(b['kind'], b['url'], b['clone_name'], b['clone_kind']) for b in d['deployed_backends'] if b['hub_item_id']=='$SPIKE'][0]")"
[ "$EB_ROW" = "entry $SPIKE_URL None None" ] \
  || die 9e "eb's snapshot row for the entry item: $EB_ROW (want entry, $SPIKE_URL, no clone)"
EB_WL="$(jq_ "[b['workload'] for b in d['deployed_backends'] if b['hub_item_id']=='$SPIKE'][0]")"
"${K[@]}" -n "$NSB" get mcpserverentry "$EB_WL" -o jsonpath='{.spec.remoteUrl}' >"$WORK/body" || die 9e "MCPServerEntry $NSB/$EB_WL missing"
[ "$(cat "$WORK/body")" = "$SPIKE_URL" ] || die 9e "Entry $NSB/$EB_WL remoteUrl: $(cat "$WORK/body") (want $SPIKE_URL)"
for ns in "$NS" "$NSB"; do
  left="$("${K[@]}" -n "$ns" get mcpremoteproxies -o name)" || die 9e "listing MCPRemoteProxy in $ns failed"
  ! grep -Eq "/(hub|shared)-$SPIKE_ITEM" <<<"$left" || die 9e "a proxy or a clone of the entry item runs in $ns: $left"
done
clone_records 9e; CLONES1="$CLONE_RECORDS"
[ "$CLONES1" -le "$CLONES0" ] || die 9e "remote clone records grew from $CLONES0 to $CLONES1"
PEB=$((PF_PORT_BASE + 3)); pf "vmcp-$NEB" "$PEB" 9e "$NSB"; PF_EB="$PF_PID"
UEB="http://127.0.0.1:$PEB/mcp"
# 방금 뜬 vMCP 는 백엔드의 첫 점검이 지나야 그 Tool 을 내놓는다
seen=""
for ((i = 0; i < 24; i++)); do
  LEB="$($MCP list "$UEB" 2>/dev/null || true)"
  [ -n "$LEB" ] && [ "$(tools_sorted "$LEB" 2>/dev/null || true)" = "['Add']" ] && { seen=1; break; }
  sleep 5
done
[ -n "$seen" ] || die 9e "eb tools/list is not [Add]: $LEB"
CEB="$($MCP call "$UEB" Add '{"a": 3, "b": 4}')" || die 9e "eb Add failed: $CEB"
call_result_is 7 "$CEB" || die 9e "eb Add != 7: $CEB"
stop_pf "$PF_EB" "$PEB"
# 플랫폼이 직접 부르는 길의 거절 둘. 다른 테넌트 네임스페이스의 Service 로는 eb 의 vMCP 를 쓴다. 사설 주소로는 spike
# Service 의 ClusterIP 를 쓴다. 설치본이 사설망을 열어 두었으면(DAEGAM_ENTRY_PRIVATE_ALLOW) 그 주소가 열렸는지 알 수
# 없으므로 사설 주소는 보지 않는다
refused_entry() { # NAME URL REASON -> 그 주소의 entry 항목을 등록해 거절을 보고 지운다
  local id code
  api GET /hub >/dev/null; id="$(find_id "$1")"
  [ -z "$id" ] || api_delete "/hub/$id" >/dev/null || true
  code="$(api POST /hub "{\"name\": \"$1\", \"kind\": \"entry\", \"url\": \"$2\", \"transport\": \"streamable-http\"}")"
  [ "$code" = "201" ] || die 9e "POST /hub $1 -> $code: $(cat "$WORK/body")"
  id="$(jq_ 'd["id"]')"
  entry_refused 9e "$id" "$3"
  api_delete "/hub/$id" >/dev/null || die 9e "delete hub item $1 failed"
}
refused_entry e2e-neighbour "http://vmcp-$NEB.$NSB.svc.cluster.local:4483/mcp" service_namespace
PRIVATE_NOTE="a private address (url_private)"
PRIVATE_ALLOW="$(platform_config DAEGAM_ENTRY_PRIVATE_ALLOW)" || die 9e "reading the platform ConfigMap failed"
if [ -z "$PRIVATE_ALLOW" ]; then
  SPIKE_IP="$("${K[@]}" -n "$SPIKE_NS" get svc "$SPIKE_SVC" -o jsonpath='{.spec.clusterIP}')" || die 9e "Service $SPIKE_NS/$SPIKE_SVC missing"
  [ -n "$SPIKE_IP" ] || die 9e "Service $SPIKE_NS/$SPIKE_SVC has no ClusterIP"
  refused_entry e2e-private "http://$SPIKE_IP:8080/mcp" url_private
else
  PRIVATE_NOTE="no private address checked, because this install sets DAEGAM_ENTRY_PRIVATE_ALLOW"
  echo "  9e: DAEGAM_ENTRY_PRIVATE_ALLOW is set on this install; the private-address refusal is not checked"
fi
TENANT="$TENANT_B" api_delete "/gateways/$EB" >/dev/null || die 9e "delete gateway eb failed"
TENANT="$TENANT_B" wait_gone "/gateways/$EB" || die 9e "gateway eb ($EB) is still there after its delete"
pass 9e "$TENANT_B's Gateway eb called the entry item through its own Entry $NSB/$EB_WL -> $SPIKE_URL (Add(3,4)=7; snapshot row without a clone, no hub- or shared- MCPRemoteProxy in $NS or $NSB, remote clone records $CLONES0 -> $CLONES1); refresh-tools refused a Service of $NSB (service_namespace) and $PRIVATE_NOTE"

# ---------- 9h. 외부 서버의 고정 헤더 (P86 8절) ----------
# 헤더를 요구하고 되돌려 주는 lab 서버(deploy/dev/lab/images/header-echo)를 테넌트 네임스페이스에 띄운다. 인증을 싣는
# 항목의 URL 은 https 이거나 그 테넌트 네임스페이스의 Service 여야 하기 때문이다(outside_auth_not_https). 틀린 값은
# 저장되지만 header_refused 로 답하고, 맞는 값은 Gateway h 를 거쳐 서버에 전달되며, 값을 바꾸면 재적용이 vMCP 를 다시
# 렌더해 새 값이 전달된다. h 를 항목 없는 리비전으로 재배포하면 사본이 없고 calculator 는 그대로 답한다. 다른 테넌트
# $TENANT_B 의 Gateway hb 도 이 항목을 쓰지만, 테넌트 ingress NetworkPolicy 때문에 이 Service 에 연결할 수 없어 호출하지
# 않고 사본만 본다. 주인이 consume 에서 그 테넌트를 빼면 사본이 없어진다
HDR_NAME="X-API-Key"; HDR_V1="e2e-p86-one-$$"; HDR_V2="e2e-p86-two-$$"; HDR_WRONG="e2e-p86-wrong-$$"
HDR_IMAGE="${HEADER_ECHO_IMAGE:-daegam-header-echo:dev}"
copies() { # GATEWAY_ID NAMESPACE -> 그 Gateway 의 헤더 사본 Secret 수. kubectl 이 실패하면 멈춘다
  local out
  out="$("${K[@]}" -n "$2" get secret -l "daegam.devops.ai.kr/id=$1" -o name)" || die 9h "kubectl get secret in $2 failed"
  grep -c "/hdr-" <<<"$out" || true
}
wait_copies() { # GATEWAY_ID NAMESPACE present|gone SECONDS
  local i n
  for ((i = 0; i < $4 / 5; i++)); do
    n="$(copies "$1" "$2")"
    case "$3" in present) [ "$n" -ge 1 ] && return 0 ;; gone) [ "$n" = 0 ] && return 0 ;; esac
    sleep 5
  done
  return 1
}
# 이전 실행이 남긴 h, hb 와 항목을 지운다
api GET /gateways >/dev/null; old="$(find_id h)"; [ -z "$old" ] || api_delete "/gateways/$old" >/dev/null || true
TENANT="$TENANT_B" api GET /gateways >/dev/null; old="$(find_id hb)"
[ -z "$old" ] || TENANT="$TENANT_B" api_delete "/gateways/$old" >/dev/null || true
api GET /hub >/dev/null; old="$(find_id e2e-header)"; [ -z "$old" ] || api_delete "/hub/$old" >/dev/null || true
cat >"$WORK/header-echo.yaml" <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: header-echo, namespace: $NS, labels: {app: header-echo}}
spec:
  replicas: 1
  selector: {matchLabels: {app: header-echo}}
  template:
    metadata: {labels: {app: header-echo}}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 1001, runAsGroup: 1001}
      containers:
        - name: echo
          image: $HDR_IMAGE
          imagePullPolicy: IfNotPresent
          env:
            - {name: HEADER_NAME, value: "$HDR_NAME"}
            - {name: HEADER_VALUES, value: "$HDR_V1,$HDR_V2"}
          ports: [{name: http, containerPort: 18082}]
          readinessProbe: {httpGet: {path: /healthz, port: http}, periodSeconds: 2}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true}
---
apiVersion: v1
kind: Service
metadata: {name: header-echo, namespace: $NS}
spec:
  selector: {app: header-echo}
  ports: [{name: http, port: 18082, targetPort: http}]
YAML
"${K[@]}" apply -f "$WORK/header-echo.yaml" >/dev/null || die 9h "header-echo apply failed"
"${K[@]}" -n "$NS" rollout status deploy/header-echo --timeout=120s >/dev/null || die 9h "header-echo did not become ready ($HDR_IMAGE)"
HDR_URL="http://header-echo.$NS.svc.cluster.local:18082/mcp"
HDR_ACCESS="{\"view\": {\"tenants\": [\"$TENANT_B\"]}, \"consume\": {\"tenants\": [\"$TENANT_B\"]}}"
code="$(api POST /hub "{\"name\": \"e2e-header\", \"kind\": \"entry\", \"url\": \"$HDR_URL\", \"transport\": \"streamable-http\", \"access\": $HDR_ACCESS, \"auth\": {\"kind\": \"header\", \"header_name\": \"$HDR_NAME\", \"header_value\": \"$HDR_WRONG\"}}")"
[ "$code" = "201" ] || die 9h "POST /hub header item -> $code: $(cat "$WORK/body")"
! grep -q "$HDR_WRONG" "$WORK/body" || die 9h "the answer carries the header value"
HID="$(jq_ 'd["id"]')"
# 방금 뜬 파드는 Ready 여도 플랫폼에서 Service 로 아직 닿지 않을 수 있다(엔드포인트와 NetworkPolicy 가 따라오는 사이).
# 그때의 판정은 unreachable 이므로 같은 틀린 값을 다시 저장해 판정을 다시 받는다
for ((i = 0; i < 12; i++)); do
  [ "$(jq_ 'd["auth"]["check"]["outcome"]')" = unreachable ] || break
  sleep 5
  code="$(api PUT "/hub/$HID/auth/header" "{\"value\": \"$HDR_WRONG\"}")"
  [ "$code" = "200" ] || die 9h "PUT the wrong header value again -> $code: $(cat "$WORK/body")"
done
[ "$(jq_ 'd["auth"]["check"]["outcome"]')" = header_refused ] || die 9h "a wrong value saved without header_refused: $(jq_ 'd["auth"]')"
code="$(api PUT "/hub/$HID/auth/header" "{\"value\": \"$HDR_V1\"}")"
[ "$code" = "200" ] && [ "$(jq_ 'd["auth"]["check"]["outcome"]')" = ok ] || die 9h "PUT header value -> $code: $(jq_ 'd.get("auth")')"
code="$(api POST "/hub/$HID/refresh-tools")"; [ "$code" = "200" ] || die 9h "refresh-tools -> $code"
H="$(mkgw h "[{\"hub_item_id\": \"$HCALC\", \"tools\": [\"Add\"]}, {\"hub_item_id\": \"$HID\"}]" "" 9h)"
deploy_doc 9h /gateways "$H"
wait_ready "/gateways/$H" 9h
api GET "/gateways/$H" >/dev/null; NH="$(jq_ 'd["k8s"]["name"]')"
PH=$((PF_PORT_BASE + 3)); pf "vmcp-$NH" "$PH"
UH="http://127.0.0.1:$PH/mcp"
echo_value() { $MCP call "$UH" echo_header '{}' 2>/dev/null || true; }
EH="$(echo_value)"; case "$EH" in *"$HDR_V1"*) ;; *) die 9h "echo_header did not see the first value: $EH" ;; esac
code="$(api PUT "/hub/$HID/auth/header" "{\"value\": \"$HDR_V2\"}")"; [ "$code" = "200" ] || die 9h "replace -> $code"
# 재적용이 리컨실 틱(60초)마다 돌고 vMCP 파드가 굴러간다
seen=""
for ((i = 0; i < 30; i++)); do
  EH="$(echo_value)"; case "$EH" in *"$HDR_V2"*) seen=1; break ;; esac; sleep 10
done
[ -n "$seen" ] || die 9h "the replaced value never reached the server: $EH"
[ "$(copies "$H" "$NS")" -ge 1 ] || die 9h "no header copy next to vMCP $NH"
# 다른 테넌트의 Gateway hb: 사본이 그 네임스페이스에 생기고, 주인이 consume 에서 빼면 없어진다
HB="$(TENANT="$TENANT_B" mkgw hb "[{\"hub_item_id\": \"$HID\"}]" "" 9h)"
TENANT="$TENANT_B" deploy_doc 9h /gateways "$HB"
TENANT="$TENANT_B" api GET "/gateways/$HB" >/dev/null; NSB="$(jq_ 'd["k8s"]["namespace"]')"
wait_copies "$HB" "$NSB" present 120 || die 9h "no header copy in $NSB for Gateway hb"
code="$(api PUT "/hub/$HID/access" '{}')"; [ "$code" = "200" ] || die 9h "narrowing the header item -> $code"
wait_copies "$HB" "$NSB" gone 300 || die 9h "the header copy stayed in $NSB after $TENANT_B left consume"
TENANT="$TENANT_B" api_delete "/gateways/$HB" >/dev/null || die 9h "delete gateway hb failed"
# 항목을 뺀 리비전으로 재배포하면 사본이 곧바로 없고 calculator 는 그대로 답한다
save_doc 9h /gateways h "{\"name\": \"h\", \"backends\": [{\"hub_item_id\": \"$HCALC\", \"tools\": [\"Add\"]}]}"
code="$(api POST "/gateways/$H/redeploy")"; [ "$code" = "200" ] || die 9h "redeploy h without the item -> $code"
wait_ready "/gateways/$H" 9h
[ "$(copies "$H" "$NS")" = "0" ] || die 9h "header copies left after h dropped the item"
seen=""
for ((i = 0; i < 12; i++)); do
  CH="$($MCP call "$UH" Add '{"a": 3, "b": 4}' 2>/dev/null || true)"; call_result_is 7 "$CH" && { seen=1; break; }; sleep 5
done
[ -n "$seen" ] || die 9h "h stopped answering Add after it dropped the item: $CH"
api_delete "/gateways/$H" >/dev/null || die 9h "delete gateway h failed"
api_delete "/hub/$HID" >/dev/null || die 9h "delete header item failed"
"${K[@]}" delete -f "$WORK/header-echo.yaml" --ignore-not-found >/dev/null
pass 9h "header item: a wrong value saved as header_refused, the right value reached the echo server through Gateway h, a replaced value after the reapply; $TENANT_B's Gateway kept a copy until $TENANT_B left consume; h without the item kept no copy and still added"

# ---------- 11. 게시 항목의 Tool 이름은 원본이 정한다 (D49) + live tools/list (D48) ----------
# published 항목의 도구 이름은 문서가 정하므로 refresh-tools 는 422 derived_tools 다 (D49). 라이브 tools/list 는 도구
# 탭이 부르는 GET /hub/{id}/tools 가 저장 없이 한다 (D48). 플랫폼이 원본 Gateway 의 vMCP 를 부른다. STEP 9 의 항목을 쓴다
code="$(api POST "/hub/$PUB/refresh-tools")"
[ "$code" = "422" ] && [ "$(jq_ "d['detail']['code']")" = derived_tools ] || die 11 "refresh-tools on a published item -> $code (expected 422 derived_tools)"
code=""; listed=""
for ((i = 0; i < 12; i++)); do
  code="$(api GET "/hub/$PUB/tools")"
  [ "$code" = "200" ] && listed="$(jq_ 'd["listed"]')" && [ "$listed" = True ] && break
  sleep 5
done
[ "$code" = "200" ] && [ "$listed" = True ] || die 11 "GET /hub/$PUB/tools -> $code listed=$listed"
PT="$(jq_ 'sorted(t["name"] for t in d["tools"])')"
[ "$PT" = "$A_TOOLS" ] || die 11 "live tools/list of the published item wrong: $PT"
pass 11 "published item: refresh-tools 422 derived_tools; live tools/list == A advertised"

# ---------- 12. exposure prerequisites: Traefik IP, TLS Secret, exposure-options ----------
LB_IP="$("${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
[ -n "$LB_IP" ] || die 12 "Service $TRAEFIK_NS/$TRAEFIK_SVC has no LoadBalancer IP (is Traefik installed?)"
"${K[@]}" get ingressclass "$INGRESS_CLASS" -o name >/dev/null || die 12 "IngressClass $INGRESS_CLASS missing"
api GET /clusters >/dev/null
CLUSTER_ID="$(find_id home)"
[ -n "$CLUSTER_ID" ] || die 12 "home cluster not registered"
api GET "/clusters/$CLUSTER_ID" >/dev/null
[ "$(jq_ 'd["ingress"]["enabled"] and d["ingress"]["allow_unauthenticated"]')" = "True" ] \
  || die 12 "home cluster ingress must be enabled with allow_unauthenticated (deploy-local default): $(jq_ 'd["ingress"]')"
# 자체 서명 인증서 → kubernetes.io/tls Secret. 이름만 API 에 보여야 하고 PEM 은 어디에도 나오면 안 된다
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$MCP_HOST" -addext "subjectAltName=DNS:$MCP_HOST" \
  -keyout "$WORK/tls.key" -out "$WORK/tls.crt" >/dev/null 2>&1 || die 12 "openssl self-signed cert failed"
"${K[@]}" -n "$NS" create secret tls "$TLS_SECRET" --cert="$WORK/tls.crt" --key="$WORK/tls.key" --dry-run=client -o yaml \
  | "${K[@]}" apply -f - >/dev/null || die 12 "apply TLS Secret $NS/$TLS_SECRET failed"
code="$(api GET "/clusters/$CLUSTER_ID/exposure-options?namespace=$NS")"
[ "$code" = "200" ] || die 12 "exposure-options -> $code"
python3 - "$WORK/body" "$TLS_SECRET" "$INGRESS_CLASS" "$NS" "$WORK/tls.crt" <<'PY' || die 12 "exposure-options wrong: $(cat "$WORK/body")"
import json, sys
raw = open(sys.argv[1]).read(); d = json.loads(raw)
secret, cls, ns, crt = sys.argv[2:6]
assert d["enabled"] is True and d["allow_unauthenticated"] is True, d
assert d["namespace"] == ns and d["namespaces"][0] == ns, d
assert cls in d["ingress_classes"] and d["defaults"]["ingress_class"] == cls, d
assert secret in d["tls_secrets"], d["tls_secrets"]
pem = open(crt).read().splitlines()[1]          # 인증서 본문 첫 줄 (base64)
assert pem not in raw and "tls.crt" not in raw and "tls.key" not in raw and "data" not in d, "secret data leaked"
PY
CM_ISSUERS="$(jq_ 'd["cluster_issuers"]')"
ISSUER="$(jq_ '(d["cluster_issuers"] or [""])[0]')"   # STEP 16 이 쓴다. 없으면 빈 값이라 STEP 16 을 건너뛴다
pass 12 "traefik $LB_IP, cluster=$CLUSTER_ID, Secret $NS/$TLS_SECRET listed in exposure-options (no data); issuers=$CM_ISSUERS"

# 한 문서는 배포를 하나만 가진다. 같은 정의를 한 벌 더 띄우려면 clone_fresh 로 복제한 뒤 각각 배포한다 (D42).
#
# 노출은 복제 본문이 아니라 배포 본문에 싣는다. 플랜 F5 의 "복제(exposure 포함)" 와 다른 자리다.
# 노출을 켠 배포를 서버가 그 자리에서 받는지 거부하는지가 배포 호출에 그대로 드러나야 하고
# (e2e_agent STEP 13a 의 422 unauthenticated_exposure 가 그 단정이다), e2e 넷이 같은 모양이 된다
# 노출 설정은 배포 본문이 받는다 (tls none)
exp_gw() { echo "{\"enabled\": true, \"host\": \"$1\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"none\"}}"; }

# ---------- 13. A 의 복제본을 http 로 노출 배포 ----------
clone_fresh 13 /gateways "$A" a-exposed
IAX="$(jq_ 'd["id"]')"
code="$(api POST "/gateways/$IAX/deploy" "{\"exposure\": $(exp_gw "$GW_HOST")}")"
[ "$code" = "201" ] || die 13 "POST /gateways/$IAX/deploy a-exposed -> $code"
NAX="$(jq_ 'd["k8s"]["name"]')"
[ "$(jq_ 'd["status"]["external_url"]')" = "http://$GW_HOST/mcp" ] || die 13 "external_url = $(jq_ 'd["status"]["external_url"]')"
[ "$(jq_ 'd["exposure"]["enabled"], d["exposure"]["host"], d["exposure"]["tls"]["mode"]')" = "True $GW_HOST none" ] || die 13 "exposure snapshot: $(jq_ 'd["exposure"]')"
jq_ 'd["warnings"]' | grep -q 'unauthenticated_exposure:' || die 13 "warnings[] lacks unauthenticated_exposure: $(jq_ 'd["warnings"]')"
wait_ready "/gateways/$IAX" 13
wait_exposed "/gateways/$IAX" 13 0
api GET "/gateways/$IAX" >/dev/null
jq_ 'd["status"]["warnings"]' | grep -q 'unauthenticated_exposure:' || die 13 "status.warnings lacks unauthenticated_exposure"
"${K[@]}" -n "$NS" get ingress "$NAX" -o jsonpath='{.spec.ingressClassName} {.spec.rules[0].host} {.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.number}' >"$WORK/body" || die 13 "Ingress $NAX missing"
[ "$(cat "$WORK/body")" = "$INGRESS_CLASS $GW_HOST vmcp-$NAX:4483" ] || die 13 "Ingress $NAX spec: $(cat "$WORK/body")"
"${K[@]}" -n "$NS" get networkpolicy "daegam-expose-$NAX" -o jsonpath='{.spec.podSelector.matchLabels}' >"$WORK/body" || die 13 "NetworkPolicy daegam-expose-$NAX missing"
grep -q "virtualmcpserver" "$WORK/body" || die 13 "expose policy selector: $(cat "$WORK/body")"
code=""
for ((i = 0; i < 12; i++)); do  # Traefik 이 라우트를 집어 들 때까지 (404 → 200)
  code="$(mcp_ingress "http://$GW_HOST/mcp" "$GW_HOST:80:$LB_IP" tools/list)"; [ "$code" = "200" ] && break; sleep 5
done
[ "$code" = "200" ] || die 13 "tools/list via http://$GW_HOST/mcp -> $code: $(cat "$WORK/rpc.raw")"
GT="$(rpc_tools)"; echo "  http://$GW_HOST/mcp tools: $GT"
[ "$GT" = "['Add', 'CreateOrder', 'GetOrder']" ] || die 13 "tools via ingress != A subset: $GT"
# 노출은 MCP 와 그 메타데이터 경로만 연다 (P50 5절). vMCP 의 상태 경로는 토큰 없이 백엔드 이름과 인증 방식을
# 답하므로 Traefik 의 404 여야 한다. 옛 렌더(Prefix /)에서는 셋 다 200 이다
paths="$("${K[@]}" -n "$NS" get ingress "$NAX" -o jsonpath='{.spec.rules[0].http.paths[*].path}')"
[ "$paths" = "/mcp /.well-known/oauth-protected-resource" ] || die 13 "Ingress $NAX paths: $paths"
for p in /status /api/backends/health /health; do
  code="$(get_ingress "http://$GW_HOST$p" "$GW_HOST:80:$LB_IP")"
  [ "$code" = "404" ] || die 13 "GET http://$GW_HOST$p via Traefik -> $code (only /mcp and the metadata path are routed)"
done
pass 13 "gateway a-exposed ($NAX) external_url=http://$GW_HOST/mcp, warnings unauthenticated_exposure, Ingress→vmcp-$NAX:4483 address assigned, tools/list via Traefik == A subset, /status /api/backends/health /health -> 404"

# ---------- 14. calculator 의 복제본을 TLS Secret 으로 노출 배포 (vMCP 앞단 front-<k8s.name>) ----------
clone_fresh 14 /mcp-servers "$I1" calc-exposed
IX="$(jq_ 'd["id"]')"
# STEP 16b 의 기준값과 감사 기록을 읽기 시작할 시각이다. 복제본은 실행마다 새 id 라 이전 실행의 호출이 없다
metrics_base 14 "/mcp-servers/$IX" "$WORK/m0-x"
X_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
code="$(api POST "/mcp-servers/$IX/deploy" "{\"exposure\": {\"enabled\": true, \"host\": \"$MCP_HOST\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"secret\", \"secret_name\": \"$TLS_SECRET\"}}}")"
[ "$code" = "201" ] || die 14 "POST /mcp-servers/$IX/deploy calc-exposed -> $code"
NX="$(jq_ 'd["k8s"]["name"]')"
[ "$(jq_ 'd["status"]["external_url"]')" = "https://$MCP_HOST/mcp" ] || die 14 "external_url = $(jq_ 'd["status"]["external_url"]')"
jq_ 'd["warnings"]' | grep -q 'unauthenticated_exposure:' || die 14 "warnings[] lacks unauthenticated_exposure"
wait_ready "/mcp-servers/$IX" 14
wait_exposed "/mcp-servers/$IX" 14 1
echo "  status.exposure: $EXPOSURE"
python3 -c "import json,sys; e=json.loads(sys.argv[1]); assert e['front_phase']=='Ready' and e['tls_secret_present'] is True and e['address_assigned'] is True, e" "$EXPOSURE" || die 14 "exposure status: $EXPOSURE"
# 새로 노출한 서버에는 옛 앞단(MCPRemoteProxy expose-<k8s.name>)이 없으므로 넘겨받기 없이 처음부터 vMCP 앞단이 Ingress
# 뒤에 서고, 넘겨받기를 기다린다는 경고도 없다 (P54 6절). wait_exposed 가 마지막으로 읽은 문서를 본다
[ "$(jq_ '[w for w in (d.get("status") or {}).get("warnings", []) if w.startswith("front_handover_pending")]')" = "[]" ] \
  || die 14 "a new exposure waits for a handover: $(jq_ 'd["status"]["warnings"]')"
# 앞단은 calc-exposed 하나를 백엔드로 둔 vMCP front-<k8s.name> 이다 (P54 5절). Entry 는 서버의 프록시 Service 를 가리킨다
FX="front-$NX"
"${K[@]}" -n "$NS" get virtualmcpserver "$FX" -o jsonpath='{.status.phase} {.spec.groupRef.name} {.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}' >"$WORK/body" || die 14 "VirtualMCPServer $FX missing"
[ "$(cat "$WORK/body")" = "Ready $FX MCPServer/$NX" ] || die 14 "vMCP front: $(cat "$WORK/body")"
"${K[@]}" -n "$NS" get mcpserverentry "$FX" -o jsonpath='{.spec.remoteUrl} {.spec.groupRef.name}' >"$WORK/body" || die 14 "MCPServerEntry $FX missing"
[ "$(cat "$WORK/body")" = "http://mcp-$NX-proxy.$NS.svc.cluster.local:8080/mcp $FX" ] || die 14 "front Entry: $(cat "$WORK/body")"
left="$("${K[@]}" -n "$NS" get mcpremoteproxy "expose-$NX" -o name --ignore-not-found)" || die 14 "listing MCPRemoteProxy expose-$NX failed"
[ -z "$left" ] || die 14 "a new exposure rendered the old front $left"
# 앞단의 다섯 객체는 리소스 값 mcp-instance-front 와 서버의 id 로 고른다. 되돌림 안내가 이 라벨로 지운다 (P54 10절). header
# 모드의 MCPAuthzConfig 는 permit-all 을 싣는다
"${K[@]}" -n "$NS" get virtualmcpservers,mcpgroups,mcpserverentries,mcpexternalauthconfigs,mcpauthzconfigs \
  -l "daegam.devops.ai.kr/resource=mcp-instance-front,daegam.devops.ai.kr/id=$IX" -o json >"$WORK/front-objs.json" \
  || die 14 "listing the front objects of $FX failed"
python3 - "$WORK/front-objs.json" "$FX" <<'PY' || die 14 "objects labelled mcp-instance-front for $IX: $(json_of "$WORK/front-objs.json" 'sorted(o["kind"] + "/" + o["metadata"]["name"] for o in d["items"])')"
import json, sys
fx = sys.argv[2]
got = {f'{o["kind"]}/{o["metadata"]["name"]}' for o in json.load(open(sys.argv[1]))["items"]}
need = {f"{k}/{fx}" for k in ("VirtualMCPServer", "MCPGroup", "MCPServerEntry", "MCPExternalAuthConfig")}
assert got == need | {f"MCPAuthzConfig/{fx}-authz"}, sorted(got)
PY
"${K[@]}" -n "$NS" get ingress "$NX" -o jsonpath='{.spec.tls[0].secretName} {.spec.tls[0].hosts[0]} {.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.number}' >"$WORK/body" || die 14 "Ingress $NX missing"
[ "$(cat "$WORK/body")" = "$TLS_SECRET $MCP_HOST vmcp-$FX:4483" ] || die 14 "Ingress $NX spec: $(cat "$WORK/body")"
# 노출 정책은 두 종류의 앞단 파드를 고른다(P54 5절). 넘겨받는 동안 두 앞단이 모두 Traefik 에서 닿아야 하기 때문이다
"${K[@]}" -n "$NS" get networkpolicy "daegam-expose-$NX" -o json >"$WORK/np.json" || die 14 "NetworkPolicy daegam-expose-$NX missing"
python3 - "$WORK/np.json" "$NX" "$TRAEFIK_NS" <<'PY' || die 14 "expose policy: $(json_of "$WORK/np.json" 'json.dumps(d["spec"])')"
import json, sys
spec = json.load(open(sys.argv[1]))["spec"]
nx, traefik = sys.argv[2:4]
sel = {e["key"]: (e["operator"], set(e["values"])) for e in spec["podSelector"].get("matchExpressions", [])}
assert not spec["podSelector"].get("matchLabels"), spec["podSelector"]
assert sel == {
    "app.kubernetes.io/name": ("In", {"mcpremoteproxy", "virtualmcpserver"}),
    "app.kubernetes.io/instance": ("In", {f"expose-{nx}", f"front-{nx}"}),
}, sel
assert spec["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == traefik, spec["ingress"]
PY
# Traefik 이 라우트를 집어 들고(404 → 200), 막 뜬 vMCP 앞단이 서버를 라우팅할 때까지 ROUTE_WAIT 안에서 본다
# (lib/mcp_rpc.sh, P54 6절)
code="$(ingress_until 'sorted(t["name"] for t in ((d or {}).get("result") or {}).get("tools", [])) == ["Add", "Divide"]' \
  "https://$MCP_HOST/mcp" "$MCP_HOST:443:$LB_IP" tools/list)"
[ "$code" = "200" ] || die 14 "tools/list via https://$MCP_HOST/mcp -> $code: $(cat "$WORK/rpc.raw")"
XT="$(rpc_tools)"
echo "  https://$MCP_HOST/mcp tools: $XT"
[ "$XT" = "['Add', 'Divide']" ] || die 14 "tools via https ingress != [Add, Divide]: $XT"
# 서빙된 인증서가 우리 Secret 의 것인지 (Traefik 기본 인증서가 아니다)
SERVED_CN="$(echo | openssl s_client -connect "$LB_IP:443" -servername "$MCP_HOST" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null || true)"
echo "$SERVED_CN" | grep -q "CN *= *$MCP_HOST" || die 14 "served certificate is not the Secret's: $SERVED_CN"
code="$(mcp_ingress "https://$MCP_HOST/mcp" "$MCP_HOST:443:$LB_IP" tools/call '{"name": "Add", "arguments": {"a": 3, "b": 4}}')"
[ "$code" = "200" ] || die 14 "tools/call Add via https -> $code"
[ "$(rpc_ 'd["result"]["structuredContent"]')" = "{'result': 7}" ] || die 14 "Add via https != 7: $(cat "$WORK/rpc.json")"
pass 14 "mcp server calc-exposed ($NX, calculator 의 복제본) external_url=https://$MCP_HOST/mcp, vMCP front $FX Ready (Entry -> mcp-$NX-proxy, objects labelled mcp-instance-front, no MCPRemoteProxy), Ingress tls=$TLS_SECRET -> vmcp-$FX:4483, expose policy selects both front kinds, served cert CN=$MCP_HOST, tools/list [Add, Divide], Add(3,4)=7"

# ---------- 15. 422 namespace_not_allowed; home 의 env 필드 PATCH 는 409 ----------
# 네임스페이스는 배포 본문이 받는다. 검증은 상태를 바꾸기 전에 터지므로 실패한 배포는 문서를 draft 그대로
# 둔다. 그래서 시험용 복제본 하나로 여러 번 시험할 수 있다
expect422() { # LABEL CODE PATH ID BODY
  local label="$1" want="$2" path="$3" id="$4" body="$5" code
  code="$(api POST "$path/$id/deploy" "$body")"
  [ "$code" = "422" ] || die 15 "$label -> $code (expected 422 $want)"
  [ "$(jq_ "d['detail']['code']")" = "$want" ] || die 15 "$label -> code $(jq_ "d['detail']['code']") (expected $want)"
}
clone_fresh 15 /mcp-servers "$I1" probe-mcp; PROBE_MS="$(jq_ 'd["id"]')"
clone_fresh 15 /gateways "$A" probe-gw; PROBE_GW="$(jq_ 'd["id"]')"
expect422 "namespace $SPIKE_NS not allowed" namespace_not_allowed /mcp-servers "$PROBE_MS" "{\"namespace\": \"$SPIKE_NS\", \"exposure\": $(exp_gw other.e2e.local)}"
expect422 "gateway namespace $SPIKE_NS not allowed" namespace_not_allowed /gateways "$PROBE_GW" "{\"namespace\": \"$SPIKE_NS\"}"
# home 의 allow_unauthenticated 는 플랫폼 설정(DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED)이 정하고 기동마다 다시
# 쓴다. PATCH 로 다른 값을 주면 다음 기동에 조용히 되돌아가므로 409 env_managed 다(D54)
api GET "/clusters/$CLUSTER_ID" >/dev/null
[ "$(jq_ "'ingress.allow_unauthenticated' in d['env_managed']")" = "True" ] \
  || die 15 "home env_managed does not list ingress.allow_unauthenticated: $(jq_ 'd["env_managed"]')"
CLUSTER_INGRESS_ORIG="$(jq_ 'json.dumps(d["ingress"])')"
PATCHED="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); d["allow_unauthenticated"]=False; print(json.dumps(d))' "$CLUSTER_INGRESS_ORIG")"
code="$(api PATCH "/clusters/$CLUSTER_ID" "{\"ingress\": $PATCHED}")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code']")" = env_managed ] \
  && [ "$(jq_ "','.join(d['detail']['fields'])")" = ingress.allow_unauthenticated ] \
  || die 15 "PATCH home ingress allow_unauthenticated=false -> $code (expected 409 env_managed)"
restore_cluster_ingress
api GET "/clusters/$CLUSTER_ID" >/dev/null
[ "$(jq_ 'd["ingress"]["allow_unauthenticated"]')" = "True" ] || die 15 "allow_unauthenticated not restored: $(jq_ 'd["ingress"]')"
# 시험용 복제본은 draft 그대로다. 클러스터에는 아무것도 없으니 문서만 지운다
code="$(api DELETE "/mcp-servers/$PROBE_MS")"; [ "$code" = "200" ] || die 15 "DELETE probe-mcp -> $code"
code="$(api DELETE "/gateways/$PROBE_GW")"; [ "$code" = "200" ] || die 15 "DELETE probe-gw -> $code"
pass 15 "422 namespace_not_allowed (mcp + gateway, $SPIKE_NS); home allow_unauthenticated PATCH -> 409 env_managed; 시험용 draft 복제본 둘은 draft 로 남아 삭제됨"

# ---------- 16. cert-manager: ClusterIssuer 가 있을 때만 ----------
if [ -n "$ISSUER" ]; then
  clone_fresh 16 /mcp-servers "$I1" calc-cm
  ICM="$(jq_ 'd["id"]')"
  code="$(api POST "/mcp-servers/$ICM/deploy" "{\"exposure\": {\"enabled\": true, \"host\": \"calc-cm.e2e.local\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"cert-manager\", \"issuer\": \"$ISSUER\"}}}")"
  [ "$code" = "201" ] || die 16 "POST /mcp-servers/$ICM/deploy calc-cm -> $code"
  NCM="$(jq_ 'd["k8s"]["name"]')"
  [ "$(jq_ 'd["status"]["external_url"]')" = "https://calc-cm.e2e.local/mcp" ] || die 16 "external_url = $(jq_ 'd["status"]["external_url"]')"
  wait_ready "/mcp-servers/$ICM" 16
  wait_exposed "/mcp-servers/$ICM" 16 1
  ready=""
  for ((i = 0; i < 24; i++)); do  # cert-manager 가 Certificate 를 발급해 <k8s.name>-tls Secret 을 만들 때까지
    api GET "/mcp-servers/$ICM" >/dev/null
    [ "$(jq_ '(d["status"].get("exposure") or {}).get("certificate_ready")')" = "True" ] && { ready=1; break; }; sleep 5
  done
  [ -n "$ready" ] || die 16 "certificate_ready never true: $(jq_ 'd["status"]["exposure"]')"
  "${K[@]}" -n "$NS" get ingress "$NCM" -o jsonpath='{.metadata.annotations.cert-manager\.io/cluster-issuer} {.spec.tls[0].secretName}' >"$WORK/body"
  [ "$(cat "$WORK/body")" = "$ISSUER $NCM-tls" ] || die 16 "cert-manager Ingress: $(cat "$WORK/body")"
  code="$(mcp_ingress "https://calc-cm.e2e.local/mcp" "calc-cm.e2e.local:443:$LB_IP" tools/list)"
  [ "$code" = "200" ] || die 16 "tools/list via cert-manager https -> $code"
  api DELETE "/mcp-servers/$ICM" >/dev/null
  pass 16 "cert-manager ClusterIssuer $ISSUER: Certificate Ready, Ingress tls=$NCM-tls, tools/list 200"
else
  # 복제는 클러스터를 보지 않아 201 이고, 노출을 실은 배포가 그 자리에서 422 다
  clone_fresh 16 /mcp-servers "$I1" calc-cm
  ICM="$(jq_ 'd["id"]')"
  code="$(api POST "/mcp-servers/$ICM/deploy" "{\"exposure\": {\"enabled\": true, \"host\": \"calc-cm.e2e.local\", \"ingress_class\": \"$INGRESS_CLASS\", \"tls\": {\"mode\": \"cert-manager\", \"issuer\": \"none\"}}}")"
  [ "$code" = "422" ] || die 16 "cert-manager without ClusterIssuer CRD -> $code (expected 422)"
  [ "$(jq_ "d['detail']['code']")" = "cert_manager_unavailable" ] || die 16 "code = $(jq_ "d['detail']['code']")"
  api DELETE "/mcp-servers/$ICM" >/dev/null
  echo "STEP 16 SKIP: no cert-manager ClusterIssuer on this cluster (422 cert_manager_unavailable confirmed)"
fi

# ---------- 16b. 메트릭: 거친 곳마다 한 번 세고, 노출 앞단은 세지 않는다 (P51 1절·6절) ----------
# 호출은 감사 기록을 남긴 곳마다 센다. calculator 는 STEP 7 의 A Add 와 STEP 9 의 C Add(A 를 거친다)를 받았다. A 는 그
# 둘과 STEP 7 의 Subtract 를, B 는 STEP 7 의 Add 를 받았다. 없는 Tool 이라 백엔드를 정하지 못한 application_error 다. C 는
# STEP 9 의 Add 를 받았다. 이 넷은 다른 실행이나 화면이 함께 부를 수 있어 적어도 그만큼 늘었는지 본다.
# calc-exposed 는 STEP 14 에서 vMCP 앞단 front-<k8s.name> 을 거친 Add 를 한 번 받았다. 앞단과 MCP Server 프록시가 감사
# 기록을 하나씩 남긴다. 수집기는 앞단 파드를 id 라벨로 그 서버에 매기고(P54 11절) 앞단의 기록은 vMCP 의 것이라 부른
# 백엔드(Entry front-<k8s.name>)를 싣는다. 그래도 메트릭은 정확히 한 번이어야 한다. 감사 기록에서 앞단의 것도 찾는다.
# 앞단이 아예 기록하지 않아 한 번인 경우와 가리기 위해서다.
# 수집기가 60초마다 로그를 읽고 롤업이 120초마다 시간 묶음을 다시 세므로 METRICS_TIMEOUT 초까지 10초마다 본다
read_16b() { # 다섯 문서의 메트릭과 STEP 14 뒤의 Add 감사 기록을 다시 읽는다. 플랫폼이 다시 뜨는 중이면 1 이다
  metrics_to 16b "/mcp-servers/$I1" "$WORK/m1-calc" || return 1
  metrics_to 16b "/gateways/$A" "$WORK/m1-a" || return 1
  metrics_to 16b "/gateways/$B" "$WORK/m1-b" || return 1
  metrics_to 16b "/gateways/$C" "$WORK/m1-c" || return 1
  metrics_to 16b "/mcp-servers/$IX" "$WORK/m1-x" || return 1
  # header 모드의 platform-admin 이라 테넌트를 주지 않으면 모든 테넌트의 기록이다. 앞단의 기록이 테넌트 없이 남았어도
  # 보인다. 답은 새것부터 1000 건까지이고, 받은 쪽에서도 tool 을 다시 거른다
  answered "$(API_OUT="$WORK/audit-x" api GET "/audit/mcp?type=mcp_tool_call&tool=Add&since=$X_SINCE&limit=1000")" \
    16b "GET /audit/mcp" "$WORK/audit-x"
}
metrics_grown() { # -> 0 은 모두 늘었다, 3 은 아직이다, 2 는 calc-exposed 가 한 번을 넘었다. 늘어난 수를 한 줄로 찍는다.
                  # 파이썬 오류는 1 이라 아직인 것과 가린다
  python3 - "$WORK" "$IX" "$NX" <<'PY'
import json, sys
from datetime import datetime
work, ix, nx = sys.argv[1:4]

def calls_grown(old, new):
    # 24시간 창은 지금 시간의 23시간 전부터라 기준값을 읽은 뒤 시가 바뀌면 가장 오래된 시간이 창에서 빠진다. 실행마다
    # 다시 쓰는 calculator 는 그 시간에 하루 전 실행의 호출이 있을 수 있으므로 두 답이 함께 가진 시간만 견준다
    first = datetime.fromisoformat(new["series"][0]["start"])
    def total(d):
        return sum(b["calls"] for b in d["series"] if datetime.fromisoformat(b["start"]) >= first)
    return total(new) - total(old)

def add_calls(d):
    return sum(r["calls"] for r in d["by_tool"] if r["tool"] == "Add")

def no_backend(d):
    # 백엔드를 정하지 못한 호출의 행이다. MCP Server 의 답에는 by_backend 가 없다
    return sum(r["outcomes"]["application_error"] for r in d.get("by_backend", []) if r["entry"] is None)

g = {}
for n in ("calc", "a", "b", "c", "x"):
    old, new = (json.load(open(f"{work}/m{stage}-{n}")) for stage in (0, 1))
    # Tool 별·백엔드별 행은 시간으로 나뉘지 않아 창 전체로 견준다. 이 실행이 만든 A·B·C·calc-exposed 에는 창에서
    # 빠질 만큼 오래된 호출이 없다
    g[n] = {"calls": calls_grown(old, new), "add": add_calls(new) - add_calls(old),
            "nob": no_backend(new) - no_backend(old)}
adds = [e for e in json.load(open(f"{work}/audit-x")) if e.get("tool") == "Add"]
front = [e for e in adds if e.get("proxy_kind") == "virtualmcpserver" and (e.get("pod") or "").startswith(f"front-{nx}-")]
server = [e for e in adds if e.get("instance_id") == ix and e.get("proxy_kind") == "mcpserver"]
print(f"  calculator +calls {g['calc']['calls']}; A +Add {g['a']['add']} +no-backend {g['a']['nob']}; "
      f"B +no-backend {g['b']['nob']}; C +Add {g['c']['add']}; calc-exposed +calls {g['x']['calls']} +Add {g['x']['add']}; "
      f"audit Add front {len(front)} (instance_id {[e.get('instance_id') for e in front]}, backend {[e.get('backend') for e in front]}) "
      f"server {len(server)}")
if g["x"]["calls"] > 1 or g["x"]["add"] > 1:
    sys.exit(2)
# calculator 는 이 실행에서 Add 만 받으므로 Tool 호출 수로 본다
ok = (g["calc"]["calls"] >= 2 and g["a"]["add"] >= 2 and g["a"]["nob"] >= 1 and g["b"]["nob"] >= 1
      and g["c"]["add"] >= 1 and g["x"]["calls"] == 1 and g["x"]["add"] == 1
      and len(front) == 1 and front[0].get("instance_id") == ix and front[0].get("backend") == f"front-{nx}"
      and len(server) == 1)
sys.exit(0 if ok else 3)
PY
}
# 다 늘 때까지(GROWN 이 3 이 아닐 때까지) METRICS_TIMEOUT 초 동안 본다. 한 번도 읽지 못하면 GROWN 은 4 다. 앞단의 기록이
# 늦게 읽혀 다음 롤업에서 두 번이 되지 않는 것은 롤업 테스트가 지킨다(platform/tests/test_mcp_metrics_rollup.py 의
# test_an_event_inserted_after_the_aggregation_is_counted_next_tick)
GROWN=4; LINE=""
for ((i = 0; i < METRICS_TIMEOUT / 10; i++)); do
  if read_16b; then
    GROWN=0; LINE="$(metrics_grown)" || GROWN=$?
    [ "$GROWN" = 3 ] || break
  fi
  sleep 10
done
[ -z "$LINE" ] || echo "$LINE"
case "$GROWN" in
  0) ;;
  2) die 16b "calc-exposed counted its one Add through the exposure front more than once" "$(cat "$WORK/m1-x")" ;;
  3) die 16b "metrics did not grow as expected within ${METRICS_TIMEOUT}s" "$(cat "$WORK/m1-x")" ;;
  4) die 16b "the platform did not answer the metrics reads within ${METRICS_TIMEOUT}s" ;;
  *) die 16b "reading the metrics failed (exit $GROWN)" "$(cat "$WORK/m1-x")" ;;
esac
pass 16b "calculator·A·B·C 가 STEP 7·9 의 호출만큼 늘었다(A·B 의 없는 Tool 은 백엔드 없는 application_error); calc-exposed 는 vMCP 앞단(backend front-$NX)과 MCP Server 에 감사 기록이 하나씩 있어도 Add 를 한 번 셌다"

# ---------- 17. delete the exposed documents -> Ingress, NetworkPolicy, vMCP front gone ----------
code="$(api DELETE "/mcp-servers/$IX")"; [ "$code" = "200" ] || die 17 "DELETE mcp server calc-exposed $IX -> $code"
code="$(api DELETE "/gateways/$IAX")"; [ "$code" = "200" ] || die 17 "DELETE gateway a-exposed $IAX -> $code"
gone=""
for ((i = 0; i < 24; i++)); do
  left="$("${K[@]}" -n "$NS" get "ingress/$NX" "ingress/$NAX" "networkpolicy/daegam-expose-$NX" "networkpolicy/daegam-expose-$NAX" \
    "virtualmcpserver/$FX" "mcpgroup/$FX" "mcpserverentry/$FX" "mcpexternalauthconfig/$FX" "mcpauthzconfig/$FX-authz" \
    "mcpserver/$NX" "virtualmcpserver/$NAX" -o name --ignore-not-found 2>/dev/null | wc -l | tr -d ' ')"
  [ "$left" = "0" ] && { gone=1; break; }; sleep 5
done
[ -n "$gone" ] || die 17 "exposure objects still present: $("${K[@]}" -n "$NS" get ingress,networkpolicy,virtualmcpservers,mcpserverentries -o name)"
# Ingress 가 지워진 뒤 Traefik 이 라우트를 내리기까지 몇 초 걸린다 (실측: 직후 406, 곧 404)
traefik_gone() { # URL RESOLVE -> 404 가 될 때까지 60초 폴링
  local code="" i
  for ((i = 0; i < 12; i++)); do
    code="$(curl -sSk -o /dev/null -w '%{http_code}' --resolve "$2" --max-time 10 "$1" || echo 000)"
    [ "$code" = "404" ] && return 0; sleep 5
  done
  die 17 "Traefik still routes $1 -> $code"
}
traefik_gone "https://$MCP_HOST/mcp" "$MCP_HOST:443:$LB_IP"
traefik_gone "http://$GW_HOST/mcp" "$GW_HOST:80:$LB_IP"
"${K[@]}" -n "$NS" get networkpolicy daegam-tenant-ingress -o name >/dev/null || die 17 "tenant policy daegam-tenant-ingress was removed"
"${K[@]}" -n "$NS" delete secret "$TLS_SECRET" --ignore-not-found >/dev/null
pass 17 "deleting calc-exposed and a-exposed removed Ingress, NetworkPolicy daegam-expose-*, the vMCP front $FX with its MCPGroup, MCPServerEntry and auth configs; Traefik answers 404; tenant policy intact"

# ---------- 8c. code mode (P56 6.2·6.3절). STEP 17 뒤, 18 앞에 돈다 ----------
# 새 단계가 실패해도 앞 STEP 의 결과를 가리지 않게 노출 STEP 뒤에 돈다. 쓰기 Tool 로 쓰는 orders 게시 항목은 STEP 18 이
# 지우므로 그 앞이다. calculator WSDL 의 오퍼레이션은 표시가 없으면 쓰기라, 서버 주인이 Add·Divide 를 읽기로 표시한 MCP
# Server calc-read 를 따로 띄워 게시한다. calculator 는 A·B 가 쓰는 그대로 두고, 표시를 바꿔 굴리는 것은 이 서버다.
# code mode 를 켠 Gateway code 의 vMCP 는 execute_tool_script 를 함께 내놓는다(header 모드라 토큰 없이 부른다). 정의 검사는
# 저장 때 쓰기 Tool 을, 재배포(노출을 켜는 PATCH)에서 노출을 거절한다. 서버의 새 리비전이 Divide 를 쓰기로 표시하면 다음
# 재적용이 판정을 다시 해 code mode 를 빼고 code_mode_closed 를 알린다. 그 vMCP 가 적용된 뒤의 렌더가 힌트 forbid 를 빼면
# 읽기 표시가 없는 Divide 도 바로 불린다
save_doc 8c /mcp-servers "$CODE_MS_NAME" "$(code_read_body "$CODE_MS_NAME" "$BP1" 1)"
CODE_MS="$SAVED_ID"
deploy_doc 8c /mcp-servers "$CODE_MS"
wait_ready "/mcp-servers/$CODE_MS" 8c
code="$(api POST "/mcp-servers/$CODE_MS/publish" '{}')"
case "$code" in 200 | 201) ;; *) die 8c "publish $CODE_MS_NAME -> $code: $(cat "$WORK/body")" ;; esac
HCODE="$(jq_ 'd["id"]')"
save_doc 8c /gateways "$CODE_GW_NAME" "{\"name\": \"$CODE_GW_NAME\", \"backends\": [{\"hub_item_id\": \"$HCODE\"}], \"code_mode\": true}"
CODE_GW="$SAVED_ID"
deploy_doc 8c /gateways "$CODE_GW"
wait_ready "/gateways/$CODE_GW" 8c
api GET "/gateways/$CODE_GW" >/dev/null; NCODE="$(jq_ 'd["k8s"]["name"]')"
PCODE=$((PF_PORT_BASE + 5)); pf "vmcp-$NCODE" "$PCODE" 8c
UCODE="http://127.0.0.1:$PCODE/mcp"
# 폴링이 부르는 MCP 호출은 하나에 10초까지만 기다린다. 기본 30초를 기다리면 마감을 지난 폴링이 그만큼 더 끈다
code_mcp() { $MCP --timeout 10 "$@"; }
# 새 vMCP 가 백엔드를 아직 못 보았을 수 있어 60초까지 본다
want="['Add', 'Divide', '$SCRIPT_TOOL']"; listed=""; deadline=$((SECONDS + 60))
while :; do
  listed="$(tools_sorted "$(code_mcp list "$UCODE" 2>/dev/null || true)" 2>/dev/null || true)"
  [ "$listed" != "$want" ] && [ "$SECONDS" -lt "$deadline" ] || break
  sleep 5
done
[ "$listed" = "$want" ] || die 8c "Gateway $CODE_GW_NAME with code mode lists $listed (want $want)"
# 스크립트는 Tool 을 키워드 인자로 부른다. 두 결과(Add 11, Divide 4)가 답 어딘가에 있어야 한다. 결과를 감싸는 꼴(구조화된
# 내용인지 글인지)은 ToolHive 가 정하므로 숫자만 찾는다
CS="$($MCP call "$UCODE" "$SCRIPT_TOOL" "$(script_args 'return {"sum": Add(a=5, b=6), "quotient": Divide(numerator=8, denominator=2)}')")" \
  || die 8c "the script through $CODE_GW_NAME failed: $CS"
python3 - "$CS" <<'PY' || die 8c "the script did not return both results (Add 11, Divide 4): $CS"
import json, sys

def numbers(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from numbers(x)
    elif isinstance(v, list):
        for x in v:
            yield from numbers(x)
    elif isinstance(v, str):
        try:
            yield from numbers(json.loads(v))
        except ValueError:
            pass
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        yield v

call = json.loads(sys.argv[1])["call"]
found = list(numbers([call.get("structuredContent"), call.get("content")]))
sys.exit(0 if not call["isError"] and 11 in found and 4 in found else 1)
PY
# 쓰기 Tool 을 더한 저장은 리비전을 쌓지 않는다. code_mode 를 보내지 않으면 저장된 값(켬)을 둔다
code="$(api PUT "/gateways/$CODE_GW" "{\"name\": \"$CODE_GW_NAME\", \"backends\": [{\"hub_item_id\": \"$HCODE\"}, {\"hub_item_id\": \"$HORD\", \"tools\": [\"CreateOrder\"]}]}")"
[ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = code_mode_not_read_only ] \
  || die 8c "adding orders' CreateOrder to $CODE_GW_NAME -> $code $(error_code "$WORK/body") (want 422 code_mode_not_read_only)"
[ "$(jq_ "d['detail'].get('names')")" = "{'CreateOrder': 'write'}" ] \
  || die 8c "the refusal names $(jq_ "d['detail'].get('names')") (want {'CreateOrder': 'write'})"
code="$(api PATCH "/gateways/$CODE_GW" "{\"exposure\": $(exp_gw "$CODE_HOST")}")"
[ "$code" = 422 ] && [ "$(error_code "$WORK/body")" = code_mode_exposed ] \
  || die 8c "exposing $CODE_GW_NAME -> $code $(error_code "$WORK/body") (want 422 code_mode_exposed)"
# 서버의 새 리비전이 Divide 를 쓰기로 표시한다. 재배포하면 서버가 Divide 에 readOnlyHint 를 false 로 알린다
code="$(api PUT "/mcp-servers/$CODE_MS" "$(code_read_body "$CODE_MS_NAME" "$BP1" 1 false)")"
[ "$code" = 200 ] || die 8c "PUT /mcp-servers/$CODE_MS marking Divide write -> $code: $(cat "$WORK/body")"
code="$(api POST "/mcp-servers/$CODE_MS/redeploy")"; [ "$code" = 200 ] || die 8c "redeploy $CODE_MS_NAME -> $code"
closed=""; state=""; deadline=$((SECONDS + CODE_CLOSE_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
  warned=""; rendered=""; listed=""; divided=""
  [ "$(api GET "/gateways/$CODE_GW")" != 200 ] \
    || warned="$(jq_ 'json.dumps([w for w in (d.get("status") or {}).get("warnings", []) if w.startswith("code_mode")])' || true)"
  rendered="$("${K[@]}" -n "$NS" get virtualmcpserver "$NCODE" -o jsonpath='{.spec.config.codeMode.enabled}' 2>/dev/null || true)"
  listed="$(tools_sorted "$(code_mcp list "$UCODE" 2>/dev/null || true)" 2>/dev/null || true)"
  divided="$(code_mcp call "$UCODE" Divide '{"numerator": 8, "denominator": 2}' 2>/dev/null || true)"
  state="warnings=$warned codeMode=${rendered:-absent} tools=$listed Divide=$divided"
  case "$warned" in *"code_mode_closed:"*"(cause: tools; tools: Divide)"*) ;; *) sleep 10; continue ;; esac
  [ -z "$rendered" ] && [ "$listed" = "['Add', 'Divide']" ] && call_result_is 4 "$divided" 2>/dev/null \
    && { closed=1; break; }
  sleep 10
done
[ -n "$closed" ] || die 8c "code mode did not close within ${CODE_CLOSE_TIMEOUT}s after Divide was marked write: $state"
stop_pf "$PF_PID" "$PCODE"
# api_delete 는 끝내 못 지워도 멈추지 않으므로 사라졌는지 본다. 서버는 Gateway 가 사라진 뒤에 지울 수 있고, 그 삭제가
# 게시 항목을 함께 지운다
api_delete "/gateways/$CODE_GW"
wait_gone "/gateways/$CODE_GW" || die 8c "gateway $CODE_GW_NAME ($CODE_GW) is still there after its delete"
api_delete "/mcp-servers/$CODE_MS"
wait_gone "/mcp-servers/$CODE_MS" || die 8c "mcp server $CODE_MS_NAME ($CODE_MS) is still there after its delete"
pass 8c "Gateway $CODE_GW_NAME over $CODE_MS_NAME (Add, Divide marked read) with code mode lists $SCRIPT_TOOL, and one script returned Add 11 and Divide 4; adding orders' CreateOrder -> 422 code_mode_not_read_only {CreateOrder: write}; exposing it -> 422 code_mode_exposed; after a server revision marked Divide write the reapply turned code mode off with code_mode_closed (cause tools, Divide), and Divide answered a direct call; gateway and server removed"

# ---------- 18. cleanup ----------
for i in "${!PFS[@]}"; do stop_pf "${PFS[$i]}" "${PFS_PORTS[$i]}"; done; PFS=(); PFS_PORTS=()
api DELETE "/gateways/$C" >/dev/null
api DELETE "/hub/$PUB" >/dev/null
# 게시된 MCP Server 는 Gateway A·B 가 붙들고 있는 동안 지울 수 없다 (409 in_use, D42)
code="$(api DELETE "/mcp-servers/$I1")"
[ "$code" = "409" ] && [ "$(jq_ "d['detail']['code']")" = "in_use" ] || die 18 "DELETE calculator while A, B reference it -> $code (expected 409 in_use): $(cat "$WORK/body")"
[ "$(jq_ "sorted(d['detail']['gateway_ids'])")" = "$WANT_REFS" ] || die 18 "in_use gateway_ids: $(jq_ "d['detail']['gateway_ids']")"
for id in "$A" "$B"; do code="$(api DELETE "/gateways/$id")"; [ "$code" = "200" ] || die 18 "DELETE gateway $id -> $code"; done
api DELETE "/hub/$SPIKE" >/dev/null
# Gateway 가 사라지면 지울 수 있고, 그 삭제가 게시 항목을 함께 지운다 (docs/guide/hub-gateway.md 6절)
for id in "$I1" "$I2"; do
  code="$(api DELETE "/mcp-servers/$id")"; [ "$code" = "200" ] || die 18 "DELETE mcp server $id -> $code"
  # 남이 클레임을 쥐고 있었으면 deleted 가 거짓이고 홀더가 마친다 (P66 2.6절)
  [ "$(jq_ 'd["deleted"]')" = "True" ] || wait_gone "/mcp-servers/$id" || die 18 "deferred delete of $id did not finish"
done
api GET /hub >/dev/null
[ "$(jq_ "[i['id'] for i in d if i['id'] in ('$HCALC', '$HORD')]")" = "[]" ] || die 18 "published items survived the mcp server deletion: $(jq_ "[i['name'] for i in d]")"
gone=""
for ((i = 0; i < 24; i++)); do
  left="$("${K[@]}" -n "$NS" get virtualmcpservers,mcpremoteproxies,mcpservers,mcpserverentries,ingresses -o name 2>/dev/null | wc -l | tr -d ' ')"
  ggroups="$("${K[@]}" -n "$NS" get mcpgroups -o name 2>/dev/null | grep -c '/gw-' || true)"; left=$((left + ggroups))
  [ "$left" = "0" ] && { gone=1; break; }; sleep 5
done
[ -n "$gone" ] || die 18 "CRs still present in $NS"
pass 18 "vMCPs, gateway groups/entries, published items, the entry item, ingresses and MCP servers deleted; A·B 가 붙들고 있는 동안 calculator 삭제는 409 in_use 였고, Gateway 를 지운 뒤의 삭제가 게시 항목을 함께 지웠다"

# ---------- 9i. 허브 이미지의 허용 (P55 6절, F8) ----------
# scripts/hub_e2e_registry.sh 가 띄운 TLS zot(k3d-hub)과 평문 upstream 이 있어야 돈다(E2E_HUB_DIR). 다른 STEP 이 모두 끝난 뒤에 돈다.
# 새 단계의 실패가 뒤 STEP 을 가리지 않게 하기 위해서다. 카탈로그 모양의 stdio lab 서버 env-echo(deploy/dev/lab/images/env-echo)를
# upstream 에 올리고, 출처가 없으니 승인(HUB_APPROVE)으로 허브 접두사 아래에 미러해 시험 키로 서명한다. 서명하지 않고 옮기기만
# 한 사본(header-echo, hub_mirror.py copy)은 422 image_not_admitted 다. 승인된 사본은 Env 와 Secret env 를 든 stdio 항목이
# 되고(admission provenance approved), Gateway g9i 가 그 Tool 을 내놓고 두 값을 돌려주며, Secret 값을 바꾸면 백엔드가 다시 떠
# 새 값이 컨테이너에 전달된다. Secret env 가 있는 항목은 다른 테넌트에 열 수 없다(422 secret_not_shareable). digest 를 거부하면
# 같은 이미지의 새 항목이 422 image_refused, g9i 의 재배포가 422 hub_backend_refused 이고 항목과 g9i 의 상태에 경고가 붙는다.
# 설정(network.ca_bundle, hub_images)은 처음 값을 두고 끝이나 cleanup 이 되돌린다. 읽기 자격 증명이 저장된 설치에서는 돌지 않는다.
# 접두사의 호스트를 바꾸면 서버가 그 자격 증명을 지우고, 되돌리는 PATCH 로는 다시 넣을 수 없기 때문이다
step_9i() {
  local hub_reg up_reg hub_ca secret_v1 secret_v2 old n code img unsigned item hid digest g9 ng9 pg9 ug9 ev seen i l9 body
  hub_reg="k3d-hub:${E2E_HUB_PORT:-$PORT_HUB_REGISTRY}"; up_reg="127.0.0.1:${E2E_HUB_UPSTREAM_PORT:-$PORT_HUB_UPSTREAM}"
  hub_ca="$E2E_HUB_DIR/ca.crt"; secret_v1="e2e-p55-one-$$"; secret_v2="e2e-p55-two-$$"
  command -v cosign >/dev/null || die 9i "cosign is not installed"
  code="$(api GET /settings)"; [ "$code" = "200" ] || die 9i "GET /settings -> $code"
  if [ "$(jq_ 'd["hub_images"]["credential_set"]')" = True ]; then
    skip 9i "a hub image read credential is stored on this install; changing the prefix would clear it, so the step runs only where none is stored"
    return 0
  fi
  HUB_SETTINGS_ORIG="$(jq_ 'json.dumps({"network": d["network"], "hub_images": {k: d["hub_images"][k] for k in ("prefix", "public_keys", "revoked_keys", "refused_digests")}})')"
  # 설정. CA 는 있던 묶음 뒤에 더한다
  body="$(python3 - "$WORK/body" "$hub_ca" "$hub_reg/hub" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ca = open(sys.argv[2]).read()
bundle = ((d["network"].get("ca_bundle") or "").rstrip() + "\n" + ca).lstrip()
print(json.dumps({"network": {"ca_bundle": bundle}, "hub_prefix": sys.argv[3]}))
PY
)"
  # 이전 실행이 남긴 g9i 와 항목을 지운다. 지워질 때까지 기다려야 이름이 비어 있다
  api GET /gateways >/dev/null; old="$(find_id g9i)"
  if [ -n "$old" ]; then api_delete "/gateways/$old" >/dev/null; wait_gone "/gateways/$old" || die 9i "gateway g9i of an earlier run is not gone"; fi
  for n in e2e-hub-env e2e-hub-again e2e-hub-unsigned; do
    api GET /hub >/dev/null; old="$(find_id "$n")"
    if [ -n "$old" ]; then api_delete "/hub/$old" >/dev/null; wait_gone "/hub/$old" || die 9i "hub item $n of an earlier run is not gone"; fi
  done
  docker tag daegam-env-echo:dev "$up_reg/e2e/env-echo:1" && docker push -q "$up_reg/e2e/env-echo:1" >/dev/null ||
    die 9i "pushing env-echo to $up_reg failed"
  docker tag daegam-header-echo:dev "$up_reg/e2e/unsigned:1" && docker push -q "$up_reg/e2e/unsigned:1" >/dev/null ||
    die 9i "pushing the unsigned image to $up_reg failed"
  # 시험 키. 비밀번호가 비어 있다 (F0 6번)
  (cd "$WORK" && COSIGN_PASSWORD="" cosign generate-key-pair --output-key-prefix hub >/dev/null 2>&1) || die 9i "cosign generate-key-pair failed"
  body="$(python3 - "$body" "$WORK/hub.pub" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
hub = {"prefix": d.pop("hub_prefix"), "public_keys": [open(sys.argv[2]).read()], "revoked_keys": [], "refused_digests": []}
print(json.dumps({**d, "hub_images": hub}))
PY
)"
  code="$(api PATCH /settings "$body")"; [ "$code" = "200" ] || die 9i "PATCH hub_images -> $code: $(cat "$WORK/body")"
  # 미러는 러너에서 돈다. 허브 레지스트리의 CA 를 파이썬(DAEGAM_CA_FILE)과 cosign(SSL_CERT_FILE)이 믿는다
  img="$(HUB_PREFIX="$hub_reg/hub" HUB_KEY="$WORK/hub.key" COSIGN_PASSWORD="" HUB_UPSTREAM="$up_reg/e2e/env-echo:1" HUB_APPROVE=yes \
    DAEGAM_CA_FILE="$hub_ca" SSL_CERT_FILE="$hub_ca" "$PY" -I "$ROOT/scripts/hub_mirror.py" mirror)" || die 9i "hub-mirror of env-echo failed"
  unsigned="$(HUB_PREFIX="$hub_reg/hub" HUB_UPSTREAM="$up_reg/e2e/unsigned:1" DAEGAM_CA_FILE="$hub_ca" \
    "$PY" -I "$ROOT/scripts/hub_mirror.py" copy)" || die 9i "copying the unsigned image failed"
  code="$(api POST /hub "{\"name\": \"e2e-hub-unsigned\", \"kind\": \"image\", \"image\": \"$unsigned\", \"port\": 18082}")"
  [ "$code" = "422" ] && [ "$(error_code "$WORK/body")" = image_not_admitted ] ||
    die 9i "an image without a hub signature -> $code (expected 422 image_not_admitted)"
  # Secret 값은 curl 의 인자가 아니라 표준 입력으로 보낸다
  item="{\"name\": \"e2e-hub-env\", \"kind\": \"image\", \"image\": \"$img\", \"stdio\": true, \"env\": [{\"name\": \"E2E_PLAIN\", \"value\": \"plain-$$\"}], \"secret_env\": [{\"name\": \"E2E_SECRET\", \"value\": \"$secret_v1\"}]}"
  code="$(api POST /hub @- <<<"$item")"; [ "$code" = "201" ] || die 9i "POST /hub env-echo -> $code"
  ! grep -q "$secret_v1" "$WORK/body" || die 9i "the answer carries the Secret value"
  [ "$(jq_ 'd["source"]["admission"]["provenance"]')" = approved ] || die 9i "admission is not approved: $(jq_ 'd["source"].get("admission")')"
  hid="$(jq_ 'd["id"]')"; digest="$(jq_ 'd["source"]["admission"]["digest"]')"
  wait_ready "/hub/$hid" 9i
  code="$(api POST "/hub/$hid/refresh-tools")"; [ "$code" = "200" ] || die 9i "refresh-tools -> $code"
  g9="$(mkgw g9i "[{\"hub_item_id\": \"$hid\"}]" "" 9i)"
  deploy_doc 9i /gateways "$g9"
  wait_ready "/gateways/$g9" 9i
  api GET "/gateways/$g9" >/dev/null; ng9="$(jq_ 'd["k8s"]["name"]')"
  pg9=$((PF_PORT_BASE + 4)); pf "vmcp-$ng9" "$pg9" 9i
  ug9="http://127.0.0.1:$pg9/mcp"
  l9="$($MCP list "$ug9")" || die 9i "tools/list through g9i failed: $l9"
  [ "$(tools_sorted "$l9")" = "['echo_env']" ] || die 9i "g9i does not list exactly echo_env: $l9"
  echo_env_value() { $MCP call "$ug9" echo_env "{\"name\": \"$1\"}" 2>/dev/null || true; }
  ev="$(echo_env_value E2E_SECRET)"; case "$ev" in *"$secret_v1"*) ;; *) die 9i "echo_env did not see the Secret value: $ev" ;; esac
  ev="$(echo_env_value E2E_PLAIN)"; case "$ev" in *"plain-$$"*) ;; *) die 9i "echo_env did not see the Env value: $ev" ;; esac
  code="$(api PUT "/hub/$hid/secret-env" @- <<<"{\"values\": {\"E2E_SECRET\": \"$secret_v2\"}}")"
  [ "$code" = "200" ] || die 9i "PUT secret-env -> $code"
  ! grep -q "$secret_v2" "$WORK/body" || die 9i "the answer carries the replaced Secret value"
  # 판이 오른 어노테이션이 백엔드를 다시 띄우고, stdio 점검이 프록시를 다시 붙인다
  seen=""
  for ((i = 0; i < 30; i++)); do
    ev="$(echo_env_value E2E_SECRET)"; case "$ev" in *"$secret_v2"*) seen=1; break ;; esac; sleep 10
  done
  [ -n "$seen" ] || die 9i "the replaced Secret value never reached the container: $ev"
  code="$(api PUT "/hub/$hid/access" "{\"view\": {\"tenants\": [\"$TENANT_B\"]}}")"
  [ "$code" = "422" ] && [ "$(error_code "$WORK/body")" = secret_not_shareable ] ||
    die 9i "opening a Secret env item to $TENANT_B -> $code (expected 422 secret_not_shareable)"
  code="$(api PATCH /settings "{\"hub_images\": {\"refused_digests\": [\"$digest\"]}}")"; [ "$code" = "200" ] || die 9i "refusing $digest -> $code"
  code="$(api POST /hub "{\"name\": \"e2e-hub-again\", \"kind\": \"image\", \"image\": \"$img\", \"stdio\": true, \"secret_env\": [{\"name\": \"E2E_SECRET\"}]}")"
  [ "$code" = "422" ] && [ "$(error_code "$WORK/body")" = image_refused ] || die 9i "a new item from a refused digest -> $code (expected 422 image_refused)"
  api GET "/hub/$hid" >/dev/null
  [ "$(jq_ 'any(w.startswith("hub_image_refused") for w in d["status"]["warnings"])')" = True ] || die 9i "no hub_image_refused on the item: $(jq_ 'd["status"]["warnings"]')"
  code="$(api POST "/gateways/$g9/redeploy")"
  [ "$code" = "422" ] && [ "$(error_code "$WORK/body")" = hub_backend_refused ] || die 9i "redeploying g9i with a refused backend -> $code (expected 422 hub_backend_refused)"
  api GET "/gateways/$g9" >/dev/null
  [ "$(jq_ 'any(w.startswith("hub_backend_refused") for w in d["status"]["warnings"])')" = True ] || die 9i "no hub_backend_refused on g9i: $(jq_ 'd["status"]["warnings"]')"
  api_delete "/gateways/$g9" >/dev/null
  api_delete "/hub/$hid" >/dev/null
  restore_hub_settings
  pass 9i "hub images: an unsigned copy refused, the approved env-echo admitted with Env and Secret env, listed and read through Gateway g9i, the replaced Secret value passed to the container after the backend rolled; sharing refused; a refused digest refused a new item and g9i's redeploy and warned both"
}
if [ -n "${E2E_HUB_DIR:-}" ]; then
  step_9i
elif [ -n "${E2E_HUB_EXPECTED:-}" ]; then
  # 후보의 k3d 잡이 레지스트리를 띄우려다 못 했다. 이 STEP 만 실패로 센다
  record_fail 9i "the hub registry did not come up (E2E_HUB_EXPECTED is set but E2E_HUB_DIR is not; see the job's hub registry step)"
else
  skip 9i "no hub registry (E2E_HUB_DIR unset; scripts/hub_e2e_registry.sh sets it up)"
fi
finish
