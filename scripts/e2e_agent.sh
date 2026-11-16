#!/usr/bin/env bash
# P12 (D26) + P13 (D27) + P16 (D33) + P17 (D34) + P35 (D65) Agent e2e against the real (orbstack) cluster. Assumes `make deploy-local`
# (platform in-cluster, ConfigMap DAEGAM_ALLOW_STUB_PROVIDER=true) and `make lab-up` (mock-soap, postgres, redis:8 in daegam-spike).
#
# Memory kind: E2E_MEMORY_KIND=postgres (default, `make e2e-agent`) binds the lab Postgres to the memory node.
# E2E_MEMORY_KIND=redis (`make e2e-agent-redis`) binds the lab Redis 8 instead (D33): STEP 4 asserts the verify result
# (redis.version >= 8, checkpointer ready), STEP 6 the redis binding env and agent.json, STEP 8r the keys and FT indexes
# through `kubectl exec deploy/redis -- redis-cli`, and STEP 12r that the keys survive the instance delete before the
# script deletes them itself (only this instance's prefix and this run's session ids; never `write_keys_zset:*` as a
# whole nor ToolHive's `daegam:` prefix). The postgres path is unchanged.
#
# Auth mode: E2E_AUTH_MODE=header (default, `make e2e-agent`) runs STEP 1~12 with the tenant header and STEP 13a
# proves that an exposed deploy is 422 in header mode (STEP 13 is a conditional SKIP). E2E_AUTH_MODE=oidc
# (`make e2e-agent-oidc`) patches ConfigMap daegam-platform-config DAEGAM_AUTH_MODE=oidc in STEP 0, rolls the
# platform, runs STEP 1~12 with a bootstrap admin token (aud daegam-api from the in-cluster mock-oidc, like
# e2e_mcp_auth.sh) and STEP 13 with data-plane user tokens (aud daegam-mcp); the EXIT trap restores the previous
# mode and rolls the platform again. Both switches go through scripts/lib/e2e_auth_mode.sh, and STEP 1 and the restore
# wait until the platform has re-applied data-plane auth (GET /api/settings/data-plane-auth pending=0 failed=0, P32).
# E2E_AUTH_MODE=local (`make e2e-agent-local`, D40) patches DAEGAM_AUTH_MODE=local
# plus the bootstrap password in Secret daegam-platform-secrets, signs in as the bootstrap admin (the server forces a
# password change first), checks discovery + JWKS on the platform, and STEP 13 mints data-plane tokens for users it
# creates with POST /users and adds as viewers (A and B in $TENANT, the ops user in e2e-ops). local mode fixes a tenant's MCP
# group to daegam-<tenant> (P32), so those tokens carry daegam-$TENANT and daegam-e2e-ops. STEP 13 pins the exposed host to the Traefik LoadBalancer IP with `curl --resolve`
# (e2e_gateway.sh STEP 12~17 do the same), so /etc/hosts is never touched.
# Turn tokens (P53): in oidc and local mode STEP 0 also sets DAEGAM_TURN_TOKENS_ENABLED=true (local adds
# DAEGAM_TURN_TOKEN_TTL_SECONDS=60) in the same ConfigMap patch, so the EXIT trap restores them with the mode. The steps
# marked (P53) read who a tool call carried from the MCP audit (GET /api/audit/mcp sub·act) and the grant records from
# the platform's MongoDB pod (mongosh inside mongodb-0, with that pod's own credentials; scripts/lib/e2e_turn_tokens.sh).
#
# Gate (12 steps + 장기 기억 + exposure + P35). 아래 적은 차례로 돈다(7d 가 7c 앞, 13(h) 가 13(f) 앞, 14 가 마지막):
#   STEP 0   (oidc·local) switch_auth_mode: ConfigMap DAEGAM_AUTH_MODE, rollout restart + status. oidc 는
#            DAEGAM_DATA_PLANE_ISSUER=idp 를 함께 넣는다(데이터 플레인이 IdP 를 믿는 oidc, P53b 6.2절)
#   STEP 1   platform reachable (port-forward), the deployed auth mode is the requested one, home cluster preflight ok,
#            (oidc·local) wait_auth_reapplied: the platform re-applied data-plane auth after the switch
#   STEP 1t  (oidc·local, P53) 위임이 열릴 사람: 관리자를 $TENANT 의 developer 로 둔다(platform-admin 의 * 행은 테넌트를 쥐지
#            않아 위임이 열리지 않는다). local 은 그 테넌트의 developer 인 승인자 approver-e2e 를 만들고, oidc 는 그룹 매핑
#            하나로 관리자 토큰에 $TENANT 의 developer 그룹을 싣는다. GET /me 의 memberships 에 $TENANT 가 있다
#   STEP 2   stub provider `stub-e2e` registered in Platform Settings (PATCH /settings, idempotent).
#            원래 없었으면 EXIT 트랩이 다시 걷는다 (실측 흔적을 Platform Settings 에 남기지 않는다)
#   STEP 3   backend calculator (WSDL, endpoint mock-soap) → MCP Server calc-agent-e2e [Add, Divide] 저장(draft) → 배포 → Ready
#   STEP 4   lab Postgres registered as Database (kind postgres, password with URL-reserved chars) + verify.
#            Without the lab Postgres the memory node is dropped (in-memory path) and the HITL steps are SKIP.
#            redis: lab Redis registered as Database redis-agent-e2e (kind redis, db 0, with password) + verify →
#            redis.version >= 8, redis.checkpointer == ready, GET detail capabilities.checkpointer == ready (no SKIP path)
#   STEP 5   template graph → provider stub-e2e, tools source = MCP Server, memory = Database → POST /agents (201, draft 로 저장만 된다)
#            → POST /validate ok. memory 노드가 있으면 장기 기억(D34)도 켠다: memory.long_term.retention_days = 30 과
#            메인의 builtin_tools.filesystem = true (/memories/ 를 읽고 쓰는 것이 파일 도구라 둘이 함께 간다). 승인이 있으면
#            Divide 의 허용 결정에 edit 을 더한다(8e)
#   STEP 5g  (E2E_SHARED_STEPS=skip 이면 Redis 실행은 건너뛴다) calc MCP Server 를 게시해 묶은 Gateway 를 도구 소스로 쓰는 Agent
#            gw-agent-e2e(memory 없음) Ready → 채팅 Add(3,4)=7, GET /metrics 의 tools.protocols 에서 그 소스가 2026-07-28
#            → Agent·Gateway·게시를 걷는다.
#            oidc 는 Gateway 소스가 요구하는 Tenant Settings agent_oauth(mock-oidc 의 client credentials agent-dev,
#            테넌트 네임스페이스의 Secret)와 agent_client_ids·mcp_group 을 먼저 둔다(EXIT 트랩이 되돌린다).
#            (P53) local: 그 클러스터의 Gateway 게이트가 열리기를 기다리고, 채팅의 vMCP 감사 기록이 sub=관리자, act=그
#            Agent 다. 실험 토큰(svc_token.py)을 vMCP 에 직접 보내 이 Gateway 에 묶인 토큰(그룹 없음)은 Add=7 이고, 다른
#            Gateway 에 묶인 토큰·이 Gateway 에 묶였지만 다른 테넌트를 적은 토큰·bound_to 없이 act 만 실은 토큰은 테넌트
#            그룹을 실었어도 빈 목록과 거부다. oidc: vMCP 감사 기록이 Agent 의 client(sub=agent-dev, act 없음)이고, act 를
#            실은 IdP 토큰은 테넌트 MCP 그룹이 있으면 전처럼 Add=7 이다
#   STEP 6   POST /deploy (본문은 cluster_id 만이다. 이름은 저장할 때 정해졌다) → Ready; Deployment·Service·NetworkPolicy
#            agent-<name>, token Secret, provider Secret copies (kubectl).
#            서버마다의 audience(P50 9절): MCP Server 소스의 토큰 키·env DAEGAM_MCP_TOKEN_<id> 가 그 서버의 audience 를
#            싣고, init 의 표 지문(DAEGAM_MCP_TOKEN_TABLE)을 머리로 보낸 agent.json 의 auth 가 그 env 다
#            redis: pod env has DAEGAM_DB_0_HOST·DAEGAM_DB_0_DB and no DAEGAM_DB_0_DATABASE; GET /agent.json with the
#            instance token (Bearer, read from the token Secret) has memory.checkpointer.kind == redis
#   STEP 6f  기능 이름(D65): 상태의 runtime.features 에 cancel, runtime.image 가 스냅숏 이미지(effective_config.images.agent_image),
#            init 컨테이너 fetch-agent-config 의 이미지가 그 런타임 이미지, 준비 검사 경로가 /healthz?probe=ready
#   STEP 7   chat SSE one turn: {"tool":"Add","args":{"a":3,"b":4}} → tool_call Add → tool_result 7 → done. 그 뒤
#            GET /metrics 의 tools.protocols 에서 MCP Server 소스도 2026-07-28. (oidc·local, P53) 파드 로그의 turn_start 가
#            grant_id 를 싣는다. 감사 기록은 9t 가 본다
#   STEP 7d  인사말: STEP 7 의 첫 SSE 이벤트가 greeting 이고 글이 템플릿의 인사말(= status.greeting), 같은 세션의 둘째 턴에는 없다
#   STEP 7c  끊김(D92, P78): STEP 7 세션에 {"sleep": 20, "tool": "Add", "after": 5} 를 보내고 3초 뒤 curl 을 끊는다 → 턴은
#            끝까지 가므로 곧바로 연 같은 세션의 다음 채팅이 409 turn_in_progress, 세션이 running 이고 done 전에 결과를 받은
#            Add 카드가 전사에 있으며, 끊은 뒤 40초 안에 running 이 거짓이 되고 전사가 done 으로 끝난다. 파드 로그의 그 시도
#            turn_end 가 done 이다. (oidc·local, P53) 그 턴의 위임 기록이 런타임의 /end 로 done 으로 끝났다(연결과 떨어져 도는
#            턴의 위임은 스트림이 닫혀도 끝나지 않는다, P78 8절)
#   STEP 7e  중지 API: 같은 세션에 {"sleep": 20} 을 보내며 응답 머리 X-Daegam-Session-Id 를 읽고 2초 뒤
#            POST /sessions/{sid}/cancel 202 → 열린 스트림이 3초 안에 error{code: turn_cancelled} 로 끝난다. 턴이 없을 때의
#            중지는 409 no_turn_in_progress
#   STEP 7t  (local, P53) 토큰 수명 60초: 스텁이 75초 기다린 뒤 Add(5,6) 을 부르는 턴이 갱신한 토큰으로 11 을
#            받고 위임 기록의 renewals >= 1 이다. 같은 턴 도중 관리자를 $TENANT 에서 빼면 갱신이 403 으로 거절되어 런타임이
#            턴을 error{code: turn_access_lost} 로 멈추고 Add 는 보내지지 않는다(P78 7절, 9t 가 그 창에 감사 기록이 없음을
#            본다). 다시 넣으면 다음 턴의 Add 가 사람의 이름으로 간다
#   STEP 8   HITL: {"tool":"Divide",...} → interrupt{allowed [approve, edit, reject]} → GET /pending → POST /decisions approve →
#            tool_result → done
#   STEP 8s  (memory, P43) 바깥 내용 뒤의 승인과 기억한 선택을 따로 띄운 Agent safety-agent-e2e(Add 의 결과를 바깥 내용으로
#            표시하고 Divide 의 승인을 기억할 수 있게 했다. 서브에이전트 calc 가 같은 Tool 을 쓴다)에서 본다. 상태의
#            runtime.features 에 tool_safety 가 있다. 세션 A: Divide 가 이유 rule 로 묻고 GET /pending 이 기억할 수 있는 선택
#            [approve, reject] 를 알린다 → approve 와 remember approve → 다음 Divide 는 묻지 않고 돈다 → GET /me/approval-choices
#            에 그 선택이 있다. 세션 B: Add 가 돌고 tool_result.untrusted 가 outside → 다음 Divide 는 기억한 승인이 있어도 이유
#            outside 로 묻고 선택은 [reject] 뿐이다 → 그 요청의 remember approve 는 런타임에 가기 전에 422 remember_refused(바깥
#            내용 때문에 물은 요청) → reject 와 remember reject → 다음 Divide 는 묻지 않고 tool_choice_rejected 로 거절된다. 세션 C: Add 뒤의 Add 가 이유 outside 로 묻는다(규칙 없는 쓰기). 세션 D: 서브에이전트가 Add 를 부른 task
#            결과가 outside 이고 메인의 다음 Add 가 이유 outside 로 묻는다. 기억한 거부가 승인을 덮어써 목록에는 Divide reject
#            하나만 있고, 끝에 그것을 계정 라우트로 지운 뒤(204) Agent 를 지운다. 앞 실행이 남긴 이 Agent 의 선택은 먼저 지운다
#   STEP 8e  고치기(D65): Divide 보류에 이름을 Add 로 바꾼 edit 은 422 edit_name_mismatch 이고 세션은 pending 그대로, 이름이
#            같은 edit(numerator 9, denominator 3)은 tool_result 가 langchain 의 안내문 뒤 3.0 이고 재개 스트림이 다시
#            알린 tool_call 과 전사의 그 카드 인자가 고친 값
#   STEP 8t  (local·memory, P53 5.3절) 관리자의 Divide 보류를 승인자 approver-e2e 가 approve → 재개의 위임 기록이 trigger
#            decide, sub=관리자(소유자), decided_by=승인자
#   STEP 8u  같은 보류를 만든 뒤 관리자를 $TENANT 에서 빼고 승인자가 approve → 위임이 승인자 자신의 것(sub=승인자,
#            session_owner=관리자)이다. 관리자는 되돌린다
#   STEP 8v  승인자가 보류의 인자를 고친 edit → 위임이 승인자 자신의 것(sub=승인자, session_owner=관리자)이다.
#            8t·8u·8v 의 Divide 감사 기록은 9t 가 본다
#   STEP 8m  long-term memory (D34): (1) Postgres 면 provision 이 만든 스키마 daegam_store 와 런타임 마이그레이션이 만든
#            store 테이블이 있다, (2) 한 세션이 /memories/ 아래에 write_file, (3) 새 세션이 read_file 로 그 내용을 읽는다,
#            (4) 저장소에 이 인스턴스의 네임스페이스로 행(Postgres)·키(Redis)가 있다, (5) ephemeral 턴이 쓴 것은 다음
#            세션에 없다, (6) DELETE /memories/<없는 sub> → deleted=0 이고 이 사용자의 행·키는 그대로다.
#            스텁은 내장 파일 도구를 스스로 안 고르므로 메시지 안 JSON 조각으로 이름을 직접 댄다
#   STEP 8r  (redis only) redis-cli: (1) --scan daegam-agent:<inst>:* has :checkpoint:·:checkpoint_write:·:session: keys,
#            (2) --scan write_keys_zset:<sid>:* is non-empty for the STEP 7·8 session ids (the library keeps that registry
#            at the root, outside the prefix), (3) FT._LIST has exactly the three indexes daegam-agent:<inst>:{checkpoint,
#            checkpoint_write, store} under this instance's prefix (store 는 장기 기억의 것이다, D34)
#   STEP 8j  (P43 7절) 판정 모델: Add 의 결과를 바깥 내용으로 표시하고 스텁 판정 모델의 injection_check(block)를 둔 Agent
#            judge-agent-e2e(memory 없음)의 runtime.features 에 tool_safety 와 injection_check 가 있다. Add(31000, 337)=31337
#            은 스텁 판정이 주입으로 보아 tool_result 가 error 이고 code 가 injection_blocked 이며 guardrail{rule:
#            injection_check, action: block} 이 오고 턴은 done 으로 끝난다. 새 세션의 Add(3,4) 는 success 에 untrusted outside 이고
#            guardrail 이벤트가 없다. 끝에 Agent 를 지운다
#   STEP 9   GET /sessions, /metrics, /events are populated (events are pushed by the runtime or read by the AuditCollector
#            every 60s). STEP 7 세션의 이벤트에 turn_end done 과 cancelled(7e 가 멈춘 턴), Add 의 도구 실행 스팬 tool_exec
#            가 함께 있고, 지표의 턴·Tool 수는 그 이벤트에서 센 값이다(P37 11절)
#   STEP 9t  (oidc·local, P53 7절) MCP Server 의 감사 기록: STEP 7 의 Add 가 sub=관리자, act=svc:agent:<k8s 이름>.
#            local 은 7t 의 갱신한 턴과 되돌린 뒤의 턴도 그렇고 끊긴 창에는 Add 가 하나도 없으며, 8t 의 Divide 는
#            sub=관리자, 8u·8v 의 Divide 는 sub=승인자다. 수집기가 60초마다 읽으므로 한 번에 기다린다
#   STEP 9r  (E2E_SHARED_STEPS=skip 이면 make e2e-agent 만) API 서버 경로(P35 14절): deploy/dev/lab/agent_route.py 가 kubecontext 클라이언트
#            (via=apiserver)와 bearer_service_client 로 이 Agent 의 /metrics·/chat(Add)을 부르고, {"sleep": 6, "tool": "Add", "after": 5} 채팅을
#            2초 뒤 끊는다. 다음 턴이 409 turn_in_progress 이고 가벼운 읽기(?view=progress)가 done 전에 결과를 받은 Add 카드를
#            보이며 40초 안에 running 이 거짓이고 전사가 done 으로 끝나야 한다(D92, 플랫폼 venv, uv run --project platform)
#   STEP 10  redeploy while a session is pending and the revision moved (deployed_revision 이 따라온다) → warnings[] pending_sessions_revision_changed,
#            Ready again, the pending session resumes on the new pod (reject) → done. 이 재배포가 replicas: 2 를 싣는다
#   STEP 10m (memory) 파드 둘: 2/2 Ready 뒤 {"sleep": 20} 턴을 도는 파드를 로그의 turn_start 로 찾고 다른
#            파드에 kubectl port-forward pod/<다른 파드> 로 붙어 인스턴스 토큰으로 런타임의 중지를 부른다 → 202, 스트림이
#            3초 안에 turn_cancelled 로 끝난다. 다른 파드에 닿은 중지가 색인 표시로 전해짐을 확정으로 본다
#   STEP 10k (memory, header) 죽은 파드의 턴(P79, D98): 도구 결과를 받은 뒤 60초 기다리는 턴을 뒤에서 연다. Postgres 는 새 말
#            {"tool": "Add", ..., "after": 60}, Redis 는 STEP 8 처럼 소유자가 승인한 {"tool": "Divide", ..., "after": 60} 의
#            재개다. 가벼운 읽기에 결과를 받은 카드가 보이면 그 턴을 도는 파드를 kubectl delete pod --grace-period=0 --force 로
#            지운다(SIGTERM 뒤 2초에 SIGKILL 이라 drain 이 시작되지만 턴은 죽는다). 지운 직후와 30초 뒤에는 세션이 아직
#            running 이고, 지운 뒤 90초 안에 ?view=progress 가 running 거짓을 읽는다. 전사는 그 카드 뒤 error{code: turn_aborted}
#            로 끝나고, 플랫폼의 그 세션 턴 목록이 그 실행을 turn_aborted 로 보이며 그 실행의 turn_end 에 repaired 가 있다. 같은
#            세션의 다음 말이 받아지고(Redis 는 409 session_pending 이 아니다) 파드 둘이 다시 Ready 다
#   STEP 10d (memory. E2E_SHARED_STEPS=skip 이면 header 만) 파드 종료(D92, P78 5절): Advanced 덮어쓰기로 grace 를 90초로 낮추면 같은 적용에서
#            DAEGAM_TURN_DRAIN_SECONDS 가 30 이 된다. {"sleep": 20} 턴과 Add 뒤 75초 기다리는 턴을 뒤에서 열고 두 턴을 도는
#            파드를 지운다 → 지켜보던 짧은 턴은 SIGTERM 뒤(지운 뒤 5초 넘어) drain 안에 done 으로 끝나고, 긴 턴은 drain 이 끝날 때(지운 뒤 30초 넘어)
#            스트림에 error{code: turn_aborted} 를 받으며, 그 세션은 저장된 Add 결과 뒤 turn_aborted 로 끝나 running 이 거짓이다
#   STEP 11  real provider one turn (E2E_LLM_PROVIDER=<registered provider with a key>), otherwise SKIP (conditional)
#   STEP 11r (local, P52) Tool 규칙. STEP 3 의 MCP Server 에 "Add 는 이 Agent 만, a 는 100 이하", "Divide 는 developer 이상"
#            규칙을 둔 리비전을 저장하면 답의 rules_notice.rolling_agents 에 이 Agent 가 있다(다시 뜰 수 있다). 재배포 뒤
#            조건 기록이 서고(conditions_closed 가 사라진다) Agent 의 토큰 재발급이 끝나면(agent_tool_hidden 의 cause reissue
#            가 사라진다) Agent 상태가 Divide 를 cause not_named 로 숨긴다고 알린다. Agent 의 Deployment 롤아웃을 기다린 뒤,
#            채팅의 Add(3,4)=7 은 사람이 누구든 이 Agent 의 이름으로 지나고, Add(500,1) 은 백엔드에 가지 않고 인자 a 를 적은
#            Tool 오류다. 서버 프록시에 바로 붙으면 플랫폼이 다시 낸 이 Agent 의 소스 토큰(Secret 의 그 키, agent_id 를 싣는다.
#            턴 위임 토큰이 꺼졌을 때 쓰는 신원)과 멤버가 아닌 사람을 실은 이 Agent 의 위임 토큰은 Add 만 보고 7 을 받으며
#            Divide 를 보지 못한다(역할만 둔 규칙은 모든 Agent 에게서 숨긴다). 이름 대지 않은 Agent 의 서비스 토큰과 위임
#            토큰은 빈 목록과 거부다. 그다음 Add 를 add_numbers 로 바꾼 리비전을 재배포하는 동안과 끝난 뒤에, 이름 대지 않은
#            Agent 의 토큰은 두 이름 어느 것도 부르지 못한다(이 Agent 의 목록이 그 순간 읽힌 거절만 센다). 끝난 뒤 이 Agent 의
#            토큰은 add_numbers 만 보고 7 을 받는다. 끝에 규칙과 오버라이드를 걷은 리비전을 재배포해 STEP 12·13 이 전처럼
#            Add·Divide 를 쓰게 한다
#   STEP 12  STEP 13 이 쓸 정의를 먼저 복제(support-e2e-ext, draft)한 뒤 delete → Deployment·Service·NetworkPolicy·token
#            Secret·provider Secret copies gone; 404 afterwards (삭제가 정의까지 지우므로 410 이 아니다). 삭제 전에 뒤에서 연
#            {"sleep": 20} 턴(memory 면 STEP 7 의 이전 세션)은 삭제 호출 뒤 30초 안에 error{code: turn_cancelled} 로 끝난다.
#            삭제가 토큰을 거두기 전에 런타임의 /turns/cancel 로 인스턴스의 턴을 모두 멈추기 때문이다(P78 5절)
#   STEP 12m 삭제가 장기 기억을 걷는다 (D34): daegam_store.store 의 이 인스턴스 prefix 행이 0 (Redis 면 store 키가 0).
#            스키마와 테이블은 남는다. 체크포인터를 남기는 12r 과 여기서 갈린다
#   STEP 12r (redis only) the (1) keys are still there (instance delete leaves Redis alone, like the Postgres rows); then the
#            script DELs daegam-agent:<inst>:* and write_keys_zset:<sid>:* per session id of this run and FT.DROPINDEX the
#            three indexes. Cleanup failures are warnings, never die
#   STEP 13a (header only) STEP 12 의 복제본을 노출과 함께 deploy → 422 unauthenticated_exposure ("requires oidc mode"),
#            복제본은 draft 그대로다 (422 는 문서를 건드리기 전에 난다)
#   STEP 13  (oidc·local) STEP 12 의 복제본 support-e2e-ext 를 노출과 함께 deploy: http://agent-e2e.e2e.local (TLS none, cors_origins one origin,
#            allowed_groups empty; memory 가 있으면 복제본도 10m 의 replicas 2 를 이어받는다) → Ready,
#            Ingress(/chat·/sessions·/memories → agent-<name>:8080, owner Deployment),
#            NetworkPolicy daegam-expose-<name>, pod env DAEGAM_OIDC_AUDIENCE=daegam-mcp and DAEGAM_ALLOWED_GROUPS empty (oidc) or
#            daegam-$TENANT (local fills an empty allowed_groups with the tenant group, P32), address assigned. Through Traefik:
#            (아래 (a)~(g) 는 `STEP 13(a)` .. `STEP 13(g)` 로 따로 찍는다. `STEP 13` 은 배포와 렌더된 객체까지다.
#             위의 STEP 13a 와 헷갈리지 말 것. 그것은 header 모드의 422 로 다른 STEP 이다)
#            (a) no token 401 unauthorized + WWW-Authenticate (+ Access-Control-Allow-Origin when Origin is sent), CORS preflight
#            (b) control-plane token (aud daegam-api) 401 invalid_token
#            (c) user A (aud daegam-mcp): Add(3,4) → 7 with a spoofed body user_sub (owner is the token sub); Divide → interrupt
#                arrives before the stream closes (incremental SSE: first byte, per-event arrival, EOF, curl timings recorded)
#            (d) user B: A's session·pending·decisions 403 not_session_owner, own list empty; A: pending 200 → approve → 4
#            (d2) 장기 기억은 사람마다 따로다 (D34): A 가 /memories/ 에 쓴 것을 A 는 읽고 B 는 못 읽는다 (네임스페이스가 토큰 sub).
#                 Traefik 을 지난 GET /memories 도 A 의 목록에만 그 파일이 있다 (P36 8절)
#            (c2) (local only) the ops token (member of e2e-ops only) on this agent of tenant $TENANT with an empty allowed_groups
#                 → 403 forbidden: a token of another tenant does not open it (P32 tenant boundary)
#            (s) (local only, P53b 6.7절) 관리자가 만든 viewer 서비스 어카운트의 토큰(sub sa:<id>, groups [daegam-$TENANT])이
#                GET /sessions 200 이다. 계정은 그 단계가 지운다
#            (t) (P53 3.3절) local: 테넌트 그룹을 실은 실험 토큰은 GET /sessions 200 이고, 거기에 act 와 5g 의 Gateway
#                bound_to 를 더하거나 act 만 더하면 403 forbidden 이다. oidc: act 를 실은 IdP 토큰도 전처럼 200 이다
#            (e) /metrics via Traefik 404 (not routed), via the Service 403 forbidden; /healthz without token is {status} only
#            (h) A 의 새 세션 첫 이벤트가 greeting 이고 X-Daegam-Session-Id 가 Access-Control-Expose-Headers 에 있다. A 가
#                {"sleep": 20} 턴을 2초 뒤 /sessions/{sid}/cancel 로 멈추면 202 이고 스트림이 turn_cancelled 로 끝나며 B 의
#                같은 요청은 403 not_session_owner. 7c 와 같은 턴을 Traefik 을 지나 3초 뒤 끊으면 A 의 같은 세션 새 턴이
#                409 turn_in_progress 이고, 가벼운 읽기가 done 전에 결과를 보이며 40초 안에 done 으로 끝난다(D92). (f) 가
#                A 를 막으므로 그 앞에서 돈다
#            (f) redeploy with allowed_groups [$OPS_GROUP] (local daegam-e2e-ops, oidc ops) → A's token 403 forbidden, ops token → 7
#            (g) delete → Ingress·daegam-expose-<name>·Deployment gone, Traefik answers 404
#   STEP 14  (E2E_SHARED_STEPS=skip 이면 make e2e-agent 만) 키 두 벌(P35 12·13절): 종류 openai 인 provider openai-e2e-a·b 를 가짜 키로 넣고(PUT …/api-key) 메인 모델과 서브
#            모델에 하나씩 쓴 Agent keys-agent-e2e(도구·memory 없음)를 배포 → Deployment env 의 DAEGAM_LLM_OPENAI_E2E_A_API_KEY
#            와 DAEGAM_LLM_OPENAI_E2E_B_API_KEY 가 서로 다른 복사본을 가리키고 OPENAI_BASE_URL·OPENAI_API_KEY 가 없다, 인스턴스
#            토큰으로 받은 agent.json 의 env_key 가 그 둘이다. 쓰임을 맞바꾼 리비전을 배포해도 env 이름이 같고, 바꾼 것 없이
#            재배포하면 daegam.devops.ai.kr/config-sha 가 같고 새 ReplicaSet 이 없으며, 둘째 provider 의 키를 바꿔 재배포하면 값이
#            바뀌고 새 파드의 DAEGAM_LLM_OPENAI_E2E_B_API_KEY 가 새 키다. 마지막에 돈다
#   cleanup  남은 복제본 삭제; MCP Server and Database cleanup requested; STEP 2 가 넣은 stub provider 제거.
#            STEP 5g·14 가 만든 것(Agent·Gateway·게시·provider)은 그 단계가 끝에서 걷고, 도중에 실패하면 EXIT 트랩이 걷는다
#
# Steps print `STEP n PASS|SKIP|FAIL: ...`. macOS has no `timeout`, so every wait is a polling loop.
# Names: 한글 display_name 을 보내고 K8s 이름은 소문자·하이픈 name 이다 (D24).
#
# 재실행의 409 는 셋으로 갈리고 처리가 서로 다르다. 플랜 F5 는 만들기의 409 를 "DELETE 뒤 다시 POST" 로
# 적었지만 이 게이트는 lib/e2e_api.sh 머리말의 규칙을 따른다. 까닭이 서로 달라 한 규칙으로 묶으면 재실행이 막힌다.
#   만들기 (save_doc): 이름 충돌이다. 지우지 않고 PUT 으로 같은 본문을 덮어 id 를 지킨다. Agent 는 삭제가 장기
#     기억까지 걷는다
#   복제 (clone_fresh): 복제본 이름 충돌이라 그 복제본을 지우고 다시 복제한다 (STEP 12)
#   배포 (deploy_doc): detail.code 가 already_deployed 다. 보통은 redeploy 로 잇는다 (STEP 6).
#     STEP 3 의 MCP Server 만 그대로 쓰는데 까닭은 그 자리에 적었다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 끝나는 길(on_exit·gate_done)과 시간 제한을 붙인 curl 은 여기에 있다 (P64)
. "$HERE/lib/e2e_exit.sh"
CTX="${KUBE_CONTEXT:-orbstack}"
# 포트 배정표는 여기에 있다
. "$HERE/lib/e2e_ports.sh"
# json_of (JSON 에서 값 꺼내기) 는 여기에 있다
. "$HERE/lib/e2e_json.sh"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
SPIKE_NS="${SPIKE_NS:-daegam-spike}"
TENANT="${E2E_TENANT:-agent}"
API_PORT="${E2E_API_PORT:-$PORT_AGENT_API}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
LLM_PROVIDER="${E2E_LLM_PROVIDER:-}"
BASE="http://127.0.0.1:${API_PORT}"
# API 는 /api 아래에 있다. /healthz(probe)만 루트에 남아 $BASE 를 그대로 쓴다.
API="$BASE/api"
STUB_PROVIDER="stub-e2e"
BP_NAME="calc-agent"; MS_NAME="calc-agent-e2e"; DS_NAME="pg-agent-e2e"
AG_NAME="support-e2e"
BP_DISPLAY="계산기 SOAP"; MS_DISPLAY="계산기 서버"; DS_DISPLAY="에이전트 메모리 DB"
AG_DISPLAY="상담 에이전트 e2e"
# --- memory 노드의 Database 종류 (D33). redis 면 lab Redis 8 (db 0, 비밀번호 있음) 을 memory 에 넣는다 ---
MEMORY_KIND="${E2E_MEMORY_KIND:-postgres}"
if [ "$MEMORY_KIND" = redis ]; then DS_NAME="redis-agent-e2e"; DS_DISPLAY="에이전트 메모리 Redis"; fi
# --- 인증 모드와 외부 노출 (D27) ---
AUTH_MODE="${E2E_AUTH_MODE:-header}"
# 다른 변형이 같은 후보에서 다시 보는 단계는 E2E_SHARED_STEPS=skip 일 때만 조건부로 건너뛴다. 태그의 클러스터 e2e 잡이
# 고른 게이트에만 준다(.github/workflows/e2e-cluster.yml). 손으로 돌리면 모든 단계를 돈다. 인증 모드도 memory 종류도 읽지
# 않는 9r·14 는 header 모드의 Postgres 실행(make e2e-agent, 후보마다 돈다)이, memory 종류만 읽는 10d 는 header 모드의 두
# memory 실행이, memory 없는 Agent 의 5g 는 Redis 실행을 뺀 셋이 본다
SHARED_SKIP=0
[ "${E2E_SHARED_STEPS:-}" != skip ] || SHARED_SKIP=1
BASE_RUN=1
[ "$SHARED_SKIP" = 0 ] || [ "$AUTH_MODE $MEMORY_KIND" = "header postgres" ] || BASE_RUN=0
OIDC_PORT="${E2E_OIDC_PORT:-$PORT_AGENT_OIDC}"
RT_PORT="${E2E_RUNTIME_PORT:-$PORT_AGENT_RUNTIME}"
OIDC="http://127.0.0.1:${OIDC_PORT}"
TRAEFIK_NS="${TRAEFIK_NS:-traefik}"; TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"
INGRESS_CLASS="${INGRESS_CLASS:-traefik}"
EXT_HOST="${E2E_AGENT_HOST:-agent-e2e.e2e.local}"
CORS_ORIGIN="${E2E_CORS_ORIGIN:-https://app.e2e.local}"
AIX_NAME="support-e2e-ext"; AIX_DISPLAY="상담 에이전트 외부 노출"
# 13(f) 의 allowed_groups 에 적는 ops 사람의 그룹. local 은 테넌트 e2e-ops 의 고정 그룹 daegam-<tenant> 이고(P32),
# oidc 는 mock-oidc 가 토큰에 그대로 싣는 이름이다
OPS_TENANT="e2e-ops"
if [ "$AUTH_MODE" = "local" ]; then OPS_GROUP="daegam-$OPS_TENANT"; else OPS_GROUP="ops"; fi
# STEP 12 가 삭제 전에 뜨는 복제본. 한 문서는 배포를 하나만 가지므로 노출용 둘째는 복제본이다 (D42)
AIX_ID=""
# --- 스텁에게 보내는 말과 P35 단계 (D65) ---
# 스텁은 메시지 안 JSON 조각의 도구를 부르고 {"sleep": n} 이면 n 초 기다린 뒤 답하며, {"after": n} 이면 도구 결과를 받은
# 뒤 n 초 기다려 답한다(runtime/stub.py). 기다리는 동안이 중지(7e·10m·13(h))를 거는 진행 중 턴이고, 끊김(7c·13(h))은
# DETACHED_MSG 턴이 끝까지 가는지 본다
ADD_MSG='{"tool":"Add","args":{"a":3,"b":4}}'
DIVIDE_MSG='{"tool":"Divide","args":{"numerator":8,"denominator":2}}'
SLEEP_MSG='{"sleep": 20}'
EDIT_ARGS='{"numerator": 9, "denominator": 3}'   # 8e 가 Divide 보류를 고치는 인자. 결과는 3.0 이다
EDIT_RESULT="3.0"
CUT_AFTER=3            # 끊기 전에 턴이 도는 초
CANCEL_AFTER=2         # 중지를 부르기 전에 턴이 도는 초
CANCEL_END_WITHIN=3    # 중지 뒤 스트림이 turn_cancelled 로 끝나야 하는 초
# 끊어도 끝까지 가는 턴 (D92, P78). 스텁이 20초 기다린 뒤 Add(5,6) 을 부르고 결과를 받은 뒤 5초 더 기다려 답한다
DETACHED_MSG='{"sleep": 20, "tool": "Add", "args": {"a": 5, "b": 6}, "after": 5}'
DETACHED_RESULT=11
DETACHED_END_WITHIN=40   # 끊은 뒤 그 턴이 끝나야(running 이 거짓) 하는 초
# 파드 종료 (10d, P78 5절). Advanced 덮어쓰기로 grace 를 낮추면 drain 은 grace − 60(preStop 5, 마감 45, 여유 10)이다.
# 지켜보는 짧은 턴은 drain 안에 done 으로 끝나고, Add 뒤 75초 기다리는 긴 턴은 drain 끝에 turn_aborted 로 마감한다.
# 짧은 턴은 파드를 찾는 10초와 preStop 5초보다 길어 SIGTERM 뒤에 끝나고 drain(30초)보다 짧다. 그래야 drain 이 읽는 사람을
# 붙잡아 둔 것을 본다
DRAIN_GRACE=90
DRAIN_SECONDS=30
DRAIN_PRESTOP_MS=5000   # 파드의 preStop (platform/daegam/k8s/templates/agent.py PRESTOP_SECONDS)
DRAIN_SHORT_MSG='{"sleep": 20}'
DRAIN_LONG_MSG='{"tool": "Add", "args": {"a": 2, "b": 9}, "after": 75}'
DRAIN_LONG_RESULT=11
ABORTED="turn_aborted"   # 파드 종료가 멈춘 턴의 오류 코드 (runtime/cancel.py)
# 죽은 파드의 턴 (10k, P79). 도구 결과를 받은 뒤 60초 기다리는 턴이다. Redis 는 Divide 를 승인해 재개를 죽인다
KILL_ADD_MSG='{"tool": "Add", "args": {"a": 4, "b": 5}, "after": 60}'
KILL_DIVIDE_MSG='{"tool": "Divide", "args": {"numerator": 6, "denominator": 3}, "after": 60}'
KILL_CARD_WITHIN=30     # 턴을 연 뒤 결과를 받은 카드가 가벼운 읽기에 보여야 하는 초
KILL_STILL_RUNNING=30   # 지운 뒤 세션이 아직 running 이어야 하는 초. drain 이 그 턴을 마감하지 않았다는 뜻이다
# 지운 뒤 running 이 거짓이어야 하는 초. 리스 60초에 SIGKILL 까지 2초, 날아가던 갱신 하나가 늘린 몫, 폴링 한 번과 여유를 더했다
KILL_FREE_WITHIN=90
DELETE_END_WITHIN=30     # 삭제를 부른 뒤 도는 턴이 turn_cancelled 로 끝나야 하는 초 (12)
SETTLE_SECONDS=3       # 재배포가 적용한 뒤 Deployment 컨트롤러가 새 ReplicaSet 을 만들 틈 (14)
MCP_MODERN="2026-07-28"               # 런타임이 우리 MCP Server·Gateway 와 맺어야 하는 MCP 규약 판 (5g·7)
CANCEL_FEATURE="cancel"               # 중지 API 의 런타임 능력 이름 (runtime/features.py)
INIT_CONTAINER="fetch-agent-config"   # Agent 파드의 init 컨테이너 (k8s/templates/agent.py)
READY_PROBE_PATH="/healthz?probe=ready"
POD_COUNT=2            # 10m 이 STEP 10 의 재배포에 싣는 replicas
GW_NAME="calc-gw-agent-e2e"; GW_AG_NAME="gw-agent-e2e"; GW_AG_DISPLAY="게이트웨이 소스 에이전트 e2e"
SAFE_AG_NAME="safety-agent-e2e"; SAFE_AG_DISPLAY="바깥 내용 승인 에이전트 e2e"; SAFE_SUB="calc"   # 8s (P43)
JUDGE_AG_NAME="judge-agent-e2e"; JUDGE_AG_DISPLAY="판정 모델 에이전트 e2e"   # 8j (P43 7절)
JUDGE_MSG='{"tool":"Add","args":{"a":31000,"b":337}}'   # 결과 31337 을 스텁 판정 모델이 주입으로 본다(runtime/stub.py)
# --- 턴 위임 토큰 (P53). 1t·5g 가 위임이 열리게 바꾼 것은 EXIT 트랩이 되돌린다 ---
TURN_TTL=60            # local 의 DAEGAM_TURN_TOKEN_TTL_SECONDS. 가장 짧은 수명이다
TURN_SLEEP=75          # 7t 의 턴이 Add 를 부르기 전에 기다리는 초. 수명보다 길고 턴 타임아웃(120초)보다 짧다
LONG_ADD_MSG="{\"sleep\": $TURN_SLEEP, \"tool\": \"Add\", \"args\": {\"a\": 5, \"b\": 6}}"
ACCESS_LOST="turn_access_lost"      # 접근을 잃은 사람의 턴이 멈춘 오류 코드 (runtime/cancel.py, P78 7절)
APPROVER_NAME="approver-e2e"         # local: $TENANT 의 developer 라 agent:approve 를 쥔 승인자 (8t·8u·8v)
TURN_GROUP="agent-e2e-developers"    # oidc: $TENANT 의 developer 로 매핑해 관리자 토큰에 싣는 그룹
# oidc 의 Gateway 소스가 쓰는 mock-oidc 의 client credentials (deploy/dev/mock-oidc.yaml). 비밀은 개발용 고정값이다
OAUTH_CLIENT_ID="agent-dev"; OAUTH_CLIENT_SECRET="agent-dev-secret"; OAUTH_SECRET="agent-oauth-e2e"
LAB_ACTOR="svc:agent:e2e-lab"        # 실험 토큰의 act
VMCP_PORT="${E2E_VMCP_PORT:-$PORT_AGENT_VMCP}"
# 11r 이 규칙을 둔 MCP Server 의 프록시에 붙는 port-forward (P52). 이름 대지 않은 Agent 의 신원과 바꾼 Tool 이름이다
RULES_PORT="${E2E_RULES_PORT:-$PORT_AGENT_RULES}"
RULES_STRANGER="svc:agent:agent-e2e-unnamed"; RULES_RENAMED="add_numbers"
RULES_OUTSIDER="e2e-rules-outsider"   # 11r 의 위임 토큰에 싣는, 어느 테넌트의 멤버도 아닌 사람
RULES_SET=0               # 11r 이 STEP 3 의 MCP Server 에 규칙을 둔 동안 1 이다. EXIT 트랩이 걷는다
ADMIN_SUB=""; APPROVER_SUB=""; APPROVER_TOKEN=""; TURN_GW_ID=""; TURN_MCP_GROUP=""
TURN_ROLE_BEFORE=""    # 1t 전 관리자의 $TENANT manual 역할 (lib/e2e_turn_tokens.sh turn_member_developer)
TURN_MEMBER_SET=0; APPROVER_ADDED=0; TENANT_PATCHED=0; OAUTH_SECRET_NS=""
KEY_PROVIDER_A="openai-e2e-a"; KEY_PROVIDER_B="openai-e2e-b"; KEY_MODEL="gpt-4o-mini"
# provider 이름에서 나온 키 env 이름 (P35 12절). 두 provider 는 종류가 같아도 env 가 다르다
KEY_ENV_A="DAEGAM_LLM_OPENAI_E2E_A_API_KEY"; KEY_ENV_B="DAEGAM_LLM_OPENAI_E2E_B_API_KEY"
KEY_A="sk-e2e-a-not-a-real-key"; KEY_B="sk-e2e-b-not-a-real-key"; KEY_B_NEW="sk-e2e-b-rotated-not-a-real-key"
KEY_AG_NAME="keys-agent-e2e"; KEY_AG_DISPLAY="키 두 벌 에이전트 e2e"
# STEP 5g·14 가 만든 것. 그 단계가 끝에서 걷고 비우며, 도중에 실패하면 EXIT 트랩이 걷는다
GW_AG_ID=""; KEY_AG_ID=""; KEY_PROVIDERS_ADDED=0
SAFE_AG_ID=""; SAFE_SIDS=""   # 8s 의 Agent 와 그 세션. 8s 가 끝에서 걷고, 도중에 실패하면 EXIT 트랩이 걷는다
JUDGE_AG_ID=""   # 8j 의 Agent. 8j 가 끝에서 걷고, 도중에 실패하면 EXIT 트랩이 걷는다
SAFE_CHOICES=""; SAFE_CHOICE_IDS=""   # 8s 가 읽은 그 Agent 에 대한 내 기억한 선택("Tool:선택")과 그 id
BG_PID=""                 # 뒤에서 SSE 를 받는 curl (sse_bg)
DRAIN_W_PID=""; DRAIN_L_PID=""   # 10d 가 뒤에서 여는 두 턴의 curl (sse_bg_as)
# 8e·9r·10m·10k·10d·12 가 연 세션. 12r 이 이 실행의 세션 id 로 Redis 를 치운다
SIDE=""; ROUTE_SID=""; SIDP=""; SIDK=""; SIDW=""; SIDL=""; SIDX=""
WORK="$(mktemp -d "${TMPDIR:-/tmp}/daegam-e2e-agent.XXXXXX")"
K=(kubectl --context "$CTX")
PF_API=""; PF_OIDC=""; PF_RT=""; PF_VMCP=""; PF_RULES=""
ADMIN_TOKEN=""; AUTH=()   # oidc 모드면 Authorization 헤더 (빈 배열은 ${AUTH[@]+"${AUTH[@]}"} 로 편다. bash 3.2)

# start_pf·stop_pf·wait_port 와 "왜 재연결 루프인가" 는 여기에 있다 (K·WORK 를 쓴다)
. "$HERE/lib/e2e_pf.sh"
kill_stale_pf "$API_PORT" "$OIDC_PORT" "$RT_PORT" "$VMCP_PORT" "$RULES_PORT"
# roll_control_plane: 인증 모드를 바꾸면 플랫폼과 UI 를 함께 굴린다(UI 의 CSP 가 기동 때 모드를 읽는다, D52)
. "$HERE/lib/e2e_control_plane.sh"

# switch_auth_mode·restore_auth_mode·wait_auth_reapplied 는 여기에 있다 (STEP 0 이 바꾸고 cleanup 이 되돌린다).
# 되돌린 뒤의 기다림은 API port-forward 로 하므로 cleanup 은 그것을 닫기 전에 되돌린다
. "$HERE/lib/e2e_auth_mode.sh"

# STEP 2 가 넣은 stub provider 를 걷는다. 실측 전부터 있었으면(lib/e2e_api.sh 의 STUB_PROVIDER_ADDED=0) 손대지 않는다.
# 그 값은 lib 를 읽기 전의 트랩에서도 0 으로 읽는다
remove_stub_provider() {
  [ "${STUB_PROVIDER_ADDED:-0}" = 1 ] || return 0
  STUB_PROVIDER_ADDED=0
  remove_providers "$STUB_PROVIDER"
}

# STEP 5g 가 만든 Agent·Gateway·게시를 걷는다. Agent 가 Gateway 를, Gateway 가 게시 항목을 붙드므로 이 차례다. 게시는
# 이 단계가 한 것이라 되돌린다. CALC_* 는 lib/e2e_calc.sh 가 두므로 그것을 읽기 전의 트랩에서도 빈 값으로 읽는다
drop_gateway_source() {
  [ -z "$GW_AG_ID" ] || api_delete "/agents/$GW_AG_ID"
  [ -z "${CALC_GW_ID:-}" ] || api_delete "/gateways/$CALC_GW_ID"
  [ -z "${CALC_HUB_ID:-}" ] || api_delete "/mcp-servers/$CALC_MS_ID/publish"
  GW_AG_ID=""; CALC_GW_ID=""; CALC_HUB_ID=""
}

# STEP 8s 가 만든 Agent 를 걷는다. 그 Agent 의 기억한 선택은 삭제가 지운다. Redis 는 Agent 삭제가 키를 남기므로(D33) 그
# Agent 와 세션의 키를 걷는다. rforget 은 lib/e2e_lab.sh 의 것이고 SAFE_AG_ID 는 그것을 읽은 뒤에만 선다
drop_safety_agent() {
  [ -n "$SAFE_AG_ID" ] || return 0
  api_delete "/agents/$SAFE_AG_ID"
  # shellcheck disable=SC2086  # 세션 id 여럿을 낱말로 나눠 넘긴다
  [ "$MEMORY_KIND" != redis ] || rforget "$SAFE_AG_ID" $SAFE_SIDS
  SAFE_AG_ID=""; SAFE_SIDS=""
}

# STEP 8j 가 만든 Agent 를 걷는다. memory 가 없어 남는 키가 없다
drop_judge_agent() {
  [ -z "$JUDGE_AG_ID" ] || api_delete "/agents/$JUDGE_AG_ID"
  JUDGE_AG_ID=""
}

# STEP 14 가 만든 Agent 와 provider 둘을 걷는다. provider 를 쓰는 Agent 가 먼저다
drop_key_agent() {
  [ -z "$KEY_AG_ID" ] || api_delete "/agents/$KEY_AG_ID"
  KEY_AG_ID=""
  [ "$KEY_PROVIDERS_ADDED" = 1 ] || return 0
  KEY_PROVIDERS_ADDED=0
  remove_providers "$KEY_PROVIDER_A" "$KEY_PROVIDER_B"
}

# 1t·5g 가 위임이 열리게 바꾼 것을 되돌린다 (P53). 관리자의 $TENANT 소속은 1t 전의 역할로(없었으면 뺀다), 승인자는
# 지우고, oidc 의 그룹 매핑과 Tenant Settings(agent_oauth·agent_client_ids·mcp_group)는 받아 둔 원래 값으로, Secret 은
# 지운다. 실패해도 멈추지 않고 사람이 할 일을 찍는다
drop_turn_setup() {
  local body code
  # turn_member_restore 는 lib/e2e_turn_tokens.sh 의 것이다. 표시는 그것을 읽은 뒤에만 선다
  [ "$TURN_MEMBER_SET" != 1 ] || turn_member_restore || true
  if [ "$APPROVER_ADDED" = 1 ]; then APPROVER_ADDED=0; api_delete "/users/$APPROVER_SUB"; fi
  # MAPPINGS_PUT 은 lib/e2e_group_mappings.sh 가 둔다. 그것을 읽기 전의 트랩에서도 0 으로 읽는다
  [ "${MAPPINGS_PUT:-0}" != 1 ] || group_mappings_restore
  if [ "$TENANT_PATCHED" = 1 ]; then
    TENANT_PATCHED=0
    code=""
    body="$(json_of "$WORK/tenant-settings.orig.json" 'json.dumps({k: d.get(k) for k in ("agent_oauth", "agent_client_ids", "mcp_group")})' 2>/dev/null)" \
      && code="$(api PATCH "/tenants/$TENANT/settings" "$body")"
    [ "$code" = 200 ] || echo "!! tenant settings restore -> ${code:-no payload}. PATCH $API/tenants/$TENANT/settings 로 agent_oauth·agent_client_ids·mcp_group 을 되돌린다"
  fi
  if [ -n "$OAUTH_SECRET_NS" ]; then
    "${K[@]}" -n "$OAUTH_SECRET_NS" delete secret "$OAUTH_SECRET" --ignore-not-found >/dev/null 2>&1 \
      || echo "!! kubectl --context $CTX -n $OAUTH_SECRET_NS delete secret $OAUTH_SECRET"
    OAUTH_SECRET_NS=""
  fi
}

# 11r 이 STEP 3 의 MCP Server 에 둔 규칙과 오버라이드를 걷는다. 다음 실행이 규칙 없는 서버에서 시작하게 한다. 실패하면
# 사람이 할 일을 찍는다
drop_tool_rules() {
  [ "$RULES_SET" = 1 ] || return 0
  RULES_SET=0
  if [ "$(api GET "/mcp-servers/$MS_ID")" = 200 ] && cp "$WORK/body" "$WORK/rules-server.json" \
    && python3 "$HERE/lib/rules_body.py" "$WORK/rules-server.json" '{}' --overrides '{}' >"$WORK/rules-body.json" \
    && [ "$(api PUT "/mcp-servers/$MS_ID" "$(cat "$WORK/rules-body.json")")" = 200 ] \
    && [ "$(api POST "/mcp-servers/$MS_ID/redeploy")" = 200 ]; then
    return 0
  fi
  echo "!! remove the Tool rules of MCP Server $MS_ID ($MS_NAME) and redeploy it: $(cat "$WORK/body")"
}

SA_ID=""   # 13(s) 의 서비스 어카운트(local)
cleanup() {
  for pid in "$BG_PID" "$DRAIN_W_PID" "$DRAIN_L_PID"; do
    [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
  done
  # port-forward 를 닫기 전에, oidc 를 header 로 되돌리기 전에 한다 (지금 열린 경로로만 API 에 닿는다). 5g 의 Agent 가
  # stub provider 를 쓰므로 그것을 걷기 전이다
  drop_gateway_source
  drop_safety_agent
  drop_judge_agent
  drop_key_agent
  drop_tool_rules
  drop_turn_setup
  # 13(s) 의 서비스 어카운트. 그 단계가 끝에서 지우고, 도중에 끝났으면 여기서 지운다
  [ -z "$SA_ID" ] || api_delete "/tenants/$TENANT/service-accounts/$SA_ID"
  remove_stub_provider
  # 되돌린 뒤 재적용을 기다리는 호출도 API port-forward 로 간다. 재연결 루프가 새 파드로 다시 붙는다. 앞 모드가
  # oidc·local 이면 상태 API 에 토큰이 들므로 받아 둔 관리자 토큰을 넘긴다
  restore_auth_mode "$ADMIN_TOKEN"
  stop_pf "$PF_API" "$API_PORT"
  stop_pf "$PF_OIDC" "$OIDC_PORT"
  stop_pf "$PF_RT" "$RT_PORT"
  stop_pf "$PF_VMCP" "$VMCP_PORT"
  stop_pf "$PF_RULES" "$RULES_PORT"
  rm -rf "$WORK"
}
on_exit cleanup

# pass·skip·cond_skip·record_fail·die·finish 와 종료 코드 계약은 여기에 있다
. "$HERE/lib/e2e_report.sh"

# api·jq_·find_id·listed_id·api_delete·wait_ready·hdr·secret_field·save_doc·deploy_ready(STEP 3·5g·14)·
# register_data_service(STEP 4)·ensure_stub_provider(STEP 2)·remove_providers·put_provider_key(STEP 14)는 여기에 있다.
# oidc·local 모드면 AUTH 의 Bearer 가 붙는다. mock-oidc 토큰(user_token)은 lib/e2e_auth_mode.sh 에 있다
. "$HERE/lib/e2e_api.sh"
# chat·sse_·msg 는 여기에 있다
. "$HERE/lib/e2e_sse.sh"
# lab 안을 들여다보는 psql_·rcli·rdel 과 lab 의 호스트·Secret·RediSearch 인덱스 이름(LAB_*)은 여기에 있다
. "$HERE/lib/e2e_lab.sh"
# calc_backend(STEP 3)·calc_publish·calc_gateway(STEP 5g)는 여기에 있다
. "$HERE/lib/e2e_calc.sh"

# -- local 인증 모드 (D40). 플랫폼이 발급자다. 토큰은 로그인으로 받고 첫 로그인은 서버가 비밀번호 바꾸기를 강제한다.
# local_login_any·local_user_token·local_user_id 와 비밀번호 셋(LOCAL_BOOT_PW 는 STEP 0 이 Secret 에 넣는다)은 여기에 있다
. "$HERE/lib/e2e_local_auth.sh"
. "$HERE/lib/e2e_service_accounts.sh"
# 실험 토큰을 서명하는 svc_token 과 lab 클라이언트로 부르는 mcp·mcp_admits·mcp_refuses 는 여기에 있다 (WORK 를 쓴다)
# shellcheck disable=SC2034  # lib/mcp_rpc.sh 가 initialize 의 clientInfo.name 으로 읽는다
MCP_CLIENT_NAME="e2e-agent"
. "$HERE/lib/mcp_rpc.sh"
# 턴 위임 토큰(P53)의 cluster_now·turn_window·platform_mongo_here·turn_grants_of·human_token·wait_gateway_gate·
# audit_expect 는 여기에 있다
. "$HERE/lib/e2e_turn_tokens.sh"
# Tool 규칙(P52, 11r)의 rules_save·rules_redeploy·rules_settled·rules_rolled·tool_names·call_refused 는 여기에 있다
. "$HERE/lib/e2e_tool_rules.sh"
# oidc 의 group_mappings_add(1t 가 더한다)·group_mappings_restore(EXIT 트랩이 되돌린다)는 여기에 있다
. "$HERE/lib/e2e_group_mappings.sh"
# 데이터 플레인 토큰 (aud daegam-mcp). USER TENANT [OIDC_GROUP] -> local 은 사람을 TENANT 의 viewer 로 넣고 로그인해 받으므로
# groups 가 [daegam-<TENANT>] 이고, oidc 는 mock-oidc 가 OIDC_GROUP(없으면 빈 목록)을 groups 에 싣는다. F6 부터 이 토큰은
# 15분이라 배포와 Ready 대기를 지나지 않도록 쓰기 직전에 받는다
dp_token() {
  if [ "$AUTH_MODE" = "local" ]; then local_user_token "$1" "$2"; return; fi
  local groups="[]"; [ -z "${3:-}" ] || groups="[\"$3\"]"
  user_token "{\"sub\": \"$1\", \"groups\": $groups}"
}
# 외부 경로 본문에 user_sub 를 넣어 본다. 런타임은 토큰 sub 를 쓰고 이 값을 무시해야 한다 (D27)
msg_sub() { python3 -c 'import json,sys; print(json.dumps({"message": sys.argv[1], "user_sub": sys.argv[2]}, ensure_ascii=False))' "$1" "$2"; }
# edit_body NAME ARGS_JSON -> 보류 하나를 NAME·ARGS 로 고치는 결정 본문 (8e)
edit_body() { python3 -c 'import json,sys; print(json.dumps({"decisions": [{"type": "edit", "edited_action": {"name": sys.argv[1], "args": json.loads(sys.argv[2])}}]}))' "$1" "$2"; }
# 장기 기억(D34) 턴의 본문: 스텁에게 파일 도구를 시킨다. 내장 도구는 이름을 직접 대야 부른다(runtime/stub.py).
# mem_msg [--ephemeral] TOOL KEY VALUE [KEY VALUE ...]
mem_msg() {
  python3 -c '
import json, sys
argv = sys.argv[1:]
ephemeral = bool(argv) and argv[0] == "--ephemeral"
if ephemeral:
    argv = argv[1:]
tool, rest = argv[0], argv[1:]
fragment = json.dumps({"tool": tool, "args": dict(zip(rest[0::2], rest[1::2]))}, ensure_ascii=False)
body = {"message": "이렇게 해라 " + fragment}
if ephemeral:
    body["ephemeral"] = True
print(json.dumps(body, ensure_ascii=False))
' "$@"
}

# 템플릿에서 Agent 저장 본문을 만드는 것은 lib/agent_body.py 다 (STEP 5·5g·14)

# --- 진행 중 턴 (7c·7e·10m·13(h)). 스텁의 기다리는 턴을 뒤에서 열어 두고 그 사이에 끊거나 멈춘다 ------------
# ext_bg TOKEN JSON: Traefik 을 지나 사용자 토큰으로 여는 sse_bg(lib/e2e_sse.sh)
ext_bg() { sse_bg "$EXT_URL/chat" "$2" --resolve "$EXT_HOST:80:$LB_IP" -H "Authorization: Bearer $1"; }
# cut_detached STEP READER CMD... -> 뒤에서 도는 $DETACHED_MSG 턴을 CUT_AFTER 초 뒤 끊는다. 턴은 끊겨도 끝까지 가므로(D92)
# 곧바로 연 같은 세션의 다음 턴 CMD(chat·chat_ext)는 409 turn_in_progress 여야 한다. 그 뒤 READER(세션을 읽어 상태 코드를 찍고
# 본문을 $WORK/body 에 남기는 함수)로 0.5초마다 보아 running 인 동안 결과를 받은 Add 카드가 전사에 있고, 끊은 뒤
# DETACHED_END_WITHIN 초 안에 running 이 거짓이 되며 전사가 done 으로 끝나야 한다. 아니면 die STEP 이다. 끊은 뒤 결과가 보인
# 때와 끝난 때(ms)를 SAVED_AFTER_MS·DONE_AFTER_MS 에, 그 시도의 id(응답 머리 X-Daegam-Turn-Id)를 CUT_TURN_ID 에 남긴다
cut_detached() {
  local step="$1" reader="$2" code cut_ms now running saved last seen=""; shift 2
  sleep "$CUT_AFTER"
  kill -0 "$BG_PID" 2>/dev/null || { cp "$WORK/bg.sse" "$WORK/body"; die "$step" "the $DETACHED_MSG turn ended before the cut"; }
  CUT_TURN_ID="$(hdr x-daegam-turn-id "$WORK/bg.hdr")"
  CUT_GRANT_ID="$(hdr x-daegam-turn-grant "$WORK/bg.hdr")"
  [ "$(hdr x-daegam-detached-turn "$WORK/bg.hdr")" = 1 ] || die "$step" "the response does not say the turn is detached: $(cat "$WORK/bg.hdr")"
  bg_stop
  cut_ms="$(now_ms)"
  code="$("$@")"
  { [ "$code" = 409 ] && grep -q turn_in_progress "$WORK/sse"; } \
    || { cp "$WORK/sse" "$WORK/body"; die "$step" "the next turn right after the cut -> $code (expected 409 turn_in_progress: the cut turn runs on)"; }
  SAVED_AFTER_MS=""; DONE_AFTER_MS=""
  while :; do
    code="$("$reader")"
    now="$(now_ms)"
    [ "$code" = 200 ] || die "$step" "reading the session after the cut -> $code"
    read -r running saved last <<<"$(jq_ "bool(d['session'].get('running')), any(i['kind'] == 'tool_call' and '$DETACHED_RESULT' in str((i.get('result') or {}).get('content')) for i in (d.get('transcript') or {}).get('items') or []), (((d.get('transcript') or {}).get('items') or [{}])[-1]).get('kind')")"
    if [ "$running" = True ]; then
      seen=1
      [ -n "$SAVED_AFTER_MS" ] || [ "$saved" != True ] || SAVED_AFTER_MS=$((now - cut_ms))
    else
      [ -n "$seen" ] || die "$step" "the session did not read running after the cut"
      [ -n "$SAVED_AFTER_MS" ] || die "$step" "the saved Add result did not show before the cut turn ended"
      [ "$last" = "done" ] || die "$step" "the cut turn ended with '$last' in the transcript (expected done)"
      DONE_AFTER_MS=$((now - cut_ms))
      return 0
    fi
    [ "$now" -lt "$((cut_ms + DETACHED_END_WITHIN * 1000))" ] || die "$step" "the cut turn still runs ${DETACHED_END_WITHIN}s after the cut"
    sleep 0.5
  done
}
# agent_pods NS ID 는 lib/e2e_control_plane.sh 에 있다
# rolled_out NS DEPLOY ID -> 롤아웃이 끝나고(옛 ReplicaSet 이 0) 내려가는 파드도 다 지워질 때까지 기다린다. phase Ready 는
# 최신 파드가 원하는 수만큼 준비됐다는 뜻이라 옛 파드가 아직 옛 설정으로 답할 수 있다. rollout status 는 옛 파드가 preStop 으로
# 내려가는 동안에도 끝나고 그 사이 Service 는 옛 파드에도 요청을 보내므로 둘 다 기다린다
rolled_out() {
  "${K[@]}" -n "$1" rollout status "deploy/$2" --timeout="${READY_TIMEOUT}s" >/dev/null \
    && wait_pods_settled "$1" "$AGENT_POD_SELECTOR$3"
}
# log_events EVENT SID EXPR -> 이 Agent 파드마다 agent 컨테이너 로그에서 그 세션의 EVENT 줄을 JSON 으로 읽어 d(줄)와
# pod(파드 이름)로 EXPR 을 줄마다 찍는다 (runtime/events.py 의 `{"daegam_event": ...}` 한 줄)
log_events() {
  local event="$1" sid="$2" expr="$3" pod
  agent_pods "$AG_NS" "$AG_ID" | while read -r pod; do
    "${K[@]}" -n "$AG_NS" logs "$pod" -c agent </dev/null 2>/dev/null | python3 -c 'import json, sys
pod, event, sid, expr = sys.argv[1:5]
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if isinstance(d, dict) and d.get("daegam_event") == event and d.get("session_id") == sid:
        print(eval(expr))' "$pod" "$event" "$sid" "$expr"
  done
}
# all_modern -> $WORK/body(GET /metrics)의 tools.protocols 가 비지 않았고 소스마다 MCP_MODERN 이면 True (P35 11절)
all_modern() { jq_ "bool(d['tools']['protocols']) and set(d['tools']['protocols'].values()) == {'$MCP_MODERN'}"; }

# --- 턴 위임 토큰 (P53). 1t·5g·7t·8t·8u·8v·9t·13(t) ---------------------------------------------------------
# chat_as TOKEN PATH JSON: 그 사람의 토큰으로 여는 chat (8t·8u·8v 의 승인자). chat 이 읽는 AUTH 를 이 호출 동안만 바꾼다
chat_as() { local AUTH=(-H "Authorization: Bearer $1"); shift; chat "$@"; }
# admin_member ROLE: 관리자의 $TENANT manual 소속을 ROLE 로 둔다. 비면 뺀다. 7t·8u 가 사람이 테넌트에서 빠진 경우를 만든다
admin_member() { local_set_member "$TENANT" "$ADMIN_SUB" "$1"; }
# agent_oauth_on STEP: oidc 의 Gateway 소스가 쓸 client credentials 를 둔다. mock-oidc 의 agent-dev 비밀을 테넌트
# 네임스페이스(STEP 3 의 MCP Server 가 뜬 곳)의 Secret 에 넣고 Tenant Settings 에 agent_oauth 를 두며 agent_client_ids 에
# agent-dev 를, mcp_group 이 비었으면 mcp-$TENANT 를 더한다. 원래 값은 EXIT 트랩이 되돌린다. TURN_MCP_GROUP 을 남긴다
agent_oauth_on() {
  local step="$1" code body
  code="$(api GET "/mcp-servers/$MS_ID")"
  [ "$code" = 200 ] || die "$step" "GET /mcp-servers/$MS_ID -> $code"
  OAUTH_SECRET_NS="$(jq_ 'd["k8s"]["namespace"]')"
  "${K[@]}" -n "$OAUTH_SECRET_NS" create secret generic "$OAUTH_SECRET" --from-literal=client_secret="$OAUTH_CLIENT_SECRET" \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null || die "$step" "Secret $OAUTH_SECRET_NS/$OAUTH_SECRET could not be applied"
  code="$(api GET "/tenants/$TENANT/settings")"
  [ "$code" = 200 ] || die "$step" "GET /tenants/$TENANT/settings -> $code"
  cp "$WORK/body" "$WORK/tenant-settings.orig.json"
  body="$(python3 -c 'import json, sys
s, tenant, cid, secret, token_url = json.load(open(sys.argv[1])), *sys.argv[2:6]
print(json.dumps({"agent_oauth": {"client_id": cid, "secret_name": secret, "token_url": token_url, "scope": "mcp.invoke"},
                  "agent_client_ids": sorted(set(s.get("agent_client_ids") or []) | {cid}),
                  "mcp_group": s.get("mcp_group") or "mcp-" + tenant}))' "$WORK/tenant-settings.orig.json" "$TENANT" \
    "$OAUTH_CLIENT_ID" "$OAUTH_SECRET" "http://mock-oidc.$PLATFORM_NS.svc:8080/token")" || die "$step" "building the tenant settings patch failed"
  TENANT_PATCHED=1
  code="$(api PATCH "/tenants/$TENANT/settings" "$body")"
  [ "$code" = 200 ] || die "$step" "PATCH /tenants/$TENANT/settings (agent_oauth) -> $code $(cat "$WORK/body")"
  TURN_MCP_GROUP="$(jq_ 'd["mcp_group"]')"
}
# pending_divide STEP: 관리자가 새 세션에 Divide 를 보내 승인 보류를 만들고 그 세션 id 를 PENDING_SID 에 남긴다
pending_divide() {
  local code
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$DIVIDE_MSG")")"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "interrupt" ]; } || { cp "$WORK/sse" "$WORK/body"; die "$1" "Divide chat -> $code $(sse_ 'names')"; }
  PENDING_SID="$(sse_ 'first("interrupt")["session_id"]')"
}
# divide_done CODE: 결정의 재개가 200 이고 Divide 를 성공시킨 뒤 done 으로 끝났다 ($WORK/sse)
divide_done() {
  [ "$1" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] \
    && [ "$(sse_ 'first("tool_result")["name"], first("tool_result")["status"]')" = "Divide success" ]
}
# resume_grant STEP SID EXPR: 그 세션의 마지막 위임 기록(결정의 재개)이 trigger decide 이고 g(그 기록)로 EXPR 이 True 여야
# 한다. GRANT_NOTE 에 그 기록의 사람 칸을 남긴다. 클러스터 안 MongoDB 가 없으면 보지 않고 그렇게 적는다
resume_grant() {
  if ! platform_mongo_here; then GRANT_NOTE="grant record not read (no MongoDB pod in $PLATFORM_NS)"; return 0; fi
  turn_grants_of "$AG_ID" "$2" "$WORK/grants.json" || die "$1" "reading the turn grants of session $2 failed"
  GRANT_NOTE="$(json_of "$WORK/grants.json" 'json.dumps({k: d[-1].get(k) for k in ("trigger", "sub", "decided_by", "session_owner")} if d else None)')"
  [ "$(json_of "$WORK/grants.json" "bool(d) and (lambda g: g.get('trigger') == 'decide' and ($3))(d[-1])")" = True ] \
    || die "$1" "the resume's grant in session $2: $GRANT_NOTE"
}

# --- 외부 경로 (STEP 13). Traefik LoadBalancer IP 에 host 를 못 박는다 (e2e_gateway.sh 와 같은 방식) -------
LB_IP=""; EXT_URL="http://$EXT_HOST"
# ext METHOD PATH TOKEN JSON [extra curl args...] -> 상태 코드. 본문 $WORK/body, 응답 헤더 $WORK/hdr. 빈 값은 "" 로 준다
ext() {
  local method="$1" path="$2" token="$3" body="$4"; shift 4
  local auth=() data=()
  [ -n "$token" ] && auth=(-H "Authorization: Bearer $token")
  [ -n "$body" ] && data=(-H 'Content-Type: application/json' -d "$body")
  curl -sS -o "$WORK/body" -D "$WORK/hdr" -w '%{http_code}' --resolve "$EXT_HOST:80:$LB_IP" --max-time 30 \
    -X "$method" "$EXT_URL$path" ${auth[@]+"${auth[@]}"} ${data[@]+"${data[@]}"} "$@" 2>/dev/null || echo 000
}
# exposure_json '<allowed_groups json>' -> deploy·redeploy 본문의 exposure 객체
exposure_json() {
  python3 -c '
import json, sys
print(json.dumps({"enabled": True, "host": sys.argv[1], "ingress_class": sys.argv[2], "tls": {"mode": "none"},
  "cors_origins": [sys.argv[3]], "allowed_groups": json.loads(sys.argv[4])}))
' "$EXT_HOST" "$INGRESS_CLASS" "$CORS_ORIGIN" "${1:-[]}"
}
# 배포 본문은 이름을 받지 않는다. 이름은 저장할 때 정해졌다 (D42)
deploy_body() { # '<allowed_groups json>'
  python3 -c '
import json, sys
print(json.dumps({"cluster_id": sys.argv[1], "exposure": json.loads(sys.argv[2])}))
' "$HOME_ID" "$(exposure_json "${1:-[]}")"
}
# chat_ext TOKEN PATH JSON [extra curl args...] -> 상태 코드. curl -N 의 출력을 파이프에서 읽으며 청크마다 도착 시각을
# 적는다 (증분 SSE 실측). SSE 본문은 $WORK/sse, 응답 머리는 $WORK/hdr, 시각은 $WORK/sse.times (JSON: events[[name, ms]],
# first_byte_ms, eof_ms, chunks, curl_starttransfer_ms, curl_total_ms. ms 는 요청 시작 기준). -w 의 통계는 본문 뒤에
# \x1e 로 구분해 붙는다
cat >"$WORK/sse_reader.py" <<'PY'
import json, os, sys, time
out_body, out_times = sys.argv[1], sys.argv[2]
t0 = time.monotonic()
chunks = []  # (arrival ms, cumulative end offset)
buf = bytearray()
while True:
    data = os.read(0, 65536)
    if not data:
        break
    buf += data
    chunks.append(((time.monotonic() - t0) * 1000, len(buf)))
eof_ms = (time.monotonic() - t0) * 1000
raw = bytes(buf)
body, sep, stats = raw.rpartition(b"\x1e")
if not sep:
    body, stats = raw, b"000 0 0"
if body.endswith(b"\n"):
    body = body[:-1]  # -w 포맷이 앞에 붙인 개행
parts = stats.decode("utf-8", "replace").split()
code = parts[0] if parts else "000"
start_ms = float(parts[1]) * 1000 if len(parts) > 1 else 0.0
total_ms = float(parts[2]) * 1000 if len(parts) > 2 else 0.0
events, offset = [], 0
for line in body.split(b"\n"):
    end = offset + len(line) + 1
    if line.startswith(b"event: "):
        t = next((t for t, e in chunks if e >= end), eof_ms)
        events.append([line[7:].decode("utf-8", "replace"), round(t, 1)])
    offset = end
prev, body_chunks = 0, 0
for _, end in chunks:
    if prev < len(body):
        body_chunks += 1
    prev = end
open(out_body, "wb").write(body)
json.dump({"code": code, "events": events, "first_byte_ms": round(chunks[0][0], 1) if chunks else None,
           "eof_ms": round(eof_ms, 1), "chunks": body_chunks, "curl_starttransfer_ms": round(start_ms, 1),
           "curl_total_ms": round(total_ms, 1)}, open(out_times, "w"))
print(code)
PY
chat_ext() {
  local token="$1" path="$2" body="$3"; shift 3
  curl -sS -N --max-time 180 --resolve "$EXT_HOST:80:$LB_IP" -D "$WORK/hdr" -X POST "$EXT_URL$path" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$body" "$@" \
    -w $'\n\x1e%{http_code} %{time_starttransfer} %{time_total}\n' 2>/dev/null \
    | python3 "$WORK/sse_reader.py" "$WORK/sse" "$WORK/sse.times"
}
times_() { json_of "$WORK/sse.times" "$1"; }
# 도착 시각 요약 한 줄: first byte / 이벤트별 / EOF / curl 의 starttransfer·total
timing_line() { times_ '"first_byte %sms, events %s, eof %sms, chunks %s, curl starttransfer %sms total %sms" % (d["first_byte_ms"], " ".join("%s@%sms" % (n, t) for n, t in d["events"]), d["eof_ms"], d["chunks"], d["curl_starttransfer_ms"], d["curl_total_ms"])'; }
# STEP 13 의 노출된 인스턴스 파드 env (AIX_NS·AIX_K8S 를 읽는다)
agent_env() { "${K[@]}" -n "$AIX_NS" get deployment "$AIX_K8S" -o json | deployment_env - agent | grep -E '^DAEGAM_(OIDC_ISSUER|OIDC_AUDIENCE|OIDC_JWKS_URL|CORS_ORIGINS|ALLOWED_GROUPS|AGENT_CLIENT_IDS)=' | sort | tr '\n' ' '; }
# STEP 13a·13(g) 뒤의 마무리. 남은 복제본을 지우고 MCP Server·Database 정리를 건다.
# 에이전트 본체는 STEP 12 가 이미 지웠고 13(g) 는 복제본까지 지운 뒤라 404 도 정상이다
final_cleanup() { # LABEL
  if [ -n "$AIX_ID" ]; then
    code="$(api DELETE "/agents/$AIX_ID")"
    case "$code" in 200|404) ;; *) die "$1" "DELETE /agents/$AIX_ID -> $code" ;; esac
  fi
  api DELETE "/mcp-servers/$MS_ID" >/dev/null || true
  { [ -n "$DS_ID" ] && api DELETE "/data-services/$DS_ID" >/dev/null; } || true
  echo "  cleanup: clone ${AIX_ID:-(none)} deleted; mcp server $MS_ID and data service ${DS_ID:-(none)} cleanup requested"
}

echo ">> context=$CTX platform_ns=$PLATFORM_NS tenant=$TENANT api=$API auth_mode=$AUTH_MODE memory_kind=$MEMORY_KIND llm_provider=${LLM_PROVIDER:-(none)}"
case "$MEMORY_KIND" in postgres|redis) ;; *) die 0 "E2E_MEMORY_KIND must be postgres or redis (got $MEMORY_KIND)" ;; esac

# ---------- 0. auth mode (oidc·local 이면 ConfigMap 을 patch 하고 rollout. cleanup 이 되돌린다) ----------
case "$AUTH_MODE" in
  header) ;;
  oidc)
    # 턴 위임 토큰(P53)도 같은 패치로 켜고 같은 트랩이 되돌린다. 이 게이트는 데이터 플레인이 IdP 를 믿는 oidc 를 보므로
    # 발급자를 idp 로 못 박는다. 플랫폼을 믿는 oidc 는 make e2e-oidc-platform 이 본다 (P53b 6.2절)
    switch_auth_mode oidc DAEGAM_TURN_TOKENS_ENABLED=true DAEGAM_DATA_PLANE_ISSUER=idp \
      || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to oidc and rolling the control plane failed"
    pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=oidc (was ${AUTH_MODE_PREV:-unset}), DAEGAM_TURN_TOKENS_ENABLED=true, DAEGAM_DATA_PLANE_ISSUER=idp; platform and UI rolled out" ;;
  local)
    # local 은 DAEGAM_OIDC_* 를 읽지 않으므로 그 키들은 그대로 두고 모드 한 줄과 부트스트랩 비밀번호만 넣는다 (D40).
    # 턴 위임 토큰(P53)은 가장 짧은 수명으로 켜 7t 의 턴이 그 수명을 넘기게 한다
    switch_auth_mode local DAEGAM_TURN_TOKENS_ENABLED=true "DAEGAM_TURN_TOKEN_TTL_SECONDS=$TURN_TTL" \
      || die 0 "switching ConfigMap $AUTH_MODE_CONFIGMAP to local and rolling the control plane failed"
    pass 0 "ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE=local (was ${AUTH_MODE_PREV:-unset}), DAEGAM_TURN_TOKENS_ENABLED=true, DAEGAM_TURN_TOKEN_TTL_SECONDS=$TURN_TTL; bootstrap password in Secret; platform and UI rolled out" ;;
  *) die 0 "E2E_AUTH_MODE must be header, oidc or local (got $AUTH_MODE)" ;;
esac

# ---------- 1. platform reachable + auth mode + home cluster ----------
"${K[@]}" -n "$PLATFORM_NS" rollout status deploy/daegam-platform --timeout=60s >/dev/null
start_pf "$PLATFORM_NS" svc/daegam-platform "${API_PORT}:9000"
PF_API=$PF_PID
wait_port "$API_PORT" || die 1 "port-forward to svc/daegam-platform did not open :$API_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/healthz")"
[ "$code" = "200" ] || die 1 "GET /healthz -> $code"
# 배포본이 요청한 모드인지 본다. 무토큰 GET /clusters 는 header 모드면 200, oidc 모드면 401 이다
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/clusters" -H "X-Daegam-Tenant: $TENANT")"
if [ "$AUTH_MODE" = "oidc" ]; then
  [ "$code" = "401" ] || die 1 "expected 401 without a token in oidc mode, got $code (did the rollout pick up DAEGAM_AUTH_MODE=oidc?)"
  start_pf "$PLATFORM_NS" svc/mock-oidc "${OIDC_PORT}:8080"
  PF_OIDC=$PF_PID
  wait_port "$OIDC_PORT" || die 1 "port-forward to svc/mock-oidc did not open :$OIDC_PORT"
  ADMIN_TOKEN="$(user_token '{"sub": "boot-admin", "groups": ["platform-admins"], "aud": "daegam-api"}')" \
    || die 1 "bootstrap admin token from $OIDC/_test/token failed"
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  code="$(api GET /me)"
  [ "$code" = "200" ] && [ "$(jq_ 'd["is_platform_admin"]')" = "True" ] || die 1 "GET /me with the bootstrap admin token -> $code $(cat "$WORK/body")"
  AUTH_NOTE="oidc (no-token 401, bootstrap admin boot-admin aud daegam-api)"
elif [ "$AUTH_MODE" = "local" ]; then
  [ "$code" = "401" ] || die 1 "expected 401 without a token in local mode, got $code (did the rollout pick up DAEGAM_AUTH_MODE=local?)"
  # 첫 기동이면 부트스트랩 비밀번호로 들어가 서버가 강제하는 비밀번호 바꾸기를 지난다. 이전 실행이 이미 바꿨으면 그 값으로 들어간다
  ADMIN_TOKEN="$(local_login_any admin "$LOCAL_BOOT_PW")"
  [ -n "$ADMIN_TOKEN" ] || die 1 "local admin login failed with both the bootstrap and the changed password"
  AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
  code="$(api GET /me)"
  [ "$code" = "200" ] && [ "$(jq_ 'd["is_platform_admin"]')" = "True" ] || die 1 "GET /me with the local admin token -> $code $(cat "$WORK/body")"
  # 디스커버리와 JWKS 는 발급자 루트에서 인증 없이 열린다. 런타임과 ToolHive 가 이것으로 검증한다
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/.well-known/openid-configuration")"
  [ "$code" = "200" ] && [ -n "$(jq_ 'd["jwks_uri"]')" ] || die 1 "GET /.well-known/openid-configuration -> $code $(cat "$WORK/body")"
  # JWKS 는 플랫폼 RSA 키 하나와 개인 토큰의 슬롯 키(ES256, kid pat-…)이고 ?use=service 는 RSA 키 하나다 (P32 4절).
  # 플랫폼 kid 는 관리자 토큰의 헤더에서 읽는다
  PLATFORM_KID="$(jwt_json "$ADMIN_TOKEN" 0 | json_of - 'd["kid"]')"
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/.well-known/jwks.json")"
  [ "$code" = "200" ] && [ "$(jq_ "[k['alg'] for k in d['keys'] if k['kid'] == '$PLATFORM_KID'] == ['RS256'] and all(k['kid'].startswith('pat-') and k['alg'] == 'ES256' for k in d['keys'] if k['kid'] != '$PLATFORM_KID')")" = "True" ] \
    || die 1 "GET /.well-known/jwks.json -> $code (want one RS256 key $PLATFORM_KID and pat- ES256 slots) $(cat "$WORK/body")"
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$BASE/.well-known/jwks.json?use=service")"
  [ "$code" = "200" ] && [ "$(jq_ "[k['kid'] for k in d['keys']] == ['$PLATFORM_KID']")" = "True" ] \
    || die 1 "GET /.well-known/jwks.json?use=service -> $code (want the platform key $PLATFORM_KID alone) $(cat "$WORK/body")"
  AUTH_NOTE="local (no-token 401, admin signed in with a password, discovery + jwks served by the platform)"
else
  [ "$code" = "200" ] || die 1 "expected 200 without a token in header mode, got $code (ConfigMap $AUTH_MODE_CONFIGMAP DAEGAM_AUTH_MODE is not header?)"
  AUTH_NOTE="header (X-Daegam-Tenant only)"
fi
if [ "$AUTH_MODE" != header ]; then
  # 모드를 바꾸면 플랫폼이 MCPOIDCConfig 와 살아 있는 소비자를 다시 적용한다. 다 될 때까지 기다린다 (P32)
  wait_auth_reapplied "$ADMIN_TOKEN" || die 1 "data-plane auth was not re-applied after switching to $AUTH_MODE"
  AUTH_NOTE="$AUTH_NOTE; data-plane auth re-applied"
fi
code="$(api GET /clusters)"
[ "$code" = "200" ] || die 1 "GET /clusters -> $code"
# 없으면 바로 아래 줄이 스스로 다룬다
HOME_ID="$(find_id home)"
HOME_OK="$(jq_ 'next((c["status"]["preflight"]["ok"] for c in d if c["name"]=="home"), "")')"
[ -n "$HOME_ID" ] && [ "$HOME_OK" = "True" ] || die 1 "cluster home missing or preflight not ok (id=$HOME_ID ok=$HOME_OK)"
api GET /version >/dev/null
pass 1 "platform reachable :$API_PORT (build $(jq_ 'd.get("api_build") or d.get("api_version")')), auth mode $AUTH_NOTE, cluster home id=$HOME_ID preflight ok"

# ---------- 1t. 턴 위임 토큰 (P53): 위임이 열릴 사람과 승인자 ----------
# 위임은 Agent 의 테넌트를 쥔 소속 행이 있는 사람에게만 열린다. 관리자의 platform-admin(*) 행은 테넌트를 쥐지 않으므로 관리자를
# $TENANT 의 developer 로 둔다 (P53 2절). 바꾼 것은 EXIT 트랩(drop_turn_setup)이 되돌린다
if [ "$AUTH_MODE" = header ]; then
  cond_skip 1t "turn tokens need a mode that confirms the person (local or oidc); header mode mints nothing"
else
  [ "$(platform_config DAEGAM_TURN_TOKENS_ENABLED)" = true ] || die 1t "ConfigMap $AUTH_MODE_CONFIGMAP lacks DAEGAM_TURN_TOKENS_ENABLED=true after STEP 0"
  if [ "$AUTH_MODE" = local ]; then
    turn_member_developer 1t
    # 8t·8u·8v 의 승인자. developer 라 agent:approve 를 쥔다. 로그인은 쓰기 직전(8t)에 한다
    APPROVER_SUB="$(local_user_ensure "$APPROVER_NAME")" || die 1t "creating $APPROVER_NAME failed"
    APPROVER_ADDED=1
    local_set_member "$TENANT" "$APPROVER_SUB" developer || die 1t "making $APPROVER_NAME a developer of $TENANT failed"
    WHO="admin $ADMIN_SUB (manual role before: ${TURN_ROLE_BEFORE:-none}) and approver $APPROVER_NAME ($APPROVER_SUB) are developers of $TENANT"
  else
    # oidc 의 소속은 토큰의 그룹이 매핑을 거쳐 요청마다 맞춰진다. 매핑 하나를 더하고 관리자 토큰에 그 그룹을 싣는다
    group_mappings_add 1t "$TENANT" "$TURN_GROUP=developer"
    ADMIN_TOKEN="$(user_token '{"sub": "boot-admin", "groups": ["platform-admins", "'"$TURN_GROUP"'"], "aud": "daegam-api"}')" \
      || die 1t "admin token with $TURN_GROUP from $OIDC/_test/token failed"
    AUTH=(-H "Authorization: Bearer $ADMIN_TOKEN")
    ADMIN_SUB="boot-admin"
    WHO="admin $ADMIN_SUB carries $TURN_GROUP, mapped to developer of $TENANT"
  fi
  code="$(api GET /me)"
  { [ "$code" = 200 ] && [ "$(jq_ "any(m['tenant'] == '$TENANT' for m in d['memberships'])")" = True ]; } \
    || die 1t "GET /me -> $code; the admin holds no membership of $TENANT: $(jq_ 'd.get("memberships")')"
  pass 1t "DAEGAM_TURN_TOKENS_ENABLED=true; $WHO"
fi

# ---------- 2. stub provider (idempotent) ----------
ensure_stub_provider 2 "$STUB_PROVIDER"
if [ -n "$LLM_PROVIDER" ]; then
  [ "$(jq_ "[p['name'] for p in d['llm_providers'] if p['name']=='$LLM_PROVIDER']")" = "['$LLM_PROVIDER']" ] \
    || die 2 "E2E_LLM_PROVIDER=$LLM_PROVIDER is not a registered provider (make llm-provider-local first)"
fi
pass 2 "provider $STUB_PROVIDER (kind stub, no key) registered in Platform Settings (added this run: $STUB_PROVIDER_ADDED; cleanup removes what it added)"

# ---------- 3. calculator backend → MCP Server [Add, Divide] 저장 → 배포 → Ready ----------
# 재실행이면 같은 이름에 새 버전이 붙는다. 끝나면 $WORK/body 가 endpoint PATCH 응답이다
calc_backend 3 "$BP_NAME" "$BP_DISPLAY"
# shellcheck disable=SC2153  # CALC_BP_ID 는 lib/e2e_calc.sh 의 calc_backend 가 둔다
BP_ID="$CALC_BP_ID"
BP_VER="$(jq_ 'd.get("version") or (d.get("versions") or [{}])[-1].get("version") or 1')"
# 이름 충돌(409)은 지우지 않고 PUT 으로 덮는다 (맨 위 "재실행의 409")
save_doc 3 /mcp-servers "$MS_NAME" \
  "{\"name\": \"$MS_NAME\", \"display_name\": \"$MS_DISPLAY\", \"backend_profile_id\": \"$BP_ID\", \"spec_version\": $BP_VER, \"tools\": [\"Add\", \"Divide\"]}"
MS_ID="$SAVED_ID"
code="$(api POST "/mcp-servers/$MS_ID/deploy" "{\"cluster_id\": \"$HOME_ID\"}")"
case "$code" in
  201|202) ;;
  # 배포의 409 가운데 여기만 redeploy 를 안 한다. 이전 실행이 남긴 배포를 그대로 쓴다. 여기서 redeploy 하면
  # StatefulSet 이 롤링하는 동안 phase 가 옛 Ready 로 남아 있어 STEP 7 의 첫 툴 호출이 롤링 중인 프록시에
  # 걸려 30초 뒤 turn_failed 가 난다 (실측). STEP 6 의 에이전트에는 이 문제가 없어 거기서는 redeploy 로 잇는다
  409) [ "$(jq_ "d['detail']['code']")" = "already_deployed" ] \
         || die 3 "POST /mcp-servers/$MS_ID/deploy -> 409 $(jq_ "d['detail']['code']"): $(cat "$WORK/body")" ;;
  *) die 3 "POST /mcp-servers/$MS_ID/deploy -> $code" ;;
esac
echo ">> waiting up to ${READY_TIMEOUT}s for mcp server $MS_ID phase=Ready"
wait_ready "/mcp-servers/$MS_ID" 3
MS_URL="$(jq_ '(d.get("status") or {}).get("url")')"
pass 3 "backend $BP_NAME id=$BP_ID v$BP_VER; mcp server $MS_NAME id=$MS_ID tools [Add, Divide] 저장 → 배포 Ready url=$MS_URL"

# ---------- 4. lab Postgres (or lab Redis 8 with E2E_MEMORY_KIND=redis) as Database (memory) ----------
DS_ID=""; HAS_MEMORY=0
if [ "$MEMORY_KIND" = redis ]; then
  # redis 경로에는 SKIP 이 없다. lab Redis 가 없거나 8 미만이면 게이트 실패다 (50-redis.yaml 은 redis:8)
  "${K[@]}" -n "$SPIKE_NS" rollout status deploy/redis --timeout=120s >/dev/null || die 4 "deploy/redis in $SPIKE_NS did not roll out (image redis:8 after make lab-up?)"
  # Service·Secret 확인, 등록·verify 와 redis.checkpointer 가 ready 인지(RediSearch·RedisJSON 이 있다)는 lab_database 가 본다
  lab_database 4 redis "$DS_NAME" "$DS_DISPLAY"
  DS_ID="$DATA_SERVICE_ID"
  # verify 응답: redis.version 의 첫 숫자가 8 이상이다
  REDIS_VERSION="$(jq_ '(d.get("redis") or {}).get("version")')"
  REDIS_MAJOR="${REDIS_VERSION%%.*}"
  case "$REDIS_MAJOR" in ''|None|*[!0-9]*) die 4 "verify has no numeric redis.version: $(cat "$WORK/body")" ;; esac
  [ "$REDIS_MAJOR" -ge 8 ] || die 4 "lab Redis is $REDIS_VERSION; the checkpointer needs Redis 8 (50-redis.yaml image redis:8, make lab-up)"
  REDIS_MODULES="$(jq_ '(d.get("redis") or {}).get("modules")')"
  # 문서에도 남았다 (validate 가 이 값을 본다. 422 redis_modules_missing)
  code="$(api GET "/data-services/$DS_ID")"
  [ "$code" = "200" ] || die 4 "GET /data-services/$DS_ID -> $code"
  DS_CAP="$(jq_ '(d.get("capabilities") or {}).get("checkpointer")')"
  [ "$DS_CAP" = "ready" ] || die 4 "capabilities.checkpointer=$DS_CAP after verify (expected ready): $(cat "$WORK/body")"
  HAS_MEMORY=1
  pass 4 "data service $DS_NAME id=$DS_ID (redis $LAB_REDIS_HOST:6379/0, with password) verify ok: redis $REDIS_VERSION modules=$REDIS_MODULES checkpointer=ready; GET detail capabilities.checkpointer=$DS_CAP"
elif "${K[@]}" -n "$SPIKE_NS" get svc postgres -o name >/dev/null 2>&1 && "${K[@]}" -n "$SPIKE_NS" get secret "$LAB_PG_SECRET" -o name >/dev/null 2>&1; then
  PG_USER="$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" username)"; PG_PASSWORD="$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" password)"
  PG_DB="$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" database)"
  case "$PG_PASSWORD" in *[@:/#]*) ;; *) echo "  note: lab postgres password has no URL-reserved char; the conninfo path is not exercised" ;; esac
  ds_body="$(lab_ds_body postgres "$DS_NAME" "$DS_DISPLAY")"
  register_data_service 4 "$DS_NAME" "$ds_body"
  DS_ID="$DATA_SERVICE_ID"
  HAS_MEMORY=1
  pass 4 "data service $DS_NAME id=$DS_ID (postgres $LAB_PG_HOST:5432/$PG_DB user=$PG_USER, password has URL-reserved chars) verify ok"
else
  skip 4 "no Service postgres / Secret $LAB_PG_SECRET in $SPIKE_NS (make lab-up applies 55-postgres.yaml); memory node dropped, HITL steps skipped"
fi

# ---------- 5. template → agent 저장 (201, draft) → validate ok ----------
code="$(api GET /agents/templates)"
[ "$code" = "200" ] || die 5 "GET /agents/templates -> $code"
[ "$(jq_ '[t["id"] for t in d]')" = "['support-agent']" ] || die 5 "templates: $(jq_ '[t["id"] for t in d]')"
# 5g·14 도 같은 템플릿에서 본문을 만든다
cp "$WORK/body" "$WORK/templates.json"
# Database 가 있으면 체크포인터와 장기 기억(D34, 30일)을 켜고, 장기 기억이 쓰는 파일 도구와 승인의 고치기(8e)와 분당 한도를
# 둔다. 없으면 memory 노드와 그것을 필요로 하는 분당 한도·승인을 뺀 인메모리 경로다
MEMORY_ARGS=()
[ -z "$DS_ID" ] || MEMORY_ARGS=(--ds-id "$DS_ID" --long-term-days 30 --files --approve-edit --rate-limit)
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/agent.json" --name "$AG_NAME" --display "$AG_DISPLAY" \
  --provider "$STUB_PROVIDER" --source "mcp_instance:$MS_ID" ${MEMORY_ARGS[@]+"${MEMORY_ARGS[@]}"}
# 이름 충돌(409)은 지우지 않고 PUT 으로 덮는다 (맨 위 "재실행의 409"). 끝나면 $WORK/body 가 저장 응답이다
save_doc 5 /agents "$AG_NAME" "$(cat "$WORK/agent.json")"
AG_ID="$SAVED_ID"; AG_REV="$(jq_ 'd["revision"]')"
[ "$(jq_ 'd["name"], d["display_name"]')" = "$AG_NAME $AG_DISPLAY" ] || die 5 "names: $(jq_ 'd["name"], d["display_name"]')"
SAVE_ISSUES="$(jq_ '[i["code"] for i in d["issues"]]')"
code="$(api POST /agents/validate "$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); print(json.dumps({"graph": b["graph"], "service": b["service"]}, ensure_ascii=False))' "$WORK/agent.json")")"
[ "$code" = "200" ] || die 5 "POST /agents/validate -> $code"
[ "$(jq_ 'd["ok"]')" = "True" ] || die 5 "validate not ok: $(jq_ 'd["issues"]')"
VAL_ISSUES="$(jq_ '[i["code"] for i in d["issues"]]')"
# 스텁 provider 에는 provider_no_key 가 면제되고 오류가 없어야 한다
[ "$(jq_ '[i for i in d["issues"] if i["level"]=="error"]')" = "[]" ] || die 5 "validate has errors: $(jq_ 'd["issues"]')"
pass 5 "agent $AG_NAME ($AG_DISPLAY) id=$AG_ID revision=$AG_REV draft 로 저장 (issues=$SAVE_ISSUES); validate ok (issues=$VAL_ISSUES); memory=$HAS_MEMORY long_term=$HAS_MEMORY (retention 30일, 파일 도구 켬)"

# ---------- 5g. Gateway 소스 (P35 11절): 런타임이 우리 Gateway(vMCP)와 2026-07-28 규약으로 붙는다 ----------
# STEP 3 의 MCP Server 를 게시해 Gateway 로 묶고 그 Gateway 를 도구 소스로 쓰는 memory 없는 Agent 를 띄운다. local 은 vMCP
# 앞에 Cedar 가 걸린 경로다. oidc 의 Gateway 소스는 Tenant Settings 의 agent_oauth(client credentials)를 쓰므로 그것을 먼저
# 둔다. 만든 것은 끝에서 drop_gateway_source 가 걷는다.
# 턴 위임 토큰(P53 3.1절): local 은 그 호출이 사람(sub)과 Agent(act)를 싣고, vMCP 가 이 Gateway 에 묶인 위임 토큰만 받는다.
# oidc 의 Gateway 소스는 사람을 싣지 않고(묶음 B 까지) Agent 의 client 로 부르며, IdP 가 낸 act 토큰은 전처럼 받는다. 감사
# 기록은 vMCP 파드의 로그에서 오므로 Gateway 를 걷기 전에 본다
if [ "$MEMORY_KIND" = redis ] && [ "$SHARED_SKIP" = 1 ]; then
  cond_skip 5g "E2E_SHARED_STEPS=skip: the Gateway source Agent has no memory, so the Redis run would repeat what make e2e-agent checks"
else
  CALC_MS_ID="$MS_ID"; CALC_MS_NAME="$MS_NAME"
  [ "$AUTH_MODE" != oidc ] || agent_oauth_on 5g
  calc_publish 5g
  calc_gateway 5g "$GW_NAME" "{\"cluster_id\": \"$HOME_ID\"}"
  GW_NS="$(jq_ 'd["k8s"]["namespace"]')"; TURN_GW_ID="$CALC_GW_ID"
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/gw-agent.json" --name "$GW_AG_NAME" --display "$GW_AG_DISPLAY" \
    --provider "$STUB_PROVIDER" --source "gateway_instance:$CALC_GW_ID"
  save_doc 5g /agents "$GW_AG_NAME" "$(cat "$WORK/gw-agent.json")"
  GW_AG_ID="$SAVED_ID"
  deploy_ready 5g /agents "$GW_AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  # Gateway 소스의 위임은 그 클러스터의 Gateway 게이트가 열렸을 때만 열린다 (P53 3.5절)
  [ "$AUTH_MODE" != local ] || wait_gateway_gate 5g "$HOME_ID"
  GW_BEFORE="$(cluster_now)" || die 5g "reading the platform's clock failed"
  code="$(chat "/agents/$GW_AG_ID/chat" "$(msg "$ADD_MSG")" -D "$WORK/chat.hdr")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 5g "POST /agents/$GW_AG_ID/chat -> $code"; }
  RESULT="$(sse_ 'first("tool_result")["content"]')"
  { [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ 'first("tool_result")["status"]')" = "success" ] && echo "$RESULT" | grep -q 7; } \
    || die 5g "Add through the Gateway: $(sse_ 'names') tool_result=$RESULT"
  GW_AFTER="$(cluster_now)" || die 5g "reading the platform's clock failed"
  turn_window 5g "$GW_AG_ID" "$(sse_ 'first("done")["session_id"]')" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$GW_BEFORE" "$GW_AFTER"
  GW_SINCE="$TURN_SINCE"; GW_UNTIL="$TURN_UNTIL"
  code="$(api GET "/agents/$GW_AG_ID/metrics")"
  [ "$code" = "200" ] || die 5g "GET /agents/$GW_AG_ID/metrics -> $code"
  GW_PROTOCOLS="$(jq_ 'd["tools"]["protocols"]')"
  [ "$(all_modern)" = "True" ] \
    || die 5g "tools.protocols=$GW_PROTOCOLS (expected $MCP_MODERN for the Gateway source, tools.last_error=$(jq_ 'd["tools"]["last_error"]'))"
  TURN_NOTE=""
  if [ "$AUTH_MODE" != header ]; then
    start_pf "$GW_NS" "svc/vmcp-$CALC_GW_K8S_NAME" "${VMCP_PORT}:4483"
    PF_VMCP=$PF_PID
    wait_port "$VMCP_PORT" || die 5g "port-forward to svc/vmcp-$CALC_GW_K8S_NAME did not open :$VMCP_PORT"
    VMCP_URL="http://127.0.0.1:${VMCP_PORT}/mcp"
    if [ "$AUTH_MODE" = local ]; then
      # 실험 토큰은 vMCP 가 믿는 사람 발급자와 daegam-mcp 로 서명한다. 거절할 셋은 테넌트 그룹을 실어 그룹 permit 이 받았을
      # 토큰이다. 그래서 거절은 위임 토큰의 forbid 가 한 것이다. vMCP 는 신원마다 Tool 목록을 30초 기억하므로 sub 를 가른다
      LAB_ISSUER="$(curl -fsS "$API/auth/config" | json_of - 'd.get("issuer") or ""')" || die 5g "GET /auth/config failed"
      LAB_BOUND="$(human_token e2e-lab-bound --act "$LAB_ACTOR" --bound-to "gateway:$CALC_GW_ID" --no-groups)" \
        || die 5g "svc_token.py (bound to this Gateway) failed"
      LAB_OTHER="$(human_token e2e-lab-other --act "$LAB_ACTOR" --bound-to gateway:e2e-other-gateway --groups "daegam-$TENANT")" \
        || die 5g "svc_token.py (another Gateway) failed"
      LAB_STRANGER="$(human_token e2e-lab-stranger --tenant e2e-stranger --act "$LAB_ACTOR" --bound-to "gateway:$CALC_GW_ID" \
        --groups "daegam-$TENANT")" || die 5g "svc_token.py (another tenant) failed"
      LAB_UNBOUND="$(human_token e2e-lab-unbound --act "$LAB_ACTOR" --groups "daegam-$TENANT")" \
        || die 5g "svc_token.py (act without bound_to) failed"
      mcp_admits 5g "$LAB_BOUND" "a token bound to this Gateway (no groups)" "$VMCP_URL"
      mcp_refuses 5g "$LAB_OTHER" "a token bound to another Gateway (with the tenant group)" "$VMCP_URL"
      mcp_refuses 5g "$LAB_STRANGER" "a token bound to this Gateway naming tenant e2e-stranger (with the tenant group)" "$VMCP_URL"
      mcp_refuses 5g "$LAB_UNBOUND" "a token with act and no bound_to (with the tenant group)" "$VMCP_URL"
      audit_expect 5g "$GW_SINCE" "gateway Add|$GW_SINCE|$GW_UNTIL|$CALC_GW_ID|Add|$ADMIN_SUB|svc:agent:agent-$GW_AG_NAME|1"
      TURN_NOTE="; turn token: vMCP audit sub=$ADMIN_SUB act=svc:agent:agent-$GW_AG_NAME; lab tokens: bound to this Gateway -> 7, bound to another Gateway / another tenant / act without bound_to -> empty list and refused"
    else
      IDP_ACT="$(user_token '{"sub": "e2e-delegated", "groups": ["'"$TURN_MCP_GROUP"'"], "extra": {"act": {"sub": "e2e-idp-actor"}}}')" \
        || die 5g "IdP token with act from $OIDC/_test/token failed"
      mcp_admits 5g "$IDP_ACT" "an IdP token with act and the tenant MCP group $TURN_MCP_GROUP" "$VMCP_URL"
      audit_expect 5g "$GW_SINCE" "gateway Add|$GW_SINCE|$GW_UNTIL|$CALC_GW_ID|Add|$OAUTH_CLIENT_ID|-|1"
      TURN_NOTE="; agent_oauth $OAUTH_CLIENT_ID: vMCP audit sub=$OAUTH_CLIENT_ID without act (oidc Gateway sources carry no person); an IdP token with act and $TURN_MCP_GROUP -> 7"
    fi
    stop_pf "$PF_VMCP" "$VMCP_PORT"; PF_VMCP=""
  fi
  drop_gateway_source
  pass 5g "Gateway $GW_NAME (publish $MS_NAME -> $CALC_PUBLISH_CODE) as the tools source of $GW_AG_NAME: Add(3,4) -> '$RESULT', tools.protocols $GW_PROTOCOLS$TURN_NOTE; agent, gateway and publish removed"
fi

# ---------- 6. deploy → Ready; kubectl objects ----------
# 배포 본문은 cluster_id 만이다. 이름은 STEP 5 에서 정해졌다 (D42)
code="$(api POST "/agents/$AG_ID/deploy" "{\"cluster_id\": \"$HOME_ID\"}")"
case "$code" in
  201) ;;
  # 이전 실행이 남긴 배포다. 같은 문서를 현재 리비전으로 재배포해 이어간다
  409) [ "$(jq_ "d['detail']['code']")" = "already_deployed" ] \
         || die 6 "POST /agents/$AG_ID/deploy -> 409 $(jq_ "d['detail']['code']"): $(cat "$WORK/body")"
       code="$(api POST "/agents/$AG_ID/redeploy")"; [ "$code" = "200" ] || die 6 "redeploy $AG_NAME -> $code" ;;
  *) die 6 "POST /agents/$AG_ID/deploy -> $code" ;;
esac
[ "$(jq_ 'd["name"], d["display_name"], d["k8s"]["name"]')" = "$AG_NAME $AG_DISPLAY agent-$AG_NAME" ] \
  || die 6 "names: $(jq_ 'd["name"], d["display_name"], d["k8s"]["name"]') (expected $AG_NAME $AG_DISPLAY agent-$AG_NAME)"
DEPLOY_WARNINGS="$(jq_ 'd.get("warnings")')"
echo ">> waiting up to ${READY_TIMEOUT}s for agent $AG_ID phase=Ready"
wait_ready "/agents/$AG_ID" 6
AG_NS="$(jq_ 'd["k8s"]["namespace"]')"; AG_K8S="$(jq_ 'd["k8s"]["name"]')"; AG_TOKEN_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
AG_DEPLOYED_REV="$(jq_ 'd["deployed_revision"]')"; LLM_SECRETS="$(jq_ '" ".join(d["llm_secrets"])')"
CHAT_URL="$(jq_ 'd["status"]["chat_url"]')"; STATUS_WARN="$(jq_ 'd["status"]["warnings"]')"
[ "$CHAT_URL" = "/api/agents/$AG_ID/chat" ] || die 6 "status.chat_url=$CHAT_URL"
"${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o name >/dev/null || die 6 "Deployment $AG_NS/$AG_K8S missing"
"${K[@]}" -n "$AG_NS" get service "$AG_K8S" -o name >/dev/null || die 6 "Service $AG_NS/$AG_K8S missing"
"${K[@]}" -n "$AG_NS" get networkpolicy "daegam-$AG_K8S" -o name >/dev/null || die 6 "NetworkPolicy $AG_NS/daegam-$AG_K8S missing"
"${K[@]}" -n "$AG_NS" get secret "$AG_TOKEN_SECRET" -o name >/dev/null || die 6 "token Secret $AG_NS/$AG_TOKEN_SECRET missing"
for s in $LLM_SECRETS; do
  "${K[@]}" -n "$AG_NS" get secret "$s" -o name >/dev/null || die 6 "provider Secret copy $AG_NS/$s missing"
done
env_names="$("${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o jsonpath='{.spec.template.spec.containers[?(@.name=="agent")].env[*].name}')"
echo "  env: $env_names"
case " $env_names " in *" DAEGAM_INSTANCE_TOKEN "*) ;; *) die 6 "DAEGAM_INSTANCE_TOKEN missing from pod env" ;; esac
# 서비스 토큰 (D41): 런타임이 MCP Server 를 부를 토큰은 같은 Secret 의 mcp_token 이고 env 로 내려간다.
# agent.json 의 소스 auth 는 모든 모드에서 bearer 다. Gateway 소스가 없으니 DAEGAM_GATEWAY_TOKEN 은 없다
case " $env_names " in *" DAEGAM_MCP_TOKEN "*) ;; *) die 6 "DAEGAM_MCP_TOKEN missing from pod env (D41)" ;; esac
case " $env_names " in *" DAEGAM_GATEWAY_TOKEN "*) die 6 "DAEGAM_GATEWAY_TOKEN must not be in pod env without a Gateway source" ;; esac
SVC_MCP_TOKEN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" mcp_token)"
[ -n "$SVC_MCP_TOKEN" ] || die 6 "token Secret $AG_NS/$AG_TOKEN_SECRET has no mcp_token key (D41)"
SVC_CLAIMS="$(jwt_json "$SVC_MCP_TOKEN" 1 | json_of - 'd["sub"], d["aud"]')"
[ "$SVC_CLAIMS" = "svc:agent:$AG_K8S daegam-internal:$TENANT" ] || die 6 "mcp_token claims: $SVC_CLAIMS (expected svc:agent:$AG_K8S daegam-internal:$TENANT)"
SVC_REV="$("${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o jsonpath='{.spec.template.metadata.annotations.daegam\.devops\.ai\.kr/svc-token-rev}')"
[ -n "$SVC_REV" ] && [ "$SVC_REV" != "0" ] || die 6 "pod template lacks daegam.devops.ai.kr/svc-token-rev (D41): '$SVC_REV'"
AG_TOKEN0="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" token)"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/agents/$AG_ID/agent.json" -H "Authorization: Bearer $AG_TOKEN0")"
[ "$code" = "200" ] || die 6 "GET /agents/$AG_ID/agent.json with the instance token -> $code"
TOOL_AUTH="$(jq_ 'd["tools"][0]["auth"]["kind"], d["tools"][0]["auth"].get("secret_env")')"
[ "$TOOL_AUTH" = "bearer DAEGAM_MCP_TOKEN" ] || die 6 "agent.json tools[0].auth = $TOOL_AUTH (expected bearer DAEGAM_MCP_TOKEN)"
# 서버마다의 audience (P50 9절): MCP Server 소스마다 토큰 하나가 키·env DAEGAM_MCP_TOKEN_<서버 id 16진수 대문자> 로
# 내려가고 테넌트 audience 와 그 서버의 audience 를 함께 싣는다. init 은 자기 템플릿의 표 지문을 머리
# X-Daegam-Mcp-Token-Table 로 보내 그 env 를 받는다. 머리가 없으면(표가 생기기 전 파드) 위의 옛 단일 토큰이다
SRC_ENV="$(mcp_token_env "$MS_ID")"
case " $env_names " in *" $SRC_ENV "*) ;; *) die 6 "$SRC_ENV missing from pod env (P50 9)" ;; esac
SRC_AUD="$(token_aud "$AG_NS" "$AG_TOKEN_SECRET" "$SRC_ENV")"
[ "$SRC_AUD" = "[\"daegam-internal:$TENANT\", \"daegam-internal:$TENANT:$MS_ID\"]" ] || die 6 "$SRC_ENV aud: $SRC_AUD"
TOKEN_TABLE="$("${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o jsonpath='{.spec.template.spec.initContainers[0].env[?(@.name=="DAEGAM_MCP_TOKEN_TABLE")].value}')"
[ -n "$TOKEN_TABLE" ] || die 6 "init container lacks DAEGAM_MCP_TOKEN_TABLE (P50 9)"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/agents/$AG_ID/agent.json" -H "Authorization: Bearer $AG_TOKEN0" -H "X-Daegam-Mcp-Token-Table: $TOKEN_TABLE")"
[ "$code" = "200" ] || die 6 "GET /agents/$AG_ID/agent.json with the token table -> $code"
TOOL_AUTH="$(jq_ 'd["tools"][0]["auth"]["kind"], d["tools"][0]["auth"].get("secret_env")')"
[ "$TOOL_AUTH" = "bearer $SRC_ENV" ] || die 6 "agent.json tools[0].auth with the token table = $TOOL_AUTH (expected bearer $SRC_ENV)"
MEMORY_NOTE=""
if [ "$HAS_MEMORY" = 1 ] && [ "$MEMORY_KIND" = redis ]; then
  # redis 바인딩은 HOST/PORT/DB/PASSWORD/URL(redis://) 이고 postgres 의 DATABASE 는 없다.
  # lab Redis 가 --requirepass 로 뜨므로 PASSWORD 도 있어야 한다 (없으면 런타임이 못 붙는다)
  for e in DAEGAM_DB_0_HOST DAEGAM_DB_0_PORT DAEGAM_DB_0_DB DAEGAM_DB_0_PASSWORD DAEGAM_DB_0_URL; do
    case " $env_names " in *" $e "*) ;; *) die 6 "$e missing from pod env (redis binding)" ;; esac
  done
  case " $env_names " in *" DAEGAM_DB_0_DATABASE "*) die 6 "DAEGAM_DB_0_DATABASE must not be in pod env for a redis binding: $env_names" ;; esac
  # 런타임이 받는 agent.json 을 인스턴스 토큰(token Secret 의 token 키)으로 직접 받아 checkpointer kind 를 본다
  # (initContainer 와 같은 경로. 테넌트 헤더 없이 Bearer 만)
  AG_TOKEN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" token)"
  [ -n "$AG_TOKEN" ] || die 6 "token Secret $AG_NS/$AG_TOKEN_SECRET has no token key"
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/agents/$AG_ID/agent.json" -H "Authorization: Bearer $AG_TOKEN")"
  [ "$code" = "200" ] || die 6 "GET /agents/$AG_ID/agent.json with the instance token -> $code"
  CKPT_KIND="$(jq_ '((d.get("memory") or {}).get("checkpointer") or {}).get("kind")')"
  CKPT_DSN_ENV="$(jq_ '((d.get("memory") or {}).get("checkpointer") or {}).get("dsn_env")')"
  [ "$CKPT_KIND" = "redis" ] || die 6 "agent.json memory.checkpointer.kind=$CKPT_KIND (expected redis): $(jq_ 'd.get("memory")')"
  # memory 가 있으면 no_shared_state 경고가 없어야 한다 (Redis 에 붙었다)
  [ "$STATUS_WARN" = "[]" ] || die 6 "status.warnings should be empty with Redis memory: $STATUS_WARN"
  MEMORY_NOTE="; redis binding env DAEGAM_DB_0_{HOST,PORT,DB,PASSWORD,URL} (no DATABASE); agent.json memory.checkpointer{kind: $CKPT_KIND, dsn_env: $CKPT_DSN_ENV} via the instance token"
elif [ "$HAS_MEMORY" = 1 ]; then
  for e in DAEGAM_DB_0_HOST DAEGAM_DB_0_PASSWORD DAEGAM_DB_0_URL; do
    case " $env_names " in *" $e "*) ;; *) die 6 "$e missing from pod env" ;; esac
  done
  # 스텁이 아닌 이상 memory 가 있으면 no_shared_state 경고가 없어야 한다 (Postgres 에 붙었다)
  [ "$STATUS_WARN" = "[]" ] || die 6 "status.warnings should be empty with Postgres memory: $STATUS_WARN"
else
  echo "$STATUS_WARN" | grep -q no_shared_state || die 6 "status.warnings lacks no_shared_state without memory: $STATUS_WARN"
fi
pass 6 "agent $AG_NAME ($AG_DISPLAY) id=$AG_ID deployed Ready rev=$AG_DEPLOYED_REV; $AG_NS/$AG_K8S Deployment·Service·NetworkPolicy, token Secret $AG_TOKEN_SECRET (mcp_token sub=svc:agent:$AG_K8S, svc-token-rev=$SVC_REV, agent.json auth bearer, $SRC_ENV aud=$SRC_AUD via table $TOKEN_TABLE), provider copies [${LLM_SECRETS:-none (stub has no key)}]; deploy warnings=$DEPLOY_WARNINGS status.warnings=$STATUS_WARN$MEMORY_NOTE"

# ---------- 6f. 기능 이름 (D65): init 이 알린 기록과 init·준비 검사의 렌더 ----------
# 기록은 init 이 agent.json 을 받을 때 실은 머리에서 오고 스냅숏 이미지의 것만 상태에 보인다(3절)
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "200" ] || die 6f "GET /agents/$AG_ID -> $code"
SNAP_IMAGE="$(jq_ 'd["effective_config"]["images"]["agent_image"]')"
RT_RECORD="$(jq_ 'd["status"].get("runtime")')"
[ "$(jq_ '(d["status"].get("runtime") or {}).get("image")')" = "$SNAP_IMAGE" ] \
  || die 6f "status.runtime=$RT_RECORD is not a record of the snapshot image $SNAP_IMAGE (an image built before P35 sends no feature header)"
[ "$(jq_ "'$CANCEL_FEATURE' in d['status']['runtime']['features']")" = "True" ] || die 6f "status.runtime.features lacks $CANCEL_FEATURE: $RT_RECORD"
STATUS_GREETING="$(jq_ 'd["status"]["greeting"]')"   # 7d 가 템플릿의 인사말과 맞댄다
POD_SPEC="$("${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o jsonpath="{.spec.template.spec.initContainers[?(@.name==\"$INIT_CONTAINER\")].image} {.spec.template.spec.containers[?(@.name==\"agent\")].image} {.spec.template.spec.containers[?(@.name==\"agent\")].readinessProbe.httpGet.path}")"
[ "$POD_SPEC" = "$SNAP_IMAGE $SNAP_IMAGE $READY_PROBE_PATH" ] \
  || die 6f "init image, agent image and readiness path: $POD_SPEC (expected $SNAP_IMAGE $SNAP_IMAGE $READY_PROBE_PATH)"
pass 6f "status.runtime $RT_RECORD matches the snapshot image; init $INIT_CONTAINER runs the runtime image $SNAP_IMAGE; readinessProbe $READY_PROBE_PATH"

# ---------- 7. chat SSE: Add(3,4) ----------
T7_BEFORE="$(cluster_now)" || die 7 "reading the platform's clock failed"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG")" -D "$WORK/chat.hdr")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 7 "POST /chat -> $code"; }
NAMES="$(sse_ 'names')"
[ "$(sse_ 'names[-1]')" = "done" ] || die 7 "stream did not end with done: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ 'first("tool_call")["name"]')" = "Add" ] || die 7 "tool_call is not Add: $NAMES"
[ "$(sse_ 'first("tool_call")["args"]')" = "{'a': 3, 'b': 4}" ] || die 7 "tool_call args: $(sse_ 'first("tool_call")["args"]')"
RESULT="$(sse_ 'first("tool_result")["content"]')"
[ "$(sse_ 'first("tool_result")["status"]')" = "success" ] && echo "$RESULT" | grep -q 7 || die 7 "tool_result: $RESULT"
DONE_TEXT="$(sse_ 'first("done")["text"]')"
echo "$DONE_TEXT" | grep -q '^결과: ' && echo "$DONE_TEXT" | grep -q 7 || die 7 "done.text=$DONE_TEXT"
SID1="$(sse_ 'first("done")["session_id"]')"; USAGE="$(sse_ 'first("done")["usage"]')"
[ "$(sse_ 'first("done")["turn"], first("done")["tool_calls"]')" = "1 1" ] || die 7 "done turn/tool_calls: $(sse_ 'first("done")')"
# 런타임이 MCP Server 소스와 맺은 규약 판 (P35 11절). 5g 의 Gateway 소스와 같은 값이다. $WORK/sse 는 7d 가 읽는다
code="$(api GET "/agents/$AG_ID/metrics")"
[ "$code" = "200" ] || die 7 "GET /metrics -> $code"
MS_PROTOCOLS="$(jq_ 'd["tools"]["protocols"]')"
[ "$(all_modern)" = "True" ] \
  || die 7 "tools.protocols=$MS_PROTOCOLS (expected $MCP_MODERN for the MCP Server source, tools.last_error=$(jq_ 'd["tools"]["last_error"]'))"
# 9t 가 이 턴의 MCP Server 감사 기록을 이 창에서 찾는다
turn_window 7 "$AG_ID" "$SID1" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T7_BEFORE" "$(cluster_now)"
T7_SINCE="$TURN_SINCE"; T7_UNTIL="$TURN_UNTIL"
# 런타임이 위임을 받은 턴은 turn_start 에 그 id 를 싣는다 (P53 6절). 토큰과 비밀은 싣지 않는다
GRANT_NOTE=""
if [ "$AUTH_MODE" != header ]; then
  GRANT7="$(log_events turn_start "$SID1" 'd.get("grant_id") or ""' | head -1 || true)"
  [ -n "$GRANT7" ] || die 7 "turn_start of $SID1 carries no grant_id: the platform opened no turn grant for the MCP Server source"
  GRANT_NOTE="; turn_start grant_id $GRANT7"
fi
pass 7 "chat SSE $NAMES; Add(3,4) -> tool_result '$RESULT'; done.text='$DONE_TEXT' session=$SID1 usage=$USAGE; tools.protocols $MS_PROTOCOLS$GRANT_NOTE"

# ---------- 7d. 인사말 (D65): 새 세션의 첫 이벤트이고 이어지는 턴에는 없다 ----------
# STEP 7 이 새 세션을 열었고 $WORK/sse 는 아직 그 턴의 것이다
TEMPLATE_GREETING="$(json_of "$WORK/templates.json" 'next(n["data"]["greeting"] for n in d[0]["graph"]["nodes"] if n["type"] == "entry")')"
[ "$(sse_ 'names[0]')" = "greeting" ] || die 7d "the first event of the new session $SID1 is not greeting: $NAMES"
[ "$(sse_ 'first("greeting")["session_id"]')" = "$SID1" ] || die 7d "greeting.session_id: $(sse_ 'first("greeting")')"
GREETING_TEXT="$(sse_ 'first("greeting")["text"]')"
{ [ "$GREETING_TEXT" = "$TEMPLATE_GREETING" ] && [ "$STATUS_GREETING" = "$TEMPLATE_GREETING" ]; } \
  || die 7d "greeting '$GREETING_TEXT', status.greeting '$STATUS_GREETING' (expected the template's '$TEMPLATE_GREETING')"
code="$(chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG" "$SID1")")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 7d "the second turn of $SID1 -> $code"; }
{ [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ '"greeting" in names')" = "False" ] && [ "$(sse_ 'first("done")["turn"]')" = "2" ]; } \
  || die 7d "the second turn of $SID1 must end with done and carry no greeting: $(sse_ 'names') turn=$(sse_ 'first("done")["turn"]')"
pass 7d "new session $SID1 opened with greeting '$GREETING_TEXT' (= status.greeting = template); its second turn $(sse_ 'names') has none"

# ---------- 7c. 끊김 (D92, P78): 연결을 끊어도 턴은 끝까지 가고 그 결과가 세션에 남는다 ----------
# 스텁이 기다리는 동안 curl 을 죽인다. 턴은 런타임의 태스크라 플랫폼 프록시가 연결을 닫아도 Add 를 부르고 done 으로 마감한다
read_sid1() { api GET "/agents/$AG_ID/sessions/$SID1"; }
chat_bg "/agents/$AG_ID/chat" "$(msg "$DETACHED_MSG" "$SID1")"
cut_detached 7c read_sid1 chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG" "$SID1")"
# 파드 로그의 그 시도 turn_end 가 done 이다. 로그는 줄마다 읽으므로 awk 가 일찍 닫지 않는다
CUT_END="$(log_events turn_end "$SID1" '"%s %s" % (d.get("turn_id"), d.get("status"))' | awk -v t="$CUT_TURN_ID" '$1 == t && !seen {print $2; seen = 1}' || true)"
[ "$CUT_END" = "done" ] || die 7c "turn_end of the cut turn $CUT_TURN_ID in the pod log: '$CUT_END' (expected done)"
# 위임은 런타임의 /end 가 done 으로 끝낸다. 응답이 연결과 떨어진 턴이라고 알렸으므로 플랫폼은 스트림이 닫혀도 위임을
# 끝내지 않는다 (P53 4절, P78 8절). 끊긴 턴의 위임은 그 응답 머리가 메아리친 id 로 고른다. 끊은 직후 보낸 다음 말도 위임을
# 열고 409 로 refused 가 되며, 시각으로 고르면 이 컴퓨터와 클러스터의 시계 차이에 따라 그것을 집는다
CUT_GRANT_NOTE=""
if [ "$AUTH_MODE" != header ] && platform_mongo_here; then
  [ -n "$CUT_GRANT_ID" ] || die 7c "the cut turn's response carries no X-Daegam-Turn-Grant: $(cat "$WORK/bg.hdr")"
  turn_grants_of "$AG_ID" "$SID1" "$WORK/grants.json" || die 7c "reading the turn grants of session $SID1 failed"
  json_of "$WORK/grants.json" "json.dumps(next((g for g in d if g['_id'] == '$CUT_GRANT_ID'), None))" >"$WORK/cut-grant.json"
  [ "$(json_of "$WORK/cut-grant.json" "bool(d) and bool(d.get('ended_at')) and d.get('end_reason') == 'done'")" = True ] \
    || die 7c "the cut turn's grant did not end with done: $(cat "$WORK/cut-grant.json")"
  CUT_GRANT_NOTE="; its grant ended ($(json_of "$WORK/cut-grant.json" "d['end_reason']"))"
fi
pass 7c "cut $DETACHED_MSG after ${CUT_AFTER}s; the next turn of $SID1 right after the cut -> 409 turn_in_progress; running with the saved Add result ${SAVED_AFTER_MS}ms after the cut; done in the transcript ${DONE_AFTER_MS}ms after the cut; turn_end{status: done}$CUT_GRANT_NOTE"

# ---------- 7e. 중지 API (D65): 연결과 상관없이 진행 중 턴을 멈춘다 ----------
chat_bg "/agents/$AG_ID/chat" "$(msg "$SLEEP_MSG" "$SID1")"
HDR_SID="$(bg_session_id)"
[ "$HDR_SID" = "$SID1" ] || die 7e "X-Daegam-Session-Id on the stream: '$HDR_SID' (expected $SID1): $(cat "$WORK/bg.hdr" 2>/dev/null)"
sleep "$CANCEL_AFTER"
CANCEL_MS="$(now_ms)"
code="$(api POST "/agents/$AG_ID/sessions/$SID1/cancel")"
{ [ "$code" = "202" ] && [ "$(jq_ 'd["session_id"]')" = "$SID1" ]; } || die 7e "POST /sessions/$SID1/cancel during the turn -> $code"
expect_cancelled_stream 7e "$CANCEL_MS"
code="$(api POST "/agents/$AG_ID/sessions/$SID1/cancel")"
{ [ "$code" = "409" ] && [ "$(jq_ 'd["detail"]["code"]')" = "no_turn_in_progress" ]; } \
  || die 7e "cancel without a turn in progress -> $code (expected 409 no_turn_in_progress)"
pass 7e "X-Daegam-Session-Id $HDR_SID; POST /sessions/$SID1/cancel 202 after ${CANCEL_AFTER}s -> the stream ended with error turn_cancelled in ${ENDED_MS}ms; cancel without a turn -> 409 no_turn_in_progress"

# ---------- 7t. 턴 위임 토큰의 수명 (P53 4·6절, local) ----------
# 토큰 수명은 TURN_TTL(60)초이고 스텁은 TURN_SLEEP(75)초 기다린 뒤 Add(5,6) 을 부른다. (1) 런타임이 수명의 1/3 이 남았을 때
# 갱신한 토큰으로 부른다. (2) 같은 턴 도중 관리자를 $TENANT 에서 빼면 갱신이 거절되고 수명이 끝나 Add 는 보내지 않은 채 Tool
# 오류다. Agent 의 이름으로 돌려 보내지도 않는다. (3) 다시 넣으면 다음 턴이 사람의 이름으로 부른다. 감사 기록은 9t 가 한 번에
# 본다
if [ "$AUTH_MODE" = local ]; then
  T7T_BEFORE="$(cluster_now)" || die 7t "reading the platform's clock failed"
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$LONG_ADD_MSG")" -D "$WORK/chat.hdr")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 7t "the ${TURN_SLEEP}s turn -> $code"; }
  { [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ 'first("tool_result")["status"]')" = "success" ] \
    && sse_ 'first("tool_result")["content"]' | grep -q 11; } \
    || die 7t "Add(5,6) after ${TURN_SLEEP}s (token life ${TURN_TTL}s): $(sse_ 'names') $(sse_ 'first("tool_result")')"
  SIDL="$(sse_ 'first("done")["session_id"]')"
  turn_window 7t "$AG_ID" "$SIDL" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T7T_BEFORE" "$(cluster_now)"
  T7T1_SINCE="$TURN_SINCE"; T7T1_UNTIL="$TURN_UNTIL"
  RENEW_NOTE=""
  if platform_mongo_here; then
    turn_grants_of "$AG_ID" "$SIDL" "$WORK/grants.json" || die 7t "reading the turn grants of session $SIDL failed"
    RENEWALS="$(json_of "$WORK/grants.json" 'd[-1].get("renewals") or 0 if d else -1')"
    [ "$RENEWALS" -ge 1 ] || die 7t "the grant of the ${TURN_SLEEP}s turn was not renewed: $(cat "$WORK/grants.json")"
    RENEW_NOTE=" (grant renewed $RENEWALS time(s))"
  fi
  T7T_BEFORE="$(cluster_now)" || die 7t "reading the platform's clock failed"
  chat_bg "/agents/$AG_ID/chat" "$(msg "$LONG_ADD_MSG")"
  sleep "$CUT_AFTER"
  admin_member "" || die 7t "removing the admin from $TENANT during the turn failed"
  bg_ends_by "$(($(now_ms) + (TURN_SLEEP + 30) * 1000))" || die 7t "the ${TURN_SLEEP}s turn did not end within $((TURN_SLEEP + 30))s"
  T7T_AFTER="$(cluster_now)" || die 7t "reading the platform's clock failed"
  admin_member developer || die 7t "making the admin a developer of $TENANT again failed"
  turn_window 7t "$AG_ID" "$(bg_session_id)" "$(hdr x-daegam-turn-grant "$WORK/bg.hdr")" "$T7T_BEFORE" "$T7T_AFTER"
  T7T2_SINCE="$TURN_SINCE"; T7T2_UNTIL="$TURN_UNTIL"
  # 접근을 잃은 사람의 갱신은 403 이라 런타임이 턴을 멈추고 Add 는 보내지 않는다 (P78 7절)
  { [ "$(sse_ 'names[-1]' "$WORK/bg.sse")" = "error" ] && [ "$(sse_ 'first("error")["code"]' "$WORK/bg.sse")" = "$ACCESS_LOST" ] \
    && [ "$(sse_ '"tool_result" in names' "$WORK/bg.sse")" = False ]; } \
    || { cp "$WORK/bg.sse" "$WORK/body"; die 7t "after the admin left $TENANT the turn must stop with error $ACCESS_LOST before Add: $(sse_ 'names' "$WORK/bg.sse")"; }
  T7T_BEFORE="$(cluster_now)" || die 7t "reading the platform's clock failed"
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG")" -D "$WORK/chat.hdr")"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ 'first("tool_result")["status"]')" = "success" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 7t "Add after the admin rejoined $TENANT -> $code $(sse_ 'names')"; }
  turn_window 7t "$AG_ID" "$(sse_ 'first("done")["session_id"]')" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T7T_BEFORE" "$(cluster_now)"
  T7T3_SINCE="$TURN_SINCE"; T7T3_UNTIL="$TURN_UNTIL"
  # 사람이 빠졌던 턴의 창은 그 위임이 열린 때부터 다시 들어온 턴의 위임이 열린 때까지다. 런타임이 /end 를 일찍 불러도
  # 그 턴이 보낸 Add 가 창 밖으로 빠지지 않는다
  T7T2_UNTIL="$T7T3_SINCE"
  pass 7t "token life ${TURN_TTL}s: Add(5,6) after ${TURN_SLEEP}s -> 11$RENEW_NOTE; the admin left $TENANT during the same kind of turn -> stopped with $ACCESS_LOST before Add; rejoined -> Add again"
else
  cond_skip 7t "renewal and the person's checks at renewal are run in local mode, where the platform confirms the person on every renewal"
fi

# ---------- 8. HITL: Divide → interrupt → pending → approve → tool_result → done ----------
if [ "$HAS_MEMORY" = 1 ]; then
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$DIVIDE_MSG")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8 "POST /chat (Divide) -> $code"; }
  NAMES="$(sse_ 'names')"
  [ "$(sse_ 'names[-1]')" = "interrupt" ] || die 8 "stream did not end with interrupt: $NAMES $(cat "$WORK/sse")"
  SID2="$(sse_ 'first("interrupt")["session_id"]')"
  [ "$(sse_ 'first("interrupt")["action_requests"][0]["name"]')" = "Divide" ] || die 8 "interrupt action: $(sse_ 'first("interrupt")')"
  # STEP 5 가 고치기를 더했다 (8e)
  [ "$(sse_ 'first("interrupt")["review_configs"][0]["allowed_decisions"]')" = "['approve', 'edit', 'reject']" ] || die 8 "review_configs: $(sse_ 'first("interrupt")["review_configs"]')"
  code="$(api GET "/agents/$AG_ID/sessions/$SID2/pending")"
  [ "$code" = "200" ] || die 8 "GET /pending -> $code"
  [ "$(jq_ 'd["action_requests"][0]["name"], d["action_requests"][0]["args"]')" = "Divide {'numerator': 8, 'denominator': 2}" ] || die 8 "pending: $(cat "$WORK/body")"
  code="$(api GET "/agents/$AG_ID")"
  [ "$(jq_ 'd["status"]["pending_approvals"]')" = "1" ] || die 8 "status.pending_approvals=$(jq_ 'd["status"]["pending_approvals"]') (expected 1)"
  code="$(chat "/agents/$AG_ID/sessions/$SID2/decisions" '{"decisions": [{"type": "approve"}]}')"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8 "POST /decisions approve -> $code"; }
  NAMES2="$(sse_ 'names')"
  [ "$(sse_ 'names[-1]')" = "done" ] || die 8 "resume did not end with done: $NAMES2 $(cat "$WORK/sse")"
  RESULT="$(sse_ 'first("tool_result")["content"]')"
  [ "$(sse_ 'first("tool_result")["name"], first("tool_result")["status"]')" = "Divide success" ] && echo "$RESULT" | grep -q 4 || die 8 "tool_result after approve: $(sse_ 'first("tool_result")')"
  [ "$(sse_ 'first("done")["session_id"], first("done")["turn"]')" = "$SID2 1" ] || die 8 "done after approve: $(sse_ 'first("done")')"
  code="$(api GET "/agents/$AG_ID/sessions/$SID2")"
  [ "$(jq_ 'd["session"]["status"], d["pending"]')" = "active False" ] || die 8 "session after approve: $(cat "$WORK/body")"
  pass 8 "Divide(8,2): chat $NAMES -> pending {Divide, allowed [approve, edit, reject]}, pending_approvals=1; approve -> $NAMES2 tool_result '$RESULT'; session $SID2 active"
else
  skip 8 "HITL needs the memory node (Postgres); step 4 skipped"
fi

# ---------- 8s. 바깥 내용 뒤의 승인과 기억한 선택 (P43, D96) ----------
# 메인 Agent 는 뒤 단계가 같은 세션에서 Add 를 부르므로 건드리지 않고 따로 띄운다. Add 의 결과는 바깥 내용이고 Divide 의 정적
# 규칙은 기억할 수 있다. 서브에이전트 calc 는 같은 Tool 노드를 쓴다. 기억한 선택은 부른 사람의 것이라 앞 실행이 남긴 이 Agent 의
# 선택을 먼저 지운다(재실행이 PUT 으로 같은 id 를 다시 쓰고, 같은 규칙이면 배포가 그 선택을 걷지 않는다)
# safety_chat SID TEXT -> 그 세션(빈 값이면 새 세션)에 TEXT 를 보낸 상태 코드. 응답은 $WORK/sse 다
safety_chat() { chat "/agents/$SAFE_AG_ID/chat" "$(msg "$2" "$1")"; }
# safety_decide SID DECISION_JSON -> 그 세션의 보류에 결정 하나를 보낸 상태 코드
safety_decide() { chat "/agents/$SAFE_AG_ID/sessions/$1/decisions" "{\"decisions\": [$2]}"; }
# safety_asks STEP LABEL CODE TOOL REASON -> TOOL 을 REASON 으로 묻는 interrupt 로 끝나야 한다. SAFE_SID 에 그 세션을 남긴다
safety_asks() {
  { [ "$3" = 200 ] && asked "$4" "$5"; } \
    || { cp "$WORK/sse" "$WORK/body"; die "$1" "$2: expected $4 asked with reason $5, got $3 $(sse_ 'names') $(sse_ 'first("interrupt")')"; }
  SAFE_SID="$(sse_ 'first("interrupt")["session_id"]')"
}
# safety_runs STEP LABEL CODE TOOL STATUS -> 묻지 않고 TOOL 의 결과가 STATUS 로 오고 done 으로 끝나야 한다. SAFE_SID 를 남긴다
safety_runs() {
  { [ "$3" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ '"interrupt" in names')" = False ] \
    && [ "$(sse_ 'first("tool_result")["name"], first("tool_result")["status"]')" = "$4 $5" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die "$1" "$2: expected $4 to end $5 without asking, got $3 $(sse_ 'names') $(sse_ 'first("tool_result")')"; }
  SAFE_SID="$(sse_ 'first("done")["session_id"]')"
}
# safety_offers STEP SID WANT -> 그 세션의 GET /pending 이 기억할 수 있는 선택으로 WANT(파이썬 목록 글)를 알려야 한다
safety_offers() {
  local code
  code="$(api GET "/agents/$SAFE_AG_ID/sessions/$2/pending")"
  { [ "$code" = 200 ] && [ "$(jq_ 'd.get("remember_options"), bool(d.get("remember_days"))')" = "$3 True" ]; } \
    || die "$1" "GET /pending of $2 -> $code $(cat "$WORK/body") (expected remember_options $3 and remember_days)"
}
# safety_choices STEP -> 이 Agent 에 대한 내 기억한 선택을 읽어 SAFE_CHOICES("Tool:선택" 을 정렬해 공백으로 이은 것)와
# SAFE_CHOICE_IDS 에 남긴다. GET 이 200 이 아니면 die STEP 이다. 값을 변수에 남기므로 $( ) 안에서 부르지 않는다
safety_choices() {
  local code
  code="$(api GET /me/approval-choices)"
  [ "$code" = 200 ] || die "$1" "GET /me/approval-choices -> $code $(cat "$WORK/body")"
  SAFE_CHOICE_IDS="$(jq_ "' '.join(c['id'] for c in d['choices'] if c['agent_id'] == '$SAFE_AG_ID')")"
  SAFE_CHOICES="$(jq_ "' '.join(sorted(c['tool'] + ':' + c['choice'] for c in d['choices'] if c['agent_id'] == '$SAFE_AG_ID'))")"
}
# safety_forget STEP -> 이 Agent 에 대한 내 선택을 계정 라우트로 하나씩 지운다(204)
safety_forget() {
  local code id
  safety_choices "$1"
  for id in $SAFE_CHOICE_IDS; do
    code="$(api DELETE "/me/approval-choices/$id")"
    [ "$code" = 204 ] || die "$1" "DELETE /me/approval-choices/$id -> $code $(cat "$WORK/body")"
  done
}
if [ "$HAS_MEMORY" = 1 ]; then
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/safety-agent.json" --name "$SAFE_AG_NAME" \
    --display "$SAFE_AG_DISPLAY" --provider "$STUB_PROVIDER" --source "mcp_instance:$MS_ID" --ds-id "$DS_ID" \
    --sub-name "$SAFE_SUB" --sub-tools --outside Add --rememberable Divide
  save_doc 8s /agents "$SAFE_AG_NAME" "$(cat "$WORK/safety-agent.json")"
  SAFE_AG_ID="$SAVED_ID"
  deploy_ready 8s /agents "$SAFE_AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  require_features 8s tool_safety
  safety_forget 8s
  # 세션 A: 정적 규칙의 승인을 기억하면 바깥 내용이 없는 대화에서 다시 묻지 않는다
  safety_asks 8s "Divide in a new session" "$(safety_chat "" "$DIVIDE_MSG")" Divide rule
  SID_A="$SAFE_SID"; SAFE_SIDS="$SID_A"
  safety_offers 8s "$SID_A" "[['approve', 'reject']]"
  code="$(safety_decide "$SID_A" '{"type": "approve", "remember": "approve"}')"
  divide_done "$code" || { cp "$WORK/sse" "$WORK/body"; die 8s "approve with remember -> $code $(sse_ 'names')"; }
  safety_runs 8s "Divide after the remembered approve" "$(safety_chat "$SID_A" "$DIVIDE_MSG")" Divide success
  safety_choices 8s
  [ "$SAFE_CHOICES" = "Divide:approve" ] || die 8s "my remembered choices for $SAFE_AG_NAME: [$SAFE_CHOICES] (expected Divide:approve)"
  # 세션 B: 바깥 내용이 들어온 대화에서는 기억한 승인이 적용되지 않고, 기억한 거부는 묻지 않고 거절한다
  safety_runs 8s "Add marked outside" "$(safety_chat "" "$ADD_MSG")" Add success
  SID_B="$SAFE_SID"; SAFE_SIDS="$SAFE_SIDS $SID_B"
  [ "$(sse_ 'first("tool_result").get("untrusted")')" = outside ] || die 8s "the Add result is not marked outside: $(sse_ 'first("tool_result")')"
  safety_asks 8s "Divide after outside content" "$(safety_chat "$SID_B" "$DIVIDE_MSG")" Divide outside
  safety_offers 8s "$SID_B" "[['reject']]"
  # 바깥 내용 때문에 물은 요청의 승인은 기억하지 않는다. 플랫폼이 런타임에 보내기 전에 거절하므로 보류는 그대로다
  code="$(safety_decide "$SID_B" '{"type": "approve", "remember": "approve"}')"
  { [ "$code" = 422 ] && [ "$(json_of "$WORK/sse" 'd["detail"]["code"]')" = remember_refused ] \
    && [[ "$(json_of "$WORK/sse" 'd["detail"]["detail"]')" == *"outside content"* ]]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 8s "remember approve on a request asked for outside content -> $code (expected 422 remember_refused for outside content)"; }
  code="$(safety_decide "$SID_B" '{"type": "reject", "remember": "reject"}')"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ]; } || { cp "$WORK/sse" "$WORK/body"; die 8s "reject with remember -> $code $(sse_ 'names')"; }
  safety_runs 8s "Divide after the remembered reject" "$(safety_chat "$SID_B" "$DIVIDE_MSG")" Divide error
  [ "$(sse_ 'json.loads(first("tool_result")["content"])["code"]')" = tool_choice_rejected ] \
    || die 8s "the remembered reject did not refuse Divide: $(sse_ 'first("tool_result")')"
  safety_choices 8s
  [ "$SAFE_CHOICES" = "Divide:reject" ] || die 8s "my remembered choices after the reject: [$SAFE_CHOICES] (expected Divide:reject, which replaced the approve)"
  # 세션 C: 규칙 없는 쓰기도 바깥 내용 뒤에는 묻는다
  safety_runs 8s "Add in a new session" "$(safety_chat "" "$ADD_MSG")" Add success
  SID_C="$SAFE_SID"; SAFE_SIDS="$SAFE_SIDS $SID_C"
  safety_asks 8s "Add after outside content" "$(safety_chat "$SID_C" "$ADD_MSG")" Add outside
  code="$(safety_decide "$SID_C" '{"type": "reject"}')"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ]; } || { cp "$WORK/sse" "$WORK/body"; die 8s "reject in $SID_C -> $code $(sse_ 'names')"; }
  # 세션 D: 서브에이전트가 받은 바깥 내용은 그 task 결과를 바깥 내용으로 만들고, 메인의 다음 쓰기가 묻는다
  code="$(safety_chat "" "$(task_call "$SAFE_SUB" "$ADD_MSG")")"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] \
    && [ "$(sse_ 'next((r.get("untrusted") for r in every("tool_result") if r["name"] == "task"), None)')" = outside ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 8s "the task result after the subagent's Add is not marked outside: $code $(sse_ 'names') $(sse_ 'every("tool_result")')"; }
  SID_D="$(sse_ 'first("done")["session_id"]')"; SAFE_SIDS="$SAFE_SIDS $SID_D"
  safety_asks 8s "the main agent's Add after the subagent read outside content" "$(safety_chat "$SID_D" "$ADD_MSG")" Add outside
  code="$(safety_decide "$SID_D" '{"type": "reject"}')"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ]; } || { cp "$WORK/sse" "$WORK/body"; die 8s "reject in $SID_D -> $code $(sse_ 'names')"; }
  # 계정 화면의 지우기: 남은 하나(Divide reject)를 지우면 목록에서 빠진다
  safety_forget 8s
  safety_choices 8s
  [ -z "$SAFE_CHOICES" ] || die 8s "choices left after deleting them: [$SAFE_CHOICES]"
  drop_safety_agent
  pass 8s "agent $SAFE_AG_NAME (tool_safety): A Divide asked (rule) offering [approve, reject], remembered approve -> next Divide ran unasked; B Add marked outside -> Divide asked (outside) offering [reject], remember approve there 422 remember_refused, remembered reject -> next Divide refused (tool_choice_rejected) and the only choice left; C Add after Add asked (outside); D the subagent's Add made the task result outside -> the main Add asked (outside); Divide reject deleted on the account route; agent removed"
else
  skip 8s "approvals need the memory node (Postgres); step 4 skipped"
fi

# ---------- 8e. 고치기 (D65): edit 은 같은 도구의 인자만 바꾼다 ----------
if [ "$HAS_MEMORY" = 1 ]; then
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$DIVIDE_MSG")")"
  { [ "$code" = "200" ] && [ "$(sse_ 'names[-1]')" = "interrupt" ]; } || { cp "$WORK/sse" "$WORK/body"; die 8e "Divide chat -> $code $(sse_ 'names')"; }
  SIDE="$(sse_ 'first("interrupt")["session_id"]')"
  # 다른 도구로 바꾸는 고치기는 재개하지 않는다. 플랫폼은 런타임의 422 를 그대로 옮긴다
  code="$(chat "/agents/$AG_ID/sessions/$SIDE/decisions" "$(edit_body Add '{"a": 9, "b": 3}')")"
  { [ "$code" = "422" ] && [ "$(json_of "$WORK/sse" 'd["detail"]["code"]')" = "edit_name_mismatch" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 8e "edit renaming Divide to Add -> $code (expected 422 edit_name_mismatch)"; }
  code="$(api GET "/agents/$AG_ID/sessions/$SIDE")"
  [ "$(jq_ 'd["session"]["status"], d["pending"]')" = "pending True" ] || die 8e "session after the rejected edit: $(jq_ 'd["session"]["status"], d["pending"]') (expected pending True)"
  code="$(chat "/agents/$AG_ID/sessions/$SIDE/decisions" "$(edit_body Divide "$EDIT_ARGS")")"
  { [ "$code" = "200" ] && [ "$(sse_ 'names[-1]')" = "done" ]; } || { cp "$WORK/sse" "$WORK/body"; die 8e "edit Divide $EDIT_ARGS -> $code $(sse_ 'names')"; }
  # 재개가 다시 알린 tool_call 과 결과가 고친 호출의 것이다. 결과 앞에는 langchain 의 고치기 안내문이 붙는다
  [ "$(sse_ "first('tool_call')['args'] == json.loads('$EDIT_ARGS')")" = "True" ] || die 8e "re-announced tool_call args: $(sse_ 'first("tool_call")')"
  RESULT="$(sse_ 'first("tool_result")["content"]')"
  { [ "$(sse_ 'first("tool_result")["name"], first("tool_result")["status"]')" = "Divide success" ] \
    && [ "$(sse_ "'Executed instead: Divide' in first('tool_result')['content'] and '$EDIT_RESULT' in first('tool_result')['content'].split('Tool response:')[-1]")" = "True" ]; } \
    || die 8e "tool_result after the edit: $RESULT (expected the langchain edit notice, then $EDIT_RESULT)"
  code="$(api GET "/agents/$AG_ID/sessions/$SIDE")"
  [ "$(jq_ "[i['call']['args'] for i in d['transcript']['items'] if i['kind'] == 'tool_call' and i['call']['name'] == 'Divide'] == [json.loads('$EDIT_ARGS')]")" = "True" ] \
    || die 8e "the Divide card in the transcript: $(jq_ "[i['call'] for i in d['transcript']['items'] if i['kind'] == 'tool_call']")"
  pass 8e "Divide pending in $SIDE: edit to Add -> 422 edit_name_mismatch and the session stays pending; edit Divide $EDIT_ARGS -> re-announced tool_call and the transcript card carry the edited args, tool_result ends with $EDIT_RESULT after the langchain notice"
else
  skip 8e "HITL needs the memory node (Postgres); step 4 skipped"
fi

# ---------- 8t·8u·8v. 승인자의 재개가 누구의 이름으로 가는가 (P53 5.3절, local) ----------
# 관리자(소유자)의 Divide 보류를 승인자가 정한다. 고친 것 없는 승인은 소유자가 확인을 지나면 소유자의 이름으로 가고 승인자는
# 위임 기록의 decided_by 에 남는다(8t). 소유자가 테넌트에서 빠졌으면(8u) 또는 승인자가 인자를 고쳤으면(8v) 승인자 자신의
# 위임이고 소유자는 session_owner 다. Divide 의 감사 기록은 9t 가 본다
if [ "$AUTH_MODE" = local ] && [ "$HAS_MEMORY" = 1 ]; then
  APPROVER_TOKEN="$(local_login_any "$APPROVER_NAME" "$LOCAL_PW")"
  [ -n "$APPROVER_TOKEN" ] || die 8t "login as $APPROVER_NAME failed"
  pending_divide 8t; SIDT="$PENDING_SID"
  T8_BEFORE="$(cluster_now)" || die 8t "reading the platform's clock failed"
  code="$(chat_as "$APPROVER_TOKEN" "/agents/$AG_ID/sessions/$SIDT/decisions" '{"decisions": [{"type": "approve"}]}' -D "$WORK/chat.hdr")"
  divide_done "$code" \
    || { cp "$WORK/sse" "$WORK/body"; die 8t "the approver's approve of $SIDT -> $code $(sse_ 'names')"; }
  turn_window 8t "$AG_ID" "$SIDT" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T8_BEFORE" "$(cluster_now)"
  T8T_SINCE="$TURN_SINCE"; T8T_UNTIL="$TURN_UNTIL"
  resume_grant 8t "$SIDT" "g['sub'] == '$ADMIN_SUB' and g.get('decided_by') == '$APPROVER_SUB' and not g.get('session_owner')"
  pass 8t "$APPROVER_NAME approved the admin's Divide in $SIDT -> done; the resume runs as the owner: $GRANT_NOTE"
  pending_divide 8u; SIDU="$PENDING_SID"
  admin_member "" || die 8u "removing the admin from $TENANT before the approval failed"
  T8_BEFORE="$(cluster_now)" || die 8u "reading the platform's clock failed"
  code="$(chat_as "$APPROVER_TOKEN" "/agents/$AG_ID/sessions/$SIDU/decisions" '{"decisions": [{"type": "approve"}]}' -D "$WORK/chat.hdr")"
  T8_AFTER="$(cluster_now)" || die 8u "reading the platform's clock failed"
  admin_member developer || die 8u "making the admin a developer of $TENANT again failed"
  divide_done "$code" \
    || { cp "$WORK/sse" "$WORK/body"; die 8u "the approver's approve of $SIDU after the owner left $TENANT -> $code $(sse_ 'names')"; }
  turn_window 8u "$AG_ID" "$SIDU" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T8_BEFORE" "$T8_AFTER"
  T8U_SINCE="$TURN_SINCE"; T8U_UNTIL="$TURN_UNTIL"
  resume_grant 8u "$SIDU" "g['sub'] == '$APPROVER_SUB' and g.get('session_owner') == '$ADMIN_SUB'"
  pass 8u "the admin left $TENANT before $APPROVER_NAME approved $SIDU -> done under the approver's own grant: $GRANT_NOTE"
  pending_divide 8v; SIDV="$PENDING_SID"
  T8_BEFORE="$(cluster_now)" || die 8v "reading the platform's clock failed"
  code="$(chat_as "$APPROVER_TOKEN" "/agents/$AG_ID/sessions/$SIDV/decisions" "$(edit_body Divide "$EDIT_ARGS")" -D "$WORK/chat.hdr")"
  divide_done "$code" \
    || { cp "$WORK/sse" "$WORK/body"; die 8v "the approver's edit of $SIDV -> $code $(sse_ 'names')"; }
  turn_window 8v "$AG_ID" "$SIDV" "$(hdr x-daegam-turn-grant "$WORK/chat.hdr")" "$T8_BEFORE" "$(cluster_now)"
  T8V_SINCE="$TURN_SINCE"; T8V_UNTIL="$TURN_UNTIL"
  resume_grant 8v "$SIDV" "g['sub'] == '$APPROVER_SUB' and g.get('session_owner') == '$ADMIN_SUB'"
  pass 8v "$APPROVER_NAME edited the admin's Divide in $SIDV to $EDIT_ARGS -> done under the approver's own grant: $GRANT_NOTE"
elif [ "$AUTH_MODE" = local ]; then
  skip 8t "an approver's resume needs the HITL turn (memory node); step 4 skipped"
else
  cond_skip 8t "an approver's resume is checked in local mode, where the platform confirms the owner again (8t·8u·8v)"
fi

# ---------- 8m. 장기 기억 (D34): 세션을 건너 남고, 임시 턴은 안 남는다 ----------
# 스텁은 내장 파일 도구를 스스로 고르지 않으므로 메시지 안 JSON 조각으로 이름을 직접 댄다(runtime/stub.py).
if [ "$HAS_MEMORY" = 1 ]; then
  MEM_PATH="/memories/e2e-note.md"; MEM_TEXT="바다거북-$(date +%s)"
  EPH_PATH="/memories/e2e-ephemeral.md"; EPH_TEXT="임시메모-$(date +%s)"
  MEM_PREFIX="$TENANT.$AG_ID"           # Postgres store 의 prefix 열 (네임스페이스를 점으로 이은 것)
  MEM_KEYS="daegam-agent:$AG_ID:store*" # Redis 의 store 키 접두어
  # (1) 저장소 자리는 파드보다 먼저 플랫폼이 만든다. Postgres 는 provision 의 CREATE SCHEMA daegam_store 다
  SCHEMA_NOTE="redis 는 스키마가 없다"
  if [ "$MEMORY_KIND" = postgres ]; then
    [ "$(psql_ "select count(*) from information_schema.schemata where schema_name='daegam_store'")" = "1" ] \
      || die 8m "schema daegam_store missing: provision must create it before the pod starts (없으면 런타임이 long_term_schema_missing 으로 기동을 막는다)"
    [ "$(psql_ "select count(*) from pg_tables where schemaname='daegam_store' and tablename='store'")" = "1" ] \
      || die 8m "table daegam_store.store missing: the runtime makes it with the langgraph store migration at startup"
    SCHEMA_NOTE="daegam_store 스키마(플랫폼)와 store 테이블(런타임 마이그레이션)"
  fi
  # (2) 한 세션에서 /memories/ 아래에 쓴다
  code="$(chat "/agents/$AG_ID/chat" "$(mem_msg write_file file_path "$MEM_PATH" content "$MEM_TEXT")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8m "POST /chat (write_file) -> $code"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || die 8m "write turn did not end with done: $(sse_ 'names') $(cat "$WORK/sse")"
  [ "$(sse_ 'first("tool_call")["name"]')" = "write_file" ] || die 8m "tool_call is not write_file: $(sse_ 'names') (파일 도구가 안 붙었으면 builtin_tools.filesystem 을 보라)"
  [ "$(sse_ 'first("tool_result")["status"]')" = "success" ] || die 8m "write_file tool_result: $(sse_ 'first("tool_result")')"
  SIDM1="$(sse_ 'first("done")["session_id"]')"
  # (3) 다른 세션에서 읽힌다. 이것이 D34 의 완료 조건 1 이다
  code="$(chat "/agents/$AG_ID/chat" "$(mem_msg read_file file_path "$MEM_PATH")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8m "POST /chat (read_file in a new session) -> $code"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || die 8m "read turn did not end with done: $(sse_ 'names') $(cat "$WORK/sse")"
  SIDM2="$(sse_ 'first("done")["session_id"]')"
  { [ -n "$SIDM2" ] && [ "$SIDM2" != "$SIDM1" ]; } || die 8m "the read turn reused the write session ($SIDM1 / $SIDM2); 새 세션이어야 기억을 증명한다"
  MEM_DONE="$(sse_ 'first("done")["text"]')"
  echo "$MEM_DONE" | grep -q -F "$MEM_TEXT" \
    || die 8m "session $SIDM2 did not read back what session $SIDM1 wrote: done.text=$MEM_DONE"
  # (4) 저장소에 이 인스턴스의 네임스페이스로 남아 있다
  if [ "$MEMORY_KIND" = postgres ]; then
    MEM_ROWS="$(psql_ "select count(*) from daegam_store.store where prefix like '$MEM_PREFIX%'")"
    [ "${MEM_ROWS:-0}" -ge 1 ] || die 8m "no daegam_store.store row with prefix like '$MEM_PREFIX%' (got '${MEM_ROWS:-}')"
    STORE_NOTE="daegam_store.store 에 prefix '${MEM_PREFIX}…' 행 $MEM_ROWS"
  else
    MEM_ROWS="$(rcli --scan --pattern "$MEM_KEYS" | grep -c . || true)"
    [ "${MEM_ROWS:-0}" -ge 1 ] || die 8m "no redis key matching $MEM_KEYS"
    STORE_NOTE="redis 키 $MEM_KEYS $MEM_ROWS 개"
  fi
  # (5) 임시 턴은 저장소를 안 붙인다. 쓰기는 그 세션 안에서 성공하고 다음 세션에는 없다
  code="$(chat "/agents/$AG_ID/chat" "$(mem_msg --ephemeral write_file file_path "$EPH_PATH" content "$EPH_TEXT")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8m "POST /chat (ephemeral write_file) -> $code"; }
  [ "$(sse_ 'first("tool_result")["status"]')" = "success" ] || die 8m "ephemeral write_file tool_result: $(sse_ 'first("tool_result")')"
  code="$(chat "/agents/$AG_ID/chat" "$(mem_msg read_file file_path "$EPH_PATH")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 8m "POST /chat (read back the ephemeral file) -> $code"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || die 8m "ephemeral read turn did not end with done: $(sse_ 'names') $(cat "$WORK/sse")"
  EPH_DONE="$(sse_ 'first("done")["text"]')"
  ! echo "$EPH_DONE" | grep -q -F "$EPH_TEXT" \
    || die 8m "the ephemeral turn left '$EPH_TEXT' in the store; 임시 턴의 /memories/ 는 세션과 함께 사라져야 한다 (done.text=$EPH_DONE)"
  # (6) 사용자별 삭제는 그 사람의 네임스페이스만 건드린다 (P17 F2b). 이 두 모드의 호출자는 tenant admin
  #     이라 남의 것도 지울 수 있으므로(403 not_memory_owner 는 platform/tests/test_rbac.py 가 본다)
  #     여기서 볼 것은 없는 사람을 지우면 0 이고 내 기억이 그대로라는 것이다
  code="$(api DELETE "/agents/$AG_ID/memories/someone-else-e2e")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["deleted"]')" = "0" ]; } \
    || die 8m "DELETE /memories/someone-else-e2e -> $code $(cat "$WORK/body") (expected 200 deleted=0)"
  if [ "$MEMORY_KIND" = postgres ]; then
    AFTER_ROWS="$(psql_ "select count(*) from daegam_store.store where prefix like '$MEM_PREFIX%'")"
  else
    AFTER_ROWS="$(rcli --scan --pattern "$MEM_KEYS" | grep -c . || true)"
  fi
  [ "$AFTER_ROWS" = "$MEM_ROWS" ] \
    || die 8m "deleting another user's memory moved this user's ($MEM_ROWS -> $AFTER_ROWS); 네임스페이스가 사람마다 갈려야 한다"
  pass 8m "long-term memory: $SCHEMA_NOTE; 세션 $SIDM1 이 쓴 '$MEM_TEXT' 를 새 세션 $SIDM2 가 읽었다 ($STORE_NOTE); ephemeral 턴이 쓴 '$EPH_TEXT' 는 다음 세션에 없다; DELETE /memories/<없는 사람> -> deleted=0 이고 내 기억은 $AFTER_ROWS 그대로"
else
  skip 8m "long-term memory needs the memory node (Postgres/Redis); step 4 skipped"
fi

# ---------- 8r. (redis) 키·인덱스 단정 셋 (D33). 같은 lab Redis 를 쓰는 다른 인스턴스의 키가 있어도 통과한다 ----------
if [ "$MEMORY_KIND" = redis ]; then
  # (1) 체크포인트·쓰기·세션 색인이 이 인스턴스의 접두어 아래에 있다
  INST_KEYS="$(rcli --scan --pattern "daegam-agent:$AG_ID:*")" || die 8r "redis-cli --scan via kubectl exec deploy/redis failed"
  N_KEYS="$(printf '%s\n' "$INST_KEYS" | grep -c . || true)"
  N_CKPT="$(printf '%s\n' "$INST_KEYS" | grep -c -F ':checkpoint:' || true)"
  N_WRITE="$(printf '%s\n' "$INST_KEYS" | grep -c -F ':checkpoint_write:' || true)"
  N_SESS="$(printf '%s\n' "$INST_KEYS" | grep -c -F ':session:' || true)"
  [ "$N_CKPT" -ge 1 ] && [ "$N_WRITE" -ge 1 ] && [ "$N_SESS" -ge 1 ] \
    || die 8r "keys under daegam-agent:$AG_ID:* lack :checkpoint:/:checkpoint_write:/:session: ($N_CKPT/$N_WRITE/$N_SESS of $N_KEYS): $(printf '%s\n' "$INST_KEYS" | head -20 | tr '\n' ' ')"
  # (2) 라이브러리의 쓰기 레지스트리는 접두어 밖의 루트에 있고 thread_id 가 이 인스턴스의 세션 id 다 (sid 마다 비어 있지 않다)
  ZSET_NOTE=""
  for sid in "$SID1" "$SID2"; do
    n="$(rcli --scan --pattern "write_keys_zset:$sid:*" | grep -c . || true)"
    [ "$n" -ge 1 ] || die 8r "no write_keys_zset:$sid:* key for session $sid"
    ZSET_NOTE="$ZSET_NOTE $sid=$n"
  done
  # (3) FT 인덱스 이름은 접두어와 같다. 체크포인터가 둘이고 장기 기억(D34)의 store 가 하나 더다
  FT_LIST="$(rcli FT._LIST)" || die 8r "redis-cli FT._LIST failed (is the lab Redis 8 with RediSearch?)"
  for idx in $LAB_REDIS_INDEXES; do
    printf '%s\n' "$FT_LIST" | grep -q -x -F "daegam-agent:$AG_ID:$idx" || die 8r "FT._LIST lacks daegam-agent:$AG_ID:$idx: $(printf '%s\n' "$FT_LIST" | tr '\n' ' ')"
  done
  N_IDX="$(printf '%s\n' "$FT_LIST" | grep -c -F "daegam-agent:$AG_ID:" || true)"
  N_IDX_WANT="$(printf '%s\n' $LAB_REDIS_INDEXES | grep -c . || true)"
  [ "$N_IDX" = "$N_IDX_WANT" ] || die 8r "FT._LIST has $N_IDX indexes under daegam-agent:$AG_ID: (expected exactly $N_IDX_WANT: $LAB_REDIS_INDEXES): $(printf '%s\n' "$FT_LIST" | tr '\n' ' ')"
  pass 8r "redis keys daegam-agent:$AG_ID:* = $N_KEYS (checkpoint $N_CKPT, checkpoint_write $N_WRITE, session $N_SESS); write_keys_zset per session:$ZSET_NOTE; FT._LIST has exactly $N_IDX_WANT indexes for this instance [$LAB_REDIS_INDEXES] (of $(printf '%s\n' "$FT_LIST" | grep -c . || true) total)"
fi

# ---------- 8j. 판정 모델이 바깥 결과를 먼저 본다 (P43 7절) ----------
python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/judge-agent.json" --name "$JUDGE_AG_NAME" \
  --display "$JUDGE_AG_DISPLAY" --provider "$STUB_PROVIDER" --source "mcp_instance:$MS_ID" --outside Add --judge
save_doc 8j /agents "$JUDGE_AG_NAME" "$(cat "$WORK/judge-agent.json")"
JUDGE_AG_ID="$SAVED_ID"
deploy_ready 8j /agents "$JUDGE_AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
require_features 8j tool_safety injection_check
code="$(chat "/agents/$JUDGE_AG_ID/chat" "$(msg "$JUDGE_MSG" "")")"
{ [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] \
  && [ "$(sse_ 'first("tool_result")["status"], json.loads(first("tool_result")["content"])["code"]')" = "error injection_blocked" ] \
  && [ "$(sse_ 'first("guardrail")["rule"], first("guardrail")["action"]')" = "injection_check block" ]; } \
  || { cp "$WORK/sse" "$WORK/body"; die 8j "Add(31000, 337) was not blocked by the judge: $code $(sse_ 'names') $(sse_ 'first("tool_result")')"; }
code="$(chat "/agents/$JUDGE_AG_ID/chat" "$(msg "$ADD_MSG" "")")"
{ [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ '"guardrail" in names')" = False ] \
  && [ "$(sse_ 'first("tool_result")["status"], first("tool_result").get("untrusted")')" = "success outside" ]; } \
  || { cp "$WORK/sse" "$WORK/body"; die 8j "Add(3,4) after the judge: $code $(sse_ 'names') $(sse_ 'first("tool_result")')"; }
drop_judge_agent
pass 8j "agent $JUDGE_AG_NAME (tool_safety, injection_check): Add(31000,337) blocked by the stub judge (injection_blocked, guardrail injection_check block); Add(3,4)=7 passed as outside content; agent removed"

# ---------- 9. sessions, metrics, events ----------
code="$(api GET "/agents/$AG_ID/sessions")"
[ "$code" = "200" ] || die 9 "GET /sessions -> $code"
N_SESSIONS="$(jq_ 'len(d["sessions"])')"
[ "$N_SESSIONS" -ge 1 ] || die 9 "no sessions listed: $(cat "$WORK/body")"
echo "$(jq_ '[s["session_id"] for s in d["sessions"]]')" | grep -q "$SID1" || die 9 "session $SID1 not in list"
# 이벤트는 AuditCollector 가 60초 주기로 파드 stdout 에서 모은다. STEP 7 세션의 것이 들어올 때까지 기다린다. 그 세션에는
# 끝난 턴(7·7d·7c)과 멈춘 턴(7e)이 함께 있다
SID1_EVENTS_IN='{e.get("status") for e in d if e["event"] == "turn_end"} >= {"done", "cancelled"} and any(e["event"] == "tool_exec" for e in d)'
N_EVENTS=0
for ((i = 0; i < 30; i++)); do
  code="$(api GET "/agents/$AG_ID/events?session_id=$SID1&limit=100")"
  [ "$code" = "200" ] || die 9 "GET /events -> $code"
  N_EVENTS="$(jq_ 'len(d)')"
  [ "$(jq_ "$SID1_EVENTS_IN")" = "True" ] && break
  sleep 5
done
[ "$(jq_ "$SID1_EVENTS_IN")" = "True" ] || die 9 "events for session $SID1 lack turn_end done·cancelled or tool_exec after 150s: $(jq_ '[(e["event"], e.get("status")) for e in d]')"
[ "$(jq_ 'sorted({e.get("session_id") for e in d}, key=str)')" = "['$SID1']" ] || die 9 "events filter by session_id leaked other sessions: $(jq_ '{e.get("session_id") for e in d}')"
EVENT_KINDS="$(jq_ 'sorted({e["event"] for e in d})')"
[ "$(jq_ 'sorted({e.get("status") for e in d if e["event"]=="turn_end"}), sorted({e.get("tool") for e in d if e["event"]=="tool_exec"})')" = "['cancelled', 'done'] ['Add']" ] \
  || die 9 "turn_end/tool_exec of session $SID1: $(cat "$WORK/body")"
# 지표의 턴·Tool 수는 이벤트 저장소에서 센다(P37 11절). 응답은 15초 기억되므로 이벤트가 든 뒤의 값을 기다린다
METRICS_FULL='d["turns"]["done"] >= 1 and d["tool_calls"]["calls"] >= 1 and d["totals"]["input_tokens"] > 0 and d["tools"]["count"] >= 1'
[ "$HAS_MEMORY" = 1 ] && METRICS_FULL="$METRICS_FULL"' and d["turns"]["done"] >= 2 and d["turns"]["pending"] >= 1 and d["tool_calls"]["calls"] >= 2'
for ((i = 0; i < 12; i++)); do
  code="$(api GET "/agents/$AG_ID/metrics")"
  [ "$code" = "200" ] || die 9 "GET /metrics -> $code"
  [ "$(jq_ "$METRICS_FULL")" = "True" ] && break
  sleep 5
done
[ "$(jq_ "$METRICS_FULL")" = "True" ] || die 9 "metrics not populated after 60s: $(cat "$WORK/body")"
METRICS="$(jq_ 'd["sessions"], d["totals"], d["turns"], d["tool_calls"]["calls"], d["latency_ms"], d["tools"]["count"], d["telemetry"]["mode"]')"
pass 9 "sessions=$N_SESSIONS; metrics sessions/totals/turns/tool_calls/latency/tools: $METRICS; events(session $SID1)=$N_EVENTS kinds=$EVENT_KINDS, turn_end done·cancelled"

# ---------- 9t. MCP Server 의 감사 기록이 사람과 Agent 를 함께 남긴다 (P53 7절) ----------
# 앞 단계가 적어 둔 창마다 MCP Server 프록시의 tools/call 기록을 본다. 수집기가 60초마다 읽으므로 한 번에 기다린다
if [ "$AUTH_MODE" = header ]; then
  cond_skip 9t "header mode mints no turn token, so the MCP audit names the Agent alone"
else
  AGENT_ACT="svc:agent:$AG_K8S"
  AUDIT_SPECS=("STEP 7 Add|$T7_SINCE|$T7_UNTIL|$MS_ID|Add|$ADMIN_SUB|$AGENT_ACT|1")
  if [ "$AUTH_MODE" = local ]; then
    AUDIT_SPECS+=("7t renewed Add|$T7T1_SINCE|$T7T1_UNTIL|$MS_ID|Add|$ADMIN_SUB|$AGENT_ACT|1"
      "7t lapsed Add|$T7T2_SINCE|$T7T2_UNTIL|$MS_ID|Add|*|*|0"
      "7t rejoined Add|$T7T3_SINCE|$T7T3_UNTIL|$MS_ID|Add|$ADMIN_SUB|$AGENT_ACT|1")
    if [ "$HAS_MEMORY" = 1 ]; then
      AUDIT_SPECS+=("8t Divide|$T8T_SINCE|$T8T_UNTIL|$MS_ID|Divide|$ADMIN_SUB|$AGENT_ACT|1"
        "8u Divide|$T8U_SINCE|$T8U_UNTIL|$MS_ID|Divide|$APPROVER_SUB|$AGENT_ACT|1"
        "8v Divide|$T8V_SINCE|$T8V_UNTIL|$MS_ID|Divide|$APPROVER_SUB|$AGENT_ACT|1")
    fi
  fi
  audit_expect 9t "$T7_SINCE" "${AUDIT_SPECS[@]}"
  pass 9t "MCP Server $MS_ID audit: ${#AUDIT_SPECS[@]} windows as expected (person in sub, $AGENT_ACT in act; nothing sent while the person had left)"
fi

# ---------- 9r. API 서버 경로 (P35 14절): 직행이 아닌 클러스터의 런타임 호출은 pods/portforward 다 ----------
# deploy/dev/lab/agent_route.py 가 이 kubecontext 의 클라이언트(via=apiserver)와 플랫폼 프록시가 쓰는 bearer_service_client
# 로 /metrics·/chat(Add)을 부르고, 끊어도 끝까지 가는 턴을 2초 뒤 끊어 다음 턴의 409 와 가벼운 읽기의 결과·done 을 본다.
# 플랫폼 venv 로 돈다. 그 길은 인증 모드도 memory 종류도 읽지 않으므로 E2E_SHARED_STEPS=skip 이면 make e2e-agent 만
# 돈다(BASE_RUN)
if [ "$BASE_RUN" = 0 ]; then
  cond_skip 9r "E2E_SHARED_STEPS=skip: the API server path reads neither the auth mode nor the memory kind; make e2e-agent runs it"
elif uv run --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/agent_route.py" --context "$CTX" \
  "$AG_NS" "$AG_K8S" "$AG_TOKEN_SECRET" >"$WORK/body" 2>"$WORK/route.err"; then
  ROUTE_SID="$(jq_ 'd["chat"]["session_id"]')"
  pass 9r "$(jq_ '"via %s, protocols %s; chat %s -> %s; cut after %ss -> next turn 409, saved result in %ss, done in %ss" % (d["via"], d["protocols"], d["chat"]["events"], d["chat"]["tool_result"], d["cut"]["after_seconds"], d["cut"]["saved_in_seconds"], d["cut"]["done_in_seconds"])')"
else
  cp "$WORK/route.err" "$WORK/body"
  die 9r "deploy/dev/lab/agent_route.py failed: $(tail -3 "$WORK/route.err")"
fi

# ---------- 10. redeploy: pending session + revision moved → warning; resume on the new pod ----------
if [ "$HAS_MEMORY" = 1 ]; then
  code="$(chat "/agents/$AG_ID/chat" "$(msg '{"tool":"Divide","args":{"numerator":9,"denominator":3}}')")"
  [ "$code" = "200" ] && [ "$(sse_ 'names[-1]')" = "interrupt" ] || { cp "$WORK/sse" "$WORK/body"; die 10 "second Divide chat -> $code $(sse_ 'names')"; }
  SID3="$(sse_ 'first("interrupt")["session_id"]')"
  # 그래프를 바꿔(인사말) 리비전을 올린다
  python3 - "$WORK/agent.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
for n in b["graph"]["nodes"]:
    if n["type"] == "entry":
        n["data"]["greeting"] = "안녕하세요. 두 번째 리비전입니다."
json.dump(b, open(sys.argv[1], "w"), ensure_ascii=False)
PY
  code="$(api PUT "/agents/$AG_ID" "$(cat "$WORK/agent.json")")"
  [ "$code" = "200" ] && [ "$(jq_ 'd["revision_created"]')" = "True" ] || die 10 "PUT /agents/$AG_ID -> $code revision_created=$(jq_ 'd["revision_created"]')"
  AG_REV2="$(jq_ 'd["revision"]')"
  [ "$AG_REV2" -gt "$AG_DEPLOYED_REV" ] || die 10 "revision did not move: $AG_REV2"
  # 상태 카드도 같은 경고를 보인다
  code="$(api GET "/agents/$AG_ID")"
  echo "$(jq_ 'd["status"]["warnings"]')" | grep -q pending_sessions_revision_changed || die 10 "status.warnings lacks pending_sessions_revision_changed: $(jq_ 'd["status"]["warnings"]')"
  # 이 재배포가 파드를 둘로 올린다. 10m 이 두 파드 사이의 중지를 본다
  code="$(api POST "/agents/$AG_ID/redeploy" "{\"replicas\": $POD_COUNT}")"
  [ "$code" = "200" ] || die 10 "POST /redeploy -> $code"
  REDEPLOY_WARNINGS="$(jq_ 'd["warnings"]')"
  echo "$REDEPLOY_WARNINGS" | grep -q "pending_sessions_revision_changed: 1 session" || die 10 "redeploy warnings: $REDEPLOY_WARNINGS"
  [ "$(jq_ 'd["deployed_revision"]')" = "$AG_REV2" ] || die 10 "deployed_revision not pinned to $AG_REV2: $(jq_ 'd["deployed_revision"]')"
  echo ">> waiting up to ${READY_TIMEOUT}s for redeployed instance phase=Ready"
  sleep 5
  wait_ready "/agents/$AG_ID" 10
  # 새 파드가 Postgres 의 보류 세션을 그대로 본다 (교차 파드 재개)
  code="$(api GET "/agents/$AG_ID/sessions/$SID3/pending")"
  [ "$code" = "200" ] && [ "$(jq_ 'd["action_requests"][0]["args"]')" = "{'numerator': 9, 'denominator': 3}" ] || die 10 "pending after redeploy -> $code $(cat "$WORK/body")"
  code="$(chat "/agents/$AG_ID/sessions/$SID3/decisions" '{"decisions": [{"type": "reject", "message": "지금은 나누지 마"}]}')"
  [ "$code" = "200" ] && [ "$(sse_ 'names[-1]')" = "done" ] || { cp "$WORK/sse" "$WORK/body"; die 10 "reject on the new pod -> $code $(sse_ 'names')"; }
  [ "$(sse_ 'first("tool_result")["status"]')" = "error" ] && echo "$(sse_ 'first("tool_result")["content"]')" | grep -q "지금은 나누지 마" || die 10 "reject tool_result: $(sse_ 'first("tool_result")')"
  # 리비전 2 의 인사말이 런타임에 실렸다
  code="$(api GET "/agents/$AG_ID/metrics")"
  [ "$(jq_ 'd["sessions"]["pending"]')" = "0" ] || die 10 "pending sessions remain: $(jq_ 'd["sessions"]')"
  pass 10 "pending session $SID3 + revision $AG_DEPLOYED_REV->$AG_REV2: redeploy warnings=$REDEPLOY_WARNINGS; Ready again; pending survived the rollout and reject resumed on the new pod -> done"
else
  code="$(api POST "/agents/$AG_ID/redeploy")"
  [ "$code" = "200" ] || die 10 "POST /redeploy -> $code"
  wait_ready "/agents/$AG_ID" 10
  skip 10 "redeploy ok (200, Ready) but the pending-session warning needs the memory node (Postgres); step 4 skipped"
fi

# ---------- 10m. 파드 둘 (D65): 다른 파드에 닿은 중지가 공유 색인의 표시로 턴을 멈춘다 ----------
# 턴을 도는 파드를 로그의 turn_start 로 찾고 다른 파드에 직접 붙어 런타임의 중지를 부른다. Service 로 부르면 어느 파드에
# 닿을지 몰라 확률로만 보이므로 파드를 골라 확정으로 본다
if [ "$HAS_MEMORY" = 1 ]; then
  # 턴이 새 파드 둘 가운데 하나에서 돌아야 파드를 골라 중지를 부를 수 있다
  rolled_out "$AG_NS" "$AG_K8S" "$AG_ID" || die 10m "deploy/$AG_K8S did not roll out to $POD_COUNT replicas"
  PODS="$(agent_pods "$AG_NS" "$AG_ID" | tr '\n' ' ')"
  [ "$(agent_pods "$AG_NS" "$AG_ID" | grep -c . || true)" = "$POD_COUNT" ] || die 10m "pods of $AG_K8S: $PODS (expected $POD_COUNT)"
  code="$(api GET "/agents/$AG_ID")"
  [ "$(jq_ 'd["status"]["ready_replicas"]')" = "$POD_COUNT" ] || die 10m "status.ready_replicas=$(jq_ 'd["status"]["ready_replicas"]') (expected $POD_COUNT)"
  # 첫 턴이라 세션 id 는 응답 머리로 온다 (플랫폼 프록시가 그대로 옮긴다)
  chat_bg "/agents/$AG_ID/chat" "$(msg "$SLEEP_MSG")"
  SIDP="$(bg_session_id)"
  [ -n "$SIDP" ] || die 10m "no X-Daegam-Session-Id on the first turn through the platform: $(cat "$WORK/bg.hdr" 2>/dev/null)"
  RUN_POD=""
  for ((i = 0; i < 20; i++)); do
    RUN_POD="$(log_events turn_start "$SIDP" 'pod' | head -1 || true)"
    [ -z "$RUN_POD" ] || break
    sleep 0.5
  done
  [ -n "$RUN_POD" ] || die 10m "no pod of $PODS logged turn_start for $SIDP"
  OTHER_POD="$(agent_pods "$AG_NS" "$AG_ID" | grep -vxF "$RUN_POD" | head -1 || true)"
  [ -n "$OTHER_POD" ] || die 10m "no pod besides $RUN_POD: $PODS"
  start_pf "$AG_NS" "pod/$OTHER_POD" "${RT_PORT}:8080"
  PF_RT=$PF_PID
  wait_port "$RT_PORT" || die 10m "port-forward to pod/$OTHER_POD did not open :$RT_PORT"
  # 런타임의 프록시 경로 주체는 인스턴스 토큰과 본문 user_sub 다. 세션의 소유자로 부른다
  code="$(api GET "/agents/$AG_ID/sessions/$SIDP")"
  [ "$code" = "200" ] || die 10m "GET /sessions/$SIDP -> $code"
  CANCEL_BODY="$(jq_ 'json.dumps({"user_sub": d["session"]["user_sub"]})')"
  INSTANCE_TOKEN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" token)"
  CANCEL_MS="$(now_ms)"
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X POST "http://127.0.0.1:${RT_PORT}/sessions/$SIDP/cancel" \
    -H "Authorization: Bearer $INSTANCE_TOKEN" -H 'Content-Type: application/json' -d "$CANCEL_BODY")"
  [ "$code" = "202" ] || die 10m "POST /sessions/$SIDP/cancel on the other pod $OTHER_POD -> $code"
  expect_cancelled_stream 10m "$CANCEL_MS"
  stop_pf "$PF_RT" "$RT_PORT"; PF_RT=""
  pass 10m "$POD_COUNT/$POD_COUNT Ready ($PODS); turn $SIDP ran on $RUN_POD, the cancel on $OTHER_POD -> 202 and the stream ended with error turn_cancelled in ${ENDED_MS}ms"
else
  skip 10m "two pods sharing a session index need the memory node (Postgres); step 4 skipped"
fi

# ---------- 10k. 죽은 파드의 턴 (P79, D98): 리스가 끝나면 다음 읽기가 그 턴을 turn_aborted 로 되살린다 ----------
# 강제로 지운 파드는 SIGTERM 뒤 kubelet 의 2초 최소값에 SIGKILL 을 받아 drain 이 시작돼도 턴을 마감하지 못한다. 그 턴의 리스는
# 갱신이 멈춘 뒤 60초에 끝나고, 그 뒤 첫 읽기(또는 sweep)가 상태를 체크포인트에 맞추고 저장된 카드 뒤에 turn_aborted 줄을 붙이며
# turn_end{repaired: true} 를 낸다. 지울 파드는 그 턴의 tool_exec 를 남긴 파드다. Postgres 는 새 말, Redis 는 승인 대기를
# 거친 재개(색인 pending, 체크포인트에는 인터럽트 없음)를 죽인다
# progress_k -> 10k 세션의 가벼운 읽기 상태 코드. 본문은 $WORK/body 다
progress_k() { api GET "/agents/$AG_ID/sessions/$SIDK?view=progress"; }
if [ "$HAS_MEMORY" = 1 ] && [ "$AUTH_MODE" = header ]; then
  rolled_out "$AG_NS" "$AG_K8S" "$AG_ID" || die 10k "deploy/$AG_K8S did not roll out to $POD_COUNT replicas"
  if [ "$MEMORY_KIND" = redis ]; then
    KILL_TOOL=Divide; KILL_ROW="the owner-approved Divide resume"
    code="$(chat "/agents/$AG_ID/chat" "$(msg "$KILL_DIVIDE_MSG")")"
    { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = interrupt ]; } \
      || { cp "$WORK/sse" "$WORK/body"; die 10k "Divide chat -> $code $(sse_ 'names') (expected interrupt)"; }
    SIDK="$(sse_ 'first("interrupt")["session_id"]')"
    chat_bg "/agents/$AG_ID/sessions/$SIDK/decisions" '{"decisions": [{"type": "approve"}]}'
  else
    KILL_TOOL=Add; KILL_ROW="a new chat"
    chat_bg "/agents/$AG_ID/chat" "$(msg "$KILL_ADD_MSG")"
    SIDK="$(bg_session_id)"
  fi
  [ -n "$SIDK" ] || die 10k "no session id for $KILL_ROW: $(cat "$WORK/bg.hdr" 2>/dev/null)"
  # 결과를 받은 카드가 저장된 뒤에 지워야 되살린 전사의 끝이 그 카드와 줄이다
  CARD_EXPR="any(i['kind'] == 'tool_call' and i['call'].get('name') == '$KILL_TOOL' and i.get('result') for i in (d.get('transcript') or {}).get('items') or [])"
  OPENED_MS="$(now_ms)"
  until [ "$(progress_k)" = 200 ] && [ "$(jq_ "bool(d['session'].get('running')) and $CARD_EXPR")" = True ]; do
    [ "$(now_ms)" -lt "$((OPENED_MS + KILL_CARD_WITHIN * 1000))" ] \
      || die 10k "the $KILL_TOOL card with its result did not show on the running session $SIDK within ${KILL_CARD_WITHIN}s: $(cat "$WORK/body")"
    sleep 0.5
  done
  KILL_POD="$(log_events tool_exec "$SIDK" 'pod' | tail -1 || true)"
  [ -n "$KILL_POD" ] || die 10k "no pod of $AG_K8S logged the $KILL_TOOL tool_exec of $SIDK"
  KILL_MS="$(now_ms)"
  "${K[@]}" -n "$AG_NS" delete pod "$KILL_POD" --grace-period=0 --force --wait=false >/dev/null 2>&1 \
    || die 10k "force-deleting pod/$KILL_POD failed"
  bg_stop
  # 지운 직후와 30초 뒤에도 리스는 살아 있다. drain 이 턴을 마감했다면 여기서 거짓이다
  code="$(progress_k)"
  { [ "$code" = 200 ] && [ "$(jq_ "bool(d['session'].get('running'))")" = True ]; } \
    || die 10k "session $SIDK right after the kill -> $code running=$(jq_ "d['session'].get('running')") (expected running: the drain did not settle the turn)"
  WAIT_S=$(((KILL_MS + KILL_STILL_RUNNING * 1000 - $(now_ms) + 999) / 1000))
  [ "$WAIT_S" -le 0 ] || sleep "$WAIT_S"
  code="$(progress_k)"
  { [ "$code" = 200 ] && [ "$(jq_ "bool(d['session'].get('running'))")" = True ]; } \
    || die 10k "session $SIDK ${KILL_STILL_RUNNING}s after the kill -> $code running=$(jq_ "d['session'].get('running')") (expected running until the lease lapses)"
  # 리스가 끝난 뒤 첫 읽기가 되살린다. 되살린 줄은 그 실행의 trace_id 를 든다. Redis 의 재개 카드는 읽을 때 승인 카드 앞의
  # 첫 카드에 합쳐지므로 카드는 있는지만 보고 끝은 줄이어야 한다
  LAST_EXPR="($CARD_EXPR, *(lambda last: (last.get('kind'), last.get('code'), last.get('trace_id')))((((d.get('transcript') or {}).get('items')) or [{}])[-1]))"
  while :; do
    code="$(progress_k)"
    [ "$code" = 200 ] || die 10k "GET /sessions/$SIDK?view=progress after the kill -> $code"
    if [ "$(jq_ "bool(d['session'].get('running'))")" = False ]; then
      read -r card last_kind last_code KILL_TRACE <<<"$(jq_ "$LAST_EXPR")"
      [ "$last_kind" = error ] && break
    fi
    [ "$(now_ms)" -lt "$((KILL_MS + KILL_FREE_WITHIN * 1000))" ] \
      || die 10k "session $SIDK still running or not repaired ${KILL_FREE_WITHIN}s after the kill: $(cat "$WORK/body")"
    sleep 2
  done
  FREED_MS=$(($(now_ms) - KILL_MS))
  { [ "$card" = True ] && [ "$last_code" = "$ABORTED" ] && [ -n "$KILL_TRACE" ] && [ "$KILL_TRACE" != None ]; } \
    || die 10k "session $SIDK after the repair: $KILL_TOOL card saved=$card, last item error/$last_code trace '$KILL_TRACE' (expected True, error/$ABORTED with the run's trace_id)"
  [ "$(jq_ "d['session']['status']")" = active ] || die 10k "session $SIDK after the repair has status $(jq_ "d['session']['status']") (expected active)"
  # 플랫폼은 되살린 turn_end 로 그 실행을 닫는다. 이벤트는 되살린 파드가 밀어 보내거나 수집기가 60초마다 읽는다
  for ((i = 0; i < 30; i++)); do
    code="$(api GET "/agents/$AG_ID/events?event=turn_end&trace_id=$KILL_TRACE")"
    [ "$code" = 200 ] || die 10k "GET /events?trace_id=$KILL_TRACE -> $code"
    [ "$(jq_ "len(d) == 1 and d[0].get('status') == '$ABORTED' and (d[0].get('data') or {}).get('repaired') is True")" = True ] && break
    sleep 5
  done
  [ "$(jq_ "len(d) == 1 and d[0].get('status') == '$ABORTED' and (d[0].get('data') or {}).get('repaired') is True")" = True ] \
    || die 10k "turn_end of run $KILL_TRACE after 150s: $(cat "$WORK/body") (expected one, status $ABORTED, data.repaired true)"
  code="$(api GET "/agents/$AG_ID/sessions/$SIDK/turns")"
  { [ "$code" = 200 ] && [ "$(jq_ "[r['status'] for t in d['turns'] for r in t['runs'] if r['trace_id'] == '$KILL_TRACE']")" = "['$ABORTED']" ]; } \
    || die 10k "GET /sessions/$SIDK/turns -> $code: run $KILL_TRACE is not listed once as $ABORTED: $(cat "$WORK/body")"
  # 되살린 세션은 다음 말을 받는다. Redis 의 재개는 색인이 pending 이었지만 되살리기가 active 로 맞췄다
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG" "$SIDK")")"
  { [ "$code" = 200 ] && [ "$(sse_ 'names[-1]')" = "done" ]; } \
    || { cp "$WORK/sse" "$WORK/body"; die 10k "the next chat on the repaired session $SIDK -> $code $(sse_ 'names')"; }
  rolled_out "$AG_NS" "$AG_K8S" "$AG_ID" || die 10k "deploy/$AG_K8S did not come back to $POD_COUNT replicas after the kill"
  pass 10k "$KILL_ROW on $KILL_POD force-deleted: running ${KILL_STILL_RUNNING}s after the kill, free and repaired ${FREED_MS}ms after it (the $KILL_TOOL card then error{code: $ABORTED}), run $KILL_TRACE listed as $ABORTED with turn_end repaired, the next chat done; $POD_COUNT/$POD_COUNT Ready again"
elif [ "$HAS_MEMORY" = 1 ]; then
  cond_skip 10k "the dead-pod step runs in the header-mode memory runs (make e2e-agent, make e2e-agent-redis)"
else
  skip 10k "a dead pod's session is repaired from the shared index; it needs the memory node (Postgres); step 4 skipped"
fi

# ---------- 10d. 파드 종료 (D92, P78 5절): 지켜보는 턴은 drain 안에 끝나고 긴 턴은 drain 끝에 turn_aborted 로 마감한다 ----------
# grace 를 Advanced 덮어쓰기로 낮추면 같은 적용에서 drain 이 그 안에 맞춰진다(플랫폼을 다시 띄우지 않는다). 그 뒤 짧은 턴과
# 긴 턴을 뒤에서 열고 두 턴을 도는 파드를 지운다. 두 스트림은 이 게이트가 끝까지 읽는다. drain 은 인증 모드를 읽지 않으므로
# E2E_SHARED_STEPS=skip 이면 header 모드의 두 memory 실행(make e2e-agent, make e2e-agent-redis)만 돈다
if [ "$HAS_MEMORY" = 1 ] && { [ "$AUTH_MODE" = header ] || [ "$SHARED_SKIP" = 0 ]; }; then
  code="$(api PUT "/agents/$AG_ID/overrides" "{\"patch\": {\"spec\": {\"template\": {\"spec\": {\"terminationGracePeriodSeconds\": $DRAIN_GRACE}}}}}")"
  [ "$code" = "200" ] || die 10d "PUT /agents/$AG_ID/overrides (grace $DRAIN_GRACE) -> $code"
  rolled_out "$AG_NS" "$AG_K8S" "$AG_ID" || die 10d "deploy/$AG_K8S did not roll out after the grace override"
  "${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o json >"$WORK/drain-deploy.json"
  DRAIN_ENV="$(deployment_env "$WORK/drain-deploy.json" agent | sed -n 's/^DAEGAM_TURN_DRAIN_SECONDS=//p')"
  POD_GRACE="$(json_of "$WORK/drain-deploy.json" 'd["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")')"
  { [ "$DRAIN_ENV" = "$DRAIN_SECONDS" ] && [ "$POD_GRACE" = "$DRAIN_GRACE" ]; } \
    || die 10d "after the grace override: DAEGAM_TURN_DRAIN_SECONDS='$DRAIN_ENV' grace '$POD_GRACE' (expected $DRAIN_SECONDS and $DRAIN_GRACE)"
  chat_bg_as drain-w "/agents/$AG_ID/chat" "$(msg "$DRAIN_SHORT_MSG")"
  DRAIN_W_PID=$BG_AS_PID
  chat_bg_as drain-l "/agents/$AG_ID/chat" "$(msg "$DRAIN_LONG_MSG")"
  DRAIN_L_PID=$BG_AS_PID
  SIDW="$(bg_session_id drain-w)"; SIDL="$(bg_session_id drain-l)"
  { [ -n "$SIDW" ] && [ -n "$SIDL" ]; } \
    || die 10d "no X-Daegam-Session-Id on the two turns: $(cat "$WORK/drain-w.hdr" "$WORK/drain-l.hdr" 2>/dev/null)"
  # 긴 턴은 Add 결과를 받은 뒤 기다린다. 그 결과가 저장된 뒤에 지워야 drain 끝에 남은 항목을 본다
  W_POD=""; L_POD=""
  for ((i = 0; i < 20; i++)); do
    [ -n "$W_POD" ] || W_POD="$(log_events turn_start "$SIDW" 'pod' | head -1 || true)"
    [ -n "$L_POD" ] || L_POD="$(log_events tool_exec "$SIDL" 'pod' | head -1 || true)"
    { [ -n "$W_POD" ] && [ -n "$L_POD" ]; } && break
    sleep 0.5
  done
  { [ -n "$W_POD" ] && [ -n "$L_POD" ]; } || die 10d "the pods of the two turns were not found in the logs (watched '$W_POD', long '$L_POD')"
  # 두 턴이 한 파드에서 돌면 그 파드 하나만 지운다
  DRAIN_PODS=("$W_POD")
  [ "$L_POD" = "$W_POD" ] || DRAIN_PODS+=("$L_POD")
  DELETE_MS="$(now_ms)"
  "${K[@]}" -n "$AG_NS" delete pod "${DRAIN_PODS[@]}" --wait=false >/dev/null || die 10d "deleting ${DRAIN_PODS[*]} failed"
  ends_by "$DRAIN_W_PID" "$((DELETE_MS + (DRAIN_SECONDS + 15) * 1000))" \
    || die 10d "the watched turn on $W_POD did not end within the drain"
  DRAIN_W_PID=""
  [ "$(sse_ 'names[-1]' "$WORK/drain-w.sse")" = "done" ] \
    || { cp "$WORK/drain-w.sse" "$WORK/body"; die 10d "the watched turn on the deleted pod $W_POD ended with $(sse_ 'names' "$WORK/drain-w.sse") (expected done)"; }
  W_MS=$(($(now_ms) - DELETE_MS))
  # preStop 안에 끝난 턴은 SIGTERM 전에 끝나 drain 이 읽는 사람을 붙잡았는지 보여 주지 못한다
  [ "$W_MS" -gt "$DRAIN_PRESTOP_MS" ] || die 10d "the watched turn ended ${W_MS}ms after the delete, before SIGTERM; it does not show the drain keeping its reader"
  # 긴 턴은 preStop 뒤 SIGTERM 에서 drain 이 끝날 때 멈추고 마지막 이벤트를 받는다. grace(preStop, drain, 마감 몫, 여유)를 넘기지 않는다
  ends_by "$DRAIN_L_PID" "$((DELETE_MS + DRAIN_GRACE * 1000))" \
    || die 10d "the long turn on $L_POD did not end by the end of the grace"
  DRAIN_L_PID=""
  L_MS=$(($(now_ms) - DELETE_MS))
  expect_stream_error 10d "$WORK/drain-l.sse" "$ABORTED"
  [ "$L_MS" -ge "$((DRAIN_SECONDS * 1000))" ] || die 10d "the long turn ended ${L_MS}ms after the delete, before the ${DRAIN_SECONDS}s drain"
  # 다른 파드에서도 그 세션을 읽는다. 저장된 Add 결과 뒤에 turn_aborted 로 끝나고 더는 돌지 않는다
  code="$(api GET "/agents/$AG_ID/sessions/$SIDL")"
  [ "$code" = "200" ] || die 10d "GET /sessions/$SIDL after the delete -> $code"
  read -r running saved last_kind last_code <<<"$(jq_ "bool(d['session'].get('running')), any(i['kind'] == 'tool_call' and '$DRAIN_LONG_RESULT' in str((i.get('result') or {}).get('content')) for i in (d.get('transcript') or {}).get('items') or []), (((d.get('transcript') or {}).get('items') or [{}])[-1]).get('kind'), (((d.get('transcript') or {}).get('items') or [{}])[-1]).get('code')")"
  { [ "$running" = False ] && [ "$saved" = True ] && [ "$last_kind" = error ] && [ "$last_code" = "$ABORTED" ]; } \
    || die 10d "session $SIDL after the drain: running=$running saved Add=$saved last item=$last_kind/$last_code (expected False, True, error/$ABORTED)"
  rolled_out "$AG_NS" "$AG_K8S" "$AG_ID" || die 10d "deploy/$AG_K8S did not come back to $POD_COUNT replicas after the delete"
  pass 10d "grace override $DRAIN_GRACE -> DAEGAM_TURN_DRAIN_SECONDS=$DRAIN_SECONDS; deleting ${DRAIN_PODS[*]}: the watched turn $SIDW ended done ${W_MS}ms after the delete, the long turn $SIDL ended error{code: $ABORTED} ${L_MS}ms after it with its Add result saved"
elif [ "$HAS_MEMORY" = 1 ]; then
  cond_skip 10d "E2E_SHARED_STEPS=skip: the drain reads no auth mode; the header-mode memory runs (make e2e-agent, make e2e-agent-redis) run it"
else
  skip 10d "the drain across two pods needs the memory node (Postgres); step 4 skipped"
fi

# ---------- 11. real provider (conditional) ----------
if [ -n "$LLM_PROVIDER" ]; then
  python3 - "$WORK/agent.json" "$LLM_PROVIDER" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
for n in b["graph"]["nodes"]:
    if n["type"] == "model":  # D28
        n["data"]["provider"] = sys.argv[2]
json.dump(b, open(sys.argv[1], "w"), ensure_ascii=False)
PY
  code="$(api PUT "/agents/$AG_ID" "$(cat "$WORK/agent.json")")"
  [ "$code" = "200" ] && [ "$(jq_ 'd["revision_created"]')" = "True" ] || die 11 "PUT provider=$LLM_PROVIDER -> $code: $(cat "$WORK/body")"
  code="$(api POST "/agents/$AG_ID/redeploy")"
  [ "$code" = "200" ] || die 11 "POST /redeploy (provider $LLM_PROVIDER) -> $code"
  LLM_SECRETS="$(jq_ '" ".join(d["llm_secrets"])')"
  [ -n "$LLM_SECRETS" ] || die 11 "llm_secrets empty after switching to $LLM_PROVIDER"
  for s in $LLM_SECRETS; do
    "${K[@]}" -n "$AG_NS" get secret "$s" -o name >/dev/null || die 11 "provider Secret copy $AG_NS/$s missing"
  done
  sleep 5
  wait_ready "/agents/$AG_ID" 11
  code="$(chat "/agents/$AG_ID/chat" "$(msg '3 더하기 4는 얼마인가? 반드시 Add 도구를 써서 계산하고 결과 숫자를 답에 적어라.')")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die 11 "POST /chat ($LLM_PROVIDER) -> $code"; }
  NAMES="$(sse_ 'names')"
  [ "$(sse_ 'names[-1]')" = "done" ] || die 11 "stream did not end with done: $NAMES $(cat "$WORK/sse")"
  [ "$(sse_ 'first("tool_call")["name"]')" = "Add" ] || die 11 "real model did not call Add: $NAMES"
  DONE_TEXT="$(sse_ 'first("done")["text"]')"; USAGE="$(sse_ 'first("done")["usage"]')"
  echo "$DONE_TEXT" | grep -q 7 || die 11 "answer lacks 7: $DONE_TEXT"
  pass 11 "provider $LLM_PROVIDER: Secret copies [$LLM_SECRETS]; chat $NAMES; Add called; answer='$DONE_TEXT' usage=$USAGE"
else
  cond_skip 11 "no E2E_LLM_PROVIDER (set it to a registered provider with a key, e.g. local-anthropic after make llm-provider-local)"
fi

# ---------- 11r. Tool 규칙 (P52 2·3·4·5절, local) ----------
# 규칙은 MCP Server 리비전의 일부다. 조건이 있는 Tool 은 모든 파드가 조건을 지키는 이미지로 도는 것을 플랫폼이 확인할
# 때까지 닫혀 있고(conditions_closed), 이름 댄 Agent 는 자기 id 를 실은 토큰을 다음 재적용이 낼 때까지 그 Tool 을 보지
# 못한다(agent_tool_hidden 의 cause reissue). 둘이 가라앉은 뒤에 부른다. 실험 토큰은 플랫폼의 서비스 발급자로 서명한 서버
# audience 의 토큰이라 프록시가 받는다. 위임 토큰의 모양은 sub 가 사람, act 가 Agent 다(P53 1절)
if [ "$AUTH_MODE" = local ]; then
  RULE_ADD="{\"callers\": {\"agents\": [\"$AG_ID\"]}, \"conditions\": [{\"path\": \"a\", \"op\": \"max\", \"value\": 100}]}"
  RULES="{\"Add\": $RULE_ADD, \"Divide\": {\"callers\": {\"roles\": [\"developer\"]}}}"
  RULES_SET=1
  rules_save 11r "$MS_ID" "$RULES"
  [ "$(json_of "$WORK/rules-saved.json" "'$AG_ID' in [a['id'] for a in d['rules_notice']['rolling_agents']]")" = True ] \
    || die 11r "the save does not list $AG_NAME among the Agents that may restart: $(json_of "$WORK/rules-saved.json" 'd["rules_notice"]')"
  rules_redeploy 11r "$MS_ID"
  rules_settled 11r "$MS_ID"
  MS_K8S="$(jq_ 'd["k8s"]["name"]')"; MS_NS="$(jq_ 'd["k8s"]["namespace"]')"
  hidden=""
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    [ "$(api GET "/agents/$AG_ID")" = 200 ] || die 11r "GET /agents/$AG_ID failed"
    hidden="$(jq_ 'json.dumps([w for w in d["status"]["warnings"] if w.startswith("agent_tool_hidden")])')"
    case "$hidden" in *"cause: reissue"*) sleep 5 ;; *) break ;; esac
  done
  case "$hidden" in
    *"cause: reissue"*) die 11r "$AG_NAME still waits for its token reissue after ${READY_TIMEOUT}s: $hidden" ;;
    *"cause: not_named; tools: Divide"*) ;;
    *) die 11r "$AG_NAME status does not say that the role-only rule hides Divide: $hidden" ;;
  esac
  # 재발급한 토큰으로 파드가 굴렀다면 다 바뀐 뒤에 부른다. 옛 파드는 agent_id 없는 토큰을 들고 있을 수 있다
  "${K[@]}" -n "$AG_NS" rollout status "deployment/$AG_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null \
    || die 11r "deployment/$AG_K8S did not finish rolling within ${READY_TIMEOUT}s"
  # 채팅: 이름 댄 Agent 는 사람이 누구든 Add 를 부른다. 조건을 어긴 호출은 백엔드에 가지 않는 Tool 오류다
  code="$(chat "/agents/$AG_ID/chat" "$(msg "$ADD_MSG")")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 11r "POST /agents/$AG_ID/chat (Add) -> $code"; }
  { [ "$(sse_ 'first("tool_result")["status"]')" = success ] && sse_ 'first("tool_result")["content"]' | grep -q 7; } \
    || die 11r "Add under a rule naming $AG_NAME: $(sse_ 'names') tool_result=$(sse_ 'first("tool_result")')"
  code="$(chat "/agents/$AG_ID/chat" "$(msg '{"tool":"Add","args":{"a":500,"b":1}}')")"
  [ "$code" = 200 ] || { cp "$WORK/sse" "$WORK/body"; die 11r "POST /agents/$AG_ID/chat (Add 500) -> $code"; }
  REFUSAL="$(sse_ 'first("tool_result")["content"]')"
  { [ "$(sse_ 'first("tool_result")["status"]')" = error ] && echo "$REFUSAL" | grep -q "refused this call: argument 'a'"; } \
    || die 11r "Add(500, 1) was not refused by the condition on a: $(sse_ 'names') tool_result=$(sse_ 'first("tool_result")')"
  # 서버 프록시에 바로: Agent 자신의 신원, 위임, 이름 대지 않은 Agent
  start_pf "$MS_NS" "svc/mcp-$MS_K8S-proxy" "${RULES_PORT}:8080"
  PF_RULES=$PF_PID
  wait_port "$RULES_PORT" || die 11r "port-forward to svc/mcp-$MS_K8S-proxy did not open :$RULES_PORT"
  RULES_URL="http://127.0.0.1:${RULES_PORT}/mcp"
  # Agent 자신의 신원은 플랫폼이 낸 그 서버의 소스 토큰이다(STEP 6 의 SRC_ENV). 규칙이 이름 대면 자기 id 를 실어야 한다(3절)
  OWN="$(secret_field "$AG_NS" "$AG_TOKEN_SECRET" "$SRC_ENV")" || die 11r "reading $SRC_ENV from Secret $AG_TOKEN_SECRET failed"
  [ "$(jwt_json "$OWN" 1 | json_of - 'd.get("agent_id")')" = "$AG_ID" ] \
    || die 11r "$AG_NAME's $SRC_ENV token carries agent_id $(jwt_json "$OWN" 1 | json_of - 'd.get("agent_id")'), want $AG_ID"
  # 위임 토큰의 사람은 이 테넌트의 멤버가 아니다. 이름 댄 Agent 는 사람이 누구든 부른다
  DELEGATED="$(svc_token "$MS_ID" --human-sub "$RULES_OUTSIDER" --act "svc:agent:$AG_K8S" --agent-id "$AG_ID" --no-groups \
    --bound-to "mcp-server:$MS_ID")" || die 11r "svc_token.py (delegated) failed"
  STRANGER="$(svc_token "$MS_ID" --sub "$RULES_STRANGER" --agent-id e2e-unnamed)" || die 11r "svc_token.py (unnamed Agent) failed"
  STRANGER_ACT="$(svc_token "$MS_ID" --human-sub "$ADMIN_SUB" --act "$RULES_STRANGER" --agent-id e2e-unnamed --no-groups \
    --bound-to "mcp-server:$MS_ID")" || die 11r "svc_token.py (unnamed Agent, delegated) failed"
  for token in "$OWN" "$DELEGATED"; do
    who="own identity"; [ "$token" = "$OWN" ] || who="delegated"
    mcp_admits 11r "$token" "$AG_NAME ($who)" "$RULES_URL"
    [ "$(tool_names "$token" "$RULES_URL")" = '["Add"]' ] || die 11r "$AG_NAME ($who) lists $(mcp_json 'd["tools"]'), want Add alone"
  done
  mcp_refuses 11r "$STRANGER" "an Agent the rule does not name" "$RULES_URL"
  mcp_refuses 11r "$STRANGER_ACT" "a delegation token of an Agent the rule does not name" "$RULES_URL"
  # 이름 바꾸기: 굴리는 동안 새 Cedar 가 옛 컨테이너를 만나도 두 이름 모두 닫혀 있다(2절)
  rules_save 11r "$MS_ID" "$RULES" "{\"Add\": {\"name\": \"$RULES_RENAMED\"}}"
  rules_redeploy 11r "$MS_ID"
  probes=0
  for ((i = 0; i < READY_TIMEOUT / 3; i++)); do
    for name in Add "$RULES_RENAMED"; do
      call_refused "$STRANGER" "$RULES_URL" "$name" || die 11r "the unnamed Agent called $name while the rename rolls: $(cat "$WORK/mcp.json")"
    done
    # 프록시가 답하지 않아 거절된 것은 세지 않는다. 이름 댄 Agent 의 목록이 그 순간 읽혀야 거절이 규칙의 것이다
    if tool_names "$OWN" "$RULES_URL" >/dev/null; then probes=$((probes + 1)); fi
    rules_rolled "$MS_K8S" "$MS_NS" && [ "$probes" -ge 3 ] && break
    sleep 3
  done
  rules_rolled "$MS_K8S" "$MS_NS" || die 11r "the backend of $MS_K8S did not roll within ${READY_TIMEOUT}s"
  rules_settled 11r "$MS_ID"
  for name in Add "$RULES_RENAMED"; do
    call_refused "$STRANGER" "$RULES_URL" "$name" || die 11r "the unnamed Agent called $name after the rename: $(cat "$WORK/mcp.json")"
  done
  NAMES="$(tool_names "$OWN" "$RULES_URL")" || die 11r "tools/list with $AG_NAME's own token failed: $(cat "$WORK/mcp.err")"
  [ "$NAMES" = "[\"$RULES_RENAMED\"]" ] || die 11r "after the rename $AG_NAME lists $NAMES, want $RULES_RENAMED alone"
  rc="$(mcp --token "$OWN" call "$RULES_URL" "$RULES_RENAMED" '{"a": 3, "b": 4}')"
  { [ "$rc" = 0 ] && mcp_json 'json.dumps(d["call"]["content"])' | grep -q 7; } \
    || die 11r "$AG_NAME call $RULES_RENAMED rc=$rc: $(cat "$WORK/mcp.json" "$WORK/mcp.err")"
  stop_pf "$PF_RULES" "$RULES_PORT"; PF_RULES=""
  # 규칙과 오버라이드를 걷는다. STEP 12·13 의 복제본은 다른 Agent 라 규칙이 남으면 Add 를 보지 못한다
  rules_save 11r "$MS_ID" '{}' '{}'
  rules_redeploy 11r "$MS_ID"
  rules_settled 11r "$MS_ID"
  for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
    rules_rolled "$MS_K8S" "$MS_NS" && break
    sleep 5
  done
  RULES_SET=0
  pass 11r "rules on $MS_NAME (Add: $AG_NAME only, a <= 100; Divide: developer and up) listed $AG_NAME as may restart; conditions recorded; $AG_NAME status hides Divide (not_named); chat Add(3,4) -> 7 and Add(500,1) -> '$REFUSAL'; at the proxy $AG_NAME's own and delegated tokens list Add alone and get 7, an unnamed Agent's service and delegation tokens get an empty list and a refused call; renaming Add to $RULES_RENAMED: $probes probes during the roll and one after refused both names to the unnamed Agent, $AG_NAME then lists $RULES_RENAMED alone -> 7; rules removed and redeployed"
else
  cond_skip 11r "Tool rules are checked in local mode (make e2e-agent-local); header mode renders no caller rule, which e2e-gateway checks with its warning"
fi

# ---------- 12. clone → delete → objects gone; 404 ----------
# STEP 13 이 쓸 정의를 삭제 전에 복제해 둔다. 한 문서는 배포를 하나만 가지므로 노출용 둘째는 복제본이고,
# 삭제는 정의까지 지우므로 복제가 먼저다 (D42).
# 노출은 복제 본문이 아니라 STEP 13·13a 의 배포 본문에 싣는다. e2e 넷이 같은 모양이고, 노출을 켠 배포를
# 서버가 받는지 거부하는지가 배포 호출에 그대로 드러나야 STEP 13a 의 422 unauthenticated_exposure 단정이
# 이름 그대로를 본다. 복제 본문도 exposure 를 받아 draft 에 남기므로 어느 쪽이든 되지만 이 게이트는 배포
# 본문으로 맞춘다. 공통 까닭은 e2e_mcp_auth.sh 의 STEP 7 주석에 있다
# 이전 실행이 남긴 복제본(409)은 지우고 다시 복제한다 (맨 위 "재실행의 409")
clone_fresh 12 /agents "$AG_ID" "$AIX_NAME" "$AIX_DISPLAY"
AIX_ID="$(jq_ 'd["id"]')"
[ "$(jq_ 'd["state"]')" = "draft" ] || die 12 "clone state=$(jq_ 'd["state"]') (expected draft)"
# 삭제는 토큰을 거두기 전에 이 Agent 의 도는 턴을 멈춘다 (P78 5절). 런타임이 인스턴스의 리스를 모두 표시하므로 플랫폼이 모르는
# 이전 세션의 턴도 멈춘다. memory 가 있으면 STEP 7 의 세션에서 턴을 돌린다. 없으면 세션이 파드 안에만 있어 STEP 10 의 재배포가
# 그 세션을 지웠으므로 새 세션이다
if [ "$HAS_MEMORY" = 1 ]; then DEL_SID="$SID1"; else DEL_SID=""; fi
chat_bg "/agents/$AG_ID/chat" "$(msg "$SLEEP_MSG" "$DEL_SID")"
SIDX="$(bg_session_id)"
[ -n "$SIDX" ] || die 12 "no X-Daegam-Session-Id on the turn before the delete: $(cat "$WORK/bg.hdr" 2>/dev/null)"
sleep "$CANCEL_AFTER"
DELETE_MS="$(now_ms)"
code="$(api DELETE "/agents/$AG_ID")"
[ "$code" = "200" ] || die 12 "DELETE /agents/$AG_ID -> $code"
CANCEL_END_WITHIN="$DELETE_END_WITHIN" expect_cancelled_stream 12 "$DELETE_MS"
DELETE_NOTE="the running turn of session $SIDX$([ -z "$DEL_SID" ] || echo " (STEP 7's)") ended error{code: turn_cancelled} ${ENDED_MS}ms after the delete call"
# 남이 클레임을 쥐고 있었으면 deleted 가 거짓이고 홀더가 마친다. 그때 회수 수는 0 이다 (P66 2.6절)
if [ "$(jq_ 'd["deleted"]')" = "False" ]; then
  wait_gone "/agents/$AG_ID" || die 12 "deferred delete did not finish"
else
  [ "$(jq_ 'd["revoked_tokens"] >= 1')" = "True" ] || die 12 "delete response: $(cat "$WORK/body")"
fi
gone=""
for ((i = 0; i < 24; i++)); do
  left=""
  "${K[@]}" -n "$AG_NS" get deployment "$AG_K8S" -o name >/dev/null 2>&1 && left="$left deployment"
  "${K[@]}" -n "$AG_NS" get service "$AG_K8S" -o name >/dev/null 2>&1 && left="$left service"
  "${K[@]}" -n "$AG_NS" get networkpolicy "daegam-$AG_K8S" -o name >/dev/null 2>&1 && left="$left networkpolicy"
  "${K[@]}" -n "$AG_NS" get secret "$AG_TOKEN_SECRET" -o name >/dev/null 2>&1 && left="$left token-secret"
  for s in $LLM_SECRETS; do "${K[@]}" -n "$AG_NS" get secret "$s" -o name >/dev/null 2>&1 && left="$left $s"; done
  [ -z "$left" ] && { gone=1; break; }
  sleep 5
done
[ -n "$gone" ] || die 12 "still present after delete:$left"
# 삭제는 정의까지 지운다. 소프트 삭제가 아니므로 410 이 아니라 404 다 (D42)
code="$(api GET "/agents/$AG_ID")"
[ "$code" = "404" ] || die 12 "GET deleted agent -> $code (expected 404)"
pass 12 "복제본 $AIX_NAME (draft, id=$AIX_ID) 를 뜬 뒤 삭제 (404 afterwards); $DELETE_NOTE; Deployment·Service·NetworkPolicy·token Secret·provider copies gone from $AG_NS"

# ---------- 12m. 삭제가 장기 기억을 걷는다 (D34). 체크포인터를 남기는 12r 과 여기서 갈린다 ----------
if [ "$HAS_MEMORY" = 1 ]; then
  if [ "$MEMORY_KIND" = postgres ]; then
    LEFT_ROWS="$(psql_ "select count(*) from daegam_store.store where prefix like '$MEM_PREFIX%'")"
    [ "${LEFT_ROWS:-x}" = "0" ] \
      || die 12m "daegam_store.store still has ${LEFT_ROWS:-?} row(s) with prefix like '$MEM_PREFIX%' after the instance delete"
    pass 12m "장기 기억이 삭제와 함께 걷혔다 (daegam_store.store 의 prefix '${MEM_PREFIX}…' 행 $MEM_ROWS -> 0). 스키마와 테이블은 남는다"
  else
    LEFT_STORE="$(rcli --scan --pattern "$MEM_KEYS" | grep -c . || true)"
    [ "${LEFT_STORE:-x}" = "0" ] \
      || die 12m "redis still has ${LEFT_STORE:-?} key(s) matching $MEM_KEYS after the instance delete"
    pass 12m "장기 기억이 삭제와 함께 걷혔다 (redis $MEM_KEYS $MEM_ROWS -> 0). 체크포인터 키는 12r 이 남아 있음을 본다"
  fi
else
  skip 12m "long-term memory needs the memory node; step 4 skipped"
fi

# ---------- 12r. (redis) 삭제 뒤 키 잔존 확인과 정리 (D33: 인스턴스 삭제는 Postgres 의 행처럼 키를 지우지 않는다) ----------
# 정리는 이 인스턴스의 접두어와 이 실행의 세션 id 로만 한다. write_keys_zset:* 전체나 ToolHive 의 daegam: 은 건드리지 않는다.
# EXIT 트랩이 아니라 여기서 하고, 정리 실패는 die 가 아니라 경고다
if [ "$MEMORY_KIND" = redis ]; then
  LEFT_KEYS="$(rcli --scan --pattern "daegam-agent:$AG_ID:*" || true)"
  N_LEFT="$(printf '%s\n' "$LEFT_KEYS" | grep -c . || true)"
  KEYS_OK=0
  # 부분 문자열 검사는 case 로 한다. `printf | grep -q` 는 grep 이 첫 일치에서 파이프를 닫아
  # 목록이 길면 printf 가 EPIPE 로 죽고 pipefail 이 그것을 실패로 본다 (실측)
  HAS_CKPT=0; HAS_SESS=0
  case "$LEFT_KEYS" in *:checkpoint:*) HAS_CKPT=1 ;; esac
  case "$LEFT_KEYS" in *:session:*) HAS_SESS=1 ;; esac
  if [ "$N_LEFT" -ge 1 ] && [ "$HAS_CKPT" = 1 ] && [ "$HAS_SESS" = 1 ]; then
    KEYS_OK=1
  else
    record_fail 12r "keys under daegam-agent:$AG_ID:* should survive the instance delete (left $N_LEFT): $(printf '%s\n' "$LEFT_KEYS" | head -20 | tr '\n' ' ')"
  fi
  N_DEL_INST="$(rdel "daegam-agent:$AG_ID:*")"
  ZSET_DEL=""
  # STEP 7·8·8e·9r·10·10m·10k·10d·12 의 세션. 모두 이 인스턴스의 것이라 함께 지운다
  for sid in "${SID1:-}" "${SID2:-}" "$SIDE" "$ROUTE_SID" "${SID3:-}" "$SIDP" "$SIDK" "$SIDW" "$SIDL" "$SIDX"; do
    [ -n "$sid" ] || continue
    ZSET_DEL="$ZSET_DEL $sid=$(rdel "write_keys_zset:$sid:*")"
  done
  IDX_DEL=""
  for name in $LAB_REDIS_INDEXES; do
    idx="daegam-agent:$AG_ID:$name"
    out="$(rcli FT.DROPINDEX "$idx" 2>/dev/null || true)"
    case "$out" in OK*) IDX_DEL="$IDX_DEL ok" ;; *) IDX_DEL="$IDX_DEL '${out:-no reply}'"; echo "!! FT.DROPINDEX $idx -> ${out:-(no reply)}" ;; esac
  done
  N_AFTER="$(rcli --scan --pattern "daegam-agent:$AG_ID:*" 2>/dev/null | grep -c . || true)"
  N_IDX_AFTER="$(rcli FT._LIST 2>/dev/null | grep -c -F "daegam-agent:$AG_ID:" || true)"
  if [ "$N_AFTER" != 0 ] || [ "$N_IDX_AFTER" != 0 ]; then
    echo "!! redis cleanup left $N_AFTER keys and $N_IDX_AFTER indexes under daegam-agent:$AG_ID: (lab Redis is ephemeral; see kubectl --context $CTX -n $SPIKE_NS exec deploy/redis -- redis-cli --scan --pattern 'daegam-agent:$AG_ID:*')"
  fi
  if [ "$KEYS_OK" = 1 ]; then
    pass 12r "keys survived the delete ($N_LEFT under daegam-agent:$AG_ID:*); cleaned: DEL $N_DEL_INST instance keys, write_keys_zset per session:$ZSET_DEL, FT.DROPINDEX$IDX_DEL; left $N_AFTER keys / $N_IDX_AFTER indexes"
  fi
fi

# ---------- 14. 키 두 벌 (P35 12·13절). 마지막에 돈다. 13a 와 13 의 끝이 저마다 부른다 ----------
# 종류 openai 인 provider 둘을 가짜 키로 넣고(D56) 메인과 서브가 하나씩 쓰는 Agent 를 도구·memory 없이 띄운다. 모델을
# 부르지 않으므로 Ready 가 된다. 만든 것은 끝에서 drop_key_agent 가 걷는다
KEY_NS=""; KEY_K8S=""; KEY_TOKEN_SECRET=""; KEY_ENV_LINES=""; KEY_JSON_ENV=""; KEY_SHA=""; KEY_ROLLOUT=""
# read_key_state: 키 Agent 의 롤아웃이 끝나기를 기다려 파드 템플릿의 설정 지문을 KEY_SHA 에, 세대와 ReplicaSet 이름을
# KEY_ROLLOUT 에, agent 컨테이너 env 의 provider 키 줄(`이름=secret:<Secret>`)을 KEY_ENV_LINES 에 남긴다. 인스턴스 토큰으로
# 받은 agent.json(init 과 같은 경로)의 메인·서브 env_key 는 KEY_JSON_ENV 다. 값을 echo 로 돌려주지 않는 것은 $( ) 안의
# die 가 부른 쪽을 멈추지 못해서다
read_key_state() {
  local token code rs
  "${K[@]}" -n "$KEY_NS" rollout status "deploy/$KEY_K8S" --timeout="${READY_TIMEOUT}s" >/dev/null || die 14 "deploy/$KEY_K8S did not roll out"
  "${K[@]}" -n "$KEY_NS" get deployment "$KEY_K8S" -o json >"$WORK/key-deploy.json"
  KEY_SHA="$(template_annotation "$WORK/key-deploy.json" "$CONFIG_SHA_ANNOTATION")"
  rs="$("${K[@]}" -n "$KEY_NS" get rs -l "$AGENT_POD_SELECTOR$KEY_AG_ID" -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | sort | tr '\n' ' ')" \
    || die 14 "listing the ReplicaSets of $KEY_K8S failed"
  KEY_ROLLOUT="generation $(json_of "$WORK/key-deploy.json" 'd["metadata"]["generation"]'), replicasets [ $rs]"
  KEY_ENV_LINES="$(deployment_env "$WORK/key-deploy.json" agent | { grep -E '^(DAEGAM_LLM_|OPENAI_)' || true; })"
  token="$(secret_field "$KEY_NS" "$KEY_TOKEN_SECRET" token)"
  code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "$API/agents/$KEY_AG_ID/agent.json" -H "Authorization: Bearer $token")"
  [ "$code" = "200" ] || die 14 "GET /agents/$KEY_AG_ID/agent.json with the instance token -> $code"
  KEY_JSON_ENV="$(jq_ 'd["main"]["model"]["provider"]["env_key"], d["subagents"][0]["model"]["provider"]["env_key"]')"
}
# keys_agent_body MAIN_PROVIDER SUB_PROVIDER: 키 Agent 의 저장 본문을 $WORK/keys.json 에 만든다
keys_agent_body() {
  python3 "$HERE/lib/agent_body.py" "$WORK/templates.json" "$WORK/keys.json" --name "$KEY_AG_NAME" --display "$KEY_AG_DISPLAY" \
    --provider "$1" --sub-name Sub --sub-description "메인이 맡기는 일을 하는 서브에이전트다." --sub-provider "$2"
}
keys_step() {
  local code patch copies copy_a copy_b first_env first_sha before pod
  if [ "$BASE_RUN" = 0 ]; then
    cond_skip 14 "E2E_SHARED_STEPS=skip: the Agent has no tools and no memory, so the key copies read neither the auth mode nor the memory kind; make e2e-agent runs it"
    return 0
  fi
  code="$(api GET /settings)"
  [ "$code" = "200" ] || die 14 "GET /settings -> $code"
  # 본문을 따로 만든다. $( ) 안의 $( ) 에 \" 가 든 인자를 넣으면 macOS 의 bash 3.2 가 JSON 을 낱말로 쪼갠다
  patch="$(providers_with "$WORK/body" \
    "{\"name\": \"$KEY_PROVIDER_A\", \"kind\": \"openai\", \"model\": \"$KEY_MODEL\"}" \
    "{\"name\": \"$KEY_PROVIDER_B\", \"kind\": \"openai\", \"model\": \"$KEY_MODEL\"}")"
  code="$(api PATCH /settings "$patch")"
  [ "$code" = "200" ] || die 14 "PATCH /settings (providers $KEY_PROVIDER_A, $KEY_PROVIDER_B) -> $code"
  KEY_PROVIDERS_ADDED=1
  put_provider_key 14 "$KEY_PROVIDER_A" "$KEY_A"
  put_provider_key 14 "$KEY_PROVIDER_B" "$KEY_B"
  keys_agent_body "$KEY_PROVIDER_A" "$KEY_PROVIDER_B"
  save_doc 14 /agents "$KEY_AG_NAME" "$(cat "$WORK/keys.json")"
  KEY_AG_ID="$SAVED_ID"
  deploy_ready 14 /agents "$KEY_AG_ID" "{\"cluster_id\": \"$HOME_ID\"}"
  KEY_NS="$(jq_ 'd["k8s"]["namespace"]')"; KEY_K8S="$(jq_ 'd["k8s"]["name"]')"; KEY_TOKEN_SECRET="$(jq_ 'd["k8s"]["token_secret"]')"
  copies=" $(jq_ '" ".join(d["llm_secrets"])') "
  read_key_state
  # (1) provider 마다 이름에서 나온 env 가 제 복사본을 가리키고 kind 의 옛 env(OPENAI_API_KEY·OPENAI_BASE_URL)는 없다
  copy_a="$(printf '%s\n' "$KEY_ENV_LINES" | sed -n "s/^$KEY_ENV_A=secret://p")"
  copy_b="$(printf '%s\n' "$KEY_ENV_LINES" | sed -n "s/^$KEY_ENV_B=secret://p")"
  { [ -n "$copy_a" ] && [ -n "$copy_b" ] && [ "$copy_a" != "$copy_b" ] && [ "$(printf '%s\n' "$KEY_ENV_LINES" | grep -c .)" = "2" ]; } \
    || die 14 "provider key env of $KEY_K8S: $(printf '%s ' "$KEY_ENV_LINES") (expected $KEY_ENV_A and $KEY_ENV_B on two copies, no OPENAI_*)"
  case "$copies" in *" $copy_a "*) ;; *) die 14 "$copy_a is not in llm_secrets:$copies" ;; esac
  case "$copies" in *" $copy_b "*) ;; *) die 14 "$copy_b is not in llm_secrets:$copies" ;; esac
  [ "$KEY_JSON_ENV" = "$KEY_ENV_A $KEY_ENV_B" ] || die 14 "agent.json main·sub env_key: $KEY_JSON_ENV (expected $KEY_ENV_A $KEY_ENV_B)"
  case "$KEY_SHA" in '' | 0) die 14 "pod template lacks $CONFIG_SHA_ANNOTATION: '$KEY_SHA'" ;; esac
  first_env="$KEY_ENV_LINES"
  # (2) 쓰임을 맞바꾼 리비전. env 이름은 provider 를 따라가므로 그대로이고 agent.json 의 자리만 바뀐다
  keys_agent_body "$KEY_PROVIDER_B" "$KEY_PROVIDER_A"
  code="$(api PUT "/agents/$KEY_AG_ID" "$(cat "$WORK/keys.json")")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["revision_created"]')" = "True" ]; } || die 14 "PUT /agents/$KEY_AG_ID (swapped providers) -> $code"
  code="$(api POST "/agents/$KEY_AG_ID/redeploy")"
  [ "$code" = "200" ] || die 14 "POST /agents/$KEY_AG_ID/redeploy (swapped providers) -> $code"
  read_key_state
  [ "$KEY_ENV_LINES" = "$first_env" ] \
    || die 14 "provider key env moved with the swap: $(printf '%s ' "$KEY_ENV_LINES") (was $(printf '%s ' "$first_env"))"
  [ "$KEY_JSON_ENV" = "$KEY_ENV_B $KEY_ENV_A" ] || die 14 "agent.json main·sub env_key after the swap: $KEY_JSON_ENV (expected $KEY_ENV_B $KEY_ENV_A)"
  # (3) 바꾼 것 없는 재배포. 설정 지문과 세대가 그대로라 새 ReplicaSet 이 없다
  first_sha="$KEY_SHA"; before="$KEY_ROLLOUT"
  code="$(api POST "/agents/$KEY_AG_ID/redeploy")"
  [ "$code" = "200" ] || die 14 "POST /agents/$KEY_AG_ID/redeploy (unchanged) -> $code"
  sleep "$SETTLE_SECONDS"
  read_key_state
  { [ "$KEY_SHA" = "$first_sha" ] && [ "$KEY_ROLLOUT" = "$before" ]; } \
    || die 14 "an unchanged redeploy rolled the pods: $CONFIG_SHA_ANNOTATION $first_sha -> $KEY_SHA, $before -> $KEY_ROLLOUT"
  # (4) 둘째 provider 의 키를 바꾼 재배포. 지문이 바뀌어 파드가 굴러가고 새 파드가 새 키를 읽는다
  put_provider_key 14 "$KEY_PROVIDER_B" "$KEY_B_NEW"
  code="$(api POST "/agents/$KEY_AG_ID/redeploy")"
  [ "$code" = "200" ] || die 14 "POST /agents/$KEY_AG_ID/redeploy (rotated key) -> $code"
  read_key_state
  [ "$KEY_SHA" != "$first_sha" ] || die 14 "$CONFIG_SHA_ANNOTATION did not change after the key of $KEY_PROVIDER_B changed: $KEY_SHA"
  pod="$(agent_pods "$KEY_NS" "$KEY_AG_ID" | head -1 || true)"
  [ -n "$pod" ] || die 14 "no running pod of $KEY_K8S after the rollout"
  [ "$("${K[@]}" -n "$KEY_NS" exec "$pod" -c agent -- python -c 'import os, sys; print(os.environ.get(sys.argv[1], ""))' "$KEY_ENV_B")" = "$KEY_B_NEW" ] \
    || die 14 "the new pod $pod does not read the rotated key of $KEY_PROVIDER_B from $KEY_ENV_B"
  drop_key_agent
  pass 14 "$KEY_ENV_A -> $copy_a, $KEY_ENV_B -> $copy_b, no OPENAI_*; agent.json env_key main·sub follow the providers; swapped revision keeps the env; unchanged redeploy keeps $CONFIG_SHA_ANNOTATION $first_sha ($before); rotating $KEY_PROVIDER_B rolled to $KEY_SHA and pod $pod reads the new key; agent and providers removed"
}

# 복제본 삭제(게이트)와 다음 실행에 대비한 정리 (MCP Server·Database 는 실패해도 게이트와 무관)
# ---------- 13a (header). 노출을 켠 deploy 는 422 unauthenticated_exposure (D27: 런타임에 무인증 외부 경로가 없다) ----------
if [ "$AUTH_MODE" = "header" ]; then
  code="$(api POST "/agents/$AIX_ID/deploy" "$(deploy_body '[]')")"
  [ "$code" = "422" ] || die 13a "exposed deploy in header mode -> $code (expected 422)"
  [ "$(jq_ "d['detail']['code']")" = "unauthenticated_exposure" ] || die 13a "code = $(jq_ "d['detail']['code']") (expected unauthenticated_exposure)"
  DETAIL="$(jq_ "d['detail']['detail']")"
  echo "$DETAIL" | grep -qi "oidc" || die 13a "detail does not name oidc mode: $DETAIL"
  # 노출 검증은 문서를 건드리기 전에 난다. 복제본은 draft 그대로다
  code="$(api GET "/agents/$AIX_ID")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["state"]')" = "draft" ]; } \
    || die 13a "clone $AIX_NAME after the 422: $code state=$(jq_ 'd["state"]') (expected 200 draft)"
  pass 13a "exposed deploy rejected in header mode: 422 unauthenticated_exposure ('$DETAIL'); 복제본 $AIX_NAME 은 draft 그대로"
  final_cleanup 13a
  cond_skip 13 "external exposure needs oidc mode (make e2e-agent-oidc patches the ConfigMap and runs it)"
  keys_step
  finish
fi
cond_skip 13a "the header-mode 422 needs header mode (make e2e-agent)"

# ---------- 13 (oidc·local). 외부 노출: Ingress 로 데이터 플레인 토큰만 가지고 채팅 (D27) ----------
LB_IP="$("${K[@]}" -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
[ -n "$LB_IP" ] || die 13 "Service $TRAEFIK_NS/$TRAEFIK_SVC has no LoadBalancer IP (is Traefik installed?)"
# deploy (노출 켬). STEP 12 가 만든 복제본을 그대로 띄운다. 갓 만든 draft 라 409 는 나지 않는다
body="$(deploy_body '[]')"
code="$(api POST "/agents/$AIX_ID/deploy" "$body")"
[ "$code" = "201" ] || die 13 "POST /agents/$AIX_ID/deploy (exposed) -> $code: $(cat "$WORK/body")"
AIX_K8S="$(jq_ 'd["k8s"]["name"]')"; AIX_NS="$(jq_ 'd["k8s"]["namespace"]')"
[ "$AIX_K8S" = "agent-$AIX_NAME" ] || die 13 "k8s name $AIX_K8S (expected agent-$AIX_NAME)"
[ "$(jq_ 'd["exposure"]["enabled"], d["exposure"]["host"], d["exposure"]["tls"]["mode"], d["exposure"]["cors_origins"], d["exposure"]["allowed_groups"]')" = "True $EXT_HOST none ['$CORS_ORIGIN'] []" ] \
  || die 13 "exposure snapshot: $(jq_ 'd["exposure"]')"
if jq_ 'd["warnings"]' | grep -q 'unauthenticated_exposure:'; then die 13 "oidc mode must not warn about unauthenticated exposure: $(jq_ 'd["warnings"]')"; fi
echo ">> waiting up to ${READY_TIMEOUT}s for the exposed agent $AIX_ID phase=Ready"
wait_ready "/agents/$AIX_ID" 13
[ "$(jq_ 'd["status"]["external_url"]')" = "$EXT_URL" ] || die 13 "status.external_url = $(jq_ 'd["status"]["external_url"]') (expected $EXT_URL)"
[ "$(jq_ 'd["status"]["chat_url"]')" = "/api/agents/$AIX_ID/chat" ] || die 13 "status.chat_url = $(jq_ 'd["status"]["chat_url"]')"
# Ingress(/chat·/sessions·/memories → agent-<name>:8080, owner Deployment)·노출 정책·검증기 env
ING="$("${K[@]}" -n "$AIX_NS" get ingress "$AIX_K8S" -o jsonpath='{.spec.rules[0].host} {.spec.rules[0].http.paths[*].path} {.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.number} owner={.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}' 2>/dev/null || true)"
[ -n "$ING" ] || die 13 "Ingress $AIX_NS/$AIX_K8S missing"
[ "$ING" = "$EXT_HOST /chat /sessions /memories $AIX_K8S:8080 owner=Deployment/$AIX_K8S" ] || die 13 "Ingress spec: $ING"
"${K[@]}" -n "$AIX_NS" get networkpolicy "daegam-expose-$AIX_K8S" -o name >/dev/null || die 13 "NetworkPolicy $AIX_NS/daegam-expose-$AIX_K8S missing"
ENVS="$(agent_env)"
echo "  env: $ENVS"
echo "$ENVS" | grep -q 'DAEGAM_OIDC_ISSUER=http' || die 13 "DAEGAM_OIDC_ISSUER missing from pod env: $ENVS"
echo "$ENVS" | grep -q 'DAEGAM_OIDC_AUDIENCE=daegam-mcp ' || die 13 "DAEGAM_OIDC_AUDIENCE is not the data-plane audience daegam-mcp: $ENVS"
# local 은 플랫폼 JWKS 를 클러스터 안 주소에서 받는다(P32 1절). oidc 는 IdP 디스커버리를 쓴다
if [ "$AUTH_MODE" = "local" ]; then
  echo "$ENVS" | grep -q 'DAEGAM_OIDC_JWKS_URL=http://[^ ]*/.well-known/jwks.json ' || die 13 "DAEGAM_OIDC_JWKS_URL should be the in-cluster platform JWKS: $ENVS"
elif echo "$ENVS" | grep -q 'DAEGAM_OIDC_JWKS_URL='; then die 13 "oidc mode must not pin DAEGAM_OIDC_JWKS_URL: $ENVS"; fi
echo "$ENVS" | grep -q "DAEGAM_CORS_ORIGINS=$CORS_ORIGIN " || die 13 "DAEGAM_CORS_ORIGINS: $ENVS"
# 빈 allowed_groups 는 local 에서 이 테넌트의 그룹으로 채워 내린다(P32). oidc 는 비워 둔다(D27)
WANT_GROUPS=""; [ "$AUTH_MODE" != "local" ] || WANT_GROUPS="daegam-$TENANT"
echo "$ENVS" | grep -q "DAEGAM_ALLOWED_GROUPS=$WANT_GROUPS " || die 13 "DAEGAM_ALLOWED_GROUPS should be '$WANT_GROUPS': $ENVS"
for ((i = 0; i < READY_TIMEOUT / 5; i++)); do
  api GET "/agents/$AIX_ID" >/dev/null
  [ "$(jq_ '(d["status"].get("exposure") or {}).get("address_assigned")')" = "True" ] && break; sleep 5
done
EXPOSURE_ST="$(jq_ 'json.dumps(d["status"]["exposure"])')"
echo "  status.exposure: $EXPOSURE_ST"
[ "$(jq_ '(d["status"].get("exposure") or {}).get("address_assigned")')" = "True" ] || die 13 "Ingress address not assigned: $EXPOSURE_ST"
pass 13 "exposed $AIX_NAME ($AIX_K8S) $EXT_URL via Traefik $LB_IP: Ingress [/chat /sessions /memories] owner Deployment/$AIX_K8S, daegam-expose-$AIX_K8S, env DAEGAM_OIDC_AUDIENCE=daegam-mcp DAEGAM_ALLOWED_GROUPS='$WANT_GROUPS', address assigned"
# (a) 무토큰 401 unauthorized + WWW-Authenticate. Origin 을 보내면 401 에도 Allow-Origin 이 붙는다. Traefik 이 라우트를
#     집어 들 때까지 404 → 401
code=""
for ((i = 0; i < 12; i++)); do
  code="$(ext POST /chat "" "$(msg 'hi')" -H "Origin: $CORS_ORIGIN")"; [ "$code" = "401" ] && break; sleep 5
done
[ "$code" = "401" ] || die "13(a)" "no-token POST $EXT_URL/chat -> $code (expected 401): $(cat "$WORK/body")"
[ "$(jq_ "d['code']")" = "unauthorized" ] || die "13(a)" "no-token code: $(cat "$WORK/body")"
WWW="$(hdr www-authenticate)"; [ -n "$WWW" ] || die "13(a)" "401 without WWW-Authenticate"
ACAO="$(hdr access-control-allow-origin)"; [ "$ACAO" = "$CORS_ORIGIN" ] || die "13(a)" "401 lacks Access-Control-Allow-Origin=$CORS_ORIGIN (got '$ACAO')"
code="$(ext OPTIONS /chat "" "" -H "Origin: $CORS_ORIGIN" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: authorization, content-type")"
{ [ "$code" = "200" ] || [ "$code" = "204" ]; } || die "13(a)" "CORS preflight -> $code"
[ "$(hdr access-control-allow-origin)" = "$CORS_ORIGIN" ] || die "13(a)" "preflight Allow-Origin: '$(hdr access-control-allow-origin)'"
hdr access-control-allow-methods | grep -q POST || die "13(a)" "preflight Allow-Methods: '$(hdr access-control-allow-methods)'"
hdr access-control-allow-headers | grep -qi authorization || die "13(a)" "preflight Allow-Headers: '$(hdr access-control-allow-headers)'"
PREFLIGHT="$code"
pass "13(a)" "no token -> 401 unauthorized ($WWW), Origin 을 보낸 401 에 Allow-Origin $ACAO, CORS preflight $PREFLIGHT"
# (b) 제어면 aud(daegam-api) 토큰은 데이터 플레인 audience 에 맞지 않아 401 invalid_token
code="$(ext POST /chat "$ADMIN_TOKEN" "$(msg 'hi')")"
{ [ "$code" = "401" ] && [ "$(jq_ "d['code']")" = "invalid_token" ]; } || die "13(b)" "control-plane aud token -> $code $(cat "$WORK/body") (expected 401 invalid_token)"
pass "13(b)" "control-plane aud (daegam-api) token -> 401 invalid_token"
# 사람 A·B 의 데이터 플레인 토큰. 노출 배포가 Ready 가 된 뒤에 받는다. local 은 둘 다 $TENANT 의 viewer 다
TOKEN_A="$(dp_token alice-e2e "$TENANT")" || die 13 "user A token failed"
TOKEN_B="$(dp_token bob-e2e "$TENANT")" || die 13 "user B token failed"
if [ "$AUTH_MODE" = "local" ]; then
  SUB_A="$(local_user_id alice-e2e)"; SUB_B="$(local_user_id bob-e2e)"
  [ -n "$SUB_A" ] && [ -n "$SUB_B" ] || die 13 "user ids for alice-e2e/bob-e2e not found"
else
  SUB_A="alice-e2e"; SUB_B="bob-e2e"
fi
# (c) 사용자 A: Add(3,4) → 7. 본문 user_sub 는 무시되고 소유자는 토큰 sub 다
code="$(chat_ext "$TOKEN_A" /chat "$(msg_sub "$ADD_MSG" spoofed-sub)")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(c)" "user A POST $EXT_URL/chat -> $code"; }
NAMES="$(sse_ 'names')"
[ "$(sse_ 'names[-1]')" = "done" ] || die "13(c)" "stream did not end with done: $NAMES $(cat "$WORK/sse")"
[ "$(sse_ 'first("tool_call")["name"]')" = "Add" ] || die "13(c)" "tool_call is not Add: $NAMES"
RESULT="$(sse_ 'first("tool_result")["content"]')"
{ [ "$(sse_ 'first("tool_result")["status"]')" = "success" ] && echo "$RESULT" | grep -q 7; } || die "13(c)" "tool_result via ingress: $RESULT"
SIDX1="$(sse_ 'first("done")["session_id"]')"; USAGE="$(sse_ 'first("done")["usage"]')"
T_ADD="$(timing_line)"
echo "  Add turn timing: $T_ADD"
# 증분 SSE: done 은 첫 이벤트보다 늦게(툴 왕복만큼) 도착하고 본문이 한 청크로 몰려 오지 않는다
[ "$(times_ 'd["chunks"] >= 2 and d["events"][-1][1] > d["events"][0][1]')" = "True" ] || die "13(c)" "SSE looks buffered (one chunk or all events at once): $T_ADD"
code="$(ext GET "/sessions/$SIDX1" "$TOKEN_A" "")"
{ [ "$code" = "200" ] && [ "$(jq_ 'd["session"]["user_sub"]')" = "$SUB_A" ]; } || die "13(c)" "GET /sessions/$SIDX1 as A -> $code $(cat "$WORK/body") (owner should be the token sub $SUB_A, not the body user_sub)"
code="$(ext GET "/sessions?limit=100" "$TOKEN_A" "")"
[ "$code" = "200" ] || die "13(c)" "GET /sessions as A -> $code"
[ "$(jq_ "sorted({s['user_sub'] for s in d['sessions']}), '$SIDX1' in [s['session_id'] for s in d['sessions']]")" = "['$SUB_A'] True" ] || die "13(c)" "A's session list: $(cat "$WORK/body")"
# HITL 한 턴: Divide → interrupt 가 스트림이 닫히기 전에 도착한다 (Traefik 증분 SSE 실측). 이어서 (d) 소유권
HITL_NOTE="HITL skipped (no memory node)"
if [ "$HAS_MEMORY" = 1 ]; then
  code="$(chat_ext "$TOKEN_A" /chat "$(msg "$DIVIDE_MSG")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(c)" "user A Divide -> $code"; }
  NAMES2="$(sse_ 'names')"
  [ "$(sse_ 'names[-1]')" = "interrupt" ] || die "13(c)" "Divide stream did not end with interrupt: $NAMES2 $(cat "$WORK/sse")"
  SIDX2="$(sse_ 'first("interrupt")["session_id"]')"
  [ "$(sse_ 'first("interrupt")["action_requests"][0]["name"]')" = "Divide" ] || die "13(c)" "interrupt action: $(sse_ 'first("interrupt")')"
  T_DIVIDE="$(timing_line)"
  echo "  Divide turn timing: $T_DIVIDE"
  [ "$(times_ 'dict(d["events"])["interrupt"] <= d["eof_ms"] and d["curl_starttransfer_ms"] <= d["curl_total_ms"]')" = "True" ] || die "13(c)" "interrupt did not arrive before EOF: $T_DIVIDE"
  pass "13(c)" "A Add(3,4) -> '$RESULT' owner $SUB_A (body user_sub ignored), $NAMES, timing: $T_ADD; Divide -> interrupt before EOF ($T_DIVIDE)"
  # (d) 사용자 B 는 A 의 세션·pending·decisions 를 못 보고 자기 목록은 비어 있다
  code="$(ext GET "/sessions/$SIDX2" "$TOKEN_B" "")"
  { [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "not_session_owner" ]; } || die "13(d)" "B GET /sessions/$SIDX2 -> $code $(cat "$WORK/body") (expected 403 not_session_owner)"
  code="$(ext GET "/sessions/$SIDX2/pending" "$TOKEN_B" "")"
  { [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "not_session_owner" ]; } || die "13(d)" "B GET /pending -> $code $(cat "$WORK/body")"
  code="$(ext POST "/sessions/$SIDX2/decisions" "$TOKEN_B" '{"decisions": [{"type": "approve"}]}')"
  { [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "not_session_owner" ]; } || die "13(d)" "B POST /decisions -> $code $(cat "$WORK/body")"
  code="$(ext GET "/sessions?limit=100" "$TOKEN_B" "")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["sessions"]')" = "[]" ]; } || die "13(d)" "B's session list should be empty: $code $(cat "$WORK/body")"
  # A(소유자)는 pending 을 보고 approve 로 재개한다 (SSE 재개도 Ingress 를 지난다)
  code="$(ext GET "/sessions/$SIDX2/pending" "$TOKEN_A" "")"
  { [ "$code" = "200" ] && [ "$(jq_ 'd["action_requests"][0]["name"]')" = "Divide" ]; } || die "13(d)" "A GET /pending -> $code $(cat "$WORK/body")"
  code="$(chat_ext "$TOKEN_A" "/sessions/$SIDX2/decisions" '{"decisions": [{"type": "approve"}]}')"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(d)" "A approve -> $code"; }
  [ "$(sse_ 'names[-1]')" = "done" ] || die "13(d)" "resume did not end with done: $(sse_ 'names')"
  RESULT2="$(sse_ 'first("tool_result")["content"]')"
  { [ "$(sse_ 'first("tool_result")["name"], first("tool_result")["status"]')" = "Divide success" ] && echo "$RESULT2" | grep -q 4; } || die "13(d)" "tool_result after approve: $(sse_ 'first("tool_result")')"
  HITL_NOTE="Divide $NAMES2 (interrupt before EOF: $T_DIVIDE); B -> 403 not_session_owner on session·pending·decisions, B's list empty; A pending 200, approve -> '$RESULT2'"
  pass "13(d)" "B -> 403 not_session_owner on A's session·pending·decisions, B's list empty; A pending 200 -> approve -> '$RESULT2'"
  # (d2) 장기 기억은 사람마다 따로다 (D34 완료 조건 2). A 가 쓴 것을 B 는 못 읽는다. 소유자는 토큰 sub 다
  XMEM_PATH="/memories/e2e-ext-note.md"; XMEM_TEXT="알리스전용-$(date +%s)"
  code="$(chat_ext "$TOKEN_A" /chat "$(mem_msg write_file file_path "$XMEM_PATH" content "$XMEM_TEXT")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(d2)" "A write_file -> $code"; }
  { [ "$(sse_ 'names[-1]')" = "done" ] && [ "$(sse_ 'first("tool_result")["status"]')" = "success" ]; } \
    || die "13(d2)" "A write_file turn: $(sse_ 'names') $(sse_ 'first("tool_result")')"
  code="$(chat_ext "$TOKEN_A" /chat "$(mem_msg read_file file_path "$XMEM_PATH")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(d2)" "A read_file -> $code"; }
  A_READ="$(sse_ 'first("done")["text"]')"
  echo "$A_READ" | grep -q -F "$XMEM_TEXT" || die "13(d2)" "A could not read back her own memory: $A_READ"
  code="$(chat_ext "$TOKEN_B" /chat "$(mem_msg read_file file_path "$XMEM_PATH")")"
  [ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(d2)" "B read_file -> $code"; }
  B_READ="$(sse_ 'first("done")["text"]')"
  ! echo "$B_READ" | grep -q -F "$XMEM_TEXT" \
    || die "13(d2)" "B read A's memory; 네임스페이스가 토큰 sub 로 갈려야 한다 (done.text=$B_READ)"
  # 기억 목록의 외부 경로도 토큰 sub 의 것만 보인다. Ingress 가 /memories 를 런타임으로 보낸다 (P36 8절)
  HAS_XMEM="'$XMEM_PATH' in [f['path'] for f in d['files']]"
  code="$(ext GET /memories "$TOKEN_A" "")"
  { [ "$code" = "200" ] && [ "$(jq_ "$HAS_XMEM")" = True ]; } \
    || die "13(d2)" "A GET /memories via Traefik -> $code $(cat "$WORK/body") (expected $XMEM_PATH in files)"
  code="$(ext GET /memories "$TOKEN_B" "")"
  { [ "$code" = "200" ] && [ "$(jq_ "$HAS_XMEM")" = False ]; } \
    || die "13(d2)" "B GET /memories via Traefik -> $code $(cat "$WORK/body") (A's $XMEM_PATH must not be listed)"
  pass "13(d2)" "장기 기억은 사람마다 따로다: alice-e2e 가 쓴 '$XMEM_TEXT' 를 자기는 읽고 bob-e2e 는 못 읽으며, Traefik 을 지난 GET /memories 도 A 의 목록에만 있다"
else
  pass "13(c)" "A Add(3,4) -> '$RESULT' owner $SUB_A (body user_sub ignored), $NAMES, timing: $T_ADD (Divide/HITL needs the memory node)"
  skip "13(d)" "session ownership needs the HITL turn (memory node); step 4 skipped"
fi
# (c2) local 만. 다른 테넌트(e2e-ops)에만 속한 사람의 토큰은 allowed_groups 가 빈 이 테넌트의 노출 에이전트에서 403 forbidden
#      이다. 플랫폼이 빈 목록을 daegam-$TENANT 로 채워 내리기 때문이다 (P32 테넌트 경계). oidc 는 D27 대로 인증된 주체 모두다
if [ "$AUTH_MODE" = "local" ]; then
  TOKEN_OPS="$(dp_token ops-e2e "$OPS_TENANT")" || die "13(c2)" "ops token failed"
  code="$(ext POST /chat "$TOKEN_OPS" "$(msg 'hi')")"
  { [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "forbidden" ]; } \
    || die "13(c2)" "a token of tenant $OPS_TENANT on the agent of $TENANT -> $code $(cat "$WORK/body") (expected 403 forbidden)"
  pass "13(c2)" "token of tenant $OPS_TENANT (groups [$OPS_GROUP]) -> 403 forbidden on the $TENANT agent whose allowed_groups is empty"
else
  cond_skip "13(c2)" "the tenant boundary of an empty allowed_groups is local mode only (oidc admits every authenticated subject, D27)"
fi
# (s) local 만 (P53b 6.7절). 서비스 어카운트: 관리자가 $TENANT 에 viewer 계정을 만들고 자기 비밀번호로 그 토큰을 낸다. 토큰은
#     sub sa:<id>, groups [daegam-$TENANT] 이고, allowed_groups 가 빈 이 노출 Agent 의 GET /sessions 가 200 이다. 사람의
#     개인 토큰을 빌리던 프로그램이 쓰는 자리다. 계정은 이 단계가 지운다. oidc 의 서비스 어카운트는 e2e-oidc-platform 이 본다
if [ "$AUTH_MODE" = "local" ]; then
  SA_NAME="agent-e2e-ci"
  sa_create "13(s)" "$TENANT" "$SA_NAME" viewer
  sa_issue "13(s)" "$TENANT" "$SA_ID" "agent-e2e ci" password "$LOCAL_PW2"
  sa_claims_ok "13(s)" "$SA_TOKEN" "$SA_ID" "daegam-$TENANT"
  code=""
  for ((i = 0; i < 6; i++)); do  # 런타임은 모르는 kid 를 보면 JWKS 를 다시 받는다
    code="$(ext GET "/sessions?limit=1" "$SA_TOKEN" "")"; [ "$code" = "200" ] && break; sleep 5
  done
  [ "$code" = "200" ] || die "13(s)" "the service-account token on the exposed agent -> $code $(cat "$WORK/body")"
  api_delete "/tenants/$TENANT/service-accounts/$SA_ID"
  SA_ID=""
  pass "13(s)" "service account $SA_NAME (viewer) issued with the admin's password: sub sa:<id>, groups [daegam-$TENANT]; GET /sessions on the exposed agent -> 200; account deleted"
else
  cond_skip "13(s)" "service accounts on the exposed Agent are checked in local mode here (make e2e-oidc-platform checks oidc)"
fi
# (t) 턴 위임 토큰 (P53 3.3절). local 의 외부 경로는 act 나 bound_to 를 실었지만 이 Agent 에 묶이지 않은 토큰을 테넌트 그룹이
#     맞아도 403 으로 거절한다. 실험 토큰은 이 파드가 믿는 발급자로 서명한다. oidc 는 그대로라 IdP 가 낸 act 토큰도 받는다.
#     (f) 가 allowed_groups 를 바꾸므로 그 앞에서 돈다
if [ "$AUTH_MODE" = "local" ]; then
  LAB_ISSUER="$(tr ' ' '\n' <<<"$ENVS" | sed -n 's/^DAEGAM_OIDC_ISSUER=//p')"
  [ -n "$LAB_ISSUER" ] || die "13(t)" "no DAEGAM_OIDC_ISSUER in the exposed pod env: $ENVS"
  LAB_PLAIN="$(human_token e2e-lab-plain)" || die "13(t)" "svc_token.py (plain) failed"
  LAB_GW="$(human_token e2e-lab-gw --act "$LAB_ACTOR" --bound-to "gateway:${TURN_GW_ID:-e2e-gateway}")" \
    || die "13(t)" "svc_token.py (bound to a Gateway) failed"
  LAB_UNBOUND="$(human_token e2e-lab-unbound --act "$LAB_ACTOR")" || die "13(t)" "svc_token.py (act without bound_to) failed"
  code="$(ext GET "/sessions?limit=1" "$LAB_PLAIN" "")"
  [ "$code" = 200 ] || die "13(t)" "a lab token with the tenant group and no act -> $code $(cat "$WORK/body") (expected 200)"
  code="$(ext GET "/sessions?limit=1" "$LAB_GW" "")"
  { [ "$code" = 403 ] && [ "$(jq_ "d['code']")" = forbidden ]; } \
    || die "13(t)" "a token bound to gateway:${TURN_GW_ID:-e2e-gateway} -> $code $(cat "$WORK/body") (expected 403 forbidden)"
  code="$(ext GET "/sessions?limit=1" "$LAB_UNBOUND" "")"
  { [ "$code" = 403 ] && [ "$(jq_ "d['code']")" = forbidden ]; } \
    || die "13(t)" "a token with act and no bound_to -> $code $(cat "$WORK/body") (expected 403 forbidden)"
  pass "13(t)" "lab tokens with the tenant group: plain -> 200, + act and bound_to gateway:${TURN_GW_ID:-e2e-gateway} -> 403 forbidden, + act alone -> 403 forbidden"
else
  TOKEN_ACT="$(user_token '{"sub": "carol-e2e", "extra": {"act": {"sub": "e2e-idp-actor"}}}')" || die "13(t)" "IdP token with act failed"
  code="$(ext GET "/sessions?limit=1" "$TOKEN_ACT" "")"
  [ "$code" = 200 ] || die "13(t)" "an IdP token with act -> $code $(cat "$WORK/body") (oidc mode must admit it as before, P53 3.3)"
  pass "13(t)" "an IdP token with act (sub carol-e2e) -> 200 on the exposed agent in oidc mode"
fi
# (e) /metrics: Ingress 는 /chat·/sessions·/memories 만 라우팅하므로 Traefik 은 404, Service 로 직접 가면 403 forbidden.
#     /healthz 는 토큰 없이 {status} 만
code="$(ext GET /metrics "$TOKEN_A" "")"
[ "$code" = "404" ] || die "13(e)" "GET /metrics via Traefik -> $code (expected 404: /metrics is not routed)"
start_pf "$AIX_NS" "svc/$AIX_K8S" "${RT_PORT}:8080"
PF_RT=$PF_PID
wait_port "$RT_PORT" || die "13(e)" "port-forward to svc/$AIX_K8S did not open :$RT_PORT"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "http://127.0.0.1:${RT_PORT}/metrics" -H "Authorization: Bearer $TOKEN_A")"
{ [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "forbidden" ]; } || die "13(e)" "GET /metrics on the Service with a user token -> $code $(cat "$WORK/body") (expected 403 forbidden)"
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' "http://127.0.0.1:${RT_PORT}/healthz")"
{ [ "$code" = "200" ] && [ "$(jq_ 'sorted(d.keys())')" = "['status']" ]; } || die "13(e)" "GET /healthz without a token leaks details: $code $(cat "$WORK/body")"
stop_pf "$PF_RT" "$RT_PORT"; PF_RT=""
pass "13(e)" "/metrics via Traefik 404 (not routed) + on the Service 403 forbidden, /healthz without a token is {status} only"
# (h) 인사말·중지·끊김이 Traefik 을 지난다 (D65, D92). A 의 새 세션은 greeting 으로 열리고 CORS 가 세션 id 머리를 드러낸다.
#     A 는 도는 턴을 중지로 멈추고 B 는 못 멈춘다. Traefik 을 지나 끊은 턴도 끝까지 가고 A 는 가벼운 읽기로 그 결과를 본다.
#     (f) 가 A 를 막으므로 그 앞에서 돈다
code="$(chat_ext "$TOKEN_A" /chat "$(msg "$ADD_MSG")" -H "Origin: $CORS_ORIGIN")"
[ "$code" = "200" ] || { cp "$WORK/sse" "$WORK/body"; die "13(h)" "user A new session with Origin -> $code"; }
SIDH="$(sse_ 'first("done")["session_id"]')"
{ [ "$(sse_ 'names[0]')" = "greeting" ] && [ "$(sse_ 'names[-1]')" = "done" ]; } || die "13(h)" "A's new session: $(sse_ 'names') (expected greeting first and done last)"
case "$(hdr access-control-expose-headers | tr '[:upper:]' '[:lower:]')" in
  *x-daegam-session-id*) ;;
  *) die "13(h)" "Access-Control-Expose-Headers '$(hdr access-control-expose-headers)' lacks X-Daegam-Session-Id" ;;
esac
[ "$(hdr x-daegam-session-id)" = "$SIDH" ] || die "13(h)" "X-Daegam-Session-Id '$(hdr x-daegam-session-id)' (expected $SIDH)"
AIX_GREETING="$(sse_ 'first("greeting")["text"]')"
code="$(api GET "/agents/$AIX_ID")"
[ "$(jq_ 'd["status"]["greeting"]')" = "$AIX_GREETING" ] || die "13(h)" "greeting '$AIX_GREETING' is not status.greeting '$(jq_ 'd["status"]["greeting"]')'"
ext_bg "$TOKEN_A" "$(msg "$SLEEP_MSG" "$SIDH")"
sleep "$CANCEL_AFTER"
code="$(ext POST "/sessions/$SIDH/cancel" "$TOKEN_B" '{}')"
{ [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "not_session_owner" ]; } \
  || die "13(h)" "B cancels A's turn -> $code $(cat "$WORK/body") (expected 403 not_session_owner)"
CANCEL_MS="$(now_ms)"
code="$(ext POST "/sessions/$SIDH/cancel" "$TOKEN_A" '{}')"
{ [ "$code" = "202" ] && [ "$(jq_ 'd["session_id"]')" = "$SIDH" ]; } || die "13(h)" "A cancels her own turn -> $code $(cat "$WORK/body")"
expect_cancelled_stream "13(h)" "$CANCEL_MS"
# Traefik 을 지나 끊은 턴도 끝까지 간다. 노출 경로의 세션 읽기는 소유자의 것이다
read_sidh() { ext GET "/sessions/$SIDH?view=progress" "$TOKEN_A" ""; }
ext_bg "$TOKEN_A" "$(msg "$DETACHED_MSG" "$SIDH")"
cut_detached "13(h)" read_sidh chat_ext "$TOKEN_A" /chat "$(msg "$ADD_MSG" "$SIDH")"
pass "13(h)" "A's new session $SIDH opened with greeting (= status.greeting) and Access-Control-Expose-Headers carries X-Daegam-Session-Id; B's cancel 403 not_session_owner, A's cancel 202 -> error turn_cancelled in ${ENDED_MS}ms; a turn cut through Traefik ran on (next turn 409, saved Add result ${SAVED_AFTER_MS}ms and done ${DONE_AFTER_MS}ms after the cut)"
# (f) allowed_groups [$OPS_GROUP] 로 redeploy → 그 그룹이 없는 A 의 토큰 403 forbidden, ops 토큰 200. ops 토큰은 재배포가
#     Ready 가 된 뒤에 받는다
code="$(api POST "/agents/$AIX_ID/redeploy" "{\"exposure\": $(exposure_json "[\"$OPS_GROUP\"]")}")"
[ "$code" = "200" ] || die "13(f)" "POST /redeploy with allowed_groups [$OPS_GROUP] -> $code: $(cat "$WORK/body")"
[ "$(jq_ 'd["exposure"]["allowed_groups"], d["exposure"]["host"]')" = "['$OPS_GROUP'] $EXT_HOST" ] || die "13(f)" "exposure after redeploy: $(jq_ 'd["exposure"]')"
sleep 5
wait_ready "/agents/$AIX_ID" 13
# 옛 파드는 옛 allowed_groups 로 답하므로 A 의 403 이 새 파드에서 왔어도 ops 요청이 옛 파드로 갈 수 있다
rolled_out "$AIX_NS" "$AIX_K8S" "$AIX_ID" || die "13(f)" "deploy/$AIX_K8S did not finish rolling out"
ENVS="$(agent_env)"
echo "$ENVS" | grep -q "DAEGAM_ALLOWED_GROUPS=$OPS_GROUP " || die "13(f)" "DAEGAM_ALLOWED_GROUPS after redeploy: $ENVS"
TOKEN_OPS="$(dp_token ops-e2e "$OPS_TENANT" "$OPS_GROUP")" || die "13(f)" "ops token failed"
code=""
for ((i = 0; i < 12; i++)); do  # 옛 파드가 물러날 때까지 200 이 섞일 수 있다
  code="$(ext POST /chat "$TOKEN_A" "$(msg 'hi')")"; [ "$code" = "403" ] && break; sleep 5
done
{ [ "$code" = "403" ] && [ "$(jq_ "d['code']")" = "forbidden" ]; } || die "13(f)" "A's token after allowed_groups [$OPS_GROUP] -> $code $(cat "$WORK/body") (expected 403 forbidden)"
code="$(chat_ext "$TOKEN_OPS" /chat "$(msg "$ADD_MSG")")"
[ "$code" = "200" ] || {
  cp "$WORK/sse" "$WORK/body"
  for pod in $(agent_pods "$AIX_NS" "$AIX_ID"); do
    echo "  $pod: $("${K[@]}" -n "$AIX_NS" logs "$pod" -c agent 2>/dev/null | grep -v healthz | tail -3 | tr '\n' '|')"
  done
  die "13(f)" "ops token POST /chat -> $code (token claims $(jwt_json "$TOKEN_OPS" 1 | json_of - '{k: d.get(k) for k in ("iss", "aud", "sub", "tenant", "groups")}'), allowed_groups [$OPS_GROUP])"
}
{ [ "$(sse_ 'names[-1]')" = "done" ] && sse_ 'first("tool_result")["content"]' | grep -q 7; } || die "13(f)" "ops token turn: $(sse_ 'names') $(cat "$WORK/sse")"
T_OPS="$(timing_line)"
pass "13(f)" "allowed_groups [$OPS_GROUP]: A's token 403 forbidden, ops token -> 7 ($T_OPS)"
# (g) 삭제 → Ingress·노출 정책·Deployment 소멸, Traefik 404
code="$(api DELETE "/agents/$AIX_ID")"
[ "$code" = "200" ] || die "13(g)" "DELETE /agents/$AIX_ID -> $code"
gone=""
for ((i = 0; i < 24; i++)); do
  left="$("${K[@]}" -n "$AIX_NS" get "ingress/$AIX_K8S" "networkpolicy/daegam-expose-$AIX_K8S" "networkpolicy/daegam-$AIX_K8S" "deployment/$AIX_K8S" "service/$AIX_K8S" -o name --ignore-not-found 2>/dev/null | tr '\n' ' ')"
  [ -z "$left" ] && { gone=1; break; }; sleep 5
done
[ -n "$gone" ] || die "13(g)" "still present after delete: $left"
code=""
for ((i = 0; i < 12; i++)); do
  code="$(ext POST /chat "$TOKEN_OPS" "$(msg 'hi')")"; [ "$code" = "404" ] && break; sleep 5
done
[ "$code" = "404" ] || die "13(g)" "Traefik still routes $EXT_URL/chat after delete -> $code"
pass "13(g)" "delete -> Ingress·daegam-expose-${AIX_K8S}·Deployment gone, Traefik answers 404 ($HITL_NOTE)"
final_cleanup 13
keys_step

finish
