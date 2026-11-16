/**
 * CSV 만들기 (P37 13절). 사용량 화면의 내려받기가 쓴다.
 *
 * 에이전트·모델 이름은 사람이 적은 글이라 스프레드시트가 수식으로 돌리면 안 된다. `=`·`+`·`-`·`@`·탭·CR 로 시작하는 글 칸은
 * 앞에 `'` 를 붙인다. 숫자 칸은 그대로 둔다.
 */
const FORMULA_START = /^[=+\-@\t\r]/;

export type CsvCell = string | number | boolean | null | undefined;

/** 칸 하나. 수식처럼 시작하는 글은 `'` 를 붙이고, 쉼표·따옴표·줄바꿈이 있으면 따옴표로 감싼다. */
export function csvCell(value: CsvCell): string {
  if (value == null) return "";
  if (typeof value !== "string") return String(value);
  const safe = FORMULA_START.test(value) ? `'${value}` : value;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** 머리 줄과 행들. 줄바꿈은 CRLF 다(RFC 4180). */
export function toCsv(header: string[], rows: CsvCell[][]): string {
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}
