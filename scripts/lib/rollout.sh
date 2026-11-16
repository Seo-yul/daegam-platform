# shellcheck shell=bash
# helm upgrade 가 굴리지 않은 Deployment 를 다시 굴린다 (D52, P85 3절). 업그레이드의 렌더가 그대로면 Deployment 의 스펙도
# 그대로라(metadata.generation 이 그대로) 파드가 새 ConfigMap·Secret 을 읽지 않는다. 그래서 e2e 가 산 ConfigMap 이나 Secret
# 에 고쳐 넣은 값이 배포 뒤에도 남는다. 대감 차트를 올리는 스크립트(scripts/deploy-local.sh 와 운영자의 배포)가
# `. "$HERE/lib/rollout.sh"` 로 읽는다. 부르는 쪽이 kubectl 명령 배열 K 와 네임스페이스 NS 를 둔다.

# generations NAME...  Deployment 마다 `이름=metadata.generation` 한 줄. 없는 Deployment 는 값이 비어 있다
generations() {
  local d
  for d in "$@"; do
    printf '%s=%s\n' "$d" "$("${K[@]}" -n "$NS" get deploy "$d" --ignore-not-found -o 'jsonpath={.metadata.generation}')"
  done
}

# restart_unchanged BEFORE NAME...  업그레이드 앞의 generations 출력 BEFORE 와 견주어 스펙이 그대로인 Deployment 를 다시
# 굴린다. 이번에 새로 만든 것은 건드리지 않는다
restart_unchanged() {
  local before="$1" d was now
  shift
  for d in "$@"; do
    was="$(sed -n "s/^$d=//p" <<<"$before")"
    now="$("${K[@]}" -n "$NS" get deploy "$d" -o 'jsonpath={.metadata.generation}')"
    if [ -n "$was" ] && [ "$was" = "$now" ]; then
      echo ">> $d: spec unchanged; rollout restart so ConfigMap and Secret changes are picked up"
      "${K[@]}" -n "$NS" rollout restart "deploy/$d"
    fi
  done
}
