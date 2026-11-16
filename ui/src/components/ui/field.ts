/**
 * Input·Textarea·Select 트리거가 공유하는 필드 룩. select.tsx 와 input.tsx 가 함께 쓰므로 따로 둔다.
 * 경계는 카드 테두리(`--ui-line`)가 아니라 `--ui-field-line` 이다. 컨트롤을 알아보는 데 필요한 경계라
 * 3:1 이 들고, 카드까지 같이 짙게 하면 평평한 면이 상자가 되기 때문이다 (index.css 의 주석).
 */
/** 파일 칸의 "파일 선택" 단추. 프리플라이트가 단추 모양을 지워 평문처럼 보이므로 보조 버튼 모양을 준다. */
export const fileButtonClass =
  "file:mr-3 file:cursor-pointer file:rounded-item file:border file:border-control-line file:bg-surface file:px-2.5 file:py-0.5 file:text-xs file:font-semibold file:text-heading";

export const fieldClass = `flex w-full rounded-chip border border-field-line bg-surface px-3 py-1.5 text-base text-heading placeholder:text-label focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/35 focus-visible:border-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${fileButtonClass}`;
