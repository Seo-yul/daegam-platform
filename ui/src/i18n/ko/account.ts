import type { GrantRevokeReason, OAuthGrantStatus, PersonalTokenStatus, RevokeReason } from "@/api/types";
import { particle } from "@/lib/particle";

/**
 * 계정과 발급자 사건. 개인 토큰의 회수 이유와 OAuth 연결(P33)의 끊긴 이유가 같은 사건을 같은 말로 쓴다. 두 목록 모두
 * `AccessRevoker` 와 접근 정리가 같은 사건에서 함께 끊기 때문이다. 펼쳐 넣은 키는 바깥의 satisfies 가 보지 못해 여기서 지킨다.
 */
const accountEvents = {
  disabled: "계정 막힘",
  deleted: "계정 삭제",
  password: "비밀번호 변경",
  membership: "테넌트 소속 해제",
  issuer_changed: "발급자 주소 변경",
  key_rotated: "서명 키 교체",
} satisfies Partial<Record<Extract<RevokeReason, GrantRevokeReason>, string>>;

/**
 * 2.0.0-rc1 이 모르는 회수 까닭 (P53b 6.5절). API 가 개인 토큰과 연결의 사유 자리에 함께 싣는다. 역할과 IdP 그룹, 테넌트 그룹,
 * 서비스 어카운트의 까닭은 토큰에만 생기지만 두 목록이 같은 말을 쓴다.
 */
const revokeCauses = {
  role_changed: "역할 낮아짐",
  unconfirmed: "오래 로그인하지 않음",
  groups_changed: "IdP 그룹 변경",
  wrong_group: "테넌트 그룹 불일치",
  accounts_off: "서비스 어카운트 꺼짐",
} satisfies Partial<Record<Extract<RevokeReason, GrantRevokeReason>, string>>;

const rule = (min: number): string => `${min}자 이상이어야 합니다.`;

/** 계정 화면 (내 MCP 토큰, 비밀번호 바꾸기)과 사이드바 맨 아래의 계정 메뉴(AccountMenu). */
export const account = {
  /** 계정 메뉴(AccountMenu)의 줄과 계정 줄의 글. headerMode 는 header 모드의 계정 줄 둘째 줄이다. */
  menu: {
    tokens: "내 MCP 토큰",
    password: "비밀번호 바꾸기",
    logout: "로그아웃",
    login: "로그인",
    signedOut: "로그인하지 않았습니다",
    headerMode: (sub: string | null): string => `헤더 인증 모드${sub !== null ? ` · ${sub}` : ""}`,
    signedIn: "로그인됨",
    approvalChoices: "기억한 선택",
  },
  /** /account/approval-choices (P43 5절). 승인 카드에서 기억한 승인과 거부다. Agent·Tool·만료 칸은 다른 화면의 이름을 쓴다. */
  approvalChoices: {
    title: "기억한 선택",
    empty: "기억한 선택이 없습니다.",
    choice: "선택",
    approve: "항상 승인",
    reject: "항상 거부",
  },
  /** /account/runs-as-me (P41 6절). 나에게 묶여 도는 일정과 트리거다. 계정 메뉴의 줄도 title 을 쓴다. */
  runsAsMe: {
    title: "나를 대신하는 자동 실행",
    empty: "나를 대신해 실행하는 일정이나 트리거가 없습니다.",
  },
  /** /account/tokens. */
  tokens: {
    title: "내 MCP 토큰",
    /** header 모드에서만 보인다. oidc 모드에도 개인 토큰이 있다 (P53b 6.5절). */
    headerMode: "개인 MCP 토큰은 header 인증 모드에서는 없습니다. 지금은 인증 없이 동작하는 개발 모드입니다.",
    /** oidc 모드에서 데이터 플레인 발급자가 아직 IdP 인 동안 (P53b 6.2절·6.5절). */
    idpIssuer:
      "이 배포본의 Gateway, 노출한 MCP Server, 노출한 Agent 는 아직 IdP 가 낸 토큰만 받습니다. 여기서 만든 토큰은 플랫폼 관리자가 플랫폼 토큰으로 전환한 뒤부터 사용할 수 있습니다.",
    /** oidc 모드의 긴 한도 (P53b 6.3절). days 는 DAEGAM_IDP_CONFIRM_SECONDS 의 날 수다. */
    confirmHint: (days: number): string =>
      `대감에 ${days}일 동안 로그인하지 않으면 이 화면의 토큰과 MCP 클라이언트 연결이 모두 회수됩니다. 대감은 로그인할 때마다 IdP 에서 이 계정이 아직 유효한지 확인합니다.`,
    newToken: "새 토큰",
    noTenant: "소속된 테넌트가 없어 토큰을 받을 수 없습니다. 테넌트 관리자에게 멤버 추가를 요청해 주세요.",
    issued: "발급한 토큰",
    grantsTitle: "연결된 MCP 클라이언트",
    grantsHelp: "MCP 클라이언트가 브라우저 로그인과 허용으로 맺은 연결입니다. 액세스 토큰은 클라이언트가 15분마다 스스로 새로 받습니다.",
    // 토큰에 실린 역할과 Tool 규칙 (P52 3절)
    rolesHint:
      "토큰에는 만들 때의 역할이 실립니다. 역할이 낮아지면 토큰은 회수되고, 높아진 역할은 이미 만든 토큰에 반영되지 않습니다. 역할을 선택한 Tool 규칙을 통과하려면 토큰을 다시 만들어 주세요.",
  },
  /** 개인 토큰 발급 폼. 연결 카드도 쓴다. */
  issue: {
    tenant: "테넌트",
    tenantHint: "토큰은 이 테넌트의 Gateway, 노출한 MCP Server, 노출한 Agent 를 부를 수 있습니다.",
    tenantPlaceholder: "테넌트 선택",
    name: "이름",
    nameHint: "어느 기기나 클라이언트에 넣었는지 알아볼 이름입니다.",
    expiry: "만료",
    expiryDays: (days: number): string => `${days}일`,
    expiryYear: "1년",
    pickDate: "날짜 선택",
    expiryDate: "만료 날짜",
    dateHint: (maxDays: number): string => `내일부터 ${maxDays}일 뒤까지 선택할 수 있습니다.`,
    password: "현재 비밀번호",
    passwordHint: "오래 쓰는 토큰이라 비밀번호를 다시 확인합니다.",
    /** oidc 모드의 폼에는 비밀번호 칸이 없다 (P53b 6.4절). */
    reauthHint: "오래 사용하는 토큰이라 최근 10분 안에 로그인하지 않았다면 IdP 로그인을 한 번 더 거칩니다.",
    wrongPassword: "비밀번호가 맞지 않습니다.",
    issuing: "발급하는 중…",
    submit: "토큰 발급",
    readyAt: (clock: string): string => `${clock}부터 사용할 수 있습니다.`,
  },
  /** 한 번만 보이는 토큰 칸. Backend API 의 읽기 토큰도 쓴다. */
  oneTime: { notice: "이 토큰은 지금 한 번만 보입니다. 복사해 주세요.", copyLabel: "토큰" },
  /** 발급한 토큰 목록. */
  tokenTable: {
    columns: { name: "이름", tenant: "테넌트", created: "만든 날", expiry: "만료", status: "상태" },
    revoke: "회수",
    empty: "발급한 토큰이 없습니다.",
    revokeTitle: (label: string): string => `토큰 ${label} 회수`,
    revokeBody: "이 토큰을 사용하는 MCP 클라이언트의 요청은 16분 안에 거절됩니다. 회수한 토큰은 되살릴 수 없습니다.",
    revoking: "회수하는 중…",
    readyFrom: (clock: string): string => `${clock}부터`,
    status: {
      active: "사용할 수 있음",
      pending: "준비 중",
      revoking: "회수 중",
      revoked: "회수됨",
      expired: "만료됨",
    } satisfies Record<PersonalTokenStatus, string>,
    reason: {
      user: "본인이 회수",
      admin: "관리자가 회수",
      ...accountEvents,
      ...revokeCauses,
      key_missing: "서명 키 없음",
    } satisfies Record<RevokeReason, string>,
  },
  /** OAuth 로 맺은 MCP 클라이언트 연결 목록. */
  grants: {
    columns: { client: "클라이언트", tenant: "테넌트", target: "대상", connected: "연결한 날", refreshed: "마지막 갱신", status: "상태" },
    disconnect: "끊기",
    empty: "연결된 MCP 클라이언트가 없습니다.",
    disconnectTitle: (client: string): string => `${client} 연결 끊기`,
    disconnectBody: "이 클라이언트는 더 이상 토큰을 갱신할 수 없고 16분 안에 요청이 거절됩니다. 다시 사용하려면 클라이언트에서 다시 로그인해야 합니다.",
    disconnecting: "끊는 중…",
    status: { active: "연결됨", expired: "만료됨", revoked: "끊김" } satisfies Record<OAuthGrantStatus, string>,
    reason: {
      user: "본인이 끊음",
      admin: "관리자가 끊음",
      client: "클라이언트가 끊음",
      ...accountEvents,
      ...revokeCauses,
      resource_gone: "대상 노출 해제",
      oauth_disabled: "로그인 연결 꺼짐",
      client_disallowed: "허용 목록에서 빠진 클라이언트",
      code_reuse: "인가 코드 재사용",
      refresh_reuse: "갱신 토큰 재사용",
      evicted: "연결 수 상한 초과",
    } satisfies Record<GrantRevokeReason, string>,
    /** 클라이언트 이름이 어디서 왔는지. 연결 허용 화면도 쓴다. */
    origin: {
      cimd: (host: string): string => `${host}${particle(host, "이/가")} 공개한 클라이언트 정보`,
      self: "클라이언트가 스스로 밝힌 이름",
    },
  },
  /** /account/password 와 비밀번호 규칙. 로그인과 사용자 관리 화면도 규칙과 잠금 문장을 쓴다. */
  password: {
    title: "비밀번호 바꾸기",
    required: "처음 로그인했거나 관리자가 비밀번호를 재설정했습니다. 계속하려면 비밀번호를 바꿔 주세요.",
    tokenWarning: (count: number): string => `비밀번호를 바꾸면 개인 MCP 토큰 ${count}개가 모두 회수됩니다.`,
    current: "현재 비밀번호",
    currentHint: "처음 받은 비밀번호나 관리자가 재설정해 준 임시 비밀번호입니다.",
    next: "새 비밀번호",
    nextHint: (min: number): string => `${rule(min)} 짧고 복잡한 비밀번호보다 긴 문장이 더 안전합니다.`,
    again: "새 비밀번호 확인",
    mismatch: "두 칸에 입력한 새 비밀번호가 서로 다릅니다.",
    changing: "바꾸는 중…",
    submit: "비밀번호 바꾸기",
    wrongCurrent: "현재 비밀번호가 맞지 않습니다.",
    weak: (min: number): string => `새 비밀번호는 ${min}자 이상이어야 하고, 아이디와 같을 수 없습니다.`,
    failed: (code: string): string => `비밀번호를 바꾸지 못했습니다 (${code}).`,
    rule,
    tooShort: (length: number, more: number): string => `지금 ${length}자입니다. ${more}자 더 입력해야 합니다.`,
    locked: "로그인 시도가 너무 많아 계정이 잠시 잠겼습니다. 나중에 다시 시도해 주세요.",
    lockedFor: (minutes: number): string => `로그인 시도가 너무 많아 계정이 잠겼습니다. ${minutes}분 뒤 다시 시도해 주세요.`,
  },
};
