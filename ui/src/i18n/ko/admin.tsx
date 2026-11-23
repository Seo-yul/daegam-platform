import type { ReactNode } from "react";

import type { LLMProviderCheck, ProviderKind, WebSearchCheck, WebSearchProvider } from "@/api/types";
import type { AuthMode } from "@/lib/auth";
import { particle } from "@/lib/particle";

import { account } from "./account";

/** 플랫폼 관리 화면 (Group Mappings, Platform Settings, Users). */
export const admin = {
  /** 플랫폼 관리자가 아닐 때 세 화면이 위에 거는 안내이자 잠긴 단추의 제목. */
  readOnly: "플랫폼 관리자만 바꿀 수 있는 설정이라 보기만 할 수 있습니다. 바꾸려면 플랫폼 관리자에게 요청해 주세요.",
  /** /admin/group-mappings. PUT 이 전체 교체라 저장 전에 한 번 묻는다. */
  groupMappings: {
    add: "매핑 추가",
    save: "저장 (전체 교체)",
    columns: { group: "Okta 그룹", tenant: "테넌트", role: "역할" },
    empty: "등록된 그룹 매핑이 없습니다. 매핑 추가로 Okta 그룹과 테넌트·role 을 잇습니다.",
    confirmTitle: "그룹 매핑 전체를 교체할까요?",
    confirmBody: (count: number): string =>
      `지금 표에 있는 ${count}개로 전체를 교체합니다. 표에서 지운 매핑으로 들어오던 사람은 다음 로그인 때 그 소속을 잃습니다.`,
    losesAdmin: "platform-admin 매핑이 하나도 남지 않습니다. 저장하면 이 화면에 다시 들어오지 못할 수 있습니다.",
    confirm: "전체 교체",
    /** 저장 전에 표에서 걸린 문제. */
    formError: {
      required: "그룹과 테넌트를 모두 입력해 주세요.",
      adminTenant: (group: string): string => `${group}: platform-admin 은 테넌트 * 에만, 테넌트 * 는 platform-admin 에만 매핑할 수 있습니다.`,
    },
    title: "Group Mappings",
    tenantPlaceholder: "테넌트 이름 또는 *",
  },
  /** /admin/settings. 칸 이름과 카드 제목은 두 언어 모두 영어다(D36). */
  platformSettings: {
    /** link 는 Tenant Settings 로 가는 링크다. */
    scopeHint: (link: ReactNode): ReactNode => (
      <>
        모든 테넌트에 적용되는 기본값입니다. 테넌트별로 덮어쓰려면 그 테넌트의{" "}
        {link}에서 합니다.
      </>
    ),
    /** names 는 키를 넣은 provider 이름을 쉼표로 이은 것이다. */
    savedKeys: (names: string): string => `저장했습니다. ${names} 의 키를 Secret 에 넣었습니다.`,
    /**
     * 허브 이미지 카드 (P55 6절). 저장은 이 카드의 단추 하나이고, 읽기 자격 증명은 같은 단추가 따로인 경로로 넣는다. 칸
     * 아래의 계산한 값(지문, 쓰는 항목, 겹치는 클러스터)은 플랫폼 관리자만 읽는다.
     */
    hubImages: {
      title: "허브 이미지",
      hint: "허브의 image 항목은 이 레지스트리에 있고 아래 공개 키로 서명된 이미지만 사용할 수 있습니다. 각 클러스터의 이미지 pull Secret 도 이 레지스트리를 받을 수 있어야 합니다.",
      prefix: "레지스트리 경로",
      prefixHint: "허브 이미지만 두는 경로를 적어 주세요.",
      publicKeys: "공개 키",
      publicKeysHint: "키를 바꾸는 동안에는 옛 키와 새 키를 함께 둡니다. 다시 서명한 뒤 옛 키를 빼도 이미 등록한 항목은 그대로 동작합니다.",
      revokedKeys: "회수한 키",
      revokedKeysHint: "유출된 키의 지문을 적으면 그 키로 허용한 항목에 경고가 붙습니다.",
      refused: "거부한 digest",
      refusedHint: "여기 적은 digest 로는 새 항목을 만들 수 없고, 이 digest 의 항목을 사용하는 Gateway 는 다음 재배포에서 거부됩니다. 이미 도는 항목에는 경고가 붙습니다.",
      refusedUsage: (count: number): string => `${count}개 항목에서 사용 중`,
      overlapWarning: (cluster: string): string => `허브 이미지 경로가 클러스터 ${cluster} 의 제품 이미지 경로와 겹칩니다. 겹치지 않는 경로로 바꿔 주세요.`,
      credential: "읽기 자격 증명",
      credentialHint: "레지스트리를 읽기만 하는 계정의 사용자 이름과 비밀번호입니다. 플랫폼 네임스페이스의 Secret 에 저장합니다.",
      /** 둘 가운데 하나만 입력했을 때 칸의 오류. */
      credentialBoth: "사용자 이름과 비밀번호를 함께 입력해 주세요.",
      /** 읽기 자격 증명을 넣었는지. 값은 화면에 다시 보이지 않는다. */
      credentialSet: "설정됨",
      credentialUnset: "비어 있음",
      username: "사용자 이름",
      password: "비밀번호",
    },
    /** OTLP 내보내기 카드 (P37 15절). 저장은 이 카드의 단추 하나다. */
    otlp: {
      title: "OTLP 내보내기",
      body: "Agent 실행의 트레이스를 OTLP/HTTP JSON 형식으로 외부 백엔드에 보냅니다. 사용자 식별자는 보내지 않습니다. JSON 을 받지 않는 백엔드라면 앞에 OTel Collector 를 두어 주세요.",
      enabled: "내보내기 켜기",
      endpoint: "Endpoint",
      endpointHint: "트레이스를 받을 주소입니다. https 주소만 허용하고, 평문 http 는 클러스터 안의 *.svc 주소만 허용합니다.",
      headersSecret: "헤더 Secret",
      headersSecretHint: "플랫폼 네임스페이스에 있는 Secret 의 이름입니다. 이름은 daegam-otlp- 로 시작해야 하고, 이 Secret 의 키와 값을 요청 헤더로 보냅니다.",
      sampleRatio: "샘플링 비율",
      sampleRatioHint: "0 에서 1 사이의 값입니다. 1 이면 모든 실행을 보냅니다.",
      sampleRatioInvalid: "0 에서 1 사이의 값을 입력해 주세요.",
      alwaysErrors: "오류로 끝난 실행은 샘플링 비율과 관계없이 항상 보냅니다",
      captureContent: "본문도 함께 보냅니다. Agent 의 record_bodies 도 켜져 있어야 합니다.",
      saved: "저장했습니다.",
      /** last 는 마지막 성공 시각이고 없으면 null 이다. 나머지는 화면 언어로 적은 수다. */
      status: (last: string | null, failures: string, dropped: string, queued: string): string =>
        `마지막 성공 ${last ?? "없음"} · 실패 ${failures}회 · 버린 실행 ${dropped}건 · 대기 중인 실행 ${queued}건`,
    },
    hints: {
      runtimeImage: "MCP Server 가 쓸 기본 이미지입니다. 비우면 daegam-mcp-runtime:0.1.0 입니다.",
      agentImage: "Agent 가 쓸 기본 이미지입니다. 비우면 daegam-agent-runtime:0.1.0 입니다.",
      model: "provider 에 모델을 따로 정하지 않았을 때 쓰는 모델입니다. 비우면 claude-opus-5 입니다.",
      cluster: "배포할 때 미리 선택해 두는 클러스터입니다. 비우면 선택하지 않습니다.",
      namespace: "{tenant}를 포함해야 합니다.",
      issuer: "OIDC 제공자의 issuer URL 입니다. 토큰을 검증할 키를 여기서 찾습니다.",
      clientId: "플랫폼이 로그인에 사용하는 클라이언트 ID 입니다.",
      audience: "컨트롤 플레인 토큰의 audience 입니다. 플랫폼 전체에 하나이고 데이터 플레인 토큰의 audience 와 다릅니다.",
    },
    oidcRedirect: "Okta 앱에 redirect URI 두 개를 등록합니다. http://localhost:5173/callback (개발) 과 배포 URL/callback 입니다.",
    /** 청지기 카드 (D31). */
    assistant: {
      title: "청지기 (플랫폼 비서)",
      body: "화면 오른쪽의 청지기 패널입니다. 사용자 신원으로 플랫폼 API 를 대신 부르며, 배포·삭제·설정 변경은 승인을 받은 뒤에 합니다. provider 는 아래 LLM providers 의 assistant default 이고, 비어 있으면 spec_generator default 를, 그것도 없으면 첫 provider 를 사용합니다. 켜져 있어도 provider 를 정하지 못하면 패널이 보이지 않습니다.",
      effortHint: "모델의 추론 강도입니다. low 가 가장 약하고 플랫폼 작업에는 충분합니다.",
      maxToolCallsHint: (max: number): string => `한 턴에서 부를 수 있는 Tool 호출 수입니다. 최대 ${max} 이며 그 위는 저장되지 않습니다.`,
      timeoutHint: "한 턴의 시간 상한입니다. 넘으면 turn_timeout 으로 턴을 닫습니다.",
      confirmDeploy: "배포 승인",
      confirmDeployHint: "끄면 배포 등급(배포·재배포·허브 게시)을 승인 없이 바로 합니다. 삭제·설정 변경은 언제나 승인을 받습니다.",
    },
    /** 웹 Tool 카드 (P39b 15절). 칸 이름은 두 언어 모두 영어다(D36). */
    webTools: {
      title: "웹 Tool",
      body: "플랫폼이 관리하는 web_fetch 와 web_search 입니다. 켜면 MCP Hub 에 플랫폼 항목으로 나타나고, Gateway 에 넣어 사용합니다. 테넌트 한도는 테넌트마다, 사용자 한도는 Agent 의 세션 소유자마다 적용됩니다.",
      fetch: "web_fetch",
      search: "web_search",
      perMinute: "Tenant limit / minute",
      perDay: "Tenant limit / day",
      perUserPerMinute: "User limit / minute",
      perUserPerDay: "User limit / day",
      provider: "Provider",
      endpoint: "Endpoint",
      endpointHint: "SearXNG 를 사용할 때 필요한 주소입니다. 다른 provider 는 비워 두면 기본 주소를 사용합니다.",
      providerNames: { brave: "Brave", tavily: "Tavily", searxng: "SearXNG", stub: "stub (테스트)" } satisfies Record<WebSearchProvider, string>,
      searchNotice: "검색어는 선택한 provider 에 보냅니다. 본문 기록을 켠 Agent 는 검색어와 URL 을 대화 기록에 남깁니다.",
      externalClients: "노출한 Gateway 에서도 허용",
      externalClientsHint: "켜면 외부 MCP 클라이언트도 노출한 Gateway 를 거쳐 웹 Tool 을 호출할 수 있습니다. 이때는 사용자 한도 없이 테넌트 한도만 적용됩니다.",
      apiKey: "API key",
      keyHint: "검색 provider 의 API 키입니다. 넣은 키는 플랫폼 네임스페이스의 Secret 에 들어가고 화면에는 다시 보이지 않습니다. SearXNG 와 stub 은 키가 없어도 됩니다.",
      checkUnsaved: "저장한 설정만 확인할 수 있습니다. 먼저 저장해 주세요.",
      /** ms 는 걸린 시간이다. */
      latency: (ms: number): string => `${ms}ms`,
      checkResult: {
        ok: "연결에 성공했습니다.",
        no_api_key: "API 키가 없습니다.",
        unauthorized: "provider 가 키를 거부했습니다.",
        quota: "provider 의 사용 한도에 도달했습니다.",
        unreachable: "provider 에 연결할 수 없습니다.",
        tls_failed: "provider 의 TLS 인증서를 검증하지 못했습니다. 사내망이면 네트워크 카드의 CA bundle 을 확인해 주세요.",
        bad_response: "provider 의 응답을 읽지 못했습니다.",
      } satisfies Record<WebSearchCheck["code"], string>,
    },
    /** 네트워크 카드 (P39b 15절). 설치 전체의 밖으로 나가는 연결 설정이다. */
    network: {
      title: "네트워크",
      caBundle: "CA bundle (PEM)",
      caHint: "TLS 검사 장비가 인증서를 다시 서명하는 사내망이나 사설 CA 를 쓰는 백엔드가 있으면 그 루트 인증서를 넣어 주세요. web_fetch, web_search 와 MCP Server 는 이 CA 가 서명한 인증서도 신뢰합니다. MCP Server 에는 다음 배포나 재배포 때 반영됩니다. 공개 인증서이므로 Secret 이 아닌 설정에 저장합니다.",
      /** n 은 지금 CA bundle 과 다른 CA 로 배포된 MCP Server 수다(data-plane-auth 의 mcp_servers_stale_ca). */
      staleCa: (n: number): string => `배포된 MCP Server ${n}개가 지금 CA bundle 과 다른 CA 로 돌고 있습니다. 다시 배포해야 바뀐 CA 를 사용합니다.`,
    },
    /** LLM providers 카드. */
    providers: {
      followSpecGenerator: "(spec_generator 를 따름)",
      add: "provider 추가",
      /** apiKey 는 키 칸의 자리표시 글이기도 하다. */
      columns: { name: "이름", kind: "종류", model: "모델", embedding: "임베딩 모델", apiKey: "API 키", connection: "연결" },
      empty: "등록된 provider 가 없습니다. provider 를 추가하고 API 키를 넣어야 LLM 기능을 쓸 수 있습니다.",
      secretHint:
        'API 키는 필수입니다. 넣은 키는 플랫폼이 플랫폼 네임스페이스의 Secret 에 넣고 화면에는 다시 보이지 않습니다. 운영자가 만든 Secret 을 쓰려면 "이미 있는 Secret 쓰기" 에 이름을 적습니다(키 항목은 api_key). 키가 없으면 저장은 되지만 LLM 작업이 422 provider_no_key 로 거부됩니다. 키를 바꾸면 청지기·스펙 생성기·Knowledge 인덱서는 바로 새 키를 사용하지만, 직접 경로의 Agent 는 재배포해야 새 키를 사용합니다. 게이트웨이 경로는 2분 안에 반영됩니다.',
    },
    /** provider 표의 한 줄. */
    provider: {
      embeddingHint: "Knowledge Base 가 쓰는 임베딩 모델입니다. 비우면 kind 기본입니다.",
      /** 임베딩 칸을 잠근 까닭. anthropic 은 임베딩 API 가 없고 openrouter·zai 는 임베딩 provider 로 쓰지 않는다. */
      noEmbedding: {
        openrouter: "OpenRouter provider 는 Knowledge Base 임베딩에 쓰지 않습니다",
        zai: "Z.AI provider 는 Knowledge Base 임베딩에 쓰지 않습니다",
      } satisfies Partial<Record<ProviderKind, string>>,
      noEmbeddingApi: "임베딩 API 가 없습니다",
      keyReplace: "바꿀 때만 입력",
      /** 키가 지금 어디에 있는지. 키 값은 화면에 다시 보이지 않는다. secret 은 Secret 이름이다. */
      key: {
        pending: "저장하면 Secret 에 넣습니다.",
        none: "키가 없습니다.",
        managed: (secret: string): string => `화면에서 넣은 키 · Secret ${secret}`,
        operator: (secret: string): string => `운영자가 만든 Secret ${secret}`,
      },
      useSecret: "이미 있는 Secret 쓰기",
      secretName: "Secret 이름",
      baseUrlDefault: "(기본값)",
      /** 스트리밍 응답에 사용량을 묻는가 (P37 2절). openai 계열 kind 만 보인다. */
      streamUsage: "스트리밍 사용량 받기",
      streamUsageHint: "stream_options 를 받지 않는 호환 서버라면 꺼 주세요. 끄면 토큰 수를 글자 수로 추정합니다.",
      check: "연결 확인",
      checkTitle: "Secret 과 키를 확인하고 provider 에 연결해 봅니다.",
      checkUnsaved: "저장한 provider 만 확인할 수 있습니다.",
      checking: "확인 중…",
      /** code 는 서버 오류 코드다. */
      checkFailed: (code: string): string => `확인하지 못했습니다: ${code}`,
      /** 연결 확인 결과 코드의 배지 문구. 서버는 고정 코드만 주고 provider 의 원문 메시지는 주지 않는다 (D25). */
      checkResult: {
        ok: "연결에 성공했습니다.",
        no_secret: "Secret 이 없습니다.",
        no_api_key: "Secret 에 api_key 가 없습니다.",
        auth: "provider 가 키를 거부했습니다.",
        connection: "provider 에 연결하지 못했습니다.",
        quota_exhausted: "키는 맞지만 크레딧을 다 썼거나 사용 한도에 도달했습니다.",
        provider_error: "provider 가 오류를 돌려줬습니다.",
      } satisfies Record<LLMProviderCheck["code"], string>,
      kindNames: { openai: "OpenAI", anthropic: "Anthropic", openrouter: "OpenRouter", zai: "Z.AI" } satisfies Record<ProviderKind, string>,
      baseUrl: "Base URL",
      /** 게이트웨이를 켰을 때 provider 줄에 붙는 "게이트웨이 반영" 상태 (P38 11절). 연결 확인 결과와 따로다. */
      gateway: {
        applied: "게이트웨이 반영됨",
        pending: "게이트웨이 반영 대기",
        rejected: "게이트웨이에서 사용하지 않음",
        hint: "연결 확인은 provider 를 바로 부릅니다. 게이트웨이는 저장한 키를 2분 안에 반영합니다.",
        /** at 은 키를 게이트웨이 사본에 넣은 시각이다. */
        keySynced: (at: string): string => `키 동기화 ${at}`,
      },
    },
    /** LLM 게이트웨이 카드 (P38 11절). 상태는 플랫폼 관리자만 읽고, 한도는 이 카드의 단추 하나로 저장한다. */
    llmGateway: {
      title: "LLM 게이트웨이",
      body: "켜면 home 클러스터의 Agent 와 청지기, 스펙 생성기, Knowledge 인덱서가 provider 대신 게이트웨이를 부릅니다. provider 키는 게이트웨이만 가지고, 켜고 끄는 것은 설치 env 의 DAEGAM_LLM_GATEWAY 입니다.",
      on: "켜짐",
      off: "꺼짐",
      address: "주소",
      namespace: "네임스페이스",
      /** applied 와 desired 는 설정 리비전이다. applied 가 없으면 아직 반영된 리비전이 없다. */
      revision: (applied: string | null, desired: string | null): string =>
        applied ? `반영된 리비전 ${applied} · 원하는 리비전 ${desired ?? "-"}` : "아직 반영된 리비전이 없습니다.",
      pods: (count: number): string => `게이트웨이 파드 ${count}개`,
      agents: (gateway: number, direct: number, moving: number): string => `게이트웨이 경로 Agent ${gateway}개 · 직접 경로 ${direct}개 · 옮기는 중 ${moving}개`,
      cleanups: (count: number): string => `롤아웃 뒤의 정리를 기다리는 Agent ${count}개`,
      revocations: (listed: number, pending: number): string => `회수한 토큰 ${listed}개 · 아직 반영되지 않은 회수 ${pending}개`,
      ingest: (expires: string | null, received: string | null): string => `사용량 수집 토큰 만료 ${expires ?? "없음"} · 마지막 수신 ${received ?? "없음"}`,
      /** tenants 는 쉼표로 이은 테넌트 이름이다. */
      clashing: (tenants: string): string =>
        `테넌트 ${tenants}${particle(tenants, "이/가")} 게이트웨이 네임스페이스를 사용하고 있습니다. DAEGAM_LLM_GATEWAY_NAMESPACE 를 먼저 바꿔 주세요.`,
      reasonsTitle: "직접 경로인 이유",
      /** 동기화가 적은 이유와 경고. 표에 없는 코드는 코드 그대로 보인다. */
      codes: {
        llm_gateway_namespace_missing: "게이트웨이 네임스페이스가 없습니다. 대감 차트를 llmGateway.mode: agentgateway 로 설치하거나 업그레이드해 주세요.",
        llm_gateway_namespace_taken: "게이트웨이 네임스페이스에 테넌트 라벨이 있어 아무것도 렌더하지 않습니다.",
        llm_gateway_foreign_install: "다른 설치가 표시한 네임스페이스라 아무것도 렌더하지 않습니다.",
        llm_gateway_no_home_cluster: "home 클러스터가 없어 게이트웨이를 렌더하지 않습니다.",
        no_signer: "플랫폼에 서명 키가 없어 토큰을 낼 수 없습니다.",
        llm_gateway_images_missing: "DAEGAM_LLM_GATEWAY_IMAGES 가 비었거나 잘못됐습니다.",
        llm_gateway_apply_failed:
          "게이트웨이를 클러스터에 적용하지 못했습니다. 원인은 플랫폼 로그에 있으며, 플랫폼이 잠시 뒤 다시 시도합니다.",
        llm_gateway_config_rejected:
          "게이트웨이 파드가 새 설정을 받지 않아 마지막으로 반영된 설정으로 되돌렸습니다. 그동안의 회수와 키 교체는 반영되지 않았습니다.",
        llm_gateway_config_dollar: "설정에 $ 가 들어 있어 렌더하지 않았습니다.",
        llm_gateway_config_too_large: "설정이 Secret 한도인 1 MiB 를 넘어 렌더하지 않았습니다.",
        llm_gateway_provider_rejected: "주소 검사를 지나지 못한 provider 가 있습니다. 그 provider 를 사용하는 Agent 는 직접 경로에 남습니다.",
        llm_gateway_paused: "게이트웨이 동기화가 멈춰 있습니다. e2e 가 네임스페이스에 멈춤 표시를 달아 두었습니다.",
        llm_ratelimit_unavailable: "한도 서비스나 Redis 가 준비되지 않아 한도 없이 요청을 통과시킵니다.",
        llm_gateway_revocations_large: "회수 목록이 5,000개를 넘었습니다.",
      } satisfies Record<string, string>,
      /** 분당 토큰 한도. Platform Settings 의 llm_gateway.limits 이고 PATCH 로 저장한다. */
      limits: {
        title: "분당 토큰 한도",
        body: "한도는 응답의 토큰 수를 1분 동안 셉니다. 한도를 넘긴 요청은 끝까지 처리되고, 그다음 요청부터 거부됩니다. 세션과 하루 예산은 Agent 가 따로 지킵니다.",
        tenantDefault: "테넌트 기본 한도",
        tenantDefaultHint: "모든 테넌트에 적용합니다. 비우면 한도가 없습니다.",
        tenants: "테넌트별 한도",
        providers: "provider 별 한도",
        providersHint: "그 provider 를 부르는 모든 테넌트를 함께 셉니다.",
        tenant: "테넌트",
        provider: "provider",
        tokens: "분당 토큰",
        add: "줄 추가",
        unknownTenant: "이 이름의 테넌트가 없습니다.",
        invalid: "1 에서 1,000,000,000 사이의 정수를 입력해 주세요.",
        saved: "저장했습니다.",
      },
    },
    /** MCP 토큰 발급자 카드 (P53b 6.9절). oidc 모드에서만 보인다. */
    issuer: {
      title: "MCP 토큰 발급자",
      body: "Gateway, 노출한 MCP Server, 노출한 Agent 가 어느 발급자의 사람 토큰을 받는지 보입니다. 바꾸는 것은 설치 env 의 DAEGAM_DATA_PLANE_ISSUER 입니다.",
      trusted: { platform: "플랫폼 토큰", idp: "IdP 토큰" },
      held: "설정은 platform 이지만 mcp_group 이 다른 테넌트의 고정 그룹이거나 daegam- 으로 시작하는 테넌트가 있어, 플랫폼이 IdP 토큰을 받는 상태로 시작했습니다. 아래 목록에 있는 테넌트의 mcp_group 을 고친 뒤 플랫폼을 다시 시작해 주세요.",
      done: "전환을 마쳤습니다. 모든 클러스터가 플랫폼 토큰을 받습니다.",
      pending: (count: number): string =>
        `전환 중입니다. 다시 렌더할 문서, 플랫폼 토큰을 아직 받지 않은 곳, 아직 확인하지 않은 클러스터가 모두 ${count}개 남았습니다.`,
      columns: {
        cluster: "클러스터",
        probedAt: "확인 시각",
        platformNamespaces: "플랫폼 네임스페이스",
        idpNamespaces: "IdP 네임스페이스",
        pendingDocuments: "렌더 대기",
        waitingVerifiers: "확인 대기",
        verifiedVerifiers: "확인 완료",
      },
      notProbed: "아직 확인하지 않음",
      probeFailed: "확인 실패",
      pendingDocumentsTitle: "다시 렌더를 기다리는 문서",
      waitingVerifiersTitle: "플랫폼 토큰을 아직 받지 않은 곳",
      blocked: "요청 오류로 막힘",
      refused: "401 거절",
      notCalled: "아직 부르지 못함",
      /** 렌더를 기다리는 문서 가운데 공유 서버의 복제본. 다른 종류는 resources.kind 의 이름이다. */
      sharedClone: "공유 복제본",
      /** clusters 는 쉼표로 이은 클러스터 id 다. */
      jwksUnreachable: (clusters: string): string =>
        `플랫폼 발급자 주소가 home 클러스터 안에서만 접근할 수 있는 주소라 원격 클러스터 ${clusters}${particle(clusters, "은/는")} 플랫폼 토큰을 검증할 수 없습니다. DAEGAM_PLATFORM_ISSUER 를 원격 클러스터에서도 접근할 수 있는 주소로 바꿔 주세요.`,
      /** largest 는 지난 하루 확인한 사람 기록의 가장 긴 IdP 액세스 토큰 수명(초)이고 없으면 null 이다. */
      tokenLife: (largest: number | null, limit: number): string =>
        `지난 하루 가장 긴 IdP 액세스 토큰 수명 ${largest === null ? "없음" : `${largest}초`} · 기준 ${limit}초 미만`,
      tokenLifeWarn:
        "IdP 액세스 토큰 수명이 기준 이상이라 화면을 열어 둔 사람의 긴 대화에서도 Tool 호출이 멈출 수 있습니다. IdP 의 액세스 토큰 수명을 줄이거나 DAEGAM_TURN_OWNER_CONFIRM_SECONDS 를 늘려 주세요.",
      listsTitle: "전환이 바꾸거나 열어 두는 것",
      listsEmpty: "확인할 것이 없습니다.",
      /** 두 값 모두에서 보이는 목록. 전환하기 전에 읽도록 둔다. */
      lists: {
        tenants_foreign_mcp_group: {
          label: "mcp_group 이 다른 테넌트의 고정 그룹이거나 daegam- 으로 시작하는 테넌트",
          note: "이 값이 남아 있으면 플랫폼 토큰으로 전환할 수 없습니다.",
        },
        gateways_idp_names: {
          label: "IdP 그룹이나 클라이언트 이름으로 호출자를 가리는 Gateway",
          note: "플랫폼 토큰에서는 이 조건이 맞지 않습니다. 조건을 Tool 규칙의 역할이나 그룹으로 옮겨 주세요.",
        },
        gateways_permit: {
          label: "Authz policy 에 permit 문장이 있는 Gateway",
          note: "전환한 뒤에도 조건에 맞는 다른 테넌트의 플랫폼 토큰을 받습니다. local 모드와 같으며, IdP 토큰을 받는 동안에는 조건에 맞는 모든 IdP 토큰을 받습니다.",
        },
        exposed_agents_foreign_groups: {
          label: "allowed_groups 에 다른 테넌트의 그룹이 있는 노출한 Agent",
          note: "그 테넌트의 사람, 서비스 어카운트, Agent 의 플랫폼 토큰도 받습니다.",
        },
        exposed_agents_open: {
          label: "allowed_groups 가 비어 있는 노출한 Agent",
          note: "audience 가 맞는 모든 IdP 토큰을 받습니다.",
        },
        exposed_agents_one_issuer: {
          label: "IdP 토큰만 받는 노출한 Agent",
          note: "런타임 이미지가 두 발급자를 지원하지 않습니다. 새 이미지로 다시 배포해 주세요.",
        },
        tenants_agent_client_ids: {
          label: "agent_client_ids 에 IdP 클라이언트를 적은 테넌트",
          note: "플랫폼 토큰으로 전환하면 Gateway 와 노출 앞단이 이 클라이언트의 토큰을 거절합니다.",
        },
        agents_gateway_sources: {
          label: "Gateway 소스를 사용하는 Agent",
          note: "전환할 때 한 번 다시 시작합니다.",
        },
        gateways_published_refused: {
          label: "게시된 Gateway 를 백엔드로 사용할 수 없는 Gateway",
          note: "IdP 토큰을 받는 동안에는 게시된 Gateway 를 백엔드로 사용할 수 없습니다. 플랫폼 토큰으로 전환하면 다시 배포할 때 사용할 수 있습니다.",
        },
      },
    },
    /** Platform Settings 의 칸 이름과 카드 제목. 두 언어 모두 영어다(D36). */
    labels: {
      title: "Platform Settings",
      tenantSettings: "Tenant Settings",
      defaults: "Defaults",
      updated: "updated",
      runtimeImage: "Default runtime image",
      agentImage: "Default agent image",
      model: "Default model",
      cluster: "Default cluster",
      homeCluster: "(home 클러스터)",
      namespace: "Namespace template",
      oidc: "OIDC",
      configured: "configured",
      issuer: "Issuer",
      clientId: "Client ID",
      audience: "Audience",
      enabled: "enabled",
      effort: "Effort",
      maxToolCalls: "Max tool calls / turn",
      turnTimeout: "Turn timeout (s)",
      providers: "LLM providers",
      specGenerator: "spec_generator default",
      firstProvider: "(첫 번째 provider)",
      assistantProvider: "assistant default",
    },
  },
  /** /admin/users. local 인증 모드(D40)의 사용자 관리. */
  users: {
    notLocal: (mode: Exclude<AuthMode, "local" | "unknown">): string =>
      `사용자 관리는 local 인증 모드에서만 있습니다. 지금은 ${mode === "oidc" ? "IdP 가 사용자를 관리합니다." : "인증 없이 동작하는 개발 모드입니다."}`,
    add: "사용자 추가",
    /** 표의 머리이자 사용자 추가 폼의 칸 이름. */
    username: "아이디",
    displayName: "이름",
    email: "이메일",
    status: "상태",
    lastLogin: "마지막 로그인",
    disabled: "사용 막힘",
    mustChangePassword: "비밀번호 변경 대기",
    never: "없음",
    makeAdmin: "관리자로 지정",
    revokeAdmin: "관리자 해제",
    enable: "사용 허용",
    disable: "사용 막기",
    /** 줄 메뉴. tokensHint 는 MCP 토큰 항목의 풀이다. OAuth 인가 서버가 켜져 있으면 연결도 다룬다. */
    tokens: "MCP 토큰",
    tokensHint: { grants: "이 사람의 개인 MCP 토큰과 MCP 클라이언트 연결을 보고 끊습니다.", tokens: "이 사람의 개인 MCP 토큰을 보고 회수합니다." },
    resetPassword: "비밀번호 재설정",
    delete: "삭제",
    deleting: "삭제 중…",
    empty: "사용자가 없습니다.",
    deleteTitle: (username: string): string => `사용자 ${username} 삭제`,
    deleteBody: "계정과 모든 테넌트 소속이 함께 사라집니다. 되돌릴 수 없습니다.",
    /** 사용자 추가 카드. 제목은 add 다. */
    create: {
      usernameHint: "소문자·숫자·점·밑줄·하이픈, 2자 이상입니다. 만든 뒤에는 바꿀 수 없습니다.",
      password: "처음 비밀번호",
      passwordHint: (min: number): string => `${account.password.rule(min)} 첫 로그인 때 본인이 바꿉니다.`,
      platformAdmin: "platform-admin 으로 만듭니다 (모든 테넌트와 플랫폼 설정을 다룹니다)",
      creating: "만드는 중…",
    },
    /** 비밀번호 재설정 대화상자. 새 비밀번호 칸의 이름은 account.password.next 다. */
    reset: {
      title: (username: string): string => `${username} 의 비밀번호 재설정`,
      body: "새 임시 비밀번호를 정합니다. 이 사람의 다른 세션은 모두 끝나고, 다음 로그인 때 이 임시 비밀번호로 들어와 본인이 다시 바꿉니다.",
      confirm: "재설정",
      pending: "재설정하는 중…",
    },
    /** 한 사람의 MCP 토큰 대화상자. 연결 목록의 제목은 account.tokens.grantsTitle 이다. */
    tokensDialog: {
      title: (username: string): string => `${username} 의 MCP 토큰`,
      personal: "개인 토큰",
    },
    title: "Users",
  },
};
