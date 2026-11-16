#!/usr/bin/env bash
# P38 F0 실험실. orbstack 에 메아리 네임스페이스(daegam-e2e-llm)와 게이트웨이 네임스페이스(daegam-llm)를 띄우고 지운다.
#
#   lab.sh up                      두 네임스페이스, 메아리 둘, 측정 파드, 게이트웨이(설정 SPEC 은 아래 apply 와 같다)
#   lab.sh apply SPEC.json KEYDIR  설정을 렌더해 Secret llm-gateway-config 에 쓴다(KEYDIR 의 파일 <slug> 가 키다).
#                                  ROLL=1 이면 템플릿 주석 daegam.devops.ai.kr/config-rev 를 바꿔 파드를 굴린다
#   lab.sh otel TOKEN_FILE         수집기 llm-otel 을 띄운다(토큰은 Secret llm-otel-token)
#   lab.sh ratelimit               한도 서비스 llm-ratelimit 과 llm-redis 를 띄운다
#   lab.sh scripts DIR             DIR 의 측정 스크립트를 ConfigMap llm-spike 로 올린다(측정 파드의 /spike)
#   lab.sh gateway [REV]           게이트웨이 Deployment·Service 를 적용한다. PRESTOP(초, 5)이 preStop sleep 이다
#   lab.sh down                    라벨 daegam.devops.ai.kr/spike=p38 인 네임스페이스만 지운다
# Env: KUBE_CONTEXT(orbstack) IMAGE(런타임 이미지, daegam-agent-runtime:0.1.0) GATEWAY_IMAGE(agentgateway v1.5.0 digest)
#
# 네임스페이스는 없을 때만 만들고 라벨 daegam.devops.ai.kr/spike=p38 를 단다. 같은 이름이 라벨 없이 있으면 멈춘다. P37 e2e 의
# 라벨(daegam.devops.ai.kr/e2e=llm-echo)은 달지 않는다. 달면 그 e2e 의 정리가 이 실험실을 지운다. 키는 가짜 값만 쓴다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
CTX="${KUBE_CONTEXT:-orbstack}"
IMAGE="${IMAGE:-daegam-agent-runtime:0.1.0}"
GATEWAY_IMAGE="${GATEWAY_IMAGE:-ghcr.io/agentgateway/agentgateway:v1.5.0@sha256:bf2f339ef326d32def2aaeb44b1b4549801293c19b89e764a4228667d97d9896}"
ECHO_NS="daegam-e2e-llm"; GW_NS="daegam-llm"; MARK="daegam.devops.ai.kr/spike=p38"
K=(kubectl --context "$CTX")

ensure_ns() { # NS LABEL...
  local ns="$1"; shift
  if "${K[@]}" get namespace "$ns" >/dev/null 2>&1; then
    [ "$("${K[@]}" get namespace "$ns" -o 'jsonpath={.metadata.labels.daegam\.devops\.ai\.kr/spike}')" = p38 ] \
      || { echo "namespace $ns exists without $MARK" >&2; exit 1; }
  else
    "${K[@]}" create namespace "$ns" >/dev/null
  fi
  "${K[@]}" label --overwrite namespace "$ns" "$MARK" "$@" >/dev/null
}

apply_config() { # SPEC KEYDIR
  local spec="$1" keydir="$2" rev args=()
  rev="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["rev"])' "$spec")"
  python3 "$HERE/render_config.py" "$spec" >"$keydir/../config.yaml"
  args=(--from-file=config.yaml="$keydir/../config.yaml")
  for f in "$keydir"/*; do args+=(--from-file="key.$(basename "$f")=$f"); done
  "${K[@]}" -n "$GW_NS" create secret generic llm-gateway-config "${args[@]}" --dry-run=client -o yaml \
    | "${K[@]}" apply --server-side --force-conflicts --field-manager=p38-lab -f - >/dev/null
  if [ "${ROLL:-0}" = 1 ]; then
    "${K[@]}" -n "$GW_NS" patch deploy llm-gateway --type merge \
      -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"daegam.devops.ai.kr/config-rev\":\"$rev\"}}}}}" >/dev/null
  fi
  echo "applied rev $rev"
}

case "${1:-}" in
  up)
    ensure_ns "$ECHO_NS" daegam.devops.ai.kr/e2e=p38-spike
    ensure_ns "$GW_NS" daegam.devops.ai.kr/component=llm-gateway pod-security.kubernetes.io/enforce=restricted
    "${K[@]}" -n "$ECHO_NS" create configmap llm-echo --from-file=llm_echo.py="$ROOT/deploy/dev/lab/llm_echo.py" \
      --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
    "${K[@]}" -n "$ECHO_NS" get configmap llm-spike >/dev/null 2>&1 \
      || "${K[@]}" -n "$ECHO_NS" create configmap llm-spike >/dev/null
    sed "s|IMAGE|$IMAGE|" "$HERE/echo.yaml" | "${K[@]}" -n "$ECHO_NS" apply -f - >/dev/null
    "${K[@]}" -n "$ECHO_NS" rollout status deploy/llm-echo --timeout=180s >/dev/null
    "${K[@]}" -n "$ECHO_NS" rollout status deploy/llm-echo-b --timeout=180s >/dev/null
    echo "echo up in $ECHO_NS"
    ;;
  gateway)
    sed -e "s|IMAGE|$GATEWAY_IMAGE|" -e "s|REV|${2:-r1}|" -e "s|PRESTOP|${PRESTOP:-5}|" "$HERE/gateway.yaml" | "${K[@]}" -n "$GW_NS" apply -f - >/dev/null
    "${K[@]}" -n "$GW_NS" rollout status deploy/llm-gateway --timeout=180s
    ;;
  apply) apply_config "$2" "$3" ;;
  otel) # TOKEN_FILE: P37 의 수집 토큰(svc_token.py --audience daegam-telemetry --sub svc:llm-gateway:<클러스터 id>)
    "${K[@]}" -n "$GW_NS" create secret generic llm-otel-token --from-file=token="$2" --dry-run=client -o yaml \
      | "${K[@]}" apply --server-side --force-conflicts --field-manager=p38-lab -f - >/dev/null
    "${K[@]}" -n "$GW_NS" apply -f "$HERE/otel.yaml" >/dev/null
    "${K[@]}" -n "$GW_NS" rollout status deploy/llm-otel --timeout=180s
    ;;
  ratelimit)
    "${K[@]}" -n "$GW_NS" get secret llm-redis >/dev/null 2>&1 \
      || "${K[@]}" -n "$GW_NS" create secret generic llm-redis --from-literal=password="$(openssl rand -hex 24)" >/dev/null
    "${K[@]}" -n "$GW_NS" apply -f "$HERE/ratelimit.yaml" >/dev/null
    "${K[@]}" -n "$GW_NS" rollout status deploy/llm-redis --timeout=180s
    "${K[@]}" -n "$GW_NS" rollout status deploy/llm-ratelimit --timeout=180s
    ;;
  scripts)
    "${K[@]}" -n "$ECHO_NS" create configmap llm-spike --from-file="$2" --dry-run=client -o yaml \
      | "${K[@]}" apply -f - >/dev/null
    ;;
  down) "${K[@]}" delete namespace -l "$MARK" --wait=false ;;
  *) echo "usage: lab.sh up|gateway [REV]|apply SPEC KEYDIR|scripts DIR|down" >&2; exit 2 ;;
esac
