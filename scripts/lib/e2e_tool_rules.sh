# shellcheck shell=bash
# Tool 규칙(P52) e2e 도우미. e2e_agent 의 11r 이 쓴다. api·jq_·die·wait_ready(lib/e2e_api.sh·e2e_report.sh), mcp·mcp_json
# (lib/mcp_rpc.sh), K·WORK·HERE·READY_TIMEOUT 을 쓴다.
#
#   rules_save STEP MS_ID RULES_JSON [OVERRIDES_JSON]
#       그 MCP Server 의 첫 backend 의 규칙(과 오버라이드)만 바꾼 새 리비전을 저장한다(lib/rules_body.py). 저장의 답은
#       $WORK/rules-saved.json 이다. 규칙을 비우면('{}') 규칙이 없는 정의다
#   rules_redeploy STEP MS_ID
#       재배포를 부른다. 기다리지 않는다(rules_settled 가 기다린다)
#   rules_settled STEP MS_ID
#       Ready 이고 배포 리비전이 지금 리비전이며, 서버 상태에 규칙이 가라앉지 않았다는 경고(conditions_closed·
#       conditions_unresolved·rules_toolhive_old·rules_front_rolling)가 없을 때까지 READY_TIMEOUT 초 기다린다. 조건이 있는
#       Tool 은 모든 파드가 조건을 지키는 이미지로 도는 것을 플랫폼이 확인할 때까지 닫혀 있다(4절)
#   rules_rolled MS_K8S_NAME NS
#       ToolHive 가 그 서버의 백엔드 StatefulSet 을 다 굴렸으면 0 이다(currentRevision == updateRevision, 모든 레플리카가 새 것)
#   tool_names TOKEN URL
#       그 토큰으로 본 tools/list 의 이름을 JSON 배열로 찍는다. 부르지 못하면 1 이다
#   call_refused TOKEN URL NAME
#       그 토큰의 NAME 호출(인자 a=3, b=4)이 결과를 받지 못했으면 0 이다

rules_save() {
  local step="$1" id="$2" code over=()
  [ -z "${4:-}" ] || over=(--overrides "$4")
  [ "$(api GET "/mcp-servers/$id")" = 200 ] || die "$step" "GET /mcp-servers/$id failed"
  cp "$WORK/body" "$WORK/rules-server.json"
  python3 "$HERE/lib/rules_body.py" "$WORK/rules-server.json" "$3" ${over[@]+"${over[@]}"} >"$WORK/rules-body.json" \
    || die "$step" "building the rules body for $id failed"
  code="$(api PUT "/mcp-servers/$id" "$(cat "$WORK/rules-body.json")")"
  [ "$code" = 200 ] || die "$step" "PUT /mcp-servers/$id with rules $3 -> $code: $(cat "$WORK/body")"
  cp "$WORK/body" "$WORK/rules-saved.json"
}

rules_redeploy() {
  local code
  code="$(api POST "/mcp-servers/$2/redeploy")"
  [ "$code" = 200 ] || die "$1" "POST /mcp-servers/$2/redeploy -> $code: $(cat "$WORK/body")"
}

rules_settled() {
  local step="$1" id="$2" state="" i
  wait_ready "/mcp-servers/$id" "$step"
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    [ "$(api GET "/mcp-servers/$id")" = 200 ] || die "$step" "GET /mcp-servers/$id failed"
    state="$(jq_ 'json.dumps([d["revision"], d["deployed_revision"], (d.get("status") or {}).get("phase"), [w.split(":")[0] for w in (d.get("status") or {}).get("warnings", []) if w.split(":")[0] in ("conditions_closed", "conditions_unresolved", "rules_toolhive_old", "rules_front_rolling")]])')"
    [ "$(json_of - 'd[0] == d[1] and d[2] == "Ready" and not d[3]' <<<"$state")" = True ] && return 0
    sleep 5
  done
  die "$step" "MCP Server $id did not settle within ${READY_TIMEOUT}s: [revision, deployed_revision, phase, rule warnings] = $state"
}

rules_rolled() {
  "${K[@]}" -n "$2" get sts -l "toolhive-name=$1" -o json >"$WORK/rules-sts.json" 2>/dev/null || return 1
  [ "$(json_of "$WORK/rules-sts.json" 'bool(d["items"]) and all(
    (s.get("status") or {}).get("currentRevision") == (s.get("status") or {}).get("updateRevision")
    and (s.get("status") or {}).get("updatedReplicas", 0) == s["spec"].get("replicas", 1)
    and (s.get("status") or {}).get("readyReplicas", 0) == s["spec"].get("replicas", 1) for s in d["items"])')" = True ]
}

tool_names() {
  [ "$(mcp --token "$1" list "$2")" = 0 ] || return 1
  mcp_json 'json.dumps(sorted(d["tools"]))'
}

call_refused() {
  [ "$(mcp --token "$1" call "$2" "$3" '{"a": 3, "b": 4}')" != 0 ]
}
