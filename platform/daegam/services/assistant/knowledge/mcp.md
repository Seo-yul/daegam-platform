# MCP Server
Backend API 로 MCP Server 를 만들거나, 배포된 것이 Ready 가 되기를 기다리거나, redeploy 가 필요한지 판단할 때 읽는다. MCP Server 는 정의(어떤 Tool 을 낼지)와 배포(클러스터에 떠 있는 것)를 한 문서에 담는다. 허브에 있는 것은 "허브 항목" 이라 부르고 이 리소스만 MCP Server 라 부른다.

## 정의
- MCP Server 는 어떤 백엔드의 어떤 버전에서 어떤 Tool 을 낼지 정한 문서다. backends[] 가 핵심이고 항목은 {backend_profile_id, spec_version, tools[], tool_overrides{}} 다.
- tools 가 비어 있으면 그 버전의 Tool 전부가 노출된다. 적으면 그 이름만 노출된다. 이름은 Backend API 버전의 tool_names 에 있어야 한다.
- tool_overrides 는 {operationId: {name?, description?}} 이고 이 문서에서만 다르게 부르는 이름과 설명이다. 이름 규칙은 `[A-Za-z0-9_]` 1~64자이고 밑줄이 양끝에 오거나 겹치면 안 된다.
- 백엔드 여러 개를 한 문서에 합칠 수 있다. 오버라이드 뒤의 Tool 이름은 문서 전체에서 유일해야 한다.
- 정의는 리비전을 가진다. create_mcp_server 가 리비전 1 이고 update_mcp_server(PUT, 전체 교체)마다 리비전이 하나 는다. name 은 바꿀 수 없고 display_name 은 바뀐다.
- runtime{image, replicas, resources} 와 bindings[{data_service_id, env_prefix}] 는 선택이다. 기본값이면 보내지 않는다.
- 만들기는 저장이다. state 가 draft 이고 클러스터에는 아무것도 없다. 띄우려면 deploy_mcp_server 를 따로 부른다.

## 상태
- state 는 draft(저장만 됨), deploying, deployed(deployed_revision 이 핀돼 떠 있음), failed(마지막 배포 실패, last_error 에 이유, 리컨실이 다시 시도), undeploying, deleting 중 하나다. 파드의 건강은 status.phase 가 따로 말한다.
- revision 은 저장된 리비전, deployed_revision 은 배포된 리비전이다. 둘이 다르면 저장은 됐지만 아직 재배포하지 않은 것이다.

## 배포
- deploy_mcp_server 본문은 {cluster_id?, namespace?, exposure?} 다. 정의는 문서에 있으니 보내지 않는다. 현재 리비전이 핀된다.
- cluster_id 를 비우면 테넌트 기본, 플랫폼 기본, home 순으로 정해진다. namespace 를 비우면 테넌트 기본 네임스페이스(보통 daegam-<tenant>)다. 다른 네임스페이스는 Tenant Settings allowed_namespaces 안에서만 된다.
- 클러스터 리소스 이름은 mcp-<name> 이다. 배포는 deploy 등급이라 승인 카드가 뜬다.
- 승인 뒤 201 이 오면 status.phase 는 보통 Pending 이다. Ready 대기는 get_mcp_server 를 몇 초 간격으로 다시 불러 status.phase 를 본다. draft 의 status 는 null 이다.

- phase 는 Pending(파드가 아직 안 뜸), Rolling(새 스펙 반영 중, 잠시 뒤 다시 본다), Ready(status.url 이 클러스터 안 주소, 노출했으면 status.external_url 이 바깥 주소), Failed(status.message 를 읽어 사용자에게 알린다), Missing(CR 이 사라짐) 이다.
- 같은 이름의 문서가 있으면 409 name_conflict 다. 목록에서 찾아 재사용한다.
- 배포 응답 warnings[] 에 network_policy_unenforced 가 있으면 클러스터가 NetworkPolicy 를 집행하지 않는다는 뜻이다. 로컬 OrbStack 에서는 늘 보이니 놀라지 않는다.
- 같은 정의로 하나 더 띄우려면 clone_mcp_server 로 이름을 바꾼 draft 를 만들고 그것을 배포한다. 복제 본문의 exposure 는 배포 설정으로 남는다.

## 외부 노출
- 배포된 것은 기본으로 클러스터 안에서만 보인다. 밖으로 열려면 배포 본문의 exposure 를 {enabled: true, host?, ingress_class?, tls?} 로 준다.
- host 를 비우면 <name>.<tenant>.<domain> 이 기본값이다. 클러스터에 domain 이 없으면 host 를 반드시 준다. 자세한 규칙과 422 는 exposure 문서에 있다.

## redeploy 와 undeploy
- 정의를 고쳐 리비전이 늘어도 배포는 옛 리비전에 핀돼 있다. redeploy_mcp_server 가 최신 리비전으로 옮긴다.
- Cluster 나 Tenant Settings 의 이미지·리소스 기본값을 바꿔도 떠 있는 것은 바뀌지 않는다. redeploy 가 다시 계산한다.
- 노출 설정만 바꿀 때는 patch_mcp_server 의 exposure 로 한다. 그 안에서 재배포가 함께 일어난다.
- undeploy_mcp_server 는 클러스터에서 내리고 정의는 남긴다(draft 로 돌아간다). 허브에 게시돼 있으면 409 published 라 먼저 게시를 내린다.
- redeploy·undeploy 는 deploy 등급이라 승인이 필요하다. 배포·해제 중(deploying·undeploying)이면 409 in_progress, 삭제 중(deleting)이면 409 deleting 이고, 저장된 Advanced overrides 가 새 렌더에 맞지 않으면 409 overrides_invalid 다.
- delete_mcp_server 는 정의와 클러스터의 것을 모두 지운다. Gateway 나 Agent 가 참조하면 409 in_use 다.

## 흔한 오류와 대처
| code | 뜻 | 대처 |
|---|---|---|
| unknown_backend_api, unknown_spec_version (422) | 백엔드 id 나 버전 번호가 없다 | Backend API 목록과 버전을 다시 조회한다 |
| invalid_backends (422) | backends 가 비었거나 옛 단수 필드와 섞였다 | backends[] 하나로만 보낸다 |
| unknown_tools (422) | tools 에 그 버전에 없는 이름이 있다 | 응답의 available 목록에서 고른다 |
| duplicate_tools, duplicate_tool_name, invalid_tool_name (422) | Tool 이름이 겹치거나 규칙 밖이다 | tool_overrides 로 이름을 바꾸거나 목록을 정리한다 |
| not_deployed (409) | draft 에 배포 쪽 동작을 불렀다 | deploy_mcp_server 를 먼저 부른다 |
| already_deployed (409) | 이미 배포된 것을 다시 배포하려 했다 | redeploy_mcp_server 를 쓴다 |
| published (409) | 게시된 것을 내리려 했다 | unpublish_mcp_server 를 먼저 부른다 |
| in_use (409) | Gateway 나 Agent 가 참조하는 것을 지우려 했다 | 참조하는 쪽을 먼저 정리한다 |
| unknown_cluster, cluster_not_allowed, namespace_not_allowed (422) | 클러스터·네임스페이스가 없거나 허용 밖이다 | cluster_id 와 namespace 를 비우고 기본값을 쓴다 |
| namespace_no_longer_allowed (409) | 재배포하려는 문서의 네임스페이스가 허용 목록에서 빠졌다 | 테넌트 관리자에게 그 네임스페이스를 허용 목록에 다시 넣어 달라고 하거나, 해제한 뒤 새로 배포한다 |
| unknown_data_service, cluster_mismatch (422) | 바인딩한 Database 가 없거나 다른 클러스터에 있다 | 같은 클러스터의 Database 를 고른다 |
| k8s_name_conflict (409) | 같은 네임스페이스에 mcp-<name> 이 이미 있다(응답의 k8s_name·namespace) | 다른 name 을 쓴다 |
| cluster_unreachable, k8s_error (502) | 클러스터에 닿지 못했다 | 잠시 뒤 다시 시도하고 반복되면 운영자에게 알린다 |
