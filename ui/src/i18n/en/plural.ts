/**
 * 영어의 수와 명사. 1 이면 단수, 그 밖은 복수다: `plural(3, "error", "errors")` 는 "3 errors". 한국어 쪽에는 없다.
 * 한국어는 수 뒤에 단위(개, 건)를 붙이고 명사 모양이 바뀌지 않는다.
 */
export function plural(n: number, one: string, other: string): string {
  return `${n} ${n === 1 ? one : other}`;
}
