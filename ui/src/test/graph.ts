import { dictionary } from "@/i18n";
import { connectionProblem, connectionText, issueText, type Issue } from "@/lib/agentGraph";

/** 이슈 목록의 code 를 순서대로 집는다. 그래프 검증은 서버와 문장이 다르고 code 가 같으므로 판정을 code 로 견준다. */
export const codes = (issues: readonly { code: string }[]): string[] => issues.map((i) => i.code);

/** 이슈 한 건의 한국어 화면 문장. 이슈는 code 와 인자만 들므로 화면과 같은 issueText 로 그려서 견준다. 없으면 undefined 다. */
export const textOf = (issue: Issue | undefined): string | undefined => (issue ? issueText(dictionary("ko"), issue) : undefined);

/** 연결할 수 없는 까닭의 한국어 화면 문장. connectionProblem 은 키와 인자만 내므로 카드와 같은 connectionText 로 그린다. 이을 수 있으면 null 이다. */
export const connectionHintOf = (...args: Parameters<typeof connectionProblem>): string | null => {
  const problem = connectionProblem(...args);
  return problem && connectionText(dictionary("ko"), problem);
};
