# shellcheck shell=bash
# OpenAI 호환 메아리 서버(deploy/dev/lab/llm_echo.py)를 클러스터에 띄우고 provider 로 등록하는 한 벌 (e2e_telemetry, install 의
# e2e_web_tools·e2e_skills). `. "$HERE/lib/e2e_llm_echo.sh"` 로 읽는다. 부르는 쪽이 ROOT·K·WORK 와 lib/e2e_api.sh(api·jq_·
# put_provider_key·remove_providers)·lib/e2e_json.sh(providers_with)·die(STEP MSG)를 둔다.
#
# 메아리는 런타임의 StubChatModel 로 답하므로 스텁 조각(lib/e2e_sse.sh 의 stub_call)으로 Tool 을 고른다. stub provider 를
# 막는 설치본(E2E_TARGET=install)에서도 그 규칙으로 에이전트를 돌릴 수 있다. 네임스페이스 ECHO_NS 는 만들 때 라벨 ECHO_LABEL 을 달고,
# 라벨 없는 같은 이름이 있으면 멈춘다. 이 파일을 쓰는 게이트들과 P38 e2e 가 같은 네임스페이스를 쓰므로 함께 돌리지 않는다.
#
# echo_agent_image
#   GET /clusters 본문($WORK/body)에서 홈 클러스터가 Agent 에 쓰는 이미지(registry_prefix/agent_image)를 ECHO_IMAGE 에
#   둔다. 설치본은 레지스트리의 digest 고정 이미지일 수 있고 설정의 기본값은 없다. 홈 클러스터에 그 값이 없으면 빈 값이다
# echo_up STEP [SERVICE...]
#   네임스페이스를 만들거나 라벨을 확인하고 ECHO_IMAGE(비었으면 Platform Settings 의 default_agent_image)로 메아리
#   Deployment 와 Service llm-echo 를 둔다. 파드 템플릿의 어노테이션 daegam.devops.ai.kr/llm-echo-sha256 이 llm_echo.py 의 해시라
#   파일이 바뀌면 파드가 새로 뜬다. 앞 게이트의 정리 단계가 지운 네임스페이스가 아직 내려가는 중이면 ECHO_NS_WAIT 초까지
#   사라지기를 기다린다. SERVICE 마다 같은 파드를 고르는 Service 를 더 둔다. 롤아웃을 기다리고 ECHO_CREATED=1 을 남긴다
# echo_provider STEP NAME [BASE_URL]
#   kind openai 의 provider NAME(모델 ECHO_CHAT_MODEL, 임베딩 ECHO_EMBED_MODEL, 주소 BASE_URL/v1, 기본은 ECHO_URL)을
#   넣고 가짜 키를 둔다. 이름을 ECHO_PROVIDERS 에 더한다
# echo_down
#   정리 단계에서 부른다. 넣은 provider 를 걷고, 라벨이 붙은 메아리 네임스페이스만 지운다(기다리지 않는다)

ECHO_NS="daegam-e2e-llm"; ECHO_LABEL="daegam.devops.ai.kr/e2e=llm-echo"
ECHO_URL="http://llm-echo.${ECHO_NS}.svc.cluster.local:8080"
ECHO_CHAT_MODEL="echo-chat"; ECHO_EMBED_MODEL="echo-embed"
ECHO_IMAGE=""; ECHO_CREATED=0; ECHO_PROVIDERS=""
ECHO_NS_WAIT=120   # 내려가는 중인 메아리 네임스페이스가 사라지기를 기다리는 한도(초)다

echo_agent_image() {
  ECHO_IMAGE="$(jq_ 'next(("/".join(p for p in (i.get("registry_prefix"), i["agent_image"]) if p)
    for c in d if c["name"] == "home" for i in [c.get("images") or {}] if i.get("agent_image")), "")')"
}

echo_up() { # STEP [SERVICE...]
  local step="$1" svc phase="" i sha
  shift
  # 정리 단계는 네임스페이스를 기다리지 않고 지운다. 게이트를 잇달아 돌리면 그것이 아직 내려가는 중일 수 있고, 그 안에는
  # 아무것도 만들 수 없다
  for ((i = 0; i < ECHO_NS_WAIT / 2; i++)); do
    phase="$("${K[@]}" get namespace "$ECHO_NS" -o 'jsonpath={.status.phase}' 2>/dev/null || true)"
    [ "$phase" = Terminating ] || break
    sleep 2
  done
  case "$phase" in
    "") "${K[@]}" create namespace "$ECHO_NS" >/dev/null
        "${K[@]}" label namespace "$ECHO_NS" "$ECHO_LABEL" >/dev/null ;;
    Terminating) die "$step" "namespace $ECHO_NS is still terminating after ${ECHO_NS_WAIT}s" ;;
    *) [ "$("${K[@]}" get namespace "$ECHO_NS" -o 'jsonpath={.metadata.labels.daegam\.devops\.ai\.kr/e2e}')" = llm-echo ] \
         || die "$step" "namespace $ECHO_NS exists without the label $ECHO_LABEL (P38 e2e may be using it)" ;;
  esac
  ECHO_CREATED=1
  if [ -z "$ECHO_IMAGE" ]; then
    api GET /settings >/dev/null
    ECHO_IMAGE="$(jq_ 'd["default_agent_image"]')"
  fi
  "${K[@]}" -n "$ECHO_NS" create configmap llm-echo --from-file=llm_echo.py="$ROOT/deploy/dev/lab/llm_echo.py" \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
  # 라벨이 붙은 네임스페이스를 다시 쓰면 ConfigMap 만 바뀌고 떠 있는 파드는 옛 llm_echo.py 를 돈다. 파일의 sha256 을
  # 파드 템플릿 어노테이션에 두어, 파일이 바뀌면 파드가 새로 뜨게 한다
  sha="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$ROOT/deploy/dev/lab/llm_echo.py")"
  "${K[@]}" -n "$ECHO_NS" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: llm-echo}
spec:
  replicas: 1
  selector: {matchLabels: {app: llm-echo}}
  template:
    metadata: {labels: {app: llm-echo}, annotations: {daegam.devops.ai.kr/llm-echo-sha256: "$sha"}}
    spec:
      containers:
        - name: echo
          image: $ECHO_IMAGE
          command: ["python", "/echo/llm_echo.py"]
          env: [{name: PYTHONPATH, value: /app}, {name: PORT, value: "8080"}]
          ports: [{containerPort: 8080}]
          readinessProbe: {httpGet: {path: /healthz, port: 8080}}
          volumeMounts: [{name: echo, mountPath: /echo}]
      volumes: [{name: echo, configMap: {name: llm-echo}}]
YAML
  for svc in llm-echo "$@"; do
    "${K[@]}" -n "$ECHO_NS" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata: {name: $svc}
spec: {selector: {app: llm-echo}, ports: [{port: 8080, targetPort: 8080}]}
YAML
  done
  "${K[@]}" -n "$ECHO_NS" rollout status deploy/llm-echo --timeout=180s >/dev/null \
    || die "$step" "llm-echo did not become ready"
}

echo_provider() { # STEP NAME [BASE_URL]
  local step="$1" name="$2" provider code
  api GET /settings >/dev/null
  # 본문을 변수에 먼저 둔다. bash 3.2 는 겹친 명령 치환 안의 {…, …} 를 중괄호 확장으로 쪼갠다
  provider="$(printf '{"name": "%s", "kind": "openai", "model": "%s", "embedding_model": "%s", "base_url": "%s/v1"}' \
    "$name" "$ECHO_CHAT_MODEL" "$ECHO_EMBED_MODEL" "${3:-$ECHO_URL}")"
  code="$(api PATCH /settings "$(providers_with "$WORK/body" "$provider")")"
  [ "$code" = 200 ] || die "$step" "PATCH /settings (echo provider $name) -> $code"
  ECHO_PROVIDERS="${ECHO_PROVIDERS:+$ECHO_PROVIDERS }$name"
  put_provider_key "$step" "$name" "sk-e2e-echo"
}

echo_down() {
  # shellcheck disable=SC2086  # provider 이름은 공백이 없어 낱말로 나눠 넘긴다
  [ -z "$ECHO_PROVIDERS" ] || remove_providers $ECHO_PROVIDERS
  # 라벨이 붙은 것만 지운다. 같은 이름의 다른 네임스페이스는 손대지 않는다
  [ "$ECHO_CREATED" = 0 ] || "${K[@]}" delete namespace -l "$ECHO_LABEL" --wait=false >/dev/null 2>&1 || true
}
