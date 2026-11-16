#!/usr/bin/env bash
# P37 (D62) 관측과 비용 e2e. 진짜 HTTP 를 지나는 OpenAI 호환 메아리 서버(deploy/dev/lab/llm_echo.py)로 에이전트와 Knowledge
# Base 를 돌리고, 런타임이 밀어 보낸 이벤트, 상관 헤더, 원장, 지표, 트레이스, 게이트웨이 사용량 수집을 본다. stub provider 는
# 쓰지 않는다. HTTP 호출이 없어 상관 헤더·stream_usage·캐시 토큰 읽기를 지나지 않고, 설치본은 그것을 막는다.
#
#   make e2e-telemetry                                orbstack (deploy-local·lab-up 이후, header 모드)
#   make e2e-telemetry E2E_TARGET=install E2E_INSTALL_USER=<관리자>
#                                                     설치본(P85 10절). Tool 이 드는 단계(승인 재개, Tool span)는 lab 의 calculator
#                                                     가 없어 조건부로 건너뛴다
#
# P37 과 P38 의 e2e 는 함께 돌리지 않는다. 둘 다 메아리 네임스페이스와 플랫폼의 게이트웨이 설정을 바꾼다. EXIT 트랩이 바꾼
# 설정(가격표, OTLP, DAEGAM_LLM_GATEWAY_URL)을 되돌리고 만든 것을 걷는다. 단계는 다음과 같다:
#   STEP 0   메아리 네임스페이스 daegam-e2e-llm(라벨 daegam.devops.ai.kr/e2e=llm-echo, 라벨 없는 같은 이름이 있으면 멈춘다)에 런타임
#            이미지로 메아리를 띄우고 Service llm-echo·llm-echo-gw 를 둔다. 가짜 키의 provider e2e-echo·e2e-echo-gw, echo-*
#            가격 규칙(캐시 읽기 포함), Database(orbstack 은 lab Postgres, 설치본은 플랫폼 네임스페이스의 pgvector Postgres),
#            e2e-echo 임베딩의 Knowledge Base 와 문서 하나를 둔다
#   STEP 1   e2e-echo 의 에이전트(승인 Tool, 서브에이전트, 그 Knowledge Base, 본문 기록)를 파드 둘로 배포한다
#   STEP 2   세 턴(승인 뒤 재개하는 Tool, 서브에이전트, 검색)의 done 에 trace_id 가 있고 30초 안에 트레이스가 보인다. 재개한
#            턴은 실행이 둘이고 전사의 done 이 trace_id 를 든다. done 에서 트레이스가 보이기까지의 시간을 적는다
#   STEP 3   메아리가 받은 traceparent 가 그 턴들의 model_call 호출 id 와 하나씩 맞고(검색 임베딩 포함) 겹치는 id 가 없다.
#            원장의 토큰이 메아리가 보낸 사용량과 같고(캐시 읽기, 추론 포함) 검색 임베딩 행은 estimated 다
#   STEP 4   에이전트의 행은 writer runtime, route direct 이고 금액이 토큰 × 가격이다. 인덱서의 행이 원장에 있다. 규칙을 바꾸고
#            다시 계산하면 사용량 합의 금액이 바뀐다
#   STEP 5   두 파드가 agent_telemetry 에 있고 이벤트가 모두 source push 다. 지표의 턴 수가 두 파드의 합이다. 파드 하나를
#            지운 뒤에도 새 파드의 턴이 들어온다. 3초 뒤 끊은 {"sleep": 20} 턴은 끝까지 가 그 시도의 turn_end{done} 을
#            밀어 보내고(D92, P78), 중지 API 로 멈춘 턴이 turn_end{cancelled} 와 model_call{cancelled} 를 남긴다
#   STEP 6   게이트웨이 경로: 플랫폼을 DAEGAM_LLM_GATEWAY_URL=llm-echo-gw 로 굴리고, e2e-echo-gw 의 Knowledge Base 에 올린
#            문서는 행을 남기지 않는다. 메아리 로그로 만든 OTLP log JSON 을 서비스 토큰(svc_token.py --audience
#            daegam-telemetry)으로 두 번 보내면 배치마다 한 행(writer gateway, knowledge_indexer)이고 다시 보낸 것은 늘지
#            않는다. 다른 테넌트를 말하는 기록 하나는 이어지지 않고 join_mismatch 가 하나 는다
#   STEP 7   틀린 토큰은 401, 1 MiB 를 넘는 본문은 413, 인스턴스 토큰의 feedback 은 reserved_event 다. 설치본이면 viewer 가 남의
#            트레이스·이벤트 목록·사용량에 403 이고 제 트레이스에는 200 이다
#   STEP 8   (orbstack) OTLP 를 메아리 네임스페이스의 OTel Collector(debug exporter)로 켜면 세 트레이스가 invoke_agent·chat·
#            execute_tool 로 보인다
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
API_PORT="${E2E_API_PORT:-$PORT_TELEMETRY_API}"
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-telemetry}"; BASE="http://127.0.0.1:${API_PORT}" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"; BASE="https://${DAEGAM_HOST:-}" ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
API="$BASE/api"
PROVIDER="e2e-echo"; PROVIDER_GW="e2e-echo-gw"
DS_NAME="pg-telemetry-e2e"; KB_NAME="faq-telemetry-e2e"; KB_GW_NAME="faq-telemetry-gw-e2e"; KB_TOOL="search_faq"
BP_NAME="calc-telemetry"; MS_NAME="calc-telemetry-e2e"
AG_NAME="telemetry-e2e"; SUB_NAME="calc"
TRACE_WAIT=30
OTLP_COLLECTOR="otel-collector"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-telemetry.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()
PF_API=""
HOME_ID=""; DS_ID=""; KB_ID=""; KB_GW_ID=""; AG_ID=""; AG_NS=""; AG_K8S=""
PRICING_SAVED=""; OTLP_SAVED=""; GATEWAY_ROLLED=0
SESSIONS=()

. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"
. "$HERE/lib/e2e_report.sh"
. "$HERE/lib/e2e_api.sh"
. "$HERE/lib/e2e_sse.sh"
. "$HERE/lib/e2e_lab.sh"
. "$HERE/lib/e2e_calc.sh"
. "$HERE/lib/e2e_control_plane.sh"
. "$HERE/lib/e2e_local_auth.sh"
# 메아리 서버와 그 provider(echo_up·echo_provider·echo_down, ECHO_NS·ECHO_URL)는 여기에 있다
. "$HERE/lib/e2e_llm_echo.sh"
ECHO_GW_URL="http://llm-echo-gw.${ECHO_NS}.svc.cluster.local:8080"

cleanup() {
  bg_stop
  # 7단계의 viewer_trace 안에서 멈추면 그 함수의 local AUTH(viewer)가 여기까지 보인다. 걷기는 관리자로 한다
  [ "$TARGET" = orbstack ] || local AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  [ -z "$AG_ID" ] || api_delete "/agents/$AG_ID"
  [ -z "$KB_ID" ] || api_delete "/knowledge-bases/$KB_ID"
  [ -z "$KB_GW_ID" ] || api_delete "/knowledge-bases/$KB_GW_ID"
  [ "$TARGET" != orbstack ] || calc_teardown
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  if [ -n "$PRICING_SAVED" ]; then
    api GET /settings/pricing >/dev/null
    # dict(...) 로 쓴다. bash 3.2 는 겹친 명령 치환 안의 {…, …} 를 중괄호 확장으로 쪼갠다
    api PUT /settings/pricing "$(json_of "$WORK/body" "json.dumps(dict(expected_version=d['version'], rules=json.loads(open('$PRICING_SAVED').read())))")" >/dev/null \
      || echo "!! price rules not restored: put back $PRICING_SAVED by hand" >&2
  fi
  [ -z "$OTLP_SAVED" ] || api PUT /settings/observability/otlp "@$OTLP_SAVED" >/dev/null || true
  echo_down
  if [ "$GATEWAY_ROLLED" = 1 ]; then
    "${K[@]}" -n "$PLATFORM_NS" set env deploy/daegam-platform DAEGAM_LLM_GATEWAY_URL- >/dev/null || true
    "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=240s >/dev/null || true
  fi
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# --- 도우미 -------------------------------------------------------------------------------------------------
# echo_lines PATH -> 메아리 파드 로그에서 그 경로의 JSON 줄(traceparent·model·usage)을 찍는다
echo_lines() {
  "${K[@]}" -n "$ECHO_NS" logs deploy/llm-echo --tail=-1 2>/dev/null \
    | python3 -c 'import json, sys
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if d.get("llm_echo") == sys.argv[1]:
        print(json.dumps(d))' "$1"
}
# trace_of STEP TRACE_ID -> 30초 안에 그 트레이스를 $WORK/trace.json 에 둔다. 걸린 초를 TRACE_SECONDS 에 남긴다
TRACE_SECONDS=0
trace_of() {
  local code i
  for ((i = 0; i < TRACE_WAIT; i++)); do
    code="$(API_OUT="$WORK/trace.json" api GET "/agents/$AG_ID/traces/$2")"
    if [ "$code" = 200 ] && [ "$(json_of "$WORK/trace.json" 'd["root"] is not None and d["root"]["status"] != "running"')" = True ]; then
      TRACE_SECONDS=$i
      return 0
    fi
    sleep 1
  done
  die "$1" "trace $2 not visible after ${TRACE_WAIT}s (last $code)"
}
turn() { # STEP TEXT [SESSION_ID] -> chat 을 보내고 done 이나 interrupt 로 끝나야 한다
  local code
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$2" "${3:-}")")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die "$1" "chat -> $code"; }
  case "$(sse_ 'names[-1]')" in done | interrupt) ;; *) cp "$WORK/sse" "$WORK/body"; die "$1" "turn ended with $(sse_ 'names')" ;; esac
}
has_tool() { [ "$TARGET" = orbstack ]; }

# --- 연결 -------------------------------------------------------------------------------------------------------
# 설치본은 e2e 관리자로 공개 주소에 로그인하고, orbstack 은 port-forward 한다 (lib/e2e_local_auth.sh)
platform_connect 0
code="$(api GET /clusters)"
[ "$code" = 200 ] || die 0 "GET /clusters -> $code"
HOME_ID="$(jq_ 'next((c["id"] for c in d if c["name"] == "home" and c["status"]["preflight"]["ok"]), "")')"
[ -n "$HOME_ID" ] || die 0 "cluster home missing or preflight not ok"
# 메아리는 홈 클러스터가 Agent 에 쓰는 이미지로 띄운다
echo_agent_image

# ---------- 0. 메아리, provider, 가격, Database, Knowledge Base ----------
echo_up 0 llm-echo-gw
echo_provider 0 "$PROVIDER"
echo_provider 0 "$PROVIDER_GW" "$ECHO_GW_URL"
api GET /settings/pricing >/dev/null
PRICING_SAVED="$WORK/pricing-before.json"
json_of "$WORK/body" 'json.dumps(d["rules"])' >"$PRICING_SAVED"
rules="$(json_of "$WORK/body" 'json.dumps({"expected_version": d["version"], "rules": [r for r in d["rules"] if not r["model"].startswith("echo-")] + [
  {"provider_kind": "openai", "provider_name": None, "model": "echo-*", "input": 2.0, "output": 8.0, "cache_read": 0.5, "effective_from": "2020-01-01"},
]})')"
code="$(api PUT /settings/pricing "$rules")"
[ "$code" = 200 ] || die 0 "PUT /settings/pricing -> $code"
if [ "$TARGET" = orbstack ]; then
  lab_database 0 postgres "$DS_NAME" "telemetry e2e Postgres"
else
  install_postgres_database 0 "$DS_NAME" "$PLATFORM_NS" "$HOME_ID"
fi
DS_ID="$DATA_SERVICE_ID"
# 메아리 서버의 임베딩 차원이다. 그 파일의 DEFAULT_DIMENSIONS 가 한 자리이고, echo-embed 는 플랫폼이 아는 모델이 아니라
# Knowledge Base 가 차원을 적어야 한다(dimensions_required)
ECHO_DIMENSIONS="$(sed -n 's/^DEFAULT_DIMENSIONS = \([0-9][0-9]*\)$/\1/p' "$ROOT/deploy/dev/lab/llm_echo.py")"
[ -n "$ECHO_DIMENSIONS" ] || die 0 "DEFAULT_DIMENSIONS not found in deploy/dev/lab/llm_echo.py"
kb_body() {
  printf '{"name": "%s", "data_service_id": "%s", "embedding": {"provider": "%s", "dimensions": %s}}' \
    "$1" "$DS_ID" "$2" "$ECHO_DIMENSIONS"
}
kb_create_fresh 0 "$KB_NAME" "$(kb_body "$KB_NAME" "$PROVIDER")"
printf '# 환불 규정\n\n구매 후 7일 안에는 전액 환불합니다.\n' >"$WORK/faq.md"
code="$(kb_upload "$KB_ID" "$WORK/faq.md")"
[ "$code" = 201 ] || die 0 "upload faq.md -> $code"
DOC_ID="$(jq_ 'd["id"]')"
kb_wait_indexed 0 "$KB_ID" "$DOC_ID"
pass 0 "echo on $ECHO_IMAGE in $ECHO_NS, providers $PROVIDER/$PROVIDER_GW, echo-* prices, DB $DS_ID, KB $KB_ID indexed"

# ---------- 1. 에이전트 ----------
MS_ID=""
if has_tool; then
  calc_mcp_server 1 "$BP_NAME" "$MS_NAME" '{}'
  MS_ID="$CALC_MS_ID"
fi
api GET /agents/templates >/dev/null
# lib/agent_body.py 가 템플릿을 채운다. Add 는 승인 뒤에만 부르고, 서브에이전트와 knowledge 노드를 더하며 본문을 남긴다
python3 "$HERE/lib/agent_body.py" "$WORK/body" "$WORK/agent.json" --name "$AG_NAME" --display "관측 e2e" \
  --provider "$PROVIDER" --ms-id "$MS_ID" --ds-id "$DS_ID" --kb-id "$KB_ID" --kb-tool "$KB_TOOL" --sub-name "$SUB_NAME" \
  --approve --record-bodies
save_doc 1 /agents "$AG_NAME" "@$WORK/agent.json"
AG_ID="$SAVED_ID"
deploy_ready 1 /agents "$AG_ID" '{"replicas": 2}'
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"
AG_TOKEN_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null || die 1 "deploy/$AG_K8S did not roll out to 2"
pass 1 "agent $AG_ID ($AG_NS/$AG_K8S) on $PROVIDER, 2 replicas"

# ---------- 2. 세 턴과 그 트레이스 ----------
TRACES=()
if has_tool; then
  turn 2 "$(stub_call Add '{"a": 3, "b": 4}')"
  [ "$(sse_ 'names[-1]')" = interrupt ] || die 2 "the approval tool did not interrupt: $(sse_ 'names')"
  S_TOOL="$(sse_ 'first("interrupt")["session_id"]')"; SESSIONS+=("$S_TOOL")
  code="$(chat "/agents/$AG_ID/sessions/$S_TOOL/decisions" '{"decisions": [{"type": "approve"}]}')"
  [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || die 2 "resume after approval -> $code $(sse_ 'names')"
  TRACES+=("$(sse_ 'first("done")["trace_id"]')")
  # 런타임은 이벤트를 2초마다 밀어 보낸다. 재개한 실행의 turn_end 가 들어올 때까지 기다린다
  runs=0
  for ((i = 0; i < TRACE_WAIT; i++)); do
    code="$(api GET "/agents/$AG_ID/sessions/$S_TOOL/turns")"
    [ "$code" = 200 ] && runs="$(jq_ 'len(d["turns"][0]["runs"]) if d["turns"] else 0')"
    [ "$runs" = 2 ] && break
    sleep 1
  done
  [ "$runs" = 2 ] || die 2 "the resumed turn should have two runs after ${TRACE_WAIT}s: $code $(cat "$WORK/body")"
else
  cond_skip 2a "no lab calculator on the install: the approval resume is not exercised"
fi
turn 2 "$(stub_call task "$(printf '{"description": "더해 줘", "subagent_type": "%s"}' "$SUB_NAME")")"
SESSIONS+=("$(sse_ 'first("done")["session_id"]')"); TRACES+=("$(sse_ 'first("done")["trace_id"]')")
turn 2 "$(stub_call "$KB_TOOL" '{"query": "환불"}')"
S_SEARCH="$(sse_ 'first("done")["session_id"]')"; SESSIONS+=("$S_SEARCH"); TRACES+=("$(sse_ 'first("done")["trace_id"]')")
WAITS=()
for t in "${TRACES[@]}"; do
  [[ "$t" =~ ^[0-9a-f]{32}$ ]] || die 2 "done has no trace_id ($t)"
  trace_of 2 "$t"
  WAITS+=("${TRACE_SECONDS}s")
  [ "$(json_of "$WORK/trace.json" 'any(s["kind"] == "model" for s in d["spans"])')" = True ] || die 2 "trace $t has no model span"
done
code="$(api GET "/agents/$AG_ID/sessions/$S_SEARCH")"
# bash 3.2 는 음수 첨자를 모른다
last_trace="${TRACES[${#TRACES[@]}-1]}"
[ "$(jq_ "[x.get('trace_id') for x in d['transcript']['items'] if x['kind'] == 'done']")" = "['$last_trace']" ] \
  || die 2 "transcript done item lost its trace_id"
pass 2 "${#TRACES[@]} traces visible after ${WAITS[*]}; transcript keeps trace_id"

# ---------- 3. 상관 헤더와 원장 토큰 ----------
echo_lines /v1/chat/completions >"$WORK/echo-chat.jsonl"
echo_lines /v1/embeddings >"$WORK/echo-embed.jsonl"
for t in "${TRACES[@]}"; do
  API_OUT="$WORK/trace-$t.json" api GET "/agents/$AG_ID/traces/$t" >/dev/null
done
python3 - "$WORK" "${TRACES[@]}" <<'PY' || die 3 "correlation or tokens do not match (see above)"
import json, sys
from pathlib import Path

work, traces = Path(sys.argv[1]), sys.argv[2:]
echo = [json.loads(line) for name in ("echo-chat.jsonl", "echo-embed.jsonl") for line in (work / name).read_text().splitlines()]
by_call = {}
for line in echo:
    parts = (line.get("traceparent") or "").split("-")
    if len(parts) == 4:
        by_call.setdefault(f"{parts[1]}-{parts[2]}", []).append(line)
bad = [c for c, lines in by_call.items() if len(lines) > 1]
for t in traces:
    trace = json.loads((work / f"trace-{t}.json").read_text())
    for span in trace["spans"]:
        if span["kind"] not in ("model", "embeddings"):
            continue
        call = f"{t}-{span['span_id']}"
        lines = by_call.get(call)
        if not lines:
            bad.append(f"no echo request for {call}")
            continue
        sent, ledger = lines[0]["usage"], span.get("ledger") or {}
        if span["kind"] == "model":
            got = ledger.get("usage") or {}
            want = {"input_tokens": sent["prompt_tokens"], "output_tokens": sent["completion_tokens"],
                    "cache_read_tokens": sent["prompt_tokens_details"]["cached_tokens"],
                    "reasoning_tokens": sent["completion_tokens_details"]["reasoning_tokens"]}
            if {k: got.get(k) for k in want} != want:
                bad.append(f"{call}: ledger {got} != echo {want}")
        elif not ledger.get("estimated"):
            bad.append(f"{call}: the search embedding row is not estimated")
for b in bad:
    print("  " + b)
sys.exit(1 if bad else 0)
PY
pass 3 "every model and embedding call of ${#TRACES[@]} traces has one echo request with the same tokens"

# ---------- 4. 쓰는 쪽과 금액, 다시 계산 ----------
python3 - "$WORK" "${TRACES[@]}" <<'PY' || die 4 "writer, route or cost mismatch"
import json, sys
from pathlib import Path

work, traces = Path(sys.argv[1]), sys.argv[2:]
bad = []
for t in traces:
    for span in json.loads((work / f"trace-{t}.json").read_text())["spans"]:
        ledger = span.get("ledger")
        if span["kind"] != "model" or ledger is None:
            continue
        if (ledger["writer"], ledger["route"]) != ("runtime", "direct"):
            bad.append(f"{span['span_id']}: {ledger['writer']}/{ledger['route']}")
        u = ledger["usage"]
        want = ((u["input_tokens"] - u["cache_read_tokens"]) * 2.0 + u["cache_read_tokens"] * 0.5 + u["output_tokens"] * 8.0) / 1e6
        if abs((span["cost_usd"] or 0) - want) > 1e-9:
            bad.append(f"{span['span_id']}: cost {span['cost_usd']} != {want}")
for b in bad:
    print("  " + b)
sys.exit(1 if bad else 0)
PY
code="$(api GET "/admin/usage?group=source&tenant=$TENANT")"
[ "$code" = 200 ] || die 4 "GET /admin/usage -> $code"
BEFORE="$(jq_ 'round(d["totals"]["cost_usd"], 9)')"
api GET /settings/pricing >/dev/null
code="$(api PUT /settings/pricing "$(json_of "$WORK/body" 'json.dumps({"expected_version": d["version"], "rules": [dict(r, output=r["output"] * 2) if r["model"] == "echo-*" else r for r in d["rules"]]})')")"
[ "$code" = 200 ] || die 4 "PUT /settings/pricing (doubled) -> $code"
since="$(date -u -v-1d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d '-1 day' +%Y-%m-%dT00:00:00Z)"
until_="$(date -u -v+1d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d '+1 day' +%Y-%m-%dT00:00:00Z)"
code="$(api POST /usage/reprice "$(printf '{"since": "%s", "until": "%s"}' "$since" "$until_")")"
[ "$code" = 202 ] || die 4 "POST /usage/reprice -> $code"
AFTER="$BEFORE"
for ((i = 0; i < 60; i++)); do
  sleep 5
  api GET "/admin/usage?group=source&tenant=$TENANT" >/dev/null
  AFTER="$(jq_ 'round(d["totals"]["cost_usd"], 9)')"
  [ "$AFTER" = "$BEFORE" ] || break
done
[ "$AFTER" != "$BEFORE" ] || die 4 "reprice did not change the cost ($BEFORE)"
INDEXER="$(jq_ 'sum(r["calls"] for r in d["rows"] if r.get("source_kind") == "knowledge_indexer")')"
[ "$INDEXER" -ge 1 ] || die 4 "no indexer rows in the ledger"
pass 4 "agent rows runtime/direct at tokens × prices; indexer rows $INDEXER; reprice $BEFORE -> $AFTER"

# ---------- 5. 파드 둘, 밀어 보내기, 중지 ----------
code="$(api GET "/agents/$AG_ID/metrics")"
[ "$code" = 200 ] || die 5 "GET metrics -> $code"
[ "$(jq_ 'd["telemetry"]["mode"] == "push" and len(d["telemetry"]["pods"]) >= 2')" = True ] \
  || die 5 "both pods should push: $(jq_ 'd["telemetry"]')"
code="$(api GET "/agents/$AG_ID/events?limit=1000")"
[ "$(jq_ 'sorted({e.get("source") for e in d})')" = "['push']" ] || die 5 "events not all pushed: $(jq_ 'sorted({e.get("source") for e in d}, key=str)')"
victim="$(agent_pods "$AG_NS" "$AG_ID" | head -1 || true)"
[ -z "$victim" ] || "${K[@]}" -n "$AG_NS" delete pod "$victim" --wait=false >/dev/null
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null || die 5 "agent did not settle after deleting $victim"
# 글만 보내면 스텁이 첫 MCP Tool(승인이 걸린 Add)을 불러 승인 대기로 끝난다. 승인이 없는 검색 Tool 을 부른다
turn 5 "$(stub_call "$KB_TOOL" '{"query": "환불"}')"
after_trace="$(sse_ 'first("done")["trace_id"]')"; SESSIONS+=("$(sse_ 'first("done")["session_id"]')")
[[ "$after_trace" =~ ^[0-9a-f]{32}$ ]] || die 5 "the turn after the pod was replaced did not end with done: $(sse_ 'names')"
trace_of 5 "$after_trace"
# 끊은 턴은 끝까지 가 그 시도의 turn_end{done} 을 밀어 보낸다 (D92, P78). 시도의 id 는 응답 머리에서 읽는다
SSE_OUT="$WORK/sleep.sse" CHAT_MAX_TIME=3 chat "/agents/$AG_ID/chat" "$(msg '{"sleep": 20}')" -D "$WORK/sleep.hdr" >/dev/null || true
cut_turn="$(hdr x-daegam-turn-id "$WORK/sleep.hdr")"; cut_sid="$(hdr x-daegam-session-id "$WORK/sleep.hdr")"
{ [ -n "$cut_turn" ] && [ -n "$cut_sid" ]; } || die 5 "the cut turn's response lacks X-Daegam-Turn-Id or X-Daegam-Session-Id: $(cat "$WORK/sleep.hdr" 2>/dev/null)"
SESSIONS+=("$cut_sid")
# 중지 API 로 멈춘 턴은 cancelled 를 남긴다. 다른 세션의 같은 턴을 뒤에서 열고 2초 뒤 멈춘다
chat_bg "/agents/$AG_ID/chat" "$(msg '{"sleep": 20}')"
stop_sid="$(bg_session_id)"
[ -n "$stop_sid" ] || { bg_stop; die 5 "the turn to stop sent no X-Daegam-Session-Id: $(cat "$WORK/bg.hdr" 2>/dev/null)"; }
SESSIONS+=("$stop_sid")
sleep 2
cancel_ms="$(now_ms)"
code="$(api POST "/agents/$AG_ID/sessions/$stop_sid/cancel")"
[ "$code" = 202 ] || { bg_stop; die 5 "POST /sessions/$stop_sid/cancel -> $code $(cat "$WORK/body")"; }
expect_cancelled_stream 5 "$cancel_ms"
cut_status=""; cancelled=""
for ((i = 0; i < 30; i++)); do
  api GET "/agents/$AG_ID/events?event=turn_end&session_id=$cut_sid&limit=20" >/dev/null
  cut_status="$(jq_ "next((e.get('status') for e in d if e.get('turn_id') == '$cut_turn'), '')")"
  api GET "/agents/$AG_ID/events?event=turn_end&session_id=$stop_sid&limit=20" >/dev/null
  cancelled="$(jq_ 'next((e["trace_id"] for e in d if e.get("status") == "cancelled" and e.get("trace_id")), "")')"
  [ -z "$cut_status" ] || [ -z "$cancelled" ] || break
  sleep 2
done
[ "$cut_status" = "done" ] || die 5 "the cut turn $cut_turn should run on to turn_end{done}, got '$cut_status'; stream $(head -c 600 "$WORK/sleep.sse" 2>/dev/null)"
[ -n "$cancelled" ] || die 5 "the stopped turn left no turn_end{cancelled} with a trace: turn_end $(jq_ '[(e.get("status"), e.get("trace_id"), e.get("ts")) for e in d]')"
api GET "/agents/$AG_ID/events?event=model_call&trace_id=$cancelled" >/dev/null
[ "$(jq_ 'any(e.get("status") == "cancelled" for e in d)')" = True ] || die 5 "no model_call{cancelled} in trace $cancelled"
pass 5 "two pushing pods, all events push, $victim replaced and turns keep arriving, cut turn $cut_turn ran on to done, stopped turn $cancelled cancelled"

# ---------- 6. 게이트웨이 경로와 사용량 수집 ----------
"${K[@]}" -n "$PLATFORM_NS" set env deploy/daegam-platform "DAEGAM_LLM_GATEWAY_URL=$ECHO_GW_URL" >/dev/null
GATEWAY_ROLLED=1
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=240s >/dev/null || die 6 "platform did not roll"
wait_no_terminating daegam-platform || die 6 "old platform pods did not go"
[ "$TARGET" = install ] || { stop_pf "$PF_API" "$API_PORT"; start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID; wait_port "$API_PORT" || die 6 "port-forward did not reopen"; }
KB_MAIN_ID="$KB_ID"
kb_create_fresh 6 "$KB_GW_NAME" "$(kb_body "$KB_GW_NAME" "$PROVIDER_GW")"
KB_GW_ID="$KB_ID"; KB_ID="$KB_MAIN_ID"
GW_SINCE="$(python3 -c 'import time; print(time.time())')"
# 문서 둘이라 임베딩 배치가 적어도 둘이다. 하나는 다른 테넌트의 기록으로 보낸다
printf '# 배송

주문 뒤 이틀 안에 보냅니다.
' >"$WORK/shipping.md"
for f in faq.md shipping.md; do
  code="$(kb_upload "$KB_GW_ID" "$WORK/$f")"
  [ "$code" = 201 ] || die 6 "upload $f to the gateway KB -> $code"
  kb_wait_indexed 6 "$KB_GW_ID" "$(jq_ 'd["id"]')"
done
echo_lines /v1/embeddings >"$WORK/echo-embed-gw.jsonl"
python3 - "$TENANT" "$WORK/otlp.json" "$WORK/otlp-foreign.json" "$WORK/echo-embed-gw.jsonl" "$GW_SINCE" <<'PY'
import json, sys

tenant, out, foreign, lines, since = sys.argv[1:6]
records = []
for line in open(lines):
    d = json.loads(line)
    # 게이트웨이 경로로 바꾼 뒤 올린 문서의 배치만이다. 앞의 호출은 이미 직접 경로의 행이 있다
    if not d.get("traceparent") or d["ts"] < float(since):
        continue
    records.append({"timeUnixNano": str(int(d["ts"] * 1e9)), "attributes": [
        {"key": k, "value": v} for k, v in (
            ("gen_ai.operation.name", {"stringValue": "embeddings"}),
            ("gen_ai.provider.name", {"stringValue": "openai"}),
            ("gen_ai.request.model", {"stringValue": d["model"]}),
            ("gen_ai.usage.input_tokens", {"intValue": str(d["usage"]["prompt_tokens"])}),
            ("daegam.traceparent", {"stringValue": d["traceparent"]}),
            ("daegam.caller.tenant", {"stringValue": tenant}),
            ("daegam.caller.kind", {"stringValue": "platform"}),
            ("daegam.caller.id", {"stringValue": "knowledge_indexer"}),
            ("daegam.provider", {"stringValue": "e2e-echo-gw"}),
            ("http.request.id", {"stringValue": d["traceparent"]}),
        )]})
if len(records) < 2:
    sys.exit("the gateway KB upload left fewer than two embedding batches in the echo log")


def wrap(rs):
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": rs}]}]}


# 마지막 배치는 다른 테넌트를 말하게 해 따로 보낸다. 잇기 문서의 테넌트와 달라 이어지지 않아야 한다
*mine, last = records
for a in last["attributes"]:
    if a["key"] == "daegam.caller.tenant":
        a["value"] = {"stringValue": "someone-else"}
json.dump(wrap(mine), open(out, "w"))
json.dump(wrap([last]), open(foreign, "w"))
PY
GW_TOKEN="$(uv run --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/svc_token.py" --context "$CTX" --namespace "$PLATFORM_NS" \
  --audience daegam-telemetry --sub "svc:llm-gateway:$HOME_ID")"
post_otlp() { curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "$API/telemetry/v1/logs" -H "Authorization: Bearer $GW_TOKEN" \
  -H 'Content-Type: application/json' --data-binary "@$1" || true; }
gateway_calls() {
  api GET "/admin/usage?group=route&tenant=$TENANT" >/dev/null
  jq_ 'sum(r["calls"] for r in d["rows"] if r.get("route") == "gateway")'
}
gw_before="$(gateway_calls)"
health_before="$(jq_ 'd["health"].get("join_mismatch", 0)')"
[ "$(post_otlp "$WORK/otlp.json")" = 200 ] || die 6 "first OTLP send -> $(cat "$WORK/body")"
[ "$(post_otlp "$WORK/otlp.json")" = 200 ] || die 6 "second OTLP send -> $(cat "$WORK/body")"
[ "$(post_otlp "$WORK/otlp-foreign.json")" = 200 ] || die 6 "foreign OTLP send -> $(cat "$WORK/body")"
sent="$(json_of "$WORK/otlp.json" 'len(d["resourceLogs"][0]["scopeLogs"][0]["logRecords"])')"
# 시간 묶음은 롤업 주기(2분)만큼 늦다. 다시 보낸 것과 다른 테넌트의 기록은 행을 더하지 않는다
gw_calls="$gw_before"
for ((i = 0; i < 40; i++)); do
  sleep 5
  gw_calls="$(gateway_calls)"
  [ "$gw_calls" -lt $((gw_before + sent)) ] || break
done
[ "$gw_calls" = $((gw_before + sent)) ] || die 6 "gateway rows $gw_before -> $gw_calls, want +$sent (a resend must not add rows)"
health_after="$(jq_ 'd["health"].get("join_mismatch", 0)')"
[ "$health_after" -gt "$health_before" ] || die 6 "join_mismatch did not grow ($health_before -> $health_after)"
pass 6 "$sent gateway records -> $((gw_calls - gw_before)) rows once despite a resend; join_mismatch $health_before -> $health_after"

# ---------- 7. 거절 ----------
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "$API/telemetry/events" -H 'Authorization: Bearer wrong' \
  -H 'Content-Type: application/json' -d '{}' || true)"
[ "$code" = 401 ] || die 7 "wrong token -> $code"
INSTANCE_TOKEN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" token)"
head -c 1100000 /dev/zero | tr '\0' 'a' >"$WORK/big.json"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "$API/telemetry/events" -H "Authorization: Bearer $INSTANCE_TOKEN" \
  -H 'Content-Type: application/json' --data-binary "@$WORK/big.json" || true)"
[ "$code" = 413 ] || die 7 "body over 1 MiB -> $code"
api GET "/agents/$AG_ID" >/dev/null
revision="$(jq_ 'd["deployed_revision"]')"
envelope="$(python3 -c 'import json, sys, uuid
print(json.dumps({"schema_version": 1, "revision": int(sys.argv[1]), "pod": {"name": "e2e", "uid": "e2e"}, "boot_id": "e2e",
  "events": [{"daegam_event": "feedback", "id": uuid.uuid4().hex, "ts": "2026-01-01T00:00:00Z"}]}))' "$revision")"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "$API/telemetry/events" -H "Authorization: Bearer $INSTANCE_TOKEN" \
  -H 'Content-Type: application/json' -d "$envelope" || true)"
[ "$code" = 200 ] && [ "$(jq_ 'd["rejected"][0]["code"]')" = reserved_event ] || die 7 "feedback with an instance token -> $code $(cat "$WORK/body")"
failed_before=$FAILED
viewer_note=""
if [ "$TARGET" = install ]; then
  install_run_passwords
  # 지난 실행의 같은 이름 사용자는 지우고 이 실행의 비밀번호로 다시 만든다. API 토큰은 로그인이 준다
  # (local_user_token 은 MCP audience 의 개인 토큰을 준다)
  local_fresh_member e2e-telemetry-viewer "$TENANT" >/dev/null || die 7 "no viewer member"
  viewer="$(local_login_any e2e-telemetry-viewer "$LOCAL_PW")"
  [ -n "$viewer" ] || die 7 "no viewer token"
  for path in "/agents/$AG_ID/traces/${TRACES[0]}" "/agents/$AG_ID/events" "/usage"; do
    code="$(api_as "$viewer" GET "$path")"
    [ "$code" = 403 ] || record_fail 7 "viewer GET $path -> $code (want 403)"
  done
  # 제 트레이스에는 200 이다. viewer 가 턴 하나를 보내고 그 실행을 읽는다. api 와 chat 이 읽는 AUTH 를 이 함수
  # 안에서만 바꾼다(bash 의 local 은 부른 함수에도 보인다)
  viewer_trace() {
    local AUTH=(-H "Authorization: Bearer $viewer")
    turn 7 "$(stub_call "$KB_TOOL" '{"query": "환불"}')"
    trace_of 7 "$(sse_ 'first("done")["trace_id"]')"
  }
  viewer_trace
  viewer_note="; viewer 403 on another's trace, events and usage, 200 on its own trace"
else
  cond_skip 7v "header mode has one principal: viewer checks run on the install"
fi
# 앞의 확인이 하나라도 FAIL 이면 PASS 를 찍지 않는다
[ "$FAILED" != "$failed_before" ] || pass 7 "wrong token 401, oversized body 413, feedback reserved_event$viewer_note"

# ---------- 8. OTLP 로 내보내기 (orbstack) ----------
if [ "$TARGET" = orbstack ]; then
  "${K[@]}" -n "$ECHO_NS" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: ConfigMap
metadata: {name: $OTLP_COLLECTOR}
data:
  config.yaml: |
    receivers: {otlp: {protocols: {http: {endpoint: 0.0.0.0:4318}}}}
    exporters: {debug: {verbosity: detailed}}
    service: {pipelines: {traces: {receivers: [otlp], exporters: [debug]}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: $OTLP_COLLECTOR}
spec:
  selector: {matchLabels: {app: $OTLP_COLLECTOR}}
  template:
    metadata: {labels: {app: $OTLP_COLLECTOR}}
    spec:
      containers:
        - name: collector
          image: ${E2E_OTEL_COLLECTOR_IMAGE:-otel/opentelemetry-collector:0.110.0}
          args: ["--config=/conf/config.yaml"]
          volumeMounts: [{name: conf, mountPath: /conf}]
      volumes: [{name: conf, configMap: {name: $OTLP_COLLECTOR}}]
---
apiVersion: v1
kind: Service
metadata: {name: $OTLP_COLLECTOR}
spec: {selector: {app: $OTLP_COLLECTOR}, ports: [{port: 4318, targetPort: 4318}]}
YAML
  "${K[@]}" -n "$ECHO_NS" rollout status "deploy/$OTLP_COLLECTOR" --timeout=180s >/dev/null || die 8 "collector did not start"
  api GET /settings/observability/otlp >/dev/null
  OTLP_SAVED="$WORK/otlp-before.json"
  cp "$WORK/body" "$OTLP_SAVED"
  code="$(api PUT /settings/observability/otlp "$(printf '{"enabled": true, "endpoint": "http://%s.%s.svc.cluster.local:4318/v1/traces"}' "$OTLP_COLLECTOR" "$ECHO_NS")")"
  [ "$code" = 200 ] || die 8 "PUT OTLP settings -> $code"
  # 다른 레플리카는 OTLP 설정을 10초 기억한다(otlp_export.SETTINGS_CACHE_SECONDS). 그 뒤에 보낸 턴이라야 대기열에 든다
  sleep 12
  turn 8 "$(stub_call "$KB_TOOL" '{"query": "환불"}')"
  seen=""
  for ((i = 0; i < 30; i++)); do
    sleep 2
    seen="$("${K[@]}" -n "$ECHO_NS" logs "deploy/$OTLP_COLLECTOR" --tail=-1 2>/dev/null | grep -oE 'Name *: (invoke_agent|chat|embeddings|execute_tool)[^ ]*' | sort -u | tr '\n' ' ' || true)"
    case "$seen" in *invoke_agent*chat*) break ;; esac
  done
  case "$seen" in *invoke_agent*) pass 8 "collector saw: $seen" ;; *) record_fail 8 "collector saw no invoke_agent span: $seen" ;; esac
else
  cond_skip 8 "the OTel Collector check runs on orbstack only"
fi

finish
