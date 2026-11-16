/**
 * MCP 클라이언트(P33)를 화면에 말하는 규칙. 연결 허용 화면과 연결 목록이 같이 쓴다. 말은 사전의 `account.grants.origin` 이다.
 *
 * 클라이언트 이름은 클라이언트가 밝힌 것이라 신원이 아니다. CIMD 면 신원 대신 문서를 공개한 호스트를, DCR 이면 스스로
 * 밝힌 이름이라는 것을 곁에 적는다(P33 16절).
 */
import type { OAuthClientKind } from "@/api/types";
import type { Messages } from "@/i18n";

/** 이름이 어디서 왔는가. 뒤에 서술어 없이 붙일 수 있는 명사구다. */
export function clientOrigin(t: Messages, kind: OAuthClientKind, host: string | null): string {
  if (kind === "cimd" && host) return t.account.grants.origin.cimd(host);
  return t.account.grants.origin.self;
}
