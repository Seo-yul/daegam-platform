import type { ReactNode } from "react";

import { particle } from "@/lib/particle";

/** Knowledge Base 의 목록, 폼, 상세, 문서 카드와 삭제 대화상자 (F6). 검색어와 맞는 것이 없을 때는 detail.listControls.noMatch 다. */
export const knowledgeBases = {
  /** 이름 칸 예시. 새로 만들기와 상세 편집이 같이 쓴다. */
  placeholders: { display: "제품 FAQ", name: "product-faq" },
  /** 토큰 수에 손을 올리면 보이는 말. 상세의 요약과 문서 표가 같이 쓴다. */
  tokensHint: "tiktoken cl100k_base 로 센 추정치입니다.",
  /** /knowledge-bases. Database 칸은 종류 이름(resources.kind)이다. */
  list: {
    create: "Knowledge Base 만들기",
    columns: { name: "이름", embedding: "임베딩", documents: "문서", state: "상태" },
    empty: (tenant: string): string => `테넌트 ${tenant}에 Knowledge Base 가 없습니다.`,
    title: "Knowledge Base",
    search: "Search knowledge bases…",
  },
  /** Chunk size·Overlap·Max chunks 칸(ChunkingFields). 새로 만들기와 상세 편집이 같이 쓴다. */
  chunking: {
    sizeHint: (min: number, max: number): string => `글자 수 (${min}..${max}).`,
    overlapHint: "앞 청크의 끝을 겹치는 글자 수 (0..size/2).",
    maxChunksHint: "Knowledge Base 전체 청크 상한.",
    size: "Chunk size",
    overlap: "Overlap",
    maxChunks: "Max chunks",
  },
  /** 새로 만들기. 만들기 단추는 shared.create 다. */
  form: {
    dataServiceHint: "벡터 테이블을 만들 Database 입니다. 만든 뒤 바꿀 수 없습니다.",
    dataServicesError: "Database 목록을 불러오지 못했습니다. 등록해 둔 Postgres 가 있어도 여기에 보이지 않습니다. 새로고침한 뒤 다시 시도해 주세요.",
    noPostgres: "이 테넌트에 Postgres Database 가 없습니다. Tenant › Database 에서 먼저 등록해 주세요.",
    pgvectorMissing:
      "이 Database 는 마지막 연결 확인에서 pgvector 확장을 찾지 못했습니다. 슈퍼유저가 먼저 CREATE EXTENSION vector 를 실행해야 만들 수 있습니다.",
    pgvectorAvailable:
      "이 Database 에 pgvector 확장이 아직 설치되지 않았습니다. 만들 때 CREATE EXTENSION vector 를 시도하며, 이 계정이 슈퍼유저가 아니면 그때 실패합니다.",
    /** link 는 Database 상세로 가는 링크이고 글자는 unverifiedLink 다. */
    unverified: (link: ReactNode): ReactNode => (
      <>
        이 Database 는 아직 연결을 확인하지 않았습니다.{" "}
        {link}
        에서 연결 확인을 먼저 누르면 pgvector 가 있는지 여기서 미리 알 수 있습니다.
      </>
    ),
    unverifiedLink: "Database 상세",
    providerHint: "Platform Settings 의 provider 중 임베딩 API 가 있는 것(openai 계열)만 보입니다. 만든 뒤 바꿀 수 없습니다.",
    settingsError: "provider 목록을 불러오지 못했습니다. 등록해 둔 provider 가 있어도 여기에 보이지 않습니다. 새로고침한 뒤 다시 시도해 주세요.",
    noProvider: "임베딩을 지원하는 provider 가 없습니다. Platform Settings 에서 openai provider 를 등록해 주세요.",
    modelHint: "비우면 provider 의 임베딩 모델입니다.",
    dimensionsRequired: "모르는 모델입니다. 차원을 입력해야 합니다.",
    /** 알려진 모델이면 그 차원이 기본값이다. */
    dimensionsDefault: (dimensions: number): string => `비우면 ${dimensions} 입니다.`,
    dimensionsPick: "모델을 선택하면 채워집니다.",
    creating: "만드는 중…",
    title: "New Knowledge Base",
    dataService: "Database (Postgres)",
    provider: "Embedding provider",
    model: "Embedding model",
    dimensions: "Dimensions",
  },
  /** 상세의 머리, 요약, 편집. 저장 단추는 shared.save 다. */
  detail: {
    backToList: "Knowledge Base 목록으로 돌아가기",
    reindexAllTitle: "모든 문서를 pending 으로 되돌립니다.",
    requesting: "요청 중…",
    reindexAll: "전체 다시 인덱싱",
    /** 삭제가 시작된 Knowledge Base. at 은 화면 언어로 적은 시각이고, orphaned 는 벡터 테이블이 남았는가다. */
    deleting: (at: string, orphaned: boolean): string =>
      `이 Knowledge Base 는 삭제 중입니다(${at}). 문서를 추가하거나 다시 인덱싱할 수 없습니다.${orphaned ? " 벡터 테이블을 지우지 못해 남아 있습니다. 삭제를 다시 누르면 같은 순서로 다시 시도합니다." : ""}`,
    dataServicesError: "Database 목록을 불러오지 못해 이름을 찾지 못했습니다.",
    tokens: (tokens: number): string => `${tokens} (추정)`,
    chunkingNotice: "chunking 을 바꾸면 저장할 때 모든 문서를 다시 인덱싱합니다. 인덱싱 중인 문서가 있으면 저장이 거부됩니다(409 indexing).",
    /** Summary 카드의 칸 이름. */
    labels: {
      table: "table",
      embedding: "embedding",
      database: "Database",
      documents: "documents",
      indexed: (indexed: number, total: number): string => `${total}개 중 ${indexed}개 인덱싱됨`,
      chunks: "chunks",
      tokens: "tokens",
      lastIndexed: "last indexed",
      edit: "Edit",
    },
  },
  /** 동작이 끝난 뒤의 알림. 상태에는 갈래와 값을 두고 그릴 때 문장을 만든다. 바뀐 것 없이 저장한 알림은 shared.saved 다. */
  notice: {
    reindexAll: (count: number): string => `${count}개 문서를 다시 인덱싱합니다.`,
    uploaded: (name: string): string => `"${name}"${particle(name, "을/를")} 업로드했습니다. 인덱서가 곧 처리합니다.`,
    added: (name: string): string => `"${name}"${particle(name, "을/를")} 추가했습니다. 인덱서가 URL 을 받아 처리합니다.`,
    reindexDoc: (name: string): string => `"${name}"${particle(name, "을/를")} 다시 인덱싱합니다.`,
    savedReindex: "저장했습니다. chunking 이 바뀌어 모든 문서를 다시 인덱싱합니다.",
  },
  /** 상세의 문서 카드(DocumentsCard). */
  documents: {
    /** 카드 머리의 한도. formats 는 받는 형식 이름들이다. */
    limits: (formats: string, mb: number, max: number): string => `${formats}, 문서당 ${mb}MB, Knowledge Base 당 ${max}개`,
    /** 한도 뒤에 붙는 폴링 안내라 앞에 구분자가 있다. */
    polling: (seconds: number): string => ` · 인덱싱이 끝날 때까지 ${seconds}초마다 자동으로 새로고침합니다`,
    upload: "파일 업로드",
    uploadHint: "선택하면 바로 올라갑니다.",
    /** 크기 한도를 넘은 파일 안내(lib/files.ts fileTooLargeText)의 조각. */
    sizeLimit: (mb: number): string => `문서 하나는 ${mb}MB`,
    splitAdvice: "나눠서 업로드해 주세요.",
    uploading: (name: string): string => `"${name}"${particle(name, "을/를")} 업로드하는 중입니다…`,
    addUrl: "URL 추가",
    urlHint: "공개 주소만 됩니다. 사설·클러스터 주소는 거부됩니다.",
    nameHint: "비우면 URL 의 마지막 조각입니다.",
    adding: "추가 중…",
    stale: "문서 목록을 새로 읽지 못했습니다. 아래는 마지막으로 읽은 목록이라 지금과 다를 수 있습니다.",
    failedNotice: '실패한 문서는 자동으로 다시 시도하지 않습니다. 고친 뒤 "다시 인덱싱" 을 눌러 주세요.',
    /** 문서 표의 머리. name 은 URL 추가 폼의 이름 칸도 쓴다. */
    columns: { name: "이름", source: "출처", size: "크기", state: "상태", chunks: "청크", tokens: "토큰 (추정)", error: "오류" },
    empty: "아직 문서가 없습니다. 파일을 업로드하거나 URL 을 추가해 주세요.",
    reindex: "다시 인덱싱",
    deleteAgain: "다시 삭제",
    deleteTitle: "문서 삭제",
    deleteBody: "벡터 테이블의 행과 본문이 함께 삭제됩니다. 워커가 처리하므로 잠시 deleting 으로 남습니다.",
    title: "Documents",
  },
  /** 삭제 확인 대화상자(DeleteDialog). */
  deleteDialog: {
    dropTable: (table: string): string => `Database 의 ${table} 테이블을 DROP 합니다.`,
    /** 200 이어도 테이블이 남았을 때. warning 은 서버가 준 코드다. */
    tableNotDropped: (warning: string): string =>
      `벡터 테이블을 지우지 못해 Knowledge Base 가 남았습니다(${warning}). Database 에 연결되는지 확인한 뒤 삭제를 다시 눌러 주세요.`,
  },
};
