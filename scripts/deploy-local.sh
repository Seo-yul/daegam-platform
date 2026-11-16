#!/usr/bin/env bash
# 로컬 클러스터에 대감 차트를 올리고 내린다 (P85 4절). OrbStack 과 태그의 클러스터 e2e 잡이 만든 k3d 가 쓴다.
#
#   make deploy-local      deploy    deploy/dev/values-local.yaml 위에 이 checkout 의 이미지 태그와 make 변수를 얹어
#                                    helm upgrade --install 한다
#   make deploy-ui-local   ui        UI 이미지 태그만 바꿔 helm upgrade --reset-then-reuse-values 한다. 차트와 값이 지난
#                                    배포와 같을 때만 UI 하나만 굴러간다
#   make undeploy-local    undeploy  릴리스와 플랫폼 네임스페이스를 지운다. MongoDB 의 PVC 와 차트가 만든 Secret 도 함께
#                                    가므로 플랫폼의 데이터를 남기지 않는다. 플랫폼이 만든 테넌트 네임스페이스, LLM
#                                    게이트웨이 네임스페이스와 그 Role·RoleBinding, 로컬 도커의 dev-<id> 태그는 남는다
#
# 이미지 태그는 PLATFORM_TAG·UI_TAG 가 있으면 그것이고, 없으면 로컬 이미지 <이름>:<판>에 그 이미지 ID 로 붙인
# dev-<id> 다. 다시 구우면 태그가 바뀌어 파드가 굴러간다. 로컬에 플랫폼 이미지가 없으면 판 태그로 가고, UI 이미지가 없으면
# 멈춘다. 업그레이드가 Deployment 의 스펙을 바꾸지 않았으면(metadata.generation 이 그대로) 그것을 다시 굴려 파드가
# 업그레이드 뒤의 ConfigMap·Secret 을 읽게 한다(D52, P85 3절). 업그레이드가 되돌리는 것은 차트가 렌더하는 키뿐이다.
# e2e 가 산 ConfigMap 에서 고친 키는 렌더한 값으로 돌아가지만, 더한 키는 Helm 의 병합이 지우지 않아 남는다. 차트가 만든
# Secret 의 키에 고쳐 넣은 값은 차트가 산 Secret 을 읽어(lookup) 다시 렌더하므로 그대로 남는다.
# LLM 게이트웨이(P38 3절)는 차트의 llmGateway 값이다(P85 8절). LLM_GATEWAY_IMAGES_FROM=mirror 면 설치 값의 레지스트리 사본을
# 쓰는 값 파일을 하나 더 얹는다.
# Env: KUBE_CONTEXT(orbstack) PLATFORM_NS(daegam-system) PLATFORM_VERSION·UI_VERSION(0.1.0) PLATFORM_TAG UI_TAG
#      MONGO(local, external 이면 함께 올린 MongoDB 없이 운영자가 만든 Secret 둘을 쓴다) LOCAL_HOST INGRESS_CLASS
#      LLM_GATEWAY(off) LLM_GATEWAY_IMAGES_FROM(비우거나 mirror) LLM_GATEWAY_NS(daegam-llm) LLM_GATEWAY_PDB(false)
#      PULL_SECRET(registry-pull) HELM_FLAGS(deploy 의 helm upgrade 에 더한다. 예: --take-ownership)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/lib/die.sh"
. "$HERE/lib/hosts.sh"
# generations, restart_unchanged(스펙이 그대로인 Deployment 를 다시 굴린다)는 여기에 있다
. "$HERE/lib/rollout.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
NS="${PLATFORM_NS:-daegam-system}"
RELEASE=daegam
CHART="$ROOT/charts/daegam"
K=(kubectl --context "$CTX")
H=(helm --kube-context "$CTX")
# 차트가 렌더하는 Deployment. 문서 파싱 서비스는 플랫폼 이미지를 명령만 바꿔 띄운다(D61)
WORKLOADS=(daegam-docparse daegam-platform daegam-ui)
# 플랫폼이 운영자가 만든 것을 쓰는 MONGO=external 의 Secret 둘
EXTERNAL_SECRETS=(daegam-platform-secrets daegam-docparse-token)
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-deploy-local.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# image_tag IMAGE VERSION FORCED MISSING  배포할 태그를 찍는다. FORCED 가 있으면 그것이다. 없으면 로컬 IMAGE:VERSION 에
# dev-<이미지 ID 앞 12자> 태그를 붙이고 그 태그를 찍는다. 로컬에 없으면 MISSING 이 fallback 일 때 VERSION 이고 fail 이면
# 멈춘다
image_tag() {
  local image="$1" version="$2" forced="$3" missing="$4" id
  if [ -n "$forced" ]; then printf '%s\n' "$forced"; return 0; fi
  id="$(docker image inspect --format '{{.Id}}' "$image:$version" 2>/dev/null | sed 's/^sha256://' | cut -c1-12)" || id=""
  if [ -n "$id" ]; then
    docker tag "$image:$version" "$image:dev-$id"
    printf 'dev-%s\n' "$id"
  elif [ "$missing" = fallback ]; then
    echo ">> $image:$version not built locally; using tag $version" >&2
    printf '%s\n' "$version"
  else
    die "$image:$version 이 로컬 도커에 없다. 이미지를 먼저 굽는다"
  fi
}

# gateway_mirror FILE  LLM_GATEWAY_IMAGES_FROM=mirror 면 LLM 게이트웨이 이미지 넷을 설치 값 파일의 REGISTRY 아래 사본과 pull
# secret PULL_SECRET 으로 바꾸는 값 파일을 FILE 에 쓰고 0 이다. 사본 참조는 차트 값의 원본에서 lib/llm_gateway_images.py 가
# 만든다. 망이 원본 레지스트리에서 시간 초과될 때 쓴다. 게이트웨이 네임스페이스는 이 배포가 만들므로 새 클러스터에서는 배포 뒤에
# make registry-pull-secret NS=daegam-llm 을 하고, 그동안 이미지를 받지 못한 게이트웨이 파드는 pull back-off 에서 스스로
# 돌아온다. 비우면 1 이다(원본)
gateway_mirror() {
  local registry
  case "${LLM_GATEWAY_IMAGES_FROM:-}" in
    "") return 1 ;;
    mirror) ;;
    *) die "LLM_GATEWAY_IMAGES_FROM 은 비우거나 mirror 다" ;;
  esac
  registry="$(install_env_value REGISTRY)" || exit 1
  [ -n "$registry" ] || die "LLM_GATEWAY_IMAGES_FROM=mirror 인데 설치 값 파일의 REGISTRY 가 비었다"
  command -v uv >/dev/null || die "uv 가 없다. 차트 값의 llmGateway.images 를 플랫폼의 가상 환경에서 읽는다 (brew install uv)"
  uv run --quiet --project "$ROOT/platform" python "$HERE/lib/llm_gateway_images.py" values "$CHART/values.yaml" \
    "$registry" "${PULL_SECRET:-registry-pull}" >"$1" || die "차트 값의 llmGateway.images 로 사본 값 파일을 만들지 못했다"
}

# overlay PLATFORM_TAG UI_TAG  values-local.yaml 위에 얹을 값(JSON). 이미지 태그, make 변수에서 온 값, LLM 게이트웨이의
# 모드·네임스페이스·PDB 다. 값은 환경으로 넘겨 따옴표를 풀지 않는다
overlay() {
  PLATFORM_IMAGE_TAG="$1" UI_IMAGE_TAG="$2" python3 - <<'PY'
import json, os

e = os.environ
values = {
    "images": {"platform": {"tag": e["PLATFORM_IMAGE_TAG"]}, "ui": {"tag": e["UI_IMAGE_TAG"]}},
    "llmGateway": {
        "mode": e.get("LLM_GATEWAY") or "off",
        "namespace": e.get("LLM_GATEWAY_NS") or "daegam-llm",
        "pdb": {"enabled": (e.get("LLM_GATEWAY_PDB") or "false") == "true"},
    },
}
if e.get("LOCAL_HOST"):
    values["host"] = e["LOCAL_HOST"]
if e.get("INGRESS_CLASS"):
    values["ingress"] = {"className": e["INGRESS_CLASS"]}
if e.get("MONGO") == "external":
    values["mongodb"] = {"bundled": False}
    values["secrets"] = {
        "platform": {"existingSecret": "daegam-platform-secrets"},
        "docparse": {"existingSecret": "daegam-docparse-token"},
    }
print(json.dumps(values, indent=1))
PY
}

# helm_failed  helm upgrade 가 실패했을 때 할 일을 찍고 멈춘다. 앞 배포가 끊겨 릴리스가 pending-install·pending-upgrade
# 로 남았으면 다음 helm 은 another operation is in progress 로 멈춘다
helm_failed() {
  die "helm upgrade 가 실패했다(위 줄). 릴리스가 pending 으로 남았으면 helm --kube-context $CTX history $RELEASE -n $NS 로 마지막 deployed 판을 찾아 helm --kube-context $CTX rollback $RELEASE <판> -n $NS 한 뒤 다시 하고, 끊긴 것이 첫 설치면 helm --kube-context $CTX uninstall $RELEASE -n $NS 다. 차트 이전에 kubectl apply 로 올린 객체가 남은 클러스터면 make undeploy-local 로 지우거나(데이터도 지운다) 한 번만 HELM_FLAGS=--take-ownership 으로 넘겨받는다"
}

deploy() {
  local platform_tag ui_tag before s values=() flags=()
  platform_tag="$(image_tag daegam-platform "${PLATFORM_VERSION:-0.1.0}" "${PLATFORM_TAG:-}" fallback)"
  ui_tag="$(image_tag daegam-ui "${UI_VERSION:-0.1.0}" "${UI_TAG:-}" fail)"
  case "${LLM_GATEWAY:-off}" in off | agentgateway) ;; *) die "LLM_GATEWAY 는 off 나 agentgateway 다" ;; esac
  case "${LLM_GATEWAY_PDB:-false}" in true | false) ;; *) die "LLM_GATEWAY_PDB 는 true 나 false 다" ;; esac
  case "${MONGO:-local}" in
    local) ;;
    external)
      for s in "${EXTERNAL_SECRETS[@]}"; do
        "${K[@]}" -n "$NS" get secret "$s" >/dev/null 2>&1 \
          || die "MONGO=external: $NS 에 Secret $s 를 먼저 만든다 (daegam-platform-secrets 는 DAEGAM_MONGO_URL, daegam-docparse-token 은 token)"
      done
      ;;
    *) die "MONGO 는 local 이나 external 이다" ;;
  esac
  overlay "$platform_tag" "$ui_tag" >"$WORK/overlay.json"
  values=(-f "$ROOT/deploy/dev/values-local.yaml" -f "$WORK/overlay.json")
  if gateway_mirror "$WORK/gateway-mirror.json"; then values+=(-f "$WORK/gateway-mirror.json"); fi
  [ -z "${HELM_FLAGS:-}" ] || read -r -a flags <<<"$HELM_FLAGS"
  before="$(generations "${WORKLOADS[@]}")"
  echo ">> helm upgrade --install $RELEASE (context $CTX, platform $platform_tag, ui $ui_tag, mongo ${MONGO:-local}, llm gateway ${LLM_GATEWAY:-off})"
  "${H[@]}" upgrade --install "$RELEASE" "$CHART" -n "$NS" --create-namespace "${values[@]}" \
    ${flags[@]+"${flags[@]}"} || helm_failed
  restart_unchanged "$before" "${WORKLOADS[@]}"
  [ "${MONGO:-local}" != local ] || "${K[@]}" -n "$NS" rollout status statefulset/mongodb --timeout=300s
  for s in "${WORKLOADS[@]}"; do "${K[@]}" -n "$NS" rollout status "deploy/$s" --timeout=240s; done
  "${K[@]}" -n "$NS" get pod,svc,pvc,pdb,ingress
}

deploy_ui() {
  local ui_tag before
  ui_tag="$(image_tag daegam-ui "${UI_VERSION:-0.1.0}" "${UI_TAG:-}" fail)"
  "${H[@]}" status "$RELEASE" -n "$NS" >/dev/null 2>&1 || die "$NS 에 릴리스 $RELEASE 가 없다. make deploy-local 을 먼저 한다"
  before="$(generations daegam-ui)"
  echo ">> helm upgrade $RELEASE --reset-then-reuse-values (context $CTX, ui $ui_tag)"
  "${H[@]}" upgrade "$RELEASE" "$CHART" -n "$NS" --reset-then-reuse-values --set "images.ui.tag=$ui_tag" || helm_failed
  restart_unchanged "$before" daegam-ui
  "${K[@]}" -n "$NS" rollout status deploy/daegam-ui --timeout=240s
}

# 지우기는 하나가 실패해도 나머지를 한다. 차트 이전(kubectl apply)의 배포가 남긴 클러스터 범위 객체도 지워 다음
# make deploy-local 이 넘겨받을 것이 없게 한다. 릴리스가 지웠으면 없다
undeploy() {
  "${H[@]}" uninstall "$RELEASE" -n "$NS" --ignore-not-found --wait --timeout 3m || echo "!! helm uninstall $RELEASE failed"
  "${K[@]}" delete namespace "$NS" --ignore-not-found --timeout=180s || echo "!! namespace $NS was not deleted"
  "${K[@]}" delete clusterrolebinding/daegam-platform clusterrole/daegam-platform clusterrole/daegam-tls-reader \
    clusterrole/daegam-tenant-objects priorityclass/daegam-sandbox namespace/daegam-probe --ignore-not-found \
    || echo "!! the cluster-scoped objects of an earlier deploy were not all deleted"
}

case "${1:-}" in
  deploy) deploy ;;
  ui) deploy_ui ;;
  undeploy) undeploy ;;
  *) die "usage: deploy-local.sh deploy|ui|undeploy" ;;
esac
