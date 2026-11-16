/**
 * 마크다운 답의 링크 규칙 (D61). 모델이 쓴 주소는 믿을 수 없으므로 react-markdown 의 `urlTransform` 한 곳에서 거르고,
 * 링크와 이미지를 그리는 부품은 거른 값을 같은 규칙으로 다시 갈라 무엇을 그릴지 정한다. 거른 값을 다시 갈라도 결과가
 * 같으므로 규칙은 이 파일에만 있다.
 *
 * - `https:`·`http:`·`mailto:` 절대 주소는 새 탭 링크이고 호스트(메일은 주소)를 `title` 에 보인다.
 * - 앱 안 경로는 조회 문자열과 조각을 떼고 react-router 링크로 낸다. `/` 로 시작하되 `//` 가 아닌 것과 같은 origin 의 절대
 *   주소가 여기에 든다. 플랫폼 API 와 인증·동의·계정 화면은 글자로 낮춘다. 주입된 답이 동의 화면이나 비밀번호 화면으로
 *   이끄는 낚시를 막으려는 것이다.
 * - 그 밖(`javascript:`·`data:`·`file:`·상대 주소·조각만 있는 주소)은 링크를 빼고 글자만 남긴다.
 */

/** 새 탭 링크로 살리는 프로토콜. */
const EXTERNAL_PROTOCOLS: ReadonlySet<string> = new Set(["https:", "http:", "mailto:"]);

/** 앱 화면이 아닌 플랫폼 API 경로. */
const API_PATH = "/api";

/** 앱 안 경로지만 링크로 두지 않는 접두어. react-router 는 대소문자를 가리지 않고 맞추므로 소문자로 견준다. */
const TEXT_ONLY_APP_PREFIXES: readonly string[] = ["/auth/", "/oauth/", "/account/", "/login", "/callback"];

/** 프로토콜로 시작하는 절대 주소. 프로토콜이 없으면 상대 주소다. */
const SCHEME = /^[a-z][a-z\d+.-]*:/i;

export type LinkTarget =
  | { kind: "external"; href: string; title: string }
  | { kind: "app"; to: string }
  | { kind: "text" };

const TEXT_ONLY: LinkTarget = { kind: "text" };

/** 주소 하나를 어떤 링크로 그릴지 가른다. `origin` 은 이 앱의 origin 이고 테스트만 바꿔 넣는다. */
export function classifyLink(raw: string, origin: string = window.location.origin): LinkTarget {
  const value = raw.trim();
  const rootPath = value.startsWith("/") && !value.startsWith("//");
  if (!rootPath && !SCHEME.test(value)) return TEXT_ONLY;
  let url: URL;
  try {
    url = new URL(value, origin);
  } catch {
    return TEXT_ONLY;
  }
  // `/\host` 처럼 브라우저가 다른 호스트로 읽는 경로는 origin 이 달라져 여기서 걸린다
  if (url.origin === origin) return appLink(url.pathname);
  if (rootPath || !EXTERNAL_PROTOCOLS.has(url.protocol)) return TEXT_ONLY;
  return { kind: "external", href: url.href, title: url.protocol === "mailto:" ? url.pathname : url.host };
}

/** react-markdown 의 `urlTransform`. 링크로 살릴 값만 돌려주고 나머지는 빈 글이라 부품이 글자로 그린다. */
export function markdownUrlTransform(url: string): string {
  const link = classifyLink(url);
  return link.kind === "external" ? link.href : link.kind === "app" ? link.to : "";
}

/** 같은 origin 의 경로. 조회 문자열과 조각은 이미 떼었다. react-router 가 맞출 때 퍼센트 부호를 풀므로 풀어서 견준다. */
function appLink(pathname: string): LinkTarget {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname).toLowerCase();
  } catch {
    return TEXT_ONLY;
  }
  if (decoded === API_PATH || decoded.startsWith(`${API_PATH}/`)) return TEXT_ONLY;
  if (TEXT_ONLY_APP_PREFIXES.some((prefix) => decoded.startsWith(prefix))) return TEXT_ONLY;
  return { kind: "app", to: pathname };
}
