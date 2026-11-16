/**
 * 내 장기 기억의 화면 규칙 (P36 8절). 기억 서랍(components/chat/MemoryDrawer.tsx)과 외부 호출 카드, 기억 API 감싸개가 같은 뿌리와
 * 상한을 쓴다. 뿌리는 런타임 `long_term.py` 의 `MEMORY_ROUTE`, 상한은 `memories_api.py` 의 `MEMORY_FILE_MAX_BYTES` 와 같고
 * platform/tests/test_chat_parity.py 가 맞댄다(D34).
 */
import { stripRoot } from "@/lib/utils";

/** 장기 기억 파일이 있는 경로의 뿌리다. */
export const MEMORY_ROOT = "/memories/";
/** 내용을 읽어 줄 수 있는 기억 파일 크기의 상한(UTF-8 바이트)이다. */
export const MEMORY_FILE_MAX_BYTES = 1_048_576;

/** 에이전트가 보는 경로(`/memories/notes/a.md`)에서 뿌리 밑의 경로(`notes/a.md`)다. 라우트의 키이고 서랍이 파일 이름으로 보인다. */
export function memoryKey(path: string): string {
  return stripRoot(path, MEMORY_ROOT);
}
