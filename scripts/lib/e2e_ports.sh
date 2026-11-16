# shellcheck shell=bash
# shellcheck disable=SC2034  # 값을 쓰는 곳은 이 파일을 읽는 스크립트다
# e2e 게이트가 127.0.0.1 에 여는 포트의 배정표. `. "$HERE/lib/e2e_ports.sh"` 로 읽는다.
# 여덟 번째 e2e 를 만드는 사람이 아홉 파일을 열지 않고 여기만 보면 빈 자리를 안다.
#
# 각 스크립트는 자기 기본값을 여기서 읽는다. `E2E_* ` 환경변수로 덮어쓰는 계약은 그대로다.
#   API_PORT="${E2E_API_PORT:-$PORT_KNOWLEDGE_API}"
#
# | 포트   | 변수                  | 게이트          | 무엇 |
# |--------|-----------------------|-----------------|------|
# |  9300  | E2E_OKTA_PORT         | e2e-okta        | 로컬 uvicorn (플랫폼, oidc) |
# | 14490~4| E2E_VMCP_PORT         | e2e-gateway     | vMCP port-forward 다섯 (base, +1, +2, +3, +4) |
# | 15000  | E2E_HUB_PORT          | e2e-gateway     | 허브 레지스트리 zot, TLS. 컨테이너와 러너가 같은 포트다 (STEP 9i, scripts/hub_e2e_registry.sh) |
# | 15001  | E2E_HUB_UPSTREAM_PORT | e2e-gateway     | upstream 평문 registry (STEP 9i, scripts/hub_e2e_registry.sh) |
# | 18082  | E2E_MCP_PORT          | e2e-instance    | MCP Server port-forward |
# | 18091  | E2E_VMCP_PORT         | e2e-mcp-auth    | vMCP port-forward |
# | 18092  | E2E_PROXY_PORT        | e2e-mcp-auth    | 내부 프록시 port-forward |
# | 18093  | E2E_PROXY_PORT        | e2e-pat         | 내부 프록시 port-forward (orbstack 만, 내부 홉) |
# | 18094  | E2E_PROXY_PORT        | e2e-oauth       | 내부 프록시 port-forward (orbstack 만, 내부 홉) |
# | 18097  | E2E_OIDC_PORT         | e2e-assistant   | mock-oidc port-forward (oidc 모드만) |
# | 18098  | E2E_OIDC_PORT         | e2e-mcp-auth    | mock-oidc port-forward |
# | 18099  | E2E_OIDC_PORT         | e2e-agent       | mock-oidc port-forward |
# | 19000  | E2E_API_PORT          | e2e-instance    | 플랫폼 port-forward |
# | 19001  | E2E_API_PORT          | e2e-gateway     | 플랫폼 port-forward |
# | 19002  | E2E_API_PORT          | e2e-agent       | 플랫폼 port-forward |
# | 19003  | E2E_API_PORT          | e2e-assistant   | 플랫폼 port-forward (svc) |
# | 19004  | E2E_API_PORT          | e2e-mcp-auth    | 플랫폼 port-forward |
# | 19005  | E2E_API_PORT          | e2e-knowledge   | 플랫폼 port-forward |
# | 19006  | E2E_POD_A_PORT        | e2e-assistant   | 플랫폼 파드 A port-forward (oidc 모드만, 교차 파드) |
# | 19007  | E2E_POD_B_PORT        | e2e-assistant   | 플랫폼 파드 B port-forward (oidc 모드만, 교차 파드) |
# | 19012  | E2E_RUNTIME_PORT      | e2e-agent       | Agent 런타임 Service·파드 port-forward (13(e), 10m) |
# | 19013  | E2E_UI_PORT           | e2e-ui-serving  | daegam-ui Service port-forward (nginx 직결, D52) |
# | 19014  | E2E_API_PORT          | e2e-ui-serving  | 플랫폼 port-forward (CSP 를 맞댄다) |
# | 19015  | E2E_API_PORT          | e2e-install     | 플랫폼 port-forward (설치본) |
# | 19016  | E2E_UI_PORT           | e2e-install     | daegam-ui port-forward (설치본) |
# | 19017  | E2E_TRAEFIK_PORT      | e2e-ui-serving  | Traefik Service port-forward (LoadBalancer IP 가 없는 클러스터) |
# | 19018  | E2E_API_PORT          | e2e-pat         | 플랫폼 port-forward (orbstack·설치본) |
# | 19020  | E2E_API_PORT          | e2e-oauth       | 플랫폼 port-forward (orbstack·설치본) |
# | 19021  | E2E_API_PORT          | e2e-chat        | 플랫폼 port-forward |
# | 19022  | E2E_RUNTIME_PORT      | e2e-chat        | Agent 런타임 Service port-forward (남의 기억이 보이지 않는지 본다) |
# | 19023  | E2E_API_PORT          | e2e-telemetry   | 플랫폼 port-forward (orbstack. 설치본은 공개 주소다) |
# | 19024  | E2E_API_PORT          | e2e-web-tools   | 플랫폼 port-forward |
# | 19025  | E2E_VMCP_PORT         | e2e-web-tools   | vMCP port-forward |
# | 19026  | E2E_FETCH_PORT        | e2e-web-tools   | fetch 복제본 프록시 port-forward (토큰 audience 확인) |
# | 19027  | E2E_CALC_PORT         | e2e-web-tools   | calculator 프록시 port-forward (토큰 audience 확인) |
# | 19028  | E2E_API_PORT          | e2e-skills      | 플랫폼 port-forward |
# | 19029  | E2E_VMCP_PORT         | e2e-agent       | vMCP port-forward (5g, 실험 토큰) |
# | 19030  | E2E_VMCP_PORT         | e2e-pat         | 규칙 Gateway 의 vMCP port-forward (4r, P52) |
# | 19031  | E2E_RULES_PORT        | e2e-agent       | 규칙을 둔 MCP Server 의 프록시 port-forward (11r, P52) |
# | 19032  | E2E_API_PORT          | e2e-sandbox     | 플랫폼 port-forward (로컬 클러스터. 설치본은 공개 주소다, P40) |
# | 19033  | E2E_API_PORT          | e2e-llm-gateway | 플랫폼 port-forward (orbstack·k3d. 설치본은 공개 주소다) |
# | 19034  | E2E_GATEWAY_PORT      | e2e-llm-gateway | LLM 게이트웨이 Service port-forward (회수한 토큰을 직접 보낸다, P38) |
# | 19035  | E2E_API_PORT          | e2e-provisioning | 플랫폼 port-forward (P44) |
# | 19036  | E2E_CALC_PORT         | e2e-provisioning | calculator 프록시 port-forward (미리 보기의 토큰과 규칙, P44b) |
# | 19037  | E2E_VMCP_PORT         | e2e-provisioning | vMCP port-forward (미리 보기의 gateway_token, P44b) |
# | 19038  | E2E_API_PORT          | e2e-oidc-platform | 플랫폼 port-forward (P53b) |
# | 19039  | E2E_OIDC_PORT         | e2e-oidc-platform | mock-oidc port-forward (P53b) |
# | 19040  | E2E_VMCP_PORT         | e2e-oidc-platform | 규칙 Gateway 의 vMCP port-forward (P53b) |
# | 19041  | E2E_API_PORT          | e2e-exchange    | 플랫폼 port-forward (P57) |
# | 19042  | E2E_VMCP_PORT         | e2e-exchange    | 소비자 Gateway C 의 vMCP port-forward (P57) |
# | 19043  | E2E_AUX_PORT          | e2e-exchange    | 사슬의 C2, 게시된 P1·P 의 vMCP port-forward (P57) |
# | 19044  | E2E_API_PORT          | e2e-evaluation  | 플랫폼 port-forward (P45) |
#
# 같은 변수 이름 E2E_VMCP_PORT 가 게이트마다 다른 뜻인 것도 여기에 적어 둔다
# (e2e-gateway 는 다섯을 쓰는 base, e2e-mcp-auth·e2e-agent 는 하나). E2E_PROXY_PORT 도 게이트마다 다른 포트다.
# 허브 레지스트리는 macOS 의 AirPlay 수신기가 쓰는 5000 을 피한다.

PORT_OKTA_API=9300
PORT_GATEWAY_VMCP_BASE=14490
PORT_HUB_REGISTRY=15000
PORT_HUB_UPSTREAM=15001
PORT_INSTANCE_MCP=18082
PORT_MCP_AUTH_VMCP=18091
PORT_MCP_AUTH_PROXY=18092
PORT_PAT_PROXY=18093
PORT_OAUTH_PROXY=18094
PORT_ASSISTANT_OIDC=18097
PORT_MCP_AUTH_OIDC=18098
PORT_AGENT_OIDC=18099
PORT_INSTANCE_API=19000
PORT_GATEWAY_API=19001
PORT_AGENT_API=19002
PORT_ASSISTANT_API=19003
PORT_MCP_AUTH_API=19004
PORT_KNOWLEDGE_API=19005
PORT_ASSISTANT_POD_A=19006
PORT_ASSISTANT_POD_B=19007
PORT_AGENT_RUNTIME=19012
PORT_UI_DIRECT=19013
PORT_UI_API=19014
PORT_INSTALL_API=19015
PORT_INSTALL_UI=19016
PORT_UI_TRAEFIK=19017
PORT_PAT_API=19018
PORT_OAUTH_API=19020
PORT_CHAT_API=19021
PORT_CHAT_RUNTIME=19022
PORT_TELEMETRY_API=19023
PORT_WEB_TOOLS_API=19024
PORT_WEB_TOOLS_VMCP=19025
PORT_WEB_TOOLS_FETCH=19026
PORT_WEB_TOOLS_CALC=19027
PORT_SKILLS_API=19028
PORT_AGENT_VMCP=19029
PORT_PAT_VMCP=19030
PORT_AGENT_RULES=19031
PORT_SANDBOX_API=19032
PORT_LLM_GATEWAY_API=19033
PORT_LLM_GATEWAY_PROXY=19034
PORT_PROVISIONING_API=19035
PORT_PROVISIONING_CALC=19036
PORT_PROVISIONING_VMCP=19037
PORT_OIDC_PLATFORM_API=19038
PORT_OIDC_PLATFORM_OIDC=19039
PORT_OIDC_PLATFORM_VMCP=19040
PORT_EXCHANGE_API=19041
PORT_EXCHANGE_VMCP=19042
PORT_EXCHANGE_AUX=19043
PORT_EVALUATION_API=19044
