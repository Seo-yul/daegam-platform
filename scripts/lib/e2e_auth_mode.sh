# shellcheck shell=bash
# 인증 모드를 바꾸고 되돌린다 (P32 8절). mock-oidc 가 내는 oidc 모드의 토큰도 여기서 받는다.
# `. "$HERE/lib/e2e_auth_mode.sh"` 로 읽는다. 부르는 쪽이 K(kubectl 배열)·CTX·PLATFORM_NS·WORK·BASE·API 를 두고
# e2e_control_plane.sh 와 e2e_json.sh 를 먼저 읽는다. user_token·id_token 을 부르는 쪽은 OIDC(mock-oidc 의 port-forward
# 주소)도 둔다.
#
# 모드를 바꾸면 플랫폼이 모든 클러스터의 MCPOIDCConfig 와 살아 있는 소비자(Gateway·노출 MCP Server·노출이나 Gateway
# 소스를 쓰는 Agent)를 스스로 다시 적용하고, 소비자는 한 틱 열 개씩 굴러간다. 그래서 모드를 바꾼 게이트는 상태 API 가
# 다 됐다고 말할 때까지 기다린 뒤 다음 단계로 간다.
#
# switch_auth_mode MODE [KEY=VALUE...]
#   ConfigMap 의 DAEGAM_AUTH_MODE 를 MODE 로, 덧붙인 키를 그 값으로 바꾸고 제어면(플랫폼·UI)을 굴린다. 바꾸기 전의
#   값(없던 키는 없음)을 기억한다. oidc 면 토큰을 낼 mock-oidc 가 있는지 먼저 본다. local 이면 부르는 쪽의
#   LOCAL_BOOT_PW 를 Secret 의 부트스트랩 비밀번호로 넣는다. 실패하면 0 이 아니다. AUTH_MODE_PREV 에 앞의 모드가 남는다
# restore_auth_mode [TOKEN]
#   기억한 값으로 되돌리고(넣은 비밀번호는 걷는다) 굴린 뒤 TOKEN 으로 wait_auth_reapplied 를 부른다. 실패해도 멈추지
#   않고 사람이 칠 명령을 찍는다. EXIT trap 에서 부르고, API port-forward 는 그 뒤에 닫는다
# auth_mode_restore_hint
#   되돌리지 않고 모드를 남기는 게이트(E2E_PAT_KEEP_MODE=1)가 부른다. restore_auth_mode 가 할 일을 사람이 칠 명령으로 찍는다
# wait_auth_reapplied [TOKEN] [SINCE]
#   GET /api/settings/data-plane-auth 를 5초마다 불러 pending=0 failed=0 이고 tick_at 이 SINCE(UTC, 기본은 마지막
#   전환이 끝난 시각) 뒤인 것을 기다린다. TOKEN 은 관리자 토큰이고 header 모드면 비운다. 기다리는 한도는
#   E2E_AUTH_REAPPLY_TIMEOUT(초, 기본 600)이다. $BASE/healthz 가 닿지 않거나 상태 API 가 401·403 이면 기다려도 답이
#   바뀌지 않으므로 바로 돌아온다. 0 이 아니면 까닭과 사람이 칠 확인 명령을 스스로 찍는다
# mock_oidc_deployed
#   PLATFORM_NS 에 mock-oidc 가 있으면 0 이다. oidc 로 바꾸는 e2e 는 모두 그것이 토큰을 낸다
# user_token CLAIMS_JSON
#   mock-oidc 의 /_test/token 에 클레임을 보내 서명된 access_token 을 찍는다. aud 의 기본값은 데이터 플레인의 daegam-mcp
#   라서 플랫폼 API 용 토큰은 "aud": "daegam-api" 를 싣는다. 실패하면 0 이 아니다
# id_token SUB AUD AUTH_OFFSET
#   mock-oidc 가 서명한 SUB 의 ID 토큰(typ ID, audience AUD)을 찍는다. auth_time 은 지금에서 AUTH_OFFSET 초 옮긴 때다.
#   플랫폼이 최근 로그인으로 읽는다(X-Daegam-Id-Token). 실패하면 0 이 아니다
#
# 바꾸고 되돌리는 ConfigMap 의 이름(AUTH_MODE_CONFIGMAP)과 그 값을 읽는 platform_config 는 e2e_control_plane.sh 에 있다.

AUTH_MODE_PREV=""          # 바꾸기 전의 DAEGAM_AUTH_MODE (없었으면 빈 값)
AUTH_MODE_RESTORE=""       # 되돌릴 ConfigMap 병합 패치. 비면 되돌릴 것이 없다
AUTH_MODE_SECRET=0         # local 로 바꾸며 Secret 에 부트스트랩 비밀번호를 넣었으면 1
AUTH_MODE_SWITCHED_AT=""   # 마지막 전환(되돌리기 포함)이 끝난 시각. 옛 파드가 다 내려간 뒤다
AUTH_API_UP_TRIES=15       # 되돌린 직후 port-forward 가 새 파드로 다시 붙는 동안 healthz 를 다시 보는 횟수(1초 간격)
# 되돌릴 때 Secret 에서 걷는 부트스트랩 비밀번호. 값이 남으면 다음 기동이 그것으로 관리자를 다시 만든다
AUTH_MODE_DROP_SECRET='{"data": {"DAEGAM_LOCAL_BOOTSTRAP_PASSWORD": null}}'

mock_oidc_deployed() {
  "${K[@]}" -n "$PLATFORM_NS" get deploy/mock-oidc -o name >/dev/null 2>&1
}

user_token() { # CLAIMS_JSON
  curl -fsS -X POST "$OIDC/_test/token" -H 'Content-Type: application/json' -d "$1" | json_of - 'd["access_token"]'
}

id_token() { # SUB AUD AUTH_OFFSET
  curl -fsS -X POST "$OIDC/_test/token" -H 'Content-Type: application/json' \
    -d "{\"sub\": \"$1\", \"aud\": \"$2\", \"id_token\": true, \"auth_time_offset\": $3}" | json_of - 'd["id_token"]'
}

_auth_mode_patch_secret() { # JSON 병합 패치
  "${K[@]}" -n "$PLATFORM_NS" patch secret daegam-platform-secrets --type merge -p "$1" >/dev/null
}

switch_auth_mode() { # MODE [KEY=VALUE...]
  local mode="$1" patches
  shift
  if [ "$mode" = oidc ] && ! mock_oidc_deployed; then
    echo "!! mock-oidc is not deployed in $PLATFORM_NS (make image-mock-oidc && kubectl apply -f deploy/dev/mock-oidc.yaml)"
    return 1
  fi
  # 바꾸는 패치와 되돌리는 패치(config_patches)다. 모드 키가 없던 배포본은 header 로 되돌린다
  patches="$(config_patches "DAEGAM_AUTH_MODE=$mode" "$@")" || return 1
  AUTH_MODE_PREV="$(json_of "$WORK/platform-config.json" '(d.get("data") or {}).get("DAEGAM_AUTH_MODE") or ""')"
  AUTH_MODE_RESTORE="$(printf '%s\n' "$patches" | sed -n 2p)"
  echo ">> ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=${AUTH_MODE_PREV:-(unset)} -> $mode $* (restored on exit)"
  if [ "$mode" = local ]; then
    AUTH_MODE_SECRET=1
    _auth_mode_patch_secret "{\"stringData\": {\"DAEGAM_LOCAL_BOOTSTRAP_PASSWORD\": \"${LOCAL_BOOT_PW:?}\"}}" || return 1
  fi
  "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge \
    -p "$(printf '%s\n' "$patches" | sed -n 1p)" >/dev/null || return 1
  roll_control_plane || return 1
  AUTH_MODE_SWITCHED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"
}

restore_auth_mode() { # [TOKEN]
  [ -n "$AUTH_MODE_RESTORE" ] || return 0
  local patch="$AUTH_MODE_RESTORE"
  AUTH_MODE_RESTORE=""
  echo ">> restoring ConfigMap $AUTH_MODE_CONFIGMAP $patch and rolling the control plane (platform, UI)"
  if [ "$AUTH_MODE_SECRET" = 1 ]; then
    AUTH_MODE_SECRET=0
    _auth_mode_patch_secret "$AUTH_MODE_DROP_SECRET" \
      || echo "!! secret restore failed. run: kubectl --context $CTX -n $PLATFORM_NS patch secret daegam-platform-secrets --type merge -p '$AUTH_MODE_DROP_SECRET'"
  fi
  if ! "${K[@]}" -n "$PLATFORM_NS" patch configmap "$AUTH_MODE_CONFIGMAP" --type merge -p "$patch" >/dev/null; then
    echo "!! restore failed. run: kubectl --context $CTX -n $PLATFORM_NS patch configmap $AUTH_MODE_CONFIGMAP --type merge -p '$patch' && kubectl --context $CTX -n $PLATFORM_NS rollout restart deploy/daegam-platform deploy/daegam-ui"
    return 0
  fi
  if ! roll_control_plane >/dev/null 2>&1; then
    echo "!! control plane rollout after the restore did not finish: kubectl --context $CTX -n $PLATFORM_NS rollout status deploy/daegam-platform deploy/daegam-ui"
    return 0
  fi
  AUTH_MODE_SWITCHED_AT="$(date -u +%Y-%m-%dT%H:%M:%S)"
  echo ">> ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=$(platform_config DAEGAM_AUTH_MODE 2>/dev/null)"
  # 기다리지 못한 까닭과 확인 명령은 wait_auth_reapplied 가 찍는다
  wait_auth_reapplied "${1:-}" || true
}

auth_mode_restore_hint() {
  [ -n "$AUTH_MODE_RESTORE" ] || return 0
  local k="kubectl --context $CTX -n $PLATFORM_NS"
  echo ">> DAEGAM_AUTH_MODE=$(platform_config DAEGAM_AUTH_MODE 2>/dev/null) is left in place. restore it with:"
  [ "$AUTH_MODE_SECRET" = 1 ] && echo "   $k patch secret daegam-platform-secrets --type merge -p '$AUTH_MODE_DROP_SECRET'"
  echo "   $k patch configmap $AUTH_MODE_CONFIGMAP --type merge -p '$AUTH_MODE_RESTORE'"
  echo "   $k rollout restart deploy/daegam-platform deploy/daegam-ui"
}

wait_auth_reapplied() { # [TOKEN] [SINCE]
  local token="${1:-}" since="${2:-$AUTH_MODE_SWITCHED_AT}" limit="${E2E_AUTH_REAPPLY_TIMEOUT:-600}" code i
  local auth=() check="check by hand: GET $API/settings/data-plane-auth"
  [ -z "$token" ] || auth=(-H "Authorization: Bearer $token")
  if ! _auth_api_up; then
    echo "!! $BASE/healthz is not reachable, so the data-plane auth reapply was not awaited; $check"
    return 1
  fi
  for ((i = 0; i < limit / 5; i++)); do
    code="$(curl -sS -o "$WORK/auth-reapply.json" -w '%{http_code}' "$API/settings/data-plane-auth" \
      ${auth[@]+"${auth[@]}"} 2>/dev/null)" || code=000
    case "$code" in
      200) _auth_reapply_settled "$since" && return 0 ;;
      401 | 403) echo "!! GET $API/settings/data-plane-auth -> $code with the given token; $check"; return 1 ;;
    esac
    sleep 5
  done
  echo "!! data-plane auth did not settle within ${limit}s: $(cat "$WORK/auth-reapply.json" 2>/dev/null); $check"
  return 1
}

# _auth_api_up -> 0 이면 $BASE/healthz 가 200 이다. 되돌린 직후에는 port-forward 가 새 파드로 다시 붙는 중일 수 있어
# AUTH_API_UP_TRIES 번까지 다시 본다
_auth_api_up() {
  wait_http "$BASE/healthz" "$AUTH_API_UP_TRIES"
}

# _auth_reapply_settled SINCE -> 0 이면 요약이 pending=0 failed=0 이고 tick_at 이 SINCE 뒤다. 두 시각은 같은 꼴의
# UTC 라 초까지 글자로 견준다
_auth_reapply_settled() {
  python3 -c 'import json,sys
s = json.load(open(sys.argv[1])).get("summary")
ok = bool(s) and s["pending"] == 0 and s["failed"] == 0 and s["tick_at"][:19] > sys.argv[2]
sys.exit(0 if ok else 1)' "$WORK/auth-reapply.json" "$1"
}
