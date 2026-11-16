#!/usr/bin/env bash
# P15 (D32) Knowledge Base e2e against the real (orbstack) cluster. Assumes `make deploy-local` (platform in-cluster,
# ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true) and `make lab-up` (postgres with pgvector in daegam-spike).
#
# Gate (10 steps):
#   STEP 1   platform reachable (port-forward), header mode, home cluster preflight ok
#   STEP 2   stub provider `stub-e2e` registered (PATCH /settings, idempotent. EXIT trap removes what it added)
#   STEP 3   lab Postgres registered as Database (kind postgres) + verify ok, extensions.vector.available == true
#   STEP 4   Knowledge Base (stub, 64 dims) created → table daegam_kb_<hex> and extension vector exist (psql)
#   STEP 5   markdown document (one paragraph shorter than chunking.size, contains "inventory") uploaded → indexed within
#            60s, chunks == 1, status.indexed == 1
#   STEP 6   psql: table row count == 1
#   STEP 7   Agent: template minus the tools node, plus a knowledge node (tool search_faq) → validate ok → deploy
#            Ready; pod env has DAEGAM_DB_0_HOST and no DAEGAM_DB_1_* (memory and knowledge share the Database)
#   STEP 8   chat {"tool":"search_faq","args":{"query":"inventory"}} → tool_call search_faq → tool_result success whose
#            content contains "inventory", marked untrusted outside (P43: the JSON sits inside <outside_content>) → done
#   STEP 9   DELETE document → state deleting → gone within 60s, psql rows == 0; chat again → results []
#   STEP 10  DELETE knowledge base → 409 in_use (agent) → delete the agent → 200 table_dropped → psql table gone
#   cleanup  agent·knowledge base·data service·provider this run created
#
# 만들기가 422 로 거절하는 경우(embedding_not_supported·invalid_data_service_kind 등)와 업로드 형식 거절(422
# unsupported_format)은 API 테스트가 지킨다(platform/tests/test_knowledge_api.py 의 test_create_422_table_leaves_no_resource,
# test_upload_documents). 이 게이트는 실제 Postgres·워커·Agent 를 지나는 길만 본다.
# Steps print `STEP n PASS|FAIL: ...`. 이 게이트에는 SKIP 경로가 없다 (조건이 안 맞으면 실패다).
# macOS has no `timeout`, so every wait is a polling loop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
TENANT="${E2E_TENANT:-knowledge}"
API_PORT="${E2E_API_PORT:-$PORT_KNOWLEDGE_API}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
STUB_PROVIDER="stub-e2e"
DS_NAME="pg-knowledge-e2e"; DS_DISPLAY="지식 DB"
KB_NAME="faq-e2e"; KB_DISPLAY="펫스토어 FAQ"
AG_NAME="knowledge-e2e"; AG_DISPLAY="지식 에이전트 e2e"
DOC_GONE_WAIT=60                   # 문서를 지운 뒤 워커가 행을 걷을 때까지의 한도(초)다
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-knowledge.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # header 모드라 Bearer 가 없다 (lib/e2e_api.sh 가 읽는다)
PF_API=""
AG_ID=""; KB_ID=""; DS_ID=""

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"

# pass·skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·api_delete·wait_gone·save_doc·deploy_ready·kb_create_fresh·kb_upload·kb_wait_indexed·ensure_stub_provider·
# remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg 는 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab Database 등록(lab_database)과 lab 안을 들여다보는 psql_ 은 여기에 있다
. "$HERE/lib/e2e_lab.sh"

cleanup() {
  # Knowledge Base 는 Agent 가 붙들고 있으면 409 in_use 라 Agent 가 사라진 뒤에 지운다
  if [ -n "$AG_ID" ]; then api_delete "/agents/$AG_ID"; wait_gone "/agents/$AG_ID"; fi
  [ -z "$KB_ID" ] || api_delete "/knowledge-bases/$KB_ID"
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  # STEP 2 가 stub provider 를 새로 넣었으면 lib 의 STUB_PROVIDER_ADDED 가 1 이다
  [ "$STUB_PROVIDER_ADDED" = 0 ] || remove_providers "$STUB_PROVIDER"
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# ---------- 1. platform reachable ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = "200" ] || die 1 "GET /healthz -> $code"
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 1 "GET /clusters -> $code (header mode expected)"
# 없으면 바로 아래 줄이 스스로 다룬다
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"]=="home"), "")')"
[ -n "$HOME_ID" ] && [ "$HOME_OK" = "True" ] || die 1 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
api GET /version >/dev/null
pass 1 "platform reachable :$API_PORT (build $(jq_ 'd.get("api_build") or d.get("api_version")')), cluster home id=$HOME_ID preflight ok"

# ---------- 2. provider: stub (embedding) ----------
ensure_stub_provider 2 "$STUB_PROVIDER"
pass 2 "provider $STUB_PROVIDER (stub) registered (added this run: $STUB_PROVIDER_ADDED)"

# ---------- 3. lab Postgres as Database + verify (pgvector available) ----------
lab_database 3 postgres "$DS_NAME" "$DS_DISPLAY"
DS_ID="$DATA_SERVICE_ID"
code="$(api GET "/data-services/$DS_ID")"
PGVECTOR_CAP="$(jq_ 'd["capabilities"].get("pgvector")')"
pass 3 "data service $DS_NAME id=$DS_ID verify ok (psycopg), extensions.vector.available=true, capabilities.pgvector=$PGVECTOR_CAP"

# ---------- 4. Knowledge Base create ----------
kb_body="$(python3 -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "display_name": sys.argv[2], "data_service_id": sys.argv[3], "embedding": {"provider": sys.argv[4]}}, ensure_ascii=False))
' "$KB_NAME" "$KB_DISPLAY" "$DS_ID" "$STUB_PROVIDER")"
kb_create_fresh 4 "$KB_NAME" "$kb_body"
KB_TABLE="$(jq_ 'd["table"]')"; KB_DIMS="$(jq_ 'd["embedding"]["dimensions"]')"; KB_MODEL="$(jq_ 'd["embedding"]["model"]')"
[ "$KB_DIMS" = "64" ] && [ "$KB_MODEL" = "stub" ] || die 4 "embedding stored as $KB_MODEL/$KB_DIMS (expected stub/64)"
[ "$(psql_ "select count(*) from pg_extension where extname='vector'")" = "1" ] || die 4 "extension vector not installed after create"
[ "$(psql_ "select count(*) from pg_tables where tablename='$KB_TABLE'")" = "1" ] || die 4 "table $KB_TABLE missing after create"
pass 4 "knowledge base $KB_NAME id=$KB_ID table=$KB_TABLE embedding stub/64 (extension·table exist)"

# ---------- 5. upload a markdown document → indexed, chunks == 1 ----------
cat >"$WORK/faq.md" <<'MD'
Petstore inventory: getInventory returns the current inventory counts grouped by status (available, pending, sold). Use it before placing an order.
MD
code="$(kb_upload "$KB_ID" "$WORK/faq.md")"
[ "$code" = "201" ] || die 5 "POST /documents (faq.md) -> $code"
DOC_ID="$(jq_ 'd["id"]')"; DOC_STATE="$(jq_ 'd["state"]')"
[ "$DOC_STATE" = "pending" ] || die 5 "uploaded document state=$DOC_STATE (expected pending)"
kb_wait_indexed 5 "$KB_ID" "$DOC_ID"
DOC_CHUNKS="$(jq_ 'd["chunks"]')"
[ "$DOC_CHUNKS" = "1" ] || die 5 "chunks=$DOC_CHUNKS (expected 1)"
code="$(api GET "/knowledge-bases/$KB_ID")"
[ "$(jq_ 'd["status"]["indexed"], d["status"]["documents"], d["status"]["chunks"]')" = "1 1 1" ] || die 5 "status: $(jq_ 'd["status"]')"
pass 5 "document faq.md id=$DOC_ID indexed (chunks=1, tokens=$(jq_ 'd.get("status", {}).get("tokens")')); status indexed/documents/chunks = 1/1/1"

# ---------- 6. psql rows ----------
ROWS="$(psql_ "select count(*) from \"$KB_TABLE\"")"
[ "$ROWS" = "1" ] || die 6 "table $KB_TABLE has $ROWS rows (expected 1)"
pass 6 "psql: $KB_TABLE rows=1"

# ---------- 7. agent with a knowledge node → deploy Ready ----------
code="$(api GET /agents/templates)"
[ "$code" = "200" ] || die 7 "GET /agents/templates -> $code"
python3 - "$WORK/body" "$WORK/agent.json" "$AG_NAME" "$AG_DISPLAY" "$STUB_PROVIDER" "$DS_ID" "$KB_ID" <<'PY'
import json, sys
templates = json.load(open(sys.argv[1]))
out, name, display, provider, ds_id, kb_id = sys.argv[2:8]
t = templates[0]
graph, layout = t["graph"], t["layout"]
main = next(n["id"] for n in graph["nodes"] if n["type"] == "agent" and n["data"]["role"] == "main")
tools = {n["id"] for n in graph["nodes"] if n["type"] == "tools"}
graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "tools"]
graph["edges"] = [e for e in graph["edges"] if e["target"] not in tools]
layout = {k: v for k, v in layout.items() if k not in tools}
for n in graph["nodes"]:
    if n["type"] == "model":
        n["data"]["provider"] = provider
    elif n["type"] == "memory":
        n["data"]["checkpointer"]["data_service_id"] = ds_id
graph["nodes"].append({"id": "knowledge", "type": "knowledge", "data": {
    "knowledge_base_id": kb_id, "tool_name": "search_faq", "description": "", "top_k": 5, "min_score": None}})
graph["edges"].append({"id": "e-knowledge", "source": main, "target": "knowledge", "kind": "knowledge"})
layout["knowledge"] = {"x": 900, "y": 160}
json.dump({"name": name, "display_name": display, "graph": graph, "layout": layout, "service": t["service"]}, open(out, "w"), ensure_ascii=False)
PY
code="$(api POST /agents/validate "$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); print(json.dumps({"graph": b["graph"], "service": b["service"]}, ensure_ascii=False))' "$WORK/agent.json")")"
[ "$code" = "200" ] && [ "$(jq_ 'd["ok"]')" = "True" ] || die 7 "validate -> $code $(jq_ 'd.get("issues")')"
VAL_ISSUES="$(jq_ '[i["code"] for i in d["issues"]]')"
# 저장의 409 는 같은 정의로 PUT 하고 배포의 409 already_deployed 는 redeploy 로 잇는다 (lib/e2e_api.sh 머리말)
save_doc 7 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_ID phase=Ready"
deploy_ready 7 /agents "$AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
AI_NS="$(jq_ 'd["k8s"]["namespace"]')"; AI_K8S="$(jq_ 'd["k8s"]["name"]')"
env_names="$("${K[@]}" -n "$AI_NS" get deployment "$AI_K8S" -o jsonpath='{.spec.template.spec.containers[?(@.name=="agent")].env[*].name}')"
echo "  env: $env_names"
case " $env_names " in *" DAEGAM_DB_0_HOST "*) ;; *) die 7 "DAEGAM_DB_0_HOST missing from pod env" ;; esac
case " $env_names " in *" DAEGAM_DB_1_HOST "*) die 7 "DAEGAM_DB_1_HOST present: memory and knowledge share one Database, so one binding is expected" ;; esac
case " $env_names " in *" DAEGAM_KNOWLEDGE_0_API_KEY "*) die 7 "DAEGAM_KNOWLEDGE_0_API_KEY present for a stub provider" ;; esac
pass 7 "agent $AG_NAME id=$AG_ID (validate issues=$VAL_ISSUES) deployed Ready; env has DAEGAM_DB_0_* only"

# ---------- 8. chat: search_faq(inventory) ----------
code="$(chat "/agents/$AG_ID/chat" "$(msg '{"tool":"search_faq","args":{"query":"inventory"}}')")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8 "POST /chat -> $code"; }
NAMES="$(sse_ 'names')"
[ "$(sse_ 'names[-1]')" = "done" ] || die 8 "stream did not end with done: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ 'first("tool_call")["name"]')" = "search_faq" ] || die 8 "tool_call is not search_faq: $NAMES"
RESULT="$(sse_ 'first("tool_result")["content"]')"
[ "$(sse_ 'first("tool_result")["status"]')" = "success" ] || die 8 "tool_result status: $(sse_ 'first("tool_result")')"
echo "$RESULT" | grep -qi inventory || die 8 "tool_result lacks the passage: $RESULT"
SCORE="$(sse_ 'content(0)["results"][0]["score"]')"
SOURCE="$(sse_ 'content(0)["results"][0]["source"]')"
[ "$SOURCE" = "faq.md" ] || die 8 "results[0].source=$SOURCE (expected faq.md)"
# Knowledge 의 검색 결과는 기본으로 바깥 내용이다(P43 2절). 런타임이 글을 감싸고 tool_result 에 그 종류를 싣는다
[ "$(sse_ 'first("tool_result").get("untrusted"), first("tool_result")["content"].startswith("<outside_content source=\"search_faq\">")')" = "outside True" ] \
  || die 8 "the search result is not marked and wrapped as outside content: $(sse_ 'first("tool_result")')"
SID1="$(sse_ 'first("done")["session_id"]')"
pass 8 "chat $NAMES; search_faq(inventory) -> 1 result source=faq.md score=$SCORE, marked outside; session=$SID1"

# ---------- 9. delete the document → rows gone → empty results ----------
code="$(api DELETE "/knowledge-bases/$KB_ID/documents/$DOC_ID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "deleting" ] || die 9 "DELETE document -> $code $(cat "$WORK/body")"
# 지우기가 실패하면 문서가 failed 로 남아 404 가 되지 않으므로 기다리지 않고 멈춘다
wait_gone "/knowledge-bases/$KB_ID/documents/$DOC_ID" "$DOC_GONE_WAIT" 'd["state"] == "failed"' || {
  [ $? != 2 ] || die 9 "document delete failed: $(jq_ 'd.get("error")')"
  die 9 "document not gone after ${DOC_GONE_WAIT}s"
}
ROWS="$(psql_ "select count(*) from \"$KB_TABLE\"")"
[ "$ROWS" = "0" ] || die 9 "table $KB_TABLE has $ROWS rows after delete (expected 0)"
code="$(chat "/agents/$AG_ID/chat" "$(msg '{"tool":"search_faq","args":{"query":"inventory"}}')")"
[ "$code" = "200" ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 9 "second chat -> $code"; }
[ "$(sse_ 'content(0)["results"]')" = "[]" ] || die 9 "results after delete: $(sse_ 'first("tool_result")["content"]')"
code="$(api GET "/knowledge-bases/$KB_ID")"
[ "$(jq_ 'd["status"]["documents"]')" = "0" ] || die 9 "status.documents=$(jq_ 'd["status"]["documents"]') after delete"
pass 9 "document deleted by the worker (rows=0, status.documents=0); search_faq -> results []"

# ---------- 10. delete knowledge base: 409 in_use → after deleting the agent → 200 table_dropped ----------
code="$(api DELETE "/knowledge-bases/$KB_ID")"
[ "$code" = "409" ] && [ "$(jq_ 'd["detail"]["code"]')" = "in_use" ] || die 10 "DELETE knowledge base with an agent -> $code $(cat "$WORK/body")"
# 가드 응답 키 둘. agent_ids 는 현재 리비전이, deployed_agent_ids 는 배포가 핀한 리비전이 참조하는 것이다 (D42)
echo "$(jq_ 'd["detail"].get("agent_ids")')" | grep -q "$AG_ID" || die 10 "in_use detail lacks agent $AG_ID: $(jq_ 'd["detail"]')"
echo "$(jq_ 'd["detail"].get("deployed_agent_ids")')" | grep -q "$AG_ID" || die 10 "in_use detail lacks deployed agent $AG_ID: $(jq_ 'd["detail"]')"
# 삭제 하나로 정의와 배포가 함께 사라진다 (D42)
code="$(api DELETE "/agents/$AG_ID")"
[ "$code" = "200" ] || [ "$code" = "202" ] || die 10 "DELETE agent -> $code"
wait_gone "/agents/$AG_ID" "$READY_TIMEOUT" || die 10 "agent $AG_ID not gone after ${READY_TIMEOUT}s"; AG_ID=""
code="$(api DELETE "/knowledge-bases/$KB_ID")"
[ "$code" = "200" ] && [ "$(jq_ 'd["deleted"], d["table_dropped"]')" = "True True" ] || die 10 "DELETE knowledge base -> $code $(cat "$WORK/body")"
KB_ID=""
[ "$(psql_ "select count(*) from pg_tables where tablename='$KB_TABLE'")" = "0" ] || die 10 "table $KB_TABLE still exists"
code="$(api GET /knowledge-bases)"
[ "$code" = "200" ] || die 10 "GET /knowledge-bases -> $code"
[ -z "$(find_id "$KB_NAME")" ] || die 10 "knowledge base still listed"
pass 10 "knowledge base delete: 409 in_use (agent) -> after deleting it 200 table_dropped=true; table gone"

finish
