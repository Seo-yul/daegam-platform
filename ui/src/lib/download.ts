/**
 * 파일 내려받기 (D61). 세션 파일, 인용 원문, 피드백 내보내기는 모두 `Content-Disposition: attachment` 로 오고 인증이
 * 헤더(Bearer)라 링크를 그대로 걸 수 없다. 그래서 `rawFetch` 로 받아 object URL 로 내려받는다.
 *
 * 파일 이름은 응답 헤더의 `filename*`(퍼센트 인코딩한 UTF-8)를 먼저 읽고 없거나 깨졌으면 ASCII 대체 이름 `filename` 이다.
 * 헤더 모양은 플랫폼 `api/_download.py` 가 만들고 표본 `agent-runtime/schema/fixtures/chat/download-headers.json` 이 정한다.
 *
 * 피드백 탭의 내보내기와 인용 원문 다운로드(F6)가 쓴다.
 */
import { rawFetch, throwIfNotOk } from "@/api/client";

/** 내려받기를 누른 뒤 object URL 을 돌려주기까지 기다리는 시간(ms). 곧바로 돌려주면 내려받기가 시작되기 전에 끊기는 브라우저가 있다. */
export const REVOKE_AFTER_MS = 10_000;

const ENCODED_NAME = /filename\*\s*=\s*UTF-8''([^;\s]+)/i;
const PLAIN_NAME = /filename\s*=\s*(?:"([^"]*)"|([^;\s]+))/i;

/** 응답 헤더 `Content-Disposition` 의 파일 이름이다. 헤더가 없거나 이름이 없으면 null 이다. */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = ENCODED_NAME.exec(header)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      // 퍼센트 인코딩이 깨졌으면 ASCII 대체 이름으로 물러선다
    }
  }
  const plain = PLAIN_NAME.exec(header);
  return plain?.[1] || plain?.[2] || null;
}

/** Blob 을 파일로 저장한다. 보이지 않는 링크를 눌러 내려받고 object URL 은 잠시 뒤 돌려준다. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);
}

/**
 * 헤더 인증이 필요한 파일을 받아 저장한다. path 는 `/api` 를 뺀 플랫폼 경로다. 실패하면 플랫폼 오류 본문을 실은
 * `ApiError` 를 던진다. 응답 헤더에 이름이 없으면 fallbackName 으로 저장한다.
 */
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const res = await rawFetch(path);
  await throwIfNotOk(res);
  const name = filenameFromDisposition(res.headers.get("Content-Disposition")) ?? fallbackName;
  saveBlob(await res.blob(), name);
}
