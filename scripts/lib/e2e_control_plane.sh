# shellcheck shell=bash
# 제어면 둘(플랫폼·UI)을 함께 굴린다 (D52). `. "$HERE/lib/e2e_control_plane.sh"` 로 읽는다.
#
# UI(daegam-ui)의 CSP 는 기동 때 ConfigMap daegam-platform-config 의 DAEGAM_AUTH_MODE·DAEGAM_OIDC_ISSUER 로 정해진다.
# 그래서 인증 모드를 바꾸면 플랫폼만이 아니라 UI 도 재시작해야 한다. 빠뜨리면 oidc 로 바꾼 뒤에도 CSP 에 IdP 가
# 없어 브라우저가 로그인 연결을 막는다. 호출자는 K(kubectl 배열)와 PLATFORM_NS 를 갖고 있다.
#
# roll_control_plane [플랫폼 대기 초]   둘을 재시작하고, 둘 다 롤아웃이 끝나고 옛 파드가 다 내려갈 때까지 기다린다.
#                                       실패하면 0 이 아니다
# platform_config KEY                   플랫폼 ConfigMap(AUTH_MODE_CONFIGMAP)의 그 값을 찍는다. 없으면 빈 문자열이다
# config_patches KEY=VALUE...           두 줄을 찍는다. 첫 줄은 그 키들을 그 값으로 두는 ConfigMap 병합 패치, 둘째 줄은 읽은
#                                       때의 값으로 되돌리는 패치다. 없던 키는 null 로 지우되, 모드 키가 없던 배포본은
#                                       header 로 되돌린다(실측 뒤 배포본은 header 여야 한다). 읽은 ConfigMap 은
#                                       $WORK/platform-config.json 에 남는다. 읽지 못하면 0 이 아니다
# agent_pods NS ID                      그 Agent 의 파드 가운데 지워지는 중이 아닌 것의 이름을 한 줄에 하나 찍는다
# restart_platform                      플랫폼만 다시 띄우고 롤아웃과 옛 파드가 내려가기를 기다린다. 붙는 길(port-forward,
#                                       공개 주소)을 다시 확인하는 것은 부르는 쪽이 한다. 실패하면 `!!` 줄을 찍고 1 이다
# roll_platform_api                     restart_platform 뒤 $API/version 이 답할 때까지 기다린다(e2e_pf.sh 의 wait_http).
#                                       부르는 쪽이 API 를 둔다. 실패하면 `!!` 줄을 찍고 1 이다
#
# 게이트 동안만 ConfigMap 키를 바꾸는 창 (e2e_install STEP 8t, e2e_runs STEP 0). 되돌릴 것과 사람이 칠 명령을 먼저 세우고, 고친
# 뒤 플랫폼을 굴린다. 되돌리기는 정리 단계가 다시 부를 수 있다. 부르는 쪽이 CTX 를 둔다
# config_window_plan KEY=VALUE...       고칠 패치와 되돌릴 패치를 CONFIG_APPLY·CONFIG_RESTORE 에, 되돌릴 패치를 사람이 칠
#                                       명령을 CONFIG_RESTORE_HINT 에 둔다. ConfigMap 은 아직 고치지 않는다. 읽지 못하면 1
# config_window_apply                   CONFIG_APPLY 로 ConfigMap 을 고친다. 실패하면 0 이 아니다
# config_window_restore                 CONFIG_RESTORE 가 있으면 그것으로 되돌리고 비운 뒤 CONFIG_ROLL_PENDING=1 로 플랫폼을
#                                       굴려야 한다고 적는다. 되돌리지 못하면 CONFIG_RESTORE_HINT 를 찍고 1 이다

# 플랫폼 ConfigMap 의 이름. 인증 모드도 여기에 있어 lib/e2e_auth_mode.sh 가 이 이름으로 바꾸고 되돌린다
AUTH_MODE_CONFIGMAP="${E2E_PLATFORM_CONFIGMAP:-daegam-platform-config}"

platform_config() { # KEY
  "${K[@]}" -n "$PLATFORM_NS" get configmap "$AUTH_MODE_CONFIGMAP" -o jsonpath="{.data.$1}"
}

config_patches() { # KEY=VALUE...
  "${K[@]}" -n "$PLATFORM_NS" get configmap "$AUTH_MODE_CONFIGMAP" -o json >"$WORK/platform-config.json" || return 1
  python3 -c 'import json,sys
data = json.load(open(sys.argv[1])).get("data") or {}
pairs = dict(a.split("=", 1) for a in sys.argv[2:])
back = {k: data.get(k) for k in pairs}
if "DAEGAM_AUTH_MODE" in pairs:
    back["DAEGAM_AUTH_MODE"] = back["DAEGAM_AUTH_MODE"] or "header"
print(json.dumps({"data": pairs}))
print(json.dumps({"data": back}))' "$WORK/platform-config.json" "$@"
}

roll_control_plane() {
  local wait="${1:-240}"
  "${K[@]}" -n "$PLATFORM_NS" rollout restart deploy/daegam-platform deploy/daegam-ui >/dev/null || return 1
  "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout="${wait}s" >/dev/null || return 1
  "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-ui --timeout=120s >/dev/null || return 1
  # rollout status 는 옛 파드가 preStop 으로 내려가는 동안에도 끝나고, 그 사이 Traefik 은 옛 파드에도 요청을 보낸다
  # (2026-09-24 실측: 돌아온 직후 한 요청이 옛 모드로 답했다). 그래서 옛 파드가 다 지워질 때까지 기다린다
  wait_no_terminating daegam-platform || return 1
  wait_no_terminating daegam-ui || return 1
}

# wait_no_terminating APP  플랫폼 네임스페이스의 app=APP 파드 가운데 내려가는 중인 것이 없어질 때까지 기다린다
wait_no_terminating() { wait_pods_settled "$PLATFORM_NS" "app=$1"; }

restart_platform() {
  "${K[@]}" -n "$PLATFORM_NS" rollout restart deploy/daegam-platform >/dev/null \
    || { echo "!! kubectl rollout restart deploy/daegam-platform failed"; return 1; }
  "${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=300s >/dev/null \
    || { echo "!! deploy/daegam-platform did not finish rolling out within 300s"; return 1; }
  wait_no_terminating daegam-platform || { echo "!! the old platform pod did not go away after the rollout"; return 1; }
}

roll_platform_api() {
  restart_platform || return 1
  wait_http "$API/version" 120 || { echo "!! the platform did not answer at $API/version after the rollout"; return 1; }
}

CONFIG_APPLY=""          # 창을 여는 병합 패치
CONFIG_RESTORE=""        # 창을 닫는 병합 패치. 비면 되돌릴 것이 없다
CONFIG_RESTORE_HINT=""   # 그 패치를 사람이 칠 명령. 고치기 전에 찍는다
CONFIG_ROLL_PENDING=0    # ConfigMap 은 되돌렸고 플랫폼을 아직 굴리지 못했다

config_window_plan() { # KEY=VALUE...
  local patches
  patches="$(config_patches "$@")" || return 1
  CONFIG_APPLY="$(printf '%s\n' "$patches" | sed -n 1p)"
  CONFIG_RESTORE="$(printf '%s\n' "$patches" | sed -n 2p)"
  CONFIG_RESTORE_HINT="restore by hand: kubectl --context $CTX -n $PLATFORM_NS patch configmap $AUTH_MODE_CONFIGMAP --type merge -p '$CONFIG_RESTORE' && kubectl --context $CTX -n $PLATFORM_NS rollout restart deploy/daegam-platform"
}

config_window_apply() {
  "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p "$CONFIG_APPLY" >/dev/null
}

config_window_restore() {
  [ -n "$CONFIG_RESTORE" ] || return 0
  # shellcheck disable=SC2034  # CONFIG_ROLL_PENDING 은 부르는 쪽이 읽는다
  if "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p "$CONFIG_RESTORE" >/dev/null; then
    CONFIG_RESTORE=""; CONFIG_ROLL_PENDING=1
    return 0
  fi
  echo "!! the ConfigMap restore failed. $CONFIG_RESTORE_HINT"
  return 1
}

# Agent 파드의 셀렉터 라벨 (k8s/templates/agent.py selector_labels). 값 뒤에 인스턴스 id 를 붙인다
AGENT_POD_SELECTOR="daegam.devops.ai.kr/resource=agent-instance,daegam.devops.ai.kr/id="

# agent_pods NS ID  jsonpath 는 없는 필드를 거르지 못해 이름과 삭제 표시를 함께 뽑아 셸에서 가른다
agent_pods() {
  "${K[@]}" -n "$1" get pods -l "$AGENT_POD_SELECTOR$2" \
    -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.deletionTimestamp}{"\n"}{end}' | awk 'NF == 1 {print $1}'
}

# Agent 파드의 기본 grace(preStop 5 + drain 120 + 마감 45 + 여유 10, P78 5절)에 여유를 더한 초. 도는 턴이 있는 파드는
# drain 이 끝날 때까지 내려가므로 그만큼 기다린다. 한가한 파드는 몇 초 안에 내려간다
POD_SETTLE_SECONDS="${E2E_POD_SETTLE_SECONDS:-190}"

# wait_pods_settled NS SELECTOR  그 파드 가운데 내려가는 중인 것이 없어질 때까지 POD_SETTLE_SECONDS 초 기다린다. kubectl 이
# 실패하면 아직 모르는 것이므로 다시 본다(옛 파드가 없다고 읽지 않는다)
wait_pods_settled() {
  local out
  for _ in $(seq 1 "$POD_SETTLE_SECONDS"); do
    if out="$("${K[@]}" -n "$1" get pod -l "$2" \
      -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}{"\n"}{end}' 2>/dev/null)"; then
      [ -z "$(printf '%s' "$out" | tr -d '\n')" ] && return 0
    fi
    sleep 1
  done
  return 1
}
