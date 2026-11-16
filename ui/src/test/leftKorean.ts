/**
 * 영어로 그린 화면에 남은 한글 (P49 9절). 글자와 함께 낭독기와 툴팁이 읽는 속성도 본다. 픽스처가 한국어로 준 값(서버가 적은
 * 배지, 표시 이름)과 아직 다른 단위가 옮기지 않은 문구는 `allow` 로 지운 뒤 본다.
 */

/** 글자 말고도 낭독기와 툴팁이 읽는 속성. */
const SPOKEN_ATTRIBUTES = ["aria-label", "title", "placeholder"] as const;

/**
 * 요소의 글자와 접근 이름·제목·자리표시 글에서 `allow` 를 앞에서부터 지우고 남은 한글 낱말. 글자와 속성 값은 한 줄에
 * 하나씩 놓이므로, 속성 값 하나와 꼭 같은 것만 지우려면 `/^모델$/gm` 처럼 줄의 처음과 끝을 건 정규식을 준다. 정규식에는
 * 모두 지우도록 g 플래그를 단다(없으면 replaceAll 이 던진다).
 */
export function leftKorean(element: Element, allow: readonly (string | RegExp)[] = []): string[] {
  const spoken = [element, ...element.querySelectorAll("*")].flatMap((node) => SPOKEN_ATTRIBUTES.map((name) => node.getAttribute(name) ?? ""));
  const text = [element.textContent ?? "", ...spoken].join("\n");
  return allow.reduce<string>((rest, korean) => rest.replaceAll(korean, ""), text).match(/[가-힣]+/g) ?? [];
}
