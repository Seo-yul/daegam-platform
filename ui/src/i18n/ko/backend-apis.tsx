import type { ReactNode } from "react";

import type { MinifyEnrich, RefinePreset } from "@/api/types";
import { particle } from "@/lib/particle";
import type { CheckGroup, CheckId, CheckSeverity } from "@/lib/verifyChecks";

/** 검사와 검사 묶음의 이름표와 풀이. 풀이의 백틱은 화면이 코드로 그린다. */
interface CheckText {
  title: string;
  description: string;
}

/** Backend API 목록·상세·만들기·다듬기와 스펙 검사 결과(VerdictPanel, lib/verifyChecks). */
export const backendApis = {
  /** 목록 열과 상세의 요약·표가 같이 쓰는 말. */
  kind: "종류",
  latestVersion: "최신 버전",
  created: "생성",
  /** 접속 정보 칸의 풀이. 상세의 접속 정보 카드와 만들기 폼이 같이 쓴다. */
  connection: {
    endpointHint: "이 백엔드를 실제로 부를 주소입니다. 스펙의 servers 대신 이 값을 사용합니다.",
    authHint: "백엔드를 부를 때 붙일 인증 방식입니다. none 이면 자격증명을 쓰지 않습니다.",
    /** 연결 칸 이름. API 필드라 두 언어 모두 영어다. */
    labels: {
      endpoint: "Endpoint",
      auth: "Auth",
      secret: "Credential Secret",
      soapHeadersSecret: "SOAP Header Secret",
    },
  },
  /** /backend-apis. */
  list: {
    create: "Backend API 만들기",
    columns: { name: "이름", tools: "Tool", endpoint: "엔드포인트", auth: "인증" },
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 Backend API 가 없습니다.`,
    empty: (tenant: string): string => `테넌트 ${tenant}에 Backend API 가 없습니다. 스펙을 업로드하면 그 Tool 로 MCP Server 를 만들 수 있습니다.`,
    emptyNew: "Backend API 만들기",
    title: "Backend APIs",
    search: "Search backend APIs…",
  },
  /** /backend-apis/:id. 접속 정보를 저장한 뒤의 알림은 shared.saved 다. */
  detail: {
    editDocs: "API 문서 편집",
    version: "버전",
    noVersion: "저장된 버전이 없습니다.",
    docsTitle: (version: number): string => `v${version} 의 OpenAPI 문서를 봅니다.`,
    openDocs: "API 문서 보기",
    sourceFormat: "원본 형식",
    updated: "수정",
    /** Tools 카드의 배지. 버전을 올리기 전에는 수를 모른다. */
    toolCount: (count: number | null): string => `${count ?? "-"}개`,
    noTools: "아직 버전을 올리지 않아 Tool 이 없습니다.",
    versionColumns: { format: "형식", check: "검사" },
    noVersions: "버전이 없습니다.",
    noResults: "검사 결과가 없습니다.",
    connectionTitle: "접속 정보",
    secretHint: "자격증명이 든 K8s Secret 이름입니다. 플랫폼은 이름만 저장하고 값은 읽지 않습니다.",
    soapHeadersHint:
      "WSDL 이 선언한 SOAP 헤더의 값을 soap_headers 키에 JSON 객체로 담은 K8s Secret 이름입니다. JSON 의 키는 헤더의 part 이름이나 element 이름입니다. 플랫폼은 이름만 저장하고 값은 읽지 않으며, 배포한 MCP Server 에는 다음 배포나 재배포 때 반영됩니다.",
    saveConnection: "접속 정보 저장",
    /** 읽기 토큰 카드. 회수 단추는 account.tokenTable.revoke 다. */
    issue: "발급",
    tokenColumns: { label: "라벨", scope: "범위", status: "상태" },
    noTokens: "토큰이 없습니다.",
    toolsTitle: "Tools",
    versionsTitle: "Versions",
    ok: "ok",
    fail: "fail",
    reportTitle: (version: number): string => `Verify report of v${version}`,
    pass: "pass",
    warn: "warn",
    tokensTitle: "Read tokens",
    tokenLabel: "토큰 라벨",
    labelPlaceholder: "라벨",
    active: "active",
    revoked: "revoked",
  },
  /** /backend-apis/new 와 /backend-apis/:id/edit. 이름 칸의 예시는 detail.nameFields.placeholders 이고, 처음 저장하는 단추는 shared.save 다. */
  new: {
    /** 제목 자리에서 표시 이름만 고치는 칸. */
    rename: {
      required: "표시 이름을 입력해 주세요.",
      start: "표시 이름 고치기",
      input: "표시 이름",
      save: "이름 저장",
      cancel: "이름 고치기 취소",
    },
    sourceLoading: "등록된 문서를 불러오는 중입니다…",
    registered: "등록된 문서",
    registeredNone: "아직 등록된 문서가 없습니다. 아래에서 처음 문서를 올립니다.",
    useRegistered: "편집기로 가져오기",
    /** 저장이 무엇을 올리는지. target 은 아래 셋 가운데 하나다. */
    saveTarget: (target: string): string => `저장 대상: ${target}`,
    target: {
      generated: "OpenAPI 로 변환한 결과 (아래 편집기의 내용)",
      file: (name: string): string => `올린 파일 ${name}`,
      text: "붙여넣은 스펙 텍스트",
    },
    noSaveTarget: "저장할 스펙이 없습니다. 파일을 올리거나 스펙을 붙여넣습니다.",
    newDocument: "새 문서",
    or: "또는 붙여넣기",
    specTextHint: "OpenAPI 나 WSDL 문서를 그대로 붙여 넣습니다. URL 이나 파일 대신 쓸 수 있습니다.",
    filenameHint:
      "붙여넣은 스펙 텍스트에 붙일 파일 이름입니다. 확장자로 형식을 알립니다. 비우면 감지한 형식에 따라 spec.yaml, spec.json, spec.wsdl 중 하나를 사용합니다. 파일을 올렸을 때는 사용하지 않습니다.",
    verifying: "검사 중…",
    verify: "검사하기",
    converting: "변환 중…",
    convert: "OpenAPI 로 변환",
    /** 두 단추의 풀이. verify 와 convert 는 단추 이름을 굵게 꾸민 요소다. */
    previewHint: (verify: ReactNode, convert: ReactNode): ReactNode => (
      <>
        {verify} 는 이 문서가 쓸 만한지와 어떤 Tool 이 나오는지를 보여 줍니다. {convert} 은 WSDL·Swagger 2.0 을 OpenAPI 3.x 로 바꿔 편집기에 올려, 저장 전에 손볼 수 있게 합니다. 바로 저장해도 서버가 같은 변환을 합니다.
      </>
    ),
    versionNote: (version: number): string => `저장하면 v${version}${particle(`v${version}`, "이/가")} 새로 생깁니다.`,
    generatedNote: "저장하면 이 편집기의 내용이 스펙으로 등록됩니다.",
    /** 같은 이름의 Backend API 가 이미 있을 때. link 는 그 Backend API 로 가는 링크다. */
    nameExists: (link: ReactNode, version: number): ReactNode => (
      <>
        이 이름의 Backend API 가 이미 있습니다. 저장하면 새 Backend API 가 아니라{" "}{link}{" "}의 v{version}{particle(`v${version}`, "이/가")} 되고, 저장된 Endpoint·Auth·Credential Secret 을 아래 값으로 덮어씁니다. 새로 만들려면 다른 이름을 적어 주세요.
      </>
    ),
    secretHint: "K8s Secret 이름",
    saveVersion: "새 버전으로 저장",
    saveNewVersion: "새 버전 저장",
    prettyView: "보기 좋게 들여쓰기",
    spec: "Spec",
    uploadFile: "Upload file",
    specText: "Spec text",
    verifyResult: "Verify result",
    format: "format",
    kind: "kind",
    mixed: "mixed",
    tools: "Tools",
    generated: "Generated OpenAPI",
    profile: "Backend API",
    title: "New Backend API",
    /** 새 Backend API 폼의 칸 이름. */
    labels: {
      filename: "파일 이름 힌트 (선택)",
    },
  },
  /** 다듬기(Refine)와 MCP Server 의 오버라이드 제안이 같이 쓰는 LLM 작업 화면의 말. */
  specJob: {
    back: "상세로 돌아가기",
    presets: "프리셋",
    run: "실행",
    running: "실행 중…",
    waiting: "결과를 기다리는 중입니다.",
    applying: "적용 중…",
    /** 프리셋 이름. 풀이는 작업마다 다르다. */
    preset: {
      enrich_descriptions: "설명 보강",
      korean_descriptions: "한국어 설명",
      fill_parameter_docs: "파라미터 설명 채우기",
      normalize_operation_ids: "operationId 정규화",
    } satisfies Record<RefinePreset, string>,
  },
  /** /backend-apis/:id/versions/:n/refine (D25). */
  refine: {
    enrich: {
      errors: {
        label: "오류 응답 설명을 본문으로 옮기기",
        hint: "4xx·5xx 응답의 설명을 operation description 에 넣습니다. 응답 정의를 덜어내도 그 설명은 남습니다.",
      },
      examples: { label: "예시를 본문으로 옮기기", hint: "example 값을 설명 문장으로 옮깁니다. 예시를 덜어내도 무엇을 넣는지는 남습니다." },
    } satisfies Record<MinifyEnrich, { label: string; hint: string }>,
    presetHint: {
      enrich_descriptions: "짧거나 없는 summary·description 을 채웁니다.",
      korean_descriptions: "설명을 한국어로 작성합니다.",
      fill_parameter_docs: "파라미터·속성의 description 을 채웁니다.",
      normalize_operation_ids: "operationId 를 일관된 이름으로 바꿉니다. 참조하는 MCP Server 는 재배포 때 확인이 필요합니다.",
    } satisfies Record<RefinePreset, string>,
    /** mode 는 다듬기 방식의 이름이고 version 은 새로 생길 버전이다. 버전을 모르면 null 이다. */
    subtitle: (mode: string, version: number | null): string => `${mode}. 채택한 항목만 새 버전 v${version ?? "?"} 에 들어갑니다.`,
    instant: "즉시",
    enrichLegend: "설명으로 옮기기 (enrich)",
    maxDescription: "설명 최대 길이 (max_description)",
    maxDescriptionHint: "비우면 자르지 않습니다.",
    maxDescriptionPlaceholder: "예: 300",
    dropExamples: "값 예시 지우기 (drop_value_examples)",
    dropExamplesHint: "스키마 값의 example 을 문서에서 뺍니다.",
    instructionPlaceholder: "예: 존댓말로 쓰고 예시 값은 넣지 않습니다.",
    llmNotice: "시작한 다듬기는 중간에 멈출 수 없습니다. 끝나면 결과가 아래에 나타납니다. 이 주소를 그대로 열면 새로고침하거나 다녀와도 이어서 봅니다.",
    unaccepted: "채택하지 않은 항목은 원본 그대로 남습니다.",
    apply: "새 버전으로 적용",
    readingSpec: "원본 스펙을 읽는 중입니다.",
    idle: "왼쪽에서 옵션을 선택하고 실행하면 원본과 채택 적용 결과를 좌우로 비교합니다.",
    /** LLM 작업의 반복 기록. */
    job: {
      creating: "job 을 만드는 중입니다.",
      batch: (batch: number, total: number): string => `묶음 ${batch}/${total}`,
      failed: (count: number): string => `실패 ${count}건`,
      passed: "통과",
      /** count 는 화면 언어로 적은 토큰 수다. */
      tokens: (count: string): string => `토큰 ${count}`,
      /** 앞의 몇 개만 적은 실패 id 뒤에 남은 수. */
      more: (count: number): string => `외 ${count}건`,
      waitingFirst: "첫 반복을 기다리는 중입니다.",
      noIterations: "반복 기록이 없습니다.",
    },
    llmJob: "LLM job",
  },
  /** verify·convert 응답의 verdict 와 저장된 버전의 검사 결과 표(components/VerdictPanel). */
  verdict: {
    columns: { level: "수준", code: "코드", count: "개수", message: "메시지" },
    noItems: "지적된 것이 없습니다.",
    /**
     * tool-args.case-collision 항목의 문장. 서버가 message 에 같은 한국어를 적고 args 에 Tool 이름과 인자 이름 목록을
     * 싣는다(platform/daegam/pipeline/verdict.py case_collision_items). names 는 인자 이름을 " · " 로 이은 것이다.
     */
    caseCollision: (tool: string, names: string): string =>
      `${tool}: 인자 ${names} 는 대소문자만 다릅니다. ToolHive 는 이 인자들을 함께 보낸 호출을 400 Invalid Request 로 거절합니다`,
    /** 검사 결과 표 머리의 한 줄. skip 이 0 이면 적지 않는다. */
    summary: (total: number, pass: number, warn: number, fail: number, skip: number): string =>
      `${total}개 검사 중 pass ${pass} · warn ${warn} · fail ${fail}${skip ? ` · skip ${skip}` : ""}`,
    toolCount: (count: number): string => `이 스펙으로 MCP Server 를 만들면 Tool ${count}개가 생깁니다.`,
    /** 펼침 칸의 머리. 낭독기만 읽는다. */
    expand: "펼치기",
    checkColumns: { check: "검사", status: "상태", severity: "실패 시", message: "메시지" },
    notApplicable: "해당 없음(REST 스펙)",
    /** 펼침 단추의 접근 이름. title 은 검사 이름이다. */
    details: (title: string): string => `${title} 상세`,
    severityTitle: {
      error: "이 검사가 실패하면 등록이 거부됩니다.",
      warn: "이 검사가 실패하면 경고로 남습니다.",
    } satisfies Record<CheckSeverity, string>,
    refs: "참고",
    /** 길어서 자른 data 미리보기. omitted 는 자른 글자 수다. */
    truncated: (preview: string, omitted: number): string => `${preview}\n… (${omitted}자 생략)`,
    noRoundtripTools: "생성된 Tool 이 없습니다.",
    /** Tool 이름 태그(ToolList). 파라미터 열은 detail.openApiDocs.parameters 다. */
    noTools: "Tool 이 없습니다.",
    toolTitle: "API 문서에서 이 Tool 을 봅니다.",
    moreTools: (count: number): string => `외 ${count}개`,
    title: "Verdict",
    location: "location",
  },
  /**
   * verify 리포트 검사의 이름표와 풀이 (lib/verifyChecks). 키는 platform/daegam/pipeline/verdict.py 의 CHECK_LEVELS 이고,
   * 묶음과 심각도는 lib 가 갖는다. 풀이는 합니다체다.
   */
  checks: {
    severity: { error: "등록 거부", warn: "경고" } satisfies Record<CheckSeverity, string>,
    group: {
      document: { title: "document", description: "문서 자체가 OpenAPI 3.x 로 읽히는지 봅니다." },
      "tool-name": { title: "Tool 이름", description: "operationId 가 MCP Tool 이름이 됩니다. 있는지, 규칙에 맞는지, 겹치지 않는지 봅니다." },
      "tool-description": { title: "Tool 설명", description: "LLM 이 Tool 을 선택할 때 쓰는 문장이 있는지 봅니다." },
      "x-soap": { title: "SOAP 계약", description: "WSDL 에서 변환된 스펙의 SOAP 계약을 브리지가 다룰 수 있는지 봅니다." },
      deep: { title: "심층 검사", description: "스키마 검증과 FastMCP 로 실제 서버를 만들어 보는 검사입니다. 의존성이 없으면 skip 입니다." },
      other: { title: "기타", description: "사전에 없는 검사입니다." },
    } satisfies Record<CheckGroup, CheckText>,
    check: {
      "document.mapping": { title: "문서 최상위 객체", description: "문서 최상위가 객체인지 봅니다." },
      "document.openapi3": {
        title: "OpenAPI 3.x 문서",
        description: "`openapi: 3.x` 문서인지 봅니다. Swagger 2.0 과 WSDL 은 변환한 뒤에 검사합니다.",
      },
      "document.has-paths": { title: "paths 존재", description: "`paths` 가 있고 비어 있지 않은지 봅니다." },
      "document.has-operations": { title: "오퍼레이션 존재", description: "HTTP 오퍼레이션이 하나 이상 있는지 봅니다." },
      "tool-name.present": {
        title: "operationId 존재",
        description: "모든 오퍼레이션에 `operationId` 가 있는지 봅니다. 이 값이 MCP Tool 이름이 됩니다.",
      },
      "tool-name.safe": { title: "이름 규칙", description: "Tool 이름이 MCP 규칙(`[A-Za-z0-9_-]`, 64자 이내)에 맞는지 봅니다." },
      "tool-name.normalized": { title: "정규화 영향 없음", description: "FastMCP 가 이름을 정규화하며 바꾸지 않는지 봅니다." },
      "tool-name.unique": { title: "이름 중복 없음", description: "Tool 이름이 서로 겹치지 않는지 봅니다." },
      "tool-name.normalization-collision": { title: "정규화 뒤 충돌 없음", description: "정규화한 뒤에 겹치는 이름이 생기지 않는지 봅니다." },
      "tool-description.present": {
        title: "설명 존재",
        description: "오퍼레이션마다 summary 또는 description 이 있는지 봅니다. LLM 이 Tool 을 선택할 때 쓰는 문장입니다.",
      },
      "x-soap.version": { title: "SOAP 버전", description: "soapVersion 이 유효한지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다." },
      "x-soap.endpoint": { title: "SOAP 엔드포인트", description: "SOAP 엔드포인트가 있는지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다." },
      "x-soap.input-element": {
        title: "요청 봉투 루트",
        description: "요청 봉투의 루트 엘리먼트가 정해져 있는지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "x-soap.output-element": {
        title: "응답 봉투 루트",
        description: "응답 봉투의 루트 엘리먼트가 정해져 있는지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "x-soap.refs": {
        title: "$ref 해석",
        description: "SOAP 계약 안의 `$ref` 가 모두 해석되는지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "x-soap.choice": {
        title: "XSD choice",
        description: "XSD choice 가 브리지가 다룰 수 있는 형태인지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "x-soap.substitution": {
        title: "XSD substitution group",
        description: "XSD substitution group 이 브리지가 다룰 수 있는 형태인지 봅니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "x-soap.mixed-rest": {
        title: "SOAP·REST 혼합 없음",
        description: "SOAP 와 REST 오퍼레이션이 섞이지 않았는지 봅니다. 섞이면 등록이 거부됩니다. REST 스펙에서는 검사 대상이 없어 pass 입니다.",
      },
      "openapi.schema-valid": { title: "OpenAPI 스키마 검증", description: "OpenAPI 3.x 스키마 검증을 통과하는지 봅니다." },
      "fastmcp.roundtrip": {
        title: "FastMCP 서버 생성",
        description: "FastMCP 로 서버를 실제로 만들어 Tool 목록이 나오는지 봅니다. 상세의 tools 에 Tool 별 이름과 파라미터가 있습니다.",
      },
      "fastmcp.tool-materialized": { title: "MCP tools/list 실체화", description: "그 Tool 이 MCP `tools/list` 형식으로 실체화되는지 봅니다." },
    } satisfies Record<CheckId, CheckText>,
  },
};
