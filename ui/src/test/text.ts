/** 화면 문장 표(오류 표·경고 표)를 검사하는 테스트 도우미다. */
import { ApiError } from "@/api/client";

/**
 * 서버가 낸 오류를 그 모양 그대로 ApiError 로 만든다. HTTPException(detail={code, detail, ...extra}) 다. 같은 모양의 HTTP 응답은
 * msw 의 `apiError` 가 만든다.
 */
export function serverError(status: number, code: string, detail: string, extra: Record<string, unknown> = {}): ApiError {
  return new ApiError(status, { detail: { code, detail, ...extra } });
}

/** 합니다체 종결. 화면 문구는 "~습니다.", "~입니다.", "~주세요.", "~하십시오." 로만 끝난다. */
export const POLITE_ENDING = /(니다|세요|십시오)\.$/;

/** 문장으로 나눈다. 나누는 자리는 "마침표 + 공백" 이라 "3.x", "example.com" 처럼 마침표 뒤가 공백이 아닌 것은 안 나뉜다. */
export function sentences(text: string): string[] {
  return text.split(/(?<=\.)\s+/);
}
