import type { ReactNode } from "react";

import type { HubAction, SkillIssue } from "@/api/types";
import { SKILL_LIMITS } from "@/lib/limits";
import { NAME_MAX } from "@/lib/names";
import { particle } from "@/lib/particle";

/** Skill 목록·상세·만들기와 검사 결과, 새 SKILL.md 틀 (D64). */
export const skills = {
  /** 목록의 주 동작이자 만들기 화면의 제목. */
  create: "Skill 만들기",
  /** 없는 Skill 을 열었을 때 목록으로 가는 링크. 상세와 새 리비전 화면이 같이 쓴다. */
  back: "Skill 목록으로 돌아가기",
  /** scope 가 shared 인 Skill 의 배지. 목록과 상세가 같이 쓴다. */
  shared: "공유",
  /** 리비전을 zip 으로 받는 단추(shared.export)의 접근 이름. 상세 제목 줄과 리비전 탭이 같이 쓴다. */
  exportRevision: (revision: number): string => `r${revision} 내보내기`,
  /** 핀보다 새 리비전이 있을 때의 배지. 참조 카드와 캔버스가 같은 말을 쓴다. */
  newerRevision: (latest: number): string => `새 리비전 r${latest}`,
  /** 접근 정책. view 의 뜻은 허브 항목과 같아 hub.access.actions.view 를 쓴다(lib/skills.ts skillActionLabels). */
  access: {
    /** 정책 문장이 Skill 을 가진 테넌트를 부르는 말. 허브 항목은 hub.access.owner 다. */
    owner: "소유 테넌트",
    actions: {
      consume: { title: "consume", hint: "Agent 에 붙이고 복제할 수 있습니다" },
      manage: { title: "manage", hint: "새 리비전·정책 변경·삭제" },
    } satisfies Record<Exclude<HubAction, "view">, { title: string; hint: string }>,
  },
  /**
   * "직접 작성" 이 여는 SKILL.md 틀의 글. 사람이 지니는 파일이라 적는 순간의 화면 언어를 따른다. 머리말 키(description:)와
   * 마크다운 표시(#, 1.)는 lib/skills.ts 의 skillTemplate 이 붙인다.
   */
  template: {
    description: "언제 이 Skill 을 쓰는지 한두 문장으로 적습니다.",
    heading: "절차",
    step: "에이전트가 따를 단계를 차례로 적습니다.",
  },
  /**
   * 검사 코드 → 화면 문장. zip 오류(`archive_*`)도 가져오기의 issue 로 오므로 여기 둔다. 서버(`skill_format.py`·`zipsafe.py`)가
   * 내는 code 를 다 덮는지는 `src/test/error-text.test.ts` 가 본다. skill_scripts_inert 는 캔버스의 그래프 이슈도 같은 code 로
   * 쓰므로 파일 하나에 붙어도, 붙인 Skill 에 붙어도 읽히게 적는다.
   */
  issues: {
    too_many_files: `파일이 너무 많습니다. Skill 하나에는 SKILL.md 를 포함해 ${SKILL_LIMITS.files}개까지 둘 수 있습니다.`,
    file_too_large: `파일이 너무 큽니다. SKILL.md 는 ${SKILL_LIMITS.skillMdKib} KiB, 다른 파일은 ${SKILL_LIMITS.fileKib} KiB 까지이니 내용을 줄이거나 여러 파일로 나눠 주세요.`,
    revision_too_large: `파일을 모두 합한 크기가 ${SKILL_LIMITS.revisionKib} KiB 를 넘습니다. 파일을 줄이거나 나눠 주세요.`,
    path_invalid: "경로가 규칙에 맞지 않습니다. references/tone.md 처럼 영문·숫자로 시작하는 상대 경로를 써 주세요.",
    path_duplicate: "같은 경로가 이미 있습니다. 대소문자만 다른 경로도 같은 경로로 보므로 다른 경로로 바꿔 주세요.",
    skill_md_missing: "최상위 폴더에 SKILL.md 가 없습니다. SKILL.md 를 추가해 주세요.",
    not_text: "이 파일은 UTF-8 텍스트가 아닙니다. UTF-8 로 저장한 텍스트 파일로 바꿔 주세요.",
    control_chars: "제어 문자가 들어 있습니다. 탭과 줄바꿈 외의 제어 문자는 삭제해 주세요.",
    invisible_chars: "화면에 보이지 않는 글자가 들어 있습니다. 아래 위치의 글자를 지워 주세요.",
    frontmatter_missing: "SKILL.md 가 머리말로 시작하지 않습니다. 맨 앞에 --- 로 감싼 name 과 description 을 적어 주세요.",
    frontmatter_invalid: "머리말을 읽을 수 없습니다. 키: 값 꼴의 YAML 로 적어 주세요.",
    name_invalid: `머리말의 name 이 이름 규칙에 맞지 않습니다. 소문자 영문, 숫자, 하이픈으로 ${NAME_MAX}자 이내로 적고, 2024 처럼 숫자로만 된 이름은 따옴표로 감싸 주세요.`,
    name_mismatch: "머리말의 name 은 바꿀 수 없습니다. 이 Skill 의 이름을 그대로 적어 주세요.",
    description_missing: "머리말에 description 이 없습니다. 언제 이 Skill 을 쓰는지 한두 문장으로 적어 주세요.",
    description_too_long: `description 은 ${SKILL_LIMITS.description}자까지입니다. 짧게 줄여 주세요.`,
    compatibility_too_long: `compatibility 는 ${SKILL_LIMITS.compatibility}자까지입니다. 짧게 줄여 주세요.`,
    license_too_long: `license 는 ${SKILL_LIMITS.license}자까지입니다. 짧게 줄여 주세요.`,
    metadata_invalid: `metadata 는 문자열 키와 문자열 값의 매핑이어야 합니다. 숫자는 따옴표로 감싸고 키는 ${SKILL_LIMITS.metadataKeys}개까지 적어 주세요.`,
    allowed_tools_invalid: `allowed-tools 는 공백이나 쉼표로 구분한 Tool 이름 목록이어야 합니다. ${SKILL_LIMITS.allowedTools}개까지 적어 주세요.`,
    unknown_frontmatter: "지원하지 않는 머리말 키가 있습니다. 저장은 되지만 에이전트는 이 키를 사용하지 않습니다.",
    skill_scripts_inert: "스크립트는 실행되지 않습니다. 에이전트는 스크립트 파일을 텍스트로만 읽습니다.",
    archive_invalid: "zip 파일을 열 수 없습니다. 손상되지 않은 zip 인지 확인해 주세요.",
    archive_too_large: `zip 이 너무 큽니다. 항목은 ${SKILL_LIMITS.zipEntries}개, 압축을 푼 크기는 ${SKILL_LIMITS.revisionKib} KiB 이내로 줄여 주세요.`,
    archive_encrypted: "암호가 걸린 zip 은 가져올 수 없습니다. 암호를 푼 zip 으로 다시 가져와 주세요.",
    archive_symlink: "zip 에 심볼릭 링크가 들어 있습니다. 링크를 빼고 다시 압축해 주세요.",
    archive_path_invalid: "zip 안의 경로가 올바르지 않습니다. 절대 경로, 상위 폴더(..), 역슬래시가 없는 zip 으로 다시 압축해 주세요.",
  } satisfies Record<string, string>,
  /** 검사 결과 한 줄(SkillIssueList)의 배지. */
  severity: { error: "오류", warning: "경고" } satisfies Record<SkillIssue["severity"], string>,
  /** /skills. 검색어와 맞는 것이 없을 때는 dataServices.list.noMatch 다. */
  list: {
    empty: (tenant: string): string => `테넌트 ${tenant}에 Skill 이 없습니다. 에이전트가 따를 절차를 SKILL.md 로 적어 만들거나 zip 으로 가져옵니다.`,
    /** 다른 테넌트가 연 Skill 카드의 꼬리. tenant 는 꾸민 테넌트 이름이다. */
    owner: (tenant: ReactNode): ReactNode => <>소유 테넌트 {tenant}</>,
    columns: { name: "이름", description: "설명", revision: "리비전", owner: "소유 테넌트" },
    title: "Skills",
    search: "Search skills…",
  },
  /** /skills/:id. 개요 탭의 이름은 ui.tabs.standard.overview, 접근 정책 탭은 hub.detail.tabs.access 다. */
  detail: {
    tabs: { files: "파일", revisions: "리비전" },
    /** 다른 테넌트가 연 Skill 의 배지. */
    ownerBadge: (tenant: string): string => `${tenant} 테넌트`,
    newRevision: "새 리비전",
    /** 가져오기가 건너뛴 zip 항목. items 는 ", " 로 이은 경로다. */
    skipped: (items: string): string => `다음 항목은 가져오지 않았습니다. ${items}`,
    referencesHint: "이 Skill 을 붙인 Agent 와 고정한 리비전입니다",
    cloneBody: (revision: number): string =>
      `최신 리비전 r${revision}${particle(`r${revision}`, "을/를")} 복사하여 이 테넌트에 새 Skill 을 만듭니다. 리비전 이력과 접근 정책은 따라오지 않고 새 Skill 은 r1 에서 시작합니다.`,
    /** 화면 언어를 따르는 복제 이름 예시. */
    clonePlaceholders: { display: "보고서 문체 복제본", name: "report-style-copy" },
  },
  /** 개요의 요약 카드(SkillSummaryCard). 이름표는 머리말 키라 영어 그대로다. */
  summary: {
    /** allowed-tools 칩 옆의 말. 규격이 실험 기능이라 하고 우리는 집행하지 않는다. */
    allowedToolsNote: "실험 기능이며 Tool 사용 권한을 주지 않습니다",
    /** bytes 는 화면에 적은 크기다. */
    size: (bytes: string, files: number): string => `${bytes} · 파일 ${files}개`,
    tenant: "tenant",
    scope: "scope",
    license: "license",
    compatibility: "compatibility",
    allowedTools: "allowed-tools",
    /** Summary 카드의 칸 이름. */
    labels: {
      size: "size",
    },
  },
  /** 상세의 파일 탭(SkillFileBrowser). 목록의 접근 이름은 편집기도 쓴다. */
  files: {
    count: (revision: number, files: number): string => `r${revision} · 파일 ${files}개`,
    listLabel: "Skill 파일",
    /** 스크립트 파일 줄의 점. 낭독기가 읽는 이름이다. */
    script: "스크립트",
    /** 스크립트 파일의 배지. 풀이는 issues.skill_scripts_inert 다. */
    scriptBadge: "실행되지 않음",
    title: "Files",
  },
  /** 직접 작성의 파일 편집기(SkillFilesEditor). 파일 삭제 단추의 이름은 resources.delete.rowLabel 이다. */
  editor: {
    addPath: "추가할 파일 경로",
    add: "파일 추가",
    duplicate: "같은 경로의 파일이 이미 있습니다.",
  },
  /** 가져오기의 파일 자리(SkillImportField). */
  importField: {
    hint: "zip 이나 SKILL.md 를 여기에 끌어 놓거나 선택합니다. zip 안의 항목이 모두 한 폴더에 들어 있으면 그 폴더 안의 파일을 가져옵니다.",
    file: "가져올 파일",
    wrongType: (name: string): string => `"${name}"${particle(name, "은/는")} 가져올 수 없는 형식입니다. zip 이나 SKILL.md 를 선택해 주세요.`,
    /** 크기 한도를 넘은 파일 안내(lib/files.ts fileTooLargeText)의 조각. size 는 화면에 적은 상한이다. */
    sizeLimit: (size: string): string => `가져올 수 있는 파일은 ${size}`,
  },
  /** 상세의 리비전 탭(SkillRevisionsCard). 바뀐 파일의 배지는 detail.specDiff.kind 다. */
  revisions: {
    latest: (revision: number): string => `최신 r${revision}`,
    columns: { revision: "리비전", note: "메모", author: "만든 사람", time: "시각" },
    /** 복제로 만든 r1 의 메모 칸. */
    clonedFrom: (tenant: string, name: string, revision: number): string => `${tenant}/${name} r${revision} 에서 복제`,
    compare: "리비전 비교",
    changedFiles: "바뀐 파일",
    same: "두 리비전의 파일이 같습니다.",
    title: "Revisions",
    digest: "digest",
  },
  /** /skills/new 와 /skills/:id/edit. 취소는 common.cancel, 저장 중은 shared.saving 이다. */
  form: {
    /** 두 탭의 이름. import 는 가져오기 단추도 쓴다. */
    write: "직접 작성",
    import: "가져오기",
    newRevisionTitle: (name: string): string => `${name} 새 리비전`,
    createSubtitle: "SKILL.md 와 함께 넣을 텍스트 파일로 에이전트가 따를 절차를 적습니다.",
    revisionSubtitle: (revision: number): string => {
      const [from, to] = [`r${revision}`, `r${revision + 1}`];
      return `${from}${particle(from, "을/를")} 고쳐 ${to}${particle(to, "을/를")} 만듭니다. 이전 리비전은 그대로 남습니다.`;
    },
    /** 화면 언어를 따르는 이름 예시. */
    placeholders: { display: "보고서 문체", name: "report-style" },
    nameHint: "SKILL.md 머리말의 name 과 같은 값입니다.",
    note: "리비전 메모",
    noteHint: "리비전 탭의 목록에 보입니다. 비워도 됩니다.",
    saveRevision: "새 리비전 저장",
    importDisplayHint: "비우면 SKILL.md 머리말의 name 을 그대로 사용합니다.",
    importing: "가져오는 중…",
    /** 검사 결과 한 줄. 문제의 자리는 파일 옆이 말하고 여기는 수만 센다. */
    validation: {
      idle: "고치기 시작하면 서버가 형식을 검사합니다.",
      checking: "검사하는 중입니다…",
      failed: "검사 요청이 실패했습니다. 저장할 때 서버가 다시 검사합니다.",
      errors: (errors: number, warnings: number): string => `오류 ${errors}개, 경고 ${warnings}개가 있습니다. 오류를 고쳐야 저장됩니다.`,
      warnings: (warnings: number): string => `경고 ${warnings}개가 있지만 저장할 수 있습니다.`,
      clean: "형식에 문제가 없습니다.",
    },
    displayName: "Display name",
  },
};
