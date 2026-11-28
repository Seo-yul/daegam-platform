# 대감 로컬 개발 환경 타깃
#
#   make cluster-up          OrbStack K8s 활성화 + 노드 Ready 대기
#   make operator-install    ToolHive CRD + 오퍼레이터 Helm 설치 (멱등). 차트 값은 deploy/toolhive/operator-values.yaml 이고
#                            사용량 지표가 빠진 run configuration 과 프록시 롤아웃까지 기다린다 (P50 G1)
#   make cert-manager-up     cert-manager + self-signed ClusterIssuer 설치 (멱등, T5). 이것을 올리면 e2e-gateway
#                            STEP 16 이 SKIP 대신 tls.mode cert-manager 경로를 실제로 돈다. 인증서는 필요 없다
#   make image               spec2openapi·mcp-runtime·agent-runtime 런타임 이미지 + lab 이미지 빌드
#   make image-s2o           spec2openapi:$(S2O_VERSION) (WSDL/OpenAPI 변환기. make image 가 함께 만든다)
#   make image-runtime       daegam-mcp-runtime:$(RUNTIME_VERSION) (P3 F1)
#   make image-agent         daegam-agent-runtime:$(AGENT_VERSION) (D26. agent-runtime/, Platform Settings default_agent_image 의 기본 태그)
#   make image-mock-oidc     daegam-mock-oidc:$(MOCK_OIDC_VERSION) (P6 dev, deploy/dev/mock-oidc.yaml 로 배포)
#   make image-web-tools     daegam-web-tools:$(WEB_TOOLS_VERSION) (P39b. web_fetch 복제본 이미지, default_web_tools_image)
#   make image-sandbox       daegam-sandbox:$(SANDBOX_VERSION) (P40. 세션마다의 코드 실행 작업 공간 파드 이미지. make image 는
#                            굽지 않는다. 릴리스 후보는 release-images 가 굽는다)
#   make image-lab           deploy/dev/lab/images 의 stub-spec-server·mock-soap·mock-petstore·header-echo·env-echo :dev 이미지
#   make lab-up              e2e 가 상시로 쓰는 로컬 실험용 백엔드(deploy/dev/lab, 네임스페이스 $(SPIKE_NS)) 배포 + verify.sh 게이트.
#                            멱등 (P1 스파이크 실증 기록은 docs/archive/reviews/2026-09-02-spike-d-plan.md)
#   make lab-clean           lab 네임스페이스 삭제
#   make test                platform 단위 테스트 (pytest)
#   make lint                platform ruff 와 lint-scripts
#   make lint-scripts        추적하는 bash·sh 스크립트 shellcheck (warning 수준). shellcheck 가 없으면 실패
#   make verify              푸시 전 로컬 게이트: 아래 갈래 셋 전부와 VERIFY_EXTRA. DB 가 있어야 도는 시험은 시험용 DB
#                            주소가 있을 때만 돌고, 없으면 건너뛴 것을 찍는다. 그 시험은 PR 의 pr-check 가 GitHub 에서
#                            돌린다. VERIFY_EXTRA 는 이 파일 끝이 읽는 ops/*/*.mk 만 둔다(공개 트리에는 없다)
#   make verify-ui           UI 만: tsc·vitest·schema.d.ts 드리프트·lint-scripts
#   make verify-platform     platform 만: ruff·schema.d.ts·uv.lock 드리프트, DAEGAM_TEST_MONGO_URL 이 있으면 pytest 도
#   make verify-runtime      agent-runtime·mcp-runtime·mock-oidc·web-tools·sandbox 의 ruff·pytest. agent-runtime 의 실제 저장소
#                            시험은 DAEGAM_TEST_PG_DSN·DAEGAM_TEST_REDIS_URL 이 있을 때만 돈다
#   make run                 platform 서버 (uvicorn, 포트 9000)
#   make e2e-instance        P3 F8 인스턴스 e2e (실제 클러스터, deploy-local·lab-up 이후. STEP 3·5·6 은 한글 display_name 과
#                            CR 이름 mcp-<name>. D24. 이름 규칙의 422 는 API 테스트가 본다)
#   make e2e-gateway         P8 F2·F7 + P9 F5 Gateway e2e (실제 클러스터, deploy-local·lab-up 이후. STEP 3 은 인스턴스 수동 게시
#                            POST /mcp-servers/{id}/publish, STEP 4 는 참조 중 해제 409 in_use, STEP 12~17 은 Traefik 노출:
#                            http Gateway, TLS Secret 뒤 MCP Server 앞단 프록시, 422 namespace_not_allowed, 삭제 정리. cert-manager 는
#                            ClusterIssuer 없으면 SKIP. STEP 16b 는 메트릭이 거친 곳마다 한 번 세고 노출 앞단을 세지 않는지
#                            롤업을 기다려 본다(몇 분 걸린다). STEP 9i 는 허브 이미지의 허용이고 scripts/hub_e2e_registry.sh 가
#                            띄운 레지스트리(E2E_HUB_DIR)가 있어야 돌고 없으면 SKIP. 422 cycle 과 409 host_conflict 는 API 테스트가 본다)
#   make e2e-agent           P12 Agent e2e (실제 클러스터, deploy-local·lab-up 이후. stub provider 등록(ConfigMap
#                            DAEGAM_ALLOW_STUB_PROVIDER=true 전제)에서 시작해 calculator MCP Server, lab Postgres Database,
#                            템플릿 그래프 검증·저장, Deploy Ready, 채팅 SSE Add(3,4)=7, HITL Divide 승인, 세션·메트릭·이벤트,
#                            보류 중 redeploy 경고와 새 파드에서 재개, 삭제 정리로 이어진다. STEP 13a 는 노출 켠 deploy 가
#                            422 unauthenticated_exposure(header) 다.
#                            E2E_LLM_PROVIDER=<등록된 provider> 면 실제 모델로 한 턴)
#   make e2e-agent-redis     P16 (D33) 같은 스크립트를 E2E_MEMORY_KIND=redis 로. lab Redis 8 을 memory 에 넣고 verify ready·바인딩 env·agent.json·redis-cli 키/인덱스 단정·삭제 뒤 정리를 본다
#   make e2e-agent-oidc      P13 (D27) 같은 스크립트를 E2E_AUTH_MODE=oidc 로. STEP 0 이 ConfigMap DAEGAM_AUTH_MODE 를 oidc 로 patch 하고
#                            플랫폼과 UI 를 굴리며(D52) 끝나면 되돌린다. 바꾼 뒤와 되돌린 뒤 재적용을 기다린다(P32). STEP 13 이 노출된 인스턴스를 Traefik 경유 데이터 플레인 토큰(mock-oidc)으로
#                            부른다: 무토큰 401, 제어면 aud 401, 채팅 7, 증분 SSE(interrupt), 타 사용자 403, /metrics 403, allowed_groups, 삭제 정리.
#                            턴 위임 토큰(P53)을 같은 patch 로 켜고, MCP Server 호출의 감사 기록이 사람과 Agent 를 함께 남기는지,
#                            oidc 의 Gateway 소스는 Agent 의 client 로 부르는지, act 를 실은 IdP 토큰이 전처럼 통하는지 본다
#   make e2e-assistant       P14 (D31) 플랫폼 안의 청지기 e2e (실제 클러스터, header 모드, deploy-local·lab-up 이후. stub provider 로
#                            스텁 steps 메시지와 승인 두 번으로 petstore 백엔드 → MCP Server Ready → Agent Ready, 비밀 거부
#                            secret_in_chat·navigate, 감사 via=assistant/<sid> 와 X-Daegam-Via 위조 불가, 세션 삭제 뒤 감사 행 유지. KEEP=1 유지)
#   make e2e-knowledge       P15 (D32) Knowledge Base e2e 10단계 (실제 클러스터, deploy-local·lab-up 이후. lab Postgres 는
#                            pgvector/pgvector:pg16 이어야 한다. 문서 하나를 올려 인덱싱하고, knowledge 노드를 단 에이전트의
#                            검색 도구를 확인한 뒤, 문서·Knowledge Base 를 지워 정리한다)
#   make e2e-web-tools       P39b (D64) 플랫폼 웹 Tool e2e 11단계 (실제 클러스터, deploy-local·lab-up 이후. 웹 Tool 을 켜고 fetch
#                            복제본·검색 엔드포인트·vMCP, 접근 경계, 주소 거절과 나가는 길, 토큰 audience, 웹 가드의 첨부·허용 목록,
#                            기본 승인과 읽기 전용, Tool 핀, fetch 끄고 켜기를 본다. DAEGAM_CA_FILE 을 주면 network.ca_bundle 로 넣는다.
#                            E2E_TARGET=install 은 메아리 서버를 모델로, petstore 의 addPet 을 계산기 대신 쓴다)
#   make e2e-skills          P39a (D64) Skill e2e 6단계 (실제 클러스터, deploy-local·lab-up 이후. Skill 을 만들고 zip 으로 리비전을
#                            더하고, Skill 을 단 에이전트의 /skills/ 읽기 전용, 핀한 리비전, 다른 테넌트에 연 Skill, 큰 Skill 묶음을 본다.
#                            E2E_TARGET=install 은 메아리 서버를 모델로, 플랫폼 네임스페이스의 Postgres 를 memory 로 쓴다)
#   make e2e-chat            P36 (D61) 채팅 경험 e2e 의 A·B 단계 (실제 클러스터, deploy-local·lab-up 이후. stub provider 로 장기 기억·
#                            knowledge 노드·서브에이전트를 단 에이전트를 띄우고 turn_id·제목·이름 바꾸기·검색, 다시 생성과 메시지 수정
#                            (서브에이전트를 부른 턴 포함), 인용 이벤트와 원문 다운로드 헤더·가림 규칙의 403, 피드백·내보내기·감사,
#                            기억 파일 보기·하나 삭제·모두 삭제를 본다. B 단계는 docx·pdf·xlsx·hwpx·txt·png 업로드와 첨부 턴, 세션
#                            파일 목록과 다운로드 헤더, 상한 오류, daegam-docparse 파드의 격리와 selfcheck 다. 끝에 세션 삭제 뒤
#                            저장소까지 본다. E2E_LLM_PROVIDER=<등록된 provider> 면 그 모델로 돌고 스텁이 메시지의 JSON 조각으로
#                            도구를 고르는 데 기대는 단계는 조건부 SKIP 이다. E2E_TARGET=install 이면 설치본에서 모델 없이 되는 것만
#                            본다. https Ingress 를 지나는 10MiB 업로드와 추출, 피드백 내보내기 헤더, daegam-docparse 파드 점검이고
#                            관리자는 e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이다)
#   make e2e-chat-redis      같은 스크립트를 E2E_MEMORY_KIND=redis 로. memory 가 lab Redis 8 이고 다시 생성을 여러 번 한 긴 세션을
#                            지운 뒤 lab Redis 에 그 세션의 키가 하나도 없는지 본다
#   make e2e-telemetry       P37 (D62) 관측과 비용 e2e (실제 클러스터, deploy-local·lab-up 이후. OpenAI 호환 메아리
#                            deploy/dev/lab/llm_echo.py 로 이벤트 밀어 보내기·상관 헤더·원장·지표·트레이스·게이트웨이 사용량 수집·
#                            OTLP 를 본다. E2E_TARGET=install 은 설치 값 파일의 컨텍스트와 공개 호스트이고 Tool 이 드는 단계는 건너뛴다)
#   make e2e-llm-gateway     P38 (D63) LLM 게이트웨이 e2e 11단계 (실제 클러스터, deploy-local·lab-up 이후. 메아리 provider
#                            둘로 경로 옮기기·키 반영·한도·회수·게이트웨이 없음·플랫폼 호출자·끄기를 본다. 게이트웨이를 켰다가
#                            되돌린다. E2E_TARGET=install 은 게이트웨이를 켠 설치(차트의 llmGateway.mode agentgateway) 뒤다)
#   make e2e-mcp-auth        P6 F4a/F4b + P8 F2·F7 vMCP OIDC 게이트 (실제 클러스터, deploy-local·lab-up·mock-oidc 이후. STEP 1 이 oidc 로
#                            바꾸고 재적용을 기다리며 끝나면 되돌린다(P32). STEP 2 는 수동 게시.
#                            STEP 7~8 은 노출된 MCP Server 의 external_url 무토큰 401·토큰 7, published 백엔드 422)
#   make e2e-oidc-platform   P53b (D104) oidc 모드의 데이터 플레인 발급자를 플랫폼으로 바꾼 게이트 (deploy-local·lab-up·mock-oidc
#                            이후. STEP 0 이 oidc 와 DAEGAM_DATA_PLANE_ISSUER=platform 으로 바꾸고 끝나면 되돌린다. 개인 토큰·
#                            로그인 연결·서비스 어카운트·규칙·Agent 의 사람 이름·긴 한도·되돌리기)
#   make e2e-pat             P32 (D58) 개인 MCP 토큰 e2e 9단계 (deploy-local·lab-up 이후. STEP 0 이 local 모드로 바꾸고 끝나면 되돌리며
#                            바꾼 뒤와 되돌린 뒤 재적용을 기다린다. 발급·틀린 비밀번호·교차 테넌트 거절, 노출 Gateway·MCP Server 의
#                            PRM·허용·거부, 회수·소속 해제·계정 막기·삭제의 끊김, 내부 홉, 오퍼레이터를 내린 느린 배포 도중의
#                            삭제(STEP 9, P66). E2E_PAT_KEEP_MODE=1 이면 local 모드와 대상을 남기고 되돌리는 명령을 찍는다.
#                            E2E_TARGET=install 은 노출해 둔 Gateway 를 쓰고, 그 파드를 굴리지 않고(설치 값 파일의
#                            E2E_POD_RESTART=true 면 굴린다) 회수에서 401 까지 잰다)
#   make e2e-oauth           P33 (D59) MCP 클라이언트 OAuth e2e 16단계 (deploy-local·lab-up 이후. STEP 0 이 local 모드와
#                            DAEGAM_OAUTH_ALLOW_HTTP=true 를 넣고 끝나면 되돌린다. 스크립트 클라이언트 deploy/dev/lab/mcp_oauth_client.py
#                            가 401·PRM·메타데이터, DCR, PKCE 인가와 동의, 코드 교환, 갱신 회전·재사용, 회수를 돌고 노출 Gateway 를
#                            부른다. 소속 아님·계정 막기의 끊김, 개인 토큰과 내부 홉(e2e-install-extra 의 같은 실행이 같은 Gateway 에
#                            e2e-pat 을 먼저 돌았으면 건너뛴다). E2E_TARGET=install 은 노출해 둔 Gateway 를 읽기만 하고 CIMD 도 본다. E2E_OAUTH_WAIT=full
#                            이면 끊은 연결의 접근 토큰이 401 이 되기까지 잰다)
#   make e2e-agent-calls     P46 Agent 호출 e2e 11단계 (로컬 클러스터는 header 모드 그대로, E2E_TARGET=install 은 설치본.
#                            스텁 provider 허용을 이 게이트 동안만 ConfigMap 에 두고 되돌린다. Agent 넷을 게시해 서로 Tool 로
#                            붙이고, 호출이 호출받은 Agent 의 실행으로 돌아 답을 돌려주는 것, Gateway 감사의 사람(설치본),
#                            순환과 넷째 Agent 의 거절(설치본), viewer 의 호출(설치본), 부른 턴의 중지가 실행을 취소하는 것,
#                            호출받은 Agent 의 승인, 실행 목록의 부른 Agent 와 토큰, 게시 해제, 호출을 끈 플랫폼을 본다)
#   make e2e-runs            P41 (D94) 자동 실행 e2e 10단계 (설치본만, E2E_INSTALL_USER 는 e2e-install 과 같은 관리자·키체인 항목.
#                            스텁 provider 허용을 이 게이트 동안만 ConfigMap 에 두고 되돌린다. 지금 실행, MongoDB 에서 당긴 일정의
#                            울림 하나, 서명한 웹훅과 그 거절, 둘째 developer 의 받은 결정함 승인, TTL 120초의 마감 거절과 그 뒤
#                            409, runs-as-me, 테넌트에서 빠지거나 viewer 로 낮춘 사람의 일정 멈춤, 비밀이 로그·감사·문서에 없음)
#   make e2e-sandbox         P40 (D95) 코드 실행 샌드박스 e2e 15단계 (실제 클러스터, deploy-local·lab-up 이후, 노드에 이미지
#                            daegam-sandbox:0.1.0. 격리 점검과 허용, 첨부 xlsx 를 읽고 PNG 를 쓰는 execute 와 그 내려받기, 실행 파일
#                            거절, 사용자 코드의 막힌 네트워크와 8080, 멈춤과 시한, 메모리 초과, 회수와 유휴 회수, 정책 거절, 읽을
#                            때의 무효와 개발용 우회, execute 승인, 명령 도중 배포 내리기. 게이트 동안 ConfigMap 을 고치고 플랫폼을
#                            굴린다. 로컬은 플랫폼과 daegam-docparse 를 한 벌씩으로 줄인다. E2E_TARGET=install 은 설치 값 파일의
#                            E2E_SANDBOX_CAPACITY 를 클러스터 상한으로 두고 관리자는 e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이다)
#   make e2e-provisioning    P44 (D103) 쓰지 않는 Agent 의 0 으로 줄이기와 P44b (D107) 미리 보기 e2e 18단계 (실제 클러스터, deploy-local·lab-up 이후.
#                            STEP 0 이 local 모드와 쉬는 창 120초, hold 60초를 ConfigMap 에 두고 끝나면 되돌린다. 창 뒤의 잠들기,
#                            읽기가 깨우지 않음, 503 agent_waking 과 다시 보내기, 깬 뒤 TTL 이 지난 세션의 410, 잠들기와 겹친 채팅,
#                            0 인 Service, 잠들 수 없는 셋, 덮어쓰기가 깨움, 깨우기 시간(E2E_PROVISIONING_WAKES 번, 기본 10, p50·p95),
#                            매일 깨우기, 플랫폼 스위치 끄기. 미리 보기의 시작과 대화, id 의 404, 원본을 이름 댄 Tool 규칙과 결정,
#                            다른 사람의 끝내기, Gateway 토큰, 쓰지 않은 끝. E2E_PROVISIONING_TIMING=only 는 준비와 시간 재기만 돈다)
#   make e2e-exchange        P57 (D110) 게시된 Gateway 를 토큰 인증의 백엔드로 쓰는 e2e 10단계 (실제 클러스터, deploy-local·lab-up
#                            이후. STEP 0 이 local 모드로 바꾸고 끝나면 되돌린다. 묶인 토큰과 소비자 집합, 게시된 Gateway 의 규칙이
#                            소비하는 Gateway 와 사슬에서 서는 것, 새 규칙이 한 층씩 따라옴, 테넌트 울타리, 소유자 forbid 의 닫힘과
#                            422, 스크립트 Tool 의 422, 떠난 소비자와 게시를 거둔 뒤의 토큰 사본 거절)
#   make e2e-evaluation      P45 (D109) 평가와 되돌리기 e2e 7단계 (실제 클러스터, deploy-local·lab-up 이후, header 모드, 스텁 모델.
#                            되돌리기가 옛 내용의 새 리비전과 기록 줄을 만들고 배포된 내용으로는 409, 쓰기 Tool 의 가상 결과와 거부,
#                            백엔드가 받지 않는 쓰기와 실제로 도는 읽기, 확인 항목의 통과와 실패, 게이트의 거절·통과·레플리카만
#                            바꾼 재배포·까닭을 남긴 건너뛰기, 동의 없는 복사의 거절과 바꿔 둔 전화번호, 좋아요를 거두면 사례와 결과가 지워짐)
#   make e2e-netpol          P6 F3a + P8 F7 NetworkPolicy 집행 검증 (kind + Calico 임시 클러스터, KEEP=1 유지.
#                            CHECK 7~11 은 daegam-expose-<k8s.name> 이 컨트롤러 네임스페이스에서 앞단 파드만 연다.
#                            CHECK 12·13 은 차트가 렌더한 NetworkPolicy daegam-ui 가 UI 파드의 나가는 연결만 막는다, D52.
#                            CHECK 14~18 은 daegam-web-fetch 가 fetch 파드의 사설 주소만 막고 DNS·공인 443 은 둔다, P39b.
#                            CHECK 19~21 은 MCP 백엔드 파드에 프록시 라벨 파드만 닿는다, T67. CHECK 22~24 는 네임스페이스의
#                            egress 정책이 메타데이터 주소만 막고 fetch 파드를 열지 않는다, T75)
#   make e2e-okta            실제 Okta 실연동 (DAEGAM_OKTA_E2E_* 없으면 SKIP, 비-SKIP 경로는 make db-up 선행)
#   make db-up / db-down     로컬 MongoDB 컨테이너 (daegam-mongo, mongo:7, 27017)
#   make runtime-db-up / runtime-db-down
#                            게이트 전용 Postgres·Redis 컨테이너 (daegam-pg-verify, pgvector/pgvector:pg16, 5442 와
#                            daegam-redis-verify, redis:8, 6389). verify-runtime 이 먼저 부르고 agent-runtime 테스트에 붙인다
#   make image-platform      daegam-platform:$(PLATFORM_VERSION) (P3 F2. API 만, UI 는 image-ui. 컨텍스트 = 저장소 루트,
#                            도장 = scripts/build-stamp.sh platform)
#   make image-ui            daegam-ui:$(UI_VERSION) (D52, nginx. 컨텍스트 = ui/, 도장 = scripts/build-stamp.sh ui)
#   make push-images         제품 이미지 일곱을 $(REGISTRY) 에 arm64 로 올리고 그 digest 에 서명한다 (D53, D66.
#                            태그는 판 번호와 도장, 바탕은 Dockerfile 이 digest 로 고정한다. docker login 선행, 커밋 안 된 변경이
#                            있으면 거절). PUSH_IMAGES="ui runtime" 으로 고른다. 서명 키 비밀번호는 터미널에서 묻는다
#                            (명령줄의 COSIGN_PASSWORD=... 는 거절한다). 키는 ~/.config/daegam/cosign.key(COSIGN_KEY 로
#                            바꾼다)이고, 키가 없거나 비밀번호가 틀리거나 키가 deploy/cosign.pub 과 한 쌍이 아니면 올리기
#                            전에 멈춘다. RELEASE=1.0.0-rc1 이면 그 git 태그가 있는 커밋에서만 돌고 이미지마다 그 태그도
#                            붙인다. 끝의 서명·취약점 보고는 zot 레지스트리의 search 확장을 읽으므로 다른
#                            레지스트리면 SKIP_IMAGE_REPORT=1 로 경고 한 줄과 함께 건너뛴다
#   make image-report        zot 레지스트리의 search 확장이 잰 지금 도장 이미지의 취약점과 서명 (D55, D66). 수용 목록
#                            (security/vex/ 의 그 이미지 범위의 목록)에 없는 CRITICAL 이나 스캔 결과가 없는 이미지가
#                            있으면 실패. HIGH 는 찍고 끝 줄에 센다. 서명은 deploy/cosign.pub 으로 확인해 서명
#                            확인됨·서명 확인 안 됨으로 보이기만 한다. CPython 을
#                            싣는 다섯 이미지는 묶인 라이브러리의 공개 취약점도 본다(P58, security/cpython-builds.json).
#                            push-images 가 끝에 부른다. REPORT_REFS="daegam-ui:<태그> ..." 로 옛 태그를,
#                            REPORT_TAG=<릴리스 태그> 로 그 태그의 일곱 이미지를 본다(릴리스 전 미리 보기)
#   make cpython-record      uv 핀이나 CPYTHON_VERSION 을 올린 뒤 security/cpython-builds.json 에 그 빌드의 판을
#                            적는다 (P58, D70. uvx·docker 필요, 이미지는 굽지 않는다)
#   make base-pins           제품 Dockerfile 일곱의 바탕 고정(FROM 의 digest)을 각 태그의 지금 index digest 로 옮긴다
#                            (D55. docker buildx 필요). 릴리스 전 미리 보기나 base-scan 이 바탕의 고칠 수 있는 발견을
#                            보일 때와 정식 릴리스로 올릴 후보 전에 돌리고, 바로 make base-scan 으로 본 뒤 커밋한다.
#                            make base-pins-check 는 쓰지 않고 낡은 고정을 알린다(있으면 1)
#   make deps-audit          제품 이미지의 lock(파이썬 uv.lock 여섯, ui 의 package-lock.json)에 고친 판이 나온
#                            HIGH·CRITICAL 이나 수용 목록에 없는 CRITICAL 이 있으면 실패 (D55. 고정한 판의 trivy 나 docker
#                            필요). 릴리스 전 점검이다
#   make base-scan           제품 Dockerfile 마지막 단계의 바탕을 고정한 digest 그대로 Trivy 로 보고, CPython 묶음은 지금
#                            핀으로 본다. 수용 목록에 없는 CRITICAL 이 있으면 실패 (D55). 후보마다 태그 전과 make base-pins
#                            뒤에 돌린다
#   make registry-pull-secret NS="ns..."  네임스페이스마다 pull secret $(PULL_SECRET) 을 docker login 자격 증명으로 만든다
#                            (PULL_SECRET_SAS="default daegam-platform" 이면 그 ServiceAccount 에도 붙인다)
#   make e2e-install         설치본 e2e (D54, P85 10절). E2E_INSTALL_USER 는 운영자가 만든 e2e 전용 관리자, 비밀번호는
#                            E2E_INSTALL_PASSWORD 또는 키체인 항목 daegam-e2e-install. E2E_INSTALL_REBOOT=1 이면 파드를 모두 지운 뒤
#                            돌아오는지도 본다. 값은 설치 값 파일(~/.config/daegam/install.env, 예시 deploy/install.env.example)이다.
#                            E2E_TAG=<후보 태그> 면 그 후보의 e2e-install-extra 가 e2e-runs 를 돌 때 STEP 8r 을 건너뛴다
#   make e2e-install-extra E2E_TAG=<후보 태그>  그 태그의 e2e: 줄이 고른 설치본 게이트(e2e-web-tools, e2e-skills, e2e-telemetry,
#                            e2e-ui-serving, e2e-pat, e2e-oauth, e2e-runs, e2e-agent-calls, e2e-sandbox 가운데)를 하나씩 차례로 돈다(scripts/run_e2e_install_extra.sh).
#                            E2E_INSTALL_EXTRA="이름 ..." 이면 태그 대신 그 이름들이다. 하나가 실패해도 나머지를 돌고, 실패한
#                            이름을 찍은 뒤 1 로 끝난다. 릴리스 후보의 e2e-install 다음에 돈다
#   make llm-gateway-uninstall  LLM 게이트웨이 네임스페이스를 지운다 (P38 3절). 네임스페이스와 플랫폼의 Role 은 대감 차트가
#                            llmGateway.mode agentgateway 일 때 렌더하고 끈 뒤에도 남긴다(P85 8절). 스위치가 꺼졌고 게이트웨이
#                            경로 파드(daegam.devops.ai.kr/llm-route=gateway)가 없을 때만 지운다. 플랫폼 ConfigMap 이 없으면
#                            멈춘다. 설치본은 INSTALL_CONTEXT=<컨텍스트>, 없으면 KUBE_CONTEXT 다. 설치 값 파일의 값을 기본으로
#                            읽지 않아 orbstack 에 하려던 거두기가 설치본에 닿지 않는다
#   make mirror-images       LLM 게이트웨이의 남의 이미지 넷(차트 값 llmGateway.images)을 $(REGISTRY)/third-party 에 digest
#                            그대로 옮기고 서명이 있는 둘을 cosign verify 로 확인한다 (P38 3절. MIRROR_IMAGES="gateway" 로 고른다.
#                            DRY_RUN=1 이면 옮길 것만 찍는다)
#   make hub-mirror HUB_UPSTREAM=<참조:태그> [HUB_SERVER=<카탈로그 서버>] [HUB_APPROVE=yes]
#                            upstream 이미지를 HUB_PREFIX 아래로 바이트 그대로 옮기고 허브 키 HUB_KEY 로 서명한다 (P55 6절,
#                            scripts/hub_mirror.py). 키 비밀번호는 COSIGN_PASSWORD, HUB_KEY_PASSWORD_FILE, 터미널 차례다.
#                            허브 레지스트리에는 docker login 이 있어야 한다
#   make hub-resign HUB_IMAGE=<허브 사본@digest> HUB_KEEP_KEYS="<공개 키 파일...>"
#                            키를 바꿀 때 남길 키로 검증되는 허브 서명의 내용을 HUB_KEY 로 다시 서명한다.
#                            HUB_REFUSED="<digest...>" 는 건너뛴다
#   make deploy-local        대감 차트(charts/daegam)를 deploy/dev/values-local.yaml 로 orbstack 에 helm upgrade --install 하고
#                            MongoDB·문서 파싱·플랫폼·UI 의 롤아웃을 기다린다 (P85 4절, scripts/deploy-local.sh). 이미지는
#                            dev-<image id> 태그이고 스펙이 그대로인 Deployment 는 rollout restart 한다. 둘 다 바꿨으면
#                            make image-platform image-ui deploy-local. MONGO=external 이면 함께 올린 MongoDB 없이 운영자가
#                            만든 Secret daegam-platform-secrets(DAEGAM_MONGO_URL 등)와 daegam-docparse-token 을 쓴다.
#                            호스트는 values-local.yaml 의 host 이고 LOCAL_HOST=… 로, IngressClass 는 INGRESS_CLASS=… 로 덮는다.
#                            LLM_GATEWAY=agentgateway 면 LLM 게이트웨이를 켜고(차트가 그 네임스페이스와 Role 을 렌더한다)
#                            LLM_GATEWAY_IMAGES_FROM=mirror 면 설치 값의 레지스트리(REGISTRY) 사본 이미지다 (P38). 차트 이전에 올린 클러스터는 make undeploy-local 이 먼저다(HELM_FLAGS=--take-ownership
#                            이면 그 객체를 넘겨받는다)
#   make deploy-ui-local     UI 만 올린다 (D52). UI 이미지 태그만 바꿔 helm upgrade --reset-then-reuse-values 하고 daegam-ui 만
#                            굴린다. 차트와 값이 지난 deploy-local 과 같을 때만 플랫폼을 건드리지 않는다. UI 만 바꿨으면
#                            make image-ui deploy-ui-local 이다
#   make undeploy-local      릴리스와 daegam-system 삭제. MongoDB 의 PVC 와 차트가 만든 Secret 도 지운다 (플랫폼 데이터 유지 안 함).
#                            테넌트 네임스페이스, LLM 게이트웨이 네임스페이스와 그 Role·RoleBinding, dev-<id> 태그는 남는다
#   make llm-provider-local  셸의 OPENAI_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY / ZAI_API_KEY 를
#                            $(PLATFORM_NS) 의 Secret daegam-llm-<출처>(키 api_key)로 넣고 배포본($(LLM_API))에
#                            provider local-<출처> 를 등록 (D25. OpenRouter 는 OPENROUTER_MODEL, 기본 moonshotai/kimi-k3.
#                            Z.AI 는 ZAI_MODEL, 기본 glm-5.3. 넷 다 없으면 안내만 하고 0)
#   make ui-install          ui/ npm 의존성 설치 (npm ci)
#   make ui-dev              ui/ vite 개발 서버 (포트 5173, API 는 $(UI_API_PROXY) 로 프록시)
#   make ui-build            ui/ 정적 빌드 → ui/dist (daegam-ui 이미지가 담는 것과 같다. 로컬에서 화면은 make ui-dev 로 본다)
#   make ui-types            platform OpenAPI → ui/src/api/schema.d.ts (산출물은 커밋, CI 는 diff 검사)
#   make ui-test             ui/ vitest
#   make ui-e2e-install      Playwright chromium 설치 (1회, 캐시됨)
#   make e2e-ui-smoke        Playwright 스모크 (목 API 9300 + vite dev 4173, 1~2분)
#   make e2e-ui-full         Playwright full (deploy-local 클러스터, ingress http://daegam.test 경유, header 모드. 허브 게시는 상세의 Publish 버튼)
#   make e2e-ui-serving      D52 UI 컨테이너 e2e. 직결(svc/daegam-ui): 빌드 도장, nginx 정적 규칙, 보안 헤더.
#                            Traefik: 경로 분할, 화면·API CSP, UI 만 바꾼 배포가 플랫폼을 건드리지 않음, 인증 모드 전환

TOOLHIVE_VERSION ?= 0.53.0
# CRD 차트 릴리스의 네임스페이스. CRD 에 찍힌 release-namespace 는 upgrade 로 바뀌지 않으므로 처음 설치한 곳과 같아야 한다
TOOLHIVE_CRDS_NS ?= default
CERT_MANAGER_VERSION ?= v1.21.2
S2O_VERSION      ?= 0.5.0
RUNTIME_VERSION  ?= 0.1.0
RUNTIME_DIR      ?= mcp-runtime
AGENT_VERSION    ?= 0.1.0
AGENT_DIR        ?= agent-runtime
MOCK_OIDC_VERSION ?= 0.1.0
MOCK_OIDC_DIR    ?= deploy/dev/mock-oidc
WEB_TOOLS_VERSION ?= 0.1.0
WEB_TOOLS_DIR    ?= web-tools
SANDBOX_VERSION  ?= 0.1.0
SANDBOX_DIR      ?= sandbox
KEEP             ?= 0
PLATFORM_VERSION ?= 0.1.0
UI_VERSION       ?= 0.1.0
# 원격 클러스터가 당기는 레지스트리 (D53). 로컬 orbstack 은 로컬 이미지를 그대로 쓰므로 쓰지 않는다
# 비우면 이미지 스크립트가 설치 값 파일(deploy/install.env.example 을 채운 것)의 REGISTRY 를 쓴다
REGISTRY         ?=
# 굽는 아키텍처. 기본은 arm64 하나다(릴리스 후보는 arm64 실행기에서 굽는다). 다른 노드는 PUSH_PLATFORMS=linux/amd64 처럼
# 고르고, 에뮬레이션으로 굽지 않으려면 그 아키텍처의 컴퓨터에서 굽는다
PUSH_PLATFORMS   ?= linux/arm64
PUSH_IMAGES      ?= platform ui runtime agent mock-oidc web-tools sandbox
PULL_SECRET      ?= registry-pull
UI_TAG           ?=
# UI 이미지의 빌드 도장 (D52). 커밋된 ui/ 트리에서 나오고 커밋 안 된 변경이 있으면 -dirty. e2e 가 덮어써 도장만
# 다른 이미지를 굽는다
UI_STAMP         ?= $(shell sh scripts/build-stamp.sh ui 2>/dev/null)
# 플랫폼 이미지의 빌드 도장 (D52). platform/ 과 agent-runtime/schema/ 기준이라 UI 만 바꾼 커밋은 이 값을 바꾸지 않는다
PLATFORM_STAMP   ?= $(shell sh scripts/build-stamp.sh platform 2>/dev/null)
PLATFORM_NS      ?= daegam-system
PLATFORM_TAG     ?=
MONGO            ?= local
# 로컬 배포의 IngressClass. 비우면 deploy/dev/values-local.yaml 의 traefik 이다
INGRESS_CLASS    ?=
# make deploy-local 의 helm upgrade 에 더하는 플래그. 차트 이전에 kubectl apply 로 올린 객체를 한 번 넘겨받을 때
# --take-ownership 이다
HELM_FLAGS       ?=
SPEC2OPENAPI_DIR ?= ../spec2openapi
KUBE_CONTEXT     ?= orbstack
# lab 네임스페이스. 이름은 P1 스파이크 때 그대로다 (e2e 스크립트가 같은 SPIKE_NS env 를 받는다).
# lab 매니페스트와 profile.json 은 daegam-spike 에 고정돼 있어, 이 값은 e2e 가 이미 떠 있는 lab 을 가리킬 때만 쓴다
SPIKE_NS         ?= daegam-spike
# e2e 스크립트가 공통으로 받는 대상. 레시피마다 같은 세 줄을 적지 않는다
E2E_ENV          = KUBE_CONTEXT=$(KUBE_CONTEXT) PLATFORM_NS=$(PLATFORM_NS) SPIKE_NS=$(SPIKE_NS)
LAB_DIR          ?= deploy/dev/lab
PLATFORM_DIR     ?= platform
PLATFORM_PORT    ?= 9000
MONGO_CONTAINER  ?= daegam-mongo
MONGO_IMAGE      ?= mongo:7
MONGO_PORT       ?= 27017
# verify-runtime 이 agent-runtime 테스트에 붙이는 게이트 전용 저장소. 개발에 쓰는 컨테이너와 이름·포트가 달라 판정용
# 실행이 개발 중인 상태와 섞이지 않는다. 포트는 기본 포트에 10 을 더한 값이다(판정용 Mongo 27027 과 같은 규칙)
TEST_PG_CONTAINER    ?= daegam-pg-verify
TEST_PG_IMAGE        ?= pgvector/pgvector:pg16
TEST_PG_PORT         ?= 5442
TEST_PG_PASSWORD     ?= pw
TEST_REDIS_CONTAINER ?= daegam-redis-verify
TEST_REDIS_IMAGE     ?= redis:8
TEST_REDIS_PORT      ?= 6389
# 컨테이너가 연결을 받을 준비가 되면 0 으로 끝나는 명령이다(container_up 의 여섯째 인자). Postgres 는 TCP(-h 127.0.0.1)로
# 묻는다. 첫 기동 때 초기화 스크립트가 띄우는 임시 서버는 유닉스 소켓으로만 연결을 받다가 내려가므로 유닉스 소켓으로
# 물으면 너무 일찍 준비됐다고 본다
MONGO_READY      = docker exec $(MONGO_CONTAINER) mongosh --quiet --eval 'db.runCommand({ping:1}).ok' | grep -q 1
TEST_PG_READY    = docker exec $(TEST_PG_CONTAINER) pg_isready -q -h 127.0.0.1 -U postgres
TEST_REDIS_READY = docker exec $(TEST_REDIS_CONTAINER) redis-cli ping | grep -q PONG
# DB 가 있어야 도는 시험 갈래. 시험용 DB 주소가 셸에 있을 때만 이 PC 에서 돌고(local), 없으면 DB 컨테이너를
# 띄우지 않고 건너뛴 것을 찍는다(ci). 그 갈래는 PR 의 pr-check 가 platform.yml·agent-runtime.yml 로 GitHub 의 서비스
# 컨테이너에서 돌린다. 주소는 DAEGAM_TEST_MONGO_URL(platform), DAEGAM_TEST_PG_DSN 과 DAEGAM_TEST_REDIS_URL(agent-runtime,
# tests/conftest.py 의 PG_DSN·REDIS_URL)이다. 로컬 컨테이너에 붙일 때는 게이트 전용 Mongo(docs/guide/local-dev.md 7절)의
# DAEGAM_TEST_MONGO_URL=mongodb://127.0.0.1:27027,
# DAEGAM_TEST_PG_DSN='host=127.0.0.1 port=$(TEST_PG_PORT) dbname=postgres user=postgres password=$(TEST_PG_PASSWORD)',
# DAEGAM_TEST_REDIS_URL=redis://127.0.0.1:$(TEST_REDIS_PORT)/0 이다. 주소가 있으면 DAEGAM_TEST_REQUIRE_STORES=1 을 함께 넘겨,
# 주소가 틀렸을 때 pytest 가 실제 저장소 시험을 건너뛰지 않고 시작부터 멈추게 한다
PLATFORM_STORES := $(if $(DAEGAM_TEST_MONGO_URL),local,ci)
RUNTIME_STORES  := $(if $(and $(DAEGAM_TEST_PG_DSN),$(DAEGAM_TEST_REDIS_URL)),local,ci)
RUNTIME_TEST_ENV = $(if $(filter local,$(RUNTIME_STORES)),DAEGAM_TEST_REQUIRE_STORES=1)
# 링크 갈래(verify-ui-links·verify-runtime-links·verify-docs)가 고른 platform 시험 가운데 Mongo 를 쓰는 것은 ci 일 때
# 건너뛰고 그 수를 pytest 가 찍는다(tests/conftest.py _worker_db)
PLATFORM_LINK_ENV = $(if $(filter ci,$(PLATFORM_STORES)),DAEGAM_TEST_SKIP_MONGO=1)
DAEGAM_CA_FILE   ?=
# LLM 게이트웨이 (P38 3절·12절). 네임스페이스는 켜든 끄든 테넌트가 쓰지 못한다. PDB 는 노드가 하나인 클러스터에서 끈다(D54).
# LLM_GATEWAY 는 비우면 로컬 배포는 off 이고, 설치 값 파일을 읽는 배포는 그 파일의 값이다. LLM_GATEWAY_IMAGES_FROM 은 로컬
# 배포만 읽는다(비우면 원본 이미지). 게이트웨이의 판은 차트 값(charts/daegam/values.yaml)의 llmGateway.images.gateway 태그와
# 같아야 한다
LLM_GATEWAY_NS   ?= daegam-llm
LLM_GATEWAY_PDB  ?= false
AGENTGATEWAY_VERSION ?= v1.5.0
LLM_GATEWAY      ?=
LLM_GATEWAY_IMAGES_FROM ?=
MIRROR_IMAGES    ?=
UI_DIR           ?= ui
UI_API_PROXY     ?= http://127.0.0.1:$(PLATFORM_PORT)
# 로컬 배포의 호스트 (D58). 비우면 deploy/dev/values-local.yaml 의 host 이고, 그 글자를 scripts/lib/hosts.sh 의 local_host
# 하나가 읽는다. 환경의 DAEGAM_HOST(설치 값)는 보지 않으므로 로컬 배포가 설치본의 호스트를 가져가지 않는다
LOCAL_HOST       ?=
# Playwright full 과 llm-provider-local 이 쓰는 배포본 주소. ingress 로 연 host 라 /etc/hosts 매핑이 필요하다
UI_FULL_HOST     ?= $(or $(LOCAL_HOST),$(shell bash -c '. scripts/lib/hosts.sh && local_host'))
UI_FULL_BASE     ?= http://$(UI_FULL_HOST)

# 사내 방화벽 CA (선택). DAEGAM_CA_FILE 이 있으면 이미지 굽기에 빌드 비밀 daegam_ca 로 넘긴다. Dockerfile 의 받기 단계만
# 그 CA 를 믿고 이미지 층에는 남지 않는다 (docs/guide/images.md 3절)
comma := ,
CA_BUILD_SECRET = $(if $(DAEGAM_CA_FILE),--secret "id=daegam_ca$(comma)src=$(DAEGAM_CA_FILE)")
# 레지스트리 순단 대응: docker build 가 실패하면 5초 뒤 한 번만 재시도
define build_retry
docker build $(CA_BUILD_SECRET) $(1) || { echo ">> docker build failed; retrying once in 5s"; sleep 5; docker build $(CA_BUILD_SECRET) $(1); }
endef

KUBECTL := kubectl --context $(KUBE_CONTEXT)
# 로컬 차트 배포(scripts/deploy-local.sh)가 받는 값. 비운 값은 그 스크립트와 deploy/dev/values-local.yaml 의 기본이다
DEPLOY_LOCAL = KUBE_CONTEXT=$(KUBE_CONTEXT) PLATFORM_NS=$(PLATFORM_NS) PLATFORM_VERSION=$(PLATFORM_VERSION) \
	UI_VERSION=$(UI_VERSION) PLATFORM_TAG=$(PLATFORM_TAG) UI_TAG=$(UI_TAG) MONGO=$(MONGO) LOCAL_HOST=$(LOCAL_HOST) \
	INGRESS_CLASS=$(INGRESS_CLASS) LLM_GATEWAY=$(LLM_GATEWAY) LLM_GATEWAY_IMAGES_FROM=$(LLM_GATEWAY_IMAGES_FROM) \
	LLM_GATEWAY_NS=$(LLM_GATEWAY_NS) LLM_GATEWAY_PDB=$(LLM_GATEWAY_PDB) PULL_SECRET=$(PULL_SECRET) \
	HELM_FLAGS="$(HELM_FLAGS)" bash scripts/deploy-local.sh
HELM    := helm --kube-context $(KUBE_CONTEXT)

.PHONY: cpython-record base-pins base-pins-check deps-audit base-scan cluster-up operator-install cert-manager-up cert-manager-clean image image-s2o image-runtime image-agent image-mock-oidc image-web-tools image-sandbox image-lab lab-up lab-clean test test-gate lint lint-scripts verify verify-changed verify-ui verify-ui-links verify-runtime-links verify-docs verify-platform verify-runtime check-types check-locks run db-up db-down runtime-db-up runtime-db-down image-platform deploy-local undeploy-local llm-provider-local e2e-instance e2e-gateway e2e-agent e2e-agent-oidc e2e-agent-redis e2e-assistant e2e-assistant-oidc e2e-knowledge e2e-chat e2e-chat-redis e2e-telemetry e2e-netpol e2e-okta ui-install ui-dev ui-build ui-types ui-test ui-e2e-install e2e-ui-smoke e2e-ui-full e2e-mcp-auth e2e-oidc-platform image-ui deploy-ui-local e2e-ui-serving push-images image-report registry-pull-secret e2e-install e2e-install-extra web-ca-local e2e-agent-local e2e-pat e2e-oauth e2e-web-tools e2e-skills e2e-runs e2e-agent-calls e2e-sandbox e2e-provisioning e2e-exchange e2e-evaluation e2e-llm-gateway llm-gateway-uninstall mirror-images hub-mirror hub-resign

cluster-up:
	@if [ "$$(orb config get k8s.enable)" != "true" ]; then \
		echo ">> enabling OrbStack kubernetes"; \
		orb config set k8s.enable true; orb stop; orb start; \
	fi
	@echo ">> waiting for node Ready"
	@for i in $$(seq 1 36); do \
		if $(KUBECTL) get nodes 2>/dev/null | grep -q ' Ready'; then $(KUBECTL) get nodes; exit 0; fi; \
		sleep 5; \
	done; echo "node not Ready"; exit 1

operator-install:
	KUBE_CONTEXT=$(KUBE_CONTEXT) TOOLHIVE_VERSION=$(TOOLHIVE_VERSION) TOOLHIVE_CRDS_NS=$(TOOLHIVE_CRDS_NS) \
		DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) bash scripts/operator-install.sh

# cert-manager 와 자체 서명 ClusterIssuer (T5). 노출의 tls.mode cert-manager 를 로컬에서 실측하려고 둔다.
# selfSigned 라 바깥 CA 도 도메인도 필요 없다. 플랫폼은 ClusterIssuer 이름만 읽으므로(cluster_issuer_names)
# 이것이 올라가면 e2e-gateway STEP 16 이 SKIP 에서 실제 경로로 바뀐다
cert-manager-up:
	$(HELM) upgrade -i cert-manager $(if $(DAEGAM_CA_FILE),--ca-file $(DAEGAM_CA_FILE)) \
		oci://quay.io/jetstack/charts/cert-manager --version $(CERT_MANAGER_VERSION) \
		-n cert-manager --create-namespace --set crds.enabled=true
	@for d in cert-manager cert-manager-webhook cert-manager-cainjector; do \
		$(KUBECTL) -n cert-manager rollout status deploy/$$d --timeout=180s; \
	done
	@# 웹훅이 받을 준비가 되기 전에 넣으면 연결 거부로 죽는다. 붙을 때까지 다시 건다
	@for i in $$(seq 1 24); do \
		$(KUBECTL) apply -f deploy/dev/cert-manager-issuer.yaml && break || sleep 5; \
	done
	$(KUBECTL) get clusterissuer

cert-manager-clean:
	-$(KUBECTL) delete -f deploy/dev/cert-manager-issuer.yaml --ignore-not-found
	-$(HELM) uninstall cert-manager -n cert-manager

image: image-s2o image-runtime image-agent image-web-tools image-lab

image-s2o:
	docker build -t spec2openapi:$(S2O_VERSION) $(SPEC2OPENAPI_DIR)

image-runtime:
	$(call build_retry,-t daegam-mcp-runtime:$(RUNTIME_VERSION) $(RUNTIME_DIR))

# Agent 파드 이미지 (D26). 컨텍스트는 agent-runtime/ 이고 설정(agent.json)은 initContainer 가 넣는다
image-agent:
	$(call build_retry,-t daegam-agent-runtime:$(AGENT_VERSION) $(AGENT_DIR))

image-mock-oidc:
	$(call build_retry,-t daegam-mock-oidc:$(MOCK_OIDC_VERSION) $(MOCK_OIDC_DIR))

# 플랫폼 web_fetch 의 복제본 이미지 (P39b 10절). 테넌트 네임스페이스에서 비밀 없이 돈다
image-web-tools:
	$(call build_retry,-t daegam-web-tools:$(WEB_TOOLS_VERSION) $(WEB_TOOLS_DIR))

# 코드 실행 작업 공간 이미지 (P40 10절). 데이터 작업 묶음을 실어 크므로 make image 에 넣지 않는다
image-sandbox:
	$(call build_retry,-t daegam-sandbox:$(SANDBOX_VERSION) $(SANDBOX_DIR))

# --- lab: e2e 가 상시로 쓰는 로컬 실험용 백엔드 (deploy/dev/lab) ------------------------

image-lab:
	docker build -t daegam-stub-spec-server:dev $(LAB_DIR)/images/stub-spec-server
	docker build --build-arg S2O_VERSION=$(S2O_VERSION) \
		--build-context s2o=$(SPEC2OPENAPI_DIR) \
		-t daegam-mock-soap:dev $(LAB_DIR)/images/mock-soap
	docker build -t daegam-mock-petstore:dev $(LAB_DIR)/images/mock-petstore
	docker build -t daegam-header-echo:dev $(LAB_DIR)/images/header-echo
	docker build -t daegam-env-echo:dev $(LAB_DIR)/images/env-echo

lab-up:
	@[ "$(SPIKE_NS)" = daegam-spike ] || { echo "lab 매니페스트는 daegam-spike 에 고정돼 있다. SPIKE_NS=$(SPIKE_NS) 는 이미 떠 있는 lab 을 e2e 가 가리킬 때만 쓴다"; exit 2; }
	$(KUBECTL) apply -f $(LAB_DIR)/manifests/00-namespace.yaml
	$(KUBECTL) -n $(SPIKE_NS) create configmap calculator-spec \
		--from-file=openapi.yaml=$(SPEC2OPENAPI_DIR)/examples/calculator.openapi.yaml \
		--from-file=profile.json=$(LAB_DIR)/profile.json \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f $(LAB_DIR)/manifests/10-stub-spec-server.yaml \
		-f $(LAB_DIR)/manifests/20-mock-soap.yaml -f $(LAB_DIR)/manifests/25-mock-petstore.yaml \
		-f $(LAB_DIR)/manifests/30-secret.yaml -f $(LAB_DIR)/manifests/50-redis.yaml \
		-f $(LAB_DIR)/manifests/55-postgres.yaml
	@# :dev 이미지를 다시 빌드해도 태그가 같아 파드가 안 바뀌므로 재시작해서 새 이미지·ConfigMap 을 쓰게 한다
	$(KUBECTL) -n $(SPIKE_NS) rollout restart deploy/stub-spec-server deploy/mock-soap deploy/mock-petstore
	$(KUBECTL) -n $(SPIKE_NS) rollout status deploy/stub-spec-server --timeout=120s
	$(KUBECTL) -n $(SPIKE_NS) rollout status deploy/mock-soap --timeout=120s
	$(KUBECTL) -n $(SPIKE_NS) rollout status deploy/mock-petstore --timeout=120s
	$(KUBECTL) -n $(SPIKE_NS) rollout status deploy/postgres --timeout=120s
	$(KUBECTL) apply -f $(LAB_DIR)/manifests/40-mcpserver.yaml
	@# 스펙이 안 바뀌어도 파드를 다시 만들어 initContainer 가 방금 재시작한 스텁에서 spec·profile 을 새로 받게 한다 (게이트 3 로그 근거)
	-$(KUBECTL) -n $(SPIKE_NS) delete pod calculator-0 --ignore-not-found --wait=true --timeout=60s
	SPIKE_NS=$(SPIKE_NS) KUBE_CONTEXT=$(KUBE_CONTEXT) bash $(LAB_DIR)/verify.sh

lab-clean:
	@[ "$(SPIKE_NS)" = daegam-spike ] || { echo "lab 매니페스트는 daegam-spike 에 고정돼 있다. SPIKE_NS=$(SPIKE_NS) 는 이미 떠 있는 lab 을 e2e 가 가리킬 때만 쓴다"; exit 2; }
	-$(KUBECTL) delete -f $(LAB_DIR)/manifests/40-mcpserver.yaml --ignore-not-found --timeout=60s
	-$(KUBECTL) delete namespace $(SPIKE_NS) --ignore-not-found --timeout=120s

# --- platform -------------------------------------------------------------

# 테스트 저장소 컨테이너를 띄운다. 떠 있으면 그대로 쓰고, 멈춰 있으면 다시 켜고, 없으면 만든다. 그 뒤 준비
# 명령이 성공할 때까지 1초 간격으로 60번 본다. 포트는 127.0.0.1 에만 연다. 테스트 저장소는 비밀번호가 없거나
# 약해서 모든 인터페이스에 열면 LAN 에서 붙을 수 있다. 이미 있는 컨테이너는 만들 때의 포트를 그대로 쓰므로, 그
# 포트가 127.0.0.1 밖에도 열려 있으면 경고한다. 한 번 docker rm -f 로 지우면 다음 실행이 127.0.0.1 로 다시 만든다.
#   $(1) 컨테이너 이름  $(2) 이미지  $(3) 호스트 포트  $(4) 컨테이너 포트  $(5) docker run 에 더 줄 인자
#   $(6) 준비됐으면 0 으로 끝나는 명령
define container_up
@if docker ps --format '{{.Names}}' | grep -qx '$(1)'; then \
	echo ">> $(1) already running"; \
elif docker ps -a --format '{{.Names}}' | grep -qx '$(1)'; then \
	echo ">> starting $(1)"; docker start $(1) >/dev/null; \
else \
	echo ">> creating $(1) ($(2) on 127.0.0.1:$(3))"; \
	docker run -d --name $(1) -p 127.0.0.1:$(3):$(4) $(5) $(2) >/dev/null; \
fi
@docker inspect -f '{{json .HostConfig.PortBindings}}' $(1) | grep -q '"HostIp":"127.0.0.1"' || \
	echo ">> warning: $(1) publishes :$(3) beyond 127.0.0.1. Remove it once (docker rm -f $(1)) to recreate it on 127.0.0.1"
@for i in $$(seq 1 60); do \
	if { $(6); } >/dev/null 2>&1; then exit 0; fi; \
	sleep 1; \
done; echo "$(1) not ready"; exit 1
endef

db-up:
	$(call container_up,$(MONGO_CONTAINER),$(MONGO_IMAGE),$(MONGO_PORT),27017,,$(MONGO_READY))

db-down:
	-docker stop $(MONGO_CONTAINER) >/dev/null 2>&1

runtime-db-up:
	$(call container_up,$(TEST_PG_CONTAINER),$(TEST_PG_IMAGE),$(TEST_PG_PORT),5432,-e POSTGRES_PASSWORD=$(TEST_PG_PASSWORD),$(TEST_PG_READY))
	$(call container_up,$(TEST_REDIS_CONTAINER),$(TEST_REDIS_IMAGE),$(TEST_REDIS_PORT),6379,,$(TEST_REDIS_READY))

runtime-db-down:
	-docker stop $(TEST_PG_CONTAINER) $(TEST_REDIS_CONTAINER) >/dev/null 2>&1

# 워커마다 DB 하나를 쓰므로(tests/conftest.py) 병렬로 돈다. 순서대로 돌리면 약 4분, 병렬로 약 1분이다.
# PYTEST_WORKERS 로 워커 수를 줄이면 PC 부하가 줄고 그만큼 오래 걸린다 (예: PYTEST_WORKERS=4)
PYTEST_WORKERS ?= auto
# 비우면 vitest 기본값이다. 수를 주면 vitest 워커를 그만큼으로 묶는다 (예: VITEST_WORKERS=2)
VITEST_WORKERS ?=
VITEST_ARGS = $(if $(VITEST_WORKERS),--maxWorkers=$(VITEST_WORKERS))
test: db-up
	cd $(PLATFORM_DIR) && uv run pytest -q -n $(PYTEST_WORKERS)

# 게이트의 플랫폼 pytest. DAEGAM_TEST_MONGO_URL 이 없으면 컨테이너를 띄우지 않고 Mongo 가 필요 없는 시험만 돌린다
# (위 PLATFORM_STORES, 1분쯤). Mongo 를 쓰는 시험은 건너뛰고 pytest 가 그 수를 센다
test-gate:
ifeq ($(PLATFORM_STORES),local)
	cd $(PLATFORM_DIR) && uv run pytest -q -n $(PYTEST_WORKERS)
else
	cd $(PLATFORM_DIR) && $(PLATFORM_LINK_ENV) uv run pytest -q -n $(PYTEST_WORKERS)
	@echo "SKIPPED platform pytest's MongoDB tests: no DAEGAM_TEST_MONGO_URL here; the pull request's pr-check runs them (platform.yml)"
endif

lint: lint-scripts
	cd $(PLATFORM_DIR) && uv run ruff check . && uv run ruff format --check .

# 셸 스크립트 lint (P64). 추적하는 bash·sh 파일 전부(scripts/, deploy/, ui/docker/)를 warning 수준으로 본다.
# info·style 은 만드는 동안 손으로 돌리는 몫이다. shellcheck 가 없으면 건너뛰지 않고 실패한다. 건너뛰는 lint 는
# 아무것도 보지 않은 통과이기 때문이다. .claude/skills 의 zsh 스크립트는 shellcheck 가 읽지 못해 뺀다
# CI(platform 워크플로)는 러너 이미지의 shellcheck 를 쓰므로 판이 로컬과 다를 수 있다
lint-scripts:
	@command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck is not installed: brew install shellcheck (Debian/Ubuntu: apt-get install shellcheck)"; exit 1; }
	git ls-files -z '*.sh' ':!:.claude/**' | xargs -0 shellcheck -S warning -x -P SCRIPTDIR
	@echo "lint-scripts: PASS"

# 생성 타입 드리프트. 계약은 플랫폼이 정하고 UI 가 받으므로 양쪽 갈래가 함께 본다. 엔드포인트를
# 더하거나 핸들러 독스트링만 고쳐도(그게 OpenAPI 설명이다) 어긋난다.
check-types:
	$(MAKE) ui-types
	@git diff --exit-code -- $(UI_DIR)/src/api/schema.d.ts || { echo "ui/src/api/schema.d.ts is out of date: commit the regenerated file"; exit 1; }

check-locks:
	@for d in $(PLATFORM_DIR) mcp-runtime agent-runtime deploy/dev/mock-oidc web-tools sandbox; do \
		(cd $$d && uv lock --check) || { echo "$$d/uv.lock is out of date: run uv lock"; exit 1; }; \
	done

# 갈래별 게이트. 바꾼 곳만 빠르게 본다.
#
# platform pytest 가 가장 오래 걸리고 나머지 갈래는 그보다 훨씬 짧다. 갈래를 나눠 UI·문서·런타임만 고쳤을 때는 그
# 갈래만 돈다.
# ui/docker 의 셸 스크립트만 고쳐도 이 갈래만 돌므로 lint-scripts 도 여기서 돈다
verify-ui: check-types lint-scripts
	cd $(UI_DIR) && npx tsc -b && npx vitest run $(VITEST_ARGS)
	@echo "verify-ui: PASS"

verify-platform: lint test-gate check-types check-locks
	@echo "verify-platform: PASS$(if $(filter ci,$(PLATFORM_STORES)), without its MongoDB tests (the pull request's pr-check runs them))"

# agent-runtime 의 실제 저장소 시험(세션 색인·전사·카운터)은 두 주소가 있을 때만 여기서 돈다. 없으면 pytest 가 그
# 시험을 건너뛰고 나머지를 돌리며, 건너뛴 것을 찍는다(위 RUNTIME_STORES)
verify-runtime:
	cd agent-runtime && uv run ruff check . && uv run ruff format --check . && $(RUNTIME_TEST_ENV) uv run pytest -q
	@$(if $(filter ci,$(RUNTIME_STORES)),echo "SKIPPED agent-runtime store tests (Postgres, Redis): DAEGAM_TEST_PG_DSN and DAEGAM_TEST_REDIS_URL are not both set here; the pull request's pr-check runs them when agent-runtime/ changed (agent-runtime.yml)",true)
	cd mcp-runtime && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
	cd deploy/dev/mock-oidc && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
	cd web-tools && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
	@# 샌드박스 시험은 데이터 작업 묶음이 필요 없어 dev 그룹만 깐다. 리눅스 전용 시험은 macOS 에서 건너뛴다
	cd sandbox && uv run --only-dev ruff check . && uv run --only-dev ruff format --check . && uv run --only-dev pytest -q
	@echo "verify-runtime: PASS$(if $(filter ci,$(RUNTIME_STORES)), without the store tests (the pull request's pr-check runs them when agent-runtime/ changed))"

# ui/ 를 읽는 platform 테스트. UI 갈래가 이것까지 돌아야 전부와 같은 것을 본다 (약 10초).
# 캔버스 검증기는 ui/src/lib/agentGraph.ts 를 읽어 서버 모델과 맞대고, 문서 인용 검사는 docs 가
# 가리키는 ui/src·ui/e2e 의 심볼이 실재하는지 본다. CSP 대칭은 ui/docker 의 셸·nginx 설정이 플랫폼과 같은
# 헤더를 내는지 본다(D52). 호스트 글자 검사는 ui/src·ui/e2e 에 플랫폼 호스트가 새로 적히지 않았는지 본다(D58).
# UI 파일을 지우거나 줄이거나 ui/docker 만 고쳐도 여기서 걸린다.
verify-ui-links:
	cd $(PLATFORM_DIR) && $(PLATFORM_LINK_ENV) uv run pytest -q tests/test_agent_graph_parity.py tests/test_doc_citations.py tests/test_ui_csp_parity.py tests/test_host_literals.py
	@echo "verify-ui-links: PASS$(if $(PLATFORM_LINK_ENV), with any MongoDB tests skipped)"

# 런타임 파일을 읽는 platform 테스트. 바이트 같은 사본(oidc.py 등)과 agent-runtime 스키마를 맞대는 것들이라
# 런타임만 고쳐도 여기서 걸린다. 고르는 규칙은 scripts/verify_changed.sh runtime-link-tests 하나이고, PR 의 pr-check 도
# 같은 목록을 platform.yml 에 넘겨 여기서 건너뛴 Mongo 시험을 돌린다
verify-runtime-links:
	cd $(PLATFORM_DIR) && $(PLATFORM_LINK_ENV) uv run pytest -q $$(bash ../scripts/verify_changed.sh runtime-link-tests)
	@echo "verify-runtime-links: PASS$(if $(PLATFORM_LINK_ENV), with its MongoDB tests skipped (the pull request's pr-check runs them))"

# 문서만 고쳤을 때. 문서가 가리키는 코드 심볼과 링크·앵커가 실재하는지, 줄 인용이 없는지와 호스트 글자를 본다
verify-docs:
	cd $(PLATFORM_DIR) && $(PLATFORM_LINK_ENV) uv run pytest -q tests/test_doc_citations.py tests/test_host_literals.py
	@echo "verify-docs: PASS$(if $(PLATFORM_LINK_ENV), with any MongoDB tests skipped)"

# 바꾼 것을 보고 갈래를 고른다(scripts/verify_changed.sh). 기준은 묶음이 갈라져 나온 origin/develop 이 아니라
# **마지막으로 통과한 자리**다. 통과할 때 그때의 HEAD 와 커밋 안 된 파일의 내용 해시를 git 디렉터리에 적고, 다음에는 그 뒤에 바뀐
# 것만 본다. 그래서 푸시하지 않은 커밋이 쌓여 있거나 남이 둔 추적 안 되는 파일이 있어도 이미 통과한 것은 다시
# 돌지 않는다. ui/ 는 verify-ui·verify-ui-links, docs/·*.md·.claude/skills/ 는 verify-docs, 런타임은
# verify-runtime·verify-runtime-links 이고 그 밖이 하나라도 있으면 verify 전부다. **푸시 직전에도 이것을 쓴다.**
verify-changed:
	@bash scripts/verify_changed.sh

# 푸시 전 로컬 게이트: lint 와 테스트를 네 파이썬 패키지 모두, UI 타입체크·단위 테스트, schema.d.ts 드리프트, uv.lock
# 드리프트. 갈래 셋이 덮는 것의 합집합이다. check-types 는 둘이 함께 의존하지만 make 가 한 번만 돈다. DB 가 있어야
# 도는 시험은 시험용 DB 주소가 있을 때만 여기서 돌고, 없으면 PR 의 pr-check 가 돌린다(위 PLATFORM_STORES·RUNTIME_STORES).
# VERIFY_EXTRA 는 끝의 include 가 읽는 파일이 더하는 갈래다. 그 줄이 이 규칙 뒤에 있어 선행 조건이 아니라 레시피에서 펼친다
verify: verify-platform verify-runtime verify-ui
	$(if $(VERIFY_EXTRA),@$(MAKE) --no-print-directory $(VERIFY_EXTRA))
	@bash scripts/verify_changed.sh record
	@echo "verify: PASS"

# 플랫폼 API 만 뜬다(D52). 화면은 make ui-dev 로 본다(vite 가 /api·/healthz 를 여기로 넘긴다)
run:
	cd $(PLATFORM_DIR) && uv run uvicorn daegam.main:app --host 127.0.0.1 --port $(PLATFORM_PORT) --reload --no-server-header

e2e-instance:
	$(E2E_ENV) bash scripts/e2e_instance.sh

e2e-gateway:
	$(E2E_ENV) bash scripts/e2e_gateway.sh

# P12 Agent e2e (D26). stub provider 는 배포본 ConfigMap 의 DAEGAM_ALLOW_STUB_PROVIDER=true 가 있어야 등록된다
# (deploy/dev/values-local.yaml 의 platform.extraConfig, 로컬 기본값). E2E_LLM_PROVIDER 에 키가 있는 등록 provider 이름(예: make llm-provider-local
# 의 local-anthropic)을 주면 마지막에 실제 모델로 한 턴을 더 실증하고, 비우면 그 STEP 은 SKIP 이다.
E2E_LLM_PROVIDER ?=
e2e-agent:
	$(E2E_ENV) E2E_LLM_PROVIDER=$(E2E_LLM_PROVIDER) bash scripts/e2e_agent.sh

# P13 Agent 외부 노출 (D27). 같은 스크립트를 oidc 모드로 돌린다. STEP 0 이 ConfigMap daegam-platform-config 의
# DAEGAM_AUTH_MODE 를 oidc 로 patch 하고 플랫폼과 UI 를 굴리며(UI 의 CSP 도 모드를 따른다, D52), 스크립트의 EXIT trap 이
# header 로 되돌리고 다시 둘을 굴린다.
# mock-oidc 파드(deploy/dev/mock-oidc.yaml)와 Traefik(LoadBalancer Service traefik/traefik) 전제. host 는 curl --resolve 로
# Traefik IP 에 못 박으므로 /etc/hosts 는 필요 없다. 턴 위임 토큰(P53)도 같은 patch 로 켜고 함께 되돌린다. STEP 5g 는
# Gateway 소스가 쓰는 client credentials(mock-oidc 의 agent-dev)를 Tenant Settings 와 테넌트 Secret 에 두고 끝나면 되돌린다.
e2e-agent-oidc:
	$(E2E_ENV) E2E_LLM_PROVIDER=$(E2E_LLM_PROVIDER) E2E_AUTH_MODE=oidc bash scripts/e2e_agent.sh

# P20 local 인증 모드 (D40). 같은 스크립트를 local 로 돌린다. STEP 0 이 ConfigMap 의 DAEGAM_AUTH_MODE 를 local 로
# patch 하고 Secret daegam-platform-secrets 에 부트스트랩 비밀번호를 넣어 플랫폼과 UI 를 굴린다. 로그인한 뒤 서버가 강제하는
# 비밀번호 바꾸기를 거쳐 그 토큰으로 나머지를 돌리고, STEP 13 의 사용자 토큰은 POST /users 로 만든 사람들이 로그인해
# POST /auth/token 으로 받는다. EXIT trap 이 ConfigMap 과 Secret 을 원래 값으로 되돌린다.
# 턴 위임 토큰(P53)은 같은 patch 로 켜고 수명을 60초로 줄인다. Gateway 와 MCP Server 호출의 감사 기록에 사람과 Agent 가 함께
# 남는지, 토큰 수명보다 긴 턴의 갱신, 턴 도중 테넌트에서 빠진 사람의 호출 거절, 승인자의 재개, 다른 대상에 묶인 위임 토큰의
# 거절을 본다. 위임 기록은 플랫폼 MongoDB 파드 안의 mongosh 로 읽고, 이 단계들이 7~10분을 더한다
e2e-agent-local:
	$(E2E_ENV) E2E_LLM_PROVIDER=$(E2E_LLM_PROVIDER) E2E_AUTH_MODE=local bash scripts/e2e_agent.sh

# P16 memory 노드의 Redis 8 체크포인터 (D33). 같은 스크립트를 E2E_MEMORY_KIND=redis 로 돌린다. lab Redis 는 50-redis.yaml 의
# redis:8 이어야 한다(make lab-up). STEP 4 verify 가 redis.version ≥ 8 과 capabilities.checkpointer=ready 를 단정하고, STEP 8 뒤와
# STEP 12 뒤에 kubectl exec deploy/redis -- redis-cli 로 이 인스턴스의 키·인덱스를 보고 스크립트가 직접 지운다(인스턴스 삭제는 키를 안 지운다)
e2e-agent-redis:
	$(E2E_ENV) E2E_MEMORY_KIND=redis bash scripts/e2e_agent.sh

# P14 비서 e2e (D31). stub provider 전제는 e2e-agent 와 같다(ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true). 스텁 steps 로 결정적이라
# 실제 provider 는 받지 않는다(실제 provider 로 재는 일은 이 타깃 밖에서 한다). mock-petstore(lab-up)가 Backend API 의 endpoint 다. KEEP=1 이면 만든
# 리소스와 Platform Settings 변경을 남긴다
e2e-assistant:
	$(E2E_ENV) KEEP=$(KEEP) bash scripts/e2e_assistant.sh

# 같은 스크립트를 oidc 모드로 돌린다 (T9). header 모드는 주체가 하나라 viewer 403·세션 소유·사용자당 리스·교차 파드
# 결정 재개를 볼 수 없어 STEP 9~11 이 조건부 SKIP 이다. oidc 모드는 STEP 0 이 ConfigMap daegam-platform-config 의
# DAEGAM_AUTH_MODE 를 oidc 로 patch 하고 플랫폼과 UI 를 굴리며, EXIT trap 이 header 로 되돌리고 다시 둘을 굴린다.
# 그룹 매핑도 STEP 0 이 넣고 되돌린다(PUT 이 표 전체를 갈아치우므로 원본을 먼저 GET 한다).
# mock-oidc 파드(deploy/dev/mock-oidc.yaml)와 replicas 2(교차 파드 STEP 11) 전제다.
e2e-assistant-oidc:
	$(E2E_ENV) KEEP=$(KEEP) E2E_AUTH_MODE=oidc bash scripts/e2e_assistant.sh

# P15 Knowledge Base e2e (D32). stub provider 전제는 e2e-agent 와 같고 lab Postgres 는 pgvector/pgvector:pg16 이어야 한다(make lab-up).
# 문서 하나를 올려 인덱싱하고 knowledge 노드를 단 에이전트의 검색 도구를 본 뒤 문서·Knowledge Base 삭제까지 10 단계다
e2e-knowledge:
	$(E2E_ENV) bash scripts/e2e_knowledge.sh

# P39b 플랫폼 웹 Tool e2e (D64). stub provider 전제는 e2e-agent 와 같고 lab 의 mock-soap 과 Postgres 를 쓴다(make lab-up).
# DAEGAM_CA_FILE 을 주면 이번 실행 동안 network.ca_bundle 로 넣고, 끝에서 웹 Tool 설정과 항목의 접근 정책을 되돌린다.
# E2E_TARGET=install 은 설치 값 파일의 컨텍스트와 공개 호스트에서 메아리 서버를 모델로, petstore 를 계산기 대신 쓰고, 관리자는
# e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이다. e2e-telemetry·e2e-skills 와 함께 돌리지 않는다
e2e-web-tools:
	$(E2E_ENV) DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) \
	E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_web_tools.sh

# P39a Skill e2e (D64). stub provider 전제는 e2e-agent 와 같고 lab Postgres 를 쓴다(make lab-up). E2E_TARGET=install 은
# 설치 값 파일의 컨텍스트와 공개 호스트에서 메아리 서버를 모델로, 플랫폼 네임스페이스의 Postgres 를 memory 로 쓴다
e2e-skills:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_skills.sh

# P36 채팅 경험 e2e (D61) 의 A·B 단계. stub provider 전제는 e2e-agent 와 같고 lab Postgres 는 pgvector/pgvector:pg16 이어야 한다
# (make lab-up). E2E_LLM_PROVIDER 는 e2e-agent 에서와 같은 뜻이다. 등록된 provider 를 주면 그 모델로 돌고, 스텁이 도구를 고르는 데
# 기대는 단계는 건너뛰거나 느슨하게 본다. 만든 Agent·Knowledge Base·MCP Server·Database 는 끝에서 지운다. E2E_TARGET=install 은
# 설치 값 파일의 컨텍스트와 공개 호스트로 모델 없이 되는 것만 보고, 관리자는 e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이다
e2e-chat:
	$(E2E_ENV) E2E_LLM_PROVIDER=$(E2E_LLM_PROVIDER) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) \
	E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_chat.sh

# 같은 스크립트를 memory 가 lab Redis 8 인 에이전트로 돌린다(D33). 끝에서 이 Agent 의 Redis 키와 인덱스를 걷는다
e2e-chat-redis:
	$(E2E_ENV) E2E_LLM_PROVIDER=$(E2E_LLM_PROVIDER) E2E_MEMORY_KIND=redis bash scripts/e2e_chat.sh

# P37 (D62) 관측과 비용 e2e. 메아리 네임스페이스 daegam-e2e-llm 을 만들고 끝에서 그 라벨이 붙은 것만 지운다. 가격표·OTLP·
# DAEGAM_LLM_GATEWAY_URL 을 바꿨으면 되돌린다. P38 e2e 와 함께 돌리지 않는다
e2e-telemetry:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_telemetry.sh

# P38 (D63) LLM 게이트웨이 e2e. 메아리 네임스페이스 daegam-e2e-llm 을 P37 e2e 와 함께 쓰므로 둘을 함께 돌리지 않는다.
# 바꾼 스위치·한도·비서 설정과 일시 정지 표시를 EXIT 트랩이 되돌린다
e2e-llm-gateway:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) \
	LLM_GATEWAY_NS=$(LLM_GATEWAY_NS) bash scripts/e2e_llm_gateway.sh

# orbstack(Flannel) 은 NetworkPolicy 를 집행하지 않으므로 kind + Calico 임시 클러스터에서 검증한다.
# 노출 CHECK 는 platform 코드의 render_expose_policy 를 uv run 으로 렌더링해 쓴다 (platform 의존성 필요). UI 의 정책은
# 대감 차트를 helm template 으로 찍어 쓴다 (helm 필요)
e2e-netpol:
	KEEP=$(KEEP) bash scripts/e2e_netpol.sh

# DAEGAM_OKTA_E2E_ISSUER/CLIENT_ID/CLIENT_SECRET 이 없으면 SKIP(exit 0). 있으면 로컬 Mongo 필요(make db-up)
e2e-okta:
	bash scripts/e2e_okta.sh

e2e-mcp-auth:
	$(E2E_ENV) bash scripts/e2e_mcp_auth.sh

# P53b 데이터 플레인 발급자 (D104). STEP 0 이 ConfigMap 을 oidc 와 DAEGAM_DATA_PLANE_ISSUER=platform 으로 바꾸고 EXIT trap 이
# 되돌린다. mock-oidc 의 토큰과 ID 토큰(auth_time)으로 사람의 개인 토큰·로그인 연결·서비스 어카운트·규칙·Agent 의 사람 이름·
# 긴 한도·되돌리기를 본다. 전제는 e2e-mcp-auth 와 같다(mock-oidc, lab-up, Traefik)
e2e-oidc-platform:
	$(E2E_ENV) bash scripts/e2e_oidc_platform.sh

# P32 개인 MCP 토큰 (D58). orbstack(기본)은 STEP 0 이 local 모드로 바꾸고 EXIT trap 이 되돌린다. calculator MCP Server 와
# Gateway 를 Traefik 에 노출해 curl --resolve 로 부른다. E2E_TARGET=install 은 설치 값 파일의 컨텍스트에서 노출해 둔 Gateway 를
# 읽기만 하고, 관리자는 e2e-install 과 같은 E2E_INSTALL_USER·키체인 항목이다. orbstack 의 STEP 9 는 ToolHive 오퍼레이터를 잠깐 0 으로 내려 느린
# 배포 도중의 삭제를 보고(P66), 정리가 오퍼레이터를 되돌린다
E2E_TARGET ?= orbstack
E2E_PAT_KEEP_MODE ?= 0
e2e-pat:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) E2E_PAT_KEEP_MODE=$(E2E_PAT_KEEP_MODE) INSTALL_CONTEXT=$(INSTALL_CONTEXT) \
	E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_pat.sh

# P33 MCP 클라이언트 OAuth (D59). orbstack(기본)은 STEP 0 이 local 모드와 평문 허용 플래그를 넣고 EXIT trap 이 되돌린다.
# 흐름은 스크립트 클라이언트 deploy/dev/lab/mcp_oauth_client.py 가 돌고, calculator Gateway 를 Traefik 에 노출해
# --resolve 로 부른다. E2E_TARGET=install 은 e2e-pat 과 같은 컨텍스트·관리자로 노출해 둔 Gateway 를 읽기만 한다.
# E2E_OAUTH_WAIT=full 이면 끊은 연결의 접근 토큰이 Gateway 에서 401 이 되기까지 잰다(16분 안)
E2E_OAUTH_WAIT ?= refresh
e2e-oauth:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) E2E_OAUTH_WAIT=$(E2E_OAUTH_WAIT) INSTALL_CONTEXT=$(INSTALL_CONTEXT) \
	E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_oauth.sh

# P41 (D94) 자동 실행. 설치본만 대상이라 E2E_TARGET 을 넘기지 않는다(스크립트가 설치본 밖은 거절한다). 관리자는 e2e-install 과
# 같은 E2E_INSTALL_USER·키체인 항목이고, 컨텍스트와 공개 호스트는 설치 값 파일의 값이다. 릴리스 후보의 e2e: 줄이 고르면
# e2e-install-extra 가 돌린다
e2e-runs:
	PLATFORM_NS=$(PLATFORM_NS) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_runs.sh

# P46. Agent 가 게시한 다른 Agent 를 Tool 로 부른다. 로컬 클러스터(태그의 클러스터 e2e 잡의 agent 조각, deploy-local 과
# lab-up 뒤)와 설치본(E2E_TARGET=install) 둘이다. 로컬은 header 모드 그대로 돌아 사람을 보는 단계와 사슬을 조건부로 건너뛴다.
# 릴리스 후보의 e2e: 줄이 고르면 k3d 의 agent 조각과 e2e-install-extra 가 돌린다
e2e-agent-calls:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_agent_calls.sh

# P40 (D95) 코드 실행 샌드박스. 로컬 클러스터(태그의 클러스터 e2e 잡이 만든 k3d)와 설치본(E2E_TARGET=install) 둘이다. 로컬은
# deploy-local·lab-up 뒤이고 노드에 make image-sandbox 의 이미지가 있어야 한다. 릴리스 후보의 e2e: 줄이 고르면 k3d 의 sandbox
# 조각과 e2e-install-extra 가 돌린다
e2e-sandbox:
	$(E2E_ENV) E2E_TARGET=$(E2E_TARGET) INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) bash scripts/e2e_sandbox.sh

# P44 (D103) 쓰지 않는 Agent 의 0 으로 줄이기와 깨우기. 로컬 클러스터만 대상이다(태그의 클러스터 e2e 잡의 provisioning 조각).
# ConfigMap 의 모드와 쉬는 창·hold·매일 깨우기 주기·플랫폼 스위치를 EXIT trap 이 되돌린다. E2E_PROVISIONING_WAKES 는 STEP 10 의
# 깨우기 시간 재기 횟수이고(0 이면 건너뛴다) E2E_PROVISIONING_TIMING=only 는 준비와 시간 재기만 돈다(F0.2 의 k3d 읽기)
E2E_PROVISIONING_WAKES ?= 10
E2E_PROVISIONING_TIMING ?=
e2e-provisioning:
	$(E2E_ENV) E2E_PROVISIONING_WAKES=$(E2E_PROVISIONING_WAKES) E2E_PROVISIONING_TIMING=$(E2E_PROVISIONING_TIMING) \
	bash scripts/e2e_provisioning.sh

# P57 (D110) 게시된 Gateway 를 토큰 인증의 백엔드로 쓴다. 로컬 클러스터만 대상이다(태그의 클러스터 e2e 잡의 exchange 조각).
# ConfigMap 의 모드를 EXIT trap 이 되돌린다
e2e-exchange:
	$(E2E_ENV) bash scripts/e2e_exchange.sh

# P45 (D109) 평가와 되돌리기. 로컬 클러스터만 대상이다(태그의 클러스터 e2e 잡의 provisioning 조각). header 모드 그대로 돌고
# ConfigMap 을 고치지 않는다
e2e-evaluation:
	$(E2E_ENV) bash scripts/e2e_evaluation.sh

# --- platform in-cluster (P3 F2) -----------------------------------------

# 같은 태그(0.1.0, IfNotPresent)로 재빌드하면 파드가 안 바뀌므로 이미지 ID 로 dev 태그를 하나 더 붙이고
# deploy-local 이 그 태그를 쓴다. 런타임 이미지는 플랫폼이 태그를 정하므로 여기만 적용한다.
# 컨텍스트는 저장소 루트(agent-runtime/schema 도 이미지에 들어간다), 제외 목록은 루트 .dockerignore(ui/ 는 뺀다).
# GIT_SHA 는 플랫폼 도장이고 /api/version 의 api_build 가 된다(D52). UI 만 바꾼 커밋은 도장도 컨텍스트도 그대로라
# 다시 구워도 캐시로 같은 이미지가 나온다.
image-platform:
	$(call build_retry,-f $(PLATFORM_DIR)/Dockerfile --build-arg GIT_SHA=$(PLATFORM_STAMP) -t daegam-platform:$(PLATFORM_VERSION) .)
	@id=$$(docker image inspect --format '{{.Id}}' daegam-platform:$(PLATFORM_VERSION) | sed 's/^sha256://' | cut -c1-12); \
	docker tag daegam-platform:$(PLATFORM_VERSION) daegam-platform:dev-$$id; \
	echo ">> tagged daegam-platform:dev-$$id (stamp $(PLATFORM_STAMP))"

# UI 이미지 (D52). 컨텍스트는 ui/ 이고 제외 목록은 ui/.dockerignore 다. 도장(UI_STAMP)이 build.json 의 ui_build 가 된다.
# 재빌드가 롤링을 일으키도록 이미지 ID 로 dev-<id> 태그를 하나 더 붙이고 deploy-ui-local·deploy-local 이 그 태그를 쓴다.
# 빌드 마지막에 이미지의 셸로 CSP 자체 검사와 nginx -t 가 돌아 하나라도 실패하면 이미지가 만들어지지 않는다
image-ui:
	$(call build_retry,-f $(UI_DIR)/Dockerfile --build-arg GIT_SHA=$(UI_STAMP) -t daegam-ui:$(UI_VERSION) $(UI_DIR))
	@id=$$(docker image inspect --format '{{.Id}}' daegam-ui:$(UI_VERSION) | sed 's/^sha256://' | cut -c1-12); \
	docker tag daegam-ui:$(UI_VERSION) daegam-ui:dev-$$id; \
	echo ">> tagged daegam-ui:dev-$$id (stamp $(UI_STAMP))"

# 제품 이미지를 레지스트리에 올리고 서명한다 (D53, D66). 로컬 태그는 건드리지 않고 buildx 가 PUSH_PLATFORMS(arm64)로 따로 굽는다.
# 자격 증명은 docker login 이 둔 키체인을 쓰고 저장소에는 없다. 서명 키 비밀번호는 스크립트가 터미널에서 묻는다.
# 비밀번호를 make 명령줄 변수로는 받지 않는다. make 가 명령줄 변수를 MAKEFLAGS 에 실어 모든 자식 프로세스에 물려주고, 값
# 안의 `$` 를 make 변수로 풀어 바꾸기 때문이다
ifeq ($(origin COSIGN_PASSWORD),command line)
$(error COSIGN_PASSWORD 는 make 명령줄에 적지 않는다. make push-images 가 터미널에서 비밀번호를 묻는다)
endif
push-images:
	REGISTRY=$(REGISTRY) PUSH_PLATFORMS=$(PUSH_PLATFORMS) PLATFORM_VERSION=$(PLATFORM_VERSION) UI_VERSION=$(UI_VERSION) \
	RUNTIME_VERSION=$(RUNTIME_VERSION) AGENT_VERSION=$(AGENT_VERSION) MOCK_OIDC_VERSION=$(MOCK_OIDC_VERSION) \
	WEB_TOOLS_VERSION=$(WEB_TOOLS_VERSION) SANDBOX_VERSION=$(SANDBOX_VERSION) RELEASE=$(RELEASE) DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) \
	SKIP_IMAGE_REPORT=$(SKIP_IMAGE_REPORT) bash scripts/push-images.sh $(PUSH_IMAGES)

# 레지스트리가 잰 이미지 취약점과 이미지마다의 서명 보고 (D55, D66). push-images 가 올린 뒤에도 따로 볼 수 있다
image-report:
	REGISTRY=$(REGISTRY) REPORT_REFS="$(REPORT_REFS)" REPORT_TAG=$(REPORT_TAG) DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) \
	bash scripts/image-report.sh $(REPORT_IMAGES)

# CPython 빌드의 판 기록 (P58, D70). 핀을 올렸을 때만 돌린다. 기록이 핀을 따라가지 않으면 platform 테스트가 실패한다
cpython-record:
	DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) bash scripts/cpython-record.sh

# 제품 이미지 바탕의 digest 고정을 옮긴다 (D55). 고정이 빠진 FROM 이 있으면 platform 테스트가 실패한다.
# base-pins-check 는 쓰지 않고 낡은 고정을 알린다(릴리스 전 점검)
base-pins:
	bash scripts/base-pins.sh

base-pins-check:
	bash scripts/base-pins.sh --check

# 제품 이미지의 lock(파이썬 여섯, ui 의 npm)과 고정한 바탕을 고정한 Trivy 로 본다 (D55). 릴리스 전 점검이고 pr-check 는
# 돌리지 않는다
deps-audit:
	DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) bash scripts/deps-audit.sh

base-scan:
	DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) bash scripts/base-scan.sh

# 레지스트리 pull secret (D53). 플랫폼은 테넌트 네임스페이스에 복사하지 않으므로 런타임·에이전트 파드가 뜨는 곳마다 만든다
registry-pull-secret:
	@[ -n "$(NS)" ] || { echo "usage: make registry-pull-secret NS=\"daegam-system daegam-default\""; exit 2; }
	KUBE_CONTEXT=$(KUBE_CONTEXT) REGISTRY=$(REGISTRY) PULL_SECRET=$(PULL_SECRET) PULL_SECRET_SAS="$(PULL_SECRET_SAS)" \
	bash scripts/registry-pull-secret.sh $(NS)

# LLM 게이트웨이 네임스페이스 거두기 (P38 3절). 설치본에는 INSTALL_CONTEXT=<설치 값 파일의 INSTALL_CONTEXT> 를 준다
llm-gateway-uninstall:
	INSTALL_CONTEXT=$(INSTALL_CONTEXT) KUBE_CONTEXT=$(KUBE_CONTEXT) LLM_GATEWAY_NS=$(LLM_GATEWAY_NS) \
	PLATFORM_NS=$(PLATFORM_NS) bash scripts/llm-gateway-install.sh uninstall

# LLM 게이트웨이의 남의 이미지 넷을 설치 값의 레지스트리(REGISTRY)에 digest 그대로 둔다 (P38 3절, D53·D55 의 예외)
mirror-images:
	REGISTRY=$(REGISTRY) AGENTGATEWAY_VERSION=$(AGENTGATEWAY_VERSION) DRY_RUN=$(DRY_RUN) \
	bash scripts/mirror-images.sh $(MIRROR_IMAGES)

# 허브 미러 (P55 6절). 입력(HUB_*, DAEGAM_CA_FILE)은 환경이나 make 명령줄의 변수이고 make 가 그대로 자식의 환경으로
# 넘긴다. 레시피 글에 값을 옮겨 적지 않으므로 값이 make 함수나 셸 글로 읽히지 않는다. 비밀번호는 명령줄 변수로 받지
# 않는다(위의 COSIGN_PASSWORD 막음)
hub-mirror:
	uv run --quiet --project $(PLATFORM_DIR) python -I scripts/hub_mirror.py mirror

hub-resign:
	uv run --quiet --project $(PLATFORM_DIR) python -I scripts/hub_mirror.py resign

e2e-install:
	E2E_TAG=$(E2E_TAG) E2E_INSTALL_EXTRA="$(E2E_INSTALL_EXTRA)" INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) \
	E2E_INSTALL_REBOOT=$(E2E_INSTALL_REBOOT) bash scripts/e2e_install.sh

e2e-install-extra:
	E2E_TAG=$(E2E_TAG) E2E_INSTALL_EXTRA="$(E2E_INSTALL_EXTRA)" INSTALL_CONTEXT=$(INSTALL_CONTEXT) E2E_INSTALL_USER=$(E2E_INSTALL_USER) \
	bash scripts/run_e2e_install_extra.sh

# 로컬 클러스터의 차트 배포 (P85 4절). 태그, 롤아웃 대기, 스펙이 그대로인 Deployment 의 재시작은 scripts/deploy-local.sh 다
deploy-local:
	$(DEPLOY_LOCAL) deploy

# UI 만 올린다 (D52). 플랫폼 Deployment 는 건드리지 않으므로 비서·에이전트 채팅의 스트림이 끊기지 않고 워커도 그대로다.
# UI 만 바꿨으면 make image-ui deploy-ui-local 이고, 둘 다 바꿨거나 차트·값을 바꿨으면 make image-platform image-ui deploy-local 이다
deploy-ui-local:
	$(DEPLOY_LOCAL) ui

undeploy-local:
	$(DEPLOY_LOCAL) undeploy

# LLM provider 키는 플랫폼 env 가 아니라 $(PLATFORM_NS) 의 Secret 에서만 온다 (D25). 로컬 배포본에 셸의
# 키를 넣는 한 줄이다. Secret daegam-llm-<kind>(키 api_key) 를 만들거나 갱신한 뒤 PATCH $(LLM_API)/settings 로
# provider local-<kind> 를 등록하고, defaults.spec_generator 가 비어 있으면 그것으로 채운다. header 모드
# (X-Daegam-Tenant: default) 전제이고 oidc 배포본이면 DAEGAM_TOKEN 을 Bearer 로 쓴다.
# 기본 대상은 ingress 로 연 배포본($(UI_FULL_BASE))이라 /etc/hosts 에 $(UI_FULL_HOST) 매핑이 있어야 닿는다.
# 매핑이 없으면 port-forward 나 make run 으로 연 주소를 LLM_API=http://127.0.0.1:$(PLATFORM_PORT)/api 로 넘긴다.
LLM_API ?= $(UI_FULL_BASE)/api
llm-provider-local:
	KUBE_CONTEXT=$(KUBE_CONTEXT) PLATFORM_NS=$(PLATFORM_NS) DAEGAM_API=$(LLM_API) bash scripts/llm_provider_local.sh

# 사내 방화벽 CA 를 Platform Settings network.ca_bundle 에 넣는다 (P39b 11절). CA 파일은 저장소 밖에 둔다
web-ca-local:
	DAEGAM_API=$(LLM_API) DAEGAM_CA_FILE=$(DAEGAM_CA_FILE) bash scripts/web_ca_local.sh

# --- ui (P7) ----------------------------------------------------------------

ui-install:
	cd $(UI_DIR) && npm ci

ui-dev:
	cd $(UI_DIR) && VITE_API_PROXY=$(UI_API_PROXY) npx vite

ui-build:
	cd $(UI_DIR) && npx tsc -b && npx vite build

# 서버를 띄우지 않고 앱 팩토리에서 OpenAPI 를 뽑는다. ui/openapi.json 은 중간 산출물(ignore),
# ui/src/api/schema.d.ts 가 커밋 대상이다. CI 는 이 타깃 뒤 `git diff --exit-code ui/src/api/schema.d.ts`.
ui-types:
	cd $(PLATFORM_DIR) && uv run python -c \
		"import json; from daegam.main import create_app; print(json.dumps(create_app().openapi(), indent=1, ensure_ascii=False))" \
		> ../$(UI_DIR)/openapi.json
	cd $(UI_DIR) && npx openapi-typescript openapi.json -o src/api/schema.d.ts

ui-test:
	cd $(UI_DIR) && npx vitest run $(VITEST_ARGS)

ui-e2e-install:
	cd $(UI_DIR) && npx playwright install chromium

# 목 API(9300)와 vite dev(4173)는 playwright.config.ts 의 webServer 가 띄우고 끝나면 내린다
e2e-ui-smoke:
	cd $(UI_DIR) && npx playwright test

# Playwright full: deploy-local 클러스터의 플랫폼을 ingress(같은 origin, /api) 로 열어 허브 → Gateway 시나리오를
# 브라우저로 1회 재현한다. header 모드 전제(oidc 면 스펙이 SKIP 로 종료).
# 사전 조건: make deploy-local + lab-up(mock-soap), /etc/hosts 의 $(UI_FULL_HOST) 매핑, Traefik ingress.
e2e-ui-full:
	@# 브라우저가 실제로 밟는 경로(ingress, 같은 origin, /api)로 검증한다. port-forward 를 쓰지 않는다.
	@# port-forward 는 rollout 직후 Terminating 파드에 붙었다 끊기고, 재연결 루프는 set -e 를 물려받아
	@# kubectl 의 첫 비영 종료에서 죽어 화면이 마지막 상태(Pending)에 고정되는 레이스가 있었다 (실측).
	@# 전제: /etc/hosts 의 $(UI_FULL_HOST) 매핑과 Traefik ingress, header 인증 모드.
	@$(KUBECTL) -n $(PLATFORM_NS) rollout status deploy/daegam-platform --timeout=60s >/dev/null
	@$(KUBECTL) -n $(PLATFORM_NS) rollout status deploy/daegam-ui --timeout=60s >/dev/null
	@# /healthz 는 플랫폼만 말한다. 화면은 daegam-ui 의 /build.json 으로 본다(D52)
	@curl -fsS -m 5 -o /dev/null $(UI_FULL_BASE)/build.json || { echo "e2e-ui-full: $(UI_FULL_BASE)/build.json unreachable (daegam-ui 가 떠 있고 Ingress 가 / 를 그리로 보내는지 확인)"; exit 1; }
	@curl -fsS -m 5 -o /dev/null $(UI_FULL_BASE)/healthz || { echo "e2e-ui-full: $(UI_FULL_BASE)/healthz unreachable. /etc/hosts 에 '$$($(KUBECTL) -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}') $(UI_FULL_HOST)' 가 있는지, Traefik ingress 가 떠 있는지 확인"; exit 1; }
	cd $(UI_DIR) && KUBE_CONTEXT=$(KUBE_CONTEXT) SPIKE_NS=$(SPIKE_NS) \
	E2E_UI_BASE=$(UI_FULL_BASE) npx playwright test --config playwright.full.config.ts

# D52 UI 컨테이너 e2e. 두 갈래다. 직결 갈래는 svc/daegam-ui 로 port-forward 해서 Ingress 와 무관하게 nginx 를 본다.
# 서빙하는 빌드가 지금 ui/ 의 도장인지, 정적 규칙(딥링크·캐시·없는 에셋 404·405·경로 정규화), 보안 헤더(모든 응답,
# 플랫폼과 같은 CSP, X-Forwarded-Proto 로 켜는 HSTS)다. Traefik 갈래는 경로
# 분할, 화면과 API 의 CSP, UI 만 바꾼 배포(make image-ui deploy-ui-local)가 플랫폼 파드를 건드리지 않는지, 인증 모드를
# 바꾸면 둘이 함께 굴러 CSP 가 따라오는지 본다. make image-platform image-ui deploy-local 이후
e2e-ui-serving:
	KUBE_CONTEXT=$(KUBE_CONTEXT) PLATFORM_NS=$(PLATFORM_NS) bash scripts/e2e_ui_serving.sh

# 운영자의 설치마다의 타깃. 운영자 저장소의 ops/<설치>/<이름>.mk 가 타깃과 VERIFY_EXTRA 를 둔다. 공개 트리에는 ops/ 가 없다
-include $(wildcard ops/*/*.mk)
