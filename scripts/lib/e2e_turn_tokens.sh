# shellcheck shell=bash
# 턴 위임 토큰(P53)을 보는 한 벌 (e2e_agent·e2e_install). `. "$HERE/lib/e2e_turn_tokens.sh"` 로 읽는다.
# 부르는 쪽이 ROOT·CTX·K(kubectl 배열)·PLATFORM_NS·WORK·API·TENANT 와 AUTH(관리자 Bearer 헤더 배열)를 두고 e2e_json.sh·
# e2e_api.sh(api·jq_)·e2e_control_plane.sh(platform_config)·e2e_report.sh(die)·e2e_local_auth.sh(local_set_member)를 먼저
# 읽는다. human_token 을 부르는 쪽은 mcp_rpc.sh(svc_token)도 읽는다.
#
# 위임 기록은 API 로 나가지 않는다. 기록을 보는 단계는 플랫폼 네임스페이스의 MongoDB 파드(StatefulSet mongodb, 컨테이너
# mongo) 안에서 mongosh 로 읽는다. 비밀번호는 그 파드의 env 에서 쓰므로 스크립트에도 프로세스 인자에도 나오지 않는다.
# 비밀의 해시는 꺼내지 않는다.
#
# 감사 기록의 창(since·until)은 감사 시각과 같은 클러스터의 시계로 잡는다. 이 컴퓨터의 시계로 잡으면 노드와 어긋난 만큼
# 기록을 놓치거나 다른 기록을 받는다
# cluster_now
#   플랫폼 파드의 시계를 kubectl exec 로 밀리초까지 읽어 ISO 8601(끝에 Z)로 찍는다. exec 가 시작한 뒤 끝나기 전에 읽으므로
#   턴 앞에서 읽은 값은 턴보다 늦지 않고 턴 뒤에서 읽은 값은 턴보다 이르지 않다. 그래서 넓히지 않고 창의 끝으로 쓴다.
#   응답의 Date 머리는 uvicorn 이 1초마다 고쳐 최대 2초 가까이 늦으므로 쓰지 않는다
# grant_window FILE MATCH PAD [OPEN_END]
#   turn_grants_of 가 쓴 FILE 에서 MATCH(`id=<위임 id>` 또는 `target=<kind>:<id>`)에 맞는 첫 위임의 opened_at 에서 PAD 초를
#   빼고 ended_at 에 PAD 초를 더해 "SINCE UNTIL" 한 줄로 찍는다. 아직 끝나지 않았으면 UNTIL 은 opened_at + OPEN_END + PAD 초이고,
#   OPEN_END 가 없으면 `-` 다. 맞는 위임이 없으면 아무것도 찍지 않는다
# turn_window STEP AGENT_ID SESSION_ID GRANT_ID BEFORE AFTER
#   한 턴의 감사 창을 TURN_SINCE·TURN_UNTIL 에 남긴다. GRANT_ID(응답 머리 X-Daegam-Turn-Grant)가 있고 클러스터 안 MongoDB 를
#   읽을 수 있으면 그 위임의 opened_at·ended_at 을 앞뒤 TURN_WINDOW_PAD 초(기본 0.5)씩 넓힌 것이다. 플랫폼은 런타임을
#   부르기 전에 opened_at 을 적고, 런타임은 턴의 마지막 Tool 호출 뒤에 /end 를 불러 플랫폼이 ended_at 을 적는다. 감사 시각은
#   프록시가 호출을 마친 때다. 두 시각을 적는 파드가 다른 노드에 있으면 노드의 시계 차이만큼 어긋나므로 그만큼 넓히고,
#   이웃한 턴은 그보다 멀리 떨어져 있다. 끝이 아직 적히지 않았으면 다섯 번 더 1초 간격으로 읽고 그래도 없으면 die STEP 이다
#   (턴이 끝났는데 위임이 열려 있는 것은 결함이다). 위임이 없거나(oidc 의 Gateway 소스) 기록을 읽을 수 없으면 턴 앞뒤의
#   cluster_now(BEFORE, AFTER) 그대로다. GRANT_ID 를 주었는데 그 위임이 없으면 die STEP
# platform_mongo_here
#   플랫폼 네임스페이스에 MongoDB 파드 mongodb-0 이 있으면 0 이다. 바깥 MongoDB(MONGO=external)면 기록을 보는 단계가
#   조건부로 건너뛴다
# platform_mongo JS
#   플랫폼 DB(ConfigMap 의 DAEGAM_MONGO_DB)에서 mongosh 가 JS 를 돌린 출력을 찍는다. 문서 몇 건을 읽거나 쓰는 데만 쓴다.
#   작은 노드의 MongoDB 에도 돌도록 훑기나 부하를 싣지 않는다
# turn_grants_of AGENT_ID SESSION_ID OUT
#   그 세션의 위임 기록을 opened_at 차례의 JSON 목록으로 OUT 에 쓴다. 칸은 _id·sub·trigger·decided_by·session_owner·
#   session_id·targets·opened_at·renewals·ended_at·end_reason 이다. 읽지 못하면 0 이 아니다
# human_token SUB [ARG...]
#   사람 발급자 LAB_ISSUER 와 daegam-mcp 로 TENANT 의 실험 토큰 한 줄을 플랫폼 서명 키로 서명해 찍는다(svc_token,
#   --human-sub SUB). ARG 는 deploy/dev/lab/svc_token.py 에 그대로 간다(--act·--bound-to·--groups·--no-groups. 뒤의
#   --tenant 가 앞의 것을 이긴다). 토큰으로 부르는 mcp_admits·mcp_refuses 는 mcp_rpc.sh 에 있다
# turn_member_developer STEP
#   관리자(GET /me 의 sub)를 TENANT 의 manual developer 로 둔다. 위임은 Agent 의 테넌트를 쥔 소속 행이 있는 사람에게만
#   열리고 platform-admin 의 * 행은 세지 않기 때문이다(P53 2절). ADMIN_SUB 와 앞의 역할 TURN_ROLE_BEFORE(없었으면 빈 값)를
#   남기고 TURN_MEMBER_SET=1 로 표시한다. 실패하면 die STEP
# turn_member_restore
#   TURN_MEMBER_SET 이면 관리자의 TENANT 소속을 TURN_ROLE_BEFORE 로 되돌린다(빈 값이면 뺀다). 실패하면 사람이 할 일을 찍고
#   표시를 남긴 채 1 이라 다시 부를 수 있다
# wait_gateway_gate STEP CLUSTER_ID
#   GET /settings/data-plane-auth 의 turn_tokens.clusters 에서 그 클러스터의 Gateway 게이트가 열리고 판정이 새것일 때까지
#   TURN_GATE_POLL 초(기본 5)마다 본다. 한도는 TURN_GATE_TIMEOUT(초, 기본 300)이고 넘기면 게이트를 붙든 문서와 까닭을 찍고
#   die STEP 이다
# audit_expect STEP SINCE SPEC...
#   GET /audit/mcp?type=mcp_tool_call&since=SINCE 를 10초마다 불러 SPEC 이 모두 맞을 때까지 본다. 수집기가 60초마다 프록시
#   로그를 읽기 때문이다. 한도는 AUDIT_TIMEOUT(초, 기본 210)이다. SPEC 은 `이름|since|until|instance_id|tool|sub|act|n` 에
#   `|outcome` 을 붙일 수 있고, 붙이면 그 outcome 의 기록만 센다(거절한 호출도 sub·act 를 싣고 denied 로 남는다).
#   n 이 1 이면 창 안에 그런 기록이 있어야 하고 0 이면 없어야 한다. tool 은 끝이 같으면 맞고(vMCP 는 Tool 이름에 백엔드
#   접두를 붙인다), sub·act 의 `*` 는 아무 값, act 의 `-` 는 act 가 없는 기록이다. 없어야 하는 SPEC 은 같은 instance_id
#   에 그 창보다 늦은 기록이 들어온 뒤에야 맞은 것으로 본다. 아니면 창마다 본 기록을 찍고 die STEP

cluster_now() {
  "${K[@]}" -n "$PLATFORM_NS" exec deploy/daegam-platform -c platform -- python -c \
    'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))'
}

grant_window() { # FILE MATCH PAD [OPEN_END]
  python3 - "$@" <<'PY'
import json, sys
from datetime import datetime, timedelta

def iso(t):
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

def when(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))

grants, (key, _, value), pad = json.load(open(sys.argv[1])), sys.argv[2].partition("="), timedelta(seconds=float(sys.argv[3]))
open_end = sys.argv[4] if len(sys.argv) > 4 else ""
if key == "id":
    g = next((g for g in grants if g.get("_id") == value), None)
else:
    g = next((g for g in grants if value in [t["kind"] + ":" + t["id"] for t in g.get("targets") or []]), None)
if g:
    opened = when(g["opened_at"])
    if g.get("ended_at"):
        until = iso(when(g["ended_at"]) + pad)
    else:
        until = iso(opened + timedelta(seconds=float(open_end)) + pad) if open_end else "-"
    print(iso(opened - pad), until)
PY
}

# shellcheck disable=SC2034  # TURN_SINCE·TURN_UNTIL 은 부르는 쪽이 읽는다
turn_window() { # STEP AGENT_ID SESSION_ID GRANT_ID BEFORE AFTER
  local i window=""
  if [ -n "$4" ] && platform_mongo_here; then
    for ((i = 0; i < 6; i++)); do
      turn_grants_of "$2" "$3" "$WORK/window-grants.json" || die "$1" "reading the turn grants of session $3 failed"
      window="$(grant_window "$WORK/window-grants.json" "id=$4" "${TURN_WINDOW_PAD:-0.5}")" \
        || die "$1" "reading the opened_at and ended_at of grant $4 failed: $(cat "$WORK/window-grants.json")"
      [ -n "$window" ] || die "$1" "the turn's grant $4 is not among the grants of session $3: $(cat "$WORK/window-grants.json")"
      [ "${window#* }" = - ] || break
      [ "$i" -eq 5 ] || sleep 1
    done
    [ "${window#* }" != - ] \
      || die "$1" "the turn ended but its grant $4 is still open after 5s: $(cat "$WORK/window-grants.json")"
    read -r TURN_SINCE TURN_UNTIL <<<"$window"
    return 0
  fi
  { [ -n "$5" ] && [ -n "$6" ]; } || die "$1" "reading the platform pod's clock failed (before '$5', after '$6')"
  TURN_SINCE="$5"
  TURN_UNTIL="$6"
}

platform_mongo_here() {
  "${K[@]}" -n "$PLATFORM_NS" get pod mongodb-0 -o name >/dev/null 2>&1
}

platform_mongo() { # JS
  local db
  db="$(platform_config DAEGAM_MONGO_DB)" || return 1
  "${K[@]}" -n "$PLATFORM_NS" exec mongodb-0 -c mongo -- sh -c \
    'mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin "$1" --eval "$2"' \
    _ "${db:-daegam}" "$1"
}

turn_grants_of() { # AGENT_ID SESSION_ID OUT
  local js
  js="$(python3 -c 'import json, sys
query = {"agent_id": sys.argv[1], "session_id": sys.argv[2]}
fields = dict.fromkeys(("sub", "trigger", "decided_by", "session_owner", "session_id", "targets", "opened_at",
                        "renewals", "ended_at", "end_reason"), 1)
print("print(JSON.stringify(db.turn_grants.find(%s, %s).sort({opened_at: 1}).toArray()))"
      % (json.dumps(query), json.dumps(fields)))' "$1" "$2")" || return 1
  platform_mongo "$js" >"$3" || return 1
  json_of "$3" 'len(d)' >/dev/null
}

human_token() { # SUB [ARG...]
  local sub="$1"
  shift
  svc_token "" --human-sub "$sub" --issuer "$LAB_ISSUER" --audience daegam-mcp "$@"
}

turn_member_developer() { # STEP
  local code
  code="$(api GET /me)"
  [ "$code" = 200 ] || die "$1" "GET /me -> $code"
  ADMIN_SUB="$(jq_ 'd["sub"]')"
  code="$(api GET "/tenants/$TENANT/members")"
  [ "$code" = 200 ] || die "$1" "GET /tenants/$TENANT/members -> $code"
  TURN_ROLE_BEFORE="$(jq_ "next((m['role'] for m in d if m['source'] == 'manual' and m['sub'] == '$ADMIN_SUB'), '')")"
  TURN_MEMBER_SET=1
  local_set_member "$TENANT" "$ADMIN_SUB" developer || die "$1" "making the admin $ADMIN_SUB a developer of $TENANT failed"
}

turn_member_restore() {
  [ "${TURN_MEMBER_SET:-0}" = 1 ] || return 0
  if ! local_set_member "$TENANT" "$ADMIN_SUB" "$TURN_ROLE_BEFORE"; then
    echo "!! restore the admin's membership of $TENANT (role '${TURN_ROLE_BEFORE:-none}') with PUT $API/tenants/$TENANT/members"
    return 1
  fi
  TURN_MEMBER_SET=0
}

wait_gateway_gate() { # STEP CLUSTER_ID
  local i code row="" gate='next((g for g in (d.get("turn_tokens") or {}).get("clusters") or [] if g.get("cluster_id") == "'"$2"'"), None)'
  for ((i = 0; i < ${TURN_GATE_TIMEOUT:-300} / ${TURN_GATE_POLL:-5}; i++)); do
    code="$(api GET /settings/data-plane-auth)"
    if [ "$code" = 200 ]; then
      row="$(jq_ "json.dumps($gate)")"
      [ "$(jq_ "(lambda g: g.get('gateway_gate_open') is True and g.get('verdict_fresh') is True)($gate or {})")" = True ] && return 0
    fi
    sleep "${TURN_GATE_POLL:-5}"
  done
  die "$1" "the Gateway gate of cluster $2 did not open within ${TURN_GATE_TIMEOUT:-300}s (GET /settings/data-plane-auth -> $code): $row"
}

audit_expect() { # STEP SINCE SPEC...
  local step="$1" since="$2" i code out=""
  shift 2
  for ((i = 0; i < ${AUDIT_TIMEOUT:-210} / 10; i++)); do
    code="$(API_OUT="$WORK/audit.json" api GET "/audit/mcp?type=mcp_tool_call&since=$since&limit=1000")"
    if [ "$code" = 200 ]; then
      out="$(python3 - "$WORK/audit.json" "$@" <<'PY'
import json, re, sys
from datetime import datetime, timezone

def when(value):
    # ToolHive 는 나노초까지 적는다. 마이크로초까지만 읽고 시간대가 없으면 UTC 로 본다
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", value or "")
    if not m:
        return None
    frac = (m.group(2) or ".")[1:7].ljust(6, "0")
    zone = m.group(3) or "Z"
    return datetime.fromisoformat(f"{m.group(1)}.{frac}" + ("+00:00" if zone == "Z" else zone)).astimezone(timezone.utc)

events = [e for e in json.load(open(sys.argv[1])) if when(e.get("ts"))]
ok, lines = True, []
for spec in sys.argv[2:]:
    name, since, until, instance, tool, sub, act, want, *outcome = spec.split("|")
    lo, hi = when(since), when(until)
    on_instance = [e for e in events if e.get("instance_id") == instance]
    window = [e for e in on_instance if lo <= when(e["ts"]) <= hi and (e.get("tool") or "").endswith(tool)]
    hits = [e for e in window if sub in ("*", e.get("sub")) and (act == "*" or (e.get("act") or "-") == act)
            and (not outcome or e.get("outcome") == outcome[0])]
    if want == "1":
        good = bool(hits)
    else:
        # 수집기가 창 뒤의 기록까지 읽었어야 없음을 믿는다
        good = not hits and any(when(e["ts"]) > hi for e in on_instance)
    ok = ok and good
    seen = sorted({(e.get("sub"), e.get("act"), e.get("outcome")) for e in window}, key=str)
    wanted = f"sub={sub} act={act}" + (f" outcome={outcome[0]}" if outcome else "")
    lines.append(f"{name}: {'ok' if good else 'missing' if want == '1' else 'unexpected or not read yet'} "
                 f"(want {'a' if want == '1' else 'no'} {tool} {wanted}; seen sub/act/outcome {seen})")
print("; ".join(lines))
sys.exit(0 if ok else 3)
PY
)" && { echo "  audit: $out"; return 0; }
    fi
    sleep 10
  done
  audit_pods || true
  die "$step" "MCP audit after ${AUDIT_TIMEOUT:-210}s (GET /audit/mcp -> $code): $out"
}

# audit_pods: 기대한 감사 기록이 없을 때 그 까닭을 가르게 프록시 파드마다 만든 시각과 재시작 수, 로그에 남은 Tool 호출 감사
# 줄(시각, Tool, 결과, 행위자)을 찍는다. 수집기는 파드가 바뀌면 옛 파드의 마지막 폴링 뒤 줄을 잃는다(audit_collector.py)
audit_pods() {
  local ns pod made restarts
  echo "  audit: proxy pods and the tool call lines in their logs"
  "${K[@]}" get pods -A -l toolhive=true --no-headers \
    -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,MADE:.metadata.creationTimestamp,RESTARTS:.status.containerStatuses[0].restartCount 2>/dev/null |
    while read -r ns pod made restarts; do
      echo "  --- $ns/$pod made=$made restarts=$restarts"
      "${K[@]}" -n "$ns" logs "$pod" --all-containers --tail=-1 </dev/null 2>/dev/null | python3 -c '
import json, sys
for line in sys.stdin:
    if "audit_id" not in line:
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("type") != "mcp_tool_call":
        continue
    chain = [a.get("sub") for a in (e.get("delegation") or {}).get("chain") or []]
    print("     ", e.get("logged_at"), (e.get("target") or {}).get("name"), e.get("outcome"),
          (e.get("subjects") or {}).get("user_id"), chain)
' | tail -n 20 || true
    done
}
