/**
 * 화면 문구 사전이 문장에 넣는 상한과 받는 형식 (P49 2절). 사전 모듈은 ui/src/i18n 을 들이지 않는 모듈만 들일 수 있으므로 이
 * 파일은 아무것도 들이지 않는다. 원래 자리(lib/feedback.ts, lib/sessionTitle.ts, lib/knowledge.ts, lib/skills.ts)가 같은 이름으로
 * 다시 내보내 쓰는 쪽은 그대로다. 서버 값과 같은지는 platform/tests/test_chat_parity.py, test_knowledge_ui_parity.py,
 * test_skill_ui_parity.py, test_evaluation_ui_parity.py 가 이 파일을 읽어 맞댄다.
 */

/** 피드백 내보내기 한 번의 최대 구간이자 구간을 비웠을 때 서버가 잡는 날 수다. 서버 `services/agent_feedback.py` 의 `EXPORT_MAX_DAYS` 와 같다. */
export const FEEDBACK_EXPORT_MAX_DAYS = 90;

/**
 * 평가 세트 하나의 사례 수 × 반복 수 상한 (P45 7절). 서버 `store/evaluation.py` 의 `MAX_CASE_RUNS` 와 같다. 오류 사전의
 * `evaluation_set_full` 문장과 사례 편집기가 읽는다.
 */
export const EVALUATION_MAX_CASE_RUNS = 100;

/**
 * 평가 실행이 미리 보기 자리를 기다리는 시간(분)이다. 지나면 실행이 `preview_limit` 으로 끝난다(P45 9절). 서버
 * `services/evaluation/runner.py` 의 `QUEUED_FOR` 와 같고 실행의 오류 문장이 읽는다.
 */
export const EVALUATION_QUEUE_MINUTES = 30;

/** 이름 바꾸기가 받는 제목 길이의 상한이다(정리한 뒤). 제목 검색어도 같은 상한이다. 런타임 turn_record.py 의 같은 이름 상수와 같은 값이다. */
export const SESSION_TITLE_MAX_CHARS = 100;

/**
 * 문서 형식. 서버가 확장자와 content type 으로 판별한다(아니면 422 unsupported_format). 서버 `daegam/documents/formats.py` 의
 * 문서 형식이 받는 확장자와 같다. 글은 인덱서가 문서 파싱 서비스로 뽑는다(D61).
 */
export const DOCUMENT_ACCEPT = ".txt,.md,.markdown,.csv,.tsv,.json,.html,.htm,.pdf,.docx,.pptx,.xlsx,.hwpx";

/** 같은 형식의 다른 확장자다. 형식 이름에서는 뺀다. */
const EXTENSION_ALIASES = new Set([".markdown", ".htm", ".jpeg"]);

/** accept 꼴 확장자 목록을 화면에 보일 형식 이름으로 바꾼다. 별칭을 빼고 점을 떼어 "pdf · docx" 처럼 잇는다. */
export function formatLabel(accept: string): string {
  return accept
    .split(",")
    .filter((ext) => !EXTENSION_ALIASES.has(ext))
    .map((ext) => ext.slice(1))
    .join(" · ");
}

/** 문서 카드와 형식 오류 문구에 보이는 형식 이름. `DOCUMENT_ACCEPT` 에서 만든다. */
export const DOCUMENT_FORMAT_LABEL = formatLabel(DOCUMENT_ACCEPT);

/** Skill 서버 형식 검사(`services/skill_format.py`)와 API(`api/skills.py`)의 상한. 검사 문장과 입력 칸이 이 값을 쓴다. */
export const SKILL_LIMITS = {
  files: 32,
  skillMdKib: 64,
  fileKib: 256,
  revisionKib: 512,
  importMib: 2,
  zipEntries: 64,
  description: 1024,
  compatibility: 500,
  license: 200,
  metadataKeys: 20,
  allowedTools: 50,
  note: 500,
} as const;
