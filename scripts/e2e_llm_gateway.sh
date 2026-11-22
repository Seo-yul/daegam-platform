#!/usr/bin/env bash
# P38 (D63) LLM 게이트웨이 e2e. OpenAI 호환·Anthropic 메아리 서버(deploy/dev/lab/llm_echo.py)를 provider 둘로 두고 게이트웨이를
# 켠 플랫폼에서 Agent 의 경로 옮기기, 키 반영, 한도, 회수, 게이트웨이가 없을 때, 플랫폼 호출자, 끄기를 본다. 모델은 부르지
# 않는다.
#
#   make e2e-llm-gateway                              로컬 클러스터(k3d·orbstack, deploy-local·lab-up 이후). 대감 차트의
#                                                     게이트웨이 네임스페이스와 Role(templates/llm-gateway.yaml)을 찍어
#                                                     적용하고(테넌트 라벨이 있으면 멈춘다), 플랫폼 ConfigMap 의
#                                                     DAEGAM_LLM_GATEWAY 를 agentgateway 로 두었다가 되돌린다
#   make e2e-llm-gateway E2E_TARGET=install E2E_INSTALL_USER=<관리자>
#                                                     설치본. 게이트웨이를 켠 설치(차트의 llmGateway.mode agentgateway)가
#                                                     먼저다. 게이트웨이를 켜지 않은 설치본이면 멈춘다
#
# P37 과 P38 의 e2e 는 함께 돌리지 않는다. 둘 다 메아리 네임스페이스 daegam-e2e-llm 을 쓴다. EXIT 트랩이 성공, 실패, 끊김
# 어느 쪽이든 바꾼 설정(ConfigMap 의 스위치, 한도, 비서, 일시 정지 표시)을 되돌리고 만든 것을 걷는다. 단계는 다음과 같다:
#   STEP 0   게이트웨이를 켜고(로컬이면 ConfigMap 을 바꾸고 플랫폼을 굴린다), 메아리 네임스페이스에 메아리를 띄워 provider
#            echo-openai·echo-anthropic(그 네임스페이스의 .svc 주소, 가짜 키)을 둔다
#   STEP 1   상태 API 가 켜짐이고 반영된 리비전이 있으며 두 provider 가 providers_ready 에 있다
#   STEP 2   화면의 키 저장 경로(D56)로 키를 넣으면 연결 확인은 곧바로 ok 이고 게이트웨이는 2분 안에 그 키를 반영한다
#   STEP 3   두 provider 를 쓰는 새 Agent 는 직접 경로(runtime_features_unknown, 키 복사본)로 시작하고, init 의 기록 뒤 재적용
#            한 번에 옮긴다. 그 롤아웃 뒤 테넌트 네임스페이스에 -llm- Secret 이 없고 파드 env 는 llm_token 을 가리킨다
#   STEP 4   채팅 턴에서 메아리는 provider 키만 받고 JWT 는 받지 않으며, 게이트웨이의 원장 행이 그 세션의 트레이스에 이어진다
#   STEP 5   바꾼 키가 재배포 없이 2분 안에 메아리에 닿는다
#   STEP 6   작은 테넌트 한도를 두면 다음 턴은 llm_gateway_rate_limited 다
#   STEP 7   해제 앞에 읽어 둔 토큰은 2분 안에 403 이고, 다른 Agent 의 곧바로 교체에서 옛 토큰도 그렇다
#   STEP 8   동기화를 멈추고(네임스페이스 어노테이션 daegam.devops.ai.kr/llm-gateway-paused) 게이트웨이를 0 으로 줄여 파드를
#            유예 없이 지우면 채팅은 10초 안에 llm_gateway_unreachable 이다. 어노테이션을 걷으면 동기화가 파드를 되살린다
#   STEP 9   비서 턴과 Knowledge 문서 하나의 인덱싱이 게이트웨이 행을 남긴다
#   STEP 10  끄고 플랫폼을 굴린 뒤 게이트웨이 경로 파드를 곧바로 지워도 채팅이 된다. 그 뒤 Agent 는 키 복사본의 직접 경로이고,
#            롤아웃 뒤 llm_token 이 빠지며, 게이트웨이 객체가 걷힌다
#
# 출력은 `STEP <n> PASS|SKIP|FAIL: ...` 이고 종료 코드 계약은 lib/e2e_report.sh 에 있다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/lib/e2e_exit.sh"
. "$HERE/lib/e2e_ports.sh"
. "$HERE/lib/e2e_json.sh"
. "$HERE/lib/hosts.sh"
TARGET="${E2E_TARGET:-orbstack}"
API_PORT="${E2E_API_PORT:-$PORT_LLM_GATEWAY_API}"
GW_PORT="${E2E_GATEWAY_PORT:-$PORT_LLM_GATEWAY_PROXY}"
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-llm-gw}"; BASE="http://127.0.0.1:${API_PORT}" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"; BASE="https://${DAEGAM_HOST:-}" ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
GW_NS="${LLM_GATEWAY_NS:-daegam-llm}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
API="$BASE/api"
OPENAI="echo-openai"; ANTHROPIC="echo-anthropic"
AG_A="llm-gw-e2e-a"; AG_B="llm-gw-e2e-b"
DS_NAME="pg-llm-gw-e2e"; KB_NAME="faq-llm-gw-e2e"
# 2분 안에 닿아야 하는 것(키, 회수)을 기다리는 한도다. 한 틱(60초)과 kubelet 의 Secret 동기화를 넘는다
APPLY_WAIT=150
UNREACHABLE_WITHIN=10
GONE_WAIT=60 # 지운 게이트웨이 파드의 컨테이너와 서비스 규칙이 사라지기를 기다리는 초
PAUSE_ANNOTATION="daegam.devops.ai.kr/llm-gateway-paused"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-llm-gateway.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()
PF_API=""; PF_GW=""
HOME_ID=""; A_ID=""; B_ID=""; A_NS=""; A_K8S=""; A_SECRET=""; B_NS=""; B_K8S=""; B_SECRET=""; DS_ID=""; KB_ID=""
LIMITS_SAVED=""; ASSISTANT_SAVED=""; PAUSED=0; SCALED=0; GATEWAY_WINDOW=0

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$GW_PORT"
. "$HERE/lib/e2e_report.sh"
. "$HERE/lib/e2e_api.sh"
. "$HERE/lib/e2e_sse.sh"
. "$HERE/lib/e2e_lab.sh"
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_local_auth.sh"
# 메아리 서버와 그 provider(echo_up·echo_provider·echo_down, ECHO_NS·ECHO_URL)는 여기에 있다
. "$HERE/lib/e2e_llm_echo.sh"

failure_report() { # 실패한 게이트가 정리 앞에 남기는 진단. 정리가 플랫폼을 굴리면 동기화의 로그가 사라진다
  echo "--- llm gateway diagnostics (namespace $GW_NS)"
  "${K[@]}" -n "$GW_NS" get deploy,pods,svc,networkpolicy,configmap,secret,role,rolebinding -o wide 2>&1 | head -n 80
  "${K[@]}" -n "$GW_NS" get events --sort-by=.lastTimestamp 2>&1 | tail -n 40
  echo "--- platform log without the access lines (every pod, last 200 lines)"
  "${K[@]}" -n "$PLATFORM_NS" logs -l app=daegam-platform --all-containers --prefix --tail=-1 --max-log-requests=10 2>&1 \
    | grep -v 'HTTP/1\.1" ' | tail -n 200
  echo "--- end of llm gateway diagnostics"
}
cleanup() {
  bg_stop
  [ "${1:-0}" != 1 ] || failure_report >&2 || true
  if [ "$PAUSED" = 1 ]; then
    "${K[@]}" annotate namespace "$GW_NS" "$PAUSE_ANNOTATION-" >/dev/null 2>&1 || true
  fi
  [ "$SCALED" = 0 ] || "${K[@]}" -n "$GW_NS" scale deploy/llm-gateway --replicas=2 >/dev/null 2>&1 || true
  [ -z "$A_ID" ] || api_delete "/agents/$A_ID"
  [ -z "$B_ID" ] || api_delete "/agents/$B_ID"
  [ -z "$KB_ID" ] || api_delete "/knowledge-bases/$KB_ID"
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  [ -z "$LIMITS_SAVED" ] || api PATCH /settings "@$LIMITS_SAVED" >/dev/null || echo "!! llm_gateway limits not restored: PATCH /settings with $LIMITS_SAVED" >&2
  [ -z "$ASSISTANT_SAVED" ] || api PATCH /settings "@$ASSISTANT_SAVED" >/dev/null || true
  echo_down
  if [ "$GATEWAY_WINDOW" = 1 ]; then
    config_window_restore || true
    [ "$CONFIG_ROLL_PENDING" = 0 ] || restart_platform || true
  fi
  stop_pf "$PF_GW" "$GW_PORT"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# --- 도우미 -------------------------------------------------------------------------------------------------
gateway_status() { # STEP -> $WORK/body 에 상태 API 의 답
  local code
  code="$(api GET /settings/llm-gateway)"
  [ "$code" = 200 ] || die "$1" "GET /settings/llm-gateway -> $code"
}
wait_gateway() { # STEP SECONDS EXPR MSG -> 상태 API 의 답(d)에서 EXPR 이 참이 될 때까지 5초마다 본다
  local i
  for ((i = 0; i < $2 / 5; i++)); do
    gateway_status "$1"
    [ "$(jq_ "bool($3)")" = True ] && return 0
    sleep 5
  done
  die "$1" "$4 not within $2s: $(cat "$WORK/body")"
}
route_of() { # ID -> "route reason"
  api GET "/agents/$1" >/dev/null
  jq_ 'd["llm_route"]["route"] + " " + str(d["llm_route"]["reason"])'
}
wait_route() { # STEP ID ROUTE SECONDS
  local i got=""
  for ((i = 0; i < $4 / 5; i++)); do
    got="$(route_of "$2")"
    [ "${got%% *}" = "$3" ] && return 0
    sleep 5
  done
  die "$1" "agent $2 route is $got, not $3 after $4s"
}
reapply_once() { # STEP -> 인증 재적용의 다음 틱을 기다린다(리컨실 60초). 그 틱이 기록을 본 경로를 정한다
  local before i
  api GET /settings/data-plane-auth >/dev/null
  before="$(jq_ '(d.get("summary") or {}).get("tick_at") or ""')"
  for ((i = 0; i < 30; i++)); do
    sleep 5
    api GET /settings/data-plane-auth >/dev/null
    [ "$(jq_ '(d.get("summary") or {}).get("tick_at") or ""')" = "$before" ] || return 0
  done
  die "$1" "no data-plane auth tick in 150s"
}
echo_after() { # TS PATH -> 그 뒤 메아리가 그 경로로 받은 JSON 줄
  "${K[@]}" -n "$ECHO_NS" logs deploy/llm-echo --tail=-1 2>/dev/null \
    | python3 -c 'import json, sys
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if d.get("llm_echo") == sys.argv[2] and d["ts"] >= float(sys.argv[1]):
        print(json.dumps(d))' "$1" "$2"
}
turn() { # STEP AGENT_ID TEXT -> done 으로 끝나야 한다. 세션 id 와 trace id 를 TURN_SESSION·TURN_TRACE 에 남긴다
  local code
  code="$(chat "/agents/$2/chat" "$(msg "$3")")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die "$1" "chat -> $code"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die "$1" "turn ended with $(sse_ 'names')"; }
  TURN_SESSION="$(sse_ 'first("done")["session_id"]')"; TURN_TRACE="$(sse_ 'first("done")["trace_id"]')"
}
turn_error() { # AGENT_ID TEXT -> 턴이 error 로 끝났으면 그 코드, 아니면 빈 줄
  chat "/agents/$1/chat" "$(msg "$2")" >/dev/null
  sse_ 'first("error")["code"] if "error" in names else ""'
}
gw_call() { # TOKEN -> 게이트웨이에 직접 보낸 요청의 상태 코드. 경로는 echo-openai 의 Completions 다
  # 토큰은 0600 머리 파일로 넘긴다. curl 의 인자에 실으면 ps 로 보인다. printf 는 셸 내장이라 인자가 남지 않는다
  local header="$WORK/gw-auth"
  (umask 077 && printf 'Authorization: Bearer %s\n' "$1" >"$header")
  curl -sS -o "$WORK/gw-body" -w '%{http_code}' -X POST "http://127.0.0.1:${GW_PORT}/p/${OPENAI}/v1/chat/completions" \
    -H "@$header" -H 'Content-Type: application/json' \
    -d '{"model": "echo-chat", "messages": [{"role": "user", "content": "hi"}]}' || true
  rm -f "$header"
}
wait_denied() { # STEP TOKEN WHAT -> 그 토큰이 APPLY_WAIT 초 안에 401·403 이 된다
  local i code=""
  for ((i = 0; i < APPLY_WAIT / 5; i++)); do
    code="$(gw_call "$2")"
    case "$code" in 401 | 403) return 0 ;; esac
    sleep 5
  done
  die "$1" "$3 still answers $code after ${APPLY_WAIT}s"
}
gateway_calls() { # -> 이 테넌트의 게이트웨이 경로 원장 호출 수
  api GET "/admin/usage?group=route&tenant=$TENANT" >/dev/null
  jq_ 'sum(r["calls"] for r in d["rows"] if r.get("route") == "gateway")'
}
wait_more_gateway_calls() { # STEP BEFORE WHAT
  local i now="$2"
  for ((i = 0; i < 40; i++)); do
    now="$(gateway_calls)"
    [ "$now" -le "$2" ] || return 0
    sleep 5
  done
  die "$1" "$3 left no gateway row ($2 -> $now)"
}
new_agent() { # STEP NAME PROVIDER [SUB_PROVIDER] -> SAVED_ID, 배포를 마친 문서, $WORK/deployed-NAME.json 에 배포 응답
  local sub=()
  [ -z "${4:-}" ] || sub=(--sub-name helper --sub-provider "$4")
  api GET /agents/templates >/dev/null
  python3 "$HERE/lib/agent_body.py" "$WORK/body" "$WORK/agent-$2.json" --name "$2" --display "LLM 게이트웨이 e2e" \
    --provider "$3" ${sub[@]+"${sub[@]}"}
  save_doc "$1" /agents "$2" "@$WORK/agent-$2.json"
  # 배포 응답은 provision 이 정한 경로다. 그 뒤의 리컨실 틱이 먼저 옮길 수 있어 이것을 본다
  deploy_doc "$1" /agents "$SAVED_ID"
  cp "$WORK/body" "$WORK/deployed-$2.json"
  wait_ready "/agents/$SAVED_ID" "$1"
  api GET "/agents/$SAVED_ID" >/dev/null
}
# --- 연결 -------------------------------------------------------------------------------------------------------
platform_connect 0
code="$(api GET /clusters)"
[ "$code" = 200 ] || die 0 "GET /clusters -> $code"
HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home"), "")')"
[ -n "$HOME_ID" ] || die 0 "cluster home missing"
echo_agent_image

# ---------- 0. 게이트웨이를 켜고 메아리와 provider 둘 ----------
label="$("${K[@]}" get namespace "$GW_NS" -o 'jsonpath={.metadata.labels.daegam\.devops\.ai\.kr/component}' 2>/dev/null || true)"
if [ "$TARGET" = install ]; then
  [ "$label" = llm-gateway ] || die 0 "namespace $GW_NS is not installed on the install's cluster; install the chart with llmGateway.mode agentgateway first"
else
  gw_tenant="$("${K[@]}" get namespace "$GW_NS" -o 'jsonpath={.metadata.labels.daegam\.devops\.ai\.kr/tenant}' 2>/dev/null || true)"
  [ -z "$gw_tenant" ] || die 0 "namespace $GW_NS carries the tenant label daegam.devops.ai.kr/tenant ($gw_tenant); set LLM_GATEWAY_NS to another name"
  # 로컬 릴리스는 게이트웨이를 끈 채라 차트가 이 네임스페이스를 렌더하지 않는다. 같은 템플릿을 찍어 kubectl 로 둔다. 늘
  # 적용하므로(멱등) 다시 쓰는 클러스터도 이 판의 Role 을 받는다. 뒤에 같은 클러스터에서 LLM_GATEWAY=agentgateway 로
  # make deploy-local 하면 이것을 HELM_FLAGS=--take-ownership 으로 넘겨받는다
  helm template daegam "$ROOT/charts/daegam" -n "$PLATFORM_NS" -f "$ROOT/charts/daegam/ci/offline-values.yaml" \
    --set llmGateway.mode=agentgateway --set "llmGateway.namespace=$GW_NS" --show-only templates/llm-gateway.yaml \
    | "${K[@]}" apply -f - >/dev/null || die 0 "applying the chart's gateway namespace $GW_NS failed"
fi
if [ "$(platform_config DAEGAM_LLM_GATEWAY)" != agentgateway ]; then
  [ "$TARGET" != install ] || die 0 "the install's platform runs with DAEGAM_LLM_GATEWAY off; deploy it with the LLM gateway on (agentgateway)"
  config_window_plan DAEGAM_LLM_GATEWAY=agentgateway || die 0 "reading ConfigMap $AUTH_MODE_CONFIGMAP failed"
  echo "   $CONFIG_RESTORE_HINT"
  config_window_apply || die 0 "patching ConfigMap $AUTH_MODE_CONFIGMAP failed"
  GATEWAY_WINDOW=1
  restart_platform || die 0 "the platform did not roll after turning the gateway on"
  restart_pf "$API_PORT"
  wait_http "$BASE/healthz" || die 0 "the platform does not answer after the roll"
fi
echo_up 0
echo_provider 0 "$OPENAI"
api GET /settings >/dev/null
claude="$(printf '{"name": "%s", "kind": "anthropic", "model": "%s", "base_url": "%s"}' "$ANTHROPIC" "$ECHO_CHAT_MODEL" "$ECHO_URL")"
code="$(api PATCH /settings "$(providers_with "$WORK/body" "$claude")")"
[ "$code" = 200 ] || die 0 "PATCH /settings (echo provider $ANTHROPIC) -> $code"
ECHO_PROVIDERS="$ECHO_PROVIDERS $ANTHROPIC"
put_provider_key 0 "$ANTHROPIC" "sk-e2e-echo"
pass 0 "gateway on in $GW_NS, echo on $ECHO_IMAGE, providers $OPENAI/$ANTHROPIC"

# ---------- 1. 상태 API ----------
ready="d['ready_at'] and {p['name'] for p in d['providers'] if p['ready']} >= {'$OPENAI', '$ANTHROPIC'}"
wait_gateway 1 300 "d['switch'] == 'agentgateway' and $ready" "both providers in providers_ready"
pass 1 "switch $(jq_ 'd["switch"]'), applied $(jq_ 'd["applied_rev"]'), $(jq_ 'len(d["pods"])') pods"

# ---------- 2. 화면의 키 저장 경로 ----------
KEY_AT="$(python3 -c 'import datetime; print(datetime.datetime.utcnow().isoformat())')"
put_provider_key 2 "$OPENAI" "sk-e2e-screen-1"
code="$(api POST "/settings/llm-providers/$OPENAI/check")"
[ "$code" = 200 ] && [ "$(jq_ 'd["code"]')" = ok ] || die 2 "connection check right after the key -> $code $(cat "$WORK/body")"
synced="next((p['key_synced_at'] or '' for p in d['providers'] if p['name'] == '$OPENAI'), '') >= '$KEY_AT'"
wait_gateway 2 "$APPLY_WAIT" "$synced and d['applied_rev'] == d['desired_rev']" "the gateway applying the new key"
pass 2 "connection check ok at once; the gateway applied the key, revision $(jq_ 'd["applied_rev"]')"

# ---------- 3. 처음은 직접 경로, 기록 뒤 재적용 한 번에 옮긴다 ----------
new_agent 3 "$AG_A" "$ANTHROPIC" "$OPENAI"
A_ID="$SAVED_ID"
A_NS="$(jq_ 'd["k8s"]["namespace"]')"; A_K8S="$(jq_ 'd["k8s"]["name"]')"; A_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
DEPLOYED="$WORK/deployed-$AG_A.json"
started="$(json_of "$DEPLOYED" 'd["llm_route"]["route"] + " " + str(d["llm_route"]["reason"])')"
[ "$started" = "direct runtime_features_unknown" ] || die 3 "a new agent should start direct (runtime_features_unknown): $started"
[ "$(json_of "$DEPLOYED" 'len(d["llm_secrets"])')" = 2 ] || die 3 "the direct agent has no key copy per provider: $(json_of "$DEPLOYED" 'd["llm_secrets"]')"
# 파드의 init 이 agent.json 을 받으며 이미지의 기능을 알렸다(D65). 다음 재적용 틱이 그 기록으로 옮긴다
reapply_once 3
wait_route 3 "$A_ID" gateway 120
"${K[@]}" -n "$A_NS" rollout status "deploy/$A_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null || die 3 "deploy/$A_K8S did not roll out"
for ((i = 0; i < 24; i++)); do
  copies="$("${K[@]}" -n "$A_NS" get secret -o name | grep -- "^secret/$A_K8S-llm-" || true)"
  [ -z "$copies" ] && break
  sleep 5
done
[ -z "$copies" ] || die 3 "key copies remain after the rollout: $copies"
"${K[@]}" -n "$A_NS" get "deploy/$A_K8S" -o json >"$WORK/deploy-a.json"
[ "$(json_of "$WORK/deploy-a.json" 'all(e.get("valueFrom", {}).get("secretKeyRef", {}).get("key") == "llm_token" for c in d["spec"]["template"]["spec"]["containers"] for e in c.get("env", []) if e["name"].startswith("DAEGAM_LLM_"))')" = True ] \
  || die 3 "the pod env still points at key copies"
[ "$(json_of "$WORK/deploy-a.json" 'd["spec"]["template"]["metadata"]["labels"].get("daegam.devops.ai.kr/llm-route")')" = gateway ] \
  || die 3 "the pod template has no llm-route label"
pass 3 "agent $A_ID moved to the gateway with one reapply; no -llm- Secret, env on llm_token"

# ---------- 4. 메아리는 provider 키만 받는다, 원장 행이 이어진다 ----------
SINCE="$(python3 -c 'import time; print(time.time())')"
turn 4 "$A_ID" "안녕"
echo_after "$SINCE" /v1/messages >"$WORK/echo-4.jsonl"
python3 - "$WORK/echo-4.jsonl" <<'PY' || die 4 "the echo saw a JWT or not the provider key"
import json, sys

lines = [json.loads(x) for x in open(sys.argv[1])]
if not lines:
    sys.exit("no /v1/messages request reached the echo")
for d in lines:
    h = d["headers"]
    if h.get("x-api-key") != "sk-e2e-echo" or "authorization" in h:
        sys.exit(f"headers {sorted(h)}: want only x-api-key sk-e2e-echo")
    if any("eyJ" in str(v) for v in h.values()):
        sys.exit("a JWT reached the echo")
PY
for ((i = 0; i < 24; i++)); do
  code="$(API_OUT="$WORK/trace.json" api GET "/agents/$A_ID/traces/$TURN_TRACE")"
  [ "$code" = 200 ] && [ "$(json_of "$WORK/trace.json" 'any((s.get("ledger") or {}).get("writer") == "gateway" for s in d["spans"] if s["kind"] == "model")')" = True ] && break
  sleep 5
done
[ "$(json_of "$WORK/trace.json" 'any((s.get("ledger") or {}).get("writer") == "gateway" for s in d["spans"] if s["kind"] == "model")')" = True ] \
  || die 4 "no gateway ledger row joined trace $TURN_TRACE"
pass 4 "the echo got only the provider key; the gateway row joined session $TURN_SESSION"

# ---------- 5. 바꾼 키가 재배포 없이 닿는다 ----------
put_provider_key 5 "$ANTHROPIC" "sk-e2e-rotated"
SINCE="$(python3 -c 'import time; print(time.time())')"
seen=""
for ((i = 0; i < APPLY_WAIT / 15; i++)); do
  turn 5 "$A_ID" "새 키"
  seen="$(echo_after "$SINCE" /v1/messages | python3 -c 'import json, sys
print(any(json.loads(x)["headers"].get("x-api-key") == "sk-e2e-rotated" for x in sys.stdin))')"
  [ "$seen" = True ] && break
  sleep 15
done
[ "$seen" = True ] || die 5 "the rotated key did not reach the echo within ${APPLY_WAIT}s"
pass 5 "the rotated key reached the echo without a redeploy"

# ---------- 6. 작은 테넌트 한도 ----------
api GET /settings >/dev/null
LIMITS_SAVED="$WORK/limits-before.json"
json_of "$WORK/body" 'json.dumps({"llm_gateway": d["llm_gateway"]})' >"$LIMITS_SAVED"
before_rev="$(api GET /settings/llm-gateway >/dev/null; jq_ 'd["limits_rev"] or ""')"
code="$(api PATCH /settings "$(printf '{"llm_gateway": {"limits": {"tenants": {"%s": 1}}}}' "$TENANT")")"
[ "$code" = 200 ] || die 6 "PATCH /settings (limits) -> $code"
wait_gateway 6 "$APPLY_WAIT" "(d['limits_rev'] or '') != '$before_rev'" "the new limit config"
# 한도 서비스는 ConfigMap 의 심볼릭 링크가 바뀌면 다시 읽는다(약 60초, F0 8번)
sleep 70
limited=""
for ((i = 0; i < 6; i++)); do
  limited="$(turn_error "$A_ID" "한도")"
  [ "$limited" = llm_gateway_rate_limited ] && break
  sleep 5
done
[ "$limited" = llm_gateway_rate_limited ] || die 6 "a turn over the tenant limit ended with '$limited'"
limited_rev="$(api GET /settings/llm-gateway >/dev/null; jq_ 'd["limits_rev"] or ""')"
code="$(api PATCH /settings "@$LIMITS_SAVED")"
[ "$code" = 200 ] || die 6 "restoring the limits -> $code"
LIMITS_SAVED=""
# 되돌린 한도도 게이트웨이에 닿아야 다음 단계가 429 를 받지 않는다. 한도 서비스가 다시 읽기까지 약 60초다(STEP 7 이 기다린다)
wait_gateway 6 "$APPLY_WAIT" "(d['limits_rev'] or '') != '$limited_rev'" "the restored limit config"
pass 6 "a turn over a 1-token tenant limit is llm_gateway_rate_limited"

# ---------- 7. 회수 ----------
start_pf "$GW_NS" svc/llm-gateway "${GW_PORT}:8080"
PF_GW=$PF_PID
wait_port "$GW_PORT" || die 7 "port-forward to svc/llm-gateway did not open :$GW_PORT"
A_TOKEN="$(secret_field "$A_NS" "$A_SECRET" llm_token)"
# STEP 6 의 한도가 풀릴 때까지 429 다. 한도 서비스가 되돌린 설정을 다시 읽고(약 60초) 분 단위 통이 비어야 한다
code=""
for ((i = 0; i < 36; i++)); do
  code="$(gw_call "$A_TOKEN")"
  [ "$code" = 429 ] || break
  sleep 5
done
[ "$code" = 200 ] || die 7 "a live agent token answers $code: $(cat "$WORK/gw-body")"
new_agent 7 "$AG_B" "$OPENAI"
B_ID="$SAVED_ID"; B_NS="$(jq_ 'd["k8s"]["namespace"]')"; B_K8S="$(jq_ 'd["k8s"]["name"]')"; B_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
reapply_once 7
wait_route 7 "$B_ID" gateway 120
code="$(api POST "/agents/$A_ID/undeploy")"
[ "$code" = 200 ] || die 7 "undeploy $A_ID -> $code"
wait_denied 7 "$A_TOKEN" "the undeployed agent's token"
B_OLD="$(secret_field "$B_NS" "$B_SECRET" llm_token)"
code="$(api POST "/agents/$B_ID/llm-token/rotate" '{"immediate": true}')"
[ "$code" = 200 ] || die 7 "immediate rotation of $B_ID -> $code"
wait_denied 7 "$B_OLD" "the old token after an immediate rotation"
pass 7 "revoked tokens get 401/403 within ${APPLY_WAIT}s (undeploy, immediate rotation)"

# ---------- 8. 게이트웨이가 없을 때 ----------
"${K[@]}" annotate namespace "$GW_NS" "$PAUSE_ANNOTATION=e2e" --overwrite >/dev/null
PAUSED=1
wait_gateway 8 90 "'llm_gateway_paused' in d['warnings']" "the paused warning"
"${K[@]}" -n "$GW_NS" scale deploy/llm-gateway --replicas=0 >/dev/null
SCALED=1
# 파드 템플릿의 라벨은 app.kubernetes.io/name 이다(k8s/templates/llm_gateway.py APP_LABEL). 유예 없이 지우지 않으면
# 파드가 preStop 동안 남고 kube-proxy 가 그리로 보낸다
deleted="$("${K[@]}" -n "$GW_NS" delete pod -l app.kubernetes.io/name=llm-gateway --grace-period=0 --force -o name 2>/dev/null || true)"
[ -n "$deleted" ] || die 8 "no gateway pod was deleted (selector app.kubernetes.io/name=llm-gateway)"
# 유예 없는 삭제는 API 의 파드 객체만 곧바로 지운다. 컨테이너는 kubelet 이 멈출 때까지, 서비스의 규칙은 kube-proxy 가
# 따라올 때까지 남아서 그사이의 턴은 게이트웨이를 거쳐 끝나거나 끊긴 연결의 오류로 끝난다. 그런 턴은 다시 보내고, 시간은
# 마지막 턴만 잰다
for ((i = 0; i < GONE_WAIT / 2; i++)); do
  start_ms="$(now_ms)"
  unreachable="$(turn_error "$B_ID" "게이트웨이 없음")"
  took=$(( ($(now_ms) - start_ms) / 1000 ))
  [ "$unreachable" != llm_gateway_unreachable ] || break
  sleep 2
done
[ "$unreachable" = llm_gateway_unreachable ] || {
  cp "$WORK/sse" "$WORK/body"
  die 8 "a turn without gateway pods ended with '$unreachable' (events $(sse_ 'names'))"
}
[ "$took" -le "$UNREACHABLE_WITHIN" ] || die 8 "llm_gateway_unreachable came after ${took}s (want ${UNREACHABLE_WITHIN}s)"
"${K[@]}" annotate namespace "$GW_NS" "$PAUSE_ANNOTATION-" >/dev/null
PAUSED=0
# 0 으로 줄인 Deployment 는 동기화가 레플리카를 되돌리기 전에도 rollout status 가 곧바로 성공이다. 동기화가 되돌린
# 레플리카가 모두 준비될 때까지 본다
back=""
for ((i = 0; i < 36; i++)); do
  back="$("${K[@]}" -n "$GW_NS" get deploy/llm-gateway -o jsonpath='{.spec.replicas} {.status.readyReplicas}' 2>/dev/null || true)"
  read -r gw_want gw_ready <<<"$back"
  [ "${gw_want:-0}" -ge 1 ] && [ "${gw_ready:-0}" = "$gw_want" ] && break
  sleep 5
done
{ [ "${gw_want:-0}" -ge 1 ] && [ "${gw_ready:-0}" = "$gw_want" ]; } || die 8 "the sync did not bring the gateway back within 180s (replicas and ready: '$back')"
SCALED=0
turn 8 "$B_ID" "다시 됨"
pass 8 "llm_gateway_unreachable in ${took}s without pods; the sync restored them"

# ---------- 9. 비서와 인덱서 ----------
api GET /settings >/dev/null
ASSISTANT_SAVED="$WORK/assistant-before.json"
json_of "$WORK/body" 'json.dumps({"assistant": d["assistant"]})' >"$ASSISTANT_SAVED"
code="$(api PATCH /settings "$(json_of "$WORK/body" 'json.dumps({"assistant": dict(d["assistant"], enabled=True)})')")"
[ "$code" = 200 ] || die 9 "enable the assistant -> $code"
calls="$(gateway_calls)"
code="$(api POST /assistant/sessions '{"title": "llm gateway e2e"}')"
[ "$code" = 201 ] || die 9 "POST /assistant/sessions -> $code"
SID="$(jq_ 'd["id"]')"
code="$(chat "/assistant/sessions/$SID/turns" "$(printf '{"message": "안녕", "provider": "%s"}' "$OPENAI")")"
[ "$code" = 200 ] || die 9 "assistant turn -> $code"
wait_more_gateway_calls 9 "$calls" "the assistant turn"
api_delete "/assistant/sessions/$SID"
if [ "$TARGET" = orbstack ]; then
  lab_database 9 postgres "$DS_NAME" "llm gateway e2e Postgres"
else
  install_postgres_database 9 "$DS_NAME" "$PLATFORM_NS" "$HOME_ID"
fi
DS_ID="$DATA_SERVICE_ID"
dims="$(sed -n 's/^DEFAULT_DIMENSIONS = \([0-9][0-9]*\)$/\1/p' "$ROOT/deploy/dev/lab/llm_echo.py")"
kb_create_fresh 9 "$KB_NAME" "$(printf '{"name": "%s", "data_service_id": "%s", "embedding": {"provider": "%s", "dimensions": %s}}' "$KB_NAME" "$DS_ID" "$OPENAI" "$dims")"
calls="$(gateway_calls)"
printf '# 환불 규정\n\n구매 후 7일 안에는 전액 환불합니다.\n' >"$WORK/faq.md"
code="$(kb_upload "$KB_ID" "$WORK/faq.md")"
[ "$code" = 201 ] || die 9 "upload faq.md -> $code"
kb_wait_indexed 9 "$KB_ID" "$(jq_ 'd["id"]')"
wait_more_gateway_calls 9 "$calls" "indexing one document"
pass 9 "an assistant turn and one indexed document left gateway rows"

# ---------- 10. 끄기 ----------
[ "$GATEWAY_WINDOW" = 1 ] || { config_window_plan DAEGAM_LLM_GATEWAY=off || die 10 "reading ConfigMap failed"; GATEWAY_WINDOW=1; }
"${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p '{"data": {"DAEGAM_LLM_GATEWAY": "off"}}' >/dev/null \
  || die 10 "patching DAEGAM_LLM_GATEWAY=off failed"
restart_platform || die 10 "the platform did not roll after turning the gateway off"
[ "$TARGET" = install ] || { restart_pf "$API_PORT"; wait_http "$BASE/healthz" || die 10 "the platform does not answer after the roll"; }
# 게이트웨이 경로 파드의 라벨이다(k8s/templates/llm_gateway.py LLM_ROUTE_LABEL)
deleted="$("${K[@]}" -n "$B_NS" delete pod -l "daegam.devops.ai.kr/llm-route=gateway" --wait=false -o name 2>/dev/null || true)"
[ -n "$deleted" ] || die 10 "no gateway-route pod of $B_ID was deleted (selector daegam.devops.ai.kr/llm-route=gateway)"
turn 10 "$B_ID" "끈 뒤"
wait_route 10 "$B_ID" direct 180
[ "$(route_of "$B_ID")" = "direct gateway_off" ] || die 10 "after turning off: $(route_of "$B_ID")"
[ -n "$("${K[@]}" -n "$B_NS" get secret -o name | grep -- "^secret/$B_K8S-llm-" || true)" ] \
  || die 10 "the agent moved back to the direct route without key copies"
for ((i = 0; i < 36; i++)); do
  held="$("${K[@]}" -n "$B_NS" get secret "$B_SECRET" -o 'jsonpath={.data.llm_token}' 2>/dev/null || true)"
  [ -z "$held" ] && break
  sleep 5
done
[ -z "$held" ] || die 10 "llm_token is still in $B_SECRET after the rollout"
for ((i = 0; i < 36; i++)); do
  "${K[@]}" -n "$GW_NS" get deploy/llm-gateway >/dev/null 2>&1 || break
  sleep 5
done
! "${K[@]}" -n "$GW_NS" get deploy/llm-gateway >/dev/null 2>&1 || die 10 "the gateway objects were not cleared"
turn 10 "$B_ID" "직접 경로"
pass 10 "turned off: chat kept working, the agent is direct with copies, llm_token went, the gateway objects are gone"

finish
