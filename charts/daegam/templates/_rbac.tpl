{{/*
플랫폼의 권한 규칙 (P3, P8, P40, P50, P63). 동사와 자원은 일부러 하나씩 적는다(`*` 없음). 규칙 목록은 여기 하나에 두고
rbac.yaml 이 ClusterRole 에 싣는다.
*/}}

{{/* ClusterRole daegam-platform 의 규칙 */}}
{{- define "daegam.rules.platform" -}}
# 이 ClusterRole 에는 Secret 동사가 없다 (P63, D75). 테넌트 네임스페이스의 Secret(ds-{id}, 토큰 Secret, 백엔드 자격 증명)은
# 네임스페이스마다 daegam-tenant-objects 로, 플랫폼 네임스페이스의 것은 Role daegam-platform 으로 받는다.
# 플랫폼이 렌더하는 ToolHive CRD (P3: mcpservers, mcpgroups, P4: 나머지)
- apiGroups: [toolhive.stacklok.dev]
  resources:
    - mcpservers
    - mcpgroups
    - virtualmcpservers
    - mcpremoteproxies
    - mcpserverentries
    - mcptoolconfigs
    - mcpoidcconfigs
    - mcpexternalauthconfigs  # D41: 소비자가 안쪽 MCPServer 에 낼 서비스 토큰의 자리
  verbs: [get, list, watch, create, update, patch, delete]
- apiGroups: [toolhive.stacklok.dev]
  resources:
    - mcpservers/status
    - mcpgroups/status
    - virtualmcpservers/status
    - mcpremoteproxies/status
    - mcpserverentries/status
    - mcptoolconfigs/status
  verbs: [get]
# 테넌트 네임스페이스(daegam-{tenant})와 그 quota·LimitRange
- apiGroups: [""]
  resources: [namespaces]
  verbs: [get, create, patch]
- apiGroups: [""]
  resources: [resourcequotas, limitranges]
  verbs: [get, list, watch, create, update, patch, delete]
# 상태 읽기(인스턴스 phase, operator preflight). pods/log 는 테넌트 네임스페이스 프록시 감사 로그 수집이다 (P6 F5)
- apiGroups: [""]
  resources: [pods, pods/log]
  verbs: [get, list]
# D41: 제어면은 내부 MCPServer 의 Tool 목록을 Bearer 토큰으로 부른다. API 서버의 services/proxy 는 Authorization 을 지우므로
# home 밖의 호출은 pods/portforward 로 지난다
- apiGroups: [""]
  resources: [pods/portforward]
  verbs: [create]
# Deployment·Service: preflight 가 Deployment 를 나열하고(NetworkPolicy 컨트롤러 탐지), Agent(D26)는 플랫폼이 적용(SSA)하고
# 이름으로 지우며 daegam.devops.ai.kr/id 라벨로 쓸어 담는(list) Deployment 와 Service agent-<name> 이다. T24 는 플랫폼보다
# 먼저 떠 JWKS 를 받지 못한 ToolHive 검증기 Deployment 의 restartedAt 어노테이션을 고친다(rollout restart)
- apiGroups: [apps]
  resources: [deployments]
  verbs: [get, list, create, patch, delete]
- apiGroups: [apps]
  resources: [statefulsets]
  verbs: [get, list]
- apiGroups: [""]
  resources: [services]
  verbs: [create, get, list, patch, delete]
# D21: 제어면은 원격 클러스터의 MCP 프록시에 API 서버의 services/proxy 로 닿는다(get = GET, create = POST). home 에서는 쓰지 않는다
- apiGroups: [""]
  resources: [services/proxy]
  verbs: [get, create]
# F3a: NetworkPolicy 컨트롤러 탐지(calico-node, cilium, kube-router DaemonSet)
- apiGroups: [apps]
  resources: [daemonsets]
  verbs: [get, list]
# F3a: 테넌트 네임스페이스의 ingress NetworkPolicy(daegam-tenant-ingress). P8: 인스턴스마다의 daegam-expose-<name> 은 인스턴스와
# 함께 지운다. D26: Agent 정책 daegam-agent-<name> 은 daegam.devops.ai.kr/id 라벨로 쓸어 담는다(list)
- apiGroups: [networking.k8s.io]
  resources: [networkpolicies]
  verbs: [get, list, create, patch, delete]
# P8 (D22): 인스턴스 노출. 노출한 인스턴스마다의 Ingress, 폼의 IngressClass 목록, cert-manager ClusterIssuer 목록과 Certificate
# 읽기(그 CRD 가 있을 때만 쓴다)
- apiGroups: [networking.k8s.io]
  resources: [ingresses]
  verbs: [get, create, patch, delete]
- apiGroups: [networking.k8s.io]
  resources: [ingressclasses]
  verbs: [list]
- apiGroups: [cert-manager.io]
  resources: [clusterissuers]
  verbs: [list]
# 노출 상태의 certificate_ready 는 테넌트 네임스페이스의 Certificate 에서 읽는다. 이 권한이 없으면 발급이 끝난 뒤에도 조회가
# AccessDenied 로 삼켜져 상태가 영영 null 이다 (T5 실측)
- apiGroups: [cert-manager.io]
  resources: [certificates]
  verbs: [get]
# P8 (D22): 노출 폼의 TLS Secret 이름. `secrets list` 는 클러스터 전체에 주지 않는다. 플랫폼이 묶이지 않은 ClusterRole
# daegam-tls-reader 를 허용된 테넌트 네임스페이스마다 RoleBinding 으로 묶는다. 그 묶음을 만들려면 바로 그 ClusterRole 의
# `bind`(RBAC 상승 검사)와 rolebindings 쓰기가 있어야 한다. subject 는 SelfSubjectReview 로 본 플랫폼 자신이다
- apiGroups: [rbac.authorization.k8s.io]
  resources: [clusterroles]
  verbs: [bind]
  resourceNames: [daegam-tls-reader]
# P50 (D69): 테넌트 네임스페이스에 두는 플랫폼 자신의 객체(MCPAuthzConfig, 원격 프록시의 ServiceAccount,
# LocalSubjectAccessReview)는 묶이지 않은 ClusterRole daegam-tenant-objects 에서 온다. `get` 으로 preflight 가 그 역할이
# 있는지 보고, 이 둘이 없으면 그 역할이 필요한 기능은 물러난다(platform/daegam/k8s/gates.py)
- apiGroups: [rbac.authorization.k8s.io]
  resources: [clusterroles]
  verbs: [get, bind]
  resourceNames: [daegam-tenant-objects]
- apiGroups: [rbac.authorization.k8s.io]
  resources: [rolebindings]
  verbs: [get, create, patch, delete]
- apiGroups: [authentication.k8s.io]
  resources: [selfsubjectreviews]
  verbs: [create]
# P40 (D95): 코드 실행 샌드박스. 메모리 규칙과 shared_nodes 는 노드를 읽고, 샌드박스 게이트는 PriorityClass daegam-sandbox 를
# 읽는다. 샌드박스 파드는 테넌트 네임스페이스마다 daegam-tenant-objects 로 만들고 지운다. pods/exec, pods/attach,
# pods/ephemeralcontainers 는 어디에도 없다
- apiGroups: [""]
  resources: [nodes]
  verbs: [get, list]
- apiGroups: [scheduling.k8s.io]
  resources: [priorityclasses]
  verbs: [get]
  resourceNames: [daegam-sandbox]
{{- end }}

{{/* ClusterRole daegam-tls-reader 의 규칙. ClusterRoleBinding 없이 허용된 테넌트 네임스페이스마다 RoleBinding 으로 묶여
노출 폼이 TLS Secret 의 이름만 나열한다(PartialObjectMetadataList, data 는 읽지 않는다) */}}
{{- define "daegam.rules.tlsReader" -}}
- apiGroups: [""]
  resources: [secrets]
  verbs: [list]
{{- end }}

{{/* ClusterRole daegam-tenant-objects 의 규칙 (P50, D69). 플랫폼이 허용된 테넌트 네임스페이스마다 RoleBinding 으로 묶는다.
MCPAuthzConfig 는 CR 마다의 Cedar 이고, ServiceAccount daegam-remote-proxy 는 원격 프록시를 Role 없이 돌리며,
LocalSubjectAccessReview 는 그 계정이 Secret 을 읽지 못함을 확인한다. 테넌트의 Secret(Database 자격 증명, 서비스 토큰)은
여기서 네임스페이스마다 주고 클러스터 전체에는 주지 않는다(P63, D75). list 와 watch 는 없다 */}}
{{- define "daegam.rules.tenantObjects" -}}
- apiGroups: [toolhive.stacklok.dev]
  resources: [mcpauthzconfigs]
  verbs: [get, list, create, patch, delete]
- apiGroups: [""]
  resources: [serviceaccounts]
  verbs: [get, create, patch]
- apiGroups: [authorization.k8s.io]
  resources: [localsubjectaccessreviews]
  verbs: [create]
- apiGroups: [""]
  resources: [secrets]
  verbs: [get, create, update, patch, delete]
# P40 (D95): Agent 의 네임스페이스와 격리 점검의 daegam-probe 에 세션마다 샌드박스 파드 하나. 만들고 지우기만 한다. 읽기는
# 클러스터 전체의 pods get·list 다
- apiGroups: [""]
  resources: [pods]
  verbs: [create, delete]
{{- end }}
