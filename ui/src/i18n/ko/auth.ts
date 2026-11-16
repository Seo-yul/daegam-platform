import { particle } from "@/lib/particle";

const restart = "클라이언트에서 연결을 다시 시작해 주세요.";
const notAMember = "이 테넌트의 멤버만 연결할 수 있습니다.";

/** 로그인(D40), OIDC 콜백과 MCP 클라이언트 연결 허용(P33 16절). 화면 문구는 합니다체다. */
export const auth = {
  /** 로그인 폼의 두 칸. */
  username: "아이디",
  password: "비밀번호",
  signingIn: "로그인 중…",
  /** 이름을 밝히지 않은 MCP 클라이언트의 이름 (P49 6절, lib/serverLiterals.ts). 연결 허용 화면과 연결 목록이 같이 쓴다. */
  unnamedClient: "이름 없는 클라이언트",
  /**
   * oidc 에서 오래 가는 자격을 만들기 전에 다시 로그인했는데(max_age, 그다음 prompt=login 과 max_age) IdP 가 새 로그인을 알려
   * 주지 않았거나, 플랫폼이 다시 로그인하라는데 지금 로그인이 이미 최근이다 (P53b 6.4절). 토큰 발급 폼과 연결 허용 화면이 쓴다.
   */
  reauthFailed: "IdP 에서 새로 로그인한 기록을 받지 못해 진행할 수 없습니다. 잠시 뒤 다시 시도하고, 계속되면 플랫폼 관리자에게 알려 주세요.",
  /** /login. 제출 단추는 account.menu.login 이고, 서버에 닿지 못했을 때는 common.networkError 다. */
  login: {
    title: "대감 로그인",
    reauth: "보안을 위해 다시 로그인해 주세요.",
    /** 없는 사용자와 틀린 비밀번호는 서버가 같은 401 을 내므로 한 문장이다. 잠김은 account.password 의 잠금 문장이다. */
    errors: {
      account_disabled: "이 계정은 사용이 막혀 있습니다. 플랫폼 관리자에게 문의해 주세요.",
      invalid_credentials: "아이디 또는 비밀번호가 맞지 않습니다.",
    },
    failed: (code: string): string => `로그인하지 못했습니다 (${code}).`,
  },
  /** /callback. OIDC 응답을 처리하지 못했을 때 다시 해 볼 길과 나갈 길을 준다. */
  callback: {
    failed: "로그인을 마치지 못했습니다. 다시 로그인하거나 잠시 뒤에 다시 시도해 주세요.",
    retry: "다시 로그인",
    home: "홈으로",
  },
  /** /oauth/consent. */
  consent: {
    title: "MCP 클라이언트 연결",
    checking: "확인하는 중입니다…",
    /** header 모드. 플랫폼이 사람 토큰을 내지 않는다. */
    headerMode: "이 배포본은 MCP 로그인 연결을 쓰지 않습니다.",
    inspecting: "요청을 확인하는 중입니다…",
    leaving: "클라이언트로 돌아갑니다. 이 탭은 닫아도 됩니다.",
    /** client 는 클라이언트가 밝힌 이름이다. */
    heading: (client: string): string => `${client}${particle(client, "이/가")} 대감에 연결하려고 합니다`,
    /** origin 은 이름이 어디서 왔는지의 명사구(account.grants.origin)다. */
    origin: (origin: string): string => `${origin}입니다.`,
    nameCollides: "잘 알려진 클라이언트의 이름을 쓰고 있지만 스스로 밝힌 이름입니다. 직접 시작하지 않았다면 거절해 주세요.",
    publicUrl: "공개 주소",
    tenant: "테넌트",
    /** user 는 로그인한 사람의 이름이고, 모르면 null 이다. */
    scope: (tenant: string, user: string | null, minutes: number, days: number): string =>
      `${tenant}의 Gateway, 노출한 MCP Server, 노출한 Agent 를 ${user ?? "이 계정"}의 이름으로 부를 수 있습니다. 액세스 토큰은 ${minutes}분마다 새로 받고 연결은 최대 ${days}일 유지됩니다.`,
    /** where 는 결과를 받을 loopback 주소(host:port)다. */
    loopback: (where: string): string =>
      `로그인 결과를 이 컴퓨터(${where})로 보냅니다. 이 컴퓨터의 어떤 프로그램이든 이 클라이언트를 내세울 수 있습니다. 이 연결을 직접 시작하지 않았다면 거절해 주세요.`,
    approve: "허용",
    deny: "거절",
    notAMember,
    /** 표에 없는 코드와 코드 없는 실패. 그 밖의 글자는 화면에 싣지 않는다. */
    generic: "요청을 처리하지 못했습니다. 클라이언트에서 다시 시작해 주세요.",
    /** 서버 오류 코드(`?error=` 와 API 오류)의 문구. */
    errors: {
      invalid_client: `클라이언트를 확인하지 못했습니다. ${restart}`,
      invalid_redirect_uri: `클라이언트가 보낸 돌아갈 주소가 등록된 주소와 다릅니다. ${restart}`,
      client_metadata_unavailable: "클라이언트 정보를 받지 못했습니다. 잠시 뒤 클라이언트에서 다시 시작해 주세요.",
      request_expired: `요청이 만료됐습니다. ${restart}`,
      request_used: `이미 답한 요청입니다. ${restart}`,
      invalid_target: `연결하려는 대상이 더 이상 노출돼 있지 않습니다. ${restart}`,
      not_a_member: notAMember,
      consent_limit: "짧은 시간에 연결을 너무 많이 허용했습니다. 10분 뒤 다시 시도해 주세요.",
      // oidc 설치의 쉬는 OAuth 서버(503, P53 B 6.5). 플랫폼 토큰으로 전환하면 깨어난다
      temporarily_unavailable: "지금은 MCP 로그인 연결을 허용할 수 없습니다. 플랫폼 관리자가 플랫폼 토큰으로 전환한 뒤 클라이언트에서 다시 시작해 주세요.",
    },
  },
};
