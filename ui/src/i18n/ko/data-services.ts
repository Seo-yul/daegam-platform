/**
 * Database 의 목록, 폼, 상세와 연결 확인 (F6). Database 가 리소스 이름이라 그 안의 데이터베이스 이름과 Redis 의 db 번호는
 * 필드 이름 대신 "DB 이름", "DB 번호" 로 부른다 (docs/guide/ui.md 3절).
 */
export const dataServices = {
  /** 이름 칸 예시. 새로 만들기와 상세 편집이 같이 쓴다. */
  placeholders: { display: "주문 DB", name: "orders-db" },
  /** /data-services. 검색어와 맞는 것이 없을 때는 detail.listControls.noMatch 다. */
  list: {
    create: "Database 만들기",
    columns: { name: "이름", kind: "종류", host: "호스트", database: "DB 이름·번호", verified: "확인" },
    empty: (tenant: string): string => `테넌트 ${tenant}에 Database 가 없습니다.`,
    title: "Database",
    search: "Search databases…",
  },
  /** 접속 정보 칸(ConnectionFields). 새로 만들기와 상세 편집이 같이 쓴다. */
  connection: {
    hostHint: "플랫폼과 에이전트가 클러스터 안에서 이 Database 에 접속하는 주소입니다. 보통 Service DNS 를 사용합니다.",
    portHint: "Postgres 는 보통 5432, Redis 는 6379 입니다.",
    database: "DB 이름",
    databaseHint: "Postgres 의 데이터베이스 이름입니다. Redis 는 쓰지 않습니다.",
    db: "DB 번호",
    dbHint: "Redis 의 DB 번호입니다. Agent 의 memory 로 쓰려면 0 이어야 합니다. RediSearch 가 db 0 에서만 인덱스를 만듭니다.",
    usernameHint: "접속 계정입니다. 비밀번호는 K8s Secret 에 들어가고 여기에 다시 보이지 않습니다.",
    host: "Host",
    port: "Port",
    username: "Username",
  },
  /** Redis 의 TLS 끝점 칸(TlsFields, P61). 새로 만들기와 상세 편집이 같이 쓴다. */
  tls: {
    enable: "TLS 포트 사용",
    enableHint:
      "Redis 가 평문 포트와 함께 TLS 포트도 열어 두었다면 켜 주세요. 플랫폼은 TLS 포트로만 접속하고, Agent 는 TLS 를 사용할 수 있게 되면, MCP Server 는 다음 배포 때 TLS 포트로 옮겨 갑니다.",
    port: "TLS port",
    portHint: "TLS 로 접속하는 포트입니다. 위 Port 는 아직 평문으로 접속하는 Agent 와 MCP Server 가 계속 사용하므로 그대로 두어 주세요.",
    ca: "CA certificate",
    caHint: "서버 인증서를 발급한 CA 의 PEM 입니다. K8s Secret 에 들어가며, 접속할 때 이 CA 로 서버 인증서와 Host 이름을 확인합니다.",
    caKeepHint: "비우면 CA 를 바꾸지 않습니다. 새 CA 로 바꿀 때만 PEM 을 넣어 주세요.",
  },
  /** 새로 만들기. 저장 단추는 shared.save 다. */
  form: {
    kindHint: "Postgres 나 Redis 입니다. 종류는 만든 뒤 바꿀 수 없습니다.",
    passwordHint: "인증이 없는 Redis 는 비워 두세요.",
    title: "New Database",
    kind: "Kind",
    kinds: { postgres: "postgres", redis: "redis" },
    cluster: "Cluster",
    password: "Password",
  },
  /** 상세. 저장 단추는 shared.save, 저장한 뒤의 알림은 shared.saved 다. */
  detail: {
    backToList: "Database 목록으로 돌아가기",
    verify: "연결 확인",
    verifying: "확인 중…",
    staleCapabilities: "지난 확인의 결과입니다. 그 뒤 접속 정보가 바뀌었으니 연결 확인을 다시 눌러 주세요.",
    passwordHint: "비우면 바꾸지 않습니다.",
    /** username 이나 password 를 바꿔 서버가 verified_at 을 지웠을 때. */
    savedCredentials: "저장했습니다. 접속 정보가 바뀌어 확인 상태를 지웠습니다. 연결 확인을 다시 눌러 주세요.",
    /** Summary 카드의 칸 이름. */
    labels: {
      cluster: "cluster",
      secret: "secret",
      password: "password",
      passwordSet: "set",
      passwordNone: "none",
      tls: "tls",
      tlsNone: "none",
      tlsEndpoint: (port: number, secret: string): string => `port ${port} · CA ${secret}`,
      plainClosed: "plain port closed",
      plainConsumers: "plain consumers",
      plainConsumersNone: "평문 포트로 접속하는 Agent 와 MCP Server 가 없습니다.",
      plainConsumersCount: (agents: number, servers: number): string =>
        `${[agents ? `Agent ${agents}개` : "", servers ? `MCP Server ${servers}개` : ""].filter(Boolean).join("와 ")}가 아직 평문 포트로 접속합니다. 소유자에게 알린 뒤 다시 배포해 주세요.`,
      /** 줄 가운데 runtime_image 가 있을 때만 붙는다. 다시 배포해도 이미지가 옛 판이면 평문에 남는다 */
      plainConsumersImage: "Agent 는 클러스터의 에이전트 이미지가 새 판이어야 TLS 포트로 옮겨 갑니다.",
      capabilities: "capabilities",
      edit: "Edit",
      newPassword: "New password",
    },
  },
  /**
   * 연결 확인(verify) 결과. 오류 사전에 없거나 이 화면에서 할 일을 말해야 하는 코드만 둔다(pages/data-services/verifyText.ts).
   * 사용자가 할 일은 `~해 주세요` 다.
   */
  verify: {
    /** 코드에 맞는 문장이 없을 때. */
    failed: "연결에 실패했습니다.",
    /** postgres·redis 가 함께 쓰는 갈래. */
    common: {
      auth_failed: "인증에 실패했습니다. Username 과 New password 를 다시 넣고 저장한 뒤 다시 확인해 주세요.",
    },
    postgres: {
      query_failed: "접속은 됐지만 질의가 거부됐습니다. verify 계정이 확장 목록을 읽을 수 있는지 확인해 주세요.",
    },
    redis: {
      select_failed: "DB 번호가 이 Redis 의 범위를 넘습니다. DB 번호를 낮추거나 서버의 databases 설정을 늘려 주세요.",
      ping_failed: "접속은 됐지만 PING 이 거부됐습니다. verify 계정의 권한을 확인해 주세요.",
      io_error: "접속 도중 통신이 끊겼습니다. 네트워크 경로와 TLS 설정을 확인해 주세요.",
      tls_failed:
        "TLS 인증서 검증에 실패했습니다. CA certificate 가 서버 인증서를 발급한 CA 인지, 서버 인증서에 Host 이름이 들어 있는지 확인해 주세요.",
      ca_missing: "CA 인증서를 담은 Secret 이 없습니다. CA certificate 를 다시 넣고 저장한 뒤 다시 확인해 주세요.",
    },
    /** redis 응답 한 줄의 조각. lib/dataServices.ts 가 " · " 로 잇는다. modules 는 ", " 로 이은 모듈 이름이다. */
    noVersion: "Redis 버전 확인 권한 없음",
    modules: (modules: string): string => `모듈 ${modules}`,
  },
  /** verify 가 남긴 capabilities 의 칩과 그 아래 설명 (D32 pgvector, D33 checkpointer). */
  capability: {
    /** verify 전이거나 표에 없는 값. 두 capability 가 같이 쓴다. */
    unverified: "연결 확인이 필요합니다",
    /** redis 의 checkpointer 칩. memory 노드 셀렉트 아래와 Database 상세가 쓴다. */
    checkpointer: {
      ready: "Redis 8 준비됨",
      dbNotZero: (db: number): string => `db ${db}에서는 사용할 수 없습니다`,
      modules_missing: "RediSearch·RedisJSON 이 없습니다",
      unknown: "연결 확인에 사용한 계정에 권한이 없습니다",
    },
    pgvector: { installed: "pgvector 있음", available: "pgvector 설치 가능", missing: "pgvector 없음" },
    /** 칩만으로는 보이지 않는 "그래서 무엇을 하라는 것인가". */
    note: {
      checkpointer: {
        dbNotZero: (db: number): string =>
          `지금 db 가 ${db}입니다. RediSearch 는 db 0 에서만 인덱스를 만들므로 Agent 의 memory 로 사용할 수 없습니다. 위 DB 번호를 0 으로 바꾸면 됩니다. 캐시나 세션 공유로만 사용한다면 이대로도 됩니다.`,
        modules_missing:
          "Agent 의 memory 로 쓰려면 RediSearch 와 RedisJSON 이 있는 Redis 8 이어야 합니다(redis:8 이미지가 둘을 번들합니다). 캐시나 세션 공유로만 쓴다면 이대로도 됩니다.",
        unknown: "verify 계정이 MODULE LIST 와 FT._LIST 를 부르지 못했습니다. 권한을 주거나 다른 계정으로 확인해 주세요.",
        unverified: "아직 확인하지 않았습니다. 연결 확인을 누르면 Agent 의 memory 로 쓸 수 있는지 알 수 있습니다.",
      },
      pgvector: {
        missing: "Knowledge Base 를 만들려면 pgvector 확장이 있어야 합니다. 슈퍼유저에게 CREATE EXTENSION vector 를 먼저 실행해 달라고 요청해 주세요.",
        available:
          "확장이 설치되지는 않았지만 설치할 수 있습니다. Knowledge Base 를 만들 때 CREATE EXTENSION 을 시도하며, 이 계정이 슈퍼유저가 아니면 그때 실패합니다.",
        unverified: "아직 확인하지 않았습니다. 연결 확인을 누르면 Knowledge Base 를 만들 수 있는지 알 수 있습니다.",
      },
    },
  },
  /** 연결 확인 여부. 목록과 상세가 같이 쓴다. */
  status: {
    verified: "verified",
    unverified: "unverified",
  },
};
