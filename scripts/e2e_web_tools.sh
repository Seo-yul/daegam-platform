#!/usr/bin/env bash
# P39b (D64) platform web tools e2e against a real cluster.
#
#   make e2e-web-tools
#       orbstack: assumes `make deploy-local` (platform in-cluster, ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true, header
#       mode) and `make lab-up` (mock-soap and Postgres in daegam-spike). DAEGAM_CA_FILE (a TLS-inspecting proxy's CA, PEM)
#       goes into network.ca_bundle for this run when set
#   make e2e-web-tools E2E_TARGET=install E2E_INSTALL_USER=<admin>
#       the install (P85 10절): its public host, local mode, tenant e2e, NetworkPolicy enforced. The cluster refuses the stub provider, so the
#       echo server (lib/e2e_llm_echo.sh) answers by the same stub rules; there is no lab, so the petstore spec's addPet
#       stands in for the calculator's Add, and the memory node uses the platform namespace's Postgres. The vMCP takes
#       the data-plane token of a fresh viewer. DAEGAM_CA_FILE is not used. Never run it together with e2e-telemetry or
#       e2e-skills
#
# The steps that need the internet are SKIP (conditional) when the fetch clone cannot reach https://example.com/.
# "The data tool" below is the calculator's Add on orbstack and petstore's addPet on the install.
#
# Gate (12 steps):
#   STEP 1   platform reachable (orbstack: port-forward, header mode; install: the public host, admin login), home cluster
#            preflight ok
#   STEP 2   web_fetch and web_search on (search provider stub, the CA when given) → the hub lists both platform items
#            Ready; the model provider (stub; on the install the echo) and the memory Database (lab Postgres; on the install
#            the platform's) registered
#   STEP 3   the data tool's MCP Server published; Gateway web-e2e (web-fetch, web-search, the data tool) deploys Ready
#            and the fetch clone is Ready with the label, annotation and no service account token on the MCP pod;
#            exposing it is 422 platform_tool_not_exposable and the exposure stays off
#   STEP 4   web_search through the vMCP returns the stub results; the endpoint without a token is 401; with the tenant
#            limit at 1 a minute a search is rate_limited; with search off it is search_disabled on the running Gateway.
#            Every search through Gateway web-e2e that answers ok is counted for STEP 5
#   STEP 5   both items narrowed to another tenant → the Gateway shows hub_access_revoked, search through it is
#            search_not_allowed and a new Gateway with the item is 422 hub_item_not_consumable; restoring undoes it.
#            The usage ledger (GET /usage, polled for 300 s) has one call per counted search for Gateway
#            web-e2e, none unpriced, and the model group has (web_search, stub, stub)
#   STEP 6   web_fetch refuses the metadata, private, cluster, mapped and NAT64 addresses (url_private and friends);
#            with internet https://example.com/ returns "Example Domain" and https://iana.org/ (a redirect to another
#            host) comes back as redirect_to
#   STEP 7   inside the fetch pod: DNS resolves and public 443 connects, while kubernetes.default.svc, 10.0.0.1 and
#            169.254.169.254 do not (each closed address prints why); the clone refuses a tenant-audience token (401)
#            and the data tool's proxy refuses the fetch-audience token (401)
#   STEP 8   Agent web-agent-e2e (web tools only, empty allow list, stub model): fetching a URL from the person's message
#            succeeds with tool_result.untrusted web; after an image attachment the same session's fetch is url_not_allowed,
#            also in the next turn without one; with example.com on the allow list it passes again
#   STEP 9   Agent web-sub-e2e (the data tool on the main agent, web tools on a subagent): fetch is url_not_allowed
#            (data tools). The image reports tool_safety (P43), so there is no default approval: in a new session the
#            data tool runs without asking (install: petstore.example.com does not answer, so the run may end in a tool
#            error); after an image attachment the same session's call stops at an approval card with reason outside.
#            That a canvas read-only mark changes nothing for a tool whose server states its effect is left to
#            platform/tests/test_agent_tool_safety.py test_canvas_marks_count_only_for_tools_whose_server_cannot_say
#   STEP 10  Agent web-pin-e2e on Gateway calc-web-e2e (install: pet-web-e2e; the data tool only): adding web-fetch to the
#            Gateway warns web_backend_pinned_agents; a deleted pod comes back without web_fetch and the status warns
#            tools_behind; removing the data tool from the Gateway warns tools_missing
#   STEP 11  fetch off removes the clone within a tick and a Gateway redeploy is 409 backends_not_ready; fetch on
#            restores the clone within a tick
#   cleanup  agents, Gateways, MCP Server, Database and provider this run created (install: also the petstore Backend API,
#            the viewer and the echo namespace); the settings sections and the items' access as they were
#
# 저장·배포의 422 와 409 규칙 하나하나는 플랫폼 테스트가 지킨다(platform/tests/test_platform_tools.py,
# test_tool_pins.py, test_agent_web.py, test_web_search.py). 이 게이트는 실제 vMCP·복제본·런타임을 지나는 길만 본다.
# Steps print `STEP n PASS|SKIP|FAIL: ...`. macOS has no `timeout`, so every wait is a polling loop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
# env_defaults 는 여기에 있다. install 갈래가 설치 값 파일을 읽는다
. "$HERE/lib/hosts.sh"
TARGET="${E2E_TARGET:-orbstack}"
API_PORT="${E2E_API_PORT:-$PORT_WEB_TOOLS_API}"
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_WEB_TOOLS_VMCP}"
FETCH_PORT="${E2E_FETCH_PORT:-$PORT_WEB_TOOLS_FETCH}"
CALC_PORT="${E2E_CALC_PORT:-$PORT_WEB_TOOLS_CALC}"
# 승인이 걸리는 데이터 Tool 이다. orbstack 은 lab 의 calculator(mock-soap)이고, lab 이 없는 설치본은 petstore 스펙의 addPet
# (POST 라 효과가 write 다)이다. 스펙의 서버 petstore.example.com 은 답하지 않는다
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-webtools}"
    OTHER_TENANT="${E2E_OTHER_TENANT:-webtools-other}"
    BASE="http://127.0.0.1:${API_PORT}"; PROVIDER="stub-e2e"; CA_FILE="${DAEGAM_CA_FILE:-}"
    BP_NAME="calc-web-e2e"; MS_NAME="calc-web-e2e"; GW_CALC="calc-web-e2e"
    DATA_TOOL="Add"; DATA_ARGS='{"a": 2, "b": 3}'; DATA_LABEL="calculator" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"; OTHER_TENANT="${E2E_OTHER_TENANT:-e2e-web-x}"
    # DAEGAM_CA_FILE(TLS 를 검사하는 프록시의 CA)은 로컬 실행에만 쓴다. 설치본으로 가는 길은 그 프록시를 지나지 않는다고 본다
    BASE="https://${DAEGAM_HOST:-}"; PROVIDER="e2e-echo-web"; CA_FILE=""
    BP_NAME="petstore-web-e2e"; MS_NAME="petstore-web-e2e"; GW_CALC="pet-web-e2e"
    DATA_TOOL="addPet"; DATA_ARGS='{"name": "e2e"}'; DATA_LABEL="petstore" ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
TICK_WAIT="${TICK_WAIT:-120}"      # 리컨실 한 틱 안에 복제본이 사라지거나 되살아나기를 기다리는 한도(초)다
API="$BASE/api"
PETSTORE_SPEC="$ROOT/mcp-runtime/tests/fixtures/petstore.openapi.yaml"
DS_NAME="pg-web-e2e"; DS_DISPLAY="웹 Tool 기억 DB"
GW_WEB="web-e2e"; GW_ONLY="web-only-e2e"
VIEWER="e2e-web-viewer"
AG_WEB="web-agent-e2e"; AG_SUB="web-sub-e2e"; AG_PIN="web-pin-e2e"
FETCH_CLONE="platform-web-fetch"
EXAMPLE="https://example.com/"
REDIRECT_URL="https://iana.org/"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-web-tools.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # header 모드는 Bearer 가 없다. 설치본은 로그인한 관리자다 (lib/e2e_api.sh 가 읽는다)
VMCP_TOKEN=""                      # vMCP 에 낼 토큰이다. header 모드는 없고, 설치본은 viewer 의 데이터 플레인 토큰이다
PF_API=""; PF_VMCP=""; PF_FETCH=""; PF_CALC=""
DS_ID=""; GW_WEB_ID=""; GW_ONLY_ID=""; GW_CALC_ID=""; AG_WEB_ID=""; AG_SUB_ID=""; AG_PIN_ID=""
PET_BP_ID=""; VIEWER_ID=""
SETTINGS_SAVED=0; ACCESS_SAVED=0
FETCH_ITEM=""; SEARCH_ITEM=""
NS=""; GW_WEB_K8S=""
INTERNET=0

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"; kill_stale_pf "$VMCP_PORT"; kill_stale_pf "$FETCH_PORT"; kill_stale_pf "$CALC_PORT"

# pass·skip·cond_skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·api_delete·wait_gone·save_doc·deploy_ready·ensure_stub_provider·remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab Database 등록(lab_database)은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# 내려가는 파드가 사라지기를 기다리는 wait_pods_settled 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# calculator 준비(calc_mcp_server·calc_publish·calc_teardown)는 여기에 있다
. "$HERE/lib/e2e_calc.sh"
# 표본 파일(chat_samples)과 업로드(upload_ok)는 여기에 있다
. "$HERE/lib/e2e_chat_files.sh"
# 플랫폼에 붙는 platform_connect, install 갈래의 viewer(local_fresh_member)와 데이터 플레인 토큰(local_data_token)은
# 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# install 갈래의 메아리 서버와 provider(echo_agent_image·echo_up·echo_provider·echo_down)는 여기에 있다
. "$HERE/lib/e2e_llm_echo.sh"

# mcp_call URL TOOL ARGS_JSON [TOKEN]  도구 하나를 불러 $WORK/mcp.json 에 {tools, call{isError, structuredContent}} 를
# 남긴다. 도구 오류도 0 으로 돌아온다(판정은 부르는 쪽이 한다). 연결하지 못하면 1 이다
mcp_call() {
  local token=()
  [ -z "${4:-}" ] || token=(--token "$4")
  uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/mcp_client.py" ${token[@]+"${token[@]}"} \
    call "$1" "$2" "$3" >"$WORK/mcp.json" 2>"$WORK/mcp.err" || [ -s "$WORK/mcp.json" ]
}
mcp_() { json_of "$WORK/mcp.json" "$1"; }
# vmcp_call TOOL ARGS_JSON  Gateway web-e2e 의 vMCP 로 부른다. VMCP_TOKEN 이 있으면 그것을 낸다
vmcp_call() { mcp_call "http://127.0.0.1:${VMCP_PORT}/mcp" "$1" "$2" "$VMCP_TOKEN"; }
# code_of  마지막 호출의 오류 코드다. 도구가 성공했으면 ok 다
code_of() { mcp_ 'd["call"]["structuredContent"].get("code", "?") if d["call"]["isError"] else "ok"'; }
# search_call ARGS_JSON  vmcp_call web_search 와 같고 성공한 검색을 OK_SEARCHES 로 센다. provider 가 값을 매긴 검색마다
# 원장에 한 행이 생기므로(platform/daegam/services/web_search.py) 5단계 끝에서 Gateway web-e2e 의 원장 줄과 맞춘다.
# 거절은 행이 없다
OK_SEARCHES=0
search_call() {
  vmcp_call web_search "$1" || return 1
  [ "$(code_of)" != ok ] || OK_SEARCHES=$((OK_SEARCHES + 1))
}
# gw_ledger  Gateway web-e2e 의 원장 줄 "calls unpriced_calls" 다. 줄이 없으면 "0 0", 읽지 못하면 "?" 다. 사용량 API 는
# 롤업이 센 시간 묶음만 읽으므로 롤업 주기(DAEGAM_USAGE_ROLLUP_SECONDS, 120초)만큼 늦다
gw_ledger() {
  [ "$(api GET "/usage?group=source&since=$LEDGER_SINCE")" = 200 ] || { echo "?"; return 0; }
  jq_ "next(((r['calls'], r['unpriced_calls']) for r in d['rows']
         if r.get('source_kind') == 'gateway_instance' and r.get('source_id') == '$GW_WEB_ID'), (0, 0))"
}

# web_settings PY  GET /settings 의 web_tools 를 PY(변수 w 를 고치는 파이썬 문)로 고쳐 PATCH 한다
web_settings() {
  local code
  code="$(api GET /settings)"
  [ "$code" = 200 ] || return 1
  python3 - "$WORK/body" "$1" >"$WORK/web-patch.json" <<'PY'
import json, sys
w = json.load(open(sys.argv[1]))["web_tools"]
exec(sys.argv[2])
print(json.dumps({"web_tools": w}))
PY
  code="$(api PATCH /settings "@$WORK/web-patch.json")"
  [ "$code" = 200 ]
}

# set_access ITEM_ID TENANTS_JSON  플랫폼 항목의 view·consume 경계를 그 테넌트 목록으로 둔다
set_access() {
  api PUT "/hub/$1/access" "{\"view\": {\"tenants\": $2}, \"consume\": {\"tenants\": $2}, \"manage\": {}}"
}

# gw_status_warnings ID  그 Gateway 상태의 warnings 를 파이썬 목록으로 찍는다
gw_status_warnings() { api GET "/gateways/$1" >/dev/null; jq_ '(d.get("status") or {}).get("warnings")'; }

# agent_body OUT NAME SPEC_PY  템플릿 위에 SPEC_PY(그래프 g 를 고치는 파이썬 문)를 얹은 Agent 정의를 OUT 에 둔다.
# 모델은 PROVIDER(orbstack 은 스텁, 설치본은 메아리), memory 는 STEP 2 의 Database 다
agent_body() {
  local out="$1" name="$2" code
  code="$(api GET /agents/templates)"
  [ "$code" = 200 ] || return 1
  python3 - "$WORK/body" "$out" "$name" "$PROVIDER" "$DS_ID" "$3" <<'PY'
import json, sys
templates, out, name, provider, ds_id, edit = json.load(open(sys.argv[1])), *sys.argv[2:7]
t = templates[0]
g, layout = t["graph"], t["layout"]
g["nodes"] = [n for n in g["nodes"] if n["type"] != "guardrail"]
g["edges"] = [e for e in g["edges"] if e["kind"] != "guardrail"]
for n in g["nodes"]:
    if n["type"] == "model":
        n["data"]["provider"] = provider
    elif n["type"] == "memory":
        n["data"]["checkpointer"]["data_service_id"] = ds_id
    elif n["type"] == "entry":
        n["data"].pop("rate_limit", None)
tools = next(n for n in g["nodes"] if n["type"] == "tools")
tools["data"].update({"allow_tools": [], "approval": {}, "timeouts": {}})
exec(edit)
layout = {k: v for k, v in layout.items() if k in {n["id"] for n in g["nodes"]}}
json.dump({"name": name, "graph": g, "layout": layout, "service": t["service"]}, open(out, "w"), ensure_ascii=False)
PY
}

# agent_up STEP NAME BODY_FILE  저장하고 배포해 Ready 를 기다린다. SAVED_ID 를 남긴다
agent_up() {
  save_doc "$1" /agents "$2" "$(cat "$3")"
  echo ">> waiting up to ${READY_TIMEOUT}s for agent $2 ($SAVED_ID) phase=Ready"
  deploy_ready "$1" /agents "$SAVED_ID" "{\"cluster_id\": \"$HOME_ID\"}"
}

# agent_rolled STEP ID  재배포한 Agent 가 Ready 이고 옛 파드가 다 사라질 때까지 기다린다. 옛 파드가 preStop 으로
# 내려가는 동안에도 rollout status 는 끝나고 그 파드가 답할 수 있다
agent_rolled() {
  local ns name
  wait_ready "/agents/$2" "$1"
  api GET "/agents/$2" >/dev/null
  ns="$(jq_ 'd["k8s"]["namespace"]')"; name="$(jq_ 'd["k8s"]["name"]')"
  "${K[@]}" -n "$ns" rollout status "deploy/$name" --timeout=180s >/dev/null || die "$1" "rollout of $name"
  wait_pods_settled "$ns" "daegam.devops.ai.kr/id=$2" || die "$1" "old pods of $name did not go away"
}

# gateway_up STEP NAME BACKENDS_JSON  Gateway NAME 을 저장하고 배포해 Ready 를 기다린다. SAVED_ID 를 남긴다
gateway_up() {
  save_doc "$1" /gateways "$2" "{\"name\": \"$2\", \"backends\": $3}"
  deploy_ready "$1" /gateways "$SAVED_ID" "{\"cluster_id\": \"$HOME_ID\"}"
}

# gateway_backends STEP ID BACKENDS_JSON  Gateway 의 백엔드를 바꿔 저장하고 재배포한다. 재배포 응답이 $WORK/body 다
gateway_backends() {
  local code
  code="$(api GET "/gateways/$2")"
  [ "$code" = 200 ] || die "$1" "GET /gateways/$2 -> $code"
  python3 - "$WORK/body" "$3" >"$WORK/gw-put.json" <<'PY'
import json, sys
d, backends = json.load(open(sys.argv[1])), json.loads(sys.argv[2])
keys = ("name", "display_name", "conflict_resolution", "failure_mode", "authz_policy", "surface", "runtime")
print(json.dumps({**{k: d[k] for k in keys if k in d}, "backends": backends}, ensure_ascii=False))
PY
  code="$(api PUT "/gateways/$2" "@$WORK/gw-put.json")"
  [ "$code" = 200 ] || die "$1" "PUT /gateways/$2 -> $code"
  code="$(api POST "/gateways/$2/redeploy")"
  [ "$code" = 200 ] || die "$1" "POST /gateways/$2/redeploy -> $code"
}

# petstore_mcp_server STEP  (install) petstore 스펙을 Backend API BP_NAME 으로 올리고(upload_backend_api) Tool DATA_TOOL
# 하나의 MCP Server MS_NAME 을 배포해 Ready 를 기다린다. calc_publish·calc_teardown 이 그대로 쓰도록 CALC_* 를 남기고
# (lib/e2e_calc.sh 머리말), 정리가 지울 Backend API 를 PET_BP_ID 에 남긴다
petstore_mcp_server() {
  local step="$1" body
  upload_backend_api "$step" "$BP_NAME" "$PETSTORE_SPEC"
  PET_BP_ID="$BACKEND_API_ID"
  CALC_BP_ID="$PET_BP_ID"; CALC_MS_NAME="$MS_NAME"
  body="$(printf '{"name": "%s", "backend_profile_id": "%s", "spec_version": %s, "tools": ["%s"]}' \
    "$MS_NAME" "$PET_BP_ID" "$BACKEND_API_VERSION" "$DATA_TOOL")"
  save_doc "$step" /mcp-servers "$MS_NAME" "$body"
  CALC_MS_ID="$SAVED_ID"
  deploy_ready "$step" /mcp-servers "$CALC_MS_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  CALC_MS_K8S_NAME="$(jq_ 'd["k8s"]["name"]')"
  CALC_NS="$(jq_ 'd["k8s"]["namespace"]')"
}

# clone_phase  fetch 복제본 MCPServer 의 phase 다. 없으면 빈 줄이다
clone_phase() { "${K[@]}" -n "$NS" get mcpserver "$FETCH_CLONE" -o jsonpath='{.status.phase}' 2>/dev/null || true; }

# in_fetch_pod PY  fetch 복제본의 MCP 컨테이너 안에서 파이썬 한 토막을 돌린다(distroless 라 셸이 없다)
in_fetch_pod() { "${K[@]}" -n "$NS" exec "$FETCH_CLONE-0" -c mcp -- python -c "$1"; }

# entry_token AUDIENCE_PART  이 네임스페이스의 Entry 토큰 Secret 가운데 aud 에 그 조각이 든 토큰을 찍는다
entry_token() {
  local s t
  for s in $("${K[@]}" -n "$NS" get secrets -o name | grep "svc-$GW_WEB_K8S-"); do
    t="$("${K[@]}" -n "$NS" get "$s" -o jsonpath='{.data.token}' | base64 -d)"
    if python3 -c 'import base64, json, sys
p = sys.argv[1].split(".")[1]; p += "=" * (-len(p) % 4)
aud = json.loads(base64.urlsafe_b64decode(p))["aud"]
sys.exit(0 if sys.argv[2] in json.dumps(aud) else 1)' "$t" "$1"; then
      printf '%s' "$t"
      return 0
    fi
  done
  return 1
}

cleanup() {
  local id item
  for id in "$AG_WEB_ID" "$AG_SUB_ID" "$AG_PIN_ID"; do
    [ -z "$id" ] || api_delete "/agents/$id"
  done
  for id in "$AG_WEB_ID" "$AG_SUB_ID" "$AG_PIN_ID"; do
    [ -z "$id" ] || wait_gone "/agents/$id" || true
  done
  for id in "$GW_WEB_ID" "$GW_ONLY_ID" "$GW_CALC_ID"; do
    [ -z "$id" ] || api_delete "/gateways/$id"
  done
  calc_teardown
  [ -z "$PET_BP_ID" ] || api_delete "/backend-apis/$PET_BP_ID"
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  # 항목의 접근 정책은 STEP 2 가 읽어 둔 그대로 되돌린다
  if [ "$ACCESS_SAVED" = 1 ]; then
    for item in "$FETCH_ITEM" "$SEARCH_ITEM"; do
      [ "$(api PUT "/hub/$item/access" "@$WORK/access-$item.json")" = 200 ] \
        || echo "!! access of hub item $item left changed" >&2
    done
  fi
  if [ "$SETTINGS_SAVED" = 1 ]; then
    [ "$(api PATCH /settings "@$WORK/settings0.json")" = 200 ] || echo "!! web tool settings left changed" >&2
  fi
  [ "$STUB_PROVIDER_ADDED" = 0 ] || remove_providers "$PROVIDER"
  [ -z "$VIEWER_ID" ] || api_delete "/users/$VIEWER_ID"
  echo_down
  stop_pf "$PF_CALC" "$CALC_PORT"; stop_pf "$PF_FETCH" "$FETCH_PORT"; stop_pf "$PF_VMCP" "$VMCP_PORT"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# ---------- 1. platform reachable ----------
# 설치본은 e2e 관리자로 공개 주소에 로그인하고, orbstack 은 port-forward 한다 (lib/e2e_local_auth.sh)
platform_connect 1
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = "200" ] || die 1 "GET /healthz -> $code"
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 1 "GET /clusters -> $code"
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"]=="home"), "")')"
[ -n "$HOME_ID" ] && [ "$HOME_OK" = "True" ] || die 1 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
# 설치본의 메아리는 홈 클러스터가 Agent 에 쓰는 이미지로 띄운다
[ "$TARGET" != install ] || echo_agent_image
NS="daegam-$TENANT"
pass 1 "platform reachable at $BASE, cluster home id=$HOME_ID preflight ok, tenant namespace $NS"

# ---------- 2. web tools on (search stub), stub provider, memory Database ----------
code="$(api GET /settings)"
[ "$code" = 200 ] || die 2 "GET /settings -> $code"
python3 - "$WORK/body" >"$WORK/settings0.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({"web_tools": d["web_tools"], "network": d["network"]}))
PY
SETTINGS_SAVED=1
web_settings 'w["fetch"]["enabled"] = True; w["search"].update({"enabled": True, "provider": "stub", "per_minute": 600, "per_day": 100000, "per_user_per_minute": 100, "per_user_per_day": 10000}); w["fetch"].update({"per_user_per_minute": 100, "per_user_per_day": 10000})' \
  || die 2 "PATCH /settings web_tools -> $(cat "$WORK/body")"
CA_NOTE="no CA"
if [ -n "$CA_FILE" ]; then
  [ -s "$CA_FILE" ] || die 2 "DAEGAM_CA_FILE=$CA_FILE is missing or empty"
  python3 -c 'import json, sys; print(json.dumps({"network": {"ca_bundle": open(sys.argv[1]).read()}}))' "$CA_FILE" >"$WORK/ca.json"
  code="$(api PATCH /settings "@$WORK/ca.json")"
  [ "$code" = 200 ] || die 2 "PATCH /settings network.ca_bundle -> $code"
  CA_NOTE="network.ca_bundle from DAEGAM_CA_FILE"
fi
code="$(api GET /hub)"
[ "$code" = 200 ] || die 2 "GET /hub -> $code"
FETCH_ITEM="$(jq_ 'next((i["id"] for i in d if i["kind"] == "platform" and i["name"] == "web-fetch"), "")')"
SEARCH_ITEM="$(jq_ 'next((i["id"] for i in d if i["kind"] == "platform" and i["name"] == "web-search"), "")')"
[ -n "$FETCH_ITEM" ] && [ -n "$SEARCH_ITEM" ] || die 2 "the hub lacks the platform items: $(jq_ '[(i["name"], i["kind"]) for i in d]')"
[ "$(jq_ '[i["status"]["ready"] for i in d if i["kind"] == "platform"]')" = "[True, True]" ] \
  || die 2 "platform items not ready: $(jq_ '[(i["name"], i["status"]) for i in d if i["kind"] == "platform"]')"
for item in "$FETCH_ITEM" "$SEARCH_ITEM"; do
  jq_ "json.dumps(next(i['access'] for i in d if i['id'] == '$item'))" >"$WORK/access-$item.json"
done
# 5단계 안에서 멈춘 실행(두 번째 신호, SIGKILL)은 두 항목을 OTHER_TENANT 로 좁힌 채 남긴다. 그 값을 그대로 두면 3단계가
# hub_item_not_consumable 로 멈추고 정리 단계가 좁힌 값을 다시 두므로, 좁혀 있으면 * 로 열고 연 값을 저장한다
for item in "$FETCH_ITEM" "$SEARCH_ITEM"; do
  [ "$(json_of "$WORK/access-$item.json" 'd["consume"]["tenants"]')" = "['$OTHER_TENANT']" ] || continue
  echo ">> hub item $item was left narrowed to $OTHER_TENANT by an earlier run; opening it to *"
  [ "$(set_access "$item" '["*"]')" = 200 ] || die 2 "reopening hub item $item -> $(cat "$WORK/body")"
  jq_ 'json.dumps(d["access"])' >"$WORK/access-$item.json"
done
ACCESS_SAVED=1
if [ "$TARGET" = install ]; then
  echo_up 2
  echo_provider 2 "$PROVIDER"
  install_postgres_database 2 "$DS_NAME" "$PLATFORM_NS" "$HOME_ID"
else
  ensure_stub_provider 2 "$PROVIDER"
  lab_database 2 postgres "$DS_NAME" "$DS_DISPLAY"
fi
DS_ID="$DATA_SERVICE_ID"
pass 2 "web_fetch and web_search on (search stub, $CA_NOTE); hub items web-fetch=$FETCH_ITEM web-search=$SEARCH_ITEM Ready; provider $PROVIDER, Database $DS_NAME=$DS_ID"

# ---------- 3. the data tool + Gateway web-e2e (fetch clone Ready) + exposure refused ----------
if [ "$TARGET" = install ]; then
  petstore_mcp_server 3
else
  calc_mcp_server 3 "$BP_NAME" "$MS_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
fi
calc_publish 3
gateway_up 3 "$GW_WEB" "[{\"hub_item_id\": \"$FETCH_ITEM\"}, {\"hub_item_id\": \"$SEARCH_ITEM\"}, {\"hub_item_id\": \"$CALC_HUB_ID\", \"tools\": [\"$DATA_TOOL\"]}]"
GW_WEB_ID="$SAVED_ID"; GW_WEB_K8S="$(jq_ 'd["k8s"]["name"]')"
[ "$(jq_ 'd["k8s"]["namespace"]')" = "$NS" ] || die 3 "Gateway namespace $(jq_ 'd["k8s"]["namespace"]') (expected $NS)"
for _ in $(seq 1 60); do [ "$(clone_phase)" = Ready ] && break; sleep 3; done
[ "$(clone_phase)" = Ready ] || die 3 "MCPServer $FETCH_CLONE phase=$(clone_phase) (expected Ready)"
"${K[@]}" -n "$NS" get pod "$FETCH_CLONE-0" -o json >"$WORK/pod.json"
POD_FACTS="$(json_of "$WORK/pod.json" '(d["metadata"]["labels"].get("daegam.devops.ai.kr/web-tool"), "daegam.devops.ai.kr/web-config" in d["metadata"]["annotations"], d["spec"].get("automountServiceAccountToken"))')"
[ "$POD_FACTS" = "fetch True False" ] || die 3 "fetch MCP pod label/annotation/automount: $POD_FACTS"
code="$(api PATCH "/gateways/$GW_WEB_ID" '{"exposure": {"enabled": true, "host": "web-e2e.e2e.local"}}')"
[ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = platform_tool_not_exposable ] \
  || die 3 "exposing $GW_WEB -> $code (expected 422 platform_tool_not_exposable)"
api GET "/gateways/$GW_WEB_ID" >/dev/null
[ "$(jq_ 'd["exposure"]["enabled"]')" = False ] || die 3 "exposure turned on after the refusal"
pass 3 "$DATA_LABEL $MS_NAME [$DATA_TOOL] published ($CALC_HUB_ID); Gateway $GW_WEB=$GW_WEB_ID Ready, fetch clone Ready (MCP pod: label fetch, web-config annotation, no SA token); exposing it -> 422 platform_tool_not_exposable"

# ---------- 4. web_search through the vMCP; 401 without a token; rate_limited; search_disabled ----------
start_pf "$NS" "svc/vmcp-$GW_WEB_K8S" "${VMCP_PORT}:4483"
PF_VMCP=$PF_PID
wait_port "$VMCP_PORT" || die 4 "port-forward to the vMCP did not open :$VMCP_PORT"
if [ "$TARGET" = install ]; then
  # local 모드의 vMCP 는 테넌트 소속의 데이터 플레인 토큰(POST /auth/token, groups daegam-<TENANT>)을 받는다. 지난
  # 실행의 같은 이름 사용자는 지우고 이 실행만 아는 비밀번호로 다시 만든다
  install_run_passwords
  VIEWER_ID="$(local_fresh_member "$VIEWER" "$TENANT")" || die 4 "creating $VIEWER in $TENANT failed"
  viewer="$(local_login_any "$VIEWER" "$LOCAL_PW")"
  [ -n "$viewer" ] || die 4 "login as $VIEWER failed"
  VMCP_TOKEN="$(local_data_token "$viewer")" || die 4 "POST /auth/token as $VIEWER failed"
fi
# 원장의 기준값이다. 이 실행이 만든 Gateway 면 0 이고, 멈춘 실행이 남긴 Gateway 를 다시 쓰면 그때의 검색이 들어 있다.
# 그 실행이 멈춘 지 롤업 한 주기(120초)가 지나지 않았으면 아직 세지 않은 행이 기준값에서 빠져, 5단계가 기대보다 많은
# calls 로 실패한다. 그때는 다시 돌리면 된다. 묶음은 시간 단위이고 사용량 API 는 비용 순 200 줄까지라 한 시간 전의
# 정각부터 읽는다. 이 머신과 서버의 시계가 정각 근처에서 어긋나도 서버의 앞 시간 묶음을 놓치지 않는다
LEDGER_SINCE="$(date -u -v-1H +%Y-%m-%dT%H:00:00Z 2>/dev/null || date -u -d '-1 hour' +%Y-%m-%dT%H:00:00Z)"
LEDGER_BASE="$(gw_ledger)"
[ "$LEDGER_BASE" != "?" ] || die 4 "GET /usage?group=source -> $(cat "$WORK/body")"
LEDGER_BASE="${LEDGER_BASE%% *}"
for _ in $(seq 1 20); do
  search_call '{"query": "daegam e2e"}' && [ "$(code_of)" = ok ] && break
  sleep 3
done
[ "$(code_of)" = ok ] || die 4 "web_search through the vMCP: $(cat "$WORK/mcp.json" "$WORK/mcp.err")"
TOOLS="$(mcp_ 'sorted(t if isinstance(t, str) else t["name"] for t in d["tools"])')"
[ "$TOOLS" = "['$DATA_TOOL', 'web_fetch', 'web_search']" ] || die 4 "vMCP tools $TOOLS"
[ "$(mcp_ 'd["call"]["structuredContent"]["provider"], len(d["call"]["structuredContent"]["results"])')" = "stub 5" ] \
  || die 4 "web_search results: $(mcp_ 'd["call"]["structuredContent"]')"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "$API/internal/mcp/web-search" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}')"
[ "$code" = 401 ] || die 4 "the search endpoint without a token -> $code (expected 401)"
web_settings 'w["search"]["per_minute"] = 1' || die 4 "PATCH search.per_minute=1 -> $(cat "$WORK/body")"
LIMITED=""
for _ in 1 2 3; do
  search_call '{"query": "limit"}'
  [ "$(code_of)" != rate_limited ] || { LIMITED=1; break; }
done
[ -n "$LIMITED" ] || die 4 "three searches at 1 a minute were never rate_limited (last $(code_of))"
RETRY="$(mcp_ 'd["call"]["structuredContent"].get("retry_after")')"
web_settings 'w["search"]["per_minute"] = 600; w["search"]["enabled"] = False' || die 4 "PATCH search off -> $(cat "$WORK/body")"
search_call '{"query": "off"}'
[ "$(code_of)" = search_disabled ] || die 4 "search off -> $(code_of) (expected search_disabled)"
web_settings 'w["search"]["enabled"] = True' || die 4 "PATCH search on -> $(cat "$WORK/body")"
pass 4 "vMCP tools $TOOLS; web_search -> 5 stub results; endpoint without a token 401; at 1 a minute rate_limited (retry_after=$RETRY); search off -> search_disabled"

# ---------- 5. narrowing the items' access ----------
for item in "$FETCH_ITEM" "$SEARCH_ITEM"; do
  code="$(set_access "$item" "[\"$OTHER_TENANT\"]")"
  [ "$code" = 200 ] || die 5 "PUT /hub/$item/access -> $code"
done
WARNINGS="$(gw_status_warnings "$GW_WEB_ID")"
echo "$WARNINGS" | grep -q hub_access_revoked || die 5 "Gateway warnings lack hub_access_revoked: $WARNINGS"
# 플랫폼은 항목과 Gateway 판정을 30초 기억한다(services/web_search.py BOUNDARY_CACHE_SECONDS·GATEWAY_CACHE_SECONDS)
for _ in $(seq 1 15); do
  search_call '{"query": "narrowed"}'
  [ "$(code_of)" = search_not_allowed ] && break
  sleep 3
done
[ "$(code_of)" = search_not_allowed ] || die 5 "search through a Gateway outside the boundary -> $(code_of) (expected search_not_allowed)"
code="$(api POST /gateways "{\"name\": \"web-narrowed-e2e\", \"backends\": [{\"hub_item_id\": \"$SEARCH_ITEM\"}]}")"
[ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = hub_item_not_consumable ] \
  || die 5 "a new Gateway with the narrowed item -> $code (expected 422 hub_item_not_consumable)"
for item in "$FETCH_ITEM" "$SEARCH_ITEM"; do
  code="$(set_access "$item" '["*"]')"
  [ "$code" = 200 ] || die 5 "restoring PUT /hub/$item/access -> $code"
done
for _ in $(seq 1 15); do
  search_call '{"query": "restored"}'
  [ "$(code_of)" = ok ] && break
  sleep 3
done
[ "$(code_of)" = ok ] || die 5 "search after restoring the policy -> $(code_of)"
# 4·5단계의 성공한 검색마다 원장에 한 행이고 거절은 행이 없다. stub 은 기본 가격(요청당 0)이 매긴다. 롤업 주기
# (120초)를 기다리므로 e2e_telemetry 처럼 5초마다 300초까지 다시 읽는다
LEDGER_WANT="$((LEDGER_BASE + OK_SEARCHES)) 0"
LEDGER="?"
for _ in $(seq 1 60); do
  LEDGER="$(gw_ledger)"
  [ "$LEDGER" != "$LEDGER_WANT" ] || break
  sleep 5
done
[ "$LEDGER" = "$LEDGER_WANT" ] \
  || die 5 "ledger of $GW_WEB (calls unpriced_calls) = $LEDGER, expected $LEDGER_WANT ($OK_SEARCHES searches ok)"
code="$(api GET "/usage?group=model&since=$LEDGER_SINCE")"
[ "$code" = 200 ] || die 5 "GET /usage?group=model -> $code"
STUB_ROW="('web_search', 'stub', 'stub')"
MODELS="$(jq_ "[(r.get('provider_kind'), r.get('provider_name'), r.get('model')) for r in d['rows']]")"
echo "$MODELS" | grep -qF "$STUB_ROW" || die 5 "the model group lacks $STUB_ROW: $MODELS"
pass 5 "narrowed to $OTHER_TENANT: Gateway hub_access_revoked, search_not_allowed, new Gateway 422 hub_item_not_consumable; restored: search ok; ledger +$OK_SEARCHES calls for $GW_WEB, none unpriced, model $STUB_ROW"

# ---------- 6. web_fetch refusals and the internet ----------
REFUSED=""
for url in "http://169.254.169.254/" "http://10.0.0.1/" "http://kubernetes.default.svc/" "http://[::ffff:127.0.0.1]/" "http://[64:ff9b::a00:1]/"; do
  vmcp_call web_fetch "{\"url\": \"$url\"}"
  case "$(code_of)" in
    url_private | url_host | url_unresolvable | url_blocked_host) REFUSED="$REFUSED $url=$(code_of)" ;;
    *) die 6 "web_fetch $url -> $(code_of) (expected a url_* refusal)" ;;
  esac
done
vmcp_call web_fetch "{\"url\": \"$EXAMPLE\"}"
if [ "$(code_of)" = ok ]; then
  INTERNET=1
  [ "$(mcp_ 'd["call"]["structuredContent"]["title"]')" = "Example Domain" ] || die 6 "example.com title: $(mcp_ 'd["call"]["structuredContent"]')"
  # iana.org 는 www.iana.org 로 보낸다(robots.txt 가 우리 이름을 막지 않는 사이트다)
  vmcp_call web_fetch "{\"url\": \"$REDIRECT_URL\"}"
  REDIRECT="$(mcp_ 'd["call"]["structuredContent"].get("redirect_to") if not d["call"]["isError"] else d["call"]["structuredContent"]')"
  case "$REDIRECT" in https://www.iana.org/*) ;; *) die 6 "$REDIRECT_URL -> $REDIRECT (expected redirect_to https://www.iana.org/)" ;; esac
  pass 6 "refused:$REFUSED; $EXAMPLE -> Example Domain; $REDIRECT_URL -> redirect_to $REDIRECT"
else
  cond_skip 6 "refused:$REFUSED; $EXAMPLE -> $(code_of) (no internet or no CA; the internet half did not run)"
fi

# ---------- 7. egress inside the fetch pod; tokens ----------
# 닫힌 주소마다 까닭(타임아웃, 거절 등)을 함께 찍는다. 정책이 막은 것과 듣는 곳이 없는 것을 가려 읽는다
EGRESS="$(in_fetch_pod 'import errno, socket
out = []
try:
    socket.getaddrinfo("example.com", 443); out.append("dns=ok")
except OSError:
    out.append("dns=fail")
for name, addr in (("public", ("example.com", 443)), ("apiserver", ("kubernetes.default.svc.cluster.local", 443)),
                   ("10.0.0.1", ("10.0.0.1", 80)), ("metadata", ("169.254.169.254", 80))):
    try:
        socket.create_connection(addr, timeout=4).close(); out.append(name + "=open")
    except OSError as e:
        out.append(name + "=closed(" + (errno.errorcode.get(e.errno or 0) or type(e).__name__) + ")")
print(" ".join(out))')"
case "$EGRESS" in *"apiserver=open"* | *"10.0.0.1=open"* | *"metadata=open"*) die 7 "the fetch pod reaches an inner address: $EGRESS" ;; esac
case "$EGRESS" in *"dns=ok"*) ;; *) die 7 "the fetch pod cannot resolve names: $EGRESS" ;; esac
[ "$INTERNET" = 0 ] || case "$EGRESS" in *"public=open"*) ;; *) die 7 "the fetch pod cannot reach public 443: $EGRESS" ;; esac
FETCH_TOKEN="$(entry_token web-fetch)" || die 7 "no fetch-audience Entry token in $NS"
TENANT_TOKEN="$(entry_token "\"daegam-internal:$TENANT\"")" || die 7 "no tenant-audience Entry token in $NS"
start_pf "$NS" "svc/mcp-$FETCH_CLONE-proxy" "${FETCH_PORT}:8080"
PF_FETCH=$PF_PID
wait_port "$FETCH_PORT" || die 7 "port-forward to the fetch proxy did not open"
start_pf "$CALC_NS" "svc/mcp-$CALC_MS_K8S_NAME-proxy" "${CALC_PORT}:8080"
PF_CALC=$PF_PID
wait_port "$CALC_PORT" || die 7 "port-forward to the $DATA_LABEL proxy did not open"
INIT='{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}}'
post_mcp() { curl -sS -o /dev/null -w '%{http_code}' -X POST "$1" -H "Authorization: Bearer $2" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d "$INIT"; }
[ "$(post_mcp "http://127.0.0.1:${FETCH_PORT}/mcp" "$FETCH_TOKEN")" = 200 ] || die 7 "the fetch clone refused its own token"
[ "$(post_mcp "http://127.0.0.1:${FETCH_PORT}/mcp" "$TENANT_TOKEN")" = 401 ] || die 7 "the fetch clone took a tenant-audience token"
[ "$(post_mcp "http://127.0.0.1:${CALC_PORT}/mcp" "$FETCH_TOKEN")" = 401 ] || die 7 "the $DATA_LABEL took the fetch-audience token"
pass 7 "fetch pod egress: $EGRESS; the clone takes its token and refuses a tenant-audience one (401); the $DATA_LABEL refuses the fetch-audience token (401)"

# ---------- 8. Agent with web tools only: origins, attachments, allow list ----------
gateway_up 8 "$GW_ONLY" "[{\"hub_item_id\": \"$FETCH_ITEM\"}, {\"hub_item_id\": \"$SEARCH_ITEM\"}]"
GW_ONLY_ID="$SAVED_ID"
agent_body "$WORK/agent-web.json" "$AG_WEB" "tools['data']['source']['id'] = '$GW_ONLY_ID'" || die 8 "agent body"
agent_up 8 "$AG_WEB" "$WORK/agent-web.json"
AG_WEB_ID="$SAVED_ID"
FETCH_MSG="$(stub_call web_fetch "{\"url\": \"$EXAMPLE\"}")"
code="$(chat "/agents/$AG_WEB_ID/chat" "$(msg "$FETCH_MSG")")"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 8 "chat -> $code"; }
SID="$(sse_ 'first("done")["session_id"]')"
# 가드가 막으면 url_* 코드다. 가드를 지나 서버가 실패한 것(fetch_timeout 같은 바깥 사정)은 가드가 허락한 것이다
FIRST="$(sse_ 'first("tool_result")["status"], first("tool_result").get("untrusted")')"
case "$(sse_ 'first("tool_result")["content"]')" in
  *url_not* | *url_blocked* | *web_turn_limit*) die 8 "fetch refused by the guard without attachments: $(sse_ 'first("tool_result")')" ;;
esac
# 런타임은 바깥 내용의 종류(web)를 싣는다
if [ "${FIRST%% *}" = success ] && [ "${FIRST#* }" != web ]; then
  die 8 "a web result not marked untrusted: $(sse_ 'first("tool_result")')"
fi
[ "${FIRST%% *}" = success ] || FIRST="the guard let it through; the server answered $(sse_ 'json.loads(first("tool_result")["content"])["code"]')"
chat_samples samples "$WORK/samples" WEB-E2E >"$WORK/samples.txt"
upload_ok 8 "$AG_WEB_ID" "$WORK/samples/$(grep '\.png$' "$WORK/samples.txt")" "d['kind'] == 'image'"
code="$(chat "/agents/$AG_WEB_ID/chat" "$(msg "$FETCH_MSG" "$SID" "$UPLOAD_ID")")"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 8 "chat with an image -> $code"; }
case "$(sse_ 'first("tool_result")["content"]')" in *url_not_allowed*) ;; *) die 8 "fetch after an attachment: $(sse_ 'first("tool_result")')" ;; esac
code="$(chat "/agents/$AG_WEB_ID/chat" "$(msg "$FETCH_MSG" "$SID")")"
case "$(sse_ 'first("tool_result")["content"]')" in *url_not_allowed*) ;; *) die 8 "fetch in the next turn: $(sse_ 'first("tool_result")')" ;; esac
agent_body "$WORK/agent-web.json" "$AG_WEB" "tools['data']['source']['id'] = '$GW_ONLY_ID'; tools['data']['allow_domains'] = ['example.com']" \
  || die 8 "agent body"
save_doc 8 /agents "$AG_WEB" "$(cat "$WORK/agent-web.json")"
code="$(api POST "/agents/$AG_WEB_ID/redeploy")"
[ "$code" = 200 ] || die 8 "redeploy with the allow list -> $code"
agent_rolled 8 "$AG_WEB_ID"
code="$(chat "/agents/$AG_WEB_ID/chat" "$(msg "$FETCH_MSG" "$SID")")"
case "$(sse_ 'first("tool_result")["content"]')" in *url_not_allowed*) die 8 "fetch with example.com allowed: $(sse_ 'first("tool_result")')" ;; esac
pass 8 "agent $AG_WEB: fetch from the person's message ($FIRST); after an image url_not_allowed, also the next turn; with example.com allowed it passes"

# ---------- 9. the data tool on the main agent, web tools on a subagent ----------
gateway_up 9 "$GW_CALC" "[{\"hub_item_id\": \"$CALC_HUB_ID\", \"tools\": [\"$DATA_TOOL\"]}]"
GW_CALC_ID="$SAVED_ID"
SUB_PY="tools['data']['source']['id'] = '$GW_CALC_ID'
g['nodes'] += [
    {'id': 'agent-sub', 'type': 'agent', 'data': {'role': 'sub', 'name': 'researcher', 'description': 'Reads web pages.', 'system_prompt': 'Fetch what you are asked.'}},
    {'id': 'model-sub', 'type': 'model', 'data': {'name': 'sub', 'provider': '$PROVIDER', 'model': None, 'max_output_tokens': 1024, 'reasoning': {'effort': 'low'}, 'temperature': None, 'fallback': None}},
    {'id': 'tools-web', 'type': 'tools', 'data': {'source': {'kind': 'gateway_instance', 'id': '$GW_ONLY_ID'}, 'allow_tools': [], 'approval': {}, 'timeouts': {}}},
]
g['edges'] += [
    {'id': 'e-delegate', 'source': 'agent-main', 'target': 'agent-sub', 'kind': 'delegate'},
    {'id': 'e-model-sub', 'source': 'agent-sub', 'target': 'model-sub', 'kind': 'model'},
    {'id': 'e-tools-web', 'source': 'agent-sub', 'target': 'tools-web', 'kind': 'tools'},
]"
agent_body "$WORK/agent-sub.json" "$AG_SUB" "$SUB_PY" || die 9 "agent body"
agent_up 9 "$AG_SUB" "$WORK/agent-sub.json"
AG_SUB_ID="$SAVED_ID"
DELEGATE="$(task_call researcher "$(stub_call web_fetch "{\"url\": \"$EXAMPLE\"}")")"
code="$(chat "/agents/$AG_SUB_ID/chat" "$(msg "$DELEGATE")")"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 9 "chat (delegate) -> $code"; }
grep -q url_not_allowed "$WORK/sse" || die 9 "the subagent's fetch beside data tools was not url_not_allowed: $(sse_ 'names')"
DATA_CALL="$(stub_call "$DATA_TOOL" "$DATA_ARGS")"
api GET "/agents/$AG_SUB_ID" >/dev/null
require_features 9 tool_safety
# data_ran LABEL -> 앞의 chat 이 남긴 상태 코드 $code 와 $WORK/sse 로, 묻지 않고 데이터 Tool 이 불렸는지 본다. 설치본의 petstore
# 는 스펙의 서버가 답하지 않아 Tool 오류로 끝날 수 있다
data_ran() {
  local ran
  ran="$(sse_ 'first("tool_result")["status"] if first("tool_result") else names')"
  case "$TARGET:$ran" in orbstack:success | install:success | install:error) ;; *) ran="" ;; esac
  [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] && [ -n "$ran" ] \
    || { cp "$WORK/sse" "$WORK/body"; die 9 "$1: $DATA_TOOL did not run without approval: $(sse_ 'names')"; }
  RAN="$ran"
}
# data_asks SID -> 세션 SID 에 이미지를 실은 턴에서 데이터 Tool 이 이유 outside 로 승인을 기다린다. 그 보류는 거부로 닫는다
data_asks() {
  upload_ok 9 "$AG_SUB_ID" "$WORK/samples/$(grep '\.png$' "$WORK/samples.txt")" "d['kind'] == 'image'"
  code="$(chat "/agents/$AG_SUB_ID/chat" "$(msg "$DATA_CALL" "$1" "$UPLOAD_ID")")"
  { [ "$code" = 200 ] && asked "$DATA_TOOL" outside; } \
    || { cp "$WORK/sse" "$WORK/body"; die 9 "$DATA_TOOL after an image in session $1 did not ask with reason outside: $(sse_ 'names') $(sse_ 'first("interrupt")')"; }
  APPROVAL="$(sse_ 'first("interrupt")["review_configs"][0]["allowed_decisions"]')"
  code="$(chat "/agents/$AG_SUB_ID/sessions/$1/decisions" '{"decisions": [{"type": "reject"}]}')"
  [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 9 "reject in session $1 -> $code $(sse_ 'names')"; }
}
code="$(chat "/agents/$AG_SUB_ID/chat" "$(msg "$DATA_CALL")")"
data_ran "a new session"
RESULT="$(sse_ 'first("tool_result")["content"]')"
data_asks "$(sse_ 'first("done")["session_id"]')"
pass 9 "agent $AG_SUB: the subagent's fetch beside the $DATA_LABEL -> url_not_allowed; $DATA_TOOL in a new session runs without approval ($RAN: ${RESULT:0:100}); after an image it asks with reason outside ($APPROVAL)"

# ---------- 10. tool pins: a web backend added later, a backend removed ----------
agent_body "$WORK/agent-pin.json" "$AG_PIN" "tools['data']['source']['id'] = '$GW_CALC_ID'" || die 10 "agent body"
agent_up 10 "$AG_PIN" "$WORK/agent-pin.json"
AG_PIN_ID="$SAVED_ID"
gateway_backends 10 "$GW_CALC_ID" "[{\"hub_item_id\": \"$CALC_HUB_ID\", \"tools\": [\"$DATA_TOOL\"]}, {\"hub_item_id\": \"$FETCH_ITEM\"}]"
echo "$(jq_ 'd.get("warnings")')" | grep -q web_backend_pinned_agents || die 10 "redeploy warnings: $(jq_ 'd.get("warnings")')"
wait_ready "/gateways/$GW_CALC_ID" 10
api GET "/agents/$AG_PIN_ID" >/dev/null
PIN_NS="$(jq_ 'd["k8s"]["namespace"]')"; PIN_K8S="$(jq_ 'd["k8s"]["name"]')"
PIN_POD="$("${K[@]}" -n "$PIN_NS" get pods -l "daegam.devops.ai.kr/id=$AG_PIN_ID" -o jsonpath='{.items[0].metadata.name}')"
[ -n "$PIN_POD" ] || die 10 "no pod of agent $AG_PIN"
"${K[@]}" -n "$PIN_NS" delete pod "$PIN_POD" --wait=false >/dev/null
sleep 5
wait_ready "/agents/$AG_PIN_ID" 10
"${K[@]}" -n "$PIN_NS" rollout status "deploy/$PIN_K8S" --timeout=180s >/dev/null || die 10 "agent pod did not come back"
wait_pods_settled "$PIN_NS" "daegam.devops.ai.kr/id=$AG_PIN_ID" || die 10 "the deleted pod did not go away"
NEW_POD="$("${K[@]}" -n "$PIN_NS" get pods -l "daegam.devops.ai.kr/id=$AG_PIN_ID" -o jsonpath='{.items[0].metadata.name}')"
[ -n "$NEW_POD" ] && [ "$NEW_POD" != "$PIN_POD" ] || die 10 "the agent pod was not replaced ($PIN_POD -> $NEW_POD)"
for _ in $(seq 1 24); do
  WARNINGS="$(api GET "/agents/$AG_PIN_ID" >/dev/null; jq_ '(d.get("status") or {}).get("warnings")')"
  echo "$WARNINGS" | grep -q tools_behind && break
  sleep 5
done
echo "$WARNINGS" | grep -q tools_behind || die 10 "agent warnings lack tools_behind: $WARNINGS"
code="$(chat "/agents/$AG_PIN_ID/chat" "$(msg "$(stub_call web_fetch "{\"url\": \"$EXAMPLE\"}")")")"
case "$(sse_ 'first("tool_result")["status"] if first("tool_result") else names')" in
  success) die 10 "the pinned agent called web_fetch: $(sse_ 'first("tool_result")')" ;;
esac
gateway_backends 10 "$GW_CALC_ID" "[{\"hub_item_id\": \"$FETCH_ITEM\"}]"
wait_ready "/gateways/$GW_CALC_ID" 10
for _ in $(seq 1 24); do
  WARNINGS="$(api GET "/agents/$AG_PIN_ID" >/dev/null; jq_ '(d.get("status") or {}).get("warnings")')"
  echo "$WARNINGS" | grep -q tools_missing && break
  sleep 5
done
echo "$WARNINGS" | grep -q tools_missing || die 10 "agent warnings lack tools_missing: $WARNINGS"
pass 10 "agent $AG_PIN: web-fetch added to $GW_CALC warned web_backend_pinned_agents; the restarted pod came back without web_fetch and warns tools_behind; $DATA_LABEL removed -> tools_missing"

# ---------- 11. fetch off and on ----------
web_settings 'w["fetch"]["enabled"] = False' || die 11 "PATCH fetch off -> $(cat "$WORK/body")"
for _ in $(seq 1 $((TICK_WAIT / 5))); do [ -z "$(clone_phase)" ] && break; sleep 5; done
[ -z "$(clone_phase)" ] || die 11 "the fetch clone is still there ${TICK_WAIT}s after fetch went off"
code="$(api POST "/gateways/$GW_WEB_ID/redeploy")"
[ "$code" = 409 ] && [ "$(jq_ 'd["detail"]["code"]')" = backends_not_ready ] || die 11 "redeploy with fetch off -> $code (expected 409 backends_not_ready)"
web_settings 'w["fetch"]["enabled"] = True' || die 11 "PATCH fetch on -> $(cat "$WORK/body")"
for _ in $(seq 1 $((TICK_WAIT / 5))); do [ "$(clone_phase)" = Ready ] && break; sleep 5; done
[ "$(clone_phase)" = Ready ] || die 11 "the fetch clone was not restored ${TICK_WAIT}s after fetch went on (phase=$(clone_phase))"
pass 11 "fetch off: the clone went within a tick and a Gateway redeploy is 409 backends_not_ready; fetch on: the clone came back"

finish
