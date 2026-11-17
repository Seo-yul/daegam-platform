# shellcheck shell=bash
# shellcheck disable=SC2034  # 남기는 전역은 이 파일을 읽는 스크립트가 읽는다
# P54·P54b·P56 F0 도구가 함께 쓰는 한 벌. `. "$HERE/lib.sh"` 로 읽는다. 이 폴더(scripts/f0)와 .github/workflows 의
# p54-f0.yml·p56-f0.yml 은 F0 측정 동안만 둔다 (P54 12·13절, P54b 11·12절, P56 8·9절).
#
# 부르는 쪽이 lib/e2e_exit.sh 와 lib/e2e_json.sh 를 먼저 읽고, HERE(이 폴더)·ROOT·CTX·WORK·API_PORT·OIDC_PORT·TENANT·
# MCP_CLIENT_NAME 을 둔 뒤 읽는다. e2e 게이트의 한 벌(scripts/lib)을 그대로 읽어 쓰고, 종료 코드와 STEP 줄은
# lib/e2e_report.sh 의 계약이다. STEP 이름은 F0 항목이다(F0.1, F0.4 ...).
# 대상은 태그의 클러스터 e2e 잡처럼 만든 k3d 다(make deploy-local·lab-up, Traefik, ToolHive, cert-manager, mock-oidc).
# 호스트는 모두 .e2e.local 아래이고 Traefik 의 LoadBalancer IP 에 curl --resolve 나 f0_tool --ip 로 못 박는다.
#
# 결과. 항목마다의 결과 줄은 `F0.x | ...` 로 찍고 F0_OUT(기본 $TMPDIR/daegam-f0-out, 워크플로는 $RUNNER_TEMP/f0-results)의
# summary.md 에도 더한다. 측정 파일(probe JSON, loop 기록, 표)도 F0_OUT 에 두고 워크플로가 아티팩트로 올린다. 토큰과
# 비밀번호는 F0_OUT 에도 로그에도 두지 않는다. F1 의 상태(id, 개인 토큰, 로그인 연결)는 F1_STATE 에 0600 으로 두고
# 올리지 않는다.
#
# f0_note ITEM MSG...            결과 줄을 찍고 summary.md 에 더한다
# f0_block ITEM FILE             파일 하나를 코드 블록으로 찍고 summary.md 에 더한다
# f0_tool ARGS...                f0_tool.py 를 플랫폼 가상 환경에서 부른다
# f0_header_connect STEP         header 모드의 플랫폼에 붙는다 (PF_API, 빈 AUTH)
# f0_server_up STEP              daegam-f0 에 시험 서버(f0_server.py)를 플랫폼과 같은 이미지로 띄운다. 주소는 F0_HTTP·F0_SSE
#                                (클러스터 안)이다
# f0_server_log ITEM             시험 서버의 로그 끝을 F0_OUT/diag-f0-mcp.txt 에 남기고 찍는다
# f0_redact                      표준 입력에서 자격 증명처럼 보이는 값을 지워 내보낸다(redact.sh. 지우는 것은 그 파일 머리)
# tool_names FILE                f0_tool probe 결과 FILE 의 tools/list 이름을 정렬해 공백으로 이어 찍는다. 파일이나 행이 없으면
#                                빈 줄이다
# vmcp_diag ITEM NS VMCP [SELECTOR [SELECTOR_NS]]
#                                vMCP 가 백엔드를 어떻게 보는지 F0_OUT/diag-VMCP.txt 에 남기고 찍는다. VirtualMCPServer 의
#                                status(discoveredBackends), vMCP 파드 로그 중 감사 이벤트가 아닌 줄, SELECTOR 가 고르는
#                                백엔드 파드의 로그(SELECTOR_NS, 기본 NS)다. 모두 f0_redact 를 거친다
# f0_server_scale STEP N         시험 서버의 레플리카를 N 으로 두고 그만큼 Ready 이거나 0 이 될 때까지 기다린다
# f0_rest_backend STEP NAME      시험 서버의 REST 백엔드를 Backend API NAME 으로 올리고 BACKEND_API_ID·BACKEND_API_VERSION 을
#                                남긴다(lib/e2e_api.sh upload_backend_api)
# f0_exposed STEP PATH           MCP Server 문서의 노출이 주소를 받고 앞단이 Ready 일 때까지 기다린다. 끝나면 $WORK/body 가
#                                그 문서다
# f0_now                         지금 시각(epoch 초, 소수 셋째 자리)
# f0_loop_start DIR NAME URL TOOL ARGS
#                                f0_tool loop 를 뒤에서 돌린다. 기록은 F0_OUT/loop-NAME.jsonl, pid 와 멈춤 파일은 DIR 이다.
#                                F0_TOKEN 이 있으면 그 토큰으로 부른다
# f0_loop_stop DIR NAME SWITCH_AT [DELETED_AT]
#                                멈추고 SWITCH_AT 을 바꾼 때로 본 lag 요약을 F0_OUT/lag-NAME.txt 에 쓴다. 요약을 쓰지 못할
#                                때만 줄을 찍는다
# hub_item STEP NAME KIND URL TRANSPORT [ACCESS_JSON]
#                                허브 항목을 만들고(409 는 그 이름의 항목을 잇는다) ITEM_ID 를 남긴다. remote 면 READY_TIMEOUT
#                                까지 Ready 를 기다리고 그 phase 를 ITEM_PHASE 에 남긴다(Ready 가 아니어도 멈추지 않는다. 부르는
#                                쪽이 정한다). 끝에 refresh-tools 를 한 번 부른다. 만들지 못하면 ITEM_ID 가 비고 코드가
#                                ITEM_CODE 다
# share_with TENANT              허브 항목을 TENANT 에도 여는 access JSON 을 찍는다
# f0_oidc_session STEP           데이터 플레인이 IdP 를 믿는 oidc 모드(DAEGAM_DATA_PLANE_ISSUER=idp)로 바꾸고 플랫폼과
#                                mock-oidc 에 다시 붙어 관리자 토큰(ADMIN_TOKEN, AUTH)을 두고 재적용을 기다린 뒤 TENANT 의
#                                mcp_group 을 MCP_GROUP 으로 둔다. MODE_SWITCHED=1 이다. 측정은 IdP 토큰과 저장된 mcp_group
#                                으로 하므로 기본값(platform, D104)이 아니라 idp 를 박는다
# f0_restore_mode                바꾼 모드를 되돌리고(재적용을 API port-forward 로 기다린다) 두 port-forward 를 닫는다
#
# F1 의 상태. 단계(prepare·watch·check)마다 프로세스가 다르므로 F1_STATE 의 파일로 잇는다
# f1_state                       F1_STATE 를 0700 으로 만든다
# f1_get KEY / f1_put KEY VALUE  상태 값 하나를 읽고 쓴다(0600)
# f1_next_round                  설치 한 번의 판을 하나 올리고 그 번호를 F1_ROUND 에 남긴다
# f1_mark_install                watch 의 끝에서 부른다. 곧 시작할 설치의 시각을 그 판의 바꾼 때로 적는다
# f1_stop_loops PHASE NAME...    그 판의 루프 NAME 들을 멈추고 바꾼 때 기준의 lag 요약을 결과에 더한다
# f1_fail PHASE MSG              prepare 면 die 다(준비가 안 되면 리허설이 없다). check 면 세고(record_fail) 이어 간다. F1 이
#                                잡으려는 것이 설치 뒤의 실패라 그 뒤의 확인과 되돌림 단계를 잃지 않게 한다

LAB_PY=(uv run --quiet --project "$ROOT/platform" python)
F0_NS="${F0_NAMESPACE:-daegam-f0}"
F0_HTTP="http://f0-mcp.${F0_NS}.svc.cluster.local:8080"
F0_SSE="http://f0-mcp.${F0_NS}.svc.cluster.local:8081"
# 정리가 지우는 WORK 밖에 둔다. 로컬에서 F0_OUT 없이 돌려도 결과가 남는다
F0_OUT="${F0_OUT:-${TMPDIR:-/tmp}/daegam-f0-out}"
mkdir -p "$F0_OUT"
TARGET=orbstack   # lib/e2e_local_auth.sh 의 갈래. 로컬 클러스터(k3d)라 부트스트랩 관리자로 들어간다
TOKEN_DAYS=7      # lib/e2e_personal_tokens.sh 가 개인 토큰의 날수로 읽는다
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
BASE="http://127.0.0.1:${API_PORT}"
API="$BASE/api"
OIDC="http://127.0.0.1:${OIDC_PORT}"
K=(kubectl --context "$CTX")
AUTH=()
ADMIN_TOKEN=""; PF_API=""; PF_OIDC=""
MODE_SWITCHED=0   # 이 실행이 인증 모드를 바꿨으면 1. 부르는 쪽의 정리가 f0_restore_mode 를 부른다

# start_pf·stop_pf·wait_port (K·WORK)
. "$ROOT/scripts/lib/e2e_pf.sh"
# roll_control_plane·platform_config, switch_auth_mode·restore_auth_mode·wait_auth_reapplied·user_token
. "$ROOT/scripts/lib/e2e_control_plane.sh"
. "$ROOT/scripts/lib/e2e_auth_mode.sh"
# local_login_any·local_fresh_member·local_admin_session·platform_connect 와 비밀번호 셋
. "$ROOT/scripts/lib/e2e_local_auth.sh"
# pass·skip·die·finish
. "$ROOT/scripts/lib/e2e_report.sh"
# api·api_as·jq_·listed_id·api_delete·wait_ready·save_doc·deploy_ready·upload_backend_api
. "$ROOT/scripts/lib/e2e_api.sh"
# issue_ok·JWKS_CACHE_WAIT
. "$ROOT/scripts/lib/e2e_personal_tokens.sh"
# calc_mcp_server·calc_publish·calc_gateway (lab 의 calculator)
. "$ROOT/scripts/lib/e2e_calc.sh"
# rpc_curl·rpc_extract·rpc_·mcp_ingress·mcp
. "$ROOT/scripts/lib/mcp_rpc.sh"
# traefik_ip·pin·url_host·exposure·restart_pods
. "$ROOT/scripts/lib/e2e_mcp_access.sh"

f0_note() { # ITEM MSG...
  local item="$1"
  shift
  echo "$item | $*"
  printf -- '- **%s** %s\n' "$item" "$*" >>"$F0_OUT/summary.md"
}

f0_block() { # ITEM FILE
  echo "---- $1: $(basename "$2")"
  cat "$2"
  # shellcheck disable=SC2016  # 백틱은 마크다운의 코드 표시다
  { printf '\n**%s** `%s`\n\n```\n' "$1" "$(basename "$2")"; cat "$2"; printf '```\n\n'; } >>"$F0_OUT/summary.md"
}

f0_tool() { "${LAB_PY[@]}" "$HERE/f0_tool.py" "$@"; }

f0_redact() { bash "$HERE/redact.sh"; }

tool_names() { # FILE
  json_of "$1" '" ".join(sorted(t["name"] for t in ((d.get("rows") or {}).get("tools/list") or {}).get("tools") or []))' \
    2>/dev/null || true
}

vmcp_diag() { # ITEM NS VMCP [SELECTOR [SELECTOR_NS]]
  local item="$1" ns="$2" vmcp="$3" sel="${4:-}" sel_ns="${5:-$2}" out="$F0_OUT/diag-$3.txt"
  {
    echo "VirtualMCPServer $ns/$vmcp status:"
    "${K[@]}" -n "$ns" get virtualmcpserver "$vmcp" -o jsonpath='{.status}' 2>&1 || true
    echo
    echo
    echo "vMCP pod log, lines that are not audit events (last 150):"
    "${K[@]}" -n "$ns" logs -l "app.kubernetes.io/name=virtualmcpserver,app.kubernetes.io/instance=$vmcp" \
      --tail=3000 2>&1 | grep -v -e '"audit_id"' -e '"auditId"' | tail -n 150 || true
    if [ -n "$sel" ]; then
      echo
      echo "backend pods in $sel_ns ($sel), last 100 lines each:"
      "${K[@]}" -n "$sel_ns" logs -l "$sel" --all-containers --prefix --tail=100 2>&1 || true
    fi
  } | f0_redact >"$out"
  f0_block "$item" "$out"
}

f0_server_log() { # ITEM
  "${K[@]}" -n "$F0_NS" logs deploy/f0-mcp --tail=150 2>&1 | f0_redact >"$F0_OUT/diag-f0-mcp.txt" || true
  f0_block "$1" "$F0_OUT/diag-f0-mcp.txt"
}

f0_now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }

f0_header_connect() { # STEP
  AUTH=()
  platform_connect "$1"
  [ "$(api GET /me)" = 200 ] || die "$1" "GET /me in header mode failed"
}

f0_server_up() { # STEP
  local step="$1" image
  image="$("${K[@]}" -n "$PLATFORM_NS" get deploy/daegam-platform -o jsonpath='{.spec.template.spec.containers[0].image}')" \
    || die "$step" "reading the platform image failed"
  "${K[@]}" create namespace "$F0_NS" --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
  "${K[@]}" -n "$F0_NS" create configmap f0-server --from-file=f0_server.py="$HERE/f0_server.py" \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
  # 플랫폼 이미지는 distroless 라 셸이 없다. venv 의 python 이 PATH 에 있다(platform/Dockerfile)
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: f0-mcp, namespace: $F0_NS, labels: {app: f0-mcp}}
spec:
  replicas: 1
  selector: {matchLabels: {app: f0-mcp}}
  template:
    metadata:
      labels: {app: f0-mcp}
      annotations: {f0/server-sha: "$(git -C "$ROOT" hash-object "$HERE/f0_server.py")"}
    spec:
      securityContext: {runAsNonRoot: true, runAsUser: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: f0
          image: $image
          imagePullPolicy: IfNotPresent
          command: [python, /f0/f0_server.py, serve]
          ports: [{containerPort: 8080}, {containerPort: 8081}]
          readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 2}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
          resources: {requests: {cpu: 20m, memory: 64Mi}, limits: {memory: 256Mi}}
          volumeMounts: [{name: f0, mountPath: /f0, readOnly: true}]
      volumes: [{name: f0, configMap: {name: f0-server}}]
---
apiVersion: v1
kind: Service
metadata: {name: f0-mcp, namespace: $F0_NS}
spec:
  selector: {app: f0-mcp}
  ports: [{name: http, port: 8080, targetPort: 8080}, {name: sse, port: 8081, targetPort: 8081}]
EOF
  "${K[@]}" -n "$F0_NS" rollout status deploy/f0-mcp --timeout=180s >/dev/null \
    || die "$step" "the F0 server in $F0_NS did not become ready"
}

f0_server_scale() { # STEP N
  local step="$1" n="$2" i ready
  "${K[@]}" -n "$F0_NS" scale deploy/f0-mcp --replicas="$n" >/dev/null || die "$step" "scaling the F0 server to $n failed"
  for ((i = 0; i < 60; i++)); do
    ready="$("${K[@]}" -n "$F0_NS" get endpoints f0-mcp -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
    if [ "$n" = 0 ] && [ -z "$ready" ]; then return 0; fi
    if [ "$n" != 0 ] && [ -n "$ready" ]; then return 0; fi
    sleep 2
  done
  die "$step" "the F0 server's endpoints did not follow the scale to $n"
}

f0_rest_backend() { # STEP NAME
  local step="$1" code
  "${LAB_PY[@]}" "$HERE/f0_server.py" spec "$F0_HTTP" >"$WORK/f0-rest.json" || die "$step" "writing the REST spec failed"
  upload_backend_api "$step" "$2" "$WORK/f0-rest.json"
  code="$(api PATCH "/backend-apis/$BACKEND_API_ID" "{\"endpoint\": \"$F0_HTTP\"}")"
  [ "$code" = 200 ] || die "$step" "PATCH /backend-apis/$BACKEND_API_ID endpoint -> $code"
}

f0_exposed() { # STEP PATH
  local i st=""
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    if [ "$(api GET "$2")" = 200 ]; then
      st="$(jq_ 'json.dumps((d.get("status") or {}).get("exposure"))')"
      [ "$(jq_ "(lambda e: 'ok' if e.get('address_assigned') and e.get('front_phase') == 'Ready' else 'no')((d.get('status') or {}).get('exposure') or {})")" = ok ] \
        && return 0
    fi
    sleep 5
  done
  die "$1" "the exposure of $2 is not ready after ${READY_TIMEOUT}s: $st"
}

f0_loop_start() { # DIR NAME URL TOOL ARGS
  local dir="$1" name="$2"
  rm -f "$dir/stop-$name"
  # 8·9 는 lib/e2e_exit.sh 가 둔 게이트의 출력이다. 이 스크립트가 끝난 뒤에도 도는 루프(F1 watch)가 그것을 붙들면
  # 워크플로의 단계가 끝나지 않는다
  nohup "${LAB_PY[@]}" "$HERE/f0_tool.py" loop --url "$3" --ip "$LB_IP" --tool "$4" --args "$5" \
    --out "$F0_OUT/loop-$name.jsonl" --stop "$dir/stop-$name" </dev/null >>"$F0_OUT/loop-$name.log" 2>&1 8>&- 9>&- &
  echo "$!" >"$dir/pid-$name"
  f0_now >"$dir/started-$name"
}

f0_loop_stop() { # DIR NAME SWITCH_AT [DELETED_AT]
  local dir="$1" name="$2" pid i
  touch "$dir/stop-$name"
  pid="$(cat "$dir/pid-$name" 2>/dev/null || true)"
  for ((i = 0; i < 30; i++)); do
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 1
  done
  f0_tool lag --loop "$F0_OUT/loop-$name.jsonl" --switch-at "$3" ${4:+--deleted-at "$4"} >"$F0_OUT/lag-$name.txt" \
    || echo "!! lag summary of loop $name failed (see $F0_OUT/loop-$name.log)"
}

hub_item() { # STEP NAME KIND URL TRANSPORT [ACCESS_JSON]
  local access="" code i
  [ -z "${6:-}" ] || access=", \"access\": $6"
  ITEM_ID=""; ITEM_PHASE=""
  ITEM_CODE="$(api POST /hub "{\"name\": \"$2\", \"kind\": \"$3\", \"url\": \"$4\", \"transport\": \"$5\"$access}")"
  case "$ITEM_CODE" in
    201) ITEM_ID="$(jq_ 'd["id"]')" ;;
    409) listed_id "$1" /hub "$2"; ITEM_ID="$LISTED_ID" ;;
    *) return 0 ;;
  esac
  if [ "$3" = remote ]; then
    for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
      [ "$(api GET "/hub/$ITEM_ID")" = 200 ] && ITEM_PHASE="$(jq_ '(d.get("status") or {}).get("phase")' || true)"
      [ "$ITEM_PHASE" = Ready ] && break
      sleep 5
    done
  fi
  code="$(api POST "/hub/$ITEM_ID/refresh-tools")"
  [ "$code" = 200 ] || echo "!! refresh-tools of $2 -> $code (F0.4 records it again)"
}

share_with() { # TENANT
  echo "{\"view\": {\"tenants\": [\"$1\"]}, \"consume\": {\"tenants\": [\"$1\"]}, \"manage\": {}}"
}

f0_oidc_session() { # STEP
  local step="$1"
  # 모드를 바꾸면 플랫폼 파드가 바뀐다. 옛 파드에 붙은 port-forward 는 포트를 쥔 채 남을 수 있어 닫고 새로 연다
  # (lib/e2e_pf.sh)
  stop_pf "$PF_API" "$API_PORT"; PF_API=""
  switch_auth_mode oidc DAEGAM_DATA_PLANE_ISSUER=idp || die "$step" "switching to oidc mode failed"
  MODE_SWITCHED=1
  start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"; PF_API=$PF_PID
  wait_port "$API_PORT" || die "$step" "port-forward to the platform did not open"
  start_pf "$PLATFORM_NS" svc/mock-oidc "${OIDC_PORT}:8080"; PF_OIDC=$PF_PID
  wait_port "$OIDC_PORT" || die "$step" "port-forward to mock-oidc did not open"
  ADMIN_TOKEN="$(user_token '{"sub": "f0-admin", "groups": ["platform-admins"], "aud": "daegam-api"}')" \
    || die "$step" "mock-oidc admin token failed"
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  wait_auth_reapplied "$ADMIN_TOKEN" || die "$step" "data-plane auth was not re-applied after switching to oidc"
  [ "$(api PATCH "/tenants/$TENANT/settings" "{\"mcp_group\": \"$MCP_GROUP\"}")" = 200 ] \
    || die "$step" "PATCH /tenants/$TENANT/settings mcp_group"
}

f0_restore_mode() {
  if [ "$MODE_SWITCHED" = 1 ]; then
    MODE_SWITCHED=0
    restore_auth_mode "$ADMIN_TOKEN"
  fi
  stop_pf "$PF_OIDC" "$OIDC_PORT"; PF_OIDC=""
  stop_pf "$PF_API" "$API_PORT"; PF_API=""
  AUTH=(); ADMIN_TOKEN=""
}

f1_state() {
  [ -n "${F1_STATE:-}" ] || die F1 "F1_STATE is not set"
  mkdir -p "$F1_STATE"
  chmod 700 "$F1_STATE"
}
f1_get() { cat "$F1_STATE/$1"; }
f1_put() { (umask 077; printf '%s\n' "$2" >"$F1_STATE/$1"); }

f1_next_round() {
  F1_ROUND=$(($(cat "$F1_STATE/round" 2>/dev/null || echo 0) + 1))
  f1_put round "$F1_ROUND"
}

f1_mark_install() { f0_now >"$F1_STATE/install-at-r$(f1_get round)"; }

f1_stop_loops() { # PHASE NAME...
  local phase="$1" round name loop
  shift
  round="$(f1_get round)"
  for name in "$@"; do
    loop="$name-r$round"
    f0_loop_stop "$F1_STATE" "$loop" "$(cat "$F1_STATE/install-at-r$round")" \
      || f1_fail "$phase" "$loop: no call succeeded before the install, so its failures say nothing about the install"
    f0_block "F1 $phase $loop (switch = the install's start)" "$F0_OUT/lag-$loop.txt"
  done
}

f1_fail() { # PHASE MSG
  [ "$1" != prepare ] || die F1 "$2"
  record_fail "F1-$1" "$2"
}
