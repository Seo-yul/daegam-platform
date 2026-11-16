/**
 * 한국어 조사 고르기. 앞말의 끝소리에 받침이 있으면 앞 것(을·이·은·과), 없으면 뒤 것(를·가·는·와)을 붙인다.
 * "을(를)" 처럼 둘 다 찍으면 화면이 기계 번역처럼 읽힌다.
 *
 * 한글은 끝 음절의 종성으로 정한다. 숫자와 영문은 읽는 소리로 정한다. 숫자는 영·일·삼·육·칠·팔, 영문 끝 글자는
 * 엘·엠·엔·알(L·M·N·R)만 받침이 있다. 다만 끝의 영문 낱말이 두 글자 이상이고 소문자 er·or 로 끝나면 단어로 읽어
 * 모음으로 끝난다(Server 는 서버, Cluster 는 클러스터, Editor 는 에디터). 그 밖의 id·약어(ECR, HTML)는 글자 이름으로 읽는다.
 */
const DIGIT_WITH_FINAL = new Set(["0", "1", "3", "6", "7", "8"]);
const LATIN_WITH_FINAL = new Set(["l", "m", "n", "r"]);

// 받침이 ㄹ 인 끝소리. "으로/로" 는 ㄹ 받침 뒤에서 받침이 없는 것처럼 "로" 다 (일·칠·팔, 엘·알)
const DIGIT_WITH_RIEUL = new Set(["1", "7", "8"]);
const LATIN_WITH_RIEUL = new Set(["l", "r"]);
const HANGUL_RIEUL = 8;

/** 끝의 공백·따옴표·괄호를 뗀 앞말. */
function trimmed(word: string): string {
  return word.replace(/[\s"'`)\]}>.,!?]+$/u, "");
}

/** 판단할 끝 글자. */
function lastLetter(word: string): string | undefined {
  return trimmed(word).at(-1);
}

/** 끝의 영문 낱말이 소문자 er·or 로 끝나 단어로 읽으면 모음으로 끝나는가(서버, 클러스터, 에디터). */
function readsAsVowelWord(word: string): boolean {
  const last = /[A-Za-z]+$/u.exec(trimmed(word))?.[0] ?? "";
  return last.length >= 2 && /(?:er|or)$/u.test(last);
}

/** 끝소리에 받침이 있는가. 판단할 글자가 없으면 false 다. */
export function hasFinalConsonant(word: string): boolean {
  const last = lastLetter(word);
  if (!last) return false;
  const code = last.charCodeAt(0);
  if (code >= 0xac00 && code <= 0xd7a3) return (code - 0xac00) % 28 !== 0;
  if (/[0-9]/.test(last)) return DIGIT_WITH_FINAL.has(last);
  return !readsAsVowelWord(word) && LATIN_WITH_FINAL.has(last.toLowerCase());
}

/** 끝소리의 받침이 ㄹ 인가. */
function hasRieulFinal(word: string): boolean {
  const last = lastLetter(word);
  if (!last) return false;
  const code = last.charCodeAt(0);
  if (code >= 0xac00 && code <= 0xd7a3) return (code - 0xac00) % 28 === HANGUL_RIEUL;
  if (/[0-9]/.test(last)) return DIGIT_WITH_RIEUL.has(last);
  return !readsAsVowelWord(word) && LATIN_WITH_RIEUL.has(last.toLowerCase());
}

const PAIRS = { "을/를": ["을", "를"], "이/가": ["이", "가"], "은/는": ["은", "는"], "과/와": ["과", "와"], "으로/로": ["으로", "로"] } as const;

export type ParticlePair = keyof typeof PAIRS;

/** 앞말에 맞는 조사 하나를 돌려준다. 앞말은 붙이지 않는다(앞말만 따로 꾸미는 JSX 에서 쓰기 쉽게). */
export function particle(word: string, pair: ParticlePair): string {
  const [withFinal, without] = PAIRS[pair];
  if (pair === "으로/로" && hasRieulFinal(word)) return without;
  return hasFinalConsonant(word) ? withFinal : without;
}
