/**
 * 여러 부품이 같이 쓰는 작은 선 아이콘.
 *
 * 크기·선 굵기·선 끝은 P24 시각 패스에서 자리마다 맞춘 값이라 그대로 옮겼다. lucide 의 같은 이름 아이콘은
 * 선 끝과 굵기가 달라 바꿔 끼우지 않는다.
 */
import { cn } from "@/lib/utils";

/** 접고 펴는 머리의 꺾쇠. 닫히면 오른쪽을 본다. */
export function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden
      className={cn("transition-transform", open ? "rotate-0" : "-rotate-90")}
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  );
}

/** 선택한 항목·복사 완료의 체크 표시. `size` 를 주지 않으면 `className` 이 크기를 정한다. */
export function CheckIcon({ className, size }: { className?: string; size?: number }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={className}
    >
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}

/** 더보기 메뉴를 여는 가로 점 셋. */
export function MoreDotsIcon({ size }: { size: number }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} fill="currentColor" aria-hidden>
      <circle cx="3" cy="8" r="1.4" />
      <circle cx="8" cy="8" r="1.4" />
      <circle cx="13" cy="8" r="1.4" />
    </svg>
  );
}
