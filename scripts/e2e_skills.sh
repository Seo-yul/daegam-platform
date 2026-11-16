#!/usr/bin/env bash
# P39a (D64) Skills e2e against a real cluster.
#
#   make e2e-skills
#       orbstack: assumes `make deploy-local` (platform in-cluster, ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true, header
#       mode) and `make lab-up` (Postgres in daegam-spike for the memory node)
#   make e2e-skills E2E_TARGET=install E2E_INSTALL_USER=<admin>
#       the install (P85 10절): its public host, local mode, tenant e2e. The cluster refuses the stub provider, so the echo server
#       (lib/e2e_llm_echo.sh) answers by the same stub rules, and the memory node uses the platform namespace's
#       Postgres. Never run it together with e2e-telemetry or e2e-web-tools
#
# Gate (6 steps):
#   STEP 1   platform reachable, the model provider (stub; on the install the echo) and the memory Database (lab Postgres; on the
#            install the platform's) registered; Skill skill-a-e2e created from files (r1) and given r2 from a zip; a broken
#            frontmatter and a tag character are each 422 invalid_skill
#   STEP 2   Agent skills-e2e (stub rules) with skill-a and skill-b on the main agent and skill-c on a subagent deploys
#            Ready; saving it with the main file tool off is 422 skills_filesystem_disabled; in the pod
#            /config/skills/main/<name>/SKILL.md is 0444, opening it for writing is refused and /config is mounted ro
#   STEP 3   chat read_file /skills/main/skill-a-e2e/SKILL.md returns r2's marker; write_file under /skills/ is refused
#   STEP 4   r3 keeps the deployment on r2; raising the pin and redeploying gives r3 in a continuing session; deleting a
#            Skill in use is 409 in_use
#   STEP 5   a Skill of tenant skills-other (install: e2e-skills-x) opened with * is attached and deployed; after
#            narrowing its policy a deleted pod comes back Ready, and saving the Agent is 422 skill_not_consumable
#   STEP 6   Agent skills-big-e2e with ten Skills of about 390 KiB each (close to the 4 MiB limit) starts with the default
#            init limit and no OOMKilled container
#   cleanup  agents, Skills, Database and provider this run created; on the install the echo namespace
#
# 형식 규칙 하나하나와 캔버스 코드는 플랫폼 테스트가 지킨다(platform/tests/test_skill_format.py, test_skills_api.py,
# test_agent_skills.py). 이 게이트는 실제 init 단계·읽기 전용 마운트·런타임을 지나는 길만 본다.
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
API_PORT="${E2E_API_PORT:-$PORT_SKILLS_API}"
case "$TARGET" in
  orbstack)
    CTX="${KUBE_CONTEXT:-orbstack}"; TENANT="${E2E_TENANT:-skills}"; OTHER_TENANT="${E2E_OTHER_TENANT:-skills-other}"
    BASE="http://127.0.0.1:${API_PORT}"; PROVIDER="stub-e2e" ;;
  install)
    install_env_defaults
    CTX="${INSTALL_CONTEXT:-}"; TENANT="${E2E_TENANT:-e2e}"; OTHER_TENANT="${E2E_OTHER_TENANT:-e2e-skills-x}"
    BASE="https://${DAEGAM_HOST:-}"; PROVIDER="e2e-echo-skills" ;;
  *) echo "E2E_TARGET 은 orbstack 이나 install 이다 (got $TARGET)"; exit 1 ;;
esac
# 정리 단계가 쓰는 이 게이트의 테넌트다. skill_create 의 `TENANT=… api` 안에서 신호를 받으면 정리 단계에도 그 테넌트가
# 보인다(bash 는 함수 앞의 대입을 그 함수가 도는 동안 둔다)
GATE_TENANT="$TENANT"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
API="$BASE/api"
DS_NAME="pg-skills-e2e"; DS_DISPLAY="Skill 기억 DB"
SK_A="skill-a-e2e"; SK_B="skill-b-e2e"; SK_C="skill-c-e2e"; SK_SHARED="skill-shared-e2e"
AG_NAME="skills-e2e"; AG_BIG="skills-big-e2e"
BIG_COUNT=10
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-skills.XXXXXX")"
K=(kubectl --context "$CTX")
AUTH=()                            # header 모드는 Bearer 가 없다. 설치본은 로그인한 관리자다 (lib/e2e_api.sh 가 읽는다)
PF_API=""
DS_ID=""; AG_ID=""; AG_BIG_ID=""
SKILL_IDS=""; SHARED_ID=""

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT"

# pass·skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"
# api·jq_·find_id·api_delete·wait_gone·save_doc·deploy_ready·ensure_stub_provider·remove_providers 는 여기에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg·stub_call 은 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab Database 등록(lab_database)은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# 내려가는 파드가 사라지기를 기다리는 wait_pods_settled 는 여기에 있다
. "$HERE/lib/e2e_control_plane.sh"
# 플랫폼에 붙는 platform_connect(설치본은 관리자 로그인, orbstack 은 port-forward)는 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
# install 갈래의 메아리 서버와 provider(echo_agent_image·echo_up·echo_provider·echo_down)는 여기에 있다
. "$HERE/lib/e2e_llm_echo.sh"

# skill_files NAME MARK [SIDE_KB]  Skill NAME 의 files 본문(JSON 목록)을 찍는다. SKILL.md 의 본문에 MARK 를 넣는다.
# SIDE_KB 를 주면 그 크기(KiB)의 곁 파일 둘을 더한다
skill_files() {
  python3 - "$1" "$2" "${3:-0}" <<'PY'
import json, sys
name, mark, side_kb = sys.argv[1], sys.argv[2], int(sys.argv[3])
files = [{"path": "SKILL.md", "content": f"---\nname: {name}\ndescription: e2e Skill {name} ({mark}).\n---\n\n# {name}\n\nMarker: {mark}\n"}]
line = "This is filler text for the Skills e2e, written to make a large Skill bundle.\n"
for i in range(2 if side_kb else 0):
    files.append({"path": f"reference/part-{i}.md", "content": line * (side_kb * 1024 // len(line))})
print(json.dumps(files))
PY
}

# skill_create STEP NAME MARK [SIDE_KB] [TENANT]  만들고 id 를 SKILL_ID 에 남긴다. 이전 실행이 남긴 같은 이름은 지우고 다시 만든다
skill_create() {
  local step="$1" body tenant="${5:-$TENANT}" code
  body="$(python3 -c 'import json, sys; print(json.dumps({"files": json.loads(sys.argv[1]), "note": "e2e"}))' "$(skill_files "$2" "$3" "${4:-0}")")"
  printf '%s' "$body" >"$WORK/skill.json"
  code="$(TENANT=$tenant api POST /skills "@$WORK/skill.json")"
  if [ "$code" = 409 ]; then
    TENANT=$tenant api GET /skills >/dev/null
    TENANT=$tenant api_delete "/skills/$(find_id "$2")"
    code="$(TENANT=$tenant api POST /skills "@$WORK/skill.json")"
  fi
  [ "$code" = 201 ] || die "$step" "POST /skills $2 -> $code"
  SKILL_ID="$(jq_ 'd["id"]')"
  SKILL_IDS="$SKILL_IDS $tenant:$SKILL_ID"
}

# agent_body OUT NAME SPEC_PY  템플릿에서 tools 노드를 뺀 정의에 SPEC_PY(그래프 g 를 고치는 파이썬 문)를 얹어 OUT 에
# 둔다. 모델은 PROVIDER(orbstack 은 스텁, 설치본은 메아리), memory 는 STEP 1 의 Database 다
agent_body() {
  local code
  code="$(api GET /agents/templates)"
  [ "$code" = 200 ] || return 1
  python3 - "$WORK/body" "$1" "$2" "$PROVIDER" "$DS_ID" "$3" <<'PY'
import json, sys
templates, out, name, provider, ds_id, edit = json.load(open(sys.argv[1])), *sys.argv[2:7]
t = templates[0]
g = t["graph"]
drop = {n["id"] for n in g["nodes"] if n["type"] in ("tools", "guardrail")}
g["nodes"] = [n for n in g["nodes"] if n["id"] not in drop]
g["edges"] = [e for e in g["edges"] if e["target"] not in drop]
for n in g["nodes"]:
    if n["type"] == "model":
        n["data"]["provider"] = provider
    elif n["type"] == "memory":
        n["data"]["checkpointer"]["data_service_id"] = ds_id
    elif n["type"] == "entry":
        n["data"].pop("rate_limit", None)
main = next(n for n in g["nodes"] if n["type"] == "agent")
exec(edit)
layout = {k: v for k, v in t["layout"].items() if k in {n["id"] for n in g["nodes"]}}
json.dump({"name": name, "graph": g, "layout": layout, "service": t["service"]}, open(out, "w"), ensure_ascii=False)
PY
}

# agent_pod NS K8S_NAME  그 Agent 의 Running 파드 이름을 찍는다
agent_pod() {
  "${K[@]}" -n "$1" get pods -o json | python3 -c 'import json, sys
pods = [p for p in json.load(sys.stdin)["items"] if p["metadata"]["name"].startswith(sys.argv[1] + "-")
        and p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
print(pods[0]["metadata"]["name"] if pods else "")' "$2"
}

cleanup() {
  local entry TENANT="$GATE_TENANT"
  for id in "$AG_ID" "$AG_BIG_ID"; do [ -z "$id" ] || api_delete "/agents/$id"; done
  for id in "$AG_ID" "$AG_BIG_ID"; do [ -z "$id" ] || wait_gone "/agents/$id" || true; done
  for entry in $SKILL_IDS; do TENANT="${entry%%:*}" api_delete "/skills/${entry#*:}"; done
  [ -z "$DS_ID" ] || api_delete "/data-services/$DS_ID"
  [ "$STUB_PROVIDER_ADDED" = 0 ] || remove_providers "$PROVIDER"
  echo_down
  stop_pf "$PF_API" "$API_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# ---------- 1. platform, provider, Database, a Skill with two revisions, format refusals ----------
# 설치본은 e2e 관리자로 공개 주소에 로그인하고, orbstack 은 port-forward 한다 (lib/e2e_local_auth.sh)
platform_connect 1
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 1 "GET /clusters -> $code"
HOME_ID="$(find_id home)"
[ -n "$HOME_ID" ] || die 1 "cluster home missing"
if [ "$TARGET" = install ]; then
  echo_agent_image
  echo_up 1
  echo_provider 1 "$PROVIDER"
  install_postgres_database 1 "$DS_NAME" "$PLATFORM_NS" "$HOME_ID"
else
  ensure_stub_provider 1 "$PROVIDER"
  lab_database 1 postgres "$DS_NAME" "$DS_DISPLAY"
fi
DS_ID="$DATA_SERVICE_ID"
skill_create 1 "$SK_A" MARK-A-R1
SK_A_ID="$SKILL_ID"
mkdir -p "$WORK/zip/$SK_A"
printf -- '---\nname: %s\ndescription: e2e Skill %s (MARK-A-R2).\n---\n\n# %s\n\nMarker: MARK-A-R2\n' "$SK_A" "$SK_A" "$SK_A" >"$WORK/zip/$SK_A/SKILL.md"
printf 'Steps for the e2e.\n' >"$WORK/zip/$SK_A/steps.md"
(cd "$WORK/zip" && zip -qr "$WORK/a-r2.zip" "$SK_A")
code="$(api POST "/skills/$SK_A_ID/revisions/import" "" -F "file=@$WORK/a-r2.zip" -F expect_revision=1)"
[ "$code" = 201 ] && [ "$(jq_ 'd["revision"]')" = 2 ] || die 1 "zip import of r2 -> $code"
python3 - "$SK_A" >"$WORK/broken.json" <<'PY'
import json, sys
print(json.dumps({"files": [{"path": "SKILL.md", "content": "---\nname: [broken\n---\nbody\n"}]}))
PY
code="$(api POST "/skills/$SK_A_ID/revisions" "$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); d["expect_revision"] = 2; print(json.dumps(d))' "$WORK/broken.json")")"
[ "$code" = 422 ] && [ "$(jq_ 'd["detail"]["code"]')" = invalid_skill ] || die 1 "broken frontmatter -> $code"
BROKEN="$(jq_ '[i["code"] for i in d["detail"]["issues"]]')"
python3 - "$SK_A" >"$WORK/tag.json" <<'PY'
import json, sys
name = sys.argv[1]
text = f"---\nname: {name}\ndescription: e2e Skill with a hidden tag.\n---\n\nHello\U000E0041\U000E0042 world\n"
print(json.dumps({"files": [{"path": "SKILL.md", "content": text}], "expect_revision": 2}))
PY
code="$(api POST "/skills/$SK_A_ID/revisions" "@$WORK/tag.json")"
[ "$code" = 422 ] && echo "$(jq_ '[i["code"] for i in d["detail"]["issues"]]')" | grep -q invisible_chars \
  || die 1 "a tag character -> $code"
pass 1 "Skill $SK_A=$SK_A_ID r1 from files, r2 from a zip; broken frontmatter 422 $BROKEN; tag character 422 invisible_chars"

# ---------- 2. Agent with Skills on the main agent and a subagent ----------
skill_create 2 "$SK_B" MARK-B
SK_B_ID="$SKILL_ID"
skill_create 2 "$SK_C" MARK-C
SK_C_ID="$SKILL_ID"
# edit_for REV  skill-a 를 리비전 REV 로 고정한 그래프 고치기다
edit_for() { printf '%s' "main['data']['skills'] = [{'id': '$SK_A_ID', 'revision': $1}, {'id': '$SK_B_ID', 'revision': 1}]
g['nodes'] += [
    {'id': 'agent-sub', 'type': 'agent', 'data': {'role': 'sub', 'name': 'helper', 'description': 'Helps.', 'system_prompt': 'Help.', 'skills': [{'id': '$SK_C_ID', 'revision': 1}]}},
    {'id': 'model-sub', 'type': 'model', 'data': {'name': 'sub', 'provider': '$PROVIDER', 'model': None, 'max_output_tokens': 1024, 'reasoning': {'effort': 'low'}, 'temperature': None, 'fallback': None}},
]
g['edges'] += [
    {'id': 'e-delegate', 'source': main['id'], 'target': 'agent-sub', 'kind': 'delegate'},
    {'id': 'e-model-sub', 'source': 'agent-sub', 'target': 'model-sub', 'kind': 'model'},
]"; }
agent_body "$WORK/agent-off.json" "$AG_NAME" "$(edit_for 2)
main['data']['builtin_tools'] = {'filesystem': False}" || die 2 "agent body"
code="$(api POST /agents/validate "$(python3 -c 'import json, sys; b = json.load(open(sys.argv[1])); print(json.dumps({"graph": b["graph"], "service": b["service"]}))' "$WORK/agent-off.json")")"
[ "$code" = 200 ] && echo "$(jq_ '[i["code"] for i in d["issues"]]')" | grep -q skills_filesystem_disabled \
  || die 2 "validate with the file tool off -> $code $(jq_ 'd.get("issues")')"
code="$(api POST /agents "$(cat "$WORK/agent-off.json")")"
[ "$code" = 422 ] || die 2 "saving with the file tool off -> $code (expected 422)"
agent_body "$WORK/agent.json" "$AG_NAME" "$(edit_for 2)" || die 2 "agent body"
save_doc 2 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_NAME ($AG_ID) phase=Ready"
deploy_ready 2 /agents "$AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"
POD="$(agent_pod "$AG_NS" "$AG_K8S")"
[ -n "$POD" ] || die 2 "no running pod for $AG_K8S"
FILES="$("${K[@]}" -n "$AG_NS" exec "$POD" -c agent -- python -c 'import errno, os, stat, sys
base = "/config/skills"
out = []
for root, _dirs, files in os.walk(base):
    for f in files:
        out.append(os.path.relpath(os.path.join(root, f), base))
mode = oct(stat.S_IMODE(os.stat(base + "/main/" + sys.argv[1] + "/SKILL.md").st_mode))
try:
    open(base + "/main/" + sys.argv[1] + "/SKILL.md", "a").close(); write = "open"
except OSError as e:
    write = errno.errorcode.get(e.errno, str(e.errno))
mount = next((line.split()[3].split(",")[0] for line in open("/proc/mounts") if line.split()[1] == "/config"), "?")
print(sorted(out), mode, write, mount)' "$SK_A")"
# 0444 라 쓰기는 권한(EACCES)에서 먼저 막히고, 쓸 수 있는 파일이어도 마운트가 ro 라 EROFS 다
case "$FILES" in
  *"'main/$SK_A/SKILL.md'"*"'main/$SK_B/SKILL.md'"*"'sub-0/$SK_C/SKILL.md'"*" 0o444 "E*" ro") ;;
  *) die 2 "skill files in the pod: $FILES" ;;
esac
pass 2 "agent $AG_NAME=$AG_ID Ready; file tool off -> skills_filesystem_disabled (422 on save); pod files ${FILES% 0o444*}; SKILL.md 0444, write ${FILES##*0o444 }"

# ---------- 3. read and write through the chat ----------
# bash 3.2 는 겹친 "$(…"$(…)"…)" 안의 {…, …} 를 중괄호 확장으로 쪼갠다. 그래서 조각을 먼저 변수에 둔다
READ_ARGS="$(printf '{"file_path": "/skills/main/%s/SKILL.md", "limit": 1000}' "$SK_A")"
READ_A="$(stub_call read_file "$READ_ARGS")"
WRITE_ARGS="$(printf '{"file_path": "/skills/main/%s/x.md", "content": "x"}' "$SK_A")"
WRITE_A="$(stub_call write_file "$WRITE_ARGS")"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$READ_A")")"
[ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 3 "chat read_file -> $code"; }
sse_ 'first("tool_result")["content"]' | grep -q MARK-A-R2 || die 3 "read_file lacks MARK-A-R2: $(sse_ 'first("tool_result")')"
SID="$(sse_ 'first("done")["session_id"]')"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$WRITE_A" "$SID")")"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 3 "chat write_file -> $code"; }
WRITE="$(sse_ 'first("tool_result")["content"]')"
case "$WRITE" in *ermission* | *denied* | *"not allowed"* | *read-only* | *Read-only*) ;; *) die 3 "write_file under /skills/ was not refused: $WRITE" ;; esac
pass 3 "read_file /skills/main/$SK_A/SKILL.md -> MARK-A-R2; write_file under /skills/ refused (${WRITE:0:80})"

# ---------- 4. r3, the pin, a continuing session, delete in use ----------
python3 -c 'import json, sys; print(json.dumps({"files": json.loads(sys.argv[1]), "expect_revision": 2}))' "$(skill_files "$SK_A" MARK-A-R3)" >"$WORK/r3.json"
code="$(api POST "/skills/$SK_A_ID/revisions" "@$WORK/r3.json")"
[ "$code" = 201 ] && [ "$(jq_ 'd["revision"]')" = 3 ] || die 4 "adding r3 -> $code"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$READ_A" "$SID")")"
sse_ 'first("tool_result")["content"]' | grep -q MARK-A-R2 || die 4 "the deployment left r2 before a redeploy: $(sse_ 'first("tool_result")')"
agent_body "$WORK/agent.json" "$AG_NAME" "$(edit_for 3)" || die 4 "agent body"
save_doc 4 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
code="$(api POST "/agents/$AG_ID/redeploy")"
[ "$code" = 200 ] || die 4 "redeploy with r3 -> $code"
wait_ready "/agents/$AG_ID" 4
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout=180s >/dev/null || die 4 "rollout of $AG_K8S"
# 옛 파드가 preStop 으로 내려가는 동안에도 rollout status 는 끝난다. 그 파드가 답하지 않게 다 사라지기를 기다린다
wait_pods_settled "$AG_NS" "daegam.devops.ai.kr/id=$AG_ID" || die 4 "old pods of $AG_K8S did not go away"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$READ_A" "$SID")")"
[ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 4 "chat after the redeploy -> $code"; }
sse_ 'first("tool_result")["content"]' | grep -q MARK-A-R3 || die 4 "the continuing session did not see r3: $(sse_ 'first("tool_result")')"
code="$(api DELETE "/skills/$SK_A_ID")"
[ "$code" = 409 ] && [ "$(jq_ 'd["detail"]["code"]')" = in_use ] || die 4 "deleting a Skill in use -> $code"
pass 4 "r3 added: the deployment stays on r2 until the pin is raised; after the redeploy session $SID reads r3; delete in use -> 409 in_use"

# ---------- 5. another tenant's Skill opened with * ----------
skill_create 5 "$SK_SHARED" MARK-SHARED 0 "$OTHER_TENANT"
SHARED_ID="$SKILL_ID"
SHARE='{"view": {"tenants": ["*"]}, "consume": {"tenants": ["*"]}, "manage": {}}'
code="$(TENANT=$OTHER_TENANT api PUT "/skills/$SHARED_ID/access" "$SHARE")"
[ "$code" = 200 ] || die 5 "opening $SK_SHARED with * -> $code"
EDIT5="$(edit_for 3)
main['data']['skills'].append({'id': '$SHARED_ID', 'revision': 1})"
agent_body "$WORK/agent.json" "$AG_NAME" "$EDIT5" || die 5 "agent body"
save_doc 5 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
code="$(api POST "/agents/$AG_ID/redeploy")"
[ "$code" = 200 ] || die 5 "redeploy with the shared Skill -> $code"
wait_ready "/agents/$AG_ID" 5
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout=180s >/dev/null || die 5 "rollout of $AG_K8S"
wait_pods_settled "$AG_NS" "daegam.devops.ai.kr/id=$AG_ID" || die 5 "old pods of $AG_K8S did not go away"
# view 는 열어 두고 consume 만 좁힌다. 보이지 않으면 저장은 unknown_skill 이다
code="$(TENANT=$OTHER_TENANT api PUT "/skills/$SHARED_ID/access" '{"view": {"tenants": ["*"]}, "consume": {"tenants": []}, "manage": {}}')"
[ "$code" = 200 ] || die 5 "narrowing $SK_SHARED -> $code"
POD="$(agent_pod "$AG_NS" "$AG_K8S")"
"${K[@]}" -n "$AG_NS" delete pod "$POD" --wait=false >/dev/null
sleep 5
"${K[@]}" -n "$AG_NS" rollout status "deploy/$AG_K8S" --timeout=180s >/dev/null || die 5 "the deleted pod did not come back"
wait_ready "/agents/$AG_ID" 5
# 그래프가 같은 저장은 검증하지 않는다(배치만 옮긴 저장). 그래서 프롬프트 한 줄을 바꿔 리비전을 만든다
agent_body "$WORK/agent.json" "$AG_NAME" "$EDIT5
main['data']['system_prompt'] += ' Narrowed.'" || die 5 "agent body"
code="$(api PUT "/agents/$AG_ID" "$(cat "$WORK/agent.json")")"
[ "$code" = 422 ] && echo "$(jq_ 'd["detail"]')" | grep -q skill_not_consumable || die 5 "saving after the narrowing -> $code"
WARNINGS="$(api GET "/agents/$AG_ID" >/dev/null; jq_ '(d.get("status") or {}).get("warnings")')"
pass 5 "$OTHER_TENANT's $SK_SHARED opened with * and attached; narrowed: a deleted pod came back Ready, saving -> 422 skill_not_consumable (warnings $WARNINGS)"

# ---------- 6. ten large Skills ----------
BIG_SKILLS=""
for i in $(seq 1 "$BIG_COUNT"); do
  skill_create 6 "skill-big-$i-e2e" "MARK-BIG-$i" 190
  BIG_SKILLS="$BIG_SKILLS {'id': '$SKILL_ID', 'revision': 1},"
done
agent_body "$WORK/agent-big.json" "$AG_BIG" "main['data']['skills'] = [$BIG_SKILLS]" || die 6 "agent body"
save_doc 6 /agents "$AG_BIG" "$(cat "$WORK/agent-big.json")"
AG_BIG_ID="$SAVED_ID"
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_BIG ($AG_BIG_ID) phase=Ready"
deploy_ready 6 /agents "$AG_BIG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
BIG_NS="$(jq_ 'd["k8s"]["namespace"]')"; BIG_K8S="$(jq_ 'd["k8s"]["name"]')"
POD="$(agent_pod "$BIG_NS" "$BIG_K8S")"
"${K[@]}" -n "$BIG_NS" get pod "$POD" -o json >"$WORK/big-pod.json"
INIT="$(json_of "$WORK/big-pod.json" '[(c["name"], (c.get("resources") or {}).get("limits", {}).get("memory")) for c in d["spec"]["initContainers"]]')"
OOM="$(json_of "$WORK/big-pod.json" '[s["name"] for s in d["status"].get("initContainerStatuses", []) + d["status"].get("containerStatuses", []) if "OOMKilled" in json.dumps(s)]')"
[ "$OOM" = "[]" ] || die 6 "OOMKilled containers: $OOM"
BYTES="$(api GET "/agents/$AG_BIG_ID" >/dev/null; "${K[@]}" -n "$BIG_NS" exec "$POD" -c agent -- python -c 'import os
print(sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk("/config/skills") for f in fs))')"
pass 6 "agent $AG_BIG with $BIG_COUNT Skills ($BYTES bytes on disk) Ready; init limits $INIT; no OOMKilled"

finish
