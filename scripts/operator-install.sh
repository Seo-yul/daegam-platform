#!/usr/bin/env bash
# ToolHive CRD 차트와 오퍼레이터를 설치하거나 올리고, 데이터 플레인이 따라올 때까지 기다린다 (make operator-install).
# 멱등이다.
#
#   make operator-install [KUBE_CONTEXT=<컨텍스트>] [TOOLHIVE_VERSION=<판>] [TOOLHIVE_CRDS_NS=<네임스페이스>]
#
# 오퍼레이터 차트의 값은 deploy/toolhive/operator-values.yaml 하나다. 손으로 --set 한 값은 다음 설치에서 사라진다.
# 그 값이 업데이트 확인과 사용량 지표를 끈다(P50 G1). 오퍼레이터가 굴러간 뒤 셋을 차례로 기다린다. 단계마다
# OPERATOR_WAIT_TIMEOUT 초 안에 끝나지 않으면 실패다.
#   1. 어느 run configuration(ConfigMap, 라벨 toolhive.stacklok.io/component=run-config)에도 사용량 지표
#      미들웨어(usagemetrics)가 남지 않는다
#   2. 그 미들웨어가 있던 프록시의 Deployment 가 새 runconfig-checksum 으로 바뀐다
#   3. toolhive=true Deployment 가 모두 TOOLHIVE_VERSION 이미지로 바뀌고 롤아웃을 끝낸다
# env 만 바뀐 설치에서는 이미지 태그가 그대로라 3 만으로는 아무것도 기다리지 않고 끝난다(P50 F0 실측).
# Env: KUBE_CONTEXT(기본 orbstack), TOOLHIVE_VERSION(필수), TOOLHIVE_CRDS_NS(기본 default. CRD 차트를 처음 설치한
#      네임스페이스여야 한다), DAEGAM_CA_FILE(사내 방화벽 CA), OPERATOR_WAIT_TIMEOUT(초, 기본 180)
# 차트는 먼저 시스템 루트로 받고, 인증서 오류일 때만 DAEGAM_CA_FILE 을 helm --ca-file 로 넘겨 다시 받는다. --ca-file 은
# 시스템 루트를 갈음하므로, 방화벽이 ghcr.io 를 검사하지 않는 때(진짜 체인)에는 그 CA 하나로 받으면 실패한다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CTX="${KUBE_CONTEXT:-orbstack}"
VERSION="${TOOLHIVE_VERSION:?TOOLHIVE_VERSION 이 비었다 (Makefile 이 넘긴다)}"
CRDS_NS="${TOOLHIVE_CRDS_NS:-default}"
VALUES="$ROOT/deploy/toolhive/operator-values.yaml"
TIMEOUT="${OPERATOR_WAIT_TIMEOUT:-180}"
CHART=oci://ghcr.io/stacklok/toolhive
K=(kubectl --context "$CTX")
H=(helm --kube-context "$CTX")
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-operator-install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# helm upgrade -i ARGS... 를 시스템 루트로 한 번 하고, 인증서 오류로 실패했고 DAEGAM_CA_FILE 이 있으면 --ca-file 로 다시
# 한다. 다른 실패는 그대로 실패다
helm_upgrade() {
  if "${H[@]}" upgrade -i "$@" 2>"$WORK/helm.err"; then
    cat "$WORK/helm.err" >&2
    return 0
  fi
  cat "$WORK/helm.err" >&2
  [ -n "${DAEGAM_CA_FILE:-}" ] && grep -qiE 'x509|certificate' "$WORK/helm.err" || return 1
  echo ">> certificate error with the system roots; retrying with --ca-file $DAEGAM_CA_FILE" >&2
  "${H[@]}" upgrade -i "$@" --ca-file "$DAEGAM_CA_FILE"
}

# die MSG 는 여기에 있다
. "$HERE/lib/die.sh"

# 단계마다 새로 잡는 기한. 앞 단계가 시간을 많이 써도 다음 단계가 제 몫을 다 받는다
deadline_from_now() { echo $(($(date +%s) + TIMEOUT)); }

# 사용량 지표 미들웨어가 있는 run configuration 의 프록시 "<네임스페이스>/<Deployment 이름>". Deployment 이름은 run
# configuration 라벨의 CR 이름이다(MCPServer·MCPRemoteProxy 모두 Deployment 이름이 CR 이름과 같다)
metric_proxies() {
  "${K[@]}" get configmap -A -l toolhive.stacklok.io/component=run-config -o json </dev/null | python3 -c '
import json, sys
for cm in json.load(sys.stdin)["items"]:
    meta = cm["metadata"]
    try:
        config = json.loads((cm.get("data") or {}).get("runconfig.json") or "{}")
    except ValueError:
        continue
    if not any(m.get("type") == "usagemetrics" for m in config.get("middleware_configs") or []):
        continue
    labels = meta.get("labels") or {}
    name = labels.get("toolhive.stacklok.io/mcp-server") or labels.get("toolhive.stacklok.io/mcp-remote-proxy")
    if not name:
        name = meta["name"][: -len("-runconfig")] if meta["name"].endswith("-runconfig") else meta["name"]
    print(meta["namespace"] + "/" + name)
'
}

# toolhive=true Deployment 마다 "<네임스페이스>/<이름> <runconfig-checksum>"
checksums() {
  "${K[@]}" get deploy -A -l toolhive=true -o json </dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin)["items"]:
    meta = d["metadata"]
    notes = ((d.get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations") or {}
    print(meta["namespace"] + "/" + meta["name"], notes.get("toolhive.stacklok.dev/runconfig-checksum", "-"))
'
}

# 설치 전 상태: 사용량 지표가 남은 프록시와 그 checksum
metric_proxies >"$WORK/metric-before"
checksums >"$WORK/sums-before"

helm_upgrade toolhive-operator-crds "$CHART/toolhive-operator-crds" --version "$VERSION" -n "$CRDS_NS"
helm_upgrade toolhive-operator "$CHART/toolhive-operator" --version "$VERSION" -n toolhive-system \
  --create-namespace -f "$VALUES"
"${K[@]}" -n toolhive-system rollout status deploy/toolhive-operator --timeout="${TIMEOUT}s"

# 1. 새 오퍼레이터가 run configuration 을 다시 쓴다(실측 10초 안팎)
deadline="$(deadline_from_now)"
while :; do
  left="$(metric_proxies)"
  [ -z "$left" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "run configurations still carry usage metrics after ${TIMEOUT}s: $(echo "$left" | tr '\n' ' ')"
  echo ">> $(echo "$left" | wc -l | tr -d ' ') run configuration(s) still carry usage metrics"
  sleep 5
done

# 2. 미들웨어가 빠진 프록시는 새 checksum 으로 굴러간다(실측 30초 안팎). 설치 전에 없던 것은 기다리지 않는다
deadline="$(deadline_from_now)"
while read -r proxy; do
  [ -n "$proxy" ] || continue
  old="$(awk -v p="$proxy" '$1 == p {print $2}' "$WORK/sums-before")"
  while :; do
    now="$(checksums | awk -v p="$proxy" '$1 == p {print $2}')"
    # 사라진 프록시(설치 사이에 지워진 것)도 기다릴 것이 없다
    if [ -z "$now" ] || [ "$now" != "$old" ]; then break; fi
    [ "$(date +%s)" -lt "$deadline" ] || die "$proxy did not roll after its run configuration dropped usage metrics"
    echo ">> waiting for $proxy to roll"
    sleep 5
  done
done <"$WORK/metric-before"

# 3. 판을 올렸으면 모든 프록시·vMCP Deployment 가 새 이미지로 굴러간다. 다 바뀌고 롤아웃이 끝날 때까지 기다려 뒤이은
#    e2e 가 굴러가는 파드를 치지 않게 한다
deadline="$(deadline_from_now)"
while :; do
  old="$("${K[@]}" get deploy -A -l toolhive=true \
    -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
    | grep -v '^$' | grep -vc ":v${VERSION}\$" || true)"
  [ "$old" = 0 ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "$old ToolHive workload(s) still not on v$VERSION after ${TIMEOUT}s"
  echo ">> $old ToolHive workload(s) not yet on v$VERSION"
  sleep 5
done
"${K[@]}" get deploy -A -l toolhive=true -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}' \
  | while read -r ns n; do "${K[@]}" -n "$ns" rollout status "deploy/$n" --timeout="${TIMEOUT}s" || exit 1; done
echo ">> ToolHive $VERSION is installed on $CTX; no run configuration carries usage metrics"
