/**
 * 리소스 이름 규칙 (D24). 서버 daegam/names.py 와 같은 정규식·상한이다.
 *  - name: K8s 이름. 소문자·숫자·단일 하이픈, 32자. 생성 뒤 바꿀 수 없다.
 *  - display_name: 화면에 보이는 이름. 앞뒤 공백을 지운 1~80자, 문자 제한 없음.
 * 화면 문구는 사전의 `ui.names` 에 있다.
 */
import type { Messages } from "@/i18n";

export const K8S_NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
export const NAME_MAX = 32;
export const DISPLAY_NAME_MAX = 80;

/**
 * 표시 이름에서 K8s 이름 후보를 만든다. NFKD 로 풀어 비 ASCII 를 지우고, 소문자로 바꾼 뒤 공백·기호 묶음을
 * 하이픈 하나로 접고 양끝을 정리한다. max(기본 32자)를 넘으면 자르고 끝에 남은 하이픈을 지운다.
 * "계산기 SOAP" 는 "soap" 가 되고, 한글만이면 "" 이다.
 */
export function slugify(display: string, max: number = NAME_MAX): string {
  const ascii = display
    .normalize("NFKD")
    .replace(/[^\x20-\x7e]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return ascii.slice(0, max).replace(/-+$/g, "");
}

/** K8s 이름 검증. 통과하면 null, 아니면 사용자가 할 일을 적은 안내 문장이다. */
export function validateName(t: Messages, name: string): string | null {
  const text = t.ui.names;
  if (!name) return text.nameRequired;
  if (name.length > NAME_MAX) return text.nameTooLong(name.length);
  if (!K8S_NAME_RE.test(name)) return text.nameRule;
  return null;
}

/** 표시 이름 검증. 비어 있어도 된다(서버가 name 으로 채운다). 공백만 있거나 80자를 넘으면 안내한다. */
export function validateDisplayName(t: Messages, display: string): string | null {
  const text = t.ui.names;
  const trimmed = display.trim();
  if (!trimmed) return display ? text.displayNameBlank : null;
  if (trimmed.length > DISPLAY_NAME_MAX) return text.displayNameTooLong(trimmed.length);
  return null;
}

/** 클러스터에 생길 리소스 이름. 규칙 안의 name 에는 서버 k8s_name() 이 항등이라 `<prefix>-<name>` 그대로다. */
export function k8sResourceName(prefix: string, name: string): string {
  return `${prefix}-${name}`;
}

/**
 * 아직 목록을 못 받아 이름을 모를 때. "없다" 가 아니라 "모른다" 는 자리다.
 *
 * 둘을 같은 말로 쓰면 목록을 불러오는 동안 멀쩡한 참조가 지워진 것처럼 보인다. 실제로 Agent 캔버스가
 * 열릴 때마다 노드 제목이 잠깐 "(삭제된 항목)" 으로 떴다.
 */
export const LOADING_LABEL = "…";

/**
 * 참조의 이름을 못 찾았을 때 보일 말. 목록을 다 받았으면(`ready`) 지워진 것(`ui.names.deleted`)이고, 아직 못 받았으면
 * 모르는 것이다. 목록 대신 `ready` 를 따로 들고 다니는 캔버스 카드가 이것을 바로 쓴다.
 */
export function unresolvedLabel(t: Messages, ready: boolean): string {
  return ready ? t.ui.names.deleted : LOADING_LABEL;
}

/** 이름 두 개. display_name 은 null 을 싣는 응답(사용자 등)과 표시 이름 없이 만드는 본문을 위해 선택이다. */
export interface Named {
  name: string;
  display_name?: string | null;
}

/** 화면에 보이는 이름. display_name 이 없으면 name 이다. */
export function displayName(x: Named): string {
  return x.display_name ?? x.name;
}

/**
 * id 로 가리킨 리소스의 이름 (D43 넷째). 목록에 있으면 표시 이름, 목록을 받았는데 없으면 `ui.names.deleted`,
 * 아직 목록을 못 받았으면 `LOADING_LABEL` 이다. 어느 경우에도 UUID 를 보이지 않는다.
 * `ready` 는 목록을 다 받았는가이고 기본은 list 가 있는가다. 목록 여럿을 함께 기다리는 캔버스 인스펙터는 따로 넘긴다.
 */
export function resolveName(t: Messages, list: readonly (Named & { id: string })[] | undefined, id: string, ready: boolean = list !== undefined): string {
  const found = list?.find((x) => x.id === id);
  return found ? displayName(found) : unresolvedLabel(t, ready);
}

/** 표시 이름과 다를 때만 보조로 보일 K8s 이름. 같으면 null 이다. */
export function secondaryName(x: Named): string | null {
  return x.display_name && x.display_name !== x.name ? x.name : null;
}

/** 고르기 목록의 한 줄. 표시 이름을 앞에 두고, 다르면 K8s 이름을 괄호로 붙인다. */
export function optionLabel(x: Named): string {
  const second = secondaryName(x);
  return second ? `${displayName(x)} (${second})` : x.name;
}

/** 목록 검색 대상. 두 이름을 함께 거른다. */
export function nameSearchText(x: Named): string {
  return x.display_name ? `${x.display_name} ${x.name}` : x.name;
}

/**
 * Backend API 의 백엔드 인증 방식. 만들기 폼과 상세의 접속 정보 카드가 같은 목록을 쓴다.
 * 서버의 `services/backend_apis.validate_auth` 가 받는 값과 같아야 한다.
 */
export const AUTH_KINDS = ["none", "basic", "bearer", "api_key"] as const;
export type AuthKindValue = (typeof AUTH_KINDS)[number];
