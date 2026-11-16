#!/usr/bin/env bash
# LLM 게이트웨이의 네임스페이스를 거둔다 (P38 3절, make llm-gateway-uninstall).
#
#   make llm-gateway-uninstall [KUBE_CONTEXT=<컨텍스트> | INSTALL_CONTEXT=<설치본 컨텍스트>] [LLM_GATEWAY_NS=daegam-llm]
#
# 네임스페이스와 플랫폼의 Role·RoleBinding 은 대감 차트가 llmGateway.mode agentgateway 일 때 helm.sh/resource-policy:
# keep 으로 렌더한다(charts/daegam/templates/llm-gateway.yaml, P85 8절). 그래서 끈 뒤의 업그레이드도 helm uninstall 도
# 그것을 지우지 않고 이것이 지운다.
# 네임스페이스 이름은 무엇을 지우기 전에 본다. DNS 라벨이어야 하고 플랫폼 네임스페이스(PLATFORM_NS), daegam-system,
# default, kube-*, toolhive-system, daegam-probe 가 아니어야 한다. 차트의 llmGateway.namespace 와 같은 규칙이다. 틀리면
# 그 네임스페이스를 PVC 와 함께 지운다.
# 스위치가 꺼졌고(플랫폼 ConfigMap 의 DAEGAM_LLM_GATEWAY 가 off) 라벨 daegam.devops.ai.kr/llm-route=gateway 인 파드가 하나도
# 없을 때만 표시(daegam.devops.ai.kr/component=llm-gateway)가 붙은 네임스페이스를 지운다. 그 파드가 남았으면 아직
# 게이트웨이를 부르는 Agent 가 있다. 플랫폼 ConfigMap 이 없으면 멈춘다. 틀린 PLATFORM_NS 나 컨텍스트는 꺼진 것과 다르다.
# 컨텍스트는 INSTALL_CONTEXT 가 있으면 그것(설치 값 파일과 같은 이름)이고, 없으면 KUBE_CONTEXT(기본 orbstack)다. 설치 값
# 파일의 INSTALL_CONTEXT 를 기본으로 읽지 않는 것은 orbstack 에 하려던 거두기가 설치본에 닿지 않게 하려는 것이다.
# Env: INSTALL_CONTEXT, KUBE_CONTEXT, LLM_GATEWAY_NS(기본 daegam-llm), PLATFORM_NS(기본 daegam-system)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/die.sh"
CTX="${INSTALL_CONTEXT:-${KUBE_CONTEXT:-orbstack}}"
LLM_GATEWAY_NS="${LLM_GATEWAY_NS:-daegam-llm}" PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
K=(kubectl --context "$CTX")
MARK_KEY="daegam.devops.ai.kr/component"
MARK_VALUE="llm-gateway"
ROUTE_LABEL="daegam.devops.ai.kr/llm-route=gateway"
PLATFORM_CONFIGMAP=daegam-platform-config

# check_namespace  LLM_GATEWAY_NS 가 게이트웨이가 쓸 수 있는 이름인가. 아니면 멈춘다
check_namespace() {
  local ns="$LLM_GATEWAY_NS"
  [[ "$ns" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || die "LLM_GATEWAY_NS '$ns' 가 DNS 라벨이 아니다"
  case "$ns" in
    "$PLATFORM_NS" | daegam-system | default | kube-* | toolhive-system | daegam-probe)
      die "LLM_GATEWAY_NS '$ns' 는 플랫폼이나 쿠버네티스의 네임스페이스다. 게이트웨이 전용 이름을 준다" ;;
  esac
}

# ns_label KEY  게이트웨이 네임스페이스의 라벨 KEY 값. 네임스페이스가 없거나 라벨이 없으면 빈 줄이다
ns_label() {
  local path
  path="{.metadata.labels.${1//./\\.}}"
  "${K[@]}" get namespace "$LLM_GATEWAY_NS" --ignore-not-found -o "jsonpath=$path"
}

uninstall() {
  local mode pods
  [ "$(ns_label "$MARK_KEY")" = "$MARK_VALUE" ] \
    || die "네임스페이스 $LLM_GATEWAY_NS 가 없거나 $MARK_KEY=$MARK_VALUE 표시가 없다. 지우지 않았다"
  [ -n "$("${K[@]}" -n "$PLATFORM_NS" get configmap "$PLATFORM_CONFIGMAP" --ignore-not-found -o name)" ] \
    || die "$PLATFORM_NS 에 ConfigMap $PLATFORM_CONFIGMAP 이 없다. PLATFORM_NS 와 컨텍스트($CTX)를 확인한다. 지우지 않았다"
  mode="$("${K[@]}" -n "$PLATFORM_NS" get configmap "$PLATFORM_CONFIGMAP" \
    -o 'jsonpath={.data.DAEGAM_LLM_GATEWAY}')" || die "ConfigMap $PLATFORM_CONFIGMAP 을 읽지 못했다"
  [ -z "$mode" ] || [ "$mode" = off ] \
    || die "DAEGAM_LLM_GATEWAY 가 $mode 다. 먼저 게이트웨이를 끈 배포(차트의 llmGateway.mode off, make 의 LLM_GATEWAY=off)를 하고 Agent 가 직접 경로로 옮겨 가기를 기다린다"
  pods="$("${K[@]}" get pods -A -l "$ROUTE_LABEL" -o 'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} {end}')" \
    || die "라벨 $ROUTE_LABEL 인 파드를 읽지 못했다"
  [ -z "$pods" ] || die "아직 게이트웨이를 부르는 파드가 있다: $pods. 옮겨 가기를 기다린 뒤 다시 한다"
  "${K[@]}" delete namespace "$LLM_GATEWAY_NS" --timeout=180s
  echo ">> $LLM_GATEWAY_NS 를 지웠다"
}

check_namespace
case "${1:-}" in
  uninstall) uninstall ;;
  *) die "usage: llm-gateway-install.sh uninstall" ;;
esac
