# shellcheck shell=bash
# MCP 를 curl 로 직접 부르는 부분. `. "$HERE/lib/mcp_rpc.sh"` 로 읽는다.
# 읽기 전에 MCP_CLIENT_NAME 을 정한다 (initialize 의 clientInfo.name. 게이트마다 다르다).
# 부르는 쪽의 WORK 를 쓰고, mcp_ingress 는 lib/e2e_api.sh 의 hdr 를, rpc_ 는 lib/e2e_json.sh 의 json_of 를 쓴다 (읽는
# 게이트가 모두 둘을 읽는다). svc_token 은 부를 때 부르는 쪽의 ROOT·CTX·PLATFORM_NS·TENANT 를 읽는다. mcp·mcp_json·
# mcp_admits·mcp_refuses 는 deploy/dev/lab/mcp_client.py 로 부르고 ROOT 와 lib/e2e_report.sh 의 die 를 쓴다.
#
# deploy/dev/lab/mcp_client.py 는 URL 로만 붙어 host 를 IP 에 못 박을 수 없다. 여기서는 curl --resolve 로
# initialize 와 notifications/initialized 를 보낸 뒤 요청 한 번을 직접 보내고, 응답이 SSE 든 JSON 이든
# 마지막 JSON-RPC 결과를 $WORK/rpc.json 에 남긴다.
#
# 프로토콜 버전은 여기 한 곳에만 있다. 올릴 때 이 줄만 고친다.
INIT_PARAMS="{\"protocolVersion\": \"2025-03-26\", \"capabilities\": {}, \"clientInfo\": {\"name\": \"${MCP_CLIENT_NAME:?MCP_CLIENT_NAME must be set before sourcing mcp_rpc.sh}\", \"version\": \"0\"}}"

# mcp_status URL [TOKEN] [HEADER_FILE] -> HTTP 코드
#   initialize 한 번을 보내 HTTP 상태만 본다 (MCP 클라이언트는 401 을 일반 MCPError 로 감싼다).
#   port-forward 주소에 쓰므로 --resolve·-k 를 붙이지 않는다. HEADER_FILE 을 주면 응답 헤더를 거기 남긴다.
mcp_status() {
  local url="$1" token="${2:-}" hdr="${3:-}" extra=()
  [ -n "$token" ] && extra+=(-H "Authorization: Bearer $token")
  [ -n "$hdr" ] && extra+=(-D "$hdr")
  curl -sS -o /dev/null -w '%{http_code}' -X POST "$url" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' ${extra[@]+"${extra[@]}"} \
    -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"initialize\", \"params\": $INIT_PARAMS}"
}

rpc_curl() { # URL RESOLVE [extra curl args...] -> 상태 코드. 본문 $WORK/rpc.raw, 헤더 $WORK/rpc.hdr
  local url="$1" resolve="$2"; shift 2
  curl -sSk -o "$WORK/rpc.raw" -D "$WORK/rpc.hdr" -w '%{http_code}' --resolve "$resolve" --max-time 30 -X POST "$url" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' "$@" 2>/dev/null || echo 000
}

rpc_extract() { # $WORK/rpc.raw (JSON 또는 SSE) -> $WORK/rpc.json
  python3 - "$WORK/rpc.raw" "$WORK/rpc.json" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
out = None
if raw.lstrip().startswith("{"):
    out = json.loads(raw)
else:
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except ValueError:
                pass
json.dump(out, open(sys.argv[2], "w"))
PY
}

# rpc_ EXPR
#   rpc_extract 가 남긴 $WORK/rpc.json 을 json_of 로 읽는다
rpc_() { json_of "$WORK/rpc.json" "$1"; }

# svc_token [SERVER_ID [SVC_TOKEN_ARG...]] -> 서비스 토큰
#   내부 MCPServer 프록시가 요구하는 TENANT 의 서비스 토큰(D41)을 찍는다. deploy/dev/lab/svc_token.py 가 플랫폼의 서명 키
#   Secret 으로 직접 서명한다. SERVER_ID 를 주면 그 서버의 audience 를 함께 싣는다. 자기 audience 로 옮겨 간 서버(P50 9절)는
#   그것이 없는 토큰을 401 로 거절한다. 그 뒤의 인자는 svc_token.py 에 그대로 간다(뒤의 --tenant 가 앞의 것을 이긴다)
svc_token() {
  local server=()
  [ -z "${1:-}" ] || server=(--server "$1")
  [ $# -eq 0 ] || shift
  uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/svc_token.py" \
    --context "$CTX" --namespace "$PLATFORM_NS" --tenant "$TENANT" ${server[@]+"${server[@]}"} "$@"
}

get_ingress() { # URL RESOLVE -> HTTP 코드. GET 한 번이고 본문은 $WORK/get.raw 다
  # 노출 Ingress 가 MCP 경로 밖(/status 등)을 막는지와 RFC 9728 메타데이터를 본다 (P50 5절)
  curl -sSk -o "$WORK/get.raw" -w '%{http_code}' --resolve "$2" --max-time 30 "$1" 2>/dev/null || echo 000
}

mcp_ingress() { # URL RESOLVE METHOD [PARAMS-JSON] [BEARER] -> 마지막 요청의 HTTP 코드. 결과는 $WORK/rpc.json
  local url="$1" resolve="$2" method="$3" params="${4:-}" token="${5:-}" code sid auth=() sess=()
  [ -n "$params" ] || params="{}"
  [ -n "$token" ] && auth=(-H "Authorization: Bearer $token")
  code="$(rpc_curl "$url" "$resolve" ${auth[@]+"${auth[@]}"} -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"initialize\", \"params\": $INIT_PARAMS}")"
  [ "$code" = "200" ] || { rpc_extract; echo "$code"; return 0; }
  sid="$(hdr mcp-session-id "$WORK/rpc.hdr")"
  [ -n "$sid" ] && sess=(-H "Mcp-Session-Id: $sid")
  rpc_curl "$url" "$resolve" ${auth[@]+"${auth[@]}"} ${sess[@]+"${sess[@]}"} -d '{"jsonrpc": "2.0", "method": "notifications/initialized"}' >/dev/null
  code="$(rpc_curl "$url" "$resolve" ${auth[@]+"${auth[@]}"} ${sess[@]+"${sess[@]}"} -d "{\"jsonrpc\": \"2.0\", \"id\": 2, \"method\": \"$method\", \"params\": $params}")"
  rpc_extract
  echo "$code"
}

# mcp [--token T] list|call URL [TOOL ARGS_JSON] -> 종료 코드
#   deploy/dev/lab/mcp_client.py 로 부른다. 결과는 $WORK/mcp.json, 오류는 $WORK/mcp.err 다
mcp() {
  local rc=0
  uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/mcp_client.py" "$@" \
    >"$WORK/mcp.json" 2>"$WORK/mcp.err" || rc=$?
  echo "$rc"
}

# mcp_json EXPR
#   mcp 가 남긴 $WORK/mcp.json 을 json_of 로 읽는다
mcp_json() { json_of "$WORK/mcp.json" "$1"; }

# call_result_is N OUTPUT
#   mcp_client.py 의 call 출력이 구조화된 결과 {"result": N} 을 돌려줬는가 (Add(3,4) 면 7). 4 와 4.0 은 같다
call_result_is() { [ "$(json_of - "d['call']['structuredContent'] == {'result': $1}" <<<"$2")" = True ]; }

# mcp_admits STEP TOKEN WHAT URL
#   그 토큰으로 tools/list 에 Add 가 보이고 Add(3,4) 가 7 이어야 한다. 아니면 die STEP 이다. 부른 Tool 이름(vMCP 는 백엔드
#   접두를 붙인다)을 MCP_ADMITTED_TOOL 에 남긴다. lib/e2e_mcp_access.sh 의 MCP_TOOL 과 겹치지 않는 이름이다
mcp_admits() {
  local rc
  rc="$(mcp --token "$2" list "$4")"
  [ "$rc" = 0 ] || die "$1" "$3 tools/list rc=$rc: $(cat "$WORK/mcp.err")"
  MCP_ADMITTED_TOOL="$(mcp_json 'next((t for t in d["tools"] if t.endswith("Add")), "")')"
  [ -n "$MCP_ADMITTED_TOOL" ] || die "$1" "$3 sees no Add tool: $(mcp_json 'd["tools"]')"
  rc="$(mcp --token "$2" call "$4" "$MCP_ADMITTED_TOOL" '{"a": 3, "b": 4}')"
  { [ "$rc" = 0 ] && mcp_json 'json.dumps(d["call"]["content"])' | grep -q 7; } \
    || die "$1" "$3 call $MCP_ADMITTED_TOOL rc=$rc: $(cat "$WORK/mcp.json" "$WORK/mcp.err")"
}

# mcp_refuses STEP TOKEN WHAT URL
#   그 토큰에 빈 tools/list 와 거부된 tools/call 이어야 한다. Cedar 가 막은 토큰의 모양이다(테넌트 울타리 P50 11절, 위임
#   토큰의 forbid P53 F0 2번). 아니면 die STEP 이다. 거부된 호출의 답은 $WORK/mcp.json·mcp.err 에 남는다
mcp_refuses() {
  local rc
  rc="$(mcp --token "$2" list "$4")"
  [ "$rc" = 0 ] || die "$1" "$3 tools/list rc=$rc: $(cat "$WORK/mcp.err")"
  [ "$(mcp_json 'd["tools"]')" = "[]" ] || die "$1" "$3 saw tools: $(mcp_json 'd["tools"]')"
  rc="$(mcp --token "$2" call "$4" Add '{"a": 3, "b": 4}')"
  [ "$rc" != 0 ] || die "$1" "$3 called Add: $(cat "$WORK/mcp.json")"
}
