{{/*
대감 차트의 공통 조각 (P85 3절~6절).
*/}}

{{/* 객체 메타데이터의 Helm 표준 라벨. 파드 템플릿에는 붙이지 않는다. 차트 판이 바뀌어도 파드가 굴러가지 않게 하려는 것이다 */}}
{{- define "daegam.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: daegam
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* 이미지 참조 `<repository>:<tag>[@<digest>]`. 레지스트리는 붙이지 않는다. 인자는 (list $ .Values.images.<구성요소>) */}}
{{- define "daegam.imageRef" -}}
{{- $root := index . 0 -}}
{{- $image := index . 1 -}}
{{- printf "%s:%s" $image.repository ($image.tag | default $root.Chart.AppVersion) -}}
{{- with $image.digest }}@{{ . }}{{ end -}}
{{- end }}

{{/* 레지스트리를 붙인 이미지 참조 */}}
{{- define "daegam.image" -}}
{{- $root := index . 0 -}}
{{- printf "%s/%s" (trimSuffix "/" $root.Values.images.registry) (include "daegam.imageRef" .) -}}
{{- end }}

{{/* 이미지 하나의 pull policy. images.pullPolicy 가 비면 digest 가 있을 때만 IfNotPresent 다 */}}
{{- define "daegam.pullPolicy" -}}
{{- $root := index . 0 -}}
{{- $image := index . 1 -}}
{{- $root.Values.images.pullPolicy | default (ternary "IfNotPresent" "Always" (ne $image.digest "")) -}}
{{- end }}

{{/* 플랫폼이 렌더하는 파드(런타임, 에이전트, 웹 Tool, 샌드박스)가 받는 pull policy. 넷이 모두 digest 일 때만 IfNotPresent 다 */}}
{{- define "daegam.homePullPolicy" -}}
{{- $i := .Values.images -}}
{{- $pinned := and $i.runtime.digest $i.agent.digest $i.webTools.digest $i.sandbox.digest -}}
{{- $i.pullPolicy | default (ternary "IfNotPresent" "Always" (ne $pinned "")) -}}
{{- end }}

{{/* 플랫폼 이미지로 뜨는 파드의 imagePullSecrets */}}
{{- define "daegam.pullSecrets" -}}
{{- with .Values.images.pullSecret }}
imagePullSecrets:
  - name: {{ . }}
{{- end }}
{{- end }}

{{/* 세 Secret 의 이름. existingSecret 이 있으면 그 이름이다 */}}
{{- define "daegam.secretName.platform" -}}
{{- .Values.secrets.platform.existingSecret | default "daegam-platform-secrets" -}}
{{- end }}
{{- define "daegam.secretName.docparse" -}}
{{- .Values.secrets.docparse.existingSecret | default "daegam-docparse-token" -}}
{{- end }}
{{- define "daegam.secretName.mongo" -}}
{{- .Values.secrets.mongo.existingSecret | default "daegam-mongo-auth" -}}
{{- end }}

{{/*
차트가 스스로 정하는 env 이름 (P85 4절). daegam.configData 가 값에서 만들어 쓸 수 있는 모든 키, 세 Secret 의 키, 컨테이너
env 로 직접 주는 키다. platform.extraConfig 와 세 extraEnv 는 이 렌더가 그 키를 쓰는지와 상관없이 이 이름을 받지 않는다.
values.schema.json 의 definitions.reservedKeys 와 같은 목록이다(platform/tests/test_chart_guards.py 가 맞댄다)
*/}}
{{- define "daegam.reservedKeys" -}}
{{- list
  "DAEGAM_IN_CLUSTER" "DAEGAM_PLATFORM_PORT" "DAEGAM_PUBLIC_URL"
  "DAEGAM_AUTH_MODE" "DAEGAM_MCP_AUDIENCE" "DAEGAM_BOOTSTRAP_ADMIN_GROUP"
  "DAEGAM_OIDC_ISSUER" "DAEGAM_OIDC_AUDIENCE" "DAEGAM_OIDC_INSECURE_HTTP" "DAEGAM_OIDC_CLIENT_ID"
  "DAEGAM_DATA_PLANE_ISSUER" "DAEGAM_IDP_CONFIRM_SECONDS"
  "DAEGAM_HOME_INGRESS_ENABLED" "DAEGAM_HOME_INGRESS_CLASS" "DAEGAM_HOME_INGRESS_CONTROLLER_NAMESPACE"
  "DAEGAM_HOME_INGRESS_DOMAIN" "DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED" "DAEGAM_HOME_INGRESS_RESERVED_HOSTS"
  "DAEGAM_HOME_INGRESS_TLS" "DAEGAM_HOME_NETWORK_POLICY" "DAEGAM_HOME_BLOCK_METADATA"
  "DAEGAM_HOME_RESOURCES" "DAEGAM_HOME_LIMIT_RANGE" "DAEGAM_HOME_REDIS_TLS"
  "DAEGAM_HOME_IMAGES" "DAEGAM_WEB_TOOLS_IMAGE" "DAEGAM_HOME_SANDBOX_IMAGE"
  "DAEGAM_MONGO_URL" "DAEGAM_LOCAL_BOOTSTRAP_USERNAME" "DAEGAM_LOCAL_BOOTSTRAP_PASSWORD"
  "DAEGAM_NAMESPACE" "DAEGAM_DOCPARSE_TOKEN"
  "DAEGAM_LLM_GATEWAY" "DAEGAM_LLM_GATEWAY_NAMESPACE" "DAEGAM_LLM_GATEWAY_IMAGES" "DAEGAM_LLM_GATEWAY_PDB" | toJson -}}
{{- end }}

{{/* ConfigMap 으로만 받는 이름. e2e 가 게이트 동안 ConfigMap 을 고쳐 바꾸는데, env 로 주면 env 가 envFrom 을 이겨 그 고침이
듣지 않는다. 세 extraEnv 는 이 이름도 받지 않는다. values.schema.json 의 definitions.configOnlyKeys 와 같다 */}}
{{- define "daegam.configOnlyKeys" -}}
{{- list
  "DAEGAM_ALLOW_STUB_PROVIDER" "DAEGAM_SANDBOX_ALLOW_UNCHECKED"
  "DAEGAM_MONGO_DB" "DAEGAM_TENANT_HEADER" "DAEGAM_DEFAULT_TENANT" "DAEGAM_TRANSCRIPT_RETENTION_DAYS"
  "DAEGAM_AGENT_SCALE_TO_ZERO" "DAEGAM_AGENT_SLEEP_AFTER_SECONDS" "DAEGAM_AGENT_HOUSEKEEPING_HOURS"
  "DAEGAM_AGENT_HOUSEKEEPING_PARALLEL" "DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS"
  "DAEGAM_EVALUATIONS_ENABLED" "DAEGAM_EVALUATION_RUN_TOKENS" "DAEGAM_EVALUATION_RUNS_PER_DAY"
  "DAEGAM_EVALUATION_RESULT_DAYS" "DAEGAM_EVALUATION_GATE_CRITERIA" | toJson -}}
{{- end }}

{{/* 차트가 만드는 Secret 의 종류(platform, docparse, mongo) 목록. existingSecret 이 있는 것과 MongoDB 를 함께 올리지
않을 때의 mongo 는 빠진다 */}}
{{- define "daegam.generatedKinds" -}}
{{- $kinds := list -}}
{{- if not .Values.secrets.platform.existingSecret }}{{ $kinds = append $kinds "platform" }}{{ end -}}
{{- if not .Values.secrets.docparse.existingSecret }}{{ $kinds = append $kinds "docparse" }}{{ end -}}
{{- if and .Values.mongodb.bundled (not .Values.secrets.mongo.existingSecret) }}{{ $kinds = append $kinds "mongo" }}{{ end -}}
{{- toJson $kinds -}}
{{- end }}

{{/* 클러스터에 닿는 렌더인가. lookup 은 클러스터 없는 렌더(helm template, lint, client dry run)에서도 처음 설치에서도
빈 값이다. 늘 있는 kube-system 네임스페이스가 보이면 클러스터에 닿은 것이다 (P85 5절) */}}
{{- define "daegam.online" -}}
{{- if lookup "v1" "Namespace" "" "kube-system" }}true{{ end -}}
{{- end }}

{{/* 릴리스 네임스페이스의 산 v1 객체 하나를 JSON 으로 낸다. 클러스터 범위의 kind(Namespace)는 네임스페이스를 보지 않고
그 이름의 객체다. 클러스터 없는 렌더와 없는 객체는 {} 다. 차트가 산 상태를 읽는 곳은 모두 이것을 부른다. 인자는
(list $ kind name) */}}
{{- define "daegam.live" -}}
{{- $root := index . 0 -}}
{{- toJson (lookup "v1" (index . 1) $root.Release.Namespace (index . 2)) -}}
{{- end }}

{{/* 16진수 무작위 값 48자(192비트). URL 과 설정 파일에 그대로 들어가도록 16진수다. randAlphaNum 은 crypto/rand 다 */}}
{{- define "daegam.hex" -}}
{{- randAlphaNum 64 | sha256sum | trunc 48 -}}
{{- end }}

{{/*
차트가 만드는 Secret 의 값 (P85 5절). 산 Secret 이 있으면 그 값을 그대로 두고 없을 때만 새로 만든다. 한 렌더 안에서 처음
부를 때 정해 .Values 의 숨은 키에 두고 다음부터 그 값을 돌려준다. 그래야 MongoDB 비밀번호와 그 URL, 문서 파싱 토큰과 그
체크섬이 템플릿 파일이 달라도 같다. 만드는 Secret 이 있는데 클러스터에 닿지 않는 렌더면 멈춘다. 산 값을 읽지 못한 채 새
값을 내면 적용하는 순간 살아 있는 비밀번호를 덮기 때문이다. MongoDB 를 함께 올리고 그 Secret 을 만드는데 PVC 는 있고 Secret
은 없으면 멈춘다. root 사용자는 빈 볼륨으로 처음 뜰 때만 만들어지므로 새 비밀번호는 맞지 않는다.
*/}}
{{- define "daegam.generatedSecrets" -}}
{{- if not (hasKey .Values "__daegamGeneratedSecrets") -}}
{{- $kinds := include "daegam.generatedKinds" . | fromJsonArray -}}
{{- $out := dict -}}
{{- if $kinds -}}
{{- if not (include "daegam.online" .) -}}
{{- $names := list -}}
{{- range $kinds }}{{ $names = append $names (printf "secrets.%s.existingSecret" .) }}{{ end -}}
{{- fail (printf "this render has no cluster access (helm template, helm lint or a client dry run), so it cannot read the live Secrets that the chart generates once and keeps; name them with %s, or render against the cluster (helm upgrade --install, or --dry-run=server)" (join ", " $names)) -}}
{{- end -}}
{{- if has "mongo" $kinds -}}
{{- $live := (include "daegam.live" (list . "Secret" "daegam-mongo-auth") | fromJson).data | default dict -}}
{{- if and (not $live) (include "daegam.live" (list . "PersistentVolumeClaim" "data-mongodb-0") | fromJson) -}}
{{- fail (printf "the PVC data-mongodb-0 exists in %s but the Secret daegam-mongo-auth does not; MongoDB made its root user from the old Secret, so a new password would not match. Restore the old Secret, or delete the PVC to start with an empty database" .Release.Namespace) -}}
{{- end -}}
{{- $_ := set $out "mongoUsername" ((get $live "username" | b64dec) | default "daegam") -}}
{{- $_ := set $out "mongoPassword" ((get $live "password" | b64dec) | default (include "daegam.hex" .)) -}}
{{- end -}}
{{- if has "platform" $kinds -}}
{{- if not (has "mongo" $kinds) -}}
{{- fail "secrets.platform is generated only together with a generated secrets.mongo and the bundled MongoDB, because its DAEGAM_MONGO_URL carries that password; name secrets.platform.existingSecret, or leave secrets.mongo.existingSecret empty" -}}
{{- end -}}
{{- $live := (include "daegam.live" (list . "Secret" "daegam-platform-secrets") | fromJson).data | default dict -}}
{{- $admin := .Values.auth.adminUsername | default (printf "admin-%s" (include "daegam.hex" . | trunc 6)) -}}
{{- $_ := set $out "adminUsername" ((get $live "DAEGAM_LOCAL_BOOTSTRAP_USERNAME" | b64dec) | default $admin) -}}
{{- $_ := set $out "adminPassword" ((get $live "DAEGAM_LOCAL_BOOTSTRAP_PASSWORD" | b64dec) | default (include "daegam.hex" .)) -}}
{{- end -}}
{{- if has "docparse" $kinds -}}
{{- $live := (include "daegam.live" (list . "Secret" "daegam-docparse-token") | fromJson).data | default dict -}}
{{- $_ := set $out "docparseToken" ((get $live "token" | b64dec) | default (include "daegam.hex" .)) -}}
{{- end -}}
{{- end -}}
{{- $_ := set .Values "__daegamGeneratedSecrets" $out -}}
{{- end -}}
{{- toJson (get .Values "__daegamGeneratedSecrets") -}}
{{- end }}

{{/* 문서 파싱 토큰의 체크섬. 차트가 만든 토큰일 때만이다. 토큰 Secret 을 지워 새 토큰이 생기면 플랫폼과 문서 파싱 서비스가
함께 굴러야 한다. 한쪽만 새 토큰이면 모든 추출이 401 이다 */}}
{{- define "daegam.docparseTokenSha" -}}
{{- $generated := include "daegam.generatedSecrets" . | fromJson -}}
{{- with $generated.docparseToken }}{{ sha256sum . | trunc 16 }}{{ end -}}
{{- end }}

{{/* MongoDB 비밀번호의 체크섬. 차트가 만든 비밀번호일 때만이다. MongoDB URL 이 그 값으로 바뀌면 플랫폼이 다시 떠 새 URL 을
읽는다 */}}
{{- define "daegam.mongoAuthSha" -}}
{{- $generated := include "daegam.generatedSecrets" . | fromJson -}}
{{- with $generated.mongoPassword }}{{ sha256sum . | trunc 16 }}{{ end -}}
{{- end }}

{{/* 함께 올린 MongoDB 의 클러스터 안 주소. 인자는 (dict "user" "password" "ns") */}}
{{- define "daegam.mongoUrl" -}}
{{- printf "mongodb://%s:%s@mongodb.%s.svc:27017/?authSource=admin" .user .password .ns -}}
{{- end }}

{{/*
플랫폼 ConfigMap 의 data (D15, P85 4절). 값에서 만든 키와 platform.extraConfig 를 합친다. 체크섬이 같은 글을 보도록
ConfigMap 과 체크섬이 이 하나를 부른다. extraConfig 는 아래 기본 키(DAEGAM_MONGO_DB 등)를 덮거나 새 키를 더할 수 있고,
daegam.reservedKeys 의 이름이면 렌더가 멈춘다(스키마가 먼저 막는다)
*/}}
{{- define "daegam.configData" -}}
{{- $v := .Values -}}
{{- $x := $v.home.exposure -}}
{{- $defaults := dict
  "DAEGAM_MONGO_DB" "daegam"
  "DAEGAM_TENANT_HEADER" "X-Daegam-Tenant"
  "DAEGAM_DEFAULT_TENANT" "default"
  "DAEGAM_TRANSCRIPT_RETENTION_DAYS" "30" -}}
{{- $d := dict
  "DAEGAM_IN_CLUSTER" "true"
  "DAEGAM_PLATFORM_PORT" "9000"
  "DAEGAM_PUBLIC_URL" (printf "%s://%s" $v.scheme $v.host)
  "DAEGAM_AUTH_MODE" $v.auth.mode
  "DAEGAM_MCP_AUDIENCE" $v.auth.mcpAudience
  "DAEGAM_HOME_INGRESS_ENABLED" (toString $x.enabled)
  "DAEGAM_HOME_INGRESS_ALLOW_UNAUTHENTICATED" (toString $x.allowUnauthenticated)
  "DAEGAM_HOME_INGRESS_RESERVED_HOSTS" $v.host
  "DAEGAM_WEB_TOOLS_IMAGE" (include "daegam.imageRef" (list . $v.images.webTools))
  "DAEGAM_HOME_SANDBOX_IMAGE" (include "daegam.imageRef" (list . $v.images.sandbox)) -}}
{{- with $v.auth.oidc.issuer }}
{{- $_ := set $d "DAEGAM_OIDC_ISSUER" . }}
{{- $_ := set $d "DAEGAM_OIDC_INSECURE_HTTP" (toString $v.auth.oidc.insecureHttp) }}
{{- with $v.auth.oidc.audience }}{{ $_ := set $d "DAEGAM_OIDC_AUDIENCE" . }}{{ end }}
{{- with $v.auth.oidc.clientId }}{{ $_ := set $d "DAEGAM_OIDC_CLIENT_ID" . }}{{ end }}
{{- end }}
{{- /* 데이터 플레인 발급자와 긴 한도(P53b 6.2절). 모든 모드에서 ConfigMap 에 적어 게이트가 ConfigMap 을 고쳐 바꾼다. 기본값을 템플릿에도
두는 것은 helm upgrade --reuse-values 가 이 키가 없던 옛 값을 그대로 쓰기 때문이다 */}}
{{- $o := $v.auth.oidc | default dict }}
{{- $_ := set $d "DAEGAM_DATA_PLANE_ISSUER" ($o.dataPlaneIssuer | default "platform") }}
{{- $_ := set $d "DAEGAM_IDP_CONFIRM_SECONDS" ($o.confirmSeconds | default 604800 | int | toString) }}
{{- with $v.auth.bootstrapAdminGroup }}{{ $_ := set $d "DAEGAM_BOOTSTRAP_ADMIN_GROUP" . }}{{ end }}
{{- with ($x.className | default $v.ingress.className) }}{{ $_ := set $d "DAEGAM_HOME_INGRESS_CLASS" . }}{{ end }}
{{- with $x.controllerNamespace }}{{ $_ := set $d "DAEGAM_HOME_INGRESS_CONTROLLER_NAMESPACE" . }}{{ end }}
{{- with $x.domain }}{{ $_ := set $d "DAEGAM_HOME_INGRESS_DOMAIN" . }}{{ end }}
{{- with ($x.tls | default (ternary "edge" "" (eq $v.ingress.tls "edge"))) }}{{ $_ := set $d "DAEGAM_HOME_INGRESS_TLS" . }}{{ end }}
{{- with $v.home.networkPolicy }}{{ $_ := set $d "DAEGAM_HOME_NETWORK_POLICY" . }}{{ end }}
{{- /* 빈 글자는 적지 않아 화면의 값을 둔다. false 는 false 로 적는다 */}}
{{- if kindIs "bool" $v.home.blockMetadata }}{{ $_ := set $d "DAEGAM_HOME_BLOCK_METADATA" (toString $v.home.blockMetadata) }}{{ end }}
{{- with $v.home.resources }}{{ $_ := set $d "DAEGAM_HOME_RESOURCES" (toJson .) }}{{ end }}
{{- with $v.home.limitRange }}{{ $_ := set $d "DAEGAM_HOME_LIMIT_RANGE" (toJson .) }}{{ end }}
{{- with $v.home.shippedRedis }}{{ $_ := set $d "DAEGAM_HOME_REDIS_TLS" (toJson .) }}{{ end }}
{{- $images := dict
  "registry_prefix" (trimSuffix "/" $v.images.registry)
  "pull_policy" (include "daegam.homePullPolicy" .)
  "runtime_image" (include "daegam.imageRef" (list . $v.images.runtime))
  "agent_image" (include "daegam.imageRef" (list . $v.images.agent)) -}}
{{- with $v.images.pullSecret }}{{ $_ := set $images "pull_secret" . }}{{ end }}
{{- $_ := set $d "DAEGAM_HOME_IMAGES" (toJson $images) }}
{{- $gw := $v.llmGateway }}
{{- $_ := set $d "DAEGAM_LLM_GATEWAY" $gw.mode }}
{{- $_ := set $d "DAEGAM_LLM_GATEWAY_NAMESPACE" $gw.namespace }}
{{- $_ := set $d "DAEGAM_LLM_GATEWAY_IMAGES" (include "daegam.gatewayImages" .) }}
{{- $_ := set $d "DAEGAM_LLM_GATEWAY_PDB" (toString $gw.pdb.enabled) }}
{{- $reserved := include "daegam.reservedKeys" . | fromJsonArray }}
{{- range $key, $value := $v.platform.extraConfig }}
{{- if has $key $reserved }}
{{- fail (printf "platform.extraConfig.%s: the chart sets this name itself; set it through its value instead" $key) }}
{{- end }}
{{- $_ := set $defaults $key (toString $value) }}
{{- end }}
{{- toYaml (merge $d $defaults) }}
{{- end }}

{{/* LLM 게이트웨이 한 벌의 이미지 넷 (DAEGAM_LLM_GATEWAY_IMAGES, platform/daegam/store/llm_gateway.py LlmGatewayImages 꼴).
모두 digest 로 못 박았으므로 IfNotPresent 다. 모드와 상관없이 낸다 (P85 8절) */}}
{{- define "daegam.gatewayImages" -}}
{{- $i := .Values.llmGateway.images -}}
{{- $out := dict "pull_policy" "IfNotPresent" -}}
{{- range $name := list "gateway" "ratelimit" "redis" "collector" }}
{{- $_ := set $out (printf "%s_image" $name) (index $i $name).image }}
{{- end }}
{{- with $i.registryPrefix }}{{ $_ := set $out "registry_prefix" (trimSuffix "/" .) }}{{ end }}
{{- with $i.pullSecret }}{{ $_ := set $out "pull_secret" . }}{{ end }}
{{- toJson $out -}}
{{- end }}

{{/* ConfigMap 의 체크섬 (키 daegam.devops.ai.kr/config-sha). 설정만 바뀌어도 플랫폼이 새 값으로 다시 뜬다 */}}
{{- define "daegam.configSha" -}}
{{- include "daegam.configData" . | sha256sum | trunc 16 -}}
{{- end }}

{{/* UI 가 읽는 두 키의 체크섬. 인증 모드가 바뀌면 UI 도 굴러 CSP 가 플랫폼과 같은 모드를 따른다(D52) */}}
{{- define "daegam.uiConfigSha" -}}
{{- $d := include "daegam.configData" . | fromYaml -}}
{{- printf "%s\n%s" $d.DAEGAM_AUTH_MODE ($d.DAEGAM_OIDC_ISSUER | default "") | sha256sum | trunc 16 -}}
{{- end }}

{{/* PodDisruptionBudget 를 둘 것인가. 켜 두었어도 복제가 하나면 두지 않는다. 하나뿐인 파드를 남기라는 PDB 는 drain 을 막는다 */}}
{{- define "daegam.pdb" -}}
{{- if and .pdb.enabled (ge (int .replicas) 2) }}true{{ end -}}
{{- end }}
