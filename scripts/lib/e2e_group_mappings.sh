# shellcheck shell=bash
# oidc 모드의 그룹 매핑을 실측 동안 더하고 되돌리는 한 벌 (e2e_agent·e2e_assistant). `. "$HERE/lib/e2e_group_mappings.sh"`
# 로 읽는다. 부르는 쪽이 WORK·API 와 관리자 AUTH 를 두고 e2e_json.sh·e2e_api.sh(api)·e2e_report.sh(die)를 먼저 읽는다.
# oidc 모드의 소속은 토큰의 그룹이 이 표를 거쳐 요청마다 맞춰진다.
#
# group_mappings_add STEP TENANT GROUP=ROLE...
#   지금 표를 $WORK/mappings.orig.json 에 받아 두고 TENANT 의 GROUP→ROLE 행을 더해 PUT 한다(PUT 은 전체 교체다). 앞선
#   실행이 되돌리지 못해 남긴 같은 (group, tenant) 행은 지우고 다시 넣는다. MAPPINGS_PUT=1 을 남긴다. 실패하면 die STEP
# group_mappings_restore
#   MAPPINGS_PUT 이 1 이면 받아 둔 표로 되돌린다. EXIT 트랩에서 부르므로 멈추지 않고, 실패하면 사람이 할 일을 찍는다

MAPPINGS_PUT=0   # group_mappings_add 가 표를 바꿨으면 1

group_mappings_add() { # STEP TENANT GROUP=ROLE...
  local step="$1" tenant="$2" code mappings
  shift 2
  code="$(api GET /group-mappings)"
  [ "$code" = 200 ] || die "$step" "GET /group-mappings -> $code"
  cp "$WORK/body" "$WORK/mappings.orig.json"
  mappings="$(python3 - "$WORK/mappings.orig.json" "$tenant" "$@" <<'PY'
import json, sys
rows, tenant, pairs = json.load(open(sys.argv[1])), sys.argv[2], [a.split("=", 1) for a in sys.argv[3:]]
added = [{"group": g, "tenant": tenant, "role": r} for g, r in pairs]
keys = {(m["group"], tenant) for m in added}
print(json.dumps([{k: m[k] for k in ("group", "tenant", "role")} for m in rows if (m["group"], m["tenant"]) not in keys] + added))
PY
)" || die "$step" "building the group mappings failed"
  MAPPINGS_PUT=1
  code="$(api PUT /group-mappings "$mappings")"
  [ "$code" = 200 ] || die "$step" "PUT /group-mappings -> $code $(cat "$WORK/body")"
}

group_mappings_restore() {
  [ "$MAPPINGS_PUT" = 1 ] || return 0
  MAPPINGS_PUT=0
  local body code
  body="$(python3 -c 'import json,sys; print(json.dumps([{k: m[k] for k in ("group","tenant","role")} for m in json.load(open(sys.argv[1]))]))' \
    "$WORK/mappings.orig.json" 2>/dev/null)" \
    || { echo "!! group mappings restore payload failed. PUT $API/group-mappings 로 손으로 되돌린다"; return 0; }
  code="$(api PUT /group-mappings "$body")" || true
  if [ "$code" = "200" ]; then echo ">> group mappings restored ($(json_of "$WORK/body" 'len(d)') rows)"
  else echo "!! group mappings restore: PUT /group-mappings -> $code. 손으로 되돌린다"; fi
}
