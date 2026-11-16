/**
 * 서버 오류 코드를 사람이 읽는 한 문장으로 바꾼다. 문장은 사전(i18n/{ko,en}/errors.ts)에 있고 여기는 코드를 표에 태우는
 * 규칙만 둔다.
 *
 * 서버는 `{code, detail}` 을 내는데 detail 은 영어이고 code 는 기계 어휘다. 표에 없는 코드는 describeError 가 폴백 문장 뒤에
 * 서버 detail 을 붙여 준다.
 */
import type { Messages } from "@/i18n";
import { ownValue } from "@/lib/utils";

/**
 * 409 참조 목록에서 푸는 키. 서버가 `in_use` 의 extra 로 싣는다. 하나라도 빠지면 화면이 무엇을 떼야 하는지 못
 * 알려 준다. 키마다 화면 이름은 사전의 `errors.refNames` 다.
 */
export const REF_KEYS = [
  "mcp_server_ids",
  "deployed_mcp_server_ids",
  "gateway_ids",
  "agent_ids",
  "deployed_agent_ids",
  "knowledge_base_ids",
  "token_ids",
] as const;

export type RefKey = (typeof REF_KEYS)[number];

/**
 * 서버 코드 꼴인가(소문자·숫자·밑줄). 오류의 `코드: 설명` 줄과 경고의 `코드: 설명` 이 같은 꼴이라 둘 다 이것으로 코드를 가른다.
 */
export function isCodeShape(head: string): boolean {
  return /^[a-z0-9_]+$/.test(head);
}

/** 코드에 맞는 문장. 표에 없으면 null 이고 호출부가 폴백을 쓴다. */
export function errorText(t: Messages, code: string): string | null {
  return ownValue(t.errors.codes, code) ?? null;
}

/**
 * Knowledge 문서와 Knowledge Base 의 오류 코드 문구. ErrorMessage 의 `code: detail` 줄 아래에 붙고 여기 없는 코드는 줄이 없다.
 * 예전에는 lib/knowledge.ts 가 따로 표를 가져 같은 코드에 다른 문장이 둘 생겼다(D43). URL 문서의 실패 코드는 `url_`·`fetch_`
 * 가족이라 코드 하나로 못 찾고 접두로 묶는다.
 */
export function knowledgeErrorText(t: Messages, code: string | null | undefined): string | null {
  if (!code) return null;
  if (code.startsWith("url_") || code.startsWith("fetch_")) return t.errors.codes.url_unreachable;
  return errorText(t, code);
}

/** 코드 하나를 화면에 쓸 꼴로 푼 것. */
export interface DescribedCode {
  /** 화면에 보일 한 문장. 표에 있으면 사전의 처방이고 없으면 서버 원문이다. */
  text: string;
  /** 표에서 처방을 찾았는가. 호출부가 원문을 따로 붙일지 정할 때 쓴다. */
  known: boolean;
  /** 서버 오류 코드. 디버깅용이고 문장의 일부가 아니다. */
  code: string | null;
}

/**
 * 코드와 detail 을 화면 언어의 한 문장으로.
 *
 * `describeError`(components/ErrorMessage.tsx)는 `ApiError` 를 받아 status·extra 까지 읽는다. 여기는
 * 코드와 detail 이 이미 갈라져 손에 들려 있을 때의 진입점이다. 서버가 문서에 남기는 `last_error` 와
 * 채팅 스트림의 `error` 이벤트가 그 꼴이고, 둘 다 예외 객체가 아니라 문자열로 온다.
 */
export function describeCode(t: Messages, code: string | null | undefined, detail?: string | null): DescribedCode {
  const known = code ? errorText(t, code) : null;
  if (known) return { text: known, known: true, code: code ?? null };
  // 표에 없으면 감추는 것보다 영어로라도 보이는 편이 낫다
  const raw = [code, detail].filter(Boolean).join(": ");
  return { text: raw || t.errors.fallback, known: false, code: code ?? null };
}

/**
 * `코드: 설명` 한 줄을 푼다. 서버가 `last_error` 를 언제나 이 꼴로 저장한다
 * (`services/instance_base.py` 의 `record_failure`).
 *
 * 앞머리가 코드 꼴(소문자·숫자·밑줄)이 아니면 코드가 없는 것으로 보고 줄 전체를 설명으로 쓴다.
 */
export function describeErrorLine(t: Messages, raw: string): DescribedCode {
  const at = raw.indexOf(": ");
  const head = (at > 0 ? raw.slice(0, at) : raw).trim();
  if (!isCodeShape(head)) return describeCode(t, null, raw);
  return describeCode(t, head, at > 0 ? raw.slice(at + 2) : null);
}

/** id → 화면에 보일 이름. 대화상자가 받아 둔 `references` 로 만든다 (D43). */
export type ReferenceNames = ReadonlyMap<string, string>;

/**
 * 참조 한 줄. 이름을 알면 "MCP Server 주문", 모르면 종류만 말한다.
 *
 * 서버가 409 에 싣는 것은 id 뿐이다. 화면은 사람에게 UUID 를 보이지 않으므로(D43 넷째) 이름을
 * 못 찾으면 id 를 적는 대신 입을 다문다. 몇 개인지는 `refLines` 가 묶어 말한다.
 */
export function refLabel(t: Messages, key: RefKey, id: string, names?: ReferenceNames): string {
  const label = t.errors.refNames[key];
  const name = names?.get(id);
  return name ? t.errors.refNamed(label, name) : label;
}

/** 참조 키 하나의 줄들. 이름을 아는 것은 한 줄씩, 모르는 것은 종류와 개수로 한 줄이다. */
export function refLines(t: Messages, key: RefKey, ids: readonly string[], names?: ReferenceNames): string[] {
  const known = ids.filter((id) => names?.get(id));
  const unknown = ids.length - known.length;
  const lines = known.map((id) => refLabel(t, key, id, names));
  if (unknown) lines.push(t.errors.refCount(t.errors.refNames[key], unknown));
  return lines;
}
