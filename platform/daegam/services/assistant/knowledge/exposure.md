# 외부 노출
배포된 것을 클러스터 밖으로 열어 달라는 요청을 받았거나 exposure 관련 422 를 받았을 때 읽는다.

## 무엇이 생기는가
- 배포된 MCP Server, Gateway, Agent 는 기본으로 클러스터 안에서만 보인다. exposure.enabled 를 켜면 플랫폼이 Ingress 와 NetworkPolicy 를 만든다. MCP Server 는 앞단 프록시가 하나 더 생긴다.
- 주소는 status.external_url 이다. MCP 와 Gateway 는 <scheme>://<host>/mcp 이고 Agent 는 <scheme>://<host> 다. scheme 은 TLS 설정에서 정해지며 따로 고르지 않는다.
- exposure 는 생성(deploy) 본문에 넣는다. 나중에 바꾸려면 MCP 와 Gateway 는 patch 의 exposure, Agent 는 redeploy 본문의 exposure 로 한다.
- 배포는 자기 exposure 스냅샷만 본다. Cluster 의 ingress 설정은 기본값과 허용 범위일 뿐이다.

## 본문
| 필드 | 뜻 |
|---|---|
| enabled | true 면 노출한다. false 면 나머지 필드는 무시된다 |
| host | 소문자 DNS-1123 호스트명. 비우면 <name>.<tenant>.<domain> 이 기본값이고 클러스터에 domain 이 없으면 필수다 |
| ingress_class | 비우면 클러스터 기본값. 클러스터 허용 목록 안이어야 한다 |
| tls | {mode: none, secret, cert-manager, edge 중 하나, secret_name?, issuer?}. 비우면 클러스터 기본 모드다. none 이면 http, 나머지는 https 다. edge 는 Ingress 앞이 TLS 를 끝내는 클러스터의 모드이고, 그 클러스터에서는 edge 만 된다 |
| cors_origins[] | Agent 만. 브라우저 origin 목록. `https://app.example.com` 꼴이고 경로가 없다 |
| allowed_groups[] | Agent 만. 비어 있을 때 oidc 모드는 인증된 주체 전부가 부를 수 있고 화면이 그 사실을 경고로 보인다. local 모드는 플랫폼이 그 테넌트의 MCP 그룹 `daegam-<tenant>` 로 채워 내려 그 테넌트의 멤버만 부른다 |

- tls.mode 가 secret 이면 secret_name 은 배포 네임스페이스에 있는 type=kubernetes.io/tls Secret 의 이름이다. cert-manager 면 issuer 는 ClusterIssuer 이름이고 Secret 은 플랫폼이 <k8s.name>-tls 로 짓는다.
- 어떤 값을 쓸 수 있는지는 cluster_exposure_options(클러스터 id, namespace)로 읽는다. ingress_classes, tls_secrets, cluster_issuers, defaults, allow_unauthenticated 가 온다.

## 인증 모드에 따른 규칙
- 플랫폼은 oidc 모드 또는 header 모드로 돈다. oidc 모드에서는 노출 앞단(MCP 앞단 프록시, Gateway 의 vMCP, Agent 런타임)이 토큰을 검증하므로 게이트가 없다.
- header 모드에는 IdP 가 없어 노출하면 무인증 엔드포인트가 된다. MCP 와 Gateway 는 Cluster ingress.allow_unauthenticated 가 true 일 때만 생성되고, 생성 응답 warnings[] 와 status.warnings[] 에 unauthenticated_exposure 경고가 붙는다. 로컬 배포본의 home 클러스터만 true 가 보통이다.
- Agent 는 header 모드에서 언제나 422 unauthenticated_exposure 다. 클러스터 설정과 무관하다. oidc 모드로 바꾸는 것은 운영자의 일이므로 사용자에게 그렇게 안내한다.
- 노출 경계 안쪽은 무인증이 정상이다. 내부 주소에 토큰이 필요 없다고 놀라지 않는다.
- Agent 외부 호출의 토큰은 데이터 플레인 audience(daegam-mcp)로 받은 OIDC 액세스 토큰이다. 제어면 API 토큰은 401 invalid_token 이다.

## host 규칙
- host 는 같은 클러스터 안에서 종류와 테넌트를 넘어 유일하다. 플랫폼 자신의 호스트(reserved_hosts)도 쓸 수 없다.
- 문서 하나에 host 는 하나다. 도메인이 더 필요하면 복제로 문서를 하나 더 만들어 배포한다.
- 기본 host 는 name 을 DNS-1123 으로 정규화해 만든다. 노출은 배포한 뒤에도 켤 수 있으므로 생성 때 통과한 이름은 노출 때도 통과한다.

## 흔한 422 와 대처
| code | 뜻 | 대처 |
|---|---|---|
| ingress_disabled | 이 클러스터는 노출을 허용하지 않는다 | enabled 를 끄고 만든 뒤 운영자에게 안내한다 |
| unauthenticated_exposure | header 모드에서 무인증 노출이 막혔다 | MCP 와 Gateway 는 enabled 를 끄거나 운영자에게 allow_unauthenticated 를 묻는다. Agent 는 oidc 모드가 필요하다 |
| invalid_host | 호스트명 형식이 아니거나 domain 이 없어 host 가 비었다 | 소문자 호스트명을 사용자에게 묻는다 |
| host_conflict (409) | 다른 배포가 이미 쓰는 host 다 | 다른 host 를 쓴다 |
| host_reserved (422) | 플랫폼 자신의 호스트(reserved_hosts)다 | 다른 host 를 쓴다 |
| invalid_value, ingress_class_not_allowed, unknown_ingress_class | IngressClass 가 비었거나 허용 밖이거나 없다 | ingress_class 를 비워 클러스터 기본값을 쓴다 |
| tls_secret_invalid | secret 모드인데 secret_name 이 없거나 그 네임스페이스에 kubernetes.io/tls Secret 이 없다 | tls_secrets 목록에서 고르거나 tls.mode 를 none 으로 둔다 |
| cert_manager_unavailable, unknown_issuer | cert-manager 가 없거나 ClusterIssuer 가 없다 | tls.mode 를 none 이나 secret 으로 바꾼다 |
| tls_mode_not_allowed | 클러스터 기본 TLS 가 edge 인데 다른 모드를 줬거나, edge 가 아닌 클러스터에 edge 를 줬다 | allowed 에 적힌 모드를 쓴다. 보통 tls 를 비워 클러스터 기본값을 따른다 |
| invalid_cors_origin | origin 에 경로가 있거나 http(s) 가 아니다 | `https://host` 꼴로 고친다 |

