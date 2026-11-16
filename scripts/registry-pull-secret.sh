#!/usr/bin/env bash
# 레지스트리 pull secret 을 네임스페이스마다 만든다 (D53). 값은 docker login 이 둔 자격 증명에서 읽는다.
#
#   make registry-pull-secret NS="daegam-system daegam-default"
#   make registry-pull-secret NS=daegam-system PULL_SECRET_SAS="default daegam-platform"
# Env: KUBE_CONTEXT(orbstack) REGISTRY(설치 값 파일의 REGISTRY) PULL_SECRET(registry-pull)
#      PULL_SECRET_SAS  이 secret 을 imagePullSecrets 로 붙일 ServiceAccount 들. 비우면 붙이지 않는다
#
# 비밀번호를 명령줄 인자로 넘기지 않는다(ps 에 보인다). Secret 매니페스트를 만들어 표준 입력으로 kubectl apply 에 준다.
# 플랫폼이 렌더하는 런타임·에이전트 파드는 Cluster 의 images.pull_secret 이름을 imagePullSecrets 로 쓴다. 플랫폼은 테넌트
# 네임스페이스에 secret 을 복사하지 않으므로 그 파드가 뜨는 네임스페이스마다 이것을 돌린다. daegam-system 의 플랫폼·UI 는
# 자기 ServiceAccount(daegam-platform, default)에 붙여야 당긴다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="${KUBE_CONTEXT:-orbstack}"
# 레지스트리(호스트/프로젝트)는 환경의 REGISTRY 이고, 없으면 설치 값 파일(scripts/lib/hosts.sh 의 install_env_file)의 값이다
. "$HERE/lib/hosts.sh"
REGISTRY="${REGISTRY:-$(install_env_value REGISTRY)}"
[ -n "$REGISTRY" ] || { echo "REGISTRY 가 비었다 (환경이나 설치 값 파일의 REGISTRY)" >&2; exit 1; }
HOST="${REGISTRY%%/*}"
NAME="${PULL_SECRET:-registry-pull}"
SAS="${PULL_SECRET_SAS:-}"
K=(kubectl --context "$CTX")
[ "$#" -gt 0 ] || { echo "usage: $0 NAMESPACE... (make registry-pull-secret NS=...)" >&2; exit 2; }

for ns in "$@"; do
  # 자격 증명 읽기는 lib/registry_auth.py 한 벌이다 (image-report.sh·resolve-images.sh 와 같다)
  PYTHONPATH="$HERE/lib" python3 - "$HOST" "$NAME" "$ns" <<'PY' | "${K[@]}" apply -f -
import base64, json, sys

from registry_auth import credentials

host, name, ns = sys.argv[1:4]
user, secret = credentials(host)
b64 = lambda raw: base64.b64encode(raw.encode()).decode()  # noqa: E731
dockercfg = {"auths": {host: {"username": user, "password": secret, "auth": b64(f"{user}:{secret}")}}}
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "type": "kubernetes.io/dockerconfigjson",
    "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/managed-by": "daegam-registry-pull-secret"}},
    "data": {".dockerconfigjson": b64(json.dumps(dockercfg))},
}))
PY
  for sa in $SAS; do
    "${K[@]}" -n "$ns" get serviceaccount "$sa" >/dev/null 2>&1 || "${K[@]}" -n "$ns" create serviceaccount "$sa" >/dev/null
    # imagePullSecrets 는 통째로 바뀌는 목록이라 있던 이름을 읽어 우리 것을 더한다
    have="$("${K[@]}" -n "$ns" get serviceaccount "$sa" -o jsonpath='{.imagePullSecrets[*].name}')"
    case " $have " in *" $NAME "*) ;; *)
      list="$(for n in $have $NAME; do printf '{"name": "%s"},' "$n"; done)"
      "${K[@]}" -n "$ns" patch serviceaccount "$sa" -p "{\"imagePullSecrets\": [${list%,}]}" >/dev/null ;;
    esac
    echo "   serviceaccount/$sa in $ns pulls with $NAME"
  done
done
