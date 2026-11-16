/**
 * 세션 파일의 화면 규칙 (P36 10절). 세션 파일 서랍(components/chat/SessionFilesDrawer.tsx), 도구 카드의 다운로드, 외부 호출 카드가
 * 같은 상한과 경로 규칙을 쓴다. 다운로드 상한은 런타임 `session_files.py` 의 `SESSION_FILE_MAX_BYTES` 와 같고
 * platform/tests/test_chat_parity.py 가 맞댄다. 업로드한 파일의 뿌리는 런타임 `uploads.py` 의 `UPLOADS_ROUTE` 다.
 *
 * 코드 실행이 꺼진 에이전트가 만드는 파일은 `write_file`·`edit_file` 로 쓴 텍스트뿐이다. 코드 실행 에이전트의 파일은 작업 공간
 * (`/workspace/`, 런타임 `sandbox.py` 의 WORKSPACE)에 있고 바이너리도 된다(P40 6절). 장기 기억(`/memories/`)은 세션이 아니라
 * 기억 저장소에 있어 기억 서랍의 몫이다.
 */
import { MEMORY_ROOT } from "@/lib/memories";
import { stripRoot } from "@/lib/utils";

/** 사용자가 업로드한 파일이 에이전트에게 보이는 경로의 뿌리다. */
export const UPLOADS_ROOT = "/uploads/";
/** 코드 실행 에이전트의 작업 공간 파일이 있는 경로의 뿌리다(P40 6절). */
export const WORKSPACE_ROOT = "/workspace/";
/** 다운로드할 수 있는 세션 파일 크기의 상한(바이트)이다. 넘으면 413 file_too_large 다. */
export const SESSION_FILE_MAX_BYTES = 10_485_760;
/** 서랍에서 미리 볼 수 있는 파일 크기의 상한(바이트)이다. 넘는 파일은 다운로드만 한다. */
export const SESSION_FILE_PREVIEW_MAX_BYTES = 262_144;

/** 성공하면 세션 파일을 남기는 파일 도구다. 도구 카드에 다운로드 단추가 선다. */
const WRITING_TOOLS = new Set(["write_file", "edit_file"]);

/** 파일 경로(`/reports/a.md`)에서 라우트의 키(`reports/a.md`)다. 앞 `/` 를 뗀다. */
export function sessionFileKey(path: string): string {
  return path.replace(/^\/+/, "");
}

/** 서랍이 보이는 이름이다. 업로드한 파일과 작업 공간 파일은 뿌리를 뗀 이름이고 에이전트 파일은 경로 그대로다. */
export function sessionFileLabel(path: string): string {
  return stripRoot(stripRoot(path, UPLOADS_ROOT), WORKSPACE_ROOT);
}

/** 경로의 마지막 조각이다. 응답 헤더에 이름이 없을 때 저장 이름이 된다. */
export function sessionFileName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1) || path;
}

/**
 * 성공한 파일 도구 호출이 세션에 남긴 파일의 경로다. 쓰기 도구가 아니거나, 성공하지 않았거나, 기억이나 업로드 경로면 null 이다.
 * 기억은 세션 파일이 아니고 업로드한 파일은 에이전트가 쓸 수 없다.
 */
export function writtenFile(name: string, args: Record<string, unknown>, status: string | undefined): string | null {
  const path = args.file_path;
  if (!WRITING_TOOLS.has(name) || status !== "success" || typeof path !== "string") return null;
  return path.startsWith(MEMORY_ROOT) || path.startsWith(UPLOADS_ROOT) ? null : path;
}
